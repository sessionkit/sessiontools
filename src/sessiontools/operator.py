"""The operator and the privileged seats, from ONE setting.

The operator is the human other seats ask (for Fable, for a decision); the
privileged seats may act as administrators. Both come from the `session`
tool's settings (`~/.sessionkit/tools/settings/session.json`, template and host
layers, `_locked` honoured, read through `sessiontools.settings`), never
from a name in code:

    {"operator": {"address": "you@host.example", "mailbox": "you",
                  "privileged": ["alice", "bob"]}}

    from sessiontools import read_operator, Operator
    op = read_operator()            # environ defaults to os.environ
    op.address, op.mailbox, op.privileged, op.is_privileged(who)

Rules:

1. `address`: env AGENT_OPERATOR_ADDR; else `operator.address`; else
   `<operator.mailbox>@<FQDN>` only when that maildir is local AND the FQDN
   has a dot; else None (the order moved here from start/policy.py).
2. `privileged`: every string in `operator.privileged` (stripped, empties
   dropped), plus the operator as SETTINGS name it: `operator.address`, else
   the derived `<mailbox>@<FQDN>`, and `operator.mailbox`. The operator is
   always privileged.
3. AGENT_OPERATOR_ADDR grants NO privilege. Any process can set it; it may
   redirect where an ask goes, never make its setter an administrator.
4. Nothing configured: no address, no mailbox, nothing privileged.
5. FAIL SAFE: an unreadable / non-JSON / bad-`_locked` / misshapen file
   reads as nothing configured; `privileged` that is not a list is ignored,
   non-string members are dropped. A bad file can only REMOVE privilege.
6. No person is a default, and nothing is guessed.

`session operator` prints the same as one line of JSON for non-Python
consumers: {"operator", "mailbox", "privileged"}.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Mapping
from typing import NamedTuple

# Overrides every other source of the operator's ask address outright, and
# grants nothing (rule 3).
AGENT_OPERATOR_ADDR_ENV = "AGENT_OPERATOR_ADDR"
# The setting: the `session` tool, `operator` block.
OPERATOR_SETTINGS_TOOL = "session"
OPERATOR_SETTINGS_BLOCK = "operator"
OPERATOR_ADDRESS_KEY = "address"        # a full address, used as written
OPERATOR_MAILBOX_KEY = "mailbox"        # a local box name, derived below
OPERATOR_PRIVILEGED_KEY = "privileged"  # a list of seat names or addresses
# '<mailbox>@<this box's FQDN>' is a real address only on the box that
# hosts that mailbox; the local maildir's existence is the measured fact.
SESSION_MAIL_ROOT_ENV = "SESSION_MAIL_ROOT"
DEFAULT_MAIL_ROOT = "~/.mail/agents"


class Operator(NamedTuple):
    """Who the operator is, and who is privileged. A frozen value."""

    address: str | None
    mailbox: str | None
    privileged: frozenset[str]

    def is_privileged(self, who: object) -> bool:
        """WHO (a seat name or an address) is privileged: an exact match
        after strip, case-insensitive. Anything not a non-empty string is
        not privileged."""
        if not isinstance(who, str) or not who.strip():
            return False
        wanted = who.strip().casefold()
        return any(member.casefold() == wanted for member in self.privileged)


def _resolve_fqdn() -> str:
    """The one seam between the derivation and the network (tests patch it)."""
    return socket.getfqdn()


def _settings_block(environ: Mapping[str, str]) -> Mapping:
    """The `operator` block of the `session` tool's template and host
    layers, or {}. Nothing a settings file holds raises out of here."""
    from sessiontools import settings
    try:
        merged, _o, _l, _v = settings.merge(
            settings.sources(OPERATOR_SETTINGS_TOOL, None, env=environ))
        block = merged.get(OPERATOR_SETTINGS_BLOCK)
    except Exception:  # noqa: BLE001 - a bad file reads as nothing configured
        return {}
    return block if isinstance(block, Mapping) else {}


def _setting_text(block: Mapping, key: str) -> str:
    value = block.get(key)
    return value.strip() if isinstance(value, str) else ""


def _privileged_setting(block: Mapping) -> set[str]:
    """`operator.privileged` if it is a list: its strings, stripped,
    empties dropped. A string, a map, a number: ignored as if absent."""
    value = block.get(OPERATOR_PRIVILEGED_KEY)
    if not isinstance(value, list):
        return set()
    return {member.strip() for member in value
            if isinstance(member, str) and member.strip()}


def _mailbox_is_local(mailbox: str, environ: Mapping[str, str]) -> bool:
    """MAILBOX is one plain name (no separator, not '.'/'..', no '@') AND
    that maildir exists under the mail root."""
    if (not mailbox or mailbox in (".", "..") or "@" in mailbox
            or os.sep in mailbox or (os.altsep and os.altsep in mailbox)):
        return False
    root = environ.get(SESSION_MAIL_ROOT_ENV) or DEFAULT_MAIL_ROOT
    return os.path.isdir(os.path.join(os.path.expanduser(root), mailbox))


def _derived_address(mailbox: str, environ: Mapping[str, str],
                     resolve_fqdn: Callable[[], str]) -> str | None:
    """`<mailbox>@<FQDN>` only when the mailbox is local and the FQDN has a
    domain - never a look-alike address that delivers nowhere."""
    if not _mailbox_is_local(mailbox, environ):
        return None
    fqdn = (resolve_fqdn() or "").strip()
    if not fqdn or "." not in fqdn:
        return None
    return f"{mailbox}@{fqdn}"


def read_operator(environ: Mapping[str, str] | None = None, *,
                  resolve_fqdn: Callable[[], str] | None = None) -> Operator:
    """The operator and the privileged set, read from settings (module
    docstring, rules 1-6). RESOLVE_FQDN replaces the FQDN lookup (a seam
    for callers and tests); the default asks the network."""
    environ = os.environ if environ is None else environ
    resolve_fqdn = _resolve_fqdn if resolve_fqdn is None else resolve_fqdn
    block = _settings_block(environ)
    mailbox = _setting_text(block, OPERATOR_MAILBOX_KEY)
    # The operator as the SETTINGS name it: the only source of privilege.
    configured = (_setting_text(block, OPERATOR_ADDRESS_KEY)
                  or (mailbox and _derived_address(mailbox, environ, resolve_fqdn))
                  or None)
    privileged = _privileged_setting(block)
    privileged.update(name for name in (configured, mailbox) if name)
    override = (environ.get(AGENT_OPERATOR_ADDR_ENV) or "").strip()
    return Operator(address=override or configured,
                    mailbox=mailbox or None,
                    privileged=frozenset(privileged))


def as_json_object(op: Operator) -> dict:
    """The `session operator` contract: exactly these three keys (keys may
    be ADDED later, never renamed or removed); `privileged` sorted."""
    return {"operator": op.address, "mailbox": op.mailbox,
            "privileged": sorted(op.privileged)}
