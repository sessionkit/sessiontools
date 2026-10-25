"""`session start`: launch a coding-agent session from an agent markdown file.

    session start                          the default seat, when it is free
    session start <agent>                  launch an agent by name
    session start --dry-run <agent>        show what would run; launch nothing
    session start --idle <agent>           boot self-directed, run no generators
    session start --converse <agent>       tell the agent this session can speak
    session start --in-tmux <agent>        in a new tmux window, not in place
    session start <agent> review the auth  trailing words = the first message

The agent's own front matter IS the launch configuration: native keys reach
the vendor untouched, `claude:` renders to argv by one rule, and the
launcher's own keys live under `start:`. Front matter supplies defaults;
the command line overrides them.

THE PORT. This package is the old launcher (session-20300609 `81e5662`,
`skills/session/scripts/session-start`) ported as package code, well
structured, in the right place. Library first:

    from sessiontools.start import parse_args, prepare, execute
    plan = prepare(parse_args(["--dry-run", "alice"]))   # a LaunchPlan
    print(render_dry_run_report(plan))

THE BRIDGE IS OFF THE ROAD. Every part is ported: tmux placement
(`placement`), resume (`resume`, planned by
`plan.resolve`/`mint`/`assemble`, so `tm session resume` runs the port)
and declared secrets (`secrets`: `pass`, the 0600 file, the
shred trap). Nothing here execs the old launcher any more, and the
package does not carry it.

THE FRONT DOOR (`holders`). A hand launch - no claude
`-n`/`--name` - of an agent a live Claude Code session already holds is
refused before anything is minted, naming the holder and its age;
`--allow-second-holder` launches anyway, saying so. A session in the
launcher's own tmux pane (`$TMUX_PANE`) is being replaced, not doubled
(a `respawn-pane -k`), and holds nothing.

THE DEFAULT SEAT (`default_seat`). A bare `session start` - no
agent named - takes the host's one default seat, named for the vendor, when
no live session holds it: the gates first (macOS, then the box), then a
claim under the seat's lock, written before the exec. Held, or a gate
failed, or the check could not run: it starts plain, as before, with one
line saying why and that it will not hear Delta. It never refuses for the
seat's sake. `session start -` is always plain (a `-` resume of a session
born as the seat says it took nothing: `RESUMED`). A NAMED launch of the
seat word meets the seat's check in place of the front door: a hand launch
that meets a holder is refused (exit 3), `-n` starts plain, and a free seat
is claimed even when a gate failed (`named_seat_start`). On the tmux path
the claim moves to the new pane's process after placement.

THE HOME-FOLDER OFFER (`homefolder`). At a terminal, a launch typed in
the home folder on macOS is asked whether to start in `~/work` instead
(made first when it is missing). A yes is as if `--directory` named it.
It comes after the front door, or after the seat's check and claim with
the seat's lock let go (a question waits on a person), and before mint.

Exit codes: 0 ok, 1 launch failed, 2 usage or a refusal, 3 the agent is
already held (the front door, or the default seat's check for a named
launch of its word), 130 Ctrl-C at the home-folder question.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from typing import NamedTuple

from sessiontools.start import default_seat, holders
from sessiontools.start.agents import NO_AGENT, list_agent_rows, list_agents
from sessiontools.start.args import parse_args
from sessiontools.start.errors import (
    EXIT_HELD,
    EXIT_INTERRUPTED,
    EXIT_LAUNCH_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    PROG,
    LaunchError,
)
from sessiontools.start.holders import enforce, front_door
from sessiontools.start.homefolder import home_folder_notice, offer_work_folder
from sessiontools.start.identity import SessionIdentity
from sessiontools.start.placement import (
    check_placeable,
    parse_tmux_target,
    place_in_tmux,
    receipt,
    should_switch,
)
from sessiontools.start.plan import (
    KEYED_NEEDS_AN_AGENT,
    Launch,
    LaunchPlan,
    assemble,
    mint,
    prepare,
    resolve,
)
from sessiontools.start.run import (
    default_window_name,
    execute,
    render_dry_run_report,
    report_generator_failures,
)


def main(argv: list[str]) -> int:
    """`session start ARGV`. Returns the exit code; a live launch does not
    return (the process becomes the vendor)."""
    argv = list(argv)
    try:
        args = parse_args(argv)
    except SystemExit as stop:          # argparse: usage, the trap, -h
        return stop.code if isinstance(stop.code, int) else EXIT_USAGE
    if args.list_agents:
        # Before anything else: a question about the agents dir, answered
        # from it, plus the default seat's built-in when the folder does not
        # shadow it: `NAME<TAB>built in` (Q12), so field 1 is always a name.
        for name, mark in list_agent_rows():
            print(f"{name}\t{mark}" if mark else name)
        return EXIT_OK
    try:
        in_tmux = bool(args.in_tmux or args.tmux_target)
        session, window = parse_tmux_target(args.tmux_target)
        switch = False
        if in_tmux:
            # Both tmux usage refusals BEFORE anything is minted or written.
            # `--switch --no-switch` first: it is wrong wherever it was
            # typed, while "nowhere to place" also depends on the
            # environment, so the more specific complaint wins.
            switch = should_switch(args.switch, args.no_switch)
            check_placeable(session)
        seat = door = None
        try:
            if args.agent is None or args.agent == default_seat.word():
                # The seat word, bare or named: the seat's own check (the
                # claim, then the sidecars, under the narrow own-pane rule)
                # stands in for the front door. A bare start is never refused
                # for it; a named one meets Q3's refusal (exit 3). The
                # home-folder offer rides inside: after the check and the
                # claim, with the seat's lock let go, before mint.
                seat = (bare_start if args.agent is None else named_seat_start)(args, in_tmux)
                launch, minted, noticed = seat.launch, seat.minted, seat.noticed
            else:
                launch = resolve(args)
                # THE FRONT DOOR: after resolve (it names the agent),
                # before mint - a refusal leaves nothing minted, written or placed.
                door = front_door(launch.agent.name, launch.cli_args, launch.environ)
                if not args.dry_run:
                    enforce(door, args.allow_second_holder, warn=_warn)
                elif door is not None and door.unmeasured:
                    _warn(f"the second-holder check could not run: {door.unmeasured}")
                if args.agent == NO_AGENT:
                    _resumed(launch, in_tmux)
                # THE HOME-FOLDER OFFER: after the front door (a refused launch
                # is asked nothing), before mint (Ctrl-C at the question leaves
                # nothing minted). Only at a terminal, in the home folder itself.
                launch, noticed = _offer(launch)
                minted = mint(launch)
        except _Interrupted:
            print(file=sys.stderr)
            return EXIT_INTERRUPTED
        plan = assemble(launch, minted)
        # The directory is final here (a resume's recorded cwd included):
        # one line before any of the three endings, the dry run too, unless
        # the offer already printed it.
        notice = None if noticed else home_folder_notice(plan.work_dir, launch.environ)
        if notice is not None:
            _warn(notice)
        if in_tmux:
            window = window or default_window_name(plan.agent, plan.vendor)
        if args.dry_run:
            report_generator_failures(plan)
            if seat is not None and seat.line is not None:
                holder = seat.line
            else:
                holder = door.report_line(args.allow_second_holder) if door else None
            print(render_dry_run_report(plan, tmux=(session, window) if in_tmux else None,
                                        switch=switch, holder=holder), end="")
            return EXIT_OK
        if in_tmux:
            report_generator_failures(plan)
            placed = place_in_tmux(plan, session, window, switch)
            if seat is not None:
                _repoint(seat, plan, placed)
            print(receipt(plan, placed))
            return EXIT_OK
        execute(plan)
    except LaunchError as error:
        print(f"{PROG}: {error}", file=sys.stderr)
        return error.code
    return EXIT_OK  # unreachable after a successful exec


def _warn(line: str) -> None:
    print(f"{PROG}: {line}", file=sys.stderr)


class _Interrupted(Exception):
    """Ctrl-C at the home-folder question, and only there: `main` exits 130."""


def _offer(launch: Launch) -> tuple[Launch, bool]:
    """THE HOME-FOLDER OFFER, as every road asks it: the launch (swapped to
    the work folder on a yes) and whether the home-folder notice already
    printed. Ctrl-C at the question becomes `_Interrupted`, so `main` exits
    130 for that Ctrl-C alone."""
    try:
        return offer_work_folder(launch)
    except KeyboardInterrupt:
        raise _Interrupted from None


class SeatStart(NamedTuple):
    """What a seat-word launch resolved to: the launch and its mint, the dry
    run's `seat:` line (None on a live launch), the claim this launch
    WROTE, if any (the tmux path re-points it at the new pane), and whether
    the home-folder offer already printed the notice."""

    launch: Launch
    minted: object
    line: str | None = None
    claim: dict | None = None
    noticed: bool = False


Offer = Callable[[Launch], tuple[Launch, bool]]


def bare_start(args: argparse.Namespace, in_tmux: bool,
               environ: Mapping[str, str] | None = None,
               warn: Callable[[str], None] = _warn,
               offer: Offer = _offer) -> SeatStart:
    """A bare `session start`: the default seat, or plain with ONE line.

    The order: a bare `--keyed` is
    refused first, exit 2, before any gate runs; then the gates (platform,
    box), which write nothing; then, under the seat's lock, the check (the
    claim, then the sidecars) and, on a free seat, the claim. The lock is
    let go there: the claim, naming this pid, is what holds the seat from
    then on. Then resolve as the seat, the home-folder offer (`offer`: a
    question waits on a person, so never under the lock), the mint, the
    claim again with the minted id, and `TAKEN`. Anything else starts plain
    with ONE line, and is offered the work folder the same way, before its
    mint. A dry run takes no lock, writes no claim and asks nothing: it
    reads the same things and reports what a live launch would do. The
    claim names this pid: the direct path execs in place, so it is the
    session's own; the tmux path re-points it at the pane after placement
    (`main`)."""
    environ = os.environ if environ is None else environ
    if args.keyed:
        raise LaunchError(KEYED_NEEDS_AN_AGENT, code=EXIT_USAGE)
    seat = default_seat.word()
    as_seat = argparse.Namespace(**{**vars(args), "agent": seat})
    direct = not in_tmux
    passed = default_seat.gate(seat, environ)
    if args.dry_run:
        said = None
        if not passed.ok:
            said = default_seat.gate_line(seat, passed)
        else:
            found = default_seat.check(seat, environ, direct=direct)
            if found.unmeasured:
                said = default_seat.unsure_line(seat, found.unmeasured)
            elif found.holder is not None:
                said = default_seat.held_line(seat, found.holder)
        launch = resolve(as_seat if said is None else args, environ=environ)
        return SeatStart(launch, mint(launch), default_seat.dry_run_line(seat, said))
    if not passed.ok:
        warn(default_seat.gate_line(seat, passed))
        return _plain(args, environ, offer)
    claimed = None
    with holders.locked(seat, environ, wait=default_seat.SEAT_LOCK_WAIT_S) as lock:
        if not lock.ok:
            said = default_seat.unsure_line(seat, lock.why)
        else:
            found = default_seat.check(seat, environ, direct=direct)
            said = (default_seat.unsure_line(seat, found.unmeasured) if found.unmeasured
                    else default_seat.held_line(seat, found.holder)
                    if found.holder is not None else None)
            if said is None:
                try:
                    fields = default_seat.claim_fields(
                        None, os.getpid(), found.procs, environ)
                except OSError as error:
                    # A bare start never fails for the seat's sake: no
                    # claim, and it starts plain with ONE line.
                    claimed = _unwritten(error)
                else:
                    claimed = _claim(seat, fields, environ)
                if isinstance(claimed, str):
                    said, claimed = default_seat.unsure_line(seat, claimed), None
    if claimed is None:
        warn(said)
        return _plain(args, environ, offer)
    taken = _finish(seat, resolve(as_seat, environ=environ), claimed, None,
                    environ, warn, offer)
    warn(default_seat.taken_line(seat))
    return taken


def named_seat_start(args: argparse.Namespace, in_tmux: bool,
                     environ: Mapping[str, str] | None = None,
                     warn: Callable[[str], None] = _warn,
                     offer: Offer = _offer) -> SeatStart:
    """A NAMED launch of the seat word (`session start <word> [--resume
    ID]`, with or without the vendor's `-n`): the seat's own check decides,
    never the front door's survey alone, because the claim counts (a
    holder still in its start race) and the narrow own-pane rule applies.

    THE ORDER, in this one place (N6):

    1. resolve (pure: every refusal as today, before anything is written);
    2. the gates (platform, box): they decide the LINE only;
    3. a FAILED gate first reads who holds the seat, read-only - no lock,
       no mkdir (N4) - so a held seat, or one that cannot be checked,
       writes nothing;
    4. otherwise, under the seat's lock, the check again and, on a FREE
       seat, the claim (N1, ruled (B): a failed gate takes it too, and its
       line says it holds the seat but cannot hear yet; on Linux as well,
       so no second holder appears once sessionmail can read Linux
       start times);
    5. with the lock let go, the home-folder offer (`offer`), then the
       mint, then the claim again with the minted id. A launch refused in
       3 or 4 is asked nothing.

    What a HELD seat means (Q3, Q16a, N2, N3: one line, the holder's):
    `--allow-second-holder` starts beside it with the door's override line
    and takes no claim; a hand launch is refused, exit 3, with the door's
    line; a spawner's `-n` launch starts plain with `HELD`. A check that
    cannot run launches as named with the named `UNSURE` line and takes no
    claim (Q17); beside a failed gate, both lines print (B2)."""
    environ = os.environ if environ is None else environ
    seat = default_seat.word()
    launch = resolve(args, environ=environ)
    hand = not holders.names_the_seat(launch.cli_args)
    allowed = bool(args.allow_second_holder)
    direct = not in_tmux
    passed = default_seat.gate(seat, environ)
    if args.dry_run:
        return _named_dry_run(seat, args, launch, passed, hand, allowed,
                              default_seat.check(seat, environ, direct=direct), environ)
    unsure = ""
    holder = claimed = resumed = None
    if not passed.ok:
        found = default_seat.check(seat, environ, direct=direct)
        holder, unsure = found.holder, found.unmeasured
    if holder is None and not unsure:
        with holders.locked(seat, environ, wait=default_seat.SEAT_LOCK_WAIT_S) as lock:
            if not lock.ok:
                unsure = lock.why
            else:
                found = default_seat.check(seat, environ, direct=direct)
                holder, unsure = found.holder, found.unmeasured
                if holder is None and not unsure:
                    # An unkeyed resume keeps its own id (the vendor picks
                    # it when none was named: `--continue`, or the picker,
                    # and the claim names none).
                    if launch.resume is not None:
                        resumed = launch.resume.session_id or None
                    via = (default_seat.VIA_NAMED_RESUME if launch.resume is not None
                           else default_seat.VIA_NAMED)
                    try:
                        fields = default_seat.claim_fields(
                            resumed, os.getpid(), found.procs, environ, via=via)
                    except OSError as error:
                        # Q17 holds for the fields as for the write: no
                        # claim, and the launch goes ahead as named.
                        claimed = _unwritten(error)
                    else:
                        claimed = _claim(seat, fields, environ)
                    if isinstance(claimed, str):
                        unsure, claimed = claimed, None
    if holder is not None:
        return _respect(seat, args, launch, holder, hand, allowed, environ, warn, offer)
    if claimed is not None:
        taken = _finish(seat, launch, claimed, resumed, environ, warn, offer)
        warn(default_seat.taken_line(seat) if passed.ok
             else default_seat.named_gate_line(seat, passed, holds=True))
        return taken
    if not passed.ok:
        warn(default_seat.named_gate_line(seat, passed, holds=False))
    warn(default_seat.named_unsure_line(seat, unsure))
    launch, noticed = offer(launch)
    return SeatStart(launch, mint(launch), noticed=noticed)


def _respect(seat: str, args: argparse.Namespace, launch: Launch, holder, hand: bool,
             allowed: bool, environ: Mapping[str, str],
             warn: Callable[[str], None], offer: Offer = _offer) -> SeatStart:
    """A named seat-word launch that met a live holder: beside it (the
    flag), refused (a hand launch, exit 3, asked nothing), or plain with
    `HELD` (`-n`). A launch that goes ahead is offered the work folder
    before its mint."""
    door = default_seat.door_for(seat, holder)
    if allowed:
        warn(door.override())
        launch, noticed = offer(launch)
        return SeatStart(launch, mint(launch), noticed=noticed)
    if hand:
        raise LaunchError(door.refusal(), code=EXIT_HELD)
    warn(default_seat.held_line(seat, holder))
    return _plain(argparse.Namespace(**{**vars(args), "agent": NO_AGENT}), environ, offer)


def _named_dry_run(seat: str, args: argparse.Namespace, launch: Launch, passed, hand: bool,
                   allowed: bool, found, environ: Mapping[str, str]) -> SeatStart:
    """What a live named seat-word launch WOULD do, read-only (N7)."""
    if found.holder is not None:
        door = default_seat.door_for(seat, found.holder)
        if allowed:
            line = f"seat:     a live launch would start ANYWAY: {door.override()}"
        elif hand:
            line = f"seat:     a live launch would REFUSE (exit {EXIT_HELD}): {door.refusal()}"
        else:
            plain = _plain(argparse.Namespace(**{**vars(args), "agent": NO_AGENT}), environ)
            said = default_seat.held_line(seat, found.holder)
            return plain._replace(line=default_seat.dry_run_line(seat, said))
    elif found.unmeasured:
        said = ([default_seat.named_gate_line(seat, passed, holds=False)]
                if not passed.ok else [])
        said.append(default_seat.named_unsure_line(seat, found.unmeasured))
        line = ("seat:     not checked: a live launch starts as named, takes no claim and "
                f"says: {' / '.join(said)}")
    elif passed.ok:
        line = default_seat.dry_run_line(seat, None)
    else:
        line = (f"seat:     {seat}, the default seat: a live launch takes it and says: "
                f"{default_seat.named_gate_line(seat, passed, holds=True)}")
    return SeatStart(launch, mint(launch), line)


def _no_offer(launch: Launch) -> tuple[Launch, bool]:
    return launch, False


def _plain(args: argparse.Namespace, environ: Mapping[str, str],
           offer: Offer = _no_offer) -> SeatStart:
    """Resolve as ARGS name it, offer the work folder, mint."""
    launch, noticed = offer(resolve(args, environ=environ))
    return SeatStart(launch, mint(launch), noticed=noticed)


def _claim(seat: str, fields: dict, environ: Mapping[str, str]) -> dict | str:
    """Under the seat's lock, a free seat: write the claim for this pid.
    Returns the claim, or why it could not be written (the caller starts
    plain, or launches as named: nothing is resolved or minted for the
    seat then). This write, before the mint, is what closes the start
    race; a refusal or a Ctrl-C after it ends this launcher, and the claim
    goes stale with its pid."""
    try:
        holders.write_claim(seat, fields, environ)
    except OSError as error:
        return _unwritten(error)
    return fields


def _unwritten(error: OSError) -> str:
    """Why the seat's claim could not be written, for the caller's line."""
    return f"the claim could not be written ({error.strerror or error})"


def _finish(seat: str, launch: Launch, fields: dict, session_id: str | None,
            environ: Mapping[str, str], warn: Callable[[str], None],
            offer: Offer) -> SeatStart:
    """After the claim, with the seat's lock let go: the home-folder offer,
    the mint, then the claim again with the minted id."""
    launch, noticed = offer(launch)
    minted = mint(launch)
    claim = _record_id(seat, fields, minted, session_id, fields["pid"], environ, warn)
    return SeatStart(launch, minted, None, claim, noticed)


def _record_id(seat: str, fields: dict, minted, session_id: str | None, pid: int,
               environ: Mapping[str, str], warn: Callable[[str], None]) -> dict:
    """Rewrite the claim with the minted id, when the mint made one here (a
    pane mint's id is born in the pane, and an unkeyed resume keeps its
    own). It runs after the claim's lock was let go (the home-folder
    question sits between them), so it takes the lock again and writes only
    while the claim still names this launcher, as the tmux re-point does.
    Returns the claim as it stands."""
    if not isinstance(minted, SessionIdentity) or minted.session_id == session_id:
        return fields
    rewritten = {**fields, "sessionId": minted.session_id}
    why = None
    with holders.locked(seat, environ, wait=default_seat.SEAT_LOCK_WAIT_S) as lock:
        if not lock.ok:
            why = lock.why
        else:
            try:
                now = holders.read_claim(seat, environ)
            except holders.ClaimUnreadable as error:
                now, why = None, str(error)
            if why is None and (not now or (now.get("pid"), now.get("procStart")) != (
                    fields.get("pid"), fields.get("procStart"))):
                why = "the claim no longer names this launcher"
            if why is None:
                try:
                    holders.write_claim(seat, rewritten, environ)
                except OSError as error:
                    why = error.strerror or str(error)
    if why is not None:
        # The first claim stands: it holds the seat by this pid; only the
        # id is missing, and a reader finds it from the sidecar.
        warn(f"the default seat's claim could not record the session id "
             f"({why}); it still holds the seat by pid {pid}")
        return fields
    return rewritten


def _resumed(launch: Launch, in_tmux: bool, warn: Callable[[str], None] = _warn) -> None:
    """`RESUMED`: a '-' resume of a session born as the seat says it takes
    nothing, naming the live holder if one exists (B8, read-only). A '-'
    resume with no id (`--continue`, the picker) has no record to read."""
    if launch.resume is None or not launch.resume.session_id:
        return
    seat = default_seat.word()
    sid = launch.resume.session_id
    if not default_seat.born_as_seat(seat, sid, launch.environ):
        return
    found = default_seat.check(seat, launch.environ, direct=not in_tmux)
    warn(default_seat.resumed_line(seat, sid, found.holder))


def _repoint(start: SeatStart, plan: LaunchPlan, placed,
             warn: Callable[[str], None] = _warn) -> None:
    """The tmux path: the claim moves from this launcher to the new pane."""
    if start.claim is None:
        return
    seat = default_seat.word()
    sid = plan.identity.session_id if plan.identity is not None else None
    why = default_seat.repoint(seat, start.claim, placed.pane_pid, placed.pane_id, sid,
                               start.launch.environ)
    if why:
        warn(default_seat.repoint_failed_line(seat, why))


__all__ = [
    "EXIT_HELD",
    "EXIT_INTERRUPTED",
    "EXIT_LAUNCH_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "LaunchError",
    "LaunchPlan",
    "assemble",
    "execute",
    "list_agents",
    "main",
    "mint",
    "parse_args",
    "prepare",
    "render_dry_run_report",
    "resolve",
]
