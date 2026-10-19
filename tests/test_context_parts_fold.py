"""context_parts' fold and exact anchors (ST-26 do-001, design sections 2,
3.2 and 5). The fixtures are built by tests/fixtures/context_parts/build.py;
every expected number below is worked out by hand from that file's records,
never by the code under test."""

from __future__ import annotations

import json
import math
from pathlib import Path

from sessiontools import context_parts as cp
from sessiontools.context_parts import transcript as fd

FIXTURES = Path(__file__).parent / "fixtures" / "context_parts"


def write(tmp_path, *records, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records)
                 + "\n", encoding="utf-8")
    return p


def call(mid, ctx, out=10, content=None, model="claude-opus-5-5", **extra):
    msg = {"id": mid, "model": model, "content": content or [],
           "usage": {"input_tokens": ctx, "output_tokens": out}}
    return {"type": "assistant", "message": msg, **extra}


def att(**a):
    return {"type": "attachment", "attachment": a}


# ---------- the anchors fixture: exact, by hand ------------------------------

def test_anchors_equal_the_hand_sums_of_the_usage_rows():
    a = cp.anchors(cp.fold(FIXTURES / "anchors.jsonl"))
    # calls 20000, 21500, 22300, 22500 (the sidechain 99999 and the
    # <synthetic> row are not calls; a1's two records are one call)
    assert a.calls == 4
    assert a.boot == 20000
    assert a.used == 22500
    assert a.since_boot == (21500 - 20000) + (22300 - 21500) + (22500 - 22300) == 2500
    # a1's output is the larger of its two records (40, 100); d1 is the last
    # call, so its 30 has not entered the window yet
    assert a.own_output == 100 + 250 + 75 == 425
    assert a.output_total == 425 + 30
    assert a.intervals == 3
    assert a.compactions == ()


def test_the_fold_sums_all_three_input_fields():
    f = cp.fold(FIXTURES / "anchors.jsonl")
    assert [c.ctx for c in f.calls] == [20000, 21500, 22300, 22500]
    assert [c.message_id for c in f.calls] == ["a1", "b1", "c1", "d1"]
    assert f.model == "claude-opus-5-5"


def test_entries_land_on_the_call_that_read_them_with_their_keys():
    f = cp.fold(FIXTURES / "anchors.jsonl")
    assert f.calls[1].entered == (cp.Entry("result", "Read /etc/hosts", 800),
                                  cp.Entry("hook", "clock-hook", len("[clock] 09:00")))
    assert f.calls[2].entered == (cp.Entry("result", "Bash git status", 120),
                                  cp.Entry("queued", "prompt", 60))
    assert f.calls[3].entered == (cp.Entry("user", "text", 6),)


def test_boot_entries_are_section_twos_boot_rows():
    boot = cp.fold(FIXTURES / "anchors.jsonl").boot_entered
    assert boot[:3] == (cp.Entry("memory", "/a/CLAUDE.md", 400),
                        cp.Entry("memory", "/b/CLAUDE.md", 100),
                        cp.Entry("skills", "skill_listing", 300))
    assert [e.category for e in boot[3:]] == ["environment", "environment", "brief"]
    assert boot[-1] == cp.Entry("brief", "text", 50)
    # prompt_snapshot is the system prompt: inside the core remainder, never an entry
    assert all(e.key != "prompt_snapshot" for e in boot)


def test_every_category_has_a_label():
    assert set(cp.LABELS) == set(cp.CATEGORIES)
    assert set(cp.BOOT_CATEGORIES).isdisjoint(cp.FIT_CATEGORIES)


# ---------- compactions: counted, never subtracted ---------------------------

def test_a_compaction_is_counted_and_reported_not_subtracted():
    a = cp.anchors(cp.fold(FIXTURES / "compaction.jsonl"))
    assert a.boot == 10000
    assert a.since_boot == 2000 + 3000 + 1000 + 1500 == 7500   # the -11000 is not in it
    assert len(a.compactions) == 1
    (c,) = a.compactions
    assert (c.call, c.before, c.after, c.dropped) == (3, 15000, 4000, 11000)
    # own output: calls 0, 1, 3, 4 (2 precedes the compaction, 5 is last)
    assert a.own_output == 400
    assert a.output_total == 600
    assert a.intervals == 4


def test_parts_carries_the_compaction_and_its_residual_adds_up():
    p = cp.parts(FIXTURES / "compaction.jsonl")
    assert p.anchors.since_boot == 7500
    assert len(p.anchors.compactions) == 1
    assert math.isclose(sum(r.tokens for r in p.rows), 7500)


# ---------- the record kinds ------------------------------------------------

def test_hook_lines_join_their_command_whichever_side_the_success_is(tmp_path):
    def success(tid, command, ctx):
        out = json.dumps({"hookSpecificOutput": {"additionalContext": ctx}})
        return att(type="hook_success", toolUseID=tid, hookName="PostToolBatch",
                   command=command, stdout=out)

    p = write(tmp_path, call("a", 100),
              success("h1", "clock", "[clock] 09:00"),
              success("h1", "pager", "pager is listening"),
              att(type="hook_additional_context", toolUseID="h1", hookName="PostToolBatch",
                  content=["[clock] 09:00", "pager is listening"]),
              # the context BEFORE its success
              att(type="hook_additional_context", toolUseID="h2", hookName="PostToolUse",
                  content=["late line"]),
              success("h2", "ears", "late line"),
              # no success at all: the event names it
              att(type="hook_additional_context", toolUseID="h3", hookName="Stop",
                  content=["orphan"]),
              call("b", 200))
    assert cp.fold(p).calls[1].entered == (
        cp.Entry("hook", "clock", 13), cp.Entry("hook", "pager", 18),
        cp.Entry("hook", "ears", 9), cp.Entry("hook", "Stop", 6))


def test_user_text_first_is_the_brief_then_user(tmp_path):
    p = write(tmp_path,
              {"type": "user", "message": {"content": [{"type": "text", "text": "brief!"}]}},
              call("a", 100),
              {"type": "user", "message": {"content": "later"}},
              call("b", 200))
    f = cp.fold(p)
    assert f.calls[0].entered == (cp.Entry("brief", "text", 6),)
    assert f.calls[1].entered == (cp.Entry("user", "text", 5),)


def test_attachment_kinds_map_to_section_twos_rows(tmp_path):
    p = write(tmp_path, call("a", 100),
              att(type="total_tokens_reminder", text="t" * 30),
              att(type="silent_turn_reminder", text="s" * 20),
              att(type="queued_command", prompt="q" * 7),
              att(type="nested_memory", path="/n/CLAUDE.md", content={"content": "m" * 11}),
              att(type="agent_listing_delta", agents=["x"]),
              att(type="deferred_tools_delta", added=["y"]),
              att(type="deferred_tools_record", entries=[]),
              att(type="prompt_snapshot", systemPrompt=["P"]),
              att(type="bash_output_audience_note", note="n"),
              call("b", 200))
    got = [(e.category, e.key) for e in cp.fold(p).calls[1].entered]
    assert got == [("reminder", "total_tokens_reminder"), ("reminder", "silent_turn_reminder"),
                   ("queued", "queued_command"), ("memory", "/n/CLAUDE.md"),
                   ("skills", "agent_listing_delta"), ("mcp", "deferred_tools_delta"),
                   ("other", "bash_output_audience_note")]
    entered = cp.fold(p).calls[1].entered
    assert entered[0].chars == 30 and entered[3].chars == 11


def test_a_tool_key_is_the_tool_and_what_it_touched(tmp_path):
    long_cmd = "echo " + "z" * 400
    p = write(tmp_path,
              call("a", 100, content=[
                  {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "fo  o"}},
                  {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": long_cmd}},
                  {"type": "tool_use", "id": "t3", "name": "TodoWrite", "input": {"todos": []}}]),
              {"type": "user", "message": {"content": [
                  {"type": "tool_result", "tool_use_id": "t1", "content": "a"},
                  {"type": "tool_result", "tool_use_id": "t2", "content": "b"},
                  {"type": "tool_result", "tool_use_id": "t3", "content": "c"},
                  {"type": "tool_result", "tool_use_id": "nope", "content": "d"}]}},
              call("b", 200))
    keys = [e.key for e in cp.fold(p).calls[1].entered]
    assert keys[0] == "Grep fo o"
    assert keys[1].startswith("Bash echo zzz") and keys[1].endswith("...")
    assert len(keys[1]) == len("Bash ") + fd._KEY_DETAIL_MAX
    assert keys[2:] == ["TodoWrite", "tool_result"]


def test_a_zero_char_entry_is_dropped(tmp_path):
    p = write(tmp_path, call("a", 100), {"type": "user", "message": {"content": ""}},
              att(type="hook_additional_context", toolUseID="h", content=[""]),
              call("b", 200))
    assert cp.fold(p).calls[1].entered == ()


# ---------- what the fold skips ---------------------------------------------

def test_unreadable_lines_are_skipped_never_fatal(tmp_path):
    p = write(tmp_path,
              call("a", 100, out=5),
              "{half a line",
              "[1, 2]",
              {"type": "assistant", "message": "not an object"},
              {"type": "assistant", "message": {"id": "s", "model": "m",
                                                "usage": {"input_tokens": "12"}}},
              {"type": "assistant", "message": {"id": "n", "model": "m",
                                                "usage": {"input_tokens": float("nan")}}},
              {"type": "assistant", "message": {"id": "t", "model": "m",
                                                "usage": {"input_tokens": True}}},
              {"type": "assistant", "message": {"id": "e", "model": "m", "usage": {}}},
              call("syn", 0, model=fd.SYNTHETIC_MODEL),
              call("side", 5000, isSidechain=True),
              {"type": "attachment", "attachment": "nope"},
              {"type": "user", "message": None},
              call("b", 150, out=7))
    f = cp.fold(p)
    assert [(c.message_id, c.ctx, c.output_tokens) for c in f.calls] == [("a", 100, 5),
                                                                          ("b", 150, 7)]
    assert f.model == "claude-opus-5-5"


def test_an_empty_transcript_folds_to_nothing(tmp_path):
    p = write(tmp_path, "")
    f = cp.fold(p)
    assert f.calls == () and f.boot_entered == () and f.model is None
    assert cp.anchors(f) == cp.Anchors()


# ---------- one call acceptance (do-003 review F3) ----------------------------
def test_call_reading_accepts_exactly_what_the_fold_counts():
    ok = call("a", 100, out=5)["message"]
    assert fd.call_reading(ok) == fd.Reading("claude-opus-5-5", 100, 5, "a")
    assert fd.call_reading({**ok, "model": "  claude-opus-5-5 "}).model == "claude-opus-5-5"
    assert fd.call_reading({**ok, "id": 7}).message_id is None
    for bad in ({**ok, "model": fd.SYNTHETIC_MODEL}, {**ok, "model": " "},
                {**ok, "model": None}, {**ok, "usage": {}}, {**ok, "usage": "x"},
                {**ok, "usage": {"input_tokens": "12"}},
                {**ok, "usage": {"input_tokens": 1, "output_tokens": float("inf")}},
                {**ok, "usage": {"input_tokens": True}}):
        assert fd.call_reading(bad) is None, bad


def test_the_fold_hooks_and_boot_count_the_same_calls(tmp_path):
    """Each counts through `call_reading`, so edge records cannot split them."""
    from sessiontools.context_parts import boot, hooks
    p = write(tmp_path,
              call("a", 100, out=5),
              call("a", 100, out=9),                       # a's second block: one call
              {"type": "assistant", "message": {"id": "e", "model": "m", "usage": {}}},
              {"type": "assistant", "message": {"id": "n", "model": "m",
                                                "usage": {"input_tokens": float("nan")}}},
              call("syn", 0, model=fd.SYNTHETIC_MODEL),
              call("side", 5000, isSidechain=True),
              call(None, 120, out=1),                      # no id: a call each time
              call(None, 130, out=1),
              call("b", 150, out=7))
    folded = len(fd.fold(p).calls)
    assert folded == 4
    assert hooks.hooks(p).calls == folded
    assert boot._timeline(p).calls == folded


def test_hooks_and_boot_carry_no_copy_of_the_acceptance():
    root = Path(__file__).resolve().parents[1] / "src" / "sessiontools" / "context_parts"
    for name in ("hooks.py", "boot.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "call_reading(" in src, name
        for copied in ("SYNTHETIC_MODEL", "_usage_ctx", "_int_count"):
            assert copied not in src, f"{name} still spells {copied}"
