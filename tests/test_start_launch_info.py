"""The launch-info marker: `session start` exports SESSIONTOOLS_LAUNCH_INFO=<id>
when its `launch-info` built-in rendered WITH a session id, so the
session-info hook leaves that id out of its block. Every other launch only
unsets an inherited marker, on the script's first line, beside the identity.
"""

from __future__ import annotations

import os

from start_support import VENDOR_STUB, launch_e2e, make_agents_root, plan_for

from sessiontools import session_info
from sessiontools.start import plan as pl
from sessiontools.start.args import parse_args
from sessiontools.start.context import (
    BUILTIN_LAUNCH_INFO,
    ContextResult,
    InjectedContext,
)
from sessiontools.start.environment import (
    LAUNCH_INFO_ENV,
    SESSION_IDENTITY_VARS,
    render_identity_unset,
    without_session_identity,
)
from sessiontools.start.identity import PaneMint, SessionIdentity
from sessiontools.start.run import render_dry_run_report

LAUNCH_INFO = "start:\n  context:\n    system-prompt:\n      - launch-info\n"
LAUNCH_INFO_FIRST_MESSAGE = "start:\n  context:\n    initial:\n      - launch-info\n"
ECHO_ONLY = "start:\n  context:\n    system-prompt:\n      - echo hello\n"
PARENT_MARKER = "11111111-2222-4333-8444-555555555555"
EXPORT = f"export {LAUNCH_INFO_ENV}="


def exported(script: str) -> list[str]:
    """The script's export lines for the marker."""
    return [line for line in script.splitlines() if line.startswith(EXPORT)]


# ---------- the one name -------------------------------------------------------------

def test_the_launcher_and_the_hook_read_one_name():
    assert LAUNCH_INFO_ENV == session_info.LAUNCH_INFO_ENV == "SESSIONTOOLS_LAUNCH_INFO"


def test_the_marker_is_scrubbed_like_the_identity():
    """It sits in SESSION_IDENTITY_VARS, so the script's first line and
    the generators' environment both drop a parent's marker."""
    assert LAUNCH_INFO_ENV in SESSION_IDENTITY_VARS
    assert LAUNCH_INFO_ENV in render_identity_unset().split()[1:]
    assert LAUNCH_INFO_ENV not in without_session_identity({LAUNCH_INFO_ENV: PARENT_MARKER})


# ---------- export -------------------------------------------------------------------

def test_launch_info_with_a_minted_id_exports_the_marker(tmp_path):
    plan = plan_for(tmp_path, LAUNCH_INFO, flags=["--dry-run"],
                    extra_env={LAUNCH_INFO_ENV: PARENT_MARKER})
    sid = plan.identity.session_id
    assert f"**Session ID**: {sid}" in plan.injected.system_prompt
    assert exported(plan.script) == [f"{EXPORT}{sid}"]
    assert PARENT_MARKER not in plan.script


def test_launch_info_through_the_first_message_door_exports_too(tmp_path):
    plan = plan_for(tmp_path, LAUNCH_INFO_FIRST_MESSAGE, flags=["--dry-run"])
    assert exported(plan.script) == [f"{EXPORT}{plan.identity.session_id}"]


def test_the_marker_comes_after_the_unset_and_before_the_declared_environment(tmp_path):
    """Launcher-owned, in generator_environ's order: scrub, the launcher's
    value, then the agent's `environment:` declarations, which win."""
    plan = plan_for(tmp_path, LAUNCH_INFO + "environment:\n  variables:\n"
                    f"    {LAUNCH_INFO_ENV}: declared\n", flags=["--dry-run"])
    lines = plan.script.splitlines()
    unset = lines.index(render_identity_unset())
    export = lines.index(f"{EXPORT}{plan.identity.session_id}")
    declared = next(i for i, line in enumerate(lines)
                    if LAUNCH_INFO_ENV in line and "declared" in line)
    cd = next(i for i, line in enumerate(lines) if line.startswith("cd "))
    assert unset < export < cd < declared


def test_a_dry_run_report_shows_the_marker(tmp_path):
    plan = plan_for(tmp_path, LAUNCH_INFO, flags=["--dry-run"])
    report = render_dry_run_report(plan)
    assert f"{EXPORT}{plan.identity.session_id}" in report


# ---------- no export: unset only ---------------------------------------------------

def test_no_launch_info_in_the_context_gives_no_export(tmp_path):
    for work, front in (("w1", ""), ("w2", ECHO_ONLY)):
        plan = plan_for(tmp_path, front, flags=["--dry-run"], work=work,
                        extra_env={LAUNCH_INFO_ENV: PARENT_MARKER})
        assert plan.identity is not None
        assert exported(plan.script) == []
        assert render_identity_unset() in plan.script.splitlines()     # the unset
        assert PARENT_MARKER not in plan.script


def test_an_idle_launch_runs_no_generator_so_gives_no_export(tmp_path):
    plan = plan_for(tmp_path, LAUNCH_INFO, flags=["--dry-run", "--idle"])
    assert plan.injected.results == []
    assert exported(plan.script) == []
    assert render_identity_unset() in plan.script.splitlines()


def test_a_failing_launch_info_built_in_gives_no_export(tmp_path, monkeypatch):
    """It rendered a failure note and no id: a marker then would hide the
    id from the seat at startup."""
    def broken(**_kwargs):
        def boom():
            raise RuntimeError("no clock")
        return {BUILTIN_LAUNCH_INFO: boom}
    monkeypatch.setattr(pl, "context_builtins", broken)
    plan = plan_for(tmp_path, LAUNCH_INFO, flags=["--dry-run"])
    assert "CONTEXT GENERATOR FAILED: `launch-info`" in plan.injected.system_prompt
    assert exported(plan.script) == []


def test_a_pane_minted_launch_gives_no_export(tmp_path):
    """The id is minted in the pane, after launch-info rendered without it."""
    root = make_agents_root(tmp_path, front_matter=LAUNCH_INFO)
    (tmp_path / "work").mkdir()
    launch = pl.resolve(parse_args(["--dry-run", "scout"]),
                        environ={"AGENTS_DIR": root, "PATH": os.environ.get("PATH", ""),
                                 "SESSIONS_DIR": str(tmp_path / "sessions")},
                        work_dir=str(tmp_path / "work"))
    plan = pl.assemble(launch, PaneMint("/k/session-key-init", "scout", "/r"))
    assert [result.ok for result in plan.injected.results] == [True]
    assert "**Session ID**" not in plan.injected.system_prompt
    assert exported(plan.script) == []
    assert render_identity_unset() in plan.script.splitlines()


def test_the_resume_plan_gives_no_export(tmp_path):
    (tmp_path / "home").mkdir()
    plan = plan_for(tmp_path, LAUNCH_INFO, flags=["--dry-run"],
                    argv_tail=["--resume", PARENT_MARKER],
                    extra_env={"HOME": str(tmp_path / "home"), LAUNCH_INFO_ENV: PARENT_MARKER})
    assert plan.resume
    assert exported(plan.script) == []
    assert render_identity_unset() in plan.script.splitlines()


# ---------- the rule, as a function ------------------------------------------------

IDENTITY = SessionIdentity("sid-1", "/tmp/sid-1", False, {"CLAUDE_SESSION_ID": "sid-1"})


def injected(*results: ContextResult) -> InjectedContext:
    return InjectedContext("", "", list(results))


def test_launch_info_marker_exports_only_an_ok_launch_info_with_an_identity():
    ok = ContextResult(BUILTIN_LAUNCH_INFO, "**Session ID**: sid-1", None)
    failed = ContextResult(BUILTIN_LAUNCH_INFO, "", "RuntimeError: no clock")
    other = ContextResult("echo hi", "hi", None)
    assert pl.launch_info_marker(injected(ok), IDENTITY) == "sid-1"
    assert pl.launch_info_marker(injected(other, ok), IDENTITY) == "sid-1"
    assert pl.launch_info_marker(injected(ok), None) is None           # pane mint
    assert pl.launch_info_marker(injected(failed), IDENTITY) is None
    assert pl.launch_info_marker(injected(other), IDENTITY) is None
    assert pl.launch_info_marker(injected(), IDENTITY) is None         # idle, resume


def test_render_launch_script_without_a_marker_exports_none():
    assert exported(pl.render_launch_script(["v"], "/w", identity=IDENTITY)) == []
    assert exported(pl.render_launch_script(["v"], "/w", identity=IDENTITY,
                                            launch_info="sid-1")) == [f"{EXPORT}sid-1"]


# ---------- behaviour: what the agent process holds ----------------------------------

MARKER_VENDOR_STUB = VENDOR_STUB + (
    f'printf \'%%s\' "${{{LAUNCH_INFO_ENV}-<unset>}}" > "%(dir)s/env-marker"\n'
    'printf \'%%s\' "${CLAUDE_SESSION_ID-<unset>}" > "%(dir)s/env-sid"\n')


def run_marker(tmp_path, front_matter):
    result, record, _work = launch_e2e(tmp_path, "scout", front_matter=front_matter,
                                       extra_env={LAUNCH_INFO_ENV: PARENT_MARKER},
                                       stub_body=MARKER_VENDOR_STUB)
    assert result.returncode == 0, result.stderr
    return ((record / "env-marker").read_text(), (record / "env-sid").read_text())


def test_the_agent_holds_its_own_id_as_the_marker_never_the_parents(tmp_path):
    marker, sid = run_marker(tmp_path, LAUNCH_INFO)
    assert sid not in ("", "<unset>", PARENT_MARKER)
    assert marker == sid


def test_an_agent_without_launch_info_holds_no_marker(tmp_path):
    marker, _sid = run_marker(tmp_path, "claude:\n  effort: high\n")
    assert marker == "<unset>"


def test_an_agents_declaration_wins_over_the_launchers_marker(tmp_path):
    marker, _sid = run_marker(tmp_path, LAUNCH_INFO + "environment:\n  variables:\n"
                              f"    {LAUNCH_INFO_ENV}: declared\n")
    assert marker == "declared"
