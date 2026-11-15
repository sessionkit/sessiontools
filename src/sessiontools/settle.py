"""The settle alert: a soft line and a hard line, read LIVE from the
settings cascade each batch, and nothing at all when no layer sets them.

By design it serves any SESSION that sets a limit, so nothing here asks who or what the session is. The settings are the only
thing that decides.

It runs as the plugin's PostToolBatch hook (`hooks/settle-hook`, a POSIX
shim that runs `main()`). Every key, default message and variable name
lives in `settle_settings` (the one home); this module only applies them.

THE SETTINGS are the `session` tool's `settle` block, read through every
layer of the cascade (`settle_settings.resolve`) on EVERY batch python
runs, so an edited layer is heard at the next tool batch. An
agent file's `settings:` overrides the host's values AND words, and the
layers below it win over it. No `settle` anywhere, or `settle: null`:
silent, and `resolve` writes the OFF stamp (`<session dir>/settle/off`)
that lets the shim skip python until a layer it lists is newer.

THE ENVIRONMENT IS NOT A LAYER. The alert reads neither `SESSION_SETTLE_AT`
nor `SESSION_SETTLE_SAY`: `session start` copies them once, at birth, into
the session layer (`settle.soft.at`, `settle.soft.say`), so a session it
launched that exports only `SESSION_SETTLE_AT` still hears one line, every
batch, from its mark. `SESSIONTOOLS_HOOK_SETTLE` is retired: a layer's
`settle: null` is the off switch.

THE SOFT LINE speaks once per crossing of `soft.at + k * soft.every`
(k = 0, 1, 2 ...); with `soft.every` unset, every batch from `soft.at`.

THE HARD LINE starts at the LOWER of `hard.at` and the SET compaction
point less `hard.before_compact` (`compact_point`; with the point
unknown, `hard.at` alone). It speaks every batch from there, or once per
crossing of `line + k * hard.every`.

ONE LINE PER BATCH, AND HARD WINS: a batch whose hard line speaks says
only that, and once the count is past the hard line the soft line stays
silent (a "settle at a good stopping point" after a "now" would undo it).

A CROSSING is a mark file, `<session dir>/settle/<soft|hard>/<tokens>`,
made with O_EXCL as skillbox's `ctx-cross` makes its own, so
a batch that finds the mark already there says nothing. Only the highest
mark crossed is made: a jump past several says one line. Each batch
removes the marks above the count, so after a compaction each crossing
above it is said again. No session dir: no marks, so a line with an
`every` is silent there (a guess is not a crossing).

THE PLACEHOLDERS, filled by plain replace on both lines: `{at}` the mark
the line crossed, `{used}` the count, `{compact_at}` the set compaction
point and `{left}` the tokens before it. Those two come ONLY from
`compact_point` and read `?` when the point is
not set; nothing infers them from a payload.

The count is `window.scan()` on the transcript the hook payload names - the
same fold `session context` prints - not a `session context` subprocess
(2.5 s a call, measured; the fold is tens of milliseconds).

PostToolBatch can start the hook before the harness has written the
batch's own assistant row (the one whose `tool_use` block the payload's
`tool_calls[].tool_use_id` names, with this turn's usage). Read at once,
the count would be the previous turn's, or none on the first turn. So
when the payload names its tool calls, `decide()` first waits for that row:
it reads the transcript's new tail every `ROW_POLL_S` and stops at the row,
or at `ROW_WAIT_S` at most, then counts what is on disk. A payload that
names no tool call is not waited for.

IT NEVER BREAKS A TURN. `main()` swallows everything and returns 0, and it
never writes to stderr: a hook's stderr or a non-zero exit shows up as an
error line. A slow, missing or unreadable read is silence.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from sessiontools import settle_settings, window
from sessiontools.settle_settings import UNKNOWN, Hard, Soft

EVENT = "PostToolBatch"

#: The placeholders, spelled in settle_settings (the one home). The last
#: two are filled from the set compaction point: a line without them
#: never reads it.
AT_FIG, USED_FIG, LEFT_FIG, COMPACT_AT_FIG = settle_settings.PLACEHOLDERS
POINT_FIGURES = (LEFT_FIG, COMPACT_AT_FIG)

#: The longest wait for this batch's own assistant row, and the poll
#: interval. The row lands tens of milliseconds after the hook starts; the
#: hook's own timeout is 5 s, so the cap stays well inside it.
ROW_WAIT_S = 1.0
ROW_POLL_S = 0.01

#: The clock and the sleep the wait uses (tests replace them).
_clock = time.monotonic
_sleep = time.sleep


# --- the two lines -------------------------------------------------------------------

def fill(text: str, at: int, used: int, compact_at: int | None) -> str:
    """TEXT with `{at}`, `{used}`, `{compact_at}` and `{left}` filled by
    plain replace - a stray brace is text, never an error. `{compact_at}`
    and `{left}` read `?` when the point is unknown; `{left}` is never
    below zero. Line breaks become spaces: one line, always."""
    if compact_at is None:
        point = left = UNKNOWN
    else:
        point = window.fmt_tokens(compact_at)
        left = window.fmt_tokens(max(compact_at - used, 0))
    text = (text.replace(AT_FIG, window.fmt_tokens(at))
                .replace(USED_FIG, window.fmt_tokens(used))
                .replace(COMPACT_AT_FIG, point)
                .replace(LEFT_FIG, left))
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def hard_line(hard: Hard, compact_at: int | None) -> int | None:
    """Where HARD starts: the lower of its `at` and the set compaction
    point less its `before_compact`. An unknown point counts `at` alone;
    None when neither can be placed."""
    marks = [] if hard.at is None else [hard.at]
    if hard.before_compact is not None and compact_at is not None:
        marks.append(max(compact_at - hard.before_compact, 0))
    return min(marks) if marks else None


def crossed(start: int, every: int | None, used: int) -> int | None:
    """The highest mark `start + k * every` at or under USED, or None when
    USED is under START. No EVERY: START itself (the line every batch)."""
    if used < start:
        return None
    if every is None:
        return start
    return start + (used - start) // every * every


def rearm(marks: Path, used: int) -> None:
    """Remove each mark in MARKS above USED: a count that fell (a
    compaction) re-arms every crossing above it. No dir: nothing."""
    try:
        names = os.listdir(marks)
    except OSError:
        return
    for name in names:
        if name.isdigit() and int(name) > used:
            try:
                os.unlink(marks / name)
            except OSError:
                pass


def first_crossing(marks: Path | None, mark: int) -> bool:
    """Make MARK's file in MARKS with O_EXCL: True if this call made it
    (the first batch past it), False if it was there or cannot be made.
    No MARKS (no session dir): False."""
    if marks is None:
        return False
    try:
        marks.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(marks / str(mark), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return False
    return True


def _speaks(start: int, every: int | None, used: int, marks: Path | None) -> int | None:
    """The mark a line crosses this batch if it speaks, else None: every
    batch from START when EVERY is unset, else only on a first crossing."""
    mark = crossed(start, every, used)
    if mark is None or (every is not None and not first_crossing(marks, mark)):
        return None
    return mark


def marks_dir(sid: str, env: Mapping[str, str]) -> Path | None:
    """`<session dir>/settle`, or None when the session has no dir."""
    from sessiontools import settings
    session = settings.session_dir(sid, env)
    return session / settle_settings.ROOT if session.is_dir() else None


def line(soft: Soft | None, hard: Hard | None, used: int, marks: Path | None,
         compact_at: Callable[[], int | None]) -> str | None:
    """The one line for a count of USED, or None. MARKS is the session's
    `settle` dir (None: no marks). COMPACT_AT is called at most once, and
    only when a figure needs the point: a hard `before_compact`, or a line
    said with `{left}` or `{compact_at}` in it."""
    point: list[int | None] = []

    def known() -> int | None:
        if not point:
            point.append(compact_at())
        return point[0]

    def say(text: str, mark: int) -> str:
        needs = any(figure in text for figure in POINT_FIGURES)
        return fill(text, mark, used, known() if needs else None)

    soft_marks = hard_marks = None
    if marks is not None:
        soft_marks = marks / settle_settings.SOFT
        hard_marks = marks / settle_settings.HARD
        rearm(soft_marks, used)
        rearm(hard_marks, used)
    past_hard = False
    if hard is not None:
        start = hard_line(hard, known() if hard.before_compact is not None else None)
        if start is not None and used >= start:
            past_hard = True
            mark = _speaks(start, hard.every, used, hard_marks)
            if mark is not None:
                return say(hard.say, mark)
    if soft is not None:
        mark = _speaks(soft.at, soft.every, used, soft_marks)   # a crossing past hard is spent
        if mark is not None and not past_hard:
            return say(soft.say, mark)
    return None


# --- the transcript ------------------------------------------------------------------

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


def batch_ids(payload: Mapping[str, object]) -> frozenset[str]:
    """The `tool_use_id` of each of the payload's `tool_calls`: the ids this
    batch's assistant row carries. Empty when the payload names none."""
    calls = payload.get("tool_calls")
    if not isinstance(calls, list):
        return frozenset()
    return frozenset(c["tool_use_id"] for c in calls
                     if isinstance(c, dict) and isinstance(c.get("tool_use_id"), str)
                     and c["tool_use_id"])


def _is_batch_row(raw: bytes, ids: frozenset[str]) -> bool:
    """`raw` is an assistant row with a `tool_use` block whose id is in
    `ids`. Parsed, never a substring match: the tool_result row that follows
    carries the same id, and only the assistant row carries the usage."""
    if not any(i.encode() in raw for i in ids):
        return False
    try:
        row = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return False
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") in ids
        for block in content)


def await_batch_row(path: Path, ids: frozenset[str]) -> bool:
    """Wait until this batch's own assistant row is on disk: True once it
    is, False at the cap or on a failed read. No ids: True at once.

    Each poll reads only what was appended since the last one, so the
    file is read once in all; a half-written last line waits for its
    newline."""
    if not ids:
        return True
    deadline = _clock() + ROW_WAIT_S
    offset, tail = 0, b""
    while True:
        try:
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            return False
        offset += len(chunk)
        *lines, tail = (tail + chunk).split(b"\n")
        if any(_is_batch_row(row, ids) for row in lines):
            return True
        if _clock() >= deadline:
            return False
        _sleep(ROW_POLL_S)


# --- the hook ------------------------------------------------------------------------

def decide(env: Mapping[str, str], payload: object) -> str | None:
    """The line to say for this batch, or None for silence.

    Silent: a payload that is not an object; a subagent's batch (payload
    `agent_id`: the count is the parent's, and the line would land in the
    subagent, so the session hears it on its own next batch); no session
    id; no `settle` in the session's cascade, read now (`settle_settings.
    resolve`: unset, null, or a layer that cannot be read; a clean OFF
    stamps `settle/off`, ON removes it); no transcript; a
    count under both lines. `used` is the last usage row, so a synthetic
    zero tail is under any line. A line fires at `used >= mark`: reaching
    the mark counts as past it.

    `used` is this batch's own: when the payload names its tool calls, the
    count waits for their assistant row (`await_batch_row`, at most
    `ROW_WAIT_S`). A row that never lands is counted as the disk has it.
    """
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    settle = settle_settings.resolve(sid, env)    # keeps the OFF stamp
    if settle is None:
        return None
    path = _transcript(payload, env)
    if path is None:
        return None
    await_batch_row(path, batch_ids(payload))
    used = window.scan(path).used

    def compact_at() -> int | None:
        from sessiontools import compact_point
        return compact_point.compact_at(sid, env)

    return line(settle.soft, settle.hard, used, marks_dir(sid, env), compact_at)


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
        payload = json.loads(stdin.read() or "null")
        said = decide(env, payload)
        if said:
            stdout.write(hook_output(said, payload) + "\n")
            stdout.flush()
    except BaseException:  # noqa: BLE001, S110 - a hook must never break a turn
        pass
    return 0
