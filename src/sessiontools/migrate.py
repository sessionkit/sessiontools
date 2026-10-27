"""`session path migrate [--dry-run] [--json]`: move tools' state and sound
folders to their new home.

    state   old ~/.session/state/                   -> ~/.sessionkit/tools/state/
    sound   old ~/.local/share/sessiontools/sound/  -> ~/.sessionkit/tools/state/sound/

Those two leaves ONLY. sessions, seats, agent and backups move in wave 4, and
nothing outside the two leaves is touched (never ~/.sessionmail, the phone,
sessions, seats, agent or backups). The old and new paths are the resolvers'
own (`settings.leaf_old`, `settings.leaf_new`), so there is no second rule.
The verb sits under `session path` because it moves exactly what `session
path` reports: after it runs, `session path --json` says `new` for both.

THE MOVE, per leaf, state first (sound's new home is inside state's):

- old absent: nothing to move. old a link that resolves to new: done (a
  second run is a no-op and says so).
- old a real folder, new absent: ONE `rename(old, new)`.
- old a real folder, new a real folder (state only: a fresh sound install
  makes `tools/state/`): every child of old is checked against new first,
  and ANY same-name collision refuses the WHOLE run, naming them; nothing
  moves and nothing merges. Else each child is renamed in, and the emptied
  old folder removed. For sound, an existing new folder is a collision.
- old itself a link (to somewhere else), new absent: the LINK is moved, and
  the receipt says so. With new present, or dangling: refused.
- After a move, the old path becomes a symlink to the new one.

Only `rename` moves anything: a symlink inside a leaf moves AS a link, with
the same target (a dangling one included); nothing is copied, followed or
merged (no copytree, no shutil.move). A cross-device move (EXDEV) is refused,
checked before anything moves and again by rename itself; there is no copy
fallback. A child that fails mid-merge puts back the ones already moved.

REFUSED, with nothing moved (exit 3): an override set (SESSION_STATE_DIR or
SESSIONTOOLS_SOUND_HOME: the operator owns that path); a collision; EXDEV;
an unexpected file type; a link whose RELATIVE target would resolve
somewhere else once moved (the old leaf itself, or a link inside it that
leads out of it: `../agent` from state); and, for a live run, any tools agent that writes
these leaves loaded or not measurable: the pane-state agent (and a
foreground `pane-state watch`, by its lock), the default-model watch unit,
and the sound engine. A foreground `default-model watch` holds no lock, so
it cannot be seen: stop it first.

`--dry-run` prints the plan and the receipt the live run would print, and
changes nothing (no folder made, no lock file created). The receipt names
what moved (or would), the size and file count of each entry (links not
followed), each link moved as a link, the links left behind, any key-like
name found (STOP and report it, build-cut.md §4), and each agent's state.

Exit: 0 moved or nothing to do; 3 refused, nothing moved; 1 a move failed
part-way (the receipt says what moved); 2 usage.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sessiontools import settings

PROG = "session path migrate"
#: The leaves this verb moves, in the order it moves them.
LEAVES = ("state", "sound")
#: Exit codes.
OK, FAILED, USAGE, REFUSED = 0, 1, 2, 3
#: Tests: launchctl and systemctl refuse to run while this is set (conftest).
FENCE_ENV = "SESSIONTOOLS_MIGRATE_NO_PROBE"
#: A name that looks like a key or a secret: reported, never read.
KEY_LIKE = re.compile(r"(?i)(\.(pem|key|p12|pfx|gpg|asc)$|^id_(rsa|dsa|ecdsa|ed25519)|"
                      r"secret|credential|private.?key)")

# What each leaf's plan says it does.
ABSENT, DONE, RENAME, MERGE, MOVE_LINK, REFUSE = (
    "absent", "done", "rename", "merge", "move-link", "refuse")
# An agent's state.
LOADED, NOT_LOADED, UNKNOWN = "loaded", "not loaded", "unknown"


@dataclass
class Entry:
    """One thing moved: the whole leaf (`name` ".") or one child of it."""
    name: str
    kind: str                 # dir, file, link or other
    size: int = 0             # bytes, by lstat: links are not followed
    files: int = 0            # non-directory entries, links included
    target: str | None = None  # a link's target, as written


@dataclass
class Plan:
    leaf: str
    old: str
    new: str
    action: str
    why: str = ""
    entries: list[Entry] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)    # links inside, moved as links
    key_like: list[str] = field(default_factory=list)
    moved: bool = False
    link_left: bool = False


@dataclass
class Agent:
    name: str
    unit: str
    state: str
    why: str = ""


# --- measuring (read-only) ----------------------------------------------------------------

def _kind(p: Path) -> str:
    if os.path.islink(p):
        return "link"
    if p.is_dir():
        return "dir"
    return "file" if p.is_file() else "other"


def _measure(top: Path, name: str, rel_root: Path) -> tuple[Entry, list[dict], list[str]]:
    """TOP's entry (size, files), the links inside it and its key-like names,
    walking with lstat only: a link is counted and listed, never followed."""
    kind = _kind(top)
    links: list[dict] = []
    keys: list[str] = []
    rel = os.path.relpath(top, rel_root)
    if KEY_LIKE.search(top.name):
        keys.append(rel)
    if kind == "link":
        target = os.readlink(top)
        links.append({"path": rel, "target": target})
        return Entry(name, kind, os.lstat(top).st_size, 1, target), links, keys
    if kind != "dir":
        return Entry(name, kind, os.lstat(top).st_size, 1), links, keys
    size, files = 0, 0
    for root, dirs, names in os.walk(top, followlinks=False):
        for d in list(dirs):
            p = Path(root, d)
            if KEY_LIKE.search(d):
                keys.append(os.path.relpath(p, rel_root))
            if os.path.islink(p):      # os.walk lists a dir link under dirs: a link
                dirs.remove(d)
                links.append({"path": os.path.relpath(p, rel_root), "target": os.readlink(p)})
                size, files = size + os.lstat(p).st_size, files + 1
        for n in names:
            p = Path(root, n)
            st = os.lstat(p)
            size, files = size + st.st_size, files + 1
            if KEY_LIKE.search(n):
                keys.append(os.path.relpath(p, rel_root))
            if os.path.islink(p):
                links.append({"path": os.path.relpath(p, rel_root), "target": os.readlink(p)})
    return Entry(name, kind, size, files), links, keys


def _dev(path: Path) -> int:
    """The device PATH (or its nearest existing ancestor) is on, links
    followed: where a rename into it would land."""
    p = path
    while not os.path.lexists(p) and p != p.parent:
        p = p.parent
    return os.stat(p).st_dev


def _cross_device(old: Path, new: Path) -> bool:
    try:
        return os.lstat(old).st_dev != _dev(new)
    except OSError:
        return False


def _escapes(root: Path, links: list[dict]) -> list[str]:
    """Each link under ROOT whose target is RELATIVE and leads out of ROOT.
    A link moves with its target as written, so once ROOT moves such a link
    resolves somewhere else (often to nothing). A relative target that stays
    inside ROOT moves with it and still resolves; an absolute one is fixed."""
    top = os.path.normpath(root)
    out = []
    for link in links:
        t = link["target"]
        if os.path.isabs(t):
            continue
        at = os.path.normpath(os.path.join(top, os.path.dirname(link["path"]), t))
        if os.path.commonpath([at, top]) != top:
            out.append(f"{link['path']} -> {t}")
    return out


def _points_at(old: Path, new: Path) -> bool:
    """OLD is a symlink that resolves to NEW (the state after a move)."""
    return os.path.islink(old) and settings._same_dir(old, new)


def plan_leaf(leaf: str, env: Mapping[str, str], incoming: set[str] | None = None) -> Plan:
    """LEAF's plan, read-only. INCOMING names children that an earlier step
    (state's move) will put into NEW: for sound, `sound` under state."""
    old, new = settings.leaf_old(leaf, env), settings.leaf_new(leaf, env)
    p = Plan(leaf, str(old), str(new), ABSENT)
    var = settings.LEAF_OVERRIDE[leaf]
    if settings.leaf_override(leaf, env) is not None:
        p.action, p.why = REFUSE, (f"{var} is set: the operator owns that path, so "
                                   "nothing is moved for it; unset it to migrate")
        return p
    new_there = os.path.lexists(new) or bool(incoming)
    if _points_at(old, new):
        p.action, p.why = DONE, "already moved: the old path is a link to the new one"
        return p
    if os.path.islink(old):
        if not os.path.exists(old):
            p.action, p.why = REFUSE, f"the old path is a dangling link (to {os.readlink(old)})"
        elif new_there:
            p.action, p.why = REFUSE, ("the old path is a link elsewhere and the new one "
                                       "exists: a human decides")
        elif not os.path.isabs(os.readlink(old)):
            p.action, p.why = REFUSE, (f"the old path is a link with a relative target "
                                       f"({os.readlink(old)}): moved, it would point somewhere "
                                       "else; a human decides")
        elif _cross_device(old, new):
            p.action, p.why = REFUSE, "EXDEV: the new path is on another device; no copy fallback"
        else:
            p.action = MOVE_LINK
            e, links, keys = _measure(old, ".", old.parent)
            p.entries, p.links, p.key_like = [e], links, keys
            p.why = f"the old path is itself a link (to {e.target}): the link is moved"
        return p
    if not os.path.lexists(old):
        p.why = "nothing at the old path"
        return p
    if not old.is_dir():
        p.action, p.why = REFUSE, "the old path is not a folder"
        return p
    if not new_there:
        if _cross_device(old, new):
            p.action, p.why = REFUSE, "EXDEV: the new path is on another device; no copy fallback"
            return p
        p.action = RENAME
        e, p.links, p.key_like = _measure(old, ".", old)
        p.entries = [e]
        _refuse_escapes(p, old)
        return p
    # new is there (or will be): state merges child by child; sound collides.
    if os.path.islink(new) or (os.path.lexists(new) and not new.is_dir()):
        p.action, p.why = REFUSE, "the new path exists and is not a plain folder"
        return p
    if leaf != "state":
        p.action, p.why = REFUSE, ("collision: the new path already exists"
                                   + (" (state's move brings a `sound`)" if incoming else ""))
        return p
    try:
        names = sorted(os.listdir(old))
        clash = sorted(set(names) & (set(os.listdir(new)) | (incoming or set())))
    except OSError as e:
        p.action, p.why = REFUSE, f"cannot list a folder: {e}"
        return p
    if clash:
        p.action, p.why = REFUSE, f"collision: {', '.join(clash)} exist in both; nothing merges"
        return p
    if _cross_device(old, new):
        p.action, p.why = REFUSE, "EXDEV: the new path is on another device; no copy fallback"
        return p
    p.action, p.why = MERGE, "the new folder exists: each child is renamed in"
    for n in names:
        e, links, keys = _measure(old / n, n, old)
        p.entries.append(e)
        p.links += links
        p.key_like += keys
    _refuse_escapes(p, old)
    return p


def _refuse_escapes(p: Plan, old: Path) -> None:
    """Refuse P when a link inside OLD leads out of it by a relative path."""
    out = _escapes(old, p.links)
    if out:
        p.action, p.why = REFUSE, ("a link inside leads out of the folder by a relative "
                                   "path; moved, it would point somewhere else: "
                                   + ", ".join(out))


def plan(env: Mapping[str, str]) -> list[Plan]:
    """Both leaves' plans, state first. Sound's new folder is inside state's,
    so a `sound` child moving with state counts as already there for sound."""
    state = plan_leaf("state", env)
    incoming: set[str] = set()
    if state.action in (RENAME, MERGE):
        old = Path(state.old)
        if os.path.lexists(old / "sound"):
            incoming.add("sound")
    sound = plan_leaf("sound", env, incoming)
    if state.action == MOVE_LINK and sound.action in (RENAME, MOVE_LINK):
        sound.action, sound.why = REFUSE, ("state's new folder would be a link elsewhere: "
                                           "move sound by hand, or state first")
    return [state, sound]


# --- the agents that write these leaves ----------------------------------------------------

def _run(argv: list[str]) -> subprocess.CompletedProcess:
    """launchctl or systemctl, read-only verbs only. Fenced in tests."""
    if os.environ.get(FENCE_ENV):
        raise RuntimeError(f"{argv[0]} is fenced off ({FENCE_ENV} is set: tests); "
                           "replace migrate._run with a double")
    return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10)


def _platform() -> str:
    return sys.platform


def _launchd(label: str) -> str:
    try:
        r = _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    except FileNotFoundError:
        return NOT_LOADED
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    return LOADED if r.returncode == 0 else NOT_LOADED


def _systemd(unit: str) -> str:
    try:
        r = _run(["systemctl", "--user", "is-active", "--quiet", unit])
    except FileNotFoundError:
        return NOT_LOADED
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    return LOADED if r.returncode == 0 else NOT_LOADED


def _lock_held(path: Path) -> str:
    """A `pane-state watch` holds PATH's flock (the agent's, or one run by
    hand). Probed read-only: an absent file is no watcher, and nothing is
    created or written."""
    import fcntl
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return NOT_LOADED
    except OSError:
        return UNKNOWN
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as e:
        return LOADED if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES) else UNKNOWN
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return NOT_LOADED
    finally:
        os.close(fd)


def agents(env: Mapping[str, str]) -> list[Agent]:
    """Each tools agent that writes state or sound, and whether it runs."""
    from sessiontools import default_model_watch, pane_state, pane_state_agent
    from sessiontools.sound import service
    out: list[Agent] = []
    darwin = _platform() == "darwin"
    for name, labels in (("pane-state agent", (pane_state_agent.LABEL, pane_state_agent.LEGACY_LABEL)),
                         ("default-model watch", (default_model_watch.LABEL,)),
                         ("sound engine", (service.LABEL, service.LEGACY_LABEL))):
        for label in labels:
            state = _launchd(label) if darwin else NOT_LOADED
            out.append(Agent(name, f"launchd {label}", state,
                             "" if darwin else "no launchd on this host"))
    if not darwin:
        out.append(Agent("default-model watch", f"systemd {default_model_watch.SYSTEMD_PATH_UNIT}",
                         _systemd(default_model_watch.SYSTEMD_PATH_UNIT)))
    lock = pane_state.lock_path(env)
    out.append(Agent("pane-state watch", f"lock {lock}", _lock_held(lock)))
    return out


# --- moving --------------------------------------------------------------------------------

def _rename(src: Path, dst: Path) -> None:
    """The only call that moves anything: rename never follows a link."""
    os.rename(src, dst)


class Failed(Exception):
    """A move failed part-way; the plans say what moved."""


def _exdev(e: OSError) -> bool:
    return e.errno == errno.EXDEV


def _clear(dst: Path) -> None:
    """Refuse to rename onto DST if anything appeared there since the plan:
    rename would silently replace a file or an empty folder."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "appeared since the plan", str(dst))


def _mkparents(path: Path) -> list[Path]:
    """Make PATH's missing ancestors; the ones made, outermost first."""
    made: list[Path] = []
    p = path.parent
    while not os.path.lexists(p) and p != p.parent:
        made.insert(0, p)
        p = p.parent
    for d in made:
        d.mkdir()
    return made


def _unmake(made: list[Path]) -> None:
    """Remove the folders _mkparents made, innermost first, while empty."""
    for d in reversed(made):
        try:
            d.rmdir()
        except OSError:
            return


def _do(p: Plan) -> None:
    old, new = Path(p.old), Path(p.new)
    if p.action in (RENAME, MOVE_LINK):
        made: list[Path] = []
        try:
            made = _mkparents(new)
            _clear(new)
            _rename(old, new)
        except OSError as e:
            _unmake(made)
            p.why = (f"EXDEV: {e}; nothing moved, no copy fallback" if _exdev(e)
                     else f"rename failed: {e}; nothing moved")
            raise Failed(p.why) from e
    elif p.action == MERGE:
        done: list[str] = []
        try:
            for e in p.entries:
                _clear(new / e.name)
                _rename(old / e.name, new / e.name)
                done.append(e.name)
            os.rmdir(old)
        except OSError as err:
            back = []
            for n in reversed(done):
                try:
                    _rename(new / n, old / n)
                except OSError:
                    back.append(n)
            p.why = (f"merge failed: {err}; " + ("EXDEV, no copy fallback; " if _exdev(err) else "")
                     + ("every child was put back" if not back
                        else f"NOT put back, still in the new folder: {', '.join(back)}"))
            raise Failed(p.why) from err
    else:
        return
    p.moved = True
    try:
        os.symlink(new, old)
    except OSError as e:
        p.why = f"moved, but no link was left at the old path: {e}"
        raise Failed(p.why) from e
    p.link_left = True


# --- the verb ------------------------------------------------------------------------------

def refusals(plans: list[Plan], found: list[Agent]) -> list[str]:
    """Why the run refuses: a leaf's own refusal, or, when anything would
    move, an agent loaded or not measurable. A dry run prints the same."""
    out = [f"{p.leaf}: {p.why}" for p in plans if p.action == REFUSE]
    if any(p.action in (RENAME, MERGE, MOVE_LINK) for p in plans):
        for a in found:
            if a.state == LOADED:
                out.append(f"{a.name} ({a.unit}) is loaded: stop it first, then run again")
            elif a.state == UNKNOWN:
                out.append(f"{a.name} ({a.unit}): cannot tell whether it runs")
    return out


def migrate(env: Mapping[str, str], *, dry_run: bool,
            probe: Callable[[Mapping[str, str]], list[Agent]] | None = None) -> dict:
    """Plan, check, and (unless DRY_RUN or refused) move. The receipt."""
    plans = plan(env)
    found = (probe or agents)(env)
    refused = refusals(plans, found)
    receipt = {"dry_run": dry_run, "refused": refused, "failed": None,
               "leaves": plans, "agents": found}
    if dry_run or refused:
        return receipt
    for p in plans:
        try:
            _do(p)
        except Failed as e:
            receipt["failed"] = f"{p.leaf}: {e}"
            break
    return receipt


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"  # pragma: no cover


def render(r: dict) -> str:
    dry = r["dry_run"]
    head = ("dry run: nothing was changed" if dry else
            "REFUSED: nothing was moved" if r["refused"] else
            "FAILED part-way" if r["failed"] else "done")
    lines = [f"{PROG}: {head}"]
    verb = "would move" if dry or r["refused"] else "moved"  # a failed leaf: see its line
    for p in r["leaves"]:
        lines.append(f"{p.leaf:6} {p.old} -> {p.new}")
        if p.action in (RENAME, MERGE, MOVE_LINK):
            lines.append(f"       {p.action}" + (f": {p.why}" if p.why else ""))
            for e in p.entries:
                what = f"link -> {e.target}" if e.kind == "link" else f"{e.files} files, {_size(e.size)}"
                lines.append(f"       {verb} {e.name if e.name != '.' else '(the whole folder)'}"
                             f"  {e.kind}, {what}")
            for link in p.links:
                lines.append(f"       link moved as a link: {link['path']} -> {link['target']}")
            if p.link_left:
                lines.append(f"       link left: {p.old} -> {p.new}")
            elif dry or r["refused"]:
                lines.append(f"       would leave a link: {p.old} -> {p.new}")
            for k in p.key_like:
                lines.append(f"       KEY-LIKE NAME: {k}  (STOP and report it before a live run)")
        else:
            lines.append(f"       {p.action}: {p.why}" if p.why else f"       {p.action}")
    lines.append("agents (a live run refuses while one is loaded):")
    for a in r["agents"]:
        lines.append(f"       {a.name:20} {a.state:10} {a.unit}" + (f"  ({a.why})" if a.why else ""))
    for why in r["refused"]:
        lines.append(f"{'would refuse' if dry else 'refused'}: {why}")
    if r["failed"]:
        lines.append(f"failed: {r['failed']}")
    if not r["refused"] and not r["failed"] and all(
            p.action in (ABSENT, DONE) for p in r["leaves"]):
        lines.append("nothing to do")
    return "\n".join(lines) + "\n"


def to_json(r: dict) -> str:
    return json.dumps({**r, "leaves": [asdict(p) for p in r["leaves"]],
                       "agents": [asdict(a) for a in r["agents"]]}, indent=1) + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description="Move tools' state and sound folders to "
                                "~/.sessionkit/tools/state/, leaving a link at each old path.",
                                epilog="Run with --dry-run first. Refuses (exit 3, nothing moved) "
                                "on an override, a collision, a cross-device move, or a loaded "
                                "tools agent.",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and the receipt; change nothing")
    p.add_argument("--json", action="store_true", help="the receipt as JSON")
    return p


def main(argv: list[str], env: Mapping[str, str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    r = migrate(os.environ if env is None else env, dry_run=a.dry_run)
    sys.stdout.write(to_json(r) if a.json else render(r))
    if r["refused"]:
        return REFUSED
    return FAILED if r["failed"] else OK
