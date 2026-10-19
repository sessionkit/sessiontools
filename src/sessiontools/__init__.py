"""sessiontools - the harness layer: sessions, their status, and how long
they have been idle - one driver per harness, a library first.

Import it, or use the command it ships: `session` (since
2030-07-29, when the session repo was retired). `session list` and
`session get` are built on the functions below; `session start` is its own
package, `sessiontools.start`; `session send` is disabled (2030-07-30).

    from sessiontools import sessions, live, find, idle_seconds
    from sessiontools import session_id, current, SESSION_ID_VARS
    from sessiontools import read_operator, Operator   # who asks, who is privileged

Every function speaks OUR words - busy, not busy, unknown - never a
harness's. `Session.state` is one of three: `busy`, `idle with children`
(the turn is over, a background agent runs) or `idle`. The first driver is
`claude_code`; the day a harness changes a
status word, that driver changes and nothing above it does.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from sessiontools.claude_code import (
    BUSY,
    IDLE,
    IDLE_WITH_CHILDREN,
    SESSION_ID_VARS,
    SIDECAR_DIR_ENV,
    STATES,
    Session,
    current,
    find,
    idle_seconds,
    live,
    session_id,
    sessions,
    sidecar_dir,
)
from sessiontools.operator import Operator, read_operator

try:
    __version__ = version("sessiontools")
except PackageNotFoundError:   # a checkout that is not installed
    __version__ = "0+unknown"

__all__ = [
    "BUSY",
    "IDLE",
    "IDLE_WITH_CHILDREN",
    "SESSION_ID_VARS",
    "SIDECAR_DIR_ENV",
    "STATES",
    "Operator",
    "Session",
    "__version__",
    "current",
    "find",
    "idle_seconds",
    "live",
    "read_operator",
    "session_id",
    "sessions",
    "sidecar_dir",
]
