"""An agent file's front matter: who owns each key, and the `start:` schema.

THE ONLY MODULE THAT IMPORTS YAML: `session start` may pull in the YAML
library for itself alone. Front matter is YAML - the same YAML Claude Code reads itself - and a
hand-written subset would be the silent-misconfiguration class this
launcher exists to end. `import sessiontools` must never reach this file
(tests/test_cli.py pins it).

The front matter IS the launch configuration, partitioned four ways:

    native       top-level keys the vendor reads itself (claude: via
                 --agent) - never rendered into argv
    vendors      `claude:` - rendered to argv by vendor.render_vendor_args
    start        the launcher's own switches (below)
    environment  the declared environment (environment.parse_environment)

Everything is loud: an unknown `start:` key, a switch that is not a real
YAML boolean, a list that is a string. The failure mode of a
silently-ignored key is a session that looks fine and is configured wrong.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

import yaml

from sessiontools.start.agents import Agent, agent_subject
from sessiontools.start.context import DEFAULT_FIRST_MESSAGE_FILE_OVER, ContextEntry
from sessiontools.start.environment import (
    EMPTY_ENVIRONMENT,
    Environment,
    parse_environment,
)
from sessiontools.start.errors import (
    EXIT_USAGE,
    AuthoringError,
    LaunchError,
    unknown_keys,
)
from sessiontools.start.vendor import VENDORS, render_vendor_args

START_SECTION = "start"
ENVIRONMENT_SECTION = "environment"
LAUNCHER_SECTIONS = (START_SECTION, ENVIRONMENT_SECTION)

FRONT_MATTER_DELIM = re.compile(r"^---[ \t]*$")


class FrontMatter(NamedTuple):
    native: dict
    vendors: dict
    start: dict
    environment: dict


def parse_agent_markdown(text: str) -> tuple[dict, str]:
    """(front matter, body). No front matter is a valid agent file; an
    OPENED but unterminated block is an authoring error - treating a broken
    header as body would launch with none of its configuration."""
    if not FRONT_MATTER_DELIM.match(text.split("\n", 1)[0]):
        return {}, text
    rest = text.split("\n", 1)[1] if "\n" in text else ""
    lines = rest.split("\n")
    for index, line in enumerate(lines):
        if FRONT_MATTER_DELIM.match(line) or line.rstrip() == "...":
            raw = "\n".join(lines[:index])
            body = "\n".join(lines[index + 1:])
            loaded = yaml.safe_load(raw) if raw.strip() else None
            if loaded is None:
                return {}, body
            if not isinstance(loaded, dict):
                raise AuthoringError("front matter must be a YAML mapping, "
                                     f"got {type(loaded).__name__}")
            return loaded, body
    raise AuthoringError("front matter opened with --- but never closed")


def partition_front_matter(front_matter: dict) -> FrontMatter:
    """Split into native / vendors / start / environment. An empty section
    (YAML None) becomes {}; a section that is not a mapping is loud."""
    native, vendors, start, environment = {}, {}, {}, {}
    for key, value in front_matter.items():
        if key in VENDORS or key in LAUNCHER_SECTIONS:
            section = {} if value is None else value
            if not isinstance(section, dict):
                raise AuthoringError(f"section {key!r} must be a mapping of keys "
                                     f"to values, got {type(value).__name__}")
            if key == START_SECTION:
                start = section
            elif key == ENVIRONMENT_SECTION:
                environment = section
            else:
                vendors[key] = section
        else:
            native[key] = value
    return FrontMatter(native=native, vendors=vendors, start=start,
                       environment=environment)


# ---------- `start:` - the launcher's own switches ---------------------------

CONTEXT_SECTION = "context"
CONTEXT_KEYS = ("system-prompt", "initial")
# An entry written as a mapping instead of a command string.
CONTEXT_ENTRY_KEYS = ("run", "informational-exit")
START_KEYS = ("idle", "converse", "keyed", "vendor", CONTEXT_SECTION,
              "first-message-file-over", "directory")

DEFAULT_VENDOR = "claude"


class Context(NamedTuple):
    """`start: context:` - two ordered lists of generator commands.

    system_prompt -- stdout rides the vendor's system-prompt-append door
    initial       -- stdout leads the session's first user message

    An entry is a command string, or a `ContextEntry` when the agent file
    wrote `{run: CMD, informational-exit: [N, ...]}` with at least one code.
    """

    system_prompt: list
    initial: list


EMPTY_CONTEXT = Context(system_prompt=[], initial=[])


class StartOptions(NamedTuple):
    idle: bool
    converse: bool
    keyed: bool
    vendor: str
    context: Context
    first_message_file_over: int
    # WHERE THE AGENT RUNS by default;
    # "" = no opinion, it runs where it was typed.
    directory: str = ""


EMPTY_START = StartOptions(idle=False, converse=False, keyed=False,
                           vendor=DEFAULT_VENDOR, context=EMPTY_CONTEXT,
                           first_message_file_over=DEFAULT_FIRST_MESSAGE_FILE_OVER)


def parse_start(section: object) -> StartOptions:
    """Validate `start:`. `start: convrse:` must not launch a silent, typed
    session that the file plainly asked to have speak."""
    if not section:
        return EMPTY_START
    if not isinstance(section, dict):
        raise AuthoringError(f"`start:` must be a mapping, got {type(section).__name__}")
    unknown = [key for key in section if key not in START_KEYS]
    if unknown:
        raise unknown_keys("start:", unknown, START_KEYS)
    vendor = section.get("vendor", DEFAULT_VENDOR)
    if not shipped_vendor(vendor):
        raise unsupported_vendor(vendor)
    return StartOptions(
        idle=_parse_bool(section.get("idle"), "start: idle:"),
        converse=_parse_bool(section.get("converse"), "start: converse:"),
        keyed=_parse_bool(section.get("keyed"), "start: keyed:"),
        vendor=vendor,
        context=parse_context(section.get(CONTEXT_SECTION)),
        first_message_file_over=_parse_bytes(
            section.get("first-message-file-over"),
            "start: first-message-file-over:",
            default=DEFAULT_FIRST_MESSAGE_FILE_OVER),
        directory=_parse_directory(section.get("directory"), "start: directory:"),
    )


def shipped_vendor(vendor: object) -> bool:
    """Is `vendor` a row of this port's vendor table? (A list or a map is
    not - and must not reach `in VENDORS` as an unhashable key.)"""
    return isinstance(vendor, str) and vendor in VENDORS


def unsupported_vendor(vendor: object) -> AuthoringError:
    """The one spelling of the vendor refusal."""
    return AuthoringError(f"start: vendor: {vendor!r} is not a supported vendor "
                          f"(known: {', '.join(sorted(VENDORS))})")


def _parse_directory(value: object, where: str) -> str:
    """A non-empty path, `~` expanded. Existence is checked at launch."""
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise AuthoringError(f"{where} must be a non-empty path, got {value!r}")
    return os.path.expanduser(value.strip())


def _parse_bytes(value: object, where: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthoringError(f"{where} must be a non-negative integer (bytes), "
                             f"got {value!r}")
    return value


def _parse_bool(value: object, where: str) -> bool:
    """`idle: maybe` is not false, it is a typo."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise AuthoringError(f"{where} must be true or false, got {value!r}")
    return value


def parse_context(section: object) -> Context:
    if not section:
        return EMPTY_CONTEXT
    if not isinstance(section, dict):
        raise AuthoringError("`start: context:` must be a mapping with "
                             f"{' and '.join(CONTEXT_KEYS)} keys, "
                             f"got {type(section).__name__}")
    unknown = [key for key in section if key not in CONTEXT_KEYS]
    if unknown:
        raise unknown_keys("start: context:", unknown, CONTEXT_KEYS)
    return Context(
        system_prompt=_parse_context_list(section.get("system-prompt"),
                                          "start: context: system-prompt:"),
        initial=_parse_context_list(section.get("initial"),
                                    "start: context: initial:"),
    )


def _parse_context_list(raw: object, where: str) -> list:
    if raw is None:
        return []
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise AuthoringError(f"`{where}` must be a LIST of commands, got "
                             f"{type(raw).__name__} -- order is part of the meaning")
    return [_parse_context_entry(entry, where) for entry in raw]


def _parse_context_entry(entry: object, where: str) -> str | ContextEntry:
    """A command string, or `{run: CMD, informational-exit: [N, ...]}`. A
    mapping that declares no code is its `run` string, exactly. Every
    misspelling is refused: a declaration that silently fell back to FAILED
    would be the quiet misconfiguration this schema exists to end."""
    if isinstance(entry, str) and entry.strip():
        return entry
    if not isinstance(entry, dict):
        raise AuthoringError(f"`{where}` entries must be non-empty command "
                             f"strings or {{run: ..., informational-exit: "
                             f"[...]}} mappings, got {entry!r}")
    unknown = [key for key in entry if key not in CONTEXT_ENTRY_KEYS]
    if unknown:
        raise unknown_keys(where, unknown, CONTEXT_ENTRY_KEYS)
    command = entry.get("run")
    if not isinstance(command, str) or not command.strip():
        raise AuthoringError(f"`{where}` entry `run:` must be a non-empty "
                             f"command string, got {command!r}")
    codes = entry.get("informational-exit", [])
    if not isinstance(codes, list):
        raise AuthoringError(f"`{where}` entry `informational-exit:` must be a "
                             f"LIST of exit codes, got {codes!r}")
    for code in codes:
        # `type(...) is int`: a YAML bool (true, yes, on) is an int subclass.
        # 0 is refused: a zero exit is success, never informational.
        if type(code) is not int or not 1 <= code <= 255:
            raise AuthoringError(f"`{where}` entry `informational-exit:` codes "
                                 f"must be integers from 1 to 255, got {code!r}")
    if not codes:
        return command
    return ContextEntry(command, frozenset(codes))


def switch_enabled(cli_flag: bool, front_matter_value: bool) -> bool:
    """A launcher switch is ON from the CLI flag OR the front matter. There
    is no `--no-idle`: every such flag is a name taken out of the vendor's
    namespace; editing the agent file changes its default."""
    return bool(cli_flag) or bool(front_matter_value)


# ---------- the whole file ---------------------------------------------------

class AgentFile(NamedTuple):
    """An agent file, read and validated: everything a launch takes from it."""

    front_matter: dict
    native: dict
    vendors: dict
    start: StartOptions
    environment: Environment

    @property
    def vendor_section(self) -> dict:
        """The section of the vendor this file launches (`claude:`)."""
        return self.vendors.get(self.start.vendor, {})


EMPTY_AGENT_FILE = AgentFile(front_matter={}, native={}, vendors={},
                             start=EMPTY_START, environment=EMPTY_ENVIRONMENT)


def load(agent: Agent) -> AgentFile:
    """Read and validate `agent`'s file; BARE_AGENT has none. Every
    authoring error - bad YAML, an unterminated header, an unknown `start:`
    key - is a usage refusal (exit 2) that names the file."""
    if not agent.file:
        return EMPTY_AGENT_FILE
    try:
        with open(agent.file, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise LaunchError(f"{agent_subject(agent)}: cannot read the agent file: {error}")
    try:
        front_matter, _body = parse_agent_markdown(text)
        parts = partition_front_matter(front_matter)
        start = parse_start(parts.start)
        environment = parse_environment(parts.environment)
        # Render the vendor section once, here, so a key the one rule cannot
        # render (`--effort:`, a nested map) is refused naming the file -
        # the old launcher first met it inside the Fable gate, as a traceback.
        render_vendor_args(start.vendor, parts.vendors.get(start.vendor, {}))
    except (ValueError, yaml.YAMLError) as error:
        raise LaunchError(f"{agent_subject(agent)}: {error}",
                          code=EXIT_USAGE)
    return AgentFile(front_matter=front_matter, native=parts.native,
                     vendors=parts.vendors, start=start, environment=environment)

