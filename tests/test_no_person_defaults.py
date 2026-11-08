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
scan catches it.

THE WIDENED SCAN: what ships gets scanned (ST-292, design (C), with its
later addition). Beyond
src's literals, the guard reads every surface of the public tree a stranger
gets (`git archive` ships tests/ too, whether or not it lands in the wheel):

- src docstrings, by `ast`;
- every file under docs/, CHANGELOG.md and README.md, as text, line by line;
- tests/: each `.py` file's string literals AND docstrings, by `ast`, and
  every other file there (fixtures, goldens) as text.

Those surfaces scan against a narrower row set than src's literals:
every P0 row, plus exactly two rows PINNED BY NAME, `domain-2` and
`product-1` (PINNED). Their patterns are read from the list at test time
like every other row's, and the names are neutral by design (SK-142), so
this file stays word-free. For these two rows only, the ruling OVERRIDES
this file's (and `_scanlist.rows`'s) convention that no particular row name
is asserted: a list that drops or renames one FAILS LOUDLY, naming it,
rather than scanning nothing. No other `ruling`-tier row gates these
surfaces; that residue is tabled in the task's evidence, not ruled.

No exemption is needed for the placeholder cast (FAKE_ROWS and the
fixtures above): measured, it matches no ruled row, and
`test_the_placeholder_cast_matches_no_ruled_row` keeps that measured. So
this file scans like every other, and a word planted anywhere in it is
caught. Comments are still out of scope: `ast` never sees one."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import _scanlist
import pytest

from sessiontools import cli, pane_state_agent
from sessiontools.sound import service
from sessiontools.web import bind

SRC = Path(__file__).resolve().parent.parent / "src"

#: The few literals src is measured to hold legitimately, by VALUE, read from
#: the code so this file never spells them. Each is spared alone: every row
#: still scans every other literal of its file. LEGACY_LABEL is the retired
#: launchd label: the retire must name it to boot the old job out and remove
#: its plist (the export's own allowlist carries the same two lines). The
#: web front door's IPv4 tailnet range is RFC 6598's shared address space, a
#: published constant that names no host: `check_bind` must hold it to accept
#: a literal tailnet address, and its refusal renders it from that one place.
LEGIT_LITERALS: dict[str, frozenset[str]] = {
    "src/sessiontools/pane_state_agent.py": frozenset({pane_state_agent.LEGACY_LABEL}),
    "src/sessiontools/sound/service.py": frozenset({service.LEGACY_LABEL}),
    "src/sessiontools/web/bind.py": frozenset({str(bind.TAILNET[0])}),
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


def _string_words_in(source: str, rows, filename: str, want) -> list[tuple[str, int, list[str]]]:
    """Every (filename, line, row names) for a str Constant that want(is_docstring, value)
    keeps and a row matches, in line order; line is where the string starts. Row NAMES
    only, never the text. f-string literal parts are Constants inside a JoinedStr, so
    `ast.walk` reaches them."""
    tree = ast.parse(source, filename=filename)
    docs = _docstring_ids(tree)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and want(id(node) in docs, node.value)):
            names = _scanlist.hits(node.value, rows)
            if names:
                found.append((filename, node.lineno, names))
    return sorted(found, key=lambda f: f[1])


def private_words_in(source: str, rows, filename: str = "<fixture>",
                     spare: frozenset[str] = frozenset()) -> list[tuple[str, int, list[str]]]:
    """Every (filename, line, row names) whose non-docstring string literal
    a row of the list matches; a literal equal to one in spare is skipped."""
    return _string_words_in(source, rows, filename,
                            lambda is_doc, value: not is_doc and value not in spare)


def docstring_words_in(source: str, rows, filename: str = "<fixture>"):
    """Every (filename, line, row names) whose DOCSTRING a row matches: src's widened half."""
    return _string_words_in(source, rows, filename, lambda is_doc, _value: is_doc)


def string_words_in(source: str, rows, filename: str = "<fixture>"):
    """Every (filename, line, row names) whose string literal or docstring a row matches:
    how a tests/ module is scanned."""
    return _string_words_in(source, rows, filename, lambda _is_doc, _value: True)


def text_words_in(text: str, rows, filename: str = "<fixture>"):
    """Every (filename, line, row names) for a line of text a row matches: how docs/,
    CHANGELOG.md, README.md and tests/' non-Python files are scanned."""
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        names = _scanlist.hits(line, rows)
        if names:
            found.append((filename, lineno, names))
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


# --- the widened scan: what ships, against every P0 row + two pinned rows -----

ROOT = SRC.parent
TESTS = ROOT / "tests"
#: The text surfaces, relative to the repo root: a directory is every file under it.
TEXT_SURFACES = ("docs", "CHANGELOG.md", "README.md")
#: The two rows pinned BY NAME beside every P0 row (ST-292 design (C)).
#: The one place this file names a row.
PINNED = ("domain-2", "product-1")


def ruled_rows():
    """The rows the widened surfaces scan against: every P0 row, plus each PINNED row
    whatever its tier. Unset list: the loud skip. A PINNED name the list lacks: FAIL,
    naming it, since scanning without it would pass quietly on nothing."""
    rows = _scanlist.rows()
    have = {r.name for r in rows}
    missing = [n for n in PINNED if n not in have]
    if missing:
        pytest.fail(f"{_scanlist.ENV}: the list has no row named {', '.join(missing)}; "
                    "this guard pins it by name (ST-292 design (C)), so it cannot scan",
                    pytrace=False)
    return [r for r in rows if r.tier == "P0" or r.name in PINNED]


def _files(base: Path) -> list[Path]:
    """base itself if a file, else every file under it but __pycache__, sorted."""
    if base.is_file():
        return [base]
    return sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def _read(path: Path) -> str:
    # errors="replace": a stray binary file is still read for its text, never a crash.
    return path.read_text(encoding="utf-8", errors="replace")


def words_in_tests_file(source: str, rows, rel: str):
    """How a tests/ file is scanned: a `.py` by its strings and docstrings, else as text."""
    if rel.endswith(".py"):
        return string_words_in(source, rows, rel)
    return text_words_in(source, rows, rel)


def _list_file(tmp_path: Path, lines: list[str]) -> Path:
    """A placeholder list in the release-scan format, for the shape tests."""
    path = tmp_path / "SCRUB-RULES.fake"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


# shape, on the placeholder cast (no real list needed)


def test_docstring_scan_reads_docstrings_only():
    source = '"""Vera ruled."""\nX = "vera"\ndef f():\n    """ask vera"""\n'
    assert docstring_words_in(source, FAKE_ROWS) == [("<fixture>", 1, ["vera"]),
                                                     ("<fixture>", 4, ["vera"])]


def test_string_scan_reads_literals_and_docstrings():
    source = '"""Vera ruled."""\nX = "vera"\n# vera, a comment\nY = f"{X}: vera"\n'
    assert [line for _f, line, _n in string_words_in(source, FAKE_ROWS)] == [1, 2, 4]


def test_text_scan_reports_each_line_by_number():
    text = "# Title\n\nVera wrote this.\nnothing here\nask VERA\n"
    assert text_words_in(text, FAKE_ROWS, "docs/x.md") == [("docs/x.md", 3, ["vera"]),
                                                          ("docs/x.md", 5, ["vera"])]


def test_ruled_rows_are_every_p0_row_and_the_pinned_two(tmp_path, monkeypatch):
    """P0 rows and the PINNED rows (whatever their tier) are in; any other ruling-tier
    row is out."""
    monkeypatch.setenv(_scanlist.ENV, str(_list_file(tmp_path, [
        "fake-p0\ti\tP0\t\\bvera\\b",
        f"{PINNED[0]}\ti\truling\t\\bvela\\b",
        f"{PINNED[1]}\ti\truling\t\\bvena\\b",
        "fake-ruling\ti\truling\t\\bvesa\\b",
    ])))
    assert sorted(r.name for r in ruled_rows()) == sorted(["fake-p0", *PINNED])


@pytest.mark.parametrize("dropped", PINNED)
def test_a_pinned_row_missing_from_the_list_fails_loudly(dropped, tmp_path, monkeypatch):
    """A list without a PINNED row is a red test that names the row, never a quiet scan."""
    kept = [n for n in PINNED if n != dropped]
    monkeypatch.setenv(_scanlist.ENV, str(_list_file(
        tmp_path, ["fake-p0\ti\tP0\t\\bvera\\b", *(f"{n}\ti\truling\t\\bvela\\b" for n in kept)])))
    with pytest.raises(pytest.fail.Exception) as raised:
        ruled_rows()
    message = str(raised.value)
    assert f"no row named {dropped};" in message
    assert all(n not in message for n in kept)


# with the private scan list: the controls, then the tree


def test_the_placeholder_cast_matches_no_ruled_row():
    """Why tests/ needs no exemption: the placeholder cast's word, and every shape
    fixture above, matches no ruled row. Were a list row ever to match one, this goes
    red here, naming the rows, before the tree scan does."""
    rows = ruled_rows()
    word = _scanlist.sample(FAKE_ROWS[0])
    names = _scanlist.hits(f" {word} ", rows)
    assert names == [], f"the placeholder cast matches ruled rows: {', '.join(names)}"


def test_every_pinned_row_and_every_p0_row_yields_a_control():
    """A pinned row with no word to plant would leave its controls proving nothing."""
    rows = ruled_rows()
    planted = {name for name, _word in _scanlist.controls(rows)}
    unplanted = sorted({r.name for r in rows} - planted)
    assert unplanted == [], f"no control word for rows: {', '.join(unplanted)}"


def test_a_planted_ruled_word_fails_each_widened_surface():
    """The positive control, per surface: every ruled row's own word, planted in a src
    docstring, a tests/ literal and docstring, and a line of markdown, is caught by that
    row. The word comes from the list at run time, never from this repo."""
    rows = ruled_rows()
    missed = []
    for name, word in _scanlist.controls(rows):
        said = f" {word} "
        cases = {
            "src module docstring": docstring_words_in(f"{said!r}\nX = 1\n", rows),
            "src function docstring": docstring_words_in(
                f"def f():\n    {said!r}\n    return 1\n", rows),
            "tests literal": string_words_in(f"X = {said!r}\n", rows),
            "tests docstring": string_words_in(f"class T:\n    {said!r}\n", rows),
            "docs line": text_words_in(f"# Title\n\nsee{said}here\n", rows),
        }
        missed.extend(f"{name} in {where}" for where, found in cases.items()
                      if not any(name in names for _f, _n, names in found))
    assert missed == [], f"the widened scan missed: {', '.join(missed)}"


def test_a_word_planted_in_this_very_file_is_caught():
    """No exemption hides a word: this guard's own source, placeholder cast and all,
    with a ruled row's word planted as a literal, a docstring and a fixture line, is
    caught by the tests/ scan at that line, every time."""
    rows = ruled_rows()
    rel = str(Path(__file__).resolve().relative_to(ROOT))
    own = _read(Path(__file__))
    end = own.count("\n")                  # own ends in a newline: the last line's number
    missed = []
    for name, word in _scanlist.controls(rows):
        said = f" {word} "
        for planted, at in ((f"PLANTED = {said!r}\n", end + 1),
                            (f"def planted():\n    {said!r}\n", end + 2)):
            found = words_in_tests_file(own + planted, rows, rel)
            if not any(name in names and line == at for _f, line, names in found):
                missed.append(name)
        found = words_in_tests_file(f'{{"text": "{word}"}}\n', rows, "tests/fixtures/planted.jsonl")
        if not any(name in names for _f, _n, names in found):
            missed.append(name)
    assert missed == [], f"a word planted in {rel} was missed for rows: {', '.join(sorted(set(missed)))}"


def test_src_docstrings_name_no_ruled_word():
    rows = ruled_rows()
    found = []
    for path in sorted(SRC.rglob("*.py")):
        found.extend(docstring_words_in(_read(path), rows, str(path.relative_to(ROOT))))
    assert found == [], "ruled scan rows hit src docstrings:\n" + _report(found)


@pytest.mark.parametrize("surface", TEXT_SURFACES)
def test_docs_changelog_and_readme_name_no_ruled_word(surface):
    base = ROOT / surface
    assert base.exists(), f"{surface} is gone: the guard would scan nothing there"
    rows = ruled_rows()
    found = []
    for path in _files(base):
        found.extend(text_words_in(_read(path), rows, str(path.relative_to(ROOT))))
    assert found == [], f"ruled scan rows hit {surface}:\n" + _report(found)


def test_tests_name_no_ruled_word():
    """tests/ ships in the public tree, so it is scanned whole, this file included."""
    files = _files(TESTS)
    assert Path(__file__).resolve() in files, "the tests/ scan must reach this guard too"
    rows = ruled_rows()
    found = []
    for path in files:
        rel = str(path.relative_to(ROOT))
        found.extend(words_in_tests_file(_read(path), rows, rel))
    assert found == [], "ruled scan rows hit tests/:\n" + _report(found)
