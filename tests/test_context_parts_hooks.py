"""`--hooks` data (ST-26 do-003): hook context by hook command, with chars,
share and repeats (exact, digit-normalised, steady); the port of an
earlier hook-dupes script.

Fixtures: tests/fixtures/context_hooks/, written by its build.py. The hand
sums below come from the line lengths its docstring states."""

from __future__ import annotations

import importlib.util
import math
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools.context_parts import hooks as H
from sessiontools.context_parts.transcript import HOOK, fold

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "context_hooks"
HOOKS = FIXTURES / "hooks.jsonl"

_spec = importlib.util.spec_from_file_location("context_hooks_fixture_build",
                                               FIXTURES / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def rows() -> dict[str, H.HookRow]:
    return {r.command: r for r in H.hooks(HOOKS).rows}


# ---------- the fixtures are what build.py writes ---------------------------
def test_the_committed_fixtures_are_what_build_py_writes(tmp_path):
    out = subprocess.run([sys.executable, str(FIXTURES / "build.py"), str(tmp_path)],
                         capture_output=True, text=True, check=True)
    built = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert built == sorted(p.name for p in FIXTURES.glob("*.jsonl"))
    assert len(out.stdout.splitlines()) == len(built)
    for name in built:
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes(), (
            f"{name} differs from build.py's output: re-run build.py and commit")


def test_the_fixture_lines_are_the_lengths_the_hand_sums_use():
    assert [len(build.STEADY_LINE), len(build.clock_line(7)), len(build.posttool_line(35)),
            len(build.mail_line(24)), len(build.HALF_LINE), len(build.TWELVE_LINE),
            len(build.PROMPT_LINE)] == [36, 18, 12, 16, 19, 20, 16]


# ---------- chars and share: exact, summing to the hook total ---------------
#: command -> (lines, chars), by hand from build.py.
HAND = {
    "pager-hook": (24, 24 * 36),
    "posttool-clock": (36, 36 * 12),          # 6 batches x 3 tools + 18 x 1
    "notifications": (24, 24 * 18),
    "session-start": (1, 400),
    "mail-hook": (24, 24 * 16),
    "twelve-hook": (12, 12 * 20),
    "half-hook": (11, 11 * 19),
    "UserPromptSubmit": (1, 16),               # no hook_success: its event name
}


def test_each_hook_commands_chars_by_hand():
    got = {c: (r.lines, r.chars) for c, r in rows().items()}
    assert got == HAND


def test_chars_and_share_sum_to_the_hook_total_exactly():
    h = H.hooks(HOOKS)
    assert h.total_chars == sum(chars for _, chars in HAND.values()) == 2977
    assert sum(r.chars for r in h.rows) == h.total_chars
    assert math.isclose(math.fsum(r.share for r in h.rows), 1.0, rel_tol=0, abs_tol=1e-12)
    for r in h.rows:
        assert r.share == r.chars / h.total_chars


def test_rows_are_largest_first():
    chars = [r.chars for r in H.hooks(HOOKS).rows]
    assert chars == sorted(chars, reverse=True)


def test_the_total_is_the_folds_hook_context_over_every_call():
    """Same join, same calls: the fold's Hook context entries, by command."""
    by_key: dict[str, int] = {}
    for call in fold(HOOKS).calls:
        for e in call.entered:
            if e.category == HOOK:
                by_key[e.key] = by_key.get(e.key, 0) + e.chars
    assert {c: r.chars for c, r in rows().items()} == by_key


def test_a_line_no_call_has_read_is_not_counted():
    assert "tail-hook" not in rows()
    assert build.TAIL_LINE in HOOKS.read_text(encoding="utf-8")


def test_calls_and_batches():
    h = H.hooks(HOOKS)
    assert (h.calls, h.batches) == (25, 24)     # call 0 boots; 24 tool batches


def test_two_hooks_under_one_batch_id_join_their_own_commands():
    r = rows()
    assert r["pager-hook"].top[0].example == build.STEADY_LINE
    assert r["notifications"].top[0].form == "[clock] #:# AEDT"


def test_boot_chars_are_the_part_the_first_call_read():
    r = rows()
    assert r["session-start"].boot_chars == 400
    assert r["session-start"].events == ("SessionStart",)
    assert all(x.boot_chars == 0 for c, x in r.items() if c != "session-start")


# ---------- repeats ---------------------------------------------------------
def test_one_hook_repeating_its_context_every_batch_is_repeating():
    """The same line in all 24 batches: every copy after the first is an
    exact repeat, and the line is steady."""
    r = rows()["pager-hook"]
    assert r.repeating
    assert (r.exact_repeat_chars, r.templated_repeat_chars) == (23 * 36, 0)
    assert r.repeat_chars == 23 * 36
    assert r.steady_chars == r.chars == 24 * 36
    assert r.batches == 24
    assert r.top == (H.HookForm(build.STEADY_LINE, build.STEADY_LINE, 24, 24, 864, True),)


def test_a_clock_line_differing_only_in_digits_is_a_repeat():
    r = rows()["notifications"]
    assert r.repeating
    assert (r.exact_repeat_chars, r.templated_repeat_chars) == (0, 23 * 18)
    assert r.steady_chars == 24 * 18
    assert r.top[0].copies == 24 and r.top[0].example == build.clock_line(1)


def test_in_batch_repeats_are_a_per_tool_hook_within_one_batch():
    r = rows()["posttool-clock"]
    assert r.templated_repeat_chars == 35 * 12
    assert r.in_batch_repeat_chars == 6 * 2 * 12     # 2 extra lines in each 3-tool batch
    assert r.in_batch_repeat_chars <= r.repeat_chars
    assert r.events == ("PostToolUse",)


def test_distinct_lines_are_not_repeats():
    r = rows()["mail-hook"]
    assert not r.repeating
    assert (r.repeat_chars, r.in_batch_repeat_chars, r.steady_chars) == (0, 0, 0)
    assert len(r.top) == H.TOP_FORMS                     # 24 forms, the 5 kept


def test_steady_needs_half_of_the_batches():
    r = rows()
    assert r["twelve-hook"].steady_chars == 240       # 12 of 24: exactly half
    assert r["half-hook"].steady_chars == 0           # 11 of 24: under half
    assert r["half-hook"].exact_repeat_chars == 10 * 19
    assert r["twelve-hook"].exact_repeat_chars == 11 * 20


def test_session_totals():
    h = H.hooks(HOOKS)
    assert h.repeat_chars == 23 * 36 + 23 * 18 + 35 * 12 + 11 * 20 + 10 * 19
    assert h.steady_chars == 864 + 432 + 432 + 240
    assert h.in_batch_repeat_chars == 144


@pytest.mark.parametrize("line, form", [
    ("[clock] 13:07 AEDT", "[clock] #:# AEDT"),
    ("ctx 42% of 1000000", "ctx #% of #"),
    ("no digits", "no digits"),
    ("v2.1.296", "v#.#.#"),
])
def test_normalise_makes_every_digit_run_one_mark(line, form):
    assert H.normalise(line) == form


# ---------- small cases -----------------------------------------------------
def write(tmp_path: Path, recs: list[dict]) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(build.dump(r) for r in recs) + "\n", encoding="utf-8")
    return p


def test_no_hooks_and_no_calls_are_empty_not_errors(tmp_path):
    h = H.hooks(write(tmp_path, []))
    assert (h.rows, h.total_chars, h.calls, h.batches) == ((), 0, 0, 0)
    h = H.hooks(write(tmp_path, [build.assistant("a", 100, 1), build.assistant("b", 200, 1)]))
    assert (h.rows, h.total_chars, h.calls) == ((), 0, 2)


def test_steady_also_needs_ten_batches(tmp_path):
    """A line in every one of 9 batches is 100% of them, but under ten."""
    recs = []
    for k in range(10):
        recs.append(build.assistant(f"c{k}", 1000 + k, 1,
                                    [build.tool_use(f"t{k}", "Bash", command="ls")]))
        recs.append(build.tool_result(f"t{k}", "r"))
        if k < 9:
            recs += build.hook(f"h{k}", "every", ["same"])
    recs.append(build.assistant("end", 2000, 1))
    r = H.hooks(write(tmp_path, recs)).rows[0]
    assert (r.batches, r.steady_chars, r.exact_repeat_chars) == (9, 0, 8 * 4)


def test_with_no_tool_batch_every_call_is_a_batch(tmp_path):
    recs = []
    for k in range(12):
        recs += build.hook(f"u{k}", "prompt", ["typed hint"], "UserPromptSubmit")
        recs.append(build.assistant(f"c{k}", 1000 + k, 1))
    h = H.hooks(write(tmp_path, recs))
    assert (h.calls, h.batches) == (12, 12)
    assert h.rows[0].steady_chars == 12 * len("typed hint")


def test_sidechain_and_bad_lines_are_skipped(tmp_path):
    recs = build.hook("s", "side", ["sidechain line"])
    for r in recs:
        r["isSidechain"] = True
    recs += build.hook("m", "main", ["main line"])
    recs.append(build.assistant("a", 100, 1))
    p = write(tmp_path, recs)
    p.write_text(p.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")
    assert [r.command for r in H.hooks(p).rows] == ["main"]


def test_a_hook_line_written_before_its_success_still_joins(tmp_path):
    pair = build.hook("x", "late-success", ["ordered the other way"])
    recs = [pair[1], pair[0], build.assistant("a", 100, 1)]
    assert H.hooks(write(tmp_path, recs)).rows[0].command == "late-success"


def test_hooks_imports_only_the_standard_library():
    code = ("import sys; before = set(sys.modules); "
            "import sessiontools.context_parts.hooks, sessiontools.context_parts.tools; "
            "new = {m.split('.')[0] for m in set(sys.modules) - before}; "
            "print(sorted(m for m in new if m != 'sessiontools' "
            "and m not in sys.stdlib_module_names))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "[]"
