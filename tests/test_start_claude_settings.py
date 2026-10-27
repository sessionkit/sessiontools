"""ST-151: the host's model and effort ride one `--settings` file.

The `claude-code` settings tool, resolved through the cascade with its host
layer read from its package's settings directory (settings.host_dir, the
only host directory), rendered (model and effortLevel only)
into a 0600 file the launcher hands claude as a computed `--settings` door.
Every tree is under a scratch HOME; every value is FAKE. Nothing here runs
a real claude: the live probe is verify-001's.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from settings_support import host
from start_support import launch_e2e, plan_for, recorded_argv

from sessiontools import settings
from sessiontools.start import claude_settings as cs
from sessiontools.start.context import SETTINGS_PREFIX, PlannedFile, materialize
from sessiontools.start.errors import LaunchError

SID = "11111111-2222-3333-4444-555555555555"
TOOL = "claude-code"
#: The launcher's default when nothing names Claude's renderer: these test
#: HOMEs have no ~/.claude, so every door carries it.
TUI = {"tui": "default"}
LAUNCHER_TUI = "tui from the launcher default (no settings file sets it)"


def chose_a_renderer(home: Path) -> None:
    """The user ran `/tui fullscreen`: their own file names `tui`."""
    w(home / ".claude" / "settings.json", {"tui": "fullscreen"})


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def kit(home: Path, tool: str = TOOL) -> Path:
    return host(home, tool)


def kit_short(tool: str = TOOL) -> str:
    """TOOL's host file as the launcher spells it, `~/...`."""
    fake = Path("/home-of-test")
    return f"~/{(kit(fake, tool) / f'{tool}.json').relative_to(fake)}"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSIONS_DIR", str(home / ".session" / "sessions"))
    return home


# --- the resolver: settings.host_dir -------------------------------------------

def test_the_kit_path_alone_is_the_host_layer(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-kit"})
    res = settings.resolve(TOOL, None)
    assert res.values == {"model": "fake-kit"}
    assert [(s.layer, s.path) for s in res.sources] == [("host", kit(tree) / "claude-code.json")]
    assert res.notes == []


def test_a_drop_in_alone_is_the_host_layer(tree):
    w(kit(tree) / "claude-code.d" / "10-a.json", {"model": "fake-dropin"})
    assert settings.resolve(TOOL, None).values == {"model": "fake-dropin"}
    assert settings.host_dir(TOOL) == (kit(tree), None)


def test_seat_and_session_override_the_host(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-host", "effortLevel": "fake-host-effort"})
    w(tree / ".session" / "seats" / "s1" / "settings" / "claude-code.json", {"model": "fake-seat"})
    w(tree / ".session" / "sessions" / SID / "settings" / "claude-code.json",
      {"effortLevel": "fake-session-effort"})
    res = settings.resolve(TOOL, SID, seat="s1")
    assert res.values == {"model": "fake-seat", "effortLevel": "fake-session-effort"}
    assert [s.layer for s in res.sources] == ["host", "seat", "session"]


def test_another_tool_reads_its_own_package_directory(tree):
    w(kit(tree, "mail") / "mail.json", {"hooks": True})
    res = settings.resolve("mail", None)
    assert res.values == {"hooks": True}
    assert res.notes == []
    assert settings.host_dir("mail") == (kit(tree, "mail"), None)


def test_the_tool_list_finds_claude_code_at_the_kit_path(tree):
    w(kit(tree) / "claude-code.json", {"model": "fake-kit"})
    assert TOOL in settings.tools(SID)
    assert TOOL in settings.TOOLS


def test_not_moved_names_the_one_host_directory(tree):
    out = settings.report(settings.resolve(TOOL, None))
    assert "not moved" in out
    assert f"no {TOOL}.json under {kit_short().rsplit('/', 1)[0]}, seats/" in out


# --- render: the v1 allowlist ----------------------------------------------------

def test_render_keeps_model_and_effort_only_and_names_the_rest(tree):
    w(kit(tree) / "claude-code.json",
      {"model": "fake-m", "effortLevel": "fake-e", "permissions": {"allow": ["x"]},
       "cleanupPeriodDays": 3, "theme": None})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.data == {"model": "fake-m", "effortLevel": "fake-e"}
    origin = str(kit(tree) / "claude-code.json")
    assert sorted(rendered.notes) == sorted([
        f"claude-code: permissions.allow (from {origin}) not rendered in v1",
        f"claude-code: cleanupPeriodDays (from {origin}) not rendered in v1"])
    assert rendered.origins["model"] == ("host", origin)


def test_render_refuses_nothing_and_drops_a_model_that_is_not_a_string(tree):
    w(kit(tree) / "claude-code.json", {"model": {"name": "fake"}, "effortLevel": 3})
    rendered = cs.render(settings.resolve(TOOL, None))
    assert rendered.data == {}
    assert len(rendered.notes) == 2


def test_own_settings_value_reads_either_spelling():
    assert cs.own_settings_value(["--x", "--settings", "a.json"]) == "a.json"
    assert cs.own_settings_value(["--settings=b.json"]) == "b.json"
    assert cs.own_settings_value(["--settings"]) == ""
    assert cs.own_settings_value(["--settings-file", "c"]) is None


# --- the launcher ----------------------------------------------------------------

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


def test_no_claude_code_file_anywhere_the_door_holds_the_tui_default(tmp_path, scratch,
                                                                    capsys):
    """A fresh host: no `claude-code` file and no renderer chosen. The door
    exists for the launcher's `tui` default alone, and nothing is said."""
    p = plan(tmp_path, scratch)
    sources = [src for token, src in p.provenance if token == "--settings"]
    assert sources == [f"computed: claude-code settings: {LAUNCHER_TUI}"]
    assert capsys.readouterr().err.count("claude-code") == 0
    (tmp_path / "live").mkdir()
    live = plan(tmp_path / "live", scratch, dry_run=False)
    item = next(f for f in live.files if isinstance(f, PlannedFile)
                and f.path == settings_door(live.argv))
    assert json.loads(item.text) == TUI


def test_no_claude_code_file_and_a_renderer_chosen_no_door(tmp_path, scratch, capsys):
    """The user's own settings name `tui`: the door would outrank them, so
    with nothing else to render there is no door, as before."""
    chose_a_renderer(scratch)
    p = plan(tmp_path, scratch)
    assert "--settings" not in p.argv
    assert capsys.readouterr().err.count("claude-code") == 0


def test_a_host_file_reaches_the_settings_door_with_its_origin(tmp_path, scratch):
    w(kit(scratch) / "claude-code.json", {"model": "fake-host", "effortLevel": "fake-e"})
    p = plan(tmp_path, scratch)
    assert settings_door(p.argv).endswith("settings.json")
    sources = [src for token, src in p.provenance if token == "--settings"]
    assert sources == [("computed: claude-code settings: model from host "
                        f"{kit_short()}, effortLevel "
                        f"from host {kit_short()}, {LAUNCHER_TUI}")]
    assert p.files == ()                                   # a dry run plans no file


def test_a_live_plan_names_a_0600_file_holding_model_and_effort_only(tmp_path, scratch):
    w(kit(scratch) / "claude-code.json",
      {"model": "fake-host", "effortLevel": "fake-e", "hooks": {"x": 1}})
    p = plan(tmp_path, scratch, dry_run=False)
    path = settings_door(p.argv)
    (item,) = [f for f in p.files if isinstance(f, PlannedFile) and f.path == path]
    assert os.path.basename(path).startswith(SETTINGS_PREFIX) and path.endswith(".json")
    assert os.path.dirname(path) == os.path.join(p.identity.session_dir, "launch")
    assert json.loads(item.text) == {"effortLevel": "fake-e", "model": "fake-host", **TUI}
    target = tmp_path / "written.json"
    materialize(PlannedFile(str(target), item.text))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert ("settings", path) in p.record.files       # the launch record names it


def test_a_seat_overrides_the_host_and_a_cli_model_stays_after_the_door(tmp_path, scratch):
    w(kit(scratch) / "claude-code.json", {"model": "fake-host", "effortLevel": "fake-e"})
    w(scratch / ".session" / "seats" / "s1" / "settings" / "claude-code.json",
      {"model": "fake-seat"})
    p = plan(tmp_path, scratch, flags=("--seat", "s1"), argv_tail=("--model", "fake-cli"),
             dry_run=False)
    item = next(f for f in p.files if isinstance(f, PlannedFile)
                and f.path == settings_door(p.argv))
    assert json.loads(item.text) == {"effortLevel": "fake-e", "model": "fake-seat", **TUI}
    # claude: a CLI --model beats --settings (ST-150 p15); the launcher keeps it.
    assert p.argv.index("--settings") < p.argv.index("--model")
    assert p.argv[p.argv.index("--model") + 1] == "fake-cli"
    (source,) = [s for t, s in p.provenance if t == "--settings"]
    assert "model from seat ~/.session/seats/s1/settings/claude-code.json" in source


def test_the_agent_layer_feeds_the_door(tmp_path, scratch):
    fm = "claude:\n  effort: high\nsettings:\n  claude-code.model: fake-agent\n"
    p = plan(tmp_path, scratch, front_matter=fm)
    (source,) = [s for t, s in p.provenance if t == "--settings"]
    assert source.startswith("computed: claude-code settings: model from agent ")


def test_an_extra_key_is_one_stderr_line(tmp_path, scratch, capsys):
    w(kit(scratch) / "claude-code.json", {"model": "fake-host", "permissions": {"deny": ["x"]}})
    plan(tmp_path, scratch)
    err = capsys.readouterr().err
    assert err.count("not rendered in v1") == 1
    assert "claude-code: permissions.deny (from " in err


def test_an_agent_rule_cut_inside_brackets_is_said_on_stderr(tmp_path, scratch, capsys):
    """The agent layer alone feeds the launch: its split rule is not rendered,
    and the note that it was cut inside brackets is said once beside that."""
    fm = "claude:\n  effort: high\nsettings:\n  claude-code.permissions.rules.Read(~/.zshrc): deny\n"
    plan(tmp_path, scratch, front_matter=fm)
    err = capsys.readouterr().err
    assert err.count("is cut at a dot inside brackets or quotes") == 1
    assert ('write it nested to keep the key whole: '
            'claude-code.permissions.rules: {"Read(~/.zshrc)": ...}') in err
    assert "claude-code: permissions.rules.Read(~/.zshrc) (from " in err


def test_only_extra_keys_no_door(tmp_path, scratch, capsys):
    chose_a_renderer(scratch)               # else the door holds the tui default
    w(kit(scratch) / "claude-code.json", {"theme": "fake"})
    p = plan(tmp_path, scratch)
    assert "--settings" not in p.argv
    assert "theme" in capsys.readouterr().err


@pytest.mark.parametrize("front_matter, tail", [
    ("claude:\n  effort: high\n  settings: mine.json\n", ()),
    # Both doors open: the agent file's wins the skip; nothing is folded.
    ("claude:\n  effort: high\n  settings: mine.json\n", ("--settings", "cli.json")),
])
def test_an_agent_files_own_settings_is_left_alone_and_named(tmp_path, scratch, capsys,
                                                             front_matter, tail):
    """The front-matter `claude: settings:` form, unchanged by the fold:
    never folded, never merged; today's argv exactly, plus one line."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    before = plan(tmp_path / "a", scratch, front_matter=front_matter, argv_tail=tail)
    capsys.readouterr()
    w(kit(scratch) / "claude-code.json", {"model": "fake-host", "theme": "fake"})
    after = plan(tmp_path / "b", scratch, front_matter=front_matter, argv_tail=tail)
    def norm(p, root):
        return [str(t).replace(p.identity.session_id, "<sid>").replace(str(root), "<tmp>")
                for t in p.argv]

    assert norm(after, tmp_path / "b") == norm(before, tmp_path / "a")
    err = capsys.readouterr().err
    assert err.count("claude-code:") == 1                       # no "not rendered" line
    assert kit_short() in err
    assert "mine.json" in err and "not applied" in err


def test_a_layer_file_the_reader_refuses_is_a_usage_refusal(tmp_path, scratch):
    w(kit(scratch) / "claude-code.json", '["not an object"]')
    with pytest.raises(LaunchError) as raised:
        plan(tmp_path, scratch)
    assert "settings:" in str(raised.value) and "not a JSON object" in str(raised.value)


def test_a_resume_carries_the_door(tmp_path, scratch):
    w(kit(scratch) / "claude-code.json", {"model": "fake-host"})
    p = plan(tmp_path, scratch, argv_tail=("--resume", SID))
    assert p.resume and settings_door(p.argv) is not None


def test_a_live_launch_writes_the_file_and_never_the_user_file(tmp_path):
    """End to end against a stub claude: the door's file is written 0600
    under the session's launch/, and ~/.claude/ is never made."""
    home = tmp_path / "scratch-home"
    w(kit(home) / "claude-code.json", {"model": "fake-host", "effortLevel": "fake-e"})
    result, record, _work = launch_e2e(tmp_path, "scout", extra_env={"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    path = Path(settings_door(argv))
    assert path.parent.name == "launch"
    assert json.loads(path.read_text()) == {"effortLevel": "fake-e", "model": "fake-host",
                                            **TUI}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (home / ".claude").exists()
