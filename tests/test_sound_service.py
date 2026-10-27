"""`session sound engine build|on|off|status` (ST-132 do-004, design.md decision 3).

No test runs a real launchctl. conftest sets SESSIONTOOLS_SOUND_NO_LAUNCHCTL,
so the real `_launchctl` raises, and the `fence` fixture here replaces it with
a double that records every call and keeps the loaded set in memory (the
model is sessionvoice's `fence`, tests/test_services_install_checks.py).
HOME is a fresh dir per test (conftest), so the plist, the log and the
binaries land under it, never in the real ~/Library/LaunchAgents.

Most tests build with a FAKE swiftc (a shell script that writes a stub), so
they are fast. One session fixture builds the real binaries with the system
swiftc (skipped without it), and one test runs `on` / `status` / `off`
end to end: the double's `bootstrap` starts the plist's own ProgramArguments
plus `--null-output` (no device), in its own process group, killed by pgid.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from wheel_support import build_wheel_offline

from sessiontools import cli, sound
from sessiontools.sound import service

REPO = Path(__file__).resolve().parent.parent
LIVE_PORTS = {8890, 2022, 8880, sound.PORT}
PING = service.ping                                  # the real one, before any fence patches it
DESIGN_KEYS = {"Label", "ProgramArguments", "RunAtLoad", "KeepAlive", "ThrottleInterval",
               "ProcessType", "LimitLoadToSessionType", "StandardOutPath", "StandardErrorPath"}


def free_port() -> int:
    """A UDP port free on 127.0.0.1 now, never one of the live ones."""
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if p not in LIVE_PORTS:
            return p


def fake_swiftc(tmp: Path, *, fail: bool = False) -> str:
    """A swiftc that writes a stub binary to its -o path (or fails like one)."""
    f = tmp / ("swiftc-fail" if fail else "swiftc")
    if fail:
        f.write_text("#!/bin/sh\necho 'engine.swift:1:1: error: boom' >&2\nexit 1\n")
    else:
        f.write_text('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\n'
                     'printf "#!/bin/sh\\nexit 0\\n" > "$2"\n')
    f.chmod(0o755)
    return str(f)


@pytest.fixture
def fence(monkeypatch, tmp_path):
    """The launchctl double, a test port, a fake swiftc, a recorded ping."""
    if sys.platform != "darwin":
        pytest.skip("the sound engine's verb is macOS only")
    home = Path(os.environ["HOME"])
    port = free_port()
    monkeypatch.setenv(sound.PORT_ENV, str(port))
    monkeypatch.delenv(sound.HOME_ENV, raising=False)
    calls: list[tuple] = []
    loaded: set[str] = set()
    state = {"bootstrap_fail": 0, "on_bootstrap": None, "on_bootout": None, "stay_loaded": False,
             "pid": 4242}                                  # the job's pid in `print`; None: not running
    pids: dict[str, int | None] = {}                       # a label's own pid, else state["pid"]

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
            return subprocess.CompletedProcess(a, 0, "", "")
        if verb == "bootout":
            label = a[1].split("/")[-1]
            if not state["stay_loaded"]:
                loaded.discard(label)
            if state["on_bootout"]:
                state["on_bootout"]()
            return subprocess.CompletedProcess(a, 0, "", "")
        return subprocess.CompletedProcess(a, 0, "", "")

    pings: list[int | None] = []
    pong = {"value": {"pid": 4242, "device": "MacBook Pro Speakers", "rate": 96000.0,
                      "running": True, "voices": 0, "sids": 0, "malformed": 0, "rtt_ms": 0.4}}

    def ping(port=None, timeout=service.PING_TIMEOUT, env=None):
        pings.append(port)
        return pong["value"]

    monkeypatch.setattr(service, "_launchctl", launchctl)
    monkeypatch.setattr(service, "ping", ping)
    monkeypatch.setattr(service, "swiftc", lambda: fake_swiftc(tmp_path))
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    return {"home": home, "port": port, "calls": calls, "loaded": loaded, "state": state,
            "pings": pings, "pong": pong, "tmp": tmp_path, "pids": pids}


def verbs(calls) -> list[str]:
    return [c[0] for c in calls]


def run(argv, capsys) -> tuple[int, str, str]:
    rc = cli.main(["sound", "engine", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


# --- the fence itself -------------------------------------------------------------

def test_the_real_launchctl_is_fenced_off_in_tests():
    assert os.environ.get(service.FENCE_ENV)
    with pytest.raises(RuntimeError, match="fenced off"):
        service._launchctl("print", service._target())


# --- the plist ----------------------------------------------------------------------

def test_the_plist_has_exactly_the_designs_keys(monkeypatch):
    monkeypatch.delenv(sound.PORT_ENV, raising=False)
    home = Path(os.environ["HOME"])
    p = service.plist()
    assert set(p) == DESIGN_KEYS
    assert p["Label"] == "dev.sessionkit.tools.sound-engine"
    assert p["ProgramArguments"] == [
        str(home / ".sessionkit/tools/state/sound/bin/sound-engine"), "--port", "44100",
        "--sessions", str(home / ".session/sessions")]
    assert p["RunAtLoad"] is True and p["KeepAlive"] is True
    assert p["ProcessType"] == "Interactive" and p["LimitLoadToSessionType"] == "Aqua"
    log = str(home / ".sessionkit/tools/log/sound-engine.log")
    assert p["StandardOutPath"] == log == p["StandardErrorPath"]
    assert "EnvironmentVariables" not in p


def test_the_plist_passes_the_sessions_root_python_resolves(monkeypatch, tmp_path):
    """ST-266: the engine is handed the sessions leaf, never left to spell
    one: old while it is real, new once the host moved (old a link to it).
    The installer's own SESSIONS_DIR is set aside: the engine never read it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "one-seats-own"))
    from sessiontools import settings
    old, new = settings.leaf_old("sessions"), settings.leaf_new("sessions")
    assert service.plist()["ProgramArguments"][3:] == ["--sessions", str(old)]   # fresh: old
    new.mkdir(parents=True)
    assert service.engine_sessions() == new                                        # new only
    old.parent.mkdir(parents=True)
    old.symlink_to(new)
    assert service.plist()["ProgramArguments"][3:] == ["--sessions", str(new)]   # migrated
    old.unlink()
    old.mkdir()
    assert service.engine_sessions() == old                                        # old real wins
    src = (REPO / "src/sessiontools/sound/engine.swift").read_text()
    assert '"--sessions": sessions = value()' in src                               # it takes it


def test_log_path_is_the_tools_log_dir_under_home(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert service.log_path(env) == tmp_path / ".sessionkit/tools/log/sound-engine.log"
    from sessiontools import settings
    assert service.log_path(env).parent == settings.log_dir(settings.PACKAGES["session"], env)


def test_the_plist_port_is_the_one_constant(monkeypatch):
    """python's PORT, the engine's default and the plist's --port agree, and
    the test override moves the plist too."""
    src = (REPO / "src/sessiontools/sound/engine.swift").read_text()
    assert f"let defaultPort = {sound.PORT}" in src
    monkeypatch.setenv(sound.PORT_ENV, "45000")
    assert service.plist()["ProgramArguments"][1:3] == ["--port", "45000"]


def test_the_throttle_outlasts_the_engines_restart_window():
    """do-003 review N5: the engine exits 3 when it cannot (re)start the
    output within `restartWithin`, and KeepAlive respawns it. The throttle
    keeps a host with no output device to one short try per THROTTLE s."""
    src = (REPO / "src/sessiontools/sound/engine.swift").read_text()
    m = re.search(r"let restartWithin = ([0-9.]+)", src)
    assert m, "engine.swift no longer names restartWithin"
    assert service.plist()["ThrottleInterval"] == service.THROTTLE == 30
    assert service.THROTTLE >= 3 * float(m.group(1))


def test_the_written_plist_passes_plutil_lint(fence, capsys):
    if not Path("/usr/bin/plutil").exists():
        pytest.skip("no plutil")
    rc, _, err = run(["on"], capsys)
    assert rc == 0, err
    path = service.plist_path()
    r = subprocess.run(["/usr/bin/plutil", "-lint", str(path)], capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr
    assert plistlib.loads(path.read_bytes()) == service.plist()
    assert oct(path.stat().st_mode & 0o777) == "0o644"


# --- on ---------------------------------------------------------------------------

def test_on_builds_writes_boots_out_then_bootstraps_and_pings(fence, capsys):
    rc, out, err = run(["on"], capsys)
    assert rc == 0, err
    path = service.plist_path()
    assert path == fence["home"] / "Library/LaunchAgents/dev.sessionkit.tools.sound-engine.plist"
    assert path.exists() and service.log_path().parent.is_dir()
    for name in service.SOURCES:                                   # built (the fake swiftc)
        assert os.access(service.bin_dir() / name, os.X_OK)
        assert service.built_from(service.bin_dir(), name) == service.source_hash(name)
    uid = os.getuid()
    target = f"gui/{uid}/{service.LABEL}"
    boot = [c for c in fence["calls"] if c[0] != "print"]
    assert boot == [("bootout", target), ("bootstrap", f"gui/{uid}", str(path))]
    # it waited for the job to be gone (print) between bootout and bootstrap
    i_out = fence["calls"].index(("bootout", target))
    i_in = fence["calls"].index(("bootstrap", f"gui/{uid}", str(path)))
    assert ("print", target) in fence["calls"][i_out:i_in]
    assert fence["pings"] == [fence["port"]]
    assert "MacBook Pro Speakers" in out and "96000" in out


def test_on_retries_a_bootstrap_once_on_error_5(fence, capsys):
    fence["state"]["bootstrap_fail"] = 1
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert verbs(fence["calls"]).count("bootstrap") == 2


def test_on_gives_up_after_the_one_retry(fence, capsys):
    fence["state"]["bootstrap_fail"] = 2
    rc, out, _ = run(["on"], capsys)
    assert rc == 1 and "bootstrap failed (5)" in out and "Input/output error" in out
    assert verbs(fence["calls"]).count("bootstrap") == 2


def test_on_waits_for_the_old_job_then_says_it_is_stuck(fence, capsys, monkeypatch):
    fence["loaded"].add(service.LABEL)
    fence["state"]["stay_loaded"] = True
    monkeypatch.setattr(service, "GONE_WAIT", 0.0)
    fence["state"]["pid"] = os.getpid()                             # the holder IS the job (do-007)
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # the old engine holds the port
    held.bind(("127.0.0.1", fence["port"]))
    try:
        rc, out, _ = run(["on"], capsys)
    finally:
        held.close()
    assert rc == 1 and "still loaded" in out
    assert "bootstrap" not in verbs(fence["calls"])


def test_on_replaces_our_own_loaded_engine_on_the_port(fence, capsys):
    """The port is held, but by our own loaded job: `on` boots it out and
    bootstraps the new one (a re-run of `on` replaces, never refuses)."""
    fence["loaded"].add(service.LABEL)
    fence["state"]["pid"] = os.getpid()                             # the holder IS the job (do-007)
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("127.0.0.1", fence["port"]))
    fence["state"]["on_bootout"] = held.close                      # bootout frees the port
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert verbs(fence["calls"]).count("bootout") == 1 and "bootstrap" in verbs(fence["calls"])


def test_on_with_no_pong_says_where_the_log_is(fence, capsys):
    fence["pong"]["value"] = None
    rc, out, _ = run(["on"], capsys)
    assert rc == 1 and "no pong" in out and str(service.log_path()) in out


def test_on_notes_a_device_that_is_not_running(fence, capsys):
    fence["pong"]["value"] = dict(fence["pong"]["value"], running=False, device="")
    rc, out, _ = run(["on"], capsys)
    assert rc == 0 and "not running yet" in out


# --- on: the refusals (exit 2, nothing written, nothing loaded) -----------------------

def nothing_changed(fence):
    assert not service.plist_path().exists()
    assert set(verbs(fence["calls"])) <= {"print"}
    assert not fence["loaded"]


def test_on_refuses_when_the_port_is_held_naming_the_holder(fence, capsys):
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("127.0.0.1", fence["port"]))
    try:
        rc, _, err = run(["on"], capsys)
    finally:
        held.close()
    assert rc == 2 and f"127.0.0.1:{fence['port']}/udp is held by" in err
    if shutil.which("lsof") or Path("/usr/sbin/lsof").exists():
        assert str(os.getpid()) in err                              # lsof named us
    nothing_changed(fence)
    assert not (service.bin_dir() / "sound-engine").exists()


# --- do-007: the port held while OUR job is loaded (do-004 review N1) -----------------
#
# A loaded job does not make the holder ours: its engine may have failed to bind
# because something else holds the port, and launchd respawns it every 30 s.
# `on` replaces the engine only when every process lsof names IS the job's pid.

def held_port(fence) -> socket.socket:
    """A foreign holder: this test process, bound to the fence's ephemeral port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", fence["port"]))
    return s


def unchanged_while_loaded(fence):
    """Refused with our job loaded: no plist, no verb but `print`, still loaded."""
    assert not service.plist_path().exists()
    assert set(verbs(fence["calls"])) <= {"print"}
    assert fence["loaded"] == {service.LABEL}
    assert not service.bin_dir().exists()                          # no build either


def test_on_refuses_a_foreign_holder_while_our_job_is_loaded(fence, capsys):
    fence["loaded"].add(service.LABEL)                              # job pid 4242, not us
    held = held_port(fence)
    try:
        rc, _, err = run(["on"], capsys)
    finally:
        held.close()
    assert rc == 2 and f"127.0.0.1:{fence['port']}/udp is held by" in err
    assert f"not by the loaded {service.LABEL}" in err and "nothing was changed" in err
    if shutil.which("lsof") or Path("/usr/sbin/lsof").exists():
        assert str(os.getpid()) in err                              # lsof named the holder
    unchanged_while_loaded(fence)


def test_on_dry_run_refuses_a_foreign_holder_while_our_job_is_loaded(fence, capsys):
    fence["loaded"].add(service.LABEL)
    held = held_port(fence)
    try:
        rc, out, err = run(["on", "--dry-run"], capsys)
    finally:
        held.close()
    assert rc == 2 and "is held by" in err and "replaced" not in out
    unchanged_while_loaded(fence)


def test_on_refuses_a_holder_while_our_job_is_loaded_but_not_running(fence, capsys):
    """`print` with no pid: the job is between respawns, so it holds nothing."""
    fence["loaded"].add(service.LABEL)
    fence["state"]["pid"] = None
    held = held_port(fence)
    try:
        rc, _, err = run(["on"], capsys)
    finally:
        held.close()
    assert rc == 2 and "is held by" in err
    unchanged_while_loaded(fence)


def test_on_refuses_a_holder_lsof_cannot_name_while_our_job_is_loaded(fence, capsys, monkeypatch):
    fence["loaded"].add(service.LABEL)
    fence["state"]["pid"] = os.getpid()                             # even if the pid would match
    monkeypatch.setattr(service, "parse_lsof", lambda text, port: [])
    held = held_port(fence)
    try:
        rc, _, err = run(["on"], capsys)
    finally:
        held.close()
    assert rc == 2 and "is held by unknown" in err
    unchanged_while_loaded(fence)


def test_on_dry_run_says_our_own_loaded_engine_will_be_replaced(fence, capsys):
    """The holder IS the job's pid (the double reports this process)."""
    if not (shutil.which("lsof") or Path("/usr/sbin/lsof").exists()):
        pytest.skip("no lsof: the holder cannot be named")
    fence["loaded"].add(service.LABEL)
    fence["state"]["pid"] = os.getpid()
    held = held_port(fence)
    try:
        rc, out, _ = run(["on", "--dry-run"], capsys)
    finally:
        held.close()
    assert rc == 0 and "held by the loaded engine (replaced)" in out
    unchanged_while_loaded(fence)


@pytest.mark.parametrize("text,want", [
    ("", []),
    ("p100\ncsound-engine\nf3\nn127.0.0.1:44100\n", [("sound-engine", 100)]),
    ("p100\ncsound-engine\nf3\nn*:44100\nf4\nn[::1]:44100\n", [("sound-engine", 100)]),
    # a client merely CONNECTED to the port is not a holder
    (("p7\ncpython\nf4\nn127.0.0.1:50000->127.0.0.1:44100\n"
      "p100\ncsound-engine\nf3\nn127.0.0.1:44100\n"), [("sound-engine", 100)]),
    ("p7\ncpython\nf4\nn127.0.0.1:50000->127.0.0.1:44100\n", []),
    # another port that merely ends the same way is not this one
    ("p8\ncx\nf3\nn127.0.0.1:144100\n", []),
    ("p100\ncsound-engine\nf3\nn127.0.0.1:44100\np200\ncnc\nf5\nn127.0.0.1:44100\n",
     [("sound-engine", 100), ("nc", 200)]),
])
def test_parse_lsof_names_only_the_processes_bound_to_the_port(text, want):
    assert service.parse_lsof(text, 44100) == want


def test_held_by_our_job_needs_every_holder_to_be_the_jobs_pid(fence):
    fence["loaded"].add(service.LABEL)                              # pid 4242
    assert service.held_by_our_job([("sound-engine", 4242)])
    assert not service.held_by_our_job([("sound-engine", 4242), ("nc", 200)])
    assert not service.held_by_our_job([("nc", 200)])
    assert not service.held_by_our_job([])                          # lsof could not say
    fence["state"]["pid"] = None
    assert not service.held_by_our_job([("sound-engine", 4242)])    # loaded, not running
    fence["loaded"].clear()
    assert not service.held_by_our_job([("sound-engine", 4242)])    # not loaded


def test_on_refuses_without_swiftc(fence, capsys, monkeypatch):
    monkeypatch.setattr(service, "swiftc", lambda: None)
    rc, _, err = run(["on"], capsys)
    assert rc == 2 and "no swiftc" in err and "nothing was changed" in err
    nothing_changed(fence)


def test_on_refuses_when_the_build_fails_and_keeps_the_old_binaries(fence, capsys, monkeypatch):
    bins = service.bin_dir()
    service.build()                                                 # a good build first (fake)
    before = {n: (bins / n).read_bytes() for n in service.SOURCES}
    (bins / "sound-event.source-sha256").write_text("old\n")       # one stale: a rebuild is due
    (bins / "sound-engine.source-sha256").write_text("old\n")
    monkeypatch.setattr(service, "swiftc", lambda: fake_swiftc(fence["tmp"], fail=True))
    rc, _, err = run(["on"], capsys)
    assert rc == 2 and "swiftc failed building" in err and "boom" in err
    nothing_changed(fence)
    assert {n: (bins / n).read_bytes() for n in service.SOURCES} == before
    assert sorted(p.name for p in bins.iterdir()) == sorted(
        [*service.SOURCES, *(f"{n}.source-sha256" for n in service.SOURCES)])   # no staging left


# --- --dry-run ----------------------------------------------------------------------

def test_on_dry_run_writes_nothing_and_changes_nothing(fence, capsys):
    rc, out, err = run(["on", "--dry-run"], capsys)
    assert rc == 0, err
    nothing_changed(fence)
    assert not service.bin_dir().exists() and not service.log_path().parent.exists()
    assert "would write" in out and service.LABEL in out and "dry run" in out
    assert "sound-engine, sound-event (stale)" in out and fence["pings"] == []


def test_on_dry_run_still_refuses_a_held_port(fence, capsys):
    held = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    held.bind(("127.0.0.1", fence["port"]))
    try:
        rc, _, err = run(["on", "--dry-run"], capsys)
    finally:
        held.close()
    assert rc == 2 and "is held by" in err
    nothing_changed(fence)


def test_off_dry_run_removes_nothing(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    fence["calls"].clear()
    rc, out, _ = run(["off", "--dry-run", "--purge"], capsys)
    assert rc == 0 and "would remove" in out
    assert service.plist_path().exists() and (service.bin_dir() / "sound-engine").exists()
    assert set(verbs(fence["calls"])) <= {"print"} and service.LABEL in fence["loaded"]


# --- off ----------------------------------------------------------------------------

def test_off_boots_out_and_removes_the_plist_keeping_the_binaries(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    fence["calls"].clear()
    rc, out, _ = run(["off"], capsys)
    assert rc == 0 and "booted out" in out
    assert [c for c in fence["calls"] if c[0] != "print"] == [("bootout", service._target())]
    assert not service.plist_path().exists() and not fence["loaded"]
    assert (service.bin_dir() / "sound-engine").exists()


def test_off_purge_removes_the_binaries(fence, capsys):
    assert run(["on"], capsys)[0] == 0
    rc, _, _ = run(["off", "--purge"], capsys)
    assert rc == 0 and list(service.bin_dir().iterdir()) == []


def test_off_when_not_loaded_runs_no_bootout(fence, capsys):
    rc, out, _ = run(["off"], capsys)
    assert rc == 0 and "was not loaded" in out
    assert set(verbs(fence["calls"])) <= {"print"}


# --- status -------------------------------------------------------------------------

def test_status_json_reports_binaries_plist_loaded_pid_and_pong(fence, capsys):
    rc, out, _ = run(["status", "--json"], capsys)
    s = json.loads(out)
    assert rc == 0 and s["loaded"] is False and s["pid"] is None and s["plist_present"] is False
    assert all(b["present"] is False and b["stale"] for b in s["binaries"].values())
    assert run(["on"], capsys)[0] == 0
    n = len(fence["calls"])
    s = json.loads(run(["status", "--json"], capsys)[1])
    assert set(verbs(fence["calls"][n:])) == {"print"}                # status changes nothing
    assert s["loaded"] is True and s["pid"] == 4242 and s["plist_present"] is True
    assert s["label"] == service.LABEL and s["port"] == fence["port"]
    for name, b in s["binaries"].items():
        assert b["present"] and not b["stale"] and b["built_from"] == service.source_hash(name)
    assert s["pong"]["device"] == "MacBook Pro Speakers"


def test_status_prints_the_new_log_and_points_at_an_old_one_only_while_it_exists(fence, capsys):
    """ST-263: the log line names ~/.sessionkit/tools/log; an old file at
    ~/Library/Logs/sessiontools adds one line naming it, text only."""
    home = Path(os.environ["HOME"])
    new = home / ".sessionkit/tools/log/sound-engine.log"
    old = home / "Library/Logs/sessiontools/sound-engine.log"
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


def test_status_text_names_a_stale_binary(fence, capsys):
    service.build()
    (service.bin_dir() / "sound-event.source-sha256").write_text("old\n")
    rc, out, _ = run(["status"], capsys)
    assert rc == 0 and re.search(r"sound-event .*\(STALE", out) and "sound-engine" in out


# --- the retire: the pre-ST-213 label (ST-213) ---------------------------------------
#
# Before ST-213 the job was service.LEGACY_LABEL. `on` and `off` boot it out and
# remove its plist; `status` names it; `--dry-run` lists the retire and changes
# nothing. Every case runs through the fence's double: old only, new only, both,
# neither, and a re-run that does nothing.

SCENARIOS = ["old", "new", "both", "neither"]


def legacy_target() -> str:
    return service._target(service.LEGACY_LABEL)


def stand_in(fence, scenario: str) -> None:
    """The host as an earlier install left it: the old job loaded with its
    plist, the new one, both, or neither."""
    if scenario in ("old", "both"):
        fence["loaded"].add(service.LEGACY_LABEL)
        service._write_plist(service.legacy_plist_path(), {"Label": service.LEGACY_LABEL})
    if scenario in ("new", "both"):
        fence["loaded"].add(service.LABEL)
        service._write_plist(service.plist_path(), service.plist())


def has_old(scenario: str) -> bool:
    return scenario in ("old", "both")


def test_the_labels_are_the_kits_scheme_and_the_legacy_is_one_literal():
    assert service.LABEL == "dev.sessionkit.tools.sound-engine"
    assert service.LEGACY_LABEL == "com.failmode.sessiontools.sound-engine"
    src = (REPO / "src/sessiontools/sound/service.py").read_text()
    assert src.count(f'"{service.LEGACY_LABEL}"') == 1
    old_vendor = ".".join(service.LEGACY_LABEL.split(".")[:2])          # only the one constant
    assert src.count(old_vendor) == 1
    assert service.legacy_plist_path().name == f"{service.LEGACY_LABEL}.plist"
    assert service.LEGACY_LABEL not in service.EPILOG                 # help names the new one only


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_on_retires_the_old_label_then_a_rerun_does_nothing(fence, capsys, scenario):
    stand_in(fence, scenario)
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert not service.legacy_plist_path().exists() and service.plist_path().exists()
    assert fence["loaded"] == {service.LABEL}
    assert (("bootout", legacy_target()) in fence["calls"]) == has_old(scenario)
    if has_old(scenario):
        i_old = fence["calls"].index(("bootout", legacy_target()))
        i_in = verbs(fence["calls"]).index("bootstrap")
        assert ("print", legacy_target()) in fence["calls"][i_old:i_in]   # waited for it to go
        assert f"retire bootout {legacy_target()}" in out
        assert f"retire remove {service.legacy_plist_path()}" in out
    else:
        assert "nothing to retire" in out
    fence["calls"].clear()                                            # the re-run
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert ("bootout", legacy_target()) not in fence["calls"] and "nothing to retire" in out
    assert not service.legacy_plist_path().exists()


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_off_removes_either_form_then_a_rerun_does_nothing(fence, capsys, scenario):
    stand_in(fence, scenario)
    rc, out, err = run(["off"], capsys)
    assert rc == 0, out + err
    assert not service.legacy_plist_path().exists() and not service.plist_path().exists()
    assert not fence["loaded"]
    boots = [c for c in fence["calls"] if c[0] == "bootout"]
    want = ([("bootout", service._target())] if scenario in ("new", "both") else []) + \
           ([("bootout", legacy_target())] if has_old(scenario) else [])
    assert boots == want
    assert ("nothing to retire" in out) == (not has_old(scenario))
    fence["calls"].clear()
    rc, out, _ = run(["off"], capsys)
    assert rc == 0 and "was not loaded" in out and "nothing to retire" in out
    assert set(verbs(fence["calls"])) <= {"print"}


@pytest.mark.parametrize("verb", [["on", "--dry-run"], ["off", "--dry-run"]])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_dry_run_lists_the_retire_and_changes_nothing(fence, capsys, scenario, verb):
    stand_in(fence, scenario)
    before = set(fence["loaded"])
    rc, out, err = run(verb, capsys)
    assert rc == 0, out + err
    assert set(verbs(fence["calls"])) <= {"print"} and fence["loaded"] == before
    assert service.legacy_plist_path().exists() == has_old(scenario)
    if has_old(scenario):
        assert f"retire would bootout {legacy_target()}" in out
        assert f"retire would remove {service.legacy_plist_path()}" in out
    else:
        assert "nothing to retire" in out and "retire would" not in out


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_status_names_a_stale_old_job_and_changes_nothing(fence, capsys, scenario):
    stand_in(fence, scenario)
    rc, out, _ = run(["status", "--json"], capsys)
    old = json.loads(out)["legacy"]
    assert rc == 0 and old["label"] == service.LEGACY_LABEL
    assert old["stale"] is old["loaded"] is old["plist_present"] is has_old(scenario)
    assert old["plist"] == str(service.legacy_plist_path())
    rc, out, _ = run(["status"], capsys)
    assert rc == 0
    assert (f"legacy {service.LEGACY_LABEL} still there" in out) == has_old(scenario)
    assert set(verbs(fence["calls"])) <= {"print"} and service.legacy_plist_path().exists() == \
        has_old(scenario)


def test_status_names_an_old_plist_that_is_not_loaded(fence, capsys):
    service._write_plist(service.legacy_plist_path(), {"Label": service.LEGACY_LABEL})
    s = json.loads(run(["status", "--json"], capsys)[1])["legacy"]
    assert (s["loaded"], s["plist_present"], s["stale"]) == (False, True, True)
    out = run(["status"], capsys)[1]
    assert f"still there (plist {service.legacy_plist_path()})" in out


def test_on_removes_an_old_plist_that_is_not_loaded_without_a_bootout(fence, capsys):
    service._write_plist(service.legacy_plist_path(), {"Label": service.LEGACY_LABEL})
    rc, out, err = run(["on"], capsys)
    assert rc == 0, out + err
    assert not service.legacy_plist_path().exists()
    assert ("bootout", legacy_target()) not in fence["calls"]
    assert "would" not in out and f"retire remove {service.legacy_plist_path()}" in out


def test_on_retires_the_old_engine_holding_the_port(fence, capsys):
    """The old-label engine holds the port: not a refusal, the retire frees it."""
    fence["loaded"].add(service.LEGACY_LABEL)
    fence["pids"][service.LEGACY_LABEL] = os.getpid()               # the holder IS the old job
    held = held_port(fence)
    fence["state"]["on_bootout"] = held.close
    try:
        rc, out, err = run(["on", "--dry-run"], capsys)
        assert rc == 0 and "held by the old-label engine (retired)" in out, out + err
        assert set(verbs(fence["calls"])) <= {"print"}
        rc, out, err = run(["on"], capsys)
    finally:
        held.close()
    assert rc == 0, out + err
    assert fence["loaded"] == {service.LABEL}
    assert verbs(fence["calls"]).index("bootstrap") > fence["calls"].index(
        ("bootout", legacy_target()))


def test_on_still_refuses_a_foreign_holder_beside_the_old_job(fence, capsys):
    fence["loaded"].add(service.LEGACY_LABEL)                       # old job pid 4242, not us
    held = held_port(fence)
    try:
        rc, _, err = run(["on"], capsys)
    finally:
        held.close()
    assert rc == 2 and "is held by" in err and "nothing was changed" in err
    assert set(verbs(fence["calls"])) <= {"print"}
    assert fence["loaded"] == {service.LEGACY_LABEL}


def test_on_exits_1_when_the_old_job_will_not_go(fence, capsys, monkeypatch):
    fence["loaded"].add(service.LEGACY_LABEL)
    fence["state"]["stay_loaded"] = True
    monkeypatch.setattr(service, "GONE_WAIT", 0.0)
    rc, out, _ = run(["on"], capsys)
    assert rc == 1 and f"{legacy_target()} still loaded" in out
    assert "bootstrap" not in verbs(fence["calls"]) and not service.plist_path().exists()


# --- build --------------------------------------------------------------------------

def test_build_is_incremental_and_force_rebuilds(fence, capsys, tmp_path):
    dest = tmp_path / "out"
    rc, out, _ = run(["build", "--dest", str(dest)], capsys)
    assert rc == 0 and out.count("(built)") == 2
    rc, out, _ = run(["build", "--dest", str(dest)], capsys)
    assert rc == 0 and out.count("(up to date)") == 2
    (dest / "sound-event").unlink()
    rc, out, _ = run(["build", "--dest", str(dest)], capsys)
    assert re.search(r"sound-event .*\(built\)", out) and re.search(r"sound-engine .*up to date", out)
    rc, out, _ = run(["build", "--dest", str(dest), "--force"], capsys)
    assert out.count("(built)") == 2
    assert not fence["calls"]                                         # build never touches launchd


def test_bad_arguments_exit_2(capsys):
    for argv in (["bogus"], [], ["on", "--purge"], ["status", "--dry-run"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["sound", "engine", *argv])
        assert e.value.code == 2
    capsys.readouterr()


def test_the_signals_are_unchanged_beside_the_engine_verb(capsys):
    """`engine` is dispatched before the event parser: `landed`/`given` keep
    their parser, and `engine` is not an event."""
    with pytest.raises(SystemExit) as e:
        cli.main(["sound", "landed"])                                  # --session is required
    assert e.value.code == 2
    capsys.readouterr()


# --- the wheel ----------------------------------------------------------------------

def test_the_wheel_ships_the_swift_sources_and_the_verb(tmp_path):
    whl = build_wheel_offline(REPO, tmp_path)
    names = set(zipfile.ZipFile(whl).namelist())
    for f in ("engine.swift", "event.swift", "service.py", "__init__.py"):
        assert f"sessiontools/sound/{f}" in names, f


# --- the real thing: swiftc, then on/status/off with a real (null-output) engine -------

@pytest.fixture(scope="session")
def real_bins(tmp_path_factory) -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("the sound binaries build with swiftc on macOS only")
    dest = tmp_path_factory.mktemp("sound-bin")
    service.build(dest)
    return dest


def test_a_real_build_makes_both_binaries_from_the_packaged_sources(real_bins):
    for name in service.SOURCES:
        b = real_bins / name
        assert os.access(b, os.X_OK) and not service.stale(real_bins, name)
    r = subprocess.run([str(real_bins / "sound-engine"), "--help"], capture_output=True, text=True,
                       check=False, timeout=30)
    assert r.returncode == 2 and "usage: sound-engine" in r.stderr


def test_on_status_off_end_to_end_with_a_null_output_engine(fence, capsys, monkeypatch, real_bins,
                                                            tmp_path):
    """The double's bootstrap starts the plist's OWN ProgramArguments (plus
    --null-output: no device), so the real `ping` proves the plist's binary
    and --port are right. Killed by its own pgid."""
    monkeypatch.setattr(service, "ping", PING)
    bins = service.bin_dir()
    bins.mkdir(parents=True)
    for f in real_bins.iterdir():
        shutil.copy2(f, bins / f.name)                                 # up to date: no rebuild
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    procs: list[subprocess.Popen] = []

    def start(data):
        procs.append(subprocess.Popen([*data["ProgramArguments"], "--null-output",
                                       "--sessions", str(sessions)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True))

    def stop():
        for p in procs:
            if p.poll() is None:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)          # our own group, by pgid
                p.wait(timeout=10)

    fence["state"]["on_bootstrap"] = start
    fence["state"]["on_bootout"] = stop

    def stamps() -> dict:
        return {n: ((bins / n).stat().st_ino, (bins / n).stat().st_mtime_ns) for n in service.SOURCES}

    before = stamps()
    try:
        rc, out, err = run(["on"], capsys)
        assert rc == 0, out + err
        assert stamps() == before                                      # no rebuild (do-004 N2)
        assert procs and procs[0].poll() is None
        assert f"pid {procs[0].pid}" in out                            # the pong came from it
        s = json.loads(run(["status", "--json"], capsys)[1])
        assert s["pong"]["pid"] == procs[0].pid and s["pong"]["output"] == "null"
        assert s["pong"]["port"] == fence["port"] and s["pong"]["rtt_ms"] >= 0
        rc, _, _ = run(["off"], capsys)
        assert rc == 0 and procs[0].poll() is not None
        assert service.ping(fence["port"], timeout=0.3) is None        # off: silent
    finally:
        stop()

