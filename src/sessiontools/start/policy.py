"""The two gates a launch passes: Fable, and the committer.

THE FABLE GATE. Fable is DENIED BY DEFAULT: the denial IS the
absence of `AGENT_ALLOW_FABLE` (1/true/yes/on), so a fresh box, a fresh
shell and a stripped agent environment all deny without anyone
remembering. It runs before ANY act of the launcher - before a mint, a
file, a window - and scans every door a model can arrive through: `--model`
in the agent file's `claude:` section and in the CLI passthrough (both
spellings, EVERY occurrence - "fable anywhere" is the fail-closed reading,
not last-wins), and every ANTHROPIC_*MODEL* / CLAUDE_CODE_*MODEL* variable
in the effective environment (host + vendor defaults + declared). The match
is a case-insensitive SUBSTRING of the value, deliberately: 'fable' and
'claude-fable-5' are both Fable, and a future 'claude-fable-6' must be
refused too. Do not "tighten" it into an equality test.

The refusal speaks of THE OPERATOR - the human who approves Fable on this
box - never a named person. It never dead-ends: it names the
operator's ask address or, where none is deliverable, says how to name one
- and always the fallback of mailing the initiator. The address is, in
order: `AGENT_OPERATOR_ADDR`; else the `session` tool's settings (template
and host layers, `~/.sessionkit/tools/settings/session.json`): `operator.address`
as written, else `operator.mailbox` derived to `<mailbox>@<FQDN>` only
where that maildir is local and the FQDN has a domain; else none. A
settings file that cannot be read costs the refusal its address, never the
refusal. Since the session repo retired, the ask is `message send`
(sessionmail).

THE COMMITTER. When the launch environment carries an AI committer identity
(GIT_COMMITTER_NAME), it is overridden with the human's from the work
tree's own git config, or unset when no human is resolvable. Never a guess.
It is KEPT only where the store has opted in to seat identity: committer
identity is a STORE setting (`commit_as`) with a PROJECT override; the
default is the human. That setting REPLACED the directory-prefix whitelist
(`~/.config/claude-agents/ai-committer-allowed`) - same safe default, wrong
axis - and the whitelist is gone from here.

This module parses no STORE.md or PROJECT.md. `tm kick` is the one reader
of `commit_as` (taskmaster `kick_dispatch.commit_identity_policy`), and
`tm kick` writes its verdict into every spawn's environment beside the GIT_*
vars: TM_COMMIT_AS (`seat` or `human`) and TM_COMMIT_AS_ROOT (the resolved
store root). The rule: keep the AI
committer only when TM_COMMIT_AS is exactly `seat`, AND TM_COMMIT_AS_ROOT is
non-empty and absolute, AND the work dir's realpath is the root's realpath
or lies under it by path component (os.path.commonpath, never a string
prefix). Anything else fails closed to the human, or unset.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools import operator as _operator

# The operator's ask address and its settings live in sessiontools.operator
# (the one reader); these names stay here for existing callers.
from sessiontools.operator import (  # noqa: F401 - re-exported for callers
    AGENT_OPERATOR_ADDR_ENV,
    DEFAULT_MAIL_ROOT,
    OPERATOR_ADDRESS_KEY,
    OPERATOR_MAILBOX_KEY,
    OPERATOR_SETTINGS_BLOCK,
    OPERATOR_SETTINGS_TOOL,
    SESSION_MAIL_ROOT_ENV,
    read_operator,
)
from sessiontools.start.environment import host_environ
from sessiontools.start.errors import EXIT_USAGE, LaunchError
from sessiontools.start.vendor import VENDORS, merge_argv, vendor_front_matter_argv

AGENT_ALLOW_FABLE_ENV = "AGENT_ALLOW_FABLE"
FABLE_ALLOW_VALUES = frozenset(("1", "true", "yes", "on"))

FABLE_MODEL_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")

# Provenance (2030-06-09): the gate exists so that an agent ASKS
# and the operator raises the model; no agent starts Fable on its own. The
# refusal says that in the operator's terms and quotes no one.
FABLE_REFUSAL_TEMPLATE = r"""REFUSING to launch model '%(model)s' - Fable is not enabled here.

  what   you asked for: %(what)s
         ('fable' and 'claude-fable-5' are the same model - both are Fable)
  rule   Fable is DENIED BY DEFAULT. This gate refuses Fable and nothing else;
         every other model launches unchanged. The denial IS the absence of the
         override, so a fresh box, a fresh shell and a stripped agent
         environment all deny without anyone having to remember.
  how    override: AGENT_ALLOW_FABLE=1 in the environment of whatever starts
         the agent. That variable is the operator's standing approval (the
         operator: the human who approves Fable on this box) - it is meant to
         be an ACT, not a mood.
  next   IF YOU ARE AN AGENT: do not work around this, and do not retry with a
         different spelling. Your move is to ASK:%(ask_line)s
         If that is refused on your host, mail your own initiator (the seat
         that started you) and say you need the operator's
         approval for Fable. Do not work around the gate.
         An agent asks; the operator decides, and raises the model to Fable.
"""


def fable_allowed(environ: Mapping[str, str]) -> bool:
    """Enabled iff the stripped, lowercased value is 1/true/yes/on. Unset,
    empty, '0', a typo - all DENY."""
    value = (environ.get(AGENT_ALLOW_FABLE_ENV) or "").strip().lower()
    return value in FABLE_ALLOW_VALUES


def _resolve_operator_fqdn() -> str:
    """This module's seam for the FQDN lookup (tests patch it); the lookup
    itself is `sessiontools.operator`'s, the one place that asks."""
    return _operator._resolve_fqdn()


def operator_ask_address(environ: Mapping[str, str]) -> str | None:
    """The operator's ask address: AGENT_OPERATOR_ADDR if set; else the
    setting `operator.address`; else `<operator.mailbox>@<host>` only when
    that mailbox is local, where `<host>` is `sessiontools.operator.host_part`
    (the name the mail drop takes as this host, never a PTR name); else
    None - never a look-alike address that delivers nowhere. Read by
    `sessiontools.operator.read_operator`, the one reader."""
    return read_operator(environ, resolve_fqdn=_resolve_operator_fqdn).address


def _ask_line(address: str | None) -> str:
    """The refusal's ask-the-operator line: a real `message send` command,
    or - no deliverable address - a non-empty sentence naming every knob
    that names one."""
    if address is None:
        return (" this host knows no deliverable\n"
                "         address for the operator -- name one with AGENT_OPERATOR_ADDR,\n"
                "         or with operator.address (or operator.mailbox, a local box)\n"
                "         in ~/.sessionkit/tools/settings/session.json.")
    return (f"\n           message send {address} \\\n"
            "             -s 'Fable request: <what for>' -f <file>")


def extract_flag_values(flag: str, args: list[str]) -> list[str]:
    """Every value given to `flag` in `args`, both spellings."""
    values = []
    for index, token in enumerate(args):
        name, sep, value = token.partition("=")
        if name != flag:
            continue
        if sep:
            values.append(value)
        else:
            following = args[index + 1] if index + 1 < len(args) else ""
            if following and not following.startswith("-"):
                values.append(following)
    return values


def _is_fable_model(value: object) -> bool:
    return "fable" in str(value).lower()


def _is_model_selecting_env_name(name: str) -> bool:
    upper = name.upper()
    return "MODEL" in upper and upper.startswith(FABLE_MODEL_ENV_PREFIXES)


def check_fable_gate(vendor: str, vendor_section: dict, cli_args: list[str],
                     environ: Mapping[str, str], declared_vars: Mapping[str, str],
                     ) -> None:
    """Refuse a Fable launch, exit 2, unless AGENT_ALLOW_FABLE enables it.
    `vendor_section` is the agent file's `claude:` section."""
    if fable_allowed(environ):
        return
    ask_line = _ask_line(operator_ask_address(environ))
    merged = merge_argv([], vendor_front_matter_argv(vendor, vendor_section),
                        list(cli_args))
    for value in extract_flag_values("--model", merged):
        if _is_fable_model(value):
            raise LaunchError(FABLE_REFUSAL_TEMPLATE % {
                "model": value, "what": f"--model {value}",
                "ask_line": ask_line}, code=EXIT_USAGE)
    effective_env = dict(host_environ(environ))
    effective_env.update(VENDORS.get(vendor, {}).get("env") or {})
    effective_env.update(declared_vars)
    for name, value in effective_env.items():
        if _is_model_selecting_env_name(name) and _is_fable_model(value):
            raise LaunchError(FABLE_REFUSAL_TEMPLATE % {
                "model": value, "what": f"{name}={value}",
                "ask_line": ask_line}, code=EXIT_USAGE)


# ---------- the committer identity -------------------------------------------

#: Kick's marker: its commit_as verdict, `seat` or `human`, always written.
COMMIT_AS_ENV = "TM_COMMIT_AS"
#: The store root that verdict was read for; "" when kick did not know it.
COMMIT_AS_ROOT_ENV = "TM_COMMIT_AS_ROOT"
#: The one value that keeps the seat's committer. Exactly this; no other spelling.
COMMIT_AS_SEAT = "seat"


class CommitterIdentity(NamedTuple):
    """mode 'keep' (nothing rendered), 'override' (export name/email), or
    'unset' (unset both; git defaults, not a guess)."""

    mode: str
    name: str
    email: str


COMMITTER_KEEP = CommitterIdentity("keep", "", "")
COMMITTER_UNSET = CommitterIdentity("unset", "", "")


def seat_committer_allowed(work_dir: str, environ: Mapping[str, str]) -> bool:
    """d01's rule: the marker says `seat` exactly, its root is non-empty and
    absolute, and the work dir is that root or lies under it BY PATH
    COMPONENT. realpath on both sides (on a real host ~/.taskmaster can be a
    symlink to the store). Anything unexpected is False: fail closed."""
    if environ.get(COMMIT_AS_ENV) != COMMIT_AS_SEAT:
        return False
    root = environ.get(COMMIT_AS_ROOT_ENV, "")
    if not root or not os.path.isabs(root):
        return False
    try:
        real_root = os.path.realpath(root)
        real_work = os.path.realpath(os.path.abspath(work_dir))
        return os.path.commonpath([real_root, real_work]) == real_root
    except (OSError, ValueError):
        return False


def resolve_committer_identity(work_dir: str,
                               environ: Mapping[str, str] | None = None,
                               ) -> CommitterIdentity:
    """Keep when there is no AI committer, or when the store opted in
    (`seat_committer_allowed`). Otherwise the human, looked up WITHOUT
    GIT_CONFIG_GLOBAL (the agent's own override file), so git reads the
    human's ~/.gitconfig cascade. Both name AND email, or unset."""
    environ = os.environ if environ is None else environ
    if not environ.get("GIT_COMMITTER_NAME"):
        return COMMITTER_KEEP
    if seat_committer_allowed(work_dir, environ):
        return COMMITTER_KEEP
    effective = os.path.abspath(work_dir)
    lookup_env = {k: v for k, v in environ.items() if k != "GIT_CONFIG_GLOBAL"}
    found = {}
    if os.path.isdir(effective):
        for key in ("user.name", "user.email"):
            result = subprocess.run(["git", "-C", effective, "config", key],
                                    capture_output=True, text=True, env=lookup_env,
                                    check=False)
            if result.returncode == 0 and result.stdout.strip():
                found[key] = result.stdout.strip()
    if found.get("user.name") and found.get("user.email"):
        return CommitterIdentity("override", found["user.name"], found["user.email"])
    return COMMITTER_UNSET
