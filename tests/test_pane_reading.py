"""The pane signal's reading (ST-134 design.md §3): BUSY, FREE or UNKNOWN.

Every table here is synthetic. The command lines are pinned to shapes
measured on one Mac (Claude Code 2.1.296) on 2030-12-08, on the seat
that wrote this test, and in findings/r02:

- a Bash tool call: `/opt/homebrew/bin/bash -c source …/shell-snapshots/… &&
  … && eval '<command>' < /dev/null && pwd -P >| /tmp/claude-…-cwd`, a
  DIRECT child of the claude pid;
- the pager monitor: `…/Python.app/Contents/MacOS/Python
  /Users/alice/.local/bin/pager listen --monitor`, also a direct child, not
  under a shell;
- `caffeinate -i -t 300`, a direct child of claude (design F5).

SESSIONTOOLS_HOOK_TURN is scrubbed for every test: the reading must not need it.
"""

from __future__ import annotations

import json

import pytest

from sessiontools import claude_code as cc
from sessiontools import procs, turn
from sessiontools.claude_code import PANE_BUSY, PANE_FREE, PANE_UNKNOWN, Reading
from sessiontools.procs import Proc

START = "Sun Dec  8 01:00:00 2030"
T0 = procs.parse_ctime(START)
NOW = T0 + 3600
SID = "f1340000-0000-4000-8000-000000000134"
CLAUDE = 5000

PY = "/opt/homebrew/Cellar/python@3.14/3.14.8_2/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
MONITOR = f"{PY} /Users/alice/.local/bin/pager listen --monitor"
CAFFEINATE = "caffeinate -i -t 300"


def tool_shell(command: str) -> str:
    """A Bash tool call as Claude Code runs it (measured shape)."""
    return ("/opt/homebrew/bin/bash -c source /Users/alice/.claude/shell-snapshots/"
            "snapshot-bash-1907254800000-zh6e7c.sh 2>/dev/null || true && shopt -u extglob "
            "2>/dev/null || true && { \\builtin unalias -- 'unsetenv'; \\builtin unset -f -- "
            "'unsetenv'; } >/dev/null 2>&1 || true && eval '" + command + "' < /dev/null "
            "&& pwd -P >| /tmp/claude-9096-cwd")


@pytest.fixture(autouse=True)
def _hook_off(monkeypatch):
    monkeypatch.delenv(turn.SWITCH_ENV, raising=False)


class Tree:
    """A process table: claude at CLAUDE, live as its sidecar says."""

    def __init__(self):
        self.table: dict[int, Proc] = {
            1: Proc(1, 0, T0, "/sbin/launchd"),
            CLAUDE: Proc(CLAUDE, 1, T0, f"claude --session-id {SID}"),
        }
        self._next = CLAUDE + 1

    def add(self, command: str | None, parent: int = CLAUDE) -> int:
        pid = self._next
        self._next += 1
        self.table[pid] = Proc(pid, parent, T0 + 60, command)
        return pid

    def monitor(self) -> Tree:
        self.add(MONITOR)
        return self


def seat(status: str = "shell", pid: int = CLAUDE, proc_start: str = START) -> cc.Session:
    return cc.Session(id=SID, harness=cc.HARNESS, busy=status == "busy",
                      updated_at=NOW - 100, pid=pid, raw_status=status, proc_start=proc_start)


def read(tree: Tree, status: str = "shell", listeners=cc.DEFAULT_LISTENERS) -> Reading:
    return cc.pane_reading(seat(status), tree.table, listeners)


# --- UNKNOWN: the reading cannot be made, and it is never FREE ---------------------

def test_a_dead_pid_is_unknown():
    tree = Tree().monitor()
    assert cc.pane_reading(seat(pid=4999), tree.table) == Reading(PANE_UNKNOWN, "no live process")


def test_a_reused_pid_is_unknown():
    tree = Tree().monitor()
    late = "Sun Dec  8 01:00:30 2030"     # 30 s off: another process holds the pid
    assert cc.pane_reading(seat(proc_start=late), tree.table).word == PANE_UNKNOWN


def test_an_empty_table_is_unknown_whatever_the_status():
    for status in ("busy", "shell", "idle", "waiting"):
        assert cc.pane_reading(seat(status), {}) == Reading(PANE_UNKNOWN, "no live process")


@pytest.mark.parametrize("word", ["compacting", "", "SHELL"])
def test_a_status_word_we_do_not_know_is_unknown(word):
    assert read(Tree().monitor(), word) == Reading(PANE_UNKNOWN, f"status {word}")


def test_a_table_read_without_commands_is_unknown_not_free():
    tree = Tree()
    tree.add(None)                         # a direct child whose command was not read
    assert read(tree) == Reading(PANE_UNKNOWN, "no commands")


# --- BUSY: the turn, a prompt, or work under a tool shell ---------------------------

def test_busy_is_the_turn_whatever_the_children():
    tree = Tree().monitor()
    assert read(tree, "busy") == Reading(PANE_BUSY, "turn")
    tree.add(tool_shell("pager listen --timeout 1200"))
    assert read(tree, "busy") == Reading(PANE_BUSY, "turn")


def test_waiting_is_busy():
    assert read(Tree().monitor(), "waiting") == Reading(PANE_BUSY, "waiting")


def test_a_background_sleep_is_work():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("sleep 30"))
    tree.add("sleep 30", parent=sh)
    assert read(tree) == Reading(PANE_BUSY, "work: sleep 30")


def test_the_2351_case_make_test_beside_a_sleep_is_busy():
    """r02's row: `shell`, bash[make test], bash[sleep 20], caffeinate, monitor."""
    tree = Tree().monitor()
    tree.add(CAFFEINATE)
    make = tree.add(tool_shell("make test"))
    m = tree.add("make test", parent=make)
    sh = tree.add("/bin/sh -c uv run pytest -q", parent=m)
    tree.add(f"{PY} /Users/x/.venv/bin/pytest -q", parent=sh)
    sleep = tree.add(tool_shell("sleep 20"))
    tree.add("sleep 20", parent=sleep)
    assert read(tree) == Reading(PANE_BUSY, "work: make test")


def test_a_foremans_tm_task_wait_is_work_by_default():
    """Q2 is still open; until it is answered, the default rule says BUSY."""
    tree = Tree().monitor()
    sh = tree.add(tool_shell("tm task wait ST-134"))
    tree.add(f"{PY} /Users/alice/.local/bin/tm task wait ST-134", parent=sh)
    assert read(tree) == Reading(PANE_BUSY, "work: tm task wait ST-134")


def test_a_tool_shell_with_nothing_under_it_is_work():
    tree = Tree().monitor()
    tree.add(tool_shell("while :; do :; done"))
    assert read(tree) == Reading(PANE_BUSY, "work: shell, nothing under it")


def test_a_pipe_helper_alone_is_work():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("tail -f build.log"))
    tree.add("tail -f build.log", parent=sh)
    assert read(tree) == Reading(PANE_BUSY, "work: tail -f build.log")


def test_a_listener_beside_work_in_one_shell_is_work():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("pager listen & make test"))
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen", parent=sh)
    tree.add("make test", parent=sh)
    assert read(tree) == Reading(PANE_BUSY, "work: make test")


def test_a_listener_shell_beside_a_work_shell_is_busy():
    tree = Tree().monitor()
    free = tree.add(tool_shell("pager listen --timeout 1200"))
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen --timeout 1200", parent=free)
    work = tree.add(tool_shell("sleep 30"))
    tree.add("sleep 30", parent=work)
    assert read(tree).word == PANE_BUSY


def test_nested_shells_are_walked_through_to_the_work():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("make check"))
    inner = tree.add("/bin/sh -c pytest", parent=sh)
    deeper = tree.add("bash -c pytest", parent=inner)
    tree.add(f"{PY} -I /Users/x/.venv/bin/pytest", parent=deeper)
    assert read(tree) == Reading(PANE_BUSY, "work: pytest")


def test_the_why_is_cut_at_forty_characters():
    tree = Tree().monitor()
    long = "rsync -a /Users/alice/a/very/long/source/path /Volumes/elsewhere/target"
    sh = tree.add(tool_shell(long))
    tree.add(long, parent=sh)
    reading = read(tree)
    assert reading.why == "work: " + long[:40]


# --- FREE: the turn is over and nothing but a listener is live ----------------------

def test_the_monitor_alone_is_free():
    """r02: with the turn over, the pager monitor alone makes the sidecar `shell`."""
    assert read(Tree().monitor()) == Reading(PANE_FREE, "nothing live")


def test_the_monitor_and_caffeinate_are_free():
    tree = Tree().monitor()
    tree.add(CAFFEINATE)
    assert read(tree) == Reading(PANE_FREE, "nothing live")


def test_a_free_workers_pager_listen_is_free():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("pager listen --timeout 1200"))
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen --timeout 1200", parent=sh)
    assert read(tree) == Reading(PANE_FREE, "listener")


def test_a_listener_through_a_nested_shell_with_a_pipe_helper_is_free():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("bash -c 'pager listen | tee l.log'"))
    inner = tree.add("bash -c pager listen | tee l.log", parent=sh)
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen", parent=inner)
    tree.add("tee l.log", parent=inner)
    assert read(tree) == Reading(PANE_FREE, "listener")


def test_idle_with_no_children_is_free():
    assert read(Tree(), "idle") == Reading(PANE_FREE, "nothing live")


# --- the listener list --------------------------------------------------------------

def _waiting_foreman() -> Tree:
    tree = Tree().monitor()
    sh = tree.add(tool_shell("tm task wait ST-134"))
    tree.add(f"{PY} /Users/alice/.local/bin/tm task wait ST-134", parent=sh)
    return tree


def test_the_list_decides_what_is_a_listener():
    assert read(_waiting_foreman(), listeners=["pager listen", "tm task wait"]).word == PANE_FREE


def test_an_empty_list_makes_every_tool_shell_work():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("pager listen"))
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen", parent=sh)
    assert read(tree, listeners=[]).word == PANE_BUSY


@pytest.mark.parametrize("junk", ["pager listen", None, 7, {"pager": "listen"}])
def test_a_list_that_is_not_a_list_trusts_nothing(junk):
    tree = Tree().monitor()
    sh = tree.add(tool_shell("pager listen"))
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen", parent=sh)
    assert read(tree, listeners=junk).word == PANE_BUSY


def test_a_listener_matches_on_its_words_not_a_prefix_of_one():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("pager listener-bench"))
    tree.add(f"{PY} /Users/x/bin/pager listener-bench", parent=sh)
    assert read(tree).word == PANE_BUSY


# --- the shapes ---------------------------------------------------------------------

@pytest.mark.parametrize("command, words", [
    (MONITOR, ["pager", "listen", "--monitor"]),
    (f"{PY} -I /Users/x/bin/pager listen", ["pager", "listen"]),
    ("python3 -m pytest -q", ["pytest", "-q"]),
    ("node /usr/local/lib/node_modules/x/cli.js run", ["cli.js", "run"]),
    ("-bash", ["bash"]),
    ("/bin/sleep 30", ["sleep", "30"]),
    ("python3", ["python3"]),
    ("", []),
    (None, []),
])
def test_command_words(command, words):
    assert cc.command_words(command) == words


@pytest.mark.parametrize("command, shell", [
    (tool_shell("sleep 30"), True),
    ("/bin/zsh -c ls", True),
    ("zsh -lc ls", True),
    ("bash --norc -c ls", True),
    ("/bin/sh -c pytest", True),
    ("dash -c true", True),
    (MONITOR, False),
    (CAFFEINATE, False),
    ("bash run-tests.sh -c", False),     # -c after the operand is the script's
    ("bash", False),
    ("fish -c ls", False),
    (None, False),
])
def test_is_tool_shell(command, shell):
    assert cc.is_tool_shell(command) is shell


# --- the turn mark is never an input ------------------------------------------------

def test_the_reading_is_the_same_with_and_without_a_turn_mark(tmp_path, monkeypatch):
    """A busy sidecar with a valid turn-ended mark reads `idle with children`
    to `state`, and BUSY here either way: the reading keys on `raw_status`."""
    cars = tmp_path / "sidecars"
    cars.mkdir()
    root = tmp_path / "sessions"
    (root / SID).mkdir(parents=True)
    monkeypatch.setenv(cc.SIDECAR_DIR_ENV, str(cars))
    monkeypatch.setenv("SESSIONS_DIR", str(root))
    (cars / f"{CLAUDE}.json").write_text(json.dumps({
        "pid": CLAUDE, "sessionId": SID, "status": "busy", "updatedAt": (NOW - 600) * 1000,
        "procStart": START}))
    tree = Tree().monitor()
    sh = tree.add(tool_shell("sleep 30"))
    tree.add("sleep 30", parent=sh)

    [before] = cc.sessions(procs=tree.table)
    assert before.state == cc.BUSY
    mark = turn.mark_path(SID)
    mark.write_text(json.dumps({"v": 1, "at": NOW - 30, "harness": "claude-code",
                                "event": "Stop", "background": 1}) + "\n")
    [after] = cc.sessions(procs=tree.table)
    assert after.state == cc.IDLE_WITH_CHILDREN     # the mark counted for `state`

    assert cc.pane_reading(before, tree.table) == cc.pane_reading(after, tree.table) \
        == Reading(PANE_BUSY, "turn")
    import os
    assert turn.SWITCH_ENV not in os.environ


# --- do-002: a childless nested shell is work (do-001 review note 2) -----------------

def test_a_childless_nested_script_shell_beside_a_listener_is_work():
    """`bash ./long.sh` between its commands has no child: it runs its own
    builtins, so it is work, and the listener beside it cannot make it FREE."""
    tree = Tree().monitor()
    sh = tree.add(tool_shell("pager listen & bash ./long.sh"))
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen", parent=sh)
    tree.add("bash ./long.sh", parent=sh)
    assert read(tree) == Reading(PANE_BUSY, "work: bash ./long.sh")


def test_a_childless_nested_c_shell_is_work_too():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("pager listen & sh -c 'while :; do :; done'"))
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen", parent=sh)
    tree.add("sh -c while :; do :; done", parent=sh)
    assert read(tree).word == PANE_BUSY


def test_a_nested_script_shell_with_a_listener_under_it_is_still_walked_through():
    tree = Tree().monitor()
    sh = tree.add(tool_shell("bash ./listen.sh"))
    inner = tree.add("bash ./listen.sh", parent=sh)
    tree.add(f"{PY} /Users/alice/.local/bin/pager listen", parent=inner)
    assert read(tree) == Reading(PANE_FREE, "listener")
