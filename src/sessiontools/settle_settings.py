"""The settle warning's settings: every key name, every default message and
every variable it reads, in ONE place, and the `session` schema rendered
from them.

A rename is a one-line change here: the schema, the birth copy and the
reader all build their paths from the names below, so nothing else spells
a key. Every name and message is a PROPOSAL, for the maintainer to rule on.

THE KEYS, under the `session` tool, at any layer of the cascade
(`sessiontools.settings`: template, host, agent, seat, session):

    settle.soft.at              tokens; the soft line starts here. Unset: no soft line.
    settle.soft.every           tokens; the soft line repeats this far apart.
    settle.soft.say             the soft line's words; {at}, {used}, {left} and
                                {compact_at} are filled, as on the hard line.
    settle.hard.at              tokens; the hard line starts here.
    settle.hard.before_compact  tokens short of the SET compaction point.
    settle.hard.every           tokens; the hard line repeats this far apart.
    settle.hard.say             the hard line's words; {used}, {left}, {at},
                                {compact_at} are filled ({left} is `?` when
                                the point is unknown).

A token amount is a number or a string: `250000` and `"250k"` alike
(`1.5M` too). The schema says so with an `anyOf` and no `type`, so the
birth copies a variable's string as written and this module reads it.

`settle: null` in a layer is off, until a layer below it sets a settle key
again; a host `_locked` naming `settle` pins it for every layer below. No
`settle` anywhere is off: nothing is said.

THE ENVIRONMENT IS NOT A LAYER. `SESSION_SETTLE_AT` and `SESSION_SETTLE_SAY`
are `x-env` on `settle.soft.at` and `settle.soft.say`: `session start`
copies them once, at birth, into `<sid>/settings/session.d/50-env.json`,
and reads them from there after (`settings.write_env_at_birth`). The birth
reads only an INSTALLED schema, so `session start`'s schema install step
writes this one into `~/.sessionkit/tools/settings/.session.schema.json`
first (`settings.install_own_schemas`).

CHEAP WHEN OFF: `resolve` keeps an OFF stamp,
`<session dir>/settle/off`. A clean OFF writes it, back-dated 1 s, listing
every layer path the cascade read or looked for (`watched`); ON removes it.
`hooks/settle-hook` exits 0 by builtins while the stamp is there and no
listed path is newer, so a session with no `settle` starts no python.
`session start` writes it at birth (`stamp_at_birth`, from
`settings.birth_main`); any other session's first batch writes it.

CHEAP WHEN ON: the agent layer's `session` section is cached in
`<session dir>/settle/agent.json`, keyed on the agent file's mtime, size
and inode, so a batch does not import the agent parser (~32 ms) while
the file is unchanged (`_agent_source`).

Standard library only, and nothing imported at module level but that: the
settle hook imports it on every batch.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# --- the names: a rename is one line here --------------------------------------

#: The settings tool the keys live under.
TOOL = "session"
#: The block every key sits in.
ROOT = "settle"
#: The two lines.
SOFT = "soft"
HARD = "hard"
#: The keys of a line.
AT = "at"
EVERY = "every"
SAY = "say"
BEFORE_COMPACT = "before_compact"

#: Under `<session dir>/settle/` (ROOT): the OFF stamp, and the agent
#: layer's cache.
OFF = "off"
AGENT_CACHE = "agent.json"

#: The variables `session start` copies once, at birth (x-env).
AT_ENV = "SESSION_SETTLE_AT"
SAY_ENV = "SESSION_SETTLE_SAY"

#: The placeholders a line may carry, filled by plain replace.
PLACEHOLDERS = ("{at}", "{used}", "{left}", "{compact_at}")
#: What {left} (and {compact_at}) read when the compaction point is not set.
UNKNOWN = "?"

# --- the words: each default message, once --------------------------------------

#: The soft line, when no layer sets `settle.soft.say` (PROPOSAL). It names
#: no verb that does not exist yet.
SOFT_SAY = ("You're past {at} ({used} used). At a good stopping point, settle: "
            "save what matters, then hand over.")
#: The hard line, when no layer sets `settle.hard.say` (PROPOSAL). {left}
#: comes from the SET compaction point only, never a guess.
HARD_SAY = ("Now: {used} used, {left} before compaction. Settle: "
            "save what matters, then hand over.")


def path(*parts: str) -> tuple[str, ...]:
    """A key's path from ROOT: path(SOFT, AT) is ('settle', 'soft', 'at')."""
    return (ROOT, *parts)


def dotted(*parts: str) -> str:
    """A key as people write it: dotted(SOFT, AT) is 'settle.soft.at'."""
    return ".".join(path(*parts))


# --- token amounts ---------------------------------------------------------------

_SCALE = {"k": 1_000, "m": 1_000_000}
#: The string form the schema accepts: digits, an optional fraction, an
#: optional k or M, any case, space around.
TOKENS_PATTERN = r"^\s*[0-9]+(\.[0-9]+)?\s*[kKmM]?\s*$"


def tokens(value: Any) -> int | None:
    """VALUE as a token count above zero, else None. A number, or a string:
    `250000`, `250k`, `250K`, `1.5M`, space ignored. A boolean is not a
    number here, though Python counts it as an int. Not finite, unparsable
    or not above zero: None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        raw = value.strip().lower()
        if not raw:
            return None
        scale = _SCALE.get(raw[-1], 1)
        if scale != 1:
            raw = raw[:-1].strip()
        try:
            number = float(raw) * scale
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    count = int(number)
    return count if count > 0 else None


# --- the schema, rendered from the names ------------------------------------------

def _tokens_schema(description: str, **extra: Any) -> dict:
    return {"description": description,
            "anyOf": [{"type": "integer", "exclusiveMinimum": 0},
                      {"type": "string", "pattern": TOKENS_PATTERN}],
            **extra}


def _say_schema(description: str, default: str, **extra: Any) -> dict:
    return {"description": description, "type": "string", "minLength": 1,
            "default": default, **extra}


def schema() -> dict:
    """The `session` tool's schema: the settle keys. Other `session` keys
    (docs/settings.md) are not in it yet, so it names no other key and
    refuses none. `session start` installs it (settings.install_own_schemas)."""
    from sessiontools.settings import X_ENV
    soft = {
        "description": ("The soft line: once at AT, then once more each EVERY past it. "
                        "Unset AT: no soft line."),
        "type": "object",
        "properties": {
            AT: _tokens_schema(f"Tokens used when the soft line starts: 250000 or '250k'. "
                               f"{AT_ENV} at launch is copied here once, at birth.",
                               **{X_ENV: AT_ENV}),
            EVERY: _tokens_schema("Tokens between soft lines past AT. Unset: every batch."),
            SAY: _say_schema(f"The soft line's words. {{at}}, {{used}}, {{left}} and "
                             f"{{compact_at}} are filled, as on the hard line; {{left}} "
                             f"and {{compact_at}} read '{UNKNOWN}' when the point is unknown. "
                             f"{SAY_ENV} at launch is copied here once, at birth.",
                             SOFT_SAY, **{X_ENV: SAY_ENV}),
        },
    }
    hard = {
        "description": ("The hard line, 'now': from the lower of AT and the set compaction "
                        "point less BEFORE_COMPACT; every batch, or each EVERY."),
        "type": "object",
        "properties": {
            AT: _tokens_schema("Tokens used when the hard line starts."),
            BEFORE_COMPACT: _tokens_schema(
                "Tokens short of the SET compaction point when the hard line starts. "
                "Unknown point: AT alone."),
            EVERY: _tokens_schema("Tokens between hard lines. Unset: every batch."),
            SAY: _say_schema("The hard line's words. {used}, {left}, {at} and {compact_at} "
                             f"are filled; {{left}} reads '{UNKNOWN}' when the point is unknown.",
                             HARD_SAY),
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{TOOL}.schema.json",
        "title": TOOL,
        "description": (f"Settings of the session tool, installed by `session start`. "
                        f"Today it names the {ROOT} keys only. At any layer: "
                        f"~/.sessionkit/tools/settings/{TOOL}.json (host), agent front matter "
                        f"settings: {{{TOOL}: {{{ROOT}: ...}}}} (agent), "
                        f"seats/<seat>/settings/{TOOL}.json (seat), "
                        f"sessions/<sid>/settings/{TOOL}.json or {TOOL}.d/*.json (session)."),
        "type": "object",
        "properties": {
            ROOT: {
                "description": (f"The settle warning. Unset is off and says nothing; null is off "
                                f"until a layer below sets a {ROOT} key; a host _locked pins it."),
                "type": ["object", "null"],
                "properties": {SOFT: soft, HARD: hard},
            },
        },
    }


# --- the reader --------------------------------------------------------------------

@dataclass(frozen=True)
class Soft:
    """The soft line, read: where it starts, how far apart it repeats
    (None: every batch), and its words."""

    at: int
    every: int | None
    say: str


@dataclass(frozen=True)
class Hard:
    """The hard line, read: its mark, its distance short of the set
    compaction point (either may be None, not both), its repeat (None:
    every batch), and its words."""

    at: int | None
    before_compact: int | None
    every: int | None
    say: str


@dataclass(frozen=True)
class Settle:
    """Both lines; a line no layer turns on is None. Never both None: that
    is off, and `read` returns None."""

    soft: Soft | None
    hard: Hard | None


def _say(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def parse(block: Any) -> Settle | None:
    """BLOCK, the merged `settle` value, read; None when it is off: not a
    map (unset, null), or neither line on. A soft line is on with a soft AT;
    a hard line with a hard AT or BEFORE_COMPACT. A value that is not a
    token amount reads as unset; a SAY that is not a non-empty string is the
    default words."""
    if not isinstance(block, Mapping):
        return None
    soft = hard = None
    raw = block.get(SOFT)
    if isinstance(raw, Mapping):
        at = tokens(raw.get(AT))
        if at is not None:
            soft = Soft(at, tokens(raw.get(EVERY)), _say(raw.get(SAY), SOFT_SAY))
    raw = block.get(HARD)
    if isinstance(raw, Mapping):
        at, before = tokens(raw.get(AT)), tokens(raw.get(BEFORE_COMPACT))
        if at is not None or before is not None:
            hard = Hard(at, before, tokens(raw.get(EVERY)), _say(raw.get(SAY), HARD_SAY))
    if soft is None and hard is None:
        return None
    return Settle(soft, hard)


def _agent_source(agent: str, sid: str | None, env: Mapping[str, str] | None,
                  cache: Path | None) -> Any:
    """The agent layer's `settings.Source` for TOOL, or None (no file, no
    `session` section, or no PyYAML). The agent file is the one
    `settings.sources` reads (the recorded file, else `agent/config/`).

    ON-PATH CACHE: parsing the front matter means
    importing the agent parser, ~32 ms of a batch, while the parse itself
    is under 1 ms. So the section is kept in CACHE, `<session dir>/settle/
    agent.json`, keyed on the file's path, mtime_ns, size and inode, and a
    batch whose key matches reads it from there with no import. A miss
    parses and rewrites the cache, but only when the file's stat is the
    same after the parse as before (an edit during the parse is not
    cached). No CACHE (no session dir): parsed every time. A cache that
    cannot be read or written is a miss, never an error."""
    from sessiontools import settings
    file, _gone = settings.agent_file_for(agent, sid, env)
    path = Path(file) if file else settings.agent_config(agent, env)
    key = _stat_key(path)
    if key is None:
        return None
    if cache is not None:
        hit = _read_cache(cache)
        if hit is not None and hit.get("key") == key:
            section = hit.get("section")
            return settings.Source("agent", path, section) if isinstance(section, dict) else None
    try:
        found = settings.agent_settings(agent, env, file)
    except ImportError:     # no PyYAML (a hook's bare python3): the layer is dropped
        return None
    section = found[1].get(TOOL) if found is not None else None
    section = section if isinstance(section, dict) else None
    if cache is not None and _stat_key(path) == key:
        _write_cache(cache, {"key": key, "section": section})
    return settings.Source("agent", path, section) if section is not None else None


#: Bumped when the cached shape changes, so an old cache is a miss.
CACHE_VERSION = 1


def _stat_key(path: Path) -> list | None:
    """PATH's cache key, or None when it is not a regular file."""
    import stat as _stat
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not _stat.S_ISREG(st.st_mode):
        return None
    return [CACHE_VERSION, str(path), st.st_mtime_ns, st.st_size, st.st_ino]


def _read_cache(cache: Path) -> dict | None:
    import json
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(cache: Path, data: dict) -> None:
    """CACHE, written whole or not at all (a temp file, then a rename)."""
    import json
    tmp = cache.with_name(f".{cache.name}.{os.getpid()}")
    try:
        cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        os.replace(tmp, cache)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _session_settle(sid: str | None, env: Mapping[str, str] | None) -> Path | None:
    """`<session dir>/settle`, or None: no sid, or no session dir."""
    if not sid:
        return None
    from sessiontools import settings
    session = settings.session_dir(sid, env)
    return session / ROOT if session.is_dir() else None


def values(sid: str | None, env: Mapping[str, str] | None = None) -> Any:
    """The merged `settle` value of session SID through every layer, the
    agent and seat named by the session's own record (`settings.identity`);
    None when no layer sets it. No PyYAML (a hook's `python3 -I`): the
    agent layer is dropped and the rest still count. `sources` + `merge`,
    as `busy.values`: `resolve` would import jsonschema. The agent layer
    comes from `_agent_source`, through its mtime-keyed cache. Raises
    settings.SettingsError or OSError on a file it cannot read."""
    from sessiontools import settings
    agent = seat = None
    if sid:
        agent, seat = settings.identity(sid, env)
    srcs = settings.sources(TOOL, sid, None, seat, env)
    if agent:
        found = _agent_source(agent, sid, env, _cache_file(sid, env))
        if found is not None:
            at = sum(1 for s in srcs if s.layer in ("template", "host"))
            srcs.insert(at, found)
    merged, _origins, _locked, _leaves = settings.merge(srcs)
    return merged.get(ROOT)


def _cache_file(sid: str | None, env: Mapping[str, str] | None) -> Path | None:
    found = _session_settle(sid, env)
    return None if found is None else found / AGENT_CACHE


def read(sid: str | None, env: Mapping[str, str] | None = None) -> Settle | None:
    """Session SID's settle lines (parse(values(...))), or None: off, or a
    layer that cannot be read. A warning never breaks a turn, so a bad
    file is silence here; `session settings` names it."""
    from sessiontools import settings
    try:
        return parse(values(sid, env))
    except (settings.SettingsError, OSError, ValueError):
        return None


# --- the OFF stamp -------------------------------------------------------------

def watched(sid: str | None, env: Mapping[str, str] | None = None) -> list[Path]:
    """Every path the cascade for session SID reads OR LOOKS FOR, present
    or not, in cascade order: the host template and file, each `.d` folder
    AND each file in it, the agent file (the recorded one and both
    `agent/config/` forms), the seat's and the session's files. A layer
    made later is a path here that becomes newer than the stamp; a file
    added to a `.d` folder changes the folder's mtime; an edit in place
    changes the file's own. The agent and seat are the session record's
    (`settings.identity`), whose own files are listed, so a record that
    names a new agent is seen too. Raises as `values` does."""
    from sessiontools import settings
    out: list[Path] = []

    def layer(directory: Path, template: bool = False) -> None:
        if template:
            out.append(directory / f"{TOOL}.template.json")
        out.append(directory / f"{TOOL}.json")
        drop_ins = directory / f"{TOOL}.d"
        out.append(drop_ins)
        try:
            out.extend(sorted(drop_ins.iterdir()))
        except OSError:
            pass

    override = settings.settings_file_override(TOOL, env)
    if override is not None:
        out.append(override)
        out.append(override.parent / f"{TOOL}.template.json")
        drop_ins = override.parent / f"{TOOL}.d"
        out.append(drop_ins)
        try:
            out.extend(sorted(drop_ins.iterdir()))
        except OSError:
            pass
    else:
        found = settings.host_dir(TOOL, env)[0]
        layer(found, template=True)
    agent = seat = None
    if sid:
        agent, seat = settings.identity(sid, env)
    if agent:
        recorded = settings.recorded_agent_file(sid, env) if sid else None
        if recorded:
            out.append(Path(recorded))
        config = settings.agent_root(env) / "config"
        out.append(config / agent / f"{agent}.md")
        out.append(config / f"{agent}.md")
    if seat:
        layer(settings.seat_dir(seat, env) / "settings")
    if sid:
        layer(settings.session_dir(sid, env) / "settings")
    return out


def stamp_off(settle_dir: Path, paths: list[Path], started: float) -> bool:
    """Write SETTLE_DIR's OFF stamp: PATHS, one a line, its mtime back-dated
    to STARTED - 1 s, as the busy sound's `sounds/busy/off` (`busy.mark_off`):
    `[ a -nt b ]` compares whole seconds on some shells, so a layer written in
    the second the resolve began still reads newer. Written whole or not at
    all (a temp file, then a rename), so the shim never reads half a list.
    A path holding a line break cannot be listed, so no stamp is written
    (python runs every batch: the safe side). True if the stamp was written."""
    if any("\n" in str(p) for p in paths):
        clear_off(settle_dir)
        return False
    off = settle_dir / OFF
    tmp = settle_dir / f".{OFF}.{os.getpid()}"
    try:
        settle_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(f"{p}\n" for p in paths))
        os.utime(tmp, (started - 1, started - 1))
        os.replace(tmp, off)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def clear_off(settle_dir: Path) -> None:
    """Remove SETTLE_DIR's OFF stamp, if any: the warning resolved ON."""
    try:
        os.unlink(settle_dir / OFF)
    except OSError:
        pass


def resolve(sid: str | None, env: Mapping[str, str] | None = None) -> Settle | None:
    """`read`, and the OFF stamp kept to match: a clean OFF (no layer sets
    the warning, every file read) stamps `<session dir>/settle/off` with
    `watched`; ON removes it. A layer that cannot be read is silence and
    stamps nothing, so a repair is seen at the next batch whatever it
    touched. No session dir: no stamp (the shim exits 0 there anyway).
    Used by the hook and by the birth."""
    import time

    from sessiontools import settings
    started = time.time()
    try:
        found = parse(values(sid, env))
        paths = watched(sid, env) if found is None else None
    except (settings.SettingsError, OSError, ValueError):
        return None
    settle_dir = _session_settle(sid, env)
    if settle_dir is not None:
        if found is None:
            stamp_off(settle_dir, paths or [], started)
        else:
            clear_off(settle_dir)
    return found


def stamp_at_birth(session: Path, env: Mapping[str, str] | None = None) -> None:
    """The birth's OFF stamp (`settings.birth_main`): resolve SESSION's
    warning once, so a seat `session start` launched starts no python in
    the hook while no layer sets it. SESSION must be the dir the hook will
    find for its id (`settings.session_dir`); any other dir is left alone,
    and the hook stamps for itself at its first batch."""
    from sessiontools import settings
    sid = session.name
    try:
        if settings.session_dir(sid, env).resolve() != session.resolve():
            return
    except OSError:
        return
    resolve(sid, env)
