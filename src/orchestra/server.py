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
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP

from orchestra import config

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("orchestra.server")

# Snapshotted at import (not resolved per call) so tests can monkeypatch these
# module attributes, which is how every existing test injects a tmp blackboard.
AGENTS_DIR = config.agents_dir()
TMUX_SESSION = config.tmux_session()
HOOK_HANDLER = Path(__file__).resolve().parent / "write_status.py"
EMIT_HANDLER = Path(__file__).resolve().parent / "emit_event.py"
PYTHON = sys.executable
STARTUP_DELAY_S = 8  # give claude time to boot before send-keys delivers the task
CLAUDE_JSON = Path.home() / ".claude.json"
# States in which a second spawn_agent for the same name is a mistake.
ACTIVE_STATES = {"starting", "working", "blocked", "idle"}

# Seams for tests: faked so no test ever spawns tmux, claude, or sleeps.
RUN = subprocess.run
SLEEP = time.sleep

# The Claude Code TUI's paste detection swallows an Enter sent in the same
# burst as text, leaving the message typed but unsubmitted — always send
# Enter as a separate keystroke after this pause.
SUBMIT_DELAY_S = 1

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


def _pre_trust(workdir: str) -> None:
    """Mark workdir trusted in ~/.claude.json so the worker skips the folder-
    trust dialog (which otherwise wedges it before the REPL, eating the
    send-keys task delivery)."""
    data: dict = {}
    if CLAUDE_JSON.exists():
        try:
            data = json.loads(CLAUDE_JSON.read_text())
        except json.JSONDecodeError:
            logger.warning("could not parse %s; leaving it untouched", CLAUDE_JSON)
            return
    project = data.setdefault("projects", {}).setdefault(workdir, {})
    project["hasTrustDialogAccepted"] = True
    CLAUDE_JSON.write_text(json.dumps(data, indent=2))


def _ensure_session() -> None:
    have = RUN(["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True, text=True)
    if have.returncode != 0:
        RUN(["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", "home"], check=True)


def _launch_claude_window(name: str, workdir: str, model: str,
                          notify_target: str | None, session_flag: str) -> str:
    """Open a tmux window running `claude` with the given session flag
    (`--session-id <uuid>` for a fresh spawn, `--resume <id>` for a respawn)
    and attach pipe-pane logging. Shared by spawn_agent and respawn_agent so
    the launch flags never drift between the two paths. Returns the tmux
    target (`session:window`).
    """
    _ensure_session()
    _pre_trust(workdir)
    d = _agent_dir(name)
    log = d / "agent.log"

    hook_cmd = f"{PYTHON} {shlex.quote(str(HOOK_HANDLER))} {shlex.quote(name)}"
    emit_cmd = f"{PYTHON} {shlex.quote(str(EMIT_HANDLER))} {shlex.quote(name)}"
    settings = json.dumps({
        # Auto-approve project-scope .mcp.json (the lead's worktree carries
        # one so it can spawn workers); an approval dialog would wedge the
        # session exactly like the folder-trust prompt did.
        "enableAllProjectMcpServers": True,
        "hooks": {
            # emit_event mirrors every hook to the observability dashboard,
            # fire-and-forget, in its own process independent of the status
            # write. Runs for ALL tools; write_status keeps its narrow matcher.
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": emit_cmd}]},
            ],
            "Stop": [
                {"hooks": [
                    {"type": "command", "command": hook_cmd},
                    {"type": "command", "command": emit_cmd},
                ]},
            ],
            "PostToolUse": [
                {"matcher": "Write|Edit|Bash",
                 "hooks": [{"type": "command", "command": hook_cmd}]},
                {"hooks": [{"type": "command", "command": emit_cmd}]},
            ],
        }
    })

    # Env the hook handler needs (it runs inside the worker's environment).
    env_prefix = (
        f"{config.AGENTS_DIR_ENV}={shlex.quote(str(AGENTS_DIR))} "
        f"{config.TMUX_SESSION_ENV}={shlex.quote(TMUX_SESSION)} "
    )
    if notify_target:
        env_prefix += f"{config.NOTIFY_TARGET_ENV}={shlex.quote(notify_target)} "

    # Bare interactive claude: no stdin redirect and no stdout pipe — either
    # would break the interactive TUI. Task is delivered by send-keys below.
    launch = (
        f"cd {shlex.quote(workdir)} && "
        f"{env_prefix}"
        f"claude --settings {shlex.quote(settings)} "
        f"--model {shlex.quote(model)} "
        f"--permission-mode acceptEdits "
        # mcp__claude-orchestrator pre-approves the orchestrator MCP tools for
        # lead sessions — acceptEdits does not cover MCP calls and a
        # permission dialog wedges unattended agents.
        f'--allowedTools "Read,Write,Edit,Bash,mcp__claude-orchestrator" '
        f"{session_flag}"
    )
    target = f"{TMUX_SESSION}:{name}"
    RUN(["tmux", "new-window", "-t", TMUX_SESSION, "-n", name, launch], check=True)
    # pipe-pane logs the pane's output without touching the process's TTY.
    RUN(["tmux", "pipe-pane", "-o", "-t", target,
         f"cat >> {shlex.quote(str(log))}"], check=True)
    return target


def spawn_agent(name: str, task_prompt: str, workdir: str,
                model: str = "sonnet", notify_target: str | None = None) -> str:
    """Launch a Claude Code worker in its own tmux window.

    name: unique agent id (e.g. 'alpha'). task_prompt: the scoped subtask
    including the status protocol. workdir: absolute WSL path (must be a git
    worktree so agents never share files). model: claude model alias
    ('sonnet' for workers, 'opus' for the lead). notify_target: agent name
    whose inbox/pane gets nudged when this agent emits a sentinel.
    Returns immediately; poll get_agent_status for progress.
    """
    sp = AGENTS_DIR / name / "status.json"
    if sp.exists():
        state = json.loads(sp.read_text()).get("state")
        if state in ACTIVE_STATES:
            return (
                f"Agent '{name}' already exists in state '{state}' — not spawning a "
                f"duplicate. Poll get_agent_status, send_to_agent, or stop_agent first."
            )

    d = _agent_dir(name)
    (d / "task.md").write_text(task_prompt)
    (d / "inbox.md").write_text(f"# Inbox for {name}\n")

    # Persisted so respawn_agent/stop_agent can recover this agent's launch
    # parameters and worktree after this call returns.
    session_id = str(uuid.uuid4())
    _write_status(name, "starting", session_id=session_id, workdir=workdir,
                  model=model, notify_target=notify_target)

    target = _launch_claude_window(name, workdir, model, notify_target,
                                   f"--session-id {session_id}")

    # Deliver the task after claude boots: one short send-keys line pointing at
    # task.md (multiline send-keys is fragile). run-shell -b keeps this tool
    # instant instead of sleeping in-process.
    kickoff = (
        f"Read {d / 'task.md'} and execute the task in it. "
        f"Check {d / 'inbox.md'} when told new messages arrived."
    )
    deliver = (
        f"sleep {STARTUP_DELAY_S} && "
        f"tmux send-keys -t {shlex.quote(target)} {shlex.quote(kickoff)} && "
        f"sleep {SUBMIT_DELAY_S} && "
        f"tmux send-keys -t {shlex.quote(target)} Enter"
    )
    RUN(["tmux", "run-shell", "-b", deliver], check=True)

    logger.info("spawned agent %s in %s (cwd=%s)", name, target, workdir)
    return (
        f"Spawned agent '{name}' in tmux window {target}, cwd={workdir}. "
        f"Task will be delivered in ~{STARTUP_DELAY_S}s; poll get_agent_status."
    )


def respawn_agent(name: str) -> str:
    """Recover a wedged or killed worker by resuming its persisted Claude
    session in a fresh tmux window, replaying the same launch flags
    spawn_agent used originally. Returns immediately."""
    sp = AGENTS_DIR / name / "status.json"
    if not sp.exists():
        return f"No agent named '{name}' — nothing to respawn."

    status = json.loads(sp.read_text())
    session_id = status.get("session_id")
    workdir = status.get("workdir")
    if not session_id or not workdir:
        return (
            f"Agent '{name}' has no persisted session id/workdir to resume "
            f"(spawned before respawn support was added?)."
        )
    model = status.get("model") or "sonnet"
    notify_target = status.get("notify_target")

    # Best-effort: if the window is already gone this is a harmless no-op.
    RUN(["tmux", "kill-window", "-t", f"{TMUX_SESSION}:{name}"])

    target = _launch_claude_window(name, workdir, model, notify_target,
                                   f"--resume {session_id}")
    _write_status(name, "starting", session_id=session_id, workdir=workdir,
                  model=model, notify_target=notify_target)

    logger.info("respawned agent %s in %s (session=%s)", name, target, session_id)
    return (
        f"Respawned agent '{name}' in tmux window {target}, resuming session "
        f"{session_id}."
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
    target = f"{TMUX_SESSION}:{name}"
    RUN(["tmux", "send-keys", "-t", target, notify])
    SLEEP(SUBMIT_DELAY_S)
    RUN(["tmux", "send-keys", "-t", target, "Enter"])
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


def stop_agent(name: str, force: bool = False) -> str:
    """Terminate an agent's tmux window and mark it stopped on the blackboard.

    Refuses to kill an agent whose persisted worktree has uncommitted changes
    unless force=True, so a stop never silently discards in-progress work.
    """
    sp = AGENTS_DIR / name / "status.json"
    if sp.exists() and not force:
        workdir = json.loads(sp.read_text()).get("workdir")
        if workdir:
            status_check = RUN(["git", "-C", workdir, "status", "--porcelain"],
                               capture_output=True, text=True)
            if (status_check.stdout or "").strip():
                return (
                    f"Refusing to stop '{name}': its worktree at {workdir} has "
                    f"uncommitted changes. Commit them or call stop_agent with "
                    f"force=True."
                )

    RUN(["tmux", "kill-window", "-t", f"{TMUX_SESSION}:{name}"])
    if (AGENTS_DIR / name).exists():
        _write_status(name, "stopped")
    return f"Stopped '{name}'."


# Register tools while keeping the plain functions importable/callable in tests.
for _fn in (spawn_agent, send_to_agent, get_agent_status, list_agents,
            read_agent_log, stop_agent, respawn_agent):
    mcp.tool(_fn)

if __name__ == "__main__":
    mcp.run()  # stdio transport by default
