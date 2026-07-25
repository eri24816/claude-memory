"""Shared fixtures.

The pref files are real files on disk, so every test that renders T1 would
otherwise read whatever the developer running the suite happens to have written
in `settings/` -- and, worse, a test that writes one would overwrite it.
"""

from __future__ import annotations

import shutil

import pytest


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point the settings directory at a fresh temp dir for every test.

    Autouse rather than opt-in: the failure mode is silent contamination of the
    developer's own memory, which is exactly the kind of thing nobody remembers
    to guard against per-test.
    """
    from claude_memory import settings

    monkeypatch.setenv("CLAUDE_MEMORY_SETTINGS", str(tmp_path / "settings"))

    # meta.md is tracked in the repo rather than living in settings/, so
    # CLAUDE_MEMORY_SETTINGS does not cover it -- and test_render writes a
    # two-line stub through settings.write("meta"). That went straight into the
    # working tree and truncated the real file, which is the shipped default
    # every clone gets. Redirect the repo dir as well, seeded with a copy so
    # tests that only read still see the real thing.
    repo = tmp_path / "repo"
    repo.mkdir()
    real_meta = settings.REPO_DIR / "meta.md"
    if real_meta.exists():
        shutil.copy(real_meta, repo / "meta.md")
    monkeypatch.setattr(settings, "REPO_DIR", repo)

    # No real daemons. Each test gets a fresh store, so daemon discovery never
    # finds the previous one and ensure_running() spawns another -- and the
    # daemon has no idle timeout, so every one of them survives the run.
    # test_daemon.py opts back in explicitly, because spawning is what it tests.
    monkeypatch.setenv("CLAUDE_MEMORY_NO_DAEMON", "1")
    return tmp_path / "settings"
