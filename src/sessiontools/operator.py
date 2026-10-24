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
   `<operator.mailbox>@<host>` only when that maildir is local; else None
   (the order moved here from start/policy.py). `<host>` is `host_part`:
   the name the local mail drop takes as THIS host, never a reverse-DNS
   (PTR) name.
2. `privileged`: every string in `operator.privileged` (stripped, empties
   dropped), plus the operator as SETTINGS name it: `operator.address`, else
   the derived `<mailbox>@<host>`, and `operator.mailbox`. The operator is
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
# '<mailbox>@<this host>' is a real address only on the box that
# hosts that mailbox; the local maildir's existence is the measured fact.
SESSION_MAIL_ROOT_ENV = "SESSION_MAIL_ROOT"
DEFAULT_MAIL_ROOT = "~/.mail/agents"
# The short host, in the mail drop's order: these variables (first set
# wins), else the `host` key of the `mail` tool's settings, else the first
# label of the hostname. The drop takes an address as on THIS host only
# when its domain is the short host or a name under it.
MAIL_HOST_ENVS = ("SESSIONMAIL_HOST", "SESSION_MAIL_HOST")
MAIL_SETTINGS_TOOL = "mail"
MAIL_HOST_KEY = "host"
# Reverse-DNS names are never a mail host, whatever they sit under.
REVERSE_DNS_SUFFIX = ".arpa"


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


def _resolve_hostname() -> str:
    """The one seam for this box's own hostname (tests patch it)."""
    return socket.gethostname()


def _mail_settings_host(environ: Mapping[str, str]) -> str:
    """The `host` key of the `mail` tool's template and host layers,
    stripped, or "". Nothing a settings file holds raises out of here."""
    from sessiontools import settings
    try:
        merged, _o, _l, _v = settings.merge(
            settings.sources(MAIL_SETTINGS_TOOL, None, env=environ))
        value = merged.get(MAIL_HOST_KEY)
    except Exception:  # noqa: BLE001 - a bad file reads as no host set
        return ""
    return value.strip() if isinstance(value, str) else ""


def short_host(environ: Mapping[str, str],
               resolve_hostname: Callable[[], str] | None = None) -> str:
    """This host's short name, lowercased, in the mail drop's order (the
    variables, the mail setting, the hostname's first label); "" if none."""
    resolve_hostname = _resolve_hostname if resolve_hostname is None else resolve_hostname
    for name in MAIL_HOST_ENVS:
        value = (environ.get(name) or "").strip()
        if value:
            return value.lower()
    found = _mail_settings_host(environ)
    if not found:
        found = (resolve_hostname() or "").strip().split(".")[0]
    return found.lower()


def is_this_host(domain: str, host: str) -> bool:
    """DOMAIN names this host: it is HOST, or a name under it (the mail
    drop's test, case-insensitive), and it is not a reverse-DNS name."""
    domain, host = domain.strip().lower(), host.strip().lower()
    if not host or domain.endswith(REVERSE_DNS_SUFFIX):
        return False
    return domain == host or domain.startswith(host + ".")


def host_part(environ: Mapping[str, str], *,
              resolve_fqdn: Callable[[], str] | None = None,
              resolve_hostname: Callable[[], str] | None = None) -> str | None:
    """The host part of a derived local address: the FQDN when it names
    this host (a name under the short host, such as a tailnet name), else
    the short host. A PTR name or an unrelated FQDN is never used. None
    when there is no short host to fall back to."""
    resolve_fqdn = _resolve_fqdn if resolve_fqdn is None else resolve_fqdn
    host = short_host(environ, resolve_hostname)
    if not host:
        return None
    fqdn = (resolve_fqdn() or "").strip().lower()
    return fqdn if is_this_host(fqdn, host) else host


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
    """`<mailbox>@<host>` only when the mailbox is local and `host_part`
    names this host - never a look-alike address that delivers nowhere."""
    if not _mailbox_is_local(mailbox, environ):
        return None
    host = host_part(environ, resolve_fqdn=resolve_fqdn)
    return f"{mailbox}@{host}" if host else None


def read_operator(environ: Mapping[str, str] | None = None, *,
                  resolve_fqdn: Callable[[], str] | None = None) -> Operator:
    """The operator and the privileged set, read from settings (module
    docstring, rules 1-6). RESOLVE_FQDN replaces the FQDN lookup (a seam
    for callers and tests); the default asks the network. The FQDN is used
    only when it names this host (`host_part`)."""
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
