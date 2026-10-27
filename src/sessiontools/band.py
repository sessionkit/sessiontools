"""`band`: how close a window is to its seat's settle threshold, as data
("thresholds belong to session, the sidebar only paints").

- OVER: at or past the settle threshold (`sessiontools.settle.threshold`,
  read-only: this module never writes or re-reads its variable itself).
- NEAR: within NEAR percent under it: `used >= threshold * (1 - NEAR/100)`.
- OK: below that.
- None (JSON null): no threshold is set, or `used` is unknown.

THE NEAR CUT IS A SETTING, read through the settings cascade
(`sessiontools.settings`: template, host, agent, seat and session layers),
under ONE key, NEAR_KEY, of the `session` tool. It is never read from the
environment: the environment is not a layer (settle's own
`SESSION_SETTLE_AT` read is the pattern a later version retires). Unset,
unreadable or not a number from 0 to 100 is the default, DEFAULT_NEAR_PCT
(any other default is a one-line change to it).

    <sessions>/<sid>/settings/session.json   {"context": {"near_pct": 10}}

No schema is shipped for the key: the cascade reads a key without one, and
nothing installs a `session` schema today.

It sits at the top level, not in `context_parts`: importing any
`context_parts` submodule runs that package's `__init__`, which imports the
fold. Here, the bare `session context --json` can carry `band` without it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

#: The settings tool the near cut is read under.
NEAR_TOOL = "session"
#: The ONE key of the near cut, dotted: `{"context": {"near_pct": N}}`.
NEAR_KEY = "context.near_pct"
#: The near cut when no layer sets one, in percent of the settle threshold.
DEFAULT_NEAR_PCT = 20

OK = "ok"
NEAR = "near"
OVER = "over"
BANDS = (OK, NEAR, OVER)


def _pct(value: Any) -> float | None:
    """VALUE as a percentage from 0 to 100, else None. A boolean is not a
    number here, though Python counts it as an int."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 100:
        return None
    return number


def _lookup(merged: Mapping, dotted: str) -> Any:
    node: Any = merged
    for part in dotted.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


def setting(sid: str | None = None, env: Mapping[str, str] | None = None) -> Any:
    """NEAR_KEY's raw merged value for session SID (None: template and host
    layers only), or None when no layer sets it or a layer cannot be read.

    SID may be a short prefix: `settings.resolve_sid` turns it into the full
    id, as `session settings` does, since `session_dir` is a literal join
    and a prefix would otherwise read no session, agent or seat layer and
    fall back to the default without a word. An AMBIGUOUS prefix is refused
    there (SettingsError) and reads as None here, so the default: never a
    guess at which session was meant.

    `sources` + `merge`, as `busy.values` reads `sounds`: `resolve` would
    also check an installed schema, which imports jsonschema. The agent
    layer needs PyYAML; without it that layer is dropped and the rest count.
    """
    from sessiontools import settings
    try:
        agent = seat = None
        if sid:
            sid, _note = settings.resolve_sid(sid, env)
            agent, seat = settings.identity(sid, env)
        try:
            srcs = settings.sources(NEAR_TOOL, sid, agent, seat, env)
        except ImportError:
            srcs = settings.sources(NEAR_TOOL, sid, None, seat, env)
        merged, _origins, _locked, _leaves = settings.merge(srcs)
    except (settings.SettingsError, OSError, ValueError):
        return None
    return _lookup(merged, NEAR_KEY)


def near_pct(sid: str | None = None, env: Mapping[str, str] | None = None) -> float:
    """The near cut in percent: NEAR_KEY through the cascade, else
    DEFAULT_NEAR_PCT."""
    found = _pct(setting(sid, env))
    return float(DEFAULT_NEAR_PCT) if found is None else found


def band(used: float | None, threshold: float | None,
         near: float = DEFAULT_NEAR_PCT) -> str | None:
    """OK, NEAR or OVER for USED tokens against THRESHOLD; None when either
    is unknown (or the threshold is not above zero)."""
    if used is None or threshold is None or threshold <= 0:
        return None
    if used >= threshold:
        return OVER
    if used >= threshold * (1 - near / 100):
        return NEAR
    return OK


def threshold(env: Mapping[str, str] | None = None) -> int | None:
    """The seat's settle threshold in tokens, as settle reads it (read-only)."""
    import os

    from sessiontools import settle
    return settle.threshold(os.environ if env is None else env)


def band_for(used: float | None, sid: str | None = None,
             env: Mapping[str, str] | None = None) -> str | None:
    """The band of USED tokens for session SID (a full id or a unique
    prefix): settle's threshold, and the near cut from the settings cascade."""
    return band(used, threshold(env), near_pct(sid, env))
