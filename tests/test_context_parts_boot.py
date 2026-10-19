"""The boot views (ST-26 do-002): `--startup` rows, rates and the core
remainder; the `/context` snapshot split; the SETTLED fencepost, one fixture
per signal; and the pointer brief, whose read is inside boot by construction.

Fixtures: tests/fixtures/context_boot/, written by its build.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC
from pathlib import Path

import pytest

from sessiontools.context_parts import boot as B
from sessiontools.context_parts.transcript import (
    BOOT_CATEGORIES,
    BRIEF,
    HOOK,
    MEMORY,
    fold,
)
from sessiontools.start.context import FIRST_MESSAGE_POINTER_LEAD, first_message_pointer

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "context_boot"

_spec = importlib.util.spec_from_file_location("context_boot_fixture_build",
                                               FIXTURES / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def fx(name: str) -> Path:
    return FIXTURES / name


def all_fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.jsonl"))


# ---------- the fixtures are what build.py writes ---------------------------
def test_the_committed_fixtures_are_what_build_py_writes(tmp_path):
    out = subprocess.run([sys.executable, str(FIXTURES / "build.py"), str(tmp_path)],
                         capture_output=True, text=True, check=True)
    built = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert built == sorted(p.name for p in all_fixtures())
    assert len(out.stdout.splitlines()) == len(built)
    for name in built:
        assert (tmp_path / name).read_bytes() == fx(name).read_bytes(), (
            f"{name} differs from build.py's output: re-run build.py and commit")


# ---------- the pointer brief: the context-boot bug, fixed by construction --
def first_user_text(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec.get("type") == "user":
            return rec["message"]["content"]
    raise AssertionError("no user record")


def test_the_pointer_lead_is_the_launchers_own_constant():
    assert B.pointer_lead() is FIRST_MESSAGE_POINTER_LEAD
    assert first_user_text(fx("agent_pointer.jsonl")).startswith(FIRST_MESSAGE_POINTER_LEAD)


def test_pointer_path_reads_the_launchers_pointer_back():
    text = first_message_pointer("/a b/first message.md", 4321)
    assert B.pointer_path(text) == "/a b/first message.md"
    assert B.pointer_path("hi there") is None
    assert B.pointer_path(FIRST_MESSAGE_POINTER_LEAD + "/x.md and more") == "/x.md"
    assert B.pointer_path(FIRST_MESSAGE_POINTER_LEAD) is None


def test_a_pointer_briefs_read_is_inside_boot():
    """The old verb skipped the pointer and took the next plain user turn
    ('are you there?', after call 0) as the first input, fencing at call 0:
    before the brief's file was ever read. Here the read is IN boot."""
    b = B.boot(fx("agent_pointer.jsonl"))
    assert b.startup.brief_pointer == build.POINTER_FILE
    brief_reads = [r for r in b.reads if r.category == BRIEF]
    assert [(r.call, r.chars) for r in brief_reads] == [(1, len(build.BRIEF_TEXT))]
    assert brief_reads[0].key == f"brief: Bash {build.CAT}"
    assert b.settled_call == 1                 # not call 0, where the old verb fenced
    assert b.total == 21100                    # the window that holds the brief
    assert any(r.category == "user" for r in b.reads)   # the stray turn is a read, not a fence


def test_the_brief_is_held_inside_boot_when_a_signal_fires_in_its_batch():
    b = B.boot(fx("agent_held.jsonl"))
    assert (b.signal, b.signal_call) == (B.MAIL_READ, 0)
    assert b.held_for_brief
    assert b.settled_call == 1
    assert [r.chars for r in b.reads if r.category == BRIEF] == [len(build.BRIEF_TEXT)]


def write(tmp_path: Path, recs: list[dict]) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(build.dump(r) for r in recs) + "\n", encoding="utf-8")
    return p


def test_an_unread_pointer_is_a_warning(tmp_path):
    recs = [build.agent_name("x")] + build.boot(build.POINTER)
    recs += [build.assistant("a0", 20000, 10, [build.tool_use("t0", "Bash", command="ls")]),
             build.tool_result("t0", "z"), build.assistant("a1", 20100, 10)]
    b = B.boot(write(tmp_path, recs))
    assert B.POINTER_UNREAD_WARNING.format(path=build.POINTER_FILE) in b.warnings
    assert (b.signal, b.settled_call, b.held_for_brief) == (B.AFTER_BRIEF, 0, False)


def test_a_later_reread_of_the_brief_never_moves_settled(tmp_path):
    """Call 0 reads the brief, call 1 claims a slice, call 3 re-reads the
    brief: SETTLED stays at call 1, with no hold."""
    recs = [build.agent_name("x")] + build.boot(build.POINTER)
    steps = [build.CAT, "tm slice claim do-002", "make test", build.CAT, "git status"]
    for i, command in enumerate(steps):
        recs += [build.assistant(f"a{i}", 20000 + 500 * i, 10,
                                 [build.tool_use(f"t{i}", "Bash", command=command)]),
                 build.tool_result(f"t{i}", "r" * 100)]
    recs.append(build.assistant("a9", 23000, 10))
    b = B.boot(write(tmp_path, recs))
    assert (b.signal, b.settled_call, b.held_for_brief) == (B.SLICE_CLAIM, 1, False)
    no_claim = [r for r in recs if "tm slice claim" not in json.dumps(r)]
    b = B.boot(write(tmp_path, no_claim))
    assert (b.signal, b.settled_call, b.signal_detail) == (B.AFTER_BRIEF, 1, "Bash make test")


# ---------- SETTLED: one fixture per signal, and the output names it --------
@pytest.mark.parametrize("name, signal, settled, detail", [
    ("agent_pointer.jsonl", B.AFTER_BRIEF, 1, "Skill taskmaster"),
    ("agent_mail.jsonl", B.MAIL_READ, 4, "Bash message read 1"),
    ("agent_claim.jsonl", B.SLICE_CLAIM, 3, "Bash cd /w && tm slice claim do-002"),
    ("agent_held.jsonl", B.MAIL_READ, 1, "Bash message read 1"),
    ("agent_unsettled.jsonl", B.NOT_SETTLED, 1, None),
    ("human.jsonl", B.TYPED_INPUT, 2, "now fix the bug"),
    ("human_voice.jsonl", B.TYPED_INPUT, 0, "Voice response: fix the build please"),
    ("human_queued.jsonl", B.TYPED_INPUT, 1, "also run lint"),
])
def test_each_settled_signal(name, signal, settled, detail):
    b = B.boot(fx(name))
    assert b.signal == signal
    assert b.signal_text == B.SIGNALS[signal]
    assert b.settled_call == settled
    assert b.signal_detail == detail


def test_every_signal_has_a_fixture():
    assert {B.boot(p).signal for p in all_fixtures()} == set(B.SIGNALS)


def test_a_mail_read_before_the_boot_readout_does_not_settle():
    """agent_mail.jsonl reads mail at call 1, BEFORE `memory wake` (call 2):
    only the read after the readout (call 4) is the first act on work."""
    b = B.boot(fx("agent_mail.jsonl"))
    assert b.signal_call == 4
    assert any(r.key == "Bash message read 9" for r in b.reads)


def test_the_earlier_of_mail_read_and_slice_claim_wins():
    assert B.boot(fx("agent_claim.jsonl")).signal == B.SLICE_CLAIM      # claim at 3, mail at 4
    assert B.boot(fx("agent_mail.jsonl")).signal == B.MAIL_READ         # mail at 4, claim at 5


def test_an_mcp_mail_tool_is_a_mail_read(tmp_path):
    recs = [build.agent_name("x")] + build.boot("go")
    recs += [build.assistant("a0", 20000, 10, [build.tool_use("t0", "Read", file_path="/r")]),
             build.tool_result("t0", "r" * 50),
             build.assistant("a1", 20200, 10, [build.tool_use("t1", "mcp__mail__mail_read",
                                                              id="7")]),
             build.tool_result("t1", "m" * 50), build.assistant("a2", 20400, 10)]
    b = B.boot(write(tmp_path, recs))
    assert (b.signal, b.settled_call, b.signal_detail) == (B.MAIL_READ, 1, "mcp__mail__mail_read")


def test_agent_identity_decides_agent_or_human():
    assert B.boot(fx("agent_pointer.jsonl")).identity == "sessiontools+st-26.worker.cafe0026"
    assert B.boot(fx("agent_mail.jsonl")).identity == "session-agent-worker:worker"
    human = B.boot(fx("human.jsonl"))
    assert (human.agent, human.identity) == (False, None)


def test_a_human_with_no_typed_input_is_not_settled(tmp_path):
    recs = build.boot("hi") + [build.assistant("a0", 17000, 5)]
    b = B.boot(write(tmp_path, recs))
    assert (b.agent, b.signal, b.settled_call) == (False, B.NOT_SETTLED, 0)


def test_the_human_fixture_skips_meta_reminder_and_slash_output():
    """Between calls 1 and 2: a caveat, a /context run, its stdout and a
    reminder, none typed; had any counted, settled would be call 1."""
    assert B.boot(fx("human.jsonl")).settled_call == 2


# ---------- the boot's exact anchors ----------------------------------------
@pytest.mark.parametrize("path", all_fixtures(), ids=lambda p: p.name)
def test_boot_anchors_are_exact(path):
    f = fold(path)
    b = B.boot(path)
    assert b.calls == len(f.calls)
    assert B.CALLS_MISMATCH_WARNING not in b.warnings
    k = b.settled_call
    assert b.total == f.calls[k].ctx
    assert b.on_demand == b.total - f.calls[0].ctx
    assert b.own_output == sum(c.output_tokens for c in f.calls[:k])
    assert b.unattributed == b.on_demand - b.own_output - b.reads_tokens
    assert all(1 <= r.call <= k for r in b.reads)
    assert sum(r.chars for r in b.reads) == sum(e.chars for c in f.calls[1:k + 1]
                                                for e in c.entered)


def test_an_empty_transcript_is_unknown_not_zero(tmp_path):
    b = B.boot(write(tmp_path, build.boot("hi")))
    assert b.startup.total is None and b.startup.core is None
    assert (b.calls, b.settled_call, b.total, b.on_demand, b.unattributed) == (
        0, None, None, None, None)
    assert b.signal == B.NOT_SETTLED
    assert B.startup(write(tmp_path, [])).total is None


# ---------- --startup: rows, per-kind rates, the core remainder -------------
def test_startup_total_is_the_first_calls_window():
    assert B.startup(fx("startup.jsonl")).total == 30000


def test_startup_rows_are_counted_at_per_kind_rates():
    s = B.startup(fx("startup.jsonl"))
    rows = {r.category: r for r in s.rows}
    mem = rows[MEMORY]
    assert (mem.chars, mem.count, mem.rate) == (2600, 2, 0.385)
    assert mem.tokens == round(2600 * 0.385)
    assert "calibrated" in mem.rate_source
    for r in s.rows:
        assert r.basis == "counted" and r.fitted and not r.exact
        assert r.tokens == round(r.chars * r.rate)
        if r.category != MEMORY:
            assert (r.rate, r.rate_source) == (B.DEFAULT_BOOT_RATE, B.DEFAULT_RATE_SOURCE)


def test_startup_names_every_boot_kind_and_a_boot_hook():
    s = B.startup(fx("startup.jsonl"))
    cats = [r.category for r in s.rows]
    assert cats[:len(BOOT_CATEGORIES)] == list(BOOT_CATEGORIES)
    assert HOOK in cats               # a SessionStart hook line is named, not core
    assert all(r.label for r in s.rows)


@pytest.mark.parametrize("path", all_fixtures(), ids=lambda p: p.name)
def test_core_is_the_boot_total_minus_the_named_rows_exactly(path):
    s = B.startup(path)
    assert isinstance(s.core.tokens, int)
    assert s.core.tokens == s.total - sum(r.tokens for r in s.rows)
    assert s.core.tokens + s.named == s.total
    assert (s.core.category, s.core.label, s.core.basis) == (B.CORE, B.CORE_LABEL, "remainder")
    assert not s.core.fitted


def test_a_core_below_zero_is_a_warning(tmp_path):
    b = B.startup(write(tmp_path, build.boot("hi") + [build.assistant("a0", 100, 5)]))
    assert b.core.tokens < 0
    assert B.CORE_NEGATIVE_WARNING in b.warnings


# ---------- the /context snapshot split -------------------------------------
def test_a_context_snapshot_splits_the_core_and_labels_its_time():
    s = B.startup(fx("human.jsonl"))
    split = {r.category: r for r in s.core_split}
    assert [r.category for r in s.core_split] == [
        "System prompt", "System tools", "MCP tools", B.CORE_REST]
    assert (split["System prompt"].tokens, split["System tools"].tokens,
            split["MCP tools"].tokens) == (2100, 11900, 1250)
    assert sum(r.tokens for r in s.core_split) == s.core.tokens
    assert split[B.CORE_REST].tokens == s.core.tokens - 15250
    assert all(split[n].basis == "snapshot" and split[n].fitted
               for n in ("System prompt", "System tools", "MCP tools"))
    assert (split[B.CORE_REST].basis, split[B.CORE_REST].label) == (
        "remainder", B.CORE_REST_LABEL)
    assert s.snapshot_label(UTC) == "from /context at 06:00"
    local = s.snapshot.at.astimezone().strftime("%H:%M")
    assert s.snapshot_label() == f"from /context at {local}"


def test_the_snapshot_is_the_meta_record_not_a_quoted_capture():
    lines = fx("human.jsonl").read_text(encoding="utf-8").splitlines()
    s = B.startup(fx("human.jsonl"))
    rec = json.loads(lines[s.snapshot.line])
    assert rec["isMeta"] is True and rec["timestamp"] == build.CONTEXT_AT
    quoted = [i for i, ln in enumerate(lines) if "Quoting a capture" in ln]
    assert quoted and quoted[0] < s.snapshot.line


def test_no_snapshot_means_no_split():
    s = B.startup(fx("startup.jsonl"))
    assert s.snapshot is None and s.core_split == () and s.snapshot_label() is None


# ---------- guards ----------------------------------------------------------
def test_the_boot_modules_import_only_the_standard_library():
    code = ("import sys; before = set(sys.modules); "
            "import sessiontools.context_parts.boot, sessiontools.context_parts.slash_context; "
            "new = {m.split('.')[0] for m in set(sys.modules) - before}; "
            "print(sorted(m for m in new if m != 'sessiontools' "
            "and m not in sys.stdlib_module_names))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "[]"


def test_boot_never_spells_the_pointer_lead_itself():
    """The lead is imported from sessiontools.start.context, never copied:
    `test_the_pointer_lead_is_the_launchers_own_constant` pins the identity,
    this pins that no second spelling can drift from it. (No guard diffs
    start/context.py against origin/master: a later task may edit it.)"""
    src = (ROOT / "src" / "sessiontools" / "context_parts" / "boot.py").read_text(
        encoding="utf-8")
    for quote in ('"', "'"):
        assert f"{quote}{FIRST_MESSAGE_POINTER_LEAD}{quote}" not in src
