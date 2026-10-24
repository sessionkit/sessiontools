"""docs/session.md is the `session` command's page, and it names every verb.

The kit imports this page as its `session` page, so a verb the dispatcher
offers and the page leaves out is a verb a reader of the kit never finds.
The verbs come from the dispatcher's own table, never a second list.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sessiontools import cli

PAGE = Path(__file__).resolve().parent.parent / "docs" / "session.md"


def _section(text: str, heading: str) -> str:
    """The body of the `## heading` section of TEXT."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert m, f"docs/session.md has no '## {heading}' section"
    return m.group(1)


def test_the_page_names_the_command_first() -> None:
    first = PAGE.read_text(encoding="utf-8").splitlines()[0]
    assert first == "# session"


@pytest.mark.parametrize("verb", [v for v, _ in cli.VERBS])
def test_every_verb_has_a_purpose_line(verb: str) -> None:
    verbs = _section(PAGE.read_text(encoding="utf-8"), "The verbs")
    assert re.search(rf"^- `{re.escape(verb)}`: \S", verbs, re.MULTILINE), (
        f"docs/session.md '## The verbs' has no '- `{verb}`: ...' line")


@pytest.mark.parametrize("verb", [v for v, _ in cli.VERBS])
def test_every_verb_has_its_form(verb: str) -> None:
    forms = _section(PAGE.read_text(encoding="utf-8"), "Each verb's form and one example")
    assert re.search(rf"^- `session {re.escape(verb)}\b", forms, re.MULTILINE), (
        f"docs/session.md gives no form for `session {verb}`")


def test_the_page_lists_no_verb_the_dispatcher_lacks() -> None:
    verbs = _section(PAGE.read_text(encoding="utf-8"), "The verbs")
    listed = set(re.findall(r"^- `([a-z-]+)`:", verbs, re.MULTILINE))
    assert listed == {v for v, _ in cli.VERBS}
