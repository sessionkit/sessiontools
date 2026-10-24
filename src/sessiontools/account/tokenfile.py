"""The host's token file: the other place a host's sign-in can live.

Some hosts do not sign in from the Keychain item. Their login shell
exports `CLAUDE_CODE_OAUTH_TOKEN`, read from a private file, and every seat
launched from that shell carries it. On such a host `use` swaps the file,
and never touches the Keychain.

**Which route a host is on is a setting, never a guess.** The `session`
setting `account.token_file` names the file, through the template and host
layers of the settings cascade (`~/.sessionkit/tools/settings/session.json`, or a
file under `session.d/`), the way `new` reads `account.passage_prefix`. It
has NO default: unset, the host's sign-in is the Keychain item. A default
path would name some host's own layout, or a file nobody's shell reads, and
a file found there would be a guess.

The value must be a path, absolute once `~` is expanded. A symlink is
followed: the swap happens in the TARGET's directory, so the link, and the
profile that reads it, keep working.

The file itself must be a private regular file (owned by this user, mode
0600, as a `file:` source must be) holding exactly ONE token line: one
non-empty line with no surrounding whitespace, and at most one trailing
newline. Anything else is refused, because a shell that reads the file
whole would see bytes a swap would drop.

`TokenFile` holds the pieces `use` builds its swap from: read, stage the
new token in a temp file beside the target, check the staged file back
through the `file:` resolver, commit it with `os.replace` and a directory
fsync, discard it, restore a backup, and the manual restore line. It holds
no flow.

No message, repr or exception text here carries a token, a file's content
or a fingerprint. Exit code 5 is a refused setting or file.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path

from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.start.secrets import SecretError

EXIT_TOKEN_FILE = reg.EXIT_REGISTRY      # 5

#: The settings tool and dotted key that name the host's token file:
#: `{"account": {"token_file": "..."}}` in the `session` tool's template or
#: host layer. No default.
SETTING_TOOL = "session"
SETTING_KEY = "account.token_file"
#: Where the person sets it, for a message.
SETTING_WHERE = ("the `session` settings cascade: ~/.sessionkit/tools/settings/session.json "
                 "or a file in ~/.sessionkit/tools/settings/session.d/")

#: A token file is one line; anything this long is not one.
MAX_BYTES = 4096
#: Staged files: `.<target name>.use-XXXXXX`, beside the target.
STAGE_INFIX = ".use-"

_WANTED = "it must be the path of the host's token file, absolute or starting with ~"


class TokenFileError(Exception):
    """A refused setting or file, or a step on the file that failed. The
    message names a key, a path or a step, never a value. `code` is the
    exit code."""

    def __init__(self, message: str, code: int = EXIT_TOKEN_FILE) -> None:
        super().__init__(message)
        self.code = code


# --- the setting ---------------------------------------------------------------------


def _token_shaped(text: str) -> bool:
    return "sk-ant-" in text or bool(reg._TOKEN_SHAPE.search(text))


def check_setting(value: object, where: str = SETTING_KEY,
                  home: str | os.PathLike | None = None) -> Path:
    """VALUE as the token file's path: a string, printable, not token-shaped,
    absolute once `~` is expanded (against HOME when given). Else exit 5,
    naming WHERE (the key and the file that set it); a token-shaped value is
    never quoted, and any other is quoted only when it is short."""
    if not isinstance(value, str):
        raise TokenFileError(f"{where} is a {type(value).__name__}: " + _WANTED)
    if _token_shaped(value):
        raise TokenFileError(f"{where} looks like a token, not a path (the value is not "
                             "shown): " + _WANTED)
    if not value:
        raise TokenFileError(f"{where} is empty: " + _WANTED)
    if not value.isprintable():
        raise TokenFileError(f"{where} holds unprintable characters (not shown): " + _WANTED)
    shown = repr(value) if len(value) <= 120 else "a long string"
    if home is not None and (value == "~" or value.startswith("~/")):
        path = os.fspath(home) + value[1:]
    else:
        path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise TokenFileError(f"{where} is {shown}, not an absolute path: " + _WANTED)
    return Path(path)


def token_file_setting(environ: Mapping[str, str] | None = None,
                       home: str | os.PathLike | None = None) -> tuple[Path, str] | None:
    """(path, origin) from SETTING_KEY through the `session` tool's template
    and host layers, or None when no layer sets it. HOME, when given, is the
    home those layers are read under (and `~` expands to). A layer file the
    reader refuses, or a bad value, is exit 5."""
    found = _setting_value(environ, home)
    if found is None:
        return None
    node, origin, home_dir = found
    return check_setting(node, f"{SETTING_KEY} (in {origin})", home_dir), origin


def token_file_is_set(environ: Mapping[str, str] | None = None,
                      home: str | os.PathLike | None = None) -> bool:
    """Whether some layer sets SETTING_KEY at all, whatever its value. A
    value `check_setting` refuses (relative, empty, not a string) still
    puts the host on the token-file route: the setting is a statement of
    the route, and a bad path in it does not make the host a Keychain
    host. A layer the reader refuses is exit 5, as in `token_file_setting`."""
    return _setting_value(environ, home) is not None


def _setting_value(environ: Mapping[str, str] | None,
                   home: str | os.PathLike | None) -> tuple[object, str, str | None] | None:
    """(raw value, origin, HOME) of SETTING_KEY through the `session` tool's
    template and host layers, unchecked; None when no layer sets it."""
    from sessiontools import settings

    env = dict(os.environ if environ is None else environ)
    if home is not None:
        env["HOME"] = str(home)
    try:
        merged, origins, _locked, _leaves = settings.merge(
            settings.sources(SETTING_TOOL, None, env=env))
    except (settings.SettingsError, OSError, ValueError) as error:
        raise TokenFileError(f"settings: {error}") from None
    node: object = merged
    for part in SETTING_KEY.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    if node is None:
        return None
    return node, origins.get(SETTING_KEY, SETTING_TOOL), env.get("HOME")


# --- the one-token-line rule ---------------------------------------------------------


def parse_token_line(blob: bytes, what: str = "the token file") -> str:
    """The one token in BLOB: one non-empty line with no surrounding
    whitespace, at most one trailing newline, UTF-8, printable. Else exit 5;
    the reason never quotes the content."""
    if len(blob) > MAX_BYTES:
        raise TokenFileError(f"{what} is {len(blob)} bytes, too long to be one token "
                             "line (content not shown)")
    try:
        text = bytes(blob).decode("utf-8")
    except UnicodeDecodeError:
        raise TokenFileError(f"{what} is not UTF-8 (content not shown)") from None
    text = text.removesuffix("\n")
    if not text:
        raise TokenFileError(f"{what} is empty")
    if "\n" in text or "\r" in text:
        raise TokenFileError(f"{what} holds more than one line (content not shown); "
                             "use swaps a file of exactly one token line")
    if text != text.strip() or not text.isprintable():
        raise TokenFileError(f"{what} is not one bare token line: it has surrounding "
                             "whitespace or unprintable characters (content not shown)")
    return text


def render(token: str) -> bytes:
    """A token file's bytes for TOKEN: the token and one newline."""
    if not isinstance(token, str):
        raise TokenFileError("a token must be a string (value not shown)")
    blob = (token + "\n").encode("utf-8")
    parse_token_line(blob, "the new token")
    return blob


# --- the file --------------------------------------------------------------------------


class TokenFile:
    """The host's token file at PATH (a link is followed to its target).
    ORIGIN, when known, is the settings file that named it."""

    def __init__(self, path: str | os.PathLike, origin: str | None = None) -> None:
        self.path = Path(os.path.expanduser(os.fspath(path)))
        self.origin = origin

    @property
    def target(self) -> Path:
        """The file the path ends at, links followed: where a swap happens."""
        return Path(os.path.realpath(self.path))

    def describe(self) -> str:
        """Which file this is, for a plan or an error. Never its content."""
        text = f"token file {self.path}"
        if self.target != self.path:
            text += f" (a link to {self.target})"
        return text

    def __repr__(self) -> str:
        return f"TokenFile({str(self.path)!r})"

    # -- checks and reads --

    def check(self) -> None:
        """The file exists and is a private regular file of ours, by stat
        alone (nothing is read). Else exit 5: `use` does not create a
        host's sign-in, and a file another user can read is already leaked."""
        target = self.target
        try:
            info = os.stat(target)
        except FileNotFoundError:
            raise TokenFileError(f"{self.describe()} does not exist: use does not create a "
                                 f"host's sign-in (check {SETTING_KEY})") from None
        except OSError as error:
            raise TokenFileError(f"{self.describe()}: cannot stat it "
                                 f"({error.strerror or type(error).__name__})") from None
        why = reg._private_info(info)
        if why is not None:
            raise TokenFileError(f"{self.describe()} refused: {why}")

    def read_bytes(self) -> bytes:
        """The file's bytes, if it passes `check` as OPENED (fstat, no link
        followed at the open: the target was resolved first) and holds one
        token line. Else exit 5. O_NONBLOCK: a FIFO cannot hang it."""
        target = self.target
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise TokenFileError(f"{self.describe()} does not exist: use does not create a "
                                 f"host's sign-in (check {SETTING_KEY})") from None
        except OSError as error:
            raise TokenFileError(f"{self.describe()}: cannot open it "
                                 f"({error.strerror or type(error).__name__})") from None
        try:
            why = reg._private_info(os.fstat(fd))
            if why is not None:
                raise TokenFileError(f"{self.describe()} refused: {why}")
            chunks, size = [], 0
            while size <= MAX_BYTES:
                chunk = os.read(fd, MAX_BYTES + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        except OSError as error:
            raise TokenFileError(f"{self.describe()} could not be read "
                                 f"({error.strerror or type(error).__name__})") from None
        finally:
            os.close(fd)
        blob = b"".join(chunks)
        parse_token_line(blob, self.describe())
        return blob

    def read(self) -> str:
        """The one token the file holds (in memory only). Else exit 5."""
        return parse_token_line(self.read_bytes(), self.describe())

    # -- the swap's pieces --

    def _stage_bytes(self, blob: bytes) -> Path:
        target = self.target
        try:
            fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}{STAGE_INFIX}")
        except OSError as error:
            raise TokenFileError(f"could not stage a file beside {target} "
                                 f"({error.strerror or type(error).__name__})") from None
        staged = Path(name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1                                   # the handle owns it now
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException as error:
            if fd >= 0:
                os.close(fd)
            self.discard(staged)
            if isinstance(error, OSError):
                raise TokenFileError(f"could not stage a file beside {target} "
                                     f"({error.strerror or type(error).__name__})") from None
            raise
        return staged

    def stage(self, token: str) -> Path:
        """TOKEN, as a token file, in a new temp file beside the target
        (same directory, so the commit is one rename), mode 0600, fsynced.
        The target is not touched. Returns the staged path."""
        return self._stage_bytes(render(token))

    def check_staged(self, staged: Path, token: str) -> None:
        """Read STAGED back through the `file:` resolver, and compare it to
        TOKEN without printing or timing either. Else exit 5."""
        try:
            back = reg.file_backend(str(staged))
        except SecretError as error:
            raise TokenFileError(f"the staged file did not read back: {error}") from None
        if not reg.same_token(back, token):
            raise TokenFileError(f"the staged file {staged} did not read back as the new "
                                 "token")

    def commit(self, staged: Path) -> None:
        """Put STAGED in the target's place: `os.replace` (atomic: a reader
        sees the old file or the new, never a part), then fsync the
        directory so the rename survives a crash. A link at the configured
        path is kept: only its target is replaced."""
        target = self.target
        if Path(staged).parent != target.parent:
            raise TokenFileError(f"the staged file {staged} is not beside {target}; "
                                 "nothing was replaced")
        try:
            os.replace(staged, target)
        except OSError as error:
            raise TokenFileError(f"could not replace {target} "
                                 f"({error.strerror or type(error).__name__})") from None
        try:
            dfd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as error:
            raise TokenFileError(f"{target} was replaced, but its directory could not be "
                                 f"synced ({error.strerror or type(error).__name__})") from None

    def discard(self, staged: Path | None) -> None:
        """Remove STAGED if it is there. Never raises: it runs on the way
        out of a failure, which must not be hidden by a second one."""
        if staged is None:
            return
        try:
            Path(staged).unlink(missing_ok=True)
        except OSError:
            pass

    def restore(self, backup: bytes) -> None:
        """Put BACKUP (the file's bytes as they were) back: staged, committed,
        then read back byte for byte. Else exit 5, the target as it is."""
        parse_token_line(backup, "the backup")
        staged = self._stage_bytes(bytes(backup))
        try:
            self.commit(staged)
        except TokenFileError:
            self.discard(staged)
            raise
        if self.read_bytes() != bytes(backup):
            raise TokenFileError(f"{self.target} did not read back as the backup")

    def restore_line(self, backup: str | os.PathLike) -> str:
        """The manual restore, by hand: paths only, both shell-quoted."""
        return (f"install -m 600 {shlex.quote(os.fspath(backup))} "
                f"{shlex.quote(str(self.target))}")


# --- the route --------------------------------------------------------------------------


def host_route(environ: Mapping[str, str] | None = None,
               home: str | os.PathLike | None = None, *,
               keychain: kc.Keychain | None = None) -> kc.Keychain | TokenFile:
    """The host's sign-in, by the `session` setting `account.token_file`:
    unset, the Keychain item (KEYCHAIN, else the real one); set, that file,
    once it passes `TokenFile.check` (it exists, a private regular file of
    ours; nothing is read). A bad value or file is exit 5. Nothing here
    runs `security`. `use` swaps by it and `status` reads by it."""
    found = token_file_setting(environ, home)
    if found is None:
        return kc.SecurityCLI() if keychain is None else keychain
    path, origin = found
    route = TokenFile(path, origin)
    route.check()
    return route
