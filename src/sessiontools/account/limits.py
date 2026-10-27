"""The limits file: `session account status`'s second usage source.

Some hosts run no usage exporter. Their status line can still write the
plan's own usage windows to a small JSON file (a "limits tap"):

    {"five_hour": {"used_percentage": N, "resets_at": EPOCH},
     "seven_day": {"used_percentage": N, "resets_at": EPOCH},
     "written_at": EPOCH, "account": "default"}

Each window is optional. `status` asks the exporter first, as before. Only
when the exporter has no answer AND the host names this file does `status`
read it (`fallback`).

**Which file is a setting, never a guess.** The `session` setting
`account.limits_file` names it, read through the template and host layers
of the settings cascade (`~/.sessionkit/tools/settings/session.json`, or a file under
`session.d/`), the way `new` reads `account.passage_prefix`. A seat, agent or
session layer cannot set it, so one seat cannot be pointed at another gauge.
It has NO default: unset, `status` prints what it printed before.

What is read, and what is refused:

- Windows `five_hour` and `seven_day` become `5h` and `7d`, the exporter's
  own window labels, so one query reads either source. Other keys are
  ignored. A window that is absent is left out.
- `written_at` is required: without it the file's age cannot be told, and an
  old file must never read as current. Past `STALE_AFTER` seconds the file
  is marked stale; its numbers are still shown.
- A window whose `resets_at` has passed measured a window that is over. It
  reads as the exporter's does (`status.stale_text`): STALE, when it ended,
  its percent not shown (`expired`, `stale`), never guessed to be 0.
- `account` must be absent or `"default"`, the host's own sign-in: any other
  account's window is not the host's usage.

Anything else (no file, unreadable, too big, not JSON, a bad field) is a
reason, and `status` reports usage as unavailable with it. Nothing from the
file is echoed but numbers, and an account name only when it is name-shaped.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from sessiontools.account import registry as reg
from sessiontools.account import status as st

#: The settings tool and dotted key that name the limits file:
#: `{"account": {"limits_file": "..."}}` in the `session` tool's template or
#: host layer. No default.
SETTING_TOOL = "session"
SETTING_KEY = "account.limits_file"

#: Seconds after `written_at` past which the file is stale.
STALE_AFTER = 900
#: A `written_at` this far ahead of the clock is refused, not clamped.
FUTURE_SLACK = 60
#: The file is a few hundred bytes; anything this big is not it.
MAX_BYTES = 64 * 1024
#: The file's window keys, and the exporter's labels for them, in order.
WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"))
#: The account the file must carry, if it carries one: the host's sign-in.
HOST_ACCOUNT = "default"

_WANTED = "it must be the path of the limits file, absolute or starting with ~"
_SHOWN_ACCOUNT = re.compile(r"[A-Za-z0-9._@+-]{1,64}")


class LimitsError(Exception):
    """A refused setting or settings layer. Names the key and the file that
    set it, never a token-shaped value."""


# --- the setting -----------------------------------------------------------------------


def _token_shaped(text: str) -> bool:
    return "sk-ant-" in text or bool(reg._TOKEN_SHAPE.search(text))


def check_setting(value: object, where: str = SETTING_KEY,
                  home: str | os.PathLike | None = None) -> Path:
    """VALUE as the limits file's path: a non-empty printable string, not
    token-shaped, absolute once `~` is expanded (against HOME when given).
    Else LimitsError naming WHERE; a token-shaped value is never quoted, and
    any other only when it is short."""
    if not isinstance(value, str):
        raise LimitsError(f"{where} is a {type(value).__name__}: " + _WANTED)
    if _token_shaped(value):
        raise LimitsError(f"{where} looks like a token, not a path (the value is not "
                          "shown): " + _WANTED)
    if not value:
        raise LimitsError(f"{where} is empty: " + _WANTED)
    if not value.isprintable():
        raise LimitsError(f"{where} holds unprintable characters (not shown): " + _WANTED)
    shown = repr(value) if len(value) <= 120 else "a long string"
    if home is not None and (value == "~" or value.startswith("~/")):
        path = os.fspath(home) + value[1:]
    else:
        path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise LimitsError(f"{where} is {shown}, not an absolute path: " + _WANTED)
    return Path(path)


def limits_setting(environ: Mapping[str, str] | None = None,
                   home: str | os.PathLike | None = None) -> Path | None:
    """SETTING_KEY through the `session` tool's template and host layers, or
    None when no layer sets it. HOME, when given, is the home those layers
    are read under (and `~` expands to). A layer file the reader refuses,
    or a bad value, is LimitsError."""
    from sessiontools import settings

    env = dict(os.environ if environ is None else environ)
    if home is not None:
        env["HOME"] = os.fspath(home)
    try:
        merged, origins, _locked, _leaves = settings.merge(
            settings.sources(SETTING_TOOL, None, env=env))
    except (settings.SettingsError, OSError, ValueError) as error:
        raise LimitsError(f"settings: {error}") from None
    node: object = merged
    for part in SETTING_KEY.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    if node is None:
        return None
    where = f"{SETTING_KEY} (in {origins.get(SETTING_KEY, SETTING_TOOL)})"
    return check_setting(node, where, env.get("HOME"))


# --- the file --------------------------------------------------------------------------


def _number(value: object) -> float | None:
    """VALUE as a finite number, or None. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _unreadable(path: Path, why: str) -> st.Usage:
    return st.Usage(None, unavailable=f"{path} could not be read ({why})")


def _malformed(path: Path, why: str) -> st.Usage:
    return st.Usage(None, unavailable=f"{path} is malformed ({why})")


def read_limits(path: str | os.PathLike, now: float) -> st.Usage:
    """The usage the limits file at PATH holds, as of NOW (epoch seconds).
    Every refusal is `Usage.unavailable` with the reason, never a raise; the
    file's content is never quoted."""
    path = Path(path)
    # Opened without blocking and checked to be a regular file before any
    # read: a FIFO (or a device) at the path would otherwise hang `status`.
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return st.Usage(None, unavailable=f"no file at {path}")
    except OSError as error:
        return _unreadable(path, error.strerror or type(error).__name__)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return _unreadable(path, "not a regular file")
        blob = b""
        while len(blob) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(blob))
            if not chunk:
                break
            blob += chunk
    except OSError as error:
        return _unreadable(path, error.strerror or type(error).__name__)
    finally:
        os.close(fd)
    if len(blob) > MAX_BYTES:
        return _malformed(path, f"larger than {MAX_BYTES} bytes")
    try:
        data = json.loads(blob.decode("utf-8"))
    except UnicodeDecodeError:
        return _malformed(path, "not UTF-8")
    except (json.JSONDecodeError, RecursionError):
        return _malformed(path, "not JSON")
    if not isinstance(data, dict):
        return _malformed(path, "not a JSON object")

    account = data.get("account")
    if account is not None and account != HOST_ACCOUNT:
        if (isinstance(account, str) and _SHOWN_ACCOUNT.fullmatch(account)
                and not _token_shaped(account)):
            whose = f"account {account}'s window"
        else:
            whose = "another account's window (its name is not shown)"
        return st.Usage(None, unavailable=f"{path} holds {whose}, not the host's")

    written_at = _number(data.get("written_at"))
    if written_at is None:
        return _malformed(path, "written_at is missing or not a number")
    age = now - written_at
    if age < -FUTURE_SLACK:
        return _malformed(path, "written_at is in the future")

    age_s = max(0, int(age))
    windows = []
    for key, label in WINDOWS:
        raw = data.get(key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            return _malformed(path, f"{key} is not an object")
        percent = _number(raw.get("used_percentage"))
        if percent is None or percent < 0:
            return _malformed(path, f"{key}.used_percentage is not a number of 0 or more")
        resets_at = _number(raw.get("resets_at"))
        expired = resets_at is not None and resets_at <= now
        windows.append(st.Window(label, None, None if expired else percent, resets_at,
                                 expired=expired, age_s=age_s,
                                 ended_s=max(0, int(now - resets_at)) if expired else None))
    if not windows:
        return st.Usage(None, unavailable=f"{path} has no usage windows")
    return st.Usage(None, tuple(windows), source=st.SOURCE_FILE, file=str(path),
                    written_at=written_at, age_s=age_s, stale=age_s > STALE_AFTER)


# --- the fallback ----------------------------------------------------------------------


def fallback(usage: st.Usage, now: float, environ: Mapping[str, str] | None = None,
             home: str | os.PathLike | None = None) -> st.Usage:
    """USAGE when it answered, or when no layer names a limits file (exactly
    today's result). Else the file's usage, or USAGE still unavailable with
    the file's reason added after the exporter's."""
    if usage.unavailable is None:
        return usage
    try:
        path = limits_setting(environ, home)
    except LimitsError as error:
        return replace(usage, unavailable=f"{usage.unavailable}; limits file: {error}")
    if path is None:
        return usage
    found = read_limits(path, now)
    if found.unavailable is not None:
        return replace(usage, unavailable=f"{usage.unavailable}; limits file: "
                                          f"{found.unavailable}")
    return found
