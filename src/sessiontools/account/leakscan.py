"""Find a leaked token without ever holding one to grep for.

A plain `grep TOKEN` needs the token in argv, which is the leak it hunts.
This scan instead finds every TOKEN-SHAPED string (`sk-ant-o<kind><NN>-`
then 20 or more of `[A-Za-z0-9_-]`), hashes each one in memory with the
registry's own `fingerprint` (`sha256[:16]`), and compares the hash with
the fingerprints the registry already holds. What it reports is a PLACE:

    path:line: token of account 'c' (registry fp match)
    path:line: token-shaped string (not in registry)

never the value, and never the value's fingerprint. A registry hit is a
leak of a token we know; an unknown shape is something token-like that the
registry cannot name, reported so a person can look.

The verify step runs it over the repo and the test tmp dirs:

    python -m sessiontools.account.leakscan [--strict] [--registry PATH] PATH...

Exit 0 is clean; 1 means a registry token was found (or, with `--strict`,
any token-shaped string); 2 is usage; 5 is a refused registry. It adds no
console script (`session` stays the only one).

Reading: files are scanned as bytes, so a binary file or a bad encoding
cannot hide a token. Directories are walked without following symlinks;
a symlinked file is skipped (it is scanned where it lives), and so are the
directories in `SKIP_DIRS`. A file that cannot be read is reported as
`unreadable`, and the scan goes on.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from sessiontools.account import registry as reg

#: The token's shape, as bytes. `oat01` (a setup token) and `ort01`
#: (a refresh token) both match; so does the synthetic `oat01-TEST-...`.
TOKEN = re.compile(rb"sk-ant-o[a-z]+\d\d-[A-Za-z0-9_-]{20,}")
#: The same, hex-encoded: `keychain.SecurityCLI` feeds the item to
#: `security -i` as hex, so a leaked write script holds the token in this
#: form. `sk-ant-o` is `736b2d616e742d6f`; the run is decoded, then matched.
HEX_TOKEN = re.compile(rb"736b2d616e742d6f(?:[0-9a-fA-F]{2})+", re.IGNORECASE)

#: Not walked: VCS internals, environments and caches, never our output.
SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__",
                       ".mypy_cache", ".pytest_cache", ".ruff_cache"})

EXIT_CLEAN = 0
EXIT_FOUND = 1
EXIT_USAGE = 2
EXIT_REGISTRY = reg.EXIT_REGISTRY       # 5


@dataclass(frozen=True)
class Hit:
    """One token-shaped string, by place. `account` is the registry name
    its fingerprint matched, or None. There is no field for the value."""

    path: str
    line: int
    account: str | None

    def text(self) -> str:
        if self.account is not None:
            return f"{self.path}:{self.line}: token of account {self.account!r} " \
                   "(registry fp match)"
        return f"{self.path}:{self.line}: token-shaped string (not in registry)"


@dataclass(frozen=True)
class Report:
    hits: tuple[Hit, ...]
    unreadable: tuple[tuple[str, str], ...]     # (path, reason)
    scanned: int

    @property
    def known(self) -> list[Hit]:
        return [hit for hit in self.hits if hit.account is not None]

    @property
    def unknown(self) -> list[Hit]:
        return [hit for hit in self.hits if hit.account is None]


def _fp(match: bytes) -> str:
    """The registry's `fingerprint`, over bytes (a token is ASCII)."""
    return hashlib.sha256(match).hexdigest()[:reg.FP_LEN]


def fps_of(registry: reg.Registry) -> dict[str, str]:
    """`{fp: name}` for every account whose fp the registry knows."""
    return {account.fp: account.name for account in registry.accounts if account.fp}


def scan_bytes(data: bytes, fps: Mapping[str, str], where: str = "<text>") -> list[Hit]:
    """Every token-shaped string in `data`, as a Hit at its 1-based line."""
    found: list[tuple[int, bytes]] = [(m.start(), m.group()) for m in TOKEN.finditer(data)]
    for run in HEX_TOKEN.finditer(data):
        found += [(run.start(), m.group())
                  for m in TOKEN.finditer(bytes.fromhex(run.group().decode("ascii")))]
    return [Hit(where, data.count(b"\n", 0, at) + 1, fps.get(_fp(token)))
            for at, token in sorted(found, key=lambda pair: pair[0])]


def scan_text(text: str, fps: Mapping[str, str], where: str = "<text>") -> list[Hit]:
    """`scan_bytes` over a string (captured output, say)."""
    return scan_bytes(text.encode("utf-8", "surrogateescape"), fps, where)


def _files(root: Path, skip_dirs: frozenset[str]) -> Iterator[Path]:
    if root.is_symlink():
        return
    if root.is_file():
        yield root
        return
    for current, dirs, files in os.walk(root):          # followlinks=False
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for name in sorted(files):
            path = Path(current) / name
            if not path.is_symlink() and path.is_file():
                yield path


def scan_paths(paths: Iterable[str | os.PathLike], fps: Mapping[str, str], *,
               skip_dirs: frozenset[str] = SKIP_DIRS) -> Report:
    """Scan every regular file under `paths` (a file is scanned itself)."""
    hits: list[Hit] = []
    unreadable: list[tuple[str, str]] = []
    scanned = 0
    for root in paths:
        for path in _files(Path(root), skip_dirs):
            try:
                data = path.read_bytes()
            except OSError as error:
                unreadable.append((str(path), error.strerror or type(error).__name__))
                continue
            scanned += 1
            hits.extend(scan_bytes(data, fps, str(path)))
    return Report(tuple(hits), tuple(unreadable), scanned)


def render(report: Report) -> str:
    lines = [hit.text() for hit in report.hits]
    lines += [f"{path}: unreadable ({why})" for path, why in report.unreadable]
    known, unknown = len(report.known), len(report.unknown)
    lines.append(f"leakscan: {report.scanned} file(s); {known} registry token(s), "
                 f"{unknown} other token-shaped string(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, out: TextIO | None = None,
         err: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = argparse.ArgumentParser(
        prog="python -m sessiontools.account.leakscan",
        description="Report token-shaped strings by path:line, never by value.")
    parser.add_argument("paths", nargs="+", metavar="PATH")
    parser.add_argument("--registry", metavar="PATH",
                        help="the registry whose fingerprints name a hit "
                             "(default ~/.sessionkit/tools/accounts/accounts.json)")
    parser.add_argument("--strict", action="store_true",
                        help="fail on any token-shaped string, not only a registry token")
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_USAGE
    try:
        registry = reg.load(args.registry)
    except reg.RegistryError as error:
        print(f"leakscan: {error}", file=err)
        return error.code
    report = scan_paths(args.paths, fps_of(registry))
    print(render(report), file=out)
    if report.known or (args.strict and report.unknown):
        return EXIT_FOUND
    return EXIT_CLEAN


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
