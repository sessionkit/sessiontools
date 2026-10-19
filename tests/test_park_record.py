"""`session park`: the record and the transcript (park.py).

The record is contract v1, written atomically and alone. The transcript says
when turns started and which tool calls were work. `work_ended` must never
let a park buy time, and `unparked` must name what ended a park: a turn that
started after it, or work done after it. Every transcript here is a fixture
in the shapes Claude Code writes, measured from live transcripts.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ANCHOR_SUMMER

from sessiontools import park
from sessiontools.claude_code import Session

SID = "5e1f0c2a-0000-4000-8000-0000000000aa"
T0 = ANCHOR_SUMMER.timestamp()   # the session's start, in these fixtures


def iso(at: float) -> str:
    """AT as Claude Code writes `timestamp`: UTC, milliseconds, a Z."""
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --- transcript records, in Claude Code's shapes ---------------------------------

def human(at: float, text: str = "do the thing") -> dict:
    return {"type": "user", "timestamp": iso(at), "origin": {"kind": "human"},
            "message": {"role": "user", "content": text}}


def page(at: float, tool_use_id: str | None = None) -> dict:
    """A pager page, or (with TOOL_USE_ID) a background call's completion."""
    body = "<task-notification>\n<task-id>b1</task-id>\n"
    if tool_use_id:
        body += f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
    body += "<summary>Monitor event</summary>\n</task-notification>"
    return {"type": "user", "timestamp": iso(at),
            "origin": {"kind": "task-notification", "producer": "session-task"},
            "message": {"role": "user", "content": body}}


def compaction(at: float) -> dict:
    return {"type": "user", "timestamp": iso(at),
            "message": {"role": "user",
                        "content": "This session is being continued from a previous conversation."}}


def meta(at: float, text: str = "Stop hook feedback: arm your listener") -> dict:
    return {"type": "user", "timestamp": iso(at), "isMeta": True,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def use(at: float, call_id: str, name: str = "Bash", **tool_input) -> dict:
    return {"type": "assistant", "timestamp": iso(at),
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": call_id, "name": name,
                                     "input": tool_input}]}}


def result(at: float, call_id: str) -> dict:
    return {"type": "user", "timestamp": iso(at),
            "message": {"role": "user", "content": [{"type": "tool_result",
                                                     "tool_use_id": call_id, "content": "ok"}]}}


def bash(at: float, call_id: str, command: str, took: float = 1.0, **extra) -> list[dict]:
    return [use(at, call_id, "Bash", command=command, **extra), result(at + took, call_id)]


def text_reply(at: float) -> dict:
    return {"type": "assistant", "timestamp": iso(at),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Noted."}]}}


def facts(*records) -> park.Facts:
    lines = []
    for r in records:
        lines.extend(json.dumps(x) for x in (r if isinstance(r, list) else [r]))
    return park.transcript_facts(lines)


def checks(word: str = park.PASS) -> tuple[park.Check, ...]:
    return tuple(park.Check(n, word, "fixture") for n in park.CHECK_NAMES)


def record(parked_at: float, ended: float | None = None, **kw) -> park.Record:
    return park.Record(SID, parked_at, ended, "fixture", kw.pop("checks", checks()), **kw)


# --- the record ------------------------------------------------------------------

@pytest.fixture
def seat_dir(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "sessions"
    (root / SID).mkdir(parents=True)
    monkeypatch.setenv("SESSIONS_DIR", str(root))
    return root / SID


def test_write_then_read_is_the_same_record(seat_dir):
    r = record(T0 + 100, T0 + 90.25, letter="<a@b>", keep_until=T0 + 9000, keep_why="a verdict",
               promises={"declared": "none", "open": [], "handed_on": None})
    path = park.write(r)
    assert path == seat_dir / "park.json"
    back, why = park.read(path, SID)
    assert why == "" and back == r
    data = json.loads(path.read_text())
    assert data["v"] == 1 and data["clean"] is True
    assert "deadline" not in json.dumps(data)


def test_write_is_0600_and_writes_nothing_else(seat_dir):
    before = sorted(p.name for p in seat_dir.iterdir())
    park.write(record(T0 + 100, T0))
    after = sorted(p.name for p in seat_dir.iterdir())
    assert after == sorted([*before, "park.json"])
    assert stat.S_IMODE((seat_dir / "park.json").stat().st_mode) == 0o600


def test_write_needs_the_session_dir_and_never_makes_one(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "sessions"))
    with pytest.raises(park.NoSessionDir):
        park.write(record(T0 + 100, T0))
    assert not (tmp_path / "sessions").exists()


def test_a_failed_rename_leaves_no_record_and_no_temp_file(seat_dir, monkeypatch):
    def refuse(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        park.write(record(T0 + 100, T0))
    assert list(seat_dir.iterdir()) == []


def test_a_failed_rename_leaves_the_old_record_as_it_was(seat_dir, monkeypatch):
    path = park.write(record(T0 + 100, T0))
    old = path.read_bytes()
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(OSError):
        park.write(record(T0 + 200, T0 + 150))
    assert path.read_bytes() == old
    assert [p.name for p in seat_dir.iterdir()] == ["park.json"]


def test_clean_is_every_check_pass_and_unknown_is_never_pass():
    assert record(T0, T0).clean
    mixed = checks()[:-1] + (park.Check("promises", park.UNKNOWN, "not declared"),)
    assert not record(T0, T0, checks=mixed).clean


def test_no_file_reads_never_parked(seat_dir):
    assert park.read(seat_dir / "park.json", SID) == (None, "never parked")


def _good() -> dict:
    return record(T0 + 100, T0 + 50).to_dict()


@pytest.mark.parametrize("change, why", [
    (lambda d: "not json", "not JSON"),
    (lambda d: [d], "not a JSON object"),
    (lambda d: {**d, "v": 2}, "not contract v1"),
    (lambda d: {**d, "v": True}, "not contract v1"),
    (lambda d: {**d, "session_id": "someone-else"}, "names another session"),
    (lambda d: {**d, "parked_at": "soon"}, "parked_at is not a time"),
    (lambda d: {**d, "parked_at": float("nan")}, "parked_at is not a time"),
    (lambda d: {**d, "work_ended_at": "earlier"}, "work_ended_at is not a time"),
    (lambda d: {**d, "work_ended_at": T0 + 101}, "work_ended_at is after parked_at"),
    (lambda d: {**d, "checks": d["checks"][:4]}, "checks is not a list of 5"),
    (lambda d: {**d, "checks": d["checks"][::-1]}, "check 'tree' is not a check"),
    (lambda d: {**d, "checks": [{**c, "word": "MAYBE"} for c in d["checks"]]}, "is not a check"),
    (lambda d: {**d, "clean": False}, "clean does not agree"),
    (lambda d: {**d, "keep_until": T0 + 5000}, "keep_until has no keep_why"),
    (lambda d: {**d, "promises": None}, "promises is not an object"),
])
def test_anything_less_than_a_whole_v1_record_is_not_a_record(seat_dir, change, why):
    data = change(_good())
    path = seat_dir / "park.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    got, reason = park.read(path, SID)
    assert got is None
    assert reason.startswith("park.json unreadable: ") and why in reason


def test_a_null_work_ended_at_is_a_record(seat_dir):
    path = park.write(record(T0 + 100, None))
    got, _ = park.read(path, SID)
    assert got is not None and got.work_ended_at is None


# --- what is work ----------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "session park --letter x --no-promises",
    "session park --check",
    "/opt/kit/bin/session park --check --json",
    "session get idle",
    "session list --json",
    "message list --unread",
    "message read 3",
    "message mark 3 read",
    "pager status",
    "pager listen --timeout 1200",
    "message read 3 && message mark 3 read && session park --no-promises",
    "session park --no-promises; session park --check",
])
def test_reading_and_recording_your_own_state_is_not_work(command):
    assert park.is_not_work(command)


@pytest.mark.parametrize("command", [
    "git commit -m done",
    "cd worktree && session park",
    "env FOO=1 message read 3",
    "message read $(make deploy)",
    "message read `make deploy`",
    "message list > /tmp/x",
    "message send someone -s hi -f body.md",
    "session start worker",
    "",
    None,
])
def test_anything_else_is_work(command):
    assert not park.is_not_work(command)


# design §10 test 13, the transcript half: the idle timer, both ways.

@pytest.mark.parametrize("command", [
    "tm task standdown T-1 --idle-wait",
    "taskmaster task standdown T-1 --idle-wait",
    "/usr/local/bin/tm task standdown T-1 --idle-wait",
    "tm task standdown --idle-wait T-1",
])
def test_the_idle_timer_is_not_work(command):
    assert park.is_not_work(command)
    assert park.is_idle_timer(park.command_words(command))


@pytest.mark.parametrize("command", [
    "tm task standdown T-1 --free",
    "tm task standdown T-1 --check --role worker",
    "tm task standdown T-1",
    "tm task wait T-1",
    "tm slice list",
    "echo tm task standdown --idle-wait",
    "grep --idle-wait log",
    "tm task --idle-wait standdown",
    "tm task standdown T-1 --idle-waiting",
    "tm task standdown T-1 --idle-wait && make test",
])
def test_the_same_verb_without_the_flag_or_any_other_verb_is_work(command):
    assert not park.is_not_work(command)


def test_a_call_that_is_not_bash_is_work():
    f = facts(use(T0 + 1, "r1", "Read", file_path="/x"), result(T0 + 2, "r1"))
    assert f.calls["r1"].work


# --- the transcript --------------------------------------------------------------

def test_turn_starts_are_prompts_pages_and_compactions_only():
    f = facts(human(T0 + 1), bash(T0 + 2, "b1", "git status"), meta(T0 + 4),
              page(T0 + 10), compaction(T0 + 20))
    assert [(t.at, t.origin) for t in f.turns] == [
        (pytest.approx(T0 + 1), "human"), (pytest.approx(T0 + 10), "task-notification"),
        (pytest.approx(T0 + 20), "no origin")]


def test_lines_that_are_not_records_are_passed_over():
    lines = ["not json", "[]", json.dumps({"type": "user"}),
             json.dumps({**human(T0 + 5), "isSidechain": True}), json.dumps(human(T0 + 9))]
    f = park.transcript_facts(lines)
    assert [t.at for t in f.turns] == [pytest.approx(T0 + 9)]
    assert f.first_at == pytest.approx(T0 + 9)


def test_a_background_agent_ends_at_its_completion():
    f = facts(use(T0 + 1, "a1", "Agent", prompt="go", run_in_background=True),
              result(T0 + 2, "a1"), page(T0 + 300, "a1"))
    call = f.calls["a1"]
    assert call.background and call.end == pytest.approx(T0 + 300)
    assert f.open_background() == []


def test_a_background_agent_with_no_completion_is_open():
    f = facts(use(T0 + 1, "a1", "Agent", prompt="go", run_in_background=True), result(T0 + 2, "a1"))
    assert [c.id for c in f.open_background()] == ["a1"]


def test_a_background_shell_is_left_to_the_process_table():
    f = facts(bash(T0 + 1, "b1", "make test", run_in_background=True))
    assert f.open_background() == []


def test_read_transcript_finds_it_and_misses_quietly(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    assert park.read_transcript(SID, "/seat/cwd") is None
    (projects / "-seat-cwd").mkdir(parents=True)
    (projects / "-seat-cwd" / f"{SID}.jsonl").write_text(json.dumps(human(T0 + 1)) + "\n")
    got = park.read_transcript(SID, "/seat/cwd")
    assert got is not None and len(got.turns) == 1


# --- work_ended_at: parking buys no time (design §10 test 5) ---------------------

def settle(t: float) -> list:
    """A worker's settle turn: commit, FREE, then park at t+20."""
    return [human(t), *bash(t + 1, "c1", "git commit -m done", took=2),
            *bash(t + 5, "f1", "tm task standdown T-1 --free", took=3),
            *bash(t + 19, "p1", "session park --no-promises", took=2)]


def test_a_settle_turn_ends_work_at_its_last_work_call():
    f = facts(*settle(T0))
    at, why = park.work_ended(f, now=T0 + 20)
    assert at == pytest.approx(T0 + 8)          # the --free call's result
    assert why == "last work: Bash tm task standdown T-1 --free"


def test_re_parking_with_no_work_between_carries_the_identical_value():
    first, _ = park.work_ended(facts(*settle(T0)), now=T0 + 20)
    prior = record(T0 + 20, first)
    later = facts(*settle(T0),
                  page(T0 + 900),                               # mail wakes the seat
                  *bash(T0 + 901, "l1", "message list --unread"),
                  *bash(T0 + 903, "r1", "message read 4"),
                  *bash(T0 + 905, "m1", "message mark 4 read"),
                  text_reply(T0 + 906),
                  *bash(T0 + 907, "i1", "tm task standdown T-1 --idle-wait"),
                  *bash(T0 + 908, "p2", "session park --no-promises"))
    at, why = park.work_ended(later, now=T0 + 909, prior=prior)
    assert at == first and why.startswith("carried from the park at ")
    assert json.dumps(at) == json.dumps(first)


def test_a_text_only_turn_is_not_work():
    prior = record(T0 + 20, T0 + 8)
    f = facts(*settle(T0), human(T0 + 600, "are you there?"), text_reply(T0 + 601))
    assert park.work_ended(f, now=T0 + 700, prior=prior)[0] == T0 + 8


def test_work_between_parks_moves_it_to_that_work():
    prior = record(T0 + 20, T0 + 8)
    f = facts(*settle(T0), page(T0 + 900), *bash(T0 + 901, "w1", "git push", took=4),
              *bash(T0 + 910, "p2", "session park --no-promises"))
    at, why = park.work_ended(f, now=T0 + 911, prior=prior)
    assert at == pytest.approx(T0 + 905) and why == "last work: Bash git push"


def test_a_background_work_call_ends_at_its_completion():
    f = facts(human(T0), *bash(T0 + 1, "b1", "make test", run_in_background=True),
              page(T0 + 400, "b1"), *bash(T0 + 401, "p1", "session park"))
    assert park.work_ended(f, now=T0 + 402)[0] == pytest.approx(T0 + 400)


def test_no_work_yet_is_the_session_start():
    f = facts(human(T0 + 3), *bash(T0 + 4, "p1", "session park"))
    assert park.work_ended(f, now=T0 + 10, started_at=T0) == (T0, "no work yet: the session's start")
    assert park.work_ended(f, now=T0 + 10)[0] == pytest.approx(T0 + 3)


def test_work_after_now_is_not_counted():
    f = facts(human(T0), *bash(T0 + 1, "w1", "git commit", took=1), *bash(T0 + 50, "w2", "make"))
    assert park.work_ended(f, now=T0 + 10)[0] == pytest.approx(T0 + 2)


def test_no_transcript_carries_or_says_so():
    assert park.work_ended(None, now=T0, prior=record(T0 - 5, T0 - 60)) == (
        T0 - 60, "transcript unreadable: carried")
    assert park.work_ended(None, now=T0) == (None, "transcript unreadable")


# --- unparked (design §10 test 4) ------------------------------------------------

PARKED = T0 + 20.5     # inside p1's call: after its tool_use, before its result


def test_the_park_turn_itself_does_not_unpark():
    f = facts(*settle(T0), meta(T0 + 22), *bash(T0 + 23, "k1", "session park --check"),
              *bash(T0 + 25, "i1", "tm task standdown T-1 --idle-wait"))
    assert park.unparked(record(PARKED, T0 + 8), f) is None


@pytest.mark.parametrize("start, origin", [
    (human, "human"), (page, "task-notification"), (compaction, "no origin")])
def test_a_turn_that_starts_after_the_park_unparks_and_is_named(start, origin):
    f = facts(*settle(T0), start(T0 + 600))
    got = park.unparked(record(PARKED, T0 + 8), f)
    assert got is not None and got.kind == "turn" and got.origin == origin
    assert got.at == pytest.approx(T0 + 600)
    assert f"({origin})" in got.why and "after the park at " in got.why


def test_work_after_a_stop_hook_continuation_unparks_and_names_the_call():
    f = facts(*settle(T0), meta(T0 + 30), *bash(T0 + 31, "w1", "git push origin x"))
    got = park.unparked(record(PARKED, T0 + 8), f)
    assert got is not None and got.kind == "work"
    assert got.why.endswith(": Bash git push origin x")


def test_a_listener_after_a_stop_hook_continuation_does_not_unpark():
    f = facts(*settle(T0), meta(T0 + 30), *bash(T0 + 31, "l1", "pager listen --timeout 1200",
                                                run_in_background=True))
    assert park.unparked(record(PARKED, T0 + 8), f) is None


def test_work_running_when_the_park_was_made_unparks_when_it_ends():
    lines = [*settle(T0)[:-2],
             {"type": "assistant", "timestamp": iso(T0 + 19), "message": {"content": [
                 {"type": "tool_use", "id": "p1", "name": "Bash",
                  "input": {"command": "session park"}},
                 {"type": "tool_use", "id": "w9", "name": "Bash", "input": {"command": "make test"}}]}},
             result(T0 + 21, "p1"), result(T0 + 90, "w9")]
    got = park.unparked(record(PARKED, T0 + 8), facts(*lines))
    assert got is not None and got.kind == "work" and got.at == pytest.approx(T0 + 90)


def test_the_earliest_thing_after_the_park_is_named():
    f = facts(*settle(T0), meta(T0 + 30), *bash(T0 + 31, "w1", "git push"), human(T0 + 600))
    assert park.unparked(record(PARKED, T0 + 8), f).kind == "work"


def _seat(status: str, updated: float) -> Session:
    return Session(id=SID, harness="claude-code", busy=status == "busy", updated_at=updated,
                   raw_status=status)


def test_a_busy_sidecar_written_after_the_park_unparks():
    f = facts(*settle(T0))
    r = record(PARKED, T0 + 8)
    got = park.unparked(r, f, _seat("busy", T0 + 700))
    assert got is not None and got.kind == "busy"
    assert park.unparked(r, f, _seat("busy", T0)) is None         # the park turn's own busy
    assert park.unparked(r, f, _seat("idle", T0 + 700)) is None   # the park turn ending


def test_no_transcript_is_never_parked():
    got = park.unparked(record(PARKED, T0 + 8), None)
    assert got is not None and got.kind == "transcript"


# --- the park's own call, chained (design-001 rule: the park turn is not work) ---------

@pytest.mark.parametrize("command", [
    "cd /x && session park --letter y --no-promises",
    "git add a.py && git commit -m done && session park --no-promises",
    "session park --no-promises && pager listen --timeout 1200",
    "session park --no-promises; session park --check",
])
def test_work_chained_before_the_park_never_unparks_it_or_counts(command):
    lines = [*settle(T0)[:-2], *bash(T0 + 19, "p1", command, took=2)]
    f = facts(*lines)
    assert f.calls["p1"].parks and not f.calls["p1"].work_after_park
    assert park.unparked(record(PARKED, T0 + 8), f) is None
    at, why = park.work_ended(f, now=PARKED)
    # what it chains before the park is dropped: an earlier value, never a later one
    assert at == pytest.approx(T0 + 8) and why.endswith("tm task standdown T-1 --free")


@pytest.mark.parametrize("command, named", [
    ("session park --no-promises && tm task standdown T-1 --free",
     "Bash tm task standdown T-1 --free"),
    ("cd /x && session park --no-promises; git push", "Bash git push"),
    ('session park --no-promises; echo "exit $?"', 'Bash echo "exit $?"'),
    ("session park --no-promises 2>&1 | tail -3", "Bash session park --no-promises 2>&1"),
])
def test_work_chained_after_the_park_unparks_it_and_buys_no_time(command, named):
    # design §4.4: "a park made before --free is unparked by it", chained or not
    lines = [*settle(T0)[:-2], *bash(T0 + 19, "p1", command, took=2)]
    f = facts(*lines)
    assert f.calls["p1"].parks and f.calls["p1"].work_after_park
    got = park.unparked(record(PARKED, T0 + 8), f)
    assert got is not None and got.kind == "work" and got.at == pytest.approx(T0 + 21)
    assert got.why.endswith(f": {named}")
    at, _ = park.work_ended(f, now=PARKED)
    assert at == pytest.approx(T0 + 8)


def test_a_chained_re_park_with_no_work_between_carries_the_identical_value():
    chained = 'session park --no-promises; echo "exit $?"'
    first = facts(*settle(T0)[:-2], *bash(T0 + 19, "p1", chained, took=2))
    at1, _ = park.work_ended(first, now=PARKED)
    prior = record(PARKED, at1)
    again = facts(*settle(T0)[:-2], *bash(T0 + 19, "p1", chained, took=2),
                  human(T0 + 600), *bash(T0 + 601, "m1", "message read 4"),
                  *bash(T0 + 602, "p2", chained, took=2))
    at2, why = park.work_ended(again, now=T0 + 603, prior=prior)
    assert at2 == at1 and why.startswith("carried from the park at ")


def test_a_check_is_not_a_park_and_chained_after_the_park_is_work():
    assert not park.runs_park("session park --check --json")
    f = facts(*settle(T0), *bash(T0 + 30, "k1", "session park --check 2>&1"))
    got = park.unparked(record(PARKED, T0 + 8), f)
    assert got is not None and got.kind == "work"


def test_a_later_chained_park_still_unparks_the_earlier_record():
    f = facts(*settle(T0), meta(T0 + 30),
              *bash(T0 + 31, "p2", 'session park --no-promises; echo "exit $?"'))
    got = park.unparked(record(PARKED, T0 + 8), f)
    assert got is not None and got.kind == "work"


# --- input queued mid-turn (measured shape: an attachment, not a user record) -----------

def queued(at: float, tool_use_id: str | None = None) -> dict:
    """A page or a background completion that arrived while a turn ran."""
    body = "<task-notification>\n<task-id>b1</task-id>\n"
    if tool_use_id:
        body += f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
    body += "</task-notification>"
    return {"type": "attachment", "timestamp": iso(at), "isSidechain": False,
            "attachment": {"type": "queued_command", "prompt": body,
                           "commandMode": "task-notification",
                           "origin": {"kind": "task-notification", "producer": "session-task"}}}


def test_a_page_queued_during_the_park_turn_unparks():
    f = facts(*settle(T0), queued(T0 + 30))
    got = park.unparked(record(PARKED, T0 + 8), f)
    assert got is not None and got.kind == "turn" and got.origin == "queued task-notification"


def test_a_page_queued_before_the_park_does_not_unpark():
    f = facts(human(T0), queued(T0 + 3), *settle(T0)[1:])
    assert park.unparked(record(PARKED, T0 + 8), f) is None


def test_a_background_agent_completed_mid_turn_is_not_open():
    f = facts(use(T0 + 1, "a1", "Agent", prompt="x", run_in_background=True), result(T0 + 2, "a1"),
              queued(T0 + 40, "a1"))
    assert f.open_background() == []
    assert f.calls["a1"].end == pytest.approx(T0 + 40)


def test_other_attachments_are_not_input():
    hook = {"type": "attachment", "timestamp": iso(T0 + 30),
            "attachment": {"type": "hook_additional_context", "content": ["[mail] x"]}}
    assert facts(hook).turns == []


@pytest.mark.parametrize("command, tail", [
    ("session park", "session park"),
    ("cd /x && session park --no-promises; echo ok", "session park --no-promises; echo ok"),
    ("session park; session park --letter y", "session park --letter y"),
    ("session park --check", None),
    ("git status", None),
    (None, None),
])
def test_park_tail_is_the_last_park_and_what_follows_it(command, tail):
    got = park.park_tail(command)
    assert (got.strip() if got else got) == tail


def test_the_and_of_a_redirection_is_not_a_separator():
    assert park.park_tail("session park 2>&1 | tail -3") == "session park 2>&1 | tail -3"
    assert park.park_tail("make test &> log; session park") == " session park"
    assert park.runs_park("make test & session park")


def test_an_older_chained_park_does_not_unpark_a_newer_record():
    f = facts(*settle(T0)[:-2], *bash(T0 + 19, "p1", 'session park; echo "exit $?"', took=2),
              human(T0 + 600), *bash(T0 + 601, "p2", "session park", took=2))
    assert park.unparked(record(T0 + 602, T0 + 8), f) is None
