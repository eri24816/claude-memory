"""The Stop-hook maintenance nudge: fires only at MAINTAIN_EVERY, per session."""

from __future__ import annotations

import re

import pytest

from claude_memory import db, hooks, settings


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


def _reminder(connection, session_id="s1"):
    payload = {"session_id": session_id}
    for _ in range(hooks.MAINTAIN_EVERY - 1):
        hooks.build_stop_context(payload, connection)
    return hooks.build_stop_context(payload, connection)


def test_reminder_refuses_to_collapse_into_one_holistic_question(connection):
    """Regression target for the actual bug this hook exists to catch: a
    holistic "anything worth capturing?" question let a real self-check answer
    "nothing new qualifies" while three sourced facts and two user actions sat
    uncaptured, because it read as "has a decision been reached?"

    The triggers themselves moved to meta.md (see the module comment in
    hooks.py). What must stay here is the forcing function -- go one at a time,
    and do not answer from whatever is most salient -- because that is the part
    that has to arrive under task load."""
    reminder = _reminder(connection).lower()

    assert "one at a time" in reminder
    assert "no decision yet" in reminder
    assert "most salient" in reminder


def test_meta_separates_findings_and_actions_from_decisions():
    """The invariant the reminder used to carry, tested where it now lives."""
    meta = settings.read("meta").lower()

    assert "decision" in meta
    assert "finding" in meta or "fact" in meta
    assert "action" in meta
    assert "does not need a decision" in meta


def test_reminder_points_at_a_trigger_list_meta_actually_has(connection):
    """Guards the delegation. The reminder names a count and a section instead
    of restating the triggers, which is only safe while meta.md still has them
    -- and meta.md is edited by hand, with nothing else to catch a silent
    renumber or a dropped trigger. Change the count in one place and this fails.
    """
    meta = settings.read("meta")
    reminder = _reminder(connection)

    body = meta.lower().split("## writing", 1)
    assert len(body) == 2, "meta.md lost the WRITING section the reminder points at"

    triggers = re.findall(r"^(\d+)\. ", body[1], flags=re.MULTILINE)
    assert [int(n) for n in triggers] == list(range(1, len(triggers) + 1)), triggers
    assert len(triggers) == 6, f"meta.md has {len(triggers)} triggers"
    assert "six triggers" in reminder.lower()


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
