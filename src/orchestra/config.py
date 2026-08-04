"""Single source of truth for every ORCHESTRA_* setting.

Before this module the default blackboard path was spelled independently in
nine files, so changing one value meant editing nine and hoping you found
them all. Everything that needs a setting now reads it from here.

**Stdlib only, and it must stay that way.** `launcher.py` imports this at
module scope and documents that it stays importable without fastmcp; the two
hook handlers (`write_status.py`, `emit_event.py`) run as bare scripts on
every agent turn and must not pay an import cost. `tests/test_config.py`
enforces this by importing the module in a clean interpreter.

Resolvers are FUNCTIONS, not module constants, so they read the environment
at call time. Callers that want a snapshot bind one at import (see
`server.AGENTS_DIR`); callers that want live values call the function.

Shell scripts cannot import this. They source `scripts/config.sh`, which
restates the same defaults in bash; `tests/test_config.py` asserts the two
agree so the pair cannot drift silently.
"""
import os
from pathlib import Path

# --- environment variable names ------------------------------------------
AGENTS_DIR_ENV = "ORCHESTRA_AGENTS_DIR"
TMUX_SESSION_ENV = "ORCHESTRA_TMUX_SESSION"
AGENT_NAME_ENV = "ORCHESTRA_AGENT_NAME"
NOTIFY_TARGET_ENV = "ORCHESTRA_NOTIFY_TARGET"
NUDGE_CMD_ENV = "ORCHESTRA_NUDGE_CMD"
EVENTS_URL_ENV = "ORCHESTRA_EVENTS_URL"
# Deliberately unprefixed: launcher.py passes these straight through to the
# upstream observability repo's start-system.sh, which reads these exact
# names. Renaming them to ORCHESTRA_* would break that hand-off.
SERVER_PORT_ENV = "SERVER_PORT"
CLIENT_PORT_ENV = "CLIENT_PORT"

# --- defaults -------------------------------------------------------------
# Visible (not a dotdir) because the blackboard holds live git worktrees an
# operator has to cd into, and namespaced so orchestra never claims a generic
# directory name in somebody's home.
DEFAULT_AGENTS_DIR_NAME = "orchestra-agents"
DEFAULT_TMUX_SESSION = "orchestra"
DEFAULT_AGENT_NAME = "peer"
DEFAULT_NUDGE_CMD = "scripts/agent_msg.py"
DEFAULT_EVENTS_URL = "http://localhost:4000/events"
DEFAULT_SERVER_PORT = 4000
DEFAULT_CLIENT_PORT = 5173


def default_agents_dir() -> Path:
    """Blackboard root when nothing is configured.

    Derived from $HOME per call rather than frozen at import: an absolute
    literal is wrong on every machine but the one it was written on, which is
    the defect this module exists to fix.
    """
    return Path.home() / DEFAULT_AGENTS_DIR_NAME


def agents_dir() -> Path:
    """Root of the filesystem blackboard (`<name>/status.json`, `inbox.md`,
    `task.md`, `agent.log`, and the agents' git worktrees).

    An empty value is treated as unset: it means a misconfigured environment,
    never a request to use the process's current directory as the blackboard.
    """
    return Path(os.environ.get(AGENTS_DIR_ENV) or default_agents_dir())


def tmux_session() -> str:
    """tmux session holding one window per agent."""
    return os.environ.get(TMUX_SESSION_ENV) or DEFAULT_TMUX_SESSION


def agent_name() -> str:
    """Name this process reports as when messaging another agent."""
    return os.environ.get(AGENT_NAME_ENV) or DEFAULT_AGENT_NAME


def notify_target() -> str | None:
    """Agent to nudge when this agent emits a sentinel, or None.

    No default, because having nobody to notify is the normal case for the
    lead. Absence has to stay distinguishable from any agent name.
    """
    return os.environ.get(NOTIFY_TARGET_ENV) or None


def nudge_cmd() -> str:
    """Command the watchdog invokes as `<cmd> lead <reason>`."""
    return os.environ.get(NUDGE_CMD_ENV) or DEFAULT_NUDGE_CMD


def events_url() -> str | None:
    """Observability endpoint, or None when emission is disabled.

    Three-way, unlike every other setting: unset means the default endpoint,
    and an EMPTY string is the documented opt-out rather than a mistake.
    """
    val = os.environ.get(EVENTS_URL_ENV)
    if val is None:
        return DEFAULT_EVENTS_URL
    if val == "":
        return None
    return val


def _port(env_name: str, default: int) -> int:
    """Unset falls back; a garbage value raises rather than silently
    defaulting, so an operator who sets a bad port hears about it instead of
    watching orchestra bind the port they thought they had overridden."""
    raw = os.environ.get(env_name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_name}={raw!r} is not a valid port number") from exc


def server_port() -> int:
    """Observability events API port."""
    return _port(SERVER_PORT_ENV, DEFAULT_SERVER_PORT)


def client_port() -> int:
    """Observability dashboard UI port."""
    return _port(CLIENT_PORT_ENV, DEFAULT_CLIENT_PORT)
