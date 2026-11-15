"""The identity block: who a launched session is, written once at launch.

A session learns on its first turn, with no tool call, its id, seat,
nickname, pane, seat-mates, whether it holds the seat's calls, and whether
it has a signing key. ONE file says so:

    <session dir>/identity.md        0600, written atomically (tmp + rename)

`<session dir>` is the session resolver's (`settings.session_dir`): every
reader goes through the resolver or the `session identity` verb, never a
path of its own.

WHO WRITES IT: the launch script, by running this module on the line after
the launch record and before the vendor (`render_identity_call`). In the
pane every fact is real: the id, `$TMUX_PANE`, the seat's claim and the live
sidecars. A placed pane runs `session start` itself and claims the seat for
itself, so there is nothing to wait for.

NEVER A GUESS. Each line says what it does not know, and why. The facts that
change after launch (call holder, seat-mates) are labelled "at launch", and
the block ends with the verb that re-checks them.

NEVER ARGV. The block reaches the agent through the session-start context,
never a command line: `ps` is world-readable. The writer's own command line
carries the facts the launcher already holds (seat, name, keyed), which a
launch's argv carried before it.

A block must never cost a session: any failure here is one stderr line and
exit 0, and the vendor still starts.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from sessiontools import claude_code, identity_block
from sessiontools import procs as _procs
from sessiontools.start import default_seat, holders
from sessiontools.start.vendor import render_argv_token

PROG = "session start: identity block"
MODULE = "sessiontools.start.boot_identity"
#: `-c`, not `-m`: under `-I` the module is imported from the installed
#: package, never from the session's working directory.
ENTRY = f"import sys; from {MODULE} import main; sys.exit(main())"

#: The reader's name for the file (`sessiontools.identity_block`): one name.
IDENTITY_FILE = identity_block.IDENTITY_FILE

HEADING = "# Who you are (at launch)"
RECHECK = "Seat-mates and call holder change after launch: re-check with `session list`."

#: The Key line. Only the unknown branch's wording changes once the
#: signing-key default is decided.
KEY_MINTED = "yes (minted at launch)"
KEY_UNKNOWN = "unknown (signing-key default pending a decision)"

NO_SEAT = "none (no seat)"
NO_NAME = "none (no name given)"
NO_PANE = "none (not in tmux)"
NOT_APPLICABLE = "n/a (no seat)"

#: The address a spawner hands a seat down by, when no seat was named.
ADDRESS_ENV = "TM_AGENT_ADDRESS"

SHORT = 8

#: The most lines the block may have (mail prints it verbatim).
MAX_LINES = 20


class Facts(NamedTuple):
    """The block's facts, each already worded: `holder` and `mates` are the
    values of their lines (`holder_value`, `mates_value`)."""

    session_id: str
    seat: str | None
    nickname: str | None
    pane: str | None
    holder: str
    mates: str
    keyed: bool


def _clean(value: str) -> str:
    """VALUE on one line, with no control character (no ANSI escape, no
    newline): the block is plain text, one fact per line."""
    return "".join("?" if ord(ch) < 0x20 or ord(ch) == 0x7f else ch for ch in value)


def render(facts: Facts) -> str:
    """The block, exactly: the heading, one fact per line in a fixed order
    with fixed labels, then the re-check line."""
    lines = [
        HEADING,
        "",
        f"- **Session ID**: {_clean(facts.session_id)}",
        f"- **Seat**: {_clean(facts.seat) if facts.seat else NO_SEAT}",
        f"- **Nickname**: {_clean(facts.nickname) if facts.nickname else NO_NAME}",
        f"- **Pane**: {_clean(facts.pane) if facts.pane else NO_PANE}",
        f"- **Call holder** (at launch): {_clean(facts.holder)}",
        f"- **Seat-mates** (at launch): {_clean(facts.mates)}",
        f"- **Key**: {KEY_MINTED if facts.keyed else KEY_UNKNOWN}",
        "",
        RECHECK,
    ]
    return "\n".join(lines) + "\n"


# ---------- the facts --------------------------------------------------------------------

def unknown(why: str) -> str:
    return f"unknown ({why})"


def holder_value(seat: str | None, holder: default_seat.Holder | None, *, mine: bool,
                 unmeasured: str = "") -> str:
    """The Call holder line's value: `yes`, `no (held by session <short id>)`,
    `n/a (no seat)`, or `unknown (<why>)`."""
    if not seat:
        return NOT_APPLICABLE
    if unmeasured:
        return unknown(unmeasured)
    if holder is None:
        return unknown("no live claim names a holder")
    if mine:
        return "yes"
    if holder.session_id:
        return f"no (held by session {holder.session_id[:SHORT]})"
    where = f", pid {holder.pid}" if holder.pid is not None else ""
    return f"no (held by a session whose {holder.no_id}{where})"


class Mate(NamedTuple):
    session_id: str
    pane: str | None


def mates_value(seat: str | None, mates: Sequence[Mate], unmeasured: str = "") -> str:
    """The Seat-mates line's value: `none`, or the count, then each mate's
    short id and pane; `n/a (no seat)`, or `unknown (<why>)`."""
    if not seat:
        return NOT_APPLICABLE
    if unmeasured:
        return unknown(unmeasured)
    if not mates:
        return "none"
    shown = ", ".join(f"{m.session_id[:SHORT]} ({m.pane or 'no pane'})" for m in mates)
    return f"{len(mates)}: {shown}"


def address_of(session_dir: Path) -> str | None:
    """The address a session's launch handed down (`TM_AGENT_ADDRESS` in its
    launch record's env, which records that name in clear), or None."""
    try:
        text = (session_dir / "launch" / "env").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    prefix = f"{ADDRESS_ENV}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):] or None
    return None


def seat_of(session_id: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The seat another session took, by the rule this block's own Seat line
    uses: its birth record's seat, else its agent's when that agent's claim
    names this session (the front-door claim `session list` reads), else the
    default seat word when it was born as that agent, else the address its
    launch handed down. None for none."""
    from sessiontools import settings as _settings
    try:
        agent, seat = _settings.identity(session_id, environ)
    except (OSError, ValueError):
        agent = seat = None
    if seat:
        return seat
    if agent and _claim_names(agent, session_id, environ):
        return agent
    if agent and agent == default_seat.word():
        return agent
    return address_of(_settings.session_dir(session_id, environ))


def _claim_names(agent: str, session_id: str,
                 environ: Mapping[str, str] | None = None) -> bool:
    """Whether AGENT's claim records SESSION_ID as its session. Reads the one
    claim file; a claim that is missing, unreadable or names another session
    (or none yet) says no. The caller lists live sessions only, and a claim
    names its session's own pid, so no liveness read is needed here."""
    try:
        claim = holders.read_claim(agent, environ)
    except (OSError, ValueError):       # ClaimUnreadable, or a name no claim may have
        return False
    return bool(claim) and claim.get("sessionId") == session_id


def seat_mates(seat: str, session_id: str, procs: Mapping,
               environ: Mapping[str, str] | None = None) -> list[Mate]:
    """The other LIVE sessions on SEAT, from the sidecars (live by
    `procs.same_process`, the one liveness rule), oldest first."""
    env = dict(environ) if environ is not None else None
    found = []
    for session in claude_code.sessions(env, dict(procs)):
        if not session.id or session.id == session_id:
            continue
        if not _procs.same_process(session.pid, session.proc_start, dict(procs)):
            continue
        if seat_of(session.id, environ) == seat:
            found.append(session)
    found.sort(key=lambda s: s.started_at or 0.0)
    return [Mate(s.id, holders.pane_of(s.tmux)) for s in found]


def gather(session_id: str, seat: str | None, nickname: str | None, *, keyed: bool,
           self_pids: Iterable[int] = (),
           environ: Mapping[str, str] | None = None,
           table: Callable[[], dict] = _procs.table) -> Facts:
    """The facts, measured where the script runs. SEAT is the launcher's
    (`--seat`, the seat whose claim the launch wrote, or the default seat
    word: `plan.launch_seat`), else the address handed down in
    ENVIRON; the pane is ENVIRON's `$TMUX_PANE`. SELF_PIDS are this
    session's own processes (the script's shell, and on the tmux path its
    parent): a claim naming one of them is this session's. One process-table
    read serves both the holder and the seat-mates."""
    environ = os.environ if environ is None else environ
    seat = seat or environ.get(ADDRESS_ENV) or None
    pane = environ.get("TMUX_PANE") or None
    if not seat:
        return Facts(session_id, None, nickname, pane, holder_value(None, None, mine=False),
                     mates_value(None, ()), keyed)
    try:
        procs = table()
    except Exception as error:   # noqa: BLE001 - say why, never raise past a launch
        procs, why = {}, f"the process table could not be read ({error})"
    else:
        why = "" if procs else "the process table came back empty (`ps` failed?)"
    if why:
        return Facts(session_id, seat, nickname, pane, unknown(why), unknown(why), keyed)
    try:
        found = default_seat.check(seat, environ, direct=False, table=lambda: procs)
    except ValueError as error:
        found = default_seat.Check(None, str(error))
    mine = found.holder is not None and (
        found.holder.pid in set(self_pids) or found.holder.session_id == session_id)
    holder = holder_value(seat, found.holder, mine=mine, unmeasured=found.unmeasured)
    try:
        mates = mates_value(seat, seat_mates(seat, session_id, procs, environ))
    except OSError as error:
        mates = unknown(f"the sidecars could not be read ({error.strerror or error})")
    return Facts(session_id, seat, nickname, pane, holder, mates, keyed)


# ---------- the write ----------------------------------------------------------------------

def identity_path(session_dir: str | os.PathLike) -> Path:
    return Path(session_dir) / IDENTITY_FILE


def write_block(session_dir: str | os.PathLike, text: str) -> Path:
    """Write TEXT to `<session_dir>/identity.md`: a temp file in the same
    directory, 0600, then a rename, so a reader sees all of it or none. The
    session dir must exist; this never makes one. Raises OSError."""
    path = identity_path(session_dir)
    fd, tmp = tempfile.mkstemp(prefix=f".{IDENTITY_FILE}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ---------- the launcher's half: the script line ----------------------------------------

def name_token(argv: Sequence[object]):
    """The value of the vendor's `-n`/`--name` in ARGV, in any spelling
    (`-n X`, `-nX`, `--name X`, `--name=X`), as the argv holds it: a str.
    None when there is none.
    The scan stops at `--`: what follows is the first message."""
    tokens = list(argv)
    for i, token in enumerate(tokens):
        if not isinstance(token, str):
            continue
        if token == "--":
            return None
        if token in holders.NAME_FLAGS:
            return tokens[i + 1] if i + 1 < len(tokens) else None
        for prefix in ("--name=", "-n"):
            if token.startswith(prefix) and not (prefix == "-n" and token.startswith("--")):
                return token[len(prefix):]
    return None


def render_identity_call(session_dir: str, argv: Sequence[object], *, seat: str | None,
                         keyed: bool, python: str | None = None) -> str:
    """The script lines that write the identity block: this interpreter,
    isolated (`-I`), `--dir` exactly as the launch record's, the name as the
    vendor gets it, and the script's own pid (`$$`). `|| :` - a block never
    stops a launch."""
    python = python or sys.executable
    parts = [shlex.quote(python), "-I", "-c", shlex.quote(ENTRY), "--dir", session_dir]
    if seat:
        parts += ["--seat", shlex.quote(seat)]
    token = name_token(argv)
    if isinstance(token, str):
        parts.append("--name=" + render_argv_token(token))
    if keyed:
        parts.append("--keyed")
    parts += ["--self-pid", '"$$"']
    return "\n".join([
        (f"# identity block: who this session is, at launch -> <session dir>/{IDENTITY_FILE} "
         "(0600, never argv)"),
        " ".join(parts) + " || :",
    ])


# ---------- the script's entry ---------------------------------------------------------

def parse_identity_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=MODULE, add_help=True)
    parser.add_argument("--dir", required=True, help="the session directory")
    parser.add_argument("--seat", default=None)
    parser.add_argument("--name", default=None)
    parser.add_argument("--keyed", action="store_true")
    parser.add_argument("--self-pid", type=int, action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """The script's entry. Always exit 0: a missing block is a stderr line,
    never a session that did not start."""
    try:
        args = parse_identity_args(list(sys.argv[1:] if argv is None else argv))
        environ = os.environ if environ is None else environ
        session_id = environ.get("CLAUDE_SESSION_ID") or os.path.basename(
            os.path.normpath(args.dir))
        facts = gather(session_id, args.seat, args.name or None, keyed=args.keyed,
                       self_pids=args.self_pid, environ=environ)
        write_block(args.dir, render(facts))
    except SystemExit:
        print(f"{PROG}: bad arguments; not written", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - a block never costs a session
        print(f"{PROG}: not written ({type(error).__name__}: {error})", file=sys.stderr)
    return 0


__all__ = [
    "IDENTITY_FILE",
    "Facts",
    "Mate",
    "gather",
    "holder_value",
    "identity_path",
    "main",
    "mates_value",
    "name_token",
    "render",
    "render_identity_call",
    "seat_mates",
    "seat_of",
    "write_block",
]


if __name__ == "__main__":
    sys.exit(main())
