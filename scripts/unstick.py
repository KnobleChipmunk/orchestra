#!/usr/bin/env python3
"""Force-submit stale pane drafts — run periodically (watchdog companion).

Usage: unstick.py <agent> [max_age_seconds=600]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestra import config, messaging  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: unstick.py <agent> [max_age_seconds]", file=sys.stderr)
        return 2
    agent = sys.argv[1]
    max_age = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
    session = config.tmux_session()
    agents_dir = config.agents_dir()
    state = agents_dir / agent / "unstick_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    result = messaging.unstick_if_stale(f"{session}:{agent}", state, max_age_s=max_age)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
