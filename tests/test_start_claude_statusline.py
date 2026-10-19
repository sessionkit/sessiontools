"""Session start renders the kit layer's `statusLine` into `--settings`.

The status line lives in the sessionkit `claude-code` layer, never in a
person's own `~/.claude`. The cascade merges the `statusLine` object key by
key, like every map; `claude_settings.render` reads it back as ONE checked
object beside `model` and `effortLevel`. Independent of `session statusline
install`: every case drives a host drop-in written here by hand, under a
scratch HOME. Every value is FAKE. Nothing here runs a real claude.

`tests/test_start_claude_settings.py` and the `dry-run-*.golden` files are
left byte for byte as they were: with no kit `statusLine`, the output is
today's (SC6).
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from settings_support import host
from start_support import launch_e2e, plan_for, recorded_argv

from sessiontools import settings
from sessiontools.start import claude_settings as cs
from sessiontools.start.context import PlannedFile, settings_argv

SID = "11111111-2222-3333-4444-555555555555"
TOOL = "claude-code"
FIXTURES = Path(__file__).parent / "fixtures"
#: the claude-code tool's settings directory relative to HOME (settings.host_dir)
KIT_REL = host(Path("/home-of-test"), TOOL).relative_to("/home-of-test")
#: the command `session statusline install` writes (design §3), tilde form
COMMAND = f"~/{KIT_REL}/statusline.sh"
#: the entry install writes into its drop-in (design §1)
ENTRY = {"type": "command", "command": COMMAND, "padding": 1}
DROP_IN = "claude-code.d/50-statusline.json"


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def kit(home: Path) -> Path:
    return host(home, TOOL)


def drop_in(home: Path, entry=ENTRY) -> Path:
    """The host drop-in as install writes it: `_about` (a meta key, never
    merged) beside the entry."""
    return w(kit(home) / DROP_IN, {"_about": "fake: written by hand for a test",
                                   "statusLine": entry})


def seat_file(home: Path, seat: str = "s1") -> Path:
    return home / ".session" / "seats" / seat / "settings" / f"{TOOL}.json"


def session_file(home: Path) -> Path:
    return home / ".session" / "sessions" / SID / "settings" / f"{TOOL}.json"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSIONS_DIR", str(home / ".session" / "sessions"))
    return home


def door_text(rendered: cs.Rendered) -> str:
    """The exact text `settings_argv` plans for RENDERED (a live plan)."""
    _argv, item = settings_argv("claude", rendered.data, directory="/fake/launch")
    assert isinstance(item, PlannedFile)
    return item.text


# --- the goldens (SC5) --------------------------------------------------------

def test_golden_with_a_kit_status_line_beside_model_and_effort(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-host", "effortLevel": "fake-e"})
    drop_in(tree)
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.notes == []
    assert door_text(rendered) == (FIXTURES / "claude-settings-statusline.golden").read_text()


def test_golden_with_a_kit_status_line_alone(tree):
    drop_in(tree)
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.notes == []
    assert door_text(rendered) == (
        FIXTURES / "claude-settings-statusline-only.golden").read_text()


def test_the_provenance_names_the_layer_and_the_drop_in(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-host"})
    drop_in(tree)
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.origins["statusLine"] == ("host", str(kit(tree) / DROP_IN))
    assert cs.provenance(rendered) == (
        "computed: claude-code settings: model from host "
        f"~/{KIT_REL}/claude-code.json, statusLine from host "
        f"~/{KIT_REL}/{DROP_IN}")


def test_the_object_is_one_value_not_one_note_per_leaf(tree):
    """Each `statusLine.*` leaf once got its own "not rendered" note; now one value."""
    drop_in(tree, {**ENTRY, "refreshInterval": 5, "hideVimModeIndicator": True})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.notes == []
    assert rendered.data == {"statusLine": {**ENTRY, "refreshInterval": 5,
                                            "hideVimModeIndicator": True}}
    assert list(rendered.data) == ["statusLine"]
    assert set(rendered.origins) == {"statusLine"}


# --- without: today's output (SC6) ---------------------------------------------

def test_without_a_status_line_the_door_text_is_todays(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-host", "effortLevel": "fake-e"})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert "statusLine" not in rendered.data
    assert door_text(rendered) == '{\n  "effortLevel": "fake-e",\n  "model": "fake-host"\n}\n'


def test_an_empty_status_line_map_adds_nothing(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-host", "statusLine": {}})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.data == {"model": "fake-host"} and rendered.notes == []


# --- a bad shape: one note naming its file, never a refusal --------------------

@pytest.mark.parametrize("entry", [
    "~/fake.sh",                                   # not a map
    ["command", "~/fake.sh"],                      # not a map
    7,                                             # not a map
    True,                                          # not a map
    {"command": "~/fake.sh"},                      # no type
    {"type": "static", "command": "~/fake.sh"},    # not type command
    {"type": "command"},                           # no command
    {"type": "command", "command": ""},            # an empty command
    {"type": "command", "command": "   "},         # a blank command
    {"type": "command", "command": 3},             # a command that is not a string
    {"type": "command", "command": ["~/fake.sh"]},
    {"type": "command", "command": {"path": "~/fake.sh"}},
])
def test_a_bad_shape_is_one_note_naming_its_file(tree, entry):
    w(kit(tree) / "claude-code.json", {"model": "fake-host"})
    path = drop_in(tree, entry)
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.data == {"model": "fake-host"}          # the rest still renders
    assert rendered.notes == [cs.bad_status_line(str(path))]
    assert rendered.notes[0] == (
        f'claude-code: statusLine (from {path}) not rendered: it needs '
        '{"type": "command", "command": "<a non-empty string>"}')


def test_a_bad_shape_names_the_file_of_the_command(tree):
    """The whole-object note names the file whose `command` is at fault, not
    the host file that set the type."""
    drop_in(tree)
    w(seat_file(tree), {"statusLine": {"command": ""}})
    rendered = cs.render(settings.resolve(TOOL, None, seat="s1"))
    assert rendered.data == {}
    assert rendered.notes == [cs.bad_status_line(str(seat_file(tree)))]


@pytest.mark.parametrize("sub, value", [
    ("padding", -1),
    ("padding", True),                 # a bool is not an int
    ("padding", 1.5),
    ("padding", "1"),
    ("refreshInterval", 0),
    ("refreshInterval", False),
    ("refreshInterval", "5"),
    ("hideVimModeIndicator", "yes"),
    ("hideVimModeIndicator", 1),
    ("colour", "fake-red"),            # not a sub-key Claude's status line names
    ("theme", {"fg": "fake"}),         # a nested map
])
def test_a_bad_sub_key_alone_is_dropped_with_a_note(tree, sub, value):
    path = drop_in(tree, {**ENTRY, sub: value})
    rendered = cs.render(settings.resolve(TOOL, None))
    expected = {k: v for k, v in ENTRY.items() if k != sub}
    assert rendered.data == {"statusLine": expected}
    assert rendered.notes == [f"claude-code: statusLine.{sub} (from {path}) not rendered in v1"]


@pytest.mark.parametrize("sub, value", [
    ("padding", 0), ("padding", 4), ("refreshInterval", 1), ("refreshInterval", 30),
    ("hideVimModeIndicator", False), ("hideVimModeIndicator", True),
])
def test_each_allowlisted_sub_key_is_kept(tree, sub, value):
    drop_in(tree, {**ENTRY, sub: value})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.data == {"statusLine": {**ENTRY, sub: value}}
    assert rendered.notes == []


def test_a_bad_sub_key_names_the_file_that_set_it(tree):
    drop_in(tree)
    w(seat_file(tree), {"statusLine": {"refreshInterval": "fake"}})
    rendered = cs.render(settings.resolve(TOOL, None, seat="s1"))
    assert rendered.data == {"statusLine": ENTRY}
    assert rendered.notes == [
        f"claude-code: statusLine.refreshInterval (from {seat_file(tree)}) not rendered in v1"]
    assert rendered.origins["statusLine"] == ("host", str(kit(tree) / DROP_IN))


def test_the_other_keys_still_get_their_notes(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-host", "theme": "fake"})
    drop_in(tree, {**ENTRY, "colour": "fake"})
    rendered = cs.render(settings.resolve(TOOL, None))
    origin = str(kit(tree) / "claude-code.json")
    assert sorted(rendered.notes) == sorted([
        f"claude-code: theme (from {origin}) not rendered in v1",
        f"claude-code: statusLine.colour (from {kit(tree) / DROP_IN}) not rendered in v1"])


def test_a_literally_dotted_key_in_a_file_is_said_not_dropped_silently(tree):
    """`{"statusLine.command": ...}` in a JSON file is a key of its own, not
    the object's `command`: each leaf keeps its "not rendered" note, as at
    7fcba78, and nothing of it is rendered."""
    path = w(kit(tree) / "claude-code.json", {
        "model": "fake-host", "statusLine.type": "command", "statusLine.command": "~/fake.sh"})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.data == {"model": "fake-host"}
    assert sorted(rendered.notes) == sorted(
        f'claude-code: "statusLine.{sub}" (from {path}) not rendered in v1'
        for sub in ("type", "command"))


def test_a_literally_dotted_key_beside_the_object_leaves_the_object_whole(tree):
    drop_in(tree)
    path = w(kit(tree) / "claude-code.json", {"statusLine.colour": {"fg": "fake"}})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.data == {"statusLine": ENTRY}
    assert rendered.notes == [f'claude-code: "statusLine.colour".fg (from {path}) not rendered in v1']


# --- the cascade: key by key (SC7) ---------------------------------------------

def test_a_seat_command_overrides_the_host_and_keeps_its_padding(tree):
    drop_in(tree)
    w(seat_file(tree), {"statusLine": {"command": "~/fake-seat.sh"}})
    rendered = cs.render(settings.resolve(TOOL, None, seat="s1"))
    assert rendered.data == {"statusLine": {**ENTRY, "command": "~/fake-seat.sh"}}
    assert rendered.origins["statusLine"] == ("seat", str(seat_file(tree)))
    assert "statusLine from seat ~/.session/seats/s1/settings/claude-code.json" in (
        cs.provenance(rendered))


def test_a_session_null_removes_the_kit_status_line_without_a_word(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-host"})
    drop_in(tree)
    w(session_file(tree), {"statusLine": None})
    rendered = cs.render(settings.resolve(TOOL, SID))
    assert rendered.data == {"model": "fake-host"}
    assert rendered.notes == []


def test_a_null_after_a_null_and_a_new_command_is_rendered(tree):
    """`null` clears the host's sub-keys; a later layer may set a whole new
    object, which is all that is rendered."""
    drop_in(tree, {**ENTRY, "refreshInterval": 5})
    w(seat_file(tree), {"statusLine": None})
    w(session_file(tree), {"statusLine": {"type": "command", "command": "~/fake-session.sh"}})
    rendered = cs.render(settings.resolve(TOOL, SID, seat="s1"))
    assert rendered.data == {"statusLine": {"type": "command", "command": "~/fake-session.sh"}}
    assert rendered.origins["statusLine"] == ("session", str(session_file(tree)))


def test_a_null_sub_key_is_unset_without_a_word(tree):
    drop_in(tree, {**ENTRY, "refreshInterval": 5})
    w(session_file(tree), {"statusLine": {"refreshInterval": None}})
    rendered = cs.render(settings.resolve(TOOL, SID))
    assert rendered.data == {"statusLine": ENTRY}
    assert rendered.notes == []


def test_a_null_command_is_a_bad_shape_naming_the_layer_that_nulled_it(tree):
    drop_in(tree)
    w(session_file(tree), {"statusLine": {"command": None}})
    rendered = cs.render(settings.resolve(TOOL, SID))
    assert rendered.data == {}
    assert rendered.notes == [cs.bad_status_line(str(session_file(tree)))]


def test_every_sub_key_null_is_unset_without_a_word(tree):
    drop_in(tree)
    w(session_file(tree), {"statusLine": {k: None for k in ENTRY}})
    rendered = cs.render(settings.resolve(TOOL, SID))
    assert rendered.data == {} and rendered.notes == []


# --- the launcher --------------------------------------------------------------

def plan(tmp_path, home: Path, front_matter: str = "claude:\n  effort: high\n",
         argv_tail=(), flags=(), dry_run=True):
    flags = ("--dry-run", *flags) if dry_run else tuple(flags)
    return plan_for(tmp_path, front_matter, argv_tail=argv_tail, flags=flags,
                    extra_env={"HOME": str(home)})


@pytest.fixture
def scratch(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def settings_door(argv: list) -> str | None:
    return argv[argv.index("--settings") + 1] if "--settings" in argv else None


def door_item(p) -> PlannedFile:
    path = settings_door(p.argv)
    (item,) = [f for f in p.files if isinstance(f, PlannedFile) and f.path == path]
    return item


def test_a_live_plan_writes_the_golden(tmp_path, scratch, capsys):
    w(kit(scratch) / "claude-code.json", {"model": "fake-host", "effortLevel": "fake-e"})
    drop_in(scratch)
    p = plan(tmp_path, scratch, dry_run=False)
    assert door_item(p).text == (FIXTURES / "claude-settings-statusline.golden").read_text()
    (source,) = [s for t, s in p.provenance if t == "--settings"]
    assert source.endswith(
        f"statusLine from host ~/{KIT_REL}/{DROP_IN}")
    assert "claude-code:" not in capsys.readouterr().err       # nothing said


def test_a_status_line_alone_opens_the_door(tmp_path, scratch):
    drop_in(scratch)
    p = plan(tmp_path, scratch)
    assert settings_door(p.argv) is not None


def test_a_bad_status_line_alone_opens_no_door_and_says_so_once(tmp_path, scratch, capsys):
    drop_in(scratch, {"type": "command"})
    p = plan(tmp_path, scratch)
    assert "--settings" not in p.argv
    err = capsys.readouterr().err
    assert err.count("statusLine") == 1
    assert f"statusLine (from {kit(scratch) / DROP_IN}) not rendered: it needs" in err


def test_a_seat_overrides_the_host_command_in_the_file(tmp_path, scratch):
    drop_in(scratch)
    w(seat_file(scratch), {"statusLine": {"command": "~/fake-seat.sh"}})
    p = plan(tmp_path, scratch, flags=("--seat", "s1"), dry_run=False)
    assert json.loads(door_item(p).text) == {
        "statusLine": {**ENTRY, "command": "~/fake-seat.sh"}}


def test_an_agent_dotted_command_reaches_the_file(tmp_path, scratch):
    drop_in(scratch)
    fm = ("claude:\n  effort: high\nsettings:\n"
          "  claude-code.statusLine.command: ~/fake-agent.sh\n")
    p = plan(tmp_path, scratch, front_matter=fm, dry_run=False)
    assert json.loads(door_item(p).text) == {
        "statusLine": {**ENTRY, "command": "~/fake-agent.sh"}}
    (source,) = [s for t, s in p.provenance if t == "--settings"]
    assert source.startswith("computed: claude-code settings: statusLine from agent ")


def test_an_agent_files_own_settings_gets_no_kit_status_line(tmp_path, scratch, capsys):
    """The agent file's `claude: settings:`: today's `skipped()` line,
    nothing merged, the kit line not applied."""
    drop_in(scratch)
    p = plan(tmp_path, scratch, front_matter="claude:\n  effort: high\n  settings: mine.json\n")
    assert p.argv.count("--settings") == 1
    assert settings_door(p.argv) == "mine.json"
    err = capsys.readouterr().err
    assert err.count("claude-code:") == 1 and "not applied" in err
    assert f"~/{KIT_REL}/{DROP_IN}" in err


def test_a_crew_settings_without_a_status_line_keeps_the_kit_one(tmp_path, scratch, capsys):
    """A CLI `--settings` is folded on top: one door, the kit line in it,
    beside the crew's own keys."""
    drop_in(scratch)
    crew = tmp_path / "crew.json"
    crew.write_text(json.dumps({"claudeMdExcludes": ["fake/x"]}), encoding="utf-8")
    p = plan(tmp_path, scratch, argv_tail=("--settings", str(crew)), dry_run=False)
    assert p.argv.count("--settings") == 1 and str(crew) not in p.argv
    assert json.loads(door_item(p).text) == {"statusLine": ENTRY,
                                             "claudeMdExcludes": ["fake/x"]}
    assert "claude-code:" not in capsys.readouterr().err


def test_a_live_launch_hands_claude_the_status_line_and_never_makes_dot_claude(tmp_path):
    home = tmp_path / "scratch-home"
    drop_in(home)
    result, record, _work = launch_e2e(tmp_path, "scout", extra_env={"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    path = Path(settings_door(recorded_argv(record)))
    assert json.loads(path.read_text()) == {"statusLine": ENTRY}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (home / ".claude").exists()
