# Setup & Operating Guide (Windows 11 + WSL2)

Lead-centric flow: the operator installs the project once, bootstraps an Opus **lead**
agent into a tmux pane, then works entirely through that pane from then on.
The lead spawns Sonnet workers via the `claude-orchestrator` MCP tools; the operator
never calls those tools directly. (The earlier Claude-Desktop-driven flow is
preserved in the [Appendix](#appendix-earlier-revision--claude-desktop-driven-flow) — superseded, not deleted.)

## 1. Install (WSL2)

```bash
cd /home/user/projects/orchestra
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest   # full suite should pass

# Confirm subscription auth (no API key):
unset ANTHROPIC_API_KEY
claude /status
```

## 2. Bootstrap the lead

**Fast path:** once the lead's worktree exists (one-time setup below), run
`.venv/bin/python scripts/up.py` any time to (re)start everything — the
tmux session, the observability dashboard, the lead, and the
watchdog/unstick loops — safely: it's strictly additive to tmux and never
touches an already-running window, so it's fine to run against a live
session. Pass `--no-dashboard` to skip the observability UI, or `--dry-run`
to preview what would start without touching anything. The manual steps
below are what `up.py` does under the hood, and remain useful for
troubleshooting or bootstrapping the lead's worktree from scratch.

The lead is an agent like any other — spawned by `spawn_agent` into its own
tmux window and git worktree — with one difference: it's the one agent whose
worktree carries a project-scope `.mcp.json` so its own Claude Code session
can call the `claude-orchestrator` tools to spawn workers.

```bash
# Lead's own worktree (repo you're orchestrating work in, e.g. a project):
cd /home/user/myrepo
git worktree add /home/user/agents/lead-wt -b agent-lead
```

`/home/user/agents/lead-wt/.mcp.json` — project-scope MCP config, no `wsl.exe`
bridge needed since the lead already runs inside WSL2:

```json
{
  "mcpServers": {
    "claude-orchestrator": {
      "command": "/home/user/projects/orchestra/.venv/bin/python",
      "args": ["-m", "orchestra.server"]
    }
  }
}
```

`spawn_agent`'s launch settings auto-approve project-scope `.mcp.json` files
(`enableAllProjectMcpServers`) so this doesn't hit an approval dialog.

Because `spawn_agent` is a plain importable Python function (not only an MCP
tool), bootstrap the lead itself with a one-off call — there's no MCP client
in the loop yet at this point:

```bash
cd /home/user/projects/orchestra
PYTHONPATH=src .venv/bin/python -c "
from orchestra.server import spawn_agent
print(spawn_agent(
    'lead',
    open('docs/LEAD.md').read() + '\n\nGoal: <what the operator wants done>',
    '/home/user/agents/lead-wt',
    model='opus',
))
"
```

Then attach and talk to it directly:

```bash
tmux attach -t orchestra   # window "lead" is where the lead's pane lives
```

From here on, the operator's side of the workflow is: type goals into the lead's
pane, read its replies. The lead decomposes, spawns workers with
`model="sonnet"`, and reports back in the same pane.

## 3. The `claude-orchestrator` MCP tools

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
- `list_agents()` — every known agent with state and last-updated time.
- `read_agent_log(name, tail=40)` — last `tail` lines of an agent's pane log.
- `stop_agent(name, force=False)` — kill an agent's tmux window and mark it
  `stopped`. Refuses if the agent's worktree has uncommitted changes unless
  `force=True`, so a stop never silently discards in-progress work.
- `respawn_agent(name)` — recover a wedged or killed agent by resuming its
  persisted Claude session (`--resume <session_id>`) in a fresh tmux window,
  replaying the same launch flags `spawn_agent` used originally.

## 4. Sentinel protocol

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

## 5. Inbox hygiene

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

## 6. Operator scripts

- `scripts/dashboard.sh` — one-screen table of every agent on the blackboard:
  name, state, age of last update, last log line. `watch -n2 scripts/dashboard.sh`
  for a live view.
- `scripts/watchdog.sh` — long-running stall watchdog. Run it in its own
  **tmux window named `watchdog`**, not cron — it needs a persistent process
  that sleeps between cycles (`scripts/watchdog.sh` runs forever; `--once`
  runs a single check) and nudges the lead's live pane, which a one-shot
  cron invocation can't do. It warns (once per agent/condition) when a
  `working` agent's status has gone stale, or a `done`/`blocked` report has
  sat unread in the lead's inbox past a threshold.

## 7. Smoke test

With the lead spawned and attached (§2), hand it a task that forces ≥2
concurrent workers, one of which hits an engineered blocker:

```
Using workers only: (1) agent alpha, workdir /home/user/agents/alpha-wt:
add __version__ = "0.1.0" to src/orchestra/__init__.py and a test for it
in tests/. (2) agent beta, workdir /home/user/agents/beta-wt: update the
EXISTING OpenAPI spec at docs/openapi.yaml to document the six orchestrator
tools. Beta must NOT create the file: if docs/openapi.yaml does not exist,
beta must print 'BLOCKED: docs/openapi.yaml not found, need its location or
permission to create it' and stop.
```

Pass criteria: the lead spawns both workers (`spawn_agent(..., model="sonnet",
notify_target="lead")` for each) → beta's target file doesn't exist in its
worktree, so it emits `BLOCKED:` → `status.json` flips to `blocked` → the
lead gets nudged, reacts via `send_to_agent`. Create the worktrees first:

```bash
cd /home/user/myrepo
git worktree add /home/user/agents/alpha-wt -b agent-alpha
git worktree add /home/user/agents/beta-wt  -b agent-beta
```

## 8. Watching agents live

Every agent — lead or worker — is an ordinary interactive Claude Code
session; attaching doesn't disturb it.

- **One agent, full screen:** `tmux attach -t orchestra`, then `Ctrl-b n`/`p`
  to cycle windows, `Ctrl-b d` to detach.
- **Two agents at once:** open two WSL terminal tabs; in each, attach a
  grouped client showing a different window:
  `tmux new-session -t orchestra \; select-window -t alpha` (tab 1) and
  `... -t beta` (tab 2). Grouped clients can sit on different windows of the
  same session.
- **Everything scrolling in one pane:** `tail -f /home/user/agents/*/agent.log`
  (pipe-pane capture of every agent's output, interleaved).
- **Blackboard dashboard:** `scripts/dashboard.sh` (or the raw
  `watch -n2 'cat /home/user/agents/*/status.json'`).

---

## Appendix: Earlier Revision — Claude-Desktop-Driven Flow

**Superseded by the lead-centric flow above.** This is Phase 1's original
design, kept here for reference: Claude Desktop (Windows) itself called the
`claude-orchestrator` tools and decomposed/dispatched work, with the operator talking
to Desktop instead of to a lead agent's pane. It predates the `ASK:` sentinel
and `respawn_agent`, and Desktop's ~4-minute tool-call timeout is what
originally forced every MCP tool to return instantly.

### A.1 Windows side — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "claude-orchestrator": {
      "command": "wsl.exe",
      "args": [
        "-e", "bash", "-lc",
        "cd /home/user/projects/orchestra && .venv/bin/python -m orchestra.server"
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
   workdir=/home/user/agents/<name>-wt. Put the full status protocol
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

Note this predates `ASK:` — the lead-centric protocol in §4 above adds it.

### A.3 Smoke-test task (send after the prompt above)

```
Using workers only: (1) agent alpha, workdir /home/user/agents/alpha-wt:
add __version__ = "0.1.0" to src/orchestra/__init__.py and a test for it
in tests/. (2) agent beta, workdir /home/user/agents/beta-wt: update the
EXISTING OpenAPI spec at docs/openapi.yaml to document the six orchestrator
tools. Beta must NOT create the file: if docs/openapi.yaml does not exist,
beta must print 'BLOCKED: docs/openapi.yaml not found, need its location or
permission to create it' and stop.
```

Beta's target file doesn't exist in its worktree, so it should emit
`BLOCKED:` — that's the engineered blocker the orchestrator must react to.

### A.4 Smoke test (pass criteria)

One task to Desktop → ≥2 concurrent workers in tmux → beta hits its
engineered blocker (reference a file that doesn't exist in its worktree) →
`status.json` flips to `blocked` → Desktop polls, sees it, reacts via
`send_to_agent`.

Observe:
- `tmux attach -t orchestra` — one window per agent (Ctrl-b n to cycle)
- `watch -n1 cat /home/user/agents/beta/status.json` — state flip
- `tail -f /home/user/agents/alpha/agent.log`


## Appendix: operator tools and quirks (Phase E learnings)

- **Reply to an agent reliably**: `.venv/bin/python scripts/say.py lead "<reply>"`
  — types into the pane with verified submission. Typing directly in the pane
  works, but the TUI silently ignores a bare Enter after any pause; if your
  Enter does nothing, type a space then Enter (or just use say.py).
- **Unstick loop**: run alongside the watchdog —
  `tmux new-window -d -t orchestra -n unstick 'while true; do <repo>/.venv/bin/python <repo>/scripts/unstick.py lead 600; sleep 60; done'`
  — force-submits any lead-pane draft unchanged for 10+ minutes.
- **Multiline messages from a shell**: quote with a literal heredoc so dashes
  and apostrophes survive:
  `scripts/agent_msg.py lead "$(cat <<'EOF' ... EOF)"`.
- **Watchdog + unstick both run in their own tmux windows** and should be
  wrapped in a restart loop; a crashed watchdog silently removes the stall
  backstop (it died once in Phase D/E).
