"""The acts: `main`, the dry-run report (and its golden), `execute`, own-
window naming, and the verb end to end against a stub `claude`. Ported from
the old suite's dry-run, golden, direct-launch e2e, do-009, window-naming
and Fable-subprocess sections. Parity checks 1 (the golden) and 4 (the six
refusals exit 2) live here."""

from __future__ import annotations

import glob
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from start_support import (
    FIXTURES,
    TIMEOUT_S,
    VENDOR_STUB,
    clean_env,
    launch_e2e,
    make_agents_root,
    plan_for,
    recorded_argv,
    session_cmd,
    without_known_paths,
    write_key_init_stub,
)

from sessiontools import start
from sessiontools.start import run
from sessiontools.start.agents import Agent
from sessiontools.start.environment import SESSION_IDENTITY_VARS
from sessiontools.start.errors import EXIT_OK, EXIT_USAGE, LaunchError

# ---------- main, in-process: dry runs ONLY (a live main execs the runner) ---------


def dry_run_main(tmp_path, monkeypatch, *argv, front_matter="claude:\n  effort: high\n"):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path, front_matter=front_matter))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    return start.main(["--dry-run", *argv])


def test_dry_run_prints_a_provenance_annotated_command(tmp_path, monkeypatch, capsys):
    assert dry_run_main(tmp_path, monkeypatch, "scout", "hello", "--model=opus") == EXIT_OK
    out = capsys.readouterr().out
    for part in ("DRY RUN", "<- computed: vendor table (claude)",
                 "<- computed: session identity (minted by the launcher)",
                 "<- front matter: claude: effort", "<- CLI passthrough",
                 "launch script:", "exec claude"):
        assert part in out


def test_every_command_token_in_dry_run_output_carries_a_source(tmp_path, monkeypatch, capsys):
    assert dry_run_main(tmp_path, monkeypatch, "scout") == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    command = []
    for line in lines[lines.index("command:") + 1:]:
        if not line.startswith("  "):
            break
        command.append(line)
    assert command and all("  <- " in line for line in command)


def test_dry_run_says_where_and_why(tmp_path, monkeypatch, capsys):
    home = tmp_path / "scout-home"
    home.mkdir()
    dry_run_main(tmp_path, monkeypatch, "scout", front_matter=f"start:\n  directory: {home}\n")
    assert f"workdir:  {home}  (agent file: start: directory:)" in capsys.readouterr().out


def test_no_agent_is_a_plain_vendor_launch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "agents" / "sessions"))
    monkeypatch.chdir(tmp_path)
    assert start.main(["--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "agent:    (none -- a plain claude session" in out
    assert "--session-id" in out and "--agent" not in out
    assert str(tmp_path / "agents" / "sessions") in out


def test_dash_names_no_agent_and_the_rest_passes_through(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.chdir(tmp_path)
    assert start.main(["--dry-run", "-", "hello", "world", "--model", "x"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "--model" in out and "hello world" in out and "--agent" not in out


def test_an_unknown_agent_is_the_resolution_error(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path))
    assert start.main(["--dry-run", "nosuch-agent"]) == EXIT_USAGE
    assert "nosuch-agent" in capsys.readouterr().err


def test_list_agents_answers_before_anything_else(tmp_path, monkeypatch, capsys):
    root = make_agents_root(tmp_path)
    monkeypatch.setenv("AGENTS_DIR", root)
    assert start.main(["--list-agents", "--in-tmux"]) == EXIT_OK
    assert capsys.readouterr().out == "scout\n"


def test_help_exits_zero_and_marks_the_agent_optional(capsys):
    assert start.main(["-h"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[agent]" in out and "plain vendor session" in out
    assert "--identity-from-env" not in out and "--window-name" not in out


def test_a_failed_generator_is_named_on_stderr_before_the_report(tmp_path, monkeypatch, capsys):
    dry_run_main(tmp_path, monkeypatch, "scout",
                 front_matter="start:\n  context:\n    system-prompt:\n      - exit 3\n")
    err = capsys.readouterr().err
    assert "session start: context: running `exit 3`" in err
    assert "context generator failed (exit 3): exited 3" in err


# ---------- execute: the files, then the exec ----------------------------------------------

class Exec(Exception):
    pass


def test_execute_writes_the_planned_files_then_execs_the_script(tmp_path, monkeypatch):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n      - echo hi\n",
                    argv_tail=["w" * 10], flags=["--first-message-file-over", "4"])
    seen = []

    def fake_exec(file, argv, env):
        # What the exec finds, read AT the exec: a real one never comes
        # back, and this stand-in comes back by RAISING, a failure
        # `execute` cleans up (a stand-in that returns is one too).
        modes = {item.path: (os.path.isdir(item.path), stat.S_IMODE(os.stat(item.path).st_mode))
                 for item in plan.files if os.path.exists(item.path)}
        seen.append((file, argv, modes,
                     sorted(os.listdir(os.path.join(plan.identity.session_dir, "launch")))))
        raise Exec
    monkeypatch.setattr(os, "execvpe", fake_exec)
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)
    with pytest.raises(Exec):
        run.execute(plan)
    file, argv, modes, names = seen[0]
    assert (file, argv) == ("bash", ["bash", "-c", plan.script])
    assert len(plan.files) == 3
    assert set(modes) == {item.path for item in plan.files}      # every one, at the exec
    for is_dir, mode in modes.values():
        assert mode == (0o700 if is_dir else 0o600)
    assert sorted(modes.values()) == [(False, 0o600), (False, 0o600), (True, 0o700)]
    assert [n.split("-")[2] for n in names if n.startswith("session-start-")] == [
        "agent", "first", "system"]
    assert ".gitignore" in names
    assert not any(os.path.exists(item.path) for item in plan.files)   # cleaned up


# ---------- 4.6: the launcher's own files live under the session's launch/ ------------------

def test_a_live_plan_puts_its_door_files_under_launch(tmp_path):
    """session-settings 4.6 (a review of 4.1,
    2030-12-08): the prompt file, the first message and the agent plugin
    sit in `<session>/launch/`, beside the record, not at the dir's top."""
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n      - echo hi\n",
                    argv_tail=["w" * 10], flags=["--first-message-file-over", "4"])
    launch_dir = os.path.join(plan.identity.session_dir, "launch")
    assert len(plan.files) == 3
    assert {os.path.dirname(item.path) for item in plan.files} == {launch_dir}
    assert all(launch_dir + "/" in token for token in plan.argv
               if isinstance(token, str) and "session-start-" in token
               and not token.startswith("Your first message"))
    assert not os.path.exists(launch_dir)          # planning writes nothing


def test_write_files_makes_launch_private_and_ignored_and_the_record_still_follows(
        tmp_path, monkeypatch):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n      - echo hi\n")
    written = run.write_files(plan)
    launch_dir = Path(plan.identity.session_dir, "launch")
    assert stat.S_IMODE(launch_dir.stat().st_mode) == 0o700
    assert (launch_dir / ".gitignore").read_text() == "*\n"
    assert all(os.path.exists(item.path) for item in plan.files)
    # The script's record comes after the exec, into the dir that now exists.
    from sessiontools.start import record
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    assert record.write_record(plan.identity.session_dir, env, ["claude"]) == str(launch_dir)
    for name in record.RECORD_FILES:
        assert (launch_dir / name).is_file(), name
    run.discard_files([item for item in written if not isinstance(item, run.MadeDir)])


def test_a_failed_launch_takes_the_launch_dir_it_made(tmp_path, monkeypatch):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n      - echo hi\n")
    monkeypatch.setattr(os, "execvpe", lambda *a: (_ for _ in ()).throw(OSError(2, "no bash")))
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)
    with pytest.raises(LaunchError):
        run.execute(plan)
    assert not os.path.exists(os.path.join(plan.identity.session_dir, "launch"))
    assert os.path.isdir(plan.identity.session_dir)    # the minted dir itself stays


def test_a_failed_exec_takes_its_files_with_it(tmp_path, monkeypatch):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n      - echo hi\n")
    monkeypatch.setattr(os, "execvpe", lambda *a: (_ for _ in ()).throw(OSError(2, "no bash")))
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)
    with pytest.raises(LaunchError) as excinfo:
        run.execute(plan)
    assert "exec failed" in str(excinfo.value)
    assert all(not os.path.exists(item.path) for item in plan.files)


def test_a_write_that_fails_takes_the_earlier_files_with_it(tmp_path, monkeypatch):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n      - echo hi\n")
    real = run.materialize
    calls = []

    def second_fails(item):
        calls.append(item)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(item)
    monkeypatch.setattr(run, "materialize", second_fails)
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("must not exec"))
    with pytest.raises(LaunchError) as excinfo:
        run.execute(plan)
    assert "could not write" in str(excinfo.value)
    assert all(not os.path.exists(item.path) for item in plan.files)


def test_a_dry_run_plan_is_never_executed(tmp_path, monkeypatch):
    plan = plan_for(tmp_path, "", flags=["--dry-run"])
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("must not exec"))
    with pytest.raises(LaunchError):
        run.execute(plan)


# ---------- own-window naming (by design) ---------------------------------------------------

def _agent(name):
    return Agent(name=name, file="", directory="", home="")


def fake_tmux(answer, calls):
    def tmux(arguments, environ=None):
        calls.append(list(arguments))
        return answer if arguments[0] == "display-message" else ""
    return tmux


def test_a_shell_window_is_named_after_the_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "run_tmux", fake_tmux("bash\tbash\n", calls))
    assert run.name_own_window(_agent("alice"), "claude",
                               environ={"TMUX": "/x,1,2", "TMUX_PANE": "%0"}) == "alice"
    assert calls[-1] == ["rename-window", "-t", "%0", "alice"]


def test_a_window_someone_named_is_never_renamed(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "run_tmux", fake_tmux("work\tbash\n", calls))
    assert run.name_own_window(_agent("alice"), "claude",
                               environ={"TMUX": "/x,1,2", "TMUX_PANE": "%0"}) is None
    assert all(call[0] != "rename-window" for call in calls)


def test_outside_tmux_nothing_is_touched(monkeypatch):
    monkeypatch.setattr(run, "run_tmux", lambda *a, **k: pytest.fail("no tmux outside tmux"))
    assert run.name_own_window(_agent("alice"), "claude", environ={}) is None
    assert run.name_own_window(_agent("alice"), "claude", environ={"TMUX": "/x"}) is None


def test_window_naming_fails_open(monkeypatch):
    def broken(arguments, environ=None):
        raise LaunchError("tmux display-message failed: no server")
    monkeypatch.setattr(run, "run_tmux", broken)
    assert run.name_own_window(_agent("alice"), "claude",
                               environ={"TMUX": "/x,1,2", "TMUX_PANE": "%0"}) is None


def test_a_plain_session_window_is_named_after_the_vendor(monkeypatch):
    calls = []
    monkeypatch.setattr(run, "run_tmux", fake_tmux("bash\tbash\n", calls))
    assert run.name_own_window(_agent(""), "claude",
                               environ={"TMUX": "/x,1,2", "TMUX_PANE": "%3"}) == "claude"


# ---------- end to end: the exec is real (a stub claude on PATH) ------------------------------

def test_direct_launch_execs_the_stub_vendor_with_identity_env(tmp_path):
    result, record, work = launch_e2e(tmp_path, "scout", "hello", "world")
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    sid = (record / "session-id-env").read_text()
    assert (record / "cwd").read_text() in (str(work.resolve()), str(work))
    assert sid and argv[argv.index("--session-id") + 1] == sid
    assert argv[argv.index("--agent") + 1] == "scout"
    assert argv[argv.index("--effort") + 1] == "high"
    assert "hello world" in argv[-1] and argv.index("--") == len(argv) - 2
    assert (record / "virtual-env").read_text() == ""       # no venv leak
    assert (record / "suggestion-env").read_text() == "false"   # SESH-734
    plugin = argv[argv.index("--plugin-dir") + 1]
    assert os.path.isfile(os.path.join(plugin, "agents", "scout.md"))


#: The stub vendor, plus one line per SESSION_IDENTITY_VARS name: what the
#: exec'd agent process actually holds (`<unset>` when the name is absent).
IDENTITY_VENDOR_STUB = VENDOR_STUB + "".join(
    f'printf \'%%s\' "${{{name}-<unset>}}" > "%(dir)s/env-{name}"\n'
    for name in SESSION_IDENTITY_VARS)
PARENT = "11111111-2222-4333-8444-555555555555"


def test_a_direct_launch_inside_a_seat_hands_the_agent_none_of_the_parents_identity(
        tmp_path):
    """ST-113 (b), behaviourally, as repro-001 S2 measured it: the direct path
    execs the script with the CALLER's environment (`run.py` `execvpe(...,
    host_environ())`), so the script's first `unset` is the only thing between
    a seat's identity and the agent. The caller holds PARENT in both id names
    and the three key handles; the exec'd vendor must hold the NEW
    CLAUDE_SESSION_ID and none of the rest. Fails with that unset line
    reverted to the pre-ST-113 list (CLAUDE_CODE_SESSION_ID passes through)."""
    parent = {name: PARENT if name.endswith("_SESSION_ID") else f"/parent/{name}"
              for name in SESSION_IDENTITY_VARS}
    result, record, _work = launch_e2e(tmp_path, "scout", extra_env=parent,
                                       stub_body=IDENTITY_VENDOR_STUB)
    assert result.returncode == 0, result.stderr
    seen = {name: (record / f"env-{name}").read_text() for name in SESSION_IDENTITY_VARS}
    new_id = seen["CLAUDE_SESSION_ID"]
    assert new_id not in ("", "<unset>", PARENT)
    argv = recorded_argv(record)
    assert argv[argv.index("--session-id") + 1] == new_id
    assert seen == {name: new_id if name == "CLAUDE_SESSION_ID" else "<unset>"
                    for name in SESSION_IDENTITY_VARS}


#: A keyed seat's git signing config: session-key-init's env block
#: (session-key-init:305-317), with a made-up public key. git reads it from
#: the ENVIRONMENT, so whoever inherits these names signs as the parent.
PARENT_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIParentParentParentParentParentParentPar parent"
PARENT_SIGNING = {
    "GIT_CONFIG_COUNT": "6",
    "GIT_CONFIG_KEY_0": "gpg.format", "GIT_CONFIG_VALUE_0": "ssh",
    "GIT_CONFIG_KEY_1": "user.signingkey", "GIT_CONFIG_VALUE_1": f"key::{PARENT_PUB}",
    "GIT_CONFIG_KEY_2": "commit.gpgsign", "GIT_CONFIG_VALUE_2": "true",
    "GIT_CONFIG_KEY_3": "tag.gpgsign", "GIT_CONFIG_VALUE_3": "true",
    "GIT_CONFIG_KEY_4": "gpg.ssh.program", "GIT_CONFIG_VALUE_4": "/parent/ssh-keygen-session",
    "GIT_CONFIG_KEY_5": "gpg.ssh.allowedSignersFile",
    "GIT_CONFIG_VALUE_5": "/parent/allowed_signers",
}
#: The agent's git reads no config FILE the runner chose (ST-120): a seat's
#: GIT_CONFIG_GLOBAL (one agent's sets commit.gpgsign=false), a HOME ~/.gitconfig
#: or the system file would answer the `git config --get` the signing tests
#: expect `<unset>` from. Pinned in the launch env, it reaches the agent with
#: the rest, so only the env block under test can set a signing key.
HERMETIC_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
#: The indexed env-config family git reads pairs from; GIT_CONFIG_GLOBAL and
#: the other GIT_CONFIG_* names are not part of it.
INDEXED_GIT_CONFIG = re.compile(r"GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+)=")
#: The git config keys the block sets, as the agent's own `git config` sees them.
SIGNING_KEYS = ("gpg.format", "user.signingkey", "commit.gpgsign", "tag.gpgsign",
                "gpg.ssh.program", "gpg.ssh.allowedSignersFile")
#: The stub vendor, plus what the agent's git would make of each signing key
#: (`<unset>` when git has no value for it).
SIGNING_VENDOR_STUB = VENDOR_STUB + "".join(
    f'printf \'%%s\' "$(git config --get {key} || echo \'<unset>\')" > "%(dir)s/git-{key}"\n'
    for key in SIGNING_KEYS)


def test_an_unkeyed_direct_launch_inside_a_keyed_seat_hands_the_agent_no_signing_config(
        tmp_path):
    """ST-118, as repro-001 S1 measured it: the direct path execs the script
    with the caller's environment, and the script's first `unset` names the
    session ids and key handles but none of the GIT_CONFIG_* names. The
    agent's git then reads the PARENT's signing key, gpg.ssh.program and
    commit.gpgsign. With the key handle (CLAUDE_SESSION_AGENT_SOCK) scrubbed,
    every commit and annotated tag it makes fails in the parent's shim
    (findings/2030-12-08-repro-001.md R1, S1)."""
    parent = {**PARENT_SIGNING, **HERMETIC_GIT, "CLAUDE_SESSION_AGENT_SOCK": "/parent/agent.sock",
              "CLAUDE_SESSION_PUB": "/parent/session.pub", "CLAUDE_SESSION_ID": PARENT}
    result, record, _work = launch_e2e(tmp_path, "scout", extra_env=parent,
                                       stub_body=SIGNING_VENDOR_STUB)
    assert result.returncode == 0, result.stderr
    assert (record / "session-id-env").read_text() not in ("", PARENT)
    seen = {key: (record / f"git-{key}").read_text() for key in SIGNING_KEYS}
    assert seen == dict.fromkeys(SIGNING_KEYS, "<unset>")


def test_an_agent_that_declares_git_env_config_gets_it_over_the_parents_block(tmp_path):
    """ST-118 rca-001 criterion 7, in the agent: the script's `unset` takes
    the parent's whole indexed family first, the declared prelude comes
    after, so an agent that asks for env git config in its own file gets
    exactly that, and none of the parent's other pairs."""
    declared = ("environment:\n  variables:\n    GIT_CONFIG_COUNT: '1'\n"
                "    GIT_CONFIG_KEY_0: gpg.format\n    GIT_CONFIG_VALUE_0: openpgp\n")
    stub = SIGNING_VENDOR_STUB + 'env | grep "^GIT_CONFIG_" | sort > "%(dir)s/git-env"\n'
    result, record, _work = launch_e2e(tmp_path, "scout", front_matter=declared,
                                       extra_env={**PARENT_SIGNING, **HERMETIC_GIT},
                                       stub_body=stub)
    assert result.returncode == 0, result.stderr
    seen = {key: (record / f"git-{key}").read_text() for key in SIGNING_KEYS}
    assert seen == {**dict.fromkeys(SIGNING_KEYS, "<unset>"), "gpg.format": "openpgp"}
    # git ignores pairs past COUNT, so the names themselves: the parent's
    # KEY_1..5 / VALUE_1..5 are gone, not merely shadowed. Only the indexed
    # family: GIT_CONFIG_GLOBAL and friends are the runner's, not the block's.
    names = (record / "git-env").read_text().splitlines()
    assert [line for line in names if INDEXED_GIT_CONFIG.match(line)] == [
        "GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=gpg.format", "GIT_CONFIG_VALUE_0=openpgp"]
    assert f"GIT_CONFIG_GLOBAL={os.devnull}" in names     # the pin reached the agent


def test_a_live_launch_writes_its_session_dir_and_0600_prompt_file(tmp_path):
    result, _record, _work = launch_e2e(
        tmp_path, "scout", front_matter="start:\n  context:\n    system-prompt:\n"
                                        "      - echo standing briefing\n")
    assert result.returncode == 0, result.stderr
    sessions = tmp_path / "agents" / "sessions"
    prompts = glob.glob(str(sessions / "*" / "launch" / "session-start-system-prompt-*.md"))
    assert len(prompts) == 1          # under launch/ since session-settings 4.6
    assert not glob.glob(str(sessions / "*" / "session-start-*"))   # none at the top
    assert stat.S_IMODE(os.stat(prompts[0]).st_mode) == 0o600
    assert "standing briefing" in Path(prompts[0]).read_text()
    assert glob.glob(str(sessions / "*" / "session-id"))


def test_direct_launch_idle_injects_only_the_boot_trigger(tmp_path):
    result, record, _work = launch_e2e(tmp_path, "--idle", "scout")
    assert result.returncode == 0, result.stderr
    assert any("Boot from your agent MD now" in arg for arg in recorded_argv(record))


@pytest.mark.parametrize("bare", [(), ("-",)], ids=["no-agent", "dash"])
def test_a_bare_idle_launch_injects_no_boot_line(tmp_path, bare):
    result, record, _work = launch_e2e(tmp_path, "--idle", *bare)
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    for word in ("agent MD", "startup manifest"):
        assert not any(word in arg for arg in argv), argv
    sessions = tmp_path / "agents" / "sessions"
    for path in sessions.rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert "agent MD" not in text and "startup manifest" not in text, path


def test_a_bare_idle_launch_with_words_sends_only_the_words(tmp_path):
    result, record, _work = launch_e2e(tmp_path, "--idle", "-", "stand", "by")
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert not any("agent MD" in arg for arg in argv), argv
    assert any(arg.strip() == "stand by" for arg in argv), argv


def test_idle_launches_with_the_agents_own_door_intact(tmp_path):
    result, record, _work = launch_e2e(tmp_path, "--idle", "scout", front_matter=DOUBLE_DOOR)
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert argv.count("--append-system-prompt-file") == 1
    assert argv[argv.index("--append-system-prompt-file") + 1] == "/tmp/mine.md"


def test_an_unrelated_passthrough_flag_still_reaches_the_vendor(tmp_path):
    result, record, _work = launch_e2e(tmp_path, "scout", "--effort", "max", "--x=1")
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    at = [i for i, arg in enumerate(argv) if arg == "--effort"]
    assert [argv[i + 1] for i in at] == ["high", "max"] and "--x=1" in argv


def test_a_keyed_live_launch_mints_through_the_primitive(tmp_path):
    stub, record_file = write_key_init_stub(tmp_path)
    result, record, _work = launch_e2e(tmp_path, "--keyed", "scout",
                                       extra_env={"SESSION_KEY_INIT": stub})
    assert result.returncode == 0, result.stderr
    assert (record / "session-id-env").read_text() == "deadbeef00000000"
    argv = Path(record_file).read_text().split()
    assert argv[argv.index("--reap-pid") + 1].isdigit()


UUID_SHAPED = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"


def test_a_keyed_dry_run_runs_no_primitive_and_creates_nothing(tmp_path):
    stub, record_file = write_key_init_stub(tmp_path)
    result, record, _work = launch_e2e(tmp_path, "--dry-run", "--keyed", "scout",
                                       extra_env={"SESSION_KEY_INIT": stub})
    assert result.returncode == 0, result.stderr
    assert not os.path.exists(record_file)
    assert not (record / "argv").exists()
    assert not (tmp_path / "agents" / "sessions").exists()
    out = result.stdout
    assert "NOT RUN" in out and "not created" in out
    assert "<keyed-session-id-minted-at-launch>" in out
    # no MINTED id: the paths the run did not make (interpreter, checkout,
    # tmp_path, $HOME) may sit under a uuid-named dir (ST-110), so only they go
    assert not re.search(UUID_SHAPED, without_known_paths(out, tmp_path))


def test_known_paths_go_but_an_id_minted_beneath_them_stays(tmp_path, monkeypatch):
    """The ST-110 scrub removes a uuid only where it is part of a path the
    run did not mint; one appended after such a path, or standing alone,
    still matches, so the dry-run check above cannot pass by scrubbing."""
    scratch = tmp_path / "3a26c166-3796-4a2c-9f1e-0123456789ab" / "scratchpad"
    monkeypatch.setenv("HOME", str(scratch / "home"))
    monkeypatch.setattr(sys, "executable", str(scratch / "venv" / "bin" / "python"))
    minted = "0f1e2d3c-4b5a-4968-8776-655443322110"
    assert not re.search(UUID_SHAPED, without_known_paths(
        f"{sys.executable} -m x --file c={scratch}/home/.claude/CLAUDE.md", scratch))
    for leak in (f"{scratch}/agents/sessions/{minted}/launch",
                 f"{sys.executable} --session-id {minted}",
                 f"session:  {minted}"):
        assert re.search(UUID_SHAPED, without_known_paths(leak, scratch)), leak


def test_a_dry_run_creates_no_session_directory_and_no_file(tmp_path):
    result, record, _work = launch_e2e(
        tmp_path, "--dry-run", "scout", "hi",
        front_matter="start:\n  context:\n    system-prompt:\n      - echo x\n")
    assert result.returncode == 0, result.stderr
    assert not (record / "argv").exists()
    assert not glob.glob(str(tmp_path / "**" / "session-id"), recursive=True)
    assert not glob.glob(str(tmp_path / "**" / "session-start-*"), recursive=True)
    assert "--append-system-prompt-file" in result.stdout and "system-prompt.md" in result.stdout


def test_a_dry_run_names_secrets_and_calls_no_backend(tmp_path):
    bin_dir = tmp_path / "pass-bin"
    bin_dir.mkdir()
    called = tmp_path / "pass-called"
    (bin_dir / "pass").write_text(f"#!/usr/bin/env bash\necho called > {called}\necho x\n")
    (bin_dir / "pass").chmod(0o755)
    result, record, _work = launch_e2e(
        tmp_path, "--dry-run", "scout",
        front_matter="environment:\n  secrets:\n    DEMO_TOKEN: pass:demo/token\n",
        extra_env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert not called.exists() and not (record / "argv").exists()
    assert "DEMO_TOKEN  <- pass:demo/token" in result.stdout
    assert "backends not called" in result.stdout
    assert "using the bridge" not in result.stderr


# ---------- parity check 4: the six refusals exit 2, and nothing launches ---------------------

DOUBLE_DOOR = ("claude:\n  append-system-prompt-file: /tmp/mine.md\n"
               "start:\n  context:\n    system-prompt:\n      - printf 'INJECTED\\n'\n")


@pytest.mark.parametrize("name, argv, front_matter, stderr_has", [
    ("the position trap", ["scout", "--dry-run"], "claude:\n  effort: high\n",
     "session start --dry-run scout"),
    ("an unknown agent", ["nosuch"], "", "nosuch.md (file agent)"),
    ("a bad start: key", ["scout"], "start:\n  keyd: true\n", "unknown key(s) under `start:`"),
    ("a non-existent --directory", ["--directory", "/no/such/dir", "scout"], "",
     "not a directory -- refusing"),
    ("a duplicated door", ["scout"], DOUBLE_DOOR, "is opened twice"),
    ("Fable", ["scout", "--model", "fable"], "", "Fable is not enabled here"),
])
def test_the_six_refusals_exit_2_and_nothing_launches(tmp_path, name, argv, front_matter,
                                                      stderr_has):
    result, record, _work = launch_e2e(tmp_path, *argv, front_matter=front_matter)
    assert result.returncode == 2, (name, result.returncode, result.stderr)
    assert not (record / "argv").exists(), f"{name}: the vendor ran"
    assert stderr_has in result.stderr, (name, result.stderr)
    assert result.stdout == ""
    sessions = tmp_path / "agents" / "sessions"
    assert not sessions.exists() or list(sessions.iterdir()) == []


def test_the_fable_refusal_asks_through_message_send(tmp_path):
    result, _record, _work = launch_e2e(
        tmp_path, "scout", "--model", "fable",
        extra_env={"AGENT_OPERATOR_ADDR": "you@elsewhere.example"})
    assert result.returncode == 2
    assert "message send you@elsewhere.example" in result.stderr
    assert "session mail" not in result.stderr


def test_a_message_containing_the_word_fable_still_launches(tmp_path):
    result, record, _work = launch_e2e(tmp_path, "scout", "ask", "about", "fable")
    assert result.returncode == 0, result.stderr
    assert "fable" in recorded_argv(record)[-1]


# ---------- parity check 1: the direct golden ----------------------------------------------

GOLDEN_FRONT_MATTER = """\
start:
  vendor: claude
  context:
    system-prompt:
      - "echo golden system prompt"
    initial:
      - "echo golden briefing"
environment:
  variables:
    DEMO_MODE: "on"
  secrets:
    DEMO_TOKEN: pass:demo/token
claude:
  effort: high
  add-dir:
    - /shared/tools
"""


def normalize_golden(text: str, tmp_path: Path) -> str:
    """Machine-specific text out: the tmp root, the session id, the date,
    and - since ST-18's launch-record line - this interpreter, which the
    record call runs and its start command names (`session_cmd`)."""
    text = text.replace(sys.executable, "<PYTHON>")
    text = text.replace(str(tmp_path), "<TMP>")
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                  "<SESSION-ID>", text)
    return re.sub(r"Current date and time: [^'\"\n]+", "Current date and time: <DATE>", text)


def test_golden_dry_run_direct(tmp_path):
    """The old suite's direct golden, run by the PORT (not the bridge), under
    the same synthetic `scout` harness: TZ=UTC, a test-owned HOME,
    AGENTS_DIR and TMPDIR. Byte-equal to the committed fixture: the old
    launcher's own, copied verbatim, plus the ST-18 launch-record lines (the
    report's `record:` line and the script's record call), which the old
    launcher never had."""
    root = make_agents_root(tmp_path, front_matter=GOLDEN_FRONT_MATTER)
    for sub in ("bin", "home", "work", "tmpdir"):
        (tmp_path / sub).mkdir()
    env = clean_env(AGENTS_DIR=root, HOME=str(tmp_path / "home"),
                    TMPDIR=str(tmp_path / "tmpdir"), TZ="UTC",
                    PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    env.pop("SESSIONS_DIR")          # the default root: <HOME>/.session/sessions
    result = subprocess.run(session_cmd("--dry-run", "scout", "review", "the", "auth"),
                            capture_output=True, text=True, timeout=TIMEOUT_S,
                            cwd=str(tmp_path / "work"), env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert "using the bridge" not in result.stderr     # the port ran, not the bridge
    actual = normalize_golden(result.stdout, tmp_path)
    expected = (FIXTURES / "dry-run-direct.golden").read_text(encoding="utf-8")
    assert actual == expected
    assert "/Users/" not in actual


def recorded_start_command(stdout: str) -> list[str]:
    """The `--start-command` the dry run's record call carries, split back
    into its words. The record call quotes it once more, so it is read as
    one shell word first; the rest of that line (the vendor argv, whose
    briefing runs on over later lines) is left unread."""
    line = next(line for line in stdout.splitlines() if " --start-command " in line)
    lexer = shlex.shlex(line.split(" --start-command ", 1)[1], posix=True)
    lexer.whitespace_split = True
    return shlex.split(lexer.get_token())


def test_the_recorded_start_command_names_this_interpreter_not_a_re_exec(tmp_path):
    """A macOS framework Python's venv stub re-execs the framework's
    `Python.app` binary, and `sys.orig_argv[0]` names that binary: it has
    none of the venv's packages, so a start command recorded from it cannot
    re-run. The record names `sys.executable` (the venv's python), then the
    rest of `sys.orig_argv`, flags included. The re-exec is faked in the
    child, so no framework build is needed."""
    fake = "/fake/Python.framework/Resources/Python.app/Contents/MacOS/Python"
    code = (f"import sys; sys.orig_argv[0] = {fake!r}; "
            "from sessiontools.cli import main; sys.exit(main())")
    root = make_agents_root(tmp_path, front_matter=GOLDEN_FRONT_MATTER)
    for sub in ("home", "work", "tmpdir"):
        (tmp_path / sub).mkdir()
    env = clean_env(AGENTS_DIR=root, HOME=str(tmp_path / "home"),
                    TMPDIR=str(tmp_path / "tmpdir"), TZ="UTC")
    env.pop("SESSIONS_DIR")          # the default root, as the direct golden
    result = subprocess.run([sys.executable, "-c", code, "start", "--dry-run", "scout"],
                            capture_output=True, text=True, timeout=TIMEOUT_S,
                            cwd=str(tmp_path / "work"), env=env, check=False)
    assert result.returncode == 0, result.stderr
    recorded = recorded_start_command(result.stdout)
    assert recorded[0] == sys.executable
    assert recorded[1:] == ["-c", code, "start", "--dry-run", "scout"]


# ---------- the parser refuses first ------------------------------------------------------

def test_the_trap_is_refused_by_the_parser(monkeypatch):
    """A refusal the parser can make is made before anything else:
    `session start bob --in-tmux` is the position trap, exit 2."""
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("must not exec"))
    assert start.main(["bob", "--in-tmux"]) == EXIT_USAGE
