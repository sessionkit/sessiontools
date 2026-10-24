"""`session get`: read one property of a live seat.

The properties and output are the session repo's `session-get` (retired
2030-06-09). `model` reads the transcript (`sessiontools.window`: the last
real model id, never `<synthetic>`); the rest come from `session list`.
Exit codes: 0 found; 1 unknown PROP; 2 no such seat, or no value for PROP.
(argparse usage errors are 2.) A SEAT is matched by the `session send`
rule; no SEAT means this session, from the environment.
"""

from __future__ import annotations

import json
import time

from sessiontools import window
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
)


class GetError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def table() -> str:
    lines = [f"{'PROP':8} {'SETTABLE':9} SOURCE"]
    lines += [f"{p:8} {'no':9} {src}" for p, src in PROPS]
    return "\n".join(lines)


def value(row: Row, prop: str, now: float | None = None) -> object:
    s = row.session
    if prop == "model":           # a transcript read: only when asked for
        return window.of(s.id, s.cwd).model
    now = time.time() if now is None else now
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
