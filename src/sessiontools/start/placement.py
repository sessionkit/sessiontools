"""Tmux placement: the launch in a new tmux window instead of in place.

    session start --in-tmux alice                     a window in this session
    session start --tmux-target work:alice alice      a window in session `work`
    session start --tmux-target work --switch alice   ...and move the client there
    session start --in-tmux alice   (outside tmux)    a window in session `agents`

The pane runs the plan's own launch script, written as a 0700 trampoline:
`render_launch_script` with a shebang, extended and never forked, so the
dry-run report of a tmux launch prints exactly what the pane will run. A
launcher-side identity puts the trampoline in its session directory; a
keyed launch mints in the pane (`identity.PaneMint`), has no session
directory yet, and gets a fresh 0700 temp directory instead.

Three rules, each measured in the old launcher before it became one:

- EVERY `-t` IS ANCHORED with `=`: tmux matches a target by
  prefix, so an unanchored `alpha` lands in an existing `alpha-beta`. After
  `new-window` the window is addressed by its `@id`, which cannot collide.
- SWITCHING FOLLOWS THE CALLER: a human at a terminal inside tmux
  is moved to the new window by default; an agent's launch (no tty) never
  moves anyone's view. `--switch` and `--no-switch` override, not both.
- `new-window` IS THE HANDOVER. Before it, a failure removes the files the
  launch wrote (nothing will ever read them). After it the pane owns them,
  so a failure there (automatic-rename, switch-client) is reported and
  nothing is unwound: the session IS placed.

Every tmux command runs under `host_environ()`: the SERVER may be born from
this very call, and a server born inside this package's venv would hand
that venv to every pane it ever creates.
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools import tmux as _tmux
from sessiontools.start.environment import host_environ, without_session_identity
from sessiontools.start.errors import EXIT_USAGE, PROG, LaunchError
from sessiontools.start.plan import LaunchPlan
from sessiontools.start.run import discard_files, run_tmux, write_files

TRAMPOLINE = "launch.sh"

#: The session a placement with no target lands in when the caller is not
#: inside tmux: one fixed name, so a second launch from a plain terminal
#: adds a window beside the first instead of a session per agent.
DEFAULT_TMUX_SESSION = "agents"


class TmuxPlacement(NamedTuple):
    """Where a tmux launch landed: what `receipt` prints."""

    session: str          # "" = the caller's current session
    window: str
    window_id: str        # tmux's own @id, the handle that cannot collide
    created_session: bool
    #: The new pane's id (`%12`) and the pid of the process it runs, as
    #: `new-window -P` printed them; "" and None when tmux printed none.
    #: The default seat's claim is re-pointed at them.
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
    environment alone, so `main` asks before anything is minted or written;
    `place_in_tmux` asks again, so its contract does not depend on a caller
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


def write_trampoline(plan: LaunchPlan) -> str:
    """The plan's script as a 0700 file a pane can execute: in the session
    directory, or for a pane-minted launch (no directory yet) in a fresh
    0700 temp directory."""
    if plan.identity is not None:
        directory = plan.identity.session_dir
    else:
        directory = tempfile.mkdtemp(prefix="session-start-pane-")
    path = os.path.join(directory, TRAMPOLINE)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(plan.script)
    os.chmod(path, 0o700)
    return path


def place_in_tmux(plan: LaunchPlan, session: str, window: str, switch: bool,
                  environ: Mapping[str, str] | None = None) -> TmuxPlacement:
    """Write the plan's files and trampoline, then open a DETACHED window
    that runs it. Returns where it landed; raises LaunchError, leaving
    nothing behind, if it never got that far."""
    environ = os.environ if environ is None else environ
    written: list = []
    trampoline = None
    try:
        written = write_files(plan)
        trampoline = write_trampoline(plan)
        created = False
        target: list[str] = []
        if session:
            created = ensure_session(session, environ)
            target = ["-t", "=" + session + ":"]
        else:
            check_placeable(session, environ)
        # tmux hands a lone command argument to `sh -c`, so the path is quoted:
        # a space or `$` in AGENTS_DIR or TMPDIR would split or expand it.
        printed = run_tmux(["new-window", "-d", "-P", "-F", NEW_WINDOW_FORMAT,
                            *target, "-n", window, shlex.quote(trampoline)],
                           environ).split()
    except BaseException:
        # The pane never ran: nothing will read these, so they go.
        discard_files(written)
        if trampoline is not None:
            try:
                os.unlink(trampoline)
                if plan.identity is None:      # its own temp directory
                    os.rmdir(os.path.dirname(trampoline))
            except OSError:
                pass
        raise
    # ---- past the handover: the pane owns the files and the launch --------
    window_id, pane_id, pane_pid = _parse_new_window(printed)
    after = [("automatic-rename off",
              ["set-window-option", "-t", window_id, "automatic-rename", "off"])]
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
#: the pid of its process (the default seat's claim names that pid).
NEW_WINDOW_FORMAT = "#{window_id} #{pane_id} #{pane_pid}"


def _parse_new_window(printed: list[str]) -> tuple[str, str, int | None]:
    """`@7 %12 4242` -> ("@7", "%12", 4242). A field tmux did not print is
    "" or None: the window id is what placement needs, the rest is the
    seat's."""
    window_id = printed[0] if printed else ""
    pane_id = printed[1] if len(printed) > 1 and printed[1].startswith("%") else ""
    try:
        pane_pid = int(printed[2]) if len(printed) > 2 else None
    except ValueError:
        pane_pid = None
    return window_id, pane_id, pane_pid


def receipt(plan: LaunchPlan, placement: TmuxPlacement,
            environ: Mapping[str, str] | None = None) -> str:
    """The one line a placement prints: who, which window (and its @id),
    where, and the session id - or that the pane mints it. A caller outside
    tmux is never switched to the window, so its line ends with how to
    reach it; a caller inside tmux gets the line unchanged."""
    environ = os.environ if environ is None else environ
    if placement.session:
        created = " (created)" if placement.created_session else ""
        where = f"session '{placement.session}'{created}"
    else:
        where = "the current session"
    sid = plan.identity.session_id if plan.identity is not None else "id minted in the pane"
    who = plan.agent.name or plan.vendor
    line = (f"{PROG}: placed {who} in tmux window {placement.window!r} "
            f"({placement.window_id}) of {where} -- session {sid}")
    if placement.session and not environ.get("TMUX"):
        line += f"; attach: tmux attach -t {shlex.quote(placement.session)}"
    return line


__all__ = [
    "DEFAULT_TMUX_SESSION",
    "NEW_WINDOW_FORMAT",
    "TRAMPOLINE",
    "TmuxPlacement",
    "check_placeable",
    "default_session",
    "ensure_session",
    "parse_tmux_target",
    "place_in_tmux",
    "receipt",
    "session_exists",
    "should_switch",
    "write_trampoline",
]
