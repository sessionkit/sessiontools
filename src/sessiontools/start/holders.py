"""The front door: a hand launch of an agent that a live Claude
Code session already holds is refused, naming the holder and its age.

Measured on master b216a19 (2030-08-02): a live
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
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping
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
        return f"session {newest.id[:8]}{place}{age}{extra}"

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
           now: float | None = None) -> Door:
    """Who holds `agent` right now. Reads the sidecars first and asks `ps`
    only when one names the agent, so a launch with no candidate costs no
    process-table read. A table that raises or comes back empty cannot say
    who is live (a working `ps` always lists at least this process): the
    door is then `unmeasured`, and opens."""
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
    held = [s for s in candidates if _procs.same_process(s.pid, s.proc_start, procs)
            and not replaced(s, own)]
    started = {s.pid: procs[s.pid].started for s in held}
    held.sort(key=lambda s: s.started_at or started.get(s.pid) or 0.0, reverse=True)
    return Door(agent, tuple(held), now, started)


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
