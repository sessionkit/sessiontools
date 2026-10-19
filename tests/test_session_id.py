"""The one session-id resolver: SESSION_ID_VARS, session_id(), current().

By design, ONE place gets the session id, and it is here. The environment only, never the disk; the first non-blank
variable wins; `current()` is `find(session_id())`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ANCHOR

import sessiontools
from sessiontools import claude_code as cc

FIRST, SECOND = "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"


@pytest.fixture
def clean_env(monkeypatch):
    """Neither variable inherited from the shell running the suite -
    this suite itself runs inside a Claude Code session."""
    for var in (FIRST, SECOND):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ── SESSION_ID_VARS ──────────────────────────────────────────────────────

def test_the_variables_are_public_and_in_the_ruled_order():
    assert cc.SESSION_ID_VARS == (FIRST, SECOND)
    assert isinstance(cc.SESSION_ID_VARS, tuple)


def test_the_three_names_are_exported_short_noun_style():
    for name in ("SESSION_ID_VARS", "session_id", "current"):
        assert name in sessiontools.__all__
    assert sessiontools.SESSION_ID_VARS is cc.SESSION_ID_VARS
    assert sessiontools.session_id is cc.session_id
    assert sessiontools.current is cc.current


# ── session_id(): the order ──────────────────────────────────────────────

def test_the_first_wins_when_both_are_set():
    assert cc.session_id({FIRST: "aaa", SECOND: "bbb"}) == "aaa"


def test_the_second_wins_when_the_first_is_unset():
    assert cc.session_id({SECOND: "bbb"}) == "bbb"


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "  \t\n "])
def test_the_second_wins_when_the_first_is_blank(blank):
    assert cc.session_id({FIRST: blank, SECOND: "bbb"}) == "bbb"


def test_neither_set_is_none():
    assert cc.session_id({}) is None
    assert cc.session_id({"SESSION_ID": "x", "CODEX_SESSION_ID": "y"}) is None


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_both_blank_is_none_not_an_empty_string(blank):
    assert cc.session_id({FIRST: blank, SECOND: blank}) is None


# ── session_id(): the value ──────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["  aaa", "aaa  ", "\taaa\n", " aaa \n"])
def test_the_value_is_stripped(raw):
    """The strip sessionmail's `_seat.session_id` keeps today
    (sessionmail 0c010ed, src/sessionmail/_seat.py:930)."""
    assert cc.session_id({FIRST: raw}) == "aaa"
    assert cc.session_id({SECOND: raw}) == "aaa"


def test_env_none_means_os_environ(clean_env):
    assert cc.session_id() is None
    clean_env.setenv(SECOND, "from-second")
    assert cc.session_id() == "from-second"
    clean_env.setenv(FIRST, " from-first ")
    assert cc.session_id() == "from-first"


def test_an_explicit_env_is_the_only_env_read(clean_env):
    clean_env.setenv(FIRST, "from-os-environ")
    assert cc.session_id({}) is None
    assert cc.session_id({SECOND: "from-dict"}) == "from-dict"


# ── session_id(): never the disk ─────────────────────────────────────────

def test_session_id_never_touches_the_disk(monkeypatch, tmp_path):
    """The pager needs the id before Claude Code writes the sidecar. With
    no sidecar directory at all, and every filesystem read wired to blow
    up, the id still comes back."""
    def boom(*_a, **_k):
        raise AssertionError("session_id() read the disk")

    env = {cc.SIDECAR_DIR_ENV: str(tmp_path / "absent"), FIRST: "sess-a"}
    # The patches are scoped to the one call: pytest's own failure report
    # reads the disk through Path, and a patch still live when it runs
    # turns an honest assertion failure into an INTERNALERROR.
    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", boom)
        m.setattr(Path, "glob", boom)
        m.setattr(Path, "exists", boom)
        m.setattr(cc, "sessions", boom)
        m.setattr(cc, "find", boom)
        got = cc.session_id(env)
    assert got == "sess-a"


# ── current() ────────────────────────────────────────────────────────────

NOW_MS = int(ANCHOR.timestamp() * 1000)   # 2030-06-09T22:34+10:00, epoch ms


def _sidecar(directory: Path, pid: int, sid: str, status: str = "busy") -> Path:
    data = {"pid": pid, "sessionId": sid, "status": status,
            "updatedAt": NOW_MS, "cwd": "/Users/x/proj", "name": "worker",
            "agent": "worker"}
    path = directory / f"{pid}.json"
    path.write_text(json.dumps(data))
    return path


def test_current_is_the_whole_session_from_its_sidecar(tmp_path):
    _sidecar(tmp_path, 41, "sess-other", status="idle")
    path = _sidecar(tmp_path, 42, "sess-me")
    env = {cc.SIDECAR_DIR_ENV: str(tmp_path), FIRST: "sess-me"}
    s = cc.current(env)
    assert isinstance(s, cc.Session)
    assert s == cc.find("sess-me", env)
    assert (s.id, s.pid, s.busy, s.path) == ("sess-me", 42, True, path)
    assert s.cwd == "/Users/x/proj" and s.name == "worker"


def test_current_follows_the_same_order_as_session_id(tmp_path):
    _sidecar(tmp_path, 1, "sess-first")
    _sidecar(tmp_path, 2, "sess-second")
    env = {cc.SIDECAR_DIR_ENV: str(tmp_path)}
    assert cc.current({**env, FIRST: "sess-first", SECOND: "sess-second"}).id == "sess-first"
    assert cc.current({**env, FIRST: " ", SECOND: " sess-second "}).id == "sess-second"


def test_current_env_none_means_os_environ(clean_env, tmp_path):
    _sidecar(tmp_path, 7, "sess-env")
    clean_env.setenv(cc.SIDECAR_DIR_ENV, str(tmp_path))
    assert cc.current() is None
    clean_env.setenv(SECOND, "sess-env")
    s = cc.current()
    assert s is not None and s.id == "sess-env"


def test_current_is_none_with_no_id(tmp_path):
    _sidecar(tmp_path, 1, "sess-a")
    assert cc.current({cc.SIDECAR_DIR_ENV: str(tmp_path)}) is None


def test_current_is_none_with_an_id_but_no_sidecar(tmp_path):
    _sidecar(tmp_path, 1, "sess-someone-else")
    assert cc.current({cc.SIDECAR_DIR_ENV: str(tmp_path), FIRST: "sess-me"}) is None


def test_current_is_none_when_the_sidecar_directory_is_missing(tmp_path):
    env = {cc.SIDECAR_DIR_ENV: str(tmp_path / "absent"), FIRST: "sess-me"}
    assert cc.current(env) is None


def test_current_writes_nothing(tmp_path):
    path = _sidecar(tmp_path, 42, "sess-me")
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(tmp_path.iterdir()))
    cc.current({cc.SIDECAR_DIR_ENV: str(tmp_path), FIRST: "sess-me"})
    assert (path.read_bytes(), path.stat().st_mtime_ns, sorted(tmp_path.iterdir())) == before
