#!/usr/bin/env python3
"""Reliable direct reply to an agent's pane — the keyboard that never loses
its Enter.

Usage: say.py <agent> <text...>

Unlike agent_msg.py (inbox + nudge, for messages the agent should read as
mail), say.py types your text straight into the agent's prompt with verified
submission — use it to answer a question the agent just asked in its pane.
"""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestra import messaging  # noqa: E402

logging.basicConfig(level=logging.INFO)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: say.py <agent> <text...>", file=sys.stderr)
        return 2
    session = os.environ.get("ORCHESTRA_TMUX_SESSION", "orchestra")
    print(messaging.type_into(f"{session}:{sys.argv[1]}", " ".join(sys.argv[2:])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
