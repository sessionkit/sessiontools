"""ST-98: the turn-ended mark and the third state, `idle with children`.

Claude Code keeps its sidecar `busy` while a background agent runs after the
turn is over (SM-626 rca-001). The reader: a busy sidecar plus a valid mark
plus a live process reads `idle with children`, idle since the mark's `at`;
anything less is exactly today's reading. The writer: `hooks/busy-sound`,
run here exactly as `hooks/hooks.json` runs it, DEFAULT OFF behind
`SESSIONTOOLS_HOOK_TURN`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import ANCHOR_SUMMER

from sessiontools import claude_code as cc
from sessiontools import get as get_mod
from sessiontools import listing, procs, snapshot, tmux, turn
from sessiontools.procs import Proc

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "busy-sound"
HOOKS = ROOT / "hooks" / "hooks.json"

START = "Mon Dec  9 09:00:00 2030"
T0 = procs.parse_ctime(START)
NOW = T0 + 3600
SID = "16550ac1-0000-4000-8000-000000000001"
OTHER = "a907dd33-0000-4000-8000-000000000002"
PID = 4242
CWD = "/st98-fleet/seat"


# --- the reader --------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    """A sidecar folder, a sessions root, and a process table in which PID
    is live (started at START, as its sidecar says)."""
    cars = tmp_path / "sidecars"
    cars.mkdir()
    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setenv("SESSION_SIDECAR_DIR", str(cars))
    monkeypatch.setenv("SESSIONS_DIR", str(root))
    table = {PID: Proc(PID, 10, T0), 10: Proc(10, 1, T0), 1: Proc(1, 0, T0)}
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(tmux, "pane_index", lambda: {10: ("default", "%11")})
    listing.repo_name.cache_clear()
    return World(cars, root, table)


class World:
    def __init__(self, cars: Path, root: Path, table: dict):
        self.cars, self.root, self.table = cars, root, table

    def car(self, status: str = "busy", updated: float = NOW - 600, sid: str = SID,
            pid: int = PID, name: str = "foreman-98") -> Path:
        path = self.cars / f"{pid}.json"
        path.write_text(json.dumps({
            "pid": pid, "sessionId": sid, "status": status, "updatedAt": updated * 1000,
            "procStart": START, "startedAt": T0 * 1000, "cwd": CWD, "name": name,
            "agent": "foreman", "version": "2.1.289"}))
        (self.root / sid).mkdir(exist_ok=True)
        return path

    def mark(self, at: float = NOW - 30, sid: str = SID, **body) -> Path:
        path = self.root / sid / turn.MARK
        path.parent.mkdir(exist_ok=True)
        data = {"v": 1, "at": at, "harness": "claude-code", "event": "Stop", "background": 1}
        data.update(body)
        path.write_text(json.dumps(data) + "\n")
        return path

    def unmark(self, sid: str = SID) -> None:
        (self.root / sid / turn.MARK).unlink()


def today(s: cc.Session) -> None:
    """S reads exactly as it did before the mark existed: busy, idle 0."""
    assert s.busy is True
    assert s.state == cc.BUSY
    assert s.turn_ended_at is None
    assert s.idle_seconds(NOW) == 0.0


def test_busy_then_idle_with_children_then_busy_again(world):
    world.car("busy", updated=NOW - 600)
    today(cc.find(SID))                                   # mid-turn: busy

    world.mark(at=NOW - 30)                               # Stop: the turn is over
    s = cc.find(SID)
    assert s.state == cc.IDLE_WITH_CHILDREN == "idle with children"
    assert s.busy is False
    assert s.raw_status == "busy"                         # the harness's word is kept
    assert s.turn_ended_at == NOW - 30
    assert s.idle_seconds(NOW) == pytest.approx(30.0)     # since the turn ended
    assert cc.idle_seconds(SID, now=NOW) == pytest.approx(30.0)

    world.unmark()                                        # the next prompt clears it
    today(cc.find(SID))


def test_no_mark_is_todays_reading_exactly(world):
    world.car("busy", updated=NOW - 600)
    s = cc.find(SID)
    today(s)
    assert cc.idle_seconds(SID, now=NOW) == 0.0


def test_a_crashed_seat_keeps_todays_reading(world):
    """The mark is there but the sidecar's process is not: a crash after Stop
    left a mark nobody will clear."""
    world.car("busy", updated=NOW - 600)
    world.mark(at=NOW - 30)
    del world.table[PID]
    today(cc.find(SID))
    assert cc.live() == []


def test_a_reused_pid_is_not_the_seat(world):
    world.car("busy", updated=NOW - 600)
    world.mark(at=NOW - 30)
    world.table[PID] = Proc(PID, 10, T0 + 500)            # started long after the sidecar says
    today(cc.find(SID))


@pytest.mark.parametrize("text", [
    "{not json", "[1, 2]", '"a string"', "",
    json.dumps({"v": 2, "at": NOW - 30}),                 # a version we do not speak
    json.dumps({"v": "1", "at": NOW - 30}),
    json.dumps({"v": True, "at": NOW - 30}),
    json.dumps({"at": NOW - 30}),                         # no version
    json.dumps({"v": 1}),                                 # no at
    json.dumps({"v": 1, "at": "soon"}),
    json.dumps({"v": 1, "at": True}),
    '{"v": 1, "at": NaN}', '{"v": 1, "at": Infinity}',
])
def test_a_corrupt_or_foreign_mark_is_no_mark(world, text):
    world.car("busy", updated=NOW - 600)
    (world.root / SID / turn.MARK).write_text(text)
    today(cc.find(SID))


def test_a_mark_older_than_the_last_status_write_is_stale(world):
    """A clear that was lost: the next status change outranks the mark."""
    world.car("busy", updated=NOW - 10)
    world.mark(at=NOW - 30)
    today(cc.find(SID))


def test_a_mark_at_the_status_write_counts(world):
    world.car("busy", updated=NOW - 30)
    world.mark(at=NOW - 30)
    assert cc.find(SID).state == cc.IDLE_WITH_CHILDREN


@pytest.mark.parametrize("word", ["idle", "shell", "waiting"])
def test_a_sidecar_that_is_not_busy_is_idle_and_its_mark_is_never_read(world, word):
    world.car(word, updated=NOW - 75)
    world.mark(at=NOW - 30)
    s = cc.find(SID)
    assert (s.busy, s.state, s.turn_ended_at) == (False, cc.IDLE, None)
    assert s.idle_seconds(NOW) == pytest.approx(75.0)    # since the status, as today


def test_no_mark_never_reads_the_process_table(world, monkeypatch):
    """The table is read only for a busy sidecar with a mark that counts."""
    def refuse():
        raise AssertionError("ps was run")
    monkeypatch.setattr(procs, "table", refuse)
    world.car("busy", updated=NOW - 600)
    world.car("idle", updated=NOW - 60, sid=OTHER, pid=PID + 1)
    world.mark(at=NOW - 30, sid=OTHER)                   # on a sidecar that is not busy
    assert {s.id: s.state for s in cc.sessions()} == {SID: cc.BUSY, OTHER: cc.IDLE}


def test_the_mark_follows_sessions_dir(world, monkeypatch, tmp_path):
    world.car("busy", updated=NOW - 600)
    world.mark(at=NOW - 30)
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "elsewhere"))
    today(cc.find(SID))


def test_the_states_are_exported_in_our_words():
    import sessiontools
    assert sessiontools.STATES == ("busy", "idle with children", "idle")
    assert (sessiontools.BUSY, sessiontools.IDLE_WITH_CHILDREN, sessiontools.IDLE) == cc.STATES


# --- the surfaces --------------------------------------------------------------------

def test_session_list_says_the_word_in_the_table_and_the_json(world):
    world.car("busy", updated=NOW - 600)
    world.mark(at=NOW - 30)
    rows = listing.rows()
    row = json.loads(listing.render_json(rows, now=NOW))[0]
    assert (row["status"], row["state"], row["idle_s"]) == ("busy", "idle with children", 30)
    table = listing.render_table(rows, now=NOW)
    header, _rule, line = table.splitlines()
    assert "STATE" in header.split()
    assert "idle with children" in line
    assert line.split()[:3] == ["16550ac1", "foreman-98", "busy"]   # seat-hearing's columns

    world.unmark()
    row = json.loads(listing.render_json(listing.rows(), now=NOW))[0]
    assert (row["status"], row["state"], row["idle_s"]) == ("busy", "busy", 600)


def test_session_get_idle_and_state(world, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    world.car("busy", updated=NOW - 600)
    assert get_mod.get(listing.rows(), "idle", "foreman", False) == "0"
    assert get_mod.get(listing.rows(), "state", "foreman", False) == "busy"
    world.mark(at=NOW - 30)
    rows = listing.rows()
    assert get_mod.get(rows, "idle", "foreman", False) == "30"
    assert get_mod.get(rows, "state", "foreman", False) == "idle with children"
    assert json.loads(get_mod.get(rows, "idle", "foreman", True)) == {
        "session_id": SID, "name": "foreman-98", "idle": 30, "status": "busy",
        "state": "idle with children"}


def test_get_lists_the_state_property():
    assert "state" in dict(get_mod.PROPS)
    assert "idle with children" in get_mod.table()


# --- the 2 s bound: ST-111's snapshot is held to the marks ----------------------------

def _forever(monkeypatch):
    """A snapshot that would otherwise be served for an hour, so only the
    mark check can make it stale."""
    monkeypatch.setenv(snapshot.MAX_AGE_ENV, "3600")


def test_a_mark_set_after_the_snapshot_is_seen_at_once(world, monkeypatch):
    _forever(monkeypatch)
    world.car("busy", updated=NOW - 600)
    assert [r.session.state for r in snapshot.rows(False)] == [cc.BUSY]
    world.mark(at=NOW - 1)
    assert [r.session.state for r in snapshot.rows(False)] == [cc.IDLE_WITH_CHILDREN]


def test_a_mark_cleared_after_the_snapshot_is_seen_at_once(world, monkeypatch):
    _forever(monkeypatch)
    world.car("busy", updated=NOW - 600)
    world.mark(at=NOW - 1)
    assert [r.session.state for r in snapshot.rows(False)] == [cc.IDLE_WITH_CHILDREN]
    world.unmark()
    assert [r.session.state for r in snapshot.rows(False)] == [cc.BUSY]


def test_an_unchanged_mark_still_serves_the_snapshot(world, monkeypatch):
    _forever(monkeypatch)
    world.car("busy", updated=NOW - 600)
    world.mark(at=NOW - 1)
    calls = []

    def compute(want_context):
        calls.append(want_context)
        return listing.rows()
    snapshot.rows(False, compute=compute)
    snapshot.rows(False, compute=compute)
    assert len(calls) == 1


def test_the_snapshot_schema_was_bumped_for_the_new_field():
    assert snapshot.SCHEMA >= 2
    assert "SESSIONS_DIR" in snapshot.WORLD_VARS


# --- the writer, through the shim --------------------------------------------------------

class Hook:
    """A private HOME and sessions root with SID's dir, and a python3 on PATH
    that counts its starts."""

    def __init__(self, root: Path):
        self.root = root
        self.sessions = root / ".session" / "sessions"
        self.dir = self.sessions / SID
        self.dir.mkdir(parents=True)
        self.mark = self.dir / turn.MARK
        self.bin = root / "bin"
        self.bin.mkdir()
        self.pymark = root / "python-started"
        py = self.bin / "python3"
        py.write_text(f'#!/bin/sh\necho x >> "{self.pymark}"\nexec "{sys.executable}" "$@"\n')
        py.chmod(0o755)

    def env(self, **extra) -> dict:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root),
               "SESSIONS_DIR": str(self.sessions), "CLAUDE_CODE_SESSION_ID": SID,
               "SESSIONTOOLS_HOOK_TURN": "on", "SESSIONTOOLS_HOOK_BUSY": "off"}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def run(self, verb: str, event: str, payload: dict | None = None,
            **env) -> subprocess.CompletedProcess:
        body = {"session_id": SID, "hook_event_name": event, **(payload or {})}
        result = subprocess.run(["/bin/sh", str(SHIM), verb], input=json.dumps(body),
                                env=self.env(**env), capture_output=True, text=True,
                                timeout=30, check=False)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
        return result

    def python_runs(self) -> int:
        try:
            return len(self.pymark.read_text().split())
        except FileNotFoundError:
            return 0

    def body(self) -> dict:
        return json.loads(self.mark.read_text())


@pytest.fixture
def hook(tmp_path):
    return Hook(tmp_path)


def verb_of(event: str) -> str:
    """The verb hooks.json gives EVENT's busy-sound command."""
    hooks = json.loads(HOOKS.read_text())["hooks"]
    commands = [h["command"] for e in hooks[event] for h in e["hooks"] if "busy-sound" in h["command"]]
    assert len(commands) == 1, event
    return commands[0].split()[-1]


def test_stop_writes_the_mark_with_the_contract_body(hook):
    import time
    before = time.time()
    hook.run(verb_of("Stop"), "Stop", {"background_tasks": [
        {"id": "a1", "status": "running"}, {"id": "a2", "status": "completed"},
        {"id": "a3", "status": "running"}]})
    data = hook.body()
    assert set(data) == {"v", "at", "harness", "event", "background"}
    assert (data["v"], data["harness"], data["event"], data["background"]) == (
        1, "claude-code", "Stop", 2)
    assert isinstance(data["at"], float) and before <= data["at"] <= time.time()
    assert hook.mark.read_text().count("\n") == 1                  # one JSON line
    assert oct(hook.mark.stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in hook.dir.iterdir()) == [turn.MARK]   # no temp left behind


def test_stop_without_background_tasks_says_null(hook):
    hook.run("stop", "Stop")
    assert hook.body()["background"] is None


@pytest.mark.parametrize("event", ["PreToolUse", "UserPromptSubmit", "SessionEnd"])
def test_the_parents_next_event_clears_the_mark(hook, event):
    hook.run("stop", "Stop", {"background_tasks": []})
    assert hook.mark.exists()
    hook.run(verb_of(event), event, {"tool_name": "Bash"} if event == "PreToolUse" else {})
    assert not hook.mark.exists()


def test_post_tool_batch_leaves_the_mark(hook):
    hook.run("stop", "Stop")
    hook.run(verb_of("PostToolBatch"), "PostToolBatch")
    assert hook.mark.exists()


@pytest.mark.parametrize("event", ["PreToolUse", "UserPromptSubmit", "SessionEnd", "Stop"])
def test_a_subagents_event_changes_nothing(hook, event):
    agent = {"agent_id": "a0c2", "agent_type": "general-purpose"}
    hook.run(verb_of(event), event, agent)                   # sets nothing
    assert not hook.mark.exists()
    hook.run("stop", "Stop", {"background_tasks": [{"status": "running"}]})
    before = hook.mark.read_text()
    hook.run(verb_of(event), event, agent)                   # clears nothing, rewrites nothing
    assert hook.mark.read_text() == before


def test_a_parent_whose_tool_input_says_agent_id_still_clears(hook):
    hook.run("stop", "Stop")
    hook.run("arm", "PreToolUse", {"tool_name": "Bash",
                                   "tool_input": {"command": 'grep \'"agent_id"\' x.json'}})
    assert not hook.mark.exists()


@pytest.mark.parametrize("switch", [None, "", "off", "0", "no", "maybe"])
def test_switched_off_nothing_is_written_or_cleared(hook, switch):
    hook.run("stop", "Stop", SESSIONTOOLS_HOOK_TURN=switch)
    assert not hook.mark.exists()
    hook.mark.write_text('{"v": 1, "at": 1.0}\n')
    for verb, event in (("arm", "PreToolUse"), ("clear", "UserPromptSubmit"), ("clear", "SessionEnd")):
        hook.run(verb, event, SESSIONTOOLS_HOOK_TURN=switch)
    assert hook.mark.exists()
    assert hook.python_runs() == 0


@pytest.mark.parametrize("word", ["on", "ON", "1", "true", "yes"])
def test_the_switch_words(hook, word):
    hook.run("stop", "Stop", SESSIONTOOLS_HOOK_TURN=word)
    assert hook.mark.exists()


def test_the_mark_is_not_behind_the_sounds_switch(hook):
    """SESSIONTOOLS_HOOK_BUSY=off (the fixture's default) silences the sound,
    not the mark."""
    hook.run("stop", "Stop", SESSIONTOOLS_HOOK_BUSY="off")
    assert hook.mark.exists()


def test_with_the_sound_on_but_unset_the_mark_still_works(hook):
    hook.run("stop", "Stop", SESSIONTOOLS_HOOK_BUSY=None)
    assert hook.mark.exists()
    hook.run("clear", "UserPromptSubmit", SESSIONTOOLS_HOOK_BUSY=None)
    assert not hook.mark.exists()


def test_pre_tool_use_never_starts_python_for_the_parent(hook):
    """No mark: one stat. A mark and a parent's payload: rm, still no python."""
    for _ in range(3):
        hook.run("arm", "PreToolUse", {"tool_name": "Read"})
    assert hook.python_runs() == 0
    hook.run("stop", "Stop")
    assert hook.python_runs() == 1                           # Stop pays, once a turn
    hook.run("arm", "PreToolUse", {"tool_name": "Read"})
    assert not hook.mark.exists()
    assert hook.python_runs() == 1


def test_no_session_dir_writes_nothing(hook, tmp_path):
    hook.run("stop", "Stop", CLAUDE_CODE_SESSION_ID="ffffffff-no-dir")
    assert not (hook.sessions / "ffffffff-no-dir").exists()


@pytest.mark.parametrize("sid", ["", "..", "a/b"])
def test_a_bad_session_id_writes_nothing(hook, sid):
    hook.run("stop", "Stop", {"session_id": sid}, CLAUDE_CODE_SESSION_ID=sid or None)
    assert not hook.mark.exists()


def test_a_broken_payload_is_silent(hook):
    result = subprocess.run(["/bin/sh", str(SHIM), "stop"], input="{not json",
                            env=hook.env(), capture_output=True, text=True, timeout=30, check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not hook.mark.exists()


# --- the writer, in process ----------------------------------------------------------------

def test_set_is_atomic_a_reader_never_sees_half_a_mark(tmp_path):
    root = tmp_path / "sessions"
    (root / SID).mkdir(parents=True)
    env = {"SESSIONS_DIR": str(root), "CLAUDE_CODE_SESSION_ID": SID}
    path = turn.mark_path(SID, env)
    turn.set_mark({"background_tasks": [{"status": "running"}] * 50}, env, now=1.0)
    done = threading.Event()

    def writer():
        for i in range(300):
            turn.set_mark({"background_tasks": [{"status": "running"}] * (i % 50)}, env,
                          now=float(i + 2))
        done.set()
    t = threading.Thread(target=writer)
    t.start()
    seen = 0
    while not done.is_set():
        assert turn.read_at(path) is not None
        seen += 1
    t.join()
    assert seen > 0
    assert sorted(p.name for p in (root / SID).iterdir()) == [turn.MARK]


def test_main_is_silent_and_off_by_default(tmp_path):
    import io
    root = tmp_path / "sessions"
    (root / SID).mkdir(parents=True)
    env = {"SESSIONS_DIR": str(root), "CLAUDE_CODE_SESSION_ID": SID}
    assert turn.main(["set"], io.StringIO("{}"), env) == 0
    assert not turn.mark_path(SID, env).exists()
    on = {**env, turn.SWITCH_ENV: "on"}
    assert turn.main(["set"], io.StringIO("{}"), on) == 0
    assert turn.mark_path(SID, on).exists()
    assert turn.main(["bogus"], io.StringIO("{}"), on) == 0
    assert turn.main(["clear"], io.StringIO("[1]"), on) == 0       # not a dict: nothing
    assert turn.mark_path(SID, on).exists()
    assert turn.main(["clear"], io.StringIO(""), on) == 0          # empty payload: the parent
    assert not turn.mark_path(SID, on).exists()


def test_the_written_mark_is_the_one_the_reader_reads(tmp_path):
    root = tmp_path / "sessions"
    (root / SID).mkdir(parents=True)
    env = {"SESSIONS_DIR": str(root)}
    at = (ANCHOR_SUMMER + timedelta(hours=10, minutes=31, seconds=44.25)).timestamp()   # +217 weeks
    turn.set_mark({"session_id": SID}, env, now=at)
    assert turn.read_at(turn.mark_path(SID, env)) == at


@pytest.mark.parametrize("tasks, want", [
    (None, None), ("x", None), ([], 0), ([{"status": "running"}], 1),
    ([{"status": "completed"}, "junk", {"status": "running"}, {}], 1),
])
def test_background_counts_running_tasks(tasks, want):
    payload = {} if tasks is None else {"background_tasks": tasks}
    assert turn.background(payload) == want


def test_the_shim_parses():
    assert os.access(SHIM, os.X_OK)
    assert subprocess.run(["/bin/sh", "-n", str(SHIM)], check=False).returncode == 0


def test_hooks_json_documents_the_switch():
    description = json.loads(HOOKS.read_text())["description"]
    assert turn.SWITCH_ENV in description and "DEFAULT OFF" in description
    assert "turn-ended" in description
