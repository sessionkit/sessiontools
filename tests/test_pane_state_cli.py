"""`session pane-state show|watch` (ST-134 do-003, design.md §5 verbs).

No tmux and no `ps` run here: `tmux.run` (the package's ONLY tmux call) and
`procs.table` are doubles, so a write would be seen, not made. The sidecar
folder and the state dir are conftest's scratch ones; SESSIONTOOLS_HOOK_TURN
is scrubbed. tests/test_pane_state_tmux.py runs the verb on a private server.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from sessiontools import claude_code as cc
from sessiontools import cli, pane_state, procs, tmux, turn
from sessiontools.procs import Proc

ROOT = Path(__file__).resolve().parents[1]
START = "Sun Dec  8 12:34:56 2030"
T0 = procs.parse_ctime(START)
PY = "/opt/homebrew/bin/python3"


@pytest.fixture(autouse=True)
def _hook_off(monkeypatch):
    monkeypatch.delenv(turn.SWITCH_ENV, raising=False)


class FakeHost:
    """Two seats in panes on server "s" (one with a test running, one with
    only its pager monitor), and a seat in no pane. Every tmux argv is kept."""

    def __init__(self, monkeypatch):
        self.calls: list[list[str]] = []
        self.held = {"%2": "FREE"}
        self.table: dict[int, Proc] = {1: Proc(1, 0, T0, "/sbin/launchd")}
        self.panes: dict[int, str] = {}
        self.busy = self.seat(101, "%1", work="make test")
        self.free = self.seat(201, "%2")
        self.loose = self.seat(301, None)
        monkeypatch.setattr(procs, "table", lambda commands=False: dict(self.table))
        monkeypatch.setattr(tmux, "sockets", lambda: ["s"])
        monkeypatch.setattr(tmux, "run", self.run)

    def seat(self, pid: int, pane: str | None, work: str | None = None) -> int:
        self.table[pid] = Proc(pid, 1, T0, f"claude --session-id {pid}")
        self.table[pid + 1] = Proc(pid + 1, pid, T0 + 60, f"{PY} /x/pager listen --monitor")
        if work:
            self.table[pid + 2] = Proc(pid + 2, pid, T0 + 60,
                                       f"/bin/bash -c source /x/s.sh && eval '{work}' < /dev/null")
            self.table[pid + 3] = Proc(pid + 3, pid + 2, T0 + 60, work)
        if pane:
            self.panes[pid] = pane
        data = {"pid": pid, "sessionId": f"0000{pid:04d}-0000-4000-8000-000000000134",
                "status": "shell", "updatedAt": (T0 + 120) * 1000, "procStart": START}
        (cc.sidecar_dir() / f"{pid}.json").write_text(json.dumps(data))
        return pid

    def run(self, arguments, socket=None, env=None, timeout=None):
        self.calls.append(list(arguments))
        fmt = arguments[-1] if arguments[:2] == ["list-panes", "-a"] else ""
        if fmt == "#{pane_pid} #{pane_id}":
            out = "".join(f"{pid} {pane}\n" for pid, pane in self.panes.items())
        elif fmt == "#{pane_id}\t#{@session_state}":
            out = "".join(f"{pane}\t{self.held.get(pane, '')}\n" for pane in self.panes.values())
        else:
            pytest.fail(f"show ran a tmux command that is not a read: {arguments}")
        return subprocess.CompletedProcess(arguments, 0, out, "")


def _sidecar_stamps() -> dict[str, tuple[int, int]]:
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in cc.sidecar_dir().iterdir()}


# --- show ---------------------------------------------------------------------------

def test_show_json_reads_every_seat_and_writes_nothing(monkeypatch, capsys):
    host = FakeHost(monkeypatch)
    before = _sidecar_stamps()
    assert cli.main(["pane-state", "show", "--json"]) == 0
    rows = {r["pid"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows[host.busy] == {"pid": host.busy, "session_id": "00000101-0000-4000-8000-000000000134",
                               "socket": "s", "pane": "%1", "state": "BUSY",
                               "why": "work: make test", "published": None}
    assert (rows[host.free]["state"], rows[host.free]["why"], rows[host.free]["published"]) \
        == ("FREE", "nothing live", "FREE")
    assert (rows[host.loose]["pane"], rows[host.loose]["state"]) == (None, "FREE")
    # reads only: the index and the option, one each, and no set-option anywhere
    assert [c[0] for c in host.calls] == ["list-panes", "list-panes"]
    assert not any("set-option" in c for call in host.calls for c in call)
    assert not pane_state.lock_path().parent.exists()          # no lock, no state file
    assert _sidecar_stamps() == before


def test_show_table_names_the_pane_the_word_and_what_the_pane_holds(monkeypatch, capsys):
    FakeHost(monkeypatch)
    assert cli.main(["pane-state", "show"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["PANE", "STATE", "PUBLISHED", "PID", "WHY"]
    assert lines[1].split()[:4] == ["s:%1", "BUSY", "-", "101"]
    assert lines[2].split()[:4] == ["s:%2", "FREE", "FREE", "201"]
    assert lines[3].split()[:4] == ["-", "FREE", "-", "301"]      # in no pane: last


def test_show_sorts_panes_by_number():
    rows = [pane_state.Row(1, "a", "s", "%10", "FREE", "x", None),
            pane_state.Row(2, "b", "s", "%9", "FREE", "x", None),
            pane_state.Row(3, "c", None, None, "FREE", "x", None)]
    assert [r.pane for r in sorted(rows, key=pane_state._row_order)] == ["%9", "%10", None]


def test_show_with_no_process_table_reads_unknown_never_free(monkeypatch, capsys):
    host = FakeHost(monkeypatch)
    host.table.clear()                                           # ps could not run
    assert cli.main(["pane-state", "show", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["state"] for r in rows} == {"UNKNOWN"}


# --- watch's surface ------------------------------------------------------------------

def test_the_verb_is_listed_and_still_one_console_script(capsys):
    assert cli.main([]) == 0
    assert "  pane-state " in capsys.readouterr().out
    assert cli.HANDLERS["pane-state"] is cli._pane_state


def test_watch_help_says_once_leaves_its_writes_standing(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["pane-state", "watch", "--help"])
    assert e.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    for flag in ("--interval S", "--once", "--dry-run", "--stats"):
        assert flag in out
    assert "STAY STANDING" in out and "take no lock" in out


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "soon"])
def test_watch_refuses_an_interval_that_is_not_a_positive_number(value, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["pane-state", "watch", "--interval", value])
    assert e.value.code == 2
    assert "--interval" in capsys.readouterr().err


def test_no_subverb_prints_the_help_and_exits_2(capsys):
    assert cli.main(["pane-state"]) == 2
    err = capsys.readouterr().err
    assert "show|watch" in err and "#{@session_state}" in err


def test_watch_passes_its_flags_to_the_publisher(monkeypatch):
    seen = {}
    monkeypatch.setattr(pane_state, "watch", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["pane-state", "watch", "--interval", "0.5", "--once", "--dry-run",
                     "--stats"]) == 0
    assert seen == {"period": 0.5, "once": True, "dry_run": True, "stats": True}
    assert cli.main(["pane-state", "watch"]) == 0
    assert seen == {"period": None, "once": False, "dry_run": False, "stats": False}


def test_watch_dry_run_once_on_the_cli_writes_nothing(monkeypatch, capsys):
    host = FakeHost(monkeypatch)
    assert cli.main(["pane-state", "watch", "--once", "--dry-run", "--stats"]) == 0
    out = capsys.readouterr().out
    assert "s %1 unset -> BUSY (work: make test)" in out
    assert "s %2 FREE -> FREE (nothing live)" in out           # found at start, re-stated
    assert out.splitlines()[-1].startswith("ticks=1 ps=1 ")
    assert not any("set-option" in c for call in host.calls for c in call)
    assert not pane_state.lock_path().parent.exists()


# --- no spawn per redraw: nothing shipped asks tmux to run a command ----------------

def test_no_format_sessiontools_ships_holds_a_command_substitution():
    """`#(...)` in a tmux format is a spawn per status interval (design.md §5),
    the one thing the signal exists to avoid. Nothing under src/ holds one."""
    offenders = []
    for path in sorted((ROOT / "src").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if "#(" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    assert os.path.isdir(ROOT / "src" / "sessiontools")             # the scan saw the package
