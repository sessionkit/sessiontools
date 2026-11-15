"""Cheap when off, cheap when on (ST-78 do-005; design.md R1, R4).

OFF: `<session dir>/settle/off`, as the busy sound's `sounds/busy/off`. A
clean OFF resolve writes it, back-dated 1 s, listing every layer path the
cascade read or looked for; ON removes it; `hooks/settle-hook` exits 0 by
builtins while it is there and no listed path is newer; `session start`'s
birth writes it; no session dir exits 0. The shim tests count python STARTS
with a python that logs each one, so "no python" is measured, not assumed.

ON: the agent layer's section is cached under `<session dir>/settle/`,
keyed on the agent file's stat, so a batch whose agent file is unchanged
does not import the agent parser. Tested across processes, because an
import is cached inside one.

Every test builds its own tree under a private HOME and SESSIONS_DIR, so
nothing reads the live machine's settings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from settings_support import host as host_dir

from sessiontools import settings
from sessiontools import settle_settings as SS

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "settle-hook"
BUSY = ROOT / "hooks" / "busy-sound"
SESSION_DIR_SH = ROOT / "hooks" / "session-dir.sh"
SID = "0f0e0d0c-0b0a-4908-8706-050403020100"
AGENT = "alice"
SEAT = "desk-1"
PARSER = "sessiontools.start.agentfile"


# --- the tree --------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path) -> Path:
    (tmp_path / ".session" / "sessions" / SID).mkdir(parents=True)
    return tmp_path


def env_of(tree: Path, **more: str) -> dict:
    return {"HOME": str(tree), "SESSIONS_DIR": str(tree / ".session" / "sessions"), **more}


def session_dir(tree: Path) -> Path:
    return tree / ".session" / "sessions" / SID


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def host(tree: Path, data) -> Path:
    return w(host_dir(tree, "session") / "session.json", data)


def agent_md(tree: Path, settle_yaml: str | None = None) -> Path:
    """The agent's file; SETTLE_YAML under `settings: session: settle:`, or
    no settle key at all (an unrelated `session` setting) when None."""
    if settle_yaml is None:
        block = "    other: 1\n"
    else:
        block = "    settle:\n" + "".join(f"      {x}\n" for x in settle_yaml.strip().splitlines())
    return w(tree / "agents" / f"{AGENT}.md",
             f"---\nname: {AGENT}\nsettings:\n  session:\n{block}---\n# {AGENT}\n")


def record(tree: Path, **fields) -> Path:
    """The birth record, `<sid>/settings/session.json`."""
    return w(session_dir(tree) / "settings" / "session.json", fields)


def age(tree: Path, seconds: float = 30) -> None:
    """Every file and folder under TREE made SECONDS old: a tree a test just
    built is not "newer" than a stamp written a moment later (the stamp is
    back-dated 1 s, so a fresh write would read newer than it)."""
    then = time.time() - seconds
    for dirpath, dirs, files in os.walk(tree):
        for name in dirs + files:
            os.utime(Path(dirpath, name), (then, then), follow_symlinks=False)


def touch_now(path: Path) -> None:
    """PATH's mtime set a few seconds ahead: newer than any stamp a test made."""
    ahead = time.time() + 5
    os.utime(path, (ahead, ahead))


def stamp(tree: Path) -> Path:
    return session_dir(tree) / SS.ROOT / SS.OFF


def listed(tree: Path) -> list[str]:
    return stamp(tree).read_text().splitlines()


# --- the shim, with python starts counted ----------------------------------------

def marked_python(tmp_path: Path) -> tuple[Path, Path]:
    """A `python` that logs each start to MARK, then runs the real one."""
    mark = tmp_path / "python-started"
    py = w(tmp_path / "marked" / "python",
           f'#!/bin/sh\necho x >> "{mark}"\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    return py, mark


def starts(mark: Path) -> int:
    try:
        return len(mark.read_text().split())
    except FileNotFoundError:
        return 0


def usage_row(tokens: int) -> str:
    return json.dumps({"type": "assistant", "message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": tokens,
                                              "cache_creation_input_tokens": 0,
                                              "cache_read_input_tokens": 0}}})


def shim(tree: Path, py: Path, used: int = 300_000, **env_more: str) -> str | None:
    """One batch through the real shim, as Claude Code runs it; the line said,
    or None. The transcript lives outside the settings tree."""
    path = tree / "transcripts" / f"{SID}.jsonl"
    path.parent.mkdir(exist_ok=True)
    with path.open("a") as f:
        f.write(usage_row(used) + "\n")
    body = {"session_id": SID, "transcript_path": str(path), "hook_event_name": "PostToolBatch"}
    env = {**env_of(tree), "PATH": "/usr/bin:/bin", "SESSIONTOOLS_PYTHON": str(py),
           "CLAUDE_CODE_SESSION_ID": SID, **env_more}
    result = subprocess.run(["/bin/sh", str(SHIM)], input=json.dumps(body), env=env,
                            capture_output=True, text=True, timeout=30, check=False)
    assert (result.returncode, result.stderr) == (0, "")
    if not result.stdout:
        return None
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


# --- criterion 1: no python while the stamp is fresh; python when a listed file is newer

def test_a_fresh_stamp_starts_no_python_and_a_newer_listed_file_starts_it(tree):
    py, mark = marked_python(tree)
    host(tree, {"other": 1})                      # a host file, no settle key: OFF
    age(tree)
    assert shim(tree, py) is None
    assert starts(mark) == 1                      # the first batch resolves, and stamps
    assert stamp(tree).is_file()
    for _ in range(3):
        assert shim(tree, py) is None
    assert starts(mark) == 1                      # fresh: builtins only, no python

    touch_now(host_dir(tree, "session") / "session.json")   # a listed file is newer
    assert shim(tree, py) is None
    assert starts(mark) == 2                      # python ran, found it still OFF, re-stamped
    age(host_dir(tree, "session"))                # time passes: the edit is older than
    assert shim(tree, py) is None                 # the new stamp, as a real edit becomes
    assert starts(mark) == 2                      # a second after it

    host(tree, {"settle": {"soft": {"at": "250k", "say": "soft {at}"}}})   # now ON
    touch_now(host_dir(tree, "session") / "session.json")
    assert shim(tree, py) == "soft 250K"
    assert starts(mark) == 3
    assert not stamp(tree).exists()               # ON removes the stamp
    assert shim(tree, py) == "soft 250K"
    assert starts(mark) == 4                      # ON: python every batch


def test_a_layer_made_after_the_stamp_is_seen(tree):
    """A path looked for and absent is listed too, so creating it counts."""
    py, mark = marked_python(tree)
    age(tree)
    assert shim(tree, py) is None and starts(mark) == 1
    assert str(host_dir(tree, "session") / "session.json") in listed(tree)
    assert shim(tree, py) is None and starts(mark) == 1
    drop = w(session_dir(tree) / "settings" / "session.d" / "10-on.json",
             {"settle": {"hard": {"at": "1k", "say": "hard {used}"}}})
    touch_now(drop.parent)
    assert shim(tree, py, used=5_000) == "hard 5K"
    assert starts(mark) == 2


def test_an_edit_in_place_inside_a_drop_in_folder_is_seen(tree):
    """Editing a `.d` file does not change its folder's mtime, so each file
    in a `.d` folder is listed itself."""
    py, mark = marked_python(tree)
    drop = w(session_dir(tree) / "settings" / "session.d" / "10-x.json", {"settle": None})
    age(tree)
    assert shim(tree, py) is None and starts(mark) == 1
    assert str(drop) in listed(tree)
    w(drop, {"settle": {"soft": {"at": "1k", "say": "on"}}})
    touch_now(drop)
    assert shim(tree, py, used=2_000) == "on"
    assert starts(mark) == 2


def test_the_agent_file_is_listed_and_an_agent_layer_turning_it_on_is_heard(tree):
    """Reviewer 3f09156a, F1: no SESSION_SETTLE_AT anywhere, only the agent
    layer sets settle.soft.at, and the hook speaks, through the real shim,
    after a stamp written while the agent file said nothing."""
    pytest.importorskip("yaml")
    py, mark = marked_python(tree)
    md = agent_md(tree)                           # a `session` section, no settle
    record(tree, agent=AGENT, agent_file=str(md))
    age(tree)
    assert shim(tree, py) is None and starts(mark) == 1
    paths = listed(tree)
    assert str(md) in paths
    config = settings.agent_root(env_of(tree)) / "config"
    assert str(config / AGENT / f"{AGENT}.md") in paths and str(config / f"{AGENT}.md") in paths
    assert shim(tree, py) is None and starts(mark) == 1
    agent_md(tree, "soft:\n  at: 250k\n  say: agent {at}")
    touch_now(md)
    assert shim(tree, py) == "agent 250K"
    assert starts(mark) == 2


def test_a_seat_layer_alone_turning_it_on_is_heard_through_the_shim(tree):
    """Reviewer 3f09156a, F1, the seat half: only a seat file sets it."""
    py, mark = marked_python(tree)
    record(tree, seat=SEAT)
    age(tree)
    assert shim(tree, py) is None and starts(mark) == 1
    seat_file = settings.seat_dir(SEAT, env_of(tree)) / "settings" / "session.json"
    assert str(seat_file) in listed(tree)
    w(seat_file, {"settle": {"soft": {"at": "250k", "say": "seat {at}"}}})
    assert shim(tree, py) == "seat 250K"          # made after the stamp: newer
    assert starts(mark) == 2


def test_a_record_naming_a_seat_later_is_seen(tree):
    """The record's own file is listed, so naming a seat re-resolves."""
    py, mark = marked_python(tree)
    w(settings.seat_dir(SEAT, env_of(tree)) / "settings" / "session.json",
      {"settle": {"soft": {"at": "1k", "say": "seat"}}})
    age(tree)
    assert shim(tree, py, used=2_000) is None and starts(mark) == 1    # no seat named: OFF
    rec = record(tree, seat=SEAT)
    touch_now(rec)
    assert shim(tree, py, used=2_000) == "seat"
    assert starts(mark) == 2


@pytest.mark.parametrize("why", ["no id", "not a bare name", "no session dir"])
def test_no_session_dir_exits_0_without_python(tree, why):
    py, mark = marked_python(tree)
    host(tree, {"settle": {"soft": {"at": "1k"}}})           # ON at the host
    env = {"no id": {"CLAUDE_CODE_SESSION_ID": ""},
           "not a bare name": {"CLAUDE_CODE_SESSION_ID": "../" + SID},
           "no session dir": {"CLAUDE_CODE_SESSION_ID": "ffffffff-0000-4000-8000-000000000000"}}
    assert shim(tree, py, used=2_000, **env[why]) is None
    assert starts(mark) == 0


def test_a_stamp_whose_list_has_no_final_newline_still_reads_every_path(tree):
    """`read` returns false on a last line with no newline; the loop still
    tests it."""
    py, mark = marked_python(tree)
    target = w(tree / "watched.json", "{}")
    age(tree)
    off = stamp(tree)
    off.parent.mkdir(parents=True)
    off.write_text(f"/nonexistent/a\n{target}")              # no final newline
    then = time.time() - 10
    os.utime(off, (then, then))
    touch_now(target)
    assert shim(tree, py) is None
    assert starts(mark) == 1


def test_the_hooks_find_the_session_dir_through_one_shared_copy():
    text = SESSION_DIR_SH.read_text()
    assert "leaf() {" in text and "session_dir() {" in text
    assert not os.access(SESSION_DIR_SH, os.X_OK), "a sourced fragment, not a hook"
    for hook in (SHIM, BUSY):
        body = hook.read_text()
        assert '. "$here/session-dir.sh"' in body, hook.name
        assert "leaf() {" not in body, hook.name


def test_the_shim_tests_no_settle_variable():
    """R7: the shim's variable test became the OFF stamp."""
    code = [x for x in SHIM.read_text().splitlines() if not x.lstrip().startswith("#")]
    assert not [x for x in code if "SETTLE" in x]


# --- the stamp itself, in process ----------------------------------------------

def test_a_clean_off_stamps_back_dated_and_lists_every_layer_path(tree):
    env = env_of(tree)
    md = agent_md(tree)
    record(tree, agent=AGENT, agent_file=str(md), seat=SEAT)
    w(session_dir(tree) / "settings" / "session.d" / "20-x.json", {})
    before = time.time()
    assert SS.resolve(SID, env) is None
    off = stamp(tree)
    assert off.stat().st_mtime <= before                         # back-dated 1 s
    assert off.stat().st_mtime >= before - 2
    hosts = host_dir(tree, "session")
    seat = settings.seat_dir(SEAT, env) / "settings"
    sess = session_dir(tree) / "settings"
    expected = [hosts / "session.template.json", hosts / "session.json", hosts / "session.d",
                md, settings.agent_root(env) / "config" / AGENT / f"{AGENT}.md",
                settings.agent_root(env) / "config" / f"{AGENT}.md",
                seat / "session.json", seat / "session.d",
                sess / "session.json", sess / "session.d", sess / "session.d" / "20-x.json"]
    assert listed(tree) == [str(p) for p in expected]


def test_every_file_the_cascade_reads_is_listed(tree):
    """`watched` against `settings.sources`: no source the cascade reads is
    left out of the stamp."""
    pytest.importorskip("yaml")
    env = env_of(tree)
    hosts = host_dir(tree, "session")
    w(hosts / "session.template.json", {})
    w(hosts / "session.json", {})
    w(hosts / "session.d" / "10-a.json", {})
    md = agent_md(tree)
    record(tree, agent=AGENT, agent_file=str(md), seat=SEAT)
    w(settings.seat_dir(SEAT, env) / "settings" / "session.d" / "10-s.json", {})
    w(session_dir(tree) / "settings" / "session.d" / "10-t.json", {})
    read = {str(s.path) for s in settings.sources("session", SID, AGENT, SEAT, env)}
    assert len(read) == 7
    assert read <= {str(p) for p in SS.watched(SID, env)}


def test_on_removes_the_stamp_and_an_unreadable_layer_writes_none(tree):
    env = env_of(tree)
    assert SS.resolve(SID, env) is None and stamp(tree).exists()
    host(tree, {"settle": {"soft": {"at": "1k"}}})
    assert SS.resolve(SID, env) is not None
    assert not stamp(tree).exists()
    host(tree, "{not json")
    assert SS.resolve(SID, env) is None
    assert not stamp(tree).exists()                              # a repair is seen at once


def test_no_session_dir_writes_no_stamp(tree):
    env = env_of(tree)
    other = "ffffffff-0000-4000-8000-000000000000"
    assert SS.resolve(other, env) is None
    assert not (tree / ".session" / "sessions" / other).exists()


def test_a_path_with_a_line_break_writes_no_stamp(tmp_path):
    settle_dir = tmp_path / "settle"
    assert SS.stamp_off(settle_dir, [tmp_path / "a", tmp_path / "b\nc"], time.time()) is False
    assert not (settle_dir / SS.OFF).exists()


def test_a_subagent_batch_stamps_nothing(tree):
    """The hook returns before it reads the cascade on a subagent's batch, so
    it must not stamp: it has not resolved anything."""
    import io

    from sessiontools import settle
    body = {"session_id": SID, "agent_id": "a1", "hook_event_name": "PostToolBatch"}
    assert settle.main(io.StringIO(json.dumps(body)), io.StringIO(), env_of(tree)) == 0
    assert not stamp(tree).exists()


# --- the birth writes it -----------------------------------------------------------

def test_the_birth_stamps_a_session_with_no_settle(tree):
    env = env_of(tree)
    assert settings.birth_main(["--dir", str(session_dir(tree))], environ=env) == 0
    assert stamp(tree).is_file()


def test_the_birth_writes_no_stamp_when_its_copy_turns_the_warning_on(tree):
    env = env_of(tree)
    settings.install_own_schemas(session_dir(tree), env)
    assert settings.birth_main(["--dir", str(session_dir(tree))],
                               environ={**env, SS.AT_ENV: "250k"}) == 0
    assert (session_dir(tree) / "settings" / "session.d" / settings.ENV_DROP_IN).is_file()
    assert not stamp(tree).exists()


def test_the_birth_leaves_a_dir_the_hook_would_not_find_alone(tree):
    """A --dir that is not settings.session_dir(<its name>): no stamp there."""
    elsewhere = tree / "elsewhere" / SID
    elsewhere.mkdir(parents=True)
    assert settings.birth_main(["--dir", str(elsewhere)], environ=env_of(tree)) == 0
    assert not (elsewhere / SS.ROOT / SS.OFF).exists()
    assert not stamp(tree).exists()


def test_a_born_seat_starts_no_python_at_its_first_batch(tree):
    """End to end: birth, then the shim, with no layer set: zero starts."""
    py, mark = marked_python(tree)
    env = env_of(tree)
    record(tree, seat=SEAT)
    age(tree)
    settings.birth_main(["--dir", str(session_dir(tree))], environ=env)
    assert shim(tree, py) is None
    assert starts(mark) == 0


# --- ON: the agent layer's cache -----------------------------------------------------

PROBE = """import json, sys
sys.path.insert(0, sys.argv[1])
from sessiontools import settle_settings
found = settle_settings.read(sys.argv[2], json.loads(sys.argv[3]))
print(json.dumps({"soft": found.soft.say if found and found.soft else None,
                  "parser": sys.argv[4] in sys.modules}))
"""


def probe(tree: Path) -> dict:
    """One fresh process's read: the soft words, and whether it imported
    the agent parser."""
    result = subprocess.run([sys.executable, "-I", "-c", PROBE, str(ROOT / "src"), SID,
                             json.dumps(env_of(tree)), PARSER],
                            capture_output=True, text=True, timeout=60, check=True)
    return json.loads(result.stdout)


def test_an_unchanged_agent_file_is_read_from_the_cache_without_the_parser(tree):
    pytest.importorskip("yaml")
    md = agent_md(tree, "soft:\n  at: 250k\n  say: first")
    record(tree, agent=AGENT, agent_file=str(md))
    assert probe(tree) == {"soft": "first", "parser": True}      # a miss parses
    assert (session_dir(tree) / SS.ROOT / SS.AGENT_CACHE).is_file()
    assert probe(tree) == {"soft": "first", "parser": False}     # a hit does not import it
    agent_md(tree, "soft:\n  at: 250k\n  say: second, longer")
    assert probe(tree) == {"soft": "second, longer", "parser": True}   # an edit is a miss
    assert probe(tree) == {"soft": "second, longer", "parser": False}


def test_the_cache_is_keyed_on_mtime_not_only_size(tree):
    pytest.importorskip("yaml")
    md = agent_md(tree, "soft:\n  at: 250k\n  say: aaaa")
    record(tree, agent=AGENT, agent_file=str(md))
    assert SS.read(SID, env_of(tree)).soft.say == "aaaa"
    agent_md(tree, "soft:\n  at: 250k\n  say: bbbb")              # same size
    touch_now(md)
    assert SS.read(SID, env_of(tree)).soft.say == "bbbb"


def test_a_cached_agent_section_keeps_its_place_in_the_cascade(tree):
    """Host < agent < seat < session, cache hit or miss alike."""
    pytest.importorskip("yaml")
    env = env_of(tree)
    host(tree, {"settle": {"soft": {"at": "100k", "say": "host"}}})
    md = agent_md(tree, "soft:\n  say: agent")
    record(tree, agent=AGENT, agent_file=str(md), seat=SEAT)
    for _ in range(2):                                           # a miss, then a hit
        assert SS.read(SID, env).soft == SS.Soft(100_000, None, "agent")
    w(settings.seat_dir(SEAT, env) / "settings" / "session.json",
      {"settle": {"soft": {"say": "seat"}}})
    assert SS.read(SID, env).soft.say == "seat"


def test_a_cache_that_cannot_be_read_is_a_miss(tree):
    pytest.importorskip("yaml")
    md = agent_md(tree, "soft:\n  at: 1k\n  say: real")
    record(tree, agent=AGENT, agent_file=str(md))
    w(session_dir(tree) / SS.ROOT / SS.AGENT_CACHE, "{broken")
    assert SS.read(SID, env_of(tree)).soft.say == "real"
    cached = json.loads((session_dir(tree) / SS.ROOT / SS.AGENT_CACHE).read_text())
    assert cached["section"] == {"settle": {"soft": {"at": "1k", "say": "real"}}}


def test_a_gone_agent_file_drops_the_layer(tree):
    pytest.importorskip("yaml")
    md = agent_md(tree, "soft:\n  at: 1k\n  say: real")
    record(tree, agent=AGENT, agent_file=str(md))
    assert SS.read(SID, env_of(tree)) is not None
    md.unlink()
    assert SS.read(SID, env_of(tree)) is None                    # not served from the cache
