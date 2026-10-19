"""The views as text and JSON (ST-26 do-004, design sections 1 and 8): no
bare number in any view, `~` / `"fitted": true` on fitted rows only, `?` for
unknown, and the warning lines.

Every view is rendered over every context fixture (context_parts,
context_boot, context_hooks) and over an empty transcript."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools.context_parts import boot as B
from sessiontools.context_parts import hooks as H
from sessiontools.context_parts import render as R
from sessiontools.context_parts import tools as T
from sessiontools.context_parts.breakdown import (
    OWN_OUTPUT,
    POOR_FIT_WARNING,
    TOO_FEW_WARNING,
    parts,
)
from sessiontools.context_parts.transcript import HOOK, LABELS, OTHER, RESULT

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
EVERY = sorted(FIXTURES.glob("context_*/*.jsonl"))
DATA = "<data>"


# ---------- the no-bare-number check ------------------------------------------
UNITS = ("tok/char", "tokens", "tok", "chars", "calls", "call", "results", "result",
         "lines", "line", "batches", "batch", "entries", "entry", "copies", "copy",
         "counted intervals", "counted interval", "hook commands", "hook command",
         "tool batches", "tool batch")
#: A figure WITH its unit or label. Anything else holding a digit is bare.
FIGURE = re.compile(
    r"~?-?\d[\d,]*(?:\.\d+)?[KM]? (?:" + "|".join(re.escape(u) for u in UNITS) + r")\b"
    r"|-?\d+(?:\.\d+)?% of \w"         # a percentage says what it is of
    r"|\bcall \d+"                     # a call is named as one
    r"|\bat least \d+ batches")        # the legend's steady rule


def bare_numbers(text: str) -> list[str]:
    """Every line of TEXT that still holds a digit once its labelled figures
    are taken out."""
    return [line for line in text.splitlines() if re.search(r"\d", FIGURE.sub("", line))]


@pytest.mark.parametrize("line", [
    "  AT BOOT   33.3K",                 # no unit
    "Unattributed 6.5K tok (3.6%)",      # a percentage of nothing
    "settled at 4",                      # a call not named as one
    "  repeats 414",
])
def test_the_check_catches_a_bare_figure(line):
    assert bare_numbers(line) == [line]


@pytest.mark.parametrize("line", [
    "  AT BOOT   33.3K tok   exact", "~0 tok", "2,977 chars", "21.0% of the window",
    "read by call 12", "0.385 tok/char", "24 copies in 24 batches", "? tok", "?% of since boot",
])
def test_the_check_passes_a_labelled_figure(line):
    assert bare_numbers(line) == []


# ---------- every view ------------------------------------------------------------
def _views(path: Path) -> dict[str, str]:
    p = parts(path)
    return {
        "parts": R.parts_text(p, session="s1", window=1_000_000),
        "parts-unknown-window": R.parts_text(p),
        "parts+startup+tools": R.parts_text(p, session="s1", window=1_000_000,
                                            startup=B.startup(path), tools=T.tools(path)),
        "startup": R.startup_text(B.startup(path), session="s1"),
        "boot": R.boot_text(B.boot(path), session="s1"),
        "hooks": R.hooks_text(H.hooks(path)),
        "hooks+parts": R.hooks_text(H.hooks(path), session="s1", parts=p),
    }


@pytest.fixture
def blank_data(monkeypatch):
    """Data (keys, commands, model ids, module strings) is not a figure:
    blank it through the one seam every view uses."""
    monkeypatch.setattr(R, "_data", lambda text: DATA)


@pytest.mark.parametrize("path", EVERY, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_view_prints_a_bare_number(path, blank_data):
    for name, text in _views(path).items():
        assert bare_numbers(text) == [], f"{name} on {path.name}"


def test_no_view_prints_a_bare_number_on_an_empty_transcript(tmp_path, blank_data):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    for name, text in _views(empty).items():
        assert bare_numbers(text) == [], name


def test_the_views_go_through_the_data_seam(blank_data):
    """The seam is really used: with it blanked, the keys are gone."""
    text = _views(FIXTURES / "context_hooks" / "tools.jsonl")["parts+startup+tools"]
    assert "/w/big.md" not in text and DATA in text


def test_the_views_print_their_data():
    v = _views(FIXTURES / "context_hooks" / "tools.jsonl")
    assert "Read /w/big.md" in v["parts+startup+tools"]
    assert "claude-opus-5-5" in v["parts"]
    hooks = R.hooks_text(H.hooks(FIXTURES / "context_hooks" / "hooks.jsonl"))
    assert "pager-hook" in hooks and "pager is listening, session box live" in hooks


def test_every_json_view_is_json():
    for path in EVERY:
        p = parts(path)
        for doc in (R.parts_json(p, startup=B.startup(path), tools=T.tools(path)),
                    R.startup_json(B.startup(path)), R.boot_json(B.boot(path)),
                    R.hooks_json(H.hooks(path), parts=p)):
            assert json.loads(json.dumps(doc)) == doc


# ---------- fitted vs exact ------------------------------------------------------------
def _line_of(text: str, label: str) -> str:
    hits = [ln for ln in text.splitlines() if ln[:1] == " " and ln.strip().startswith(label)]
    assert len(hits) == 1, (label, hits)
    return hits[0]


def _figure(line: str) -> str:
    m = re.search(r"(~?-?[\d.,]+[KM]?|\?) tok\b", line)
    assert m, line
    return m.group(1)


@pytest.mark.parametrize("name", ["synthetic.jsonl", "young.jsonl", "poor.jsonl"])
def test_fitted_rows_carry_the_mark_and_exact_rows_do_not(name):
    path = FIXTURES / "context_parts" / name
    p = parts(path)
    text = R.parts_text(p)
    for r in p.rows:
        if r.rate is None and r.fitted:
            continue                                     # never entered: not printed
        fig = _figure(_line_of(text, r.label))
        assert fig.startswith("~") == r.fitted, (r.label, fig)
    assert not _figure(_line_of(text, "AT BOOT")).startswith("~")
    assert not _figure(_line_of(text, "SINCE BOOT")).startswith("~")


def test_the_tools_sub_rows_carry_the_mark():
    """do-004 review F1: every `--tools` sub-row and every largest-result row
    is a fitted figure, so each prints `~` (render.py `_tools_lines`)."""
    path = FIXTURES / "context_hooks" / "tools.jsonl"
    t = T.tools(path)
    lines = R.parts_text(parts(path), tools=t).splitlines()
    at = next(i for i, ln in enumerate(lines) if ln.strip().startswith(LABELS[RESULT]))
    block = lines[at + 1: at + 1 + len(t.rows)]
    largest = [ln for ln in lines if "read by call" in ln]
    assert len(block) == len(t.rows) > 0 and len(largest) == len(t.largest) > 0
    for ln in block + largest:
        assert _figure(ln).startswith("~"), ln


def test_the_boot_reads_carry_the_mark_and_the_exact_rows_do_not():
    """do-004 review F1: `--boot`'s counted read rows print `~`; AT STARTUP,
    ON DEMAND, own output and Unattributed are exact or residual, unmarked."""
    b = B.boot(FIXTURES / "context_boot" / "agent_mail.jsonl")
    text = R.boot_text(b)
    counted = [r for r in R.boot_json(b)["rows"]
               if r["section"] == "on_demand" and r["fitted"]]
    assert counted
    on_demand = text.split("ON DEMAND", 1)[1]
    for r in counted:
        assert _figure(_line_of(on_demand, r["label"])).startswith("~"), r["label"]
    for label in ("AT STARTUP", "ON DEMAND", "Own output", "Unattributed"):
        assert not _figure(_line_of(text, label)).startswith("~"), label


@pytest.mark.parametrize("path", EVERY, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_json_rows_say_fitted_exactly_where_the_data_does(path):
    p = parts(path)
    docs = [R.parts_json(p, startup=B.startup(path), tools=T.tools(path)),
            R.startup_json(B.startup(path)), R.boot_json(B.boot(path)),
            R.hooks_json(H.hooks(path), parts=p)]
    for doc in docs:
        rows = list(doc["rows"])
        if doc.get("tools"):
            rows += doc["tools"]["rows"] + doc["tools"]["largest"]
        for row in rows:
            assert not (row["exact"] and row["fitted"]), row
            assert row["fitted"] == (row["basis"] in ("fitted", "fallback", "counted",
                                                      "snapshot")), row
            assert row["exact"] == (row["basis"] == "exact" and (
                row["tokens"] is not None or row.get("unit") == "chars")), row
    by = {r["category"]: r for r in docs[0]["rows"] if r.get("section") == "since_boot"}
    assert by[OWN_OUTPUT]["exact"] and not by[OWN_OUTPUT]["fitted"]


def test_the_jq_example_selects_only_fitted_rows():
    """design section 1: `.rows[] | select(.fitted)`."""
    path = FIXTURES / "context_parts" / "synthetic.jsonl"
    doc = R.parts_json(parts(path), startup=B.startup(path))
    fitted = {r["category"] for r in doc["rows"] if r["fitted"]}
    assert RESULT in fitted and HOOK in fitted
    assert not fitted & {"boot", "since_boot", OWN_OUTPUT, "unattributed", "core"}


def test_boot_rows_mark_counted_and_snapshot_not_the_remainder():
    s = B.startup(FIXTURES / "context_boot" / "human.jsonl")
    text = R.startup_text(s)
    assert not _figure(_line_of(text, s.core.label)).startswith("~")
    for r in s.core_split:
        assert _figure(_line_of(text, r.label)).startswith("~") == r.fitted
    for r in s.rows:
        assert _figure(_line_of(text, r.label)).startswith("~")
    assert "from /context at" in text


# ---------- unknown is ? ---------------------------------------------------------------
def test_an_unknown_window_is_a_question_mark():
    p = parts(FIXTURES / "context_parts" / "synthetic.jsonl")
    assert "of a window of ? tok" in R.parts_text(p)
    doc = R.parts_json(p)
    assert (doc["window"], doc["pct"], doc["free"]) == (None, None, None)


def test_a_known_window_has_its_share_and_free():
    p = parts(FIXTURES / "context_parts" / "synthetic.jsonl")
    used = p.anchors.used
    doc = R.parts_json(p, window=1_000_000)
    assert (doc["used"], doc["window"], doc["pct"], doc["free"]) == (
        used, 1_000_000, used * 100 // 1_000_000, 1_000_000 - used)


def test_an_empty_transcript_is_unknown_everywhere(tmp_path):
    empty = tmp_path / "e.jsonl"
    empty.write_text("", encoding="utf-8")
    assert "? tok used" in R.parts_text(parts(empty))
    assert R.startup_text(B.startup(empty)).startswith("Startup: ? tok")
    assert "? tok: no call yet" in R.boot_text(B.boot(empty))
    doc = R.parts_json(parts(empty))
    assert doc["used"] is None and doc["rows"][0]["tokens"] is None
    assert R.startup_json(B.startup(empty))["rows"][0] == {
        "category": "startup", "label": "Startup (total)", "tokens": None, "basis": "exact",
        "exact": False, "fitted": False, "section": "total"}


def test_no_growth_makes_the_unattributed_share_unknown(tmp_path):
    one = tmp_path / "one.jsonl"
    one.write_text(json.dumps({"type": "assistant", "message": {
        "id": "a", "model": "claude-opus-5-5", "content": [],
        "usage": {"input_tokens": 100, "output_tokens": 1}}}) + "\n", encoding="utf-8")
    p = parts(one)
    assert p.unattributed_share is None
    assert "what the fit misses: ?% of since boot" in R.parts_text(p)
    assert R.parts_json(p)["unattributed_share"] is None


def test_a_category_that_never_entered_is_absent_not_zero():
    path = FIXTURES / "context_hooks" / "tools.jsonl"
    p = parts(path)
    unseen = [r for r in p.rows if r.rate is None and r.fitted]
    assert unseen, "the tools fixture has categories that never entered"
    text = R.parts_text(p)
    doc = {r["category"]: r for r in R.parts_json(p)["rows"]}
    for r in unseen:
        assert r.label not in text
        assert doc[r.category]["tokens"] is None and doc[r.category]["seen"] is False


def test_a_category_fitted_at_zero_is_a_real_zero():
    p = parts(FIXTURES / "context_parts" / "synthetic.jsonl")
    rows = tuple(dataclasses.replace(r, rate=0.0, tokens=0.0) if r.category == OTHER else r
                 for r in p.rows)
    p0 = dataclasses.replace(p, rows=rows)
    assert _figure(_line_of(R.parts_text(p0), LABELS[OTHER])) == "~0"
    row = next(r for r in R.parts_json(p0)["rows"] if r["category"] == OTHER)
    assert (row["tokens"], row["rate"], row["fitted"]) == (0.0, 0.0, True)


@pytest.mark.parametrize("n, out", [
    (None, "?"), (0, "0"), (812, "812"), (999.4, "999"), (999.6, "1.0K"), (33_300, "33.3K"),
    (214_000, "214K"), (999_400, "999K"), (1_000_000, "1.00M"), (-6_500, "-6.5K"), (-0.2, "0"),
])
def test_num_is_compact_and_unknown_is_a_question_mark(n, out):
    assert R.num(n) == out


# ---------- warnings and labels -----------------------------------------------------------
def test_the_poor_fit_warning_line_prints_when_flagged():
    p = parts(FIXTURES / "context_parts" / "poor.jsonl")
    assert p.poor_fit
    assert f"warning: {POOR_FIT_WARNING}" in R.parts_text(p).splitlines()
    assert R.parts_json(p)["poor_fit"] is True


def test_no_warning_line_on_a_good_fit():
    p = parts(FIXTURES / "context_parts" / "synthetic.jsonl")
    assert not p.warnings
    assert "warning:" not in R.parts_text(p)


def test_the_young_seat_warning_and_its_rows_say_fallback():
    p = parts(FIXTURES / "context_parts" / "young.jsonl")
    text = R.parts_text(p)
    assert f"warning: {TOO_FEW_WARNING}" in text.splitlines()
    assert "fallback rate, not fitted" in text


def test_own_output_says_which_output_it_is():
    p = parts(FIXTURES / "context_parts" / "compaction.jsonl")
    line = _line_of(R.parts_text(p), "Own output")
    assert "inside the counted intervals" in line
    assert f"every call's output: {R.tok(p.anchors.output_total)}" in line
    assert R.parts_json(p)["own_output_all_calls"] == p.anchors.output_total


def test_a_compaction_is_a_line_not_a_silent_subtraction():
    p = parts(FIXTURES / "context_parts" / "compaction.jsonl")
    c = p.anchors.compactions[0]
    text = R.parts_text(p)
    assert (f"compaction at call {c.call}: {R.tok(c.before)} before, {R.tok(c.after)} after; "
            f"{R.tok(c.dropped)} dropped, not subtracted from since boot") in text
    assert R.parts_json(p)["compactions"] == [
        {"call": c.call, "before": c.before, "after": c.after, "dropped": c.dropped}]


def test_the_legend_explains_the_mark_and_unattributed():
    text = R.parts_text(parts(FIXTURES / "context_parts" / "synthetic.jsonl"))
    assert R.LEGEND_FITTED in text and R.LEGEND_UNITS in text
    assert "image" in R.LEGEND_UNATTRIBUTED and R.LEGEND_UNATTRIBUTED in text


# ---------- --tools in --parts --------------------------------------------------------------
def test_parts_with_tools_prints_each_tool_under_tool_results():
    path = FIXTURES / "context_hooks" / "tools.jsonl"
    t = T.tools(path)
    lines = R.parts_text(parts(path), tools=t).splitlines()
    at = next(i for i, ln in enumerate(lines) if ln.strip().startswith(LABELS[RESULT]))
    block = lines[at + 1: at + 1 + len(t.rows)]
    assert [ln.split()[0] for ln in block] == [r.tool for r in t.rows]
    largest = [ln for ln in lines if "read by call" in ln]
    assert len(largest) == len(t.largest) == 5
    assert "Read /w/big.md" in largest[0] and "9,000 chars, read by call 1" in largest[0]
    doc = R.parts_json(parts(path), tools=t)["tools"]
    assert sum(r["chars"] for r in doc["rows"]) == doc["row"]["chars"]
    assert [x["label"] for x in doc["largest"]] == [x.key for x in t.largest]


# ---------- --hooks ---------------------------------------------------------------------------
def test_hooks_prints_exact_chars_and_each_command():
    h = H.hooks(FIXTURES / "context_hooks" / "hooks.jsonl")
    text = R.hooks_text(h)
    assert text.splitlines()[0].startswith(f"Hook context: {h.total_chars:,} chars from 8 hook")
    for r in h.rows:
        assert f"  {r.command}  (" in text


def test_hooks_with_parts_labels_the_gap_to_the_fitted_row():
    """The do-003 review's F2: the hooks total spans every call; the fitted
    row only the counted intervals. Boot chars and a compaction's chars are
    printed as the difference, so the two figures reconcile."""
    path = FIXTURES / "context_hooks" / "hooks.jsonl"
    h, p = H.hooks(path), parts(path)
    doc = R.hooks_json(h, parts=p)
    row = next(r for r in p.rows if r.category == HOOK)
    assert doc["boot_chars"] == 400
    assert doc["boot_chars"] + doc["since_boot_chars"] == h.total_chars
    assert doc["since_boot_chars"] - doc["not_in_parts_row_chars"] == row.chars
    text = R.hooks_text(h, parts=p)
    assert "at boot (call 0): 400 chars; since boot: 2,577 chars" in text
    assert f"the --parts Hook context row: {R.tok(row.tokens, True)} from {row.chars:,} chars" \
        in text


def test_hooks_gap_names_a_compactions_hook_lines(tmp_path):
    build = _load_build()
    recs = []
    ctx = [10_000, 11_000, 3_000, 4_000]               # the third call follows a compaction
    for k, c in enumerate(ctx):
        if k:
            recs += build.hook(f"h{k}", "clock", [f"[clock] 13:0{k}"])
        recs.append(build.assistant(f"c{k}", c, 10))
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(build.dump(r) for r in recs) + "\n", encoding="utf-8")
    doc = R.hooks_json(H.hooks(path), parts=parts(path))
    assert doc["not_in_parts_row_chars"] == len("[clock] 13:02")
    assert ("13 chars entered in a compaction's interval: here, not in that row"
            in R.hooks_text(H.hooks(path), parts=parts(path)))


def test_a_long_hook_line_is_cut_in_text_and_whole_in_json():
    h = H.hooks(FIXTURES / "context_hooks" / "hooks.jsonl")
    start = next(r for r in h.rows if r.command == "session-start")
    text = R.hooks_text(h)
    assert "w" * 400 not in text
    assert f"\"{'w' * (R.EXAMPLE_MAX - 3)}...\"" in text
    doc = next(r for r in R.hooks_json(h)["rows"] if r["command"] == "session-start")
    assert doc["top"][0]["example"] == start.top[0].example == "w" * 400


@pytest.mark.parametrize("text, limit, out", [
    ("short", 10, "short"), ("a\nb\t c", 10, "a b c"), ("x" * 12, 10, "xxxxxxx..."),
])
def test_cut_makes_one_short_line(text, limit, out):
    assert R.cut(text, limit) == out


# ---------- the boot views ----------------------------------------------------------------------
def test_boot_names_its_signal_and_settled_call():
    b = B.boot(FIXTURES / "context_boot" / "agent_mail.jsonl")
    text = R.boot_text(b)
    assert f"the window of call {b.settled_call} of {b.calls} calls" in text
    assert b.signal_text in text and b.signal_detail in text
    doc = R.boot_json(b)
    assert (doc["signal"], doc["settled_call"], doc["total"]) == (b.signal, b.settled_call,
                                                                   b.total)


def test_boot_held_for_the_brief_says_so():
    b = B.boot(FIXTURES / "context_boot" / "agent_held.jsonl")
    assert b.held_for_brief
    assert f"held to call {b.settled_call}" in R.boot_text(b)


def test_boot_reads_sum_to_their_rows():
    b = B.boot(FIXTURES / "context_boot" / "agent_mail.jsonl")
    doc = R.boot_json(b)
    counted = [r for r in doc["rows"] if r["section"] == "on_demand" and r["fitted"]]
    assert sum(r["tokens"] for r in counted) == b.reads_tokens
    unatt = next(r for r in doc["rows"] if r["category"] == "unattributed")
    assert unatt["tokens"] == b.unattributed


def test_render_and_band_import_only_the_standard_library():
    code = ("import sys; before = set(sys.modules); "
            "import sessiontools.context_parts.render, sessiontools.band; "
            "new = {m.split('.')[0] for m in set(sys.modules) - before}; "
            "print(sorted(m for m in new if m != 'sessiontools' "
            "and m not in sys.stdlib_module_names))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "[]"


def _load_build():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "context_hooks_fixture_build_for_render", FIXTURES / "context_hooks" / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("share, out", [
    (None, "?% of x"), (0.0, "0.0% of x"), (-0.00001, "0.0% of x"), (0.036, "3.6% of x"),
    (-0.12, "-12.0% of x"),
])
def test_pct_says_what_it_is_of_and_never_prints_a_negative_zero(share, out):
    assert R.pct(share, "x") == out


def test_the_figure_column_holds_for_long_keys_and_every_label():
    """A long tool key is cut to the label column; no category label is."""
    path = FIXTURES / "context_hooks" / "tools.jsonl"
    long = R._line("Bash " + "x" * 200, "~1.0K tok", "note", 8)
    short = R._line("Read", "~1.0K tok", "note", 8)
    assert long.index("~1.0K tok") == short.index("~1.0K tok")
    for label in LABELS.values():
        assert R._line(label, "1 tok", "", 4).lstrip().startswith(label)
    lines = R.parts_text(parts(path), tools=T.tools(path)).splitlines()
    ends = {ln.index(" tok") for ln in lines if ln.startswith("    ") and " tok   " in ln}
    assert len(ends) == 1
