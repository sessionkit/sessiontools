"""The agent file: front matter, who owns each key, the `start:` and
`environment:` schemas, and `load()`'s refusals. Ported from the old suite's
front-matter, environment-loader and context-loader sections."""

from __future__ import annotations

import os

import pytest
from start_support import make_agents_root

from sessiontools.start import agentfile as af
from sessiontools.start.agents import BARE_AGENT, resolve_agent
from sessiontools.start.context import ContextEntry
from sessiontools.start.environment import EMPTY_ENVIRONMENT, parse_environment
from sessiontools.start.errors import EXIT_USAGE, AuthoringError, LaunchError

# ---------- the loader: front matter split from body, partitioned ----------


def test_front_matter_splits_from_body():
    fm, body = af.parse_agent_markdown("---\nname: scout\nmodel: opus\n---\nYou are scout.\n")
    assert fm == {"name": "scout", "model": "opus"}
    assert body == "You are scout.\n"


def test_file_without_front_matter_is_all_body():
    assert af.parse_agent_markdown("Just a prompt.\n") == ({}, "Just a prompt.\n")


def test_unterminated_front_matter_is_loud():
    with pytest.raises(ValueError):
        af.parse_agent_markdown("---\nname: scout\nNo closing delimiter.\n")


def test_non_mapping_front_matter_is_loud():
    with pytest.raises(ValueError):
        af.parse_agent_markdown("---\n- a\n- list\n---\nbody\n")


def test_partition_native_vendor_start_and_environment():
    parts = af.partition_front_matter({
        "name": "scout", "model": "opus", "claude": {"effort": "high"},
        "start": {"idle": True}, "environment": {"variables": {"FOO": "bar"}}})
    assert parts.native == {"name": "scout", "model": "opus"}
    assert parts.vendors == {"claude": {"effort": "high"}}
    assert parts.start == {"idle": True}
    assert parts.environment == {"variables": {"FOO": "bar"}}


def test_a_dropped_vendors_section_is_native_now():
    """The port ships the claude row only: a `codex:` section is no vendor's
    and so sits with the native keys, which the launcher never renders."""
    assert af.partition_front_matter({"codex": {"x": 1}}).native == {"codex": {"x": 1}}


def test_empty_vendor_section_normalizes_to_mapping():
    parts = af.partition_front_matter({"claude": None, "start": None})
    assert parts.vendors == {"claude": {}}
    assert parts.start == {}


def test_non_mapping_vendor_section_is_loud():
    with pytest.raises(ValueError):
        af.partition_front_matter({"claude": ["effort", "high"]})


# ---------- `start:` ----------------------------------------------------------

def test_context_lists_parse_in_declared_order():
    start = af.parse_start({"converse": True, "context": {
        "system-prompt": ["taskmaster context --role $ROLE --tasks", "launch-info"],
        "initial": ["scout-briefing"]}})
    assert start.converse is True and start.idle is False
    assert start.context.system_prompt == ["taskmaster context --role $ROLE --tasks",
                                           "launch-info"]
    assert start.context.initial == ["scout-briefing"]


def test_absent_start_block_is_an_empty_start():
    assert af.parse_start({}) == af.EMPTY_START
    assert af.parse_start(None) == af.EMPTY_START
    assert af.EMPTY_START.context == af.EMPTY_CONTEXT


def test_an_unknown_start_key_is_loud():
    with pytest.raises(ValueError) as excinfo:
        af.parse_start({"convrse": True})
    assert "convrse" in str(excinfo.value)
    assert "unknown key(s) under `start:`: 'convrse' (known: idle, converse" in str(excinfo.value)


def test_an_unknown_context_key_is_loud():
    with pytest.raises(ValueError) as excinfo:
        af.parse_context({"system_prompt": ["x"]})
    assert "system_prompt" in str(excinfo.value)


def test_keyed_parses_into_start_options():
    assert af.parse_start({"keyed": True}).keyed is True
    assert af.parse_start({}).keyed is False


def test_a_context_list_must_be_a_list_because_order_is_the_semantic():
    with pytest.raises(ValueError):
        af.parse_context({"system-prompt": "launch-info"})


@pytest.mark.parametrize("bad", [[""], ["   "], [None], [["not", "a", "string"]]])
def test_context_entries_must_be_non_empty_command_strings(bad):
    with pytest.raises(ValueError):
        af.parse_context({"initial": bad})


@pytest.mark.parametrize("bad", ["maybe", 1, "true"])
def test_launcher_switches_must_be_real_booleans(bad):
    with pytest.raises(ValueError):
        af.parse_start({"idle": bad})


def test_a_switch_is_on_from_either_the_cli_or_the_front_matter():
    assert af.switch_enabled(False, False) is False
    assert af.switch_enabled(True, False) is True
    assert af.switch_enabled(False, True) is True
    assert af.switch_enabled(True, True) is True


def test_start_first_message_file_over_is_parsed_and_validated():
    assert af.parse_start({"first-message-file-over": 4096}).first_message_file_over == 4096
    assert af.parse_start({}).first_message_file_over == 131072
    for bad in ("big", -1, True):
        with pytest.raises(ValueError):
            af.parse_start({"first-message-file-over": bad})


def test_only_the_claude_vendor_is_supported():
    assert af.parse_start({"vendor": "claude"}).vendor == "claude"
    with pytest.raises(ValueError) as excinfo:
        af.parse_start({"vendor": "codex"})
    assert "known: claude" in str(excinfo.value)


@pytest.mark.parametrize("vendor", [["claude"], {"claude": True}])
def test_a_vendor_that_is_not_a_string_is_the_same_refusal(vendor):
    """Not a TypeError from an unhashable `in VENDORS` (the old launcher's)."""
    with pytest.raises(ValueError) as excinfo:
        af.parse_start({"vendor": vendor})
    assert "is not a supported vendor (known: claude)" in str(excinfo.value)


def test_agent_directory_expands_the_home_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert af.parse_start({"directory": "~/den"}).directory == str(tmp_path / "den")


def test_start_directory_must_be_a_path():
    with pytest.raises(ValueError) as caught:
        af.parse_start({"directory": ""})
    assert "start: directory:" in str(caught.value)


# ---------- `environment:` ------------------------------------------------------

def test_absent_environment_block_is_an_empty_environment():
    assert af.partition_front_matter({"name": "scout"}).environment == {}
    assert parse_environment({}) == EMPTY_ENVIRONMENT
    assert parse_environment(None) == EMPTY_ENVIRONMENT


def test_environment_parses_all_three_parts():
    env = parse_environment({
        "variables": {"VOICEMODE_VOICES": "af_sky", "COUNT": 3, "DEBUG": True},
        "files": ["~/.claude/agents/scout/.env", "./.voicemode.env"],
        "secrets": {"GITHUB_TOKEN": "pass:scout/services/github/pat/scout"}})
    assert env.variables == {"VOICEMODE_VOICES": "af_sky", "COUNT": "3", "DEBUG": "true"}
    assert env.files == ["~/.claude/agents/scout/.env", "./.voicemode.env"]
    assert env.secrets == {"GITHUB_TOKEN": "pass:scout/services/github/pat/scout"}


def test_a_mistyped_environment_key_is_loud():
    with pytest.raises(ValueError) as excinfo:
        parse_environment({"variable": {"FOO": "bar"}})
    assert "variable" in str(excinfo.value)


def test_files_must_be_a_list_because_order_is_the_semantic():
    with pytest.raises(ValueError):
        parse_environment({"files": "~/.env"})


def test_variable_values_must_be_scalars():
    with pytest.raises(ValueError):
        parse_environment({"variables": {"FOO": {"nested": 1}}})


def test_a_variable_with_no_value_is_refused_never_exported_as_none():
    with pytest.raises(ValueError) as excinfo:
        parse_environment({"variables": {"FOO": None}})
    assert "FOO" in str(excinfo.value)
    assert parse_environment({"variables": {"FOO": ""}}).variables == {"FOO": ""}


@pytest.mark.parametrize("bad", ["2FOO", "FOO BAR", "FOO;rm -rf /", ""])
def test_invalid_variable_names_are_refused(bad):
    with pytest.raises(ValueError):
        parse_environment({"variables": {bad: "x"}})


def test_invalid_secret_names_and_refs_are_refused_at_parse_time():
    with pytest.raises(ValueError):
        parse_environment({"secrets": {"FOO-BAR": "pass:a/b"}})
    for bad in ("pass", ":ref", "pass:"):
        with pytest.raises(ValueError):
            parse_environment({"secrets": {"TOKEN": bad}})


# ---------- load(): the whole file, every authoring error exit 2 --------------

def test_load_reads_and_validates_the_whole_file(tmp_path):
    root = make_agents_root(tmp_path, front_matter=(
        "name: scout\nclaude:\n  effort: high\nstart:\n  converse: true\n"
        "environment:\n  variables:\n    ROLE: worker\n"))
    loaded = af.load(resolve_agent("scout", root=root))
    assert loaded.native == {"name": "scout"}
    assert loaded.vendor_section == {"effort": "high"}
    assert loaded.start.converse is True
    assert loaded.environment.variables == {"ROLE": "worker"}


def test_a_plain_session_has_no_agent_file():
    assert af.load(BARE_AGENT) == af.EMPTY_AGENT_FILE


@pytest.mark.parametrize("front_matter, words", [
    ("start:\n  keyd: true\n", "keyd"),
    ("start:\n  idle: maybe\n", "start: idle:"),
    ("environment:\n  variable:\n    A: b\n", "variable"),
    ("claude: [a, b]\n", "section 'claude'"),
    # The old launcher met this one inside the Fable gate, as a traceback.
    ("claude:\n  --effort: high\n", "must not start with '-'"),
    ("claude:\n  mcp:\n    server: x\n", "nested mapping"),
])
def test_an_authoring_error_is_a_usage_refusal_naming_the_file(tmp_path, front_matter, words):
    root = make_agents_root(tmp_path, front_matter=front_matter)
    with pytest.raises(LaunchError) as excinfo:
        af.load(resolve_agent("scout", root=root))
    assert excinfo.value.code == EXIT_USAGE
    assert words in str(excinfo.value)
    assert os.path.join(root, "scout.md") in str(excinfo.value)


@pytest.mark.parametrize("text", [
    "---\nname: scout\nno closing delimiter\n",     # unterminated
    "---\n- a\n- list\n---\n",                      # not a mapping
    "---\nname: [unclosed\n---\n",                  # not YAML
])
def test_a_malformed_front_matter_is_exit_2_not_a_traceback(tmp_path, text):
    """DEVIATION from the old launcher, on purpose: it let these escape as a
    Python traceback, exit 1. The port refuses in its own words, exit 2."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "scout.md").write_text(text)
    with pytest.raises(LaunchError) as excinfo:
        af.load(resolve_agent("scout", root=str(tmp_path / "agents")))
    assert excinfo.value.code == EXIT_USAGE
    assert "scout.md" in str(excinfo.value)


# ---------- a context entry may declare its informational exits (ST-114) ------

def test_a_mapping_entry_declares_its_informational_exits():
    parsed = af.parse_context({"initial": [
        "launch-info", {"run": "pager status", "informational-exit": [3]}]})
    assert parsed.initial == ["launch-info",
                              ContextEntry("pager status", frozenset({3}))]


@pytest.mark.parametrize("entry", [{"run": "pager status"},
                                   {"run": "pager status", "informational-exit": []}])
def test_a_mapping_that_declares_no_code_is_its_command_string(entry):
    assert af.parse_context({"initial": [entry]}).initial == ["pager status"]


def test_a_mapping_entry_parses_from_yaml_front_matter():
    front_matter, _ = af.parse_agent_markdown(
        "---\nstart:\n  context:\n    system-prompt:\n"
        "      - run: pager status\n        informational-exit: [3, 4]\n---\n")
    assert af.parse_start(front_matter["start"]).context.system_prompt == [
        ContextEntry("pager status", frozenset({3, 4}))]


@pytest.mark.parametrize(("entry", "says"), [
    ({"run": "pager status", "informatonal-exit": [3]}, "informatonal-exit"),
    ({"informational-exit": [3]}, "run:"),
    ({"run": "", "informational-exit": [3]}, "run:"),
    ({"run": "   "}, "run:"),
    ({"run": ["pager", "status"]}, "run:"),
    ({"run": "pager status", "informational-exit": 3}, "LIST"),
    ({"run": "pager status", "informational-exit": None}, "LIST"),
    ({"run": "pager status", "informational-exit": "3"}, "LIST"),
    ({"run": "pager status", "informational-exit": [0]}, "1 to 255"),
    ({"run": "pager status", "informational-exit": [256]}, "1 to 255"),
    ({"run": "pager status", "informational-exit": [-1]}, "1 to 255"),
    ({"run": "pager status", "informational-exit": ["3"]}, "1 to 255"),
    ({"run": "pager status", "informational-exit": [3.0]}, "1 to 255"),
    ({"run": "pager status", "informational-exit": [True]}, "1 to 255"),
    ({"run": "pager status", "informational-exit": [False]}, "1 to 255"),
])
def test_a_misspelt_declaration_is_refused_naming_the_list(entry, says):
    with pytest.raises(AuthoringError) as excinfo:
        af.parse_context({"initial": [entry]})
    message = str(excinfo.value)
    assert "start: context: initial:" in message and says in message


def test_a_yaml_yes_is_a_bool_and_is_refused_as_an_exit_code():
    front_matter, _ = af.parse_agent_markdown(
        "---\nstart:\n  context:\n    initial:\n"
        "      - run: pager status\n        informational-exit: [yes]\n---\n")
    with pytest.raises(AuthoringError):
        af.parse_start(front_matter["start"])
