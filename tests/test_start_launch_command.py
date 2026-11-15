"""ST-285 do-001: the launch record keeps the line as ENTERED.

`launch/command` gains `launch_command:` (the `session start` line as typed)
and `launch_dir:` (the directory it was typed in), after `vendor:`, `start:`
and `cwd:`, which stay as they were. A resume records neither (ruling 6).
"""

from __future__ import annotations

import glob
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from start_support import launch_e2e, plan_for, write_key_init_stub
from test_start_placement import pane_ran, tmux_launch_e2e, window_id_of
from test_start_record import no_repo_env

from sessiontools.start import record, run
from sessiontools.start.errors import LaunchError
from sessiontools.start.record import (
    COMMAND_FILE,
    ENV_FILE,
    LAUNCH_COMMAND_ENV,
    LAUNCH_DIR_ENV,
    LAUNCH_FROM_ENV_FLAG,
    RECORD_DIR,
    LaunchRecord,
    entered_dir,
    entered_line,
    launch_environ,
    parse_command,
    render_command,
    render_record_call,
)

RESUME_ID = "0f0f0f0f-0000-4000-8000-000000000000"

#: Typed lines that must survive the file: quotes, a line break, a `$`.
AWKWARD = [
    ["reviewer"],
    ["--directory", "/a dir", "reviewer", "review the auth"],
    ["reviewer", "it's", 'say "hi"', "$HOME", "two\nlines"],
    ["--in-tmux", "reviewer", "--model", "opus", "--", "x;y && z"],
]


def command_fields(directory: Path) -> dict[str, str]:
    return parse_command((directory / RECORD_DIR / COMMAND_FILE).read_text())


# ---------- the pure half: the line, the dir, render and parse -----------------

def test_the_entered_line_is_spelled_session_start():
    assert entered_line(["reviewer", "--directory", "X"]) == "session start reviewer --directory X"
    assert entered_line([]) == "session start"
    assert shlex.split(entered_line(["reviewer", "two words"])) == [
        "session", "start", "reviewer", "two words"]


@pytest.mark.parametrize("argv", AWKWARD)
def test_render_and_parse_round_trip(argv):
    vendor = ["claude", "--session-id", "sid", "--", "a\nb"]
    line = entered_line(argv)
    text = render_command(vendor, "py -m session start", "/w d", launch_command=line,
                          launch_dir="/typed\nhere")
    fields = parse_command(text)
    assert list(fields) == ["vendor", "start", "cwd", "launch_command", "launch_dir"]
    assert shlex.split(fields["vendor"]) == vendor
    assert shlex.split(fields["cwd"]) == ["/w d"]
    assert shlex.split(fields["launch_command"]) == ["session", "start", *argv]
    assert shlex.split(fields["launch_dir"]) == ["/typed\nhere"]


def test_the_first_three_keys_are_unchanged_and_the_new_two_follow_them():
    before = render_command(["claude", "-x"], "session start scout", "/w")
    after = render_command(["claude", "-x"], "session start scout", "/w",
                           launch_command="session start scout", launch_dir="/w")
    assert before == "vendor: claude -x\nstart: session start scout\ncwd: /w\n"
    assert after == before + "launch_command: session start scout\nlaunch_dir: /w\n"


def test_no_entered_line_writes_neither_key():
    text = render_command(["claude"], "s", "/w", launch_command="", launch_dir="/w")
    assert "launch_command" not in text and "launch_dir" not in text


def test_a_pre_change_record_parses_with_neither_key():
    fields = parse_command("vendor: claude --session-id x\nstart: /py session start reviewer\n"
                           "cwd: /w\n")
    assert set(fields) == {"vendor", "start", "cwd"}


def test_parse_skips_lines_that_are_not_keys():
    assert parse_command("junk\n  indented: no\nkey: 'a\nb'\n") == {"key": "'a\nb'"}


def test_launch_dir_is_pwd_when_it_names_the_same_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert entered_dir({"PWD": str(link)}, cwd=str(real)) == str(link)


@pytest.mark.parametrize("pwd", ["elsewhere", "relative", "missing", None])
def test_launch_dir_is_getcwd_when_pwd_is_stale_relative_or_absent(tmp_path, pwd):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "elsewhere").mkdir()
    environ = {"elsewhere": {"PWD": str(tmp_path / "elsewhere")},
               "relative": {"PWD": "real"},
               "missing": {"PWD": str(tmp_path / "gone")},
               None: {}}[pwd]
    assert entered_dir(environ, cwd=str(real)) == str(real)


# ---------- the script's half: the record call carries both, exactly ------------
# Ruling 7, review F2: the line rides the script's ENVIRONMENT, never its argv.

def run_call(tmp_path, rec, tail="", env=None):
    """RECORD's call, then TAIL, under bash, with the environment
    `run.execute` would give it (`launch_environ`) unless ENV is given."""
    call = render_record_call(rec, "claude --session-id sid")
    environ = {**no_repo_env(tmp_path), **(launch_environ(rec) if env is None else env)}
    return call, subprocess.run(["bash", "-c", call + "\n" + tail], capture_output=True,
                                text=True, env=environ, check=False)


def awkward_record(session: Path, line: str) -> LaunchRecord:
    return LaunchRecord(session_dir=shlex.quote(str(session)), shown_dir=str(session),
                        start_command="py session start", work_dir="/w", files=(),
                        secret_names=(), launch_command=line, launch_dir="/typed dir")


def test_the_record_call_carries_the_line_through_its_environment_exactly(tmp_path):
    """The typed line, quotes, a `$` and a line break included, reaches the
    file exactly as entered, by the environment, unexpanded."""
    session = tmp_path / "session"
    session.mkdir()
    line = entered_line(["reviewer", "it's $HOME", "two\nlines"])
    _call, result = run_call(tmp_path, awkward_record(session, line))
    assert result.returncode == 0 and result.stderr == "", result.stderr
    fields = command_fields(session)
    assert fields["launch_command"] == line
    assert shlex.split(fields["launch_dir"]) == ["/typed dir"]
    assert shlex.split(fields["vendor"]) == ["claude", "--session-id", "sid"]


def test_no_word_of_the_entered_line_is_in_the_record_calls_argv(tmp_path):
    """F2's test: the call is text in the `bash -c` script, which is argv
    (`ps` is world-readable). No word of the line, nor its dir, is in it."""
    session = tmp_path / "session"
    session.mkdir()
    line = entered_line(["reviewer", "zebrafish", "quokka"])
    call, result = run_call(tmp_path, awkward_record(session, line))
    assert result.returncode == 0, result.stderr
    for word in ("zebrafish", "quokka", "/typed dir", "typed"):
        assert word not in call
    assert "--launch-command" not in call and "--launch-dir" not in call
    assert f" {LAUNCH_FROM_ENV_FLAG} " in call
    assert command_fields(session)["launch_command"] == line


def test_nothing_after_the_record_inherits_the_line_and_the_env_file_omits_it(tmp_path):
    """The hand-off is unset on the line after the call (the identity block
    and the vendor never see it), and the env file is the env the vendor
    gets, so it never lists it."""
    session = tmp_path / "session"
    session.mkdir()
    _call, result = run_call(
        tmp_path, awkward_record(session, entered_line(["reviewer", "zebrafish"])),
        tail=f'echo "after=${{{LAUNCH_COMMAND_ENV}-unset}}/${{{LAUNCH_DIR_ENV}-unset}}"')
    assert result.stdout.strip() == "after=unset/unset", result.stdout
    env_text = (session / RECORD_DIR / ENV_FILE).read_text()
    assert LAUNCH_COMMAND_ENV not in env_text and LAUNCH_DIR_ENV not in env_text
    assert "zebrafish" not in env_text


def test_a_record_with_no_entered_line_passes_no_flag_and_is_handed_nothing():
    rec = LaunchRecord(session_dir="/s", shown_dir="/s", start_command="s", work_dir="/w",
                       files=(), secret_names=())
    assert LAUNCH_FROM_ENV_FLAG not in render_record_call(rec, "claude")
    assert launch_environ(rec) == {} and launch_environ(None) == {}


def test_main_reads_both_keys_from_the_environment(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    code = record.main(["--dir", str(session), "--start-command", "py session start reviewer",
                        "--cwd", "/w", LAUNCH_FROM_ENV_FLAG, "--", "claude", "--session-id",
                        "sid"],
                       environ={**no_repo_env(tmp_path),
                                LAUNCH_COMMAND_ENV: "session start reviewer 'a b'",
                                LAUNCH_DIR_ENV: "/typed"})
    assert code == 0
    fields = command_fields(session)
    assert fields["launch_command"] == "session start reviewer 'a b'"
    assert fields["launch_dir"] == "/typed"
    assert fields["start"] == "py session start reviewer"


def test_main_without_the_flag_writes_neither_key_even_with_the_env_set(tmp_path):
    """An inherited value is never recorded unless the call asks for it."""
    session = tmp_path / "session"
    session.mkdir()
    record.main(["--dir", str(session), "--", "claude"],
                environ={**no_repo_env(tmp_path), LAUNCH_COMMAND_ENV: "session start x"})
    assert set(command_fields(session)) == {"vendor", "start", "cwd"}


def test_main_with_the_flag_and_nothing_handed_says_so_and_still_records(tmp_path, capsys):
    session = tmp_path / "session"
    session.mkdir()
    assert record.main(["--dir", str(session), LAUNCH_FROM_ENV_FLAG, "--", "claude"],
                       environ=no_repo_env(tmp_path)) == 0
    assert set(command_fields(session)) == {"vendor", "start", "cwd"}
    assert f"{LAUNCH_COMMAND_ENV} was not handed down" in capsys.readouterr().err


def test_the_typed_value_flags_are_gone(tmp_path, capsys):
    """No road back to argv: `--launch-command VALUE` is a bad argument."""
    session = tmp_path / "session"
    session.mkdir()
    record.main(["--dir", str(session), "--launch-command", "session start x", "--",
                 "claude"], environ=no_repo_env(tmp_path))
    assert "bad arguments" in capsys.readouterr().err
    assert not (session / RECORD_DIR / COMMAND_FILE).exists()


def test_execute_hands_the_line_to_the_script_by_its_environment(tmp_path, monkeypatch):
    """`run.execute` puts the plan's line in the script's environment, and
    drops an inherited value of either name first."""
    plan = plan_for(tmp_path, "", argv_tail=["zebrafish"])
    monkeypatch.setenv(LAUNCH_DIR_ENV, "/stale")
    monkeypatch.setenv(LAUNCH_COMMAND_ENV, "session start stale")
    seen = []

    class Exec(Exception):
        pass

    def fake_exec(file, argv, env):
        seen.append(env)
        raise Exec
    monkeypatch.setattr(os, "execvpe", fake_exec)
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)
    with pytest.raises(Exec):
        run.execute(plan)
    assert seen[0][LAUNCH_COMMAND_ENV] == plan.record.launch_command
    assert seen[0][LAUNCH_DIR_ENV] == plan.record.launch_dir
    resumed = plan._replace(record=None)
    env = run.script_environ(resumed)
    assert LAUNCH_COMMAND_ENV not in env and LAUNCH_DIR_ENV not in env


# ---------- the plan: every road fills it from the argv as typed -----------------

def test_a_direct_plan_records_the_typed_line_not_the_evaluated_one(tmp_path):
    plan = plan_for(tmp_path, "", argv_tail=["review", "the auth"],
                    flags=("--directory", str(tmp_path / "work"), "--dry-run"))
    line = plan.record.launch_command
    assert shlex.split(line) == ["session", "start", "--directory", str(tmp_path / "work"),
                                 "--dry-run", "scout", "review", "the auth"]
    assert "--session-id" in plan.argv           # the evaluated argv has it ...
    assert "--session-id" not in line            # ... the entered line never
    assert f" {LAUNCH_FROM_ENV_FLAG} " in plan.script
    assert "--launch-command" not in plan.script
    assert run.script_environ(plan)[LAUNCH_COMMAND_ENV] == line


def test_a_tmux_plan_keeps_the_placement_flags(tmp_path):
    stub, _record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, "", argv_tail=["hello"], flags=("--in-tmux", "--keyed"),
                    extra_env={"SESSION_KEY_INIT": stub})
    assert plan.record.launch_command == "session start --in-tmux --keyed scout hello"


@pytest.mark.parametrize("keyed", [False, True])
def test_a_resume_records_neither_key_and_never_the_resume_line(tmp_path, keyed):
    """Ruling 6, foreman decision 22:06: a resume plans no record, so a keyed
    fork's new dir has no `launch_command` (absent) and `--resume` is never
    recorded."""
    stub, _record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, "", argv_tail=["--resume", RESUME_ID],
                    flags=("--dry-run", *(("--keyed",) if keyed else ())),
                    extra_env={"SESSION_KEY_INIT": stub})
    assert (plan.identity is not None) is keyed
    assert plan.record is None
    assert LAUNCH_FROM_ENV_FLAG not in plan.script


@pytest.mark.parametrize("bare", [["--continue"], ["-c"], ["--resume"]])
def test_a_keyed_resume_with_no_id_is_refused_before_any_record(tmp_path, bare):
    """Review r3 carry (a): no id means no source to copy from; resolve
    refuses it outright, so `<root>//launch/command` is never looked up."""
    stub, _record = write_key_init_stub(tmp_path)
    with pytest.raises(LaunchError) as raised:
        plan_for(tmp_path, "", argv_tail=bare, flags=("--dry-run", "--keyed"),
                 extra_env={"SESSION_KEY_INIT": stub})
    assert raised.value.code == 2
    assert "FORK" in str(raised.value)


# ---------- end to end: a real launch writes both ---------------------------------

def only_session(tmp_path: Path) -> Path:
    found = glob.glob(str(tmp_path / "agents" / "sessions" / "*" / RECORD_DIR))
    assert len(found) == 1, found
    return Path(found[0]).parent


def test_a_direct_launch_records_the_line_and_the_logical_dir(tmp_path):
    link = tmp_path / "via-link"
    link.symlink_to(tmp_path / "work")      # launch_e2e makes work; the link follows
    result, _rec, work = launch_e2e(tmp_path, "scout", "hello", "two words",
                                    extra_env={"PWD": str(link)})
    assert result.returncode == 0, result.stderr
    assert link.resolve() == work.resolve()
    fields = command_fields(only_session(tmp_path))
    assert fields["launch_command"] == "session start scout hello 'two words'"
    assert shlex.split(fields["launch_dir"]) == [str(link)]
    assert "--session-id" in fields["vendor"]                  # evaluated, unchanged
    assert "--session-id" not in fields["launch_command"]


def test_a_stale_pwd_records_the_real_dir(tmp_path):
    result, _rec, work = launch_e2e(tmp_path, "scout", extra_env={"PWD": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    fields = command_fields(only_session(tmp_path))
    assert fields["launch_command"] == "session start scout"
    assert os.path.samefile(shlex.split(fields["launch_dir"])[0], work)


def test_a_keyed_tmux_launch_records_the_entered_line_in_the_pane(tmp_path):
    stub, _key_record = write_key_init_stub(tmp_path)
    result, rec, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "--keyed", "scout",
                                         extra_env={"SESSION_KEY_INIT": stub})
    assert result.returncode == 0, result.stderr
    assert pane_ran(rec, window_id_of(result.stdout)), result.stdout
    session = tmp_path / "agents" / "sessions" / (rec / "session-id-env").read_text()
    fields = command_fields(session)
    assert fields["launch_command"] == "session start --tmux-target eng --keyed scout"
    assert "--session-id" not in fields["launch_command"]


# ---------- a fresh HOME: the record lands where the leaf rule says --------------

def test_a_fresh_home_records_under_the_tools_state_sessions_leaf(tmp_path, monkeypatch):
    """ST-285 do-006 (A2): the record's session dir is the sessions LEAF
    (settings.sessions_root -> leaf_path, ST-288's rule), never a literal
    `~/.session`. On a HOME with no `~/.session/` and no SESSIONS_DIR, a real
    launch writes launch/command under `~/.sessionkit/tools/state/sessions/`,
    makes nothing under `~/.session/`, and `session get` reads it back from
    the same place."""
    from sessiontools import get as get_mod
    from sessiontools import listing, settings
    from sessiontools.claude_code import Session

    home = tmp_path / "home"
    home.mkdir()
    result, rec, work = launch_e2e(tmp_path, "scout", "hello",
                                   extra_env={"HOME": str(home)},
                                   drop_env=("SESSIONS_DIR",))
    assert result.returncode == 0, result.stderr
    sid = (rec / "session-id-env").read_text()
    leaf = home / ".sessionkit" / "tools" / "state" / "sessions"
    assert (leaf / sid / RECORD_DIR / COMMAND_FILE).is_file(), sorted(
        str(p.relative_to(home)) for p in home.rglob("*"))
    assert not (home / ".session").exists()
    fields = command_fields(leaf / sid)
    assert fields["launch_command"] == "session start scout hello"
    assert shlex.split(fields["launch_dir"]) == [str(work)]

    # The read side resolves the same leaf: `session get` from the same HOME.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SESSIONS_DIR")
    assert settings.leaf_path("sessions") == (leaf, "new")
    row = listing.Row(Session(id=sid, harness="claude-code", busy=False, updated_at=0,
                              name="scout"), repo="r", tmux_socket=None,
                      tmux_pane=None, started=None)
    assert get_mod.launch_value(row, "launch_command") == "session start scout hello"
    assert get_mod.launch_value(row, "launch_dir") == str(work)
