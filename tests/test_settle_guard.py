"""The compaction guard (ST-78 do-006): `hooks/settle-hook precompact`.

A PreCompact hook, the belt under `idleCompaction: false`: it skips (exit 2,
measured in do-001 on the threshold path) an AUTO compaction when the count
is BELOW the SET compaction point; a manual compaction always passes; settle
off or an unknown point is silent and a no-op.

The criteria are tested in process (`settle_guard.decide` / `main`) and
THROUGH THE REAL SHIM, as `hooks/hooks.json` runs it, with python starts
counted. Every tree is under a scratch HOME and SESSIONS_DIR: nothing reads
this machine's settings or this session's own launch.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from settings_support import host as host_dir

from sessiontools import settle_guard as G
from sessiontools import settle_settings as SS
from sessiontools.start.record import render_command

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "settle-hook"
HOOKS = ROOT / "hooks" / "hooks.json"
MODULE = ROOT / "src" / "sessiontools" / "settle_guard.py"
SID = "5a5a5a5a-1b1b-4c4c-8d8d-9e9e9e9e9e9e"

#: autoCompactWindow 100000 in the launch's --settings door: the point is
#: 100,000 - 20,000 - 13,000 (design R5; do-001 a1's own probe window).
WINDOW = 100_000
POINT = 67_000
ON = {"settle": {"soft": {"at": 50_000}}}


# --- the tree --------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path) -> Path:
    (tmp_path / ".session" / "sessions" / SID).mkdir(parents=True)
    return tmp_path


def session_dir(tree: Path) -> Path:
    return tree / ".session" / "sessions" / SID


def env_of(tree: Path, **more: str) -> dict:
    return {"HOME": str(tree), "SESSIONS_DIR": str(tree / ".session" / "sessions"), **more}


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def settle_on(tree: Path, data=None) -> Path:
    """The host layer, `settle` ON (or DATA)."""
    return w(host_dir(tree, "session") / "session.json", ON if data is None else data)


def launched(tree: Path, door: dict | None = None) -> None:
    """The launch record: `claude --settings <door>`, the door setting the
    window (do-003's render), so the point is SET and known."""
    launch = session_dir(tree) / "launch"
    door_file = w(launch / "session-start-settings-abcd1234.json",
                  {"autoCompactWindow": WINDOW} if door is None else door)
    vendor = ["claude", "--session-id", SID, "--settings", str(door_file)]
    w(launch / "command", render_command(vendor, "session start x", str(tree)))


def usage_row(tokens: int) -> str:
    return json.dumps({"type": "assistant", "message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": tokens,
                                              "cache_creation_input_tokens": 0,
                                              "cache_read_input_tokens": 0}}})


def transcript(tree: Path, *rows: str) -> Path:
    return w(tree / "transcripts" / f"{SID}.jsonl", "".join(r + "\n" for r in rows))


def payload(path: Path, trigger: str | None = "auto", **more) -> dict:
    body = {"session_id": SID, "transcript_path": str(path), "cwd": "/nowhere",
            "hook_event_name": "PreCompact", "custom_instructions": None, **more}
    if trigger is not None:
        body["trigger"] = trigger
    return body


@pytest.fixture
def armed(tree) -> Path:
    """Settle ON, the point SET (67,000); returns the tree."""
    settle_on(tree)
    launched(tree)
    return tree


def run(tree: Path, body, **env_more: str) -> tuple[int, str]:
    """`settle_guard.main` in process: (exit code, stdout)."""
    out = io.StringIO()
    code = G.main(io.StringIO(json.dumps(body)), out, env_of(tree, **env_more))
    return code, out.getvalue()


# --- criterion 1: auto below -> 2; auto at/above -> 0; manual -> 0 ----------------

def test_an_auto_compaction_below_the_set_point_is_skipped(armed):
    code, out = run(armed, payload(transcript(armed, usage_row(POINT - 1))))
    assert code == G.SKIP == 2
    assert out == "settle: auto compaction skipped at 66K, below the set compaction point 67K\n"


@pytest.mark.parametrize("used", [POINT, POINT + 1, 900_000])
def test_an_auto_compaction_at_or_above_the_set_point_passes_silently(armed, used):
    assert run(armed, payload(transcript(armed, usage_row(used)))) == (0, "")


@pytest.mark.parametrize("trigger", ["manual", None, "", "AUTO", "idle"])
def test_any_trigger_but_auto_passes_silently_however_low_the_count(armed, trigger):
    """A manual compaction (/compact) always passes: someone asked for it.
    A missing or unknown trigger is not auto either."""
    assert run(armed, payload(transcript(armed, usage_row(1_000)), trigger)) == (0, "")


def test_the_count_is_the_last_usage_row_not_the_peak(armed):
    path = transcript(armed, usage_row(90_000), usage_row(20_000))
    assert run(armed, payload(path))[0] == 2


# --- a threshold compaction mid-turn: the last usage row under-counts it -----------

def prompt_row(chars: int = 46_052) -> str:
    """The user's next prompt, after the last usage row (do-001's probe: a
    46k-character prompt, so Claude Code counted 74,461 against 62,924)."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": "x" * chars}})


def tool_result_row() -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "big output"}]}})


def test_do_001s_live_threshold_compaction_passes(armed):
    """do-001 (b), replayed: point 67,000, last usage row 62,924, then the
    new prompt. Claude Code compacted at preTokens 74,461 (over the point),
    so the guard must not skip it, though the usage row alone is below."""
    path = transcript(armed, usage_row(62_924), prompt_row())
    assert run(armed, payload(path)) == (0, "")


def test_tool_results_after_the_last_usage_row_pass(armed):
    path = transcript(armed, usage_row(10_000), tool_result_row())
    assert run(armed, payload(path)) == (0, "")


def test_rows_that_are_not_messages_after_the_last_usage_row_still_skip(armed):
    """An idle seat's tail: attachments, queue, mode and system rows are not
    messages in the count, so the usage row is still the whole context."""
    tail = [json.dumps({"type": t, "message": {"content": "x"}} if t == "attachment"
                       else {"type": t, "subtype": "stop_hook_summary"})
            for t in ("attachment", "queue-operation", "mode", "system", "last-prompt")]
    path = transcript(armed, usage_row(10_000), *tail)
    assert run(armed, payload(path))[0] == 2


def test_a_usage_row_after_the_prompt_makes_the_count_whole_again(armed):
    path = transcript(armed, usage_row(5_000), prompt_row(), usage_row(20_000))
    assert run(armed, payload(path))[0] == 2


def test_whole(tmp_path):
    def t(*rows):
        return w(tmp_path / "t.jsonl", "".join(r + "\n" for r in rows))
    assert G.whole(t(usage_row(1)))
    assert G.whole(t())
    assert not G.whole(t(usage_row(1), prompt_row(10)))
    assert not G.whole(t(prompt_row(10)))
    assert G.whole(t(usage_row(1), json.dumps({"type": "user", "message": {"content": ""}})))
    assert G.whole(t(usage_row(1), "{half-written"))
    bad = json.dumps({"type": "assistant", "message": {"content": "x", "usage": {
        "input_tokens": "a", "cache_creation_input_tokens": 1}}})
    assert not G.whole(t(usage_row(1), bad))          # scan skips it: not a usage row


# --- criterion 1: point unknown or settle off -> 0, silent ------------------------

def test_no_launch_record_is_an_unknown_point_and_passes(tree):
    settle_on(tree)
    assert run(tree, payload(transcript(tree, usage_row(1_000)))) == (0, "")


@pytest.mark.parametrize("door", [{}, {"autoCompactWindow": "auto"}], ids=["unset", "auto"])
def test_a_door_that_sets_no_window_is_an_unknown_point_and_passes(tree, door):
    settle_on(tree)
    launched(tree, door)
    assert run(tree, payload(transcript(tree, usage_row(1_000)))) == (0, "")


@pytest.mark.parametrize("off", ["DISABLE_COMPACT", "DISABLE_AUTO_COMPACT"])
def test_compaction_switched_off_has_no_point_and_passes(armed, off):
    path = transcript(armed, usage_row(1_000))
    assert run(armed, payload(path), **{off: "1"}) == (0, "")


def test_the_point_is_the_set_one_the_variable_outranking_the_door(armed):
    """CLAUDE_CODE_AUTO_COMPACT_WINDOW outranks --settings, as in Claude
    Code (compact_point): 400k sets a point of 367k, so 100k is below it."""
    path = transcript(armed, usage_row(100_000))
    assert run(armed, payload(path))[0] == 0                  # 100k >= 67k
    code, out = run(armed, payload(path), CLAUDE_CODE_AUTO_COMPACT_WINDOW="400000")
    assert code == 2 and "367K" in out


def test_settle_unset_passes_and_stamps_off(tree):
    launched(tree)
    assert run(tree, payload(transcript(tree, usage_row(1_000)))) == (0, "")
    assert (session_dir(tree) / SS.ROOT / SS.OFF).is_file()   # the alert's OFF stamp


def test_settle_null_passes(tree):
    settle_on(tree, ON)
    launched(tree)
    w(session_dir(tree) / "settings" / "session.d" / "90-off.json", {"settle": None})
    assert run(tree, payload(transcript(tree, usage_row(1_000)))) == (0, "")


def test_an_unreadable_settings_layer_passes(tree):
    w(host_dir(tree, "session") / "session.json", "{not json")
    launched(tree)
    assert run(tree, payload(transcript(tree, usage_row(1_000)))) == (0, "")


def test_settle_turned_on_in_the_session_layer_is_heard_live(tree):
    """Read live through the cascade: the same tree passes while settle is
    unset, then skips once a layer sets it, with no restart."""
    launched(tree)
    path = transcript(tree, usage_row(1_000))
    assert run(tree, payload(path))[0] == 0
    w(session_dir(tree) / "settings" / "session.json", ON)
    assert run(tree, payload(path))[0] == 2


# --- unknown counts and other payloads pass ----------------------------------------

def test_a_transcript_with_no_usage_row_is_no_reading_and_passes(armed):
    path = transcript(armed, json.dumps({"type": "user", "message": {"content": "hi"}}))
    assert run(armed, payload(path)) == (0, "")


def test_a_synthetic_zero_tail_after_a_climb_is_no_reading_and_passes(armed):
    synthetic = json.dumps({"type": "assistant", "message": {
        "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0}}})
    path = transcript(armed, usage_row(40_000), synthetic)
    assert run(armed, payload(path)) == (0, "")


def test_a_missing_transcript_passes(armed):
    body = payload(armed / "transcripts" / "gone.jsonl")
    assert run(armed, body) == (0, "")


def test_a_subagent_payload_passes(armed):
    path = transcript(armed, usage_row(1_000))
    assert run(armed, payload(path, agent_id="a1b2c3"))[0] == 0
    assert run(armed, payload(path, agent_type="worker"))[0] == 2  # main thread, --agent


@pytest.mark.parametrize("raw", ["", "null", "[]", "{", "\"auto\""])
def test_a_payload_that_is_not_an_object_passes(armed, raw):
    out = io.StringIO()
    assert G.main(io.StringIO(raw), out, env_of(armed)) == 0 and out.getvalue() == ""


def test_no_session_id_passes(armed):
    body = payload(transcript(armed, usage_row(1_000)))
    del body["session_id"]
    assert run(armed, body) == (0, "")


def test_any_error_passes(armed, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(G.window, "scan", boom)
    assert run(armed, payload(transcript(armed, usage_row(1_000)))) == (0, "")


def test_reading():
    assert G.reading(G.window.Usage(used=10, peak=10, usage_rows=1))
    assert G.reading(G.window.Usage(used=0, peak=0, usage_rows=1))     # genuinely ~0
    assert not G.reading(G.window.Usage(used=0, peak=0, usage_rows=0))
    assert not G.reading(G.window.Usage(used=0, peak=5, usage_rows=2))


# --- through the real shim, as hooks.json runs it ---------------------------------

def marked_python(tmp_path: Path, real: str = sys.executable) -> tuple[Path, Path]:
    """A `python` that logs each start to MARK, then runs REAL."""
    mark = tmp_path / "python-started"
    py = w(tmp_path / "marked" / "python", f'#!/bin/sh\necho x >> "{mark}"\nexec "{real}" "$@"\n')
    py.chmod(0o755)
    return py, mark


def starts(mark: Path) -> int:
    try:
        return len(mark.read_text().split())
    except FileNotFoundError:
        return 0


def shim(tree: Path, body, py: Path, *args: str, **env_more: str) -> subprocess.CompletedProcess:
    env = {**env_of(tree), "PATH": "/usr/bin:/bin", "SESSIONTOOLS_PYTHON": str(py),
           "CLAUDE_CODE_SESSION_ID": SID, **env_more}
    return subprocess.run(["/bin/sh", str(SHIM), *(args or ("precompact",))],
                          input=json.dumps(body), env=env, capture_output=True,
                          text=True, timeout=30, check=False)


def test_the_shim_skips_below_with_the_reason_on_stderr_and_nothing_on_stdout(armed):
    py, _ = marked_python(armed)
    result = shim(armed, payload(transcript(armed, usage_row(10_000))), py)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "settle: auto compaction skipped at 10K, below the set compaction point 67K\n"


@pytest.mark.parametrize(("used", "trigger"), [(POINT, "auto"), (500_000, "auto"),
                                               (10_000, "manual")])
def test_the_shim_passes_at_or_above_and_on_manual_silently(armed, used, trigger):
    py, _ = marked_python(armed)
    result = shim(armed, payload(transcript(armed, usage_row(used)), trigger), py)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_shim_passes_a_threshold_compaction_mid_turn_silently(armed):
    py, _ = marked_python(armed)
    result = shim(armed, payload(transcript(armed, usage_row(62_924), prompt_row())), py)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_shim_passes_an_unknown_point_silently(tree):
    settle_on(tree)
    py, _ = marked_python(tree)
    result = shim(tree, payload(transcript(tree, usage_row(10_000))), py)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def age(tree: Path, seconds: float = 30) -> None:
    then = time.time() - seconds
    for dirpath, dirs, files in os.walk(tree):
        for name in dirs + files:
            os.utime(Path(dirpath, name), (then, then), follow_symlinks=False)


def test_settle_off_through_the_shim_is_silent_and_then_starts_no_python(tree):
    """Settle unset: the first call resolves OFF and stamps; the next starts
    no python at all (the alert's own OFF stamp, design R1)."""
    launched(tree)
    py, mark = marked_python(tree)
    body = payload(transcript(tree, usage_row(10_000)))
    age(tree)
    first = shim(tree, body, py)
    assert (first.returncode, first.stdout, first.stderr) == (0, "", "")
    assert starts(mark) == 1
    second = shim(tree, body, py)
    assert (second.returncode, second.stdout, second.stderr) == (0, "", "")
    assert starts(mark) == 1


def test_a_python_that_exits_2_without_the_reason_never_skips(armed):
    """Python 2 exits 2 on `-I` (its usage on stderr): that must pass."""
    fake = w(armed / "py2" / "python", "#!/bin/sh\necho 'Unknown option: -I' >&2\nexit 2\n")
    fake.chmod(0o755)
    result = shim(armed, payload(transcript(armed, usage_row(10_000))), fake)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize("body", ["#!/bin/sh\nexit 1\n", "#!/bin/sh\nkill -9 $$\n",
                                  "#!/bin/sh\necho 'settle: x'\nexit 1\n"],
                         ids=["crash", "killed", "reason-but-1"])
def test_a_failing_python_never_skips(armed, body):
    fake = w(armed / "bad" / "python", body)
    fake.chmod(0o755)
    result = shim(armed, payload(transcript(armed, usage_row(10_000))), fake)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_an_unknown_verb_exits_0_without_python(armed):
    py, mark = marked_python(armed)
    result = shim(armed, payload(transcript(armed, usage_row(10_000))), py, "compact")
    assert (result.returncode, result.stdout, result.stderr, starts(mark)) == (0, "", "", 0)


def test_the_batch_entry_is_unchanged_by_the_verb(armed):
    """No argument is still the settle alert: it speaks on stdout, exit 0."""
    py, _ = marked_python(armed)
    body = {"session_id": SID, "transcript_path": str(transcript(armed, usage_row(60_000))),
            "hook_event_name": "PostToolBatch"}
    env = {**env_of(armed), "PATH": "/usr/bin:/bin", "SESSIONTOOLS_PYTHON": str(py),
           "CLAUDE_CODE_SESSION_ID": SID}
    result = subprocess.run(["/bin/sh", str(SHIM)], input=json.dumps(body), env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    assert (result.returncode, result.stderr) == (0, "")
    said = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "60K" in said


# --- criterion 2: hooks.json registers it and says what it does -------------------

def test_hooks_json_registers_the_guard_on_auto_precompact():
    spec = json.loads(HOOKS.read_text())["hooks"]["PreCompact"]
    assert spec == [{"matcher": "auto", "hooks": [{
        "type": "command",
        "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/settle-hook" precompact',
        "timeout": 5}]}]


def test_hooks_json_says_what_the_guard_does():
    description = json.loads(HOOKS.read_text())["description"]
    for word in ("PreCompact", "settle-hook precompact", "idleCompaction: false",
                 "trigger: auto", "BELOW", "whole context", "exit 2", "MANUAL", "settle: null",
                 "unknown point", "UNMEASURED", "sessiontools.settle_guard"):
        assert word in description, word


def test_the_guard_says_nothing_about_who_the_session_is():
    """As the alert (test_settle): any session whose settings set it."""
    import re
    forbidden = re.compile(r"TM_AGENT_ADDRESS|\b(?:role|seat|crew)s?\b", re.IGNORECASE)
    assert [line for line in MODULE.read_text().splitlines() if forbidden.search(line)] == []
