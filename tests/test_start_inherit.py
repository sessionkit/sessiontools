"""ST-143: `environment: inherit: [NAME, ...]`, the one way a launcher's
credential reaches the session it starts.

`host_environ` drops CREDENTIAL_VARS and the script's first line unsets them
(`test_start_credentials.py`). An agent that WANTS its launcher's value says
so by name, and the value then travels the way a declared secret does: in
the 0600 file the prelude sources last and removes, never argv, never the
tmux server. The banner names each name (carried, or not set -- not
carried), the launch record masks it by name, and a dry run reads nothing.
A name that is not set is not an abort.

FAKE values only. Every environment handed to a launch here sets or pops
the credential names explicitly, so a real one in the test runner's own
environment is never read, printed or asserted on.
"""

from __future__ import annotations

import glob
import os
import stat
import subprocess

import pytest
from start_support import (
    TIMEOUT_S,
    at_a_terminal,
    clean_env,
    make_agents_root,
    plan_for,
)

from sessiontools.start import plan as plan_module
from sessiontools.start import run, secrets
from sessiontools.start.environment import (
    CREDENTIAL_VARS,
    Environment,
    parse_environment,
    warn_env_file_traps,
)
from sessiontools.start.errors import AuthoringError

TOKEN = "sk-ant-oat01-FAKE-ST143-inherit"
NAME = "CLAUDE_CODE_OAUTH_TOKEN"
INHERIT = f"environment:\n  inherit: [{NAME}]\n"


def no_credentials(**extra: str) -> dict:
    """An env dict with every credential name gone, then `extra` laid on."""
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(extra)
    return env


# ---------- parsing ------------------------------------------------------------

def test_inherit_is_parsed_as_names_in_declared_order_repeats_collapsed():
    env = parse_environment({"inherit": ["B_NAME", NAME, "B_NAME"]})
    assert env.inherit == ("B_NAME", NAME)


def test_an_environment_without_inherit_inherits_nothing():
    assert parse_environment({"variables": {"A": "1"}}).inherit == ()
    assert parse_environment(None).inherit == ()


@pytest.mark.parametrize("raw", [NAME, {NAME: True}, 7])
def test_inherit_must_be_a_list(raw):
    with pytest.raises(AuthoringError, match="must be a LIST"):
        parse_environment({"inherit": raw})


@pytest.mark.parametrize("bad", ["1ABC", "A-B", "A B", "", None, 3, "$(id)"])
def test_an_inherit_name_must_be_a_valid_environment_name(bad):
    with pytest.raises(AuthoringError, match="environment: inherit:"):
        parse_environment({"inherit": [bad]})


@pytest.mark.parametrize("other", [
    {"secrets": {NAME: "pass:a/b"}},
    {"variables": {NAME: "x"}},
])
def test_a_name_inherited_and_declared_elsewhere_is_refused(other):
    with pytest.raises(AuthoringError, match="one name, one source"):
        parse_environment({"inherit": [NAME], **other})


def test_an_unknown_key_still_names_inherit_among_the_known():
    with pytest.raises(AuthoringError, match="inherit"):
        parse_environment({"inherits": [NAME]})


def test_any_valid_name_may_be_inherited_not_only_a_credential():
    """D4: the declaration IS the rule's "declared"."""
    assert parse_environment({"inherit": ["MY_TOOL_HOME"]}).inherit == ("MY_TOOL_HOME",)


# ---------- the carrier: read, report, the file text --------------------------

def test_read_inherited_takes_the_launchers_value_and_marks_an_absent_name():
    carried, absent = secrets.read_inherited((NAME, "NOT_THERE"), {NAME: TOKEN})
    assert carried.ok and carried.value == TOKEN and carried.backend == "inherit"
    assert not absent.ok and absent.value is None
    assert TOKEN not in repr(carried)


def test_the_report_names_each_and_never_the_value(capsys):
    secrets.report_inherited(secrets.read_inherited((NAME, "NOT_THERE"), {NAME: TOKEN}))
    out, err = capsys.readouterr()
    assert out == f"inherit: {NAME} carried from the launcher's environment\n"
    assert err == ("inherit: NOT_THERE not set in the launcher's environment "
                   "-- not carried\n")
    assert TOKEN not in out + err


def test_the_file_text_carries_a_set_name_and_skips_an_absent_one():
    value = "FAKE 'quoted' $HOME"
    text = secrets.secret_file_text(
        secrets.read_inherited((NAME, "NOT_THERE"), {NAME: value}))
    assert "NOT_THERE" not in text
    shell = subprocess.run(["bash", "-c", f'{text}printf %s "${NAME}"'],
                           capture_output=True, text=True, check=True,
                           env=no_credentials())
    assert shell.stdout == value


def test_an_env_file_trap_is_warned_about_when_only_inherit_rides_the_file(tmp_path, capsys):
    trapped = tmp_path / "t.env"
    trapped.write_text("trap 'echo bye' EXIT\n")
    warn_env_file_traps(Environment({}, [str(trapped)], {}, (NAME,)), str(tmp_path))
    assert "contains a `trap`" in capsys.readouterr().err


# ---------- the plan -------------------------------------------------------------

def test_a_live_plan_reads_the_value_into_a_repr_less_result_and_plans_the_file(tmp_path):
    plan = plan_for(tmp_path, INHERIT, extra_env={NAME: TOKEN})
    assert plan.inherit == (NAME,)
    assert [(r.name, r.ok) for r in plan.inherited] == [(NAME, True)]
    assert plan.secret_file and os.path.basename(plan.secret_file).startswith(
        "session-start-secrets-")
    assert not os.path.exists(plan.secret_file)          # planning writes nothing
    assert TOKEN not in plan.script
    assert TOKEN not in " ".join(str(token) for token in plan.argv)
    assert TOKEN not in repr(plan)


def test_the_value_arrives_after_the_first_line_unsets_it(tmp_path):
    """D3 needs no exemption: line 1 unsets the name, the file restores it."""
    plan = plan_for(tmp_path, INHERIT, extra_env={NAME: TOKEN})
    lines = plan.script.splitlines()
    unset = next(i for i, line in enumerate(lines) if line.startswith("unset "))
    source = next(i for i, line in enumerate(lines)
                  if line.startswith("if [ -r ") and plan.secret_file in line
                  and ". " in line)
    assert NAME in lines[unset].split() and unset < source


def test_the_launch_record_masks_an_inherited_name(tmp_path):
    plan = plan_for(tmp_path, INHERIT, extra_env={NAME: TOKEN})
    assert NAME in plan.record.secret_names
    assert f"--secret {NAME}" in plan.script


def test_an_absent_name_still_plans_and_is_not_an_abort(tmp_path):
    plan = plan_for(tmp_path, "environment:\n  inherit: [ST143_NOT_SET]\n")
    assert [(r.name, r.ok) for r in plan.inherited] == [("ST143_NOT_SET", False)]
    assert plan.secret_file


def test_without_inherit_no_secret_file_is_planned(tmp_path):
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", extra_env={NAME: TOKEN})
    assert plan.inherit == () and plan.inherited == () and plan.secret_file is None


def test_a_resume_carries_inherit_too(tmp_path):
    plan = plan_for(tmp_path, INHERIT, argv_tail=["--resume", "abc"],
                    extra_env={NAME: TOKEN})
    assert plan.resume and [(r.name, r.ok) for r in plan.inherited] == [(NAME, True)]
    assert plan.secret_file and TOKEN not in plan.script


def test_a_dry_run_names_inherit_and_reads_no_value(tmp_path, monkeypatch):
    read = []
    monkeypatch.setattr(plan_module, "read_inherited", lambda *a: read.append(a) or [])
    plan = plan_for(tmp_path, INHERIT, extra_env={NAME: TOKEN}, flags=["--dry-run"])
    assert read == [] and plan.inherited == ()
    assert os.path.basename(plan.secret_file) == "secrets.env"
    report = run.render_dry_run_report(plan)
    assert f"  {NAME}  <- the launcher's environment" in report
    assert "no value read" in report
    assert TOKEN not in report


# ---------- the launch -----------------------------------------------------------

def test_execute_carries_the_value_in_the_0600_file_never_the_exec_env(tmp_path, monkeypatch,
                                                                     capsys):
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(NAME, TOKEN)        # the launcher holds it
    plan = plan_for(tmp_path, INHERIT, extra_env={NAME: TOKEN})
    seen = {}

    def fake_exec(file, argv, env):
        seen["mode"] = stat.S_IMODE(os.stat(plan.secret_file).st_mode)
        with open(plan.secret_file) as handle:
            seen["text"] = handle.read()
        seen["argv"], seen["env"] = argv, dict(env)
        raise SystemExit(0)
    monkeypatch.setattr(os, "execvpe", fake_exec)
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        run.execute(plan)
    assert seen["mode"] == 0o600
    assert seen["text"].startswith(f"export {NAME}=")
    assert NAME not in seen["env"]                      # never the process env
    assert TOKEN not in " ".join(seen["argv"])
    out, err = capsys.readouterr()
    assert f"inherit: {NAME} carried from the launcher's environment" in out
    assert TOKEN not in out + err


def test_execute_with_the_name_unset_launches_and_says_so(tmp_path, monkeypatch, capsys):
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    plan = plan_for(tmp_path, INHERIT)
    seen = {}

    def fake_exec(file, argv, env):
        with open(plan.secret_file) as handle:
            seen["text"] = handle.read()
        raise SystemExit(0)
    monkeypatch.setattr(os, "execvpe", fake_exec)
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)
    with pytest.raises(SystemExit):
        run.execute(plan)
    assert seen["text"] == ""
    assert (f"inherit: {NAME} not set in the launcher's environment -- not carried"
            in capsys.readouterr().err)


# ---------- end to end: what the vendor sees ---------------------------------------

VENDOR_SEES = """#!/usr/bin/env bash
printf '%%s' "${CLAUDE_CODE_OAUTH_TOKEN-UNSET}" > "%(dir)s/token"
printf '%%s' "${ANTHROPIC_API_KEY-UNSET}" > "%(dir)s/api-key"
printf '%%s\\0' "$@" > "%(dir)s/argv"
true
"""


def launch_holding_a_token(tmp_path, front_matter):
    """`session start scout` as a subprocess whose launcher holds FAKE
    credentials, against a stub claude that writes what it got."""
    record, bin_dir = tmp_path / "record", tmp_path / "vbin"
    record.mkdir()
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(VENDOR_SEES % {"dir": record})
    (bin_dir / "claude").chmod(0o755)
    root = make_agents_root(tmp_path, front_matter=front_matter)
    (tmp_path / "work").mkdir()
    env = clean_env(AGENTS_DIR=root, SESSIONS_DIR=os.path.join(root, "sessions"),
                    PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for name in CREDENTIAL_VARS:
        env.pop(name, None)
    env.update({NAME: TOKEN, "ANTHROPIC_API_KEY": "sk-ant-api03-FAKE-ST143-e2e"})
    result = subprocess.run(at_a_terminal("scout", "hello"), capture_output=True, text=True,
                            timeout=TIMEOUT_S, cwd=str(tmp_path / "work"), env=env,
                            check=False)
    assert result.returncode == 0, result.stderr
    return result, record, root


def test_without_inherit_the_vendor_holds_no_credential_of_its_launcher(tmp_path):
    _result, record, _root = launch_holding_a_token(tmp_path, "claude:\n  effort: high\n")
    assert (record / "token").read_text() == "UNSET"
    assert (record / "api-key").read_text() == "UNSET"


def test_with_inherit_the_vendor_holds_that_one_name_and_nothing_else(tmp_path):
    result, record, root = launch_holding_a_token(tmp_path, INHERIT)
    assert (record / "token").read_text() == TOKEN
    assert (record / "api-key").read_text() == "UNSET"      # not declared: not carried
    assert TOKEN not in (record / "argv").read_text()
    assert TOKEN not in result.stdout + result.stderr
    assert f"inherit: {NAME} carried" in result.stdout
    [env_file] = glob.glob(os.path.join(root, "sessions", "*", "launch", "env"))
    with open(env_file) as handle:
        recorded = handle.read()
    assert TOKEN not in recorded
    assert f"{NAME}={'*' * len(TOKEN)}" in recorded.splitlines()
