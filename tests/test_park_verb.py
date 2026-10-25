"""`session park` and `session park --check [SEAT]`: the verb (park.py, design §6, §7).

Every seat here is FAKE and lives in a temp tree: its sidecar row, its
process table, its transcript, its session dir and mail root, and a scratch
store whose task holds a worktree repo. The verb's seams (env, rows, table,
now, listeners, own_pid) carry them in; nothing reads the live fleet, a
live mailbox or live config, and nothing is mailed or killed.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import ANCHOR_SUMMER
from git_support import HOOKS_OFF, hermetic_env

from sessiontools import claude_code as cc
from sessiontools import cli, park, procs, window
from sessiontools.listing import Row
from sessiontools.procs import Proc

SID = "5e1f0c2a-0000-4000-8000-0000000000dd"
OTHER = "9a9a9a9a-0000-4000-8000-0000000000ee"
START = "Sun Dec  8 01:00:00 2030"
T0 = procs.parse_ctime(START)
PARK_AT = T0 + 100.0
# The far-future --keep-until sentinel: relative to the REAL now, never a literal year.
FAR_FUTURE = (datetime.now(UTC) + timedelta(days=3650)).replace(microsecond=0)
FAR_Z = f"{FAR_FUTURE:%Y-%m-%dT%H:%M:%SZ}"
# The past one: relative to the INJECTED now (PARK_AT), so it is past for good.
PAST = datetime.fromtimestamp(PARK_AT, UTC).replace(microsecond=0) - timedelta(days=3650)
PY = "/opt/homebrew/bin/python3.14"
MONITOR = f"{PY} /Users/u/.local/bin/pager listen --monitor"


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def use(at: float, call_id: str, command: str) -> dict:
    return {"type": "assistant", "timestamp": iso(at),
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}}


def result(at: float, call_id: str) -> dict:
    return {"type": "user", "timestamp": iso(at),
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call_id, "content": "ok"}]}}


def human(at: float) -> dict:
    return {"type": "user", "timestamp": iso(at), "origin": {"kind": "human"},
            "message": {"role": "user", "content": "one more thing"}}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "commit.gpgsign=false", *HOOKS_OFF, *args], cwd=cwd,
                   capture_output=True, text=True, check=True, env=hermetic_env())


def tool_shell(command: str) -> str:
    return ("/opt/homebrew/bin/bash -c source /Users/u/.claude/shell-snapshots/"
            "snapshot-bash-1.sh 2>/dev/null || true && eval '" + command + "' < /dev/null")


class Home:
    """A temp world for one seat (SID) and, beside it, another (OTHER)."""

    def __init__(self, root: Path):
        self.root = root
        self.sessions = root / "sessions"
        self.mail = root / "mail"
        (self.sessions / SID).mkdir(parents=True)
        (self.sessions / OTHER).mkdir(parents=True)
        store = root / "store"
        store.mkdir()
        git(store, "init", "-q")
        (store / ".gitignore").write_text("**/worktree/\n")
        self.task = store / "projects" / "p" / "tasks" / "WEB-9_feat_x"
        self.task.mkdir(parents=True)
        (self.task / "README.md").write_text("---\ntask_id: WEB-9\ntitle: x\nstatus: active\n---\n")
        git(store, "add", "-A")
        git(store, "commit", "-q", "-m", "seed")
        wt = self.task / "worktree"
        wt.mkdir()
        git(wt, "init", "-q")
        git(wt, "commit", "-q", "--allow-empty", "-m", "seed")
        self.env = {"CLAUDE_CODE_SESSION_ID": SID, "SESSIONS_DIR": str(self.sessions),
                    "SESSION_MAIL_ROOT": str(self.mail),
                    "CLAUDE_PROJECTS_DIR": str(root / "projects"),
                    "SESSION_SIDECAR_DIR": str(root / "sidecars"), "HOME": str(root / "home")}
        self.table: dict[int, Proc] = {1: Proc(1, 0, T0, "/sbin/launchd")}
        self.rows: list[Row] = []
        self.pids: dict[str, int] = {}
        self.lines: dict[str, list[dict]] = {}
        for i, sid in enumerate((SID, OTHER)):
            pid = 4000 + i * 1000
            self.pids[sid] = pid
            self.table[pid] = Proc(pid, 1, T0, f"claude --session-id {sid}")
            self.table[pid + 1] = Proc(pid + 1, pid, T0 + 1, MONITOR)
            s = cc.Session(id=sid, harness=cc.HARNESS, busy=True, raw_status="busy",
                           updated_at=T0 + 10, pid=pid, proc_start=START, cwd=str(self.task),
                           name=f"p+web-9.{sid[:8]}", started_at=T0)
            self.rows.append(Row(session=s, repo="store", tmux_socket="default",
                                 tmux_pane=f"%{pid}", started=T0))
            # A settle turn: a commit, then the park's own call (no result yet).
            self.lines[sid] = [use(T0 + 50, "toolu_c", "git commit -m x"), result(T0 + 51, "toolu_c"),
                               use(T0 + 99, "toolu_p", "session park --no-promises")]
        self._next = 4100
        self.err = ""

    def transcript(self, sid: str = SID, *extra: dict) -> None:
        d = Path(self.env["CLAUDE_PROJECTS_DIR"]) / window.encode_dir(str(self.task))
        d.mkdir(parents=True, exist_ok=True)
        lines = self.lines[sid] + list(extra)
        (d / f"{sid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    def shell(self, command: str, sid: str = SID) -> int:
        """A tool shell under SID's claude process running COMMAND; its child's pid."""
        shell = self._next
        self._next += 10
        self.table[shell] = Proc(shell, self.pids[sid], T0 + 2, tool_shell(command))
        self.table[shell + 1] = Proc(shell + 1, shell, T0 + 2, command)
        return shell + 1

    def run(self, *argv: str, now: float = PARK_AT, own_pid: int = 999_999,
            env: dict | None = None, capsys=None) -> tuple[int, str]:
        for sid in (SID, OTHER):
            self.transcript(sid)
        code = park.main(list(argv), env=self.env if env is None else env,
                         rows=lambda: self.rows, table=lambda: self.table, now=now,
                         listeners=lambda: cc.DEFAULT_LISTENERS, own_pid=own_pid)
        out, self.err = capsys.readouterr() if capsys is not None else ("", "")
        return code, out

    @property
    def record(self) -> Path:
        return self.sessions / SID / "park.json"


@pytest.fixture
def home(tmp_path) -> Home:
    return Home(tmp_path)


def run(home: Home, capsys, *argv: str, **kw) -> tuple[int, str]:
    return home.run(*argv, capsys=capsys, **kw)


def lines_of(text: str) -> dict[str, str]:
    """`name -> word` for each check line of a text report."""
    rows = {}
    for ln in text.splitlines():
        parts = ln.split()
        if ln.startswith("  ") and parts and parts[0] in park.CHECK_NAMES:
            rows[parts[0]] = parts[1]
    return rows


# --- design §10 test 1, and the end-to-end temp-HOME run ---------------------------------

def test_park_then_check_reads_parked_and_clean_and_both_exit_0(home, capsys):
    code, out = run(home, capsys, "--no-promises", "--letter", "<l@h>")
    assert code == park.EXIT_CLEAN, out
    assert out.startswith("parked: yes, at ")
    assert "work ended" in out and "last work: Bash git commit -m x" in out
    assert "clean: yes" in out
    data = json.loads(home.record.read_text())
    assert data["clean"] is True and data["letter"] == "<l@h>"
    assert data["work_ended_at"] == pytest.approx(T0 + 51)
    assert data["parked_at"] == PARK_AT
    assert [c["word"] for c in data["checks"]] == [park.PASS] * 5

    code, out = run(home, capsys, "--check", now=PARK_AT + 5)
    assert code == park.EXIT_CLEAN, out
    assert out.splitlines()[1] == "clean: yes"
    assert out.startswith("parked: yes, at ")
    assert set(lines_of(out).values()) == {park.PASS}


def test_the_cli_runs_the_verb(home, monkeypatch, capsys):
    """`session park` reaches park.main through cli.main, and `--help` exits 0."""
    assert "park" in dict(cli.VERBS)
    with pytest.raises(SystemExit) as e:
        cli.main(["park", "--help"])
    assert e.value.code == 0
    help_text = capsys.readouterr().out
    for code in ("0  parked, clean", "1  parked, not clean", "3  --check only: not parked",
                 "4  park only: the write failed"):
        assert code in help_text
    monkeypatch.setattr(park, "main", lambda argv: 42 if argv == ["--check"] else 0)
    assert cli.main(["park", "--check"]) == 42


# --- the exit codes (design §6), each pinned -------------------------------------------------

def test_a_park_that_is_not_clean_still_writes_and_exits_1(home, capsys):
    code, out = run(home, capsys, "--promise", "the verdict at six")
    assert code == park.EXIT_NOT_CLEAN
    assert "clean: no" in out and lines_of(out)["promises"] == park.FAIL
    assert json.loads(home.record.read_text())["clean"] is False
    code, out = run(home, capsys, "--check", now=PARK_AT + 5)
    assert code == park.EXIT_NOT_CLEAN and out.startswith("parked: yes")


def test_check_names_the_failing_check(home, capsys):
    run(home, capsys, "--no-promises")
    home.shell("make test")
    code, out = run(home, capsys, "--check", now=PARK_AT + 5)
    assert code == park.EXIT_NOT_CLEAN
    assert lines_of(out)["work"] == park.FAIL and "work: make test" in out


def test_never_parked_is_exit_3(home, capsys):
    code, out = run(home, capsys, "--check")
    assert code == park.EXIT_NOT_PARKED
    assert out.startswith("parked: no (never parked)")
    assert set(lines_of(out)) == set(park.CHECK_NAMES)     # the live checks are still shown


def test_a_turn_after_the_park_is_exit_3_and_names_it(home, capsys):
    """design §10 test 4's exit code (decompose review, note A)."""
    run(home, capsys, "--no-promises")
    home.lines[SID].append(human(PARK_AT + 60))
    code, out = run(home, capsys, "--check", now=PARK_AT + 70)
    assert code == park.EXIT_NOT_PARKED
    assert out.startswith("parked: no (a turn started at ") and "(human)" in out
    code, out = run(home, capsys, "--check", "--json", now=PARK_AT + 70)
    data = json.loads(out)
    assert code == park.EXIT_NOT_PARKED and data["parked"] is False
    assert data["turn"] == {"kind": "turn", "at": pytest.approx(PARK_AT + 60), "origin": "human"}


def test_work_after_the_park_is_exit_3(home, capsys):
    run(home, capsys, "--no-promises")
    home.lines[SID] += [use(PARK_AT + 30, "toolu_w", "git push"), result(PARK_AT + 31, "toolu_w")]
    code, out = run(home, capsys, "--check", now=PARK_AT + 40)
    assert code == park.EXIT_NOT_PARKED and "work after the park" in out and "git push" in out


def test_an_unreadable_record_is_exit_3(home, capsys):
    home.record.write_text("{not json")
    code, out = run(home, capsys, "--check")
    assert code == park.EXIT_NOT_PARKED and out.startswith("parked: no (park.json unreadable")


def test_a_failed_write_is_exit_4_and_leaves_nothing(home, capsys, monkeypatch):
    """design §10 test 11's exit code (decompose review, note A)."""
    def boom(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(park.os, "replace", boom)
    code, _out = run(home, capsys, "--no-promises")
    assert code == park.EXIT_WRITE_FAILED
    assert sorted(p.name for p in (home.sessions / SID).iterdir()) == []
    assert "the write failed" in home.err and "nothing written" in home.err
    assert not any(p.name.startswith(".park.json") for p in (home.sessions / SID).iterdir())


def _crash(*_a, **_k):
    raise RuntimeError("boom")


@pytest.mark.parametrize("argv,where", [
    (["--check", OTHER[:8]], "report"),
    (["--check"], "report"),
    (["--check", "--json"], "render_json"),
    (["--no-promises"], "run_checks"),
])
def test_a_crash_is_exit_2_never_a_verdict(home, capsys, monkeypatch, argv, where):
    """Python's traceback exit is 1, which --check documents as "parked, not
    clean": a scripted caller would read a crash as a park. It exits 2."""
    run(home, capsys, "--no-promises")
    before = home.record.read_bytes()
    monkeypatch.setattr(park, where, _crash)
    code, out = run(home, capsys, *argv, now=PARK_AT + 5)
    assert code == park.EXIT_USAGE and out == ""
    assert "internal error (RuntimeError: boom): no verdict" in home.err
    assert home.record.read_bytes() == before


def test_another_seat_whose_listing_fails_is_exit_2(home, capsys):
    """`--check SEAT` reads `rows()` unguarded: its failure is no verdict."""
    def rows():
        raise OSError("no listing")
    code = park.main(["--check", OTHER[:8]], env=home.env, rows=rows,
                     table=lambda: home.table, now=PARK_AT,
                     listeners=lambda: cc.DEFAULT_LISTENERS, own_pid=999_999)
    assert code == park.EXIT_USAGE
    assert "internal error (OSError: no listing)" in capsys.readouterr().err


def test_no_session_id_is_exit_2_and_writes_nothing(home, capsys):
    env = {k: v for k, v in home.env.items() if k != "CLAUDE_CODE_SESSION_ID"}
    assert run(home, capsys, "--no-promises", env=env)[0] == park.EXIT_USAGE
    assert run(home, capsys, "--check", env=env)[0] == park.EXIT_USAGE
    assert not home.record.exists()


def test_no_session_dir_is_exit_2_and_makes_none(home, capsys):
    (home.sessions / SID).rmdir()
    assert run(home, capsys, "--no-promises")[0] == park.EXIT_USAGE
    assert not (home.sessions / SID).exists()


def test_no_such_live_seat_is_exit_2(home, capsys):
    code, _out = run(home, capsys, "--check", "nobody-by-that-name")
    assert code == park.EXIT_USAGE
    assert "no live seat matches 'nobody-by-that-name'" in home.err


@pytest.mark.parametrize("argv", [
    ["--check", "--letter", "x"],
    ["--check", "--no-promises"],
    ["--check", "--promise", "x"],
    ["--check", "--keep-until", FAR_Z, "--why", "x"],
    ["--no-promises", "--promise", "x"],
    ["--no-promises", "--handed-on", "x"],
    ["--why", "no keep-until"],
])
def test_usage_errors_exit_2_and_write_nothing(home, capsys, argv):
    with pytest.raises(SystemExit) as e:
        run(home, capsys, *argv)
    assert e.value.code == park.EXIT_USAGE
    assert not home.record.exists()


# --- design §10 test 7: --json matches the text ----------------------------------------------

FIXTURES = {
    "clean": lambda h: None,
    "dirty tree": lambda h: (h.task / "worktree" / "new.txt").write_text("x"),
    "work": lambda h: h.shell("make test"),
    "claim": lambda h: ((h.task / "harness" / ".claim").mkdir(parents=True),
                        (h.task / "harness" / ".claim" / "do-1.json").write_text(json.dumps(
                            {"slice_id": "do-1", "session_id": SID, "released_at": None}))),
    "mail": lambda h: ([(h.mail / SID / d).mkdir(parents=True) for d in ("new", "cur", "tmp")],
                       (h.mail / SID / "new" / "1.M1.h").write_text("Message-ID: <u@h>\n\nhi\n")),
    "turn after": lambda h: h.lines[SID].append(human(PARK_AT + 60)),
}


@pytest.mark.parametrize("fixture", list(FIXTURES))
@pytest.mark.parametrize("parked", [True, False])
def test_json_and_text_agree_on_the_verdict_and_the_exit_code(home, capsys, fixture, parked):
    if parked:
        run(home, capsys, "--no-promises")
    FIXTURES[fixture](home)
    code_text, text = run(home, capsys, "--check", now=PARK_AT + 70)
    code_json, raw = run(home, capsys, "--check", "--json", now=PARK_AT + 70)
    data = json.loads(raw)
    assert code_text == code_json
    assert text.startswith("parked: yes") is data["parked"]
    assert ("clean: yes" in text) is data["clean"]
    assert lines_of(text) == {c["name"]: c["word"] for c in data["checks"]}
    assert (data["record"] is not None) is parked
    if fixture != "clean":
        assert code_text != park.EXIT_CLEAN


def test_a_parks_json_matches_its_text(home, capsys):
    code_json, raw = run(home, capsys, "--promise", "a", "--json")
    data = json.loads(raw)
    assert code_json == park.EXIT_NOT_CLEAN and data["parked"] is True and data["clean"] is False
    assert data["record"]["promises"] == {"declared": "open", "open": ["a"], "handed_on": None}
    assert data["turn"] is None and data["why_not_parked"] is None


# --- design §10 test 8: park writes only park.json; --check writes nothing ----------------------

def snapshot(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[os.path.relpath(os.path.join(dirpath, d), root)] = None
        for f in filenames:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


def test_park_writes_only_its_record_and_check_writes_nothing(home, capsys):
    for sid in (SID, OTHER):
        home.transcript(sid)
    before = snapshot(home.root)
    run(home, capsys, "--no-promises", "--keep-until", FAR_Z, "--why", "x")
    after = snapshot(home.root)
    added = set(after) - set(before)
    changed = {k for k in before if k in after and before[k] != after[k]}
    assert added == {os.path.join("sessions", SID, "park.json")}
    assert changed == set() and set(before) <= set(after)
    for argv in (["--check"], ["--check", "--json"], ["--check", OTHER[:8]]):
        run(home, capsys, *argv, now=PARK_AT + 5)
        assert snapshot(home.root) == after, argv


# --- design §10 test 12: keep-until -----------------------------------------------------------

def test_keep_until_is_written_and_shown(home, capsys):
    t = "2030-12-08T17:00:00+00:00"
    code, out = run(home, capsys, "--no-promises", "--keep-until", t, "--why", "the verdict",
                    now=datetime.fromisoformat(t).timestamp() - 3600)
    assert code == park.EXIT_CLEAN, out
    data = json.loads(home.record.read_text())
    assert data["keep_until"] == datetime.fromisoformat(t).timestamp()
    assert data["keep_why"] == "the verdict"
    assert "keep until: " in out and "(why: the verdict)" in out
    assert "deadline" not in json.dumps(data)


def test_keep_until_takes_a_z(home, capsys):
    run(home, capsys, "--no-promises", "--keep-until", FAR_Z, "--why", "x")
    assert json.loads(home.record.read_text())["keep_until"] == pytest.approx(FAR_FUTURE.timestamp())


@pytest.mark.parametrize("argv", [
    ["--keep-until", PAST.isoformat(), "--why", "past"],
    ["--keep-until", f"{FAR_FUTURE:%Y-%m-%dT%H:%M:%S}", "--why", "no offset"],
    ["--keep-until", f"{FAR_FUTURE:%Y-%m-%d}", "--why", "a date, no offset"],
    ["--keep-until", "+2h", "--why", "relative"],
    ["--keep-until", "in two hours", "--why", "relative"],
    ["--keep-until", FAR_Z],
    ["--keep-until", FAR_Z, "--why", "  "],
])
def test_a_bad_keep_until_is_exit_2_and_writes_nothing(home, capsys, argv):
    with pytest.raises(SystemExit) as e:
        run(home, capsys, "--no-promises", *argv)
    assert e.value.code == park.EXIT_USAGE
    assert not home.record.exists()


def test_a_re_park_without_keep_until_clears_it(home, capsys):
    run(home, capsys, "--no-promises", "--keep-until", FAR_Z, "--why", "x")
    assert json.loads(home.record.read_text())["keep_until"] is not None
    run(home, capsys, "--no-promises", now=PARK_AT + 1)
    data = json.loads(home.record.read_text())
    assert data["keep_until"] is None and data["keep_why"] is None


def test_keep_until_never_moves_work_ended_at(home, capsys):
    run(home, capsys, "--no-promises")
    first = json.loads(home.record.read_text())["work_ended_at"]
    run(home, capsys, "--no-promises", "--keep-until", FAR_Z, "--why", "x",
        now=PARK_AT + 1)
    assert json.loads(home.record.read_text())["work_ended_at"] == first


# --- parking buys no time, through the verb (R5) -------------------------------------------------

def test_a_re_park_with_no_work_since_keeps_work_ended_at(home, capsys):
    run(home, capsys, "--no-promises")
    first = json.loads(home.record.read_text())
    home.lines[SID] += [result(PARK_AT + 1, "toolu_p"), use(PARK_AT + 20, "toolu_p2",
                                                            "session park --no-promises")]
    run(home, capsys, "--no-promises", now=PARK_AT + 21)
    again = json.loads(home.record.read_text())
    assert again["work_ended_at"] == first["work_ended_at"]
    assert again["work_ended_why"].startswith("carried from the park at ")
    assert again["parked_at"] == PARK_AT + 21


# --- another seat, read from here --------------------------------------------------------------

def test_own_tool_shell_is_skipped_only_for_the_own_seat(home, capsys):
    """The own call (`session park --check`) is not work; read from another
    seat, the same shell is."""
    run(home, capsys, "--no-promises")
    me = home.shell("session park --check")
    code, out = run(home, capsys, "--check", own_pid=me, now=PARK_AT + 5)
    assert code == park.EXIT_CLEAN, out
    env = {**home.env, "CLAUDE_CODE_SESSION_ID": OTHER}
    code, out = run(home, capsys, "--check", SID[:8], own_pid=me, env=env, now=PARK_AT + 5)
    assert lines_of(out)["work"] == park.FAIL, out


def test_another_seat_reads_its_record_and_names_it(home, capsys):
    run(home, capsys, "--no-promises")
    env = {**home.env, "CLAUDE_CODE_SESSION_ID": OTHER}
    code, raw = run(home, capsys, "--check", SID[:8], "--json", env=env, now=PARK_AT + 5)
    data = json.loads(raw)
    assert code == park.EXIT_CLEAN and data["session_id"] == SID
    assert data["name"] == f"p+web-9.{SID[:8]}"


@pytest.mark.parametrize("order", [
    ["--check", SID[:8], "--json"],
    ["--check", "--json", SID[:8]],
    ["--json", "--check", SID[:8]],
    [SID[:8], "--check", "--json"],
])
def test_check_seat_and_json_parse_in_any_order(home, capsys, order):
    """`--check --json SEAT` was refused (SEAT left unclaimed by --check's
    optional value); every order of the three now reads the same seat."""
    run(home, capsys, "--no-promises")
    env = {**home.env, "CLAUDE_CODE_SESSION_ID": OTHER}
    code, raw = run(home, capsys, *order, env=env, now=PARK_AT + 5)
    data = json.loads(raw)
    assert code == park.EXIT_CLEAN and data["session_id"] == SID and data["parked"] is True


def test_a_seat_without_check_is_exit_2_and_writes_nothing(home, capsys):
    with pytest.raises(SystemExit) as e:
        run(home, capsys, "--no-promises", OTHER[:8])
    assert e.value.code == park.EXIT_USAGE
    assert not home.record.exists() and not (home.sessions / OTHER / "park.json").exists()


def test_another_seats_mail_pass_needs_its_parks_mail_pass(home, capsys):
    """Its subscriptions are in its environment, not ours: with no park to
    prove it had none, a live mail PASS is UNKNOWN."""
    env = {**home.env, "CLAUDE_CODE_SESSION_ID": OTHER}
    _code, out = run(home, capsys, "--check", SID[:8], env=env)
    assert lines_of(out)["mail"] == park.UNKNOWN and "subscriptions not known" in out
    run(home, capsys, "--no-promises")
    _code, out = run(home, capsys, "--check", SID[:8], env=env, now=PARK_AT + 5)
    assert lines_of(out)["mail"] == park.PASS


def test_another_seat_parked_while_subscribed_stays_unknown(home, capsys):
    env = {**home.env, "SESSIONMAIL_SUBSCRIBE": "p"}
    assert run(home, capsys, "--no-promises", env=env)[0] == park.EXIT_NOT_CLEAN
    other = {**home.env, "CLAUDE_CODE_SESSION_ID": OTHER}
    code, out = run(home, capsys, "--check", SID[:8], env=other, now=PARK_AT + 5)
    assert code == park.EXIT_NOT_CLEAN and lines_of(out)["mail"] == park.UNKNOWN


def test_the_own_seats_subscriptions_come_from_its_environment(home, capsys):
    env = {**home.env, "SESSIONMAIL_SUBSCRIBE": "p"}
    code, out = run(home, capsys, "--no-promises", env=env)
    assert code == park.EXIT_NOT_CLEAN and "subscribed to p: not read" in out


# --- the listener list is the setting, by default ------------------------------------------------

def test_the_default_listener_list_is_the_pane_state_setting(home, capsys, monkeypatch):
    from sessiontools import pane_state
    monkeypatch.setattr(pane_state, "listeners", lambda env=None: ("tail -f",))
    home.shell("tail -f /var/log/x")
    for sid in (SID, OTHER):
        home.transcript(sid)
    code = park.main(["--no-promises"], env=home.env, rows=lambda: home.rows,
                     table=lambda: home.table, now=PARK_AT, own_pid=999_999)
    out = capsys.readouterr().out
    assert code == park.EXIT_CLEAN, out
    assert "listener only (tail -f /var/log/x)" in out


# --- keep_until parsing, alone ---------------------------------------------------------------------

def test_parse_keep_until():
    now = ANCHOR_SUMMER.timestamp()
    later, earlier = ANCHOR_SUMMER + timedelta(days=3650), ANCHOR_SUMMER - timedelta(days=3650)
    assert park.parse_keep_until(later.isoformat(), now) == later.timestamp()
    for bad in ("+2h", f"{later:%Y-%m-%dT%H:%M:%S}", f"{earlier:%Y-%m-%dT%H:%M:%SZ}", "soon", ""):
        with pytest.raises(ValueError):
            park.parse_keep_until(bad, now)
