#!/usr/bin/env python3
"""Shared inbox+nudge delivery for agent-to-agent messaging.

Delivery contract (the only messaging pattern in the system):
  1. The full message is appended to the target agent's inbox.md — atomic,
     reliable, survives lost nudges.
  2. A constant single-line nudge ("Check your inbox.") is typed into the
     target's tmux pane, Enter sent separately after a pause (the TUI's paste
     detection swallows same-burst Enters).
  3. Draft-collision guard: if a human (or queued text) is sitting in the
     target's prompt line, the keystroke nudge is SKIPPED — the inbox write
     stands, and the watchdog / turn-end inbox check picks it up.
"""
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("orchestra.messaging")

NUDGE_LINE = "Check your inbox."
SUBMIT_DELAY_S = 1

# Seams for tests: faked so no test ever touches tmux or sleeps.
RUN = subprocess.run
SLEEP = time.sleep


def pane_has_draft(pane_target: str) -> bool:
    """True if the pane's prompt line ('❯ ...') holds unsubmitted text."""
    cap = RUN(["tmux", "capture-pane", "-p", "-t", pane_target],
              capture_output=True, text=True)
    if cap.returncode != 0:
        return False
    prompt_lines = [ln for ln in cap.stdout.splitlines() if ln.lstrip().startswith("❯")]
    if not prompt_lines:
        return False
    return bool(prompt_lines[-1].lstrip().lstrip("❯").strip())


def deliver(target: str, message: str, sender: str, agents_dir: Path,
            session: str) -> str:
    """Append message to target's inbox and nudge its pane (unless drafting)."""
    d = agents_dir / target
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with (d / "inbox.md").open("a") as f:
        f.write(f"\n[{ts}] {sender.upper()}: {message}\n")

    pane = f"{session}:{target}"
    if pane_has_draft(pane):
        logger.info("pane %s has a draft; inbox written, nudge skipped", pane)
        return f"Delivered to '{target}' inbox; nudge skipped (draft in pane)."

    RUN(["tmux", "send-keys", "-t", pane, NUDGE_LINE])
    SLEEP(SUBMIT_DELAY_S)
    RUN(["tmux", "send-keys", "-t", pane, "Enter"])
    return f"Delivered to '{target}' and nudged."
