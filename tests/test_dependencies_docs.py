"""docs/dependencies.md says what it lists, and links uv.lock for the rest.

The page lists the direct dependencies and the tools a user meets. The full
resolved set, with every package at the version the repo is tested with, is
uv.lock's. The first screen says both, and links the lock relatively, so a
checkout reads it offline. Neither the page nor the README calls the page the
full or complete list, because the lock is.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPS = (ROOT / "docs" / "dependencies.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

LOCK_LINE = ("The full resolved set, every package at the version this repo "
             "is tested with, is in [`uv.lock`][uv-lock].")
SCOPE_LINE = ("This page lists sessiontools' direct dependencies and the tools "
              "a user meets:")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _first_screen(text: str) -> str:
    return _flat(text.split("\n## ", 1)[0])


def test_dependencies_page_says_what_it_lists_on_its_first_screen():
    assert SCOPE_LINE in _first_screen(DEPS)


def test_dependencies_page_links_the_lock_on_its_first_screen():
    assert (ROOT / "uv.lock").is_file()
    assert LOCK_LINE in _first_screen(DEPS)
    assert re.search(r"^\[uv-lock\]: \.\./uv\.lock$", DEPS, re.MULTILINE)


def test_neither_page_calls_the_dependencies_page_the_full_list():
    for name, text in (("docs/dependencies.md", DEPS), ("README.md", README)):
        flat = _flat(text).lower()
        for claim in ("the full list", "complete list", "everything sessiontools depends on"):
            assert claim not in flat, (name, claim)
