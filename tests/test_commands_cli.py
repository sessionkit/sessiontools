"""`session commands` and `session list --past`, the verbs (ST-320): their
help, TARGET, the four views, the filters and the exits, end to end.

Every transcript here is SYNTHETIC, written by `_world`: transcripts hold
secrets, so none is copied into a test. `CLAUDE_PROJECTS_DIR` and
`SESSIONS_DIR` point at a temp dir, as the ledger's tests do.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

import pytest

from sessiontools import cli, commands, past
from sessiontools.ledger import transcript

KIN = "00000000-0000-4000-8000-00000000a001"      # named by settings/session.json
RESUMED = "00000000-0000-4000-8000-00000000a002"  # named by its identity block; copies KIN's line
OTHER = "00000000-0000-4000-8000-00000000b001"    # another seat, named by its identity block
NOBODY = "00000000-0000-4000-8000-00000000c001"   # a transcript no record names


def _use(tid, command, ts, side=False):
    return {"type": "assistant", "timestamp": ts, "isSidechain": side,
            "message": {"content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                     "input": {"command": command}}]}}


def _result(tid, ts, side=False, sid=KIN):
    return {"type": "user", "timestamp": ts, "isSidechain": side, "sessionId": sid,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                     "content": "out"}]}}


def _prompt(text, ts):
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


def _write(path, records):
    """Write the transcript and stamp its mtime with its LAST record, as a
    live transcript's is. The stamps are 2030 fixtures, ahead of the clock,
    and `--since` skips a file last written before it (ST-330)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    last = max(datetime.fromisoformat(r["timestamp"]) for r in records)
    os.utime(path, (last.timestamp(), last.timestamp()))


@pytest.fixture
def world(tmp_path, monkeypatch):
    proj = tmp_path / "projects" / "-tmp-proj"
    first = [_prompt("hello", "2030-06-09T03:00:00.000Z"),
             _use("t1", "FOO=1 git status 2>&1 | head -3", "2030-06-09T03:00:01.000Z"),
             _result("t1", "2030-06-09T03:00:02.000Z"),
             _use("t2", "cd /x && bash -c 'make test'", "2030-06-09T04:00:01.000Z"),
             _result("t2", "2030-06-09T04:00:02.000Z"),
             _prompt([{"type": "text", "text": "again"}], "2030-06-09T04:30:00.000Z"),
             _use("t3", "sleep 600", "2030-06-09T05:00:00.000Z")]       # no result: not an item
    _write(proj / f"{KIN}.jsonl", first)
    _write(proj / KIN / "subagents" / "agent-1.jsonl",
           [_use("s1", "rg -n needle src", "2030-06-09T04:10:00.000Z", side=True),
            _result("s1", "2030-06-09T04:10:01.000Z", side=True)])
    _write(proj / f"{RESUMED}.jsonl", first[:3] + [
        _use("t4", "git log --oneline | wc -l", "2030-12-08T09:00:00.000Z"),
        _result("t4", "2030-12-08T09:00:01.000Z", sid=RESUMED)])
    _write(proj / f"{OTHER}.jsonl", [_use("o1", "ls", "2030-06-09T03:00:00.000Z"),
                                     _result("o1", "2030-06-09T03:00:01.000Z", sid=OTHER)])
    _write(proj / f"{NOBODY}.jsonl", [_use("n1", "uv run pytest -q", "2030-06-09T03:00:00.000Z"),
                                      _result("n1", "2030-06-09T03:00:01.000Z", sid=NOBODY)])
    sessions = tmp_path / "sessions"
    (sessions / KIN / "settings").mkdir(parents=True)
    (sessions / KIN / "settings" / "session.json").write_text('{"agent": "kin"}')
    (sessions / RESUMED).mkdir()
    (sessions / RESUMED / "identity.md").write_text("# Who you are\n\n- **Seat**: kin\n")
    (sessions / OTHER).mkdir()
    (sessions / OTHER / "identity.md").write_text("# Who you are\n\n- **Seat**: other\n")
    (sessions / NOBODY).mkdir()
    (sessions / NOBODY / "identity.md").write_text("# Who you are\n\n- **Seat**: none (no seat)\n")
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("SESSIONS_DIR", str(sessions))
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _run(argv, capsys):
    rc = cli.main(argv)
    out, err = capsys.readouterr()
    return rc, out, err


def test_the_verb_is_registered_with_its_parser():
    assert "commands" in dict(cli.VERBS)
    assert cli.HANDLERS["commands"] is cli._commands
    assert cli.PARSERS["commands"] == "sessiontools.commands.cli:build_parser"


@pytest.mark.parametrize("argv", [["commands", "--help"], ["commands", "-h"]])
def test_help_is_the_whole_page_on_stdout_exit_0(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    out, err = capsys.readouterr()
    assert e.value.code == 0 and err == ""
    assert "--count" in out and "TARGET" in out and "WHEN" in out
    assert max(len(line) for line in out.splitlines()) <= 76


def test_a_seat_is_every_session_recorded_as_it_and_a_call_is_listed_once(world, capsys):
    rc, out, err = _run(["commands", "kin"], capsys)
    lines = out.splitlines()
    assert rc == 0
    assert [l.split(" ", 3)[3].strip() for l in lines] == [
        "git status ; head -3", "[bash -c] make test", "rg -n needle src",
        "git log --oneline ; wc -l"]          # t1 once, though two transcripts hold it
    assert lines[2].split()[2] == KIN[:8] + "*"          # the subagent's call is marked
    assert err == "4 of 4 Bash calls, 2 sessions of kin\n"


def test_a_session_id_prefix_is_that_session_whatever_names_it(world, capsys):
    rc, out, err = _run(["commands", NOBODY[:33]], capsys)
    assert rc == 0 and out.endswith("[uv run] pytest -q\n")
    assert err == "1 of 1 Bash calls, 1 session of ?\n"


def test_count_is_one_line_per_program_and_verb_most_frequent_first(world, capsys):
    rc, out, err = _run(["commands", "kin", "--count"], capsys)
    assert rc == 0
    assert out.splitlines()[0].split() == ["2", "git"] or out.splitlines()[:2] == [
        "      1  git status", "      1  head"]
    assert {" ".join(l.split()[1:]) for l in out.splitlines()} == {
        "git status", "head", "make test", "rg", "git log", "wc"}
    assert err == "6 programs in 4 of 4 Bash calls, 2 sessions of kin\n"


def test_grep_keeps_the_programs_it_matches(world, capsys):
    _rc, out, err = _run(["commands", "kin", "--count", "--grep", "^git "], capsys)
    assert sorted(" ".join(l.split()[1:]) for l in out.splitlines()) == ["git log", "git status"]
    assert err == "2 programs in 2 of 4 Bash calls, 2 sessions of kin\n"
    _rc, out, err = _run(["commands", "kin", "--grep", "^make"], capsys)
    assert out.count("\n") == 1 and "make test" in out


def test_raw_is_the_command_as_written(world, capsys):
    rc, out, _err = _run(["commands", KIN, "--raw", "--grep", "status"], capsys)
    assert rc == 0 and out.splitlines()[1] == "FOO=1 git status 2>&1 | head -3"
    assert out.splitlines()[0].startswith("--- ")


def test_json_is_one_record_per_program(world, capsys):
    _rc, out, _err = _run(["commands", "kin", "--json", "--until", "2030-12-08"], capsys)
    recs = [json.loads(l) for l in out.splitlines()]
    assert [(r["program"], r["verb"], r["via"]) for r in recs] == [
        ("git", "status", ""), ("head", "", ""), ("make", "test", "bash -c"), ("rg", "", "")]
    assert recs[0] == {"seat": "kin", "session": KIN, "ts": "2030-06-09T03:00:02.000Z",
                       "program": "git", "verb": "status", "via": "",
                       "words": ["git", "status"], "call": "t1", "subagent": False}
    assert recs[3]["subagent"] is True


def test_since_and_until_bound_the_calls(world, capsys):
    _rc, out, err = _run(["commands", "kin", "--since", "2030-12-08T00:00+00:00"], capsys)
    assert out.count("\n") == 1 and "git log" in out
    assert err == "1 of 1 Bash calls, 2 sessions of kin\n"


def test_a_call_half_a_second_after_when_is_after_it(world, capsys):
    # a transcript's stamps carry milliseconds (the egg review, N1)
    proj = world / "projects" / "-tmp-proj"
    _write(proj / f"{OTHER}.jsonl", [_use("o2", "ls", "2030-06-09T05:00:00.500Z"),
                                     _result("o2", "2030-06-09T05:00:00.700Z", sid=OTHER)])
    rc, out, _err = _run(["commands", OTHER, "--since", "2030-06-09T05:00:00+00:00"], capsys)
    assert rc == 0 and out.count("\n") == 1
    rc, out, _err = _run(["commands", OTHER, "--until", "2030-06-09T05:00:00+00:00"], capsys)
    assert out == ""


def test_exit_1_when_nothing_matches_target(world, capsys):
    rc, out, err = _run(["commands", "nosuchseat"], capsys)
    assert (rc, out) == (1, "") and "no seat and no session matches 'nosuchseat'" in err


@pytest.mark.parametrize("argv", [["commands", "kin", "--grep", "("],
                                  ["commands", "kin", "--since", "yesterday-ish"],
                                  ["commands", "kin", "--width", "-1"],
                                  ["commands", "kin", "--count", "--raw"],
                                  ["commands"]])
def test_exit_2_when_the_line_is_wrong(world, argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
    assert capsys.readouterr().err.startswith("usage: session commands")


def test_list_past_is_one_line_a_session_oldest_first(world, capsys):
    rc, out, _err = _run(["list", "--past", "kin"], capsys)
    rows = [l.split() for l in out.splitlines()]
    assert rc == 0 and rows[0] == ["ID", "START", "LAST", "WRITE", "TURNS"]
    assert [(r[0], r[-1]) for r in rows[1:]] == [(KIN[:8], "2"), (RESUMED[:8], "1")]


def test_list_past_json_and_its_exit_1(world, capsys):
    rc, out, err = _run(["list", "--past", "other", "--json"], capsys)
    (row,) = json.loads(out)
    assert (row["id"], row["seat"], row["turns"]) == (OTHER, "other", 0)
    assert row["start"] == 1907204400.0 and len(row["transcripts"]) == 1
    rc, out, err = _run(["list", "--past", "nosuchseat"], capsys)
    assert (rc, out) == (1, "") and "no session is recorded as seat 'nosuchseat'" in err


def test_which_record_names_the_seat(world):
    assert past.seat_of(KIN) == "kin"          # settings/session.json
    assert past.seat_of(RESUMED) == "kin"      # the identity block
    assert past.seat_of(OTHER) == "other"
    assert past.seat_of(NOBODY) is None        # "none (no seat)" names nobody
    (world / "sessions" / NOBODY / "birth-cert.txt").write_text(
        f"session-birth-v1\nsession-id: {NOBODY}\nagent: kin\n")
    assert past.seat_of(NOBODY) is None        # sessionmail's file is never read
    (world / "sessions" / RESUMED / "settings").mkdir()
    (world / "sessions" / RESUMED / "settings" / "session.json").write_text('{"agent": "x"}')
    assert past.seat_of(RESUMED) == "x"        # sessiontools' own record wins


def test_the_ledger_reader_reads_a_subagent_file_only_when_asked(world):
    sub = world / "projects" / "-tmp-proj" / KIN / "subagents" / "agent-1.jsonl"
    assert transcript.read(sub).raws == []
    assert [r.cmd for r in transcript.read(sub, sidechain=True).raws] == ["rg -n needle src"]
    main = world / "projects" / "-tmp-proj" / f"{KIN}.jsonl"
    assert [r.cmd for r in transcript.read(main).raws if r.kind == "Bash"] == [
        "FOO=1 git status 2>&1 | head -3", "cd /x && bash -c 'make test'"]


def test_when_reads_its_four_spellings():
    assert commands.utc("2030-12-08T00:00+00:00") == "2030-12-08T00:00:00.000Z"
    for when in ("7d", "48h", "30m", "2030-12-08"):
        assert commands.utc(when).endswith("Z")
    with pytest.raises(ValueError):
        commands.utc("soon")
