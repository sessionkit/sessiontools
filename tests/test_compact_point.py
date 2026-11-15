"""ST-78 do-003: the SET compaction point, the one figure `{left}` comes from.

`compact_point.read` takes the window from what a hook can see, in Claude
Code's own order (the variable, the recorded `--autocompact`, the
recorded `--settings` file's `autoCompactWindow`) and cuts design R5's
threshold from it; None when nothing sets one. Every tree is under a
scratch HOME and SESSIONS_DIR; nothing reads this seat's own launch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from settings_support import host
from start_support import launch_e2e

from sessiontools import compact_point as cp
from sessiontools.start import claude_settings as cs
from sessiontools.start.record import render_command

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def env_for(home: Path, **extra: str) -> dict:
    return {"HOME": str(home), "SESSIONS_DIR": str(home / "sessions"), **extra}


def launched(home: Path, *argv: str, door: dict | str | None = None) -> Path:
    """A session SID whose launch record's `vendor:` line is `claude ARGV`,
    plus `--settings <door file>` when DOOR is given (a dict is written to
    the file, as the launcher writes it; a str is passed as the value)."""
    launch = home / "sessions" / SID / "launch"
    launch.mkdir(parents=True)
    vendor = ["claude", "--session-id", SID, *argv]
    if isinstance(door, dict):
        path = launch / "session-start-settings-abcd1234.json"
        path.write_text(json.dumps(door, indent=2, sort_keys=True) + "\n")
        vendor += ["--settings", str(path)]
    elif isinstance(door, str):
        vendor += ["--settings", door]
    (launch / "command").write_text(render_command(vendor, "session start x", str(home)))
    return launch


# --- the formula (design R5) -----------------------------------------------------

def test_the_threshold_is_the_window_less_the_reserve_and_the_buffer():
    assert cp.threshold(1_000_000) == 967_000          # M3: a 1M model's point
    assert cp.threshold(100_000) == 67_000             # do-001 a1's 80,000 - 13,000
    assert cp.threshold(400_000) == 400_000 - 20_000 - 13_000


def test_the_percent_override_lowers_it_never_raises_it():
    assert cp.threshold(1_000_000, 25) == 245_000      # a measured figure
    # floor(330,000 x (70/100)) in doubles, as Claude Code's JS computes it:
    # 0.7 is not exact, so the product is 230,999.99..., and the point 230,999.
    assert cp.threshold(350_000, 70) == 230_999
    assert cp.threshold(1_000_000, 100) == 967_000     # 980,000 is above the point
    assert cp.threshold(1_000_000, 0) == 967_000       # outside (0, 100]: ignored
    assert cp.threshold(1_000_000, 101) == 967_000


# --- unknown: nothing sets a window ----------------------------------------------

def test_nothing_set_anywhere_is_none(home):
    assert cp.read(SID, env_for(home)) is None
    assert cp.compact_at(SID, env_for(home)) is None
    assert cp.compact_at(None, env_for(home)) is None


def test_a_door_without_a_window_is_none(home):
    launched(home, door={"model": "fake", "idleCompaction": False, "tui": "default"})
    assert cp.compact_at(SID, env_for(home)) is None


@pytest.mark.parametrize("window", ["auto", 99_999, True, {"default": 400000}, "400000"])
def test_a_window_claude_would_not_take_as_a_number_is_none(home, window):
    launched(home, door={"autoCompactWindow": window})
    assert cp.compact_at(SID, env_for(home)) is None


def test_a_percent_alone_sets_no_point(home):
    """The override is a percent OF a window; with no window set, unknown."""
    assert cp.compact_at(SID, env_for(home, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="70")) is None


def test_a_missing_or_broken_door_is_none_never_a_raise(home):
    launched(home, door=str(home / "gone.json"))
    assert cp.compact_at(SID, env_for(home)) is None
    (home / "bad.json").write_text("{not json")
    (home / "sessions" / SID / "launch" / "command").unlink()
    (home / "sessions" / SID / "launch" / "command").write_text(
        render_command(["claude", "--settings", str(home / "bad.json")], "", ""))
    assert cp.compact_at(SID, env_for(home)) is None


def test_a_record_that_will_not_split_is_none(home):
    launch = home / "sessions" / SID / "launch"
    launch.mkdir(parents=True)
    (launch / "command").write_text("vendor: claude --settings 'unclosed\n")
    assert cp.launch_argv(SID, env_for(home)) is None
    assert cp.compact_at(SID, env_for(home)) is None


# --- set: each source, in Claude Code's order ------------------------------------

def test_the_door_window_sets_the_point(home):
    launched(home, door={"autoCompactWindow": 400_000, "idleCompaction": False})
    point = cp.read(SID, env_for(home))
    assert point.at == cp.threshold(400_000) == 367_000
    assert point.window == 400_000
    assert point.source.startswith("--settings ") and point.source.endswith(".json")


def test_the_door_window_with_the_percent_override(home):
    launched(home, door={"autoCompactWindow": 350_000})
    env = env_for(home, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="70")
    assert cp.compact_at(SID, env) == cp.threshold(350_000, 70) == 230_999


def test_an_inline_settings_value_is_read_too(home):
    launched(home, door=json.dumps({"autoCompactWindow": 500_000}))
    assert cp.compact_at(SID, env_for(home)) == 467_000


def test_a_tilde_door_is_under_the_launchs_home(home):
    (home / "crew.json").write_text(json.dumps({"autoCompactWindow": 200_000}))
    launched(home, door="~/crew.json")
    assert cp.compact_at(SID, env_for(home)) == 167_000


def test_the_variable_outranks_the_flag_and_the_door(home):
    launched(home, "--autocompact", "300000", door={"autoCompactWindow": 400_000})
    point = cp.read(SID, env_for(home, CLAUDE_CODE_AUTO_COMPACT_WINDOW="500000"))
    assert (point.at, point.window, point.source) == (
        467_000, 500_000, "CLAUDE_CODE_AUTO_COMPACT_WINDOW")


def test_the_variable_alone_needs_no_launch_record(home):
    env = env_for(home, CLAUDE_CODE_AUTO_COMPACT_WINDOW="400k")
    assert cp.compact_at(None, env) == 367_000


def test_a_variable_that_is_not_a_window_falls_through(home):
    launched(home, door={"autoCompactWindow": 400_000})
    env = env_for(home, CLAUDE_CODE_AUTO_COMPACT_WINDOW="auto")
    assert cp.compact_at(SID, env) == 367_000


def test_the_flag_outranks_the_door(home):
    launched(home, "--autocompact", "300000", door={"autoCompactWindow": 400_000})
    point = cp.read(SID, env_for(home))
    assert (point.at, point.source) == (267_000, "--autocompact")


def test_the_flag_set_to_auto_hands_the_window_back_to_claude(home):
    """`--autocompact auto` skips every settings file: Claude Code's own
    choice, which a hook cannot see. Unknown, not the door's window."""
    launched(home, "--autocompact=auto", door={"autoCompactWindow": 400_000})
    assert cp.compact_at(SID, env_for(home)) is None


# --- off: no auto compaction, no point ---------------------------------------------

@pytest.mark.parametrize("name", ["DISABLE_COMPACT", "DISABLE_AUTO_COMPACT"])
def test_a_disable_variable_is_no_point(home, name):
    launched(home, door={"autoCompactWindow": 400_000})
    assert cp.compact_at(SID, env_for(home, **{name: "1"})) is None


def test_auto_compact_disabled_in_the_door_is_no_point(home):
    launched(home, door={"autoCompactWindow": 400_000, "autoCompactEnabled": False})
    assert cp.compact_at(SID, env_for(home)) is None
    env = env_for(home, CLAUDE_CODE_AUTO_COMPACT_WINDOW="500000")
    assert cp.compact_at(SID, env) is None


# --- off below the door: Claude's own files, highest first (`ei`; review of do-003) ---

def claude_file(path: Path, data: dict | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


def test_auto_compact_off_in_the_user_file_is_no_point(home):
    """The by-hand off switch: `autoCompactEnabled: false`
    in the seat's own user settings. Claude Code (`vp`, `ei`) then never
    auto-compacts, idle included, whichever source set the window."""
    claude_file(home / ".claude" / "settings.json", {"autoCompactEnabled": False})
    launched(home, door={"autoCompactWindow": 400_000})
    assert cp.compact_at(SID, env_for(home)) is None
    env = env_for(home, CLAUDE_CODE_AUTO_COMPACT_WINDOW="500000")
    assert cp.compact_at(SID, env) is None


def test_the_user_file_is_under_claude_config_dir_when_set(home, tmp_path):
    config = tmp_path / "config"
    claude_file(config / "settings.json", {"autoCompactEnabled": False})
    launched(home, door={"autoCompactWindow": 400_000})
    assert cp.compact_at(SID, env_for(home, CLAUDE_CONFIG_DIR=str(config))) is None
    claude_file(home / ".claude" / "settings.json", {"autoCompactEnabled": False})
    other = tmp_path / "other-config"
    other.mkdir()
    assert cp.compact_at(SID, env_for(home, CLAUDE_CONFIG_DIR=str(other))) == 367_000


def test_the_project_and_local_files_are_under_claude_project_dir(home, project):
    launched(home, door={"autoCompactWindow": 400_000})
    claude_file(project / ".claude" / "settings.json", {"autoCompactEnabled": False})
    assert cp.compact_at(SID, env_for(home)) == 367_000      # no project dir: not seen
    assert cp.compact_at(SID, env_for(home, CLAUDE_PROJECT_DIR=str(project))) is None


@pytest.mark.parametrize("files, expected", [
    # The highest source that SETS the key wins: door, local, project, user, legacy.
    ({"door": True, "user": False}, 367_000),
    ({"local": True, "project": False, "user": False}, 367_000),
    ({"local": False, "user": True}, None),
    ({"project": True, "user": False}, 367_000),
    ({"project": False, "user": True}, None),
    ({"user": True, "legacy": False}, 367_000),
    ({"legacy": False}, None),
    ({"legacy": True}, 367_000),
    ({"user": "false"}, 367_000),       # only a JSON false is off
])
def test_the_highest_source_that_sets_the_key_wins(home, project, files, expected):
    door = {"autoCompactWindow": 400_000}
    if "door" in files:
        door["autoCompactEnabled"] = files["door"]
    launched(home, door=door)
    paths = {"local": project / ".claude" / "settings.local.json",
             "project": project / ".claude" / "settings.json",
             "user": home / ".claude" / "settings.json",
             "legacy": home / ".claude.json"}
    for name, value in files.items():
        if name != "door":
            claude_file(paths[name], {"autoCompactEnabled": value})
    assert cp.compact_at(SID, env_for(home, CLAUDE_PROJECT_DIR=str(project))) == expected


def test_the_legacy_global_config_is_under_claude_config_dir_when_set(home, tmp_path):
    config = tmp_path / "config"
    claude_file(config / ".claude.json", {"autoCompactEnabled": False})
    launched(home, door={"autoCompactWindow": 400_000})
    assert cp.compact_at(SID, env_for(home, CLAUDE_CONFIG_DIR=str(config))) is None
    assert cp.global_config(env_for(home)) == home / ".claude.json"


def test_setting_sources_drops_the_files_it_leaves_out(home, project):
    claude_file(home / ".claude" / "settings.json", {"autoCompactEnabled": False})
    env = env_for(home, CLAUDE_PROJECT_DIR=str(project))
    launched(home, "--setting-sources", "project,local", door={"autoCompactWindow": 400_000})
    assert cp.compact_at(SID, env) == 367_000


def test_a_broken_claude_file_sets_nothing(home, project):
    claude_file(home / ".claude" / "settings.json", "{not json")
    claude_file(project / ".claude" / "settings.json", "[false]")
    launched(home, door={"autoCompactWindow": 400_000})
    assert cp.compact_at(SID, env_for(home, CLAUDE_PROJECT_DIR=str(project))) == 367_000


def test_claude_files_are_not_opened_without_a_window(home, monkeypatch):
    """The off switch is read only once a window is found: a seat with no
    point set costs the hook no extra file reads."""
    def no(*_a, **_k):
        raise AssertionError("opened a Claude settings file with no window set")
    monkeypatch.setattr(cp, "compaction_enabled", no)
    assert cp.compact_at(SID, env_for(home)) is None


def test_the_off_switch_reader_imports_no_yaml():
    """The settle hook imports this module on every batch, maybe under a
    bare python3: the reader of Claude's files must not pull in
    `sessiontools.start` (PyYAML)."""
    import subprocess
    import sys
    code = ("import sys, sessiontools.compact_point as cp; "
            "cp.compaction_enabled(None, [], {'HOME': '/nonexistent'}); "
            "print(sorted(m for m in sys.modules if m == 'yaml' or "
            "m.startswith('sessiontools.start')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "[]"


def test_claude_settings_files_has_one_definition():
    assert cs.claude_settings_files is cp.claude_settings_files


# --- one spelling: the names the door renders are the names read back --------------

def test_the_door_and_the_reader_share_the_key_names():
    assert cs.AUTO_COMPACT_WINDOW == cp.AUTO_COMPACT_WINDOW == "autoCompactWindow"
    assert cs.IDLE_COMPACTION == cp.IDLE_COMPACTION == "idleCompaction"
    assert {cp.AUTO_COMPACT_WINDOW, cp.IDLE_COMPACTION} <= set(cs.RENDERED)


def test_a_real_launch_door_is_read_back(tmp_path):
    """End to end against a stub claude: `session start` renders the
    window into its door and records the argv; the reader finds the door
    from that record and cuts the point from what it rendered."""
    scratch = tmp_path / "scratch-home"
    w = host(scratch, "claude-code") / "claude-code.json"
    w.parent.mkdir(parents=True, exist_ok=True)
    w.write_text(json.dumps({"autoCompactWindow": 400_000, "idleCompaction": False}))
    result, _record, _work = launch_e2e(tmp_path, "scout", extra_env={"HOME": str(scratch)})
    assert result.returncode == 0, result.stderr
    (command,) = tmp_path.rglob("launch/command")
    sessions = command.parent.parent.parent
    sid = command.parent.parent.name
    env = {"HOME": str(scratch), "SESSIONS_DIR": str(sessions)}
    point = cp.read(sid, env)
    assert point is not None and point.at == 367_000 and point.window == 400_000
    door = json.loads(Path(point.source.removeprefix("--settings ")).read_text())
    assert door["idleCompaction"] is False


# --- the text, read as Claude Code 2.1.316 reads it (do-003 review) -----------------

@pytest.mark.parametrize("text, window", [
    ("400000", 400_000), ("400k", 400_000), ("400K", 400_000), ("0.4m", 400_000),
    (" 400k ", 400_000), ("400,000", 400_000), ("400_000", 400_000), ("4e5", 400_000),
    ("400000tokens", 400_000),                        # JS parseInt: the leading digits
    ("50000", 100_000), ("2000000", 1_000_000),       # clamped, as `cQt` clamps
    ("200", 100_000),                                 # no bare thousands in the variable
])
def test_the_variable_is_read_and_clamped_as_claude_reads_it(text, window):
    assert cp.env_window(text) == window


@pytest.mark.parametrize("text", ["auto", "", "0", "-5", "k", "abc", "4.5e0"])
def test_a_variable_that_is_no_number_is_none(text):
    assert cp.env_window(text) is None


@pytest.mark.parametrize("text, window", [
    ("200", 200_000), ("1000", 1_000_000), ("200k", 200_000), ("300000", 300_000),
    ("auto", None), ("50000", None), ("2m", None), ("99", None),
])
def test_the_flag_takes_bare_thousands_and_refuses_out_of_range(text, window):
    assert cp.flag_window(text) == window


def test_a_clamped_variable_sets_the_clamped_point(home):
    env = env_for(home, CLAUDE_CODE_AUTO_COMPACT_WINDOW="50000")
    assert cp.read(None, env) == cp.Point(67_000, 100_000, cp.WINDOW_ENV)


def test_a_bare_thousands_flag_sets_the_point(home):
    launched(home, "--autocompact", "200", door={"autoCompactWindow": 400_000})
    assert cp.read(SID, env_for(home)) == cp.Point(167_000, 200_000, cp.WINDOW_FLAG)


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_a_disable_variable_that_is_not_true_leaves_the_point(home, value):
    launched(home, door={"autoCompactWindow": 400_000})
    for name in cp.OFF_ENVS:
        assert cp.compact_at(SID, env_for(home, **{name: value})) == 367_000


@pytest.mark.parametrize("value", ["true", "YES", " on "])
def test_a_disable_variable_reads_any_true_word(home, value):
    launched(home, door={"autoCompactWindow": 400_000})
    assert cp.compact_at(SID, env_for(home, DISABLE_COMPACT=value)) is None


# --- the last two corners of `ep` and `parseFloat` (do-003 refine) --------------

@pytest.mark.parametrize("text, window", [
    ("400 000", 400_000), ("400 000", 400_000), ("400 000", 400_000),
    ("1,000,000", 1_000_000),
    ("400,000_000", 100_000),        # mixed separators are not grouped: parseInt's 400
    ("Infinityk", 1_000_000),        # parseFloat reads Infinity: above, so clamped
    ("-Infinityk", None),            # below 1: unusable
    ("Infinity", None),              # parseInt reads no Infinity
])
def test_the_variable_reads_every_separator_and_infinity_as_claude_does(text, window):
    assert cp.env_window(text) == window


def test_the_separators_are_spelled_as_escapes_not_invisible_bytes():
    source = Path(cp.__file__).read_text(encoding="utf-8")
    assert source.isascii()


@pytest.mark.parametrize("text", ["Infinityk", "Infinity"])
def test_an_infinite_flag_is_refused(text):
    assert cp.flag_window(text) is None
