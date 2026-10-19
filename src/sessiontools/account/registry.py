"""The account registry: names, never tokens.

`~/.sessionkit/tools/accounts/accounts.json` maps a short NAME (`a`, `c`, `p`)
to an email, an ordered list of token SOURCES, and a fingerprint:

    {"version": 1,
     "accounts": [
       {"name": "a", "email": "ai@example.com",
        "sources": ["file:~/.config/tokens/oauth-token-ai@example.com"],
        "fp": null}]}

- Order is display order (a, c, p: as configured) and is kept exactly.
- A source is `backend:ref`, the syntax `session start` already has. The
  first source that resolves wins, so list order is cost order: a 0600
  `file` is free, `pass` costs a PIN and a key touch, `passage` a touch.
- `fp = sha256(token)[:16]` (hex) lets `status` and `use` turn a token they
  see back into a name without the registry ever holding the token.

The file is mode 0600 and written atomically (a temp file in the same
directory, then a rename). A file that is not a private regular file, or
does not parse as a registry, is REFUSED with exit 5 and is never silently
rewritten: `save` refuses to replace it too, so a person sees the bad file
before anything overwrites it.

THE ACCOUNT FOLDER is `settings.accounts_dir()`, `~/.sessionkit/tools/accounts/`
(the account-home move): every account file (the registry, `status`'s config, `use`'s
ledger and backups, `new`'s setup-token scratch) derives from `accounts_dir`
here. A folder this package makes there is 0700, each missing parent too
(`make_private_dirs`); an existing one is never chmodded. The folder before
the kit's home (OLD_ACCOUNTS_SUBDIR) is never read and never written: when it
holds files and the new folder does not exist, the accounts are STRANDED
(`stranded`). A reading verb says so once on stderr (`say_stranded`) and reads
the new folder only; a writing verb refuses, exit 5 (`refuse_stranded`). Both
name the mover, `python -m sessiontools.account.movehome`, by this process's
own interpreter.

No message, repr or exception text in this module carries a token value.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from sessiontools import settings
from sessiontools.start.secrets import (
    SecretError,
    is_private,
    pass_backend,
    passage_backend,
)

VERSION = 1
#: The account folder before the kit's home, under HOME. The stranded check
#: and the mover are its only users: never opened for content, never
#: written, never a fallback (the account-home move, as settings_root is for settings).
OLD_ACCOUNTS_SUBDIR = Path(".config/sessiontools")
REGISTRY_NAME = "accounts.json"
#: The mover's module, run by this process's interpreter (`mover_command`).
MOVER_MODULE = "sessiontools.account.movehome"

#: Exit codes this module raises with.
EXIT_USAGE = 2           # a duplicate or unknown name
EXIT_UNRESOLVED = 4      # every source of an account failed
EXIT_REGISTRY = 5        # the registry file is refused

FP_LEN = 16
#: Matched with `fullmatch`, never `match`: `$` also matches before a
#: trailing newline, so `match` would let "a\n" in as a name.
_FP = re.compile(rf"[0-9a-f]{{{FP_LEN}}}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
#: Token-shaped text. The registry refuses to hold anything like it, in
#: any field, so a token pasted where a path belongs never reaches disk.
_TOKEN_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")

FIELDS = ("name", "email", "sources", "fp")


class RegistryError(Exception):
    """A registry the module refuses, or a lookup that failed. The message
    names a path, a name or a source; never a token. `code` is the exit
    code a caller should leave with."""

    def __init__(self, message: str, code: int = EXIT_REGISTRY) -> None:
        super().__init__(message)
        self.code = code


def _env(home: str | os.PathLike | None) -> dict[str, str] | None:
    """The env `settings` derives paths from: HOME=home, or None (the
    process's own HOME, read when called, so a test's HOME holds)."""
    return None if home is None else {"HOME": os.fspath(home)}


def accounts_dir(home: str | os.PathLike | None = None) -> Path:
    """`<home>/.sessionkit/tools/accounts/`, `settings.accounts_dir`: the ONE
    folder every account file derives from."""
    return settings.accounts_dir(_env(home))


def old_accounts_dir(home: str | os.PathLike | None = None) -> Path:
    """`<home>/` + OLD_ACCOUNTS_SUBDIR: the folder before the kit's home, for
    the stranded check and the mover only."""
    return settings.home(_env(home)) / OLD_ACCOUNTS_SUBDIR


def default_path(home: str | os.PathLike | None = None) -> Path:
    """`<home>/.sessionkit/tools/accounts/accounts.json`; `home` defaults to
    the user's home, read when called (so a test's HOME holds)."""
    return accounts_dir(home) / REGISTRY_NAME


def make_private_dirs(directory: str | os.PathLike) -> Path:
    """DIRECTORY, and each missing parent, made 0700 one at a time
    (`Path.mkdir(parents=True)` makes the parents with the umask's mode).
    An existing folder is left as it is, never chmodded. Every look is the
    strict stat (`settings._stat_mode`): a path that cannot be looked at
    raises SettingsUnreadable, never reads as absent. A file where a folder
    belongs raises NotADirectoryError. Returns DIRECTORY."""
    target = Path(directory)
    missing: list[Path] = []
    here = target
    while True:
        mode = settings._stat_mode(here)
        if mode is not None:
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(here))
            break
        missing.append(here)
        if here.parent == here:
            break
        here = here.parent
    for path in reversed(missing):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            continue                  # a racing writer made it: leave it as it is
        os.chmod(path, 0o700)         # mkdir's mode is cut by the umask
    return target


# --- the stranded accounts: still only in the old folder ------------------------

#: Said once per process on stderr by a reading verb, formatted with {old},
#: {new} (`~` paths) and {mover} (the dry run's command line).
STRANDED_LINE = ("sessiontools: the accounts in {old} have not been moved to {new}, so "
                 "they were not read and nothing was written. Move them with `{mover}` "
                 "(a dry run), then `{mover} --apply`, then re-run.")
#: A writing verb's refusal, exit 5: a write would make the new folder and
#: leave both, which the mover refuses.
STRANDED_REFUSAL = ("refused: the accounts in {old} have not been moved to {new}, so "
                    "nothing was read and nothing was written (a write here would leave "
                    "both folders). Move them with `{mover}` (a dry run), then "
                    "`{mover} --apply`, then re-run.")
#: True once this process has said STRANDED_LINE.
_said_stranded = False


def mover_command() -> str:
    """`<this interpreter> -m sessiontools.account.movehome`, the dry run.
    `sys.executable`, never a bare `python`: under `uv tool install` the
    tool's own interpreter is not on PATH."""
    return f"{shlex.quote(sys.executable or 'python3')} -m {MOVER_MODULE}"


def stranded(home: str | os.PathLike | None = None) -> bool:
    """True when the accounts are still only in the old folder: the new
    folder does not exist, and the old one is a folder holding at least
    one entry. Kinds and names only: nothing old is opened. Every look is
    the strict stat, so a folder that cannot be looked at RAISES
    RegistryError (exit 5) naming it, never reads as absent."""
    try:
        if settings._stat_mode(accounts_dir(home)) is not None:
            return False
        old = old_accounts_dir(home)
        return settings._is_dir(old) and bool(settings._entries(old))
    except settings.SettingsUnreadable as error:
        raise RegistryError(f"{error.filename} cannot be looked at "
                            f"({error.strerror or 'unreadable'}): it may hold accounts, so it "
                            "is never read as absent; nothing was read or written") from None


def _stranded_text(template: str, home: str | os.PathLike | None) -> str:
    env = _env(home)
    return template.format(old=settings._short(str(old_accounts_dir(home)), env) + "/",
                           new=settings._short(str(accounts_dir(home)), env) + "/",
                           mover=mover_command())


def stranded_line(home: str | os.PathLike | None = None) -> str:
    return _stranded_text(STRANDED_LINE, home)


def say_stranded(home: str | os.PathLike | None = None, err: TextIO | None = None) -> bool:
    """A reading verb's check: when the accounts are stranded, STRANDED_LINE
    on ERR (stderr), at most once per process. Returns whether they are.
    An unreadable folder: RegistryError, exit 5 (`stranded`)."""
    global _said_stranded
    if not stranded(home):
        return False
    if not _said_stranded:
        _said_stranded = True
        print(stranded_line(home), file=sys.stderr if err is None else err, flush=True)
    return True


def refuse_stranded(home: str | os.PathLike | None = None) -> None:
    """A writing verb's check: RegistryError (exit 5), STRANDED_REFUSAL, when
    the accounts are stranded. Nothing is written either way."""
    if stranded(home):
        raise RegistryError(_stranded_text(STRANDED_REFUSAL, home))


def fingerprint(token: str) -> str:
    """`sha256(token)[:16]`, hex. A 64-bit prefix of a hash of a
    high-entropy token is a name for it, not a usable secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:FP_LEN]


def same_token(a: str, b: str) -> bool:
    """True when `a` and `b` are the same token: `hmac.compare_digest` over
    their SHA-256 digests, so the comparison neither prints nor times
    either value. `new`'s read-back uses it, and a later
    caller can."""
    return hmac.compare_digest(hashlib.sha256(a.encode("utf-8")).digest(),
                               hashlib.sha256(b.encode("utf-8")).digest())


# --- sources -----------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One `backend:ref`. `ref` is a path or a pass or passage entry, never a value."""

    backend: str
    ref: str

    def __str__(self) -> str:
        return f"{self.backend}:{self.ref}"


def _private_check(path: str) -> str | None:
    """Why `path` is not a private regular file of ours, or None if it is:
    a regular file (a symlink is followed), owned by this user, mode 0600
    (`secrets.is_private`)."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return "no such file"
    except OSError as error:
        return f"cannot stat it ({error.strerror or error})"
    why = _private_info(info)
    if why is None and not is_private(path):
        return f"mode {stat.S_IMODE(info.st_mode):04o}, not 0600"
    return why


def _private_info(info: os.stat_result) -> str | None:
    """`_private_check` on a stat result already taken (an `fstat`, say)."""
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_uid != os.getuid():
        return "not owned by this user"
    if stat.S_IMODE(info.st_mode) != 0o600:           # `secrets.is_private`'s rule
        return f"mode {stat.S_IMODE(info.st_mode):04o}, not 0600"
    return None


def file_backend(ref: str) -> str:
    """`file:PATH`: the first line of a private file (`~` is expanded).

    REFUSES unless PATH is a regular file, owned by this user, mode 0600:
    a token another user could read is already leaked, and using it would
    hide that. The reason names the path, never the content."""
    path = os.path.expanduser(ref)
    why = _private_check(path)
    if why is not None:
        raise SecretError(f"file:{ref} refused: {why}")
    # Then check what was OPENED (fstat) by the same rule, so the file read
    # is a file that passed: the path could be swapped between the check
    # and the open. O_NONBLOCK: a FIFO cannot hang it.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise SecretError(f"file:{ref} refused: no such file") from None
    except OSError as error:
        raise SecretError(f"file:{ref} refused: cannot open it "
                          f"({error.strerror or type(error).__name__})") from None
    try:
        why = _private_info(os.fstat(fd))
        if why is not None:
            raise SecretError(f"file:{ref} refused: {why}")
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1                                   # the handle owns it now
            first = handle.readline()
    except (OSError, UnicodeDecodeError) as error:
        raise SecretError(f"file:{ref} could not be read "
                          f"({getattr(error, 'strerror', None) or type(error).__name__})") from None
    finally:
        if fd >= 0:
            os.close(fd)
    value = first.strip()
    if not value:
        raise SecretError(f"file:{ref} is empty")
    return value


Backend = Callable[[str], str]

#: `file` is v1's addition; `pass` is `session start`'s own backend, reused;
#: `passage` (age) is `new`'s, and is registered here only.
BACKENDS: dict[str, Backend] = {"file": file_backend, "pass": pass_backend,
                                "passage": passage_backend}

#: What each backend costs to resolve, for `list`. passage
#: costs Touch ID, or a key touch with a hardware age key.
COST = {"file": "free", "pass": "key touch", "passage": "touch"}


def parse_source(text: str, backends: Iterable[str] | None = None) -> Source:
    """`backend:ref` -> Source. Split at the FIRST colon, so a ref may hold
    more. Refuses an empty part, an unknown backend, or a token-shaped
    string (that is a token where a path belongs: never echoed back)."""
    known = tuple(BACKENDS if backends is None else backends)
    if not isinstance(text, str):
        raise RegistryError(f"a source must be a string `backend:ref`, not {type(text).__name__}",
                            EXIT_USAGE)
    if _TOKEN_SHAPE.search(text):
        raise RegistryError("a source looks like a token, not `backend:ref` "
                            "(refused; the value is not shown)", EXIT_USAGE)
    backend, sep, ref = text.partition(":")
    if not sep or not backend or not ref:
        raise RegistryError(f"source {text!r} is not `backend:ref` (known backends: "
                            f"{', '.join(sorted(known))})", EXIT_USAGE)
    if backend not in known:
        raise RegistryError(f"source {text!r}: unknown backend {backend!r} "
                            f"(known: {', '.join(sorted(known))})", EXIT_USAGE)
    return Source(backend, ref)


# --- accounts and the registry -----------------------------------------------


@dataclass(frozen=True)
class Account:
    name: str
    email: str
    sources: tuple[Source, ...]
    fp: str | None = None

    def to_json(self) -> dict:
        return {"name": self.name, "email": self.email,
                "sources": [str(source) for source in self.sources], "fp": self.fp}


def make_account(name: str, email: str, sources: Iterable[str | Source],
                 fp: str | None = None) -> Account:
    """A checked Account: a short name, an email, at least one parseable
    source, an fp that is None or 16 lowercase hex digits."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise RegistryError(f"account name {name!r} is not a short name "
                            "(letters, digits, . _ -; at most 32)", EXIT_USAGE)
    if not isinstance(email, str) or "@" not in email or _TOKEN_SHAPE.search(email):
        raise RegistryError(f"account {name!r}: email must be an address", EXIT_USAGE)
    parsed = tuple(source if isinstance(source, Source) else parse_source(source)
                   for source in sources)
    if not parsed:
        raise RegistryError(f"account {name!r}: at least one source is required", EXIT_USAGE)
    if fp is not None and (not isinstance(fp, str) or not _FP.fullmatch(fp)):
        raise RegistryError(f"account {name!r}: fp must be null or {FP_LEN} lowercase hex digits",
                            EXIT_USAGE)
    return Account(name, email, parsed, fp)


@dataclass
class Registry:
    """The accounts in registry order. Mutators keep order and refuse a
    duplicate or an unknown name; nothing is written until `save`."""

    accounts: list[Account] = field(default_factory=list)
    version: int = VERSION

    def names(self) -> list[str]:
        return [account.name for account in self.accounts]

    def get(self, name: str) -> Account:
        for account in self.accounts:
            if account.name == name:
                return account
        known = ", ".join(self.names()) or "none registered"
        raise RegistryError(f"no account named {name!r} (known: {known})", EXIT_USAGE)

    def find(self, name: str) -> Account | None:
        return next((account for account in self.accounts if account.name == name), None)

    def by_fp(self, fp: str | None) -> Account | None:
        """The account whose fingerprint is `fp`, or None: an unknown fp is
        `unknown`, never a guess."""
        if fp is None:
            return None
        return next((account for account in self.accounts if account.fp == fp), None)

    def add(self, account: Account) -> None:
        if self.find(account.name) is not None:
            raise RegistryError(f"an account named {account.name!r} is already registered",
                                EXIT_USAGE)
        self.accounts.append(account)

    def remove(self, name: str) -> Account:
        account = self.get(name)
        self.accounts.remove(account)
        return account

    def replace(self, account: Account) -> None:
        """Swap in a new value for an existing name, in place (order kept)."""
        old = self.get(account.name)
        self.accounts[self.accounts.index(old)] = account

    def to_json(self) -> dict:
        return {"version": self.version,
                "accounts": [account.to_json() for account in self.accounts]}


def from_json(data: object, where: str = "registry") -> Registry:
    """A Registry from parsed JSON, checked whole: the right version, a list
    of objects with exactly the known keys, no duplicate names."""
    if not isinstance(data, dict):
        raise RegistryError(f"{where}: top level must be an object")
    unknown = sorted(set(data) - {"version", "accounts"})
    if unknown:
        raise RegistryError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}")
    version = data.get("version")
    if type(version) is not int or version != VERSION:     # `true == 1` in Python
        raise RegistryError(f"{where}: version must be {VERSION}, not {data.get('version')!r}")
    rows = data.get("accounts")
    if not isinstance(rows, list):
        raise RegistryError(f"{where}: `accounts` must be a list")
    registry = Registry()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise RegistryError(f"{where}: accounts[{index}] must be an object")
        extra = sorted(set(row) - set(FIELDS))
        if extra:
            raise RegistryError(f"{where}: accounts[{index}] has unknown key(s) "
                                f"{', '.join(map(repr, extra))}")
        sources = row.get("sources")
        if not isinstance(sources, list):
            raise RegistryError(f"{where}: accounts[{index}].sources must be a list")
        try:
            account = make_account(row.get("name"), row.get("email"), sources, row.get("fp"))
            registry.add(account)
        except RegistryError as error:
            raise RegistryError(f"{where}: accounts[{index}]: {error}") from None
    return registry


# --- the file ------------------------------------------------------------------


def _refuse_unprivate(path: Path) -> None:
    why = _private_check(str(path))
    if why is not None:
        raise RegistryError(f"registry {path} refused: {why} (fix it by hand; "
                            "it is never rewritten)")
    # A symlinked registry is refused too: `save` renames
    # a temp file over the target, which would replace the link with a file
    # and leave the link's target stale, so `load` refuses it as well.
    if path.is_symlink():
        raise RegistryError(f"registry {path} refused: it is a symlink (make it a "
                            "real file; it is never rewritten)")


def exists(path: str | os.PathLike | None = None) -> bool:
    return os.path.lexists(default_path() if path is None else path)


def load(path: str | os.PathLike | None = None, *, missing_ok: bool = True) -> Registry:
    """Read the registry. A missing file is an empty registry (or, with
    `missing_ok=False`, exit 5). Anything else that is not a private regular
    file holding a valid registry is refused, exit 5. Never writes."""
    target = default_path() if path is None else Path(path)
    if not os.path.lexists(target):
        if missing_ok:
            return Registry()
        raise RegistryError(f"no registry at {target}")
    _refuse_unprivate(target)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise RegistryError(f"registry {target} could not be read "
                            f"({getattr(error, 'strerror', None) or type(error).__name__})") from None
    if _TOKEN_SHAPE.search(text):
        raise RegistryError(f"registry {target} refused: it holds a token-shaped string "
                            "(a registry holds names, never tokens; the value is not shown)")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise RegistryError(f"registry {target} is not valid JSON "
                            f"(line {error.lineno}, column {error.colno})") from None
    return from_json(data, str(target))


def render(registry: Registry) -> str:
    return json.dumps(registry.to_json(), indent=2) + "\n"


def save(registry: Registry, path: str | os.PathLike | None = None) -> Path:
    """Write the registry atomically, mode 0600: a temp file in the same
    directory, fsynced, then renamed over the target. The parent is made
    0700 if missing.

    An existing target that `load` would refuse is refused here too, exit 5,
    and left byte-identical: a bad file is shown to a person, never
    silently replaced."""
    target = default_path() if path is None else Path(path)
    text = render(registry)
    if _TOKEN_SHAPE.search(text):     # make_account refuses this already; a backstop
        raise RegistryError("refusing to write a registry holding a token-shaped string")
    if os.path.lexists(target):
        load(target)                  # raises on a bad mode or bad JSON; writes nothing
    try:
        make_private_dirs(target.parent)
    except OSError as error:
        raise RegistryError(f"registry {target} not written: its folder could not be made "
                            f"({error.strerror or type(error).__name__}"
                            f"{', ' + str(error.filename) if error.filename else ''})"
                            ) from None
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return target


# --- resolving a token (in memory only) -----------------------------------------


@dataclass(frozen=True)
class Resolved:
    """A token and where it came from. The token is kept out of the repr,
    so no traceback or log line can print it (as `secrets.SecretResult`)."""

    account: str
    source: Source
    token: str = field(repr=False)


def resolve(account: Account, backends: Mapping[str, Backend] | None = None) -> Resolved:
    """The first source of `account` that resolves, in registry order.
    Every source failing is exit 4, naming each source and its reason.
    The token lives only in the returned value."""
    table = BACKENDS if backends is None else backends
    reasons = []
    for source in account.sources:
        resolver = table.get(source.backend)
        if resolver is None:
            reasons.append(f"{source}: unknown backend")
            continue
        try:
            token = resolver(source.ref)
        except SecretError as error:
            reasons.append(f"{source}: {error}")
            continue
        return Resolved(account.name, source, token)
    raise RegistryError(f"account {account.name!r}: no source resolved ("
                        + "; ".join(reasons) + ")", EXIT_UNRESOLVED)
