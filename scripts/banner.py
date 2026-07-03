#!/usr/bin/env python3
"""Prints an ASCII-art banner spelling ORCHESTRA."""

WORD = "ORCHESTRA"

FONT: dict[str, list[str]] = {
    "O": ["#####", "#...#", "#...#", "#...#", "#####"],
    "R": ["####.", "#...#", "####.", "#..#.", "#...#"],
    "C": [".####", "#....", "#....", "#....", ".####"],
    "H": ["#...#", "#...#", "#####", "#...#", "#...#"],
    "E": ["#####", "#....", "###..", "#....", "#####"],
    "S": [".####", "#....", ".###.", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#.."],
    "A": [".###.", "#...#", "#####", "#...#", "#...#"],
}


def banner() -> str:
    rows = [" ".join(FONT[letter][row] for letter in WORD) for row in range(5)]
    border = "=" * len(rows[0])
    return "\n".join([border, *rows, border])


if __name__ == "__main__":
    print(banner())
