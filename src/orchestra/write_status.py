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
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestra.write_status")

DEFAULT_AGENTS_DIR = Path(os.environ.get("ORCHESTRA_AGENTS_DIR", "/home/tusk/agents"))
TAIL_BYTES = 4000
REASON_MAX = 300
# On Stop, the final assistant message may not be flushed to the transcript
# yet (observed live: transcript and hook ran in the same second, sentinel
# missing). Re-read a few times before concluding "no sentinel".
FLUSH_RETRIES = 3
FLUSH_WAIT_S = 0.5
SLEEP = time.sleep  # seam for tests


def _sentinel_lines(text: str) -> list[tuple[str, str]]:
    """(sentinel, rest-of-line) for every LINE-INITIAL sentinel, in order.

    Only line-initial occurrences count: agents QUOTE the protocol in prose
    ("print 'DONE: <summary>' and stop"), and matching those produced false
    done states. Leading whitespace is tolerated; quotes/bullets are not.
    """
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        for s in SENTINELS:
            if stripped.startswith(s):
                rest = stripped[len(s):].strip()[:REASON_MAX] or "unspecified"
                out.append((s, rest))
                break
    return out


def _assistant_text(raw: str, max_chars: int = TAIL_BYTES,
                    last_only: bool = False) -> str:
    """Concatenate text blocks of assistant messages from a JSONL transcript,
    in order, keeping the last max_chars. last_only=True returns just the
    FINAL assistant message — the only one that reflects the current turn's
    outcome. Returns "" if no assistant text is found (non-JSONL input)."""
    texts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    texts = [t for t in texts if t]
    if last_only:
        return texts[-1][-max_chars:] if texts else ""
    return "\n".join(texts)[-max_chars:]


SENTINELS = ("DONE:", "BLOCKED:", "ASK:")


def _full_assistant_text(event: dict) -> str:
    transcript = event.get("transcript_path")
    if not (transcript and Path(transcript).exists()):
        return ""
    return _assistant_text(Path(transcript).read_text(), max_chars=10**9)


def _maybe_notify(name: str, event: dict, agents_dir: Path,
                  notify_target: str | None, session: str | None) -> None:
    """Nudge the notify target when a NEW sentinel appears (dedupe by count).

    Counts are over the full assistant text so transcript growth can only
    increase them; the last-notified count persists in the agent dir, so the
    same sentinel is never re-nudged on later Stop/PostToolUse events.
    """
    if not (notify_target and session):
        return
    full = _full_assistant_text(event)
    found = _sentinel_lines(full)
    count = len(found)
    state_path = agents_dir / name / "notify_state.json"
    seen = 0
    if state_path.exists():
        try:
            seen = json.loads(state_path.read_text()).get("notified_count", 0)
        except json.JSONDecodeError:
            seen = 0
    if count <= seen:
        return
    # Report the latest sentinel line as the event body.
    line = f"{found[-1][0]} {found[-1][1]}"[:400]
    from orchestra import messaging
    messaging.deliver(target=notify_target, message=line, sender=name,
                      agents_dir=agents_dir, session=session)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"notified_count": count}))


def handle_event(name: str, event: dict, agents_dir: Path,
                 notify_target: str | None = None,
                 session: str | None = None) -> dict:
    """Compute and write status.json for one hook event; returns the status."""
    hook = event.get("hook_event_name", "")

    def read_tail() -> str:
        transcript = event.get("transcript_path")
        if not (transcript and Path(transcript).exists()):
            return ""
        raw = Path(transcript).read_text()
        # Real transcripts are JSONL where one trailing tool-result line can
        # exceed any raw byte tail. Scan ONLY the final assistant message:
        # earlier turns' sentinels are history, not this turn's outcome
        # (a prior DONE: was re-detected after a sentinel-less cleanup turn).
        # Fall back to a raw tail for non-JSONL content.
        return _assistant_text(raw, last_only=True) or raw[-TAIL_BYTES:]

    tail = read_tail()
    if hook == "Stop":
        # Wait for the transcript to stop growing before scanning: a stale
        # sentinel from a prior turn can be present while the fresh one is
        # still flushing, so "a sentinel exists" is not a safe early exit.
        for _ in range(FLUSH_RETRIES):
            SLEEP(FLUSH_WAIT_S)
            again = read_tail()
            if again == tail:
                break
            tail = again

    found = _sentinel_lines(tail)
    latest, rest = found[-1] if found else (None, None)

    state, blocker, needs, summary, question = "working", None, None, None, None
    if latest == "DONE:":
        if hook == "Stop":
            state = "done"
            summary = rest
        # mid-turn DONE: leave state working; Stop will settle it
    elif latest == "BLOCKED:":
        # Blockers surface immediately, even mid-turn.
        state = "blocked"
        blocker = rest
        needs = "orchestrator_input"
    elif latest == "ASK:":
        # Questions keep the worker on-task: state stays working.
        needs = "lead_input"
        question = rest
    elif hook == "Stop":
        state = "idle"
        needs = "awaiting_input"

    status = {
        "agent": name,
        "state": state,
        "last_action": hook,
        "blocker_reason": blocker,
        "needs": needs,
        "summary": summary,
        "question": question,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    d = agents_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps(status, indent=2))
    _maybe_notify(name, event, agents_dir, notify_target, session)
    return status


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        logger.warning("hook stdin was not valid JSON; proceeding with empty event")
        event = {}
    handle_event(
        name, event, DEFAULT_AGENTS_DIR,
        notify_target=os.environ.get("ORCHESTRA_NOTIFY_TARGET"),
        session=os.environ.get("ORCHESTRA_TMUX_SESSION"),
    )
    # Exit 0 = allow the Stop/tool to proceed normally.
    print(json.dumps({"continue": True}))


if __name__ == "__main__":
    main()
