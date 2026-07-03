"""Tests for the ORCHESTRA ASCII-art banner (scripts/banner.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from banner import banner  # noqa: E402

EXPECTED = "\n".join(
    [
        "=" * 53,
        "##### ####. .#### #...# ##### .#### ##### ####. .###.",
        "#...# #...# #.... #...# #.... #.... ..#.. #...# #...#",
        "#...# ####. #.... ##### ###.. .###. ..#.. ####. #####",
        "#...# #..#. #.... #...# #.... ....# ..#.. #..#. #...#",
        "##### #...# .#### #...# ##### ####. ..#.. #...# #...#",
        "=" * 53,
    ]
)


def test_banner_returns_expected_ascii_art() -> None:
    assert banner() == EXPECTED


def test_banner_is_deterministic() -> None:
    assert banner() == banner()


def test_banner_has_bordered_lines_of_equal_width() -> None:
    lines = banner().split("\n")
    assert len(lines) == 7
    widths = {len(line) for line in lines}
    assert widths == {53}
    assert lines[0] == "=" * 53
    assert lines[-1] == "=" * 53


def test_banner_uses_only_expected_characters() -> None:
    assert set(banner()) <= set("#. =\n")
