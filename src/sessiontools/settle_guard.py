"""The compaction guard: a PreCompact hook that skips an AUTO compaction
below the SET compaction point.

The first guard is `idleCompaction: false`, which the `claude-code` tool
renders into `session start`'s `--settings`. This is the second,
the belt under it: a hook is the only thing that sees the moment itself.
Claude Code runs `PreCompact` before every summary, with `trigger: "auto"`
for a threshold compaction and for an idle one alike, and `trigger:
"manual"` for `/compact`. An exit 2 skips that compaction (measured
LIVE on the threshold path, `Compaction blocked by PreCompact
hook`; the idle path passes the same gate by static reading, NOT measured).

It runs as `hooks/settle-hook precompact` (the same POSIX shim as the settle
alert, so the same OFF stamp spares python while no layer sets `settle`).

THE RULE. Skip (exit 2) when ALL of these hold, else pass (exit 0):

- the trigger is `auto` (a manual compaction always passes: someone asked);
- the payload is the session's own, not a subagent's (`agent_id`: its
  context is not the one counted here);
- the session's `settle` block is ON, read live through the cascade
  (`settle_settings.resolve`, which keeps the OFF stamp as the alert does):
  unset or `settle: null` is a no-op;
- the SET compaction point is known (`compact_point.compact_at`, the one
  reader the hard line's `{left}` uses; never inferred from a payload);
- the count (`window.scan` on the payload's transcript, the alert's own
  fold) is a reading (a transcript with no usage row, or a `<synthetic>`
  zero tail, is not), it is the WHOLE context (`whole`: no message row
  after the last usage row, as when a session is idle; a threshold
  compaction comes with the new prompt or tool results on the transcript,
  so the last usage row under-counts it), and it is BELOW that point.

So an auto compaction at or above the set point is Claude Code's threshold
doing its job, and passes; so does one that comes with messages after the
last usage row (a threshold or reactive compaction mid-turn, whose count
the transcript cannot give: blocking it could leave a session whose real
window is smaller than the one set unable to compact). One below the point
on a whole count (an idle compaction, the turn over) is the one the settle
lines did not warn of, and is skipped. Unknown anything passes: the guard
never blocks on a guess.

SILENT UNLESS IT SKIPS. On exit 0 a PreCompact hook's stdout joins the
summary's instructions, so a pass prints nothing. A skip says one line,
the reason, which the shim moves to stderr: Claude Code puts a blocking
hook's stderr in its `Compaction blocked by PreCompact hook` message.

IT NEVER BREAKS A COMPACTION BY ACCIDENT: `main()` swallows every error and
passes (0); only the rule above returns 2.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Mapping

from sessiontools import settle_settings, window

EVENT = "PreCompact"

#: The payload's `trigger` for an automatic compaction (threshold or idle).
#: Anything else (`manual`, missing, unknown) passes.
AUTO = "auto"

#: The exit codes Claude Code reads from a PreCompact hook.
PASS = 0
SKIP = 2

#: The one line a skip says (the shim sends it to stderr).
REASON = ("settle: auto compaction skipped at {used}, below the set compaction "
          "point {compact_at}")


def reading(usage: window.Usage) -> bool:
    """USAGE's `used` is a count: at least one usage row, and not a
    `<synthetic>` zero tail after a climb (window.Usage). A
    zero that is not a reading would read as "far below the point"."""
    return usage.usage_rows > 0 and not (usage.used == 0 and usage.peak > 0)


#: The transcript rows that are messages in the context Claude Code counts.
MESSAGE_TYPES = ("user", "assistant")


def _usage_row(msg: dict) -> bool:
    """Is MSG a row `window.scan` takes as a usage row (its own test)?"""
    u = msg.get("usage")
    if not u or not isinstance(u, dict):
        return False
    try:
        s = window._usage_sum(u)
    except TypeError:
        return False
    return isinstance(s, int | float) and math.isfinite(s)


def whole(path) -> bool:
    """Is the last usage row the WHOLE context: no message row after it?

    Claude Code counts the context as the last usage row plus an estimate
    of every message after it (a new prompt, tool results). A threshold
    compaction is checked before the next request, so those rows are on
    the transcript and the last usage row alone UNDER-counts it: a
    live probe compacted at `preTokens` 74,461, over its 67,000 point,
    while its last usage row read 62,924 (the new prompt, 46k characters,
    came after it). That count is a lower bound, not a reading, so the
    guard passes. An idle session's turn has ended on its own assistant row,
    so there its last usage row is the whole count. Rows that are not
    messages (attachments, queue and mode rows) do not count."""
    tail = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"message"' not in line:
                continue            # a JSON key is always quoted: cheap pre-check
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") not in MESSAGE_TYPES:
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            if _usage_row(msg):
                tail = False
            elif msg.get("content"):
                tail = True
    return not tail


def decide(env: Mapping[str, str], payload: object) -> str | None:
    """The reason to skip this compaction, or None to let it run.

    None (pass): a payload that is not an object; a trigger that is not
    `auto`; a subagent's payload (`agent_id`); no session id; no `settle`
    in the session's cascade, read now; no set compaction point; no
    transcript, or one with no reading (`reading`); a count at or above
    the point; a count that is not the whole context (`whole`: a message
    row after the last usage row, as at a threshold compaction)."""
    if not isinstance(payload, dict) or payload.get("trigger") != AUTO:
        return None
    if payload.get("agent_id"):
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    if settle_settings.resolve(sid, env) is None:    # keeps the OFF stamp
        return None
    from sessiontools import compact_point
    point = compact_point.compact_at(sid, env)
    if point is None:
        return None
    from sessiontools import settle
    path = settle._transcript(payload, env)
    if path is None:
        return None
    usage = window.scan(path)
    if not reading(usage) or usage.used >= point or not whole(path):
        return None
    used = usage.used
    return (REASON.replace("{used}", window.fmt_tokens(used))
                  .replace("{compact_at}", window.fmt_tokens(point)))


def main(stdin=None, stdout=None, env: Mapping[str, str] | None = None) -> int:
    """Read the PreCompact payload: print the reason and return SKIP (2), or
    print nothing and return PASS (0). Any error passes."""
    try:
        env = os.environ if env is None else env
        stdin = sys.stdin if stdin is None else stdin
        stdout = sys.stdout if stdout is None else stdout
        reason = decide(env, json.loads(stdin.read() or "null"))
        if reason:
            stdout.write(reason + "\n")
            stdout.flush()
            return SKIP
    except BaseException:  # noqa: BLE001, S110 - a hook must never break by accident
        pass
    return PASS
