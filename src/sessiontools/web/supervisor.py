"""The supervisor: the front door starts its rooms, restarts one that exits,
and stops them all by process group.

PROPOSED (kit-web-shell design §6). At start each found room's command is
launched with `{socket}` set to `<run>/<name>.sock`, in a process group of
its own (`start_new_session`), with its output to `<log>/<name>.log`. A
room that exits is restarted with backoff: 1 s, doubling, to 60 s; a room
that stayed up a full minute starts again from 1 s. While it waits the room
is `restarting`, and the menu says so. At stop, each group gets SIGTERM,
then SIGKILL after 5 s, so a room's own children go with it.

States: `starting` (running, no socket yet), `up` (running, socket
present), `restarting` (exited, waiting to start again), `stopped`.

Stdlib only.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from sessiontools.web import unixhttp
from sessiontools.web.rooms import Room

#: The first restart delay, and the cap it doubles to.
BACKOFF_FIRST = 1.0
BACKOFF_MAX = 60.0
#: A room up this long has its backoff reset.
STABLE_AFTER = 60.0
#: SIGTERM, then SIGKILL after this many seconds.
STOP_GRACE = 5.0
#: How often the watcher looks at its rooms.
POLL = 0.2


@dataclass
class _Slot:
    room: Room
    socket: Path
    log: Path
    proc: subprocess.Popen | None = None
    started_at: float = 0.0
    restart_at: float | None = None
    delay: float = 0.0
    starts: int = 0
    last_exit: int | None = None
    error: str | None = None


class Supervisor:
    """Rooms run as children in their own process groups.

    RUN_DIR holds the sockets (it must be 0700) and LOG_DIR the logs. The
    backoff, grace and poll times may be shortened by tests."""

    def __init__(self, rooms: Iterable[Room], run_dir: Path, log_dir: Path, *,
                 backoff_first: float = BACKOFF_FIRST, backoff_max: float = BACKOFF_MAX,
                 stable_after: float = STABLE_AFTER, grace: float = STOP_GRACE,
                 poll: float = POLL, clock: Callable[[], float] = time.monotonic) -> None:
        self.run_dir = Path(run_dir)
        self.log_dir = Path(log_dir)
        self.backoff_first, self.backoff_max = backoff_first, backoff_max
        self.stable_after, self.grace, self.poll = stable_after, grace, poll
        self.clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._slots: dict[str, _Slot] = {}
        for room in rooms:
            sock = self.run_dir / f"{room.name}.sock"
            unixhttp.check_path(sock)
            self._slots[room.name] = _Slot(room=room, socket=sock,
                                           log=self.log_dir / f"{room.name}.log")

    # --- one room -----------------------------------------------------------

    def _spawn(self, slot: _Slot) -> None:
        """Start SLOT's room. A command that cannot be started (its venv was
        removed since discovery, say) is that room's failure alone: it is
        logged, the room is `restarting` with backoff, and every other room
        and the watcher go on."""
        slot.restart_at = None
        with open(slot.log, "ab") as log:
            try:
                slot.proc = subprocess.Popen(slot.room.argv(slot.socket),
                                             stdin=subprocess.DEVNULL, stdout=log,
                                             stderr=subprocess.STDOUT,
                                             start_new_session=True, close_fds=True)
            except OSError as e:
                slot.proc = None
                slot.last_exit = None
                slot.error = f"could not start {slot.room.command[0]!r}: {e.strerror}"
                log.write(f"session web: {slot.error}\n".encode())
                self._backoff(slot, ran=0.0)
                return
        slot.error = None
        slot.started_at = self.clock()
        slot.starts += 1

    def _backoff(self, slot: _Slot, *, ran: float) -> None:
        """Schedule SLOT's next start: 1 s, doubling to the cap, from 1 s
        again after a run of STABLE_AFTER."""
        if ran >= self.stable_after or slot.delay == 0:
            slot.delay = self.backoff_first
        else:
            slot.delay = min(slot.delay * 2, self.backoff_max)
        slot.restart_at = self.clock() + slot.delay

    def _reap(self, slot: _Slot) -> None:
        """SLOT's room has exited: schedule its restart."""
        assert slot.proc is not None
        slot.last_exit = slot.proc.returncode
        _killpg(slot.proc.pid, signal.SIGKILL)  # anything it left in its group
        slot.proc = None
        self._backoff(slot, ran=self.clock() - slot.started_at)
        try:  # a crashed room leaves its socket: it is not `up` until a new one binds
            slot.socket.unlink()
        except FileNotFoundError:
            pass

    def _tick(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            now = self.clock()
            for slot in self._slots.values():
                if slot.proc is not None and slot.proc.poll() is not None:
                    self._reap(slot)
                if slot.proc is None and slot.restart_at is not None and now >= slot.restart_at:
                    self._spawn(slot)

    def _watch(self) -> None:
        while not self._stop.wait(self.poll):
            self._tick()

    # --- the whole web --------------------------------------------------------

    def start(self) -> None:
        """Start every room and the watcher."""
        unixhttp.private_dir(self.run_dir)
        self.log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock:
            for slot in self._slots.values():
                self._spawn(slot)
        self._thread = threading.Thread(target=self._watch, name="web-supervisor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the watcher, then every room by process group: SIGTERM, and
        SIGKILL to any group still alive after the grace."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            procs = [s.proc for s in self._slots.values() if s.proc is not None]
            for p in procs:
                _killpg(p.pid, signal.SIGTERM)
            deadline = time.monotonic() + self.grace
            for p in procs:
                try:
                    p.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            for p in procs:
                _killpg(p.pid, signal.SIGKILL)  # the leader and anything left in its group
                p.wait()
            for s in self._slots.values():
                s.proc = None
                s.restart_at = None
                try:
                    s.socket.unlink()
                except FileNotFoundError:
                    pass

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # --- what the menu reads --------------------------------------------------

    def socket(self, name: str) -> Path:
        return self._slots[name].socket

    def state(self, name: str) -> str:
        with self._lock:
            slot = self._slots[name]
            if self._stop.is_set() and slot.proc is None:
                return "stopped"
            if slot.proc is None or slot.proc.poll() is not None:
                return "restarting"
            return "up" if slot.socket.exists() else "starting"

    def info(self, name: str) -> dict[str, object]:
        with self._lock:
            slot = self._slots[name]
            return {"state": self.state(name), "pid": slot.proc.pid if slot.proc else None,
                    "starts": slot.starts, "last_exit": slot.last_exit, "delay": slot.delay,
                    "error": slot.error, "log": str(slot.log)}


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
