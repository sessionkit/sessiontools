"""Pin the default model: put `"model"` back in Claude Code's settings.json.

The core of `session default-model`. Nothing here prints or parses argv; a
verb calls `enforce()` and reports its `Outcome`.

**Opt-in.** The pin is the `session` tool's `model.default` setting, read
from the template and host layers only (one settings.json serves every seat,
so the pin is a HOST fact)::

    ~/.sessionkit/tools/settings/session.json   {"model": {"default": "opus"}}

UNSET is anything but a non-empty string: no key, `null`, `""`, a number, a
block that is not a map, a file that does not parse or cannot be read.
UNSET does nothing at all: settings.json is not read, no backup, no log, no
write.

**What a pin may name** is a closed list shipped with the package (`MODELS`):
the aliases plus every model id in `window.WINDOWS`. A value off the list is
refused, so a typo never becomes every new seat's default. **Fable is never
pinned**, whatever the environment says: a pin is a standing default for
every later launch, so the start gate's one-launch override does not apply.

**The write** is a raw-text edit of the top-level `"model"` member only, in
this order:

1. read the bytes and parse them; a file that does not parse is refused;
2. locate the top-level members (a walker over `raw_decode`, standard
   library only); a duplicate top-level `"model"`, or one whose value is an
   object or array, is refused; a nested `"model"` is never touched;
3. edit: replace the value's span, or insert the member first, or create
   the file when only the config dir exists (an absent config dir is
   refused: it is not ours to create);
4. validate: the new parse equals the old with only `model` changed, and
   every byte outside the edited span is identical;
5. back up the old bytes into the state dir (`default-model/backups/`),
   read the copy back, keep the newest `BACKUPS_KEPT`;
6. re-stat: if the file changed since step 1, start the pass again, up to
   `PASSES` times with a short backoff, then refuse;
7. replace atomically (a temp file beside the target, mode kept, a
   symlinked settings.json written at its target);
8. read back and check `model` is the pin;
9. log one JSON line to `default-model/log.jsonl`.

**A byte-order mark is refused**, not preserved: Claude Code writes none and
its own parser rejects one, so a file that starts with one is not a file
this tool can vouch for.

Exit codes: 0 done, in sync or unset; 3 refused, nothing written; 4 a
backup, write or read-back failed; 5 settings.json unreadable.
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sessiontools.window import WINDOWS

SETTINGS_TOOL = "session"
SETTINGS_BLOCK = "model"
SETTINGS_KEY = "default"
KEY = "model"

#: Aliases Claude Code resolves to the newest model in a family.
ALIASES: tuple[str, ...] = ("opus", "sonnet", "haiku")

STATE_SUBDIR = "default-model"
BACKUP_PREFIX = "settings.json."
BACKUPS_KEPT = 10
#: Passes before a file that keeps changing under us is refused.
PASSES = 3
BACKOFF = (0.05, 0.2)

OK, REFUSED, FAILED, UNREADABLE = 0, 3, 4, 5

_BACKUP_RE = re.compile(r"^settings\.json\.(\d{8}-\d{6})(?:-(\d+))?$")
_WS = re.compile(r"[ \t\n\r]*")
_BOM = b"\xef\xbb\xbf"


# --- Fable ------------------------------------------------------------------

def is_fable(value: object) -> bool:
    """The start gate's own rule (`start.policy._is_fable_model`), kept here
    because importing `sessiontools.start` loads PyYAML. A test pins the two
    to the same answer."""
    return "fable" in str(value).lower()


# --- the list ---------------------------------------------------------------

@dataclass(frozen=True)
class Model:
    name: str
    kind: str          # "alias" | "id"
    pinnable: bool
    why: str           # "" when pinnable


def models() -> tuple[Model, ...]:
    """The closed list a pin may name: the aliases, then every id in
    `window.WINDOWS`. Fable rows are listed and never pinnable."""
    rows = [(n, "alias") for n in ALIASES] + [(n, "id") for n in WINDOWS]
    return tuple(Model(n, k, not is_fable(n), "never pinned (Fable)" if is_fable(n) else "")
                 for n, k in rows)


def check_pin(value: str) -> str | None:
    """Why VALUE may not be pinned, or None when it may."""
    if is_fable(value):
        return f"model.default {json.dumps(value)} is Fable: Fable is never pinned"
    if value not in {m.name for m in models()}:
        return (f"model.default {json.dumps(value)} is not a known model: "
                "`session default-model list` names the ones a pin may use")
    return None


# --- the setting ------------------------------------------------------------

@dataclass(frozen=True)
class Setting:
    value: str | None   # the pin, or None when UNSET
    why: str            # what was found, said either way


def setting(environ: Mapping[str, str] | None = None) -> Setting:
    """`model.default` from the `session` tool's template and host layers.
    Nothing a settings file holds raises out of here: a bad file is UNSET."""
    from sessiontools import settings
    env = os.environ if environ is None else environ
    try:
        merged, _o, _l, _v = settings.merge(settings.sources(SETTINGS_TOOL, None, env=env))
    except Exception as e:  # noqa: BLE001 - a bad file reads as nothing configured
        return Setting(None, f"the session settings could not be read ({e.__class__.__name__})")
    block = merged.get(SETTINGS_BLOCK)
    if block is None:
        return Setting(None, "model.default is not set")
    if not isinstance(block, Mapping):
        return Setting(None, "the model block is not a map")
    value = block.get(SETTINGS_KEY)
    if not isinstance(value, str) or value == "":
        return Setting(None, f"model.default is {json.dumps(value)}, not a model name")
    return Setting(value, f"model.default is {json.dumps(value)}")


# --- where ------------------------------------------------------------------

def settings_path() -> Path:
    """`$CLAUDE_CONFIG_DIR/settings.json`, else `~/.claude/settings.json`
    (never resolved; a symlink is written at its target)."""
    from sessiontools.statusline import config_dir
    return config_dir() / "settings.json"


def state_dir(environ: Mapping[str, str] | None = None) -> Path:
    from sessiontools.snapshot import state_dir as root
    return root(environ) / STATE_SUBDIR


# --- the walker -------------------------------------------------------------

class Refused(Exception):
    """Nothing written; the message says why."""


@dataclass(frozen=True)
class Member:
    key: str
    key_start: int
    key_end: int
    value_start: int
    value_end: int


@dataclass(frozen=True)
class Shape:
    open: int                  # index of the top-level `{`
    close: int                 # index of its `}`
    members: tuple[Member, ...]


def walk(text: str) -> Shape:
    """The top-level members of a JSON object, with exact spans. TEXT has
    already parsed; a top level that is not an object is refused."""
    dec = json.JSONDecoder()
    i = _WS.match(text, 0).end()
    if i >= len(text) or text[i] != "{":
        raise Refused("settings.json is not a JSON object")
    open_ = i
    i = _WS.match(text, i + 1).end()
    out: list[Member] = []
    if text[i] == "}":
        return Shape(open_, i, ())
    while True:
        key, key_end = dec.raw_decode(text, i)
        j = _WS.match(text, key_end).end()      # at ':'
        vstart = _WS.match(text, j + 1).end()
        _value, vend = dec.raw_decode(text, vstart)
        out.append(Member(key, i, key_end, vstart, vend))
        k = _WS.match(text, vend).end()
        if text[k] == "}":
            return Shape(open_, k, tuple(out))
        i = _WS.match(text, k + 1).end()        # past ','


# --- the edit ---------------------------------------------------------------

@dataclass(frozen=True)
class Edit:
    new: str
    start: int           # the edited span in the OLD text: [start, end)
    end: int
    inserted: str        # what replaced it
    pathway: str         # "replaced" | "inserted"
    before: object       # the old value, None when absent


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def plan_edit(text: str, pin: str) -> Edit | None:
    """The raw-text edit that makes top-level `model` the pin, or None when
    it already is. Raises Refused."""
    try:
        old = json.loads(text)
    except ValueError:
        raise Refused("settings.json does not parse as JSON (mid-write, or not "
                      "plain JSON): the next change retries") from None
    if not isinstance(old, dict):
        raise Refused("settings.json is not a JSON object")
    shape = walk(text)
    hits = [m for m in shape.members if m.key == KEY]
    if len(hits) > 1:
        raise Refused('settings.json has more than one top-level "model"')
    value = json.dumps(pin)
    if hits:
        m = hits[0]
        current = old[KEY]
        if isinstance(current, dict | list):
            raise Refused('settings.json\'s "model" is an object or array, not a model name')
        if current == pin and isinstance(current, str):
            return None
        edit = Edit(text[:m.value_start] + value + text[m.value_end:],
                    m.value_start, m.value_end, value, "replaced", current)
    elif shape.members:
        first = shape.members[0]
        lead = text[shape.open + 1:first.key_start]        # the first member's indent
        colon = text[first.key_end:first.value_start]      # its `: ` spelling
        piece = f"{lead}{json.dumps(KEY)}{colon}{value},"
        at = shape.open + 1
        edit = Edit(text[:at] + piece + text[at:], at, at, piece, "inserted", None)
    else:
        nl = _newline(text)
        piece = f"{{{nl}  {json.dumps(KEY)}: {value}{nl}}}"
        edit = Edit(text[:shape.open] + piece + text[shape.close + 1:],
                    shape.open, shape.close + 1, piece, "inserted", None)
    _validate(text, old, edit, pin)
    return edit


def _validate(text: str, old: dict, edit: Edit, pin: str) -> None:
    """The new text parses to the old with only `model` changed, and every
    byte outside the edited span is the old one."""
    want = dict(old)
    want[KEY] = pin
    try:
        got = json.loads(edit.new)
    except ValueError:
        raise Refused("the edited text does not parse: nothing written") from None
    if got != want:
        raise Refused("the edit would change more than \"model\": nothing written")
    tail = len(text) - edit.end
    if (edit.new[:edit.start] != text[:edit.start]
            or edit.new[len(edit.new) - tail:] != text[edit.end:]
            or edit.new[edit.start:len(edit.new) - tail] != edit.inserted):
        raise Refused("the edit would touch bytes outside \"model\": nothing written")


def created_text(pin: str) -> str:
    return f'{{\n  {json.dumps(KEY)}: {json.dumps(pin)}\n}}\n'


# --- backups and the log ----------------------------------------------------

def _backup_order(name: str) -> tuple[str, int]:
    m = _BACKUP_RE.match(name)
    assert m
    return (m.group(1), int(m.group(2) or 1))


def backups(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Our backups, oldest first. Only our own name pattern counts."""
    d = state_dir(environ) / "backups"
    try:
        names = [n for n in os.listdir(d) if _BACKUP_RE.match(n)]
    except FileNotFoundError:
        return []
    return [d / n for n in sorted(names, key=_backup_order)]


def _backup(data: bytes, environ: Mapping[str, str] | None, now: float) -> Path:
    d = state_dir(environ) / "backups"
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        # Past the highest number this second already holds, never into a
        # gap the ring pruned: a reused name would sort as the oldest.
        held = [_backup_order(p.name)[1] for p in backups(environ)
                if _backup_order(p.name)[0] == stamp]
        n = max(held, default=0) + 1
        dest = d / (BACKUP_PREFIX + stamp + (f"-{n}" if n > 1 else ""))
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        ok = dest.read_bytes() == data
    except OSError as e:
        raise _Failed(f"the backup did not land ({e}): nothing written") from None
    if not ok:
        raise _Failed(f"the backup {dest} reads back different: nothing written")
    for old in backups(environ)[:-BACKUPS_KEPT]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def _log(environ: Mapping[str, str] | None, record: dict) -> None:
    d = state_dir(environ)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(d / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


# --- the pass ---------------------------------------------------------------

class _Failed(Exception):
    pass


class _Changed(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    code: int
    state: str              # unset | in-sync | planned | replaced | inserted | created
    #                         | refused | failed | unreadable
    message: str
    pin: str | None = None
    file: Path | None = None
    before: object = None
    backup: Path | None = None


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _mode(path: Path) -> int:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return 0o600


def enforce(environ: Mapping[str, str] | None = None, *, dry_run: bool = False,
            sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.time,
            between: Callable[[Path], None] | None = None) -> Outcome:
    """ONE pass: if the pin is set and settings.json's `model` differs, put
    it back. UNSET does nothing. `between` runs after the backup and before
    the re-stat (a test's stand-in for a concurrent writer)."""
    found = setting(environ)
    if found.value is None:
        return Outcome(OK, "unset", f"default-model: {found.why}: nothing to do")
    pin = found.value
    why = check_pin(pin)
    if why:
        return Outcome(REFUSED, "refused", f"default-model: {why}: nothing written", pin)
    path = settings_path()
    for attempt in range(PASSES):
        try:
            return _pass(pin, path, environ, dry_run, clock, between)
        except _Changed:
            if attempt + 1 < PASSES:
                sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)])
    return Outcome(REFUSED, "refused",
                   f"default-model: {path} changed while we read it, {PASSES} times: "
                   "nothing written; the next change retries", pin, path)


def _pass(pin: str, path: Path, environ: Mapping[str, str] | None, dry_run: bool,
          clock: Callable[[], float], between: Callable[[Path], None] | None) -> Outcome:
    if not path.parent.is_dir():
        return Outcome(REFUSED, "refused",
                       f"default-model: no Claude Code config dir at {path.parent}: "
                       "nothing written (it is not ours to create)", pin, path)
    from sessiontools.statusline import settings_target
    target = settings_target(path)
    sig = _signature(target)
    try:
        data = target.read_bytes() if sig is not None else None
    except FileNotFoundError:
        data, sig = None, None
    except OSError as e:
        return Outcome(UNREADABLE, "unreadable",
                       f"default-model: {path} is unreadable ({e})", pin, path)
    try:
        if data is None:
            new, pathway, before = created_text(pin), "created", None
        else:
            if data.startswith(_BOM):
                raise Refused("settings.json starts with a byte-order mark")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise Refused("settings.json is not UTF-8") from None
            edit = plan_edit(text, pin)
            if edit is None:
                return Outcome(OK, "in-sync",
                               f"default-model: model is {json.dumps(pin)} in {path}: in sync",
                               pin, path, pin)
            new, pathway, before = edit.new, edit.pathway, edit.before
    except Refused as e:
        return Outcome(REFUSED, "refused", f"default-model: {e}: nothing written", pin, path)
    said = (f"model {json.dumps(before) if data is not None and pathway == 'replaced' else 'absent'}"
            f" -> {json.dumps(pin)} in {path}")
    if dry_run:
        where = (f"; the backup would go in {state_dir(environ) / 'backups'}"
                 if data is not None else "")
        return Outcome(OK, "planned", f"default-model: would set {said} ({pathway}){where}",
                       pin, path, before)
    now = clock()
    try:
        backup = _backup(data, environ, now) if data is not None else None
    except _Failed as e:
        return Outcome(FAILED, "failed", f"default-model: {e}", pin, path, before)
    if between is not None:
        between(target)
    if _signature(target) != sig:
        raise _Changed
    from sessiontools.statusline import _write_bytes
    try:
        _write_bytes(target, new.encode("utf-8"), _mode(target))
    except OSError as e:
        return Outcome(FAILED, "failed",
                       f"default-model: the write to {path} failed ({e}); "
                       f"backup {backup}", pin, path, before, backup)
    try:
        back = json.loads(target.read_bytes().decode("utf-8"))
        ok = isinstance(back, dict) and back.get(KEY) == pin
    except (OSError, ValueError):
        ok = False
    if not ok:
        return Outcome(FAILED, "failed",
                       f"default-model: {path} does not read back with model "
                       f"{json.dumps(pin)}; backup {backup}", pin, path, before, backup)
    tail = f" (backup {backup})" if backup else ""
    try:
        _log(environ, {"ts": datetime.fromtimestamp(now, UTC).isoformat(),
                       "file": str(path), "before": before, "after": pin,
                       "backup": str(backup) if backup else None, "pathway": pathway})
    except OSError as e:
        return Outcome(FAILED, "failed",
                       f"default-model: {said}{tail}, but the log line did not land ({e})",
                       pin, path, before, backup)
    return Outcome(OK, pathway, f"default-model: {said}{tail}", pin, path, before, backup)
