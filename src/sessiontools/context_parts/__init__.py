"""What filled a session's context window: the fold, the exact anchors and a
per-session fit.

A NEW package on purpose: `window.scan` and `settle` stay byte-identical, so the default
`session context` one-liner and settle's hook path pay nothing. Only the
flag views (`--parts`, `--startup`, `--boot`, `--hooks`, `--tools`) read a
transcript a second time, through here.

    from sessiontools import context_parts
    p = context_parts.parts(transcript_path)
    p.anchors.boot, p.anchors.since_boot, p.anchors.own_output   # exact
    for row in p.rows: row.label, row.tokens, row.basis           # data, no rendering
    p.unattributed, p.unattributed_share, p.poor_fit, p.warnings

Three modules: `transcript` (the fold: calls and what entered before each),
`breakdown` (exact anchors, the fit, the residual) and `nnls` (the solver).
Standard library only: `import sessiontools` reaches nothing else.
"""

from __future__ import annotations

from sessiontools.context_parts.breakdown import (
    FALLBACK_RATE,
    MIN_INTERVALS,
    POOR_FIT_SHARE,
    Anchors,
    Compaction,
    Fit,
    Parts,
    Row,
    anchors,
    fit,
    parts,
)
from sessiontools.context_parts.transcript import (
    BOOT_CATEGORIES,
    CATEGORIES,
    FIT_CATEGORIES,
    LABELS,
    Call,
    Entry,
    Fold,
    fold,
)

__all__ = [
    "BOOT_CATEGORIES", "CATEGORIES", "FALLBACK_RATE", "FIT_CATEGORIES", "LABELS",
    "MIN_INTERVALS", "POOR_FIT_SHARE", "Anchors", "Call", "Compaction", "Entry",
    "Fit", "Fold", "Parts", "Row", "anchors", "fit", "fold", "parts",
]
