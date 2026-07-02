# Multi-Agent Claude Coding Architecture: Desktop Orchestrating Claude Code

## TL;DR
- **Phase 1 is buildable today with no API key.** A custom FastMCP server on the WSL2 side, bridged into Claude Desktop via `wsl.exe`, spawns and manages multiple worker `claude` agents in tmux panes. Desktop→worker direction is solid; worker→Desktop "unsolicited push" is BLOCKED at the protocol level, so you approximate it with a status-file blackboard the orchestrator polls via a tool. Auth runs entirely on subscription OAuth because Anthropic paused the June 15 billing change.
- **Phase 2 (true peer-to-peer) is a WORKAROUND, not native.** Agents cannot push unsolicited messages to each other, but a shared file-based/HTTP "blackboard" MCP server that all agents poll gives you a coordinated network today. `claude mcp serve` lets one Code instance expose its built-in tools to another, but with no MCP passthrough and stdio-only 1:1 coupling it is a weak peer link.
- **The single biggest constraint is that MCP tool calls are request/response from the client's side and Claude Desktop has a hard ~4-minute tool-call timeout with no sampling/elicitation support.** Every design decision below flows from those two facts: keep tool calls instant and make workers write state to files that the orchestrator reads on its next turn.

---

## Deliverable 0: Primitive Inventory

Each primitive is labeled and cited. Anything I could not verify against docs is marked UNVERIFIED and excluded from Phase 1.

| Primitive | Status | One-line description + citation |
|---|---|---|
| **Headless mode `claude -p` / `--print`** | WORKS-TODAY | Runs the full agent loop non-interactively, prints result, exits. Source: Claude Code Docs "Run Claude Code programmatically" (code.claude.com/docs/en/headless). |
| **`--output-format stream-json`** | WORKS-TODAY | Newline-delimited JSON events, one per line; requires `--verbose`, and token deltas require `--include-partial-messages`. Source: same headless doc. |
| **`--output-format json`** | WORKS-TODAY | Single structured envelope with `result`, `session_id`, `total_cost_usd`, `num_turns`. Source: headless doc. |
| **`--resume <id>` / `--continue`** | WORKS-TODAY | Resume a specific session by ID or the most recent; session lookup scoped to project dir + worktrees. Source: headless doc. |
| **`--input-format stream-json`** | WORKAROUND | Enables bidirectional streaming input, but the message format is undocumented beyond the flags table (open GitHub docs issue #24594). Excluded from the core Phase 1 wiring. |
| **`--bare` mode** | WORKS-TODAY (but excluded) | Skips OAuth/keychain, MCP, hooks, CLAUDE.md; REQUIRES `ANTHROPIC_API_KEY` or `apiKeyHelper`. Excluded because it forces an API key. Docs also state it "will become the default for `-p` in a future release." Source: headless doc + authentication doc. |
| **Claude Agent SDK (Python + TS)** | WORKAROUND | Same agent loop as a library; technically reads `CLAUDE_CODE_OAUTH_TOKEN`, but Anthropic's Legal page directs developers building products/services to use API keys. Excluded from the Phase 1 core to keep it no-key and ToS-clean. Source: code.claude.com/docs/en/legal-and-compliance. |
| **`claude mcp serve` (Code as MCP server)** | WORKS-TODAY | Exposes Claude Code's own tools (Bash, Read, Write, Edit, LS, Glob, Grep, etc.) over stdio to another MCP client. No MCP passthrough: connected clients cannot reach servers Code itself connects to. Source: code.claude.com/docs/en/mcp. |
| **MCP client role in Claude Code** | WORKS-TODAY | Code connects to stdio/HTTP/SSE/WebSocket MCP servers; supports list_changed and tool search. Source: code.claude.com/docs/en/mcp. |
| **MCP client role in Claude Desktop** | WORKS-TODAY | Desktop launches stdio servers from `claude_desktop_config.json` and remote servers via Custom Connectors (Streamable HTTP + OAuth). Source: support.claude.com local-MCP-server article. |
| **Claude Code subagents / Task tool** | WORKS-TODAY | Orchestrator spawns subagents via the Task tool; they run concurrently (practical ceiling ~10), each with an isolated context, reporting results back only to the spawning conversation. Source: code.claude.com/docs/en/agents. |
| **Agent Teams** | WORKS-TODAY (experimental, off by default) | Lead + teammates share a task list and message each other; split-pane mode needs tmux or iTerm2. Source: code.claude.com/docs/en/agent-teams. |
| **Background bash tasks** | WORKS-TODAY | Ctrl+B backgrounds a task; `/tasks` lists them. Under `claude -p`, background shells are killed ~5s after the final result. Source: headless doc + tools-reference. |
| **Hooks system** | WORKS-TODAY | ~30 lifecycle events incl. PreToolUse, PostToolUse, Stop, SubagentStop, SubagentStart, Notification, SessionStart/End. Command/HTTP/prompt/agent handler types; exit 2 blocks. Source: code.claude.com/docs/en/hooks. |
| **MCP Channels (`--channels`, `claude/channel` capability)** | WORKS-TODAY (research preview) | An MCP server can PUSH events into a running Claude Code session; requires v2.1.80+, subscription auth (no API key), and explicit `--channels` naming. Custom channels need `--dangerously-load-development-channels`. Source: code.claude.com/docs/en/channels-reference. |
| **Permission flags: `--allowedTools`, `--permission-mode`, `--dangerously-skip-permissions`** | WORKS-TODAY | Pre-approve tools or set session-wide modes (acceptEdits, dontAsk, plan). Source: headless doc + permission-modes doc. |
| **settings.json / CLAUDE.md / output styles / slash commands** | WORKS-TODAY | Project + user config, memory, and reusable commands; CLAUDE.md is read by all worktrees. Source: Claude Code settings + memory docs. |
| **Git worktrees (`--worktree`/`-w`, subagent `isolation: worktree`, `.worktreeinclude`)** | WORKS-TODAY | Isolated checkout per session/subagent under `.claude/worktrees/`; official guidance for parallel sessions. Source: code.claude.com/docs/en/worktrees. |
| **MCP progress notifications** | WORKS-TODAY (Code) / BLOCKED (Desktop) | Servers emit `notifications/progress`; Claude Code can reset idle timeout on them, but Claude Desktop's client ignores them and hard-terminates at ~4 min. Source: code.claude.com/docs/en/mcp + GitHub issues #22542, #44032, #65643. |
| **MCP sampling (`sampling/createMessage`)** | BLOCKED (Desktop) | Server-initiated LLM calls through the client. Not supported in Claude Desktop; also not supported as a client capability in Claude Code. Source: modelcontextprotocol.info sampling doc; anthropics/claude-code issue #1785. |
| **MCP elicitation** | WORKS-TODAY (Code) / BLOCKED (Desktop/Cowork) | Server requests structured user input mid-call. Works in Claude Code CLI; Cowork/Desktop returns an immediate cancel. Source: code.claude.com/docs/en/mcp; anthropics/claude-code issue #56243. |
| **Teleport / remote features** | UNVERIFIED | "Remote Control" (claude.ai/mobile driving a local session) exists but is not a load-bearing orchestration primitive here. Excluded from Phase 1. |

---

## Deliverable 1: Feasibility Assessment

**Strategic read: Phase 1 works today as an orchestrator-worker "blackboard" pattern. Phase 2's ambition (unsolicited peer-to-peer messaging) exceeds what MCP + Claude Desktop expose, so it degrades to a shared-state polling network. Nothing about the core Phase 1 loop requires an API key.**

### Phase 1 — achievable today
| Capability | Status | Evidence / limitation |
|---|---|---|
| Desktop decomposes a task and dispatches N Code agents | WORKS-TODAY | Desktop calls a custom MCP tool (`spawn_agent`) that launches worker `claude` sessions or tmux panes. |
| Agents run concurrently | WORKS-TODAY | OS-level: independent processes/tmux panes. Claude Code itself also runs subagents in parallel (~10 ceiling). |
| Desktop directs each agent | WORKS-TODAY | Orchestrator MCP tools: `spawn_agent`, `send_to_agent` (writes to a per-agent inbox file + tmux send-keys). |
| Agent reports status/blockers back to Desktop | WORKAROUND | Agents write to status files; orchestrator reads via `get_agent_status`. Workers CANNOT push unsolicited messages into a Desktop chat turn. |
| Desktop streams intermediate results | BLOCKED | Desktop MCP tool calls are request/response; a single tool call returns once. Desktop's client also ignores `notifications/progress` and times out at ~4 min. The orchestrator sees updates only when it calls a polling tool on its next turn. |

### Phase 2 — mostly missing platform features
| Capability | Status | Evidence / limitation |
|---|---|---|
| Agent A delegates directly to Agent B | WORKAROUND | Via a shared blackboard MCP server both connect to, or `claude mcp serve` making B's tools callable by A. No native "agent address book." |
| Unsolicited server-initiated messages to a client | BLOCKED (Desktop) / PARTIAL (Code) | MCP servers can only push into Claude *Code* sessions via Channels (research preview), not into Claude Desktop. Desktop has no server-push path. |
| Shared task state across all agents | WORKAROUND | File-based or HTTP blackboard/message-bus MCP server. Agent Teams has a native shared task list but is experimental, off by default, and Code-only. |
| MCP server-initiated requests (sampling/elicitation) | BLOCKED (Desktop) | Desktop supports neither sampling nor elicitation surfacing as client capabilities. |

**The Desktop timeout, verbatim:** the client raises "No result received from the Claude Desktop app after waiting 4 minutes" (GitHub issue #44032, anthropics/claude-code), and the same issue documents that Desktop "sends notifications/cancelled after 4min regardless of server state," across both stdio and HTTP transports, with the `timeout` field in `claude_desktop_config.json` ignored. This is the hard constraint that forces the blackboard design.

**Decision rule for what belongs in Phase 1:** if a capability requires the server to *initiate* a message to Claude Desktop, it is BLOCKED — design it as "worker writes file, orchestrator polls tool." If it only requires Desktop to *call* a tool, it is WORKS-TODAY.

---

## Deliverable 2: Phase 1 Architecture (topology)

```
WINDOWS 11 SIDE                                    │  WSL2 (Ubuntu) SIDE
                                                   │
┌─────────────────────────────┐                   │
│ Claude Desktop (ORCHESTRATOR)│                   │
│  auth: Desktop login (OAuth) │                   │
│  role: decompose, dispatch,  │                   │
│        poll status, react    │                   │
└───────────────┬──────────────┘                   │
                │ stdio (JSON-RPC)                  │
                │ claude_desktop_config.json        │
                │ command = wsl.exe                 │
                ▼                                   │
        ┌───────────────────────────────────────────────────────┐
        │  wsl.exe -e bash -lc "python3 orchestrator_server.py"   │
        │  (stdio bridge: Windows → WSL)                          │
        └───────────────────────────────┬───────────────────────┘
                                         │
                                         ▼  runs INSIDE WSL2
                         ┌──────────────────────────────────────┐
                         │  ORCHESTRATOR MCP SERVER (FastMCP,     │
                         │  Python). Tools:                       │
                         │   spawn_agent / send_to_agent /        │
                         │   get_agent_status / list_agents /     │
                         │   read_agent_log / stop_agent          │
                         │  State dir: /home/tusk/agents/         │
                         └───────┬───────────────┬───────────────┘
                                 │ tmux new-window / send-keys
                                 ▼               ▼
        ┌────────────────────────────┐ ┌────────────────────────────┐
        │ tmux window: agent-alpha    │ │ tmux window: agent-beta     │
        │  claude (interactive)       │ │  claude (interactive)       │
        │  cwd: worktree A            │ │  cwd: worktree B            │
        │  auth: claude CLI OAuth     │ │  auth: claude CLI OAuth     │
        │  Stop/PostToolUse hook ───┐ │ │  Stop/PostToolUse hook ───┐ │
        └───────────────────────────┼─┘ └───────────────────────────┼─┘
                                    │                                │
                                    ▼                                ▼
                         /home/tusk/agents/<name>/status.json  (blackboard)
                         /home/tusk/agents/<name>/inbox.md
                         /home/tusk/agents/<name>/agent.log
```

**Machine + auth annotation:**
- **Claude Desktop (Windows):** orchestrator. Auth = Desktop login (subscription OAuth). No API key.
- **Bridge:** `wsl.exe` launches the MCP server process inside WSL2. Pure stdio; no network transport, no key.
- **Orchestrator MCP server (WSL2):** plain Python + FastMCP. No Claude auth of its own — it only manages processes and files.
- **Worker `claude` instances (WSL2):** each authenticates with the logged-in `claude` CLI OAuth (subscription). No API key.
- **Mac (secondary):** identical pattern; the MCP server config path differs and the bridge is not needed (Desktop and Code are on the same Unix host, so `command` is `python3` directly). Cross-machine is not required for Phase 1; if you ever span machines, the explicit transport would be Streamable HTTP (run the blackboard as an HTTP MCP server and point both machines' Code instances at its URL).

---

## Deliverable 3: Phase 1 Implementation Plan

**Strategy first:** Claude Desktop is a poor long-running process manager (request/response tools, ~4-min timeout, no server push). So we make the *WSL MCP server* the process manager and the *filesystem* the message bus. Desktop only ever issues short tool calls: "spawn these," "what's the status," "send this note." Workers are real `claude` sessions in tmux panes (so you can watch them and they use subscription OAuth).

This mirrors Anthropic's own orchestrator-worker research pattern. Per Anthropic Engineering's "How we built our multi-agent research system," "Each subagent needs an objective, an output format, guidance on the tools and sources to use, and clear task boundaries" — the four-part contract that prevents subagent drift. That same write-up reports the multi-agent setup "outperformed a single Claude Opus 4 agent by 90.2% on their research eval," and embeds concrete scaling rules: "1 agent for simple fact-finding, 2 to 4 subagents for direct comparisons, and more than 10 subagents for complex research." Those rules justify the 2–4 agent default here.

### 3.1 What wires Desktop to Code
A **FastMCP stdio server running in WSL2**, bridged to Desktop via `wsl.exe`. It exposes management tools and never blocks: `spawn_agent` returns immediately after launching a tmux window; the orchestrator learns outcomes by polling `get_agent_status`, which reads `status.json` files. This sidesteps the Desktop timeout entirely because no tool call waits for an agent to finish.

**Why tmux panes running interactive `claude` rather than `claude -p` subprocesses:** (1) interactive `claude` keeps drawing from subscription limits with zero API-key risk; (2) you can visually attach to any pane to watch or intervene; (3) panes persist so a blocked agent waits for input instead of exiting; (4) it matches Nate's existing WSL2 + tmux + worktree workflow. `claude -p` is the clearly-subordinate alternative: use it for fire-and-forget subtasks where you want a single JSON result and no human-in-the-loop (spec in 5.4 notes).

### 3.2 How bidirectional messaging works
- **Desktop → agent (direction 1):** `send_to_agent(name, message)` appends to `inbox.md` and issues `tmux send-keys` to type the message into that pane's live `claude` session. WORKS-TODAY.
- **Agent → Desktop (direction 2):** each worker runs a **Stop hook** and a **PostToolUse hook** that write a structured `status.json` (state: working|blocked|done, last_action, blocker_reason, needs). The orchestrator calls `get_agent_status` on its next turn and reacts. This is the WORKAROUND for the BLOCKED "unsolicited push": the worker cannot interrupt Desktop, but its blocker is durably recorded and surfaced the instant Desktop polls.

**Decision rule:** anything the worker needs the orchestrator to *know* goes into `status.json` via a hook. Anything the orchestrator needs the worker to *do* goes through `send_to_agent`.

### 3.3 Orchestrator prompt structure
Three layers:
1. **Desktop project instructions (decomposition):** "You are the orchestrator. Decompose the user's task into 2–4 independent, file-disjoint subtasks. For each, call `spawn_agent` with a scoped prompt and a distinct worktree. After dispatching, poll `get_agent_status` for all agents each turn. When any agent is `blocked`, read its `blocker_reason` and either `send_to_agent` a resolution or re-scope. Never wait on a single tool call; always poll."
2. **Per-agent task template:** objective, output format, allowed tools, explicit file boundaries, and the status protocol contract (below). This four-part contract is exactly what Anthropic's multi-agent guidance prescribes.
3. **Status protocol:** every worker writes `status.json` on each Stop and after key tool calls; blockers set `state:"blocked"` + `blocker_reason` + `needs`.

---

## Deliverable 4: Phase 2 Gap Analysis

**What Phase 2 needs that does not exist natively:** (a) an inter-agent addressing/messaging protocol where any agent can send to any other; (b) server-initiated push into arbitrary clients (esp. Desktop); (c) shared mutable task state with locking. MCP gives us pieces but not the whole.

| Phase 2 approximation | Status | Notes |
|---|---|---|
| **Shared blackboard/message-bus MCP server** (all agents connect; tools `post_message`, `read_messages`, `claim_task`, `update_task`) | WORKAROUND | Best available today. Agents coordinate through shared state they all poll. File-locking or an HTTP+SQLite backend prevents races. Recommended Phase 2 substrate. |
| **`claude mcp serve` peer link** (Agent A calls Agent B's tools) | WORKAROUND | Real but weak: stdio 1:1, no MCP passthrough (B cannot expose the servers *it* connects to), and B exposes only its built-in tools, not a "delegate a task" verb. Good for "A asks B to edit a file," not for rich delegation. |
| **File-based mailboxes** (`inbox/<agent>.md`) | WORKAROUND | Simple, robust, matches Phase 1. No push; recipients poll. |
| **tmux `send-keys` peer messaging** | WORKAROUND | Agent A's Bash tool can `tmux send-keys` into Agent B's pane. Works today; fragile (injects raw text into a live prompt) and needs a sender/format convention. |
| **Channels for peer push into Code** | WORKAROUND (research preview, Code-only) | A channel MCP server can push events into a running Claude *Code* session — the only real server→agent push that exists. Requires v2.1.80+, subscription auth, explicit `--channels`, and custom servers need `--dangerously-load-development-channels`. Does NOT push into Desktop. |
| **Agent Teams native shared task list + peer messaging** | WORKAROUND (experimental, off by default, Code-only) | The closest first-party thing to Phase 2, but Desktop is not the lead and it is disabled by default. |
| **Server-initiated push into Desktop** | BLOCKED | Desktop supports no sampling, no elicitation surfacing, no channel push, and times out long calls. |
| **Desktop maintaining overall task state while agents talk peer-to-peer** | WORKAROUND | Desktop reads the same blackboard via its orchestrator MCP server; it stays the source of truth by polling, not by being pushed to. |

**Recommended Phase 2 design (committed):** promote the Phase 1 status directory into a proper **blackboard MCP server** (HTTP + SQLite, run in WSL2) that every worker AND the Desktop orchestrator connect to. Workers claim/post tasks and messages there; peer-to-peer delegation happens by one agent posting a task another claims. Desktop keeps global state by polling the same store. This is a WORKAROUND, not native P2P, but it is the only pattern fully buildable today without server-push.

---

## Deliverable 5: Minimal Working Prototype Spec (Windows 11 + WSL2)

**Pass criteria recap:** one task to Desktop → Desktop dispatches ≥2 concurrent Code agents with distinct scoped subtasks → ≥1 agent reports a blocker → Desktop visibly reacts. Everything below is buildable today, no API key.

### 5.1 Files to create (all inside WSL2 unless noted)

**`/home/tusk/orchestrator/requirements.txt`**
```
fastmcp>=2.0
```
(FastMCP bundles the MCP Python SDK and uvicorn; no other packages needed. `tmux` is installed via `sudo apt install tmux`, not pip.)

**`/home/tusk/orchestrator/orchestrator_server.py`**
```python
#!/usr/bin/env python3
"""Orchestrator MCP server: spawns and coordinates Claude Code worker agents
in tmux panes, using a filesystem blackboard for status. Runs inside WSL2.
No API key: worker `claude` sessions use the logged-in CLI OAuth (subscription).
"""
import json
import subprocess
import shlex
from pathlib import Path
from datetime import datetime, timezone
from fastmcp import FastMCP

AGENTS_DIR = Path("/home/tusk/agents")
AGENTS_DIR.mkdir(parents=True, exist_ok=True)
TMUX_SESSION = "orchestra"

mcp = FastMCP("claude-orchestrator")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_dir(name: str) -> Path:
    d = AGENTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ensure_session() -> None:
    # Create the tmux session if it does not exist.
    have = subprocess.run(
        ["tmux", "has-session", "-t", TMUX_SESSION],
        capture_output=True, text=True
    )
    if have.returncode != 0:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", "home"],
            check=True
        )


@mcp.tool()
def spawn_agent(name: str, task_prompt: str, workdir: str) -> str:
    """Launch a Claude Code worker in its own tmux window.

    name: unique agent id (e.g. 'alpha'). task_prompt: the scoped subtask +
    status protocol. workdir: absolute WSL path (ideally a git worktree).
    Returns immediately; does NOT wait for the agent to finish.
    """
    _ensure_session()
    d = _agent_dir(name)
    status = d / "status.json"
    log = d / "agent.log"
    inbox = d / "inbox.md"
    prompt_file = d / "task.md"

    prompt_file.write_text(task_prompt)
    inbox.write_text(f"# Inbox for {name}\n")
    status.write_text(json.dumps({
        "agent": name, "state": "starting", "last_action": None,
        "blocker_reason": None, "needs": None, "updated": _now(),
    }, indent=2))

    # Seed a status write via Stop/PostToolUse hooks using --settings inline
    # JSON so the worker records blocked/done with no project-level config.
    hook_cmd = (
        f"python3 /home/tusk/orchestrator/write_status.py {shlex.quote(name)}"
    )
    settings = json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": hook_cmd}]}],
            "PostToolUse": [{"matcher": "Write|Edit|Bash",
                             "hooks": [{"type": "command", "command": hook_cmd}]}],
        }
    })

    # Interactive claude in the pane => subscription OAuth, watchable, persistent.
    launch = (
        f"cd {shlex.quote(workdir)} && "
        f"claude --settings {shlex.quote(settings)} "
        f"< {shlex.quote(str(prompt_file))} 2>&1 | tee -a {shlex.quote(str(log))}"
    )
    subprocess.run(
        ["tmux", "new-window", "-t", TMUX_SESSION, "-n", name, launch],
        check=True
    )
    return f"Spawned agent '{name}' in tmux window {TMUX_SESSION}:{name}, cwd={workdir}"


@mcp.tool()
def list_agents() -> str:
    """List all known agents and their current state."""
    out = []
    for d in sorted(AGENTS_DIR.iterdir()):
        sp = d / "status.json"
        if sp.exists():
            s = json.loads(sp.read_text())
            out.append({"agent": s.get("agent"), "state": s.get("state"),
                        "updated": s.get("updated")})
    return json.dumps(out, indent=2) if out else "No agents yet."


@mcp.tool()
def get_agent_status(name: str) -> str:
    """Return the full status.json for one agent (state, blocker_reason, needs)."""
    sp = _agent_dir(name) / "status.json"
    return sp.read_text() if sp.exists() else f"No status for '{name}'."


@mcp.tool()
def read_agent_log(name: str, tail: int = 40) -> str:
    """Return the last `tail` lines of an agent's log for inspection."""
    lp = _agent_dir(name) / "agent.log"
    if not lp.exists():
        return f"No log for '{name}'."
    lines = lp.read_text().splitlines()
    return "\n".join(lines[-tail:])


@mcp.tool()
def send_to_agent(name: str, message: str) -> str:
    """Send a message to a running agent: append to its inbox AND type it into
    the live tmux pane so the interactive claude session receives it."""
    d = _agent_dir(name)
    with (d / "inbox.md").open("a") as f:
        f.write(f"\n[{_now()}] ORCHESTRATOR: {message}\n")
    # send-keys types the message then Enter into the agent's pane.
    subprocess.run(["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{name}",
                    message, "Enter"])
    return f"Delivered to '{name}'."


@mcp.tool()
def stop_agent(name: str) -> str:
    """Terminate an agent's tmux window."""
    subprocess.run(["tmux", "kill-window", "-t", f"{TMUX_SESSION}:{name}"])
    return f"Stopped '{name}'."


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
```

**`/home/tusk/orchestrator/write_status.py`** (the hook handler workers run)
```python
#!/usr/bin/env python3
"""Stop/PostToolUse hook handler. Reads hook JSON on stdin, updates the agent's
status.json. Detects a blocker by scanning the transcript tail for a sentinel
the worker is told to emit: 'BLOCKED:' followed by a reason.
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

AGENTS_DIR = Path("/home/tusk/agents")


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        event = json.load(sys.stdin)
    except Exception:
        event = {}

    d = AGENTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    status_path = d / "status.json"

    state = "working"
    blocker = None
    needs = None
    hook = event.get("hook_event_name", "")

    # On Stop, inspect the transcript tail for a BLOCKED sentinel or completion.
    transcript = event.get("transcript_path")
    tail_text = ""
    if transcript and Path(transcript).exists():
        tail_text = Path(transcript).read_text()[-4000:]

    if "BLOCKED:" in tail_text:
        state = "blocked"
        after = tail_text.split("BLOCKED:", 1)[1].strip()
        blocker = after.splitlines()[0][:300] if after else "unspecified"
        needs = "orchestrator_input"
    elif hook == "Stop":
        state = "done"

    status_path.write_text(json.dumps({
        "agent": name,
        "state": state,
        "last_action": hook,
        "blocker_reason": blocker,
        "needs": needs,
        "updated": datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    # Exit 0 = allow. (Exit 2 on Stop would force the worker to keep going.)
    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
```

**`%USERPROFILE%\AppData\Roaming\Claude\claude_desktop_config.json`** (Windows side)
```json
{
  "mcpServers": {
    "claude-orchestrator": {
      "command": "wsl.exe",
      "args": [
        "-e", "bash", "-lc",
        "cd /home/tusk/orchestrator && python3 orchestrator_server.py"
      ]
    }
  }
}
```

### 5.2 Exact commands to run

In WSL2:
```bash
sudo apt update && sudo apt install -y tmux
cd /home/tusk/orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
chmod +x orchestrator_server.py write_status.py
# sanity check the CLI is logged in on subscription OAuth (no API key):
unset ANTHROPIC_API_KEY
claude /status        # confirm subscription auth
# create two worktrees for file-disjoint work:
cd /home/tusk/myrepo
git worktree add /home/tusk/agents/alpha-wt -b agent-alpha
git worktree add /home/tusk/agents/beta-wt  -b agent-beta
```
> If the venv's Python is not the interpreter the config invokes, change the config's launch string to the venv Python explicitly, e.g. `.../.venv/bin/python3 orchestrator_server.py`, and use the venv python in the hook command too.

On Windows: save the config JSON, then fully **Quit** Claude Desktop (not just close the window) and relaunch. Confirm the `claude-orchestrator` server shows connected in Settings → Developer.

### 5.3 Orchestrator prompt (paste into Desktop)
```
You are the ORCHESTRATOR. You manage Claude Code worker agents through the
claude-orchestrator tools. Rules:
1. Decompose my task into 2-4 INDEPENDENT, file-disjoint subtasks.
2. For each subtask call spawn_agent(name, task_prompt, workdir). Use
   workdir=/home/tusk/agents/<name>-wt. Put the full status protocol
   (below) into every task_prompt.
3. After spawning, call list_agents and get_agent_status each turn to poll.
   NEVER assume an agent is done without polling.
4. If any agent state is "blocked", read blocker_reason, then either
   send_to_agent(name, "<resolution>") or re-scope and re-dispatch.
5. Report progress to me after each poll.

STATUS PROTOCOL to embed in every task_prompt:
"You are agent <NAME>. Objective: <one sentence>. Output format: <what to
produce>. Allowed tools: Read, Write, Edit, Bash. Stay strictly within your
worktree. If you cannot proceed (missing dependency, ambiguous spec, failing
build you cannot fix), print a line starting exactly with 'BLOCKED:' followed
by one sentence describing what you need, then stop. Otherwise complete the
task and stop."
```

### 5.4 Per-agent prompt templates (the orchestrator fills these)
- **Agent alpha (will succeed):** "You are agent alpha. Objective: add a `/health` endpoint returning `{status:'ok'}`. Output format: edited source + a one-line summary. Allowed tools: Read, Write, Edit, Bash. Stay in /home/tusk/agents/alpha-wt. If blocked, print 'BLOCKED: ...' and stop."
- **Agent beta (engineered to block):** "You are agent beta. Objective: wire the new endpoint into the OpenAPI spec at `docs/openapi.yaml`. Output format: edited spec. Allowed tools: Read, Write, Edit, Bash. Stay in /home/tusk/agents/beta-wt. If the file does not exist, print 'BLOCKED: docs/openapi.yaml not found, need its location or permission to create it' and stop." (Since that file is absent in the worktree, beta emits BLOCKED, its Stop hook writes `state:"blocked"`, and the orchestrator's next `get_agent_status` poll surfaces it — at which point Desktop reacts by `send_to_agent(beta, "create docs/openapi.yaml as a new minimal spec")`.)

> **`claude -p` variant (subordinate option):** for a fire-and-forget subtask, replace the tmux launch with `claude -p "<prompt>" --output-format json --allowedTools "Read,Edit"` captured to a file the orchestrator reads. Use only when no human-in-the-loop is needed; you lose the watchable pane and the ability to `send_to_agent` mid-run.

### 5.5 How the user observes each step
- **tmux panes:** `tmux attach -t orchestra` in WSL2 shows one window per agent with live `claude` output. Switch windows with `Ctrl-b n`.
- **Log files:** `tail -f /home/tusk/agents/alpha/agent.log`.
- **Blackboard:** `watch -n1 cat /home/tusk/agents/beta/status.json` shows the state flip to `blocked`.
- **Desktop chat:** the orchestrator narrates each poll and its reaction to the blocker, satisfying "Desktop visibly reacts."

---

## Recommendations (staged, with thresholds)

1. **Build the 5.x prototype first (this week).** It proves the whole Phase 1 loop with ~150 lines of Python and no API key. Success threshold: beta's blocker appears in `status.json` and Desktop's chat shows it reacting. If Desktop's ~4-min timeout ever bites, it means a tool call is doing real work — move that work into a spawned agent and keep tools instant.
2. **Harden the blackboard before scaling past ~3 agents.** Replace flat `status.json` files with the HTTP+SQLite blackboard MCP server (Deliverable 4) once you run 4+ agents or hit file-race symptoms (stale reads, clobbered status). Threshold to switch: any observed lost update, or >3 concurrent agents.
3. **Adopt worktree isolation for every agent that writes files.** You already use worktrees; make `spawn_agent` require a worktree path so two agents never edit the same file. This is a hard guarantee, not a guideline.
4. **Keep auth on subscription OAuth; do not introduce an API key unless you cross into always-on/production automation.** Anthropic's June 15, 2026 subscriber notice states pricing for the Claude Agent SDK "isn't changing for the time being," that they are "revising the plan to better support how users build with Claude subscriptions," and that they "will share advance notice before any future change takes effect." For now `claude -p` and interactive `claude` both draw from your subscription. (The shelved May 14 plan would have moved Agent SDK/`claude -p` usage to a separate monthly credit, roughly $20 Pro up to $200 Max/Enterprise, billed at standard API rates — reportedly unwinding a subsidy that ran, per Zed's growth lead Franciska Dethlefsen citing engineer Matthew Diakonov, "at roughly 15 to 30 times the equivalent API cost.") Reassess only if that change un-pauses, or if you move this to an unattended server, where an API key under Commercial Terms is the correct, ToS-clean path.
5. **Treat Phase 2 peer-to-peer as blackboard-mediated, not direct push.** Commit to the shared-store pattern; revisit only if Anthropic promotes Channels out of research preview with a Desktop-side push path, or ships Agent Teams as a stable, Desktop-drivable feature. Those two are the benchmarks that would unlock "real" P2P.
6. **Do not put the Agent SDK in the no-key core.** If you later want SDK ergonomics (structured outputs, callbacks), isolate it behind an API key and mark it clearly optional, because the Legal page steers SDK product-building to API keys.

## Caveats
- **The billing pause is temporary.** The no-API-key economics rest on Anthropic's June 15 pause of the Agent SDK credit change; they committed to advance notice before any future change. Watch for it.
- **`--bare` will reportedly become the `-p` default "in a future release."** Since `--bare` requires an API key, a future default flip could break no-key headless subtasks. The prototype uses interactive `claude` in panes specifically to avoid depending on `-p` defaults.
- **`claude mcp serve` is a genuinely weak peer link** (stdio 1:1, no passthrough, built-in tools only) — do not over-invest in it for Phase 2.
- **Channels and Agent Teams are research-preview / experimental** and may change syntax or availability without notice; keep them out of anything you need to be reproducible.
- **Windows stdio quirks:** launch the WSL server via `wsl.exe -e bash -lc "..."`; if the MSIX build of Desktop virtualizes the config path, edit the real `%APPDATA%\Claude\claude_desktop_config.json` and fully quit/relaunch. Avoid `npx`-style bare commands on the Windows side; use `wsl.exe`. Always use `%USERPROFILE%` for the Windows config path, never `$HOME`.
- **OAuth token refresh on long runs:** `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) is a one-year token, but the underlying access tokens are short-lived and auto-refresh; very long unattended sessions can still hit refresh hiccups — another reason to prefer interactive panes you can see. Also: if `ANTHROPIC_API_KEY` is present in the environment, `-p` mode always uses it, so `unset` it to stay on subscription.
- **Path hygiene across machines:** Windows home is `%USERPROFILE%` (user Tusk), WSL/Linux home is `/home/tusk`, Mac home is `/Users/chipmunk`. Never conflate them; the WSL server and worktrees live under `/home/tusk`, the Desktop config under `%USERPROFILE%\AppData\Roaming\Claude`.