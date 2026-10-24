"""`session default-model watch` and `unit`: what makes the pin standing.

The pin's one pass is `default_model.enforce`. Something has to run it
again each time settings.json is rewritten, and this module holds the two
ways, neither of which installs anything:

- **`unit launchd|systemd`** PRINTS a launchd agent (`WatchPaths`) or a pair
  of systemd user units (`PathChanged=`) that run `session default-model
  enforce` on each change. The user saves and loads it; nothing here calls
  launchctl or systemctl, writes a unit file or loads one.
- **`watch`** is a foreground stat-poll for a host with neither, or a person
  who wants to see it work: enforce once, then again whenever a watched
  path changes. It holds no lock: two watchers converge on the same pin.

**What is watched** (`watched_paths`) is the same set for both:
settings.json (and its target when it is a symlink), so a rewrite of
`"model"` is put back; and the `session` tool's host settings (its
`session.json`, its `session.d/` drop-ins and the directory that holds
them), so setting, changing or clearing the pin takes effect at once rather
than at settings.json's next change. The directory is watched because a
file that does not exist yet cannot be: it is how creating `session.json`
is seen. Its other files changing costs one pass that finds nothing to do.

**The environment** a unit renders: launchd and systemd give a job neither
the shell's environment nor a PATH lookup, so the unit names the absolute
`session` of this install and carries each variable that moves where
`enforce` reads or writes (`RENDERED_ENV`), ONLY when it is set at render
time, so the job guards the same settings.json, reads the same pin and
keeps its backups where `show` did in the shell that ran `unit`. HOME is the
service manager's own.

Stdlib only, and nothing that loads PyYAML (AGENTS.md rule 3).
"""
from __future__ import annotations

import os
import plistlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sessiontools import default_model as dm

#: The launchd label, pane-state's label shape. The plist's name and Label.
LABEL = "dev.sessionkit.tools.default-model"
#: The systemd user units: the path unit triggers the service of its name.
SYSTEMD_PATH_UNIT = "sessiontools-default-model.path"
SYSTEMD_SERVICE_UNIT = "sessiontools-default-model.service"
#: launchd's minimum gap between two runs; it bounds a burst of events,
#: and our own write's event finds the file in sync.
THROTTLE = 10
#: Seconds between `watch`'s polls, unless `--interval` says otherwise.
INTERVAL = 1.0
KINDS = ("launchd", "systemd")

#: Variables that move what `enforce` reads or writes, rendered into a unit
#: when set: CLAUDE_CONFIG_DIR (the settings.json guarded), SESSION_STATE_DIR
#: (the backups and the log). The `session` tool's settings-file variables,
#: if it ever has any (settings.SETTINGS_FILE_ENV), are added by
#: `rendered_env`, so a new one cannot be missed here.
RENDERED_ENV = ("CLAUDE_CONFIG_DIR", "SESSION_STATE_DIR")


class Refused(Exception):
    """The unit was not printed; the message says why (exit 3)."""


# --- what is watched ------------------------------------------------------------------------------

def _host_settings(environ: Mapping[str, str] | None) -> tuple[Path, Path, Path]:
    """(the `session` tool's host-layer file, its drop-in dir, the folder
    holding both): the settings-file override's when one is set, else
    host_dir's. The same rule `settings.host_files` reads by."""
    from sessiontools import settings
    override = settings.settings_file_override(dm.SETTINGS_TOOL, environ)
    if override is not None:
        folder = Path(os.path.abspath(override)).parent
        return Path(os.path.abspath(override)), folder / f"{dm.SETTINGS_TOOL}.d", folder
    folder = Path(os.path.abspath(settings.host_dir(dm.SETTINGS_TOOL, environ)[0]))
    return folder / f"{dm.SETTINGS_TOOL}.json", folder / f"{dm.SETTINGS_TOOL}.d", folder


def watched_paths(environ: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """settings.json, its symlink target when it has one, then the `session`
    tool's host-layer file, drop-in dir and their folder. In that order,
    each once, absolute."""
    from sessiontools.statusline import settings_target
    path = dm.settings_path()
    out = [path, Path(os.path.abspath(settings_target(path))), *_host_settings(environ)]
    seen: list[Path] = []
    for p in out:
        if p not in seen:
            seen.append(p)
    return tuple(seen)


def _sig(path: Path, follow: bool) -> tuple[int, int, int, int] | None:
    try:
        st = os.stat(path, follow_symlinks=follow)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode)


def snapshot(environ: Mapping[str, str] | None = None) -> tuple:
    """What `watch` compares tick to tick: each watched path's own stat and
    its target's (a symlink retargeted is a change), and for a directory
    every entry's. Anything that cannot be stat'ed is None, so appearing
    and vanishing are changes too."""
    out: list = []
    for p in watched_paths(environ):
        out.append((str(p), _sig(p, False), _sig(p, True)))
        if os.path.isdir(p):
            try:
                names = sorted(os.listdir(p))
            except OSError:
                names = []
            out.extend((str(p / n), _sig(p / n, True)) for n in names)
    return tuple(out)


# --- watch ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Watched:
    code: int
    ticks: int      # polls made
    passes: int     # enforce passes run


#: States whose line is said every time: a write, or a write that failed.
WROTE = ("replaced", "inserted", "created", "failed")


def watch(environ: Mapping[str, str] | None = None, *, interval: float = INTERVAL,
          say: Callable[[str], None] = print, warn: Callable[[str], None] = print,
          sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.time,
          ticks: int | None = None,
          stopped: Callable[[], bool] = lambda: False) -> Watched:
    """Enforce once, then poll every INTERVAL seconds and enforce again each
    time `snapshot` moves. Until STOPPED says so, or TICKS polls (a test's
    budget; None is no budget), or an interrupt the caller catches.

    UNSET at the start: say so and return 0 having polled nothing (no
    watcher is started). A pin that is refused at the start (Fable, off the
    list): return 3, polling nothing. While it runs, the setting is re-read
    on each change, so clearing it stops the writes and a bad value is said
    and written past, never acted on.

    SAY gets each write's line and each change of what the pass found;
    WARN gets refusals and failures. CLOCK and SLEEP go to `enforce` too (the
    backup stamp, the changed-under-us backoff)."""
    found = dm.setting(environ)
    if found.value is None:
        say(f"default-model watch: {found.why}: nothing to watch")
        return Watched(dm.OK, 0, 0)
    why = dm.check_pin(found.value)
    if why:
        warn(f"default-model watch: {why}: nothing watched, nothing written")
        return Watched(dm.REFUSED, 0, 0)
    said = {"message": "", "wrote": False}

    def one_pass() -> None:
        try:
            out = dm.enforce(environ, sleep=sleep, clock=clock)
        except Exception as e:  # noqa: BLE001 - one bad pass never stops the watcher
            warn(f"default-model watch: the pass failed: {e!r}")
            said.update(message="", wrote=False)
            return
        # In sync just after our own write is that write's event coming
        # back: the write's line already said it. Anything else is said
        # when it is a write or a failed one, or when it is news.
        echo = out.state == "in-sync" and said["wrote"]
        if out.state in WROTE or (not echo and out.message != said["message"]):
            (say if out.code == dm.OK else warn)(out.message)
        said.update(message=out.message, wrote=out.state in WROTE and out.code == dm.OK)

    # The snapshot is taken BEFORE each pass: a change landing during the
    # pass is seen on the next tick. Our own write moves it too, and that
    # tick's pass finds the file in sync and writes nothing.
    last = snapshot(environ)
    one_pass()
    passes, n = 1, 0
    while (ticks is None or n < ticks) and not stopped():
        sleep(interval)
        n += 1
        now = snapshot(environ)
        if now != last:
            last = now
            one_pass()
            passes += 1
    return Watched(dm.OK, n, passes)


# --- unit ----------------------------------------------------------------------------------------

def session_bin(environ: Mapping[str, str] | None = None) -> Path | None:
    """The absolute `session` of this install (pane-state's agent's rule:
    beside the interpreter, else the one on PATH): a unit runs no PATH
    lookup."""
    from sessiontools.pane_state_agent import session_bin as find
    return find(environ)


def rendered_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """RENDERED_ENV plus the `session` tool's settings-file variables, each
    one that is set and not empty, as an absolute path (`~` expanded): a
    service manager's job has no shell to expand it and no useful cwd."""
    from sessiontools import settings
    e = os.environ if environ is None else environ
    names = (*RENDERED_ENV, *settings.SETTINGS_FILE_ENV.get(dm.SETTINGS_TOOL, ()))
    home = e.get("HOME") or os.path.expanduser("~")
    out: dict[str, str] = {}
    for name in names:
        raw = e.get(name)
        if raw:
            if raw == "~" or raw.startswith("~/"):
                raw = home + raw[1:]
            out[name] = os.path.abspath(raw)
    return out


def log_path(environ: Mapping[str, str] | None = None) -> Path:
    e = os.environ if environ is None else environ
    home = Path(e.get("HOME") or os.path.expanduser("~"))
    return home / "Library" / "Logs" / "sessiontools" / "default-model.log"


def _preflight(environ: Mapping[str, str] | None) -> tuple[Path, str | None]:
    """(the `session` to run, a note for stderr or None). Refused: no
    `session` found, or a pin set to a value enforce would refuse."""
    found = dm.setting(environ)
    if found.value is not None:
        why = dm.check_pin(found.value)
        if why:
            raise Refused(f"{why}: no unit printed")
    program = session_bin(environ)
    if program is None:
        raise Refused("no `session` console script found beside this interpreter or on "
                      "PATH: no unit printed")
    note = None
    if found.value is None:
        note = (f"default-model unit: {found.why}: the unit will do nothing until "
                "model.default is set")
    return program, note


def launchd(environ: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """(the launchd agent's plist text, a note or None). Raises Refused."""
    program, note = _preflight(environ)
    log = str(log_path(environ))
    data: dict = {
        "Label": LABEL,
        "ProgramArguments": [str(program), "default-model", "enforce"],
        "WatchPaths": [str(p) for p in watched_paths(environ)],
        "RunAtLoad": True,
        "ThrottleInterval": THROTTLE,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    env = rendered_env(environ)
    if env:
        data["EnvironmentVariables"] = env
    return plistlib.dumps(data).decode("utf-8"), note


def _sd(value: str) -> str:
    """A systemd unit-file value: `%` doubled (no specifier expands)."""
    return value.replace("%", "%%")


def _sd_word(value: str) -> str:
    """One word of a systemd command line or assignment: quoted, with `\\`
    and `"` escaped, when it holds anything a bare word cannot."""
    value = _sd(value)
    if value and not any(c in value for c in ' \t\n"\'\\;$'):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemd(environ: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """(both systemd user units, each under a `# file: NAME` line, a note
    or None). Raises Refused."""
    program, note = _preflight(environ)
    path_unit = [f"# file: {SYSTEMD_PATH_UNIT}",
                 "[Unit]",
                 "Description=Put the pinned default model back in Claude Code's settings.json",
                 "",
                 "[Path]",
                 *[f"PathChanged={_sd(str(p))}" for p in watched_paths(environ)],
                 f"Unit={SYSTEMD_SERVICE_UNIT}",
                 "",
                 "[Install]",
                 "WantedBy=default.target"]
    service = [f"# file: {SYSTEMD_SERVICE_UNIT}",
               "[Unit]",
               "Description=One pass of session default-model enforce",
               "",
               "[Service]",
               "Type=oneshot",
               *[f"Environment={_sd_word(f'{k}={v}')}" for k, v in rendered_env(environ).items()],
               f"ExecStart={_sd_word(str(program))} default-model enforce"]
    return "\n".join(path_unit) + "\n\n" + "\n".join(service) + "\n", note


def unit(kind: str, environ: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """KIND's unit text and a note. Prints nothing, writes nothing."""
    if kind == "launchd":
        return launchd(environ)
    if kind == "systemd":
        return systemd(environ)
    raise ValueError(f"unknown unit kind {kind!r}: one of {', '.join(KINDS)}")
