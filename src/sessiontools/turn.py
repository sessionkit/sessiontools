"""The turn-ended mark: "this seat's turn is over", written by a hook,
read by a driver.

By design: a seat whose turn has ended while a background agent runs is
`idle with children`, not busy. Claude Code keeps its sidecar `busy` for as long as such an agent lives (the status
is `isLoading || delegatedActive`, as measured), so the sidecar alone
cannot tell "turn running" from "turn over, children live". This file is the
second signal. CONTRACT v1:

- THE FILE: `<session dir>/turn-ended` (`settings.session_dir`, so
  `$SESSIONS_DIR` moves it with every other session file). It sits beside
  `mail/`, not in it: it is harness state, not mail.
- THE BODY: one JSON line, `{"v": 1, "at": <epoch s, float>, "harness":
  "claude-code", "event": "Stop", "background": <n running, or null>}`.
- WRITTEN ATOMICALLY: a temp file in the same folder, then rename. A reader
  sees no file or a whole one.
- SET on the harness's Stop; CLEARED (unlinked) on its PreToolUse,
  UserPromptSubmit and SessionEnd. An event that carries `agent_id` is a
  subagent's and is IGNORED, set and clear alike: a subagent's events carry
  the PARENT's session id.

THE READER is `read_at()`: the mark's `at`, or None when there is no mark.
An unknown `v`, a body that is not JSON or not an object, or an `at` that is
not a finite number is NO MARK: the reading is then exactly today's. The
driver decides what a mark means (`claude_code`); this module only knows the
file.

THE WRITER is `main()`, run by `hooks/busy-sound` (the busy sound's hook set):
`set` on Stop, `clear` where the shim could not tell a parent's event from a
subagent's without parsing. DEFAULT OFF: it does nothing unless
`SESSIONTOOLS_HOOK_TURN` is `on` (or 1/true/yes). Turning it on for a seat is
a config change; with it off every reading is today's. It never creates a session dir, never raises, and never
writes a byte to stdout or stderr: a hook is never the reason a turn breaks.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sessiontools import settings

#: The mark's file name, in the session dir.
MARK = "turn-ended"
#: The contract version this reader and writer speak.
VERSION = 1
HARNESS = "claude-code"
EVENT = "Stop"

#: The writer's switch. Unset is OFF.
SWITCH_ENV = "SESSIONTOOLS_HOOK_TURN"
ON_WORDS = frozenset({"on", "1", "true", "yes"})
SID_ENV = "CLAUDE_CODE_SESSION_ID"
VERBS = ("set", "clear")


def mark_path(sid: str, env: Mapping[str, str] | None = None) -> Path:
    """Where SID's mark lives: `<session dir>/turn-ended`."""
    return settings.session_dir(sid, env) / MARK


def _number(value: Any) -> float | None:
    """VALUE as a finite float, else None. A boolean is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse(text: str) -> float | None:
    """A mark's body -> its `at`, or None when it is not a v1 mark."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("v") != VERSION or isinstance(data.get("v"), bool):
        return None
    return _number(data.get("at"))


def read_at(path: Path) -> float | None:
    """The mark at PATH -> its `at`; None when there is none: missing,
    unreadable, or not a v1 mark."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return parse(text)


# --- the writer ---------------------------------------------------------------------

def is_sid(sid: object) -> bool:
    """A session id that names one directory: no `/`, not `.` or `..`."""
    return isinstance(sid, str) and bool(sid) and "/" not in sid and sid not in (".", "..")


def switched_on(env: Mapping[str, str]) -> bool:
    return (env.get(SWITCH_ENV) or "").strip().lower() in ON_WORDS


def background(payload: Mapping[str, Any]) -> int | None:
    """How many of the Stop payload's `background_tasks` are running; None
    when the payload does not say (no list there)."""
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return None
    return sum(1 for t in tasks if isinstance(t, Mapping) and t.get("status") == "running")


def body(at: float, payload: Mapping[str, Any]) -> str:
    """The one JSON line contract v1 names."""
    return json.dumps({"v": VERSION, "at": at, "harness": HARNESS, "event": EVENT,
                       "background": background(payload)}) + "\n"


def _session(payload: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
    """The PARENT's session id, or None: none named, or a subagent's event."""
    if payload.get("agent_id"):
        return None
    sid = env.get(SID_ENV) or payload.get("session_id")
    return sid if is_sid(sid) else None


def set_mark(payload: Mapping[str, Any], env: Mapping[str, str],
             now: float | None = None) -> Path | None:
    """Stop: write the mark, atomically. The session dir must exist already
    (no identity, no mark). Returns the path written, or None."""
    sid = _session(payload, env)
    if sid is None:
        return None
    path = mark_path(sid, env)
    if not path.parent.is_dir():
        return None
    at = time.time() if now is None else now
    tmp = path.with_name(f".{MARK}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body(at, payload))
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return None
    return path


def clear_mark(payload: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """PreToolUse, UserPromptSubmit, SessionEnd: unlink the mark. True when
    there was one to unlink. A subagent's event clears nothing."""
    sid = _session(payload, env)
    if sid is None:
        return False
    try:
        mark_path(sid, env).unlink()
    except OSError:
        return False
    return True


def main(argv: Sequence[str] | None = None, stdin=None,
         env: Mapping[str, str] | None = None) -> int:
    """`set|clear` with the hook payload on stdin. Always 0, never a byte on
    stdout or stderr."""
    args = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if env is None else env
    try:
        if not switched_on(environ):
            return 0
        verb = args[0] if args else ""
        if verb not in VERBS:
            return 0
        raw = (sys.stdin if stdin is None else stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        if verb == "set":
            set_mark(payload, environ)
        else:
            clear_mark(payload, environ)
    except Exception:  # noqa: BLE001 - a hook never breaks a turn
        return 0
    return 0
