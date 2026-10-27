"""`session list` computes once (ST-111): the single-flight snapshot.

Every test runs in conftest's fresh SESSION_STATE_DIR, never the real one.
The computation is a counting fake, so "exactly one" is counted, not
inferred; the concurrency tests use real flock, in threads and in separate
processes."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from settings_support import host
from test_listing import NOW, fleet  # noqa: F401 - the fixture, reused

from sessiontools import listing, snapshot
from sessiontools.claude_code import Session
from sessiontools.listing import Row
from sessiontools.window import Fullness


def a_row(sid="73f2f616-aaaa", fullness=None) -> Row:
    s = Session(id=sid, harness="claude-code", busy=False, updated_at=1000.0, pid=100,
                cwd="/w/repo", name="bob-f1", agent="bob", raw_status="shell",
                path=Path("/s/100.json"), proc_start="Sun Jun  9 17:00:00 2030",
                started_at=900.0, version="2.1.283", tmux="default:@1.%11")
    return Row(session=s, repo="repo", tmux_socket="default", tmux_pane="%11",
               started=900.0, fullness=fullness)


FULL = Fullness(used=250_000, peak=300_000, window=1_000_000, pct=25, model="claude-opus-5-5")


class Counter:
    """A compute that counts its calls and takes SLOW seconds."""

    def __init__(self, slow=0.0, rows=None):
        self.calls = 0
        self.slow = slow
        self.rows = rows
        self.lock = threading.Lock()

    def __call__(self, want_context):
        with self.lock:
            self.calls += 1
        if self.slow:
            time.sleep(self.slow)
        if self.rows is not None:
            return self.rows
        return [a_row(fullness=FULL if want_context else None)]


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- the shape round-trips ---------------------------------------------------------

def test_a_row_round_trips_through_the_wire():
    for row in (a_row(), a_row(fullness=FULL)):
        back = snapshot.row_from_wire(json.loads(json.dumps(snapshot.row_to_wire(row))))
        assert back == row


def test_served_output_is_byte_identical_to_computed(fleet):  # noqa: F811
    computed = listing.with_context(listing.rows())
    served = snapshot.rows(True)                       # computes, writes
    again = snapshot.rows(True, compute=Counter())     # served from the file
    for found in (served, again):
        assert listing.render_json(found, now=NOW) == listing.render_json(computed, now=NOW)
        assert listing.render_table(found, now=NOW) == listing.render_table(computed, now=NOW)


# --- one computation, many readers ---------------------------------------------------

@pytest.mark.parametrize("k", [2, 8])
def test_k_concurrent_callers_cause_exactly_one_computation(k):
    compute = Counter(slow=0.3)
    barrier = threading.Barrier(k)
    results = [None] * k

    def caller(i):
        barrier.wait()
        results[i] = snapshot.rows(True, compute=compute)

    threads = [threading.Thread(target=caller, args=(i,)) for i in range(k)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert compute.calls == 1
    assert all(r == [a_row(fullness=FULL)] for r in results)


def test_waiters_share_a_computation_slower_than_the_max_age(monkeypatch):
    """A 0.6 s computation, a 0.1 s max age, and three callers that arrive
    0.3 s INTO it: the computation began before they came, longer ago than
    the window, and they still take its result rather than compute again."""
    monkeypatch.setenv(snapshot.MAX_AGE_ENV, "0.1")
    compute = Counter(slow=0.6)
    first = threading.Thread(target=snapshot.rows, args=(True,), kwargs={"compute": compute})
    first.start()
    time.sleep(0.3)
    late = [threading.Thread(target=snapshot.rows, args=(True,), kwargs={"compute": compute})
            for _ in range(3)]
    for t in late:
        t.start()
    for t in (first, *late):
        t.join(timeout=10)
    assert compute.calls == 1


CHILD = r"""
import os, sys, time
from pathlib import Path
from sessiontools import cli, listing
from sessiontools.claude_code import Session
from sessiontools.listing import Row
counter, go = sys.argv[1], Path(sys.argv[2])
def rows(env=None):
    fd = os.open(counter, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, b"x\n"); os.close(fd)
    time.sleep(0.5)
    s = Session(id="p-1", harness="claude-code", busy=False, updated_at=1.0, pid=7)
    return [Row(session=s, repo="r", tmux_socket="default", tmux_pane="%1", started=None)]
listing.rows = rows                       # the computation, counted
listing.with_context = lambda found, env=None: found
while not go.exists():
    time.sleep(0.005)
sys.exit(cli.main(["list", "--json"]))
"""


def test_k_concurrent_session_list_processes_cause_exactly_one_computation(tmp_path):
    """K separate `session list --json` processes, released together: one
    computes, the rest block on the lock and print its result."""
    k = 5
    counter, go = tmp_path / "computations", tmp_path / "go"
    children = [subprocess.Popen([sys.executable, "-c", CHILD, str(counter), str(go)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(k)]
    time.sleep(0.5)          # every child is up and polling for GO
    go.touch()
    outs = [c.communicate(timeout=30) for c in children]
    assert [c.returncode for c in children] == [0] * k, outs
    assert all([r["session_id"] for r in json.loads(o)] == ["p-1"] for o, _e in outs)
    assert counter.read_text().count("x") == 1


# --- max age ----------------------------------------------------------------------

def test_a_young_snapshot_serves_and_a_stale_one_recomputes():
    compute, clock = Counter(), Clock()
    snapshot.rows(True, compute=compute, clock=clock)
    clock.t += 1.9
    snapshot.rows(True, compute=compute, clock=clock)
    assert compute.calls == 1
    clock.t += 0.2                                   # 2.1 s after the computation began
    snapshot.rows(True, compute=compute, clock=clock)
    assert compute.calls == 2


def test_a_snapshot_stamped_in_the_future_is_stale():
    compute, clock = Counter(), Clock()
    snapshot.rows(True, compute=compute, clock=clock)
    clock.t -= 60                                    # the clock went back
    snapshot.rows(True, compute=compute, clock=clock)
    assert compute.calls == 2


def test_a_snapshot_finished_in_the_future_is_stale():
    """A FINISHED stamp ahead of the clock would pass "finished after the
    caller arrived" for as long as it lies, and serve forever."""
    compute, clock = Counter(), Clock()
    snapshot.rows(True, compute=compute, clock=clock)
    _rewrite(finished=clock.t + 1e9)
    clock.t += 3600
    snapshot.rows(True, compute=compute, clock=clock)
    assert compute.calls == 2


def test_max_age_defaults_to_two_seconds():
    assert snapshot.max_age() == snapshot.DEFAULT_MAX_AGE == 2.0


@pytest.mark.parametrize("raw, want", [("5", 5.0), ("0.5", 0.5), ("0", 0.0),
                                       ("-1", 2.0), ("soon", 2.0), ("nan", 2.0), ("", 2.0)])
def test_max_age_from_the_environment(monkeypatch, raw, want):
    monkeypatch.setenv(snapshot.MAX_AGE_ENV, raw)
    assert snapshot.max_age() == want


def _host_settings(value):
    d = host(Path(os.environ["HOME"]), "session")
    d.mkdir(parents=True, exist_ok=True)
    (d / "session.json").write_text(json.dumps({"list": {"max_age": value}}))


def test_max_age_from_the_settings_cascade_and_the_env_wins(monkeypatch):
    _host_settings(7)
    assert snapshot.max_age() == 7.0
    monkeypatch.setenv(snapshot.MAX_AGE_ENV, "3")
    assert snapshot.max_age() == 3.0


@pytest.mark.parametrize("value", [True, "x", -2, None])
def test_a_bad_max_age_setting_is_passed_over(value):
    _host_settings(value)
    assert snapshot.max_age() == 2.0


def test_an_unreadable_settings_file_is_passed_over():
    d = host(Path(os.environ["HOME"]), "session")
    d.mkdir(parents=True)
    (d / "session.json").write_text("{not json")
    assert snapshot.max_age() == 2.0


def test_max_age_zero_turns_the_cache_off(monkeypatch):
    monkeypatch.setenv(snapshot.MAX_AGE_ENV, "0")
    compute = Counter()
    snapshot.rows(True, compute=compute)
    snapshot.rows(True, compute=compute)
    assert compute.calls == 2


# --- fresh ------------------------------------------------------------------------

def test_fresh_computes_and_still_refreshes_the_snapshot():
    compute, clock = Counter(), Clock()
    snapshot.rows(True, compute=compute, clock=clock)
    clock.t += 0.5
    snapshot.rows(True, fresh=True, compute=compute, clock=clock)
    assert compute.calls == 2
    assert snapshot.read(snapshot.snapshot_path())["started"] == clock.t
    snapshot.rows(True, compute=compute, clock=clock)   # served from the fresh one
    assert compute.calls == 2


@pytest.mark.parametrize("word, fresh", [("1", True), ("yes", True), ("TRUE", True),
                                         ("0", False), ("", False)])
def test_fresh_from_the_environment(monkeypatch, word, fresh):
    compute = Counter()
    snapshot.rows(True, compute=compute)
    monkeypatch.setenv(snapshot.FRESH_ENV, word)
    snapshot.rows(True, compute=compute)
    assert compute.calls == (2 if fresh else 1)


def test_fresh_never_waits_on_a_held_lock(monkeypatch):
    monkeypatch.setattr(snapshot, "LOCK_WAIT_S", 30.0)
    lock = snapshot.snapshot_path().with_name("list.lock")
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t0 = time.monotonic()
        snapshot.rows(True, fresh=True, compute=Counter())
        assert time.monotonic() - t0 < 1
    finally:
        os.close(fd)


# --- what is never served -------------------------------------------------------------

def _rewrite(**change):
    path = snapshot.snapshot_path()
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("change", [{"schema": snapshot.SCHEMA + 1}, {"version": "0.0.0-other"},
                                    {"context": "yes"}, {"started": "then"}, {"rows": "many"},
                                    {"rows": [{"session": {"id": "x"}}]},
                                    {"finished": 0}])
def test_a_snapshot_from_another_schema_or_malformed_recomputes(change):
    compute = Counter()
    snapshot.rows(True, compute=compute)
    _rewrite(**change)
    assert snapshot.rows(True, compute=compute) == [a_row(fullness=FULL)]
    assert compute.calls == 2
    assert snapshot.read(snapshot.snapshot_path())["schema"] == snapshot.SCHEMA   # rewritten


@pytest.mark.parametrize("body", ["", "{half", "[]", "null", "\x00\xff"])
def test_a_corrupt_snapshot_recomputes_and_is_replaced(body):
    compute = Counter()
    path = snapshot.snapshot_path()
    path.parent.mkdir(parents=True)
    path.write_text(body, errors="surrogateescape")
    assert snapshot.rows(True, compute=compute) == [a_row(fullness=FULL)]
    assert compute.calls == 1
    assert snapshot.read(path)["rows"]


def test_a_missing_snapshot_computes():
    compute = Counter()
    assert not snapshot.snapshot_path().exists()
    snapshot.rows(True, compute=compute)
    assert compute.calls == 1 and snapshot.snapshot_path().is_file()


def test_another_worlds_snapshot_is_never_served(monkeypatch, tmp_path):
    compute = Counter()
    snapshot.rows(True, compute=compute)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "elsewhere"))
    snapshot.rows(True, compute=compute)
    assert compute.calls == 2


def test_context_is_never_served_from_a_snapshot_without_it():
    compute = Counter()
    snapshot.rows(False, compute=compute)
    assert snapshot.rows(True, compute=compute) == [a_row(fullness=FULL)]
    assert compute.calls == 2


def test_a_snapshot_with_context_serves_a_caller_without_it_stripped():
    compute = Counter()
    snapshot.rows(True, compute=compute)
    assert snapshot.rows(False, compute=compute) == [a_row(fullness=None)]
    assert compute.calls == 1


# --- the file and the lock ---------------------------------------------------------------

def test_the_snapshot_is_private_and_leaves_no_temp_files():
    snapshot.rows(True, compute=Counter())
    path = snapshot.snapshot_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert sorted(p.name for p in path.parent.iterdir()) == ["list.lock", "snapshot.json"]


def test_an_unwritable_state_dir_still_lists(monkeypatch, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a dir")
    monkeypatch.setenv(snapshot.STATE_DIR_ENV, str(blocker))
    compute = Counter()
    assert snapshot.rows(True, compute=compute) == [a_row(fullness=FULL)]
    assert snapshot.rows(True, compute=compute) == [a_row(fullness=FULL)]
    assert compute.calls == 2


def test_a_lock_held_too_long_is_given_up_and_the_caller_computes(monkeypatch):
    monkeypatch.setattr(snapshot, "LOCK_WAIT_S", 0.2)
    lock = snapshot.snapshot_path().with_name("list.lock")
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        compute = Counter()
        assert snapshot.rows(True, compute=compute) == [a_row(fullness=FULL)]
        assert compute.calls == 1
    finally:
        os.close(fd)


def test_a_wall_clock_stepped_back_never_stretches_the_lock_wait(monkeypatch):
    """The give-up deadline runs on the monotonic clock: a wall clock that
    goes back an hour on every read must not keep a waiter on a hung holder."""
    monkeypatch.setattr(snapshot, "LOCK_WAIT_S", 0.2)
    lock = snapshot.snapshot_path().with_name("list.lock")
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    wall = [1_000_000.0]

    def backwards():
        wall[0] -= 3600
        return wall[0]

    compute, done = Counter(), []
    try:
        t = threading.Thread(target=lambda: done.append(
            snapshot.rows(True, compute=compute, clock=backwards)))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive(), "the waiter is still waiting"
        assert done and compute.calls == 1
    finally:
        os.close(fd)


HOLDER = r"""
import sys, time
from pathlib import Path
from sessiontools import snapshot
marker = Path(sys.argv[1])
def compute(want_context):
    marker.touch()          # it holds the lock now, mid-computation
    time.sleep(60)
    return []
snapshot.rows(True, compute=compute)
"""


def test_a_holder_killed_mid_computation_never_wedges_a_waiter(monkeypatch, tmp_path):
    """A `session list` SIGKILLed while it computes, holding the lock: the
    kernel drops its flock, so a waiter takes the lock at once and computes,
    long before the give-up deadline."""
    monkeypatch.setattr(snapshot, "LOCK_WAIT_S", 30.0)
    marker = tmp_path / "computing"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(marker)])
    try:
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert holder.poll() is None, "the holder died before it held the lock"
            assert time.monotonic() < deadline, "the holder never began computing"
            time.sleep(0.01)
        compute, done = Counter(), []
        t = threading.Thread(target=lambda: done.append(snapshot.rows(True, compute=compute)))
        t.start()
        time.sleep(0.2)
        assert t.is_alive() and compute.calls == 0      # it is waiting on the holder
        t0 = time.monotonic()
        holder.kill()
        holder.wait(timeout=5)
        t.join(timeout=5)
        assert not t.is_alive(), "the waiter is wedged on a dead holder's lock"
        assert time.monotonic() - t0 < 5
        assert done == [[a_row(fullness=FULL)]] and compute.calls == 1
        left = sorted(p.name for p in snapshot.snapshot_path().parent.iterdir())
        assert left == ["list.lock", "snapshot.json"]   # no temp file from the dead one
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()


def test_the_state_dir_defaults_under_home(monkeypatch):
    """A fresh HOME: the state leaf's fresh default is NEW (ST-266, wave 3)."""
    monkeypatch.delenv(snapshot.STATE_DIR_ENV)
    home = Path(os.environ["HOME"])
    assert snapshot.snapshot_path() == home / ".sessionkit/tools/state/list/snapshot.json"


# --- the command -------------------------------------------------------------------------

def _counting_rows(monkeypatch):
    calls = []
    real = listing.rows

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(listing, "rows", counted)
    return calls


def test_session_list_serves_its_snapshot_and_fresh_bypasses_it(fleet, monkeypatch, capsys):  # noqa: F811
    from sessiontools import cli
    calls = _counting_rows(monkeypatch)
    assert cli.main(["list", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli.main(["list", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert [list(r) for r in second] == [list(r) for r in first]
    assert [r["session_id"] for r in second] == ["73f2f616-aaaa", "a907dd33-bbbb"]
    assert cli.main(["list", "--json", "--fresh"]) == 0
    assert len(calls) == 2
    monkeypatch.setenv(snapshot.FRESH_ENV, "1")
    assert cli.main(["list"]) == 0
    assert len(calls) == 3
    capsys.readouterr()


def test_session_list_pane_reads_the_snapshot(fleet, monkeypatch, capsys):  # noqa: F811
    from sessiontools import cli
    calls = _counting_rows(monkeypatch)
    assert cli.main(["list", "--json"]) == 0
    assert cli.main(["list", "--pane", "bob"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "%11"
    assert len(calls) == 1


# --- who reads the snapshot: `session list` only (ST-111 inventory-001) ------------
#
# The verbs that make a live decision read the listing itself, every call:
# `session get` (sessionmail's pager asks it for IDLE), `session context`,
# and `session start`'s front door (refuses a second holder of an agent).
# A 2 s old answer there could double a seat or call a live one dead.

def _young_snapshot_that_lies():
    """A young snapshot naming a GHOST seat and missing the live alice."""
    from dataclasses import replace
    live = listing.rows()
    ghost = replace(live[0], session=replace(live[0].session, id="99999999-dddd",
                                             name="ghost-g1", agent="ghost", pid=999))
    lie = [r for r in live if r.session.agent != "alice"] + [ghost]
    now = time.time()
    assert snapshot.write(snapshot.snapshot_path(), lie, True, now, now)


def test_session_list_serves_the_lie_the_live_verbs_never_see_it(fleet, capsys):  # noqa: F811
    from sessiontools import cli, procs
    from sessiontools.start import holders
    _young_snapshot_that_lies()
    assert cli.main(["list", "--json"]) == 0              # the snapshot is young: served
    served = {r["agent"] for r in json.loads(capsys.readouterr().out)}
    assert "ghost" in served and "alice" not in served
    assert cli.main(["get", "status", "alice"]) == 0       # get: alice is live
    assert capsys.readouterr().out.strip() == "shell"
    assert cli.main(["get", "status", "ghost"]) != 0      # get: no ghost
    capsys.readouterr()
    door = holders.survey("alice", table=lambda: procs.table())
    assert door.held                                      # the front door: alice holds alice


def test_the_live_verbs_never_open_the_snapshot(fleet, monkeypatch, capsys):  # noqa: F811
    from test_listing import CWD, _transcript

    from sessiontools import cli, procs
    from sessiontools.start import holders

    def refuse(*_a, **_k):
        raise AssertionError("a live verb read the snapshot")

    monkeypatch.setattr(snapshot, "rows", refuse)
    monkeypatch.setattr(snapshot, "read", refuse)
    _transcript(fleet, monkeypatch, "73f2f616-aaaa", CWD,
                {"model": "claude-opus-5-5", "usage": {"input_tokens": 306_188}})
    for argv in (["get", "idle", "alice"], ["get", "status", "bob"], ["get", "pane", "bob"],
                 ["context", "bob"]):
        assert cli.main(argv) == 0, argv
    capsys.readouterr()
    holders.survey("bob", table=lambda: procs.table())
