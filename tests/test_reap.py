"""`session reap`, the dry run (ST-146 do-001; design.md §2-§4).

Every seat here is FAKE: an injected `listing.Row` and a synthetic process
table, shaped as `tests/test_pane_reading.py` pins them (measured live).
Task trees are tmp directories. Only the tail tests run git, and only in a
scratch repo of their own. Nothing here reads the live fleet or touches tmux.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env

from sessiontools import claude_code as cc
from sessiontools import cli, procs, reap, turn
from sessiontools.listing import Row
from sessiontools.procs import Proc

START = "Sun Dec  8 01:00:00 2030"
T0 = procs.parse_ctime(START)
NOW = T0 + 6 * 3600
HOUR = 3600

PY = "/opt/homebrew/Cellar/python@3.14/3.14.8_2/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"
MONITOR = f"{PY} /Users/alice/.local/bin/pager listen --monitor"


def tool_shell(command: str) -> str:
    """A Bash tool call as Claude Code runs it (the shape test_pane_reading pins)."""
    return ("/opt/homebrew/bin/bash -c source /Users/alice/.claude/shell-snapshots/"
            "snapshot-bash-1907254800000-zh6e7c.sh 2>/dev/null || true && eval '"
            + command + "' < /dev/null && pwd -P >| /tmp/claude-9096-cwd")


@pytest.fixture(autouse=True)
def _hook_off(monkeypatch):
    monkeypatch.delenv(turn.SWITCH_ENV, raising=False)


class Fleet:
    """Fake seats: one Row each, and one process table they all live in."""

    def __init__(self):
        self.table: dict[int, Proc] = {1: Proc(1, 0, T0, "/sbin/launchd")}
        self.rows: list[Row] = []
        self._pid = 5000

    def _next(self) -> int:
        self._pid += 1
        return self._pid

    def seat(self, cwd: Path | str | None, name: str | None = None, *,
             idle: float = 2 * HOUR, status: str = "idle", under: str | None = None,
             live: bool = True, pane: str | None = "auto", socket: str | None = "default",
             turn_ended: bool = False) -> Row:
        """A seat idle IDLE seconds. UNDER puts a tool shell running that
        command under it (`pager listen` is a listener; anything else work)."""
        pid = self._next()
        sid = f"{pid:08x}-0000-4000-8000-000000000146"
        if pane == "auto":                  # a pane of its own: one per seat
            pane = f"%{pid}"
        if live:
            self.table[pid] = Proc(pid, 1, T0, f"claude --session-id {sid}")
            monitor = self._next()
            self.table[monitor] = Proc(monitor, pid, T0 + 1, MONITOR)
            if under is not None:
                shell, work = self._next(), self._next()
                self.table[shell] = Proc(shell, pid, T0 + 2, tool_shell(under))
                self.table[work] = Proc(work, shell, T0 + 2, under)
        busy = status == "busy" and not turn_ended
        s = cc.Session(id=sid, harness=cc.HARNESS, busy=busy, raw_status=status,
                       updated_at=NOW - idle, pid=pid, proc_start=START,
                       cwd=str(cwd) if cwd is not None else None, name=name,
                       turn_ended_at=(NOW - idle) if turn_ended else None)
        row = Row(session=s, repo="store", tmux_socket=socket,
                  tmux_pane=pane, started=T0)
        self.rows.append(row)
        return row

    def plan(self, limit: int = reap.DEFAULT_IDLE_SECONDS, tail=None):
        return reap.plan(self.rows, self.table, NOW, limit,
                         tail_check=tail or (lambda d: (True, "")))


def task_dir(root: Path, task_id: str = "ST-1", status: str = "completed",
             extra: str = "", slices: dict | None = None) -> Path:
    d = root / "projects" / "p" / "tasks" / f"{task_id}_feat_x"
    d.mkdir(parents=True, exist_ok=True)
    (d / "README.md").write_text(f"---\ntask_id: {task_id}\ntitle: x\nstatus: {status}\n"
                                 f"{extra}---\n\n# {task_id}\n")
    if slices is not None:
        (d / "harness").mkdir(exist_ok=True)
        (d / "harness" / "slices.json").write_text(json.dumps(slices))
    return d


def only(verdicts):
    assert len(verdicts) == 1, [v.why for v in verdicts]
    return verdicts[0]


# --- the limit --------------------------------------------------------------------------

@pytest.mark.parametrize("text, seconds", [("45m", 2700), ("1h", 3600), ("90s", 90),
                                           ("1.5h", 5400), (" 2h ", 7200)])
def test_idle_parses_seconds_minutes_and_hours(text, seconds):
    assert reap.parse_idle(text) == seconds


@pytest.mark.parametrize("text", ["45", "45M", "1h30m", "m", "-5m", "0m", "", "3d"])
def test_idle_refuses_anything_else(text):
    with pytest.raises(ValueError):
        reap.parse_idle(text)


def test_the_default_limit_is_one_hour():
    assert reap.DEFAULT_IDLE_SECONDS == 3600


def test_a_seat_idle_under_the_limit_is_not_listed(tmp_path):
    d = task_dir(tmp_path)
    f = Fleet()
    f.seat(d, idle=59 * 60)
    f.seat(d, idle=HOUR)                       # not MORE than the limit
    over = f.seat(d, idle=HOUR + 1)
    assert [v.row for v in f.plan()] == [over]


def test_a_busy_seat_is_never_listed(tmp_path):
    f = Fleet()
    f.seat(task_dir(tmp_path), status="busy", idle=5 * HOUR)
    assert f.plan() == []


def test_another_limit_lists_more(tmp_path):
    f = Fleet()
    f.seat(task_dir(tmp_path), idle=50 * 60)
    assert f.plan() == []
    assert only(f.plan(limit=reap.parse_idle("45m"))).verdict == reap.CLOSE


def test_longest_idle_first(tmp_path):
    d = task_dir(tmp_path)
    f = Fleet()
    a = f.seat(d, idle=2 * HOUR)
    b = f.seat(d, idle=5 * HOUR)
    assert [v.row for v in f.plan()] == [b, a]


# --- the seat: positive evidence it is FREE -----------------------------------------------

def test_a_free_seat_on_a_done_task_closes(tmp_path):
    f = Fleet()
    f.seat(task_dir(tmp_path, "ST-1", "completed"), name="p+st-1.worker.abcd1234")
    v = only(f.plan())
    assert (v.verdict, v.why) == (reap.CLOSE, "task ST-1 is completed; tail clean")
    assert v.reading.word == cc.PANE_FREE


def test_a_free_listener_seat_on_a_done_task_closes(tmp_path):
    f = Fleet()
    f.seat(task_dir(tmp_path), under="pager listen --timeout 1200")
    v = only(f.plan())
    assert v.verdict == reap.CLOSE and v.reading.why == "listener"


@pytest.mark.parametrize("kw, why", [
    ({"status": "waiting"}, "seat BUSY (waiting)"),
    ({"under": "tm task wait ST-1"}, "seat BUSY (work: tm task wait ST-1)"),
    ({"under": "make test"}, "seat BUSY (work: make test)"),
    ({"live": False}, "seat UNKNOWN (no live process)"),
    ({"status": "compacting"}, "seat UNKNOWN (status compacting)"),
    ({"status": "busy", "turn_ended": True}, "seat BUSY (turn)"),      # idle with children
])
def test_every_reading_but_free_is_keep_on_a_done_task(tmp_path, kw, why):
    f = Fleet()
    f.seat(task_dir(tmp_path, status="completed"), **kw)
    v = only(f.plan())
    assert (v.verdict, v.why) == (reap.KEEP, why)


def test_idle_with_children_is_listed_from_the_turns_end_and_kept(tmp_path):
    f = Fleet()
    row = f.seat(task_dir(tmp_path), status="busy", turn_ended=True, idle=3 * HOUR)
    assert row.session.state == cc.IDLE_WITH_CHILDREN
    v = only(f.plan())
    assert v.idle_s == 3 * HOUR and v.verdict == reap.KEEP and v.why == "seat BUSY (turn)"


@pytest.mark.parametrize("pane, socket", [(None, None), ("%3", None)])
def test_a_seat_with_no_pane_is_keep(tmp_path, pane, socket):
    """A pane with no server is no address either: tmux.run reads a None
    socket as the AMBIENT server, where the same %N is another pane."""
    f = Fleet()
    f.seat(task_dir(tmp_path), pane=pane, socket=socket)
    v = only(f.plan())
    assert (v.verdict, v.why) == (reap.KEEP, "no pane (nothing to close by id)")


# --- the task: positive evidence it is DONE ------------------------------------------------

@pytest.mark.parametrize("status", reap.TERMINAL)
def test_each_terminal_status_closes(tmp_path, status):
    f = Fleet()
    f.seat(task_dir(tmp_path, status=status))
    assert only(f.plan()).verdict == reap.CLOSE


@pytest.mark.parametrize("status", ["active", "review", "blocked", "todo", "inbox"])
def test_every_open_status_is_keep_and_named(tmp_path, status):
    f = Fleet()
    f.seat(task_dir(tmp_path, status=status))
    v = only(f.plan())
    assert (v.verdict, v.why) == (reap.KEEP, f"task ST-1 is {status} (not done)")


# README R4: the three waiting-on-the-user shapes, measured live. The
# slate is written beside each README only to show it is not what decides.
WAITING_ON_THE_USER = [
    ("ST-151", "review", {"slices": [{"id": "do-001", "status": "passing"},
                                     {"id": "land-001", "status": "new", "role": "user"}]}),
    ("ST-153", "active", {"slices": [{"id": "do-001", "status": "passing"},
                                     {"id": "verify-001", "status": "passing"}]}),
    ("ST-154", "active", {"slices": [{"id": "do-001", "status": "passing"},
                                     {"id": "do-002", "status": "new", "role": "foreman"}]}),
]


@pytest.mark.parametrize("task_id, status, slices", WAITING_ON_THE_USER)
def test_a_seat_waiting_on_the_user_is_keep_naming_the_status(tmp_path, task_id, status, slices):
    d = task_dir(tmp_path, task_id, status, slices=slices)
    f = Fleet()
    f.seat(d, name=f"sessiontools+{task_id.lower()}.foreman.798e6e18", idle=2 * HOUR)
    v = only(f.plan())
    assert (v.verdict, v.why) == (reap.KEEP, f"task {task_id} is {status} (not done)")


def test_hatched_is_off_review_with_every_delivery_landed_is_keep(tmp_path):
    d = task_dir(tmp_path, status="review",
                 extra="deliveries:\n- sha: 'abc'\n  stage: landed\n")
    f = Fleet()
    f.seat(d)
    v = only(f.plan())
    assert reap.HATCHED_CLOSES is False
    assert v.verdict == reap.KEEP
    assert v.why == "task ST-1 is review (not done; hatched does not count as done yet)"


def test_a_task_with_no_status_is_keep(tmp_path):
    d = tmp_path / "T"
    d.mkdir()
    (d / "README.md").write_text("---\ntask_id: ST-9\n---\nbody\n")
    f = Fleet()
    f.seat(d)
    assert only(f.plan()).why == "task ST-9 is no status (not done)"


def test_a_status_yaml_reads_as_a_date_is_keep_named_and_still_renders(tmp_path):
    """`status: 2030-12-08` is a date to YAML. It is not done, the reason
    names it as written, and --json does not crash on it (it did, at 4e1924f)."""
    d = tmp_path / "T"
    d.mkdir()
    (d / "README.md").write_text("---\ntask_id: ST-9\nstatus: 2030-12-08\n---\nbody\n")
    f = Fleet()
    f.seat(d)
    verdicts = f.plan()
    assert only(verdicts).why == "task ST-9 is 2030-12-08 (not done)"
    [row] = json.loads(reap.render_json(verdicts))
    assert (row["verdict"], row["task_status"]) == ("KEEP", "2030-12-08")


def test_a_seat_with_no_task_is_keep(tmp_path):
    (tmp_path / "README.md").write_text("# a project, no front matter\n")
    f = Fleet()
    f.seat(tmp_path, name="sessionmail+super.3987c186")
    f.seat(None)
    for v in f.plan():
        assert (v.verdict, v.why) == (reap.KEEP, "no task (a seat with no task is never closed)")


def test_a_worker_in_the_worktree_finds_its_task(tmp_path):
    d = task_dir(tmp_path)
    (d / "worktree" / "src").mkdir(parents=True)
    (d / "worktree" / "README.md").write_text("# the code repo's README\n")
    f = Fleet()
    f.seat(d / "worktree" / "src")
    v = only(f.plan())
    assert v.task.task_id == "ST-1" and v.task.dir == d and v.verdict == reap.CLOSE


def test_front_matter_without_task_id_is_walked_past(tmp_path):
    d = task_dir(tmp_path)
    inner = d / "docs"
    inner.mkdir()
    (inner / "README.md").write_text("---\ntitle: docs\n---\n")
    assert reap.find_task(str(inner))[0].task_id == "ST-1"


@pytest.mark.parametrize("text", [
    "---\ntask_id: ST-1\nstatus: [completed\n---\n",          # YAML that does not parse
    "---\n- a list\n---\n",                                  # not a mapping
    "---\ntask_id: ST-1\nstatus: completed\n",               # never closed
])
def test_an_unparsable_readme_is_keep_and_stops_the_walk(tmp_path, text):
    parent = task_dir(tmp_path, "ST-0", "completed")         # a DONE task above it
    d = parent / "child"
    d.mkdir()
    (d / "README.md").write_text(text)
    f = Fleet()
    f.seat(d)
    v = only(f.plan())
    assert v.verdict == reap.KEEP
    assert v.why.startswith("task README unreadable (") and v.task is None


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="NOT TESTED as root: root searches a mode 000 folder, so none can be built")
def test_a_readme_that_cannot_be_stat_ed_stops_the_walk(tmp_path):
    """From 3.14 `Path.is_file()` returns False for a README it cannot stat;
    3.11 to 3.13 raised. Either way the walk stops: never a parent task."""
    parent = task_dir(tmp_path, "ST-0", "completed")         # a DONE task above it
    deep = parent / "sub" / "deep"
    deep.mkdir(parents=True)
    (parent / "sub").chmod(0o000)
    try:
        got = reap.find_task(str(deep))
    finally:
        (parent / "sub").chmod(0o700)
    assert got == (None, f"task README unreadable ({deep / 'README.md'}: PermissionError)")


# --- the name token: fail-closed (design.md §8, 12:34 and 13:34) -------------------------

@pytest.mark.parametrize("name, token", [
    ("sessiontools+st-151.foreman.798e6e18", "st-151"),
    ("sessionmail+super.3987c186", "super"),
    ("acme+gc", "gc"),
    ("alice", None),
    (None, None),
])
def test_the_token_is_between_the_first_plus_and_the_next_dot(name, token):
    assert reap.name_token(name) == token


@pytest.mark.parametrize("name, says", [
    ("sessiontools+super.3987c186", "super"),
    ("acme+gc.11112222", "gc"),
    ("sessiontools+st-2.worker.abcd1234", "st-2"),
])
def test_a_name_that_names_another_task_is_keep_on_a_done_task(tmp_path, name, says):
    f = Fleet()
    f.seat(task_dir(tmp_path, "ST-1", "completed"), name=name)
    v = only(f.plan())
    assert (v.verdict, v.why) == (reap.KEEP, f"name says {says}, cwd says ST-1")


def test_the_token_compares_case_insensitively(tmp_path):
    f = Fleet()
    f.seat(task_dir(tmp_path, "ST-1", "completed"), name="p+ST-1.worker.x")
    assert only(f.plan()).verdict == reap.CLOSE


def test_a_name_with_no_token_leaves_cwd_to_decide(tmp_path):
    """The 12:34 rule as written; design.md §8's no-token item is undecided."""
    f = Fleet()
    f.seat(task_dir(tmp_path, "ST-1", "completed"), name="alice-talk")
    assert only(f.plan()).verdict == reap.CLOSE


# --- the tail: no unsaved work, and no lock taken ----------------------------------------

def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """The fixtures' git, hermetic (ST-161): the caller's config never reaches it."""
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *HOOKS_OFF, *args], cwd=cwd,
                          capture_output=True, text=True, check=True, env=hermetic_env())


@pytest.fixture
def store(tmp_path):
    """A scratch store repo holding one completed task, committed clean."""
    root = tmp_path / "store"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("**/worktree/\n")
    d = task_dir(root, "ST-1", "completed")
    (d / "notes.md").write_text("notes\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    return d


def test_a_clean_tail_closes(store):
    f = Fleet()
    f.seat(store)
    v = only(f.plan(tail=reap.tail))
    assert (v.verdict, v.why) == (reap.CLOSE, "task ST-1 is completed; tail clean")


def test_a_dirty_store_is_keep_naming_the_paths(store):
    (store / "notes.md").write_text("changed\n")
    (store / "new.md").write_text("new\n")
    f = Fleet()
    f.seat(store)
    v = only(f.plan(tail=reap.tail))
    assert v.verdict == reap.KEEP
    assert v.why.startswith("task ST-1 is completed; store dirty: ")
    assert "notes.md" in v.why and "new.md" in v.why


def test_dirt_elsewhere_in_the_store_is_not_this_tasks(store):
    other = task_dir(store.parents[3], "ST-2", "active")
    (other / "scratch.md").write_text("x\n")
    f = Fleet()
    f.seat(store)
    assert only(f.plan(tail=reap.tail)).verdict == reap.CLOSE


def test_a_dirty_worktree_is_keep(store):
    wt = store / "worktree"
    wt.mkdir()
    git(wt, "init", "-q")
    (wt / "a.py").write_text("x = 1\n")
    f = Fleet()
    f.seat(store)
    v = only(f.plan(tail=reap.tail))
    assert v.verdict == reap.KEEP and "worktree dirty: ?? a.py" in v.why


@pytest.mark.parametrize("where", ["store", "worktree"])
def test_config_that_hides_untracked_files_cannot_make_a_tail_clean(store, where):
    """`status.showUntrackedFiles=no` drops new files from a plain
    --porcelain: at 1fa6e95 an unsaved new file read "tail clean" and CLOSE.
    The tail pins --untracked-files=normal, so config can never hide it."""
    root = store.parents[3]
    if where == "worktree":
        root = store / "worktree"
        root.mkdir()
        git(root, "init", "-q")
        (root / "unsaved.py").write_text("x = 1\n")
    else:
        (store / "unsaved.md").write_text("draft\n")
    git(root, "config", "status.showUntrackedFiles", "no")
    assert git(root, "status", "--porcelain").stdout == ""   # the control: hidden
    f = Fleet()
    f.seat(store)
    v = only(f.plan(tail=reap.tail))
    assert v.verdict == reap.KEEP
    assert f"{where} dirty: ?? " in v.why and "unsaved." in v.why


def test_a_task_outside_any_repo_is_unreadable_and_keep(tmp_path):
    f = Fleet()
    f.seat(task_dir(tmp_path / "loose"))
    v = only(f.plan(tail=reap.tail))
    assert v.verdict == reap.KEEP and "store unreadable (git exit" in v.why


def test_many_dirty_paths_are_counted_not_listed(tmp_path):
    out = "".join(f"?? f{i}.md\n" for i in range(5))
    def fake(args, cwd):
        return subprocess.CompletedProcess(args, 0, out, "")
    clean, detail = reap.tail(tmp_path, git=fake)
    assert not clean and detail.endswith("+2 more") and "f3.md" not in detail


def test_a_dry_run_leaves_the_store_index_unchanged(store):
    """design.md §5.2: a plain `git status` rewrites a stale index under
    index.lock; the tail check must not. A stale stat is made by moving a
    tracked file's mtime, the content unchanged."""
    root = store.parents[3]
    index = root / ".git" / "index"
    old = time.time() - 1000
    os.utime(store / "notes.md", (old, old))
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    f = Fleet()
    f.seat(store)
    assert only(f.plan(tail=reap.tail)).verdict == reap.CLOSE
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    assert not (root / ".git" / "index.lock").exists()
    git(root, "status", "--porcelain")      # the control: a plain status DOES rewrite it
    assert index.read_bytes() != before[0]


def test_the_tail_runs_git_with_no_optional_locks(tmp_path):
    seen = []
    def fake(args, cwd):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    reap.tail(tmp_path, git=fake)
    assert seen == [["status", "--porcelain", "--untracked-files=normal",
                     "--ignore-submodules=none", "--", "."]]
    import inspect
    src = inspect.getsource(reap._git)
    assert '"--no-optional-locks"' in src and '"GIT_OPTIONAL_LOCKS": "0"' in src


# --- render and the verb -----------------------------------------------------------------

def test_the_table_names_each_verdict_and_counts_them(tmp_path):
    f = Fleet()
    f.seat(task_dir(tmp_path / "a", "ST-1", "completed"), name="p+st-1.worker.aaaa1111")
    f.seat(task_dir(tmp_path / "b", "ST-2", "active"), name="p+st-2.foreman.bbbb2222",
           idle=3 * HOUR)
    text = reap.render_table(f.plan(), reap.DEFAULT_IDLE_SECONDS)
    lines = text.splitlines()
    assert lines[0].split() == ["SHORT", "NAME", "IDLE", "VERDICT", "WHY"]
    assert "3h00m  KEEP     task ST-2 is active (not done)" in lines[1]
    assert "2h00m  CLOSE    task ST-1 is completed; tail clean" in lines[2]
    assert lines[-1] == "dry run: 1 would close, 1 kept. Nothing was done."


def test_no_seat_past_the_limit_says_so():
    assert reap.render_table([], 2700) == "No seat idle past 45m. dry run: nothing was done."


def test_json_carries_the_verdict_the_reason_and_the_task(tmp_path):
    d = task_dir(tmp_path)
    f = Fleet()
    f.seat(d, name="p+st-1.worker.aaaa1111")
    [row] = json.loads(reap.render_json(f.plan()))
    assert row["verdict"] == "CLOSE" and row["why"] == "task ST-1 is completed; tail clean"
    assert (row["task_id"], row["task_status"], row["task_dir"]) == ("ST-1", "completed", str(d))
    assert row["pane_signal"] == {"word": "FREE", "why": "nothing live"}
    assert row["idle_s"] == 2 * HOUR and row["tmux_pane"] == f"%{row['pid']}"


def run(argv, fleet, capsys):
    rc = reap.main(argv, rows=lambda: fleet.rows, table=lambda: fleet.table, now=NOW,
                   listeners=lambda: cc.DEFAULT_LISTENERS)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_the_verb_is_a_dry_run_with_exit_0(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(reap, "tail", lambda d, git=None: (True, ""))
    f = Fleet()
    f.seat(task_dir(tmp_path), idle=50 * 60)
    rc, out, _ = run(["--idle", "45m"], f, capsys)
    assert rc == 0 and "CLOSE" in out and out.rstrip().endswith("Nothing was done.")
    rc, out, _ = run(["--json"], f, capsys)
    assert rc == 0 and json.loads(out) == []


def test_a_bad_idle_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        reap.main(["--idle", "45"], rows=list, table=dict, now=NOW, listeners=tuple)
    assert e.value.code == 2 and "--idle" in capsys.readouterr().err


def test_reap_is_a_session_verb_with_help(capsys):
    assert "reap" in cli.HANDLERS
    assert any(v == "reap" for v, _ in cli.VERBS)
    with pytest.raises(SystemExit) as e:
        cli.main(["reap", "--help"])
    out = capsys.readouterr().out
    assert e.value.code == 0
    for word in ("--idle", "--json", "--yes", "FREE", "completed", "no-optional-locks"):
        assert word in out


def test_session_reap_runs_end_to_end_on_an_empty_fleet(capsys):
    """conftest walls the sidecar folder off: no live seat, nothing listed."""
    assert cli.main(["reap"]) == 0
    assert capsys.readouterr().out.startswith("No seat idle past 1h00m.")


def test_the_cli_does_not_import_reap_or_yaml():
    """reap reads front matter through start.agentfile (yaml): imported by
    its handler only, so `import sessiontools.cli` loads neither."""
    import sys
    probe = ("import sys; import sessiontools.cli; "
             "print('sessiontools.reap' in sys.modules, 'yaml' in sys.modules, "
             "'jsonschema' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True).stdout
    assert out.split() == ["False", "False", "False"]


def test_importing_reap_alone_loads_no_yaml():
    import sys
    probe = "import sys; import sessiontools.reap; print('yaml' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True).stdout
    assert out.strip() == "False"
