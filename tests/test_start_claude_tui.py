"""The launcher's one default in the settings door: `"tui": "default"`.

Claude Code offers its fullscreen renderer to every session while no
setting names `tui`. A launched session gets `tui: default` (the renderer
it already runs) in its computed `--settings` door, so the offer is not
made - but ONLY when nothing else names `tui`. The door outranks Claude's
own user, project and local settings, so setting it over one of those
would undo the user's own `/tui` choice. Claude's files are read, never
written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from settings_support import host
from start_support import launch_e2e, plan_for, recorded_argv

from sessiontools import settings
from sessiontools.start import claude_settings as cs
from sessiontools.start.context import PlannedFile

TOOL = "claude-code"
DEFAULT = {"tui": "default"}


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


@pytest.fixture
def scratch(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def env_of(home: Path, **extra) -> dict:
    return {"HOME": str(home), **extra}


# --- which of Claude's own files name tui -------------------------------------------

def test_the_files_read_are_the_users_then_the_projects_then_the_local_one(tmp_path, scratch):
    work = tmp_path / "work"
    assert cs.claude_settings_files(str(work), env_of(scratch)) == [
        scratch / ".claude" / "settings.json",
        work / ".claude" / "settings.json",
        work / ".claude" / "settings.local.json"]


def test_claude_config_dir_moves_the_users_file(tmp_path, scratch):
    config = tmp_path / "config"
    files = cs.claude_settings_files(None, env_of(scratch, CLAUDE_CONFIG_DIR=str(config)))
    assert files == [config / "settings.json"]


def test_nothing_names_tui_on_a_fresh_host(tmp_path, scratch):
    assert cs.claude_sets_tui(str(tmp_path / "work"), env_of(scratch)) is None


@pytest.mark.parametrize("where", ["user", "project", "local", "config-dir"])
def test_any_of_claudes_own_files_naming_tui_is_found(tmp_path, scratch, where):
    work = tmp_path / "work"
    env = env_of(scratch)
    path = {"user": scratch / ".claude" / "settings.json",
            "project": work / ".claude" / "settings.json",
            "local": work / ".claude" / "settings.local.json",
            "config-dir": tmp_path / "config" / "settings.json"}[where]
    if where == "config-dir":
        env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "config")
    w(path, {"tui": "fullscreen"})
    assert cs.claude_sets_tui(str(work), env) == str(path)


def test_under_claude_config_dir_the_home_file_is_not_the_users(tmp_path, scratch):
    """Claude reads the user's file from CLAUDE_CONFIG_DIR when it is set,
    so a `tui` left in ~/.claude is not the one it reads."""
    w(scratch / ".claude" / "settings.json", {"tui": "fullscreen"})
    env = env_of(scratch, CLAUDE_CONFIG_DIR=str(tmp_path / "config"))
    assert cs.claude_sets_tui(None, env) is None


@pytest.mark.parametrize("content", ["{nope", '["tui"]', '"tui"', ""])
def test_a_file_that_is_not_one_json_object_names_nothing(tmp_path, scratch, content):
    w(scratch / ".claude" / "settings.json", content)
    assert cs.claude_sets_tui(None, env_of(scratch)) is None


def test_an_unreadable_file_names_nothing(tmp_path, scratch):
    (scratch / ".claude" / "settings.json").mkdir(parents=True)     # a directory
    assert cs.claude_sets_tui(None, env_of(scratch)) is None


def test_other_keys_are_not_tui(tmp_path, scratch):
    w(scratch / ".claude" / "settings.json", {"model": "opus", "theme": "dark"})
    assert cs.claude_sets_tui(None, env_of(scratch)) is None


# --- the default, against what the door already holds -------------------------------

def empty() -> cs.Rendered:
    return cs.Rendered({}, {}, [], ())


def test_the_default_fills_an_empty_door_and_names_itself():
    rendered = cs.with_tui_default(empty(), None)
    assert rendered.data == DEFAULT
    assert rendered.origins["tui"] == (cs.LAUNCHER_DEFAULT, "")
    assert cs.provenance(rendered) == (
        "computed: claude-code settings: tui from the launcher default "
        "(no settings file sets it)")


def test_a_users_tui_means_no_default():
    assert cs.with_tui_default(empty(), "/home/x/.claude/settings.json").data == {}


def test_a_tui_already_in_the_door_wins():
    held = cs.Rendered({"tui": "fullscreen"}, {"tui": ("host", "/k.json")}, [], ())
    assert cs.with_tui_default(held, None) is held


def test_a_cascade_layer_naming_tui_is_rendered_and_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    w(host(tmp_path, TOOL) / f"{TOOL}.json", {"tui": "fullscreen"})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert "tui" in cs.RENDERED
    assert rendered.notes == []                       # rendered, not "not rendered"
    assert cs.with_tui_default(rendered, None).data == {"tui": "fullscreen"}


# --- the launcher -------------------------------------------------------------------

def door(p) -> dict | None:
    if "--settings" not in p.argv:
        return None
    path = p.argv[p.argv.index("--settings") + 1]
    (item,) = [f for f in p.files if isinstance(f, PlannedFile) and f.path == path]
    return json.loads(item.text)


def plan(tmp_path, home: Path, argv_tail=()):
    return plan_for(tmp_path, "claude:\n  effort: high\n", argv_tail=argv_tail,
                    extra_env={"HOME": str(home)})


def test_a_fresh_host_gets_a_door_holding_only_the_default(tmp_path, scratch):
    assert door(plan(tmp_path, scratch)) == DEFAULT


def test_a_users_renderer_choice_is_never_overridden(tmp_path, scratch):
    w(scratch / ".claude" / "settings.json", {"tui": "fullscreen"})
    w(host(scratch, TOOL) / f"{TOOL}.json", {"model": "fake-host"})
    assert door(plan(tmp_path, scratch)) == {"model": "fake-host"}


def test_a_project_renderer_choice_under_the_work_dir_is_never_overridden(tmp_path, scratch):
    w(tmp_path / "work" / ".claude" / "settings.local.json", {"tui": "fullscreen"})
    assert door(plan(tmp_path, scratch)) is None


def test_a_live_launch_reads_the_users_file_and_never_writes_it(tmp_path):
    """End to end against a stub claude: the user's own settings name no
    `tui`, so the door carries the default, and their file is byte for
    byte what it was."""
    home = tmp_path / "scratch-home"
    user = w(home / ".claude" / "settings.json", '{"model": "opus"}\n')
    before = user.read_bytes()
    result, record, _work = launch_e2e(tmp_path, "scout", extra_env={"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert json.loads(Path(argv[argv.index("--settings") + 1]).read_text()) == DEFAULT
    assert user.read_bytes() == before
    assert sorted(p.name for p in (home / ".claude").iterdir()) == ["settings.json"]
