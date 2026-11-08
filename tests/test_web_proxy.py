"""`sessiontools.web.unixhttp` and `sessiontools.web.proxy` (kit-web-shell
design §2 and §6): a room listens on a unix socket 0600 in a 0700 dir and
answers only its own uid; the proxy strips and sets the identity headers,
drops a room's `Set-Cookie: kit_session`, strips the prefix, and streams
unbuffered. Every server here is a unix socket in a tmp dir, served on a
thread and shut down in the test: no TCP, no child process."""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from web_fakes import short_dir

from sessiontools.web import proxy, unixhttp
from sessiontools.web.unixhttp import SocketRefused, UnixHTTPConnection

# --- a room and a front door, both on unix sockets ------------------------------------


class EchoRoom(unixhttp.QuietHandler):
    """Answers with what it was sent; `/cookie` sets two cookies; `/events`
    writes one event, waits for the test, then writes a second."""

    protocol_version = "HTTP/1.1"

    def _json(self, data: dict, extra: list[tuple[str, str]] = ()) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/events":
            gate: threading.Event = self.server.second_event
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()  # no Content-Length: the stream is close-delimited
            self.wfile.write(b"data: one\n\n")
            self.wfile.flush()
            self.server.first_written.set()
            if gate.wait(10):
                self.server.second_written_after_first_read = True
                self.wfile.write(b"data: two\n\n")
                self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/cookie":
            return self._json({}, [("Set-Cookie", "kit_session=evil; Path=/"),
                                   ("Set-Cookie", "Kit_Session=evil2"),
                                   ("Set-Cookie", "room=1; Path=/talk")])
        self._json({"path": self.path, "headers": [[k, v] for k, v in self.headers.items()]})

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        self._json({"path": self.path, "body": self.rfile.read(n).decode()})


class FrontDoor(unixhttp.QuietHandler):
    """The test's front door: every request goes straight to the proxy (the
    gate is do-001's and tested there)."""

    protocol_version = "HTTP/1.1"

    def _go(self) -> None:
        proxy.forward(self, self.server.room_socket, prefix="/talk", user="alice", timeout=10)

    do_GET = do_POST = do_HEAD = _go


@contextmanager
def running(path: Path, handler) -> Iterator[unixhttp.RoomServer]:
    srv = unixhttp.serve(path, handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


@pytest.fixture
def web() -> Iterator[dict]:
    """A room at <tmp>/rooms/talk.sock and a front door at <tmp>/front/web.sock."""
    with short_dir() as d:
        room_sock, front_sock = d / "rooms" / "talk.sock", d / "front" / "web.sock"
        with running(room_sock, EchoRoom) as room:
            room.second_event = threading.Event()
            room.first_written = threading.Event()
            room.second_written_after_first_read = False
            with running(front_sock, FrontDoor) as front:
                front.room_socket = room_sock
                try:
                    yield {"dir": d, "room": room, "front": front, "room_sock": room_sock,
                           "front_sock": front_sock}
                finally:
                    room.second_event.set()


def _get(sock: Path, path: str, headers: dict | list | None = None, method: str = "GET",
         body: bytes | None = None):
    c = UnixHTTPConnection(sock, host="127.0.0.1:8750", timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    c.putheader("Host", "127.0.0.1:8750")
    for k, v in (headers.items() if isinstance(headers, dict) else headers or []):
        c.putheader(k, v)
    if body is not None:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    return c, r


# --- the room's socket: 0600, 0700 dir, own uid only, never TCP --------------------------

def test_a_room_listens_on_a_0600_unix_socket_in_a_0700_dir_never_tcp():
    with short_dir() as d, running(d / "r" / "talk.sock", EchoRoom) as srv:
        assert srv.socket.family == socket.AF_UNIX
        assert stat.S_IMODE(os.stat(d / "r" / "talk.sock").st_mode) == 0o600
        assert stat.S_IMODE(os.stat(d / "r").st_mode) == 0o700
        unixhttp.private_socket(d / "r" / "talk.sock")  # the front door's check passes
    assert not (d / "r" / "talk.sock").exists()  # closed, and the socket removed


def test_the_peer_uid_is_read_from_the_kernel():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with a, b:
        assert unixhttp.peer_uid(a) == os.getuid()


def test_a_peer_of_another_uid_is_refused():
    with short_dir() as d, running(d / "r" / "talk.sock", EchoRoom) as srv:
        srv.allow_uid = os.getuid() + 1  # as if the caller were another user
        c = UnixHTTPConnection(d / "r" / "talk.sock", timeout=5)
        with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError)):
            c.request("GET", "/")
            c.getresponse()
        c.close()


def test_a_room_will_not_bind_in_a_too_open_dir():
    with short_dir() as d:
        (d / "open").mkdir(mode=0o755)
        os.chmod(d / "open", 0o755)
        with pytest.raises(SocketRefused, match="0700"):
            unixhttp.serve(d / "open" / "talk.sock", EchoRoom)


def test_a_socket_path_too_long_for_sun_path_is_refused():
    with pytest.raises(SocketRefused, match="at most"):
        unixhttp.check_path(Path("/tmp/" + "x" * 120 + ".sock"))


def test_the_front_door_refuses_a_socket_that_is_not_private():
    with short_dir() as d, running(d / "r" / "talk.sock", EchoRoom):
        os.chmod(d / "r" / "talk.sock", 0o666)
        with pytest.raises(SocketRefused, match="0600"):
            unixhttp.private_socket(d / "r" / "talk.sock")
        os.chmod(d / "r" / "talk.sock", 0o600)
        os.chmod(d / "r", 0o755)
        with pytest.raises(SocketRefused, match="0700 directory"):
            unixhttp.private_socket(d / "r" / "talk.sock")
        os.chmod(d / "r", 0o700)
    with short_dir() as d:
        (d / "plain").write_text("")
        with pytest.raises(SocketRefused, match="not a socket"):
            unixhttp.private_socket(d / "plain")


# --- the pure parts of the proxy -------------------------------------------------------

@pytest.mark.parametrize(("path", "target"), [
    ("/talk/", "/"), ("/talk/events", "/events"), ("/talk/x?y=1", "/x?y=1"),
])
def test_the_prefix_is_stripped(path, target):
    assert proxy.room_target(path, "/talk") == target


@pytest.mark.parametrize("path", ["/talk", "/talkx/", "/settings/", "/"])
def test_a_path_outside_the_prefix_is_not_the_rooms(path):
    with pytest.raises(ValueError):
        proxy.room_target(path, "/talk")


def test_only_kit_session_is_taken_from_the_cookie():
    assert proxy.strip_session_cookie("a=1; kit_session=S; b=2; kit_session=T") == "a=1; b=2"
    assert proxy.strip_session_cookie("kit_session=S") == ""
    assert proxy.strip_session_cookie("kit_sessionx=1") == "kit_sessionx=1"


def test_request_headers_strip_identity_and_hop_by_hop_then_set_the_front_doors():
    got = proxy.request_headers(
        [("Host", "127.0.0.1:8750"), ("X-Sessionkit-User", "mallory"),
         ("x-sessionkit-prefix", "/evil"), ("X-SessionKit-Anything", "1"),
         ("Cookie", "a=1; kit_session=SECRET"), ("Connection", "keep-alive, X-Drop-Me"),
         ("X-Drop-Me", "1"), ("Keep-Alive", "5"), ("Content-Length", "99"), ("Accept", "*/*")],
        user="alice", prefix="/talk")
    assert got == [("Host", "127.0.0.1:8750"), ("Cookie", "a=1"), ("Accept", "*/*"),
                   ("X-Sessionkit-User", "alice"), ("X-Sessionkit-Prefix", "/talk")]


@pytest.mark.parametrize("name", ["X_Sessionkit_User", "x_sessionkit-user", "X-Sessionkit_Prefix"])
def test_an_identity_header_spelled_with_underscores_is_dropped(name):
    """WSGI and CGI fold `_` and `-` into one HTTP_X_SESSIONKIT_USER, so a
    room behind either would read the forged one."""
    got = proxy.request_headers([(name, "mallory")], user="alice", prefix="/talk")
    assert got == [("X-Sessionkit-User", "alice"), ("X-Sessionkit-Prefix", "/talk")]


def test_response_headers_drop_a_rooms_kit_session_cookie_and_hop_by_hop():
    got = proxy.response_headers([("Set-Cookie", "kit_session=x; Path=/"),
                                  ("set-cookie", " KIT_SESSION=y"), ("Set-Cookie", "room=1"),
                                  ("Transfer-Encoding", "chunked"), ("Content-Type", "text/html")])
    assert got == [("Set-Cookie", "room=1"), ("Content-Type", "text/html")]


# --- the proxy, end to end over two unix sockets ------------------------------------------

def test_forged_identity_headers_are_dropped_and_the_front_doors_set(web):
    c, r = _get(web["front_sock"], "/talk/echo?q=1",
                [("X-Sessionkit-User", "mallory"), ("X-Sessionkit-Prefix", "/x"),
                 ("Cookie", "a=1; kit_session=SECRET; b=2"), ("X-Custom", "kept")])
    seen = json.loads(r.read())
    c.close()
    assert r.status == 200 and seen["path"] == "/echo?q=1"
    h = {}
    for k, v in seen["headers"]:
        h.setdefault(k.lower(), []).append(v)
    assert h["x-sessionkit-user"] == ["alice"] and h["x-sessionkit-prefix"] == ["/talk"]
    assert h["cookie"] == ["a=1; b=2"] and h["x-custom"] == ["kept"]
    assert h["host"] == ["127.0.0.1:8750"]
    assert "SECRET" not in json.dumps(seen) and "mallory" not in json.dumps(seen)


def test_a_rooms_set_cookie_kit_session_is_dropped(web):
    c, r = _get(web["front_sock"], "/talk/cookie")
    r.read()
    cookies = r.headers.get_all("Set-Cookie")
    c.close()
    assert cookies == ["room=1; Path=/talk"]


def test_a_post_body_is_forwarded(web):
    c, r = _get(web["front_sock"], "/talk/send", method="POST", body=b"hello")
    got = json.loads(r.read())
    c.close()
    assert got == {"path": "/send", "body": "hello"}


def test_a_stream_passes_through_unbuffered(web):
    """The first event reaches the client before the room writes the second."""
    c, r = _get(web["front_sock"], "/talk/events")
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
    first = r.read1(64)
    assert first == b"data: one\n\n"
    assert not web["room"].second_written_after_first_read
    web["room"].second_event.set()
    rest = r.read()
    c.close()
    assert rest == b"data: two\n\n" and web["room"].second_written_after_first_read


def test_a_room_that_is_not_running_is_a_502(web):
    web["front"].room_socket = web["dir"] / "rooms" / "gone.sock"
    c, r = _get(web["front_sock"], "/talk/")
    body = r.read().decode()
    c.close()
    assert r.status == 502 and "talk: not running" in body


def test_a_room_whose_socket_is_not_private_is_not_dialled(web):
    os.chmod(web["room_sock"], 0o666)
    c, r = _get(web["front_sock"], "/talk/")
    body = r.read().decode()
    c.close()
    assert r.status == 502 and "refused" in body


def test_a_path_outside_the_prefix_is_a_404_and_websocket_a_501(web):
    c, r = _get(web["front_sock"], "/talkx/")
    r.read()
    c.close()
    assert r.status == 404
    c, r = _get(web["front_sock"], "/talk/ws", {"Upgrade": "websocket", "Connection": "Upgrade"})
    r.read()
    c.close()
    assert r.status == 501
