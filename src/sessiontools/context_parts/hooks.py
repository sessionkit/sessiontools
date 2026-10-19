"""`--hooks`: hook context by hook command, and how much of it repeats
(a port of the `hook-dupes` script).

Every `hook_additional_context` line that entered the window is joined to
the hook COMMAND that wrote it, through the `hook_success` record carrying
the same `toolUseID` (either side of it), exactly as the fold joins it: a
line that matches no success's stdout falls back to the one success that
carried context, else the hook's event name. Per command:

- CHARS are exact: the lines' own lengths. SHARE is chars over all hook
  chars, so the rows' chars sum to the total exactly and their shares to 1.
- A REPEAT is a line this hook already wrote earlier in the session, so the
  window holds it again:
  - EXACT: the same text;
  - TEMPLATED: the same once every digit run is one `#` (DIGITS), and not
    exact: `[clock] 13:07 AEDT` then `[clock] 13:08 AEDT`.
  The first copy of a line is never a repeat. IN-BATCH is the part of those
  repeats whose earlier copy came in the SAME batch: a PostToolUse hook
  firing once per tool call of one batch (the prototype's `dup` + `templ`).
- STEADY: a line form (digit-normalised) that arrives in at least
  STEADY_MIN_BATCHES batches AND in at least STEADY_SHARE of the session's
  batches: the clock or pager line that rides on every batch. Its chars
  count whole, first copy included: it is the cost of the line being there.

A BATCH is what entered between two calls: a line's batch is the call that
first read it. The session's batch count, the steady denominator, is the
calls a tool result entered before (the tool batches; every call when no
tool ran). Calls are counted as the fold counts them, and a line no call
has read yet (after the last call) is not in the window and is not counted,
so the total equals the fold's Hook context chars over every call.

Data only: nothing here renders. Tokens are not estimated: chars are exact,
and the fitted Hook context row is `breakdown.parts`'s.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from sessiontools.context_parts.transcript import (
    _hook_context,
    _PendingHook,
    _resolve_hooks,
    call_reading,
)

#: A digit run, as the templated repeat normalises it.
DIGITS = re.compile(r"\d+")
#: A steady line arrives in at least this many batches...
STEADY_MIN_BATCHES = 10
#: ...and in at least this share of the session's batches.
STEADY_SHARE = 0.5
#: The line forms each hook row keeps, largest by chars first.
TOP_FORMS = 5


def normalise(line: str) -> str:
    """The line's form: every digit run made `#`, so a clock line that
    differs only in its digits has one form."""
    return DIGITS.sub("#", line)


# ---------- data ------------------------------------------------------------
@dataclass(frozen=True)
class HookForm:
    """One line form a hook wrote: `example` is its first exact text."""

    form: str
    example: str
    copies: int
    batches: int
    chars: int
    steady: bool


@dataclass(frozen=True)
class HookRow:
    """One hook command's context. Every figure is chars, exact."""

    command: str
    events: tuple[str, ...]
    lines: int                     # copies that entered the window
    chars: int
    share: float                   # of all hook chars
    batches: int                   # batches this hook wrote into
    boot_chars: int                # the part read by the first call
    exact_repeat_chars: int
    templated_repeat_chars: int
    in_batch_repeat_chars: int     # of the two above, an earlier copy in the same batch
    steady_chars: int
    top: tuple[HookForm, ...] = ()

    @property
    def repeat_chars(self) -> int:
        """Chars of lines this hook had already written: exact + templated."""
        return self.exact_repeat_chars + self.templated_repeat_chars

    @property
    def repeat_share(self) -> float:
        return self.repeat_chars / self.chars if self.chars else 0.0

    @property
    def repeating(self) -> bool:
        return self.repeat_chars > 0


@dataclass(frozen=True)
class Hooks:
    """Hook context by command, largest first. `total_chars` is every row's
    chars together; `batches` is the steady denominator (see the module)."""

    rows: tuple[HookRow, ...]
    total_chars: int
    calls: int
    batches: int

    @property
    def repeat_chars(self) -> int:
        return sum(r.repeat_chars for r in self.rows)

    @property
    def steady_chars(self) -> int:
        return sum(r.steady_chars for r in self.rows)

    @property
    def in_batch_repeat_chars(self) -> int:
        return sum(r.in_batch_repeat_chars for r in self.rows)


# ---------- the pass --------------------------------------------------------
@dataclass(frozen=True)
class _Line:
    batch: int                     # the call that reads it
    event: str
    text: str
    pending: _PendingHook


def _is_call(msg: dict, seen: set) -> bool:
    """A NEW call: accepted by the fold's own `call_reading`, and a message
    id not seen before."""
    r = call_reading(msg)
    if r is None:
        return False
    mid = r.message_id
    if mid is not None:
        if mid in seen:
            return False
        seen.add(mid)
    return True


def _has_tool_result(rec: dict) -> bool:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _read(path: Path) -> tuple[list[_Line], dict, int, set]:
    """Hook lines with their batch, the hook successes, the call count and
    the batches a tool result entered."""
    lines: list[_Line] = []
    successes: dict[str | None, list] = {}
    seen: set = set()
    calls = 0
    tool_batches: set = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            kind = rec.get("type")
            if kind == "assistant":
                msg = rec.get("message")
                if isinstance(msg, dict) and _is_call(msg, seen):
                    calls += 1
            elif kind == "user":
                if _has_tool_result(rec):
                    tool_batches.add(calls)
            elif kind == "attachment":
                a = rec.get("attachment")
                if not isinstance(a, dict):
                    continue
                tid = a.get("toolUseID") if isinstance(a.get("toolUseID"), str) else None
                name = a.get("hookName") if isinstance(a.get("hookName"), str) else "hook"
                if a.get("type") == "hook_success":
                    cmd = a.get("command") if isinstance(a.get("command"), str) else None
                    successes.setdefault(tid, []).append((cmd or name, _hook_context(a)))
                elif a.get("type") == "hook_additional_context":
                    event = a.get("hookEvent") if isinstance(a.get("hookEvent"), str) else name
                    content = a.get("content")
                    for text in content if isinstance(content, list) else [content]:
                        if isinstance(text, str) and text:
                            lines.append(_Line(calls, event, text, _PendingHook(tid, name, text)))
    return lines, successes, calls, tool_batches


def _row(command: str, lines: list[_Line], total: int, batches: int) -> HookRow:
    seen_text: set[str] = set()
    seen_form: set[str] = set()
    in_batch: dict[int, set[str]] = {}
    exact = templated = same_batch = 0
    forms: dict[str, dict] = {}
    for ln in lines:
        form = normalise(ln.text)
        n = len(ln.text)
        repeat = True
        if ln.text in seen_text:
            exact += n
        elif form in seen_form:
            templated += n
        else:
            repeat = False
        batch_forms = in_batch.setdefault(ln.batch, set())
        if repeat and form in batch_forms:
            same_batch += n
        batch_forms.add(form)
        seen_text.add(ln.text)
        seen_form.add(form)
        f = forms.setdefault(form, {"example": ln.text, "copies": 0, "chars": 0,
                                    "batches": set()})
        f["copies"] += 1
        f["chars"] += n
        f["batches"].add(ln.batch)

    def is_steady(f: dict) -> bool:
        k = len(f["batches"])
        return k >= STEADY_MIN_BATCHES and k >= STEADY_SHARE * batches

    hook_forms = [HookForm(form, f["example"], f["copies"], len(f["batches"]), f["chars"],
                           is_steady(f)) for form, f in forms.items()]
    chars = sum(len(ln.text) for ln in lines)
    top = sorted(hook_forms, key=lambda h: -h.chars)[:TOP_FORMS]   # stable: first seen wins a tie
    return HookRow(
        command=command,
        events=tuple(dict.fromkeys(ln.event for ln in lines)),
        lines=len(lines),
        chars=chars,
        share=chars / total if total else 0.0,
        batches=len({ln.batch for ln in lines}),
        boot_chars=sum(len(ln.text) for ln in lines if ln.batch == 0),
        exact_repeat_chars=exact,
        templated_repeat_chars=templated,
        in_batch_repeat_chars=same_batch,
        steady_chars=sum(h.chars for h in hook_forms if h.steady),
        top=tuple(top),
    )


def hooks(path: Path | str) -> Hooks:
    """`--hooks`: every hook command's context in the window, with repeats."""
    lines, successes, calls, tool_batches = _read(Path(path))
    lines = [ln for ln in lines if ln.batch < calls]          # read by a call
    commands = _resolve_hooks([ln.pending for ln in lines], successes)
    by_command: dict[str, list[_Line]] = {}
    for ln in lines:
        by_command.setdefault(commands[id(ln.pending)], []).append(ln)
    total = sum(len(ln.text) for ln in lines)
    batches = len({b for b in tool_batches if b < calls}) or calls
    rows = [_row(c, ls, total, batches) for c, ls in by_command.items()]
    rows.sort(key=lambda r: -r.chars)                          # stable: first seen wins a tie
    return Hooks(rows=tuple(rows), total_chars=total, calls=calls, batches=batches)
