"""The hidden-failure note, in process: what `pre` and `post` decide, the
note's text, and the hook JSON (ST-319).

The shim and its environment gate are tested through the real shim in
test_hidden_failure_hook.py. Every payload here is synthetic: no transcript
content, no real output.
"""

from __future__ import annotations

import io
import json

import pytest

from sessiontools import hidden_failure as hf

SEAT = {"TASKMASTER_ROLE": "worker"}
KEEP = "cmd > log 2>&1; s=$?; tail log; exit $s"
FAILED = "tests/test_a.py::test_x FAILED\n==== 3 failed, 12 passed in 1.2s ====\n"
GREEN = "==== 15 passed in 1.2s ====\n"


def response(stdout: str = "", stderr: str = "", **extra) -> dict:
    """A success-path Bash tool_response, shaped as do-001 measured it on
    Claude Code 2.1.325 (keys only; values synthetic)."""
    return {"stdout": stdout, "stderr": stderr, "interrupted": False,
            "isImage": False, "noOutputExpected": False, **extra}


def body(command: str, tool_response: object = None, event: str = "PostToolUse",
         **extra) -> dict:
    out = {"session_id": "s", "hook_event_name": event, "tool_name": "Bash",
           "tool_input": {"command": command}, **extra}
    if tool_response is not None:
        out["tool_response"] = tool_response
    return out


def post(command: str, tool_response: object) -> str | None:
    return hf.post(body(command, tool_response))


def pre(command: str) -> str | None:
    return hf.pre(body(command, event="PreToolUse"))


# 1. PostToolUse: gate | tail with failure text and exit 0 ------------------

def test_gate_into_tail_with_failure_text_gets_exactly_one_note():
    note = post("uv run pytest -q 2>&1 | tail -20", response(FAILED))
    assert note is not None and "\n" not in note
    assert note == ("[hidden-failure] uv run pytest | tail exited 0 but its output "
                    "shows a failure (pytest_failed): the pipe reported tail's "
                    "status, not uv run pytest's. Keep the gate's status: " + KEEP)


def test_a_green_run_gets_no_note():
    assert post("pytest -q | tail -5", response(GREEN)) is None


def test_gate_into_head_with_failure_text_gets_a_note_too():
    note = post("make check 2>&1 | head -40", response("make: *** [test] Error 1\n"))
    assert note is not None
    assert note.startswith("[hidden-failure] make | head exited 0 ")
    assert "(make_error)" in note


def test_the_failure_text_may_be_on_stderr():
    note = post("git push 2>&1 | tail -3", response("", "error: failed to push some refs\n"))
    assert note is not None and "git push | tail" in note
    assert "(fatal_or_error_prefix)" in note


@pytest.mark.parametrize(("text", "label"), [
    ("FAILED tests/x.py::y\n", "pytest_failed"),
    ("1 error in 0.3s\n", "pytest_failed"),
    ("make[2]: *** [all] Error 2\n", "make_error"),
    ("Traceback (most recent call last):\n", "traceback"),
    ("fatal: not a git repository\n", "fatal_or_error_prefix"),
    ("sh: pytest: command not found\n", "not_found"),
    ("Permission denied\n", "permission_denied"),
    ("nothing to commit, working tree clean\n", "git_nothing_to_commit"),
])
def test_each_marker_names_its_label(text, label):
    note = post("pytest | tail", response(text))
    assert note is not None and f"({label})" in note


@pytest.mark.parametrize("command", [
    "pytest -q 2>&1 | tail -20",
    "FOO=1 nice -n 19 timeout 60 make check 2>&1 | tail -5",
    "python -m pytest tests/ | tail",
    "git commit -m wip 2>&1 | tail -3",
    "message send a -s b -f c | head -2",
    "make |& tail",
    "cd x && pytest | tail -5",
    "pytest | tail -5 && echo ok",
    "pytest | grep -v warn | tail -5",
])
def test_the_gates_and_shapes_that_get_a_note(command):
    assert post(command, response(FAILED)) is not None


# 2. ... and every non-zero or unknown status gets none ------------------------

@pytest.mark.parametrize("tool_response", [
    response(FAILED, interrupted=True),
    response(FAILED, returnCodeInterpretation="No matches found"),
    response(FAILED, backgroundTaskId="b1"),
    "Exit code 0\n" + FAILED,                    # not an object
    [FAILED],
    None,                                        # PostToolUseFailure has no tool_response
    response(FAILED, interrupted="no"),          # an odd type is an unknown status
    response(FAILED, returnCodeInterpretation=1),
    {"stdout": FAILED.encode().hex(), "stderr": 7},
])
def test_a_non_zero_or_unknown_status_gets_no_note(tool_response):
    assert post("pytest -q | tail -20", tool_response) is None


def test_an_empty_interpretation_or_background_id_is_not_a_status():
    note = post("pytest | tail", response(FAILED, returnCodeInterpretation="",
                                          backgroundTaskId=""))
    assert note is not None


# 3. What is not a gate | head/tail -------------------------------------------

@pytest.mark.parametrize("command", [
    "pytest || tail -5 log",                         # `||` is not a pipe
    "make test || echo failed",
    "echo 'pytest | tail' > note.txt",               # quoted text is not code
    'grep -E "make|tail" Makefile',
    "cat > f.sh <<'EOF'\npytest | tail -5\nEOF\necho done",   # heredoc body
    "x=$(pytest | tail -1); echo $x",                # nested in $( )
    "ls | tail -5",                                  # not a gate
    "git log | head -5",                             # not a gate
    "pytest | tail -5; echo done",                   # not the last statement
    "pytest | tail -5 || echo fallback",             # the and-or list masks it
    "for f in a b; do pytest $f | tail -1; done",    # inside a compound
    "pytest | wc -l",                                # reads to EOF
    "set -o pipefail; pytest | tail -5",             # pipefail keeps the status
    "tail -5 log | make",                            # the reader is upstream
    "pytest",
    "",
])
def test_no_note(command):
    assert post(command, response(FAILED)) is None


# 4. PreToolUse: gate | head, never tail ---------------------------------------

def test_gate_into_head_gets_the_pre_line():
    line = pre("make test 2>&1 | head -50")
    assert line == ("[hidden-failure] make | head reports head's status, not make's, "
                    "and head may cut off the failure. Keep the gate's status: " + KEEP)


@pytest.mark.parametrize("command", [
    "make test 2>&1 | tail -50",          # tail: no PreToolUse line
    "pytest || head -5 log",
    "echo 'make | head'",
    "ls | head",
    "set -o pipefail; make | head",
    "make | head; echo done",
])
def test_no_pre_line(command):
    assert pre(command) is None


def test_pre_needs_no_tool_response():
    assert hf.pre(body("pytest | head -3", event="PreToolUse")) is not None


# 5. Privacy: the note quotes nothing but the gate's name ----------------------

def test_the_note_quotes_no_output_and_no_argument():
    secret_out = "SECRET-OUT-7f3a\nFAILED tests/t.py::SECRET-TEST\n"
    command = "uv run pytest tests/SECRET-PATH -k SECRET-K 2>&1 | tail -SECRET"
    for line in (post(command, response(secret_out, "SECRET-ERR")),
                 hf.pre(body(command.replace("tail", "head"), event="PreToolUse"))):
        assert line is not None
        assert "SECRET" not in line


# 6. The payload ------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    None, [], "pytest | tail", {"tool_name": "Bash"},
    {"tool_name": "Bash", "tool_input": "pytest | tail"},
    {"tool_name": "Bash", "tool_input": {"command": ["pytest", "|", "tail"]}},
    {"tool_name": "Read", "tool_input": {"command": "pytest | tail"},
     "tool_response": response(FAILED)},
])
def test_an_odd_payload_is_silent(payload):
    assert hf.pre(payload) is None and hf.post(payload) is None


def test_a_subagents_call_is_not_silenced():
    """design.md decision 8: the note is about the call, and a subagent's
    Bash hides a failure the same way."""
    payload = body("pytest | tail", response(FAILED), agent_id="a1")
    assert hf.post(payload) is not None


# 7. The gate on the environment ---------------------------------------------------

@pytest.mark.parametrize(("env", "on"), [
    ({}, False),
    ({"TASKMASTER_ROLE": ""}, False),
    ({"CLAUDE_CODE_AGENT": ""}, False),
    ({"CLAUDE_CODE_SESSION_ATTENDED": "1"}, False),    # seats carry it too: not a marker
    ({"TASKMASTER_ROLE": "worker"}, True),
    ({"CLAUDE_CODE_AGENT": "wimo"}, True),
    ({**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "off"}, False),
    ({**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "OFF"}, False),
    ({**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "0"}, False),
    ({**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "False"}, False),
    ({**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "no"}, False),
    ({**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "on"}, True),
    ({**SEAT, "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": ""}, True),
])
def test_active_on_seats_only_and_off_by_switch(env, on):
    assert hf.active(env) is on


# 8. main(): the hook JSON, and never a decision ---------------------------------

def run_main(verb: str, payload: object, env: dict | None = None) -> str:
    out = io.StringIO()
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    assert hf.main(verb, stdin, out, SEAT if env is None else env) == 0
    return out.getvalue()


@pytest.mark.parametrize(("verb", "payload", "event"), [
    ("post", body("pytest | tail", response(FAILED)), "PostToolUse"),
    ("pre", body("pytest | head", event="PreToolUse"), "PreToolUse"),
])
def test_main_prints_additional_context_and_nothing_else(verb, payload, event):
    out = run_main(verb, payload)
    assert out.endswith("\n") and out.count("\n") == 1
    data = json.loads(out)
    assert data == {"hookSpecificOutput": {"hookEventName": event,
                                           "additionalContext": data["hookSpecificOutput"]["additionalContext"]}}
    for key in ("decision", "permissionDecision", "updatedInput", "continue", "reason"):
        assert key not in out


@pytest.mark.parametrize(("verb", "payload", "env"), [
    ("post", body("pytest | tail", response(GREEN)), SEAT),
    ("post", body("pytest | tail", response(FAILED)), {}),
    ("pre", body("pytest | head"), {"CLAUDE_CODE_AGENT": "x",
                                    "SESSIONTOOLS_HOOK_HIDDEN_FAILURE": "off"}),
    ("stop", body("pytest | tail", response(FAILED)), SEAT),
    ("", body("pytest | tail", response(FAILED)), SEAT),
    ("post", "{not json", SEAT),
    ("post", "", SEAT),
])
def test_main_is_silent_and_returns_0(verb, payload, env):
    assert run_main(verb, payload, env) == ""


def test_main_swallows_a_crash(monkeypatch):
    def boom(_payload):
        raise RuntimeError("x")
    monkeypatch.setattr(hf, "post", boom)
    assert run_main("post", body("pytest | tail", response(FAILED))) == ""


def test_main_reads_nothing_when_off():
    class Untouchable(io.StringIO):
        def read(self, *_a):
            raise AssertionError("stdin read while off")
    assert hf.main("post", Untouchable(), io.StringIO(), {}) == 0
