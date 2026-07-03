# Glossary

**Blackboard** — The filesystem directory tree at `$ORCHESTRA_AGENTS_DIR`
(default `/home/tusk/agents`) that agents use as shared state instead of
direct calls: each agent gets `<name>/status.json`, `inbox.md`, `task.md`,
and `agent.log`. It exists because every MCP tool must return instantly (the
~4-minute Desktop tool-call timeout), so the lead and workers coordinate by
reading and writing these files rather than blocking on one another.

**Sentinel** — One of three exact status lines a worker prints in its own
output — `DONE: <summary>`, `BLOCKED: <reason>`, or `ASK: <question>` — which
`write_status.py` scans for in the transcript tail on every `Stop` or
`PostToolUse` hook event and translates into `status.json`'s `state` field
(`done`, `blocked`, or `working`/`ASK` with `needs: lead_input`). A bare
`Stop` with no sentinel means `idle` (awaiting input), never `done`, since
`Stop` fires at the end of every turn.

**Nudge** — The notification a worker's hook sends to its `notify_target`
(usually `lead`) the moment a new sentinel appears: `write_status.py`
appends a message to the target's `inbox.md` and types a short line into its
live tmux pane via `send-keys` (see `messaging.deliver` / `send_to_agent` in
`server.py`). It is deduped by sentinel count so the same sentinel never
nudges twice.

**Worktree** — The isolated git working directory (`git worktree`) that
`spawn_agent`'s `workdir` argument must point to for each agent, so that
concurrent agents never edit the same checked-out files. `alpha-wt` is
agent alpha's worktree, checked out on branch `agent-alpha`.

**Lead** — The Opus-tier orchestrating agent (as described in
`docs/LEAD.md`) that decomposes a goal into independent worker subtasks,
calls `spawn_agent` for each, reacts to nudges by reading its inbox and the
relevant `status.json` files, and reviews/merges worker output via PR. The
lead orchestrates and reviews; it never implements feature code itself.
