"""The default seat's other roads (ST-256 do-004).

- A NAMED launch of the seat word, fresh or `--resume`, with or without
  claude's `-n`: the seat's own check decides (the claim, then the
  sidecars, under the narrow own-pane rule), never the front door's survey
  alone. A hand launch that meets a holder is refused, exit 3 (Q3); `-n`
  starts plain with HELD (Q16a); `--allow-second-holder` starts beside it
  and takes no claim (N2). A failed gate decides the line only: on a FREE
  seat the claim is taken all the same, and the line says it cannot hear
  yet (N1, ruled (B)); on a held seat ONE line prints, the holder's (N3);
  the held read is read-only (N4). A check that cannot run launches as
  named with UNSURE (Q17), beside the gate's line if that failed too (B2).
- The tmux path: the claim moves from the launcher to the new pane.
- A '-' resume of a session born as the seat prints RESUMED, naming the
  live holder when there is one (B8).

Hermetic, as tests/test_start_singleton.py is, whose helpers these reuse.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from start_support import STUB_SID, clean_env, session_cmd
from test_start_singleton import (
    HEAR,
    NO_BOX_STDERR,
    SEAT,
    SID,
    claim_of,
    dead_pid,
    proc_start,
    seat_env,
    write_seat_claim,
    write_sidecar,
)

from sessiontools import procs
from sessiontools import start as st
from sessiontools.start import default_seat, holders
from sessiontools.start.args import parse_args
from sessiontools.start.errors import EXIT_HELD, EXIT_OK, LaunchError
from sessiontools.start.identity import BIRTH_SETTINGS
from sessiontools.start.placement import TmuxPlacement
from sessiontools.start.plan import assemble

on_macos = pytest.mark.skipif(sys.platform != "darwin",
                              reason="the default seat is macOS-only until sessionmail SM-932")
REFUSE = "is already held by"
NAMED_NO_BOX = (f"this session is {SEAT}: box {SEAT} is missing, {HEAR}; "
                f"repair: message setup --box {SEAT}")
HOLDS_NO_BOX = (f"this session is {SEAT} and holds the default seat, but cannot hear yet: "
                f"box {SEAT} is missing, {HEAR}; repair: message setup --box {SEAT}")


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(default_seat, "current_platform", lambda: "darwin")


@pytest.fixture
def work(tmp_path, monkeypatch):
    directory = tmp_path / "work"
    directory.mkdir()
    monkeypatch.chdir(directory)
    return directory


def named(env: dict, *flags: str, in_tmux: bool = False):
    """`start.named_seat_start` for `session start FLAGS <word> ...`
    (FLAGS carry the word where it goes): (start, the lines said)."""
    said: list[str] = []
    seat = st.named_seat_start(parse_args(list(flags)), in_tmux, environ=env,
                               warn=said.append)
    return seat, said


def refused(env: dict, *flags: str, in_tmux: bool = False) -> str:
    with pytest.raises(LaunchError) as stop:
        named(env, *flags, in_tmux=in_tmux)
    assert stop.value.code == EXIT_HELD
    return str(stop.value)


def no_holders_dir(tmp_path: Path) -> bool:
    return not (tmp_path / "state" / holders.HOLDERS_DIR).exists()


def live_child():
    return subprocess.Popen(["sleep", "30"])


# ---------- a named launch, the gate passed -----------------------------------------

def test_named_seat_word_takes_a_free_seat(tmp_path, macos, work):
    env = seat_env(tmp_path)
    seat, said = named(env, SEAT)
    assert seat.launch.agent.name == SEAT and seat.claim is not None
    claim = claim_of(env)
    assert claim["sessionId"] == seat.minted.session_id and claim["pid"] == os.getpid()
    assert claim["via"] == "named start"
    assert said == [default_seat.taken_line(SEAT)]


def test_named_seat_word_while_held_is_refused(tmp_path, macos, work):
    env = seat_env(tmp_path)
    write_sidecar(env)
    line = refused(env, SEAT)
    assert line.startswith(f"{SEAT} {REFUSE} session {SID[:8]} (pid {os.getpid()}")
    assert line.endswith("pass --allow-second-holder to start one anyway")
    assert claim_of(env) is None


def test_named_seat_word_in_the_start_race_is_refused(tmp_path, macos, work):
    """A live CLAIM and no sidecar: the door alone would let this through."""
    env = seat_env(tmp_path)
    write_seat_claim(env)
    assert REFUSE in refused(env, SEAT)
    assert claim_of(env)["sessionId"] == SID       # untouched


def test_named_seat_word_from_the_holders_own_pane_is_refused(tmp_path, macos, work):
    """Typed in the seat's own tool shell: same pane, the holder is the
    launcher's ancestor (here: itself). Nested (direct) and --in-tmux."""
    env = seat_env(tmp_path, TMUX_PANE="%4")
    write_seat_claim(env, pane="%4")
    assert "tmux %4" in refused(env, SEAT)
    assert REFUSE in refused(env, "--in-tmux", SEAT, in_tmux=True)


def test_a_refusal_for_a_holder_with_no_id_says_so(tmp_path, macos, work):
    """B3: a claim-sourced holder with no id yet reads as HELD does."""
    env = seat_env(tmp_path)
    write_seat_claim(env, sid=None)
    line = refused(env, SEAT)
    assert f"held by a session (id not known yet) (pid {os.getpid()}" in line
    assert "session  (" not in line


def test_allow_second_holder_named_starts_beside_and_takes_no_claim(tmp_path, macos, work):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    before = holders.claim_paths(SEAT, env)[1].read_bytes()
    seat, [line] = named(env, "--allow-second-holder", SEAT)
    assert seat.launch.agent.name == SEAT and seat.claim is None
    assert line.startswith(f"starting a second holder of {SEAT} beside session {SID[:8]}")
    assert holders.claim_paths(SEAT, env)[1].read_bytes() == before


def test_spawner_named_seat_word_takes_a_free_seat(tmp_path, macos, work):
    env = seat_env(tmp_path)
    seat, said = named(env, SEAT, "-n", "seat-x")
    assert seat.launch.agent.name == SEAT and claim_of(env)["pid"] == os.getpid()
    assert said == [default_seat.taken_line(SEAT)]


def test_spawner_named_seat_word_while_held_starts_plain_with_held(tmp_path, macos, work):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    seat, [line] = named(env, SEAT, "-n", "seat-x")
    assert seat.launch.agent.name == "" and seat.claim is None
    assert "-n" in seat.launch.cli_args               # the spawner's name rides on
    assert line.startswith(f"starting plain: the default seat {SEAT} is held by session {SID}")


def test_named_seat_word_unsure_launches_named_and_takes_no_claim(tmp_path, macos, work,
                                                                  monkeypatch):
    """Q17: the check cannot run (an empty table): named, UNSURE, no claim."""
    monkeypatch.setattr(default_seat._procs, "table", dict)
    env = seat_env(tmp_path)
    seat, said = named(env, SEAT)
    assert seat.launch.agent.name == SEAT and seat.claim is None and claim_of(env) is None
    assert said == [(f"this session is {SEAT}: could not check who holds the default seat "
                     f"{SEAT} (the process table came back empty (`ps` failed?)), so it "
                     "takes no claim and may be a second holder")]


def test_a_busy_lock_on_the_named_road_is_q17(tmp_path, macos, work, monkeypatch):
    env = seat_env(tmp_path)
    monkeypatch.setattr(default_seat, "SEAT_LOCK_WAIT_S", 0.2)
    with holders.locked(SEAT, env):
        seat, [line] = named(env, SEAT)
    assert seat.launch.agent.name == SEAT and claim_of(env) is None
    assert "held the lock" in line and line.endswith("may be a second holder")


def test_a_named_start_takes_the_seat_from_a_stale_claim(tmp_path, macos, work):
    env = seat_env(tmp_path)
    write_seat_claim(env, pid=dead_pid())
    seat, _said = named(env, SEAT)
    assert seat.launch.agent.name == SEAT and claim_of(env)["pid"] == os.getpid()


# ---------- a named launch, the gate failed --------------------------------------------

def test_named_seat_word_without_a_box_takes_a_free_seat_with_the_line(tmp_path, macos, work):
    """N1 (B): the claim is taken all the same; the line says it holds the
    seat and cannot hear yet, and why."""
    env = seat_env(tmp_path, box=2, stderr=NO_BOX_STDERR)
    seat, said = named(env, SEAT)
    assert seat.launch.agent.name == SEAT and claim_of(env)["pid"] == os.getpid()
    assert said == [HOLDS_NO_BOX]


def test_named_seat_word_unboxed_on_a_free_seat(tmp_path, macos, work):
    """N5: UNBOXED, named, free: it takes the seat and says it may not hear."""
    env = seat_env(tmp_path, box=97)
    seat, [line] = named(env, SEAT)
    assert seat.launch.agent.name == SEAT and claim_of(env) is not None
    assert line == (f"this session is {SEAT} and holds the default seat, but may not hear "
                    f"yet: could not check box {SEAT} (`message box path` exited 97), so "
                    "this session may not hear Delta messages or calls")


def test_named_seat_word_on_linux_takes_the_claim(tmp_path, work, monkeypatch):
    """N1 (B) condition 2: on Linux the claim is taken all the same, so no
    second holder appears once sessionmail SM-932 lands."""
    monkeypatch.setattr(default_seat, "current_platform", lambda: "linux")
    env = seat_env(tmp_path)
    seat, [line] = named(env, SEAT)
    assert seat.launch.agent.name == SEAT and claim_of(env)["pid"] == os.getpid()
    assert line == (f"this session is {SEAT} and holds the default seat, but cannot hear "
                    f"yet: the default seat {SEAT} is macOS-only for now: sessionmail cannot "
                    f"yet read Linux start times, {HEAR}")
    assert not (tmp_path / "stubs" / "message.calls").exists()


def test_spawner_named_seat_word_with_a_failed_gate_launches_named(tmp_path, macos, work):
    """B1: `-n` on a failed gate with the seat free is a NAMED launch."""
    env = seat_env(tmp_path, box=2, stderr=NO_BOX_STDERR)
    seat, said = named(env, SEAT, "-n", "seat-x")
    assert seat.launch.agent.name == SEAT and claim_of(env) is not None
    assert said == [HOLDS_NO_BOX]


@pytest.mark.parametrize("gate", ["NO_BOX", "UNBOXED", "LINUX"])
@pytest.mark.parametrize("source", ["claim", "sidecar"])
def test_named_seat_word_with_a_failed_gate_still_meets_the_holder(tmp_path, work, monkeypatch,
                                                                   gate, source):
    """M1 + N3 + N4: the holder check runs whatever the gate says, READ-ONLY
    (no lock file, no claim, no holders/), and ONE line prints, the holder's:
    a hand launch exits 3 with the door's line; `-n` starts plain with HELD;
    the flag starts beside with the override line (N2)."""
    platform = "linux" if gate == "LINUX" else "darwin"
    monkeypatch.setattr(default_seat, "current_platform", lambda: platform)
    box = {"NO_BOX": 2, "UNBOXED": 97, "LINUX": 0}[gate]
    env = seat_env(tmp_path, box=box, stderr=NO_BOX_STDERR if box == 2 else "")
    if source == "claim":
        # holders/ exists here (the claim is in it): the claim is untouched
        # and no lock file appears.
        write_seat_claim(env)
        before = holders.claim_paths(SEAT, env)[1].read_bytes()
    else:
        write_sidecar(env)
    assert REFUSE in refused(env, SEAT)
    seat, [line] = named(env, SEAT, "-n", "seat-x")
    assert seat.launch.agent.name == "" and line.startswith("starting plain: the default seat")
    seat, [line] = named(env, "--allow-second-holder", SEAT)
    assert seat.launch.agent.name == SEAT and line.startswith("starting a second holder")
    lock, claim = holders.claim_paths(SEAT, env)
    assert not lock.exists()
    if source == "claim":
        assert claim.read_bytes() == before
    else:
        assert no_holders_dir(tmp_path)


def test_named_start_in_the_holders_shell_with_a_failed_gate_is_refused(tmp_path, macos, work):
    """N5: the narrow own-pane rule on the failed-gate road."""
    env = seat_env(tmp_path, box=2, stderr=NO_BOX_STDERR, TMUX_PANE="%4")
    write_sidecar(env, tmux="priv:@1.%4")
    assert REFUSE in refused(env, SEAT)
    assert no_holders_dir(tmp_path)


def test_a_failed_gate_and_an_unmeasured_check_print_both_lines(tmp_path, macos, work,
                                                                monkeypatch):
    """B2 + N5: two facts, two lines; named, no claim, nothing written."""
    monkeypatch.setattr(default_seat._procs, "table", dict)
    env = seat_env(tmp_path, box=2, stderr=NO_BOX_STDERR)
    seat, said = named(env, SEAT)
    assert seat.launch.agent.name == SEAT and seat.claim is None
    assert said == [NAMED_NO_BOX,
                    (f"this session is {SEAT}: could not check who holds the default seat "
                     f"{SEAT} (the process table came back empty (`ps` failed?)), so it "
                     "takes no claim and may be a second holder")]
    assert not (tmp_path / "state").exists()


# ---------- named resume and --continue --------------------------------------------------

def test_named_resume_takes_a_free_seat(tmp_path, macos, work):
    """An unkeyed resume keeps its own id: the claim names the resumed one."""
    env = seat_env(tmp_path)
    seat, said = named(env, SEAT, "--resume", SID)
    assert seat.launch.agent.name == SEAT and seat.minted is None
    claim = claim_of(env)
    assert claim["sessionId"] == SID and claim["via"] == "named resume"
    assert said == [default_seat.taken_line(SEAT)]


def test_named_resume_while_held_is_refused(tmp_path, macos, work):
    env = seat_env(tmp_path)
    child = live_child()
    try:
        holders.write_claim(SEAT, {"sessionId": "other", "pid": child.pid,
                                   "procStart": proc_start(child.pid)}, env)
        assert REFUSE in refused(env, SEAT, "--resume", SID)
    finally:
        child.kill()
        child.wait()


def test_named_continue_takes_a_free_seat_with_no_id(tmp_path, macos, work):
    """`--continue`: the vendor picks the session, so the claim names no id;
    a later HELD finds it in the sidecar of the claim's pid."""
    env = seat_env(tmp_path)
    seat, _said = named(env, SEAT, "--continue")
    assert claim_of(env)["sessionId"] is None and seat.claim["sessionId"] is None
    _other, [line] = named(env, SEAT, "-n", "x")
    assert "held by a session (id not known yet)" in line
    write_sidecar(env, sid=SID)
    _other, [line] = named(env, SEAT, "-n", "x")
    assert f"held by session {SID} (pid {os.getpid()}" in line


def test_named_seat_resume_opens_no_system_prompt_door(tmp_path, macos, work):
    """A shadowing body on a named resume: no append door in the argv (the
    session keeps the prompt it was born with, MEASURED in f02)."""
    env = seat_env(tmp_path)
    (tmp_path / "agents" / f"{SEAT}.md").write_text(
        "---\ndescription: mine\n---\nYou are my default seat.\n")
    seat, _said = named(env, SEAT, "--resume", SID)
    plan = assemble(seat.launch, seat.minted)
    assert "--append-system-prompt-file" not in plan.argv
    assert "You are my default seat." not in " ".join(plan.argv)
    assert "--agent" in plan.argv


# ---------- RESUMED: a '-' resume of a seat-born session --------------------------------

def born(env: dict, sid: str, data: dict) -> None:
    path = Path(env["SESSIONS_DIR"], sid, BIRTH_SETTINGS)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def dash_resume(env: dict, monkeypatch, capsys, *flags: str) -> tuple[int, str]:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    code = st.main(["--dry-run", "-", *flags])
    return code, capsys.readouterr().err


def test_dash_resume_of_a_seat_born_session_is_plain_with_the_line(tmp_path, macos, work,
                                                                   monkeypatch, capsys):
    env = seat_env(tmp_path)
    born(env, SID, {"agent": SEAT})
    code, err = dash_resume(env, monkeypatch, capsys, "--resume", SID)
    assert code == EXIT_OK
    assert (f"session start: resuming plain: session {SID[:8]} was started as {SEAT}, the "
            f"default seat, and a resume through '-' does not take it, {HEAR}; "
            f"'session start {SEAT} --resume {SID}' takes it back while it is free") in err
    assert claim_of(env) is None and no_holders_dir(tmp_path)
    assert not (tmp_path / "stubs" / "message.calls").exists()     # no gate on '-'


def test_resumed_names_the_live_holder(tmp_path, macos, work, monkeypatch, capsys):
    """B8: the same read-only check as the dry run names who holds it now."""
    env = seat_env(tmp_path)
    born(env, SID, {"agent": SEAT})
    child = live_child()
    try:
        holders.write_claim(SEAT, {"sessionId": STUB_SID, "pid": child.pid,
                                   "procStart": proc_start(child.pid)}, env)
        _code, err = dash_resume(env, monkeypatch, capsys, "--resume", SID)
    finally:
        child.kill()
        child.wait()
    assert (f"; the default seat {SEAT} is held now by session {STUB_SID} "
            f"(pid {child.pid}") in err
    assert "takes it back" not in err


@pytest.mark.parametrize("birth", [{"agent": "scout"}, {"agent": SEAT, "seat": "proj/role"}, None])
def test_dash_resume_of_another_session_prints_nothing(tmp_path, macos, work, monkeypatch,
                                                       capsys, birth):
    env = seat_env(tmp_path)
    if birth is not None:
        born(env, SID, birth)
    _code, err = dash_resume(env, monkeypatch, capsys, "--resume", SID)
    assert "resuming plain" not in err


def test_dash_continue_prints_no_resumed_line(tmp_path, macos, work, monkeypatch, capsys):
    env = seat_env(tmp_path)
    _code, err = dash_resume(env, monkeypatch, capsys, "--continue")
    assert "resuming plain" not in err


# ---------- the dry run on the named road (N7) -----------------------------------------

def dry(env: dict, monkeypatch, capsys, *argv: str) -> tuple[int, str]:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    code = st.main(["--dry-run", *argv])
    captured = capsys.readouterr()
    return code, captured.out


def test_dry_run_on_the_named_road(tmp_path, macos, work, monkeypatch, capsys):
    env = seat_env(tmp_path, box=2, stderr=NO_BOX_STDERR)
    code, out = dry(env, monkeypatch, capsys, SEAT)
    assert code == EXIT_OK
    assert (f"seat:     {SEAT}, the default seat: a live launch takes it and says: "
            f"{HOLDS_NO_BOX}") in out
    assert not (tmp_path / "state").exists()
    write_sidecar(env)
    _code, out = dry(env, monkeypatch, capsys, SEAT)
    assert f"seat:     a live launch would REFUSE (exit {EXIT_HELD}): {SEAT} {REFUSE}" in out
    _code, out = dry(env, monkeypatch, capsys, "--allow-second-holder", SEAT)
    assert "seat:     a live launch would start ANYWAY: starting a second holder" in out
    _code, out = dry(env, monkeypatch, capsys, SEAT, "-n", "x")
    assert "seat:     not taken: a live launch starts plain and says: starting plain:" in out
    assert "--agents" not in out
    assert no_holders_dir(tmp_path)


def test_a_named_dry_run_never_exits_3(tmp_path, macos, work, monkeypatch, capsys):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    code, out = dry(env, monkeypatch, capsys, SEAT)
    assert code == EXIT_OK and "would REFUSE" in out


def test_a_live_named_refusal_exits_3_through_main(tmp_path, macos, work, monkeypatch, capsys):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert st.main([SEAT]) == EXIT_HELD
    assert f"session start: {SEAT} {REFUSE}" in capsys.readouterr().err


# ---------- the tmux path: the claim moves to the pane ---------------------------------

def test_bare_in_tmux_from_the_holders_own_pane_is_held(tmp_path, macos, work):
    """A tmux placement never replaces its own pane: a holder in the
    launcher's pane that is NOT its ancestor still holds the seat."""
    from test_start_singleton import bare
    child = live_child()
    try:
        env = seat_env(tmp_path, TMUX_PANE="%4")
        holders.write_claim(SEAT, {"sessionId": SID, "pid": child.pid,
                                   "procStart": proc_start(child.pid), "pane": "%4"}, env)
        launch, _minted, _line, [line] = bare(env, "--in-tmux", in_tmux=True)
        assert launch.agent.name == "" and f"held by session {SID}" in line
        launch, _minted, _line, _said = bare(env)          # direct: it is replaced
        assert launch.agent.name == SEAT
    finally:
        child.kill()
        child.wait()


def _placed(pid: int | None, pane: str = "%12") -> TmuxPlacement:
    return TmuxPlacement("eng", SEAT, "@7", False, pane, pid)


class _Plan:
    def __init__(self, identity):
        self.identity = identity


def test_the_claim_moves_to_the_pane(tmp_path, macos, work):
    env = seat_env(tmp_path, TMUX_PANE="%1")
    seat = st.bare_start(parse_args(["--in-tmux"]), True, environ=env, warn=lambda _l: None)
    assert seat.claim["pid"] == os.getpid() and seat.claim["pane"] == "%1"
    child = live_child()
    try:
        said: list[str] = []
        st._repoint(seat, _Plan(seat.minted), _placed(child.pid), warn=said.append)
        claim = claim_of(env)
        assert said == []
        assert claim["pid"] == child.pid and claim["pane"] == "%12"
        assert claim["procStart"] == proc_start(child.pid)
        assert claim["sessionId"] == seat.minted.session_id and claim["via"] == "tmux placement"
        assert claim["agent"] == SEAT and claim["v"] == 1
    finally:
        child.kill()
        child.wait()


def test_a_pane_minted_claim_has_no_id_and_held_says_so(tmp_path, macos, work):
    """A keyed pane mint: the launcher never knows the id, so the claim has
    `sessionId` null, and HELD says `id minted in the pane`."""
    env = seat_env(tmp_path)
    write_seat_claim(env, sid=None)
    launcher = claim_of(env)
    child = live_child()
    try:
        assert default_seat.repoint(SEAT, launcher, child.pid, "%12", None, env) is None
        from test_start_singleton import bare
        _launch, _minted, _line, [line] = bare(env)
        assert "held by a session (id minted in the pane)" in line
        assert f"pid {child.pid}, tmux %12" in line
    finally:
        child.kill()
        child.wait()


@pytest.mark.parametrize("case, why", [
    ("not ours", "the claim no longer names this launcher"),
    ("dead pane", "is not running"),
    ("no pane", "tmux printed no pane pid or id"),
])
def test_a_failed_repoint_says_so(tmp_path, macos, work, case, why):
    """B4: the re-point fails after the window opened: it says so on
    stderr, and the launch is not unwound."""
    env = seat_env(tmp_path)
    write_seat_claim(env)
    launcher = dict(claim_of(env))
    if case == "not ours":
        launcher["procStart"] = "Sun Jun  9 17:00:00 2030"
    pid = None if case == "no pane" else dead_pid() if case == "dead pane" else os.getpid()
    result = default_seat.repoint(SEAT, launcher, pid, "%12", None, env)
    assert result is not None and why in result
    assert claim_of(env)["pid"] == os.getpid()           # unchanged
    line = default_seat.repoint_failed_line(SEAT, result)
    assert line.startswith(f"placed, but the default seat {SEAT}'s claim could not move")


def test_a_plain_tmux_start_moves_no_claim(tmp_path, macos, work):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    said: list[str] = []
    seat = st.bare_start(parse_args(["--in-tmux"]), True, environ=env, warn=said.append)
    assert seat.claim is None
    st._repoint(seat, _Plan(seat.minted), _placed(os.getpid()), warn=said.append)
    assert claim_of(env)["sessionId"] == SID


# ---------- real launches: one holder ---------------------------------------------------

SEAT_STUB = """#!/usr/bin/env bash
printf '%%s\\0' "$@" > "%(dir)s/argv.$$"
case " $* " in
  *" --agents "*) for _ in $(seq 300); do [ -e "%(dir)s/release" ] && exit 0; sleep 0.1; done ;;
esac
"""

#: A `message` whose exit is the launch's own `$MESSAGE_EXIT` (default 0).
PER_LAUNCH_MESSAGE = """#!/bin/sh
case "${MESSAGE_EXIT:-0}" in
  0) printf '/box/x' ;;
  2) printf "message box path: no box 'x' under /nowhere\\n" >&2 ;;
esac
exit "${MESSAGE_EXIT:-0}"
"""


def race(tmp_path: Path, launches: list[tuple[list[str], dict]]) -> tuple[list, dict]:
    """LAUNCHES (argv after `session start`, extra env) released together on
    one barrier file; the seat's stub stays alive until all others exit.
    Returns ([(code, stderr)], {pid: argv})."""
    record = tmp_path / "record"
    record.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(SEAT_STUB % {"dir": record})
    (bin_dir / "message").write_text(PER_LAUNCH_MESSAGE)
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    base = clean_env(**seat_env(tmp_path, path=f"{bin_dir}{os.pathsep}{os.environ['PATH']}"))
    (tmp_path / "work").mkdir(exist_ok=True)
    barrier = tmp_path / "go"
    runs = []
    for argv, extra in launches:
        command = " ".join(f"'{part}'" for part in session_cmd(*argv))
        script = f'while [ ! -e "{barrier}" ]; do sleep 0.01; done; exec {command}'
        runs.append(subprocess.Popen(["sh", "-c", script], cwd=tmp_path / "work",
                                     env={**base, **extra}, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True))
    time.sleep(0.5)
    barrier.touch()
    try:
        deadline = time.monotonic() + 120
        while (sum(run.poll() is not None for run in runs) < len(runs) - 1
               and time.monotonic() < deadline):
            time.sleep(0.1)
    finally:
        (record / "release").touch()
    results = [(run.wait(timeout=60), run.communicate()[1]) for run in runs]
    argvs = {path.name.split(".")[1]: path.read_text().split("\0")[:-1]
             for path in record.glob("argv.*")}
    return results, argvs


@on_macos
def test_two_named_failed_gate_launches_give_one_holder(tmp_path):
    """N1 (B): NO_BOX on both; one takes the seat, the other is refused."""
    results, argvs = race(tmp_path, [([SEAT], {"MESSAGE_EXIT": "2"})] * 2)
    assert sorted(code for code, _err in results) == [0, EXIT_HELD], results
    assert sum("--agent" in argv for argv in argvs.values()) == 1, argvs
    assert any("holds the default seat, but cannot hear yet" in err for _c, err in results)


@on_macos
def test_an_unboxed_named_launch_racing_a_bare_start_gives_one_holder(tmp_path):
    results, argvs = race(tmp_path, [([SEAT], {"MESSAGE_EXIT": "97"}), ([], {})])
    assert sum("--agent" in argv for argv in argvs.values()) == 1, (argvs, results)
    (named_code, named_err), (bare_code, bare_err) = results
    assert bare_code == 0
    if named_code == EXIT_HELD:          # the bare start won
        assert "this session is" in bare_err and REFUSE in named_err
    else:                                # the named one won; the bare start is plain
        assert named_code == 0 and "is held by" in bare_err


@on_macos
def test_tmux_named_start_claims_the_pane(tmp_path):
    """On the conftest's private server: the claim ends naming the pane's
    pid and id, not the launcher's (which has exited). The PATH wall keeps
    its own `message` (a test's stub would be refused), so the gate fails
    here, and the NAMED road takes the claim all the same (N1 (B))."""
    from test_start_placement import PANE_STUB, pane_ran, tmux_launch_e2e
    state = tmp_path / "state"
    result, record, _work = tmux_launch_e2e(
        tmp_path, "--tmux-target", "eng", SEAT, stub_body=PANE_STUB,
        extra_env={"SESSION_STATE_DIR": str(state),
                   "SESSION_SIDECAR_DIR": str(tmp_path / "sidecars")})
    assert result.returncode == 0, result.stderr
    assert pane_ran(record), result.stderr
    assert f"this session is {SEAT} and holds the default seat" in result.stderr
    assert "could not move" not in result.stderr
    claim = json.loads((state / "holders" / f"{SEAT}.json").read_text())
    assert claim["pid"] == int((record / "pid").read_text())
    assert claim["pane"].startswith("%") and claim["via"] == "tmux placement"
    assert claim["sessionId"] == (record / "session-id-env").read_text()
    assert holders.claim_is_live(claim, procs.table())
