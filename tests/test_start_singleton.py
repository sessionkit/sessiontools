"""The default seat's singleton on the direct path (ST-256 do-003).

A bare `session start` takes the host's one default seat when no live
session holds it: the gates first (macOS, then `message box path <word>`),
then the claim under the seat's lock, written before the exec. Held, a
failed gate, or a check that cannot run: it starts plain with ONE line.

Hermetic. `message` is a stub on PATH that records its argv and answers as
told; the state dir, the sidecars and the sessions root are the test's own.
A live holder is THIS pytest process (or a child `sleep`), described by a
claim or a sidecar with its real `procStart`; a dead pid is a reaped
child's. The in-process tests call `start.bare_start`, which returns the
launch instead of exec'ing it, with the platform patched to macOS; the
subprocess tests exec a stub vendor and run on macOS only (the seat is
macOS-only until sessionmail SM-932).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from start_support import (
    STUB_SID,
    at_a_terminal,
    clean_env,
    launch_e2e,
    make_agents_root,
    write_key_init_stub,
)

from sessiontools import procs
from sessiontools import start as st
from sessiontools.start import default_seat, holders
from sessiontools.start.args import parse_args
from sessiontools.start.context import BARE_CONVERSE_SIGNAL, IDLE_BOOT_TRIGGER
from sessiontools.start.errors import EXIT_OK, EXIT_USAGE, LaunchError
from sessiontools.start.identity import BIRTH_SETTINGS, SessionIdentity
from sessiontools.start.plan import prepare

SEAT = default_seat.word()
SID = "37f09460-1111-2222-3333-444455556666"
HEAR = "so this session will NOT hear Delta messages or calls"
on_macos = pytest.mark.skipif(sys.platform != "darwin",
                              reason="the default seat is macOS-only until sessionmail SM-932")

MESSAGE_STUB = """#!/bin/sh
printf '%%s\\n' "$*" >> "%(calls)s"
printf '%%s' "%(stdout)s"
printf '%%s' "%(stderr)s" >&2
exit %(code)d
"""
NO_BOX_STDERR = "message box path: no box 'x' under /nowhere/.mail/agents\n"
USAGE_STDERR = ("Usage: message box list|path [args]\nTry 'message box --help' for help.\n\n"
                "Error: No such command 'path'.\n")


# ---------- fixtures ------------------------------------------------------------

def message_stub(directory: Path, code: int = 0, stderr: str = "") -> Path:
    """A `message` on PATH in DIRECTORY: it records each call's argv in
    `message.calls` beside it, prints STDERR and exits CODE."""
    directory.mkdir(parents=True, exist_ok=True)
    calls = directory / "message.calls"
    stub = directory / "message"
    stub.write_text(MESSAGE_STUB % {"calls": calls, "stdout": f"/box/{SEAT}" if code == 0 else "",
                                    "stderr": stderr, "code": code})
    stub.chmod(0o755)
    return calls


def seat_env(tmp_path: Path, box: int = 0, stderr: str = "", path: str | None = None,
             **extra: str) -> dict:
    """An environ for one launch: private agents, sessions, state and
    sidecar dirs, and the `message` stub first on PATH."""
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    stubs = tmp_path / "stubs"
    message_stub(stubs, box, stderr)
    env = {"AGENTS_DIR": str(agents), "SESSIONS_DIR": str(tmp_path / "sessions"),
           "SESSION_STATE_DIR": str(tmp_path / "state"),
           "SESSION_SIDECAR_DIR": str(tmp_path / "sidecars"),
           "SESSION_KEY_INIT": os.environ["SESSION_KEY_INIT"],
           "HOME": os.environ["HOME"],
           "PATH": path if path is not None else f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    env.update(extra)
    return env


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(default_seat, "current_platform", lambda: "darwin")


@pytest.fixture
def work(tmp_path, monkeypatch):
    directory = tmp_path / "work"
    directory.mkdir()
    monkeypatch.chdir(directory)
    return directory


def bare(env: dict, *flags: str, in_tmux: bool = False):
    """`start.bare_start` for `session start FLAGS`: (launch, minted, the
    dry run's seat line, the lines said)."""
    said: list[str] = []
    seat = st.bare_start(parse_args(list(flags)), in_tmux, environ=env, warn=said.append)
    return seat.launch, seat.minted, seat.line, said


def claim_of(env: dict) -> dict | None:
    return holders.read_claim(SEAT, env)


def proc_start(pid: int) -> str:
    return holders.proc_start_of(pid, procs.table())


def write_seat_claim(env: dict, pid: int | None = None, sid: str | None = SID,
                     pane: str | None = None, proc: str | None = None) -> None:
    pid = os.getpid() if pid is None else pid
    holders.write_claim(SEAT, {"sessionId": sid, "pid": pid,
                               "procStart": proc or proc_start(os.getpid()),
                               "pane": pane, "via": default_seat.VIA_BARE}, env)


def write_sidecar(env: dict, pid: int | None = None, sid: str = SID, agent: str = SEAT,
                  tmux: str | None = "priv:@1.%9") -> None:
    pid = os.getpid() if pid is None else pid
    directory = Path(env["SESSION_SIDECAR_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": sid, "agent": agent, "status": "idle",
         "procStart": proc_start(os.getpid()), "tmux": tmux, "cwd": "/nowhere",
         "updatedAt": int(time.time() * 1000), "startedAt": int(time.time() * 1000)}))


def e2e_path(tmp_path: Path, stubs: Path) -> str:
    """`launch_e2e`'s PATH with the `message` stub added: its stub vendor
    (`<tmp>/bin`) FIRST, so no test can reach a real vendor binary."""
    return os.pathsep.join([str(tmp_path / "bin"), str(stubs), os.environ["PATH"]])


def dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def took_the_seat(launch) -> bool:
    return launch.agent.name == SEAT


# ---------- first, second, stale -------------------------------------------------

def test_first_bare_start_takes_the_seat(tmp_path, macos, work):
    env = seat_env(tmp_path)
    launch, minted, line, said = bare(env)
    assert took_the_seat(launch) and launch.agent.builtin and line is None
    claim = claim_of(env)
    assert claim["sessionId"] == minted.session_id
    assert claim["pid"] == os.getpid() and claim["procStart"] == proc_start(os.getpid())
    assert claim["agent"] == SEAT and claim["via"] == "bare start" and claim["pane"] is None
    assert said == [f"this session is {SEAT}, the default seat on this host (box {SEAT})"]
    calls = (tmp_path / "stubs" / "message.calls").read_text().splitlines()
    assert calls == [f"box path {SEAT}"]


def test_second_bare_start_starts_plain_with_the_line(tmp_path, macos, work):
    """The first holder is this live process: the second start is plain,
    and the line names the holder's FULL session id."""
    env = seat_env(tmp_path)
    _launch, first, _line, _said = bare(env)
    launch, _minted, _line, said = bare(env)
    assert launch.agent.name == ""
    [line] = said
    assert line.startswith(f"starting plain: the default seat {SEAT} is held by session "
                           f"{first.session_id} (pid {os.getpid()}, live ")
    assert line.endswith(f"{HEAR}; that one does")
    assert claim_of(env)["sessionId"] == first.session_id      # untouched


def test_a_live_sidecar_with_no_claim_holds_the_seat(tmp_path, macos, work):
    """Source 2: a holder that wrote no claim (born before this feature, or
    a hand `--agent`) is found by its sidecar."""
    env = seat_env(tmp_path)
    write_sidecar(env)
    launch, _minted, _line, [line] = bare(env)
    assert launch.agent.name == ""
    assert f"held by session {SID} (pid {os.getpid()}, tmux %9, live " in line
    assert claim_of(env) is None


def test_a_sidecar_of_another_agent_holds_nothing(tmp_path, macos, work):
    env = seat_env(tmp_path)
    write_sidecar(env, agent="scout")
    launch, _minted, _line, _said = bare(env)
    assert took_the_seat(launch)


def test_dead_pid_claim_is_taken_over(tmp_path, macos, work):
    env = seat_env(tmp_path)
    write_seat_claim(env, pid=dead_pid())
    launch, minted, _line, _said = bare(env)
    assert took_the_seat(launch)
    assert claim_of(env)["pid"] == os.getpid()
    assert claim_of(env)["sessionId"] == minted.session_id


def test_reused_pid_claim_is_taken_over(tmp_path, macos, work):
    """This pid, but a start time an hour off: another process had it."""
    env = seat_env(tmp_path)
    started = procs.table()[os.getpid()].started
    write_seat_claim(env, proc=time.strftime(procs.CTIME, time.gmtime(started - 3600)))
    launch, minted, _line, _said = bare(env)
    assert took_the_seat(launch)
    assert claim_of(env)["sessionId"] == minted.session_id


@pytest.mark.parametrize("text", ["{half a claim", '{"pid": "me"}', "null"])
def test_unparseable_claim_is_stale(tmp_path, macos, work, text):
    """Taken over under the lock - and the sidecar survey still runs."""
    env = seat_env(tmp_path)
    _lock, path = holders.claim_paths(SEAT, env)
    path.parent.mkdir(parents=True)
    path.write_text(text)
    launch, _minted, _line, _said = bare(env)
    assert took_the_seat(launch)
    path.write_text(text)
    write_sidecar(env)
    launch, _minted, _line, [line] = bare(env)
    assert launch.agent.name == "" and f"held by session {SID}" in line


# ---------- the own pane: replaced, or nested --------------------------------------

def test_claim_in_own_pane_is_replaced_not_held(tmp_path, macos, work):
    """A `respawn-pane -k` launch: the old holder lives in this pane and is
    NOT this launcher's ancestor (a child `sleep` stands in), so it is
    being replaced, and the seat is taken."""
    child = subprocess.Popen(["sleep", "30"])
    try:
        env = seat_env(tmp_path, TMUX_PANE="%4")
        holders.write_claim(SEAT, {"sessionId": SID, "pid": child.pid,
                                   "procStart": proc_start(child.pid), "pane": "%4"}, env)
        launch, _minted, _line, _said = bare(env)
        assert took_the_seat(launch)
        assert claim_of(env)["pid"] == os.getpid() and claim_of(env)["pane"] == "%4"
    finally:
        child.kill()
        child.wait()


def test_nested_start_under_the_holder_is_held(tmp_path, macos, work):
    """A start typed in the seat's own tool shell shares its pane, and the
    holder is the launcher's ancestor (here: the launcher itself): HELD,
    never replaced - for the claim and for the sidecar alike."""
    env = seat_env(tmp_path, TMUX_PANE="%4")
    write_seat_claim(env, pane="%4")
    launch, _minted, _line, [line] = bare(env)
    assert launch.agent.name == "" and f"held by session {SID}" in line and "tmux %4" in line
    holders.claim_paths(SEAT, env)[1].unlink()
    write_sidecar(env, tmux="priv:@1.%4")
    launch, _minted, _line, [line] = bare(env)
    assert launch.agent.name == "" and f"held by session {SID}" in line


def test_a_claim_with_no_id_names_the_sidecars(tmp_path, macos, work):
    """A claim written before the mint (or by a pane mint) has no id: HELD
    finds it in the live sidecar of the claim's pid, else says so."""
    env = seat_env(tmp_path)
    write_seat_claim(env, sid=None)
    _launch, _minted, _line, [line] = bare(env)
    assert f"held by a session (id not known yet) (pid {os.getpid()}" in line
    write_sidecar(env, sid=SID)
    _launch, _minted, _line, [line] = bare(env)
    assert f"held by session {SID} (pid {os.getpid()}" in line


# ---------- two at once -----------------------------------------------------------

def test_the_lock_serialises_the_check_and_the_claim(tmp_path, macos, work, monkeypatch):
    """A unit of the race: while one launch holds the seat's lock, a second
    waits; once the first has claimed and let go, the second reads HELD."""
    env = seat_env(tmp_path)
    monkeypatch.setattr(default_seat, "SEAT_LOCK_WAIT_S", 10.0)
    inside, release = threading.Event(), threading.Event()

    def first():
        with holders.locked(SEAT, env) as lock:
            assert lock.ok
            inside.set()
            release.wait(5)
            write_seat_claim(env)

    thread = threading.Thread(target=first)
    thread.start()
    inside.wait(5)
    threading.Timer(0.3, release.set).start()
    launch, _minted, _line, [line] = bare(env)
    thread.join()
    assert launch.agent.name == "" and f"held by session {SID}" in line


#: The seat vendor stub. The holder stays alive until the test touches
#: `release`, never for a fixed span: the order below has every other launch
#: check while the holder lives, by construction. The 600 s loop is an
#: orphan bound only, so a stub cannot outlive a crashed run.
SEAT_STUB = """#!/usr/bin/env bash
printf '%%s\\0' "$@" > "%(dir)s/argv.$$"
printf '%%s' "$CLAUDE_SESSION_ID" > "%(dir)s/sid.$$"
case " $* " in
  *" --agents "*) for _ in $(seq 6000); do [ -e "%(dir)s/release" ] && exit 0; sleep 0.1; done ;;
esac
"""

#: A `sitecustomize` the race test writes into its own tmp dir and puts on
#: the launches' PYTHONPATH. It acts only in a `python -m sessiontools.cli`
#: process whose environment names SINGLETON_ORDER_DIR, which only that test
#: sets. It makes the four launches take the seat's lock in an order the
#: test chooses, instead of the scheduler's, by waiting on marker files the
#: other launches write (`<name>.<pid>` in that dir). Every wait is bounded
#: (60 s); a wait that runs out leaves a `timeout-<name>.<pid>` marker the
#: test fails on, so a broken order fails, it never hangs.
#:
#: - The first launch to reach the lock is ELECTED (an O_EXCL file). It
#:   takes the lock first and holds it until the others are really
#:   contending: three refused `flock` tries (`blocked`) in the id-unknown
#:   case. In the id-known case it waits for the other three to arrive.
#: - id-unknown: the holder writes its claim (no session id yet, by design)
#:   and lets the lock go. Then it waits, before it takes the lock again to
#:   write the id (`_record_id`), until the three others have checked. Each
#:   of them reads the claim while it has no id.
#: - id-known: the three others wait, BEFORE they try the lock, until the
#:   holder has written the id (`recorded`). Then they contend three ways:
#:   the first of them to get the lock holds it until the other two are
#:   refused. A launch never waits for `recorded` while it holds the lock:
#:   the holder needs that lock to record, so that would deadlock.
#: - The two budgets this test does not test are lifted, in these launches
#:   only: the seat lock's wait (SEAT_LOCK_WAIT_S) and the box check's
#:   timeout (BOX_CHECK_TIMEOUT_S). Each has its own in-process test
#:   (test_busy_seat_lock_starts_plain, test_box_check_timeout_is_unboxed).
ORDER_SHIM = '''\
import os
import sys

DIR = os.environ.get("SINGLETON_ORDER_DIR")
if DIR and list(getattr(sys, "orig_argv", ())[1:3]) == ["-m", "sessiontools.cli"]:
    import contextlib
    import errno
    import fcntl
    import time
    from pathlib import Path

    from sessiontools import start as _start
    from sessiontools.start import default_seat as _seat
    from sessiontools.start import holders as _holders

    D = Path(DIR)
    CASE = os.environ["SINGLETON_ORDER_CASE"]
    PID = os.getpid()
    BOUND = 60.0
    _seat.SEAT_LOCK_WAIT_S = 300.0
    _seat.BOX_CHECK_TIMEOUT_S = 300.0

    def mark(name):
        (D / f"{name}.{PID}").write_text("")

    def await_marks(name, count):
        deadline = time.monotonic() + BOUND
        while len(list(D.glob(f"{name}.*"))) < count:
            if time.monotonic() > deadline:
                mark(f"timeout-{name}")
                return
            time.sleep(0.01)

    def first_to(name):
        try:
            os.close(os.open(D / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            return False
        return True

    class _Fcntl:
        """`holders`' view of fcntl: a refused lock try is marked."""

        def __getattr__(self, name):
            return getattr(fcntl, name)

        def flock(self, fd, operation):
            try:
                return fcntl.flock(fd, operation)
            except OSError as error:
                if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    mark("blocked")
                raise

    _holders.fcntl = _Fcntl()
    real_locked = _holders.locked
    state = {"first": True}

    @contextlib.contextmanager
    def locked(agent, environ=None, wait=_holders.LOCK_WAIT_S):
        first, state["first"] = state["first"], False
        elected = first and first_to("elected")
        if first:
            mark("arrived")
            if not elected:
                await_marks("recorded" if CASE == "id-known" else "holding", 1)
        with real_locked(agent, environ, wait) as lock:
            if first and lock.ok:
                if elected:
                    mark("holding")
                    if CASE == "id-unknown":
                        await_marks("blocked", 3)
                    else:
                        await_marks("arrived", 4)
                elif CASE == "id-known" and first_to("second"):
                    await_marks("blocked", 2)
            yield lock

    _holders.locked = locked
    real_check = _seat.check

    def check(seat, environ=None, **kwargs):
        found = real_check(seat, environ, **kwargs)
        if found.holder is not None:
            mark("checked")
        elif found.unmeasured:
            mark("unsure")
        return found

    _seat.check = check
    real_record = _start._record_id

    def record_id(*args, **kwargs):
        if CASE == "id-unknown":
            await_marks("checked", 3)
        try:
            return real_record(*args, **kwargs)
        finally:
            mark("recorded")

    _start._record_id = record_id
'''


def marked(order: Path, name: str) -> set[str]:
    """The pids that wrote `<name>.<pid>` in the race test's order dir."""
    return {path.name.split(".", 1)[1] for path in order.glob(f"{name}.*")}


@on_macos
@pytest.mark.parametrize("case", ["id-unknown", "id-known"])
def test_simultaneous_bare_starts_give_one_holder(tmp_path, case):
    """Four real launches contend for the seat at once: exactly one execs
    the seat (and stays alive until released), and three start plain naming
    it. The interleaving is forced (ORDER_SHIM), never left to the
    scheduler, so each case asserts ONE wording:

    - id-unknown: the three check while the holder's claim has no session
      id yet, the worst case the claim exists for. Each still reads HELD,
      naming the holder's pid; afterwards the claim names the holder's id.
    - id-known: the three check after the holder has written its id, and
      each names it.

    RESIDUAL, named: a waiter's check reads the process table with one `ps`
    call, bounded at 5 s inside `procs.table`. If that call runs out, the
    table is empty and that launch starts plain with UNSURE instead of
    naming the holder. That needs one `ps` to stall for 5 s."""
    record = tmp_path / "record"
    record.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(SEAT_STUB % {"dir": record})
    (bin_dir / "claude").chmod(0o755)
    order = tmp_path / "order"
    order.mkdir()
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(ORDER_SHIM)
    env = clean_env(**seat_env(tmp_path, path=f"{tmp_path / 'stubs'}{os.pathsep}{bin_dir}"
                               f"{os.pathsep}{os.environ['PATH']}",
                               SINGLETON_ORDER_DIR=str(order), SINGLETON_ORDER_CASE=case))
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(shim), env.get("PYTHONPATH")]))
    (tmp_path / "work").mkdir()
    runs = [subprocess.Popen(at_a_terminal(), cwd=tmp_path / "work", env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(4)]
    try:
        # A hang bound only: the order above decides what happens, not this.
        deadline = time.monotonic() + 120
        while sum(run.poll() is not None for run in runs) < 3 and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        (record / "release").touch()
    results = [(run.wait(timeout=60), *run.communicate()) for run in runs]
    assert [code for code, _out, _err in results] == [0, 0, 0, 0], results
    assert not list(order.glob("timeout-*")), sorted(path.name for path in order.iterdir())
    argvs = {path.name.split(".")[1]: path.read_text().split("\0")[:-1]
             for path in record.glob("argv.*")}
    seats = [pid for pid, argv in argvs.items() if "--agent" in argv]
    assert len(seats) == 1, argvs
    holder = seats[0]
    holder_sid = (record / f"sid.{holder}").read_text()
    waiters = set(argvs) - {holder}
    assert len(waiters) == 3 and marked(order, "holding") == {holder}, argvs
    assert marked(order, "checked") == waiters                   # each read HELD
    assert not marked(order, "unsure")
    held = [err for _code, _out, err in results if "is held by " in err]
    assert len(held) == 3, results
    if case == "id-unknown":
        assert waiters <= marked(order, "blocked")       # all three contended the lock
        assert all(f"is held by a session (id not known yet) (pid {holder}" in err
                   for err in held), held
        claim = json.loads(holders.claim_paths(SEAT, env)[1].read_text())
        assert claim["pid"] == int(holder) and claim["sessionId"] == holder_sid
    else:
        assert len(waiters & marked(order, "blocked")) >= 2     # they contended three ways
        assert all(f"is held by session {holder_sid} (pid {holder}" in err
                   for err in held), held


# ---------- the gates --------------------------------------------------------------

def test_missing_box_starts_plain_with_the_repair_line(tmp_path, macos, work):
    env = seat_env(tmp_path, box=2, stderr=NO_BOX_STDERR)
    launch, _minted, _line, said = bare(env)
    assert launch.agent.name == ""
    assert said == [(f"starting plain: box {SEAT} is missing, {HEAR}; "
                     f"repair: message setup --box {SEAT}")]
    assert not (tmp_path / "state").exists()      # no lock, no claim, no mkdir


def test_a_usage_error_is_unboxed_not_no_box(tmp_path, macos, work):
    """B7: exit 2 is also click's usage error (an old `message` with no
    `box path`): it reads as UNBOXED, naming why, never the repair line."""
    env = seat_env(tmp_path, box=2, stderr=USAGE_STDERR)
    _launch, _minted, _line, [line] = bare(env)
    assert line == (f"starting plain: could not check box {SEAT} (`message box path` refused "
                    f"the call: No such command 'path'.), {HEAR}")
    assert not (tmp_path / "state").exists()


def test_unchecked_box_starts_plain(tmp_path, macos, work):
    env = seat_env(tmp_path, path="/usr/bin:/bin")
    launch, _minted, _line, [line] = bare(env)
    assert launch.agent.name == ""
    assert line == f"starting plain: could not check box {SEAT} (`message` is not on PATH), {HEAR}"


def test_another_exit_is_unboxed(tmp_path, macos, work):
    env = seat_env(tmp_path, box=97)
    _launch, _minted, _line, [line] = bare(env)
    assert f"(`message box path` exited 97), {HEAR}" in line


def test_box_check_timeout_is_unboxed():
    def slow(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("message", default_seat.BOX_CHECK_TIMEOUT_S)
    assert default_seat.box_gate(SEAT, {}, run=slow) == default_seat.Gate(
        False, "UNBOXED", "`message box path` took over 5s")


def test_linux_starts_plain_with_the_macos_only_line(tmp_path, work, monkeypatch, capsys):
    """Unit, live and dry run: the platform gate comes first, so the box is
    never asked and the state dir never written."""
    assert default_seat.gate(SEAT, {}, platform="linux") == default_seat.Gate(False, "LINUX")
    monkeypatch.setattr(default_seat, "current_platform", lambda: "linux")
    env = seat_env(tmp_path)
    launch, _minted, _line, said = bare(env)
    expected = (f"starting plain: the default seat {SEAT} is macOS-only for now: "
                f"sessionmail cannot yet read Linux start times, {HEAR}")
    assert launch.agent.name == "" and said == [expected]
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert st.main(["--dry-run"]) == EXIT_OK
    assert f"seat:     not taken: a live launch starts plain and says: {expected}" in \
        capsys.readouterr().out
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "stubs" / "message.calls").exists()


# ---------- fail closed --------------------------------------------------------------

@pytest.mark.parametrize("table, why", [
    (lambda: (_ for _ in ()).throw(OSError("ps exploded")),
     "the process table could not be read (ps exploded)"),
    (dict, "the process table came back empty (`ps` failed?)"),
])
def test_unreadable_process_table_starts_plain(tmp_path, macos, work, monkeypatch, table, why):
    monkeypatch.setattr(default_seat._procs, "table", table)
    env = seat_env(tmp_path)
    launch, _minted, _line, [line] = bare(env)
    assert launch.agent.name == ""
    assert line == (f"starting plain: could not check who holds the default seat {SEAT} "
                    f"({why}), {HEAR}")
    assert claim_of(env) is None


def test_busy_seat_lock_starts_plain(tmp_path, macos, work, monkeypatch):
    env = seat_env(tmp_path)
    monkeypatch.setattr(default_seat, "SEAT_LOCK_WAIT_S", 0.2)
    with holders.locked(SEAT, env) as lock:
        assert lock.ok
        launch, _minted, _line, [line] = bare(env)
    assert launch.agent.name == "" and "held the lock" in line and line.endswith(HEAR)
    assert claim_of(env) is None


def test_unwritable_state_dir_starts_plain(tmp_path, macos, work):
    (tmp_path / "state").write_text("a file where the state dir goes")
    env = seat_env(tmp_path)
    launch, _minted, _line, [line] = bare(env)
    assert launch.agent.name == "" and "could not be opened" in line


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_unreadable_claim_starts_plain(tmp_path, macos, work):
    """Permission denied is a check that cannot run, not a stale claim."""
    env = seat_env(tmp_path)
    write_seat_claim(env, pid=dead_pid())
    path = holders.claim_paths(SEAT, env)[1]
    path.chmod(0)
    try:
        launch, _minted, _line, [line] = bare(env)
    finally:
        path.chmod(0o600)
    assert launch.agent.name == "" and "could not be read" in line


def test_bare_start_never_exits_3(tmp_path, macos, work, monkeypatch, capsys):
    """A held seat never refuses a bare start: the front door is not met."""
    env = seat_env(tmp_path)
    write_seat_claim(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert st.main(["--dry-run"]) == EXIT_OK
    assert "would REFUSE" not in capsys.readouterr().out


# ---------- keyed --------------------------------------------------------------------

def test_bare_keyed_is_refused_even_when_the_seat_is_free(tmp_path, macos, work):
    env = seat_env(tmp_path)
    with pytest.raises(LaunchError) as stop:
        bare(env, "--keyed")
    assert stop.value.code == EXIT_USAGE and "--keyed needs an agent" in str(stop.value)
    assert not (tmp_path / "stubs" / "message.calls").exists()
    assert not (tmp_path / "state").exists()


def test_keyed_dash_is_still_refused(tmp_path):
    env = seat_env(tmp_path)
    with pytest.raises(LaunchError, match="--keyed needs an agent") as stop:
        prepare(parse_args(["--keyed", "-"]), environ=env, work_dir=str(tmp_path))
    assert stop.value.code == EXIT_USAGE


def test_keyed_shadowing_file_takes_the_seat(tmp_path, macos, work):
    """A shadowing file that says `start: keyed: true` keys the seat (Q14);
    the claim names the keyed id key-init minted, and this pid."""
    stub, record = write_key_init_stub(tmp_path)
    env = seat_env(tmp_path, SESSION_KEY_INIT=stub)
    (tmp_path / "agents" / f"{SEAT}.md").write_text(
        "---\ndescription: mine\nstart:\n  keyed: true\n---\n")
    launch, minted, _line, _said = bare(env)
    assert took_the_seat(launch) and launch.keyed and minted.session_id == STUB_SID
    assert claim_of(env)["sessionId"] == STUB_SID and claim_of(env)["pid"] == os.getpid()
    assert f"--agent-name {SEAT}" in Path(record).read_text()


# ---------- the escape hatch, the dry run --------------------------------------------

@on_macos
def test_dash_is_always_plain(tmp_path):
    """`session start -`: no gate, no lock, no claim, no line."""
    stubs = tmp_path / "stubs"
    calls = message_stub(stubs)
    result, record, _work = launch_e2e(
        tmp_path, "-", extra_env={"SESSION_STATE_DIR": str(tmp_path / "state"),
                                  "PATH": e2e_path(tmp_path, stubs)})
    assert result.returncode == 0, result.stderr
    assert "--agent" not in (record / "argv").read_text().split("\0")
    assert result.stderr == ""
    assert not calls.exists() and not (tmp_path / "state").exists()


@on_macos
def test_first_bare_start_takes_the_seat_end_to_end(tmp_path):
    """The real verb: the stub vendor is exec'd in place with the seat's
    argv, and the claim names ITS pid (exec kept the launcher's)."""
    pid_stub = ("#!/usr/bin/env bash\nprintf '%%s\\0' \"$@\" > \"%(dir)s/argv\"\n"
                "printf '%%s' \"$$\" > \"%(dir)s/pid\"\n"
                "printf '%%s' \"$CLAUDE_SESSION_ID\" > \"%(dir)s/sid\"\n")
    stubs = tmp_path / "stubs"
    message_stub(stubs)
    state = tmp_path / "state"
    result, record, _work = launch_e2e(
        tmp_path, stub_body=pid_stub,
        extra_env={"SESSION_STATE_DIR": str(state),
                   "PATH": e2e_path(tmp_path, stubs)})
    assert result.returncode == 0, result.stderr
    argv = (record / "argv").read_text().split("\0")[:-1]
    assert argv[argv.index("--agent") + 1] == SEAT and "--agents" in argv
    claim = json.loads((state / "holders" / f"{SEAT}.json").read_text())
    assert claim["pid"] == int((record / "pid").read_text())
    assert claim["sessionId"] == (record / "sid").read_text()
    assert f"this session is {SEAT}, the default seat on this host" in result.stderr


def test_dry_run_reports_the_seat_and_writes_no_claim(tmp_path, macos, work, monkeypatch,
                                                      capsys):
    env = seat_env(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert st.main(["--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert (f"seat:     {SEAT}, the default seat: a live launch takes it "
            f"(box {SEAT} found; no live holder)") in out
    assert "--agents" in out
    assert not (tmp_path / "state").exists()
    write_seat_claim(env)
    before = holders.claim_paths(SEAT, env)[1].read_bytes()
    assert st.main(["--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert (f"seat:     not taken: a live launch starts plain and says: starting plain: "
            f"the default seat {SEAT} is held by session {SID}") in out
    assert "--agents" not in out
    assert holders.claim_paths(SEAT, env)[1].read_bytes() == before
    assert not holders.claim_paths(SEAT, env)[0].exists()       # no lock taken


# ---------- what the seat's session reads as -----------------------------------------

def test_birth_settings_name_the_seat_and_no_package_file(tmp_path, macos, work):
    env = seat_env(tmp_path)
    _launch, minted, _line, _said = bare(env)
    birth = json.loads(Path(minted.session_dir, BIRTH_SETTINGS).read_text())
    assert birth == {"agent": SEAT}


def test_birth_settings_record_a_shadowing_file(tmp_path, macos, work):
    env = seat_env(tmp_path)
    shadow = tmp_path / "agents" / f"{SEAT}.md"
    shadow.write_text("---\ndescription: mine\n---\n")
    _launch, minted, _line, _said = bare(env)
    birth = json.loads(Path(minted.session_dir, BIRTH_SETTINGS).read_text())
    assert birth == {"agent": SEAT, "agent_file": os.path.realpath(shadow)}


def _seat_plan(tmp_path, *flags):
    root = make_agents_root(tmp_path, name="other")
    env = {"AGENTS_DIR": root, "SESSIONS_DIR": str(tmp_path / "sessions"),
           "PATH": os.environ["PATH"], "SESSION_KEY_INIT": os.environ["SESSION_KEY_INIT"]}
    (tmp_path / "work").mkdir(exist_ok=True)
    return prepare(parse_args([*flags, SEAT]), environ=env, work_dir=str(tmp_path / "work"))


def _injected(plan) -> str:
    return plan.injected.initial + plan.injected.system_prompt


def test_idle_default_seat_gets_no_boot_line(tmp_path):
    plan = _seat_plan(tmp_path, "--idle")
    assert plan.agent.builtin and IDLE_BOOT_TRIGGER not in _injected(plan)


def test_converse_default_seat_gets_the_bare_signal(tmp_path):
    plan = _seat_plan(tmp_path, "--converse")
    assert plan.agent.builtin and BARE_CONVERSE_SIGNAL in _injected(plan)


def test_the_minted_identity_is_the_seats(tmp_path, macos, work):
    env = seat_env(tmp_path)
    _launch, minted, _line, _said = bare(env)
    assert isinstance(minted, SessionIdentity) and minted.minted
