"""ST-2's gating Success Criteria, at the ITEM level (the fields of
`ledger.items()`), on the REAL counter: tiktoken cl100k_base in its uv child.
The views over these items, and the same gates' view-level wording, are
do-002's (render.py).

Every expected figure here is the counter re-run over a text taken from the
RAW records themselves (`test_ledger_support.oracle()`), never from the reader, so a
reader that counted the wrong text, or estimated chars ÷ N, fails. "Equals
`tokens` over X" is read as OQ3 was ruled:
`tokens`' tiktoken backend over the raw text that entered the window.

Every test needs the real counter, so all of them carry `requires_counter`:
on a host without uv or a warm vocabulary they SKIP with a loud reason, and
verify-001 asserts zero skips on the build Mac.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path

import pytest
from test_ledger_support import (
    materialise,
    oracle,
    real_counter,
    records,
    requires_counter,
)

from sessiontools import ledger
from sessiontools.ledger import count

pytestmark = requires_counter

POLICY = "POLICY-synthetic.md"
FIXTURE_NAMES = ["nested-twice.jsonl", "policy-cat.jsonl", "policy-reads.jsonl",
                 "partial-read.jsonl", "partial-cover.jsonl", "partial-noeof.jsonl",
                 "boot.jsonl", "bash.jsonl", "grep.jsonl", "edge.jsonl", "budget-200.jsonl",
                 "edited.jsonl"]


@pytest.fixture(scope="module")
def counter():
    return real_counter()


@pytest.fixture
def fx(tmp_path):
    return materialise(tmp_path)


def tok(counter, *texts: str) -> list[int]:
    return counter.count(list(texts))


# --- the nested load counted twice (stands in for 03c563c5) ---------------------------

def test_gate_nested_load_twice(fx, counter):
    its = [it for it in ledger.items(fx / "nested-twice.jsonl", counter) if it.kind == "nested"]
    loaded = next(r for r in records(fx / "nested-twice.jsonl")
                  if r.get("type") == "attachment")["attachment"]["content"]["content"]
    (want,) = tok(counter, loaded)
    assert len(its) == 2 and its[0].key == its[1].key
    assert its[1].repeat_of == its[0].seq and its[0].repeat_of is None
    assert its[0].read_tokens == its[1].read_tokens == want


# --- POLICY.md, 26,878 tokens ------------------------------------------------------

def test_gate_policy_a_persisted_cat(fx, counter):
    (it,) = ledger.items(fx / "policy-cat.jsonl", counter)
    rec = next(r for r in records(fx / "policy-cat.jsonl") if "toolUseResult" in r)
    preview, stdout = rec["message"]["content"][0]["content"], rec["toolUseResult"]["stdout"]
    assert len(stdout.encode()) <= 30_000
    assert it.kind == "Bash" and it.read_tokens == tok(counter, preview)[0]
    assert (it.persisted_bytes, it.persisted_tokens) == (120_991, 26_878)
    assert it.persisted_tokens != tok(counter, stdout)[0]          # never the capped stdout


def test_gate_policy_a_with_the_file_gone_the_not_read_tokens_are_unknown(fx, counter):
    (fx / POLICY).unlink()
    (it,) = ledger.items(fx / "policy-cat.jsonl", counter)
    assert it.persisted_bytes == 120_991 and it.persisted_tokens is None
    assert "persisted file gone" in it.note


def test_gate_policy_b_reads_covering_every_line(fx, counter):
    its = ledger.items(fx / "policy-reads.jsonl", counter)
    texts = [r["message"]["content"][0]["content"] for r in records(fx / "policy-reads.jsonl")
             if "toolUseResult" in r]
    assert len(its) == 3
    assert [it.read_tokens for it in its] == tok(counter, *texts)
    for it in its:
        assert (it.file_lines, it.file_lines_exact, it.file_tokens) == (2526, True, 26_878)
        assert it.repeat_of is None
    assert sum(it.read_tokens for it in its) > 26_878             # the prefixes are counted
    covered = sorted((it.read_start_line, it.read_start_line + it.read_lines - 1) for it in its)
    assert covered[0][0] == 1 and covered[-1][1] >= 2526
    assert all(a[1] + 1 == b[0] for a, b in itertools.pairwise(covered))


# --- a partial Read (offset/limit) ---------------------------------------------------

def test_gate_partial_read(fx, counter):
    first = ledger.items(fx / "partial-read.jsonl", counter)[0]
    lines = (fx / "partial-40.txt").read_text().split("\n")
    shown = next(r for r in records(fx / "partial-read.jsonl")
                 if "toolUseResult" in r)["message"]["content"][0]["content"]
    bare = "\n".join(lines[10:20])
    (file_tokens, prefixed, unprefixed) = tok(counter, (fx / "partial-40.txt").read_text(),
                                              shown, bare)
    assert first.file_lines == 40 and first.file_lines_exact is True
    assert (first.read_start_line, first.read_lines) == (11, 10)
    assert first.read_tokens == prefixed
    assert first.read_tokens < first.file_tokens == file_tokens
    assert first.read_tokens > unprefixed                     # proves the prefixes were counted


def test_gate_partial_reads_covering_the_file_are_two_rows(fx, counter):
    its = ledger.items(fx / "partial-cover.jsonl", counter)
    assert [(it.read_start_line, it.read_lines) for it in its] == [(1, 20), (21, 21)]
    assert {it.file_lines for it in its} == {40}


# --- boot memory -------------------------------------------------------------------

def test_gate_boot_memory(fx, counter):
    its = ledger.items(fx / "boot.jsonl", counter)
    files = next(r for r in records(fx / "boot.jsonl")
                 if r.get("type") == "attachment")["attachment"]["files"]
    assert [it.kind for it in its] == ["boot", "boot"]
    assert [it.key for it in its] == [f["path"] for f in files]
    assert [it.read_tokens for it in its] == tok(counter, *[f["content"] for f in files])


# --- Bash output, not the command ----------------------------------------------------

def test_gate_bash_output_not_the_command(fx, counter):
    seq = ledger.items(fx / "bash.jsonl", counter)[0]
    out = next(r for r in records(fx / "bash.jsonl")
               if "toolUseResult" in r)["message"]["content"][0]["content"]
    assert seq.read_tokens == tok(counter, out)[0]
    assert seq.cmd_tokens == tok(counter, "seq 1 5000")[0]
    assert seq.read_tokens > 1000 * seq.cmd_tokens


def test_gate_bash_no_output_counts_the_placeholder(fx, counter):
    true = ledger.items(fx / "bash.jsonl", counter)[1]
    assert true.note == "no output"
    assert true.read_tokens == tok(counter, "(Bash completed with no output)")[0] == 7


def test_gate_bash_persisted_preview_read_full_output_not_read(fx, counter):
    cat = ledger.items(fx / "bash.jsonl", counter)[2]
    preview = [r for r in records(fx / "bash.jsonl")
               if "toolUseResult" in r][2]["message"]["content"][0]["content"]
    assert cat.read_tokens == tok(counter, preview)[0]
    assert (cat.persisted_bytes, cat.persisted_tokens) == (120_991, 26_878)


# --- other tool results ----------------------------------------------------------------

def test_gate_other_tool_results(fx, counter):
    (it,) = ledger.items(fx / "grep.jsonl", counter)
    assert (it.kind, it.key) == ("tool", "Grep: def scan")
    assert it.read_tokens == tok(counter, "Found 1 file\nsrc/sessiontools/window.py")[0]


# --- no chars/N: every figure is the counter over its source text ------------------------

@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_gate_no_chars_over_n(fx, counter, name):
    its = ledger.items(fx / name, counter)
    srcs = oracle(fx / name)
    assert len(its) == len(srcs)
    pool = sorted({t for s in srcs for t in (s["read"] or [])}
                  | {s[k] for s in srcs for k in ("cmd", "full") if k in s}
                  | {Path(it.key).read_text(encoding="utf-8") for it in its
                     if it.file_tokens_from == "disk"}
                  | {Path(s["persisted"]).read_text(encoding="utf-8") for s in srcs
                     if "persisted" in s})
    n = dict(zip(pool, counter.count(pool), strict=True))
    fulls = {}
    for it, s in zip(its, srcs, strict=True):
        if "full" in s:
            fulls.setdefault(it.key, set()).add(n[s["full"]])
    for it, s in zip(its, srcs, strict=True):
        assert it.counter == count.LABEL
        if s["read"] is None:
            assert it.read_tokens is None
        else:
            assert it.read_tokens == sum(n[t] for t in s["read"]), it
        if "cmd" in s:
            assert it.cmd_tokens == n[s["cmd"]]
        else:
            assert it.cmd_tokens is None
        if it.file_tokens_from == "transcript":
            assert it.file_tokens in fulls[it.key]
        elif it.file_tokens_from == "disk":
            assert it.file_tokens == n[Path(it.key).read_text(encoding="utf-8")]
        else:
            assert it.file_tokens is None
        if "persisted" in s and os.path.exists(s["persisted"]):
            assert it.persisted_tokens == n[Path(s["persisted"]).read_text(encoding="utf-8")]
        else:
            assert it.persisted_tokens is None


# --- counting is over the RAW text (OQ3) ------------------------------------------------

def test_a_load_with_front_matter_is_counted_raw(fx, counter):
    it = next(it for it in ledger.items(fx / "edge.jsonl", counter)
              if it.key.endswith("fm/CLAUDE.md"))
    raw = next(r for r in records(fx / "edge.jsonl") if r.get("type") == "attachment"
               )["attachment"]["content"]["content"]
    body = raw.split("---\n", 2)[2]
    assert raw.startswith("---\n")
    whole, without = tok(counter, raw, body)
    assert it.read_tokens == whole != without
