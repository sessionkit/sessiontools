"""The pane signal's publisher, in-process (ST-134 do-002, design.md §5).

Every outside call is a double that counts: the `ps` table, the server
list, the pane index, the option read and the batched write. No tmux and no
`ps` run here; tests/test_pane_state_tmux.py drives a private tmux server.
The sidecar folder is conftest's scratch one. SESSIONTOOLS_HOOK_TURN is
scrubbed for every test.
"""

from __future__ import annotations

import io
import json
import os
import threading

import pytest
from settings_support import host

from sessiontools import claude_code as cc
from sessiontools import pane_state, procs, tmux, turn
from sessiontools.pane_state import STATE_KEY, WHY_KEY, Publisher, Transition
from sessiontools.procs import Proc

START = "Sun Dec  8 12:34:56 2030"
T0 = procs.parse_ctime(START)
PY = "/opt/homebrew/bin/python3"
MONITOR = f"{PY} /Users/alice/.local/bin/pager listen --monitor"


def tool_shell(command: str) -> str:
    return f"/opt/homebrew/bin/bash -c source /x/shell-snapshots/s.sh && eval '{command}' < /dev/null"


@pytest.fixture(autouse=True)
def _hook_off(monkeypatch):
    monkeypatch.delenv(turn.SWITCH_ENV, raising=False)


class World:
    """Seats, their processes and the panes they sit in, all doubles."""

    def __init__(self):
        self.table: dict[int, Proc] = {1: Proc(1, 0, T0, "/sbin/launchd")}
        self.panes: dict[int, str] = {}            # pane_pid -> pane id, server "s"
        self.found: dict[str, str] = {}            # what the panes held at start
        self.writes: list[list[tuple[str, str, str | None]]] = []
        self.fail: set[str] = set()                # panes whose write fails
        self.now = 1000.0
        self.counts = {"ps": 0, "panes": 0, "read": 0, "write": 0}
        self._next = 100

    # -- doubles --
    def ps(self):
        self.counts["ps"] += 1
        return dict(self.table)

    def index(self, sock):
        self.counts["panes"] += 1
        return dict(self.panes)

    def read_option(self, sock, key):
        self.counts["read"] += 1
        return dict(self.found)

    def write(self, sock, items):
        self.counts["write"] += 1
        self.writes.append(list(items))
        return not any(pane in self.fail for pane, _k, _v in items)

    def publisher(self, **kw) -> Publisher:
        return Publisher(clock=lambda: self.now, ps=self.ps, sockets=lambda: ["s"],
                         panes=self.index, read_option=self.read_option, write=self.write, **kw)

    # -- building --
    def pid(self) -> int:
        self._next += 1
        return self._next

    def add(self, command, parent) -> int:
        pid = self.pid()
        self.table[pid] = Proc(pid, parent, T0 + 60, command)
        return pid

    def seat(self, pane: str, status: str = "shell", work: str | None = None,
             tmux_field: str | None = "auto") -> int:
        pid = self.pid()
        self.table[pid] = Proc(pid, 1, T0, f"claude --session-id {pid}")
        self.add(MONITOR, pid)
        if work:
            sh = self.add(tool_shell(work), pid)
            self.add(work, sh)
        self.panes[pid] = pane
        self.sidecar(pid, status, tmux_field=f"x:@1.{pane}" if tmux_field == "auto" else tmux_field)
        return pid

    def sidecar(self, pid: int, status: str, tmux_field: str | None = None) -> None:
        data = {"pid": pid, "sessionId": f"0000{pid:04d}-0000-4000-8000-000000000134",
                "status": status, "updatedAt": (T0 + 120) * 1000, "procStart": START}
        if tmux_field:
            data["tmux"] = tmux_field
        path = cc.sidecar_dir() / f"{pid}.json"
        # a new size moves the stamp even within one mtime tick
        path.write_text(json.dumps(data) + " " * (len(status) + len(self.writes)))

    def kill(self, pid: int) -> None:
        for child in [p for p in self.table.values() if p.ppid == pid]:
            self.kill(child.pid)
        del self.table[pid]

    def flat(self) -> list[tuple[str, str, str | None]]:
        return [i for batch in self.writes for i in batch]


def _work_child(world: World, seat: int) -> int:
    [sh] = [p.pid for p in world.table.values() if p.ppid == seat and "eval" in (p.command or "")]
    return sh


# --- writing on change only -------------------------------------------------------

def test_the_first_tick_writes_each_pane_once_in_one_call_per_server():
    w = World()
    w.seat("%1", work="sleep 30")
    w.seat("%2")
    pub = w.publisher()
    pub.seed()
    changes = pub.tick()
    assert {t.pane: t.new.word for t in changes} == {"%1": "BUSY", "%2": "FREE"}
    assert w.counts["write"] == 1
    assert sorted(w.flat()) == sorted([
        ("%1", STATE_KEY, "BUSY"), ("%1", WHY_KEY, "work: sleep 30"),
        ("%2", STATE_KEY, "FREE"), ("%2", WHY_KEY, "nothing live")])


def test_an_unchanged_reading_is_never_written_again():
    w = World()
    w.seat("%1", work="sleep 30")
    w.seat("%2")
    pub = w.publisher()
    pub.tick()
    for _ in range(5):
        w.now += 1
        assert pub.tick() == []
    assert w.counts["write"] == 1 and pub.stats.writes == 4
    assert w.counts["ps"] == 6           # BUSY by work: the table is read every tick


def test_with_no_work_and_no_change_no_ps_runs_until_the_safety_read():
    w = World()
    w.seat("%1")
    w.seat("%2", status="busy")
    pub = w.publisher()
    pub.tick()
    for _ in range(29):
        w.now += 1
        pub.tick()
    assert w.counts["ps"] == 1 and w.counts["write"] == 1 and w.counts["panes"] == 1
    w.now += 1                           # 30 s since the last read
    pub.tick()
    assert w.counts["ps"] == 2 and w.counts["write"] == 1 and w.counts["panes"] == 1
    assert pub.stats.ticks == 31 and pub.stats.ps_runs == 2


def test_a_sidecar_write_makes_the_tick_dirty_and_only_changed_keys_are_written():
    w = World()
    seat = w.seat("%1", status="busy")
    pub = w.publisher()
    pub.tick()
    w.writes.clear()
    w.now += 1
    w.sidecar(seat, "shell", tmux_field="x:@1.%1")
    [t] = pub.tick()
    assert (t.old.word, t.new.word) == ("BUSY", "FREE")
    assert w.flat() == [("%1", STATE_KEY, "FREE"), ("%1", WHY_KEY, "nothing live")]
    assert w.counts["ps"] == 2


def test_a_why_change_alone_writes_only_the_why():
    w = World()
    seat = w.seat("%1", work="sleep 30")
    pub = w.publisher()
    pub.tick()
    w.writes.clear()
    w.kill(_work_child(w, seat))
    sh = w.add(tool_shell("make test"), seat)
    w.add("make test", sh)
    w.now += 1
    pub.tick()
    assert w.flat() == [("%1", WHY_KEY, "work: make test")]


def test_work_ending_with_no_sidecar_write_turns_free_on_the_next_tick():
    """This shape: the sidecar stays `shell`; only the tree changes."""
    w = World()
    seat = w.seat("%1", work="sleep 30")
    pub = w.publisher()
    assert pub.tick()[0].new.word == "BUSY"
    w.kill(_work_child(w, seat))
    w.now += 1
    [t] = pub.tick()
    assert t.new == cc.Reading("FREE", "nothing live")
    w.now += 1
    assert pub.tick() == [] and w.counts["ps"] == 2     # no work left: no more ps


# --- gone seats, start and stop -----------------------------------------------------

def test_a_gone_sidecar_unsets_its_pane():
    w = World()
    seat = w.seat("%1")
    pub = w.publisher()
    pub.tick()
    (cc.sidecar_dir() / f"{seat}.json").unlink()
    w.now += 1
    [t] = pub.tick()
    assert t.new is None
    assert w.writes[-1] == [("%1", STATE_KEY, None), ("%1", WHY_KEY, None)]
    assert pub.current == {}


def test_a_dead_process_unsets_its_pane_within_the_safety_read():
    w = World()
    seat = w.seat("%1")
    pub = w.publisher()
    pub.tick()
    w.kill(seat)                          # a crash: no sidecar write
    w.now += 29
    assert pub.tick() == []
    w.now += 1
    [t] = pub.tick()
    assert t.new is None and pub.current == {}


def test_start_rewrites_what_the_panes_held_and_clears_a_seatless_free():
    w = World()
    w.seat("%1", work="sleep 30")
    w.found = {"%1": "FREE", "%7": "FREE"}         # a SIGKILLed predecessor's words
    pub = w.publisher()
    assert pub.seed() == 2
    pub.tick()
    assert sorted(w.flat(), key=str) == sorted([
        ("%1", STATE_KEY, "BUSY"), ("%1", WHY_KEY, "work: sleep 30"),
        ("%7", STATE_KEY, None), ("%7", WHY_KEY, None)], key=str)


def test_clear_unsets_every_option_it_holds():
    w = World()
    w.seat("%1", work="sleep 30")
    w.seat("%2")
    pub = w.publisher()
    pub.tick()
    w.writes.clear()
    pub.clear()
    assert sorted(w.flat()) == sorted([
        ("%1", STATE_KEY, None), ("%1", WHY_KEY, None),
        ("%2", STATE_KEY, None), ("%2", WHY_KEY, None)])
    assert pub.current == {} and w.counts["write"] == 2


# --- failures, the index and odd seats ----------------------------------------------

def test_a_failed_batch_is_retried_pane_by_pane_and_the_index_reread():
    w = World()
    w.seat("%1")
    w.seat("%2", work="sleep 30")
    w.fail = {"%1"}
    pub = w.publisher()
    applied = pub.tick()
    assert [t.pane for t in applied] == ["%2"]
    assert w.counts["write"] == 3                  # the batch, then each pane alone
    assert set(pub.current) == {("s", "%2")}
    w.fail = set()
    w.now += 1
    pub.tick()                                    # forced: %1 written, panes found again
    assert set(pub.current) == {("s", "%1"), ("s", "%2")}
    assert w.counts["panes"] == 2


def test_the_index_is_read_once_and_again_only_for_a_new_seat():
    w = World()
    w.seat("%1", work="sleep 30")
    pub = w.publisher()
    for _ in range(3):
        pub.tick()
        w.now += 1
    assert w.counts["panes"] == 1
    w.seat("%2")
    pub.tick()
    assert w.counts["panes"] == 2


def test_a_seat_outside_tmux_is_never_written_and_never_rereads_the_index():
    w = World()
    seat = w.seat("%1", work="sleep 30")
    del w.panes[seat]
    pub = w.publisher()
    for _ in range(3):
        assert pub.tick() == []
        w.now += 1
    assert w.counts["panes"] == 1 and w.counts["write"] == 0



def test_a_seat_an_index_read_missed_is_found_again_at_the_safety_read():
    """do-002 review note 1: a server that answered the probe but gave no panes
    (a list-panes that timed out) left a seat 'in no pane' for its life. It is
    looked for again at the next 30 s safety read - not every tick."""
    w = World()
    w.seat("%1", work="sleep 30")
    saved, w.panes = w.panes, {}                   # the one index read comes back empty
    pub = w.publisher()
    assert pub.tick() == []                        # nowhere to write: UNKNOWN, never FREE
    w.panes = saved
    for _ in range(29):
        w.now += 1
        assert pub.tick() == []
    assert w.counts["panes"] == 1                  # work is live, ps runs, no index re-read
    w.now += 1
    [t] = pub.tick()
    assert (t.pane, t.new.word) == ("%1", "BUSY") and w.counts["panes"] == 2


def test_a_seat_outside_tmux_costs_no_index_read_at_the_safety_read():
    """A complete index that places a seat nowhere is believed: no re-read."""
    w = World()
    w.seat("%1")
    seat = w.seat("%2")
    del w.panes[seat]
    pub = w.publisher()
    pub.tick()
    for _ in range(61):
        w.now += 1
        pub.tick()
    assert w.counts["panes"] == 1 and w.counts["ps"] == 3

def test_two_seats_in_one_pane_read_the_safe_side():
    w = World()
    w.seat("%1")
    w.seat("%1", work="sleep 30")
    pub = w.publisher()
    [t] = pub.tick()
    assert t.new.word == "BUSY"


def test_a_sidecar_tmux_field_naming_another_pane_is_counted_not_obeyed():
    w = World()
    w.seat("%1", tmux_field="x:@1.%99")
    pub = w.publisher()
    [t] = pub.tick()
    assert t.pane == "%1" and pub.stats.mismatches == 1


def test_an_empty_table_unsets_and_tries_again_next_tick():
    w = World()
    w.seat("%1")
    pub = w.publisher()
    pub.tick()
    real = w.table
    w.table = {}
    w.now += 1
    pub._force = True
    [t] = pub.tick()
    assert t.new is None                           # no reading: UNKNOWN, never FREE
    w.table = real
    w.now += 1
    [t] = pub.tick()
    assert t.new.word == "FREE"


def test_a_busy_seat_with_a_turn_mark_runs_no_second_ps(monkeypatch, tmp_path):
    """The tick's own table goes to `sessions()`: a mark never costs a ps."""
    monkeypatch.setattr(procs, "table", lambda *a, **k: pytest.fail("an uncounted ps"))
    w = World()
    seat = w.seat("%1", status="busy")
    sid = f"0000{seat:04d}-0000-4000-8000-000000000134"
    mark = turn.mark_path(sid)
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(json.dumps({"v": 1, "at": T0 + 200, "harness": "claude-code",
                                "event": "Stop", "background": 1}) + "\n")
    pub = w.publisher()
    [t] = pub.tick()
    assert t.new == cc.Reading("BUSY", "turn") and w.counts["ps"] == 1


def test_dry_run_writes_nothing_and_reports_every_transition():
    w = World()
    seat = w.seat("%1", work="sleep 30")
    pub = w.publisher(dry_run=True)
    pub.seed()
    assert [t.new.word for t in pub.tick()] == ["BUSY"]
    w.kill(_work_child(w, seat))
    w.now += 1
    assert [t.new.word for t in pub.tick()] == ["FREE"]
    assert [t.new for t in pub.clear()] == [None]
    assert w.counts["write"] == 0


def test_a_transition_line_names_both_words():
    t = Transition("s", "%1", None, cc.Reading("BUSY", "work: sleep 30"))
    assert t.line() == "s %1 unset -> BUSY (work: sleep 30)"
    assert Transition("s", "%1", cc.Reading("FREE", "x"), None).line() == "s %1 FREE -> unset"


# --- the lock -----------------------------------------------------------------------

def test_the_lock_lives_in_the_state_dir():
    from sessiontools.snapshot import state_dir
    assert pane_state.lock_path() == state_dir() / "pane-state" / "watch.lock"
    assert "state" in str(state_dir()) and str(state_dir()) == os.environ["SESSION_STATE_DIR"]


def test_a_second_holder_is_refused_and_told_who_holds_it():
    path = pane_state.lock_path()
    fd = pane_state.acquire(path)
    try:
        assert fd is not None
        assert pane_state.acquire(path) is None
        assert pane_state.holder(path).startswith(f"pid {os.getpid()} on ")
    finally:
        os.close(fd)
    again = pane_state.acquire(path)
    assert again is not None
    os.close(again)


def test_a_second_watch_exits_0_naming_the_holder():
    path = pane_state.lock_path()
    fd = pane_state.acquire(path)
    err = io.StringIO()
    try:
        w = World()
        assert pane_state.watch(once=True, err=err, publisher=w.publisher()) == 0
    finally:
        os.close(fd)
    assert f"pid {os.getpid()}" in err.getvalue() and "another publisher holds" in err.getvalue()
    assert w.counts == {"ps": 0, "panes": 0, "read": 0, "write": 0}



def test_a_dry_run_takes_no_lock_and_creates_no_file():
    """An observer: it never keeps a publisher from starting, and leaves a real
    host's state dir exactly as it found it (design.md §10)."""
    w = World()
    w.seat("%1")
    out = io.StringIO()
    assert pane_state.watch(once=True, dry_run=True, out=out,
                            publisher=w.publisher(dry_run=True)) == 0
    assert not pane_state.lock_path().parent.exists()
    assert w.counts["write"] == 0 and "unset -> FREE (nothing live)" in out.getvalue()


def test_a_dry_run_runs_beside_a_live_publisher():
    fd = pane_state.acquire(pane_state.lock_path())
    try:
        w = World()
        w.seat("%1")
        out, err = io.StringIO(), io.StringIO()
        assert pane_state.watch(once=True, dry_run=True, out=out, err=err,
                                publisher=w.publisher(dry_run=True)) == 0
    finally:
        os.close(fd)
    assert err.getvalue() == "" and "-> FREE" in out.getvalue()
    assert w.counts["write"] == 0

def test_watch_ticks_until_stopped_then_unsets_everything():
    w = World()
    w.seat("%1", work="sleep 30")
    stop = threading.Event()
    pub = w.publisher()
    ticks = []
    real = pub.tick

    def tick():
        ticks.append(1)
        if len(ticks) == 3:
            stop.set()
        return real()

    pub.tick = tick
    out = io.StringIO()
    assert pane_state.watch(period=0.01, stop=stop, publisher=pub, stats=True, out=out) == 0
    assert len(ticks) == 3
    assert w.writes[-1] == [("%1", STATE_KEY, None), ("%1", WHY_KEY, None)]
    assert out.getvalue().startswith("ticks=3 ps=3 ")
    assert pane_state.acquire(pane_state.lock_path()) is not None    # released


def test_watch_once_leaves_its_writes_standing():
    w = World()
    w.seat("%1")
    pub = w.publisher()
    assert pane_state.watch(once=True, publisher=pub) == 0
    assert w.counts["write"] == 1 and pub.current


def test_a_tick_that_raises_is_logged_and_the_loop_goes_on():
    w = World()
    stop = threading.Event()
    pub = w.publisher()
    calls = []

    def tick():
        calls.append(1)
        if len(calls) == 2:
            stop.set()
        raise RuntimeError("boom")

    pub.tick = tick
    err = io.StringIO()
    assert pane_state.watch(period=0.01, stop=stop, publisher=pub, err=err) == 0
    assert err.getvalue().count("tick failed") == 2


# --- the interval setting and the batched write -------------------------------------

def _host(data):
    d = host(os.environ["HOME"], "session")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "session.json"), "w") as f:
        json.dump(data, f)


def test_the_interval_defaults_to_one_second():
    assert pane_state.interval() == 1.0


@pytest.mark.parametrize("value, want", [(0.5, 0.5), (2, 2.0), (0, 1.0), (-1, 1.0),
                                         (True, 1.0), ("2", 1.0), (None, 1.0)])
def test_the_interval_setting(value, want):
    _host({"pane_state": {"interval": value}})
    assert pane_state.interval() == want


def test_set_options_is_one_tmux_call_joined_by_semicolons(monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(tmux, "run", lambda args, socket=None, **k: calls.append((args, socket)) or Done())
    assert tmux.set_options("st", [("%1", "@a", "BUSY"), ("%1", "@b", "work: x;"), ("%2", "@a", None)])
    assert calls == [([
        "set-option", "-p", "-t", "%1", "@a", "BUSY", ";",
        "set-option", "-p", "-t", "%1", "@b", "work: x", ";",
        "set-option", "-p", "-u", "-t", "%2", "@a"], "st")]
    assert tmux.set_options("st", []) is True and len(calls) == 1


def test_set_options_is_false_when_tmux_refuses_or_is_missing(monkeypatch):
    class Refused:
        returncode = 1

    monkeypatch.setattr(tmux, "run", lambda *a, **k: Refused())
    assert tmux.set_options("st", [("%1", "@a", "x")]) is False

    def missing(*a, **k):
        raise OSError("no tmux")

    monkeypatch.setattr(tmux, "run", missing)
    assert tmux.set_options("st", [("%1", "@a", "x")]) is False
