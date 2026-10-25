"""The process table, read once: every pid's parent and start time.

Liveness is one rule, the one `session list` always used (and sessionmail's
doorbell uses since 2030-06-09): a sidecar's session is live when its pid is
running AND that process started within SAME_PROCESS_SECONDS of the
sidecar's `procStart`. A pid the OS has handed to something else is not the
session. `procStart` comes in two encodings (`parse_start`): on macOS it is
`ps lstart` in UTC ctime, so `ps` is asked in UTC; on Linux it is the
process's start in clock ticks since boot, which becomes a wall-clock time
by the rule `ps` itself uses for `lstart`.

`table(commands=True)` adds each process's command line: the pane
signal sorts a seat's tool shells by what runs under them. Callers that do
not ask get the table they always got, with `command` None.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

CTIME = "%a %b %d %H:%M:%S %Y"
SAME_PROCESS_SECONDS = 2.0


def parse_ctime(text: str | None) -> float | None:
    """UTC ctime -> epoch seconds; None when it is not one. A one-digit day
    is space-padded ("Sep  8"), so runs of spaces are folded first."""
    if not text:
        return None
    try:
        when = datetime.strptime(" ".join(text.split()) + " +0000", "%a %b %d %H:%M:%S %Y %z")
    except ValueError:
        return None
    return when.timestamp()


#: Linux's kernel statistics, whose `btime` line is the boot time in epoch
#: seconds. A module constant so a test can point it at a file of its own.
PROC_STAT = "/proc/stat"

#: Clock ticks per second when the system cannot say. Linux reports process
#: start times in USER_HZ, 100 on every mainstream architecture.
DEFAULT_CLOCK_TICKS = 100


def boot_time() -> int | None:
    """The `btime` line of PROC_STAT: when the system booted, in epoch
    seconds. None where there is no such file (macOS) or no such line."""
    try:
        with open(PROC_STAT, encoding="ascii", errors="replace") as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name == "btime":
                    return int(value) if value.strip().isdigit() else None
    except OSError:
        return None
    return None


def clock_ticks() -> int:
    """Clock ticks per second (`SC_CLK_TCK`), DEFAULT_CLOCK_TICKS when the
    system cannot say."""
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return DEFAULT_CLOCK_TICKS
    return hz if hz > 0 else DEFAULT_CLOCK_TICKS


def parse_start(text: str | None) -> float | None:
    """A sidecar's `procStart` -> epoch seconds; None when it is not one.

    Two encodings, told apart by their shape, never by the platform:
    - UTC ctime (`Sun Jun  9 17:00:00 2030`), what Claude Code writes on
      macOS: `parse_ctime`.
    - Bare digits (`88300`), what it writes on Linux: the process's start
      in clock ticks since boot (field 22 of `/proc/<pid>/stat`). It
      becomes `btime + ticks // HZ`, the rule procps uses to print
      `lstart`, so it lands on the second the process table shows. With no
      boot time to add (no PROC_STAT) it is None: not provably live.
      The kernel prints starttime as an unsigned 64-bit number, at most 20
      digits; a longer run of digits is not one, and is None too."""
    if text and text.isascii() and text.isdigit():
        if len(text) > 20:
            return None
        boot = boot_time()
        return None if boot is None else float(boot + int(text) // clock_ticks())
    return parse_ctime(text)


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    started: float | None
    #: The command line as `ps` prints it (argv joined by spaces), only when
    #: the table was read with `commands=True`; None means "not read".
    command: str | None = None


#: `lstart` in the C locale is always five words: `Sun Jun  9 17:00:00 2030`
#: (a one-digit day is padded, and `split` folds the padding).
LSTART_WORDS = 5


def table(commands: bool = False) -> dict[int, Proc]:
    """pid -> Proc, from ONE `ps`. Empty when ps cannot be run: no process
    is then provably live, which callers read as "none", never as an error.

    COMMANDS adds the `command=` column, last, after the five words of
    `lstart`; a process whose command `ps` prints empty gets "". Without it
    the table is exactly the one every caller always read."""
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    fields = "pid=,ppid=,lstart=,command=" if commands else "pid=,ppid=,lstart="
    try:
        out = subprocess.run(["ps", "-axo", fields],
                             capture_output=True, text=True, timeout=5, env=env, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_table(out, commands)


def parse_table(out: str, commands: bool = False) -> dict[int, Proc]:
    """`ps` output -> pid -> Proc (the half of `table` with no spawn)."""
    procs: dict[int, Proc] = {}
    for line in out.splitlines():
        if commands:
            parts = line.split(None, 2 + LSTART_WORDS)
            if len(parts) < 2 + LSTART_WORDS:
                continue
            started = " ".join(parts[2:2 + LSTART_WORDS])
            command: str | None = parts[2 + LSTART_WORDS] if len(parts) > 2 + LSTART_WORDS else ""
        else:
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            started, command = parts[2], None
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        procs[pid] = Proc(pid, ppid, parse_ctime(started), command)
    return procs


def children(procs: dict[int, Proc]) -> dict[int, list[int]]:
    """ppid -> its children's pids, each list in pid order."""
    out: dict[int, list[int]] = {}
    for proc in sorted(procs.values(), key=lambda p: p.pid):
        out.setdefault(proc.ppid, []).append(proc.pid)
    return out


def same_process(pid: int | None, proc_start: str | None, procs: dict[int, Proc]) -> bool:
    """True when PID is running and started when the sidecar says it did."""
    proc = procs.get(pid) if pid is not None else None
    if proc is None or proc.started is None:
        return False
    claimed = parse_start(proc_start)
    return claimed is not None and abs(proc.started - claimed) <= SAME_PROCESS_SECONDS


def ancestors(pid: int | None, procs: dict[int, Proc]) -> Iterator[int]:
    """PID, its parent, its grandparent ... while the table knows them."""
    seen: set[int] = set()
    while pid and pid in procs and pid not in seen:
        yield pid
        seen.add(pid)
        pid = procs[pid].ppid
