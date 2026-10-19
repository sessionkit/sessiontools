"""The vendor, as data: what a harness's command line looks like.

A vendor is a row in VENDORS, never a branch on its name; adding one is a
table edit. The port ships the claude row only - no agent file declares
`start: vendor:`, `codex:`, `pi:` or a `config:` map (17 MDs scanned,
2030-07-29) - but keeps the table's whole shape, so the codex and
pi rows can come back as ten lines each.

Every entry carries the same fields; `None` means the vendor has no such
door:

    cmd                     the executable
    identity_door           hands the vendor the agent (claude: --agent)
    agent_plugin_dir_arg    carries the agent's definition (NOT a door:
                            --plugin-dir is repeatable, and both live agents
                            declare their own)
    system_prompt_door      appends INLINE system-prompt text
    system_prompt_file_door appends system-prompt text held in a FILE -
                            preferred: a context block runs to tens of
                            kilobytes and argv is world-readable in `ps`
    session_id_flag         pins the session id
    prompt_separator        token before the first message (claude: `--`,
                            so a message starting with a dash stays one)
    config_door             takes key=value entries from a `config:` map
    settings_door           takes ONE settings file (claude: --settings,
                            one slot); the launcher computes it
                            from the `claude-code` settings tool
    env                     launcher-owned environment defaults; NOT a door

NAMING IS LOAD-BEARING: a field ending in `_door` or `_flag` IS a door, and
the duplicate-door guard finds it by that suffix alone - a new door is
guarded the moment it is in the table.

THE PASSTHROUGH EDGE. Launcher flags come before the agent name;
everything after it is the vendor's, verbatim. Leading bare words are the
first message; from the first dash token on it is vendor territory - the
launcher never guesses a flag's arity, so it never drops or reorders.
"""

from __future__ import annotations

import shlex
from typing import NamedTuple

from sessiontools.start.errors import AuthoringError

VENDORS: dict[str, dict] = {
    "claude": {
        "cmd": "claude",
        "identity_door": "--agent",
        "agent_plugin_dir_arg": "--plugin-dir",
        "system_prompt_door": "--append-system-prompt",
        "system_prompt_file_door": "--append-system-prompt-file",
        "session_id_flag": "--session-id",
        "prompt_separator": "--",
        "config_door": None,
        "settings_door": "--settings",
        # No prompt suggestions in any claude session this
        # launcher starts.
        "env": {"CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false"},
    },
}


# ---------- the one rendering rule ------------------------------------------

def config_scalar(value: object) -> str:
    """A YAML scalar as the text a config parser or `export` reads:
    booleans lowercase (`true`/`false`), everything else str()."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def render_vendor_args(vendor: str, section: dict) -> list[str]:
    """One vendor section to argv by exactly one rule: scalar -> `--k v`;
    list -> the flag once per element; True -> bare `--k`; False or None ->
    omitted; a `config:` map -> `<config_door> path=value` when the vendor
    has a config door. Anything else - a nested map outside the config door,
    a key already spelled with dashes - is an authoring error, loudly."""
    table = VENDORS[vendor]
    args: list[str] = []
    for key, value in section.items():
        if str(key).startswith("-"):
            raise AuthoringError(
                f"vendor key {key!r} must not start with '-': write "
                f"`{str(key).lstrip('-')}:` and let the rendering rule add the dashes")
        if key == "config" and isinstance(value, dict):
            door = table["config_door"]
            if door is None:
                raise AuthoringError(f"vendor {vendor!r} has no config door; a "
                                     "`config:` map cannot render")
            for path, entry in value.items():
                if isinstance(entry, (dict, list)):
                    raise AuthoringError(f"config entry {path!r} must be a scalar")
                args += [door, f"{path}={config_scalar(entry)}"]
            continue
        flag = f"--{key}"
        if value is True:
            args.append(flag)
        elif value is False or value is None:
            continue
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    raise AuthoringError(f"list entries for {key!r} must be scalars")
                args += [flag, str(item)]
        elif isinstance(value, dict):
            raise AuthoringError(
                f"no rendering rule for nested mapping under {key!r} -- only a "
                "`config:` map behind a vendor's config door renders")
        else:
            args += [flag, str(value)]
    return args


def vendor_front_matter_argv(vendor: str, section: dict,
                             agent_ref: str | None = None) -> list[str]:
    """The front-matter half of a vendor's argv: the identity door (when the
    vendor has one and an agent is named), then the rendered section. Native
    top-level keys are never rendered - the vendor reads them itself."""
    table = VENDORS[vendor]
    args: list[str] = []
    if agent_ref is not None and table["identity_door"] is not None:
        args += [table["identity_door"], agent_ref]
    return args + render_vendor_args(vendor, section)


def merge_argv(computed: list, front_matter_args: list, cli_args: list) -> list:
    """computed -> front matter -> CLI, by concatenation: the vendor's own
    parser makes the last scalar win and lists accumulate. The launcher
    never deduplicates - that needs every flag's arity."""
    return list(computed) + list(front_matter_args) + list(cli_args)


def split_rest(rest: list[str]) -> tuple[list[str], list[str]]:
    """Post-agent-name args -> (message words, vendor args): the leading run
    of bare words, then everything from the first dash token on."""
    for index, token in enumerate(rest):
        if token.startswith("-"):
            return list(rest[:index]), list(rest[index:])
    return list(rest), []


class ShellVar(NamedTuple):
    """An argv slot whose VALUE only the launch script's own shell knows:
    rendered `"$NAME"`, never a quoted literal. One case today - a keyed
    launch placed in tmux, whose session id is minted INSIDE the pane
    (`identity.PaneMint`), so the launcher cannot spell it and the
    trampoline expands it at boot. Reach for this only when the value does
    not exist until the script runs."""

    name: str


class ShellExpr(NamedTuple):
    """An argv slot MIXING literal text and a shell expansion, rendered
    inside double quotes: `"seat+${CLAUDE_SESSION_ID:0:8}"` - a `{shortsid}`
    filled in the pane. ShellVar is the whole-value case; this is the mixed
    one. The literal parts arrive already escaped for a double-quoted bash
    string (`identity.expand_shortsid`)."""

    text: str


def render_argv_token(token: str | ShellVar | ShellExpr) -> str:
    """One argv slot as a shell would need it typed: shlex-quoted, except a
    ShellVar or ShellExpr, which expand in the pane that runs the script."""
    if isinstance(token, ShellVar):
        return f'"${token.name}"'
    if isinstance(token, ShellExpr):
        return f'"{token.text}"'
    return shlex.quote(token)


# ---------- the duplicate-door guard ----------------------------------------

# Advice for an operator whose own declaration collides with a door the
# launcher computed, keyed by TABLE FIELD, never by flag spelling.
DOOR_ESCAPES = {
    "system_prompt_door":
        "to own the system prompt outright, set `start: idle: true` (or pass "
        "--idle): the launcher then injects nothing and the door is yours",
    "system_prompt_file_door":
        "to own the system prompt outright, set `start: idle: true` (or pass "
        "--idle): the launcher then injects nothing and the door is yours",
    "session_id_flag":
        "the launcher owns the session id -- it IS the session's identity, "
        "minted and persisted before launch -- so remove the declaration",
    "identity_door":
        "this door carries the agent named on the command line, so remove the "
        "declaration and launch the other agent by name instead",
    "settings_door":
        "the launcher folds a CLI --settings into its own door and computes none "
        "beside the agent file's `claude: settings:`, so this clash should not "
        "happen: report it",
}
DOOR_ESCAPE_DEFAULT = (
    "remove the declaration, or stop the launcher computing this door")


def vendor_doors(vendor: str) -> dict[str, str]:
    """{flag: table field} for every door the table names for `vendor`,
    found by field-name suffix; doors the vendor lacks (None) are absent."""
    return {value: field for field, value in VENDORS[vendor].items()
            if field.endswith(("_door", "_flag")) and isinstance(value, str)}


def door_is_opened_by(flag: str, args: list[str]) -> bool:
    """Is `flag` in `args` in either spelling the vendor parses - `--flag v`
    or `--flag=v`? The `=` is part of the prefix, so a LONGER flag that
    merely starts with the door's spelling (`--session-id-file`) is not
    this door. (A short option's attached value is not matched: only the
    vendor knows its arity, and no computed door is a short option.)"""
    return any(token == flag or token.startswith(flag + "=") for token in args)


def guard_computed_doors(vendor: str, launcher_args: list[str],
                         declared_args: list[str], cli_args: list[str]) -> None:
    """Refuse a launch that would open a launcher-computed door twice.

    claude keeps the LAST `--append-system-prompt-file` and supports only
    one, so a declared one would silently drop every injected context block
    while the launch reports success. REFUSE, do not pick a winner
    (2030-06-06): front matter and CLI passthrough alike. Only doors
    the launcher actually computed for this launch are guarded.

    One door is relaxed upstream, never here: a CLI passthrough `--settings`
    beside a computed settings door is FOLDED into that door as its top
    layer (claude_settings.fold), and its tokens are dropped before this
    guard runs. An agent file's `claude: settings:` gets no computed door.
    """
    doors = vendor_doors(vendor)
    computed = [flag for flag in launcher_args if flag in doors]
    sources = ((f"the agent file's `{vendor}:` section", declared_args),
               ("CLI passthrough (args after the agent name)", cli_args))
    for flag in computed:
        clashes = [label for label, args in sources
                   if door_is_opened_by(flag, args)]
        if not clashes:
            continue
        escape = DOOR_ESCAPES.get(doors[flag], DOOR_ESCAPE_DEFAULT)
        raise AuthoringError(
            f"vendor door {flag} is opened twice: by the launcher itself "
            f"(computed) and by {' and by '.join(clashes)}. {vendor} resolves a "
            "repeated flag by its own rule (claude keeps the LAST occurrence "
            "and supports only one), so one of the two values would be silently "
            f"discarded -- refusing rather than guessing which you meant. "
            f"Escape: {escape}.")
