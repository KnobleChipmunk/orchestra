"""Tests for the Stop/PostToolUse hook handler that maintains status.json.

The handler reads the hook event JSON on stdin (simulated here by calling
handle_event directly), scans the transcript tail for BLOCKED:/DONE: sentinels,
and writes the agent's status.json on the blackboard.
"""
import json
from pathlib import Path

from orchestra.write_status import handle_event


def _read_status(agents_dir: Path, name: str) -> dict:
    return json.loads((agents_dir / name / "status.json").read_text())


def _transcript(tmp_path: Path, text: str) -> str:
    p = tmp_path / "transcript.jsonl"
    p.write_text(text)
    return str(p)


def test_blocked_sentinel_sets_blocked_state(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, "some output\nBLOCKED: docs/openapi.yaml not found\n")
    handle_event("beta", {"hook_event_name": "Stop", "transcript_path": transcript}, tmp_path)
    status = _read_status(tmp_path, "beta")
    assert status["state"] == "blocked"
    assert status["blocker_reason"] == "docs/openapi.yaml not found"
    assert status["needs"] == "orchestrator_input"


def test_done_sentinel_sets_done_state(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, "all tests pass\nDONE: /health endpoint added\n")
    handle_event("alpha", {"hook_event_name": "Stop", "transcript_path": transcript}, tmp_path)
    status = _read_status(tmp_path, "alpha")
    assert status["state"] == "done"
    assert status["summary"] == "/health endpoint added"


def test_stop_without_sentinel_is_idle_not_done(tmp_path: Path) -> None:
    # Stop fires on EVERY turn end; without a sentinel the agent is awaiting input.
    transcript = _transcript(tmp_path, "I have a question about the schema.\n")
    handle_event("alpha", {"hook_event_name": "Stop", "transcript_path": transcript}, tmp_path)
    status = _read_status(tmp_path, "alpha")
    assert status["state"] == "idle"
    assert status["needs"] == "awaiting_input"


def test_post_tool_use_is_working(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, "editing files\n")
    handle_event(
        "alpha", {"hook_event_name": "PostToolUse", "transcript_path": transcript}, tmp_path
    )
    assert _read_status(tmp_path, "alpha")["state"] == "working"


def test_latest_sentinel_wins(tmp_path: Path) -> None:
    # Agent was blocked earlier, then unblocked and finished: DONE appears later.
    transcript = _transcript(tmp_path, "BLOCKED: no spec file\n...resolved...\nDONE: spec written\n")
    handle_event("beta", {"hook_event_name": "Stop", "transcript_path": transcript}, tmp_path)
    assert _read_status(tmp_path, "beta")["state"] == "done"


def test_missing_transcript_still_writes_status(tmp_path: Path) -> None:
    handle_event(
        "gamma",
        {"hook_event_name": "Stop", "transcript_path": str(tmp_path / "nope.jsonl")},
        tmp_path,
    )
    status = _read_status(tmp_path, "gamma")
    assert status["state"] == "idle"
    assert status["agent"] == "gamma"


def test_status_has_timestamp_and_last_action(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, "working\n")
    handle_event(
        "alpha", {"hook_event_name": "PostToolUse", "transcript_path": transcript}, tmp_path
    )
    status = _read_status(tmp_path, "alpha")
    assert status["last_action"] == "PostToolUse"
    assert "updated" in status and status["updated"]


def test_blocker_reason_truncated(tmp_path: Path) -> None:
    transcript = _transcript(tmp_path, "BLOCKED: " + "x" * 1000 + "\n")
    handle_event("beta", {"hook_event_name": "Stop", "transcript_path": transcript}, tmp_path)
    assert len(_read_status(tmp_path, "beta")["blocker_reason"]) <= 300


def test_sentinel_found_in_jsonl_transcript_despite_long_trailing_lines(tmp_path: Path) -> None:
    # Real transcripts are JSONL; a single trailing tool-result line can exceed
    # the old 4000-char raw tail, hiding the sentinel. The handler must scan
    # assistant message text, not raw bytes.
    lines = [
        json.dumps({"type": "assistant",
                    "message": {"content": [{"type": "text",
                                             "text": "DONE: openapi.yaml created"}]}}),
        json.dumps({"type": "user",
                    "message": {"content": [{"type": "tool_result",
                                             "content": "x" * 8000}]}}),
    ]
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(lines) + "\n")
    handle_event("beta", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "beta" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "openapi.yaml created"


def test_jsonl_latest_assistant_sentinel_wins(tmp_path: Path) -> None:
    def msg(text: str) -> str:
        return json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "text", "text": text}]}})
    p = tmp_path / "transcript.jsonl"
    p.write_text(msg("BLOCKED: missing file") + "\n" + msg("DONE: resolved and finished") + "\n")
    handle_event("beta", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    assert json.loads((tmp_path / "beta" / "status.json").read_text())["state"] == "done"
