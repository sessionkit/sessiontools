"""`session list` names the session that holds the default seat (ST-264).

Measured on the fresh-3 walk (0.3.1, a fresh Mac): a bare `session start`
took the seat and said so, and `session list` then named the one live
session `work-9d`, the name Claude Code wrote in its sidecar, with nothing
saying it was the seat. The sidecar's `agent` is the agent TYPE, so it reads
the seat word for a holder and a non-holder alike; only the holder claim
(`holders.read_claim`, ST-256's per-host singleton) says which session
holds the seat.

Every case here runs `session list` end to end through the CLI, in a temp
HOME with no state-dir override, so the claim is found where a stranger's
would be (`holders.holders_dir`, never a hand-spelled path):

- the HOLDER (claim by session id, and claim by pid before the id is
  recorded) must show the seat word in its table line and its JSON row;
- a second, NON-HOLDER session of the same agent must not;
- a STALE claim (its pid gone) names no holder;
- a PLAIN session (no agent) is listed as before;
- a NAMED agent's holder (its hand launch took the agent's claim at the
  front door) shows that agent's name, and a second session of it does not.

The fix shows the seat BESIDE the name: a SEAT column right after NAME
(`-` when none) and a `seat` key (a word, else null) on every JSON row.
NAME stays the derived name, as `match`, `get`, `reap` and `park` read it.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from sessiontools import cli, listing, procs, tmux
from sessiontools.procs import Proc
from sessiontools.start import default_seat, holders

T0 = procs.parse_ctime("Sun Jun  9 12:34:56 2030")
CWD = "/st264-fresh/work"           # gone on purpose: repo_name falls back to the basename

SEAT = default_seat.word()
HOLDER = ("c240ec8c-56a7-4a6a-a47a-535e77d41898", 7095, "work-9d")
SECOND = ("5d1e0b77-1111-4a6a-a47a-535e77d41898", 7200, "work-3f")
PLAIN = ("9a0c4e21-2222-4a6a-a47a-535e77d41898", 7300, "notes-1a")

@pytest.fixture
def host(tmp_path, monkeypatch):
    """A temp HOME with no SESSION_STATE_DIR: the state dir resolves as a
    stranger's does. Three live sidecars, no tmux panes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SESSION_STATE_DIR", raising=False)
    sidecars = tmp_path / "sidecars"
    sidecars.mkdir()
    monkeypatch.setenv("SESSION_SIDECAR_DIR", str(sidecars))
    listing.repo_name.cache_clear()
    table = {1: Proc(1, 0, T0)}
    for _sid, pid, _name in (HOLDER, SECOND, PLAIN):
        table[pid] = Proc(pid, 1, T0)
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(tmux, "pane_index", dict)
    start = holders.proc_start_of(HOLDER[1], table)

    def sidecar(seat, agent):
        sid, pid, name = seat
        body = {"pid": pid, "sessionId": sid, "name": name, "status": "busy",
                "updatedAt": T0 * 1000, "startedAt": T0 * 1000, "procStart": start,
                "cwd": CWD, "version": "2.1.310"}
        if agent is not None:
            body["agent"] = agent
        (sidecars / f"{pid}.json").write_text(json.dumps(body))

    sidecar(HOLDER, SEAT)
    sidecar(SECOND, SEAT)             # same agent TYPE: `agent` alone cannot tell them apart
    sidecar(PLAIN, None)
    return {"home": home, "table": table, "start": start}


def _claim(host, *, session_id, pid, start=None):
    """The claim as `session start` writes it (`default_seat.claim_fields`'s
    keys), through `holders.write_claim`: the path is the resolver's."""
    path = holders.write_claim(SEAT, {
        "sessionId": session_id, "pid": pid, "procStart": start or host["start"],
        "pane": None, "host": "fresh-3", "claimedAt": "2030-06-09T12:34:56+00:00",
        "via": default_seat.VIA_BARE})
    assert path.is_relative_to(host["home"])
    return path


def _list(capsys, *flags):
    assert cli.main(["list", "--no-context", "--fresh", *flags]) == 0
    return capsys.readouterr().out


def _line(table: str, sid: str) -> str:
    [line] = [ln for ln in table.splitlines() if ln.startswith(sid[:8])]
    return line


def _row(capsys, sid: str) -> dict:
    [row] = [r for r in json.loads(_list(capsys, "--json")) if r["session_id"] == sid]
    return row


def _says_seat(text: str) -> bool:
    """SEAT as a word of its own: `claude`, `(claude)`, `seat=claude` count;
    `claude-x` or `xclaude` do not."""
    return re.search(rf"(?<![\w-]){re.escape(SEAT)}(?![\w-])", text) is not None


def _seat_keys(row: dict) -> set[str]:
    """The JSON keys, other than `agent` (the type, true of any claude
    session), whose value names the seat."""
    return {k for k, v in row.items() if k != "agent" and v == SEAT}


def test_the_seat_word_is_the_strangers_word():
    assert SEAT == "claude"


def test_the_holder_by_session_id_shows_the_seat_word(host, capsys):
    _claim(host, session_id=HOLDER[0], pid=HOLDER[1])
    line = _line(_list(capsys), HOLDER[0])
    assert _says_seat(line), line
    assert _seat_keys(_row(capsys, HOLDER[0]))


def test_the_holder_by_pid_before_its_id_is_recorded_shows_the_seat_word(host, capsys):
    # The bare start writes the claim with no id, then rewrites it with the
    # minted one (`start._record_id`); a pane mint's id is born in the pane.
    _claim(host, session_id=None, pid=HOLDER[1])
    line = _line(_list(capsys), HOLDER[0])
    assert _says_seat(line), line
    assert _seat_keys(_row(capsys, HOLDER[0]))


def test_a_second_session_of_the_same_agent_is_not_the_holder(host, capsys):
    _claim(host, session_id=HOLDER[0], pid=HOLDER[1])
    line = _line(_list(capsys), SECOND[0])
    assert not _says_seat(line), line
    assert line.split()[1] == SECOND[2]
    row = _row(capsys, SECOND[0])
    assert not _seat_keys(row), row
    assert row["name"] == SECOND[2]


def test_a_stale_claim_names_no_holder(host, capsys):
    # The claim names the holder's id but a pid that is not running: a
    # crash-left claim holds nothing (`holders.claim_is_live`).
    _claim(host, session_id=HOLDER[0], pid=9999)
    out = _list(capsys)
    for sid, _pid, _name in (HOLDER, SECOND, PLAIN):
        assert not _says_seat(_line(out, sid))


def test_a_plain_session_is_listed_as_before(host, capsys):
    _claim(host, session_id=HOLDER[0], pid=HOLDER[1])
    line = _line(_list(capsys), PLAIN[0])
    assert line.split()[:2] == [PLAIN[0][:8], PLAIN[2]]
    assert not _says_seat(line), line
    row = _row(capsys, PLAIN[0])
    assert (row["name"], row["agent"]) == (PLAIN[2], None)
    assert not _seat_keys(row), row


# --- beside, not instead: the derived name stays, SEAT and `seat` are added ---

def test_the_holder_keeps_its_derived_name_and_shows_the_seat_beside_it(host, capsys):
    _claim(host, session_id=HOLDER[0], pid=HOLDER[1])
    out = _list(capsys)
    header = out.splitlines()[0].split()
    assert header[:3] == ["SHORT", "NAME", "SEAT"]
    assert _line(out, HOLDER[0]).split()[:3] == [HOLDER[0][:8], HOLDER[2], SEAT]
    row = _row(capsys, HOLDER[0])
    assert (row["name"], row["seat"], row["agent"]) == (HOLDER[2], SEAT, SEAT)
    assert list(row)[3:5] == ["name", "seat"]


def test_every_session_that_holds_no_seat_has_a_dash_and_a_null(host, capsys):
    _claim(host, session_id=HOLDER[0], pid=HOLDER[1])
    out = _list(capsys)
    for sid, _pid, name in (SECOND, PLAIN):
        assert _line(out, sid).split()[:3] == [sid[:8], name, "-"]
    rows = {r["session_id"]: r for r in json.loads(_list(capsys, "--json"))}
    assert {sid: r["seat"] for sid, r in rows.items()} == {
        HOLDER[0]: SEAT, SECOND[0]: None, PLAIN[0]: None}


def test_no_claim_at_all_lists_every_seat_as_null(host, capsys):
    assert not holders.holders_dir().exists()
    rows = json.loads(_list(capsys, "--json"))
    assert [r["seat"] for r in rows] == [None, None, None]
    assert not holders.holders_dir().exists()       # a list creates nothing


def test_a_claim_whose_process_restarted_names_no_holder(host, capsys):
    # The holder's pid, but a start that is not its process's: a reused pid.
    _claim(host, session_id=HOLDER[0], pid=HOLDER[1], start="Sun Dec  8 12:34:56 2030")
    assert _row(capsys, HOLDER[0])["seat"] is None


def test_a_claim_naming_another_id_names_no_one_else(host, capsys):
    # An id is the claim's whole answer: the pid is not tried after it.
    _claim(host, session_id="0badc0de-0000-4a6a-a47a-535e77d41898", pid=HOLDER[1])
    assert [r["seat"] for r in json.loads(_list(capsys, "--json"))] == [None, None, None]


@pytest.mark.parametrize("text", ["{not json", "[]", '{"pid": "7095"}', ""])
def test_a_corrupt_claim_lists_as_before(host, capsys, text):
    _claim(host, session_id=HOLDER[0], pid=HOLDER[1]).write_text(text)
    out = _list(capsys)
    assert _line(out, HOLDER[0]).split()[:3] == [HOLDER[0][:8], HOLDER[2], "-"]


def test_an_unreadable_claim_lists_as_before(host, capsys, monkeypatch):
    _claim(host, session_id=HOLDER[0], pid=HOLDER[1])

    def unreadable(agent, environ=None):
        raise holders.ClaimUnreadable("the claim could not be read (Permission denied)")

    monkeypatch.setattr(holders, "read_claim", unreadable)
    rows = json.loads(_list(capsys, "--json"))
    assert [r["seat"] for r in rows] == [None, None, None]


def test_a_pane_claim_names_the_session_its_shell_runs(host, capsys):
    # The keyed tmux road: the claim names the pane and the pane's shell
    # (it does not exec), with no id yet; the session is the shell's child.
    shell = 7000
    host["table"][shell] = Proc(shell, 1, T0)
    host["table"][HOLDER[1]] = Proc(HOLDER[1], shell, T0)
    _rewrite_tmux(host, HOLDER[1], "sessiontools:@154.%415")
    holders.write_claim(SEAT, {
        "sessionId": None, "pid": shell, "procStart": host["start"], "pane": "%415",
        "host": "fresh-3", "claimedAt": "2030-06-09T12:34:56+00:00",
        "via": default_seat.VIA_TMUX})
    assert _row(capsys, HOLDER[0])["seat"] == SEAT
    assert _row(capsys, SECOND[0])["seat"] is None


def test_a_pane_claim_whose_shell_is_not_the_sessions_parent_names_no_one(host, capsys):
    # The same pane id on another tmux server: the claim's shell runs, but
    # the session is not its child.
    shell = 7000
    host["table"][shell] = Proc(shell, 1, T0)
    _rewrite_tmux(host, HOLDER[1], "other:@1.%415")
    holders.write_claim(SEAT, {
        "sessionId": None, "pid": shell, "procStart": host["start"], "pane": "%415",
        "host": "fresh-3", "claimedAt": "2030-06-09T12:34:56+00:00",
        "via": default_seat.VIA_TMUX})
    assert _row(capsys, HOLDER[0])["seat"] is None


def test_a_served_snapshot_reads_the_claim_now(host, capsys, monkeypatch):
    # The snapshot carries no seat: one made before the claim still serves
    # the seat that holds it now, and one made while it held serves none
    # once the claim is gone.
    monkeypatch.setenv("SESSION_LIST_MAX_AGE", "3600")
    assert cli.main(["list", "--no-context", "--fresh", "--json"]) == 0
    capsys.readouterr()
    path = _claim(host, session_id=HOLDER[0], pid=HOLDER[1])
    assert cli.main(["list", "--no-context", "--json"]) == 0
    served = {r["session_id"]: r["seat"] for r in json.loads(capsys.readouterr().out)}
    assert served[HOLDER[0]] == SEAT
    path.unlink()
    assert cli.main(["list", "--no-context", "--json"]) == 0
    assert all(r["seat"] is None for r in json.loads(capsys.readouterr().out))


def _rewrite_tmux(host, pid, tmux_field):
    """Give the sidecar of PID a `tmux` field, as Claude Code writes it."""
    path = os.path.join(os.environ["SESSION_SIDECAR_DIR"], f"{pid}.json")
    with open(path) as f:
        body = json.load(f)
    body["tmux"] = tmux_field
    with open(path, "w") as f:
        json.dump(body, f)


# --- a named agent's claim: the front door's, beside the default seat's -------
#
# A hand launch of a named agent (no `-n`) takes that agent's claim before it
# execs (`start._door`: `default_seat.claim_fields(..., via=VIA_DOOR)` written
# by `holders.write_claim`), in the same `holders/` as the default seat's. The
# claim says which session holds the agent, and the door refuses a second hand
# launch naming that session, so SEAT names it too. A spawner's `-n` launch of
# the same agent takes no claim and shows `-`.

NAMED = "scout"
SCOUT = ("7a1d3e90-3333-4a6a-a47a-535e77d41898", 7400, "scout-2b")
KICKED = ("8b2e4fa1-4444-4a6a-a47a-535e77d41898", 7500, "scout-kicked")


@pytest.fixture
def named(host):
    """HOST plus two live `scout` sessions: SCOUT holds the agent's claim, as
    its hand launch wrote it; KICKED is a second scout a spawner named."""
    for sid, pid, name in (SCOUT, KICKED):
        host["table"][pid] = Proc(pid, 1, T0)
        body = {"pid": pid, "sessionId": sid, "name": name, "status": "idle",
                "updatedAt": T0 * 1000, "startedAt": T0 * 1000, "procStart": host["start"],
                "cwd": CWD, "version": "2.1.310", "agent": NAMED}
        path = os.path.join(os.environ["SESSION_SIDECAR_DIR"], f"{pid}.json")
        with open(path, "w") as f:
            json.dump(body, f)
    fields = default_seat.claim_fields(SCOUT[0], SCOUT[1], host["table"], {},
                                       via=default_seat.VIA_DOOR)
    path = holders.write_claim(NAMED, fields)
    assert path == holders.claim_paths(NAMED)[1]
    assert path.is_relative_to(host["home"])
    return host


def test_a_named_agents_holder_shows_its_name_in_seat(named, capsys):
    out = _list(capsys)
    assert _line(out, SCOUT[0]).split()[:3] == [SCOUT[0][:8], SCOUT[2], NAMED]
    row = _row(capsys, SCOUT[0])
    assert (row["name"], row["seat"], row["agent"]) == (SCOUT[2], NAMED, NAMED)


def test_a_second_session_of_the_named_agent_shows_a_dash(named, capsys):
    out = _list(capsys)
    assert _line(out, KICKED[0]).split()[:3] == [KICKED[0][:8], KICKED[2], "-"]
    assert _row(capsys, KICKED[0])["seat"] is None


def test_a_named_claim_and_the_default_seats_claim_list_side_by_side(named, capsys):
    _claim(named, session_id=HOLDER[0], pid=HOLDER[1])
    rows = {r["session_id"]: r["seat"] for r in json.loads(_list(capsys, "--json"))}
    assert rows == {HOLDER[0]: SEAT, SECOND[0]: None, PLAIN[0]: None,
                    SCOUT[0]: NAMED, KICKED[0]: None}


def test_a_named_claim_alone_names_no_default_seat_holder(named, capsys):
    rows = {r["session_id"]: r["seat"] for r in json.loads(_list(capsys, "--json"))}
    assert [sid for sid, seat in rows.items() if seat] == [SCOUT[0]]
    assert rows[HOLDER[0]] is None


def test_a_stale_named_claim_names_no_holder(named, capsys):
    # The holder's id, but a pid that is not running: a crash-left claim.
    claim = holders.read_claim(NAMED)
    holders.write_claim(NAMED, {**claim, "pid": 9999})
    rows = {r["session_id"]: r["seat"] for r in json.loads(_list(capsys, "--json"))}
    assert rows[SCOUT[0]] is None
    assert NAMED not in rows.values()
