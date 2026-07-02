#!/usr/bin/env python3
"""Stop/PostToolUse hook handler run by worker Claude Code sessions.

Reads the hook event JSON on stdin, scans the transcript tail for the status
protocol sentinels, and writes the agent's status.json on the blackboard.

Sentinel protocol (embedded in every worker's task prompt):
  BLOCKED: <reason>  -> state=blocked, needs orchestrator input
  DONE: <summary>    -> state=done
  Stop with neither  -> state=idle (Stop fires on EVERY turn end, so a bare
                        Stop means "awaiting input", never "done")
The latest sentinel in the transcript wins, so a worker that was blocked and
later unblocked reads as done.

Usage (wired via --settings at spawn): write_status.py <agent_name>
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestra.write_status")

DEFAULT_AGENTS_DIR = Path(os.environ.get("ORCHESTRA_AGENTS_DIR", "/home/tusk/agents"))
TAIL_BYTES = 4000
REASON_MAX = 300


def _sentinel_after(tail: str, sentinel: str) -> str | None:
    """Return the first line following the LAST occurrence of sentinel, or None."""
    if sentinel not in tail:
        return None
    after = tail.rsplit(sentinel, 1)[1].strip()
    return (after.splitlines()[0][:REASON_MAX] if after else "unspecified") or "unspecified"


def handle_event(name: str, event: dict, agents_dir: Path) -> dict:
    """Compute and write status.json for one hook event; returns the status."""
    hook = event.get("hook_event_name", "")

    tail = ""
    transcript = event.get("transcript_path")
    if transcript and Path(transcript).exists():
        tail = Path(transcript).read_text()[-TAIL_BYTES:]

    blocked_pos = tail.rfind("BLOCKED:")
    done_pos = tail.rfind("DONE:")

    state, blocker, needs, summary = "working", None, None, None
    if hook == "Stop":
        if done_pos > blocked_pos:
            state = "done"
            summary = _sentinel_after(tail, "DONE:")
        elif blocked_pos >= 0:
            state = "blocked"
            blocker = _sentinel_after(tail, "BLOCKED:")
            needs = "orchestrator_input"
        else:
            state = "idle"
            needs = "awaiting_input"
    elif blocked_pos > done_pos:
        # Mid-turn hooks can still surface a blocker early.
        state = "blocked"
        blocker = _sentinel_after(tail, "BLOCKED:")
        needs = "orchestrator_input"

    status = {
        "agent": name,
        "state": state,
        "last_action": hook,
        "blocker_reason": blocker,
        "needs": needs,
        "summary": summary,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    d = agents_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(status, indent=2))
    return status


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        logger.warning("hook stdin was not valid JSON; proceeding with empty event")
        event = {}
    handle_event(name, event, DEFAULT_AGENTS_DIR)
    # Exit 0 = allow the Stop/tool to proceed normally.
    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
