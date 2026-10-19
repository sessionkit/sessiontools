"""Non-negative least squares, pure standard library: min ||Ax - y|| with
every x >= 0 (Lawson and Hanson's active-set method, 1974, chapter 23).

No numpy: `import sessiontools` stays stdlib-only, and the problem is tiny -
a handful of columns (one per category) over a few hundred rows (one per
interval). So the solver works on the normal equations, G = A'A and b = A'y,
which are p x p however many rows there are, with each column scaled to unit
norm first so a column of large char counts cannot swamp a small one.

    from sessiontools.context_parts.nnls import nnls
    x = nnls(rows, y)   # one coefficient per column, every one >= 0
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: Relative tolerance on the scaled problem: a gradient or a pivot this small
#: is zero.
TOL = 1e-10
MAX_ITERATIONS = 500


def _solve(g: list[list[float]], b: list[float], idx: list[int]) -> list[float] | None:
    """Solve g[idx][idx] s = b[idx] by Gaussian elimination with partial
    pivoting; None when the subsystem is singular."""
    n = len(idx)
    m = [[g[i][j] for j in idx] + [b[i]] for i in idx]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) <= TOL:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            if f:
                for k in range(col, n + 1):
                    m[r][k] -= f * m[col][k]
    s = [0.0] * n
    for r in range(n - 1, -1, -1):
        s[r] = (m[r][n] - sum(m[r][k] * s[k] for k in range(r + 1, n))) / m[r][r]
    return s


def nnls_normal(g: Sequence[Sequence[float]], b: Sequence[float]) -> list[float]:
    """NNLS from the normal equations G = A'A, b = A'y. A column whose norm
    is zero (a category that never entered) gets 0."""
    p = len(b)
    scale = [math.sqrt(g[j][j]) if g[j][j] > 0 else 0.0 for j in range(p)]
    live = [j for j in range(p) if scale[j] > 0]
    gs = [[g[i][j] / (scale[i] * scale[j]) if scale[i] and scale[j] else 0.0
           for j in range(p)] for i in range(p)]
    bs = [b[j] / scale[j] if scale[j] else 0.0 for j in range(p)]
    bmax = max((abs(v) for v in bs), default=0.0)
    tol = TOL * max(1.0, bmax)

    x = [0.0] * p
    passive: list[int] = []
    for _ in range(MAX_ITERATIONS):
        w = [bs[j] - sum(gs[j][k] * x[k] for k in range(p)) for j in range(p)]
        candidates = [j for j in live if j not in passive and w[j] > tol]
        if not candidates:
            break
        passive.append(max(candidates, key=lambda j: w[j]))
        while True:
            s_p = _solve(gs, bs, passive)
            if s_p is None:
                # The newest column is a combination of the others: it adds
                # nothing, so it leaves the passive set at zero.
                x[passive.pop()] = 0.0
                break
            s = [0.0] * p
            for j, v in zip(passive, s_p, strict=True):
                s[j] = v
            if all(s[j] > 0 for j in passive):
                x = s
                break
            alpha = min(x[j] / (x[j] - s[j]) for j in passive if s[j] <= 0)
            x = [x[j] + alpha * (s[j] - x[j]) for j in range(p)]
            for j in list(passive):
                if x[j] <= tol:
                    passive.remove(j)
                    x[j] = 0.0
    return [max(0.0, x[j] / scale[j]) if scale[j] else 0.0 for j in range(p)]


def normal_equations(rows: Sequence[Sequence[float]],
                     y: Sequence[float]) -> tuple[list[list[float]], list[float]]:
    """G = A'A and b = A'y, accumulated row by row."""
    p = len(rows[0]) if rows else 0
    g = [[0.0] * p for _ in range(p)]
    b = [0.0] * p
    for row, target in zip(rows, y, strict=True):
        for i in range(p):
            ri = row[i]
            if not ri:
                continue
            b[i] += ri * target
            gi = g[i]
            for j in range(p):
                gi[j] += ri * row[j]
    return g, b


def nnls(rows: Sequence[Sequence[float]], y: Sequence[float]) -> list[float]:
    """min ||Ax - y|| subject to x >= 0, A given as ROWS. Every coefficient
    returned is >= 0; an empty problem gives an empty list."""
    if not rows:
        return []
    g, b = normal_equations(rows, y)
    return nnls_normal(g, b)
