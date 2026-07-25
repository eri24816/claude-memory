"""Installing onto a machine that is not the author's.

Every case here was once a step in a doc, and each one failed the same way: done
by hand once, then silently out of date. The failures are all silent, which is
why they are tested rather than documented.
"""

from __future__ import annotations

import json

import pytest

from claude_memory import cli, install, settings


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_MEMORY_SETTINGS", str(tmp_path / "settings"))
    monkeypatch.setenv("CLAUDE_MEMORY_SKILLS", str(tmp_path / "skills"))
    return tmp_path


def test_meta_ships_with_the_repo(fresh):
    """meta.md describes the system, not the user, so it is tracked and arrives
    with the clone -- no seeding step to skip and no `git pull` that leaves it
    behind. An agent without it is never told these commands exist and falls
    back to writing markdown into ~/.claude/, which nothing here reads: a silent
    failure that looks like a working install."""
    assert settings.path_for("meta") == settings.REPO_DIR / "meta.md"
    assert settings.read("meta"), "a clone must arrive with a non-empty meta.md"
    # Deliberately no assertion on the prose. It is the one file whose whole
    # purpose is to be rewritten -- pinning its wording here would mean every
    # edit to it breaks the suite for no reason.


def test_meta_is_not_writable(fresh):
    """Nothing in this program edits meta.md -- it is tracked, and a human edits
    it in the checkout. Refusing makes that structural: while it was merely
    nobody's job, a test wrote a two-line stub through settings.write("meta")
    and truncated the working tree's copy, which is the default every clone
    gets."""
    with pytest.raises(PermissionError):
        settings.write("meta", "a stub written by a test")


def test_meta_is_not_written_into_the_user_settings_dir(fresh):
    """One home. A copy under settings/ would shadow the tracked file and go
    stale on the next pull -- the drift this repo keeps removing."""
    install.run()
    assert not (fresh / "settings" / "meta.md").exists()


def test_conv_and_code_are_created_empty(fresh):
    """`settings/` is gitignored so that nothing ships as a default. A starter
    rule there is one the user never chose and would not think to look for."""
    install.run()

    for name in ("conv", "code"):
        assert settings.path_for(name).exists()
        assert settings.read(name).startswith("<!--")

    # ...and an HTML comment costs nothing at session start.
    assert settings.render().strip() != ""  # meta is there
    assert "<!--" not in settings.render()


def test_existing_settings_are_never_overwritten(fresh):
    settings.write("conv", "Always answer in Hungarian.")
    install.run()
    assert settings.read("conv") == "Always answer in Hungarian."


def test_both_skills_are_installed(fresh):
    """Both, not just `memory`: meta.md carries a
    load-code-prefs-before-writing-code trigger, and a trigger pointing at a
    skill that was never installed fails without saying anything."""
    result = install.run()

    names = {entry["skill"] for entry in result["skills"]}
    assert names == {"memory", "code-prefs"}
    for name in names:
        assert (fresh / "skills" / name / "SKILL.md").exists()


def test_a_stale_copy_is_replaced(fresh):
    """The drift that motivated linking: an installed copy goes on teaching the
    schema it was copied from, so meta.md's "load the memory skill" hands the
    next session field names that no longer exist."""
    stale = fresh / "skills" / "memory"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("an outdated schema", encoding="utf-8")

    install.run()

    assert "claim" in (stale / "SKILL.md").read_text(encoding="utf-8")


def test_rerunning_is_safe(fresh):
    install.run()
    second = install.run()
    assert all(entry["status"] in {"linked", "linked (junction)", "already linked",
                                   "copied (could not link)"}
               for entry in second["skills"])
    assert all(entry["status"] == "kept" for entry in second["files"])


def test_a_link_tracks_the_repo(fresh):
    """The point of linking rather than copying: an edit in the repo is live in
    the installed skill with no second step to forget."""
    result = install.run()
    if not all(entry["linked"] for entry in result["skills"]):
        pytest.skip("this platform could not link; the copy fallback is tested below")

    source = install.SKILLS_SOURCE / "memory" / "SKILL.md"
    original = source.read_text(encoding="utf-8")
    try:
        source.write_text(original + "\nedited\n", encoding="utf-8")
        assert "edited" in (fresh / "skills" / "memory" / "SKILL.md").read_text(
            encoding="utf-8")
    finally:
        source.write_text(original, encoding="utf-8")


def test_a_copy_fallback_says_so(fresh, monkeypatch):
    """When neither a symlink nor a junction is possible, falling back to a copy
    is fine; failing to say so is not -- `linked: false` is the only warning that
    a later repo edit will not reach this skill."""
    from pathlib import Path

    monkeypatch.setattr(Path, "symlink_to",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    monkeypatch.setattr(install, "_junction", lambda source, destination: False)
    result = install.run()

    assert all(entry["linked"] is False for entry in result["skills"])
    assert all("install" in entry.get("note", "") for entry in result["skills"])
    assert (fresh / "skills" / "memory" / "SKILL.md").exists()


def test_init_installs_as_well(fresh, capsys, monkeypatch):
    """`init` is the command the doc names first, so it does the whole job. A
    store with no skills linked is not a working install, and a second step is a
    second step to forget."""
    monkeypatch.setenv("CLAUDE_MEMORY_NO_DAEMON", "1")
    assert cli.main(["--db", str(fresh / "memory.db"), "init"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["db"].endswith("memory.db")
    assert (fresh / "skills" / "memory" / "SKILL.md").exists()
    assert settings.path_for("conv").exists()
