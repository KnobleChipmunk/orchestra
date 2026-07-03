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
