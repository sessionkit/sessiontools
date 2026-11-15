"""`session respawn`, resolve and plan (ST-30 do-001; design.md §1-§3, §6)
and the act's order and refusals (do-002; design.md §4-§5, super ruling 3).

Every seat here is FAKE: an injected `listing.Row`, a synthetic process
table and a fake `tmux.panes`. The launch records are files under the
conftest's walled SESSIONS_DIR. Nothing reads the live fleet or runs tmux:
`--go`'s tmux calls and its exec are the Host's recorders, never the real
ones. The real act, on a private server, is `test_respawn_tmux.py`.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from sessiontools import claude_code as cc
from sessiontools import cli, procs, respawn, settings
from sessiontools.listing import Row
from sessiontools.procs import Proc
from sessiontools.start import pane as _pane
from sessiontools.start.record import render_command

START = "Sun Dec  8 01:00:00 2030"
T0 = procs.parse_ctime(START)
ME = 9000                      # this process: session respawn
SHELL = 8000                   # the seat's tool shell, its parent


def sock_dir() -> str:
    return os.path.join(os.environ["TMUX_TMPDIR"], f"tmux-{os.getuid()}")


class Host:
    """One fake host: seats in panes, a process table, one tmux server's
    panes per socket name, and the caller's environment."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.rows: list[Row] = []
        self.table: dict[int, Proc] = {1: Proc(1, 0, T0)}
        self.panes: dict[str | None, dict[int, str]] = {}
        self.env = dict(os.environ)
        self.on_path = True
        self.live_model: str | None = "claude-opus-5-5"
        self._pid = 5000
        #: --go's recorders: every tmux call (argv), and the one exec.
        self.runs: list[list[str]] = []
        self.execs: list[tuple[str, list[str], dict]] = []
        #: tmux's answer per subcommand: (returncode, stdout, stderr), or an
        #: exception to raise. display-message fails by default, so no test
        #: reaches `placement.environment_warnings` (and a real tmux) by accident.
        self.answers: dict[str, tuple[int, str, str] | BaseException] = {
            "display-message": (1, "", "no server")}
        self.on_exec = None            # called inside the fake exec, before it records
        self.exec_error: OSError | None = None

    def seat(self, sid: str, pane: str | None = "%3", socket: str | None = "default",
             line: str | None = "session start reviewer", directory: str | Path | None = None,
             name: str = "reviewer", record: bool = True, own: bool = False) -> Row:
        """A live seat in PANE on SOCKET, its launch record holding LINE.
        OWN makes it the caller's: this process runs under it, in its pane."""
        self._pid += 1
        pid = self._pid
        self.table[pid] = Proc(pid, 1, T0)
        if pane is not None and socket is not None:
            self.panes.setdefault(socket, {})[pid] = pane
        if own:
            self.table[SHELL] = Proc(SHELL, pid, T0)
            self.table[ME] = Proc(ME, SHELL, T0)
            self.env["TMUX_PANE"] = pane or ""
            self.env["TMUX"] = f"{sock_dir()}/{socket},4242,0"
            self.env["CLAUDE_CODE_SESSION_ID"] = sid
        if directory is None:
            directory = self.tmp / f"dir-{pid}"
            Path(directory).mkdir()
        if record:
            launch = settings.session_dir(sid, self.env) / "launch"
            launch.mkdir(parents=True)
            text = render_command(["claude", "--session-id", sid], "session start", "/w",
                                  launch_command=line or "", launch_dir=str(directory))
            (launch / "command").write_text(text)
        s = cc.Session(id=sid, harness=cc.HARNESS, busy=False, raw_status="idle",
                       updated_at=T0, pid=pid, proc_start=START, cwd=str(directory),
                       name=name, agent=name)
        row = Row(session=s, repo="repo", tmux_socket=socket if pane else None,
                  tmux_pane=pane, started=T0)
        self.rows.append(row)
        return row

    def world(self) -> respawn.World:
        return respawn.World(environ=self.env, rows=lambda: list(self.rows),
                             table=lambda: dict(self.table),
                             panes=lambda socket: dict(self.panes.get(socket, {})),
                             which=lambda name: "/bin/session" if self.on_path else None,
                             pid=ME, model=lambda row: self.live_model,
                             run=self._run, execvpe=self._exec, now=lambda: 1_700_000_000.0)

    def _run(self, argv):
        self.runs.append(list(argv))
        verb = argv[3]
        answer = self.answers.get(verb, (0, "", ""))
        if isinstance(answer, BaseException):
            raise answer
        code, out, err = answer
        return subprocess.CompletedProcess(list(argv), code, out, err)

    def _exec(self, file, argv, env):
        if self.on_exec is not None:
            self.on_exec()
        if self.exec_error is not None:
            raise self.exec_error
        self.execs.append((file, list(argv), dict(env)))

    def run(self, *argv: str) -> int:
        return respawn.main(list(argv), world=self.world())


SID = "0041271d-0000-4000-8000-000000000030"
OTHER = "7a7a7a7a-0000-4000-8000-000000000030"


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def refused(host: Host, capsys, *argv: str) -> str:
    assert host.run(*argv) == 2
    out, err = capsys.readouterr()
    assert out == "", "a refusal prints no plan"
    assert err.startswith("session respawn: ") and err.count("\n") == 1, err
    return err


def plan_rows(text: str) -> dict[str, str]:
    lines = text.splitlines()
    assert lines[0] == "session respawn: plan, nothing touched (--go acts)"
    return {line[2:12].strip(): line[13:] for line in lines[1:]}


# ---------- §1 the own pane -------------------------------------------------------------------

def test_tmux_pane_unset_refuses(host, capsys):
    host.seat(SID, own=True)
    del host.env["TMUX_PANE"]
    assert "$TMUX_PANE is unset or empty" in refused(host, capsys)


def test_tmux_pane_empty_refuses(host, capsys):
    host.seat(SID, own=True)
    host.env["TMUX_PANE"] = ""
    assert "$TMUX_PANE is unset or empty" in refused(host, capsys)


def test_tmux_empty_refuses(host, capsys):
    host.seat(SID, own=True)
    host.env["TMUX"] = ""
    assert "not inside tmux" in refused(host, capsys)


def test_no_ancestor_is_a_pane_refuses(host, capsys):
    host.seat(SID, own=True)
    host.table[SHELL] = Proc(SHELL, 1, T0)          # the shell is no longer under the seat
    err = refused(host, capsys)
    assert "no ancestor of this process is a pane" in err and "%3" in err


def test_ancestry_in_another_pane_refuses_and_names_both(host, capsys):
    host.seat(SID, own=True)
    host.env["TMUX_PANE"] = "%9"
    err = refused(host, capsys)
    assert "$TMUX_PANE is %9" in err and "runs in %3" in err


def test_the_same_pane_id_on_another_socket_is_not_a_match(host, capsys):
    """The 11 Sep failure: `%3` on another server answered for this `%3`."""
    host.seat(SID, own=True)
    host.rows.clear()
    host.seat(OTHER, pane="%3", socket="tm-store-work")
    err = refused(host, capsys)
    assert "no live seat in pane %3 on default" in err


def test_zero_seats_in_the_pane_refuses(host, capsys):
    host.seat(SID, own=True)
    host.rows.clear()
    assert "no live seat in pane %3" in refused(host, capsys)


def test_two_seats_in_the_pane_refuse_and_are_listed(host, capsys):
    host.seat(SID, own=True)
    host.seat(OTHER, pane="%3", name="scout")
    err = refused(host, capsys)
    assert "ambiguous: 2 live seats in pane %3" in err
    assert "0041271d reviewer" in err and "7a7a7a7a scout" in err


def test_tmp_and_private_tmp_are_one_server(host, capsys, tmp_path):
    """$TMUX names the socket by one spelling and the socket dir by another:
    both resolve to one path, so the seat is found."""
    host.seat(SID, own=True)
    real = os.path.realpath(sock_dir())
    link = tmp_path / "alias"
    link.symlink_to(os.path.dirname(real))
    host.env["TMUX"] = f"{link}/{os.path.basename(real)}/default,4242,0"
    assert host.run() == 0
    assert plan_rows(capsys.readouterr().out)["pane"].startswith("%3 on default")


# ---------- §1 a SID --------------------------------------------------------------------------

def test_a_sid_shorter_than_8_refuses(host, capsys):
    host.seat(SID)
    assert "too short" in refused(host, capsys, "0041271")


def test_a_name_refuses(host, capsys):
    host.seat(SID, name="ops")
    err = refused(host, capsys, "ops")
    assert "not a session id" in err and "session list" in err


def test_a_long_name_refuses_too(host, capsys):
    host.seat(SID, name="reviewer")
    assert "not a session id" in refused(host, capsys, "reviewer")


def test_an_ambiguous_prefix_refuses_and_lists_the_matches(host, capsys):
    host.seat(SID, pane="%3")
    host.seat("0041271d-ffff-4000-8000-000000000031", pane="%4", name="scout")
    err = refused(host, capsys, "0041271d")
    assert "ambiguous: 2 live seats start '0041271d'" in err
    assert "0041271d reviewer (%3)" in err and "0041271d scout (%4)" in err


def test_an_unknown_sid_refuses(host, capsys):
    host.seat(SID)
    assert "no live seat has a session id starting" in refused(host, capsys, "deadbeef")


def test_a_sid_with_no_pane_refuses(host, capsys):
    host.seat(SID, pane=None)
    assert "is not in tmux" in refused(host, capsys, "0041271d")


def test_a_full_sid_plans_and_is_case_insensitive(host, capsys):
    host.seat(SID)
    assert host.run(SID.upper()) == 0
    assert plan_rows(capsys.readouterr().out)["seat"].startswith("0041271d  reviewer")


def test_a_sid_naming_another_seats_pane_plans_from_outside_tmux(host, capsys):
    """Plan only: no $TMUX_PANE is needed, and the pane is the row's own."""
    host.seat(SID, pane="%7", socket="tm-store-work")
    assert host.run("0041271d") == 0
    rows = plan_rows(capsys.readouterr().out)
    assert rows["pane"].startswith("%7 on tm-store-work, by session id 0041271d")
    assert "plan only" in rows["pane"]
    assert rows["tmux"].startswith("tmux -L tm-store-work respawn-pane -k -t %7 ")


def test_a_sid_naming_this_seat_goes_through_the_own_pane_check(host, capsys):
    host.seat(SID, own=True)
    assert host.run("0041271d") == 0
    assert "confirmed by process ancestry" in plan_rows(capsys.readouterr().out)["pane"]


def test_a_sid_naming_this_seat_refuses_when_the_pane_check_fails(host, capsys):
    host.seat(SID, own=True)
    host.table[SHELL] = Proc(SHELL, 1, T0)
    assert "no ancestor of this process is a pane" in refused(host, capsys, "0041271d")


def test_a_sid_naming_this_seat_refuses_when_the_pane_holds_another(host, capsys):
    """The environment says this is seat SID, but the confirmed pane holds
    another seat: the two must agree."""
    host.seat(OTHER, own=True, name="scout")
    host.seat(SID, pane="%5")
    host.env["CLAUDE_CODE_SESSION_ID"] = SID
    err = refused(host, capsys, "0041271d")
    assert "names this seat, but pane %3 holds 7a7a7a7a scout" in err


def test_there_is_no_session_id_flag(host, capsys):
    host.seat(SID)
    with pytest.raises(SystemExit) as stop:
        host.run("--session-id", SID)
    assert stop.value.code == 2
    assert "unrecognized arguments: --session-id" in capsys.readouterr().err


# ---------- §2 the record ---------------------------------------------------------------------

def test_no_launch_command_refuses_with_the_way_out(host, capsys):
    """A pre-change seat: its record has the first three keys only."""
    host.seat(SID, own=True, line=None)
    err = refused(host, capsys)
    assert "no launch_command recorded" in err and "kill the window and kick" in err


def test_a_seat_with_no_launch_record_refuses(host, capsys):
    """A keyed resume's fork: a new session dir with no `launch/` at all."""
    host.seat(SID, own=True, record=False)
    assert "no launch_command recorded" in refused(host, capsys)


def test_a_line_that_is_not_session_start_refuses(host, capsys):
    host.seat(SID, own=True, line="claude --model opus")
    assert "is not `session start`" in refused(host, capsys)


def test_a_line_that_does_not_split_refuses(host, capsys):
    host.seat(SID, own=True, line="session start reviewer 'open")
    assert "does not split" in refused(host, capsys)


@pytest.mark.parametrize("flag", ["--session-id X", "--session-id=X"])
def test_a_recorded_session_id_refuses(host, capsys, flag):
    host.seat(SID, own=True, line=f"session start reviewer {flag}")
    assert "carries --session-id" in refused(host, capsys)


@pytest.mark.parametrize("token", ["--resume 0041271d", "-r", "--continue", "-c",
                                   "--resume=0041271d"])
def test_a_recorded_resume_refuses(host, capsys, token):
    host.seat(SID, own=True, line=f"session start reviewer {token}")
    assert "carries a resume" in refused(host, capsys)


def test_a_record_with_no_launch_dir_refuses(host, capsys):
    host.seat(SID, own=True)
    command = settings.session_dir(SID, host.env) / "launch" / "command"
    command.write_text("vendor: claude\nstart: s\ncwd: /w\nlaunch_command: session start reviewer\n")
    assert "no launch_dir recorded" in refused(host, capsys)


def test_a_launch_dir_that_is_gone_refuses(host, capsys, tmp_path):
    host.seat(SID, own=True, directory=tmp_path / "gone")
    assert "is gone or not a directory" in refused(host, capsys)


def test_session_not_on_path_refuses(host, capsys):
    host.seat(SID, own=True)
    host.on_path = False
    assert "`session` is not on this PATH" in refused(host, capsys)


def test_a_first_message_file_that_is_gone_refuses(host, capsys, tmp_path):
    gone = tmp_path / "gone.md"
    host.seat(SID, own=True, line=f"session start --first-message-file {gone} reviewer")
    err = refused(host, capsys)
    assert "--first-message-file" in err and "no such file" in err


def test_a_first_message_file_that_is_0644_refuses(host, capsys, tmp_path):
    loose = tmp_path / "loose.md"
    loose.write_text("hello")
    loose.chmod(0o644)
    host.seat(SID, own=True, line=f"session start --first-message-file={loose} reviewer")
    assert "it must be 0600" in refused(host, capsys)


def test_a_first_message_file_that_is_0600_plans_and_is_named(host, capsys, tmp_path):
    ok = tmp_path / "ok.md"
    ok.write_text("hello")
    ok.chmod(0o600)
    host.seat(SID, own=True, line=f"session start --first-message-file {ok} reviewer")
    assert host.run() == 0
    rows = plan_rows(capsys.readouterr().out)
    assert rows["message"] == f"{ok} (checked: ours, 0600)"
    assert rows["pane line"] == f"session start --first-message-file {ok} reviewer"


# ---------- §3 + §6 the three plan shapes ------------------------------------------------------

def test_the_plan_for_a_typed_seat(host, capsys):
    row = host.seat(SID, own=True, line="session start reviewer")
    directory = row.session.cwd
    assert host.run() == 0
    out = capsys.readouterr().out
    rows = plan_rows(out)
    assert list(rows) == ["seat", "pane", "record", "entered", "dir", "pane line",
                          "message", "model", "log", "tmux"]
    assert rows["seat"] == "0041271d  reviewer  (agent reviewer)"
    assert rows["pane"] == "%3 on default, by $TMUX_PANE, confirmed by process ancestry"
    assert rows["record"] == str(settings.session_dir(SID) / "launch" / "command")
    assert rows["entered"] == "session start reviewer"
    assert rows["dir"] == directory
    assert rows["pane line"] == "session start reviewer"
    assert rows["message"] == "none"
    assert rows["log"] == str(settings.log_dir(settings.PACKAGES["session"]) / "respawn.log")
    assert rows["tmux"] == (f"tmux -L default respawn-pane -k -t %3 -c {shlex.quote(directory)}"
                            " -e SESSION_START_ENTERED=… -e SESSION_START_ENTERED_DIR=…"
                            " 'session start reviewer'")


def test_the_plan_for_a_placed_seat_strips_the_placement(host, capsys):
    host.seat(SID, own=True,
              line="session start --in-tmux --tmux-target agents:reviewer --switch --keyed reviewer")
    assert host.run() == 0
    rows = plan_rows(capsys.readouterr().out)
    assert rows["entered"] == "session start --in-tmux --tmux-target agents:reviewer --switch --keyed reviewer"
    assert rows["pane line"] == "session start --keyed reviewer"
    assert rows["tmux"].endswith(" 'session start --keyed reviewer'")


def test_the_plan_for_bare_words_shows_the_file_and_never_the_words_in_tmux(host, capsys):
    host.seat(SID, own=True, line="session start --in-tmux reviewer check the auth")
    assert host.run() == 0
    out = capsys.readouterr().out
    rows = plan_rows(out)
    launches = _pane.launches_root(host.env)
    message, _, note = rows["message"].partition(" ")
    assert message.startswith(launches + os.sep) and message.endswith("/first-message.md")
    assert note == "(a new 0600 file, written by --go)"
    assert rows["pane line"] == (f"session start --first-message-file {message} "
                                 "--first-message-file-over 0 reviewer")
    for word in ("check", "auth"):
        assert word not in rows["tmux"] and word not in rows["pane line"]
    assert out.count("check the auth") == 1, "the words are printed once, as entered"


def test_the_plan_touches_nothing(host, capsys):
    host.seat(SID, own=True, line="session start reviewer review the auth")
    command = settings.session_dir(SID, host.env) / "launch" / "command"
    before = command.read_text()
    assert host.run() == 0
    assert not os.path.exists(_pane.launches_root(host.env)), "no message file is written"
    assert not respawn.log_path(host.env).exists(), "no log line is written"
    assert command.read_text() == before


# ---------- §7 the model: one drift line (ruling 2) --------------------------------------------

def test_a_typed_model_that_differs_from_the_live_one_is_one_drift_line(host, capsys):
    host.seat(SID, own=True, line="session start reviewer --model claude-sonnet-5")
    assert host.run() == 0
    out = capsys.readouterr().out
    assert plan_rows(out)["model"] == ("live model claude-opus-5-5, "
                                       "the respawn will start with claude-sonnet-5")
    assert out.count("live model") == 1


def test_a_typed_model_that_matches_shows_no_drift(host, capsys):
    host.seat(SID, own=True, line="session start reviewer --model=claude-opus-5-5")
    assert host.run() == 0
    row = plan_rows(capsys.readouterr().out)["model"]
    assert row == "claude-opus-5-5, as typed in the line and live now"


def test_no_typed_model_says_the_launch_resolves_it(host, capsys):
    host.seat(SID, own=True, line="session start reviewer")
    host.live_model = None
    assert host.run() == 0
    row = plan_rows(capsys.readouterr().out)["model"]
    assert row.startswith("live model unknown; the line types no --model")


# ---------- --go: the act (do-002) -----------------------------------------------------------

def log_text(host: Host) -> str:
    path = respawn.log_path(host.env)
    return path.read_text() if path.exists() else ""


def launches(host: Host) -> list[Path]:
    root = Path(_pane.launches_root(host.env))
    return sorted(root.glob("*/*")) if root.exists() else []


def test_go_logs_then_sets_remain_on_exit_then_execs_tmux_on_the_own_pane(host, capsys):
    host.seat(SID, own=True, line="session start --in-tmux reviewer")
    seen = {}
    host.on_exec = lambda: seen.update(log=log_text(host), runs=list(host.runs))
    assert host.run("--go") == 0          # only the fake exec returns
    [(file, argv, env)] = host.execs
    from sessiontools import tmux
    directory = host.rows[0].session.cwd
    assert file == tmux.TMUX_BIN and argv[0] == tmux.TMUX_BIN
    assert argv[1:] == ["-L", "default", "respawn-pane", "-k", "-t", "%3", "-c", directory,
                        "-e", "SESSION_START_ENTERED=session start --in-tmux reviewer",
                        "-e", f"SESSION_START_ENTERED_DIR={directory}",
                        "session start reviewer"]
    # the order: the log line, then remain-on-exit, then the exec
    assert seen["log"].count("\n") == 1
    assert seen["runs"][0] == ["tmux", "-L", "default", "set-option", "-p", "-t", "%3",
                               "remain-on-exit", "failed"]
    assert "VIRTUAL_ENV" not in env, "tmux runs under ST-285's host environ"
    assert "respawning %3 on default" in capsys.readouterr().err


def test_the_log_line_is_reaps_shape_and_the_file_is_0600(host):
    row = host.seat(SID, own=True, line="session start reviewer")
    assert host.run("--go") == 0
    path = respawn.log_path(host.env)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.parent == settings.log_dir(settings.PACKAGES["session"], host.env)
    fields = log_text(host).rstrip("\n").split("\t")
    assert fields[1:] == [row.short, "reviewer", "reviewer", "default:%3", row.session.cwd,
                          "session start reviewer", "own pane"]
    assert fields[0].startswith("20")     # an ISO time


def test_a_second_go_appends(host):
    host.seat(SID, own=True)
    assert host.run("--go") == 0 and host.run("--go") == 0
    assert log_text(host).count("\n") == 2


def test_go_writes_the_new_message_file_before_the_exec_and_logs_no_word(host):
    host.seat(SID, own=True, line="session start --in-tmux reviewer check the auth")
    files = []
    host.on_exec = lambda: files.extend(launches(host))
    assert host.run("--go") == 0
    [path] = files
    assert path.read_text() == "check the auth"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    argv = host.execs[0][1]
    assert argv[-1] == (f"session start --first-message-file {path} "
                        "--first-message-file-over 0 reviewer")
    entered = [a for a in argv if a.startswith("SESSION_START_ENTERED=")]
    assert entered == ["SESSION_START_ENTERED=session start --in-tmux reviewer check the auth"]
    for word in ("check", "auth"):
        assert word not in log_text(host)
        assert word not in argv[-1]


def test_an_unwritable_log_refuses_and_touches_nothing(host, capsys):
    host.seat(SID, own=True, line="session start reviewer review the auth")
    logs = settings.log_dir(settings.PACKAGES["session"], host.env)
    logs.mkdir(parents=True)
    logs.chmod(0o500)
    try:
        err = refused(host, capsys, "--go")
    finally:
        logs.chmod(0o700)
    assert "respawn.log cannot be written" in err and "the pane is untouched" in err
    assert launches(host) == [], "no message file"
    assert host.runs == [] and host.execs == [], "no tmux"


def test_a_log_write_that_fails_takes_the_message_file_back(host, capsys, monkeypatch):
    host.seat(SID, own=True, line="session start reviewer review the auth")

    def full(fd, text):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(respawn, "append", full)
    err = refused(host, capsys, "--go")
    assert "No space left on device" in err
    assert launches(host) == [] and host.runs == [] and host.execs == []


def test_a_message_file_that_cannot_be_written_logs_no_line(host, capsys):
    host.seat(SID, own=True, line="session start reviewer review the auth")
    root = Path(_pane.launches_root(host.env))
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("a file where the launches dir goes")
    err = refused(host, capsys, "--go")
    assert "first-message file" in err and "the pane is untouched" in err
    assert log_text(host) == "", "a line is never logged for an act that did not happen"
    assert host.runs == [] and host.execs == []


def test_a_tmux_without_remain_on_exit_failed_is_degraded_not_refused(host, capsys):
    host.seat(SID, own=True)
    host.answers["set-option"] = (1, "", "invalid option value: failed")
    assert host.run("--go") == 0
    err = capsys.readouterr().err
    assert ("could not set remain-on-exit failed (invalid option value: failed); "
            "a respawn that fails will close the pane with its reason") in err
    assert err.count("remain-on-exit") == 1
    assert len(host.execs) == 1, "the respawn still runs"


def test_a_hung_set_option_is_degraded_too(host, capsys):
    host.seat(SID, own=True)
    host.answers["set-option"] = subprocess.TimeoutExpired(["tmux"], 5)
    assert host.run("--go") == 0
    assert "could not set remain-on-exit failed" in capsys.readouterr().err
    assert len(host.execs) == 1


def test_the_env_warning_names_the_panes_session(host, capsys, monkeypatch):
    host.seat(SID, own=True)
    host.answers["display-message"] = (0, "agents\n", "")
    asked = []
    from sessiontools.start import placement

    def warnings(session, environ=None):
        asked.append(session)
        return ["warning: AGENTS_DIR is '/a' here but '/b' in tmux's environment; ..."]
    monkeypatch.setattr(placement, "environment_warnings", warnings)
    assert host.run("--go") == 0
    assert asked == ["agents"]
    assert host.runs[1] == ["tmux", "-L", "default", "display-message", "-p", "-t", "%3",
                            "#{session_name}"]
    assert "session respawn: warning: AGENTS_DIR is '/a' here" in capsys.readouterr().err


def test_tmux_that_cannot_be_execd_exits_1_and_takes_the_file_back(host, capsys):
    host.seat(SID, own=True, line="session start reviewer review the auth")
    host.exec_error = FileNotFoundError(2, "No such file or directory")
    assert host.run("--go") == 1
    assert "tmux could not be run" in capsys.readouterr().err
    assert launches(host) == []


def test_go_on_another_seats_pane_by_sid_refuses_with_ruling_3(host, capsys):
    """Super ruling 3: SID --go on any pane but the caller's own refuses;
    the ancestry check cannot hold for someone else's pane."""
    host.seat(SID, own=True, pane="%3")
    host.seat(OTHER, pane="%7", name="scout")
    err = refused(host, capsys, OTHER[:8], "--go")
    assert "acting on another seat is not built; see the plan" in err
    assert "%7" in err
    assert log_text(host) == "" and host.runs == [] and host.execs == []


def test_go_on_another_seat_refuses_from_outside_tmux_too(host, capsys):
    host.seat(OTHER, pane="%7", name="scout")
    err = refused(host, capsys, OTHER, "--go")
    assert respawn.NOT_BUILT in err
    assert host.execs == []


def test_the_same_sid_without_go_still_plans(host, capsys):
    host.seat(OTHER, pane="%7", name="scout")
    assert host.run(OTHER[:8]) == 0
    assert plan_rows(capsys.readouterr().out)["pane"].startswith("%7 on default")


def test_a_sid_naming_this_pane_acts_by_the_pair_with_no_session_id(host, capsys):
    """T1 (do-001 review): CLAUDE_CODE_SESSION_ID unset, the SID's seat is
    this process's (socket, pane) pair: it is the caller's own, so --go acts."""
    host.seat(SID, own=True)
    del host.env["CLAUDE_CODE_SESSION_ID"]
    assert host.run(SID[:8], "--go") == 0
    assert host.execs[0][1][host.execs[0][1].index("-t") + 1] == "%3"


def test_go_still_refuses_for_the_plans_own_reasons_first(host, capsys):
    host.seat(SID, own=True, line=None)
    assert "no launch_command recorded" in refused(host, capsys, "--go")
    assert log_text(host) == "" and host.execs == []


def test_go_help_no_longer_says_not_built():
    text = respawn.build_parser().format_help()
    assert "not built yet" not in text and "--go" in text


def test_the_verb_is_wired(monkeypatch):
    seen = []
    monkeypatch.setattr(respawn, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["respawn", "0041271d"]) == 0
    assert seen == [["0041271d"]]
    assert "respawn" in dict(cli.VERBS)
    assert cli.PARSERS["respawn"] == "sessiontools.respawn:build_parser"


def test_outside_tmux_with_no_sid_refuses_through_the_cli(capsys):
    """The conftest clears $TMUX and $TMUX_PANE: the real World refuses at once."""
    assert cli.main(["respawn"]) == 2
    assert "$TMUX_PANE is unset or empty" in capsys.readouterr().err


# ---------- a fresh HOME: every path through tools' resolver (do-005, A2) ----------------

def test_a_fresh_home_logs_under_the_tools_log_dir_and_never_makes_dot_session(
        host, capsys, monkeypatch, tmp_path):
    """ST-30 do-005 (A2). On a HOME with no `~/.session/`, no SESSIONS_DIR and
    no SESSION_STATE_DIR, every path respawn touches comes from the resolver
    (settings.session_dir, settings.log_dir, the state leaf): the record is
    read from `~/.sessionkit/tools/state/sessions/`, the first-message file
    is written under `~/.sessionkit/tools/state/launches/`, the log line lands
    in `~/.sessionkit/tools/log/respawn.log`, and no `~/.session` is made.

    This process's own environment points at a DECOY home with neither
    override set: a read or write that ignored the world's environ would land
    there (or refuse for want of a record), and the decoy must stay empty."""
    home, decoy = tmp_path / "home", tmp_path / "decoy"
    home.mkdir()
    decoy.mkdir()
    for var in ("SESSIONS_DIR", "SESSION_STATE_DIR"):
        host.env.pop(var, None)
        monkeypatch.delenv(var, raising=False)
    host.env["HOME"] = str(home)
    monkeypatch.setenv("HOME", str(decoy))
    kit = home / ".sessionkit" / "tools"

    row = host.seat(SID, own=True, line="session start reviewer review the auth")
    record = kit / "state" / "sessions" / SID / "launch" / "command"
    assert record.is_file(), "the fixture's record follows the same leaf rule"

    assert host.run() == 0
    rows = plan_rows(capsys.readouterr().out)
    assert rows["record"] == str(record)
    assert rows["log"] == str(kit / "log" / "respawn.log")

    assert host.run("--go") == 0
    log = kit / "log" / "respawn.log"
    assert log.is_file() and stat.S_IMODE(log.stat().st_mode) == 0o600
    assert log.read_text().rstrip("\n").split("\t")[1] == row.short
    [message] = sorted((kit / "state" / "launches").glob("*/*"))
    assert message.read_text() == "review the auth"
    assert not (home / ".session").exists()
    assert list(decoy.iterdir()) == [], sorted(str(p) for p in decoy.rglob("*"))
