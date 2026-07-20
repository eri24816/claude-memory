"""The CLI as an agent actually invokes it: a subprocess, with a real console.

These go through the shell rather than calling main() in-process, because the
bugs this file exists to catch live exactly in that gap -- stream encoding and
file decoding, neither of which a Python-level test touches.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

CJK_NODE = [{
    "title": "Mixed script note",
    "summary": "幫 user 切 input 選 seed — an em dash and CJK in one summary.",
    "type": "fact",
    "about_user": False,
    "window_start": "2026-07-20",
}]


def run(store, *arguments, encoding: str | None = None):
    environment = {"PATH": "", "SYSTEMROOT": "", "PYTHONIOENCODING": encoding or ""}
    import os

    merged = {**os.environ, **{k: v for k, v in environment.items() if v}}
    if encoding:
        merged["PYTHONIOENCODING"] = encoding
    else:
        merged.pop("PYTHONIOENCODING", None)
    return subprocess.run(
        [sys.executable, "-m", "claude_memory", "--db", str(store), *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=merged,
    )


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "memory.db"
    payload = tmp_path / "nodes.json"
    # utf-8-sig: this is how PowerShell's `Out-File -Encoding utf8` writes, and
    # the skill tells the agent to hand `remember` a file on Windows.
    payload.write_text(json.dumps(CJK_NODE, ensure_ascii=False), encoding="utf-8-sig")
    result = run(path, "remember", "--file", str(payload))
    assert result.returncode == 0, result.stderr
    return path


def test_remember_reads_a_file_with_a_bom(store):
    """Regression: --file used plain utf-8, so the BOM PowerShell writes made
    every documented write on Windows fail to parse."""
    result = run(store, "dig", "Mixed script note")
    assert result.returncode == 0, result.stderr
    assert "幫" in result.stdout


@pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
def test_search_survives_a_non_utf8_console(store, encoding):
    """Regression: stdout inherited the Windows cp1252 codepage, so any hit
    containing CJK died with UnicodeEncodeError before printing anything.

    Masked for a long time because every manual check exported
    PYTHONIOENCODING=utf-8 first, which is exactly what an agent does not do.
    """
    result = run(store, "search", "seed input", encoding=encoding)
    assert result.returncode == 0, result.stderr
    assert "Mixed script note" in result.stdout


def run_hook(store, command, payload, encoding: str | None = None):
    import os

    merged = {**os.environ, "CLAUDE_MEMORY_DB": str(store)}
    if encoding:
        merged["PYTHONIOENCODING"] = encoding
    else:
        merged.pop("PYTHONIOENCODING", None)
    return subprocess.run(
        [sys.executable, "-m", "claude_memory.hooks", command],
        input=json.dumps(payload), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=merged,
    )


def context_of(result):
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_user_prompt_submit_injects_the_rendered_block(store):
    """The per-message path, which was designed early and only wired much later:
    every check until then exercised search directly, never the hook."""
    context = context_of(run_hook(store, "user-prompt-submit", {
        "prompt": "what does the mixed script note say about seed input",
        "cwd": str(store.parent),
    }))
    assert context is not None, "expected an injection for a substantive prompt"
    assert context.startswith("# memory that could be useful:")
    assert "Mixed script note" in context


@pytest.mark.parametrize("prompt", ["ok", "yes", "do it", ""])
def test_user_prompt_submit_stays_silent_on_trivial_messages(store, prompt):
    """Injecting three unrelated nodes into a message that needed none is worse
    than injecting nothing, and this fires on every single message."""
    result = run_hook(store, "user-prompt-submit", {"prompt": prompt,
                                                    "cwd": str(store.parent)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_hooks_survive_a_malformed_payload(store):
    """A hook must never break a session, so it degrades to silence."""
    result = subprocess.run(
        [sys.executable, "-m", "claude_memory.hooks", "user-prompt-submit"],
        input="not json at all", capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_dig_reports_a_miss_without_crashing(store):
    result = run(store, "dig", "No such memory")
    assert result.returncode == 4
    assert "No such memory" in result.stdout
