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
import json
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


def pane_draft_text(pane_target: str) -> str:
    """Unsubmitted text in the pane's input box, or "" if the box is empty.

    Only the LAST '❯' line counts — scrollback renders past submissions with
    the same prefix. The TUI pads the live input box with NBSP, so strip that
    alongside regular whitespace.
    """
    cap = RUN(["tmux", "capture-pane", "-p", "-t", pane_target],
              capture_output=True, text=True)
    if cap.returncode != 0:
        return ""
    prompt_lines = [ln for ln in cap.stdout.splitlines() if ln.lstrip().startswith("❯")]
    if not prompt_lines:
        return ""
    return prompt_lines[-1].lstrip().lstrip("❯").strip().strip("\xa0").strip()


def deliver(target: str, message: str, sender: str, agents_dir: Path,
            session: str) -> str:
    """Append message to target's inbox and nudge its pane (unless drafting)."""
    d = agents_dir / target
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with (d / "inbox.md").open("a") as f:
        f.write(f"\n[{ts}] {sender.upper()}: {message}\n")

    pane = f"{session}:{target}"
    draft = pane_draft_text(pane)
    if draft.startswith(NUDGE_LINE):
        # A previous nudge lost its Enter (typically arrived mid-turn) and is
        # sitting unsubmitted. A bare Enter is IGNORED on a stale input box —
        # the TUI only honors Enter following recent keystrokes — so type a
        # fresh char first. Our message is already in the inbox the stuck
        # nudge points at.
        _force_submit(pane)
        return f"Delivered to '{target}'; resubmitted a stuck nudge."
    if draft:
        logger.info("pane %s has a draft; inbox written, nudge skipped", pane)
        return f"Delivered to '{target}' inbox; nudge skipped (draft in pane)."

    RUN(["tmux", "send-keys", "-t", pane, NUDGE_LINE])
    SLEEP(SUBMIT_DELAY_S)
    RUN(["tmux", "send-keys", "-t", pane, "Enter"])
    # Verify the box drained; a mid-turn arrival can swallow the Enter.
    SLEEP(SUBMIT_DELAY_S)
    if pane_draft_text(pane).startswith(NUDGE_LINE):
        _force_submit(pane)
        return f"Delivered to '{target}'; nudge needed a submit retry."
    return f"Delivered to '{target}' and nudged."


def type_into(pane: str, text: str) -> str:
    """Type text directly into a pane's prompt with verified submission.

    For human/operator replies (scripts/say.py). Refuses if a different draft
    already occupies the input box; retries via _force_submit if the TUI
    parks the text (bare-Enter-after-pause is ignored on a stale box).
    """
    existing = pane_draft_text(pane)
    if existing and existing != text:
        return f"Refusing: a different draft is parked in {pane}: {existing!r}"
    RUN(["tmux", "send-keys", "-t", pane, text])
    SLEEP(SUBMIT_DELAY_S)
    RUN(["tmux", "send-keys", "-t", pane, "Enter"])
    SLEEP(SUBMIT_DELAY_S)
    if pane_draft_text(pane):
        _force_submit(pane)
        return f"Typed into {pane} (needed a submit retry)."
    return f"Typed into {pane}."


def unstick_if_stale(pane: str, state_path: Path, max_age_s: float,
                     now: float | None = None) -> str:
    """Force-submit a pane draft that has sat unchanged past max_age_s.

    Human Enters are silently swallowed by the TUI (observed repeatedly);
    a parked draft blocks the nudge channel indefinitely — one blocked the
    lead for three hours overnight. Called periodically by the watchdog.
    Tracks the last-seen draft in state_path; a changed draft restarts the
    clock so nobody's in-progress typing is ever submitted.
    """
    if now is None:
        now = time.time()
    draft = pane_draft_text(pane)
    if not draft:
        if state_path.exists():
            state_path.unlink()
        return "empty box; nothing to do."
    prev = {}
    if state_path.exists():
        try:
            prev = json.loads(state_path.read_text())
        except json.JSONDecodeError:
            prev = {}
    if prev.get("draft") == draft:
        if now - prev.get("first_seen", now) >= max_age_s:
            _force_submit(pane)
            state_path.unlink(missing_ok=True)
            return f"submitted stale draft ({int(max_age_s)}s+ unchanged): {draft[:60]!r}"
        return "draft unchanged; waiting out the age threshold."
    state_path.write_text(json.dumps({"draft": draft, "first_seen": now}))
    return "new draft recorded; clock started."


def _force_submit(pane: str) -> None:
    """Submit whatever sits in the input box: fresh keystroke, then Enter."""
    RUN(["tmux", "send-keys", "-t", pane, " "])
    SLEEP(SUBMIT_DELAY_S)
    RUN(["tmux", "send-keys", "-t", pane, "Enter"])
