"""The clock line: the time and the context as used/max on every tool batch,
switched on, and a warn line at a mark the session set.

As in test_settle, the behaviour that matters is tested THROUGH THE REAL
SHIM, as `hooks/hooks.json` runs it: `sh hooks/clock-hook` with the payload
on stdin and an environment built from nothing, so no variable of the
machine running the suite leaks in. The pure functions get their own tests.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sessiontools import clockline, settle, window

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "clock-hook"
SETTLE_SHIM = ROOT / "hooks" / "settle-hook"
PICKER = ROOT / "hooks" / "pick-python.sh"
HOOKS = ROOT / "hooks" / "hooks.json"
MODULE = ROOT / "src" / "sessiontools" / "clockline.py"

SID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
ON = {"SESSIONTOOLS_HOOK_CLOCK": "on"}

#: 12:34 UTC on a synthetic Sunday, with a zone name: what `decide` is handed in process.
NOW = time.struct_time((2030, 6, 9, 12, 34, 0, 6, 160, 0, "UTC", 0))


def usage_row(tokens: int, model: str = "claude-opus-5-5") -> str:
    return json.dumps({"type": "assistant", "message": {
        "model": model, "usage": {"input_tokens": tokens,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}})


def transcript(tmp_path: Path, *tokens: int, model: str = "claude-opus-5-5",
               name: str = SID) -> Path:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("".join(usage_row(t, model) + "\n" for t in tokens))
    return path


def payload(path: Path | None, **extra) -> dict:
    body = {"session_id": SID, "cwd": "/nowhere", "hook_event_name": "PostToolBatch"}
    if path is not None:
        body["transcript_path"] = str(path)
    body.update(extra)
    return body


@pytest.fixture
def pybin(tmp_path: Path) -> Path:
    """A bin dir whose `python3` is the interpreter running this suite."""
    bindir = tmp_path / "pybin"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    return bindir


def run_shim(stdin: str | dict, env: dict, bindir: Path | None, *,
             system: bool = True, shim: Path = SHIM) -> subprocess.CompletedProcess:
    """The shim exactly as Claude Code runs it, from an environment built
    from nothing but PATH, HOME, TZ=UTC and what the test adds."""
    path = [str(bindir)] if bindir else []
    if system:
        path += ["/usr/bin", "/bin"]
    full = {"PATH": os.pathsep.join(path), "HOME": "/nonexistent", "TZ": "UTC", **env}
    data = stdin if isinstance(stdin, str) else json.dumps(stdin)
    return subprocess.run(["/bin/sh", str(shim)], input=data, env=full,
                          capture_output=True, text=True, timeout=30, check=False)


def assert_silent(result: subprocess.CompletedProcess) -> None:
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def said(result: subprocess.CompletedProcess) -> list[str]:
    """The lines the hook said: exactly one stdout line of hook JSON, whose
    additionalContext is one line, or two joined by a newline."""
    assert result.returncode == 0 and result.stderr == "", result.stderr
    out_lines = result.stdout.splitlines()
    assert len(out_lines) == 1, result.stdout
    out = json.loads(out_lines[0])["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolBatch"
    lines = out["additionalContext"].split("\n")
    assert 1 <= len(lines) <= 2 and all(line and line.strip() == line for line in lines)
    return lines


def decide(env: dict, body: object) -> list[str] | None:
    text = clockline.decide(env, body, now=NOW)
    return None if text is None else text.split("\n")


CLOCK_UTC = re.compile(r"^\[clock\] \d\d:\d\d UTC \| ")


# 1. The line, end to end through the shim ---------------------------------

def test_the_shim_says_the_clock_and_used_over_max(tmp_path, pybin):
    over = transcript(tmp_path, 100, 182_000)
    lines = said(run_shim(payload(over), dict(ON), pybin))
    assert len(lines) == 1
    assert CLOCK_UTC.match(lines[0]), lines[0]
    assert lines[0].endswith(" | ctx 182K/1.00M (18%)")
    assert "mail" not in lines[0].lower()


def test_the_shim_says_the_warn_line_at_the_token_mark(tmp_path, pybin):
    over = transcript(tmp_path, 262_000)
    lines = said(run_shim(payload(over), {**ON, "SESSION_CLOCK_WARN_AT": "250k"}, pybin))
    assert lines[0].endswith(" | ctx 262K/1.00M (26%)")
    assert lines[1] == "ctx past 250K: 262K of 1.00M used."


def test_the_shim_says_the_warn_line_at_the_percent_mark(tmp_path, pybin):
    over = transcript(tmp_path, 812_000)
    lines = said(run_shim(payload(over), {**ON, "SESSION_CLOCK_WARN_AT": "80%"}, pybin))
    assert lines == [lines[0], "ctx past 80% (800K): 812K of 1.00M used."]


def test_the_clock_is_the_hooks_own_local_time(tmp_path, pybin):
    over = transcript(tmp_path, 1_000)
    before = time.strftime("%H:%M", time.gmtime())
    line = said(run_shim(payload(over), dict(ON), pybin))[0]
    after = time.strftime("%H:%M", time.gmtime())
    assert line[len("[clock] "):len("[clock] 00:00")] in {before, after}


# 2. The line's forms, in process --------------------------------------------

def test_a_known_window(tmp_path):
    assert decide(ON, payload(transcript(tmp_path, 182_000))) == [
        "[clock] 12:34 UTC | ctx 182K/1.00M (18%)"]


def test_a_200k_model(tmp_path):
    path = transcript(tmp_path, 150_000, model="claude-haiku-4-5")
    assert decide(ON, payload(path)) == ["[clock] 12:34 UTC | ctx 150K/200K (75%)"]


def test_context_max_tightens_the_window(tmp_path):
    path = transcript(tmp_path, 150_000)
    assert decide({**ON, "CONTEXT_MAX": "300000"}, payload(path)) == [
        "[clock] 12:34 UTC | ctx 150K/300K (50%)"]


@pytest.mark.parametrize("model", ["claude-unknown-9", ""])
def test_an_unknown_window_is_a_question_mark_never_a_guess(tmp_path, model):
    path = transcript(tmp_path, 182_000, model=model)
    assert decide(ON, payload(path)) == ["[clock] 12:34 UTC | ctx 182K/?"]


def test_a_count_over_the_mapped_window_is_unknown(tmp_path):
    path = transcript(tmp_path, 250_000, model="claude-haiku-4-5")
    assert decide(ON, payload(path)) == ["[clock] 12:34 UTC | ctx 250K/?"]


def test_no_usage_rows_is_ctx_question_mark(tmp_path):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text('{"type": "user", "message": {"content": "hi"}}\n')
    assert decide(ON, payload(path)) == ["[clock] 12:34 UTC | ctx ?"]


def test_no_transcript_is_ctx_question_mark(tmp_path):
    env = {**ON, "CLAUDE_PROJECTS_DIR": str(tmp_path / "empty")}
    assert decide(env, payload(tmp_path / "missing.jsonl")) == ["[clock] 12:34 UTC | ctx ?"]
    assert decide(env, payload(None, session_id="")) == ["[clock] 12:34 UTC | ctx ?"]


def test_the_synthetic_zero_tail_is_not_a_reading(tmp_path):
    """`used == 0` after a real peak: a `<synthetic>` row, not an empty
    window. It would otherwise claim 0% on a full session."""
    path = transcript(tmp_path, 900_000)
    with path.open("a") as f:
        f.write(usage_row(0, model=window.SYNTHETIC_MODEL) + "\n")
    env = {**ON, "SESSION_CLOCK_WARN_AT": "1"}
    assert decide(env, payload(path)) == ["[clock] 12:34 UTC | ctx ?"]


def test_a_genuine_zero_is_a_reading(tmp_path):
    path = transcript(tmp_path, 0)
    assert decide(ON, payload(path)) == ["[clock] 12:34 UTC | ctx 0/1.00M (0%)"]


def test_the_count_is_the_last_row_not_the_peak(tmp_path):
    path = transcript(tmp_path, 900_000, 100_000)
    assert decide(ON, payload(path)) == ["[clock] 12:34 UTC | ctx 100K/1.00M (10%)"]


def test_no_transcript_path_falls_back_to_the_projects_dir(tmp_path):
    projects = tmp_path / "projects"
    d = projects / window.encode_dir("/work/here")
    d.mkdir(parents=True)
    transcript(d, 182_000)
    env = {**ON, "CLAUDE_PROJECTS_DIR": str(projects)}
    assert decide(env, payload(None, cwd="/work/here")) == [
        "[clock] 12:34 UTC | ctx 182K/1.00M (18%)"]


# 3. The warn line -----------------------------------------------------------

@pytest.mark.parametrize(("at", "shown"), [("250000", "250K"), ("250k", "250K"),
                                           ("250K", "250K"), (" 250 k ", "250K"),
                                           ("0.25M", "250K")])
def test_the_token_mark_in_settles_grammar(tmp_path, at, shown):
    path = transcript(tmp_path, 262_000)
    lines = decide({**ON, "SESSION_CLOCK_WARN_AT": at}, payload(path))
    assert lines[1] == f"ctx past {shown}: 262K of 1.00M used."


@pytest.mark.parametrize(("at", "past"), [("80%", "80% (800K)"), ("12.5%", "12.5% (125K)"),
                                          (" 50 % ", "50% (500K)"), ("100%", "100% (1.00M)")])
def test_the_percent_mark(tmp_path, at, past):
    path = transcript(tmp_path, 1_000_000)
    lines = decide({**ON, "SESSION_CLOCK_WARN_AT": at}, payload(path))
    assert lines[1] == f"ctx past {past}: 1.00M of 1.00M used."


def test_the_percent_mark_is_of_the_tightened_window(tmp_path):
    path = transcript(tmp_path, 160_000)
    env = {**ON, "SESSION_CLOCK_WARN_AT": "80%", "CONTEXT_MAX": "200000"}
    assert decide(env, payload(path))[1] == "ctx past 80% (160K): 160K of 200K used."


def test_reaching_the_mark_counts_as_past_it(tmp_path):
    env = {**ON, "SESSION_CLOCK_WARN_AT": "250000"}
    assert len(decide(env, payload(transcript(tmp_path, 250_000, name="at")))) == 2
    assert len(decide(env, payload(transcript(tmp_path, 249_999, name="under")))) == 1


def test_a_token_mark_with_the_window_unknown_names_no_max(tmp_path):
    path = transcript(tmp_path, 262_000, model="claude-unknown-9")
    lines = decide({**ON, "SESSION_CLOCK_WARN_AT": "250k"}, payload(path))
    assert lines == ["[clock] 12:34 UTC | ctx 262K/?", "ctx past 250K: 262K used."]


def test_a_percent_mark_with_the_window_unknown_never_warns(tmp_path):
    path = transcript(tmp_path, 990_000, model="claude-unknown-9")
    assert decide({**ON, "SESSION_CLOCK_WARN_AT": "1%"}, payload(path)) == [
        "[clock] 12:34 UTC | ctx 990K/?"]


def test_under_the_mark_is_the_clock_line_alone(tmp_path):
    path = transcript(tmp_path, 100_000)
    assert len(decide({**ON, "SESSION_CLOCK_WARN_AT": "250k"}, payload(path))) == 1
    assert len(decide({**ON, "SESSION_CLOCK_WARN_AT": "80%"}, payload(path))) == 1


@pytest.mark.parametrize("at", [None, "", "   ", "nonsense", "0", "-5", "0k", "k", "nan",
                                "inf", "12x", "0%", "-1%", "101%", "nan%", "inf%", "%",
                                "x%", "0.00000001%", "80%%"])
def test_no_usable_mark_is_no_warn_line(tmp_path, at):
    path = transcript(tmp_path, 990_000)
    env = dict(ON) if at is None else {**ON, "SESSION_CLOCK_WARN_AT": at}
    assert decide(env, payload(path)) == ["[clock] 12:34 UTC | ctx 990K/1.00M (99%)"]


def test_the_mark_never_falls_back_to_the_settle_threshold(tmp_path):
    path = transcript(tmp_path, 990_000)
    assert len(decide({**ON, settle.AT_ENV: "1"}, payload(path))) == 1


def test_the_degraded_forms_never_warn(tmp_path):
    env = {**ON, "SESSION_CLOCK_WARN_AT": "1", "CLAUDE_PROJECTS_DIR": str(tmp_path / "p")}
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert decide(env, payload(empty)) == ["[clock] 12:34 UTC | ctx ?"]
    assert decide(env, payload(tmp_path / "missing.jsonl")) == ["[clock] 12:34 UTC | ctx ?"]


# 4. The switch: off by default, on by word -----------------------------------

@pytest.mark.parametrize("value", [None, "", "off", "OFF", "0", "false", "no", "nope",
                                   "onn", " on", "on ", "\ton\n", "2", "enabled"])
def test_off_by_default_and_with_every_other_word(tmp_path, pybin, value):
    over = transcript(tmp_path, 182_000)
    env = {} if value is None else {"SESSIONTOOLS_HOOK_CLOCK": value}
    assert_silent(run_shim(payload(over), env, pybin))
    assert clockline.decide(env, payload(over)) is None


@pytest.mark.parametrize("value", ["on", "ON", "On", "oN", "1", "true", "TRUE", "True",
                                   "yes", "YES", "Yes"])
def test_on_with_each_on_word(tmp_path, pybin, value):
    over = transcript(tmp_path, 182_000)
    lines = said(run_shim(payload(over), {"SESSIONTOOLS_HOOK_CLOCK": value}, pybin))
    assert lines[0].endswith("ctx 182K/1.00M (18%)")


@pytest.mark.parametrize("value", ["on", "On", " on", "on ", "1", "TRUE", "yes", "off", "",
                                   "no", "onn", "y", "t"])
def test_the_shim_and_the_module_agree_on_the_switch(tmp_path, value):
    """The shim's `case` decides; the module re-checks. They never differ."""
    probe = tmp_path / "probe"
    probe.write_text('case "${SESSIONTOOLS_HOOK_CLOCK:-}" in\n'
                     + next(line for line in SHIM.read_text().splitlines()
                            if line.lstrip().startswith("[Oo][Nn]")).rstrip().removesuffix(";;")
                     + "echo on ;;\n  *) echo off ;;\nesac\n")
    got = subprocess.run(["/bin/sh", str(probe)], env={"SESSIONTOOLS_HOOK_CLOCK": value},
                         capture_output=True, text=True, timeout=30, check=True).stdout
    assert (got.strip() == "on") is clockline.switched_on({"SESSIONTOOLS_HOOK_CLOCK": value})


def test_off_starts_no_python(tmp_path):
    """Off is decided in the shim: whatever python the picker would pick,
    none starts."""
    mark = tmp_path / "started"
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("python3", "python"):
        py = d / name
        py.write_text(f'#!/bin/sh\necho x >> "{mark}"\nexec "{sys.executable}" "$@"\n')
        py.chmod(0o755)
    session = d / "session"
    session.write_text(f"#!{d / 'python'}\n")
    session.chmod(0o755)
    over = transcript(tmp_path, 182_000)
    for env in ({}, {"SESSIONTOOLS_HOOK_CLOCK": "off"},
                {"SESSIONTOOLS_HOOK_CLOCK": "", "SESSION_CLOCK_WARN_AT": "1"},
                {"SESSIONTOOLS_HOOK_CLOCK": "off", "SESSIONTOOLS_PYTHON": str(d / "python")}):
        assert_silent(run_shim(payload(over), env, d))
    assert not mark.exists()
    # The control: switched on, the same bin dir starts python and speaks.
    said(run_shim(payload(over), dict(ON), d))
    assert mark.exists()


def test_off_needs_no_python_at_all(tmp_path):
    over = transcript(tmp_path, 182_000)
    assert_silent(run_shim(payload(over), {}, None, system=False))


# 5. A subagent's batch is silent ---------------------------------------------

def test_a_subagent_batch_is_silent(tmp_path, pybin):
    over = transcript(tmp_path, 182_000)
    env = {**ON, "SESSION_CLOCK_WARN_AT": "1"}
    assert_silent(run_shim(payload(over, agent_id="a1b2c3", agent_type="Explore"),
                           dict(env), pybin))
    # agent_type alone is a main thread run with --agent: it is told.
    said(run_shim(payload(over, agent_type="worker"), dict(env), pybin))


# 6. Any failure: exit 0, no output, no stderr ---------------------------------

def test_a_payload_that_is_not_an_object_is_silent(tmp_path, pybin):
    for body in ("", "garbage", "[1, 2]", "null", '"a string"', "{", "7"):
        assert_silent(run_shim(body, dict(ON), pybin))


def test_an_unreadable_transcript_is_ctx_question_mark(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file")
    locked = transcript(tmp_path, 182_000, name="locked")
    locked.chmod(0)
    try:
        assert decide(dict(ON), payload(locked)) == ["[clock] 12:34 UTC | ctx ?"]
    finally:
        locked.chmod(0o600)


def test_a_fifo_named_as_the_transcript_never_blocks(tmp_path, pybin):
    fifo = tmp_path / f"{SID}.jsonl"
    os.mkfifo(fifo)
    env = {**ON, "CLAUDE_PROJECTS_DIR": str(tmp_path / "p")}
    lines = said(run_shim(payload(fifo), env, pybin))
    assert lines[0].endswith(" | ctx ?")


def test_no_python3_on_path_is_silent(tmp_path):
    over = transcript(tmp_path, 182_000)
    assert_silent(run_shim(payload(over), dict(ON), None, system=False))


@pytest.mark.parametrize("body", ['echo "Traceback: boom" >&2; exit 1',
                                  'echo "bad" >&2; exit 2', "kill -9 $$"])
def test_a_python3_that_fails_is_silent(tmp_path, body):
    bindir = tmp_path / "broken"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    over = transcript(tmp_path, 182_000)
    assert_silent(run_shim(payload(over), dict(ON), bindir))


def test_a_crash_inside_is_exit_0_and_no_stderr(tmp_path, pybin):
    """The module itself raising: a src/ whose clockline blows up on import."""
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    for f in (SHIM, PICKER):
        (plugin / "hooks" / f.name).write_text(f.read_text())
    pkg = plugin / "src" / "sessiontools"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "clockline.py").write_text("raise RuntimeError('boom')\n")
    over = transcript(tmp_path, 182_000)
    assert_silent(run_shim(payload(over), dict(ON), pybin, shim=plugin / "hooks" / SHIM.name))


def test_main_never_raises_and_always_returns_0(tmp_path, monkeypatch):
    class Exploding(io.StringIO):
        def read(self, *a):
            raise KeyboardInterrupt

    out = io.StringIO()
    assert clockline.main(Exploding(), out, dict(ON)) == 0
    assert out.getvalue() == ""

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(clockline.window, "scan", boom)
    body = json.dumps(payload(transcript(tmp_path, 1)))
    assert clockline.main(io.StringIO(body), out, dict(ON)) == 0
    assert out.getvalue() == ""


def test_main_reads_no_stdin_when_off():
    class Untouchable(io.StringIO):
        def read(self, *a):
            raise AssertionError("stdin was read")

    out = io.StringIO()
    assert clockline.main(Untouchable(), out, {}) == 0
    assert clockline.main(Untouchable(), out, {"SESSIONTOOLS_HOOK_CLOCK": "off"}) == 0
    assert out.getvalue() == ""


def test_main_echoes_the_event_name(tmp_path):
    out = io.StringIO()
    body = json.dumps(payload(transcript(tmp_path, 1), hook_event_name="PostToolUse"))
    assert clockline.main(io.StringIO(body), out, dict(ON)) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


# 7. The count is this batch's own --------------------------------------------

TOOL_ID = "toolu_clock0001"


def batch_row(tokens: int) -> str:
    return json.dumps({"type": "assistant", "message": {
        "model": "claude-opus-5-5", "role": "assistant",
        "content": [{"type": "tool_use", "id": TOOL_ID, "name": "Bash",
                     "input": {"command": "date"}}],
        "usage": {"input_tokens": tokens, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": 20}}})


def test_the_batch_row_that_lands_after_the_hook_starts_is_counted(tmp_path):
    path = transcript(tmp_path, 100_000)

    def land():
        with path.open("a") as f:
            f.write(batch_row(300_000) + "\n")

    timer = threading.Timer(0.25, land)
    timer.start()
    try:
        body = payload(path, tool_calls=[{"tool_name": "Bash", "tool_use_id": TOOL_ID}])
        lines = decide({**ON, "SESSION_CLOCK_WARN_AT": "250k"}, body)
    finally:
        timer.join()
    assert lines == ["[clock] 12:34 UTC | ctx 300K/1.00M (30%)",
                     "ctx past 250K: 300K of 1.00M used."]


# 8. The clock ------------------------------------------------------------------

def test_the_clock_format():
    assert clockline.clock(NOW) == "12:34 UTC"


@pytest.mark.parametrize(("raw", "shown"), [("12:34 ", "12:34"), ("12:34 +0000", "12:34 +0000"),
                                            ("12:34  X", "12:34 X")])
def test_an_empty_or_odd_zone_leaves_no_doubled_space(monkeypatch, raw, shown):
    monkeypatch.setattr(clockline.time, "strftime", lambda fmt, t: raw)
    assert clockline.clock(NOW) == shown
    text = clockline.line(NOW, None, None)
    assert "  " not in text and text == f"[clock] {shown} | ctx ?"


# 9. Plugin wiring ----------------------------------------------------------------

def test_hooks_json_runs_the_clock_after_settle_with_timeout_5():
    entries = json.loads(HOOKS.read_text())["hooks"]["PostToolBatch"]
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    clock = '"${CLAUDE_PLUGIN_ROOT}/hooks/clock-hook"'
    assert commands[0] == '"${CLAUDE_PLUGIN_ROOT}/hooks/settle-hook"'
    assert commands.count(clock) == 1 and commands.index(clock) > 0
    assert all(h["type"] == "command" and h["timeout"] == 5
               for entry in entries for h in entry["hooks"])


def test_hooks_json_says_what_it_does_and_how_to_switch_it_on():
    description = json.loads(HOOKS.read_text())["description"]
    for word in ("clock-hook", f"{clockline.SWITCH_ENV}=on", clockline.WARN_ENV):
        assert word in description, word


def test_the_shim_exists_is_executable_and_posix_sh():
    assert SHIM.is_file()
    assert SHIM.stat().st_mode & stat.S_IXUSR
    assert SHIM.read_text().startswith("#!/bin/sh\n")


def test_the_shim_sources_the_one_picker_and_keeps_no_copy():
    text = SHIM.read_text()
    assert '. "$here/pick-python.sh"' in text
    assert "pick() {" not in text
    assert "SESSIONTOOLS_PYTHON:-" not in text


def test_the_shim_never_runs_a_bare_python3():
    code = [line for line in SHIM.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert not any(re.search(r"(^|[\s;|&(])python3\b", line) for line in code), code


def test_the_variable_names_agree_between_the_shim_and_the_module():
    text = SHIM.read_text()
    assert clockline.SWITCH_ENV in text and "sessiontools.clockline" in text


FORBIDDEN = re.compile(r"TM_AGENT_ADDRESS|\b(?:role|seat|crew)s?\b", re.IGNORECASE)


@pytest.mark.parametrize("source", [MODULE, SHIM], ids=lambda p: p.name)
def test_no_role_seat_or_crew_check_anywhere_in_the_source(source):
    """Any session that switched it on is told; nothing asks who it is."""
    hits = [(n, line) for n, line in enumerate(source.read_text().splitlines(), 1)
            if FORBIDDEN.search(line)]
    assert hits == []


# 10. The pure functions ------------------------------------------------------------

@pytest.mark.parametrize(("raw", "size", "mark"), [
    ("250k", None, (250_000, None)), ("250k", 1_000_000, (250_000, None)),
    ("80%", 1_000_000, (800_000, 80.0)), ("80%", None, None), ("12.5%", 200_000, (25_000, 12.5)),
    ("", 1_000_000, None), ("x", 1_000_000, None), ("101%", 1_000_000, None),
])
def test_warn_at(raw, size, mark):
    assert clockline.warn_at({"SESSION_CLOCK_WARN_AT": raw}, size) == mark


def test_warn_at_unset_is_none():
    assert clockline.warn_at({}, 1_000_000) is None


@pytest.mark.parametrize(("usage", "used"), [
    (None, None), (window.Usage(), None), (window.Usage(used=0, peak=5, usage_rows=2), None),
    (window.Usage(used=0, peak=0, usage_rows=1), 0), (window.Usage(used=7, peak=9, usage_rows=2), 7),
])
def test_reading(usage, used):
    assert clockline.reading(usage) == used


def test_decide_is_none_unless_switched_on_and_handed_an_object(tmp_path):
    over = transcript(tmp_path, 1)
    assert clockline.decide({}, payload(over)) is None
    assert clockline.decide(dict(ON), "not a dict") is None
    assert clockline.decide(dict(ON), payload(over)) is not None
