"""The front door (SESH-497, ST-46 do-001): `session start <agent>` refuses a
hand launch of an agent a live Claude Code session already holds.

Re-measured on master b216a19 first (2030-08-02): a live sidecar
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

import pytest
from start_support import launch_e2e, recorded_argv

from sessiontools import procs
from sessiontools.start import EXIT_HELD, holders
from sessiontools.start import args as a
from sessiontools.start.errors import LaunchError

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
    {"proc": "Sat Dec  7 12:34:56 2030"},                 # the pid, reused
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
# pane left dead. A holder IN THE LAUNCHER'S OWN PANE is being replaced.
# Hermetic: TMUX_PANE alone, no TMUX - no tmux server is touched.

def test_a_same_pane_respawn_is_not_refused(tmp_path, sidecars):
    write_sidecar(sidecars, tmux="priv:@1.%4")
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


def test_the_own_pane_holder_is_dropped_but_another_still_refuses(tmp_path, sidecars):
    write_sidecar(sidecars, sid="aaaaaaaa-own", tmux="priv:@1.%4", age_s=60)
    write_sidecar(sidecars, sid="bbbbbbbb-other", tmux="priv:@2.%7", age_s=3 * 3600)
    result, record, _ = run(tmp_path, sidecars, "scout", extra={"TMUX_PANE": "%4"})
    assert result.returncode == EXIT_HELD
    # Names the OTHER pane's holder, and counts no "+1 more": the own-pane
    # session is being replaced, so it is not a holder at all.
    assert (f"scout is already held by session bbbbbbbb (pid {os.getpid()}, tmux %7), "
            "live 3h") in result.stderr
    assert "more live" not in result.stderr
    assert not vendor_ran(record)


def test_a_same_pane_dry_run_has_no_holder_line(tmp_path, sidecars):
    write_sidecar(sidecars, tmux="priv:@1.%4")
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

