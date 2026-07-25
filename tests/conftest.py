"""Shared fixtures.

The pref files are real files on disk, so every test that renders T1 would
otherwise read whatever the developer running the suite happens to have written
in `settings/` -- and, worse, a test that writes one would overwrite it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point the settings directory at a fresh temp dir for every test.

    Autouse rather than opt-in: the failure mode is silent contamination of the
    developer's own memory, which is exactly the kind of thing nobody remembers
    to guard against per-test.
    """
    from claude_memory import db

    monkeypatch.setenv("CLAUDE_MEMORY_SETTINGS", str(tmp_path / "settings"))

    # The legacy path is a module constant pointing at a real location under the
    # developer's home directory. The orphaned-legacy check reads it, so without
    # this the suite consults -- and reports the node count of -- whatever real
    # pre-0.1.0 store happens to be on the machine running the tests.
    # Both, deliberately: setenv reaches the hook subprocesses the CLI tests
    # spawn, setattr reaches this process, where the constant was resolved at
    # import time and no longer consults the environment.
    monkeypatch.setenv("CLAUDE_MEMORY_LEGACY_DB", str(tmp_path / "legacy.db"))

    # No real daemons. Each test gets a fresh store, so daemon discovery never
    # finds the previous one and ensure_running() spawns another -- and the
    # daemon has no idle timeout, so every one of them survives the run.
    # test_daemon.py opts back in explicitly, because spawning is what it tests.
    monkeypatch.setenv("CLAUDE_MEMORY_NO_DAEMON", "1")
    monkeypatch.setattr(db, "LEGACY_DB_PATH", tmp_path / "legacy" / "memory.db")
    return tmp_path / "settings"
