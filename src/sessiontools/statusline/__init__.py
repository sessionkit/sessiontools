"""`session statusline install | check | sample`: Claude Code's status line.

The script Claude Code runs is `statusline.sh`, beside this file. This
module never renders on Claude Code's behalf: it COPIES the script and
points `statusLine` at it (`install`), says what is there (`check`), and
prints the sample payload (`sample`). The copy is the user's to edit; a
stamp line after the shebang lets `check` tell pristine from edited from
not ours.

Install writes the sessionkit `claude-code` layer, never `~/.claude`.
K is `~/.sessionkit/tools/settings/`, the claude-code tool's settings
directory (settings.host_dir): the script goes to
K/statusline.sh and the entry to the drop-in D,
K/claude-code.d/50-statusline.json, which install owns whole.
`session start` renders it into its `--settings` file.
Nothing under `~/.claude` or `$CLAUDE_CONFIG_DIR` is opened for writing.

Install reads the whole kit host layer (template, claude-code.json, every
drop-in), merged key by key as `session start` merges it, classifies, then
acts:

    X  D or another kit host file unreadable          refuse, exit 5
    E  no statusLine, no statusline.sh                write both
    F  no statusLine, statusline.sh there             pristine-ours: write the entry
                                                      (and upgrade); else refuse
    C  entry ours, file pristine and current          nothing to do
    U  entry ours, file pristine, another version     replace the file
    M  entry ours, no file                            write the file
    D  entry ours, file edited or not ours            refuse
    O  any other statusLine                           refuse; --force: in D, back D
                                                      up and set ours; in a file read
                                                      before D, set ours in D (theirs
                                                      untouched); read after D, refuse
    L  no key or entry ours, statusline.sh a link     refuse

Before any of these, and --force never overrides it: K, D or the script
resolving inside the config dir (a person's own symlink) is refused, exit
3; so is an entry a `_locked` path read before D would keep from applying
(the layer is merged as if D were written, at plan time).

"The entry" is the statusLine the layer resolves to, wherever it was set:
ours hand-written into claude-code.json is ours. `--force` backs up first
(every file that is not pristine-ours, and D when its entry is replaced),
then replaces, then reads back: D's own bytes, and the layer re-resolved,
which must CONTAIN the entry D holds (a sub-key D does not set may still
come from an earlier file; the report names it). A `statusline.sh` that is
not a file or a link is refused in every state.

`check` reads the same kit layer, says what overrides it, and exits 0
only when ours is what a session start seat runs and nothing visible blocks
it. Both verbs REPORT `$CLAUDE_CONFIG_DIR`, else `~/.claude`, the dir a
bare `claude` reads: its statusLine and statusline.sh, with what wins where
(claude_report). Read only, never a reason to refuse.

Exit codes: 0 done or nothing to do, 2 usage, 3 refused (nothing written),
4 a write, a backup or the read-back failed, 5 a settings file unreadable
or statusline.sh not a file or link.

Stdlib only, and nothing heavy at import: `session` must stay fast.
`sessiontools.settings` (stdlib only) is imported by install alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sessiontools.verbs import Group, resolve

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "statusline.sh"
SAMPLE = HERE / "sample.json"
NAME = "statusline.sh"
SETTINGS = "settings.json"
#: The settings tool whose host layer install writes.
TOOL = "claude-code"
#: The drop-in install owns whole, under K/claude-code.d/.
DROP_IN = "50-statusline.json"
ABOUT = ("Written by `session statusline install` (sessiontools). session start passes "
         "statusLine to claude as --settings. Edit or delete it; `session statusline check` "
         "says what is here.")

OK, USAGE, REFUSED, FAILED, UNREADABLE = 0, 2, 3, 4, 5

STAMP_RE = re.compile(rb"^# sessiontools-statusline (\S+) sha256:([0-9a-f]{64})$")

#: Hand copies that came before this one, by the sha256 of their bytes.
#: Named so the user knows what `--force` would replace.
KNOWN = {
    "a19e82a30f205cca95d8bc38747175b98e01bdf2091305171144d5186cc18333":
        "an earlier hand copy of this status line (an older revision of this script)",
    "68f55691a4eb23e88c9e8beb4d175ce2fc0234334b9a29ed665b7953082ff55f":
        "an earlier hand copy of this status line (claude-code-tools' scripts/statusline.sh)",
}

#: File-based managed settings (code.claude.com/docs/en/managed-settings):
#: managed-settings.json, then managed-settings.d/*.json in alphabetical order.
MANAGED_DIRS = {
    "darwin": Path("/Library/Application Support/ClaudeCode"),
    "linux": Path("/etc/claude-code"),
    "win32": Path(r"C:\Program Files\ClaudeCode"),
}

STATES = {
    "X": "a settings file is unreadable",
    "E": "empty slot",
    "F": "a statusline.sh with no settings entry",
    "C": "installed and current",
    "U": "installed, another version",
    "M": "the entry is ours, the script is missing",
    "D": "the entry is ours, the script is edited or not ours",
    "O": "another status line is set",
    "L": "statusline.sh is a symlink",
}


class Failed(Exception):
    """A write, a backup or a read-back failed: exit 4, no traceback."""


# --- what we ship -------------------------------------------------------------

def version() -> str:
    from sessiontools import __version__
    return __version__


def shipped() -> bytes:
    return SCRIPT.read_bytes()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stamped(data: bytes | None = None, ver: str | None = None) -> bytes:
    """The shipped script with the stamp line added after the shebang."""
    data = shipped() if data is None else data
    ver = version() if ver is None else ver
    stamp = f"# sessiontools-statusline {ver} sha256:{sha(data)}\n".encode()
    first, nl, rest = data.partition(b"\n")
    return first + nl + stamp + rest


@dataclass
class Script:
    """What a statusline.sh holds, judged against what we ship."""
    kind: str                 # pristine | edited | foreign
    version: str | None = None
    stamp_sha: str | None = None
    body: bytes = b""         # the bytes without the stamp line
    known: str | None = None  # a named earlier hand copy

    @property
    def current(self) -> bool:
        return self.kind == "pristine" and self.stamp_sha == sha(shipped())


def judge(data: bytes) -> Script:
    lines = data.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = STAMP_RE.match(line.rstrip(b"\r\n"))
        if m:
            body = b"".join(lines[:i] + lines[i + 1:])
            ver, hexd = m.group(1).decode(), m.group(2).decode()
            kind = "pristine" if sha(body) == hexd else "edited"
            return Script(kind, ver, hexd, body)
    return Script("foreign", body=data, known=KNOWN.get(sha(data)))


# --- where -------------------------------------------------------------------

def config_dir() -> Path:
    """$CLAUDE_CONFIG_DIR if set, else ~/.claude (HOME-based, never resolved)."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(os.path.expanduser(raw)) if raw else Path(os.path.expanduser("~")) / ".claude"
    return Path(os.path.abspath(path))


def kit_dir() -> Path:
    """K: `~/.sessionkit/tools/settings/`, the claude-code tool's settings
    directory (`settings.host_dir`; HOME-based, never resolved)."""
    from sessiontools import settings as st
    return Path(os.path.abspath(st.host_dir(TOOL)[0]))


def drop_in_path(kdir: Path) -> Path:
    """D: the drop-in install owns whole."""
    return kdir / f"{TOOL}.d" / DROP_IN


def command_for(cdir: Path) -> str:
    """The command for <dir>/statusline.sh: the tilde form under HOME
    (`~/.sessionkit/tools/settings/statusline.sh`), which the shell
    claude runs it in expands, and which survives a kit dir synced between
    hosts; else the absolute path."""
    home = Path(os.path.abspath(os.path.expanduser("~")))
    try:
        rel = cdir.relative_to(home)
    except ValueError:
        return str(cdir / NAME)
    return f"~/{rel.as_posix()}/{NAME}" if rel.parts else f"~/{NAME}"


def shown(path: Path) -> str:
    return str(path)


def is_ours(entry: object, cdir: Path) -> bool:
    """`type` is `command` and the command, ~ expanded, is <dir>/statusline.sh."""
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return False
    cmd = entry.get("command")
    if not isinstance(cmd, str):
        return False
    return os.path.normpath(os.path.expanduser(cmd.strip())) == os.path.normpath(cdir / NAME)


def new_entry(cdir: Path, old: object = None) -> dict:
    """The entry install writes. The user's other fields in an object are
    kept (padding never reset, refreshInterval, unknown keys); a fresh
    entry carries padding 1."""
    if isinstance(old, dict):
        entry = dict(old)
        entry["type"] = "command"
        entry["command"] = command_for(cdir)
        return entry
    return {"type": "command", "command": command_for(cdir), "padding": 1}


# --- reading the slot -----------------------------------------------------------

_MISSING = object()


@dataclass
class Slot:
    cdir: Path
    settings_path: Path
    settings: dict | None = None        # None: no settings.json
    error: str | None = None            # set: X
    entry: object = _MISSING
    script_path: Path = Path()
    file: str = "none"                  # none | file | link | other
    script: Script | None = None
    link_target: str | None = None
    # The kit layer (read_kit). The legacy slot `check` reads leaves
    # them at their defaults: one file, which holds the entry it judges.
    error_path: Path | None = None      # the unreadable file (X); None: settings_path
    own_entry: object = _MISSING        # D's own statusLine; `entry` is the resolved one
    owner: Path | None = None           # the file that sets the entry's command
    order: int = 0                      # owner read before (-1), is (0), after (1) D
    origins: dict = field(default_factory=dict)   # statusLine.<sub> -> its file
    setters: list = field(default_factory=list)   # (file, its statusLine) for each that sets it
    sources: list = field(default_factory=list)   # the layer's settings.Source list, cascade order
    locked: dict = field(default_factory=dict)    # dotted path -> the file that locks it

    @property
    def has_key(self) -> bool:
        return self.entry is not _MISSING

    @property
    def elsewhere(self) -> bool:
        """The entry install judges is set in a file other than settings_path."""
        return self.owner is not None and self.order != 0


def read_settings(path: Path) -> tuple[dict | None, str | None]:
    """(the object or None if absent, an error or None)."""
    if not os.path.lexists(path):
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return None, f"cannot read: {e}"
    try:
        data = json.loads(text)
    except ValueError as e:
        return None, f"not JSON: {e}"
    if not isinstance(data, dict):
        return None, f"not a JSON object (a {type(data).__name__})"
    return data, None


def read_slot(cdir: Path, settings_path: Path | None = None) -> Slot:
    """One settings file and <dir>/statusline.sh: the file's own statusLine
    is the entry. `check` reads `~/.claude` this way; `read_kit` widens it."""
    slot = Slot(cdir, settings_path or cdir / SETTINGS, script_path=cdir / NAME)
    slot.settings, slot.error = read_settings(slot.settings_path)
    if slot.settings is not None and "statusLine" in slot.settings:
        slot.entry = slot.settings["statusLine"]
    _read_script(slot)
    return slot


def _rank(path: Path, kdir: Path) -> tuple:
    """A kit host file's place in cascade order: template, <tool>.json, then
    the drop-ins by name (settings.sources)."""
    if path.name == f"{TOOL}.template.json" and path.parent == kdir:
        return (0, "")
    if path.parent == kdir:
        return (1, "")
    return (2, path.name)


def read_kit(kdir: Path) -> Slot:
    """The whole kit host layer, as `session start` reads it: every host
    file in K is read (one unreadable is X), merged key by key, and the
    statusLine it resolves to is the entry. `own_entry` is D's own."""
    from sessiontools import settings as st
    d = drop_in_path(kdir)
    slot = read_slot(kdir, d)
    slot.entry = _MISSING
    if slot.settings is not None and "statusLine" in slot.settings:
        slot.own_entry = slot.settings["statusLine"]
    if slot.error:
        slot.error_path = d
        return slot
    files = st._host_files(kdir, TOOL)
    if not files:
        return slot
    sources = []
    for f in files:
        data, err = (slot.settings, None) if f == d else read_settings(f)
        if err or data is None:
            slot.error, slot.error_path = err or "cannot read", f
            return slot
        layer = "template" if _rank(f, kdir)[0] == 0 else "host"
        sources.append(st.Source(layer, f, data))
        if "statusLine" in data:
            slot.setters.append((f, data["statusLine"]))
    try:
        values, origins, slot.locked, _leaves = st.merge(sources)
    except st.SettingsError as e:
        slot.error, slot.error_path = str(e), kdir
        return slot
    slot.sources = sources
    if "statusLine" not in values:
        return slot
    slot.entry = values["statusLine"]
    slot.origins = {k: Path(v) for k, v in origins.items()
                    if k == "statusLine" or k.startswith("statusLine.")}
    owner = (slot.origins.get("statusLine.command") or slot.origins.get("statusLine")
             or next(iter(slot.origins.values())))
    slot.owner = owner
    mine, theirs = _rank(d, kdir), _rank(owner, kdir)
    slot.order = 0 if owner == d else (-1 if theirs < mine else 1)
    return slot


def _read_script(slot: Slot) -> None:
    """What <dir>/statusline.sh is: none, a file (judged), a link or other."""
    try:
        st = os.lstat(slot.script_path)
    except FileNotFoundError:
        return
    except OSError as e:
        slot.file = "other"
        slot.link_target = str(e)
        return
    if stat.S_ISLNK(st.st_mode):
        slot.file = "link"
        slot.link_target = os.readlink(slot.script_path)
    elif stat.S_ISREG(st.st_mode):
        slot.file = "file"
        try:
            slot.script = judge(slot.script_path.read_bytes())
        except OSError:
            slot.script = Script("foreign")
    else:
        slot.file = "other"


def classify(slot: Slot) -> str:
    if slot.error:
        return "X"
    if not slot.has_key:
        return {"none": "E", "link": "L", "file": "F"}.get(slot.file, "F")
    if is_ours(slot.entry, slot.cdir):
        if slot.file == "none":
            return "M"
        if slot.file == "link":
            return "L"
        s = slot.script
        if s is not None and s.kind == "pristine":
            return "C" if s.current else "U"
        return "D"
    return "O"


def pristine_ours(slot: Slot) -> bool:
    return slot.file == "file" and slot.script is not None and slot.script.kind == "pristine"


# --- the plan --------------------------------------------------------------------

@dataclass
class Plan:
    state: str
    mkdirs: list[Path] = field(default_factory=list)    # to create, in order
    backups: list[Path] = field(default_factory=list)   # what to back up, in order
    script: str | None = None                            # create | replace
    entry: dict | None = None                            # the statusLine to set in D
    refusal: str | None = None
    forceable: bool = True                               # does --force get past the refusal
    code: int = OK
    notes: list[str] = field(default_factory=list)


def describe_entry(entry: object, where: Path) -> str:
    """The "you've already got one" line for a statusLine that is not ours,
    set in WHERE."""
    verbatim = json.dumps(entry, ensure_ascii=False)
    if isinstance(entry, dict) and entry.get("type") == "command" and isinstance(entry.get("command"), str):
        cmd = entry["command"].strip()
        expanded = os.path.expanduser(cmd)
        if os.path.isabs(expanded) and os.path.isfile(expanded):
            return f"you've already got one: {cmd} (set in {shown(where)})"
    return f"you've already got one, inline in {shown(where)}: {verbatim}"


def describe_file(slot: Slot) -> str:
    if slot.file == "link":
        return f"you've already got one: a symlink to {slot.link_target} ({shown(slot.script_path)})"
    line = f"you've already got one: {shown(slot.script_path)}"
    s = slot.script
    if s is not None and s.kind == "edited":
        line += f" (sessiontools {s.version}, edited: `session statusline check --diff` shows how)"
    elif s is not None and s.known:
        line += f" ({s.known})"
    return line


def _inside(path: str | Path, cfg: str, cfg_id: tuple[int, int] | None) -> bool:
    """Is PATH the config dir or under it? Decided by IDENTITY: every
    existing ancestor of realpath(PATH), itself included, is compared with
    the config dir by (st_dev, st_ino), so a case variant or a macOS
    firmlink route (/System/Volumes/Data/...) of the same dir is caught,
    which a string compare is not. The plain prefix test
    on the realpath is kept too: it catches a link into a config dir that
    does not exist yet."""
    real = os.path.realpath(path)
    if real == cfg or real.startswith(cfg.rstrip(os.sep) + os.sep):
        return True
    if cfg_id is None:
        return False
    cur = real
    while True:
        try:
            st = os.stat(cur)
        except OSError:
            pass
        else:
            if (st.st_dev, st.st_ino) == cfg_id:
                return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def into_config_dir(slot: Slot) -> str | None:
    """The refusal when K, D or the script is inside the config dir
    (`$CLAUDE_CONFIG_DIR`, else `~/.claude`): a person's own symlink must
    never lead install into it. None when all three are outside."""
    cfg = os.path.realpath(config_dir())
    try:
        st = os.stat(cfg)
        cfg_id: tuple[int, int] | None = (st.st_dev, st.st_ino)
    except OSError:
        cfg_id = None
    for what, path in (("the kit dir", slot.cdir), ("the drop-in", slot.settings_path),
                       ("the script", slot.script_path)):
        if _inside(path, cfg, cfg_id):
            return (f"{what} {shown(path)} resolves to {os.path.realpath(path)}, inside {cfg}, "
                    f"the dir a bare claude reads: install never writes there. Point it "
                    f"outside {cfg}, then run install again")
    return None


def locked_out(slot: Slot, entry: dict) -> str | None:
    """The refusal when a `_locked` path read before D keeps D's entry from
    applying: the layer is merged as it would be with ENTRY written into D,
    and must contain it, as the read-back requires after the write. None
    when it would."""
    from sessiontools import settings as st
    d, kdir = slot.settings_path, slot.cdir
    mine = dict(slot.settings) if slot.settings is not None else {"_about": ABOUT}
    mine["statusLine"] = entry
    srcs = [src for src in slot.sources if Path(src.path) != d] + [st.Source("host", d, mine)]
    srcs.sort(key=lambda src: _rank(Path(src.path), kdir))
    try:
        values, _origins, locked, _leaves = st.merge(srcs)
    except st.SettingsError:
        return None                      # X: read_kit has refused already
    if _contains(values.get("statusLine", _MISSING), entry):
        return None
    locks = sorted((lp, f) for lp, f in locked.items()
                   if (lp == "statusLine" or lp.startswith("statusLine."))
                   and _rank(Path(f), kdir) < _rank(d, kdir))
    if not locks:
        return (f"the layer would not resolve statusLine to the entry {DROP_IN} would hold; "
                "nothing written")
    named = "; ".join(f"{shown(Path(f))} locks {lp}" for lp, f in locks)
    return (f"{named}, and is read before {DROP_IN}, so the statusLine install would set "
            "there would not be read. Remove the lock first, then run install again")


def plan(slot: Slot, force: bool) -> Plan:
    state = classify(slot)
    p = Plan(state)
    cdir = slot.cdir
    inside = into_config_dir(slot)
    if inside:
        p.refusal = inside
        p.forceable = False
        p.code = REFUSED
        return p
    if state == "X":
        p.refusal = (f"{shown(slot.error_path or slot.settings_path)} is unreadable ({slot.error}); "
                     "nothing written, --force included: fix or move it first")
        p.code = UNREADABLE
        return p
    if slot.file == "other":
        p.refusal = (f"{shown(slot.script_path)} is not a file or a symlink; "
                     "nothing written, --force included: move it first")
        p.code = UNREADABLE
        return p
    if state == "O" and slot.order > 0:
        p.refusal = (f"{shown(slot.owner)} sets statusLine and is read after {DROP_IN}, so it "
                     f"wins: {json.dumps(slot.entry, ensure_ascii=False)}")
        p.forceable = False
        p.code = REFUSED
        return p
    if not slot.has_key or state == "O":
        p.entry = new_entry(cdir, slot.own_entry if state == "O" else None)
        lock = locked_out(slot, p.entry)
        if lock:
            p.entry = None
            p.refusal = lock
            p.forceable = False
            p.code = REFUSED
            return p
    if slot.has_key and slot.elsewhere and state != "O":
        p.notes.append(f"statusLine (ours) is set in {shown(slot.owner)}, not {DROP_IN}; left there")
    if not force:
        refuse = None
        if state == "F" and not pristine_ours(slot):
            refuse = describe_file(slot) + " (not wired into settings)"
        elif state in ("D", "L"):
            refuse = describe_file(slot)
        elif state == "O":
            refuse = describe_entry(slot.entry, slot.owner or slot.settings_path)
        if refuse:
            p.refusal = refuse
            p.code = REFUSED
            return p
    if state == "C":
        return p
    # The --force backup rule: a statusline.sh that is not pristine-ours is
    # backed up before it is replaced, whatever the state.
    if slot.file in ("file", "link") and not pristine_ours(slot):
        p.backups.append(slot.script_path)
    if state == "O" and not slot.elsewhere:
        p.backups.insert(0, slot.settings_path)
    if state in ("E", "M"):
        p.script = "create"
    elif state in ("F", "U", "D", "L", "O"):
        if slot.file == "file" and slot.script is not None and slot.script.current:
            p.script = None          # ours and current already: only the entry is missing
        else:
            p.script = "replace" if slot.file != "none" else "create"
    if not cdir.is_dir():
        p.mkdirs.append(cdir)
    if p.entry is not None and not slot.settings_path.parent.is_dir():
        p.mkdirs.append(slot.settings_path.parent)
    return p


# --- writing -----------------------------------------------------------------------

def _umask() -> int:
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def _write_bytes(path: Path, data: bytes, mode: int) -> None:
    """Atomic: a temp file beside `path`, then os.replace. A symlink at
    `path` is replaced by the file; its target is never opened."""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def settings_target(path: Path) -> Path:
    """Where settings.json is really written: a symlink's resolved target."""
    return Path(os.path.realpath(path)) if path.is_symlink() else path


def _write_settings(path: Path, data: dict) -> None:
    target = settings_target(path)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_umask()
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _write_bytes(target, text.encode("utf-8"), mode)


def backup_name(path: Path, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    base = path.with_name(f"{path.name}.bak-{stamp}")
    cand, n = base, 2
    while os.path.lexists(cand):
        cand = base.with_name(f"{base.name}-{n}")
        n += 1
    return cand


def _copy_for_backup(path: Path, dest: Path, data: bytes) -> None:
    """Write the copy: a new file only (never over an existing name), the
    original's mode kept."""
    with open(dest, "xb") as f:
        f.write(data)
    shutil.copymode(path, dest)


def _backup(path: Path, as_link: bool) -> Path:
    """Copy `path` beside itself and read the copy back. Raises Failed:
    nothing has been replaced yet.

    `as_link`: a link that install REPLACES (statusline.sh) is backed up as
    a link, because its target is never written. A link install writes
    THROUGH (settings.json, written at its target) is backed up as a copy
    of the target's bytes: a link backup would point at the new content."""
    dest = backup_name(path)
    try:
        if as_link and path.is_symlink():
            target = os.readlink(path)
            os.symlink(target, dest)
            ok = os.readlink(dest) == target
        else:
            data = path.read_bytes()
            _copy_for_backup(path, dest, data)
            ok = dest.read_bytes() == data
    except OSError as e:
        raise Failed(f"backup {shown(dest)} did not land ({e}): nothing replaced") from None
    if not ok:
        raise Failed(f"backup {shown(dest)} did not land (it reads back different): "
                     "nothing replaced")
    return dest


def git_repo_of(path: Path) -> Path | None:
    """The nearest directory at or above `path` holding .git; no git fork."""
    for d in (path, *path.parents):
        if os.path.lexists(d / ".git"):
            return d
    return None


# --- the verbs -------------------------------------------------------------------------

def _out(line: str = "") -> None:
    print(line)


def _err(line: str) -> None:
    sys.stdout.flush()      # piped, the header must still come before the refusal
    print(line, file=sys.stderr)


def preview() -> str | None:
    """Our line, rendered from sample.json with both switches off (no
    side effects), under a 2 s timeout. None on any failure."""
    import subprocess
    bash = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")
    if not bash:
        return None
    env = {**os.environ, "SESSIONTOOLS_STATUSLINE_TAP": "off", "SESSIONTOOLS_STATUSLINE_LOG": "off"}
    try:
        got = subprocess.run([bash, str(SCRIPT)], input=SAMPLE.read_bytes(), env=env,
                             capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    line = got.stdout.decode("utf-8", "replace").strip("\n")
    if got.returncode != 0 or not line or "\n" in line:
        return None
    return line


def borrowed(origins: dict, owner: Path | None) -> list[tuple[str, Path]]:
    """(statusLine.<sub>, file) for every sub-key set by a file other than
    OWNER: under the key-by-key merge it still applies."""
    return [(k, f) for k, f in origins.items() if k.startswith("statusLine.") and f != owner]


def install(dry_run: bool = False, force: bool = False) -> int:
    """Write the kit layer (K), never `~/.claude`: the script, then D. A
    stranded host (settings.stranded) gets NOTHING written, --force and
    --dry-run included: a first install would make K a host layer, and the
    files still at the old path would then never be said again."""
    from sessiontools import settings as st
    kdir = kit_dir()
    if st.stranded(TOOL):
        _out(f"session statusline install: {shown(kdir)}")
        sys.stdout.flush()
        st.say_stranded(TOOL)
        _err(f"Nothing written: the {TOOL} settings have not been moved. "
             "--force does not override this.")
        return REFUSED
    slot = read_kit(kdir)
    p = plan(slot, force)
    ver = version()
    d = slot.settings_path
    _out(f"session statusline install: {shown(kdir)}")
    _out(f"  state {p.state}: {STATES[p.state]}")
    for note in p.notes:
        _out(f"  note: {note}")
    report, _blocked = claude_report()
    if p.refusal:
        _err(p.refusal)
        if p.code == REFUSED and not p.forceable:
            _err("Nothing written. --force does not override this.")
        elif p.code == REFUSED:
            line = preview()
            if line:
                _err("sessiontools' status line would show (sample payload):")
                _err(f"  {line}")
            if p.state == "O" and slot.elsewhere:
                _err(f"Nothing written. `--force` sets ours in {shown(d)}, which is read after "
                     f"{shown(slot.owner)}; that file is left as it is.")
            else:
                _err("Nothing written. `--force` replaces it, backing up first.")
        _say(report)
        return p.code
    if p.state == "C":
        _out(f"already installed (sessiontools {slot.script.version if slot.script else ver}); "
             "nothing to do")
        for sub, f in borrowed(slot.origins, slot.owner):
            _out(f"  {sub} comes from {shown(f)}")
        _say(report)
        return OK

    # The plan, always first.
    _out("plan:")
    for m in p.mkdirs:
        _out(f"  create {shown(m)}/")
    for b in p.backups:
        _out(f"  backup {shown(b)} → {shown(backup_name(b))}")
    if p.script:
        was = ""
        if p.script == "replace" and slot.script is not None and slot.script.kind == "pristine":
            old = slot.script.version
            if old == ver and slot.script.stamp_sha != sha(shipped()):
                old = f"{old} ({slot.script.stamp_sha[:8]})"
                was = f" (v{old} → v{ver} ({sha(shipped())[:8]}))"
            else:
                was = f" (v{old} → v{ver})"
        else:
            was = f" (sessiontools {ver})"
        _out(f"  {p.script} {shown(slot.script_path)}{was}")
    if p.entry is not None:
        where = shown(d)
        if d.is_symlink():
            where += f" (a symlink; written at {shown(settings_target(d))})"
        verb = "create" if slot.settings is None else "set statusLine in"
        _out(f"  {verb} {where}: \"statusLine\": {json.dumps(p.entry, ensure_ascii=False)}")
        if slot.has_key:
            _out(f"    (was: {json.dumps(slot.entry, ensure_ascii=False)}"
                 + (f", set in {shown(slot.owner)}, which is left as it is)" if slot.elsewhere else ")"))
    if slot.settings is None and p.entry is not None:
        _out(f"  note: {shown(d)} is created holding _about and statusLine")
    if not shutil.which("jq"):
        _out("  note: jq is not on PATH; the line reads `statusline: jq not found` until it is")
    _say(report)
    if dry_run:
        _out("dry run: nothing written")
        return OK

    # Then do it: the dirs, backups, the script, D last. A run cut short
    # after the script leaves K with no host file (a .sh is not one), so it
    # can never hide the old dir.
    try:
        written: list[str] = []
        backups: list[Path] = []
        if kdir in p.mkdirs:
            _mkdir(kdir)
        for b in p.backups:
            backups.append(_backup(b, as_link=b != d))
        body = stamped()
        if p.script:
            try:
                _write_bytes(slot.script_path, body, 0o755)
            except OSError as e:
                raise Failed(f"cannot write {shown(slot.script_path)}: {e}") from None
            written.append(NAME)
        if p.entry is not None:
            for m in p.mkdirs:
                if m != kdir:
                    _mkdir(m)
            data = dict(slot.settings) if slot.settings is not None else {"_about": ABOUT}
            data["statusLine"] = p.entry
            try:
                _write_settings(d, data)
            except OSError as e:
                raise Failed(f"cannot write {shown(d)}: {e}") from None
            written.append(DROP_IN)
        origins = readback(slot, p, body)
    except Failed as e:
        _err(f"session statusline install: {e}")
        return FAILED

    for b in backups:
        _out(f"backed up: {shown(b)}")
    _out(f"installed: {shown(slot.script_path)} (sessiontools {ver}), "
         f"statusLine runs {command_for(kdir)}; read back: both match; "
         "session start seats show it from their next launch")
    owner = d if p.entry is not None else slot.owner
    others = borrowed(origins, owner)
    for sub, f in others:
        _out(f"  {sub} comes from {shown(f)}")
    if p.state == "O" and slot.elsewhere:
        still = sorted({sub.split(".", 1)[1].split(".")[0] for sub, f in others if f == slot.owner})
        applies = f" (its {', '.join(still)} still appl{'ies' if len(still) == 1 else 'y'})" if still else ""
        _out(f"yours in {shown(slot.owner)} is still there; {DROP_IN} now wins over it{applies}. "
             f"Delete {DROP_IN} to get yours back.")
    repo = git_repo_of(kdir)
    if repo is not None and written:
        names = " and ".join(written)
        extra = f" (and {len(backups)} backup{'s' if len(backups) != 1 else ''} untracked)" if backups else ""
        _out(f"{shown(repo)} is a git repo: {names} {'is' if len(written) == 1 else 'are'} "
             f"now modified there{extra}. Nothing was committed.")
    return OK


def _say(report: list[str]) -> None:
    """The ~/.claude report: information on every run, never a reason to refuse."""
    for line in report:
        _out(f"  {line}")


def _mkdir(path: Path) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise Failed(f"cannot create {shown(path)}: {e}") from None


def _contains(got: object, want: dict) -> bool:
    return isinstance(got, dict) and all(k in got and got[k] == v for k, v in want.items())


def readback(slot: Slot, p: Plan, body: bytes) -> dict:
    """Re-read what was written; any mismatch raises Failed. D's own bytes
    must hold the entry written. Then the layer is re-resolved as `session
    start` resolves it (settings.sources: template and host, through
    host_dir), and its statusLine must CONTAIN D's entry, every key D sets
    equal: a sub-key D does not set may still come from an earlier file.
    Returns statusLine.<sub> -> file."""
    from sessiontools import settings as st
    data, error = read_settings(slot.settings_path)
    want = p.entry
    if want is not None:
        got = (data or {}).get("statusLine", _MISSING) if error is None else _MISSING
        if got != want:
            seen = "nothing" if got is _MISSING else json.dumps(got, ensure_ascii=False)
            raise Failed(f"install wrote {DROP_IN} statusLine {json.dumps(want, ensure_ascii=False)} "
                         f"but read back {seen}: the status line is NOT installed")
    try:
        values, origins, _locked, _leaves = st.merge(st.sources(TOOL, None))
    except (OSError, st.SettingsError) as e:
        raise Failed(f"install wrote {DROP_IN} but the layer does not resolve ({e}): "
                     "the status line is NOT installed") from None
    resolved = values.get("statusLine", _MISSING)
    ok = _contains(resolved, want) if want is not None else is_ours(resolved, slot.cdir)
    if not ok:
        seen = "nothing" if resolved is _MISSING else json.dumps(resolved, ensure_ascii=False)
        raise Failed(f"session start would resolve statusLine to {seen}, not ours: "
                     "the status line is NOT installed")
    try:
        on_disk = slot.script_path.read_bytes()
        mode = os.stat(slot.script_path).st_mode
    except OSError as e:
        raise Failed(f"install wrote {NAME} but cannot read it back ({e}): "
                     "the status line is NOT installed") from None
    s = judge(on_disk)
    expected = body if p.script else None
    if (expected is not None and on_disk != expected) or not s.kind == "pristine":
        raise Failed(f"install wrote {NAME} (sha256 {sha(body)[:12]}) but read back "
                     f"sha256 {sha(on_disk)[:12]}: the status line is NOT installed")
    if not mode & stat.S_IXUSR:
        raise Failed(f"install wrote {NAME} but it reads back not executable: "
                     "the status line is NOT installed")
    return {k: Path(v) for k, v in origins.items() if k.startswith("statusLine.")}


# --- check ---------------------------------------------------------------------------------

def managed_files() -> list[Path]:
    base = MANAGED_DIRS.get(sys.platform)
    if base is None:
        return []
    files = [base / "managed-settings.json"]
    drop = base / "managed-settings.d"
    try:
        files += sorted(p for p in drop.iterdir()
                        if p.suffix == ".json" and not p.name.startswith("."))
    except OSError:
        pass
    return [f for f in files if f.is_file()]


def project_files(cwd: Path) -> list[Path]:
    return [f for f in (cwd / ".claude" / "settings.local.json", cwd / ".claude" / SETTINGS)
            if f.is_file()]


def claude_report() -> tuple[list[str], bool]:
    """(lines, blocked): what `$CLAUDE_CONFIG_DIR`, else `~/.claude`, holds:
    the dir a bare `claude` reads. Read only: nothing there is ever edited,
    removed, backed up or chmodded. Its
    statusLine and its statusline.sh are reported with what wins where; a
    `disableAllHooks: true` there blocks (the --settings file does not set
    it, so it still applies to a session start seat); an unreadable
    settings.json is said, never X: it is not ours to write."""
    cdir = config_dir()
    slot = read_slot(cdir)
    where = shown(slot.settings_path)
    lines: list[str] = []
    blocked = False
    if slot.error:
        lines.append(f"claude: {where} is unreadable ({slot.error}); not ours to fix")
    elif slot.has_key:
        # Ours only when the script it runs carries our stamp: Claude Code's own
        # /statusline and its docs use the same `~/.claude/statusline.sh` entry.
        stamped_there = slot.file == "file" and slot.script is not None and (
            slot.script.kind in ("pristine", "edited"))
        mark = (" (sessiontools' own, from an install before the kit layer)"
                if is_ours(slot.entry, cdir) and stamped_there else "")
        lines.append(f"claude: {where} sets statusLine {json.dumps(slot.entry, ensure_ascii=False)}{mark}: "
                     "a session start seat shows the kit's; a bare claude shows this one. "
                     "Left as it is.")
    if slot.settings and slot.settings.get("disableAllHooks") is True:
        blocked = True
        lines.append(f"claude: {where} sets disableAllHooks: no status line runs, a session start "
                     "seat's included (its --settings file does not set it)")
    path, s = shown(slot.script_path), slot.script
    if slot.file == "link":
        what = f"a symlink to {slot.link_target}"
    elif slot.file == "other":
        what = "not a file or a symlink"
    elif s is not None and s.kind == "pristine":
        what = f"pristine sessiontools {s.version}, from an install before the kit layer"
    elif s is not None and s.kind == "edited":
        what = f"edited (from sessiontools {s.version})"
    elif s is not None:
        what = s.known or "not ours"
    else:
        what = None
    if what:
        lines.append(f"claude: {path}: {what}. Left as it is.")
    return lines, blocked


def check(diff: bool = False, cwd: Path | None = None) -> int:
    """Say what is there: the kit layer (K, read as session start reads
    it), what overrides it, and what `~/.claude` holds (claude_report).
    Exit 0: ours is installed (pristine or edited) and nothing visible
    blocks it; 1: it is not, or something does; 5: a kit settings file is
    unreadable. A `~/.claude` status line never makes it 1."""
    kdir = kit_dir()
    slot = read_kit(kdir)
    state = classify(slot)
    ver = version()
    healthy = state in ("C", "U", "D") and slot.file == "file" and (
        slot.script is not None and slot.script.kind != "foreign")
    _out(f"session statusline check: {shown(kdir)}")
    _out(f"  state {state}: {STATES[state]}")
    sys.stdout.flush()
    from sessiontools import settings as st
    st.say_stranded(TOOL)
    if slot.error:
        _out(f"  settings: {shown(slot.error_path or slot.settings_path)} is unreadable ({slot.error})")
        return UNREADABLE
    if not slot.has_key:
        why = "does not exist" if slot.settings is None else "has no statusLine"
        _out(f"  settings: no kit file sets statusLine ({shown(slot.settings_path)} {why})")
    else:
        mark = "ours" if is_ours(slot.entry, kdir) else "not ours"
        line = (f"  settings: statusLine = {json.dumps(slot.entry, ensure_ascii=False)} ({mark}), "
                f"set in {shown(slot.owner)}")
        if mark == "not ours" and slot.order > 0:
            line += f", which is read after {DROP_IN}, so it wins"
        _out(line)
        for sub, f in borrowed(slot.origins, slot.owner):
            _out(f"  settings: {sub} comes from {shown(f)}")
        applied = set(slot.origins.values())
        for f, _value in slot.setters:
            if f not in applied:
                _out(f"  settings: {shown(f)} also sets statusLine; none of it applies")
    s = slot.script
    path = shown(slot.script_path)
    if slot.file == "none":
        _out(f"  script: {path} does not exist")
    elif slot.file == "link":
        _out(f"  script: {path} is a symlink to {slot.link_target}")
    elif slot.file == "other":
        _out(f"  script: {path} is not a file or a symlink")
    elif s is not None and s.kind == "pristine":
        if s.current:
            _out(f"  script: {path}: pristine, sessiontools {s.version}, current")
        else:
            _out(f"  script: {path}: pristine, sessiontools {s.version}; this sessiontools "
                 f"ships {ver}: `session statusline install` upgrades it")
    elif s is not None and s.kind == "edited":
        _out(f"  script: {path}: edited (from sessiontools {s.version}); "
             "`session statusline check --diff` shows the edits")
    elif s is not None:
        _out(f"  script: {path}: not ours" + (f": {s.known}" if s.known else ""))

    blocked = False
    for f in managed_files():
        data, err = read_settings(f)
        if err or data is None:
            _out(f"  managed: {shown(f)} is unreadable ({err})")
            continue
        if data.get("allowManagedHooksOnly") is True:
            blocked = True
            _out(f"  managed: {shown(f)} sets allowManagedHooksOnly: a custom status line "
                 "does not run on this machine (Claude Code drops it without warning)")
        if "statusLine" in data:
            blocked = True
            _out(f"  managed: {shown(f)} sets statusLine, which wins over yours: "
                 f"{json.dumps(data['statusLine'], ensure_ascii=False)}")
        if data.get("disableAllHooks") is True:
            blocked = True
            _out(f"  managed: {shown(f)} sets disableAllHooks: no status line runs")
    for f in project_files(cwd or Path.cwd()):
        data, err = read_settings(f)
        if data and "statusLine" in data:
            _out(f"  project: {shown(f)} sets statusLine: it wins over ~/.claude's for a bare "
                 f"claude here; a session start seat shows the kit's: "
                 f"{json.dumps(data['statusLine'], ensure_ascii=False)}")
        if data and data.get("disableAllHooks") is True:
            blocked = True
            _out(f"  project: {shown(f)} sets disableAllHooks: no status line runs here")
    report, claude_blocked = claude_report()
    blocked = blocked or claude_blocked
    for line in report:
        _out(f"  {line}")
    if not shutil.which("jq"):
        _out("  jq: not on PATH; the line reads `statusline: jq not found`")
    _out("  a --settings typed on a launch's command line is folded on top of this (its keys "
         "win, except a key a lower file locks with _locked); one in the agent file's "
         "claude: section gets none of this; an agent, seat or session layer may override "
         "it: session settings SID claude-code shows a seat's")
    _out("  not visible here: MDM or server-managed settings; the line also needs workspace trust")
    if diff and slot.file == "file" and s is not None:
        _out(diff_text(s, path))
    return OK if healthy and not blocked else 1


def diff_text(s: Script, path: str) -> str:
    import difflib
    ours = shipped().decode("utf-8", "replace").splitlines(keepends=True)
    theirs = s.body.decode("utf-8", "replace").splitlines(keepends=True)
    out = "".join(difflib.unified_diff(ours, theirs, f"sessiontools {version()} statusline.sh",
                                       f"{path} (stamp line dropped)"))
    return out.rstrip("\n") if out else "  diff: no edits against the shipped script"


def sample() -> int:
    sys.stdout.write(SAMPLE.read_text(encoding="utf-8"))
    sys.stdout.flush()
    return OK


# --- the command line ---------------------------------------------------------------------

SUBVERBS = (
    ("install", ("copy the script into the kit's claude-code layer and set statusLine there "
                 "(--dry-run, --force)")),
    ("check", ("say what the kit layer holds, what is edited, what overrides it, and what "
               "~/.claude holds, read only (--diff)")),
    ("sample", ("print the sample payload: "
                "session statusline sample | ~/.sessionkit/tools/settings/statusline.sh")),
)


def usage() -> str:
    rows = "\n".join(f"  {v:9} {d}" for v, d in SUBVERBS)
    return ("Usage: session statusline <install|check|sample> [options]\n\n"
            f"Claude Code's status line, from sessiontools.\n\n{rows}\n\n"
            "A seat launched by `session start` shows it; a bare claude does not.\n"
            "To get it in a plain claude: alias claude='session start -'\n")


def _refuse_unreadable(verb: str, run: Callable[[], int]) -> int:
    """RUN's exit code. A settings path the reader cannot look at
    (settings.SettingsUnreadable: a kit folder that lists but cannot be
    searched, say) is refused as UNREADABLE, naming the path, never a
    traceback. Both verbs stat the kit layer before anything is written, so
    install has written nothing when this refuses."""
    from sessiontools import settings as st
    try:
        return run()
    except st.SettingsUnreadable as e:
        _err(f"session statusline {verb}: refused: {e}")
        if verb == "install":
            _err("Nothing written, --force included: fix or move it first.")
        return UNREADABLE


def _install_parser():
    """`session statusline install`'s parser."""
    import argparse
    p = argparse.ArgumentParser(prog="session statusline install",
                                description="Copy the status-line script to "
                                            "~/.sessionkit/tools/settings/ and set "
                                            "statusLine in its claude-code.d/"
                                            "50-statusline.json, which session start "
                                            "passes to claude. ~/.claude is never "
                                            "written. The plan prints first; an existing "
                                            "status line is never replaced without "
                                            "--force.")
    p.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    p.add_argument("--force", action="store_true",
                   help="replace an existing status line, backing it up first")
    return p


def _check_parser():
    """`session statusline check`'s parser."""
    import argparse
    p = argparse.ArgumentParser(prog="session statusline check",
                                description="What status line the kit layer "
                                            "(~/.sessionkit/tools/settings/) holds, "
                                            "whether it is edited, and what overrides it. "
                                            "~/.claude (or $CLAUDE_CONFIG_DIR) is reported, "
                                            "never written: a bare claude shows its line, "
                                            "a session start seat the kit's.")
    p.add_argument("--diff", action="store_true",
                   help="show the kit copy's edits against the shipped script")
    return p


def _sample_parser():
    """`session statusline sample`'s parser: no options."""
    import argparse
    return argparse.ArgumentParser(prog="session statusline sample",
                                   description="Print the sample payload (stdin JSON).")


#: `session statusline`: every sub-verb is dispatched by hand from the first
#: word, one parser each, named by SUBVERBS. `main` reads the parser from here.
GROUP = Group(subs={name: f"sessiontools.statusline:_{name}_parser" for name, _ in SUBVERBS})


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(usage())
        return OK
    sub, rest = argv[0], argv[1:]
    if sub not in GROUP.subs:
        _err(f"session statusline: unknown subcommand {sub!r}\n")
        sys.stderr.write(usage())
        return USAGE
    p = resolve(GROUP.subs[sub])()
    if sub == "install":
        a = p.parse_args(rest)
        return _refuse_unreadable("install", lambda: install(dry_run=a.dry_run, force=a.force))
    if sub == "check":
        a = p.parse_args(rest)
        return _refuse_unreadable("check", lambda: check(diff=a.diff))
    p.parse_args(rest)
    return sample()
