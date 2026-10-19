"""No private word in what src SAYS or DEFAULTS to.

A stranger's install has none of our people, personas, hosts or places, so
no string a tool prints, raises or falls back to may name one. Those words
are private, so this guard never spells them out: it reads them at run time
from the private scan list (tests/_scanlist.py, the file SESSIONKIT_SCRUB_RULES
names) and SKIPS LOUDLY without it. A failure names the list's rows by their
NAME column and the file:line, never the matched text.

The scan reads every `src/` module with `ast` and checks each string literal
that is NOT a docstring - which covers a `DEFAULT = "<a name>"`, a raised
message, an argparse `help=` or `description=`, and the literal parts of an
f-string. Comments and docstrings are out of scope: `ast` never sees a
comment, and docstrings are skipped by position.

The rendered `--help` of `session` and of each verb is checked too, so a
name assembled at runtime cannot slip past the literal scan.

The scanner is proved, not trusted. Its shape is pinned on the placeholder
cast through a FAKE row, so those tests run with or without the real list.
With the real list, `test_a_planted_private_word_fails_the_scan` plants each
row's own control word as a default in a src-like fixture and asserts the
scan catches it."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import _scanlist
import pytest

from sessiontools import cli, pane_state_agent
from sessiontools.sound import service

SRC = Path(__file__).resolve().parent.parent / "src"

#: The few literals src is measured to hold legitimately, by VALUE, read from
#: the code so this file never spells them. Each is spared alone: every row
#: still scans every other literal of its file. LEGACY_LABEL is the retired
#: launchd label: the retire must name it to boot the old job out and remove
#: its plist (the export's own allowlist carries the same two lines).
LEGIT_LITERALS: dict[str, frozenset[str]] = {
    "src/sessiontools/pane_state_agent.py": frozenset({pane_state_agent.LEGACY_LABEL}),
    "src/sessiontools/sound/service.py": frozenset({service.LEGACY_LABEL}),
}

#: `session start`'s --seat and --key-label help show a project+role seat
#: address by example: a product's seat, not a person. A row that matches that
#: example is left out of args.py and of `start --help`, by WHAT IT MATCHES,
#: never by its name (the names are the list's to change). The example is read
#: from args.py (seat_examples), so this file never spells it.
SEAT_ARGS = "src/sessiontools/start/args.py"
SEAT_FLAGS = ("--seat", "--key-label")
SEAT_VERB = "start"
#: The help's own name for the shape, which it shows beside the example.
SEAT_PLACEHOLDER = "project+role"
_SEAT_ADDRESS = re.compile(r"\b[a-z][a-z0-9-]*\+[a-z]+\b")


def _help_text(node: ast.AST) -> str:
    """The literal text of a help= value: a string, or an f-string's literal parts."""
    return "".join(n.value for n in ast.walk(node)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))


def seat_examples() -> tuple[str, ...]:
    """The seat addresses the SEAT_FLAGS help shows by example, read from args.py:
    each distinct project+role address in those help= texts but the placeholder, sorted."""
    tree = ast.parse((SRC.parent / SEAT_ARGS).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in SEAT_FLAGS):
            for kw in node.keywords:
                if kw.arg == "help":
                    found.update(_SEAT_ADDRESS.findall(_help_text(kw.value)))
    return tuple(sorted(found - {SEAT_PLACEHOLDER}))


def seat_split(rows):
    """(kept, left_out) of rows, split on the seat example (`_scanlist.split`)."""
    return _scanlist.split(rows, seat_examples())


def rows_for_src(rel: str):
    """The rows that scan src file rel: every row, but the seat example's in args.py."""
    rows = _scanlist.rows()
    return seat_split(rows)[0] if rel == SEAT_ARGS else rows


def rows_for_help(verb: str):
    """The rows that scan `session <verb> --help`: every row, but the seat example's in
    `start`'s, which renders that help."""
    rows = _scanlist.rows()
    return seat_split(rows)[0] if verb == SEAT_VERB else rows

#: A row of the placeholder cast, for the scanner's shape tests: no real list needed.
FAKE_ROWS = [_scanlist.Row("vera", "P0", re.compile(r"\bvera\b", re.IGNORECASE | re.MULTILINE), 1)]


def _docstring_ids(tree: ast.AST) -> set[int]:
    """The id() of every docstring Constant: the first statement of a
    module, class or function body when it is a bare string expression."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def private_words_in(source: str, rows, filename: str = "<fixture>",
                     spare: frozenset[str] = frozenset()) -> list[tuple[str, int, list[str]]]:
    """Every (filename, line, row names) whose non-docstring string literal
    a row of the list matches; a literal equal to one in spare is skipped.
    Row NAMES only, never the literal. f-string literal parts are Constants
    inside a JoinedStr, so `ast.walk` reaches them."""
    tree = ast.parse(source, filename=filename)
    skip = _docstring_ids(tree)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip and node.value not in spare):
            names = _scanlist.hits(node.value, rows)
            if names:
                found.append((filename, node.lineno, names))
    return found


def _report(found: list[tuple[str, int, list[str]]]) -> str:
    return "\n".join(f"  {f}:{n}: rows {', '.join(names)}" for f, n, names in found)


# --- the scanner's shape, on the placeholder cast (no list needed) ------------


def test_scanner_catches_a_planted_default():
    """The rule's whole point: a planted `DEFAULT = "vera"` FAILS the scan,
    and the report names the row, not the word."""
    found = private_words_in('DEFAULT = "vera"\n', FAKE_ROWS)
    assert found == [("<fixture>", 1, ["vera"])]


@pytest.mark.parametrize("source", [
    'OPERATOR = "Vera"\n',
    'def f():\n    raise ValueError("waiting on Vera\'s ruling")\n',
    'p.add_argument("--x", help="ask vera first")\n',
    'p = ArgumentParser(description="Vera answers")\n',
    'def f(n):\n    return f"{n}: vera was here"\n',
    'ROW = ("sound", "signal: VERA\'s line")\n',
    'def f():\n    x = 1\n    "vera, a bare string that is not the first statement"\n',
])
def test_scanner_catches_names_outside_docstrings(source):
    found = private_words_in(source, FAKE_ROWS)
    assert found, source


@pytest.mark.parametrize("source", [
    '"""Vera ruled this (voice 12:34)."""\n',
    'class C:\n    """Vera\'s review."""\n',
    'def f():\n    """Vera asked for it."""\n    return 1\n',
    'async def f():\n    """Vera, 2030-12-08."""\n',
    '# Vera, voice 12:34: a comment is never seen\nX = 1\n',
    'PATH = "alice/veranda"\nWORD = "avera"\n',
])
def test_scanner_spares_docstrings_comments_and_non_names(source):
    found = private_words_in(source, FAKE_ROWS)
    assert found == [], source


def test_the_leave_outs_are_measured_and_small():
    """A leave-out is a measured exception, never a blanket. The seat example is ONE
    address, shown by both flags' help, in the file and verb named; a spared literal
    stands once in its file. Needs no list."""
    examples = seat_examples()
    assert len(examples) == 1, f"want one seat example in {SEAT_FLAGS} help, found {len(examples)}"
    text = (SRC.parent / SEAT_ARGS).read_text(encoding="utf-8")
    assert text.count(examples[0]) == len(SEAT_FLAGS), "each seat flag's help shows the example once"
    assert SEAT_VERB in {v for v, _ in cli.VERBS}
    for rel, values in LEGIT_LITERALS.items():
        text = (SRC.parent / rel).read_text(encoding="utf-8")
        assert all(text.count(repr(v)) + text.count(f'"{v}"') == 1 for v in values), rel


def test_a_spared_literal_spares_only_itself():
    """The value spare skips that literal alone: the same word elsewhere in
    the file, in a longer literal, is still caught."""
    source = 'OLD = "vera.label"\nMSG = "see vera.label"\n'
    found = private_words_in(source, FAKE_ROWS, spare=frozenset({"vera.label"}))
    assert found == [("<fixture>", 2, ["vera"])]


# --- with the private scan list: the control, then the tree -------------------


def test_a_planted_private_word_fails_the_scan():
    """The positive control: every row's own word, planted as a default in a
    src-like fixture, is caught by that row. The word comes from the list at
    run time, never from this repo, and a miss is reported by row name."""
    rows = _scanlist.rows()
    missed = []
    for name, word in _scanlist.controls(rows):
        found = private_words_in(f"DEFAULT = {f' {word} '!r}\n", rows)
        if not any(name in names for _f, _n, names in found):
            missed.append(name)
    assert missed == [], f"the scan missed a planted word for rows: {', '.join(missed)}"


def test_a_planted_private_word_fails_the_scan_beside_the_seat_example():
    """The seat example hides only the rows it matches: in args.py, every other row's
    planted word is still caught."""
    rows = rows_for_src(SEAT_ARGS)
    missed = []
    for name, word in _scanlist.controls(rows):
        found = private_words_in(f"DEFAULT = {f' {word} '!r}\n", rows, SEAT_ARGS)
        if not any(name in names for _f, _n, names in found):
            missed.append(name)
    assert missed == [], f"{SEAT_ARGS}: the scan missed rows: {', '.join(missed)}"


def test_rows_are_left_out_only_for_the_seat_example():
    """The left-out set is non-empty, and every row in it was left out for the seat
    example: a dead example, or a row dropped for anything else, is red. Rows are
    reported by NAME, and no particular name is asserted."""
    _kept, left_out = seat_split(_scanlist.rows())
    assert left_out, "no row matches the seat example: the leave-out is dead, drop it"
    stray = sorted(n for n, why in left_out.items() if set(why) != set(seat_examples()))
    assert stray == [], f"left out for no seat example, by row: {stray}"


def _renamed(text: str) -> str:
    """text with every row's NAME rewritten, everything after the first TAB byte-identical."""
    out = []
    for i, line in enumerate(text.split("\n")):
        if line.strip() and not line.lstrip().startswith("#"):
            line = f"renamed-{i}\t{line.split(chr(9), 1)[1]}"
        out.append(line)
    return "\n".join(out)


def test_the_leave_out_is_rename_invariant(tmp_path, monkeypatch):
    """Rename every row of the real list, keep every pattern: the same rows (by line)
    are left out of args.py and `start --help`, so a rename cannot change this guard."""
    def by_line():
        kept, left_out = seat_split(_scanlist.rows())
        line_of = {r.name: r.lineno for r in _scanlist.rows()}
        return [r.lineno for r in kept], sorted(line_of[n] for n in left_out)
    before = by_line()
    renamed = tmp_path / "SCRUB-RULES.renamed"
    renamed.write_text(_renamed(Path(os.environ[_scanlist.ENV]).read_text(encoding="utf-8")),
                       encoding="utf-8")
    monkeypatch.setenv(_scanlist.ENV, str(renamed))
    names = {r.name for r in _scanlist.rows()}
    assert all(n.startswith("renamed-") for n in names)
    assert by_line() == before


def test_src_string_literals_name_no_private_word():
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC.parent))
        rows = rows_for_src(rel)
        found.extend(private_words_in(path.read_text(encoding="utf-8"), rows, rel,
                                      LEGIT_LITERALS.get(rel, frozenset())))
    assert found == [], "private scan rows hit src string literals:\n" + _report(found)


def _help(argv: list[str], capsys) -> str:
    """The text `session <argv>` prints. argparse's --help exits; `start`
    (its own parser in sessiontools.start) prints and returns instead."""
    try:
        cli.main(argv)
    except SystemExit:
        pass
    out = capsys.readouterr()
    return out.out + out.err


def test_session_usage_names_no_private_word():
    rows = _scanlist.rows()
    found = _scanlist.hits(cli.usage(), rows)
    assert found == [], f"private scan rows hit `session` usage: {', '.join(found)}"


@pytest.mark.parametrize("verb", [v for v, _ in cli.VERBS])
def test_each_verb_help_names_no_private_word(verb, capsys):
    rows = rows_for_help(verb)
    text = _help([verb, "--help"], capsys)
    assert text.strip(), f"session {verb} --help printed nothing"
    found = _scanlist.hits(text, rows)
    assert found == [], f"private scan rows hit `session {verb} --help`: {', '.join(found)}"
