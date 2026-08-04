# Setup & Operating Guide

Lead-centric flow: the operator installs the project once, bootstraps an Opus **lead**
agent into a tmux pane, then works entirely through that pane from then on.
The lead spawns Sonnet workers via the `claude-orchestrator` MCP tools; the operator
never calls those tools directly. (The earlier Claude-Desktop-driven flow is
preserved in the [Appendix](#appendix-earlier-revision--claude-desktop-driven-flow) — superseded, not deleted.)

Orchestra was built and is run on WSL2 under Windows 11, but nothing in it is
Windows specific. Any Linux or macOS host with tmux and the Claude Code CLI
will do. Where a step is genuinely WSL2 only, it says so.

## 1. Prerequisites and configuration

### What you need installed

| Requirement | Notes |
|---|---|
| Python 3.12 or newer | `pyproject.toml` sets `requires-python = ">=3.12"` |
| `tmux` | Every agent, including the lead, runs in a tmux window |
| bash 4.0 or newer | The operator scripts use associative arrays (`declare -A`). macOS ships bash 3.2; install a newer one with `brew install bash` and run the scripts and test suite under it |
| Claude Code CLI, logged in | Subscription OAuth. See the API key note below |
| `git` | Every agent works in its own worktree |

**No API key.** Orchestra runs entirely on subscription OAuth and
`ANTHROPIC_API_KEY` must stay **unset**. If it is set, `claude` silently bills
the API instead of your subscription. `scripts/up.py` refuses to start when it
finds that variable set.

### Settings orchestra reads

Every setting below is resolved in exactly one place, `src/orchestra/config.py`,
with `scripts/config.sh` mirroring the shell-visible subset. All of them are
optional: each has a working default.

| Variable | Default | What it controls |
|---|---|---|
| `ORCHESTRA_AGENTS_DIR` | `$HOME/orchestra-agents` | Blackboard root. Holds each agent's `status.json`, `inbox.md`, `task.md`, `agent.log`, and the agents' git worktrees |
| `ORCHESTRA_TMUX_SESSION` | `orchestra` | tmux session holding one window per agent |
| `ORCHESTRA_AGENT_NAME` | `peer` | Name a process reports as when it messages another agent |
| `ORCHESTRA_NOTIFY_TARGET` | none | Agent to nudge when this agent emits a sentinel. Injected automatically by `spawn_agent`; you do not set it by hand |
| `ORCHESTRA_NUDGE_CMD` | `scripts/agent_msg.py` | Command the watchdog invokes as `<cmd> lead <reason>` |
| `ORCHESTRA_EVENTS_URL` | `http://localhost:4000/events` | Observability endpoint. Set it to the **empty string** to disable event emission entirely |
| `SERVER_PORT` | `4000` | Observability events API port |
| `CLIENT_PORT` | `5173` | Observability dashboard UI port |

`SERVER_PORT` and `CLIENT_PORT` carry no `ORCHESTRA_` prefix on purpose:
`scripts/up.py` passes them straight through to the upstream observability
repo's `start-system.sh`, which reads those exact names.

### Shorthand used in this guide

The commands below refer to two paths that orchestra does **not** read from
the environment. They are documentation shorthand so that no step depends on
one person's home directory. Setting them changes nothing about how orchestra
behaves; it only makes the commands here copy-pasteable.

| Shorthand | What it means |
|---|---|
| `$ORCHESTRA_REPO` | Where you cloned this repository. The code derives this itself from its own file location |
| `$TARGET_REPO` | The repository your agents will do work in. Often not this repo |

Export the four together before you start, substituting your own paths:

```bash
# Documentation shorthand for this guide only.
export ORCHESTRA_REPO="$HOME/projects/orchestra"
export TARGET_REPO="$HOME/projects/my-project"

# Read by orchestra at runtime. Both shown at their defaults, so you can
# skip these entirely unless you want something else.
export ORCHESTRA_AGENTS_DIR="$HOME/orchestra-agents"
export ORCHESTRA_TMUX_SESSION=orchestra
```

`.env.example` lists the runtime settings in a form you can copy to `.env`.
Nothing in orchestra loads `.env` automatically: it is a record of your choices
to source from your shell profile.

## 2. Install

```bash
git clone https://github.com/KnobleChipmunk/orchestra "$ORCHESTRA_REPO"
cd "$ORCHESTRA_REPO"
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest   # full suite should pass under bash 4+

# Confirm subscription auth (no API key):
unset ANTHROPIC_API_KEY
claude /status
```

The blackboard directory is created on demand the first time an agent is
spawned; you do not need to make it yourself.

## 3. Bootstrap the lead

**Fast path:** once the lead's worktree exists (one-time setup below), run
`.venv/bin/python scripts/up.py` any time to (re)start everything: the
tmux session, the observability dashboard, the lead, and the
watchdog/unstick loops. It is strictly additive to tmux and never
touches an already-running window, so it is safe against a live
session. Pass `--no-dashboard` to skip the observability UI, or `--dry-run`
to preview what would start without touching anything. The manual steps
below are what `up.py` does under the hood, and remain useful for
troubleshooting or bootstrapping the lead's worktree from scratch.

The lead is an agent like any other, spawned by `spawn_agent` into its own
tmux window and git worktree, with one difference: it is the one agent whose
worktree carries a project-scope `.mcp.json` so its own Claude Code session
can call the `claude-orchestrator` tools to spawn workers.

```bash
# The lead's worktree, branched off the repo you want work done in:
cd "$TARGET_REPO"
git worktree add "$ORCHESTRA_AGENTS_DIR/lead-wt" -b agent-lead
```

Now write `$ORCHESTRA_AGENTS_DIR/lead-wt/.mcp.json`, the project-scope MCP
config. No `wsl.exe` bridge is needed, since the lead already runs in the same
environment as the server:

```bash
cat > "$ORCHESTRA_AGENTS_DIR/lead-wt/.mcp.json" <<EOF
{
  "mcpServers": {
    "claude-orchestrator": {
      "command": "$ORCHESTRA_REPO/.venv/bin/python",
      "args": ["-m", "orchestra.server"]
    }
  }
}
EOF
```

JSON has no variable expansion, so the heredoc above bakes in the absolute
path at write time. If you write the file by hand, substitute the real path to
your venv's Python.

`spawn_agent`'s launch settings auto-approve project-scope `.mcp.json` files
(`enableAllProjectMcpServers`) so this does not hit an approval dialog.

Because `spawn_agent` is a plain importable Python function and not only an MCP
tool, bootstrap the lead itself with a one-off call. There is no MCP client
in the loop yet at this point:

```bash
cd "$ORCHESTRA_REPO"
PYTHONPATH=src .venv/bin/python -c "
from orchestra import config
from orchestra.server import spawn_agent
print(spawn_agent(
    'lead',
    open('docs/LEAD.md').read() + '\n\nGoal: <what the operator wants done>',
    str(config.agents_dir() / 'lead-wt'),
    model='opus',
))
"
```

Then attach and talk to it directly:

```bash
tmux attach -t "$ORCHESTRA_TMUX_SESSION"   # window "lead" is where the lead's pane lives
```

From here on, the operator's side of the workflow is: type goals into the lead's
pane, read its replies. The lead decomposes, spawns workers with
`model="sonnet"`, and reports back in the same pane.

## 4. The `claude-orchestrator` MCP tools

Used by the lead (never directly by the operator):

- `spawn_agent(name, task_prompt, workdir, model="sonnet", notify_target=None)`
  — launch an agent in its own tmux window. `workdir` must be a git worktree
  (agents never share checked-out files). `model`: `"opus"` for the lead,
  `"sonnet"` for workers. `notify_target`: agent name whose inbox/pane gets
  nudged when this agent emits a sentinel (workers pass `notify_target="lead"`).
  Refuses to spawn a duplicate if `name` already exists in an active state
  (`starting`/`working`/`blocked`/`idle`).
- `send_to_agent(name, message)` — append `message` to `name`'s inbox and
  nudge its live tmux pane.
- `get_agent_status(name)` — full `status.json` (state, blocker_reason, needs).
- `list_agents()` — every known agent with state and last update time.
- `read_agent_log(name, tail=40)` — last `tail` lines of an agent's pane log.
- `stop_agent(name, force=False)` — kill an agent's tmux window and mark it
  `stopped`. Refuses if the agent's worktree has uncommitted changes unless
  `force=True`, so a stop never silently discards in-progress work.
- `respawn_agent(name)` — recover a wedged or killed agent by resuming its
  persisted Claude session (`--resume <session_id>`) in a fresh tmux window,
  replaying the same launch flags `spawn_agent` used originally.

## 5. Sentinel protocol

Every agent's task prompt embeds this contract (see `docs/WORKER.md`); the
`Stop`/`PostToolUse` hooks scan the transcript for it and write `status.json`:

- `DONE: <one-line summary>` → `state=done`. Objective fully met and verified.
- `BLOCKED: <one sentence>` → `state=blocked`, `needs=orchestrator_input`.
  Cannot proceed without help.
- `ASK: <one specific question>` → `state` stays `working`,
  `needs=lead_input`. The agent keeps working on other parts while waiting;
  it checks its own inbox for the answer when nudged with "Check your inbox."
- Bare `Stop` with none of the above → `state=idle`, `needs=awaiting_input`.
  `Stop` fires at the end of *every* turn, so this is the only case that
  means "stopped without finishing" — never treat a plain `Stop` as done.

The latest sentinel in the transcript wins, so an agent that was blocked and
later unblocked reads as `done`.

## 6. Inbox hygiene

Agents read their inbox with `orchestra.inbox`, not a raw `cat`:

- `read_new(inbox_path)` — message blocks appended since the last processed
  marker (everything, if no marker exists yet).
- `mark_processed(inbox_path)` — appends a `<!-- processed-through: ... -->`
  marker; call this after acting on `read_new`'s output so those messages
  aren't read again.
- `rotate(inbox_path)` — once processed messages exceed a threshold (200 by
  default), archives everything through the last marker into a sibling
  `inbox.archive.md`, leaving only the header plus any unprocessed tail.

This is what keeps a long-running lead or worker from re-scanning old
messages on every "Check your inbox." nudge.

## 7. Operator scripts

All three resolve their defaults through `scripts/config.sh`, so they follow
`$ORCHESTRA_AGENTS_DIR` without further configuration.

- `scripts/dashboard.sh` — one-screen table of every agent on the blackboard:
  name, state, age of last update, last log line. `watch -n2 scripts/dashboard.sh`
  for a live view.
- `scripts/watchdog.sh` — long-running stall watchdog. Run it in its own
  **tmux window named `watchdog`**, not cron. It needs a persistent process
  that sleeps between cycles (`scripts/watchdog.sh` runs forever; `--once`
  runs a single check) and nudges the lead's live pane, which a one-shot
  cron invocation cannot do. It warns (once per agent/condition) when a
  `working` agent's status has gone stale, or a `done`/`blocked` report has
  sat unread in the lead's inbox past a threshold.
- `scripts/reset_agents.sh` — tear down the tmux session and clear agent state.
  Human and bootstrap use only; agents must never invoke it. Refuses to touch
  a dirty worktree unless given `--force`.

## 8. Smoke test

With the lead spawned and attached (§3), hand it a task that forces two or more
concurrent workers, one of which hits an engineered blocker. Create the
worktrees first:

```bash
cd "$TARGET_REPO"
git worktree add "$ORCHESTRA_AGENTS_DIR/alpha-wt" -b agent-alpha
git worktree add "$ORCHESTRA_AGENTS_DIR/beta-wt"  -b agent-beta
```

Then paste this into the lead's pane, substituting the two absolute worktree
paths you just created:

```
Using workers only: (1) agent alpha, workdir <alpha-wt path>:
add __version__ = "0.1.0" to src/orchestra/__init__.py and a test for it
in tests/. (2) agent beta, workdir <beta-wt path>: update the
EXISTING OpenAPI spec at docs/openapi.yaml to document the six orchestrator
tools. Beta must NOT create the file: if docs/openapi.yaml does not exist,
beta must print 'BLOCKED: docs/openapi.yaml not found, need its location or
permission to create it' and stop.
```

Pass criteria: the lead spawns both workers (`spawn_agent(..., model="sonnet",
notify_target="lead")` for each) → beta's target file doesn't exist in its
worktree, so it emits `BLOCKED:` → `status.json` flips to `blocked` → the
lead gets nudged, reacts via `send_to_agent`.

## 9. Watching agents live

Every agent, lead or worker, is an ordinary interactive Claude Code
session; attaching does not disturb it.

- **One agent, full screen:** `tmux attach -t "$ORCHESTRA_TMUX_SESSION"`, then
  `Ctrl-b n`/`p` to cycle windows, `Ctrl-b d` to detach.
- **Two agents at once:** open two terminal tabs; in each, attach a
  grouped client showing a different window:
  `tmux new-session -t "$ORCHESTRA_TMUX_SESSION" \; select-window -t alpha` (tab 1) and
  `... -t beta` (tab 2). Grouped clients can sit on different windows of the
  same session.
- **Everything scrolling in one pane:** `tail -f "$ORCHESTRA_AGENTS_DIR"/*/agent.log`
  (pipe-pane capture of every agent's output, interleaved).
- **Blackboard dashboard:** `scripts/dashboard.sh` (or the raw
  `watch -n2 'cat "$ORCHESTRA_AGENTS_DIR"/*/status.json'`).

---

## Appendix: Earlier Revision — Claude-Desktop-Driven Flow

**Superseded by the lead-centric flow above.** This is Phase 1's original
design, kept here for reference: Claude Desktop (Windows) itself called the
`claude-orchestrator` tools and decomposed/dispatched work, with the operator talking
to Desktop instead of to a lead agent's pane. It predates the `ASK:` sentinel
and `respawn_agent`, and Desktop's ~4-minute tool-call timeout is what
originally forced every MCP tool to return instantly.

This flow is WSL2 specific: Claude Desktop runs on the Windows side and
reaches the server through `wsl.exe`.

### A.1 Windows side — `%APPDATA%\Claude\claude_desktop_config.json`

Substitute your own checkout path for `<ORCHESTRA_REPO>`; this file is read by
Claude Desktop and expands no shell variables.

```json
{
  "mcpServers": {
    "claude-orchestrator": {
      "command": "wsl.exe",
      "args": [
        "-e", "bash", "-lc",
        "cd <ORCHESTRA_REPO> && .venv/bin/python -m orchestra.server"
      ]
    }
  }
}
```

Fully **Quit** Claude Desktop (system tray → Quit, not just close) and
relaunch. Confirm `claude-orchestrator` shows connected in Settings →
Developer.

### A.2 Orchestrator prompt (paste into Desktop)

```
You are the ORCHESTRATOR. You manage Claude Code worker agents through the
claude-orchestrator tools. Rules:
0. You NEVER do the work yourself. You must not write code, produce
   artifacts, or answer the task directly — your ONLY job is calling the
   claude-orchestrator tools and reporting on the workers. If a task seems
   doable in one step, still delegate it to a single worker.
1. Decompose my task into 2-4 INDEPENDENT, file-disjoint subtasks.
2. For each subtask call spawn_agent(name, task_prompt, workdir). Use
   workdir=<blackboard dir>/<name>-wt. Put the full status protocol
   (below) into every task_prompt.
3. After spawning, call list_agents and get_agent_status each turn to poll.
   Do NOT end your turn after dispatching: keep calling get_agent_status
   repeatedly within the same turn (each call is instant) until every agent
   is done or blocked, or you have polled ~15 times. NEVER assume an agent
   is done without polling. A state of "idle" means the agent stopped
   without finishing — read its log and prompt it.
4. If any agent state is "blocked", read blocker_reason, then either
   send_to_agent(name, "<resolution>") or re-scope and re-dispatch.
5. Report progress to me after each poll.

STATUS PROTOCOL to embed in every task_prompt:
"You are agent <NAME>. Objective: <one sentence>. Output format: <what to
produce>. Allowed tools: Read, Write, Edit, Bash. Stay strictly within your
worktree. If you cannot proceed (missing dependency, ambiguous spec, failing
build you cannot fix), print a line starting exactly with 'BLOCKED:' followed
by one sentence describing what you need, then stop. When the task is fully
complete, print a line starting exactly with 'DONE:' followed by a one-line
summary, then stop."
```

Note this predates `ASK:` — the lead-centric protocol in §5 above adds it.

### A.3 Smoke-test task (send after the prompt above)

Same task as §8, with the worktree paths written out in full.

Beta's target file doesn't exist in its worktree, so it should emit
`BLOCKED:` — that's the engineered blocker the orchestrator must react to.

### A.4 Smoke test (pass criteria)

One task to Desktop → ≥2 concurrent workers in tmux → beta hits its
engineered blocker (reference a file that doesn't exist in its worktree) →
`status.json` flips to `blocked` → Desktop polls, sees it, reacts via
`send_to_agent`.

Observe:
- `tmux attach -t "$ORCHESTRA_TMUX_SESSION"` — one window per agent (Ctrl-b n to cycle)
- `watch -n1 cat "$ORCHESTRA_AGENTS_DIR"/beta/status.json` — state flip
- `tail -f "$ORCHESTRA_AGENTS_DIR"/alpha/agent.log`


## Appendix: operator tools and quirks (Phase E learnings)

- **Reply to an agent reliably**: `.venv/bin/python scripts/say.py lead "<reply>"`
  — types into the pane with verified submission. Typing directly in the pane
  works, but the TUI silently ignores a bare Enter after any pause; if your
  Enter does nothing, type a space then Enter (or just use say.py).
- **Unstick loop**: run alongside the watchdog —
  `tmux new-window -d -t "$ORCHESTRA_TMUX_SESSION" -n unstick 'while true; do "$ORCHESTRA_REPO"/.venv/bin/python "$ORCHESTRA_REPO"/scripts/unstick.py lead 600; sleep 60; done'`
  — force-submits any lead-pane draft unchanged for 10+ minutes.
- **Multiline messages from a shell**: quote with a literal heredoc so dashes
  and apostrophes survive:
  `scripts/agent_msg.py lead "$(cat <<'EOF' ... EOF)"`.
- **Watchdog + unstick both run in their own tmux windows** and should be
  wrapped in a restart loop; a crashed watchdog silently removes the stall
  backstop (it died once in Phase D/E).
