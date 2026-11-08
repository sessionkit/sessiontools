"""The rooms: which packages add pages to the front door, found two ways and
checked by one validator.

PROPOSED (kit-web-shell design §4; spec "Rooms are discovered from one
descriptor, two transports"). Each package that has a room ships ONE
function, `room() -> dict`:

    {"name": "talk", "title": "Talk", "package": "sessionvoice",
     "command": [sys.executable, "-m", "talk", "serve", "--kit", "--unix", "{socket}"]}

The front door reaches it by one of two transports:

1. SAME VENV: the `sessionkit.web` entry-point group in the front door's
   own environment (`uv tool install sessionkit`). Each entry is loaded and
   called; no command is run.
2. SEPARATE VENVS: for each CATALOG room that transport 1 did not find,
   `shutil.which` the package's command (Talk's is `sessionvoice-web`,
   never `talk`: macOS ships `/usr/bin/talk`), then `<cmd> web-room` (2 s
   timeout), which prints `json.dumps(room())`, the same function. Its
   `sys.executable` names its own venv, so the command it returns runs
   there.

Both hand the same dict to ONE `validate_room`. A catalog room that neither
finds is ABSENT, and the menu shows its hint (*"install sessionvoice to get
Talk"*). Discovery runs at front-door start and on `session web rescan`,
never per request; `rescan` records what it found in `rooms.json` in the
web dir.

Stdlib only.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

#: The entry-point group a package's `room()` is declared in.
GROUP = "sessionkit.web"
#: The argument that asks a package's command for its descriptor.
WEB_ROOM_ARG = "web-room"
#: How long `<cmd> web-room` may take.
WEB_ROOM_TIMEOUT = 2.0
#: The placeholder in a room's command that the front door fills with the socket path.
SOCKET = "{socket}"
#: The file `session web rescan` writes in the web dir.
ROOMS_FILE = "rooms.json"

#: Names the front door keeps for itself.
RESERVED = ("settings", "login", "static")
_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
#: The keys a descriptor may carry. Any other key is refused: a room is
#: reached by its socket only, so a host, port, address or URL has no place.
KEYS = ("name", "title", "package", "command")
#: Command arguments that would give a room a TCP listener.
_TCP_ARG = re.compile(r"--(host|port|bind|listen|address|addr)(=.*)?")


class RoomRefused(ValueError):
    """A descriptor that `validate_room` refuses. The message says why."""


@dataclass(frozen=True)
class CatalogEntry:
    """A room the kit knows: its name, title, the package that provides it,
    and that package's command on a separate-venv host (`<package>-web`,
    the family's name for the command that serves a package's page)."""

    name: str
    title: str
    package: str
    command: str

    @property
    def hint(self) -> str:
        return f"install {self.package} to get {self.title}"


#: The rooms the kit knows, in menu order.
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(name="talk", title="Talk", package="sessionvoice", command="sessionvoice-web"),
)


@dataclass(frozen=True)
class Room:
    """A validated room: what the supervisor starts and the proxy reaches.
    TRANSPORT says how it was found (`entry-point` or `command`), and SOURCE
    names the entry point or the command's path."""

    name: str
    title: str
    package: str
    command: tuple[str, ...]
    transport: str = ""
    source: str = ""

    def argv(self, socket_path: Path | str) -> list[str]:
        """The command with `{socket}` filled in."""
        return [a.replace(SOCKET, str(socket_path)) for a in self.command]


def validate_room(data: Any, *, transport: str = "", source: str = "") -> Room:
    """DATA as a Room, or RoomRefused. The ONE check both transports pass:
    a dict of exactly the known keys; a name of `[a-z][a-z0-9-]*`, not one
    the front door keeps; a title and package; a command that is a
    non-empty list of strings, takes `{socket}`, and asks for no TCP
    listener."""
    if not isinstance(data, dict):
        raise RoomRefused(f"a room descriptor is a JSON object, not {type(data).__name__}")
    extra = sorted(set(data) - set(KEYS))
    if extra:
        raise RoomRefused(f"unknown key(s) {', '.join(map(repr, extra))}: a room is reached "
                          "by its unix socket only")
    name = data.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise RoomRefused(f"room name {name!r}: must match [a-z][a-z0-9-]*")
    if name in RESERVED:
        raise RoomRefused(f"room name {name!r} is the front door's own")
    for key in ("title", "package"):
        v = data.get(key)
        if not isinstance(v, str) or not v.strip() or len(v) > 64:
            raise RoomRefused(f"room {name!r}: {key} must be a short string")
    command = data.get("command")
    if (not isinstance(command, list) or not command
            or not all(isinstance(a, str) and a for a in command)):
        raise RoomRefused(f"room {name!r}: command must be a non-empty list of strings")
    if not any(SOCKET in a for a in command):
        raise RoomRefused(f"room {name!r}: command must take {SOCKET}, the unix socket it "
                          "listens on")
    for a in command:
        if _TCP_ARG.fullmatch(a):
            raise RoomRefused(f"room {name!r}: command argument {a!r} asks for a TCP "
                              "listener; a room listens on its unix socket only")
    return Room(name=name, title=data["title"].strip(), package=data["package"].strip(),
                command=tuple(command), transport=transport, source=source)


@dataclass
class Discovery:
    """What one discovery found: the ROOMS by name, in catalog order then
    the rest; the catalog entries that are ABSENT; and the REFUSED
    descriptors, each with its reason."""

    rooms: dict[str, Room] = field(default_factory=dict)
    absent: list[CatalogEntry] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"rooms": [{**asdict(r), "command": list(r.command)} for r in self.rooms.values()],
                "absent": [e.name for e in self.absent],
                "refused": [{"source": s, "reason": why} for s, why in self.refused]}


def _entry_points() -> Iterable[metadata.EntryPoint]:
    return metadata.entry_points(group=GROUP)


def _ask_command(path: str, run: Callable[..., subprocess.CompletedProcess]) -> Any:
    """`PATH web-room`'s descriptor, parsed; RoomRefused if it fails."""
    try:
        r = run([path, WEB_ROOM_ARG], capture_output=True, text=True,
                timeout=WEB_ROOM_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RoomRefused(f"`{path} {WEB_ROOM_ARG}` took over {WEB_ROOM_TIMEOUT:g} s") from None
    except OSError as e:
        raise RoomRefused(f"`{path} {WEB_ROOM_ARG}` could not run ({e.strerror})") from None
    if r.returncode != 0:
        raise RoomRefused(f"`{path} {WEB_ROOM_ARG}` exited {r.returncode}")
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise RoomRefused(f"`{path} {WEB_ROOM_ARG}` printed no JSON descriptor") from None


def discover(*, catalog: tuple[CatalogEntry, ...] = CATALOG,
             entry_points: Callable[[], Iterable[Any]] | None = None,
             which: Callable[[str], str | None] = shutil.which,
             run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Discovery:
    """Find the rooms: the entry points first, then, for each catalog room
    they did not provide, its package's command on PATH."""
    found = Discovery()
    by_ep: dict[str, Room] = {}
    for ep in (entry_points or _entry_points)():
        source = f"{ep.name} = {ep.value}"
        try:
            room = validate_room(ep.load()(), transport="entry-point", source=source)
        except RoomRefused as e:
            found.refused.append((source, str(e)))
            continue
        except Exception as e:  # noqa: BLE001 - a package's own code: any failure is its refusal
            found.refused.append((source, f"loading it failed ({type(e).__name__})"))
            continue
        if room.name in by_ep:
            found.refused.append((source, f"a second room named {room.name!r}"))
            continue
        by_ep[room.name] = room
    for entry in catalog:
        if entry.name in by_ep:
            found.rooms[entry.name] = by_ep.pop(entry.name)
            continue
        path = which(entry.command)
        if path is None:
            found.absent.append(entry)
            continue
        source = f"{path} {WEB_ROOM_ARG}"
        try:
            room = validate_room(_ask_command(path, run), transport="command", source=source)
            if room.name != entry.name:
                raise RoomRefused(f"it describes {room.name!r}, not {entry.name!r}")
        except RoomRefused as e:
            found.refused.append((source, str(e)))
            found.absent.append(entry)
            continue
        found.rooms[entry.name] = room
    for name in sorted(by_ep):  # rooms from packages the catalog does not know (Q2)
        found.rooms[name] = by_ep[name]
    return found


def save(directory: Path, found: Discovery) -> Path:
    """Record FOUND in the web dir's `rooms.json` (0600), for the front door."""
    from sessiontools.web import auth
    store = auth.Store(directory)
    store.write_json(ROOMS_FILE, found.to_json())
    return Path(directory) / ROOMS_FILE


def load(directory: Path, *, catalog: tuple[CatalogEntry, ...] = CATALOG) -> Discovery | None:
    """`rooms.json` as a Discovery, each room re-validated; None when no
    rescan has written one."""
    from sessiontools.web import auth
    data = auth.Store(directory).read_json(ROOMS_FILE)
    if data is None:
        return None
    found = Discovery()
    for row in data.get("rooms", []):
        if not isinstance(row, dict):
            continue
        transport, source = row.get("transport", ""), row.get("source", "")
        desc = {k: row.get(k) for k in KEYS}
        try:
            room = validate_room(desc, transport=str(transport), source=str(source))
        except RoomRefused as e:
            found.refused.append((str(source), str(e)))
            continue
        found.rooms[room.name] = room
    known = {e.name: e for e in catalog}
    found.absent = [known[n] for n in known if n not in found.rooms]
    return found
