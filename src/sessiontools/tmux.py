"""Which tmux pane a process runs in, across every tmux server on the host.

A pane id is per SERVER, not per machine: `%4` on the default server and
`%4` on `tm-store-work` were live at the same moment (measured 2030-07-10).
So a pane is only an address with its server, and `locate` returns both.
The socket DIRECTORY is the registry: tmux names each file after the `-L`
name. Default first, because most seats live there.

A socket FILE is not a server: a server that died leaves its file behind,
and a host collects them (1034 files, 6 live servers on one host,
2030-08-11 - one `session list` forked tmux 1034 times, about 6 s). So
`sockets()` knocks first, in-process: a plain connect() that is refused is
a dead file, skipped with no tmux fork. That is tmux's own rule - its
client reads ECONNREFUSED as "no server" too.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket as _socket
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path

from sessiontools.procs import Proc, ancestors

TMUX_BIN = shutil.which("tmux") or "tmux"

#: A connect() that fails with one of these proves no server is there.
NO_SERVER = frozenset({errno.ECONNREFUSED, errno.ENOENT, errno.ENOTSOCK})
#: A live server answers a local connect at once; this only bounds a wedged one.
KNOCK_SECONDS = 0.5


def run(arguments: list[str], socket: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    """One tmux call, the package's only one. SOCKET names a server (`-L`);
    None is the AMBIENT server ($TMUX, else default) - what a process in a
    pane wants when it acts on its own pane. A refusal is the returncode;
    a missing tmux raises OSError, a hung one TimeoutExpired."""
    command = [TMUX_BIN, *(["-L", socket] if socket else []), *arguments]
    return subprocess.run(command, capture_output=True, text=True, env=env,
                          timeout=timeout, check=False)


#: Separates the commands of one tmux call: an argument that is exactly `;`.
SEPARATOR = ";"


def set_options(socket: str, items: list[tuple[str, str, str | None]],
                timeout: float = 5) -> bool:
    """Set or unset pane user options on SOCKET in ONE tmux call:
    each (pane, key, value) is `set-option -p -t PANE KEY VALUE`, or with
    value None `set-option -p -u -t PANE KEY`, joined by `;`. True when
    tmux took every command. tmux stops a batch at its first failure (a
    pane that is gone), so False means "some or none were applied" and
    the caller decides what to retry. A value's trailing `;` would be read
    by tmux as a separator (measured, tmux 3.8), so it is dropped."""
    if not items:
        return True
    arguments: list[str] = []
    for pane, key, value in items:
        if arguments:
            arguments.append(SEPARATOR)
        if value is None:
            arguments += ["set-option", "-p", "-u", "-t", pane, key]
        else:
            arguments += ["set-option", "-p", "-t", pane, key, value.rstrip(";")]
    try:
        return run(arguments, socket, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def answers(path: Path) -> bool:
    """False only when the socket at PATH provably has no server: the
    connect is refused, or finds no socket. Anything else - a timeout, a
    path too long to connect to, a permission - is not proof, so it counts
    as live and `panes()` asks tmux, as before. In-process: no fork."""
    knock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    knock.settimeout(KNOCK_SECONDS)
    try:
        knock.connect(str(path))
    except OSError as e:
        return e.errno not in NO_SERVER
    finally:
        knock.close()
    return True


def sockets() -> list[str]:
    """Every LIVE tmux server's `-L` name, "default" first. A socket file
    no server answers is skipped without forking tmux; a folder that cannot
    be read is ["default"], the one server worth one try."""
    base = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    try:
        files = sorted(base.iterdir())
    except OSError:
        return ["default"]
    names = [p.name for p in files if _is_socket(p) and answers(p)]
    return sorted(names, key=lambda n: n != "default")


def _is_socket(path: Path) -> bool:
    """A file gone between the listing and the stat is no server: tmux's
    own client unlinks a dead socket it finds, so this race is real."""
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def panes(socket: str) -> dict[int, str]:
    """pane_pid -> pane_id on one server; empty when it does not answer (a
    stale socket file is existence, not liveness)."""
    try:
        out = run(["list-panes", "-a", "-F", "#{pane_pid} #{pane_id}"], socket, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    found: dict[int, str] = {}
    for line in out.stdout.splitlines():
        pid, _, pane = line.partition(" ")
        if pid.isdigit() and pane:
            found[int(pid)] = pane
    return found


def pane_option(socket: str, key: str) -> dict[str, str] | None:
    """pane_id -> KEY's value, for every pane on SOCKET where it is set and
    not empty; None when the server does not answer."""
    try:
        out = run(["list-panes", "-a", "-F", "#{pane_id}\t#{" + key + "}"], socket, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    found: dict[str, str] = {}
    for line in out.stdout.splitlines():
        pane, _, value = line.partition("\t")
        if pane and value:
            found[pane] = value
    return found


def pane_pid(socket: str, pane: str) -> int | None:
    """The pid of PANE's CURRENT occupant on SOCKET; None when it is gone."""
    try:
        out = run(["display-message", "-p", "-t", pane, "#{pane_pid}"], socket, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return int(text) if out.returncode == 0 and text.isdigit() else None


def pane_index() -> dict[int, tuple[str, str]]:
    """pane_pid -> (socket, pane_id) over every server; the default server
    wins a clash because it is read first."""
    index: dict[int, tuple[str, str]] = {}
    for socket in sockets():
        for pid, pane in panes(socket).items():
            index.setdefault(pid, (socket, pane))
    return index


def locate(pid: int | None, procs: dict[int, Proc],
           index: dict[int, tuple[str, str]]) -> tuple[str, str] | None:
    """(socket, pane_id) of the pane PID runs in: the first ancestor that is
    a pane's own process. None outside tmux."""
    for p in ancestors(pid, procs):
        if p in index:
            return index[p]
    return None
