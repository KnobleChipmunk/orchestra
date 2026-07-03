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


def test_stop_retries_read_when_final_message_not_yet_flushed(tmp_path: Path, monkeypatch) -> None:
    # Race seen live: at Stop-hook time the transcript existed but the final
    # assistant message (with the sentinel) had not been flushed yet. The
    # handler must re-read briefly before concluding "idle".
    from orchestra import write_status

    p = tmp_path / "transcript.jsonl"
    p.write_text(json.dumps({"type": "assistant",
                             "message": {"content": [{"type": "text", "text": "working..."}]}}) + "\n")

    flushed = []

    def late_flush(_seconds: float) -> None:
        if not flushed:
            flushed.append(True)
            p.write_text(p.read_text() + json.dumps(
                {"type": "assistant",
                 "message": {"content": [{"type": "text", "text": "BLOCKED: file missing"}]}}) + "\n")

    monkeypatch.setattr(write_status, "SLEEP", late_flush)
    handle_event("beta", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "beta" / "status.json").read_text())
    assert status["state"] == "blocked"
    assert status["blocker_reason"] == "file missing"


def test_stop_waits_out_stale_sentinel_when_fresh_one_still_flushing(
    tmp_path: Path, monkeypatch
) -> None:
    # Run-3 regression: transcript already contained an old BLOCKED: from a
    # prior turn; the fresh DONE: had not flushed yet. A presence-based retry
    # exits on the stale sentinel — the handler must instead wait for the
    # transcript to stop growing before scanning.
    from orchestra import write_status

    def msg(text: str) -> str:
        return json.dumps({"type": "assistant",
                           "message": {"content": [{"type": "text", "text": text}]}})

    p = tmp_path / "transcript.jsonl"
    p.write_text(msg("BLOCKED: openapi.yaml missing") + "\n")
    flushed = []

    def late_flush(_seconds: float) -> None:
        if not flushed:
            flushed.append(True)
            p.write_text(p.read_text() + msg("DONE: spec created") + "\n")

    monkeypatch.setattr(write_status, "SLEEP", late_flush)
    handle_event("beta", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "beta" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "spec created"


# --- v2: sentinel-transition notify + ASK sentinel ---------------------------

def _jmsg(text: str) -> str:
    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": text}]}})


def _notify_setup(monkeypatch, tmp_path: Path):
    """Fake messaging.RUN/SLEEP; route notifications to agent 'lead'."""
    from orchestra import messaging, write_status
    calls: list[list[str]] = []

    class R:
        returncode = 0
        stdout = "❯ \n"
        stderr = ""

    monkeypatch.setattr(messaging, "RUN", lambda cmd, **kw: (calls.append(list(cmd)), R())[1])
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    monkeypatch.setattr(write_status, "SLEEP", lambda s: None)
    return calls


def test_new_sentinel_notifies_lead(tmp_path: Path, monkeypatch) -> None:
    calls = _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(_jmsg("DONE: parser built") + "\n")
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="lead", session="orchestra")
    inbox = (tmp_path / "lead" / "inbox.md").read_text()
    assert "w1" in inbox.lower() and "DONE: parser built" in inbox
    assert any(c[:2] == ["tmux", "send-keys"] for c in calls)


def test_same_sentinel_not_renotified(tmp_path: Path, monkeypatch) -> None:
    calls = _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(_jmsg("DONE: parser built") + "\n")
    ev = {"hook_event_name": "Stop", "transcript_path": str(p)}
    handle_event("w1", ev, tmp_path, notify_target="lead", session="orchestra")
    n_first = len([c for c in calls if c[:2] == ["tmux", "send-keys"]])
    handle_event("w1", ev, tmp_path, notify_target="lead", session="orchestra")
    n_second = len([c for c in calls if c[:2] == ["tmux", "send-keys"]])
    assert n_first > 0
    assert n_second == n_first  # dedupe: no new keystrokes for an old sentinel


def test_second_distinct_sentinel_notifies_again(tmp_path: Path, monkeypatch) -> None:
    calls = _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(_jmsg("BLOCKED: need spec") + "\n")
    ev = {"hook_event_name": "Stop", "transcript_path": str(p)}
    handle_event("w1", ev, tmp_path, notify_target="lead", session="orchestra")
    n1 = len([c for c in calls if c[:2] == ["tmux", "send-keys"]])
    p.write_text(p.read_text() + _jmsg("DONE: resolved") + "\n")
    handle_event("w1", ev, tmp_path, notify_target="lead", session="orchestra")
    n2 = len([c for c in calls if c[:2] == ["tmux", "send-keys"]])
    assert n2 > n1


def test_no_notify_without_target(tmp_path: Path, monkeypatch) -> None:
    calls = _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(_jmsg("DONE: x") + "\n")
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    assert not calls


def test_ask_sentinel_keeps_working_and_records_question(
    tmp_path: Path, monkeypatch
) -> None:
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(_jmsg("ASK: should the endpoint return 404 or empty list?") + "\n")
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="lead", session="orchestra")
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "working"
    assert status["needs"] == "lead_input"
    assert "404" in status["question"]
    assert "404" in (tmp_path / "lead" / "inbox.md").read_text()


def test_ask_then_done_resolves_to_done(tmp_path: Path, monkeypatch) -> None:
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(_jmsg("ASK: which db?") + "\n" + _jmsg("DONE: used dev db") + "\n")
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="lead", session="orchestra")
    assert json.loads((tmp_path / "w1" / "status.json").read_text())["state"] == "done"
