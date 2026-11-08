"""The front door: one address, one login, one menu, Settings built in, and a
room for each installed package.

PROPOSED (kit-web-shell design §3, §5 and §7). `serve(host, port)` runs
`check_bind` BEFORE any socket is opened, then listens on that address
only (`127.0.0.1` by default).

Every request passes `auth.gate` first (Host, Origin, session). Without a
session a page request is sent to `/login` and anything else gets a 401,
so the menu, Settings, every room and every hint sit behind the login.

- `/login`: the pairing-code form; a correct POST sets `kit_session`.
- `/logout` (POST): ends this browser's session.
- `/`: the menu. Settings first, then each catalog room: a link when it
  is up, *"Talk: not running, restarting"* when it is down, and its
  install hint (*"install sessionvoice to get Talk"*) when it is absent.
- `/settings/`: READ-ONLY. Each tool in `settings.PACKAGES`, then each
  tool `settings.discovered_tools` finds by its installed schema, sorted.
  For each: the values, origins and locks of its template and host layers
  (`settings.sources` with no session, or `settings.folder_sources` for a
  discovered tool, then `settings.merge`), drawn from its schema by
  `settings_page.draw_tool`, or as a flat table when it has none. GET and
  HEAD only; there is no write path at all.
- `/<room>/...`: proxied to the room's unix socket (`proxy.forward`). An
  absent room is a 404 with its hint.

The rooms come from `rooms.json` in the web dir, written by discovery at
start and by `session web rescan`; the front door re-reads it when it
changes and restarts its supervisor on the new set. Nothing is logged:
a request line can carry anything.

Stdlib only.
"""

from __future__ import annotations

import base64
import getpass
import html
import json
import os
import signal
import socket
import socketserver
import sys
import threading
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from sessiontools import settings
from sessiontools.web import auth, proxy, rooms, settings_page, unixhttp
from sessiontools.web.bind import check_bind
from sessiontools.web.supervisor import Supervisor

#: `session web serve`'s default port.
DEFAULT_PORT = 8750
#: The largest login form body read.
MAX_FORM = 4096
#: Headers on every page the front door writes itself. The referrer policy
#: is `same-origin`, NOT `no-referrer`: under the Fetch spec a form POST
#: from a `no-referrer` page carries `Origin: null`, which `auth.gate`
#: refuses, so no login or logout could ever succeed. `same-origin` sends
#: the real Origin (and Referer) to the front door itself and nothing to
#: any other site (design §3, "Referrer-Policy and Origin").
PAGE_HEADERS = (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'"),
                ("Referrer-Policy", "same-origin"))


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>{html.escape(title)}"
            f"</title></head>\n<body>\n<h1>{html.escape(title)}</h1>\n{body}\n</body></html>\n"
            ).encode()


LOGIN_FORM = ('<form method="post" action="/login">'
              '<label>Pairing code <input name="code" autocomplete="one-time-code" autofocus>'
              '</label> <button>Log in</button></form>\n'
              "<p>Run <code>session web pair</code> on the host for a code.</p>")
LOGOUT_FORM = '<form method="post" action="/logout"><button>Log out</button></form>'
#: The largest passkey ceremony body read (JSON: an RSA key is the longest).
MAX_JSON = 16384
#: The passkey script's path: an open one, so the login page can load it.
PASSKEY_JS_PATH = "/login/passkey.js"
#: What the login page adds when this name has a passkey. The script is a
#: file, not inline: the CSP is `default-src 'self'`.
PASSKEY_SIGNIN = ('<p><button id="passkey-signin" type="button">Sign in with a passkey</button>'
                  '</p>\n<p id="passkey-said" role="status"></p>\n'
                  f'<script src="{PASSKEY_JS_PATH}" defer></script>')
#: What the menu adds for a session a pairing code opened just now.
PASSKEY_ENROL = ('<p><button id="passkey-enrol" type="button">Add a passkey for this device'
                 '</button> Next time, sign in with Touch ID or Face ID instead of a code.</p>\n'
                 '<p id="passkey-said" role="status"></p>\n'
                 f'<script src="{PASSKEY_JS_PATH}" defer></script>')
#: How a passkey is named in the browser's list (the RP's display name).
RP_NAME = "Session Kit"


def passkey_module() -> Any:
    """`sessiontools.web.passkey`, or None when the `passkey` extra is not
    installed (no `cryptography`). Imported only when a passkey is asked
    for, so the front door itself stays stdlib-only."""
    try:
        from sessiontools.web import passkey
    except ImportError:
        return None
    return passkey


def passkey_script() -> bytes:
    """The browser's half of the passkey ceremonies (`passkey.js`)."""
    from importlib.resources import files
    return files("sessiontools.web").joinpath("passkey.js").read_bytes()


def settings_rows(env: dict[str, str] | None = None) -> list[tuple[str, list[tuple[str, str, str]],
                                                                  str | None]]:
    """(tool, [(key, value as JSON, origin file)], error or None) for each
    tool in `settings.PACKAGES`: its template and host layers only, as plain
    rows. The page itself draws through `settings_sections`. Reads files;
    writes nothing, and imports neither PyYAML nor jsonschema."""
    out = []
    for tool in settings.PACKAGES:
        try:
            srcs = settings.sources(tool, None, env=env)
            _values, origins, _locked, leaves = settings.merge(srcs)
        except (settings.SettingsError, OSError, ValueError) as e:
            out.append((tool, [], f"{type(e).__name__}: {e}"))
            continue
        out.append((tool, [(k, json.dumps(leaves[k]), str(origins.get(k, "")))
                           for k in sorted(leaves)], None))
    return out


#: The line under a discovered tool's heading.
DISCOVERED_NOTE = "Not a tool sessiontools reads: its files in {folder}."
#: The line at the top of the Settings page.
SETTINGS_INTRO = ("<p>Read-only. Each tool's template and host layers: the value of each key, "
                  "the file that set it, and the file that locks it. A tool with an installed "
                  "schema is laid out by that schema.</p>")


def _load_schema(path: Path | None) -> tuple[Any, str | None]:
    """(the parsed schema at PATH, None), (None, None) when there is none,
    or (None, why) when it cannot be read or parsed."""
    if path is None:
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def _unreadable_section(tool: str, error: Exception, note: str = "") -> str:
    line = f"<p>{html.escape(note)}</p>\n" if note else ""
    return (f'<section id="tool-{html.escape(tool)}">\n<h2>{html.escape(tool)}</h2>\n{line}'
            f"<p>unreadable: {html.escape(f'{type(error).__name__}: {error}')}</p>\n</section>")


def _section(tool: str, srcs: list[settings.Source], schema_path: Path | None,
             note: str = "") -> str:
    values, origins, locked, _leaves = settings.merge(srcs)
    schema, why = _load_schema(schema_path)
    return settings_page.draw_tool(tool, values, origins, locked, schema=schema,
                                   schema_file=str(schema_path or ""), schema_error=why,
                                   note=note)


def settings_sections(env: dict[str, str] | None = None) -> list[str]:
    """The Settings page's sections: one per tool in `settings.PACKAGES`,
    then one per discovered tool, then a list of what discovery saw and did
    not show. Each tool is drawn on its own, so one that cannot be read
    shows why and the rest still draw. Reads files; writes nothing, and
    imports neither PyYAML nor jsonschema."""
    out = []
    for tool in settings.PACKAGES:
        try:
            srcs = settings.sources(tool, None, env=env)
            out.append(_section(tool, srcs, settings.find_schema(tool, env)[0]))
        except (settings.SettingsError, OSError, ValueError) as e:
            out.append(_unreadable_section(tool, e))
    try:
        found, notes = settings.discovered_tools(env)
    except (settings.SettingsError, OSError, ValueError) as e:
        found, notes = [], [f"other tools were not looked for: {type(e).__name__}: {e}"]
    for d in found:
        note = DISCOVERED_NOTE.format(folder=d.folder)
        try:
            out.append(_section(d.tool, settings.folder_sources(d.tool, d.folder), d.schema,
                                note))
        except (settings.SettingsError, OSError, ValueError) as e:
            out.append(_unreadable_section(d.tool, e, note))
    if notes:
        out.append('<section id="not-shown">\n<h2>Not shown</h2>\n<ul>\n'
                   + "\n".join(f"<li>{html.escape(n)}</li>" for n in notes)
                   + "\n</ul>\n</section>")
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sessionkit-web"
    server: FrontDoor

    def log_message(self, format: str, *args: Any) -> None:
        return  # a request line can carry anything; nothing is logged

    # --- answers ------------------------------------------------------------

    def _send(self, status: int, body: bytes, *, ctype: str = "text/html; charset=utf-8",
              headers: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:  # say so: the client must not send another request on it
            self.send_header("Connection", "close")
        for k, v in PAGE_HEADERS + tuple(headers):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, status: int, title: str, body: str, **kw: Any) -> None:
        self._send(status, _page(title, body), **kw)

    def _secure(self) -> bool:
        """Whether this request came by a name, over https (its cookie gets
        `Secure`). Only asked after `auth.gate` has passed its one Host."""
        return self.server.front.secure_for(self.headers.get("Host"))

    def _json(self, status: int, obj: dict[str, Any], *,
              headers: tuple[tuple[str, str], ...] = ()) -> None:
        self._send(status, json.dumps(obj).encode(), ctype="application/json",
                   headers=headers)

    def _body_json(self) -> dict[str, Any] | None:
        """The request's JSON object (at most MAX_JSON bytes), or None. The
        body is always read off the connection, even when it is not used:
        bytes left unread would be parsed as the next request on it. A
        chunked body (no Content-Length) is not read: the connection closes
        after the answer, so nothing is left on it."""
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_JSON:
            self.close_connection = True
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    def _passkey_name(self) -> str | None:
        """The name this request came by, when a passkey can be used on it:
        the extra is installed and the Host is a configured name (https)."""
        name = self.server.front.name_of(self.headers.get("Host"))
        return name if name and passkey_module() is not None else None

    def _redirect(self, to: str, *, headers: tuple[tuple[str, str], ...] = ()) -> None:
        self._send(303, b"", ctype="text/plain", headers=(("Location", to),) + headers)

    # --- the routes -----------------------------------------------------------

    def _handle(self) -> None:
        srv = self.server
        srv.reload_rooms()
        verdict = auth.gate(srv.store, srv.front, self.command, self.path, self.headers)
        if verdict.status == 403:
            return self._html(403, "Forbidden", f"<p>{html.escape(verdict.reason)}</p>")
        if verdict.status == 401:
            if self.command in ("GET", "HEAD"):
                return self._redirect("/login")
            return self._html(401, "Log in first", LOGIN_FORM)
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/login":
            return self._login()
        if path == PASSKEY_JS_PATH and self.command in ("GET", "HEAD"):
            if passkey_module() is None:
                return self._html(404, "Not found", "")
            return self._send(200, passkey_script(), ctype="text/javascript; charset=utf-8")
        if path in ("/login/passkey/begin", "/login/passkey/finish"):
            return self._passkey_login(path.rsplit("/", 1)[1])
        if auth.is_open(path):  # any other /login/<asset>: none
            return self._html(404, "Not found", "")
        if path in ("/passkey/enrol/begin", "/passkey/enrol/finish"):
            return self._passkey_enrol(path.rsplit("/", 1)[1], verdict.token)
        if path == "/logout":
            if self.command != "POST":
                return self._html(405, "Method not allowed", LOGOUT_FORM,
                                  headers=(("Allow", "POST"),))
            if verdict.token:
                srv.store.revoke(verdict.token)
            return self._redirect("/login", headers=(
                ("Set-Cookie", auth.clear_cookie(secure=self._secure())),))
        name = path.split("/")[1]
        if path in ("/", "/settings", "/settings/"):
            if self.command not in ("GET", "HEAD"):
                return self._html(405, "Method not allowed", "<p>Read-only.</p>",
                                  headers=(("Allow", "GET, HEAD"),))
            if path == "/":
                return self._menu(verdict.token)
            if path == "/settings":
                return self._redirect("/settings/")
            return self._settings()
        return self._room(name, path)

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle

    def _login_page(self) -> str:
        """The login form, and the passkey button when this name has one.
        A passkeys file that is refused never takes the code form with it:
        the pairing code is the way back in."""
        name = self._passkey_name()
        try:
            kept = bool(name and self.server.store.passkeys(name))
        except auth.AuthError:
            kept = False
        return LOGIN_FORM + "\n" + PASSKEY_SIGNIN if kept else LOGIN_FORM

    def _login(self) -> None:
        srv = self.server
        if self.command in ("GET", "HEAD"):
            return self._html(200, "Log in", self._login_page())
        if self.command != "POST":
            return self._html(405, "Method not allowed", LOGIN_FORM,
                              headers=(("Allow", "GET, HEAD, POST"),))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_FORM:
            self.close_connection = True
            return self._html(400, "Bad request", LOGIN_FORM)
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        code = (form.get("code") or [""])[0]
        try:
            token = srv.store.login(code)
        except auth.LoginRefused as e:
            return self._html(403, "Log in", f"<p>{html.escape(str(e))}</p>\n{LOGIN_FORM}")
        return self._redirect("/", headers=(
            ("Set-Cookie", auth.session_cookie(token, secure=self._secure())),))

    def _passkey_login(self, step: str) -> None:
        """POST /login/passkey/begin, then /finish: a passkey opens a session.
        Open paths (the gate has checked Host and Origin); a 404 where no
        passkey can be used: no extra, not a name, or none for this name."""
        srv = self.server
        if self.command != "POST":
            return self._html(405, "Method not allowed", "", headers=(("Allow", "POST"),))
        msg = self._body_json()  # read first, whatever the answer: see _body_json
        name = self._passkey_name()
        try:
            creds = srv.store.passkeys(name) if name else []
        except auth.AuthError as e:
            return self._json(503, {"error": str(e)})
        if not creds:
            return self._json(404, {"error": "no passkey here: log in with a pairing code"})
        if step == "begin":
            chal = srv.challenges.issue("login")
            return self._json(200, {"challenge": _b64u(chal), "rpId": name,
                                    "allow": [c["id"] for c in creds]})
        if msg is None:
            return self._json(400, {"error": "not a JSON object"})
        chal = srv.challenges.take(msg.get("challenge"), "login")
        why = ("the sign-in took too long, or was already used: try again" if chal is None
               else passkey_module().login(msg, chal, self.headers.get("Origin", ""), creds))
        if why:
            return self._json(403, {"error": why})
        try:
            token = srv.store.passkey_login(msg["id"], name)
        except auth.AuthError as e:
            return self._json(403, {"error": str(e)})
        return self._json(200, {"to": "/"}, headers=(
            ("Set-Cookie", auth.session_cookie(token, secure=self._secure())),))

    def _passkey_enrol(self, step: str, token: str | None) -> None:
        """POST /passkey/enrol/begin, then /finish: add a passkey for this
        name. Only a session a pairing code opened just now may (`may_enrol`)."""
        srv = self.server
        if self.command != "POST":
            return self._html(405, "Method not allowed", "", headers=(("Allow", "POST"),))
        msg = self._body_json()  # read first, whatever the answer: see _body_json
        name = self._passkey_name()
        if not name:
            return self._json(404, {"error": "a passkey needs the extra and an https name"})
        if not srv.store.may_enrol(token):
            return self._json(403, {"error": "add a passkey within 10 minutes of a pairing-code "
                                             "login: run `session web pair` and log in again"})
        bound = auth.session_key(token or "")
        if step == "begin":
            chal = srv.challenges.issue("enrol", bound)
            return self._json(200, {
                "challenge": _b64u(chal), "rpId": name, "rpName": RP_NAME,
                "user": {"id": _b64u(os.urandom(16)), "name": srv.user,
                         "displayName": f"{srv.user} ({RP_NAME})"},
                "exclude": [c["id"] for c in srv.store.passkeys(name)]})
        if msg is None:
            return self._json(400, {"error": "not a JSON object"})
        chal = srv.challenges.take(msg.get("challenge"), "enrol", bound)
        if chal is None:
            return self._json(403, {"error": "that took too long, or was already used: try "
                                             "again"})
        cred, why = passkey_module().register(msg, chal, self.headers.get("Origin", ""))
        if cred is None:
            return self._json(403, {"error": why})
        try:
            return self._json(200, {"added": srv.store.add_passkey(cred)})
        except auth.AuthError as e:
            return self._json(503, {"error": str(e)})

    def _menu(self, token: str | None) -> None:
        srv = self.server
        found, sup = srv.rooms_now()
        items = ['<li><a href="/settings/">Settings</a></li>']
        order = [e.name for e in rooms.CATALOG] + sorted(set(found.rooms) - {
            e.name for e in rooms.CATALOG})
        absent = {e.name: e for e in found.absent}
        for name in order:
            if name in found.rooms:
                room = found.rooms[name]
                t = html.escape(room.title)
                if sup is not None and sup.state(name) == "up":
                    items.append(f'<li><a href="/{html.escape(name)}/">{t}</a></li>')
                else:
                    log = html.escape(str(sup.info(name)["log"])) if sup else ""
                    items.append(f"<li>{t}: not running, restarting "
                                 f"(log: <code>{log}</code>)</li>")
            elif name in absent:
                e = absent[name]
                items.append(f"<li>{html.escape(e.title)}: {html.escape(e.hint)}</li>")
        enrol = self._passkey_name() and srv.store.may_enrol(token)
        self._html(200, "Session Kit", "<ul>\n" + "\n".join(items) + "\n</ul>\n"
                   + (PASSKEY_ENROL + "\n" if enrol else "") + LOGOUT_FORM)

    def _settings(self) -> None:
        parts = [SETTINGS_INTRO, *settings_sections(self.server.env), LOGOUT_FORM]
        self._html(200, "Settings", "\n".join(parts))

    def _room(self, name: str, path: str) -> None:
        srv = self.server
        found, sup = srv.rooms_now()
        if name in found.rooms:
            if path == f"/{name}":
                return self._redirect(f"/{name}/")
            room = found.rooms[name]
            if sup is None or sup.state(name) != "up":
                log = sup.info(name)["log"] if sup else ""
                return self._html(503, room.title, f"<p>{html.escape(room.title)}: not running, "
                                  f"restarting (log: <code>{html.escape(str(log))}</code>)</p>")
            proxy.forward(self, sup.socket(name), prefix=f"/{name}", user=srv.user)
            return None
        for e in found.absent:
            if e.name == name:
                return self._html(404, e.title, f"<p>{html.escape(e.hint)}</p>")
        return self._html(404, "Not found", "")


class FrontDoor(ThreadingHTTPServer):
    """The front door on ADDRESS (already through `check_bind`). The rooms
    are read from WEB_DIR's `rooms.json` and supervised from WEB_DIR's
    `log/` and RUN_DIR (WEB_DIR's `run/` unless given). SUPERVISOR_KW
    shortens its delays in tests."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, store: auth.Store,
                 names: tuple[str, ...] = (),
                 env: dict[str, str] | None = None, user: str | None = None,
                 run_dir: Path | None = None,
                 supervisor_kw: dict[str, Any] | None = None) -> None:
        host = check_bind(address[0])  # again, so no caller can skip it
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        super().__init__((host, address[1]), Handler)
        self.store = store
        self.front = auth.Front(port=self.server_address[1], bound=host, names=names)
        self.env = env
        self.user = user or getpass.getuser()
        self.run_dir = Path(run_dir) if run_dir is not None else store.directory / "run"
        self.challenges = auth.Challenges()
        self.supervisor_kw = supervisor_kw or {}
        self._rooms_lock = threading.Lock()
        self._rooms_id: tuple | None = ("not read yet",)
        self._found = rooms.Discovery(absent=list(rooms.CATALOG))
        self._sup: Supervisor | None = None
        self.reload_rooms()

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # no getfqdn: no DNS at start
        self.server_name, self.server_port = self.server_address[:2]

    def rooms_now(self) -> tuple[rooms.Discovery, Supervisor | None]:
        with self._rooms_lock:
            return self._found, self._sup

    def reload_rooms(self) -> None:
        """Re-read `rooms.json` if it changed since the last read, and run a
        new supervisor on the rooms it names. A room set whose sockets are
        refused (a path over the limit, a run dir that is not private) runs
        no supervisor: one line says why on stderr, and the menu shows each
        room not running. `serve` refuses such a set at start."""
        path = self.store.directory / rooms.ROOMS_FILE
        try:
            st = os.stat(path)
            ident: tuple | None = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            ident = None
        with self._rooms_lock:
            if ident == self._rooms_id:
                return
            found = rooms.load(self.store.directory) if ident else None
            found = found or rooms.Discovery(absent=list(rooms.CATALOG))
            old, self._sup = self._sup, None
            if old is not None:
                old.stop()
            if found.rooms:
                sup = None
                try:
                    sup = Supervisor(found.rooms.values(), self.run_dir,
                                     self.store.directory / "log", **self.supervisor_kw)
                    sup.start()
                except unixhttp.SocketRefused as e:
                    print(f"session web: the rooms were not started: {e}", file=sys.stderr,
                          flush=True)
                    sup = None
                self._sup = sup
            self._found, self._rooms_id = found, ident

    def server_close(self) -> None:
        with self._rooms_lock:
            if self._sup is not None:
                self._sup.stop()
                self._sup = None
        super().server_close()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def check_sockets(run_dir: Path, names: Iterable[str]) -> None:
    """Each room's socket path, `RUN_DIR/<name>.sock`, measured BEFORE any
    socket: one over the platform's limit (`unixhttp.SUN_PATH_LIMIT`) is
    SocketRefused, in one line naming its length, the limit and the fix."""
    for name in names:
        sock = Path(run_dir) / f"{name}.sock"
        n = unixhttp.path_bytes(sock)
        if n > unixhttp.SUN_PATH_LIMIT:
            raise unixhttp.SocketRefused(
                f"room {name!r}: its socket path {sock} is {n} bytes, over the "
                f"{unixhttp.SUN_PATH_LIMIT}-byte limit for a unix socket path here; set "
                f"{settings.WEB_RUN_DIR_ENV} to a shorter directory of yours (it is made "
                "0700) and run again")


def serve(host: str, port: int, *, names: tuple[str, ...] = (),
          env: dict[str, str] | None = None) -> int:
    """`session web serve`: check the address and NAMES, find the rooms,
    check their socket paths and run dir, and serve until SIGINT or SIGTERM.
    Every check comes before any socket; a refused one is raised
    (BindRefused, SocketRefused, ValueError for a name) for the CLI to print
    in one line."""
    bound = check_bind(host)
    named = auth.Front(port=port, bound=bound, names=names).names  # a bad name: before any socket
    store = auth.Store(settings.web_dir(env))
    store.ensure()
    found = rooms.discover()
    rooms.save(store.directory, found)
    run = settings.web_run_dir(env)
    if found.rooms:
        check_sockets(run, found.rooms)
        unixhttp.private_dir(run)
    srv = FrontDoor((bound, port), store=store, names=names, env=env, run_dir=run)

    def stop(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    shown = f"[{bound}]" if ":" in bound else bound
    print(f"session web: the front door is at http://{shown}:{srv.front.port}/ "
          "(get a code with `session web pair`)", flush=True)
    for name in named:
        print(f"session web: and by the name {name}, over https through a TLS proxy "
              f"(the proxy forwards to http://{shown}:{srv.front.port})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    print("session web: stopped", file=sys.stderr)
    return 0
