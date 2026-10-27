"""Read a launched session's identity block: `session identity [SID]`.

`session start` writes ONE file per launched session, `identity.md` in the
session's dir (`sessiontools.start.boot_identity`, run in the pane before
the vendor starts). This module is its reader, and the only one: the
`session identity` verb prints it, and the session-info hook's `identity`
verb hands it to the agent as session-start context. Both go through
`settings.session_dir`, never a path of their own, so a later move of the
sessions root moves every reader with it.

THE CONTRACT (sessionmail's `memory wake` runs the verb and prints its
stdout verbatim):

    session identity [SID]     SID defaults to $CLAUDE_SESSION_ID

    exit 0   the block on stdout, byte for byte; nothing on stderr
    exit 3   no block for that session (not launched by `session start`,
             or launched before the block existed): one stderr line,
             nothing on stdout
    other    a fault (no SID, a SID that is not a session id, an ambiguous
             short id, an unreadable file): one stderr line, nothing on
             stdout. 2 is a usage fault, 1 any other.

Bounded and fast: one file read, no network, no subprocess. A file larger
than MAX_BYTES is a fault, never a partial block.

Stdlib only, and nothing from `sessiontools.start` (which loads PyYAML):
the hook imports this from its own python, and `import sessiontools.cli`
must never load yaml.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from sessiontools import settings

PROG = "session identity"

#: The block's file name in the session dir. The writer
#: (`sessiontools.start.boot_identity`) uses this same name.
IDENTITY_FILE = "identity.md"

#: The env var the verb's SID defaults to.
SID_ENV = "CLAUDE_SESSION_ID"

#: The block is at most 20 short lines; anything past this is not a block.
MAX_BYTES = 16 * 1024

EXIT_OK = 0
EXIT_FAULT = 1
EXIT_USAGE = 2
EXIT_NO_BLOCK = 3


class IdentityError(Exception):
    """A fault, or no block: CODE is the verb's exit, the message one line."""

    def __init__(self, message: str, code: int = EXIT_FAULT):
        super().__init__(" ".join(str(message).split()))
        self.code = code


class NoBlock(IdentityError):
    """No identity block for the session: exit 3."""

    def __init__(self, message: str):
        super().__init__(message, EXIT_NO_BLOCK)


def valid_sid(sid: str) -> bool:
    """SID can name a directory under the sessions root, and nothing else:
    non-empty, no path separator, no NUL, not `.` or `..`."""
    return bool(sid) and sid not in (".", "..") and "/" not in sid \
        and "\0" not in sid and (os.altsep is None or os.altsep not in sid)


def identity_path(sid: str, env: Mapping[str, str] | None = None) -> Path:
    """`<session dir>/identity.md` for a FULL session id, by the resolver."""
    return settings.session_dir(sid, env) / IDENTITY_FILE


def read_block(sid: str, env: Mapping[str, str] | None = None) -> bytes:
    """The block's bytes for the FULL session id SID. Raises NoBlock when the
    session has none, IdentityError for any other fault. One open, one read."""
    if not valid_sid(sid):
        raise IdentityError(f"not a session id: {sid!r}", EXIT_USAGE)
    path = identity_path(sid, env)
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        raise NoBlock(f"no identity block for session {sid} "
                      f"(not launched by session start, or launched before it wrote one)"
                      ) from None
    except OSError as e:
        raise IdentityError(f"cannot read {path}: {e.strerror or e}") from None
    if len(data) > MAX_BYTES:
        raise IdentityError(f"{path} is larger than {MAX_BYTES} bytes: not an identity block")
    return data


def resolve(sid: str, env: Mapping[str, str] | None = None) -> str:
    """The full session id for SID, which may be a short prefix (the
    resolver's rule: one match is that id, more than one is a fault)."""
    if not valid_sid(sid):
        raise IdentityError(f"not a session id: {sid!r}", EXIT_USAGE)
    try:
        full, _note = settings.resolve_sid(sid, env)
    except settings.SettingsError as e:
        raise IdentityError(str(e)) from None
    return full


class _Parser(argparse.ArgumentParser):
    """A usage error is ONE stderr line and exit 2: the contract's fault
    shape, which argparse's usage-then-error pair is not."""

    def error(self, message: str):  # type: ignore[override]
        self.exit(EXIT_USAGE, f"{self.prog}: {message} (see --help)\n")


EPILOG = """\
exit 0: the block, byte for byte, on stdout.
exit 3: no block for that session (it was not launched by `session start`,
        or was launched before it wrote one); one line on stderr.
other:  a fault (2 usage, 1 anything else); one line on stderr.
One file read: no network, no subprocess."""


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog=PROG, epilog=EPILOG,
                formatter_class=argparse.RawDescriptionHelpFormatter,
                description="Print the identity block `session start` wrote for a "
                            "session: who it is, as it was at launch.")
    p.add_argument("sid", nargs="?", metavar="SID",
                   help=f"the session id, or a unique prefix of it (default: ${SID_ENV})")
    return p


def _fail(message: str, code: int, stderr) -> int:
    stderr.write(f"{PROG}: {message}\n")
    return code


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None,
         stdout=None, stderr=None) -> int:
    """`session identity [SID]`. Returns the exit code (see the module doc)."""
    env = os.environ if env is None else env
    stderr = sys.stderr if stderr is None else stderr
    a = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    sid = a.sid if a.sid is not None else env.get(SID_ENV, "")
    if not sid:
        return _fail(f"no session id: name one, or set ${SID_ENV}", EXIT_USAGE, stderr)
    try:
        data = read_block(resolve(sid, env), env)
    except IdentityError as e:
        return _fail(str(e), e.code, stderr)
    out = sys.stdout.buffer if stdout is None else stdout
    try:
        out.write(data)
        out.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return EXIT_OK
