"""`session respawn --go` for real, on a PRIVATE tmux server (ST-30 do-002).

The server is `-L st30-<rand>` under conftest's scratch TMUX_TMPDIR. The
sidecar folder, the sessions root, the state dir and HOME are conftest's
scratch ones too, so `listing.rows()` sees only these seats. Every test
checks the wall before it starts anything. Nothing here runs `--go` against
a live seat.

A SEAT is a Python stand-in for claude. It writes its own sidecar, records
its argv, cwd and start time, then waits. When a test drops a trigger file
it runs the command named there AS ITS OWN CHILD, which is how a seat runs
`session respawn` from its tool shell. The child is this checkout's CLI by
absolute interpreter path.

- Design (i)-(v): the seat is started by hand in a pane, its launch record
  is written by the test, and `session` on the pane's PATH is a STUB that
  records argv, cwd and the hand-off env, then sleeps.
- Design (vi)-(vii): the seat is started by the REAL `session start`, with
  the seat script as the vendor (`claude`), and `session` is this checkout's
  CLI. The respawned pane runs the real `session start` again.

(iv) is super ruling 3, not the design's: `SID --go` naming another seat's
pane is refused, and nothing on that pane changes.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from start_support import walled_bin
from test_start_placement import e2e_env, private_env
from test_start_singleton import message_stub

from sessiontools import respawn, settings
from sessiontools.start import default_seat
from sessiontools.start import pane as _pane
from sessiontools.start.record import parse_command, render_command

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

TIMEOUT = 30

#: The seat. CONFIG is spliced in as JSON. `fake NAME` is a hand-started
#: seat; anything else is claude's argv from `session start`, the id taken
#: from `--session-id` and the agent from `--agent`. On SIGHUP it lingers
#: CONFIG["linger"] seconds before it exits, recording both moments: a
#: holder still alive while its successor launches (design §8).
SEAT = r'''
import json, os, signal, subprocess, sys, time
CONFIG = json.loads(@@CONFIG@@)
ctl, sidecars = CONFIG["ctl"], CONFIG["sidecars"]
argv = sys.argv[1:]
pid = os.getpid()

def after(flag):
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None

if argv[:1] == ["fake"]:
    sid, name, agent = "%08x-0000-4000-8000-000000000030" % pid, argv[1], argv[1]
else:
    sid = after("--session-id")
    agent = after("--agent") or ""
    name = agent or "claude"

def note(kind, **extra):
    path = os.path.join(ctl, kind + "-" + sid + ".json")
    with open(path + ".tmp", "w") as f:
        json.dump({"t_ns": time.time_ns(), "pid": pid, **extra}, f)
    os.rename(path + ".tmp", path)

def hup(signum, frame):
    note("hup")
    time.sleep(float(CONFIG["linger"]))
    note("exit")
    os._exit(0)

signal.signal(signal.SIGHUP, hup)
env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], env=env,
                       capture_output=True, text=True).stdout.strip()
# claude's own `tmux` field, `session:@window.%pane`, from the ambient server
where = subprocess.run(["tmux", "display-message", "-p", "-t", os.environ["TMUX_PANE"],
                        "#{session_name}:#{window_id}.#{pane_id}"],
                       capture_output=True, text=True).stdout.strip()
side = {"pid": pid, "sessionId": sid, "status": "idle",
        "updatedAt": int((time.time() - 7200) * 1000), "procStart": start,
        "cwd": os.getcwd(), "name": name, "agent": agent, "tmux": where}
tmp = os.path.join(sidecars, "." + str(pid) + ".tmp")
with open(tmp, "w") as f:
    json.dump(side, f)
os.rename(tmp, os.path.join(sidecars, str(pid) + ".json"))
note("seat", sid=sid, argv=sys.argv, cwd=os.getcwd())
go = os.path.join(ctl, "go-" + sid)
while True:
    if os.path.exists(go):
        with open(go) as f:
            command = json.load(f)
        os.unlink(go)
        with open(os.path.join(ctl, "out-" + sid), "w") as out:
            code = subprocess.run(command, stdout=out, stderr=subprocess.STDOUT).returncode
        with open(os.path.join(ctl, "rc-" + sid), "w") as f:
            f.write(str(code))
    time.sleep(0.05)
'''

#: The STUB `session` the respawned pane runs in (i)-(v): what it was
#: given, where, with which hand-off and when, then it waits.
STUB_SESSION = r'''
import json, os, sys, time
t_ns = time.time_ns()
record = {"t_ns": t_ns, "pid": os.getpid(), "argv": sys.argv[1:], "cwd": os.getcwd(),
          "entered": os.environ.get("SESSION_START_ENTERED"),
          "entered_dir": os.environ.get("SESSION_START_ENTERED_DIR")}
path = os.path.join(@@CTL@@, "stub-" + str(os.getpid()) + ".json")
with open(path + ".tmp", "w") as f:
    json.dump(record, f)
os.rename(path + ".tmp", path)
time.sleep(300)
'''

#: The respawn a seat runs as its child: this checkout's CLI.
RESPAWN = [sys.executable, "-m", "sessiontools.cli", "respawn"]


def wait_for(predicate, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    return predicate()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.fixture(autouse=True)
def _walled():
    assert os.path.basename(os.environ["TMUX_TMPDIR"]).startswith("st-tmux-")
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    assert "no-sidecars" in os.environ["SESSION_SIDECAR_DIR"]
    assert "no-sessions" in os.environ["SESSIONS_DIR"]


class Server:
    """One private server and the seats in it, each in a pane of its own."""

    def __init__(self, tmp: Path, env: dict, linger: float = 0):
        self.tmp = tmp
        self.env = env
        self.socket = f"st30-{secrets.token_hex(3)}"
        self.ctl = tmp / "ctl"
        self.ctl.mkdir()
        self.sidecars = Path(env["SESSION_SIDECAR_DIR"])
        self.sidecars.mkdir(parents=True, exist_ok=True)
        self.seat_script = tmp / "seat.py"
        config = {"ctl": str(self.ctl), "sidecars": str(self.sidecars), "linger": linger}
        self.seat_script.write_text(SEAT.replace("@@CONFIG@@", repr(json.dumps(config))))
        # HOME is the test's and `-f /dev/null`: no user tmux.conf is read.
        out = self.tmux("-f", "/dev/null", "new-session", "-d", "-x", "200", "-y", "50",
                        "-s", "agents", "sleep 300")
        assert out.returncode == 0, out.stderr

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True,
                              text=True, check=False, timeout=10, env=private_env(self.env))

    def window(self, command: str, cwd: Path) -> str:
        out = self.tmux("new-window", "-d", "-t", "agents:", "-c", str(cwd), "-P", "-F",
                        "#{pane_id}", command)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def fact(self, pane: str, fmt: str) -> str:
        out = self.tmux("display-message", "-p", "-t", pane, fmt)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def seats(self) -> dict[str, dict]:
        found = {}
        for path in self.ctl.glob("seat-*.json"):
            data = json.loads(path.read_text())
            found[data["sid"]] = data
        return found

    def new_seat(self, known: set[str]) -> dict:
        """The one seat that started since KNOWN, waiting for it."""
        def fresh():
            new = [s for sid, s in self.seats().items() if sid not in known]
            return new[0] if len(new) == 1 else None
        seat = wait_for(fresh)
        assert seat, f"no new seat: {self.seats()} {self.tmux('list-panes', '-a').stdout}"
        return seat

    def trigger(self, sid: str, *argv: str) -> None:
        go = self.ctl / f"go-{sid}"
        (self.ctl / "go.tmp").write_text(json.dumps([*RESPAWN, *argv]))
        os.rename(self.ctl / "go.tmp", go)

    def answer(self, sid: str) -> tuple[int, str]:
        """The respawn child's exit code and its output."""
        rc = wait_for(lambda: (self.ctl / f"rc-{sid}").exists())
        assert rc, "the seat never ran the respawn"
        return (int((self.ctl / f"rc-{sid}").read_text()),
                (self.ctl / f"out-{sid}").read_text())


# ---------- (i)-(v): a hand-started seat, a written record, a stub `session` --------------------

class Hand(Server):
    def __init__(self, tmp: Path, monkeypatch):
        self.bin = tmp / "bin"
        self.bin.mkdir()
        ctl = tmp / "ctl"
        stub = self.bin / "session"
        stub.write_text(f"#!{sys.executable}\n" + STUB_SESSION.replace("@@CTL@@",
                                                                      repr(str(ctl))))
        stub.chmod(0o755)
        # Before the server is born: the pane (and so its respawn) finds the stub.
        monkeypatch.setenv("PATH", f"{self.bin}{os.pathsep}{os.environ['PATH']}")
        super().__init__(tmp, dict(os.environ))

    def seat(self, name: str = "reviewer", line: str | None = "session start --in-tmux reviewer",
             directory: Path | None = None) -> tuple[str, dict]:
        """A seat in a pane of its own, its launch record holding LINE."""
        here = self.tmp / f"seat-cwd-{name}"
        here.mkdir(exist_ok=True)
        known = set(self.seats())
        pane = self.window(f"{sys.executable} {self.seat_script} fake {name}", here)
        seat = self.new_seat(known)
        if line is not None:
            self.record(seat["sid"], line, directory or self.tmp / "work")
        return pane, seat

    def record(self, sid: str, line: str, directory: Path) -> None:
        directory.mkdir(exist_ok=True)
        launch = settings.session_dir(sid) / "launch"
        launch.mkdir(parents=True)
        (launch / "command").write_text(render_command(
            ["claude", "--session-id", sid], "session start", str(directory),
            launch_command=line, launch_dir=str(directory)))

    def stubs(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in self.ctl.glob("stub-*.json")]


@pytest.fixture
def hand(tmp_path, monkeypatch):
    return Hand(tmp_path, monkeypatch)


def log() -> Path:
    return settings.log_dir(settings.PACKAGES["session"]) / respawn.LOG_FILE


def message_files() -> list[Path]:
    root = Path(_pane.launches_root())
    return sorted(root.glob("*/*")) if root.exists() else []


def untouched(server: Server, pane: str, seat: dict, start_command: str) -> None:
    assert alive(seat["pid"]), "the seat in the pane is still the seat"
    assert server.fact(pane, "#{pane_start_command}") == start_command
    assert server.fact(pane, "#{pane_dead}") == "0"


def test_i_go_respawns_the_own_pane_with_the_records_line(hand, tmp_path):
    entered = "session start --in-tmux reviewer check the ZEBRA"
    pane, seat = hand.seat(line=entered)
    pid_before = hand.fact(pane, "#{pane_pid}")
    hand.trigger(seat["sid"], "--go")

    [stub] = wait_for(hand.stubs) or [None]
    assert stub, f"the pane never ran `session`: {hand.tmux('list-panes', '-a').stdout}"
    work = os.path.realpath(tmp_path / "work")
    [message] = message_files()
    # the pane line: placement stripped, the words moved to a new 0600 file
    assert stub["argv"] == ["start", "--first-message-file", str(message),
                            "--first-message-file-over", "0", "reviewer"]
    assert message.read_text() == "check the ZEBRA"
    assert stat.S_IMODE(message.stat().st_mode) == 0o600
    assert os.path.realpath(stub["cwd"]) == work, "it runs in launch_dir"
    # the hand-off: the respawned seat would record the line as entered
    assert stub["entered"] == entered
    assert os.path.realpath(stub["entered_dir"]) == work
    # tmux: the same pane, a new process, no first-message word in its line
    assert pane in hand.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.split()
    assert hand.fact(pane, "#{pane_pid}") != pid_before
    started = hand.fact(pane, "#{pane_start_command}")
    assert "ZEBRA" not in started and "check" not in started
    assert "--first-message-file" in started
    assert hand.tmux("show-options", "-p", "-t", pane, "-v",
                     "remain-on-exit").stdout.strip() == "failed"
    # the log line came BEFORE the kill, and names no word either
    assert wait_for(lambda: not alive(seat["pid"]), 10), "the old seat was killed"
    text = log().read_text()
    assert text.count("\n") == 1 and f"{hand.socket}:{pane}" in text
    assert "ZEBRA" not in text
    assert log().stat().st_mtime_ns <= stub["t_ns"]
    assert stat.S_IMODE(log().stat().st_mode) == 0o600


def test_ii_without_go_the_pane_and_the_log_are_unchanged(hand):
    pane, seat = hand.seat()
    start_command = hand.fact(pane, "#{pane_start_command}")
    pid = hand.fact(pane, "#{pane_pid}")
    hand.trigger(seat["sid"])
    code, out = hand.answer(seat["sid"])
    assert code == 0, out
    assert out.startswith("session respawn: plan, nothing touched (--go acts)")
    assert f"%{pane[1:]} on {hand.socket}, by $TMUX_PANE, confirmed by process ancestry" in out
    untouched(hand, pane, seat, start_command)
    assert hand.fact(pane, "#{pane_pid}") == pid
    assert not log().exists() and message_files() == [] and hand.stubs() == []


def test_iii_two_seats_in_one_pane_refuse_and_change_nothing(hand):
    here = hand.tmp / "two"
    here.mkdir()
    two = (f"{sys.executable} {hand.seat_script} fake twoa & "
           f"{sys.executable} {hand.seat_script} fake twob & wait")
    pane = hand.window(f"sh -c '{two}'", here)
    assert wait_for(lambda: len(hand.seats()) == 2)
    seats = list(hand.seats().values())
    for seat in seats:
        hand.record(seat["sid"], "session start twoa", hand.tmp / "work")
    start_command = hand.fact(pane, "#{pane_start_command}")
    pid = hand.fact(pane, "#{pane_pid}")
    hand.trigger(seats[0]["sid"], "--go")
    code, out = hand.answer(seats[0]["sid"])
    assert code == 2, out
    assert f"ambiguous: 2 live seats in pane {pane}" in out
    for seat in seats:
        untouched(hand, pane, seat, start_command)
    assert hand.fact(pane, "#{pane_pid}") == pid
    assert not log().exists() and message_files() == [] and hand.stubs() == []


def test_iv_sid_go_naming_another_pane_refuses_by_ruling_3(hand):
    """REPLACED by super ruling 3: SID --go naming another pane REFUSES with
    the ruling's reason; that pane's pid, pane_start_command and the log are
    unchanged; the caller survives."""
    caller_pane, caller = hand.seat(name="reviewer")
    other_pane, other = hand.seat(name="scout", line="session start scout review the ZEBRA")
    start_command = hand.fact(other_pane, "#{pane_start_command}")
    pid = hand.fact(other_pane, "#{pane_pid}")
    hand.trigger(caller["sid"], other["sid"][:8], "--go")
    code, out = hand.answer(caller["sid"])
    assert code == 2, out
    assert "acting on another seat is not built; see the plan" in out
    assert other_pane in out
    untouched(hand, other_pane, other, start_command)
    assert hand.fact(other_pane, "#{pane_pid}") == pid
    assert alive(caller["pid"]), "the caller survives"
    assert hand.fact(caller_pane, "#{pane_dead}") == "0"
    assert not log().exists() and message_files() == [] and hand.stubs() == []


def test_v_an_unwritable_log_dir_refuses_and_the_pane_is_untouched(hand):
    pane, seat = hand.seat(line="session start reviewer review the ZEBRA")
    start_command = hand.fact(pane, "#{pane_start_command}")
    pid = hand.fact(pane, "#{pane_pid}")
    logs = log().parent
    logs.mkdir(parents=True)
    logs.chmod(0o500)
    try:
        hand.trigger(seat["sid"], "--go")
        code, out = hand.answer(seat["sid"])
    finally:
        logs.chmod(0o700)
    assert code == 2, out
    assert "respawn.log cannot be written" in out and "the pane is untouched" in out
    untouched(hand, pane, seat, start_command)
    assert hand.fact(pane, "#{pane_pid}") == pid
    assert not log().exists() and message_files() == [] and hand.stubs() == []


# ---------- (vi)-(vii): the REAL `session start`, the seat as its vendor -----------------------

class Real(Server):
    """A seat launched by this checkout's `session start` in a pane of the
    private server: `claude` is the seat script, `session` the real CLI."""

    def __init__(self, tmp: Path, agent: str, front_matter: str, linger: float = 0):
        env, _record, self.work = e2e_env(tmp, front_matter=front_matter, agent=agent)
        self.bin = tmp / "bin"
        # The default seat's gate asks `message box path`: the box exists.
        # The stub goes IN the wall (this test's own scratch walled-bin),
        # where `private_env` allows a `message`.
        message_stub(Path(walled_bin()))
        super().__init__(tmp, env, linger)
        (self.bin / "claude").write_text(f"#!{sys.executable}\n"
                                         + self.seat_script.read_text())
        (self.bin / "claude").chmod(0o755)

    def launch(self, line: str) -> tuple[str, dict]:
        pane = self.window(line, self.work)
        seat = self.new_seat(set())
        return pane, seat

    def launch_command(self, sid: str) -> str:
        path = settings.session_dir(sid, self.env) / "launch" / "command"
        assert wait_for(path.exists), f"no launch record for {sid}"
        return parse_command(path.read_text())["launch_command"]


MATERIALISED = re.compile(r"materialised at (\S+) \(")


def said(seat: dict) -> str:
    """The first message the vendor was given: the argument after `--`
    (`session start` may add its own lines after the words). A message that
    came by `--first-message-file ... -over 0` is materialised to a file and
    the vendor is handed a pointer to it (ST-285): its text is read there."""
    argv = seat["argv"]
    message = argv[argv.index("--") + 1] if "--" in argv[:-1] else ""
    pointer = MATERIALISED.search(message)
    return Path(pointer.group(1)).read_text() if pointer else message


def test_vi_end_to_end_the_entered_line_survives_two_respawns(tmp_path):
    real = Real(tmp_path, "scout", "claude:\n  effort: high\n")
    entered = "session start scout review the ZEBRA"
    pane, first = real.launch(entered)
    assert real.launch_command(first["sid"]) == entered
    assert said(first).startswith("review the ZEBRA")

    real.trigger(first["sid"], "--go")
    second = real.new_seat({first["sid"]})
    assert real.launch_command(second["sid"]) == entered, "the hand-off held"
    assert said(second).startswith("review the ZEBRA"), "the words reached the vendor"
    assert os.path.realpath(second["cwd"]) == os.path.realpath(real.work)
    started = real.fact(pane, "#{pane_start_command}")
    assert "ZEBRA" not in started and "--first-message-file" in started

    # the first respawn's message file goes (ST-286 would sweep it): the
    # record never named it, so the second respawn does not need it
    gone = sorted(Path(_pane.launches_root(real.env)).glob("*/*"))
    assert gone, "the first respawn wrote a message file"
    for path in gone:
        path.unlink()
    real.trigger(second["sid"], "--go")
    third = real.new_seat({first["sid"], second["sid"]})
    assert real.launch_command(third["sid"]) == entered
    assert said(third).startswith("review the ZEBRA")
    lines = (settings.log_dir(settings.PACKAGES["session"], real.env)
             / respawn.LOG_FILE).read_text().splitlines()
    assert len(lines) == 2 and all(f"{real.socket}:{pane}" in line for line in lines)


@pytest.mark.skipif(sys.platform != "darwin",
                    reason="the default seat is macOS-only until sessionmail SM-932")
def test_vii_a_held_default_seat_respawns_and_keeps_the_seat(tmp_path):
    """Design §8: `respawn-pane -k` does not wait for the old seat to die.
    Here it lingers 5s after SIGHUP, so its claim and sidecar are still live
    when the new `session start` checks the seat: `holders.replacing` (same
    pane, direct path, not an ancestor) must exempt it, or the new seat
    would start plain."""
    real = Real(tmp_path, "scout", "", linger=5)
    word = default_seat.word()
    claim = Path(real.env["SESSION_STATE_DIR"]) / "holders" / f"{word}.json"
    pane, first = real.launch("session start")
    assert first["argv"][first["argv"].index("--agent") + 1] == word, "it took the seat"
    assert json.loads(claim.read_text())["pid"] == first["pid"]

    real.trigger(first["sid"], "--go")
    second = real.new_seat({first["sid"]})
    hup = wait_for(lambda: (real.ctl / f"hup-{first['sid']}.json").exists())
    assert hup, "the old seat got SIGHUP"
    exited = wait_for(lambda: (real.ctl / f"exit-{first['sid']}.json").exists(), 15)
    assert exited
    gone_at = json.loads((real.ctl / f"exit-{first['sid']}.json").read_text())["t_ns"]
    assert gone_at > second["t_ns"], "the old holder was alive while the new one launched"
    assert "--agent" in second["argv"], ("the respawn was started plain: "
                                         + real.tmux("capture-pane", "-p", "-t", pane).stdout
                                         + claim.read_text())
    assert second["argv"][second["argv"].index("--agent") + 1] == word
    assert json.loads(claim.read_text())["pid"] == second["pid"]
    assert real.launch_command(second["sid"]) == "session start"
    assert real.fact(pane, "#{pane_dead}") == "0"


def test_viii_end_to_end_the_typed_model_pin_survives_the_respawn(tmp_path):
    """README criterion 3 (ST-30 verify-001): `--go` respawns with the same
    launch_command AND model pin. The pin typed in the line reaches the
    respawned vendor's argv, and the record still holds the line as entered."""
    real = Real(tmp_path, "scout", "claude:\n  effort: high\n")
    entered = "session start scout review the ZEBRA --model claude-sonnet-5"
    pane, first = real.launch(entered)
    assert real.launch_command(first["sid"]) == entered

    def pin(seat: dict) -> list[str]:
        argv = seat["argv"]
        return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--model"]

    assert pin(first) == ["claude-sonnet-5"], first["argv"]
    real.trigger(first["sid"], "--go")
    second = real.new_seat({first["sid"]})
    assert pin(second) == ["claude-sonnet-5"], second["argv"]
    assert real.launch_command(second["sid"]) == entered, "the hand-off held"
    assert said(second).startswith("review the ZEBRA")
    assert real.fact(pane, "#{pane_dead}") == "0"
