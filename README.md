# orchestra

A lead-centric multi-agent orchestrator: the operator talks to an Opus-tier **lead**
agent in a tmux pane; the lead decomposes goals into independent, file-disjoint
subtasks and spawns Sonnet **worker** agents through the `claude-orchestrator`
FastMCP tools, one git worktree per worker, coordinating over a filesystem
blackboard.

- Every MCP tool returns instantly and never waits on an agent — the original
  constraint came from Claude Desktop's ~4-minute tool-call timeout, and it
  still holds now that the lead itself calls these tools: workers report state
  via `Stop`/`PostToolUse` hooks writing `status.json`, the lead polls/reacts.
- Every agent (lead included) is a plain interactive `claude` session running
  in its own tmux window, one git worktree each, subscription OAuth only — no
  API key anywhere.
- Coordination is entirely file-based: `<name>/status.json`, `inbox.md`,
  `task.md`, `agent.log` under the blackboard (`$ORCHESTRA_AGENTS_DIR`,
  default `/home/user/agents`).

## Requirements

- Python 3.10+, `tmux`, `jq`
- **bash 4.0 or newer.** The operator scripts use associative arrays
  (`declare -A`). macOS ships bash 3.2, so on a Mac install a newer bash
  (`brew install bash`) and run the scripts and test suite with it. Under
  bash 3.2 the `dashboard` and `watchdog` tests fail with
  `declare: -A: invalid option`.

## MCP tools (`claude-orchestrator`)

- `spawn_agent(name, task_prompt, workdir, model="sonnet", notify_target=None)`
  — launch a worker (or the lead) in its own tmux window. `workdir` must be a
  git worktree. `model` is `"opus"` for the lead, `"sonnet"` for workers.
  `notify_target` is the agent name whose inbox/pane gets nudged when this
  agent emits a sentinel (workers set it to `"lead"`).
- `send_to_agent(name, message)` — append to an agent's inbox and nudge its pane.
- `get_agent_status(name)` — read one agent's `status.json`.
- `list_agents()` — every known agent with state and last-updated time.
- `read_agent_log(name, tail=40)` — tail of an agent's pane log.
- `stop_agent(name, force=False)` — kill an agent's tmux window; refuses if
  its worktree has uncommitted changes unless `force=True`.
- `respawn_agent(name)` — recover a wedged/killed agent by resuming its
  persisted Claude session in a fresh tmux window.

## Sentinel protocol

Workers (and the lead) report state by printing one of these, each on its own
line:

- `DONE: <summary>` → `state=done`.
- `BLOCKED: <reason>` → `state=blocked`, `needs=orchestrator_input`.
- `ASK: <question>` → agent keeps working; `needs=lead_input`, answered via
  the asker's inbox.
- A bare `Stop` with no sentinel → `state=idle` (`needs=awaiting_input`) —
  `Stop` fires at the end of *every* turn, so it never by itself means done.

## Operator scripts

- `scripts/dashboard.sh` — one-screen blackboard overview: name, state, age
  of last update, last log line, for every agent.
- `scripts/watchdog.sh` — long-running stall watchdog; run it in its own
  **tmux window named `watchdog`** (not cron — it needs a persistent process
  that sleeps between cycles and nudges the lead's live pane). Flags stalled
  workers and unread `DONE:`/`BLOCKED:` reports the lead's own turn-end
  inbox check might miss between turns.

## Inbox hygiene

Agents consume their inbox through `orchestra.inbox` (`read_new`,
`mark_processed`, `rotate`) instead of a raw `cat`: `read_new` returns only
messages appended since the last processed marker, and `rotate` archives old
processed messages into `inbox.archive.md` — so a long-running agent never
re-reads years of old messages.

See `docs/setup.md` for install, bootstrapping the lead, the sentinel/inbox
mechanics in more detail, and the smoke test (plus an appendix on the earlier
Claude-Desktop-driven flow this superseded). Terminology: `docs/glossary.md`.
Design rationale: `docs-research-phase1.md`; implementation notes: `CLAUDE.md`.
