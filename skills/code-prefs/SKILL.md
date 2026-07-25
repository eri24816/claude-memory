---
name: code-prefs
description: The user's coding constraints and conventions. Load before writing or editing code in any language — it holds build/test invocations, environment quirks, and style rules that are not derivable from the code itself.
---

# Coding conventions

The conventions live in `settings/code.md` inside the claude-memory repo — a
file, not this skill, so that adding a convention is an edit rather than an
append, and near-duplicates can be merged instead of accumulating.

**Read it now**, then apply what is relevant:

```
<claude-memory>/settings/code.md
```

If `CLAUDE_MEMORY_SETTINGS` is set, the file is at `$CLAUDE_MEMORY_SETTINGS/code.md`
instead. `python -m claude_memory where --code` prints the resolved path.

## Adding a convention

Edit the file. Do not write a node — `remember` rejects `code-pref` and points
back here.

Consolidate rather than append: if a rule already covers the ground, rewrite it
to include the new case. The whole reason these left the node store is that
nodes could only be appended, so restatements piled up and nothing ever merged
them.
