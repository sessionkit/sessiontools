"""A `message` that answers `message path NAME` from one folder, the way
sessionmail answers from its mail root: for the tests of the readers that
ask sessionmail where a box is (the operator's mailbox, `session park`'s
mail check) and never spell the root themselves.

    env = message_answers_from(tmp_path / "boxes")   # {"PATH": ...}
    calls(env)                                       # each argv, one line

The stub is a shell script on PATH, so the real subprocess path runs: the
PATH lookup, the exit code, the stderr. It never reads the live machine's
mail."""

from __future__ import annotations

import os
from pathlib import Path

#: Exit 0 and the box's folder when ROOT/NAME is a directory; else
#: sessionmail's own no-box answer (exit 2, no usage lines). Any other
#: call is a usage error, as click says one.
STUB = """#!/bin/sh
printf '%%s\\n' "$*" >> "%(calls)s"
if [ "$1" != path ] || [ $# -ne 2 ]; then
  printf 'Usage: message path [BOX] [--why]\\n\\nError: not a call this stub knows\\n' >&2
  exit 2
fi
if [ -d "%(root)s/$2" ]; then
  printf '%%s\\n' "%(root)s/$2"
  exit 0
fi
printf "message path: no box '%%s' under %%s\\n" "$2" "%(root)s" >&2
exit 2
"""

#: A `message` too old for the verb: click's usage error, exit 2.
OLD_STUB = """#!/bin/sh
printf '%%s\\n' "$*" >> "%(calls)s"
printf "Usage: message <verb> [args]\\nTry 'message --help' for help.\\n\\nError: No such command 'path'.\\n" >&2
exit 2
"""


def _write(stubs: Path, body: str, log: bool = True, **fields: str) -> Path:
    stubs.mkdir(parents=True, exist_ok=True)
    stub = stubs / "message"
    calls_file = stubs / "message.calls" if log else os.devnull
    stub.write_text(body % {"calls": calls_file, **fields})
    stub.chmod(0o755)
    return stubs


def _env(stubs: Path, path: str | None) -> dict[str, str]:
    rest = os.environ.get("PATH", "") if path is None else path
    return {"PATH": f"{stubs}{os.pathsep}{rest}" if rest else str(stubs)}


def message_answers_from(root: Path | str, path: str | None = None,
                         log: bool = True) -> dict[str, str]:
    """An environ whose PATH finds a `message` answering from ROOT (each
    folder under it is a box), first, then PATH (default: this process's).
    The stub lives beside ROOT, in `<root>.message/`; ROOT need not exist.
    LOG False: it records no calls (a test that snapshots the tree)."""
    root = Path(root)
    return _env(_write(Path(f"{root}.message"), STUB, log, root=str(root)), path)


def old_message(stubs: Path, path: str | None = None) -> dict[str, str]:
    """An environ whose PATH finds a `message` with no `path` verb."""
    return _env(_write(stubs, OLD_STUB), path)


def calls(env: dict[str, str]) -> list[str]:
    """The argv of every call the stub first on ENV's PATH took, in order."""
    log = Path(env["PATH"].split(os.pathsep)[0]) / "message.calls"
    return log.read_text().splitlines() if log.exists() else []
