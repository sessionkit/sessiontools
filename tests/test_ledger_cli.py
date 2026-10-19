"""`session ledger`, the verb (ST-2 do-003): its help, its four views end to
end on a fixture transcript, its SEAT resolution and its exits.

SEAT is a session id or prefix, never a path (decompose-001 review): each
test points `CLAUDE_PROJECTS_DIR` at a temp dir holding `<proj>/<sid>.jsonl`
and names the sid, as `session context`'s tests do. The views are checked
against `ledger.render` over `ledger.items` with the real counter, so a test
here proves the WIRING; the figures themselves are gated in
test_ledger_gates.py.
"""

from __future__ import annotations

import json
import shutil

import pytest
from test_ledger_support import REAL_ENV, materialise, real_counter, requires_counter

from sessiontools import cli, ledger
from sessiontools.ledger import render

SID = "00000000-0000-4000-8000-000000000008"      # bash.jsonl's own sessionId
NESTED_SID = "00000000-0000-4000-8000-000000000001"  # nested-twice.jsonl's


def _run(argv, capsys):
    rc = cli.main(argv)
    out, err = capsys.readouterr()
    return rc, out, err


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """A projects dir holding bash.jsonl and nested-twice.jsonl under their
    own session ids, the real counter's caches in the environment, and no
    session id of the caller's own."""
    (tmp_path / "fx").mkdir()
    fx = materialise(tmp_path / "fx")
    root = tmp_path / "projects"
    proj = root / "-tmp-ledger-proj"
    proj.mkdir(parents=True)
    shutil.copyfile(fx / "bash.jsonl", proj / f"{SID}.jsonl")
    shutil.copyfile(fx / "nested-twice.jsonl", proj / f"{NESTED_SID}.jsonl")
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    for var, value in REAL_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    return proj


# --- the verb and its help ----------------------------------------------------------

def test_ledger_is_a_verb_with_a_handler_and_a_usage_row(capsys):
    assert "ledger" in dict(cli.VERBS)
    assert cli.HANDLERS["ledger"] is cli._ledger
    rc, out, _ = _run([], capsys)
    assert rc == 0 and "\n  ledger " in out


def test_help_gives_every_option_a_description_and_an_example(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["ledger", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for opt in ("SEAT", "--summary", "--short", "--long", "--json", "--width N"):
        lines = out.splitlines()
        at = next(i for i, ln in enumerate(lines) if ln.strip().startswith(opt + " "))
        assert lines[at].strip()[len(opt):].strip(), f"{opt} has no description"
        assert lines[at + 1].strip().startswith("session ledger"), f"{opt} has no example"
    assert "(default)" in out and f"default {render.DEFAULT_WIDTH}" in out
    assert "tiktoken cl100k_base" in out and "±10%" in out
    assert "Exit 1" in out and "Exit 2" in out


# --- end to end, each view ----------------------------------------------------------

@requires_counter
@pytest.mark.parametrize("flag,view", [
    ([], "summary"), (["--summary"], "summary"), (["--short"], "short"),
    (["--long"], "long"), (["--json"], "json")])
def test_each_view_runs_end_to_end_on_a_fixture_transcript(projects, capsys, flag, view):
    path = projects / f"{SID}.jsonl"
    want = render.VIEWS[view](ledger.items(path, real_counter()), session=path.stem)
    rc, out, err = _run(["ledger", *flag, SID], capsys)
    assert (rc, err) == (0, "")
    assert out == want
    assert out.strip(), "a view printed nothing"


@requires_counter
def test_a_session_id_prefix_resolves_as_session_context_does(projects, capsys):
    (projects / f"{NESTED_SID}.jsonl").unlink()   # the two fixture ids share 35 chars
    rc, out, _ = _run(["ledger", "--json", SID[:8]], capsys)
    assert rc == 0
    assert {json.loads(ln)["session"] for ln in out.splitlines()} == {SID}


@requires_counter
def test_no_seat_reads_this_sessions_own_transcript(projects, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", NESTED_SID)
    rc, out, _ = _run(["ledger", "--short"], capsys)
    assert rc == 0
    assert out.splitlines()[0].startswith(f"ledger {NESTED_SID[:8]} --short")


@requires_counter
def test_the_nested_load_twice_shows_twice_in_the_short_view(projects, capsys):
    rc, out, _ = _run(["ledger", "--short", NESTED_SID], capsys)
    assert rc == 0
    names = [ln.split(" ", 1)[1] for ln in out.splitlines()[1:]]
    assert sum(n == "CLAUDE.md" for n in names) == 2


@requires_counter
def test_width_cuts_the_short_views_commands(projects, capsys):
    path = projects / f"{SID}.jsonl"
    want = render.short(ledger.items(path, real_counter()), session=path.stem, width=40)
    rc, out, _ = _run(["ledger", "--short", "--width", "40", SID], capsys)
    assert (rc, out) == (0, want)
    assert "cut to 40 chars" in out.splitlines()[0]


@requires_counter
def test_json_from_two_sessions_concatenates_one_record_per_line(projects, capsys):
    corpus = ""
    for sid in (SID, NESTED_SID):
        rc, out, _ = _run(["ledger", "--json", sid], capsys)
        assert rc == 0
        corpus += out
    recs = [json.loads(ln) for ln in corpus.splitlines()]
    assert {r["session"] for r in recs} == {SID, NESTED_SID}
    assert all(r["counter"] == real_counter().label for r in recs)


# --- the exits ----------------------------------------------------------------------

def test_no_transcript_is_exit_1_naming_the_seat(projects, capsys):
    rc, out, err = _run(["ledger", "ffffffff-none"], capsys)
    assert (rc, out) == (1, "")
    assert "no transcript for 'ffffffff-none'" in err


def test_a_counter_that_cannot_run_is_exit_2_and_loud(projects, capsys, monkeypatch, tmp_path):
    empty = tmp_path / "no-vocab"
    empty.mkdir()
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(empty))
    rc, out, err = _run(["ledger", "--short", SID], capsys)
    assert (rc, out) == (2, "")
    assert err.startswith("session ledger: token counter: no cl100k_base vocabulary")
    assert "run `tokens` once" in err


@pytest.mark.parametrize("argv,why", [
    (["--long", "--width", "40"], "--width applies to --short only"),
    (["--width", "40"], "--width applies to --short only"),
    (["--short", "--width", "0"], "--width must be a positive number"),
    (["--short", "--long"], "not allowed with"),
])
def test_usage_errors_are_exit_2(projects, capsys, argv, why):
    with pytest.raises(SystemExit) as e:
        cli.main(["ledger", *argv, SID])
    assert e.value.code == 2
    assert why in capsys.readouterr().err


@requires_counter
def test_a_reader_that_stops_early_is_not_an_error(projects, capsys, monkeypatch):
    """`session ledger --long | head`: the pipe closes before the view is
    written. No traceback, and exit 0, not exit 1 ("no transcript")."""
    import os
    r, w = os.pipe()
    os.close(r)                     # the reader has gone, as `head` does
    out = os.fdopen(w, "w")
    monkeypatch.setattr("sys.stdout", out)
    try:
        assert cli.main(["ledger", "--long", SID]) == 0
        assert capsys.readouterr().err == ""
    finally:
        out.close()
