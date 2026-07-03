"""Tests for inbox hygiene: processed markers + rotation (inbox.py).

Uses real tmp inbox files under tmp_path; the module only touches the
filesystem so no mocking is needed.
"""
from pathlib import Path

from orchestra import inbox


def _msg(sender: str, text: str, ts: str) -> str:
    return f"\n[{ts}] {sender}: {text}\n"


def _make_inbox(tmp_path: Path, name: str = "d3") -> Path:
    d = tmp_path / name
    d.mkdir()
    p = d / "inbox.md"
    p.write_text(f"# Inbox for {name}\n")
    return p


def test_read_new_returns_all_messages_when_nothing_processed(tmp_path: Path) -> None:
    p = _make_inbox(tmp_path)
    with p.open("a") as f:
        f.write(_msg("LEAD", "first message", "2026-07-03T10:00:00+00:00"))
        f.write(_msg("LEAD", "second message", "2026-07-03T10:01:00+00:00"))

    new = inbox.read_new(p)

    assert len(new) == 2
    assert "first message" in new[0]
    assert "second message" in new[1]


def test_read_new_excludes_header(tmp_path: Path) -> None:
    p = _make_inbox(tmp_path)
    with p.open("a") as f:
        f.write(_msg("LEAD", "hello", "2026-07-03T10:00:00+00:00"))

    new = inbox.read_new(p)

    assert not any("Inbox for" in m for m in new)


def test_mark_processed_then_read_new_returns_nothing(tmp_path: Path) -> None:
    p = _make_inbox(tmp_path)
    with p.open("a") as f:
        f.write(_msg("LEAD", "seen already", "2026-07-03T10:00:00+00:00"))

    inbox.mark_processed(p)

    assert inbox.read_new(p) == []


def test_message_appended_after_mark_processed_is_returned(tmp_path: Path) -> None:
    p = _make_inbox(tmp_path)
    with p.open("a") as f:
        f.write(_msg("LEAD", "old news", "2026-07-03T10:00:00+00:00"))

    inbox.mark_processed(p)

    with p.open("a") as f:
        f.write(_msg("LEAD", "fresh update", "2026-07-03T11:00:00+00:00"))

    new = inbox.read_new(p)

    assert len(new) == 1
    assert "fresh update" in new[0]
    assert "old news" not in new[0]


def test_read_new_missing_file_returns_empty(tmp_path: Path) -> None:
    assert inbox.read_new(tmp_path / "does-not-exist" / "inbox.md") == []


def test_rotate_moves_processed_into_archive_and_keeps_header_and_tail(
    tmp_path: Path,
) -> None:
    p = _make_inbox(tmp_path)
    with p.open("a") as f:
        f.write(_msg("LEAD", "old 1", "2026-07-03T10:00:00+00:00"))
        f.write(_msg("LEAD", "old 2", "2026-07-03T10:01:00+00:00"))

    inbox.mark_processed(p)

    with p.open("a") as f:
        f.write(_msg("LEAD", "still fresh", "2026-07-03T11:00:00+00:00"))

    inbox.rotate(p, max_processed=1)

    archive = p.with_name("inbox.archive.md")
    archive_text = archive.read_text()
    assert "old 1" in archive_text
    assert "old 2" in archive_text

    remaining = p.read_text()
    assert remaining.startswith("# Inbox for d3")
    assert "still fresh" in remaining
    assert "old 1" not in remaining
    assert "old 2" not in remaining


def test_rotate_is_noop_below_threshold(tmp_path: Path) -> None:
    p = _make_inbox(tmp_path)
    with p.open("a") as f:
        f.write(_msg("LEAD", "one message", "2026-07-03T10:00:00+00:00"))

    inbox.mark_processed(p)
    inbox.rotate(p, max_processed=50)

    assert not p.with_name("inbox.archive.md").exists()
    assert "one message" in p.read_text()


def test_rotate_noop_when_nothing_processed(tmp_path: Path) -> None:
    p = _make_inbox(tmp_path)
    with p.open("a") as f:
        f.write(_msg("LEAD", "unprocessed", "2026-07-03T10:00:00+00:00"))

    inbox.rotate(p, max_processed=0)

    assert not p.with_name("inbox.archive.md").exists()
    assert "unprocessed" in p.read_text()


def test_rotate_repeatable_across_multiple_cycles(tmp_path: Path) -> None:
    p = _make_inbox(tmp_path)
    with p.open("a") as f:
        f.write(_msg("LEAD", "batch 1 msg", "2026-07-03T10:00:00+00:00"))
    inbox.mark_processed(p)
    inbox.rotate(p, max_processed=0)

    with p.open("a") as f:
        f.write(_msg("LEAD", "batch 2 msg", "2026-07-03T11:00:00+00:00"))
    inbox.mark_processed(p)
    inbox.rotate(p, max_processed=0)

    archive_text = p.with_name("inbox.archive.md").read_text()
    assert "batch 1 msg" in archive_text
    assert "batch 2 msg" in archive_text
    assert p.read_text().strip() == "# Inbox for d3"
