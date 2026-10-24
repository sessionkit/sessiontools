"""Settings: the cascade every tool reads its settings through, and the one
reader that computes it - `session settings <sid> [tool]`.

The tree and the cascade are settled; the merge rule and this reader are
proposed. No configuration library: the merge is hand-rolled and the
schema check is jsonschema, by design. `import sessiontools.settings` reaches nothing outside the standard
library; PyYAML (the agent layer) and jsonschema are imported only inside
the functions that need them.

THE LAYERS, top to bottom; each overrides the one above it:

    template  <pkg>/<tool>.template.json                 shipped
    host      <pkg>/<tool>.json | <tool>.d/*.json,
              or the file SETTINGS_FILE_ENV names (see host_files)
    agent     ~/.session/agent/config/<name>.md, front matter `settings:`
    seat      ~/.session/seats/<seat>/settings/<tool>.json | <tool>.d/
    session   ~/.session/sessions/<sid>/settings/<tool>.json | <tool>.d/

`<pkg>` is `~/.sessionkit/<package>/settings/`, the settings directory of
the package that ships the tool (`~/.sessionkit/tools/settings/session.json`):
PACKAGES maps each tool (a command) to its package, and package_dir is the
ONE place the directory's shape is spelled; host_dir joins the two. There
is no other host directory, no fallback and no variable that moves it.
`from sessiontools.settings import PACKAGES, package_dir, host_dir` is the
public way in for any other reader or writer.

In one layer `<tool>.json` is read first, then `<tool>.d/*.json` in name
order. THE MERGE: a map merges key by key, recursively; any
other value, a list included, replaces the one above it. `_locked` in any
file is a list of dotted paths; a locked path ignores that key in every
layer below the file that locked it. The environment is NOT a layer: the
birth writes it into the session layer once (`<tool>.d/50-env.json`).

THE READER READS NEW PATHS ONLY. A tool whose
files have not moved has no host, seat or session source here, and
`session settings` says "not moved" for it. A tool whose host files are
still only at the old path is STRANDED (`stranded`): the reader says so
ONCE per process on stderr (`say_stranded`, never stdout), naming both
folders, and reads and writes nothing old. A hook says nothing (`quiet`).
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

LAYERS = ("template", "host", "agent", "seat", "session")
#: The tools that read their settings through this cascade, named once.
#: `session settings --help` lists them from here (its help was once a
#: hand-typed string that predated `sounds`). sessiontools' own are `session`
#: (snapshot, operator, start) and `sounds` (busy); tests/test_settings.py
#: pins each module constant that names a tool, and each schema the repo
#: ships under settings/, to this tuple. `mail`, `ears`, `mouth` and `phone`
#: belong to other repos, so nothing here can derive them: add a tool's name
#: when it moves onto the cascade.
TOOLS = ("mail", "session", "ears", "mouth", "sounds", "claude-code", "view", "phone")
#: The kit's home under HOME. One constant: no variable or flag moves it.
KIT_DIR = ".sessionkit"
#: Each tool (a command) -> the package that ships it, whose settings
#: directory (package_dir) holds the tool's template, host file, drop-ins
#: and schema. The ONE map: host_dir and every directory scan go through
#: it, never a guess from a directory's name. tests/test_settings.py pins
#: the keys to TOOLS.
PACKAGES = {"mail": "mail", "session": "tools", "ears": "voice", "mouth": "voice",
            "sounds": "tools", "claude-code": "tools", "view": "view", "phone": "phone"}
#: The variable that names a tool's SETTINGS FILE, for the tools whose
#: installer writes the tool's schema beside that file (sessionvoice's
#: `install_schema`, the only writer of dot-named schemas, does so for these
#: two): without the entry, the schema cannot be found whenever the variable
#: is set. An override cannot be learned in general (each tool names its own,
#: and nothing in the settings tree records where a tool was pointed), so the
#: one supported place is host_dir(tool); a tool joins this table
#: when its installer does the same. The same variable names the tool's
#: host-layer file, so the cascade reads that layer from it too (host_files).
#: Each tool's names are read in order, the tool's own rule: the current
#: name, then any old name it still accepts (the mouth's `MOUTH_SETTINGS`,
#: then the old `VOICEMODE_MOUTH_SETTINGS`). Presence, not truthiness, picks
#: the name: a current name set to "" hides the old one (settings_file_variable).
#: tests/test_settings.py pins the keys to TOOLS.
SETTINGS_FILE_ENV = {"ears": ("EARS_CONF",),
                     "mouth": ("MOUTH_SETTINGS", "VOICEMODE_MOUTH_SETTINGS")}
SESSION_DIR = ".session"
#: Keys a layer file may carry that are not settings.
META_KEYS = ("_locked", "_about", "$schema")


class SettingsError(Exception):
    """A layer file the reader refuses; the message names the file and key."""


class SettingsUnreadable(SettingsError, OSError):
    """A settings path the reader cannot look at: it may be there, so it is
    never read as absent. Both a SettingsError (a command refuses it,
    naming the path) and the OSError it was, with the same errno, strerror,
    filename and message, so a caller that caught the OSError up to 3.13
    still catches it."""


class SettingsDenied(SettingsUnreadable, PermissionError):
    """SettingsUnreadable for EACCES / EPERM: the PermissionError that
    climbed out of the reader up to 3.13, now a SettingsError too."""


@dataclass(frozen=True)
class Source:
    layer: str
    path: Path
    data: dict


@dataclass
class Resolved:
    tool: str
    values: dict
    #: leaf, spelled by `_shown` -> the file that set it
    origins: dict[str, str] = field(default_factory=dict)
    #: dotted path -> the file that locked it
    locked: dict[str, str] = field(default_factory=dict)
    sources: list[Source] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: leaf, spelled by `_shown` -> its value, what the report prints
    leaves: dict[str, Any] = field(default_factory=dict)
    #: said, never refused: a recorded agent file that is gone,
    #: two leaves whose paths join to one dotted string, an agent key the
    #: dotted shorthand cut inside brackets or quotes
    notes: list[str] = field(default_factory=list)
    #: leaf, spelled by `_shown` -> its path, one key per segment
    paths: dict[str, tuple] = field(default_factory=dict)

    @property
    def moved(self) -> bool:
        """True once the tool has a file at a NEW host, seat or session path."""
        return any(s.layer in ("host", "seat", "session") for s in self.sources)


# --- roots -----------------------------------------------------------------

def home(env: Mapping[str, str] | None = None) -> Path:
    e = os.environ if env is None else env
    return Path(e.get("HOME") or os.path.expanduser("~"))


def settings_root(env: Mapping[str, str] | None = None) -> Path:
    """DEPRECATED: the settings tree before the kit's home, the ONE place this
    package names it. Kept for callers outside this package; here it is
    used only to say a tool is stranded (`stranded`): never opened for
    content, never written, never a fallback. Removed after the last caller
    moves. Read a tool's settings directory from host_dir."""
    return home(env) / ".settings"


def session_root(env: Mapping[str, str] | None = None) -> Path:
    return home(env) / SESSION_DIR


def kit_root(env: Mapping[str, str] | None = None) -> Path:
    """`~/.sessionkit/`, the kit's home."""
    return home(env) / KIT_DIR


def package_dir(package: str, env: Mapping[str, str] | None = None) -> Path:
    """`~/.sessionkit/<package>/settings/`: PACKAGE's settings directory,
    package first (its state and log sit beside `settings/`). The ONE place
    the shape is spelled: every reader, writer and test derives the path
    from here."""
    return kit_root(env) / package / "settings"


#: `session account`'s folder under its package's home, beside `settings/`
#: and never inside it: `session settings` reads that tree, and this folder
#: holds the token backups.
ACCOUNTS_DIR = "accounts"


def accounts_dir(env: Mapping[str, str] | None = None) -> Path:
    """`~/.sessionkit/tools/accounts/`: the folder `session account` keeps
    its registry, config, ledger and backups in (the account-home move). The ONE place the
    shape is spelled, as package_dir is for settings: the package is
    PACKAGES["session"], never a literal."""
    return kit_root(env) / PACKAGES["session"] / ACCOUNTS_DIR


def host_dir(tool: str, env: Mapping[str, str] | None = None) -> tuple[Path, str | None]:
    """(the directory TOOL's template, host layer and schema are read from,
    None): package_dir of PACKAGES[tool]. The second item is always None;
    the pair is kept so a caller written against it keeps working. A tool
    the map does not name has no directory, never a guessed one:
    SettingsError."""
    directory = _tool_dir(tool, env)
    if directory is None:
        raise SettingsError(f"{tool}: no package ships it (PACKAGES names "
                            f"{', '.join(PACKAGES)}), so it has no settings directory")
    return directory, None


def _tool_dir(tool: str, env: Mapping[str, str] | None = None) -> Path | None:
    """host_dir's directory, or None for a tool PACKAGES does not name."""
    package = PACKAGES.get(tool)
    return None if package is None else package_dir(package, env)


# --- a stranded tool: its host files are still only at the old path ---------

#: The line said when a tool is stranded: formatted with {old} and {new},
#: the two folders as `~` paths. Worded as sessionmail's stranded line is.
STRANDED_LINE = ("sessiontools: the settings in {old} have not been moved, so they were "
                 "not read and nothing was written. Move the files in {old} to {new}, "
                 "then re-run.")
#: The tools this process has said the stranded line for: once each.
_said_stranded: set[str] = set()
#: Above zero inside `quiet`: no stranded line is said.
_quiet = 0


#: The errnos `Path.is_file()` / `is_dir()` / `exists()` read as ABSENT up to
#: 3.13: no such entry, a file where a folder should be, a bad file
#: descriptor, a symlink loop. Every other OSError raised (reap._ABSENT).
_ABSENT = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})


def _unreadable(error: OSError, path: Path) -> SettingsUnreadable:
    """ERROR, raised at PATH, as the SettingsUnreadable to raise."""
    kind = SettingsDenied if error.errno in (errno.EACCES, errno.EPERM) else SettingsUnreadable
    return kind(error.errno, error.strerror, error.filename or str(path))


def _stat_mode(path: Path) -> int | None:
    """PATH's `st_mode`, symlinks followed, or None where it is ABSENT (an
    errno in _ABSENT, or a path the OS cannot take). Any other OSError
    RAISES SettingsUnreadable (SettingsDenied for EACCES / EPERM) naming
    the path, on every version. THE strict stat of this module: from 3.14
    `Path.is_file()` / `is_dir()` / `exists()` return False for a path they
    cannot stat, so a settings folder that lists but cannot be searched read
    as one with no file, and a set value fell to its default without a
    word. Every check that must tell "absent" from "cannot look" asks here
    (_is_file, _is_dir)."""
    try:
        return path.stat().st_mode
    except OSError as e:
        if e.errno in _ABSENT:
            return None
        raise _unreadable(e, path) from e
    except ValueError:
        return None


def _is_file(path: Path) -> bool:
    """`path.is_file()` as 3.13 answered it, on every version (_stat_mode)."""
    mode = _stat_mode(path)
    return mode is not None and stat.S_ISREG(mode)


def _is_dir(path: Path) -> bool:
    """`path.is_dir()` as 3.13 answered it, on every version (_stat_mode)."""
    mode = _stat_mode(path)
    return mode is not None and stat.S_ISDIR(mode)


def _entries(directory: Path) -> list[Path]:
    """`directory.iterdir()`, a folder that cannot be listed raised as
    SettingsUnreadable naming it, as _stat_mode raises; one that is gone
    raises as it always did."""
    try:
        return list(directory.iterdir())
    except OSError as e:
        if e.errno in _ABSENT:
            raise
        raise _unreadable(e, directory) from e


def _read_text(path: Path) -> str:
    """PATH's text: a file that cannot be read raised as SettingsUnreadable
    naming it, as _stat_mode raises; one that is gone raises as it always
    did."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        if e.errno in _ABSENT:
            raise
        raise _unreadable(e, path) from e


def _has_host(directory: Path, tool: str) -> bool:
    """Does DIRECTORY hold a host file for TOOL, `<tool>.json` or
    `<tool>.d/`? Kinds only (a stat each): nothing is opened."""
    return _is_file(directory / f"{tool}.json") or _is_dir(directory / f"{tool}.d")


def stranded(tool: str, env: Mapping[str, str] | None = None) -> bool:
    """True when TOOL's host files are still only at the old path: none in
    host_dir(tool), and `<tool>.json` or `<tool>.d/` under
    `settings_root()/<tool>/`. The old files are never opened, read,
    written or used as a fallback: only their kind is checked. A tool
    PACKAGES does not name is never stranded."""
    new = _tool_dir(tool, env)
    if new is None or _has_host(new, tool):
        return False
    return _has_host(settings_root(env) / tool, tool)


def stranded_line(tool: str, env: Mapping[str, str] | None = None) -> str:
    """STRANDED_LINE for TOOL: its old folder and its new one, as `~` paths."""
    old = _short(str(settings_root(env) / tool), env) + "/"
    new = _short(str(host_dir(tool, env)[0]), env) + "/"
    return STRANDED_LINE.format(old=old, new=new)


def say_stranded(tool: str, env: Mapping[str, str] | None = None) -> bool:
    """THE one place the stranded line is said: on stderr, never stdout, at
    most once per tool per process, and never inside `quiet`. Returns
    whether TOOL is stranded, said or not, so a writer can refuse."""
    if not stranded(tool, env):
        return False
    if not _quiet and tool not in _said_stranded:
        _said_stranded.add(tool)
        print(stranded_line(tool, env), file=sys.stderr, flush=True)
    return True


@contextmanager
def quiet() -> Iterator[None]:
    """No stranded line is said inside: a hook's process (busy-sound) is
    SILENT whatever it reads, the fail-silent contract."""
    global _quiet
    _quiet += 1
    try:
        yield
    finally:
        _quiet -= 1


def _host_files(directory: Path, tool: str) -> list[Path]:
    """The template and host-layer files DIRECTORY holds for TOOL."""
    template = directory / f"{tool}.template.json"
    return ([template] if _is_file(template) else []) + layer_files(directory, tool)


def _same_files(a: list[Path], b: list[Path]) -> bool:
    """Is every file in B one of A's, once symlinks are followed?"""
    real = {p.resolve() for p in a}
    return all(p.resolve() in real for p in b)


def settings_file_variable(tool: str, env: Mapping[str, str] | None = None) -> str | None:
    """The name of TOOL's settings-file variable that is read: the first of
    its SETTINGS_FILE_ENV names that is set in ENV (presence, not
    truthiness: a current name set to "" hides an old one), else its
    current name. None for a tool not in the table. A message that names
    the variable names this one, the one actually read."""
    names = SETTINGS_FILE_ENV.get(tool)
    if not names:
        return None
    e = os.environ if env is None else env
    return next((n for n in names if n in e), names[0])


def settings_file_override(tool: str, env: Mapping[str, str] | None = None) -> Path | None:
    """The file TOOL's settings-file variable (settings_file_variable) names,
    or None: a tool not in the table, or no name of it set, or the name read
    set to "" (an empty value is no override, and an old name it hides is
    not read). `~` expands against the HOME of ENV. The one read of the
    variable: the schema lookup and the host layer both go through here, so
    they cannot disagree on it."""
    variable = settings_file_variable(tool, env)
    if not variable:
        return None
    named = (os.environ if env is None else env).get(variable) or ""
    if not named:
        return None
    return Path(_expand_tilde(named, env))


class HostFiles(NamedTuple):
    """TOOL's template file (or None), its host-layer files in read order,
    and the notes about what was not read."""

    template: Path | None
    host: list[Path]
    notes: list[str]


def host_files(tool: str, env: Mapping[str, str] | None = None) -> HostFiles:
    """Where TOOL's template and host layer are read from.

    With an override F (settings_file_override), the tool's own settings
    file: the host layer is F itself when it is a file, whatever its name,
    then `<F's dir>/<tool>.d/*.json` in name order; the template is
    `<F's dir>/<tool>.template.json`. host_dir(tool) is NOT read:
    an override beats the default, never a stack, so the cascade reports
    the file the tool reads and no other. When the default directory still
    holds files the override does not reach, a note names both; F set but
    not a file is no host-layer file and no fallback, and a note says so.

    The schema keeps its own rule (schema_candidates: beside F, then
    host_dir(tool)), deliberately: a schema describes the tool, not one
    settings file.

    Without an override: host_dir's directory, no note; with no host file
    there, a stranded tool is said on stderr (say_stranded) and nothing old
    is read. A tool PACKAGES does not name has no host directory: no
    template, no host layer, a note."""
    override = settings_file_override(tool, env)
    if override is None:
        directory = _tool_dir(tool, env)
        if directory is None:
            return HostFiles(None, [], [(f"{tool}: no package ships it (PACKAGES), so no "
                                         f"template or host layer is read")])
        template = directory / f"{tool}.template.json"
        host = layer_files(directory, tool)
        if not host:
            say_stranded(tool, env)
        return HostFiles(template if _is_file(template) else None, host, [])
    parent = override.parent
    template = parent / f"{tool}.template.json"
    drop_ins = parent / f"{tool}.d"
    host = [override] if _is_file(override) else []
    if _is_dir(drop_ins):
        host.extend(sorted(p for p in _entries(drop_ins) if _is_file(p) and p.suffix == ".json"))
    found = HostFiles(template if _is_file(template) else None, host, [])
    variable = settings_file_variable(tool, env)
    default = _tool_dir(tool, env)
    if not _is_file(override):
        found.notes.append(f"{tool}: ${variable} names {override}, which is not a file: "
                           f"no host-layer file is read from it, and {default} is not "
                           f"read in its place")
    old_files = _host_files(default, tool) if default is not None else []
    reached = ([found.template] if found.template else []) + found.host
    if old_files and not _same_files(reached, old_files):
        found.notes.append(f"{tool}: {default} is shadowed by ${variable} ({override}) "
                           f"and not read (the tool reads the file its variable names; "
                           f"move or remove the old files)")
    return found


#: Moves every session dir: tm sets it to `~/.taskmaster/sessions` for its
#: crews (task 4.1b drops that). The launcher and this reader read the SAME
#: variable, so a launch and `session settings` agree on the directory.
SESSIONS_DIR_ENV = "SESSIONS_DIR"
#: An ambiguous short id names at most this many candidates, then the total
#: (a one-letter prefix on a busy host named ~100 ids, about 3.8 KB).
AMBIGUOUS_SHOW = 10


def sessions_root(env: Mapping[str, str] | None = None) -> Path:
    """`$SESSIONS_DIR`, else `~/.session/sessions`: the one root under which
    every session's dir is made (task 4.1), whatever the agent."""
    e = os.environ if env is None else env
    override = e.get(SESSIONS_DIR_ENV)
    if override:
        return Path(os.path.expanduser(override))
    return session_root(env) / "sessions"


def session_dir(sid: str, env: Mapping[str, str] | None = None) -> Path:
    return sessions_root(env) / sid


def resolve_sid(sid: str, env: Mapping[str, str] | None = None) -> tuple[str, str | None]:
    """(full id, note) for a session id that may be a SHORT prefix.

    `session_dir` is a literal join, so a short id named no directory and the
    agent and seat layers vanished without a word. Here: a full id whose dir
    exists is itself; else a case-insensitive prefix over the directory names
    under `sessions_root`, where exactly one match is that full id and more
    than one REFUSES (SettingsError naming them: never guess; over
    AMBIGUOUS_SHOW it names that many and the total). No match keeps
    the id and returns a note to say so; it is not refused, because a session
    with no dir still has its host and template layers."""
    root = sessions_root(env)
    if _is_dir(root / sid):
        return sid, None
    try:
        names = [d.name for d in root.iterdir() if _is_dir(d)]
    except OSError:
        names = []
    low = sid.lower()
    hits = sorted(n for n in names if n.lower().startswith(low))
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > AMBIGUOUS_SHOW:
        raise SettingsError(f"session id {sid} is ambiguous under {root}: "
                            f"{', '.join(hits[:AMBIGUOUS_SHOW])}, ...\n"
                            f"{len(hits)} sessions match {sid}, the first "
                            f"{AMBIGUOUS_SHOW} shown; name more of it")
    if hits:
        raise SettingsError(f"session id {sid} is ambiguous under {root}: "
                            f"{', '.join(hits)}; name more of it")
    return sid, (f"no session directory for {sid} under {root}: its session layer, "
                 f"and the agent and seat it records, are not read")


def seat_dir(seat: str, env: Mapping[str, str] | None = None) -> Path:
    return session_root(env) / "seats" / seat


def agent_config(agent: str, env: Mapping[str, str] | None = None) -> Path:
    """The agent's MD under `agent/config/`: `<name>/<name>.md` beside its
    extras (avatar, portrait, letter, tour) when that directory exists,
    else the flat `<name>.md`. The directory layout wins, as the launcher's
    does."""
    config = session_root(env) / "agent" / "config"
    if _is_dir(config / agent):
        return config / agent / f"{agent}.md"
    return config / f"{agent}.md"


def agent_home(agent: str, env: Mapping[str, str] | None = None) -> Path:
    return session_root(env) / "agent" / "home" / agent


def ensure_private_dir(path: str | Path) -> Path:
    """`path` as a 0700 directory holding a `.gitignore` of `*`, made by
    whichever writer gets there first and left alone when it is there.
    For state under a tree that is a git repo and must never reach it: a
    session's `launch/` (the launcher's prompt file and secret file) and
    its `mail/` (the pager's key, ride, stamps and marks). The parent must
    exist; this never makes a session dir (FileNotFoundError if it is
    missing). Returns the path."""
    directory = Path(path)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    os.chmod(directory, 0o700)
    try:
        fd = os.open(directory / ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return directory
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write("*\n")
    return directory


# --- files -----------------------------------------------------------------

def read_json_object(path: Path) -> dict:
    try:
        data = json.loads(_read_text(path))
    except json.JSONDecodeError as e:
        raise SettingsError(f"{path}: not JSON ({e.msg}, line {e.lineno})") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: not a JSON object")
    return data


def layer_files(directory: Path, tool: str) -> list[Path]:
    """`<dir>/<tool>.json` first, then `<dir>/<tool>.d/*.json` in name order."""
    out: list[Path] = []
    one = directory / f"{tool}.json"
    if _is_file(one):
        out.append(one)
    d = directory / f"{tool}.d"
    if _is_dir(d):
        out.extend(sorted(p for p in _entries(d) if _is_file(p) and p.suffix == ".json"))
    return out


# --- the agent layer: front matter -------------------------------------------

def expand_dotted(settings: Mapping[str, Any], where: str) -> dict:
    """The front matter shorthand: `mouth.voice: x` is `{mouth: {voice: x}}`.

    Expansion runs down a TOP-LEVEL key until it reaches a map, and never
    inside one: a seat or box name that is a map key stays whole
    (`myproject+task-7.worker`). A path set both ways is refused, and the
    error names the key.
    """
    out: dict = {}
    for key, value in settings.items():
        parts = str(key).split(".")
        node = out
        for part in parts[:-1]:
            cur = node.get(part)
            if cur is None:
                cur = node[part] = {}
            elif not isinstance(cur, dict):
                raise SettingsError(f"{where}: {key} set both dotted and nested")
            node = cur
        last = parts[-1]
        if last in node:
            if isinstance(node[last], dict) and isinstance(value, dict):
                _merge_strict(node[last], value, where, key)
            else:
                raise SettingsError(f"{where}: {key} set both dotted and nested")
        else:
            node[last] = value
    return out


def _merge_strict(into: dict, more: Mapping, where: str, key: str) -> None:
    for k, v in more.items():
        if k in into:
            if isinstance(into[k], dict) and isinstance(v, dict):
                _merge_strict(into[k], v, where, f"{key}.{k}")
            else:
                raise SettingsError(f"{where}: {key}.{k} set both dotted and nested")
        else:
            into[k] = v


def agent_settings(agent: str, env: Mapping[str, str] | None = None,
                   file: str | Path | None = None) -> tuple[Path, dict] | None:
    """The `settings:` map of the agent's MD, expanded; None when the MD or
    the map is absent. The MD is FILE when given (the one the launch used),
    else `agent/config/`. The only place this module touches YAML."""
    path = Path(file) if file else agent_config(agent, env)
    if not _is_file(path):
        return None
    from sessiontools.start.agentfile import parse_agent_markdown  # PyYAML lives there
    front, _body = parse_agent_markdown(_read_text(path))
    raw = front.get("settings") if isinstance(front, dict) else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SettingsError(f"{path}: settings: is not a map")
    return path, expand_dotted(raw, str(path))


# --- sources -----------------------------------------------------------------

def recorded_agent_file(sid: str, env: Mapping[str, str] | None = None) -> str | None:
    """The agent file this session was launched from, as its birth recorded
    it in the session layer's `session.json` (`agent_file`); None for a
    session born before births recorded it, or for a bare session."""
    found = None
    for p in layer_files(session_dir(sid, env) / "settings", "session"):
        value = read_json_object(p).get("agent_file")
        if isinstance(value, str) and value:
            found = value
    return found


def agent_file_for(agent: str, sid: str | None, env: Mapping[str, str] | None = None,
                   agent_file: str | None = None) -> tuple[str | None, str | None]:
    """(the file the agent layer is read from, or None for `agent/config/`;
    a problem line, or None). The launcher's file, else the recorded one.
    A recorded file that is gone falls back to `agent/config/` and SAYS so,
    naming it: never silently."""
    file = agent_file or (recorded_agent_file(sid, env) if sid else None)
    if file and not _is_file(Path(file)):
        return None, (f"agent_file {file} is gone; the agent layer is read from "
                      f"{agent_config(agent, env)} instead")
    return file, None


def sources(tool: str, sid: str | None, agent: str | None = None, seat: str | None = None,
            env: Mapping[str, str] | None = None,
            agent_file: str | None = None) -> list[Source]:
    """Every file that feeds TOOL for this session, in cascade order. `sid`
    None (a session not yet born: a pane-minted launch) has no session
    layer. The agent layer is read from AGENT_FILE (the launcher passes the
    file it resolved), else the file the birth recorded, else
    `agent/config/`."""
    out: list[Source] = []
    found = host_files(tool, env)
    if found.template is not None:
        out.append(Source("template", found.template, read_json_object(found.template)))
    for p in found.host:
        out.append(Source("host", p, read_json_object(p)))
    if agent:
        file, _gone = agent_file_for(agent, sid, env, agent_file)
        found = agent_settings(agent, env, file)
        if found is not None:
            path, expanded = found
            section = expanded.get(tool)
            if isinstance(section, dict):
                out.append(Source("agent", path, section))
    if seat:
        for p in layer_files(seat_dir(seat, env) / "settings", tool):
            out.append(Source("seat", p, read_json_object(p)))
    if sid:
        for p in layer_files(session_dir(sid, env) / "settings", tool):
            out.append(Source("session", p, read_json_object(p)))
    return out


# --- the merge ---------------------------------------------------------------

def _leaves(data: Mapping, prefix: tuple = ()) -> list[tuple[tuple, Any]]:
    """Every leaf of DATA with its path. An EMPTY map is no leaf: under
    "maps merge key by key" it adds nothing and clears nothing (a review
    finding: a converter writing `"subscriptions": {}` must not
    drop the host's)."""
    out: list[tuple[tuple, Any]] = []
    for k, v in data.items():
        path = prefix + (str(k),)
        if isinstance(v, dict):
            out.extend(_leaves(v, path))
        else:
            out.append((path, v))
    return out


def _joined(path: tuple) -> str:
    return ".".join(path)


def _shown(path: tuple) -> str:
    """PATH as the report spells it: segments joined by `.`, a segment
    written as a JSON string when it holds a `.` or a `"`, or is empty.
    An unquoted `.` is always a separator, so no two paths are spelled
    alike, where `_joined` gives `("a.b",)` and `("a", "b")` one string."""
    return ".".join(json.dumps(seg, ensure_ascii=False) if not seg or "." in seg or '"' in seg
                    else seg for seg in path)


def _unflatten(leaves: Mapping[tuple, Any]) -> dict:
    out: dict = {}
    for path, value in leaves.items():
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return out


def _under_lock(joined: str, locked: Mapping[str, str]) -> str | None:
    """The file that locks the leaf JOINED ("a.b.c"): a lock equal to it, on
    an ancestor of it, or on a descendant (a scalar here would wipe the
    locked entry). Matched at dot boundaries on the JOINED string, never by
    splitting: a map key may itself carry a dot (`super.sessionmail`, 71 of
    112 seat names, found in review)."""
    for lock, who in locked.items():
        if joined == lock or joined.startswith(lock + ".") or lock.startswith(joined + "."):
            return who
    return None


def _fold(srcs: list[Source]) -> tuple[dict[tuple, Any], dict[tuple, str], dict[str, str]]:
    """Fold SRCS top to bottom: each leaf's value and origin by its path,
    one key per segment, and the file that locks each dotted path."""
    leaves: dict[tuple, Any] = {}
    origins: dict[tuple, str] = {}
    locked: dict[str, str] = {}
    for src in srcs:
        data = {k: v for k, v in src.data.items() if k not in META_KEYS}
        for path, value in _leaves(data):
            if _under_lock(_joined(path), locked):
                continue
            for p in [p for p in leaves if p[:len(path)] == path or path[:len(p)] == p]:
                del leaves[p]
                del origins[p]
            leaves[path] = value
            origins[path] = str(src.path)
        locks = src.data.get("_locked") or []
        if not isinstance(locks, list) or not all(isinstance(x, str) for x in locks):
            raise SettingsError(f"{src.path}: _locked is not a list of dotted paths")
        for lp in locks:
            locked.setdefault(lp, str(src.path))
    return leaves, origins, locked


def merge(srcs: list[Source]) -> tuple[dict, dict[str, str], dict[str, str], dict[str, Any]]:
    """Fold SRCS top to bottom: values, origin per leaf, lock per path, and
    the leaf values. Origins and leaf values are keyed by the JOINED path,
    which callers outside this module look up (`subscriptions.<box>`), so
    two leaves whose paths join alike share one key here; `resolve` keeps
    them apart."""
    leaves, origins, locked = _fold(srcs)
    return (_unflatten(leaves),
            {_joined(p): f for p, f in origins.items()},
            dict(locked),
            {_joined(p): v for p, v in leaves.items()})


# --- the schema --------------------------------------------------------------

def schema_candidates(tool: str, env: Mapping[str, str] | None = None) -> list[Path]:
    """Every file TOOL's schema may be read from, first choice first: the dot
    name `.<tool>.schema.json` (what the installer writes, so the installed copy),
    then the visible `<tool>.schema.json`; in the directory of the tool's
    settings file when SETTINGS_FILE_ENV names its variable and it is set
    (unset or empty is no override), then in host_dir(tool) (none for a tool
    PACKAGES does not name). A directory reached twice is listed once."""
    dirs: list[Path] = []
    override = settings_file_override(tool, env)
    if override is not None:
        dirs.append(override.parent)
    default = _tool_dir(tool, env)
    if default is not None:
        dirs.append(default)
    out: list[Path] = []
    for directory in dirs:
        for name in (f".{tool}.schema.json", f"{tool}.schema.json"):
            if directory / name not in out:
                out.append(directory / name)
    return out


def find_schema(tool: str,
                env: Mapping[str, str] | None = None) -> tuple[Path | None, list[Path]]:
    """(the schema TOOL is read from, or None; every other candidate that is
    a file, not read). The first candidate that is a file wins and is the
    only one read, never a merge; a file reached under two names (a symlink)
    is one. Every reader goes through here, so listing, birth and checking
    cannot disagree."""
    found = [p for p in schema_candidates(tool, env) if _is_file(p)]
    if not found:
        return None, []
    read = found[0]
    return read, [p for p in found[1:] if p.resolve() != read.resolve()]


def schema_notes(tool: str, env: Mapping[str, str] | None = None) -> list[str]:
    """One line per schema file found for TOOL and not read (find_schema)."""
    read, not_read = find_schema(tool, env)
    return [f"{tool}: schema {other} is shadowed by {read} and not read "
            f"(the installed copy is read first; remove the other)" for other in not_read]


def schema_path(tool: str, env: Mapping[str, str] | None = None) -> Path:
    """The schema TOOL is read from (find_schema); with none, the visible
    name in host_dir(tool), a path that is not a file. A tool PACKAGES does
    not name has no schema path: SettingsError."""
    read, _not_read = find_schema(tool, env)
    if read is not None:
        return read
    return host_dir(tool, env)[0] / f"{tool}.schema.json"


def check_schema(tool: str, values: dict, origins: Mapping[tuple, str],
                 env: Mapping[str, str] | None = None) -> list[str]:
    """Problems with VALUES against the tool's shipped schema, each naming the
    key and the file that set it. ORIGINS is keyed by path, one key per
    segment. Empty when there is no schema."""
    path, _not_read = find_schema(tool, env)
    if path is None:
        return []
    try:
        import jsonschema
    except ImportError:
        return [f"schema not checked: jsonschema is not installed ({path})"]
    schema = read_json_object(path)
    validator = jsonschema.Draft202012Validator(schema)
    out: list[str] = []
    for err in sorted(validator.iter_errors(values), key=lambda e: list(e.absolute_path)):
        path = tuple(str(x) for x in err.absolute_path)
        if not path:
            out.append(f"(root): {err.message} (?)")
            continue
        who = origins.get(path) or next((f for k, f in origins.items() if k[:len(path)] == path), "?")
        out.append(f"{_shown(path)}: {err.message} ({who})")
    return out


# --- the environment at birth -------------------------------------------------
#
# Each tool's INSTALLED schema names its own variables, one `x-env` on the key
# the variable sets; the key's `type` says how the string is read. The birth
# walks the installed schemas; a variable no schema names is not a setting
# and is not read. The level is the session layer, one
# `<tool>.d/50-env.json` per tool, each variable logged by name.

X_ENV = "x-env"
ENV_DROP_IN = "50-env.json"
SETTINGS_LOG = "settings.jsonl"
TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off")
BIRTH_PROG = "session start: settings at birth"
#: How the launch script runs `birth_main`: pinned interpreter, `-I -c`, as
#: the launch record does (record.ENTRY says why not `-m`).
BIRTH_ENTRY = "import sys; from sessiontools.settings import birth_main; sys.exit(birth_main())"

# The schema install. The birth reads only INSTALLED schemas, and a tool
# that installs its schema from a hook does so after the vendor starts,
# which is too late for the first session on a fresh HOME. So the launch
# script runs the tool's own install command first, in the environment the
# birth then reads. One tool needs it today: mail, whose `pager` has the
# verb. A release without the verb fails with a usage error; that, and any
# other failure, is logged and never stops a launch.

#: The command, argv form; the launch script runs it only when its first
#: word is on PATH.
SCHEMA_INSTALL = ("pager", "settings", "install")
SCHEMA_INSTALL_PROG = "session start: schema install"
#: How the launch script runs `schema_install_main`, as BIRTH_ENTRY.
SCHEMA_INSTALL_ENTRY = ("import sys; from sessiontools.settings import schema_install_main; "
                        "sys.exit(schema_install_main())")
#: Seconds before the command is killed. It takes a fraction of a second;
#: this bounds a program that hangs, it is not a budget.
SCHEMA_INSTALL_TIMEOUT = 10.0
#: The command's own line for a file it could not write. Only this failure
#: reaches the terminal: a usage error from a release without the verb is
#: the same exit status (2), so the text tells them apart, and an unknown
#: text stays quiet (logged) rather than noisy.
SCHEMA_INSTALL_FAILED = "pager settings install: could not write "
#: The logged output is capped: another program of the same name on PATH
#: must not grow the log on every launch.
SCHEMA_INSTALL_KEEP_LINES = 20
SCHEMA_INSTALL_KEEP_CHARS = 500


class EnvKey(NamedTuple):
    """One variable a tool's schema names: the key path it sets, the
    variable's name, and the key's own schema (its `type` reads the string)."""

    path: tuple[str, ...]
    variable: str
    schema: dict


def schema_tools(env: Mapping[str, str] | None = None) -> list[str]:
    """Every tool with an installed schema (find_schema): a tool PACKAGES
    names whose host_dir holds one under either name, or a SETTINGS_FILE_ENV
    tool with one beside its settings file. The map, never a directory
    scan: one package's directory holds several tools' files."""
    names = set(PACKAGES) | set(SETTINGS_FILE_ENV)
    return sorted(name for name in names if find_schema(name, env)[0] is not None)


def env_keys(tool: str, env: Mapping[str, str] | None = None) -> list[EnvKey]:
    """The `x-env` entries of TOOL's installed schema, at any depth of
    `properties`. Empty with no schema."""
    path, _not_read = find_schema(tool, env)
    if path is None:
        return []
    out: list[EnvKey] = []

    def walk(node: Mapping, prefix: tuple[str, ...]) -> None:
        props = node.get("properties")
        if not isinstance(props, Mapping):
            return
        for name, sub in props.items():
            if not isinstance(sub, Mapping):
                continue
            here = prefix + (str(name),)
            var = sub.get(X_ENV)
            if isinstance(var, str) and var:
                out.append(EnvKey(here, var, dict(sub)))
            walk(sub, here)

    walk(read_json_object(path), ())
    return out


def read_env_value(raw: str, schema: Mapping[str, Any]) -> Any:
    """RAW, a variable's string, read by the type of the key it sets: an
    object of booleans takes a list split on commas AND whitespace as a map
    of true (`tm kick` writes SESSIONMAIL_SUBSCRIBE space-separated, which is
    sessionmail's own reading; a name holds neither), and any other object
    is refused; an array, a comma list; an integer or a number, parsed; a
    boolean, from 1/true/yes/on or 0/false/no/off (any case); a string (or a
    key with no type), as written. ValueError names what could not be
    read, a key of any other type included."""
    kind = schema.get("type")
    if kind == "object":
        extra = schema.get("additionalProperties")
        if not (isinstance(extra, Mapping) and extra.get("type") == "boolean"):
            raise ValueError("an object key reads only a list of names into a map of true")
        return {name: True for name in re.split(r"[,\s]+", raw) if name}
    if kind == "array":
        return [x.strip() for x in raw.split(",") if x.strip()]
    if kind == "integer":
        return int(raw.strip())
    if kind == "number":
        return float(raw.strip())
    if kind == "boolean":
        word = raw.strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise ValueError(f"not a boolean: {raw!r} (one of {', '.join(TRUE_WORDS + FALSE_WORDS)})")
    if kind in (None, "string"):
        return raw
    raise ValueError(f"no reading for a key of type {kind!r}")


def _nest(path: tuple[str, ...], value: Any, into: dict) -> None:
    node = into
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _log(log_file: Path, entry: Mapping[str, Any]) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def write_env_at_birth(session: Path, environ: Mapping[str, str],
                       env: Mapping[str, str] | None = None,
                       now: str | None = None) -> list[dict]:
    """Read every variable an installed schema names from ENVIRON into
    SESSION's `settings/<tool>.d/50-env.json`, once: a tool whose file is
    already there keeps it (a resumed session keeps its file). Each name is
    logged in SESSION's `log/settings.jsonl`, never its value; a variable
    set but empty is logged `env-empty` and not read. The file is 0600 in
    0700 dirs. Returns the log entries. When no installed schema names any variable, one
    `birth-no-schema` entry is logged and returned. A value that cannot be read is logged as refused and left
    out; the rest of the tool's variables are still written. Each schema
    file found and not read (find_schema) is logged `schema-shadowed`,
    naming the file read and the file not read."""
    from datetime import UTC, datetime

    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    log_file = session / "log" / SETTINGS_LOG
    entries: list[dict] = []
    tools = schema_tools(env)
    named = False       # does any installed schema name an x-env variable?
    for tool in tools:
        read_from, not_read = find_schema(tool, env)
        for other in not_read:
            shadow = {"ts": stamp, "event": "schema-shadowed", "tool": tool,
                      "read": str(read_from), "not_read": str(other)}
            _log(log_file, shadow)
            entries.append(shadow)
        target = session / "settings" / f"{tool}.d" / ENV_DROP_IN
        values: dict = {}
        read: list[dict] = []
        keys = env_keys(tool, env)
        named = named or bool(keys)
        for key in keys:
            raw = environ.get(key.variable)
            if raw is None:
                continue
            base = {"ts": stamp, "tool": tool, "variable": key.variable,
                    "key": ".".join(key.path), "file": str(target)}
            if raw == "":       # set but empty: logged, so a reader can tell it from unset
                read.append({**base, "event": "env-empty"})
                continue
            try:
                _nest(key.path, read_env_value(raw, key.schema), values)
            except ValueError as error:
                read.append({**base, "event": "env-refused", "error": str(error)})
                continue
            read.append({**base, "event": "env-at-birth"})
        if not read:
            continue
        if _stat_mode(target) is not None:
            read = [{**e, "event": "env-kept"} for e in read if e["event"] == "env-at-birth"]
        elif values:
            # 0700 dirs and a 0600 file, as `launch/`: it holds raw values (a
            # review finding on 8587f02).
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(values, indent=2, sort_keys=True) + "\n")
        for entry in read:
            _log(log_file, entry)
        entries.extend(read)
    if not named:       # nothing for the birth to read: say so once, so a reader can see why
        none = {"ts": stamp, "event": "birth-no-schema",
                "root": str(kit_root(env)), "schemas": tools}
        _log(log_file, none)
        entries.append(none)
    return entries


def birth_main(argv: list[str] | None = None,
               environ: Mapping[str, str] | None = None) -> int:
    """The launch script's entry: `--dir SESSION_DIR`. Always exit 0: a
    settings file not written is a stderr line, never a session that did
    not start (as the launch record)."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--dir" or not args[1]:
        print(f"{BIRTH_PROG}: usage: --dir SESSION_DIR; nothing written", file=sys.stderr)
        return 0
    source = os.environ if environ is None else environ
    try:
        for entry in write_env_at_birth(Path(args[1]), source, env=source):
            if entry["event"] == "env-refused":
                print(f"{BIRTH_PROG}: {entry['variable']} not read into "
                      f"{entry['tool']}.{entry['key']}: {entry['error']}", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - settings never cost a session
        print(f"{BIRTH_PROG}: not written ({type(error).__name__}: {error})", file=sys.stderr)
    return 0


def _capped(data: bytes) -> tuple[list[str], bool]:
    """DATA's lines, at most SCHEMA_INSTALL_KEEP_LINES of them, each cut to
    SCHEMA_INSTALL_KEEP_CHARS; and whether anything was cut."""
    lines = data.decode("utf-8", errors="replace").splitlines()
    kept = [x[:SCHEMA_INSTALL_KEEP_CHARS] for x in lines[:SCHEMA_INSTALL_KEEP_LINES]]
    cut = len(lines) > SCHEMA_INSTALL_KEEP_LINES or any(
        len(x) > SCHEMA_INSTALL_KEEP_CHARS for x in lines[:SCHEMA_INSTALL_KEEP_LINES])
    return kept, cut


def run_schema_install(session: Path, command: list[str], environ: Mapping[str, str],
                       timeout: float = SCHEMA_INSTALL_TIMEOUT,
                       now: str | None = None) -> tuple[dict, list[str]]:
    """Run COMMAND (a tool's schema install) in ENVIRON and log ONE
    `schema-install` entry in SESSION's `log/settings.jsonl`: the command,
    the program PATH found, its exit status and its output (capped; a cut
    is noted as `truncated`). Its stdin is /dev/null and its output is
    captured, so nothing it says reaches the terminal and nothing can wait
    on a keyboard. It runs in a session of its own, so a timeout kills
    whatever it started. Returns the entry and the lines of the command's
    own could-not-write failure (empty when there is none)."""
    import shutil
    import signal
    import subprocess
    from datetime import UTC, datetime

    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    entry: dict = {"ts": stamp, "event": "schema-install", "command": " ".join(command)}
    failed: list[str] = []
    found = shutil.which(command[0], path=environ.get("PATH", ""))
    if found is None:
        entry["error"] = f"{command[0]}: not found on PATH"
    else:
        entry["path"] = found
        try:
            proc = subprocess.Popen([found, *command[1:]], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=dict(environ), start_new_session=True)
        except OSError as error:
            entry["error"] = f"{type(error).__name__}: {error}"
        else:
            try:
                out, err = proc.communicate(timeout=timeout)
                entry["exit"] = proc.returncode
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    out, err = proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:    # a descendant left its group, holding a pipe
                    out, err = b"", b""
                entry["exit"] = None
                entry["timeout"] = timeout
            truncated = []
            for name, data in (("out", out), ("err", err)):
                entry[name], cut = _capped(data)
                if cut:
                    truncated.append(name)
            if truncated:
                entry["truncated"] = truncated
            if entry["exit"]:
                failed = [x for x in err.decode("utf-8", errors="replace").splitlines()
                          if x.startswith(SCHEMA_INSTALL_FAILED)]
    _log(session / "log" / SETTINGS_LOG, entry)
    return entry, failed


def schema_install_main(argv: list[str] | None = None,
                        environ: Mapping[str, str] | None = None,
                        timeout: float = SCHEMA_INSTALL_TIMEOUT) -> int:
    """The launch script's entry: `--dir SESSION_DIR -- COMMAND...`. Runs
    the command (run_schema_install) before the birth line. Always exit 0
    and silent, except ONE stderr line when the command says it could not
    write a file, naming the log that holds the rest: settings never stop
    a launch, and a release without the verb is not news on every launch."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[0] != "--dir" or not args[1] or args[2] != "--":
        print(f"{SCHEMA_INSTALL_PROG}: usage: --dir SESSION_DIR -- COMMAND...; nothing run",
              file=sys.stderr)
        return 0
    session, command = Path(args[1]), args[3:]
    source = os.environ if environ is None else environ
    try:
        entry, failed = run_schema_install(session, command, source, timeout=timeout)
    except Exception as error:  # noqa: BLE001 - settings never cost a session
        print(f"{SCHEMA_INSTALL_PROG}: not run ({type(error).__name__}: {error})",
              file=sys.stderr)
        return 0
    if failed:
        paths = ", ".join(x[len(SCHEMA_INSTALL_FAILED):] for x in failed)
        print(f"{SCHEMA_INSTALL_PROG}: {entry['command']} exit {entry['exit']}, could not "
              f"write {paths}; recorded in {session / 'log' / SETTINGS_LOG}", file=sys.stderr)
    return 0


# --- the reader --------------------------------------------------------------

def resolve(tool: str, sid: str | None, agent: str | None = None, seat: str | None = None,
            env: Mapping[str, str] | None = None, agent_file: str | None = None) -> Resolved:
    """TOOL's settings for session SID: merged, with the file behind each key."""
    srcs = sources(tool, sid, agent, seat, env, agent_file)
    leaves, origins, locked = _fold(srcs)
    values = _unflatten(leaves)
    problems = check_schema(tool, values, origins, env)
    notes = schema_notes(tool, env)
    notes.extend(host_files(tool, env).notes)
    if agent:
        _file, gone = agent_file_for(agent, sid, env, agent_file)
        if gone:
            notes.append(gone)
    groups = _joined_groups(origins)
    notes.extend(_collisions(tool, origins, srcs, env, groups))
    collided = {p for paths in groups.values() if len(paths) > 1 for p in paths}
    notes.extend(_splits(tool, origins, srcs, env, collided))
    return Resolved(tool, values, {_shown(p): f for p, f in origins.items()}, dict(locked), srcs,
                    problems, {_shown(p): v for p, v in leaves.items()}, notes,
                    {_shown(p): p for p in leaves})


#: Where a note on the agent's shorthand ends: the spelling that keeps a key whole.
NESTED_HINT = "write it nested to keep the key whole: "

_OPENERS, _CLOSERS = "([{", ")]}"


def _joined_groups(origins: Mapping[tuple, str]) -> dict[str, list[tuple]]:
    """The surviving leaf paths grouped by their joined dotted string."""
    groups: dict[str, list[tuple]] = {}
    for path in origins:
        groups.setdefault(_joined(path), []).append(path)
    return groups


def _count(n: int) -> str:
    return "two" if n == 2 else str(n)


def _listed(items: list[str]) -> str:
    return f"{', '.join(items[:-1])} and {items[-1]}" if len(items) > 1 else items[0]


def _collisions(tool: str, origins: Mapping[tuple, str], srcs: list[Source],
                env: Mapping[str, str] | None,
                groups: Mapping[str, list[tuple]] | None = None) -> list[str]:
    """One note for each set of surviving leaves whose different paths join
    to one dotted string, naming each path and its file in cascade order.
    When the agent layer is one side, the note ends with the nested spelling
    that keeps the key whole."""
    rank = {str(s.path): i for i, s in enumerate(srcs)}
    layer = {str(s.path): s.layer for s in srcs}
    if groups is None:
        groups = _joined_groups(origins)
    out = []
    for paths in groups.values():
        if len(paths) < 2:
            continue
        paths.sort(key=lambda p: rank.get(origins[p], len(srcs)))
        named = [f"{_shown(p)} ({_short(origins[p], env)})" for p in paths]
        note = (f"{_listed(named)} join to one dotted path: "
                f"{_count(len(paths))} keys, neither overrides the other")
        if any(layer.get(origins[p]) == "agent" for p in paths):
            others = [p for p in paths if layer.get(origins[p]) != "agent"] or paths
            note += f"; {NESTED_HINT}{_nested(tool, min(others, key=len))}"
        out.append(note)
    return out


def _inside_cuts(path: tuple) -> list[int]:
    """Each i where the cut between PATH[i] and PATH[i+1] falls inside
    brackets or a double quote. Read left to right: `"` opens and closes a
    quote; outside one, `(`, `[`, `{` add to the depth and `)`, `]`, `}`
    take from it, never below 0. A cut is inside when, at the end of
    segment i, the depth is above 0 or a quote is open."""
    depth, quoted, out = 0, False, []
    for i, seg in enumerate(path[:-1]):
        for ch in seg:
            if ch == '"':
                quoted = not quoted
            elif quoted:
                continue
            elif ch in _OPENERS:
                depth += 1
            elif ch in _CLOSERS:
                depth = max(0, depth - 1)
        if depth or quoted:
            out.append(i)
    return out


def _runs(path: tuple, cuts: list[int]) -> list[tuple]:
    """PATH's segments in runs: the segments an inside cut joins share one run."""
    runs: list[tuple] = []
    start = 0
    for i in range(len(path)):
        if i not in cuts:
            runs.append(tuple(path[start:i + 1]))
            start = i + 1
    return runs


def _whole(run: tuple) -> str:
    """One run of segments as the key it was likely meant to be: rejoined at
    its dots, and unquoted when the rejoined key is one JSON string (a key
    copied from the report's own quoted spelling)."""
    key = ".".join(run)
    if len(run) > 1 and len(key) > 1 and key[0] == key[-1] == '"':
        try:
            whole = json.loads(key)
        except ValueError:
            return key
        if isinstance(whole, str):
            return whole
    return key


def _splits(tool: str, origins: Mapping[tuple, str], srcs: list[Source],
            env: Mapping[str, str] | None, collided: set[tuple]) -> list[str]:
    """One note for each agent key cut at a dot inside brackets or quotes
    (`_inside_cuts`): the dotted shorthand splits at every dot, so a rule
    such as `Read(~/.zshrc)` becomes two keys, and its row reads like the
    rule. Only surviving agent-layer leaves are read (a JSON key is whole as
    written), one note per split key, and never for a leaf a collision
    note already names. The note ends with the nested spelling of the key
    likely meant."""
    layer = {str(s.path): s.layer for s in srcs}
    keys: dict[tuple, str] = {}
    for path, origin in origins.items():
        if layer.get(origin) != "agent" or path in collided:
            continue
        cuts = _inside_cuts(path)
        if cuts:
            keys.setdefault(path[:cuts[-1] + 2], origin)
    out = []
    for key, origin in keys.items():
        cuts = _inside_cuts(key)
        runs = _runs(key, cuts)
        pieces = [json.dumps(seg, ensure_ascii=False) for run in runs if len(run) > 1 for seg in run]
        meant = sum(len(run) > 1 for run in runs)
        out.append(f"{_shown(key)} ({_short(origin, env)}) is cut at a dot inside brackets or "
                   f"quotes: {_listed(pieces)} are {_count(len(pieces))} keys where "
                   f"{'one was' if meant == 1 else f'{meant} were'} likely meant; "
                   f"{NESTED_HINT}{_nested(tool, tuple(_whole(run) for run in runs))}")
    return out


def _nested(tool: str, path: tuple) -> str:
    """How an agent's front matter writes PATH so no key is split: the tool
    and the leading dot-free segments dotted, the rest nested from the first
    segment that holds a dot (`expand_dotted` stops at a map)."""
    cut = next((i for i, seg in enumerate(path) if "." in seg), len(path))
    head = ".".join((tool, *path[:cut]))
    body = "..."
    for seg in reversed(path[cut:]):
        body = f"{{{json.dumps(seg, ensure_ascii=False)}: {body}}}"
    return f"{head}: {body}"


# --- homes --------------------------------------------------------------------

SESSION_HOME_ENV = "SESSION_HOME"


class Home(NamedTuple):
    """Where a session's home is, and why. `place` is the directory. With
    `per_session` the session id was not known when this was computed (a
    pane-minted launch): `place` is then the sessions ROOT, and the home is
    `<place>/<sid>/home`, finished where the id is."""

    place: str
    layer: str      # agent | seat | session | host | template, or dir (home_dir)
    origin: str     # the file that set it
    per_session: bool = False


def _expand_tilde(path: str, env: Mapping[str, str] | None) -> str:
    """`~` and `~/x` against the HOME this reader reads (the env passed in),
    not the process's own: a launch computes another user's tree."""
    if path == "~":
        return str(home(env))
    if path.startswith("~/"):
        return str(home(env) / path[2:])
    return path


def _cascade_rank(res: Resolved, origin: str) -> int:
    """Where the file ORIGIN sits in RES's cascade, top to bottom: a higher
    rank is lower down, so it wins. -1 for a file not among the sources."""
    return max((i for i, s in enumerate(res.sources) if str(s.path) == origin), default=-1)


def session_home(res: Resolved, sid: str | None, agent: str | None, seat: str | None,
                 env: Mapping[str, str] | None = None) -> Home | None:
    """This session's home from the `session` tool's resolved settings, or
    None: no home by default. `home_dir: PATH` is the place and implies
    `home: true`. `home: true` puts it by the LAYER that set it: agent ->
    `agent/home/<agent>/`, seat -> `seats/<seat>/home/`, session (and the
    host or template layer: one per session) -> `sessions/<sid>/home/`. The
    lowest layer that sets `home` wins, which `merge` has already decided,
    so `home: false` below a `home: true` is no home.

    `home` is the SWITCH and `home_dir` the PLACE. A `home: false` in a
    lower file than the `home_dir` turns the home off; a `home_dir` below a
    `home: false` turns it back on; a lower `home: true` only says "on", and
    the place stays the `home_dir`. `home: false` and `home_dir` in ONE file
    contradict each other and are refused, naming the file."""
    values, origins = res.values, res.origins
    home_dir = values.get("home_dir")
    if home_dir:
        origin = origins.get("home_dir", "?")
        if not isinstance(home_dir, str):
            raise SettingsError(f"{origin}: home_dir is not a path")
        home_origin = origins.get("home")
        switched_off = values.get("home") is False
        if switched_off and home_origin == origin:
            raise SettingsError(f"{origin}: home: false and home_dir in one file")
        if not switched_off or _cascade_rank(res, origin) > _cascade_rank(res, home_origin or ""):
            return Home(_expand_tilde(home_dir, env), "dir", origin)
        return None
    if values.get("home") is not True:
        return None
    origin = res.origins.get("home", "?")
    layer = next((s.layer for s in res.sources if str(s.path) == origin), "?")
    if layer == "agent" and agent:
        return Home(str(agent_home(agent, env)), layer, origin)
    if layer == "seat" and seat:
        return Home(str(seat_dir(seat, env) / "home"), layer, origin)
    if sid:
        return Home(str(session_dir(sid, env) / "home"), layer, origin)
    return Home(str(sessions_root(env)), layer, origin, per_session=True)


def identity(sid: str, env: Mapping[str, str] | None = None) -> tuple[str | None, str | None]:
    """(agent, seat) from the session layer of the `session` tool, written at
    birth; (None, None) until that tool has moved."""
    agent = seat = None
    for p in layer_files(session_dir(sid, env) / "settings", "session"):
        data = read_json_object(p)
        agent = data.get("agent", agent)
        seat = data.get("seat", seat)
    return (agent if isinstance(agent, str) else None,
            seat if isinstance(seat, str) else None)


def tools(sid: str, agent: str | None = None, seat: str | None = None,
          env: Mapping[str, str] | None = None) -> list[str]:
    """Every tool with a file at a new path for this session, plus the host's."""
    names: set[str] = set()
    for tool, package in PACKAGES.items():  # the map: a package's directory holds several tools
        d = package_dir(package, env)
        if any(d.glob(f"{tool}.*json")) or _is_dir(d / f"{tool}.d"):
            names.add(tool)
    for tool in SETTINGS_FILE_ENV:
        if settings_file_override(tool, env) is None:
            continue
        found = host_files(tool, env)
        if found.template or found.host:
            names.add(tool)
        else:   # the default dir is not read under an override: it lists nothing
            names.discard(tool)
    for directory in (seat_dir(seat, env) / "settings" if seat else None,
                      session_dir(sid, env) / "settings"):
        if directory and _is_dir(directory):
            for p in _entries(directory):
                if p.suffix == ".json" and _is_file(p):
                    names.add(p.stem)
                elif _is_dir(p) and p.name.endswith(".d"):
                    names.add(p.name[:-2])
    if agent:
        found = agent_settings(agent, env, agent_file_for(agent, sid, env)[0])
        if found is not None:
            names.update(k for k, v in found[1].items() if isinstance(v, dict))
    return sorted(names)


# --- the report --------------------------------------------------------------

def _short(path: str, env: Mapping[str, str] | None = None) -> str:
    h = str(home(env))
    return "~" + path[len(h):] if path.startswith(h + "/") else path


# Maps the tool reads PER OWNER, never merged: each layer's entries act on
# their own (mail's subscriptions make routing rows per layer; `false` is the
# same as absent). The report lists them by layer, so the page never shows a
# merged `false` that routing ignores.
PER_OWNER: dict[str, tuple[str, ...]] = {"mail": ("subscriptions",)}


def _per_owner_lines(res: Resolved, env: Mapping[str, str] | None) -> list[str]:
    lines = []
    for top in PER_OWNER.get(res.tool, ()):
        rows = []
        for src in res.sources:
            entries = src.data.get(top)
            if isinstance(entries, Mapping):
                for name, value in entries.items():
                    shown = json.dumps(value, ensure_ascii=False)
                    lock = _under_lock(f"{top}.{name}", res.locked)
                    tail = f"  [locked by {_short(lock, env)}]" if lock else ""
                    rows.append(f"    {_shown((top, str(name))):<28} {shown:<18} "
                                f"{_short(str(src.path), env)}{tail}")
        if rows:
            lines.append(f"  {top}: per owner, each layer's entries act alone (false = absent)")
            lines.extend(rows)
    return lines


def report(res: Resolved, env: Mapping[str, str] | None = None) -> str:
    """The lines `session settings` prints for one tool."""
    layers = [s.layer for s in res.sources]
    found = ", ".join(dict.fromkeys(layers)) or "nothing"
    if not res.moved:
        override = settings_file_override(res.tool, env)
        directory = _tool_dir(res.tool, env)
        hosts = _short(str(directory), env) if directory is not None else "no package (PACKAGES)"
        where = f"{hosts}, seats/<seat>/settings, sessions/<sid>/settings"
        missing = f"no {res.tool}.json under {where}"
        if override is not None:     # the default dir is not read under an override
            missing = (f"no {_short(str(override), env)} (${settings_file_variable(res.tool, env)}), "
                       f"and no {res.tool}.json under seats/<seat>/settings, "
                       f"sessions/<sid>/settings")
        lines = [f"{res.tool}  not moved: {missing}"]
        lines.extend(f"  NOTE {n}" for n in res.notes)
        return "\n".join(lines) + "\n"
    lines = [f"{res.tool}  ({found})"]
    owned = PER_OWNER.get(res.tool, ())
    for key, file in sorted(res.origins.items()):
        path = res.paths.get(key) or tuple(key.split("."))
        if path[0] in owned:
            continue
        shown = json.dumps(res.leaves.get(key), ensure_ascii=False)
        lock = _under_lock(_joined(path), res.locked)
        tail = f"  [locked by {_short(lock, env)}]" if lock else ""
        lines.append(f"  {key:<30} {shown:<18} {_short(file, env)}{tail}")
    lines.extend(_per_owner_lines(res, env))
    for p in res.problems:
        lines.append(f"  REFUSED {p}")
    for n in res.notes:
        lines.append(f"  NOTE {n}")
    return "\n".join(lines) + "\n"
