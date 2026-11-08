"""`session path`: where tools keeps each leaf of its state, for peers.

    session path LEAF [NAME]     one absolute path, and a newline
    session path agent NAME      NAME's home, `<agent leaf>/home/NAME`
    session path agent --config  the agents folder the launcher looks in
    session path agent NAME --config
                                 NAME's agent folder (nothing for a flat
                                 agent or the built-in seat)
    session path --json          {LEAF: {"path", "source"[, "stranded"]}}, every leaf
    session path LEAF [NAME] --json   that leaf's entry alone, unwrapped (with
                                 a sessions ID or seats SEAT, its path is
                                 that folder)
    session path agent NAME --json    the agent contract, `agent_report`:
                                 {version, root, items{home, agents, config}},
                                 with or without --config
    session path agent --config --json
                                 the agents folder: {path, source, rung}
    session path                 every leaf, one line each, for a person
    session path migrate [--dry-run] [--json]
                                 move the state and sound leaves to their new
                                 home (sessiontools.migrate)

A VIEW onto sessiontools.settings, never a second rule: each leaf's answer
is `settings.leaf_path` (state, sessions, seats, agent, backups, sound), and
`log` is `settings.log_dir` of tools' package. NAME joins a session
id under sessions (`settings.session_dir`), a seat under seats
(`settings.seat_dir`) or an agent under agent (`settings.agent_home`, its
SESSION_HOME `<agent leaf>/home/NAME`), literally: no short-id lookup, so a
script gets the folder a session of that id has, whether or not it exists yet.
An agent's home is printed, exit 0, whether or not it exists, and NAME need
not be an agent: the join never reads the agents folder.

`--config`, on the agent leaf only, answers from the LAUNCHER's lookup
(`start.agents`), so the folder it names is the one an agent starts from:
`agents_root` without NAME, `resolve_agent(NAME).directory` with one. A flat
`NAME.md` agent and the built-in default seat have no folder: empty stdout,
exit 0. A NAME the launcher refuses (unknown, or a folder without its
`NAME.md`) exits 2 with the launcher's own message.

`agent NAME --json` is the one answer that is not `{path, source}`: it is
`agent_report`'s contract, so a peer gets the home and the folder in one
call and caches on `version`. `items.config.path` is "" when NAME has no
folder (a flat or built-in agent): test it before joining `avatar.jpg`. A
NAME the launcher refuses does not fail it: exit 0, `items.config.refused`
holds the launcher's message, and `items.home` is still a true answer.

`source` is one of SOURCES, exactly these strings (sessionmail's
vmailbox-gen parses them): `override` (its variable is set), `old` (the
old folder, while it is real), `new` (the new folder: moved, or old a link
to it), `fresh-old` and `fresh-new` (neither folder there yet: where a
first write would go). `log` has no override and no old folder that is
read, so it is `new` or `fresh-new`. `stranded` names a leaf's NEW folder
when the old one is used while the new one holds something of its own; it
is left out otherwise (`settings.leaf_stranded`).

Peers in other languages run this verb rather than copy a path; a Python
peer imports the resolvers (`settings.sessions_root(env)` and the rest), and
for an agent's home and folder `paths.agent_report(name, env)`, the dict
the verb prints.
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
NAMED = {"sessions": settings.session_dir, "seats": settings.seat_dir,
         "agent": settings.agent_home}
#: What a refused NAME is not, per NAMED leaf.
NOUNS = {"sessions": "a session id", "seats": "a seat", "agent": "an agent"}
#: The one leaf `--config` answers for.
CONFIG_LEAF = "agent"
#: The words `rung` may say, for the help: `start.agents.RUNGS`, copied so
#: the help needs no `start` import (a test holds the two equal).
_RUNGS = ("AGENTS_DIR", "config", "agents")

EPILOG = f"""leaves: {', '.join(LEAVES)}
  `session path migrate --dry-run` plans the move of state and sound.
  NAME joins a session id under sessions, a seat under seats, or an agent
  under agent (its home: home/NAME in the agent leaf, printed whether or
  not it exists).
  agent --config prints the agents folder the launcher looks in; with NAME,
  NAME's agent folder, or nothing for a flat agent or the built-in seat.

--json: every leaf (or LEAF alone) as {{"path", "source"}}, and "stranded"
when the old folder is used while the new one holds something of its own.
source is one of: {', '.join(SOURCES)}.
agent --config --json: the agents folder as {{"path", "source", "rung"}};
rung is one of: {', '.join(_RUNGS)}. On a fresh host (no old root of its
own) it is the agent leaf's config/ before it exists: rung config,
source fresh-new.
agent NAME --json (with or without --config): {{"version", "root",
"items": {{"home", "agents", "config"}}}}. items.config has "path", "" for
no folder (a flat or built-in agent: test it before joining avatar.jpg),
"file", the agent file (set for a flat or built-in agent too), "builtin",
and "refused" (the launcher's message) when it refuses NAME; a refused NAME
has "path" and "file" both "".

exit: 0; 2 with the usage for an unknown LEAF, a bad NAME, a NAME on a
leaf that takes none, or --config on any leaf but agent. agent NAME
--config also exits 2 when the launcher refuses NAME (unknown, or a folder
without its NAME.md); agent NAME --json does not.

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


def _either(words) -> str:
    """`a`, `a and b`, `a, b and c`."""
    words = list(words)
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def check_name(leaf: str, name: str) -> None:
    """Refuse a NAME for LEAF: a leaf outside NAMED takes none, and a NAME
    that is empty, `.`, `..` or holds a `/` or NUL would leave the leaf."""
    if leaf not in NAMED:
        raise Usage(f"{leaf} takes no NAME: only {_either(NAMED)} do")
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise Usage(f"{name!r} is not {NOUNS[leaf]}")


def path_of(leaf: str, name: str | None = None,
            env: Mapping[str, str] | None = None) -> Path:
    """LEAF's folder, or NAME's folder inside it (sessions, seats and agent
    only; under agent, NAME's home). A bad NAME is refused (check_name)."""
    found = resolve(leaf, env)[0]
    if name is None:
        return found
    check_name(leaf, name)
    return NAMED[leaf](name, env)


def config_of(name: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """`session path agent [NAME] --config`, from the launcher's lookup:
    the agents folder, or NAME's agent folder ("" for a flat agent or the
    built-in seat: no folder). A NAME the launcher refuses is a Usage
    carrying its message. `start.agents` is imported here, not at load, so
    every other leaf keeps its import cost."""
    from sessiontools.start import agents
    from sessiontools.start.errors import LaunchError
    if name is None:
        return _abs(agents.agents_root(env))
    check_name(CONFIG_LEAF, name)
    try:
        directory = agents.resolve_agent(name, environ=env).directory
    except LaunchError as e:
        raise Usage(str(e)) from None
    return _abs(directory) if directory else ""


def _rung_source(rung: str, root: str, env: Mapping[str, str] | None) -> str:
    """`source` for the agents folder, in SOURCES' words: AGENTS_DIR is
    `override`; the agent leaf's `config/` takes that leaf's source; the
    pre-4.2 flat folder is `old` while it is a directory, else `fresh-old`."""
    from sessiontools.start import agents
    if rung == agents.RUNGS[0]:
        return "override"
    if rung == agents.RUNGS[1]:
        return resolve(CONFIG_LEAF, env)[1]
    return "old" if os.path.isdir(root) else "fresh-old"


def agents_entry(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The agents folder as `--json` shows it: `{path, source, rung}`, the
    rung from `start.agents.agents_rung` (the one copy of the chain):
    `session path agent --config --json`."""
    from sessiontools.start import agents
    root, rung = agents.agents_rung(env)
    return {"path": _abs(root), "source": _rung_source(rung, root, env), "rung": rung}


def agent_report(name: str, env: Mapping[str, str] | None = None) -> dict:
    """`session path agent NAME --json` (with or without `--config`): the
    contract a peer caches on `version`.

        {"version": sessiontools.__version__,
         "root":  the agent leaf's entry (path, source[, stranded]),
         "items": {"home":   {path, source},          agent_home(NAME)
                   "agents": {path, source, rung},    agents_entry()
                   "config": {path, source, file, builtin[, refused]}}}

    `config` is `resolve_agent(NAME)`: path is its directory ("" for a flat
    agent or the built-in seat), file its file. A NAME the launcher refuses
    (unknown, or a folder without its NAME.md) is not a failure here: the
    home is still a true answer, so config carries "" and `refused`, the
    launcher's message. A bad NAME is a Usage (check_name)."""
    import sessiontools
    from sessiontools.start import agents
    from sessiontools.start.errors import LaunchError
    check_name(CONFIG_LEAF, name)
    root = entry(CONFIG_LEAF, env)
    folder = agents_entry(env)
    config: dict = {"path": "", "source": folder["source"], "file": "", "builtin": False}
    try:
        found = agents.resolve_agent(name, environ=env)
    except LaunchError as e:
        config["refused"] = str(e)
    else:
        config.update(path=_abs(found.directory) if found.directory else "",
                      file=_abs(found.file) if found.file else "", builtin=found.builtin)
    return {"version": sessiontools.__version__,
            "root": root,
            "items": {"home": {"path": _abs(settings.agent_home(name, env)),
                               "source": root["source"]},
                      "agents": folder,
                      "config": config}}


def build_parser() -> argparse.ArgumentParser:
    """`session path`'s parser. LEAF is checked by the verb, not argparse,
    so it refuses in its own words; completion offers LEAVES."""
    p = argparse.ArgumentParser(prog=PROG, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Print where tools keeps LEAF (NAME inside it), as "
                                            "the settings resolvers answer on this host.")
    offer(p.add_argument("leaf", nargs="?", metavar="LEAF"), LEAVES)
    p.add_argument("name", nargs="?", metavar="NAME",
                   help="a session id (sessions), a seat (seats) or an agent (agent)")
    p.add_argument("--json", action="store_true",
                   help="path, source and stranded, for every leaf or LEAF alone; "
                        "with agent NAME, the agent contract")
    p.add_argument("--config", action="store_true",
                   help="agent only: the agents folder the launcher looks in, "
                        "or NAME's agent folder")
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
        if a.config and a.leaf != CONFIG_LEAF:
            raise Usage(f"--config is for the {CONFIG_LEAF} leaf only")
        if a.leaf is None:  # argparse fills LEAF before NAME: no NAME here
            out = json.dumps(report(env), indent=1) + "\n" if a.json else _table(env)
        elif a.json and a.leaf == CONFIG_LEAF and a.name is not None:
            out = json.dumps(agent_report(a.name, env)) + "\n"   # --config or not
        elif a.json and a.config:
            out = json.dumps(agents_entry(env)) + "\n"
        elif a.json:
            found = path_of(a.leaf, a.name, env)
            e = entry(a.leaf, env)
            out = json.dumps({**e, "path": _abs(found)} if a.name is not None else e) + "\n"
        elif a.config:
            found = config_of(a.name, env)
            out = found + "\n" if found else ""      # no folder: zero bytes
        else:
            out = _abs(path_of(a.leaf, a.name, env)) + "\n"
    except Usage as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 2
    sys.stdout.write(out)
    return 0
