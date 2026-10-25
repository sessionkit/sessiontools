"""`session park`: a seat says it has settled, and the record says so.

A seat parks as the last step of its settle. The park writes one file,
`<session dir>/park.json`, holding when it parked, when its last real work
ended, and what each check found. `session park --check` reads it back:
parked or not, clean or not, and why. Nothing here kills, mails, writes
config, or computes a deadline: the record holds facts, and whatever acts
on them is somebody else's.

This module is the record, the transcript and the five checks. The verb
builds on them.

THE CHECKS (`run_checks`, in CHECK_NAMES order), each PASS, FAIL or UNKNOWN:
`tree` (no unsaved work, read as `session reap` reads a tail), `work` (no
running work but listeners and the idle timer, no background agent still
running), `claim` (no claim held in the cwd's task), `mail` (no unread mail
in the session box) and `promises` (what the park declared). Positive
evidence only: UNKNOWN is never PASS, and a FAIL beside an UNKNOWN is FAIL.
Every read is a file, `ps` or `git --no-optional-locks`: no sessionmail or
taskmaster import, no subprocess of theirs, nothing written.

THE RECORD, contract v1 (`read`, `write`):

- THE FILE: `settings.session_dir(sid) / "park.json"`, so `$SESSIONS_DIR`
  moves it with every other session file. Only the seat writes its own,
  and the session dir must exist already: no dir, no record.
- WRITTEN ATOMICALLY: a temp file in the same folder, opened 0600, then
  rename. A reader sees no file or a whole one, and a failed write leaves
  neither the record nor the temp file behind.
- THE BODY: `v`, `session_id`, `parked_at`, `work_ended_at` (float or
  null, never after `parked_at`), `work_ended_why`, `letter`,
  `keep_until`, `keep_why`, `promises`, `checks` (five, in a fixed order,
  each PASS, FAIL or UNKNOWN with a why) and `clean` (true only when all
  five are PASS). Times are epoch seconds, as floats. There is NO deadline
  field.
- THE READER refuses anything less than a whole v1 record: no file, not
  JSON, a different session id, a time that is not a finite number, a check
  it does not know. A refused record means NOT PARKED, never parked.

THE TRANSCRIPT (`transcript_facts`): Claude Code's own `<sid>.jsonl`, read
in one pass. It yields the turn starts, every tool call with when it began
and ended, and whether each call was WORK.

- A TURN START is a `user` record that is not a subagent's (`isSidechain`),
  not meta (`isMeta`: a skill body, a Stop hook's feedback), and not only
  tool results. A typed prompt, a pager page, a background completion and a
  compaction's summary all start one. So does input that arrives while a
  turn runs: Claude Code records it as an `attachment` of type
  `queued_command` (a page or a background completion, mid-turn), and it is
  new input all the same.
- A call is WORK unless it is a Bash call whose every segment is a
  NOT_WORK command: the seat reading or recording its own state and mail,
  re-arming its listener, or running its idle timer. Anything this code
  does not recognise is work. A turn with no tool calls is not work.
- THE PARK'S OWN CALL never counts as work for `work_ended`, whatever it
  chains, so a chained re-park buys no time. What it chains before the park
  ran before the record; work it chains AFTER the park ran after it, and
  unparks it. Run `session park` as a call of its own.

`work_ended` is the end of the last work call, carried unchanged from the
previous park when no work happened since: parking buys no time.
`unparked` says why a record no longer holds: a turn that started after
the park, or work done after it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sessiontools import settings
from sessiontools.claude_code import WHY_WIDTH, Session, command_words

#: The record's file name, in the session dir.
RECORD = "park.json"
#: The contract version this reader and writer speak.
VERSION = 1

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
WORDS = (PASS, FAIL, UNKNOWN)
#: The five checks, in the order they run and are printed.
CHECK_NAMES = ("tree", "work", "claim", "mail", "promises")

#: Commands that are not work: the seat reading or recording its own state
#: and mail, and re-arming its listener. A seat that is warned, reads the
#: warning, marks it read and parks has done no work. Each entry is the
#: command word and the arguments that must follow it.
NOT_WORK: tuple[tuple[str, ...], ...] = (
    ("session", "park"),
    ("session", "get"),
    ("session", "list"),
    ("message", "list"),
    ("message", "read"),
    ("message", "mark"),
    ("pager", "status"),
    ("pager", "listen"),
)


@dataclass(frozen=True)
class Timer:
    """A command that only waits: one of HEADS exactly, as its first words,
    and FLAG as a whole word somewhere after them."""

    heads: tuple[tuple[str, ...], ...]
    flag: str


#: The worker's idle timer. A FREE worker ends its turn by running this,
#: backgrounded: it waits for its next assignment, claims nothing and
#: changes nothing, so it is not work, and without this no FREE worker
#: could ever park clean. The match is deliberately narrow: those three
#: words first, AND `--idle-wait` as a whole word after them. The same verb
#: without that flag (`--free`, `--check`, bare), and every other verb of
#: the same tool, is work. Never a substring of the line.
IDLE_TIMER = Timer(heads=(("tm", "task", "standdown"), ("taskmaster", "task", "standdown")),
                   flag="--idle-wait")


def is_idle_timer(words: list[str]) -> bool:
    """True when WORDS (`command_words` of one command) are IDLE_TIMER."""
    n = len(IDLE_TIMER.heads[0])
    return (any(tuple(words[:len(h)]) == h for h in IDLE_TIMER.heads)
            and IDLE_TIMER.flag in words[n:])


# --- the record -------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    """One check's result: NAME, WORD (PASS, FAIL or UNKNOWN) and WHY."""

    name: str
    word: str
    why: str

    def to_dict(self) -> dict:
        return {"name": self.name, "word": self.word, "why": self.why}


@dataclass(frozen=True)
class Record:
    """One park, as written to `park.json`."""

    session_id: str
    parked_at: float
    work_ended_at: float | None
    work_ended_why: str
    checks: tuple[Check, ...]
    letter: str | None = None
    keep_until: float | None = None
    keep_why: str | None = None
    promises: Mapping[str, Any] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """True only when every check reads PASS: UNKNOWN is never PASS."""
        return bool(self.checks) and all(c.word == PASS for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "v": VERSION,
            "session_id": self.session_id,
            "parked_at": self.parked_at,
            "work_ended_at": self.work_ended_at,
            "work_ended_why": self.work_ended_why,
            "letter": self.letter,
            "keep_until": self.keep_until,
            "keep_why": self.keep_why,
            "promises": dict(self.promises),
            "checks": [c.to_dict() for c in self.checks],
            "clean": self.clean,
        }


def record_path(sid: str, env: Mapping[str, str] | None = None) -> Path:
    """Where SID's record lives: `<session dir>/park.json`."""
    return settings.session_dir(sid, env) / RECORD


def _number(value: Any) -> float | None:
    """VALUE as a finite float, else None. A boolean is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse(text: str, sid: str) -> tuple[Record | None, str]:
    """A record's body -> (Record, "") or (None, why it is not one). SID is
    the session the file belongs to: a body naming another is not its."""
    try:
        data = json.loads(text)
    except ValueError:
        return None, "not JSON"
    if not isinstance(data, dict):
        return None, "not a JSON object"
    v = data.get("v")
    if isinstance(v, bool) or v != VERSION:
        return None, f"not contract v{VERSION} (v is {v!r})"
    if data.get("session_id") != sid:
        return None, f"names another session ({data.get('session_id')!r})"
    parked_at = _number(data.get("parked_at"))
    if parked_at is None:
        return None, "parked_at is not a time"
    ended_raw = data.get("work_ended_at")
    ended = _number(ended_raw)
    if ended_raw is not None and ended is None:
        return None, "work_ended_at is not a time"
    if ended is not None and ended > parked_at:
        return None, "work_ended_at is after parked_at"
    why = data.get("work_ended_why")
    if not isinstance(why, str):
        return None, "work_ended_why is not text"
    checks_raw = data.get("checks")
    if not isinstance(checks_raw, list) or len(checks_raw) != len(CHECK_NAMES):
        return None, f"checks is not a list of {len(CHECK_NAMES)}"
    checks = []
    for want, c in zip(CHECK_NAMES, checks_raw, strict=True):
        if (not isinstance(c, dict) or c.get("name") != want or c.get("word") not in WORDS
                or not isinstance(c.get("why"), str)):
            return None, f"check {want!r} is not a check"
        checks.append(Check(want, c["word"], c["why"]))
    letter = data.get("letter")
    if letter is not None and not isinstance(letter, str):
        return None, "letter is not text"
    keep_raw = data.get("keep_until")
    keep = _number(keep_raw)
    keep_why = data.get("keep_why")
    if keep_raw is not None and keep is None:
        return None, "keep_until is not a time"
    if keep_why is not None and not isinstance(keep_why, str):
        return None, "keep_why is not text"
    if keep is not None and not (keep_why or "").strip():
        return None, "keep_until has no keep_why"
    promises = data.get("promises")
    if not isinstance(promises, dict):
        return None, "promises is not an object"
    record = Record(sid, parked_at, ended, why, tuple(checks), letter, keep, keep_why, promises)
    if data.get("clean") is not record.clean:
        return None, "clean does not agree with the checks"
    return record, ""


def read(path: Path, sid: str) -> tuple[Record | None, str]:
    """The record at PATH, for session SID -> (Record, "") or (None, why).
    No file reads as "never parked"."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "never parked"
    except (OSError, UnicodeError) as e:
        return None, f"park.json unreadable ({type(e).__name__})"
    record, why = parse(text, sid)
    return (record, "") if record else (None, f"park.json unreadable: {why}")


class NoSessionDir(Exception):
    """The seat has no session dir: park never makes one."""


def write(record: Record, env: Mapping[str, str] | None = None) -> Path:
    """RECORD to its seat's `park.json`, atomically: a 0600 temp file in the
    same folder, then rename. Writes nothing else. Raises NoSessionDir when
    the session dir does not exist, and OSError (after removing the temp
    file) when the write fails; either way no record is written."""
    path = record_path(record.session_id, env)
    if not path.parent.is_dir():
        raise NoSessionDir(str(path.parent))
    tmp = path.with_name(f".{RECORD}.{os.getpid()}.tmp")
    body = json.dumps(record.to_dict(), indent=2) + "\n"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return path


# --- the transcript ---------------------------------------------------------------

#: A shell's command separators. A segment is what lies between them. The
#: `&` of a redirection (`2>&1`, `&>file`) is not one.
_SEPARATORS = re.compile(r"&&|\|\||(?<![>&])&(?!>)|[;|\n]")
#: Forms that can run or write something a segment's words do not show:
#: command substitution and redirection. A command holding any is work.
_HIDDEN = ("$(", "`", ">", "<")
#: A background completion names the call it finished.
_TOOL_USE_ID = re.compile(r"<tool-use-id>\s*([^<\s]+)\s*</tool-use-id>")


def _not_work_words(words: list[str]) -> bool:
    return (any(tuple(words[:len(entry)]) == entry for entry in NOT_WORK)
            or is_idle_timer(words))


def is_not_work(command: object) -> bool:
    """True when the Bash COMMAND is not work: every segment between its
    separators starts with a NOT_WORK command or is the IDLE_TIMER. A
    command with a substitution or a redirection, an empty one, or one that
    is not text, is work."""
    if not isinstance(command, str) or any(h in command for h in _HIDDEN):
        return False
    segments = [s for s in _SEPARATORS.split(command) if s.strip()]
    return bool(segments) and all(_not_work_words(command_words(s)) for s in segments)


def _is_park(words: list[str]) -> bool:
    """`session park` that writes a record: not `--check`, which only reads."""
    return tuple(words[:2]) == ("session", "park") and "--check" not in words[2:]


def park_tail(command: object) -> str | None:
    """The Bash COMMAND from its last segment that writes a park record to
    its end, or None when no segment does. What the call chains BEFORE that
    segment ran before the record was written; the tail is the park and
    whatever ran after it, inside the same call."""
    if not isinstance(command, str):
        return None
    tail, start = None, 0
    for sep in [*_SEPARATORS.finditer(command), None]:
        end = sep.start() if sep else len(command)
        if _is_park(command_words(command[start:end])):
            tail = command[start:]
        if sep:
            start = sep.end()
    return tail


def runs_park(command: object) -> bool:
    """True when the Bash COMMAND has a segment that writes a park record:
    the call is a park's own."""
    return park_tail(command) is not None


def _label(name: str, tool_input: Mapping[str, Any]) -> str:
    """What a call was, short: `Bash git commit -m x` cut to WHY_WIDTH."""
    if name == "Bash" and isinstance(tool_input.get("command"), str):
        for segment in _SEPARATORS.split(tool_input["command"]):
            words = command_words(segment)
            if words and (not _not_work_words(words) or any(h in segment for h in _HIDDEN)):
                return f"{name} {' '.join(words)}"[:WHY_WIDTH]
    return name[:WHY_WIDTH]


@dataclass
class Call:
    """One tool call: when it began (`at_use`), when its result came back
    (`at_result`), and, for a backgrounded call, when its completion was
    announced (`at_done`)."""

    id: str
    name: str
    work: bool
    background: bool
    label: str
    at_use: float
    at_result: float | None = None
    at_done: float | None = None
    #: The call runs `session park` (not `--check`): a park's own call.
    parks: bool = False
    #: A park's own call that chains WORK after the park (`session park &&
    #: git push`, `; echo $?`, `2>&1 | tail`): that work ran after the record
    #: was written, so it unparks it. What it chains before the park does not.
    work_after_park: bool = False

    @property
    def end(self) -> float:
        """When the call's work ended: its completion for a backgrounded
        call, else its result, else (still running) when it began."""
        if self.background and self.at_done is not None:
            return self.at_done
        return self.at_result if self.at_result is not None else self.at_use


@dataclass(frozen=True)
class TurnStart:
    at: float
    #: `human`, `task-notification`, or `no origin` (a compaction's summary).
    origin: str


@dataclass
class Facts:
    """What one pass over a transcript found."""

    turns: list[TurnStart] = field(default_factory=list)
    calls: dict[str, Call] = field(default_factory=dict)
    first_at: float | None = None

    def work_calls(self) -> list[Call]:
        return [c for c in self.calls.values() if c.work]

    def open_background(self) -> list[Call]:
        """Backgrounded calls other than Bash (processes show those) whose
        completion has not been seen: in-process work that may be running."""
        return [c for c in self.calls.values()
                if c.background and c.name != "Bash" and c.at_done is None]


def _when(value: Any) -> float | None:
    """A transcript `timestamp` (ISO 8601, `Z` or an offset) -> epoch s."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value)  # 3.11+ reads a trailing Z
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.timestamp()


def _text(content: Any) -> str:
    """A message's text, whether it is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _results_only(content: Any) -> bool:
    return (isinstance(content, list) and bool(content)
            and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content))


def transcript_facts(lines: Iterable[str]) -> Facts:
    """One pass over a transcript's LINES. A line that is not a JSON object,
    a subagent's record, and a record with no timestamp are passed over."""
    facts = Facts()
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain") is True:
            continue
        at = _when(rec.get("timestamp"))
        if at is None:
            continue
        if facts.first_at is None:
            facts.first_at = at
        message = rec.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        kind = rec.get("type")
        if kind == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                call_id, name = block.get("id"), block.get("name")
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if not isinstance(call_id, str) or not isinstance(name, str):
                    continue
                work = not (name == "Bash" and is_not_work(tool_input.get("command")))
                tail = park_tail(tool_input.get("command")) if name == "Bash" else None
                after = tail is not None and not is_not_work(tail)
                facts.calls[call_id] = Call(call_id, name, work,
                                            tool_input.get("run_in_background") is True,
                                            _label(name, {"command": tail} if after else tool_input),
                                            at, parks=tail is not None, work_after_park=after)
        elif kind == "user":
            if _results_only(content):
                for block in content:
                    call = facts.calls.get(block.get("tool_use_id"))
                    if call is not None and call.at_result is None:
                        call.at_result = at
                continue
            if rec.get("isMeta") is True:
                continue
            origin = rec.get("origin")
            label = origin.get("kind") if isinstance(origin, dict) else None
            facts.turns.append(TurnStart(at, label if isinstance(label, str) and label
                                         else "no origin"))
            _done(facts, _text(content), at)
        elif kind == "attachment":
            queued = rec.get("attachment")
            if not isinstance(queued, dict) or queued.get("type") != "queued_command":
                continue
            origin = queued.get("origin")
            label = origin.get("kind") if isinstance(origin, dict) else queued.get("commandMode")
            facts.turns.append(TurnStart(at, f"queued {label}" if isinstance(label, str) and label
                                         else "queued"))
            _done(facts, _text(queued.get("prompt")), at)
    return facts


def _done(facts: Facts, text: str, at: float) -> None:
    """Each call TEXT names in a `<tool-use-id>` completed at AT."""
    for call_id in _TOOL_USE_ID.findall(text):
        call = facts.calls.get(call_id)
        if call is not None and call.at_done is None:
            call.at_done = at


def read_transcript(sid: str, cwd: str | None,
                    env: Mapping[str, str] | None = None) -> Facts | None:
    """SID's transcript, found as `session context` finds it, read once.
    None when there is none, or it cannot be read."""
    from sessiontools import window
    path = window.transcript_path(cwd, sid, env)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return transcript_facts(fh)
    except OSError:
        return None


# --- work_ended_at and unpark -----------------------------------------------------

def clock(at: float) -> str:
    """AT as local `HH:MM:SS`."""
    return datetime.fromtimestamp(at).astimezone().strftime("%H:%M:%S")


def stamp(at: float) -> str:
    """AT as local ISO 8601, to the second."""
    return datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")


def work_ended(facts: Facts | None, now: float, prior: Record | None = None,
               started_at: float | None = None) -> tuple[float | None, str]:
    """(work_ended_at, why) for a park made at NOW.

    The end of the last WORK call at or before NOW. When PRIOR (the seat's
    previous record) exists and no work ended after its park, PRIOR's value
    is carried unchanged: re-parking moves nothing. No work at all and no
    prior record: the session's start (STARTED_AT, else the transcript's
    first record). FACTS None (no readable transcript): PRIOR's value
    carried, else None. A call that runs `session park` is never counted,
    whatever it chains: were it, every chained re-park would move the value
    to that park, and parking would buy time. Dropping the work it chains
    errs toward an earlier value, the safe way; work chained after the park
    unparks the record instead (`unparked`)."""
    if facts is None:
        if prior is not None:
            return prior.work_ended_at, "transcript unreadable: carried"
        return None, "transcript unreadable"
    ended = [c for c in facts.work_calls() if not c.parks and c.end <= now]
    if prior is not None and not any(c.end > prior.parked_at for c in ended):
        return prior.work_ended_at, f"carried from the park at {stamp(prior.parked_at)}: no work since"
    if ended:
        last = max(ended, key=lambda c: c.end)
        return min(last.end, now), f"last work: {last.label}"
    start = started_at if started_at is not None else facts.first_at
    if start is None:
        return None, "no work yet, and no start known"
    return min(start, now), "no work yet: the session's start"


@dataclass(frozen=True)
class Unparked:
    """Why a record no longer holds. KIND is `turn` (a turn started after
    the park), `work` (a work call after it), `busy` (the sidecar says a
    turn is running) or `transcript` (it cannot be read, so nothing proves
    the seat is still parked)."""

    kind: str
    why: str
    at: float | None = None
    origin: str | None = None


def unparked(record: Record, facts: Facts | None,
             session: Session | None = None) -> Unparked | None:
    """None while RECORD still holds; else the first thing after its park
    that ended it. Only a turn START unparks, never the park turn's own
    tool results or meta records; a WORK call made after the park, or one
    still running when it was made and ending after it, unparks too, a
    NOT_WORK one does not. The call that made the park (one running `session
    park`, begun at or before it) does not unpark its own record for what it
    chains before the park (`cd x && session park`), only for WORK chained
    after it (`session park && git push`, `; echo $?`), which ran after the
    record was written. A SESSION the harness calls busy, with its status
    written after the park, unparks. A missing transcript is never parked."""
    if facts is None:
        return Unparked("transcript", "transcript unreadable")
    since = f"after the park at {clock(record.parked_at)}"
    events: list[Unparked] = []
    for turn in facts.turns:
        if turn.at > record.parked_at:
            events.append(Unparked("turn", f"a turn started at {clock(turn.at)} ({turn.origin}) {since}",
                                   turn.at, turn.origin))
            break
    for call in facts.work_calls():
        if call.parks and not call.work_after_park and call.at_use <= record.parked_at:
            continue
        if call.end > record.parked_at:
            at = call.at_use if call.at_use > record.parked_at else call.end
            events.append(Unparked("work", f"work {since}: {call.label}", at))
    if events:
        return min(events, key=lambda e: e.at if e.at is not None else math.inf)
    if (session is not None and session.raw_status == "busy"
            and session.updated_at > record.parked_at):
        return Unparked("busy", f"a turn is running (busy since {clock(session.updated_at)}) {since}",
                        session.updated_at)
    return None


# --- the five checks ---------------------------------------------------
#
# Each check returns a Check whose word is PASS, FAIL or UNKNOWN: reap's one
# idea, positive evidence only, so UNKNOWN is never PASS. When a check finds
# both a FAIL and an UNKNOWN, FAIL is the word: it is the positive evidence
# that the seat is not clean. `run_checks` runs all five in order, each even
# after an earlier one fails, and a check that raises reads UNKNOWN.

def _count(names: list[str], limit: int = 3) -> str:
    """Up to LIMIT of NAMES, then `+N more`."""
    more = f" +{len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


# 3.1 tree -------------------------------------------------------------------------

def check_tree(cwd: str | None, git: Any = None) -> Check:
    """No unsaved work where the seat sits. A task seat: the task's
    `worktree/` and its store at the task dir, read as `session reap` reads
    its tail. No task: the same read on the cwd itself (`worktree/` if it
    has one, and its repo at `-- .`). Any part dirty is FAIL; any part
    unreadable, with none dirty, is UNKNOWN. GIT is reap's `_git` seam."""
    from sessiontools import reap
    git = git or reap._git
    if not cwd:
        return Check("tree", UNKNOWN, "no cwd known")
    task, problem = reap.find_task(cwd)
    if problem:
        return Check("tree", UNKNOWN, problem)
    if task is not None:
        parts, prefix = reap.tail_parts(task.dir, git, "store"), f"task {task.task_id}: "
    else:
        parts, prefix = reap.tail_parts(Path(cwd), git, "cwd"), "no task: "
    problems = [p.detail for p in parts if p.state != reap.PART_CLEAN]
    if any(p.state == reap.PART_DIRTY for p in parts):
        return Check("tree", FAIL, prefix + "; ".join(problems))
    if problems:
        return Check("tree", UNKNOWN, prefix + "; ".join(problems))
    return Check("tree", PASS, prefix + " and ".join(p.where for p in parts) + " clean")


# 3.2 work -------------------------------------------------------------------------

def _listener_text(pid: int, table: dict, kids: dict[int, list[int]],
                   tokens: tuple[tuple[str, ...], ...]) -> str:
    """The first listener under the tool shell PID, as text."""
    queue, seen = list(kids.get(pid, [])), {pid}
    while queue:
        child = queue.pop(0)
        if child in seen or child not in table:
            continue
        seen.add(child)
        queue.extend(kids.get(child, []))
        words = command_words(table[child].command)
        if words and (any(tuple(words[:len(t)]) == t for t in tokens) or is_idle_timer(words)):
            return " ".join(words)[:WHY_WIDTH]
    return "listener"


def check_work(session: Session | None, table: dict, facts: Facts | None,
               listeners: object = None, own_pid: int | None = None) -> Check:
    """No running work other than listeners. Three reads:

    - THE PROCESSES under the seat's claude process, sorted as the pane
      signal sorts them (`sort_tool_shell`): a tool shell running anything
      but a listener (LISTENERS, `pane_state.listeners`) or the IDLE_TIMER
      is work. A direct child that is not a tool shell (the pager monitor,
      an MCP server, caffeinate) is the harness's own and never counted.
      OWN_PID, given only when the seat is this process's own, skips the
      tool shell running this very call (park, or `--check`).
    - THE TRANSCRIPT: a backgrounded call that is not Bash (an Agent runs
      inside the claude process, with no process of its own) and has no
      completion seen is work.
    - THE SIDECAR: a seat whose turn ended with children still running
      (`idle with children`) is not settled.
    """
    from sessiontools import claude_code, procs
    if listeners is None:
        listeners = claude_code.DEFAULT_LISTENERS
    fails: list[str] = []
    unknowns: list[str] = []
    found: list[str] = []
    if session is None:
        unknowns.append("no sidecar for the seat")
    elif not table or not procs.same_process(session.pid, session.proc_start, table):
        unknowns.append("no live process")
    else:
        kids = procs.children(table)
        direct = [table[c] for c in kids.get(session.pid, []) if c != session.pid]
        own = set(procs.ancestors(own_pid, table)) if own_pid is not None else set()
        tokens = claude_code.listener_tokens(listeners)
        if any(p.command is None for p in direct):
            unknowns.append("no commands in the process table")
        else:
            for proc in direct:
                if not claude_code.is_tool_shell(proc.command) or proc.pid in own:
                    continue
                work = claude_code.sort_tool_shell(proc.pid, table, kids, tokens,
                                                   also_listener=is_idle_timer)
                if work is None:
                    found.append(_listener_text(proc.pid, table, kids, tokens))
                else:
                    fails.append(f"work: {work}")
        if session.state == claude_code.IDLE_WITH_CHILDREN:
            fails.append("idle with children")
    if facts is None:
        unknowns.append("transcript unreadable: background agents not read")
    else:
        fails.extend(f"background agent {c.id} running: no completion seen"
                     for c in facts.open_background())
    if fails:
        return Check("work", FAIL, _count(fails))
    if unknowns:
        return Check("work", UNKNOWN, "; ".join(unknowns))
    if found:
        return Check("work", PASS, f"listener only ({_count(sorted(set(found)))})")
    return Check("work", PASS, "nothing running")


# 3.3 claim ------------------------------------------------------------------------

#: The slate files a harness may hold, in either vocabulary.
SLATES = ("slices.json", "features.json")
#: Name tokens that name no task: a super's and a GC's seat.
NO_TASK_TOKENS = ("super", "gc")


def _slate_items(data: Any) -> list | None:
    """A slate's list of slices (or features), else None."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("slices", "features"):
            if isinstance(data.get(key), list):
                return data[key]
    return None


def check_claim(sid: str, cwd: str | None, name: str | None = None) -> Check:
    """No claim held, read from the task at the cwd as plain JSON, with no
    taskmaster import and no `tm` subprocess: the claim sidecar
    (`harness/.claim/*.json`: a record naming SID with no `released_at`) and
    the slate (a slice `in_progress` whose `claimed_by` ends in `.SID`).
    A seat whose NAME names a task other than the cwd's is UNKNOWN, as reap
    keeps it. No cwd known is UNKNOWN: with nowhere to look, "no task at
    cwd" would be silence read as evidence."""
    from sessiontools import reap
    if not cwd:
        return Check("claim", UNKNOWN, "no cwd known")
    task, problem = reap.find_task(cwd)
    if problem:
        return Check("claim", UNKNOWN, problem)
    token = reap.name_token(name)
    if (token is not None and token.lower() not in NO_TASK_TOKENS
            and (task is None or token.lower() != task.task_id.lower())):
        return Check("claim", UNKNOWN,
                     f"name says {token}, cwd says {task.task_id if task else 'no task'}")
    if task is None:
        return Check("claim", PASS, "no task at cwd: no claim to hold")
    harness = task.dir / "harness"
    held: list[str] = []
    unknowns: list[str] = []
    claims = harness / ".claim"
    files: list[Path] = []
    try:
        mode = reap.stat_mode(claims)
        if mode is not None and stat.S_ISDIR(mode):
            files = sorted(claims.glob("*.json"))
    except OSError as e:
        unknowns.append(f"claim dir unreadable ({type(e).__name__})")
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            unknowns.append(f"claim {f.name} unreadable ({type(e).__name__})")
            continue
        if not isinstance(rec, dict):
            unknowns.append(f"claim {f.name} unreadable (not an object)")
            continue
        if rec.get("session_id") == sid and rec.get("released_at") is None:
            held.append(str(rec.get("slice_id") or f.stem))
    for slate in SLATES:
        path = harness / slate
        try:
            mode = reap.stat_mode(path)
        except OSError as e:
            unknowns.append(f"{slate} unreadable ({type(e).__name__})")
            continue
        if mode is None or not stat.S_ISREG(mode):
            continue
        try:
            items = _slate_items(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            unknowns.append(f"{slate} unreadable ({type(e).__name__})")
            continue
        if items is None:
            unknowns.append(f"{slate} unreadable (no slice list)")
            continue
        for item in items:
            if (isinstance(item, dict) and item.get("status") == "in_progress"
                    and isinstance(item.get("claimed_by"), str)
                    and item["claimed_by"].endswith(f".{sid}")):
                held.append(str(item.get("id")))
    if held:
        slices = list(dict.fromkeys(held))
        word = "slice" if len(slices) == 1 else "slices"
        return Check("claim", FAIL, f"holds the claim on {word} {_count(slices)} in {task.task_id}")
    if unknowns:
        return Check("claim", UNKNOWN, "; ".join(unknowns))
    return Check("claim", PASS, f"no claim held in {task.task_id}")


# 3.4 mail -------------------------------------------------------------------------

#: The boxes a seat also hears. Read, never resolved.
SUBSCRIBE_ENV = "SESSIONMAIL_SUBSCRIBE"
#: A maildir file's info: `:2,` and its flags. `D` marks a draft.
_INFO = re.compile(r":2,([A-Za-z]*)$")
#: The marks a marks-file line may carry; only `read` makes mail read.
_MARKS = ("seen", "read", "done")
#: No header block is read past this many bytes.
_HEADER_BYTES = 64 * 1024


def subscribed(env: Mapping[str, str] | None = None) -> list[str]:
    """The boxes `SESSIONMAIL_SUBSCRIBE` names, in ENV (default os.environ)."""
    env = os.environ if env is None else env
    return [b for b in re.split(r"[\s,]+", env.get(SUBSCRIBE_ENV) or "") if b]


def _mark(line: str) -> tuple[str, str] | None:
    """(message id, mark) on one marks-file LINE, both forms: the current line
    (`<stamp> <id> <mark> <seat> <session> [reason]`, five fields or more,
    the reason may be empty) and the older four-field one (`<id> <mark>
    <session> <stamp>`). None for a line that is neither."""
    parts = line.split()
    if len(parts) >= 5 and parts[2] in _MARKS:
        return parts[1].strip("<>"), parts[2]
    if len(parts) >= 2 and parts[1] in _MARKS:
        return parts[0].strip("<>"), parts[1]
    return None


def read_ids(sid: str, env: Mapping[str, str] | None = None) -> set[str]:
    """The Message-IDs session SID has marked `read`, from its own marks file
    (`<session dir>/mail/marks`), decoded as sessionmail decodes it. No
    file: none. Raises OSError when the file is there and cannot be read."""
    path = settings.session_dir(sid, env) / "mail" / "marks"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return set()
    return {m[0] for m in map(_mark, text.splitlines()) if m and m[1] == "read"}


def _headers(path: Path) -> tuple[str, str]:
    """(Message-ID, Supersedes) of the mail at PATH, bare. Reads the header
    block only, never the body."""
    from email.parser import BytesHeaderParser
    from email.policy import compat32
    lines = []
    size = 0
    with open(path, "rb") as fh:
        for line in fh:
            if line in (b"\n", b"\r\n") or size > _HEADER_BYTES:
                break
            lines.append(line)
            size += len(line)
    headers = BytesHeaderParser(policy=compat32).parsebytes(b"".join(lines))

    def bare(name: str) -> str:
        return " ".join(str(headers.get(name) or "").split()).strip("<>")
    return bare("Message-ID"), bare("Supersedes")


def check_mail(sid: str, env: Mapping[str, str] | None = None,
               subscribe: list[str] | None = None) -> Check:
    """No unread mail in SID's session box, by the rule `message list
    --unread` keeps: the regular files in `new/` and `cur/`, drafts (the
    `D` flag) not counted, superseded mail hidden, and unread meaning no
    `read` mark in SID's own marks file (`seen` does not count). A message
    with no Message-ID can never be marked read: it is unread, named by its
    file. SUBSCRIBE, the boxes the seat also hears, cannot be read here:
    any at all is UNKNOWN. Only ids are named, never a subject or a body."""
    from sessiontools import operator, reap
    env = os.environ if env is None else env
    root = Path(env.get(operator.SESSION_MAIL_ROOT_ENV) or operator.DEFAULT_MAIL_ROOT).expanduser()
    box = root / sid
    unread: list[str] = []
    unknowns: list[str] = []
    if reap.stat_mode(box) is None:     # raises where the root cannot be searched
        verdict = Check("mail", PASS, "no session box: nothing was ever delivered")
    elif not ((box / "new").is_dir() and (box / "cur").is_dir()):
        verdict = Check("mail", UNKNOWN, "session box unreadable (not a maildir)")
    else:
        try:
            read = read_ids(sid, env)
            files = [p for d in ("new", "cur") for p in sorted((box / d).iterdir())
                     if (mode := reap.stat_mode(p)) is not None and stat.S_ISREG(mode)]
        except OSError as e:
            read, files = set(), []
            unknowns.append(f"session box or marks unreadable ({type(e).__name__})")
        mail: list[tuple[Path, str, str]] = []
        for p in files:
            info = _INFO.search(p.name)
            if info and "D" in info.group(1):
                continue
            try:
                mail.append((p, *_headers(p)))
            except (OSError, ValueError) as e:
                unknowns.append(f"{p.name} unreadable ({type(e).__name__})")
        superseded = {sup for _p, _mid, sup in mail if sup}
        for p, mid, _sup in mail:
            if mid and mid in superseded:
                continue
            if not mid:
                unread.append(p.name)
            elif mid not in read:
                unread.append(f"<{mid}>")
        if unread:
            verdict = Check("mail", FAIL, f"{len(unread)} unread: {_count(unread)}")
        elif unknowns:
            verdict = Check("mail", UNKNOWN, "; ".join(unknowns))
        else:
            verdict = Check("mail", PASS, "session box: 0 unread")
    boxes = subscribed(env) if subscribe is None else subscribe
    if boxes and verdict.word == PASS:
        return Check("mail", UNKNOWN, f"subscribed to {_count(boxes)}: not read")
    return verdict


# 3.5 promises ---------------------------------------------------------------------

class PromiseUsage(ValueError):
    """`--no-promises` with `--promise` or `--handed-on`: a usage error."""


def declare_promises(no_promises: bool = False, promise: list[str] | None = None,
                     handed_on: str | None = None) -> dict:
    """The record's `promises` from the park's flags: `declared` is `none`,
    `open`, `handed on`, or None (nothing declared). Raises PromiseUsage when
    `--no-promises` comes with either of the others."""
    texts = [t for t in (promise or []) if t.strip()]
    if no_promises and (texts or handed_on):
        raise PromiseUsage("--no-promises with --promise or --handed-on")
    if no_promises:
        declared = "none"
    elif handed_on:
        declared = "handed on"
    elif texts:
        declared = "open"
    else:
        declared = None
    return {"declared": declared, "open": texts, "handed_on": handed_on or None}


def check_promises(promises: Mapping[str, Any] | None) -> Check:
    """What the seat DECLARED at its park: nothing on disk records a
    promise, so this measures nothing and says so. Silence is not evidence:
    nothing declared is UNKNOWN."""
    p = promises or {}
    texts = [t for t in p.get("open") or [] if isinstance(t, str)]
    handed = p.get("handed_on")
    if p.get("declared") == "none":
        return Check("promises", PASS, "declared none")
    if isinstance(handed, str) and handed:
        n = f"{len(texts)} " if texts else ""
        return Check("promises", PASS, f"{n}handed on in {handed}")
    if texts:
        more = f" +{len(texts) - 1} more" if len(texts) > 1 else ""
        return Check("promises", FAIL, f"open: {texts[0][:WHY_WIDTH]}{more}")
    return Check("promises", UNKNOWN, "not declared (--no-promises, --promise TEXT, --handed-on REF)")


# --- all five ----------------------------------------------------------------------

@dataclass
class World:
    """What the checks read, injectable. SESSION is the seat's
    sidecar row; FACTS its transcript, read once; OWN_PID this process's pid
    when the seat is its own, else None; ENV holds SESSIONS_DIR and
    SESSION_MAIL_ROOT; SUBSCRIBE the boxes the seat hears (None: read
    SESSIONMAIL_SUBSCRIBE from ENV); GIT reap's git seam (None: the real
    one)."""

    sid: str
    session: Session | None
    table: dict
    facts: Facts | None
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    own_pid: int | None = None
    listeners: object = None
    subscribe: list[str] | None = None
    git: Any = None


def _guarded(name: str, run: Any) -> Check:
    try:
        return run()
    except Exception as e:  # noqa: BLE001 - a check that raises is UNKNOWN, never a crash
        return Check(name, UNKNOWN, type(e).__name__)


def run_checks(world: World, promises: Mapping[str, Any] | None) -> tuple[Check, ...]:
    """The five checks, in CHECK_NAMES order, each run even after an earlier
    one fails. PROMISES is what the park declared (`declare_promises`)."""
    cwd = world.session.cwd if world.session else None
    name = world.session.name if world.session else None
    runs = {
        "tree": lambda: check_tree(cwd, world.git),
        "work": lambda: check_work(world.session, world.table, world.facts,
                                   world.listeners, world.own_pid),
        "claim": lambda: check_claim(world.sid, cwd, name),
        "mail": lambda: check_mail(world.sid, world.env, world.subscribe),
        "promises": lambda: check_promises(promises),
    }
    return tuple(_guarded(n, runs[n]) for n in CHECK_NAMES)


# --- the verb -------------------------------------------------------

#: `session park`'s exit codes.
EXIT_CLEAN = 0        # parked, clean
EXIT_NOT_CLEAN = 1    # parked, not clean (a park still wrote its record)
EXIT_USAGE = 2        # usage, no session id or dir, no such live seat: nothing written
EXIT_NOT_PARKED = 3   # --check: never parked, unparked, or the record unreadable
EXIT_WRITE_FAILED = 4  # park: the write failed, nothing written

#: The flags that make a park; none of them goes with --check.
PARK_FLAGS = (("letter", "--letter"), ("no_promises", "--no-promises"), ("promise", "--promise"),
              ("handed_on", "--handed-on"), ("keep_until", "--keep-until"), ("why", "--why"))

EPILOG = """\
session park writes this seat's record, <session dir>/park.json, and prints
what --check prints. It runs five checks, each PASS, FAIL or UNKNOWN, and
the seat is clean only when all five PASS (UNKNOWN is never PASS):
  tree      no unsaved work in the task's worktree/ and store (or the cwd)
  work      nothing running but listeners and the idle timer
  claim     no claim held in the cwd's task
  mail      no unread mail in the session box
  promises  what this park declares: --no-promises, --promise TEXT
            (open), or --handed-on REF (handed on). Saying nothing is UNKNOWN.
work_ended_at is the end of the last real work, never the park: parking
again with no work since keeps it, so parking buys no time. Nothing here
computes a deadline, kills, mails or writes config.

--keep-until T --why R records a request to be kept until T, an absolute
ISO 8601 time with an offset (2030-06-09T17:00:00+00:00, or Z). Nothing
acts on it here. A park without it clears it.

--check [SEAT] writes nothing. It reads the record and runs tree, work,
claim and mail again now. A turn that started after the park, or work done
after it, means not parked. Read another seat by name, repo or session-id
prefix; checking your own seat from a later turn reads not parked, because
that turn unparked it. Run `session park` as a call of its own: work
chained after it unparks it.

exit codes:
  0  parked, clean
  1  parked, not clean (park: the record is written; fix and park again)
  2  usage, no session id, no session dir, or no such live seat: nothing written
  3  --check only: not parked (never parked, unparked, or the record unreadable)
  4  park only: the write failed, nothing written
An internal error also exits 2, never 0 or 1: it is no verdict.
"""


@dataclass(frozen=True)
class Report:
    """The one verdict both renders print. RECORD is the park as read (or
    as just written); WHY_NOT_PARKED is None while it holds; UNPARKED, when
    set, is what ended it; CHECKS are the checks as read now (a park's own,
    for the park that wrote them)."""

    sid: str
    name: str | None
    record: Record | None
    why_not_parked: str | None
    checks: tuple[Check, ...]
    unparked: Unparked | None = None

    @property
    def parked(self) -> bool:
        return self.record is not None and self.why_not_parked is None

    @property
    def clean(self) -> bool:
        """True only when every check reads PASS: UNKNOWN is never PASS."""
        return len(self.checks) == len(CHECK_NAMES) and all(c.word == PASS for c in self.checks)

    def exit_code(self) -> int:
        if not self.parked:
            return EXIT_NOT_PARKED
        return EXIT_CLEAN if self.clean else EXIT_NOT_CLEAN


def report(sid: str, name: str | None, record: Record | None, read_why: str,
           facts: Facts | None, session: Session | None,
           checks: tuple[Check, ...]) -> Report:
    """The verdict for --check: no record is not parked, for READ_WHY (`never
    parked`, `park.json unreadable: ...`); a record is parked until
    `unparked` names what ended it."""
    if record is None:
        return Report(sid, name, None, read_why or "never parked", checks)
    gone = unparked(record, facts, session)
    return Report(sid, name, record, gone.why if gone else None, checks, gone)


def render_text(rep: Report) -> str:
    lines = []
    if rep.parked and rep.record is not None:
        r = rep.record
        ended = clock(r.work_ended_at) if r.work_ended_at is not None else "unknown"
        lines.append(f"parked: yes, at {clock(r.parked_at)} (work ended {ended}: {r.work_ended_why})")
    else:
        lines.append(f"parked: no ({rep.why_not_parked})")
    lines.append(f"clean: {'yes' if rep.clean else 'no'}")
    lines += [f"  {c.name:<10}{c.word:<9}{c.why}" for c in rep.checks]
    if rep.parked and rep.record is not None and rep.record.keep_until is not None:
        lines.append(f"keep until: {stamp(rep.record.keep_until)} (why: {rep.record.keep_why})")
    return "\n".join(lines)


def render_json(rep: Report) -> str:
    gone = rep.unparked
    return json.dumps({
        "v": VERSION,
        "session_id": rep.sid,
        "name": rep.name,
        "parked": rep.parked,
        "why_not_parked": rep.why_not_parked,
        "turn": ({"kind": gone.kind, "at": gone.at, "origin": gone.origin} if gone else None),
        "clean": rep.clean,
        "checks": [c.to_dict() for c in rep.checks],
        "record": rep.record.to_dict() if rep.record is not None else None,
    })


def parse_keep_until(text: str, now: float) -> float:
    """T for --keep-until, as epoch seconds: ISO 8601 WITH an offset (or Z),
    and later than NOW. A relative form (`+2h`), a T with no offset, and a
    past T are refused (ValueError): the record holds a request, and park
    computes no time."""
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"{text!r} is not an ISO 8601 time (absolute, with an offset)") from None
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(f"{text!r} has no offset (add +HH:MM or Z)")
    at = when.timestamp()
    if at <= now:
        raise ValueError(f"{text!r} is not in the future")
    return at


def _other_seat_mail(checks: tuple[Check, ...], record: Record | None) -> tuple[Check, ...]:
    """Another seat's mail check, read from here. Its subscriptions live in
    ITS environment, which this process cannot read. They are fixed for the
    life of a seat, and its own park read them: a park-time mail PASS proves
    it had none. Without that proof a live PASS is UNKNOWN."""
    proven = record is not None and any(c.name == "mail" and c.word == PASS for c in record.checks)
    if proven:
        return checks
    return tuple(Check("mail", UNKNOWN, f"{c.why}; subscriptions not known from another seat "
                                        "(no park with mail PASS)")
                 if c.name == "mail" and c.word == PASS else c for c in checks)


def _err(text: str) -> None:
    print(f"session park: {text}", file=sys.stderr)


def main(argv: list[str], *, env: Mapping[str, str] | None = None,
         rows: Any = None, table: Any = None, now: float | None = None,
         listeners: Any = None, own_pid: int | None = None, git: Any = None) -> int:
    """`session park` and `session park --check [SEAT]`. ENV, ROWS (a callable
    giving `listing.rows()`), TABLE (a callable giving the process table with
    commands), NOW, LISTENERS (a callable giving the listener list), OWN_PID
    and GIT are seams for tests; by default the real ones.

    An unexpected error exits 2, never 0 or 1: Python's own traceback exit
    is 1, which --check documents as "parked, not clean", and a scripted
    caller must not read a crash as a park."""
    try:
        return _main(argv, env=env, rows=rows, table=table, now=now,
                     listeners=listeners, own_pid=own_pid, git=git)
    except Exception as e:  # noqa: BLE001 - any crash is no verdict, exit 2
        _err(f"internal error ({type(e).__name__}: {e}): no verdict")
        return EXIT_USAGE


def build_parser() -> argparse.ArgumentParser:
    """`session park`'s parser."""
    p = argparse.ArgumentParser(prog="session park", epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Say this seat has settled, and record what the "
                                            "checks found; or, with --check, read it back.")
    # SEAT is a positional of its own, not --check's optional value: with
    # `nargs="?"` on --check, `--check --json SEAT` left SEAT unclaimed and
    # argparse refused it. Any order of --check, --json and SEAT now parses.
    p.add_argument("--check", action="store_true",
                   help="write nothing: read the park back (this seat, or SEAT)")
    p.add_argument("seat", nargs="?", metavar="SEAT",
                   help="with --check: the live seat to read (name, repo or session-id prefix)")
    p.add_argument("--letter", metavar="REF", help="the letter or handover: a Message-ID or a path")
    p.add_argument("--no-promises", action="store_true", help="declare: no promises open")
    p.add_argument("--promise", action="append", metavar="TEXT",
                   help="declare an open promise (repeatable)")
    p.add_argument("--handed-on", metavar="REF", help="declare the promises handed on, in REF")
    p.add_argument("--keep-until", metavar="T", help="ask to be kept until T (needs --why)")
    p.add_argument("--why", metavar="R", help="why --keep-until")
    p.add_argument("--json", action="store_true", help="one JSON object")
    return p


def _main(argv: list[str], *, env: Mapping[str, str] | None, rows: Any, table: Any,
          now: float | None, listeners: Any, own_pid: int | None, git: Any) -> int:
    p = build_parser()
    a = p.parse_args(argv)

    from sessiontools import claude_code
    env = dict(os.environ) if env is None else env
    clock_now = time.time if now is None else (lambda: now)
    if rows is None:
        from sessiontools import listing

        def rows() -> list:
            return listing.rows(env)
    if table is None:
        from sessiontools import procs

        def table() -> dict:
            return procs.table(commands=True)
    if listeners is None:
        from sessiontools import pane_state

        def listeners() -> object:
            return pane_state.listeners(env)

    if a.seat is not None and not a.check:
        p.error(f"SEAT goes with --check (got {a.seat!r}): a park writes this seat only")
    if a.check:
        used = [flag for attr, flag in PARK_FLAGS if getattr(a, attr)]
        if used:
            p.error(f"--check takes no park flags (got {', '.join(used)})")
        return _check(a.seat or None, a.json, env, rows, table, listeners,
                      os.getpid() if own_pid is None else own_pid, git)

    try:
        promises = declare_promises(a.no_promises, a.promise, a.handed_on)
    except PromiseUsage as e:
        p.error(str(e))
    keep_until = None
    if a.why is not None and a.keep_until is None:
        p.error("--why goes with --keep-until")
    if a.keep_until is not None:
        if not (a.why or "").strip():
            p.error("--keep-until needs --why R")
        try:
            keep_until = parse_keep_until(a.keep_until, clock_now())
        except ValueError as e:
            p.error(f"--keep-until: {e}")

    sid = claude_code.session_id(env)
    if not sid:
        _err("no session id in the environment: nothing written")
        return EXIT_USAGE
    if not settings.session_dir(sid, env).is_dir():
        _err(f"no session dir for {sid}: nothing written")
        return EXIT_USAGE
    session, name = _own_seat(sid, env, rows)
    cwd = session.cwd if session else None
    facts = read_transcript(sid, cwd, env)
    world = World(sid, session, table(), facts, env,
                  os.getpid() if own_pid is None else own_pid, listeners(), None, git)
    checks = run_checks(world, promises)
    prior, _why = read(record_path(sid, env), sid)
    parked_at = clock_now()
    ended, ended_why = work_ended(facts, parked_at, prior,
                                  session.started_at if session else None)
    record = Record(sid, parked_at, ended, ended_why, checks, a.letter,
                    keep_until, a.why.strip() if keep_until is not None and a.why else None,
                    promises)
    try:
        write(record, env)
    except NoSessionDir as e:
        _err(f"no session dir ({e}): nothing written")
        return EXIT_USAGE
    except OSError as e:
        _err(f"the write failed ({type(e).__name__}: {e}): nothing written")
        return EXIT_WRITE_FAILED
    rep = Report(sid, name, record, None, checks)
    print(render_json(rep) if a.json else render_text(rep))
    return EXIT_CLEAN if rep.clean else EXIT_NOT_CLEAN


def _own_seat(sid: str, env: Mapping[str, str], rows: Any) -> tuple[Session | None, str | None]:
    """This seat's sidecar and name: its live row, else its sidecar alone."""
    from sessiontools import claude_code
    try:
        found = rows()
    except Exception:  # noqa: BLE001 - no listing is no row; the sidecar is read below
        found = []
    row = next((r for r in found if r.session.id == sid), None)
    if row is not None:
        return row.session, row.name
    session = claude_code.find(sid, dict(env))
    return session, (session.name if session else None)


def _check(seat: str | None, as_json: bool, env: Mapping[str, str], rows: Any, table: Any,
           listeners: Any, own_pid: int, git: Any) -> int:
    """`session park --check [SEAT]`: reads only, writes nothing."""
    from sessiontools import claude_code, listing
    me = claude_code.session_id(env)
    if seat:
        found = rows()
        row = listing.match(found, seat)
        if row is None:
            _err(f"no live seat matches {seat!r} ({len(found)} live; see `session list`)")
            return EXIT_USAGE
        sid, session, name = row.session.id, row.session, row.name
    else:
        if not me:
            _err("no SEAT given and no session id in the environment")
            return EXIT_USAGE
        sid = me
        session, name = _own_seat(sid, env, rows)
    own = sid == me
    record, read_why = read(record_path(sid, env), sid)
    facts = read_transcript(sid, session.cwd if session else None, env)
    world = World(sid, session, table(), facts, env, own_pid if own else None, listeners(),
                  None if own else [], git)
    checks = run_checks(world, record.promises if record else None)
    if not own:
        checks = _other_seat_mail(checks, record)
    rep = report(sid, name, record, read_why, facts, session, checks)
    print(render_json(rep) if as_json else render_text(rep))
    return rep.exit_code()
