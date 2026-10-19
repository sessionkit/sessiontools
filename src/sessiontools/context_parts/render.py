"""The views as text and JSON: `--parts` (with
`--tools`), `--startup`, `--boot` and `--hooks`.

THE RULES every view is tested against:

1. NO BARE NUMBER. Every figure carries its unit (`tok`, `chars`, `calls`,
   ...) and its line says what it is. A percentage says what it is of. A
   call is named `call N`. Text the transcript supplies (a tool key, a hook
   command, a model id) is data, not a figure, and goes through `_data`.
2. EXACT AND FITTED ARE DISTINGUISHABLE: a fitted figure is `~` in text
   and `"fitted": true` in JSON. An exact row carries neither. A remainder
   or a residual is neither exact nor fitted, and says which it is.
3. UNKNOWN PRINTS `?` (JSON null), never a guess. A category that never
   entered has no rate: it is left out of the text and is null in JSON. A
   category fitted at a rate of zero is a real `~0 tok`.

Own output is labelled with WHICH output it is: the
since-boot row is the output inside the counted intervals, the figure the
residual uses; every call's output is printed beside it.

The `--hooks` total spans every call, the boot call and a compaction's
interval included; the `--parts` Hook context row spans the counted
intervals only. With `parts=`, the hooks view prints both and the gap
between them, so they never look as if they disagree.

Data in, strings and dicts out: nothing here reads a transcript, the
environment or a setting. The band is computed by `sessiontools.band` and
passed in.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sessiontools.context_parts import hooks as hooks_mod
from sessiontools.context_parts.boot import Boot, BootRow, Startup
from sessiontools.context_parts.breakdown import OWN_OUTPUT, UNATTRIBUTED, Parts, Row
from sessiontools.context_parts.hooks import HookRow, Hooks
from sessiontools.context_parts.tools import Tools
from sessiontools.context_parts.transcript import CATEGORIES, HOOK, LABELS, RESULT

UNKNOWN = "?"
#: The widest a label column runs before the figure column.
LABEL_WIDTH = 50
#: The figure column's width.
FIGURE_WIDTH = 12
#: The longest a line of hook text runs in the text view (the JSON keeps it whole).
EXAMPLE_MAX = 60
#: The longest a tool key or hook command runs in a label.
KEY_MAX = 60

FITTED_MARK = "~"
LEGEND_FITTED = ("~ = estimated: an exact total split by a per-session fit, or counted "
                 "at a rate. Exact rows are not marked.")
LEGEND_UNITS = "tok = tokens; chars = characters; ? = unknown, never a guess."
LEGEND_UNATTRIBUTED = ("Unattributed is what the split does not explain, images "
                       "included: an image counts no chars, so its tokens land there.")
LEGEND_HOOKS = ("chars are exact: the lines' own lengths. A repeat is a line this hook "
                "already wrote; digits only = the same text once every digit run is one "
                "mark. Steady = a line form in at least {n} batches and in at least "
                "half of the tool batches.")


# ---------- figures ---------------------------------------------------------
def _data(text: str) -> str:
    """Text the transcript or a module supplies: a key, a command, a model.
    Not a figure; the no-bare-number test blanks it through this one seam."""
    return text


def num(n: float | None) -> str:
    """A compact figure, no unit: 812, 33.3K, 214K, 1.00M; `?` for unknown."""
    if n is None:
        return UNKNOWN
    a = abs(n)
    if round(a) < 1000:
        s = f"{round(a)}"
    elif a < 99_950:
        s = f"{a / 1000:.1f}K"
    elif a < 999_500:
        s = f"{round(a / 1000)}K"
    else:
        s = f"{a / 1_000_000:.2f}M"
    return f"-{s}" if n < 0 and s != "0" else s


def tok(n: float | None, fitted: bool = False) -> str:
    """Tokens: `33.3K tok`, `~2.1K tok` when fitted, `? tok` when unknown."""
    return f"{FITTED_MARK if fitted and n is not None else ''}{num(n)} tok"


def chars(n: int | None) -> str:
    """Chars are exact counts, so they print whole: `2,977 chars`."""
    return f"{UNKNOWN if n is None else f'{n:,}'} chars"


def pct(share: float | None, of: str) -> str:
    """A share (0-1) as a percentage that says what it is of."""
    if share is None:
        return f"{UNKNOWN}% of {of}"
    figure = f"{share * 100:.1f}"
    return f"{'0.0' if figure == '-0.0' else figure}% of {of}"


def rate(r: float | None) -> str:
    return f"{UNKNOWN if r is None else f'{r:.3f}'} tok/char"


def count(n: int, unit: str, plural: str | None = None) -> str:
    return f"{n} {unit if n == 1 else (plural or unit + 's')}"


def cut(text: str, limit: int) -> str:
    """One line, at most LIMIT chars: whitespace runs made one space."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit - 3] + "..."


def _line(label: str, figure: str, note: str = "", indent: int = 2) -> str:
    width = max(LABEL_WIDTH - indent, 4)
    if len(label) > width:                     # a long key keeps the figure column
        label = cut(label, width)
    out = f"{' ' * indent}{label:<{width}} {figure:>{FIGURE_WIDTH}}"
    return f"{out}   {note}" if note else out


def _header(title: str, session: str | None, model: str | None) -> str:
    who = f" of {_data(session)}" if session else ""
    m = f" ({_data(model)})" if model else ""
    return f"{title}{who}{m}"


def _window_text(used: int | None, window: int | None) -> str:
    if used is None:
        return f"{tok(None)} used"
    if not window:
        return f"{tok(used)} used, of a window of {tok(None)} (the model's is not known)"
    return (f"{tok(used)} used of a {tok(window)} window "
            f"({pct(used / window, 'the window')}), {tok(window - used)} free")


def _warnings(warnings: Iterable[str]) -> list[str]:
    return [f"warning: {_data(w)}" for w in warnings]


# ---------- JSON rows ---------------------------------------------------------
def _row_json(category: str, label: str, tokens: float | None, basis: str,
              exact: bool, fitted: bool, **extra: Any) -> dict:
    out = {"category": category, "label": label, "tokens": tokens, "basis": basis,
           "exact": exact, "fitted": fitted}
    out.update(extra)
    return out


def _window_json(used: int | None, window: int | None) -> dict:
    ok = used is not None and bool(window)
    return {"used": used, "window": window or None,
            "pct": (used * 100 // window) if ok else None,      # type: ignore[operator]
            "free": (window - used) if ok else None}            # type: ignore[operator]


def _seen(r: Row) -> bool:
    """A since-boot category that entered (it has a rate, zero included)."""
    return r.basis in ("exact", "residual") or r.rate is not None


# ---------- startup ---------------------------------------------------------
def _boot_row_text(r: BootRow, indent: int) -> str:
    if r.basis == "remainder":
        note = ("exact remainder: the startup total minus the rows below"
                if r.category == "core" else "remainder: the core minus the /context rows")
    elif r.basis == "snapshot":
        note = "/context's estimate"
    elif r.basis == "counted":
        note = (f"counted: {chars(r.chars)} at {rate(r.rate)}, "
                f"{_data(r.rate_source or '')}; {count(r.count, 'entry', 'entries')}")
    else:
        note = "exact"
    return _line(_data(r.label), tok(r.tokens, r.fitted), note, indent)


def _startup_lines(s: Startup, indent: int) -> list[str]:
    out = []
    if s.core is not None:
        out.append(_boot_row_text(s.core, indent))
        if s.core_split:
            out.append(f"{' ' * (indent + 2)}split {_data(s.snapshot_label() or '')}:")
            out += [_boot_row_text(r, indent + 4) for r in s.core_split]
    out += [_boot_row_text(r, indent) for r in s.rows]
    return out


def _boot_row_json(r: BootRow, section: str) -> dict:
    return _row_json(r.category, r.label, r.tokens, r.basis, r.exact, r.fitted,
                     section=section, chars=r.chars, rate=r.rate,
                     rate_source=r.rate_source, entries=r.count)


def _startup_rows_json(s: Startup, section: str) -> list[dict]:
    out = []
    if s.core is not None:
        out.append(_boot_row_json(s.core, section))
        out += [_boot_row_json(r, section + ".core") for r in s.core_split]
    out += [_boot_row_json(r, section) for r in s.rows]
    return out


def startup_text(s: Startup, *, session: str | None = None) -> str:
    """`--startup`: what was in the window before the first reply."""
    lines = [(f"{_header('Startup', session, None)}: {tok(s.total)} in the window before "
             f"the first reply (exact: the first call's window)")]
    if s.total is None:
        lines.append("  no call yet: nothing to split")
    lines += _startup_lines(s, 2)
    lines += _warnings(s.warnings)
    lines += ["", LEGEND_FITTED, LEGEND_UNITS]
    return "\n".join(lines)


def startup_json(s: Startup, *, session: str | None = None) -> dict:
    return {"view": "startup", "session": session, "total": s.total,
            "rows": [_row_json("startup", "Startup (total)", s.total, "exact", s.total is not None, False,
                               section="total"), *_startup_rows_json(s, "startup")],
            "snapshot": s.snapshot_label(), "brief_pointer": s.brief_pointer,
            "warnings": list(s.warnings)}


# ---------- boot ------------------------------------------------------------
def _reads_by_category(b: Boot) -> list[tuple[str, int, int, int]]:
    """(category, entries, chars, tokens) of the boot reads, in category order."""
    acc: dict[str, list[int]] = {}
    for r in b.reads:
        a = acc.setdefault(r.category, [0, 0, 0])
        a[0] += 1
        a[1] += r.chars
        a[2] += r.tokens
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return [(c, *acc[c]) for c in sorted(acc, key=lambda c: order.get(c, len(order)))]


def _boot_kind(b: Boot) -> str:
    return f"agent {_data(b.identity)}" if b.agent and b.identity else (
        "agent" if b.agent else "human session")


def boot_text(b: Boot, *, session: str | None = None) -> str:
    """`--boot`: the startup, then everything up to SETTLED."""
    head = _header("Boot", session, None)
    if b.total is None:
        lines = [f"{head} ({_boot_kind(b)}): {tok(None)}: no call yet"]
        lines += _warnings(b.warnings)
        return "\n".join(lines)
    lines = [(f"{head} ({_boot_kind(b)}): {tok(b.total)} at SETTLED, the window of "
             f"call {b.settled_call} of {count(b.calls, 'call')} (exact)")]
    signal = f"  settled by: {_data(b.signal_text)}"
    if b.signal_detail:
        signal += f": {_data(cut(b.signal_detail, KEY_MAX))}"
        if b.signal_call is not None:
            signal += f" (issued by call {b.signal_call})"
    lines.append(signal)
    if b.held_for_brief:
        lines.append(f"  held to call {b.settled_call}: the first call that has seen the "
                     f"brief's own read")
    lines.append(_line("AT STARTUP", tok(b.startup.total), "exact: the first call's window"))
    lines += _startup_lines(b.startup, 4)
    lines.append(_line("ON DEMAND", tok(b.on_demand), "exact: settled minus startup"))
    lines.append(_line("Own output (thinking, text, calls)", tok(b.own_output),
                       "exact: the output of every call before settled", 4))
    for c, n, ch, t in _reads_by_category(b):
        lines.append(_line(_data(LABELS.get(c, c)), tok(t, True),
                           f"counted: {chars(ch)} in {count(n, 'entry', 'entries')}", 4))
    lines.append(_line("Unattributed", tok(b.unattributed),
                       "on demand minus own output and the counted rows", 4))
    lines += _warnings(b.warnings)
    lines += ["", LEGEND_FITTED, LEGEND_UNITS, LEGEND_UNATTRIBUTED]
    return "\n".join(lines)


def boot_json(b: Boot, *, session: str | None = None) -> dict:
    rows = [_row_json("boot", "Boot (total, at settled)", b.total, "exact", b.total is not None,
                      False, section="total"),
            _row_json("startup", "At startup", b.startup.total, "exact",
                      b.startup.total is not None, False, section="total"),
            *_startup_rows_json(b.startup, "startup"),
            _row_json("on_demand", "On demand", b.on_demand, "exact", b.on_demand is not None,
                      False, section="total"),
            _row_json(OWN_OUTPUT, "Own output (thinking, text, calls)", b.own_output, "exact",
                      b.own_output is not None, False, section="on_demand",
                      output="every call before settled")]
    rows += [_row_json(c, LABELS.get(c, c), t, "counted", False, True, section="on_demand",
                       chars=ch, entries=n) for c, n, ch, t in _reads_by_category(b)]
    rows.append(_row_json(UNATTRIBUTED, "Unattributed", b.unattributed, "residual", False, False,
                          section="on_demand"))
    return {"view": "boot", "session": session, "agent": b.agent, "identity": b.identity,
            "calls": b.calls, "settled_call": b.settled_call, "total": b.total,
            "signal": b.signal, "signal_text": b.signal_text, "signal_detail": b.signal_detail,
            "signal_call": b.signal_call, "held_for_brief": b.held_for_brief,
            "rows": rows, "warnings": list(b.warnings)}


# ---------- parts (and --tools) -------------------------------------------------
def _since_row_text(r: Row, p: Parts) -> str:
    a = p.anchors
    if r.category == OWN_OUTPUT:
        return _line(_data(r.label), tok(r.tokens), "exact: the output inside the counted "
                     f"intervals; every call's output: {tok(a.output_total)}", 4)
    if r.basis == "residual":
        return _line(_data(r.label), tok(r.tokens),
                     f"what the fit misses: {pct(p.unattributed_share, 'since boot')}", 4)
    how = "fitted" if r.basis == "fitted" else "fallback rate, not fitted"
    note = f"{how}: {chars(r.chars)} at {rate(r.rate)}"
    if r.category == RESULT:
        note += "; by tool: --tools"
    elif r.category == HOOK:
        note += "; by hook: --hooks"
    return _line(_data(r.label), tok(r.tokens, r.fitted), note, 4)


def _tools_lines(t: Tools) -> list[str]:
    out = []
    for r in t.rows:
        out.append(_line(_data(cut(r.tool, KEY_MAX)), tok(r.tokens, True),
                         f"{count(r.results, 'result')}, {chars(r.chars)} "
                         f"({pct(r.share, 'tool chars')})", 6))
    if t.largest:
        out.append(f"{' ' * 6}largest single results, by chars:")
        for x in t.largest:
            out.append(_line(_data(cut(x.key, KEY_MAX)), tok(x.tokens, True),
                             f"{chars(x.chars)}, read by call {x.call}", 8))
    return out


def parts_text(p: Parts, *, session: str | None = None, window: int | None = None,
               startup: Startup | None = None, tools: Tools | None = None) -> str:
    """`--parts` (with `tools=`, `--parts --tools`): boot, then since boot."""
    a = p.anchors
    lines = [(f"{_header('Context', session, p.model)}: "
             f"{_window_text(a.used if a.calls else None, window)}")]
    if not a.calls:
        lines.append("  no calls yet: nothing to split")
        return "\n".join(lines)
    lines.append(_line("AT BOOT", tok(a.boot), "exact: the first call's window"))
    if startup is not None:
        lines += _startup_lines(startup, 4)
    lines.append(_line("SINCE BOOT", tok(a.since_boot), "exact: the sum of every call's "
                       f"growth, over {count(a.intervals, 'counted interval')}"))
    for c in a.compactions:
        lines.append(f"    compaction at call {c.call}: {tok(c.before)} before, {tok(c.after)} "
                     f"after; {tok(c.dropped)} dropped, not subtracted from since boot")
    for r in p.rows:
        if not _seen(r):
            continue
        lines.append(_since_row_text(r, p))
        if tools is not None and r.category == RESULT:
            lines += _tools_lines(tools)
    lines += _warnings(p.warnings)
    lines += ["", LEGEND_FITTED, LEGEND_UNITS, LEGEND_UNATTRIBUTED]
    return "\n".join(lines)


def _since_row_json(r: Row) -> dict:
    seen = _seen(r)
    extra: dict[str, Any] = {"section": "since_boot", "chars": r.chars, "rate": r.rate,
                             "seen": seen}
    if r.category == OWN_OUTPUT:
        extra["output"] = "inside the counted intervals"
    return _row_json(r.category, r.label, r.tokens if seen else None, r.basis, r.exact,
                     r.fitted, **extra)


def tools_json(t: Tools) -> dict:
    return {"row": _since_row_json(t.row), "rate": t.rate, "basis": t.basis,
            "rows": [_row_json("tool", r.tool, r.tokens, t.basis, False, True, results=r.results,
                               chars=r.chars, share=r.share) for r in t.rows],
            "largest": [_row_json("tool_result", x.key, x.tokens, t.basis, False, True,
                                  tool=x.tool, call=x.call, chars=x.chars) for x in t.largest]}


def parts_json(p: Parts, *, session: str | None = None, window: int | None = None,
               startup: Startup | None = None, tools: Tools | None = None,
               band: str | None = None) -> dict:
    """`--parts --json`. `band` is `band.band_for(...)`'s answer, passed in."""
    a = p.anchors
    used = a.used if a.calls else None
    rows = [_row_json("boot", "At boot", a.boot if a.calls else None, "exact",
                      bool(a.calls), False, section="total")]
    if startup is not None:
        rows += _startup_rows_json(startup, "boot")
    rows.append(_row_json("since_boot", "Since boot", a.since_boot if a.calls else None,
                          "exact", bool(a.calls), False, section="total",
                          intervals=a.intervals))
    rows += [_since_row_json(r) for r in p.rows]
    out = {"view": "parts", "session": session, "model": p.model,
           **_window_json(used, window), "band": band,
           "calls": a.calls, "own_output_all_calls": a.output_total,
           "unattributed_share": p.unattributed_share, "poor_fit": p.poor_fit,
           "too_few_intervals": p.too_few_intervals, "fitted_intervals": p.fit.intervals,
           "compactions": [{"call": c.call, "before": c.before, "after": c.after,
                            "dropped": c.dropped} for c in a.compactions],
           "rows": rows, "warnings": list(p.warnings)}
    if tools is not None:
        out["tools"] = tools_json(tools)
    return out


# ---------- hooks -----------------------------------------------------------
def _hook_lines(r: HookRow) -> list[str]:
    events = ", ".join(_data(e) for e in r.events)
    out = [f"  {_data(cut(r.command, KEY_MAX))}  ({events})",
           (f"      {chars(r.chars)}, {pct(r.share, 'hook chars')}; "
           f"{count(r.lines, 'line')} in {count(r.batches, 'batch', 'batches')}"),
           (f"      repeats {chars(r.repeat_chars)} ({pct(r.repeat_share, 'its chars')}): "
           f"exact {chars(r.exact_repeat_chars)}, digits only "
           f"{chars(r.templated_repeat_chars)}; in one batch {chars(r.in_batch_repeat_chars)}"),
           f"      steady {chars(r.steady_chars)}"]
    for f in r.top:
        out.append(f"      \"{_data(cut(f.example, EXAMPLE_MAX))}\": "
                   f"{count(f.copies, 'copy', 'copies')} in "
                   f"{count(f.batches, 'batch', 'batches')}, {chars(f.chars)}"
                   f"{', steady' if f.steady else ''}")
    return out


def _hooks_split(h: Hooks, p: Parts | None) -> tuple[int, int, Row | None, int | None]:
    """(boot chars, since-boot chars, the fitted Hook context row, the gap:
    since-boot chars the row leaves out, in compactions' intervals)."""
    boot = sum(r.boot_chars for r in h.rows)
    since = h.total_chars - boot
    row = next((r for r in p.rows if r.category == HOOK), None) if p is not None else None
    gap = since - row.chars if row is not None and row.chars is not None else None
    return boot, since, row, gap


def hooks_text(h: Hooks, *, session: str | None = None, parts: Parts | None = None) -> str:
    """`--hooks`: hook context by command, with repeats."""
    lines = [(f"{_header('Hook context', session, None)}: {chars(h.total_chars)} from "
             f"{count(len(h.rows), 'hook command')} over {count(h.calls, 'call')} "
             f"(exact; every call, the boot call included), "
             f"{count(h.batches, 'tool batch', 'tool batches')}")]
    boot, since, row, gap = _hooks_split(h, parts)
    lines.append(f"  at boot (call 0): {chars(boot)}; since boot: {chars(since)}")
    if row is not None:
        note = (f"  the --parts Hook context row: {tok(row.tokens, row.fitted)} from "
                f"{chars(row.chars)}, since boot in the counted intervals only")
        if gap:
            note += f"; {chars(gap)} entered in a compaction's interval: here, not in that row"
        lines.append(note)
    lines.append(f"  repeats {chars(h.repeat_chars)} "
                 f"({pct(h.repeat_chars / h.total_chars if h.total_chars else None, 'hook chars')})"
                 f"; steady {chars(h.steady_chars)}; in one batch {chars(h.in_batch_repeat_chars)}")
    for r in h.rows:
        lines += _hook_lines(r)
    lines += ["", LEGEND_HOOKS.format(n=hooks_mod.STEADY_MIN_BATCHES), LEGEND_UNITS]
    return "\n".join(lines)


def hooks_json(h: Hooks, *, session: str | None = None, parts: Parts | None = None) -> dict:
    boot, since, row, gap = _hooks_split(h, parts)
    return {
        "view": "hooks", "session": session, "total_chars": h.total_chars, "calls": h.calls,
        "batches": h.batches, "boot_chars": boot, "since_boot_chars": since,
        "repeat_chars": h.repeat_chars, "steady_chars": h.steady_chars,
        "in_batch_repeat_chars": h.in_batch_repeat_chars,
        "parts_row": _since_row_json(row) if row is not None else None,
        "not_in_parts_row_chars": gap,
        "rows": [_row_json(HOOK, r.command, None, "exact", True, False,
                           unit="chars", command=r.command, events=list(r.events),
                           lines=r.lines, chars=r.chars, share=r.share, batches=r.batches,
                           boot_chars=r.boot_chars, exact_repeat_chars=r.exact_repeat_chars,
                           templated_repeat_chars=r.templated_repeat_chars,
                           in_batch_repeat_chars=r.in_batch_repeat_chars,
                           repeat_chars=r.repeat_chars, steady_chars=r.steady_chars,
                           top=[{"form": f.form, "example": f.example, "copies": f.copies,
                                 "batches": f.batches, "chars": f.chars, "steady": f.steady}
                                for f in r.top])
                 for r in h.rows],
    }
