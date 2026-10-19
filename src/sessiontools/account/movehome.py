"""`python -m sessiontools.account.movehome [--home H] [--apply]`: move the
account folder into the kit's home (the account-home move).

The old folder is `registry.old_accounts_dir()` (HOME + OLD_ACCOUNTS_SUBDIR);
the new one is `registry.accounts_dir()`, `~/.sessionkit/tools/accounts/`, the
ONE helper every account path derives from. `session account` reads the new
folder only, and while the old one holds files and the new one does not it
says the accounts are stranded and refuses to write. This module is the
move, run once per host at the operator's word. It is never a side effect of
an upgrade, and it is not a `session` verb: `session` stays the one console
script, and the old folder exists only on hosts that ran account switching
before it moved.

A DRY RUN by default; `--apply` acts. In order, each step passes or exits:

1. The old folder absent, or empty: "nothing to move", exit 0. Anything at
   the new path (a folder, even an empty one, a file, a link): REFUSED,
   exit 3, nothing written. A symlink or a file at the old path: REFUSED,
   exit 3.
2. The manifest of the old folder: every entry's relative path, kind,
   mode, size and sha256, walked without following a link. A symlink or a
   special file inside: REFUSED, exit 3. Each entry is printed as
   `path mode size`, then the count and the byte total; a digest is never
   printed, and a file's content never read out. A file not 0600, or a
   folder not 0700, is a WARNING; it is moved as it is, never repaired.
3. `--apply`: the new folder's missing parents are made 0700, then ONE
   `os.rename(old, new)`: atomic on one filesystem, modes and mtimes
   carried. A rename that fails (EXDEV: another filesystem) is REFUSED,
   exit 3; nothing was copied, and the parents it made are removed.
4. The manifest of the new folder, compared with step 2's (paths, kinds,
   modes, sizes, sha256). Equal: `verified: N files, B bytes, sha256 equal`,
   exit 0. Not: renamed back, exit 4, naming the differing paths only.

Every look is the strict stat: a path that cannot be looked at is
never "absent". An old or new folder, or any entry, that cannot be stat'd,
listed or read is exit 6, naming the path. Nothing else is touched: not
`~/.config` itself, not the settings tree, not mail.

Exit codes: 0 moved and verified, or nothing to move, or the dry run; 2
usage; 3 refused, nothing moved; 4 the verify failed (renamed back); 6 a
path could not be looked at.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from sessiontools import settings
from sessiontools.account import registry as reg

PROG = f"python -m {reg.MOVER_MODULE}"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_VERIFY = 4
EXIT_UNREADABLE = 6

FILE_MODE = 0o600
DIR_MODE = 0o700
_CHUNK = 1 << 16


class MoveError(Exception):
    """A refusal or a failure. The message names paths, never content."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Entry:
    """One manifest row. `sha256` stays out of the repr: a token file's
    digest is never printed."""

    path: str                  # relative to the folder; "." is the folder itself
    kind: str                  # "dir" or "file"
    mode: int
    size: int
    sha256: str | None = field(default=None, repr=False)

    def line(self) -> str:
        name = _shown_name(self.path) + ("/" if self.kind == "dir" else "")
        size = "-" if self.kind == "dir" else str(self.size)
        return f"  {name}  {self.mode:04o}  {size}"

    def warning(self) -> str | None:
        want = DIR_MODE if self.kind == "dir" else FILE_MODE
        if self.mode == want:
            return None
        return (f"warning: {_shown_name(self.path)} is mode {self.mode:04o}, not "
                f"{want:04o} (moved as it is; not repaired)")


def _shown_name(name: str) -> str:
    """NAME as printed: a token-shaped run in a file name is masked."""
    return reg._TOKEN_SHAPE.sub("<token-shaped, not shown>", name)


def _unreadable(error: OSError, path: Path, what: str) -> MoveError:
    return MoveError(f"{path} could not be {what} ({error.strerror or type(error).__name__}): "
                     "it is never read as absent; nothing was moved", EXIT_UNREADABLE)


def _lstat(path: Path) -> os.stat_result | None:
    """PATH's lstat, or None where it is ABSENT (`settings._ABSENT`, the
    strict-stat rule). Any other error: exit 6, naming the path."""
    try:
        return os.lstat(path)
    except OSError as error:
        if error.errno in settings._ABSENT:
            return None
        raise _unreadable(error, path, "looked at") from None
    except ValueError:
        return None


def _list(directory: Path) -> list[str]:
    try:
        return sorted(os.listdir(directory))
    except OSError as error:
        raise _unreadable(error, directory, "listed") from None


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                sha.update(chunk)
    except OSError as error:
        raise _unreadable(error, path, "read") from None
    return sha.hexdigest()


def manifest(root: Path) -> list[Entry]:
    """Every entry under ROOT, ROOT itself first as ".", in name order.
    Links are never followed: a symlink or a special file is exit 3."""
    entries: list[Entry] = []

    def visit(path: Path, rel: str) -> None:
        info = _lstat(path)
        if info is None:
            raise MoveError(f"{path} went away while it was being read; nothing was moved",
                            EXIT_UNREADABLE)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            raise MoveError(f"{path} is a symlink: the mover never follows or moves one; "
                            "nothing was moved (resolve it by hand, then re-run)",
                            EXIT_REFUSED)
        if stat.S_ISDIR(info.st_mode):
            entries.append(Entry(rel, "dir", mode, 0))
            for name in _list(path):
                visit(path / name, name if rel == "." else f"{rel}/{name}")
        elif stat.S_ISREG(info.st_mode):
            entries.append(Entry(rel, "file", mode, info.st_size, _digest(path)))
        else:
            raise MoveError(f"{path} is not a file or a folder; nothing was moved",
                            EXIT_REFUSED)

    visit(root, ".")
    return entries


def _differing(before: list[Entry], after: list[Entry]) -> list[str]:
    a = {entry.path: entry for entry in before}
    b = {entry.path: entry for entry in after}
    return sorted(path for path in a.keys() | b.keys() if a.get(path) != b.get(path))


def _missing_parents(directory: Path) -> list[Path]:
    """DIRECTORY and its ancestors that are absent, deepest first."""
    missing = []
    here = directory
    while _lstat(here) is None and here.parent != here:
        missing.append(here)
        here = here.parent
    return missing


def move(home: str | os.PathLike | None = None, *, apply: bool = False,
         rename: Callable[[Path, Path], None] = os.rename,
         out: TextIO | None = None, err: TextIO | None = None) -> int:
    """The move (module doc, steps 1-4). Returns the exit code. `rename` is
    a seam for the verify's test; production's is `os.rename`."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        return _move(home, apply=apply, rename=rename, out=out)
    except MoveError as error:
        print(f"{PROG}: {error}", file=err)
        return error.code


def _move(home, *, apply: bool, rename: Callable[[Path, Path], None], out: TextIO) -> int:
    env = reg._env(home)
    old, new = reg.old_accounts_dir(home), reg.accounts_dir(home)

    def show(path: Path) -> str:
        return settings._short(str(path), env) + "/"

    # 1. the two folders
    info = _lstat(old)
    if info is None:
        print(f"nothing to move: {show(old)} does not exist", file=out)
        return EXIT_OK
    if stat.S_ISLNK(info.st_mode):
        raise MoveError(f"{show(old)} is a symlink: the mover never follows or moves one; "
                        "nothing was moved", EXIT_REFUSED)
    if not stat.S_ISDIR(info.st_mode):
        raise MoveError(f"{show(old)} is not a folder; nothing was moved", EXIT_REFUSED)
    if not _list(old):
        print(f"nothing to move: {show(old)} is empty", file=out)
        return EXIT_OK
    if _lstat(new) is not None:
        raise MoveError(f"both folders exist, {show(old)} and {show(new)}: refused, nothing "
                        "was written. Merge them by hand, then remove the old one",
                        EXIT_REFUSED)

    # 2. the manifest
    before = manifest(old)
    files = [entry for entry in before if entry.kind == "file"]
    total = sum(entry.size for entry in files)
    print(f"{show(old)} -> {show(new)}", file=out)
    for entry in before:
        print(entry.line(), file=out)
    print(f"{len(files)} file(s), {total} byte(s)", file=out)
    for entry in before:
        warning = entry.warning()
        if warning:
            print(warning, file=out)
    if not apply:
        print(f"dry run: nothing was written. To move, add --apply: {reg.mover_command()} "
              "--apply", file=out)
        return EXIT_OK

    # 3. the parents, then one rename
    made = _missing_parents(new.parent)
    try:
        reg.make_private_dirs(new.parent)
    except settings.SettingsUnreadable as error:
        raise _unreadable(error, Path(error.filename or new.parent), "looked at") from None
    except OSError as error:
        _remove_made(made)
        raise MoveError(f"{show(new.parent)} could not be made "
                        f"({error.strerror or type(error).__name__}); nothing was moved",
                        EXIT_REFUSED) from None
    try:
        rename(old, new)
    except OSError as error:
        _remove_made(made)
        why = ("another filesystem" if error.errno == errno.EXDEV
               else error.strerror or type(error).__name__)
        raise MoveError(f"{show(old)} could not be renamed to {show(new)} ({why}); nothing "
                        "was copied or moved", EXIT_REFUSED) from None

    # 4. verify
    after = manifest(new)
    differ = _differing(before, after)
    if differ:
        try:
            rename(new, old)
            where = f"renamed back to {show(old)}"
        except OSError as error:
            where = (f"and the rename back FAILED ({error.strerror or type(error).__name__}): "
                     f"the files are in {show(new)}")
        raise MoveError(f"the verify failed on {', '.join(map(_shown_name, differ))}; {where}",
                        EXIT_VERIFY)
    print(f"moved {show(old)} to {show(new)}\n"
          f"verified: {len(files)} file(s), {total} byte(s), sha256 equal", file=out)
    return EXIT_OK


def _remove_made(made: list[Path]) -> None:
    """Remove the parents this run made (deepest first), if still empty."""
    for path in made:
        try:
            os.rmdir(path)
        except OSError:
            pass


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None,
         err: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Move the account folder into ~/.sessionkit/tools/accounts/. A dry run "
                    "unless --apply. Prints paths, modes and sizes, never a token or a digest.")
    parser.add_argument("--home", metavar="H", help="the home to move in (default: $HOME)")
    parser.add_argument("--apply", action="store_true", help="move (else: the dry run)")
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_USAGE
    return move(args.home, apply=args.apply, out=out, err=err)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
