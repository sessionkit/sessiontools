"""The fold: one read-only pass over a transcript, giving per API call
`(ctx, output_tokens, entered)`, built as a NEW module: `window.scan` and `settle` are not
touched and never call this.

A CALL is one main-chain assistant message id with a usage row and a real
model (never `<synthetic>`). Claude Code writes one record per content
block, all carrying the same id; the first record's input sum is the call's
`ctx`, and its `output_tokens` is the largest any of them reports.

`entered` is every user and attachment record written since the previous
call, as `Entry(category, key, chars)`: what came into the window for THIS
call to read. Call 0's entries are the boot; call k+1's entries, plus call
k's own output, are what grew the window over interval k.

The categories are the ones LABELS names below. A record kind that carries no context
of its own (`hook_success`: its text is the `hook_additional_context` row;
`prompt_snapshot`: the system prompt, inside the core remainder;
`deferred_tools_record`: bookkeeping) is not an entry.

A line the fold cannot read is SKIPPED, as `window.scan` skips it: one bad
line never costs a session its reading.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

#: Claude Code's `message.model` for messages it made itself.
SYNTHETIC_MODEL = "<synthetic>"

# ---------- the categories --------------------------------------------------
# Boot rows: counted at boot, at a fixed rate per kind.
MEMORY = "memory"            # instructions files[], nested_memory
SKILLS = "skills"            # skill_listing, agent_listing_delta
MCP = "mcp"                  # mcp_instructions_delta, deferred_tools_delta, command_permissions
ENVIRONMENT = "environment"  # environment, session_context, model, date
BRIEF = "brief"              # the first user record
# Since-boot rows: fitted per session.
RESULT = "result"            # tool_result, joined to its tool_use
HOOK = "hook"                # hook_additional_context, joined to its command
QUEUED = "queued"            # queued_command
REMINDER = "reminder"        # total_tokens_reminder, silent_turn_reminder
USER = "user"                # user text records
OTHER = "other"              # every other attachment

BOOT_CATEGORIES = (MEMORY, SKILLS, MCP, ENVIRONMENT, BRIEF)
FIT_CATEGORIES = (RESULT, HOOK, QUEUED, REMINDER, USER, OTHER)
CATEGORIES = BOOT_CATEGORIES + FIT_CATEGORIES

#: Each category in words.
LABELS: dict[str, str] = {
    MEMORY: "Memory files",
    SKILLS: "Skills index / agents index",
    MCP: "MCP instructions, deferred tools, permissions",
    ENVIRONMENT: "Environment and session facts",
    BRIEF: "The brief",
    RESULT: "Tool results",
    HOOK: "Hook context",
    QUEUED: "Queued notifications",
    REMINDER: "Harness reminders",
    USER: "User / mail text",
    OTHER: "Other attachments",
}

_ATTACHMENT_CATEGORY: dict[str, str] = {
    "instructions": MEMORY,
    "nested_memory": MEMORY,
    "skill_listing": SKILLS,
    "agent_listing_delta": SKILLS,
    "mcp_instructions_delta": MCP,
    "deferred_tools_delta": MCP,
    "command_permissions": MCP,
    "environment": ENVIRONMENT,
    "session_context": ENVIRONMENT,
    "model": ENVIRONMENT,
    "date": ENVIRONMENT,
    "queued_command": QUEUED,
    "total_tokens_reminder": REMINDER,
    "silent_turn_reminder": REMINDER,
}

#: Attachment kinds that put nothing in the window of their own.
NOT_CONTEXT = frozenset({"hook_success", "prompt_snapshot", "deferred_tools_record"})

#: Tool-input fields that name what a call touched, in order of preference.
_KEY_FIELDS = ("file_path", "path", "notebook_path", "command", "url", "pattern",
               "query", "skill", "prompt")
_KEY_DETAIL_MAX = 160


class Entry(NamedTuple):
    """One thing that entered the window: its category, a key naming it (the
    tool and its path or command for a result, the hook command for hook
    context, the attachment kind otherwise) and its size in chars."""

    category: str
    key: str
    chars: int


@dataclass(frozen=True)
class Call:
    """One API call. `ctx` is the window it read (input + cache creation +
    cache read, exact); `output_tokens` its own output (exact); `entered` what
    arrived since the previous call."""

    ctx: int
    output_tokens: int
    entered: tuple[Entry, ...] = ()
    message_id: str | None = None


@dataclass(frozen=True)
class Fold:
    """The transcript's calls in order, and the last real model id."""

    calls: tuple[Call, ...] = ()
    model: str | None = None

    @property
    def boot_entered(self) -> tuple[Entry, ...]:
        """What was in the window before the first reply (call 0's entries)."""
        return self.calls[0].entered if self.calls else ()


@dataclass
class _PendingHook:
    """A hook_additional_context line whose command is resolved after the
    pass (its hook_success may be written either side of it)."""

    tool_use_id: str | None
    hook_name: str
    line: str


@dataclass
class _OpenCall:
    ctx: int
    output_tokens: int
    message_id: str | None
    entered: list = field(default_factory=list)


def _int_count(value: object) -> int | None:
    """A token count as an int, or None when it is not a finite number."""
    if isinstance(value, bool):
        return None
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _usage_ctx(u: dict) -> int | None:
    parts = [_int_count(u.get(k)) for k in
             ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    if any(p is None for p in parts):
        return None
    return sum(parts)  # type: ignore[arg-type]


class Reading(NamedTuple):
    """What one assistant record says of its call: the model, the window it
    read (`ctx`), its output tokens and its message id (None when it has
    none)."""

    model: str
    ctx: int
    output_tokens: int
    message_id: str | None


def real_model(msg: dict) -> str | None:
    """MSG's model id, stripped, or None when it is missing, blank or
    Claude Code's own `<synthetic>`."""
    m = msg.get("model")
    if not isinstance(m, str) or not m.strip() or m.strip() == SYNTHETIC_MODEL:
        return None
    return m.strip()


def call_reading(msg: dict) -> Reading | None:
    """The call an assistant MESSAGE records, or None when it is not one: a
    real model (`real_model`) and a readable usage row (input + cache counts
    and output_tokens, each a finite number or absent).

    THE call acceptance: `fold`, `hooks` and `boot` all count calls through
    this one function, so their call counts cannot drift apart. Which record
    is a NEW call (its message id not seen before) stays with each caller,
    since the fold also folds a repeat's output_tokens into its call."""
    model = real_model(msg)
    if model is None:
        return None
    u = msg.get("usage")
    if not u or not isinstance(u, dict):
        return None
    ctx = _usage_ctx(u)
    out_tok = _int_count(u.get("output_tokens"))
    if ctx is None or out_tok is None:
        return None
    mid = msg.get("id") if isinstance(msg.get("id"), str) else None
    return Reading(model, ctx, out_tok, mid)


def _text_of(content: object) -> str:
    """A content field's text: a string, or the text blocks of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict) and isinstance(b.get("text"), str):
                out.append(b["text"])
        return "\n".join(out)
    return ""


def _tool_key(name: str, tool_input: object) -> str:
    """'Read /path/to/file', 'Bash git status', or the bare tool name."""
    if isinstance(tool_input, dict):
        for f in _KEY_FIELDS:
            v = tool_input.get(f)
            if isinstance(v, str) and v.strip():
                detail = " ".join(v.split())
                if len(detail) > _KEY_DETAIL_MAX:
                    detail = detail[:_KEY_DETAIL_MAX - 3] + "..."
                return f"{name} {detail}"
    return name


def _hook_context(success: dict) -> str:
    """The additionalContext a hook_success record's stdout carried."""
    stdout = success.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        try:
            out = json.loads(stdout)
        except json.JSONDecodeError:
            return ""
        if isinstance(out, dict):
            spec = out.get("hookSpecificOutput")
            if isinstance(spec, dict) and isinstance(spec.get("additionalContext"), str):
                return spec["additionalContext"]
    return ""


def _attachment_entries(a: dict, hooks: list) -> list:
    kind = a.get("type")
    if not isinstance(kind, str) or kind in NOT_CONTEXT:
        return []
    if kind == "hook_additional_context":
        content = a.get("content")
        lines = content if isinstance(content, list) else [content]
        tid = a.get("toolUseID") if isinstance(a.get("toolUseID"), str) else None
        name = a.get("hookName") if isinstance(a.get("hookName"), str) else "hook"
        out = []
        for line in lines:
            if isinstance(line, str) and line:
                pending = _PendingHook(tid, name, line)
                hooks.append(pending)
                out.append(pending)
        return out
    if kind == "instructions":
        files = a.get("files")
        out = []
        if isinstance(files, list):
            for f in files:
                if isinstance(f, dict) and isinstance(f.get("content"), str):
                    path = f.get("path") if isinstance(f.get("path"), str) else kind
                    out.append(Entry(MEMORY, path, len(f["content"])))
        return out
    if kind == "nested_memory":
        content = a.get("content")
        text = content.get("content") if isinstance(content, dict) else content
        path = a.get("path") if isinstance(a.get("path"), str) else kind
        return [Entry(MEMORY, path, len(_text_of(text) or json.dumps(a, ensure_ascii=False)))]
    if kind == "queued_command":
        prompt = _text_of(a.get("prompt"))
        mode = a.get("commandMode") if isinstance(a.get("commandMode"), str) else kind
        return [Entry(QUEUED, mode, len(prompt))]
    if kind in ("skill_listing", "total_tokens_reminder", "silent_turn_reminder", "model"):
        text = _text_of(a.get("content")) or _text_of(a.get("text"))
        if text:
            return [Entry(_ATTACHMENT_CATEGORY[kind], kind, len(text))]
    category = _ATTACHMENT_CATEGORY.get(kind, OTHER)
    return [Entry(category, kind, len(json.dumps(a, ensure_ascii=False)))]


def _user_entries(rec: dict, tools: dict, brief_seen: bool) -> list:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    text_category = USER if brief_seen else BRIEF
    if isinstance(content, str):
        return [Entry(text_category, "text", len(content))]
    out = []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                key = tools.get(tid, "tool_result") if isinstance(tid, str) else "tool_result"
                out.append(Entry(RESULT, key, len(_text_of(b.get("content")))))
            elif b.get("type") == "text" and isinstance(b.get("text"), str):
                out.append(Entry(text_category, "text", len(b["text"])))
    return out


def _resolve_hooks(hooks: list, successes: dict) -> dict:
    """id(pending) -> the hook command its line came from, else its event."""
    out = {}
    for h in hooks:
        cands = successes.get(h.tool_use_id, [])
        command = next((c for c, ctx in cands if ctx and h.line in ctx), None)
        if command is None:
            with_ctx = [c for c, ctx in cands if ctx]
            command = with_ctx[0] if len(with_ctx) == 1 else h.hook_name
        out[id(h)] = command
    return out


def fold(path: Path) -> Fold:
    """Read a transcript once; return its calls, each with what entered."""
    calls: list[_OpenCall] = []
    by_id: dict[str, _OpenCall] = {}
    pending: list = []
    tools: dict[str, str] = {}
    hooks: list[_PendingHook] = []
    successes: dict[str | None, list] = {}
    model: str | None = None
    brief_seen = False

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            kind = rec.get("type")
            if kind == "assistant":
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if (isinstance(b, dict) and b.get("type") == "tool_use"
                                and isinstance(b.get("id"), str)):
                            name = b.get("name") if isinstance(b.get("name"), str) else "tool"
                            tools[b["id"]] = _tool_key(name, b.get("input"))
                model = real_model(msg) or model
                r = call_reading(msg)
                if r is None:
                    continue
                mid = r.message_id
                if mid is not None and mid in by_id:
                    c = by_id[mid]
                    c.output_tokens = max(c.output_tokens, r.output_tokens)
                    continue
                c = _OpenCall(r.ctx, r.output_tokens, mid, pending)
                pending = []
                calls.append(c)
                if mid is not None:
                    by_id[mid] = c
            elif kind == "user":
                entries = _user_entries(rec, tools, brief_seen)
                if any(e.category == BRIEF for e in entries):
                    brief_seen = True
                pending.extend(entries)
            elif kind == "attachment":
                a = rec.get("attachment")
                if not isinstance(a, dict):
                    continue
                if a.get("type") == "hook_success":
                    tid = a.get("toolUseID") if isinstance(a.get("toolUseID"), str) else None
                    cmd = a.get("command") if isinstance(a.get("command"), str) else None
                    name = a.get("hookName") if isinstance(a.get("hookName"), str) else "hook"
                    successes.setdefault(tid, []).append((cmd or name, _hook_context(a)))
                    continue
                pending.extend(_attachment_entries(a, hooks))

    commands = _resolve_hooks(hooks, successes)

    def final(items: list) -> tuple[Entry, ...]:
        return tuple(Entry(HOOK, commands[id(e)], len(e.line)) if isinstance(e, _PendingHook)
                     else e for e in items if (e.chars if isinstance(e, Entry) else 1) > 0)

    return Fold(calls=tuple(Call(c.ctx, c.output_tokens, final(c.entered), c.message_id)
                            for c in calls), model=model)
