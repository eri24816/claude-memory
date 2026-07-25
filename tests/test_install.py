"""Installing onto a machine that is not the author's.

Every case here is a defect that shipped in 0.1.0 and was found by a real
migration, not a hypothetical. The shared shape: a step the docs asked a human
to do by hand, which was then done once and never again.
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


def test_meta_is_seeded_because_a_store_without_it_is_not_an_install(fresh):
    """The defect this exists for: settings/ is gitignored whole, so meta.md
    shipped with nobody. An agent with no meta file is never told these commands
    exist and falls back to writing markdown into ~/.claude/, which nothing here
    reads -- a silent, total failure that looks like a working install."""
    install.run()

    text = settings.read("meta")
    assert "claude_memory search" in text
    assert "code-prefs" in text, "the trigger for conventions must survive seeding"


def test_the_template_does_not_name_the_author(fresh):
    install.run()
    assert "Eric" not in settings.read("meta")


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
    """code-prefs had never been installed at all, so meta.md's
    load-code-prefs-before-writing-code trigger pointed at nothing."""
    result = install.run()

    names = {entry["skill"] for entry in result["skills"]}
    assert names == {"memory", "code-prefs"}
    for name in names:
        assert (fresh / "skills" / name / "SKILL.md").exists()


def test_a_stale_copy_is_replaced(fresh):
    """The drift that was actually observed: after 0.1.0 the installed `memory`
    skill still taught the pre-0.1.0 title+summary schema while the repo taught
    claim+detail, so meta.md's "load the memory skill" handed the next session
    the wrong field schema."""
    stale = fresh / "skills" / "memory"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("title + summary, the old schema", encoding="utf-8")

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


def test_init_seeds_as_well(fresh, capsys, monkeypatch):
    """`init` is the command the doc names first. A store with no meta.md is a
    broken install, so the two cannot be separate steps."""
    monkeypatch.setenv("CLAUDE_MEMORY_NO_DAEMON", "1")
    assert cli.main(["--db", str(fresh / "memory.db"), "init"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["db"].endswith("memory.db")
    assert settings.path_for("meta").exists()
