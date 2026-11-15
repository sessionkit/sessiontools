"""`session get`: read one property of a live seat.

The properties and output are the session repo's `session-get` (retired
2030-06-09). `model` reads the transcript (`sessiontools.window`: the last
real model id, never `<synthetic>`); the rest come from `session list`.
`last_request_at` and `cache_age_s` read it too: the last real-model
usage row's `timestamp` (the response's END, ISO-8601 UTC) and the seconds
since, so a caller keeps a margin before the cache TTL (`window`'s CACHE
CLOCK). They are readings only: nothing acts on them.
`launch_command` and `launch_dir` come from the session's launch record
(`<session dir>/launch/command`): the `session start` line as the
human ENTERED it, and the directory it was entered from. `cwd` is still
where the agent is now. A seat launched before the record held them, or
with no `launch/` dir, has no value: exit 2, never a fallback to the
evaluated `start:` or `vendor:` lines. A RESUMED seat reports its ORIGINAL
launch_command, or none (a keyed resume forks into a dir with no record):
the resume line is never recorded, so respawning one starts a fresh seat
of the same agent.
Exit codes: 0 found; 1 unknown PROP; 2 no such seat, or no value for PROP.
(argparse usage errors are 2.) A SEAT is matched by the `session send`
rule; no SEAT means this session, from the environment.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Mapping

from sessiontools import settings, window
from sessiontools.claude_code import session_id
from sessiontools.listing import Row, match

#: (prop, source) in the order `session get` lists them.
PROPS = (
    ("id", "session list"),
    ("short", "session list"),
    ("pid", "session list"),
    ("name", "session list"),
    ("agent", "session list"),
    ("cwd", "session list"),
    ("status", "session list: the harness's own word"),
    ("state", "session list: busy, idle with children or idle"),
    ("idle", "session list: 0 while busy, else seconds since the state began"),
    ("version", "session list"),
    ("pane", "session list"),
    ("model", "the transcript: the last real model id"),
    ("launch_command", "the launch record: the session start line as ENTERED"),
    ("launch_dir", "the launch record: the directory it was entered from"),
    ("last_request_at", "the transcript: the last real-model usage row's timestamp, UTC"),
    ("cache_age_s", "the transcript: seconds since last_request_at; keep a margin"),
)

#: The props read from `<session dir>/launch/command`.
LAUNCH_PROPS = ("launch_command", "launch_dir")

#: The props read from the transcript's cache clock (`window`).
CACHE_PROPS = ("last_request_at", "cache_age_s")


class GetError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def table() -> str:
    w = max(len(p) for p, _ in PROPS)
    lines = [f"{'PROP':{w}} {'SETTABLE':9} SOURCE"]
    lines += [f"{p:{w}} {'no':9} {src}" for p, src in PROPS]
    return "\n".join(lines)


def launch_value(row: Row, prop: str, env: Mapping[str, str] | None = None) -> str:
    """`launch_command` or `launch_dir` from the seat's launch record. A
    missing file, a missing key or an unreadable record is exit 2: a seat
    launched before the record held them. It never falls back to `start:`
    or `vendor:`: the evaluated argv carries `--session-id`, so replaying it
    resumes the old seat. ENV picks the sessions leaf (None: this process's
    environment), so a caller that names the record's path reads that one."""
    from sessiontools.start import record
    absent = GetError(f"session get: no {prop} recorded for {row.name} "
                      "(launched before it was recorded)", 2)
    try:
        text = (settings.session_dir(row.session.id, env) / "launch" / "command").read_text()
    except (OSError, UnicodeError):
        raise absent from None
    v = record.parse_command(text).get(prop)
    if not v:
        raise absent
    if prop == "launch_dir":      # stored shlex-quoted: one word
        try:
            words = shlex.split(v)
        except ValueError:
            raise absent from None
        if len(words) != 1:
            raise absent
        return words[0]
    return v                      # shell text, printed as it is


def value(row: Row, prop: str, now: float | None = None) -> object:
    s = row.session
    if prop in LAUNCH_PROPS:      # a file read: only when asked for
        return launch_value(row, prop)
    if prop == "model":           # a transcript read: only when asked for
        return window.of(s.id, s.cwd).model
    now = time.time() if now is None else now
    if prop in CACHE_PROPS:       # a transcript read: only when asked for
        f = window.of(s.id, s.cwd)
        return f.last_request_at if prop == "last_request_at" else f.cache_age_s(now)
    return {
        "id": s.id, "short": row.short, "pid": s.pid, "name": row.name,
        "agent": s.agent, "cwd": s.cwd, "status": s.raw_status,
        "state": s.state, "idle": int(s.idle_seconds(now)), "version": s.version, "pane": row.tmux_pane,
    }[prop]


def resolve(found: list[Row], seat: str | None, env: dict[str, str] | None = None) -> Row:
    if seat:
        row = match(found, seat)
        if row is None:
            raise GetError(f"session get: no live seat matches {seat!r} "
                           f"({len(found)} live; see `session list`)", 2)
        return row
    sid = session_id(env)
    for row in found:
        if sid and row.session.id == sid:
            return row
    raise GetError("session get: no SEAT given and this session is not a live one "
                   "(no CLAUDE_CODE_SESSION_ID, or no sidecar for it)", 2)


def get(found: list[Row], prop: str, seat: str | None, as_json: bool,
        env: dict[str, str] | None = None) -> str:
    if prop not in dict(PROPS):
        raise GetError(f"session get: unknown PROP {prop!r}; one of: "
                       + ", ".join(p for p, _ in PROPS), 1)
    row = resolve(found, seat, env)
    v = value(row, prop)
    if v is None:
        raise GetError(f"session get: no {prop} known for {row.name}", 2)
    if not as_json:
        return str(v)
    out = {"session_id": row.session.id, "name": row.name, prop: v}
    if prop == "idle":
        out["status"] = row.session.raw_status
        out["state"] = row.session.state
    return json.dumps(out)
