"""The pane signal end to end, on a PRIVATE tmux server (ST-134 do-002).

The server is `-L st134-<rand>` under conftest's scratch TMUX_TMPDIR. The
sidecar folder, the state dir and HOME are conftest's scratch ones too. No
real server, sidecar, tmux.conf, ~/.claude or launchd is reached, and the
first thing each test does is check that.

A FAKE CLAUDE runs in each pane. It reads its own `lstart`, starts one
tool shell the way Claude Code does (a direct `bash -c … eval '…' < /dev/null
&& pwd -P` child), then writes its own sidecar, status `shell`. The
publisher runs as a real `watch` subprocess at the default 1 s interval. The
test polls `show-options -p -v @session_state` every 0.1 s.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from sessiontools import pane_state, procs, turn

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

BOUND = 2.0          # the criterion: BUSY / FREE within 2 s

FAKE_CLAUDE = textwrap.dedent("""\
    import json, os, subprocess, sys, time
    sidecars, mode, bindir = sys.argv[1:4]
    pid = os.getpid()
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], env=env,
                           capture_output=True, text=True).stdout.strip()
    stdin = None
    if mode == "sleep":
        script = "eval 'sleep 30' < /dev/null && pwd -P >| /dev/null"
    elif mode == "listener":
        script = "eval '" + bindir + "/pager listen --timeout 60' < /dev/null && pwd -P >| /dev/null"
    else:                                    # a tool shell with nothing under it
        script = "read -t 60 && pwd -P >| /dev/null"
        stdin = subprocess.PIPE
    shell = subprocess.Popen(["bash", "-c", script], stdin=stdin)
    data = {"pid": pid, "sessionId": "0000" + str(pid).zfill(4)[-4:] + "-0000-4000-8000-000000000134",
            "status": "shell", "updatedAt": int(time.time() * 1000), "procStart": start,
            "tmux": "st134:@1." + os.environ.get("TMUX_PANE", "")}
    tmp = os.path.join(sidecars, "." + str(pid) + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.rename(tmp, os.path.join(sidecars, str(pid) + ".json"))
    time.sleep(90)
""")

PAGER = "import time\ntime.sleep(60)\n"

WATCH = "import sys; from sessiontools.pane_state import watch; sys.exit(watch(stats=True))"
SHOW = "import sys; from sessiontools.cli import main; sys.exit(main(['pane-state', 'show', '--json']))"


@pytest.fixture(autouse=True)
def _walled(monkeypatch):
    monkeypatch.delenv(turn.SWITCH_ENV, raising=False)
    assert os.path.basename(os.environ["TMUX_TMPDIR"]).startswith("st-tmux-")
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    assert "no-sidecars" in os.environ["SESSION_SIDECAR_DIR"]
    assert ".claude" not in os.environ["SESSION_SIDECAR_DIR"]


class Server:
    def __init__(self, tmp: Path):
        self.socket = f"st134-{secrets.token_hex(3)}"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "pager").write_text(f"#!{sys.executable}\n{PAGER}")
        (self.bin / "pager").chmod(0o755)
        self.fake = tmp / "fake_claude.py"
        self.fake.write_text(FAKE_CLAUDE)
        self.watchers: list[subprocess.Popen] = []

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.socket, *args],
                              capture_output=True, text=True, check=False, timeout=10)

    def pane(self, command: str, first: bool = False) -> str:
        verb = ["new-session", "-d", "-x", "80", "-y", "24"] if first else ["new-window", "-d"]
        out = self.tmux(*verb, "-P", "-F", "#{pane_id}", command)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def seat(self, mode: str, first: bool = False) -> str:
        sidecars = os.environ["SESSION_SIDECAR_DIR"]
        return self.pane(f"{sys.executable} {self.fake} {sidecars} {mode} {self.bin}", first)

    def state(self, pane: str) -> str | None:
        out = self.tmux("show-options", "-p", "-t", pane, "-v", "@session_state")
        return out.stdout.strip() if out.returncode == 0 else None

    def why(self, pane: str) -> str | None:
        out = self.tmux("show-options", "-p", "-t", pane, "-v", "@session_state_why")
        return out.stdout.strip() if out.returncode == 0 else None

    def wait_for(self, pane: str, word: str | None, bound: float = BOUND) -> float:
        t0 = time.monotonic()
        while time.monotonic() - t0 < bound:
            if self.state(pane) == word:
                return time.monotonic() - t0
            time.sleep(0.1)
        pytest.fail(f"{pane}: {self.state(pane)!r} ({self.why(pane)!r}), not {word!r} "
                    f"within {bound} s")

    def watch(self) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", WATCH], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.watchers.append(p)
        return p

    def pane_pid(self, pane: str) -> int:
        return int(self.tmux("display-message", "-p", "-t", pane, "#{pane_pid}").stdout.strip())

    def seat_written(self, pane: str) -> None:
        """Wait until the fake claude in PANE has written its sidecar."""
        pid = self.pane_pid(pane)
        path = Path(os.environ["SESSION_SIDECAR_DIR"]) / f"{pid}.json"
        t0 = time.monotonic()
        while not path.exists():
            assert time.monotonic() - t0 < 10, f"no sidecar from {pane}"
            time.sleep(0.05)

    def stop(self) -> None:
        for p in self.watchers:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)       # our own group only
                p.wait(timeout=10)


@pytest.fixture
def server(tmp_path):
    s = Server(tmp_path)
    yield s
    s.stop()          # conftest kills the private server and its panes


def _sleep_under(pid: int) -> int:
    table = procs.table(commands=True)
    for p in table.values():
        parent = table.get(p.ppid)
        if (p.command or "").startswith("sleep 30") and parent and parent.ppid == pid:
            return p.pid
    pytest.fail(f"no sleep 30 under {pid}")


def test_busy_free_and_unknown_on_a_private_server(server):
    sleeper = server.seat("sleep", first=True)
    listener = server.seat("listener")
    empty = server.seat("empty")
    bystander = server.pane("sleep 90")             # no seat in it
    for pane in (sleeper, listener, empty):
        server.seat_written(pane)
    # a SIGKILLed predecessor's words: a wrong FREE on a busy seat, and on a seatless pane
    assert server.tmux("set-option", "-p", "-t", sleeper, "@session_state", "FREE").returncode == 0
    assert server.tmux("set-option", "-p", "-t", bystander, "@session_state", "FREE").returncode == 0

    watcher = server.watch()
    t_busy = server.wait_for(sleeper, "BUSY")
    assert server.why(sleeper) == "work: sleep 30"
    server.wait_for(listener, "FREE")
    assert server.why(listener) == "listener"
    server.wait_for(empty, "BUSY")
    assert server.why(empty) == "work: shell, nothing under it"
    server.wait_for(bystander, None)                # full re-write at start

    os.kill(_sleep_under(server.pane_pid(sleeper)), signal.SIGTERM)
    t_free = server.wait_for(sleeper, "FREE")
    assert server.why(sleeper) == "nothing live"
    print(f"BUSY after {t_busy:.2f} s, FREE {t_free:.2f} s after the kill")
    assert t_busy < BOUND and t_free < BOUND

    # a second publisher on the same lock exits 0 and names the holder
    second = subprocess.run([sys.executable, "-c", WATCH], capture_output=True, text=True,
                            timeout=20, check=False)
    assert second.returncode == 0
    assert f"pid {watcher.pid} on " in second.stderr
    assert server.state(listener) == "FREE"         # and it touched nothing

    # show reads every seat and what its pane holds, beside the live publisher
    before = {pane: (server.state(pane), server.why(pane)) for pane in (sleeper, listener, empty)}
    shown = subprocess.run([sys.executable, "-c", SHOW], capture_output=True, text=True,
                           timeout=20, check=False)
    assert shown.returncode == 0, shown.stderr
    rows = {r["pane"]: r for r in json.loads(shown.stdout)}
    assert {p: (rows[p]["state"], rows[p]["published"]) for p in before} == {
        sleeper: ("FREE", "FREE"), listener: ("FREE", "FREE"), empty: ("BUSY", "BUSY")}
    assert {pane: (server.state(pane), server.why(pane)) for pane in before} == before

    # SIGTERM: every option it wrote is unset - UNKNOWN, never a stale FREE
    watcher.send_signal(signal.SIGTERM)
    out, err = watcher.communicate(timeout=10)
    assert watcher.returncode == 0, err
    for pane in (sleeper, listener, empty, bystander):
        assert server.state(pane) is None and server.why(pane) is None
    assert out.strip().splitlines()[-1].startswith("ticks=")


class _Clock:
    """Monotonic time the test moves: watch()'s wait advances it, so a
    minute of ticks runs in about a second, on real ps and real tmux."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Minute:
    """watch()'s STOP: each wait is one tick's interval of clock time. It keeps
    the counters at the first wait (start-up done) and the last (before the
    stop unsets everything), and stops after SECONDS."""

    def __init__(self, clock: _Clock, pub, seconds: float):
        self.clock, self.pub, self.seconds = clock, pub, seconds
        self.start: pane_state.Stats | None = None
        self.end: pane_state.Stats | None = None
        self._from = clock.now

    def wait(self, period: float) -> bool:
        snap = pane_state.Stats(**vars(self.pub.stats))
        if self.start is None:
            self.start = snap
        self.clock.now += period
        if self.clock.now - self._from > self.seconds:
            self.end = snap
            return True
        return False

    def set(self) -> None:
        self.clock.now = float("inf")


def test_a_quiet_minute_costs_no_tmux_run_and_only_the_safety_ps(server):
    """watch --stats over 60 s with no change and no work: 0 tmux runs and only
    the 30 s safety reads of the process table (design.md §6 row 3, part 3).
    A FREE seat (its only child the pager stand-in) sits in a pane; nothing
    moves. Real ps, real tmux on the private server; the clock is the test's."""
    pane = server.seat("listener", first=True)
    server.seat_written(pane)
    clock = _Clock()
    pub = pane_state.Publisher(clock=clock)
    minute = _Minute(clock, pub, 60.0)
    out = io.StringIO()
    assert pane_state.watch(period=1.0, stop=minute, publisher=pub, stats=True, out=out) == 0
    start, end = minute.start, minute.end
    # start-up: the seed read, the pane index, the one write of FREE
    assert (start.ticks, start.ps_runs, start.tmux_runs, start.writes) == (1, 1, 3, 2)
    quiet = (end.ticks - start.ticks, end.ps_runs - start.ps_runs, end.tmux_runs - start.tmux_runs)
    print(f"a quiet minute: {quiet[0]} ticks, {quiet[1]} ps, {quiet[2]} tmux")
    assert quiet == (60, 2, 0)                      # the reads at 30 s and 60 s; no tmux at all
    assert server.state(pane) is None               # and the stop unset it
    assert out.getvalue().strip().startswith("ticks=61 ps=3 tmux=4 ")
