"""`sessiontools.web.server` and `session web serve` (kit-web-shell design §3,
§5 and §7), end to end and in process: the front door on 127.0.0.1 port 0,
under a temp HOME, with a fake Talk room (a child on a unix socket) behind
it. Every server is shut down and every room stopped in the test; no
0.0.0.0, no ::."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest
from web_fakes import (
    alive,
    referrer_policy,
    request_origin,
    short_dir,
    wait_for,
    write_fake_room,
)

from sessiontools import cli, settings
from sessiontools.web import auth, rooms, server
from sessiontools.web.bind import BindRefused
from sessiontools.web.rooms import Discovery, Room

FAST = {"backoff_first": 0.05, "backoff_max": 0.2, "grace": 0.5, "poll": 0.02}
TALK_ENTRY = {e.name: e for e in rooms.CATALOG}["talk"]


@pytest.fixture
def home(monkeypatch) -> Iterator[Path]:
    """A HOME short enough for the room sockets under it, with one host
    setting for Settings to show."""
    with short_dir("st-home-") as h:
        monkeypatch.setenv("HOME", str(h))
        host = settings.package_dir("tools") / "session.json"
        host.parent.mkdir(parents=True)
        host.write_text(json.dumps({"model": {"default": "opus"}}))
        yield h


def _talk(program: Path, mode: str = "serve") -> Room:
    return Room(name="talk", title="Talk", package="sessionvoice",
                command=(sys.executable, str(program), mode, "{socket}"))


def _set_rooms(store: auth.Store, *found: Room) -> None:
    """What `session web rescan` records: FOUND, and the catalog's other rooms absent."""
    names = {r.name for r in found}
    rooms.save(store.directory, Discovery(rooms={r.name: r for r in found},
                                          absent=[e for e in rooms.CATALOG if e.name not in names]))


@pytest.fixture
def door(home) -> Iterator[dict]:
    store = auth.Store(settings.web_dir())
    store.ensure()
    _set_rooms(store)
    srv = server.FrontDoor(("127.0.0.1", 0), store=store, supervisor_kw=FAST)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    pids: set[int] = set()
    try:
        yield {"srv": srv, "store": store, "port": srv.front.port, "home": home, "pids": pids,
               "program": write_fake_room(home)}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
    assert wait_for(lambda: not any(alive(p) for p in pids), timeout=5), "a room outlived stop"


def _req(d: dict, method: str, path: str, *, cookie: str | None = None,
         origin: bool = True, body: bytes | None = None, ctype: str | None = None):
    c = http.client.HTTPConnection("127.0.0.1", d["port"], timeout=10)
    headers = {}
    if cookie:
        headers["Cookie"] = f"kit_session={cookie}"
    if origin and method not in ("GET", "HEAD"):
        headers["Origin"] = f"http://127.0.0.1:{d['port']}"
    if ctype:
        headers["Content-Type"] = ctype
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data.decode("utf-8", "replace")


def _login(d: dict) -> str:
    code = d["store"].pair().code
    r, _ = _req(d, "POST", "/login", body=f"code={code}".encode(),
                ctype="application/x-www-form-urlencoded")
    assert r.status == 303 and r.getheader("Location") == "/"
    cookie = r.getheader("Set-Cookie")
    return cookie.split(";", 1)[0].split("=", 1)[1]


def _up(d: dict) -> None:
    def ready():
        _f, sup = d["srv"].rooms_now()
        return sup is not None and sup.state("talk") == "up"
    _req(d, "GET", "/login")  # any request re-reads rooms.json
    assert wait_for(ready)
    d["pids"].add(d["srv"].rooms_now()[1].info("talk")["pid"])


# --- the wall ---------------------------------------------------------------------------

def test_the_wall_nothing_but_the_login_without_a_cookie(door):
    _set_rooms(door["store"], _talk(door["program"]))
    _up(door)
    for path in ("/", "/settings/", "/talk/", "/talk/x", "/nope"):
        r, body = _req(door, "GET", path)
        assert r.status == 303 and r.getheader("Location") == "/login", path
        assert "Talk" not in body and "sessionvoice" not in body
    r, _ = _req(door, "POST", "/settings/")
    assert r.status == 401
    r, body = _req(door, "GET", "/login")
    assert r.status == 200 and 'name="code"' in body


def test_a_wrong_host_is_refused_even_for_the_login(door):
    c = http.client.HTTPConnection("127.0.0.1", door["port"], timeout=10)
    c.request("GET", "/login", headers={"Host": f"localhost:{door['port']}"})
    r = c.getresponse()
    r.read()
    c.close()
    assert r.status == 403


# --- pair and login ---------------------------------------------------------------------

def test_pair_then_in_and_the_code_works_once(door):
    code = door["store"].pair().code
    form = f"code={code}".encode()
    r, _ = _req(door, "POST", "/login", body=form, origin=False,
                ctype="application/x-www-form-urlencoded")
    assert r.status == 403  # no Origin: refused, and the code is not spent
    r, _ = _req(door, "POST", "/login", body=form, ctype="application/x-www-form-urlencoded")
    assert r.status == 303
    cookie = r.getheader("Set-Cookie")
    assert cookie.endswith("; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
    assert "Domain" not in cookie and "Secure" not in cookie
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    r, body = _req(door, "GET", "/settings/", cookie=token)
    assert r.status == 200
    r, body = _req(door, "POST", "/login", body=form, ctype="application/x-www-form-urlencoded")
    assert r.status == 403 and "no pairing code is pending" in body


def test_logout_ends_this_browsers_session(door):
    token = _login(door)
    r, _ = _req(door, "POST", "/logout", cookie=token)
    assert r.status == 303 and r.getheader("Set-Cookie").startswith("kit_session=;")
    r, _ = _req(door, "GET", "/", cookie=token)
    assert r.status == 303 and r.getheader("Location") == "/login"


# --- a name: the same front door over https, behind a TLS proxy --------------------------

NAME = "mac.tail1234.example.net"


@pytest.fixture
def named(home) -> Iterator[dict]:
    """A front door that also answers NAME: what a TLS proxy forwards
    is the browser's own Host and an https Origin, over plain http."""
    store = auth.Store(settings.web_dir())
    store.ensure()
    _set_rooms(store)
    srv = server.FrontDoor(("127.0.0.1", 0), store=store, names=(NAME,), supervisor_kw=FAST)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    try:
        yield {"srv": srv, "store": store, "port": srv.front.port}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def _as(d: dict, method: str, path: str, host: str, origin: str | None, *,
        cookie: str | None = None, body: bytes | None = None):
    """A request whose Host and Origin are HOST and ORIGIN, as a proxy hands them on."""
    c = http.client.HTTPConnection("127.0.0.1", d["port"], timeout=10)
    headers = {"Host": host}
    if origin:
        headers["Origin"] = origin
    if cookie:
        headers["Cookie"] = f"kit_session={cookie}"
    if body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r, data.decode("utf-8", "replace")


def test_a_name_logs_in_over_https_and_its_cookie_is_secure(named):
    code = named["store"].pair().code
    r, _ = _as(named, "POST", "/login", NAME, f"https://{NAME}", body=f"code={code}".encode())
    assert r.status == 303
    cookie = r.getheader("Set-Cookie")
    assert cookie.endswith("; Secure") and "Domain" not in cookie
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    r, _ = _as(named, "GET", "/settings/", NAME, None, cookie=token)
    assert r.status == 200
    r, _ = _as(named, "POST", "/logout", NAME, f"https://{NAME}", cookie=token)
    assert r.status == 303 and r.getheader("Set-Cookie").endswith("; Secure")


def test_the_address_still_logs_in_over_http_beside_a_name(named):
    code = named["store"].pair().code
    host = f"127.0.0.1:{named['port']}"
    r, _ = _as(named, "POST", "/login", host, f"http://{host}", body=f"code={code}".encode())
    assert r.status == 303 and "Secure" not in r.getheader("Set-Cookie")


@pytest.mark.parametrize("host,origin", [
    (NAME, f"http://{NAME}"),                 # a name is https, never http
    (NAME, "https://127.0.0.1"),              # Origin and Host disagree
    ("127.0.0.1:{port}", "https://127.0.0.1:{port}"),  # the address is http, never https
    ("127.0.0.1:{port}", f"https://{NAME}"),
    (f"other.{NAME}", f"https://other.{NAME}"),  # a name that was not given
])
def test_each_host_keeps_its_own_scheme(named, host, origin):
    code = named["store"].pair().code
    host, origin = host.format(port=named["port"]), origin.format(port=named["port"])
    r, _ = _as(named, "POST", "/login", host, origin, body=f"code={code}".encode())
    assert r.status == 403
    assert named["store"]._pending() is not None  # refused at the gate: the code is not spent


# --- the browser's Origin: what the page's Referrer-Policy makes of it ----------------------

class Browser:
    """A browser as far as Origin goes: it loads a page, keeps that page's
    referrer policy (from its Referrer-Policy header, as a document does),
    and submits the page's form with the Origin the Fetch spec derives from
    that policy, never one the test picks."""

    def __init__(self, d: dict) -> None:
        self.d = d
        self.origin = f"http://127.0.0.1:{d['port']}"
        self.cookie: str | None = None
        self.policy: str | None = None

    def _send(self, method: str, path: str, *, origin: str | None = None,
              body: bytes | None = None):
        c = http.client.HTTPConnection("127.0.0.1", self.d["port"], timeout=10)
        headers = {}
        if self.cookie:
            headers["Cookie"] = f"kit_session={self.cookie}"
        if origin is not None:
            headers["Origin"] = origin
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read().decode("utf-8", "replace")
        c.close()
        set_cookie = r.getheader("Set-Cookie")
        if set_cookie:
            value = set_cookie.split(";", 1)[0].split("=", 1)[1]
            self.cookie = value or None
        return r, data

    def load(self, path: str):
        r, body = self._send("GET", path)
        assert r.status == 200, (path, r.status)
        # The header is the page's whole policy: no <meta name=referrer> overrides it.
        assert 'name="referrer"' not in body.lower()
        self.policy = referrer_policy(r.getheader("Referrer-Policy"))
        return r, body

    def submit(self, action: str, form: str = ""):
        """POST the loaded page's form to ACTION (same origin, as every
        front-door form is)."""
        assert self.policy is not None, "load a page first"
        origin = request_origin(self.policy, self.origin, self.origin, "POST")
        return self._send("POST", action, origin=origin, body=form.encode())


def test_the_browser_model_follows_the_fetch_spec():
    a, b = "http://127.0.0.1:8750", "http://127.0.0.1:9999"
    s = "https://kit.example.net"
    assert referrer_policy(None) == "strict-origin-when-cross-origin"
    assert referrer_policy("no-referrer") == "no-referrer"
    assert referrer_policy("no-referrer, bogus") == "no-referrer"  # unknown tokens skipped
    assert referrer_policy("no-referrer, same-origin") == "same-origin"  # the last one wins
    assert request_origin("no-referrer", a, a, "GET") is None
    assert request_origin("no-referrer", a, a, "POST") == "null"  # the Firefox report
    assert request_origin("same-origin", a, a, "POST") == a
    assert request_origin("same-origin", a, b, "POST") == "null"
    assert request_origin("same-origin", s, s, "POST") == s
    assert request_origin("strict-origin-when-cross-origin", s, a, "POST") == "null"
    assert request_origin("strict-origin-when-cross-origin", a, a, "POST") == a


def test_a_browser_logs_in_from_the_login_page(door):
    b = Browser(door)
    _r, body = b.load("/login")
    assert 'action="/login"' in body
    code = door["store"].pair().code
    r, _ = b.submit("/login", f"code={code}")
    assert r.status == 303 and r.getheader("Location") == "/", r.status
    assert b.cookie and door["store"].check(b.cookie)


def test_a_browser_logs_out_from_the_menu(door):
    b = Browser(door)
    b.cookie = _login(door)
    _r, body = b.load("/")
    assert 'action="/logout"' in body
    token = b.cookie
    r, _ = b.submit("/logout")
    assert r.status == 303 and r.getheader("Location") == "/login", r.status
    assert not door["store"].check(token)


def test_each_page_the_front_door_writes_lets_its_form_carry_the_origin(door):
    token = _login(door)
    pages = [_req(door, "GET", "/login"), _req(door, "GET", "/", cookie=token),
             _req(door, "GET", "/settings/", cookie=token),
             _req(door, "POST", "/settings/"),  # the 401 page, with a login form
             _req(door, "POST", "/login", body=b"code=wrong",
                  ctype="application/x-www-form-urlencoded")]  # the 403 page, with the form
    origin = f"http://127.0.0.1:{door['port']}"
    for r, _body in pages:
        policy = referrer_policy(r.getheader("Referrer-Policy"))
        assert request_origin(policy, origin, origin, "POST") == origin, (r.status, policy)


def test_a_null_origin_is_still_refused(door):
    """`Origin: null` is what an opaque origin sends (a sandboxed frame, a
    data: URL, a no-referrer page): it names no one, so it stays refused."""
    code = door["store"].pair().code
    c = http.client.HTTPConnection("127.0.0.1", door["port"], timeout=10)
    c.request("POST", "/login", body=f"code={code}".encode(),
              headers={"Origin": "null", "Content-Type": "application/x-www-form-urlencoded"})
    r = c.getresponse()
    body = r.read().decode()
    c.close()
    assert r.status == 403 and "origin not allowed" in body
    token = _login(door)  # the code was not spent by the refused POST
    c = http.client.HTTPConnection("127.0.0.1", door["port"], timeout=10)
    c.request("POST", "/logout", headers={"Origin": "null", "Cookie": f"kit_session={token}"})
    r = c.getresponse()
    r.read()
    c.close()
    assert r.status == 403 and door["store"].check(token)


# --- Settings: built in, read-only --------------------------------------------------------

def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_settings_shows_each_tool_with_values_and_origins(door):
    token = _login(door)
    r, body = _req(door, "GET", "/settings/", cookie=token)
    assert r.status == 200
    for tool in settings.PACKAGES:
        assert f"<h2>{tool}</h2>" in body
    assert "model.default" in body and "&quot;opus&quot;" in body
    assert str(settings.package_dir("tools") / "session.json") in body


def test_settings_has_no_write_path(door):
    token = _login(door)
    before = _tree_hash(settings.kit_root())
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r, _ = _req(door, method, "/settings/", cookie=token,
                    body=b'{"model": {"default": "evil"}}', ctype="application/json")
        assert r.status == 405 and r.getheader("Allow") == "GET, HEAD", method
    assert _tree_hash(settings.kit_root()) == before


def test_rendering_settings_imports_neither_yaml_nor_jsonschema():
    probe = ("import sys; from sessiontools.web import server; server.settings_rows(); "
             "print('yaml' in sys.modules, 'jsonschema' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "False False"


def test_rendering_the_drawn_page_imports_neither_yaml_nor_jsonschema(tmp_path):
    # A HOME where the page really draws: a mapped tool with its schema and a
    # discovered one. On an empty HOME no schema is read and the probe would
    # prove nothing about the drawn path.
    tools = tmp_path / ".sessionkit" / settings.PACKAGES["sounds"] / "settings"
    tools.mkdir(parents=True)
    (tools / "sounds.schema.json").write_text(
        (Path(__file__).resolve().parent.parent / "settings" / "sounds.schema.json").read_text())
    (tools / "sounds.json").write_text(json.dumps({"enabled": True}))
    extra = tmp_path / ".sessionkit" / "extra" / "settings"
    extra.mkdir(parents=True)
    (extra / ".alpha.schema.json").write_text(json.dumps(
        {"type": "object", "properties": {"on": {"type": "boolean"}}}))
    (extra / "alpha.json").write_text(json.dumps({"on": True}))
    probe = ("import sys; from sessiontools.web import server; "
             "page = ''.join(server.settings_sections()); "
             "print(page.count('Drawn from'), 'yaml' in sys.modules, 'jsonschema' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                       env={**os.environ, "HOME": str(tmp_path)})
    assert r.stdout.strip() == "2 False False"


# --- Settings drawn from each tool's schema (read-only) -------------------------------------

REPO = Path(__file__).resolve().parent.parent


class _Markup(HTMLParser):
    """The tags, attribute names and forms a browser would see on a page."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: set[str] = set()
        self.forms: list[dict[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs |= {name for name, _ in attrs}
        if tag == "form":
            self.forms.append(dict(attrs))


def _markup(body: str) -> _Markup:
    m = _Markup()
    m.feed(body)
    m.close()
    return m


def _section_of(body: str, tool: str) -> str:
    """TOOL's section of the page: up to the next section with an id (a
    group inside a tool has none)."""
    start = body.index(f'<section id="tool-{tool}">')
    end = body.find('<section id="', start + 1)
    return body[start:end if end >= 0 else len(body)]


def _settings_page(door) -> str:
    r, body = _req(door, "GET", "/settings/", cookie=_login(door))
    assert r.status == 200
    return body


def _extra(home: Path, package: str = "extra") -> Path:
    folder = settings.package_dir(package)
    assert folder.is_relative_to(home)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_s1_a_tool_with_an_installed_schema_is_drawn_from_it(door):
    tools = settings.package_dir("tools")
    (tools / "sounds.schema.json").write_text((REPO / "settings" / "sounds.schema.json").read_text())
    # The template sets busy.trigger and locks it, so the host's own value
    # for it is ignored, as every reader of the cascade ignores it.
    template = tools / "sounds.template.json"
    template.write_text(json.dumps({"busy": {"trigger": "landed"}, "_locked": ["busy.trigger"]}))
    host = tools / "sounds.json"
    host.write_text(json.dumps({"enabled": True, "busy": {"trigger": "batch"}}))
    part = _section_of(_settings_page(door), "sounds")
    assert "Settings of the sounds a seat plays on what its agent is doing." in part
    rows = part.split("<tr>")
    enabled = next(r for r in rows if "<code>enabled</code>" in r)
    trigger = next(r for r in rows if "<code>busy.trigger</code>" in r)
    assert "<td>On</td>" in enabled and str(host) in enabled  # a boolean reads as a switch
    assert "<td>landed</td>" in trigger and "<td>batch</td>" not in trigger  # the lock holds
    assert f"<code>{template}</code><br>locked by <code>{template}</code>" in trigger
    assert f"Drawn from <code>{tools / 'sounds.schema.json'}</code>" in part
    assert "<h3>busy</h3>" in part and "unset" in part  # busy.file and the rest, not set


def test_s1_the_session_tool_keeps_its_flat_table_with_no_schema(door):
    part = _section_of(_settings_page(door), "session")
    assert "Drawn from" not in part and "model.default" in part and "&quot;opus&quot;" in part


def test_s4_a_browser_logs_out_from_the_settings_page(door):
    b = Browser(door)
    b.cookie = _login(door)
    r, body = b.load("/settings/")
    assert 'action="/logout"' in body  # first: submit() would post to any action at all
    assert r.getheader("Referrer-Policy") == "same-origin"
    token = b.cookie
    r, _ = b.submit("/logout")
    assert r.status == 303 and r.getheader("Location") == "/login", r.status
    assert not door["store"].check(token)


def test_s4_a_browser_post_to_settings_passes_the_gate_and_is_refused_by_the_page(door):
    b = Browser(door)
    b.cookie = _login(door)
    b.load("/settings/")
    before = _tree_hash(settings.kit_root())
    r, _ = b.submit("/settings/", "model.default=evil")
    assert r.status == 405 and r.getheader("Allow") == "GET, HEAD", r.status  # not 403
    for origin in (f"http://127.0.0.1:{door['port'] + 1}", "null"):
        r, body = b._send("POST", "/settings/", origin=origin, body=b"model.default=evil")
        assert r.status == 403 and "origin not allowed" in body, origin
    assert _tree_hash(settings.kit_root()) == before


def test_s5_without_a_session_settings_names_no_tool(door):
    schema_path = _extra(door["home"]) / ".alpha.schema.json"
    schema_path.write_text(json.dumps({"type": "object"}))
    r, body = _req(door, "GET", "/settings/")
    assert r.status == 303 and r.getheader("Location") == "/login"
    for tool in (*settings.PACKAGES, "alpha"):
        assert tool not in body


def test_s6_the_page_has_no_script_no_style_and_one_form_logout(door):
    # Drawn tools on the page too, not only flat tables: a mapped one with its
    # schema and a discovered one.
    tools = settings.package_dir("tools")
    (tools / "sounds.schema.json").write_text((REPO / "settings" / "sounds.schema.json").read_text())
    folder = _extra(door["home"])
    (folder / ".alpha.schema.json").write_text(json.dumps(
        {"type": "object", "properties": {"g": {"type": "object", "properties": {
            "on": {"type": "boolean"}}}}}))
    (folder / "alpha.json").write_text(json.dumps({"g": {"on": True}}))
    body = _settings_page(door)
    assert body.count("Drawn from") == 2
    m = _markup(body)
    assert "script" not in m.tags and "style" not in m.tags and "style" not in m.attrs
    assert not any(a.startswith("on") for a in m.attrs)
    assert m.forms == [{"method": "post", "action": "/logout"}]


def test_d1_a_discovered_tool_is_drawn_after_the_mapped_ones_from_its_folder(door):
    folder = _extra(door["home"])
    (folder / ".alpha.schema.json").write_text(json.dumps({
        "type": "object", "title": "Alpha",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"},
                       "c": {"type": "integer"}}}))
    template = folder / "alpha.template.json"
    template.write_text(json.dumps({"a": 1, "b": 1, "c": 1, "_locked": ["c"]}))
    host = folder / "alpha.json"
    host.write_text(json.dumps({"b": 2, "c": 2}))
    (folder / "alpha.d").mkdir()
    drop = folder / "alpha.d" / "10-x.json"
    drop.write_text(json.dumps({"b": 3}))
    body = _settings_page(door)
    at = body.index('<section id="tool-alpha">')
    assert all(body.index(f'<section id="tool-{t}">') < at for t in settings.PACKAGES)
    part = _section_of(body, "alpha")
    assert f"Not a tool sessiontools reads: its files in {folder}." in part
    assert "<h2>Alpha <code>alpha</code></h2>" in part
    rows = part.split("<tr>")
    row = {k: next(r for r in rows if f"<code>{k}</code>" in r) for k in "abc"}
    assert "<td>1</td>" in row["a"] and str(template) in row["a"]
    assert "<td>3</td>" in row["b"] and str(drop) in row["b"]  # the drop-in wins
    assert "<td>1</td>" in row["c"] and f"locked by <code>{template}</code>" in row["c"]


def test_d4_a_mapped_tools_stray_schema_is_listed_once_and_not_drawn_twice(door):
    stray = settings.package_dir("tools") / ".ears.schema.json"
    stray.write_text(json.dumps({"type": "object", "title": "Stray"}))
    body = _settings_page(door)
    assert body.count('<section id="tool-ears">') == 1 and "Stray" not in body
    shown = body[body.index('<section id="not-shown">'):]
    assert str(stray) in shown and "belongs to the voice package" in shown


@pytest.mark.parametrize("bad", ["{not json", "[1, 2]"])
def test_d6_a_bad_discovered_schema_falls_back_to_the_flat_table(door, bad):
    folder = _extra(door["home"])
    (folder / ".alpha.schema.json").write_text(bad)
    (folder / "alpha.json").write_text(json.dumps({"mode": "quiet"}))
    (folder / ".beta.schema.json").write_text(json.dumps({"type": "object", "title": "Beta"}))
    body = _settings_page(door)
    part = _section_of(body, "alpha")
    assert "unreadable schema" in part and "mode" in part and "&quot;quiet&quot;" in part
    assert "<h2>Beta <code>beta</code></h2>" in body


def test_d6_a_discovered_tool_whose_values_cannot_be_read_says_so(door):
    folder = _extra(door["home"])
    (folder / ".alpha.schema.json").write_text(json.dumps({"type": "object"}))
    (folder / "alpha.json").write_text("[1, 2]")
    (folder / ".beta.schema.json").write_text(json.dumps({"type": "object", "title": "Beta"}))
    body = _settings_page(door)
    assert "unreadable: SettingsError" in _section_of(body, "alpha")
    assert "<h2>Beta <code>beta</code></h2>" in body


def test_d10_a_page_with_discovered_tools_writes_nothing(door):
    folder = _extra(door["home"])
    (folder / ".alpha.schema.json").write_text(json.dumps({"type": "object"}))
    (folder / "alpha.json").write_text(json.dumps({"on": True}))
    token = _login(door)
    before = _tree_hash(settings.kit_root())
    r, _ = _req(door, "GET", "/settings/", cookie=token)
    assert r.status == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r, _ = _req(door, method, "/settings/", cookie=token, body=b"{}",
                    ctype="application/json")
        assert r.status == 405, method
    assert _tree_hash(settings.kit_root()) == before


@pytest.mark.parametrize("bad", ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>'])
def test_d11_hostile_text_in_a_discovered_tool_is_escaped(door, bad):
    folder = _extra(door["home"])
    (folder / ".alpha.schema.json").write_text(json.dumps({
        "type": "object", "title": bad, "description": bad,
        "properties": {"k": {"type": "string", "title": bad, "x-hint": bad}}}))
    (folder / "alpha.json").write_text(json.dumps({"k": bad, bad: bad}))
    body = _settings_page(door)
    assert "Drawn from" in _section_of(body, "alpha")  # the drawn path, not a flat table
    m = _markup(body)
    assert "script" not in m.tags and "img" not in m.tags
    assert not any(a.startswith("on") for a in m.attrs)


# --- the menu and the rooms: present, down, absent ---------------------------------------

def test_talk_present_then_absent_after_a_rescan(door):
    token = _login(door)
    _set_rooms(door["store"], _talk(door["program"]))
    _up(door)
    pid = door["srv"].rooms_now()[1].info("talk")["pid"]
    r, body = _req(door, "GET", "/", cookie=token)
    assert r.status == 200 and '<a href="/settings/">Settings</a>' in body
    assert '<a href="/talk/">Talk</a>' in body and "install sessionvoice" not in body
    assert body.index("Settings") < body.index("Talk")
    r, body = _req(door, "GET", "/talk/hello?x=1", cookie=token)
    assert r.status == 200 and json.loads(body) == {"room": "talk", "path": "/hello?x=1",
                                                    "pid": pid}
    r, _ = _req(door, "GET", "/talk", cookie=token)
    assert r.status == 303 and r.getheader("Location") == "/talk/"

    _set_rooms(door["store"])  # the room's package removed, then `session web rescan`
    r, body = _req(door, "GET", "/", cookie=token)
    assert "Talk: install sessionvoice to get Talk" in body and 'href="/talk/"' not in body
    r, body = _req(door, "GET", "/talk/", cookie=token)
    assert r.status == 404 and "install sessionvoice to get Talk" in body
    assert wait_for(lambda: not alive(pid))  # its supervisor stopped it


def test_talk_absent_from_the_start_shows_the_hint(door):
    token = _login(door)
    r, body = _req(door, "GET", "/", cookie=token)
    assert r.status == 200 and "Talk: install sessionvoice to get Talk" in body
    r, body = _req(door, "GET", "/talk/x", cookie=token)
    assert r.status == 404 and TALK_ENTRY.hint in body


def test_a_crashed_room_shows_not_running_and_its_log(door):
    token = _login(door)
    _set_rooms(door["store"], _talk(door["program"], "crash"))
    _req(door, "GET", "/login")
    _f, sup = door["srv"].rooms_now()
    assert wait_for(lambda: sup.info("talk")["starts"] >= 2)
    r, body = _req(door, "GET", "/", cookie=token)
    assert "Talk: not running, restarting" in body and "talk.log" in body
    r, body = _req(door, "GET", "/talk/", cookie=token)
    assert r.status == 503 and "not running" in body


def test_an_unknown_path_is_404_behind_the_login(door):
    token = _login(door)
    r, _ = _req(door, "GET", "/nope/", cookie=token)
    assert r.status == 404


# --- session web serve: the bind check comes first ---------------------------------------

@pytest.fixture
def no_sockets(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a socket was opened")
    monkeypatch.setattr(socket, "socket", refuse)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "localhost", "192.168.1.10"])
def test_serve_refuses_an_address_before_any_socket(host, no_sockets, capsys):
    rc = cli.main(["web", "serve", "--host", host])
    assert rc == 2
    assert repr(host) in capsys.readouterr().err


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_the_front_door_itself_refuses_an_any_address(host, no_sockets, tmp_path):
    with pytest.raises(BindRefused):
        server.FrontDoor((host, 0), store=auth.Store(tmp_path / "web"))


def test_serve_defaults_to_loopback(monkeypatch):
    got = {}
    monkeypatch.delenv("SESSION_WEB_NAMES", raising=False)
    monkeypatch.setattr(server, "serve", lambda host, port, names=(), env=None: got.update(
        host=host, port=port, names=names) or 0)
    assert cli.main(["web", "serve"]) == 0
    assert got == {"host": "127.0.0.1", "port": server.DEFAULT_PORT, "names": ()}


def test_serve_takes_names_from_the_flag_and_the_environment(monkeypatch):
    got = {}
    monkeypatch.setattr(server, "serve", lambda host, port, names=(), env=None: got.update(
        host=host, names=names) or 0)
    monkeypatch.setenv("SESSION_WEB_NAMES", " b.tail1234.example.net,c.example  ")
    assert cli.main(["web", "serve", "--name", "a.tail1234.example.net"]) == 0
    assert got == {"host": "127.0.0.1",
                   "names": ("a.tail1234.example.net", "b.tail1234.example.net", "c.example")}


@pytest.mark.parametrize("argv,env", [
    (["--name", "localhost"], {}),
    (["--name", "127.0.0.1"], {}),
    (["--name", "bad name"], {}),
    ([], {"SESSION_WEB_NAMES": "ok.example, x.localhost"}),
])
def test_serve_refuses_a_bad_name_before_any_socket(argv, env, no_sockets, capsys,
                                                    monkeypatch):
    monkeypatch.delenv("SESSION_WEB_NAMES", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["web", "serve", *argv]) == 2
    assert "--name or SESSION_WEB_NAMES" in capsys.readouterr().err


#: Ports other kit services already hold on a host: voicemode's `serve`
#: (8765) and the Settings app (8770). The front door must not default onto one.
KIT_PORTS = {8765, 8770}


def test_the_default_port_is_no_other_kit_service_port():
    assert server.DEFAULT_PORT not in KIT_PORTS
    assert 1024 < server.DEFAULT_PORT < 65536


@pytest.mark.parametrize("port", ["0", "65536"])
def test_serve_refuses_a_port_out_of_range(port, no_sockets):
    assert cli.main(["web", "serve", "--port", port]) == 2


def test_serve_says_each_name_it_answers(home, monkeypatch, capsys):
    class Door:
        def __init__(self, address, **kw):
            self.front = auth.Front(port=address[1], names=kw["names"])

        def serve_forever(self):
            return

        def server_close(self):
            return

    monkeypatch.setattr(rooms, "discover", lambda: Discovery(absent=list(rooms.CATALOG)))
    monkeypatch.setattr(server, "FrontDoor", Door)
    monkeypatch.setattr(server.signal, "signal", lambda *a: None)
    assert server.serve("127.0.0.1", 8750, names=("Mac.Tail1234.example.net.",)) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8750/" in out
    assert "by the name mac.tail1234.example.net, over https" in out
