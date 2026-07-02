# Setup & Smoke Test (Windows 11 + WSL2)

## 1. WSL2 side

```bash
cd /home/tusk/projects/orchestra
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest   # 24 tests should pass

# Confirm subscription auth (no API key):
unset ANTHROPIC_API_KEY
claude /status

# Worktrees for the smoke test (in whatever repo you're testing against):
cd /home/tusk/myrepo
git worktree add /home/tusk/agents/alpha-wt -b agent-alpha
git worktree add /home/tusk/agents/beta-wt  -b agent-beta
```

## 2. Windows side — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "claude-orchestrator": {
      "command": "wsl.exe",
      "args": [
        "-e", "bash", "-lc",
        "cd /home/tusk/projects/orchestra && .venv/bin/python -m orchestra.server"
      ]
    }
  }
}
```

Fully **Quit** Claude Desktop (system tray → Quit, not just close) and
relaunch. Confirm `claude-orchestrator` shows connected in Settings →
Developer.

## 3. Orchestrator prompt (paste into Desktop)

```
You are the ORCHESTRATOR. You manage Claude Code worker agents through the
claude-orchestrator tools. Rules:
0. You NEVER do the work yourself. You must not write code, produce
   artifacts, or answer the task directly — your ONLY job is calling the
   claude-orchestrator tools and reporting on the workers. If a task seems
   doable in one step, still delegate it to a single worker.
1. Decompose my task into 2-4 INDEPENDENT, file-disjoint subtasks.
2. For each subtask call spawn_agent(name, task_prompt, workdir). Use
   workdir=/home/tusk/agents/<name>-wt. Put the full status protocol
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

## 4. Smoke-test task (send after the prompt above)

```
Using workers only: (1) agent alpha, workdir /home/tusk/agents/alpha-wt:
add __version__ = "0.1.0" to src/orchestra/__init__.py and a test for it
in tests/. (2) agent beta, workdir /home/tusk/agents/beta-wt: update the
EXISTING OpenAPI spec at docs/openapi.yaml to document the six orchestrator
tools. Beta must NOT create the file: if docs/openapi.yaml does not exist,
beta must print 'BLOCKED: docs/openapi.yaml not found, need its location or
permission to create it' and stop.
```

Beta's target file doesn't exist in its worktree, so it should emit
`BLOCKED:` — that's the engineered blocker the orchestrator must react to.

## 5. Smoke test (pass criteria)

One task to Desktop → ≥2 concurrent workers in tmux → beta hits its
engineered blocker (reference a file that doesn't exist in its worktree) →
`status.json` flips to `blocked` → Desktop polls, sees it, reacts via
`send_to_agent`.

Observe:
- `tmux attach -t orchestra` — one window per agent (Ctrl-b n to cycle)
- `watch -n1 cat /home/tusk/agents/beta/status.json` — state flip
- `tail -f /home/tusk/agents/alpha/agent.log`

## 6. Watching agents live (recommended during any run)

The workers are ordinary interactive Claude Code sessions — attach and you
see their full streaming TUIs in real time. Nothing about attaching disturbs
them.

- **One agent, full screen:** `tmux attach -t orchestra`, then `Ctrl-b n`/`p`
  to cycle windows, `Ctrl-b d` to detach.
- **Two agents at once:** open two WSL terminal tabs; in each, attach a
  grouped client showing a different window:
  `tmux new-session -t orchestra \; select-window -t alpha` (tab 1) and
  `... -t beta` (tab 2). Grouped clients can sit on different windows of the
  same session.
- **Everything scrolling in one pane:** `tail -f /home/tusk/agents/*/agent.log`
  (pipe-pane capture of every agent's output, interleaved).
- **Blackboard dashboard:** `watch -n2 'cat /home/tusk/agents/*/status.json'`.
