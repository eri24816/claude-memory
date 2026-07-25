"""A persistent process that keeps the embedding model warm.

Every hook invocation is a fresh `python -m claude_memory.hooks ...` process,
and fastembed cold-starting its ONNX runtime costs about 1.2s -- paid again on
every single message, not just the first, because argv processes share nothing.
This process loads the model once via retrieval.search() and answers hook
requests over localhost HTTP for as long as it runs. Eric asked for it to
persist, so there is no idle timeout: it runs until `daemon stop` or reboot.

Database connections are still opened fresh per request (cheap -- SQLite open
plus WAL is milliseconds) rather than held across requests, so the daemon adds
no read-consistency question beyond what every other caller already has. The
only state this process caches is the model.

Discovery is a JSON file next to the database (port + a liveness ping, not a
pid): the port is ephemeral because a fixed port collides with unrelated local
tools, and hooks are fresh processes with nothing else to remember it by.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from . import db

SERVICE_TAG = "claude-memory-daemon"


def _discovery_path() -> Path:
    return db.DEFAULT_DB_PATH.parent / "daemon.json"


def _read_discovery() -> dict | None:
    try:
        return json.loads(_discovery_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _ping(port: int, timeout: float = 0.3) -> bool:
    """Confirm a live claude-memory daemon, not just something on the port.

    The port is ephemeral and ours alone almost always, but checking the
    service tag rather than trusting any 200 response costs nothing and rules
    out the rare case of a stale discovery file pointing at an unrelated
    process that happens to have been assigned the same port since.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=timeout) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read().decode("utf-8"))
            return body.get("service") == SERVICE_TAG
    except (OSError, ValueError):
        return False


def discover_running() -> int | None:
    """Return a live daemon's port, or None if nothing usable answers."""
    info = _read_discovery()
    if not info or "port" not in info:
        return None
    return info["port"] if _ping(info["port"]) else None


def ensure_running() -> None:
    """Start the daemon in the background if nothing is already answering.

    Fire-and-forget: the caller does not wait for the model to load, only for
    the process to exist. Called from session_start() so warmup happens while
    Eric is still reading the autoloaded block, hidden behind however long
    that takes -- by the time his first message reaches user_prompt_submit(),
    the daemon has had a head start on the cold start it would otherwise pay.
    """
    if discover_running() is not None:
        return

    if os.environ.get("CLAUDE_MEMORY_NO_DAEMON"):
        # An opt-out for anything that runs the hooks many times over throwaway
        # stores -- the test suite above all. The daemon is deliberately
        # immortal (no idle timeout) and discovers itself through a file beside
        # the store, so a caller that uses a fresh store per invocation never
        # finds the previous one and spawns another every time. That leaked ~100
        # orphaned daemons holding 3.8 GB before anyone noticed, and it degrades
        # the developer's machine rather than anything the suite can see fail.
        return

    command = [sys.executable, "-m", "claude_memory.daemon", "serve"]
    kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP get it off this
        # process's console; CREATE_BREAKAWAY_FROM_JOB is what actually lets
        # it outlive the hook if Claude Code runs hooks inside a Windows Job
        # Object with kill-on-close (a common way to guarantee hook cleanup).
        # Breakaway fails outright if the job doesn't permit it, so fall back
        # rather than let that failure take the whole spawn down with it.
        base_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        try:
            subprocess.Popen(command, creationflags=base_flags | breakaway, **kwargs)
            return
        except OSError:
            pass
        subprocess.Popen(command, creationflags=base_flags, **kwargs)
    else:
        subprocess.Popen(command, start_new_session=True, **kwargs)


def stop() -> int | None:
    """Terminate the daemon if one is registered. Returns its pid, or None."""
    info = _read_discovery()
    if info is None:
        return None
    _terminate(info["pid"])
    try:
        _discovery_path().unlink()
    except OSError:
        pass
    return info["pid"]


def request(command: str, payload: dict, timeout: float = 12.0) -> dict | None:
    """Forward a hook call to a running daemon.

    None means "no daemon to ask" -- the caller falls back to doing the work
    in-process for this one call, which is why this never raises.
    """
    port = discover_running()
    if port is None:
        return None
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/{command}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # the stdlib default writes every request to stderr of a daemon with none

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path == "/ping":
            self._respond(200, {"service": SERVICE_TAG, "ok": True})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        from . import hooks

        handlers = {
            "session-start": hooks.build_session_start_context,
            "user-prompt-submit": hooks.build_user_prompt_context,
        }
        builder = handlers.get(self.path.lstrip("/"))
        if builder is None:
            self._respond(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}

        connection = db.connect()
        try:
            context = builder(payload, connection)
        finally:
            connection.close()
        self._respond(200, {"additionalContext": context})

    def _respond(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def serve() -> None:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    path = _discovery_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    try:
        server.serve_forever()
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    command = arguments[0] if arguments else "status"

    if command == "serve":
        serve()
        return 0

    if command == "start":
        already = discover_running() is not None
        ensure_running()
        print("already running" if already else "starting")
        return 0

    if command == "stop":
        pid = stop()
        print(f"stopped pid {pid}" if pid else "not running")
        return 0

    if command == "status":
        port = discover_running()
        print(f"running on port {port}" if port else "not running")
        return 0

    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
