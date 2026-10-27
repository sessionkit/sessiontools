"""`session list` computes once: a single-flight snapshot every caller reads.

Three pollers on one host ran `session list` back to back, each
paying for the whole listing. Compute it once; caching is the DEFAULT. So `session list` keeps its last result in
a state-dir file and serves it while it is young:

- THE FILE: `<state dir>/list/snapshot.json`, written atomically (a temp
  file in the same folder, then rename), so a reader sees the old file or
  the new one, never half of one. The folder is 0700 and the file 0600: it
  names every seat's cwd.
- THE STAMP: a snapshot carries `SCHEMA`, the package version and a key of
  the environment that shapes the listing (HOME, the sidecar, tmux and
  transcript folders, CONTEXT_MAX, the uid). Any difference and it is not
  served: never across an upgrade, never another world's answer.
- SINGLE FLIGHT: a caller that finds no young snapshot takes `list.lock`
  (flock) and checks again before it computes. A caller that arrives while
  another computes blocks on the lock and reads that one's result: it
  accepts a snapshot younger than MAX AGE, or one that finished after it
  arrived. K concurrent callers, one computation. The lock is waited on
  for LOCK_WAIT_S at most; past that the caller computes on its own (a hung
  lister never hangs every caller).
- MAX AGE: `$SESSION_LIST_MAX_AGE` (seconds), else `list.max_age` from the
  `session` tool's settings (`~/.sessionkit/tools/settings/session.json`, the
  template and host layers), else DEFAULT_MAX_AGE. 0 turns the cache off.
- FRESH: `--fresh`, or `$SESSION_LIST_FRESH` set to a true word, computes
  now, without waiting on anyone, and still writes the snapshot.
- THE TURN MARK: a row read `idle with children` or `busy` from a
  turn-ended mark the hook may set or clear at any moment, and the state
  must show within 2 s of either. A served snapshot is held to the marks
  on disk (`claude_code.mark_current`, one small read per busy seat): if
  any mark no longer gives its row's reading, the snapshot is stale.
- A snapshot that is missing, unreadable, corrupt, stale, from another
  schema, version or world, or without CONTEXT when CONTEXT is asked for,
  is recomputed. The cache is an optimisation, never an error: a state dir
  that cannot be written only costs the caller its own computation.
"""

from __future__ import annotations

import errno
import fcntl
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any

from sessiontools.claude_code import Session, mark_current
from sessiontools.listing import Row
from sessiontools.window import Fullness

#: Bumped whenever the snapshot's layout or a Row's fields change meaning.
#: 2: Session.turn_ended_at, the third state.
SCHEMA = 2
DEFAULT_MAX_AGE = 2.0
#: How long a caller waits on another's computation before computing itself.
LOCK_WAIT_S = 15.0
LOCK_POLL_S = 0.01

STATE_DIR_ENV = "SESSION_STATE_DIR"            # settings.SESSION_STATE_DIR_ENV
MAX_AGE_ENV = "SESSION_LIST_MAX_AGE"
FRESH_ENV = "SESSION_LIST_FRESH"
TOOL = "session"
TRUE_WORDS = ("1", "true", "yes", "on")
#: The variables the listing reads: a snapshot made under other values is
#: another world's answer.
WORLD_VARS = ("HOME", "SESSION_SIDECAR_DIR", "TMUX_TMPDIR", "CLAUDE_PROJECTS_DIR", "CONTEXT_MAX",
              "SESSIONS_DIR")

Compute = Callable[[bool], list[Row]]


# --- where, and what is in force ------------------------------------------------

def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """The state leaf, `settings.state_root`: `$SESSION_STATE_DIR`, else the
    old `~/.session/state/` while it is real, else
    `~/.sessionkit/tools/state/` (settings.leaf_path)."""
    from sessiontools import settings
    return settings.state_root(env)


def snapshot_path(env: Mapping[str, str] | None = None) -> Path:
    return state_dir(env) / "list" / "snapshot.json"


def _seconds(value: Any) -> float | None:
    """VALUE as finite seconds >= 0, else None. A boolean is not a number."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def max_age(env: Mapping[str, str] | None = None) -> float:
    """The age past which a snapshot is not served, in seconds: the variable,
    else the `session` tool's `list.max_age`, else 2. A value that cannot be
    read is passed over, never an error."""
    e = os.environ if env is None else env
    raw = e.get(MAX_AGE_ENV)
    if raw is not None and raw.strip():
        found = _seconds(raw.strip())
        if found is not None:
            return found
    found = _seconds(_setting(env))
    return DEFAULT_MAX_AGE if found is None else found


def _setting(env: Mapping[str, str] | None) -> Any:
    """`list.max_age` from the `session` tool's template and host layers, or
    None. A file that cannot be read is no setting."""
    from sessiontools import settings
    try:
        merged, _o, _l, _v = settings.merge(settings.sources(TOOL, None, env=env))
    except Exception:  # noqa: BLE001 - a settings file never costs the listing
        return None
    block = merged.get("list")
    return block.get("max_age") if isinstance(block, Mapping) else None


def fresh_from_env(env: Mapping[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return (e.get(FRESH_ENV) or "").strip().lower() in TRUE_WORDS


def world_key(env: Mapping[str, str] | None = None) -> dict:
    """What the listing's answer depends on besides the machine itself. An
    unset variable is null, so unset and empty differ (CLAUDE_PROJECTS_DIR
    set empty isolates a test)."""
    e = os.environ if env is None else env
    key: dict[str, Any] = {v: e.get(v) for v in WORLD_VARS}
    key["uid"] = os.getuid()
    return key


def _plain(env: Mapping[str, str] | None) -> dict[str, str] | None:
    return None if env is None else dict(env)


def _version() -> str:
    from sessiontools import __version__
    return str(__version__)


# --- rows on the wire --------------------------------------------------------------

_SESSION_FIELDS = tuple(f.name for f in fields(Session))
_FULLNESS_FIELDS = tuple(f.name for f in fields(Fullness))


def row_to_wire(row: Row) -> dict:
    s = asdict(row.session)
    s["path"] = str(row.session.path) if row.session.path is not None else None
    return {"session": s, "repo": row.repo, "tmux_socket": row.tmux_socket,
            "tmux_pane": row.tmux_pane, "started": row.started,
            "fullness": asdict(row.fullness) if row.fullness is not None else None}


def row_from_wire(data: Mapping[str, Any]) -> Row:
    """The Row ROW_TO_WIRE wrote. Raises (KeyError, TypeError, ValueError) on
    anything else: the caller treats that as corrupt."""
    s = dict(data["session"])
    if set(s) != set(_SESSION_FIELDS):
        raise ValueError("session fields differ")
    if s["path"] is not None:
        s["path"] = Path(s["path"])
    f = data["fullness"]
    if f is not None and set(f) != set(_FULLNESS_FIELDS):
        raise ValueError("fullness fields differ")
    return Row(session=Session(**s), repo=str(data["repo"]),
               tmux_socket=data["tmux_socket"], tmux_pane=data["tmux_pane"],
               started=data["started"], fullness=Fullness(**f) if f is not None else None)


# --- the file ---------------------------------------------------------------------

def read(path: Path) -> dict | None:
    """The snapshot at PATH as a dict, or None: missing, unreadable, not JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(path: Path, found: list[Row], context: bool, started: float, finished: float,
          env: Mapping[str, str] | None = None) -> bool:
    """FOUND as the snapshot at PATH, atomically. False (and nothing left
    behind) when it cannot be written."""
    body = {"schema": SCHEMA, "version": _version(), "world": world_key(env),
            "context": context, "started": started, "finished": finished,
            "rows": [row_to_wire(r) for r in found]}
    tmp = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        os.replace(tmp, path)
        return True
    except (OSError, TypeError, ValueError):
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def usable(snap: dict | None, want_context: bool, arrived: float, age_limit: float,
           now: float, env: Mapping[str, str] | None = None) -> list[Row] | None:
    """SNAP's rows if a caller that ARRIVED at that time may be served them,
    else None. Young enough means: computed at most AGE_LIMIT before the
    caller arrived, or finished after it arrived (it waited for that one). A
    snapshot stamped in the future, began or finished (the clock went back,
    or the file was written by hand), is stale: a future FINISHED would
    otherwise pass "finished after it arrived" for as long as it lies. Without
    CONTEXT asked for, a snapshot with CONTEXT serves, stripped of it."""
    if not snap or snap.get("schema") != SCHEMA or snap.get("version") != _version():
        return None
    if snap.get("world") != world_key(env):
        return None
    has_context = snap.get("context")
    if not isinstance(has_context, bool) or (want_context and not has_context):
        return None
    started, finished = _seconds(snap.get("started")), _seconds(snap.get("finished"))
    if started is None or finished is None or finished < started or finished > now + 1:
        return None
    if not (arrived - started <= age_limit or finished >= arrived):
        return None
    raw = snap.get("rows")
    if not isinstance(raw, list):
        return None
    try:
        found = [row_from_wire(r) for r in raw]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not all(mark_current(r.session, _plain(env)) for r in found):
        return None
    if not want_context:
        found = [replace(r, fullness=None) for r in found]
    return found


@contextmanager
def _locked(path: Path, wait: float):
    """Hold an exclusive flock on PATH, yielding True; False if it could not
    be had within WAIT seconds or the file cannot be opened (the caller then
    computes on its own). WAIT runs on the monotonic clock: a wall clock
    stepped back mid-wait would otherwise stretch it by the step. A holder
    that dies, even by SIGKILL, drops the flock with its last descriptor, so
    a waiter takes the lock at once and computes."""
    fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield False
        return
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    yield False
                    return
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(LOCK_POLL_S)
        yield True
    finally:
        os.close(fd)        # closing drops the flock


# --- the one entry ------------------------------------------------------------------

def _compute_rows(want_context: bool) -> list[Row]:
    from sessiontools import listing
    found = listing.rows()
    return listing.with_context(found) if want_context else found


def rows(want_context: bool, fresh: bool = False, env: Mapping[str, str] | None = None,
         compute: Compute | None = None, clock: Callable[[], float] = time.time) -> list[Row]:
    """The listing's rows, from a young snapshot or computed once for every
    caller waiting on it. WANT_CONTEXT asks for CONTEXT filled. FRESH (or
    `$SESSION_LIST_FRESH`) computes now and refreshes the snapshot."""
    compute = compute or _compute_rows
    path = snapshot_path(env)
    fresh = fresh or fresh_from_env(env)
    limit = max_age(env)
    arrived = clock()
    if fresh or limit <= 0:
        started = clock()
        found = compute(want_context)
        write(path, found, want_context, started, clock(), env)
        return found
    hit = usable(read(path), want_context, arrived, limit, arrived, env)
    if hit is not None:
        return hit
    with _locked(path.with_name("list.lock"), LOCK_WAIT_S):
        hit = usable(read(path), want_context, arrived, limit, clock(), env)
        if hit is not None:
            return hit
        started = clock()
        found = compute(want_context)
        write(path, found, want_context, started, clock(), env)
    return found
