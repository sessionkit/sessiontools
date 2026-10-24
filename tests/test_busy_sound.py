"""The busy sound's sound and trigger (ST-66 do-003): the `play`/`stop` seam
ST-90 uses, the waiter, the claude pid, the batch trigger, and the hook shim
`hooks/busy-sound` exactly as `hooks/hooks.json` runs it.

The player is never `afplay` here: a fake records its pid in a mark file and
sleeps, so a test can see that a sound started and that it was killed. Every
test builds a private HOME and SESSIONS_DIR; the teardown stops every slot it
made and kills every fake player it saw.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from settings_support import host

from sessiontools import busy

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "busy-sound"
HOOKS = ROOT / "hooks" / "hooks.json"
SID = "0f0e0d0c-0b0a-4908-8706-050403020100"


# --- helpers ---------------------------------------------------------------------

def until(check, timeout: float = 5.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(step)
    return bool(check())


def gone(pid: int) -> bool:
    """PID is not running: absent, or a zombie waiting to be reaped (a waiter
    `play` spawned from this test process is this process's child)."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                         capture_output=True, text=True, check=False).stdout.strip()
    return out == "" or out.startswith("Z")


def pids(mark: Path) -> list[int]:
    try:
        return [int(x) for x in mark.read_text().split()]
    except FileNotFoundError:
        return []


class Rig:
    """A private tree: a session dir, a sound file, a fake player."""

    def __init__(self, root: Path):
        self.root = root
        self.session = root / ".session" / "sessions" / SID
        self.session.mkdir(parents=True)
        self.wav = root / "dance.wav"
        self.wav.write_text("RIFF")
        self.mark = root / "played"
        # Every mark a fake player writes to: teardown kills each pid in each.
        self.marks: list[Path] = [self.mark]
        self.bin = root / "bin"
        self.bin.mkdir()
        # The fake player: records its pid, then plays for 30 s. Called as
        # `player MARK FILE` by the seam tests, and as `afplay FILE` (on PATH)
        # by the shim tests, so it writes to a fixed mark either way.
        self.player = self.bin / "afplay"
        self.player.write_text(f'#!/bin/sh\necho $$ >> "{self.mark}"\nexec sleep 30\n')
        self.player.chmod(0o755)
        self.short = self.bin / "short-player"
        self.short.write_text(f'#!/bin/sh\necho $$ >> "{self.mark}"\nexit 0\n')
        self.short.chmod(0o755)
        self.pymark = root / "python-started"
        py = self.bin / "python3"
        py.write_text(f'#!/bin/sh\necho x >> "{self.pymark}"\nexec "{sys.executable}" "$@"\n')
        py.chmod(0o755)
        self.slots: list[Path] = []
        # A private mouth dir (the speech guard reads it); made on demand.
        self.mouth = root / "mouth"
        self.held: list[subprocess.Popen] = []

    def speak(self, pid: int | None = None) -> None:
        """The mouth is speaking: `playing.json` beside a `player.pid` that
        names PID (default: this test process, which is alive)."""
        self.mouth.mkdir(exist_ok=True)
        (self.mouth / "player.pid").write_text(str(pid or os.getpid()))
        (self.mouth / "playing.json").write_text('{"utt": "x", "session": "s"}')

    def hush(self) -> None:
        (self.mouth / "playing.json").unlink(missing_ok=True)

    def dead_pid(self) -> int:
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def slot(self, name: str = "busy") -> Path:
        path = self.session / "sounds" / name
        self.slots.append(path)
        return path

    @property
    def busy(self) -> Path:
        return self.slot("busy")

    def settings(self, **busy_keys) -> Path:
        block = {"auto_arm": True, "file": str(self.wav), "delay": 8}
        block.update(busy_keys)
        path = self.session / "settings" / "sounds.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabled": True, "busy": block}))
        return path

    def env(self, **extra) -> dict:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root),
               "SESSIONS_DIR": str(self.root / ".session" / "sessions"),
               "CLAUDE_CODE_SESSION_ID": SID, "CLAUDE_PID": str(os.getpid())}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def shim(self, verb: str, payload: dict | None = None, **env) -> subprocess.CompletedProcess:
        body = {"session_id": SID, "hook_event_name": "x", **(payload or {})}
        return subprocess.run(["/bin/sh", str(SHIM), verb], input=json.dumps(body),
                              env=self.env(**env), capture_output=True, text=True,
                              timeout=30, check=False)

    def python_runs(self) -> int:
        try:
            return len(self.pymark.read_text().split())
        except FileNotFoundError:
            return 0

    def teardown(self) -> None:
        for slot in self.slots:
            busy.stop(slot)
        for proc in self.held:
            proc.kill()
            proc.wait()
        for pid in (pid for mark in self.marks for pid in pids(mark)):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / ".session" / "sessions"))
    # The waiter inherits this env: its mouth is the rig's, never the host's.
    monkeypatch.setenv("VOICEMODE_MOUTH_DIR", str(tmp_path / "mouth"))
    # An ambient MOUTH_DIR or SESSIONVOICE_HOME would win over the rig's dir.
    for name in ("MOUTH_DIR", "SESSIONVOICE_HOME", "VOICEMODE_BASE_DIR"):
        monkeypatch.delenv(name, raising=False)
    r = Rig(tmp_path)
    yield r
    r.teardown()


def silent(result: subprocess.CompletedProcess) -> None:
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


# --- the seam, on its own, as ST-90 will call it ------------------------------------

def test_play_at_zero_starts_at_once_with_no_payload_settings_or_hook(rig):
    slot = rig.slot("unread")
    token = busy.play(slot, rig.wav, 0, player=(str(rig.short),))
    assert token and (slot / "token").read_text() == token
    assert until(lambda: pids(rig.mark)), "the player never started"
    assert busy.stop(slot) is True
    assert not (slot / "token").exists() and not (slot / "pgid").exists()


def test_stop_kills_the_player_mid_sound(rig):
    slot = rig.slot("unread")
    busy.play(slot, rig.wav, 0, player=(str(rig.player),))
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    waiter = int((slot / "pgid").read_text().split()[0])
    assert not gone(player)
    assert busy.stop(slot) is True
    assert until(lambda: gone(player)), "the player outlived stop"
    assert until(lambda: gone(waiter)), "the waiter outlived stop"


def test_a_second_play_on_an_armed_slot_returns_none(rig):
    slot = rig.slot("unread")
    assert busy.play(slot, rig.wav, 30) is not None
    assert busy.play(slot, rig.wav, 0) is None


def test_two_slots_play_and_stop_independently(rig):
    a, b = rig.slot("a"), rig.slot("b")
    busy.play(a, rig.wav, 0, player=(str(rig.player),))
    assert until(lambda: len(pids(rig.mark)) == 1)
    busy.play(b, rig.wav, 0, player=(str(rig.player),))
    assert until(lambda: len(pids(rig.mark)) == 2)
    first, second = pids(rig.mark)
    busy.stop(a)
    assert until(lambda: gone(first))
    time.sleep(0.2)
    assert not gone(second), "stopping one slot stopped the other"
    busy.stop(b)
    assert until(lambda: gone(second))


def test_stop_on_an_empty_or_missing_slot_is_false_and_quiet(rig, tmp_path):
    empty = rig.slot("empty")
    empty.mkdir(parents=True)
    assert busy.stop(empty) is False
    assert busy.stop(tmp_path / "no" / "such" / "slot") is False


def test_a_sound_that_played_out_leaves_the_slot_armed_until_stop(rig):
    slot = rig.slot("unread")
    busy.play(slot, rig.wav, 0, player=(str(rig.short),))
    assert until(lambda: pids(rig.mark))
    waiter = int((slot / "pgid").read_text().split()[0])
    assert until(lambda: gone(waiter))
    assert (slot / "token").exists(), "a played-out sound freed its slot: a batch would replay it"
    assert busy.play(slot, rig.wav, 0, player=(str(rig.short),)) is None
    assert busy.stop(slot) is True
    assert len(pids(rig.mark)) == 1


def test_parallel_plays_arm_the_slot_once(rig):
    slot = rig.slot("busy")
    got: list[str | None] = []
    start = threading.Barrier(16)

    def one():
        start.wait()
        got.append(busy.play(slot, rig.wav, 30))

    threads = [threading.Thread(target=one) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([t for t in got if t]) == 1
    assert busy.stop(slot) is True


def test_the_race_a_stop_before_the_delay_means_no_player_ever_starts(rig):
    slot = rig.slot("busy")
    busy.play(slot, rig.wav, 0.5, player=(str(rig.player),))
    busy.stop(slot)
    time.sleep(1.2)
    assert pids(rig.mark) == []


def test_a_bad_delay_arms_nothing(rig):
    for delay in (-1, float("nan"), float("inf")):
        assert busy.play(rig.slot("x"), rig.wav, delay) is None
    assert not (rig.session / "sounds" / "x" / "token").exists()


def test_play_never_makes_the_session_dir(tmp_path):
    slot = tmp_path / "no-session" / "sounds" / "busy"
    assert busy.play(slot, tmp_path / "x.wav", 0) is None
    assert not (tmp_path / "no-session").exists()


# --- the waiter ------------------------------------------------------------------

def test_the_stale_token_a_waiter_for_batch_a_stays_silent_in_batch_b(rig):
    slot = rig.slot("busy")
    slot.mkdir(parents=True)
    (slot / "token").write_text("bbbbbbbbbbbbbbbb")
    assert busy.wait(slot, "aaaaaaaaaaaaaaaa", 0, rig.wav, None, (str(rig.short),)) == 0
    assert pids(rig.mark) == []


def test_a_waiter_whose_token_is_gone_stays_silent(rig):
    slot = rig.slot("busy")
    slot.mkdir(parents=True)
    assert busy.wait(slot, "aaaaaaaaaaaaaaaa", 0, rig.wav, None, (str(rig.short),)) == 0
    assert pids(rig.mark) == []


def test_a_waiter_for_a_seat_that_has_gone_stays_silent(rig):
    slot = rig.slot("busy")
    slot.mkdir(parents=True)
    (slot / "token").write_text("aaaaaaaaaaaaaaaa")
    dead = subprocess.Popen(["true"])
    dead.wait()
    assert busy.wait(slot, "aaaaaaaaaaaaaaaa", 0, rig.wav, dead.pid, (str(rig.short),)) == 0
    assert pids(rig.mark) == []


def test_a_waiter_with_its_token_and_a_live_seat_plays(rig):
    slot = rig.slot("busy")
    slot.mkdir(parents=True)
    (slot / "token").write_text("aaaaaaaaaaaaaaaa")
    busy.wait(slot, "aaaaaaaaaaaaaaaa", 0, rig.wav, os.getpid(), (str(rig.short),))
    assert len(pids(rig.mark)) == 1


def test_wait_main_reads_the_argv_play_writes_and_an_empty_pid_skips_the_check(rig):
    slot = rig.slot("busy")
    slot.mkdir(parents=True)
    (slot / "token").write_text("aaaaaaaaaaaaaaaa")
    assert busy.wait_main([str(slot), "aaaaaaaaaaaaaaaa", "0", str(rig.wav), "",
                           str(rig.short)]) == 0
    assert len(pids(rig.mark)) == 1
    assert busy.wait_main(["too", "few"]) == 0


# --- the speech guard (ST-90 do-003, design decision 3) -------------------------------

def first_seen(mark: Path, timeout: float = 5.0) -> float | None:
    """time.monotonic() when MARK first held a player pid, polled at 10 ms."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pids(mark):
            return time.monotonic()
        time.sleep(0.01)
    return None


def first_seen_each(*marks: Path, timeout: float = 5.0) -> dict[Path, float | None]:
    """For each MARK, time.monotonic() when it first held a player pid, all
    polled in one 10 ms loop so no mark waits on another; None if not
    inside TIMEOUT."""
    seen: dict[Path, float | None] = dict.fromkeys(marks)
    end = time.monotonic() + timeout
    while None in seen.values() and time.monotonic() < end:
        now = time.monotonic()
        for mark in marks:
            if seen[mark] is None and pids(mark):
                seen[mark] = now
        time.sleep(0.01)
    return seen


def test_a_mouth_speaking_at_the_deadline_holds_the_start_until_silent_for_the_delay(rig):
    rig.speak()
    slot = rig.slot("busy")
    busy.play(slot, rig.wav, 0.3, player=(str(rig.player),))
    time.sleep(0.9)                                 # three delays, all spoken over
    assert pids(rig.mark) == [], "the sound started over the mouth"
    hushed = time.monotonic()
    rig.hush()
    started = first_seen(rig.mark)
    assert started is not None, "the sound never started after the mouth went silent"
    # The countdown restarts at the last poll that saw speech (<= 0.1 s
    # before the hush), so the start is at least delay - step after it.
    assert 0.2 <= started - hushed <= 1.0, started - hushed


#: The control for test_delay_zero_waits_for_silence: the waiter's own launch
#: (this interpreter, -I, this src/, `import sessiontools.busy`), then it
#: writes its READY file (argv[2]), then sleeps in 100 ms sleeps until the
#: mouth's playing file (argv[3]) goes, then a player at once. Its own few
#: lines, never `busy.wait`. Gives up after 30 s. The 100 ms is the waiter's
#: contract ("wakes every STEP (<= 100 ms)"), written as a literal and never
#: read from `busy.STEP`: a control that tracked STEP would slow down with
#: it, and a slower poll would pass (ST-124 review).
WATCH_HUSH = ("import os, subprocess, sys, time; sys.path.insert(0, sys.argv[1]); "
              "import sessiontools.busy as busy\n"
              "open(sys.argv[2], 'w').close()\n"
              "end = time.monotonic() + 30\n"
              "while os.path.exists(sys.argv[3]):\n"
              "    if time.monotonic() > end: sys.exit(1)\n"
              "    time.sleep(0.1)\n"
              "subprocess.Popen(sys.argv[4:], stdin=subprocess.DEVNULL, "
              "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
#: When test_delay_zero_waits_for_silence hushes the mouth: once per trial,
#: this long after that trial's control is READY. The waiter and the control
#: are launched together and import the same module, so a waiter that polls
#: every STEP looks at the mouth about 0.03 s + k * STEP after READY, and
#: starts at its first look past the hush. The control looks every 0.1 s, so
#: about 0.05 s past either hush. A trial fails a poll whose next look falls
#: more than the 0.5 s allowed past the control's. One hush leaves a blind
#: band: at 1.15 s (ST-128) a 1.2 s poll looks at 1.23 s and a 1.5 s poll at
#: 1.53 s, and both passed. The second, at 1.65 s, catches them: they next
#: look at 2.43 s and 3.03 s. The modelled lag past the control, in seconds,
#: matched by the measured one (ST-129 findings/write-tests-001):
#:
#:     STEP   0.1   0.5   0.7   0.9   1.0   1.1   1.2   1.5   2.0
#:     1.15  0.03  0.33  0.23  0.63  0.83  1.03  0.03  0.33  0.83
#:     1.65  0.03  0.33  0.43  0.13  0.33  0.53  0.73  1.33  0.33
#:
#: So one hush or the other catches every poll from 0.9 s to 2.0 s; 0.5 s
#: and 0.7 s pass, the 0.5 s slack's accepted limit. Literals, like the
#: control's 100 ms, never derived from STEP.
HUSH_AFTER_READY = (1.15, 1.65)


def delay_zero_trial(rig, name: str, hush_after: float) -> tuple[float, float]:
    """One trial of test_delay_zero_waits_for_silence, in slot NAME with its
    own player and control: the mouth speaks, a delay-0 waiter and its
    control start, and the mouth is hushed HUSH_AFTER seconds after the
    control is READY. Returns (lag, load): how long after the hush the
    guarded sound started, and how long the control took."""
    mark = rig.root / f"played-{name}"
    player = rig.bin / f"player-{name}"
    player.write_text(f'#!/bin/sh\necho $$ >> "{mark}"\nexec sleep 30\n')
    player.chmod(0o755)
    control_mark = rig.root / f"control-played-{name}"
    ready = rig.root / f"control-ready-{name}"
    control = rig.bin / f"control-player-{name}"
    control.write_text(f'#!/bin/sh\necho $$ >> "{control_mark}"\nexec sleep 30\n')
    control.chmod(0o755)
    rig.marks += [mark, control_mark]
    rig.speak()
    slot = rig.slot(name)
    busy.play(slot, rig.wav, 0, player=(str(player),))
    watcher = subprocess.Popen([sys.executable, "-I", "-c", WATCH_HUSH, str(busy.SRC), str(ready),
                                str(rig.mouth / busy.MOUTH_PLAYING), str(control), str(rig.wav)],
                               start_new_session=True, close_fds=True, cwd="/",
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    rig.held.append(watcher)
    assert until(ready.exists, timeout=15.0, step=0.005), "the control never started"
    readied = time.monotonic()
    time.sleep(0.5)
    assert pids(mark) == [], "delay 0 played over the mouth"
    assert pids(control_mark) == [], "the control did not wait for the hush"
    time.sleep(max(0.0, readied + hush_after - time.monotonic()))
    assert pids(mark) == [], "delay 0 played over the mouth before the hush"
    hushed = time.monotonic()
    rig.hush()
    seen = first_seen_each(mark, control_mark, timeout=15.0)
    started, baseline = seen[mark], seen[control_mark]
    assert started is not None, "delay 0 never started after the mouth went silent"
    assert baseline is not None, "the control never started: the host is wedged"
    return started - hushed, baseline - hushed


def test_delay_zero_waits_for_silence(rig):
    # How fast "at once" is depends on the host's load, not on the waiter
    # (ST-124: 0.977 s against a fixed 0.5 s at a load of about 10). So a
    # CONTROL runs beside the waiter, under the same load: launched with it,
    # asleep in 100 ms sleeps (the waiter's contract), watching for the same
    # hush, then starting the same kind of player (WATCH_HUSH). A late wake
    # from a sleep slows both; a regression in the waiter, a slower poll
    # included, slows only the waiter.
    # The guarded sound may lag the control by at most 0.5 s, so a waiter
    # that sits on after the silence still fails, quiet host or busy one.
    # The hush is timed from the control's READY file, once per trial at
    # two offsets (HUSH_AFTER_READY), so a slow poll's sleep holds one hush
    # early, not by chance late: a 1.0 s poll lags the control by about
    # 0.8 s at the first, a 1.2 s poll by about 0.7 s at the second.
    # An offset fails only when two trials in a row miss it. A slow poll's
    # lag is set by its phase and comes back each time (within 0.06 s on a
    # quiet host, ST-129); a lag from load is chance, and once was 0.5 s at
    # a load of about 10 with a 0.1 s poll (ST-130).
    for n, hush_after in enumerate(HUSH_AFTER_READY):
        misses: list[str] = []
        while len(misses) < 2:
            lag, load = delay_zero_trial(rig, f"unread-{n}-{len(misses)}", hush_after)
            if lag <= load + 0.5:
                break
            misses.append(f"started {lag:.3f} s after the hush; the control took {load:.3f} s")
        assert len(misses) < 2, f"hushed {hush_after} s after READY, twice: " + "; ".join(misses)


def test_the_mouth_starting_mid_sound_kills_the_player_with_no_replay(rig):
    slot = rig.slot("busy")
    busy.play(slot, rig.wav, 0, player=(str(rig.player),))
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    waiter = int((slot / "pgid").read_text().split()[0])
    spoke = time.monotonic()
    rig.speak()
    assert until(lambda: gone(player), timeout=2.0, step=0.005)
    assert time.monotonic() - spoke <= 0.2, "the cut took longer than 0.2 s"
    assert until(lambda: gone(waiter)), "the waiter outlived the cut"
    rig.hush()
    time.sleep(0.5)
    assert len(pids(rig.mark)) == 1, "the sound replayed after the mouth went silent"
    assert (slot / "token").exists(), "the cut freed the slot: a batch would replay it"
    assert busy.play(slot, rig.wav, 0, player=(str(rig.player),)) is None


def test_a_missing_mouth_dir_means_the_guard_does_nothing(rig, tmp_path):
    assert not rig.mouth.exists()
    assert busy.mouth_speaking(rig.mouth) is False
    assert busy.mouth_speaking(tmp_path / "no" / "such" / "mouth") is False
    slot = rig.slot("busy")
    busy.play(slot, rig.wav, 0, player=(str(rig.player),))
    assert until(lambda: pids(rig.mark)), "no mouth dir stopped the sound"


def test_an_unreadable_playing_file_still_counts(rig):
    rig.speak()
    (rig.mouth / "playing.json").chmod(0o000)
    try:
        assert busy.mouth_speaking(rig.mouth) is True
    finally:
        (rig.mouth / "playing.json").chmod(0o600)


def test_the_file_is_only_stated_never_parsed(rig):
    rig.speak()
    (rig.mouth / "playing.json").write_text("not json at all {")
    assert busy.mouth_speaking(rig.mouth) is True
    (rig.mouth / "playing.json").write_text("")
    assert busy.mouth_speaking(rig.mouth) is True


@pytest.mark.parametrize("pid_text", [None, "", "abc", "1", "0", "-4"])
def test_playing_beside_a_missing_or_bad_player_pid_does_not_count(rig, pid_text):
    rig.speak()
    if pid_text is None:
        (rig.mouth / "player.pid").unlink()
    else:
        (rig.mouth / "player.pid").write_text(pid_text)
    assert busy.mouth_speaking(rig.mouth) is False


def test_playing_beside_a_dead_player_pid_does_not_count_and_the_sound_plays(rig):
    """The killed-player gap: a SIGKILLed mouth leaves `playing.json` behind,
    with a `player.pid` naming a dead process. It must not silence every mode."""
    rig.speak(pid=rig.dead_pid())
    assert busy.mouth_speaking(rig.mouth) is False
    slot = rig.slot("busy")
    busy.play(slot, rig.wav, 0, player=(str(rig.player),))
    assert until(lambda: pids(rig.mark)), "a stale playing.json silenced the sound"


def test_an_unreadable_player_pid_fails_toward_silence(rig):
    rig.speak()
    (rig.mouth / "player.pid").chmod(0o000)
    try:
        assert busy.mouth_speaking(rig.mouth) is True
    finally:
        (rig.mouth / "player.pid").chmod(0o600)


def test_a_live_player_pid_alone_is_not_speaking(rig):
    rig.speak()
    rig.hush()
    assert (rig.mouth / "player.pid").exists()
    assert busy.mouth_speaking(rig.mouth) is False


# --- ST-135: a player.pid that is not a regular file never blocks the hook --------
#
# The engine's twin is ST-132 do-012 (`pidFileLive`). `_pid_live` opens player.pid
# O_NONBLOCK and fstat()s the descriptor: a FIFO with no writer used to block
# `read_text()`'s open forever (the seat's hook, and its turn, hung), and a
# directory raised IsADirectoryError, read as "unreadable", which held the seat.
# A link is followed, as in the engine; only the kind it reaches decides.

def not_a_pid_file(rig: Rig, kind: str) -> Path:
    """Make <mouth>/player.pid a KIND that is not a regular file, beside a
    playing.json. Returns the FIFO a blocked reader would wait on, if any."""
    rig.speak()
    pid = rig.mouth / "player.pid"
    pid.unlink()
    target = rig.root / "elsewhere" / "player.pid" if kind.startswith("link") else pid
    target.parent.mkdir(exist_ok=True)
    if kind.endswith("fifo"):
        os.mkfifo(target)
    else:
        target.mkdir()
    if target != pid:
        pid.symlink_to(target)
    return target


def bounded(call, fifo: Path, limit: float = 1.0):
    """CALL's result and how long it took, run on a daemon thread. If it is
    still running after LIMIT seconds it is blocked on FIFO's open: open a
    writer to release it (so no thread is left hanging) and fail."""
    out: dict = {}

    def run():
        t = time.monotonic()
        out["value"] = call()
        out["took"] = time.monotonic() - t

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(limit)
    if th.is_alive():
        if fifo.is_fifo():
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        th.join(1.0)
        pytest.fail(f"blocked for over {limit}s on {fifo}")
    return out["value"], out["took"]


@pytest.mark.parametrize("kind", ["fifo", "dir", "link-fifo", "link-dir"])
def test_a_player_pid_that_is_not_a_file_never_blocks_and_is_not_live(rig, kind):
    target = not_a_pid_file(rig, kind)
    value, took = bounded(lambda: busy._pid_live(rig.mouth / "player.pid"), target)
    assert value is False and took < 1.0
    value, took = bounded(lambda: busy.mouth_speaking(rig.mouth), target)
    assert value is False and took < 1.0


def test_a_mouth_reached_through_a_link_to_a_live_pid_still_speaks(rig):
    """The green guard (engine parity, test_a_mouth_reached_through_links_still_
    speaks): a link to a regular pid file is followed, not refused."""
    rig.speak()
    real = rig.root / "elsewhere" / "player.pid"
    real.parent.mkdir()
    real.write_text(f"{os.getpid()}\n")
    (rig.mouth / "player.pid").unlink()
    (rig.mouth / "player.pid").symlink_to(real)
    assert busy._pid_live(rig.mouth / "player.pid") is True
    assert busy.mouth_speaking(rig.mouth) is True
    real.write_text(str(rig.dead_pid()))
    assert busy.mouth_speaking(rig.mouth) is False


def test_any_sessions_speech_counts(rig):
    rig.speak()
    (rig.mouth / "playing.json").write_text(json.dumps({"session": "someone-else"}))
    assert busy.mouth_speaking(rig.mouth) is True


def test_a_waiter_held_off_by_speech_still_ends_when_its_slot_is_stopped(rig):
    rig.speak()
    slot = rig.slot("busy")
    slot.mkdir(parents=True)
    (slot / "token").write_text("aaaaaaaaaaaaaaaa")
    done = threading.Event()

    def run():
        busy.wait(slot, "aaaaaaaaaaaaaaaa", 0, rig.wav, None, (str(rig.short),),
                  mouth=rig.mouth)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.3)
    assert not done.is_set()
    (slot / "token").unlink()
    assert done.wait(1.0), "a held waiter did not see its token go"
    assert pids(rig.mark) == []


def test_a_waiter_held_off_by_speech_ends_when_its_seat_goes(rig):
    rig.speak()
    slot = rig.slot("busy")
    slot.mkdir(parents=True)
    (slot / "token").write_text("aaaaaaaaaaaaaaaa")
    seat = subprocess.Popen(["sleep", "30"])
    done = threading.Event()

    def run():
        busy.wait(slot, "aaaaaaaaaaaaaaaa", 0, rig.wav, seat.pid, (str(rig.short),),
                  mouth=rig.mouth)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.3)
    assert not done.is_set()
    seat.kill()
    seat.wait()
    assert done.wait(1.0), "a held waiter outlived its seat"
    assert pids(rig.mark) == []


def test_wait_takes_an_injected_mouth_over_the_env(rig, tmp_path, monkeypatch):
    other = tmp_path / "other-mouth"
    monkeypatch.delenv("MOUTH_DIR", raising=False)
    monkeypatch.delenv("SESSIONVOICE_HOME", raising=False)
    monkeypatch.setenv("VOICEMODE_MOUTH_DIR", str(other))
    rig.speak()                                     # the rig's mouth speaks
    slot = rig.slot("busy")
    slot.mkdir(parents=True)
    (slot / "token").write_text("aaaaaaaaaaaaaaaa")
    clock = iter([0.0, 0.0, 0.05, 0.1, 0.15, 0.2])
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 3:
            (slot / "token").unlink(missing_ok=True)

    busy.wait(slot, "aaaaaaaaaaaaaaaa", 0, rig.wav, None, (str(rig.short),),
              mouth=rig.mouth, step=0.05, clock=lambda: next(clock, 1.0), sleep=sleep)
    assert slept == [0.05, 0.05, 0.05], "the injected mouth was not the one read"
    assert pids(rig.mark) == []


def _heard(root: Path, kind: str = "lock") -> Path:
    """Give ROOT a heard log, sessionvoice's POPULATED: a `heard.lock`
    (KIND "lock") or a `heard_*.jsonl` (KIND "jsonl")."""
    d = root / "logs" / "conversations"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("heard.lock" if kind == "lock" else "heard_2030-06-09.jsonl")).write_text("")
    return root


def _bare(root: Path) -> Path:
    """ROOT exists, down to its conversations dir, but holds no heard log."""
    (root / "logs" / "conversations").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "conversations" / "other.txt").write_text("")
    return root


def test_the_mouth_dir_is_sessionvoices_rule(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    home = {"HOME": str(h)}
    sv, vm = h / ".sessionvoice", h / ".voicemode"

    # a fresh HOME: sessionvoice's default home
    assert busy.mouth_dir(home) == sv / "mouth"
    # existing but empty homes never decide
    _bare(vm)
    assert busy.mouth_dir(home) == sv / "mouth"
    vm.joinpath("mouth").mkdir()
    assert busy.mouth_dir(home) == sv / "mouth"
    # only ~/.voicemode holds a heard log: adopted in place
    _heard(vm, "jsonl")
    assert busy.mouth_dir(home) == vm / "mouth"
    _bare(sv)
    assert busy.mouth_dir(home) == vm / "mouth"
    # both hold one: ~/.sessionvoice
    _heard(sv, "lock")
    assert busy.mouth_dir(home) == sv / "mouth"

    # the home variables: SESSIONVOICE_HOME, then VOICEMODE_BASE_DIR
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "/s"}) == Path("/s/mouth")
    assert busy.mouth_dir({**home, "VOICEMODE_BASE_DIR": "/b"}) == Path("/b/mouth")
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "/s",
                           "VOICEMODE_BASE_DIR": "/b"}) == Path("/s/mouth")
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "",
                           "VOICEMODE_BASE_DIR": "/b"}) == Path("/b/mouth")
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "",
                           "VOICEMODE_BASE_DIR": ""}) == sv / "mouth"
    # the mouth's own variable, then its old name, by presence
    assert busy.mouth_dir({**home, "MOUTH_DIR": "/m"}) == Path("/m")
    assert busy.mouth_dir({**home, "VOICEMODE_MOUTH_DIR": "/o"}) == Path("/o")
    assert busy.mouth_dir({**home, "MOUTH_DIR": "/m", "VOICEMODE_MOUTH_DIR": "/o",
                           "SESSIONVOICE_HOME": "/s"}) == Path("/m")
    assert busy.mouth_dir({**home, "VOICEMODE_MOUTH_DIR": "/o",
                           "SESSIONVOICE_HOME": "/s"}) == Path("/o")
    # a set but empty MOUTH_DIR hides the old name and falls to the home rule
    assert busy.mouth_dir({**home, "MOUTH_DIR": "", "VOICEMODE_MOUTH_DIR": "/o"}) == sv / "mouth"
    assert busy.mouth_dir({**home, "MOUTH_DIR": "", "VOICEMODE_MOUTH_DIR": "/o",
                           "SESSIONVOICE_HOME": "/s"}) == Path("/s/mouth")
    assert busy.mouth_dir({**home, "VOICEMODE_MOUTH_DIR": ""}) == sv / "mouth"
    # `~` in each variable is the HOME of the env given
    assert busy.mouth_dir({**home, "MOUTH_DIR": "~/m"}) == h / "m"
    assert busy.mouth_dir({**home, "MOUTH_DIR": "~"}) == h
    assert busy.mouth_dir({**home, "VOICEMODE_MOUTH_DIR": "~/o"}) == h / "o"
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "~/s"}) == h / "s" / "mouth"
    assert busy.mouth_dir({**home, "VOICEMODE_BASE_DIR": "~/vm"}) == h / "vm" / "mouth"
    # `$VAR` in a home variable expands against the env given
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "$HOME/s"}) == h / "s" / "mouth"
    assert busy.mouth_dir({**home, "X": "/x",
                           "VOICEMODE_BASE_DIR": "${X}/vm"}) == Path("/x/vm/mouth")
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "/s/$NOPE"}) == Path("/s/$NOPE/mouth")
    # an unclosed `${` is left as it is, and so is everything after it
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "/s/${X$HOME"}) \
        == Path("/s/${X$HOME/mouth")
    # `~//` is still under HOME, never at the root
    assert busy.mouth_dir({**home, "MOUTH_DIR": "~//m"}) == h / "m"
    assert busy.mouth_dir({**home, "SESSIONVOICE_HOME": "~//s"}) == h / "s" / "mouth"
    # MOUTH_DIR expands as a parsed path does: a leading `./` drops away
    assert busy.mouth_dir({**home, "MOUTH_DIR": "./~/m"}) == h / "m"


def test_the_mouth_dir_creates_nothing(tmp_path):
    h = tmp_path / "h"
    h.mkdir()
    busy.mouth_dir({"HOME": str(h)})
    assert list(h.iterdir()) == []


def test_an_unreadable_conversations_dir_is_not_a_heard_log(tmp_path):
    h = tmp_path / "h"
    convo = _heard(h / ".voicemode", "jsonl") / "logs" / "conversations"
    convo.chmod(0o000)
    try:
        if os.access(convo, os.R_OK):
            pytest.skip("this user reads past a mode-0 dir")
        assert busy.mouth_dir({"HOME": str(h)}) == h / ".sessionvoice" / "mouth"
    finally:
        convo.chmod(0o700)


# sessionvoice's resolver, copied here (never imported: sessiontools does not
# depend on sessionvoice) from its `ears/home.py` (`populated`,
# `sessionvoice_home`), `ears/envnames.py` (`get` for MOUTH_DIR) and
# `mouth/paths.py` (`mouth_dir`). It reads the PROCESS env and HOME, as
# sessionvoice does; the table test below runs it under fake HOMEs.

def _sv_populated(root) -> bool:
    d = Path(root) / "logs" / "conversations"
    try:
        return (d / "heard.lock").exists() or any(d.glob("heard_*.jsonl"))
    except OSError:
        return False


def _sv_home() -> Path:
    for name in ("SESSIONVOICE_HOME", "VOICEMODE_BASE_DIR"):
        raw = os.environ.get(name)
        if raw:
            return Path(os.path.expanduser(os.path.expandvars(raw)))
    h = Path(os.path.expanduser("~"))
    if _sv_populated(h / ".sessionvoice"):
        return h / ".sessionvoice"
    if _sv_populated(h / ".voicemode"):
        return h / ".voicemode"
    return h / ".sessionvoice"


def _sv_envnames_get(new: str, old: str):
    for name in (new, old):
        if name in os.environ:
            return os.environ[name]
    return None


def _sv_mouth_dir() -> Path:
    env = _sv_envnames_get("MOUTH_DIR", "VOICEMODE_MOUTH_DIR")
    if env:
        return Path(env).expanduser()
    return _sv_home() / "mouth"


#: (the env, with "@" for the fake HOME; the homes given a heard log
#: ("sv"/"vm", by lock or jsonl); the homes that exist bare).
MOUTH_TABLE = [
    ({}, (), ()),
    ({}, (), ("sv", "vm")),
    ({}, (("sv", "lock"),), ()),
    ({}, (("sv", "jsonl"),), ("vm",)),
    ({}, (("vm", "lock"),), ()),
    ({}, (("vm", "jsonl"),), ("sv",)),
    ({}, (("sv", "lock"), ("vm", "jsonl")), ()),
    ({"SESSIONVOICE_HOME": "/s"}, (("vm", "lock"),), ()),
    ({"VOICEMODE_BASE_DIR": "/b"}, (("sv", "lock"),), ()),
    ({"SESSIONVOICE_HOME": "/s", "VOICEMODE_BASE_DIR": "/b"}, (), ()),
    ({"SESSIONVOICE_HOME": "", "VOICEMODE_BASE_DIR": "/b"}, (), ()),
    ({"SESSIONVOICE_HOME": "", "VOICEMODE_BASE_DIR": ""}, (("vm", "lock"),), ()),
    ({"SESSIONVOICE_HOME": "~/s"}, (), ()),
    ({"VOICEMODE_BASE_DIR": "~"}, (), ()),
    ({"SESSIONVOICE_HOME": "$HOME/s"}, (), ()),
    ({"SESSIONVOICE_HOME": "${HOME}/s/$UNSET_ONE"}, (), ()),
    ({"SESSIONVOICE_HOME": "/s/${UNSET_ONE$HOME"}, (), ()),
    ({"VOICEMODE_BASE_DIR": "$HOME/s/${HOME"}, (), ()),
    ({"MOUTH_DIR": "/m"}, (("vm", "lock"),), ()),
    ({"MOUTH_DIR": "~/m"}, (), ()),
    ({"MOUTH_DIR": "~"}, (), ()),
    ({"VOICEMODE_MOUTH_DIR": "/o"}, (), ()),
    ({"VOICEMODE_MOUTH_DIR": "~/o"}, (), ()),
    ({"MOUTH_DIR": "/m", "VOICEMODE_MOUTH_DIR": "/o"}, (), ()),
    ({"MOUTH_DIR": "", "VOICEMODE_MOUTH_DIR": "/o"}, (), ()),
    ({"MOUTH_DIR": "", "VOICEMODE_MOUTH_DIR": "/o"}, (("vm", "jsonl"),), ()),
    ({"VOICEMODE_MOUTH_DIR": ""}, (("vm", "lock"),), ()),
    ({"MOUTH_DIR": "", "SESSIONVOICE_HOME": "/s"}, (), ()),
    ({"VOICEMODE_MOUTH_DIR": "/o", "SESSIONVOICE_HOME": "/s"}, (), ()),
    ({"MOUTH_DIR": "~//m"}, (), ()),
    ({"MOUTH_DIR": "./~/m"}, (), ()),
    ({"VOICEMODE_MOUTH_DIR": "~//"}, (), ()),
    ({"SESSIONVOICE_HOME": "~//s"}, (), ()),
    ({"VOICEMODE_BASE_DIR": "~//"}, (), ()),
    ({"SESSIONVOICE_HOME": "./~/s"}, (), ()),
    ({"MOUTH_DIR": "rel/m"}, (), ()),
    ({"SESSIONVOICE_HOME": "rel/s"}, (), ()),
]

_MOUTH_VARS = ("MOUTH_DIR", "VOICEMODE_MOUTH_DIR", "SESSIONVOICE_HOME", "VOICEMODE_BASE_DIR",
               "UNSET_ONE")


@pytest.mark.parametrize(("env", "heard", "bare"), MOUTH_TABLE)
def test_the_mouth_dir_equals_sessionvoices_resolver(tmp_path, monkeypatch, env, heard, bare):
    h = tmp_path / "home"
    h.mkdir()
    roots = {"sv": h / ".sessionvoice", "vm": h / ".voicemode"}
    for key, kind in heard:
        _heard(roots[key], kind)
    for key in bare:
        _bare(roots[key])
    for name in _MOUTH_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(h))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    want = _sv_mouth_dir()
    assert busy.mouth_dir(dict(os.environ)) == want
    assert busy.mouth_dir() == want
    assert busy.mouth_dir({"HOME": str(h), **env}) == want


@pytest.mark.parametrize("fake_home", ["/", "/h/", "//h"])
@pytest.mark.parametrize(("name", "value"), [("MOUTH_DIR", "~//m"), ("MOUTH_DIR", "~"),
                                             ("SESSIONVOICE_HOME", "~//s"),
                                             ("SESSIONVOICE_HOME", "~")])
def test_the_mouth_dir_takes_home_as_sessionvoice_does(monkeypatch, fake_home, name, value):
    # HOME at the root, with a trailing slash, or with two leading ones: `~`
    # expands as sessionvoice's own expanduser does. Only stats run here.
    for n in _MOUTH_VARS:
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("HOME", fake_home)
    monkeypatch.setenv(name, value)
    assert busy.mouth_dir({"HOME": fake_home, name: value}) == _sv_mouth_dir()


def test_voicemode_base_dir_moves_the_mouth_the_waiter_reads(rig, monkeypatch):
    monkeypatch.delenv("VOICEMODE_MOUTH_DIR")
    base = rig.root / "vm"
    monkeypatch.setenv("VOICEMODE_BASE_DIR", str(base))
    rig.mouth = base / "mouth"
    base.mkdir()
    rig.speak()
    slot = rig.slot("busy")
    busy.play(slot, rig.wav, 0, player=(str(rig.player),))
    time.sleep(0.4)
    assert pids(rig.mark) == [], "the waiter did not read $VOICEMODE_BASE_DIR/mouth"
    rig.hush()
    assert until(lambda: pids(rig.mark))


def test_through_the_shim_a_batch_over_the_mouth_makes_no_sound(rig):
    """The done-when, in small: a batch that waits on the mouth's speech for
    longer than the delay plays nothing, and its end leaves nothing behind."""
    rig.settings(delay=0.2)
    rig.speak()
    env = {"VOICEMODE_MOUTH_DIR": str(rig.mouth)}
    silent(rig.shim("arm", **env))
    time.sleep(0.8)                                 # the batch runs over the line
    assert pids(rig.mark) == [], "the busy sound played over the mouth"
    silent(rig.shim("end", **env))
    rig.hush()
    time.sleep(0.4)
    assert pids(rig.mark) == []


# --- the claude pid --------------------------------------------------------------

def test_claude_pid_comes_from_the_hook_env():
    assert busy.claude_pid({"CLAUDE_PID": "4242"}, ancestor=lambda: 7) == 4242


@pytest.mark.parametrize("raw", ["", "  ", "1", "0", "x12", "-5"])
def test_without_a_usable_claude_pid_the_ancestor_walk_is_the_fallback(raw):
    assert busy.claude_pid({"CLAUDE_PID": raw}, ancestor=lambda: 77) == 77
    assert busy.claude_pid({}, ancestor=lambda: None) is None


def test_the_ancestor_walk_finds_claude_not_the_shim():
    chain = {300: (200, "python3.14"), 200: (100, "sh"), 100: (50, "claude"), 50: (1, "bash")}
    assert busy.claude_ancestor(300, ps=chain.get) == 100


def test_the_ancestor_walk_without_claude_is_none_and_bounded():
    assert busy.claude_ancestor(300, ps={300: (200, "sh"), 200: (1, "launchd")}.get) is None
    loop = {10: (11, "sh"), 11: (10, "sh")}
    assert busy.claude_ancestor(10, ps=loop.get) is None
    assert busy.claude_ancestor(10, ps=lambda _pid: None) is None


def test_the_ancestor_walk_reads_ps_basenames():
    me = busy._ps(os.getpid())
    assert me is not None and me[0] == os.getppid() and "/" not in me[1]


# --- the batch trigger, in process -------------------------------------------------

def test_arm_plays_the_settings_sound_after_its_delay_and_end_stops_it(rig, monkeypatch):
    rig.settings(delay=0.2)
    # The waiter inherits this process's env, so the fake `afplay` is found first.
    monkeypatch.setenv("PATH", f"{rig.bin}:/usr/bin:/bin")
    env = rig.env()
    assert busy.arm({"session_id": SID}, env) is not None
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    assert busy.end({"session_id": SID}, env) is True
    assert until(lambda: gone(player))


def test_a_subagents_tool_arms_nothing_and_its_batch_end_stops_nothing(rig):
    rig.settings(delay=30)
    env = rig.env()
    assert busy.arm({"session_id": SID, "agent_id": "a0c2"}, env) is None
    assert not (rig.busy / "token").exists()
    assert busy.arm({"session_id": SID}, env) is not None
    assert busy.end({"session_id": SID, "agent_id": "a0c2"}, env) is False
    assert (rig.busy / "token").exists(), "a subagent's batch end stopped the parent's sound"
    assert busy.end({"session_id": SID}, env) is True


def test_clear_stops_even_for_a_payload_with_agent_id(rig):
    rig.settings(delay=30)
    env = rig.env()
    busy.arm({"session_id": SID}, env)
    assert busy.clear({"session_id": SID, "agent_id": "x"}, env) is True


@pytest.mark.parametrize("dot", [".", ".."])
def test_a_dot_session_id_names_no_session_and_writes_nothing(rig, dot):
    """`.` and `..` would resolve to the sessions dir or its parent (ST-90
    do-004 review, advisory 1): `_sid()` refuses them, from the env or the
    payload, so the hook verbs neither stamp `off` nor arm a sound there."""
    rig.settings(delay=30)
    env = rig.env(CLAUDE_CODE_SESSION_ID=dot)
    bare = rig.env(CLAUDE_CODE_SESSION_ID=None)
    assert busy._sid({}, env) is None
    assert busy._sid({"session_id": dot}, bare) is None
    assert busy.arm({"session_id": dot}, env) is None
    assert busy.arm({"session_id": dot}, bare) is None
    assert busy.clear({"session_id": dot}, env) is False
    sessions = rig.root / ".session" / "sessions"
    assert not (sessions / "sounds").exists(), "a sound slot in the sessions dir"
    assert not (sessions.parent / "sounds").exists(), "a sound slot above the sessions dir"


@pytest.mark.parametrize("sid,ok", [
    (SID, True), ("x", True), ("...", True), (".x", True),
    ("", False), (".", False), ("..", False), ("a/b", False), ("../x", False),
    (None, False), (7, False),
])
def test_is_sid(sid, ok):
    assert busy.is_sid(sid) is ok


def test_main_is_always_zero_and_silent(rig, capsys):
    import io
    rig.settings(delay=30)
    env = rig.env()
    assert busy.main(["arm"], io.StringIO("{not json"), env) == 0
    assert busy.main(["arm"], io.StringIO("[1]"), env) == 0
    assert busy.main(["bogus"], io.StringIO("{}"), env) == 0
    assert busy.main([], io.StringIO("{}"), env) == 0
    assert busy.main(["arm"], io.StringIO("{}"), {**env, "SESSIONTOOLS_HOOK_BUSY": "Off"}) == 0
    assert not (rig.busy / "token").exists()
    assert busy.main(["arm"], io.StringIO(""), env) == 0
    assert (rig.busy / "token").exists(), "an empty payload with the env sid should arm"
    assert busy.main(["end"], io.StringIO("{}"), env) == 0
    assert not (rig.busy / "token").exists()
    assert capsys.readouterr() == ("", "")


# --- the shim, as hooks.json runs it ------------------------------------------------

def test_the_shim_arms_returns_at_once_and_the_waiter_is_detached(rig):
    rig.settings(delay=5)
    t0 = time.monotonic()
    result = rig.shim("arm", {"tool_name": "Bash"})
    elapsed = time.monotonic() - t0
    silent(result)
    assert elapsed < 2.0, f"arm took {elapsed:.2f}s: it waited for the delay"
    assert (rig.busy / "token").exists()
    waiter = int((rig.busy / "pgid").read_text().split()[0])
    pgid = subprocess.run(["ps", "-o", "pgid=", "-p", str(waiter)],
                          capture_output=True, text=True, check=False).stdout.strip()
    assert pgid == str(waiter), "the waiter is not its own process-group leader"
    silent(rig.shim("end"))
    assert not (rig.busy / "token").exists()
    assert until(lambda: gone(waiter))


def test_through_the_shim_a_long_batch_plays_and_its_end_stops_the_sound(rig):
    rig.settings(delay=0.3)
    silent(rig.shim("arm"))
    assert until(lambda: pids(rig.mark), timeout=5), "no sound after the delay"
    player = pids(rig.mark)[0]
    silent(rig.shim("arm"))                         # the batch's second tool
    assert len(pids(rig.mark)) == 1
    silent(rig.shim("end"))
    assert until(lambda: gone(player)), "the sound outlived the batch"


def test_through_the_shim_a_short_batch_makes_no_sound(rig):
    rig.settings(delay=2.0)
    silent(rig.shim("arm"))
    time.sleep(0.2)
    silent(rig.shim("end"))
    time.sleep(2.3)
    assert pids(rig.mark) == []


@pytest.mark.parametrize("event", ["Stop", "UserPromptSubmit", "SessionEnd"])
def test_stop_and_user_prompt_submit_clear_an_interrupted_batch(rig, event):
    rig.settings(delay=0.2)
    silent(rig.shim("arm"))
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    silent(rig.shim("clear", {"hook_event_name": event}))
    assert until(lambda: gone(player))
    assert not (rig.busy / "token").exists() and not (rig.busy / "pgid").exists()


def test_stops_verb_stop_clears_an_interrupted_batch_like_clear(rig):
    """Stop's verb is `stop` (ST-98, so the turn mark can tell it from
    UserPromptSubmit); to the sound it is `clear`."""
    rig.settings(delay=0.2)
    silent(rig.shim("arm"))
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    silent(rig.shim("stop", {"hook_event_name": "Stop"}))
    assert until(lambda: gone(player))
    assert not (rig.busy / "token").exists() and not (rig.busy / "pgid").exists()


def test_with_the_turn_mark_on_the_sound_still_arms_and_ends(rig):
    """The mark reads the payload first when it must; the sound's python gets
    the same bytes."""
    rig.settings(delay=0.2)
    (rig.session / "turn-ended").write_text('{"v": 1, "at": 1.0}\n')
    silent(rig.shim("arm", SESSIONTOOLS_HOOK_TURN="on"))
    assert not (rig.session / "turn-ended").exists()
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    silent(rig.shim("stop", {"hook_event_name": "Stop"}, SESSIONTOOLS_HOOK_TURN="on"))
    assert until(lambda: gone(player))
    assert (rig.session / "turn-ended").exists()


def test_through_the_shim_a_subagent_batch_neither_arms_nor_ends(rig):
    rig.settings(delay=30)
    silent(rig.shim("arm", {"agent_id": "a0c2"}))
    assert not (rig.busy / "token").exists()
    silent(rig.shim("arm"))
    silent(rig.shim("end", {"agent_id": "a0c2"}))
    assert (rig.busy / "token").exists()
    silent(rig.shim("end"))
    assert not (rig.busy / "token").exists()


def test_the_off_path_never_starts_python(rig):
    """A seat without the setting: python once (it stamps `off`), then never
    again - not on later tools, not on a batch end, Stop or UserPromptSubmit."""
    silent(rig.shim("arm"))
    assert rig.python_runs() == 1
    assert (rig.busy / "off").exists()
    for _ in range(3):
        silent(rig.shim("arm"))
        silent(rig.shim("end"))
        silent(rig.shim("clear"))
    assert rig.python_runs() == 1


@pytest.mark.parametrize("trigger", ["landed", "finished", "landd"])
def test_a_trigger_that_is_not_batch_stamps_off_and_then_starts_no_python(rig, trigger):
    """ST-90 do-001: the batch guard. A seat set to `landed` or `finished`
    (or a typo) is OFF for the batch trigger: one python to stamp `off`,
    then nothing, and no batch sound however long the batch runs."""
    path = rig.settings(trigger=trigger, delay=0)
    past = time.time() - 60             # older than `off`, even to `-nt`'s whole seconds
    os.utime(path, (past, past))
    silent(rig.shim("arm"))
    assert rig.python_runs() == 1
    assert (rig.busy / "off").exists()
    assert not (rig.busy / "token").exists()
    for _ in range(3):
        silent(rig.shim("arm"))
        silent(rig.shim("end"))
        silent(rig.shim("clear"))
    assert rig.python_runs() == 1
    time.sleep(0.3)
    assert pids(rig.mark) == []


def test_switching_the_session_layer_back_to_batch_re_resolves_and_arms(rig):
    rig.settings(trigger="landed")
    silent(rig.shim("arm"))
    assert (rig.busy / "off").exists()
    time.sleep(1.1)                     # past `off`'s whole second, for `-nt`
    rig.settings(trigger="batch", delay=30)
    silent(rig.shim("arm"))
    assert (rig.busy / "token").exists()
    assert not (rig.busy / "off").exists()


def test_a_session_layer_file_newer_than_off_re_resolves_and_an_older_one_does_not(rig):
    silent(rig.shim("arm"))
    assert rig.python_runs() == 1
    path = rig.settings(delay=30, auto_arm=False)        # written after `off`
    silent(rig.shim("arm"))
    assert rig.python_runs() == 2, "a newer session-layer file did not re-resolve"
    # `off` is re-stamped 1 s before each resolve, so for a second or two
    # after the write the file can still read newer to `-nt`'s whole seconds,
    # and an arm may resolve again. Then it converges: quiet.
    time.sleep(2.1)
    silent(rig.shim("arm"))
    settled = rig.python_runs()
    assert settled <= 3
    for _ in range(3):
        silent(rig.shim("arm"))
    assert rig.python_runs() == settled, "the OFF cache never went quiet again"
    past = time.time() - 60
    os.utime(path, (past, past))
    silent(rig.shim("arm"))
    assert rig.python_runs() == settled


def test_a_host_layer_switch_on_is_heard_mid_session(rig):
    silent(rig.shim("arm"))
    hostfile = host(rig.root, "sounds") / "sounds.json"
    hostfile.parent.mkdir(parents=True)
    hostfile.write_text(json.dumps({"enabled": True, "busy": {
        "auto_arm": True, "file": str(rig.wav), "delay": 30}}))
    silent(rig.shim("arm"))
    assert (rig.busy / "token").exists()
    assert not (rig.busy / "off").exists()


@pytest.mark.parametrize("word", ["off", "OFF", "0", "false", "No"])
def test_the_off_switch_starts_no_python_even_when_set(rig, word):
    rig.settings(delay=0)
    silent(rig.shim("arm", SESSIONTOOLS_HOOK_BUSY=word))
    assert rig.python_runs() == 0
    assert not (rig.busy / "token").exists()


def test_no_session_id_or_no_session_dir_is_silent_and_starts_no_python(rig):
    rig.settings(delay=0)
    silent(rig.shim("arm", CLAUDE_CODE_SESSION_ID=None))
    silent(rig.shim("arm", CLAUDE_CODE_SESSION_ID="99999999-0000-0000-0000-000000000000"))
    silent(rig.shim("arm", CLAUDE_CODE_SESSION_ID="../" + SID))
    silent(rig.shim("arm", CLAUDE_CODE_SESSION_ID="."))    # the sessions dir
    silent(rig.shim("arm", CLAUDE_CODE_SESSION_ID=".."))   # its parent
    silent(rig.shim("end", CLAUDE_CODE_SESSION_ID=".."))
    assert rig.python_runs() == 0


def test_a_sessions_dir_given_as_a_literal_tilde_is_read_under_home(rig):
    """`SESSIONS_DIR=~/...` unexpanded (as python's expanduser reads it): the
    shim expands it against HOME too, so the two agree on the session dir."""
    rig.settings(delay=30)
    silent(rig.shim("arm", SESSIONS_DIR="~/.session/sessions"))
    assert (rig.busy / "token").exists()
    silent(rig.shim("end", SESSIONS_DIR="~/.session/sessions"))
    assert not (rig.busy / "token").exists()


def test_an_unknown_verb_is_silent(rig):
    rig.settings(delay=0)
    silent(rig.shim("bogus"))
    assert rig.python_runs() == 0


def test_no_python3_on_path_is_silent(rig):
    rig.settings(delay=0)
    (rig.bin / "python3").unlink()
    result = subprocess.run(["/bin/sh", str(SHIM), "arm"], input="{}", capture_output=True,
                            text=True, timeout=30, check=False,
                            env={**rig.env(), "PATH": "/nonexistent"})
    silent(result)


# --- the interpreter (ST-90 do-002: decision 2, ruling 1 = B) -------------------------

def marked_python(rig, name: str) -> tuple[Path, Path]:
    """A python that logs each start to its own mark, then runs the real one."""
    mark = rig.root / f"{name}-started"
    d = rig.root / name
    d.mkdir()
    py = d / "python"
    py.write_text(f'#!/bin/sh\necho x >> "{mark}"\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    return py, mark


def fake_session(rig, shebang: str) -> Path:
    """A `session` on the shim's PATH. The shim only reads its first line."""
    tool = rig.bin / "session"
    tool.write_text(f"{shebang}\nimport sys\nsys.exit('never run')\n")
    tool.chmod(0o755)
    return tool


def starts(mark: Path) -> int:
    try:
        return len(mark.read_text().split())
    except FileNotFoundError:
        return 0


def test_the_session_tools_shebang_python_runs_the_hook(rig):
    venv, mark = marked_python(rig, "venv")
    fake_session(rig, f"#!{venv}")
    rig.settings(delay=30)
    silent(rig.shim("arm"))
    assert starts(mark) == 1, "the session tool's python did not run the hook"
    assert rig.python_runs() == 0, "python3 ran although `session` named a python"
    assert (rig.busy / "token").exists()
    silent(rig.shim("end"))
    assert starts(mark) == 2
    assert not (rig.busy / "token").exists()


def test_sessiontools_python_wins_over_the_session_shebang(rig):
    venv, venv_mark = marked_python(rig, "venv")
    over, over_mark = marked_python(rig, "override")
    fake_session(rig, f"#!{venv}")
    rig.settings(delay=30)
    silent(rig.shim("arm", SESSIONTOOLS_PYTHON=str(over)))
    assert (starts(over_mark), starts(venv_mark), rig.python_runs()) == (1, 0, 0)
    assert (rig.busy / "token").exists()


def test_sessiontools_python_may_be_a_name_on_path(rig):
    over, over_mark = marked_python(rig, "override")
    (rig.bin / "py-override").symlink_to(over)
    rig.settings(delay=30)
    silent(rig.shim("arm", SESSIONTOOLS_PYTHON="py-override"))
    assert (starts(over_mark), rig.python_runs()) == (1, 0)


@pytest.mark.parametrize("override", ["/no/such/python", "no-such-python"])
def test_a_sessiontools_python_that_cannot_run_falls_back_to_python3(rig, override):
    venv, venv_mark = marked_python(rig, "venv")
    fake_session(rig, f"#!{venv}")
    rig.settings(delay=30)
    silent(rig.shim("arm", SESSIONTOOLS_PYTHON=override))
    assert (rig.python_runs(), starts(venv_mark)) == (1, 0)
    assert (rig.busy / "token").exists()


def test_a_non_executable_sessiontools_python_falls_back_to_python3(rig):
    over, over_mark = marked_python(rig, "override")
    over.chmod(0o644)
    rig.settings(delay=30)
    silent(rig.shim("arm", SESSIONTOOLS_PYTHON=str(over)))
    assert (rig.python_runs(), starts(over_mark)) == (1, 0)


@pytest.mark.parametrize("shebang", [
    "#!/usr/bin/env python3",           # through env: a space, so not a bare path
    "#!{venv} -I",                      # a bare path with an argument
    "#!/bin/sh",                        # a wrapper script, not a python
    "#!/no/such/dir/python3",           # a python that is not there
    "#!{venv}\r",                      # a CRLF line: the \r is [[:space:]] (do-002 advisory 3)
    "import sys",                       # no shebang at all
    "",                                 # an empty file
])
def test_a_session_shebang_that_is_not_a_bare_python_path_falls_back_to_python3(rig, shebang):
    venv, venv_mark = marked_python(rig, "venv")
    fake_session(rig, shebang.format(venv=venv))
    rig.settings(delay=30)
    silent(rig.shim("arm"))
    assert (rig.python_runs(), starts(venv_mark)) == (1, 0)
    assert (rig.busy / "token").exists()


def test_an_unreadable_session_falls_back_to_python3(rig):
    venv, venv_mark = marked_python(rig, "venv")
    fake_session(rig, f"#!{venv}").chmod(0o311)     # executable, not readable
    if os.access(rig.bin / "session", os.R_OK):
        pytest.skip("running as a user who reads mode 0311 files (root)")
    rig.settings(delay=30)
    silent(rig.shim("arm"))
    assert (rig.python_runs(), starts(venv_mark)) == (1, 0)


def test_no_session_on_path_falls_back_to_python3(rig):
    assert not (rig.bin / "session").exists()
    rig.settings(delay=30)
    silent(rig.shim("arm"))
    assert rig.python_runs() == 1
    assert (rig.busy / "token").exists()


def test_with_a_session_on_path_the_off_path_still_never_starts_python(rig):
    """The choice sits after every fast path: a seat without the setting
    starts the chosen python once, to stamp `off`, then never again."""
    venv, venv_mark = marked_python(rig, "venv")
    fake_session(rig, f"#!{venv}")
    silent(rig.shim("arm"))
    assert (starts(venv_mark), rig.python_runs()) == (1, 0)
    assert (rig.busy / "off").exists()
    for _ in range(3):
        silent(rig.shim("arm"))
        silent(rig.shim("end"))
        silent(rig.shim("clear"))
    silent(rig.shim("arm", SESSIONTOOLS_HOOK_BUSY="off"))
    assert (starts(venv_mark), rig.python_runs()) == (1, 0)


def test_through_the_session_tools_python_the_hook_reads_the_agent_layer(rig):
    """The point of B: a sound switched on ONLY in the agent's front matter
    arms through the shim, because the chosen python has PyYAML (the dev
    venv's python stands in for the CLI's venv)."""
    pytest.importorskip("yaml")
    venv, mark = marked_python(rig, "venv")
    md = rig.root / "agents" / "alice.md"
    md.parent.mkdir()
    md.write_text(f"---\nname: alice\nsettings:\n  sounds.enabled: true\n"
                  f"  sounds.busy.auto_arm: true\n  sounds.busy.file: {rig.wav}\n"
                  f"  sounds.busy.delay: 30\n---\n# alice\n")
    record = rig.session / "settings" / "session.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"agent": "alice", "agent_file": str(md)}))
    fake_session(rig, f"#!{venv}")
    silent(rig.shim("arm"))
    assert (starts(mark), rig.python_runs()) == (1, 0)
    assert (rig.busy / "token").exists(), "the agent layer was not read in the hook"
    assert not (rig.busy / "off").exists()
    silent(rig.shim("end"))
    assert not (rig.busy / "token").exists()


# --- the signals: `landed`, `given`, and multi-slot end/clear (ST-90 do-004) ---------

def seat(rig, monkeypatch, pid: int | None = None) -> dict:
    """A sidecar naming SID with PID (default: this live test process), and
    the rig's env pointing at it. The waiter `signal` spawns inherits this
    process's env, so its PATH finds the fake `afplay` first."""
    side = rig.root / "sidecars"
    side.mkdir(exist_ok=True)
    (side / "seat.json").write_text(json.dumps({
        "sessionId": SID, "status": "busy", "updatedAt": time.time() * 1000,
        "pid": os.getpid() if pid is None else pid}))
    monkeypatch.setenv("PATH", f"{rig.bin}:/usr/bin:/bin")
    monkeypatch.setenv("SESSION_SIDECAR_DIR", str(side))
    return rig.env(SESSION_SIDECAR_DIR=str(side))


def landed(rig, **busy_keys) -> Path:
    """Settings with the trigger `landed` and no delay (it defaults to 0)."""
    rig.slot("unread")                  # the teardown stops it
    keys = {"trigger": "landed", **busy_keys}
    path = rig.settings(**keys)
    if "delay" not in busy_keys:
        data = json.loads(path.read_text())
        del data["busy"]["delay"]
        path.write_text(json.dumps(data))
    return path


def test_landed_with_the_trigger_landed_plays_at_once_on_the_unread_slot(rig, monkeypatch):
    env = seat(rig, monkeypatch)
    landed(rig)
    unread = rig.slot("unread")
    t0 = time.monotonic()
    assert busy.signal("landed", SID, "<m@x>", env) == busy.PLAYED
    assert until(lambda: pids(rig.mark), timeout=3), "no sound on landed"
    assert time.monotonic() - t0 < 2.0, "landed waited: its delay should default to 0"
    assert (unread / "token").exists()
    assert not (rig.busy / "token").exists(), "landed touched the batch trigger's slot"
    assert (unread / "pgid").exists()


@pytest.mark.parametrize("trigger", [None, "batch", "finished"])
def test_landed_with_any_other_trigger_is_ignored(rig, monkeypatch, trigger):
    env = seat(rig, monkeypatch)
    rig.settings(delay=0, **({} if trigger is None else {"trigger": trigger}))
    decision, why = busy.decide("landed", SID, None, env)
    assert decision == busy.IGNORED and "not 'landed'" in why
    assert not (rig.slot("unread") / "token").exists()
    time.sleep(0.3)
    assert pids(rig.mark) == []


@pytest.mark.parametrize("bad", ["unset", "disabled", "typo", "nofile"])
def test_landed_with_no_valid_sound_is_ignored(rig, monkeypatch, bad):
    env = seat(rig, monkeypatch)
    if bad == "disabled":                # a valid landed block, switched off
        path = landed(rig)
        data = json.loads(path.read_text())
        data["enabled"] = False
        path.write_text(json.dumps(data))
    elif bad == "typo":
        rig.settings(trigger="landd")
    elif bad == "nofile":
        landed(rig, file=str(rig.root / "missing.wav"))
    assert busy.signal("landed", SID, None, env) == busy.IGNORED
    assert not (rig.slot("unread") / "token").exists()


@pytest.mark.parametrize("auto_arm", ["absent", False])
def test_landed_plays_without_auto_arm(rig, monkeypatch, auto_arm):
    """`auto_arm` is the batch trigger's switch alone: a landed block without
    it (or with it false) still plays (ST-90 do-004 review, advisory 2)."""
    env = seat(rig, monkeypatch)
    path = landed(rig)
    data = json.loads(path.read_text())
    if auto_arm == "absent":
        del data["busy"]["auto_arm"]
    else:
        data["busy"]["auto_arm"] = auto_arm
    path.write_text(json.dumps(data))
    assert busy.signal("landed", SID, None, env) == busy.PLAYED
    assert until(lambda: pids(rig.mark), timeout=3), "no sound on landed without auto_arm"
    assert (rig.slot("unread") / "token").exists()


def test_given_stops_the_unread_sound_and_is_ignored_when_nothing_plays(rig, monkeypatch):
    env = seat(rig, monkeypatch)
    landed(rig)
    unread = rig.slot("unread")
    assert busy.signal("given", SID, None, env) == busy.IGNORED
    assert busy.signal("landed", SID, None, env) == busy.PLAYED
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    assert busy.signal("given", SID, "<m@x>", env) == busy.STOPPED
    assert until(lambda: gone(player)), "the sound outlived given"
    assert not (unread / "token").exists() and not (unread / "pgid").exists()
    assert busy.signal("given", SID, None, env) == busy.IGNORED


def test_given_needs_no_settings_and_stops_a_slot_armed_before_a_switch(rig, monkeypatch):
    """A seat that switched away from `landed` mid-sound still hears `given`."""
    env = seat(rig, monkeypatch)
    landed(rig)
    assert busy.signal("landed", SID, None, env) == busy.PLAYED
    rig.settings(trigger="batch", delay=8)
    assert busy.signal("given", SID, None, env) == busy.STOPPED


def test_a_second_landed_while_armed_is_ignored_one_sound_per_run_of_lines(rig, monkeypatch):
    env = seat(rig, monkeypatch)
    landed(rig)
    assert busy.signal("landed", SID, "<a@x>", env) == busy.PLAYED
    decision, why = busy.decide("landed", SID, "<b@x>", env)
    assert decision == busy.IGNORED and "armed already" in why
    assert until(lambda: pids(rig.mark))
    time.sleep(0.3)
    assert len(pids(rig.mark)) == 1


@pytest.mark.parametrize("how", ["no sidecar", "dead pid", "no pid"])
def test_landed_for_a_seat_that_is_not_live_is_ignored(rig, monkeypatch, how):
    env = seat(rig, monkeypatch, pid=rig.dead_pid())
    side = Path(env["SESSION_SIDECAR_DIR"]) / "seat.json"
    if how == "no sidecar":
        side.unlink()
    elif how == "no pid":
        data = json.loads(side.read_text())
        del data["pid"]
        side.write_text(json.dumps(data))
    landed(rig)
    decision, why = busy.decide("landed", SID, None, env)
    assert decision == busy.IGNORED and "no live seat" in why
    assert not (rig.slot("unread") / "token").exists()


def test_landed_without_a_session_dir_is_ignored_and_makes_nothing(rig, monkeypatch, tmp_path):
    env = seat(rig, monkeypatch)
    landed(rig)
    other = "1f0e0d0c-0b0a-4908-8706-050403020100"
    assert busy.signal("landed", other, None, env) == busy.IGNORED
    assert not (tmp_path / ".session" / "sessions" / other).exists()


def test_a_set_delay_is_honoured_and_given_inside_it_means_no_sound(rig, monkeypatch):
    env = seat(rig, monkeypatch)
    landed(rig, delay=0.5)
    assert busy.signal("landed", SID, None, env) == busy.PLAYED
    time.sleep(0.1)
    assert busy.signal("given", SID, None, env) == busy.STOPPED
    time.sleep(0.8)
    assert pids(rig.mark) == []


def test_the_speech_guard_applies_to_landed(rig, monkeypatch):
    env = seat(rig, monkeypatch)
    landed(rig)
    rig.speak()
    assert busy.signal("landed", SID, None, env) == busy.PLAYED
    time.sleep(0.5)
    assert pids(rig.mark) == [], "landed played over the mouth"
    rig.hush()
    assert until(lambda: pids(rig.mark), timeout=3), "landed never played after the hush"
    player = pids(rig.mark)[0]
    rig.speak()
    assert until(lambda: gone(player), timeout=2), "the mouth starting did not cut landed"


@pytest.mark.parametrize("event,sid", [
    ("finished", SID),                  # HELD: not built
    ("bogus", SID),
    ("landed", ""),
    ("landed", "../x"),
    ("landed", "."),
    ("landed", ".."),
    ("given", "."),
    ("given", ".."),
])
def test_a_usage_error_raises_value_error(event, sid):
    with pytest.raises(ValueError):
        busy.signal(event, sid)


def test_end_stops_the_unread_slot_and_a_subagents_end_does_not(rig, monkeypatch):
    env = seat(rig, monkeypatch)
    landed(rig)
    unread = rig.slot("unread")
    busy.signal("landed", SID, None, env)
    assert busy.end({"session_id": SID, "agent_id": "a0c2"}, env) is False
    assert (unread / "token").exists(), "a subagent's batch end stopped the unread sound"
    assert busy.end({"session_id": SID}, env) is True
    assert not (unread / "token").exists()


def test_clear_stops_every_slot_at_once(rig, monkeypatch):
    seat(rig, monkeypatch)
    slots = [rig.slot(name) for name in busy.SLOTS]
    for slot in slots:
        busy.play(slot, rig.wav, 30)
    assert busy.clear({"session_id": SID}, rig.env()) is True
    assert [s.name for s in slots if (s / "token").exists()] == []
    assert busy.clear({"session_id": SID}, rig.env()) is False


def test_the_slots_are_busy_unread_finished():
    assert busy.SLOTS == ("busy", "unread", "finished")


def test_through_the_shim_a_batch_end_stops_landed_and_a_subagents_end_does_not(rig, monkeypatch):
    env = seat(rig, monkeypatch)
    landed(rig)
    unread = rig.slot("unread")
    assert busy.signal("landed", SID, None, env) == busy.PLAYED
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    silent(rig.shim("end", {"agent_id": "a0c2"}))
    time.sleep(0.2)
    assert not gone(player), "a subagent's batch end stopped the landed sound"
    silent(rig.shim("end"))
    assert until(lambda: gone(player)), "the landed sound outlived the batch end"
    assert not (unread / "token").exists()


@pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop", "SessionEnd"])
def test_through_the_shim_clear_stops_landed(rig, monkeypatch, event):
    env = seat(rig, monkeypatch)
    landed(rig)
    busy.signal("landed", SID, None, env)
    assert until(lambda: pids(rig.mark))
    player = pids(rig.mark)[0]
    silent(rig.shim("clear", {"hook_event_name": event}))
    assert until(lambda: gone(player))
    assert not (rig.slot("unread") / "token").exists()


@pytest.mark.parametrize("name", ["busy", "unread", "finished"])  # the shim's list, literally
@pytest.mark.parametrize("verb", ["end", "clear"])
def test_the_shims_fast_path_sees_a_sound_in_any_slot(rig, verb, name):
    """The shim's slot list must match busy.SLOTS: a token in ANY one slot
    starts python on end/clear, and it is stopped."""
    slot = rig.slot(name)
    busy.play(slot, rig.wav, 30)
    silent(rig.shim(verb))
    assert rig.python_runs() == 1, f"the shim did not see a sound in {name}/"
    assert not (slot / "token").exists()


def test_the_off_path_starts_no_python_with_all_slots_empty(rig):
    """Every slot dir present, none armed (as after sounds that were
    stopped): end and clear start no python, nor does arm once `off` is
    stamped."""
    for name in busy.SLOTS:
        rig.slot(name).mkdir(parents=True, exist_ok=True)
    silent(rig.shim("arm"))
    assert rig.python_runs() == 1 and (rig.busy / "off").exists()
    for _ in range(3):
        silent(rig.shim("arm"))
        silent(rig.shim("end"))
        silent(rig.shim("clear"))
    assert rig.python_runs() == 1


# --- `session sound`, the CLI -------------------------------------------------------

def run_cli(capsys, *argv) -> tuple[int, str, str]:
    from sessiontools import cli
    try:
        rc = cli.main(["sound", *argv])
    except SystemExit as e:             # argparse's usage error
        rc = e.code
    out, err = capsys.readouterr()
    return rc, out, err


def test_session_sound_landed_plays_and_is_quiet_without_json(rig, monkeypatch, capsys):
    seat(rig, monkeypatch)
    landed(rig)
    assert run_cli(capsys, "landed", "--session", SID, "--ref", "<m@x>") == (0, "", "")
    assert until(lambda: pids(rig.mark))
    rc, out, err = run_cli(capsys, "given", "--session", SID, "--json")
    assert (rc, err) == (0, "")
    assert json.loads(out) == {"event": "given", "decision": "stopped",
                               "why": "the unread sound was stopped", "ref": None}


def test_session_sound_json_says_why_it_ignored(rig, monkeypatch, capsys):
    seat(rig, monkeypatch)
    rig.settings(delay=8)
    rc, out, _ = run_cli(capsys, "landed", "--session", SID, "--ref", "<m@x>", "--json")
    got = json.loads(out)
    assert rc == 0 and got["decision"] == "ignored" and got["ref"] == "<m@x>"
    assert "'batch'" in got["why"]


def test_session_sound_is_zero_for_an_unknown_session(rig, monkeypatch, capsys):
    seat(rig, monkeypatch)
    assert run_cli(capsys, "landed", "--session", "nobody")[:2] == (0, "")


@pytest.mark.parametrize("argv", [
    (),
    ("landed",),                        # no --session
    ("finished", "--session", SID),     # HELD
    ("bogus", "--session", SID),
    ("landed", "--session", "a/b"),
    ("landed", "--session", "."),
    ("given", "--session", ".."),
])
def test_session_sound_usage_errors_exit_2(rig, capsys, argv):
    rc, out, err = run_cli(capsys, *argv)
    assert rc == 2 and out == "" and err
    assert not (rig.slot("unread") / "token").exists()


def test_session_sound_finished_says_it_is_held(rig, capsys):
    _, _, err = run_cli(capsys, "finished", "--session", SID)
    assert "held" in err


def test_session_sound_is_in_the_usage(capsys):
    from sessiontools import cli
    assert cli.main([]) == 0
    assert "sound" in capsys.readouterr().out


# --- the wiring ----------------------------------------------------------------------

def test_hooks_json_wires_every_event_and_keeps_settle():
    hooks = json.loads(HOOKS.read_text())["hooks"]

    def commands(event):
        return [h["command"] for e in hooks[event] for h in e["hooks"]]

    shim = '"${CLAUDE_PLUGIN_ROOT}/hooks/busy-sound"'
    assert commands("PreToolUse") == [f"{shim} arm"]
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    assert commands("PostToolBatch") == ['"${CLAUDE_PLUGIN_ROOT}/hooks/settle-hook"',
                                         f"{shim} end",
                                         '"${CLAUDE_PLUGIN_ROOT}/hooks/clock-hook"']
    assert commands("Stop") == [f"{shim} stop"]      # stop: clear, and sets the turn mark
    for event in ("UserPromptSubmit", "SessionEnd"):
        assert commands(event) == [f"{shim} clear"], event
    for event in hooks.values():
        for e in event:
            for h in e["hooks"]:
                assert h["type"] == "command" and h["timeout"] <= 5


def test_hooks_json_names_the_trigger_key():
    description = json.loads(HOOKS.read_text())["description"]
    assert "busy.trigger" in description
    for mode in ("batch", "landed", "finished"):
        assert mode in description


def test_the_shim_is_executable_and_parses():
    assert os.access(SHIM, os.X_OK)
    assert subprocess.run(["/bin/sh", "-n", str(SHIM)], check=False).returncode == 0


# --- the stop sound (ST-236): the child, the verb, the shim ----------------------------
#
# Fake `mouth` and `afplay` only: each logs its argv and records its pid in a
# mark the teardown kills. No real player is ever on PATH.

class Stop:
    """The rig's stop-sound fakes. `afplay` on PATH logs its argv (and keeps
    playing 30 s, as the rig's own fake does, so a busy sound can be seen to
    stop). `mouth` runs BODY after logging, so a test picks how it answers.
    `short` is a logging player that exits at once, for in-process runs."""

    def __init__(self, rig: Rig):
        self.rig = rig
        self.file = rig.root / "done.aiff"
        self.file.write_text("FORM")
        self.mouth_log = rig.root / "mouth.log"
        self.afplay_log = rig.root / "afplay.log"
        self.pids = rig.root / "stop-pids"
        rig.marks.append(self.pids)
        rig.player.write_text(f'#!/bin/sh\necho $$ >> "{rig.mark}"\n'
                              f'echo "$@" >> "{self.afplay_log}"\nexec sleep 30\n')
        self.short = rig.bin / "short-afplay"
        self.short.write_text(f'#!/bin/sh\necho $$ >> "{self.pids}"\n'
                              f'echo "$@" >> "{self.afplay_log}"\nexit 0\n')
        self.short.chmod(0o755)
        self.mouth = rig.bin / "mouth"

    def mouth_does(self, body: str = "exit 0") -> Path:
        self.mouth.write_text(f'#!/bin/sh\necho $$ >> "{self.pids}"\n'
                              f'echo "$@" >> "{self.mouth_log}"\n{body}\n')
        self.mouth.chmod(0o755)
        return self.mouth

    def mouth_cannot_exec(self) -> Path:
        """Found on PATH (executable), but its interpreter does not exist:
        the exec raises OSError."""
        self.mouth.write_text("#!/nonexistent/interpreter\n")
        self.mouth.chmod(0o755)
        return self.mouth

    def settings(self, file: Path | str | None = None, **extra) -> Path:
        data = {"enabled": True, "stop": {"file": str(file or self.file)}}
        data.update(extra)
        path = self.rig.session / "settings" / "sounds.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    @staticmethod
    def _lines(path: Path) -> list[str]:
        try:
            return path.read_text().splitlines()
        except FileNotFoundError:
            return []

    def mouth_calls(self) -> list[str]:
        return self._lines(self.mouth_log)

    def afplay_calls(self) -> list[str]:
        return self._lines(self.afplay_log)

    @property
    def off(self) -> Path:
        return self.rig.session / "sounds" / "stop" / "off"


@pytest.fixture
def st(rig):
    return Stop(rig)


def settled(timeout: float = 0.5) -> None:
    """Give a detached stop child time to do what it should NOT do."""
    time.sleep(timeout)


# the child's work, in process (design Tests 2 and 13) -------------------------------

def test_stop_run_mouth_ok_queues_once_and_no_afplay(st):
    st.mouth_does("exit 0")
    assert busy.stop_run(st.file, (str(st.mouth),), (str(st.short),)) == "mouth"
    assert st.mouth_calls() == [f"play {st.file}"]
    assert st.afplay_calls() == []


def test_stop_run_mouth_absent_plays_afplay_once(st):
    assert busy.stop_run(st.file, (str(st.rig.bin / "no-mouth"),), (str(st.short),)) == "player"
    assert st.afplay_calls() == [str(st.file)]


def test_stop_run_mouth_absent_on_path_plays_afplay_once(st, monkeypatch):
    """The default MOUTH is looked up on PATH: with none there, afplay."""
    monkeypatch.setenv("PATH", str(st.rig.bin))
    assert busy.stop_run(st.file, busy.MOUTH, (str(st.short),)) == "player"
    assert st.afplay_calls() == [str(st.file)] and st.mouth_calls() == []


@pytest.mark.parametrize("code", [1, 2, 127])
def test_stop_run_mouth_non_zero_falls_back_to_afplay_once(st, code):
    st.mouth_does(f"exit {code}")
    assert busy.stop_run(st.file, (str(st.mouth),), (str(st.short),)) == "player"
    assert st.mouth_calls() == [f"play {st.file}"]
    assert st.afplay_calls() == [str(st.file)]


def test_stop_run_mouth_timeout_is_killed_and_not_followed_by_afplay(st):
    st.mouth_does("exec sleep 30")
    began = time.monotonic()
    # 2 s, not less: under load the fake's shell may not reach its log line
    # in a shorter timeout, and the mouth must be seen to have been asked.
    assert busy.stop_run(st.file, (str(st.mouth),), (str(st.short),), timeout=2.0) == ""
    assert time.monotonic() - began < 10
    assert st.mouth_calls() == [f"play {st.file}"]
    assert st.afplay_calls() == []
    assert until(lambda: all(gone(p) for p in pids(st.pids))), "the mouth outlived its timeout"


@pytest.mark.parametrize("sig", ["KILL", "TERM"])
def test_stop_run_mouth_killed_by_a_signal_is_not_followed_by_afplay(st, sig):
    """A signal is not a refusal: the mouth may have queued before it died,
    so, as after a timeout, nothing follows (never two sounds)."""
    st.mouth_does(f"kill -{sig} $$")
    assert busy.stop_run(st.file, (str(st.mouth),), (str(st.short),)) == ""
    assert st.mouth_calls() == [f"play {st.file}"]
    assert st.afplay_calls() == []


def test_stop_run_mouth_that_cannot_exec_falls_back_to_afplay_once(st, monkeypatch):
    st.mouth_cannot_exec()
    monkeypatch.setenv("PATH", f"{st.rig.bin}:/usr/bin:/bin")
    assert busy.stop_run(st.file, ("mouth",), (str(st.short),)) == "player"
    assert st.afplay_calls() == [str(st.file)]


def test_stop_run_a_player_that_cannot_run_is_quiet(st):
    assert busy.stop_run(st.file, (str(st.rig.bin / "no-mouth"),),
                         (str(st.rig.bin / "no-player"),)) == ""


def test_stop_main_reads_the_argv_stop_play_writes(st):
    st.mouth_does("exit 2")
    argv = [str(st.file), "5.0", "1", str(st.mouth), str(st.short)]
    assert busy.stop_main(argv) == 0
    assert st.mouth_calls() == [f"play {st.file}"]
    assert st.afplay_calls() == [str(st.file)]


@pytest.mark.parametrize("argv", [[], ["f"], ["f", "x", "1", "m"], ["f", "1", "z", "m"]])
def test_stop_main_on_a_bad_argv_is_zero(argv):
    assert busy.stop_main(argv) == 0


def test_stop_play_detaches_the_child_and_it_queues_through_the_mouth(st):
    st.mouth_does("exit 0")
    began = time.monotonic()
    assert busy.stop_play(st.file, mouth=(str(st.mouth),), player=(str(st.short),)) is True
    assert time.monotonic() - began < 2
    assert until(lambda: st.mouth_calls() == [f"play {st.file}"])
    settled()
    assert st.afplay_calls() == []


def test_stop_play_returns_at_once_with_a_slow_mouth(st):
    st.mouth_does("exec sleep 30")
    began = time.monotonic()
    assert busy.stop_play(st.file, mouth=(str(st.mouth),), player=(str(st.short),),
                          timeout=2.0) is True
    assert time.monotonic() - began < 2
    assert until(lambda: st.mouth_calls())
    assert until(lambda: all(gone(p) for p in pids(st.pids)))
    settled()
    assert st.afplay_calls() == [], "a timed-out mouth was followed by afplay"


def test_stop_play_never_raises(st, monkeypatch):
    def boom(*a, **k):
        raise OSError("no fork")
    monkeypatch.setattr(busy.subprocess, "Popen", boom)
    assert busy.stop_play(st.file) is False
    assert busy.stop_play(st.file, mouth=()) is False


# the verb, in process (design Tests 3 and 4) ---------------------------------------

@pytest.fixture
def launches(monkeypatch):
    got: list[str] = []
    monkeypatch.setattr(busy, "stop_play", lambda file, **k: got.append(str(file)) or True)
    return got


def test_stop_sound_on_clears_off_and_launches_once(st, launches):
    st.off.parent.mkdir(parents=True)
    st.off.write_text("")
    st.settings()
    assert busy.stop_sound({}, st.rig.env()) is True
    assert launches == [str(st.file)]
    assert not st.off.exists()


def test_stop_sound_off_stamps_off_back_dated_and_launches_nothing(st, launches):
    before = time.time()
    assert busy.stop_sound({}, st.rig.env()) is False
    after = time.time()
    assert launches == []
    mtime = st.off.stat().st_mtime
    assert before - 1.0 - 0.01 <= mtime <= after - 1.0 + 0.01


def test_stop_sound_for_a_subagent_does_nothing_and_stamps_nothing(st, launches):
    st.settings()
    assert busy.stop_sound({"agent_id": "a0c2"}, st.rig.env()) is False
    assert launches == [] and not st.off.parent.exists()


def test_stop_sound_without_a_session_dir_does_nothing(st, launches, tmp_path):
    env = st.rig.env(CLAUDE_CODE_SESSION_ID="99999999-0000-0000-0000-000000000000")
    assert busy.stop_sound({}, env) is False
    assert launches == []
    assert not (tmp_path / ".session" / "sessions" / "99999999-0000-0000-0000-000000000000").exists()


@pytest.mark.parametrize("sid", [None, "", ".", "..", "a/b"])
def test_stop_sound_without_a_usable_sid_does_nothing(st, launches, sid):
    st.settings()
    env = st.rig.env(CLAUDE_CODE_SESSION_ID=None)
    assert busy.stop_sound({"session_id": sid}, env) is False
    assert launches == []


@pytest.mark.parametrize("on", [False, True])
def test_stop_sound_touches_only_its_own_slot(st, launches, on):
    """ST-70 F6: the stop path never reaches `sounds/busy/` (an engine's
    record and the busy stamp survive it), nor any other slot."""
    busy_slot = st.rig.busy
    busy_slot.mkdir(parents=True)
    (busy_slot / "off").write_text("")
    (busy_slot / f"engine.{os.getpid()}").write_text("{}")
    before = {p.name: p.stat().st_mtime_ns for p in busy_slot.iterdir()}
    if on:
        st.settings()
    busy.stop_sound({}, st.rig.env())
    assert {p.name: p.stat().st_mtime_ns for p in busy_slot.iterdir()} == before
    made = sorted(p.name for p in (st.rig.session / "sounds").iterdir()
                  if not p.name.startswith("."))
    assert made == (["busy"] if on else ["busy", "stop"])


def test_stop_turn_stops_an_armed_busy_slot_and_still_plays(st, launches):
    st.settings()
    slot = st.rig.busy
    busy.play(slot, st.rig.wav, 0, player=(str(st.rig.player),))
    assert until(lambda: pids(st.rig.mark))
    player = pids(st.rig.mark)[0]
    assert busy.stop_turn({}, st.rig.env()) is True
    assert until(lambda: gone(player)), "the busy sound outlived Stop"
    assert not (slot / "token").exists()
    assert launches == [str(st.file)]


@pytest.mark.parametrize("verb", ["end", "clear"])
def test_only_stop_plays_it_end_and_clear_never_do(st, launches, verb):
    st.settings()
    assert busy.main([verb], io_of({}), st.rig.env()) == 0
    assert launches == [] and not st.off.exists()


def test_main_stop_plays_it_and_is_silent(st, launches, capsys):
    st.settings()
    assert busy.main(["stop"], io_of({}), st.rig.env()) == 0
    assert launches == [str(st.file)]
    assert capsys.readouterr() == ("", "")


def test_main_stop_is_zero_and_silent_when_the_play_blows_up(st, monkeypatch, capsys):
    st.settings()

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(busy, "stop_sound", boom)
    assert busy.main(["stop"], io_of({}), st.rig.env()) == 0
    assert capsys.readouterr() == ("", "")


def io_of(payload) -> object:
    import io
    return io.StringIO(json.dumps(payload))


# the shim, as hooks.json runs it (design Tests 5-10, 12) ----------------------------

def test_through_the_shim_unset_stop_plays_nothing(st):
    st.mouth_does("exit 0")
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    settled()
    assert st.mouth_calls() == [] and st.afplay_calls() == []
    assert st.off.exists()


def test_through_the_shim_set_stop_queues_the_sound_once(st):
    st.mouth_does("exit 0")
    st.settings()
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert until(lambda: st.mouth_calls() == [f"play {st.file}"])
    settled()
    assert st.mouth_calls() == [f"play {st.file}"]
    assert st.afplay_calls() == []


def test_through_the_shim_each_stop_queues_once(st):
    st.mouth_does("exit 0")
    st.settings()
    for n in (1, 2):
        silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
        assert until(lambda n=n: len(st.mouth_calls()) == n)
    settled()
    assert len(st.mouth_calls()) == 2 and st.afplay_calls() == []


def test_through_the_shim_no_mouth_plays_afplay_once(st):
    st.settings()
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert until(lambda: st.afplay_calls() == [str(st.file)])
    settled()
    assert st.afplay_calls() == [str(st.file)]


def test_through_the_shim_a_mouth_that_refuses_falls_back_to_afplay_once(st):
    st.mouth_does("exit 2")
    st.settings()
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert until(lambda: st.afplay_calls() == [str(st.file)])
    settled()
    assert st.mouth_calls() == [f"play {st.file}"]
    assert st.afplay_calls() == [str(st.file)]


def test_through_the_shim_a_subagents_stop_plays_nothing(st):
    st.mouth_does("exit 0")
    st.settings()
    silent(st.rig.shim("stop", {"hook_event_name": "Stop", "agent_id": "a0c2"}))
    silent(st.rig.shim("stop", {"hook_event_name": "Stop", "agent_id": "a0c2"},
                       SESSIONTOOLS_HOOK_TURN="on"))
    settled()
    assert st.mouth_calls() == [] and st.afplay_calls() == []


@pytest.mark.parametrize("word", ["off", "0", "false", "No"])
def test_through_the_shim_the_off_switch_silences_it(st, word):
    st.mouth_does("exit 0")
    st.settings()
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}, SESSIONTOOLS_HOOK_BUSY=word))
    settled()
    assert st.mouth_calls() == [] and st.afplay_calls() == []
    assert st.rig.python_runs() == 0


@pytest.mark.parametrize("bad", [{"enabled": "true"}, {"stop": "on"},
                                 {"stop": {"file": "relative.aiff"}},
                                 {"stop": {"file": "/no/such.aiff"}}])
def test_through_the_shim_a_bad_value_is_silent(st, bad):
    st.mouth_does("exit 0")
    data = {"enabled": True, "stop": {"file": str(st.file)}}
    data.update(bad)
    path = st.settings()
    path.write_text(json.dumps(data))
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    settled()
    assert st.mouth_calls() == [] and st.afplay_calls() == []


def test_through_the_shim_the_hook_returns_promptly_with_a_slow_mouth(st):
    st.mouth_does("exec sleep 30")
    st.settings()
    began = time.monotonic()
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert time.monotonic() - began < 3, "the hook waited for the mouth"
    assert until(lambda: st.mouth_calls() == [f"play {st.file}"])
    # The mouth is killed while the child still waits on it (as the teardown
    # would): the child must not start afplay after it, nor outlive the test.
    for pid in pids(st.pids):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    settled()
    assert st.afplay_calls() == [], "a killed mouth was followed by afplay"


def test_through_the_shim_an_off_seats_second_stop_starts_no_python(st):
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert st.rig.python_runs() == 1 and st.off.exists()
    for _ in range(3):
        silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert st.rig.python_runs() == 1


def test_through_the_shim_a_newer_session_layer_re_resolves_and_plays(st):
    st.mouth_does("exit 0")
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert st.rig.python_runs() == 1
    st.settings()                         # written after `off`: newer to `-nt`
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert st.rig.python_runs() == 2
    assert until(lambda: st.mouth_calls() == [f"play {st.file}"])
    assert not st.off.exists()


def test_through_the_shim_an_older_layer_file_does_not_re_resolve(st):
    path = st.settings(file="/no/such.aiff")
    past = time.time() - 60
    os.utime(path, (past, past))
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert st.rig.python_runs() == 1 and st.off.exists()
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert st.rig.python_runs() == 1


def test_through_the_shim_after_the_first_arm_an_off_seats_stop_starts_no_python(st):
    """D8: the arm's resolve stamps `sounds/stop/off`, so the first Stop of a
    seat that called a tool first starts no python."""
    silent(st.rig.shim("arm"))
    assert st.rig.python_runs() == 1 and st.off.exists()
    silent(st.rig.shim("end"))
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    silent(st.rig.shim("clear", {"hook_event_name": "UserPromptSubmit"}))
    assert st.rig.python_runs() == 1


def test_through_the_shim_an_afplay_busy_seat_starts_no_python_at_stop(st):
    """D8 for a seat with only a busy sound: the arm stamps stop's `off`."""
    path = st.rig.settings(delay=30)
    past = time.time() - 60             # older than `off`, even to `-nt`'s whole seconds
    os.utime(path, (past, past))
    silent(st.rig.shim("arm"))
    silent(st.rig.shim("end"))
    runs = st.rig.python_runs()
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert st.rig.python_runs() == runs


def test_through_the_shim_an_on_seat_starts_python_every_stop(st):
    st.mouth_does("exit 0")
    st.settings()
    silent(st.rig.shim("arm"))
    assert not st.off.exists()
    for n in (2, 3):
        silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
        assert st.rig.python_runs() == n


def test_through_the_shim_stop_still_stops_the_busy_sound_and_plays(st):
    """R7: an armed busy sound is stopped by Stop as before, and the stop
    sound still plays (it is not in the slots `clear` stops)."""
    st.mouth_does("exit 0")
    path = st.rig.settings(delay=0.2)
    data = json.loads(path.read_text())
    data["stop"] = {"file": str(st.file)}
    path.write_text(json.dumps(data))
    silent(st.rig.shim("arm"))
    assert until(lambda: str(st.rig.wav) in st.afplay_calls())
    player = pids(st.rig.mark)[0]
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    assert until(lambda: gone(player)), "the busy sound outlived Stop"
    assert not (st.rig.busy / "token").exists()
    assert until(lambda: st.mouth_calls() == [f"play {st.file}"])


@pytest.mark.parametrize("verb", ["end", "clear"])
def test_through_the_shim_end_and_clear_never_play_it(st, verb):
    """Only `stop` plays it: with a busy sound armed (so python runs on
    end and clear), still nothing reaches the mouth or afplay for it."""
    st.mouth_does("exit 0")
    path = st.rig.settings(delay=30)
    data = json.loads(path.read_text())
    data["stop"] = {"file": str(st.file)}
    path.write_text(json.dumps(data))
    silent(st.rig.shim("arm"))
    assert (st.rig.busy / "token").exists()
    silent(st.rig.shim(verb))
    assert not (st.rig.busy / "token").exists()
    settled()
    assert st.mouth_calls() == [] and str(st.file) not in st.afplay_calls()


def test_through_the_shim_a_layer_that_is_not_json_is_silent(st):
    st.mouth_does("exit 0")
    path = st.settings()
    path.write_text("{")
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}))
    settled()
    assert st.mouth_calls() == [] and st.afplay_calls() == []


def test_through_the_shim_no_python_is_silent(st):
    st.settings()
    (st.rig.bin / "python3").unlink()
    result = subprocess.run(["/bin/sh", str(SHIM), "stop"], input="{}", capture_output=True,
                            text=True, timeout=30, check=False,
                            env={**st.rig.env(), "PATH": "/nonexistent"})
    silent(result)


def test_through_the_shim_a_python_that_crashes_is_silent(st):
    st.settings()
    crash = st.rig.bin / "crash-python"
    crash.write_text("#!/bin/sh\necho Traceback >&2\nexit 1\n")
    crash.chmod(0o755)
    silent(st.rig.shim("stop", {"hook_event_name": "Stop"}, SESSIONTOOLS_PYTHON=str(crash)))
