"""A room's socket path and the platform's limit (ST-261, folded into ST-249
do-005).

A unix socket path holds at most 103 bytes on macOS (107 on Linux): sun_path
less its NUL. A long HOME put `~/.sessionkit/tools/web/run/talk.sock` past it
and `session web serve` died with a SocketRefused traceback. Now the path is
measured before any socket: at the limit it binds; one byte over is ONE line
naming the length, the limit and the fix (`SESSION_WEB_RUN_DIR`), exit 2, no
traceback. The 0600 socket and the 0700 same-uid directory hold either way.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest
from web_fakes import TALK, install_command, short_dir

from sessiontools import cli, settings
from sessiontools.web import rooms, server, unixhttp
from sessiontools.web.rooms import Discovery, Room
from sessiontools.web.unixhttp import QuietHandler, RoomServer, SocketRefused

LIMIT = unixhttp.SUN_PATH_LIMIT
SOCK = "talk.sock"
ROOM = Room(name="talk", title="Talk", package="sessionvoice",
            command=("sessionvoice-web", "--unix", "{socket}"))


def _padded(base: Path, total: int, tail: str) -> Path:
    """A directory under BASE such that `<it>/<tail>` is TOTAL bytes long."""
    room = total - len(os.fsencode(str(base))) - 1 - len(os.fsencode(tail)) - 1
    assert room > 0, "the base dir is already too long for this test"
    return base / ("d" * room)


@pytest.fixture
def base() -> Iterator[Path]:
    with short_dir("st-261-") as d:
        yield d


def test_the_limit_is_the_platforms_sun_path_less_its_nul():
    want = 107 if sys.platform.startswith("linux") else 103
    assert LIMIT == want == unixhttp.SUN_PATH_MAX - 1


# --- the limit itself, measured on a real bind -----------------------------------------

def test_a_socket_path_exactly_at_the_limit_binds_0600_in_a_0700_dir(base):
    run = _padded(base, LIMIT, SOCK)
    path = run / SOCK
    assert unixhttp.path_bytes(path) == LIMIT
    unixhttp.check_path(path)
    server.check_sockets(run, ["talk"])
    srv = RoomServer(path, QuietHandler)
    try:
        st = os.lstat(path)
        assert stat.S_ISSOCK(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o600
        assert st.st_uid == os.getuid()
        assert stat.S_IMODE(os.lstat(run).st_mode) == 0o700
        unixhttp.private_socket(path)  # the front door's own check passes too
    finally:
        srv.server_close()
    assert not path.exists()


def test_one_byte_over_the_limit_is_refused_before_any_socket(base, monkeypatch):
    run = _padded(base, LIMIT + 1, SOCK)
    path = run / SOCK
    assert unixhttp.path_bytes(path) == LIMIT + 1

    def refuse(*a, **k):
        raise AssertionError("a socket was made")
    monkeypatch.setattr(socket, "socket", refuse)
    with pytest.raises(SocketRefused, match=f"{LIMIT + 1} bytes.*at most {LIMIT} bytes"):
        unixhttp.check_path(path)
    with pytest.raises(SocketRefused, match="at most"):
        RoomServer(path, QuietHandler)
    assert not run.exists()  # refused before the directory was even made


def test_check_sockets_names_the_length_the_limit_and_the_fix_in_one_line(base):
    run = _padded(base, LIMIT + 1, SOCK)
    with pytest.raises(SocketRefused) as got:
        server.check_sockets(run, ["talk"])
    msg = str(got.value)
    assert "\n" not in msg
    assert f"{LIMIT + 1} bytes" in msg
    assert f"{LIMIT}-byte limit" in msg
    assert settings.WEB_RUN_DIR_ENV in msg
    assert str(run / SOCK) in msg


def test_the_length_is_bytes_not_characters(base):
    # "é" is one character and two bytes: a path of LIMIT characters is over.
    run = _padded(base, LIMIT - 1, SOCK)
    path = run.parent / ("é" + run.name[1:]) / SOCK
    assert len(str(path)) == LIMIT - 1 and unixhttp.path_bytes(path) == LIMIT
    unixhttp.check_path(path)
    longer = run.parent / ("é" + run.name) / SOCK
    assert len(str(longer)) == LIMIT and unixhttp.path_bytes(longer) == LIMIT + 1
    with pytest.raises(SocketRefused):
        unixhttp.check_path(longer)


# --- the run dir: HOME's, or SESSION_WEB_RUN_DIR -------------------------------------

def test_the_run_dir_is_under_the_web_dir_unless_the_variable_names_another(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert settings.web_run_dir(env) == settings.web_dir(env) / "run"
    env[settings.WEB_RUN_DIR_ENV] = "/tmp/kw"
    assert settings.web_run_dir(env) == Path("/tmp/kw")
    env[settings.WEB_RUN_DIR_ENV] = ""
    assert settings.web_run_dir(env) == settings.web_dir(env) / "run"


def test_a_relative_run_dir_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    got = settings.web_run_dir({"HOME": str(tmp_path), settings.WEB_RUN_DIR_ENV: "r"})
    assert got == Path(os.getcwd()) / "r"


# --- session web serve, in process --------------------------------------------------

@pytest.fixture
def one_room(monkeypatch):
    monkeypatch.setattr(rooms, "discover", lambda: Discovery(rooms={"talk": ROOM}))


def test_serve_one_byte_over_refuses_in_one_line_exit_2_no_socket(base, one_room,
                                                                     monkeypatch, capsys):
    run = _padded(base, LIMIT + 1, SOCK)
    monkeypatch.setenv(settings.WEB_RUN_DIR_ENV, str(run))
    made = []
    real = socket.socket

    def watch(*a, **k):
        made.append(a)
        return real(*a, **k)
    monkeypatch.setattr(socket, "socket", watch)
    assert cli.main(["web", "serve"]) == 2
    out = capsys.readouterr()
    lines = out.err.splitlines()
    assert len(lines) == 1, out.err
    assert lines[0].startswith("session web serve: room 'talk': its socket path ")
    assert f"{LIMIT + 1} bytes" in lines[0] and f"{LIMIT}-byte limit" in lines[0]
    assert settings.WEB_RUN_DIR_ENV in lines[0]
    assert "Traceback" not in out.err and out.out == ""
    assert made == []  # no TCP listener, no room socket
    assert not run.exists()


def test_serve_with_a_run_dir_that_is_not_private_refuses_in_one_line(base, one_room,
                                                                     monkeypatch, capsys):
    run = base / "r"
    run.mkdir(mode=0o755)
    os.chmod(run, 0o755)
    monkeypatch.setenv(settings.WEB_RUN_DIR_ENV, str(run))
    assert cli.main(["web", "serve"]) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "must be 0700" in err and "Traceback" not in err


class _Door:
    """A stand-in front door: records what serve gave it, serves nothing."""
    got: ClassVar[dict] = {}

    def __init__(self, address, **kw) -> None:
        _Door.got = {"address": address, **kw}
        self.front = type("F", (), {"port": 8750})()

    def serve_forever(self) -> None:
        return

    def server_close(self) -> None:
        return


def test_serve_exactly_at_the_limit_goes_on_to_the_front_door(base, one_room,
                                                               monkeypatch, capsys):
    run = _padded(base, LIMIT, SOCK)
    monkeypatch.setenv(settings.WEB_RUN_DIR_ENV, str(run))
    monkeypatch.setattr(server, "FrontDoor", _Door)
    monkeypatch.setattr(server.signal, "signal", lambda *a: None)
    assert cli.main(["web", "serve"]) == 0
    assert _Door.got["run_dir"] == run
    assert stat.S_IMODE(os.lstat(run).st_mode) == 0o700
    assert "the front door is at" in capsys.readouterr().out


def test_serve_with_no_rooms_checks_no_socket_path(base, monkeypatch, capsys):
    monkeypatch.setattr(rooms, "discover", lambda: Discovery(absent=list(rooms.CATALOG)))
    run = _padded(base, LIMIT + 1, SOCK)
    monkeypatch.setenv(settings.WEB_RUN_DIR_ENV, str(run))
    monkeypatch.setattr(server, "FrontDoor", _Door)
    monkeypatch.setattr(server.signal, "signal", lambda *a: None)
    assert cli.main(["web", "serve"]) == 0
    assert not run.exists()


# --- the bug as reported: a long HOME, the real CLI, a child process ------------------

def test_a_long_home_gives_one_line_and_exit_2_not_a_traceback(base, tmp_path):
    """EARS-448's case: HOME is long enough that `.sessionkit/tools/web/run/
    talk.sock` is one byte over. The real `session web serve`, in a child,
    with a fake Talk on PATH: exit 2, one stderr line, no traceback."""
    tail = str(Path(".sessionkit") / settings.PACKAGES["session"] / settings.WEB_DIR / "run"
               / SOCK)
    home = _padded(base, LIMIT + 1, tail)
    home.mkdir()
    bin_dir = tmp_path / "bin"
    install_command(bin_dir, descriptor=TALK)
    env = {k: v for k, v in os.environ.items() if k != settings.WEB_RUN_DIR_ENV}
    env.update(HOME=str(home), PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}")
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; from sessiontools import cli; sys.exit(cli.main(sys.argv[1:]))",
         "web", "serve", "--port", "1"],
        env=env, capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 2, r.stderr
    assert "Traceback" not in r.stderr
    lines = r.stderr.splitlines()
    assert len(lines) == 1, r.stderr
    sock = home / tail
    assert f"its socket path {sock} is {LIMIT + 1} bytes" in lines[0]
    assert f"{LIMIT}-byte limit" in lines[0] and settings.WEB_RUN_DIR_ENV in lines[0]
    assert not sock.parent.exists()


# --- a rescan after start: one line on stderr, never a handler traceback ---------------

def test_a_refused_room_set_at_reload_runs_no_supervisor_and_says_why_once(base, capsys):
    from sessiontools.web import auth
    store = auth.Store(base / "web")
    store.ensure()
    rooms.save(store.directory, Discovery(rooms={"talk": ROOM}))
    run = _padded(base, LIMIT + 1, SOCK)
    door = server.FrontDoor(("127.0.0.1", 0), store=store, run_dir=run)
    try:
        found, sup = door.rooms_now()
        assert "talk" in found.rooms and sup is None
        door.reload_rooms()  # unchanged rooms.json: not re-read, not said again
        err = capsys.readouterr().err
        assert len(err.splitlines()) == 1, err
        assert err.startswith("session web: the rooms were not started: ")
        assert f"{LIMIT + 1} bytes" in err and "Traceback" not in err
    finally:
        door.server_close()
