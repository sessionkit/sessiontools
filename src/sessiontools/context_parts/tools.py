"""`--tools`: the fitted "Tool results" row split by tool, and the largest
results.

A tool result is joined to the `tool_use` that asked for it by the fold
(`Entry(RESULT, key, chars)`, key = the tool name, then its path or
command). This view takes exactly the results `breakdown.parts` puts in its
Tool results row - those entered in a COUNTED interval (not the boot call,
not across a compaction) - and multiplies each tool's chars by that row's
own rate. So:

- the tools' chars sum to the row's chars exactly, and their tokens to the
  row's tokens (one rate times a split sum; float addition aside);
- every figure is as fitted as the row: `basis` is the row's ("fitted", or
  "fallback" at chars/4 for a young seat);
- the LARGEST are single results by chars, with their key and the call that
  read them; a tie keeps transcript order.

A result whose `tool_use` the fold never saw is keyed `tool_result` and is
its own row, not dropped.

Data only: nothing here renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sessiontools.context_parts.breakdown import MIN_INTERVALS, Parts, Row, parts
from sessiontools.context_parts.transcript import RESULT, Fold, fold

#: How many single results the view names.
TOP_RESULTS = 5


def tool_name(key: str) -> str:
    """'Read /etc/hosts' -> 'Read'; 'tool_result' (unjoined) stays itself."""
    return key.split(" ", 1)[0]


@dataclass(frozen=True)
class ToolRow:
    """One tool's share of the Tool results row."""

    tool: str
    results: int
    chars: int
    tokens: float                  # chars x the row's rate
    share: float                   # of the row's chars (and so of its tokens)


@dataclass(frozen=True)
class ToolResult:
    """One tool result: `call` is the call that read it."""

    key: str
    tool: str
    call: int
    chars: int
    tokens: float


@dataclass(frozen=True)
class Tools:
    """The Tool results row (`breakdown.parts`'s, unchanged), split by tool,
    largest first, and the TOP_RESULTS largest single results."""

    row: Row
    rate: float | None
    basis: str
    rows: tuple[ToolRow, ...]
    largest: tuple[ToolResult, ...]
    parts: Parts

    @property
    def chars(self) -> int:
        return sum(r.chars for r in self.rows)

    @property
    def tokens(self) -> float:
        return sum(r.tokens for r in self.rows)


def _counted_results(f: Fold) -> list[tuple[int, str, int]]:
    """(call, key, chars) of every result in a counted interval: the same
    entries breakdown sums into its Tool results row."""
    out = []
    calls = f.calls
    for k in range(len(calls) - 1):
        if calls[k + 1].ctx - calls[k].ctx < 0:
            continue
        out.extend((k + 1, e.key, e.chars) for e in calls[k + 1].entered if e.category == RESULT)
    return out


def tools(source: Fold | Path | str, top: int = TOP_RESULTS,
          min_intervals: int = MIN_INTERVALS) -> Tools:
    """`--tools`: Tool results by tool, with the `top` largest results."""
    f = source if isinstance(source, Fold) else fold(Path(source))
    p = parts(f, min_intervals)
    row = next(r for r in p.rows if r.category == RESULT)
    rate = row.rate
    r = rate or 0.0
    results = _counted_results(f)
    total = sum(chars for _, _, chars in results)

    by_tool: dict[str, list[int]] = {}
    for _, key, chars in results:
        by_tool.setdefault(tool_name(key), []).append(chars)
    rows = [ToolRow(t, len(cs), sum(cs), sum(cs) * r, sum(cs) / total if total else 0.0)
            for t, cs in by_tool.items()]
    rows.sort(key=lambda x: -x.chars)                     # stable: first seen wins a tie

    ranked = sorted(results, key=lambda x: -x[2])         # stable: transcript order on a tie
    largest = tuple(ToolResult(key, tool_name(key), call, chars, chars * r)
                    for call, key, chars in ranked[:max(top, 0)])
    return Tools(row=row, rate=rate, basis=row.basis, rows=tuple(rows), largest=largest,
                 parts=p)
