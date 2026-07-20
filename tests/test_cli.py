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


def test_dig_reports_a_miss_without_crashing(store):
    result = run(store, "dig", "No such memory")
    assert result.returncode == 4
    assert "No such memory" in result.stdout
