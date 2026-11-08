"""HTTP over a unix socket, both ends: what a room listens on, and what the
front door dials.

PROPOSED (kit-web-shell design §2: "The auth boundary: how a room trusts the
front door"). A room listens on a unix socket ONLY, mode 0600, in a 0700
directory of its own user, with no TCP port. Every connection's peer uid
must be the room's own uid (macOS `LOCAL_PEERCRED`, Linux `SO_PEERCRED`);
the front door runs as that uid. A browser cannot dial a unix socket, so no
page can reach a room around the login.

- `peer_uid(conn)`: the uid at the other end, or None when the kernel will
  not say (and then the connection is refused).
- `private_socket(path)`: the front door's check before it dials: a socket
  of this user's, 0600 at most, in a 0700 directory of this user's.
- `RoomServer` / `serve(path, handler)`: a room's listener. A kit package
  may use it; the fake rooms in the tests do.
- `UnixHTTPConnection`: `http.client` over a unix socket.

Stdlib only.
"""

from __future__ import annotations

import http.client
import os
import socket
import socketserver
import stat
import struct
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

#: macOS sys/un.h: getsockopt(SOL_LOCAL=0, LOCAL_PEERCRED) gives a struct xucred.
LOCAL_PEERCRED = 0x001
#: The head of struct xucred: cr_version, cr_uid.
XUCRED = struct.Struct("Ii")
#: sun_path's size, the NUL included: 108 bytes on Linux, 104 on macOS (and
#: the BSDs, and anything else: the smaller is the safe guess).
SUN_PATH_MAX = 108 if sys.platform.startswith("linux") else 104
#: The longest socket path that binds here, in bytes: sun_path less its NUL
#: (103 on macOS, 107 on Linux). A path this long binds; one byte more is
#: refused (`check_path`) before any socket is made.
SUN_PATH_LIMIT = SUN_PATH_MAX - 1


class SocketRefused(ValueError):
    """A socket path, or the directory it is in, is refused. The message names it."""


def peer_uid(conn: socket.socket) -> int | None:
    """The uid at the other end of a unix socket connection, or None if the
    kernel will not say."""
    try:
        if sys.platform == "darwin":
            raw = conn.getsockopt(0, LOCAL_PEERCRED, 256)
            if len(raw) < XUCRED.size:
                return None
            version, uid = XUCRED.unpack_from(raw)
            return uid if version == 0 else None
        if hasattr(socket, "SO_PEERCRED"):
            size = struct.calcsize("3i")
            _pid, uid, _gid = struct.unpack(
                "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size))
            return uid
    except (OSError, struct.error):
        return None
    return None


def private_dir(path: Path) -> None:
    """PATH is a directory of this user's, 0700. One that is missing is
    made 0700; a symlink, another user's or a too-open one is refused."""
    path = Path(path)
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise SocketRefused(f"{path}: not a directory; refusing it")
    if st.st_uid != os.getuid():
        raise SocketRefused(f"{path}: owned by uid {st.st_uid}, not this user; refusing it")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise SocketRefused(f"{path}: mode {stat.S_IMODE(st.st_mode):04o}; a socket directory "
                            "must be 0700")


def path_bytes(path: Path) -> int:
    """PATH's length as the kernel measures it: bytes, not characters."""
    return len(os.fsencode(str(path)))


def check_path(path: Path) -> None:
    """A socket path short enough for sun_path, or SocketRefused."""
    n = path_bytes(path)
    if n > SUN_PATH_LIMIT:
        raise SocketRefused(f"{path}: {n} bytes; a unix socket path holds at most "
                            f"{SUN_PATH_LIMIT} bytes here")


def private_socket(path: Path) -> None:
    """Before the front door dials PATH: a socket owned by this user, no
    wider than 0600, in a 0700 directory of this user's. Anything else is
    refused (SocketRefused); a missing one raises FileNotFoundError."""
    path = Path(path)
    st = os.lstat(path)
    if not stat.S_ISSOCK(st.st_mode):
        raise SocketRefused(f"{path}: not a socket; refusing it")
    if st.st_uid != os.getuid():
        raise SocketRefused(f"{path}: owned by uid {st.st_uid}, not this user; refusing it")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SocketRefused(f"{path}: mode {stat.S_IMODE(st.st_mode):04o}; a room socket "
                            "must be 0600")
    d = os.lstat(path.parent)
    if not stat.S_ISDIR(d.st_mode) or d.st_uid != os.getuid() or stat.S_IMODE(d.st_mode) != 0o700:
        raise SocketRefused(f"{path.parent}: not a 0700 directory of this user's; refusing "
                            "the socket in it")


class RoomServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """An HTTP server on a unix socket: bound 0600 (umask 0o177, so there is
    no moment it is wider), in a 0700 directory, and answering only a peer
    whose uid is this process's own. No TCP port anywhere."""

    daemon_threads = True
    allow_uid: int

    def __init__(self, path: Path, handler: type[BaseHTTPRequestHandler]) -> None:
        path = Path(path)
        check_path(path)
        private_dir(path.parent)
        with_stale = path.exists() or path.is_symlink()
        if with_stale:
            st = os.lstat(path)
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
                raise SocketRefused(f"{path}: exists and is not a socket of this user's")
            os.unlink(path)
        self.allow_uid = os.getuid()
        super().__init__(str(path), handler)

    def server_bind(self) -> None:
        old = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(old)
        st = os.lstat(self.server_address)
        self._socket_id = (st.st_dev, st.st_ino)

    def verify_request(self, request, client_address) -> bool:
        return peer_uid(request) == self.allow_uid

    def server_close(self) -> None:
        super().server_close()
        try:
            st = os.lstat(self.server_address)
            if (st.st_dev, st.st_ino) == getattr(self, "_socket_id", None):
                os.unlink(self.server_address)
        except OSError:
            pass


class QuietHandler(BaseHTTPRequestHandler):
    """A handler for a unix socket: its client address is empty, so the
    default access log (which indexes it) is replaced by nothing."""

    def address_string(self) -> str:
        return "unix"

    def log_message(self, format: str, *args) -> None:
        return


def serve(path: Path, handler: type[BaseHTTPRequestHandler]) -> RoomServer:
    """A room's listener at PATH, bound but not yet serving."""
    return RoomServer(path, handler)


class UnixHTTPConnection(http.client.HTTPConnection):
    """`http.client` to a unix socket. HOST is only what goes in the Host
    header; the connection is to PATH."""

    def __init__(self, path: Path, host: str = "localhost", timeout: float | None = 30) -> None:
        super().__init__(host, timeout=timeout)
        self.unix_path = str(path)

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if self.timeout is not None:
                s.settimeout(self.timeout)
            s.connect(self.unix_path)
        except BaseException:
            s.close()
            raise
        self.sock = s
