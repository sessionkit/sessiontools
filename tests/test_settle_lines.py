"""The settle alert's two lines, read LIVE from the cascade (ST-78 do-004).

The soft line once per crossing of `soft.at + k * soft.every`, the hard
line every batch (or once per `hard.every`) from the lower of `hard.at`
and the SET compaction point less `hard.before_compact`; one line per
batch, hard wins; `{left}` and `{compact_at}` only from the set point,
`?` when unknown; an agent layer's words and values; a compaction
re-arms the marks; `session start`'s birth copy of SESSION_SETTLE_AT
keeps today's behaviour.

Every test builds its own tree under a private HOME and SESSIONS_DIR and
passes that environment explicitly, so nothing reads the live machine's
settings, launch records or Claude Code variables.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from settings_support import host as host_dir

from sessiontools import compact_point, settings, settle, window
from sessiontools import settle_settings as SS
from sessiontools.settle_settings import Hard, Soft

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "settle-hook"
SID = "0f0e0d0c-0b0a-4908-8706-050403020100"

pytestmark = pytest.mark.usefixtures("tree")


# --- the tree --------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path) -> Path:
    (tmp_path / ".session" / "sessions" / SID).mkdir(parents=True)
    return tmp_path


def env_of(tree: Path, **more: str) -> dict:
    return {"HOME": str(tree), "SESSIONS_DIR": str(tree / ".session" / "sessions"), **more}


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def session_dir(tree: Path) -> Path:
    return tree / ".session" / "sessions" / SID


def host(tree: Path, block) -> Path:
    return w(host_dir(tree, "session") / "session.json", {"settle": block})


def session_layer(tree: Path, block, name: str = "10-test.json") -> Path:
    return w(session_dir(tree) / "settings" / "session.d" / name, {"settle": block})


def usage_row(tokens: int) -> str:
    return json.dumps({"type": "assistant", "message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": tokens,
                                              "cache_creation_input_tokens": 0,
                                              "cache_read_input_tokens": 0}}})


def batch(tree: Path, used: int, env: dict | None = None) -> str | None:
    """One batch at a count of USED: the transcript's last usage row is
    USED, and the hook decides in process. The line, or None."""
    path = tree / f"{SID}.jsonl"
    with path.open("a") as f:
        f.write(usage_row(used) + "\n")
    body = {"session_id": SID, "cwd": "/nowhere", "hook_event_name": "PostToolBatch",
            "transcript_path": str(path)}
    out = io.StringIO()
    assert settle.main(io.StringIO(json.dumps(body)), out, env or env_of(tree)) == 0
    if not out.getvalue():
        return None
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, out.getvalue()
    return json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]


def run(tree: Path, counts: list[int], env: dict | None = None) -> list[str | None]:
    return [batch(tree, n, env) for n in counts]


def k(n: int) -> str:
    return window.fmt_tokens(n)


# --- the soft line ------------------------------------------------------------------

def test_soft_at_250k_every_25k_speaks_at_250k_275k_300k_once_each(tree):
    host(tree, {"soft": {"at": "250k", "every": "25k", "say": "soft {at} {used}"}})
    said = run(tree, [100_000, 249_999, 250_000, 260_000, 274_999, 275_000, 280_000,
                      299_000, 300_000, 301_000, 310_000])
    assert said == [None, None, "soft 250K 250K", None, None, "soft 275K 275K", None,
                    None, "soft 300K 300K", None, None]


def test_soft_every_unset_is_every_batch_from_soft_at(tree):
    """Reviewer 3f09156a, F3: this is what keeps today's behaviour for a seat
    that exports only SESSION_SETTLE_AT."""
    host(tree, {"soft": {"at": "250k", "say": "soft {at} {used}"}})
    said = run(tree, [249_999, 250_000, 250_001, 260_000, 400_000])
    assert said == [None, "soft 250K 250K", "soft 250K 250K", "soft 250K 260K",
                    "soft 250K 400K"]


def test_a_jump_past_several_marks_says_one_line_naming_the_highest(tree):
    host(tree, {"soft": {"at": "250k", "every": "25k", "say": "past {at}"}})
    assert run(tree, [320_000, 324_000, 325_000]) == ["past 300K", None, "past 325K"]


def test_a_compaction_rearms_the_marks_above_the_count(tree):
    host(tree, {"soft": {"at": "250k", "every": "25k", "say": "past {at}"}})
    assert run(tree, [250_000, 275_000, 280_000]) == ["past 250K", "past 275K", None]
    marks = session_dir(tree) / SS.ROOT / SS.SOFT
    assert sorted(os.listdir(marks)) == ["250000", "275000"]
    assert run(tree, [60_000]) == [None]                       # compacted
    assert os.listdir(marks) == []
    assert run(tree, [250_000, 260_000, 275_000]) == ["past 250K", None, "past 275K"]


def test_a_fall_keeps_the_marks_at_or_under_the_count(tree):
    host(tree, {"soft": {"at": "250k", "every": "25k", "say": "past {at}"}})
    assert run(tree, [250_000, 275_000, 300_000]) == ["past 250K", "past 275K", "past 300K"]
    assert run(tree, [280_000, 290_000, 300_000]) == [None, None, "past 300K"]


def test_marks_are_made_with_o_excl_so_only_the_first_batch_speaks(tree):
    marks = session_dir(tree) / SS.ROOT / SS.SOFT
    assert settle.first_crossing(marks, 250_000) is True
    assert settle.first_crossing(marks, 250_000) is False
    assert (marks / "250000").is_file()
    assert oct(marks.stat().st_mode & 0o777) == oct(0o700)


def test_with_no_session_dir_a_line_with_every_is_silent_and_one_without_speaks(tree):
    """No dir, no marks: a guess is not a crossing, so no line repeats every batch."""
    os.rmdir(session_dir(tree))
    host(tree, {"soft": {"at": "1k", "every": "1k", "say": "soft"}})
    assert run(tree, [5_000, 6_000]) == [None, None]
    assert not session_dir(tree).exists()
    host(tree, {"soft": {"at": "1k", "say": "soft"}})
    assert run(tree, [5_000, 6_000]) == ["soft", "soft"]


# --- the hard line ------------------------------------------------------------------

def test_hard_at_350k_speaks_every_batch(tree):
    host(tree, {"hard": {"at": "350k", "say": "now {at} {used}"}})
    said = run(tree, [349_999, 350_000, 350_001, 360_000, 900_000])
    assert said == [None, "now 350K 350K", "now 350K 350K", "now 350K 360K",
                    "now 350K 900K"]


def test_hard_every_speaks_once_per_crossing_from_its_line(tree):
    host(tree, {"hard": {"at": "350k", "every": "10k", "say": "now {at}"}})
    said = run(tree, [350_000, 355_000, 360_000, 365_000, 381_000])
    assert said == ["now 350K", None, "now 360K", None, "now 380K"]


def test_hard_wins_when_both_cross(tree):
    host(tree, {"soft": {"at": "250k", "every": "25k", "say": "soft {at}"},
                "hard": {"at": "350k", "say": "hard {at}"}})
    said = run(tree, [250_000, 275_000, 300_000, 325_000, 350_000, 375_000])
    assert said == ["soft 250K", "soft 275K", "soft 300K", "soft 325K", "hard 350K",
                    "hard 350K"]


def test_past_the_hard_line_the_soft_line_is_silent_and_its_crossing_is_spent(tree):
    """hard.every set, so the hard line is quiet between its crossings: the
    soft line still says nothing there, and does not save its crossing up."""
    host(tree, {"soft": {"at": "250k", "every": "25k", "say": "soft {at}"},
                "hard": {"at": "350k", "every": "50k", "say": "hard {at}"}})
    assert run(tree, [350_000, 375_000, 380_000, 400_000]) == [
        "hard 350K", None, None, "hard 400K"]
    marks = session_dir(tree) / SS.ROOT / SS.SOFT
    assert "375000" in os.listdir(marks)


def test_the_kin_case(tree):
    """design.md R2: soft from 250k every 25k, hard at 350k or 25k short of
    a set point; here no point is set, so 350k it is, and {left} is `?`."""
    host(tree, {"soft": {"at": 250000, "every": 25000},
                "hard": {"at": 350000, "before_compact": 25000}})
    said = run(tree, [240_000, 250_000, 260_000, 275_000, 300_000, 325_000, 349_000,
                      350_000, 351_000])
    soft = [s for s in said if s and s.startswith("You're past")]
    assert [s.split()[2] for s in soft] == ["250K", "275K", "300K", "325K"]
    assert said[0] is None and said[2] is None and said[6] is None
    assert said[7] == "Now: 350K used, ? before compaction. Settle: save what matters, then hand over."
    assert said[8].startswith("Now: 351K used, ? before")


# --- {left} and {compact_at}: only from the SET point --------------------------------

ALL = "at {at} used {used} left {left} point {compact_at}"


def test_left_and_compact_at_are_unknown_with_no_set_point(tree):
    host(tree, {"hard": {"at": "350k", "before_compact": "25k", "say": ALL},
                "soft": {"at": "100k", "say": ALL}})
    assert run(tree, [100_000, 360_000]) == ["at 100K used 100K left ? point ?",
                                             "at 350K used 360K left ? point ?"]


def test_left_comes_from_the_set_point_and_places_the_hard_line(tree):
    """A launch-set window (here the variable Claude Code and the hook both
    see): the point is compact_point's, and the hard line is the lower of
    hard.at and the point less before_compact."""
    env = env_of(tree, **{compact_point.WINDOW_ENV: "400000"})
    point = compact_point.threshold(400_000)
    assert point == compact_point.compact_at(SID, env) == 367_000
    host(tree, {"hard": {"at": "350k", "before_compact": "25k", "say": ALL}})
    line = point - 25_000                                     # 342k, under 350k
    assert run(tree, [line - 1, line, 360_000, 400_000], env) == [
        None,
        f"at {k(line)} used {k(line)} left {k(point - line)} point {k(point)}",
        f"at {k(line)} used 360K left {k(point - 360_000)} point {k(point)}",
        f"at {k(line)} used 400K left 0 point {k(point)}"]      # never below zero


def test_hard_at_under_the_point_line_wins(tree):
    env = env_of(tree, **{compact_point.WINDOW_ENV: "1000000"})
    host(tree, {"hard": {"at": "350k", "before_compact": "25k", "say": "{at} {left}"}})
    point = compact_point.compact_at(SID, env)
    assert run(tree, [349_000, 350_000], env) == [None, f"350K {k(point - 350_000)}"]


def test_before_compact_alone_is_off_with_no_point_and_on_with_one(tree):
    host(tree, {"hard": {"before_compact": "25k", "say": "{at}"}})
    assert run(tree, [900_000]) == [None]
    env = env_of(tree, **{compact_point.WINDOW_ENV: "400000"})
    assert run(tree, [900_000], env) == ["342K"]


def test_compaction_off_is_an_unknown_point(tree):
    env = env_of(tree, **{compact_point.WINDOW_ENV: "400000", "DISABLE_AUTO_COMPACT": "1"})
    host(tree, {"hard": {"at": "350k", "before_compact": "25k", "say": "{left} {compact_at}"}})
    assert run(tree, [342_000, 350_000], env) == [None, "? ?"]


def test_the_point_is_not_read_when_no_figure_needs_it():
    def refuse():
        raise AssertionError("the point was read")
    soft = Soft(250_000, None, "soft {at}")
    assert settle.line(soft, None, 300_000, None, refuse) == "soft 250K"
    assert settle.line(soft, Hard(350_000, None, None, "{left}"), 300_000, None,
                       refuse) == "soft 250K"


def test_the_point_is_read_once_per_batch():
    calls = []

    def point():
        calls.append(1)
        return 500_000
    hard = Hard(None, 25_000, None, "{left} {compact_at}")
    assert settle.line(None, hard, 480_000, None, point) == "20K 500K"
    assert calls == [1]


# --- the agent layer overrides values AND words (R3) ---------------------------------

def agent_md(tree: Path, settle_yaml: str) -> Path:
    body = "\n".join("      " + x for x in settle_yaml.strip("\n").splitlines())
    return w(tree / "agents" / "alice.md",
             f"---\nname: alice\nsettings:\n  session:\n    settle:\n{body}\n---\n# alice\n")


def record(tree: Path, **fields) -> Path:
    return w(session_dir(tree) / "settings" / "session.json", fields)


def test_an_agent_layer_settle_hard_say_replaces_the_host_message(tree):
    pytest.importorskip("yaml")
    host(tree, {"hard": {"at": "350k", "say": "host: {used}"}})
    assert run(tree, [360_000]) == ["host: 360K"]
    md = agent_md(tree, "hard:\n  say: 'Alice, now: {used}, {left} left.'")
    record(tree, agent="alice", agent_file=str(md))
    assert run(tree, [361_000]) == ["Alice, now: 361K, ? left."]


def test_an_agent_layer_overrides_the_host_values_and_a_session_layer_beats_it(tree):
    pytest.importorskip("yaml")
    host(tree, {"soft": {"at": "100k"}, "hard": {"at": "200k", "say": "host {at}"}})
    md = agent_md(tree, "hard:\n  at: 350k\n  say: agent {at}")
    record(tree, agent="alice", agent_file=str(md))
    assert run(tree, [200_000, 350_000]) == [batch_soft(100_000, 200_000), "agent 350K"]
    session_layer(tree, {"hard": {"at": "300k"}})
    assert run(tree, [300_000]) == ["agent 300K"]


def batch_soft(at: int, used: int) -> str:
    return SS.SOFT_SAY.replace("{at}", k(at)).replace("{used}", k(used))


def test_the_agent_override_through_the_real_shim(tree):
    """The hook as Claude Code runs it: the agent's words reach the line."""
    pytest.importorskip("yaml")
    host(tree, {"hard": {"at": "1k", "say": "host"}})
    md = agent_md(tree, "hard:\n  say: agent {used}")
    record(tree, agent="alice", agent_file=str(md))
    path = w(tree / f"{SID}.jsonl", usage_row(5_000) + "\n")
    bindir = tree / "pybin"
    stub = w(bindir / "python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    body = {"session_id": SID, "transcript_path": str(path), "hook_event_name": "PostToolBatch"}
    result = subprocess.run(["/bin/sh", str(SHIM)], input=json.dumps(body),
                            env={**env_of(tree), "PATH": f"{bindir}:/usr/bin:/bin",
                                 "CLAUDE_CODE_SESSION_ID": SID},
                            capture_output=True, text=True, timeout=30, check=False)
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "agent 5K"


# --- LIVE: an edited layer is heard at the next batch (R4) ----------------------------

def test_an_edited_layer_is_heard_at_the_next_batch(tree):
    host(tree, {"soft": {"at": "250k", "say": "soft {at}"}})
    assert run(tree, [300_000]) == ["soft 250K"]
    host(tree, {"soft": {"at": "350k", "say": "soft {at}"}})
    assert run(tree, [300_000]) == [None]
    session_layer(tree, {"hard": {"at": "290k", "say": "hard {at}"}})
    assert run(tree, [300_000]) == ["hard 290K"]
    session_layer(tree, None, name="20-off.json")             # settle: null, below
    assert run(tree, [300_000]) == [None]


def test_an_unreadable_layer_is_silence(tree):
    host(tree, {"soft": {"at": "1k"}})
    w(session_dir(tree) / "settings" / "session.d" / "10-bad.json", "{not json")
    assert run(tree, [5_000]) == [None]


# --- today's behaviour: SESSION_SETTLE_AT copied at birth ----------------------------

def test_a_session_start_seat_exporting_only_session_settle_at_keeps_todays_line(tree):
    """`session start` installs the schema, then copies the variable once
    into the session layer; the hook reads it there: one line, every batch,
    from the mark. The variable in the hook's own environment is not read."""
    env = env_of(tree)
    settings.install_own_schemas(session_dir(tree), env)
    settings.write_env_at_birth(session_dir(tree), {SS.AT_ENV: "250k"}, env=env)
    drop = session_dir(tree) / "settings" / "session.d" / settings.ENV_DROP_IN
    assert json.loads(drop.read_text()) == {"settle": {"soft": {"at": "250k"}}}
    assert run(tree, [249_000, 250_000, 260_000], env) == [
        None, batch_soft(250_000, 250_000), batch_soft(250_000, 260_000)]
    drop.unlink()
    assert run(tree, [260_000], {**env, SS.AT_ENV: "250k"}) == [None]


def test_the_birth_copy_of_the_say_variable_replaces_the_soft_words(tree):
    env = env_of(tree)
    settings.install_own_schemas(session_dir(tree), env)
    settings.write_env_at_birth(session_dir(tree),
                                {SS.AT_ENV: "1k", SS.SAY_ENV: "wrap up at {at}"}, env=env)
    assert run(tree, [5_000], env) == ["wrap up at 1K"]


# --- the pure parts -----------------------------------------------------------------

@pytest.mark.parametrize(("hard", "point", "line"), [
    (Hard(350_000, None, None, ""), None, 350_000),
    (Hard(350_000, 25_000, None, ""), None, 350_000),           # unknown point: at alone
    (Hard(350_000, 25_000, None, ""), 367_000, 342_000),        # the lower
    (Hard(300_000, 25_000, None, ""), 367_000, 300_000),
    (Hard(None, 25_000, None, ""), 367_000, 342_000),
    (Hard(None, 25_000, None, ""), None, None),
    (Hard(None, 500_000, None, ""), 367_000, 0),               # never below zero
])
def test_hard_line(hard, point, line):
    assert settle.hard_line(hard, point) == line


@pytest.mark.parametrize(("start", "every", "used", "mark"), [
    (250_000, None, 249_999, None), (250_000, None, 250_000, 250_000),
    (250_000, None, 900_000, 250_000), (250_000, 25_000, 274_999, 250_000),
    (250_000, 25_000, 275_000, 275_000), (250_000, 25_000, 320_000, 300_000),
])
def test_crossed(start, every, used, mark):
    assert settle.crossed(start, every, used) == mark


@pytest.mark.parametrize(("text", "point", "said"), [
    ("{at}|{used}|{left}|{compact_at}", None, "250K|300K|?|?"),
    ("{at}|{used}|{left}|{compact_at}", 367_000, "250K|300K|67K|367K"),
    ("{left}", 200_000, "0"),
    ("a { b } {x}\n\n{left}\n", None, "a { b } {x} ?"),
])
def test_fill(text, point, said):
    assert settle.fill(text, 250_000, 300_000, point) == said


def test_rearm_ignores_what_is_not_a_mark(tree):
    marks = session_dir(tree) / "m"
    for name in ("100", "300", "notes", ".hidden"):
        w(marks / name, "")
    settle.rearm(marks, 200)
    assert sorted(os.listdir(marks)) == [".hidden", "100", "notes"]
    settle.rearm(tree / "no-such-dir", 0)
