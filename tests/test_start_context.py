"""Context: the generators, launch-info, idle, converse, and the doors'
files. Real subprocesses (`printf`, a failing command, a hang) - "runs the
agent's generators and survives their failures" is the claim. Ported from
the old suite's context and delivery sections."""

from __future__ import annotations

import json
import os
import stat
import sys
import time

import pytest
from start_support import quiet

from sessiontools.start import context as c
from sessiontools.start.agentfile import (
    EMPTY_START,
    parse_agent_markdown,
    parse_start,
    switch_enabled,
)
from sessiontools.start.agents import Agent
from sessiontools.start.errors import EXIT_LAUNCH_FAILED, ContextRefused, LaunchError
from sessiontools.start.vendor import VENDORS


def build(start, **kwargs):
    kwargs.setdefault("announce", quiet)
    return c.build_injected_context(start, **kwargs)


def run(entry, **kwargs):
    kwargs.setdefault("announce", quiet)
    return c.run_context_entry(entry, **kwargs)


# ---------- ordered, with provenance -----------------------------------------

def test_two_entries_render_in_order_each_with_a_provenance_marker():
    start = parse_start({"context": {"system-prompt": [
        "printf '%s\\n' 'ROLE: FOREMAN'", "launch-info"]}})
    text = build(start, builtins=c.context_builtins(agent="scout", host="testhost")).system_prompt
    assert text.index("ROLE: FOREMAN") < text.index("# Launch Info")
    assert "<!-- source: printf '%s\\n' 'ROLE: FOREMAN' -->" in text
    assert "<!-- source: launch-info -->" in text
    assert text.index("<!-- source: printf") < text.index("ROLE: FOREMAN")


def test_a_generators_environment_is_the_one_it_was_given():
    result = run('printf "role=%s\\n" "$ROLE"', env={"ROLE": "FOREMAN", "PATH": "/usr/bin:/bin"})
    assert result.ok, result.error
    assert result.output.strip() == "role=FOREMAN"


def test_entries_run_sequentially_so_a_later_one_can_see_an_earlier_one(tmp_path):
    results = c.run_context_entries(
        ["printf 'first\\n' > made-by-entry-one", "cat made-by-entry-one"],
        cwd=str(tmp_path), announce=quiet)
    assert [r.ok for r in results] == [True, True]
    assert results[1].output.strip() == "first"


def test_the_marker_names_the_entry_exactly_as_the_agent_file_declared_it():
    entry = "taskmaster context --role $ROLE --tasks"
    rendered = c.render_context([c.ContextResult(entry, "board\n", None)])
    assert rendered.splitlines()[0] == f"<!-- source: {entry} -->"


# ---------- failures are visible, and the launch proceeds ---------------------

def test_a_failing_generator_leaves_a_trace_and_the_launch_proceeds():
    start = parse_start({"context": {"system-prompt": [
        "printf 'before\\n'", "printf 'boom\\n' >&2; exit 3", "printf 'after\\n'"]}})
    injected = build(start)
    text = injected.system_prompt
    assert "before" in text and "after" in text
    assert "FAILED" in text and "exited 3" in text and "boom" in text
    assert [r.ok for r in injected.results] == [True, False, True]


def test_a_failed_generators_partial_output_is_kept():
    result = run("printf 'half a briefing\\n'; exit 1")
    assert not result.ok and "half a briefing" in result.output
    rendered = c.render_context([result])
    assert "half a briefing" in rendered and "FAILED" in rendered


def test_a_command_that_does_not_exist_is_reported_not_crashed():
    result = run("no-such-context-generator-ca209")
    assert not result.ok and "exited 127" in result.error


def test_a_hung_generator_is_bounded_and_reported():
    result = run("sleep 30", timeout=1)
    assert not result.ok and "timed out" in result.error


def test_the_per_generator_cap_is_the_old_contract():
    assert c.CONTEXT_TIMEOUT_S == 60


def test_a_silent_generator_is_still_labelled():
    rendered = c.render_context([run("true")])
    assert "<!-- source: true -->" in rendered and "no output" in rendered


def test_a_broken_builtin_is_reported_rather_than_taking_the_launch_down():
    def explode():
        raise RuntimeError("the generator is broken")
    result = run("launch-info", builtins={"launch-info": explode})
    assert not result.ok and "the generator is broken" in result.error


def test_the_builtin_is_produced_in_process_not_shelled_out():
    result = run("launch-info", builtins=c.context_builtins(host="testhost"),
                 env={"PATH": ""})
    assert result.ok and "# Launch Info" in result.output


# ---------- the stderr line per generator (map section 8 q5) ------------------

def test_each_shelled_out_generator_is_announced_on_stderr_before_it_runs(capsys):
    """An agent's wake can take ~55 s; a dry run silent for a minute looks hung.
    One stderr line per shelled-out generator, BEFORE it runs; built-ins run
    in-process and say nothing; stdout is untouched."""
    start = parse_start({"context": {"system-prompt": ["launch-info", "printf 'x\\n'"],
                                     "initial": ["true"]}})
    c.build_injected_context(start, builtins=c.context_builtins(host="h"))
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == ["session start: context: running `printf 'x\\n'`",
                                "session start: context: running `true`"]


def test_idle_announces_nothing_because_nothing_runs(capsys):
    c.build_injected_context(parse_start({"context": {"initial": ["true"]}}), idle=True)
    assert capsys.readouterr().err == ""


# ---------- launch-info ----------------------------------------------------------

def test_launch_info_reports_the_facts_only_the_launcher_knows():
    block = c.generate_launch_info(
        agent="scout", session_id="0198-abcd", converse=False,
        command=["claude", "--agent", "scout", "--effort", "max"],
        now="Sunday, 2030-06-09 12:34:56 UTC", host="testhost")
    for fact in ("# Launch Info", "Sunday, 2030-06-09 12:34:56 UTC", "testhost",
                 "scout", "0198-abcd", "claude --agent scout --effort max"):
        assert fact in block


def test_launch_info_omits_a_fact_it_was_not_given():
    block = c.generate_launch_info(now="now", host="testhost")
    for absent in ("Session ID", "Agent", "Launched as"):
        assert absent not in block


def test_launch_info_states_the_voice_mode_either_way():
    assert "ACTIVE" in c.generate_launch_info(converse=True, now="n", host="h")
    assert "inactive" in c.generate_launch_info(converse=False, now="n", host="h")


# ---------- the first message ---------------------------------------------------

def test_the_briefing_precedes_the_user_prompt_in_the_first_message():
    start = parse_start({"context": {"initial": ["printf 'BRIEFING: three open\\n'"]}})
    initial = build(start, message="review the auth code").initial
    assert initial.index("BRIEFING") < initial.index("review the auth")


def test_a_user_prompt_with_no_initial_list_is_the_whole_first_message():
    assert build(EMPTY_START, message="ship it").initial.strip() == "ship it"


def test_no_context_and_no_prompt_means_nothing_is_injected():
    assert build(EMPTY_START) == c.InjectedContext("", "", [])


def test_the_two_lists_go_to_two_different_destinations():
    start = parse_start({"context": {"system-prompt": ["printf 'STANDING\\n'"],
                                     "initial": ["printf 'TURN-ONE\\n'"]}})
    injected = build(start)
    assert "STANDING" in injected.system_prompt and "STANDING" not in injected.initial
    assert "TURN-ONE" in injected.initial and "TURN-ONE" not in injected.system_prompt


# ---------- idle and converse --------------------------------------------------

FLEET_WORDS = ("agent MD", "startup manifest")

# The agent-file wording the converse line must not carry on a bare launch.
VOICE_FLEET_WORDS = ("letter", "VOICE line", "heard.state", "mouth", "Talk", "seat")


def test_idle_with_an_agent_file_injects_only_the_boot_line_and_runs_no_generator(tmp_path):
    marker = tmp_path / "generator-ran"
    start = parse_start({"idle": True, "context": {
        "system-prompt": [f"touch {marker}; printf 'TASK BOARD\\n'", "launch-info"],
        "initial": ["printf 'BRIEFING\\n'"]}})
    injected = build(start, idle=True, agent_file=True, cwd=str(tmp_path),
                     builtins=c.context_builtins(host="testhost"))
    assert injected.system_prompt == "" and injected.results == []
    assert not marker.exists(), "an idle launch ran a context generator"
    assert injected.initial == c.IDLE_BOOT_TRIGGER + "\n"


def test_idle_with_no_agent_file_injects_nothing(tmp_path):
    marker = tmp_path / "generator-ran"
    start = parse_start({"idle": True, "context": {
        "system-prompt": [f"touch {marker}"], "initial": [f"touch {marker}"]}})
    for case in (EMPTY_START, start):
        injected = build(case, idle=True, cwd=str(tmp_path))
        assert injected == c.InjectedContext("", "", [])
    assert not marker.exists(), "an idle launch ran a context generator"


def test_a_bare_idle_launch_is_neutral_by_default():
    # The keyword defaults to the bare case: a caller that says nothing
    # about an agent file never gets the boot line.
    initial = build(EMPTY_START, idle=True, converse=True, message="stand by").initial
    assert c.IDLE_BOOT_TRIGGER not in initial
    for word in FLEET_WORDS + VOICE_FLEET_WORDS:
        assert word not in initial


def test_bare_idle_still_carries_the_launch_only_signals_in_order():
    initial = build(EMPTY_START, idle=True, converse=True, message="stand by",
                    agent_file=False).initial
    assert initial == c.BARE_CONVERSE_SIGNAL + "\n\nstand by\n"


def test_bare_idle_carries_the_users_words_alone():
    initial = build(EMPTY_START, idle=True, message="stand by").initial
    assert initial == "stand by\n"


def test_agent_idle_still_carries_the_launch_only_signals_in_order():
    initial = build(EMPTY_START, idle=True, converse=True, message="stand by",
                    agent_file=True).initial
    assert initial == f"{c.IDLE_BOOT_TRIGGER}\n\n{c.CONVERSE_SIGNAL}\n\nstand by\n"


def test_idle_from_the_cli_and_from_front_matter_agree():
    start = parse_start({"idle": True})
    for agent_file in (True, False):
        assert (build(start, idle=switch_enabled(False, start.idle), agent_file=agent_file).initial
                == build(EMPTY_START, idle=switch_enabled(True, False),
                         agent_file=agent_file).initial)


def test_agent_file_does_not_change_a_non_idle_launch():
    start = parse_start({"context": {"initial": ["printf 'BRIEFING\\n'"]}})
    assert (build(start, message="go", agent_file=True)
            == build(start, message="go", agent_file=False))


def test_converse_by_front_matter_tells_the_agent_it_can_speak():
    # Front matter lives in an agent file, so this launch has one.
    start = parse_start({"converse": True})
    initial = build(start, converse=switch_enabled(False, start.converse),
                    agent_file=True).initial
    assert c.CONVERSE_SIGNAL in initial
    assert "converse tool" in initial and "mouth say" in initial


def test_a_bare_converse_launch_gets_the_neutral_voice_line():
    start = parse_start({"context": {"initial": ["printf 'BRIEFING\\n'"]}})
    for case in (EMPTY_START, start):
        initial = build(case, converse=True, message="go").initial
        assert c.BARE_CONVERSE_SIGNAL in initial
        assert c.CONVERSE_SIGNAL not in initial
        assert initial.endswith(c.BARE_CONVERSE_SIGNAL + "\n\ngo\n")
    assert build(EMPTY_START, converse=True, message="go").initial == (
        c.BARE_CONVERSE_SIGNAL + "\n\ngo\n")


def test_an_agent_file_converse_launch_keeps_the_fleet_voice_line():
    start = parse_start({"context": {"initial": ["printf 'BRIEFING\\n'"]}})
    initial = build(start, converse=True, message="go", agent_file=True).initial
    assert initial.endswith(f"BRIEFING\n\n{c.CONVERSE_SIGNAL}\n\ngo\n")
    assert c.BARE_CONVERSE_SIGNAL not in initial


def test_the_converse_signal_follows_the_agent_file_idle_or_not():
    for idle in (True, False):
        bare = build(EMPTY_START, idle=idle, converse=True).initial
        agent = build(EMPTY_START, idle=idle, converse=True, agent_file=True).initial
        assert c.BARE_CONVERSE_SIGNAL in bare and c.CONVERSE_SIGNAL not in bare
        assert c.CONVERSE_SIGNAL in agent and c.BARE_CONVERSE_SIGNAL not in agent
    assert c.converse_signal(True) == c.CONVERSE_SIGNAL
    assert c.converse_signal(False) == c.BARE_CONVERSE_SIGNAL


def test_the_bare_voice_line_names_the_tool_and_no_fleet_terms():
    line = c.BARE_CONVERSE_SIGNAL
    assert "AVAILABLE" in line and "can speak" in line
    assert "VoiceMode converse tool" in line
    for word in VOICE_FLEET_WORDS + FLEET_WORDS:
        assert word not in line
    for order in ("Do NOT type", "first response aloud", "ACTIVE"):
        assert order not in line


def test_the_voice_line_states_a_fact_and_the_letter_decides():
    # The line once said "speak your first response aloud. Do NOT type it."
    # - an order that overrode a letter saying stay silent, while the user
    # was asleep. A capability, not an order.
    assert "AVAILABLE" in c.CONVERSE_SIGNAL and "letter" in c.CONVERSE_SIGNAL
    for order in ("Do NOT type", "first response aloud", "ACTIVE"):
        assert order not in c.CONVERSE_SIGNAL


def test_the_speak_signal_appears_only_when_converse_was_requested():
    start = parse_start({"context": {"initial": ["printf 'BRIEF\\n'"]}})
    assert "speak" not in build(start, message="hello").initial.lower()
    assert "speak" not in build(EMPTY_START, idle=True).initial.lower()


# ---------- the doors' files -----------------------------------------------------

def test_claude_takes_the_system_prompt_by_its_file_door(tmp_path):
    text = "<!-- source: launch-info -->\n# Launch Info\n"
    argv, planned = c.system_prompt_argv("claude", text, directory=str(tmp_path))
    assert argv == ["--append-system-prompt-file", planned.path]
    assert text not in " ".join(argv)
    assert not os.path.exists(planned.path)          # planned, not written
    c.materialize(planned)
    with open(planned.path, encoding="utf-8") as handle:
        assert handle.read() == text
    assert stat.S_IMODE(os.stat(planned.path).st_mode) == 0o600


def test_an_empty_context_block_opens_no_door_at_all(tmp_path):
    assert c.system_prompt_argv("claude", "", directory=str(tmp_path)) == ([], None)


def test_a_dry_run_opens_the_door_against_a_representative_path(tmp_path):
    argv, planned = c.system_prompt_argv("claude", "briefing", directory=str(tmp_path),
                                         dry_run=True)
    assert argv == ["--append-system-prompt-file", str(tmp_path / c.DRY_RUN_PROMPT_NAME)]
    assert planned is None and list(tmp_path.iterdir()) == []


def test_a_planned_file_is_created_exclusively(tmp_path):
    """A name that already exists is a failure, never an overwrite (the
    mkstemp guarantee, kept now that the plan picks the name)."""
    path = tmp_path / "taken.md"
    path.write_text("someone else's")
    with pytest.raises(FileExistsError):
        c.write_private_file(str(path), "mine")
    assert path.read_text() == "someone else's"


def test_live_names_keep_the_old_mkstemp_shape(tmp_path):
    """A startup-report tool globs session-start-system-prompt-*.md."""
    names = {c.planned_path(str(tmp_path), c.PROMPT_PREFIX, ".md", "x", False)
             for _ in range(20)}
    assert len(names) == 20
    for name in names:
        base = os.path.basename(name)
        assert base.startswith("session-start-system-prompt-") and base.endswith(".md")


def test_claude_carries_the_agent_definition_in_a_plugin_dir(tmp_path):
    md = tmp_path / "scout.md"
    md.write_text("---\nname: scout\n---\nbody\n", encoding="utf-8")
    agent = Agent(name="scout", file=str(md), directory="", home="")
    argv, planned = c.agent_plugin_argv("claude", agent, directory=str(tmp_path))
    assert argv == ["--plugin-dir", planned.path, "--add-dir", planned.path]
    root = c.materialize(planned)
    for carried in (os.path.join(root, "agents", "scout.md"),
                    os.path.join(root, ".claude", "agents", "scout.md")):
        with open(carried, encoding="utf-8") as handle:
            assert handle.read() == md.read_text(encoding="utf-8")
        assert os.path.isfile(carried) and not os.path.islink(carried)
    with open(os.path.join(root, ".claude-plugin", "plugin.json")) as handle:
        assert json.load(handle)["name"] == "session-agent-scout"
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    c.discard(planned)
    assert not os.path.exists(root)


def test_a_plain_session_carries_no_definition(tmp_path):
    bare = Agent(name="", file="", directory="", home="")
    assert c.agent_plugin_argv("claude", bare, directory=str(tmp_path)) == ([], None)


def test_a_dry_run_names_the_plugin_dir_and_creates_nothing(tmp_path):
    md = tmp_path / "scout.md"
    md.write_text("---\nname: scout\n---\n", encoding="utf-8")
    agent = Agent(name="scout", file=str(md), directory="", home="")
    argv, planned = c.agent_plugin_argv("claude", agent, directory=str(tmp_path),
                                        dry_run=True)
    named = os.path.join(str(tmp_path), "agent-plugin")
    assert argv == ["--plugin-dir", named, "--add-dir", named]
    assert planned is None and list(tmp_path.iterdir()) == [md]


def test_the_add_dir_names_the_carrier_dir_only(tmp_path):
    """The add-dir grants file access: the carrier dir, never the launch dir
    the prompt and first-message files sit in."""
    md = tmp_path / "scout.md"
    md.write_text("---\nname: scout\n---\n", encoding="utf-8")
    agent = Agent(name="scout", file=str(md), directory="", home="")
    argv, planned = c.agent_plugin_argv("claude", agent, directory=str(tmp_path))
    added = argv[argv.index("--add-dir") + 1]
    assert added == planned.path != str(tmp_path)
    assert os.path.basename(added).startswith(c.PLUGIN_PREFIX)
    root = c.materialize(planned)
    assert sorted(os.listdir(root)) == [".claude", ".claude-plugin", "agents"]
    assert os.listdir(os.path.join(root, ".claude")) == ["agents"]
    c.discard(planned)


def test_a_vendor_without_an_add_dir_flag_gets_the_plugin_half_alone(tmp_path, monkeypatch):
    monkeypatch.setitem(VENDORS["claude"], "agent_add_dir_arg", None)
    md = tmp_path / "scout.md"
    md.write_text("---\nname: scout\n---\n", encoding="utf-8")
    agent = Agent(name="scout", file=str(md), directory="", home="")
    argv, _ = c.agent_plugin_argv("claude", agent, directory=str(tmp_path), dry_run=True)
    assert argv == ["--plugin-dir", os.path.join(str(tmp_path), "agent-plugin")]


def test_a_failed_carrier_write_leaves_nothing_behind(tmp_path):
    agent = Agent(name="scout", file=str(tmp_path / "gone.md"), directory="", home="")
    _argv, planned = c.agent_plugin_argv("claude", agent, directory=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        c.materialize(planned)
    assert not os.path.exists(planned.path)


# ---------- the add-dir copy drops what only an add-dir agent honours ---------

def _carry(tmp_path, source: bytes) -> tuple[bytes, bytes]:
    """(plugin copy, add-dir copy) of an agent file holding `source`."""
    md = tmp_path / "scout.md"
    md.write_bytes(source)
    agent = Agent(name="scout", file=str(md), directory="", home="")
    _argv, planned = c.agent_plugin_argv("claude", agent, directory=str(tmp_path))
    root = c.materialize(planned)
    try:
        with open(os.path.join(root, "agents", "scout.md"), "rb") as handle:
            plugin = handle.read()
        with open(os.path.join(root, ".claude", "agents", "scout.md"), "rb") as handle:
            add_dir = handle.read()
    finally:
        c.discard(planned)
    return plugin, add_dir


def test_the_dropped_keys_are_the_three_only_an_add_dir_agent_honours():
    assert c.ADD_DIR_DROPPED_KEYS == ("hooks", "mcpServers", "permissionMode")


@pytest.mark.parametrize("line", [
    b"permissionMode: bypassPermissions\n",
    b"mcpServers: {voice: {command: speak}}\n",
    b"hooks: {}\n",
])
def test_each_key_is_left_out_of_the_add_dir_copy_alone(tmp_path, line):
    source = b"---\nname: scout\n" + line + b"model: haiku\n---\nbody\n"
    plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == b"---\nname: scout\nmodel: haiku\n---\nbody\n"
    assert plugin == source


def test_all_three_go_and_everything_else_stays_byte_for_byte(tmp_path):
    source = (b"---\n"
              b"# the scout\n"
              b"name: scout\n"
              b"permissionMode: acceptEdits\n"
              b"description: looks around   # trailing comment\n"
              b"hooks:\n"
              b"  Stop:\n"
              b"    - hooks:\n"
              b"        - type: command\n"
              b"          command: echo done\n"
              b"\n"
              b"tools: Read, Grep\n"
              b"mcpServers:\n"
              b"  voice:\n"
              b"    command: speak\n"
              b"    args: [--quiet]\n"
              b"model: haiku\n"
              b"---\n"
              b"\n"
              b"The body.\n")
    plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == (b"---\n"
                       b"# the scout\n"
                       b"name: scout\n"
                       b"description: looks around   # trailing comment\n"
                       b"\n"
                       b"tools: Read, Grep\n"
                       b"model: haiku\n"
                       b"---\n"
                       b"\n"
                       b"The body.\n")
    assert plugin == source


def test_a_nested_hooks_block_goes_whole_even_across_a_blank_line(tmp_path):
    source = (b"---\nname: scout\nhooks:\n  Stop:\n\n# inside\n"
              b"    - command: x\n  PreToolUse: []\nmodel: haiku\n---\n")
    _plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == b"---\nname: scout\nmodel: haiku\n---\n"


def test_a_block_sequence_at_column_zero_goes_with_its_key(tmp_path):
    source = b"---\nname: scout\nmcpServers:\n- voice\n- search\nmodel: haiku\n---\n"
    _plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == b"---\nname: scout\nmodel: haiku\n---\n"


def test_a_dropped_key_at_the_end_keeps_the_blank_line_before_the_fence(tmp_path):
    source = b"---\nname: scout\npermissionMode: default\n\n---\nbody"
    _plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == b"---\nname: scout\n\n---\nbody"


def test_crlf_line_endings_and_a_missing_final_newline_survive(tmp_path):
    source = b"---\r\nname: scout\r\nhooks:\r\n  Stop: []\r\nmodel: haiku\r\n---\r\nbody"
    _plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == b"---\r\nname: scout\r\nmodel: haiku\r\n---\r\nbody"


def test_a_quoted_key_is_still_the_key(tmp_path):
    source = b"---\nname: scout\n\"permissionMode\": bypassPermissions\n---\n"
    _plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == b"---\nname: scout\n---\n"


def test_a_dot_dot_dot_closing_fence_is_honoured(tmp_path):
    source = b"---\nname: scout\nhooks: {}\n...\npermissionMode: x\n"
    _plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == b"---\nname: scout\n...\npermissionMode: x\n"


@pytest.mark.parametrize("source", [
    b"no front matter at all\npermissionMode: bypassPermissions\n",
    b"---\nname: scout\nmodel: haiku\n---\nbody\n",
    b"---\nname: scout\nhooksExtra: 1\nmcpServersOld: {}\npermissionModes: [a]\n---\n",
    b"---\nname: scout\n  hooks: indented under nothing\n---\n",
    b"---\nname: scout\n# hooks: a comment\n---\n",
    b"---\nname: scout\n---\npermissionMode: bypassPermissions\nhooks:\n  Stop: []\n",
    b"---\nname: scout\npermissionMode: bypassPermissions\n",
    b"",
])
def test_a_file_with_none_of_the_keys_in_its_front_matter_is_copied_byte_for_byte(
        tmp_path, source):
    plugin, add_dir = _carry(tmp_path, source)
    assert plugin == source
    assert add_dir == source


def test_bytes_that_are_not_utf8_pass_through_untouched(tmp_path):
    source = b"---\nname: scout\ndescription: caf\xe9\nhooks: {}\n---\nbody \xff\n"
    _plugin, add_dir = _carry(tmp_path, source)
    assert add_dir == b"---\nname: scout\ndescription: caf\xe9\n---\nbody \xff\n"


def test_the_filtered_copy_still_parses_to_the_same_other_keys(tmp_path):
    source = (b"---\nname: scout\nhooks:\n  Stop:\n    - command: x\n"
              b"permissionMode: bypassPermissions\nmodel: haiku\n---\nbody\n")
    _plugin, add_dir = _carry(tmp_path, source)
    before, body = parse_agent_markdown(source.decode())
    after, body_after = parse_agent_markdown(add_dir.decode())
    for key in c.ADD_DIR_DROPPED_KEYS:
        before.pop(key, None)
    assert after == before and body_after == body


def test_every_vendor_declares_its_doors_explicitly():
    for vendor, table in VENDORS.items():
        for field in ("agent_plugin_dir_arg", "agent_add_dir_arg", "system_prompt_door",
                      "system_prompt_file_door"):
            assert field in table, (vendor, field)
            assert table[field] is None or isinstance(table[field], str)


# ---------- an exit the agent file declares informational (ST-114) -----------

# The SM-706 shape after its Change A: a true "not yet" line on stdout, exit 3
# (the pager's documented quiet). No fixture here declares 2 ("could not
# look" is a real fault, and FAILED is right for it); "another code" is 4.
SM706 = "printf 'not yet - not deaf\\n'; exit 3"
NOTE_TAIL = ("This agent file declares exit 3 informational for this "
             "generator, so this is its answer, not a failure.")


def failed_block(source, reason, output):
    """Today's FAILED block, spelled out: SC2/R3 compare against THIS text."""
    return (f"<!-- source: {source} -->\n"
            f"CONTEXT GENERATOR FAILED: `{source}` {reason}. This section is "
            "missing or incomplete -- do not read its absence as an empty "
            f"result.\n{output}\n")


def test_a_declared_exit_renders_a_note_not_a_failure():
    result = run(c.ContextEntry(SM706, frozenset({3})))
    assert result.ok and result.error is None
    rendered = c.render_context([result])
    assert rendered == (f"<!-- source: {SM706} -->\n"
                        f"CONTEXT GENERATOR NOTE: `{SM706}` exited 3. {NOTE_TAIL}\n"
                        "not yet - not deaf\n")
    assert "FAILED" not in rendered and "missing or incomplete" not in rendered


def test_the_note_keeps_the_last_stderr_line():
    command = "printf 'answer\\n'; printf 'one\\nlast word\\n' >&2; exit 3"
    rendered = c.render_context([run(c.ContextEntry(command, frozenset({3})))])
    assert (f"CONTEXT GENERATOR NOTE: `{command}` exited 3 -- last word. "
            f"{NOTE_TAIL}\nanswer\n") in rendered


def test_a_declared_exit_with_no_stdout_renders_the_header_alone():
    command = "printf 'only on stderr\\n' >&2; exit 3"
    rendered = c.render_context([run(c.ContextEntry(command, frozenset({3})))])
    assert rendered == (f"<!-- source: {command} -->\n"
                        f"CONTEXT GENERATOR NOTE: `{command}` exited 3 -- "
                        f"only on stderr. {NOTE_TAIL}\n")
    assert "no output" not in rendered


def test_an_undeclared_exit_is_todays_failed_block_exactly():
    expected = failed_block(SM706, "exited 3", "not yet - not deaf")
    assert c.render_context([run(SM706)]) == expected
    assert c.render_context([run(c.ContextEntry(SM706, frozenset({4})))]) == expected


def test_a_timeout_on_a_declared_entry_is_still_a_failure():
    result = run(c.ContextEntry("sleep 30", frozenset({3})), timeout=1)
    assert not result.ok and result.note is None
    assert c.render_context([result]) == (
        "<!-- source: sleep 30 -->\nCONTEXT GENERATOR FAILED: `sleep 30` timed out "
        "after 1s. This section is missing or incomplete -- do not read its "
        "absence as an empty result.\n")


def test_a_broken_builtin_declared_informational_is_still_a_failure():
    def explode():
        raise RuntimeError("broken")
    result = run(c.ContextEntry("launch-info", frozenset({1})),
                 builtins={"launch-info": explode})
    assert not result.ok and result.note is None and "broken" in result.error


def test_a_declared_entry_that_exits_zero_is_plain_success():
    command = "printf 'all good\\n'"
    declared = c.render_context([run(c.ContextEntry(command, frozenset({3})))])
    assert declared == c.render_context([run(command)])
    assert "NOTE" not in declared


def test_a_declared_entry_with_no_codes_is_its_command_string():
    for command in (SM706, "printf 'fine\\n'", "true"):
        assert (c.render_context([run(c.ContextEntry(command))])
                == c.render_context([run(command)]))


def test_a_declared_entry_is_announced_and_labelled_by_its_command(capsys):
    result = c.run_context_entry(c.ContextEntry(SM706, frozenset({3})))
    assert result.source == SM706
    assert capsys.readouterr().err == f"session start: context: running `{SM706}`\n"


def test_a_declared_entry_reaches_a_builtin_by_its_command():
    result = run(c.ContextEntry("launch-info", frozenset({3})),
                 builtins={"launch-info": lambda: "built in\n"})
    assert result == c.ContextResult("launch-info", "built in\n", None)


def test_the_sm706_shape_end_to_end_from_the_agent_file():
    front_matter, _ = parse_agent_markdown(
        "---\n"
        "start:\n"
        "  context:\n"
        "    initial:\n"
        "      - printf 'briefing\\n'\n"
        "      - run: sh -c \"echo not yet - not deaf; exit 3\"\n"
        "        informational-exit: [3]\n"
        "---\n")
    injected = build(parse_start(front_matter["start"]))
    source = 'sh -c "echo not yet - not deaf; exit 3"'
    assert injected.initial == (
        "<!-- source: printf 'briefing\\n' -->\nbriefing\n\n"
        f"<!-- source: {source} -->\n"
        f"CONTEXT GENERATOR NOTE: `{source}` exited 3. {NOTE_TAIL}\n"
        "not yet - not deaf\n")
    assert [r.ok for r in injected.results] == [True, True]


# ---------- a REQUIRED entry that fails stops the run (ST-307) ----------------

def required(command, codes=()):
    return c.ContextEntry(command, frozenset(codes), True)


def test_a_failing_required_entry_stops_the_run_and_keeps_its_streams(tmp_path):
    entries = ["printf 'one\\n'",
               required("printf 'wake half\\n'; printf 'a\\nwhy it broke\\n' >&2; exit 1"),
               "touch later-ran"]
    results = c.run_context_entries(entries, cwd=str(tmp_path), announce=quiet)
    assert len(results) == 2
    assert not (tmp_path / "later-ran").exists()
    failed = results[-1]
    assert failed.required and not failed.ok
    assert failed.output == "wake half\n"
    assert failed.stderr == "a\nwhy it broke\n"
    assert failed.error == "exited 1 -- why it broke"
    assert c.stopped_on_required(results)


def test_a_required_entry_that_succeeds_does_not_stop(tmp_path):
    entries = [required("printf 'fine\\n'"), "touch later-ran"]
    results = c.run_context_entries(entries, cwd=str(tmp_path), announce=quiet)
    assert [r.ok for r in results] == [True, True]
    assert results[0].required and not results[1].required
    assert (tmp_path / "later-ran").exists()
    assert not c.stopped_on_required(results)


def test_a_failing_unmarked_entry_does_not_stop(tmp_path):
    results = c.run_context_entries(["exit 1", "touch later-ran"], cwd=str(tmp_path),
                                    announce=quiet)
    assert [r.ok for r in results] == [False, True]
    assert (tmp_path / "later-ran").exists()


def test_a_required_entrys_declared_informational_exit_does_not_stop(tmp_path):
    entries = [required(SM706, {3}), "touch later-ran"]
    results = c.run_context_entries(entries, cwd=str(tmp_path), announce=quiet)
    assert results[0].ok and results[0].note is not None
    assert (tmp_path / "later-ran").exists()


def test_a_required_builtin_that_raises_stops_the_run(tmp_path):
    def explode():
        raise RuntimeError("no clock")
    results = c.run_context_entries([required("launch-info"), "touch later-ran"],
                                    cwd=str(tmp_path), announce=quiet,
                                    builtins={"launch-info": explode})
    assert len(results) == 1 and not results[0].ok and results[0].required
    assert "no clock" in results[0].error
    assert not (tmp_path / "later-ran").exists()


def test_a_required_system_prompt_failure_refuses_and_runs_no_initial_entry(tmp_path):
    start = parse_start({"context": {
        "system-prompt": ["printf 'x\\n' >&2; exit 2",
                          {"run": "printf 'wake\\n'; exit 1", "required": True},
                          "touch later-sp"],
        "initial": ["touch later-initial"]}})
    with pytest.raises(ContextRefused) as excinfo:
        build(start, cwd=str(tmp_path))
    refused = excinfo.value
    assert isinstance(refused, LaunchError) and refused.code == EXIT_LAUNCH_FAILED == 1
    assert refused.door == "system-prompt"
    assert refused.position == (2, 3)
    assert refused.result.source == "printf 'wake\\n'; exit 1"
    assert refused.result.output == "wake\n"
    assert [r.source for r in refused.earlier] == ["printf 'x\\n' >&2; exit 2"]
    assert str(refused) == "required context generator `printf 'wake\\n'; exit 1` exited 1"
    assert not (tmp_path / "later-sp").exists()
    assert not (tmp_path / "later-initial").exists()


def test_a_required_initial_failure_refuses_naming_the_initial_door(tmp_path):
    """design-001 review F3: the marking works through the other door too."""
    start = parse_start({"context": {
        "system-prompt": ["printf 'ok\\n'", "exit 4"],
        "initial": ["exit 5",
                    {"run": "printf 'brief\\n' >&2; exit 1", "required": True},
                    "touch later-initial"]}})
    with pytest.raises(ContextRefused) as excinfo:
        build(start, cwd=str(tmp_path))
    refused = excinfo.value
    assert refused.door == "initial"
    assert refused.position == (2, 3)
    assert refused.result.stderr == "brief\n"
    assert [r.source for r in refused.earlier] == ["exit 4", "exit 5"]
    assert not (tmp_path / "later-initial").exists()


def test_idle_runs_nothing_so_a_required_entry_cannot_refuse_it():
    start = parse_start({"context": {"system-prompt": [
        {"run": "exit 1", "required": True}]}})
    assert build(start, idle=True).results == []


def test_a_launch_with_a_passing_required_entry_builds_as_usual():
    start = parse_start({"context": {"system-prompt": [
        {"run": "printf 'wake\\n'", "required": True}]}})
    assert build(start).system_prompt == "<!-- source: printf 'wake\\n' -->\nwake\n"


# ---------- a timeout: partial output, kept only when required (ST-307) --------

def test_a_required_timeout_keeps_its_partial_stdout_and_stderr():
    result = run(required("printf 'partial\\n'; printf 'err so far\\n' >&2; sleep 30"),
                 timeout=1)
    assert not result.ok and result.error == "timed out after 1s"
    assert result.output == "partial\n"
    assert result.stderr == "err so far\n"


def test_a_required_timeout_keeps_output_with_no_final_newline():
    result = run(required("printf 'no newline'; exec sleep 30"), timeout=1)
    assert result.output == "no newline"


def test_a_required_timeout_that_wrote_nothing_has_empty_output():
    result = run(required("sleep 30"), timeout=1)
    assert result.output == "" and result.stderr == ""
    assert not result.ok


def test_a_required_timeout_stops_the_run(tmp_path):
    results = c.run_context_entries([required("sleep 30"), "touch later-ran"],
                                    cwd=str(tmp_path), announce=quiet, timeout=1)
    assert len(results) == 1 and "timed out" in results[0].error
    assert not (tmp_path / "later-ran").exists()


def test_a_grandchild_holding_the_pipe_does_not_hang_the_timeout():
    began = time.monotonic()
    result = run(required("printf 'partial\\n'; (sleep 5; echo late) & sleep 5"),
                 timeout=1)
    assert time.monotonic() - began < 4
    assert result.output == "partial\n" and "timed out" in result.error


def test_an_unmarked_timeout_keeps_no_output_as_today():
    result = run("printf 'partial\\n'; sleep 30", timeout=1)
    assert result.output == "" and not result.required


def test_an_unmarked_timeouts_failed_block_is_pinned_byte_for_byte():
    """design §6: any later change to the timeout branch shows as a diff here.
    Unmarked and `required: false` render the same, partial output dropped."""
    command = "printf 'partial\\n'; sleep 30"
    expected = (
        f"<!-- source: {command} -->\n"
        f"CONTEXT GENERATOR FAILED: `{command}` timed out after 1s. This section "
        "is missing or incomplete -- do not read its absence as an empty "
        "result.\n")
    assert c.render_context([run(command, timeout=1)]) == expected
    assert c.render_context([run(c.ContextEntry(command, frozenset(), False),
                                 timeout=1)]) == expected


def test_render_ignores_the_new_fields():
    plain = c.ContextResult("x", "out\n", "exited 1")
    marked = c.ContextResult("x", "out\n", "exited 1", required=True, stderr="why\n")
    assert c.render_context([plain]) == c.render_context([marked])


def test_an_unmarked_file_runs_to_the_same_injected_context_as_required_false():
    """The opt-in proof at run level: failing and timing-out entries, unmarked
    vs `required: false` on each, give identical injected text."""
    commands = ["printf 'a\\n'", "printf 'b\\n'; exit 3", "printf 'c\\n'; sleep 30"]
    unmarked = parse_start({"context": {"system-prompt": commands,
                                        "initial": commands}})
    marked_false = parse_start({"context": {
        "system-prompt": [{"run": x, "required": False} for x in commands],
        "initial": [{"run": x, "required": False} for x in commands]}})
    assert unmarked == marked_false
    one, two = build(unmarked, timeout=1), build(marked_false, timeout=1)
    assert (one.system_prompt, one.initial) == (two.system_prompt, two.initial)
    assert [r.ok for r in one.results] == [True, False, False] * 2


# ---------- a timeout kills the whole process group (ST-317) -------------------

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # it exists, owned by someone else
        return True
    return True


def _gone_within(pid: int, seconds: float) -> bool:
    """Poll briefly: a killed sleeper is reparented to init, which reaps it
    a moment later; until then `kill(pid, 0)` still finds the zombie."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


@pytest.mark.parametrize("make", [lambda cmd: cmd, required],
                         ids=["unmarked", "required"])
def test_a_timed_out_generators_grandchildren_are_killed_with_it(tmp_path, make):
    """The shell is the direct child; the sleeper it forks is a grandchild.
    Killing only the shell left the sleeper running (ST-317): the command
    runs in its own process group, and a timeout kills the GROUP."""
    pidfile = tmp_path / "sleeper.pid"
    command = f"sleep 300 & echo $! > {pidfile}; printf 'partial\\n'; wait"
    pid = None
    try:
        result = run(make(command), timeout=1)
        pid = int(pidfile.read_text().strip())
        assert result.error == "timed out after 1s"
        assert _gone_within(pid, 5), f"sleeper pid {pid} outlived the timeout"
    finally:
        if pid is None and pidfile.exists():
            pid = int(pidfile.read_text().strip())
        if pid is not None and _alive(pid):
            os.kill(pid, 9)  # by its exact pid: the test never leaks it


@pytest.fixture
def launcher_stdin_with_data():
    """fd 0 of THIS process is a pipe holding a line, for the length of the
    test. pytest's own capture leaves /dev/null on fd 0, which would make a
    stdin test pass with or without the fix; a planted pipe tells them apart."""
    read_end, write_end = os.pipe()
    os.write(write_end, b"leaked from the launcher\n")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    os.close(read_end)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)


def test_a_generators_stdin_is_dev_null_not_the_launchers(launcher_stdin_with_data):
    """A generator in its own session inherited the launcher's stdin; with a
    pty slave there, the launcher's own later stdout write failed EIO (exit
    120, ST-317 fix-002). Its stdin is /dev/null, so it holds no terminal."""
    probe = ("import os; s = os.fstat(0); n = os.stat(os.devnull); "
             "print((s.st_dev, s.st_ino, s.st_rdev) == (n.st_dev, n.st_ino, n.st_rdev))")
    result = run(f"{sys.executable} -c \"{probe}\"", timeout=10)
    assert result.ok and result.output.strip() == "True"


def test_a_generator_that_reads_stdin_sees_eof_not_the_launchers_input(
        launcher_stdin_with_data):
    result = run("cat; printf 'after\\n'", timeout=10)
    assert result.ok and result.output == "after\n"


def test_a_required_timeout_still_keeps_its_partial_output_after_a_group_kill(tmp_path):
    pidfile = tmp_path / "sleeper.pid"
    command = (f"sleep 300 & echo $! > {pidfile}; printf 'partial\\n'; "
               "printf 'err so far\\n' >&2; wait")
    try:
        result = run(required(command), timeout=1)
        assert result.output == "partial\n" and result.stderr == "err so far\n"
    finally:
        if pidfile.exists():
            pid = int(pidfile.read_text().strip())
            if _alive(pid):
                os.kill(pid, 9)


def test_a_pipe_held_outside_the_group_does_not_hang_the_drain(tmp_path):
    """A process that left the group (its own setsid) survives the group
    kill and still holds stdout: the post-kill drain is bounded, and what
    was written before the timeout is kept."""
    pidfile = tmp_path / "escaper.pid"
    escaper = (f"import os, time; os.setsid(); open({str(pidfile)!r}, 'w')"
               f".write(str(os.getpid())); time.sleep(30)")
    command = (f"printf 'partial\\n'; {sys.executable} -c \"{escaper}\" & "
               "sleep 30")
    began = time.monotonic()
    try:
        result = run(required(command), timeout=1)
        took = time.monotonic() - began
        assert took < 1 + c.DRAIN_AFTER_KILL_S + 2
        assert result.output == "partial\n" and result.error == "timed out after 1s"
    finally:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if pidfile.exists() and pidfile.read_text().strip():
            pid = int(pidfile.read_text().strip())
            if _alive(pid):
                os.kill(pid, 9)
