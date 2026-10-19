"""Exact anchors, a per-session fit, and the residual.

ANCHORS ARE EXACT, straight from the API's own counts:
- boot       = the first call's ctx;
- since boot = the sum of every POSITIVE per-call growth, ctx[k+1] - ctx[k].
  A negative growth is a compaction: counted and reported as a line, never
  subtracted from since-boot;
- own output = the output_tokens of each call whose output entered the window
  inside a counted interval (every call but the last, and but one followed
  by a compaction). `output_total` is every call's, the last one included.

THE SPLIT IS FITTED: per interval, growth minus own output is regressed on
each since-boot category's chars (non-negative least squares), giving one
tokens-per-char rate per category for THIS session. A row is its chars over
the counted intervals times its rate.

THE RESIDUAL IS ALWAYS THERE: Unattributed = since boot - own output - the
fitted rows. Over 10% of since-boot (either sign) sets `poor_fit` and its
warning line. Under MIN_INTERVALS counted intervals there is no fit: every
rate is FALLBACK_RATE (0.25 tokens per char, chars/4) and `too_few_intervals`
is set, with its own line.

Data only: nothing here renders. Every row carries `basis` - "exact",
"fitted", "fallback" or "residual" - and `exact` / `fitted` follow from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sessiontools.context_parts.nnls import nnls
from sessiontools.context_parts.transcript import (
    FIT_CATEGORIES,
    LABELS,
    OTHER,
    Fold,
    fold,
)

#: Fewer counted intervals than this and the session is not fitted.
MIN_INTERVALS = 30
#: The young-seat rate: chars/4.
FALLBACK_RATE = 0.25
#: An Unattributed share above this (either sign) is a poor fit.
POOR_FIT_SHARE = 0.10

OWN_OUTPUT = "own_output"
UNATTRIBUTED = "unattributed"
ROW_LABELS: dict[str, str] = {
    OWN_OUTPUT: "Own output (thinking, text, calls)",
    **{c: LABELS[c] for c in FIT_CATEGORIES},
    UNATTRIBUTED: "Unattributed",
}

POOR_FIT_WARNING = "fit is poor for this session; rows are rough"
TOO_FEW_WARNING = "too few calls to fit; rows are chars/4"


@dataclass(frozen=True)
class Compaction:
    """A call whose window was smaller than the one before it."""

    call: int        # the index of the call after the drop
    before: int      # ctx of the call before
    after: int       # ctx of this call

    @property
    def dropped(self) -> int:
        return self.before - self.after


@dataclass(frozen=True)
class Anchors:
    """The exact totals. `used` is the last call's ctx (the window now)."""

    calls: int = 0
    boot: int = 0
    used: int = 0
    since_boot: int = 0
    own_output: int = 0
    output_total: int = 0
    intervals: int = 0                      # counted (non-negative) intervals
    compactions: tuple[Compaction, ...] = ()


@dataclass(frozen=True)
class Fit:
    """One tokens-per-char rate per since-boot category. A category that
    never entered has rate None. `fitted` False means the fallback rate."""

    rates: dict[str, float | None]
    fitted: bool
    intervals: int


@dataclass(frozen=True)
class Row:
    """One since-boot row. `tokens` is exact for own output, chars x rate for
    a category, and the remainder for Unattributed."""

    category: str
    label: str
    tokens: float
    basis: str                       # exact | fitted | fallback | residual
    chars: int | None = None
    rate: float | None = None

    @property
    def exact(self) -> bool:
        return self.basis == "exact"

    @property
    def fitted(self) -> bool:
        return self.basis in ("fitted", "fallback")


@dataclass(frozen=True)
class Parts:
    """What filled the window since boot: anchors, rows, the residual."""

    anchors: Anchors
    fit: Fit
    rows: tuple[Row, ...]
    unattributed: float
    unattributed_share: float | None
    poor_fit: bool
    too_few_intervals: bool
    warnings: tuple[str, ...]
    model: str | None = None


def _fit_category(category: str) -> str:
    """A boot kind arriving after boot (a nested CLAUDE.md, a skills delta)
    is fitted with the other attachments."""
    return category if category in FIT_CATEGORIES else OTHER


def _intervals(f: Fold) -> list[tuple[int, int, dict[str, int]]]:
    """(growth, own output, chars by fit category) per COUNTED interval."""
    out = []
    calls = f.calls
    for k in range(len(calls) - 1):
        growth = calls[k + 1].ctx - calls[k].ctx
        if growth < 0:
            continue
        chars: dict[str, int] = {}
        for e in calls[k + 1].entered:
            c = _fit_category(e.category)
            chars[c] = chars.get(c, 0) + e.chars
        out.append((growth, calls[k].output_tokens, chars))
    return out


def anchors(f: Fold) -> Anchors:
    """The exact totals of a fold."""
    calls = f.calls
    if not calls:
        return Anchors()
    since = own = 0
    compactions = []
    intervals = 0
    for k in range(len(calls) - 1):
        growth = calls[k + 1].ctx - calls[k].ctx
        if growth < 0:
            compactions.append(Compaction(k + 1, calls[k].ctx, calls[k + 1].ctx))
            continue
        intervals += 1
        since += growth
        own += calls[k].output_tokens
    return Anchors(calls=len(calls), boot=calls[0].ctx, used=calls[-1].ctx,
                   since_boot=since, own_output=own,
                   output_total=sum(c.output_tokens for c in calls),
                   intervals=intervals, compactions=tuple(compactions))


def fit(f: Fold, min_intervals: int = MIN_INTERVALS) -> Fit:
    """One rate per since-boot category: NNLS over the counted intervals,
    or FALLBACK_RATE for every category under `min_intervals` of them."""
    ivs = _intervals(f)
    seen = {c for _, _, chars in ivs for c, n in chars.items() if n > 0}
    if len(ivs) < min_intervals:
        return Fit({c: (FALLBACK_RATE if c in seen else None) for c in FIT_CATEGORIES},
                   fitted=False, intervals=len(ivs))
    rows = [[float(chars.get(c, 0)) for c in FIT_CATEGORIES] for _, _, chars in ivs]
    y = [float(g - out) for g, out, _ in ivs]
    x = nnls(rows, y)
    return Fit({c: (x[i] if c in seen else None) for i, c in enumerate(FIT_CATEGORIES)},
               fitted=True, intervals=len(ivs))


def parts(source: Fold | Path | str, min_intervals: int = MIN_INTERVALS) -> Parts:
    """The since-boot breakdown of a transcript (a path) or a fold."""
    f = source if isinstance(source, Fold) else fold(Path(source))
    a = anchors(f)
    ft = fit(f, min_intervals)
    totals: dict[str, int] = {c: 0 for c in FIT_CATEGORIES}
    for _, _, chars in _intervals(f):
        for c, n in chars.items():
            totals[c] += n

    basis = "fitted" if ft.fitted else "fallback"
    rows = [Row(OWN_OUTPUT, ROW_LABELS[OWN_OUTPUT], float(a.own_output), "exact")]
    for c in FIT_CATEGORIES:
        rate = ft.rates[c]
        rows.append(Row(c, ROW_LABELS[c], totals[c] * (rate or 0.0), basis,
                        chars=totals[c], rate=rate))
    unattributed = a.since_boot - sum(r.tokens for r in rows)
    share = unattributed / a.since_boot if a.since_boot else None
    rows.append(Row(UNATTRIBUTED, ROW_LABELS[UNATTRIBUTED], unattributed, "residual"))

    poor = share is not None and abs(share) > POOR_FIT_SHARE
    too_few = not ft.fitted
    warnings = tuple(w for w, on in ((TOO_FEW_WARNING, too_few), (POOR_FIT_WARNING, poor))
                     if on)
    return Parts(anchors=a, fit=ft, rows=tuple(rows), unattributed=unattributed,
                 unattributed_share=share, poor_fit=poor, too_few_intervals=too_few,
                 warnings=warnings, model=f.model)
