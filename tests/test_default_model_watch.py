"""`session default-model watch` and `unit`: the pin made standing.

`watch` is driven with an injected sleep (which doubles as "what happens
between two polls"), clock and tick budget, so no test waits on a real
timer. `unit` only prints: every test that renders one also fences off
anything that could install or load it.

Every test runs under the conftest's temporary HOME and state dir, with
CLAUDE_CONFIG_DIR scrubbed; a guard asserts every watched path lies under
the test's temp tree, so no test reaches a real settings.json.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest
from settings_support import host, write_session_settings

from sessiontools import cli, default_model_cli, settings
from sessiontools import default_model as dm
from sessiontools import default_model_watch as dmw

SAMPLE = '{\n  "env": {"ANTHROPIC_MODEL": "x"},\n  "model": "sonnet",\n  "theme": "dark"\n}\n'


@pytest.fixture(autouse=True)
def _under_tmp(tmp_path_factory):
    base = tmp_path_factory.getbasetemp().resolve()
    for p in dmw.watched_paths():
        assert p.resolve().is_relative_to(base), p
    assert dm.state_dir().resolve().is_relative_to(base)


@pytest.fixture
def home() -> Path:
    return Path(os.environ["HOME"])


@pytest.fixture
def claude(home) -> Path:
    d = home / ".claude"
    d.mkdir()
    return d


@pytest.fixture
def no_service_manager(monkeypatch):
    """No launchctl or systemctl, by any door: each process start is looked
    at and one naming either fails the test."""
    def fence(real):
        def run(args, *a, **k):
            words = [args] if isinstance(args, str | bytes | os.PathLike) else list(args)
            name = os.path.basename(str(words[0]).split()[0]) if words else ""
            if name in ("launchctl", "systemctl"):
                raise AssertionError(f"{name} was run: {args!r}")
            return real(args, *a, **k)
        return run
    monkeypatch.setattr(subprocess, "run", fence(subprocess.run))
    monkeypatch.setattr(subprocess, "Popen", fence(subprocess.Popen))
    monkeypatch.setattr(os, "system", fence(os.system))


def pin(home: Path, value) -> Path:
    return write_session_settings(home, {"model": {"default": value}})


def put(claude: Path, text: str) -> Path:
    p = claude / "settings.json"
    p.write_text(text)
    return p


def model_of(path: Path):
    return json.loads(path.read_text())["model"]


class Lines:
    def __init__(self):
        self.said: list[str] = []
        self.warned: list[str] = []

    def say(self, line: str) -> None:
        self.said.append(line)

    def warn(self, line: str) -> None:
        self.warned.append(line)


def run_watch(lines: Lines, steps=(), ticks: int | None = None, **kw) -> dmw.Watched:
    """`watch` with a sleep that runs STEPS[i] before poll i (then nothing),
    and a tick budget of len(STEPS) unless TICKS says otherwise."""
    steps = list(steps)
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        i = len(slept) - 1
        if i < len(steps) and steps[i] is not None:
            steps[i]()

    kw.setdefault("clock", lambda: 1_800_000_000.0 + len(slept))
    return dmw.watch(say=lines.say, warn=lines.warn, sleep=sleep,
                     ticks=len(steps) if ticks is None else ticks, **kw)


# --- what is watched ------------------------------------------------------------------------------

def test_watched_paths_are_settings_json_and_the_session_host_settings(home, claude):
    folder = Path(os.path.abspath(host(home, "session")))
    assert dmw.watched_paths() == (claude / "settings.json", folder / "session.json",
                                   folder / "session.d", folder)


def test_a_symlinked_settings_json_watches_its_target_too(home, claude, tmp_path):
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir()
    target.write_text(SAMPLE)
    (claude / "settings.json").symlink_to(target)
    paths = dmw.watched_paths()
    assert paths[:2] == (claude / "settings.json", target)


def test_claude_config_dir_moves_the_watched_settings_json(home, tmp_path, monkeypatch):
    moved = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    assert dmw.watched_paths()[0] == moved / "settings.json"


def test_a_session_settings_file_variable_moves_the_watched_host_file(home, tmp_path,
                                                                      monkeypatch):
    """No such variable exists for `session` today; when one does, the
    watched host file follows it the way the settings reader does."""
    monkeypatch.setitem(settings.SETTINGS_FILE_ENV, "session", ("SESSION_SETTINGS_TEST",))
    f = tmp_path / "elsewhere" / "mine.json"
    monkeypatch.setenv("SESSION_SETTINGS_TEST", str(f))
    assert dmw.watched_paths()[1:] == (f, f.parent / "session.d", f.parent)


# --- watch: unset is a no-op ------------------------------------------------------------------

UNSET_FILES = [None, {}, {"model": {}}, {"model": {"default": None}},
               {"model": {"default": ""}}, {"model": {"default": 7}}, {"model": "opus"},
               "{ not json"]


@pytest.mark.parametrize("data", UNSET_FILES)
def test_watch_unset_starts_no_watcher_and_writes_nothing(home, claude, data):
    if data is not None:
        write_session_settings(home, data)
    p = put(claude, SAMPLE.replace('"sonnet"', '"fable"'))
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    lines = Lines()
    got = run_watch(lines, steps=[lambda: pytest.fail("polled while unset")], ticks=1)
    assert got == dmw.Watched(0, 0, 0)
    assert len(lines.said) == 1 and lines.said[0].endswith(": nothing to watch")
    assert lines.warned == []
    assert p.read_bytes() == before and p.stat().st_mtime_ns == mtime
    assert not (dm.state_dir()).exists()


def test_cli_watch_unset_says_so_and_exits_0(home, claude, capsys):
    put(claude, SAMPLE)
    assert cli.main(["default-model", "watch"]) == 0
    out, err = capsys.readouterr()
    assert out == "default-model watch: model.default is not set: nothing to watch\n"
    assert err == ""
    assert not dm.state_dir().exists()


# --- watch: refused at the start ------------------------------------------------------------

@pytest.mark.parametrize("value", ["fable", "Fable", "claude-fable-5-1", "not-a-model"])
def test_watch_a_refused_pin_is_exit_3_and_polls_nothing(home, claude, value):
    pin(home, value)
    p = put(claude, SAMPLE)
    lines = Lines()
    got = run_watch(lines, steps=[lambda: pytest.fail("polled")], ticks=1)
    assert got == dmw.Watched(3, 0, 0)
    assert lines.said == [] and len(lines.warned) == 1
    assert "nothing watched, nothing written" in lines.warned[0]
    assert p.read_text() == SAMPLE
    assert not dm.state_dir().exists()


def test_cli_watch_fable_is_exit_3_on_stderr(home, claude, capsys):
    pin(home, "fable")
    put(claude, SAMPLE)
    assert cli.main(["default-model", "watch"]) == 3
    out, err = capsys.readouterr()
    assert out == "" and "Fable is never pinned" in err


# --- watch: set ---------------------------------------------------------------------------------

def test_watch_enforces_once_at_the_start(home, claude):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    lines = Lines()
    got = run_watch(lines, ticks=0)
    assert got == dmw.Watched(0, 0, 1)
    assert model_of(p) == "opus"
    assert lines.said == [(f'default-model: model "sonnet" -> "opus" in {p} '
                           f'(backup {dm.backups()[0]})')]


def test_an_unchanged_tick_runs_no_pass(home, claude, monkeypatch):
    pin(home, "opus")
    p = put(claude, SAMPLE.replace('"sonnet"', '"opus"'))
    calls = []
    real = dm.enforce
    monkeypatch.setattr(dm, "enforce", lambda *a, **k: calls.append(1) or real(*a, **k))
    lines = Lines()
    got = run_watch(lines, steps=[None, None, None])
    assert got == dmw.Watched(0, 3, 1) and len(calls) == 1
    assert dm.backups() == [] and model_of(p) == "opus"


def test_a_rewrite_between_ticks_is_put_back_on_the_next_tick(home, claude):
    pin(home, "opus")
    p = put(claude, SAMPLE.replace('"sonnet"', '"opus"'))
    rewritten = SAMPLE.replace('"sonnet"', '"claude-sonnet-4-5"')
    lines = Lines()
    got = run_watch(lines, steps=[lambda: p.write_text(rewritten), None, None])
    # tick 1 sees the rewrite and puts it back; tick 2 sees our own write
    # and finds it in sync; tick 3 sees nothing.
    assert got == dmw.Watched(0, 3, 3)
    assert p.read_text() == SAMPLE.replace('"sonnet"', '"opus"')
    [backup] = dm.backups()
    assert backup.read_text() == rewritten
    # The write is said once; the in-sync at the start is said, its echo
    # after our own write is not.
    assert lines.said == [f'default-model: model is "opus" in {p}: in sync',
                          (f'default-model: model "claude-sonnet-4-5" -> "opus" in {p} '
                           f'(backup {backup})')]
    log = (dm.state_dir() / "log.jsonl").read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["before"] == "claude-sonnet-4-5"


def test_a_deleted_model_key_is_inserted_on_the_next_tick(home, claude):
    pin(home, "opus")
    p = put(claude, '{\n  "model": "opus",\n  "theme": "dark"\n}\n')
    run_watch(Lines(), steps=[lambda: p.write_text('{\n  "theme": "dark"\n}\n'), None])
    assert p.read_text() == '{\n  "model": "opus",\n  "theme": "dark"\n}\n'


def test_setting_the_pin_takes_effect_without_a_settings_json_change(home, claude):
    """The pin's own file is watched, so a new pin lands now."""
    p = put(claude, SAMPLE)
    pin(home, "opus")
    lines = Lines()
    run_watch(lines, steps=[lambda: pin(home, "haiku"), None])
    assert model_of(p) == "haiku"


def test_a_new_drop_in_takes_effect(home, claude):
    """A `session.d/` that did not exist at the start is seen when it does."""
    pin(home, "opus")
    p = put(claude, SAMPLE)

    def drop_in():
        write_session_settings(home, {"model": {"default": "haiku"}},
                               name="session.d/50-pin.json")

    run_watch(Lines(), steps=[drop_in, None])
    assert model_of(p) == "haiku"


def test_clearing_the_pin_while_watching_stops_the_writes(home, claude):
    f = pin(home, "opus")
    p = put(claude, SAMPLE)

    def clear_and_rewrite():
        f.write_text("{}")
        p.write_text(SAMPLE.replace('"sonnet"', '"fable"'))

    lines = Lines()
    got = run_watch(lines, steps=[clear_and_rewrite, None])
    assert got.code == 0
    assert model_of(p) == "fable"
    assert len(dm.backups()) == 1            # the start's write only
    assert lines.said[-1] == "default-model: model.default is not set: nothing to do"


def test_a_bad_pin_while_watching_is_said_and_never_acted_on(home, claude):
    f = pin(home, "opus")
    p = put(claude, SAMPLE)

    def to_fable():
        f.write_text(json.dumps({"model": {"default": "fable"}}))
        p.write_text(SAMPLE.replace('"sonnet"', '"haiku"'))

    lines = Lines()
    got = run_watch(lines, steps=[to_fable])
    assert got.code == 0
    assert model_of(p) == "haiku"
    assert len(lines.warned) == 1 and "Fable is never pinned" in lines.warned[0]


def test_a_pass_that_raises_does_not_stop_the_watcher(home, claude, monkeypatch):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    real = dm.enforce
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(dm, "enforce", flaky)
    lines = Lines()
    rewrite = SAMPLE.replace('"sonnet"', '"claude-sonnet-4-5"')
    got = run_watch(lines, steps=[lambda: p.write_text(rewrite),
                                  lambda: p.write_text(SAMPLE)])
    assert got == dmw.Watched(0, 2, 3)
    assert any("the pass failed" in w and "boom" in w for w in lines.warned)
    assert model_of(p) == "opus"


def test_stopped_ends_the_loop(home, claude):
    pin(home, "opus")
    put(claude, SAMPLE.replace('"sonnet"', '"opus"'))
    polls = []
    got = dmw.watch(say=lambda _l: None, warn=lambda _l: None,
                    sleep=lambda _s: polls.append(1), stopped=lambda: len(polls) >= 2)
    assert got == dmw.Watched(0, 2, 1)


def test_watch_sleeps_the_interval(home, claude):
    pin(home, "opus")
    put(claude, SAMPLE)
    slept = []
    dmw.watch(say=lambda _l: None, interval=2.5, sleep=slept.append, ticks=2)
    assert slept == [2.5, 2.5]


def test_the_injected_clock_stamps_the_backup(home, claude):
    pin(home, "opus")
    put(claude, SAMPLE)
    import time
    stamp = 1_700_000_000.0
    dmw.watch(say=lambda _l: None, clock=lambda: stamp, sleep=lambda _s: None, ticks=0)
    [backup] = dm.backups()
    assert backup.name == "settings.json." + time.strftime("%Y%m%d-%H%M%S",
                                                           time.localtime(stamp))


def test_cli_watch_passes_the_interval_and_an_interrupt_is_exit_0(home, monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        raise KeyboardInterrupt

    monkeypatch.setattr(dmw, "watch", fake)
    assert cli.main(["default-model", "watch", "--interval", "0.5"]) == 0
    assert seen["interval"] == 0.5


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "soon"])
def test_cli_watch_a_bad_interval_is_exit_2(capsys, value):
    with pytest.raises(SystemExit) as e:
        cli.main(["default-model", "watch", "--interval", value])
    assert e.value.code == 2


# --- unit: launchd ------------------------------------------------------------------------------

def unit_out(capsys, kind: str) -> tuple[int, str, str]:
    code = cli.main(["default-model", "unit", kind])
    out, err = capsys.readouterr()
    return code, out, err


def test_launchd_is_a_plist_naming_the_absolute_session_and_the_watched_paths(
        home, claude, capsys, no_service_manager):
    pin(home, "opus")
    code, out, err = unit_out(capsys, "launchd")
    assert code == 0 and err == ""
    data = plistlib.loads(out.encode())
    assert data["Label"] == "dev.sessionkit.tools.default-model"
    program, *args = data["ProgramArguments"]
    assert Path(program).is_absolute() and Path(program).name == "session"
    assert args == ["default-model", "enforce"]
    assert data["WatchPaths"] == [str(p) for p in dmw.watched_paths()]
    assert data["RunAtLoad"] is True
    assert "KeepAlive" not in data
    assert data["ThrottleInterval"] == dmw.THROTTLE
    log = str(home / "Library" / "Logs" / "sessiontools" / "default-model.log")
    assert data["StandardOutPath"] == data["StandardErrorPath"] == log


def test_launchd_watches_the_pin_files_too(home, claude, capsys, no_service_manager):
    """settings.json AND the session tool's host settings."""
    pin(home, "opus")
    data = plistlib.loads(unit_out(capsys, "launchd")[1].encode())
    folder = os.path.abspath(host(home, "session"))
    assert str(claude / "settings.json") in data["WatchPaths"]
    assert {os.path.join(folder, "session.json"), os.path.join(folder, "session.d"),
            folder} <= set(data["WatchPaths"])


def test_unit_installs_nothing_and_loads_nothing(home, claude, capsys, no_service_manager):
    pin(home, "opus")
    before = sorted(p for p in home.rglob("*"))
    for kind in dmw.KINDS:
        assert unit_out(capsys, kind)[0] == 0
    assert sorted(p for p in home.rglob("*")) == before
    assert not (home / "Library").exists() and not (home / ".config").exists()


def test_launchd_carries_the_env_set_at_render_time(home, claude, capsys, tmp_path,
                                                    monkeypatch, no_service_manager):
    """The job reads the same settings.json and state dir as the shell that rendered it."""
    pin(home, "opus")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("SESSION_STATE_DIR", "~/my-state")
    data = plistlib.loads(unit_out(capsys, "launchd")[1].encode())
    assert data["EnvironmentVariables"] == {"CLAUDE_CONFIG_DIR": str(claude),
                                            "SESSION_STATE_DIR": str(home / "my-state")}


def test_launchd_carries_no_env_when_none_is_set(home, claude, capsys, monkeypatch,
                                                 no_service_manager):
    pin(home, "opus")
    monkeypatch.delenv("SESSION_STATE_DIR")
    data = plistlib.loads(unit_out(capsys, "launchd")[1].encode())
    assert "EnvironmentVariables" not in data


def test_an_empty_variable_is_not_carried(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    assert "CLAUDE_CONFIG_DIR" not in dmw.rendered_env()


def test_a_session_settings_file_variable_is_carried(home, tmp_path, monkeypatch):
    monkeypatch.setitem(settings.SETTINGS_FILE_ENV, "session", ("SESSION_SETTINGS_TEST",))
    monkeypatch.setenv("SESSION_SETTINGS_TEST", str(tmp_path / "s.json"))
    assert dmw.rendered_env()["SESSION_SETTINGS_TEST"] == str(tmp_path / "s.json")


# --- unit: systemd ------------------------------------------------------------------------------

def split_units(text: str) -> dict[str, list[str]]:
    files: dict[str, list[str]] = {}
    name = None
    for line in text.splitlines():
        if line.startswith("# file: "):
            name = line.removeprefix("# file: ")
            files[name] = []
        else:
            assert name, "text before the first # file: line"
            files[name].append(line)
    return files


def test_systemd_is_a_path_and_a_service_unit(home, claude, capsys, no_service_manager):
    pin(home, "opus")
    code, out, err = unit_out(capsys, "systemd")
    assert code == 0 and err == ""
    files = split_units(out)
    assert list(files) == [dmw.SYSTEMD_PATH_UNIT, dmw.SYSTEMD_SERVICE_UNIT]
    path_unit, service = files[dmw.SYSTEMD_PATH_UNIT], files[dmw.SYSTEMD_SERVICE_UNIT]
    assert [ln.removeprefix("PathChanged=") for ln in path_unit
            if ln.startswith("PathChanged=")] == [str(p) for p in dmw.watched_paths()]
    assert f"Unit={dmw.SYSTEMD_SERVICE_UNIT}" in path_unit
    assert "WantedBy=default.target" in path_unit
    assert "Type=oneshot" in service
    [execstart] = [ln for ln in service if ln.startswith("ExecStart=")]
    program, *args = execstart.removeprefix("ExecStart=").split(" ")
    assert Path(program).is_absolute() and Path(program).name == "session"
    assert args == ["default-model", "enforce"]


def test_systemd_carries_the_env_set_at_render_time(home, claude, capsys, monkeypatch,
                                                    no_service_manager):
    pin(home, "opus")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    service = split_units(unit_out(capsys, "systemd")[1])[dmw.SYSTEMD_SERVICE_UNIT]
    assert f"Environment=CLAUDE_CONFIG_DIR={claude}" in service
    assert any(ln.startswith("Environment=SESSION_STATE_DIR=") for ln in service)


@pytest.mark.parametrize("raw,want", [
    ("/a/b", "/a/b"),
    ("/a b/c", '"/a b/c"'),
    ("/100%/x", "/100%%/x"),
    ('/q"x', '"/q\\"x"'),
    ("/back\\slash", '"/back\\\\slash"'),
])
def test_a_systemd_word_is_quoted_and_escaped(raw, want):
    assert dmw._sd_word(raw) == want


def test_a_systemd_watched_path_doubles_a_percent():
    assert dmw._sd("/x/100%") == "/x/100%%"


# --- unit: refusals and the unset note ---------------------------------------------------------

@pytest.mark.parametrize("kind", dmw.KINDS)
@pytest.mark.parametrize("value", ["fable", "FABLE-6", "not-a-model"])
def test_unit_for_a_refused_pin_is_exit_3_and_prints_no_unit(home, capsys, kind, value,
                                                             no_service_manager):
    pin(home, value)
    code, out, err = unit_out(capsys, kind)
    assert code == 3 and out == ""
    assert err.startswith("default-model unit: ") and "no unit printed" in err


@pytest.mark.parametrize("kind", dmw.KINDS)
def test_unit_while_unset_prints_the_unit_and_says_it_will_do_nothing(home, capsys, kind,
                                                                      no_service_manager):
    code, out, err = unit_out(capsys, kind)
    assert code == 0 and out
    assert "the unit will do nothing until model.default is set" in err


@pytest.mark.parametrize("kind", dmw.KINDS)
def test_unit_with_no_session_found_is_exit_3(home, capsys, kind, monkeypatch,
                                              no_service_manager):
    monkeypatch.setattr(dmw, "session_bin", lambda _e=None: None)
    code, out, err = unit_out(capsys, kind)
    assert code == 3 and out == "" and "no `session` console script" in err


def test_unit_needs_a_kind(capsys):
    for argv in (["unit"], ["unit", "bogus"], ["unit", "launchd", "extra"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["default-model", *argv])
        assert e.value.code == 2


def test_the_core_unit_refuses_an_unknown_kind():
    with pytest.raises(ValueError):
        dmw.unit("cron")


# --- help ----------------------------------------------------------------------------------------

def test_help_gives_both_install_steps_and_run_one_pinner(capsys):
    with pytest.raises(SystemExit):
        cli.main(["default-model", "--help"])
    out = capsys.readouterr().out
    # Rendered beside you and moved only when it worked: a refusal prints
    # nothing, and a redirect into LaunchAgents would leave an empty plist.
    assert f"session default-model unit launchd > {dmw.LABEL}.plist &&" in out
    assert f"mv {dmw.LABEL}.plist ~/Library/LaunchAgents/" in out
    assert "unit launchd > ~/Library/LaunchAgents/" not in out
    assert f"launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{dmw.LABEL}.plist" in out
    assert "mkdir -p ~/Library/Logs/sessiontools" in out
    assert f"systemctl --user enable --now {dmw.SYSTEMD_PATH_UNIT}" in out
    assert "Run one pinner" in " ".join(out.split())
    assert "nothing here installs or loads one" in " ".join(out.split())


def test_the_awk_line_in_help_splits_the_systemd_stream(home, claude, tmp_path, capsys,
                                                        monkeypatch):
    """The help's split command, run for real on the real stream, in a temp
    dir: each `# file:` block lands in its own file."""
    import shutil
    awk = shutil.which("awk")
    if awk is None:
        pytest.skip("no awk")
    pin(home, "opus")
    text, _ = dmw.unit("systemd")
    out = tmp_path / "units"
    out.mkdir()
    # The fixture fences subprocess; this test does not use it.
    subprocess.run([awk, "-v", f"d={out}", '/^# file: /{f = d "/" $3} {print > f}'],
                   input=text, text=True, check=True)
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [dmw.SYSTEMD_PATH_UNIT, dmw.SYSTEMD_SERVICE_UNIT])
    assert "ExecStart=" in (out / dmw.SYSTEMD_SERVICE_UNIT).read_text()
    assert default_model_cli.EPILOG.count('/^# file: /{f = d "/" $3} {print > f}') == 1
