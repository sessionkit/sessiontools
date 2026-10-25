"""The vendor table, the one rendering rule, the merge, the duplicate-door
guard and the assembled argv. Ported from the old suite; the codex/pi
cases now run against a synthetic table row, because the port ships the
claude row only while keeping the table's shape."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from start_support import make_agents_root

from sessiontools.start import vendor as v
from sessiontools.start.agents import Agent, resolve_agent
from sessiontools.start.context import DRY_RUN_MESSAGE_NAME, PlannedFile, PlannedPlugin
from sessiontools.start.identity import SessionIdentity
from sessiontools.start.plan import build_annotated_argv, build_vendor_argv

CONFIG_VENDOR = {   # the old codex row's shape: a config door and no other
    "cmd": "acme", "identity_door": None, "agent_plugin_dir_arg": None, "agents_door": None,
    "system_prompt_door": None, "system_prompt_file_door": None,
    "session_id_flag": None, "prompt_separator": None, "config_door": "-c",
    "settings_door": None, "env": {}}
INLINE_VENDOR = dict(CONFIG_VENDOR, cmd="inline", config_door=None,
                     system_prompt_door="--append-system-prompt")


@pytest.fixture
def acme(monkeypatch):
    monkeypatch.setitem(v.VENDORS, "acme", CONFIG_VENDOR)
    monkeypatch.setitem(v.VENDORS, "inline", INLINE_VENDOR)


# ---------- the table stays data ----------------------------------------------

def test_vendor_table_is_data_with_one_schema():
    fields = {"cmd", "identity_door", "agent_plugin_dir_arg", "agents_door",
              "system_prompt_door",
              "system_prompt_file_door", "session_id_flag", "prompt_separator",
              "config_door", "settings_door", "env"}
    for vendor, table in v.VENDORS.items():
        assert set(table) == fields, vendor
        assert isinstance(table["cmd"], str) and table["cmd"]
        assert isinstance(table["env"], dict)


def test_the_port_ships_the_claude_row_only():
    assert list(v.VENDORS) == ["claude"]


def test_claude_vendor_disables_prompt_suggestion_for_every_launch():
    """SESH-734: a standing order."""
    assert v.VENDORS["claude"]["env"]["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] == "false"


def test_the_agent_plugin_arg_is_not_a_door():
    """--plugin-dir is REPEATABLE; a door name would make the guard refuse
    every agent that declares its own plugin-dir - which both live ones do."""
    assert "--plugin-dir" not in v.vendor_doors("claude")


# ---------- the one rendering rule -------------------------------------------

def test_native_model_key_is_not_synthesized_into_argv():
    argv = v.vendor_front_matter_argv("claude", {}, agent_ref="scout")
    assert argv == ["--agent", "scout"]


def test_scalar_list_and_boolean_render():
    assert v.render_vendor_args("claude", {
        "effort": "high", "add-dir": ["~/tasks", "~/notes"], "chrome": True}) == [
        "--effort", "high", "--add-dir", "~/tasks", "--add-dir", "~/notes", "--chrome"]


def test_false_and_null_render_to_nothing():
    assert v.render_vendor_args("claude", {"chrome": False, "effort": None}) == []


def test_numeric_scalars_render_as_strings():
    assert v.render_vendor_args("claude", {"autocompact": 200000}) == ["--autocompact", "200000"]


def test_new_vendor_flag_renders_with_no_launcher_change():
    assert v.render_vendor_args("claude", {"foo": "bar"}) == ["--foo", "bar"]


def test_a_config_door_renders_dotted_paths_and_lowercase_booleans(acme):
    assert v.render_vendor_args("acme", {"config": {
        "shell_environment_policy.inherit": "all", "tui.notifications": True}}) == [
        "-c", "shell_environment_policy.inherit=all", "-c", "tui.notifications=true"]


def test_config_map_without_a_config_door_is_loud():
    with pytest.raises(ValueError):
        v.render_vendor_args("claude", {"config": {"a.b": 1}})


def test_nested_mapping_outside_the_config_door_is_loud():
    with pytest.raises(ValueError):
        v.render_vendor_args("claude", {"mcp": {"server": "x"}})


def test_dash_prefixed_keys_are_loud():
    with pytest.raises(ValueError) as excinfo:
        v.render_vendor_args("claude", {"--effort": "high"})
    assert "write `effort:`" in str(excinfo.value)


def test_per_key_annotation_matches_the_whole_section_rendering():
    """The assembly renders one key at a time for provenance; that is only
    honest while the rule carries no cross-key state."""
    section = {"effort": "high", "add-dir": ["/a", "/b"], "verbose": True,
               "quiet": False, "model": "opus"}
    per_key = [token for key, value in section.items()
               for token in v.render_vendor_args("claude", {key: value})]
    assert per_key == v.render_vendor_args("claude", section)


# ---------- merge and the passthrough edge ------------------------------------

def test_scalar_cli_flag_overrides_front_matter_default():
    argv = v.merge_argv([], v.render_vendor_args("claude", {"effort": "high"}),
                        ["--effort", "max"])
    assert argv == ["--effort", "high", "--effort", "max"]


def test_list_flags_accumulate_across_front_matter_and_cli():
    argv = v.merge_argv([], v.render_vendor_args("claude", {"add-dir": ["~/tasks"]}),
                        ["--add-dir", "~/scratch"])
    assert argv == ["--add-dir", "~/tasks", "--add-dir", "~/scratch"]


def test_merge_order_is_computed_then_front_matter_then_cli():
    assert v.merge_argv(["C"], ["F"], ["L"]) == ["C", "F", "L"]


@pytest.mark.parametrize("rest, split", [
    (["review", "the", "auth", "code"], (["review", "the", "auth", "code"], [])),
    (["--effort", "max", "hello"], ([], ["--effort", "max", "hello"])),
    (["fix", "the", "bug", "--chrome"], (["fix", "the", "bug"], ["--chrome"])),
    ([], ([], [])),
])
def test_split_rest(rest, split):
    assert v.split_rest(rest) == split


# ---------- the duplicate-door guard -----------------------------------------

def build_with_prompt(tmp_path, section, cli_args=(), text="ctx"):
    root = make_agents_root(tmp_path)
    agent = resolve_agent("scout", root=root)
    identity = SessionIdentity("sid-9", str(tmp_path), False, {})
    return build_vendor_argv("claude", section, agent=agent, identity=identity,
                             cli_args=list(cli_args), system_prompt_text=text,
                             directory=str(tmp_path))


def test_doors_are_found_by_table_field_name_not_by_flag_spelling(acme):
    doors = v.vendor_doors("claude")
    assert doors["--append-system-prompt-file"] == "system_prompt_file_door"
    assert doors["--session-id"] == "session_id_flag"
    assert doors["--agent"] == "identity_door"
    assert "--" not in doors and "config_door" not in doors.values()
    assert v.vendor_doors("acme") == {"-c": "config_door"}


def test_a_new_vendor_door_is_guarded_with_no_change_to_the_guard(monkeypatch):
    monkeypatch.setitem(v.VENDORS, "brief", dict(
        CONFIG_VENDOR, cmd="brief", config_door=None,
        system_prompt_door="--brief-flag-nobody-hardcoded"))
    with pytest.raises(ValueError) as excinfo:
        build_vendor_argv("brief", {"brief-flag-nobody-hardcoded": "mine"},
                          system_prompt_text="ctx")
    assert "--brief-flag-nobody-hardcoded" in str(excinfo.value)


def test_a_declared_system_prompt_door_refuses_rather_than_dropping_context(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        build_with_prompt(tmp_path, {"append-system-prompt-file": "/tmp/mine.md"})
    message = str(excinfo.value)
    assert "--append-system-prompt-file" in message
    assert "the launcher itself" in message
    assert "`claude:` section" in message
    assert "start: idle: true" in message and "--idle" in message


def test_cli_passthrough_is_treated_exactly_like_front_matter(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        build_with_prompt(tmp_path, {}, cli_args=["--append-system-prompt-file", "/x"])
    assert "CLI passthrough" in str(excinfo.value)


def test_both_sources_at_once_are_both_named(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        build_with_prompt(tmp_path, {"append-system-prompt-file": "/tmp/a.md"},
                          cli_args=["--append-system-prompt-file", "/tmp/b.md"])
    assert "`claude:` section" in str(excinfo.value)
    assert "CLI passthrough" in str(excinfo.value)


def test_the_equals_spelling_of_a_door_is_the_same_door(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        build_with_prompt(tmp_path, {}, cli_args=["--append-system-prompt-file=/tmp/m"])
    assert "--append-system-prompt-file" in str(excinfo.value)


def test_a_longer_flag_that_starts_with_a_door_is_not_that_door(tmp_path):
    argv = build_with_prompt(tmp_path, {}, cli_args=["--session-id-file=/tmp/x"], text="")
    assert "--session-id-file=/tmp/x" in argv
    assert argv.count("--session-id") == 1


def test_the_guard_covers_every_computed_door_not_just_the_prompt(tmp_path):
    for section, flag in (({"agent": "other"}, "--agent"),
                          ({"session-id": "sid-hijack"}, "--session-id")):
        with pytest.raises(ValueError) as excinfo:
            build_with_prompt(tmp_path, section, text="")
        assert flag in str(excinfo.value)
        assert "idle" not in str(excinfo.value)


def test_idle_frees_the_door_the_error_pointed_at(tmp_path):
    argv = build_with_prompt(tmp_path, {"append-system-prompt-file": "/tmp/mine.md"},
                             text="")
    assert argv.count("--append-system-prompt-file") == 1
    assert argv[argv.index("--append-system-prompt-file") + 1] == "/tmp/mine.md"


def test_an_unrelated_repeated_flag_is_still_the_merge_contract(tmp_path):
    argv = build_with_prompt(tmp_path, {"effort": "high"}, cli_args=["--effort", "max"])
    assert argv.index("high") < argv.index("max")


def test_a_door_the_launcher_never_computes_repeats_freely(acme):
    argv = build_vendor_argv("acme", {"config": {"model": "o3", "sandbox": "x"}},
                             cli_args=["-c", "approval=never"])
    assert argv.count("-c") == 3


def test_a_refused_assembly_writes_nothing(tmp_path):
    """The old launcher wrote the 0600 prompt file BEFORE the guard and had
    to discard it on refusal; the port writes nothing until execute."""
    with pytest.raises(ValueError):
        build_with_prompt(tmp_path, {"append-system-prompt-file": "/tmp/mine.md"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["agents"]


# ---------- the assembled argv -------------------------------------------------

def test_vendor_argv_pins_session_id_first_then_front_matter_then_cli(tmp_path):
    agent = resolve_agent("scout", root=make_agents_root(tmp_path))
    identity = SessionIdentity("sid-9", "/tmp/x", False, {})
    argv = build_vendor_argv("claude", {"effort": "high"}, agent=agent,
                             identity=identity, cli_args=["--effort", "max"],
                             initial_message="hello there")
    assert argv[0] == "claude"
    assert argv[1:3] == ["--session-id", "sid-9"]
    assert argv.index("high") < argv.index("max")
    assert argv[-2:] == ["--", "hello there"]
    assert argv[argv.index("--agent") + 1] == "scout"


def test_vendor_without_a_session_id_door_gets_no_flag(acme):
    argv = build_vendor_argv("acme", {}, initial_message="hi")
    assert "--session-id" not in argv
    assert argv[0] == "acme" and argv[-1] == "hi"


def test_an_inline_system_prompt_door_carries_the_text(acme):
    argv = build_vendor_argv("inline", {}, system_prompt_text="context text")
    assert argv[1:] == ["--append-system-prompt", "context text"]


def test_a_vendor_with_no_system_prompt_door_is_loud_not_silent(acme):
    with pytest.raises(ValueError) as excinfo:
        build_vendor_argv("acme", {}, system_prompt_text="context text")
    assert "acme" in str(excinfo.value)


def test_annotated_tokens_are_exactly_the_launch_argv(tmp_path):
    agent = Agent(name="scout", file="/x/scout.md", directory="", home="")
    identity = SessionIdentity("sid-1", str(tmp_path), False, {"CLAUDE_SESSION_ID": "sid-1"})
    kwargs = {"agent": agent, "identity": identity, "cli_args": ["--model", "opus"],
              "initial_message": "hi there", "system_prompt_text": "prompt text",
              "directory": str(tmp_path), "dry_run": True}
    annotated, _files = build_annotated_argv("claude", {"effort": "high", "add-dir": ["/a"]},
                                             **kwargs)
    argv = build_vendor_argv("claude", {"effort": "high", "add-dir": ["/a"]}, **kwargs)
    assert [token for token, _ in annotated] == argv
    sources = dict(annotated)
    assert sources["--session-id"].startswith("computed: session identity")
    assert sources["--effort"] == "front matter: claude: effort"
    assert sources["--model"] == "CLI passthrough"
    assert sources["claude"] == "computed: vendor table (claude)"
    assert sources["hi there"].startswith("computed: initial message")


def test_a_live_assembly_names_the_files_and_writes_none(tmp_path):
    md = tmp_path / "scout.md"
    md.write_text("---\nname: scout\n---\n")
    agent = Agent(name="scout", file=str(md), directory="", home="")
    identity = SessionIdentity("sid-1", str(tmp_path), False, {})
    annotated, files = build_annotated_argv(
        "claude", {}, agent=agent, identity=identity, initial_message="w" * 10,
        system_prompt_text="briefing", directory=str(tmp_path),
        first_message_file_over=4)
    argv = [token for token, _ in annotated]
    prompt, plugin, message = files
    assert isinstance(prompt, PlannedFile) and prompt.text == "briefing"
    assert argv[argv.index("--append-system-prompt-file") + 1] == prompt.path
    assert os.path.basename(prompt.path).startswith("session-start-system-prompt-")
    assert prompt.path.endswith(".md")
    assert isinstance(plugin, PlannedPlugin) and plugin.agent_file == str(md)
    assert os.path.basename(plugin.path).startswith("session-start-agent-plugin-")
    assert message.text == "w" * 10 and message.path in argv[-1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scout.md"]


# ---------- the first-message door ---------------------------------------------

def _pointer_path(pointer: str) -> str:
    import re
    return re.search(r" at (\S+) \(\d+ bytes\)", pointer).group(1)


def test_long_initial_message_goes_to_a_planned_0600_file(tmp_path):
    from sessiontools.start.context import materialize
    text = "w" * 1025
    annotated, files = build_annotated_argv(
        "claude", {}, initial_message=text, directory=str(tmp_path),
        first_message_file_over=1024)
    argv = [token for token, _ in annotated]
    assert argv[-2] == "--"
    assert text not in argv[-1]
    path = _pointer_path(argv[-1])
    # The pointer's WORDS stay short; the path is tmp_path's, as deep as TMPDIR
    # makes it, so it is measured apart (ST-119: a long TMPDIR broke `< 400`).
    assert len(argv[-1].replace(path, "", 1)) < 400
    assert path.startswith(str(tmp_path))
    assert "0600 file" in annotated[-1][1]
    materialize(files[0])
    assert Path(path).read_text() == text
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_short_initial_message_stays_inline(tmp_path):
    argv = build_vendor_argv("claude", {}, initial_message="hello there",
                             directory=str(tmp_path))
    assert argv[-2:] == ["--", "hello there"]


def test_long_initial_message_dry_run_names_the_representative_path(tmp_path):
    argv = build_vendor_argv("claude", {}, initial_message="w" * 1025,
                             directory=str(tmp_path), dry_run=True,
                             first_message_file_over=1024)
    assert _pointer_path(argv[-1]) == str(tmp_path / DRY_RUN_MESSAGE_NAME)
    assert not list(tmp_path.iterdir())


def test_first_message_file_over_default_is_very_large_and_inline(tmp_path):
    text = "w" * 20000
    assert build_vendor_argv("claude", {}, initial_message=text,
                             directory=str(tmp_path))[-1] == text
