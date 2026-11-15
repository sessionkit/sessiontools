"""The hidden-failure note THROUGH THE REAL SHIM, as `hooks/hooks.json`
runs it: `sh hooks/hidden-failure pre|post` with the payload on stdin and
an environment built from nothing, so no variable of the machine running
the suite leaks in (ST-319).

"Python never starts" is proved, not assumed: the `python3` the shim would
pick is a stub that leaves a mark file before it runs the real one.
Synthetic payloads only.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sessiontools import hidden_failure as hf

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "hidden-failure"
HOOKS = ROOT / "hooks" / "hooks.json"
MODULE = ROOT / "src" / "sessiontools" / "hidden_failure.py"

SEAT = {"TASKMASTER_ROLE": "worker"}
FAILED = "tests/test_a.py::test_x FAILED\n==== 3 failed, 12 passed in 1.2s ====\n"
ENTRY = '"${CLAUDE_PLUGIN_ROOT}/hooks/hidden-failure"'


def response(stdout: str = "", **extra) -> dict:
    return {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False,
            "noOutputExpected": False, **extra}


def post_body(command: str = "pytest -q 2>&1 | tail -20", tool_response: object = None) -> dict:
    return {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": response(FAILED) if tool_response is None else tool_response}


def pre_body(command: str = "make test 2>&1 | head -40") -> dict:
    return {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}}


@pytest.fixture
def pybin(tmp_path: Path) -> Path:
    """A bin dir whose `python3` marks that it ran, then runs this suite's."""
    bindir = tmp_path / "pybin"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/sh\n: > "{tmp_path}/python-ran"\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    return bindir


def ran_python(bindir: Path) -> bool:
    return (bindir.parent / "python-ran").exists()


def run_shim(verb: str, stdin: str | dict, env: dict, bindir: Path | None, *,
             system: bool = True, shim: Path = SHIM) -> subprocess.CompletedProcess:
    path = [str(bindir)] if bindir else []
    if system:
        path += ["/usr/bin", "/bin"]
    full = {"PATH": os.pathsep.join(path), "HOME": "/nonexistent", **env}
    data = stdin if isinstance(stdin, str) else json.dumps(stdin)
    argv = ["/bin/sh", str(shim)] + ([verb] if verb else [])
    return subprocess.run(argv, input=data, env=full, capture_output=True, text=True,
                          timeout=30, check=False)


def note(result: subprocess.CompletedProcess, event: str) -> str:
    """The one line the hook said; nothing but additionalContext."""
    assert result.returncode == 0 and result.stderr == "", result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    data = json.loads(lines[0])
    assert list(data) == ["hookSpecificOutput"]
    out = data["hookSpecificOutput"]
    assert sorted(out) == ["additionalContext", "hookEventName"]
    assert out["hookEventName"] == event
    assert "\n" not in out["additionalContext"]
    return out["additionalContext"]


def assert_silent(result: subprocess.CompletedProcess) -> None:
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


# 1. End to end ---------------------------------------------------------------

def test_post_gate_into_tail_with_failure_text_gets_one_note(pybin):
    text = note(run_shim("post", post_body(), SEAT, pybin), "PostToolUse")
    assert text.startswith("[hidden-failure] pytest | tail exited 0 ")
    assert "cmd > log 2>&1; s=$?; tail log; exit $s" in text


def test_post_green_and_non_zero_runs_get_none(pybin):
    assert_silent(run_shim("post", post_body(tool_response=response("15 passed\n")), SEAT, pybin))
    assert_silent(run_shim("post", post_body(tool_response=response(FAILED, interrupted=True)),
                           SEAT, pybin))
    assert_silent(run_shim("post", post_body(tool_response=response(
        FAILED, returnCodeInterpretation="No matches found")), SEAT, pybin))
    assert_silent(run_shim("post", post_body(tool_response=response(
        FAILED, backgroundTaskId="b9")), SEAT, pybin))


def test_pre_gate_into_head_gets_the_line_and_tail_gets_none(pybin):
    text = note(run_shim("pre", pre_body(), {"CLAUDE_CODE_AGENT": "a"}, pybin), "PreToolUse")
    assert text.startswith("[hidden-failure] make | head reports head's status")
    assert_silent(run_shim("pre", pre_body("make test 2>&1 | tail -40"), SEAT, pybin))


def test_the_payload_reaches_python_as_text_never_evaluated(tmp_path, pybin):
    """The payload passes through a here-document: a `$( )` or a backtick in
    it stays text."""
    mark = tmp_path / "evaluated"
    evil = f"$(touch {mark}) `touch {mark}` ${{HOME}} \\\\ \nEOF\n" + FAILED
    text = note(run_shim("post", post_body(tool_response=response(evil)), SEAT, pybin),
                "PostToolUse")
    assert "pytest_failed" in text
    assert not mark.exists()


# 2. Seats only: no marker, no python -------------------------------------------

@pytest.mark.parametrize("env", [
    {},
    {"TASKMASTER_ROLE": ""},
    {"CLAUDE_CODE_AGENT": ""},
    {"CLAUDE_CODE_SESSION_ATTENDED": "1"},
    {**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "off"},
    {**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "Off"},
    {**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "0"},
    {**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "FALSE"},
    {**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "no"},
])
@pytest.mark.parametrize("verb", ["pre", "post"])
def test_no_seat_marker_or_switched_off_is_a_no_op_before_python(pybin, env, verb):
    body = post_body() if verb == "post" else pre_body()
    assert_silent(run_shim(verb, body, env, pybin))
    assert not ran_python(pybin)


@pytest.mark.parametrize(("verb", "body"), [
    ("post", post_body("pytest -q")),                    # no pipe, no head/tail
    ("post", {**post_body("ls -la"), "tool_response": response("ok\n")}),
    ("pre", pre_body("make test 2>&1 | tail -5")),       # pre wants head
    ("pre", pre_body("git status")),
    ("stop", post_body()),                               # not a verb of this hook
    ("", post_body()),
])
def test_a_call_with_nothing_to_find_never_starts_python(pybin, verb, body):
    assert_silent(run_shim(verb, body, SEAT, pybin))
    assert not ran_python(pybin)


#: One output per FAIL_MARKERS label, each matching ONLY that label's pattern
#: as far as the shim's words go: the shim's marker words must let each through.
MARKER_SAMPLES = {
    "pytest_failed": "1 failed in 0.2s\n",
    "make_error": "make[1]: *** [all] Stop.\n",
    "traceback": "Traceback (most recent call last):\n",
    "fatal_or_error_prefix": "fatal: bad object\n",
    "not_found": "sh: x: command not found\n",
    "permission_denied": "Permission denied\n",
    "git_nothing_to_commit": "no changes added to commit\n",
}


def test_marker_samples_cover_every_marker():
    assert set(MARKER_SAMPLES) == set(hf.FAIL_MARKERS)
    for label, text in MARKER_SAMPLES.items():
        assert hf.marker(text) == label, label


@pytest.mark.parametrize("label", sorted(MARKER_SAMPLES))
def test_every_marker_gets_through_the_shims_prefilter(pybin, label):
    body = post_body("make check | tail", response(MARKER_SAMPLES[label]))
    assert f"({label})" in note(run_shim("post", body, SEAT, pybin), "PostToolUse")


@pytest.mark.parametrize("command", [
    "pytest | tail", "python -m pytest | tail", "uv run pytest | tail", "make | tail",
    "/usr/bin/make -C x | tail", "message send a -s b -f c | tail",
    "git commit -m x | tail", "git push | tail",
])
def test_every_gate_gets_through_the_shims_prefilter(pybin, command):
    assert hf.is_gate(hf.stage_name(command.split(" | ")[0].split()), hf.GATES)
    note(run_shim("post", post_body(command), SEAT, pybin), "PostToolUse")


def test_a_green_gate_into_tail_never_starts_python(pybin):
    body = post_body(tool_response=response("==== 15 passed in 1.2s ====\n"))
    assert_silent(run_shim("post", body, SEAT, pybin))
    assert not ran_python(pybin)


def test_a_candidate_call_does_start_python(pybin):
    """The stub's mark is real: the cases above prove something."""
    run_shim("post", post_body(), SEAT, pybin)
    assert ran_python(pybin)


# 3. Fails silent ---------------------------------------------------------------

def test_bad_json_is_silent(pybin):
    assert_silent(run_shim("post", "{not json | tail", SEAT, pybin))


def test_no_python_at_all_is_silent(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert_silent(run_shim("post", post_body(), SEAT, empty, system=False))


def test_an_old_python_is_silent(tmp_path):
    bindir = tmp_path / "old"
    bindir.mkdir()
    stub = bindir / "python3"
    # The shim runs `python3 -I -c PROGRAM FILE VERB`. The stub runs PROGRAM
    # on the real python with sys.version_info set to 3.9, the argv kept.
    wrapper = ("import sys; prog = sys.argv[1]; sys.argv = ['-c'] + sys.argv[2:]; "
               "sys.version_info = (3, 9, 0); exec(compile(prog, '<c>', 'exec'))")
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -c "{wrapper}" "$3" "$4" "$5"\n')
    stub.chmod(0o755)
    assert_silent(run_shim("post", post_body(), SEAT, bindir))
    # the same stub without the 3.9 says the note: the guard is what stopped it
    stub.write_text(stub.read_text().replace("sys.version_info = (3, 9, 0); ", ""))
    assert note(run_shim("post", post_body(), SEAT, bindir), "PostToolUse")


def test_a_crashing_python_is_silent(tmp_path):
    bindir = tmp_path / "crash"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text("#!/bin/sh\necho boom >&2\nkill -SEGV $$\n")
    stub.chmod(0o755)
    assert_silent(run_shim("post", post_body(), SEAT, bindir))


def test_a_missing_module_is_silent(tmp_path, pybin):
    hooks = tmp_path / "plugin" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("hidden-failure", "pick-python.sh"):
        (hooks / name).write_bytes((ROOT / "hooks" / name).read_bytes())
    assert_silent(run_shim("post", post_body(), SEAT, pybin, shim=hooks / "hidden-failure"))


def test_no_picker_beside_it_is_silent(tmp_path, pybin):
    lone = tmp_path / "lone" / "hidden-failure"
    lone.parent.mkdir()
    lone.write_bytes(SHIM.read_bytes())
    assert_silent(run_shim("post", post_body(), SEAT, pybin, shim=lone))
    assert not ran_python(pybin)


# 4. Speed: under 50 ms on a 10 KB output (recorded in docs/hidden-failure-hook.md)

def ten_kb_payload() -> dict:
    lines = [f"tests/test_m{i:03d}.py::test_case PASSED" for i in range(240)]
    out = ("\n".join(lines) + "\n" + FAILED)
    out = out + "." * (10_240 - len(out))
    return post_body("uv run pytest -q 2>&1 | tail -300", response(out))


def wall_ms(verb: str, body: dict, env: dict) -> float:
    start = time.perf_counter()
    run_shim(verb, body, env, None)
    return (time.perf_counter() - start) * 1000


def test_the_10kb_payload_is_10kb_and_gets_the_note():
    body = ten_kb_payload()
    assert len(body["tool_response"]["stdout"]) == 10_240
    assert hf.post(body) is not None


def test_the_shim_is_quick_on_a_10kb_output():
    """A regression guard, not the measurement: the figure (< 50 ms) is
    measured on a quiet host and recorded in the doc. Here the bound is
    loose, so a loaded test host does not flake; importing the package
    (~40 ms more) or a second python would still show over many runs."""
    env = {**SEAT, "SESSIONTOOLS_PYTHON": sys.executable}
    body = ten_kb_payload()
    runs = sorted(wall_ms("post", body, env) for _ in range(5))
    assert runs[2] < 1000, runs


# 5. The module and the shim ---------------------------------------------------

def test_the_module_imports_the_standard_library_only():
    """The shim loads the FILE, never the package: an import of sessiontools
    here would fail there, and the package's imports would cost the budget."""
    tree = ast.parse(MODULE.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= {"__future__", "json", "os", "re", "sys"}, names


def test_the_shim_loads_the_file_and_never_imports_the_package():
    code = "\n".join(line for line in SHIM.read_text().splitlines()
                     if not line.lstrip().startswith("#"))
    assert "spec_from_file_location" in code
    assert "src/sessiontools/hidden_failure.py" in code
    assert not re.search(r"(from|import) sessiontools", code)


def test_the_shim_exists_is_executable_and_posix_sh():
    assert SHIM.is_file()
    assert SHIM.stat().st_mode & stat.S_IXUSR
    assert SHIM.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run(["/bin/sh", "-n", str(SHIM)], check=False).returncode == 0


def test_the_shim_sources_the_one_picker_and_keeps_no_copy():
    text = SHIM.read_text()
    assert '. "$here/pick-python.sh"' in text
    assert "pick() {" not in text
    assert "SESSIONTOOLS_PYTHON:-" not in text


def test_the_shim_checks_the_seat_before_it_reads_stdin():
    code = [line for line in SHIM.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    seat = next(i for i, line in enumerate(code) if "TASKMASTER_ROLE" in line)
    read = next(i for i, line in enumerate(code) if "$(cat)" in line)
    pick = next(i for i, line in enumerate(code) if line.strip() == "pick || exit 0")
    assert seat < read < pick
    assert "CLAUDE_CODE_AGENT" in code[seat]
    assert not any(re.search(r"(^|[\s;|&(])python3\b", line) for line in code), code


def test_the_shim_never_answers_with_a_decision():
    text = SHIM.read_text() + MODULE.read_text()
    for key in ('"decision"', '"permissionDecision"', '"updatedInput"'):
        assert key not in text, key


# 6. Plugin wiring ----------------------------------------------------------------

def test_hooks_json_runs_pre_on_pretooluse_and_post_on_posttooluse_for_bash():
    hooks = json.loads(HOOKS.read_text())["hooks"]
    pre = [(e.get("matcher"), h) for e in hooks["PreToolUse"] for h in e["hooks"]
           if "hidden-failure" in h["command"]]
    post = [(e.get("matcher"), h) for e in hooks["PostToolUse"] for h in e["hooks"]
            if "hidden-failure" in h["command"]]
    assert [(m, h["command"]) for m, h in pre] == [("Bash", f"{ENTRY} pre")]
    assert [(m, h["command"]) for m, h in post] == [("Bash", f"{ENTRY} post")]
    for _m, h in pre + post:
        assert h["type"] == "command" and h["timeout"] == 5


def test_hooks_json_never_registers_posttoolusefailure():
    """do-001: a failed Bash call fires PostToolUseFailure only, and it is
    already visible; the note is for exit 0."""
    hooks = json.loads(HOOKS.read_text())["hooks"]
    assert "PostToolUseFailure" not in hooks


def test_hooks_json_says_what_it_does_and_how_to_switch_it_off():
    description = json.loads(HOOKS.read_text())["description"]
    start = description.index("hidden-failure, the hidden-failure note")
    para = description[start:description.index(
        "(hooks/hidden-failure, sessiontools.hidden_failure)", start)]
    for word in ("TASKMASTER_ROLE", "CLAUDE_CODE_AGENT",
                 f"{hf.SWITCH_ENV}=off", "PostToolUseFailure", "never blocks"):
        assert word in para, word
