"""The Claude Code driver against sidecars shaped like the real ones.

The specimen fields are those a real `~/.claude/sessions/<pid>.json`
carried on a real host on 2030-06-09 (version 2.1.273): sessionId, pid,
cwd, name, agent, status, updatedAt (epoch ms).
"""

from __future__ import annotations

import json
import time

import pytest

from sessiontools import claude_code as cc


@pytest.fixture
def sidecars(tmp_path, monkeypatch):
    monkeypatch.setenv(cc.SIDECAR_DIR_ENV, str(tmp_path))

    def write(pid: int, session_id: str, status: str, updated_ms: float, **extra):
        data = {"pid": pid, "sessionId": session_id, "status": status,
                "updatedAt": updated_ms, "cwd": "/Users/x/.bob", "name": "bob-89",
                "agent": "bob", **extra}
        (tmp_path / f"{pid}.json").write_text(json.dumps(data))
        return tmp_path / f"{pid}.json"

    return write


NOW = 1_907_215_200.0   # 2030-06-09T06:00:00Z, as an epoch


def test_busy_is_zero_whatever_the_clock_says(sidecars):
    sidecars(1, "sess-a", "busy", (NOW - 600) * 1000)
    assert cc.idle_seconds("sess-a", now=NOW) == 0.0


@pytest.mark.parametrize("word", ["shell", "idle", "waiting"])
def test_any_other_word_is_seconds_since_the_status_changed(sidecars, word):
    sidecars(1, "sess-a", word, (NOW - 75) * 1000)
    assert cc.idle_seconds("sess-a", now=NOW) == pytest.approx(75.0)


def test_no_sidecar_is_unknown_not_zero(sidecars):
    assert cc.idle_seconds("nobody", now=NOW) is None
    assert cc.find("nobody") is None


def test_the_harness_words_stay_in_the_driver(sidecars):
    """Nothing above the driver sees `shell` or `waiting`: a Session says
    busy or not, and keeps the raw word only for the curious."""
    sidecars(1, "sess-a", "waiting", NOW * 1000)
    s = cc.find("sess-a")
    assert s is not None
    assert s.busy is False
    assert s.raw_status == "waiting"
    assert s.harness == "claude-code"
    assert s.pid == 1 and s.agent == "bob" and s.name == "bob-89"


def test_a_malformed_sidecar_is_skipped_and_the_rest_are_read(sidecars, tmp_path):
    (tmp_path / "7.json").write_text("{not json")
    (tmp_path / "8.json").write_text(json.dumps(["a", "list"]))
    (tmp_path / "9.json").write_text(json.dumps({"sessionId": "s9"}))   # no status
    sidecars(1, "sess-a", "busy", NOW * 1000)
    assert [s.id for s in cc.sessions()] == ["sess-a"]


def test_sessions_are_newest_status_first(sidecars):
    sidecars(1, "old", "idle", (NOW - 900) * 1000)
    sidecars(2, "new", "idle", (NOW - 5) * 1000)
    assert [s.id for s in cc.sessions()] == ["new", "old"]


def test_a_missing_directory_is_no_sessions_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv(cc.SIDECAR_DIR_ENV, str(tmp_path / "absent"))
    assert cc.sessions() == []
    assert cc.idle_seconds("anything") is None


def test_the_default_directory_is_claude_codes_own(monkeypatch):
    monkeypatch.delenv(cc.SIDECAR_DIR_ENV, raising=False)
    assert cc.sidecar_dir().parts[-2:] == (".claude", "sessions")


def test_the_clock_defaults_to_now(sidecars):
    sidecars(1, "sess-a", "idle", (time.time() - 30) * 1000)
    secs = cc.idle_seconds("sess-a")
    assert secs is not None and 29 <= secs <= 35
