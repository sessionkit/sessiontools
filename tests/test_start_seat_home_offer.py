"""The home-folder offer on the default seat's roads.

The seat's check and claim run under the seat's lock; the offer is a
question that waits on a person, so it is asked after the claim, with the
lock let go, and before the mint. A launch the seat refuses is asked
nothing; a launch that starts plain beside a holder, a failed gate or an
unmeasured check is asked before its own mint. A dry run asks nothing.
Ctrl-C at the question exits 130 with nothing minted: the claim already
written names this pid and goes stale with it.

The roads are driven with `offer` replaced by a spy that records what was
true when it was asked; `main()` is driven at a fake terminal with
`execute` replaced, as tests/test_start_homefolder.py does."""

from __future__ import annotations

import os
import sys

import pytest
from test_start_homefolder import (
    ASK_MAKE,
    PREFIX,
    WORDING,
    Interrupting,
    Terminal,
)
from test_start_singleton import (
    SEAT,
    SID,
    claim_of,
    dead_pid,
    proc_start,
    seat_env,
    write_seat_claim,
)

from sessiontools import start as st
from sessiontools.start import default_seat, holders
from sessiontools.start.args import parse_args
from sessiontools.start.errors import EXIT_HELD, EXIT_INTERRUPTED, EXIT_OK, LaunchError
from sessiontools.start.homefolder import OFFER_SOURCE


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(default_seat, "current_platform", lambda: "darwin")


@pytest.fixture
def work(tmp_path, monkeypatch):
    directory = tmp_path / "work"
    directory.mkdir()
    monkeypatch.chdir(directory)
    return directory


@pytest.fixture
def mints(monkeypatch):
    """Every launch minted, in order; the real mint still runs."""
    seen = []
    real = st.mint

    def counting(launch):
        seen.append(launch)
        return real(launch)

    monkeypatch.setattr(st, "mint", counting)
    return seen


class Spy:
    """An `offer` that records, at the moment it is asked: the launch, the
    claim on disk, whether the seat's lock is free, and how many mints had
    run. It answers with ANSWER(launch), default a no."""

    def __init__(self, env: dict, mints: list, answer=None) -> None:
        self.env, self.mints, self.answer = env, mints, answer
        self.asked: list[dict] = []

    def __call__(self, launch):
        with holders.locked(SEAT, self.env, wait=0) as lock:
            free = lock.ok
        self.asked.append({"launch": launch, "claim": claim_of(self.env),
                           "lock_free": free, "mints": len(self.mints)})
        return self.answer(launch) if self.answer else (launch, False)


def bare(env: dict, offer, *flags: str):
    said: list[str] = []
    seat = st.bare_start(parse_args(list(flags)), False, environ=env, warn=said.append,
                         offer=offer)
    return seat, said


def named(env: dict, offer, *flags: str):
    said: list[str] = []
    seat = st.named_seat_start(parse_args(list(flags)), False, environ=env,
                               warn=said.append, offer=offer)
    return seat, said


def to_work(directory):
    def answer(launch):
        return launch._replace(work_dir=str(directory), work_dir_source=OFFER_SOURCE), True
    return answer


# ---------- the bare road ----------------------------------------------------------

def test_bare_free_seat_asks_after_the_claim_outside_the_lock_before_the_mint(
        tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    spy = Spy(env, mints)
    seat, said = bare(env, spy)
    [asked] = spy.asked
    assert asked["launch"].agent.name == SEAT
    assert asked["claim"]["pid"] == os.getpid() and asked["claim"]["sessionId"] is None
    assert asked["lock_free"] and asked["mints"] == 0
    assert claim_of(env)["sessionId"] == seat.minted.session_id   # recorded after
    assert said == [default_seat.taken_line(SEAT)]


def test_bare_yes_reaches_the_seat_launch(tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    seat, _said = bare(env, Spy(env, mints, to_work(elsewhere)))
    assert seat.launch.agent.name == SEAT and seat.noticed
    assert seat.launch.work_dir == str(elsewhere)
    assert seat.launch.work_dir_source == OFFER_SOURCE
    assert seat.claim["sessionId"] == seat.minted.session_id


def test_bare_held_seat_asks_the_plain_launch(tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    before = holders.claim_paths(SEAT, env)[1].read_bytes()
    spy = Spy(env, mints)
    seat, [line] = bare(env, spy)
    [asked] = spy.asked
    assert asked["launch"].agent.name == "" and asked["mints"] == 0
    assert seat.launch.agent.name == "" and line.startswith("starting plain:")
    assert holders.claim_paths(SEAT, env)[1].read_bytes() == before


def test_bare_failed_gate_asks_the_plain_launch(tmp_path, work, mints, monkeypatch):
    monkeypatch.setattr(default_seat, "current_platform", lambda: "linux")
    env = seat_env(tmp_path)
    spy = Spy(env, mints)
    seat, [_line] = bare(env, spy)
    [asked] = spy.asked
    assert asked["launch"].agent.name == "" and asked["claim"] is None
    assert seat.launch.agent.name == "" and seat.claim is None


def test_bare_dry_run_asks_nothing(tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    spy = Spy(env, mints)
    bare(env, spy, "--dry-run")
    assert spy.asked == []


def test_bare_ctrl_c_mints_nothing_and_the_claim_names_this_pid(tmp_path, macos, work, mints):
    env = seat_env(tmp_path)

    def interrupted(_launch):
        raise st._Interrupted

    with pytest.raises(st._Interrupted):
        bare(env, interrupted)
    assert mints == []
    claim = claim_of(env)
    assert claim["pid"] == os.getpid() and claim["sessionId"] is None


def test_bare_claim_fields_unmeasurable_starts_plain_and_asks(
        tmp_path, macos, work, mints, monkeypatch):
    """A bare start never fails for the seat's sake: when the claim's own
    fields cannot be had (this pid missing from the table), it starts
    plain with ONE unsure line, takes no claim, and is still asked before
    its mint. It never escapes as an OSError."""
    env = seat_env(tmp_path)

    def unmeasurable(*_a, **_k):
        raise OSError("pid 1 is not in the process table")

    monkeypatch.setattr(default_seat, "claim_fields", unmeasurable)
    spy = Spy(env, mints)
    seat, said = bare(env, spy)
    [asked] = spy.asked
    assert asked["launch"].agent.name == "" and asked["mints"] == 0
    assert seat.launch.agent.name == "" and len(mints) == 1
    assert seat.claim is None and claim_of(env) is None
    assert said == [default_seat.unsure_line(
        SEAT, "the claim could not be written (pid 1 is not in the process table)")]


def test_a_claim_that_moved_on_during_the_question_is_not_overwritten(
        tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    other = dead_pid()

    def moved(launch):
        holders.write_claim(SEAT, {"sessionId": SID, "pid": other,
                                   "procStart": proc_start(os.getpid()),
                                   "pane": None, "via": default_seat.VIA_BARE}, env)
        return launch, False

    seat, said = bare(env, moved)
    assert claim_of(env)["pid"] == other and claim_of(env)["sessionId"] == SID
    assert said[0] == ("the default seat's claim could not record the session id "
                       "(the claim no longer names this launcher); it still holds the "
                       f"seat by pid {os.getpid()}")
    assert seat.claim["sessionId"] is None


# ---------- the named road ---------------------------------------------------------

def test_named_free_seat_asks_after_the_claim_outside_the_lock_before_the_mint(
        tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    spy = Spy(env, mints)
    seat, said = named(env, spy, SEAT)
    [asked] = spy.asked
    assert asked["claim"]["pid"] == os.getpid() and asked["claim"]["via"] == "named start"
    assert asked["lock_free"] and asked["mints"] == 0
    assert claim_of(env)["sessionId"] == seat.minted.session_id
    assert said == [default_seat.taken_line(SEAT)]


def test_named_hand_launch_on_a_held_seat_is_refused_and_asked_nothing(
        tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    spy = Spy(env, mints)
    with pytest.raises(LaunchError) as stop:
        named(env, spy, SEAT)
    assert stop.value.code == EXIT_HELD
    assert spy.asked == [] and mints == []


def test_named_second_holder_is_asked_and_takes_no_claim(tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    before = holders.claim_paths(SEAT, env)[1].read_bytes()
    spy = Spy(env, mints)
    seat, _said = named(env, spy, "--allow-second-holder", SEAT)
    [asked] = spy.asked
    assert asked["launch"].agent.name == SEAT and asked["mints"] == 0
    assert seat.claim is None
    assert holders.claim_paths(SEAT, env)[1].read_bytes() == before


def test_named_spawner_on_a_held_seat_asks_the_plain_launch(tmp_path, macos, work, mints):
    env = seat_env(tmp_path)
    write_seat_claim(env)
    spy = Spy(env, mints)
    seat, _said = named(env, spy, SEAT, "-n", "seat-x")
    [asked] = spy.asked
    assert asked["launch"].agent.name == "" and asked["mints"] == 0
    assert seat.launch.agent.name == ""


def test_named_unsure_check_asks_before_the_mint(tmp_path, macos, work, mints, monkeypatch):
    env = seat_env(tmp_path)
    monkeypatch.setattr(holders, "locked", _busy_lock)
    asked_with = []

    def recording(launch):
        asked_with.append({"launch": launch, "mints": len(mints)})
        return launch, False

    seat, _said = named(env, recording, SEAT)
    [asked] = asked_with
    assert asked["launch"].agent.name == SEAT and asked["mints"] == 0
    assert seat.claim is None and claim_of(env) is None


def test_named_claim_fields_unmeasurable_launches_as_named_and_asks(
        tmp_path, macos, work, mints, monkeypatch):
    """Q17 on the named road: when the claim's own fields cannot be had
    (this pid missing from the table), the launch goes ahead as named
    with the unsure line, takes no claim, and is asked before its mint.
    It never escapes as an OSError."""
    env = seat_env(tmp_path)

    def unmeasurable(*_a, **_k):
        raise OSError("pid 1 is not in the process table")

    monkeypatch.setattr(default_seat, "claim_fields", unmeasurable)
    spy = Spy(env, mints)
    seat, said = named(env, spy, SEAT)
    [asked] = spy.asked
    assert asked["launch"].agent.name == SEAT and asked["mints"] == 0
    assert seat.claim is None and claim_of(env) is None
    assert said == [default_seat.named_unsure_line(
        SEAT, "the claim could not be written (pid 1 is not in the process table)")]


class _busy_lock:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return holders.Locked(False, "the lock is busy")

    def __exit__(self, *_exc):
        return False


# ---------- through main(), at a fake terminal --------------------------------------

@pytest.fixture
def bare_at_home(tmp_path, macos, monkeypatch):
    """main([]) - a bare start - in the home folder on macOS at a fake
    terminal; returns a runner taking stdin, giving (code, plan, stderr)."""
    home = tmp_path / "home"
    home.mkdir()
    env = seat_env(tmp_path, HOME=str(home))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.chdir(home)
    plans = []
    monkeypatch.setattr(st, "execute", plans.append)

    def run(stdin):
        err = Terminal()
        monkeypatch.setattr(sys, "stdin", stdin if isinstance(stdin, Terminal)
                            else Terminal(stdin))
        monkeypatch.setattr(sys, "stderr", err)
        code = st.main([])
        return code, (plans[0] if plans else None), err.getvalue()

    run.home, run.env = home, env
    return run


def test_main_bare_yes_takes_the_seat_in_the_work_folder(bare_at_home):
    code, plan, err = bare_at_home("\n")
    home = bare_at_home.home
    assert code == EXIT_OK
    assert plan.agent.name == SEAT
    assert plan.work_dir == str(home / "work") and plan.work_dir_source == OFFER_SOURCE
    assert f"cd {home / 'work'}" in plan.script
    assert claim_of(bare_at_home.env)["sessionId"] == plan.identity.session_id
    assert err.count(WORDING) == 1
    taken = PREFIX + default_seat.taken_line(SEAT)
    assert err.index(ASK_MAKE) < err.index(f"{PREFIX}made ~/work; starting there") < err.index(taken)


def test_main_bare_no_takes_the_seat_at_home_warning_once(bare_at_home):
    code, plan, err = bare_at_home("n\n")
    assert (code, plan.agent.name, plan.work_dir) == (EXIT_OK, SEAT, str(bare_at_home.home))
    assert err.count(WORDING) == 1 and err.count(ASK_MAKE) == 1
    assert not (bare_at_home.home / "work").exists()


def test_main_bare_ctrl_c_exits_130_with_nothing_minted(bare_at_home, mints):
    code, plan, err = bare_at_home(Interrupting())
    assert code == EXIT_INTERRUPTED == 130
    assert (plan, mints) == (None, [])
    assert "Traceback" not in err
    assert claim_of(bare_at_home.env)["pid"] == os.getpid()   # stale once this pid ends
    assert not (bare_at_home.home / "work").exists()


def test_main_bare_held_seat_still_offers(bare_at_home):
    write_seat_claim(bare_at_home.env)
    code, plan, err = bare_at_home("\n")
    assert (code, plan.agent.name) == (EXIT_OK, "")
    assert plan.work_dir == str(bare_at_home.home / "work")
    assert "starting plain:" in err and err.count(ASK_MAKE) == 1


def test_main_ctrl_c_elsewhere_is_not_130(bare_at_home, monkeypatch):
    """Only the question's Ctrl-C is turned into 130."""
    def boom(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(default_seat, "gate", boom)
    with pytest.raises(KeyboardInterrupt):
        bare_at_home("\n")
