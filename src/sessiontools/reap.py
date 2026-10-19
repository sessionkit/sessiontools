"""`session reap`: which idle seats may be closed, and why. A DRY RUN.

Reap idle agents, and be kind about it. Past an hour idle the
prompt cache is cold, so a seat that wakes pays a full context rewrite. This verb lists every live seat idle past
a limit (1h by default, `--idle` to change it), each with a verdict, CLOSE
or KEEP, and the reason. By default it is a DRY RUN and closes nothing.
`--yes` acts on the CLOSE rows, one seat at a time (`act`): every gate is
re-read at the act, the pane signal once more just before the kill, then
`close_seat` kills the seat's PANE by id (never the window), and `verify`
proves the pane, the pid and its descendants gone. A survivor is named,
never killed. One line per act goes to `reap.log` in the state dir.

THE ONE IDEA: never try to detect "waiting on the user". CLOSE
only on POSITIVE evidence, on both sides, and every unknown is KEEP:

- the SEAT: its pane signal (`claude_code.pane_reading`) reads FREE.
  BUSY (`turn`, `waiting`, `work: ...`) and UNKNOWN are KEEP, and so is a
  seat with no tmux pane, which has nothing to close by id;
- the TASK: found from the seat's cwd, it has a TERMINAL status
  (`TERMINAL`). An open task, no task, an unreadable README and a seat whose
  name names another task are all KEEP;
- the TAIL: no unsaved work, read with `git --no-optional-locks status`, in
  the task's `worktree/` and in the store that holds the task dir.

The front matter is read through `sessiontools.start.agentfile`, the one
module that imports yaml (AGENTS.md rule 3), and only when a README opens
with `---`. The CLI imports this module inside its handler only, so
`import sessiontools.cli` loads neither yaml nor jsonschema.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sessiontools.claude_code import (
    DEFAULT_LISTENERS,
    PANE_FREE,
    Reading,
    Session,
    pane_reading,
)
from sessiontools.listing import Row, fmt_duration
from sessiontools.procs import Proc, ancestors, children

#: The default idle limit: an hour.
DEFAULT_IDLE_SECONDS = 3600

CLOSE = "CLOSE"
KEEP = "KEEP"

#: taskmaster's terminal states (lifecycle.md): the only statuses that close.
TERMINAL = ("completed", "archived", "duplicate", "todont")

#: Whether "hatched" (`review` with every delivery landed) counts as done is
#: an open question. v1 ships it OFF: such a task is KEEP, and the reason says
#: so.
HATCHED_CLOSES = False

#: `--idle`: a number and a unit, s, m (MINUTES) or h. The settle hook's `m`
#: is a million TOKENS (`settle._SCALE`); this parser is reap's own.
_IDLE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh])\s*$")
_UNIT = {"s": 1, "m": 60, "h": 3600}

#: How long one `git status` may take before the tail reads unreadable.
GIT_SECONDS = 10.0
#: How many dirty paths a reason names before it says "+N more".
TAIL_PATHS = 3


def parse_idle(text: str) -> int:
    """`45m` -> 2700. `<n>s`, `<n>m` or `<n>h`, n above 0; ValueError otherwise."""
    m = _IDLE.match(text or "")
    if not m:
        raise ValueError(f"not a duration: {text!r} (use <n>s, <n>m or <n>h; m is minutes)")
    seconds = round(float(m.group(1)) * _UNIT[m.group(2)])
    if seconds <= 0:
        raise ValueError(f"the idle limit must be above 0, not {text!r}")
    return seconds


# --- a path's kind, read so an unreadable path raises ----------------------------------

#: The errors `Path.is_file()` / `is_dir()` / `exists()` read as "not there"
#: up to 3.13: no such entry, a file where a folder should be, a bad file
#: descriptor, a symlink loop. Every other OSError raised.
_ABSENT = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})


def stat_mode(path: Path) -> int | None:
    """PATH's `st_mode`, symlinks followed, or None where it is not there
    (the errors in _ABSENT, or a path the OS cannot take). Any other OSError
    RAISES, on every version. From 3.14 `Path.is_file()` / `is_dir()` /
    `exists()` return False for a path they cannot stat, so a folder that
    cannot be searched reads as an empty one. A check that must tell
    "absent" from "cannot look" asks here instead."""
    try:
        return path.stat().st_mode
    except OSError as e:
        if e.errno in _ABSENT:
            return None
        raise
    except ValueError:
        return None


# --- the task -------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    """A task found from a seat's cwd: its id, status and directory."""

    task_id: str
    status: object          # whatever the front matter says; only a TERMINAL str closes
    dir: Path
    deliveries: object = None


def find_task(cwd: str | None) -> tuple[Task | None, str | None]:
    """(task, problem). Walks up from CWD to the first directory whose
    README.md opens with front matter carrying a string `task_id`. A README
    with no front matter, or front matter with no task_id, is walked past.
    A README that opens front matter and cannot be read or parsed STOPS the
    walk with a problem: the seat is KEEP, never resolved to a parent."""
    if not cwd:
        return None, None
    here = Path(cwd)
    for d in (here, *here.parents):
        readme = d / "README.md"
        try:
            mode = stat_mode(readme)
            if mode is None or not stat.S_ISREG(mode):
                continue
            text = readme.read_text(errors="replace")
        except OSError as e:
            return None, f"task README unreadable ({readme}: {type(e).__name__})"
        if not text.startswith("---"):
            continue
        try:
            from sessiontools.start.agentfile import parse_agent_markdown
            front, _body = parse_agent_markdown(text)
        except Exception as e:  # noqa: BLE001 - yaml.YAMLError would need `import yaml`
            return None, f"task README unreadable ({readme}: {type(e).__name__})"
        task_id = front.get("task_id")
        if isinstance(task_id, str) and task_id.strip():
            return Task(task_id.strip(), front.get("status"), d, front.get("deliveries")), None
    return None, None


def hatched(task: Task) -> bool:
    """The reading of "hatched": `review`, and every delivery (at least
    one) has `stage: landed`. Closes nothing while HATCHED_CLOSES is off."""
    d = task.deliveries
    return (task.status == "review" and isinstance(d, list) and bool(d)
            and all(isinstance(e, Mapping) and e.get("stage") == "landed" for e in d))


def is_done(task: Task) -> bool:
    """DONE: a terminal status, or (only with HATCHED_CLOSES on) hatched."""
    return task.status in TERMINAL or (HATCHED_CLOSES and hatched(task))


def name_token(name: str | None) -> str | None:
    """The text between the first `+` of a seat's name and the next `.`
    (or the end): `myproject+task-7.worker.798e6e18` -> `task-7`,
    `sessionmail+super.3987c186` -> `super`. None when there is no `+`.
    Whatever it says is the token: `super` and `gc` too."""
    if not name or "+" not in name:
        return None
    return name.split("+", 1)[1].split(".", 1)[0]


# --- the tail -------------------------------------------------------------------------

Git = Callable[[list[str], Path], subprocess.CompletedProcess]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """`git --no-optional-locks ARGS` in CWD. The flag is load-bearing: a
    plain `git status` refreshes the index and writes it back under
    `index.lock` (git-status(1), BACKGROUND REFRESH), and the store is ONE
    repo every seat commits to. Every inherited `GIT_*` is
    dropped: a caller's `GIT_DIR`, `GIT_WORK_TREE` or `GIT_INDEX_FILE` would
    point the status at another repo, which could read clean."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    return subprocess.run(["git", "--no-optional-locks", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=GIT_SECONDS, check=False)


#: Each pins what `git status` would otherwise take from config, so a repo's
#: or user's setting can never HIDE unsaved work: `status.showUntrackedFiles
#: =no` drops new files from --porcelain, and `diff.ignoreSubmodules` /
#: `submodule.<name>.ignore` drop a dirty submodule. Config can cost a close,
#: never cause one.
STATUS_ARGS = ("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none")


#: A part of the tail is one of these.
PART_CLEAN = "clean"
PART_DIRTY = "dirty"
PART_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Part:
    """One read of the tail: WHERE (`worktree`, `store`, ...), its STATE
    (PART_CLEAN, PART_DIRTY or PART_UNREADABLE) and the DETAIL that names
    the paths or the failure ("" when clean)."""

    where: str
    state: str
    detail: str = ""


def _porcelain(where: str, args: list[str], cwd: Path, git: Git) -> Part:
    """One `git status --porcelain`, as a Part."""
    try:
        done = git([*STATUS_ARGS, *args], cwd)
    except (OSError, subprocess.SubprocessError) as e:
        return Part(where, PART_UNREADABLE, f"{where} unreadable ({type(e).__name__})")
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()[:1]
        return Part(where, PART_UNREADABLE,
                    f"{where} unreadable (git exit {done.returncode}{': ' + first[0] if first else ''})")
    lines = [ln for ln in (done.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return Part(where, PART_CLEAN)
    shown = ", ".join(" ".join(ln.split()) for ln in lines[:TAIL_PATHS])
    more = f" +{len(lines) - TAIL_PATHS} more" if len(lines) > TAIL_PATHS else ""
    return Part(where, PART_DIRTY, f"{where} dirty: {shown}{more}")


def _own_repo(worktree: Path, git: Git) -> tuple[bool, str]:
    """(True, "") when WORKTREE is the top of a repo of its own. A
    `worktree/` with no `.git` is NOT: git walks up to the store, which
    ignores `**/worktree/`, so its unsaved files would read clean. Not its own repo is unreadable, so KEEP."""
    try:
        done = git(["rev-parse", "--show-toplevel"], worktree)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"worktree unreadable ({type(e).__name__})"
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()[:1]
        return False, f"worktree unreadable (git exit {done.returncode}{': ' + first[0] if first else ''})"
    top = (done.stdout or "").strip()
    try:
        same = bool(top) and Path(top).resolve() == worktree.resolve()
    except OSError:
        same = False
    if not same:
        return False, f"worktree unreadable (not a repo of its own: git's top is {top or 'unknown'})"
    return True, ""


def tail_parts(task_dir: Path, git: Git = _git, label: str = "store") -> list[Part]:
    """Each read of the tail at TASK_DIR, as a Part: its `worktree/` (when it
    has one, and only as a repo of its own), then the repo that holds
    TASK_DIR at `-- .`, named LABEL. Takes no lock and writes nothing."""
    parts = []
    worktree = task_dir / "worktree"
    if worktree.is_dir():
        own, detail = _own_repo(worktree, git)
        parts.append(_porcelain("worktree", [], worktree, git) if own
                     else Part("worktree", PART_UNREADABLE, detail))
    parts.append(_porcelain(label, ["--", "."], task_dir, git))
    return parts


def tail(task_dir: Path, git: Git = _git) -> tuple[bool, str]:
    """(clean, detail): no unsaved work in the task's `worktree/` (if it has
    one) or in the store that holds TASK_DIR. Dirty or unreadable is not
    clean, and the detail names the paths or the failure. Takes no lock."""
    problems = [p.detail for p in tail_parts(task_dir, git) if p.state != PART_CLEAN]
    return (not problems), "; ".join(problems)


# --- the verdict ----------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    row: Row
    idle_s: int
    verdict: str            # CLOSE or KEEP
    why: str
    reading: Reading | None = None
    task: Task | None = None

    def to_dict(self) -> dict:
        r, s = self.row, self.row.session
        return {
            "session_id": s.id,
            "short": r.short,
            "name": r.name,
            "pid": s.pid,
            "cwd": s.cwd,
            "tmux_socket": r.tmux_socket,
            "tmux_pane": r.tmux_pane,
            "idle_s": self.idle_s,
            "verdict": self.verdict,
            "why": self.why,
            "pane_signal": ({"word": self.reading.word, "why": self.reading.why}
                            if self.reading else None),
            "task_id": self.task.task_id if self.task else None,
            "task_status": self.task.status if self.task else None,
            "task_dir": str(self.task.dir) if self.task else None,
        }


def judge(row: Row, table: dict, now: float,
          listeners: object = DEFAULT_LISTENERS,
          tail_check: Callable[[Path], tuple[bool, str]] | None = None,
          shared: tuple[str, ...] = ()) -> Verdict:
    """One listed seat's verdict. Each gate can only KEEP; CLOSE is what is
    left when every gate has positive evidence. SHARED
    names the other live seats in this seat's pane: a pane kill takes every
    process in it, so a shared pane is KEEP."""
    s = row.session
    idle = int(s.idle_seconds(now))

    def keep(why: str, reading: Reading | None = None, task: Task | None = None) -> Verdict:
        return Verdict(row, idle, KEEP, why, reading, task)

    if not row.tmux_pane or not row.tmux_socket:
        return keep("no pane (nothing to close by id)")
    if shared:
        return keep(f"pane shared with {', '.join(shared)}")
    reading = pane_reading(s, table, listeners)
    if reading.word != PANE_FREE:
        return keep(f"seat {reading.word} ({reading.why})", reading)
    task, problem = find_task(s.cwd)
    if problem:
        return keep(problem, reading)
    if task is None:
        return keep("no task (a seat with no task is never closed)", reading)
    token = name_token(s.name)
    if token is not None and token.lower() != task.task_id.lower():
        return keep(f"name says {token}, cwd says {task.task_id}", reading, task)
    if not is_done(task):
        status = "no status" if task.status in (None, "") else str(task.status)
        note = "; hatched does not count as done yet" if hatched(task) else ""
        return keep(f"task {task.task_id} is {status} (not done{note})", reading, task)
    clean, detail = (tail_check or tail)(task.dir)
    if not clean:
        return keep(f"task {task.task_id} is {task.status}; {detail}", reading, task)
    return Verdict(row, idle, CLOSE, f"task {task.task_id} is {task.status}; tail clean",
                   reading, task)


def sharers(rows: list[Row]) -> dict[str, tuple[str, ...]]:
    """session id -> the shorts of every OTHER live seat in the same pane
    (the same socket and pane id). A seat alone in its pane is absent."""
    by_pane: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        if r.tmux_socket and r.tmux_pane:
            by_pane.setdefault((r.tmux_socket, r.tmux_pane), []).append(r)
    out: dict[str, tuple[str, ...]] = {}
    for group in by_pane.values():
        if len(group) > 1:
            for r in group:
                out[r.session.id] = tuple(o.short for o in group if o is not r)
    return out


def listed(rows: list[Row], now: float, limit: int) -> list[Row]:
    """The seats idle MORE than LIMIT seconds, longest idle first. Idle is
    `Session.idle_seconds`: 0 while busy, from the turn's end for `idle
    with children`."""
    over = [r for r in rows if r.session.idle_seconds(now) > limit]
    return sorted(over, key=lambda r: r.session.idle_seconds(now), reverse=True)


def plan(rows: list[Row], table: dict, now: float, limit: int = DEFAULT_IDLE_SECONDS,
         listeners: object = DEFAULT_LISTENERS,
         tail_check: Callable[[Path], tuple[bool, str]] | None = None) -> list[Verdict]:
    """Every seat idle past LIMIT, judged. Reads only: no lock, no write. A
    pane is shared when ANY live seat sits in it, listed or not."""
    shared = sharers(rows)
    return [judge(r, table, now, listeners, tail_check, shared.get(r.session.id, ()))
            for r in listed(rows, now, limit)]


# --- the act: --yes, one seat at a time -------------------------------

#: How long the close may take to show: the pane gone and every pid dead.
VERIFY_SECONDS = 5.0
#: How often verify looks again within that bound.
VERIFY_STEP = 0.1

CLOSED = "closed"
SKIPPED = "kept"
FAILED = "failed"


def close_seat(socket: str, pane: str) -> tuple[bool, str]:
    """THE close, and the one place it happens: `kill-pane` by id on the
    seat's own server, never `kill-window` (a window can hold a sibling
    seat). It is one function on purpose, so it can be swapped for another
    tool's close. No socket is refused: `tmux.run` reads None as the
    AMBIENT server, where the same `%N` is some other pane."""
    if not socket or not pane:
        return False, "no socket or no pane: never killed"
    from sessiontools import tmux
    try:
        done = tmux.run(["kill-pane", "-t", pane], socket, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"kill-pane failed ({type(e).__name__})"
    if done.returncode != 0:
        first = (done.stderr or "").strip().splitlines()[:1]
        return False, f"kill-pane failed (exit {done.returncode}{': ' + first[0] if first else ''})"
    return True, ""


def _reread(s: Session) -> Session | None:
    """The seat's sidecar, read again."""
    from sessiontools.claude_code import find
    return find(s.id)


def _pane_pid(socket: str, pane: str) -> int | None:
    from sessiontools import tmux
    return tmux.pane_pid(socket, pane)


def _pane_tty_pids(socket: str, pane: str) -> set[int] | None:
    """Every pid whose controlling terminal is PANE's tty: the pane's own
    session, which its hangup reaches whoever their parent is. A job the
    pane's shell forked and let go (`(job &)`) has ppid 1, so it is in no
    process tree under the pane, yet kill-pane takes it. None when the tty
    or the `ps` cannot be read, which the act reads as KEEP."""
    from sessiontools import tmux
    try:
        out = tmux.run(["display-message", "-p", "-t", pane, "#{pane_tty}"], socket, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    tty = out.stdout.strip()
    if out.returncode != 0 or not tty.startswith("/dev/"):
        return None
    try:
        ps = subprocess.run(["ps", "-t", tty, "-o", "pid="], capture_output=True, text=True,
                            timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    words = ps.stdout.split()
    if not words or not all(w.isdigit() for w in words):
        return None
    return {int(w) for w in words}


_PANE_ID = re.compile(r"^%\d+$")


def _server_panes(socket: str) -> list[str] | None:
    """Every pane id on SOCKET's server (`list-panes -a`). None when tmux
    cannot be read or answers with anything but pane ids: never "no panes"."""
    from sessiontools import tmux
    try:
        out = tmux.run(["list-panes", "-a", "-F", "#{pane_id}"], socket, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    ids = out.stdout.split()
    if out.returncode != 0 or not ids or not all(_PANE_ID.match(i) for i in ids):
        return None
    return ids


# --- the seat's line: what may sit between the pane and the seat ----------------------

#: Shells, by name. One may hold the pane only as a login shell (`-bash`) or
#: an interactive one (`zsh`, `bash -l`), never with a command (`sh -c ...`),
#: whose work after the seat would die with the pane.
_SHELLS = frozenset({"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh"})
_SHELL_FLAGS = frozenset({"-l", "-i", "-il", "-li", "--login", "--interactive"})
_INTERPRETER = re.compile(r"^[Pp]ython[0-9.]*$")


def _script_argv(command: str | None) -> list[str]:
    """COMMAND's words, past a leading python interpreter: a console script
    reads `<python> <path>/tm task work ...` in `ps`."""
    words = (command or "").split()
    if words and _INTERPRETER.match(os.path.basename(words[0])):
        words = words[1:]
    return words


#: Each launcher's script name and the words it must run with.
_LAUNCHERS = {"tm": ("task", "work"), "taskmaster": ("task", "work"), "session": ("start",)}

#: A launcher's real path (symlinks resolved) -> the words it must run with.
Launchers = Mapping[str, tuple[str, ...]]


def installed_launchers(which: Callable[[str], str | None] = shutil.which) -> dict:
    """The launchers INSTALLED here: `tm`, `taskmaster` and `session` as this
    PATH resolves them, each by its real path. Only these are launchers: a
    script merely CALLED `tm` somewhere else is not.
    A name not on PATH has no launcher, so its lines read KEEP."""
    found: dict[str, tuple[str, ...]] = {}
    for name, words in _LAUNCHERS.items():
        path = which(name)
        if path:
            found[os.path.realpath(path)] = words
    return found


def is_launcher(command: str | None, real: Launchers) -> bool:
    """`tm task work` (taskmaster's seat launcher; `taskmaster task work`
    too) or `session start` (sessiontools' own), run from the script REAL
    names (`installed_launchers`). `ps` shows a console script by the path
    it was run from, so a bare or relative name, or a script of the same
    name elsewhere, is not a launcher. Nothing else is."""
    w = _script_argv(command)
    if not w or not os.path.isabs(w[0]):
        return False
    words = real.get(os.path.realpath(w[0]))
    return (words is not None and os.path.basename(w[0]) in _LAUNCHERS
            and tuple(w[1:1 + len(words)]) == words)


def is_holder_shell(command: str | None) -> bool:
    """A login shell (`-bash`, `-zsh`) or an interactive one with nothing
    but login/interactive flags (`zsh`, `bash -l`). `sh -c CMD` is NOT one."""
    w = (command or "").split()
    if not w:
        return False
    if w[0].startswith("-"):
        return len(w) == 1 and os.path.basename(w[0][1:]) in _SHELLS
    return os.path.basename(w[0]) in _SHELLS and all(f in _SHELL_FLAGS for f in w[1:])


def line_problem(pid: int, holder: int, table: dict, real: Launchers) -> str | None:
    """None when the seat's line is allowed: every process strictly between the pane's holder and the seat
    is a launcher (`is_launcher`), and the holder, unless it is the seat, is
    a launcher or a login/interactive shell (`is_holder_shell`). A launcher
    is the one installed here (REAL, `installed_launchers`), never a name
    alone. kill-pane
    takes the whole line, and anything else on it (a script with work left
    after the seat, a REPL, an editor's terminal) is not the seat's to end.
    Else the reason, naming the process. HOLDER must be on PID's line."""
    line = list(ancestors(pid, table))
    upto = line.index(holder)

    def named(p: int) -> str:
        return f"{p} ({((table[p].command if p in table else None) or '?')[:40]})"

    for p in line[1:upto]:
        if not is_launcher(table[p].command, real):
            return f"{named(p)} sits between the pane and the seat, and is not a launcher"
    if holder != pid and not (is_launcher(table[holder].command, real)
                              or is_holder_shell(table[holder].command)):
        return f"pane held by {named(holder)}: not a launcher or a login shell"
    return None


@dataclass
class World:
    """What an act reads and does, each a seam for tests. ROWS, TABLE and
    LISTENERS are fresh reads (`listing.rows`, `procs.table(commands=True)`,
    `pane_state.listeners`); REREAD reads one sidecar again; PANE_TTY_PIDS
    reads who is on the pane's terminal; SERVER_PANES every pane id on a
    server; LAUNCHERS the launchers installed here; CLOSE is `close_seat`."""

    rows: Callable[[], list[Row]]
    table: Callable[[], dict]
    listeners: Callable[[], object]
    now: Callable[[], float] = time.time
    tail_check: Callable[[Path], tuple[bool, str]] | None = None
    reread: Callable[[Session], Session | None] = _reread
    pane_pid: Callable[[str, str], int | None] = _pane_pid
    pane_tty_pids: Callable[[str, str], set[int] | None] = _pane_tty_pids
    server_panes: Callable[[str], list[str] | None] = _server_panes
    launchers: Callable[[], Launchers] = installed_launchers
    close: Callable[[str, str], tuple[bool, str]] = close_seat
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Act:
    verdict: Verdict        # the listed row the act was asked for
    outcome: str            # CLOSED, SKIPPED or FAILED
    why: str

    def line(self) -> str:
        r = self.verdict.row
        if self.outcome == CLOSED:
            return f"closed {r.short} ({r.name})"
        return f"{self.outcome} {r.short} ({r.name}): {self.why}"


def descendants(pid: int, table: dict) -> dict[int, Proc]:
    """Every process under PID in TABLE, by parent pid, PID itself not included."""
    kids = children(table)
    out: dict[int, Proc] = {}
    queue = list(kids.get(pid, []))
    while queue:
        p = queue.pop(0)
        if p in out or p == pid or p not in table:
            continue
        out[p] = table[p]
        queue.extend(kids.get(p, []))
    return out


def _alive(p: Proc, table: dict) -> bool:
    """P still runs: its pid is in TABLE with the same start (a reused pid is not P)."""
    q = table.get(p.pid)
    return q is not None and (p.started is None or q.started == p.started)


#: What `verify` says when a read failed: the close is NOT verified.
UNREADABLE = "unreadable"


def verify(socket: str, pane: str, watch: dict[int, Proc], w: World) -> list[str]:
    """[] when the pane is gone and every process in WATCH is dead within
    VERIFY_SECONDS; else what is left, named (pid and command). A survivor
    is NEVER killed: reap does not force. A read that
    fails (an empty process table: `ps` failed; the server's panes not
    readable) is UNREADABLE, never "all gone"."""
    steps = max(1, int(VERIFY_SECONDS / VERIFY_STEP))
    left: list[str] = []
    for i in range(steps):
        table = w.table()
        panes = w.server_panes(socket)
        if not table or panes is None:
            left = ([f"process table {UNREADABLE}"] if not table else []) + (
                [f"panes on {socket} {UNREADABLE}"] if panes is None else [])
        else:
            survivors = [p for p in watch.values() if _alive(p, table)]
            pane_open = pane in panes
            if not survivors and not pane_open:
                return []
            left = ([f"pane {pane} still open"] if pane_open else []) + [
                f"{p.pid} ({(table[p.pid].command or '?')[:60]})" for p in survivors]
        if i < steps - 1:
            w.sleep(VERIFY_STEP)
    return left


def act(v: Verdict, limit: int, w: World) -> Act:
    """`--yes` on one listed seat: re-measure every gate, then close it, then
    verify. Stops at the first gate that fails, and says why. The list is
    advice; this is the act."""
    if v.verdict != CLOSE:
        return Act(v, SKIPPED, v.why)
    was = v.row
    sid, pid, socket, pane = was.session.id, was.session.pid, was.tmux_socket, was.tmux_pane

    def skip(why: str) -> Act:
        return Act(v, SKIPPED, why)

    # 1. Re-read at the act: the seat, fresh, by session id.
    rows = w.rows()
    now_rows = [r for r in rows if r.session.id == sid]
    if not now_rows:
        return skip("gone since the list")
    row = now_rows[0]
    s = row.session
    if s.pid != pid or s.proc_start != was.session.proc_start:
        return skip(f"pid changed since the list ({pid} -> {s.pid})")
    if (row.tmux_socket, row.tmux_pane) != (socket, pane):
        return skip(f"moved since the list ({socket}:{pane} -> {row.tmux_socket}:{row.tmux_pane})")
    now = w.now()
    if s.idle_seconds(now) <= limit:
        return skip(f"no longer idle past {fmt_duration(limit)} (state {s.state})")
    # The fresh verdict re-runs every gate, the tail (2) included.
    listeners = w.listeners()
    fresh = judge(row, w.table(), now, listeners, w.tail_check,
                  sharers(rows).get(sid, ()))
    if fresh.verdict != CLOSE:
        return skip(fresh.why)

    # 3. Just before the kill: the pane signal ONCE MORE, then the pane's own pid.
    last = w.reread(s)
    table = w.table()
    if last is None or last.pid != pid:
        return skip("sidecar gone or changed before the kill")
    reading = pane_reading(last, table, listeners)
    if reading.word != PANE_FREE:
        return skip(f"turned {reading.word} ({reading.why}) before the kill")
    holder = w.pane_pid(socket, pane)
    line = set(ancestors(pid, table))
    if holder is None or holder not in line:
        return skip(f"pane {pane} is held by {holder}, not this seat")
    # The pane kill takes the whole line from the holder down, so only a
    # launcher (or the holder's own login shell) may sit on it.
    problem = line_problem(pid, holder, table, w.launchers())
    if problem:
        return skip(problem)
    mine = descendants(pid, table)
    # kill-pane hangs up the whole pane, not just the seat: a shell that runs
    # the seat AND a job of its own (a hand-launched claude beside a dev
    # server) would lose that job too. Anything in the pane's tree, or on the
    # pane's terminal (a job forked off and let go has ppid 1, in no tree),
    # that is neither on the seat's line nor under the seat is KEEP, and
    # named. A terminal that cannot be read is KEEP.
    on_tty = w.pane_tty_pids(socket, pane)
    if on_tty is None or holder not in on_tty:
        return skip(f"pane {pane}'s terminal could not be read")
    others = sorted({*descendants(holder, table), *on_tty} - line - set(mine))
    if others:
        named = ", ".join(f"{p} ({((table[p].command if p in table else None) or '?')[:40]})"
                          for p in others[:TAIL_PATHS])
        more = f" +{len(others) - TAIL_PATHS} more" if len(others) > TAIL_PATHS else ""
        return skip(f"pane {pane} also holds {named}{more}, not this seat's")
    # A server's last pane takes the server, and the server SIGTERMs its
    # own jobs (`run-shell -b`, hooks): in no tree under the pane, and not on
    # its terminal. Unreadable is KEEP too.
    panes = w.server_panes(socket)
    if panes is None:
        return skip(f"the panes on {socket} could not be read")
    if pane not in panes:
        return skip(f"pane {pane} gone from {socket} before the kill")
    if set(panes) == {pane}:
        return skip(f"pane {pane} is the last on {socket}: closing it would end the server "
                    "and its jobs")
    # The seat, its descendants and its line up to the holder must all go.
    path = list(ancestors(pid, table))
    watch = {**{p: table[p] for p in path[:path.index(holder) + 1]}, **mine}

    ok, detail = w.close(socket, pane)
    if not ok:
        return Act(v, FAILED, detail)
    # 4. Verify the effect: a kill sent is not a close.
    left = verify(socket, pane, watch, w)
    if any(UNREADABLE in x for x in left):
        return Act(v, FAILED, "kill-pane sent, but not verified: " + ", ".join(left))
    if left:
        return Act(v, FAILED, "kill-pane sent, but not closed: survivors " + ", ".join(left))
    return Act(v, CLOSED, fresh.why)


def log_act(a: Act, now: float | None = None, env: Mapping[str, str] | None = None) -> None:
    """One line per act in `reap.log` in the state dir: time,
    short, name, task, pane, outcome and why, tab-separated. Best effort: a
    log that cannot be written never undoes or blocks an act."""
    from datetime import datetime

    from sessiontools import snapshot
    r = a.verdict.row
    task = a.verdict.task.task_id if a.verdict.task else "-"
    when = datetime.fromtimestamp(time.time() if now is None else now).astimezone()
    fields = [when.isoformat(timespec="seconds"), r.short, r.name, task,
              f"{r.tmux_socket}:{r.tmux_pane}", a.outcome, " ".join(a.why.split())]
    try:
        d = snapshot.state_dir(env)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "reap.log", "a") as f:
            f.write("\t".join(fields) + "\n")
    except OSError as e:
        print(f"session reap: reap.log not written ({type(e).__name__})", file=sys.stderr)


# --- render ---------------------------------------------------------------------------

NAME_WIDTH = 40


def render_table(verdicts: list[Verdict], limit: int, acting: bool = False) -> str:
    if not verdicts:
        tail_word = "" if acting else " dry run:"
        return f"No seat idle past {fmt_duration(limit)}.{tail_word} nothing was done."
    width = min(NAME_WIDTH, max(len("NAME"), *(len(v.row.name) for v in verdicts)))
    lines = [f"{'SHORT':9} {'NAME':{width}} {'IDLE':>6}  {'VERDICT':7}  WHY"]
    for v in verdicts:
        lines.append(f"{v.row.short:9} {v.row.name[:width]:{width}} "
                     f"{fmt_duration(v.idle_s):>6}  {v.verdict:7}  {v.why}")
    closing = sum(v.verdict == CLOSE for v in verdicts)
    if acting:
        lines.append(f"--yes: {closing} to close, {len(verdicts) - closing} kept. "
                     "Each is re-checked at the act.")
    else:
        lines.append(f"dry run: {closing} would close, {len(verdicts) - closing} kept. "
                     "Nothing was done.")
    return "\n".join(lines)


def render_json(verdicts: list[Verdict], acts: list[Act] | None = None) -> str:
    """`default=str`: front matter is YAML, so a status can be a date or
    another value JSON has no type for. One such README must not cost the
    whole listing. With ACTS (`--yes`), each row carries its `act`."""
    done = {a.verdict.row.session.id: a for a in acts or []}
    out = []
    for v in verdicts:
        d = v.to_dict()
        if acts is not None:
            a = done.get(v.row.session.id)
            d["act"] = {"outcome": a.outcome, "why": a.why} if a else None
        out.append(d)
    return json.dumps(out, indent=2, default=str)


# --- the verb -------------------------------------------------------------------------

EPILOG = """\
A seat is listed when it has been idle MORE than the limit. It reads CLOSE
only when all of these hold, and KEEP, with the reason, otherwise:
  - it has a tmux pane, no other seat shares that pane, and its pane signal
    (session pane-state) reads FREE;
  - its cwd is inside a task whose status is completed, archived, duplicate
    or todont (an open task may be waiting on a human: never closed);
  - its name, if it carries a +<task>. token, names that same task;
  - the task's worktree/ (a repo of its own) and its store directory have no
    unsaved work (git --no-optional-locks status: no lock is taken).
Without --yes it is a DRY RUN: nothing is closed.

--yes acts on the CLOSE rows (or just the SHORTs named), one at a time. At
the act every gate is read again; the pane signal once more just before the
kill, and the pane must hold nothing but the seat, in its process tree or on
its terminal (a shell that also runs a job of its own is KEEP: the kill
would hang that job up too). Between the pane and the seat only a launcher
(tm task work, session start: the one installed on PATH, by its real path,
never a name alone) or the pane's own login shell may sit, and
the pane may not be its server's last (that would end the server and its
jobs); then the
seat's PANE is killed by id (kill-pane, never kill-window), on
the server the seat was read from. A close is verified: the pane gone and the seat's process,
its line up to the pane and every descendant dead; a read that fails is
"not verified", exit 1. A survivor is named, never killed. A pane's last
kill closes its window, and a session's last its session: a human attached
there sees it end. Each act is one line in reap.log in the state dir.

Exit 0: a clean read, and every act closed and verified. 1: a --yes act was
refused at a gate, or a close did not verify. 2: a usage error, including a
SHORT that is not the session-id prefix of exactly one listed seat."""


def select(verdicts: list[Verdict], shorts: list[str]) -> tuple[list[Verdict], str | None]:
    """(the verdicts SHORTS name, error). Each SHORT must be the session-id
    prefix of exactly ONE listed seat: matched on the id only, never by
    name (`st-15` was inside three names on one host)."""
    picked: list[Verdict] = []
    for short in shorts:
        q = short.strip().lower()
        if not q:
            return [], "an empty SHORT names no seat"
        hits = [v for v in verdicts if v.row.session.id.lower().startswith(q)]
        if len(hits) != 1:
            many = ", ".join(v.row.short for v in hits)
            return [], (f"{short!r} names no listed seat" if not hits
                        else f"{short!r} names {len(hits)} listed seats ({many})")
        if hits[0] not in picked:
            picked.append(hits[0])
    return picked, None


def build_parser() -> argparse.ArgumentParser:
    """`session reap`'s parser."""
    p = argparse.ArgumentParser(prog="session reap", epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Every seat idle past the limit, CLOSE or KEEP, "
                                            "and why. A dry run unless --yes.")
    p.add_argument("short", nargs="*", metavar="SHORT",
                   help="only these seats: a session-id prefix of exactly one listed seat")
    p.add_argument("--idle", metavar="DURATION", default=None,
                   help="the idle limit: <n>s, <n>m or <n>h, m is minutes (default 1h)")
    p.add_argument("--json", action="store_true", help="the same rows as JSON")
    p.add_argument("--yes", action="store_true",
                   help="act: close the CLOSE rows, each re-checked at the act")
    return p


def main(argv: list[str], rows: Callable[[], list[Row]] | None = None,
         table: Callable[[], dict] | None = None, now: float | None = None,
         listeners: Callable[[], object] | None = None,
         world: World | None = None) -> int:
    """`session reap`. ROWS, TABLE, LISTENERS and WORLD are seams for tests;
    by default the fresh `listing.rows()`, `procs.table(commands=True)`, the
    `pane_state.listeners` setting, and the real tmux close."""
    p = build_parser()
    a = p.parse_args(argv)
    try:
        limit = parse_idle(a.idle) if a.idle is not None else DEFAULT_IDLE_SECONDS
    except ValueError as e:
        p.error(f"--idle: {e}")
    if world is None:
        if rows is None:
            from sessiontools import listing
            rows = listing.rows
        if table is None:
            from sessiontools import procs as _procs

            def table() -> dict:
                return _procs.table(commands=True)
        if listeners is None:
            from sessiontools import pane_state
            listeners = pane_state.listeners
        world = World(rows=rows, table=table, listeners=listeners)
        if now is not None:
            world.now = lambda: now
    verdicts = plan(world.rows(), world.table(), world.now(), limit, world.listeners(),
                    world.tail_check)
    if a.short:
        verdicts, error = select(verdicts, a.short)
        if error:
            p.error(error)
    if not a.yes:
        print(render_json(verdicts) if a.json else render_table(verdicts, limit))
        return 0

    # --yes: CLOSE rows act; a KEEP row named by SHORT is a refusal (exit 1).
    if not a.json:
        print(render_table(verdicts, limit, acting=True), flush=True)
    acts: list[Act] = []
    for v in verdicts:
        if v.verdict != CLOSE and not a.short:
            continue
        done = act(v, limit, world)
        log_act(done, world.now())
        acts.append(done)
        if not a.json:
            print(done.line(), flush=True)
    if a.json:
        print(render_json(verdicts, acts))
    return 0 if all(x.outcome == CLOSED for x in acts) else 1
