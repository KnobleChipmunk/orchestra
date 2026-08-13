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


def _juser(text: str) -> str:
    # Real user turn: message.content is a STRING, with origin={"kind":
    # "human"} and promptSource="typed" — the shape of the ~2500 real
    # human-typed entries found cross-tabulating ~/.claude/projects/*/*.jsonl.
    # A prior version of this fixture omitted origin/promptSource, which
    # masked a bug where origin was (wrongly) treated as a synthetic-entry
    # marker — every real user turn was excluded and the tests still passed.
    return json.dumps({"type": "user", "origin": {"kind": "human"}, "promptSource": "typed",
                       "message": {"role": "user", "content": text}})


def _jtool_result(content: str) -> str:
    # Tool-result entries also arrive as type="user" but with content as a
    # LIST of blocks carrying type="tool_result" — not a real user turn.
    # Shape confirmed against a live transcript.
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"tool_use_id": "toolu_1", "type": "tool_result", "content": content}]}})


def _jassistant_tool(name: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}]}})


def _jmeta(text: str) -> str:
    # Synthetic mid-turn injection (local-command-caveat / system-reminder):
    # type="user", string content, isMeta=True. Shape confirmed against a
    # live transcript (~/.claude/projects/*/*.jsonl) — not a real user turn.
    return json.dumps({"type": "user", "isMeta": True,
                       "message": {"role": "user", "content": text}})


def _jsystem_prompt(text: str) -> str:
    # Synthetic mid-turn injection (task-notification / cross-session
    # message): type="user", string content, promptSource="system", an
    # "origin" block, isMeta NOT necessarily set. Shape confirmed against a
    # live transcript — not a real user turn.
    return json.dumps({"type": "user", "promptSource": "system",
                       "origin": {"kind": "task-notification"},
                       "message": {"role": "user", "content": text}})


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


def test_quoted_sentinel_in_prose_is_not_detected(tmp_path: Path, monkeypatch) -> None:
    # Phase C gate finding: the LEAD quoted the protocol ("print 'DONE:
    # <summary>' and stop") while planning, and its Stop hook read that as a
    # real DONE. Only line-initial sentinels count.
    calls = _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(_jmsg(
        "I will spawn two workers. Each prompt ends with: when complete, "
        "print 'DONE: <summary>' and stop; if stuck print 'BLOCKED: <reason>'."
    ) + "\n")
    handle_event("lead", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="boss", session="orchestra")
    status = json.loads((tmp_path / "lead" / "status.json").read_text())
    assert status["state"] == "idle"
    assert not calls  # and no notify fired for the quoted text


def test_line_initial_sentinel_still_detected(tmp_path: Path, monkeypatch) -> None:
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(_jmsg("Both verified.\nDONE: glossary and banner complete") + "\n")
    handle_event("lead", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "lead" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "glossary and banner complete"


def test_stop_after_a_done_turn_without_new_sentinel_is_idle(
    tmp_path: Path, monkeypatch
) -> None:
    # Live finding (0508510): the lead's post-DONE cleanup turn ended and the
    # hook re-detected the PREVIOUS turn's DONE from transcript history,
    # marking a busy lead done with a stale summary. A real user entry (the
    # nudge/keystroke that woke the new turn) separates the DONE turn from
    # the sentinel-less cleanup turn that follows it; detection must scope to
    # assistant text after that boundary, not to the whole transcript.
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("Continue with Phase D.") + "\n"
        + _jmsg("DONE: Phase D complete, PRs merged") + "\n"
        + _juser("continue") + "\n"
        + _jmsg("Workers stopped, worktrees pruned. Spawning d4 now.") + "\n"
    )
    handle_event("lead", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "lead" / "status.json").read_text())
    assert status["state"] == "idle"


def test_sentinel_followed_by_trailing_wrapup_in_same_turn_is_done(
    tmp_path: Path, monkeypatch
) -> None:
    # A worker's turn: real user prompt, then DONE:, then a further assistant
    # text block in the SAME turn (no new user entry in between) — e.g. a
    # wrap-up line after the sentinel. The sentinel must still be detected;
    # this is the swallowed-sentinel bug from HANDOFF.md.
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("Please finish the parser task.") + "\n"
        + _jmsg("DONE: parser built") + "\n"
        + _jmsg("Let me know if you need anything else.") + "\n"
    )
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="lead", session="orchestra")
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "parser built"


def test_sentinel_in_earlier_block_of_text_tool_text_turn_is_done(
    tmp_path: Path, monkeypatch
) -> None:
    # text -> tool_use -> text within one turn, sentinel in the FIRST text
    # block. The swallowed-sentinel bug: scanning only the final text block
    # misses this entirely.
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("Wrap up and verify.") + "\n"
        + _jmsg("DONE: openapi.yaml created") + "\n"
        + _jassistant_tool("Bash") + "\n"
        + _jtool_result("gate output: all green") + "\n"
        + _jmsg("Ran the gates once more to confirm.") + "\n"
    )
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="lead", session="orchestra")
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "openapi.yaml created"


def test_meta_injection_does_not_reset_turn_boundary(tmp_path: Path, monkeypatch) -> None:
    # Claude Code injects synthetic isMeta=True user-role entries mid-turn
    # (local-command-caveat, system-reminder). These must not count as a
    # fresh turn boundary or an earlier same-turn sentinel is lost.
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("Please finish the parser task.") + "\n"
        + _jmsg("DONE: parser built") + "\n"
        + _jmeta("<system-reminder>Some housekeeping note.</system-reminder>") + "\n"
        + _jmsg("Let me know if you need anything else.") + "\n"
    )
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="lead", session="orchestra")
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "parser built"


def test_task_notification_injection_does_not_reset_turn_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    # Background task-notification / cross-session-message entries arrive as
    # type="user" with promptSource="system" and no isMeta flag. These must
    # not count as a fresh turn boundary either.
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("Please finish the parser task.") + "\n"
        + _jmsg("DONE: parser built") + "\n"
        + _jsystem_prompt("<task-notification>background review finished</task-notification>")
        + "\n"
        + _jmsg("Noted the background review too.") + "\n"
    )
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="lead", session="orchestra")
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "parser built"


def test_long_trailing_text_does_not_push_sentinel_out_of_scan_window(
    tmp_path: Path, monkeypatch
) -> None:
    # A sentinel followed by a long same-turn wrap-up block must not be
    # truncated away by the scan-window cap.
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("Wrap up.") + "\n"
        + _jmsg("DONE: parser built") + "\n"
        + _jmsg("x" * 5000) + "\n"
    )
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path,
                 notify_target="lead", session="orchestra")
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "parser built"


def test_no_user_entries_at_all_falls_back_to_last_block_only(
    tmp_path: Path, monkeypatch
) -> None:
    # Degenerate transcript slice with no real user entry anywhere at all
    # (e.g. a rotated/partial file). With no turn signal to scope by,
    # rejoining every assistant block would resurrect the exact stale-DONE
    # bug 0508510 fixed. Fall back to the single most-recent assistant text
    # block, matching the pre-turn-scoping protection.
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(
        _jmsg("DONE: Phase D complete, PRs merged") + "\n"
        + _jmsg("Workers stopped, worktrees pruned. Spawning d4 now.") + "\n"
    )
    handle_event("lead", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "lead" / "status.json").read_text())
    assert status["state"] == "idle"


def test_real_human_turn_still_resets_boundary_despite_origin_field(
    tmp_path: Path, monkeypatch
) -> None:
    # Review finding: real human-typed turns carry origin={"kind": "human"}
    # in production transcripts (confirmed across ~2500 real entries). An
    # earlier version of the boundary check treated ANY origin field as a
    # synthetic-entry marker, which excluded every real user turn and made
    # the whole turn-scoping feature silently never engage. A tool-only
    # sentinel-less turn after a real DONE turn must read as idle, not a
    # stale re-detected DONE.
    _notify_setup(monkeypatch, tmp_path)
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("continue phase 1") + "\n"
        + _jmsg("DONE: phase 1 complete") + "\n"
        + _juser("continue") + "\n"
        + _jassistant_tool("Bash") + "\n"
        + _jtool_result("ok") + "\n"
    )
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "idle"


def test_null_message_entry_does_not_crash_the_hook(tmp_path: Path) -> None:
    # A malformed/partially-flushed JSONL line can parse as valid JSON with
    # an explicit "message": null (dict.get(key, default) does not
    # substitute the default when the key is present with value None).
    # The hook must survive this, not crash before writing status.json.
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("continue phase 1") + "\n"
        + _jmsg("DONE: phase 1 complete") + "\n"
        + json.dumps({"type": "user", "message": None}) + "\n"
    )
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "done"
    assert status["summary"] == "phase 1 complete"


def test_text_less_final_turn_after_boundary_does_not_fall_back_to_raw_bytes(
    tmp_path: Path,
) -> None:
    # A real turn boundary is found, but the final turn is text-less (ends
    # in a bare tool call). This must resolve to idle from the scoped ""
    # result directly — NOT fall back to an unscoped raw byte tail, which
    # could resurface a stale sentinel from an earlier turn.
    p = tmp_path / "t.jsonl"
    p.write_text(
        _juser("continue phase 1") + "\n"
        + _jmsg("DONE: phase 1 complete") + "\n"
        + _juser("continue") + "\n"
        + _jassistant_tool("Bash") + "\n"
        + _jtool_result("ok") + "\n"
    )
    handle_event("w1", {"hook_event_name": "Stop", "transcript_path": str(p)}, tmp_path)
    status = json.loads((tmp_path / "w1" / "status.json").read_text())
    assert status["state"] == "idle"
