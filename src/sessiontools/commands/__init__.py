"""`session commands`: the Bash commands a seat's sessions ran, cut down to
the programs.

The ledger says what each command put in the window; this says what RAN.
It is a view of the same items: `ledger.transcript.read` reads the
transcript (its reader, not a second one), the Bash items are kept, and
`commands.shell.programs` cuts each to its programs. Nothing is counted in
tokens here.

A call is listed once, by its tool-use id, however many transcripts hold its
line (a resumed session copies its parent's). A call with no result in the
transcript (cut off, or still running) is not an item of the ledger's and is
not listed. A subagent's calls are its session's: they are read from the
subagents' transcripts and marked.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sessiontools import past, window
from sessiontools.commands import shell
from sessiontools.ledger import transcript as _transcript


@dataclass(frozen=True)
class Call:
    """One Bash call: when its result came back, whose it was, as written."""

    ts: str                 # the transcript's own UTC timestamp, or ""
    session: str
    subagent: bool
    command: str
    id: str | None

    def programs(self) -> list[shell.Program]:
        return shell.programs(self.command)


def resolve(target: str, env: Mapping[str, str] | None = None) -> tuple[str, list[past.Past]]:
    """`(label, sessions)` for TARGET: a seat with recorded sessions is all
    of them; else every session whose id starts with TARGET. Empty: neither."""
    rows = past.sessions(target, env)
    if rows:
        return target, rows
    ids = sorted({p.stem for p in window.projects_dir(env).glob(f"*/{target}*.jsonl")})
    rows = [past.Past(id=sid, seat=past.seat_of(sid, env) or "?", start=None, last_write=None,
                      turns=None, transcripts=past.transcripts_of(sid, env)) for sid in ids]
    return (rows[0].seat if len(rows) == 1 else "?"), rows


def calls(rows: list[past.Past], since: str | None = None,
          until: str | None = None) -> Iterator[Call]:
    """Every Bash call of these sessions, each once, in file order. SINCE
    and UNTIL are UTC `...Z` strings (`utc()`); a transcript not written
    since SINCE is not opened."""
    seen: set[str] = set()
    floor = datetime.fromisoformat(since).timestamp() if since else None
    for row in rows:
        for main in row.transcripts:
            for path, sub in [(main, False)] + [(p, True) for p in past.subagents_of(main)]:
                try:
                    if floor is not None and path.stat().st_mtime < floor:
                        continue
                    raws = _transcript.read(path, sidechain=sub).raws
                except OSError:
                    continue
                for r in raws:
                    ts = r.ts or ""
                    if r.kind != "Bash" or (r.tool_use_id and r.tool_use_id in seen):
                        continue
                    if (since and ts < since) or (until and ts >= until):
                        continue
                    if r.tool_use_id:
                        seen.add(r.tool_use_id)
                    yield Call(ts=ts, session=row.id, subagent=sub, command=r.cmd or "",
                               id=r.tool_use_id)


def utc(when: str) -> str:
    """`7d`, `48h`, `30m`, `2030-06-09` or `2030-06-09T05:00` (local time)
    as the UTC string a transcript's timestamps sort against: to the
    millisecond, as they are, so a call half a second after WHEN is after
    it. ValueError: none of those."""
    m = re.fullmatch(r"(\d+)([dhm])", when)
    if m:
        unit = {"d": "days", "h": "hours", "m": "minutes"}[m.group(2)]
        t = datetime.now().astimezone() - timedelta(**{unit: int(m.group(1))})
    else:
        t = datetime.fromisoformat(when)
        t = t.astimezone() if t.tzinfo is None else t
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def local(ts: str, fmt: str = "%a %H:%M") -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime(fmt)
    except ValueError:
        return "?"


def kept(call: Call, grep: re.Pattern | None) -> list[shell.Program]:
    """CALL's programs without bare `cd`; [] when GREP matches none of them."""
    progs = [p for p in call.programs() if p.name != "cd"]
    return progs if grep is None or any(grep.search(p.label) for p in progs) else []


