"""The front door's proxy: one request from the browser, forwarded to a room
over its unix socket, and the room's answer streamed back.

PROPOSED (kit-web-shell design §2 and §6; spec "A room is reachable only
through the front door" and "Streams pass through unbuffered").

ON THE WAY IN, the proxy:

- REMOVES the browser's `kit_session` cookie (other cookies pass), every
  inbound `X-Sessionkit-*` header (spelled with `_` too, which a WSGI or
  CGI room would read as the same name), and the hop-by-hop headers;
- SETS `X-Sessionkit-User` (who the front door logged in) and
  `X-Sessionkit-Prefix` (e.g. `/talk`), which a room in kit mode trusts
  over its socket only;
- strips the prefix: `/talk/events?x` reaches the room as `/events?x`.

ON THE WAY OUT, it drops any `Set-Cookie: kit_session` the room sends, so a
room can neither set nor overwrite the front door's session, and the
hop-by-hop headers. It copies the body AS IT ARRIVES, flushing each read,
so each Server-Sent Event reaches the browser when it is written. A body
of unknown length is sent close-delimited.

Before it dials, the socket must be private (`unixhttp.private_socket`).
A WebSocket upgrade is answered 501: the splice is designed (§6) and built
when phone becomes a room, not in the spike.

Stdlib only.
"""

from __future__ import annotations

import http.client
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from sessiontools.web import auth, unixhttp

#: The headers that belong to one hop, never forwarded (RFC 9110 §7.6.1).
HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                        "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade"})
#: The prefix of the headers the front door owns.
IDENTITY_PREFIX = "x-sessionkit-"
USER_HEADER = "X-Sessionkit-User"
PREFIX_HEADER = "X-Sessionkit-Prefix"
#: The largest request body forwarded.
MAX_BODY = 10 * 1024 * 1024
#: Bytes per read when copying a response.
CHUNK = 64 * 1024


def room_target(path: str, prefix: str) -> str:
    """PATH (the request target) as the room sees it: PREFIX stripped.
    `/talk/` and `/talk/x?y` become `/` and `/x?y`. ValueError when PATH is
    not under PREFIX + `/`."""
    if not prefix.startswith("/") or prefix.endswith("/"):
        raise ValueError(f"prefix {prefix!r}: must start with / and not end with one")
    if not path.startswith(prefix + "/"):
        raise ValueError(f"{path!r} is not under {prefix}/")
    return path[len(prefix):]


def _connection_tokens(values: Iterable[str]) -> set[str]:
    return {t.strip().lower() for v in values for t in v.split(",") if t.strip()}


def strip_session_cookie(value: str) -> str:
    """A Cookie header's VALUE with every `kit_session` pair removed."""
    keep = []
    for part in value.split(";"):
        k, _, _ = part.strip().partition("=")
        if part.strip() and k.strip() != auth.COOKIE_NAME:
            keep.append(part.strip())
    return "; ".join(keep)


def request_headers(headers, *, user: str, prefix: str) -> list[tuple[str, str]]:
    """The headers to send the room: the browser's, less hop-by-hop, less
    every `X-Sessionkit-*`, less `kit_session` from Cookie; plus the front
    door's own identity headers. HEADERS is an `email.message.Message`
    (what `http.server` gives a handler) or a list of pairs."""
    items = list(headers.items()) if hasattr(headers, "items") else list(headers)
    drop = HOP_BY_HOP | _connection_tokens(v for k, v in items if k.lower() == "connection")
    out = []
    for k, v in items:
        lk = k.lower()
        # `_` too: a room behind WSGI/CGI reads X_Sessionkit_User and
        # X-Sessionkit-User as the same HTTP_X_SESSIONKIT_USER
        if lk in drop or lk.replace("_", "-").startswith(IDENTITY_PREFIX) or lk == "content-length":
            continue
        if lk == "cookie":
            v = strip_session_cookie(v)
            if not v:
                continue
        out.append((k, v))
    out.append((USER_HEADER, user))
    out.append((PREFIX_HEADER, prefix))
    return out


def _is_session_set_cookie(value: str) -> bool:
    name = value.split(";", 1)[0].partition("=")[0].strip()
    return name.lower() == auth.COOKIE_NAME


def response_headers(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """The room's response headers as the browser gets them: less
    hop-by-hop, and less any `Set-Cookie` of `kit_session`."""
    pairs = list(pairs)
    drop = HOP_BY_HOP | _connection_tokens(v for k, v in pairs if k.lower() == "connection")
    return [(k, v) for k, v in pairs
            if k.lower() not in drop
            and not (k.lower() == "set-cookie" and _is_session_set_cookie(v))]


def _plain(handler: BaseHTTPRequestHandler, status: int, text: str) -> int:
    body = (text + "\n").encode()
    handler.send_response_only(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
    return status


def forward(handler: BaseHTTPRequestHandler, socket_path: Path, *, prefix: str, user: str,
            timeout: float = 30) -> int:
    """Forward HANDLER's request (already through `auth.gate`) to the room at
    SOCKET_PATH, under PREFIX, as USER; stream the answer back. Returns the
    status sent to the browser."""
    try:
        target = room_target(handler.path, prefix)
    except ValueError:
        return _plain(handler, 404, "not found")
    if "websocket" in (handler.headers.get("Upgrade") or "").lower():
        return _plain(handler, 501, "WebSocket upgrades are not proxied yet")
    if handler.headers.get("Transfer-Encoding"):
        return _plain(handler, 411, "a request body needs a Content-Length")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return _plain(handler, 400, "bad Content-Length")
    if length < 0 or length > MAX_BODY:
        return _plain(handler, 413, "request body too large")
    body = handler.rfile.read(length) if length else None
    title = prefix.strip("/")
    try:
        unixhttp.private_socket(socket_path)
    except FileNotFoundError:
        return _plain(handler, 502, f"{title}: not running")
    except unixhttp.SocketRefused as e:
        return _plain(handler, 502, f"{title}: its socket is refused ({e})")
    conn = unixhttp.UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        try:
            conn.putrequest(handler.command, target, skip_host=True, skip_accept_encoding=True)
            for k, v in request_headers(handler.headers, user=user, prefix=prefix):
                conn.putheader(k, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException):
            return _plain(handler, 502, f"{title}: not answering")
        out = response_headers(resp.getheaders())
        sized = any(k.lower() == "content-length" for k, _ in out)
        handler.send_response_only(resp.status, resp.reason)
        for k, v in out:
            handler.send_header(k, v)
        no_body = handler.command == "HEAD" or resp.status in (204, 304) or resp.status < 200
        if not sized and not no_body:
            handler.send_header("Connection", "close")
            handler.close_connection = True
        handler.end_headers()
        if no_body:
            return resp.status
        try:
            while True:
                chunk = resp.read1(CHUNK)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except (TimeoutError, BrokenPipeError, ConnectionResetError, http.client.HTTPException):
            handler.close_connection = True
        return resp.status
    finally:
        conn.close()
