"""The persistent daemon, exercised as a real subprocess against a scratch DB.

Never point these at the real store: ensure_running() spawns a genuine
detached process, so a stray run here must not end up serving -- or writing
its discovery file next to -- Eric's actual memory.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from claude_memory import daemon, db, store


@pytest.fixture
def scratch_db(tmp_path, monkeypatch):
    """Point the whole module at a throwaway path, in-process and in children.

    monkeypatch covers this process; the env var covers ensure_running()'s
    subprocess, which re-reads CLAUDE_MEMORY_DB at its own import time.
    """
    path = tmp_path / "memory.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    monkeypatch.setenv("CLAUDE_MEMORY_DB", str(path))
    # This module is the one place that spawns real daemons, so it opts back in
    # to what conftest disables for everything else.
    monkeypatch.delenv("CLAUDE_MEMORY_NO_DAEMON", raising=False)

    connection = db.connect(path)
    connection.close()
    yield path

    # Wait for registration before stopping. ensure_running() is fire-and-forget,
    # so teardown usually arrives before the daemon has written its discovery
    # file -- stop() then finds nothing to stop, and the daemon, which has no
    # idle timeout, survives the test run forever. That is how ~100 orphaned
    # daemons accumulated holding 3.8 GB. Bounded, because most of these tests
    # never spawn one and must not pay a full timeout each.
    _wait_for(lambda: daemon.discover_running() is not None, timeout=3.0)
    daemon.stop()


def _wait_for(predicate, timeout=8.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def test_ensure_running_starts_a_daemon_that_answers_ping(scratch_db):
    assert daemon.discover_running() is None
    daemon.ensure_running()
    port = _wait_for(daemon.discover_running)
    assert port is not None
    assert daemon._ping(port)


def test_ensure_running_is_a_noop_once_up(scratch_db):
    daemon.ensure_running()
    port = _wait_for(daemon.discover_running)
    discovery_before = json.loads(daemon._discovery_path().read_text(encoding="utf-8"))

    daemon.ensure_running()
    time.sleep(0.3)
    discovery_after = json.loads(daemon._discovery_path().read_text(encoding="utf-8"))
    assert discovery_before == discovery_after, "a second ensure_running spawned another process"


def test_request_matches_the_in_process_path(scratch_db):
    """Regression target: daemon-backed retrieval must return exactly what the
    direct path returns, since user_prompt_submit() falls back to direct
    whenever the daemon is not yet up -- the two paths have to agree."""
    connection = db.connect(scratch_db)
    store.remember(connection, [{
        "claim": "Daemon parity fact",
        "detail": "A fact that must retrieve identically whether the daemon or the direct path answers.",
        "type": "fact", "about_user": False, "window_start": "2026-07-20",
    }])
    connection.close()

    from claude_memory import hooks

    payload = {"prompt": "what does the daemon parity fact verify", "cwd": str(scratch_db.parent)}

    direct_connection = db.connect(scratch_db)
    try:
        direct_block = hooks.build_user_prompt_context(payload, direct_connection)
    finally:
        direct_connection.close()

    daemon.ensure_running()
    assert _wait_for(daemon.discover_running) is not None
    result = _wait_for(lambda: daemon.request("user-prompt-submit", payload))
    assert result is not None
    assert result["additionalContext"] == direct_block
    assert "Daemon parity fact" in direct_block


def test_second_request_is_faster_than_the_first(scratch_db):
    """The whole point: the first request pays fastembed's cold start inside
    the daemon: every request after should not pay it again."""
    connection = db.connect(scratch_db)
    store.remember(connection, [{
        "claim": "Warmup timing fact",
        "detail": "A fact used only to give the daemon something to embed against for timing.",
        "type": "fact", "about_user": False, "window_start": "2026-07-20",
    }])
    connection.close()

    daemon.ensure_running()
    assert _wait_for(daemon.discover_running) is not None
    payload = {"prompt": "warmup timing fact query", "cwd": str(scratch_db.parent)}

    start = time.monotonic()
    first = daemon.request("user-prompt-submit", payload, timeout=15.0)
    first_elapsed = time.monotonic() - start
    assert first is not None

    start = time.monotonic()
    second = daemon.request("user-prompt-submit", payload, timeout=15.0)
    second_elapsed = time.monotonic() - start
    assert second is not None

    assert second_elapsed < first_elapsed / 2


def test_stop_terminates_and_clears_discovery(scratch_db):
    daemon.ensure_running()
    assert _wait_for(daemon.discover_running) is not None

    pid = daemon.stop()
    assert pid is not None
    assert not daemon._discovery_path().exists()
    assert daemon.discover_running() is None


def test_discover_running_ignores_a_stale_discovery_file(scratch_db):
    """A discovery file can outlive the process it names -- a crash, a kill
    from outside daemon stop. Nothing on that port should read as 'running'."""
    daemon._discovery_path().parent.mkdir(parents=True, exist_ok=True)
    daemon._discovery_path().write_text(
        json.dumps({"port": 1, "pid": 999999}), encoding="utf-8"
    )
    assert daemon.discover_running() is None


def test_request_returns_none_with_no_daemon(scratch_db):
    assert daemon.request("user-prompt-submit", {"prompt": "anything"}) is None


def test_user_prompt_submit_hook_uses_the_daemon_once_warm(scratch_db):
    """End to end through the actual hook entry point as a subprocess -- what
    session-start's warmup is optimizing for -- not the daemon module directly."""
    import subprocess
    import sys

    connection = db.connect(scratch_db)
    store.remember(connection, [{
        "claim": "Hook-path daemon fact",
        "detail": "Verifies user_prompt_submit() reaches the daemon rather than always falling back.",
        "type": "fact", "about_user": False, "window_start": "2026-07-20",
    }])
    connection.close()

    daemon.ensure_running()
    assert _wait_for(daemon.discover_running) is not None
    # Prime the model so the timed call below measures the daemon path, not a cold load.
    daemon.request("user-prompt-submit", {"prompt": "priming", "cwd": str(scratch_db.parent)})

    payload = json.dumps({
        "prompt": "hook-path daemon fact check", "cwd": str(scratch_db.parent),
    })
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "claude_memory.hooks", "user-prompt-submit"],
        input=payload, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "CLAUDE_MEMORY_DB": str(scratch_db)},
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Hook-path daemon fact" in context
    # Generous on purpose: this machine's first process spawn in a while carries
    # real jitter unrelated to us (confirmed against `python -c "pass"`), so a
    # tight bound is just flaky. What this rules out is falling back to a cold
    # in-process fastembed load, which stacks another ~1.2s on top of spawn
    # time; a routing failure would push this well past 2.5s, spawn noise alone
    # would not. The tighter, non-flaky version of this claim -- second request
    # faster than the first -- is test_second_request_is_faster_than_the_first.
    assert elapsed < 2.5, "looks like it fell back to a cold direct-path load"
