"""Write the ledger's transcript fixtures (ST-2 design §7). Rebuild with

    python3 tests/fixtures/ledger/build.py

Every transcript is SYNTHETIC, built from the record shapes Claude Code
writes (README Context; design §3; re-measured by do-001 on one Mac on
2030-12-08): fake session ids, no mail, no real transcript copied in.
The builder and its output are both committed; a test pins that a rebuild
gives the committed bytes.

Shapes this mirrors, measured, not guessed:

- A Read's `tool_result` is `"\\n".join(f"{start+i}\\t{line}")` over
  `toolUseResult.file.content.split("\\n")`; the content is the range's lines
  joined by newlines (16 of 16 partial reads, 68 of 68 full ones).
- `totalLines` is `len(text.split("\\n"))`: a newline-ended file of N lines
  has `totalLines` N+1, and the last Read of it ends with an empty
  `"<N+1>\\t"` (67 of 67 full reads). So POLICY's 2,526 lines are 2,527 here,
  as Claude Code would write them; the foreman's ruling (12:34) makes the
  reader report 2,526.
- A persisted Bash: the `tool_result` is the `<persisted-output>` preview
  (the first ~2KB, cut at a line end); `toolUseResult.stdout` is the first
  30,000 bytes; `persistedOutputSize` is the full size in bytes.
- A no-output Bash's `tool_result` is the placeholder
  `(Bash completed with no output)`, with `stdout` and `stderr` empty.
- A nested load's `attachment.content` is an object; the text is its `content`.

`@FIXTURES@` stands for this directory's absolute path on the host that runs
the test; the tests copy the files to a tmp dir and put the real path in.

The POLICY file is SYNTHETIC too (ST-144): `policy_text()` generates a
filler rulebook from a fixed word list and a seeded LCG, so it carries no
real rule, name, host, address or Message-ID. It keeps the shapes the gates
need: 2,526 lines, so the 1-1,000 / 1,001-2,000 / 2,001-end reads split, and
120,991 bytes, well over the 30,000 a persisted Bash keeps in `stdout`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AT = "@FIXTURES@"
POLICY = "POLICY-synthetic.md"
PARTIAL = "partial-40.txt"
EDITED = "edited.txt"
VERSION = "2.1.296"
CWD = "/Users/agent/work/demo"
#: budget-200's fake store root, CWD-style (ST-144): as long as the real root
#: it replaced (50 characters), so the paths and commands keep their lengths.
STORE = "/Users/agent/work/example-org/demo-task-store-root"
NO_OUTPUT = "(Bash completed with no output)"


class Session:
    """One synthetic transcript: records appended in order, uuids and tool
    ids counted, timestamps one second apart."""

    def __init__(self, n: int):
        self.n = n
        self.sid = f"00000000-0000-4000-8000-{n:012d}"
        self.recs: list[dict] = []
        self.k = 0
        self.parent: str | None = None

    def _uuid(self) -> str:
        self.k += 1
        return f"{self.n:08d}-0000-4000-9000-{self.k:012d}"

    def _ts(self) -> str:
        return f"2030-06-09T08:{self.k // 60:02d}:{self.k % 60:02d}.000Z"

    def _base(self, kind: str, **more) -> dict:
        u = self._uuid()
        rec = {"parentUuid": self.parent, "isSidechain": False, "type": kind, **more,
               "uuid": u, "timestamp": self._ts(), "userType": "external",
               "entrypoint": "cli", "cwd": CWD, "sessionId": self.sid,
               "version": VERSION, "gitBranch": "master"}
        self.parent = u
        self.recs.append(rec)
        return rec

    def prompt(self, text: str) -> None:
        self._base("user", message={"role": "user", "content": text})

    def attach(self, att: dict) -> dict:
        return self._base("attachment", attachment=att)

    def nested(self, path: str, text: str) -> dict:
        return self.attach({"type": "nested_memory", "path": path, "displayPath": path,
                            "content": {"path": path, "type": "Project", "content": text,
                                        "contentDiffersFromDisk": False}})

    def boot(self, files: list[tuple[str, str]]) -> None:
        self.attach({"type": "instructions",
                     "files": [{"path": p, "type": "Project", "content": c} for p, c in files]})

    def call(self, name: str, inp: dict, content, tur, is_error: bool | None = False,
             sidechain: bool = False) -> str:
        tid = f"toolu_{self.n:04d}{self.k + 1:06d}"
        a = self._base("assistant", message={
            "model": "claude-opus-5-5", "id": f"msg_{self.k:06d}", "type": "message",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp,
                         "caller": {"type": "direct"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100,
                      "cache_read_input_tokens": 1000, "output_tokens": 50}})
        block = {"tool_use_id": tid, "type": "tool_result", "content": content}
        if is_error is not None:
            block["is_error"] = is_error
        r = self._base("user", message={"role": "user", "content": [block]},
                       toolUseResult=tur, sourceToolAssistantUUID=a["uuid"])
        if sidechain:
            a["isSidechain"] = r["isSidechain"] = True
        return tid

    def read(self, path: str, text: str, offset: int | None = None,
             limit: int | None = None) -> None:
        segs = text.split("\n")
        start = offset or 1
        got = segs[start - 1:(start - 1 + limit) if limit else None]
        inp = {"file_path": path}
        if offset is not None:
            inp["offset"] = offset
        if limit is not None:
            inp["limit"] = limit
        shown = "\n".join(f"{start + i}\t{s}" for i, s in enumerate(got))
        self.call("Read", inp, shown, {"type": "text", "file": {
            "filePath": path, "content": "\n".join(got), "numLines": len(got),
            "startLine": start, "totalLines": len(segs)}}, is_error=None)

    def bash(self, cmd: str, out: str, desc: str = "run it") -> None:
        content = out.rstrip("\n") if out else NO_OUTPUT
        self.call("Bash", {"command": cmd, "description": desc}, content,
                  {"stdout": out.rstrip("\n"), "stderr": "", "interrupted": False,
                   "isImage": False, "noOutputExpected": False})

    def bash_persisted(self, cmd: str, out: str, saved: str) -> None:
        b = out.encode("utf-8")
        head = out[:2000]
        head = head[:head.rfind("\n")]
        preview = (f"<persisted-output>\nOutput too large ({len(b) / 1024:.1f}KB). "
                   f"Full output saved to: {saved}\n\nPreview (first 2KB):\n{head}\n...\n"
                   f"</persisted-output>")
        self.call("Bash", {"command": cmd, "description": "show it"}, preview,
                  {"stdout": b[:30000].decode("utf-8", "ignore"), "stderr": "",
                   "interrupted": False, "isImage": False, "noOutputExpected": False,
                   "persistedOutputPath": saved, "persistedOutputSize": len(b)})

    def jsonl(self) -> str:
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.recs)


TASK_CLAUDE = """# Task: TM-9

Ship the demo widget, then stop.

Your role protocol is injected via your agent definition at spawn. This file
carries task-specific context only.

## Notes

- The widget lives in `src/demo/widget.py`.
- Its tests are `tests/test_widget.py`; run `make test` before done.
- Never edit `src/demo/legacy.py`: it belongs to TM-8.
"""

STORE_CLAUDE = """# Claude Instructions

**Always load the `taskmaster` skill when working in this project.**

Read `POLICY.md`, the store's ratified working rules.

## Key Commands

```bash
tm task list -a 'all:word'
tm task worktree TM-123
```
"""

FRONT_MATTER_CLAUDE = """---
task_id: TM-10
title: A task whose CLAUDE.md starts with front matter
tags: [demo, front-matter]
---

# Task: TM-10

The counter reads this raw, front matter included (OQ3).
"""


#: The synthetic POLICY's vocabulary: shop-floor nouns and glue words, nothing real.
POLICY_WORDS = (
    "widget", "gear", "lever", "spring", "bolt", "panel", "crate", "ledger", "shelf",
    "order", "batch", "queue", "drawer", "label", "ticket", "bench", "spool", "valve",
    "the", "a", "each", "every", "no", "one", "two", "its", "this", "that", "their",
    "is", "stays", "goes", "waits", "moves", "keeps", "reads", "writes", "checks",
    "names", "counts", "holds", "opens", "closes", "marks", "sends", "takes",
    "before", "after", "until", "unless", "when", "while", "where", "and", "or",
    "but", "so", "then", "first", "last", "never", "always", "only", "again",
    "quiet", "plain", "small", "whole", "late", "early", "spare", "loose", "tidy",
    "in", "on", "by", "for", "with", "from", "to", "of", "at", "under", "over")
POLICY_TITLES = ("Shelves", "Orders", "Batches", "Queues", "Drawers", "Labels", "Tickets",
                 "Benches", "Spools", "Valves", "Crates", "Panels", "Levers", "Springs")


def policy_text() -> str:
    """The synthetic POLICY (ST-144): 144 numbered rules of filler prose and
    bullets, wrapped under 77 columns, newline-ended. Deterministic: a seeded
    LCG picks the words, so every host builds the same bytes. Each bullet
    carries an em dash, so its bytes outnumber its characters as real prose's do."""
    state = 0x1352896

    def rnd(n: int) -> int:
        nonlocal state
        state = (state * 1103515245 + 12345) % 2**31
        return (state >> 8) % n

    def sentence() -> str:
        words = [POLICY_WORDS[rnd(len(POLICY_WORDS))] for _ in range(6 + rnd(12))]
        return words[0].capitalize() + " " + " ".join(words[1:]) + "."

    def wrap(text: str, width: int = 76) -> list[str]:
        lines, cur = [], ""
        for w in text.split():
            if cur and len(cur) + 1 + len(w) > width:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}" if cur else w
        return [*lines, cur]

    out = ["# Working rules for the demo widget shop", "",
           "A synthetic rulebook: generated, not written. Every rule is filler.", ""]
    for k in range(1, 145):
        out += [f"## {k}. {POLICY_TITLES[k % len(POLICY_TITLES)]} rule {k}", ""]
        for _ in range(1 + rnd(2)):
            out += [*wrap(" ".join(sentence() for _ in range(2 + rnd(4)))), ""]
        for _ in range(2 + rnd(4)):
            out += [f"- {line}" if i == 0 else f"  {line}"
                    for i, line in enumerate(wrap(sentence() + " \u2014 " + sentence(), 74))]
        out.append("")
    return "\n".join(out)


def edited_text(version: int) -> str:
    """A design note before (1: 10 lines) and after (2: 14 lines) an edit.
    The file on disk is version 2, as a seat that edited it leaves it."""
    lines = [f"## {k}. Section {k}" if k % 5 == 1 else f"Line {k} of version {version}."
             for k in range(1, 11 if version == 1 else 15)]
    return "\n".join(lines) + "\n"


def partial_text() -> str:
    return "".join(f"{k:02d} the quick brown fox jumps over the lazy dog, line {k} of 40\n"
                   for k in range(1, 41))


def build() -> dict[str, str]:
    policy = policy_text()
    ppath = f"{AT}/{POLICY}"
    part = partial_text()
    qpath = f"{AT}/{PARTIAL}"
    out: dict[str, str] = {}

    # The nested load counted twice: stands in for sessionmail engineer 03c563c5.
    s = Session(1)
    s.prompt("Work the task.")
    tpath = "/Users/agent/store/projects/demo/TM-9_feat_widget/CLAUDE.md"
    s.nested(tpath, TASK_CLAUDE)
    s.bash("ls src/demo", "legacy.py\nwidget.py\n")
    s.nested(tpath, TASK_CLAUDE)
    out["nested-twice.jsonl"] = s.jsonl()

    # POLICY (a): one cat of it, persisted.
    s = Session(2)
    s.prompt("Read the policy.")
    s.bash_persisted(f"cat {ppath}", policy, ppath)
    out["policy-cat.jsonl"] = s.jsonl()

    # POLICY (b): Reads in ranges that together cover every line, no overlap.
    s = Session(3)
    s.prompt("Read the policy, all of it.")
    s.read(ppath, policy, offset=1, limit=1000)
    s.read(ppath, policy, offset=1001, limit=1000)
    s.read(ppath, policy, offset=2001, limit=1000)   # 2001-2527: Claude Code's phantom line
    out["policy-reads.jsonl"] = s.jsonl()

    # A partial Read (offset/limit) of a 40-line file; the second Read reaches
    # the end, which is the end-of-file evidence for "lines in the file = 40".
    s = Session(4)
    s.prompt("Look at lines 11-20.")
    s.read(qpath, part, offset=11, limit=10)
    s.read(qpath, part, offset=21, limit=100)
    out["partial-read.jsonl"] = s.jsonl()

    # Two Reads that together cover the file: two rows, each its own range.
    s = Session(5)
    s.prompt("Read it in two halves.")
    s.read(qpath, part, offset=1, limit=20)
    s.read(qpath, part, offset=21)   # "the rest": no limit, so it reaches the end
    out["partial-cover.jsonl"] = s.jsonl()

    # The lone partial Read: no end-of-file evidence, so totalLines (41) is
    # carried as given and flagged inexact (the foreman's ruling, 12:34).
    s = Session(6)
    s.prompt("Just lines 11-20.")
    s.read(qpath, part, offset=11, limit=10)
    out["partial-noeof.jsonl"] = s.jsonl()

    # Boot memory: one instructions attachment carrying two files.
    s = Session(7)
    s.boot([("/Users/agent/store/CLAUDE.md", STORE_CLAUDE), (tpath, TASK_CLAUDE)])
    s.prompt("Go.")
    out["boot.jsonl"] = s.jsonl()

    # Bash: output not the command; no output; persisted.
    s = Session(8)
    s.prompt("Run three commands.")
    s.bash("seq 1 5000", "".join(f"{k}\n" for k in range(1, 5001)))
    s.bash("true", "")
    s.bash_persisted(f"cat {ppath}", policy, ppath)
    out["bash.jsonl"] = s.jsonl()

    # Another tool: one Grep.
    s = Session(9)
    s.prompt("Find scan.")
    s.call("Grep", {"pattern": "def scan", "path": "src", "output_mode": "files_with_matches"},
           "Found 1 file\nsrc/sessiontools/window.py",
           {"mode": "files_with_matches", "filenames": ["src/sessiontools/window.py"],
            "numFiles": 1})
    out["grep.jsonl"] = s.jsonl()

    # Edges outside the gates (design §7): a Skill and its meta body, an
    # image Read, <|endoftext|>, a sidechain call, a bad line, a duplicate
    # attachment, a front-matter load, a background Bash, an error result.
    s = Session(10)
    s.prompt("Edges.")
    tid = s.call("Skill", {"skill": "demo:widget"}, "Launching skill: demo:widget",
                 {"success": True, "commandName": "demo:widget"}, is_error=None)
    s._base("user", message={"role": "user", "content": [
        {"type": "text", "text": "Base directory for this skill: /x/demo\n\n# Widget skill\n\nBuild widgets."}]},
        isMeta=True, sourceToolUseID=tid)
    s.call("Read", {"file_path": "/Users/agent/work/demo/shot.png"},
           [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "iVBORw0KGgo="}}],
           {"type": "image", "file": {"base64": "iVBORw0KGgo=", "type": "image/png",
                                      "originalSize": 8}}, is_error=None)
    s.bash("printf '<|endoftext|>'", "<|endoftext|>")
    s.call("Bash", {"command": "cat /etc/hosts", "description": "sub"}, "127.0.0.1 localhost",
           {"stdout": "127.0.0.1 localhost", "stderr": ""}, sidechain=True)
    dup = s.nested("/Users/agent/work/demo/fm/CLAUDE.md", FRONT_MATTER_CLAUDE)
    s.recs.append(dict(dup))   # the same attachment uuid twice: kept once
    s.call("Bash", {"command": "sleep 600", "run_in_background": True},
           "Command running in background with ID: b1x2y3z4.",
           {"stdout": "", "stderr": "", "interrupted": False, "isImage": False,
            "noOutputExpected": False, "backgroundTaskId": "b1x2y3z4"})
    s.call("Bash", {"command": "false", "description": "fail"}, "Error: Exit code 1",
           "Error: Exit code 1", is_error=True)
    out["edge.jsonl"] = s.jsonl().replace("\n", "\n{not json\n", 1)

    # 200 items for the short report's budget (do-002's gate): long paths,
    # colliding base names, long commands, each at least as long on average
    # as design evidence budget.py's real sample (its base names 10.0 chars,
    # paths 116.4, commands 451.9, re-measured by do-002 on
    # 2030-12-08; the gate test asserts the floor). Figures are 4-digit: in
    # cl100k_base a 4-digit and a 5-digit number are both 2 tokens, so this
    # costs the short report what 5-digit sizes would, at a tenth of the bytes.
    s = Session(11)
    s.prompt("A heavy session.")
    projects = ["sessionmail", "sessiontools", "taskmaster", "sessionvoice", "comms"]
    names = ["CLAUDE.md", "README.md", "design.md", "progress.json", "window.py",
             "transcript.py", "features.json"]
    body = " ".join(str(k % 10) for k in range(520))   # ~1,040 tokens: 1 a digit, 1 a space
    for i in range(200):
        proj = projects[i % len(projects)]
        root = (f"{STORE}/projects/{proj}/"
                f"tasks/{proj[:2].upper()}-{100 + i % 37}_feat_some-longer-task-slug")
        stem = names[i % 7].split(".")[0]
        if i % 3 == 2:
            s.bash([
                (f"cd {root}/worktree && uv run pytest -q tests/test_{stem}.py "
                 f"-k 'not slow and not network' --maxfail=1 2>&1 | tail -n 40; "
                 f'echo "exit $?"; git -C {root}/worktree status --short --branch; '
                 f"git -C {root}/worktree log --oneline -5 --decorate"),
                (f"git -C {root}/worktree diff --stat master...HEAD -- src tests && "
                 f"git -C {root}/worktree log --format='%h %an %s' -10 -- src/{proj}; "
                 f"ls -la {root}/worktree/src/{proj} {root}/worktree/tests | head -n 60"),
                (f"tm slice show do-{i % 9:03d} --task {proj[:2].upper()}-{100 + i % 37} "
                 f"--format text 2>&1 | sed -n '1,80p'; cat {root}/harness/progress.json | "
                 f"python3 -m json.tool | tail -n 40; wc -l {root}/README.md {root}/design.md"),
            ][i % 9 // 3], body + "\n")
        elif i % 3 == 1:
            s.nested(f"{root}/{names[i % 7]}", body)
        else:
            s.read(f"{root}/worktree/src/{proj}/{names[i % 7]}", body)
    out["budget-200.jsonl"] = s.jsonl()

    # A file edited between reads (do-001 review N1, a real transcript ae66ce7f):
    # a full read of version 1 (totalLines 11), then a partial read of
    # version 2 (totalLines 15). The second row must not pair version 2's
    # line count with version 1's tokens; the disk holds version 2.
    s = Session(12)
    s.prompt("Read the note, edit it, look again.")
    epath = f"{AT}/{EDITED}"
    s.read(epath, edited_text(1))
    s.read(epath, edited_text(2), offset=3, limit=4)
    out["edited.jsonl"] = s.jsonl()
    return out


def main(argv: list[str]) -> int:
    for name, text in build().items():
        (HERE / name).write_text(text, encoding="utf-8")
    (HERE / POLICY).write_text(policy_text(), encoding="utf-8")
    (HERE / PARTIAL).write_text(partial_text(), encoding="utf-8")
    (HERE / EDITED).write_text(edited_text(2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
