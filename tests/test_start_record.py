"""ST-18: the launch record - env (secrets masked), command, startup files.

Planted fakes only: every "secret" below is made up in this file, and the
tests assert that its VALUE appears nowhere in what was written.
"""

from __future__ import annotations

import glob
import os
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
from start_support import launch_e2e, plan_for, recorded_argv, write_key_init_stub
from test_start_placement import pane_ran, tmux_launch_e2e

from sessiontools.start import record, run
from sessiontools.start.record import (
    COMMAND_FILE,
    ENV_FILE,
    GITIGNORE,
    RECORD_DIR,
    STARTUP_FILE,
    ensure_record_dir,
    git_ignore_state,
    is_secret_name,
    looks_secret_value,
    render_env,
    should_mask,
    vendor_startup_files,
    write_record,
)

#: Planted, 40 chars, mixed classes: a name rule AND the backstop would catch it.
FAKE_TOKEN = "abcDEF0123456789ghiJKL0123456789mnoPQR01"
#: Planted under an innocent name: only the backstop can catch it.
FAKE_INNOCENT = "Zq8xT2vLm9Rb4Kw7Yc1Hn6Ps3Gd5Fj0Ae8Ut2Wo4Xi"


def no_repo_env(tmp_path: Path) -> dict:
    """An environ whose git can never find a repo above tmp_path."""
    return {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
            "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)}


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True,
                          text=True, check=False)


def env_lines(directory: Path) -> dict:
    text = (directory / RECORD_DIR / ENV_FILE).read_text()
    return dict(line.split("=", 1) for line in text.splitlines())


# ---------- masking ------------------------------------------------------------

def test_a_planted_token_leaves_only_its_name_and_its_length():
    text = render_env({"FOO_TOKEN": FAKE_TOKEN, "HOME": "/h"})
    assert f"FOO_TOKEN={'*' * len(FAKE_TOKEN)}\n" in text
    assert FAKE_TOKEN not in text
    assert "HOME=/h\n" in text


@pytest.mark.parametrize("name", ["PATH", "CLAUDE_CODE_AGENT", "TM_AGENT_ADDRESS", "EMAIL",
                                  "CLAUDE_SESSION_ID", "TERM_SESSION_ID"])
def test_identity_stays_clear_even_when_it_looks_random(name):
    assert not should_mask(name, FAKE_INNOCENT)


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "OPENAI_API_KEY", "DB_PASSWORD", "MY_PASSWD",
                                  "CLIENT_SECRET", "SSH_AUTH_SOCK", "GITHUB_PAT",
                                  "GOOGLE_CREDENTIALS", "AWS_REGION", "aws_profile",
                                  "anthropic_api_key"])
def test_a_name_that_says_secret_is_masked(name):
    assert is_secret_name(name)


@pytest.mark.parametrize("name", ["PATH", "MANPATH", "PATHEXT", "GIT_AUTHOR_NAME",
                                  "GIT_AUTHOR_EMAIL", "HOME", "TERM", "SHELL"])
def test_a_plain_name_is_not(name):
    """PAT is a word, not a substring (PATH stays clear); AUTHOR is not AUTH."""
    assert not is_secret_name(name)


def test_an_empty_secret_shows_empty_so_set_versus_empty_is_visible():
    lines = render_env({"FOO_TOKEN": "", "AWS_REGION": "ap-southeast-2"}).splitlines()
    assert "FOO_TOKEN=" in lines
    assert "AWS_REGION=" + "*" * len("ap-southeast-2") in lines


def test_the_backstop_catches_a_credential_under_an_innocent_name():
    text = render_env({"INNOCENT": FAKE_INNOCENT})
    assert FAKE_INNOCENT not in text and text == f"INNOCENT={'*' * len(FAKE_INNOCENT)}\n"


@pytest.mark.parametrize("value", [
    "0123456789abcdef0123456789abcdef01234567",      # a sha: hex never passes 4 bits
    "6c2b1f0e-9a8d-4c7b-a6e5-d4c3b2a1f0e9",          # a uuid
    "/Users/someone/Code/a/very/long/path/to/somewhere/else",
    "short",
    "a value with many different words in it, all of them ordinary",
])
def test_the_backstop_leaves_ordinary_long_values_alone(value):
    assert not looks_secret_value(value)


def test_a_declared_secret_is_masked_whatever_its_name():
    text = render_env({"DEMO_MODE": "on"}, declared=("DEMO_MODE",))
    assert text == "DEMO_MODE=**\n"


def test_one_line_per_variable_even_when_a_value_has_newlines():
    text = render_env({"MULTI": "a\nb", "Z": "z"})
    assert text == "MULTI=a\\nb\nZ=z\n"


def test_names_are_sorted():
    assert [line.split("=")[0] for line in render_env({"b": "1", "A": "2", "a": "3"})
            .splitlines()] == ["A", "a", "b"]


# ---------- the write: modes, once, the git guard ----------------------------------

def write(tmp_path: Path, environ: dict | None = None, **kwargs) -> str | None:
    session = tmp_path / "session"
    session.mkdir(exist_ok=True)
    environ = {**no_repo_env(tmp_path), **(environ or {})}
    return write_record(str(session), environ, kwargs.pop("argv", ["claude", "-x"]),
                        **kwargs)


def test_the_record_is_0700_dir_and_0600_files(tmp_path):
    directory = write(tmp_path, start_command="session start scout", work_dir="/w",
                      files=[("agent-file", "/a/scout.md")])
    assert directory == str(tmp_path / "session" / RECORD_DIR)
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    for name in (GITIGNORE, *record.RECORD_FILES):
        assert stat.S_IMODE(os.stat(os.path.join(directory, name)).st_mode) == 0o600, name
    assert Path(directory, GITIGNORE).read_text() == "*\n"
    assert Path(directory, STARTUP_FILE).read_text() == "agent-file\t/a/scout.md\n"


def test_the_command_file_reads_back_as_argv(tmp_path):
    argv = ["claude", "--session-id", "sid", "--", "two words\nand a line"]
    directory = write(tmp_path, argv=argv, start_command="session start scout 'two words'",
                      work_dir="/a dir")
    fields = dict(line.split(": ", 1) for line in
                  Path(directory, COMMAND_FILE).read_text().split("\n") if ": " in line
                  and not line.startswith(" "))
    assert shlex.split(fields["start"]) == ["session", "start", "scout", "two words"]
    assert shlex.split(fields["cwd"]) == ["/a dir"]
    text = Path(directory, COMMAND_FILE).read_text()
    assert shlex.split(text.split("vendor: ", 1)[1].split("\nstart: ")[0]) == argv


def test_outside_any_repo_it_is_written(tmp_path):
    assert git_ignore_state(str(tmp_path / "x" / "env"), no_repo_env(tmp_path)) == "no-repo"
    assert write(tmp_path) is not None


def test_inside_a_repo_check_ignore_proves_the_record_ignored(tmp_path):
    """A real host's sessions root has been a repo that does not ignore
    session dirs: the
    record's own `.gitignore` has to carry it, and git has to agree."""
    repo = tmp_path / "sessions"
    repo.mkdir()
    assert git("init", "-q", cwd=repo).returncode == 0
    session = repo / "sid-1"
    session.mkdir()
    (session / "session-id").write_text("sid-1")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "FOO_TOKEN": FAKE_TOKEN}
    directory = write_record(str(session), env, ["claude"])
    assert directory is not None
    for name in record.RECORD_FILES:
        assert git("check-ignore", "-q", f"sid-1/{RECORD_DIR}/{name}",
                   cwd=repo).returncode == 0, name
    git("add", "-A", cwd=repo)
    staged = git("diff", "--cached", "--name-only", cwd=repo).stdout.split()
    assert staged == ["sid-1/session-id"], staged     # `git add -A` takes none of it


def test_a_repo_that_will_not_ignore_it_gets_nothing_but_the_gitignore(tmp_path, monkeypatch,
                                                                        capsys):
    monkeypatch.setattr(record, "git_ignore_state", lambda *a, **k: "not-ignored")
    assert write(tmp_path, {"FOO_TOKEN": FAKE_TOKEN}) is None
    directory = tmp_path / "session" / RECORD_DIR
    assert sorted(os.listdir(directory)) == [GITIGNORE]
    assert "NOT WRITTEN" in capsys.readouterr().err


def test_a_record_is_written_once_and_never_overwritten(tmp_path, capsys):
    first = write(tmp_path, {"A": "first"})
    before = Path(first, ENV_FILE).read_text()
    assert write(tmp_path, {"A": "second"}) is None
    assert Path(first, ENV_FILE).read_text() == before
    assert "left as it is" in capsys.readouterr().err


def test_a_launch_dir_holding_only_the_launchers_files_still_gets_its_record(tmp_path):
    """4.6: the prompt file and plugin go into launch/ before the exec, so
    "written once" is the record's own files, never the dir."""
    session = tmp_path / "session"
    session.mkdir()
    ensure_record_dir(str(session))
    (session / RECORD_DIR / "session-start-system-prompt-abcd1234.md").write_text("wake")
    directory = write_record(str(session), no_repo_env(tmp_path), ["claude"])
    assert directory == str(session / RECORD_DIR)
    assert sorted(os.listdir(directory)) == sorted(
        [GITIGNORE, "session-start-system-prompt-abcd1234.md", *record.RECORD_FILES])


def test_ensure_record_dir_never_makes_the_session_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        ensure_record_dir(str(tmp_path / "no-such-session"))


def test_main_never_fails_a_launch(tmp_path, capsys):
    assert record.main(["--dir", str(tmp_path / "missing" / "deeper"), "--", "claude"]) == 0
    assert "not written" in capsys.readouterr().err
    assert record.main(["--no-such-flag"]) == 0
    assert "nothing recorded" in capsys.readouterr().err


def test_main_takes_the_vendor_argv_after_the_separator(tmp_path):
    (tmp_path / "s").mkdir()
    assert record.main(["--dir", str(tmp_path / "s"), "--start-command", "session start x",
                        "--cwd", "/w", "--file", "plugin=/p", "--secret", "DEMO_MODE",
                        "--", "claude", "--file", "--", "msg"],
                       environ={**no_repo_env(tmp_path), "DEMO_MODE": "on"}) == 0
    text = (tmp_path / "s" / RECORD_DIR / COMMAND_FILE).read_text()
    assert text.startswith("vendor: claude --file -- msg\n")
    assert (tmp_path / "s" / RECORD_DIR / STARTUP_FILE).read_text() == "plugin\t/p\n"
    assert "DEMO_MODE=**" in (tmp_path / "s" / RECORD_DIR / ENV_FILE).read_text()


# ---------- the startup files ------------------------------------------------------

def test_the_vendor_layer_is_what_exists_root_first(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("u")
    (home / ".claude.json").write_text("{}")
    project = tmp_path / "project"
    work = project / "sub"
    (work / ".claude").mkdir(parents=True)
    (project / "CLAUDE.md").write_text("p")
    (work / "CLAUDE.md").write_text("w")
    (work / ".claude" / "settings.local.json").write_text("{}")
    (work / ".mcp.json").write_text("{}")
    found = vendor_startup_files("claude", str(work), str(home))
    assert found == [("claude-md", str(home / ".claude" / "CLAUDE.md")),
                     ("mcp", str(home / ".claude.json")),
                     ("claude-md", str(project / "CLAUDE.md")),
                     ("claude-md", str(work / "CLAUDE.md")),
                     ("settings", str(work / ".claude" / "settings.local.json")),
                     ("mcp", str(work / ".mcp.json"))]
    assert vendor_startup_files("nobody", str(work), str(home)) == []


# ---------- the plan: the script carries the call, on both identity paths -----------

LONG_CONTEXT = "start:\n  context:\n    system-prompt:\n      - echo standing\n"


def test_a_launcher_minted_script_records_before_the_exec(tmp_path):
    plan = plan_for(tmp_path, LONG_CONTEXT)
    assert plan.record is not None
    lines = plan.script.splitlines()
    call = next(i for i, line in enumerate(lines) if "from sessiontools.start.record import main" in line)
    assert lines[call - 1].startswith("# -> ") and RECORD_DIR in lines[call - 1]
    assert f"--dir {shlex.quote(plan.identity.session_dir)} " in lines[call]
    end_prelude = next(i for i, line in enumerate(lines) if "end declared environment" in line)
    exec_line = next(i for i, line in enumerate(lines) if line.startswith("exec "))
    assert end_prelude < call < exec_line
    tags = dict(plan.record.files)
    assert tags["agent-file"].endswith("scout.md")
    assert tags["system-prompt"].startswith(plan.identity.session_dir)
    assert tags["plugin"].startswith(plan.identity.session_dir)


def test_a_pane_minted_script_records_into_the_dir_the_pane_mints(tmp_path):
    stub, _record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, "", flags=("--in-tmux", "--keyed"),
                    extra_env={"SESSION_KEY_INIT": stub})
    assert plan.identity is None and plan.pane_mint is not None
    root = shlex.quote(plan.pane_mint.sessions_root)
    lines = plan.script.splitlines()
    call = next(i for i, line in enumerate(lines) if "from sessiontools.start.record import main" in line)
    assert f'--dir {root}/"$CLAUDE_SESSION_ID" ' in lines[call]
    mint = next(i for i, line in enumerate(lines) if "session-key-init failed" in line)
    assert mint < call


@pytest.mark.parametrize("keyed", [False, True])
def test_a_resume_records_nothing(tmp_path, keyed):
    """design.md: "skip" on resume - an unkeyed resume has no dir of its
    own; a keyed fork's record would part its dry run from the old launcher's."""
    stub, _record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, "", argv_tail=["--resume", "0f0f0f0f-0000-4000-8000-000000000000"],
                    flags=("--dry-run", *(("--keyed",) if keyed else ())),
                    extra_env={"SESSION_KEY_INIT": stub})
    assert (plan.identity is not None) is keyed
    assert plan.record is None
    assert "sessiontools.start.record" not in plan.script


def test_the_dry_run_report_names_the_record(tmp_path):
    plan = plan_for(tmp_path, "", flags=("--dry-run",))
    report = run.render_dry_run_report(plan)
    assert f"record:   {plan.identity.session_dir}/{RECORD_DIR}/" in report
    assert not os.path.exists(plan.identity.session_dir)


# ---------- end to end: a real launch writes it (criterion 1, 2) -------------------

def test_a_direct_launch_writes_the_record_with_the_planted_token_masked(tmp_path):
    result, rec, _work = launch_e2e(tmp_path, "scout", "hello",
                                   front_matter="claude:\n  effort: high\n" + LONG_CONTEXT,
                                   extra_env={"FOO_TOKEN": FAKE_TOKEN})
    assert result.returncode == 0, result.stderr
    assert "Warning" not in result.stderr and "launch record" not in result.stderr
    sessions = glob.glob(str(tmp_path / "agents" / "sessions" / "*" / RECORD_DIR))
    assert len(sessions) == 1
    session = Path(sessions[0]).parent
    for name in (GITIGNORE, *record.RECORD_FILES):
        assert stat.S_IMODE((session / RECORD_DIR / name).stat().st_mode) == 0o600
    env = env_lines(session)
    assert env["FOO_TOKEN"] == "*" * len(FAKE_TOKEN)
    assert env["CLAUDE_SESSION_ID"] == session.name          # the child's id, clear
    assert env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] == "false"   # the script's own exports
    # launch/ holds the launcher's prompt and plugin too (4.6): every file
    # under it, the plugin's included, is clear of the token.
    for path in session.joinpath(RECORD_DIR).rglob("*"):
        if path.is_file():
            assert FAKE_TOKEN not in path.read_text(), path
    command = (session / RECORD_DIR / COMMAND_FILE).read_text()
    vendor = shlex.split(command.split("vendor: ", 1)[1].split("\nstart: ")[0])
    assert vendor[0] == "claude"
    assert vendor[1:] == recorded_argv(rec)                 # what the vendor actually got
    assert "start scout hello" in command
    startup = (session / RECORD_DIR / STARTUP_FILE).read_text()
    assert "agent-file\t" in startup and "system-prompt\t" in startup
    assert "plugin\t" in startup


def test_a_pane_minted_launch_writes_the_record_in_the_pane(tmp_path):
    stub, _key_record = write_key_init_stub(tmp_path)
    result, rec, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "--keyed", "scout",
                                         extra_env={"SESSION_KEY_INIT": stub,
                                                    "FOO_TOKEN": FAKE_TOKEN})
    assert result.returncode == 0, result.stderr
    assert pane_ran(rec), result.stdout
    session = tmp_path / "agents" / "sessions" / (rec / "session-id-env").read_text()
    env = env_lines(session)
    assert env["FOO_TOKEN"] == "*" * len(FAKE_TOKEN)
    assert env["CLAUDE_SESSION_ID"] == session.name
    assert "agent-file\t" in (session / RECORD_DIR / STARTUP_FILE).read_text()
