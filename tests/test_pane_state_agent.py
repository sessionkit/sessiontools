"""`session pane-state agent on|off|status`: the publisher's launchd agent.

No test runs a real launchctl. conftest sets SESSIONTOOLS_PANE_STATE_NO_LAUNCHCTL,
so the real `_launchctl` raises, and the `fence` fixture here replaces it with
a double that records every call and keeps the loaded set in memory (the
model is tests/test_sound_service.py). HOME, SESSION_STATE_DIR and
TMUX_TMPDIR are fresh per test (conftest), so the plist, the log and the
watch lock land in scratch dirs, never in the real ~/Library/LaunchAgents,
and any tmux is a private server.

Most tests stand the job in with a written lock line. One test runs the
plist for real: the double's `bootstrap` starts the plist's own
ProgramArguments with its own EnvironmentVariables (so the job finds tmux by
the plist's PATH alone), in its own process group, killed by pgid.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from sessiontools import cli, pane_state
from sessiontools import pane_state_agent as agent

REPO = Path(__file__).resolve().parent.parent
PLIST_KEYS = {"Label", "ProgramArguments", "EnvironmentVariables", "RunAtLoad", "KeepAlive",
              "ThrottleInterval", "LimitLoadToSessionType", "StandardOutPath",
              "StandardErrorPath"}
CHANGING = {"bootstrap", "bootout", "load", "unload", "kickstart", "kill", "enable", "disable"}


def executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def fence(monkeypatch, tmp_path):
    """The launchctl double, a fake tmux on PATH, macOS assumed."""
    home = Path(os.environ["HOME"])
    tools = tmp_path / "tools"
    tmux = executable(tools / "tmux")
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setattr(agent, "_macos", lambda: True)
    monkeypatch.setattr(agent, "POLL", 0.0)
    monkeypatch.setattr(agent, "RETRY_PAUSE", 0.0)
    calls: list[tuple] = []
    loaded: set[str] = set()
    state = {"bootstrap_fail": 0, "stay_loaded": False, "pid": 4242, "takes_lock": True,
             "on_bootstrap": None, "on_bootout": None}
    pids: dict[str, int | None] = {}                       # a label's own pid, else state["pid"]

    def take_lock():
        path = pane_state.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"pid {state['pid']} on testhost since 2030-12-08T12:34:56Z\n")

    def launchctl(*a):
        calls.append(a)
        verb = a[0]
        if verb == "print":
            label = a[1].split("/")[-1]
            if label in loaded:
                n = pids.get(label, state["pid"])
                pid = f"\tpid = {n}\n" if n is not None else ""
                return subprocess.CompletedProcess(a, 0, f"{label} = {{\n{pid}}}\n", "")
            return subprocess.CompletedProcess(a, 113, "", "Could not find service")
        if verb == "bootstrap":
            if state["bootstrap_fail"]:
                state["bootstrap_fail"] -= 1
                return subprocess.CompletedProcess(a, 5, "", "Bootstrap failed: 5: Input/output error")
            data = plistlib.loads(Path(a[2]).read_bytes())
            loaded.add(data["Label"])
            if state["on_bootstrap"]:
                state["on_bootstrap"](data)
            elif state["takes_lock"]:
                take_lock()
            return subprocess.CompletedProcess(a, 0, "", "")
        if verb == "bootout":
            label = a[1].split("/")[-1]
            if state["on_bootout"]:
                state["on_bootout"]()
            if not state["stay_loaded"]:
                loaded.discard(label)
            return subprocess.CompletedProcess(a, 0, "", "")
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(agent, "_launchctl", launchctl)
    return {"home": home, "calls": calls, "loaded": loaded, "state": state, "tmux": tmux,
            "tmp": tmp_path, "take_lock": take_lock, "pids": pids}


def verbs(calls) -> list[str]:
    return [c[0] for c in calls]


def run(argv, capsys) -> tuple[int, str, str]:
    rc = cli.main(["pane-state", "agent", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.fixture
def held_by_us():
    """The watch lock really held (flock) by THIS process: a hand-run publisher."""
    fd = pane_state.acquire(pane_state.lock_path())
    assert fd is not None
    yield os.getpid()
    os.close(fd)


# --- the fence ----------------------------------------------------------------------

def test_the_real_launchctl_is_fenced_off_in_tests():
    assert os.environ.get(agent.FENCE_ENV) == "1"
    assert agent.FENCE_ENV == "SESSIONTOOLS_PANE_STATE_NO_LAUNCHCTL"
    with pytest.raises(RuntimeError, match="fenced off"):
        agent._launchctl("print", agent._target())


def test_the_fence_execs_no_launchctl_even_one_first_on_path(tmp_path, monkeypatch):
    """A launchctl FIRST on PATH would record its run; through the fence it never
    runs, by the verb either."""
    marker = tmp_path / "ran"
    bin_ = tmp_path / "bin"
    executable(bin_ / "launchctl", f"#!/bin/sh\necho \"$@\" >> {marker}\n")
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setattr(agent, "_macos", lambda: True)
    with pytest.raises(RuntimeError, match="fenced off"):
        agent._launchctl("bootstrap", agent._domain(), "/nonexistent.plist")
    with pytest.raises(RuntimeError, match="fenced off"):
        cli.main(["pane-state", "agent", "status"])
    assert not marker.exists()


def test_conftest_sets_the_fence_for_every_test():
    src = (REPO / "tests/conftest.py").read_text()
    assert f'monkeypatch.setenv("{agent.FENCE_ENV}", "1")' in src
    assert "autouse=True" in src


# --- the label: one constant ----------------------------------------------------------

def test_the_label_is_one_literal_in_the_module():
    src = (REPO / "src/sessiontools/pane_state_agent.py").read_text()
    assert re.findall(r'^LABEL = "([^"]+)"$', src, re.MULTILINE) == [agent.LABEL]
    assert agent.LABEL == "dev.sessionkit.tools.pane-state"          # the kit's one scheme
    assert src.count(f'"{agent.LABEL}"') == 1
    vendor = ".".join(agent.LABEL.split(".")[:2])                    # a rename is this one line
    assert src.count(vendor) == 1
    assert agent.LABEL in agent.EPILOG


def test_everything_else_derives_from_the_label(fence, monkeypatch):
    monkeypatch.setattr(agent, "LABEL", "org.example.renamed")
    assert agent.plist_path().name == "org.example.renamed.plist"
    assert agent._target() == f"gui/{os.getuid()}/org.example.renamed"
    assert agent.plist(Path("/x/session"), fence["tmux"])["Label"] == "org.example.renamed"
    assert agent.status()["label"] == "org.example.renamed"


# --- the plist ----------------------------------------------------------------------

def test_the_plist_has_exactly_the_designs_keys(fence):
    program = agent.session_bin()
    p = agent.plist(program, fence["tmux"])
    assert set(p) == PLIST_KEYS
    assert "ProcessType" not in p
    assert p["Label"] == agent.LABEL
    assert Path(p["ProgramArguments"][0]).is_absolute()
    assert p["ProgramArguments"][1:] == ["pane-state", "watch"]
    assert p["RunAtLoad"] is True and p["KeepAlive"] is True
    assert p["ThrottleInterval"] == agent.THROTTLE == 30
    assert p["LimitLoadToSessionType"] == "Aqua"
    log = str(fence["home"] / ".sessionkit/tools/log/pane-state.log")
    assert p["StandardOutPath"] == log == p["StandardErrorPath"]


def test_log_path_is_the_tools_log_dir_under_home(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert agent.log_path(env) == tmp_path / ".sessionkit/tools/log/pane-state.log"
    from sessiontools import settings
    assert agent.log_path(env).parent == settings.log_dir(settings.PACKAGES["session"], env)


def test_the_path_is_tmuxs_directory_then_launchds_own(fence):
    env = agent.environment(fence["tmux"])
    assert env["PATH"].split(":") == [str(fence["tmux"].parent), "/usr/bin", "/bin",
                                      "/usr/sbin", "/sbin"]
    assert agent.environment(Path("/usr/bin/tmux"))["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"


def test_only_the_variables_that_are_set_pass_through(fence, monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", "/tmp/private-sockets")
    monkeypatch.setenv("SESSION_STATE_DIR", "/tmp/state")
    monkeypatch.delenv("SESSION_SIDECAR_DIR", raising=False)
    monkeypatch.setenv("SESSIONS_DIR", "")
    monkeypatch.setenv("SESSION_HOME", "/tmp/not-on-the-watch-path")
    env = agent.environment(fence["tmux"])
    assert env == {"PATH": env["PATH"], "TMUX_TMPDIR": "/tmp/private-sockets",
                   "SESSION_STATE_DIR": "/tmp/state"}


def test_the_pass_through_list_is_what_the_watch_path_reads():
    """Each passed variable is read by a module on the watch path; SESSION_HOME
    is the launcher's and HOME is launchd's own."""
    readers = {"TMUX_TMPDIR": "tmux.py", "SESSION_STATE_DIR": "snapshot.py",
               "SESSION_SIDECAR_DIR": "claude_code.py", "SESSIONS_DIR": "settings.py"}
    assert set(agent.PASS_THROUGH) == set(readers)
    for name, module in readers.items():
        assert f'"{name}"' in (REPO / "src/sessiontools" / module).read_text(), name
    assert "SESSION_HOME" not in agent.PASS_THROUGH and "HOME" not in agent.PASS_THROUGH


def test_the_program_is_the_session_beside_the_interpreter_never_resolved(tmp_path, monkeypatch):
    base = executable(tmp_path / "base/bin/python3")
    venv = tmp_path / "venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(base)
    script = executable(venv / "session")
    monkeypatch.setattr(agent.sys, "executable", str(venv / "python"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert agent.session_bin() == script


def test_the_program_falls_back_to_session_on_path_resolved(tmp_path, monkeypatch):
    real = executable(tmp_path / "tool/bin/session")
    link = tmp_path / "local/bin/session"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    monkeypatch.setattr(agent.sys, "executable", str(tmp_path / "elsewhere/python"))
    monkeypatch.setenv("PATH", f"{link.parent}:/usr/bin:/bin")
    assert agent.session_bin() == real.resolve()


# --- refusals: exit 2, nothing changed --------------------------------------------------

def assert_nothing_changed(fence):
    assert not agent.plist_path().exists()
    assert not set(verbs(fence["calls"])) & CHANGING


def test_on_refuses_off_macos(fence, capsys, monkeypatch):
    monkeypatch.setattr(agent, "_macos", lambda: False)
    rc, _, err = run(["on"], capsys)
    assert rc == 2 and "macOS only" in err
    assert_nothing_changed(fence)


def test_on_refuses_with_no_session_script(fence, capsys, monkeypatch):
    monkeypatch.setattr(agent, "session_bin", lambda env=None: None)
    rc, _, err = run(["on"], capsys)
    assert rc == 2 and "no `session`" in err
    assert_nothing_changed(fence)


def test_on_refuses_with_no_tmux(fence, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(agent, "session_bin", lambda env=None: Path("/x/bin/session"))
    rc, _, err = run(["on"], capsys)
    assert rc == 2 and "no tmux" in err
    assert_nothing_changed(fence)


@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_on_refuses_while_another_publisher_holds_the_lock(fence, capsys, held_by_us, dry):
    rc, out, err = run(["on", *dry], capsys)
    assert rc == 2, out + err
    assert f"pid {held_by_us} on " in err and "not by the loaded" in err
    assert "Nothing was changed" in err
    assert_nothing_changed(fence)


def test_on_refuses_a_foreign_holder_while_our_job_is_loaded(fence, capsys, held_by_us):
    fence["loaded"].add(agent.LABEL)                      # job pid 4242, the lock is ours (pid)
    rc, _, err = run(["on"], capsys)
    assert rc == 2 and "not by the loaded" in err
    assert_nothing_changed(fence)


# --- on --------------------------------------------------------------------------

def test_on_writes_boots_out_then_bootstraps_and_proves_the_job_holds_the_lock(fence, capsys):
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    path = agent.plist_path()
    assert path == fence["home"] / f"Library/LaunchAgents/{agent.LABEL}.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["Label"] == agent.LABEL and data["ProgramArguments"][1:] == ["pane-state", "watch"]
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    assert agent.log_path().parent.is_dir()
    changing = [c for c in fence["calls"] if c[0] in CHANGING]
    assert changing == [("bootout", agent._target()),
                        ("bootstrap", f"gui/{os.getuid()}", str(path))]
    assert f"on     {agent.LABEL}: pid 4242 holds {pane_state.lock_path()}" in out


def test_the_written_plist_passes_plutil_lint(fence, capsys):
    if not Path("/usr/bin/plutil").exists():
        pytest.skip("no plutil")
    assert run(["on"], capsys)[0] == 0
    r = subprocess.run(["/usr/bin/plutil", "-lint", str(agent.plist_path())],
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr


def test_on_exits_1_naming_the_log_when_the_job_never_takes_the_lock(fence, capsys, monkeypatch):
    monkeypatch.setattr(agent, "PROOF_WAIT", 0.0)
    fence["state"]["takes_lock"] = False
    rc, out, _ = run(["on"], capsys)
    assert rc == 1
    assert "did not take the watch lock" in out and str(agent.log_path()) in out


def test_on_exits_1_when_the_lock_names_another_pid(fence, capsys, monkeypatch):
    """A line left by an earlier holder is not the proof: it must name the job."""
    monkeypatch.setattr(agent, "PROOF_WAIT", 0.0)
    fence["state"]["takes_lock"] = False
    path = pane_state.lock_path()
    path.parent.mkdir(parents=True)
    path.write_text("pid 1 on testhost since 2030-12-08T12:34:56Z\n")   # stale: nobody holds it
    rc, out, _ = run(["on"], capsys)
    assert rc == 1 and "did not take the watch lock" in out


def test_on_waits_for_the_job_to_take_the_lock(fence, capsys, monkeypatch):
    """The proof polls: the job takes the lock only on a later look."""
    fence["state"]["takes_lock"] = False
    looks = {"n": 0}
    real = agent.job_pid

    def job_pid():
        looks["n"] += 1
        if looks["n"] == 4:                       # the refusal check's look, then two misses
            fence["take_lock"]()
        return real()

    monkeypatch.setattr(agent, "job_pid", job_pid)
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert looks["n"] == 4


def test_on_replaces_our_own_loaded_job(fence, capsys):
    """The lock line names the loaded job's pid: the job holds it (no probe), so
    `on` boots it out and loads it again."""
    fence["loaded"].add(agent.LABEL)
    fence["take_lock"]()
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert [c[0] for c in fence["calls"] if c[0] in CHANGING] == ["bootout", "bootstrap"]


def test_a_stale_lock_line_is_not_a_holder(fence, capsys):
    path = pane_state.lock_path()
    path.parent.mkdir(parents=True)
    path.write_text("pid 1 on testhost since 2030-12-08T12:34:56Z\n")
    assert agent.lock(None) == {"path": str(path), "held": False, "holder": None,
                                "holder_pid": None, "ours": False}
    assert run(["on"], capsys)[0] == 0


def test_on_retries_a_bootstrap_once(fence, capsys):
    fence["state"]["bootstrap_fail"] = 1
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert verbs(fence["calls"]).count("bootstrap") == 2


def test_on_gives_up_after_the_one_retry(fence, capsys):
    fence["state"]["bootstrap_fail"] = 2
    rc, out, _ = run(["on"], capsys)
    assert rc == 1 and "bootstrap failed (5)" in out
    assert verbs(fence["calls"]).count("bootstrap") == 2


def test_on_says_when_the_old_job_will_not_go(fence, capsys, monkeypatch):
    monkeypatch.setattr(agent, "GONE_WAIT", 0.0)
    fence["loaded"].add(agent.LABEL)
    fence["take_lock"]()
    fence["state"]["stay_loaded"] = True
    rc, out, _ = run(["on"], capsys)
    assert rc == 1 and "still loaded" in out
    assert "bootstrap" not in verbs(fence["calls"])


def test_on_dry_run_prints_the_plist_and_changes_nothing(fence, capsys, monkeypatch):
    state_dir = Path(os.environ["SESSION_STATE_DIR"])
    before = sorted(state_dir.rglob("*"))
    rc, out, err = run(["on", "--dry-run"], capsys)
    assert rc == 0, err
    for needle in (agent.LABEL, "<key>KeepAlive</key>", "<true/>", str(agent.log_path()),
                   "<string>pane-state</string>", "<string>watch</string>",
                   f"<string>{fence['tmux'].parent}:/usr/bin:/bin:/usr/sbin:/sbin</string>",
                   "<key>TMUX_TMPDIR</key>", "lock   ", "not held",
                   "dry run: nothing was written"):
        assert needle in out, needle
    program = re.search(r"<key>ProgramArguments</key>\s*<array>\s*<string>([^<]+)</string>", out)
    assert program and Path(program.group(1)).is_absolute()
    assert_nothing_changed(fence)
    assert not agent.log_path().parent.exists()
    assert sorted(state_dir.rglob("*")) == before            # no lock, no lock dir


# --- off -------------------------------------------------------------------------

def test_off_boots_out_and_removes_the_plist(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    fence["calls"].clear()
    rc, out, _ = run(["off"], capsys)
    assert rc == 0 and "booted out" in out
    assert not agent.plist_path().exists()
    assert ("bootout", agent._target()) in fence["calls"]
    assert agent.LABEL not in fence["loaded"]


def test_off_when_not_loaded_runs_no_bootout(fence, capsys):
    rc, out, _ = run(["off"], capsys)
    assert rc == 0 and "was not loaded" in out
    assert "bootout" not in verbs(fence["calls"])


def test_off_dry_run_removes_nothing(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    fence["calls"].clear()
    rc, out, _ = run(["off", "--dry-run"], capsys)
    assert rc == 0 and "would remove" in out and "(loaded)" in out
    assert agent.plist_path().exists()
    assert not set(verbs(fence["calls"])) & CHANGING


# --- status ----------------------------------------------------------------------

def test_status_json_reports_label_plist_loaded_pid_lock_and_log(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    fence["calls"].clear()
    rc, out, _ = run(["status", "--json"], capsys)
    assert rc == 0
    s = json.loads(out)
    assert s["label"] == agent.LABEL
    assert s["plist"] == str(agent.plist_path()) and s["plist_present"] is True
    assert s["loaded"] is True and s["pid"] == 4242
    assert s["program"][1:] == ["pane-state", "watch"]
    assert s["lock"] == {"path": str(pane_state.lock_path()), "held": True,
                         "holder": "pid 4242 on testhost since 2030-12-08T12:34:56Z",
                         "holder_pid": 4242, "ours": True}
    assert s["log"] == str(agent.log_path())
    assert set(verbs(fence["calls"])) == {"print"}               # status changes nothing


def test_status_prints_the_new_log_and_points_at_an_old_one_only_while_it_exists(fence, capsys):
    """ST-263: the log line names ~/.sessionkit/tools/log; an old file at
    ~/Library/Logs/sessiontools adds one line naming it, text only."""
    new = fence["home"] / ".sessionkit/tools/log/pane-state.log"
    old = fence["home"] / "Library/Logs/sessiontools/pane-state.log"
    rc, out, _ = run(["status"], capsys)
    assert rc == 0 and f"log    {new}" in out
    assert "old log" not in out and "Library/Logs" not in out
    assert not old.parent.exists()                               # status made nothing
    old.parent.mkdir(parents=True)
    old.write_text("x\n")
    rc, out, _ = run(["status"], capsys)
    assert rc == 0 and f"log    {new}" in out
    assert out.count("old log") == 1 and f"old log  {old} (no longer written)" in out
    s = json.loads(run(["status", "--json"], capsys)[1])
    assert s["log"] == str(new) and "old_log" not in s and str(old) not in json.dumps(s)
    assert old.read_text() == "x\n"                             # never moved or deleted


def test_status_when_off(fence, capsys):
    rc, out, _ = run(["status", "--json"], capsys)
    s = json.loads(out)
    assert rc == 0 and s["plist_present"] is False and s["loaded"] is False and s["pid"] is None
    assert s["lock"]["held"] is False and s["program"] is None
    assert not pane_state.lock_path().exists()                 # status created nothing


def test_status_says_when_the_holder_is_not_the_job(fence, capsys, held_by_us):
    fence["loaded"].add(agent.LABEL)
    rc, out, _ = run(["status"], capsys)
    assert rc == 0
    assert "held by NOT the loaded job" in out and f"pid {held_by_us}" in out
    assert "launchd retries every 30 s" in out


# --- the retire: the pre-ST-213 label (ST-213) ---------------------------------------
#
# Before ST-213 the job was agent.LEGACY_LABEL. `on` and `off` boot it out and
# remove its plist; `status` names it; `--dry-run` lists the retire and changes
# nothing. Every case runs through the fence's double: old only, new only, both,
# neither, and a re-run that does nothing.

SCENARIOS = ["old", "new", "both", "neither"]


def legacy_target() -> str:
    return agent._target(agent.LEGACY_LABEL)


def stand_in(fence, scenario: str) -> None:
    """The host as an earlier install left it: the old job loaded with its
    plist, the new one (holding the lock), both, or neither."""
    if scenario in ("old", "both"):
        fence["loaded"].add(agent.LEGACY_LABEL)
        agent._write_plist(agent.legacy_plist_path(), {"Label": agent.LEGACY_LABEL})
    if scenario in ("new", "both"):
        fence["loaded"].add(agent.LABEL)
        agent._write_plist(agent.plist_path(), {"Label": agent.LABEL})
        fence["take_lock"]()


def has_old(scenario: str) -> bool:
    return scenario in ("old", "both")


def test_the_legacy_label_is_one_literal_and_not_in_the_help():
    assert agent.LEGACY_LABEL == "com.failmode.sessiontools.pane-state"
    src = (REPO / "src/sessiontools/pane_state_agent.py").read_text()
    assert src.count(f'"{agent.LEGACY_LABEL}"') == 1
    old_vendor = ".".join(agent.LEGACY_LABEL.split(".")[:2])          # only the one constant
    assert src.count(old_vendor) == 1
    assert agent.legacy_plist_path().name == f"{agent.LEGACY_LABEL}.plist"
    assert agent.LEGACY_LABEL not in agent.EPILOG


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_on_retires_the_old_label_then_a_rerun_does_nothing(fence, capsys, scenario):
    stand_in(fence, scenario)
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert not agent.legacy_plist_path().exists() and agent.plist_path().exists()
    assert fence["loaded"] == {agent.LABEL}
    assert (("bootout", legacy_target()) in fence["calls"]) == has_old(scenario)
    if has_old(scenario):
        i_old = fence["calls"].index(("bootout", legacy_target()))
        i_in = verbs(fence["calls"]).index("bootstrap")
        assert ("print", legacy_target()) in fence["calls"][i_old:i_in]   # waited for it to go
        assert f"retire bootout {legacy_target()}" in out
        assert f"retire remove {agent.legacy_plist_path()}" in out
    else:
        assert "nothing to retire" in out
    fence["calls"].clear()                                            # the re-run
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert ("bootout", legacy_target()) not in fence["calls"] and "nothing to retire" in out
    assert not agent.legacy_plist_path().exists()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_off_removes_either_form_then_a_rerun_does_nothing(fence, capsys, scenario):
    stand_in(fence, scenario)
    rc, out, err = run(["off"], capsys)
    assert rc == 0, out + err
    assert not agent.legacy_plist_path().exists() and not agent.plist_path().exists()
    assert not fence["loaded"]
    boots = [c for c in fence["calls"] if c[0] == "bootout"]
    want = ([("bootout", agent._target())] if scenario in ("new", "both") else []) + \
           ([("bootout", legacy_target())] if has_old(scenario) else [])
    assert boots == want
    assert ("nothing to retire" in out) == (not has_old(scenario))
    fence["calls"].clear()
    rc, out, _ = run(["off"], capsys)
    assert rc == 0 and "was not loaded" in out and "nothing to retire" in out
    assert not set(verbs(fence["calls"])) & CHANGING


@pytest.mark.parametrize("verb", [["on", "--dry-run"], ["off", "--dry-run"]])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_dry_run_lists_the_retire_and_changes_nothing(fence, capsys, scenario, verb):
    stand_in(fence, scenario)
    before = set(fence["loaded"])
    rc, out, err = run(verb, capsys)
    assert rc == 0, out + err
    assert not set(verbs(fence["calls"])) & CHANGING and fence["loaded"] == before
    assert agent.legacy_plist_path().exists() == has_old(scenario)
    if has_old(scenario):
        assert f"retire would bootout {legacy_target()}" in out
        assert f"retire would remove {agent.legacy_plist_path()}" in out
    else:
        assert "nothing to retire" in out and "retire would" not in out


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_status_names_a_stale_old_job_and_changes_nothing(fence, capsys, scenario):
    stand_in(fence, scenario)
    rc, out, _ = run(["status", "--json"], capsys)
    old = json.loads(out)["legacy"]
    assert rc == 0 and old["label"] == agent.LEGACY_LABEL
    assert old["stale"] is old["loaded"] is old["plist_present"] is has_old(scenario)
    assert old["plist"] == str(agent.legacy_plist_path())
    rc, out, _ = run(["status"], capsys)
    assert rc == 0
    assert (f"legacy {agent.LEGACY_LABEL} still there" in out) == has_old(scenario)
    assert set(verbs(fence["calls"])) == {"print"}
    assert agent.legacy_plist_path().exists() == has_old(scenario)


def test_on_removes_an_old_plist_that_is_not_loaded_without_a_bootout(fence, capsys):
    agent._write_plist(agent.legacy_plist_path(), {"Label": agent.LEGACY_LABEL})
    s = json.loads(run(["status", "--json"], capsys)[1])["legacy"]
    assert (s["loaded"], s["plist_present"], s["stale"]) == (False, True, True)
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert not agent.legacy_plist_path().exists()
    assert ("bootout", legacy_target()) not in fence["calls"]
    assert f"retire remove {agent.legacy_plist_path()}" in out


@pytest.fixture
def held_by_the_old_job(fence):
    """The watch lock really held (flock) by THIS process, standing in for the
    old-label job: `print` gives the old job this pid; its bootout lets go."""
    fd = {"n": pane_state.acquire(pane_state.lock_path())}
    assert fd["n"] is not None
    fence["loaded"].add(agent.LEGACY_LABEL)
    fence["pids"][agent.LEGACY_LABEL] = os.getpid()

    def let_go():
        if fd["n"] is not None:
            os.close(fd["n"])
            fd["n"] = None

    fence["state"]["on_bootout"] = let_go
    yield fence
    let_go()


def test_on_retires_the_old_job_holding_the_lock(held_by_the_old_job, capsys):
    """The old-label job holds the watch lock: not a refusal, the retire frees it."""
    fence = held_by_the_old_job
    rc, out, err = run(["on", "--dry-run"], capsys)
    assert rc == 0 and "(the old-label job: retired)" in out, out + err
    assert not set(verbs(fence["calls"])) & CHANGING
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert fence["loaded"] == {agent.LABEL} and "pid 4242 holds" in out
    assert verbs(fence["calls"]).index("bootstrap") > fence["calls"].index(
        ("bootout", legacy_target()))


def test_on_still_refuses_a_foreign_holder_beside_the_old_job(fence, capsys, held_by_us):
    fence["loaded"].add(agent.LEGACY_LABEL)                         # old job pid 4242, not us
    rc, _, err = run(["on"], capsys)
    assert rc == 2 and "not by the loaded" in err
    assert_nothing_changed(fence)
    assert fence["loaded"] == {agent.LEGACY_LABEL}


def test_on_exits_1_when_the_old_job_will_not_go(fence, capsys, monkeypatch):
    monkeypatch.setattr(agent, "GONE_WAIT", 0.0)
    fence["loaded"].add(agent.LEGACY_LABEL)
    fence["state"]["stay_loaded"] = True
    rc, out, _ = run(["on"], capsys)
    assert rc == 1 and f"{legacy_target()} still loaded" in out
    assert "bootstrap" not in verbs(fence["calls"]) and not agent.plist_path().exists()


# --- the lock helpers ---------------------------------------------------------------

@pytest.mark.parametrize("line, want", [
    ("pid 812 on host since 2030-12-08T12:34:56Z", 812),
    ("pid 812", 812),
    ("pid x on host", None),
    ("", None),
    (None, None),
    ("812", None),
])
def test_line_pid(line, want):
    assert agent.line_pid(line) == want


def test_the_probe_creates_nothing_and_sees_a_real_hold(tmp_path, held_by_us):
    missing = tmp_path / "no/such/watch.lock"
    assert agent.probe(missing) is False and not missing.parent.exists()
    assert agent.probe(pane_state.lock_path()) is True
    other = tmp_path / "free.lock"
    other.write_text("pid 1 on host since then\n")
    assert agent.probe(other) is False


def test_the_probe_lets_go_at_once(tmp_path):
    path = tmp_path / "watch.lock"
    assert agent.probe(path) is False
    path.write_text("")
    assert agent.probe(path) is False
    fd = pane_state.acquire(path)                             # a probe never kept it
    assert fd is not None
    os.close(fd)


# --- the verb ------------------------------------------------------------------------

def test_bad_arguments_exit_2(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["pane-state", "agent"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        cli.main(["pane-state", "agent", "install"])
    assert e.value.code == 2


def test_the_help_names_the_label_and_the_verbs(capsys):
    with pytest.raises(SystemExit):
        cli.main(["pane-state", "agent", "--help"])
    out = capsys.readouterr().out
    assert "on|off|status" in out and agent.LABEL in out and "IS the deploy" in out


def test_pane_state_help_lists_agent(capsys):
    assert cli.main(["pane-state"]) == 2
    err = capsys.readouterr().err
    assert "show|watch|agent" in err and "launchd agent" in err
    assert "Nothing starts the publisher" not in err


def test_show_and_watch_are_unchanged_beside_the_agent_verb(monkeypatch):
    seen = {}
    monkeypatch.setattr(pane_state, "watch", lambda **kw: seen.update(kw) or 0)
    assert cli.main(["pane-state", "watch", "--once"]) == 0
    assert seen["once"] is True


# --- end to end: the plist's own command, by the plist's own PATH ----------------------

@pytest.fixture
def real_job(fence, monkeypatch):
    """The double's bootstrap starts the plist's ProgramArguments with ONLY the
    plist's EnvironmentVariables (plus HOME, which launchd sets), in its own
    process group; bootout SIGTERMs it. Killed by pgid at teardown."""
    rest = os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep)
                           if d != str(fence["tmux"].parent))
    tmux = shutil.which("tmux", path=rest)
    if tmux is None:
        pytest.skip("needs tmux")
    if agent.session_bin() is None:
        pytest.skip("needs the `session` console script")
    monkeypatch.setenv("PATH", f"{Path(tmux).parent}:/usr/bin:/bin")   # the real tmux, not the fake
    procs: list[subprocess.Popen] = []

    def start(data):
        env = {**data["EnvironmentVariables"], "HOME": os.environ["HOME"]}
        log = open(data["StandardOutPath"], "ab")   # noqa: SIM115 - the child keeps it
        proc = subprocess.Popen(data["ProgramArguments"], env=env, stdout=log, stderr=log,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        log.close()
        procs.append(proc)
        fence["state"]["pid"] = proc.pid

    def stop():
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)

    fence["state"]["on_bootstrap"] = start
    fence["state"]["on_bootout"] = stop
    yield fence
    for proc in procs:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)


def test_on_status_off_end_to_end_with_the_real_watch(real_job, capsys):
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err + agent.log_path().read_text()
    pid = real_job["state"]["pid"]
    assert f"pid {pid} holds" in out
    assert agent.probe(pane_state.lock_path()) is True
    data = plistlib.loads(agent.plist_path().read_bytes())
    assert data["EnvironmentVariables"]["SESSION_STATE_DIR"] == os.environ["SESSION_STATE_DIR"]
    assert data["EnvironmentVariables"]["TMUX_TMPDIR"] == os.environ["TMUX_TMPDIR"]
    s = json.loads((run(["status", "--json"], capsys))[1])
    assert s["lock"]["ours"] is True and s["lock"]["holder_pid"] == pid
    rc, out, _ = run(["off"], capsys)
    assert rc == 0 and "booted out" in out
    assert agent.probe(pane_state.lock_path()) is False
