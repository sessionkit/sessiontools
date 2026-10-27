"""`session path`: where tools keeps each leaf of its state, for peers.

    session path LEAF [NAME]     one absolute path, and a newline
    session path --json          {LEAF: {"path", "source"[, "stranded"]}}, every leaf
    session path LEAF [NAME] --json   that leaf's entry alone, unwrapped (with
                                 NAME, its path is NAME's folder)
    session path                 every leaf, one line each, for a person
    session path migrate [--dry-run] [--json]
                                 move the state and sound leaves to their new
                                 home (sessiontools.migrate)

A VIEW onto sessiontools.settings, never a second rule: each leaf's answer
is `settings.leaf_path` (state, sessions, seats, agent, backups, sound), and
`log` is `settings.log_dir` of tools' package. NAME joins a session
id under sessions (`settings.session_dir`) or a seat under seats
(`settings.seat_dir`), literally: no short-id lookup, so a script gets the
folder a session of that id has, whether or not it exists yet.

`source` is one of SOURCES, exactly these strings (sessionmail's
vmailbox-gen parses them): `override` (its variable is set), `old` (the
old folder, while it is real), `new` (the new folder: moved, or old a link
to it), `fresh-old` and `fresh-new` (neither folder there yet: where a
first write would go). `log` has no override and no old folder that is
read, so it is `new` or `fresh-new`. `stranded` names a leaf's NEW folder
when the old one is used while the new one holds something of its own; it
is left out otherwise (`settings.leaf_stranded`).

Peers in other languages run this verb rather than copy a path; a Python
peer imports the resolvers (`settings.sessions_root(env)` and the rest).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from sessiontools import settings
from sessiontools.verbs import Group, offer

PROG = "session path"
#: The leaves `session path` answers, in the order `--json` lists them:
#: tools' state leaves, then its log folder.
LOG = "log"
LEAVES = (*settings.LEAVES, LOG)
#: What `source` may say: the resolver's own words.
SOURCES = settings.LEAF_SOURCES
#: The leaves a NAME joins, and the resolver that joins it.
NAMED = {"sessions": settings.session_dir, "seats": settings.seat_dir}

EPILOG = f"""leaves: {', '.join(LEAVES)}
  `session path migrate --dry-run` plans the move of state and sound.
  NAME joins a session id under sessions, or a seat under seats.

--json: every leaf (or LEAF alone) as {{"path", "source"}}, and "stranded"
when the old folder is used while the new one holds something of its own.
source is one of: {', '.join(SOURCES)}.

The rule, per leaf: its variable when set (SESSION_STATE_DIR, SESSIONS_DIR,
SESSIONTOOLS_SOUND_HOME); else the old folder while it is real; else the new
one under ~/.sessionkit/tools/state/; else where a first write would go."""


class Usage(Exception):
    """A LEAF or NAME the verb refuses: exit 2, with the usage."""


def resolve(leaf: str, env: Mapping[str, str] | None = None) -> tuple[Path, str]:
    """(path, source) for LEAF, from the settings resolvers alone."""
    if leaf == LOG:
        found = settings.log_dir(settings.PACKAGES["session"], env)
        return found, "new" if os.path.isdir(found) else "fresh-new"
    if leaf not in settings.LEAVES:
        raise Usage(f"unknown leaf {leaf!r}: one of {', '.join(LEAVES)}")
    return settings.leaf_path(leaf, env)


def _abs(path: Path) -> str:
    return os.path.abspath(path)


def entry(leaf: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """LEAF as `--json` shows it: path and source, and stranded when set."""
    found, source = resolve(leaf, env)
    out = {"path": _abs(found), "source": source}
    stranded = settings.leaf_stranded(leaf, env) if leaf in settings.LEAVES else None
    if stranded is not None:
        out["stranded"] = _abs(stranded)
    return out


def report(env: Mapping[str, str] | None = None) -> dict[str, dict[str, str]]:
    """Every leaf, in LEAVES order: `session path --json`."""
    return {leaf: entry(leaf, env) for leaf in LEAVES}


def path_of(leaf: str, name: str | None = None,
            env: Mapping[str, str] | None = None) -> Path:
    """LEAF's folder, or NAME's folder inside it (sessions and seats only).
    A NAME that is empty, `.`, `..` or holds a `/` would leave the leaf:
    refused."""
    found = resolve(leaf, env)[0]
    if name is None:
        return found
    if leaf not in NAMED:
        raise Usage(f"{leaf} takes no NAME: only {' and '.join(NAMED)} do")
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise Usage(f"{name!r} is not a {'session id' if leaf == 'sessions' else 'seat'}")
    return NAMED[leaf](name, env)


def build_parser() -> argparse.ArgumentParser:
    """`session path`'s parser. LEAF is checked by the verb, not argparse,
    so it refuses in its own words; completion offers LEAVES."""
    p = argparse.ArgumentParser(prog=PROG, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Print where tools keeps LEAF (NAME inside it), as "
                                            "the settings resolvers answer on this host.")
    offer(p.add_argument("leaf", nargs="?", metavar="LEAF"), LEAVES)
    p.add_argument("name", nargs="?", metavar="NAME",
                   help="a session id (sessions) or a seat (seats)")
    p.add_argument("--json", action="store_true",
                   help="path, source and stranded, for every leaf or LEAF alone")
    return p


def _table(env: Mapping[str, str] | None) -> str:
    rows = []
    for leaf, e in report(env).items():
        rows.append(f"{leaf:9} {e['source']:9} {e['path']}")
        if "stranded" in e:
            rows.append(f"{'':9} {'stranded':9} {e['stranded']} (not read: the old folder is used)")
    return "\n".join(rows) + "\n"


#: `migrate` is dispatched by hand before LEAF is parsed: it is no leaf, so
#: the two never clash. Completion and the dispatcher read this one table.
GROUP = Group(subs={"migrate": "sessiontools.migrate:build_parser"},
              rest="sessiontools.paths:build_parser")


def main(argv: list[str], env: Mapping[str, str] | None = None) -> int:
    if argv[:1] and argv[0] in GROUP.subs:
        from sessiontools import migrate
        return migrate.main(argv[1:], env)
    parser = build_parser()
    a = parser.parse_args(argv)
    try:
        if a.leaf is None:  # argparse fills LEAF before NAME: no NAME here
            out = json.dumps(report(env), indent=1) + "\n" if a.json else _table(env)
        elif a.json:
            found = path_of(a.leaf, a.name, env)
            e = entry(a.leaf, env)
            out = json.dumps({**e, "path": _abs(found)} if a.name is not None else e) + "\n"
        else:
            out = _abs(path_of(a.leaf, a.name, env)) + "\n"
    except Usage as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 2
    sys.stdout.write(out)
    return 0
