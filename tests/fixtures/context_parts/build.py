#!/usr/bin/env python3
"""Build the context_parts fixtures: synthetic Claude Code transcripts whose
totals can be worked out by hand, or whose per-category rates are PLANTED.

    python tests/fixtures/context_parts/build.py            # rewrite them here
    python tests/fixtures/context_parts/build.py OUTDIR     # write them elsewhere

Deterministic (a seeded RNG), so the committed .jsonl files are exactly what
this writes; tests/test_context_parts_fixtures.py rebuilds them and compares.

- anchors.jsonl     four calls, hand-computable: boot, since-boot, own output,
                    duplicate records of one message id, a sidechain, a
                    <synthetic> row, a bad line, a tool and a hook join.
- compaction.jsonl  six calls, one compaction (the window drops 15000 -> 4000).
- synthetic.jsonl   150 intervals with PLANTED tokens-per-char rates (RATES).
- young.jsonl       10 intervals: too few to fit.
- poor.jsonl        a planted session plus 60 intervals of growth no category
                    explains: a poor fit.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

MODEL = "claude-opus-5-5"

#: The planted rates synthetic.jsonl and poor.jsonl are built from.
RATES = {"result": 0.31, "hook": 0.42, "queued": 0.27, "reminder": 0.55,
         "user": 0.29, "other": 0.36}


def dump(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False, sort_keys=True)


def usage(ctx: int, out: int) -> dict:
    """ctx split over all three input fields, so the fold must sum them."""
    creation = ctx // 10
    return {"input_tokens": 5, "cache_creation_input_tokens": creation,
            "cache_read_input_tokens": ctx - 5 - creation, "output_tokens": out}


def assistant(mid: str, ctx: int, out: int, content=None, model: str = MODEL,
              sidechain: bool = False) -> dict:
    rec = {"type": "assistant", "message": {
        "id": mid, "model": model, "role": "assistant",
        "content": content or [{"type": "text", "text": "ok"}],
        "usage": usage(ctx, out)}}
    if sidechain:
        rec["isSidechain"] = True
    return rec


def tool_use(tid: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": tool_input}


def tool_result(tid: str, content) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": content}]}}


def user_text(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def attachment(**a) -> dict:
    return {"type": "attachment", "attachment": a}


def hook_pair(hid: str, command: str, line: str) -> list[dict]:
    stdout = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolBatch",
                                                "additionalContext": line}})
    return [attachment(type="hook_success", hookName="PostToolBatch", toolUseID=hid,
                       hookEvent="PostToolBatch", command=command, stdout=stdout + "\n",
                       stderr="", exitCode=0, content=""),
            attachment(type="hook_additional_context", content=[line],
                       hookName="PostToolBatch", toolUseID=hid, hookEvent="PostToolBatch")]


def boot_records() -> list[dict]:
    return [
        attachment(type="instructions", files=[
            {"path": "/a/CLAUDE.md", "type": "Project", "content": "x" * 400},
            {"path": "/b/CLAUDE.md", "type": "Project", "content": "y" * 100}]),
        attachment(type="skill_listing", content="S" * 300),
        attachment(type="environment", snapshot={"workingDirectory": "/w", "platform": "darwin"}),
        attachment(type="date", date="2030-12-08"),
        attachment(type="prompt_snapshot", systemPrompt=["P" * 5000]),
        user_text("B" * 50),
    ]


def anchors_fixture() -> list[str]:
    """Hand sums (tests/test_context_parts_fold.py):
    calls 20000, 21500, 22300, 22500 -> boot 20000, since boot 2500, used 22500;
    output 100 (max of 40, 100), 250, 75, 30 -> own output 425, total 455."""
    recs = boot_records()
    recs += [
        assistant("a1", 20000, 40, [{"type": "thinking", "thinking": ""}]),
        assistant("a1", 20000, 100, [tool_use("toolu_1", "Read", file_path="/etc/hosts")]),
        assistant("side", 99999, 9999, sidechain=True),
        tool_result("toolu_1", "r" * 800),
        *hook_pair("h1", "clock-hook", "[clock] 09:00"),
        assistant("b1", 21500, 250, [tool_use("toolu_2", "Bash", command="git status")]),
    ]
    lines = [dump(r) for r in recs]
    lines.append("{not json")
    recs = [
        assistant("syn", 0, 0, model="<synthetic>"),
        tool_result("toolu_2", [{"type": "text", "text": "s" * 120}]),
        attachment(type="queued_command", prompt="q" * 60, commandMode="prompt"),
        assistant("c1", 22300, 75),
        user_text("thanks"),
        assistant("d1", 22500, 30),
    ]
    return lines + [dump(r) for r in recs]


def compaction_fixture() -> list[str]:
    """ctx 10000, 12000, 15000, 4000, 5000, 6500; output 100 each.
    since boot 2000 + 3000 + 1000 + 1500 = 7500 (the -11000 is a compaction,
    not subtracted); own output: calls 0, 1, 3, 4 = 400; total 600."""
    recs = boot_records()
    for i, ctx in enumerate((10000, 12000, 15000, 4000, 5000, 6500)):
        if i:
            recs.append(user_text("u" * 40))
        recs.append(assistant(f"msg{i}", ctx, 100))
    return [dump(r) for r in recs]


def planted(rng: random.Random, n: int, prefix: str = "p") -> tuple[list[dict], int, int]:
    """N intervals whose non-output growth is exactly round(sum rate x chars)
    over the categories that entered; returns (records, the last call's ctx
    and its output)."""
    presence = {"result": 0.8, "hook": 0.9, "queued": 0.2, "reminder": 0.5,
                "user": 0.3, "other": 0.3}
    sizes = {"result": (100, 3000), "hook": (20, 600), "queued": (100, 1000),
             "reminder": (40, 200), "user": (20, 800), "other": (100, 1500)}
    recs = boot_records()
    ctx = 25000
    out = rng.randint(50, 3000)
    recs.append(assistant(f"{prefix}0", ctx, out,
                          [tool_use(f"{prefix}t0", "Bash", command="make test")]))
    for k in range(1, n + 1):
        tokens = 0.0
        for cat, p in presence.items():
            if rng.random() >= p:
                continue
            chars = rng.randint(*sizes[cat])
            if cat == "result":
                recs.append(tool_result(f"{prefix}t{k - 1}", "r" * chars))
            elif cat == "hook":
                recs += hook_pair(f"{prefix}h{k}", "clock-hook", "h" * chars)
            elif cat == "queued":
                recs.append(attachment(type="queued_command", prompt="q" * chars))
            elif cat == "reminder":
                recs.append(attachment(type="total_tokens_reminder", text="t" * chars))
            elif cat == "user":
                recs.append(user_text("u" * chars))
            else:
                a = {"type": "edited_text_file", "filename": "/f", "snippet": "e" * chars}
                chars = len(json.dumps(a, ensure_ascii=False))
                recs.append(attachment(**a))
            tokens += RATES[cat] * chars
        ctx += out + round(tokens)
        out = rng.randint(50, 3000)
        recs.append(assistant(f"{prefix}{k}", ctx, out,
                              [tool_use(f"{prefix}t{k}", "Bash", command="make test")]))
    return recs, ctx, out


def synthetic_fixture() -> list[str]:
    recs, _, _ = planted(random.Random(26), 150)
    return [dump(r) for r in recs]


def young_fixture() -> list[str]:
    recs, _, _ = planted(random.Random(7), 10)
    return [dump(r) for r in recs]


def poor_fixture() -> list[str]:
    """100 planted intervals, then 60 where NOTHING entered but the window
    grew 3000 past the call's own output: growth no category can absorb."""
    recs, ctx, out = planted(random.Random(41), 100)
    for k in range(60):
        ctx += out + 3000          # the previous call's output, plus the unexplained
        out = 200
        recs.append(assistant(f"x{k}", ctx, out))
    return [dump(r) for r in recs]


FIXTURES = {
    "anchors.jsonl": anchors_fixture,
    "compaction.jsonl": compaction_fixture,
    "synthetic.jsonl": synthetic_fixture,
    "young.jsonl": young_fixture,
    "poor.jsonl": poor_fixture,
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
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    for p in build(target):
        print(p)
