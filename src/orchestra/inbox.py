#!/usr/bin/env python3
"""Read-side inbox hygiene: processed markers + rotation.

The write side (`server.py` / `messaging.deliver`) only ever APPENDS
blank-line-delimited blocks of the form `[<iso8601>] SENDER: text` to
`<name>/inbox.md`, starting from a `# Inbox for <name>` header. This module
is the consume side agents use instead of `cat inbox.md`:

  - `read_new` returns only the message blocks appended since the last
    `mark_processed` call.
  - `mark_processed` appends a `<!-- processed-through: <iso8601> -->`
    marker line. Everything after the LAST such marker in the file is
    "new" — position in the file, not the timestamp value, is what
    determines newness, so the marker can be appended idempotently without
    needing to compare clocks.
  - `rotate` moves everything up to and including the last marker into a
    sibling `inbox.archive.md` once the processed backlog exceeds
    `max_processed`, leaving only the header plus any unprocessed tail in
    `inbox.md`. This is what keeps a long-running agent from re-scanning
    years of old messages on every read.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("orchestra.inbox")

HEADER_PREFIX = "# Inbox for "
MARKER_PREFIX = "<!-- processed-through: "
MARKER_SUFFIX = " -->"
DEFAULT_MAX_PROCESSED = 200


def _blocks(text: str) -> list[str]:
    """Split inbox content on blank lines into header/message/marker blocks."""
    return [b.strip() for b in text.split("\n\n") if b.strip()]


def _is_header(block: str) -> bool:
    return block.startswith(HEADER_PREFIX)


def _is_marker(block: str) -> bool:
    return block.startswith(MARKER_PREFIX)


def _last_marker_index(blocks: list[str]) -> int:
    for i in range(len(blocks) - 1, -1, -1):
        if _is_marker(blocks[i]):
            return i
    return -1


def read_new(inbox_path: Path) -> list[str]:
    """Return message blocks appended after the last processed marker.

    If no marker exists yet, every message in the file is new. Returns []
    if the inbox doesn't exist.
    """
    if not inbox_path.exists():
        return []
    blocks = _blocks(inbox_path.read_text())
    tail = blocks[_last_marker_index(blocks) + 1:]
    return [b for b in tail if not _is_header(b)]


def mark_processed(inbox_path: Path) -> None:
    """Append a processed marker recording the point read_new() consumed through."""
    ts = datetime.now(timezone.utc).isoformat()
    with inbox_path.open("a") as f:
        f.write(f"\n{MARKER_PREFIX}{ts}{MARKER_SUFFIX}\n")
    logger.info("marked %s processed through %s", inbox_path, ts)


def rotate(inbox_path: Path, max_processed: int = DEFAULT_MAX_PROCESSED) -> None:
    """Archive processed messages once their count exceeds max_processed.

    Moves every block up to and including the last processed marker into a
    sibling `inbox.archive.md`, leaving only the `# Inbox for <name>`
    header plus any unprocessed tail in inbox.md. A no-op if nothing has
    been marked processed yet, or if the processed backlog is still under
    the threshold.
    """
    if not inbox_path.exists():
        return
    blocks = _blocks(inbox_path.read_text())
    marker_idx = _last_marker_index(blocks)
    if marker_idx == -1:
        return

    header = next((b for b in blocks if _is_header(b)), f"# Inbox for {inbox_path.parent.name}")
    processed = [b for b in blocks[:marker_idx + 1] if not _is_header(b)]
    processed_messages = [b for b in processed if not _is_marker(b)]
    if len(processed_messages) <= max_processed:
        return

    archive_path = inbox_path.with_name(inbox_path.stem + ".archive" + inbox_path.suffix)
    archive_is_empty = not archive_path.exists() or archive_path.stat().st_size == 0
    with archive_path.open("a") as f:
        if archive_is_empty:
            f.write(header + "\n\n")
        else:
            f.write("\n")
        f.write("\n\n".join(processed) + "\n")

    unprocessed = [b for b in blocks[marker_idx + 1:] if not _is_header(b)]
    new_content = header + "\n"
    if unprocessed:
        new_content += "\n" + "\n\n".join(unprocessed) + "\n"
    inbox_path.write_text(new_content)
    logger.info("rotated %s: archived %d processed messages", inbox_path, len(processed_messages))
