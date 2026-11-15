"""`sessiontools.web.supervisor` (kit-web-shell design §6): rooms start in
their own process groups, a room that exits is restarted with backoff, and
stop takes each whole group down. These tests run real child processes (a
fake room program on a unix socket in a tmp dir), shorten every delay, and
check that no child or grandchild is left behind."""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import time
from collections.abc import Iterator

import pytest
from web_fakes import alive, short_dir, wait_for, write_fake_room

from sessiontools.web import supervisor as sup
from sessiontools.web.rooms import Room
from sessiontools.web.unixhttp import UnixHTTPConnection

FAST = {"backoff_first": 0.05, "backoff_max": 0.2, "grace": 0.5, "poll": 0.02}
#: How long a room that is "up" may take to give its first answer. "up" means
#: only that its socket file exists; under load (ST-322: ~25 on 16 cores) the
#: room can take longer than one request's 5 s to answer, so a test that asks
#: waits for the answer itself, up to this bound.
ANSWER_WITHIN = 30.0


@pytest.fixture
def place(tmp_path) -> Iterator[dict]:
    with short_dir() as run:
        yield {"run": run / "run", "log": tmp_path / "log",
               "program": write_fake_room(tmp_path), "tmp": tmp_path}


def _room(place: dict, mode: str, marker: str | None = None) -> Room:
    argv = [sys.executable, str(place["program"]), mode, "{socket}"] + ([marker] if marker else [])
    return Room(name="talk", title="Talk", package="sessionvoice", command=tuple(argv))


@pytest.fixture
def started() -> Iterator[list]:
    """Supervisors a test starts, each stopped after it, and every pid any
    of them ran checked dead: no orphan is left behind."""
    made: list[sup.Supervisor] = []
    yield made
    pids = set()
    for s in made:
        pids |= set(getattr(s, "_seen_pids", set()))
        s.stop()
    assert wait_for(lambda: not any(alive(p) for p in pids), timeout=5), "a room outlived stop"


def _start(started: list, place: dict, room: Room, **kw) -> sup.Supervisor:
    s = sup.Supervisor([room], place["run"], place["log"], **{**FAST, **kw})
    s._seen_pids = set()
    original = s._spawn

    def spawn(slot):
        original(slot)
        if slot.proc is not None:
            s._seen_pids.add(slot.proc.pid)

    s._spawn = spawn
    started.append(s)
    s.start()
    return s


def _ask(s: sup.Supervisor, timeout: float = 5) -> dict:
    c = UnixHTTPConnection(s.socket("talk"), timeout=timeout)
    try:
        c.request("GET", "/")
        return json.loads(c.getresponse().read())
    finally:
        c.close()


def _answer(s: sup.Supervisor, within: float = ANSWER_WITHIN) -> dict:
    """The room's first answer, waited for: the readiness a test that asks
    must wait on is an answer, not state "up" (the socket file exists, ST-322).
    Each try gets what is left of WITHIN, so a slow room is waited out in one
    request and a refused or missing socket is tried again, never slept on."""
    end = time.monotonic() + within
    got: list[dict] = []
    errors: list[Exception] = []

    def answered() -> bool:
        try:
            got.append(_ask(s, timeout=max(end - time.monotonic(), 0.1)))
        except (OSError, http.client.HTTPException) as e:
            errors.append(e)
            return False
        return True

    assert wait_for(answered, timeout=within), \
        f"talk did not answer within {within} s: {errors[-1] if errors else None!r}"
    return got[0]


# --- the helper's wait (ST-322) ------------------------------------------------------

class _Sockets:
    """A stand-in supervisor for `_answer`: `socket()` gives each of PATHS in
    turn, then the last for good, and counts the asks."""

    def __init__(self, *paths):
        self.paths, self.asks = list(paths), 0

    def socket(self, name: str):
        self.asks += 1
        return self.paths.pop(0) if len(self.paths) > 1 else self.paths[0]


def test_answer_tries_again_until_the_room_answers(place, started):
    """A missing socket (the room not bound yet) is tried again, not failed:
    the third ask finds the room and its answer is returned."""
    s = _start(started, place, _room(place, "serve"))
    assert wait_for(lambda: s.state("talk") == "up")
    missing = place["run"] / "missing.sock"
    stand_in = _Sockets(missing, missing, s.socket("talk"))
    assert _answer(stand_in)["room"] == "talk"
    assert stand_in.asks == 3


def test_answer_fails_by_its_own_bound_naming_the_last_error(place):
    """A room that never answers fails the test once WITHIN has passed, not
    after one request's 5 s, and the message names why the last try failed."""
    began = time.monotonic()
    with pytest.raises(AssertionError, match=r"did not answer within 0.3 s: (FileNotFoundError|ConnectionRefusedError)"):
        _answer(_Sockets(place["run"] / "never.sock"), within=0.3)
    assert time.monotonic() - began < 3


def test_answer_gives_a_silent_room_the_rest_of_its_bound_in_one_request(place):
    """A socket that accepts but never answers (a room too slow to serve) is
    waited on in ONE request bounded by WITHIN, not re-asked every 5 s."""
    place["run"].mkdir(parents=True, exist_ok=True)
    path = place["run"] / "silent.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(8)
        stand_in = _Sockets(path)
        began = time.monotonic()
        with pytest.raises(AssertionError, match=r"did not answer within 0.5 s: (TimeoutError|timeout)"):
            _answer(stand_in, within=0.5)
        assert 0.4 < time.monotonic() - began < 3
        assert stand_in.asks <= 2  # the first try took the whole bound; at most wait_for's last look follows
    finally:
        listener.close()


# --- present -------------------------------------------------------------------------

def test_a_present_room_is_started_in_its_own_group_and_answers(place, started):
    s = _start(started, place, _room(place, "serve"))
    assert wait_for(lambda: s.state("talk") == "up")
    pid = s.info("talk")["pid"]
    assert os.getpgid(pid) == pid != os.getpgid(0)  # a group of its own
    assert _answer(s)["room"] == "talk"
    assert s.socket("talk") == place["run"] / "talk.sock"
    s.stop()
    assert s.state("talk") == "stopped"
    assert not alive(pid) and not s.socket("talk").exists()


def test_the_rooms_output_goes_to_its_log(place, started):
    room = Room(name="talk", title="Talk", package="sessionvoice",
                command=(sys.executable, "-c",
                         "import sys; print('hello from talk', sys.argv[1]); sys.exit(0)",
                         "{socket}"))
    s = _start(started, place, room)
    log = place["log"] / "talk.log"
    assert wait_for(lambda: log.exists() and "hello from talk" in log.read_text())
    assert str(s.socket("talk")) in log.read_text()


# --- crashed ---------------------------------------------------------------------------

def test_a_crashed_room_is_restarted_with_backoff_and_shown_restarting(place, started):
    s = _start(started, place, _room(place, "crash"))
    assert wait_for(lambda: s.info("talk")["starts"] >= 4, timeout=10)
    info = s.info("talk")
    assert info["last_exit"] == 1 and info["delay"] == FAST["backoff_max"]
    assert wait_for(lambda: s.state("talk") == "restarting")


def test_a_room_that_crashes_once_comes_back_up(place, started):
    marker = str(place["tmp"] / "crashed-once")
    s = _start(started, place, _room(place, "crash-once", marker))
    assert wait_for(lambda: s.state("talk") == "up")
    assert s.info("talk")["starts"] == 2 and s.info("talk")["last_exit"] == 1
    assert _answer(s)["room"] == "talk"


def test_a_room_that_cannot_be_started_is_restarting_and_the_others_run(place, started):
    """A room whose command is gone (its venv removed since discovery) is
    that room's failure alone: start does not raise, the other room is up,
    and nothing is left running when start is followed by stop."""
    gone = Room(name="gone", title="Gone", package="nothing",
                command=(str(place["tmp"] / "no-such-command"), "{socket}"))
    s = sup.Supervisor([_room(place, "serve"), gone], place["run"], place["log"], **FAST)
    s._seen_pids = set()
    started.append(s)
    s.start()
    pid = s.info("talk")["pid"]
    s._seen_pids.add(pid)
    assert wait_for(lambda: s.state("talk") == "up")
    info = s.info("gone")
    assert info["state"] == "restarting" and info["starts"] == 0
    assert "could not start" in info["error"]
    assert "could not start" in (place["log"] / "gone.log").read_text()
    assert wait_for(lambda: s.info("gone")["delay"] == FAST["backoff_max"])  # retried, backing off
    assert s._thread is not None and s._thread.is_alive()


def test_a_command_that_vanishes_while_restarting_does_not_stop_the_watcher(place, started):
    """The watcher outlives one room's failed restart, so the OTHER room is
    still restarted when it crashes."""
    exe = place["tmp"] / "python"
    exe.symlink_to(sys.executable)
    crashing = Room(name="crashing", title="C", package="p",
                    command=(str(exe), str(place["program"]), "crash", "{socket}"))
    s = sup.Supervisor([crashing, _room(place, "serve")], place["run"], place["log"], **FAST)
    s._seen_pids = set()
    started.append(s)
    s.start()
    exe.unlink()
    assert wait_for(lambda: s.info("crashing")["error"] is not None)
    assert wait_for(lambda: s.state("talk") == "up")
    first = s.info("talk")["pid"]
    s._seen_pids.add(first)
    os.killpg(first, 9)
    assert wait_for(lambda: s.info("talk")["starts"] == 2 and s.state("talk") == "up")
    s._seen_pids.add(s.info("talk")["pid"])
    assert s._thread.is_alive()


class _Proc:
    def __init__(self, pid: int = 999999999, rc: int = 1) -> None:
        self.pid, self.returncode = pid, rc


def test_the_backoff_doubles_to_its_cap_and_resets_after_a_stable_run(place, monkeypatch):
    monkeypatch.setattr(sup, "_killpg", lambda *a: None)
    now = [0.0]
    s = sup.Supervisor([_room(place, "serve")], place["run"], place["log"],
                       backoff_first=1, backoff_max=60, stable_after=60, clock=lambda: now[0])
    slot = s._slots["talk"]
    delays = []
    for _ in range(8):
        slot.proc, slot.started_at = _Proc(), now[0]
        now[0] += 0.5  # it died quickly
        s._reap(slot)
        delays.append(slot.delay)
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]
    assert slot.restart_at == now[0] + 60
    slot.proc, slot.started_at = _Proc(), now[0]
    now[0] += 61  # it stayed up a full minute
    s._reap(slot)
    assert slot.delay == 1


# --- stop by process group --------------------------------------------------------------

def test_stop_takes_the_rooms_whole_group_its_children_too(place, started):
    marker = place["tmp"] / "kid.pid"
    s = _start(started, place, _room(place, "child", str(marker)))
    assert wait_for(lambda: s.state("talk") == "up" and marker.exists() and marker.read_text())
    kid = int(marker.read_text())
    room = s.info("talk")["pid"]
    assert alive(kid) and os.getpgid(kid) == room
    s.stop()
    assert wait_for(lambda: not alive(kid) and not alive(room), timeout=5)


def test_a_room_that_ignores_sigterm_is_killed_after_the_grace(place, started):
    s = _start(started, place, _room(place, "stubborn"), grace=0.3)
    assert wait_for(lambda: s.state("talk") == "up")
    pid = s.info("talk")["pid"]
    t = time.monotonic()
    s.stop()
    assert time.monotonic() - t < 3
    assert wait_for(lambda: not alive(pid), timeout=3)


def test_a_socket_path_too_long_is_refused_before_anything_starts(tmp_path):
    from sessiontools.web.unixhttp import SocketRefused
    deep = tmp_path / ("d" * 90)
    with pytest.raises(SocketRefused, match="at most"):
        sup.Supervisor([Room(name="talk", title="T", package="p",
                             command=("x", "{socket}"))], deep, tmp_path)
