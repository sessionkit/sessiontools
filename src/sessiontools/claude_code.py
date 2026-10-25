"""Claude Code - the first driver.

Claude Code writes one sidecar per live session, `<dir>/<pid>.json`, and
keeps its `status` and `updatedAt` current as the session works. Three of
our tools each grew a private reader of that file (sessionmail's
`_seat._sidecar_idle`, taskmaster's `agents/discovery.py`, `session get
idle`); this is the one copy they share.

The words are the harness's, not ours. Claude Code says `busy`, `shell`,
`idle`, `waiting`; a driver translates: busy is busy, anything else is
not, and no sidecar is unknown. Nothing above this module should ever
see a harness's vocabulary, by design: the API never speaks Claude
Code's vocabulary, so the day a status word changes, one driver changes
and nothing else does.

THREE STATES: `busy`, `idle with children` and `idle`, in `Session.state`.
Claude Code keeps the sidecar
`busy` while a background agent lives, after the turn is over, so a second
signal says the turn ended: the `turn-ended` mark (`sessiontools.turn`). A
busy sidecar reads `idle with children`, idle since the mark's `at`, only
when ALL of these hold; otherwise the reading is exactly the one before the
mark existed:

- the mark is a valid v1 mark (`turn.read_at`);
- its `at` is no older than the sidecar's `updatedAt`: a status written
  after the mark outranks it, so a mark whose clear was lost goes stale at
  the next status change ("at >= updatedAt"). Claude Code does not
  re-write `updatedAt` at a busy -> busy turn end (measured), so a fresh
  mark always passes;
- the sidecar's process is live (`procs.same_process`): a crash after Stop
  leaves a mark nobody will clear.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

HARNESS = "claude-code"

#: The three states, in our words (`Session.state`).
BUSY = "busy"
IDLE_WITH_CHILDREN = "idle with children"
IDLE = "idle"
STATES = (BUSY, IDLE_WITH_CHILDREN, IDLE)

#: Where the sidecars live. The same rule every reader already used:
#: `SESSION_SIDECAR_DIR`, else `~/.claude/sessions`.
SIDECAR_DIR_ENV = "SESSION_SIDECAR_DIR"


def sidecar_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(SIDECAR_DIR_ENV) or (Path.home() / ".claude" / "sessions"))


@dataclass(frozen=True)
class Session:
    """One live session as the harness reports it, in our words."""

    id: str
    harness: str
    busy: bool
    updated_at: float          # epoch seconds, when status last changed
    pid: int | None = None
    cwd: str | None = None
    name: str | None = None
    agent: str | None = None
    raw_status: str | None = None   # the harness's own word, for the curious
    path: Path | None = None        # the sidecar it came from
    proc_start: str | None = None   # the process's start as written - liveness (procs.parse_start)
    started_at: float | None = None  # epoch seconds, when the session began
    version: str | None = None      # the harness's version
    #: Where the harness says it runs: `<tmux session>:@window.%pane`, e.g.
    #: `sessiontools:@22.%45`. The SESSION name, not the socket:
    #: it names the pane but not the server, so it is a check, not an address.
    tmux: str | None = None
    #: When the turn ended, epoch seconds: set only for `idle with children`
    #: (the harness says busy, a valid turn-ended mark says the turn is over).
    turn_ended_at: float | None = None

    @property
    def state(self) -> str:
        """`busy`, `idle with children` or `idle`: one word, ours."""
        if self.busy:
            return BUSY
        return IDLE_WITH_CHILDREN if self.turn_ended_at is not None else IDLE

    @property
    def since(self) -> float:
        """When the state began, epoch seconds: the turn's end for `idle with
        children`, else when the status last changed."""
        return self.turn_ended_at if self.turn_ended_at is not None else self.updated_at

    def idle_seconds(self, now: float | None = None) -> float:
        """0 while busy; else seconds since the state began (`since`)."""
        if self.busy:
            return 0.0
        now = time.time() if now is None else now
        return max(0.0, now - self.since)


class _Table:
    """The process table, read at most once and only when asked: a mark on a
    busy sidecar is the only thing that needs it outside `live()`."""

    def __init__(self, table: dict | None = None):
        self._table = table

    def get(self) -> dict:
        if self._table is None:
            from sessiontools import procs as _procs
            self._table = _procs.table()
        return self._table


def turn_ended_at(sid: str, updated_at: float,
                  env: dict[str, str] | None = None) -> float | None:
    """The `at` of SID's turn-ended mark when it may count: a valid v1 mark
    no older than UPDATED_AT (the sidecar's last status write). None
    otherwise. Says nothing about liveness: that is the caller's."""
    from sessiontools import turn
    if not turn.is_sid(sid):
        return None
    at = turn.read_at(turn.mark_path(sid, env))
    return at if at is not None and at >= updated_at else None


def mark_current(s: Session, env: dict[str, str] | None = None) -> bool:
    """True while the turn-ended mark on disk still gives S's reading: the
    cheap check a cached Session is held to (the list snapshot's). Only a
    session the harness calls busy reads its mark; any other is current."""
    if s.raw_status != "busy":
        return True
    return turn_ended_at(s.id, s.updated_at, env) == s.turn_ended_at


def _read(path: Path, env: dict[str, str] | None = None,
          procs: _Table | None = None) -> Session | None:
    """One sidecar -> one Session, or None when it is not one: unreadable,
    not JSON, not a dict, or missing the two fields a verdict needs. A busy
    sidecar with a mark that counts, whose process is live, is `idle with
    children` (module docstring)."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("sessionId")
    status = data.get("status")
    updated = data.get("updatedAt")
    if not isinstance(sid, str) or not status or updated is None:
        return None
    try:
        updated_at = float(updated) / 1000.0
    except (TypeError, ValueError):
        return None
    busy = status == "busy"
    pid = data.get("pid") if isinstance(data.get("pid"), int) else None
    proc_start = data.get("procStart") if isinstance(data.get("procStart"), str) else None
    ended = turn_ended_at(sid, updated_at, env) if busy else None
    if ended is not None:
        from sessiontools import procs as _procs
        table = (procs or _Table()).get()
        if _procs.same_process(pid, proc_start, table):
            busy = False
        else:
            ended = None
    return Session(
        id=sid,
        harness=HARNESS,
        busy=busy,
        updated_at=updated_at,
        pid=pid,
        cwd=data.get("cwd") if isinstance(data.get("cwd"), str) else None,
        name=data.get("name") if isinstance(data.get("name"), str) else None,
        agent=data.get("agent") if isinstance(data.get("agent"), str) else None,
        raw_status=status if isinstance(status, str) else None,
        path=path,
        proc_start=proc_start,
        started_at=_ms(data.get("startedAt")),
        version=data.get("version") if isinstance(data.get("version"), str) else None,
        tmux=data.get("tmux") if isinstance(data.get("tmux"), str) else None,
        turn_ended_at=ended,
    )


def _ms(value: object) -> float | None:
    """Epoch milliseconds -> seconds; None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 1000.0


def sessions(env: dict[str, str] | None = None,
             procs: dict | None = None) -> list[Session]:
    """Every session with a readable sidecar, newest status first. A
    directory that is missing or unreadable is an empty list, not an
    error: no harness here is a fact, not a fault. PROCS, the process
    table, is read (once) only if a busy sidecar has a turn-ended mark."""
    try:
        paths = sorted(sidecar_dir(env).glob("*.json"))
    except OSError:
        return []
    table = _Table(procs)
    found = [s for s in (_read(p, env, table) for p in paths) if s is not None]
    return sorted(found, key=lambda s: s.updated_at, reverse=True)


def live(env: dict[str, str] | None = None,
         procs: dict | None = None) -> list[Session]:
    """The sessions whose process is running NOW - `sessions()` minus every
    sidecar a crash left behind or whose pid was reused (`procs.same_process`,
    the rule `session list` always used). Newest status first."""
    from sessiontools import procs as _procs
    table = _procs.table() if procs is None else procs
    return [s for s in sessions(env, table) if _procs.same_process(s.pid, s.proc_start, table)]


def find(session_id: str, env: dict[str, str] | None = None) -> Session | None:
    """The session with this id, or None when no sidecar names it."""
    for s in sessions(env):
        if s.id == session_id:
            return s
    return None


#: Where Claude Code puts THIS session's id, first non-blank wins, in this
#: order. The chain was written
#: three times and never shared; this is the one copy. Public, so a caller
#: that needs the names (to scrub a child's environment, say) reads them
#: here instead of spelling them again.
SESSION_ID_VARS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def session_id(env: dict[str, str] | None = None) -> str | None:
    """This session's id, from the environment ONLY; None when there is none.

    Never the disk: the pager needs the id before Claude Code has written
    its sidecar. The first NON-BLANK variable in SESSION_ID_VARS wins, and
    its value is stripped - the rule sessionmail's `_seat.session_id`
    already keeps (sessionmail 0c010ed, src/sessionmail/_seat.py:59
    SESSION_VARS, :930 session_id), except that no id is None here, not
    that module's `-` marks-line placeholder.
    """
    env = os.environ if env is None else env
    for var in SESSION_ID_VARS:
        value = (env.get(var) or "").strip()
        if value:
            return value
    return None


def current(env: dict[str, str] | None = None) -> Session | None:
    """This session, read from its sidecar: `find(session_id(env))`.

    None when the environment names no session, or when no sidecar names
    the one it does. Read-only, like everything in this driver.
    """
    sid = session_id(env)
    return None if sid is None else find(sid, env)


def idle_seconds(session_id: str, now: float | None = None,
                 env: dict[str, str] | None = None) -> float | None:
    """The one number the pager wants: 0 while busy, else seconds since
    the state began (the turn's end, for `idle with children`); None when
    nothing can be said. With no turn-ended mark, the rule of every reader
    it replaces."""
    s = find(session_id, env)
    return None if s is None else s.idle_seconds(now)


# --- the pane signal ----------------------------------------------------------
#
# Whether the seat in a pane is safe to close by hand: BUSY, FREE or UNKNOWN.
# A reading SEPARATE from `Session.state`: `state`,
# `idle_seconds` and the library spec are untouched, and the turn-ended mark
# is never an input - the reading keys on `raw_status`, which a mark never
# changes, so it is the same with SESSIONTOOLS_HOOK_TURN on or off.
#
# Claude Code writes `shell` whenever a background shell lives, and the
# pager monitor alone is one, so `shell` is not the test. The process
# tree is: a tool call runs as a DIRECT `<shell> -c …` child of the claude
# pid, so with the turn over the seat is FREE only when every such tool
# shell holds nothing but a known listener (a FREE worker's `pager listen`).
# Direct children that are not tool shells (the pager monitor, MCP servers,
# `caffeinate`) are the harness's own and are never counted. The
# default is WORK: nothing this code does not know can make a seat FREE.

PANE_BUSY = "BUSY"
PANE_FREE = "FREE"
PANE_UNKNOWN = "UNKNOWN"
PANE_WORDS = (PANE_BUSY, PANE_FREE, PANE_UNKNOWN)

#: The listener list's default (`pane_state.listeners` in the `session`
#: tool's settings): a command word and its first arguments.
DEFAULT_LISTENERS: tuple[str, ...] = ("pager listen",)

#: A tool shell, and the nested shells walked through rather than counted.
SHELLS = frozenset({"bash", "zsh", "sh", "dash"})
#: Pipe helpers: they count only beside something else.
NEUTRAL = frozenset({"tail", "head", "tee", "cat"})
#: Interpreters whose command word is the script they run.
INTERPRETERS = frozenset({"Python", "node", "ruby", "perl"})
#: How much of a work command the `why` keeps.
WHY_WIDTH = 40


@dataclass(frozen=True)
class Reading:
    """One pane's signal: WORD is BUSY, FREE or UNKNOWN; WHY says what made
    it (`turn`, `waiting`, `work: make test`, `listener`, `nothing live`,
    `no live process`, `status <word>`)."""

    word: str
    why: str


def _base(word: str) -> str:
    """argv0 -> its basename; a login shell's leading `-` dropped."""
    return os.path.basename(word).lstrip("-")


def _interpreter(base: str) -> bool:
    return base.startswith("python") or base in INTERPRETERS


def command_words(command: str | None) -> list[str]:
    """A command line -> its command word and the arguments after it. The
    word is argv0's basename, or for an interpreter (python*, Python, node,
    ruby, perl) the basename of the first argument that is not an option:
    `Python /Users/x/.local/bin/pager listen --monitor` -> pager, listen,
    --monitor. `ps` joins argv with spaces, so this splits on them."""
    argv = (command or "").split()
    if not argv:
        return []
    base = _base(argv[0])
    if _interpreter(base):
        for i, arg in enumerate(argv[1:], start=1):
            if not arg.startswith("-"):
                return [os.path.basename(arg), *argv[i + 1:]]
        return [base]
    return [base, *argv[1:]]


def is_tool_shell(command: str | None) -> bool:
    """`<bash|zsh|sh|dash> … -c …`: how Claude Code runs a Bash tool call.
    Deliberately loose: any short-option cluster with a `c` before the
    first operand, not the `shell-snapshots` path."""
    argv = (command or "").split()
    if not argv or _base(argv[0]) not in SHELLS:
        return False
    for arg in argv[1:]:
        if arg.startswith("--"):
            continue
        if arg.startswith("-") and len(arg) > 1:
            if "c" in arg[1:]:
                return True
            continue
        return False
    return False


def listener_tokens(listeners: object) -> tuple[tuple[str, ...], ...]:
    """The listener list as token tuples; non-strings and blanks dropped."""
    if isinstance(listeners, str) or not isinstance(listeners, (list, tuple)):
        return ()
    return tuple(tuple(entry.split()) for entry in listeners
                 if isinstance(entry, str) and entry.split())


def _is_listener(words: list[str], tokens: tuple[tuple[str, ...], ...]) -> bool:
    return any(tuple(words[:len(t)]) == t for t in tokens)


def _text(words: list[str]) -> str:
    return " ".join(words)[:WHY_WIDTH]


def sort_tool_shell(pid: int, table: dict, kids: dict[int, list[int]],
                    tokens: tuple[tuple[str, ...], ...],
                    also_listener: Callable[[list[str]], bool] | None = None) -> str | None:
    """None when the tool shell PID is a LISTENER (at least one listener
    under it and no work); else the text of its first work command.
    ALSO_LISTENER, a predicate on a process's command words, names one more
    kind of listener for a caller that needs it; the pane signal passes none.
    Walks
    every descendant breadth first. A nested shell WITH children is walked
    through and not counted; one with NO child at that instant is work
    (`bash ./long.sh` between commands runs its own builtins, and a listener
    beside it must not make the seat FREE). A process
    whose command was not read, or is empty, is work."""
    listener = False
    neutral: str | None = None
    queue = list(kids.get(pid, []))
    seen: set[int] = {pid}
    while queue:
        child = queue.pop(0)
        if child in seen or child not in table:
            continue
        seen.add(child)
        queue.extend(kids.get(child, []))
        words = command_words(table[child].command)
        if not words:
            return "?"
        if words[0] in SHELLS:
            if any(k in table for k in kids.get(child, [])):
                continue
            return _text(words)
        if _is_listener(words, tokens) or (also_listener is not None and also_listener(words)):
            listener = True
        elif words[0] in NEUTRAL:
            neutral = neutral or _text(words)
        else:
            return _text(words)
    if listener:
        return None
    return neutral or "shell, nothing under it"


def pane_reading(s: Session, table: dict,
                 listeners: object = DEFAULT_LISTENERS) -> Reading:
    """S's pane signal from the process TABLE, read with commands
    (`procs.table(commands=True)`). LISTENERS is the listener list
    (`pane_state.listeners`). UNKNOWN whenever the reading cannot be made,
    and never FREE then."""
    from sessiontools import procs as _procs
    if not table or not _procs.same_process(s.pid, s.proc_start, table):
        return Reading(PANE_UNKNOWN, "no live process")
    status = s.raw_status
    if status == "busy":
        # Hook OFF cannot tell a running turn from background agents; both are BUSY.
        return Reading(PANE_BUSY, "turn")
    if status == "waiting":
        return Reading(PANE_BUSY, "waiting")
    if status not in ("shell", "idle"):
        return Reading(PANE_UNKNOWN, f"status {status}")
    kids = _procs.children(table)
    direct = [table[c] for c in kids.get(s.pid, []) if c != s.pid]
    if any(p.command is None for p in direct):
        return Reading(PANE_UNKNOWN, "no commands")
    tokens = listener_tokens(listeners)
    listener = False
    for proc in direct:
        if not is_tool_shell(proc.command):
            continue
        work = sort_tool_shell(proc.pid, table, kids, tokens)
        if work is not None:
            return Reading(PANE_BUSY, f"work: {work}")
        listener = True
    return Reading(PANE_FREE, "listener" if listener else "nothing live")
