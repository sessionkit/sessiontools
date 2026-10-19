"""`tmux.run`: the package's one tmux call - a named server, or the ambient one.
And `sockets()`: only the servers that answer, knocked for in-process."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess

import pytest

from sessiontools import tmux


def test_run_names_the_server_only_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw))
                        or subprocess.CompletedProcess(cmd, 0, "", ""))
    tmux.run(["list-panes"], "work", timeout=5)
    tmux.run(["rename-window", "-t", "%1", "bob"], env={"A": "1"})
    assert calls[0][0] == [tmux.TMUX_BIN, "-L", "work", "list-panes"]
    assert calls[0][1]["timeout"] == 5
    assert calls[1][0] == [tmux.TMUX_BIN, "rename-window", "-t", "%1", "bob"]   # ambient
    assert calls[1][1]["env"] == {"A": "1"} and calls[1][1]["check"] is False


# --- sockets(): a socket FILE is not a server (ST-111, ST-82) ----------------

def _socket_dir():
    base = os.path.join(os.environ["TMUX_TMPDIR"], f"tmux-{os.getuid()}")
    os.makedirs(base, mode=0o700, exist_ok=True)
    return base


def _plant_dead(base, count):
    """COUNT socket files no server answers: bound, then closed."""
    for i in range(count):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(os.path.join(base, f"dead-{i:03d}"))
        s.close()


def _listening(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(4)
    return s


def _count_tmux(monkeypatch):
    forks = []
    real = subprocess.run

    def counting(command, *a, **k):
        if os.path.basename(command[0]) == "tmux":
            forks.append(list(command))
        return real(command, *a, **k)

    monkeypatch.setattr(subprocess, "run", counting)
    return forks


def test_sockets_skips_dead_files_and_keeps_live_ones_default_first():
    base = _socket_dir()
    _plant_dead(base, 5)
    with open(os.path.join(base, "not-a-socket"), "w"):
        pass
    held = [_listening(os.path.join(base, n)) for n in ("zeta", "default", "alpha")]
    try:
        assert tmux.sockets() == ["default", "alpha", "zeta"]
    finally:
        for s in held:
            s.close()


def test_sockets_with_only_dead_files_is_empty_not_default():
    _plant_dead(_socket_dir(), 3)
    assert tmux.sockets() == []


def test_sockets_unreadable_folder_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "nowhere"))
    assert tmux.sockets() == ["default"]


def test_answers_counts_an_unprovable_failure_as_live(monkeypatch):
    class Hung:
        def __init__(self, *a):
            pass

        def settimeout(self, _):
            pass

        def connect(self, _):
            raise TimeoutError("timed out")

        def close(self):
            pass

    monkeypatch.setattr(tmux._socket, "socket", Hung)
    assert tmux.answers("/whatever") is True


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_dead_sockets_fork_no_tmux_beside_a_live_scratch_server(monkeypatch):
    """N dead files plus one real scratch server in the walled TMUX_TMPDIR:
    sockets() names only the server and forks nothing; the pane index forks
    tmux exactly once, for the live server."""
    base = _socket_dir()
    _plant_dead(base, 40)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
    env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]
    subprocess.run(["tmux", "-L", "st111-scratch", "new-session", "-d", "sleep 60"],
                   env=env, check=True, capture_output=True)
    forks = _count_tmux(monkeypatch)
    assert tmux.sockets() == ["st111-scratch"]
    assert forks == []
    index = tmux.pane_index()
    assert len(forks) == 1 and forks[0][1:3] == ["-L", "st111-scratch"]
    assert len(index) == 1 and all(where[0] == "st111-scratch" for where in index.values())
