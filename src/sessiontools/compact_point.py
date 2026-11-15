"""Where THIS seat's auto-compaction fires, as the launch SET it.

The settle hook's hard line says how far a seat is from compaction
(`{left}`). That figure comes from the point a launch set, never from a
guess: no hook payload or status-line field holds a threshold, so
nothing here reads one. When nothing a hook can see sets the point, the
answer is None, and the line prints `?`.

THE NAMES. Claude Code's own setting keys that `session start`'s
`claude-code` tool renders into its `--settings` door
(`start.claude_settings.RENDERED`) are spelled here, once:
`autoCompactWindow` (the window the threshold is cut from) and
`idleCompaction` (compaction while a seat is idle; this module never
reads it: an idle compaction has no point to count down to).

THE SOURCES, in the order Claude Code 2.1.316 ranks them (read
statically, and `autoCompactWindow` in `--settings` confirmed live), the
ones a hook can see:

1. `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in the environment the hook inherits
   from its claude.
2. `--autocompact <auto|tokens>` on the claude argv the launch recorded
   (`<session dir>/launch/command`'s `vendor:` line). `auto` hands the
   window back to Claude Code (a server value, an experiment or the
   model's window): unknown here.
3. `autoCompactWindow` in the `--settings` file on that same argv: the
   door the launch wrote, or a crew's own file. Claude Code read it at
   launch and does not watch it; nor is it re-resolved from the cascade
   here, so an edited layer cannot make this answer differ from Claude's.

Not seen, so not read: managed policy (above `--settings`). The user,
project and local settings files sit below `--settings`,
so a window set only there is not read either: it reads as None, unknown,
never inferred. Those files are read for the off switch only (OFF, below).

THE POINT (`TJ`/`jZ`): `window - reserve - 13 000`, where the reserve
is `min(max output, 20 000)`, taken as 20 000 (every model this fleet
runs has a max output of 20k or more; a hook does not know the model's).
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (a percent, over 0 and up to 100) lowers
it to that percent of `window - reserve`, never above the plain point.
The window is not capped at the model's context window here: the model is
not known to a hook either.

THE TEXT: a window in the variable or the flag is read as Claude Code
reads it (`env_window`, `flag_window`): `400k`, `0.4m`, `400000`,
`400,000`; the variable is clamped to WINDOW_MIN..WINDOW_MAX, and the
flag takes a bare 100 to 1000 as thousands.

OFF: `DISABLE_COMPACT` or `DISABLE_AUTO_COMPACT` set to `1`, `true`,
`yes` or `on` (Claude Code's own test; `0` is not off) means no auto
compaction: no point, None. So does `autoCompactEnabled: false`, taken as
Claude Code takes it (`vp` reads it through `ei`): from the HIGHEST source
that sets the key, whichever source set the window. The order is the
`--settings` file, then the local and project files under
CLAUDE_PROJECT_DIR (Claude Code gives it to every hook), then the user's
(`claude_settings_files`), then the legacy global config
(`global_config`). A recorded `--setting-sources` drops the files it
leaves out, as it does for Claude. Off turns idle compaction off too
(`F3o` returns "compaction_off"). This is how a seat turns compaction
off by hand: in its own `~/.claude/settings.json`, below a window the
door may set. These files are opened only once a window is found.

Standard library only, nothing imported at module level but that and
`sessiontools.argv_flags` (the last-wins flag scan, itself importing
nothing): the settle hook imports it on every batch.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from sessiontools.argv_flags import last_value as _flag

# --- Claude Code's names, spelled once -------------------------------------------

#: The setting the threshold is cut from, a number of tokens or "auto".
AUTO_COMPACT_WINDOW = "autoCompactWindow"
#: Compaction while idle; rendered by the `claude-code` tool, never read here.
IDLE_COMPACTION = "idleCompaction"
#: The setting that turns auto compaction off.
AUTO_COMPACT_ENABLED = "autoCompactEnabled"
#: The `--autocompact` value that hands the choice back to Claude Code. A
#: settings file takes it only per model (`modelSettings`), never top level.
WINDOW_AUTO = "auto"
#: The range Claude Code's schema gives `autoCompactWindow`, in tokens.
WINDOW_MIN = 100_000
WINDOW_MAX = 1_000_000

#: The variables a hook inherits from its claude.
WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
PCT_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
OFF_ENVS = ("DISABLE_COMPACT", "DISABLE_AUTO_COMPACT")

#: The claude flags read off the recorded argv.
WINDOW_FLAG = "--autocompact"
SETTINGS_FLAG = "--settings"
SOURCES_FLAG = "--setting-sources"

#: Claude Code's own files below `--settings`. The project root a hook is
#: given, the config home, and the legacy global config in that home.
PROJECT_ENV = "CLAUDE_PROJECT_DIR"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
GLOBAL_CONFIG = ".claude.json"
#: `--setting-sources`' names for the files `claude_settings_files` lists,
#: in its order.
SOURCE_NAMES = ("user", "project", "local")

#: `min(max output, 20 000)`, taken whole (see the module doc).
OUTPUT_RESERVE = 20_000
#: What the threshold keeps clear below the effective window.
BUFFER = 13_000

#: The launch record this reads: `start.record`'s directory, file and line.
RECORD_DIR = "launch"
COMMAND_FILE = "command"
VENDOR_LINE = "vendor: "


class Point(NamedTuple):
    """The set compaction point: `at` tokens, cut from `window`, which
    `source` set (the variable, the flag, or `--settings <file>`)."""

    at: int
    window: int
    source: str


def is_window(value: Any) -> bool:
    """Is VALUE a window Claude Code's schema takes as a number: an int (not
    a bool), WINDOW_MIN to WINDOW_MAX tokens?"""
    return type(value) is int and WINDOW_MIN <= value <= WINDOW_MAX


def threshold(window: int, pct: float | None = None) -> int:
    """The token count at which a WINDOW compacts: the effective
    window less BUFFER, or PCT percent of the effective window when that
    is lower. A PCT outside (0, 100] is ignored, as Claude Code does."""
    effective = window - OUTPUT_RESERVE
    plain = effective - BUFFER
    if pct is not None and 0 < pct <= 100:
        return min(math.floor(effective * (pct / 100)), plain)
    return plain


# Claude Code 2.1.316's read of a window written as text (`zke`, `ep`,
# `Nks` @262153373): a `k`/`m` suffix is JS `parseFloat` times the scale;
# without one, an exponent (`4e5`) or grouped thousands
# (`400,000`, `400_000`) are read whole, else JS `parseInt` (leading
# digits). JS `\d` is ASCII, hence [0-9]. JS `parseFloat` also reads a
# leading `Infinity`, so `Infinityk` is above the range, not unusable.
_JS_FLOAT = re.compile(r"[+-]?(?:Infinity|(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)")
_JS_INT = re.compile(r"[+-]?[0-9]+")
_EXPONENT = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE][+-]?[0-9]+")
#: Claude Code's thousands separators: `_`, `,`, NO-BREAK SPACE, NARROW
#: NO-BREAK SPACE and a plain space, written as escapes so none is invisible.
_SEP = "_,\u00a0\u202f "
_GROUPED = re.compile(f"[+-]?[0-9]{{1,3}}([{_SEP}])[0-9]{{3}}(?:\\1[0-9]{{3}})*")
_SEPARATORS = re.compile(f"[{_SEP}]")


def _js_number(text: str) -> float:
    """Claude Code's `ep`: TEXT (trimmed, no suffix) as a number, NaN when
    it is none."""
    if len(text) <= 32:
        if _EXPONENT.fullmatch(text):
            number = float(text)
            return number if number.is_integer() else math.nan
        if _GROUPED.fullmatch(text):
            return float(_SEPARATORS.sub("", text))
    found = _JS_INT.match(text)
    return float(found.group()) if found else math.nan


def _window_kind(text: str, bare_thousands: bool) -> tuple[str, int | None]:
    """Claude Code's `zke`: TEXT as `("in_range", tokens)`, `("below",
    None)`, `("above", None)` or `("unusable", None)`. BARE_THOUSANDS (the
    flag's read, not the variable's) takes 100 to 1000 without a suffix
    as thousands: `--autocompact 200` is 200k."""
    text = text.strip()
    scale = {"m": 1_000_000, "k": 1_000}.get(text[-1:].lower())
    if scale is None:
        number = _js_number(text)
    else:
        found = _JS_FLOAT.match(text)
        number = float(found.group()) * scale if found else math.nan
    if bare_thousands and scale is None and 100 <= number <= 1000:
        number *= 1000
    if math.isnan(number) or number < 1:
        return "unusable", None
    if number < WINDOW_MIN:
        return "below", None
    if number > WINDOW_MAX:
        return "above", None
    return "in_range", math.floor(number + 0.5)          # JS Math.round


def env_window(text: str) -> int | None:
    """The window CLAUDE_CODE_AUTO_COMPACT_WINDOW's TEXT sets (`cQt`): its
    tokens, clamped to WINDOW_MIN..WINDOW_MAX as Claude Code clamps it, or
    None when it is no number (`auto` included): Claude then reads on."""
    kind, tokens = _window_kind(text, bare_thousands=False)
    return {"in_range": tokens, "below": WINDOW_MIN, "above": WINDOW_MAX}.get(kind)


def flag_window(text: str) -> int | None:
    """The window `--autocompact TEXT` sets (`_fn`): its tokens, or None for
    `auto` and for a value Claude Code refuses to start with."""
    kind, tokens = _window_kind(text, bare_thousands=True)
    return tokens if kind == "in_range" else None


def _truthy(value: str | None) -> bool:
    """Claude Code's `He`, how it reads DISABLE_COMPACT and
    DISABLE_AUTO_COMPACT: `1`, `true`, `yes` or `on`, any case."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _pct(env: Mapping[str, str]) -> float | None:
    try:
        return float(env.get(PCT_ENV, ""))
    except ValueError:
        return None


def launch_argv(sid: str | None, env: Mapping[str, str] | None = None) -> list[str] | None:
    """The claude argv session SID's launch recorded (its `vendor:` line),
    or None: no session, no record, or a line that will not split."""
    if not sid:
        return None
    from sessiontools import settings
    path = os.path.join(settings.session_dir(sid, env), RECORD_DIR, COMMAND_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(VENDOR_LINE):
                    return shlex.split(line[len(VENDOR_LINE):])
    except (OSError, ValueError):
        return None
    return None


def _json_object(path: str | os.PathLike) -> dict | None:
    """The file at PATH as one JSON object, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _settings_file(value: str, env: Mapping[str, str]) -> dict | None:
    """The `--settings` VALUE as one JSON object (inline JSON or a file,
    `~` the launch's HOME), or None when it cannot be read."""
    if value.lstrip().startswith("{"):
        try:
            data = json.loads(value)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    home = env.get("HOME") or os.path.expanduser("~")
    return _json_object(home + value[1:] if value == "~" or value.startswith("~/") else value)


def claude_settings_files(work_dir: str | None,
                          env: Mapping[str, str] | None = None) -> list[Path]:
    """Claude Code's own settings files that a claude launched in WORK_DIR
    reads, and that the door outranks: the user's (`$CLAUDE_CONFIG_DIR/
    settings.json`, else `~/.claude/settings.json`, `~` the launch's HOME),
    then the project's and the local one under WORK_DIR. Lowest first,
    SOURCE_NAMES' order. Standard library only, for the settle hook;
    `start.claude_settings` re-exports it."""
    from sessiontools import settings
    env = os.environ if env is None else env
    config_dir = env.get(CONFIG_DIR_ENV)
    user = (Path(settings._expand_tilde(config_dir, env)) if config_dir
            else settings.home(env) / ".claude")
    files = [user / "settings.json"]
    if work_dir:
        files += [Path(work_dir) / ".claude" / "settings.json",
                  Path(work_dir) / ".claude" / "settings.local.json"]
    return files


def global_config(env: Mapping[str, str] | None = None) -> Path:
    """Claude Code's legacy global config: `$CLAUDE_CONFIG_DIR/.claude.json`,
    else `~/.claude.json` (not under `~/.claude`), `~` the launch's HOME."""
    from sessiontools import settings
    env = os.environ if env is None else env
    config_dir = env.get(CONFIG_DIR_ENV)
    base = Path(settings._expand_tilde(config_dir, env)) if config_dir else settings.home(env)
    return base / GLOBAL_CONFIG


def compaction_enabled(door: Mapping[str, Any] | None, argv: list[str],
                       env: Mapping[str, str]) -> bool:
    """Is auto compaction on, as Claude Code's `vp` decides past the
    variables? `autoCompactEnabled` from the highest source that sets it
    (`ei`): the `--settings` DOOR, then the local, project and user files
    (less any a recorded `--setting-sources` leaves out), then the legacy
    global config. Only `false` is off; a file that cannot be read sets
    nothing."""
    if door is not None and AUTO_COMPACT_ENABLED in door:
        return door[AUTO_COMPACT_ENABLED] is not False
    files = claude_settings_files(env.get(PROJECT_ENV) or None, env)
    sources = _flag(argv, SOURCES_FLAG)
    kept = None if sources is None else {name.strip() for name in sources.split(",")}
    for name, path in reversed(list(zip(SOURCE_NAMES, files))):
        if kept is not None and name not in kept:
            continue
        data = _json_object(path)
        if data is not None and AUTO_COMPACT_ENABLED in data:
            return data[AUTO_COMPACT_ENABLED] is not False
    legacy = _json_object(global_config(env))
    return legacy is None or legacy.get(AUTO_COMPACT_ENABLED) is not False


def read(sid: str | None, env: Mapping[str, str] | None = None) -> Point | None:
    """Session SID's SET compaction point (the module doc's sources, in
    order), or None: compaction off, or nothing a hook can see sets a
    window. Never raises for a file it cannot read: a warning never breaks
    a turn, and an unreadable source is an unknown point."""
    env = os.environ if env is None else env
    if any(_truthy(env.get(name)) for name in OFF_ENVS):
        return None
    argv = launch_argv(sid, env) or []
    door = _flag(argv, SETTINGS_FLAG)
    data = _settings_file(door, env) if door else None
    point = _window_point(argv, door, data, env)
    if point is None or not compaction_enabled(data, argv, env):
        return None
    return point


def _window_point(argv: list[str], door: str | None, data: dict | None,
                  env: Mapping[str, str]) -> Point | None:
    """The point cut from the first window a hook can see (the module
    doc's sources, in order), or None."""
    pct = _pct(env)
    window = env_window(env[WINDOW_ENV]) if WINDOW_ENV in env else None
    if window is not None:
        return Point(threshold(window, pct), window, WINDOW_ENV)
    flag = _flag(argv, WINDOW_FLAG)
    if flag is not None:                    # the flag outranks every settings file
        window = flag_window(flag)
        if window is None:                  # `auto`: Claude Code's own choice
            return None
        return Point(threshold(window, pct), window, WINDOW_FLAG)
    value = data.get(AUTO_COMPACT_WINDOW) if data is not None else None
    if is_window(value):
        return Point(threshold(value, pct), value, f"{SETTINGS_FLAG} {door}")
    return None


def compact_at(sid: str | None, env: Mapping[str, str] | None = None) -> int | None:
    """The token count where session SID auto-compacts, as set (`read`), or
    None when it is unknown. The one figure the hard line's `{left}` and
    `{compact_at}` come from."""
    point = read(sid, env)
    return point.at if point is not None else None


__all__ = [
    "AUTO_COMPACT_ENABLED",
    "AUTO_COMPACT_WINDOW",
    "BUFFER",
    "CONFIG_DIR_ENV",
    "GLOBAL_CONFIG",
    "IDLE_COMPACTION",
    "OFF_ENVS",
    "OUTPUT_RESERVE",
    "PCT_ENV",
    "PROJECT_ENV",
    "SETTINGS_FLAG",
    "SOURCES_FLAG",
    "SOURCE_NAMES",
    "WINDOW_AUTO",
    "WINDOW_ENV",
    "WINDOW_FLAG",
    "WINDOW_MAX",
    "WINDOW_MIN",
    "Point",
    "claude_settings_files",
    "compact_at",
    "compaction_enabled",
    "env_window",
    "flag_window",
    "global_config",
    "is_window",
    "launch_argv",
    "read",
    "threshold",
]
