"""`pane_state.listeners`: the pane signal's trust list, which fails NARROW (ST-134 R4)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from settings_support import host

from sessiontools import pane_state
from sessiontools.claude_code import DEFAULT_LISTENERS


def _settings_dir() -> Path:
    home = Path(os.environ["HOME"])
    assert "no-home" in str(home), "the walled HOME, never the live one"
    d = host(home, "session")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _host(data=None, raw=None):
    (_settings_dir() / "session.json").write_text(raw if raw is not None else json.dumps(data))


def test_nothing_configured_is_pager_listen_only():
    assert pane_state.listeners() == ("pager listen",) == DEFAULT_LISTENERS


def test_a_list_replaces_the_default_exactly():
    _host({"pane_state": {"listeners": ["pager listen", "  tm   task wait "]}})
    assert pane_state.listeners() == ("pager listen", "tm task wait")


def test_an_empty_list_means_no_listeners():
    _host({"pane_state": {"listeners": []}})
    assert pane_state.listeners() == ()


def test_blanks_and_non_strings_are_dropped():
    _host({"pane_state": {"listeners": ["", "  ", 7, None, "pager listen"]}})
    assert pane_state.listeners() == ("pager listen",)


@pytest.mark.parametrize("value", ["tm task wait", {"a": 1}, 3, None])
def test_a_listeners_that_is_not_a_list_is_the_default(value):
    _host({"pane_state": {"listeners": value}})
    assert pane_state.listeners() == DEFAULT_LISTENERS


@pytest.mark.parametrize("data", [{"pane_state": "x"}, {"pane_state": [1]}, {"other": 1}])
def test_a_misshapen_block_is_the_default(data):
    _host(data)
    assert pane_state.listeners() == DEFAULT_LISTENERS


def test_an_unreadable_file_is_the_default():
    _host(raw="{not json")
    assert pane_state.listeners() == DEFAULT_LISTENERS


def test_the_template_layer_supplies_it_and_host_replaces_it():
    (_settings_dir() / "session.template.json").write_text(
        json.dumps({"pane_state": {"listeners": ["pager listen", "tm task wait"]}}))
    assert pane_state.listeners() == ("pager listen", "tm task wait")
    _host({"pane_state": {"listeners": ["pager listen"]}})
    assert pane_state.listeners() == ("pager listen",)
