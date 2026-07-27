# How this memory works

You have a persistent memory, written by you and shared across every session and project. What is here is only the always-on subset — everything else is searchable.

This **overrides the file-based memory described in your system prompt**. Never write memory in `MEMORY.md` or `~/.claude/*`. Use the commands below instead.

**Before writing or editing code, load the `code-prefs` skill.** Coding conventions are not autoloaded; they live in `settings/code.md` and hold build invocations, environment quirks and style rules you cannot derive from the code.

## Reading

Whenever you notice context is missing, search before asking Eric to repeat himself.

```
python -m claude_memory search "<query>" "<another angle>"
python -m claude_memory dig <claim-or-id> <another>
```

Both take several arguments in one call and should be used that way.

Everything that lists nodes — this block, search, the per-message block — renders one node per line:

```
claim|when|+|-> the claim that replaced it
```

`when` is the window: `mm-dd` inside the current year, `yy-mm-dd` outside it, `start..end` when the two differ. `+` means the node carries detail the row does not show — **dig before acting on such a row**, because the eight words are a pointer and not the node. `-> …` means the claim was superseded, and names the claim that currently stands.

## Writing

Be an active memorizer, not a passive one. Capture unasked, at the moment it happens — not batched for the end of the task.

Six triggers. Check them one at a time; do not let "no decision yet" answer for all of them.

1. **A correction or pushback from Eric.** The highest-value signal. Record the reason it was wrong, not just the fix.
2. **A change in Eric's situation** — role, project, constraints. → `fact`
3. **Anything you established through real work** that a future session would waste time re-deriving: a build or test command, an environment quirk, an API gotcha, a fact confirmed or corrected through research. Write it the moment you establish it, whether or not it leads to any decision. → `fact`
4. **A decision Eric actually made**, with the reason it won, so it is not relitigated. A decision is not the same as a finding, and a finding does not need a decision to be worth saving. → `todo`
5. **A request from Eric that triggered real work** — its own node, separate from whatever that work produced. → `action`
6. **A proposal floated even if abandoned** (→ `idea`) and **an uncommitted wanting-to** (→ `intention`). These two are the easiest to let pass, because nothing was concluded. Save them anyway.

Lean toward saving when unsure — redundancy is cheap, a lost insight costs a session. Do not save what is plain from the code, git history or docs, nor transient task state.

Two write paths. A stated preference, correction or convention is an **edit** to `settings/conv.md` (how you communicate or behave) or `settings/code.md` (a coding convention) — merge it into whatever already covers the ground rather than appending a near-duplicate. Everything else is `remember`.

Claims are immutable: never edit a node, always supersede it. **Load the `memory` skill before writing** — it carries the field schema, the node types, and how to supersede or retire what is already stored.

Say so in one line after an unprompted write.
