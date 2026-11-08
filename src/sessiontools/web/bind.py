"""The address the front door may listen on, checked before anything listens.

PROPOSED (kit-web-shell design §5; spec "One front door binds loopback or a
literal tailnet address"). `check_bind(host)` accepts:

- `127.0.0.1` (the default) or `::1`, loopback;
- a LITERAL tailnet address, in `100.64.0.0/10` (the CGNAT range)
  or `fd7a:115c:a1e0::/48` (the tailnet ULA prefix), given explicitly.

It refuses everything else, each refusal naming the address: `0.0.0.0` and
`::` (every interface), `""` (which a socket reads as every interface), any
other address, and ANY host name. A name can resolve anywhere, and to
something else tomorrow, so `localhost` is refused as well: the check reads
the string it was given and never resolves it. No socket is touched here.

Nothing here opens a port to the internet. Exposing the front door to the
tailnet (through a TLS proxy) is a separate go; to the internet, never.
"""

from __future__ import annotations

import ipaddress

#: The default bind address.
DEFAULT_HOST = "127.0.0.1"

#: The loopback addresses accepted, by value.
LOOPBACK = (ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1"))

#: The tailnet ranges a literal address is accepted from. Each is spelled
#: once, here; the refusal below renders them from this tuple.
TAILNET = (ipaddress.ip_network("100.64.0.0/10"),
           ipaddress.ip_network("fd7a:115c:a1e0::/48"))


class BindRefused(ValueError):
    """An address the front door will not listen on. The message names it."""


def check_bind(host: str) -> str:
    """HOST, as the canonical address to bind, or BindRefused naming it.

    HOST must be a literal IP address, exactly: no surrounding whitespace,
    no brackets, no zone (`%en0`), no port. `0:0:0:0:0:0:0:1` is `::1` and
    is returned as `::1`."""
    if not isinstance(host, str):
        raise BindRefused(f"bind address must be a string, not {type(host).__name__}")
    if host == "":
        raise BindRefused("refusing to bind '': an empty host listens on every interface; "
                          f"use {DEFAULT_HOST}")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused(
            f"refusing to bind {host!r}: not a literal IP address (a host name can "
            f"resolve anywhere; give {DEFAULT_HOST}, ::1 or a literal tailnet address)"
        ) from None
    if addr.is_unspecified:
        raise BindRefused(f"refusing to bind {host!r}: it listens on every interface; "
                          f"use {DEFAULT_HOST}")
    if isinstance(addr, ipaddress.IPv6Address) and addr.scope_id:
        raise BindRefused(f"refusing to bind {host!r}: a scoped address is not loopback "
                          "or tailnet")
    if addr in LOOPBACK:
        return str(addr)
    if any(addr.version == net.version and addr in net for net in TAILNET):
        return str(addr)
    raise BindRefused(f"refusing to bind {host!r}: only {DEFAULT_HOST}, ::1 or a literal "
                      f"tailnet address ({', '.join(str(net) for net in TAILNET)})")
