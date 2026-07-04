"""Tests for the shared inbox+nudge delivery helper (messaging.py).

RUN is faked — no test touches tmux. Inbox files are real files in tmp dirs.
"""
from pathlib import Path

import pytest

from orchestra import messaging


class FakeRun:
    """Captures tmux calls; returns canned stdout for capture-pane."""

    def __init__(self, pane_text: str = "❯ ") -> None:
        self.calls: list[list[str]] = []
        self.pane_text = pane_text

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))

        class R:
            returncode = 0
            stderr = ""
            stdout = self.pane_text if cmd[:2] == ["tmux", "capture-pane"] else ""

        return R()


@pytest.fixture()
def fake(monkeypatch, tmp_path: Path) -> FakeRun:
    f = FakeRun()
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    return f


def _deliver(fake_run: FakeRun, tmp_path: Path, **kw) -> str:
    defaults = dict(target="lead", message="alpha is DONE", sender="alpha",
                    agents_dir=tmp_path, session="orchestra")
    defaults.update(kw)
    return messaging.deliver(**defaults)


def test_deliver_appends_inbox_with_sender(fake: FakeRun, tmp_path: Path) -> None:
    _deliver(fake, tmp_path, message="build finished")
    inbox = (tmp_path / "lead" / "inbox.md").read_text()
    assert "ALPHA:" in inbox.upper()
    assert "build finished" in inbox


def test_deliver_nudges_with_constant_line_and_separate_enter(
    fake: FakeRun, tmp_path: Path
) -> None:
    _deliver(fake, tmp_path)
    sends = [c for c in fake.calls if c[:2] == ["tmux", "send-keys"]]
    assert len(sends) == 2
    assert sends[0][-1] == "Check your inbox."
    assert "-t" in sends[0] and "orchestra:lead" in sends[0]
    assert sends[1][-1] == "Enter"


def test_deliver_skips_keystrokes_when_pane_has_human_draft(
    monkeypatch, tmp_path: Path
) -> None:
    f = FakeRun(pane_text="some output\n❯ half-typed human sentence\n─────\n")
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    result = _deliver(f, tmp_path)
    assert not any(c[:2] == ["tmux", "send-keys"] for c in f.calls)
    assert "half-typed" not in (tmp_path / "lead" / "inbox.md").read_text()
    assert (tmp_path / "lead" / "inbox.md").exists()  # inbox still written
    assert "draft" in result.lower()


def test_pane_with_empty_prompt_is_not_a_draft(fake: FakeRun, tmp_path: Path) -> None:
    fake.pane_text = "old output\n❯ \n─────\n"
    _deliver(fake, tmp_path)
    assert any(c[:2] == ["tmux", "send-keys"] for c in fake.calls)


def test_deliver_multiline_message_lands_whole_in_inbox(
    fake: FakeRun, tmp_path: Path
) -> None:
    _deliver(fake, tmp_path, message="line1\nline2")
    assert "line1\nline2" in (tmp_path / "lead" / "inbox.md").read_text()
    sends = [c for c in fake.calls if c[:2] == ["tmux", "send-keys"]]
    assert all("\n" not in c[-1] for c in sends)  # keystrokes stay single-line


# --- draft guard must inspect the input box, not scrollback ------------------

TUI = """\
● ACK 1: W1: DONE: pilot worker finished
❯ Check your inbox.
● Working on it
{rule}
❯\xa0{prompt}
{rule}
  Opus 4.8 | 50k | thinking: Off
"""
RULE = "─" * 60


def _tui(prompt: str) -> str:
    return TUI.format(rule=RULE, prompt=prompt)


def test_submitted_history_lines_are_not_drafts(monkeypatch, tmp_path: Path) -> None:
    # Scrollback shows past submissions with the same ❯ prefix as the live
    # prompt; only the input box (between the bottom rules) counts.
    f = FakeRun(pane_text=_tui(""))
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    _deliver(f, tmp_path)
    assert any(c[:2] == ["tmux", "send-keys"] for c in f.calls)


def test_real_draft_in_input_box_is_detected(monkeypatch, tmp_path: Path) -> None:
    f = FakeRun(pane_text=_tui("half-typed human thought"))
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    _deliver(f, tmp_path)
    assert not any(c[:2] == ["tmux", "send-keys"] for c in f.calls)


def test_stuck_nudge_in_input_box_is_resubmitted_not_skipped(
    monkeypatch, tmp_path: Path
) -> None:
    # A nudge whose Enter was lost mid-turn sits in the input box. Treating it
    # as a human draft would deadlock all future nudges — deliver must instead
    # self-heal by sending Enter alone.
    f = FakeRun(pane_text=_tui(messaging.NUDGE_LINE))
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    result = _deliver(f, tmp_path)
    sends = [c for c in f.calls if c[:2] == ["tmux", "send-keys"]]
    # Bare Enter is ignored on a stale box; a fresh keystroke must precede it.
    assert sends[0][-1] == " "
    assert sends[-1][-1] == "Enter"
    assert "stuck" in result.lower() or "resubmit" in result.lower()


class SequenceRun(FakeRun):
    """capture-pane returns successive canned texts across calls."""

    def __init__(self, pane_texts: list[str]) -> None:
        super().__init__()
        self.pane_texts = list(pane_texts)

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))

        text = ""
        if cmd[:2] == ["tmux", "capture-pane"]:
            text = self.pane_texts.pop(0) if len(self.pane_texts) > 1 else self.pane_texts[0]

        class R:
            returncode = 0
            stderr = ""
            stdout = text

        return R()


def test_deliver_verifies_submission_and_retries_once(
    monkeypatch, tmp_path: Path
) -> None:
    # Box empty before send; after Enter the nudge is STILL sitting there
    # (mid-turn swallow) -> deliver must retype-and-Enter once more.
    f = SequenceRun([_tui(""), _tui(messaging.NUDGE_LINE), _tui("")])
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    _deliver(f, tmp_path)
    sends = [c[-1] for c in f.calls if c[:2] == ["tmux", "send-keys"]]
    assert sends[:2] == [messaging.NUDGE_LINE, "Enter"]
    assert " " in sends[2:] and sends[-1] == "Enter"  # retry: fresh char + Enter


def test_deliver_no_retry_when_submission_verified(
    monkeypatch, tmp_path: Path
) -> None:
    f = SequenceRun([_tui(""), _tui("")])
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    _deliver(f, tmp_path)
    sends = [c[-1] for c in f.calls if c[:2] == ["tmux", "send-keys"]]
    assert sends == [messaging.NUDGE_LINE, "Enter"]


# --- type_into: verified direct typing (for human replies to a pane) ---------

def test_type_into_sends_text_then_enter_and_verifies(
    monkeypatch, tmp_path: Path
) -> None:
    f = SequenceRun([_tui(""), _tui("")])
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    messaging.type_into("orchestra:lead", "Approve and continue")
    sends = [c[-1] for c in f.calls if c[:2] == ["tmux", "send-keys"]]
    assert sends == ["Approve and continue", "Enter"]


def test_type_into_retries_when_text_still_parked(monkeypatch, tmp_path: Path) -> None:
    f = SequenceRun([_tui(""), _tui("Approve and continue"), _tui("")])
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    messaging.type_into("orchestra:lead", "Approve and continue")
    sends = [c[-1] for c in f.calls if c[:2] == ["tmux", "send-keys"]]
    assert sends[-2:] == [" ", "Enter"]  # force-submit retry


def test_type_into_refuses_over_existing_draft(monkeypatch, tmp_path: Path) -> None:
    f = SequenceRun([_tui("someone elses draft")])
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    result = messaging.type_into("orchestra:lead", "new text")
    assert "draft" in result.lower()
    assert not any(c[:2] == ["tmux", "send-keys"] for c in f.calls)


# --- stale-draft auto-submit (unstick) ---------------------------------------

def test_unstick_submits_draft_unchanged_beyond_threshold(
    monkeypatch, tmp_path: Path
) -> None:
    f = SequenceRun([_tui("finished reply that lost its Enter"), _tui("")])
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    state = tmp_path / "unstick_state.json"
    # First observation: records draft, does not submit
    r1 = messaging.unstick_if_stale("orchestra:lead", state, max_age_s=600, now=1000.0)
    assert "recorded" in r1.lower()
    assert not any(c[:2] == ["tmux", "send-keys"] for c in f.calls)
    # Same draft 11 minutes later: submit
    f.pane_texts = [_tui("finished reply that lost its Enter"), _tui("")]
    r2 = messaging.unstick_if_stale("orchestra:lead", state, max_age_s=600, now=1660.0)
    assert "submitted" in r2.lower()
    sends = [c[-1] for c in f.calls if c[:2] == ["tmux", "send-keys"]]
    assert sends == [" ", "Enter"]


def test_unstick_resets_when_draft_changes(monkeypatch, tmp_path: Path) -> None:
    f = SequenceRun([_tui("first draft")])
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    state = tmp_path / "s.json"
    messaging.unstick_if_stale("orchestra:lead", state, max_age_s=600, now=0.0)
    f.pane_texts = [_tui("second draft - still typing")]
    r = messaging.unstick_if_stale("orchestra:lead", state, max_age_s=600, now=1200.0)
    assert "recorded" in r.lower()  # changed draft restarts the clock
    assert not any(c[:2] == ["tmux", "send-keys"] for c in f.calls)


def test_unstick_noop_on_empty_box(monkeypatch, tmp_path: Path) -> None:
    f = SequenceRun([_tui("")])
    monkeypatch.setattr(messaging, "RUN", f)
    monkeypatch.setattr(messaging, "SLEEP", lambda s: None)
    r = messaging.unstick_if_stale("orchestra:lead", tmp_path / "s.json", max_age_s=600, now=0.0)
    assert "empty" in r.lower()
    assert not any(c[:2] == ["tmux", "send-keys"] for c in f.calls)
