#!/usr/bin/env python3
"""Idempotent one-shot bring-up for the whole orchestra system: tmux session,
observability dashboard, the lead agent, and the watchdog/unstick loops.

Usage: up.py [--no-dashboard] [--dry-run]

Re-execs itself under <repo>/.venv/bin/python if it isn't already running
there, so `python3 scripts/up.py` (or a bare `./scripts/up.py`) works from any
shell without the caller having to remember the venv path.
"""
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _reexec_into_venv() -> None:
    """No-op if already running under the project venv, or if that venv
    doesn't exist yet (check_prereqs reports that clearly with bootstrap
    instructions instead of silently failing here)."""
    if not VENV_PYTHON.exists():
        return
    if Path(sys.executable).resolve() == VENV_PYTHON.resolve():
        return
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve())] + sys.argv[1:])


_reexec_into_venv()

sys.path.insert(0, str(REPO_ROOT / "src"))

from orchestra import launcher  # noqa: E402

logging.basicConfig(level=logging.INFO)


def main() -> int:
    return launcher.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
