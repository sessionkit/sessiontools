"""The front door: a hand launch of an agent that a live Claude
Code session already holds is refused, naming the holder and its age.

Measured on master b216a19 (2030-12-08): a live
sidecar for agent `scout` stood beside `session start scout hello`, and the
launch exec'd the vendor anyway, exit 0. The incident behind the card was
the same shape (15 Jun, `alice`, a human's own shell): a second `alice` beside the
first, two seats answering as one agent.

THE RULE:

- LIVE is `claude_code.live()`'s rule and nothing else: a sidecar whose pid
  is running with the start time the sidecar names (`procs.same_process`),
  so a crash-left sidecar or a reused pid holds nothing.
- A HOLDER of agent A is a live session whose sidecar says `agent == A`,
  EXCEPT one in the launcher's own tmux pane: a sidecar whose `tmux` pane
  (`pane_of`) equals the launch environment's `$TMUX_PANE` is being
  REPLACED, not doubled. `respawn --go`
  runs `tmux respawn-pane -k` with `exec session start <agent>`; `-k` does
  not wait for the old claude to die, so its sidecar can still read live
  here, and the door refused, leaving the pane dead. No `$TMUX_PANE` (not
  in tmux) exempts nothing; a sidecar naming no pane exempts nothing.
- The check runs only for a HAND launch: no claude `-n`/`--name` in the
  passthrough. A spawner (`tm kick`) names every seat with `-n`, and seat
  uniqueness for spawned seats is the spawner's door, not this one. The
  sidecar's `agent` is the agent TYPE (four live seats of one type is normal), so
  a door that checked named seats too would refuse every kick.
- A plain session (no agent) is never checked.
- CANNOT MEASURE (no process table): fail OPEN, loudly. One broken `ps`
  must not stop every launch on the host. A missing sidecar dir is not a
  failure: it is no holders.

`--allow-second-holder` launches anyway, with one stderr line naming who it
starts beside. A refusal is EXIT_HELD (3): a caller can tell "someone holds
this agent" from a launch that failed (1) or a usage refusal (2).

THE CLAIM (per agent, for any caller). A sidecar is written only after the
vendor starts, so a survey of sidecars alone cannot see a launch still in its
start race. A claim closes that: under a host-local flock on
`<state dir>/holders/<agent>.lock`, a launch reads `<agent>.json` and, when
nobody live holds the agent, writes its own claim there BEFORE it execs. The
claim names the launcher's pid and that pid's `procStart`, spelled as a
sidecar spells them, so `procs.same_process` - the one liveness rule - reads
both. exec keeps the pid and the start time, so the claim lives exactly as
long as the session. Nobody releases it: a dead pid, or a reused one with
another start time, is stale, and the next claim overwrites it. Every helper
here takes the AGENT's name and finds its pair through `claim_paths`; none
knows which agent is the default seat (that policy is `default_seat`'s).

`replacing` is the NARROWER own-pane rule: a holder in the launcher's pane
is being replaced only on the direct path, and never when it is one of the
launcher's own ancestors (a nested start in a seat's own tool shell shares
its pane, and replaces nothing). `survey` keeps the broad `replaced` by
default, so the front door keeps its broad rule until it switches to the
narrow one.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from sessiontools import claude_code
from sessiontools import procs as _procs
from sessiontools.listing import fmt_duration
from sessiontools.start.errors import EXIT_HELD, LaunchError

#: claude's own spellings of the session's display name (`-n, --name
#: <name>`). A launch carrying one was named by its spawner.
NAME_FLAGS = ("-n", "--name")

OVERRIDE_FLAG = "--allow-second-holder"


class Door(NamedTuple):
    """What the front door found for one launch. `holders` is newest
    first; empty means nobody holds the agent (or the check did not run)."""

    agent: str
    holders: tuple
    now: float
    started: Mapping              # pid -> process start (epoch s), the age fallback
    unmeasured: str | None = None  # why the check could not run (fail open)

    @property
    def held(self) -> bool:
        return bool(self.holders)

    def holder_phrase(self) -> str:
        """`session 37f09460 (pid 78996, tmux %4), live 23m (+1 more live)`."""
        newest = self.holders[0]
        where = [f"pid {newest.pid}"] if newest.pid is not None else []
        pane = pane_of(newest.tmux)
        if pane:
            where.append(f"tmux {pane}")
        place = f" ({', '.join(where)})" if where else ""
        began = newest.started_at or self.started.get(newest.pid)
        age = f", live {fmt_duration(self.now - began)}" if began else ""
        more = len(self.holders) - 1
        extra = f" (+{more} more live)" if more else ""
        # A holder the default seat's claim found may have no id yet (a pane
        # mint before its sidecar, a named --continue): say so, as HELD
        # does, never an empty `session  (pid ...)`.
        who = (f"session {newest.id[:8]}" if newest.id
               else f"a session ({getattr(newest, 'no_id', None) or 'id not known yet'})")
        return f"{who}{place}{age}{extra}"

    def refusal(self) -> str:
        return (f"{self.agent} is already held by {self.holder_phrase()} -- refusing a "
                f"second holder; pass {OVERRIDE_FLAG} to start one anyway")

    def override(self) -> str:
        return (f"starting a second holder of {self.agent} beside "
                f"{self.holder_phrase()} ({OVERRIDE_FLAG})")

    def report_line(self, allowed: bool) -> str | None:
        """The dry-run report's line: what a live launch WOULD do. None when
        there is nothing to say (no holder, no failed measurement)."""
        if self.unmeasured:
            return f"holder:   (not checked: {self.unmeasured}; a live launch proceeds)"
        if not self.held:
            return None
        if allowed:
            return f"holder:   a live launch would start ANYWAY: {self.override()}"
        return f"holder:   a live launch would REFUSE (exit {EXIT_HELD}): {self.refusal()}"


def pane_of(tmux: str | None) -> str | None:
    """The pane part of a sidecar's `tmux` (`sessiontools:@154.%415` ->
    `%415`); None when it names none."""
    if not tmux:
        return None
    tail = tmux.rsplit(".", 1)[-1]
    return tail if tail.startswith("%") else None


def own_pane(environ: Mapping[str, str] | None = None) -> str | None:
    """The launcher's own tmux pane (`$TMUX_PANE`, e.g. `%415`); None when
    it is not in tmux (unset or empty)."""
    environ = os.environ if environ is None else environ
    return environ.get("TMUX_PANE") or None


def replaced(session, own: str | None) -> bool:
    """True when `session` lives in the launcher's own pane, so this launch
    replaces it (a `respawn-pane -k` whose old claude has not died yet).
    Both sides must name a pane: no `$TMUX_PANE`, or a sidecar with no
    usable `tmux`, replaces nothing."""
    return own is not None and pane_of(session.tmux) == own


def replacing(pid: int | None, tmux: str | None, own: str | None, procs: Mapping,
              *, direct: bool, launcher: int | None = None) -> bool:
    """The NARROW own-pane rule: True only when this launch replaces the
    holder at PID (pane TMUX, a sidecar's `tmux` or a claim's `pane`). All
    three must hold: the launch is on the DIRECT path (a tmux placement
    never replaces its own pane); both sides name a pane and it is the
    same one (`replaced`'s test: no pane on either side replaces nothing);
    and PID is not the launcher or one of its ancestors in PROCS (a nested
    start in a seat's own tool shell shares the seat's pane, and the seat
    is its ancestor). LAUNCHER defaults to this process."""
    if not direct or own is None or pane_of(tmux) != own:
        return False
    launcher = os.getpid() if launcher is None else launcher
    return pid not in set(_procs.ancestors(launcher, procs))


def names_the_seat(cli_args: Iterable[str]) -> bool:
    """True when the vendor passthrough carries claude's `-n`/`--name`, in
    any spelling claude accepts: `-n X`, `-nX`, `--name X`, `--name=X`."""
    for token in cli_args:
        if token in NAME_FLAGS or token.startswith("--name="):
            return True
        if token.startswith("-n") and not token.startswith("--"):
            return True
    return False


def survey(agent: str, environ: Mapping[str, str] | None = None,
           table: Callable[[], dict] = _procs.table,
           now: float | None = None,
           exempt: Callable[[object, str | None, dict], bool] | None = None) -> Door:
    """Who holds `agent` right now. Reads the sidecars first and asks `ps`
    only when one names the agent, so a launch with no candidate costs no
    process-table read. A table that raises or comes back empty cannot say
    who is live (a working `ps` always lists at least this process): the
    door is then `unmeasured`, and opens. EXEMPT(session, own pane, table)
    says which live sidecar this launch replaces; the default is the front
    door's broad `replaced`, and a caller with a narrower rule passes its
    own (`replacing`)."""
    now = time.time() if now is None else now
    env = dict(environ) if environ is not None else None
    candidates = [s for s in claude_code.sessions(env) if s.agent == agent]
    if not candidates:
        return Door(agent, (), now, {})
    try:
        procs = table()
    except Exception as error:   # noqa: BLE001 - fail open on ANY failure, loudly
        return Door(agent, (), now, {},
                    unmeasured=f"the process table could not be read ({error})")
    if not procs:
        return Door(agent, (), now, {},
                    unmeasured="the process table came back empty (`ps` failed?)")
    own = own_pane(env)
    exempt = exempt or (lambda session, pane, _table: replaced(session, pane))
    held = [s for s in candidates if _procs.same_process(s.pid, s.proc_start, procs)
            and not exempt(s, own, procs)]
    started = {s.pid: procs[s.pid].started for s in held}
    held.sort(key=lambda s: s.started_at or started.get(s.pid) or 0.0, reverse=True)
    return Door(agent, tuple(held), now, started)


def sidecars(environ: Mapping[str, str] | None = None) -> list:
    """Every session sidecar the vendor wrote, live or not (the survey's
    source), for a caller that needs one by pid or pane."""
    return list(claude_code.sessions(dict(environ) if environ is not None else None))


def front_door(agent: str, cli_args: Iterable[str],
               environ: Mapping[str, str] | None = None,
               table: Callable[[], dict] = _procs.table,
               now: float | None = None) -> Door | None:
    """The door for one launch: None when it does not apply (a plain
    session, or a seat its spawner named with `-n`), else the survey."""
    if not agent or names_the_seat(cli_args):
        return None
    return survey(agent, environ, table=table, now=now)


def enforce(door: Door | None, allowed: bool, warn: Callable[[str], None]) -> None:
    """A live launch at the door: refuse a held agent (LaunchError,
    EXIT_HELD), or, with the override, say whose holder it starts beside.
    An unmeasured door warns and opens."""
    if door is None:
        return
    if door.unmeasured:
        warn(f"the second-holder check could not run: {door.unmeasured} -- launching unchecked")
        return
    if not door.held:
        return
    if not allowed:
        raise LaunchError(door.refusal(), code=EXIT_HELD)
    warn(door.override())


# ---------- the claim: one per agent, under a host-local lock -------------------

#: The directory under the state dir that holds every agent's lock and claim.
HOLDERS_DIR = "holders"

#: How long a launch waits for an agent's lock, and how often it tries:
#: `snapshot._locked`'s polling, on the agent's own lock file.
LOCK_WAIT_S = 10.0
LOCK_POLL_S = 0.05

#: The claim file's schema version.
CLAIM_VERSION = 1


class ClaimUnreadable(OSError):
    """The claim file exists and cannot be read (permission denied, say):
    a failure to measure, never a stale claim."""


def holders_dir(environ: Mapping[str, str] | None = None) -> Path:
    """`<state dir>/holders/`. Computed, never created: a read makes no
    directory."""
    from sessiontools import snapshot
    return snapshot.state_dir(environ) / HOLDERS_DIR


def claim_paths(agent: str, environ: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """(lock, claim) for AGENT: `<state dir>/holders/<agent>.lock` and
    `<agent>.json`. Creates nothing. A name that could leave the directory
    or hide in it - empty, with a `/` or a NUL, `..`, or a leading `.` - is
    refused (ValueError): a command line names agents, and `resolve_agent`
    accepts `a/b` when `<root>/a/b/b.md` exists."""
    if (not agent or "/" in agent or "\\" in agent or "\0" in agent
            or agent.startswith(".") or ".." in agent):
        raise ValueError(f"{agent!r} cannot name a holder's claim: an agent's claim "
                         "file is its name, so it may not be empty or carry '/', "
                         "'..' or a leading '.'")
    base = holders_dir(environ)
    return base / f"{agent}.lock", base / f"{agent}.json"


class Locked(NamedTuple):
    """What `locked` yields: `ok` when the agent's flock is held, else
    `why` it is not."""

    ok: bool
    why: str = ""


@contextmanager
def locked(agent: str, environ: Mapping[str, str] | None = None,
           wait: float = LOCK_WAIT_S) -> Iterator[Locked]:
    """Hold AGENT's exclusive flock, yielding Locked(True); Locked(False,
    why) when it could not be had within WAIT seconds or the lock file
    cannot be made. The one call here that makes `holders/` (0700) and the
    lock file (0600); its content means nothing. A holder that dies, even
    by SIGKILL, drops the flock with its last descriptor. WAIT runs on the
    monotonic clock."""
    lock, _claim = claim_paths(agent, environ)
    fd = None
    try:
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        yield Locked(False, f"the lock file {lock} could not be opened ({error.strerror or error})")
        return
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    yield Locked(False, f"the lock {lock} failed ({error.strerror or error})")
                    return
                if time.monotonic() >= deadline:
                    yield Locked(False, f"another launch held the lock {lock} for {wait:g}s")
                    return
                time.sleep(LOCK_POLL_S)
        yield Locked(True)
    finally:
        os.close(fd)        # closing drops the flock


def read_claim(agent: str, environ: Mapping[str, str] | None = None) -> dict | None:
    """AGENT's claim as written, or None when there is none or it does not
    parse as a claim (no file, not JSON, not an object, no integer `pid`):
    a claim that names no pid is STALE, as a dead pid's is. A file that
    exists and cannot be READ raises ClaimUnreadable: that is a failure to
    measure, not a stale claim. Reads only: creates nothing."""
    _lock, path = claim_paths(agent, environ)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise ClaimUnreadable(f"the claim {path} could not be read "
                              f"({getattr(error, 'strerror', None) or error})") from error
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    return data


def claim_is_live(claim: Mapping | None, procs: Mapping) -> bool:
    """True when CLAIM names a running pid that started when it says
    (`procs.same_process`, the sidecars' rule)."""
    if not claim:
        return False
    start = claim.get("procStart")
    return _procs.same_process(claim.get("pid"), start if isinstance(start, str) else None,
                               dict(procs))


def live_claim(agent: str, procs: Mapping, environ: Mapping[str, str] | None = None,
               *, direct: bool, launcher: int | None = None) -> dict | None:
    """AGENT's claim when it names a live holder that this launch does not
    replace (`replacing`), else None (none, stale, unparseable, or
    replaced). Raises ClaimUnreadable as `read_claim` does."""
    claim = read_claim(agent, environ)
    if not claim_is_live(claim, procs):
        return None
    pane = claim.get("pane") if isinstance(claim.get("pane"), str) else None
    if replacing(claim["pid"], pane, own_pane(environ), procs, direct=direct,
                 launcher=launcher):
        return None
    return claim


def proc_start_of(pid: int, procs: Mapping) -> str | None:
    """PID's start as a sidecar's `procStart` spells it on macOS: `ps`'s
    `lstart` in UTC ctime. None when the table does not know the pid."""
    proc = procs.get(pid)
    if proc is None or proc.started is None:
        return None
    return time.strftime(_procs.CTIME, time.gmtime(proc.started))


def write_claim(agent: str, claim: Mapping, environ: Mapping[str, str] | None = None) -> Path:
    """Write AGENT's claim: `{"v": 1, "agent": AGENT, **CLAIM}`, as a temp
    file and a rename, 0600, so a reader never sees half of one. Call it
    under `locked(agent)`. Raises OSError when it cannot be written."""
    _lock, path = claim_paths(agent, environ)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"v": CLAIM_VERSION, "agent": agent, **claim}
    fd, tmp = tempfile.mkstemp(prefix=f".{agent}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path
