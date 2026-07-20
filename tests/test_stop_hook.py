"""The Stop-hook maintenance nudge: fires only at MAINTAIN_EVERY, per session."""

from __future__ import annotations

import pytest

from claude_memory import db, hooks


@pytest.fixture
def connection():
    conn = db.connect(":memory:")
    yield conn
    conn.close()


def test_no_session_id_stays_silent(connection):
    """A payload the hook cannot key state on must not raise or fire."""
    assert hooks.build_stop_context({}, connection) == ""


@pytest.mark.parametrize("turn", range(1, hooks.MAINTAIN_EVERY))
def test_stays_silent_below_the_threshold(connection, turn):
    payload = {"session_id": "s1"}
    for _ in range(turn):
        result = hooks.build_stop_context(payload, connection)
    assert result == "", f"fired early at turn {turn}"


def test_fires_at_maintain_every(connection):
    payload = {"session_id": "s1"}
    for _ in range(hooks.MAINTAIN_EVERY - 1):
        assert hooks.build_stop_context(payload, connection) == ""
    result = hooks.build_stop_context(payload, connection)
    assert result != ""
    assert str(hooks.MAINTAIN_EVERY) in result


def test_fires_again_at_the_next_multiple(connection):
    """Regression risk: additionalContext on Stop forces the turn to continue
    even without decision:block, so firing on every stop -- not just multiples
    of MAINTAIN_EVERY -- would mean the conversation could never end."""
    payload = {"session_id": "s1"}
    fired_at = []
    for turn in range(1, hooks.MAINTAIN_EVERY * 2 + 1):
        if hooks.build_stop_context(payload, connection):
            fired_at.append(turn)
    assert fired_at == [hooks.MAINTAIN_EVERY, hooks.MAINTAIN_EVERY * 2]


def test_counters_are_independent_per_session(connection):
    """A fresh session must not inherit another session's count."""
    payload_a = {"session_id": "session-a"}
    payload_b = {"session_id": "session-b"}
    for _ in range(hooks.MAINTAIN_EVERY - 1):
        hooks.build_stop_context(payload_a, connection)
    assert hooks.build_stop_context(payload_b, connection) == ""


def test_reminder_checks_findings_and_actions_separately_from_decisions(connection):
    """Regression target for the actual bug this hook exists to catch: a
    holistic "anything worth capturing?" question let a real self-check answer
    "nothing new qualifies" while three sourced facts and two user actions sat
    uncaptured, because it read as "has a decision been reached?" A checklist
    that separates findings/actions from decisions is the fix under test."""
    payload = {"session_id": "s1"}
    for _ in range(hooks.MAINTAIN_EVERY - 1):
        hooks.build_stop_context(payload, connection)
    reminder = hooks.build_stop_context(payload, connection)

    assert "decision" in reminder.lower()
    assert "finding" in reminder.lower() or "fact" in reminder.lower()
    assert "action" in reminder.lower()
    assert "does not need a decision" in reminder or "no decision" in reminder.lower()


def test_stop_hook_end_to_end_as_a_subprocess(tmp_path):
    """Real subprocess, matching how Claude Code actually invokes it -- the
    other two hooks already broke in ways only a subprocess run exposed."""
    import json
    import os
    import subprocess
    import sys

    db_path = tmp_path / "memory.db"
    env = {**os.environ, "CLAUDE_MEMORY_DB": str(db_path)}

    def run_stop(session_id):
        return subprocess.run(
            [sys.executable, "-m", "claude_memory.hooks", "stop"],
            input=json.dumps({"session_id": session_id}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env,
        )

    for _ in range(hooks.MAINTAIN_EVERY - 1):
        result = run_stop("subprocess-session")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == ""

    result = run_stop("subprocess-session")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert str(hooks.MAINTAIN_EVERY) in payload["hookSpecificOutput"]["additionalContext"]
