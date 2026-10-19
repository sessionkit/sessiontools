"""The settle alert (ST-10): silent unless the session set its threshold.

By design it serves any SESSION that sets a limit: no filter on who the
session is.

The card's success criteria are tested THROUGH THE REAL SHIM, as
`hooks/hooks.json` runs it: `sh hooks/settle-hook` with the payload on
stdin and an environment built from nothing, so no variable of the machine
running the suite leaks in. The pure functions get their own tests below.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools import settle, window

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "settle-hook"
HOOKS = ROOT / "hooks" / "hooks.json"
MODULE = ROOT / "src" / "sessiontools" / "settle.py"

SID = "0f0e0d0c-0b0a-4908-8706-050403020100"
FAR_OVER = 5_000_000


def usage_row(tokens: int, model: str = "claude-opus-5-5") -> str:
    return json.dumps({"type": "assistant", "message": {
        "model": model, "usage": {"input_tokens": tokens,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}})


def transcript(tmp_path: Path, *tokens: int, name: str = SID) -> Path:
    path = tmp_path / f"{name}.jsonl"
    path.write_text("".join(usage_row(t) + "\n" for t in tokens))
    return path


def payload(path: Path | None, **extra) -> dict:
    body = {"session_id": SID, "cwd": "/nowhere", "hook_event_name": "PostToolBatch"}
    if path is not None:
        body["transcript_path"] = str(path)
    body.update(extra)
    return body


@pytest.fixture
def pybin(tmp_path: Path) -> Path:
    """A bin dir whose `python3` is the interpreter running this suite (the
    package needs 3.11+; the host's /usr/bin/python3 may be older)."""
    bindir = tmp_path / "pybin"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    return bindir


def run_shim(stdin: str | dict, env: dict, bindir: Path | None, *,
             system: bool = True) -> subprocess.CompletedProcess:
    """The shim exactly as Claude Code runs it: stdin JSON, env inherited.
    The env is built from nothing but PATH, HOME and what the test adds."""
    path = [str(bindir)] if bindir else []
    if system:
        path += ["/usr/bin", "/bin"]
    full = {"PATH": os.pathsep.join(path), "HOME": env.pop("HOME", "/nonexistent"),
            **env}
    data = stdin if isinstance(stdin, str) else json.dumps(stdin)
    return subprocess.run(["/bin/sh", str(SHIM)], input=data, env=full,
                          capture_output=True, text=True, timeout=30, check=False)


def assert_silent(result: subprocess.CompletedProcess) -> None:
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def said(result: subprocess.CompletedProcess) -> str:
    """The one line the hook said: exactly one stdout line, valid hook JSON."""
    assert result.returncode == 0 and result.stderr == ""
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    out = json.loads(lines[0])["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolBatch"
    line = out["additionalContext"]
    assert "\n" not in line and line.strip() == line and line
    return line


# 1. No threshold: silent, on every call ------------------------------------

@pytest.mark.parametrize("at", [None, "", "   "])
def test_no_threshold_is_silent_on_every_call(tmp_path, pybin, at):
    over = transcript(tmp_path, 10, FAR_OVER)
    env = {} if at is None else {"SESSION_SETTLE_AT": at}
    for body in (payload(over), payload(None), payload(over, agent_id=""),
                 "", "not json", json.dumps(payload(over))):
        assert_silent(run_shim(body, dict(env), pybin))


@pytest.mark.parametrize("at", ["nonsense", "0", "-5", "0k", "k", "nan", "inf", "12x"])
def test_an_unusable_threshold_is_silent(tmp_path, pybin, at):
    over = transcript(tmp_path, FAR_OVER)
    assert_silent(run_shim(payload(over), {"SESSION_SETTLE_AT": at}, pybin))


# 2. Threshold under the count: exactly one line, with its own number -------

@pytest.mark.parametrize(("at", "shown"), [("250000", "250K"), ("250k", "250K"),
                                           ("250K", "250K"), ("1.5M", "1.50M"),
                                           (" 300k ", "300K")])
def test_past_the_threshold_says_one_line_with_its_own_number(tmp_path, pybin, at, shown):
    over = transcript(tmp_path, 100, 2_000_000)
    line = said(run_shim(payload(over), {"SESSION_SETTLE_AT": at}, pybin))
    assert line == settle.DEFAULT_SAY.replace("{at}", shown)
    assert f"You're past {shown}." in line


def test_the_default_line_names_no_verb_that_does_not_exist():
    """Super, 12:34: no `session respawn` until it is ruled and built."""
    assert "respawn" not in settle.DEFAULT_SAY
    assert "`" not in settle.DEFAULT_SAY
    assert "\n" not in settle.DEFAULT_SAY


def test_reaching_the_threshold_counts_as_past_it(tmp_path, pybin):
    """The operator is `>=` (design dec. 2; design review note 4)."""
    env = {"SESSION_SETTLE_AT": "250000"}
    said(run_shim(payload(transcript(tmp_path, 250_000)), dict(env), pybin))
    assert_silent(run_shim(payload(transcript(tmp_path, 249_999)), dict(env), pybin))


# 3. Threshold over the count: silent ---------------------------------------

def test_under_the_threshold_is_silent(tmp_path, pybin):
    under = transcript(tmp_path, 100_000, 249_000)
    assert_silent(run_shim(payload(under), {"SESSION_SETTLE_AT": "250k"}, pybin))


def test_the_count_is_the_last_row_not_the_peak(tmp_path, pybin):
    """A session that compacted back under its mark is not told."""
    compacted = transcript(tmp_path, 400_000, 60_000)
    assert_silent(run_shim(payload(compacted), {"SESSION_SETTLE_AT": "250k"}, pybin))


def test_a_synthetic_zero_tail_is_silent_even_with_the_peak_over(tmp_path, pybin):
    """Design review note 1: `used == 0` after a peak over the mark."""
    path = tmp_path / f"{SID}.jsonl"
    path.write_text(usage_row(400_000) + "\n"
                    + usage_row(0, model=window.SYNTHETIC_MODEL) + "\n")
    assert window.scan(path).used == 0 and window.scan(path).peak == 400_000
    assert_silent(run_shim(payload(path), {"SESSION_SETTLE_AT": "250k"}, pybin))


def test_no_transcript_path_falls_back_to_the_projects_dir(tmp_path, pybin):
    projects = tmp_path / "projects"
    cwd = "/work/some.project"
    home = projects / window.encode_dir(cwd)
    home.mkdir(parents=True)
    transcript(home, FAR_OVER)
    env = {"SESSION_SETTLE_AT": "250k", "CLAUDE_PROJECTS_DIR": str(projects)}
    line = said(run_shim(payload(None, cwd=cwd), env, pybin))
    assert "250K" in line


# 4. The override replaces the line -----------------------------------------

def test_the_override_replaces_the_line_and_fills_at_and_used(tmp_path, pybin):
    over = transcript(tmp_path, 312_000)
    env = {"SESSION_SETTLE_AT": "250k",
           "SESSION_SETTLE_SAY": "Mark {at}, now {used}: wrap up, {at}."}
    assert said(run_shim(payload(over), env, pybin)) == "Mark 250K, now 312K: wrap up, 250K."


@pytest.mark.parametrize("say", ["a lone { brace", "{0} {used!r} {", "}{", "100% {"])
def test_a_stray_brace_in_the_override_is_text_not_an_error(tmp_path, pybin, say):
    over = transcript(tmp_path, 312_000)
    env = {"SESSION_SETTLE_AT": "250k", "SESSION_SETTLE_SAY": say}
    assert said(run_shim(payload(over), env, pybin)) == say


def test_an_empty_override_is_the_default(tmp_path, pybin):
    over = transcript(tmp_path, 312_000)
    env = {"SESSION_SETTLE_AT": "250k", "SESSION_SETTLE_SAY": "  "}
    assert said(run_shim(payload(over), env, pybin)).startswith("You're past 250K.")


def test_a_multiline_override_is_still_one_line(tmp_path, pybin):
    over = transcript(tmp_path, 312_000)
    env = {"SESSION_SETTLE_AT": "250k", "SESSION_SETTLE_SAY": "first\n\nsecond {at}\n"}
    assert said(run_shim(payload(over), env, pybin)) == "first second 250K"


# 5. The off switch, plugin still loaded ------------------------------------

@pytest.mark.parametrize("off", ["off", "OFF", "Off", "0", "false", "FALSE", "no", "No",
                                 " off ", " OFF ", "\tNo\n"])
def test_the_off_switch_silences_it(tmp_path, pybin, off):
    over = transcript(tmp_path, FAR_OVER)
    env = {"SESSION_SETTLE_AT": "250k", "SESSIONTOOLS_HOOK_SETTLE": off}
    assert_silent(run_shim(payload(over), env, pybin))


@pytest.mark.parametrize("on", ["", "on", "1", "yes", "true"])
def test_anything_else_leaves_it_on(tmp_path, pybin, on):
    over = transcript(tmp_path, FAR_OVER)
    env = {"SESSION_SETTLE_AT": "250k", "SESSIONTOOLS_HOOK_SETTLE": on}
    said(run_shim(payload(over), env, pybin))


# 6. A failed or unavailable read: no output, exit 0, empty stderr ----------

def test_a_failed_read_is_silent(tmp_path, pybin):
    env = {"SESSION_SETTLE_AT": "1"}
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    no_usage = tmp_path / "no-usage.jsonl"
    no_usage.write_text('{"type": "user", "message": {"content": "hi"}}\n{half')
    unreadable = transcript(tmp_path, FAR_OVER, name="locked")
    unreadable.chmod(0)
    cases = [
        payload(tmp_path / "missing.jsonl", session_id=""),     # nothing to find
        payload(tmp_path / "missing.jsonl"),                    # glob finds nothing
        payload(empty), payload(no_usage), payload(tmp_path),   # a directory
        "", "garbage", "[1, 2]", "null", '"a string"', "{",
        json.dumps({"transcript_path": 7, "session_id": 9}),
    ]
    if os.geteuid() != 0:
        cases.append(payload(unreadable))
    for body in cases:
        assert_silent(run_shim(body, {**env, "CLAUDE_PROJECTS_DIR": str(tmp_path / "p")},
                               pybin))
    unreadable.chmod(0o600)


def test_a_fifo_named_as_the_transcript_never_blocks(tmp_path, pybin):
    fifo = tmp_path / f"{SID}.jsonl"
    os.mkfifo(fifo)
    env = {"SESSION_SETTLE_AT": "1", "CLAUDE_PROJECTS_DIR": str(tmp_path / "p")}
    assert_silent(run_shim(payload(fifo), env, pybin))


def test_no_python3_on_path_is_silent(tmp_path):
    over = transcript(tmp_path, FAR_OVER)
    assert_silent(run_shim(payload(over), {"SESSION_SETTLE_AT": "1"}, None, system=False))


@pytest.mark.parametrize("body", ['echo "Traceback: boom" >&2; exit 1',
                                  'echo "bad" >&2; exit 2', "kill -9 $$"])
def test_a_python3_that_fails_is_silent(tmp_path, body):
    bindir = tmp_path / "broken"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    over = transcript(tmp_path, FAR_OVER)
    assert_silent(run_shim(payload(over), {"SESSION_SETTLE_AT": "1"}, bindir))


OLD_PYTHON = Path("/usr/bin/python3")


def _old_system_python() -> bool:
    if not OLD_PYTHON.is_file():
        return False
    try:
        out = subprocess.run([str(OLD_PYTHON), "-c",
                              "import sys; print(sys.version_info >= (3, 11))"],
                             capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "False"


@pytest.mark.skipif(not _old_system_python(), reason="no python3 older than 3.11 at /usr/bin")
def test_a_python3_older_than_3_11_is_silent(tmp_path):
    over = transcript(tmp_path, FAR_OVER)
    assert_silent(run_shim(payload(over), {"SESSION_SETTLE_AT": "1"}, None))


# 6b. The interpreter: the one busy-sound runs (ST-208) ----------------------
#
# `uv tool install` puts `session` on PATH and keeps its python off it, and
# a clean Mac's PATH python3 is Apple's 3.9.6. So the hook takes the python
# from the `session` command's shebang, as busy-sound does, through the ONE
# picker both source: hooks/pick-python.sh.

PICKER = ROOT / "hooks" / "pick-python.sh"
BUSY = ROOT / "hooks" / "busy-sound"


def marked_python(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """A `python` that logs each start to its own mark, then runs the real one."""
    mark = tmp_path / f"{name}-started"
    d = tmp_path / name / "bin"
    d.mkdir(parents=True)
    py = d / "python"
    py.write_text(f'#!/bin/sh\necho x >> "{mark}"\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    return py, mark


def starts(mark: Path) -> int:
    try:
        return len(mark.read_text().split())
    except FileNotFoundError:
        return 0


def tool_bin(tmp_path: Path, shebang: str, python3: str | None = None) -> Path:
    """A bin dir as `uv tool install` leaves it: a `session` whose first line
    is SHEBANG (the shim only reads that line), and, when PYTHON3 is given,
    a `python3` stub running that sh body, first on PATH."""
    bindir = tmp_path / "toolbin"
    bindir.mkdir()
    session = bindir / "session"
    session.write_text(f"{shebang}\nimport sys\nsys.exit('never run')\n")
    session.chmod(0o755)
    if python3 is not None:
        stub = bindir / "python3"
        stub.write_text(f"#!/bin/sh\n{python3}\n")
        stub.chmod(0o755)
    return bindir


#: A PATH python3 that must never run the hook: it exits non-zero.
REFUSES = 'echo "PATH python3 ran" >&2; exit 3'


def test_the_session_shebang_python_runs_the_hook_over_a_path_python3(tmp_path):
    venv, mark = marked_python(tmp_path, "venv")
    bindir = tool_bin(tmp_path, f"#!{venv}", python3=REFUSES)
    over = transcript(tmp_path, FAR_OVER)
    line = said(run_shim(payload(over), {"SESSION_SETTLE_AT": "1"}, bindir))
    assert line.startswith("You're past 1.")
    assert starts(mark) == 1


@pytest.mark.skipif(not _old_system_python(), reason="no python3 older than 3.11 at /usr/bin")
def test_the_session_shebang_python_wins_over_an_old_path_python3(tmp_path):
    """The stranger's Mac: PATH is the tool bin and the system dirs, whose
    python3 is older than 3.11. Before ST-208 this was silent."""
    venv, mark = marked_python(tmp_path, "venv")
    bindir = tool_bin(tmp_path, f"#!{venv}")
    over = transcript(tmp_path, FAR_OVER)
    env = {"SESSION_SETTLE_AT": "1"}
    said(run_shim(payload(over), env, bindir))
    assert starts(mark) == 1
    # The same PATH with no `session` is the old behaviour: silent.
    (bindir / "session").unlink()
    assert_silent(run_shim(payload(over), env, bindir))
    assert starts(mark) == 1


def test_sessiontools_python_wins_over_the_session_shebang(tmp_path):
    venv, venv_mark = marked_python(tmp_path, "venv")
    alt, alt_mark = marked_python(tmp_path, "override")
    bindir = tool_bin(tmp_path, f"#!{venv}", python3=REFUSES)
    over = transcript(tmp_path, FAR_OVER)
    env = {"SESSION_SETTLE_AT": "1", "SESSIONTOOLS_PYTHON": str(alt)}
    said(run_shim(payload(over), env, bindir))
    assert (starts(alt_mark), starts(venv_mark)) == (1, 0)


@pytest.mark.parametrize("override", ["/no/such/python", "no-such-python"])
def test_a_sessiontools_python_that_cannot_run_falls_back_to_python3(tmp_path, pybin,
                                                                      override):
    over = transcript(tmp_path, FAR_OVER)
    env = {"SESSION_SETTLE_AT": "1", "SESSIONTOOLS_PYTHON": override}
    said(run_shim(payload(over), env, pybin))


def test_no_session_on_path_falls_back_to_python3(tmp_path):
    py3, mark = marked_python(tmp_path, "path")
    bindir = tmp_path / "plain"
    bindir.mkdir()
    (bindir / "python3").symlink_to(py3)
    assert not (bindir / "session").exists()
    over = transcript(tmp_path, FAR_OVER)
    said(run_shim(payload(over), {"SESSION_SETTLE_AT": "1"}, bindir, system=False))
    assert starts(mark) == 1


@pytest.mark.parametrize("shebang", [
    "#!/usr/bin/env python3",           # through env: a space, so not a bare path
    "#!{venv} -I",                      # a bare path with an argument
    "#!/no/such/dir/python3",           # a python that is not there
    "import sys",                       # no shebang at all
])
def test_a_session_shebang_that_is_not_a_bare_python_path_falls_back_to_python3(
        tmp_path, shebang):
    venv, venv_mark = marked_python(tmp_path, "venv")
    py3, py3_mark = marked_python(tmp_path, "path")
    bindir = tool_bin(tmp_path, shebang.format(venv=venv))
    (bindir / "python3").symlink_to(py3)
    over = transcript(tmp_path, FAR_OVER)
    said(run_shim(payload(over), {"SESSION_SETTLE_AT": "1"}, bindir))
    assert (starts(py3_mark), starts(venv_mark)) == (1, 0)


@pytest.mark.parametrize("env", [{}, {"SESSION_SETTLE_AT": ""},
                                 {"SESSION_SETTLE_AT": "1", "SESSIONTOOLS_HOOK_SETTLE": "off"}],
                         ids=["unset", "empty", "switched-off"])
def test_no_python_starts_unset_or_switched_off_whatever_the_picker_would_pick(
        tmp_path, env):
    venv, venv_mark = marked_python(tmp_path, "venv")
    alt, alt_mark = marked_python(tmp_path, "override")
    bindir = tool_bin(tmp_path, f"#!{venv}", python3=REFUSES)
    over = transcript(tmp_path, FAR_OVER)
    assert_silent(run_shim(payload(over), {**env, "SESSIONTOOLS_PYTHON": str(alt)}, bindir))
    assert_silent(run_shim(payload(over), dict(env), bindir))
    assert (starts(venv_mark), starts(alt_mark)) == (0, 0)


@pytest.mark.parametrize("body", ['echo "Traceback: boom" >&2; exit 1', "kill -9 $$"])
def test_a_session_shebang_python_that_fails_is_silent(tmp_path, body):
    venv = tmp_path / "venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_text(f"#!/bin/sh\n{body}\n")
    venv.chmod(0o755)
    bindir = tool_bin(tmp_path, f"#!{venv}")
    over = transcript(tmp_path, FAR_OVER)
    assert_silent(run_shim(payload(over), {"SESSION_SETTLE_AT": "1"}, bindir))


def test_a_hook_dir_without_the_picker_is_silent(tmp_path, pybin):
    """A partial plugin copy: `.` of a missing file would exit 1 (measured,
    macOS /bin/sh), so the shim checks first and exits 0."""
    hooks = tmp_path / "plugin" / "hooks"
    hooks.mkdir(parents=True)
    shim = hooks / "settle-hook"
    shim.write_text(SHIM.read_text())
    shim.chmod(0o755)
    (tmp_path / "plugin" / "src").symlink_to(ROOT / "src")
    over = transcript(tmp_path, FAR_OVER)
    full = {"PATH": f"{pybin}:/usr/bin:/bin", "HOME": "/nonexistent", "SESSION_SETTLE_AT": "1"}
    result = subprocess.run(["/bin/sh", str(shim)], input=json.dumps(payload(over)),
                            env=full, capture_output=True, text=True, timeout=30,
                            check=False)
    assert_silent(result)
    # With the picker beside it, the same copy speaks: the test isolates it.
    (hooks / "pick-python.sh").write_text(PICKER.read_text())
    result = subprocess.run(["/bin/sh", str(shim)], input=json.dumps(payload(over)),
                            env=full, capture_output=True, text=True, timeout=30,
                            check=False)
    said(result)


def test_both_hooks_source_the_one_picker_and_neither_keeps_a_copy():
    picker = PICKER.read_text()
    assert "pick() {" in picker
    assert not PICKER.stat().st_mode & stat.S_IXUSR, "a sourced fragment, not a hook"
    for hook in (SHIM, BUSY):
        text = hook.read_text()
        assert '. "$here/pick-python.sh"' in text, hook.name
        assert "pick() {" not in text, hook.name
        assert "SESSIONTOOLS_PYTHON:-" not in text, hook.name


def test_the_shim_never_runs_a_bare_python3():
    """The bug: `command -v python3 || exit 0`, then PATH python3."""
    code = [line for line in SHIM.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert not any(re.search(r"(^|[\s;|&(])python3\b", line) for line in code), code


# 7. A subagent's batch is silent -------------------------------------------

def test_a_subagent_batch_is_silent(tmp_path, pybin):
    over = transcript(tmp_path, FAR_OVER)
    env = {"SESSION_SETTLE_AT": "1"}
    assert_silent(run_shim(payload(over, agent_id="a1b2c3", agent_type="Explore"),
                           dict(env), pybin))
    # agent_type alone is a main thread run with --agent: it is told.
    said(run_shim(payload(over, agent_type="worker"), dict(env), pybin))


# 8. Nothing decides on who the session is ----------------------------------

FORBIDDEN = re.compile(r"TM_AGENT_ADDRESS|\b(?:role|seat|crew)s?\b", re.IGNORECASE)


@pytest.mark.parametrize("source", [MODULE, SHIM], ids=lambda p: p.name)
def test_no_role_seat_or_crew_check_anywhere_in_the_source(source):
    """Any session with a threshold is told. The words are kept
    out of the comments too, so a match is never explained away."""
    hits = [(n, line) for n, line in enumerate(source.read_text().splitlines(), 1)
            if FORBIDDEN.search(line)]
    assert hits == []


def test_the_pattern_would_catch_them():
    for text in ("os.environ['TM_AGENT_ADDRESS']", "if role == 'worker'", "Seat",
                 "crews", "the seat's"):
        assert FORBIDDEN.search(text), text
    for text in ("agent_id", "roleplay", "seated", "unseat"):
        assert not FORBIDDEN.search(text), text


# 9. Plugin wiring ----------------------------------------------------------

def test_hooks_json_declares_post_tool_batch_to_the_shim():
    spec = json.loads(HOOKS.read_text())
    entries = spec["hooks"]["PostToolBatch"]
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    # settle-hook first; the busy sound's batch end (ST-66) runs beside it.
    assert commands[0] == '"${CLAUDE_PLUGIN_ROOT}/hooks/settle-hook"'
    assert commands.count('"${CLAUDE_PLUGIN_ROOT}/hooks/settle-hook"') == 1
    assert all(h["type"] == "command" and h["timeout"] == 5
               for entry in entries for h in entry["hooks"])


def test_hooks_json_says_what_it_does_and_how_to_switch_it_off():
    """The hook rules: visibly in the plugin, switchable on its own."""
    description = json.loads(HOOKS.read_text())["description"]
    for word in ("settle-hook", "PostToolBatch", settle.AT_ENV, settle.SAY_ENV,
                 f"{settle.SWITCH_ENV}=off"):
        assert word in description, word


def test_the_shim_exists_is_executable_and_posix_sh():
    assert SHIM.is_file()
    assert SHIM.stat().st_mode & stat.S_IXUSR
    assert SHIM.read_text().startswith("#!/bin/sh\n")


def test_the_variable_names_agree_between_the_shim_and_the_module():
    text = SHIM.read_text()
    assert settle.AT_ENV in text and settle.SWITCH_ENV in text


# The pure functions ---------------------------------------------------------

@pytest.mark.parametrize(("raw", "at"), [("250000", 250_000), ("250k", 250_000),
                                         ("250 K", 250_000), ("1.5M", 1_500_000),
                                         ("1m", 1_000_000), ("0.5k", 500), ("1", 1),
                                         ("", None), ("x", None), ("-1", None),
                                         ("0.0001", None), ("1e400", None),
                                         ("-inf", None), ("M", None)])
def test_threshold_parses(raw, at):
    assert settle.threshold({"SESSION_SETTLE_AT": raw}) == at


@pytest.mark.parametrize(("value", "off"), [("off", True), (" OFF ", True), ("False", True),
                                            ("0", True), ("NO", True), ("", False),
                                            ("on", False), ("offline", False)])
def test_switched_off(value, off):
    assert settle.switched_off({"SESSIONTOOLS_HOOK_SETTLE": value}) is off


def test_threshold_unset_is_none():
    assert settle.threshold({}) is None


def test_decide_is_none_unless_asked(tmp_path):
    over = transcript(tmp_path, FAR_OVER)
    assert settle.decide({}, payload(over)) is None
    assert settle.decide({"SESSION_SETTLE_AT": "1"}, "not a dict") is None
    assert settle.decide({"SESSION_SETTLE_AT": "1"}, payload(over)) is not None


def test_main_never_raises_and_always_returns_0(tmp_path):
    class Exploding(io.StringIO):
        def read(self, *a):
            raise KeyboardInterrupt

    out = io.StringIO()
    assert settle.main(Exploding(), out, {"SESSION_SETTLE_AT": "1"}) == 0
    assert out.getvalue() == ""


def test_main_reads_no_stdin_without_a_threshold():
    class Untouchable(io.StringIO):
        def read(self, *a):
            raise AssertionError("stdin was read")

    out = io.StringIO()
    assert settle.main(Untouchable(), out, {}) == 0
    assert settle.main(Untouchable(), out, {"SESSION_SETTLE_AT": "1",
                                            "SESSIONTOOLS_HOOK_SETTLE": "off"}) == 0
    assert out.getvalue() == ""


def test_main_echoes_the_event_name(tmp_path):
    over = transcript(tmp_path, FAR_OVER)
    out = io.StringIO()
    body = json.dumps(payload(over, hook_event_name="PostToolUse"))
    assert settle.main(io.StringIO(body), out, {"SESSION_SETTLE_AT": "1"}) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
