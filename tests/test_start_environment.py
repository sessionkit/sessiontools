"""The declared-environment prelude, RUN in a real bash with a scrubbed
environment - an outcome, not a plan - plus host_environ and the dry-run
view of secrets. Ported from the old suite's environment section. The
`pass` backend, the 0600 file and the shred trap are test_start_secrets.py
(slice 4)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from start_support import TIMEOUT_S

from sessiontools.start import environment as e

BASH = shutil.which("bash") or "/bin/bash"
ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def run_prelude(prelude: str, cwd: str, env: dict | None = None):
    """Run a rendered prelude, then `exec env`: what comes back is the
    environment the LAUNCHED process would have had."""
    base = {"PATH": "/usr/bin:/bin", "HOME": cwd} if env is None else env
    result = subprocess.run([BASH, "-c", prelude + "\nexec /usr/bin/env\n"], cwd=cwd,
                            env=base, capture_output=True, text=True,
                            timeout=TIMEOUT_S, check=False)
    assert result.returncode == 0, result.stderr
    child = {}
    for line in result.stdout.splitlines():
        match = ENV_LINE.match(line)
        if match:
            child[match.group(1)] = match.group(2)
    return child, result.stderr


def env_from(**section) -> e.Environment:
    return e.parse_environment(section)


def test_declared_variables_reach_the_launched_process_only(tmp_path):
    before = dict(os.environ)
    child, _ = run_prelude(e.render_env_prelude(env_from(
        variables={"VOICEMODE_VOICES": "af_sky"})), str(tmp_path))
    assert child["VOICEMODE_VOICES"] == "af_sky"
    assert dict(os.environ) == before


def test_the_prelude_never_asks_tmux_to_set_anything():
    prelude = e.render_env_prelude(env_from(
        variables={"FOO": "bar"}, files=["~/.env"], secrets={"TOKEN": "pass:a/b"}),
        secret_file="/tmp/secrets")
    for forbidden in ("set-environment", "setenv", "update-environment"):
        assert forbidden not in prelude
    for line in prelude.splitlines():
        code = line.split("#", 1)[0]
        assert not re.search(r"(?<![\w/-])tmux(?![\w-])", code), line


def test_values_with_shell_metacharacters_survive_quoting(tmp_path):
    nasty = "a b$(touch /tmp/pwned) 'c' \"d\" \\e"
    child, _ = run_prelude(e.render_env_prelude(env_from(variables={"NASTY": nasty})),
                           str(tmp_path))
    assert child["NASTY"] == nasty


def test_project_local_file_wins_over_agent_level_file(tmp_path):
    agent_env = tmp_path / "agent.env"
    agent_env.write_text("VOICEMODE_VOICES=agent_voice\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".voicemode.env").write_text("VOICEMODE_VOICES=project_voice\n")
    child, _ = run_prelude(e.render_env_prelude(env_from(
        files=[str(agent_env), "./.voicemode.env"])), str(project))
    assert child["VOICEMODE_VOICES"] == "project_voice"


def test_relative_paths_are_left_for_the_shell_and_others_expanded():
    prelude = e.render_env_prelude(env_from(files=["~/.claude/a/.env", "./.env"]))
    assert "./.env" in prelude
    assert "~" not in prelude
    assert os.path.expanduser("~/.claude/a/.env") in prelude


def test_files_source_in_declared_order_with_later_winning(tmp_path):
    first = tmp_path / "first.env"
    first.write_text("SHARED=first\nONLY_FIRST=1\n")
    second = tmp_path / "second.env"
    second.write_text("SHARED=second\n")
    child, _ = run_prelude(e.render_env_prelude(env_from(files=[str(first), str(second)])),
                           str(tmp_path))
    assert child["SHARED"] == "second" and child["ONLY_FIRST"] == "1"


def test_sourced_variables_are_exported_to_the_launched_process(tmp_path):
    path = tmp_path / "a.env"
    path.write_text("PLAIN_ASSIGNMENT=visible\n")
    child, _ = run_prelude(e.render_env_prelude(env_from(files=[str(path)])), str(tmp_path))
    assert child["PLAIN_ASSIGNMENT"] == "visible"


def test_a_missing_declared_file_is_loud_and_a_missing_local_override_is_not(tmp_path):
    missing = str(tmp_path / "nope.env")
    child, stderr = run_prelude(e.render_env_prelude(env_from(
        files=[missing, "./.voicemode.env"])), str(tmp_path))
    assert "nope.env" in stderr
    assert ".voicemode.env" not in stderr
    assert child


def test_the_prelude_reads_only_what_the_agent_declared():
    """The token-fallback trap is dead: no path the agent did not declare."""
    prelude = e.render_env_prelude(env_from(variables={"A": "1"}))
    assert ".env" not in prelude and "token" not in prelude.lower()


# ---------- secrets, as a dry run sees them -----------------------------------

def test_a_dry_run_names_secrets_and_resolves_none():
    assert e.declared_secrets({"DEMO_TOKEN": "pass:demo/token"}) == [
        e.DeclaredSecret("DEMO_TOKEN", "pass", "demo/token")]


def test_the_secret_prelude_sources_the_file_last_and_removes_it(tmp_path):
    secret = tmp_path / "secrets.env"
    secret.write_text("export TOKEN=from-secret\n")
    local = tmp_path / "a.env"
    local.write_text("TOKEN=from-env-file\n")
    child, _ = run_prelude(e.render_env_prelude(
        env_from(files=[str(local)], secrets={"TOKEN": "pass:x"}),
        secret_file=str(secret)), str(tmp_path))
    assert child["TOKEN"] == "from-secret"      # a stray .env line cannot shadow it
    assert not secret.exists()


def test_a_sourced_env_file_carrying_a_trap_warns(tmp_path, capsys):
    trapped = tmp_path / "trapped.env"
    trapped.write_text("export A=1\ntrap 'echo bye' EXIT\n")
    clean = tmp_path / "clean.env"
    clean.write_text("export B=2\n")
    e.warn_env_file_traps(e.Environment({}, [str(trapped), str(clean)], {"T": "pass:x"}),
                          str(tmp_path))
    err = capsys.readouterr().err
    assert "last-one-wins" in err and str(trapped) in err and str(clean) not in err
    e.warn_env_file_traps(e.Environment({}, [str(trapped)], {}), str(tmp_path))
    assert capsys.readouterr().err == ""


# ---------- host_environ -------------------------------------------------------

def test_host_environ_scrubs_the_launchers_own_venv():
    venv = "/tmp/fake-venv"
    scrubbed = e.host_environ({"VIRTUAL_ENV": venv,
                               "PATH": os.pathsep.join([venv + "/bin", "/usr/bin", "/bin"]),
                               "HOME": "/tmp/h"})
    assert "VIRTUAL_ENV" not in scrubbed
    assert scrubbed["PATH"] == os.pathsep.join(["/usr/bin", "/bin"])
    assert scrubbed["HOME"] == "/tmp/h"


def test_host_environ_without_a_venv_is_a_plain_copy():
    environ = {"PATH": "/usr/bin", "HOME": "/tmp/h"}
    assert e.host_environ(environ) == environ
    assert e.host_environ(environ) is not environ
