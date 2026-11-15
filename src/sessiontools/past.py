"""A seat's past sessions on this host: `session list --past SEAT`.

`session list` shows the live seats. This reads what stays behind when a
session ends, and nothing a live seat needs:

    which seat    the first of two records that names one (`seat_of`):
                  `settings/session.json`'s "agent", which `session start`
                  writes, then the identity block's Seat line. Both are
                  sessiontools' own. A session neither names is not listed:
                  its seat is unknown here. (sessionmail's birth certificate
                  names more sessions, and is NOT read: sessionmail depends
                  on this package, never the reverse. ST-320, ruled.)
    start         the first timestamp in its transcript; with no transcript
                  on this host, when its session folder was made.
    last write    its newest transcript's modification time, or None.
    turns         the user's turns in its transcripts: prompts, never tool
                  results or a subagent's lines. None with no transcript.

Reads only. One pass over each transcript; a line is parsed only when it
could be a prompt, so a 10 MB transcript costs milliseconds.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sessiontools import identity_block, settings, window

_SEAT_LINE = re.compile(r"^- \*\*Seat\*\*: *(\S+)", re.MULTILINE)


@dataclass(frozen=True)
class Past:
    id: str
    seat: str
    start: float | None            # epoch seconds
    last_write: float | None       # epoch seconds
    turns: int | None
    transcripts: tuple[Path, ...]


def seat_of(sid: str, env: Mapping[str, str] | None = None) -> str | None:
    """The seat SID ran as, from the first record that names one; None when
    none does."""
    home = settings.session_dir(sid, env)
    try:
        agent = json.loads((home / "settings" / "session.json").read_text("utf-8")).get("agent")
        if isinstance(agent, str) and agent.strip():
            return agent.strip()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        m = _SEAT_LINE.search((home / identity_block.IDENTITY_FILE).read_text("utf-8", "replace"))
    except OSError:
        return None
    return m.group(1) if m and m.group(1) != "none" else None


def transcripts_of(sid: str, env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """SID's transcripts under every project folder, oldest first by name."""
    return tuple(sorted(window.projects_dir(env).glob(f"*/{sid}.jsonl")))


def subagents_of(transcript: Path) -> tuple[Path, ...]:
    """The transcripts of the subagents TRANSCRIPT's session started."""
    return tuple(sorted((transcript.parent / transcript.stem / "subagents").glob("*.jsonl")))


def sessions(seat: str, env: Mapping[str, str] | None = None) -> list[Past]:
    """Every recorded session of SEAT, oldest first."""
    root = settings.sessions_root(env)
    found: list[Past] = []
    for home in root.iterdir() if root.is_dir() else ():
        sid = home.name
        if not home.is_dir() or seat_of(sid, env) != seat:
            continue
        paths = transcripts_of(sid, env)
        start, turns = _start_and_turns(paths)
        found.append(Past(
            id=sid, seat=seat,
            start=start if start is not None else home.stat().st_mtime,
            last_write=max((p.stat().st_mtime for p in paths), default=None),
            turns=turns, transcripts=paths))
    return sorted(found, key=lambda p: (p.start or 0, p.id))


def _start_and_turns(paths: tuple[Path, ...]) -> tuple[float | None, int | None]:
    start: float | None = None
    turns = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if start is None and '"timestamp"' in line:
                        start = _epoch(_field(line, "timestamp"))
                    if '"user"' in line and '"tool_use_id"' not in line \
                            and _is_prompt(line):
                        turns += 1
        except OSError:
            continue
    return start, (turns if paths else None)


def _field(line: str, key: str):
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec.get(key) if isinstance(rec, dict) else None


def _is_prompt(line: str) -> bool:
    try:
        rec = json.loads(line)
    except ValueError:
        return False
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return False
    if rec.get("isMeta") is True or rec.get("isSidechain") is True:
        return False
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def _epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except ValueError:
        return None


def _clock(epoch: float | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone().strftime("%a %d %b %H:%M")


def render_table(rows: list[Past]) -> str:
    lines = [f"{'ID':8}  {'START':16}  {'LAST WRITE':16}  {'TURNS':>5}"]
    for r in rows:
        turns = "-" if r.turns is None else str(r.turns)
        lines.append(f"{r.id[:8]}  {_clock(r.start):16}  {_clock(r.last_write):16}  {turns:>5}")
    return "\n".join(lines)


def render_json(rows: list[Past]) -> str:
    return json.dumps([{"id": r.id, "seat": r.seat, "start": r.start,
                        "last_write": r.last_write, "turns": r.turns,
                        "transcripts": [str(p) for p in r.transcripts]} for r in rows], indent=2)
