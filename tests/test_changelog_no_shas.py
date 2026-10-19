"""CHANGELOG.md carries no commit shas (ST-192).

The public repos are exported as one clean commit each, so a sha in the
changelog points at history the reader cannot see. Bare tracker refs
(`ST-102`, `SK-24`) stay; only the hex goes.

The guard is any 7-40 character run of lowercase hex standing alone as a
word: `git log --oneline` prints 7 or more, a full sha is 40. There is no
exemption list. An all-hex English word of 7 or more letters (`defaced`,
`effaced`) or a bare number of 7 or more digits would trip it; reword the
line rather than widen the guard.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


def sha_lines(text: str) -> list[tuple[int, str]]:
    """Each (line number, sha) in `text`, numbered from 1."""
    return [(n, m.group())
            for n, line in enumerate(text.splitlines(), start=1)
            for m in SHA.finditer(line)]


def test_changelog_has_no_commit_shas():
    found = sha_lines(CHANGELOG.read_text(encoding="utf-8"))
    assert not found, "commit shas in CHANGELOG.md: " + ", ".join(
        f"line {n}: {sha}" for n, sha in found)


def test_finds_short_and_full_shas_with_their_lines():
    text = "- Fixed (ST-62, 61d1607).\n\nTag `v0.0.4` (" + "a" * 40 + ")\n"
    assert sha_lines(text) == [(1, "61d1607"), (3, "a" * 40)]


def test_finds_every_sha_on_one_line():
    assert [s for _, s in sha_lines("(4d31b8f, 66e5dc5, 96559ff)")] == [
        "4d31b8f", "66e5dc5", "96559ff"]


def test_leaves_tracker_refs_versions_and_dates_alone():
    text = ("(ST-102, SK-24, SESH-1114) v0.1.0 2030-12-08 "
            "`git log v0.0.4..v0.1.0` 0600 abc1234x deadbee5f00d1234567890abcdef12345678901234")
    assert sha_lines(text) == []


def test_six_hex_and_uppercase_are_not_shas():
    assert sha_lines("cafe12 CAFEBABE") == []
