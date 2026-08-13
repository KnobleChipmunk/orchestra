#!/usr/bin/env python3
"""Idempotent bring-up for the orchestra system: tmux session, observability
dashboard, lead agent, and watchdog/unstick loops.

Strictly additive to tmux — only has-session/list-windows/new-session/
new-window are ever issued. Never kill-*, since a live production session may
be attached when this runs for real. Stdlib-only at module scope:
`orchestra.server` (which pulls fastmcp) is imported lazily, only inside
ensure_lead's default spawn path, and only once a spawn is actually about to
happen.
"""
import argparse
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

State = Literal["UP", "STARTED", "SKIPPED", "FAILED"]


@dataclass(frozen=True)
class Result:
    name: str
    state: State
    detail: str = ""


REPO_ROOT = Path(__file__).resolve().parents[2]
# Mirrors server.py's AGENTS_DIR convention exactly, duplicated (not
# imported) to keep this module stdlib-only at import time.
AGENTS_DIR = Path(os.environ.get("ORCHESTRA_AGENTS_DIR", "/home/user/agents"))
TMUX_SESSION = os.environ.get("ORCHESTRA_TMUX_SESSION", "orchestra")
OBS_REPO = Path.home() / "projects" / "multi-agent-observability"
SERVER_PORT = int(os.environ.get("SERVER_PORT", "4000"))
CLIENT_PORT = int(os.environ.get("CLIENT_PORT", "5173"))
LEAD_WT = AGENTS_DIR / "lead-wt"
LEAD_MD = REPO_ROOT / "docs" / "LEAD.md"

# Seams for tests: faked so no test ever spawns tmux, sleeps, or touches the
# network.
RUN = subprocess.run
SLEEP = time.sleep
URLOPEN = urlopen

SpawnFn = Callable[..., str]


def check_prereqs(no_dashboard: bool) -> list[str]:
    """Collect every unmet prerequisite (not just the first) so an operator
    sees the whole picture in one run."""
    errors: list[str] = []

    in_venv = sys.prefix != sys.base_prefix
    importable = (importlib.util.find_spec("orchestra") is not None
                  and importlib.util.find_spec("fastmcp") is not None)
    if not (in_venv and importable):
        errors.append(
            "not running under the orchestra project's venv interpreter (or "
            "'orchestra'/'fastmcp' aren't importable) — run: "
            f"{REPO_ROOT}/.venv/bin/python {REPO_ROOT}/scripts/up.py "
            f'(first time: cd {REPO_ROOT} && python3 -m venv .venv && '
            '.venv/bin/pip install -e ".[dev]")'
        )

    if os.environ.get("ANTHROPIC_API_KEY"):
        errors.append(
            "ANTHROPIC_API_KEY is set — orchestra agents use subscription "
            "OAuth, not an API key; unset ANTHROPIC_API_KEY before continuing"
        )

    if shutil.which("tmux") is None:
        errors.append("tmux is not installed or not on PATH — install it (e.g. `apt install tmux`)")

    if not LEAD_MD.exists():
        errors.append(f"{LEAD_MD} is missing — cannot build the lead's operating brief")

    if not no_dashboard:
        if not OBS_REPO.is_dir():
            errors.append(
                f"{OBS_REPO} not found — clone it as a sibling repo: "
                "git clone https://github.com/disler/claude-code-hooks-multi-agent-observability "
                f"{OBS_REPO}"
            )
        if find_bun() is None:
            errors.append(
                "bun not found on PATH or at ~/.bun/bin/bun — install it: "
                "curl -fsSL https://bun.sh/install | bash"
            )

    return errors


def find_bun() -> str | None:
    found = shutil.which("bun")
    if found:
        return found
    fallback = Path.home() / ".bun" / "bin" / "bun"
    return str(fallback) if fallback.exists() else None


def http_up(url: str, timeout: float = 2.0) -> bool:
    """Any HTTP response — even an error status — means something answered.
    Only a connection-level failure (refused, timed out, black-holed) means
    down. The 2.0s bound is the WSL2 black-hole cap (docs/observability.md)."""
    try:
        URLOPEN(url, timeout=timeout).close()
        return True
    except HTTPError:
        return True
    except (URLError, OSError):
        return False


def dashboard_healthy() -> bool:
    return (http_up(f"http://localhost:{SERVER_PORT}/events/recent")
            and http_up(f"http://localhost:{CLIENT_PORT}/"))


def tmux_window_names() -> set[str]:
    result = RUN(["tmux", "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}"],
                 capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    return {name for name in result.stdout.splitlines() if name}


def ensure_session(dry_run: bool) -> None:
    """Mirrors server._ensure_session on this module's own RUN seam (not
    imported, to keep tmux plumbing fastmcp-free)."""
    have = RUN(["tmux", "has-session", "-t", TMUX_SESSION], capture_output=True, text=True)
    if have.returncode != 0 and not dry_run:
        RUN(["tmux", "new-session", "-d", "-s", TMUX_SESSION, "-n", "home"], check=True)


def _dashboard_start_cmd() -> str:
    # A fresh `tmux new-window` shell may not source ~/.bun/bin onto PATH,
    # so prepend it explicitly rather than trust the child shell's profile.
    bun = find_bun()
    bun_dir = str(Path(bun).parent) if bun else str(Path.home() / ".bun" / "bin")
    return (
        f'export PATH="{bun_dir}:$PATH" && '
        f'cd {shlex.quote(str(OBS_REPO))} && '
        f'SERVER_PORT={SERVER_PORT} CLIENT_PORT={CLIENT_PORT} ./scripts/start-system.sh'
    )


def start_dashboard(dry_run: bool, attempts: int = 30, delay: float = 2.0) -> Result:
    ensure_session(dry_run)

    if dashboard_healthy():
        return Result("dashboard", "UP", f"http://localhost:{CLIENT_PORT} (already running)")

    obs_exists = "obs" in tmux_window_names()

    if not obs_exists:
        if dry_run:
            return Result("dashboard", "STARTED",
                           f"WOULD START tmux window '{TMUX_SESSION}:obs' running start-system.sh")
        # kill_port in start-system.sh kills whatever's bound to 4000/5173 —
        # only ever reached here because dashboard_healthy() just returned
        # False AND no 'obs' window exists to poll instead.
        RUN(["tmux", "new-window", "-d", "-t", TMUX_SESSION, "-n", "obs",
             _dashboard_start_cmd()], check=True)
    elif dry_run:
        return Result("dashboard", "SKIPPED",
                       f"tmux window '{TMUX_SESSION}:obs' already exists (dry-run: not polling)")

    for _ in range(attempts):
        SLEEP(delay)
        if dashboard_healthy():
            return Result("dashboard", "STARTED", f"http://localhost:{CLIENT_PORT}")

    return Result("dashboard", "FAILED",
                  f"{TMUX_SESSION}:obs did not become healthy after {attempts} attempts "
                  f"({attempts * delay:.0f}s) — check `tmux capture-pane -t {TMUX_SESSION}:obs -p`")


def ensure_window(name: str, cmd: str, dry_run: bool) -> Result:
    if name in tmux_window_names():
        return Result(name, "SKIPPED", f"tmux window '{TMUX_SESSION}:{name}' already exists")
    if dry_run:
        return Result(name, "STARTED", f"WOULD START tmux window '{TMUX_SESSION}:{name}'")
    RUN(["tmux", "new-window", "-d", "-t", TMUX_SESSION, "-n", name, cmd], check=True)
    return Result(name, "STARTED", f"tmux window '{TMUX_SESSION}:{name}' created")


def _default_spawn_agent(name: str, task_prompt: str, workdir: str, *, model: str) -> str:
    from orchestra.server import spawn_agent  # lazy: only when actually spawning
    return spawn_agent(name, task_prompt, workdir, model=model)


def ensure_lead(dry_run: bool, spawn_fn: SpawnFn | None = None) -> Result:
    if "lead" in tmux_window_names():
        return Result("lead", "SKIPPED", f"tmux window '{TMUX_SESSION}:lead' already exists")

    if not LEAD_WT.is_dir():
        return Result("lead", "FAILED",
            f"{LEAD_WT} does not exist — bootstrap it: cd <your project repo> && "
            f"git worktree add {LEAD_WT} -b agent-lead (see docs/setup.md §2)")
    if not (LEAD_WT / ".mcp.json").exists():
        return Result("lead", "FAILED",
            f"{LEAD_WT}/.mcp.json is missing — see docs/setup.md §2 to create it")
    if not LEAD_MD.exists():
        return Result("lead", "FAILED", f"{LEAD_MD} not found — cannot build the lead's prompt")

    prompt = LEAD_MD.read_text() + "\n\nAwait instructions from the operator in your pane."

    if dry_run:
        return Result("lead", "STARTED", f"WOULD SPAWN lead in {LEAD_WT} (model=fable)")

    spawn = spawn_fn or _default_spawn_agent
    # notify_target intentionally omitted: the lead has nobody above it to notify.
    outcome = spawn("lead", prompt, str(LEAD_WT), model="fable")

    if outcome.startswith("Agent 'lead' already exists"):
        return Result("lead", "FAILED",
            f"{outcome} Use respawn_agent('lead'), not up.py, to recover an existing lead.")
    return Result("lead", "STARTED", outcome)


def watchdog_cmd() -> str:
    return f"cd {REPO_ROOT} && while true; do scripts/watchdog.sh; sleep 5; done"


def unstick_cmd() -> str:
    return f"while true; do {sys.executable} {REPO_ROOT}/scripts/unstick.py lead 600; sleep 60; done"


def summarize(results: list[Result]) -> int:
    name_w = max(len("NAME"), *(len(r.name) for r in results))
    state_w = max(len("STATE"), *(len(r.state) for r in results))

    lines = [f"{'NAME':<{name_w}} {'STATE':<{state_w}}  DETAIL"]
    for r in results:
        lines.append(f"{r.name:<{name_w}} {r.state:<{state_w}}  {r.detail}")
    print("\n".join(lines))
    print()
    print(f"Dashboard: http://localhost:{CLIENT_PORT}  "
          f"(events API: http://localhost:{SERVER_PORT}/events/recent)")
    print(f"Attach:    tmux attach -t {TMUX_SESSION}")

    return 1 if any(r.state == "FAILED" for r in results) else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="up.py", description=(
        "Idempotently start the orchestra tmux session, observability "
        "dashboard, lead agent, and watchdog/unstick loops. Safe to "
        "re-run: never touches an already-running window."
    ))
    parser.add_argument("--no-dashboard", action="store_true",
                         help=f"skip the observability dashboard (ports {SERVER_PORT}/{CLIENT_PORT})")
    parser.add_argument("--dry-run", action="store_true",
                         help="print what would be started; make no tmux/network/spawn calls")
    return parser


def main(argv: list[str] | None = None, spawn_fn: SpawnFn | None = None) -> int:
    args = _build_parser().parse_args(argv)

    errors = check_prereqs(no_dashboard=args.no_dashboard)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    results: list[Result] = []

    if args.no_dashboard:
        results.append(Result("dashboard", "SKIPPED", "disabled (--no-dashboard)"))
    else:
        results.append(start_dashboard(args.dry_run))

    # Idempotent repeat: start_dashboard already ensures the session when it
    # runs, but --no-dashboard skips that path entirely, so guarantee it here.
    ensure_session(args.dry_run)
    results.append(ensure_lead(args.dry_run, spawn_fn=spawn_fn))
    results.append(ensure_window("watchdog", watchdog_cmd(), args.dry_run))
    results.append(ensure_window("unstick", unstick_cmd(), args.dry_run))

    return summarize(results)


if __name__ == "__main__":
    sys.exit(main())
