"""The sound engine's hook side (ST-132 do-002): `busy.player`, the engine's
record, `sessiontools.sound` (python's client), the native sender
`src/sessiontools/sound/event.swift`, and the engine branch of
`hooks/busy-sound`. design.md decisions 2, 4 and 5.

No test opens an audio device or loads launchd. The "engine" is a UDP socket
the test binds on an ephemeral 127.0.0.1 port (`FakeEngine`), or the do-001
engine itself under `--null-output` (the encoder parity test). Never :44100,
never a live port. The sender and the engine are built once per session with
the system `swiftc`; those tests skip cleanly without it.

"Nothing was sent" is checked without sleeping: after the hook returns, the
test sends a MARK datagram to the same socket and reads up to it. Loopback
delivers in send order, and the sender has exited, so anything it sent is
already queued ahead of the mark.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sessiontools import busy
from sessiontools import sound as engine_client

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "busy-sound"
HOOKS = ROOT / "hooks" / "hooks.json"
SCHEMA = ROOT / "settings" / "sounds.schema.json"
SOUND_SRC = ROOT / "src" / "sessiontools" / "sound"
SID = "0f0e0d0c-0b0a-4908-8706-050403020100"
LIVE_PORTS = {44100, 8890, 2022, 8880}
MARK = b"MARK"


# --- the binaries, built once ----------------------------------------------------------

def _build(tmp_path_factory, source: str, name: str) -> Path:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        pytest.skip("the sound binaries build with swiftc on macOS only")
    out = tmp_path_factory.mktemp(name) / name
    subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", str(out), str(SOUND_SRC / source)],
                   check=True, capture_output=True, timeout=600)
    return out


@pytest.fixture(scope="session")
def sender_bin(tmp_path_factory) -> Path:
    return _build(tmp_path_factory, "event.swift", "sound-event")


@pytest.fixture(scope="session")
def engine_bin(tmp_path_factory) -> Path:
    return _build(tmp_path_factory, "engine.swift", "sound-engine")


# --- a fake engine: a UDP socket on an ephemeral port ------------------------------------

class FakeEngine:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        assert self.port not in LIVE_PORTS

    def drain(self) -> list[bytes]:
        """Every datagram that arrived before now (see the module doc)."""
        poke = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            poke.sendto(MARK, ("127.0.0.1", self.port))
        finally:
            poke.close()
        got = []
        while True:
            data = self.sock.recv(65536)
            if data == MARK:
                return got
            got.append(data)

    def close(self):
        self.sock.close()


@pytest.fixture
def fake():
    f = FakeEngine()
    yield f
    f.close()


def dead_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_pid():
    """A second live pid: not this process, and never 1, whoever started pytest.
    A child that sleeps until teardown, then is killed and reaped by its handle."""
    p = subprocess.Popen(["/bin/sleep", "600"])
    try:
        yield p.pid
    finally:
        p.kill()
        p.wait()


def decode(data: bytes) -> tuple[str, str, float | None]:
    """(event, sid, t) of one /sound/event datagram, checking its framing."""
    def string(off):
        end = data.index(b"\0", off)
        nxt = (end + 4) & ~3
        assert data[end:nxt] == b"\0" * (nxt - end)
        return data[off:end].decode(), nxt

    address, off = string(0)
    tags, off = string(off)
    assert address == "/sound/event" and tags in (",ssd", ",ss")
    ev, off = string(off)
    sid, off = string(off)
    t = None
    if tags == ",ssd":
        (t,) = struct.unpack(">d", data[off:off + 8])
        off += 8
    assert off == len(data)
    return ev, sid, t


# --- the wire: python's encoder, byte for byte ------------------------------------------

def test_encode_is_the_76_byte_wire_of_decision_4():
    t = 1907254800.25
    data = engine_client.encode("arm", SID, t)
    expected = (b"/sound/event\0\0\0\0" + b",ssd\0\0\0\0" + b"arm\0"
                + SID.encode() + b"\0\0\0\0" + struct.pack(">d", t))
    assert data == expected and len(data) == 76


def test_encode_without_t_is_the_short_form():
    assert engine_client.encode("end", "x") == b"/sound/event\0\0\0\0,ss\0end\0x\0\0\0"


def test_the_port_is_one_constant_in_python_and_both_swift_sources():
    assert engine_client.PORT == 44100
    for src, pattern in [("engine.swift", r"let defaultPort = (\d+)"),
                         ("event.swift", r"let defaultPort: UInt16 = (\d+)")]:
        (found,) = re.findall(pattern, (SOUND_SRC / src).read_text())
        assert int(found) == engine_client.PORT, src


@pytest.mark.parametrize("raw,port", [("", 44100), ("51234", 51234), ("0", 44100),
                                      ("70000", 44100), ("x1", 44100), (" 6000 ", 6000)])
def test_the_port_env_moves_it_for_tests_only_when_it_is_a_port(raw, port):
    assert engine_client.port({engine_client.PORT_ENV: raw}) == port


def test_the_sender_home_rule():
    assert engine_client.home({"HOME": "/h"}) == Path("/h/.sessionkit/tools/state/sound")  # fresh: new
    assert engine_client.sender({"HOME": "/h", engine_client.HOME_ENV: "/s"}) == Path(
        "/s/bin/sound-event")


def test_send_arrives_and_a_dead_port_never_raises(fake):
    assert engine_client.send("arm", SID, 12.5, to_port=fake.port) is True
    assert fake.drain() == [engine_client.encode("arm", SID, 12.5)]
    assert engine_client.send("arm", SID, env={engine_client.PORT_ENV: str(dead_port())}) is True
    # A second send on a fresh unconnected socket: no ECONNREFUSED surfaces.
    assert engine_client.send("end", SID, env={engine_client.PORT_ENV: str(dead_port())}) is True


def test_installed_means_an_executable_sender(tmp_path):
    env = {engine_client.HOME_ENV: str(tmp_path)}
    assert engine_client.installed(env) is False
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "sound-event").write_text("#!/bin/sh\n")
    assert engine_client.installed(env) is False
    (tmp_path / "bin" / "sound-event").chmod(0o755)
    assert engine_client.installed(env) is True


# --- the sender: parity with python's subagent rule (decision 5) --------------------------

BIG = "x" * 200_000
CORPUS = {
    "main thread": {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                    "tool_input": {"command": "ls"}},
    "subagent": {"session_id": SID, "agent_id": "a0c2", "agent_type": "general-purpose"},
    "nested key": {"tool_input": {"agent_id": "a0c2", "deep": [{"agent_id": "x"}]}},
    "string value": {"tool_input": {"prompt": "agent_id"}, "note": "agent_id"},
    "escaped in a string": {"tool_input": {"command": 'echo "agent_id": "a0c2", {"agent_id": 1}'}},
    "null": {"agent_id": None},
    "empty string": {"agent_id": ""},
    "false": {"agent_id": False},
    "zero": {"agent_id": 0},
    "zero float": {"agent_id": 0.0},
    "negative zero": {"agent_id": -0.0},
    "empty list": {"agent_id": []},
    "empty object": {"agent_id": {}},
    "true": {"agent_id": True},
    "one": {"agent_id": 1},
    "small float": {"agent_id": 0.001},
    "negative": {"agent_id": -3},
    "list of zero": {"agent_id": [0]},
    "object": {"agent_id": {"a": 1}},
    "space string": {"agent_id": " "},
    "after nesting": {"tool_input": {"a": [1, {"b": "}]{["}], "c": "\\"}, "agent_id": "a0c2"},
    "backslash before quote": {"x": "\\", "agent_id": "a0c2"},
    "unicode": {"x": "é ✓ 🎵", "agent_id": "a0c2"},
    "big payload, agent last": {"tool_input": {"content": BIG}, "agent_id": "a0c2"},
    "big payload, main": {"tool_input": {"content": BIG}},
    "agent_id-like keys": {"agent_ids": "x", "xagent_id": "y", "agent_i": "z"},
}
RAW_CORPUS = {
    # (JSON text, what python's json.loads makes of agent_id's truth)
    "pretty printed": ('{\n  "session_id" : "x",\n  "agent_id" :\n   "a0c2"\n}\n', True),
    "duplicate key, last null": ('{"agent_id": "a0c2", "agent_id": null}', False),
    "duplicate key, last set": ('{"agent_id": null, "agent_id": "a0c2"}', True),
    "unicode-escaped key": ('{"agent\\u005fid": "a0c2"}', True),
    "escaped quote in the key's neighbour": ('{"a\\"": "agent_id", "agent_id": 0}', False),
    "exponent zero": ('{"agent_id": 0e5}', False),
    "exponent": ('{"agent_id": 1e-3}', True),
}


def run_sender(sender_bin, port, *args, payload: str | None = None) -> None:
    r = subprocess.run([str(sender_bin), "--port", str(port), *args],
                       input=(payload or "").encode(), capture_output=True, timeout=30, check=False)
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")


@pytest.mark.parametrize("name", list(CORPUS))
def test_the_senders_subagent_check_is_pythons(sender_bin, fake, name):
    payload = CORPUS[name]
    run_sender(sender_bin, fake.port, "--hook", "arm", SID, payload=json.dumps(payload))
    sent = fake.drain()
    assert bool(sent) == (not bool(payload.get("agent_id"))), sent
    if sent:
        assert decode(sent[0])[:2] == ("arm", SID)


@pytest.mark.parametrize("name", list(RAW_CORPUS))
def test_the_senders_subagent_check_on_raw_json(sender_bin, fake, name):
    text, truthy = RAW_CORPUS[name]
    assert bool(json.loads(text).get("agent_id")) is truthy      # the corpus is right
    run_sender(sender_bin, fake.port, "--hook", "end", SID, payload=text)
    assert bool(fake.drain()) == (not truthy)


@pytest.mark.parametrize("payload,sent", [
    ("", True), ("  \n", True),                 # python's main reads {} and arms
    ("[1]", False), ("not json", False), ('{"a": 1', False), ('{"a": "x}', False),
    ('{"a": 1} {"b": 2}', False), ("}", False),
])
def test_a_payload_that_is_not_one_object_sends_what_python_would(sender_bin, fake, payload, sent):
    run_sender(sender_bin, fake.port, "--hook", "arm", SID, payload=payload)
    assert bool(fake.drain()) == sent


def test_without_hook_the_sender_reads_no_stdin_and_sends(sender_bin, fake):
    run_sender(sender_bin, fake.port, "stop", SID, payload=json.dumps({"agent_id": "a0c2"}))
    (data,) = fake.drain()
    assert decode(data)[:2] == ("stop", SID)


def test_the_senders_datagram_is_pythons_encoding_76_bytes(sender_bin, fake):
    before = time.time()
    run_sender(sender_bin, fake.port, "--hook", "arm", SID, payload="{}")
    after = time.time()
    (data,) = fake.drain()
    _ev, _sid, t = decode(data)
    assert len(data) == 76
    assert before - 0.01 <= t <= after + 0.01
    assert data == engine_client.encode("arm", SID, t)


def test_the_sender_reads_the_port_env(sender_bin, fake):
    r = subprocess.run([str(sender_bin), "clear", SID], capture_output=True, timeout=30, check=False,
                       env={"SESSIONTOOLS_SOUND_PORT": str(fake.port), "PATH": "/usr/bin:/bin"})
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert [decode(d)[0] for d in fake.drain()] == ["clear"]


@pytest.mark.parametrize("argv", [[], ["arm"], ["arm", SID, "extra"], ["arm", ""], ["", SID],
                                  ["arm", "a/b"], ["arm", ".."], ["arm", "x" * 300],
                                  ["--port", "0", "arm", SID], ["--port", "x", "arm", SID],
                                  ["--port"]])
def test_bad_arguments_send_nothing_exit_0_and_say_nothing(sender_bin, fake, argv):
    r = subprocess.run([str(sender_bin), *argv], capture_output=True, timeout=30, check=False,
                       env={"SESSIONTOOLS_SOUND_PORT": str(fake.port), "PATH": "/usr/bin:/bin"})
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert fake.drain() == []


def test_nothing_listening_is_exit_0_and_silent(sender_bin):
    port = dead_port()
    for _ in range(3):
        run_sender(sender_bin, port, "--hook", "arm", SID, payload="{}")


def test_the_sender_imports_darwin_only():
    """No Foundation: +1.1 ms on every tool (design.md decision 5)."""
    src = (SOUND_SRC / "event.swift").read_text()
    imports = [x.split()[1] for x in src.splitlines() if x.startswith("import ")]
    assert imports == ["Darwin"]


# --- python: busy.player and the record ----------------------------------------------------

class Rig:
    """A private HOME, sessions root, session dir, sound file, fake afplay,
    python3 marker, mouth dir, and an installed (built) sender."""

    def __init__(self, root: Path, port: int, sender: Path | None):
        self.root = root
        self.sessions = root / ".session" / "sessions"
        self.session = self.sessions / SID
        self.session.mkdir(parents=True)
        self.wav = root / "dance.wav"
        self.wav.write_text("RIFF")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.mark = root / "played"
        player = self.bin / "afplay"
        player.write_text(f'#!/bin/sh\necho $$ >> "{self.mark}"\nexec sleep 30\n')
        player.chmod(0o755)
        self.pymark = root / "python-started"
        py = self.bin / "python3"
        py.write_text(f'#!/bin/sh\necho x >> "{self.pymark}"\nexec "{sys.executable}" "$@"\n')
        py.chmod(0o755)
        self.home = root / "sound-home"
        (self.home / "bin").mkdir(parents=True)
        if sender is not None:
            (self.home / "bin" / "sound-event").symlink_to(sender)
        self.port = port
        self.mouth = root / "mouth"

    @property
    def busy(self) -> Path:
        return self.session / "sounds" / "busy"

    def record(self, pid: int | None = None) -> Path:
        return self.busy / f"engine.{pid or os.getpid()}"

    def settings(self, **busy_keys) -> Path:
        block = {"auto_arm": True, "file": str(self.wav), "delay": 8, "player": "engine"}
        block.update(busy_keys)
        block = {k: v for k, v in block.items() if v is not None}
        path = self.session / "settings" / "sounds.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"enabled": True, "busy": block}))
        old = time.time() - 30          # older than any record a test writes
        os.utime(path, (old, old))
        return path

    def env(self, **extra) -> dict:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root),
               "SESSIONS_DIR": str(self.sessions), "CLAUDE_CODE_SESSION_ID": SID,
               "CLAUDE_PID": str(os.getpid()), "VOICEMODE_MOUTH_DIR": str(self.mouth),
               "SESSIONTOOLS_SOUND_HOME": str(self.home),
               "SESSIONTOOLS_SOUND_PORT": str(self.port)}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def shim(self, verb: str, payload: dict | None = None, **env) -> subprocess.CompletedProcess:
        body = {"session_id": SID, "hook_event_name": "x", **(payload or {})}
        r = subprocess.run(["/bin/sh", str(SHIM), verb], input=json.dumps(body),
                           env=self.env(**env), capture_output=True, text=True,
                           timeout=30, check=False)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), r
        return r

    def python_runs(self) -> int:
        try:
            return len(self.pymark.read_text().split())
        except FileNotFoundError:
            return 0

    def players(self) -> list[int]:
        try:
            return [int(x) for x in self.mark.read_text().split()]
        except FileNotFoundError:
            return []

    def teardown(self) -> None:
        for name in busy.SLOTS:
            busy.stop(self.session / "sounds" / name)
        for pid in self.players():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@pytest.fixture
def rig(tmp_path, monkeypatch, fake):
    """A rig whose engine is `fake` and whose sender is a stand-in that only
    needs to be executable (python never runs it)."""
    stand_in = tmp_path / "stand-in-sender"
    stand_in.write_text("#!/bin/sh\nexit 0\n")
    stand_in.chmod(0o755)
    r = Rig(tmp_path, fake.port, stand_in)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SESSIONS_DIR", str(r.sessions))
    monkeypatch.setenv("VOICEMODE_MOUTH_DIR", str(r.mouth))
    # An ambient MOUTH_DIR or SESSIONVOICE_HOME would win over the rig's dir.
    for name in ("MOUTH_DIR", "SESSIONVOICE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", f"{r.bin}:/usr/bin:/bin")
    monkeypatch.setenv(engine_client.PORT_ENV, str(fake.port))
    yield r
    r.teardown()


@pytest.fixture
def shim_rig(tmp_path, monkeypatch, fake, sender_bin):
    """A rig with the REAL sender installed, for the shim."""
    r = Rig(tmp_path, fake.port, sender_bin)
    monkeypatch.setenv(engine_client.PORT_ENV, str(fake.port))
    yield r
    r.teardown()


@pytest.mark.parametrize("value,player", [(None, "afplay"), ("afplay", "afplay"),
                                          ("engine", "engine")])
def test_the_player_key(rig, value, player):
    rig.settings(player=value)
    sound = busy.config(SID)
    assert sound is not None and sound.player == player


@pytest.mark.parametrize("value", ["Engine", "AFPLAY", "mouth", "", 5, True, None, ["engine"]])
def test_any_other_player_is_silent_in_every_mode(rig, value):
    path = rig.settings()
    doc = json.loads(path.read_text())
    doc["busy"]["player"] = value                 # null included: set, and not a player
    path.write_text(json.dumps(doc))
    assert busy.config(SID) is None


def test_unset_player_is_todays_sound_exactly(rig):
    rig.settings(player=None)
    wav = rig.wav
    assert busy.config(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True)


def test_the_engine_arm_writes_the_record_and_sends_arm(rig, fake):
    rig.settings(delay=2.5)
    before = time.time()
    assert busy.arm({"session_id": SID}, rig.env()) == busy.ENGINE
    after = time.time()
    rec = rig.record()
    body = json.loads(rec.read_text())
    assert body == {"v": 1, "file": str(rig.wav), "delay": 2.5, "pid": os.getpid(),
                    "mouth": str(rig.mouth)}
    st = rec.stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert before - 1 - 0.01 <= st.st_mtime <= after - 1 + 0.01     # back-dated 1 s, as `off`
    assert not (rig.busy / "token").exists() and not (rig.busy / "off").exists()
    assert [x for x in rig.busy.iterdir() if x.name.startswith(".engine.")] == []   # no temp
    (data,) = fake.drain()
    assert decode(data)[:2] == ("arm", SID)
    assert rig.players() == [], "the engine path started afplay"


def test_the_engine_arm_unlinks_every_other_record(rig, fake):
    rig.settings()
    rig.busy.mkdir(parents=True)
    old = rig.record(999_999)
    old.write_text("{}")
    keep = rig.busy / "engine.notapid"
    keep.write_text("x")
    busy.arm({"session_id": SID}, rig.env())
    assert sorted(p.name for p in busy.records(rig.busy)) == [rig.record().name]
    assert keep.exists(), "only engine.<digits> is a record"


def test_a_subagents_arm_writes_and_sends_nothing(rig, fake):
    rig.settings()
    assert busy.arm({"session_id": SID, "agent_id": "a0c2"}, rig.env()) is None
    assert not rig.busy.exists() or busy.records(rig.busy) == []
    assert fake.drain() == []


def test_not_installed_writes_the_record_and_sends_nothing(rig, fake):
    rig.settings()
    env = rig.env(SESSIONTOOLS_SOUND_HOME=str(rig.root / "nowhere"))
    assert busy.arm({"session_id": SID}, env) is None
    assert rig.record().exists(), "the record keeps the shim on its fast path"
    assert fake.drain() == []


def test_no_claude_pid_means_no_record_and_no_sound(rig, fake, monkeypatch):
    rig.settings()
    monkeypatch.setattr(busy, "claude_pid", lambda env: None)
    assert busy.arm({"session_id": SID}, rig.env()) is None
    assert not rig.busy.exists() or busy.records(rig.busy) == []
    assert fake.drain() == []


def test_switching_to_afplay_unlinks_the_record_tells_the_engine_and_plays(rig, fake):
    rig.settings(delay=0.1)
    busy.arm({"session_id": SID}, rig.env())
    assert fake.drain()
    rig.settings(delay=0.1, player="afplay")
    token = busy.arm({"session_id": SID}, rig.env())
    assert token and token != busy.ENGINE
    assert busy.records(rig.busy) == []
    assert [decode(d)[:2] for d in fake.drain()] == [("clear", SID)]
    deadline = time.monotonic() + 5
    while not rig.players() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert rig.players(), "afplay never started after the switch"


def test_switching_off_unlinks_the_record_and_tells_the_engine(rig, fake):
    rig.settings()
    busy.arm({"session_id": SID}, rig.env())
    fake.drain()
    rig.settings(auto_arm=False)
    assert busy.arm({"session_id": SID}, rig.env()) is None
    assert (rig.busy / "off").exists() and busy.records(rig.busy) == []
    assert [decode(d)[0] for d in fake.drain()] == ["clear"]


def test_an_afplay_seat_with_no_records_sends_nothing(rig, fake):
    rig.settings(player="afplay", delay=30)
    assert busy.arm({"session_id": SID}, rig.env()) not in (None, busy.ENGINE)
    assert fake.drain() == []


# --- the shim: the engine branch ----------------------------------------------------------

def events(fake) -> list[tuple[str, str]]:
    return [decode(d)[:2] for d in fake.drain()]


def test_after_the_first_arm_the_engine_path_starts_no_python(shim_rig, fake):
    """The fast path: python once (it writes the record and sends arm), then
    every arm, end, stop and clear is the shim plus the native sender."""
    shim_rig.settings()
    shim_rig.shim("arm")
    assert shim_rig.python_runs() == 1 and shim_rig.record().exists()
    assert events(fake) == [("arm", SID)]
    for _ in range(3):
        shim_rig.shim("arm")
        shim_rig.shim("arm")
        shim_rig.shim("end")
    shim_rig.shim("stop", {"hook_event_name": "Stop"})
    shim_rig.shim("clear", {"hook_event_name": "UserPromptSubmit"})
    assert shim_rig.python_runs() == 1, "python ran on the engine's fast path"
    assert [e for e, _ in events(fake)] == ["arm", "arm", "end"] * 3 + ["stop", "clear"]
    assert shim_rig.players() == []


def test_an_engine_seat_with_a_stop_sound_plays_it_and_the_record_survives(shim_rig, fake):
    """ST-236 (design Test 11): on an engine seat the stop sound plays through
    the mouth at Stop, the stop path never touches `sounds/busy/` (the engine's
    record survives), and nothing is sent beyond the shim's own `stop`."""
    path = shim_rig.settings()
    done = shim_rig.root / "done.aiff"
    done.write_text("FORM")
    data = json.loads(path.read_text())
    data["stop"] = {"file": str(done)}
    stat_before = path.stat()
    path.write_text(json.dumps(data))
    os.utime(path, (stat_before.st_atime, stat_before.st_mtime))   # still old
    log = shim_rig.root / "mouth.log"
    mouth = shim_rig.bin / "mouth"
    mouth.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
    mouth.chmod(0o755)
    shim_rig.shim("arm")
    assert shim_rig.python_runs() == 1 and shim_rig.record().exists()
    assert not (shim_rig.session / "sounds" / "stop" / "off").exists()
    assert events(fake) == [("arm", SID)]
    record = shim_rig.record().read_bytes()
    shim_rig.shim("stop", {"hook_event_name": "Stop"})
    assert shim_rig.python_runs() == 2                    # ON: python at Stop
    assert events(fake) == [("stop", SID)]
    assert shim_rig.record().read_bytes() == record
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not log.exists():
        time.sleep(0.02)
    assert log.read_text().splitlines() == [f"play {done}"]
    assert shim_rig.players() == []                       # no afplay


def test_the_shim_sends_the_exact_76_byte_datagram(shim_rig, fake):
    shim_rig.settings()
    shim_rig.shim("arm")
    fake.drain()
    before = time.time()
    shim_rig.shim("arm", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    after = time.time()
    (data,) = fake.drain()
    _ev, _sid, t = decode(data)
    assert len(data) == 76 and before - 0.01 <= t <= after + 0.01
    assert data == engine_client.encode("arm", SID, t)


def test_through_the_shim_a_subagents_arm_and_end_are_not_sent_but_stop_and_clear_are(
        shim_rig, fake):
    shim_rig.settings()
    shim_rig.shim("arm")
    fake.drain()
    sub = {"agent_id": "a0c2"}
    shim_rig.shim("arm", sub)
    shim_rig.shim("end", sub)
    assert fake.drain() == []
    shim_rig.shim("stop", sub)
    shim_rig.shim("clear", sub)
    assert [e for e, _ in events(fake)] == ["stop", "clear"]
    assert shim_rig.python_runs() == 1


def test_turn_mark_on_and_present_a_subagents_arm_is_still_not_sent(shim_rig, fake):
    """design-001 review, finding 1: with the mark on and present, the shim
    has read stdin into $payload; the sender must get THAT copy, or it reads
    an empty stdin and the subagent filter passes vacuously."""
    shim_rig.settings()
    shim_rig.shim("arm")
    fake.drain()
    mark = shim_rig.session / "turn-ended"
    mark.write_text('{"v": 1, "at": 1.0}\n')
    shim_rig.shim("arm", {"agent_id": "a0c2"}, SESSIONTOOLS_HOOK_TURN="on")
    assert fake.drain() == []
    assert mark.exists(), "a subagent's event changed the turn mark"
    shim_rig.shim("arm", SESSIONTOOLS_HOOK_TURN="on")
    assert events(fake) == [("arm", SID)]
    assert not mark.exists()


def test_turn_mark_on_stop_still_sets_the_mark_and_sends_stop(shim_rig, fake):
    shim_rig.settings()
    shim_rig.shim("arm")
    fake.drain()
    shim_rig.shim("stop", {"hook_event_name": "Stop"}, SESSIONTOOLS_HOOK_TURN="on")
    assert (shim_rig.session / "turn-ended").exists()
    assert events(fake) == [("stop", SID)]


@pytest.mark.parametrize("parent_is_init", [False, True], ids=["any-parent", "parent-pid-1"])
def test_a_resumed_seat_a_new_claude_pid_re_resolves_its_own_record(shim_rig, fake, live_pid,
                                                                     monkeypatch, parent_is_init):
    # parent-pid-1: an orphaned run, whose parent is pid 1, a pid no seat may hold.
    if parent_is_init:
        monkeypatch.setattr(os, "getppid", lambda: 1)
    shim_rig.settings()
    first, resumed = os.getpid(), live_pid             # both alive, both > 1
    shim_rig.shim("arm", CLAUDE_PID=str(first))
    assert shim_rig.record(first).exists()
    shim_rig.shim("arm", CLAUDE_PID=str(resumed))
    assert shim_rig.python_runs() == 2, "the new pid did not re-resolve"
    assert [p.name for p in busy.records(shim_rig.busy)] == [f"engine.{resumed}"]
    shim_rig.shim("arm", CLAUDE_PID=str(resumed))
    assert shim_rig.python_runs() == 2
    assert [e for e, _ in events(fake)] == ["arm", "arm", "arm"]


def test_a_session_layer_newer_than_the_record_re_resolves(shim_rig, fake):
    path = shim_rig.settings()
    shim_rig.shim("arm")
    new = time.time() + 5
    os.utime(path, (new, new))
    shim_rig.shim("arm")
    assert shim_rig.python_runs() == 2


def test_switching_to_afplay_mid_session_through_the_shim(shim_rig, fake):
    path = shim_rig.settings()
    shim_rig.shim("arm")
    fake.drain()
    doc = json.loads(path.read_text())
    doc["busy"].update(player="afplay", delay=30)
    path.write_text(json.dumps(doc))
    new = time.time() + 5
    os.utime(path, (new, new))
    shim_rig.shim("arm")
    assert events(fake) == [("clear", SID)]
    assert (shim_rig.busy / "token").exists() and busy.records(shim_rig.busy) == []
    shim_rig.shim("end")
    assert fake.drain() == [] and not (shim_rig.busy / "token").exists()


def test_a_stale_record_beside_an_armed_afplay_slot_end_reaches_both(shim_rig, fake):
    """`end` must go to the sender AND to python: the shim keeps one copy of
    stdin. A subagent's end goes to neither."""
    shim_rig.settings(player="afplay", delay=30)
    shim_rig.shim("arm")
    assert (shim_rig.busy / "token").exists()
    shim_rig.record().write_text("{}")             # a stale record
    shim_rig.shim("end", {"agent_id": "a0c2"})
    assert fake.drain() == [] and (shim_rig.busy / "token").exists()
    shim_rig.shim("end")
    assert events(fake) == [("end", SID)]
    assert not (shim_rig.busy / "token").exists(), "python never got the payload"


def test_without_claude_pid_stop_and_clear_glob_for_a_record(shim_rig, fake):
    """No arm came first (the record is written by hand), so the first Stop
    starts python once to stamp the stop sound's `off` (it has no stamp yet);
    the second Stop starts none. The datagrams are as before."""
    shim_rig.busy.mkdir(parents=True)
    shim_rig.record(424242).write_text("{}")
    shim_rig.shim("stop", CLAUDE_PID=None)
    shim_rig.shim("clear", CLAUDE_PID=None)
    shim_rig.shim("end", CLAUDE_PID="")
    assert [e for e, _ in events(fake)] == ["stop", "clear", "end"]
    assert shim_rig.python_runs() == 1
    assert (shim_rig.session / "sounds" / "stop" / "off").exists()
    shim_rig.shim("stop", CLAUDE_PID=None)
    assert [e for e, _ in events(fake)] == ["stop"]
    assert shim_rig.python_runs() == 1
    assert busy.records(shim_rig.busy) != []


def test_with_claude_pid_a_record_for_another_pid_is_not_this_seats(shim_rig, fake):
    shim_rig.busy.mkdir(parents=True)
    shim_rig.record(424242).write_text("{}")
    shim_rig.shim("end")
    assert fake.drain() == [] and shim_rig.python_runs() == 0


def test_engine_not_installed_the_hook_exits_0_silent_and_sends_nothing(shim_rig, fake):
    """Fail silent (README success criterion): no sender, then nothing is
    sent, ever; the record still keeps python off the fast path."""
    shim_rig.settings()
    gone = {"SESSIONTOOLS_SOUND_HOME": str(shim_rig.root / "nowhere")}
    shim_rig.shim("arm", **gone)
    for verb in ("arm", "end", "stop", "clear"):
        shim_rig.shim(verb, **gone)
    assert fake.drain() == []
    assert shim_rig.python_runs() == 1


@pytest.mark.parametrize("layout", ["old-only", "fresh", "migrated"])
def test_the_hooks_sender_is_where_sound_home_says(shim_rig, fake, layout):
    """ST-266 do-003 (the decompose review's busy-sound rule): with no
    SESSIONTOOLS_SOUND_HOME, the engine is installed wherever sound.home()
    resolves (the old folder while it is real; else the new one, the fresh
    default now) and the hook's bash rule must find the same sender with no
    python: its fast-path `end` reaches the engine. A wrong guess is silent."""
    home = {"HOME": str(shim_rig.root)}
    old = shim_rig.root / ".local/share/sessiontools/sound"
    new = shim_rig.root / ".sessionkit/tools/state/sound"
    if layout == "old-only":
        old.mkdir(parents=True)
    if layout == "migrated":
        new.mkdir(parents=True)
        old.parent.mkdir(parents=True)
        old.symlink_to(new)
    want = {"old-only": old, "fresh": new, "migrated": new}[layout]
    assert engine_client.home(home) == want
    sender = engine_client.sender(home)
    sender.parent.mkdir(parents=True, exist_ok=True)
    sender.symlink_to(shim_rig.home / "bin" / "sound-event")    # the real, built sender
    shim_rig.settings()
    unset = {"SESSIONTOOLS_SOUND_HOME": None}
    shim_rig.shim("arm", **unset)                                # python: the record, and arm
    shim_rig.shim("end", **unset)                                # the shim and the sender alone
    assert events(fake) == [("arm", SID), ("end", SID)]
    assert shim_rig.python_runs() == 1


def test_nothing_listening_the_hook_exits_0_silent(shim_rig):
    shim_rig.settings()
    port = {"SESSIONTOOLS_SOUND_PORT": str(dead_port())}
    t0 = time.monotonic()
    for verb in ("arm", "arm", "end", "stop", "clear"):
        shim_rig.shim(verb, **port)
    assert time.monotonic() - t0 < 20


def test_a_seat_without_the_key_is_unchanged_no_record_no_datagram(shim_rig, fake):
    shim_rig.settings(player=None, delay=30)
    shim_rig.shim("arm")
    assert (shim_rig.busy / "token").exists()
    assert busy.records(shim_rig.busy) == []
    shim_rig.shim("end")
    assert fake.drain() == [] and not (shim_rig.busy / "token").exists()


# --- the two encoders into the real engine (do-001 review, N2) -----------------------------

class NullEngine:
    def __init__(self, engine_bin: Path, sessions: Path):
        self.proc = subprocess.Popen(
            [str(engine_bin), "--null-output", "--port", "0", "--sessions", str(sessions),
             "--null-length", "30"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.log: list[dict] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        ready = self.wait_for("ready", timeout=20)
        assert ready, self.log
        self.port = ready["port"]
        assert self.port not in LIVE_PORTS

    def _read(self):
        for line in self.proc.stderr:
            with self._cv:
                self.log.append(json.loads(line))
                self._cv.notify_all()

    def wait_for(self, ev, n=1, timeout=5.0):
        end = time.monotonic() + timeout
        with self._cv:
            while True:
                got = [x for x in self.log if x["ev"] == ev]
                if len(got) >= n:
                    return got[n - 1]
                left = end - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return None
                self._cv.wait(left)

    def close(self):
        if self.proc.poll() is None:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)   # our own group
        self.proc.wait(timeout=10)


def test_python_send_and_the_sender_both_drive_the_do_001_engine(engine_bin, sender_bin, rig):
    rig.settings(delay=0)
    busy.write_record(rig.busy, busy.config(SID), os.getpid(), time.time())
    eng = NullEngine(engine_bin, rig.sessions)
    try:
        env = {engine_client.PORT_ENV: str(eng.port)}
        assert engine_client.send("arm", SID, env=env)                    # python's encoder
        assert eng.wait_for("played"), eng.log
        run_sender(sender_bin, eng.port, "--hook", "end", SID, payload="{}")   # the sender's
        stopped = eng.wait_for("stopped")
        assert stopped and stopped["by"] == "end" and stopped["from"] == "playing"
        run_sender(sender_bin, eng.port, "--hook", "arm", SID, payload="{}")
        assert eng.wait_for("played", n=2), eng.log
        assert engine_client.send("clear", SID, env=env)
        assert eng.wait_for("stopped", n=2)["by"] == "clear"
        assert not [x for x in eng.log if x["ev"] in ("refused", "no-record", "seat-gone")]
    finally:
        eng.close()


# --- the wiring and the docs ---------------------------------------------------------------

def test_the_schema_names_the_player():
    player = json.loads(SCHEMA.read_text())["properties"]["busy"]["properties"]["player"]
    assert player["enum"] == ["afplay", "engine"] and player["type"] == "string"


def test_hooks_json_names_the_player_and_the_port():
    description = json.loads(HOOKS.read_text())["description"]
    assert "busy.player" in description and "127.0.0.1:44100" in description


def test_the_shim_still_parses():
    assert subprocess.run(["/bin/sh", "-n", str(SHIM)], check=False).returncode == 0


# --- no external process on the fast path (do-010; design.md decision 5) ------------------

def tripwire_path(root: Path) -> tuple[str, Path]:
    """A PATH where EVERY command the shim could find by name is a tripwire:
    one stub, linked under each name in /bin and /usr/bin (plus python3 and
    afplay), that logs the name it was run as. The sender is run by its full
    path, so it still sends. Builtins (`[`, printf, read, command -v) never
    touch PATH, so the log holds exactly the external commands run by name."""
    trip = root / "tripwire"
    trip.mkdir()
    log = root / "tripped"
    stub = root / "tripwire-stub"
    stub.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "${{0##*/}}" >> "{log}"\n')
    stub.chmod(0o755)
    names = {"python3", "afplay"}
    for d in ("/bin", "/usr/bin"):
        names.update(p.name for p in Path(d).iterdir())
    for name in names:
        (trip / name).symlink_to(stub)
    return str(trip), log


def tripped(log: Path) -> list[str]:
    try:
        return log.read_text().split()
    except FileNotFoundError:
        return []


def test_the_engine_fast_path_runs_no_external_command(shim_rig, fake):
    """do-005 measured `src=$(dirname "$0")/../src` forking dirname on EVERY
    hook call (+3.15 ms). After the first arm, the engine's arm, end, stop
    and clear run builtins and the sender only: nothing is looked up on PATH."""
    shim_rig.settings()
    shim_rig.shim("arm")                     # python resolves and writes the record
    fake.drain()
    path, log = tripwire_path(shim_rig.root)
    for verb in ("arm", "arm", "end", "stop", "clear"):
        shim_rig.shim(verb, PATH=path)
    ran = tripped(log)
    assert ran == [], f"the engine's fast path ran external commands: {ran}"
    assert [e for e, _ in events(fake)] == ["arm", "arm", "end", "stop", "clear"]
    assert shim_rig.python_runs() == 1


def test_the_stat_fast_paths_run_no_external_command(shim_rig, fake):
    """The same for the shim's other fast paths: busy switched off, an arm
    with its token in place, and an end or clear with nothing armed."""
    path, log = tripwire_path(shim_rig.root)
    shim_rig.shim("arm", PATH=path, SESSIONTOOLS_HOOK_BUSY="off")
    shim_rig.shim("end", PATH=path)          # nothing armed in any slot
    shim_rig.shim("clear", PATH=path)
    shim_rig.busy.mkdir(parents=True)
    (shim_rig.busy / "token").write_text("t")
    shim_rig.shim("arm", PATH=path)          # this batch is armed already
    ran = tripped(log)
    assert ran == [], f"a stat fast path ran external commands: {ran}"
    assert fake.drain() == [] and shim_rig.python_runs() == 0


def test_the_shim_finds_src_when_run_by_a_bare_name(shim_rig, fake):
    """`sh busy-sound` from hooks/ gives "$0" with no slash. dirname said "."
    there, and the builtin form must too, or python cannot import src/ and the
    first arm is silently lost."""
    shim_rig.settings()
    # No site-packages (-S): the venv's installed sessiontools would import
    # whatever src says, so only src/ itself can make this arm work.
    py = shim_rig.root / "python-no-site"
    py.write_text(f'#!/bin/sh\necho x >> "{shim_rig.pymark}"\nexec "{sys.executable}" -S "$@"\n')
    py.chmod(0o755)
    body = json.dumps({"session_id": SID, "hook_event_name": "PreToolUse"})
    r = subprocess.run(["/bin/sh", "busy-sound", "arm"], cwd=SHIM.parent, input=body,
                       env=shim_rig.env(SESSIONTOOLS_PYTHON=str(py)), capture_output=True,
                       text=True, timeout=30, check=False)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert shim_rig.python_runs() == 1 and shim_rig.record().exists()
    assert events(fake) == [("arm", SID)]
