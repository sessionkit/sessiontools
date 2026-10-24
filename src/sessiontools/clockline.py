"""The clock line: on every tool batch, the time and how full the context
window is, and a second line once the count reaches a mark the session set.

    [clock] 12:34 UTC | ctx 182K/1.00M (18%)
    ctx past 250K: 262K of 1.00M used.

It runs as the plugin's PostToolBatch hook (`hooks/clock-hook`, a POSIX
shim that runs `main()`), beside the settle alert. Two variables, read from
the environment the hook inherits:

    SESSIONTOOLS_HOOK_CLOCK   on / 1 / true / yes (any case): the line is
                              said. Anything else, unset included, is OFF,
                              and the shim exits before python starts.
    SESSION_CLOCK_WARN_AT     the warn mark: tokens (250000, 250k, 1.5M, the
                              settle alert's grammar) or a percent of the
                              window (80%, 12.5%). Unset, empty, unparsable,
                              not above zero, or a percent with the window
                              unknown: no warn line.

The count is the settle alert's: `window.scan()` on the transcript the
payload names, once this batch's own assistant row is on disk
(`settle.await_batch_row`), and `window.resolve_window()` for the size, so
`CONTEXT_MAX` tightens it as it does for `session context`. Nothing in
`settle` is changed; its helpers are imported.

NEVER A GUESS. A window that cannot be known renders `ctx 182K/?`; no
transcript, no usage row, or the `<synthetic>` zero tail (used 0 after a
real peak, see `window.Usage`) renders `ctx ?`. The clock is still said.
`ctx ?` never warns; `ctx 182K/?` warns at a token mark only, since a
percent mark needs the window. No mail count: mail is not this hook's.

The warn mark does not fall back to `SESSION_SETTLE_AT`: with both on one
value the session would hear one crossing twice. The warn line is a figure,
not an instruction; the settle alert is the instruction.

IT NEVER BREAKS A TURN. `main()` swallows everything and returns 0, and it
never writes to stderr.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections.abc import Mapping

from sessiontools import settle, window

SWITCH_ENV = "SESSIONTOOLS_HOOK_CLOCK"
WARN_ENV = "SESSION_CLOCK_WARN_AT"

#: Exactly these (any case) switch the line on. No whitespace is trimmed:
#: the shim's `case` decides first and trims nothing, so the two agree.
ON_WORDS = frozenset({"on", "1", "true", "yes"})

#: The clock, as `date '+%H:%M %Z'` prints it.
CLOCK_FORMAT = "%H:%M %Z"


def switched_on(env: Mapping[str, str]) -> bool:
    return env.get(SWITCH_ENV, "").lower() in ON_WORDS


def _percent(raw: str) -> float | None:
    """`80%` -> 80.0; None unless finite and 0 < p <= 100."""
    try:
        p = float(raw[:-1].strip())
    except ValueError:
        return None
    return p if math.isfinite(p) and 0 < p <= 100 else None


def warn_at(env: Mapping[str, str], size: int | None) -> tuple[int, float | None] | None:
    """The warn mark as `(tokens, percent)`, percent None for a token mark;
    None for no warn line. A percent mark needs the window: `size` None
    gives None, since a mark that cannot be known is never claimed."""
    raw = env.get(WARN_ENV, "").strip()
    if not raw:
        return None
    if raw.endswith("%"):
        p = _percent(raw)
        if p is None or size is None:
            return None
        at = int(size * p / 100)
        return (at, p) if at > 0 else None
    at = settle.threshold({settle.AT_ENV: raw})
    return None if at is None else (at, None)


def clock(now: time.struct_time) -> str:
    """`12:34 UTC`. `%Z` can be empty on some hosts: no stray space."""
    return " ".join(time.strftime(CLOCK_FORMAT, now).split())


def reading(usage: window.Usage | None) -> int | None:
    """The count in the window, or None when there is none to claim: no
    transcript, no usage row, or the `<synthetic>` zero tail."""
    if usage is None or usage.usage_rows == 0:
        return None
    if usage.used == 0 and usage.peak > 0:
        return None
    return usage.used


def line(now: time.struct_time, used: int | None, size: int | None) -> str:
    """The first line: `[clock] HH:MM TZ | ctx used/max (pct%)`, or its
    degraded forms `ctx used/?` and `ctx ?`."""
    if used is None:
        ctx = "ctx ?"
    elif size is None:
        ctx = f"ctx {window.fmt_tokens(used)}/?"
    else:
        ctx = (f"ctx {window.fmt_tokens(used)}/{window.fmt_tokens(size)} "
               f"({used * 100 // size}%)")
    return f"[clock] {clock(now)} | {ctx}"


def warning(used: int | None, size: int | None,
            mark: tuple[int, float | None] | None) -> str | None:
    """The second line once `used >= at` (reaching the mark counts), or None."""
    if used is None or mark is None:
        return None
    at, pct = mark
    if used < at:
        return None
    past = window.fmt_tokens(at) if pct is None else f"{pct:g}% ({window.fmt_tokens(at)})"
    of = "" if size is None else f" of {window.fmt_tokens(size)}"
    return f"ctx past {past}: {window.fmt_tokens(used)}{of} used."


def _usage(payload: Mapping[str, object], env: Mapping[str, str]) -> window.Usage | None:
    """This batch's fold, or None: no transcript, or one that cannot be read."""
    path = settle._transcript(payload, env)
    if path is None:
        return None
    try:
        settle.await_batch_row(path, settle.batch_ids(payload))
        return window.scan(path)
    except OSError:
        return None


def decide(env: Mapping[str, str], payload: object,
           now: time.struct_time | None = None) -> str | None:
    """What to say for this batch, or None for silence.

    Silent: switched off; a payload that is not an object; a subagent's
    batch (payload `agent_id`: the count is the parent's, and the line
    would land in the subagent). Otherwise the clock line, always, and the
    warn line under it (joined by a newline) at or past the mark."""
    if not switched_on(env) or not isinstance(payload, dict):
        return None
    if payload.get("agent_id"):
        return None
    usage = _usage(payload, env)
    used = reading(usage)
    size = None if used is None else window.resolve_window(usage.model, used, env)
    text = line(time.localtime() if now is None else now, used, size)
    warn = warning(used, size, warn_at(env, size))
    return text if warn is None else f"{text}\n{warn}"


def main(stdin=None, stdout=None, env: Mapping[str, str] | None = None) -> int:
    """Read the hook payload, maybe print one line of JSON. Always 0."""
    try:
        env = os.environ if env is None else env
        stdin = sys.stdin if stdin is None else stdin
        stdout = sys.stdout if stdout is None else stdout
        if not switched_on(env):
            return 0            # before reading stdin: nothing asked, nothing done
        payload = json.loads(stdin.read() or "null")
        text = decide(env, payload)
        if text:
            stdout.write(settle.hook_output(text, payload) + "\n")
            stdout.flush()
    except BaseException:  # noqa: BLE001, S110 - a hook must never break a turn
        pass
    return 0
