"""The process table, read once: every pid's parent and start time.

Liveness is one rule, the one `session list` always used (and sessionmail's
doorbell uses since 2030-06-09): a sidecar's session is live when its pid is
running AND that process started within SAME_PROCESS_SECONDS of the
sidecar's `procStart`. A pid the OS has handed to something else is not the
session. `ps` is asked in UTC because `procStart` is UTC ctime.

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
    claimed = parse_ctime(proc_start)
    return claimed is not None and abs(proc.started - claimed) <= SAME_PROCESS_SECONDS


def ancestors(pid: int | None, procs: dict[int, Proc]) -> Iterator[int]:
    """PID, its parent, its grandparent ... while the table knows them."""
    seen: set[int] = set()
    while pid and pid in procs and pid not in seen:
        yield pid
        seen.add(pid)
        pid = procs[pid].ppid
