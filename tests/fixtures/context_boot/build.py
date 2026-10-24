#!/usr/bin/env python3
"""Build the context boot fixtures (ST-26 do-002): synthetic Claude Code
transcripts for `--startup` and `--boot`, one per SETTLED signal.

    python tests/fixtures/context_boot/build.py            # rewrite them here
    python tests/fixtures/context_boot/build.py OUTDIR     # write them elsewhere

Deterministic: tests/test_context_parts_boot.py rebuilds them and compares.
The record helpers are context_parts/build.py's (loaded by path, not copied),
and the pointer brief is written by the launcher's own
`sessiontools.start.context.first_message_pointer`, so its shape is pinned.

- startup.jsonl        every boot kind before the first call, and a SessionStart
                       hook line: rows, rates and the core remainder.
- agent_pointer.jsonl  a pointer brief, its read, a stray plain user turn (the
                       old context-boot fencepost), then a Skill call: settles
                       on the first tool call after the brief's own reads.
- agent_mail.jsonl     a `message read` BEFORE the boot readout (ignored), the
                       readout (`memory wake`), then `message read`: mail-read.
- agent_claim.jsonl    `tm slice claim` before any mail read: slice-claim.
- agent_unsettled.jsonl a pointer, its read, then only text: not settled.
- agent_held.jsonl     the brief's read and a mail read in ONE batch: SETTLED is
                       held to the first call that has seen the brief.
- human.jsonl          no agent identity; slash-command records, a reminder,
                       a recorded `/context` run, then typed input.
- human_voice.jsonl    the first input is spoken (a `Voice response:` result).
- human_queued.jsonl   the first input is queued while a call runs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from sessiontools.start.context import first_message_pointer

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "context_parts_fixture_build", HERE.parent / "context_parts" / "build.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

assistant, attachment, dump = base.assistant, base.attachment, base.dump
tool_use, tool_result, user_text, hook_pair = (base.tool_use, base.tool_result,
                                               base.user_text, base.hook_pair)

SESSION = "0b007000-0000-4000-8000-000000000026"
POINTER_FILE = f"/srv/seat/.session/sessions/{SESSION}/launch/first-message-26.md"
BRIEF_TEXT = "Your assignment: implement feature do-002. " + "b" * 2957   # 3000 chars
POINTER = first_message_pointer(POINTER_FILE, len(BRIEF_TEXT))
CAT = f"cat {POINTER_FILE}"

#: The recorded /context run human.jsonl carries (its markdown form).
CONTEXT_REPORT = """## Context Usage

**Model:** claude-opus-5-5
**Tokens:** 19.5k / 1m (2%)

### Estimated usage by category

| Category | Tokens | Percentage |
|----------|--------|------------|
| System prompt | 2.1k | 0.2% |
| System tools | 11.9k | 1.2% |
| System tools (deferred) | 19.9k | 2.0% |
| MCP tools | 1,250 | 0.1% |
| Custom agents | 54 | 0.0% |
| Memory files | 3.1k | 0.3% |
| Skills | 2.3k | 0.2% |
| Messages | 1.2k | 0.1% |
| Free space | 947.5k | 94.8% |
| Autocompact buffer | 33k | 3.3% |

### Custom Agents

| Agent Type | Source | Tokens |
|------------|--------|--------|
| worker | user | 54 |
"""
CONTEXT_AT = "2030-06-09T06:00:00.000Z"


def agent_name(name: str) -> dict:
    return {"type": "agent-name", "agentName": name, "sessionId": SESSION}


def agent_setting(setting: str) -> dict:
    return {"type": "agent-setting", "agentSetting": setting, "sessionId": SESSION}


def boot(brief: str) -> list[dict]:
    """The boot kinds a real seat's first call reads, then the brief."""
    return [
        attachment(type="instructions", files=[
            {"path": "/store/CLAUDE.md", "type": "Project", "content": "x" * 2000},
            {"path": "/store/task/CLAUDE.md", "type": "Project", "content": "y" * 600}]),
        attachment(type="session_context", context={"userEmail": "e" * 200,
                                                     "gitStatus": "g" * 400}),
        attachment(type="date", date="2030-12-08"),
        attachment(type="prompt_snapshot", systemPrompt=["P" * 9000]),
        user_text(brief),
        attachment(type="environment", snapshot={"workingDirectory": "/w",
                                                 "platform": "darwin"}),
        attachment(type="model", text="m" * 120),
        attachment(type="skill_listing", content="S" * 1200),
        attachment(type="agent_listing_delta", agents=["worker", "foreman"]),
        attachment(type="mcp_instructions_delta", instructions="i" * 300),
        attachment(type="deferred_tools_delta", tools=["Monitor", "TaskCreate"]),
        attachment(type="command_permissions", allowed=["Bash(git:*)"]),
        attachment(type="total_tokens_reminder", text="<total_tokens>15000000</total_tokens>"),
    ]


def startup_fixture() -> list[str]:
    recs = [agent_name("sessiontools+st-26.worker.cafe0026")]
    recs += boot("Implement do-002.")
    recs += hook_pair("ss1", "memory-wake-hook", "w" * 400)
    recs += [assistant("s0", 30000, 120, [tool_use("st0", "Bash", command="ls")]),
             tool_result("st0", "f" * 300),
             assistant("s1", 30500, 40)]
    return [dump(r) for r in recs]


def agent_pointer_fixture() -> list[str]:
    """calls 20000 / 21100 / 21800 / 22300. The brief's read (call 0) enters
    call 1; a stray plain user turn arrives after call 0 (where the old
    verb fenced); call 1's Skill is the first act: settled = call 1."""
    recs = [agent_name("sessiontools+st-26.worker.cafe0026")]
    recs += boot(POINTER)
    recs += [
        assistant("p0", 20000, 50, [tool_use("pt0", "Bash", command=CAT,
                                             description="Read the brief")]),
        tool_result("pt0", BRIEF_TEXT),
        user_text("are you there?"),
        *hook_pair("ph0", "clock-hook", "[clock] 12:50"),
        assistant("p1", 21100, 80, [tool_use("pt1", "Skill", skill="taskmaster")]),
        tool_result("pt1", "k" * 2000),
        assistant("p2", 21800, 60, [tool_use("pt2", "Read", file_path="/w/design.md")]),
        tool_result("pt2", "d" * 1500),
        assistant("p3", 22300, 30),
    ]
    return [dump(r) for r in recs]


def agent_mail_fixture() -> list[str]:
    """calls 20000 .. 24000: call 1 runs `message read` before the readout
    (ignored), call 2 `memory wake`, call 3 `message list`, call 4
    `message read 1`: settled = call 4, mail-read."""
    recs = [agent_setting("session-agent-worker:worker")]
    recs += boot(POINTER)
    steps = [("Bash", {"command": CAT}), ("Bash", {"command": "message read 9"}),
             ("Bash", {"command": "memory wake"}),
             ("Bash", {"command": "message list --unread"}),
             ("Bash", {"command": "message read 1"}),
             ("Bash", {"command": "tm slice claim do-002"})]
    for i, (name, tool_input) in enumerate(steps):
        recs.append(assistant(f"msg{i}", 20000 + 1000 * i, 40,
                              [tool_use(f"mt{i}", name, **tool_input)]))
        recs.append(tool_result(f"mt{i}", BRIEF_TEXT if i == 0 else "o" * 500))
    recs.append(assistant("m9", 27000, 10))
    return [dump(r) for r in recs]


def agent_claim_fixture() -> list[str]:
    """call 3 claims a slice, call 4 reads mail (no readout): slice-claim
    comes first, settled = call 3."""
    recs = [agent_name("sessiontools+st-26.worker.cafe0026")]
    recs += boot(POINTER)
    steps = [("Bash", {"command": CAT}), ("Skill", {"skill": "taskmaster"}),
             ("Read", {"file_path": "/w/README.md"}),
             ("Bash", {"command": "cd /w && tm slice claim do-002"}),
             ("Bash", {"command": "message read 1"})]
    for i, (name, tool_input) in enumerate(steps):
        recs.append(assistant(f"c{i}", 20000 + 900 * i, 30,
                              [tool_use(f"ct{i}", name, **tool_input)]))
        recs.append(tool_result(f"ct{i}", BRIEF_TEXT if i == 0 else "o" * 400))
    recs.append(assistant("c9", 25000, 10))
    return [dump(r) for r in recs]


def agent_unsettled_fixture() -> list[str]:
    recs = [agent_name("sessiontools+st-26.worker.cafe0026")]
    recs += boot(POINTER)
    recs += [assistant("u0", 20000, 50, [tool_use("ut0", "Bash", command=CAT)]),
             tool_result("ut0", BRIEF_TEXT),
             assistant("u1", 20900, 70)]
    return [dump(r) for r in recs]


def human_fixture() -> list[str]:
    """No agent identity. calls 18000 / 18600 / 19300 / 21000; the typed
    'now fix the bug' arrives after call 2: settled = call 2. Between calls 1
    and 2, none of them typed: a slash command's caveat (isMeta) and stdout,
    a recorded /context run (isMeta), and a reminder."""
    meta = {"type": "user", "isMeta": True, "timestamp": CONTEXT_AT,
            "message": {"role": "user", "content": CONTEXT_REPORT}}
    quoted = tool_result("ht1", "Quoting a capture: " + CONTEXT_REPORT)
    recs = boot("hi there")
    recs += [
        assistant("h0", 18000, 90, [tool_use("ht0", "Read", file_path="/w/notes.md")]),
        tool_result("ht0", "n" * 900),
        assistant("h1", 18600, 70, [tool_use("ht1", "Bash", command="cat capture.txt")]),
        quoted,
        {"type": "user", "isMeta": True, "message": {"role": "user", "content":
            "<local-command-caveat>Caveat: the messages below were generated by the "
            "user while running local commands.</local-command-caveat>"}},
        meta,
        user_text("<local-command-stdout>Context Usage ...</local-command-stdout>"),
        user_text("<system-reminder>a reminder</system-reminder>"),
        assistant("h2", 19300, 60),
        user_text("now fix the bug"),
        assistant("h3", 21000, 200),
    ]
    return [dump(r) for r in recs]


def agent_held_fixture() -> list[str]:
    """Call 0 reads the pointer's file AND `message read 1` in one batch:
    mail-read fires at call 0, before any call has seen the brief, so
    SETTLED is held to call 1, the first call that has."""
    recs = [agent_name("sessiontools+st-26.worker.cafe0026")]
    recs += boot(POINTER)
    recs += [
        assistant("e0", 20000, 50, [tool_use("et0", "Read", file_path=POINTER_FILE),
                                    tool_use("et1", "Bash", command="message read 1")]),
        tool_result("et0", BRIEF_TEXT),
        tool_result("et1", "o" * 700),
        assistant("e1", 21200, 40, [tool_use("et2", "Bash", command="make test")]),
        tool_result("et2", "t" * 300),
        assistant("e2", 21600, 20),
    ]
    return [dump(r) for r in recs]


def human_voice_fixture() -> list[str]:
    """No agent identity: the first typed input is SPOKEN (a voice tool's
    'Voice response:' result) after call 0, so settled = call 0."""
    recs = boot("let's talk")
    recs += [assistant("v0", 17000, 30, [tool_use("vt0", "converse", message="Hi")]),
             tool_result("vt0", "Voice response: fix the build please"),
             assistant("v1", 17400, 30)]
    return [dump(r) for r in recs]


def human_queued_fixture() -> list[str]:
    """No agent identity: the first typed input is QUEUED while call 1 ran
    (a queued_command, commandMode prompt), so settled = call 1."""
    recs = boot("hi")
    recs += [assistant("q0", 17000, 30, [tool_use("qt0", "Bash", command="ls")]),
             tool_result("qt0", "l" * 100),
             assistant("q1", 17200, 30, [tool_use("qt1", "Bash", command="make")]),
             attachment(type="queued_command", prompt="also run lint", commandMode="prompt"),
             tool_result("qt1", "m" * 100),
             assistant("q2", 17500, 30)]
    return [dump(r) for r in recs]


FIXTURES = {
    "startup.jsonl": startup_fixture,
    "agent_pointer.jsonl": agent_pointer_fixture,
    "agent_mail.jsonl": agent_mail_fixture,
    "agent_claim.jsonl": agent_claim_fixture,
    "agent_unsettled.jsonl": agent_unsettled_fixture,
    "agent_held.jsonl": agent_held_fixture,
    "human.jsonl": human_fixture,
    "human_voice.jsonl": human_voice_fixture,
    "human_queued.jsonl": human_queued_fixture,
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
