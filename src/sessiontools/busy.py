"""The busy sound: a sound file plays once a tool batch has run past a
delay, and stops the moment the batch returns.

At its simplest it is just a busy sound: a file to play, a delay time, and it
always stops when the tool batch stops.

THREE PARTS, bottom to top:

- THE SETTINGS: `config()`, `batch_sound()` and the OFF cache.
- THE SOUND, the seam the signalled triggers use: `play(slot, file, delay)` and `stop(slot)`
  on a SLOT dir, one sound per slot. They know nothing of batches, payloads or
  settings. The signalled trigger (the user's line delivered while the seat is
  busy) calls them on its own slot with delay 0. The waiter carries the SPEECH
  GUARD, so every trigger has it: the delay counts only time
  when the mouth is silent, nothing starts while it speaks, and a sound that
  is playing is cut the moment it starts (`mouth_speaking()`).
- THE BATCH TRIGGER: `arm` (PreToolUse), `end` (PostToolBatch) and `clear`
  (Stop, UserPromptSubmit, SessionEnd), run by `main()` from the hook shim
  `hooks/busy-sound`. All the batch rules live here, and none of the process
  rules. `end` and `clear` stop EVERY slot (SLOTS), not only `busy/`, so a
  signalled sound stops at the same events.
- THE SIGNALS: `signal(event, sid, ref)`, the CLI's
  `session sound landed|given`. Another tool (the pager) says the moment;
  sessiontools decides whether anything plays. `landed` plays on
  `sounds/unread/` only when the trigger resolves to `landed`; `given` stops
  it. `finished` is HELD (its addressing is not yet decided) and not built.
- THE STOP SOUND: `stop_config`, `stop_sound` and `stop_play`. Stop's verb,
  `stop`, clears the slots as before, then plays the `stop` block's file
  once, through the mouth's queue. See THE STOP SOUND below.

THE SETTINGS are the `sounds` tool of the cascade (`sessiontools.settings`):
`~/.sessionkit/tools/settings/sounds.json`, the agent's front matter `settings:`,
`seats/<seat>/settings/sounds.json`, `sessions/<sid>/settings/sounds.json`
(each with its `sounds.d/*.json`). A main switch, then each sound's own block,
so the stop sound sits beside `busy`, and clip sounds can slot in too::

    {"enabled": true,
     "busy": {"auto_arm": true, "file": "~/x.wav", "delay": 8},
     "stop": {"file": "~/done.aiff"}}

`config()` is the sound, shared with the signalled (second) trigger. It knows
nothing of hook payloads or batches. It reports `auto_arm` and does not act on
it. `batch_sound()` is the batch trigger's reading: on only when the trigger
is `batch`, `auto_arm` is true AND `delay` is set.

THE TRIGGER: `busy.trigger` picks ONE mode.

- absent, or `"batch"`: a tool batch still running after `delay` (the
  original trigger, unchanged). `delay` is required.
- `"landed"`: the user's line is in the seat's queue, unread (the pager signals).
- `"finished"`: the ears have closed the user's utterance (the ears signal).
- anything else (a wrong type, an unknown word, a list, null): SILENT, in
  every mode. A typo never falls back to `batch`, as a bad `delay` never
  plays at 0.

Outside `batch`, `delay` is optional and defaults to 0 ("got it" at once).
A seat whose trigger is not `batch` is OFF for the batch trigger, so its
first arm stamps `off` and it pays what a seat with no setting pays.

THE PLAYER: `busy.player` picks who plays it.

- absent, or `"afplay"`: today's path, byte for byte: a detached waiter and
  afplay per sound (`play`/`stop`).
- `"engine"`: the resident sound engine (`sessiontools.sound`, engine.swift).
  The first arm of a session resolves here, writes the engine's RECORD,
  `<slot>/engine.<claude pid>` ({"v": 1, "file", "delay", "pid", "mouth"}),
  and sends `arm`. After that the shim sends every event with the native
  sender and no python; the engine owns the delay, the speech guard, the
  latch and the stop. The batch trigger only: `landed` keeps afplay.
- anything else, wrong case included: SILENT in every mode, as `trigger`.

The record is keyed on the claude pid because `claude --resume` keeps the
session id but not the process: a resumed seat misses the old record,
resolves once, and writes its own. Every resolve that does not come out
`engine` unlinks the records, and tells the engine `clear` if there were any,
so a sound the engine is counting cannot outlive the switch.

THE OFF CACHE: PreToolUse fires on every tool, and python costs 30-50 ms, so
the hook shim decides from `stat`s. The first arm of a session resolves once.
If the batch trigger comes out OFF, `resolve_for_batch()` stamps
`<session dir>/sounds/busy/off`, and the shim skips python for the rest of the
session unless a host- or session-layer `sounds` file is newer (`[ -nt ]`).
`/bin/sh`'s `-nt` compares WHOLE SECONDS (measured), so
the stamp is back-dated to one second before the resolve started: a file
written during or after the resolve is always newer to the second.

THE STOP SOUND: the seat's turn ended (Stop, the shim's `stop` verb). The
`stop` block holds one key, `file`, and is validated as `busy` is: silent
unless `enabled` is the boolean true, `stop` is a map and `stop.file` names a
readable regular file (`~` expanded, absolute). A bad value is silent, never
a fallback. A subagent's Stop (`agent_id`) plays nothing.

- It plays ONCE per Stop, in one DETACHED child (`stop_main`), so the hook
  never waits: `mouth play FILE` when `mouth` is on PATH, which queues it
  behind any line still speaking; `afplay FILE` only when the mouth is
  absent, exits non-zero (no device, no player) or cannot be run. A mouth
  that times out or is killed by a signal is NOT followed by afplay: it
  may have queued, and two sounds is worse than one late one. No `--device`: the mouth's own rule
  picks it.
- Its slot dir, `sounds/stop/`, is NOT in SLOTS: it holds only its OFF
  stamp, so no `clear` touches it, and once queued the sound is the
  mouth's (`mouth stop` flushes it).
- Its OFF stamp, `sounds/stop/off`, follows the busy stamp's rule (the
  shim tests it on `stop` only). Both the arm's resolve and Stop write it,
  so a seat that made a tool call before its first Stop starts no python
  at Stop unless its stop sound is ON.

NEVER RAISES out of `config()`, `batch_sound()` or `resolve_for_batch()`:
their callers are hooks, and a hook must never be the reason a turn breaks.
A layer file that will not parse, a missing PyYAML, a missing session dir:
each is silence, never an error.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import secrets
import shutil
import signal as _signal  # `signal` is the sound-signal verb below
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sessiontools import settings

TOOL = "sounds"
SOUND = "busy"
#: The batch trigger's OFF stamp, in its slot dir.
OFF = "off"
#: How far the OFF stamp is back-dated, in seconds: `/bin/sh -nt` compares
#: whole seconds, so one second is the least that always reads as older.
BACKDATE = 1.0
#: `busy.trigger`'s modes. Unset means BATCH.
BATCH = "batch"
LANDED = "landed"
FINISHED = "finished"
TRIGGERS = frozenset({BATCH, LANDED, FINISHED})
#: The slots under `<session dir>/sounds/`, one sound each: the batch
#: trigger's, `landed`'s (the user's line delivered, unread) and `finished`'s
#: (held). `end` and `clear` stop every one; the shim's fast path tests
#: each for a `token` or `pgid`, so its list must match this one.
UNREAD = "unread"
SLOTS = (SOUND, UNREAD, FINISHED)
#: `busy.player`'s values. Unset means AFPLAY.
AFPLAY = "afplay"
ENGINE = "engine"
PLAYERS = frozenset({AFPLAY, ENGINE})
#: The stop sound's block in the settings, and its slot dir (the OFF stamp
#: only). NOT in SLOTS: no `clear` touches it.
STOP = "stop"
#: The engine's record in the slot dir: `engine.<claude pid>`. The shim tests
#: `engine.$CLAUDE_PID` with one stat, and globs `engine.*` without one.
RECORD = "engine."


@dataclass(frozen=True)
class Sound:
    """The busy sound as the cascade sets it, validated. `file` is absolute
    (`~` expanded) and was readable when read. `delay` is seconds (>= 0, may
    be fractional); unset, it is None for the `batch` trigger (which needs
    it) and 0 for `landed` and `finished`. `auto_arm` is the batch trigger's
    switch. `trigger` is one of TRIGGERS, `batch` when unset. `player` is one
    of PLAYERS, `afplay` when unset; only the batch trigger reads it."""

    file: Path
    delay: float | None
    auto_arm: bool
    trigger: str = BATCH
    player: str = AFPLAY


# --- the cascade ---------------------------------------------------------------

def values(sid: str, env: Mapping[str, str] | None = None) -> dict:
    """The merged `sounds` settings of session SID: every layer, the agent
    and seat named by the session's own record (`settings.identity`).

    No PyYAML (the hooks' `python3 -I` may be a bare interpreter): the agent
    layer cannot be read, so it is dropped and the host, seat and session
    layers still count. Uses `sources` + `merge`, not `resolve`: `resolve`
    also checks the installed schema, which imports jsonschema - a cost a
    hook does not need, and `_valid` checks every key the hook reads.
    Raises `settings.SettingsError` (or OSError) on a file it cannot read;
    `config` turns that into silence."""
    agent, seat = settings.identity(sid, env)
    try:
        srcs = settings.sources(TOOL, sid, agent, seat, env)
    except ImportError:
        srcs = settings.sources(TOOL, sid, None, seat, env)
    merged, _origins, _locked, _leaves = settings.merge(srcs)
    return merged


def _number(value: Any) -> float | None:
    """VALUE as a finite number >= 0, else None. A boolean is not a number
    here, though Python counts it as an int."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _path(value: Any, env: Mapping[str, str] | None) -> Path | None:
    """VALUE as a readable regular file, `~` expanded against the HOME of ENV;
    else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    home = settings.home(env)
    if raw == "~":
        path = home
    elif raw.startswith("~/"):
        path = home / raw[2:]
    else:
        path = Path(raw)
    if not path.is_absolute():
        return None
    try:
        if not path.is_file() or not os.access(path, os.R_OK):
            return None
    except OSError:
        return None
    return path


def _valid(merged: Mapping[str, Any], env: Mapping[str, str] | None = None) -> Sound | None:
    """The busy sound in MERGED settings, or None (silent). Silent unless
    `enabled` is the boolean true, `busy` is a map and `busy.file` names a
    readable file. A `delay` that is SET must be a finite number >= 0, or
    the whole sound is silent (a typo never plays at 0). `auto_arm` is true
    only as the boolean true. A `trigger` that is SET must be one of
    TRIGGERS, exactly, or the whole sound is silent in every mode (a typo
    never falls back to `batch`). A `player` that is SET must be one of
    PLAYERS, exactly, by the same rule. Unset `delay` outside `batch` is 0."""
    if merged.get("enabled") is not True:
        return None
    block = merged.get(SOUND)
    if not isinstance(block, Mapping):
        return None
    trigger = block.get("trigger", BATCH)
    if not isinstance(trigger, str) or trigger not in TRIGGERS:
        return None
    player = block.get("player", AFPLAY)
    if not isinstance(player, str) or player not in PLAYERS:
        return None
    file = _path(block.get("file"), env)
    if file is None:
        return None
    delay = None
    if "delay" in block:
        delay = _number(block["delay"])
        if delay is None:
            return None
    elif trigger != BATCH:
        delay = 0.0
    return Sound(file=file, delay=delay, auto_arm=block.get("auto_arm") is True,
                 trigger=trigger, player=player)


def config(sid: str, env: Mapping[str, str] | None = None) -> Sound | None:
    """The busy sound for session SID, or None: unset, disabled, invalid, or
    unreadable is silence. Never raises. Knows nothing of hooks: the batch
    trigger and the signalled trigger both start here."""
    if not sid:
        return None
    try:
        return _valid(values(sid, env), env)
    except Exception:  # noqa: BLE001 - a hook's settings never break a turn
        return None


def batch_sound(sid: str, env: Mapping[str, str] | None = None) -> Sound | None:
    """The sound the BATCH trigger plays: `config` with the trigger `batch`
    (or unset), `auto_arm` true and a `delay` set. Anything else is OFF for
    the batch trigger: a `landed` or `finished` seat's arm stamps `off`, and
    the shim then starts no python for it."""
    sound = config(sid, env)
    if (sound is None or sound.trigger != BATCH or not sound.auto_arm
            or sound.delay is None):
        return None
    return sound


# --- the OFF cache -------------------------------------------------------------

def slot_dir(sid: str, env: Mapping[str, str] | None = None, name: str = SOUND) -> Path:
    """Slot NAME of session SID: `<session dir>/sounds/<name>/`. The default
    is the batch trigger's, `busy/`."""
    return settings.session_dir(sid, env) / TOOL / name


def _ensure_slot(slot: Path) -> bool:
    """SLOT and its parent `sounds/` as 0700 private dirs, never the session
    dir itself. False when the session dir is missing or anything fails."""
    try:
        settings.ensure_private_dir(slot.parent)
        settings.ensure_private_dir(slot)
    except OSError:
        return False
    return True


def mark_off(slot: Path, started: float) -> bool:
    """Stamp SLOT's `off`, its mtime back-dated to STARTED - 1 s. Re-stamps an
    `off` that is already there (a re-resolve that comes out OFF again must
    refresh the stamp, or the shim would start python on every tool). False
    when it could not be written."""
    if not _ensure_slot(slot):
        return False
    off = slot / OFF
    try:
        fd = os.open(off, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        stamp = started - BACKDATE
        os.utime(off, (stamp, stamp))
    except OSError:
        return False
    return True


def clear_off(slot: Path) -> None:
    """Remove SLOT's `off`, if any: the batch trigger resolved ON."""
    with contextlib.suppress(OSError):
        (slot / OFF).unlink()


def resolve_for_batch(sid: str, env: Mapping[str, str] | None = None,
                      now: Callable[[], float] = time.time) -> Sound | None:
    """What the arm runs first: the batch trigger's sound, or None. OFF
    stamps `off` (back-dated to before this resolve began); ON removes it.
    Anything but ON with `player: engine` also unlinks the engine's records
    (and tells the engine `clear` if there were any). Nothing is written when
    the session dir does not exist. Never raises."""
    return _resolve(sid, env, now)[0]


def _resolve(sid: str, env: Mapping[str, str] | None,
             now: Callable[[], float]) -> tuple[Sound | None, float]:
    """`resolve_for_batch`, and when it started (the record's back-date)."""
    started = now()
    if not sid:
        return None, started
    sound = batch_sound(sid, env)
    # The cache is an optimisation, never an error.
    with contextlib.suppress(Exception):
        slot = slot_dir(sid, env)
        if slot.parent.parent.is_dir():
            if sound is None:
                mark_off(slot, started)
            else:
                clear_off(slot)
            if (sound is None or sound.player != ENGINE) and drop_records(slot):
                _tell_engine("clear", sid, env)
            _stamp_stop(sid, env, started)
    return sound, started


# --- the engine's record -----------------------------------------------------------

def _record_pid(name: str) -> int | None:
    """The claude pid a record's file NAME carries, or None: not a record."""
    if not name.startswith(RECORD):
        return None
    tail = name[len(RECORD):]
    return int(tail) if tail.isdigit() else None


def records(slot: Path) -> list[Path]:
    """The engine's records in SLOT, `engine.<digits>`, sorted. Never raises."""
    try:
        return sorted(p for p in slot.iterdir() if _record_pid(p.name) is not None)
    except OSError:
        return []


def drop_records(slot: Path, keep: Path | None = None) -> int:
    """Unlink every record in SLOT but KEEP. How many went."""
    gone = 0
    for path in records(slot):
        if path != keep:
            with contextlib.suppress(OSError):
                path.unlink()
                gone += 1
    return gone


def write_record(slot: Path, sound: Sound, pid: int, started: float,
                 env: Mapping[str, str] | None = None) -> Path | None:
    """Write SLOT's record for the seat whose claude pid is PID: 0600,
    atomic (temp, then rename), its mtime back-dated to STARTED - 1 s as
    `off` is, so a layer file written during the resolve still reads newer
    to the shim's whole-second `-nt`. Unlinks every other record. The path,
    or None when it could not be written. Never raises."""
    if sound.delay is None or not _ensure_slot(slot):
        return None
    path = slot / f"{RECORD}{pid}"
    body = {"v": 1, "file": str(sound.file), "delay": sound.delay, "pid": pid,
            "mouth": str(mouth_dir(env))}
    tmp = slot / f".{path.name}.{os.getpid()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(body) + "\n")
        stamp = started - BACKDATE
        os.utime(tmp, (stamp, stamp))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - a hook never breaks a turn
        _unlink(tmp)
        return None
    drop_records(slot, keep=path)
    return path


def _tell_engine(event: str, sid: str, env: Mapping[str, str] | None) -> bool:
    """Send EVENT to the engine, but only when it is installed (its sender is
    executable): not installed counts as off, for python as for the shim."""
    from sessiontools import sound as engine
    try:
        return engine.installed(env) and engine.send(event, sid, env=env)
    except Exception:  # noqa: BLE001
        return False


# --- the sound: a slot, a token, a process group (the signal seam) -------------
#
# A SLOT is a directory holding at most one sound. `token` is the sentinel,
# made with O_EXCL so parallel callers arm it once; `pgid` names the waiter's
# process group and the token it serves. Only `stop` frees a slot: a sound that
# played out leaves it armed, so a long batch's next tool does not replay it.

TOKEN = "token"
PGID = "pgid"
#: The player: plays a file once, killable by its process group, always on
#: macOS. Resolved on PATH, so tests put a fake one first.
PLAYER: tuple[str, ...] = ("afplay",)
SRC = Path(__file__).resolve().parents[1]
#: How the waiter is run: the caller's own interpreter, `-I`, this src/.
WAIT_ENTRY = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from sessiontools.busy import wait_main; sys.exit(wait_main(sys.argv[2:]))")


def _read_token(slot: Path) -> str | None:
    try:
        return (slot / TOKEN).read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeError):
        return None


def _read_pgid(slot: Path) -> tuple[int, str] | None:
    try:
        pgid, token = (slot / PGID).read_text(encoding="ascii").split()
        return int(pgid), token
    except (OSError, UnicodeError, ValueError):
        return None


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def play(slot: str | Path, file: str | Path, delay: float = 0.0, *,
         claude_pid: int | None = None, player: Sequence[str] = PLAYER) -> str | None:
    """Arm SLOT: after DELAY seconds (0: at once) play FILE once, unless the
    slot was stopped first. Returns the new token, or None when the slot is
    armed already (the O_EXCL race lost) or anything failed. Never raises,
    and returns at once: the wait and the sound run in a DETACHED waiter, its
    own process group, stdio on /dev/null. CLAUDE_PID, when given, is checked
    after the delay: a seat that has gone plays nothing. SLOT and its parent
    are made 0700; the grandparent (a session dir) must exist."""
    slot = Path(slot)
    try:
        delay = float(delay)
        if not math.isfinite(delay) or delay < 0 or not _ensure_slot(slot):
            return None
        token = secrets.token_hex(8)
        try:
            fd = os.open(slot / TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return None
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(token)
    except Exception:  # noqa: BLE001 - a hook never breaks a turn
        return None
    try:
        argv = [sys.executable, "-I", "-c", WAIT_ENTRY, str(SRC), str(slot), token,
                repr(delay), str(file), str(claude_pid or ""), *player]
        waiter = subprocess.Popen(argv, start_new_session=True, close_fds=True, cwd="/",
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        _write_atomic(slot / PGID, f"{waiter.pid} {token}\n")
    except Exception:  # noqa: BLE001
        if _read_token(slot) == token:
            _unlink(slot / TOKEN)
        return None
    return token


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def stop(slot: str | Path) -> bool:
    """Free SLOT: the token goes first (no waiter can pass its check after
    this), then the waiter's process group is killed - the sleeping waiter or
    the player mid-sound, as one. True when a token was there to end. Never
    raises; safe on an empty or missing slot. A group is killed only when its
    `pgid` record names the token being ended (pid reuse)."""
    slot = Path(slot)
    try:
        ending = slot / f".{TOKEN}.ending.{os.getpid()}"
        try:
            os.replace(slot / TOKEN, ending)
        except FileNotFoundError:
            token = None
        else:
            token = ending.read_text(encoding="ascii").strip() or None
            _unlink(ending)
        record = _read_pgid(slot)
        if record is not None:
            pgid, owner = record
            if token is not None and owner == token and _group_alive(pgid):
                with contextlib.suppress(OSError):
                    os.killpg(pgid, _signal.SIGTERM)
            _unlink(slot / PGID)
        return token is not None
    except Exception:  # noqa: BLE001
        return False


# --- the speech guard: is the mouth speaking? --------------------------------------
#
# UNBLESSED: `playing.json` is the mouth's INTERNAL file (sessionvoice's mouth,
# which began as voicemode's), with no documented contract (its maintainers are
# asked to bless it or to name the surface they would rather we read).
# Every read of the mouth's state goes through MOUTH_PLAYING, MOUTH_PID and
# `mouth_speaking()` below, so a different surface is a one-line change there.
# WHERE the mouth lives is sessionvoice's rule, copied (not imported) into
# `mouth_dir()`: `$MOUTH_DIR`, else `<sessionvoice home>/mouth`.
# We only stat and read; we never write, lock or create anything in the mouth
# dir or under any home it is looked for in.

#: The mouth's "speaking now" file: written (tmp + rename) when its player
#: takes a line, before synthesis; unlinked when the line ends. UNBLESSED.
MOUTH_PLAYING = "playing.json"
#: The mouth player's pid file: unlinked in the player's `finally`, so a
#: clean exit clears it and a SIGKILL leaves a dead pid.
MOUTH_PID = "player.pid"
#: The waiter's poll, in seconds: the delay's countdown and the cut.
STEP = 0.1


#: sessionvoice's home variables, read in this order (its `SESSIONVOICE_HOME`,
#: then the old `VOICEMODE_BASE_DIR`).
MOUTH_HOME_ENV = ("SESSIONVOICE_HOME", "VOICEMODE_BASE_DIR")
#: The mouth dir's own variable, then its old name (presence, not truthiness).
MOUTH_DIR_ENV = ("MOUTH_DIR", "VOICEMODE_MOUTH_DIR")
#: sessionvoice's default home, then the old one, under $HOME.
MOUTH_HOMES = (".sessionvoice", ".voicemode")

#: The standard library's own pattern: an unclosed `${` takes the rest of the
#: value with it, so nothing after it expands.
_VAR = re.compile(r"\$(\w+|\{[^}]*\}?)", re.ASCII)


def _expandvars(raw: str, e: Mapping[str, str]) -> str:
    """`os.path.expandvars`, against the mapping E instead of the process env:
    `$NAME` and `${NAME}` when NAME is in E; anything else, an unclosed `${`
    and all that follows it included, is left as it is."""
    def one(m: re.Match[str]) -> str:
        name = m.group(1)
        if name.startswith("{"):
            if not name.endswith("}"):
                return m.group(0)
            name = name[1:-1]
        return e[name] if name in e else m.group(0)

    return _VAR.sub(one, raw) if "$" in raw else raw


def _populated(root: Path) -> bool:
    """sessionvoice's POPULATED: `<root>/logs/conversations` holds a
    `heard.lock` or a `heard_*.jsonl`. A dir that exists but holds no heard
    log never decides. A stat, never a write; an OSError is False."""
    d = root / "logs" / "conversations"
    try:
        return (d / "heard.lock").exists() or any(d.glob("heard_*.jsonl"))
    except OSError:
        return False


def mouth_dir(env: Mapping[str, str] | None = None) -> Path:
    """The mouth dir, by sessionvoice's own rule (its `mouth/paths.py` and
    `ears/home.py`), copied here so that nothing imports sessionvoice:

    1. `$MOUTH_DIR` if it is set, else the old `$VOICEMODE_MOUTH_DIR` if that
       is set (presence: a set `MOUTH_DIR` hides the old name even when
       empty). A non-empty value is the dir; an empty one falls through.
    2. Else `<home>/mouth`, where home is the first of: `$SESSIONVOICE_HOME`,
       `$VOICEMODE_BASE_DIR` (each only when non-empty, with `$VAR` and `~`
       expanded); `~/.sessionvoice` if it holds a heard log; `~/.voicemode`
       if it holds a heard log; else `~/.sessionvoice`.

    `~` and `$VAR` expand against ENV and its HOME. Only stats: nothing is
    written, locked or created under any home."""
    e = os.environ if env is None else env
    home = settings.home(env)

    def expanduser(raw: str) -> str:
        # `os.path.expanduser`, with HOME from ENV: HOME without its trailing
        # `/`, then the rest as given (`~//m` stays under HOME, not at `/m`).
        if raw == "~" or raw.startswith("~/"):
            return (str(home).rstrip("/") + raw[1:]) or "/"
        return os.path.expanduser(raw)           # `~user`, or no `~` at all

    def path_expanduser(raw: str) -> Path:
        # `Path(raw).expanduser()`, with HOME from ENV: it looks at the first
        # part of the parsed path, so `./~/m` is under HOME too.
        p = Path(raw)
        if p.anchor or not p.parts or not p.parts[0].startswith("~"):
            return p
        return Path(expanduser(p.parts[0])).joinpath(*p.parts[1:])

    name = next((n for n in MOUTH_DIR_ENV if n in e), None)
    explicit = e[name] if name is not None else None
    if explicit:
        return path_expanduser(explicit)
    for n in MOUTH_HOME_ENV:
        raw = e.get(n)
        if raw:
            return Path(expanduser(_expandvars(raw, e))) / "mouth"
    for root in (home / h for h in MOUTH_HOMES):
        if _populated(root):
            return root / "mouth"
    return home / MOUTH_HOMES[0] / "mouth"


def _pid_live(path: Path) -> bool:
    """PATH names a live process. Missing, empty or not a pid: False (the
    killed-player gap). Unreadable: True, which fails toward silence. Not a
    regular file (a FIFO, a directory, either reached through a link): False,
    and it never blocks. The open is O_NONBLOCK, so a FIFO with no writer
    cannot hang the hook; the fstat of that descriptor decides the kind. A
    link is followed, as the engine's `pidFileLive` does."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        chunks = []
        while chunk := os.read(fd, 4096):
            chunks.append(chunk)
        raw = b"".join(chunks).decode("ascii").strip()
    except (OSError, UnicodeError):
        return True
    finally:
        os.close(fd)
    return raw.isdigit() and int(raw) > 1 and _alive(int(raw))


def mouth_speaking(mouth: Path) -> bool:
    """The mouth in dir MOUTH is speaking (or synthesising): its
    MOUTH_PLAYING file is there AND its MOUTH_PID names a live process. The
    file is only stat'ed, never parsed, so an unreadable one still counts. No
    mouth dir: False, so the guard does nothing. Never raises. UNBLESSED
    surface: see MOUTH_PLAYING."""
    try:
        os.stat(mouth / MOUTH_PLAYING)
    except OSError:
        return False
    try:
        return _pid_live(mouth / MOUTH_PID)
    except Exception:  # noqa: BLE001 - a guard that cannot tell stays quiet
        return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def wait(slot: str | Path, token: str, delay: float, file: str | Path,
         claude_pid: int | None = None, player: Sequence[str] = PLAYER, *,
         mouth: str | Path | None = None, step: float = STEP,
         clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> int:
    """The waiter, inside its own process group. Play FILE once, after DELAY
    seconds of a SILENT mouth, if SLOT still holds TOKEN and CLAUDE_PID (if
    given) is alive. Exits when the sound ends; frees nothing.

    THE SPEECH GUARD, so every trigger has it:

    1. The delay counts silence. The waiter wakes every STEP (<= 100 ms);
       while the mouth speaks, the countdown goes back to the full DELAY.
       `delay: 0` means "as soon as the mouth is silent".
    2. The pre-play check: the token, the claude pid, and the mouth silent.
       Token and pid are checked on every step too, so a waiter held off by
       a long line still ends when its slot is stopped or its seat goes.
    3. While the player runs, the mouth is polled every STEP. If it starts,
       the player is killed and the waiter exits. The slot stays armed: no
       replay inside that batch.
    4. ANY session's speech counts: one mouth, one pair
       of ears. MOUTH is the mouth dir, `mouth_dir()` when None."""
    slot = Path(slot)
    here = mouth_dir() if mouth is None else Path(mouth)
    deadline = clock() + delay
    while True:
        if _read_token(slot) != token:
            return 0                    # stopped, or another sound's slot now
        if claude_pid and not _alive(claude_pid):
            return 0                    # the seat has gone
        if mouth_speaking(here):
            deadline = clock() + delay  # the countdown restarts at the full delay
            sleep(step)
            continue
        left = deadline - clock()
        if left <= 0:
            break
        sleep(min(step, left))
    try:
        child = subprocess.Popen([*player, str(file)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return 0
    if _read_token(slot) != token:
        child.kill()
        child.wait()
        return 0
    while True:
        try:
            child.wait(timeout=step)
        except subprocess.TimeoutExpired:
            if mouth_speaking(here):
                child.kill()            # the mouth started: never under its voice
                child.wait()
                return 0
        else:
            return 0


def wait_main(argv: Sequence[str]) -> int:
    """`SLOT TOKEN DELAY FILE CLAUDE_PID [PLAYER...]`, as `play` spawns it."""
    try:
        slot, token, delay, file, pid, *player = argv
        return wait(slot, token, float(delay), file,
                    int(pid) if pid.isdigit() else None, tuple(player) or PLAYER)
    except Exception:  # noqa: BLE001 - a detached waiter has no one to tell
        return 0


# --- whose seat: the claude pid ---------------------------------------------------

CLAUDE_PID_ENV = "CLAUDE_PID"
#: Bounds the ancestor walk: hook -> shim -> claude is two hops (a spike
#: measured it).
MAX_HOPS = 8


def _ps(pid: int) -> tuple[int, str] | None:
    """(ppid, command basename) of PID, by `ps`; None when it cannot say."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2, check=False).stdout
        ppid, comm = out.strip().split(None, 1)
        return int(ppid), os.path.basename(comm.strip())
    except Exception:  # noqa: BLE001
        return None


def claude_ancestor(start: int | None = None,
                    ps: Callable[[int], tuple[int, str] | None] = _ps) -> int | None:
    """The nearest ancestor of START (default: this process's parent) whose
    command is `claude`, or None. A FALLBACK only: a hook's env carries
    CLAUDE_PID. Never `os.getppid()` alone: the hook's python is the shim's
    child, and the shim exits with the hook (measured)."""
    pid = os.getppid() if start is None else start
    for _ in range(MAX_HOPS):
        if pid <= 1:
            return None
        found = ps(pid)
        if found is None:
            return None
        ppid, comm = found
        if comm == "claude":
            return pid
        pid = ppid
    return None


def claude_pid(env: Mapping[str, str],
               ancestor: Callable[[], int | None] = claude_ancestor) -> int | None:
    """`$CLAUDE_PID` from the hook's env (claude sets it, and overwrites an
    inherited one, as a spike measured), else the ancestor walk."""
    raw = env.get(CLAUDE_PID_ENV, "").strip()
    if raw.isdigit() and int(raw) > 1:
        return int(raw)
    return ancestor()


# --- the signals: `session sound landed|given` -------------------------------------
#
# Another tool says the MOMENT; sessiontools stays the one player and decides.
# The pager sends `landed` when it HOLDS a wake line for a busy seat instead of
# paging it, so the hold IS the busy verdict: nothing here reads busy (one
# reader, by design). `given` is the precise stop; the hook's
# `end` and `clear` stop the slot anyway. `finished` (the ears) is HELD.

#: The events `signal()` takes, and the one it does not build yet.
GIVEN = "given"
EVENTS = (LANDED, GIVEN)
HELD_EVENTS = (FINISHED,)
#: `signal()`'s decisions.
PLAYED = "played"
STOPPED = "stopped"
IGNORED = "ignored"


def is_sid(sid: object) -> bool:
    """True when SID can name a session dir: a non-empty string with no `/`
    that is not `.` or `..`, which would resolve to the sessions dir or its
    parent. The shim's `case` mirrors this; keep the two in step."""
    return isinstance(sid, str) and sid not in ("", ".", "..") and "/" not in sid


def _seat_pid(sid: str, env: Mapping[str, str] | None) -> int | None:
    """The seat's own claude pid, from its sidecar (`claude_code.find`), when
    that process is alive; else None. Never the signaller's env: the pager
    is not the seat."""
    from sessiontools import claude_code
    try:
        found = claude_code.find(sid, None if env is None else dict(env))
    except Exception:  # noqa: BLE001
        return None
    pid = found.pid if found is not None else None
    return pid if pid is not None and pid > 1 and _alive(pid) else None


def decide(event: str, sid: str, ref: str | None = None,
           env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """`signal()` with its reason: (decision, why). Raises ValueError for a
    usage error (an unknown or held event, a bad session id), and nothing
    else."""
    if event in HELD_EVENTS:
        raise ValueError(f"{event!r} is held (its addressing is not decided yet)")
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}: one of {', '.join(EVENTS)}")
    if not is_sid(sid):
        raise ValueError(f"not a session id: {sid!r}")
    try:
        slot = slot_dir(sid, env, UNREAD)
        if event == GIVEN:
            if stop(slot):
                return STOPPED, "the unread sound was stopped"
            return IGNORED, "nothing was armed in the unread slot"
        sound = config(sid, env)
        if sound is None:
            return IGNORED, "no busy sound: unset, disabled, or invalid"
        if sound.trigger != LANDED:
            return IGNORED, f"the trigger is {sound.trigger!r}, not 'landed'"
        if not slot.parent.parent.is_dir():
            return IGNORED, "no session dir"
        pid = _seat_pid(sid, env)
        if pid is None:
            return IGNORED, "no live seat for this session (no sidecar, or its pid is gone)"
        if play(slot, sound.file, sound.delay or 0.0, claude_pid=pid) is not None:
            return PLAYED, f"armed the unread slot, delay {sound.delay or 0.0:g}s"
        if (slot / TOKEN).exists():
            return IGNORED, "the unread slot is armed already: one sound per run of lines"
        return IGNORED, "the unread slot could not be armed"
    except Exception as e:  # noqa: BLE001 - a signal never breaks its sender
        return IGNORED, f"failed: {type(e).__name__}"


def signal(event: str, sid: str, ref: str | None = None,
           env: Mapping[str, str] | None = None) -> str:
    """Signal EVENT for session SID: `landed` (the user's line is held, unread,
    for this busy seat) or `given` (it was given). Returns PLAYED, STOPPED or
    IGNORED.

    - `landed`: the sound from `config()` through the full cascade; IGNORED
      unless its trigger is `landed`. Then `play()` on `sounds/unread/` at
      its delay (unset: 0), checked against the seat's own claude pid. A
      second `landed` while the slot is armed is IGNORED (one sound per run
      of lines). The waiter's speech guard applies.
    - `given`: `stop()` the `unread` slot.

    REF (a Message-ID) is kept for the caller's trail only. Raises
    ValueError on a usage error; otherwise never raises."""
    return decide(event, sid, ref, env)[0]


# --- the batch trigger: the hook verbs ------------------------------------------

SWITCH_ENV = "SESSIONTOOLS_HOOK_BUSY"
SID_ENV = "CLAUDE_CODE_SESSION_ID"
OFF_WORDS = frozenset({"off", "0", "false", "no"})
#: `stop` is Stop's verb: `clear` to the sound, then the stop sound. The hook
#: shim tells it apart from UserPromptSubmit's `clear` for the turn-ended mark.
VERBS = ("arm", "end", "stop", "clear")


def _sid(payload: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
    """The session id the shim used: `$CLAUDE_CODE_SESSION_ID`, else the
    payload's `session_id` (they are equal in every hook, as a spike measured)."""
    sid = env.get(SID_ENV) or payload.get("session_id")
    return sid if is_sid(sid) else None


def arm(payload: Mapping[str, Any], env: Mapping[str, str]) -> str | None:
    """PreToolUse: on the first tool of a batch, play the busy sound after its
    delay. A subagent's tool arms nothing. Returns the token (afplay), ENGINE
    when the engine was told `arm`, or None.

    `player: engine`: write the record for this seat's claude pid, then send
    `arm` (the engine counts the delay). The shim sends every later arm
    itself, with no python, while the record is fresh. No claude pid means
    no record and no sound."""
    if payload.get("agent_id"):
        return None
    sid = _sid(payload, env)
    if sid is None:
        return None
    sound, started = _resolve(sid, env, time.time)
    if sound is None or sound.delay is None:
        return None
    pid = claude_pid(env)
    if sound.player == ENGINE:
        if pid is None or write_record(slot_dir(sid, env), sound, pid, started, env) is None:
            return None
        return ENGINE if _tell_engine("arm", sid, env) else None
    return play(slot_dir(sid, env), sound.file, sound.delay, claude_pid=pid)


def end(payload: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """PostToolBatch: the batch is over, so the sound stops. A SUBAGENT's
    batch end is skipped: a foreground Agent call is one tool in the parent's
    batch, and the parent's sound plays until it returns (measured in a spike)."""
    if payload.get("agent_id"):
        return False
    return clear(payload, env)


def clear(payload: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """Stop, UserPromptSubmit, SessionEnd (and `end`): stop whatever is armed
    in EVERY slot (SLOTS: `busy`, `unread`, `finished`), so no interrupted
    batch leaves a sound or a sentinel behind, and the user's line that rides
    along at the batch end, or wakes the seat as a prompt, stops its
    `landed` sound. True when any slot had a token.
    Never touches the OFF cache."""
    sid = _sid(payload, env)
    if sid is None:
        return False
    stopped = [stop(slot_dir(sid, env, name)) for name in SLOTS]
    return any(stopped)


# --- the stop sound: the seat's turn ended ------------------------------------------
#
# Stop's verb plays the `stop` block's file once, after `clear`. One detached
# child does the playing, so the hook returns at once: the mouth first (its
# queue keeps the order behind a line still speaking), afplay only when the
# mouth is absent or refuses. Its slot dir holds only the OFF stamp.

#: The mouth's CLI, resolved on PATH, so tests put a fake first. Called as
#: `mouth play FILE`: it queues FILE as a line and returns.
MOUTH: tuple[str, ...] = ("mouth",)
#: How long the child waits for `mouth play` before it gives up, in seconds.
#: A mouth that times out may have queued, so no afplay follows it.
MOUTH_TIMEOUT = 5.0
#: How the stop child is run: the caller's own interpreter, `-I`, this src/.
STOP_ENTRY = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "from sessiontools.busy import stop_main; sys.exit(stop_main(sys.argv[2:]))")


def stop_config(sid: str, env: Mapping[str, str] | None = None) -> Path | None:
    """The stop sound's file for session SID, or None (silent). Silent unless
    `enabled` is the boolean true, `stop` is a map and `stop.file` names a
    readable regular file (`~` expanded against the HOME of ENV, absolute).
    Other keys in the block are ignored. Never raises."""
    if not sid:
        return None
    try:
        merged = values(sid, env)
        if merged.get("enabled") is not True:
            return None
        block = merged.get(STOP)
        if not isinstance(block, Mapping):
            return None
        return _path(block.get("file"), env)
    except Exception:  # noqa: BLE001 - a hook's settings never break a turn
        return None


def _stamp_stop(sid: str, env: Mapping[str, str] | None, started: float) -> Path | None:
    """Resolve the stop sound and keep its OFF stamp: OFF stamps
    `sounds/stop/off` back-dated to before STARTED, ON removes it. The file,
    or None. Writes only under `sounds/stop/`, and nothing at all when the
    session dir does not exist. The arm's resolve and Stop both call it.
    Never raises."""
    file = stop_config(sid, env)
    with contextlib.suppress(Exception):
        slot = slot_dir(sid, env, STOP)
        if slot.parent.parent.is_dir():
            if file is None:
                mark_off(slot, started)
            else:
                clear_off(slot)
    return file


def stop_play(file: str | Path, *, mouth: Sequence[str] = MOUTH,
              player: Sequence[str] = PLAYER, timeout: float = MOUTH_TIMEOUT) -> bool:
    """Start the stop child for FILE and return at once: its own process
    group, stdio on /dev/null, cwd `/`, this process's env (PATH, the mouth's
    variables, HOME). True when it started. Never raises."""
    try:
        if not mouth or not player:
            return False
        argv = [sys.executable, "-I", "-c", STOP_ENTRY, str(SRC), str(file),
                repr(float(timeout)), str(len(mouth)), *mouth, *player]
        subprocess.Popen(argv, start_new_session=True, close_fds=True, cwd="/",
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001 - a hook never breaks a turn
        return False
    return True


def _quiet_run(argv: Sequence[str], timeout: float | None = None) -> int:
    """Run ARGV with stdio on /dev/null: its exit status. Raises what
    `subprocess.run` raises (OSError, TimeoutExpired, after it killed it)."""
    return subprocess.run(list(argv), timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode


def stop_run(file: str | Path, mouth: Sequence[str] = MOUTH,
             player: Sequence[str] = PLAYER, timeout: float = MOUTH_TIMEOUT) -> str:
    """The stop child's work, in order. Which one played it: "mouth",
    "player", or "" (nothing: a mouth that timed out, or a player that could
    not run).

    1. MOUTH found on PATH: `MOUTH play FILE`, bounded by TIMEOUT. Exit 0:
       queued, done. Non-zero, or it cannot be run (OSError): fall back.
       Timed out (it is killed) or killed by a signal: nothing follows, as
       it may have queued.
    2. The fallback: `PLAYER FILE`, errors swallowed."""
    file = str(file)
    if mouth and shutil.which(mouth[0]):
        try:
            code = _quiet_run([*mouth, "play", file], timeout)
        except subprocess.TimeoutExpired:
            return ""
        except OSError:
            code = 1                     # it could not run: it queued nothing
        if code == 0:
            return "mouth"
        if code < 0:                     # a signal ended it, not a refusal
            return ""
    try:
        _quiet_run([*player, file])
    except Exception:  # noqa: BLE001
        return ""
    return "player"


def stop_main(argv: Sequence[str]) -> int:
    """`FILE TIMEOUT N MOUTH... PLAYER...`, as `stop_play` spawns it: N is
    how many words the mouth's argv has. Always 0."""
    try:
        file, timeout, n, *rest = argv
        count = int(n)
        mouth, player = tuple(rest[:count]), tuple(rest[count:])
        stop_run(file, mouth or MOUTH, player or PLAYER, float(timeout))
    except Exception:  # noqa: BLE001 - a detached child has no one to tell
        return 0
    return 0


def stop_sound(payload: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """Stop's sound: resolve the `stop` block (its OFF stamp kept, as the arm
    keeps it) and, when ON, start the stop child once. A subagent's Stop
    (`agent_id`) and a session with no dir play nothing and stamp nothing.
    Touches only `sounds/stop/`. True when the child started."""
    if payload.get("agent_id"):
        return False
    sid = _sid(payload, env)
    if sid is None or not slot_dir(sid, env, STOP).parent.parent.is_dir():
        return False
    file = _stamp_stop(sid, env, time.time())
    if file is None:
        return False
    return stop_play(file)


def stop_turn(payload: Mapping[str, Any], env: Mapping[str, str]) -> bool:
    """Stop: `clear` every slot first (unchanged), then the stop sound. True
    when the stop child started."""
    clear(payload, env)
    return stop_sound(payload, env)


def main(argv: Sequence[str] | None = None, stdin=None,
         env: Mapping[str, str] | None = None) -> int:
    """`busy-sound arm|end|stop|clear` with the hook payload on stdin. Always 0,
    never a byte on stdout or stderr: a hook never breaks a turn. So a
    stranded `sounds` tool is not said here (settings.quiet), as no hook
    says it."""
    args = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if env is None else env
    with settings.quiet():
        return _main(args, stdin, environ)


def _main(args: list[str], stdin, environ: Mapping[str, str]) -> int:
    try:
        if environ.get(SWITCH_ENV, "").strip().lower() in OFF_WORDS:
            return 0
        verb = args[0] if args else ""
        if verb not in VERBS:
            return 0
        raw = (sys.stdin if stdin is None else stdin).read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        {"arm": arm, "end": end, "stop": stop_turn, "clear": clear}[verb](payload, environ)
    except Exception:  # noqa: BLE001
        return 0
    return 0
