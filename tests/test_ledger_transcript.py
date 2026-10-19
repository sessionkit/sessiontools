"""The ledger's reader and item rules (ST-2 design §3), on a FAKE counter:
these run on any host, uv or not. The real counts are test_ledger_gates.py's.

Every fixture is synthetic, written by `tests/fixtures/ledger/build.py`.
The transcripts follow the record shapes Claude Code writes. `POLICY-synthetic.md`
is a generated stand-in for a store's rulebook (ST-144): filler prose from a
fixed word list, 2,526 lines, 120,991 bytes, 26,878 tokens by `tokens`. It
has the shapes the gates need: over 2,000 lines, so three 1,000-line reads
split it, and over 30,000 bytes, so a `cat` of it is persisted. A rebuild
must give the committed bytes, and the sha256 below pins them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os

import pytest
from test_ledger_support import FIXTURES, FakeCounter, materialise, records

from sessiontools import ledger
from sessiontools.ledger import transcript

_spec = importlib.util.spec_from_file_location("ledger_fixture_build", FIXTURES / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


@pytest.fixture
def fx(tmp_path):
    return materialise(tmp_path)


def run(path, counter=None):
    return ledger.items(path, counter or FakeCounter())


def write(tmp_path, s, name="t.jsonl"):
    p = tmp_path / name
    p.write_text(s.jsonl(), encoding="utf-8")
    return p


# --- the fixtures themselves ------------------------------------------------------

def test_the_POLICY_fixture_is_the_synthetic_one_byte_for_byte():
    data = (FIXTURES / "POLICY-synthetic.md").read_bytes()
    assert len(data) == 120_991
    assert data.count(b"\n") == 2_526
    assert hashlib.sha256(data).hexdigest() == (
        "ad3c3bb44621b3c5c3fe6ccae0c92002e47a370e18fb07e3c8c6c4da709f387c")


def test_the_POLICY_fixture_is_generated_and_carries_no_address():
    """ST-144: the stand-in is the builder's output, word for word from its
    own list, so no name, host, address or Message-ID can ride in on it."""
    text = build.policy_text()
    assert (FIXTURES / build.POLICY).read_text(encoding="utf-8") == text
    assert "@" not in text and "<" not in text and "/" not in text
    words = {w.strip(".").lower() for line in text.splitlines() for w in line.split()}
    allowed = ({w.lower() for w in build.POLICY_WORDS + build.POLICY_TITLES}
               | {"#", "##", "-", "\u2014", "rule", "rules", "rulebook:", "working", "demo",
                  "shop", "synthetic", "generated,", "not", "written", "filler"})
    assert {w for w in words if not w.rstrip(".").isdigit()} <= allowed


def test_a_rebuild_gives_the_committed_fixtures():
    for name, text in build.build().items():
        assert (FIXTURES / name).read_text(encoding="utf-8") == text, name
    assert (FIXTURES / build.POLICY).read_text(encoding="utf-8") == build.policy_text()
    assert (FIXTURES / build.PARTIAL).read_text(encoding="utf-8") == build.partial_text()
    assert (FIXTURES / build.EDITED).read_text(encoding="utf-8") == build.edited_text(2)


def test_the_fixtures_carry_claude_codes_own_totalLines():
    """Claude Code counts the empty segment after a final newline, so the
    fixtures say 2,527 for POLICY; the reader, not the fixture, corrects it."""
    recs = [json.loads(line) for line in (FIXTURES / "policy-reads.jsonl").read_text().splitlines()]
    totals = {r["toolUseResult"]["file"]["totalLines"] for r in recs if "toolUseResult" in r}
    assert totals == {2527}


# --- reading -----------------------------------------------------------------------

def test_sidechains_bad_lines_and_duplicate_attachments_are_skipped(fx):
    its = run(fx / "edge.jsonl")
    assert [it.key for it in its].count("/Users/agent/work/demo/fm/CLAUDE.md") == 1
    assert "cat /etc/hosts" not in [it.key for it in its]       # isSidechain: true
    assert [it.seq for it in its] == list(range(1, len(its) + 1))


def test_the_session_id_comes_from_the_records(fx):
    its = run(fx / "grep.jsonl")
    assert {it.session for it in its} == {"00000000-0000-4000-8000-000000000009"}


def test_a_transcript_with_no_session_id_falls_back_to_its_stem(tmp_path):
    p = tmp_path / "abc123.jsonl"
    p.write_text(json.dumps({"type": "attachment", "uuid": "u1", "attachment": {
        "type": "nested_memory", "path": "/x/CLAUDE.md", "content": {"content": "hi"}}}) + "\n")
    assert [it.session for it in run(p)] == ["abc123"]


def test_read_text_is_the_tool_result_never_tool_use_result(fx):
    """A persisted Bash's read is its preview; stdout (capped at 30,000
    bytes) is never counted. The fake counter makes the difference visible."""
    it = run(fx / "policy-cat.jsonl")[0]
    rec = next(r for r in records(fx / "policy-cat.jsonl") if "toolUseResult" in r)
    preview = rec["message"]["content"][0]["content"]
    assert preview.startswith("<persisted-output>")
    assert it.read_tokens == len(preview.split()) + 1
    assert it.read_tokens != len(rec["toolUseResult"]["stdout"].split()) + 1


def test_a_skill_row_counts_its_meta_body(fx):
    skill = run(fx / "edge.jsonl")[0]
    assert skill.kind == "tool" and skill.key == "Skill: demo:widget"
    result, body = "Launching skill: demo:widget", ("Base directory for this skill: /x/demo"
                                                   "\n\n# Widget skill\n\nBuild widgets.")
    assert skill.read_tokens == (len(result.split()) + 1) + (len(body.split()) + 1)


def test_a_companion_that_arrives_before_its_result_is_still_joined(tmp_path):
    s = build.Session(50)
    tid = f"toolu_{s.n:04d}{s.k + 2:06d}"   # the id call() will mint next
    s._base("user", message={"role": "user", "content": [{"type": "text", "text": "a b c"}]},
            isMeta=True, sourceToolUseID=tid)
    assert s.call("Skill", {"skill": "x"}, "Launching skill: x", {"success": True}) == tid
    (it,) = run(write(tmp_path, s))
    assert it.read_tokens == 4 + 4


def test_an_image_block_is_unknown_never_zero(fx):
    img = run(fx / "edge.jsonl")[1]
    assert img.kind == "Read" and img.read_tokens is None
    assert "image" in img.note
    assert img.read_lines is None and img.repeat_of is None


def test_a_list_tool_result_sums_its_text_blocks(tmp_path):
    s = build.Session(51)
    s.call("WebFetch", {"url": "https://example.com", "prompt": "p"},
           [{"type": "text", "text": "one two"}, {"type": "text", "text": "three"}], {})
    (it,) = run(write(tmp_path, s))
    assert it.read_tokens == 3 + 2 and it.key == "WebFetch: https://example.com"


def test_endoftext_in_a_result_does_not_crash_the_reader(fx):
    assert any(it.key == "printf '<|endoftext|>'" for it in run(fx / "edge.jsonl"))


@pytest.mark.parametrize("name, inp, key", [
    ("Grep", {"pattern": "def x", "path": "src"}, "Grep: def x"),
    ("Glob", {"pattern": "**/*.py"}, "Glob: **/*.py"),
    ("Edit", {"file_path": "/a/b.py", "old_string": "x"}, "Edit: /a/b.py"),
    ("Write", {"file_path": "/a/c.py", "content": "y"}, "Write: /a/c.py"),
    ("WebSearch", {"query": "tiktoken", "mode": "standard"}, "WebSearch: tiktoken"),
    ("Agent", {"prompt": "long", "description": "survey"}, "Agent: survey"),
    ("mcp__x__y", {"n": 3, "first": "s", "second": "t"}, "mcp__x__y: s"),
    ("Nothing", {"n": 3}, "Nothing"),
])
def test_another_tool_is_keyed_by_its_name_and_main_input(tmp_path, name, inp, key):
    s = build.Session(52)
    s.call(name, inp, "ok", {})
    (it,) = run(write(tmp_path, s))
    assert (it.kind, it.key, it.read_tokens) == ("tool", key, 2)


def test_an_error_result_is_still_a_row(fx):
    err = next(it for it in run(fx / "edge.jsonl") if it.key == "false")
    assert err.is_error and err.read_tokens == 4 + 1


def test_bash_notes_no_output_background_and_persisted(fx):
    its = {it.key: it for it in run(fx / "bash.jsonl")}
    assert its["true"].note == "no output" and its["true"].read_tokens == 5 + 1
    assert its["seq 1 5000"].note is None
    persisted = next(it for k, it in its.items() if k.startswith("cat "))
    assert persisted.note == "persisted" and persisted.persisted_bytes == 120_991
    bg = next(it for it in run(fx / "edge.jsonl") if it.key == "sleep 600")
    assert bg.note == "background"


def test_the_command_is_counted_apart_from_its_output(fx):
    seq = run(fx / "bash.jsonl")[0]
    assert seq.cmd_tokens == 4 and seq.read_tokens == 5000 + 1


def test_two_tool_results_in_one_record_share_no_tool_use_result(tmp_path):
    s = build.Session(53)
    a = s._base("assistant", message={"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "a"}},
        {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "b"}}]})
    s._base("user", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "x"},
        {"type": "tool_result", "tool_use_id": "t2", "content": "y"}]},
        toolUseResult={"stdout": "", "stderr": ""}, sourceToolAssistantUUID=a["uuid"])
    its = run(write(tmp_path, s))
    assert [(it.key, it.note) for it in its] == [("a", None), ("b", None)]


def test_paths_are_normpathed_not_resolved(tmp_path):
    s = build.Session(54)
    s.nested("/x/./y/../y/CLAUDE.md", "a")
    s.nested("/x/y/CLAUDE.md", "a")
    its = run(write(tmp_path, s))
    assert [it.key for it in its] == ["/x/y/CLAUDE.md"] * 2 and its[1].repeat_of == 1


# --- file figures: the EOF rule (by ruling) ------------------------------------------

def test_eof_evidence_makes_file_lines_the_true_count(fx):
    rows = run(fx / "policy-reads.jsonl")
    assert [(it.read_start_line, it.read_lines) for it in rows] == [(1, 1000), (1001, 1000),
                                                                    (2001, 527)]
    assert {(it.file_lines, it.file_lines_exact) for it in rows} == {(2526, True)}


def test_with_no_eof_evidence_total_lines_is_carried_and_flagged(fx):
    (it,) = run(fx / "partial-noeof.jsonl")
    assert it.file_lines == 41 and it.file_lines_exact is False


def test_a_file_with_no_final_newline_keeps_total_lines(tmp_path):
    s = build.Session(55)
    s.read("/x/a.txt", "one\ntwo\nthree", offset=2, limit=5)
    (it,) = run(write(tmp_path, s))
    assert (it.read_start_line, it.read_lines) == (2, 2)
    assert it.file_lines == 3 and it.file_lines_exact is True


def test_a_blank_last_line_counts_as_wc_l_counts_it(tmp_path):
    s = build.Session(56)
    s.read("/x/b.txt", "one\n\n")
    (it,) = run(write(tmp_path, s))
    assert it.file_lines == 2 and it.file_lines_exact is True   # wc -l: 2


def test_loads_count_their_own_lines_exactly(fx):
    for it in run(fx / "boot.jsonl") + run(fx / "nested-twice.jsonl"):
        if it.kind in ("boot", "nested"):
            assert it.file_lines == 12 and it.file_lines_exact is True
            assert (it.read_start_line, it.read_lines) == (1, 12)


def test_a_bash_or_tool_row_has_no_file_figures(fx):
    for it in run(fx / "bash.jsonl") + run(fx / "grep.jsonl"):
        assert (it.file_lines, it.file_lines_exact, it.file_tokens, it.read_lines) == (
            None, None, None, None)


# --- file tokens: transcript, then disk, then unknown --------------------------------

def test_file_tokens_come_from_a_full_read_in_the_transcript_first(tmp_path):
    s = build.Session(57)
    s.read("/gone/f.txt", "a b\nc\n", offset=2, limit=1)
    s.read("/gone/f.txt", "a b\nc\n")
    its = run(write(tmp_path, s))
    assert [(it.file_tokens, it.file_tokens_from) for it in its] == [(4, "transcript")] * 2


def test_the_nearest_full_read_describes_each_row(tmp_path):
    s = build.Session(58)
    s.read("/gone/f.txt", "v1\n")
    s.read("/gone/f.txt", "v two two\n")
    s.read("/gone/f.txt", "v two two\n", offset=1, limit=1)
    assert [it.file_tokens for it in run(write(tmp_path, s))] == [2, 4, 4]


def test_else_the_file_on_disk_now_labelled_disk(fx):
    (it,) = run(fx / "partial-noeof.jsonl")
    text = (fx / "partial-40.txt").read_text()
    assert (it.file_tokens, it.file_tokens_from) == (len(text.split()) + 1, "disk")


def test_a_file_that_is_gone_is_unknown_never_a_guess(fx):
    (fx / "partial-40.txt").unlink()
    (it,) = run(fx / "partial-noeof.jsonl")
    assert (it.file_tokens, it.file_tokens_from) == (None, None) and it.note == "file gone"


def test_a_fifo_is_never_opened_and_non_utf8_is_unknown(tmp_path):
    fifo, binary = tmp_path / "pipe", tmp_path / "bin"
    os.mkfifo(fifo)
    binary.write_bytes(b"\xff\xfe\x00")
    s = build.Session(59)
    s.read(str(fifo), "x\ny", offset=1, limit=1)   # partial: no full read to fall back on
    s.read(str(binary), "x\ny", offset=1, limit=1)
    its = run(write(tmp_path, s))
    assert [it.file_tokens for it in its] == [None, None]
    assert [it.note for it in its] == ["file not text", "file not text"]


# --- one row, one version of the file (foreman, do-001 review N1) ------------------------

def test_a_file_edited_between_reads_never_mixes_two_versions_in_a_row(fx):
    """ae66ce7f's shape: a full read of version 1, then a partial read of
    version 2. The second row's file_lines is version 2's, so its
    file_tokens may not be version 1's: it comes from the disk, which holds
    version 2 (its line count matches)."""
    v1, v2 = run(fx / "edited.jsonl")
    assert (v1.file_lines, v1.file_lines_exact, v1.file_tokens_from) == (10, True, "transcript")
    assert v1.file_tokens == len(build.edited_text(1).split()) + 1
    assert (v2.file_lines, v2.file_lines_exact) == (15, False)   # totalLines, no EOF evidence
    assert (v2.file_tokens, v2.file_tokens_from) == (len(build.edited_text(2).split()) + 1, "disk")
    assert v2.file_tokens != v1.file_tokens and v2.note is None


def test_when_the_disk_holds_another_version_the_file_tokens_are_unknown(fx):
    (fx / build.EDITED).write_text(build.edited_text(1), encoding="utf-8")   # reverted
    v1, v2 = run(fx / "edited.jsonl")
    assert v1.file_tokens_from == "transcript"
    assert (v2.file_tokens, v2.file_tokens_from, v2.note) == (None, None, "file changed")
    assert v2.file_lines == 15                       # the row's own figure stands


def test_an_edited_file_that_is_gone_is_unknown(fx):
    (fx / build.EDITED).unlink()
    _, v2 = run(fx / "edited.jsonl")
    assert (v2.file_tokens, v2.note) == (None, "file gone")


def test_a_later_full_read_of_the_rows_own_version_beats_an_earlier_one_of_another(tmp_path):
    s = build.Session(60)
    s.read("/gone/f.txt", "one\n")                         # totalLines 2
    s.read("/gone/f.txt", "a b\nc d e\n", offset=1, limit=1)   # totalLines 3
    s.read("/gone/f.txt", "a b\nc d e\n")                 # totalLines 3, full
    assert [it.file_tokens for it in run(write(tmp_path, s))] == [2, 6, 6]


def test_a_read_with_no_total_lines_takes_the_nearest_full_read(tmp_path):
    """No totalLines, no file_lines: nothing in the row to mismatch, so the
    nearest full read stands (design §3 as written)."""
    s = build.Session(61)
    s.read("/gone/f.txt", "a b\n")
    s.call("Read", {"file_path": "/gone/f.txt"}, "File unchanged since last read.",
           {"type": "file_unchanged", "file": {"filePath": "/gone/f.txt"}}, is_error=None)
    _, stub = run(write(tmp_path, s))
    assert (stub.file_lines, stub.file_tokens, stub.file_tokens_from) == (None, 3, "transcript")


def test_a_persisted_file_that_is_gone_is_unknown(fx):
    (fx / build.POLICY).unlink()
    (it,) = run(fx / "policy-cat.jsonl")
    assert it.persisted_bytes == 120_991 and it.persisted_tokens is None
    assert it.note == "persisted; persisted file gone"


# --- repeats ---------------------------------------------------------------------

def test_a_second_nested_load_repeats_the_first(fx):
    its = [it for it in run(fx / "nested-twice.jsonl") if it.kind == "nested"]
    assert [it.repeat_of for it in its] == [None, its[0].seq]


def test_reads_whose_ranges_do_not_overlap_are_no_repeat(fx):
    assert [it.repeat_of for it in run(fx / "policy-reads.jsonl")] == [None, None, None]
    assert [it.repeat_of for it in run(fx / "partial-cover.jsonl")] == [None, None]


def test_overlapping_reads_and_a_read_after_a_load_are_repeats(tmp_path):
    s = build.Session(60)
    text = "".join(f"l{k}\n" for k in range(1, 11))
    s.read("/r/a.txt", text, offset=1, limit=5)     # 1-5
    s.read("/r/a.txt", text, offset=5, limit=3)     # 5-7: overlaps 1-5
    s.read("/r/a.txt", text, offset=8, limit=2)     # 8-9: no overlap
    s.nested("/r/CLAUDE.md", "x")
    s.read("/r/CLAUDE.md", "x\n", offset=1, limit=1)   # after a whole-file load
    s.bash("cat /r/a.txt", text)                        # commands are not parsed for paths
    assert [it.repeat_of for it in run(write(tmp_path, s))] == [None, 1, None, None, 4, None]


# --- a repeat is one version; another is a re-read after edit (foreman, O1) ---------------

def test_a_read_of_an_edited_file_is_a_re_read_after_edit_never_a_repeat(fx):
    """edited.jsonl: a full read of version 1 (totalLines 11), then lines 3-6
    of version 2 (totalLines 15). Their lines overlap, their versions do not."""
    v1, v2 = run(fx / "edited.jsonl")
    assert (v1.repeat_of, v1.reread_of) == (None, None)
    assert (v2.repeat_of, v2.reread_of) == (None, v1.seq)


def test_the_same_line_count_with_other_content_is_another_version(tmp_path):
    """An edit that keeps the line count: the lines both reads hold differ."""
    s = build.Session(63)
    s.read("/gone/f.txt", "a\nb\nc\n")
    s.read("/gone/f.txt", "a\nB\nc\n", offset=2, limit=1)     # line 2 changed
    s.read("/gone/f.txt", "a\nB\nc\n", offset=1, limit=1)     # line 1 did not
    its = run(write(tmp_path, s))
    assert [it.repeat_of for it in its] == [None, None, 1]
    assert [it.reread_of for it in its] == [None, 1, None]


def test_a_re_read_of_the_new_version_repeats_it_and_a_revert_repeats_the_old(tmp_path):
    s = build.Session(64)
    one, two = "x\ny\n", "x\ny\nz\n"
    s.read("/gone/f.txt", one)                      # 1: version 1
    s.read("/gone/f.txt", two)                      # 2: version 2, after an edit
    s.read("/gone/f.txt", two, offset=2, limit=2)   # 3: version 2 again
    s.read("/gone/f.txt", one)                      # 4: reverted to version 1
    its = run(write(tmp_path, s))
    assert [it.repeat_of for it in its] == [None, None, 2, 1]
    assert [it.reread_of for it in its] == [None, 1, None, None]


def test_a_load_and_a_read_of_one_version_repeat_across_conventions(tmp_path):
    """A load counts true lines (`wc -l`), a Read `totalLines` (one more for
    a final newline), and a boot load's text has its final newline stripped
    (measured on real transcripts, 2030-08-14: 536 of 537 boot
    loads equal the file less its final newlines, 60 of 70 nested loads the
    file itself). One version still repeats; another is a re-read."""
    s = build.Session(65)
    text = "# T\n\nbody\n"
    s.nested("/gone/CLAUDE.md", text)                  # 1
    s.read("/gone/CLAUDE.md", text)                    # 2: same version, totalLines 4
    s.boot([("/gone/AGENTS.md", text.rstrip("\n"))])   # 3
    s.read("/gone/AGENTS.md", text, offset=2, limit=2)  # 4: same version, no EOF evidence
    s.read("/gone/AGENTS.md", text + "more\n")          # 5: another version
    its = run(write(tmp_path, s))
    assert [it.repeat_of for it in its] == [None, 1, None, 3, None]
    assert [it.reread_of for it in its] == [None, None, None, None, 3]


def test_a_read_with_no_range_is_never_a_repeat(tmp_path):
    s = build.Session(61)
    s.nested("/r/x.md", "x")
    s.call("Read", {"file_path": "/r/x.md"}, "File unchanged since last read.",
           {"type": "file_unchanged", "file": {"filePath": "/r/x.md"}})
    its = run(write(tmp_path, s))
    assert its[1].read_lines is None and its[1].repeat_of is None
    assert its[1].file_lines is None and its[1].file_lines_exact is None


# --- labels, the record, one counter call --------------------------------------------

def test_two_paths_with_one_base_name_get_k_tags(fx):
    assert [it.label for it in run(fx / "boot.jsonl")] == ["CLAUDE.md#1", "CLAUDE.md#2"]
    assert {it.label for it in run(fx / "nested-twice.jsonl") if it.kind == "nested"} == {
        "CLAUDE.md"}


def test_a_command_label_is_its_whitespace_collapsed_text(tmp_path):
    s = build.Session(62)
    s.bash("echo   a\n  b", "a b\n")
    assert run(write(tmp_path, s))[0].label == "echo a b"


def test_the_whole_report_is_counted_in_one_counter_call(fx):
    c = FakeCounter()
    its = ledger.items(fx / "policy-reads.jsonl", c)
    assert len(c.calls) == 1 and len(its) == 3
    assert len(c.calls[0]) == len(set(c.calls[0]))   # each distinct text once


def test_the_record_names_every_figure_with_its_unit_and_the_counter(fx):
    rec = run(fx / "policy-cat.jsonl")[0].record()
    assert list(rec) == ["session", "seq", "kind", "key", "label", "ts", "is_error",
                         "read_tokens", "read_start_line", "read_lines", "file_tokens",
                         "file_tokens_from", "file_lines", "file_lines_exact", "cmd_tokens",
                         "persisted_bytes", "persisted_tokens", "repeat_of", "reread_of", "note",
                         "counter"]
    assert rec["counter"] == FakeCounter.label
    json.dumps(rec)


def test_the_reader_alone_does_no_counting(fx):
    t = transcript.read(fx / "bash.jsonl")
    assert [r.kind for r in t.raws] == ["Bash", "Bash", "Bash"]
    assert t.raws[1].texts == [build.NO_OUTPUT]
    assert not hasattr(t.raws[0], "read_tokens")
