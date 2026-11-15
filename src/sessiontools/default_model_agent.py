"""`session default-model agent on|off|status`: the pin's enforcer, run by a
per-user launchd agent on every change of what it watches.

It is the pane-state agent (`sessiontools.pane_state_agent`) for a job of
another kind: on demand, not resident. The job runs `session default-model
enforce` once at load and again on each change of settings.json or the
`session` tool's host settings (`WatchPaths`), and exits between runs.

    plist     ~/Library/LaunchAgents/<LABEL>.plist
    log       ~/.sessionkit/tools/log/default-model.log (default_model_watch.log_path)
    runs      <the install's own `session`> default-model enforce

ONE RENDERER. The plist `on` writes is the exact text `session default-model
unit launchd` prints: `default_model_watch.launchd()`, called here and never
re-built, so the hand install and `on` load the same job. The plist path is
the one the hand install uses, so `on` replaces a job loaded by hand, and
`off` removes it.

OPT-IN. Nothing calls `on` but a user: no setup, install or migrate step
runs it. An unset pin is not a refusal: the job watches the host settings
too, so setting the pin later takes effect at once. `on` says so, with the
note `unit` gives.

PROOF OF ON is `launchctl print` exiting 0: the job is loaded. It never
waits for a pid; the job runs `enforce` and exits, so no pid is its healthy
state (the service file's `kind: on-demand`).

THE SERVICE FILE. `on` writes ~/.sessionkit/tools/services/default-model.json
(enabled true) right after the plist, and `off` rewrites it (enabled false)
after the plist is removed; the file is kept. `service_file` is the writer.
A failed write does not stop the launchd act: the verb finishes it, prints
`service could not write ...`, and exits 4. A refusal writes no file.

Exit codes are the `session default-model` group's own (default_model.OK,
REFUSED, FAILED): 0 done; 2 usage; 3 refused before anything changed (not
macOS, no `session`, a pin enforce would refuse); 4 tried and failed (the
plist or the service file not written, a bootstrap refused, the job not
loaded after it).

THE FENCE: while FENCE_ENV is set (tests/conftest.py sets it for every
test), `_launchctl` refuses to run launchctl at all. Tests replace
`_launchctl` with a double; one that forgets fails loudly instead of
loading a real agent. This module keeps its own copy of the launchd
helpers, as the sound engine's does, so a test that doubles one module's
launchctl cannot reach another's.

Stdlib only, and nothing that loads PyYAML (AGENTS.md rule 3).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import plistlib
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from sessiontools import default_model as dm
from sessiontools import default_model_watch as dmw
from sessiontools import service_file

#: The launchd label, imported from the renderer: never re-spelled here.
LABEL = dmw.LABEL
#: Tests: launchctl refuses to run while this is set (conftest sets it).
FENCE_ENV = "SESSIONTOOLS_DEFAULT_MODEL_NO_LAUNCHCTL"
#: How long `on` waits for launchd to drop the old job after a bootout.
GONE_WAIT = 10.0
POLL = 0.2
#: The pause before the one bootstrap retry.
RETRY_PAUSE = 1.0
PROG = "session default-model agent"
#: The service file's name (the label's tail), its description and kind.
SERVICE = service_file.name_of(LABEL)
DESCRIPTION = "puts the pinned default model back in Claude Code's settings.json"
#: A WatchPaths job with RunAtLoad and no KeepAlive: no pid is healthy.
KIND = "on-demand"
#: What `dmw.launchd()`'s refusal ends with; `on` says what it did instead.
_UNIT_REFUSED = ": no unit printed"


class Refused(Exception):
    """Refused before anything changed: exit 3 (default_model.REFUSED)."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _macos() -> bool:
    return sys.platform == "darwin"


def _home(env: Mapping[str, str] | None = None) -> Path:
    return Path(_env(env).get("HOME") or str(Path.home()))


def plist_path(env: Mapping[str, str] | None = None) -> Path:
    """~/Library/LaunchAgents/<LABEL>.plist: where the help's hand install
    puts it too."""
    return _home(env) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_path(env: Mapping[str, str] | None = None) -> Path:
    """The job's log: the path the plist names (default_model_watch's)."""
    return dmw.log_path(env)


def service_record(enabled: bool) -> dict:
    """default-model.json, as `on` (ENABLED) and `off` write it (the
    services design 1.2)."""
    return service_file.record(label=LABEL, enabled=enabled, install=f"{PROG} on",
                               uninstall=f"{PROG} off", description=DESCRIPTION, kind=KIND)


# --- launchd ------------------------------------------------------------------------

def _launchctl(*a: str) -> subprocess.CompletedProcess:
    if os.environ.get(FENCE_ENV):
        raise RuntimeError(f"launchctl is fenced off ({FENCE_ENV} is set: tests); "
                           f"replace default_model_agent._launchctl with a double")
    return subprocess.run(["launchctl", *a], capture_output=True, text=True, check=False)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target() -> str:
    return f"{_domain()}/{LABEL}"


def _print() -> dict | None:
    """`launchctl print`'s two fields we read, {pid, last_exit}, each an int
    or None; None when the job is not loaded. Only the `key = N` form is
    read: the rest of `print`'s output is not API."""
    r = _launchctl("print", _target())
    if r.returncode != 0:
        return None
    out: dict = {"pid": None, "last_exit": None}
    for line in r.stdout.splitlines():
        k, _, v = line.strip().partition(" = ")
        v = v.strip()
        if k == "pid" and v.isdigit():
            out["pid"] = int(v)
        elif k == "last exit code" and v.lstrip("-").isdigit():
            out["last_exit"] = int(v)
    return out


def loaded() -> bool:
    return _launchctl("print", _target()).returncode == 0


def _wait_gone(wait_s: float | None = None) -> bool:
    """bootout returns before launchd has removed the job: wait until
    `print` says it is gone."""
    deadline = time.monotonic() + (GONE_WAIT if wait_s is None else wait_s)
    while loaded():
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


# --- the verbs ----------------------------------------------------------------------

def _say(*lines: str) -> None:
    for x in lines:
        print(x)


def _unit() -> tuple[str, str | None]:
    """The plist text `unit launchd` prints, and its note. Refused (exit 3)
    off macOS, and for the renderer's own refusals."""
    if not _macos():
        raise Refused("the default-model agent is a launchd agent: macOS only; on Linux, "
                      "`session default-model unit systemd` prints the units")
    try:
        return dmw.launchd()
    except dmw.Refused as e:
        raise Refused(f"{str(e).removesuffix(_UNIT_REFUSED)}; nothing was changed") from None


def _watched(text: str) -> int:
    return len(plistlib.loads(text.encode()).get("WatchPaths", []))


def cmd_on(dry_run: bool) -> int:
    text, note = _unit()
    path = plist_path()
    if dry_run:
        _say(f"plist  would write {path}:",
             text.rstrip(),
             *service_file.declare(service_record(True), dry_run=True)[0],
             f"launchd would: bootout {_target()}, then bootstrap {_domain()} {path}",
             "dry run: nothing was written, and no launchctl verb that changes anything ran")
        if note:
            print(note, file=sys.stderr)
        return dm.OK
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)     # launchd makes no log folder
        path.parent.mkdir(parents=True, exist_ok=True)
        service_file.replace_bytes(path, text.encode())
    except OSError as e:
        _say(f"plist  could not write {path}: {e.strerror or e}; nothing was loaded")
        return dm.FAILED
    _say(f"plist  {path}")
    lines, declared = service_file.declare(service_record(True), dry_run=False)
    _say(*lines)                                 # a failed write: the act goes on, exit 4
    _launchctl("bootout", _target())
    if not _wait_gone():
        _say(f"launchd: {_target()} still loaded {GONE_WAIT:.0f} s after bootout")
        return dm.FAILED
    r = _bootstrap(path)
    if r.returncode != 0:
        _say(f"launchd: bootstrap failed ({r.returncode}): {(r.stderr or r.stdout).strip()}")
        return dm.FAILED
    if not loaded():
        _say(f"launchd: bootstrap exited 0, but {_target()} is not loaded; see {log_path()}")
        return dm.FAILED
    _say(f"on     {LABEL}: loaded (on demand: runs enforce on each change of its "
         f"{_watched(text)} watched paths)")
    if note:
        print(note, file=sys.stderr)
    return dm.OK if declared else dm.FAILED


def cmd_off(dry_run: bool) -> int:
    if not _macos():
        raise Refused("the default-model agent is a launchd agent: macOS only")
    path = plist_path()
    if dry_run:
        _say(f"launchd would: bootout {_target()} ({'loaded' if loaded() else 'not loaded'})",
             f"plist  would remove {path} ({'present' if path.exists() else 'absent'})",
             *service_file.declare(service_record(False), dry_run=True)[0],
             "dry run: nothing was removed or written, and no launchctl verb that changes "
             "anything ran")
        return dm.OK
    was = loaded()
    if was:
        _launchctl("bootout", _target())
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    _say(f"off    {LABEL} ({'booted out' if was else 'was not loaded'}); plist removed: {path}")
    lines, declared = service_file.declare(service_record(False), dry_run=False)
    _say(*lines)
    return dm.OK if declared else dm.FAILED


def _service() -> dict:
    """The service file, read only: {path, present, enabled}. enabled is
    true or false from the file, else None (absent, or not a JSON object)."""
    target = service_file.path(SERVICE)
    rec = service_file.read(SERVICE)
    enabled = rec.get("enabled") if rec is not None else None
    return {"path": str(target), "present": target.exists(),
            "enabled": enabled if isinstance(enabled, bool) else None}


def status() -> dict:
    path = plist_path()
    job = _print()
    program = None
    with contextlib.suppress(OSError, plistlib.InvalidFileException, ValueError):
        program = plistlib.loads(path.read_bytes()).get("ProgramArguments")
    return {"label": LABEL, "plist": str(path), "plist_present": path.exists(),
            "program": program, "loaded": job is not None,
            "pid": job["pid"] if job else None,
            "last_exit": job["last_exit"] if job else None,
            "log": str(log_path()), "service": _service()}


def _service_line(sv: dict) -> str:
    if not sv["present"]:
        state = "absent"
    elif sv["enabled"] is None:
        state = "not JSON"
    else:
        state = f"enabled: {json.dumps(sv['enabled'])}"
    return f"service {sv['path']} ({state})"


def cmd_status(as_json: bool) -> int:
    if not _macos():
        raise Refused("the default-model agent is a launchd agent: macOS only")
    s = status()
    if as_json:
        print(json.dumps(s, indent=1))
        return dm.OK
    _say(f"plist  {s['plist']} ({'present' if s['plist_present'] else 'absent'})",
         *([f"runs   {' '.join(s['program'])}"] if s["program"] else []),
         f"loaded {s['loaded']}" + (f", pid {s['pid']}" if s["pid"] else ""),
         *([f"last exit {s['last_exit']}"] if s["last_exit"] is not None else []),
         f"log    {s['log']}",
         _service_line(s["service"]))
    return dm.OK


EPILOG = f"""\
`on` IS the deploy on a Mac: it writes the plist into ~/Library/LaunchAgents
(it loads again at every login) and bootstraps it into gui/<uid>. The
plist is the exact text `session default-model unit launchd` prints, and
the job runs `session default-model enforce` on each change of
settings.json or the pin. `on` exits 0 once launchd has it loaded: it is
an on-demand job, so no pid is healthy. `off` boots it out and removes the
plist, a hand-installed one too. Opt-in: nothing runs `on` for you, and
with the pin unset the job does nothing until model.default is set.
Label {LABEL}.
Both write {service_file.shown(SERVICE)} (enabled true
on `on`, false on `off`; the file is kept), the file `sessionkit service
list` reads; --dry-run prints it and writes nothing. If that write fails,
the job is still loaded (or booted out) as asked, and the verb prints
  {service_file.COULD_NOT_WRITE.format(path="PATH", reason="REASON")}
then exits 4: run it again.
Exit codes: 0 done; 2 usage; 3 refused, nothing changed (not macOS, no
session found, a pin enforce would refuse); 4 a plist or service-file
write or a launchctl load failed."""


def build_parser() -> argparse.ArgumentParser:
    """`session default-model agent`'s parser: on, off, status."""
    p = argparse.ArgumentParser(prog=PROG, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Switch on, switch off, or report the per-user "
                                            "launchd agent that runs `session default-model "
                                            "enforce` on every change. Opt-in.")
    sub = p.add_subparsers(dest="verb", required=True, metavar="on|off|status")
    on = sub.add_parser("on", help="write the plist `unit launchd` prints, and load it")
    on.add_argument("--dry-run", action="store_true", help="say what it would do; change nothing")
    off = sub.add_parser("off", help="boot the agent out and remove its plist")
    off.add_argument("--dry-run", action="store_true", help="say what it would do; change nothing")
    st = sub.add_parser("status", help="plist, loaded, pid, last exit, log and service file")
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
        return dm.REFUSED
