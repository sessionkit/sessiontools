"""context_parts' NNLS and per-session fit (ST-26 do-001, design section
3.2): planted rates recovered, never a negative rate, the residual always
present, the poor-fit and too-few-calls flags."""

from __future__ import annotations

import importlib.util
import itertools
import math
import random
from pathlib import Path

import pytest

from sessiontools import context_parts as cp
from sessiontools.context_parts.nnls import nnls, nnls_normal

FIXTURES = Path(__file__).parent / "fixtures" / "context_parts"


def _build_module():
    spec = importlib.util.spec_from_file_location("context_parts_build", FIXTURES / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RATES = _build_module().RATES


# ---------- the solver ------------------------------------------------------

def residual(rows, y, x):
    return math.sqrt(sum((sum(a * b for a, b in zip(r, x, strict=True)) - t) ** 2
                         for r, t in zip(rows, y, strict=True)))


def lstsq_subset(rows, y, cols):
    """Unconstrained least squares on a column subset (normal equations,
    Gauss-Jordan); None when singular. The brute-force reference."""
    n = len(cols)
    g = [[sum(r[i] * r[j] for r in rows) for j in cols] for i in cols]
    b = [sum(r[i] * t for r, t in zip(rows, y, strict=True)) for i in cols]
    m = [g[i] + [b[i]] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda k: abs(m[k][c]))
        if abs(m[piv][c]) < 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        for k in range(n):
            if k != c:
                f = m[k][c] / m[c][c]
                m[k] = [a - f * bb for a, bb in zip(m[k], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def brute_nnls_residual(rows, y):
    """The best residual over every feasible active set: the true NNLS optimum."""
    p = len(rows[0])
    best = residual(rows, y, [0.0] * p)
    for k in range(1, p + 1):
        for cols in itertools.combinations(range(p), k):
            s = lstsq_subset(rows, y, cols)
            if s is None or any(v < 0 for v in s):
                continue
            x = [0.0] * p
            for c, v in zip(cols, s, strict=True):
                x[c] = v
            best = min(best, residual(rows, y, x))
    return best


def test_nnls_recovers_an_exact_positive_solution():
    rows = [[1, 2, 0], [0, 1, 3], [2, 0, 1], [1, 1, 1], [3, 1, 2]]
    truth = [0.5, 1.25, 2.0]
    y = [sum(a * b for a, b in zip(r, truth, strict=True)) for r in rows]
    x = nnls(rows, y)
    assert x == pytest.approx(truth, rel=1e-9)


def test_nnls_clamps_where_least_squares_would_go_negative():
    rows = [[1, 0], [0, 1], [1, 1]]
    y = [1, -1, 0]          # unconstrained: x = (1/3*..., negative second)
    x = nnls(rows, y)
    assert x == pytest.approx([0.5, 0.0], abs=1e-12)


def test_nnls_all_negative_targets_give_all_zero():
    assert nnls([[1, 2], [3, 4]], [-5, -1]) == [0.0, 0.0]


def test_nnls_zero_column_and_empty_problem():
    x = nnls([[1, 0], [2, 0], [3, 0]], [2, 4, 6])
    assert x == pytest.approx([2.0, 0.0])
    assert nnls([], []) == []
    assert nnls_normal([[0.0]], [0.0]) == [0.0]


def test_nnls_duplicate_columns_stay_non_negative_and_optimal():
    rows = [[1, 1, 2], [2, 2, 1], [3, 3, 0], [1, 1, 1]]
    y = [3, 5, 6, 3]
    x = nnls(rows, y)
    assert min(x) >= 0
    assert residual(rows, y, x) == pytest.approx(brute_nnls_residual(rows, y), abs=1e-6)


@pytest.mark.parametrize("seed", range(40))
def test_nnls_matches_brute_force_and_is_never_negative(seed):
    rng = random.Random(seed)
    p = rng.randint(1, 5)
    rows = [[rng.choice([0.0, rng.uniform(0, 1000)]) for _ in range(p)]
            for _ in range(rng.randint(p, 30))]
    y = [rng.uniform(-500, 1500) for _ in rows]
    x = nnls(rows, y)
    assert len(x) == p and min(x) >= 0
    assert residual(rows, y, x) == pytest.approx(brute_nnls_residual(rows, y),
                                                 rel=1e-6, abs=1e-6)


# ---------- the fit on planted rates ------------------------------------------

def test_the_fit_recovers_planted_rates_within_two_percent():
    p = cp.parts(FIXTURES / "synthetic.jsonl")
    assert p.fit.fitted and p.fit.intervals == 150
    for category, planted in RATES.items():
        got = p.fit.rates[category]
        assert got is not None and got >= 0
        assert abs(got - planted) / planted < 0.02, (category, got, planted)
    assert not p.poor_fit and not p.too_few_intervals and p.warnings == ()
    assert abs(p.unattributed_share) < 0.01


def test_rows_are_chars_times_rate_and_the_residual_closes_since_boot():
    p = cp.parts(FIXTURES / "synthetic.jsonl")
    rows = {r.category: r for r in p.rows}
    assert [r.category for r in p.rows] == ["own_output", *cp.FIT_CATEGORIES, "unattributed"]
    assert rows["own_output"].tokens == p.anchors.own_output
    assert rows["own_output"].exact and not rows["own_output"].fitted
    for c in cp.FIT_CATEGORIES:
        r = rows[c]
        assert r.fitted and not r.exact and r.basis == "fitted"
        assert r.tokens == pytest.approx(r.chars * r.rate)
        assert r.label == cp.LABELS[c]
    un = rows["unattributed"]
    assert un.basis == "residual" and not un.exact and not un.fitted
    assert un.tokens == p.unattributed
    assert sum(r.tokens for r in p.rows) == pytest.approx(p.anchors.since_boot)
    assert p.unattributed_share == pytest.approx(p.unattributed / p.anchors.since_boot)


def test_the_unattributed_row_is_always_present():
    for name in ("anchors", "compaction", "synthetic", "young", "poor"):
        p = cp.parts(FIXTURES / f"{name}.jsonl")
        assert p.rows[-1].category == "unattributed", name


def test_unexplained_growth_is_a_poor_fit_with_its_warning():
    p = cp.parts(FIXTURES / "poor.jsonl")
    assert p.fit.fitted
    assert p.unattributed_share > 0.10
    assert p.poor_fit
    assert p.warnings == ("fit is poor for this session; rows are rough",)
    assert all(r >= 0 for r in p.fit.rates.values() if r is not None)


def test_under_thirty_intervals_is_chars_over_four_and_flagged():
    p = cp.parts(FIXTURES / "young.jsonl")
    assert p.anchors.intervals == 10 < cp.MIN_INTERVALS == 30
    assert not p.fit.fitted and p.too_few_intervals
    assert "too few calls to fit; rows are chars/4" in p.warnings
    for r in p.rows:
        if r.category in cp.FIT_CATEGORIES:
            assert r.basis == "fallback" and r.fitted
            if r.chars:
                assert r.rate == cp.FALLBACK_RATE == 0.25
                assert r.tokens == r.chars / 4
            else:
                assert r.rate is None and r.tokens == 0


def test_the_threshold_is_counted_intervals_not_calls():
    young = cp.fold(FIXTURES / "young.jsonl")
    assert not cp.fit(young).fitted
    assert cp.fit(young, min_intervals=10).fitted
    assert not cp.fit(young, min_intervals=11).fitted


def test_a_category_that_never_entered_has_no_rate(tmp_path):
    p = cp.parts(FIXTURES / "anchors.jsonl")
    assert p.fit.rates["reminder"] is None and p.fit.rates["other"] is None
    assert p.fit.rates["result"] == cp.FALLBACK_RATE


def test_a_boot_kind_after_boot_is_fitted_as_other():
    f = cp.Fold(calls=(cp.Call(100, 0), cp.Call(150, 0, (cp.Entry("memory", "/x", 200),))))
    p = cp.parts(f)
    assert {r.category: r.chars for r in p.rows if r.chars is not None}["other"] == 200


def test_no_calls_gives_no_share_and_no_poor_fit():
    p = cp.parts(cp.Fold())
    assert p.anchors.since_boot == 0 and p.unattributed == 0
    assert p.unattributed_share is None and not p.poor_fit and p.too_few_intervals


def test_a_negative_residual_beyond_ten_percent_is_also_poor():
    # every interval's growth is far BELOW what its chars imply at any rate
    # the fit can share with the over-predicted ones: force it by fallback
    calls = [cp.Call(1000, 0)]
    for k in range(5):
        calls.append(cp.Call(1000 + 10 * (k + 1), 0, (cp.Entry("result", "r", 400),)))
    p = cp.parts(cp.Fold(calls=tuple(calls)))
    assert p.too_few_intervals
    assert p.unattributed == 50 - 5 * 100
    assert p.poor_fit and p.unattributed_share < -0.10
