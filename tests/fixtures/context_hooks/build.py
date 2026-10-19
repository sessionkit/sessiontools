#!/usr/bin/env python3
"""Build the --hooks and --tools fixtures (ST-26 do-003): synthetic Claude
Code transcripts whose hook and tool figures can be summed by hand.

    python tests/fixtures/context_hooks/build.py            # rewrite them here
    python tests/fixtures/context_hooks/build.py OUTDIR     # write them elsewhere

Deterministic: tests/test_context_parts_hooks.py rebuilds them and compares.
The record helpers are context_parts/build.py's (loaded by path, not copied).

- hooks.jsonl  25 calls: call 0 is the boot, calls 0-23 each issue a tool
               batch, call 24 only answers. So 24 tool batches (1-24), and
               these hooks (the hand sums are in the test):
  - pager-hook        the SAME line every batch: exact repeats, steady.
  - notifications     `[clock] 13:MM AEDT`, only the minutes change: templated
                      repeats, steady by its form. It shares each batch's
                      toolUseID with pager-hook: each line joins its own.
  - posttool-clock    a PostToolUse line per tool call, seconds differ; every
                      4th batch has 3 tools, so 2 in-batch repeats there.
  - mail-hook         a different line every batch, no digits: no repeats.
  - half-hook         one line in batches 1-11: 11 < 50% of 24, NOT steady.
  - twelve-hook       one line in batches 1-12: exactly 50%, steady.
  - session-start     one SessionStart line read by the boot call.
  - UserPromptSubmit  a line with no hook_success: keyed by its event name.
  - tail-hook         a line after the last call: no call read it, not counted.
- tools.jsonl  41 calls, each growth = own output + 0.3 x the result chars
               that entered (a fit recovers 0.3); results from Read, Bash,
               Grep, an MCP tool and one whose tool_use was never written.
               One interval is a compaction: its result (the biggest of all)
               is not in the Tool results row, so not in --tools either.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "context_parts_fixture_build_for_hooks", HERE.parent / "context_parts" / "build.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

assistant, attachment, dump = base.assistant, base.attachment, base.dump
tool_use, tool_result, boot_records = base.tool_use, base.tool_result, base.boot_records

# ---------- hooks.jsonl -----------------------------------------------------
BATCHES = 24
STEADY_LINE = "pager is listening, session box live"         # 36 chars
HALF_LINE = "half of the batches"                             # 19 chars
TWELVE_LINE = "exactly half of them"                          # 20 chars
SESSION_START_LINE = "w" * 400
PROMPT_LINE = "prompt hook line"                              # 16 chars
TAIL_LINE = "nobody read this"


def clock_line(k: int) -> str:
    return f"[clock] 13:{k:02d} AEDT"                         # 18 chars


def posttool_line(s: int) -> str:
    return f"[t] 13:00:{s:02d}"                               # 12 chars


def mail_line(k: int) -> str:
    return "mail from " + chr(ord("a") + k) * 6               # 16 chars, no digits


def tools_in(k: int) -> int:
    """Tool calls in batch k (issued by call k-1): 3 in every 4th, else 1."""
    return 3 if k % 4 == 1 else 1


def hook(hid: str, command: str | None, lines: list[str], event: str = "PostToolBatch",
         ) -> list[dict]:
    """A hook_success (unless `command` is None) and its context lines."""
    recs = []
    if command is not None:
        stdout = json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                    "additionalContext": "\n".join(lines)}})
        recs.append(attachment(type="hook_success", hookName=event, toolUseID=hid,
                               hookEvent=event, command=command, stdout=stdout + "\n",
                               stderr="", exitCode=0, content=""))
    recs.append(attachment(type="hook_additional_context", content=lines, hookName=event,
                           toolUseID=hid, hookEvent=event))
    return recs


def hooks_fixture() -> list[str]:
    recs = boot_records()
    recs += hook("ss", "session-start", [SESSION_START_LINE], "SessionStart")
    second = 0
    for k in range(1, BATCHES + 1):
        n = tools_in(k)
        tids = [f"t{k}-{i}" for i in range(n)]
        recs.append(assistant(f"c{k - 1}", 20000 + 1000 * (k - 1), 50,
                              [tool_use(t, "Bash", command=f"step {k}") for t in tids]))
        for t in tids:
            recs.append(tool_result(t, "r" * 100))
            recs += hook(t, "posttool-clock", [posttool_line(second)], "PostToolUse")
            second += 1
        batch = f"b{k}"
        recs += hook(batch, "notifications", [clock_line(k)])
        # Two hooks under one batch id: each line joins the success that carried it.
        recs += hook(batch, "pager-hook", [STEADY_LINE])
        recs += hook(f"msg{k}", "mail-hook", [mail_line(k)])
        if k <= 11:
            recs += hook(f"h{k}", "half-hook", [HALF_LINE])
        if k <= 12:
            recs += hook(f"w{k}", "twelve-hook", [TWELVE_LINE])
        if k == 5:
            recs += hook("ups", None, [PROMPT_LINE], "UserPromptSubmit")
    recs.append(assistant(f"c{BATCHES}", 20000 + 1000 * BATCHES, 50))
    recs += hook("tail", "tail-hook", [TAIL_LINE])
    return [dump(r) for r in recs]


# ---------- tools.jsonl -----------------------------------------------------
TOOL_RATE = 0.3
COMPACT_AT = 20                    # the interval into call 20 is a compaction
#: (tool, input, result chars) issued by call k-1, read by call k.
TOOL_STEPS = {
    1: ("Read", {"file_path": "/w/big.md"}, 9000),
    2: ("Bash", {"command": "make test"}, 4000),
    3: ("Grep", {"pattern": "def tools"}, 700),
    4: ("mcp__sessionmail__mail_read", {"n": 1}, 1500),
    5: ("Bash", {"command": "cat log.txt"}, 8000),
    6: (None, None, 600),                             # its tool_use was never written
    7: ("Read", {"file_path": "/w/tie-a.md"}, 6000),
    8: ("Read", {"file_path": "/w/tie-b.md"}, 6000),
    COMPACT_AT: ("Read", {"file_path": "/w/huge.md"}, 20000),
}
TOOL_CALLS = 41


def step(k: int) -> tuple[str | None, dict | None, int]:
    return TOOL_STEPS.get(k, ("Bash", {"command": "git status"}, 200 + 10 * k))


def tools_fixture() -> list[str]:
    recs = boot_records()
    ctx = 25000
    for k in range(TOOL_CALLS):
        nxt = k + 1
        content = None
        if nxt < TOOL_CALLS:
            name, tool_input, _ = step(nxt)
            if name is not None:
                content = [tool_use(f"u{nxt}", name, **tool_input)]
        recs.append(assistant(f"k{k}", ctx, 100, content))
        if nxt < TOOL_CALLS:
            _, _, chars = step(nxt)
            recs.append(tool_result(f"u{nxt}", "x" * chars))
            ctx = 3000 if nxt == COMPACT_AT else ctx + 100 + round(TOOL_RATE * chars)
    return [dump(r) for r in recs]


FIXTURES = {
    "hooks.jsonl": hooks_fixture,
    "tools.jsonl": tools_fixture,
}


def build(outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, make in FIXTURES.items():
        path = outdir / name
        path.write_text("\n".join(make()) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    for p in build(target):
        print(p)
