"""`session respawn [SID] [--go]`: restart a seat in its own pane, with the
`session start` line it was launched with.

    session respawn            the seat in THIS pane ($TMUX_PANE)
    session respawn 0041271d   that seat, in whatever pane it is in

With no `--go` it prints the plan and changes nothing. The line it replays
is the seat's launch RECORD (`<session dir>/launch/command`): the
`session start` line as it was entered, never tmux's `pane_start_command`,
which tmux re-quotes. Only a seat started with `session start` has one.

It refuses rather than guesses, exit 2 with one line saying why:

- THE PANE. `$TMUX_PANE` is the answer, never inferred: an untargeted tmux
  query answers with the ACTIVE pane, which is the human's. It is checked
  ONCE, on the ambient server ($TMUX): the first ancestor of this process
  that is a pane's own process must be that pane. Both halves come from one
  server, so pane ids of two servers are never compared.
- THE SEAT. The fresh `session list` rows, matched on the (socket, pane)
  PAIR: `%3` on another server is not this `%3`. Exactly one seat.
- A SID is a full session id or a prefix of 8+ characters, never a name
  (`reviewer` matches `reviewer-2` too). An ambiguous prefix lists its
  matches.
- THE RECORD. No `launch_command` (a pre-change seat, a keyed resume fork,
  or not started by `session start`); a line that is not `session start`;
  one carrying `--session-id` or a resume token (the new seat would start
  on the old one's id); a `launch_dir` that is gone; no `session` on PATH;
  a hidden `--first-message-file` the pane would refuse.

The pane runs `pane.pane_line(pane.pane_argv(entered, F))`, the derivation
`session start` itself uses: placement flags off (a placed seat respawns IN
PLACE) and bare first-message words moved to a new 0600 file F.

`--go` acts, on the caller's OWN pane only (super ruling 3: the ancestry
check is the only guard against killing a neighbour, and it cannot hold for
another seat's pane, so `SID --go` on any other pane refuses). In order:

1. `respawn.log` (the tools log dir) is opened, 0600. It cannot be opened:
   refused, and nothing else is touched. The log is the only trace an act
   leaves once it has killed its own caller.
2. The new first-message file, when the line carries bare words.
3. The log line, written BEFORE the kill. A write that fails takes the
   message file back and refuses. (Opening first and writing after the file
   means a line is never logged for an act that a failed file stopped.)
4. `set-option -p remain-on-exit failed` on the pane, a SEPARATE call: a
   tmux without `failed` degrades with one stderr line, and the respawn
   still runs.
5. `exec` into `tmux respawn-pane -k`, the hand-off passed by `-e`, so the
   respawned seat records the ORIGINAL entered line. Exec, because the pane
   is about to be killed with this process in it.

Exit codes: 0 the plan was printed (an own-pane `--go` does not return: it
becomes tmux); 2 refused (usage included); 1 tmux could not be exec'd.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sessiontools import settings
from sessiontools.listing import Row
from sessiontools.procs import Proc

PROG = "session respawn"
LOG_FILE = "respawn.log"
#: The shortest SID prefix taken: the `short` that `session list` prints.
MIN_SID = 8
#: What a session id is made of. Anything else is a name, and refused.
SID_CHARS = re.compile(r"^[0-9a-f-]+$")
SESSION_ID_FLAG = "--session-id"
#: In the plan's tmux row, an `-e` value is shown as this, never spelled:
#: SESSION_START_ENTERED holds the entered line, words and all.
ELIDED = "…"

EPILOG = """\
With no SID the target is the seat in this tmux pane ($TMUX_PANE), checked
once against this process's own ancestry on the same tmux server. A SID is
a full session id or a prefix of at least 8 characters (see `session list`),
never a name. The line replayed is the seat's launch record, the
`session start` line as it was entered; a seat not started by
`session start` has none and is refused. Without --go nothing is touched.

--go acts on THIS pane only: it writes one line to respawn.log in the tools
log dir (refusing if it cannot), then respawns the pane with `tmux
respawn-pane -k`. A SID naming another seat's pane is planned, never acted
on: acting on another seat is not built.

Exit codes: 0 the plan was printed; 2 refused, with one line saying why;
1 tmux could not be run.
"""
#: Super ruling 3 (Mon 2026-11-02), the reason verbatim.
NOT_BUILT = "acting on another seat is not built; see the plan"
#: A `set-option` or `display-message` call is quick; a hung tmux is not.
TMUX_TIMEOUT_S = 5


class Refused(Exception):
    """One line saying why; exit 2."""


@dataclass
class World:
    """Everything respawn reads from outside, as seams for tests. The
    defaults are the real host: the environment, a FRESH `listing.rows()`
    (never the snapshot: a young one can name a recycled pane), one `ps`,
    one tmux server's panes, PATH, this process's pid and the transcript's
    model."""

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    rows: Callable[[], list[Row]] | None = None
    table: Callable[[], dict[int, Proc]] | None = None
    panes: Callable[[str | None], dict[int, str]] | None = None
    #: A command on the PATH the respawned pane gets: the tmux client's
    #: (`tmux_environ`), which drops the caller's venv.
    which: Callable[[str], str | None] | None = None
    pid: int = field(default_factory=os.getpid)
    model: Callable[[Row], str | None] | None = None
    #: One tmux call that returns (set-option, display-message).
    run: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None
    #: The act's last step: become tmux. The default never returns.
    execvpe: Callable[[str, list[str], Mapping[str, str]], object] = os.execvpe
    now: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        env = dict(self.environ)
        if self.which is None:
            path = tmux_environ(env).get("PATH")
            self.which = lambda name: shutil.which(name, path=path)
        if self.run is None:
            def run(argv: Sequence[str]) -> subprocess.CompletedProcess:
                return subprocess.run(list(argv), capture_output=True, text=True,
                                      env=tmux_environ(env), timeout=TMUX_TIMEOUT_S,
                                      check=False)
            self.run = run
        if self.rows is None:
            from sessiontools import listing
            self.rows = lambda: listing.rows(env)
        if self.table is None:
            from sessiontools import procs
            self.table = procs.table
        if self.panes is None:
            from sessiontools import tmux
            self.panes = tmux.panes
        if self.model is None:
            from sessiontools import window

            def live_model(row: Row) -> str | None:
                return window.of(row.session.id, row.session.cwd, env).model
            self.model = live_model


# ---------- 1. the target: (socket, pane) and the one seat in it -------------------------------

@dataclass(frozen=True)
class Target:
    row: Row
    socket: str            # the `-L` name
    pane: str              # `%N`
    how: str               # how the pane was found, for the plan
    own: bool = False      # the caller's own pane, confirmed by ancestry: --go may act


def socket_dir(environ: Mapping[str, str]) -> str:
    """tmux's socket folder, the one `tmux.sockets` reads: every `-L` name
    is a file in it."""
    base = environ.get("TMUX_TMPDIR") or "/tmp"
    return os.path.realpath(os.path.join(base, f"tmux-{os.getuid()}"))


def socket_path(name: str, environ: Mapping[str, str]) -> str:
    """The resolved path of the `-L NAME` server: `/tmp` and `/private/tmp`
    are one folder, so one server never splits in two."""
    return os.path.realpath(os.path.join(socket_dir(environ), name))


def ambient(environ: Mapping[str, str]) -> tuple[str, str | None, str]:
    """(the resolved socket path, its `-L` name or None, `$TMUX_PANE`) of the
    server and pane this process runs in. Refused when either is unset: an
    untargeted tmux query would answer with the ACTIVE pane, the human's."""
    pane = environ.get("TMUX_PANE") or ""
    if not pane:
        raise Refused("$TMUX_PANE is unset or empty: no pane to respawn "
                      "(run it in the seat's own pane, or give a SID)")
    tmux_env = environ.get("TMUX") or ""
    raw = tmux_env.rsplit(",", 2)[0] if tmux_env else ""
    if not raw:
        raise Refused("not inside tmux: $TMUX is unset or empty")
    path = os.path.realpath(raw)
    name = os.path.basename(path) if os.path.dirname(path) == socket_dir(environ) else None
    return path, name, pane


def own_pane(world: World, rows: list[Row]) -> Target:
    """The seat in this pane: `$TMUX_PANE`, confirmed by ONE same-socket
    ancestry check, then the one row on that (socket, pane) pair."""
    path, name, pane = ambient(world.environ)
    panes = world.panes(name)
    procs = world.table()
    from sessiontools.procs import ancestors
    found = next((panes[p] for p in ancestors(world.pid, procs) if p in panes), None)
    where = name or path
    if found is None:
        raise Refused(f"$TMUX_PANE is {pane}, but no ancestor of this process is a pane's "
                      f"process on {where}: the pane is not confirmed")
    if found != pane:
        raise Refused(f"$TMUX_PANE is {pane}, but this process runs in {found} on {where}: "
                      "the two disagree")
    here = [r for r in rows
            if r.tmux_pane == pane and r.tmux_socket
            and socket_path(r.tmux_socket, world.environ) == path]
    if not here:
        raise Refused(f"no live seat in pane {pane} on {where}")
    if len(here) > 1:
        raise Refused(f"ambiguous: {len(here)} live seats in pane {pane} on {where}: "
                      + listed(here))
    return Target(row=here[0], socket=name or path, pane=pane,
                  how="by $TMUX_PANE, confirmed by process ancestry", own=True)


def listed(rows: list[Row]) -> str:
    return "; ".join(f"{r.short} {r.name} ({r.tmux_pane or 'no pane'})" for r in rows)


def by_sid(world: World, rows: list[Row], sid: str) -> Target:
    """The seat whose id starts with SID: exactly one, and in a pane. A SID
    naming the caller's own seat goes through `own_pane` too, and the two
    must agree."""
    query = sid.lower()
    if not SID_CHARS.match(query):
        raise Refused(f"{sid!r} is not a session id: give a session id, or a prefix of "
                      f"at least {MIN_SID} characters; see `session list`")
    if len(query) < MIN_SID:
        raise Refused(f"{sid!r} is too short: a session id prefix needs at least "
                      f"{MIN_SID} characters; see `session list`")
    matches = [r for r in rows if r.session.id.lower().startswith(query)]
    if not matches:
        raise Refused(f"no live seat has a session id starting {sid!r} "
                      f"({len(rows)} live; see `session list`)")
    if len(matches) > 1:
        raise Refused(f"ambiguous: {len(matches)} live seats start {sid!r}: " + listed(matches))
    row = matches[0]
    if not row.tmux_pane or not row.tmux_socket:
        raise Refused(f"{row.short} {row.name} is not in tmux: no pane to respawn")
    if _is_own(world, row):
        own = own_pane(world, rows)
        if own.row.session.id != row.session.id:
            raise Refused(f"{sid!r} names this seat, but pane {own.pane} holds "
                          f"{own.row.short} {own.row.name}: the two disagree")
        return Target(row=row, socket=own.socket, pane=own.pane,
                      how=f"by session id {row.short}, this seat's own pane: "
                          "$TMUX_PANE, confirmed by process ancestry", own=True)
    return Target(row=row, socket=row.tmux_socket, pane=row.tmux_pane,
                  how=f"by session id {row.short}, from the seat's own ancestry "
                      "(session list); plan only")


def _is_own(world: World, row: Row) -> bool:
    """ROW is the caller's own seat: its id is this session's, or its
    (socket, pane) pair is this process's ambient one."""
    from sessiontools.claude_code import session_id
    if session_id(dict(world.environ)) == row.session.id:
        return True
    pane = world.environ.get("TMUX_PANE") or ""
    raw = (world.environ.get("TMUX") or "").rsplit(",", 2)[0]
    return bool(pane and raw and row.tmux_pane == pane and row.tmux_socket
                and socket_path(row.tmux_socket, world.environ) == os.path.realpath(raw))


# ---------- 2-3. the record, its refusals, and the line the pane runs ---------------------------

@dataclass(frozen=True)
class Plan:
    target: Target
    record: str                    # the launch record's path
    entered: str                   # launch_command, as recorded
    launch_dir: str
    pane_line: str
    message: str                   # the message row
    model: str                     # the model row
    log: str
    tmux_argv: list[str]
    #: The first message the act writes to `message_file` (None: no words).
    #: Never printed: the plan shows the path, not the words.
    message_body: str | None = field(default=None, repr=False)
    message_file: str | None = None
    token: str | None = None


def record_path(row: Row, environ: Mapping[str, str]) -> Path:
    return settings.session_dir(row.session.id, environ) / "launch" / "command"


def log_path(environ: Mapping[str, str]) -> Path:
    """`respawn.log` in the tools log dir, beside reap's (super N1)."""
    return settings.log_dir(settings.PACKAGES["session"], environ) / LOG_FILE


def read_command(row: Row, environ: Mapping[str, str]) -> str:
    """`launch_command` through the launch record's reader, which has no
    fallback. A seat with none was not started by `session start`. ENVIRON
    is the one `record_path` names, so the plan's record row is the file
    read."""
    from sessiontools.get import GetError, launch_value
    try:
        return launch_value(row, "launch_command", environ)
    except GetError:
        raise Refused(f"{row.short} {row.name} has no launch_command recorded: it was not "
                      "started by `session start` (or is a pre-change seat, or a keyed "
                      "resume's fork); kill the window and kick it instead") from None


def read_dir(row: Row, environ: Mapping[str, str]) -> str:
    """`launch_dir`, read after the line has split: an unclosed quote in the
    line swallows the record's next key, and the line is the cause."""
    from sessiontools.get import GetError, launch_value
    try:
        directory = launch_value(row, "launch_dir", environ)
    except GetError:
        raise Refused(f"{row.short} {row.name} has no launch_dir recorded: "
                      "nowhere to respawn it from") from None
    if not os.path.isdir(directory):
        raise Refused(f"launch_dir {directory} is gone or not a directory: "
                      "the pane would start somewhere else")
    return directory


def entered_argv(line: str) -> list[str]:
    """The recorded line's argv after `session start`, or a refusal: the
    line must split, be `session start`, and carry no session id."""
    try:
        words = shlex.split(line)
    except ValueError as error:
        raise Refused(f"the recorded line does not split ({error}): {line!r}") from None
    if words[:2] != ["session", "start"]:
        raise Refused(f"the recorded line is not `session start`: {line!r}")
    entered = words[2:]
    if any(w == SESSION_ID_FLAG or w.startswith(SESSION_ID_FLAG + "=") for w in entered):
        raise Refused(f"the recorded line carries {SESSION_ID_FLAG}: the new seat would "
                      "start on the old seat's id")
    from sessiontools.start.resume import detect_resume
    # claude's spellings, over the whole line: whatever the vendor, a
    # recorded resume is a record that is wrong.
    resume = detect_resume("claude", entered)
    if resume is not None:
        raise Refused(f"the recorded line carries a resume ({' '.join(resume.tokens)}): the "
                      "new seat would resume the old one; a resume is never recorded")
    return entered


def message_file_flag(entered: list[str]) -> str | None:
    """The hidden `--first-message-file PATH` in the launcher prefix, if any."""
    from sessiontools.start.pane import MESSAGE_FILE_FLAG, split_prefix
    prefix, _tail = split_prefix(entered)
    for index, token in enumerate(prefix):
        if token == MESSAGE_FILE_FLAG and index + 1 < len(prefix):
            return prefix[index + 1]
        if token.startswith(MESSAGE_FILE_FLAG + "="):
            return token.split("=", 1)[1]
    return None


def typed_model(entered: list[str]) -> str | None:
    """A `--model` typed in the vendor passthrough (the last one), else None."""
    from sessiontools.start.pane import split_prefix
    from sessiontools.start.vendor import split_rest
    _prefix, tail = split_prefix(entered)
    vendor = split_rest(tail[1:])[1] if tail else []
    found = None
    for index, token in enumerate(vendor):
        if token == "--model" and index + 1 < len(vendor):
            found = vendor[index + 1]
        elif token.startswith("--model="):
            found = token.split("=", 1)[1]
    return found


def model_row(typed: str | None, live: str | None) -> str:
    """ONE line (ruling 2): the drift when the line's typed pin and the live
    model differ; never a refusal. The record is the authority."""
    now = live or "unknown"
    if typed is None:
        return (f"live model {now}; the line types no --model, so the respawn "
                "resolves it at launch, as a fresh `session start` does")
    if typed != live:
        return f"live model {now}, the respawn will start with {typed}"
    return f"{typed}, as typed in the line and live now"


def respawn_argv(socket: str, pane: str, directory: str, line: str,
                 env: Mapping[str, str]) -> list[str]:
    """The `tmux respawn-pane -k` call (design.md §4): the line as ONE
    argument, run in DIRECTORY, with the hand-off ENV passed by `-e`."""
    argv = ["tmux", "-L", socket, "respawn-pane", "-k", "-t", pane, "-c", directory]
    for name, value in env.items():
        argv += ["-e", f"{name}={value}"]
    return argv + [line]


def render_tmux(argv: list[str]) -> str:
    """ARGV as a shell would need it typed, every `-e` value elided."""
    shown = [f"{a.split('=', 1)[0]}={ELIDED}" if index and argv[index - 1] == "-e" else a
             for index, a in enumerate(argv)]
    return " ".join(a if a.endswith("=" + ELIDED) else shlex.quote(a) for a in shown)


def plan(world: World, sid: str | None) -> Plan:
    """Every read and every refusal; nothing is written."""
    from sessiontools.start import pane as _pane
    rows = world.rows()
    target = by_sid(world, rows, sid) if sid is not None else own_pane(world, rows)
    row = target.row
    line = read_command(row, world.environ)
    entered = entered_argv(line)
    directory = read_dir(row, world.environ)
    if world.which("session") is None:
        raise Refused("`session` is not on this PATH: the respawned pane gets this "
                      "client's PATH, and would die with 127")
    existing = message_file_flag(entered)
    if existing is not None:
        from sessiontools.start.errors import LaunchError
        try:
            _pane.read_message_file(existing)
        except LaunchError as error:
            raise Refused(f"the recorded line's {error}") from None
    words = _pane.message_words(entered)
    token = None
    if words:
        token = _pane.new_token()
        new_file = _pane.message_file_path(token, world.environ)
        message = f"{new_file} (a new 0600 file, written by --go)"
    else:
        new_file = None
        message = (f"{existing} (checked: ours, 0600)" if existing is not None else "none")
    pane_line = _pane.pane_line(_pane.pane_argv(entered, new_file))
    handoff = _pane.handoff_env(entered, directory)
    return Plan(target=target, record=str(record_path(row, world.environ)), entered=line,
                launch_dir=directory, pane_line=pane_line, message=message,
                model=model_row(typed_model(entered), world.model(row)),
                log=str(log_path(world.environ)),
                tmux_argv=respawn_argv(target.socket, target.pane, directory, pane_line,
                                       handoff),
                message_body=_pane.message_text(words) if words else None,
                message_file=new_file, token=token)


# ---------- 4-5. the act (--go) ----------------------------------------------------------------

def tmux_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment every tmux client here runs under: the
    caller's, less its venv and credentials. The respawned pane gets this
    client's PATH, so `session` is looked up on it too."""
    from sessiontools.start.environment import host_environ
    return host_environ(environ)


def log_line(p: Plan, now: float) -> str:
    """One tab-separated line, reap's shape: time, short, name, agent,
    socket:pane, launch_dir, the PANE line (never the entered one: no bare
    first-message word reaches the log) and who acted."""
    from datetime import datetime
    row = p.target.row
    when = datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
    fields = [when, row.short, row.name or "-", row.session.agent or "-",
              f"{p.target.socket}:{p.target.pane}", p.launch_dir, p.pane_line, "own pane"]
    return "\t".join(" ".join(str(f).split()) for f in fields) + "\n"


def open_log(path: str) -> int:
    """`respawn.log`, opened for append (0600 when it is new), or a refusal:
    an act that kills its own caller leaves no other trace."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise Refused(f"{path} cannot be written ({error.strerror or error}): --go refused, "
                      "the pane is untouched") from None


def write_message(world: World, p: Plan) -> str | None:
    """The new 0600 first-message file at the path the plan names, or None
    when the line carries no bare words."""
    if p.message_body is None or p.token is None:
        return None
    from sessiontools.start import pane as _pane
    try:
        path = _pane.write_message_file(p.message_body, p.token, world.environ)
    except OSError as error:
        raise Refused(f"the first-message file {p.message_file} cannot be written "
                      f"({error.strerror or error}): --go refused, the pane is untouched"
                      ) from None
    if path != p.message_file:           # the pane line names p.message_file
        _pane.discard_message_file(path)
        raise Refused(f"the first-message file landed at {path}, not {p.message_file}: "
                      "--go refused, the pane is untouched")
    return path


def append(fd: int, text: str) -> None:
    data = text.encode("utf-8")
    while data:
        written = os.write(fd, data)
        data = data[written:]


def keep_failed_corpse(world: World, p: Plan) -> None:
    """`remain-on-exit failed` on the pane, its OWN call: a tmux without
    `failed` would stop a `;` batch before the respawn. A refusal degrades
    with one stderr line; the respawn still runs."""
    from sessiontools.start.placement import REMAIN_ON_EXIT
    argv = ["tmux", "-L", p.target.socket, "set-option", "-p", "-t", p.target.pane,
            "remain-on-exit", REMAIN_ON_EXIT]
    try:
        result = world.run(argv)
        detail = (result.stderr or "").strip() if result.returncode != 0 else None
    except (OSError, subprocess.TimeoutExpired) as error:
        detail = str(error)
    if detail is not None:
        print(f"{PROG}: respawning, but could not set remain-on-exit {REMAIN_ON_EXIT} "
              f"({detail or 'no stderr'}); a respawn that fails will close the pane "
              "with its reason", file=sys.stderr)


def environment_lines(world: World, p: Plan) -> list[str]:
    """The env rule: the respawned pane resolves in tmux's environment,
    not this caller's. The own pane's server is the ambient one, which is
    the server `placement.environment_warnings` asks. Advisory only."""
    try:
        result = world.run(["tmux", "-L", p.target.socket, "display-message", "-p",
                            "-t", p.target.pane, "#{session_name}"])
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    from sessiontools.start.placement import environment_warnings
    return environment_warnings(result.stdout.strip(), world.environ)


def act(world: World, p: Plan) -> int:
    """`--go`: log, message file, remain-on-exit, then exec into tmux. The
    caller's own pane only (ruling 3)."""
    if not p.target.own:
        raise Refused(f"{p.target.row.short} {p.target.row.name} is in {p.target.pane} on "
                      f"{p.target.socket}, not this pane: {NOT_BUILT}")
    from sessiontools.start import pane as _pane
    fd = open_log(p.log)
    message_file = None
    try:
        message_file = write_message(world, p)
        try:
            append(fd, log_line(p, world.now()))
        except OSError as error:
            raise Refused(f"{p.log} cannot be written ({error.strerror or error}): --go "
                          "refused, the pane is untouched") from None
    except BaseException:
        _pane.discard_message_file(message_file)
        raise
    finally:
        os.close(fd)
    keep_failed_corpse(world, p)
    for line in environment_lines(world, p):
        print(f"{PROG}: {line}", file=sys.stderr)
    print(f"{PROG}: respawning {p.target.pane} on {p.target.socket} (logged in {p.log})",
          file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    from sessiontools import tmux
    argv = [tmux.TMUX_BIN, *p.tmux_argv[1:]]
    try:
        # Past here this process is tmux, and then it is killed with the
        # pane. If tmux refuses the respawn, the new message file stays
        # behind: harmless, a planned sweep of `launches/` removes it.
        world.execvpe(argv[0], argv, tmux_environ(world.environ))
    except OSError as error:
        _pane.discard_message_file(message_file)
        print(f"{PROG}: tmux could not be run ({error.strerror or error}); "
              f"{p.log} names an act that did not happen", file=sys.stderr)
        return 1
    return 0                               # only a test's execvpe returns


# ---------- 6. the plan ------------------------------------------------------------------------

def render(p: Plan) -> str:
    row = p.target.row
    rows = [
        ("seat", f"{row.short}  {row.name}  (agent {row.session.agent or '-'})"),
        ("pane", f"{p.target.pane} on {p.target.socket}, {p.target.how}"),
        ("record", p.record),
        ("entered", p.entered),
        ("dir", p.launch_dir),
        ("pane line", p.pane_line),
        ("message", p.message),
        ("model", p.model),
        ("log", p.log),
        ("tmux", render_tmux(p.tmux_argv)),
    ]
    return "\n".join([f"{PROG}: plan, nothing touched (--go acts)",
                      *(f"  {k:10} {v}" for k, v in rows)])


# ---------- the verb ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """`session respawn`'s parser. No `--session-id` flag: the SID is
    positional (super ruling 1)."""
    p = argparse.ArgumentParser(prog=PROG, allow_abbrev=False, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Restart a seat in its own tmux pane with the "
                                            "`session start` line it was launched with. "
                                            "A plan unless --go.")
    p.add_argument("sid", nargs="?", default=None, metavar="SID",
                   help="that seat: a session id, or a prefix of at least 8 characters "
                        "(default: the seat in this pane)")
    p.add_argument("--go", action="store_true",
                   help="act: log, then respawn THIS pane (a SID naming another "
                        "seat's pane is refused)")
    return p


def main(argv: list[str], world: World | None = None) -> int:
    """`session respawn`. WORLD is the seam for tests."""
    a = build_parser().parse_args(argv)
    world = world if world is not None else World()
    try:
        p = plan(world, a.sid)
        if a.go:
            return act(world, p)
    except Refused as why:
        print(f"{PROG}: {why}", file=sys.stderr)
        return 2
    print(render(p))
    return 0


__all__ = ["Plan", "Refused", "Target", "World", "build_parser", "main", "plan", "render"]
