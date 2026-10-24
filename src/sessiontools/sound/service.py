"""`session sound engine build|on|off|status`: the sound engine's ON/OFF.

Everything is per user. Nothing goes in the plugin
dir (it can be a versioned cache) and nothing in a deploy checkout:

    sources   sessiontools/sound/engine.swift, event.swift (package data:
              the wheel ships them, a test pins it)
    binaries  <sound home>/bin/sound-engine and sound-event, built by the
              system `swiftc -O -swift-version 5`; each beside a
              `<name>.source-sha256` naming the source it was built from
    plist     ~/Library/LaunchAgents/<LABEL>.plist
    log       ~/Library/Logs/sessiontools/sound-engine.log

`<sound home>` is `sessiontools.sound.home()`:
`$SESSIONTOOLS_SOUND_HOME`, else `~/.local/share/sessiontools/sound`.

ON and OFF are the plist's presence plus its load. A plist in
LaunchAgents loads again at the next login, so writing it is part of the
deploy, and only `on` writes it. **On a real host, `on` IS the deploy.**

Exit codes: 0 done; 1 tried and failed (bootstrap refused, no pong);
2 refused before anything changed (no swiftc, the build failed, the port is
held, not macOS) or a usage error.

THE RETIRE (the label rename). Before the rename the job was LEGACY_LABEL. `on` and `off`
retire it: a loaded old job is booted out and its plist removed, through
`_launchctl` like everything else. `status` names an old job or plist that
is still there. Nothing to retire is a no-op that says so, and `--dry-run`
lists the retire and changes nothing. The old engine holding the port is
not a refusal: the retire frees it.

THE FENCE: while `SESSIONTOOLS_SOUND_NO_LAUNCHCTL` is set (tests/conftest.py
sets it for every test), `_launchctl` refuses to run launchctl at all. Tests
replace `_launchctl` with a double; one that forgets fails loudly instead of
loading a real agent.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.resources
import json
import os
import plistlib
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from sessiontools import sound

#: The launchd label: the one constant. The plist's name, the launchctl
#: target, the plist's Label and the help text all derive from it.
LABEL = "dev.sessionkit.tools.sound-engine"
#: legacy detection: retire the label from before the rename (on/off remove it, status names it).
LEGACY_LABEL = "com.failmode.sessiontools.sound-engine"
ENGINE = "sound-engine"
#: Each binary and the source it is built from.
SOURCES = {ENGINE: "engine.swift", sound.SENDER: "event.swift"}
#: Tests: launchctl refuses to run while this is set (conftest sets it).
FENCE_ENV = "SESSIONTOOLS_SOUND_NO_LAUNCHCTL"
#: The minimum gap launchd keeps between two spawns. The engine exits 3 when
#: it cannot (re)start the output device within 10 s, and exits 1 when its
#: port is taken. Under KeepAlive, 10 (the first figure chosen) would
#: respawn a host with no output device every ~10 s, for ever, with a log
#: line each time. At 30 a hopeless host costs one short
#: try per 30 s, and a real crash is back within 30 s: the busy sound waits
#: 8 s before it plays at all, so a gap that long goes unheard.
THROTTLE = 30
PING_TIMEOUT = 2.0
RESEND = 0.25
GONE_WAIT = 10.0


class Refused(Exception):
    """Refused before anything changed: exit 2."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _home(env: Mapping[str, str] | None = None) -> Path:
    return Path(_env(env).get("HOME") or str(Path.home()))


def bin_dir(env: Mapping[str, str] | None = None) -> Path:
    return sound.home(env) / "bin"


def plist_path(env: Mapping[str, str] | None = None, label: str | None = None) -> Path:
    return _home(env) / "Library" / "LaunchAgents" / f"{label or LABEL}.plist"


def legacy_plist_path(env: Mapping[str, str] | None = None) -> Path:
    return plist_path(env, LEGACY_LABEL)


def log_path(env: Mapping[str, str] | None = None) -> Path:
    return _home(env) / "Library" / "Logs" / "sessiontools" / "sound-engine.log"


def source(name: str) -> Path:
    """The packaged Swift source for binary NAME (from the wheel or the checkout)."""
    return Path(str(importlib.resources.files("sessiontools.sound") / SOURCES[name]))


def source_hash(name: str) -> str:
    return hashlib.sha256(source(name).read_bytes()).hexdigest()


def _hash_file(dest: Path, name: str) -> Path:
    return dest / f"{name}.source-sha256"


def built_from(dest: Path, name: str) -> str | None:
    """The source hash binary NAME in DEST was built from, or None."""
    try:
        return _hash_file(dest, name).read_text().strip() or None
    except OSError:
        return None


def stale(dest: Path, name: str) -> bool:
    """NAME needs a build: missing, not executable, or built from another
    source. The hash, not the mtime: a wheel install stamps every file with
    the install time, and a downgrade has an older source."""
    b = dest / name
    return not (b.is_file() and os.access(b, os.X_OK)) or built_from(dest, name) != source_hash(name)


def swiftc() -> str | None:
    return shutil.which("swiftc")


def build(dest: Path | None = None, *, force: bool = False,
          env: Mapping[str, str] | None = None) -> dict[str, dict]:
    """Compile each stale binary into DEST (default the sound home's bin).
    All or nothing: every binary is compiled into a staging dir beside DEST
    first, and only when all of them built are they renamed into place (a
    rename, so a running engine or hook never sees half a file). A failed
    build leaves DEST exactly as it was. Raises Refused."""
    if sys.platform != "darwin":
        raise Refused("the sound engine is macOS only (AVAudioEngine)")
    dest = bin_dir(env) if dest is None else dest
    todo = [n for n in SOURCES if force or stale(dest, n)]
    out: dict[str, dict] = {}
    if todo:
        cc = swiftc()
        if not cc:
            raise Refused("no swiftc on PATH: install the Xcode command line tools "
                          "(xcode-select --install)")
        dest.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".build.", dir=dest) as stage:
            for name in todo:
                tmp = os.path.join(stage, name)
                r = subprocess.run([cc, "-O", "-swift-version", "5", "-o", tmp, str(source(name))],
                                   capture_output=True, text=True, check=False)
                if r.returncode != 0 or not os.path.isfile(tmp) or not os.path.getsize(tmp):
                    tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-5:])
                    raise Refused(f"swiftc failed building {name} (exit {r.returncode}); "
                                  f"nothing was changed: {tail}")
                os.chmod(tmp, 0o755)
            for name in todo:
                os.replace(os.path.join(stage, name), dest / name)
                _hash_file(dest, name).write_text(source_hash(name) + "\n")
    for name in SOURCES:
        out[name] = {"path": str(dest / name), "built": name in todo}
    return out


# --- launchd -----------------------------------------------------------------------

def _launchctl(*a: str) -> subprocess.CompletedProcess:
    if os.environ.get(FENCE_ENV):
        raise RuntimeError(f"launchctl is fenced off ({FENCE_ENV} is set: tests); "
                           f"replace service._launchctl with a double")
    return subprocess.run(["launchctl", *a], capture_output=True, text=True, check=False)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target(label: str | None = None) -> str:
    return f"{_domain()}/{label or LABEL}"


def loaded(label: str | None = None) -> bool:
    return _launchctl("print", _target(label)).returncode == 0


def _pid_of(label: str | None = None) -> int | None:
    r = _launchctl("print", _target(label))
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        k, _, v = line.strip().partition(" = ")
        if k == "pid" and v.strip().isdigit():
            return int(v)
    return None


def job_pid() -> int | None:
    """The running job's pid, from `launchctl print`, or None."""
    return _pid_of()


def _wait_gone(wait_s: float | None = None, label: str | None = None) -> bool:
    """bootout returns before launchd has removed the job (sessionvoice
    `ears/services.py` `_bootstrap`): wait until `print` says it is gone."""
    deadline = time.monotonic() + (GONE_WAIT if wait_s is None else wait_s)
    while loaded(label):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def _bootstrap(path: Path) -> subprocess.CompletedProcess:
    """bootstrap, with ONE retry: a bootstrap in the window after a bootout
    fails "5: Input/output error" (measured twice, 2030-12-08)."""
    r = _launchctl("bootstrap", _domain(), str(path))
    if r.returncode != 0:
        time.sleep(1.0)
        r = _launchctl("bootstrap", _domain(), str(path))
    return r


# --- the retire: the label from before the rename --------------------------------------

def legacy() -> dict:
    """The old LEGACY_LABEL job and plist, read only: {label, target, loaded,
    pid, plist, plist_present, stale}. stale: either is still there."""
    path = legacy_plist_path()
    is_loaded = loaded(LEGACY_LABEL)
    present = path.exists()
    return {"label": LEGACY_LABEL, "target": _target(LEGACY_LABEL), "loaded": is_loaded,
            "pid": _pid_of(LEGACY_LABEL) if is_loaded else None, "plist": str(path),
            "plist_present": present, "stale": is_loaded or present}


def _retire_lines(old: dict, dry_run: bool) -> list[str]:
    if not old["stale"]:
        return ["retire nothing to retire: no job or plist under the old label"]
    would = "would " if dry_run else ""
    out = []
    if old["loaded"]:
        out.append(f"retire {would}bootout {old['target']} (the old label)")
    if old["plist_present"]:
        out.append(f"retire {would}remove {old['plist']}")
    return out


def retire(old: dict, wait: bool) -> bool:
    """Boot out the old job (when loaded) and remove its plist (when there).
    WAIT: until launchd has dropped it, as `on` needs (the port). False
    when it is still loaded after GONE_WAIT; the plist is removed either way."""
    gone = True
    if old["loaded"]:
        _launchctl("bootout", old["target"])
        if wait:
            gone = _wait_gone(label=LEGACY_LABEL)
    with contextlib.suppress(FileNotFoundError):
        legacy_plist_path().unlink()
    return gone


def _legacy_note(old: dict) -> list[str]:
    if not old["stale"]:
        return []
    what = " and ".join(w for w, on in (("loaded", old["loaded"]),
                                        (f"plist {old['plist']}", old["plist_present"])) if on)
    return [f"legacy {old['label']} still there ({what}): the next `on` or `off` retires it"]


def plist(env: Mapping[str, str] | None = None) -> dict:
    """The agent's plist, with exactly these keys (ThrottleInterval is
    THROTTLE). No EnvironmentVariables: the
    records carry the mouth dir, and the sessions root defaults."""
    log = str(log_path(env))
    return {
        "Label": LABEL,
        "ProgramArguments": [str(bin_dir(env) / ENGINE), "--port", str(sound.port(env))],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _write_plist(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            plistlib.dump(data, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


# --- the port and the pong -----------------------------------------------------------

def parse_lsof(text: str, port: int) -> list[tuple[str, int]]:
    """The (command, pid) of every process lsof's field output (`-Fpcn`)
    shows BOUND to PORT, in order, once each. `-iUDP:PORT` also matches a
    socket merely connected to PORT (`<local>->127.0.0.1:PORT`): that is a
    client, not a holder, so only the local side of a name counts."""
    out: list[tuple[str, int]] = []
    pid: int | None = None
    cmd = ""
    for line in text.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid, cmd = (int(val) if val.isdigit() else None), ""
        elif tag == "c":
            cmd = val
        elif tag == "n" and pid is not None:
            local = val.split("->", 1)[0]
            if local.endswith(f":{port}") and (cmd, pid) not in out:
                out.append((cmd, pid))
    return out


def port_holders(port: int) -> list[tuple[str, int]] | None:
    """None when 127.0.0.1:PORT/udp is free (a brief bind, released at once);
    else the processes holding it, from lsof (read-only). An empty list means
    it is held, but lsof did not say by whom."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((sound.HOST, port))
        return None
    except OSError:
        pass
    finally:
        s.close()
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        r = subprocess.run([lsof, "-nP", f"-iUDP:{port}", "-Fpcn"], capture_output=True,
                           text=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_lsof(r.stdout, port)


def describe(holders: list[tuple[str, int]]) -> str:
    return "; ".join(f"{c} {p}" for c, p in holders) or "unknown"


def held_by_our_job(holders: list[tuple[str, int]], pid: int | None = None) -> bool:
    """Every holder lsof names is our loaded job's own process (PID, default
    the job's). A loaded job is not enough: its engine may have failed to bind
    because something else holds the port, and launchd respawns it. A holder
    lsof cannot name, or a job with no pid (not running), is not ours."""
    pid = job_pid() if pid is None else pid
    return pid is not None and bool(holders) and {p for _, p in holders} == {pid}


def ping(port: int | None = None, timeout: float = PING_TIMEOUT,
         env: Mapping[str, str] | None = None) -> dict | None:
    """Send `/sound/ping ,d T` and wait up to TIMEOUT for the engine's
    `/sound/pong ,ds T STATUS`. The status as a dict, with `rtt_ms` added,
    or None (no engine, or no answer). The ping is re-sent every RESEND s
    with a fresh T: an engine launchd has only just started may not have
    bound its port when the first one arrives, and UDP drops it."""
    to = sound.port(env) if port is None else port
    head = sound._osc_string("/sound/pong") + sound._osc_string(",ds")
    sent: dict[float, float] = {}                            # T -> when it was sent
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((sound.HOST, 0))
        deadline = time.monotonic() + timeout
        next_send = 0.0
        while (now := time.monotonic()) < deadline:
            if now >= next_send:
                t = time.time()
                while t in sent:
                    t += 1e-6
                sent[t] = now
                s.sendto(sound._osc_string("/sound/ping") + sound._osc_string(",d")
                         + struct.pack(">d", t), (sound.HOST, to))
                next_send = now + RESEND
            s.settimeout(max(0.001, min(next_send, deadline) - time.monotonic()))
            try:
                data, _ = s.recvfrom(65536)
            except TimeoutError:
                continue
            if not data.startswith(head) or len(data) < len(head) + 8:
                continue
            (echo,) = struct.unpack(">d", data[len(head):len(head) + 8])
            if echo not in sent:
                continue                                     # not one of ours
            status = json.loads(data[len(head) + 8:].rstrip(b"\0").decode("utf-8"))
            if isinstance(status, dict):
                status["rtt_ms"] = round((time.monotonic() - sent[echo]) * 1000, 2)
                return status
    except (OSError, ValueError):
        return None
    finally:
        s.close()
    return None


# --- the verbs --------------------------------------------------------------------

def _say(*lines: str) -> None:
    for x in lines:
        print(x)


def _pong_line(p: dict) -> str:
    return (f"pong   {p.get('rtt_ms')} ms: pid {p.get('pid')}, device {p.get('device', '?')!r} "
            f"at {p.get('rate', '?')} Hz, running {p.get('running')}, voices {p.get('voices')}, "
            f"sids {p.get('sids')}, malformed {p.get('malformed')}")


def cmd_build(dest: Path | None, force: bool) -> int:
    for name, b in build(dest, force=force).items():
        _say(f"{name:12} {b['path']}  ({'built' if b['built'] else 'up to date'})")
    return 0


def cmd_on(dry_run: bool) -> int:
    path, port = plist_path(), sound.port()
    dest = bin_dir()
    if sys.platform != "darwin":
        raise Refused("the sound engine is macOS only (AVAudioEngine)")
    todo = [n for n in SOURCES if stale(dest, n)]
    old = legacy()
    holders = port_holders(port)
    ours = holders is not None and held_by_our_job(holders)  # our own engine: replaced below
    olds = (holders is not None and not ours and old["pid"] is not None
            and held_by_our_job(holders, old["pid"]))         # the pre-rename engine: retired
    if holders is not None and not ours and not olds:
        raise Refused(f"127.0.0.1:{port}/udp is held by {describe(holders)}, not by the loaded "
                      f"{LABEL}; nothing was changed")
    data = plist()
    held = ("held by the loaded engine (replaced)" if ours else
            "held by the old-label engine (retired)" if olds else "free")
    if dry_run:
        _say(f"build  {', '.join(todo) + ' (stale)' if todo else 'up to date'} in {dest}",
             f"port   127.0.0.1:{port}/udp {held}",
             *_retire_lines(old, dry_run=True),
             f"plist  would write {path}:",
             plistlib.dumps(data).decode().rstrip(),
             f"launchd would: bootout {_target()}, then bootstrap {_domain()} {path}",
             "dry run: nothing was written, and no launchctl verb that changes anything ran")
        return 0
    if todo and not swiftc():
        raise Refused("no swiftc on PATH: install the Xcode command line tools "
                      "(xcode-select --install); nothing was changed")
    build(dest)
    _say(*_retire_lines(old, dry_run=False))
    if not retire(old, wait=True):
        _say(f"launchd: {old['target']} still loaded {GONE_WAIT:.0f} s after bootout")
        return 1
    log_path().parent.mkdir(parents=True, exist_ok=True)
    _write_plist(path, data)
    _say(f"plist  {path}")
    _launchctl("bootout", _target())
    if not _wait_gone():
        _say(f"launchd: {_target()} still loaded {GONE_WAIT:.0f} s after bootout")
        return 1
    r = _bootstrap(path)
    if r.returncode != 0:
        _say(f"launchd: bootstrap failed ({r.returncode}): {(r.stderr or r.stdout).strip()}")
        return 1
    p = ping(port)
    if p is None:
        _say(f"loaded {LABEL}, but no pong on 127.0.0.1:{port} within {PING_TIMEOUT:.0f} s; "
             f"see {log_path()}")
        return 1
    _say(f"on     {LABEL}", _pong_line(p))
    if not p.get("running"):
        _say(f"note   the output device is not running yet (see {log_path()})")
    return 0


def cmd_off(dry_run: bool, purge: bool) -> int:
    path = plist_path()
    dest = bin_dir()
    bins = [dest / n for n in SOURCES] + [_hash_file(dest, n) for n in SOURCES]
    old = legacy()
    if dry_run:
        _say(f"launchd would: bootout {_target()} ({'loaded' if loaded() else 'not loaded'})",
             f"plist  would remove {path} ({'present' if path.exists() else 'absent'})",
             *_retire_lines(old, dry_run=True),
             *([f"purge  would remove {b}" for b in bins if b.exists()] if purge else []),
             "dry run: nothing was removed, and no launchctl verb that changes anything ran")
        return 0
    was = loaded()
    if was:
        _launchctl("bootout", _target())
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    _say(f"off    {LABEL} ({'booted out' if was else 'was not loaded'}); plist removed: {path}")
    _say(*_retire_lines(old, dry_run=False))
    retire(old, wait=False)
    if purge:
        for b in bins:
            with contextlib.suppress(FileNotFoundError):
                b.unlink()
        _say(f"purge  removed the binaries in {dest}")
    return 0


def status() -> dict:
    dest = bin_dir()
    bins = {}
    for name in SOURCES:
        b = dest / name
        bins[name] = {"path": str(b), "present": b.is_file(), "built_from": built_from(dest, name),
                      "source": source_hash(name), "stale": stale(dest, name)}
    is_loaded = loaded()
    return {"label": LABEL, "port": sound.port(), "binaries": bins,
            "plist": str(plist_path()), "plist_present": plist_path().exists(),
            "loaded": is_loaded, "pid": job_pid() if is_loaded else None,
            "pong": ping(), "log": str(log_path()), "legacy": legacy()}


def cmd_status(as_json: bool) -> int:
    s = status()
    if as_json:
        print(json.dumps(s, indent=1))
        return 0
    for name, b in s["binaries"].items():
        state = "absent" if not b["present"] else "STALE" if b["stale"] else "up to date"
        _say(f"{name:12} {b['path']}  ({state}; built from {(b['built_from'] or '-')[:12]})")
    _say(f"plist  {s['plist']} ({'present' if s['plist_present'] else 'absent'})",
         f"loaded {s['loaded']}" + (f", pid {s['pid']}" if s["pid"] else ""),
         _pong_line(s["pong"]) if s["pong"] else f"pong   none on 127.0.0.1:{s['port']}",
         f"log    {s['log']}",
         *_legacy_note(s["legacy"]))
    return 0


EPILOG = f"""\
`on` IS the deploy on a real host: it writes the plist into
~/Library/LaunchAgents (it loads again at every login) and bootstraps it
into gui/<uid>. `off` boots it out and removes the plist; the binaries stay
unless --purge. Both retire a job left under the old label
(status names one that is still there).
Label {LABEL}; port 127.0.0.1:{sound.PORT}/udp.
Exit 0 done, 1 tried and failed (bootstrap, no pong), 2 refused before
anything changed (no swiftc, build failed, port held) or usage."""


def build_parser() -> argparse.ArgumentParser:
    """`session sound engine`'s parser: build, on, off, status."""
    p = argparse.ArgumentParser(prog="session sound engine", epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Build, switch on, switch off, or report the sound "
                                            "engine (one resident AVAudioEngine process, a "
                                            "per-user launchd agent).")
    sub = p.add_subparsers(dest="verb", required=True, metavar="build|on|off|status")
    b = sub.add_parser("build", help="compile sound-engine and sound-event (no launchd)")
    b.add_argument("--dest", type=Path, metavar="DIR", help="where to put them (default: the "
                   "sound home's bin)")
    b.add_argument("--force", action="store_true", help="rebuild even when up to date")
    on = sub.add_parser("on", help="build if stale, probe the port, write the plist, load it")
    on.add_argument("--dry-run", action="store_true", help="say what it would do; change nothing")
    off = sub.add_parser("off", help="boot the agent out and remove its plist")
    off.add_argument("--dry-run", action="store_true", help="say what it would do; change nothing")
    off.add_argument("--purge", action="store_true", help="also remove the binaries")
    st = sub.add_parser("status", help="binaries, plist, loaded, pid, and the engine's pong")
    st.add_argument("--json", action="store_true", help="machine-readable")
    return p


def main(argv: list[str]) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    try:
        if a.verb == "build":
            return cmd_build(a.dest, a.force)
        if a.verb == "on":
            return cmd_on(a.dry_run)
        if a.verb == "off":
            return cmd_off(a.dry_run, a.purge)
        return cmd_status(a.json)
    except Refused as e:
        print(f"session sound engine {a.verb}: refused: {e}", file=sys.stderr)
        return 2
