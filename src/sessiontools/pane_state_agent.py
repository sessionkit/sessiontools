"""`session pane-state agent on|off|status`: the pane signal's publisher,
`session pane-state watch`, run by a per-user launchd agent, one per host.

It is the sound engine's agent (`sessiontools.sound.service`) with three
differences: what it runs, the environment it gives the job, and how `on`
proves the job is up.

    plist     ~/Library/LaunchAgents/<LABEL>.plist
    log       ~/.sessionkit/tools/log/pane-state.log (settings.log_dir)
    runs      <the install's own `session`> pane-state watch

WHAT IT RUNS. launchd runs no shell and does no PATH lookup, so the plist
names the `session` console script by its absolute path: the one beside
the interpreter running `on` (the install's `bin/`), else the one on PATH.
`sys.executable` is never resolved first: a venv's `python` is a symlink
to the base interpreter, whose directory holds no `session`. On an
editable install the job runs the checkout as it was when the job last
started; `on` again is the restart.

ITS ENVIRONMENT. launchd gives a job PATH `/usr/bin:/bin:/usr/sbin:/sbin`,
and `tmux.TMUX_BIN` is looked up on PATH at import, so the plist's PATH is
the directory of the tmux `on` finds, then those four. The variables that
move where `watch` looks are passed through, each ONLY when it is set at
`on` time, so the job reads the sidecars, lock, turn marks and tmux sockets
the shell that ran `on` reads (PASS_THROUGH). HOME is launchd's own.

ONE PER HOST. `watch` takes an flock and exits 0 when another publisher
holds it, so under KeepAlive a foreign holder would make the job respawn
once per THROTTLE for as long as that holder lives. `on` therefore refuses
while the lock is held by anything but the loaded job, naming the holder.
"Held" is measured, never read: the lock file keeps its holder's line
(`pid N on HOST since T`) after the holder exits. When that line names the
loaded job's live pid, the job holds it (only a holder writes it). Any
other case is a non-blocking flock probe on the file, opened without
O_CREAT and closed at once. Nothing here creates the lock or its directory.

PROOF OF ON. After the bootstrap, `on` waits up to PROOF_WAIT for the lock
line to name the job's pid. It never probes while it waits: a probe could
take the lock in the instant the starting job reaches for it.

Exit codes, as the sound engine's: 0 done; 1 tried and failed (bootstrap
refused, the job never took the lock); 2 refused before anything changed
(not macOS, no `session`, no tmux, the lock held by another process) or a
usage error.

THE RETIRE (the label rename). Before the rename the job was LEGACY_LABEL. `on` and `off`
retire it: a loaded old job is booted out and its plist removed, through
`_launchctl` like everything else. `status` names an old job or plist that
is still there. Nothing to retire is a no-op that says so, and `--dry-run`
lists the retire and changes nothing. The old job holding the watch lock is
not a refusal: the retire frees it.

THE FENCE: while FENCE_ENV is set (tests/conftest.py sets it for every
test), `_launchctl` refuses to run launchctl at all. Tests replace
`_launchctl` with a double; one that forgets fails loudly instead of
loading a real agent.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from sessiontools import pane_state, settings

#: The launchd label: the one constant. The plist's name, the launchctl
#: target, the plist's Label and the help text all derive from it, so a
#: rename is this one line.
LABEL = "dev.sessionkit.tools.pane-state"
#: legacy detection: retire the label from before the rename (on/off remove it, status names it).
LEGACY_LABEL = "com.failmode.sessiontools.pane-state"
#: Tests: launchctl refuses to run while this is set (conftest sets it).
FENCE_ENV = "SESSIONTOOLS_PANE_STATE_NO_LAUNCHCTL"
#: The minimum gap launchd keeps between two spawns: a crashed publisher is
#: back within it, and a job that finds the lock taken costs one short run
#: per THROTTLE seconds rather than a tight loop.
THROTTLE = 30
#: How long `on` waits for the job to hold the lock.
PROOF_WAIT = 10.0
#: How long `on` waits for launchd to drop the old job after a bootout.
GONE_WAIT = 10.0
POLL = 0.2
#: The pause before the one bootstrap retry.
RETRY_PAUSE = 1.0
#: launchd's own PATH for a job; the plist puts tmux's directory first.
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
#: The variables that move where `watch` looks, and who reads each:
#: TMUX_TMPDIR (tmux.sockets), SESSION_STATE_DIR (snapshot.state_dir, so
#: the lock), SESSION_SIDECAR_DIR (claude_code.sidecar_dir), SESSIONS_DIR
#: (settings.sessions_root, through turn.mark_path).
PASS_THROUGH = ("TMUX_TMPDIR", "SESSION_STATE_DIR", "SESSION_SIDECAR_DIR", "SESSIONS_DIR")
PROG = "session pane-state agent"


class Refused(Exception):
    """Refused before anything changed: exit 2."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _macos() -> bool:
    return sys.platform == "darwin"


def _home(env: Mapping[str, str] | None = None) -> Path:
    return Path(_env(env).get("HOME") or str(Path.home()))


def plist_path(env: Mapping[str, str] | None = None, label: str | None = None) -> Path:
    return _home(env) / "Library" / "LaunchAgents" / f"{label or LABEL}.plist"


def legacy_plist_path(env: Mapping[str, str] | None = None) -> Path:
    return plist_path(env, LEGACY_LABEL)


#: The job's log file, in the tools package's log directory.
LOG_NAME = "pane-state.log"


def log_path(env: Mapping[str, str] | None = None) -> Path:
    return settings.log_dir(settings.PACKAGES["session"], env) / LOG_NAME


# --- what the job runs, and with what -----------------------------------------------

def session_bin(env: Mapping[str, str] | None = None) -> Path | None:
    """The absolute `session` console script of this install: beside the
    interpreter (never resolved first), else the one on PATH, resolved."""
    beside = Path(sys.executable).parent / "session"
    if beside.is_file() and os.access(beside, os.X_OK):
        return Path(os.path.abspath(beside))
    found = shutil.which("session", path=_env(env).get("PATH"))
    return Path(found).resolve() if found else None


def tmux_bin(env: Mapping[str, str] | None = None) -> Path | None:
    """The tmux on PATH, as an absolute path; None when there is none."""
    found = shutil.which("tmux", path=_env(env).get("PATH"))
    return Path(os.path.abspath(found)) if found else None


def environment(tmux: Path, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The plist's EnvironmentVariables: PATH with tmux's directory first,
    then each PASS_THROUGH variable that is set (and not empty)."""
    e = _env(env)
    dirs = [str(tmux.parent), *[d for d in SYSTEM_PATH if d != str(tmux.parent)]]
    out = {"PATH": ":".join(dirs)}
    for name in PASS_THROUGH:
        if e.get(name):
            out[name] = e[name]
    return out


def plist(program: Path, tmux: Path, env: Mapping[str, str] | None = None) -> dict:
    log = str(log_path(env))
    return {
        "Label": LABEL,
        "ProgramArguments": [str(program), "pane-state", "watch"],
        "EnvironmentVariables": environment(tmux, env),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE,
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


# --- launchd ------------------------------------------------------------------------

def _launchctl(*a: str) -> subprocess.CompletedProcess:
    if os.environ.get(FENCE_ENV):
        raise RuntimeError(f"launchctl is fenced off ({FENCE_ENV} is set: tests); "
                           f"replace pane_state_agent._launchctl with a double")
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
    """bootout returns before launchd has removed the job: wait until
    `print` says it is gone."""
    deadline = time.monotonic() + (GONE_WAIT if wait_s is None else wait_s)
    while loaded(label):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL)
    return True


def _bootstrap(path: Path) -> subprocess.CompletedProcess:
    """bootstrap, with ONE retry: a bootstrap in the window just after a
    bootout can fail "5: Input/output error"."""
    r = _launchctl("bootstrap", _domain(), str(path))
    if r.returncode != 0:
        time.sleep(RETRY_PAUSE)
        r = _launchctl("bootstrap", _domain(), str(path))
    return r


# --- the retire: the label from before the rename ------------------------------------

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
    WAIT: until launchd has dropped it, as `on` needs (the lock). False
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


# --- the lock -----------------------------------------------------------------------

def _line(path: Path) -> str | None:
    """The lock file's holder line, or None when there is no file."""
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def line_pid(line: str | None) -> int | None:
    """N from `pid N on HOST since T`, the line `pane_state.acquire` writes."""
    words = (line or "").split()
    if len(words) >= 2 and words[0] == "pid" and words[1].isdigit():
        return int(words[1])
    return None


def probe(path: Path) -> bool:
    """True while some process holds PATH's flock. Opens without O_CREAT
    (no file: nobody holds it) and lets go at once."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return True
        raise
    finally:
        os.close(fd)
    return False


def lock(job: int | None, env: Mapping[str, str] | None = None) -> dict:
    """Who holds the watch lock: {path, held, holder, holder_pid, ours}.
    A line naming JOB (the loaded job's live pid) is the job's hold, with no
    probe; anything else is probed."""
    path = pane_state.lock_path(env)
    line = _line(path)
    pid = line_pid(line)
    if job is not None and pid == job:
        held, ours = True, True
    else:
        held, ours = probe(path), False
    return {"path": str(path), "held": held, "holder": line if held else None,
            "holder_pid": pid if held else None, "ours": ours}


def _wait_held_by_job() -> int | None:
    """The job's pid once the lock line names it, within PROOF_WAIT; None
    if it never does. Reads only: a probe here could take the lock in the
    instant the starting job reaches for it."""
    path = pane_state.lock_path()
    deadline = time.monotonic() + PROOF_WAIT
    while True:
        job = job_pid()
        if job is not None and line_pid(_line(path)) == job:
            return job
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL)


# --- the verbs ----------------------------------------------------------------------

def _say(*lines: str) -> None:
    for x in lines:
        print(x)


def _lock_line(lk: dict) -> str:
    if not lk["held"]:
        return f"lock   {lk['path']}: not held"
    who = "the loaded job" if lk["ours"] else "NOT the loaded job"
    return f"lock   {lk['path']}: held by {who}: {lk['holder'] or 'unknown'}"


def cmd_on(dry_run: bool) -> int:
    if not _macos():
        raise Refused("the pane-state agent is a launchd agent: macOS only")
    program = session_bin()
    if program is None:
        raise Refused("no `session` console script beside this interpreter or on PATH; "
                      "nothing was changed")
    tmux = tmux_bin()
    if tmux is None:
        raise Refused("no tmux on PATH: the job could not publish; nothing was changed")
    old = legacy()
    lk = lock(job_pid())
    olds = (lk["held"] and not lk["ours"] and old["pid"] is not None
            and lk["holder_pid"] == old["pid"])              # the old-label job: retired
    if lk["held"] and not lk["ours"] and not olds:
        raise Refused(f"the watch lock {lk['path']} is held by {lk['holder'] or 'an unknown process'}, "
                      f"not by the loaded {LABEL}; stop that publisher first. "
                      f"Nothing was changed")
    path = plist_path()
    data = plist(program, tmux)
    if dry_run:
        _say(_lock_line(lk) + (" (replaced)" if lk["ours"] else
                               " (the old-label job: retired)" if olds else ""),
             *_retire_lines(old, dry_run=True),
             f"plist  would write {path}:",
             plistlib.dumps(data).decode().rstrip(),
             f"launchd would: bootout {_target()}, then bootstrap {_domain()} {path}",
             "dry run: nothing was written, and no launchctl verb that changes anything ran")
        return 0
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
    pid = _wait_held_by_job()
    if pid is None:
        _say(f"loaded {LABEL}, but the job did not take the watch lock "
             f"{pane_state.lock_path()} within {PROOF_WAIT:.0f} s; see {log_path()}")
        return 1
    _say(f"on     {LABEL}: pid {pid} holds {pane_state.lock_path()}")
    return 0


def cmd_off(dry_run: bool) -> int:
    path = plist_path()
    old = legacy()
    if dry_run:
        _say(f"launchd would: bootout {_target()} ({'loaded' if loaded() else 'not loaded'})",
             f"plist  would remove {path} ({'present' if path.exists() else 'absent'})",
             *_retire_lines(old, dry_run=True),
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
    return 0


def status() -> dict:
    path = plist_path()
    is_loaded = loaded()
    pid = job_pid() if is_loaded else None
    program = None
    with contextlib.suppress(OSError, plistlib.InvalidFileException, ValueError):
        program = plistlib.loads(path.read_bytes()).get("ProgramArguments")
    return {"label": LABEL, "plist": str(path), "plist_present": path.exists(),
            "program": program, "loaded": is_loaded, "pid": pid,
            "lock": lock(pid), "log": str(log_path()), "legacy": legacy()}


def cmd_status(as_json: bool) -> int:
    s = status()
    if as_json:
        print(json.dumps(s, indent=1))
        return 0
    lk = s["lock"]
    _say(f"plist  {s['plist']} ({'present' if s['plist_present'] else 'absent'})",
         *([f"runs   {' '.join(s['program'])}"] if s["program"] else []),
         f"loaded {s['loaded']}" + (f", pid {s['pid']}" if s["pid"] else ""),
         _lock_line(lk),
         f"log    {s['log']}",
         *settings.legacy_log_pointer(LOG_NAME),  # drop after 0.3.3 (the old-log pointer)
         *_legacy_note(s["legacy"]))
    if s["loaded"] and lk["held"] and not lk["ours"]:
        _say("note   another publisher holds the lock: the job exits at each start and "
             f"launchd retries every {THROTTLE} s until that publisher stops")
    return 0


EPILOG = f"""\
`on` IS the deploy on a real host: it writes the plist into
~/Library/LaunchAgents (it loads again at every login) and bootstraps it
into gui/<uid>. The job runs `session pane-state watch`, one per host, and
`on` exits 0 only once the job holds the watch lock. `off` boots it out
(the publisher unsets every option it wrote) and removes the plist.
Both retire a job left under the old label (status names one that is
still there). Label {LABEL}.
Exit 0 done, 1 tried and failed (bootstrap, the job never took the lock),
2 refused before anything changed (not macOS, no session or tmux, the lock
held by another publisher) or usage."""


def build_parser() -> argparse.ArgumentParser:
    """`session pane-state agent`'s parser: on, off, status."""
    p = argparse.ArgumentParser(prog=PROG, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Switch on, switch off, or report the per-user "
                                            "launchd agent that runs the pane signal's "
                                            "publisher, `session pane-state watch`.")
    sub = p.add_subparsers(dest="verb", required=True, metavar="on|off|status")
    on = sub.add_parser("on", help="write the plist, load it, wait for the job to hold the lock")
    on.add_argument("--dry-run", action="store_true", help="say what it would do; change nothing")
    off = sub.add_parser("off", help="boot the agent out and remove its plist")
    off.add_argument("--dry-run", action="store_true", help="say what it would do; change nothing")
    st = sub.add_parser("status", help="plist, loaded, pid, and who holds the watch lock")
    st.add_argument("--json", action="store_true", help="machine-readable")
    return p


def main(argv: list[str]) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    try:
        if a.verb == "on":
            return cmd_on(a.dry_run)
        if a.verb == "off":
            return cmd_off(a.dry_run)
        return cmd_status(a.json)
    except Refused as e:
        print(f"{PROG} {a.verb}: refused: {e}", file=sys.stderr)
        return 2
