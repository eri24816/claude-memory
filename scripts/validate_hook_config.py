"""Validate that the SessionStart hook is well-formed in user settings."""

import json
from pathlib import Path

settings_path = Path.home() / ".claude" / "settings.json"
settings = json.loads(settings_path.read_text(encoding="utf-8"))

session_start = settings.get("hooks", {}).get("SessionStart", [])
commands = [
    hook
    for group in session_start
    for hook in group.get("hooks", [])
    if hook.get("type") == "command"
]

print("settings.json parses:", True)
print("SessionStart groups:", len(session_start))
for hook in commands:
    print("  command:", hook["command"], hook.get("args"))
    print("  timeout:", hook.get("timeout"))

print("other keys preserved:", sorted(k for k in settings if k != "hooks"))
