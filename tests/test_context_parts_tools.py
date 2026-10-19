"""`--tools` data (ST-26 do-003): the fitted Tool results row split by tool,
with the 5 largest results by chars and their key.

Fixtures: tests/fixtures/context_hooks/tools.jsonl (written by its build.py),
and every context_parts fixture, so the sums hold on fitted and chars/4
sessions alike."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

from sessiontools.context_parts import tools as T
from sessiontools.context_parts.breakdown import parts
from sessiontools.context_parts.transcript import RESULT

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TOOLS = FIXTURES / "context_hooks" / "tools.jsonl"

_spec = importlib.util.spec_from_file_location("context_hooks_fixture_build_for_tools",
                                               FIXTURES / "context_hooks" / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def every_fixture() -> list[Path]:
    return sorted(FIXTURES.glob("context_*/*.jsonl"))


@pytest.mark.parametrize("path", every_fixture(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_tool_rows_sum_to_the_tool_results_row(path):
    t = T.tools(path)
    row = next(r for r in parts(path).rows if r.category == RESULT)
    assert t.row == row                                  # the row itself, unchanged
    assert t.chars == row.chars                          # exact
    assert math.isclose(t.tokens, row.tokens, rel_tol=1e-12, abs_tol=1e-9)
    assert t.basis == row.basis and t.rate == row.rate
    if t.rows:
        assert math.isclose(math.fsum(r.share for r in t.rows), 1.0, abs_tol=1e-12)


def test_the_fitted_row_is_split_by_tool_at_its_own_rate():
    t = T.tools(TOOLS)
    assert t.basis == "fitted"
    assert t.rate == pytest.approx(build.TOOL_RATE, rel=1e-3)
    assert {r.tool: (r.results, r.chars) for r in t.rows} == {
        "Bash": (33, 4000 + 8000 + sum(200 + 10 * k for k in [*range(9, 20), *range(21, 41)])),
        "Read": (3, 9000 + 6000 + 6000),                  # /w/huge.md is across the compaction
        "mcp__sessionmail__mail_read": (1, 1500),
        "Grep": (1, 700),
        "tool_result": (1, 600),                          # its tool_use was never written
    }
    for r in t.rows:
        assert r.tokens == r.chars * t.rate
        assert r.share == r.chars / t.chars
    assert [r.chars for r in t.rows] == sorted((r.chars for r in t.rows), reverse=True)


def test_the_five_largest_are_by_chars_with_their_key():
    t = T.tools(TOOLS)
    assert [(r.key, r.tool, r.call, r.chars) for r in t.largest] == [
        ("Read /w/big.md", "Read", 1, 9000),
        ("Bash cat log.txt", "Bash", 5, 8000),
        ("Read /w/tie-a.md", "Read", 7, 6000),            # a tie keeps transcript order
        ("Read /w/tie-b.md", "Read", 8, 6000),
        ("Bash make test", "Bash", 2, 4000),
    ]
    assert all(r.tokens == r.chars * t.rate for r in t.largest)


def test_a_result_across_a_compaction_is_in_neither_the_row_nor_the_largest():
    t = T.tools(TOOLS)
    assert t.parts.anchors.compactions
    assert all(r.key != "Read /w/huge.md" for r in t.largest)
    assert t.chars == t.row.chars


def test_a_young_seat_is_split_at_chars_over_four():
    young = FIXTURES / "context_parts" / "young.jsonl"
    t = T.tools(young)
    assert (t.basis, t.rate) == ("fallback", 0.25)
    assert all(r.tokens == r.chars * 0.25 for r in t.rows)


def test_top_is_a_setting(tmp_path):
    assert len(T.tools(TOOLS, top=2).largest) == 2
    assert T.tools(TOOLS, top=0).largest == ()
    assert len(T.tools(TOOLS, top=99).largest) == sum(r.results for r in T.tools(TOOLS).rows)


def test_no_tool_results_is_an_empty_split(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(build.dump(r) for r in [build.assistant("a", 100, 1),
                                                   build.assistant("b", 200, 1)]) + "\n",
                 encoding="utf-8")
    t = T.tools(p)
    assert (t.rows, t.largest, t.chars, t.tokens, t.rate) == ((), (), 0, 0, None)


@pytest.mark.parametrize("key, name", [
    ("Read /etc/hosts", "Read"),
    ("Bash git status", "Bash"),
    ("mcp__sessionmail__mail_read", "mcp__sessionmail__mail_read"),
    ("tool_result", "tool_result"),
])
def test_tool_name_is_the_keys_first_word(key, name):
    assert T.tool_name(key) == name


def test_a_fold_or_a_path_give_the_same_view():
    from sessiontools.context_parts.transcript import fold
    assert T.tools(fold(TOOLS)) == T.tools(TOOLS)
    assert T.tools(str(TOOLS)) == T.tools(TOOLS)
