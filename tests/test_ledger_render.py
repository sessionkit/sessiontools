"""The ledger's four views (ST-2 design §5, slice do-002): long, short,
summary and json, rendered from `ledger.items()`.

Two halves. The first runs on the FAKE counter, on any host: layout, order,
marks, and the README's "No bare number" rule over every view. The second
is the README's gating Success Criteria AS IT WORDS THEM, at the VIEW level,
on the REAL counter (tiktoken cl100k_base in its uv child), so it carries
`requires_counter` and skips loudly where that cannot run. The item-level
twins of these gates are test_ledger_gates.py's.

The tables are read back by `table()`, a parser written here, apart from
`render`: columns are found where every line of the table is blank for two
or more characters, so a view that misaligns a figure fails to parse.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import statistics
from pathlib import Path

import pytest
from test_ledger_support import (
    FIXTURES,
    FakeCounter,
    materialise,
    oracle,
    real_counter,
    records,
    requires_counter,
)

from sessiontools import ledger
from sessiontools.ledger import count, render

_spec = importlib.util.spec_from_file_location("ledger_fixture_build_r", FIXTURES / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

FIXTURE_NAMES = ["nested-twice.jsonl", "policy-cat.jsonl", "policy-reads.jsonl",
                 "partial-read.jsonl", "partial-cover.jsonl", "partial-noeof.jsonl",
                 "boot.jsonl", "bash.jsonl", "grep.jsonl", "edge.jsonl", "budget-200.jsonl",
                 "edited.jsonl"]
POLICY = "POLICY-synthetic.md"
TITLE_ROWS = (render.LONG_COLUMNS, render.KIND_COLUMNS, render.TOP_COLUMNS,
              render.REPEAT_COLUMNS)
UNIT_WORDS = ("tokens", "lines", "line", "bytes", "items", "chars")


@pytest.fixture
def fx(tmp_path):
    return materialise(tmp_path)


class Fake(FakeCounter):
    """The words-plus-one fake, under a label with no digit in it, so the
    no-bare-number check reads only the view's own words."""

    label = "fake counter, words plus one"


def fake(path):
    return ledger.items(path, Fake())


def write(tmp_path, s, name="t.jsonl"):
    p = tmp_path / name
    p.write_text(s.jsonl(), encoding="utf-8")
    return p


# --- reading a rendered table back, apart from render --------------------------------

NAME_COLUMNS = {"item", "path or command", "path"}


def _parse(head: str, body: list[str], titles: tuple[str, ...]) -> list[dict[str, str]]:
    """Columns are where every line is blank for two or more characters,
    up to the last column's title; a row blank up to the last column
    continues the row above (a multi-line command)."""
    last_at = head.index(titles[-1])
    width = max(len(ln) for ln in [head, *body])
    grid = [ln.ljust(width) for ln in [head, *body]]
    blank = [all(g[c] == " " for g in grid) for c in range(last_at)]
    gaps, c = [], 0
    while c < last_at:
        if blank[c] and c + 1 < last_at and blank[c + 1]:
            start = c
            while c < last_at and blank[c]:
                c += 1
            gaps.append((start, c))
        else:
            c += 1
    spans = list(zip([0, *(e for _, e in gaps)], [*(b for b, _ in gaps), None], strict=True))
    assert spans[-1][0] == last_at, "the last column does not start at its title"
    assert [head[a:b].strip() for a, b in spans] == list(titles), "columns misaligned"
    rows: list[dict[str, str]] = []
    for ln in body:
        cells = [ln[a:b].strip() for a, b in spans]
        if rows and not any(cells[:-1]):
            rows[-1][titles[-1]] += "\n" + cells[-1]
        else:
            rows.append(dict(zip(titles, cells, strict=True)))
    return rows


def tables(text: str) -> list[tuple[tuple[str, ...], list[dict[str, str]], set[int]]]:
    """Every table in TEXT: (its titles, its rows, the line numbers it holds).
    A table is a title row and the lines under it up to a blank line."""
    lines = text.split("\n")
    out = []
    for i, ln in enumerate(lines):
        titles = tuple(re.split(r"\s{2,}", ln.strip()))
        if titles not in TITLE_ROWS:
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip():
            j += 1
        out.append((titles, _parse(ln, lines[i + 1:j], titles), set(range(i, j))))
    return out


def table(text: str, titles: tuple[str, ...]) -> list[dict[str, str]]:
    (rows,) = [rows for t, rows, _ in tables(text) if t == titles]
    return rows


def long_rows(items) -> list[dict[str, str]]:
    return table(render.long(items), render.LONG_COLUMNS)


# --- short ---------------------------------------------------------------------------

def test_short_names_the_unit_once_in_its_header_never_per_line(fx):
    out = render.short(fake(fx / "bash.jsonl")).splitlines()
    assert "tokens" in out[0].split() and Fake.label in out[0]
    assert all("tokens" not in ln for ln in out[1:])
    assert all(re.match(r"^(\d+|\?) \S", ln) for ln in out[1:])


def test_short_is_largest_first_ties_in_transcript_order_unknown_last(fx):
    its = fake(fx / "edge.jsonl")
    lines = render.short(its).splitlines()[1:]
    assert len(lines) == len(its)
    figures = [ln.split(" ", 1)[0] for ln in lines]
    known = [int(f) for f in figures if f != "?"]
    assert known == sorted(known, reverse=True)
    assert lines[-1] == "? shot.png"                  # the image Read: unknown, never 0
    tied = [it for it in its if it.read_tokens == known[0]]
    assert [ln.split(" ", 1)[1] for ln in lines[:len(tied)]] == [
        render._cut(it.label, 16) if it.kind not in render.FILE_KINDS else it.label
        for it in tied]


def test_short_shows_a_file_by_base_name_with_its_k_tag(fx):
    its = fake(fx / "boot.jsonl")
    want = sorted(((it.read_tokens, it.label) for it in its), key=lambda x: -x[0])
    assert render.short(its).splitlines()[1:] == [f"{n} {name}" for n, name in want]
    assert {name for _, name in want} == {"CLAUDE.md#1", "CLAUDE.md#2"}


def test_short_cuts_a_command_to_width_with_whitespace_collapsed(tmp_path):
    s = build.Session(70)
    s.bash("git   -C /a/very/long/path\n  log --oneline", "x\n")
    its = fake(write(tmp_path, s))
    assert render.short(its).splitlines()[1] == "2 git -C /a/very/l"
    assert render.short(its, width=5).splitlines()[1] == "2 git -"
    assert "cut to 5 chars" in render.short(its, width=5).splitlines()[0]


def test_short_cuts_another_tools_key_too(fx):
    lines = render.short(fake(fx / "edge.jsonl")).splitlines()
    assert any(ln.endswith(" Skill: demo:widg") for ln in lines)


# --- long ----------------------------------------------------------------------------

def test_long_is_one_row_per_item_in_transcript_order(fx):
    its = fake(fx / "budget-200.jsonl")
    rows = long_rows(its)
    assert [r["#"] for r in rows] == [f"#{it.seq}" for it in its]
    assert [r["came in"] for r in rows] == [it.kind for it in its]


def test_long_prints_the_full_path_and_the_full_command(fx):
    its = fake(fx / "budget-200.jsonl")
    for it, r in zip(its, long_rows(its), strict=True):
        if it.kind == "Bash":
            assert r["item"] == f"$ {it.key}"
        else:
            assert r["item"].split("  [")[0] == it.key


def test_long_prints_a_multi_line_command_as_continuation_lines(tmp_path):
    s = build.Session(71)
    s.bash("for f in a b; do\n  echo $f\ndone", "a\nb\n")
    s.bash("true", "")
    its = fake(write(tmp_path, s))
    rows = long_rows(its)
    assert len(rows) == 2
    assert rows[0]["item"] == "$ for f in a b; do\necho $f\ndone"
    out = render.long(its).splitlines()
    at = out.index(next(ln for ln in out if ln.endswith("$ for f in a b; do")))
    assert out[at + 1].strip() == "echo $f" and out[at + 1].endswith("    echo $f")


def test_long_marks_inexact_lines_disk_tokens_tags_and_unknowns(fx):
    (noeof,) = long_rows(fake(fx / "partial-noeof.jsonl"))
    assert noeof["file lines"] == "~41" and noeof["file tokens"].endswith("*")
    boot = long_rows(fake(fx / "boot.jsonl"))
    assert [r["item"].rsplit("  ", 1)[1] for r in boot] == ["[CLAUDE.md#1]", "[CLAUDE.md#2]"]
    image = next(r for r in long_rows(fake(fx / "edge.jsonl")) if r["item"].endswith("shot.png"))
    assert (image["read tokens"], image["read lines"], image["file tokens"]) == ("?", "?", "?")
    head = render.long([]).splitlines()
    assert all(mark in "\n".join(head[:3]) for mark in ("? unknown", "- not applicable",
                                                       "~ totalLines", "* counted on disk"))


def test_long_clamps_a_read_range_to_the_files_exact_lines_and_keeps_the_data_raw(fx):
    """R1 (foreman): edited.jsonl #1 reads all of a 10-line file, which
    Claude Code gives as numLines 11. The view shows 1-10; the JSON keeps 11,
    and the tokens read are the same either way."""
    its = fake(fx / "edited.jsonl")
    v1, _ = long_rows(its)
    assert (v1["read lines"], v1["file lines"]) == ("1-10", "10")
    rec = json.loads(render.jsonl(its).splitlines()[0])
    assert (rec["read_start_line"], rec["read_lines"], rec["file_lines"]) == (1, 11, 10)
    assert v1["read tokens"] == f"{rec['read_tokens']:,}"


def test_long_does_not_clamp_an_inexact_count_and_a_read_past_the_end_holds_none(tmp_path):
    s = build.Session(74)
    s.read("/gone/f.txt", "a\nb\nc\nd\n", offset=2, limit=2)   # no EOF evidence: ~5
    s.read("/gone/g.txt", "a\nb\n")                             # 1-3 as given: 2 lines
    s.read("/gone/g.txt", "a\nb\n", offset=3, limit=1)          # only the empty segment
    rows = long_rows(fake(write(tmp_path, s)))
    assert [(r["read lines"], r["file lines"]) for r in rows] == [
        ("2-3", "~5"), ("1-2", "2"), ("0", "2")]


def test_long_labels_a_re_read_after_edit_never_a_repeat(fx):
    """O1 (foreman): edited.jsonl #2 reads version 2 after #1 read
    version 1. It is a re-read after an edit, and no repeat in any view."""
    its = fake(fx / "edited.jsonl")
    _, v2 = long_rows(its)
    assert v2["note"] == "re-read after edit of #1"
    assert "repeat of" not in render.long(its)
    assert render.summary(its).rstrip().endswith(
        "repeats: the same lines entered the window again\n  none")


def test_long_gives_a_command_and_another_tool_no_file_figures(fx):
    for r in long_rows(fake(fx / "bash.jsonl")) + long_rows(fake(fx / "grep.jsonl")):
        assert (r["read lines"], r["file tokens"], r["file lines"]) == ("-", "-", "-")
    (grep,) = long_rows(fake(fx / "grep.jsonl"))
    assert grep["cmd tokens"] == grep["not read bytes"] == "-"


# --- summary -------------------------------------------------------------------------

def test_summary_counts_and_totals_by_kind_with_unknowns_apart(fx):
    its = fake(fx / "edge.jsonl")
    rows = {r["came in"]: r for r in table(render.summary(its), render.KIND_COLUMNS)}
    assert list(rows) == ["Read", "nested", "boot", "Bash", "other tools", "total"]
    for name, kinds in render.KIND_ROWS:
        mine = [it for it in its if it.kind in kinds]
        assert rows[name]["items"] == f"{len(mine):,}"
        assert rows[name]["read tokens"] == f"{sum(it.read_tokens or 0 for it in mine):,}"
    assert rows["total"]["items"] == f"{len(its):,}"
    assert (rows["Read"]["unknown items"], rows["total"]["unknown items"]) == ("1", "1")


def test_summary_lists_the_ten_largest_a_file_in_ranges_as_one_entry(tmp_path):
    s = build.Session(72)
    for k in range(12):
        s.bash(f"echo {k}", " ".join(["w"] * (k + 1)) + "\n")
    text = "".join(f"line {k} of the file\n" for k in range(30))
    for off in (1, 11, 21):
        s.read("/gone/big.txt", text, offset=off, limit=10)
    its = fake(write(tmp_path, s))
    top = table(render.summary(its), render.TOP_COLUMNS)
    assert len(top) == render.TOP_N == 10
    reads = [it for it in its if it.kind == "Read"]
    assert top[0] == {"read tokens": f"{sum(it.read_tokens for it in reads):,}", "items": "3",
                      "path or command": "/gone/big.txt"}
    figures = [int(r["read tokens"].replace(",", "")) for r in top]
    assert figures == sorted(figures, reverse=True)
    assert [r["path or command"] for r in top[1:]] == [f"echo {k}" for k in range(11, 2, -1)]


def test_summary_cuts_a_command_to_sixty_chars(tmp_path):
    s = build.Session(73)
    s.bash("echo " + "x" * 100, "y\n")
    (row,) = table(render.summary(fake(write(tmp_path, s))), render.TOP_COLUMNS)
    assert row["path or command"] == ("echo " + "x" * 100)[:render.SUMMARY_WIDTH]


def test_summary_says_none_when_nothing_repeats(fx):
    out = render.summary(fake(fx / "bash.jsonl"))
    assert out.rstrip().endswith("repeats: the same lines entered the window again\n  none")


def test_an_empty_session_renders_headers_only(tmp_path):
    for name, view in render.VIEWS.items():
        out = view([], session="abcdef0123456789")
        if name == "json":
            assert out == ""
        else:
            assert "ledger abcdef01" in out.splitlines()[0]


def test_the_views_and_the_default():
    assert list(render.VIEWS) == ["summary", "short", "long", "json"]
    assert render.DEFAULT_VIEW == "summary" and render.DEFAULT_WIDTH == 16


# --- no bare number, in any view (README Requirements) ---------------------------------

FIGURE = re.compile(r"^~?(\?|-|\d[\d,]*(-\d[\d,]*)?)\*?$")
ID = re.compile(r"^#\d+$")


def _no_bare_number_in_prose(line: str, sid: str) -> None:
    """A header or legend line: every number is followed by a unit word, or is a percent."""
    words = line.replace(sid, "").split()
    for i, w in enumerate(words):
        if re.fullmatch(r"[(\[]?\d[\d,]*[)\].,;:]?", w):
            nxt = words[i + 1] if i + 1 < len(words) else ""
            assert nxt.strip("(),.;:") in UNIT_WORDS, f"bare number {w!r} in {line!r}"


def _no_bare_number_in_table(rows: list[dict[str, str]], titles: tuple[str, ...]) -> None:
    for r in rows:
        for title, cell in r.items():
            if title in NAME_COLUMNS:
                continue                                  # a path or command: a name
            if title in ("#", "repeat of"):
                assert ID.match(cell) or cell in ("total", ""), (title, cell)
            elif title in ("came in", "note"):
                assert not re.search(r"(?<!#)\b\d", cell), (title, cell)
            else:
                assert any(u in title.split() for u in UNIT_WORDS), title
                assert cell == "" or FIGURE.match(cell), (title, cell)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_no_bare_number_in_any_view(fx, name):
    """README Requirements: every figure names its unit. In a table the unit
    is the column title; in a header or legend it is the next word."""
    its = fake(fx / name)
    sid = its[0].session[:8]
    for view in (render.long, render.summary):
        out = view(its)
        found = tables(out)
        assert found, view
        in_tables = set().union(*(at for _, _, at in found))
        for titles, rows, _ in found:
            _no_bare_number_in_table(rows, titles)
        for i, ln in enumerate(out.split("\n")):
            if i not in in_tables:
                _no_bare_number_in_prose(ln, sid)
    short = render.short(its).splitlines()
    _no_bare_number_in_prose(short[0], sid)
    assert "tokens" in short[0].split()
    for rec in (json.loads(ln) for ln in render.jsonl(its).splitlines()):
        for k, v in rec.items():
            if isinstance(v, int) and not isinstance(v, bool):
                assert k in ("seq", "repeat_of", "reread_of") or k.endswith(
                    ("_tokens", "_lines", "_line", "_bytes")), k


# --- json ----------------------------------------------------------------------------

def test_json_is_one_record_per_item_and_nothing_else(fx):
    its = fake(fx / "edge.jsonl")
    lines = render.jsonl(its).splitlines()
    assert [json.loads(ln) for ln in lines] == [it.record() for it in its]


# --- THE GATES, at the view level, on the real counter -----------------------------------

@pytest.fixture(scope="module")
def counter():
    return real_counter()


@requires_counter
def test_gate_short_shows_the_nested_twice_file_twice_with_each_cost(fx, counter):
    its = ledger.items(fx / "nested-twice.jsonl", counter)
    loaded = next(r for r in records(fx / "nested-twice.jsonl")
                  if r.get("type") == "attachment")["attachment"]["content"]["content"]
    (cost,) = counter.count([loaded])
    lines = render.short(its).splitlines()[1:]
    assert [ln for ln in lines if ln.endswith(" CLAUDE.md")] == [f"{cost} CLAUDE.md"] * 2


@requires_counter
def test_gate_summary_repeats_name_the_second_load_and_its_cost(fx, counter):
    its = ledger.items(fx / "nested-twice.jsonl", counter)
    loaded = next(r for r in records(fx / "nested-twice.jsonl")
                  if r.get("type") == "attachment")["attachment"]["content"]["content"]
    (cost,) = counter.count([loaded])
    first, second = [it for it in its if it.kind == "nested"]
    reps = table(render.summary(its), render.REPEAT_COLUMNS)
    assert reps[0] == {"#": f"#{second.seq}", "came in": "nested", "read tokens": f"{cost:,}",
                       "repeat of": f"#{first.seq}", "path": second.key}
    assert reps[-1]["#"] == "total" and reps[-1]["read tokens"] == f"{cost:,}"


@requires_counter
def test_gate_long_shows_policy_a_not_read_bytes_and_tokens(fx, counter):
    (row,) = long_rows(ledger.items(fx / "policy-cat.jsonl", counter))
    assert (row["not read bytes"], row["not read tokens"]) == ("120,991", "26,878")
    preview = next(r for r in records(fx / "policy-cat.jsonl")
                   if "toolUseResult" in r)["message"]["content"][0]["content"]
    assert row["read tokens"] == f"{counter.count([preview])[0]:,}"
    assert row["item"] == f"$ cat {fx / POLICY}"


@requires_counter
def test_gate_long_shows_unknown_not_read_tokens_with_the_file_gone(fx, counter):
    (fx / POLICY).unlink()
    (row,) = long_rows(ledger.items(fx / "policy-cat.jsonl", counter))
    assert (row["not read bytes"], row["not read tokens"]) == ("120,991", "?")
    assert "persisted file gone" in row["note"]


@requires_counter
def test_gate_policy_b_long_rows_and_the_summary_lists_it_first_as_one_entry(fx, counter):
    its = ledger.items(fx / "policy-reads.jsonl", counter)
    rows = long_rows(its)
    assert [(r["file lines"], r["file tokens"].rstrip("*")) for r in rows] == [
        ("2,526", "26,878")] * 3
    # R1: the last read's END is clamped to the file's 2,526 lines (numLines is 527)
    assert [r["read lines"] for r in rows] == ["1-1,000", "1,001-2,000", "2,001-2,526"]
    texts = [r["message"]["content"][0]["content"] for r in records(fx / "policy-reads.jsonl")
             if "toolUseResult" in r]
    counts = counter.count(texts)
    assert [r["read tokens"] for r in rows] == [f"{n:,}" for n in counts]
    out = render.summary(its)
    first = table(out, render.TOP_COLUMNS)[0]
    assert first == {"read tokens": f"{sum(counts):,}", "items": "3",
                     "path or command": str(fx / POLICY)}
    assert sum(counts) > 26_878
    assert "repeats: the same lines entered the window again\n  none\n" in out


@requires_counter
def test_gate_long_shows_the_partial_read_with_its_figures(fx, counter):
    first, _ = long_rows(ledger.items(fx / "partial-read.jsonl", counter))
    shown = next(r for r in records(fx / "partial-read.jsonl")
                 if "toolUseResult" in r)["message"]["content"][0]["content"]
    whole = (fx / "partial-40.txt").read_text()
    prefixed, file_tokens = counter.count([shown, whole])
    assert first["read lines"] == "11-20"                       # the limit (10), from the offset
    assert first["file lines"] == "40"
    assert first["read tokens"] == f"{prefixed:,}"
    assert first["file tokens"] == f"{file_tokens:,}*"          # counted on disk now
    assert prefixed < file_tokens


@requires_counter
def test_gate_long_shows_the_two_boot_rows_with_their_figures(fx, counter):
    rows = long_rows(ledger.items(fx / "boot.jsonl", counter))
    files = next(r for r in records(fx / "boot.jsonl")
                 if r.get("type") == "attachment")["attachment"]["files"]
    counts = counter.count([f["content"] for f in files])
    assert [r["came in"] for r in rows] == ["boot", "boot"]
    assert [r["item"].split("  [")[0] for r in rows] == [f["path"] for f in files]
    assert [r["read tokens"] for r in rows] == [f"{n:,}" for n in counts]
    assert [r["file tokens"] for r in rows] == [f"{n:,}" for n in counts]


@requires_counter
def test_gate_long_shows_bash_output_apart_from_the_command(fx, counter):
    seq, true, cat = long_rows(ledger.items(fx / "bash.jsonl", counter))
    assert int(seq["read tokens"].replace(",", "")) > 1000 * int(seq["cmd tokens"])
    assert (true["read tokens"], true["note"]) == ("7", "no output")
    assert (cat["not read bytes"], cat["not read tokens"]) == ("120,991", "26,878")


@requires_counter
def test_gate_other_tools_one_row_in_long_and_json_keyed_by_tool_and_input(fx, counter):
    its = ledger.items(fx / "grep.jsonl", counter)
    (row,) = long_rows(its)
    (rec,) = [json.loads(ln) for ln in render.jsonl(its).splitlines()]
    assert row["came in"] == rec["kind"] == "tool"
    assert row["item"] == rec["key"] == "Grep: def scan"
    assert row["read tokens"] == f"{rec['read_tokens']:,}"


# The short report's budget. budget-200's paths and commands are at least as
# long, on average, as design evidence budget.py's real sample of 133 paths
# and 67 commands from the 60 latest transcripts on one Mac. Re-measured twice by
# do-002 on 2030-12-08 (the sample moves as transcripts land):
# base names 10.02 / 10.13 chars, paths 116.4 / 117.2, commands 451.9 /
# 430.5 (whitespace collapsed). The floors below are the larger of each.
SAMPLE_BASE, SAMPLE_PATH, SAMPLE_CMD = 10.13, 117.2, 451.9
#: Measured by do-002 (2030-12-08, one Mac) at width 16: 1,564 tokens
#: for 200 items, so 21.8% headroom under 2,000 (design §5 measured 1,789 on
#: its sample, 10.5%). Widths 20 and 24 gave 1,674 and 1,784. Re-measured by
#: ST-144 fix-001 (2030-12-08, same host) after the fake store root
#: replaced the real one, same length: 1,542 tokens, 22.9% headroom.
#: THE FIXTURE'S HEADROOM IS NOT THE REAL ONE. The real sample costs more per
#: item: design evidence budget.py, re-run by the do-002 review on
#: 2030-12-08 (the same Mac), gave 1,718 tokens at width 16 with `#k` tags, 14.1%
#: headroom, against this fixture's 1,564 (do-002 review R2). The gate holds
#: on both; read 14.1% as the margin, 22.9% as this fixture's alone.
BUDGET, MEASURED = 2_000, 1_542


def test_the_budget_fixture_is_at_least_as_long_as_the_real_sample(fx):
    its = fake(fx / "budget-200.jsonl")
    files = [it.key for it in its if it.kind in render.FILE_KINDS]
    cmds = [" ".join(it.key.split()) for it in its if it.kind == "Bash"]
    assert len(its) == 200 and len(files) >= 133 and len(cmds) >= 66
    assert statistics.mean(len(os.path.basename(p)) for p in files) >= SAMPLE_BASE
    assert statistics.mean(len(p) for p in files) >= SAMPLE_PATH
    assert statistics.mean(len(c) for c in cmds) >= SAMPLE_CMD
    assert min(len(c) for c in cmds) > render.DEFAULT_WIDTH        # every command is cut
    assert len({it.label for it in its if "#" in it.label}) > 100    # colliding base names


@requires_counter
def test_gate_short_report_of_200_items_is_under_2000_tokens(fx, counter):
    its = ledger.items(fx / "budget-200.jsonl", counter)
    out = render.short(its)
    (cost,) = counter.count([out])
    assert len(out.splitlines()) == 201
    assert cost < BUDGET, f"{cost} tokens"
    assert abs(cost - MEASURED) <= 50, f"{cost} tokens: re-measure and record the headroom"


@requires_counter
def test_gate_collectable_json_concatenates_with_cat(fx, counter, tmp_path):
    a = render.jsonl(ledger.items(fx / "nested-twice.jsonl", counter))
    b = render.jsonl(ledger.items(fx / "bash.jsonl", counter))
    (tmp_path / "a.jsonl").write_text(a)
    (tmp_path / "b.jsonl").write_text(b)
    corpus = (tmp_path / "a.jsonl").read_text() + (tmp_path / "b.jsonl").read_text()  # cat
    recs = [json.loads(ln) for ln in corpus.splitlines()]
    assert len(recs) == len(a.splitlines()) + len(b.splitlines()) == 6
    assert {r["session"] for r in recs} == {"00000000-0000-4000-8000-000000000001",
                                            "00000000-0000-4000-8000-000000000008"}
    assert all(r["counter"] == count.LABEL for r in recs)


@requires_counter
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_gate_every_token_figure_in_json_is_the_counter_over_its_source(fx, counter, name):
    """The README's no-chars/N gate in its own words: every token figure in
    `--json` equals the counter re-run over its source text."""
    recs = [json.loads(ln) for ln in render.jsonl(ledger.items(fx / name, counter)).splitlines()]
    srcs = oracle(fx / name)
    assert len(recs) == len(srcs)
    disk = {r["key"]: Path(r["key"]).read_text(encoding="utf-8") for r in recs
            if r["file_tokens_from"] == "disk"}
    saved = {s["persisted"]: Path(s["persisted"]).read_text(encoding="utf-8") for s in srcs
             if "persisted" in s and os.path.exists(s["persisted"])}
    pool = sorted({t for s in srcs for t in (s["read"] or [])}
                  | {s[k] for s in srcs for k in ("cmd", "full") if k in s}
                  | set(disk.values()) | set(saved.values()))
    n = dict(zip(pool, counter.count(pool), strict=True))       # one child for the lot
    fulls = {n[s["full"]] for s in srcs if "full" in s}
    for rec, src in zip(recs, srcs, strict=True):
        assert rec["counter"] == count.LABEL
        assert rec["read_tokens"] == (None if src["read"] is None
                                      else sum(n[t] for t in src["read"])), rec
        assert rec["cmd_tokens"] == (n[src["cmd"]] if "cmd" in src else None)
        if rec["file_tokens_from"] == "disk":
            assert rec["file_tokens"] == n[disk[rec["key"]]]
        elif rec["file_tokens_from"] == "transcript":
            assert rec["file_tokens"] in fulls
        else:
            assert rec["file_tokens"] is None
        persisted = src.get("persisted")
        assert rec["persisted_tokens"] == (n[saved[persisted]] if persisted in saved else None)


# --- version-consistent rows (do-001 review N1; foreman) --------------------------------

@requires_counter
def test_gate_an_edited_file_never_shows_two_versions_in_one_row(fx, counter):
    """Row 1 is the full read of version 1; row 2 a partial read of version 2,
    whose file figures both describe version 2 (from the disk, which holds it)."""
    old = next(r for r in records(fx / "edited.jsonl")
               if "toolUseResult" in r)["toolUseResult"]["file"]["content"]
    new = (fx / "edited.txt").read_text()
    old_tokens, new_tokens = counter.count([old, new])
    v1, v2 = long_rows(ledger.items(fx / "edited.jsonl", counter))
    assert (v1["file lines"], v1["file tokens"]) == ("10", f"{old_tokens:,}")
    assert (v2["file lines"], v2["file tokens"]) == ("~15", f"{new_tokens:,}*")
    assert old_tokens != new_tokens


@requires_counter
def test_gate_a_version_mismatch_renders_unknown(fx, counter):
    old = next(r for r in records(fx / "edited.jsonl")
               if "toolUseResult" in r)["toolUseResult"]["file"]["content"]
    (fx / "edited.txt").write_text(old + "\n")            # the disk is version 1 again
    _, v2 = long_rows(ledger.items(fx / "edited.jsonl", counter))
    assert (v2["file lines"], v2["file tokens"]) == ("~15", "?")
    assert "file changed" in v2["note"]
