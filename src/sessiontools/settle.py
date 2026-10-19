"""The settle alert: one light line each turn once a session is past the
threshold it set for itself, and nothing at all when it set none.

By design it serves any SESSION that sets a limit, so nothing here asks who or what the session is. The threshold is the only
thing that decides.

It runs as the plugin's PostToolBatch hook (`hooks/settle-hook`, a POSIX
shim that runs `main()`). Three variables, read from the environment the
hook inherits:

    SESSION_SETTLE_AT          the threshold, in tokens: 250000, 250k, 1.5M.
                               Unset, empty, unparsable or <= 0: silent.
    SESSION_SETTLE_SAY         replaces the line; {at} and {used} are filled.
    SESSIONTOOLS_HOOK_SETTLE   off / 0 / false / no: silent, plugin loaded.

The count is `window.scan()` on the transcript the hook payload names - the
same fold `session context` prints - not a `session context` subprocess
(2.5 s a call, measured; the fold is tens of milliseconds).

IT NEVER BREAKS A TURN. `main()` swallows everything and returns 0, and it
never writes to stderr: a hook's stderr or a non-zero exit shows up as an
error line. A slow, missing or unreadable read is silence.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from sessiontools import window

AT_ENV = "SESSION_SETTLE_AT"
SAY_ENV = "SESSION_SETTLE_SAY"
SWITCH_ENV = "SESSIONTOOLS_HOOK_SETTLE"

#: Any of these (any case, surrounding space ignored) switches the hook off.
OFF_WORDS = frozenset({"off", "0", "false", "no"})

#: The default line: it names no verb that does not exist yet. `{at}` is
#: the session's own mark.
DEFAULT_SAY = ("You're past {at}. At a good stopping point, settle: "
               "save what matters, then hand over.")

EVENT = "PostToolBatch"

_SCALE = {"k": 1_000, "m": 1_000_000}


def switched_off(env: Mapping[str, str]) -> bool:
    return env.get(SWITCH_ENV, "").strip().lower() in OFF_WORDS


def threshold(env: Mapping[str, str]) -> int | None:
    """`SESSION_SETTLE_AT` in tokens, or None: unset, empty, unparsable,
    not finite, or not above zero. `250000`, `250k`, `250K`, `1.5M`."""
    raw = env.get(AT_ENV, "").strip().lower()
    if not raw:
        return None
    scale = _SCALE.get(raw[-1], 1)
    if scale != 1:
        raw = raw[:-1].strip()
    try:
        number = float(raw)
    except ValueError:
        return None
    if not math.isfinite(number):
        return None
    at = int(number * scale)
    return at if at > 0 else None


def message(env: Mapping[str, str], at: int, used: int) -> str:
    """The one line: `SESSION_SETTLE_SAY` if set, else the default, with
    `{at}` and `{used}` filled by plain replace - a stray brace is text,
    never an error. Line breaks become spaces: one line, always."""
    text = env.get(SAY_ENV, "").strip() or DEFAULT_SAY
    text = text.replace("{at}", window.fmt_tokens(at)).replace("{used}", window.fmt_tokens(used))
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _transcript(payload: Mapping[str, object], env: Mapping[str, str]) -> Path | None:
    """The payload's `transcript_path` if it is a regular file (a FIFO would
    block the read), else the one `window` finds for its cwd and id."""
    named = payload.get("transcript_path")
    if isinstance(named, str) and named:
        path = Path(named)
        if path.is_file():
            return path
    sid, cwd = payload.get("session_id"), payload.get("cwd")
    if not isinstance(sid, str) or not sid:
        return None
    return window.transcript_path(cwd if isinstance(cwd, str) else None, sid, env)


def decide(env: Mapping[str, str], payload: object) -> str | None:
    """The line to say for this batch, or None for silence.

    Silent: switched off; no threshold; a payload that is not an object; a
    subagent's batch (payload `agent_id`: the count is the parent's, and
    the line would land in the subagent, so the session hears it on its own
    next batch); no transcript; a count under the threshold. `used` is the
    last usage row, so a synthetic zero tail is under any threshold.
    Fires at `used >= at`: reaching the mark counts as past it.
    """
    if switched_off(env):
        return None
    at = threshold(env)
    if at is None or not isinstance(payload, dict):
        return None
    if payload.get("agent_id"):
        return None
    path = _transcript(payload, env)
    if path is None:
        return None
    used = window.scan(path).used
    if used < at:
        return None
    return message(env, at, used)


def hook_output(line: str, payload: Mapping[str, object]) -> str:
    """The JSON Claude Code reads from a PostToolBatch hook: one line."""
    event = payload.get("hook_event_name")
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": event if isinstance(event, str) and event else EVENT,
        "additionalContext": line}})


def main(stdin=None, stdout=None, env: Mapping[str, str] | None = None) -> int:
    """Read the hook payload, maybe print one line of JSON. Always 0."""
    try:
        env = os.environ if env is None else env
        stdin = sys.stdin if stdin is None else stdin
        stdout = sys.stdout if stdout is None else stdout
        if switched_off(env) or threshold(env) is None:
            return 0            # before reading stdin: nothing asked, nothing done
        payload = json.loads(stdin.read() or "null")
        line = decide(env, payload)
        if line:
            stdout.write(hook_output(line, payload) + "\n")
            stdout.flush()
    except BaseException:  # noqa: BLE001, S110 - a hook must never break a turn
        pass
    return 0
