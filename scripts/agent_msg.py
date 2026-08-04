#!/usr/bin/env python3
"""Peer messaging CLI — any agent (or human) sends a message to another agent.

Usage: agent_msg.py <target-agent> <message...>

Appends the full message to the target's inbox.md and nudges its tmux pane
(unless a draft is sitting in the prompt). Sender is taken from
ORCHESTRA_AGENT_NAME if set, else "peer".
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
        print("usage: agent_msg.py <target-agent> <message...>", file=sys.stderr)
        return 2
    target = sys.argv[1]
    message = " ".join(sys.argv[2:])
    result = messaging.deliver(
        target=target,
        message=message,
        sender=os.environ.get("ORCHESTRA_AGENT_NAME", "peer"),
        agents_dir=Path(os.environ.get("ORCHESTRA_AGENTS_DIR", "/home/user/agents")),
        session=os.environ.get("ORCHESTRA_TMUX_SESSION", "orchestra"),
    )
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
