#!/usr/bin/env python3
"""Orchestrator MCP server: spawns and coordinates Claude Code worker agents
in tmux windows, using a filesystem blackboard for status. Runs inside WSL2,
bridged to Claude Desktop via wsl.exe (stdio).

No API key anywhere: worker `claude` sessions use the logged-in CLI OAuth
(subscription). Every tool returns instantly — Desktop's ~4-minute tool-call
timeout means no tool may ever wait on an agent.
"""
import json
import logging
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("orchestra.server")

AGENTS_DIR = Path(os.environ.get("ORCHESTRA_AGENTS_DIR", "/home/tusk/agents"))
TMUX_SESSION = os.environ.get("ORCHESTRA_TMUX_SESSION", "orchestra")
HOOK_HANDLER = Path(__file__).resolve().parent / "write_status.py"
PYTHON = sys.executable
STARTUP_DELAY_S = 8  # give claude time to boot before send-keys delivers the task

# Seam for tests: faked so no test ever spawns tmux or claude.
RUN = subprocess.run

mcp = FastMCP("claude-orchestrator")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_dir(name: str) -> Path:
    d = AGENTS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_status(name: str, state: str, **extra: str | None) -> None:
    status = {
        "agent": name,
        "state": state,
        "last_action": None,
        "blocker_reason": None,
        "needs": None,
        "summary": None,
        "updated": _now(),
    }
    status.update(extra)
    (_agent_dir(name) / "status.json").write_text(json.dumps(status, indent=2))


def _ensure_session() -> None:
    have = RUN(["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True, text=True)
    if have.returncode != 0:
        RUN(["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", "home"], check=True)


def spawn_agent(name: str, task_prompt: str, workdir: str) -> str:
    """Launch a Claude Code worker in its own tmux window.

    name: unique agent id (e.g. 'alpha'). task_prompt: the scoped subtask
    including the status protocol. workdir: absolute WSL path (must be a git
    worktree so agents never share files). Returns immediately; poll
    get_agent_status for progress.
    """
    _ensure_session()
    d = _agent_dir(name)
    log = d / "agent.log"
    (d / "task.md").write_text(task_prompt)
    (d / "inbox.md").write_text(f"# Inbox for {name}\n")
    _write_status(name, "starting")

    hook_cmd = f"{PYTHON} {shlex.quote(str(HOOK_HANDLER))} {shlex.quote(name)}"
    settings = json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": hook_cmd}]}],
            "PostToolUse": [{"matcher": "Write|Edit|Bash",
                             "hooks": [{"type": "command", "command": hook_cmd}]}],
        }
    })

    # Bare interactive claude: no stdin redirect and no stdout pipe — either
    # would break the interactive TUI. Task is delivered by send-keys below.
    launch = (
        f"cd {shlex.quote(workdir)} && "
        f"claude --settings {shlex.quote(settings)} "
        f"--permission-mode acceptEdits "
        f'--allowedTools "Read,Write,Edit,Bash"'
    )
    target = f"{TMUX_SESSION}:{name}"
    RUN(["tmux", "new-window", "-t", TMUX_SESSION, "-n", name, launch], check=True)
    # pipe-pane logs the pane's output without touching the process's TTY.
    RUN(["tmux", "pipe-pane", "-o", "-t", target,
         f"cat >> {shlex.quote(str(log))}"], check=True)

    # Deliver the task after claude boots: one short send-keys line pointing at
    # task.md (multiline send-keys is fragile). run-shell -b keeps this tool
    # instant instead of sleeping in-process.
    kickoff = (
        f"Read {d / 'task.md'} and execute the task in it. "
        f"Check {d / 'inbox.md'} when told new messages arrived."
    )
    deliver = (
        f"sleep {STARTUP_DELAY_S} && "
        f"tmux send-keys -t {shlex.quote(target)} {shlex.quote(kickoff)} Enter"
    )
    RUN(["tmux", "run-shell", "-b", deliver], check=True)

    logger.info("spawned agent %s in %s (cwd=%s)", name, target, workdir)
    return (
        f"Spawned agent '{name}' in tmux window {target}, cwd={workdir}. "
        f"Task will be delivered in ~{STARTUP_DELAY_S}s; poll get_agent_status."
    )


def send_to_agent(name: str, message: str) -> str:
    """Send a message to a running agent: append it to the agent's inbox and
    type a short notify line into its live tmux pane."""
    d = AGENTS_DIR / name
    if not d.exists():
        return f"No agent named '{name}'."
    with (d / "inbox.md").open("a") as f:
        f.write(f"\n[{_now()}] ORCHESTRATOR: {message}\n")
    # Full (possibly multiline) message lives in the inbox; the pane gets one
    # tmux-safe line.
    notify = " ".join(message.split()) or f"New message in {d / 'inbox.md'}"
    if len(notify) > 200 or "\n" in message:
        notify = f"New orchestrator message — read {d / 'inbox.md'} and act on it."
    RUN(["tmux", "send-keys", "-t", f"{TMUX_SESSION}:{name}", notify, "Enter"])
    return f"Delivered to '{name}'."


def get_agent_status(name: str) -> str:
    """Return the full status.json for one agent (state, blocker_reason, needs)."""
    sp = AGENTS_DIR / name / "status.json"
    return sp.read_text() if sp.exists() else f"No status for '{name}'."


def list_agents() -> str:
    """List all known agents with their current state and last update time."""
    out = []
    if AGENTS_DIR.exists():
        for d in sorted(AGENTS_DIR.iterdir()):
            sp = d / "status.json"
            if sp.exists():
                s = json.loads(sp.read_text())
                out.append({"agent": s.get("agent"), "state": s.get("state"),
                            "updated": s.get("updated")})
    return json.dumps(out, indent=2) if out else "No agents yet."


def read_agent_log(name: str, tail: int = 40) -> str:
    """Return the last `tail` lines of an agent's pane log."""
    lp = AGENTS_DIR / name / "agent.log"
    if not lp.exists():
        return f"No log for '{name}'."
    return "\n".join(lp.read_text().splitlines()[-tail:])


def stop_agent(name: str) -> str:
    """Terminate an agent's tmux window and mark it stopped on the blackboard."""
    RUN(["tmux", "kill-window", "-t", f"{TMUX_SESSION}:{name}"])
    if (AGENTS_DIR / name).exists():
        _write_status(name, "stopped")
    return f"Stopped '{name}'."


# Register tools while keeping the plain functions importable/callable in tests.
for _fn in (spawn_agent, send_to_agent, get_agent_status, list_agents,
            read_agent_log, stop_agent):
    mcp.tool(_fn)

if __name__ == "__main__":
    mcp.run()  # stdio transport by default
