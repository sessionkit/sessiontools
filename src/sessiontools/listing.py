"""`session list`: the live sessions as rows, a table or JSON.

The rows are the library's `live()` sessions, each with its git repo and the
tmux pane it runs in. The JSON keys and the table columns are the ones the
callers were written against (`session-list` in the session repo,
retired 2030-06-09): some read `agent` and `status`,
voicemode reads `session_id`, `pid` and `tmux_pane`, seat-hearing reads the
table's first two columns after its two header lines.

CONTEXT is how full each seat's window is (`sessiontools.window`): the
table's last column, and five `context_*` JSON keys - the ones sessionview's
sidebar reads. It costs a transcript read per seat, so it is filled only by
`with_context()`, which `session list` calls unless `--no-context`; `get`,
`send` and `--pane` never pay for it.

STATE is the seat's state in our words: `busy`, `idle with
children` (the harness says busy, but the turn is over and a background
agent runs) or `idle`. A column after STATUS and a `state` JSON key; STATUS
and `status` keep the harness's own word, as callers read it.

NOT PORTED YET, and said so rather than faked: the per-session /recap
sub-line. Its JSON key is present and null (null is "not known", never
zero). `--no-recap` is accepted, so no caller breaks.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

from sessiontools import procs as _procs
from sessiontools import tmux as _tmux
from sessiontools import window as _window
from sessiontools.claude_code import Session, live
from sessiontools.window import Fullness

NOT_PORTED_KEYS = ("recap",)


@cache
def repo_name(cwd: str) -> str:
    """The git repo the cwd is in, else the cwd's basename. A REPO, not a
    project: every taskmaster seat runs in the store checkout.

    Found in-process, with no git fork: one `git rev-parse --show-toplevel`
    per seat cwd was the largest fork source left in `session list`.
    The walk keeps rev-parse's answers: the toplevel is the first directory,
    from the cwd's real path up, holding a `.git` entry git would accept - a
    directory with a HEAD (a plain repo), or a `gitdir:` FILE whose target
    exists (a linked worktree, a submodule). A `.git` directory with no HEAD
    is passed over, as git does; a broken `.git` file stops the walk, as git
    fails there. A cwd that is gone or cannot be entered is no repo: git
    cannot `-C` into it."""
    top = _toplevel(cwd)
    return top.name if top is not None else (Path(cwd).name or cwd)


def _toplevel(cwd: str) -> Path | None:
    """The work tree root rev-parse would print for CWD, or None."""
    if not cwd or not os.path.isdir(cwd) or not os.access(cwd, os.X_OK):
        return None
    here = Path(os.path.realpath(cwd))
    for d in (here, *here.parents):
        dotgit = d / ".git"
        try:
            if dotgit.is_dir():
                if (dotgit / "HEAD").is_file():
                    return d
                continue
            if dotgit.is_file():
                return d if _gitfile_points_somewhere(dotgit) else None
        except OSError:
            continue
    return None


def _gitfile_points_somewhere(dotgit: Path) -> bool:
    """A `.git` FILE reads `gitdir: <path>`, relative to its own folder or
    absolute; git refuses one whose target is not a directory, or that it
    cannot stat. The stat is explicit: from 3.14 `Path.is_dir()` returns
    False there, and up to 3.13 it raised and the walk climbed on past the
    `.git` file, where git stops."""
    try:
        text = dotgit.read_text(errors="replace")
    except OSError:
        return False
    first = text.splitlines()[0] if text else ""
    if not first.startswith("gitdir:"):
        return False
    target = first[len("gitdir:"):].strip()
    if not target:
        return False
    try:
        return stat.S_ISDIR((dotgit.parent / target).stat().st_mode)
    except (OSError, ValueError):
        return False


def fmt_duration(seconds: float) -> str:
    """'45s', '58m', '10h46m', '1d10h'."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


@dataclass(frozen=True)
class Row:
    session: Session
    repo: str
    tmux_socket: str | None
    tmux_pane: str | None
    started: float | None       # the process's start, epoch seconds
    fullness: Fullness | None = None  # None until with_context(): not asked

    @property
    def short(self) -> str:
        return self.session.id[:8]

    @property
    def name(self) -> str:
        return self.session.name or self.session.agent or self.short

    def runtime_s(self, now: float) -> int:
        return int(now - self.started) if self.started is not None else 0

    def idle_s(self, now: float) -> int:
        """Since the state began - a busy seat's IDLE is how long it has been
        busy, as the table always showed it; an `idle with children` seat's
        is since its turn ended. (`session get idle` is the other number: 0
        while busy.)"""
        return max(0, int(now - self.session.since))

    def to_dict(self, now: float) -> dict:
        s = self.session
        row = {
            "session_id": s.id,
            "short": self.short,
            "pid": s.pid,
            "name": self.name,
            "agent": s.agent,
            "status": s.raw_status,
            "state": s.state,
            "repo": self.repo,
            "project": self.repo,          # deprecated alias of repo; callers still read it
            "cwd": s.cwd,
            "version": s.version,
            "tmux_pane": self.tmux_pane,
            "tmux_socket": self.tmux_socket,
            "runtime_s": self.runtime_s(now),
            "idle_s": self.idle_s(now),
        }
        row.update(dict.fromkeys(NOT_PORTED_KEYS))
        row.update((self.fullness or _window.UNKNOWN).fields())
        return row


def rows(env: dict[str, str] | None = None) -> list[Row]:
    """Every live session as a Row, newest status first."""
    table = _procs.table()
    index = _tmux.pane_index()
    found = []
    for s in live(env, procs=table):
        where = _tmux.locate(s.pid, table, index)
        proc = table.get(s.pid) if s.pid is not None else None
        found.append(Row(session=s, repo=repo_name(s.cwd or ""),
                         tmux_socket=where[0] if where else None,
                         tmux_pane=where[1] if where else None,
                         started=proc.started if proc else None))
    return found


def with_context(found: list[Row], env: dict[str, str] | None = None) -> list[Row]:
    """The rows with CONTEXT filled - one transcript read per seat. A seat
    that cannot be read is unknown; it never costs the listing."""
    return [replace(r, fullness=_window.of(r.session.id, r.session.cwd, env)) for r in found]


def match(found: list[Row], query: str) -> Row | None:
    """The `session send` rule, first match wins: QUERY inside the name or
    the repo, or a prefix of the session id. Case-insensitive."""
    q = query.lower()
    for r in found:
        if q in r.name.lower() or q in r.repo.lower() or r.session.id.lower().startswith(q):
            return r
    return None


def match_id_first(found: list[Row], query: str) -> Row | None:
    """A session id prefix first, then `match`: `session context 50` is
    session 50c009c5 before it is the seat named alice-50."""
    q = query.lower()
    return next((r for r in found if r.session.id.lower().startswith(q)), None) or match(found, query)


def render_table(found: list[Row], now: float | None = None) -> str:
    if not found:
        return "No running sessions found."
    now = time.time() if now is None else now
    header = (f"{'SHORT':10} {'NAME':28} {'STATUS':8} {'STATE':18} {'REPO':16} "
              f"{'PANE':6} {'RUNTIME':>8} {'IDLE':>8} {'CONTEXT':>7}")
    lines = [header, "-" * len(header)]
    for r in found:
        lines.append(f"{r.short:10} {r.name[:28]:28} {(r.session.raw_status or '-'):8} "
                     f"{r.session.state:18} {r.repo[:16]:16} {(r.tmux_pane or '-'):6} "
                     f"{fmt_duration(r.runtime_s(now)):>8} {fmt_duration(r.idle_s(now)):>8} "
                     f"{_window.cell(r.fullness or _window.UNKNOWN):>7}")
    return "\n".join(lines)


def render_json(found: list[Row], now: float | None = None) -> str:
    now = time.time() if now is None else now
    return json.dumps([r.to_dict(now) for r in found], indent=2)
