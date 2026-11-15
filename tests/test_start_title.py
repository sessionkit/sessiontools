"""The terminal title a launch sets: one short OSC 0 (the seat, else the
agent, else the vendor) written by the launch script just before its exec,
so a terminal that titles a tab from the foreground process's arguments
does not show the whole launch line before the vendor sets its own.

The script's guards are run, not just read: the title line goes through a
real bash with stdout on a pseudo-terminal (`pty_run.py`) or on a pipe, and
the bytes that come out are the assertion. Every plan is built under a
temporary HOME."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from start_support import PTY_RUN, TIMEOUT_S, make_agents_root, plan_for

from sessiontools.start import plan as pl
from sessiontools.start import run
from sessiontools.start.args import parse_args
from sessiontools.start.plan import TITLE_OPT_OUT, prepare

ESC, BEL = "\x1b", "\x07"
SID = "11111111-2222-3333-4444-555555555555"


def osc0(name: str) -> bytes:
    return f"{ESC}]0;{name}{BEL}".encode()


def home_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home)}


def run_line(line: str, *, tty: bool, env_extra: dict | None = None,
             cwd: Path | None = None) -> bytes:
    """What LINE writes to stdout under a real bash: on a pseudo-terminal
    when TTY, else on a pipe. The environment is minimal and carries no
    opt-out unless ENV_EXTRA sets one."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update(env_extra or {})
    argv = ["bash", "-c", line]
    if tty:
        argv = [sys.executable, PTY_RUN, *argv]
    result = subprocess.run(argv, capture_output=True, env=env, timeout=TIMEOUT_S,
                            cwd=cwd, check=True)
    return result.stdout


def title_line_of(script: str) -> str:
    """The title's two lines (comment, then the guarded write), as rendered."""
    lines = script.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("# terminal title"))
    return "\n".join(lines[at:at + 2])


# ---------- the rendered line -----------------------------------------------------------

def test_the_line_is_one_osc0_guarded_by_a_terminal_and_the_opt_out():
    line = pl.render_title_line("scout")
    assert "if [ -t 1 ]" in line
    assert f'[ -z "${{{TITLE_OPT_OUT}:-}}" ]' in line
    assert "printf '\\033]0;%s\\007' scout" in line
    # no title stack: exec leaves nobody to pop a push
    assert "22;" not in line and "23;" not in line


def test_nothing_to_show_renders_no_line():
    assert pl.render_title_line("") == ""
    assert pl.render_title_line(f"{ESC}{BEL}\n\t ") == ""
    assert "terminal title" not in pl.render_launch_script(["v"], "/w")


def test_the_title_is_the_last_line_before_the_exec():
    lines = pl.render_launch_script(["vendor", "--flag"], "/w", title="scout",
                                    identity_call="# identity marker").splitlines()
    assert lines[-1] == "exec vendor --flag"
    assert lines[-3].startswith("# terminal title")
    assert lines[-2].startswith("if [ -t 1 ]")
    assert lines.index("# identity marker") < len(lines) - 3


@pytest.mark.parametrize("raw, shown", [
    (f"a{ESC}]0;evil{BEL}b", "a]0;evilb"),        # ESC and BEL: no break-out
    ("a\x9b31mb\x9dc", "a31mbc"),                  # C1 CSI and OSC
    ("a\x00b\x7fc\nd\re\tf", "abcdef"),             # NUL, DEL, newlines, tab
    ("  web+reviewer  ", "web+reviewer"),          # edges trimmed
    ("café ✳", "café ✳"),                          # printable unicode kept
])
def test_clean_title_takes_out_every_control_character(raw, shown):
    assert pl.clean_title(raw) == shown


# ---------- the line, run ---------------------------------------------------------------

def test_at_a_terminal_it_writes_exactly_one_osc0():
    assert run_line(pl.render_title_line("scout"), tty=True) == osc0("scout")


def test_on_a_pipe_it_writes_nothing():
    assert run_line(pl.render_title_line("scout"), tty=False) == b""


@pytest.mark.parametrize("value", ["1", "true", "0"])
def test_the_opt_out_set_non_empty_writes_nothing(value):
    line = pl.render_title_line("scout")
    assert run_line(line, tty=True, env_extra={TITLE_OPT_OUT: value}) == b""


def test_the_opt_out_set_empty_still_writes_the_title():
    line = pl.render_title_line("scout")
    assert run_line(line, tty=True, env_extra={TITLE_OPT_OUT: ""}) == osc0("scout")


def test_a_hostile_name_cannot_break_out(tmp_path):
    marker = tmp_path / "ran"
    hostile = (f"x{ESC}]0;owned{BEL}{ESC}[2J'\"; touch {marker}; "
               f"$(touch {marker}) `touch {marker}` %s %n \\033")
    out = run_line(pl.render_title_line(hostile), tty=True, cwd=tmp_path)
    shown = pl.clean_title(hostile)
    assert out == osc0(shown)
    # one sequence: one ESC, one BEL, and the name's own never reached the tty
    assert out.count(ESC.encode()) == 1 and out.count(BEL.encode()) == 1
    # no substitution ran, and printf read the name as text, not a format
    assert not marker.exists()
    assert b"%s %n \\033" in out


# ---------- the plan picks the name -----------------------------------------------------

def test_a_named_agent_titles_with_its_name(tmp_path):
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", extra_env=home_env(tmp_path))
    assert "printf '\\033]0;%s\\007' scout;" in plan.script


def test_a_seat_beats_the_agent_name(tmp_path):
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", flags=["--seat", "web+reviewer"],
                    extra_env=home_env(tmp_path))
    assert "printf '\\033]0;%s\\007' web+reviewer;" in plan.script
    assert "%s\\007' scout;" not in plan.script


def test_a_plain_session_titles_with_the_vendor(tmp_path):
    root = make_agents_root(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    environ = {"AGENTS_DIR": root, "PATH": os.environ.get("PATH", ""),
               "SESSIONS_DIR": os.path.join(root, "sessions"),
               "SESSION_KEY_INIT": os.environ["SESSION_KEY_INIT"], **home_env(tmp_path)}
    plan = prepare(parse_args(["--dry-run", "-"]), environ=environ, work_dir=str(work))
    assert plan.agent.name == ""
    assert "printf '\\033]0;%s\\007' claude;" in plan.script


def test_the_name_is_the_window_names_own_rule():
    """One rule for agent-else-vendor: the window name and the title read
    the same function, never a copy."""
    assert run.default_window_name is pl.default_window_name


def test_a_resume_titles_too(tmp_path):
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", argv_tail=["--resume", SID],
                    extra_env=home_env(tmp_path))
    assert plan.resume
    lines = plan.script.splitlines()
    assert lines[-1].startswith("exec claude")
    assert lines[-2].endswith("printf '\\033]0;%s\\007' scout; fi")


def test_the_planned_script_writes_the_title_at_a_terminal(tmp_path):
    """The line as the plan renders it, run: the seat's name reaches the tty."""
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", flags=["--seat", "web+reviewer"],
                    extra_env=home_env(tmp_path))
    assert run_line(title_line_of(plan.script), tty=True) == osc0("web+reviewer")


def test_the_dry_run_report_shows_the_title_line(tmp_path):
    plan = plan_for(tmp_path, "claude:\n  effort: high\n", flags=["--dry-run"],
                    extra_env=home_env(tmp_path))
    report = run.render_dry_run_report(plan)
    assert "  # terminal title: a short name, not the launch line" in report
    assert "  if [ -t 1 ]" in report and "%s\\007' scout; fi" in report
