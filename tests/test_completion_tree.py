"""`sessiontools.completion.tree()`: the verb tree shell completion is built
from, read from the dispatcher's own parsers and nothing else."""

from __future__ import annotations

import argparse
import subprocess
import sys

import pytest

from sessiontools import busy, cli, completion, get, paths, settings, verbs
from sessiontools.completion import Flag, Node


@pytest.fixture(scope="module")
def root() -> Node:
    return completion.tree()


def at(root: Node, *path: str) -> Node:
    n = root
    for word in path:
        n = n.subs[word]
    return n


def flags(n: Node) -> set[str]:
    return {f.name for f in n.flags}


def flag(n: Node, name: str) -> Flag:
    return next(f for f in n.flags if f.name == name)


# --- the top level is the dispatcher's table -------------------------------------------------

def test_the_verbs_are_exactly_the_handlers_in_order(root):
    assert list(root.subs) == list(cli.HANDLERS)
    assert root.first_words() == tuple(cli.HANDLERS)
    assert flags(root) == {"--help"}


def test_mail_and_the_disabled_verbs_are_never_offered(root):
    assert "mail" not in root.subs
    for verb in cli.DISABLED:
        assert verb not in root.subs


# --- the card's named sub-verbs ---------------------------------------------------------------

@pytest.mark.parametrize("path,subs", [
    (("sound", "engine"), {"build", "on", "off", "status"}),
    (("account",), {"list", "status", "use"}),
    (("pane-state",), {"show", "watch", "agent"}),
    (("pane-state", "agent"), {"on", "off", "status"}),
    (("statusline",), {"install", "check", "sample"}),
    (("default-model",), {"show", "list", "enforce", "watch", "unit"}),
])
def test_the_named_sub_verbs_are_offered(root, path, subs):
    assert subs <= set(at(root, *path).subs)


def test_default_model_unit_offers_its_two_kinds(root):
    assert set(at(root, "default-model", "unit").choices_at(0)) == {"launchd", "systemd"}


def test_sound_engine_is_a_sub_verb_of_sound(root):
    assert list(at(root, "sound").subs) == ["engine"]


# --- derived from the parsers, never a hand list or --help text -------------------------------

def _sources():
    for verb, ref in cli.PARSERS.items():
        src = verbs.resolve(ref)
        if isinstance(src, verbs.Group):
            if src.rest is not None:
                yield (verb,), verbs.resolve(src.rest)()
            for sub, sub_ref in src.subs.items():
                yield (verb, sub), verbs.resolve(sub_ref)()
        else:
            yield (verb,), src()


def _walk(path, parser):
    """Every (path, parser) under PARSER, through its subparsers."""
    yield path, parser
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            for sub, sp in a.choices.items():
                yield from _walk((*path, sub), sp)


@pytest.mark.parametrize("path,parser", [pp for src in _sources() for pp in _walk(*src)],
                         ids=lambda v: " ".join(v) if isinstance(v, tuple) else "")
def test_every_parser_long_flag_is_offered_at_its_path(root, path, parser):
    longs = {o for a in parser._actions for o in a.option_strings if o.startswith("--")}
    assert longs <= flags(at(root, *path))


@pytest.mark.parametrize("path,parser", [pp for src in _sources() for pp in _walk(*src)],
                         ids=lambda v: " ".join(v) if isinstance(v, tuple) else "")
def test_every_sub_verb_a_parser_accepts_is_offered(root, path, parser):
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            assert set(a.choices) <= set(at(root, *path).subs)


def test_a_flag_added_to_a_builder_appears(monkeypatch):
    built = cli._operator_parser

    def more():
        p = built()
        p.add_argument("--added-here", action="store_true")
        return p

    monkeypatch.setattr(cli, "_operator_parser", more)
    assert "--added-here" in flags(completion.tree().subs["operator"])


def test_a_sub_verb_added_to_a_group_appears(monkeypatch):
    extra = verbs.Group(subs={**cli.SOUND.subs, "added": "sessiontools.cli:_operator_parser"},
                        rest=cli.SOUND.rest)
    monkeypatch.setattr(cli, "SOUND", extra)
    assert "added" in completion.tree().subs["sound"].subs


def test_help_text_is_never_read(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the tree read --help text")

    monkeypatch.setattr(argparse.ArgumentParser, "format_help", refuse)
    monkeypatch.setattr(argparse.ArgumentParser, "format_usage", refuse)
    completion.tree()


def test_short_only_options_are_left_out():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-x", action="store_true")
    p.add_argument("-y", "--why", action="store_true")
    assert flags(completion.node("t", p)) == {"--why"}


# --- positional choices: the verbs' own tables ------------------------------------------------

def _action(parser, dest):
    return next(a for a in parser._actions if a.dest == dest)


@pytest.mark.parametrize("builder,dest,table", [
    (cli._sound_parser, "event", busy.EVENTS),
    (cli._settings_parser, "tool", settings.TOOLS),
    (cli._get_parser, "prop", get.PROPS),
    (paths.build_parser, "leaf", paths.LEAVES),
])
def test_the_hints_are_the_verbs_own_tables_by_identity(builder, dest, table):
    action = _action(builder(), dest)
    assert getattr(action, verbs.COMPLETION_CHOICES) is table
    assert action.choices is None  # the verb refuses in its own words, not argparse's


def test_sound_event_offers_the_signals_and_not_the_held_one(root):
    sound = at(root, "sound")
    assert sound.choices_at(0) == busy.EVENTS
    for held in busy.HELD_EVENTS:
        assert held not in sound.first_words()


def test_sound_offers_engine_and_the_events_at_its_first_word(root):
    """Review finding 3: `engine` is dispatched by hand before EVENT is read,
    so both are words `session sound` accepts first."""
    assert at(root, "sound").first_words() == ("engine", *busy.EVENTS)


def test_settings_tool_is_the_second_positional(root):
    s = at(root, "settings")
    assert s.choices_at(0) == ()  # SID: free-form
    assert s.choices_at(1) == settings.TOOLS


def test_get_prop_offers_the_property_names(root):
    assert at(root, "get").choices_at(0) == tuple(name for name, _ in get.PROPS)
    assert at(root, "get").choices_at(1) == ()  # SEAT: free-form


def test_free_form_names_are_not_offered(root):
    """Seats, agents and accounts would need tmux, the agents directory or
    the registry (PyYAML) read at TAB: not in this version."""
    assert at(root, "start").choices_at(0) == ()
    for sub in ("use", "remove"):
        assert at(root, "account", sub).choices_at(0) == ()
    assert at(root, "reap").choices_at(3) == ()


def test_argparse_choices_are_offered_and_repeat_with_their_positional():
    p = argparse.ArgumentParser()
    p.add_argument("shell", choices=("bash", "zsh"))
    p.add_argument("more", nargs="*", choices=("a", "b"))
    n = completion.node("t", p)
    assert n.choices_at(0) == ("bash", "zsh")
    assert n.choices_at(1) == n.choices_at(5) == ("a", "b")


def test_non_string_choices_complete_as_their_text():
    p = argparse.ArgumentParser()
    p.add_argument("n", type=int, choices=range(3))
    assert completion.node("t", p).choices_at(0) == ("0", "1", "2")


def test_a_positional_of_n_words_counts_as_n_positionals():
    p = argparse.ArgumentParser()
    p.add_argument("pair", nargs=2, metavar=("A", "B"), choices=("x", "y"))
    p.add_argument("last", choices=("z",))
    n = completion.node("t", p)
    assert [q.name for q in n.positionals] == ["A", "B", "last"]
    assert n.choices_at(1) == ("x", "y")
    assert n.choices_at(2) == ("z",)


# --- flags and their values -------------------------------------------------------------------

def test_a_switch_takes_no_value_and_an_option_does(root):
    assert flag(at(root, "list"), "--json") == Flag("--json")
    assert flag(at(root, "list"), "--pane") == Flag("--pane", takes_value=True)
    assert flag(at(root, "context"), "--max") == Flag("--max", takes_value=True)


def test_dir_and_path_values_complete_from_the_filesystem(root):
    assert flag(at(root, "start"), "--directory").value == "dir"
    assert flag(at(root, "sound", "engine", "build"), "--dest").value == "dir"
    p = argparse.ArgumentParser()
    p.add_argument("--file", metavar="PATH")
    p.add_argument("--out", type=completion.Path)
    n = completion.node("t", p)
    assert flag(n, "--file").value == flag(n, "--out").value == "file"


# --- REMAINDER: start's vendor passthrough ----------------------------------------------------

def test_start_offers_nothing_after_the_agent(root):
    start = at(root, "start")
    assert start.stop_after == 1
    assert "--dry-run" in flags(start)
    assert start.choices_at(1) == ()
    assert [p.name for p in start.positionals] == ["agent"]


# --- groups -----------------------------------------------------------------------------------

def test_a_group_sub_replaces_the_placeholder(root):
    agent = at(root, "pane-state", "agent")
    assert set(agent.subs) == {"on", "off", "status"}
    assert "--help" in flags(agent)


def test_a_group_with_no_rest_parser_still_offers_help(root):
    assert flags(at(root, "statusline")) == {"--help"}


# --- cost and side effects --------------------------------------------------------------------

def test_building_the_tree_starts_no_process(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a process was started")

    with monkeypatch.context() as m:
        m.setattr(subprocess, "Popen", refuse)
        m.setattr(subprocess, "run", refuse)
        completion.tree()


def test_importing_completion_loads_no_yaml():
    probe = ("import sys, sessiontools.completion; "
             "print('yaml' in sys.modules, 'jsonschema' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "False False"
