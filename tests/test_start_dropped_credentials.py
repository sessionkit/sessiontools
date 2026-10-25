"""A credential the launch drops is named on stderr, never in silence.

`host_environ` drops CREDENTIAL_VARS and the script's first line unsets
them (`test_start_credentials.py`); an agent carries one only by declaring
it (`test_start_inherit.py`). A launcher that HOLDS one the agent file does
not declare now says so: one line per name, before the launch, naming the
name and the `inherit:` fix. The drop itself, and every exit code, stay as
they were.

SENTINEL values only. Every environment handed to a launch here sets or
pops the credential names explicitly, so a real one in the test runner's
own environment is never read, printed or asserted on.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from start_support import TIMEOUT_S, clean_env, make_agents_root, plan_for, session_cmd

from sessiontools.start.environment import (
    CREDENTIAL_VARS,
    Environment,
    dropped_credential_lines,
    host_environ,
)
from sessiontools.start.errors import LaunchError

#: One sentinel per name: none may ever reach stdout or stderr.
SENTINELS = {name: f"SENTINEL-{index}-do-not-print-{name.lower()}"
             for index, name in enumerate(CREDENTIAL_VARS)}
AGENT = "/agents/scout.md"


def expected(name: str, where: str) -> str:
    return (f"{name} is set here but not handed down; to carry it, add "
            f"`environment: inherit: [{name}]` to {where}")


def stderr_notes(err: str) -> list[str]:
    """The dropped-credential lines in a captured stderr."""
    return [line for line in err.splitlines() if "is set here but not handed down" in line]


def assert_no_sentinel(*texts: str) -> None:
    joined = "".join(texts)
    for value in SENTINELS.values():
        assert value not in joined
        assert value[-12:] not in joined           # nor any tail of it


# ---------- the pure helper ------------------------------------------------------

@pytest.mark.parametrize("name", CREDENTIAL_VARS)
def test_a_set_undeclared_credential_gives_one_line_naming_it_and_the_fix(name):
    lines = dropped_credential_lines(Environment({}, [], {}), {name: SENTINELS[name]}, AGENT)
    assert lines == [expected(name, AGENT)]
    assert_no_sentinel(*lines)


@pytest.mark.parametrize("name", CREDENTIAL_VARS)
def test_with_no_agent_file_the_line_says_the_agent_file(name):
    lines = dropped_credential_lines(Environment({}, [], {}), {name: SENTINELS[name]}, "")
    assert lines == [expected(name, "the agent file")]


def test_all_three_set_give_three_lines_in_the_set_order():
    lines = dropped_credential_lines(Environment({}, [], {}), SENTINELS, AGENT)
    assert lines == [expected(name, AGENT) for name in CREDENTIAL_VARS]
    assert_no_sentinel(*lines)


@pytest.mark.parametrize("name", CREDENTIAL_VARS)
@pytest.mark.parametrize("case", ["inherited", "secrets", "variables", "unset", "empty"])
def test_a_declared_unset_or_empty_credential_gives_no_line(name, case):
    environment = {
        "inherited": Environment({}, [], {}, (name,)),
        "secrets": Environment({}, [], {name: "pass:a/b"}),
        "variables": Environment({name: "declared"}, [], {}),
    }.get(case, Environment({}, [], {}))
    environ = {"unset": {}, "empty": {name: ""}}.get(case, {name: SENTINELS[name]})
    assert dropped_credential_lines(environment, environ, AGENT) == []


def test_only_the_undeclared_name_is_named():
    first, second, third = CREDENTIAL_VARS
    environment = Environment({}, [], {second: "pass:a/b"}, (first,))
    assert dropped_credential_lines(environment, SENTINELS, AGENT) == [expected(third, AGENT)]


def test_a_files_entry_does_not_silence_it():
    """A sourced file may set the name, but that is not knowable without
    sourcing it, and the launcher's own value is dropped either way."""
    name = CREDENTIAL_VARS[0]
    environment = Environment({}, ["~/creds.env"], {})
    assert dropped_credential_lines(environment, {name: SENTINELS[name]}, AGENT) == [
        expected(name, AGENT)]


def test_the_drop_itself_is_unchanged():
    assert not set(CREDENTIAL_VARS) & set(host_environ(SENTINELS))


# ---------- the plan: resolve() speaks, once ---------------------------------------

def credential_env(name: str | None) -> dict:
    """Every credential name explicitly empty, then NAME set to its sentinel."""
    env = dict.fromkeys(CREDENTIAL_VARS, "")
    if name:
        env[name] = SENTINELS[name]
    return env


@pytest.mark.parametrize("name", CREDENTIAL_VARS)
@pytest.mark.parametrize("flags", [[], ["--dry-run"]], ids=["live", "dry-run"])
def test_a_plan_warns_once_on_stderr_and_never_prints_the_value(tmp_path, capsys, name, flags):
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", extra_env=credential_env(name),
                    flags=flags)
    out, err = capsys.readouterr()
    agent_file = str(tmp_path / "agents" / "scout.md")
    assert stderr_notes(err) == [f"session start: {expected(name, agent_file)}"]
    assert_no_sentinel(out, err, plan.script, repr(plan))


@pytest.mark.parametrize("name", CREDENTIAL_VARS)
@pytest.mark.parametrize("front_matter, flags", [
    ("environment:\n  inherit: [{name}]\n", []),
    ("environment:\n  secrets:\n    {name}: pass:a/b\n", ["--dry-run"]),
    ("environment:\n  variables:\n    {name}: declared\n", []),
], ids=["inherited", "secrets", "variables"])
def test_a_declared_credential_gives_no_line(tmp_path, capsys, name, front_matter, flags):
    plan_for(tmp_path, front_matter.format(name=name), extra_env=credential_env(name),
             flags=flags)
    out, err = capsys.readouterr()
    assert stderr_notes(err) == []
    assert_no_sentinel(out, err)


def test_no_credential_set_gives_no_line(tmp_path, capsys):
    plan_for(tmp_path, "claude:\n  effort: high\n", extra_env=credential_env(None))
    assert stderr_notes(capsys.readouterr().err) == []


def test_a_resume_warns_too(tmp_path, capsys):
    name = CREDENTIAL_VARS[0]
    plan_for(tmp_path, "claude:\n  effort: high\n", argv_tail=["--resume", "abc"],
             extra_env=credential_env(name))
    assert len(stderr_notes(capsys.readouterr().err)) == 1


def test_a_refused_launch_does_not_warn_first(tmp_path, capsys):
    with pytest.raises(LaunchError, match="--key-label"):
        plan_for(tmp_path, "claude:\n  effort: high\n", flags=["--key-label", "x"],
                 extra_env=credential_env(CREDENTIAL_VARS[0]))
    assert stderr_notes(capsys.readouterr().err) == []


# ---------- end to end: the verb, as a user runs it ----------------------------------

@pytest.mark.parametrize("name", CREDENTIAL_VARS)
def test_the_verb_names_the_dropped_credential_and_exits_as_before(tmp_path, name):
    root = make_agents_root(tmp_path, front_matter="claude:\n  effort: high\n")
    (tmp_path / "work").mkdir()
    env = clean_env(AGENTS_DIR=root, SESSIONS_DIR=os.path.join(root, "sessions"))
    for other in CREDENTIAL_VARS:
        env.pop(other, None)
    env[name] = SENTINELS[name]
    result = subprocess.run(session_cmd("--dry-run", "scout"), capture_output=True,
                            text=True, timeout=TIMEOUT_S, cwd=str(tmp_path / "work"),
                            env=env, check=False)
    assert result.returncode == 0, result.stderr
    agent_file = os.path.join(root, "scout.md")
    assert stderr_notes(result.stderr) == [f"session start: {expected(name, agent_file)}"]
    assert_no_sentinel(result.stdout, result.stderr)
