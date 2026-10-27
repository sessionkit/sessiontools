"""The sound engine's interface: one OSC datagram, fire and forget.

A hook says the MOMENT; the engine (`engine.swift`, one resident process)
owns the delay, the speech guard, the stop and the sound. The wire:

    /sound/event ,ssd  EVENT SID T     T = the sender's wall clock, Unix seconds

OSC 1.0: big-endian, strings NUL-terminated and padded to 4 bytes. A busy
event with a full uuid sid is 76 bytes. It goes from an UNCONNECTED UDP
socket to 127.0.0.1:PORT (a connected one surfaces ECONNREFUSED on the next
call, as a spike measured). No reply, no retry: every error is ignored, so an engine
that is off or dead is silent by construction.

Two senders speak it, and the tests hold them to the same bytes:

- `send()` here: python's client. The busy sound's first arm of a session
  uses it (`sessiontools.busy`), after it writes the engine's record.
- `event.swift`, the `sound-event` binary: every later hook, with no python
  (the hook's latency budget).

Stdlib only: `import sessiontools` stays dependency-free.
"""

from __future__ import annotations

import contextlib
import os
import socket
import struct
import time
from collections.abc import Mapping
from pathlib import Path

#: The engine's UDP port on 127.0.0.1: below the macOS
#: ephemeral range (49152-65535), unassigned, clear of :8890, :2022 and :8880.
#: The ONE constant: `engine.swift` and `event.swift` carry the same number,
#: and a test pins that all three agree.
PORT = 44100
#: Tests only: moves the port everywhere it is read (python, the sender).
PORT_ENV = "SESSIONTOOLS_SOUND_PORT"
#: Where the binaries live, per user: `<home>/bin/sound-engine`, `sound-event`.
HOME_ENV = "SESSIONTOOLS_SOUND_HOME"
HOST = "127.0.0.1"
ADDRESS = "/sound/event"
#: The busy sound's events: the hook shim's four verbs, verbatim. Reserved
#: for the batch trigger; later senders use names of their own.
EVENTS = ("arm", "end", "stop", "clear")
SENDER = "sound-event"


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def port(env: Mapping[str, str] | None = None) -> int:
    """PORT, or `$SESSIONTOOLS_SOUND_PORT` when it is a port (1-65535)."""
    raw = _env(env).get(PORT_ENV, "").strip()
    if raw.isdigit() and 0 < int(raw) <= 65535:
        return int(raw)
    return PORT


def home(env: Mapping[str, str] | None = None) -> Path:
    """The sound leaf, `settings.sound_root`: `$SESSIONTOOLS_SOUND_HOME`,
    else the old `~/.local/share/sessiontools/sound/` while it is real, else
    `~/.sessionkit/tools/state/sound/` (settings.leaf_path). The hook shim
    (hooks/busy-sound) carries the same rule in bash."""
    from sessiontools import settings  # here: settings imports this module
    return settings.sound_root(_env(env))


def sender(env: Mapping[str, str] | None = None) -> Path:
    """The hook's sender binary, `<home>/bin/sound-event`."""
    return home(env) / "bin" / SENDER


def installed(env: Mapping[str, str] | None = None) -> bool:
    """The engine counts as installed when its sender is executable. Not
    installed counts as off: the shim sends nothing then, and neither does
    python, so a seat never half-uses the engine."""
    return os.access(sender(env), os.X_OK)


def _osc_string(text: str) -> bytes:
    raw = text.encode("utf-8") + b"\0"
    return raw + b"\0" * (-len(raw) % 4)


def encode(event: str, sid: str, t: float | None = None) -> bytes:
    """One `/sound/event` message: `,ssd EVENT SID T`, or `,ss` when T is None
    (the engine then stands its arrival time in for T)."""
    if t is None:
        return _osc_string(ADDRESS) + _osc_string(",ss") + _osc_string(event) + _osc_string(sid)
    return (_osc_string(ADDRESS) + _osc_string(",ssd") + _osc_string(event)
            + _osc_string(sid) + struct.pack(">d", float(t)))


def send(event: str, sid: str, t: float | None = None, *,
         env: Mapping[str, str] | None = None, to_port: int | None = None) -> bool:
    """Send EVENT for session SID to the engine, once, and return at once. T
    defaults to now (the wall clock). True when the datagram left; False on
    any error. Never raises, never waits, never reads a reply."""
    try:
        data = encode(event, sid, time.time() if t is None else t)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except Exception:  # noqa: BLE001 - a sender never breaks its caller
        return False
    try:
        sock.sendto(data, (HOST, port(env) if to_port is None else to_port))
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    return True
