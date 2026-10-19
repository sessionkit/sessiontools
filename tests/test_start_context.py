"""Context: the generators, launch-info, idle, converse, and the doors'
files. Real subprocesses (`printf`, a failing command, a hang) - "runs the
agent's generators and survives their failures" is the claim. Ported from
the old suite's context and delivery sections."""

from __future__ import annotations

import json
import os
import stat

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
        now="Sunday, 2030-06-09 22:34:56 AEST", host="testhost")
    for fact in ("# Launch Info", "Sunday, 2030-06-09 22:34:56 AEST", "testhost",
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

def test_idle_injects_nothing_and_runs_no_generator(tmp_path):
    marker = tmp_path / "generator-ran"
    start = parse_start({"idle": True, "context": {
        "system-prompt": [f"touch {marker}; printf 'TASK BOARD\\n'", "launch-info"],
        "initial": ["printf 'BRIEFING\\n'"]}})
    injected = build(start, idle=True, cwd=str(tmp_path),
                     builtins=c.context_builtins(host="testhost"))
    assert injected.system_prompt == "" and injected.results == []
    assert not marker.exists(), "an idle launch ran a context generator"
    assert injected.initial.strip() == c.IDLE_BOOT_TRIGGER


def test_idle_still_carries_the_launch_only_signals():
    initial = build(EMPTY_START, idle=True, converse=True, message="stand by").initial
    for part in (c.IDLE_BOOT_TRIGGER, c.CONVERSE_SIGNAL, "stand by"):
        assert part in initial


def test_idle_from_the_cli_and_from_front_matter_agree():
    start = parse_start({"idle": True})
    assert (build(start, idle=switch_enabled(False, start.idle)).initial
            == build(EMPTY_START, idle=switch_enabled(True, False)).initial)


def test_converse_by_front_matter_tells_the_agent_it_can_speak():
    start = parse_start({"converse": True})
    initial = build(start, converse=switch_enabled(False, start.converse)).initial
    assert c.CONVERSE_SIGNAL in initial
    assert "converse tool" in initial and "mouth say" in initial


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
    assert argv == ["--plugin-dir", planned.path]
    root = c.materialize(planned)
    carried = os.path.join(root, "agents", "scout.md")
    with open(carried, encoding="utf-8") as handle:
        assert handle.read() == md.read_text(encoding="utf-8")
    assert not os.path.islink(carried)
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
    assert argv == ["--plugin-dir", os.path.join(str(tmp_path), "agent-plugin")]
    assert planned is None and list(tmp_path.iterdir()) == [md]


def test_every_vendor_declares_its_doors_explicitly():
    for vendor, table in VENDORS.items():
        for field in ("agent_plugin_dir_arg", "system_prompt_door", "system_prompt_file_door"):
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
