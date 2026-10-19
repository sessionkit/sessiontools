"""tests/_scanlist.py, the private scan list's reader for the guard tests, on a FAKE
list of placeholder words: these run with or without the real list."""
import re
import subprocess
import sys
from pathlib import Path

import _scanlist
import pytest

FAKE = (
    "# a comment, then a blank line\n"
    "\n"
    "vera\ts\tP0\t\\bVera\\b\n"
    "ivy-any-case\ti\tP0\tivy\n"
    "theo-dir\ts\truling\t~/\\.(theo|dave)\\b\n"
    "carol-tab\ts\truling\tcarol\\tbob\n"          # everything after the third TAB is the regex
    "  padded  \ti\truling\t  alice  "
)


@pytest.fixture
def listfile(tmp_path, monkeypatch):
    p = tmp_path / "SCRUB-RULES"
    p.write_text(FAKE + "\n")
    monkeypatch.setenv(_scanlist.ENV, str(p))
    _scanlist._cached.cache_clear()
    yield p
    _scanlist._cached.cache_clear()


def test_load_reads_the_four_fields(listfile):
    rows = _scanlist.load(listfile)
    assert [(r.name, r.tier, r.lineno) for r in rows] == [
        ("vera", "P0", 3), ("ivy-any-case", "P0", 4), ("theo-dir", "ruling", 5),
        ("carol-tab", "ruling", 6), ("padded", "ruling", 7)]
    assert rows[3].pattern.pattern == "carol\\tbob"
    assert rows[4].pattern.pattern == "alice"     # fields are stripped, as release-scan does


def test_the_case_flag_is_honoured(listfile):
    rows = _scanlist.rows()
    assert _scanlist.hits("Vera", rows) == ["vera"]
    assert _scanlist.hits("vera", rows) == []                  # s: case kept
    assert _scanlist.hits("IVY", rows) == ["ivy-any-case"]     # i: case ignored


def test_hits_are_names_matched_line_by_line(listfile):
    rows = _scanlist.rows()
    assert _scanlist.hits("x ~/.theo/log\nVera\nnothing\ncarol\tbob", rows) == \
        ["carol-tab", "theo-dir", "vera"]
    assert _scanlist.hits("carol\nbob", rows) == []            # no match across a line break
    assert _scanlist.hits("", rows) == []


def test_rows_picks_a_tier(listfile):
    assert [r.name for r in _scanlist.rows("P0")] == ["vera", "ivy-any-case"]
    assert len(_scanlist.rows("P0", "ruling")) == 5
    with pytest.raises(ValueError):
        _scanlist.rows("P1")


def test_rows_takes_no_row_names():
    with pytest.raises(TypeError):
        _scanlist.rows(but=("vera",))                          # rows are never left out by name


# Neutral text a guarded subject might carry by design: two samples, each matched by one
# fake row, and one that no row matches.
SAMPLES = ("see ~/.theo for logs", "Ivy League", "nothing to see")


def test_split_leaves_out_the_rows_a_sample_matches(listfile):
    kept, left_out = _scanlist.split(_scanlist.rows(), SAMPLES)
    assert [r.name for r in kept] == ["vera", "carol-tab", "padded"]
    assert left_out == {"ivy-any-case": ("Ivy League",), "theo-dir": ("see ~/.theo for logs",)}
    kept, left_out = _scanlist.split(_scanlist.rows("P0"), SAMPLES)
    assert [r.name for r in kept] == ["vera"] and list(left_out) == ["ivy-any-case"]
    assert _scanlist.split(_scanlist.rows(), ())[1] == {}       # no samples, nothing left out


def test_split_refuses_one_string_and_an_empty_result(listfile):
    with pytest.raises(TypeError):
        _scanlist.split(_scanlist.rows(), "Ivy League")
    with pytest.raises(pytest.fail.Exception):
        _scanlist.split(_scanlist.rows("P0"), ("Vera and ivy",))   # nothing left to scan with


def renamed(text, prefix):
    """text with every row's NAME rewritten, everything after the first TAB byte-identical."""
    out = []
    for i, line in enumerate(text.split("\n")):
        if line.strip() and not line.lstrip().startswith("#"):
            line = f"{prefix}-{i}\t{line.split(chr(9), 1)[1]}"
        out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize("prefix", ["neutral", "vera"])
def test_split_is_rename_invariant(tmp_path, listfile, monkeypatch, prefix):
    """Rename every row, keep every pattern: the same rows (by line) are kept and left out,
    for the same samples. A guard built on split() cannot tell a renamed list apart."""
    def by_line():
        rows = _scanlist.rows()
        line_of = {r.name: r.lineno for r in rows}
        kept, left_out = _scanlist.split(rows, SAMPLES)
        return [r.lineno for r in kept], {line_of[n]: why for n, why in left_out.items()}
    before = by_line()
    p = tmp_path / "SCRUB-RULES.renamed"
    p.write_text(renamed(FAKE, prefix) + "\n")
    names = [r.name for r in _scanlist.load(p)]
    assert names != [r.name for r in _scanlist.load(listfile)] and len(set(names)) == len(names)
    assert [r.pattern.pattern for r in _scanlist.load(p)] == [r.pattern.pattern for r in _scanlist.load(listfile)]
    monkeypatch.setenv(_scanlist.ENV, str(p))
    _scanlist._cached.cache_clear()
    assert by_line() == before
    assert before[1]                                           # something was left out to compare


def test_unset_skips_loudly_naming_the_variable(monkeypatch):
    monkeypatch.delenv(_scanlist.ENV, raising=False)
    with pytest.raises(pytest.skip.Exception) as e:
        _scanlist.rows()
    assert str(e.value).startswith(_scanlist.SKIP_PREFIX)
    assert _scanlist.ENV in str(e.value)
    monkeypatch.setenv(_scanlist.ENV, "")                       # empty is unset
    with pytest.raises(pytest.skip.Exception):
        _scanlist.rows()


def test_set_but_missing_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(_scanlist.ENV, str(tmp_path / "nowhere"))
    with pytest.raises(pytest.fail.Exception) as e:
        _scanlist.rows()
    assert "cannot read" in str(e.value)


@pytest.mark.parametrize("line, why", [
    ("vera\ts\tP0", "want 4"),
    ("vera\tx\tP0\tVera", "case must be"),
    ("vera\ts\tP9\tVera", "tier must be"),
    ("\ts\tP0\tVera", "empty name"),
    ("vera\ts\tP0\t(Vera", "bad regex"),
])
def test_a_bad_row_is_refused_by_line_never_by_its_text(tmp_path, line, why):
    p = tmp_path / "list"
    p.write_text(f"# ok\n{line}\n")
    with pytest.raises(_scanlist.ListError) as e:
        _scanlist.load(p)
    assert why in str(e.value) and f"{p}:2" in str(e.value)
    assert "Vera" not in str(e.value)                          # the regex is never echoed


def test_an_empty_list_is_refused(tmp_path):
    p = tmp_path / "list"
    p.write_text("# only comments\n\n")
    with pytest.raises(_scanlist.ListError, match="no rows"):
        _scanlist.load(p)


@pytest.mark.parametrize("regex, flags", [
    (r"\bVera\b", ""),
    (r"ivy", "i"),
    (r"(?<![0-9a-f]{4})dave|dave(?![0-9a-f]{4})", "i"),        # lookarounds, an alternation
    (r"~/\.(theo|dave)\b", ""),
    (r"\b(10\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3})\b", ""),
    (r"(\+99[ 0-9]|\b04[0-9]{2}[ -]?[0-9]{3}\b)", ""),
    (r"\b([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])? (Mon|Tue)\b", ""),
    (r"\b[a-z][a-z0-9-]*\+([a-z]+-[0-9]+\.)?(carol|bob)\b", "i"),
    (r"bob('s)?\b", ""),
    (r"[^x]vera\s+\d\W\S", ""),
    (r"theo.?ivy", ""),
])
def test_a_sample_is_synthesised_and_matches(regex, flags):
    row = _scanlist.Row("r", "P0", re.compile(regex, re.MULTILINE | (re.IGNORECASE if flags else 0)), 1)
    word = _scanlist.sample(row)
    assert word and row.pattern.search(f" {word} ")


@pytest.mark.parametrize("regex", [r"(a)\1", r"^$", r"\b"])
def test_no_sample_when_the_regex_is_beyond_the_generator(regex):
    assert _scanlist.sample(_scanlist.Row("r", "P0", re.compile(regex), 1)) is None


def test_controls_cover_every_fake_row(listfile):
    rows = _scanlist.rows()
    got = _scanlist.controls(rows)
    assert [n for n, _ in got] == [r.name for r in rows]
    for name, word in got:
        assert name in _scanlist.hits(f"x {word} x", rows)


def test_controls_fail_when_nothing_can_be_planted():
    with pytest.raises(pytest.fail.Exception):
        _scanlist.controls([_scanlist.Row("r", "P0", re.compile(r"(a)\1"), 1)])


SUMMARY_SUITE = '''
import pytest
import _scanlist

def test_a():
    _scanlist.rows()

def test_b():
    _scanlist.rows("P0")

def test_c():
    pytest.skip("another reason")
'''


@pytest.mark.parametrize("listed", [False, True])
def test_the_terminal_summary_counts_the_skips(tmp_path, listfile, listed):
    """Without the list, a bare run (no -rs) says how many guards skipped and names the
    variable; with it, the guards run and the section is absent."""
    (tmp_path / "test_guards.py").write_text(SUMMARY_SUITE)
    conftest = tmp_path / "conftest.py"
    conftest.write_text("import _scanlist\n\n"
                        "def pytest_terminal_summary(terminalreporter):\n"
                        "    _scanlist.summary(terminalreporter)\n")
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(_scanlist.__file__).parent)}
    if listed:
        env[_scanlist.ENV] = str(listfile)
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        str(tmp_path / "test_guards.py")],
                       capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
                       check=False)
    if listed:
        assert "2 passed, 1 skipped" in p.stdout, p.stdout
        assert "private scan list" not in p.stdout
    else:
        assert "3 skipped" in p.stdout, p.stdout
        assert "private scan list: 2 guard tests skipped" in p.stdout
        assert f"export {_scanlist.ENV}=" in p.stdout
