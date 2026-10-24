"""The sound engine's core (ST-132 do-001): `src/sessiontools/sound/engine.swift`.

pytest builds the engine ONCE per session with the system `swiftc` (as
sessionvoice's input-watch fixture does) and skips cleanly without it. No test
opens an audio device: the core has no Core Audio at all. Two modes drive it:

- `--script STEPS.json`: virtual time, no sockets. Every rule of design.md
  decision 4 is pinned here deterministically, so a loaded host cannot flake
  it (ST-129 saw the busy-sound timing test flake at load 10).
- `--null-output`: real UDP on 127.0.0.1, an EPHEMERAL port (`--port 0`, the
  `ready` line names it; never :44100 or a live port), real records, real
  mouth files. Voices are counted in virtual frames.

Datagrams come from `osc()` below, the wire format in design.md decision 4. It
stands in for do-002's `sessiontools.sound.send`, which lands in parallel.

Every engine a test starts runs in its own process group and is killed by
that pgid at teardown, never by a pattern.
"""

from __future__ import annotations

import itertools
import json
import os
import pwd
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sessiontools" / "sound" / "engine.swift"
SID = "0f0e0d0c-0b0a-4908-8706-050403020100"
SID2 = "1f1e1d1c-1b1a-4918-8716-151413121110"
LIVE_PORTS = {44100, 8890, 2022, 8880}


# --- the binary ----------------------------------------------------------------

@pytest.fixture(scope="session")
def engine(tmp_path_factory) -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("the sound engine builds with swiftc on macOS only")
    out = tmp_path_factory.mktemp("sound-engine") / "sound-engine"
    subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", str(out), str(SRC)],
                   check=True, capture_output=True, timeout=600)
    return out


# --- helpers -------------------------------------------------------------------

def osc_str(s: str) -> bytes:
    b = s.encode() + b"\0"
    return b + b"\0" * (-len(b) % 4)


def osc(address: str, *args) -> bytes:
    """One OSC 1.0 message: str -> s, float -> d (float64), int -> i."""
    tags, body = ",", b""
    for a in args:
        if isinstance(a, str):
            tags, body = tags + "s", body + osc_str(a)
        elif isinstance(a, float):
            tags, body = tags + "d", body + struct.pack(">d", a)
        elif isinstance(a, int):
            tags, body = tags + "i", body + struct.pack(">i", a)
        else:
            raise TypeError(a)
    return osc_str(address) + osc_str(tags) + body


def event(ev: str, sid: str = SID, t: float | None = None) -> bytes:
    if t is None:
        t = time.time()
    return osc("/sound/event", ev, sid, float(t))


def dead_pid() -> int:
    """A pid that WAS a process and is not any more."""
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


@pytest.fixture
def live_pid():
    """A second live pid: not this process, and never 1, whoever started pytest.
    A child that sleeps until teardown, then is killed and reaped by its handle."""
    p = subprocess.Popen(["/bin/sleep", "600"])
    try:
        yield p.pid
    finally:
        p.kill()
        p.wait()


@pytest.fixture
def world(tmp_path):
    """A private sessions root, a sound file, a mouth dir."""
    root = tmp_path / "sessions"
    root.mkdir()
    sound = tmp_path / "busy.wav"
    sound.write_bytes(b"RIFF-not-really")
    mouth = tmp_path / "mouth"
    mouth.mkdir()
    return {"root": root, "sound": sound, "mouth": mouth, "tmp": tmp_path}


def record(world, sid: str = SID, *, pid: int | None = None, delay: float = 2.0,
           file: Path | str | None = None, mtime: float | None = None, name: str | None = None,
           body: dict | str | None = None) -> Path:
    """Write <root>/<sid>/sounds/busy/engine.<pid>, as do-002's python will."""
    pid = os.getpid() if pid is None else pid
    slot = world["root"] / sid / "sounds" / "busy"
    slot.mkdir(parents=True, exist_ok=True)
    path = slot / (name or f"engine.{pid}")
    if body is None:
        body = {"v": 1, "file": str(file or world["sound"]), "delay": delay, "pid": pid,
                "mouth": str(world["mouth"])}
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def lines(text: str) -> list[dict]:
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def script(engine, world, steps, *, until=None, length=1.0) -> list[dict]:
    path = world["tmp"] / "steps.json"
    doc = {"steps": steps}
    if until is not None:
        doc["until"] = until
    path.write_text(json.dumps(doc))
    r = subprocess.run([str(engine), "--script", str(path), "--sessions", str(world["root"]),
                        "--null-length", str(length)],
                       capture_output=True, text=True, check=False, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
    return lines(r.stderr)


def evs(log: list[dict], *names: str) -> list[dict]:
    return [x for x in log if x["ev"] in names]


def arm(t, sid=SID, **kw):
    return {"t": t, "event": "arm", "sid": sid, **kw}


def stop(t, verb="end", sid=SID, **kw):
    return {"t": t, "event": verb, "sid": sid, **kw}


# --- script: the state machine, deterministic ------------------------------------

def test_arm_counts_the_delay_then_plays_then_latches(engine, world):
    record(world, delay=2.0)
    log = script(engine, world, [arm(0)], length=1.5)
    armed, played, finished = evs(log, "armed"), evs(log, "played"), evs(log, "finished")
    assert [x["t"] for x in armed] == [0]
    assert armed[0]["delay"] == 2 and armed[0]["file"] == str(world["sound"])
    assert [x["t"] for x in played] == [2.0]
    assert [x["t"] for x in finished] == [3.5]
    assert finished[0]["frames"] == 72000            # 1.5 s at 48 kHz, virtual
    assert log[-1]["ev"] == "done" and log[-1]["sids"] == 1   # latched until end


@pytest.mark.parametrize("phase_t", [1.0, 2.5, 5.0])   # counting, playing, latched
def test_repeated_arms_are_no_ops_in_every_phase(engine, world, phase_t):
    record(world, delay=2.0)
    log = script(engine, world, [arm(0), arm(phase_t), arm(phase_t + 0.05)], length=1.0)
    assert len(evs(log, "armed")) == 1
    assert len(evs(log, "played")) == 1               # no replay inside the batch


@pytest.mark.parametrize("verb", ["end", "stop", "clear"])
def test_a_stop_while_counting_means_nothing_plays(engine, world, verb):
    record(world, delay=2.0)
    log = script(engine, world, [arm(0), stop(1.0, verb)], until=10)
    assert evs(log, "played") == []
    (s,) = evs(log, "stopped")
    assert s["t"] == 1.0 and s["by"] == verb and s["from"] == "counting"
    assert log[-1]["sids"] == 0


@pytest.mark.parametrize("verb", ["end", "stop", "clear"])
def test_a_stop_while_playing_fades_the_voice(engine, world, verb):
    record(world, delay=0.5)
    log = script(engine, world, [arm(0), stop(1.0, verb)], length=5.0)
    (s,) = evs(log, "stopped")
    assert s["from"] == "playing" and s["fade_ms"] == 20
    assert s["frames"] == 24000                        # 0.5 s played
    assert evs(log, "finished") == []
    assert log[-1]["voices"] == 0 and log[-1]["sids"] == 0


def test_after_the_stop_the_next_batch_plays_again(engine, world):
    record(world, delay=1.0)
    log = script(engine, world, [arm(0), stop(3), arm(4)], length=1.0)
    assert [x["t"] for x in evs(log, "played")] == [1.0, 5.0]


def test_speech_restarts_the_countdown_at_the_full_delay(engine, world):
    record(world, delay=2.0)
    log = script(engine, world, [arm(0), {"t": 1.0, "mouth": True}, {"t": 4.0, "mouth": False}],
                 until=20)
    assert [x["t"] for x in evs(log, "played")] == [6.0]   # silence from 4.0, plus 2 s


def test_nothing_starts_under_the_mouth(engine, world):
    record(world, delay=0)
    log = script(engine, world, [{"t": 0, "mouth": True}, arm(0.5), {"t": 3.0, "mouth": False}],
                 until=20)
    assert [x["t"] for x in evs(log, "played")] == [3.0]


def test_delay_zero_with_a_silent_mouth_starts_at_once(engine, world):
    record(world, delay=0)
    log = script(engine, world, [arm(0.25)], until=5)
    assert [x["t"] for x in evs(log, "played")] == [0.25]


def test_a_playing_sound_is_cut_when_the_mouth_starts_and_stays_latched(engine, world):
    record(world, delay=0)
    log = script(engine, world, [arm(0), {"t": 0.5, "mouth": True}, {"t": 1.0, "mouth": False},
                                 arm(1.5), stop(3), arm(4)], length=10.0, until=4.5)
    cut = evs(log, "cut")
    assert [x["t"] for x in cut] == [0.5] and cut[0]["frames"] == 24000
    assert [x["t"] for x in evs(log, "played")] == [0, 4.0]   # no replay until the stop
    assert [x["from"] for x in evs(log, "stopped")] == ["latched"]


def test_any_sessions_speech_holds_every_seat(engine, world):
    record(world, SID, delay=0)
    record(world, SID2, delay=0)
    log = script(engine, world, [{"t": 0, "mouth": True}, arm(0.1, SID), arm(0.2, SID2),
                                 {"t": 2.0, "mouth": False}], until=5)
    assert sorted((x["sid"], x["t"]) for x in evs(log, "played")) == [(SID, 2.0), (SID2, 2.0)]


def test_two_sids_count_and_play_independently(engine, world):
    record(world, SID, delay=1.0)
    record(world, SID2, delay=3.0)
    log = script(engine, world, [arm(0, SID), arm(0.5, SID2), stop(4, sid=SID)], length=10, until=5)
    played = {x["sid"]: x for x in evs(log, "played")}
    assert played[SID]["t"] == 1.0 and played[SID2]["t"] == 3.5
    assert played[SID]["voice"] != played[SID2]["voice"]
    assert [x["sid"] for x in evs(log, "stopped")] == [SID]


def test_a_dead_seat_at_arm_plays_nothing(engine, world):
    record(world, pid=dead_pid(), delay=0)
    log = script(engine, world, [arm(0), arm(1), arm(2)], until=5)
    assert evs(log, "armed", "played") == []
    gone = evs(log, "seat-gone")
    assert len(gone) == 1 and gone[0]["at"] == "arm"   # logged once, not per tool


def test_the_seat_dying_while_counting_forgets_it(engine, world):
    record(world, delay=5.0)
    log = script(engine, world, [arm(0), {"t": 2.0, "kill": os.getpid()}], until=10)
    assert evs(log, "played") == []
    (g,) = evs(log, "seat-gone")
    assert g["from"] == "counting" and g["t"] == 2.0
    assert log[-1]["sids"] == 0


def test_the_seat_dying_while_playing_cuts_the_voice(engine, world):
    """The one deliberate change from today's waiter (design.md decision 4)."""
    record(world, delay=0)
    log = script(engine, world, [arm(0), {"t": 1.0, "kill": os.getpid()}], length=10, until=5)
    (g,) = evs(log, "seat-gone")
    assert g["from"] == "playing" and g["frames"] == 48000 and g["fade_ms"] == 20
    assert log[-1]["voices"] == 0 and log[-1]["sids"] == 0


def test_a_latched_seat_that_dies_is_swept(engine, world):
    record(world, delay=0)
    log = script(engine, world, [arm(0), {"t": 5.0, "kill": os.getpid()}], length=1, until=200)
    (g,) = evs(log, "seat-gone")
    assert g["from"] == "latched" and 5.0 < g["t"] <= 61.0 + 1e-6
    assert log[-1]["sids"] == 0


def test_a_stale_arm_is_dropped_and_a_stale_stop_is_not(engine, world):
    record(world, delay=0)
    log = script(engine, world, [arm(10, sent=7.5), arm(11, sent=10.5),
                                 stop(12, sent=1.0)], length=10, until=13)
    assert [x["t"] for x in evs(log, "played")] == [11.0]
    assert [x["t"] for x in evs(log, "stopped")] == [12.0]
    assert log[-1]["stale"] == 1


def test_an_unknown_event_is_ignored_and_counted(engine, world):
    record(world, delay=0)
    log = script(engine, world, [{"t": 0, "event": "partial", "sid": SID},
                                 {"t": 0, "event": "landed", "sid": SID}], until=2)
    assert evs(log, "armed", "played") == []
    assert log[-1]["unknown"] == 2


def test_eviction_after_ten_minutes_unused(engine, world):
    record(world, delay=0)
    log = script(engine, world, [arm(0)], length=1.0)
    (e,) = evs(log, "evicted")
    assert e["file"] == str(world["sound"]) and e["t"] == 601.0   # last use: played out at 1.0
    assert log[-1]["files"] == 0


def test_a_file_in_use_is_never_evicted(engine, world):
    record(world, delay=0)
    log = script(engine, world, [arm(0)], length=900.0, until=1000)
    assert evs(log, "evicted") == []
    assert [x["t"] for x in evs(log, "finished")] == [900.0]


def test_the_voice_table_holds_eight_and_the_ninth_is_refused(engine, world):
    sids = [f"sid-{i}" for i in range(9)]
    for s in sids:
        record(world, s, delay=0)
    log = script(engine, world, [arm(0.01 * i, s) for i, s in enumerate(sids)], length=10, until=1)
    assert len(evs(log, "played")) == 8
    (r,) = evs(log, "refused")
    assert r["sid"] == "sid-8" and r["reason"] == "voices"


def test_a_missing_sound_file_plays_nothing(engine, world):
    record(world, delay=0, file=world["tmp"] / "gone.wav")
    log = script(engine, world, [arm(0), arm(1)], until=3)
    (r,) = evs(log, "refused")
    assert r["reason"] == "unreadable"
    assert evs(log, "played") == []


def test_no_record_plays_nothing_and_says_so_once(engine, world):
    log = script(engine, world, [arm(0), arm(1)], until=2)
    assert [x["ev"] for x in log if x["ev"] != "done"] == ["no-record"]


@pytest.mark.parametrize("body", [
    "not json",
    {"v": 2, "file": "/x.wav", "delay": 0, "pid": 0},
    {"v": 1, "file": "relative.wav", "delay": 0},
    {"v": 1, "file": "/x.wav", "delay": -1},
    {"v": 1, "file": "/x.wav", "delay": True},
    {"v": 1, "file": "/x.wav", "delay": 0, "pid": "1"},
    {"v": 1, "file": "/x.wav", "delay": 0, "mouth": 5},
])
def test_a_malformed_record_plays_nothing(engine, world, body):
    if isinstance(body, dict):
        body = {**body, "pid": body.get("pid", os.getpid()), "file": body["file"]}
    record(world, delay=0, body=body)
    log = script(engine, world, [arm(0)], until=2)
    assert evs(log, "armed", "played") == []
    assert evs(log, "no-record")


def test_a_record_named_for_another_pid_is_refused(engine, world):
    record(world, name=f"engine.{os.getpid() + 1}", delay=0)
    log = script(engine, world, [arm(0)], until=2)
    assert evs(log, "armed") == [] and evs(log, "no-record")


def test_temp_files_beside_the_record_are_ignored(engine, world):
    record(world, delay=0)
    (world["root"] / SID / "sounds" / "busy" / f"engine.{os.getpid()}.tmp").write_text("{}")
    (world["root"] / SID / "sounds" / "busy" / "off").write_text("")
    log = script(engine, world, [arm(0)], until=2)
    assert len(evs(log, "played")) == 1


# --- several records for one sid (design-001 review, finding 3) -------------------

def test_several_records_the_live_pid_wins_over_a_newer_dead_one(engine, world):
    live = record(world, delay=0, mtime=1_000_000)
    record(world, pid=dead_pid(), delay=0, file=world["tmp"] / "other.wav", mtime=2_000_000)
    log = script(engine, world, [arm(0)], until=2)
    (a,) = evs(log, "armed")
    assert a["record"] == str(live) and a["pid"] == os.getpid()


@pytest.mark.parametrize("parent_is_init", [False, True], ids=["any-parent", "parent-pid-1"])
def test_several_live_records_the_newest_mtime_wins(engine, world, live_pid, monkeypatch,
                                                    parent_is_init):
    # parent-pid-1: an orphaned run, whose parent is pid 1, a pid no seat may hold.
    if parent_is_init:
        monkeypatch.setattr(os, "getppid", lambda: 1)
    other = world["tmp"] / "other.wav"
    other.write_bytes(b"x")
    me = os.getpid()
    record(world, pid=me, delay=0, mtime=1_000_000)
    newest = record(world, pid=live_pid, delay=0, file=other, mtime=2_000_000)
    log = script(engine, world, [arm(0)], until=2)
    (a,) = evs(log, "armed")
    assert a["record"] == str(newest) and a["file"] == str(other)


def test_several_records_all_dead_is_a_gone_seat(engine, world):
    record(world, pid=dead_pid(), delay=0)
    record(world, pid=dead_pid(), delay=0)
    log = script(engine, world, [arm(0)], until=2)
    assert evs(log, "armed") == [] and len(evs(log, "seat-gone")) == 1


# --- the parser, through the script's raw datagrams --------------------------------

MALFORMED = {
    "garbage": b"\x00\x01\x02",
    "unpadded": b"/sound/event\0,ss\0arm\0",
    "wrong address": osc("/sound/evnt", "arm", SID, 1.0),
    "wrong types": osc("/sound/event", "arm", SID, 7),
    "one string": osc("/sound/event", "arm"),
    "short double": osc_str("/sound/event") + osc_str(",ssd") + osc_str("arm") + osc_str(SID)
    + b"\0\0\0\0",
    "trailing bytes": osc("/sound/event", "arm", SID, 1.0) + b"\0\0\0\0",
    "empty event": osc("/sound/event", "", SID, 1.0),
    "dot sid": osc("/sound/event", "arm", ".", 1.0),
    "slash sid": osc("/sound/event", "arm", "a/b", 1.0),
    "empty sid": osc("/sound/event", "arm", "", 1.0),
    "long sid": osc("/sound/event", "arm", "x" * 300, 1.0),
    "nan t": osc_str("/sound/event") + osc_str(",ssd") + osc_str("arm") + osc_str(SID)
    + struct.pack(">d", float("nan")),
    "ping with a string": osc("/sound/ping", "hello"),
}


def test_malformed_datagrams_are_dropped_and_counted(engine, world):
    record(world, delay=0)
    steps = [{"t": 0, "datagram": d.hex()} for d in MALFORMED.values()]
    log = script(engine, world, steps, until=1)
    assert evs(log, "armed", "played") == []
    assert log[-1]["malformed"] == len(MALFORMED)


def test_the_canonical_and_the_short_form_both_arm(engine, world):
    record(world, SID, delay=0)
    record(world, SID2, delay=0)
    steps = [{"t": 0, "datagram": osc("/sound/event", "arm", SID, 0.0).hex()},
             {"t": 0, "datagram": osc("/sound/event", "arm", SID2).hex()}]
    log = script(engine, world, steps, until=1)
    assert sorted(x["sid"] for x in evs(log, "played")) == sorted([SID, SID2])
    assert log[-1]["malformed"] == 0


def test_a_ping_through_the_parser_is_answered(engine, world):
    log = script(engine, world, [{"t": 0, "datagram": osc("/sound/ping", 1.5).hex()},
                                 {"t": 0, "datagram": osc("/sound/ping").hex()}], until=1)
    assert len(evs(log, "pong")) == 2


def test_a_bad_script_exits_2_naming_it(engine, world):
    path = world["tmp"] / "bad.json"
    path.write_text(json.dumps({"steps": [{"t": 0, "frobnicate": 1}]}))
    r = subprocess.run([str(engine), "--script", str(path), "--sessions", str(world["root"])],
                       capture_output=True, text=True, check=False, timeout=30)
    assert r.returncode == 2 and "unknown step" in r.stderr


def test_the_device_mode_is_fenced_off_in_tests(engine):
    """Bare, the engine opens the default output device (the launchd agent's
    mode). conftest sets SESSIONTOOLS_SOUND_NO_DEVICE, so here it refuses."""
    assert os.environ.get("SESSIONTOOLS_SOUND_NO_DEVICE")
    r = subprocess.run([str(engine), "--port", "0"], capture_output=True, text=True, check=False,
                       timeout=30)
    assert r.returncode == 2 and "SESSIONTOOLS_SOUND_NO_DEVICE" in r.stderr


@pytest.mark.parametrize("argv", [["--port", "70000"], ["--port"], ["--bogus"],
                                  ["--null-length", "0"], ["--volume", "1.5"], ["--volume", "-0.1"],
                                  ["--volume", "nan"], ["--evict-after", "0"],
                                  ["--offline", "x.wav"],                       # no --script
                                  ["--null-output", "--offline", "x.wav", "--script", "s.json"]])
def test_bad_arguments_exit_2(engine, argv):
    r = subprocess.run([str(engine), *argv], capture_output=True, text=True, check=False, timeout=30)
    assert r.returncode == 2 and "usage:" in r.stderr


# --- live: --null-output, real UDP on 127.0.0.1 ---------------------------------------

class Live:
    """A --null-output engine on an ephemeral 127.0.0.1 port, in its own pgid."""

    def __init__(self, engine, world, *extra):
        self.proc = subprocess.Popen(
            [str(engine), "--null-output", "--port", "0", "--sessions", str(world["root"]),
             *extra],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.log: list[dict] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        ready = self.wait_for("ready", timeout=20)
        assert ready, f"no ready line: {self.log}"
        self.port = ready["port"]
        assert ready["host"] == "127.0.0.1" and self.port not in LIVE_PORTS
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5)

    def _read(self):
        for line in self.proc.stderr:
            with self._cv:
                self.log.append(json.loads(line))
                self._cv.notify_all()

    def wait_for(self, ev, n=1, timeout=5.0):
        end = time.monotonic() + timeout
        with self._cv:
            while True:
                got = [x for x in self.log if x["ev"] == ev]
                if len(got) >= n:
                    return got[n - 1]
                left = end - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return None
                self._cv.wait(left)

    def send(self, data: bytes):
        self.sock.sendto(data, ("127.0.0.1", self.port))

    def ping(self, t: float = 12.5) -> dict:
        self.send(osc("/sound/ping", t))
        data, _ = self.sock.recvfrom(4096)
        head = osc_str("/sound/pong") + osc_str(",ds")
        assert data.startswith(head)
        (echo,) = struct.unpack(">d", data[len(head):len(head) + 8])
        assert echo == t
        return json.loads(data[len(head) + 8:].rstrip(b"\0"))

    def close(self):
        self.sock.close()
        if self.proc.poll() is None:
            pgid = os.getpgid(self.proc.pid)
            os.killpg(pgid, signal.SIGKILL)               # our own group, by pgid
        self.proc.wait(timeout=10)


@pytest.fixture
def live(engine, world):
    started = []

    def start(*extra):
        lv = Live(engine, world, *extra)
        started.append(lv)
        return lv

    yield start
    for lv in started:
        lv.close()


def test_live_ping_answers_with_status(live):
    lv = live()
    s = lv.ping()
    assert s["output"] == "null" and s["port"] == lv.port and s["pid"] == lv.proc.pid
    assert s["voices"] == 0 and s["voices_max"] == 8 and s["sids"] == 0
    assert s["malformed"] == 0 and s["stale"] == 0


def test_live_a_real_datagram_arms_and_plays(live, world):
    record(world, delay=0)
    lv = live("--null-length", "0.3")
    lv.send(event("arm"))
    assert lv.wait_for("played")
    assert lv.wait_for("finished", timeout=5)["frames"] == 14400
    lv.send(event("end"))
    assert lv.wait_for("stopped")["from"] == "latched"
    assert lv.ping()["sids"] == 0


def test_live_the_short_form_is_accepted(live, world):
    record(world, delay=0)
    lv = live()
    lv.send(osc("/sound/event", "arm", SID))
    assert lv.wait_for("played")
    assert lv.ping()["malformed"] == 0


def test_live_a_delay_is_honoured(live, world):
    record(world, delay=0.4)
    lv = live()
    t0 = time.monotonic()
    lv.send(event("arm"))
    assert lv.wait_for("played", timeout=5)
    assert time.monotonic() - t0 >= 0.4


def test_live_malformed_are_dropped_and_the_engine_survives(live, world):
    record(world, delay=0)
    lv = live()
    for d in MALFORMED.values():
        lv.send(d)
    lv.send(b"\xff" * 5000)                           # bigger than the buffer: truncated
    s = lv.ping()
    assert s["malformed"] == len(MALFORMED) + 1
    assert not [x for x in lv.log if x["ev"] in ("armed", "played")]
    assert not [x for x in lv.log if x["ev"] == "malformed"]   # counted, never one line each


def test_live_a_stale_arm_is_dropped_and_a_stale_stop_is_not(live, world):
    record(world, delay=0)
    lv = live("--null-length", "30")
    lv.send(event("arm", t=time.time() - 5))
    assert lv.ping()["stale"] == 1
    assert not [x for x in lv.log if x["ev"] == "played"]
    lv.send(event("arm"))
    assert lv.wait_for("played")
    lv.send(event("stop", t=time.time() - 60))
    assert lv.wait_for("stopped")["by"] == "stop"


def test_live_the_real_mouth_files_hold_the_sound(live, world):
    """The guard reads <mouth>/playing.json plus a live player.pid, as busy does."""
    record(world, delay=0)
    mouth = world["mouth"]
    (mouth / "playing.json").write_text("{}")
    (mouth / "player.pid").write_text(f"{os.getpid()}\n")
    lv = live()
    lv.send(event("arm"))
    assert lv.wait_for("armed")
    time.sleep(0.4)
    assert not [x for x in lv.log if x["ev"] == "played"]
    (mouth / "playing.json").unlink()
    assert lv.wait_for("played", timeout=5)


def test_live_a_dead_mouth_pid_does_not_hold_the_sound(live, world):
    """playing.json left behind by a SIGKILLed player: not speaking."""
    record(world, delay=0)
    (world["mouth"] / "playing.json").write_text("{}")
    (world["mouth"] / "player.pid").write_text(str(dead_pid()))
    lv = live()
    lv.send(event("arm"))
    assert lv.wait_for("played")


def test_live_a_held_port_exits_nonzero_naming_bind(engine, world):
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    try:
        port = holder.getsockname()[1]
        r = subprocess.run([str(engine), "--null-output", "--port", str(port),
                            "--sessions", str(world["root"])],
                           capture_output=True, text=True, check=False, timeout=30)
        assert r.returncode == 1
        (err,) = lines(r.stderr)
        assert err["ev"] == "error" and err["what"] == "bind" and err["port"] == port
    finally:
        holder.close()


def test_live_sigterm_exits_0(engine, world):
    lv = Live(engine, world)
    try:
        lv.proc.send_signal(signal.SIGTERM)
        assert lv.proc.wait(timeout=10) == 0
        assert lv.wait_for("exit", timeout=2)["signal"] == "TERM"
    finally:
        lv.close()


def test_the_engine_imports_only_system_frameworks():
    """The system toolchain and nothing else (design.md decision 3)."""
    imports = [x.split()[1] for x in SRC.read_text().splitlines() if x.startswith("import ")]
    assert imports == ["AVFoundation", "CoreAudio", "Foundation", "Synchronization"]


def test_the_engine_never_touches_the_microphone():
    """`inputNode` would ask for the microphone (design.md decision 6)."""
    code = [x.split("//")[0] for x in SRC.read_text().splitlines()]
    assert not [x for x in code if "inputNode" in x or "AVCaptureDevice" in x]


RT_FORBIDDEN = ["append(", "Array(", "String(", "[]", "journal", "emit(", "trace", "print(",
                "lock", "Lock", "Dispatch", "allocate", "malloc", "fopen", "write(", "read(",
                "Date(", "json(", "self.", "as!", "as?", "[String", "map", "filter", "reduce",
                "\\(", "Sound", "holds", "sounds"]


def test_the_render_block_keeps_the_real_time_rules():
    """The render body (RT-BEGIN..RT-END) reads raw memory and atomics only:
    nothing in it may allocate, lock, log, or touch a class (retain/release)."""
    text = SRC.read_text()
    body = text[text.index("// RT-BEGIN"):text.index("// RT-END")]
    code = "\n".join(x.split("//")[0] for x in body.splitlines())
    assert "func render(" in code
    assert [w for w in RT_FORBIDDEN if w in code] == []


def cpu_seconds(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "cputime=", "-p", str(pid)],
                         capture_output=True, text=True, check=True).stdout.strip()
    m, s = out.split(":")[-2:]
    return int(m) * 60 + float(s)


def test_live_a_held_delay_zero_arm_polls_and_does_not_spin(live, world):
    """Regression: with `delay: 0` under a speaking mouth the deadline is always
    "now"; the engine must poll at the 100 ms tick, not wake back-to-back."""
    record(world, delay=0)
    (world["mouth"] / "playing.json").write_text("{}")
    (world["mouth"] / "player.pid").write_text(str(os.getpid()))
    lv = live()
    lv.send(event("arm"))
    assert lv.wait_for("armed")
    before = cpu_seconds(lv.proc.pid)
    time.sleep(1.5)
    assert cpu_seconds(lv.proc.pid) - before < 0.3     # a spin burns ~1.5 s here
    assert not [x for x in lv.log if x["ev"] == "played"]


# --- do-003: the audio, rendered OFFLINE (manual rendering mode, no device) -------
#
# `--script STEPS --offline OUT.wav` drives the same virtual time through the
# REAL graph: the source node's voices, the main mixer, the fades. The render
# is frame-exact (a voice started at t begins at frame round(t * 48000)), so the
# file says exactly what the speakers would have played. The sounds are DC
# levels written as 16-bit WAVs: a level survives decode, Int16 and the mixer
# exactly, so a sum of two is easy to read.

RATE = 48000
FADE = RATE * 20 // 1000                              # 960 frames
TOL = 1e-4


def wav(path: Path, levels, seconds: float, rate: int = RATE) -> Path:
    """A 16-bit WAV holding one constant level per channel (a float or a tuple)."""
    import wave
    levels = levels if isinstance(levels, tuple) else (levels,)
    frame = b"".join(struct.pack("<h", round(x * 32768)) for x in levels)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(len(levels))
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frame * round(seconds * rate))
    return path


def read_wav(path: Path) -> tuple[int, list[float], list[float]]:
    """The offline render: 48 kHz stereo float32 -> (rate, left, right)."""
    from array import array
    data = path.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    off, fmt, pcm = 12, None, None
    while off + 8 <= len(data):
        cid, size = data[off:off + 4], struct.unpack("<I", data[off + 4:off + 8])[0]
        body = data[off + 8:off + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            pcm = body
        off += 8 + size + (size & 1)
    tag, channels, rate, _, _, bits = fmt
    assert channels == 2 and bits == 32 and tag in (3, 0xFFFE)   # float (plain or extensible)
    samples = array("f")
    samples.frombytes(pcm)
    return rate, list(samples[0::2]), list(samples[1::2])


def render(engine, world, steps, *, until, extra=(), env=None):
    """Run a script through the offline graph: (log, left, right)."""
    path = world["tmp"] / "steps.json"
    path.write_text(json.dumps({"steps": steps, "until": until}))
    out = world["tmp"] / "out.wav"
    r = subprocess.run([str(engine), "--script", str(path), "--sessions", str(world["root"]),
                        "--offline", str(out), *extra],
                       capture_output=True, text=True, check=False, timeout=120, env=env)
    assert r.returncode == 0, r.stderr
    rate, left, right = read_wav(out)
    assert rate == RATE and len(left) == round(until * RATE)
    return lines(r.stderr), left, right


def level(xs: list[float], a: int, b: int, want: float):
    """Every frame in [a, b) sits at WANT."""
    bad = [(i, x) for i, x in enumerate(xs[a:b], a) if abs(x - want) > TOL]
    assert not bad, f"{len(bad)} frames off {want}, first {bad[:3]}"


@pytest.fixture
def sound(world):
    """A 2 s mono sound at level 0.25."""
    return wav(world["tmp"] / "busy.wav", 0.25, 2.0)


def test_offline_silence_before_the_delay_then_the_sound(engine, world, sound):
    record(world, delay=0.5, file=sound)
    log, left, right = render(engine, world, [arm(0)], until=1.5)
    assert [x["t"] for x in evs(log, "played")] == [0.5]
    for ch in (left, right):                          # mono plays on both sides
        level(ch, 0, RATE // 2, 0.0)                  # silence before the delay
        level(ch, RATE // 2, round(1.5 * RATE), 0.25)  # the sound from its exact frame


def test_offline_a_stop_fades_over_20_ms_then_silence(engine, world, sound):
    record(world, delay=0, file=sound)
    log, left, _ = render(engine, world, [arm(0), stop(1.0)], until=1.5)
    (s,) = evs(log, "stopped")
    assert s["from"] == "playing" and s["frames"] == RATE and s["fade_ms"] == 20
    level(left, 0, RATE, 0.25)
    fade = left[RATE:RATE + FADE]
    assert fade[0] == pytest.approx(0.25, abs=TOL)                    # starts from full
    assert all(a > b for a, b in itertools.pairwise(fade))                 # strictly down: no click
    assert fade[FADE // 2] == pytest.approx(0.125, abs=1e-3)          # linear
    assert 0 < fade[-1] < 0.001
    level(left, RATE + FADE, len(left), 0.0)
    assert log[-1]["voices"] == 0 and log[-1]["fading"] == 0


def test_offline_the_mouth_cuts_the_sound_with_a_fade_and_it_stays_cut(engine, world, sound):
    record(world, delay=0, file=sound)
    steps = [arm(0), {"t": 0.5, "mouth": True}, {"t": 0.75, "mouth": False}, arm(1.0)]
    log, left, _ = render(engine, world, steps, until=1.5)
    (cut,) = evs(log, "cut")
    assert cut["t"] == 0.5 and cut["frames"] == RATE // 2
    level(left, 0, RATE // 2, 0.25)
    fade = left[RATE // 2:RATE // 2 + FADE]
    assert fade[0] == pytest.approx(0.25, abs=TOL) and all(a > b for a, b in itertools.pairwise(fade))
    level(left, RATE // 2 + FADE, len(left), 0.0)    # latched: the second arm replays nothing
    assert len(evs(log, "played")) == 1


def test_offline_two_sids_are_mixed(engine, world):
    a = wav(world["tmp"] / "a.wav", 0.2, 1.0)                 # mono
    b = wav(world["tmp"] / "b.wav", (0.3, -0.1), 2.0)         # stereo: each side its own
    record(world, SID, delay=0, file=a)
    record(world, SID2, delay=0.5, file=b)
    log, left, right = render(engine, world, [arm(0, SID), arm(0, SID2)], until=2.0)
    assert sorted((x["sid"], x["t"]) for x in evs(log, "played")) == [(SID, 0), (SID2, 0.5)]
    level(left, 0, RATE // 2, 0.2)
    level(right, 0, RATE // 2, 0.2)
    level(left, RATE // 2, RATE, 0.5)                 # both: the sum
    level(right, RATE // 2, RATE, 0.1)
    level(left, RATE, 2 * RATE, 0.3)                  # a played out at 1.0: b alone
    level(right, RATE, 2 * RATE, -0.1)
    (fin,) = [x for x in evs(log, "finished") if x["sid"] == SID]
    assert fin["t"] == 1.0 and fin["frames"] == RATE  # played out, frame-exact


def test_offline_a_sound_plays_out_then_latches(engine, world):
    short = wav(world["tmp"] / "short.wav", 0.25, 0.5)
    record(world, delay=0, file=short)
    log, left, _ = render(engine, world, [arm(0), arm(1.0)], until=1.5)
    (fin,) = evs(log, "finished")
    assert fin["t"] == 0.5 and fin["frames"] == RATE // 2
    level(left, 0, RATE // 2, 0.25)
    level(left, RATE // 2, len(left), 0.0)            # no fade on a natural end, and no replay
    assert len(evs(log, "played")) == 1


def test_offline_the_next_batch_reuses_the_cached_decode(engine, world, sound):
    record(world, delay=0, file=sound)
    log, left, _ = render(engine, world, [arm(0), stop(0.5), arm(1.0), {"t": 1.25, "ping": 1}],
                          until=1.5)
    assert [x["t"] for x in evs(log, "played")] == [0, 1.0]
    (st,) = evs(log, "status")
    assert st["decoded"] == 1 and st["decoded_bytes"] == 2 * RATE * 4   # 48 kHz stereo Int16
    level(left, RATE, round(1.5 * RATE), 0.25)


@pytest.mark.parametrize("vol, want", [("0.5", 0.125), ("0", 0.0)])
def test_offline_volume_scales_the_output(engine, world, sound, vol, want):
    record(world, delay=0, file=sound)
    log, left, right = render(engine, world, [arm(0), {"t": 0.5, "ping": 1}], until=1.0,
                              extra=["--volume", vol])
    level(left, 0, RATE, want)
    level(right, 0, RATE, want)
    assert evs(log, "status")[0]["volume"] == float(vol)


def test_offline_a_44k1_file_is_resampled_to_48k(engine, world):
    cd = wav(world["tmp"] / "cd.wav", 0.25, 1.0, rate=44100)
    record(world, delay=0, file=cd)
    log, left, _ = render(engine, world, [arm(0)], until=1.5)
    (fin,) = evs(log, "finished")
    assert abs(fin["frames"] - RATE) <= 64            # 1 s, now in 48 kHz frames
    level(left, 1000, RATE - 1000, 0.25)              # away from the converter's edges
    level(left, RATE + 1000, len(left), 0.0)


def test_offline_an_undecodable_file_is_refused_and_silent(engine, world):
    record(world, delay=0)                            # world's busy.wav is not audio
    log, left, _ = render(engine, world, [arm(0), {"t": 0.5, "ping": 1}], until=1.0)
    (r,) = evs(log, "refused")
    assert r["reason"] == "undecodable"
    st = evs(log, "status")[0]
    assert st["refused"] == 1 and st["last_refused"]["reason"] == "undecodable"
    level(left, 0, len(left), 0.0)


def test_offline_eviction_frees_the_decode(engine, world, sound):
    record(world, delay=0, file=sound)
    log, _, _ = render(engine, world, [arm(0), stop(0.5), {"t": 1.0, "ping": 1},
                                       {"t": 3.0, "ping": 1}],
                       until=3.5, extra=["--evict-after", "2"])
    first, later = evs(log, "status")
    assert first["decoded"] == 1 and later["decoded"] == 0
    assert [x["t"] for x in evs(log, "evicted")] == [2.5]


def test_offline_the_trace_names_each_voice_start_frame(engine, world, sound):
    record(world, SID, delay=0.25, file=sound)
    record(world, SID2, delay=0.5, file=sound)
    tr = world["tmp"] / "trace.jsonl"
    render(engine, world, [arm(0, SID), arm(0, SID2)], until=1.0, extra=["--trace", str(tr)])
    voices = lines(tr.read_text())
    assert sorted((x["sid"], x["sample"]) for x in voices) == [(SID, RATE // 4), (SID2, RATE // 2)]
    assert all(x["ev"] == "voice" and x["host_ns"] is None and x["output"] == "offline"
               for x in voices)                       # offline: no host time to give


def test_offline_status_says_what_the_output_is(engine, world):
    log, left, _ = render(engine, world, [{"t": 0.1, "ping": 1}], until=0.2)
    st = evs(log, "status")[0]
    assert st["output"] == "offline" and st["rate"] == RATE and st["voices_max"] == 8
    assert st["decoded"] == 0 and st["refused"] == 0 and st["rendered_frames"] == round(0.1 * RATE)
    level(left, 0, len(left), 0.0)


# --- do-003: TCC. A file in a protected folder is refused UNOPENED ----------------
#
# conftest sets HOME to a fresh dir, so "$HOME/Documents" here is a plain test
# dir: nothing real is protected or touched. The check runs before any output
# loads the file, so it holds for the null output too (no AVFoundation needed).

@pytest.mark.parametrize("where", ["Desktop", "Documents", "Downloads", "Library/Mobile Documents",
                                   "documents"])                   # APFS ignores case
def test_a_file_in_a_tcc_folder_is_refused(engine, world, where):
    home = Path(os.environ["HOME"])
    path = home / where / "busy.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    wav(path, 0.25, 0.5)
    record(world, delay=0, file=path)
    log = script(engine, world, [arm(0), {"t": 0.5, "ping": 1}], until=1)
    (r,) = evs(log, "refused")
    assert r["reason"] == "tcc" and evs(log, "played") == []
    assert evs(log, "status")[0]["last_refused"]["reason"] == "tcc"


def test_a_symlink_into_a_tcc_folder_is_refused(engine, world):
    home = Path(os.environ["HOME"])
    (home / "Downloads").mkdir(parents=True, exist_ok=True)
    wav(home / "Downloads" / "busy.wav", 0.25, 0.5)
    link = world["tmp"] / "innocent.wav"
    link.symlink_to(home / "Downloads" / "busy.wav")
    hop = world["tmp"] / "dir-link"
    hop.symlink_to(home / "Downloads")                    # a directory link, then the file
    for f in (link, hop / "busy.wav", world["tmp"] / "x" / ".." / "innocent.wav"):
        record(world, delay=0, file=f)
        log = script(engine, world, [arm(0)], until=0.5)
        assert [x["reason"] for x in evs(log, "refused")] == ["tcc"], f


def test_a_file_outside_the_tcc_folders_still_plays_offline(engine, world):
    """The check refuses only what it must: a sibling named like one plays."""
    home = Path(os.environ["HOME"])
    path = home / "Documents-not" / "busy.wav"
    path.parent.mkdir(parents=True)
    wav(path, 0.25, 1.0)
    record(world, delay=0, file=path)
    log, left, _ = render(engine, world, [arm(0)], until=0.5)
    assert evs(log, "refused") == [] and len(evs(log, "played")) == 1
    level(left, 0, len(left), 0.25)


# --- do-003: the trace, live (--null-output: real UDP, no device) -------------------

def test_live_the_trace_has_one_line_per_datagram_with_its_host_time(live, world):
    tr = world["tmp"] / "trace.jsonl"
    record(world, delay=0)
    before = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
    lv = live("--trace", str(tr))
    lv.send(event("arm"))
    lv.send(osc("/sound/event", "end", SID))
    lv.send(b"junk")
    lv.ping()
    after = time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
    got = lines(tr.read_text())
    assert [(x["ev"], x["kind"]) for x in got] == [("datagram", "event"), ("datagram", "event"),
                                                   ("datagram", "malformed"), ("datagram", "ping")]
    assert [x.get("event") for x in got[:2]] == ["arm", "end"]
    assert got[0]["sid"] == SID and isinstance(got[0]["t"], float) and got[1]["t"] is None
    hosts = [x["host_ns"] for x in got]
    assert before <= hosts[0] and hosts == sorted(hosts) and hosts[-1] <= after   # CLOCK_UPTIME_RAW


# --- do-001 review N1: the no-record latch is pruned ---------------------------------

def test_the_no_record_latch_forgets_a_sid_after_ten_minutes(engine, world):
    """`refused` (sid -> the last refusal logged) no longer grows forever: a
    sid refused more than ten minutes ago is pruned, and may log once again."""
    log = script(engine, world, [arm(0, SID), arm(1, SID), arm(700, SID2), arm(701, SID)],
                 until=702)
    assert [(x["ev"], x["sid"], x["t"]) for x in log if x["ev"] == "no-record"] == [
        ("no-record", SID, 0), ("no-record", SID2, 700), ("no-record", SID, 701)]


# --- do-006: the TCC refusal, widened (the super's ruling, do-003 review N2) ---------
#
# Every path the engine would touch is classified from its STRING first: the
# sound file, the mouth's playing.json and player.pid, the sessions root. Nothing
# here creates or touches anything under the real ~/Documents, ~/Desktop,
# ~/Downloads, ~/Library/CloudStorage or /Volumes: HOME is conftest's fresh dir,
# /Volumes and /net paths are strings that are never looked at, and a network or
# removable mount is a temp dir declared with --mounts. `--classify` reports every
# path the walk lstat()ed, which is the proof that a refused place is not touched.

def classify(engine, *paths, mounts=None, cwd=None) -> list[dict]:
    argv = [str(engine)]
    for p in paths:
        argv += ["--classify", str(p)]
    if mounts is not None:
        argv += ["--mounts", str(mounts)]
    r = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30, cwd=cwd)
    assert r.returncode == 0, r.stderr
    return lines(r.stdout)


def mounts_file(world, **kinds) -> Path:
    """--mounts FILE: each kind -> a temp dir to treat as that kind of mount."""
    f = world["tmp"] / "mounts.json"
    f.write_text(json.dumps([{"on": str(on), "kind": k} for k, on in kinds.items()]))
    return f


def untouched(got: dict, root: str | Path) -> bool:
    """Nothing the walk lstat()ed is ROOT or inside it."""
    r = str(root).lower().rstrip("/")
    return all(t.lower() != r and not t.lower().startswith(r + "/") for t in got["touched"])


def script_x(engine, world, steps, *extra, until=None, length=1.0, sessions=None) -> list[dict]:
    """`script`, with extra engine arguments (--mounts) and another sessions root."""
    path = world["tmp"] / "steps.json"
    doc = {"steps": steps}
    if until is not None:
        doc["until"] = until
    path.write_text(json.dumps(doc))
    r = subprocess.run([str(engine), "--script", str(path),
                        "--sessions", str(sessions or world["root"]),
                        "--null-length", str(length), *extra],
                       capture_output=True, text=True, check=False, timeout=20)
    assert r.returncode == 0, r.stderr
    return lines(r.stderr)


HOME_PLACES = [("Desktop", "folder"), ("Documents", "folder"), ("Downloads", "folder"),
               ("documents", "folder"),                                  # APFS ignores case
               ("Library/Mobile Documents/com~apple~CloudDocs", "icloud"),
               ("Library/CloudStorage/Dropbox", "cloud"),
               ("Library/CloudStorage/GoogleDrive-a@b.c/My Drive", "cloud")]


@pytest.mark.parametrize("where,kind", HOME_PLACES)
def test_classify_refuses_the_home_places_without_touching_them(engine, where, kind):
    home = Path(os.environ["HOME"])
    top = home / where.split("/")[0] if not where.startswith("Library/") else \
        home / "/".join(where.split("/")[:2])
    (got,) = classify(engine, home / where / "busy.wav")
    assert got["refused"] == kind and untouched(got, top)


@pytest.mark.parametrize("path,kind,root", [
    ("/Volumes/USBSTICK/busy.wav", "volume", "/Volumes/USBSTICK"),
    ("/volumes/usbstick/busy.wav", "volume", "/Volumes/USBSTICK"),
    ("/System/Volumes/Data/Volumes/Backup/busy.wav", "volume", "/System/Volumes/Data/Volumes/Backup"),
    ("/net/fileserver/share/busy.wav", "network", "/net/fileserver"),
    ("/Network/Servers/host/busy.wav", "network", "/Network/Servers"),
])
def test_classify_refuses_volumes_and_the_automounter_from_the_string(engine, path, kind, root):
    (got,) = classify(engine, path)
    assert got["refused"] == kind and untouched(got, root)


def test_classify_refuses_network_and_removable_mounts_anywhere(engine, world):
    share, stick = world["tmp"] / "share", world["tmp"] / "stick"
    share.mkdir()
    stick.mkdir()
    f = mounts_file(world, network=share, removable=stick)
    a, b, c = classify(engine, share / "mouth" / "player.pid", stick / "busy.wav", share, mounts=f)
    assert a["refused"] == "network" and untouched(a, share)
    assert b["refused"] == "removable" and untouched(b, stick)
    assert c["refused"] == "network" and untouched(c, share)            # the mount point itself


def test_classify_reads_the_kernels_mount_table(engine):
    """A real non-local mount (one host had autofs on
    /System/Volumes/Data/home) is refused from getfsstat(MNT_NOWAIT) alone: nothing is mounted for the test."""
    out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=False).stdout
    nonlocal_ = [m.group(1) for m in (re.match(r".+? on (/.*) \((.*)\)$", x) for x in out.splitlines())
                 if m and m.group(1) != "/" and "local" not in m.group(2).split(", ")]
    if not nonlocal_:
        pytest.skip("this host has no non-local mount to read")
    (got,) = classify(engine, nonlocal_[0] + "/someone/busy.wav")
    assert got["refused"] == "network" and untouched(got, nonlocal_[0])


def test_classify_follows_a_symlink_but_never_into_the_place(engine, world):
    share = world["tmp"] / "share"
    share.mkdir()
    f = mounts_file(world, network=share)
    to_volume = world["tmp"] / "to-volume"
    to_volume.symlink_to("/Volumes/USBSTICK")                 # a string: never looked at
    to_share = world["tmp"] / "to-share"
    to_share.symlink_to(share)
    to_docs = world["tmp"] / "to-docs"
    to_docs.symlink_to(Path(os.environ["HOME"]) / "Documents")
    a, b, c = classify(engine, to_volume / "busy.wav", to_share / "busy.wav", to_docs / "x" / "y",
                       mounts=f)
    assert a["refused"] == "volume" and untouched(a, "/Volumes/USBSTICK")
    assert str(to_volume) in a["touched"]                    # the link itself was read
    assert b["refused"] == "network" and untouched(b, share)
    assert c["refused"] == "folder" and untouched(c, Path(os.environ["HOME"]) / "Documents")


def test_classify_allows_what_it_must(engine, world):
    """Only the places themselves: lookalike names, a dir called Volumes that is
    not /Volumes, a relative path, and an ordinary temp file all pass."""
    home = Path(os.environ["HOME"])
    paths = [world["sound"], home / "Documents-not" / "x.wav", home / "Library" / "CloudStorageX",
             world["tmp"] / "Volumes" / "x.wav", world["tmp"] / "net" / "x"]
    got = classify(engine, *paths, "busy.wav", cwd=world["tmp"])
    assert [g["refused"] for g in got] == [None] * (len(paths) + 1)
    assert got[-1]["touched"][-1] == str(world["tmp"] / "busy.wav")     # from the cwd


def test_bad_mounts_exit_2(engine, world):
    bad = world["tmp"] / "bad.json"
    for body in ("{}", '[{"on": "rel", "kind": "network"}]', '[{"on": "/x", "kind": "fast"}]'):
        bad.write_text(body)
        r = subprocess.run([str(engine), "--classify", "/x", "--mounts", str(bad)],
                           capture_output=True, text=True, check=False, timeout=30)
        assert r.returncode == 2 and "--mounts" in r.stderr, body


# The engine itself: a refused mouth latches its seat at arm, silent, before
# any tick reads it; every other seat goes on.

def mouth_cases(world):
    """(the mouth dir string, its class, extra args). The share is real (a temp
    dir declared a network mount) and holds a speaking mouth whose player.pid is
    a FIFO: an engine that opened it would block, and the run would time out."""
    share = world["tmp"] / "share"
    (share / "mouth").mkdir(parents=True)
    (share / "mouth" / "playing.json").write_text("{}")
    os.mkfifo(share / "mouth" / "player.pid")
    f = mounts_file(world, network=share)
    home = Path(os.environ["HOME"])
    return [(home / "Documents" / "voicemode" / "mouth", "folder", ()),   # VOICEMODE_MOUTH_DIR there
            (home / "Library" / "CloudStorage" / "Dropbox" / "mouth", "cloud", ()),
            (Path("/Volumes/USBSTICK/mouth"), "volume", ()),
            (share / "mouth", "network", ("--mounts", str(f)))]


def test_a_refused_mouth_is_refused_at_arm_and_its_seat_stays_silent(engine, world):
    for mouth, kind, extra in mouth_cases(world):
        rec = {"v": 1, "file": str(world["sound"]), "delay": 0, "pid": os.getpid(),
               "mouth": str(mouth)}
        record(world, SID, body=rec)
        record(world, SID2, delay=0.5)                       # another seat, an ordinary mouth
        log = script_x(engine, world, [arm(0, SID), arm(0.1, SID2), {"t": 1.0, "ping": 1},
                                       arm(2, SID)], *extra, until=3)
        (r,) = evs(log, "refused")                           # once: the repeat arm is a no-op
        assert (r["what"], r["reason"], r["class"], r["sid"], r["t"]) == ("mouth", "tcc", kind, SID, 0)
        assert r["path"] == str(mouth / "playing.json"), kind
        assert [x["sid"] for x in evs(log, "played")] == [SID2], kind   # SID2 plays, SID never
        st = evs(log, "status")[0]
        assert st["refused"] == 1 and st["last_refused"]["what"] == "mouth", kind


def test_after_a_refused_mouth_the_next_batch_is_refused_again(engine, world):
    rec = {"v": 1, "file": str(world["sound"]), "delay": 0, "pid": os.getpid(),
           "mouth": "/Volumes/USBSTICK/mouth"}
    record(world, body=rec)
    log = script_x(engine, world, [arm(0), stop(1), arm(2)], until=3)
    assert [x["t"] for x in evs(log, "refused")] == [0, 2] and evs(log, "played") == []


@pytest.mark.parametrize("where,kind", [("Library/CloudStorage/Dropbox", "cloud"),
                                        ("Library/Mobile Documents/com~apple~CloudDocs", "icloud")])
def test_a_sound_file_in_a_cloud_folder_is_refused(engine, world, where, kind):
    path = Path(os.environ["HOME"]) / where / "busy.wav"
    record(world, delay=0, file=path)                        # never created: never looked at
    log = script(engine, world, [arm(0)], until=0.5)
    (r,) = evs(log, "refused")
    assert (r["reason"], r["class"], r["file"]) == ("tcc", kind, str(path))
    assert evs(log, "played") == []


def test_a_sound_file_on_a_volume_or_a_share_is_refused(engine, world):
    share = world["tmp"] / "share"
    share.mkdir()
    f = mounts_file(world, network=share)
    for path, kind in ((Path("/Volumes/USBSTICK/busy.wav"), "volume"), (share / "busy.wav", "network")):
        record(world, delay=0, file=path)
        log = script_x(engine, world, [arm(0)], "--mounts", str(f), until=0.5)
        assert [(x["reason"], x["class"]) for x in evs(log, "refused")] == [("tcc", kind)], kind
        assert evs(log, "played") == []


def test_a_sessions_root_in_a_protected_place_is_never_read(engine, world):
    root = Path(os.environ["HOME"]) / "Documents" / "sessions"   # never created
    log = script_x(engine, world, [arm(0), {"t": 0.5, "ping": 1}], until=1, sessions=root)
    (r,) = evs(log, "refused")
    assert (r["what"], r["path"], r["class"]) == ("sessions", str(root), "folder")
    assert [x["t"] for x in evs(log, "sessions-refused")] == [0]
    assert evs(log, "played") == [] and evs(log, "status")[0]["sids"] == 0


def test_live_a_mouth_that_turns_protected_after_arm_holds_silence(live, world):
    """The mouth is a symlink that moves into a protected place after the arm
    (the engine re-classifies every tick). It is refused once, at the tick,
    counts as speaking (silence), its FIFO player.pid is never opened, and the
    engine keeps answering."""
    home = Path(os.environ["HOME"])
    docs_mouth = home / "Documents" / "mouth"
    docs_mouth.mkdir(parents=True)
    (docs_mouth / "playing.json").write_text("{}")
    os.mkfifo(docs_mouth / "player.pid")
    link = world["tmp"] / "mouth-link"
    link.symlink_to(world["mouth"])                           # an ordinary, silent mouth at arm
    record(world, body={"v": 1, "file": str(world["sound"]), "delay": 3.0, "pid": os.getpid(),
                        "mouth": str(link)})
    lv = live()
    lv.send(event("arm"))
    assert lv.wait_for("armed")
    tmp_link = world["tmp"] / "mouth-link.new"
    tmp_link.symlink_to(docs_mouth)
    os.replace(tmp_link, link)                                # now it points into Documents
    r = lv.wait_for("refused", timeout=5)
    assert r and (r["what"], r["at"], r["class"]) == ("mouth", "tick", "folder")
    assert r["path"] == str(link / "playing.json")
    time.sleep(3.5)                                           # past the 3 s delay: still silent
    assert lv.wait_for("played", timeout=0.1) is None
    s = lv.ping()                                             # alive: the FIFO was never opened
    assert s["sids"] == 1 and len(evs(lv.log, "refused")) == 1   # logged once, not per tick


# --- do-008: the TCC tail (do-006 review N1-N4) -------------------------------------
#
# The same wall as do-006: HOME is conftest's fresh dir, so "$HOME/Documents" and
# "$HOME/Library/Containers" here are plain test dirs (made only where a test needs
# something real to be there), and every other protected path is a string that is
# never looked at.

def fake_home() -> Path:
    """conftest's fresh HOME, never the account's real one."""
    home = Path(os.environ["HOME"])
    assert home != Path(pwd.getpwuid(os.getuid()).pw_dir), "HOME is the real home"
    return home


def seat_record(slot: Path, *, file: Path, mouth: Path, delay: float = 0) -> Path:
    """A valid record for this process in SLOT (a record dir anywhere)."""
    slot.mkdir(parents=True, exist_ok=True)
    path = slot / f"engine.{os.getpid()}"
    path.write_text(json.dumps({"v": 1, "file": str(file), "delay": delay, "pid": os.getpid(),
                                "mouth": str(mouth)}))
    return path


# N3: other apps' data (macOS 14's App Data prompt) and the Full Disk Access stores.

APP_PLACES = [("Library/Containers/com.apple.mail/Data/x.wav", "appdata", "Library/Containers"),
              ("Library/Group Containers/group.com.apple.notes/x.wav", "appdata",
               "Library/Group Containers"),
              ("library/group containers/g/x.wav", "appdata", "Library/Group Containers"),
              ("Library/Mail/V10/x.wav", "private", "Library/Mail"),
              ("Library/Messages/chat.db", "private", "Library/Messages")]


@pytest.mark.parametrize("where,kind,top", APP_PLACES)
def test_classify_refuses_other_apps_data_without_touching_it(engine, where, kind, top):
    home = fake_home()
    (got,) = classify(engine, home / where)
    assert got["refused"] == kind and untouched(got, home / top)


def test_classify_still_allows_lookalikes_of_the_app_places(engine):
    home = fake_home()
    paths = [home / "Library" / "ContainersX" / "x.wav", home / "Library" / "Mailbox" / "x.wav",
             home / "Library" / "Messages-old" / "x", home / "Containers" / "x.wav",
             home / "Library" / "Group" / "Containers" / "x"]
    assert [g["refused"] for g in classify(engine, *paths)] == [None] * len(paths)


def test_a_sound_file_in_another_apps_container_is_refused(engine, world):
    path = fake_home() / "Library" / "Containers" / "com.other.app" / "Data" / "busy.wav"
    record(world, delay=0, file=path)                        # never created: never looked at
    log = script(engine, world, [arm(0)], until=0.5)
    (r,) = evs(log, "refused")
    assert (r["reason"], r["class"], r["file"]) == ("tcc", "appdata", str(path))
    assert evs(log, "played") == []


def test_a_mouth_in_a_group_container_is_refused_at_arm(engine, world):
    mouth = fake_home() / "Library" / "Group Containers" / "group.x" / "mouth"
    record(world, body={"v": 1, "file": str(world["sound"]), "delay": 0, "pid": os.getpid(),
                        "mouth": str(mouth)})
    log = script(engine, world, [arm(0)], until=0.5)
    (r,) = evs(log, "refused")
    assert (r["what"], r["class"], r["path"]) == ("mouth", "appdata", str(mouth / "playing.json"))


# N1: each seat's record dir is classified before it is listed.

def test_a_session_dir_linked_into_documents_is_refused_not_listed(engine, world):
    """root/<SID> is a link into Documents, which holds a VALID record: had the
    engine listed it, SID would have armed and played."""
    docs_seat = fake_home() / "Documents" / "seat"
    seat_record(docs_seat / "sounds" / "busy", file=world["sound"], mouth=world["mouth"])
    (world["root"] / SID).symlink_to(docs_seat)
    record(world, SID2, delay=0.5)                           # another seat, an ordinary one
    log = script(engine, world, [arm(0, SID), arm(0.1, SID2), arm(1, SID), {"t": 1.5, "ping": 1}],
                 until=2)
    (r,) = evs(log, "refused")                               # once: the repeat arm says nothing
    assert (r["what"], r["reason"], r["class"], r["sid"], r["at"], r["t"]) == \
        ("records", "tcc", "folder", SID, "arm", 0)
    assert r["path"] == str(world["root"] / SID / "sounds" / "busy")
    assert evs(log, "armed")[0]["sid"] == SID2 and len(evs(log, "armed")) == 1
    assert [x["sid"] for x in evs(log, "played")] == [SID2]
    st = evs(log, "status")[0]
    assert st["refused"] == 1 and st["last_refused"]["what"] == "records"


def test_a_sounds_busy_link_into_a_cloud_folder_is_refused(engine, world):
    cloud = fake_home() / "Library" / "CloudStorage" / "Dropbox" / "busy"
    seat_record(cloud, file=world["sound"], mouth=world["mouth"])
    (world["root"] / SID / "sounds").mkdir(parents=True)
    (world["root"] / SID / "sounds" / "busy").symlink_to(cloud)
    log = script(engine, world, [arm(0)], until=0.5)
    (r,) = evs(log, "refused")
    assert (r["what"], r["class"]) == ("records", "cloud") and evs(log, "played") == []


def test_a_session_dir_linked_to_a_volume_is_refused_from_the_string(engine, world):
    (world["root"] / SID).symlink_to("/Volumes/USBSTICK/seat")  # a string: never looked at
    log = script(engine, world, [arm(0)], until=0.5)
    (r,) = evs(log, "refused")
    assert (r["what"], r["class"]) == ("records", "volume") and evs(log, "armed") == []


def test_a_record_that_links_into_documents_is_refused(engine, world):
    real = seat_record(fake_home() / "Documents" / "rec", file=world["sound"], mouth=world["mouth"])
    slot = world["root"] / SID / "sounds" / "busy"
    slot.mkdir(parents=True)
    (slot / real.name).symlink_to(real)
    log = script(engine, world, [arm(0)], until=0.5)
    (r,) = evs(log, "refused")
    assert (r["what"], r["class"], r["path"]) == ("records", "folder", str(slot / real.name))
    assert evs(log, "armed") == []


def test_links_that_stay_in_allowed_places_still_arm(engine, world):
    """No false refusal: a linked session dir and a linked record, both in temp dirs."""
    elsewhere = world["tmp"] / "elsewhere"
    seat_record(elsewhere / "sounds" / "busy", file=world["sound"], mouth=world["mouth"])
    (world["root"] / SID).symlink_to(elsewhere)
    real = seat_record(world["tmp"] / "rec", file=world["sound"], mouth=world["mouth"])
    slot = world["root"] / SID2 / "sounds" / "busy"
    slot.mkdir(parents=True)
    (slot / real.name).symlink_to(real)
    log = script(engine, world, [arm(0, SID), arm(0, SID2)], until=0.5)
    assert evs(log, "refused") == []
    assert sorted(x["sid"] for x in evs(log, "played")) == sorted([SID, SID2])


def test_live_an_allowed_session_dir_is_classified_again_at_the_next_arm(live, world):
    """Only refusals are cached: a seat dir that was fine at one arm and is a
    link into Documents at the next is refused, not listed from a stale verdict."""
    ok = world["tmp"] / "seat-ok"
    seat_record(ok / "sounds" / "busy", file=world["sound"], mouth=world["mouth"], delay=30)
    docs_seat = fake_home() / "Documents" / "seat"
    seat_record(docs_seat / "sounds" / "busy", file=world["sound"], mouth=world["mouth"], delay=30)
    link = world["root"] / SID
    link.symlink_to(ok)
    lv = live()
    lv.send(event("arm"))
    assert lv.wait_for("armed")
    lv.send(event("end"))
    assert lv.wait_for("stopped")
    tmp_link = world["root"] / "seat.new"
    tmp_link.symlink_to(docs_seat)
    os.replace(tmp_link, link)                               # now it leads into Documents
    lv.send(event("arm"))
    r = lv.wait_for("refused")
    assert r and (r["what"], r["class"], r["sid"]) == ("records", "folder", SID)
    s = lv.ping()
    assert s["sids"] == 0 and s["refused"] == 1 and len(evs(lv.log, "armed")) == 1


# N2 and N4: a mouth that turns refused at a tick is counted like one refused at
# arm, once while it stays refused, and again if it comes back and is refused anew.

def test_live_a_tick_time_mouth_refusal_is_counted_once_per_episode(live, world):
    home = fake_home()
    docs_mouth = home / "Documents" / "mouth"
    docs_mouth.mkdir(parents=True)
    (docs_mouth / "playing.json").write_text("{}")
    os.mkfifo(docs_mouth / "player.pid")                     # opened, it would hang the engine
    link = world["tmp"] / "mouth-link"
    link.symlink_to(world["mouth"])

    def point(to: Path):
        tmp_link = world["tmp"] / "mouth-link.new"
        tmp_link.symlink_to(to)
        os.replace(tmp_link, link)

    record(world, body={"v": 1, "file": str(world["sound"]), "delay": 30.0, "pid": os.getpid(),
                        "mouth": str(link)})
    lv = live()
    lv.send(event("arm"))
    assert lv.wait_for("armed")
    assert lv.ping()["refused"] == 0
    point(docs_mouth)
    r = lv.wait_for("refused")
    assert r and (r["what"], r["at"], r["class"], r["sid"]) == ("mouth", "tick", "folder", SID)
    time.sleep(0.5)                                          # five more ticks, still refused
    s = lv.ping()
    assert s["refused"] == 1 and s["last_refused"]["at"] == "tick"
    assert s["last_refused"]["sid"] == SID and len(evs(lv.log, "refused")) == 1
    point(world["mouth"])                                    # back to an ordinary mouth
    time.sleep(0.5)
    point(docs_mouth)                                        # refused anew: logged and counted
    assert lv.wait_for("refused", n=2)
    s = lv.ping()
    assert s["refused"] == 2 and s["sids"] == 1 and lv.wait_for("played", timeout=0.1) is None


# The engine reads the paths its check resolved, not the links again (a link
# that moved between the check and the read led the do-006 live test into its
# FIFO, about 1 run in 12). Links that stay in allowed places must still work.

def test_a_mouth_reached_through_links_still_speaks(engine, world):
    real = world["tmp"] / "real-mouth"
    real.mkdir()
    (real / "playing.json").write_text("{}")
    pidfile = world["tmp"] / "pid"
    pidfile.write_text(str(os.getpid()))                     # a live pid: speaking
    (real / "player.pid").symlink_to(pidfile)
    link = world["tmp"] / "mouth-link"
    link.symlink_to(real)
    record(world, body={"v": 1, "file": str(world["sound"]), "delay": 0, "pid": os.getpid(),
                        "mouth": str(link)})
    log = script(engine, world, [arm(0)], until=1)
    assert evs(log, "refused") == [] and evs(log, "played") == []   # held by the mouth
    pidfile.write_text(str(dead_pid()))                      # the player is gone: silent
    log = script(engine, world, [arm(0)], until=1)
    assert [x["sid"] for x in evs(log, "played")] == [SID]


# --- do-009: the read takes the checked path, never the name again -------------------
#
# A `relink` step arms a race in --script: at PATH's next gap (the instant between
# its TCC check and its read) the engine itself renames a fresh link over PATH, as a
# racing writer would. So the swap lands exactly between the check and the read,
# every run. The protected targets are under conftest's fake HOME (fake_home()).

def relink(t, path: Path, to: Path) -> dict:
    return {"t": t, "relink": str(path), "to": str(to)}


def test_offline_a_sound_link_moved_after_the_check_plays_what_was_checked(engine, world, sound):
    """busy.wav is reached through a link. Between the check and the decode the
    link moves into Documents, where a LOUDER sound waits: the render holds the
    checked sound's level (0.25), never the swapped target's (0.5)."""
    docs = fake_home() / "Documents"
    docs.mkdir(parents=True)
    loud = wav(docs / "loud.wav", 0.5, 2.0)
    name = world["tmp"] / "busy-link.wav"
    name.symlink_to(sound)
    record(world, delay=0, file=name)
    log, left, _ = render(engine, world, [relink(0, name, loud), arm(0), stop(0.5), arm(1.0),
                                          {"t": 1.25, "ping": 1}], until=1.5)
    (moved,) = evs(log, "relinked")
    assert moved["path"] == str(name) and os.readlink(name) == str(loud)
    (played,) = evs(log, "played")
    assert played["t"] == 0 and played["file"] == str(name)        # the name, as before
    level(left, 0, RATE // 2 - FADE, 0.25)                          # the checked file, not loud.wav
    (r,) = evs(log, "refused")                                      # the next check sees the move
    assert (r["t"], r["reason"], r["class"], r["file"]) == (1.0, "tcc", "folder", str(name))
    level(left, RATE, len(left), 0.0)
    assert evs(log, "status")[0]["refused"] == 1


def test_a_sound_link_moved_to_a_missing_place_after_the_check_still_plays(engine, world):
    """The null output too: it reads the checked path. Had it looked the name up
    again, the dangling link would have made the sound unreadable."""
    name = world["tmp"] / "busy-link.wav"
    name.symlink_to(world["sound"])
    record(world, delay=0, file=name)
    gone = fake_home() / "Documents" / "never" / "made.wav"        # never created
    log = script(engine, world, [relink(0, name, gone), arm(0)], until=0.5)
    assert len(evs(log, "relinked")) == 1 and evs(log, "refused") == []
    assert [x["file"] for x in evs(log, "played")] == [str(name)]
    assert not gone.parent.exists()


def test_offline_a_name_that_leads_elsewhere_is_decoded_again(engine, world, sound):
    """The cache is the name's, with the checked path stored: once the name
    resolves to another allowed file, the next batch decodes that file."""
    other = wav(world["tmp"] / "other.wav", 0.5, 2.0)
    st = sound.stat()
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))            # same size, same mtime:
    assert other.stat().st_size == st.st_size                        # only the path tells them apart
    name = world["tmp"] / "busy-link.wav"
    name.symlink_to(sound)
    record(world, delay=0, file=name)
    log, left, _ = render(engine, world, [relink(0, name, other), arm(0), stop(0.5), arm(1.0),
                                          {"t": 1.25, "ping": 1}], until=1.5)
    assert [x["t"] for x in evs(log, "played")] == [0, 1.0] and evs(log, "refused") == []
    level(left, 0, RATE // 2 - FADE, 0.25)
    level(left, RATE, round(1.5 * RATE), 0.5)
    assert evs(log, "status")[0]["decoded"] == 1                   # replaced, not kept beside


def test_offline_a_linked_sound_played_twice_is_decoded_once(engine, world, sound):
    name = world["tmp"] / "busy-link.wav"
    name.symlink_to(sound)
    record(world, delay=0, file=name)
    log, left, _ = render(engine, world, [arm(0), stop(0.5), arm(1.0), {"t": 1.25, "ping": 1}],
                          until=1.5)
    assert [(x["t"], x["file"]) for x in evs(log, "played")] == [(0, str(name)), (1.0, str(name))]
    (st,) = evs(log, "status")
    assert st["decoded"] == 1 and st["decoded_bytes"] == 2 * RATE * 4
    level(left, RATE, round(1.5 * RATE), 0.25)


def test_offline_two_names_for_one_file_each_keep_their_own_name(engine, world, sound):
    """A link and the file it names: each seat's lines carry its own name, each
    name holds its own decode, and eviction frees both by name."""
    name = world["tmp"] / "busy-link.wav"
    name.symlink_to(sound)
    record(world, SID, delay=0, file=name)
    record(world, SID2, delay=0, file=sound)
    log, left, _ = render(engine, world, [arm(0, SID), arm(0, SID2), {"t": 0.25, "ping": 1},
                                          stop(0.5, sid=SID), stop(0.5, sid=SID2),
                                          {"t": 3.0, "ping": 1}],
                          until=3.5, extra=["--evict-after", "2"])
    assert sorted((x["sid"], x["file"]) for x in evs(log, "played")) == \
        sorted([(SID, str(name)), (SID2, str(sound))])
    level(left, 0, RATE // 4, 0.5)                                  # both voices, mixed
    first, later = evs(log, "status")
    assert first["decoded"] == 2 and later["decoded"] == 0
    assert sorted(x["file"] for x in evs(log, "evicted")) == sorted([str(name), str(sound)])


def test_offline_a_checked_sound_swapped_for_a_link_is_unreadable_not_followed(engine, world, sound):
    """No link on the way: the checked path is the file itself. Swapped for a
    link into Documents before the decode, it is refused as unreadable; the
    decode never follows it to the louder sound."""
    docs = fake_home() / "Documents"
    docs.mkdir(parents=True)
    loud = wav(docs / "loud.wav", 0.5, 2.0)
    record(world, delay=0, file=sound)
    log, left, _ = render(engine, world, [relink(0, sound, loud), arm(0)], until=0.5)
    assert len(evs(log, "relinked")) == 1 and evs(log, "played") == []
    (r,) = evs(log, "refused")
    assert (r["reason"], r["file"]) == ("unreadable", str(sound))
    level(left, 0, len(left), 0.0)


def test_a_checked_sound_swapped_for_a_link_is_unreadable_to_the_null_output(engine, world):
    docs_sound = fake_home() / "Documents" / "x.wav"                # fake HOME: a readable file,
    docs_sound.parent.mkdir(parents=True)                           # so only a followed link plays
    docs_sound.write_bytes(b"RIFF")
    record(world, delay=0)
    log = script(engine, world, [relink(0, world["sound"], docs_sound), arm(0)], until=0.5)
    (r,) = evs(log, "refused")
    assert (r["reason"], r["file"]) == ("unreadable", str(world["sound"]))
    assert evs(log, "played") == []


# Records.load: open(O_NOFOLLOW | O_NONBLOCK), fstat and read from that fd (do-008 R1).

def test_a_record_swapped_for_a_link_after_find_checked_it_is_never_read(engine, world):
    """find lstat()s a plain record; before load reads it, it becomes a link to a
    VALID record in Documents. Had load followed it, the seat would have armed."""
    rec = record(world, delay=0)
    docs_rec = seat_record(fake_home() / "Documents" / "rec", file=world["sound"],
                           mouth=world["mouth"])
    log = script(engine, world, [relink(0, rec, docs_rec), arm(0)], until=0.5)
    assert len(evs(log, "relinked")) == 1 and os.readlink(rec) == str(docs_rec)
    assert evs(log, "armed") == [] and evs(log, "played") == []
    assert [x["sid"] for x in evs(log, "no-record")] == [SID]


def test_a_record_that_is_a_fifo_never_hangs_the_engine(engine, world):
    slot = world["root"] / SID / "sounds" / "busy"
    slot.mkdir(parents=True)
    os.mkfifo(slot / f"engine.{os.getpid()}")                       # no writer: a blocking open hangs
    log = script(engine, world, [arm(0)], until=0.5)
    assert [x["sid"] for x in evs(log, "no-record")] == [SID] and evs(log, "armed") == []


def test_a_record_larger_than_the_cap_is_not_read(engine, world):
    rec = record(world, delay=0)
    body = json.loads(rec.read_text())
    body["pad"] = "x" * 70_000
    rec.write_text(json.dumps(body))
    log = script(engine, world, [arm(0)], until=0.5)
    assert evs(log, "armed") == [] and [x["sid"] for x in evs(log, "no-record")] == [SID]


def test_a_relink_step_without_a_target_is_a_script_error(engine, world):
    path = world["tmp"] / "steps.json"
    path.write_text(json.dumps({"steps": [{"t": 0, "relink": str(world["sound"])}]}))
    r = subprocess.run([str(engine), "--script", str(path), "--sessions", str(world["root"])],
                       capture_output=True, text=True, check=False, timeout=60)
    assert r.returncode == 2 and "relink step needs a to" in r.stderr


# --- do-011: the sound file is opened once, and decoded from that descriptor ------------
#
# decode() opens the checked path with O_NOFOLLOW_ANY (no link anywhere in it, the last
# component included) and decodes /dev/fd/N, a dup of that descriptor: it never looks
# the name up again (do-009 review N1). A relink's `at` lands it at "open" (just before
# that open) or "decode" (after it, before the read); a directory may be relinked too.
# Every protected target is under conftest's fake HOME.

def relink_at(t, path: Path, to: Path, at: str) -> dict:
    return {**relink(t, path, to), "at": at}


def loud_in_documents(name: str = "loud.wav") -> Path:
    docs = fake_home() / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    return wav(docs / name, 0.5, 2.0)


@pytest.mark.parametrize("fmt", ["wav", "aiff", "caf", "m4a"])
def test_offline_the_decode_reads_its_descriptor_in_each_format(engine, world, fmt):
    """AVAudioFile decodes /dev/fd/N on this macOS. The name has no extension
    for it to go by, so the format is told from the bytes. The lossless ones
    hold their level exactly; AAC is lossy, so it is held loosely."""
    src = wav(world["tmp"] / "src.wav", 0.25, 2.0)
    if fmt == "wav":
        sound = src
    else:
        afconvert = shutil.which("afconvert")
        if afconvert is None:
            pytest.skip("no afconvert")
        kind = {"aiff": ["-f", "AIFF", "-d", "BEI16"], "caf": ["-f", "caff", "-d", "LEI16"],
                "m4a": ["-f", "m4af", "-d", "aac"]}[fmt]
        sound = world["tmp"] / f"busy.{fmt}"
        subprocess.run([afconvert, *kind, str(src), str(sound)], check=True, timeout=60,
                       capture_output=True)
    record(world, delay=0, file=sound)
    log, left, _ = render(engine, world, [arm(0), {"t": 0.25, "ping": 1}], until=0.5)
    assert evs(log, "refused") == [] and [x["file"] for x in evs(log, "played")] == [str(sound)]
    assert evs(log, "status")[0]["decoded"] == 1
    if fmt == "m4a":
        mid = left[RATE // 8: RATE // 4]
        assert abs(sum(mid) / len(mid) - 0.25) < 0.02
    else:
        level(left, 0, RATE // 2, 0.25)


def test_offline_a_sound_swapped_after_the_open_plays_what_was_opened(engine, world, sound):
    """THE window do-009 left: the name is swapped for a link into Documents
    after the open and before the read. The decode reads its descriptor, so
    the checked sound (0.25) plays and the swapped target (0.5) is never read."""
    loud = loud_in_documents()
    record(world, delay=0, file=sound)
    log, left, _ = render(engine, world, [relink_at(0, sound, loud, "decode"), arm(0)], until=0.5)
    (moved,) = evs(log, "relinked")
    assert (moved["path"], moved["at"]) == (str(sound), "decode")
    assert os.readlink(sound) == str(loud)
    assert evs(log, "refused") == [] and [x["file"] for x in evs(log, "played")] == [str(sound)]
    level(left, 0, RATE // 2, 0.25)


def test_offline_a_sound_swapped_just_before_the_open_is_unreadable(engine, world, sound):
    loud = loud_in_documents()
    record(world, delay=0, file=sound)
    log, left, _ = render(engine, world, [relink_at(0, sound, loud, "open"), arm(0)], until=0.5)
    assert [x["at"] for x in evs(log, "relinked")] == ["open"] and evs(log, "played") == []
    (r,) = evs(log, "refused")
    assert (r["reason"], r["file"]) == ("unreadable", str(sound))
    level(left, 0, len(left), 0.0)


@pytest.mark.parametrize("at", ["check", "open"])
def test_offline_a_directory_swapped_for_a_link_is_never_crossed(engine, world, at):
    """The intermediate-directory swap: the sound's DIRECTORY becomes a link
    into Documents, where a louder sound of the same name waits. The open
    refuses a link anywhere on the path (ELOOP), so it is unreadable, and the
    louder sound is never read."""
    d = world["tmp"] / "sounds"
    d.mkdir()
    sound = wav(d / "busy.wav", 0.25, 2.0)
    docs = fake_home() / "Documents" / "sounds"
    docs.mkdir(parents=True)
    wav(docs / "busy.wav", 0.5, 2.0)
    record(world, delay=0, file=sound)
    log, left, _ = render(engine, world, [relink_at(0, d, docs, at), arm(0)], until=0.5)
    (moved,) = evs(log, "relinked")
    assert moved["path"] == str(d) and os.readlink(d) == str(docs)
    assert evs(log, "played") == []
    (r,) = evs(log, "refused")
    assert (r["reason"], r["file"]) == ("unreadable", str(sound))
    level(left, 0, len(left), 0.0)


def test_a_directory_swapped_for_a_link_is_unreadable_to_the_null_output(engine, world):
    d = world["tmp"] / "sounds"
    d.mkdir()
    sound = d / "busy.wav"
    sound.write_bytes(world["sound"].read_bytes())
    docs = fake_home() / "Documents" / "sounds"
    docs.mkdir(parents=True)
    (docs / "busy.wav").write_bytes(b"RIFF")                         # readable: only a crossing plays
    record(world, delay=0, file=sound)
    log = script(engine, world, [relink_at(0, d, docs, "open"), arm(0)], until=0.5)
    assert len(evs(log, "relinked")) == 1 and evs(log, "played") == []
    (r,) = evs(log, "refused")
    assert (r["reason"], r["file"]) == ("unreadable", str(sound))


def test_a_record_dir_swapped_for_a_link_after_the_listing_is_never_read(engine, world):
    """find lists sounds/busy, then the DIRECTORY becomes a link into Documents
    holding a VALID record of the same name. Had the record's open crossed the
    link, the seat would have armed."""
    rec = record(world, delay=0)
    docs_slot = fake_home() / "Documents" / "busy"
    seat_record(docs_slot, file=world["sound"], mouth=world["mouth"])
    log = script(engine, world, [relink(0, rec.parent, docs_slot), arm(0)], until=0.5)
    assert len(evs(log, "relinked")) == 1 and os.readlink(rec.parent) == str(docs_slot)
    assert evs(log, "armed") == [] and evs(log, "played") == []
    assert [x["sid"] for x in evs(log, "no-record")] == [SID]


def test_a_relink_step_at_an_unknown_moment_is_a_script_error(engine, world):
    path = world["tmp"] / "steps.json"
    path.write_text(json.dumps({"steps": [{"t": 0, "relink": str(world["sound"]),
                                           "to": str(world["sound"]), "at": "later"}]}))
    r = subprocess.run([str(engine), "--script", str(path), "--sessions", str(world["root"])],
                       capture_output=True, text=True, check=False, timeout=60)
    assert r.returncode == 2 and "check, open or decode" in r.stderr


# --- do-012: a player.pid that is not a regular file never blocks the tick ---------------
#
# pidFileLive opens player.pid with O_NONBLOCK (and O_NOFOLLOW_ANY), then fstat()s it: a
# FIFO there used to block the open with no writer, and the tick is shared by every seat.
# Not a regular file (a FIFO, a directory): not live, so it does not hold the seat. The
# mouth dir is an ALLOWED temp dir here; only a refused mouth was safe before.

def not_a_pid_file(path: Path, kind: str) -> None:
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_a_player_pid_that_is_not_a_file_never_blocks_and_is_not_live(engine, world, kind):
    (world["mouth"] / "playing.json").write_text("{}")
    not_a_pid_file(world["mouth"] / "player.pid", kind)
    record(world, delay=0)
    path = world["tmp"] / "steps.json"
    path.write_text(json.dumps({"steps": [arm(0)], "until": 0.5}))
    r = subprocess.run([str(engine), "--script", str(path), "--sessions", str(world["root"])],
                       capture_output=True, text=True, check=False, timeout=10)
    assert r.returncode == 0, r.stderr
    log = lines(r.stderr)
    assert evs(log, "refused") == [] and [x["sid"] for x in evs(log, "played")] == [SID]


def test_live_a_fifo_player_pid_keeps_the_engine_responsive(live, world):
    """The tick reads the mouth every few ms: with the FIFO there the engine
    still plays (not live) and still answers a ping, on every tick after."""
    (world["mouth"] / "playing.json").write_text("{}")
    os.mkfifo(world["mouth"] / "player.pid")
    record(world, delay=0.2)
    lv = live()
    lv.send(event("arm"))
    assert lv.wait_for("armed") and lv.wait_for("played", timeout=5)
    time.sleep(0.3)                                          # more ticks over the FIFO
    s = lv.ping()
    assert s["refused"] == 0 and lv.proc.poll() is None
