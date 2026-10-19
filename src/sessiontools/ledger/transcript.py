"""The ledger's transcript reader: records in, raw items out, NO counting.

The ledger's own reader. It does not import, extend or wrap
`window.scan`, and nothing in settle, `session list` or `session context`
calls it: it runs only when someone asks for a ledger.

One pass, in transcript order. An unparseable line is skipped (as
`window.scan` skips it), and so is any `isSidechain: true` record. A raw
item is one of:

    kind     source record                                      key
    Read     tool_result joined to a Read tool_use               input.file_path
    nested   attachment.type == "nested_memory"                  attachment.path
    boot     attachment.type == "instructions", per files[] entry  files[i].path
    Bash     tool_result joined to a Bash tool_use               input.command
    tool     tool_result joined to any other tool_use            "<Tool>: <main input>"

WHAT IS READ IS WHAT ENTERED THE WINDOW: the `tool_result` block in
`message.content`, never `toolUseResult`'s copy (README Requirements). A
Read's text keeps its `N\\t` prefixes, a persisted Bash its preview.
`toolUseResult` gives only the figures ABOUT the item (`startLine`,
`numLines`, `totalLines`, `persistedOutputSize`, the full read's raw
`file.content`), and `toolUseResult.stdout` is never used: it is capped at
30,000 bytes.

Paths are `os.path.normpath`ed, never resolved through symlinks (a stated
limit).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

#: The kinds a file is read by. Bash and other tools are not file kinds.
FILE_KINDS = ("Read", "nested", "boot")

#: The main input of a tool that is neither Read nor Bash, for its key.
#: Any other tool uses the first string value in its input.
MAIN_INPUT = {
    "Grep": "pattern", "Glob": "pattern",
    "Edit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path",
    "WebFetch": "url", "WebSearch": "query", "Skill": "skill", "Agent": "description",
}


@dataclass
class Raw:
    """One item as the transcript holds it, before any counting.

    `texts` are the pieces that entered the window (summed when counted);
    None means they cannot be counted (an image block), never zero.
    """

    pos: int                       # the source record's line number in the transcript
    kind: str
    key: str
    ts: str | None = None
    is_error: bool = False
    texts: list[str] | None = field(default_factory=list)
    cmd: str | None = None                 # Bash: the command, counted apart
    start_line: int | None = None          # Read: toolUseResult.file.startLine
    num_lines: int | None = None           # Read: toolUseResult.file.numLines
    total_lines: int | None = None         # Read: toolUseResult.file.totalLines
    raw_content: str | None = None         # Read: toolUseResult.file.content; a load's own text
    persisted_path: str | None = None      # Bash: persistedOutputPath
    persisted_bytes: int | None = None     # Bash: persistedOutputSize
    notes: list[str] = field(default_factory=list)
    tool_use_id: str | None = None


@dataclass
class Transcript:
    session: str
    raws: list[Raw]


def norm(path: str) -> str:
    return os.path.normpath(path) if path else path


def line_count(text: str) -> int:
    """The file's own line count, as `wc -l` gives it for a newline-ended
    file: a final line with no newline still counts."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def read(path: Path) -> Transcript:
    """Every raw item in the transcript at PATH, in transcript order."""
    uses: dict[str, dict] = {}
    raws: list[Raw] = []
    by_use: dict[str, Raw] = {}
    companions: dict[str, list[str]] = {}
    seen_atts: set[str] = set()
    session = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for pos, line in enumerate(f, 1):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain") is True:
                continue
            if not session and isinstance(rec.get("sessionId"), str):
                session = rec["sessionId"]
            kind = rec.get("type")
            if kind == "assistant":
                for b in _blocks(rec):
                    if b.get("type") == "tool_use" and isinstance(b.get("id"), str):
                        uses.setdefault(b["id"], b)
            elif kind == "user":
                _user(rec, pos, uses, raws, by_use, companions)
            elif kind == "attachment":
                uuid = rec.get("uuid")
                if isinstance(uuid, str):
                    if uuid in seen_atts:
                        continue
                    seen_atts.add(uuid)
                raws.extend(_attachment(rec, pos))
    for tid, texts in companions.items():   # a companion may come before its result
        r = by_use.get(tid)
        if r is not None and r.texts is not None:
            r.texts.extend(texts)
    return Transcript(session=session or path.stem, raws=raws)


def _blocks(rec: dict) -> list[dict]:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _user(rec: dict, pos: int, uses: dict, raws: list, by_use: dict, companions: dict) -> None:
    src = rec.get("sourceToolUseID")
    if rec.get("isMeta") is True and isinstance(src, str):
        # A Skill's body (and any tool's meta companion) is what the call
        # put in the window, though not through its tool_result.
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        text = _text_of(content)
        if text is not None:
            companions.setdefault(src, []).extend(text)
        return
    results = [b for b in _blocks(rec) if b.get("type") == "tool_result"]
    # toolUseResult belongs to the record; with two results in one record
    # it is not known whose it is, so neither takes it.
    tur = rec.get("toolUseResult") if len(results) == 1 else None
    tur = tur if isinstance(tur, dict) else {}
    for b in results:
        tid = b.get("tool_use_id")
        if not isinstance(tid, str) or tid in by_use:
            continue
        use = uses.get(tid, {})
        r = _result(use, b, tur, pos, rec.get("timestamp"))
        r.tool_use_id = tid
        by_use[tid] = r
        raws.append(r)


def _text_of(content) -> list[str] | None:
    """The text pieces of a tool_result's (or a message's) content: a string,
    or a list of blocks whose text blocks are each a piece. Any block with
    no text to count (an image) makes the whole unknown: None."""
    if content is None:
        return [""]
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                out.append(b["text"])
            elif isinstance(b, str):
                out.append(b)
            else:
                return None
        return out
    return None


def _unknown_note(content) -> str:
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") != "text":
                return str(b.get("type") or "unknown block")
    return "unknown content"


def _result(use: dict, block: dict, tur: dict, pos: int, ts) -> Raw:
    name = use.get("name") if isinstance(use.get("name"), str) else "?"
    inp = use.get("input") if isinstance(use.get("input"), dict) else {}
    texts = _text_of(block.get("content"))
    r = Raw(pos=pos, kind="tool", key="", ts=ts if isinstance(ts, str) else None,
            is_error=block.get("is_error") is True, texts=texts)
    if texts is None:
        r.notes.append(_unknown_note(block.get("content")))
    if name == "Read":
        r.kind = "Read"
        f = tur.get("file") if isinstance(tur.get("file"), dict) else {}
        path = inp.get("file_path") if isinstance(inp.get("file_path"), str) else f.get("filePath")
        r.key = norm(path) if isinstance(path, str) else "?"
        if tur.get("type") == "text" and _ints(f, "startLine", "numLines"):
            r.start_line, r.num_lines = f["startLine"], f["numLines"]
            if type(f.get("totalLines")) is int:
                r.total_lines = f["totalLines"]
            if isinstance(f.get("content"), str):
                r.raw_content = f["content"]
    elif name == "Bash":
        r.kind = "Bash"
        cmd = inp.get("command") if isinstance(inp.get("command"), str) else ""
        r.key = r.cmd = cmd
        if isinstance(tur.get("persistedOutputPath"), str):
            r.persisted_path = tur["persistedOutputPath"]
            size = tur.get("persistedOutputSize")
            r.persisted_bytes = size if type(size) is int else None
            r.notes.append("persisted")
        elif tur.get("backgroundTaskId"):
            r.notes.append("background")
        elif tur.get("stdout") == "" and tur.get("stderr") == "":
            # Not an empty tool_result: Claude Code writes a placeholder,
            # and that placeholder is what is counted.
            r.notes.append("no output")
    else:
        r.key = f"{name}: {_main_input(name, inp)}" if _main_input(name, inp) else name
    return r


def _ints(d: dict, *keys: str) -> bool:
    return all(type(d.get(k)) is int for k in keys)


def _main_input(name: str, inp: dict) -> str:
    want = MAIN_INPUT.get(name)
    if want and isinstance(inp.get(want), str):
        return inp[want]
    for v in inp.values():
        if isinstance(v, str):
            return v
    return ""


def _attachment(rec: dict, pos: int) -> list[Raw]:
    att = rec.get("attachment")
    if not isinstance(att, dict):
        return []
    ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
    if att.get("type") == "nested_memory" and isinstance(att.get("path"), str):
        # Its content is an OBJECT, {path, type, content, ...}; the text
        # loaded is content.content (README Context).
        c = att.get("content")
        text = c.get("content") if isinstance(c, dict) else c
        if not isinstance(text, str):
            return []
        return [_load("nested", att["path"], text, pos, ts)]
    if att.get("type") == "instructions" and isinstance(att.get("files"), list):
        return [_load("boot", f["path"], f["content"], pos, ts) for f in att["files"]
                if isinstance(f, dict) and isinstance(f.get("path"), str)
                and isinstance(f.get("content"), str)]
    return []


def _load(kind: str, path: str, text: str, pos: int, ts: str | None) -> Raw:
    """A nested or boot load: the whole file entered the window."""
    n = line_count(text)
    return Raw(pos=pos, kind=kind, key=norm(path), ts=ts, texts=[text], start_line=1,
               num_lines=n, total_lines=n, raw_content=text)
