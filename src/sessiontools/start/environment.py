"""The declared environment: what an agent file says its session carries.

THE RULE: nothing reaches an agent's environment that its own file did not
declare. The only things that set a variable are `variables:`, a file in
`files:`, a backend named in `secrets:`, and a name listed in `inherit:`.

Credentials are the rule's sharpest edge: a seat launched
holding CLAUDE_CODE_OAUTH_TOKEN once handed it to every seat it started,
and a tmux server it birthed kept it for every pane it ever made. So
`host_environ` drops CREDENTIAL_VARS, the launch script's first line
unsets them, and the one way a launcher's credential reaches its child is
`inherit: [NAME, ...]`: opt-in, named, carried through the same 0600 file
as `secrets:` (never argv, never the tmux server), and logged by name only.

`.env` files are shell, not data, so bash does the sourcing: the launcher
RENDERS a prelude (every value quoted by shlex) that runs inside the
launched process's own shell, after the cd and before the exec. Nothing
touches the tmux server, the calling shell, or the launcher's own
os.environ.

Secrets are NAMED here: a dry run prints them and resolves nothing (it
never did). A live launch resolves them in `secrets` (the `pass` backend,
the 0600 file), and this module's prelude sources that file LAST and
removes it; the shred trap is armed in `plan`.

`host_environ` lives here too: the environment every child of the launch
starts from - the user's, never a venv the launcher itself runs in. So does
`without_session_identity`: the caller's session identity is never handed
down, the new session gets only the one `session start` minted.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools.claude_code import SESSION_ID_VARS
from sessiontools.session_info import LAUNCH_INFO_ENV
from sessiontools.start.errors import PROG, AuthoringError, unknown_keys
from sessiontools.start.vendor import config_scalar

ENVIRONMENT_KEYS = ("variables", "files", "secrets", "inherit")

# POSIX-shell-safe names. Validated, not trusted: a name is interpolated
# into `export <name>=...`, so an unchecked one is a command-injection door.
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Project-local: resolved against the session's working directory AFTER the
# cd, so `./.voicemode.env` means "this project's override".
RELATIVE_PREFIX = "./"

# The backends a `secrets:` reference may name. Parsing checks the name
# here; the resolvers live in secrets.py (`BACKENDS`).
SECRET_BACKENDS = ("pass",)

# What a dry run names the 0600 secret file (a live launch mints a private
# random name; a dry run writes nothing and says where it would be).
DRY_RUN_SECRET_NAME = "secrets.env"


class Environment(NamedTuple):
    """The `environment:` block, validated.

    variables -- name -> value, set in the launched process only
    files     -- paths sourced in declared order, later winning
    secrets   -- name -> 'backend:ref', reported by name, never by value
    inherit   -- names carried from the LAUNCHER's own environment,
                 through the 0600 secret file; reported by name, never value
    """

    variables: dict
    files: list
    secrets: dict
    inherit: tuple = ()


EMPTY_ENVIRONMENT = Environment(variables={}, files=[], secrets={})


def parse_environment(section: object) -> Environment:
    """Validate an `environment:` block. Unknown keys are loud: `variable:`
    for `variables:` would otherwise launch a session with none of its
    declared environment and no hint that anything was missed."""
    if not section:
        return EMPTY_ENVIRONMENT
    if not isinstance(section, dict):
        raise AuthoringError("`environment:` must be a mapping, "
                             f"got {type(section).__name__}")
    unknown = [key for key in section if key not in ENVIRONMENT_KEYS]
    if unknown:
        raise unknown_keys("environment:", unknown, ENVIRONMENT_KEYS)
    variables = _parse_variables(section.get("variables"))
    secrets = _parse_secrets(section.get("secrets"))
    inherit = _parse_inherit(section.get("inherit"))
    for name in inherit:
        # One name, one source: a name both inherited and declared elsewhere
        # would be decided by sourcing order, which nobody reading the file
        # can see. Say so instead.
        for other, declared in (("variables:", variables), ("secrets:", secrets)):
            if name in declared:
                raise AuthoringError(
                    f"environment variable {name!r} is under both `environment: "
                    f"inherit:` and `environment: {other}` -- one name, one "
                    "source: remove it from one of them")
    return Environment(
        variables=variables,
        files=_parse_files(section.get("files")),
        secrets=secrets,
        inherit=inherit,
    )


def _parse_variables(raw: object) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AuthoringError("`environment: variables:` must be a mapping of "
                             f"NAME to value, got {type(raw).__name__}")
    variables = {}
    for name, value in raw.items():
        check_env_name(name, "environment: variables:")
        if isinstance(value, (dict, list)):
            raise AuthoringError(f"environment variable {name!r} must be a "
                                 f"scalar, got {type(value).__name__}")
        if value is None:
            # `FOO:` is YAML null, and str(None) is "None" - a value nobody
            # wrote. Empty and absent are different sessions: the file says.
            raise AuthoringError(
                f"environment variable {name!r} has no value (`{name}:` is YAML "
                f"null) -- write `{name}: ''` for an empty value, or remove the line")
        variables[str(name)] = config_scalar(value)
    return variables


def _parse_files(raw: object) -> list:
    if raw is None:
        return []
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise AuthoringError("`environment: files:` must be a LIST of paths, got "
                             f"{type(raw).__name__} -- order is the whole semantic "
                             "(later wins)")
    files = []
    for path in raw:
        if not isinstance(path, str) or not path.strip():
            raise AuthoringError("`environment: files:` entries must be non-empty "
                                 f"paths, got {path!r}")
        files.append(path)
    return files


def _parse_secrets(raw: object) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise AuthoringError("`environment: secrets:` must be a mapping of NAME "
                             f"to 'backend:ref', got {type(raw).__name__}")
    secrets = {}
    for name, spec in raw.items():
        check_env_name(name, "environment: secrets:")
        if not isinstance(spec, str):
            raise AuthoringError(f"secret {name!r} must be a 'backend:ref' "
                                 f"string, got {type(spec).__name__}")
        split_secret_ref(spec)          # a bad ref is an authoring error
        secrets[str(name)] = spec
    return secrets


def _parse_inherit(raw: object) -> tuple:
    """`inherit: [NAME, ...]`: the names this agent takes from the
    launcher's own environment. A list, as `files:` is: a bare string would
    otherwise be read one character at a time. Repeats collapse."""
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise AuthoringError("`environment: inherit:` must be a LIST of environment "
                             f"variable names, got {type(raw).__name__}")
    for name in raw:
        check_env_name(name, "environment: inherit:")
    return tuple(dict.fromkeys(raw))


def check_env_name(name: object, where: str) -> None:
    if not isinstance(name, str) or not ENV_NAME.match(name):
        raise AuthoringError(
            f"{where} {name!r} is not a valid environment variable name "
            "(letters, digits and underscore, not starting with a digit)")


def split_secret_ref(spec: str) -> tuple[str, str]:
    """`backend:ref` -> (backend, ref)."""
    backend, separator, ref = spec.partition(":")
    if not separator or not backend or not ref:
        raise AuthoringError(
            f"secret reference {spec!r} must be 'backend:ref' (e.g. "
            f"'pass:some/path'); known backends: {', '.join(sorted(SECRET_BACKENDS))}")
    return backend, ref


class DeclaredSecret(NamedTuple):
    """A secret as a dry run sees it: named, never resolved."""

    name: str
    backend: str
    ref: str


def declared_secrets(secrets: dict) -> list[DeclaredSecret]:
    """Resolving a credential is an act - a backend subprocess, a key
    touch, an audit entry - and a dry run performs none."""
    return [DeclaredSecret(name, *split_secret_ref(spec))
            for name, spec in secrets.items()]


# ---------- the generated bash prelude ---------------------------------------

def render_env_prelude(environment: Environment,
                       secret_file: str | None = None) -> str:
    """The bash that establishes the declared environment, in this order:
    `variables:`, then `files:` in declared order (the more local file wins,
    over variables too), then secrets LAST so a stray `.env` line cannot
    shadow a declared credential. Every path and value is quoted by shlex;
    every file is sourced under `set -a`, so what it sets is exported to the
    process that follows, and only to it."""
    lines = [
        "# --- session start: declared environment -----------------",
        "# Runs inside the launched process's own shell, after the cd and",
        "# before the exec. Nothing here touches the tmux server, the calling",
        "# shell, or a sibling pane.",
    ]
    if environment.variables:
        lines.append("# environment: variables:")
        for name, value in environment.variables.items():
            check_env_name(name, "environment: variables:")
            lines.append(f"export {name}={shlex.quote(value)}")
    if environment.files:
        lines.append("# environment: files: -- declared order, later wins")
        for path in environment.files:
            lines.extend(_render_source_file(path))
    if secret_file:
        quoted = shlex.quote(str(secret_file))
        lines.extend([
            "# environment: secrets: and inherit: -- values arrive in a 0600",
            "# file (never argv, never this script), sourced once and removed.",
            f"if [ -r {quoted} ]; then set -a; . {quoted}; set +a; rm -f {quoted}; else",
            f"  printf '%s\\n' 'session start: secret file missing: '{quoted} >&2",
            "fi",
        ])
    lines.append("# --- end declared environment -------------------------------------")
    return "\n".join(lines) + "\n"


def _render_source_file(path: str) -> list[str]:
    """Source one declared file. A `./` path stays relative (bash resolves
    it in the session's directory) and may be silently absent - "no
    override in this project" is its normal state; any other path is
    expanded here and its absence is said out loud."""
    if path.startswith(RELATIVE_PREFIX):
        quoted, complain = shlex.quote(path), False
    else:
        quoted, complain = shlex.quote(os.path.expanduser(path)), True
    line = f"if [ -r {quoted} ]; then set -a; . {quoted}; set +a;"
    if complain:
        return [
            line + " else",
            f"  printf '%s\\n' 'session start: env file not found: '{quoted} >&2",
            "fi",
        ]
    return [line + " fi"]


def warn_env_file_traps(environment: Environment, work_dir: str) -> None:
    """A sourced env file that arms its own `trap` REPLACES the launcher's
    credential-shred trap (bash traps are last-one-wins per signal). A
    warning, and only when the launch carries secrets - without them there
    is nothing a clobbered shred could leak. An `inherit:` list counts: what
    it carries travels in the same file."""
    if not environment.secrets and not environment.inherit:
        return
    for path in environment.files:
        candidate = (os.path.join(work_dir, path)
                     if path.startswith(RELATIVE_PREFIX)
                     else os.path.expanduser(path))
        try:
            with open(candidate, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        if re.search(r"(^|[;&|`$(]\s*)trap\s", text, re.MULTILINE):
            print(f"{PROG}: note: env file {path} contains a `trap` command -- a trap "
                  "on EXIT/HUP/INT/TERM in a sourced file silently REPLACES "
                  "the launcher's credential-shred/reaper trap (bash traps "
                  "are last-one-wins per signal)",
                  file=sys.stderr)


#: The SESSION'S identity: names `session start` mints and never hands down
#: from its caller. The id chain is sessiontools' one copy (SESSION_ID_VARS);
#: the key handles and the git signing block are session-key-init's env
#: block. A launch from inside a live seat once handed the PARENT's
#: CLAUDE_CODE_SESSION_ID to the context generators, and the resolver reads
#: that name first. It handed the parent's signing block on too, so
#: an unkeyed child's git signed as the parent through the parent's shim
#: and, with the socket gone, failed every commit.
#: LAUNCH_INFO_ENV is the launch-info marker: the id this launch's
#: launch-info carries, which the session-info hook leaves out of its block.
#: A parent's marker must never reach a child launched without launch-info,
#: so it is scrubbed here and exported again only by `render_launch_script`.
SESSION_IDENTITY_VARS = (*SESSION_ID_VARS,
                         "CLAUDE_SESSION_AGENT_SOCK", "CLAUDE_SESSION_AGENT_PID",
                         "CLAUDE_SESSION_PUB", "GIT_CONFIG_COUNT", LAUNCH_INFO_ENV)

#: The rest of the signing block: git's indexed env-config pairs, an open
#: family, so they are named by PREFIX. session-key-init owns the whole
#: channel (it writes GIT_CONFIG_COUNT outright, never appends), so the
#: family goes, not just its six keys. Never GIT_CONFIG_GLOBAL, _SYSTEM,
#: _NOSYSTEM or _PARAMETERS: those are not the session's (the launcher reads
#: GIT_CONFIG_GLOBAL as the agent's own override file). A prefix, not a
#: digit pattern, so the script's bash `${!prefix@}` and this module drop
#: exactly the same names.
SESSION_IDENTITY_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


#: CREDENTIALS: names that hold an account's key. A launch never hands these down from its caller:
#: `host_environ` drops them, so no child, no tmux server it births and no
#: in-place exec starts holding them, and the launch script's first line
#: unsets them, for a pane on a server something else birthed. The way a
#: credential DOES reach a session is declared: `secrets:` (a backend) or
#: `inherit:` (the launcher's own value, by name). ANTHROPIC_AUTH_TOKEN joins
#: the other two because `session account`'s preflight already treats it as one.
#: Extending the set is one line.
CREDENTIAL_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def is_session_identity(name: str) -> bool:
    """Is NAME part of a session's identity: a SESSION_IDENTITY_VARS name,
    or one of the SESSION_IDENTITY_PREFIXES family? The ONE test every scrub
    applies, Python-side and (by `render_identity_unset`) in the script."""
    return name in SESSION_IDENTITY_VARS or name.startswith(SESSION_IDENTITY_PREFIXES)


def without_session_identity(environ: Mapping[str, str]) -> dict[str, str]:
    """`environ` minus every session-identity name (`is_session_identity`):
    what a child of the launch may inherit before the NEW session's identity
    is laid on top."""
    return {name: value for name, value in environ.items()
            if not is_session_identity(name)}


def render_identity_unset() -> str:
    """The launch script's first line: the same names `is_session_identity`
    drops, then CREDENTIAL_VARS, as bash. The fixed names are
    listed; the prefix family expands by name at run time
    (`${!GIT_CONFIG_KEY_@}`: bash 3.2 and 5.x, empty and harmless when none
    are set, even under `set -u`). The credentials are the backstop for a
    pane on a tmux server sessiontools did not birth (a human's shell, `tm
    kick`): it holds whatever that server holds. An `inherit:` name comes
    back AFTER this line, from the secret file the prelude sources last."""
    return " ".join(["unset", *SESSION_IDENTITY_VARS,
                     *("${!" + prefix + "@}" for prefix in SESSION_IDENTITY_PREFIXES),
                     *CREDENTIAL_VARS])


def host_environ(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment a launch hands its CHILDREN: the user's, with any
    Python venv the launcher itself runs in taken out, and no credential
    (CREDENTIAL_VARS). Measured in the old launcher: a leaked
    VIRTUAL_ENV made every child's `python3` a stripped venv interpreter,
    and session-key-init's keybirth.py died on `import cryptography`. The
    same holds for `uv run session ...` here.

    Every child starts here: the in-place exec, every tmux call (so a
    server `new-session` births holds no credential for its later panes),
    the context generators and key-init. A credential the session needs is
    declared (`secrets:` or `inherit:`) and arrives by the 0600 file."""
    env = dict(os.environ if environ is None else environ)
    for name in CREDENTIAL_VARS:
        env.pop(name, None)
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        venv_bin = os.path.join(venv, "bin")
        env["PATH"] = os.pathsep.join(
            entry for entry in env.get("PATH", "").split(os.pathsep)
            if entry and entry != venv_bin)
    return env
