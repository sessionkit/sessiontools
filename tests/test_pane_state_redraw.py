"""No spawn per tmux redraw, measured (ST-134 do-003, design.md §6 row 3).

A PRIVATE tmux server (`-L st134-<rand>` under conftest's scratch
TMUX_TMPDIR) runs one pane, `sleep 600`. A client is attached in a pty
with `status-interval 1`. For 20 s its status line shows
`#{@session_state}`, and the server's children are sampled every 0.5 ms
in-process (libproc on Darwin, /proc on Linux: a `pgrep` loop would itself
spawn, and would miss a job that lives a few ms). Only the pane's process may
be there. The value is changed mid-run, and the client's screen must show
the new word: the status line is being redrawn, from tmux's own memory.

CONTROL: the same server and client with `#(date)` in the status line. The
same sampler must see children appear: the instrument can see a spawn.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import secrets
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from sessiontools import turn

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

WATCH_SECONDS = 20.0
CONTROL_SECONDS = 5.0           # status-interval 1: about one job a second
SAMPLE_SECONDS = 0.0005


def _sampler():
    """children(pid) -> the pids whose parent is PID, read with no spawn."""
    if sys.platform == "darwin":
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = (ctypes.c_int * 1024)()

        def children(pid: int) -> set[int]:
            n = lib.proc_listchildpids(pid, buf, ctypes.sizeof(buf))
            return set(buf[:n]) if n > 0 else set()
        return children
    if Path("/proc/self/task").is_dir():
        def children(pid: int) -> set[int]:
            found: set[int] = set()
            for task in Path(f"/proc/{pid}/task").glob("*/children"):
                try:
                    found.update(int(x) for x in task.read_text().split())
                except OSError:
                    pass
            return found
        return children
    pytest.skip("no in-process child listing on this platform")


@pytest.fixture(autouse=True)
def _walled(monkeypatch):
    monkeypatch.delenv(turn.SWITCH_ENV, raising=False)
    assert os.path.basename(os.environ["TMUX_TMPDIR"]).startswith("st-tmux-")
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ


class Attached:
    """A private server with one pane and one client attached in a pty."""

    def __init__(self):
        self.socket = f"st134-{secrets.token_hex(3)}"
        out = self.tmux("new-session", "-d", "-x", "100", "-y", "10", "-P", "-F", "#{pane_id}",
                        "sleep 600")
        assert out.returncode == 0, out.stderr
        self.pane = out.stdout.strip()
        for option in (("status", "on"), ("status-interval", "1"), ("status-left", ""),
                       ("status-right-length", "60")):
            assert self.tmux("set-option", "-g", *option).returncode == 0
        self.server = int(self.tmux("display-message", "-p", "#{pid}").stdout.strip())
        self.pane_pids = {int(x) for x in
                          self.tmux("list-panes", "-a", "-F", "#{pane_pid}").stdout.split()}
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 100, 0, 0))
        env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
        env["TERM"] = "xterm-256color"
        env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]
        self.client = subprocess.Popen(["tmux", "-L", self.socket, "attach"], stdin=slave,
                                       stdout=slave, stderr=slave, env=env,
                                       start_new_session=True)
        os.close(slave)
        self.master = master
        os.set_blocking(master, False)
        self.screen = b""
        t0 = time.monotonic()
        while not self.tmux("list-clients", "-F", "#{client_name}").stdout.strip():
            assert time.monotonic() - t0 < 10, "the client never attached"
            self.drain()
            time.sleep(0.05)

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True,
                              text=True, check=False, timeout=10)

    def drain(self) -> None:
        """Read what the client drew, so it never blocks on a full pty."""
        while select.select([self.master], [], [], 0)[0]:
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                return
            if not chunk:
                return
            self.screen += chunk

    def spawns(self, seconds: float, children) -> set[int]:
        """Pids that were the server's children, other than its panes, at any
        sample over SECONDS."""
        seen: set[int] = set()
        t0 = time.monotonic()
        next_drain = t0
        while time.monotonic() - t0 < seconds:
            seen |= children(self.server) - self.pane_pids
            now = time.monotonic()
            if now >= next_drain:
                self.drain()
                next_drain = now + 0.05
            time.sleep(SAMPLE_SECONDS)
        self.drain()
        return seen

    def close(self) -> None:
        if self.client.poll() is None:
            self.client.terminate()
            self.client.wait(timeout=10)
        os.close(self.master)


@pytest.fixture
def attached():
    a = Attached()
    yield a
    a.close()          # conftest kills the private server


def test_the_status_line_reads_the_option_with_no_spawn_and_the_control_sees_one(attached):
    children = _sampler()
    a = attached
    assert a.tmux("set-option", "-p", "-t", a.pane, "@session_state", "BUSY").returncode == 0
    assert a.tmux("set-option", "-g", "status-right", "<#{@session_state}>").returncode == 0

    first = a.spawns(WATCH_SECONDS / 2, children)
    assert a.tmux("set-option", "-p", "-t", a.pane, "@session_state", "FREE").returncode == 0
    second = a.spawns(WATCH_SECONDS / 2, children)
    assert first == set() and second == set(), f"the server spawned {first | second}"
    assert b"<BUSY>" in a.screen and b"<FREE>" in a.screen      # redrawn, from memory

    # CONTROL: the same run with a command substitution. The instrument sees
    # it, and sees it again each status interval, not once at the change.
    assert a.tmux("set-option", "-g", "status-right", "<#(date +%s)>").returncode == 0
    spawned = a.spawns(CONTROL_SECONDS, children)
    print(f"no spawn over {WATCH_SECONDS:g} s with #{{@session_state}}; "
          f"#(date) spawned {len(spawned)} children in {CONTROL_SECONDS:g} s")
    assert len(spawned) >= 3, f"the control saw {sorted(spawned)}: the instrument is blind"
