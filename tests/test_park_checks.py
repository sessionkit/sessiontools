"""`session park`: the five checks (park.py, design §3).

Each check reads PASS, FAIL or UNKNOWN, and UNKNOWN is never PASS. Every
seat here is FAKE: a synthetic process table in the shapes
`tests/test_pane_reading.py` pins, task trees and mailboxes in tmp
directories, and git only in scratch repos of the test's own. Nothing reads
the live fleet, a live mailbox or live config.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env

from sessiontools import claude_code as cc
from sessiontools import park, procs, reap
from sessiontools.listing import Row
from sessiontools.procs import Proc

SID = "5e1f0c2a-0000-4000-8000-0000000000bb"
OTHER = "9a9a9a9a-0000-4000-8000-0000000000cc"
START = "Sun Jun  9 01:00:00 2030"
T0 = procs.parse_ctime(START)

PY = "/opt/homebrew/Cellar/python@3.14/3.14.8_2/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
MONITOR = f"{PY} /Users/u/.local/bin/pager listen --monitor"


def tool_shell(command: str) -> str:
    """A Bash tool call as Claude Code runs it (the shape test_pane_reading pins)."""
    return ("/opt/homebrew/bin/bash -c source /Users/u/.claude/shell-snapshots/"
            "snapshot-bash-1907254800000-zh6e7c.sh 2>/dev/null || true && eval '"
            + command + "' < /dev/null && pwd -P >| /tmp/claude-9096-cwd")


class Seat:
    """One fake seat: its claude process, the pager monitor beside it, and
    the tool shells added under it."""

    def __init__(self, status: str = "busy", cwd: str | None = None, name: str | None = None,
                 turn_ended: bool = False):
        self.pid = 4000
        self.table: dict[int, Proc] = {1: Proc(1, 0, T0, "/sbin/launchd"),
                                       self.pid: Proc(self.pid, 1, T0, f"claude --session-id {SID}"),
                                       4001: Proc(4001, self.pid, T0 + 1, MONITOR),
                                       4002: Proc(4002, self.pid, T0 + 1, "caffeinate -i -t 300")}
        self._next = 4100
        self.session = cc.Session(id=SID, harness=cc.HARNESS, busy=status == "busy" and not turn_ended,
                                  raw_status=status, updated_at=T0 + 60, pid=self.pid,
                                  proc_start=START, cwd=cwd, name=name,
                                  turn_ended_at=T0 + 60 if turn_ended else None)

    def shell(self, *commands: str) -> int:
        """A tool shell under the seat, each of COMMANDS a child of it."""
        shell = self._next
        self._next += 10
        self.table[shell] = Proc(shell, self.pid, T0 + 2, tool_shell(" && ".join(commands)))
        for i, c in enumerate(commands, start=1):
            self.table[shell + i] = Proc(shell + i, shell, T0 + 2, c)
        return shell

    def work(self, facts: park.Facts | None = None, own_pid: int | None = None,
             listeners: object = None) -> park.Check:
        return park.check_work(self.session, self.table,
                               park.Facts() if facts is None else facts, listeners, own_pid)


def facts(*records: dict) -> park.Facts:
    return park.transcript_facts(json.dumps(r) for r in records)


def use(call_id: str, name: str, **tool_input) -> dict:
    return {"type": "assistant", "timestamp": "2030-06-09T01:00:05.000Z",
            "message": {"content": [{"type": "tool_use", "id": call_id, "name": name,
                                     "input": tool_input}]}}


def done(call_id: str) -> dict:
    return {"type": "user", "timestamp": "2030-06-09T01:09:00.000Z",
            "origin": {"kind": "task-notification"},
            "message": {"content": f"<task-notification>\n<tool-use-id>{call_id}</tool-use-id>\n"
                                   "<status>completed</status>\n</task-notification>"}}


# --- git and task trees ---------------------------------------------------------------

def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *HOOKS_OFF, *args], cwd=cwd,
                          capture_output=True, text=True, check=True, env=hermetic_env())


def task_dir(root: Path, task_id: str = "WEB-9", status: str = "active") -> Path:
    d = root / "projects" / "p" / "tasks" / f"{task_id}_feat_x"
    d.mkdir(parents=True, exist_ok=True)
    (d / "README.md").write_text(f"---\ntask_id: {task_id}\ntitle: x\nstatus: {status}\n---\n")
    return d


@pytest.fixture
def store(tmp_path) -> Path:
    """A scratch store holding one active task with a worktree repo, all clean."""
    root = tmp_path / "store"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("**/worktree/\n")
    d = task_dir(root)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    wt = d / "worktree"
    wt.mkdir()
    git(wt, "init", "-q")
    git(wt, "commit", "-q", "--allow-empty", "-m", "seed")
    return d


# --- a folder that cannot be searched (ST-206) ------------------------------------------
# From 3.14, Path.is_dir() / is_file() / exists() return False for a path they
# cannot stat; 3.11 to 3.13 raised PermissionError. A check reading through
# such a folder must say UNKNOWN on every version, never PASS.

needs_non_root = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="NOT TESTED as root: root searches a mode 000 folder, so none can be built")


@contextlib.contextmanager
def locked(d: Path, mode: int = 0o000):
    """D at MODE for the body, its mode restored after, pass or fail."""
    d.chmod(mode)
    try:
        yield d
    finally:
        d.chmod(0o700)


# --- 3.1 tree ---------------------------------------------------------------------------

def test_a_clean_task_reads_pass_naming_both_parts(store):
    got = park.check_tree(str(store))
    assert (got.word, got.why) == (park.PASS, "task WEB-9: worktree and store clean")


def test_a_cwd_deep_in_the_worktree_reads_its_task(store):
    sub = store / "worktree" / "src"
    sub.mkdir()
    assert park.check_tree(str(sub)).word == park.PASS


def test_a_dirty_worktree_fails_and_names_the_path(store):
    (store / "worktree" / "draft.py").write_text("x\n")
    got = park.check_tree(str(store))
    assert got.word == park.FAIL and got.why == "task WEB-9: worktree dirty: ?? draft.py"


def test_a_dirty_store_fails(store):
    (store / "notes.md").write_text("unsaved\n")
    got = park.check_tree(str(store))
    assert got.word == park.FAIL and got.why.startswith("task WEB-9: store dirty: ?? ")
    assert got.why.endswith("WEB-9_feat_x/notes.md")


def test_dirty_beats_unreadable(store, tmp_path):
    (store / "draft.md").write_text("x\n")
    (store / "worktree" / ".git").rename(tmp_path / "moved.git")   # no longer its own repo
    got = park.check_tree(str(store))
    assert got.word == park.FAIL and "worktree unreadable" in got.why and "store dirty" in got.why


def test_no_task_reads_the_cwd_repo(tmp_path):
    repo = tmp_path / "home-repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "seed")
    assert park.check_tree(str(repo)) == park.Check("tree", park.PASS, "no task: cwd clean")
    (repo / "scratch.txt").write_text("x\n")
    assert park.check_tree(str(repo)).word == park.FAIL


def test_a_cwd_outside_any_repo_is_unknown(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    got = park.check_tree(str(loose))
    assert got.word == park.UNKNOWN and got.why.startswith("no task: cwd unreadable (git exit")


def test_an_unreadable_task_readme_is_unknown(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    (d / "README.md").write_text("---\n: [unclosed\n---\n")
    got = park.check_tree(str(d))
    assert got.word == park.UNKNOWN and "README unreadable" in got.why


def test_no_cwd_is_unknown():
    assert park.check_tree(None).word == park.UNKNOWN


def test_tree_runs_git_with_reaps_status_args(tmp_path):
    seen = []

    def fake(args, cwd):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    assert park.check_tree(str(tmp_path), git=fake).word == park.PASS
    assert seen == [[*reap.STATUS_ARGS, "--", "."]]


# --- reap's tail_parts: the refactor keeps tail() exactly ---------------------------------

@pytest.mark.parametrize("out, code, state", [
    ("", 0, reap.PART_CLEAN), ("?? a\n", 0, reap.PART_DIRTY), ("", 128, reap.PART_UNREADABLE)])
def test_tail_parts_tells_dirty_from_unreadable(tmp_path, out, code, state):
    def fake(args, cwd):
        return subprocess.CompletedProcess(args, code, out, "fatal: not a git repository")
    [part] = reap.tail_parts(tmp_path, fake)
    assert part.where == "store" and part.state == state
    clean, detail = reap.tail(tmp_path, fake)
    assert clean == (state == reap.PART_CLEAN) and detail == part.detail


def test_tail_parts_takes_a_label_for_the_dot_part(tmp_path):
    def fake(args, cwd):
        return subprocess.CompletedProcess(args, 128, "", "fatal: x")
    [part] = reap.tail_parts(tmp_path, fake, "cwd")
    assert part.detail == "cwd unreadable (git exit 128: fatal: x)"


# --- 3.2 work ---------------------------------------------------------------------------

def test_a_seat_with_only_the_monitor_and_caffeinate_has_nothing_running():
    assert Seat().work() == park.Check("work", park.PASS, "nothing running")


def test_a_listener_shell_passes_and_is_named():
    s = Seat()
    s.shell(f"{PY} /Users/u/.local/bin/pager listen --timeout 1200")
    assert s.work() == park.Check("work", park.PASS, "listener only (pager listen --timeout 1200)")


def test_a_tool_shell_running_work_fails():
    s = Seat()
    s.shell("make test")
    assert s.work() == park.Check("work", park.FAIL, "work: make test")


def test_own_ancestry_is_not_counted_but_another_seats_check_counts_it():
    """design §10 test 9: the shell running this very `session park`."""
    s = Seat()
    shell = s.shell(f"{PY} /Users/u/.local/bin/session park --no-promises")
    me = shell + 1
    assert s.work(own_pid=me) == park.Check("work", park.PASS, "nothing running")
    got = s.work()                                         # --check SEAT from elsewhere
    assert got.word == park.FAIL and got.why.startswith("work: session park")


def test_own_ancestry_skips_only_its_own_shell():
    s = Seat()
    me = s.shell("session park") + 1
    s.shell("sleep 600")
    got = s.work(own_pid=me)
    assert got == park.Check("work", park.FAIL, "work: sleep 600")


def test_a_custom_listener_list_is_honoured():
    s = Seat()
    s.shell("tail -f /var/log/x")
    assert s.work(listeners=("tail -f",)).word == park.PASS
    assert s.work().word == park.FAIL


def test_idle_with_children_fails():
    s = Seat(status="busy", turn_ended=True)
    assert s.session.state == cc.IDLE_WITH_CHILDREN
    assert s.work() == park.Check("work", park.FAIL, "idle with children")


def test_an_empty_process_table_is_unknown():
    s = Seat()
    got = park.check_work(s.session, {}, park.Facts())
    assert got == park.Check("work", park.UNKNOWN, "no live process")


def test_a_process_that_is_not_the_session_is_unknown():
    s = Seat()
    s.table[s.pid] = Proc(s.pid, 1, T0 + 999, "something else")
    assert s.work().word == park.UNKNOWN


def test_no_commands_in_the_table_is_unknown():
    s = Seat()
    s.table[4001] = Proc(4001, s.pid, T0 + 1, None)
    assert s.work() == park.Check("work", park.UNKNOWN, "no commands in the process table")


def test_no_sidecar_is_unknown():
    assert park.check_work(None, Seat().table, park.Facts()).word == park.UNKNOWN


def test_an_unreadable_transcript_is_unknown():
    s = Seat()
    got = park.check_work(s.session, s.table, None)
    assert got.word == park.UNKNOWN and "transcript unreadable" in got.why


def test_work_beats_unknown():
    s = Seat()
    s.shell("make test")
    assert park.check_work(s.session, s.table, None).word == park.FAIL


# design §10 test 10: a background agent runs inside the claude process.

def test_a_background_agent_with_no_completion_fails():
    f = facts(use("toolu_A1", "Agent", prompt="go", run_in_background=True))
    got = Seat().work(f)
    assert got == park.Check("work", park.FAIL,
                             "background agent toolu_A1 running: no completion seen")


def test_a_background_agent_with_its_completion_passes():
    f = facts(use("toolu_A1", "Agent", prompt="go", run_in_background=True), done("toolu_A1"))
    assert Seat().work(f).word == park.PASS


def test_a_foreground_agent_is_not_open_work():
    assert Seat().work(facts(use("toolu_A2", "Agent", prompt="go"))).word == park.PASS


# design §10 test 13, the process half: the idle timer, both ways.

@pytest.mark.parametrize("argv0", ["tm", "taskmaster"])
def test_the_idle_timer_is_a_listener_for_park(argv0):
    s = Seat()
    s.shell(f"{PY} /Users/u/.local/bin/{argv0} task standdown T-1 --idle-wait")
    got = s.work()
    named = f"{argv0} task standdown T-1 --idle-wait"[:cc.WHY_WIDTH]
    assert got == park.Check("work", park.PASS, f"listener only ({named})")


@pytest.mark.parametrize("command", [
    "tm task standdown T-1 --free",
    "tm task standdown T-1 --check --role worker",
    "tm task standdown T-1",
    "tm task wait T-1",
    "tm slice list",
    "tm task --idle-wait standdown",
    "tm task standdown T-1 --idle-waiting",
    "grep --idle-wait log",
])
def test_any_other_tm_verb_or_flag_is_work(command):
    s = Seat()
    s.shell(f"{PY} /Users/u/.local/bin/{command}")
    got = s.work()
    assert got.word == park.FAIL and got.why.startswith("work: ")


def test_a_timer_with_work_beside_it_is_work():
    s = Seat()
    s.shell(f"{PY} /Users/u/.local/bin/tm task standdown T-1 --idle-wait", "make test")
    assert s.work() == park.Check("work", park.FAIL, "work: make test")


def test_the_pane_signal_still_reads_the_timer_as_work():
    """The timer is a listener for park ONLY: pane_reading (and so reap) is unchanged."""
    s = Seat(status="idle")
    s.shell(f"{PY} /Users/u/.local/bin/tm task standdown T-1 --idle-wait")
    reading = cc.pane_reading(s.session, s.table)
    assert reading.word == cc.PANE_BUSY
    assert reading.why.startswith("work: tm task standdown T-1 --idle-wait")


# --- 3.3 claim --------------------------------------------------------------------------

def claim(d: Path, slice_id: str, sid: str = SID, released: str | None = None) -> None:
    (d / "harness" / ".claim").mkdir(parents=True, exist_ok=True)
    (d / "harness" / ".claim" / f"{slice_id}.json").write_text(json.dumps({
        "task_id": "WEB-9", "slice_id": slice_id, "session_id": sid,
        "claimed_at": "2030-06-09T06:00:00+00:00", "released_at": released, "claim_history": []}))


def slate(d: Path, *slices: dict, name: str = "slices.json", key: str = "slices") -> None:
    (d / "harness").mkdir(exist_ok=True)
    (d / "harness" / name).write_text(json.dumps({key: list(slices)}))


def test_no_harness_holds_no_claim(tmp_path):
    d = task_dir(tmp_path)
    assert park.check_claim(SID, str(d)) == park.Check("claim", park.PASS, "no claim held in WEB-9")


def test_an_unreleased_claim_record_fails_and_names_the_slice(tmp_path):
    d = task_dir(tmp_path)
    claim(d, "do-002")
    got = park.check_claim(SID, str(d))
    assert got == park.Check("claim", park.FAIL, "holds the claim on slice do-002 in WEB-9")


def test_an_in_progress_slate_claim_fails_without_a_sidecar(tmp_path):
    d = task_dir(tmp_path)
    slate(d, {"id": "do-001", "status": "passing", "claimed_by": f"worker.{SID}"},
          {"id": "do-002", "status": "in_progress", "claimed_by": f"worker.{SID}"})
    assert park.check_claim(SID, str(d)).why == "holds the claim on slice do-002 in WEB-9"


def test_a_features_json_slate_is_read_too(tmp_path):
    d = task_dir(tmp_path)
    slate(d, {"id": "f-1", "status": "in_progress", "claimed_by": f"worker.{SID}"},
          name="features.json", key="features")
    assert park.check_claim(SID, str(d)).word == park.FAIL


def test_the_same_slice_from_both_sources_is_named_once(tmp_path):
    d = task_dir(tmp_path)
    claim(d, "do-002")
    slate(d, {"id": "do-002", "status": "in_progress", "claimed_by": f"worker.{SID}"})
    assert park.check_claim(SID, str(d)).why == "holds the claim on slice do-002 in WEB-9"


def test_released_claims_and_other_sessions_claims_pass(tmp_path):
    d = task_dir(tmp_path)
    claim(d, "do-001", released="2030-06-09T06:05:00+00:00")
    claim(d, "do-003", sid=OTHER)
    slate(d, {"id": "do-003", "status": "in_progress", "claimed_by": f"worker.{OTHER}"},
          {"id": "do-004", "status": "in_progress", "claimed_by": f"worker.x{SID[1:]}"})
    assert park.check_claim(SID, str(d)).word == park.PASS


def test_an_unparseable_claim_file_is_unknown(tmp_path):
    d = task_dir(tmp_path)
    (d / "harness" / ".claim").mkdir(parents=True)
    (d / "harness" / ".claim" / "do-002.json").write_text("{not json")
    got = park.check_claim(SID, str(d))
    assert got.word == park.UNKNOWN and "claim do-002.json unreadable" in got.why


def test_an_unparseable_slate_is_unknown_but_a_held_claim_still_fails(tmp_path):
    d = task_dir(tmp_path)
    (d / "harness").mkdir()
    (d / "harness" / "slices.json").write_text("[")
    assert park.check_claim(SID, str(d)).word == park.UNKNOWN
    claim(d, "do-002")
    assert park.check_claim(SID, str(d)).word == park.FAIL


@pytest.mark.parametrize("cwd", [None, ""])
def test_no_cwd_known_is_unknown_not_no_task(cwd):
    """With no sidecar there is no cwd: nowhere to look is not "no claim"."""
    assert park.check_claim(SID, cwd) == park.Check("claim", park.UNKNOWN, "no cwd known")


def test_no_task_at_the_cwd_holds_no_claim(tmp_path):
    got = park.check_claim(SID, str(tmp_path))
    assert got == park.Check("claim", park.PASS, "no task at cwd: no claim to hold")


@pytest.mark.parametrize("name", ["web+super.3987c186", "acme+gc", "pat", None])
def test_a_name_that_names_no_task_is_not_a_mismatch(tmp_path, name):
    assert park.check_claim(SID, str(tmp_path), name).word == park.PASS


def test_a_name_that_names_another_task_is_unknown(tmp_path):
    d = task_dir(tmp_path)
    got = park.check_claim(SID, str(d), "p+web-8.aaaa1111")
    assert got == park.Check("claim", park.UNKNOWN, "name says web-8, cwd says WEB-9")
    got = park.check_claim(SID, str(tmp_path), "p+web-8.aaaa1111")
    assert got.why == "name says web-8, cwd says no task"
    assert park.check_claim(SID, str(d), "p+web-9.aaaa1111").word == park.PASS


@needs_non_root
def test_a_claim_dir_that_cannot_be_stat_ed_is_unknown_not_pass(tmp_path):
    d = task_dir(tmp_path)
    vault = tmp_path / "vault"
    (vault / ".claim").mkdir(parents=True)
    (d / "harness").mkdir()
    (d / "harness" / ".claim").symlink_to(vault / ".claim")
    with locked(vault):
        got = park.check_claim(SID, str(d))
    assert got == park.Check("claim", park.UNKNOWN, "claim dir unreadable (PermissionError)")


@needs_non_root
def test_a_slate_that_cannot_be_stat_ed_is_unknown_not_pass(tmp_path):
    d = task_dir(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "slices.json").write_text(json.dumps({"slices": []}))
    (d / "harness").mkdir()
    (d / "harness" / "slices.json").symlink_to(vault / "slices.json")
    with locked(vault):
        got = park._guarded("claim", lambda: park.check_claim(SID, str(d)))
    assert got.word == park.UNKNOWN and "PermissionError" in got.why


@needs_non_root
def test_a_harness_that_cannot_be_searched_is_unknown_not_pass(tmp_path):
    d = task_dir(tmp_path)
    slate(d, {"id": "do-002", "status": "in_progress", "claimed_by": f"p+web-9.worker.{OTHER}"})
    with locked(d / "harness"):
        got = park._guarded("claim", lambda: park.check_claim(SID, str(d)))
    assert got.word == park.UNKNOWN and "PermissionError" in got.why


# --- 3.4 mail ---------------------------------------------------------------------------

@pytest.fixture
def mail(tmp_path, monkeypatch):
    """A temp mail root and sessions dir; returns a helper that delivers."""
    root, sessions = tmp_path / "mail", tmp_path / "sessions"
    (sessions / SID).mkdir(parents=True)
    env = {"SESSION_MAIL_ROOT": str(root), "SESSIONS_DIR": str(sessions)}

    class Box:
        dir = root / SID
        marks = sessions / SID / "mail" / "marks"

        def __init__(self):
            self.env = env
            self.n = 0

        def make(self):
            for sub in ("new", "cur", "tmp"):
                (self.dir / sub).mkdir(parents=True, exist_ok=True)

        def deliver(self, mid: str | None, sub: str = "new", info: str = "",
                    supersedes: str | None = None) -> Path:
            self.make()
            self.n += 1
            head = f"From: a@b\nSubject: secret subject {self.n}\n"
            if mid:
                head += f"Message-ID: <{mid}>\n"
            if supersedes:
                head += f"Supersedes: <{supersedes}>\n"
            p = self.dir / sub / f"17921{self.n:05d}.M1P1.host{info}"
            p.write_text(head + "\nsecret body\n")
            return p

        def mark(self, *lines: str) -> None:
            self.marks.parent.mkdir(parents=True, exist_ok=True)
            self.marks.write_text("".join(f"{ln}\n" for ln in lines))

        def check(self, subscribe: list[str] | None = None) -> park.Check:
            return park.check_mail(SID, self.env, [] if subscribe is None else subscribe)
    return Box()


def test_no_box_means_nothing_was_ever_delivered(mail):
    assert mail.check() == park.Check("mail", park.PASS, "no session box: nothing was ever delivered")


def test_an_empty_box_passes(mail):
    mail.make()
    assert mail.check() == park.Check("mail", park.PASS, "session box: 0 unread")


def test_unread_mail_fails_naming_ids_never_subjects(mail):
    mail.deliver("1.a@h")
    mail.deliver("2.a@h", sub="cur", info=":2,S")
    got = mail.check()
    assert got == park.Check("mail", park.FAIL, "2 unread: <1.a@h>, <2.a@h>")
    assert "secret" not in got.why


def test_read_marks_in_both_line_forms_make_mail_read(mail):
    mail.deliver("1.a@h")
    mail.deliver("2.a@h")
    mail.deliver("3.a@h")
    mail.mark(f"2030-06-09T17:00:00+00:00 1.a@h read worker {SID}",           # ruled, empty reason
              f"2030-06-09T17:01:00+00:00 <2.a@h> read worker {SID} opened",  # ruled, reason
              f"3.a@h read {SID} 2030-06-09T00:00:00")                        # the old four fields
    assert mail.check().word == park.PASS


def test_a_seen_only_mark_is_still_unread(mail):
    mail.deliver("1.a@h")
    mail.mark(f"2030-06-09T17:00:00+00:00 1.a@h seen ride {SID}")
    assert mail.check().why == "1 unread: <1.a@h>"


def test_a_draft_and_a_superseded_message_do_not_count(mail):
    mail.deliver("draft@h", sub="cur", info=":2,DS")
    mail.deliver("old@h")
    mail.deliver("new@h", supersedes="old@h")
    mail.mark(f"2030-06-09T17:00:00+00:00 new@h read worker {SID}")
    assert mail.check().word == park.PASS


def test_a_message_with_no_id_is_unread_and_named_by_its_file(mail):
    p = mail.deliver(None)
    assert mail.check().why == f"1 unread: {p.name}"


def test_many_unread_are_counted(mail):
    for i in range(5):
        mail.deliver(f"{i}.a@h")
    assert mail.check().why == "5 unread: <0.a@h>, <1.a@h>, <2.a@h> +2 more"


def test_an_unreadable_marks_file_is_unknown(mail):
    mail.deliver("1.a@h")
    mail.marks.mkdir(parents=True)                    # a directory where the file should be
    got = mail.check()
    assert got.word == park.UNKNOWN and "unreadable" in got.why


def test_a_box_that_is_not_a_maildir_is_unknown(mail):
    mail.dir.mkdir(parents=True)
    assert mail.check().word == park.UNKNOWN


def test_a_subscribed_box_is_unknown_and_named(mail):
    mail.make()
    got = mail.check(["sessionvoice"])
    assert got == park.Check("mail", park.UNKNOWN, "subscribed to sessionvoice: not read")


def test_subscriptions_are_read_from_the_environment(mail):
    mail.make()
    env = {**mail.env, "SESSIONMAIL_SUBSCRIBE": "sessionvoice, sessionmail"}
    assert park.subscribed(env) == ["sessionvoice", "sessionmail"]
    assert park.check_mail(SID, env).word == park.UNKNOWN
    assert park.check_mail(SID, {**mail.env, "SESSIONMAIL_SUBSCRIBE": " "}).word == park.PASS


def test_unread_in_the_own_box_beats_a_subscription(mail):
    mail.deliver("1.a@h")
    assert mail.check(["sessionvoice"]).word == park.FAIL


def test_mail_reads_only_the_header_block(mail):
    p = mail.deliver("1.a@h")
    p.write_bytes(b"Message-ID: <1.a@h>\n\n" + b"\xff\xfe not text " * 10_000)
    mail.mark(f"2030-06-09T17:00:00+00:00 1.a@h read worker {SID}")
    assert mail.check().word == park.PASS


@needs_non_root
def test_a_mail_root_that_cannot_be_searched_is_unknown_not_no_box(mail):
    mail.deliver("1.a@h")
    with locked(mail.dir.parent):
        got = park._guarded("mail", mail.check)
    assert got.word == park.UNKNOWN and "PermissionError" in got.why


@needs_non_root
def test_a_new_folder_listed_but_not_searchable_is_unknown_not_zero_unread(mail):
    mail.deliver("1.a@h")
    with locked(mail.dir / "new", 0o400):
        got = mail.check()
    assert got == park.Check("mail", park.UNKNOWN,
                             "session box or marks unreadable (PermissionError)")


# --- 3.5 promises -----------------------------------------------------------------------

@pytest.mark.parametrize("flags, word, why", [
    ({}, park.UNKNOWN, "not declared (--no-promises, --promise TEXT, --handed-on REF)"),
    ({"no_promises": True}, park.PASS, "declared none"),
    ({"promise": ["the verdict at 12:34"]}, park.FAIL, "open: the verdict at 12:34"),
    ({"promise": ["a", "b"]}, park.FAIL, "open: a +1 more"),
    ({"promise": ["a", "b"], "handed_on": "<m@h>"}, park.PASS, "2 handed on in <m@h>"),
    ({"handed_on": "log/x.md"}, park.PASS, "handed on in log/x.md"),
])
def test_promises_are_what_was_declared(flags, word, why):
    assert park.check_promises(park.declare_promises(**flags)) == park.Check("promises", word, why)


@pytest.mark.parametrize("flags", [{"promise": ["a"]}, {"handed_on": "<m@h>"}])
def test_no_promises_with_another_promise_flag_is_a_usage_error(flags):
    with pytest.raises(park.PromiseUsage):
        park.declare_promises(no_promises=True, **flags)


def test_no_declaration_at_all_is_unknown():
    assert park.check_promises(None).word == park.UNKNOWN


# --- all five ---------------------------------------------------------------------------

def test_run_checks_runs_all_five_in_order_even_after_a_failure(tmp_path):
    s = Seat(cwd=str(tmp_path))
    s.shell("make test")
    w = park.World(SID, s.session, s.table, park.Facts(),
                   env={"SESSION_MAIL_ROOT": str(tmp_path / "m"), "SESSIONS_DIR": str(tmp_path)},
                   subscribe=[])
    got = park.run_checks(w, park.declare_promises(no_promises=True))
    assert [c.name for c in got] == list(park.CHECK_NAMES)
    assert [c.word for c in got] == [park.UNKNOWN, park.FAIL, park.PASS, park.PASS, park.PASS]


def test_a_check_that_raises_reads_unknown_naming_the_exception(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(park, "check_claim", boom)
    s = Seat(cwd=str(tmp_path))
    w = park.World(SID, s.session, s.table, park.Facts(), env={}, subscribe=[])
    got = {c.name: c for c in park.run_checks(w, None)}
    assert got["claim"] == park.Check("claim", park.UNKNOWN, "RuntimeError")


# --- reap is unchanged (design §10 test 6) ------------------------------------------------

def test_reap_verdicts_are_byte_identical_with_and_without_a_park_record(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("SESSIONS_DIR", str(sessions))
    root = tmp_path / "store"
    root.mkdir()
    git(root, "init", "-q")
    done_dir, open_dir = task_dir(root, "WEB-1", "completed"), task_dir(root, "WEB-2", "active")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    table = {1: Proc(1, 0, T0, "/sbin/launchd")}
    rows = []
    for i, cwd in enumerate([done_dir, open_dir, tmp_path / "loose"]):
        pid, sid = 5000 + i * 10, f"{5000 + i:08x}-0000-4000-8000-000000000181"
        table[pid] = Proc(pid, 1, T0, f"claude --session-id {sid}")
        table[pid + 1] = Proc(pid + 1, pid, T0 + 1, MONITOR)
        s = cc.Session(id=sid, harness=cc.HARNESS, busy=False, raw_status="idle",
                       updated_at=T0, pid=pid, proc_start=START, cwd=str(cwd))
        rows.append(Row(session=s, repo="store", tmux_socket="default", tmux_pane=f"%{pid}",
                        started=T0))
        (sessions / sid).mkdir(parents=True)
    now = T0 + 3 * 3600
    without = reap.render_json(reap.plan(rows, table, now, tail_check=reap.tail))
    for r in rows:
        park.write(park.Record(r.session.id, now - 60, now - 90, "fixture",
                               tuple(park.Check(n, park.PASS, "x") for n in park.CHECK_NAMES)))
    assert all((sessions / r.session.id / "park.json").is_file() for r in rows)
    with_record = reap.render_json(reap.plan(rows, table, now, tail_check=reap.tail))
    assert with_record == without
    assert '"CLOSE"' in without and '"KEEP"' in without
