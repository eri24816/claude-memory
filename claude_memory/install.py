"""One idempotent command that puts the moving parts where they belong.

What a working install needs beyond the store:

  * The skills discoverable from `~/.claude/skills`. They are *linked*, not
    copied, so a repo edit is live everywhere by construction; a copy stops
    tracking its source and goes on teaching whatever it was copied from.
  * `settings/conv.md` and `settings/code.md` to exist, empty, so the agent has
    somewhere obvious to put a rule it is told.

`meta.md` is not here: it describes the system rather than the user, so it is
tracked in the repo and arrives with the clone.

Re-running is always safe, and is the documented fix after any change to
`skills/` on a platform where linking was not possible.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from . import settings

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
SKILLS_SOURCE = REPO_DIR / "skills"
SKILL_NAMES = ("memory", "code-prefs")

# Blank files rather than starter rules. `settings/` is gitignored whole
# precisely so nothing ships as a default -- these hold what the user tells the
# agent about itself, and a shipped opinion there is one they never chose and
# will not notice. The header is an HTML comment, which settings.render() strips
# before T1, so the explanation costs nothing per session.
STUBS = {
    "conv": """<!--
How the agent should communicate and behave. Loaded whole, every session.

Edit this file when a preference is stated or a correction is made. MERGE into
whatever section already covers the ground -- appending a near-duplicate is the
failure this file exists to end. Keep it tight: every character is paid for on
every session start.
-->
""",
    "code": """<!--
Coding constraints and conventions. NOT autoloaded -- reached through the
`code-prefs` skill, so it can afford to be longer than conv.md.

Build and test invocations, environment quirks, style rules -- anything a
session would otherwise re-derive or get wrong. Merge, do not append.
-->
""",
}


def skills_dir() -> Path:
    return Path(os.environ.get("CLAUDE_MEMORY_SKILLS") or Path.home() / ".claude" / "skills")


def _junction(source: Path, destination: Path) -> bool:
    """Windows directory junction: a real link that needs no special privilege.

    The symlink path fails on a default Windows install -- it wants Developer
    Mode or elevation -- and that was measured here, not assumed. A junction is
    the same thing for directories and has needed no privilege since Vista, so
    on the one platform where symlinks are awkward the link still tracks the
    repo instead of silently becoming a copy that drifts.
    """
    if os.name != "nt":
        return False
    import subprocess

    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(destination), str(source)],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and destination.is_dir()


def _is_link(path: Path) -> bool:
    """Symlink or junction. `is_symlink` is False for a junction on Python 3.11,
    but both report the reparse-point attribute."""
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & 0x400)  # REPARSE_POINT
    except (OSError, AttributeError):
        return False


def _link_skill(name: str, destination_root: Path) -> dict[str, Any]:
    """Link the repo's skill into the skills directory, copying only if it cannot.

    The copy fallback is honest about what it is: `linked: false` is what tells
    the caller a repo edit will not reach this one.
    """
    source = SKILLS_SOURCE / name
    if not source.is_dir():
        return {"skill": name, "status": "missing in repo", "linked": False}

    destination = destination_root / name
    destination_root.mkdir(parents=True, exist_ok=True)

    if _is_link(destination):
        try:
            if destination.resolve() == source.resolve():
                return {"skill": name, "status": "already linked", "linked": True}
        except OSError:
            pass
        # A link somewhere else, or a broken one. Junctions are not files, so
        # unlink is wrong for them; rmdir removes the link without following it
        # into the target -- deleting the repo's skills would be catastrophic.
        try:
            destination.rmdir()
        except OSError:
            destination.unlink()
    elif destination.exists():
        # A stale copy from a previous install. Replacing it is the entire point
        # of running this: the copy is what drifted.
        shutil.rmtree(destination)

    try:
        destination.symlink_to(source, target_is_directory=True)
        return {"skill": name, "status": "linked", "linked": True}
    except (OSError, NotImplementedError):
        pass

    if _junction(source, destination):
        return {"skill": name, "status": "linked (junction)", "linked": True}

    shutil.copytree(source, destination)
    return {
        "skill": name,
        "status": "copied (could not link)",
        "linked": False,
        "note": "re-run `python -m claude_memory install` after editing skills/",
    }


def _seed_settings() -> list[dict[str, Any]]:
    """Write the settings files that are missing. Never overwrites."""
    results = []
    for name, text in STUBS.items():
        path = settings.path_for(name)
        if path.exists() and path.read_text(encoding="utf-8-sig").strip():
            results.append({"file": str(path), "status": "kept"})
            continue
        settings.write(name, text)
        results.append({"file": str(path), "status": "created"})
    return results


def run(skills_destination: Path | None = None) -> dict[str, Any]:
    """Seed settings, install the skills, and report what changed."""
    destination = Path(skills_destination) if skills_destination else skills_dir()
    return {
        "settings_dir": str(settings.settings_dir()),
        "files": _seed_settings(),
        "skills_dir": str(destination),
        "skills": [_link_skill(name, destination) for name in SKILL_NAMES],
    }
