"""`session send`: type text into a live session's tmux pane.

Two phases, as the session repo learned them: the text goes in
LITERALLY (`send-keys -l`), a pause, then Enter as its own call - a TUI
debounces pasted input, and an Enter riding with the text races ahead and
never submits. If the text lands but Enter fails, the line would sit one
keystroke from execution in someone else's input box, so it is cleared
(C-u). Every send leaves one `pane-send:` audit line on stderr with a
digest of the text, never the text.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time

from sessiontools import procs as _procs
from sessiontools import tmux as _tmux
from sessiontools.procs import ancestors

DELAY_VAR = "SESSION_SEND_ENTER_DELAY"
DEFAULT_DELAY = 0.4


def enter_delay(env: dict[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    try:
        return max(0.0, float(env.get(DELAY_VAR, DEFAULT_DELAY)))
    except ValueError:
        return DEFAULT_DELAY


def _keys(socket: str, pane: str, *keys: str) -> int:
    try:
        return _tmux.run(["send-keys", "-t", pane, *keys], socket, timeout=5).returncode
    except (OSError, subprocess.SubprocessError):
        return 127


def still_belongs(socket: str, pane: str, pid: int | None) -> bool:
    """The occupant check, before anything is typed: `%NN` names a pane
    SLOT, not its occupant - `tmux respawn-pane` swaps who lives there and
    keeps the id. The session's pid (or an ancestor of it) must still BE the
    pane's current `#{pane_pid}`; a dead, respawned or recycled pane refuses."""
    if not pid:
        return False
    now = _tmux.pane_pid(socket, pane)
    return now is not None and now in ancestors(pid, _procs.table())


def send_keys(socket: str, pane: str, text: str) -> bool:
    """Type TEXT into PANE on SOCKET and submit it. True only if both landed.
    `--` ends tmux's options, so a TEXT that starts with a dash is typed."""
    text_rc = _keys(socket, pane, "-l", "--", text)
    enter_rc = None
    cleared = False
    if text_rc == 0:
        time.sleep(enter_delay())
        enter_rc = _keys(socket, pane, "Enter")
        if enter_rc != 0:
            _keys(socket, pane, "C-u")
            cleared = True
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    line = (f"pane-send: target={socket}:{pane} text_rc={text_rc} "
            f"enter_rc={enter_rc} sha256={digest}")
    if cleared:
        line += " STRANDED-CLEARED (Enter phase failed; input buffer wiped)"
    print(line, file=sys.stderr)
    return text_rc == 0 and enter_rc == 0
