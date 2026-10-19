"""`session reap --yes`, the act, through its seams (ST-146 do-002; design.md §5).

Every seat is FAKE (test_reap's `Fleet`): injected rows and a synthetic
process table. The close is a double that only edits that table, so no test
here reaches tmux at all. The real kill-pane runs only in
tests/test_reap_tmux.py, on a private server.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess

import pytest
from git_support import hermetic_env
from test_reap import HOUR, NOW, Fleet, git, task_dir

from sessiontools import claude_code as cc
from sessiontools import reap, snapshot


class Act:
    """A World over a Fleet. CLOSE kills (in the table) the pane's seat and
    every process under it, unless told to leave some behind."""

    def __init__(self, fleet: Fleet, tail=None):
        self.fleet = fleet
        self.closed: list[tuple[str, str]] = []
        self.gone_panes: set[tuple[str, str]] = set()
        self.leave: set[int] = set()         # pids the close leaves alive
        self.close_ok = (True, "")
        self.reads = 0
        self.after_list = None              # called on the second rows() read
        self.on_tty: set[int] = set()       # pids on the pane's terminal beyond its tree
        self.off_tty: set[int] = set()      # pids in the holder's tree NOT on its terminal
        self.tty_unreadable = False
        self.other_panes = ["%0"]           # the server's other panes: not the last by default
        self.panes_unreadable = False
        self.world = reap.World(
            rows=self.rows, table=lambda: fleet.table, listeners=lambda: cc.DEFAULT_LISTENERS,
            now=lambda: NOW, tail_check=tail or (lambda d: (True, "")),
            reread=self.reread, pane_pid=self.pane_pid, close=self.close,
            sleep=lambda s: None)
        self.world.pane_tty_pids = self.pane_tty_pids
        self.world.server_panes = self.server_panes
        self.world.launchers = lambda: INSTALLED

    def rows(self):
        self.reads += 1
        if self.reads == 2 and self.after_list:
            self.after_list()
        return self.fleet.rows

    def reread(self, s):
        return next((r.session for r in self.fleet.rows if r.session.id == s.id), None)

    def pane_pid(self, socket, pane):
        if (socket, pane) in self.gone_panes:
            return None
        for r in self.fleet.rows:
            if (r.tmux_socket, r.tmux_pane) == (socket, pane) and r.session.pid in self.fleet.table:
                return r.session.pid
        return None

    def pane_tty_pids(self, socket, pane):
        """The pane's terminal: its holder and that holder's tree, plus ON_TTY."""
        holder = self.world.pane_pid(socket, pane)
        if self.tty_unreadable or holder is None:
            return None
        return ({holder, *reap.descendants(holder, self.fleet.table), *self.on_tty}
                - self.off_tty)

    def server_panes(self, socket):
        """Every pane on SOCKET: the fleet's still open, plus OTHER_PANES."""
        if self.panes_unreadable:
            return None
        return [*self.other_panes, *(r.tmux_pane for r in self.fleet.rows
                                     if r.tmux_socket == socket and r.tmux_pane
                                     and (socket, r.tmux_pane) not in self.gone_panes)]

    def close(self, socket, pane):
        """The pane's hangup: its holder, each seat in it, and their trees
        go (bar LEAVE)."""
        holder = self.world.pane_pid(socket, pane)
        self.closed.append((socket, pane))
        if self.close_ok[0]:
            self.gone_panes.add((socket, pane))
            roots = {holder} if holder is not None else set()
            roots |= {r.session.pid for r in self.fleet.rows
                      if (r.tmux_socket, r.tmux_pane) == (socket, pane)}
            for root in roots:
                for p in [root, *reap.descendants(root, self.fleet.table)]:
                    if p not in self.leave:
                        self.fleet.table.pop(p, None)
        return self.close_ok

    def main(self, argv, capsys):
        rc = reap.main(argv, world=self.world)
        out = capsys.readouterr()
        return rc, out.out, out.err


def swap(fleet: Fleet, row, **changes):
    """Replace ROW's session in the fleet with CHANGES applied."""
    new = dataclasses.replace(row, session=dataclasses.replace(row.session, **changes))
    fleet.rows[fleet.rows.index(row)] = new
    return new


def log_lines():
    path = snapshot.state_dir() / "reap.log"
    return path.read_text().splitlines() if path.exists() else []


# --- the happy path ---------------------------------------------------------------------

def test_a_done_idle_seat_is_closed_by_pane_id_and_logged(tmp_path, capsys):
    f = Fleet()
    row = f.seat(task_dir(tmp_path), name="p+st-1.worker.aaaa1111", pane="%7", socket="st146-x")
    a = Act(f)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 0
    assert a.closed == [("st146-x", "%7")]
    assert f"closed {row.short} (p+st-1.worker.aaaa1111)" in out
    assert "--yes: 1 to close, 0 kept." in out and "dry run" not in out
    [line] = log_lines()
    fields = line.split("\t")
    assert fields[1:7] == [row.short, "p+st-1.worker.aaaa1111", "ST-1", "st146-x:%7", "closed",
                           "task ST-1 is completed; tail clean"]


def test_keep_rows_are_never_acted_on(tmp_path, capsys):
    f = Fleet()
    done = f.seat(task_dir(tmp_path / "a", "ST-1", "completed"))
    f.seat(task_dir(tmp_path / "b", "ST-2", "active"))
    f.seat(tmp_path / "nowhere")
    f.seat(task_dir(tmp_path / "c", "ST-3", "completed"), under="make test")
    a = Act(f)
    rc, _out, _ = a.main(["--yes"], capsys)
    assert rc == 0 and a.closed == [(done.tmux_socket, done.tmux_pane)]
    assert len(log_lines()) == 1


def test_no_close_row_is_exit_0_and_closes_nothing(tmp_path, capsys):
    f = Fleet()
    f.seat(task_dir(tmp_path, status="active"))
    a = Act(f)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 0 and a.closed == [] and "--yes: 0 to close, 1 kept." in out


# --- gate 1: the re-read at the act -----------------------------------------------------

def test_a_seat_gone_since_the_list_is_kept(tmp_path, capsys):
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    a = Act(f)
    a.after_list = lambda: f.rows.remove(row)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [] and "gone since the list" in out


def test_a_seat_turned_busy_between_the_list_and_the_act_is_kept(tmp_path, capsys):
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    a = Act(f)
    a.after_list = lambda: swap(f, row, busy=True, raw_status="busy", updated_at=NOW)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert f"kept {row.short}" in out and "no longer idle past 1h00m (state busy)" in out
    assert log_lines()[0].split("\t")[5] == "kept"


def test_a_seat_whose_tool_shell_started_work_since_the_list_is_kept(tmp_path, capsys):
    """Still idle by the sidecar, but a tool shell now runs work: BUSY."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    a = Act(f)
    def start_work():
        pid = row.session.pid
        f.table[9001] = reap.Proc(9001, pid, f.table[pid].started, "bash -c eval 'make test'")
        f.table[9002] = reap.Proc(9002, 9001, f.table[pid].started, "make test")
    a.after_list = start_work
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [] and "seat BUSY (work: make test)" in out


def test_a_new_pid_or_a_new_pane_is_kept(tmp_path, capsys):
    f = Fleet()
    one = f.seat(task_dir(tmp_path / "a"))
    two = f.seat(task_dir(tmp_path / "b"))
    a = Act(f)
    def change():
        swap(f, one, proc_start="Sun Dec  8 12:34:56 2030")
        i = f.rows.index(two)
        f.rows[i] = dataclasses.replace(two, tmux_pane="%999")
    a.after_list = change
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert "pid changed since the list" in out and "moved since the list" in out


def test_a_task_reopened_since_the_list_is_kept(tmp_path, capsys):
    d = task_dir(tmp_path)
    f = Fleet()
    f.seat(d)
    a = Act(f)
    a.after_list = lambda: task_dir(tmp_path, status="active")
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [] and "task ST-1 is active (not done)" in out


def test_a_seat_that_joins_the_pane_since_the_list_keeps_it(tmp_path, capsys):
    f = Fleet()
    f.seat(task_dir(tmp_path), pane="%5")
    a = Act(f)
    a.after_list = lambda: f.seat(None, pane="%5")
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [] and "pane shared with" in out


# --- gate 2: the tail, and gate 3: the last read -----------------------------------------

def test_a_tail_dirty_at_the_act_is_kept(tmp_path, capsys):
    f = Fleet()
    f.seat(task_dir(tmp_path))
    calls = []
    def tail(d):
        calls.append(d)
        return (True, "") if len(calls) == 1 else (False, "store dirty: ?? new.md")
    a = Act(f, tail=tail)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [] and "store dirty: ?? new.md" in out


def test_a_seat_turned_busy_during_the_tail_check_is_kept(tmp_path, capsys):
    """The seam between gates 2 and 3: the act's own tail check is where the
    seat wakes. The last read, after the tail, catches it."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    calls = []
    def tail(d):
        calls.append(d)
        if len(calls) == 2:                  # the act's tail, not the list's
            swap(f, row, busy=True, raw_status="busy", updated_at=NOW)
        return True, ""
    a = Act(f, tail=tail)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert "turned BUSY (turn) before the kill" in out


def test_a_pane_held_by_another_process_is_kept(tmp_path, capsys):
    f = Fleet()
    f.seat(task_dir(tmp_path))
    a = Act(f)
    a.world.pane_pid = lambda socket, pane: 4242
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [] and "is held by 4242, not this seat" in out


def test_the_pane_may_be_held_by_the_seats_launcher(tmp_path, capsys):
    """On a fleet host the pane's own process is the `session start` launcher and
    claude is its child: an ancestor holds the pane, which is the seat's."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    pid = row.session.pid
    f.table[4000] = reap.Proc(4000, 1, f.table[pid].started, SESSION_START)
    f.table[pid] = dataclasses.replace(f.table[pid], ppid=4000)
    a = Act(f)
    a.world.pane_pid = lambda socket, pane: None if a.closed else 4000
    rc, _out, _ = a.main(["--yes"], capsys)
    assert rc == 0 and a.closed == [(row.tmux_socket, row.tmux_pane)]


def test_a_pane_whose_holder_runs_another_job_is_kept(tmp_path, capsys):
    """kill-pane hangs up the whole pane: a shell that runs the seat AND a
    job of its own would lose that job. Anything in the pane's tree that is
    neither on the seat's line nor under the seat keeps the seat (ST-146
    do-002 review)."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    pid = row.session.pid
    started = f.table[pid].started
    f.table[4000] = reap.Proc(4000, 1, started, "-zsh")
    f.table[pid] = dataclasses.replace(f.table[pid], ppid=4000)
    f.table[4001] = reap.Proc(4001, 4000, started, "npm run dev")
    a = Act(f)
    a.world.pane_pid = lambda socket, pane: None if a.closed else 4000
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert f"pane {row.tmux_pane} also holds 4001 (npm run dev), not this seat's" in out
    assert 4001 in f.table


def test_a_job_let_go_on_the_panes_terminal_keeps_the_seat(tmp_path, capsys):
    """A job the pane's shell forked and let go (`(job &)`) has ppid 1: it is
    in no tree under the pane, yet it is on the pane's terminal and the
    hangup takes it. It keeps the seat, named (ST-146 do-002 review 2)."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    f.table[4002] = reap.Proc(4002, 1, f.table[row.session.pid].started, "sleep 302")
    a = Act(f)
    a.on_tty = {4002}
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert f"pane {row.tmux_pane} also holds 4002 (sleep 302), not this seat's" in out
    assert 4002 in f.table


def test_a_process_on_the_terminal_the_table_never_saw_keeps_the_seat(tmp_path, capsys):
    """Born after the table was read: unknown, so KEEP, named with '?'."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    a = Act(f)
    a.on_tty = {4003}
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert f"pane {row.tmux_pane} also holds 4003 (?), not this seat's" in out


@pytest.mark.parametrize("how", ["unreadable", "holder-off-tty"])
def test_a_terminal_that_cannot_be_read_keeps_the_seat(tmp_path, capsys, how):
    """Who is on the pane's terminal is unknown, or the reading does not even
    hold the pane's own process: either way it is not proof, so KEEP."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    a = Act(f)
    if how == "unreadable":
        a.tty_unreadable = True
    else:
        a.world.pane_tty_pids = lambda socket, pane: {4004}
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert f"pane {row.tmux_pane}'s terminal could not be read" in out


# --- the close and its verification -------------------------------------------------------

def test_a_kill_that_fails_is_a_failure(tmp_path, capsys):
    f = Fleet()
    f.seat(task_dir(tmp_path))
    a = Act(f)
    a.close_ok = (False, "kill-pane failed (exit 1: can't find pane: %9)")
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and "failed " in out and "can't find pane" in out
    assert log_lines()[0].split("\t")[5] == "failed"


def test_a_survivor_is_named_and_never_killed(tmp_path, capsys):
    """The seat's listener outliving it is the failure the verify exists
    for: named with its pid and command, and left alone."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path), under="pager listen --timeout 60")
    listener = next(p for p in f.table.values()
                    if p.command == "pager listen --timeout 60")
    a = Act(f)
    a.leave = {listener.pid}
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and len(a.closed) == 1            # one kill, never a second
    assert f"survivors {listener.pid} (pager listen --timeout 60)" in out
    assert listener.pid in f.table                    # never killed
    assert row.session.pid not in f.table


def test_a_reused_pid_is_not_a_survivor(tmp_path, capsys):
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    pid = row.session.pid
    monitor = next(p for p in f.table.values() if p.ppid == pid)
    a = Act(f)
    real_close = a.close
    def close(socket, pane):
        result = real_close(socket, pane)
        f.table[monitor.pid] = reap.Proc(monitor.pid, 1, monitor.started + 500, "something new")
        return result
    a.world.close = close
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 0, out


def test_a_pane_still_open_is_a_failure(tmp_path, capsys):
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    a = Act(f)
    real = a.server_panes
    a.world.server_panes = lambda socket: [*real(socket), row.tmux_pane]   # never goes
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and f"pane {row.tmux_pane} still open" in out


def test_verify_waits_out_its_bound_and_no_longer(tmp_path):
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    a = Act(f)
    slept = []
    a.world.sleep = slept.append
    watch = {row.session.pid: f.table[row.session.pid]}
    left = reap.verify("s", "%1", watch, a.world)
    assert left and len(slept) == int(reap.VERIFY_SECONDS / reap.VERIFY_STEP) - 1


# --- what is never killed -----------------------------------------------------------------

@pytest.mark.parametrize("pane, socket", [(None, "default"), ("%3", None), (None, None)])
def test_a_row_with_no_socket_or_pane_is_never_killed(tmp_path, capsys, pane, socket):
    f = Fleet()
    f.seat(task_dir(tmp_path), pane=pane, socket=socket)
    a = Act(f)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 0 and a.closed == [] and "no pane (nothing to close by id)" in out


def test_close_seat_refuses_no_socket_without_calling_tmux(monkeypatch):
    from sessiontools import tmux
    def boom(*a, **k):
        raise AssertionError("tmux was called")
    monkeypatch.setattr(tmux, "run", boom)
    assert reap.close_seat(None, "%3") == (False, "no socket or no pane: never killed")
    assert reap.close_seat("st146-x", "") == (False, "no socket or no pane: never killed")


def test_close_seat_kills_the_pane_by_id_on_the_seats_own_server(monkeypatch):
    from sessiontools import tmux
    seen = []
    def run(arguments, socket=None, env=None, timeout=None):
        seen.append((arguments, socket))
        return subprocess.CompletedProcess(arguments, 0, "", "")
    monkeypatch.setattr(tmux, "run", run)
    assert reap.close_seat("st146-x", "%3") == (True, "")
    assert seen == [(["kill-pane", "-t", "%3"], "st146-x")]
    import inspect
    assert "kill-window" not in inspect.getsource(reap).replace("never `kill-window`", "") \
        .replace("never kill-window", "")


def test_two_seats_in_one_pane_are_both_kept(tmp_path, capsys):
    f = Fleet()
    a1 = f.seat(task_dir(tmp_path / "a", "ST-1"), pane="%4")
    a2 = f.seat(task_dir(tmp_path / "b", "ST-2"), pane="%4")
    a = Act(f)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 0 and a.closed == []
    assert f"pane shared with {a2.short}" in out and f"pane shared with {a1.short}" in out


def test_a_seat_sharing_a_pane_with_an_unlisted_seat_is_kept(tmp_path):
    f = Fleet()
    f.seat(task_dir(tmp_path), pane="%4")
    other = f.seat(None, pane="%4", idle=60)          # under the limit: not listed
    [v] = f.plan()
    assert v.verdict == reap.KEEP and v.why == f"pane shared with {other.short}"


@pytest.mark.parametrize("kw, why", [
    ({"status": "waiting"}, "seat BUSY (waiting)"),
    ({"under": "tm task wait ST-1"}, "seat BUSY (work: tm task wait ST-1)"),
])
def test_a_done_seat_that_is_waiting_or_working_is_kept_by_yes(tmp_path, capsys, kw, why):
    f = Fleet()
    f.seat(task_dir(tmp_path), **kw)
    a = Act(f)
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 0 and a.closed == [] and why in out


# --- SHORT ----------------------------------------------------------------------------------

def two_done(tmp_path):
    f = Fleet()
    r1 = f.seat(task_dir(tmp_path / "a", "ST-1"))
    r2 = f.seat(task_dir(tmp_path / "b", "ST-2"))
    return f, r1, r2


def test_a_short_acts_on_just_that_seat(tmp_path, capsys):
    f, _r1, r2 = two_done(tmp_path)
    a = Act(f)
    rc, _out, _ = a.main(["--yes", r2.short], capsys)
    assert rc == 0 and a.closed == [(r2.tmux_socket, r2.tmux_pane)]


def test_a_short_that_names_two_seats_is_a_usage_error(tmp_path, capsys):
    f, r1, r2 = two_done(tmp_path)
    common = r1.session.id[:4]
    assert r2.session.id.startswith(common)
    a = Act(f)
    with pytest.raises(SystemExit) as e:
        a.main(["--yes", common], capsys)
    assert e.value.code == 2 and a.closed == []
    assert "names 2 listed seats" in capsys.readouterr().err


@pytest.mark.parametrize("short", ["ffffffff", "", "st-1"])
def test_a_short_that_names_no_listed_seat_is_a_usage_error(tmp_path, capsys, short):
    f, _r1, _r2 = two_done(tmp_path)
    a = Act(f)
    with pytest.raises(SystemExit) as e:
        a.main(["--yes", short], capsys)
    assert e.value.code == 2 and a.closed == []


def test_a_short_naming_a_keep_seat_is_refused_exit_1(tmp_path, capsys):
    f = Fleet()
    row = f.seat(task_dir(tmp_path, status="active"))
    a = Act(f)
    rc, out, _ = a.main(["--yes", row.short], capsys)
    assert rc == 1 and a.closed == [] and f"kept {row.short}" in out


def test_a_short_without_yes_is_a_filtered_dry_run(tmp_path, capsys):
    f, r1, r2 = two_done(tmp_path)
    a = Act(f)
    rc, out, _ = a.main([r1.short], capsys)
    assert rc == 0 and a.closed == [] and r1.short in out and r2.short not in out
    assert log_lines() == []


# --- JSON, and the dry run stays a dry run ------------------------------------------------

def test_json_with_yes_carries_each_act(tmp_path, capsys):
    f = Fleet()
    done = f.seat(task_dir(tmp_path / "a", "ST-1"))
    f.seat(task_dir(tmp_path / "b", "ST-2", "active"))
    a = Act(f)
    rc, out, _ = a.main(["--yes", "--json"], capsys)
    rows = {r["session_id"]: r for r in json.loads(out)}
    assert rc == 0
    assert rows[done.session.id]["act"] == {"outcome": "closed",
                                            "why": "task ST-1 is completed; tail clean"}
    assert [r["act"] for r in rows.values() if r["session_id"] != done.session.id] == [None]


def test_a_dry_run_never_closes_and_never_logs(tmp_path, capsys):
    f = Fleet()
    f.seat(task_dir(tmp_path))
    a = Act(f)
    rc, out, _ = a.main([], capsys)
    assert rc == 0 and a.closed == [] and log_lines() == []
    rc, out, _ = a.main(["--json"], capsys)
    assert rc == 0 and "act" not in json.loads(out)[0]


# --- the tail: worktree/ must be a repo of its own (do-001 review §3) -----------------------

@pytest.fixture
def store(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("**/worktree/\n")
    d = task_dir(root, "ST-1", "completed")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    return d


def test_a_plain_directory_worktree_with_a_file_is_keep(store):
    """No .git in worktree/: a plain `git status` there walks up to the
    store, which ignores **/worktree/, and reads clean (the control). The
    tail must not: it is not a repo of its own, so unreadable, KEEP."""
    wt = store / "worktree"
    wt.mkdir()
    (wt / "notes.md").write_text("unsaved\n")
    control = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True,
                             text=True, check=True, env=hermetic_env())   # ST-161
    assert control.stdout == ""                       # the leak, as measured
    f = Fleet()
    f.seat(store)
    [v] = f.plan(tail=reap.tail)
    assert v.verdict == reap.KEEP
    assert "worktree unreadable (not a repo of its own" in v.why


def test_a_worktree_inside_another_repo_is_keep(store, tmp_path):
    """worktree/ under a repo that is not its own (its parent's): not its top."""
    git(store, "init", "-q")                           # the task dir is a repo now
    (store / "worktree").mkdir()
    (store / "worktree" / "x.md").write_text("x\n")
    clean, detail = reap.tail(store)
    assert not clean and "not a repo of its own" in detail


def test_a_real_worktree_repo_still_reads_clean(store):
    wt = store / "worktree"
    wt.mkdir()
    git(wt, "init", "-q")
    git(wt, "commit", "-q", "--allow-empty", "-m", "seed")
    f = Fleet()
    f.seat(store)
    [v] = f.plan(tail=reap.tail)
    assert (v.verdict, v.why) == (reap.CLOSE, "task ST-1 is completed; tail clean")


def test_a_linked_git_worktree_reads_as_its_own_repo(store, tmp_path):
    """`tm task worktree` makes a LINKED worktree (a .git FILE): its top is itself."""
    main = tmp_path / "code"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "commit", "-q", "--allow-empty", "-m", "seed")
    git(main, "worktree", "add", "-q", str(store / "worktree"), "-b", "feat")
    assert reap.tail(store) == (True, "")
    (store / "worktree" / "draft.py").write_text("x\n")
    clean, detail = reap.tail(store)
    assert not clean and detail == "worktree dirty: ?? draft.py"


def test_the_worktree_half_asks_for_the_top_first(tmp_path):
    (tmp_path / "worktree").mkdir()
    seen = []
    def fake(args, cwd):
        seen.append((args, cwd.name))
        out = str(cwd) + "\n" if args[0] == "rev-parse" else ""
        return subprocess.CompletedProcess(args, 0, out, "")
    assert reap.tail(tmp_path, git=fake) == (True, "")
    assert seen[0] == (["rev-parse", "--show-toplevel"], "worktree")
    assert seen[1][0][:4] == ["status", "--porcelain", "--untracked-files=normal",
                              "--ignore-submodules=none"]


def test_log_act_never_raises_when_the_state_dir_cannot_be_written(tmp_path, monkeypatch,
                                                                   capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("SESSION_STATE_DIR", str(blocker / "state"))
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    v = reap.Verdict(row, 2 * HOUR, reap.CLOSE, "x")
    reap.log_act(reap.Act(v, reap.CLOSED, "x"))
    assert "reap.log not written" in capsys.readouterr().err


# --- ST-146 do-004: the hardening (R1, R2, m1, m2, m10) -------------------------------------

def _under(f: Fleet, row, *commands: str) -> list[int]:
    """Put a line of COMMANDS above ROW's seat, the first just above it; the
    last holds the pane. Returns their pids, seat-side first."""
    pid, started = row.session.pid, f.table[row.session.pid].started
    pids, child = [], pid
    for command in commands:
        p = f._next()
        f.table[p] = reap.Proc(p, 1, started, command)
        f.table[child] = dataclasses.replace(f.table[child], ppid=p)
        pids.append(p)
        child = p
    return pids


TM_WORK = "/u/.local/share/uv/tools/taskmaster/bin/python /u/.local/bin/tm task work ST-1 -y"
SESSION_START = "/u/.local/share/uv/tools/sessiontools/bin/python /u/.local/bin/session start"

#: The launchers "installed" for the seam tests (`installed_launchers`' shape).
INSTALLED = {os.path.realpath(p): w for p, w in (
    ("/u/.local/bin/tm", ("task", "work")), ("/x/bin/taskmaster", ("task", "work")),
    ("/u/.local/bin/session", ("start",)))}


@pytest.mark.parametrize("line", [
    (TM_WORK,),                                   # live host: 12 seats
    (TM_WORK, "-bash"),                           # live host: 31 seats
    (SESSION_START,),
    (f"{SESSION_START} --agent x", "-zsh"),
    (TM_WORK, "/bin/zsh -l"),
])
def test_a_seat_under_an_allowed_line_is_closed(tmp_path, capsys, line):
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    pids = _under(f, row, *line)
    a = Act(f)
    a.world.pane_pid = lambda socket, pane: None if a.closed else pids[-1]
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 0, out
    assert all(p not in f.table for p in pids)


@pytest.mark.parametrize("line, culprit", [
    (("python3 -c import subprocess; subprocess.run(...)",), 0),       # the wrapper holds it
    (("python3 wrapper.py", "sh -c python3 wrapper.py; true"), 0),     # R1's repro shape
    ((TM_WORK, "sh -c tm task work ST-1 -y; deploy"), 1),               # a shell with a command
    (("vim notes.md", "-zsh"), 0),                                      # an editor's :terminal
    (("-bash", TM_WORK), 0),                                            # a shell mid-line
    (("node repl.js", TM_WORK), 0),
    (("bash", TM_WORK), 0),                                             # merely CALLED bash
    (("python3 /srv/x/bin/tm task work ST-1 -y",), 0),                  # merely CALLED tm
    (("/tmp/evil/tm task work ST-1", "-bash"), 0),                      # ... mid-line
    (("tm task work ST-1",), 0),                                        # a bare name
])
def test_anything_else_on_the_seats_line_keeps_it(tmp_path, capsys, line, culprit):
    """R1 (ST-146 do-004): kill-pane takes the whole line; only a launcher,
    or the holder's own login shell, may sit on it. Anything else is KEEP,
    named, and nothing is killed."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    pids = _under(f, row, *line)
    a = Act(f)
    a.world.pane_pid = lambda socket, pane: pids[-1]
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [], out
    assert f"{pids[culprit]} (" in out
    assert all(p in f.table for p in pids)


def test_a_line_process_that_outlives_the_close_is_named(tmp_path, capsys):
    """verify watches the line too: a launcher left alive is a survivor."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    [launcher] = _under(f, row, TM_WORK)
    a = Act(f)
    a.world.pane_pid = lambda socket, pane: None if a.closed else launcher
    a.leave = {launcher}
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and f"survivors {launcher} (" in out and launcher in f.table


@pytest.mark.parametrize("command, launcher, shell", [
    (TM_WORK, True, False),
    ("/u/.local/bin/tm task work ST-1", True, False),
    ("/x/python3.12 /x/bin/taskmaster task work TM-1 -y", True, False),
    (SESSION_START, True, False),
    ("tm task work ST-1", False, False),                  # a bare name: not the installed one
    ("Python session start", False, False),
    ("python3 /elsewhere/tm task work ST-1", False, False),   # merely CALLED tm
    ("/u/.local/bin/session task work ST-1", False, False),   # the real script, wrong words
    ("/u/.local/bin/tm task wait ST-1", False, False),
    ("python3 /x/tm task wait ST-1", False, False),
    ("python3 -I /x/tm task work ST-1", False, False),
    ("session list", False, False),
    ("-bash", False, True),
    ("-zsh", False, True),
    ("zsh", False, True),
    ("/bin/bash --login", False, True),
    ("sh -c claude; true", False, False),
    ("bash script.sh", False, False),
    ("-bash -c x", False, False),
    ("", False, False),
    (None, False, False),
])
def test_what_counts_as_a_launcher_and_a_holder_shell(command, launcher, shell):
    assert reap.is_launcher(command, INSTALLED) is launcher
    assert reap.is_holder_shell(command) is shell


def test_the_last_pane_on_its_server_is_kept(tmp_path, capsys):
    """R2: closing it would end the server, and the server's jobs with it."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    a = Act(f)
    a.other_panes = []
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert f"pane {row.tmux_pane} is the last on default" in out


@pytest.mark.parametrize("how, why", [
    ("unreadable", "the panes on default could not be read"),
    ("gone", "gone from default before the kill"),
])
def test_a_server_whose_panes_cannot_be_trusted_is_kept(tmp_path, capsys, how, why):
    f = Fleet()
    f.seat(task_dir(tmp_path))
    a = Act(f)
    if how == "unreadable":
        a.panes_unreadable = True
    else:
        a.world.server_panes = lambda socket: ["%0", "%1"]
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [] and why in out


def test_a_child_of_the_holder_off_its_terminal_keeps_the_seat(tmp_path, capsys):
    """m10: the tree half of 91f507f, pinned. A child of the holder in a
    session of its own is not on the pane's terminal; only the tree sees it."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    [shell] = _under(f, row, "-zsh")
    f.table[4005] = reap.Proc(4005, shell, f.table[shell].started, "sleep 304")
    a = Act(f)
    a.world.pane_pid = lambda socket, pane: None if a.closed else shell
    a.off_tty = {4005}
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == []
    assert f"pane {row.tmux_pane} also holds 4005 (sleep 304), not this seat's" in out


@pytest.mark.parametrize("broken, word", [
    ("table", "process table unreadable"),
    ("panes", "panes on default unreadable"),
])
def test_an_unreadable_verify_is_never_a_close(tmp_path, capsys, broken, word):
    """m1: `ps` failing (an empty table) or tmux failing after the kill is
    NOT "all gone": the close is not verified, exit 1."""
    f = Fleet()
    f.seat(task_dir(tmp_path))
    a = Act(f)
    real_close = a.close
    def close(socket, pane):
        result = real_close(socket, pane)
        if broken == "table":
            a.world.table = dict
        else:
            a.panes_unreadable = True
        return result
    a.world.close = close
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and "kill-pane sent, but not verified" in out and word in out
    assert log_lines()[0].split("\t")[5] == "failed"


def test_an_inherited_git_dir_cannot_make_a_dirty_tail_read_clean(store, tmp_path, monkeypatch):
    """m2: GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE from the caller would
    point the status at another, clean repo. _git drops every GIT_*."""
    (store / "unsaved.md").write_text("draft\n")
    other = tmp_path / "clean"
    other.mkdir()
    git(other, "init", "-q")
    git(other, "commit", "-q", "--allow-empty", "-m", "seed")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    clean, detail = reap.tail(store)
    assert not clean and "unsaved.md" in detail


def test_a_launcher_is_the_installed_script_by_its_real_path(tmp_path):
    """The review of do-004: a launcher is the `tm` this PATH resolves, by
    its real path, so the console-script symlink counts and a copy of the
    same name elsewhere does not. A launcher not on PATH is none."""
    real = tmp_path / "tools" / "taskmaster" / "bin" / "tm"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tm").symlink_to(real)
    copy = tmp_path / "elsewhere" / "tm"
    copy.parent.mkdir()
    copy.write_text(real.read_text())
    found = reap.installed_launchers(lambda n: str(tmp_path / "bin" / n) if n == "tm" else None)
    assert found == {os.path.realpath(real): ("task", "work")}
    assert reap.is_launcher(f"python3 {tmp_path}/bin/tm task work ST-1", found)
    assert reap.is_launcher(f"python3 {real} task work ST-1", found)
    assert not reap.is_launcher(f"python3 {copy} task work ST-1", found)
    assert reap.installed_launchers(lambda n: None) == {}


def test_a_bare_launcher_name_is_not_resolved_against_the_reapers_cwd(tmp_path, monkeypatch):
    """The second review of do-004 (n3): a bare or relative `tm` in `ps` is
    never the launcher, even when reap itself runs from the launcher's own
    bin directory, where `realpath("tm")` WOULD land on the installed
    script. Without the isabs guard, this is the one place the name alone
    could pass."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tm").write_text("#!/bin/sh\n")
    found = {os.path.realpath(bin_dir / "tm"): ("task", "work")}
    monkeypatch.chdir(bin_dir)
    assert reap.is_launcher(f"python3 {bin_dir}/tm task work ST-1", found)   # the control
    assert not reap.is_launcher("tm task work ST-1", found)
    assert not reap.is_launcher("python3 tm task work ST-1", found)
    assert not reap.is_launcher("python3 ./tm task work ST-1", found)


def test_a_line_with_no_launcher_installed_is_kept(tmp_path, capsys):
    """Nothing on PATH resolves `tm`: no process on the line is a launcher,
    so the seat is KEEP. A gate may only turn a CLOSE into a KEEP."""
    f = Fleet()
    row = f.seat(task_dir(tmp_path))
    [launcher] = _under(f, row, TM_WORK)
    a = Act(f)
    a.world.pane_pid = lambda socket, pane: launcher
    a.world.launchers = dict
    rc, out, _ = a.main(["--yes"], capsys)
    assert rc == 1 and a.closed == [] and f"{launcher} (" in out and "not a launcher" in out
