"""The front door (SESH-497, ST-46 do-001): `session start <agent>` refuses a
hand launch of an agent a live Claude Code session already holds.

Re-measured on master b216a19 first (2030-12-08): a live sidecar
for `scout` beside `session start scout hello` - exit 0, the stub vendor
ran. Every e2e test below that expects a refusal failed there.

Hermetic: the holder is THIS pytest process (alive, its start time read
from `ps`) described by a sidecar in a private `SESSION_SIDECAR_DIR`; the
vendor is a stub `claude` on PATH; sessions mint into a private
SESSIONS_DIR; tmux is the conftest's private server. No real seat, never
`~/.claude/sessions`, never `~/.claude/settings.json`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from start_support import (
    TIMEOUT_S,
    at_a_terminal,
    clean_env,
    launch_e2e,
    make_agents_root,
    recorded_argv,
)

import sessiontools.start as st
from sessiontools import procs
from sessiontools.start import EXIT_HELD, default_seat, holders
from sessiontools.start import args as a
from sessiontools.start.errors import LaunchError
from sessiontools.start.placement import TmuxPlacement

SID = "37f09460-1111-2222-3333-444455556666"
AGE_S = 23 * 60 + 30          # "live 23m", however many seconds the test takes


def proc_start(pid: int) -> str:
    """PID's start time as Claude Code writes `procStart`: UTC ctime."""
    started = procs.table()[pid].started
    return time.strftime(procs.CTIME, time.gmtime(started))


def write_sidecar(directory: Path, *, pid: int | None = None, agent: str | None = "scout",
                  sid: str = SID, proc: str | None = None, tmux: str | None = "priv:@1.%4",
                  age_s: float = AGE_S, name: str | None = None) -> Path:
    """One Claude Code sidecar. Default: a LIVE holder of `scout` - this
    process, with its real start time."""
    pid = os.getpid() if pid is None else pid
    data = {"pid": pid, "sessionId": sid, "status": "idle",
            "updatedAt": int(time.time() * 1000),
            "startedAt": int((time.time() - age_s) * 1000),
            "procStart": proc if proc is not None else proc_start(os.getpid()),
            "cwd": "/nowhere"}
    if agent is not None:
        data["agent"] = agent
    if tmux is not None:
        data["tmux"] = tmux
    if name is not None:
        data["name"] = name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}-{sid[:4]}.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def sidecars(tmp_path) -> Path:
    return tmp_path / "sidecars"


def run(tmp_path: Path, sidecars: Path, *verb_args: str, extra: dict | None = None):
    """`session start ...` as a subprocess with a stub vendor, the sidecars
    and a private SESSIONS_DIR. Returns (result, record_dir, sessions_dir)."""
    sessions = tmp_path / "minted"
    env = {"SESSION_SIDECAR_DIR": str(sidecars), "SESSIONS_DIR": str(sessions)}
    env.update(extra or {})
    result, record, _work = launch_e2e(tmp_path, *verb_args, extra_env=env)
    return result, record, sessions


def vendor_ran(record: Path) -> bool:
    return (record / "argv").exists()


def minted_anything(sessions: Path) -> bool:
    return sessions.exists() and any(sessions.iterdir())


# ---------- the refusal, end to end -------------------------------------------

REFUSAL = (f"session start: scout is already held by session {SID[:8]} "
           f"(pid {os.getpid()}, tmux %4), live 23m -- refusing a second holder; "
           "pass --allow-second-holder to start one anyway")


def test_a_live_holder_refuses_a_hand_launch_before_anything_is_written(tmp_path, sidecars):
    write_sidecar(sidecars)
    result, record, sessions = run(tmp_path, sidecars, "scout", "hello")
    assert result.returncode == EXIT_HELD == 3
    assert result.stderr.splitlines() == [REFUSAL]
    assert not vendor_ran(record)
    assert not minted_anything(sessions)


def test_the_refusal_names_the_newest_holder_and_counts_the_rest(tmp_path, sidecars):
    write_sidecar(sidecars, sid="aaaaaaaa-old", age_s=3 * 3600)
    write_sidecar(sidecars, sid="bbbbbbbb-new", age_s=90, tmux=None)
    result, record, _ = run(tmp_path, sidecars, "scout")
    assert result.returncode == EXIT_HELD
    assert (f"scout is already held by session bbbbbbbb (pid {os.getpid()}), "
            "live 1m (+1 more live) -- refusing") in result.stderr
    assert not vendor_ran(record)


def test_a_resume_is_a_new_holder_too(tmp_path, sidecars):
    write_sidecar(sidecars)
    result, record, _ = run(tmp_path, sidecars, "scout", "--resume", "abc")
    assert result.returncode == EXIT_HELD
    assert not vendor_ran(record)


def windows() -> list[str]:
    """Every window on the private server as `<session> <window id>`.

    Ids, never `#{window_name}` (ST-127): automatic-rename makes the name
    track the pane's current command, so a read taken before the new
    pane's shell has exec'd says `tmux` and a later one `bash`. A window id
    is fixed for the window's life and never reused on a server."""
    return subprocess.run(["tmux", "list-windows", "-a", "-F", "#{session_name} #{window_id}"],
                          capture_output=True, text=True, check=True).stdout.splitlines()


def next_window_id() -> int:
    """The id tmux hands the NEXT window, read by opening a probe window:
    tmux numbers windows from one counter that only climbs, so a window
    opened and closed again in between still shows up here."""
    probe = subprocess.run(["tmux", "new-window", "-d", "-t", "held", "-P", "-F", "#{window_id}"],
                           capture_output=True, text=True, check=True).stdout.strip()
    return int(probe.removeprefix("@"))


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_a_tmux_placement_is_refused_before_any_window_opens(tmp_path, sidecars):
    """On the conftest's PRIVATE server (TMUX_TMPDIR, TMUX unset) only."""
    assert "TMUX" not in os.environ and os.environ["TMUX_TMPDIR"]
    subprocess.run(["tmux", "new-session", "-d", "-s", "held"], check=True)
    before = windows()
    write_sidecar(sidecars)
    result, _record, sessions = run(tmp_path, sidecars, "--tmux-target", "held", "scout")
    assert result.returncode == EXIT_HELD, result.stderr
    assert windows() == before          # no window open now, in any session
    newest = max(int(line.split()[1].removeprefix("@")) for line in before)
    assert next_window_id() == newest + 1   # and none opened and closed meanwhile
    assert not minted_anything(sessions)


# ---------- the override ------------------------------------------------------

def test_the_override_launches_and_says_whose_holder_it_joins(tmp_path, sidecars):
    write_sidecar(sidecars)
    result, record, sessions = run(tmp_path, sidecars, "--allow-second-holder", "scout")
    assert result.returncode == 0, result.stderr
    assert vendor_ran(record) and minted_anything(sessions)
    assert result.stderr.splitlines() == [
        (f"session start: starting a second holder of scout beside session {SID[:8]} "
         f"(pid {os.getpid()}, tmux %4), live 23m (--allow-second-holder)")]


def test_the_override_is_silent_with_nobody_to_join(tmp_path, sidecars):
    result, record, _ = run(tmp_path, sidecars, "--allow-second-holder", "scout")
    assert result.returncode == 0 and vendor_ran(record)
    assert result.stderr == ""


def test_the_override_after_the_agent_name_is_the_position_trap(capsys):
    with pytest.raises(SystemExit) as stop:
        a.parse_args(["scout", "--allow-second-holder"])
    assert stop.value.code == 2
    assert "session start --allow-second-holder scout" in capsys.readouterr().err


# ---------- where the door does not apply -------------------------------------

@pytest.mark.parametrize("named", [["-n", "scout+1234"], ["--name", "s"], ["--name=s"],
                                   ["-ns"]])
def test_a_seat_its_spawner_named_is_not_checked(tmp_path, sidecars, named):
    write_sidecar(sidecars)
    result, record, _ = run(tmp_path, sidecars, "scout", *named)
    assert result.returncode == 0, result.stderr
    assert vendor_ran(record) and result.stderr == ""
    assert named[0] in recorded_argv(record)


def test_a_plain_session_is_never_checked(tmp_path, sidecars):
    write_sidecar(sidecars, agent=None)
    write_sidecar(sidecars, agent="scout", sid="cccccccc-x")
    result, record, _ = run(tmp_path, sidecars)
    assert result.returncode == 0, result.stderr
    assert vendor_ran(record)


@pytest.mark.parametrize("dead", [
    {"pid": 99_999_999},                                  # no such process
    {"proc": "Sun Dec  8 12:34:56 2030"},                 # the pid, reused
])
def test_a_dead_or_reused_sidecar_holds_nothing(tmp_path, sidecars, dead):
    write_sidecar(sidecars, **dead)
    result, record, _ = run(tmp_path, sidecars, "scout")
    assert result.returncode == 0, result.stderr
    assert vendor_ran(record) and result.stderr == ""


def test_another_agent_holds_nothing_of_this_one(tmp_path, sidecars):
    write_sidecar(sidecars, agent="bob")
    result, record, _ = run(tmp_path, sidecars, "scout")
    assert result.returncode == 0 and vendor_ran(record)


def test_a_missing_sidecar_dir_is_no_holders(tmp_path):
    result, record, _ = run(tmp_path, tmp_path / "absent", "scout")
    assert result.returncode == 0 and vendor_ran(record)
    assert result.stderr == ""


# ---------- the dry run answers the question, exit 0 --------------------------

def test_a_dry_run_reports_the_refusal_and_exits_zero(tmp_path, sidecars):
    write_sidecar(sidecars)
    result, record, sessions = run(tmp_path, sidecars, "--dry-run", "scout")
    assert result.returncode == 0, result.stderr
    assert not vendor_ran(record) and not minted_anything(sessions)
    lines = result.stdout.splitlines()
    held = [line for line in lines if line.startswith("holder:")]
    assert held == [f"holder:   a live launch would REFUSE (exit 3): {REFUSAL[15:]}"]
    assert lines.index(held[0]) == 4       # after the banner, agent, vendor, mode


def test_a_dry_run_with_the_override_says_it_would_launch(tmp_path, sidecars):
    write_sidecar(sidecars)
    result, _, _ = run(tmp_path, sidecars, "--dry-run", "--allow-second-holder", "scout")
    assert result.returncode == 0
    assert "holder:   a live launch would start ANYWAY: starting a second holder of scout" \
        in result.stdout


def test_a_dry_run_with_no_holder_has_no_holder_line(tmp_path, sidecars):
    result, _, _ = run(tmp_path, sidecars, "--dry-run", "scout")
    assert result.returncode == 0 and "holder:" not in result.stdout


# ---------- cannot measure: fail OPEN, loudly ---------------------------------

def broken_ps(tmp_path: Path) -> str:
    """A PATH whose `ps` fails: the process table comes back empty."""
    bin_dir = tmp_path / "broken-ps"
    bin_dir.mkdir()
    stub = bin_dir / "ps"
    stub.write_text("#!/bin/sh\necho 'ps: walled off' >&2\nexit 1\n")
    stub.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def test_an_unreadable_process_table_fails_open_with_one_line(tmp_path, sidecars):
    write_sidecar(sidecars)
    # launch_e2e's own stub `claude` dir (tmp_path/bin, made when it runs)
    # stays FIRST: a PATH without it would reach the host's real vendor.
    path = f"{tmp_path / 'bin'}{os.pathsep}{broken_ps(tmp_path)}"
    result, record, _ = run(tmp_path, sidecars, "scout", extra={"PATH": path})
    assert vendor_ran(record), result.stderr
    assert result.returncode == 0
    assert result.stderr.splitlines() == [
        ("session start: the second-holder check could not run: the process table came "
         "back empty (`ps` failed?) -- launching unchecked")]


# ---------- the door, in process ------------------------------------------------

def test_names_the_seat_reads_every_spelling():
    assert holders.names_the_seat(["--model", "x", "-n", "s"])
    assert holders.names_the_seat(["--name=s"])
    assert holders.names_the_seat(["-ns"])
    assert not holders.names_the_seat(["--model", "x", "--no-chrome"])
    assert not holders.names_the_seat([])


def test_pane_of():
    assert holders.pane_of("sessiontools:@154.%415") == "%415"
    assert holders.pane_of("weird") is None
    assert holders.pane_of(None) is None


def test_no_candidate_asks_no_process_table(sidecars):
    write_sidecar(sidecars, agent="bob")

    def table():
        raise AssertionError("ps asked with no candidate")

    door = holders.survey("scout", {"SESSION_SIDECAR_DIR": str(sidecars)}, table=table)
    assert not door.held and door.unmeasured is None


def test_a_raising_process_table_is_unmeasured_and_opens(sidecars):
    write_sidecar(sidecars)

    def table():
        raise OSError("no ps")

    door = holders.survey("scout", {"SESSION_SIDECAR_DIR": str(sidecars)}, table=table)
    assert door.unmeasured == "the process table could not be read (no ps)"
    warned = []
    holders.enforce(door, allowed=False, warn=warned.append)   # no raise
    assert warned == [("the second-holder check could not run: the process table could "
                       "not be read (no ps) -- launching unchecked")]


def test_enforce_raises_exit_held(sidecars):
    write_sidecar(sidecars)
    door = holders.front_door("scout", [], {"SESSION_SIDECAR_DIR": str(sidecars)})
    with pytest.raises(LaunchError) as raised:
        holders.enforce(door, allowed=False, warn=lambda _l: None)
    assert raised.value.code == EXIT_HELD
    assert str(raised.value) == REFUSAL[len("session start: "):]


def test_front_door_does_not_apply_to_a_plain_or_named_launch(sidecars):
    write_sidecar(sidecars)
    env = {"SESSION_SIDECAR_DIR": str(sidecars)}
    assert holders.front_door("", [], env) is None
    assert holders.front_door("scout", ["-n", "seat"], env) is None
    assert holders.front_door("scout", [], env).held


# ---------- same-pane respawn: REPLACED, not doubled (ST-46 do-003) -----------
# A merge hold: `respawn --go` runs `tmux respawn-pane -k` with `exec
# session start <agent>` and no -n. -k does not wait for the old claude to
# die, so its sidecar can still read LIVE when the door surveys: exit 3, the
# pane left dead. A holder IN THE LAUNCHER'S OWN PANE is being replaced -
# on the direct path, and only when it is NOT the launcher's ancestor
# (ST-260): the launcher respawn-pane starts is tmux's child, so the dying
# claude is a stranger to it. The `stranger` fixture is that shape: a live
# process beside the launcher, never above it (this pytest IS above it).
# Hermetic: TMUX_PANE alone, no TMUX - no tmux server is touched.

@pytest.fixture
def stranger():
    """A live pid that is not the e2e launcher's ancestor: a sibling child
    of this pytest, as the old claude is to a respawned pane's launcher."""
    child = subprocess.Popen(["sleep", "300"])
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait()


def test_a_same_pane_respawn_is_not_refused(tmp_path, sidecars, stranger):
    write_sidecar(sidecars, pid=stranger, proc=proc_start(stranger), tmux="priv:@1.%4")
    result, record, sessions = run(tmp_path, sidecars, "scout", "hello",
                                   extra={"TMUX_PANE": "%4"})
    assert result.returncode == 0, result.stderr
    assert vendor_ran(record) and minted_anything(sessions)
    assert result.stderr == ""


def test_a_different_pane_second_start_is_still_refused(tmp_path, sidecars):
    write_sidecar(sidecars, tmux="priv:@1.%4")
    result, record, sessions = run(tmp_path, sidecars, "scout", "hello",
                                   extra={"TMUX_PANE": "%9"})
    assert result.returncode == EXIT_HELD == 3
    assert result.stderr.splitlines() == [REFUSAL]
    assert not vendor_ran(record) and not minted_anything(sessions)


def test_not_in_tmux_exempts_nothing(tmp_path, sidecars):
    write_sidecar(sidecars, tmux="priv:@1.%4")
    result, record, _ = run(tmp_path, sidecars, "scout", extra={"TMUX_PANE": ""})
    assert result.returncode == EXIT_HELD
    assert not vendor_ran(record)


def test_a_sidecar_with_no_tmux_field_exempts_nothing(tmp_path, sidecars):
    write_sidecar(sidecars, tmux=None)
    result, record, _ = run(tmp_path, sidecars, "scout", extra={"TMUX_PANE": "%4"})
    assert result.returncode == EXIT_HELD
    assert not vendor_ran(record)


def test_the_own_pane_holder_is_dropped_but_another_still_refuses(tmp_path, sidecars,
                                                                  stranger):
    write_sidecar(sidecars, pid=stranger, proc=proc_start(stranger), sid="aaaaaaaa-own",
                  tmux="priv:@1.%4", age_s=60)
    write_sidecar(sidecars, sid="bbbbbbbb-other", tmux="priv:@2.%7", age_s=3 * 3600)
    result, record, _ = run(tmp_path, sidecars, "scout", extra={"TMUX_PANE": "%4"})
    assert result.returncode == EXIT_HELD
    # Names the OTHER pane's holder, and counts no "+1 more": the own-pane
    # session is being replaced, so it is not a holder at all.
    assert (f"scout is already held by session bbbbbbbb (pid {os.getpid()}, tmux %7), "
            "live 3h") in result.stderr
    assert "more live" not in result.stderr
    assert not vendor_ran(record)


def test_a_same_pane_dry_run_has_no_holder_line(tmp_path, sidecars, stranger):
    write_sidecar(sidecars, pid=stranger, proc=proc_start(stranger), tmux="priv:@1.%4")
    result, _, _ = run(tmp_path, sidecars, "--dry-run", "scout", extra={"TMUX_PANE": "%4"})
    assert result.returncode == 0 and "holder:" not in result.stdout


@pytest.mark.parametrize("pane, tmux, held", [
    ("%4", "priv:@1.%4", False),        # own pane: replaced
    ("%9", "priv:@1.%4", True),         # another pane: a holder
    (None, "priv:@1.%4", True),         # not in tmux: nothing exempt
    ("", "priv:@1.%4", True),           # empty TMUX_PANE: same as unset
    ("%4", None, True),                 # sidecar names no pane
    ("%4", "weird", True),              # sidecar's tmux has no pane part
])
def test_survey_exempts_only_the_launchers_own_pane(sidecars, pane, tmux, held):
    write_sidecar(sidecars, tmux=tmux)
    env = {"SESSION_SIDECAR_DIR": str(sidecars)}
    if pane is not None:
        env["TMUX_PANE"] = pane
    assert holders.survey("scout", env).held is held



# ---------- the shared per-agent claim helpers (ST-256 do-003) -------------------
# ST-260 calls these for any named agent (inputs/04, 07), so they are tested
# here with a named agent and no seat word in sight.

def _dead_pid() -> int:
    """A pid that WAS running and is not now: a reaped child's."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def _state_env(tmp_path: Path, **extra: str) -> dict:
    env = {"SESSION_STATE_DIR": str(tmp_path / "state"),
           "SESSION_SIDECAR_DIR": str(tmp_path / "sidecars")}
    env.update(extra)
    return env


def test_claim_paths_are_per_agent(tmp_path):
    env = _state_env(tmp_path)
    scout_lock, scout_claim = holders.claim_paths("scout", env)
    seat_lock, seat_claim = holders.claim_paths("alice", env)
    assert scout_lock == tmp_path / "state" / "holders" / "scout.lock"
    assert scout_claim == tmp_path / "state" / "holders" / "scout.json"
    assert {scout_lock, scout_claim}.isdisjoint({seat_lock, seat_claim})


@pytest.mark.parametrize("name", ["", "a/b", "..", "a..b", ".hidden", "../up", "a\0b", "a\\b"])
def test_claim_paths_refuse_a_name_that_could_leave_the_dir(tmp_path, name):
    with pytest.raises(ValueError, match="cannot name a holder's claim"):
        holders.claim_paths(name, _state_env(tmp_path))


def test_a_read_creates_nothing(tmp_path):
    """Read-only means no mkdir (r4 N4): the paths, a read and a liveness
    check leave no `holders/` behind."""
    env = _state_env(tmp_path)
    holders.claim_paths("scout", env)
    assert holders.read_claim("scout", env) is None
    assert holders.live_claim("scout", procs.table(), env, direct=True) is None
    assert not (tmp_path / "state").exists()


def test_claim_round_trip_for_a_named_agent(tmp_path):
    """Write, read live, read stale once the pid is dead: all through the
    helper, 0600, for a named agent."""
    env = _state_env(tmp_path)
    table = procs.table()
    me = os.getpid()
    path = holders.write_claim("scout", {"sessionId": SID, "pid": me,
                                         "procStart": holders.proc_start_of(me, table)}, env)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    claim = holders.read_claim("scout", env)
    assert claim["agent"] == "scout" and claim["v"] == 1 and claim["sessionId"] == SID
    assert holders.live_claim("scout", table, env, direct=True) == claim
    dead = _dead_pid()
    holders.write_claim("scout", {"sessionId": SID, "pid": dead,
                                  "procStart": holders.proc_start_of(me, table)}, env)
    assert holders.live_claim("scout", procs.table(), env, direct=True) is None
    assert not list(path.parent.glob("*.tmp"))


def test_a_reused_pid_is_stale(tmp_path):
    env = _state_env(tmp_path)
    table = procs.table()
    me = os.getpid()
    hour_off = time.strftime(procs.CTIME, time.gmtime(table[me].started - 3600))
    holders.write_claim("scout", {"pid": me, "procStart": hour_off}, env)
    assert holders.read_claim("scout", env) is not None
    assert holders.live_claim("scout", table, env, direct=True) is None


@pytest.mark.parametrize("text", ["{not json", "[]", '{"pid": "12"}', '{"pid": true}', ""])
def test_an_unparseable_claim_is_stale(tmp_path, text):
    env = _state_env(tmp_path)
    _lock, path = holders.claim_paths("scout", env)
    path.parent.mkdir(parents=True)
    path.write_text(text)
    assert holders.read_claim("scout", env) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_claim_is_a_failure_to_measure(tmp_path):
    """Permission denied is not stale (r4 N4): it raises, and the caller
    reads it as a check that could not run."""
    env = _state_env(tmp_path)
    _lock, path = holders.claim_paths("scout", env)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(0)
    try:
        with pytest.raises(holders.ClaimUnreadable, match="could not be read"):
            holders.read_claim("scout", env)
    finally:
        path.chmod(0o600)


def test_locked_is_exclusive_and_bounded(tmp_path):
    """A second taker waits at most WAIT and is told why; the first's exit
    frees the lock at once."""
    env = _state_env(tmp_path)
    with holders.locked("scout", env) as first:
        assert first.ok
        lock, _claim = holders.claim_paths("scout", env)
        assert oct(lock.stat().st_mode & 0o777) == "0o600"
        began = time.monotonic()
        with holders.locked("scout", env, wait=0.2) as second:
            assert not second.ok and "held the lock" in second.why
        assert time.monotonic() - began < 2
    with holders.locked("scout", env, wait=0.2) as third:
        assert third.ok


def test_locked_says_why_when_the_state_dir_cannot_be_made(tmp_path):
    blocker = tmp_path / "state"
    blocker.write_text("a file where the state dir goes")
    with holders.locked("scout", _state_env(tmp_path), wait=0.1) as lock:
        assert not lock.ok and "could not be opened" in lock.why


def _chain(*pids: int) -> dict:
    """A process table where each pid's parent is the next one."""
    table = {}
    for pid, parent in zip(pids, (*pids[1:], 1), strict=True):
        table[pid] = procs.Proc(pid, parent, 0.0)
    return table


def test_replacing_is_the_narrow_rule():
    """True only on the direct path, in the own pane, for a holder that is
    not the launcher or its ancestor."""
    table = _chain(30, 20, 10)          # 30 (launcher) <- 20 <- 10
    table[99] = procs.Proc(99, 1, 0.0)  # a stranger: e.g. the old claude respawn-pane -k kills
    kw = {"launcher": 30}
    assert holders.replacing(99, "s:@1.%4", "%4", table, direct=True, **kw)
    assert holders.replacing(99, "%4", "%4", table, direct=True, **kw)      # a claim's pane
    assert not holders.replacing(99, "s:@1.%4", "%4", table, direct=False, **kw)
    assert not holders.replacing(20, "s:@1.%4", "%4", table, direct=True, **kw)   # an ancestor
    assert not holders.replacing(30, "s:@1.%4", "%4", table, direct=True, **kw)   # itself
    assert not holders.replacing(99, "s:@1.%5", "%4", table, direct=True, **kw)   # another pane
    assert not holders.replacing(99, None, "%4", table, direct=True, **kw)        # no pane
    assert not holders.replacing(99, None, None, table, direct=True, **kw)        # neither in tmux


def test_survey_keeps_the_broad_rule_by_default(tmp_path, sidecars, monkeypatch):
    """`survey`'s DEFAULT exempt stays the broad `replaced`: a holder in the
    launcher's pane is exempt even when it is the launcher's ancestor. The
    front door passes the narrow rule in, so it holds that holder."""
    write_sidecar(sidecars)                      # this pytest, in pane %4
    env = {"SESSION_SIDECAR_DIR": str(sidecars), "TMUX_PANE": "%4",
           "SESSION_STATE_DIR": str(tmp_path / "state")}
    assert not holders.survey("scout", env).held
    narrow = holders.survey("scout", env, exempt=lambda s, own, t: holders.replacing(
        s.pid, s.tmux, own, t, direct=True))
    assert narrow.held
    assert holders.front_door("scout", [], env).held


# ---------- ST-260: the narrow own-pane rule at the front door ---------------------
# The broad rule exempted ANY holder in the launcher's $TMUX_PANE. A tmux
# placement typed in the holder's own tool shell, and a start nested in
# that shell, replace nothing: both must meet the door.

def test_a_nested_direct_start_in_the_holders_own_shell_is_refused(tmp_path, sidecars):
    """The holder (this pytest) is the launcher's ANCESTOR and shares its
    pane: a start typed in a seat's own tool shell, not a respawn."""
    write_sidecar(sidecars, tmux="priv:@1.%4")
    result, record, sessions = run(tmp_path, sidecars, "scout", "hello",
                                   extra={"TMUX_PANE": "%4"})
    assert result.returncode == EXIT_HELD, result.stderr
    assert result.stderr.splitlines() == [REFUSAL]
    assert not vendor_ran(record) and not minted_anything(sessions)


def test_a_nested_dry_run_says_it_would_refuse(tmp_path, sidecars):
    write_sidecar(sidecars, tmux="priv:@1.%4")
    result, _, _ = run(tmp_path, sidecars, "--dry-run", "scout", extra={"TMUX_PANE": "%4"})
    assert result.returncode == 0
    assert f"holder:   a live launch would REFUSE (exit 3): {REFUSAL[15:]}" in result.stdout


@pytest.mark.parametrize("holder", ["ancestor", "stranger"])
def test_in_tmux_from_the_holders_own_pane_is_refused(tmp_path, sidecars, stranger, holder):
    """A tmux placement never replaces the launcher's pane, so the own-pane
    holder is a holder whatever its ancestry. Refused before placement:
    no tmux server is asked anything."""
    pid = os.getpid() if holder == "ancestor" else stranger
    write_sidecar(sidecars, pid=pid, proc=proc_start(pid), tmux="priv:@1.%4")
    result, record, sessions = run(tmp_path, sidecars, "--tmux-target", "held", "scout",
                                   extra={"TMUX_PANE": "%4"})
    assert result.returncode == EXIT_HELD, result.stderr
    assert result.stderr.splitlines() == [REFUSAL.replace(f"pid {os.getpid()}", f"pid {pid}")]
    assert not vendor_ran(record) and not minted_anything(sessions)


def test_an_unrelated_pane_is_unchanged(tmp_path, sidecars, stranger):
    """A holder in another pane refuses; a launch with no holder in sight
    launches - whatever the launcher's own pane."""
    write_sidecar(sidecars, pid=stranger, proc=proc_start(stranger), tmux="priv:@1.%7")
    result, record, _ = run(tmp_path, sidecars, "scout", extra={"TMUX_PANE": "%4"})
    assert result.returncode == EXIT_HELD
    assert not vendor_ran(record)


def test_front_door_applies_the_narrow_rule(tmp_path, sidecars, stranger):
    env = {"SESSION_SIDECAR_DIR": str(sidecars), "TMUX_PANE": "%4",
           "SESSION_STATE_DIR": str(tmp_path / "state")}
    write_sidecar(sidecars, pid=stranger, proc=proc_start(stranger), tmux="priv:@1.%4")
    assert not holders.front_door("scout", [], env).held                 # a respawn
    assert not holders.front_door("scout", [], env, direct=True).held
    assert holders.front_door("scout", [], env, direct=False).held       # --in-tmux
    # The same holder as the launcher's PARENT: a start nested in its shell.
    table = procs.table()
    table[4242] = procs.Proc(4242, stranger, 0.0)
    assert holders.front_door("scout", [], env, table=lambda: table, launcher=4242).held


def test_front_door_with_no_candidate_and_no_claim_asks_no_process_table(tmp_path, sidecars):
    write_sidecar(sidecars, agent="bob")

    def table():
        raise AssertionError("ps asked with no candidate and no claim")

    env = {"SESSION_SIDECAR_DIR": str(sidecars), "SESSION_STATE_DIR": str(tmp_path / "state")}
    door = holders.front_door("scout", [], env, table=table)
    assert not door.held and door.unmeasured is None


# ---------- ST-260: the door reads, and a hand launch takes, the agent's claim ------

def _claim_for(env: dict, pid: int, pane: str | None = None, sid: str | None = None) -> None:
    holders.write_claim("scout", {"sessionId": sid, "pid": pid,
                                  "procStart": proc_start(pid), "pane": pane,
                                  "via": "front door"}, env)


def _run_env(tmp_path: Path, sidecars: Path, **extra: str) -> dict:
    return {"SESSION_SIDECAR_DIR": str(sidecars), "SESSION_STATE_DIR": str(tmp_path / "state"),
            **extra}


def test_a_live_claim_holds_the_agent_with_no_sidecar(tmp_path, sidecars, stranger):
    """A launch still in its start race has written its claim and no
    sidecar yet: the door refuses on the claim alone."""
    env = _run_env(tmp_path, sidecars)
    _claim_for(env, stranger, pane="%7")
    result, record, sessions = run(tmp_path, sidecars, "scout", extra=env)
    assert result.returncode == EXIT_HELD, result.stderr
    assert result.stderr.splitlines() == [
        (f"session start: scout is already held by a session (id not known yet) "
         f"(pid {stranger}, tmux %7), live 0s -- refusing a second holder; pass "
         "--allow-second-holder to start one anyway")]
    assert not vendor_ran(record) and not minted_anything(sessions)


def test_a_claim_and_its_own_sidecar_are_one_holder(tmp_path, sidecars, stranger):
    env = _run_env(tmp_path, sidecars)
    _claim_for(env, stranger, pane="%7")
    write_sidecar(sidecars, pid=stranger, proc=proc_start(stranger), tmux="priv:@1.%7")
    door = holders.front_door("scout", [], env)
    assert [h.pid for h in door.holders] == [stranger]
    assert door.holders[0].id == SID                  # the sidecar's, with its id


def test_a_dead_claim_holds_nothing(tmp_path, sidecars):
    env = _run_env(tmp_path, sidecars)
    _claim_for(env, os.getpid())
    claim = holders.read_claim("scout", env)
    holders.write_claim("scout", {**claim, "pid": _dead_pid()}, env)
    assert not holders.front_door("scout", [], env).held


def test_a_respawn_replaces_a_claim_in_its_pane_too(tmp_path, sidecars, stranger):
    env = _run_env(tmp_path, sidecars, TMUX_PANE="%4")
    _claim_for(env, stranger, pane="%4")
    assert not holders.front_door("scout", [], env).held
    assert holders.front_door("scout", [], env, direct=False).held


VENDOR_RECORDS_PID = """#!/usr/bin/env bash
printf '%%s' "$$" > "%(dir)s/pid.$$"
"""


def test_a_free_hand_launch_claims_the_agent_before_it_execs(tmp_path, sidecars):
    """The claim names the launcher's pid, which exec keeps: the vendor's
    own `$$`. `via` says the front door wrote it."""
    env = _run_env(tmp_path, sidecars)
    result, record, _work = launch_e2e(tmp_path, "scout", extra_env=env,
                                       stub_body=VENDOR_RECORDS_PID)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    [pid_file] = record.glob("pid.*")
    claim = holders.read_claim("scout", env)
    assert claim["pid"] == int(pid_file.read_text())
    assert claim["via"] == "front door" and claim["agent"] == "scout"
    assert claim["sessionId"]                         # the minted id, recorded


@pytest.mark.parametrize("flags", [["--dry-run"], []])
def test_no_claim_for_a_dry_run_or_a_spawner_named_seat(tmp_path, sidecars, flags):
    env = _run_env(tmp_path, sidecars)
    tail = [] if flags else ["-n", "scout+1"]
    result, _record, _ = run(tmp_path, sidecars, *flags, "scout", *tail, extra=env)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "state" / "holders").exists()


def test_the_override_beside_a_holder_takes_no_claim(tmp_path, sidecars, stranger):
    env = _run_env(tmp_path, sidecars)
    write_sidecar(sidecars, pid=stranger, proc=proc_start(stranger))
    result, record, _ = run(tmp_path, sidecars, "--allow-second-holder", "scout", extra=env)
    assert result.returncode == 0 and vendor_ran(record)
    assert len(result.stderr.splitlines()) == 1
    assert "starting a second holder of scout beside" in result.stderr
    assert holders.read_claim("scout", env) is None


VENDOR_HOLDS = """#!/usr/bin/env bash
printf '%%s' "$$" > "%(dir)s/pid.$$"
sleep 4
"""


def test_two_simultaneous_launches_give_one_holder_and_one_refusal(tmp_path, sidecars):
    """Both start before either has a sidecar (none is ever written here):
    the agent's lock and claim alone decide. Whichever takes the lock first
    holds; the other waits for the lock, reads the live claim, exit 3."""
    env = _run_env(tmp_path, sidecars)
    record = tmp_path / "record"
    record.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(VENDOR_HOLDS % {"dir": record})
    stub.chmod(0o755)
    root = make_agents_root(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    full = clean_env(AGENTS_DIR=root, SESSIONS_DIR=os.path.join(root, "sessions"),
                     PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **env)
    launches = [subprocess.Popen(at_a_terminal("scout"), cwd=str(work), env=full,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)]
    outcomes = sorted((p.wait(timeout=TIMEOUT_S), p.stderr.read()) for p in launches)
    for p in launches:
        p.stdout.close()
        p.stderr.close()
    (ok, ok_err), (held, held_err) = outcomes
    assert (ok, held) == (0, EXIT_HELD), outcomes
    assert ok_err == ""
    assert "scout is already held by" in held_err and "refusing a second holder" in held_err
    assert len(list(record.glob("pid.*"))) == 1                # ONE vendor ran


# ---------- ST-260: fail OPEN, loudly, when the claim cannot be had ----------------

@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_claim_warns_once_and_launches(tmp_path, sidecars):
    env = _run_env(tmp_path, sidecars)
    _lock, path = holders.claim_paths("scout", env)
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    path.chmod(0)
    try:
        result, record, _ = run(tmp_path, sidecars, "scout", extra=env)
    finally:
        path.chmod(0o600)
    assert result.returncode == 0 and vendor_ran(record), result.stderr
    [line] = result.stderr.splitlines()
    assert line.startswith("session start: the second-holder check could not run: the claim ")
    assert line.endswith("-- launching unchecked")


def test_a_lock_that_cannot_be_opened_warns_once_and_launches(tmp_path, sidecars):
    env = _run_env(tmp_path, sidecars)
    lock, _claim = holders.claim_paths("scout", env)
    lock.mkdir(parents=True)                   # a directory where the lock file goes
    result, record, _ = run(tmp_path, sidecars, "scout", extra=env)
    assert result.returncode == 0 and vendor_ran(record), result.stderr
    [line] = result.stderr.splitlines()
    assert line.startswith("session start: scout's claim could not be taken (the lock file ")
    assert line.endswith("-- launching with no claim, so a second launch starting now is "
                         "not held off")
    assert holders.read_claim("scout", env) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes a 0500 dir")
def test_an_unwritable_claim_warns_once_and_launches(tmp_path, sidecars):
    env = _run_env(tmp_path, sidecars)
    lock, _claim = holders.claim_paths("scout", env)
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    lock.chmod(0o600)
    lock.parent.chmod(0o500)
    try:
        result, record, _ = run(tmp_path, sidecars, "scout", extra=env)
    finally:
        lock.parent.chmod(0o700)
    assert result.returncode == 0 and vendor_ran(record), result.stderr
    [line] = result.stderr.splitlines()
    assert line.startswith("session start: scout's claim could not be taken (the claim "
                           "could not be written (")


def test_a_name_that_cannot_carry_a_claim_warns_once_and_launches(tmp_path, sidecars):
    """`resolve_agent` accepts `a/b`; `claim_paths` refuses it. The door still
    reads the sidecars, and the launch goes ahead with no claim."""
    env = _run_env(tmp_path, sidecars)
    said: list[str] = []
    launch = SimpleNamespace(agent=SimpleNamespace(name="team/scout"), environ=env,
                             cli_args=[], resume=None)
    args = a.parse_args(["team/scout"])
    door, taken = st.door_start(args, launch, False, warn=said.append)
    assert taken is None and not door.held
    assert len(said) == 1 and "cannot name a holder's claim" in said[0]
    assert not (tmp_path / "state").exists()


# ---------- ST-260: the --in-tmux claim follows the pane, as the seat's does --------

def test_a_named_agents_claim_moves_to_the_pane(tmp_path, stranger):
    env = _state_env(tmp_path)
    table = procs.table()
    fields = default_seat.claim_fields(None, os.getpid(), table, env, via="front door")
    holders.write_claim("scout", fields, env)
    launch = SimpleNamespace(agent=SimpleNamespace(name="scout"), environ=env)
    start = st.SeatStart(launch, None, None, fields)
    said: list[str] = []
    placed = TmuxPlacement("eng", "scout", "@7", False, "%12", stranger)
    st._repoint(start, SimpleNamespace(identity=None), placed, warn=said.append)
    claim = holders.read_claim("scout", env)
    assert said == []
    assert claim["pid"] == stranger and claim["pane"] == "%12"
    assert claim["via"] == "tmux placement" and claim["agent"] == "scout"


def test_a_named_agents_failed_repoint_names_the_agent(tmp_path):
    env = _state_env(tmp_path)
    launch = SimpleNamespace(agent=SimpleNamespace(name="scout"), environ=env)
    start = st.SeatStart(launch, None, None, {"pid": os.getpid(), "procStart": "x"})
    said: list[str] = []
    placed = TmuxPlacement("eng", "scout", "@7", False, "%12", None)
    st._repoint(start, SimpleNamespace(identity=None), placed, warn=said.append)
    assert said == [("placed, but scout's claim could not move to the new pane (tmux printed "
                     "no pane pid or id for the new window); scout reads free until that "
                     "session writes its sidecar")]
    assert "default seat" not in said[0]
