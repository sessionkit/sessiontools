"""The session-info hooks: a session title with the short id, and a block of
the session's facts, at SessionStart. Both default off.

The fail-silent and switch criteria are tested THROUGH THE REAL SHIM, as
`hooks/hooks.json` runs it: `sh hooks/session-info <verb>` with the payload
on stdin and an environment built from nothing (PATH, HOME and what the
test adds), never from `os.environ`. A seat running the suite may carry the
switches or the launch-info marker; none of it may leak in. The pure
functions take `env` explicitly.
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

from sessiontools import session_info

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "session-info"
HOOKS = ROOT / "hooks" / "hooks.json"
PICKER = ROOT / "hooks" / "pick-python.sh"
MODULE = ROOT / "src" / "sessiontools" / "session_info.py"

SID = "0f0e0d0c-0b0a-4908-8706-050403020100"
SHORT = "0f0e0d0c"
OTHER = "aaaabbbb-0b0a-4908-8706-050403020100"
SOURCES = ("startup", "resume", "clear", "compact", "fork")
TITLE_ON = {session_info.TITLE_ENV: "on"}
CONTEXT_ON = {session_info.CONTEXT_ENV: "on"}
BOTH_ON = {**TITLE_ON, **CONTEXT_ON}


def payload(**extra) -> dict:
    body = {"session_id": SID, "cwd": "/work/dir", "source": "startup",
            "transcript_path": "/t/0f0e0d0c.jsonl", "hook_event_name": "SessionStart"}
    body.update(extra)
    return {k: v for k, v in body.items() if v is not None}


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


def run_shim(verb: str | None, stdin: str | dict, env: dict, bindir: Path | None, *,
             system: bool = True, shim: Path = SHIM) -> subprocess.CompletedProcess:
    """The shim exactly as Claude Code runs it: stdin JSON, env inherited.
    The env is built from nothing but PATH, HOME and what the test adds."""
    path = [str(bindir)] if bindir else []
    if system:
        path += ["/usr/bin", "/bin"]
    full = {"PATH": os.pathsep.join(path), "HOME": "/nonexistent", **env}
    data = stdin if isinstance(stdin, str) else json.dumps(stdin)
    argv = ["/bin/sh", str(shim)] + ([verb] if verb is not None else [])
    return subprocess.run(argv, input=data, env=full, capture_output=True, text=True,
                          timeout=30, check=False)


def assert_silent(result: subprocess.CompletedProcess) -> None:
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def emitted(result: subprocess.CompletedProcess) -> dict:
    """The hook's one stdout line, parsed: valid SessionStart hook JSON."""
    assert result.returncode == 0 and result.stderr == ""
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    out = json.loads(lines[0])["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    return out


def block_lines(out: dict | None) -> list[str]:
    assert out is not None
    return out["hookSpecificOutput"]["additionalContext"].splitlines()


# 1. The title verb ------------------------------------------------------------

@pytest.mark.parametrize("source", ["startup", "resume", "fork", None])
def test_the_title_is_the_short_id_on_startup_resume_fork_and_no_source(pybin, source):
    out = emitted(run_shim("title", payload(source=source), dict(TITLE_ON), pybin))
    assert out == {"hookEventName": "SessionStart", "sessionTitle": SHORT}


@pytest.mark.parametrize("source", ["clear", "compact", "something-new"])
def test_no_title_where_claude_code_does_not_apply_it(pybin, source):
    assert_silent(run_shim("title", payload(source=source), dict(TITLE_ON), pybin))


@pytest.mark.parametrize("source", ["startup", "resume", "fork"])
def test_a_custom_title_is_never_overwritten(pybin, source):
    body = payload(source=source, session_title="my session")
    assert_silent(run_shim("title", body, dict(TITLE_ON), pybin))


def test_an_empty_session_title_is_no_title():
    out = session_info.title_output(payload(session_title=""), TITLE_ON)
    assert out == {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                          "sessionTitle": SHORT}}


def test_a_short_session_id_is_the_whole_title():
    out = session_info.title_output(payload(session_id="abc"), TITLE_ON)
    assert out is not None and out["hookSpecificOutput"]["sessionTitle"] == "abc"


def test_the_title_verb_says_nothing_of_the_context():
    out = session_info.title_output(payload(), BOTH_ON)
    assert out is not None and set(out["hookSpecificOutput"]) == {"hookEventName",
                                                                  "sessionTitle"}


# 2. The context verb ----------------------------------------------------------

def test_the_context_block_carries_the_four_facts(pybin):
    out = emitted(run_shim("context", payload(), dict(CONTEXT_ON), pybin))
    assert set(out) == {"hookEventName", "additionalContext"}
    assert out["additionalContext"].splitlines() == [
        "# Session Info",
        "",
        f"- **Session ID**: {SID}",
        "- **Working directory**: /work/dir",
        "- **Source**: startup",
        "- **Transcript**: /t/0f0e0d0c.jsonl",
    ]


@pytest.mark.parametrize("source", SOURCES)
def test_the_context_block_fires_on_every_source(source):
    lines = block_lines(session_info.context_output(payload(source=source), CONTEXT_ON))
    assert f"- **Source**: {source}" in lines
    assert f"- **Session ID**: {SID}" in lines


@pytest.mark.parametrize("key", ["cwd", "source", "transcript_path"])
@pytest.mark.parametrize("value", [None, "", 7, ["x"]])
def test_an_absent_or_empty_field_is_omitted_never_printed(key, value):
    lines = block_lines(session_info.context_output(payload(**{key: value}), CONTEXT_ON))
    label = {k: lbl for lbl, k in session_info.FIELDS}[key]
    assert not any(line.startswith(f"- **{label}**") for line in lines)
    assert not any("None" in line for line in lines)
    assert len(lines) == 5


def test_the_context_verb_sets_no_title():
    out = session_info.context_output(payload(), BOTH_ON)
    assert out is not None and "sessionTitle" not in out["hookSpecificOutput"]


# 3. The dedup with launch-info: startup only ----------------------------------

def marker(value: str) -> dict:
    return {**CONTEXT_ON, session_info.LAUNCH_INFO_ENV: value}


def test_an_equal_marker_on_startup_omits_the_session_id_line(pybin):
    out = emitted(run_shim("context", payload(), marker(SID), pybin))
    lines = out["additionalContext"].splitlines()
    assert not any("Session ID" in line for line in lines)
    assert lines[2:] == ["- **Working directory**: /work/dir", "- **Source**: startup",
                         "- **Transcript**: /t/0f0e0d0c.jsonl"]


def test_an_equal_marker_with_no_source_counts_as_startup():
    """No source is startup for the dedup, as for the title; the Source line
    stays omitted, since the harness sent none."""
    lines = block_lines(session_info.context_output(payload(source=None), marker(SID)))
    assert not any("Session ID" in line for line in lines)
    assert not any("Source" in line for line in lines)
    assert lines[2:] == ["- **Working directory**: /work/dir",
                         "- **Transcript**: /t/0f0e0d0c.jsonl"]


@pytest.mark.parametrize("source", ["compact", "resume", "clear", "fork"])
def test_an_equal_marker_on_any_other_source_still_prints_the_id(source):
    lines = block_lines(session_info.context_output(payload(source=source), marker(SID)))
    assert f"- **Session ID**: {SID}" in lines


@pytest.mark.parametrize("source", [*SOURCES, None])
@pytest.mark.parametrize("env", [marker(OTHER), marker(SID[:8]), marker(""), CONTEXT_ON],
                         ids=["unequal", "prefix", "empty", "absent"])
def test_an_unequal_or_absent_marker_prints_the_id_on_every_source(source, env):
    lines = block_lines(session_info.context_output(payload(source=source), env))
    assert f"- **Session ID**: {SID}" in lines


@pytest.mark.parametrize("source", [None, ""])
def test_a_block_with_no_field_line_is_not_emitted(pybin, source):
    """Deduped id, no source, no cwd, no transcript: a bare heading is not
    a block, so the hook prints nothing (do-001 review L1)."""
    body = payload(source=source, cwd=None, transcript_path=None)
    assert_silent(run_shim("context", body, marker(SID), pybin))
    assert session_info.context_output(body, marker(SID)) is None


def test_one_field_line_left_is_still_a_block(pybin):
    """The same payload with only its source left still emits, and the Source
    line is the block's one field line."""
    body = payload(cwd=None, transcript_path=None)
    out = emitted(run_shim("context", body, marker(SID), pybin))
    assert out["additionalContext"].splitlines() == [
        session_info.HEADING, "", "- **Source**: startup"]


def test_the_marker_through_the_shim_unequal_prints(pybin):
    out = emitted(run_shim("context", payload(), marker(OTHER), pybin))
    assert f"- **Session ID**: {SID}" in out["additionalContext"].splitlines()


# 4. The switches: default off, each on its own --------------------------------

ON_SPELLINGS = ["on", "ON", "On", "1", "true", "True", "yes", "YES"]
NOT_ON = [None, "", "off", "0", "false", "no", "enabled", "onn"]


@pytest.mark.parametrize("value", ON_SPELLINGS)
def test_each_on_spelling_turns_each_verb_on(pybin, value):
    emitted(run_shim("title", payload(), {session_info.TITLE_ENV: value}, pybin))
    emitted(run_shim("context", payload(), {session_info.CONTEXT_ENV: value}, pybin))


@pytest.mark.parametrize("value", NOT_ON)
def test_unset_or_anything_else_leaves_each_verb_off(pybin, value):
    for verb, name in (("title", session_info.TITLE_ENV),
                       ("context", session_info.CONTEXT_ENV)):
        env = {} if value is None else {name: value}
        assert_silent(run_shim(verb, payload(), env, pybin))


@pytest.mark.parametrize(("value", "on"), [("on", True), ("TRUE", True), ("Yes", True),
                                           ("1", True), ("", False), ("off", False),
                                           ("onn", False), ("2", False)])
def test_switched_on(value, on):
    assert session_info.switched_on({session_info.TITLE_ENV: value},
                                    session_info.TITLE_ENV) is on
    assert session_info.switched_on({}, session_info.TITLE_ENV) is False


def test_each_switch_turns_on_only_its_own_verb(pybin):
    assert_silent(run_shim("context", payload(), dict(TITLE_ON), pybin))
    assert_silent(run_shim("title", payload(), dict(CONTEXT_ON), pybin))


@pytest.mark.parametrize("verb", [None, "", "both", "Title", "--help"])
def test_an_unknown_verb_is_silent(pybin, verb):
    assert_silent(run_shim(verb, payload(), dict(BOTH_ON), pybin))


def marked_python(tmp_path: Path) -> tuple[Path, Path]:
    """A python that logs each start to a mark, then runs the real one."""
    mark = tmp_path / "started"
    py = tmp_path / "marked-python"
    py.write_text(f'#!/bin/sh\necho x >> "{mark}"\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    return py, mark


@pytest.mark.parametrize("verb", ["title", "context"])
def test_switched_off_no_python_starts(tmp_path, verb):
    py, mark = marked_python(tmp_path)
    env = {"SESSIONTOOLS_PYTHON": str(py)}
    assert_silent(run_shim(verb, payload(), {**env, session_info.TITLE_ENV: "off",
                                             session_info.CONTEXT_ENV: "off"}, None))
    assert_silent(run_shim(verb, payload(), dict(env), None))
    assert not mark.exists()
    # On, the same python runs: the mark proves the probe works.
    emitted(run_shim(verb, payload(), {**env, **BOTH_ON}, None))
    assert mark.exists()


def test_the_module_rechecks_the_switch():
    for verb in ("title", "context"):
        out = io.StringIO()
        assert session_info.main([verb], io.StringIO(json.dumps(payload())), out, {}) == 0
        assert out.getvalue() == ""
    assert session_info.title_output(payload(), {}) is None
    assert session_info.context_output(payload(), {}) is None


# 5. Fail silent ---------------------------------------------------------------

@pytest.mark.parametrize("verb", ["title", "context"])
def test_no_python_on_path_is_silent(verb):
    """No python3 and no `session` on PATH: `pick` finds nothing. Not a
    missing SESSIONTOOLS_PYTHON, which falls back to PATH's python3."""
    assert_silent(run_shim(verb, payload(), dict(BOTH_ON), None, system=False))


@pytest.mark.parametrize("verb", ["title", "context"])
@pytest.mark.parametrize("body", ["", "not json", "{", "[]", '"a string"', "42", "null",
                                  "{}", '{"session_id": ""}', '{"session_id": 5}',
                                  '{"cwd": "/x", "source": "startup"}'])
def test_garbage_stdin_is_silent(pybin, verb, body):
    assert_silent(run_shim(verb, body, dict(BOTH_ON), pybin))


@pytest.mark.parametrize("body", ['echo "Traceback: boom" >&2; exit 1',
                                  'echo "bad" >&2; exit 2', "kill -9 $$"])
@pytest.mark.parametrize("verb", ["title", "context"])
def test_a_python_that_crashes_is_silent(tmp_path, body, verb):
    bindir = tmp_path / "broken"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    assert_silent(run_shim(verb, payload(), dict(BOTH_ON), bindir))


def test_a_crash_inside_the_module_is_silent(monkeypatch):
    def boom(*_args):
        raise RuntimeError("boom")
    monkeypatch.setitem(session_info.VERBS, "title", boom)
    out = io.StringIO()
    assert session_info.main(["title"], io.StringIO(json.dumps(payload())), out,
                             TITLE_ON) == 0
    assert out.getvalue() == ""


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
@pytest.mark.parametrize("verb", ["title", "context"])
def test_a_python_older_than_3_11_is_silent(verb):
    assert_silent(run_shim(verb, payload(), dict(BOTH_ON), None))


def test_a_hook_dir_without_the_picker_is_silent(tmp_path, pybin):
    hooks = tmp_path / "plugin" / "hooks"
    hooks.mkdir(parents=True)
    shim = hooks / "session-info"
    shim.write_text(SHIM.read_text())
    shim.chmod(0o755)
    (tmp_path / "plugin" / "src").symlink_to(ROOT / "src")
    assert_silent(run_shim("title", payload(), dict(TITLE_ON), pybin, shim=shim))
    (hooks / "pick-python.sh").write_text(PICKER.read_text())
    emitted(run_shim("title", payload(), dict(TITLE_ON), pybin, shim=shim))


# 6. A subagent's SessionStart does nothing -------------------------------------

@pytest.mark.parametrize("verb", ["title", "context"])
def test_a_payload_with_agent_id_is_silent(pybin, verb):
    body = payload(agent_id="a1b2c3", agent_type="Explore")
    assert_silent(run_shim(verb, body, dict(BOTH_ON), pybin))


@pytest.mark.parametrize("verb", ["title", "context"])
def test_agent_type_alone_is_a_main_session_and_still_emits(pybin, verb):
    """`agent_type` without `agent_id` is a `claude --agent` session."""
    emitted(run_shim(verb, payload(agent_type="worker"), dict(BOTH_ON), pybin))


# 7. The shim and the plugin wiring --------------------------------------------

def test_the_shim_exists_is_executable_posix_sh_and_parses():
    assert SHIM.is_file()
    assert SHIM.stat().st_mode & stat.S_IXUSR
    assert SHIM.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run(["/bin/sh", "-n", str(SHIM)], check=False).returncode == 0


def test_the_shim_sources_the_one_picker_and_never_runs_a_bare_python3():
    text = SHIM.read_text()
    assert '. "$here/pick-python.sh"' in text
    assert "pick() {" not in text and "SESSIONTOOLS_PYTHON:-" not in text
    code = [line for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert not any(re.search(r"(^|[\s;|&(])python3\b", line) for line in code), code


def test_the_variable_names_agree_between_the_shim_and_the_module():
    text = SHIM.read_text()
    assert session_info.TITLE_ENV in text and session_info.CONTEXT_ENV in text


def test_hooks_json_registers_both_verbs_under_session_start():
    spec = json.loads(HOOKS.read_text())
    entries = spec["hooks"]["SessionStart"]
    hooks = [h for entry in entries for h in entry["hooks"]]
    commands = [h["command"] for h in hooks]
    for verb in ("title", "context"):
        want = f'"${{CLAUDE_PLUGIN_ROOT}}/hooks/session-info" {verb}'
        assert commands.count(want) == 1, commands
    assert all(h["type"] == "command" and h["timeout"] == 5 for h in hooks)
    assert all("matcher" not in entry for entry in entries)   # every source


def test_hooks_json_says_what_they_do_and_how_to_switch_them_on():
    description = json.loads(HOOKS.read_text())["description"]
    for word in ("session-info", "SessionStart", f"{session_info.TITLE_ENV}=on",
                 f"{session_info.CONTEXT_ENV}=on", session_info.LAUNCH_INFO_ENV,
                 "DEFAULT OFF", "/clear"):
        assert word in description, word


def test_the_module_imports_only_the_standard_library():
    imports = re.findall(r"^(?:from|import) ([\w.]+)", MODULE.read_text(), re.MULTILINE)
    assert {i.split(".")[0] for i in imports} <= {"__future__", "json", "os", "sys",
                                                  "collections"}, imports


# 8. The identity verb: the block `session start` wrote, as context -------------
#
# ON by default; its switch is the file. The block is planted under a
# sessions root of the test's own (SESSIONS_DIR), and the hook must emit the
# very bytes `session identity` prints.

IDENTITY_TEXT = ("# Who you are (at launch)\n\n- **Session ID**: " + SID + "\n"
                 "- **Pane**: %271\n- **Nickname**: café ✓\n\n"
                 "Seat-mates and call holder change after launch: re-check with "
                 "`session list`.\n")


@pytest.fixture
def sessions(tmp_path: Path) -> Path:
    root = tmp_path / "sessions"
    root.mkdir()
    return root


def plant(root: Path, sid: str = SID, text: str | bytes = IDENTITY_TEXT) -> Path:
    from sessiontools import identity_block
    d = root / sid
    d.mkdir(parents=True, exist_ok=True)
    path = d / identity_block.IDENTITY_FILE
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return path


def roots(root: Path, **extra: str) -> dict:
    return {"SESSIONS_DIR": str(root), **extra}


@pytest.mark.parametrize("source", [*SOURCES, None])
def test_identity_emits_the_block_on_every_source_with_no_switch_set(pybin, sessions, source):
    plant(sessions)
    out = emitted(run_shim("identity", payload(source=source), roots(sessions), pybin))
    assert out == {"hookEventName": "SessionStart", "additionalContext": IDENTITY_TEXT}


def test_identity_emits_the_same_bytes_session_identity_prints(pybin, sessions):
    from sessiontools import identity_block
    plant(sessions)
    verb = io.BytesIO()
    assert identity_block.main([SID], env=roots(sessions), stdout=verb,
                               stderr=io.StringIO()) == 0
    out = emitted(run_shim("identity", payload(), roots(sessions), pybin))
    assert out["additionalContext"].encode("utf-8") == verb.getvalue()


def test_identity_reads_the_payload_session_not_the_env(pybin, sessions):
    plant(sessions, SID, "mine\n")
    plant(sessions, OTHER, "theirs\n")
    env = roots(sessions, CLAUDE_SESSION_ID=OTHER)
    assert emitted(run_shim("identity", payload(), env, pybin))["additionalContext"] == "mine\n"


def test_identity_is_silent_with_no_block(pybin, sessions):
    assert_silent(run_shim("identity", payload(), roots(sessions), pybin))
    (sessions / SID).mkdir()
    assert_silent(run_shim("identity", payload(), roots(sessions), pybin))


def test_identity_after_clear_names_the_new_session_and_is_silent(pybin, sessions):
    plant(sessions, OTHER)          # the launched session's block, not this id's
    assert_silent(run_shim("identity", payload(source="clear"), roots(sessions), pybin))


@pytest.mark.parametrize("value", ["off", "OFF", "Off", "0", "false", "False", "no", "NO"])
def test_identity_off_spellings_switch_it_off(pybin, sessions, value):
    plant(sessions)
    env = roots(sessions, **{session_info.IDENTITY_ENV: value})
    assert_silent(run_shim("identity", payload(), env, pybin))
    assert session_info.identity_output(payload(), env) is None


@pytest.mark.parametrize("value", ["", "on", "1", "yes", "enabled", "offf"])
def test_identity_anything_else_leaves_it_on(pybin, sessions, value):
    plant(sessions)
    env = roots(sessions, **{session_info.IDENTITY_ENV: value})
    emitted(run_shim("identity", payload(), env, pybin))


def test_identity_off_no_python_starts(tmp_path, sessions):
    plant(sessions)
    py, mark = marked_python(tmp_path)
    env = roots(sessions, SESSIONTOOLS_PYTHON=str(py))
    assert_silent(run_shim("identity", payload(), {**env, session_info.IDENTITY_ENV: "off"},
                           None))
    assert not mark.exists()
    emitted(run_shim("identity", payload(), env, None))
    assert mark.exists()


def test_identity_switches_are_its_own(pybin, sessions):
    """The title and context switches neither start nor stop identity."""
    plant(sessions)
    off = {session_info.TITLE_ENV: "off", session_info.CONTEXT_ENV: "off"}
    emitted(run_shim("identity", payload(), roots(sessions, **off), pybin))
    assert_silent(run_shim("context", payload(),
                           roots(sessions, **{session_info.IDENTITY_ENV: "on"}), pybin))


def test_identity_a_subagent_is_silent(pybin, sessions):
    plant(sessions)
    body = payload(agent_id="a1b2c3", agent_type="Explore")
    assert_silent(run_shim("identity", body, roots(sessions), pybin))


@pytest.mark.parametrize("body", ["", "not json", "[]", "null", "{}", '{"session_id": ""}',
                                  '{"session_id": 5}', '{"session_id": "../x"}',
                                  '{"session_id": ".."}'])
def test_identity_garbage_stdin_is_silent(pybin, sessions, body):
    plant(sessions)
    assert_silent(run_shim("identity", body, roots(sessions), pybin))


@pytest.mark.parametrize("data", [b"\xff\xfe not utf-8\n", b"", b"  \n\n",
                                  b"x" * (16 * 1024 + 1)])
def test_identity_a_block_it_cannot_use_is_silent(pybin, sessions, data):
    plant(sessions, text=data)
    assert_silent(run_shim("identity", payload(), roots(sessions), pybin))


def test_identity_an_unreadable_block_is_silent(pybin, sessions):
    path = plant(sessions)
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("this user reads a 0000 file")
        assert_silent(run_shim("identity", payload(), roots(sessions), pybin))
    finally:
        path.chmod(0o600)


def test_identity_no_python_or_a_crashing_one_is_silent(tmp_path, sessions):
    plant(sessions)
    assert_silent(run_shim("identity", payload(), roots(sessions), None, system=False))
    bindir = tmp_path / "broken"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text('#!/bin/sh\necho "Traceback: boom" >&2; exit 1\n')
    stub.chmod(0o755)
    assert_silent(run_shim("identity", payload(), roots(sessions), bindir))


def test_identity_a_crash_inside_the_module_is_silent(monkeypatch):
    def boom(*_args):
        raise RuntimeError("boom")
    monkeypatch.setitem(session_info.VERBS, "identity", boom)
    out = io.StringIO()
    assert session_info.main(["identity"], io.StringIO(json.dumps(payload())), out, {}) == 0
    assert out.getvalue() == ""


def test_identity_the_module_rechecks_the_switch(sessions):
    plant(sessions)
    env = roots(sessions, **{session_info.IDENTITY_ENV: "off"})
    out = io.StringIO()
    assert session_info.main(["identity"], io.StringIO(json.dumps(payload())), out, env) == 0
    assert out.getvalue() == ""
    on = io.StringIO()
    session_info.main(["identity"], io.StringIO(json.dumps(payload())), on, roots(sessions))
    assert json.loads(on.getvalue())["hookSpecificOutput"]["additionalContext"] == IDENTITY_TEXT


def test_identity_is_registered_and_described():
    spec = json.loads(HOOKS.read_text())
    commands = [h["command"] for entry in spec["hooks"]["SessionStart"]
                for h in entry["hooks"]]
    assert commands.count('"${CLAUDE_PLUGIN_ROOT}/hooks/session-info" identity') == 1
    description = spec["description"]
    assert session_info.IDENTITY_ENV in description and "identity.md" in description
    assert session_info.IDENTITY_ENV in SHIM.read_text()
