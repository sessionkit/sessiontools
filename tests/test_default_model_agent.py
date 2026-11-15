"""`session default-model agent on|off|status`: the enforcer's launchd agent.

No test runs a real launchctl. conftest sets SESSIONTOOLS_DEFAULT_MODEL_NO_LAUNCHCTL,
so the real `_launchctl` raises, and the `fence` fixture here replaces it with
a double that records every call and keeps the loaded set in memory (the
model is tests/test_pane_state_agent.py). HOME and SESSION_STATE_DIR are
fresh per test (conftest), so the plist, the log and the service file land
in scratch dirs, never in the real ~/Library/LaunchAgents.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

from sessiontools import cli, default_model_cli, service_file, settings
from sessiontools import default_model as dm
from sessiontools import default_model_agent as agent
from sessiontools import default_model_watch as dmw

REPO = Path(__file__).resolve().parent.parent
CHANGING = {"bootstrap", "bootout", "load", "unload", "kickstart", "kill", "enable", "disable"}
SERVICE_KEYS = ["service", "name", "label", "enabled", "install", "uninstall", "description",
                "kind"]
#: `unit launchd` and `unit systemd` at the base this slice built on, for one
#: fixed environment (HOME /home-of-test, `session` at /opt/st/bin/session,
#: CLAUDE_CONFIG_DIR and SESSION_STATE_DIR unset, the pin unset). The agent
#: must not move a byte of either.
BASE_LAUNCHD = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>dev.sessionkit.tools.default-model</string>
	<key>ProgramArguments</key>
	<array>
		<string>/opt/st/bin/session</string>
		<string>default-model</string>
		<string>enforce</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>/home-of-test/.sessionkit/tools/log/default-model.log</string>
	<key>StandardOutPath</key>
	<string>/home-of-test/.sessionkit/tools/log/default-model.log</string>
	<key>ThrottleInterval</key>
	<integer>10</integer>
	<key>WatchPaths</key>
	<array>
		<string>/home-of-test/.claude/settings.json</string>
		<string>/home-of-test/.sessionkit/tools/settings/session.json</string>
		<string>/home-of-test/.sessionkit/tools/settings/session.d</string>
		<string>/home-of-test/.sessionkit/tools/settings</string>
	</array>
</dict>
</plist>
"""
BASE_SYSTEMD = """\
# file: sessiontools-default-model.path
[Unit]
Description=Put the pinned default model back in Claude Code's settings.json

[Path]
PathChanged=/home-of-test/.claude/settings.json
PathChanged=/home-of-test/.sessionkit/tools/settings/session.json
PathChanged=/home-of-test/.sessionkit/tools/settings/session.d
PathChanged=/home-of-test/.sessionkit/tools/settings
Unit=sessiontools-default-model.service

[Install]
WantedBy=default.target

# file: sessiontools-default-model.service
[Unit]
Description=One pass of session default-model enforce

[Service]
Type=oneshot
ExecStart=/opt/st/bin/session default-model enforce
"""


@pytest.fixture
def fence(monkeypatch):
    """The launchctl double, macOS assumed, no waits."""
    monkeypatch.setattr(agent, "_macos", lambda: True)
    monkeypatch.setattr(agent, "POLL", 0.0)
    monkeypatch.setattr(agent, "RETRY_PAUSE", 0.0)
    calls: list[tuple] = []
    loaded: set[str] = set()
    state = {"bootstrap_fail": 0, "stay_loaded": False, "pid": None, "last_exit": 0,
             "loads": True, "on_bootstrap": None}

    def launchctl(*a):
        calls.append(a)
        verb = a[0]
        if verb == "print":
            label = a[1].split("/")[-1]
            if label in loaded:
                body = f"\tlast exit code = {state['last_exit']}\n"
                if state["pid"] is not None:
                    body += f"\tpid = {state['pid']}\n"
                return subprocess.CompletedProcess(a, 0, f"{label} = {{\n{body}}}\n", "")
            return subprocess.CompletedProcess(a, 113, "", "Could not find service")
        if verb == "bootstrap":
            if state["bootstrap_fail"]:
                state["bootstrap_fail"] -= 1
                return subprocess.CompletedProcess(a, 5, "", "Bootstrap failed: 5: Input/output error")
            data = plistlib.loads(Path(a[2]).read_bytes())
            if state["on_bootstrap"]:
                state["on_bootstrap"](data)
            if state["loads"]:
                loaded.add(data["Label"])
            return subprocess.CompletedProcess(a, 0, "", "")
        if verb == "bootout":
            if not state["stay_loaded"]:
                loaded.discard(a[1].split("/")[-1])
            return subprocess.CompletedProcess(a, 0, "", "")
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(agent, "_launchctl", launchctl)
    return {"calls": calls, "loaded": loaded, "state": state}


def verbs(calls) -> list[str]:
    return [c[0] for c in calls]


def run(argv, capsys) -> tuple[int, str, str]:
    rc = cli.main(["default-model", "agent", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def service_json() -> Path:
    return settings.services_dir() / "default-model.json"


def pin(value: str) -> None:
    folder = settings.host_dir(dm.SETTINGS_TOOL)[0]
    Path(folder).mkdir(parents=True, exist_ok=True)
    (Path(folder) / "session.json").write_text(json.dumps({"model": {"default": value}}))


def nothing_written() -> bool:
    return not agent.plist_path().exists() and not settings.services_dir().exists()


# --- the fence ----------------------------------------------------------------------

def test_the_real_launchctl_is_fenced_off_in_tests():
    assert os.environ.get(agent.FENCE_ENV) == "1"
    assert agent.FENCE_ENV == "SESSIONTOOLS_DEFAULT_MODEL_NO_LAUNCHCTL"
    with pytest.raises(RuntimeError, match="fenced off"):
        agent._launchctl("print", agent._target())


def test_the_fence_execs_no_launchctl_even_one_first_on_path(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    exe = bin_ / "launchctl"
    exe.write_text(f"#!/bin/sh\necho \"$@\" >> {marker}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setattr(agent, "_macos", lambda: True)
    with pytest.raises(RuntimeError, match="fenced off"):
        agent._launchctl("bootstrap", agent._domain(), "/nonexistent.plist")
    with pytest.raises(RuntimeError, match="fenced off"):
        cli.main(["default-model", "agent", "status"])
    assert not marker.exists()


def test_conftest_sets_the_fence_beside_the_other_two():
    src = (REPO / "tests/conftest.py").read_text()
    assert f'monkeypatch.setenv("{agent.FENCE_ENV}", "1")' in src
    for other in ("SESSIONTOOLS_PANE_STATE_NO_LAUNCHCTL", "SESSIONTOOLS_SOUND_NO_LAUNCHCTL"):
        assert f'monkeypatch.setenv("{other}", "1")' in src


# --- the label and the plist: one renderer ----------------------------------------------

def test_the_label_is_the_renderers_never_respelled():
    src = (REPO / "src/sessiontools/default_model_agent.py").read_text()
    assert agent.LABEL is dmw.LABEL
    assert f'"{dmw.LABEL}"' not in src
    assert agent.plist_path().name == f"{dmw.LABEL}.plist"
    assert agent.plist_path().parent == Path(os.environ["HOME"]) / "Library" / "LaunchAgents"
    assert agent.LABEL in agent.EPILOG


def test_the_plist_on_writes_is_the_unit_launchd_text_byte_for_byte(fence, capsys):
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert agent.plist_path().read_bytes() == dmw.launchd()[0].encode()
    assert agent.log_path() == dmw.log_path()
    assert agent.log_path().parent.is_dir()                   # launchd makes no log folder
    assert oct(agent.plist_path().stat().st_mode & 0o777) == "0o644"


def test_the_plist_carries_the_pin_environment_as_unit_does(fence, capsys, monkeypatch, tmp_path):
    claude = tmp_path / "claude"
    claude.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    assert run(["on"], capsys)[0] == 0
    data = plistlib.loads(agent.plist_path().read_bytes())
    assert data["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"] == str(claude)
    assert agent.plist_path().read_bytes() == dmw.launchd()[0].encode()


@pytest.fixture
def fixed_env(monkeypatch, tmp_path):
    """The fixed environment BASE_LAUNCHD and BASE_SYSTEMD were rendered in."""
    monkeypatch.setenv("HOME", "/home-of-test")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("SESSION_STATE_DIR", raising=False)
    monkeypatch.setattr(dmw, "session_bin", lambda environ=None: Path("/opt/st/bin/session"))


def test_unit_launchd_and_systemd_are_byte_identical_to_the_base(fixed_env, capsys):
    """design 4.6: default_model_watch is not edited, so `unit` prints what it
    printed before the agent existed."""
    for kind, want in (("launchd", BASE_LAUNCHD), ("systemd", BASE_SYSTEMD)):
        assert cli.main(["default-model", "unit", kind]) == 0
        assert capsys.readouterr().out == want, kind


# --- the service record ---------------------------------------------------------------

def test_the_service_record_carries_every_designed_key():
    r = agent.service_record(True)
    assert list(r) == SERVICE_KEYS
    assert r == {"service": 1, "name": "default-model",
                 "label": "dev.sessionkit.tools.default-model", "enabled": True,
                 "install": "session default-model agent on",
                 "uninstall": "session default-model agent off",
                 "description": "puts the pinned default model back in Claude Code's "
                                "settings.json",
                 "kind": "on-demand"}
    assert agent.service_record(False)["enabled"] is False
    assert service_file.check(r) == []
    assert r["name"] == agent.SERVICE == agent.LABEL.removeprefix("dev.sessionkit.tools.")


# --- on ------------------------------------------------------------------------------

def test_on_writes_the_plist_and_file_boots_out_then_bootstraps(fence, capsys):
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    changing = [c for c in fence["calls"] if c[0] in CHANGING]
    assert changing == [("bootout", agent._target()),
                        ("bootstrap", agent._domain(), str(agent.plist_path()))]
    assert agent.LABEL in fence["loaded"]
    assert f"plist  {agent.plist_path()}" in out
    assert f"service {service_json()} (enabled: true)" in out
    n = len(dmw.watched_paths())
    assert out.rstrip().splitlines()[-1] == (
        f"on     {agent.LABEL}: loaded (on demand: runs enforce on each change of its "
        f"{n} watched paths)")


def test_on_with_the_pin_unset_says_the_job_waits_for_it(fence, capsys):
    rc, _, err = run(["on"], capsys)
    assert rc == 0
    assert "the unit will do nothing until model.default is set" in err


def test_on_with_a_pin_set_says_no_note(fence, capsys):
    pin("opus")
    rc, _, err = run(["on"], capsys)
    assert rc == 0 and err == ""


def test_on_proves_the_job_by_print_and_never_waits_for_a_pid(fence, capsys):
    fence["state"]["pid"] = None                     # on demand: no pid between runs
    assert run(["on"], capsys)[0] == 0
    assert verbs(fence["calls"])[-1] == "print"


def test_on_replaces_a_job_already_loaded(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    fence["calls"].clear()
    assert run(["on"], capsys)[0] == 0
    assert [c[0] for c in fence["calls"] if c[0] in CHANGING] == ["bootout", "bootstrap"]
    assert agent.LABEL in fence["loaded"]


def test_on_retries_a_bootstrap_once(fence, capsys):
    fence["state"]["bootstrap_fail"] = 1
    assert run(["on"], capsys)[0] == 0
    assert verbs(fence["calls"]).count("bootstrap") == 2


def test_a_failed_bootstrap_is_exit_4_with_the_file_enabled(fence, capsys):
    fence["state"]["bootstrap_fail"] = 2
    rc, out, _ = run(["on"], capsys)
    assert rc == dm.FAILED == 4
    assert "launchd: bootstrap failed (5)" in out
    assert json.loads(service_json().read_text())["enabled"] is True
    assert agent.plist_path().exists()


def test_bootstrap_0_but_not_loaded_is_exit_4(fence, capsys):
    fence["state"]["loads"] = False
    rc, out, _ = run(["on"], capsys)
    assert rc == 4
    assert "is not loaded" in out


def test_on_says_when_the_old_job_will_not_go(fence, capsys, monkeypatch):
    assert run(["on"], capsys)[0] == 0
    fence["state"]["stay_loaded"] = True
    monkeypatch.setattr(agent, "GONE_WAIT", 0.0)
    rc, out, _ = run(["on"], capsys)
    assert rc == 4 and "still loaded" in out


def test_a_failed_plist_write_is_exit_4_and_loads_nothing(fence, capsys):
    launch_agents = agent.plist_path().parent
    launch_agents.parent.mkdir(parents=True, exist_ok=True)
    launch_agents.write_text("not a folder\n")
    rc, out, _ = run(["on"], capsys)
    assert rc == 4
    assert f"plist  could not write {agent.plist_path()}: " in out
    assert not set(verbs(fence["calls"])) & CHANGING
    assert not settings.services_dir().exists()


# --- refusals: exit 3, nothing written --------------------------------------------------

@pytest.mark.parametrize("verb", [["on"], ["on", "--dry-run"], ["off"], ["off", "--dry-run"],
                                  ["status"]])
def test_refused_off_macos_exit_3_with_nothing_written(fence, capsys, monkeypatch, verb):
    monkeypatch.setattr(agent, "_macos", lambda: False)
    rc, out, err = run(verb, capsys)
    assert rc == dm.REFUSED == 3
    assert "macOS only" in err and out == ""
    assert nothing_written()
    assert fence["calls"] == []


def test_on_off_macos_names_the_linux_route(fence, capsys, monkeypatch):
    monkeypatch.setattr(agent, "_macos", lambda: False)
    _, _, err = run(["on"], capsys)
    assert "`session default-model unit systemd` prints the units" in err


@pytest.mark.parametrize("bad", ["fable", "not-a-model"])
@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_a_pin_enforce_would_refuse_is_exit_3_with_nothing_written(fence, capsys, bad, dry):
    pin(bad)
    rc, out, err = run(["on", *dry], capsys)
    assert rc == 3
    assert err.startswith(f"{agent.PROG} on: refused: ")
    assert err.rstrip().endswith("; nothing was changed")
    assert "no unit printed" not in err
    assert out == ""
    assert nothing_written()
    assert not agent.log_path().parent.exists()
    assert fence["calls"] == []


def test_no_session_script_is_exit_3_with_nothing_written(fence, capsys, monkeypatch):
    monkeypatch.setattr(dmw, "session_bin", lambda environ=None: None)
    rc, _, err = run(["on"], capsys)
    assert rc == 3 and "no `session` console script" in err
    assert nothing_written()


# --- dry run ----------------------------------------------------------------------------

def test_on_dry_run_prints_plist_and_file_and_runs_only_print(fence, capsys):
    rc, out, _ = run(["on", "--dry-run"], capsys)
    assert rc == 0
    assert f"plist  would write {agent.plist_path()}:\n{dmw.launchd()[0]}" in out + "\n"
    block = f"service would write {service_json()}:\n" \
            f"{service_file.render(agent.service_record(True))}"
    assert block in out + "\n"
    assert out.rstrip().splitlines()[-1] == ("dry run: nothing was written, and no launchctl "
                                             "verb that changes anything ran")
    assert nothing_written()
    assert not agent.log_path().parent.exists()
    assert set(verbs(fence["calls"])) <= {"print"}


def test_off_dry_run_prints_the_file_and_changes_nothing(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    before = service_json().read_bytes()
    fence["calls"].clear()
    rc, out, _ = run(["off", "--dry-run"], capsys)
    assert rc == 0
    assert f"launchd would: bootout {agent._target()} (loaded)" in out
    assert f"plist  would remove {agent.plist_path()} (present)" in out
    block = f"service would write {service_json()}:\n" \
            f"{service_file.render(agent.service_record(False))}"
    assert block in out + "\n"
    assert out.rstrip().splitlines()[-1] == ("dry run: nothing was removed or written, and no "
                                             "launchctl verb that changes anything ran")
    assert set(verbs(fence["calls"])) == {"print"}
    assert agent.plist_path().exists() and service_json().read_bytes() == before


# --- off --------------------------------------------------------------------------------

def test_on_then_off_leaves_one_file_enabled_true_then_false(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    assert sorted(settings.services_dir().iterdir()) == [service_json()]
    on = json.loads(service_json().read_text())
    assert list(on) == SERVICE_KEYS and on["enabled"] is True
    assert service_json().read_text() == service_file.render(agent.service_record(True))
    rc, out, err = run(["off"], capsys)
    assert rc == 0, out + err
    assert f"off    {agent.LABEL} (booted out); plist removed: {agent.plist_path()}" in out
    assert f"service {service_json()} (enabled: false)" in out
    assert agent.LABEL not in fence["loaded"] and not agent.plist_path().exists()
    assert sorted(settings.services_dir().iterdir()) == [service_json()]     # kept
    assert json.loads(service_json().read_text()) == {**on, "enabled": False}


def test_off_when_not_loaded_runs_no_bootout_and_writes_the_file(fence, capsys):
    rc, out, _ = run(["off"], capsys)
    assert rc == 0 and "was not loaded" in out
    assert "bootout" not in verbs(fence["calls"])
    assert json.loads(service_json().read_text())["enabled"] is False


def test_off_removes_a_hand_installed_plist(fence, capsys):
    path = agent.plist_path()
    path.parent.mkdir(parents=True)
    path.write_text(dmw.launchd()[0])
    fence["loaded"].add(agent.LABEL)
    assert run(["off"], capsys)[0] == 0
    assert not path.exists() and agent.LABEL not in fence["loaded"]


# --- a failed service write: the act is done, exit 4 -----------------------------------

def unwritable_services():
    """A file where the services folder goes: every write fails, for real."""
    settings.services_dir().parent.mkdir(parents=True, exist_ok=True)
    settings.services_dir().write_text("not a folder\n")


def test_a_failed_service_write_on_still_loads_the_job_and_exits_4(fence, capsys):
    unwritable_services()
    rc, out, err = run(["on"], capsys)
    assert rc == 4, out + err
    line = next(x for x in out.splitlines() if x.startswith("service could not write "))
    assert line.startswith(f"service could not write {service_json()}: ")
    assert line.endswith("; the previous file is unchanged")
    assert agent.LABEL in fence["loaded"]                               # the act is done
    assert [c[0] for c in fence["calls"] if c[0] in CHANGING] == ["bootout", "bootstrap"]
    assert f"on     {agent.LABEL}: loaded" in out
    assert agent.plist_path().read_bytes() == dmw.launchd()[0].encode()


def test_a_failed_service_write_on_off_still_boots_out_and_exits_4(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    before = service_json().read_bytes()
    os.chmod(settings.services_dir(), 0o500)
    try:
        rc, out, _ = run(["off"], capsys)
    finally:
        os.chmod(settings.services_dir(), 0o755)
    assert rc == 4
    assert "service could not write " in out and "the previous file is unchanged" in out
    assert agent.LABEL not in fence["loaded"] and not agent.plist_path().exists()
    assert service_json().read_bytes() == before                         # enabled true, whole


# --- status -----------------------------------------------------------------------------

def test_status_json_after_on(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    fence["state"].update(pid=777, last_exit=0)
    fence["calls"].clear()
    rc, out, _ = run(["status", "--json"], capsys)
    assert rc == 0
    s = json.loads(out)
    assert s == {"label": agent.LABEL, "plist": str(agent.plist_path()), "plist_present": True,
                 "program": [str(dmw.session_bin()), "default-model", "enforce"],
                 "loaded": True, "pid": 777, "last_exit": 0, "log": str(agent.log_path()),
                 "service": {"path": str(service_json()), "present": True, "enabled": True}}
    assert set(verbs(fence["calls"])) == {"print"}                       # reads only


def test_status_text_after_on_and_after_off(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    fence["state"]["last_exit"] = 3
    rc, out, _ = run(["status"], capsys)
    assert rc == 0
    assert f"plist  {agent.plist_path()} (present)" in out
    assert "runs   " in out and out.count("default-model enforce") == 1
    assert "loaded True\n" in out and "last exit 3" in out
    assert f"service {service_json()} (enabled: true)" in out
    assert run(["off"], capsys)[0] == 0
    _, out, _ = run(["status"], capsys)
    assert "(absent)" in out and "loaded False" in out and "last exit" not in out
    assert f"service {service_json()} (enabled: false)" in out


def test_status_names_an_absent_and_a_bad_service_file(fence, capsys):
    _, out, _ = run(["status"], capsys)
    assert f"service {service_json()} (absent)" in out
    service_json().parent.mkdir(parents=True)
    service_json().write_text("{not json")
    _, out, _ = run(["status"], capsys)
    assert f"service {service_json()} (not JSON)" in out
    _, out, _ = run(["status", "--json"], capsys)
    assert json.loads(out)["service"] == {"path": str(service_json()), "present": True,
                                          "enabled": None}


def test_print_reads_only_the_key_equals_n_forms(fence):
    fence["loaded"].add(agent.LABEL)
    fence["state"].update(pid=12, last_exit=-9)
    assert agent._print() == {"pid": 12, "last_exit": -9}
    fence["loaded"].clear()
    assert agent._print() is None


# --- dispatch, help and docs ------------------------------------------------------------

def test_the_group_hands_agent_to_this_module():
    from sessiontools import verbs as v
    group = v.resolve(cli.PARSERS["default-model"])
    assert group is default_model_cli.GROUP
    assert v.resolve(group.subs["agent"]) is agent.build_parser


@pytest.mark.parametrize("argv", [[], ["bogus"], ["on", "--json"], ["status", "--dry-run"]])
def test_bad_arguments_exit_2(capsys, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(["default-model", "agent", *argv])
    assert e.value.code == 2


def test_the_help_names_the_label_the_file_the_failure_line_and_the_codes(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["default-model", "agent", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    flat = " ".join(out.split())
    assert agent.LABEL in out
    assert "~/.sessionkit/tools/services/default-model.json" in out
    assert service_file.COULD_NOT_WRITE.format(path="PATH", reason="REASON") in out
    assert "then exits 4" in flat and "Opt-in" in flat
    assert "3 refused, nothing changed" in flat and "4 a plist or service-file write" in flat


def test_the_verb_help_lists_agent(capsys):
    with pytest.raises(SystemExit):
        cli.main(["default-model", "--help"])
    out = capsys.readouterr().out
    assert "show|list|enforce|watch|unit|agent" in out
    assert re.search(r"^\s+agent\s+on\|off\|status", out, re.MULTILINE)


def test_the_cli_tables_name_agent():
    assert "agent" in dict(cli.VERBS)["default-model"]
    assert "session default-model agent on|off|status" in cli.__doc__


def test_the_page_names_the_file_and_quotes_the_failure_line():
    page = (REPO / "docs" / "default-model.md").read_text()
    flat = " ".join(page.split())
    assert "~/.sessionkit/tools/services/default-model.json" in page
    assert service_file.COULD_NOT_WRITE.format(path="PATH", reason="REASON") in page
    for line in ("session default-model agent on", "session default-model agent off",
                 "session default-model agent status"):
        assert line in page, line
    assert "Nothing turns itself on." in flat
    assert "Nothing installs itself" not in flat


def test_nothing_turns_the_agent_on_but_the_verb():
    """Opt-in: no module but this one and the dispatcher reaches `cmd_on`."""
    hits = []
    for p in (REPO / "src").rglob("*.py"):
        if p.name in ("default_model_agent.py",):
            continue
        text = p.read_text()
        if "default_model_agent" in text and ("cmd_on" in text or ".main(" in text):
            hits.append(p.name)
    assert hits == ["default_model_cli.py"]
