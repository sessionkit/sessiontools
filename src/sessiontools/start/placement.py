"""Tmux placement: the launch in a new tmux window instead of in place.

    session start --in-tmux alice                     a window in this session
    session start --tmux-target work:alice alice      a window in session `work`
    session start --tmux-target work --switch alice   ...and move the client there
    session start --in-tmux alice   (outside tmux)    a window in session `agents`

The pane runs the ENTERED line again: `session start` with the
placement flags taken off, and any bare first-message words moved to a
0600 file (`pane`). No launch script is frozen to disk, so a `respawn-pane
-k` starts a fresh seat. The pane's run is an ordinary in-place launch:
mint, files, claim, record, exec. The launcher that places it mints
nothing and takes no claim; it refuses what needs no act, writes the
message file, and opens the window.

Four rules, the first three measured in the old launcher before it became
one:

- EVERY `-t` IS ANCHORED with `=`: tmux matches a target by
  prefix, so an unanchored `alpha` lands in an existing `alpha-beta`. After
  `new-window` the window is addressed by its `@id`, which cannot collide.
- SWITCHING FOLLOWS THE CALLER: a human at a terminal inside tmux
  is moved to the new window by default; an agent's launch (no tty) never
  moves anyone's view. `--switch` and `--no-switch` override, not both.
- `new-window` IS THE HANDOVER. Before it, a failure removes the message
  file (nothing will ever read it). After it the pane owns it, so a
  failure there (a window option, switch-client) is reported and nothing
  is unwound: the session IS placed.
- A PANE THAT FAILS STAYS (`remain-on-exit failed`): a refused pane, or a
  server whose PATH has no `session`, leaves its reason readable in the
  dead pane instead of closing on it.

A PLACED PANE RESOLVES IN TMUX'S ENVIRONMENT (review F1, ruled): its
variables are the tmux server's global environment, the session's own
over it, and the hand-off; not the caller's. So a caller's `AGENTS_DIR=/x
session start --in-tmux pip` finds `pip` where tmux's `AGENTS_DIR` says.
The pane's own resolution and claim are the authority; the launcher's
check is advisory. `environment_warnings` gives the launcher ONE line per
RESOLUTION_VARS name the caller set to something tmux's environment does
not hold, and never refuses.

Every tmux command runs under `host_environ()`: the SERVER may be born from
this very call, and a server born inside this package's venv would hand
that venv to every pane it ever creates.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools import tmux as _tmux
from sessiontools.start.environment import host_environ, without_session_identity
from sessiontools.start.errors import EXIT_USAGE, PROG, LaunchError
from sessiontools.start.pane import discard_message_file, temp_window_name
from sessiontools.start.run import run_tmux

#: The session a placement with no target lands in when the caller is not
#: inside tmux: one fixed name, so a second launch from a plain terminal
#: adds a window beside the first instead of a session per agent.
DEFAULT_TMUX_SESSION = "agents"

#: A pane that exits non-zero STAYS, showing its own stderr and tmux's
#: `Pane is dead (status N, ...)`: a refused pane (a held agent, a missing
#: message file) or a shell that cannot find `session` (status 127) leaves
#: a readable reason. A pane that exits 0 closes.
#: Measured on tmux 3.8; a tmux without the `failed` value refuses the
#: option, and the placement says so and goes on (`place_pane`).
REMAIN_ON_EXIT = "failed"


#: What a pane resolves its agent, its state and its sessions by. A caller
#: whose value differs from tmux's is told, once per name (review F1).
RESOLUTION_VARS = ("AGENTS_DIR", "SESSION_STATE_DIR", "SESSIONS_DIR")

#: The advisory read must never hold a launch up for long.
SHOW_ENVIRONMENT_TIMEOUT_S = 5


class TmuxPlacement(NamedTuple):
    """Where a tmux launch landed: what `receipt` prints."""

    session: str          # "" = the caller's current session
    window: str
    window_id: str        # tmux's own @id, the handle that cannot collide
    created_session: bool
    #: The new pane's id (`%12`) and the pid of the process it runs, as
    #: `new-window -P` printed them; "" and None when tmux printed none.
    pane_id: str = ""
    pane_pid: int | None = None


def parse_tmux_target(value: str | None) -> tuple[str, str]:
    """SESSION[:WINDOW] -> (session, window); either half may be empty."""
    if not value:
        return "", ""
    session, _sep, window = value.partition(":")
    return session, window


def default_session(session: str, environ: Mapping[str, str] | None = None) -> str:
    """The session a placement targets: SESSION as typed; with none typed
    and the caller outside tmux, DEFAULT_TMUX_SESSION, which `ensure_session`
    creates (detached, born the scrubbed way) or reuses. Inside tmux an
    empty SESSION stays empty: the window opens in the caller's session."""
    environ = os.environ if environ is None else environ
    if session or environ.get("TMUX"):
        return session
    return DEFAULT_TMUX_SESSION


def should_switch(switch: bool, no_switch: bool,
                  environ: Mapping[str, str] | None = None,
                  interactive: bool | None = None) -> bool:
    """Whether placement moves the caller's tmux client to the new window:
    by default only for an interactive human inside tmux. Both flags at once
    is a question with no answer, refused."""
    if switch and no_switch:
        raise LaunchError("--switch and --no-switch together: pick one", code=EXIT_USAGE)
    if no_switch:
        return False
    if switch:
        return True
    environ = os.environ if environ is None else environ
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    return bool(environ.get("TMUX")) and interactive


def check_placeable(session: str, environ: Mapping[str, str] | None = None) -> None:
    """Refuse a tmux launch with nowhere to go. Answerable from argv and the
    environment alone, so `main` asks before anything is written;
    `place_pane` asks again, so its contract does not depend on a caller
    having asked first."""
    environ = os.environ if environ is None else environ
    if not session and not environ.get("TMUX"):
        raise LaunchError("nowhere to place the window: not inside tmux and no "
                          "--tmux-target SESSION given", code=EXIT_USAGE)


def session_exists(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Is there a tmux session named exactly NAME (`=` anchored)? A non-zero
    has-session IS the answer (a missing server answers the same way), and
    so is tmux being absent: the create path then says so loudly."""
    try:
        result = _tmux.run(["has-session", "-t", "=" + name], env=host_environ(environ))
    except OSError:
        return False
    return result.returncode == 0


def ensure_session(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """The named session exists after this call. True if it was created.

    `new-session` may BIRTH the server, and a tmux server keeps its starter's
    environment as its global one for every pane it ever makes: so it gets
    the caller's environment WITHOUT the caller's session identity (a
    server born inside a seat handed the parent's ids to every later
    pane) and WITHOUT any credential (a probe measured a
    token-less client's later window still holding the starter's token).
    The identity goes here; `run_tmux` applies `host_environ`, which drops
    CREDENTIAL_VARS. Only here - `new-window` builds its pane from the
    server's."""
    environ = os.environ if environ is None else environ
    if session_exists(name, environ):
        return False
    run_tmux(["new-session", "-d", "-s", name], without_session_identity(environ))
    return True


def _show_environment(arguments: list[str], environ: Mapping[str, str]
                      ) -> dict[str, str | None] | None:
    """`tmux show-environment ARGUMENTS` as {name: value}, a removed name
    (`-NAME`) as None. None when tmux gives no answer: no server, no such
    session, no tmux, or one that hangs."""
    try:
        result = _tmux.run(["show-environment", *arguments], env=host_environ(environ),
                           timeout=SHOW_ENVIRONMENT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    shown: dict[str, str | None] = {}
    for line in result.stdout.splitlines():
        if line.startswith("-"):
            shown[line[1:]] = None
            continue
        name, sep, value = line.partition("=")
        if sep:
            shown[name] = value
    return shown


def pane_environment(session: str, environ: Mapping[str, str] | None = None
                     ) -> dict[str, str | None] | None:
    """The environment tmux builds a new pane in SESSION from: the server's
    global environment (`show-environment -g`), the session's own over it
    when the session exists ("" is the caller's current one). None when no
    server answers: a server this placement births is born from the
    caller's own environment, so there is nothing to differ from."""
    environ = os.environ if environ is None else environ
    found = _show_environment(["-g"], environ)
    if found is None:
        return None
    own = _show_environment(["-t", "=" + session] if session else [], environ)
    if own:
        found.update(own)
    return found


def environment_warnings(session: str, environ: Mapping[str, str] | None = None
                         ) -> list[str]:
    """One line per RESOLUTION_VARS name the caller set (non-empty) to a
    value tmux's environment for SESSION does not hold (`pane_environment`):
    a placed pane resolves in tmux's environment, not the caller's. A name
    the caller left unset or empty, or one that matches, gives none; so
    does a server that does not answer. Advisory: never a refusal."""
    environ = os.environ if environ is None else environ
    mine = {name: environ.get(name, "") for name in RESOLUTION_VARS}
    if not any(mine.values()):
        return []
    theirs = pane_environment(session, environ)
    if theirs is None:
        return []
    lines = []
    for name, value in mine.items():
        server = theirs.get(name) or ""
        if value and value != server:
            shown = shlex.quote(server) if server else "unset"
            lines.append(f"warning: {name} is {shlex.quote(value)} here but {shown} in "
                         "tmux's environment; a placed pane resolves in tmux's "
                         "environment, so the pane uses tmux's")
    return lines


def placement_sequence(line: str, cwd: str, session: str, window: str, token: str,
                       handoff: Mapping[str, str]) -> list[str]:
    """The ONE tmux call that places a pane, so nothing lands between the
    window and its options:

        new-window -d -P -F FMT -c CWD [-t =S:] -n ssp-TOKEN -e K=V... LINE
          ; set-option -p -t [=S:]ssp-TOKEN remain-on-exit failed
          ; rename-window -t [=S:]ssp-TOKEN WINDOW

    The temporary name is unique per launch, so its `-t` finds this window
    and no other; with no SESSION it is looked up in the caller's current
    session. A `set-option -p` with NO target would hit the caller's pane,
    not the new `-d` one (measured, tmux 3.8)."""
    temporary = temp_window_name(token)
    target = ["-t", "=" + session + ":"] if session else []
    named = ("=" + session + ":" if session else "") + temporary
    handed = [part for name, value in sorted(handoff.items())
              for part in ("-e", f"{name}={value}")]
    return ["new-window", "-d", "-P", "-F", NEW_WINDOW_FORMAT, "-c", cwd, *target,
            "-n", temporary, *handed, line,
            _tmux.SEPARATOR, "set-option", "-p", "-t", named, "remain-on-exit", REMAIN_ON_EXIT,
            _tmux.SEPARATOR, "rename-window", "-t", named, window]


def place_pane(line: str, cwd: str, session: str, window: str, token: str, switch: bool,
               handoff: Mapping[str, str], message_file: str | None = None,
               environ: Mapping[str, str] | None = None) -> TmuxPlacement:
    """Open a DETACHED window running LINE (the pane's own `session start`)
    in CWD, HANDOFF in its environment (`placement_sequence`). Returns where
    it landed; raises LaunchError, having removed MESSAGE_FILE, if it never
    got that far."""
    environ = os.environ if environ is None else environ
    try:
        created = False
        if session:
            created = ensure_session(session, environ)
        else:
            check_placeable(session, environ)
        try:
            result = _tmux.run(placement_sequence(line, cwd, session, window, token, handoff),
                               env=host_environ(environ))
        except FileNotFoundError:
            raise LaunchError("tmux is not installed (or not on PATH)") from None
        printed = result.stdout.split()
        if not printed or not printed[0].startswith("@"):
            detail = result.stderr.strip() or "(no stderr)"
            raise LaunchError(f"tmux new-window failed: {detail}")
    except BaseException:
        # The pane never ran: nothing will read the message file, so it goes.
        discard_message_file(message_file)
        raise
    # ---- past the handover: the pane owns the file and the launch ---------
    window_id, pane_id, pane_pid = _parse_new_window(printed)
    after = []
    if result.returncode != 0:
        # new-window ran and a later command of the sequence did not (a tmux
        # with no `remain-on-exit failed`): say so, and name the window.
        detail = result.stderr.strip() or "(no stderr)"
        print(f"{PROG}: placed, but could not set remain-on-exit {REMAIN_ON_EXIT} "
              f"({detail}); a pane that fails will close with its reason", file=sys.stderr)
        after.append(("name the window", ["rename-window", "-t", window_id, window]))
    after.append(("automatic-rename off",
                  ["set-window-option", "-t", window_id, "automatic-rename", "off"]))
    if created:
        # `new-session` made window 0, a bare shell, and `new-window -d`
        # left it current: make the agent's window the one an attach lands
        # on. No client is attached to a session just made, so nobody's
        # view moves.
        after.append(("select the new window", ["select-window", "-t", window_id]))
    if switch:
        after.append(("switch the client to it", ["switch-client", "-t", window_id]))
    for description, arguments in after:
        try:
            run_tmux(arguments, environ)
        except LaunchError as error:
            # Degraded, not failed: the agent is running.
            print(f"{PROG}: placed, but could not {description} ({error})", file=sys.stderr)
    return TmuxPlacement(session=session, window=window, window_id=window_id,
                         created_session=created, pane_id=pane_id, pane_pid=pane_pid)


#: What `new-window -P` prints: the window's @id, then the new pane's id and
#: the pid of its process.
NEW_WINDOW_FORMAT = "#{window_id} #{pane_id} #{pane_pid}"


def _parse_new_window(printed: list[str]) -> tuple[str, str, int | None]:
    """`@7 %12 4242` -> ("@7", "%12", 4242). A field tmux did not print is
    "" or None: the window id is what placement needs."""
    window_id = printed[0] if printed else ""
    pane_id = printed[1] if len(printed) > 1 and printed[1].startswith("%") else ""
    try:
        pane_pid = int(printed[2]) if len(printed) > 2 else None
    except ValueError:
        pane_pid = None
    return window_id, pane_id, pane_pid


#: The receipt's session id: the pane mints it, after this launcher exits.
PANE_MINTS = "id minted in the pane"


def receipt(who: str, placement: TmuxPlacement,
            environ: Mapping[str, str] | None = None) -> str:
    """The one line a placement prints: who, which window (and its @id),
    where, and that the pane mints the session id. A caller outside tmux is
    never switched to the window, so its line ends with how to reach it; a
    caller inside tmux gets the line unchanged."""
    environ = os.environ if environ is None else environ
    if placement.session:
        created = " (created)" if placement.created_session else ""
        where = f"session '{placement.session}'{created}"
    else:
        where = "the current session"
    line = (f"{PROG}: placed {who} in tmux window {placement.window!r} "
            f"({placement.window_id}) of {where} -- session {PANE_MINTS}")
    if placement.session and not environ.get("TMUX"):
        line += f"; attach: tmux attach -t {shlex.quote(placement.session)}"
    return line


__all__ = [
    "DEFAULT_TMUX_SESSION",
    "NEW_WINDOW_FORMAT",
    "PANE_MINTS",
    "REMAIN_ON_EXIT",
    "RESOLUTION_VARS",
    "TmuxPlacement",
    "check_placeable",
    "default_session",
    "ensure_session",
    "environment_warnings",
    "pane_environment",
    "parse_tmux_target",
    "place_pane",
    "placement_sequence",
    "receipt",
    "session_exists",
    "should_switch",
]
