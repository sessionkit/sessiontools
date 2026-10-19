"""The pane signal's publisher and its settings.

The reading itself is the driver's (`claude_code.pane_reading`). This module
puts it where tmux can show it with no spawn per redraw: a pane user option,
`@session_state` (BUSY, FREE or UNKNOWN), with `@session_state_why` beside
it. A display reads `#{@session_state}` from tmux's own memory. An UNSET
option means UNKNOWN, so a display shows FREE only on the exact word.

ONE PUBLISHER PER HOST (`watch`): an flock on
`$SESSION_STATE_DIR/pane-state/watch.lock`. A second `watch` names the
holder and exits 0. Each tick (`pane_state.interval`, 1 s by default):

1. stat every sidecar, in-process. A file that is new, gone or whose
   (mtime_ns, size) moved makes the tick dirty;
2. read the process table, ONE `ps` with commands, only when the tick is
   dirty, a seat read BUSY by WORK last time (a background job can exit with
   no sidecar write), or SAFETY_SECONDS have passed. The sidecars are read
   with that same table (`claude_code.sessions(env, procs=table)`), so a
   busy sidecar with a turn mark never runs a second, uncounted `ps`;
3. locate each live seat's pane (`tmux.locate`), cached by (pid,
   procStart). The pane index (one `list-panes` per live server) is read
   only when a seat is new or a write failed. The sidecar's `tmux` field is a
   cross-check, never the address: it names the pane, not the server;
4. write only what changed, one `tmux` call per server per changed tick. A
   seat that is gone, or whose process is dead, gets its options UNSET.

At start it reads what every pane already holds (one `list-panes` per
server) and rewrites all of it, so a publisher that was SIGKILLed is
corrected, and a FREE left on a pane with no seat is cleared. On SIGTERM or
SIGINT it unsets every option it wrote: a stopped publisher leaves UNKNOWN
behind, never a stale FREE. A DRY RUN takes no lock and writes nothing.

`show` reads every seat once and writes nothing. `main` is the verb,
`session pane-state show|watch` (docs/pane-state-signal.md).

THE SETTINGS, in the `session` tool's settings (`~/.sessionkit/tools/
settings/session.json`), read through `sessiontools.settings` the way `operator.py`
reads its block:

    {"pane_state": {"listeners": ["pager listen"], "interval": 1.0}}

The listener list is a TRUST list: anything on it can make a
working seat read FREE. So it fails NARROW. An unreadable or misshapen
file, or a `listeners` that is not a list, reads as the default (`pager
listen` only). A list replaces the default exactly, with its non-string and
blank members dropped. An empty list means no listeners at all: every tool
shell is then work, the safe side.
"""

from __future__ import annotations

import datetime
import errno
import fcntl
import math
import os
import signal
import socket as _socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from sessiontools.claude_code import (
    DEFAULT_LISTENERS,
    PANE_BUSY,
    PANE_FREE,
    PANE_UNKNOWN,
    Reading,
)
from sessiontools.verbs import Group

#: The `session` tool's settings (`~/.sessionkit/tools/settings/session.json`).
SETTINGS_TOOL = "session"
SETTINGS_BLOCK = "pane_state"
LISTENERS_KEY = "listeners"
INTERVAL_KEY = "interval"

DEFAULT_INTERVAL = 1.0
#: A process-table read happens at least this often, whatever else is true:
#: a seat that died with no sidecar write is seen within this.
SAFETY_SECONDS = 30.0

#: The pane user options a display reads.
STATE_KEY = "@session_state"
WHY_KEY = "@session_state_why"

LOCK_DIR = "pane-state"
LOCK_NAME = "watch.lock"

#: Which reading wins when two seats share a pane: the safe side first.
_RANK = {PANE_BUSY: 0, PANE_UNKNOWN: 1, PANE_FREE: 2}


# --- the settings ---------------------------------------------------------------------

def _block(environ: Mapping[str, str]) -> Mapping:
    """The `pane_state` block of the `session` tool's template and host
    layers, or {}. Nothing a settings file holds raises out of here."""
    from sessiontools import settings
    try:
        merged, _o, _l, _v = settings.merge(
            settings.sources(SETTINGS_TOOL, None, env=environ))
        block = merged.get(SETTINGS_BLOCK)
    except Exception:  # noqa: BLE001 - a bad file reads as nothing configured
        return {}
    return block if isinstance(block, Mapping) else {}


def listeners(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The listener list: `pane_state.listeners` when it is a list (strings
    stripped, blanks and non-strings dropped), else DEFAULT_LISTENERS."""
    environ = os.environ if environ is None else environ
    value = _block(environ).get(LISTENERS_KEY)
    if not isinstance(value, list):
        return DEFAULT_LISTENERS
    return tuple(" ".join(v.split()) for v in value if isinstance(v, str) and v.split())


def interval(environ: Mapping[str, str] | None = None) -> float:
    """Seconds between ticks: `pane_state.interval` when it is a finite
    number above 0, else DEFAULT_INTERVAL. A boolean is not a number."""
    environ = os.environ if environ is None else environ
    value = _block(environ).get(INTERVAL_KEY)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_INTERVAL
    return float(value) if math.isfinite(value) and value > 0 else DEFAULT_INTERVAL


# --- the publisher ----------------------------------------------------------------------

Place = tuple[str, str]          # (socket, pane_id)


@dataclass
class Stats:
    """What the publisher spent: ticks, `ps` runs, tmux runs (index reads,
    option reads and writes alike), options written or unset, and the
    located panes whose sidecar `tmux` field named another pane."""

    ticks: int = 0
    ps_runs: int = 0
    tmux_runs: int = 0
    writes: int = 0
    mismatches: int = 0

    def line(self) -> str:
        return (f"ticks={self.ticks} ps={self.ps_runs} tmux={self.tmux_runs} "
                f"writes={self.writes} mismatches={self.mismatches}")


@dataclass(frozen=True)
class Transition:
    """One pane's change: OLD -> NEW, where None is unset (UNKNOWN)."""

    socket: str
    pane: str
    old: Reading | None
    new: Reading | None

    @property
    def place(self) -> Place:
        return (self.socket, self.pane)

    def line(self) -> str:
        old = self.old.word if self.old else "unset"
        new = f"{self.new.word} ({self.new.why})" if self.new else "unset"
        return f"{self.socket} {self.pane} {old} -> {new}"


#: Marks a value found on a pane at start: it never equals a real reading,
#: so the first tick rewrites (or unsets) every one of them.
_FOUND = "\0found at start"


def _safer(a: Reading | None, b: Reading) -> Reading:
    return b if a is None or _RANK.get(b.word, 1) < _RANK.get(a.word, 1) else a


def _commands_table() -> dict:
    from sessiontools import procs
    return procs.table(commands=True)


class Publisher:
    """The tick loop's state. Every outside call is injectable, so a test
    can count spawns: PS reads the table, SOCKETS lists live servers (no
    spawn), PANES reads one server's index, READ_OPTION one server's
    option values, WRITE one batched `tmux.set_options`. DRY_RUN computes
    and reports every transition and writes nothing."""

    def __init__(self, env: Mapping[str, str] | None = None,
                 listeners_: tuple[str, ...] | list[str] | None = None, *,
                 dry_run: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 ps: Callable[[], dict] | None = None,
                 sockets: Callable[[], list[str]] | None = None,
                 panes: Callable[[str], dict[int, str]] | None = None,
                 read_option: Callable[[str, str], dict[str, str] | None] | None = None,
                 write: Callable[[str, list[tuple[str, str, str | None]]], bool] | None = None):
        from sessiontools import tmux
        self.env: dict[str, str] = dict(os.environ if env is None else env)
        self.listeners = listeners(self.env) if listeners_ is None else tuple(listeners_)
        self.dry_run = dry_run
        self.stats = Stats()
        #: What tmux holds, as this publisher last wrote or found it.
        self.current: dict[Place, Reading] = {}
        self._clock = clock
        self._ps = ps or _commands_table
        self._sockets = sockets or tmux.sockets
        self._panes = panes or tmux.panes
        self._read_option = read_option or tmux.pane_option
        self._write = write or tmux.set_options
        self._stamps: dict[str, tuple[int, int]] | None = None
        self._last_ps: float | None = None
        self._work_live = False
        self._force = True
        self._places: dict[tuple[int | None, str | None], Place | None] = {}
        self._index: dict[int, Place] | None = None
        #: The last index read had a server answer with no panes (a live
        #: server always has one, so that read failed, e.g. timed out).
        self._index_partial = False
        #: Seats found in no pane by such a read: looked for again at the
        #: next safety read, never every tick (a seat outside tmux is common).
        self._unplaced: set[tuple[int | None, str | None]] = set()

    # -- start -------------------------------------------------------------------------

    def seed(self) -> int:
        """Read what every pane already holds, so the first tick rewrites or
        unsets all of it (a SIGKILLed predecessor's words included). The
        number of panes found holding a value."""
        found = 0
        for sock in self._sockets():
            self.stats.tmux_runs += 1
            values = self._read_option(sock, STATE_KEY) or {}
            for pane, word in values.items():
                self.current[(sock, pane)] = Reading(word, _FOUND)
                found += 1
        self._force = True
        return found

    # -- one tick ----------------------------------------------------------------------

    def _stat(self) -> dict[str, tuple[int, int]]:
        from sessiontools.claude_code import sidecar_dir
        stamps: dict[str, tuple[int, int]] = {}
        try:
            entries = os.scandir(sidecar_dir(self.env))
        except OSError:
            return stamps
        with entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                stamps[entry.name] = (st.st_mtime_ns, st.st_size)
        return stamps

    def _read_index(self) -> dict[int, Place]:
        index: dict[int, Place] = {}
        partial = False
        for sock in self._sockets():
            self.stats.tmux_runs += 1
            found = self._panes(sock)
            partial = partial or not found
            for pid, pane in found.items():
                index.setdefault(pid, (sock, pane))
        self._index_partial = partial
        return index

    def _place(self, s, table: dict) -> Place | None:
        from sessiontools import tmux
        key = (s.pid, s.proc_start)
        if key in self._places:
            place = self._places[key]
        else:
            if self._index is None:
                self._index = self._read_index()
            place = tmux.locate(s.pid, table, self._index)
            self._places[key] = place
            if place is None and self._index_partial:
                self._unplaced.add(key)
        if place is not None and s.tmux and not s.tmux.endswith("." + place[1]):
            self.stats.mismatches += 1
        return place

    def tick(self) -> list[Transition]:
        """One tick: the transitions it applied (or, dry, would apply)."""
        from sessiontools import claude_code, procs
        self.stats.ticks += 1
        now = self._clock()
        stamps = self._stat()
        dirty = stamps != self._stamps
        self._stamps = stamps
        due = self._last_ps is None or now - self._last_ps >= SAFETY_SECONDS
        if not (dirty or due or self._work_live or self._force):
            return []
        self._force = False
        if due:
            # A seat an incomplete index placed nowhere is looked for again.
            for key in self._unplaced:
                self._places.pop(key, None)
            self._unplaced.clear()
        table = self._ps()
        self.stats.ps_runs += 1
        self._last_ps = now
        if not table:
            self._force = True          # no table proves nothing live: try again next tick
        self._index = None
        desired: dict[Place, Reading] = {}
        work = False
        live_keys = set()
        for s in claude_code.sessions(self.env, procs=table):
            if not procs.same_process(s.pid, s.proc_start, table):
                continue                # gone: its pane, if we wrote one, is unset below
            live_keys.add((s.pid, s.proc_start))
            place = self._place(s, table)
            if place is None:
                continue                # not in tmux: nothing to write
            reading = claude_code.pane_reading(s, table, self.listeners)
            if reading.word == PANE_BUSY and reading.why.startswith("work:"):
                work = True
            desired[place] = _safer(desired.get(place), reading)
        for key in [k for k in self._places if k not in live_keys]:
            del self._places[key]
        self._unplaced &= live_keys
        self._work_live = work
        return self._apply(desired)

    # -- writing -----------------------------------------------------------------------

    @staticmethod
    def _items(t: Transition) -> list[tuple[str, str, str | None]]:
        if t.new is None:
            return [(t.pane, STATE_KEY, None), (t.pane, WHY_KEY, None)]
        items: list[tuple[str, str, str | None]] = []
        if t.old is None or t.old.word != t.new.word:
            items.append((t.pane, STATE_KEY, t.new.word))
        if t.old is None or t.old.why != t.new.why:
            items.append((t.pane, WHY_KEY, t.new.why))
        return items

    def _commit(self, t: Transition, written: int) -> None:
        if t.new is None:
            self.current.pop(t.place, None)
        else:
            self.current[t.place] = t.new
        self.stats.writes += written

    def _apply(self, desired: dict[Place, Reading]) -> list[Transition]:
        changes = [Transition(*place, self.current.get(place), r)
                   for place, r in desired.items() if self.current.get(place) != r]
        changes += [Transition(*place, old, None)
                    for place, old in self.current.items() if place not in desired]
        if self.dry_run:
            for t in changes:
                self._commit(t, 0)
            return changes
        return self._send(changes)

    def _send(self, changes: list[Transition]) -> list[Transition]:
        by_socket: dict[str, list[Transition]] = {}
        for t in changes:
            by_socket.setdefault(t.socket, []).append(t)
        applied: list[Transition] = []
        for sock, ts in by_socket.items():
            items = [i for t in ts for i in self._items(t)]
            self.stats.tmux_runs += 1
            if self._write(sock, items):
                for t in ts:
                    self._commit(t, len(self._items(t)))
                applied += ts
                continue
            # tmux stops a batch at its first failure (a pane that is gone):
            # find the panes again next tick, and retry this batch pane by pane.
            self._places.clear()
            self._force = True
            for t in ts:
                one = self._items(t)
                self.stats.tmux_runs += 1
                if self._write(sock, one):
                    self._commit(t, len(one))
                    applied.append(t)
                else:
                    # gone (nothing left to unset), or in doubt (rewritten next tick)
                    self.current.pop(t.place, None)
        return applied

    def clear(self) -> list[Transition]:
        """Unset every option this publisher holds: UNKNOWN, never stale."""
        changes = [Transition(*place, old, None) for place, old in self.current.items()]
        if self.dry_run:
            self.current.clear()
            return changes
        applied = self._send(changes)
        self.current.clear()
        return applied


# --- the lock and the loop ---------------------------------------------------------------

def lock_path(env: Mapping[str, str] | None = None) -> Path:
    """`<state dir>/pane-state/watch.lock` (the state dir is snapshot.py's)."""
    from sessiontools.snapshot import state_dir
    return state_dir(env) / LOCK_DIR / LOCK_NAME


def acquire(path: Path) -> int | None:
    """Take PATH's flock without waiting and write who holds it; the open
    descriptor, or None when another process holds it. Closing the
    descriptor (or dying, even by SIGKILL) drops it. OSError when the file
    cannot be opened at all."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    started = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.ftruncate(fd, 0)
    os.write(fd, f"pid {os.getpid()} on {_socket.gethostname()} since {started}\n".encode())
    return fd


def holder(path: Path) -> str:
    """Who holds PATH's lock, as the holder wrote it; "unknown" if unreadable."""
    try:
        return path.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _stamp() -> str:
    now = time.time()
    clock = datetime.datetime.fromtimestamp(now, datetime.UTC).astimezone().strftime("%H:%M:%S.%f")[:-3]
    return f"{now:.3f} {clock}"


def watch(env: Mapping[str, str] | None = None, *, period: float | None = None,
          once: bool = False, dry_run: bool = False, verbose: bool = False,
          stats: bool = False, out: TextIO | None = None, err: TextIO | None = None,
          stop: threading.Event | None = None,
          publisher: Publisher | None = None) -> int:
    """The publisher, until SIGTERM/SIGINT or STOP (or one tick with ONCE).
    Exit 0, including when another publisher holds the lock (it is named on
    ERR); 1 when the lock file cannot be opened. On the way out it unsets
    every option it wrote, except after ONCE: a one-shot write was asked
    for, so it stands (and nothing keeps it current). DRY_RUN never writes tmux
    and logs every transition on OUT; VERBOSE logs them while writing.
    STATS prints the spawn counters on OUT at the end.

    DRY_RUN takes NO lock and creates no file: it is an observer, so it runs
    beside a live publisher, never keeps one from starting, and leaves a real
    host's state dir as it found it."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    environ = os.environ if env is None else env
    fd: int | None = None
    if not dry_run:
        path = lock_path(environ)
        try:
            fd = acquire(path)
        except OSError as e:
            print(f"pane-state watch: cannot open the lock {path}: {e}", file=err)
            return 1
        if fd is None:
            print(f"pane-state watch: another publisher holds {path}: {holder(path)}", file=err)
            return 0
    stop = threading.Event() if stop is None else stop
    previous: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    pub = publisher or Publisher(environ, dry_run=dry_run)
    wait = interval(environ) if period is None else period
    log = dry_run or verbose
    try:
        pub.seed()
        while True:
            try:
                for t in pub.tick():
                    if log:
                        print(f"{_stamp()} {t.line()}", file=out, flush=True)
            except Exception as e:  # noqa: BLE001 - one bad tick never stops the publisher
                print(f"pane-state watch: tick failed: {e!r}", file=err, flush=True)
                pub._force = True
            if once or stop.wait(wait):
                break
    finally:
        for t in ([] if once else pub.clear()):
            if log:
                print(f"{_stamp()} {t.line()}", file=out, flush=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if fd is not None:
            os.close(fd)
        if stats:
            print(pub.stats.line(), file=out, flush=True)
    return 0


# --- show: read once, write nothing -------------------------------------------------------

@dataclass(frozen=True)
class Row:
    """One seat as `show` reads it: where it sits, the reading the publisher
    would write, and the word its pane holds now (None: unset, UNKNOWN)."""

    pid: int | None
    session_id: str
    socket: str | None
    pane: str | None
    word: str
    why: str
    published: str | None

    def as_json(self) -> dict:
        return {"pid": self.pid, "session_id": self.session_id, "socket": self.socket,
                "pane": self.pane, "state": self.word, "why": self.why,
                "published": self.published}


def show(env: Mapping[str, str] | None = None,
         listeners_: tuple[str, ...] | list[str] | None = None, *,
         ps: Callable[[], dict] | None = None,
         sockets: Callable[[], list[str]] | None = None,
         panes: Callable[[str], dict[int, str]] | None = None,
         read_option: Callable[[str, str], dict[str, str] | None] | None = None) -> list[Row]:
    """Every seat with a sidecar, read ONCE: its pane, its reading and what
    the pane holds now. It writes nothing - no tmux option, no lock, no
    file - so it is safe on a real host beside a live publisher. Costs one
    `ps` and two `list-panes` per live server."""
    from sessiontools import claude_code, tmux
    environ = dict(os.environ if env is None else env)
    names = listeners(environ) if listeners_ is None else tuple(listeners_)
    table = (ps or _commands_table)()
    index: dict[int, Place] = {}
    held: dict[Place, str] = {}
    for sock in (sockets or tmux.sockets)():
        for pid, pane in (panes or tmux.panes)(sock).items():
            index.setdefault(pid, (sock, pane))
        for pane, word in ((read_option or tmux.pane_option)(sock, STATE_KEY) or {}).items():
            held[(sock, pane)] = word
    rows = []
    for s in claude_code.sessions(environ, procs=table):
        place = tmux.locate(s.pid, table, index) if table else None
        reading = claude_code.pane_reading(s, table, names)
        rows.append(Row(s.pid, s.id, place[0] if place else None, place[1] if place else None,
                        reading.word, reading.why, held.get(place) if place else None))
    return sorted(rows, key=_row_order)


def _row_order(r: Row) -> tuple:
    """By server, then pane number (%2 before %10); seats in no pane last."""
    number = int(r.pane[1:]) if r.pane and r.pane[1:].isdigit() else -1
    return (r.pane is None, r.socket or "", number, r.pane or "", r.pid or 0)


def render(rows: list[Row]) -> str:
    """`show`'s table. A seat in no pane is `-`; an unset option is `-`."""
    head = ("PANE", "STATE", "PUBLISHED", "PID", "WHY")
    body = [(f"{r.socket}:{r.pane}" if r.pane else "-", r.word, r.published or "-",
             str(r.pid or "-"), r.why) for r in rows]
    widths = [max(len(row[i]) for row in [head, *body]) for i in range(4)]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row[:4], widths)) + "  " + row[4]
                     for row in [head, *body])


# --- the verb: `session pane-state show|watch` ---------------------------------------------

PANE_STATE_EPILOG = """\
A display reads the signal from tmux's own memory, with no spawn:
  #{@session_state}       BUSY, FREE, or unset (UNKNOWN)
  #{@session_state_why}   the reason; raw command text, never put it in a
                          status format unescaped
A display shows FREE only on the exact word FREE: unset is UNKNOWN.

On a real host a per-user launchd agent runs the publisher, one per host:
`session pane-state agent on|off|status` (`agent --help` says more). See
docs/pane-state-signal.md."""


def _positive(text: str) -> float:
    import argparse
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number of seconds: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a number of seconds above 0, not {text!r}")
    return value


def build_parser():
    """`session pane-state`'s parser: show, watch, and a placeholder `agent`
    (help only; `GROUP` dispatches `agent` before this parser runs)."""
    import argparse
    p = argparse.ArgumentParser(prog="session pane-state", epilog=PANE_STATE_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Each seat's BUSY / FREE / UNKNOWN signal, published "
                                            "on its tmux pane as the user option "
                                            "@session_state.")
    sub = p.add_subparsers(dest="verb", metavar="show|watch|agent")
    sp = sub.add_parser("show", help="read every seat once; writes nothing",
                        description="Every seat with a sidecar: its pane, the reading, the "
                                    "reason, and what the pane holds now. Writes nothing.")
    sp.add_argument("--json", action="store_true", help="machine-readable: one JSON array")
    wp = sub.add_parser("watch", help="the publisher: one per host, until SIGTERM",
                        description="Publish every seat's reading on its pane, on change only. "
                                    "One publisher per host (a second says who holds the lock "
                                    "and exits 0). SIGTERM or SIGINT unsets every option it "
                                    "wrote, so a stopped publisher leaves UNKNOWN, never a "
                                    "stale FREE.")
    wp.add_argument("--interval", type=_positive, metavar="S",
                    help=f"seconds between ticks (default: pane_state.interval, else "
                         f"{DEFAULT_INTERVAL:g})")
    wp.add_argument("--once", action="store_true",
                    help="one tick, then exit. Its writes STAY STANDING: nothing keeps them "
                         "current and nothing unsets them")
    wp.add_argument("--dry-run", action="store_true",
                    help="log every transition; never write tmux, take no lock")
    wp.add_argument("--stats", action="store_true",
                    help="on exit, print the spawn counts: ticks, ps runs, tmux runs")
    sub.add_parser("agent", add_help=False,
                   help="on|off|status: the launchd agent that runs `watch`, one per host")
    return p


#: `session pane-state`: `agent` is dispatched by hand, to its own parser,
#: before this module's parser sees its arguments.
GROUP = Group(subs={"agent": "sessiontools.pane_state_agent:build_parser"},
              rest="sessiontools.pane_state:build_parser")


def main(argv: list[str]) -> int:
    """`session pane-state show [--json]` and `session pane-state watch
    [--interval S] [--once] [--dry-run] [--stats]`. `session pane-state agent
    on|off|status` is handed to `pane_state_agent` first, by `GROUP.subs`,
    so this parser never sees its arguments."""
    if argv[:1] and argv[0] in GROUP.subs:
        from sessiontools import pane_state_agent
        return pane_state_agent.main(argv[1:])
    import json
    p = build_parser()
    a = p.parse_args(argv)
    if a.verb == "show":
        rows = show()
        print(json.dumps([r.as_json() for r in rows]) if a.json else render(rows))
        return 0
    if a.verb == "watch":
        return watch(period=a.interval, once=a.once, dry_run=a.dry_run, stats=a.stats)
    p.print_help(sys.stderr)
    return 2
