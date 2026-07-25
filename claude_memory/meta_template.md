# How this memory works

Nodes are shared across every session and project. What follows is the always-on
subset; the rest is searchable. This file and `conv.md` are the only things
loaded whole — everything else is one line until you dig.

**Reading.** When context is missing, search before asking:
`python -m claude_memory search "<query>" "<other angle>"` — several queries in
one call cost far less than several calls. Rows read `type, date, claim`; a
trailing `+` means the node carries detail you have not seen. Expand with
`python -m claude_memory dig <id-or-claim> <another>`, also batched. A claim is
at most 8 words, so a row is never truncated — but a `+` row is a pointer, not
the whole node.

**Before writing code**, load the `code-prefs` skill. Conventions are not in
this file.

**Writing.** Capture as part of finishing a task, unasked, at the moment it
happens. Two paths, and picking the wrong one is how memory drifts:

- A stated preference, a correction, or a convention → **edit a file**:
  `settings/conv.md` for behaviour, `settings/code.md` for code. Merge it into
  what is there. Appending a near-duplicate is the failure these files exist to
  end.
- Anything about the world or the user → `remember`. Load the `memory` skill for
  the schema.

Capture corrections and the reason behind them; decisions, with why they won;
findings established through real work, whether or not a decision followed; and
Eric's own requests when they trigger real work. Not what the code, git history
or these files already say. When unsure, save — redundancy is cheap, a lost
insight costs a session.

Claims are immutable: never edit a node, supersede it. Say so in one line after
an unprompted write.
