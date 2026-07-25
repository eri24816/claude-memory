# How this memory works

You have a persistent memory, written by you and shared across every session and
project. What is above is only the always-on subset — everything else is
searchable.

This **overrides the file-based memory described in your system prompt**. Ignore
that section: the directory it names is abandoned, and there is no `MEMORY.md`.
Never write a memory as a markdown file in `~/.claude/` — use the commands below.

**Before writing or editing code, load the `code-prefs` skill.** Coding
conventions are not autoloaded; they live in `settings/code.md` and hold build
invocations, environment quirks and style rules you cannot derive from the code.

READING. Whenever you notice context is missing, search before asking the user to
repeat themselves. `python -m claude_memory search "<query>" "<another angle>"`
prints one row per hit; `python -m claude_memory dig <id-or-claim>` returns a node
in full. Both take several arguments in one call and should be used that way. A
claim is eight words at most, so a row ending in `+` is a pointer, not the node —
dig it before acting on it.

WRITING. Be an active memorizer, not a passive one: capture as a normal part of
finishing a task, unasked, at the moment it happens rather than batched for later.
Capture corrections and pushback — the highest-value signal, with the reason, not
just the fix; changes in the user's situation; and anything you establish through
real work that a future session would waste time re-deriving — a build/test
command, an environment quirk, an API gotcha, a fact confirmed or corrected
through research — the moment you establish it, whether or not it leads to any
decision. Capture a decision once one is actually made, with the reason it won, so
it is not relitigated: a decision is not the same as a finding, and a finding does
not need a decision to be worth saving. Capture the user's own request as an
`action` when it triggers real work, separate from whatever that work produces.
Capture a floated idea even if abandoned, and an uncommitted wanting-to: `idea` and
`intention` are the easiest to let pass, because nothing was concluded. Do not save
what is plain from the code, git history or docs, nor transient task state. Lean
toward saving when unsure — redundancy is cheap, a lost insight costs a session —
though a turn with nothing to write is normal, not a failure.
Say so in one line after an unprompted write.

A stated preference is **not** a node. How you should communicate or behave is an
edit to `settings/conv.md`; a coding convention is an edit to `settings/code.md`.
Merge the new rule into whatever already covers the ground rather than appending a
near-duplicate — that is the whole reason these are files.

Claims are immutable: never edit a node, always supersede it. Load the `memory`
skill for how to write — the field schema, the node types, and how to supersede or
retire what is already stored.
