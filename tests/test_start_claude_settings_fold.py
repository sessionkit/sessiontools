"""A crew's CLI `--settings` is FOLDED into the computed settings door.

A launch whose CLI passthrough opens `--settings` beside a computed door
gets that file folded in as the TOP layer of the `claude-code` cascade,
merged BEFORE render: its keys win over every layer, a host `_locked` path
still holds, and its leaves render as given (the v1 allowlist filters the
cascade's leaves only). The launch carries ONE `--settings`, the computed
one. An agent file's `claude: settings:` is not folded: that launch keeps
today's skip (tests/test_start_claude_settings.py). Every value here is
made up.
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
from sessiontools.start.errors import LaunchError

SID = "11111111-2222-3333-4444-555555555555"
TOOL = "claude-code"
FM = "claude:\n  effort: high\n"
#: The launcher's default when nothing names Claude's renderer: these test
#: HOMEs have no ~/.claude, so every door carries it.
TUI = {"tui": "default"}


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def host_file(home: Path) -> Path:
    return host(home, TOOL) / f"{TOOL}.json"


def chose_a_renderer(home: Path) -> None:
    """The user ran `/tui fullscreen`: their own file names `tui`."""
    w(home / ".claude" / "settings.json", {"tui": "fullscreen"})


@pytest.fixture
def scratch(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def plan(tmp_path, home: Path, argv_tail=(), front_matter: str = FM, flags=(),
         dry_run=False):
    flags = ("--dry-run", *flags) if dry_run else tuple(flags)
    return plan_for(tmp_path, front_matter, argv_tail=argv_tail, flags=flags,
                    extra_env={"HOME": str(home)})


def door(p) -> dict:
    """The computed door's JSON, from the planned file (a live plan)."""
    assert p.argv.count("--settings") == 1
    path = p.argv[p.argv.index("--settings") + 1]
    (item,) = [f for f in p.files if isinstance(f, PlannedFile) and f.path == path]
    return json.loads(item.text)


def door_source(p) -> str:
    (source,) = [s for t, s in p.provenance if t == "--settings"]
    return source


# --- the fold itself: claude_settings.fold over a resolved cascade ---------------

@pytest.fixture
def tree(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSIONS_DIR", str(home / ".session" / "sessions"))
    return home


def test_a_the_folded_file_wins_over_host_seat_and_session(tree):
    """Test (a): the crew layer is the TOP layer, above the session layer."""
    w(host_file(tree), {"model": "fake-host", "effortLevel": "fake-host-e"})
    w(tree / ".session" / "seats" / "s1" / "settings" / f"{TOOL}.json",
      {"model": "fake-seat", "effortLevel": "fake-seat-e"})
    w(tree / ".session" / "sessions" / SID / "settings" / f"{TOOL}.json",
      {"model": "fake-session"})
    crew = w(tree / "crew.json", {"model": "fake-crew"})
    res = settings.resolve(TOOL, SID, seat="s1")
    assert cs.render(res).data["model"] == "fake-session"      # before the fold
    folded = cs.render(cs.fold(res, cs.passthrough_source(str(crew))), str(crew))
    assert folded.data == {"model": "fake-crew", "effortLevel": "fake-seat-e"}
    assert folded.origins["model"] == (cs.PASSTHROUGH, str(crew))
    assert folded.origins["effortLevel"][0] == "seat"
    assert folded.folded == str(crew)
    assert str(crew) not in folded.files                       # the cascade's files only


def test_b_a_host_lock_beats_the_folded_file(tree):
    """Test (b): a host `_locked` path ignores the crew's key; the rest folds."""
    w(host_file(tree), {"model": "fake-host", "_locked": ["model"]})
    crew = w(tree / "crew.json", {"model": "fake-crew", "effortLevel": "fake-crew-e"})
    res = cs.fold(settings.resolve(TOOL, None), cs.passthrough_source(str(crew)))
    data = cs.render(res, str(crew)).data
    assert data == {"model": "fake-host", "effortLevel": "fake-crew-e"}


def test_a_lock_on_a_map_holds_for_every_leaf_under_it(tree):
    w(host_file(tree), {"statusLine": {"type": "command", "command": "~/fake-host.sh"},
                        "_locked": ["statusLine"]})
    crew = w(tree / "crew.json", {"statusLine": {"command": "~/fake-crew.sh", "padding": 2}})
    res = cs.fold(settings.resolve(TOOL, None), cs.passthrough_source(str(crew)))
    assert cs.render(res, str(crew)).data == {
        "statusLine": {"type": "command", "command": "~/fake-host.sh"}}


def test_the_folded_leaves_render_as_given_and_the_cascade_is_still_filtered(tree):
    """Exempt by LEAF: the crew's keys v1 does not render arrive whole and
    unsaid; a host key v1 does not render is still said and dropped."""
    w(host_file(tree), {"model": "fake-host", "permissions": {"deny": ["fake-host-rule"]}})
    crew = w(tree / "crew.json", {"claudeMdExcludes": ["fake/a", "fake/b"],
                                  "permissions": {"allow": ["fake-crew-rule"]},
                                  "env": {"FAKE": "1"}})
    res = cs.fold(settings.resolve(TOOL, None), cs.passthrough_source(str(crew)))
    rendered = cs.render(res, str(crew))
    assert rendered.data == {"model": "fake-host", "claudeMdExcludes": ["fake/a", "fake/b"],
                             "permissions": {"allow": ["fake-crew-rule"]},
                             "env": {"FAKE": "1"}}
    assert rendered.notes == [cs.not_rendered("permissions.deny", str(host_file(tree)))]
    assert next(iter(rendered.data)) == "model"                # RENDERED keys first


def test_a_crew_status_line_wins_whole_over_the_kit_one(tree):
    w(host(tree, TOOL) / f"{TOOL}.d" / "50-statusline.json",
      {"statusLine": {"type": "command", "command": "~/fake-kit.sh", "padding": 1}})
    crew = w(tree / "crew.json", {"statusLine": {"type": "command",
                                                 "command": "~/fake-crew.sh",
                                                 "fakeSubKey": True}})
    res = cs.fold(settings.resolve(TOOL, None), cs.passthrough_source(str(crew)))
    rendered = cs.render(res, str(crew))
    # The kit's padding still merges in under the crew's line, key by key;
    # the crew's own sub-key is kept as given, never checked.
    assert rendered.data == {"statusLine": {"type": "command", "command": "~/fake-crew.sh",
                                            "padding": 1, "fakeSubKey": True}}
    assert rendered.notes == []
    assert rendered.origins["statusLine"] == (cs.PASSTHROUGH, str(crew))


def test_a_status_line_all_from_the_crew_is_rendered_as_given(tree):
    w(host_file(tree), {"model": "fake-host"})
    crew = w(tree / "crew.json", {"statusLine": {"type": "fake-type", "command": ""}})
    res = cs.fold(settings.resolve(TOOL, None), cs.passthrough_source(str(crew)))
    rendered = cs.render(res, str(crew))
    assert rendered.data["statusLine"] == {"type": "fake-type", "command": ""}
    assert rendered.notes == []


def test_a_bad_kit_status_sub_key_is_still_said_beside_a_crew_command(tree):
    w(host(tree, TOOL) / f"{TOOL}.d" / "50-statusline.json",
      {"statusLine": {"type": "command", "command": "~/fake-kit.sh", "padding": -1}})
    crew = w(tree / "crew.json", {"statusLine": {"command": "~/fake-crew.sh"}})
    res = cs.fold(settings.resolve(TOOL, None), cs.passthrough_source(str(crew)))
    rendered = cs.render(res, str(crew))
    assert rendered.data == {"statusLine": {"type": "command", "command": "~/fake-crew.sh"}}
    assert len(rendered.notes) == 1 and "statusLine.padding" in rendered.notes[0]


def test_a_crew_null_unsets_a_lower_key(tree):
    w(host_file(tree), {"model": "fake-host", "effortLevel": "fake-e"})
    crew = w(tree / "crew.json", {"model": None})
    res = cs.fold(settings.resolve(TOOL, None), cs.passthrough_source(str(crew)))
    assert cs.render(res, str(crew)).data == {"effortLevel": "fake-e"}


# --- reading the value: a file or inline JSON ------------------------------------

def test_inline_json_is_read_as_the_top_layer(tree):
    w(host_file(tree), {"model": "fake-host"})
    value = '{"model": "fake-inline"}'
    src = cs.passthrough_source(value)
    assert src.layer == cs.PASSTHROUGH and str(src.path) == cs.INLINE
    rendered = cs.render(cs.fold(settings.resolve(TOOL, None), src), value)
    assert rendered.data == {"model": "fake-inline"}
    assert cs.provenance(rendered).endswith(f"; folded on top: the CLI {cs.INLINE}")


def test_a_tilde_is_the_launchs_home_and_a_relative_path_is_under_cwd(tmp_path):
    fake_home = tmp_path / "fake-home"
    w(fake_home / "crew.json", {"model": "fake-tilde"})
    w(tmp_path / "work" / "rel.json", {"model": "fake-rel"})
    env = {"HOME": str(fake_home)}
    assert cs.passthrough_source("~/crew.json", env=env).data == {"model": "fake-tilde"}
    src = cs.passthrough_source("rel.json", cwd=str(tmp_path / "work"))
    assert src.path == tmp_path / "work" / "rel.json"


@pytest.mark.parametrize("value, said", [
    ("", "--settings has no value to fold"),
    ("{not json", "not JSON"),
    ("[1]", "cannot be read"),              # not `{`: a path, and no such file
    ('{"a": 1} trailing', "not JSON"),
])
def test_a_value_that_cannot_be_folded_is_a_settings_error(tmp_path, value, said):
    with pytest.raises(settings.SettingsError) as raised:
        cs.passthrough_source(value, cwd=str(tmp_path))
    assert said in str(raised.value)


def test_a_file_that_is_not_one_object_is_a_settings_error(tmp_path):
    w(tmp_path / "list.json", "[1, 2]")
    with pytest.raises(settings.SettingsError, match="not a JSON object"):
        cs.passthrough_source(str(tmp_path / "list.json"))


def test_without_own_settings_drops_both_spellings_and_nothing_else():
    assert cs.without_own_settings(["--x", "--settings", "a.json", "--y"]) == ["--x", "--y"]
    assert cs.without_own_settings(["--settings=b.json", "--model", "m"]) == ["--model", "m"]
    assert cs.without_own_settings(["--x", "--settings"]) == ["--x"]
    assert cs.without_own_settings(["--settings-file", "c"]) == ["--settings-file", "c"]


def test_own_settings_values_names_every_settings_in_order():
    assert cs.own_settings_values(["--settings", "a.json", "--settings=b.json"]) == [
        "a.json", "b.json"]
    assert cs.own_settings_values(["--x", "--settings"]) == [""]
    assert cs.own_settings_values(["--settings-file", "c"]) == []
    # The value after a bare `--settings` is its value, never a second flag.
    assert cs.own_settings_values(["--settings", "--settings=d"]) == ["--settings=d"]


# --- the launcher -------------------------------------------------------------------

@pytest.mark.parametrize("spelling", ["two", "equals"])
def test_a_crew_settings_is_folded_into_one_door(tmp_path, scratch, capsys, spelling):
    w(host_file(scratch), {"model": "fake-host", "effortLevel": "fake-e"})
    crew = w(tmp_path / "crew.json", {"model": "fake-crew", "claudeMdExcludes": ["fake/x"]})
    tail = (("--settings", str(crew)) if spelling == "two" else (f"--settings={crew}",))
    p = plan(tmp_path, scratch, argv_tail=(*tail, "--model", "fake-cli"))
    assert door(p) == {"effortLevel": "fake-e", "model": "fake-crew",
                       "claudeMdExcludes": ["fake/x"], **TUI}
    assert str(crew) not in [str(t) for t in p.argv]
    assert not any(str(t).startswith("--settings=") for t in p.argv)
    # The CLI --model still rides after the door, where claude makes it win.
    assert p.argv.index("--settings") < p.argv.index("--model")
    source = door_source(p)
    assert f"model from {cs.PASSTHROUGH} {crew}" in source
    assert source.endswith(f"; folded on top: the CLI --settings {crew}")
    assert "claude-code:" not in capsys.readouterr().err       # nothing said: folded


def test_a_host_lock_beats_the_crew_at_launch(tmp_path, scratch):
    w(host_file(scratch), {"model": "fake-host", "_locked": ["model"]})
    crew = w(tmp_path / "crew.json", {"model": "fake-crew"})
    p = plan(tmp_path, scratch, argv_tail=("--settings", str(crew)))
    assert door(p) == {"model": "fake-host", **TUI}
    assert door_source(p).startswith("computed: claude-code settings: model from host ")


def test_a_relative_crew_settings_is_read_under_the_working_directory(tmp_path, scratch):
    w(host_file(scratch), {"model": "fake-host"})
    w(tmp_path / "work" / "crew.json", {"effortLevel": "fake-work"})
    p = plan(tmp_path, scratch, argv_tail=("--settings", "crew.json"))
    assert door(p) == {"model": "fake-host", "effortLevel": "fake-work", **TUI}


def test_a_dry_run_folds_and_names_the_file(tmp_path, scratch):
    w(host_file(scratch), {"model": "fake-host"})
    crew = w(tmp_path / "crew.json", {"model": "fake-crew"})
    p = plan(tmp_path, scratch, argv_tail=("--settings", str(crew)), dry_run=True)
    assert p.argv.count("--settings") == 1 and str(crew) not in p.argv
    assert p.files == ()
    assert door_source(p).endswith(f"; folded on top: the CLI --settings {crew}")


def test_with_no_claude_code_file_the_crew_settings_is_not_read(tmp_path, scratch, capsys):
    """No computed door (no `claude-code` file, and the user's own settings
    name `tui`): the argv is as before, and the crew file is not even
    opened (it does not exist here)."""
    chose_a_renderer(scratch)
    p = plan(tmp_path, scratch, argv_tail=("--settings", "no-such.json"))
    assert p.argv[p.argv.index("--settings") + 1] == "no-such.json"
    assert p.argv.count("--settings") == 1
    assert "claude-code" not in capsys.readouterr().err


def test_with_no_claude_code_file_the_crew_settings_is_folded_for_the_tui_default(
        tmp_path, scratch, capsys):
    """A fresh host: no `claude-code` file, no renderer chosen. The door
    exists for the `tui` default, so the crew file is folded into it, as on
    a host that has a `claude-code` file."""
    crew = w(tmp_path / "crew.json", {"model": "fake-crew"})
    p = plan(tmp_path, scratch, argv_tail=("--settings", str(crew)))
    assert door(p) == {"model": "fake-crew", **TUI}
    assert p.argv.count("--settings") == 1 and str(crew) not in p.argv
    assert "claude-code" not in capsys.readouterr().err


def test_a_crew_settings_that_names_tui_wins_over_the_default(tmp_path, scratch):
    crew = w(tmp_path / "crew.json", {"tui": "fullscreen"})
    p = plan(tmp_path, scratch, argv_tail=("--settings", str(crew)))
    assert door(p) == {"tui": "fullscreen"}
    assert f"tui from {cs.PASSTHROUGH} {crew}" in door_source(p)


def test_a_fold_with_nothing_to_render_keeps_the_crew_tokens(tmp_path, scratch, capsys):
    """The fold leaves no data (the host has only a key v1 does not render,
    the crew file is empty, and the user's settings name `tui`, so there
    is no default either): no door, and claude reads the crew file alone."""
    chose_a_renderer(scratch)
    w(host_file(scratch), {"theme": "fake"})
    crew = w(tmp_path / "crew.json", {})
    p = plan(tmp_path, scratch, argv_tail=("--settings", str(crew)))
    assert p.argv[p.argv.index("--settings") + 1] == str(crew)
    assert p.argv.count("--settings") == 1
    assert "theme" in capsys.readouterr().err


@pytest.mark.parametrize("content, said", [
    ('["not an object"]', "not a JSON object"),
    ("{nope", "not JSON"),
    (None, "cannot be read"),
])
def test_a_crew_settings_that_cannot_be_folded_refuses_the_launch(tmp_path, scratch,
                                                                  content, said):
    w(host_file(scratch), {"model": "fake-host"})
    crew = tmp_path / "crew.json"
    if content is not None:
        w(crew, content)
    with pytest.raises(LaunchError) as raised:
        plan(tmp_path, scratch, argv_tail=("--settings", str(crew)))
    assert raised.value.code == 2
    assert "settings:" in str(raised.value) and said in str(raised.value)
    assert str(crew) in str(raised.value)


@pytest.mark.parametrize("tail", [
    ("--settings", "{a}", "--settings", "{b}"),
    ("--settings={a}", "--settings={b}"),
])
def test_a_crew_settings_given_twice_refuses_the_launch(tmp_path, scratch, tail):
    """The fold takes ONE file: folding the first and dropping the second
    would lose the second's keys without a word, so the launch refuses."""
    w(host_file(scratch), {"model": "fake-host"})
    a = w(tmp_path / "a.json", {"effortLevel": "fake-a"})
    b = w(tmp_path / "b.json", {"model": "fake-b"})
    with pytest.raises(LaunchError) as raised:
        plan(tmp_path, scratch, argv_tail=tuple(t.format(a=a, b=b) for t in tail))
    assert raised.value.code == 2
    said = str(raised.value)
    assert "--settings 2 times" in said and str(a) in said and str(b) in said


def test_c_the_front_matter_settings_form_is_unchanged(tmp_path, scratch, capsys):
    """Test (c): an agent file's `claude: settings:` is never folded: no
    door, its own `--settings` in argv, and today's one skip line."""
    w(host_file(scratch), {"model": "fake-host"})
    crew = w(tmp_path / "crew.json", {"model": "fake-crew"})
    p = plan(tmp_path, scratch, front_matter=f"claude:\n  effort: high\n  settings: {crew}\n")
    assert p.argv.count("--settings") == 1
    assert p.argv[p.argv.index("--settings") + 1] == str(crew)
    assert not [f for f in p.files if isinstance(f, PlannedFile)
                and "settings" in Path(f.path).name and f.path != str(crew)]
    err = capsys.readouterr().err
    assert err.count("claude-code:") == 1 and "not applied" in err
    assert "nothing is merged or refused" in err


def test_a_resume_folds_too(tmp_path, scratch):
    w(host_file(scratch), {"model": "fake-host"})
    crew = w(tmp_path / "crew.json", {"effortLevel": "fake-crew"})
    p = plan(tmp_path, scratch, argv_tail=("--resume", SID, "--settings", str(crew)),
             dry_run=True)
    assert p.resume and p.argv.count("--settings") == 1 and str(crew) not in p.argv
    assert "effortLevel from passthrough" in door_source(p)


def test_a_live_launch_writes_the_folded_file(tmp_path):
    """End to end against a stub claude: one --settings, the computed file,
    holding the host's effort and the crew's model and excludes."""
    home = tmp_path / "scratch-home"
    w(host_file(home), {"model": "fake-host", "effortLevel": "fake-e"})
    crew = w(tmp_path / "crew.json", {"model": "fake-crew", "claudeMdExcludes": ["fake/x"]})
    result, record, _work = launch_e2e(tmp_path, "scout", "--settings", str(crew),
                                       extra_env={"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert argv.count("--settings") == 1 and str(crew) not in argv
    path = Path(argv[argv.index("--settings") + 1])
    assert path.parent.name == "launch"
    assert json.loads(path.read_text()) == {"claudeMdExcludes": ["fake/x"],
                                            "effortLevel": "fake-e", "model": "fake-crew",
                                            **TUI}
    assert json.loads(crew.read_text()) == {"model": "fake-crew",
                                            "claudeMdExcludes": ["fake/x"]}   # untouched


# --- no residue: the fold's refusals come BEFORE the mint (plan.rehearse_fold) ------

def session_dirs(tmp_path: Path) -> list[str]:
    """Every session directory a launch left under any `sessions/` folder in
    the test's tree (the agents root's SESSIONS_DIR and HOME's alike)."""
    return sorted(str(d.relative_to(tmp_path)) for s in tmp_path.rglob("sessions")
                  if s.is_dir() for d in s.iterdir())


@pytest.mark.parametrize("tail, said", [
    (("--settings", "nope.json"), "cannot be read"),
    (("--settings", "{a}", "--settings", "{b}"), "--settings 2 times"),
    (("--settings={a}", "--settings={b}"), "--settings 2 times"),
])
def test_a_crew_settings_the_fold_refuses_leaves_no_session_dir(tmp_path, tail, said):
    """End to end: the launch exits 2 and mints nothing, so no session
    directory is left behind (the refusal used to come after the mint)."""
    home = tmp_path / "scratch-home"
    w(host_file(home), {"model": "fake-host"})
    a = w(tmp_path / "a.json", {"effortLevel": "fake-a"})
    b = w(tmp_path / "b.json", {"model": "fake-b"})
    result, record, _work = launch_e2e(tmp_path, "scout",
                                       *[t.format(a=a, b=b) for t in tail],
                                       extra_env={"HOME": str(home)})
    assert result.returncode == 2, result.stderr
    assert said in result.stderr
    assert session_dirs(tmp_path) == []
    assert not list(record.iterdir())                        # claude never ran


def test_a_launch_that_folds_does_mint_its_session_dir(tmp_path):
    """The control for the test above: the same launch with a good file
    mints, so `session_dirs` sees a session directory when there is one."""
    home = tmp_path / "scratch-home"
    w(host_file(home), {"model": "fake-host"})
    crew = w(tmp_path / "crew.json", {"effortLevel": "fake-crew"})
    result, _record, _work = launch_e2e(tmp_path, "scout", "--settings", str(crew),
                                        extra_env={"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    assert len(session_dirs(tmp_path)) == 1


@pytest.mark.parametrize("tail, said", [
    (("--settings", "nope.json"), "cannot be read"),
    (("--settings", "{a}", "--settings", "{b}"), "--settings 2 times"),
])
def test_the_rehearsal_refuses_before_the_mint(tmp_path, scratch, monkeypatch, tail, said):
    """The refusal is the rehearsal's: `prepare` raises it, before `mint`
    is reached (mint is made to fail loudly here)."""
    from sessiontools.start import plan as plan_mod

    def no_mint(*_args, **_kwargs):
        raise AssertionError("minted before the fold was checked")

    monkeypatch.setattr(plan_mod, "mint", no_mint)
    w(host_file(scratch), {"model": "fake-host"})
    a = w(tmp_path / "a.json", {"effortLevel": "fake-a"})
    b = w(tmp_path / "b.json", {"model": "fake-b"})
    with pytest.raises(LaunchError) as raised:
        plan(tmp_path, scratch, argv_tail=tuple(t.format(a=a, b=b) for t in tail))
    assert raised.value.code == 2 and said in str(raised.value)


def test_with_no_claude_code_file_a_missing_crew_settings_still_launches(tmp_path):
    """No fold, no rehearsal: with no claude-code file anywhere and a
    renderer chosen (no `tui` default), the crew's `--settings` goes to
    claude as given, even one that does not exist."""
    home = tmp_path / "scratch-home"
    home.mkdir()
    chose_a_renderer(home)
    result, record, _work = launch_e2e(tmp_path, "scout", "--settings", "nope.json",
                                       extra_env={"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert argv[argv.index("--settings") + 1] == "nope.json"


def test_on_a_fresh_host_a_missing_crew_settings_is_refused_before_the_mint(tmp_path):
    """No claude-code file and no renderer chosen: the door carries the
    `tui` default, so the crew file is folded, and one that cannot be read
    is refused (exit 2) before anything is minted, as on any host with a
    claude-code file."""
    home = tmp_path / "scratch-home"
    home.mkdir()
    result, record, _work = launch_e2e(tmp_path, "scout", "--settings", "nope.json",
                                       extra_env={"HOME": str(home)})
    assert result.returncode == 2, result.stdout
    assert "nope.json" in result.stderr and "cannot be read" in result.stderr
    assert not (record / "argv").exists()
    sessions = tmp_path / "agents" / "sessions"
    assert not sessions.exists() or not list(sessions.iterdir()), "minted an identity"


def test_a_resume_rehearses_the_fold_before_the_mint(tmp_path, scratch, monkeypatch, capsys):
    """A resume's crew `--settings` is checked before the mint too, under
    the directory the resume runs in; the resume's cwd line is not said
    by the rehearsal."""
    from sessiontools.start import plan as plan_mod

    def no_mint(*_args, **_kwargs):
        raise AssertionError("minted before the fold was checked")

    monkeypatch.setattr(plan_mod, "mint", no_mint)
    w(host_file(scratch), {"model": "fake-host"})
    with pytest.raises(LaunchError) as raised:
        plan(tmp_path, scratch, argv_tail=("--resume", SID, "--settings", "nope.json"))
    assert raised.value.code == 2 and "cannot be read" in str(raised.value)
    assert str(tmp_path / "work" / "nope.json") in str(raised.value)
    assert "resuming from" not in capsys.readouterr().err
