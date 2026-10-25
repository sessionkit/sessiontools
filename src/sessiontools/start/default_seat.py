"""The default seat: the one session per host named for its vendor.

THE NAME. The seat's word is the vendor's own key in `VENDORS`, so this
module carries no vendor's name: `word()` reads it from the row for
`agentfile.DEFAULT_VENDOR`, and a second vendor's seat would be its own
word. `session agent default` prints it, for the readers that cannot import
sessiontools (phone, the kit's setup, the wake word).

THE BUILT-IN. The package ships the seat's agent file, `default_seat.md`
beside this module: front matter with one `description:` line and an EMPTY
body (an empty body changes nothing in the system prompt). `resolve_agent`
falls back to it for the seat word only, and only when the user's agents
folder has no file of that name: a user's own file SHADOWS it. It is read
from the package and never copied into the agents folder.

THE DOOR (Q1, ruled). The vendor reserves its own word for its own built-in
agent: a plugin agent of that name is ignored (F3). So the seat's definition
rides the vendor's agents door, inline - the word, the SHIPPED description
and an empty prompt, public text only - and no plugin dir is written for it.
A shadowing file's description is accepted and NOT carried (Q18); its body
rides the 0600 system-prompt file door, first in that one file (Q2); and of
its native keys only `name:` (the seat word), `description:` and `settings:`
(this launcher's own settings layer) are accepted - every other one is
refused, naming it (Q15). The empty prompt changes nothing in the system
prompt (F7).

THE SINGLETON. A bare `session start` takes the seat when it is free, and
only one live session on a host holds it. Its order, in `start.main`:

1. the gates, before anything is locked or written (`gate`): macOS only
   until sessionmail can read Linux start times, then `message box path
   <word>` (exit 2 with no usage error = the box is missing). A failed
   gate starts plain with its one line, and never writes the state dir;
2. under the agent's lock (`holders.locked`), who holds it (`check`): a
   live claim first, then a live sidecar naming the word, each under the
   narrow own-pane rule (`holders.replacing`), from ONE process-table read;
3. free: the claim is written before the launch execs, naming this
   launcher's pid (exec keeps it), and `TAKEN` is said. Held: `HELD`.
   A check that cannot run FAILS CLOSED - plain, with `UNSURE` - because a
   wrong "free" is a second session hearing the seat's mail. The seat logic
   never refuses a bare start.

The lines are `taken_line`, `held_line`, `gate_line` and `unsure_line`;
each is one stderr line after `session start: `, and every word in them
that names the seat comes from the vendor row.

THE OTHER ROADS (`start.named_seat_start`, do-004). A NAMED launch of the
word takes or respects the same claim: the named lines are
`named_gate_line` and `named_unsure_line`, a refusal is the front door's
own line built from the seat's holder (`door_for`), a `-` resume of a
seat-born session says `resumed_line`, and the tmux path moves the claim
to the new pane (`repoint`).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from importlib import resources
from typing import NamedTuple

from sessiontools import procs as _procs
from sessiontools.listing import fmt_duration
from sessiontools.start import holders
from sessiontools.start.agentfile import (
    DEFAULT_VENDOR,
    AgentFile,
    parse_agent_markdown,
    shipped_vendor,
    unsupported_vendor,
)
from sessiontools.start.agents import Agent
from sessiontools.start.environment import host_environ
from sessiontools.start.errors import AuthoringError
from sessiontools.start.vendor import VENDORS

#: The built-in template's file name, in this package. Vendor-neutral on
#: purpose: the seat's word comes from the vendor row, not from a file name.
TEMPLATE = "default_seat.md"

#: The mark `--list-agents` puts in the second, TAB-separated column of the
#: built-in's line (Q12). Field 1 stays a name `resolve_agent` accepts.
BUILT_IN_MARK = "built in"


def word(vendor: str = DEFAULT_VENDOR) -> str:
    """The default seat's name for VENDOR: that vendor's key in `VENDORS`.
    Reads no host state. A vendor with no row is the agent file's own
    "unsupported vendor" error."""
    if not shipped_vendor(vendor):
        raise unsupported_vendor(vendor)
    return vendor


def template_path() -> str:
    """The built-in template's path inside the installed package."""
    return str(resources.files(__package__).joinpath(TEMPLATE))


#: The native front matter keys a file of the seat's name may carry: `name:`
#: (when it is the seat word), `description:` (accepted, not carried) and
#: `settings:` (this launcher's own settings layer, read whatever carries the
#: agent). Any other one the plugin dir would have handed the vendor, and the
#: inline door does not: refused in this cut, never dropped in silence.
ACCEPTED_NATIVE_KEYS = ("name", "description", "settings")


def is_seat(agent: Agent | None, vendor: str) -> bool:
    """Is AGENT the default seat for VENDOR: named with that vendor's word,
    whether the built-in carries it or a user's file shadows it?"""
    return (agent is not None and bool(agent.name) and shipped_vendor(vendor)
            and agent.name == word(vendor))


def shipped_description() -> str:
    """The built-in template's `description:`: the one text the agents door
    carries, whoever's file the seat launches from (Q18)."""
    with open(template_path(), encoding="utf-8") as handle:
        front_matter, _body = parse_agent_markdown(handle.read())
    return str(front_matter["description"])


def definition_json(vendor: str) -> str:
    """The seat's inline definition for VENDOR's agents door: its word, the
    shipped description and an EMPTY prompt. Never a body: argv is
    world-readable, and an empty prompt keeps the vendor's own (F7)."""
    return json.dumps({word(vendor): {"description": shipped_description(), "prompt": ""}})


def definition_argv(vendor: str, agent: Agent | None) -> list[str] | None:
    """[agents door, definition] for the seat on a vendor with that door,
    else None: every other agent keeps its plugin dir."""
    door = VENDORS[vendor].get("agents_door")
    if door is None or not is_seat(agent, vendor):
        return None
    return [door, definition_json(vendor)]


def carried_body(agent: Agent, agent_file: AgentFile) -> str:
    """The text a shadowing file's body puts in the system-prompt file door:
    the body without its outer blank lines, ending in one newline, or "" when
    there is none - the built-in's is empty - or the agent is not the seat."""
    if not is_seat(agent, agent_file.start.vendor):
        return ""
    text = agent_file.body.strip("\n")
    return text + "\n" if text.strip() else ""


def prompt_text(body: str, injected: str) -> str:
    """One system-prompt file: the seat file's BODY first, then a blank
    line, then the `start: context: system-prompt:` output (Q2)."""
    if body and injected:
        return f"{body}\n{injected}"
    return body or injected


def check_front_matter(agent: Agent, agent_file: AgentFile) -> None:
    """Refuse a file of the seat's name whose native keys the inline door
    cannot carry (Q15): a `name:` that is not the seat word, or any key but
    name/description/settings. One refusal names them all."""
    vendor = agent_file.start.vendor
    if not is_seat(agent, vendor):
        return
    seat = word(vendor)
    native = agent_file.native
    if "name" in native and native["name"] != seat:
        raise AuthoringError(
            f"`name: {native['name']}` in the default seat's file: a file named for "
            f"the seat may only call itself {seat!r} (or leave `name:` out)")
    refused = sorted(str(key) for key in native if key not in ACCEPTED_NATIVE_KEYS)
    if refused:
        keys = ", ".join(f"`{key}:`" for key in refused)
        raise AuthoringError(
            f"{keys} cannot be carried for the default seat ({seat!r}) in this "
            "cut: its definition goes inline, and only the shipped description "
            "rides it. A file named for the seat may carry `name:`, "
            "`description:`, `settings:`, its body and the launcher and vendor "
            "sections; set the seat's model in its `settings:` section (the "
            "launcher's own settings layer), or start another agent by name")


# ---------- the singleton: the gates, the check, the lines ----------------------

#: How long the box check may take: `message box path` answers in ~0.1 s.
BOX_CHECK_TIMEOUT_S = 5.0

#: How long a bare start waits for the seat's lock before it starts plain.
SEAT_LOCK_WAIT_S = holders.LOCK_WAIT_S

#: What a session that does not hold the seat loses: phone's own wording.
WILL_NOT_HEAR = "so this session will NOT hear Delta messages or calls"
#: Why the seat is macOS-only for now, in the LINUX lines.
NO_LINUX_YET = "sessionmail cannot yet read Linux start times"

#: The platform the seat can hear on, until sessionmail can read Linux
#: start times.
HEARING_PLATFORM = "darwin"

#: A claim's `via`: how the launch that wrote it came to hold the seat. It
#: is for the reader only; nothing decides on it but the no-id wording.
VIA_BARE = "bare start"
VIA_NAMED = "named start"
VIA_NAMED_RESUME = "named resume"
VIA_TMUX = "tmux placement"


def current_platform() -> str:
    """`sys.platform`, read at call time (a test patches this)."""
    return sys.platform


class Gate(NamedTuple):
    """The gates' answer: `ok`, else which one failed (`LINUX`, `NO_BOX`
    or `UNBOXED`) and, for `UNBOXED`, why the box could not be checked."""

    ok: bool
    failed: str = ""
    why: str = ""


def _usage_error(stderr: str) -> bool:
    """True when STDERR is a command-line usage error (click's `Usage:` and
    `Error:` lines) rather than `message box path`'s own "no box" answer:
    both exit 2, and an old `message` with no `box` verb is the former."""
    return any(line.startswith(("Usage:", "Error:")) for line in stderr.splitlines())


def box_gate(seat: str, environ: Mapping[str, str] | None = None,
             run: Callable = subprocess.run) -> Gate:
    """`message box path SEAT`, read-only, under `host_environ` with a
    BOX_CHECK_TIMEOUT_S bound. Exit 0: the box is there. Exit 2 with no
    usage error: `NO_BOX`. Anything else - not on PATH, a timeout, a usage
    error, another exit - is `UNBOXED`, naming why. It never makes a box."""
    try:
        result = run(["message", "box", "path", seat], capture_output=True, text=True,
                     timeout=BOX_CHECK_TIMEOUT_S, env=host_environ(environ),
                     stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        return Gate(False, "UNBOXED", "`message` is not on PATH")
    except subprocess.TimeoutExpired:
        return Gate(False, "UNBOXED",
                    f"`message box path` took over {BOX_CHECK_TIMEOUT_S:g}s")
    except OSError as error:
        return Gate(False, "UNBOXED", f"`message` could not run: {error.strerror or error}")
    if result.returncode == 0:
        return Gate(True)
    stderr = result.stderr or ""
    if result.returncode == 2 and not _usage_error(stderr):
        return Gate(False, "NO_BOX")
    if result.returncode == 2:
        said = [line for line in stderr.splitlines() if line.startswith("Error:")]
        detail = said[0].removeprefix("Error:").strip() if said else "a usage error"
        return Gate(False, "UNBOXED", f"`message box path` refused the call: {detail}")
    return Gate(False, "UNBOXED", f"`message box path` exited {result.returncode}")


def gate(seat: str, environ: Mapping[str, str] | None = None,
         platform: str | None = None, run: Callable = subprocess.run) -> Gate:
    """The gates in order: the platform, then the box. The first that fails
    decides; nothing after it runs."""
    platform = current_platform() if platform is None else platform
    if platform != HEARING_PLATFORM:
        return Gate(False, "LINUX")
    return box_gate(seat, environ, run=run)


class Holder(NamedTuple):
    """Who holds the seat: its session id (None when the claim names none
    yet), pid, tmux pane and start time (epoch s, for the age)."""

    session_id: str | None
    pid: int | None
    pane: str | None
    started: float | None
    #: Said in place of the id when there is none: why it is not known.
    no_id: str = "id not known yet"


class Check(NamedTuple):
    """The seat's check: the live holder, or None; `unmeasured` says why it
    could not run (fail closed). `procs` is the table it read, for the claim
    the caller then writes."""

    holder: Holder | None
    unmeasured: str = ""
    procs: Mapping = {}


def _sidecar_id(claim: Mapping, environ: Mapping[str, str] | None) -> str | None:
    """A claim with no session id yet: the id from the live sidecar of the
    claim's pid, else of its pane, when one is there."""
    sessions = holders.sidecars(environ)
    for session in sessions:
        if session.pid == claim.get("pid") and session.id:
            return session.id
    pane = claim.get("pane")
    for session in sessions:
        if pane and holders.pane_of(session.tmux) == pane and session.id:
            return session.id
    return None


def _claim_holder(claim: Mapping, procs: Mapping,
                  environ: Mapping[str, str] | None) -> Holder:
    sid = claim.get("sessionId") if isinstance(claim.get("sessionId"), str) else None
    sid = sid or _sidecar_id(claim, environ)
    pid = claim["pid"]
    proc = procs.get(pid)
    pane = claim.get("pane") if isinstance(claim.get("pane"), str) else None
    no_id = ("id minted in the pane" if claim.get("via") == VIA_TMUX
             else "id not known yet")
    return Holder(sid, pid, pane, proc.started if proc is not None else None, no_id)


def check(seat: str, environ: Mapping[str, str] | None = None, *, direct: bool,
          launcher: int | None = None, table: Callable[[], dict] | None = None) -> Check:
    """Who holds SEAT now, from ONE process-table read: the live claim, then
    the newest live sidecar naming SEAT, each unless this launch replaces it
    (`holders.replacing`: DIRECT path, own pane, not an ancestor). Reads
    only. A table that raises or comes back empty, or a claim that exists
    and cannot be read, is `unmeasured`; an unparseable claim is stale."""
    try:
        procs = (table or _procs.table)()
    except Exception as error:   # noqa: BLE001 - fail closed on ANY failure, loudly
        return Check(None, f"the process table could not be read ({error})")
    if not procs:
        return Check(None, "the process table came back empty (`ps` failed?)")
    try:
        claim = holders.live_claim(seat, procs, environ, direct=direct, launcher=launcher)
    except holders.ClaimUnreadable as error:
        return Check(None, str(error), procs)
    if claim is not None:
        return Check(_claim_holder(claim, procs, environ), procs=procs)
    door = holders.survey(
        seat, environ, table=lambda: procs,
        exempt=lambda session, own, table_: holders.replacing(
            session.pid, session.tmux, own, table_, direct=direct, launcher=launcher))
    if door.unmeasured:
        return Check(None, door.unmeasured, procs)
    if door.held:
        newest = door.holders[0]
        began = newest.started_at or door.started.get(newest.pid)
        return Check(Holder(newest.id or None, newest.pid, holders.pane_of(newest.tmux),
                            began), procs=procs)
    return Check(None, procs=procs)


def claim_fields(session_id: str | None, pid: int, procs: Mapping,
                 environ: Mapping[str, str] | None = None, via: str = VIA_BARE) -> dict:
    """The seat's claim for PID: its `procStart` from PROCS, the session
    id (None when not known yet), the pane, and the reader-only fields."""
    start = holders.proc_start_of(pid, procs)
    if start is None:
        raise OSError(f"pid {pid} is not in the process table")
    return {"sessionId": session_id, "pid": pid, "procStart": start,
            "pane": holders.own_pane(environ), "host": socket.gethostname().split(".")[0],
            "claimedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "via": via}


def holder_phrase(holder: Holder, now: float | None = None) -> str:
    """`session <full id> (pid 78996, tmux %4, live 23m)`."""
    now = time.time() if now is None else now
    who = f"session {holder.session_id}" if holder.session_id else f"a session ({holder.no_id})"
    where = [f"pid {holder.pid}"] if holder.pid is not None else []
    if holder.pane:
        where.append(f"tmux {holder.pane}")
    if holder.started:
        where.append(f"live {fmt_duration(now - holder.started)}")
    return f"{who} ({', '.join(where)})" if where else who


def taken_line(seat: str) -> str:
    return f"this session is {seat}, the default seat on this host (box {seat})"


def held_line(seat: str, holder: Holder, now: float | None = None) -> str:
    return (f"starting plain: the default seat {seat} is held by "
            f"{holder_phrase(holder, now)}, {WILL_NOT_HEAR}; that one does")


def unsure_line(seat: str, why: str) -> str:
    return (f"starting plain: could not check who holds the default seat {seat} "
            f"({why}), {WILL_NOT_HEAR}")


def gate_line(seat: str, failed: Gate) -> str:
    """The bare line for a failed gate: `LINUX`, `NO_BOX` or `UNBOXED`."""
    if failed.failed == "LINUX":
        return (f"starting plain: the default seat {seat} is macOS-only for now: "
                f"{NO_LINUX_YET}, {WILL_NOT_HEAR}")
    if failed.failed == "NO_BOX":
        return (f"starting plain: box {seat} is missing, {WILL_NOT_HEAR}; "
                f"repair: message setup --box {seat}")
    return f"starting plain: could not check box {seat} ({failed.why}), {WILL_NOT_HEAR}"


def dry_run_line(seat: str, said: str | None) -> str:
    """The dry run's `seat:` line: a live launch takes the seat (SAID is
    None), or starts plain and says SAID."""
    if said is None:
        return (f"seat:     {seat}, the default seat: a live launch takes it "
                f"(box {seat} found; no live holder)")
    return f"seat:     not taken: a live launch starts plain and says: {said}"


def user_agent_file(agent: Agent) -> str | None:
    """AGENT's file when it is a USER's file; None for no file and for the
    built-in, whose path is the package's and moves with the install, so
    it counts as no agent file (idle, converse, the settings layer and the
    birth record read it as bare)."""
    return agent.file if agent.file and not agent.builtin else None


# ---------- the other roads (do-004): named launches, tmux, a '-' resume --------

def named_gate_line(seat: str, failed: Gate, holds: bool) -> str:
    """A NAMED seat-word launch's line for a failed gate (Q16b). HOLDS: it
    took the claim all the same (N1, ruled (B)), so the line says it holds
    the seat and cannot hear yet, and why; otherwise it took none (Q17's
    case beside a failed gate)."""
    if failed.failed == "UNBOXED":
        head = (f"this session is {seat} and holds the default seat, but may not hear yet"
                if holds else f"this session is {seat}")
        return (f"{head}: could not check box {seat} ({failed.why}), so this session "
                "may not hear Delta messages or calls")
    head = (f"this session is {seat} and holds the default seat, but cannot hear yet"
            if holds else f"this session is {seat}")
    if failed.failed == "LINUX":
        return (f"{head}: the default seat {seat} is macOS-only for now: "
                f"{NO_LINUX_YET}, {WILL_NOT_HEAR}")
    return f"{head}: box {seat} is missing, {WILL_NOT_HEAR}; repair: message setup --box {seat}"


def named_unsure_line(seat: str, why: str) -> str:
    """Q17: a NAMED seat-word launch whose holder check cannot run launches
    as named, takes no claim, and says so."""
    return (f"this session is {seat}: could not check who holds the default seat {seat} "
            f"({why}), so it takes no claim and may be a second holder")


class _HolderView(NamedTuple):
    """A seat holder as `holders.Door` reads one (a sidecar's fields), so
    the door's own refusal and override lines name it."""

    id: str
    pid: int | None
    tmux: str | None
    started_at: float | None
    no_id: str


def door_for(seat: str, holder: Holder, now: float | None = None) -> holders.Door:
    """The front door's verdict built from the SEAT's holder (the claim or a
    sidecar, under the narrow rule), so a refused named launch prints the
    door's own line (Q3). A holder with no id yet reads as HELD does (B3)."""
    view = _HolderView(holder.session_id or "", holder.pid, holder.pane, holder.started,
                       holder.no_id)
    return holders.Door(seat, (view,), time.time() if now is None else now, {})


def resumed_line(seat: str, session_id: str, holder: Holder | None,
                 now: float | None = None) -> str:
    """`RESUMED`: a '-' resume of a session born as the seat takes nothing.
    It names the live holder when there is one (B8: the same read-only check
    as the dry run), else how to take the seat back."""
    head = (f"resuming plain: session {session_id[:8]} was started as {seat}, the default "
            f"seat, and a resume through '-' does not take it, {WILL_NOT_HEAR}")
    if holder is not None:
        return f"{head}; the default seat {seat} is held now by {holder_phrase(holder, now)}"
    return (f"{head}; 'session start {seat} --resume {session_id}' takes it back "
            "while it is free")


def born_as_seat(seat: str, session_id: str, environ: Mapping[str, str] | None = None) -> bool:
    """True when SESSION_ID's birth record says it was born as the seat:
    agent SEAT and no project+role seat. A session with no record (the vendor's
    own, or born before birth records) was not."""
    from sessiontools import settings as _settings
    try:
        agent, role_seat = _settings.identity(session_id, environ)
    except (OSError, ValueError):
        return False
    return agent == seat and not role_seat


def repoint(seat: str, launcher_claim: Mapping, pane_pid: int | None, pane_id: str,
            session_id: str | None, environ: Mapping[str, str] | None = None,
            table: Callable[[], dict] | None = None) -> str | None:
    """The tmux path: move the seat's claim from this launcher to the new
    pane's process, under the seat's lock, but only while the claim still
    names THIS launcher (its pid and procStart: nobody else could take the
    seat while it lives). Returns None when done, else why not (B4: the
    caller says so; the seat then reads free until the pane's session
    writes its sidecar)."""
    if not pane_pid or not pane_id:
        return "tmux printed no pane pid or id for the new window"
    with holders.locked(seat, environ, wait=SEAT_LOCK_WAIT_S) as lock:
        if not lock.ok:
            return lock.why
        try:
            claim = holders.read_claim(seat, environ)
        except holders.ClaimUnreadable as error:
            return str(error)
        if not claim or (claim.get("pid"), claim.get("procStart")) != (
                launcher_claim.get("pid"), launcher_claim.get("procStart")):
            return "the claim no longer names this launcher"
        try:
            procs = (table or _procs.table)()
        except Exception as error:   # noqa: BLE001 - say why, never raise past a placed pane
            return f"the process table could not be read ({error})"
        start = holders.proc_start_of(pane_pid, procs)
        if start is None:
            return f"the pane's pid {pane_pid} is not running"
        fields = {**claim, "sessionId": session_id, "pid": pane_pid, "procStart": start,
                  "pane": pane_id, "via": VIA_TMUX}
        fields.pop("v", None)
        fields.pop("agent", None)
        try:
            holders.write_claim(seat, fields, environ)
        except OSError as error:
            return f"the claim could not be written ({error.strerror or error})"
    return None


def repoint_failed_line(seat: str, why: str) -> str:
    """B4: the re-point failed after the window opened."""
    return (f"placed, but the default seat {seat}'s claim could not move to the new pane "
            f"({why}); the seat reads free until that session writes its sidecar")
