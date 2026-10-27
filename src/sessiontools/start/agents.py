"""Which agent a name means, and where its sessions live.

The agents folder is `$AGENTS_DIR`, else the agent leaf's `config/`
(settings.agent_root) once it exists, else the pre-4.2 flat folder
(settings.legacy_agents_dir), the default by design. (The old launcher defaulted to `~/.claude/agents`,
where an agent's folder could be a directory with no `<name>.md` - so every
caller without AGENTS_DIR, the launchd jobs among them, could not start it.)

Two layouts, the directory one winning: `<root>/<n>/<n>.md` and the flat
`<root>/<n>.md`. When neither exists, the default seat's word (the vendor's
own name, `default_seat.word()`) resolves to the package's built-in template;
a user's file of that name shadows it. `--list-agents` answers from
exactly the same rules, so a picker never offers a name that will not start.

Every session's directory is `<sessions>/<sid>/` (settings.sessions_root),
whatever its agent. It was `<agents dir>/sessions/`, or
`<agent>/home/sessions/` when that existed; the old flat agents folder's
`sessions` is a symlink to the sessions root. `$SESSIONS_DIR` still
overrides.

AGENTS_DIR moves the agent-file lookup here and nothing else: the agent
leaf, an agent's home (`agent_home`, a SESSION_HOME) and the settings
reader's agent file (`agent_config`) never read it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools import settings as _settings
from sessiontools.start.errors import EXIT_USAGE, LaunchError

AGENTS_DIR_ENV = "AGENTS_DIR"
#: The pre-4.2 flat agents folder, `~/` + its path under HOME, for importers.
DEFAULT_AGENTS_DIR = f"~/{_settings.SESSION_DIR}/{_settings.OLD_AGENTS_DIR}"
#: The agents folder under the OLD root (4.2): `agent/config/`, for importers.
#: agents_root joins `config` to the agent leaf (settings.agent_root).
AGENT_CONFIG_DIR = ("agent", "config")
# One root for every session's directory (task 4.1): the settings reader's
# `sessiontools.settings.sessions_root` computes it, and `sessions_root`
# below is that, as a str. These two names stay for their importers.
SESSIONS_DIR_ENV = _settings.SESSIONS_DIR_ENV
#: The sessions leaf's OLD path under HOME, for importers.
DEFAULT_SESSIONS_DIR = (_settings.SESSION_DIR, "sessions")

# The agent that means "none" on a command line that goes on to carry a
# first message or vendor flags: argparse hands the first bare word to the
# positional whatever it says, and `--` does not separate (measured
# 2030-06-09). `session start` must be callable without an agent.
NO_AGENT = "-"


class Agent(NamedTuple):
    """A resolved agent: its markdown file, and its directory layout if any.

    `home` is the directory agent's `home/` when that exists - the sessions
    root anchors there; a file-only agent's sessions live under the agents
    dir itself, durable rather than a mktemp that dies on reboot.
    """

    name: str
    file: str
    directory: str
    home: str
    #: True for the default seat's built-in template, read from the package
    #: (`default_seat.md`): its `file` is no user's file.
    builtin: bool = False


BARE_AGENT = Agent(name="", file="", directory="", home="")


def agents_root(environ: Mapping[str, str] | None = None) -> str:
    """`$AGENTS_DIR`, else the agent leaf's `config/` once it exists, else
    the pre-4.2 flat folder, which keeps a symlink per agent after the move."""
    environ = os.environ if environ is None else environ
    if environ.get(AGENTS_DIR_ENV):
        return os.path.expanduser(environ[AGENTS_DIR_ENV])
    config = os.path.join(str(_settings.agent_root(environ)), AGENT_CONFIG_DIR[-1])
    if os.path.isdir(config):
        return config
    # The same HOME as the branch above:
    # a caller passing a foreign HOME gets that HOME's old folder.
    return str(_settings.legacy_agents_dir(environ))


def resolve_agent(name: str, root: str | None = None,
                  environ: Mapping[str, str] | None = None) -> Agent:
    """`name` as a directory agent or a file agent, else, for the default
    seat's word only, the package's built-in template. A directory without
    its markdown file is its own, more specific error (the seat word's too:
    a broken shadowing file is refused, never silently passed over); an
    unknown agent names BOTH candidate paths - honest about what was
    looked at."""
    root = agents_root(environ) if root is None else os.path.expanduser(root)
    directory = os.path.join(root, name)
    dir_file = os.path.join(directory, f"{name}.md")
    flat_file = os.path.join(root, f"{name}.md")
    if os.path.isdir(directory):
        if not os.path.isfile(dir_file):
            raise LaunchError(f"agent directory {directory} exists but {name}.md "
                              "is missing inside it", code=EXIT_USAGE)
        home = os.path.join(directory, "home")
        return Agent(name=name, file=dir_file, directory=directory,
                     home=home if os.path.isdir(home) else "")
    if os.path.isfile(flat_file):
        return Agent(name=name, file=flat_file, directory="", home="")
    builtin = builtin_agent(name)
    if builtin is not None:
        return builtin
    raise LaunchError(f"agent {name!r} not found -- looked for {directory}/ "
                      f"(directory agent) and {flat_file} (file agent)",
                      code=EXIT_USAGE)


def resolve_agent_or_bare(name: str | None, root: str | None = None,
                          environ: Mapping[str, str] | None = None) -> Agent:
    """`resolve_agent`, or BARE_AGENT when no agent was named (None or `-`):
    a plain vendor session - a minted id, no agent file, no key. A bare
    start reaches this with None only when it did NOT take the default
    seat: `start.bare_start` resolves the seat's word itself when it does."""
    if name is None or name == NO_AGENT:
        return BARE_AGENT
    return resolve_agent(name, root=root, environ=environ)


def builtin_agent(name: str) -> Agent | None:
    """The built-in Agent when NAME is the default seat's word, else None.
    Imported here, not at load: `default_seat` reads the vendor row through
    the agent-file module, which imports this one."""
    from sessiontools.start import default_seat
    if name != default_seat.word():
        return None
    return Agent(name=name, file=default_seat.template_path(), directory="",
                 home="", builtin=True)


def list_agents(root: str | None = None,
                environ: Mapping[str, str] | None = None) -> list[str]:
    """Every name `resolve_agent` would accept, sorted - THE PICKER'S SOURCE
    OF TRUTH: it answers "which names can I use?". A directory without its markdown file is not listed: a
    picker must not offer a refusal. The default seat's word is always
    listed: the built-in resolves when the folder has no file of that name.
    A missing or unreadable root lists the built-in alone, never an
    exception."""
    return [name for name, _mark in list_agent_rows(root=root, environ=environ)]


def list_agent_rows(root: str | None = None,
                    environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """`list_agents` as (name, mark) rows, sorted by name: the mark is
    `default_seat.BUILT_IN_MARK` for the built-in, and empty for a name the
    folder holds. A seat word the folder shadows is one row, unmarked. A
    seat word whose directory lacks its markdown file is not listed at all,
    since `resolve_agent` refuses it."""
    from sessiontools.start import default_seat
    root = agents_root(environ) if root is None else os.path.expanduser(root)
    rows = dict.fromkeys(_folder_agents(root), "")
    seat = default_seat.word()
    if seat not in rows and not os.path.isdir(os.path.join(root, seat)):
        rows[seat] = default_seat.BUILT_IN_MARK
    return sorted(rows.items())


def _folder_agents(root: str) -> set[str]:
    """The names the agents folder ROOT holds by the two layouts. A missing
    or unreadable root holds none."""
    try:
        entries = os.listdir(root)
    except OSError:
        return set()
    names = set()
    for entry in entries:
        if entry.startswith("."):
            continue
        full = os.path.join(root, entry)
        if os.path.isdir(full):
            if os.path.isfile(os.path.join(full, f"{entry}.md")):
                names.add(entry)
        elif entry.endswith(".md"):
            names.add(entry[:-3])
    return names


def agent_subject(agent: Agent) -> str:
    """What leads an error message: the agent file, or the fact of none."""
    return agent.file or "(no agent)"


def sessions_root(agent: Agent, root: str | None = None,
                  environ: Mapping[str, str] | None = None) -> str:
    """settings.sessions_root (`$SESSIONS_DIR`, else the sessions leaf): one root whatever the
    agent (task 4.1). `agent` and `root` no longer move it; they are kept so
    no caller changes. HOME is read from `environ`, as the settings reader
    reads it, so a launch and `session settings` agree on the directory."""
    return str(_settings.sessions_root(os.environ if environ is None else environ))
