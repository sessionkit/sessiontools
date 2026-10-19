"""The boot views: `--startup` (the first call) and `--boot` (up to SETTLED).

STARTUP is the first call. Its total is exact: the first call's ctx. The
named rows are what entered before it (call 0's entries in the fold), grouped
by category and counted at a fixed rate per kind (BOOT_RATES: CLAUDE.md at
0.385 tokens per char, calibrated on two seats; every other kind at chars/4,
uncalibrated). Counted rows are estimates, `fitted`. CLAUDE CODE CORE (system
prompt, built-in tools, MCP schemas) is not in the transcript: it is the boot
total minus the named rows, an exact integer remainder that absorbs the rows'
error. When the transcript holds a recorded `/context` run (an `isMeta`
message), its System prompt / System tools / MCP tools rows split the core,
labelled with the run's time.

BOOT runs to SETTLED, the call whose window is the seat's boot:
- an AGENT (the transcript names one: an `agent-name` or `agent-setting`
  record) settles at its first act on work - the call that issues the first
  mail read or mail claim after the boot readout (`memory wake`), or the
  first `tm slice claim`, whichever comes first; failing both, the first
  call that uses a tool after the brief's own reads. A seat's work arrives as
  mail and slices, not typed input, so typed input is never its signal.
- a HUMAN session (no agent identity) settles at the last call before the
  first typed input after the brief.
The call that issues an act read a window the act's result is not in yet,
so it is "the last call before" the act.

THE BRIEF is the first user record. When it starts with the launcher's
pointer lead (FIRST_MESSAGE_POINTER_LEAD, "Your first message is materialised
at ..."), the brief is really the file it points at, and the call that reads
that file is part of the brief: SETTLED is never before the call that sees
its result. That is the context-boot bug (a pointer brief mistaken for the
first input), fixed by construction.

Data only: nothing here renders. Every row carries `basis` - "exact",
"counted", "snapshot" or "remainder".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sessiontools.context_parts import slash_context
from sessiontools.context_parts.slash_context import Snapshot
from sessiontools.context_parts.transcript import (
    BOOT_CATEGORIES,
    BRIEF,
    CATEGORIES,
    LABELS,
    MEMORY,
    RESULT,
    Fold,
    _tool_key,
    call_reading,
    fold,
)

# ---------- per-kind rates ------------------------------------------------
#: Tokens per char for a kind with no calibration: chars/4.
DEFAULT_BOOT_RATE = 0.25
#: Calibrated kinds. CLAUDE.md: the
#: `instructions` attachment split at 0.385 tok/char, calibrated on two seats.
BOOT_RATES: dict[str, float] = {MEMORY: 0.385}
RATE_SOURCES: dict[str, str] = {MEMORY: "calibrated (CLAUDE.md, two seats)"}
DEFAULT_RATE_SOURCE = "chars/4, uncalibrated"

CORE = "core"
CORE_LABEL = "Claude Code core (prompt + tools)"
CORE_REST = "core_rest"
CORE_REST_LABEL = "Core, not split by /context"

# ---------- SETTLED signals -----------------------------------------------
MAIL_READ = "mail-read"
SLICE_CLAIM = "slice-claim"
AFTER_BRIEF = "after-brief"
TYPED_INPUT = "typed-input"
NOT_SETTLED = "not-settled"
SIGNALS: dict[str, str] = {
    MAIL_READ: "first mail read or claim after the boot readout",
    SLICE_CLAIM: "first tm slice claim",
    AFTER_BRIEF: "first tool call after the brief's own reads",
    TYPED_INPUT: "last call before the first typed input",
    NOT_SETTLED: "not settled yet: no act on work, every call is boot",
}

#: The boot readout an agent runs first (taskmaster's `memory wake`).
READOUT = re.compile(r"\bmemory\s+wake\b")
#: A Bash command that reads or claims mail.
MAIL_COMMAND = re.compile(r"\bmessage\s+read\b|\bsession\s+mail\s+(?:read|claim|arm|wait)\b")
#: A tool (an MCP mail tool) that reads or claims mail.
MAIL_TOOL = re.compile(r"(?:^|_)(?:mail|message)_(?:read|claim)$")
#: A Bash command that claims a slice.
SLICE_CLAIM_COMMAND = re.compile(r"\b(?:tm|taskmaster)\s+slice\s+claim\b")
#: What a pointer brief says after the lead: the path, then "(N bytes)".
_POINTER_PATH = re.compile(r"^(\S.*?) \(\d+ bytes\)")
#: Records Claude Code writes for a slash command's output, not typed text.
_NOT_TYPED_PREFIXES = ("<local-command-stdout>", "<local-command-stderr>",
                       "<local-command-caveat>")
_VOICE_INPUT = "Voice response:"
#: The longest typed-input excerpt a human boot's signal detail carries.
DETAIL_MAX = 60

POINTER_UNREAD_WARNING = "the brief points at a file no call read: {path}"
CORE_NEGATIVE_WARNING = "the named rows exceed the boot total: their rates overshoot"
CALLS_MISMATCH_WARNING = "the boot timeline and the fold disagree on the calls"


def pointer_lead() -> str:
    """The launcher's pointer lead, from the one place it is defined.

    Imported here, not at module top: `sessiontools.start` pulls in PyYAML
    for agent front matter, and `import sessiontools.context_parts.boot`
    stays standard-library only (only reading a boot needs the lead)."""
    from sessiontools.start.context import FIRST_MESSAGE_POINTER_LEAD
    return FIRST_MESSAGE_POINTER_LEAD


def pointer_path(text: str, lead: str | None = None) -> str | None:
    """The path a pointer brief names, or None when `text` is not one."""
    lead = pointer_lead() if lead is None else lead
    if not isinstance(text, str) or not text.lstrip().startswith(lead):
        return None
    rest = text.lstrip()[len(lead):]
    m = _POINTER_PATH.match(rest)
    if m:
        return m.group(1)
    head = rest.split(None, 1)
    return head[0] if head else None


def boot_rate(category: str) -> tuple[float, str]:
    """(tokens per char, where that rate comes from) for a boot kind."""
    if category in BOOT_RATES:
        return BOOT_RATES[category], RATE_SOURCES[category]
    return DEFAULT_BOOT_RATE, DEFAULT_RATE_SOURCE


# ---------- data ------------------------------------------------------------
@dataclass(frozen=True)
class BootRow:
    """One row of a boot view. `tokens` is exact for a total, chars x rate
    (rounded) for a counted kind, `/context`'s estimate for a snapshot row,
    and the integer remainder for the core."""

    category: str
    label: str
    tokens: int
    basis: str                     # exact | counted | snapshot | remainder
    chars: int | None = None
    rate: float | None = None
    rate_source: str | None = None
    count: int = 0                 # entries in the row (files, listings, reads)

    @property
    def exact(self) -> bool:
        return self.basis == "exact"

    @property
    def fitted(self) -> bool:
        """An estimate: counted at a rate, or `/context`'s own figure."""
        return self.basis in ("counted", "snapshot")


@dataclass(frozen=True)
class Startup:
    """The first call: an exact total, named rows, the core remainder, and
    the core's split when a `/context` run was recorded. `total` is None when
    the transcript has no call yet (unknown, never a guess)."""

    total: int | None
    rows: tuple[BootRow, ...] = ()
    core: BootRow | None = None
    core_split: tuple[BootRow, ...] = ()
    snapshot: Snapshot | None = None
    brief_pointer: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def named(self) -> int:
        """The named rows' tokens, together."""
        return sum(r.tokens for r in self.rows)

    def snapshot_label(self, tz=None) -> str | None:
        """'from /context at HH:MM' (local unless `tz`), or None."""
        if self.snapshot is None:
            return None
        hhmm = self.snapshot.hhmm(tz)
        return f"from /context at {hhmm}" if hhmm else "from /context (time not recorded)"


@dataclass(frozen=True)
class BootRead:
    """Something that entered between the first call and SETTLED: a read, a
    hook line, mail. `call` is the call that first saw it."""

    call: int
    category: str
    key: str
    chars: int
    tokens: int                    # counted: chars x the kind's rate, rounded
    rate: float


@dataclass(frozen=True)
class Boot:
    """Up to SETTLED. `total` (the settled call's window), `on_demand` (total
    minus startup) and `own_output` (the outputs of the calls before
    settled) are exact; `reads` are counted; `unattributed` is what is left
    of on_demand. `signal` names which SETTLED signal fired (SIGNALS),
    `signal_detail` the tool call that fired it."""

    startup: Startup
    agent: bool
    identity: str | None
    calls: int
    settled_call: int | None
    total: int | None
    on_demand: int | None
    own_output: int | None
    reads: tuple[BootRead, ...]
    signal: str
    signal_detail: str | None = None
    signal_call: int | None = None
    held_for_brief: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def reads_tokens(self) -> int:
        return sum(r.tokens for r in self.reads)

    @property
    def unattributed(self) -> int | None:
        """on_demand minus own output minus the counted reads."""
        if self.on_demand is None or self.own_output is None:
            return None
        return self.on_demand - self.own_output - self.reads_tokens

    @property
    def signal_text(self) -> str:
        return SIGNALS[self.signal]


# ---------- the timeline: one more read-only pass ---------------------------
@dataclass(frozen=True)
class _ToolUse:
    seq: int              # order among all tool uses
    call: int             # the call that issued it
    name: str
    input: object
    key: str

    def strings(self) -> list[str]:
        if isinstance(self.input, dict):
            return [v for v in self.input.values() if isinstance(v, str)]
        return [self.input] if isinstance(self.input, str) else []

    def command(self) -> str:
        if isinstance(self.input, dict) and isinstance(self.input.get("command"), str):
            return self.input["command"]
        return ""


@dataclass
class _Timeline:
    calls: int = 0
    identity: str | None = None
    brief: str | None = None
    tool_uses: list = field(default_factory=list)
    typed: list = field(default_factory=list)      # (calls made before it, its text)
    snapshot: Snapshot | None = None


def _typed_text(rec: dict) -> str | None:
    """What a human typed (or spoke) in this user record, or None when it is
    not typed input: a tool result, a meta record, a reminder or a slash
    command's output."""
    if rec.get("isMeta"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                inner = b.get("content")
                inner_text = inner if isinstance(inner, str) else _text_of_prompt(inner)
                if _VOICE_INPUT in inner_text:
                    return inner_text[inner_text.index(_VOICE_INPUT):]
            elif b.get("type") == "text" and isinstance(b.get("text"), str):
                texts.append(b["text"])
    for t in texts:
        s = t.strip()
        if not s or s.startswith(_NOT_TYPED_PREFIXES):
            continue
        if s.startswith("<system-reminder>") and s.endswith("</system-reminder>"):
            continue
        return s
    return None


def _first_text(rec: dict) -> str | None:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [b["text"] for b in content if isinstance(b, dict)
                 and b.get("type") == "text" and isinstance(b.get("text"), str)]
        if texts:
            return "\n".join(texts)
    return None


def _text_of_prompt(prompt: object) -> str:
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return "\n".join(b["text"] for b in prompt
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _excerpt(text: str) -> str:
    """One line of typed text, at most DETAIL_MAX chars."""
    line = " ".join(text.split())
    return line if len(line) <= DETAIL_MAX else line[:DETAIL_MAX - 3] + "..."


def _timeline(path: Path) -> _Timeline:
    """Calls (counted as the fold counts them), tool uses, typed input, the
    brief, the agent identity and the first `/context` snapshot."""
    t = _Timeline()
    by_id: dict[str, int] = {}
    seq = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for n, line in enumerate(f):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            kind = rec.get("type")
            if kind == "agent-name" and isinstance(rec.get("agentName"), str):
                t.identity = t.identity or rec["agentName"].strip() or None
            elif kind == "agent-setting" and isinstance(rec.get("agentSetting"), str):
                t.identity = t.identity or rec["agentSetting"].strip() or None
            elif kind == "assistant":
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                call = _call_index(msg, by_id, t)
                if call is None:
                    continue
                content = msg.get("content")
                for b in content if isinstance(content, list) else ():
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        name = b.get("name") if isinstance(b.get("name"), str) else "tool"
                        t.tool_uses.append(_ToolUse(seq, call, name, b.get("input"),
                                                    _tool_key(name, b.get("input"))))
                        seq += 1
            elif kind == "user":
                if t.snapshot is None:
                    t.snapshot = slash_context.from_record(rec, n)
                if t.brief is None:
                    text = _first_text(rec)
                    if text is not None:
                        t.brief = text
                        continue
                typed = _typed_text(rec) if t.brief is not None else None
                if typed is not None:
                    t.typed.append((t.calls, typed))
            elif kind == "attachment":
                a = rec.get("attachment")
                if (isinstance(a, dict) and a.get("type") == "queued_command"
                        and a.get("commandMode") == "prompt" and t.brief is not None):
                    t.typed.append((t.calls, _text_of_prompt(a.get("prompt"))))
    return t


def _call_index(msg: dict, by_id: dict, t: _Timeline) -> int | None:
    """This assistant record's call, accepted by the fold's own
    `call_reading`, or None."""
    r = call_reading(msg)
    if r is None:
        return None
    mid = r.message_id
    if mid is not None and mid in by_id:
        return by_id[mid]
    index = t.calls
    t.calls += 1
    if mid is not None:
        by_id[mid] = index
    return index


# ---------- the views -------------------------------------------------------
def _startup(f: Fold, t: _Timeline, lead: str) -> Startup:
    pointer = pointer_path(t.brief, lead) if t.brief is not None else None
    if not f.calls:
        return Startup(total=None, brief_pointer=pointer)
    total = f.calls[0].ctx
    chars: dict[str, int] = {}
    count: dict[str, int] = {}
    for e in f.boot_entered:
        chars[e.category] = chars.get(e.category, 0) + e.chars
        count[e.category] = count.get(e.category, 0) + 1
    order = list(BOOT_CATEGORIES) + [c for c in CATEGORIES if c not in BOOT_CATEGORIES]
    rows = []
    for c in order:
        if c not in chars:
            continue
        rate, source = boot_rate(c)
        rows.append(BootRow(c, LABELS[c], round(chars[c] * rate), "counted",
                            chars=chars[c], rate=rate, rate_source=source, count=count[c]))
    core_tokens = total - sum(r.tokens for r in rows)
    core = BootRow(CORE, CORE_LABEL, core_tokens, "remainder")
    warnings = [CORE_NEGATIVE_WARNING] if core_tokens < 0 else []

    split: list[BootRow] = []
    if t.snapshot is not None and t.snapshot.core_rows():
        for name, tokens in t.snapshot.core_rows().items():
            split.append(BootRow(name, name, tokens, "snapshot"))
        split.append(BootRow(CORE_REST, CORE_REST_LABEL,
                             core_tokens - sum(r.tokens for r in split), "remainder"))
    return Startup(total=total, rows=tuple(rows), core=core, core_split=tuple(split),
                   snapshot=t.snapshot, brief_pointer=pointer, warnings=tuple(warnings))


def _brief_reads(t: _Timeline, pointer: str | None) -> list[_ToolUse]:
    """The tool uses that read the pointer brief's file: those in the FIRST
    call that reads it. A later re-read is a read like any other, and never
    moves SETTLED."""
    if not pointer:
        return []
    reads = [u for u in t.tool_uses if any(pointer in s for s in u.strings())]
    if not reads:
        return []
    first = min(u.call for u in reads)
    return [u for u in reads if u.call == first]


def _agent_signal(t: _Timeline, brief_reads: list[_ToolUse]) -> tuple[str, _ToolUse | None]:
    readout = next((u.seq for u in t.tool_uses if READOUT.search(u.command())), -1)
    mail = next((u for u in t.tool_uses if u.seq > readout and
                 (MAIL_COMMAND.search(u.command()) or MAIL_TOOL.search(u.name))), None)
    claim = next((u for u in t.tool_uses if SLICE_CLAIM_COMMAND.search(u.command())), None)
    firsts = [(u.seq, sig, u) for sig, u in ((MAIL_READ, mail), (SLICE_CLAIM, claim)) if u]
    if firsts:
        _, sig, u = min(firsts, key=lambda x: x[0])
        return sig, u
    after = max((u.seq for u in brief_reads), default=-1)
    read_calls = {u.call for u in brief_reads}
    act = next((u for u in t.tool_uses if u.seq > after and u.call not in read_calls), None)
    return (AFTER_BRIEF, act) if act else (NOT_SETTLED, None)


def startup(path: Path | str) -> Startup:
    """`--startup`: what was in the window before the first reply."""
    path = Path(path)
    return _startup(fold(path), _timeline(path), pointer_lead())


def boot(path: Path | str) -> Boot:
    """`--boot`: the startup, plus everything up to SETTLED."""
    path = Path(path)
    f = fold(path)
    t = _timeline(path)
    lead = pointer_lead()
    s = _startup(f, t, lead)
    warnings = list(s.warnings)
    if t.calls != len(f.calls):
        warnings.append(CALLS_MISMATCH_WARNING)
    agent = t.identity is not None
    n = len(f.calls)
    if not n:
        return Boot(startup=s, agent=agent, identity=t.identity, calls=0,
                    settled_call=None, total=None, on_demand=None, own_output=None,
                    reads=(), signal=NOT_SETTLED, warnings=tuple(warnings))

    brief_reads = _brief_reads(t, s.brief_pointer)
    if s.brief_pointer and not brief_reads:
        warnings.append(POINTER_UNREAD_WARNING.format(path=s.brief_pointer))

    detail = None
    signal_call = None
    if agent:
        signal, act = _agent_signal(t, brief_reads)
        settled = act.call if act else n - 1
        if act:
            detail, signal_call = act.key, act.call
    elif t.typed:
        signal = TYPED_INPUT
        before, text = t.typed[0]
        settled = max(before - 1, 0)
        signal_call = settled
        detail = _excerpt(text)
    else:
        signal, settled = NOT_SETTLED, n - 1

    held = False
    if brief_reads:
        floor = max(u.call for u in brief_reads) + 1
        if floor < n and settled < floor:
            settled, held = floor, True
    settled = min(settled, n - 1)

    read_keys = {u.key for u in brief_reads}
    first_read_call = min((u.call for u in brief_reads), default=n)
    reads = []
    for k in range(1, settled + 1):
        for e in f.calls[k].entered:
            category = e.category
            key = e.key
            if category == RESULT and k > first_read_call and e.key in read_keys:
                category, key = BRIEF, f"brief: {e.key}"
            rate, _ = boot_rate(category)
            reads.append(BootRead(k, category, key, e.chars, round(e.chars * rate), rate))

    total = f.calls[settled].ctx
    own = sum(f.calls[k].output_tokens for k in range(settled))
    return Boot(startup=s, agent=agent, identity=t.identity, calls=n,
                settled_call=settled, total=total, on_demand=total - f.calls[0].ctx,
                own_output=own, reads=tuple(reads), signal=signal, signal_detail=detail,
                signal_call=signal_call, held_for_brief=held, warnings=tuple(warnings))
