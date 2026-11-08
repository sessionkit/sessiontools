"""The front door's passkey (PROPOSED, door-passkey): `session web serve`
with the `passkey` extra, end to end over HTTP, with a software
authenticator (a P-256 or RSA key from `cryptography` signing what a
browser's would). The door runs on 127.0.0.1 port 0 under a temp HOME and
also answers NAME, as it does behind a TLS proxy: the browser's Host
and an https Origin, over plain http.

The rules under test: a passkey is added only by a session a pairing code
opened under 10 minutes ago, by a name, with the extra; a passkey opens
the same session cookie a code does, but one that cannot add a passkey;
the bare address, or a door without the extra, is the pairing code alone."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from web_fakes import short_dir

from sessiontools import cli, settings
from sessiontools.web import auth, passkey, rooms, server
from sessiontools.web.rooms import Discovery

NAME = "mac.tail1234.example.net"
ORIGIN = f"https://{NAME}"
FAST = {"backoff_first": 0.05, "backoff_max": 0.2, "grace": 0.5, "poll": 0.02}


# --- a software authenticator -------------------------------------------------------------

class Authenticator:
    """One passkey on one device: what `navigator.credentials` would hand
    the page, made with a real key, for RP_ID."""

    def __init__(self, rp_id: str = NAME, alg: int = passkey.ES256) -> None:
        self.rp_id, self.alg = rp_id, alg
        self.key = (ec.generate_private_key(ec.SECP256R1()) if alg == passkey.ES256
                    else rsa.generate_private_key(public_exponent=65537, key_size=2048))
        self.id = passkey.b64u(os.urandom(16))

    def auth_data(self, flags: int = passkey.UP | passkey.UV, rp_id: str | None = None) -> bytes:
        return (hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([flags])
                + (0).to_bytes(4, "big"))

    @staticmethod
    def client_data(kind: str, challenge: str, origin: str, **more) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin,
                           "crossOrigin": False, **more}).encode()

    def spki(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)

    def create(self, offer: dict, origin: str = ORIGIN, **over) -> dict:
        cdj = over.pop("cdj", None) or self.client_data("webauthn.create", offer["challenge"],
                                                        origin)
        msg = {"challenge": offer["challenge"], "id": self.id, "name": "iPhone",
               "clientDataJSON": passkey.b64u(cdj),
               "authenticatorData": passkey.b64u(over.pop("ad", None) or self.auth_data()),
               "publicKey": passkey.b64u(self.spki()), "alg": self.alg}
        msg.update(over)
        return msg

    def sign(self, data: bytes) -> bytes:
        if self.alg == passkey.ES256:
            return self.key.sign(data, ec.ECDSA(hashes.SHA256()))
        return self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def get(self, offer: dict, origin: str = ORIGIN, **over) -> dict:
        cdj = over.pop("cdj", None) or self.client_data("webauthn.get", offer["challenge"], origin)
        ad = over.pop("ad", None) or self.auth_data()
        msg = {"challenge": offer["challenge"], "id": self.id,
               "clientDataJSON": passkey.b64u(cdj), "authenticatorData": passkey.b64u(ad),
               "signature": passkey.b64u(self.sign(ad + hashlib.sha256(cdj).digest()))}
        msg.update(over)
        return msg


# --- the door ----------------------------------------------------------------------------

@pytest.fixture
def door(monkeypatch) -> Iterator[dict]:
    with short_dir("st-pk-") as home:
        monkeypatch.setenv("HOME", str(home))
        store = auth.Store(settings.web_dir())
        store.ensure()
        rooms.save(store.directory, Discovery(absent=list(rooms.CATALOG)))
        srv = server.FrontDoor(("127.0.0.1", 0), store=store, names=(NAME,), user="alice",
                               supervisor_kw=FAST)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02},
                             daemon=True)
        t.start()
        try:
            yield {"srv": srv, "store": store, "port": srv.front.port,
                   "addr": f"127.0.0.1:{srv.front.port}"}
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(5)


def req(d: dict, method: str, path: str, *, host: str = NAME, origin: str | None = None,
        cookie: str | None = None, body: dict | bytes | None = None):
    """A request as a TLS proxy hands it on: HOST, and for a POST the
    page's ORIGIN (https for the name, http for the address)."""
    c = http.client.HTTPConnection("127.0.0.1", d["port"], timeout=10)
    headers = {"Host": host}
    if method == "POST":
        headers["Origin"] = origin or (f"https://{host}" if host == NAME else f"http://{host}")
    if cookie:
        headers["Cookie"] = f"kit_session={cookie}"
    data = None
    if isinstance(body, dict):
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif body is not None:
        data = body
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    text = raw.decode("utf-8", "replace")
    try:
        obj = json.loads(text) if r.getheader("Content-Type") == "application/json" else None
    except ValueError:
        obj = None
    return r, text, obj


def token_of(r) -> str:
    return r.getheader("Set-Cookie").split(";", 1)[0].split("=", 1)[1]


def code_login(d: dict, host: str = NAME) -> str:
    code = d["store"].pair().code
    r, _, _ = req(d, "POST", "/login", host=host, body=f"code={code}".encode())
    assert r.status == 303
    return token_of(r)


def enrol(d: dict, token: str, device: Authenticator) -> dict:
    r, _, offer = req(d, "POST", "/passkey/enrol/begin", cookie=token, body={})
    assert r.status == 200, offer
    r, _, got = req(d, "POST", "/passkey/enrol/finish", cookie=token, body=device.create(offer))
    assert r.status == 200, got
    return got


def begin_login(d: dict) -> dict:
    r, _, offer = req(d, "POST", "/login/passkey/begin", body={})
    assert r.status == 200, offer
    return offer


# --- the whole path ----------------------------------------------------------------------

@pytest.mark.parametrize("alg", [passkey.ES256, passkey.RS256])
def test_code_then_enrol_then_a_passkey_opens_the_door(door, alg):
    device = Authenticator(alg=alg)
    token = code_login(door)
    r, page, _ = req(door, "GET", "/", cookie=token)
    assert r.status == 200 and 'id="passkey-enrol"' in page
    assert f'<script src="{server.PASSKEY_JS_PATH}" defer></script>' in page
    got = enrol(door, token, device)
    keys = door["store"].passkeys()
    assert [k["rp_id"] for k in keys] == [NAME] and got["added"] == auth.passkey_id(keys[0])
    assert keys[0]["name"] == "iPhone" and keys[0]["used"] == 0
    assert (door["store"].directory / auth.PASSKEYS_FILE).stat().st_mode & 0o777 == 0o600

    req(door, "POST", "/logout", cookie=token)
    r, page, _ = req(door, "GET", "/login")
    assert 'id="passkey-signin"' in page and 'name="code"' in page  # the code stays
    offer = begin_login(door)
    assert offer["rpId"] == NAME and offer["allow"] == [device.id]
    r, _, got = req(door, "POST", "/login/passkey/finish", body=device.get(offer))
    assert r.status == 200 and got == {"to": "/"}
    cookie = r.getheader("Set-Cookie")
    assert cookie.endswith("; Secure") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    new = token_of(r)
    r, page, _ = req(door, "GET", "/settings/", cookie=new)
    assert r.status == 200
    assert door["store"].passkeys()[0]["used"] > 0
    # a passkey's session never adds a passkey
    r, page, _ = req(door, "GET", "/", cookie=new)
    assert 'id="passkey-enrol"' not in page
    r, _, got = req(door, "POST", "/passkey/enrol/begin", cookie=new, body={})
    assert r.status == 403 and "pairing-code" in got["error"]


def test_the_script_is_a_file_the_login_may_load(door):
    r, js, _ = req(door, "GET", server.PASSKEY_JS_PATH)
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/javascript")
    assert "navigator.credentials.get" in js and "navigator.credentials.create" in js
    assert r.getheader("Content-Security-Policy") == "default-src 'self'; frame-ancestors 'none'"


# --- who may add a passkey ----------------------------------------------------------------

def test_no_session_may_not_enrol(door):
    r, _, _ = req(door, "POST", "/passkey/enrol/begin", body={})
    assert r.status == 401


def test_a_session_older_than_the_code_window_may_not_enrol(door, monkeypatch):
    token = code_login(door)
    later = door["store"]._now() + auth.CODE_TTL
    monkeypatch.setattr(door["store"], "clock", lambda: later)
    r, page, _ = req(door, "GET", "/", cookie=token)
    assert r.status == 200 and 'id="passkey-enrol"' not in page
    r, _, got = req(door, "POST", "/passkey/enrol/begin", cookie=token, body={})
    assert r.status == 403 and "session web pair" in got["error"]


def test_a_session_from_before_via_was_kept_may_not_enrol(door):
    token = code_login(door)
    path = door["store"].directory / auth.SESSIONS_FILE
    data = json.loads(path.read_text())
    for row in data["sessions"].values():
        del row["via"]
    path.write_text(json.dumps(data))
    assert door["store"].check(token)  # still a live session
    assert not door["store"].may_enrol(token)


def test_the_address_has_no_passkey(door):
    token = code_login(door, host=door["addr"])
    r, page, _ = req(door, "GET", "/", host=door["addr"], cookie=token)
    assert r.status == 200 and "passkey" not in page
    r, _, _ = req(door, "POST", "/passkey/enrol/begin", host=door["addr"], cookie=token, body={})
    assert r.status == 404


def test_an_enrol_challenge_is_bound_to_its_session(door):
    a, b = code_login(door), code_login(door)
    r, _, offer = req(door, "POST", "/passkey/enrol/begin", cookie=a, body={})
    r, _, _ = req(door, "POST", "/passkey/enrol/finish", cookie=b,
                    body=Authenticator().create(offer))
    assert r.status == 403 and door["store"].passkeys() == []


@pytest.mark.parametrize("bad", ["origin", "rp", "no-uv", "type", "frame", "alg", "key"])
def test_a_bad_registration_keeps_nothing(door, bad):
    device = Authenticator()
    token = code_login(door)
    r, _, offer = req(door, "POST", "/passkey/enrol/begin", cookie=token, body={})
    ch = offer["challenge"]
    over = {
        "origin": {"cdj": device.client_data("webauthn.create", ch, "https://evil.example")},
        "rp": {"ad": device.auth_data(rp_id="evil.example")},
        "no-uv": {"ad": device.auth_data(flags=passkey.UP)},
        "type": {"cdj": device.client_data("webauthn.get", ch, ORIGIN)},
        "frame": {"cdj": device.client_data("webauthn.create", ch, ORIGIN, crossOrigin=True)},
        "alg": {"alg": -8},
        "key": {"publicKey": passkey.b64u(b"not a key")},
    }[bad]
    r, _, got = req(door, "POST", "/passkey/enrol/finish", cookie=token,
                    body=device.create(offer, **over))
    assert r.status == 403 and got["error"]
    assert door["store"].passkeys() == []


# --- a passkey login that must fail -------------------------------------------------------

@pytest.fixture
def kept(door) -> dict:
    device = Authenticator()
    enrol(door, code_login(door), device)
    return {**door, "device": device}


@pytest.mark.parametrize("bad", ["signature", "origin", "rp", "no-uv", "unknown", "type"])
def test_a_bad_assertion_opens_nothing(kept, bad):
    device = kept["device"]
    offer = begin_login(kept)
    ch = offer["challenge"]
    if bad == "signature":
        msg = device.get(offer, signature=passkey.b64u(Authenticator().sign(b"x")))
    elif bad == "origin":
        msg = device.get(offer, cdj=device.client_data("webauthn.get", ch, "https://evil.example"))
    elif bad == "rp":
        msg = device.get(offer, ad=device.auth_data(rp_id="evil.example"))
    elif bad == "no-uv":
        msg = device.get(offer, ad=device.auth_data(flags=passkey.UP))
    elif bad == "unknown":
        msg = Authenticator().get(offer)
    else:
        msg = device.get(offer, cdj=device.client_data("webauthn.create", ch, ORIGIN))
    r, _, got = req(kept, "POST", "/login/passkey/finish", body=msg)
    assert r.status == 403 and got["error"] and r.getheader("Set-Cookie") is None


def test_a_challenge_works_once(kept):
    offer = begin_login(kept)
    msg = kept["device"].get(offer)
    r, _, _ = req(kept, "POST", "/login/passkey/finish", body=msg)
    assert r.status == 200
    r, _, got = req(kept, "POST", "/login/passkey/finish", body=msg)
    assert r.status == 403 and "already used" in got["error"]


def test_an_enrol_challenge_never_signs_in(kept):
    token = code_login(kept)
    r, _, offer = req(kept, "POST", "/passkey/enrol/begin", cookie=token, body={})
    r, _, _ = req(kept, "POST", "/login/passkey/finish", body=kept["device"].get(offer))
    assert r.status == 403


def test_a_passkey_login_needs_the_doors_own_origin(kept):
    offer = begin_login(kept)
    r, _, _ = req(kept, "POST", "/login/passkey/finish", origin=f"http://{NAME}",
                  body=kept["device"].get(offer))
    assert r.status == 403


def test_a_removed_passkey_opens_nothing(kept):
    offer = begin_login(kept)
    msg = kept["device"].get(offer)
    kept["store"].remove_passkeys([auth.passkey_id(kept["store"].passkeys()[0])])
    r, _, got = req(kept, "POST", "/login/passkey/finish", body=msg)
    assert r.status == 404 and "pairing code" in got["error"]


def test_a_passkey_is_for_its_own_name_only(kept, monkeypatch):
    other = "phone.tail1234.example.net"
    monkeypatch.setattr(kept["srv"], "front", auth.Front(port=kept["port"], names=(NAME, other)))
    r, page, _ = req(kept, "GET", "/login", host=other)
    assert r.status == 200 and "passkey-signin" not in page
    r, _, _ = req(kept, "POST", "/login/passkey/begin", host=other, origin=f"https://{other}",
                  body={})
    assert r.status == 404


def test_a_body_that_is_not_a_json_object_is_refused(kept):
    begin_login(kept)
    for body in (b"[1]", b"{", b"", b"x" * (server.MAX_JSON + 1)):
        c = http.client.HTTPConnection("127.0.0.1", kept["port"], timeout=10)
        c.request("POST", "/login/passkey/finish", body=body,
                  headers={"Host": NAME, "Origin": ORIGIN, "Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        c.close()
        assert r.status == 400


def test_one_kept_alive_connection_carries_the_whole_ceremony(kept):
    """A browser's fetch reuses its connection: a body the door left unread
    would be parsed as the next request (found in Chrome: a 501)."""
    c = http.client.HTTPConnection("127.0.0.1", kept["port"], timeout=10)
    h = {"Host": NAME, "Origin": ORIGIN, "Content-Type": "application/json"}

    def post(path, obj):
        c.request("POST", path, body=json.dumps(obj).encode(), headers=h)
        r = c.getresponse()
        return r, json.loads(r.read())

    r, offer = post("/login/passkey/begin", {"any": "thing"})
    assert r.status == 200
    r, got = post("/login/passkey/finish", kept["device"].get(offer))
    assert r.status == 200 and got == {"to": "/"}
    h["Cookie"] = f"kit_session={token_of(r)}"
    r, _ = post("/passkey/enrol/begin", {})
    assert r.status == 403  # a passkey's session: refused, and the body still read
    r, offer = post("/login/passkey/begin", {})
    assert r.status == 200
    c.close()


def test_a_refused_passkeys_file_never_takes_the_code_with_it(kept):
    path = kept["store"].directory / auth.PASSKEYS_FILE
    path.write_text("[not json")
    r, page, _ = req(kept, "GET", "/login")
    assert r.status == 200 and 'name="code"' in page and "passkey-signin" not in page
    r, _, got = req(kept, "POST", "/login/passkey/begin", body={})
    assert r.status == 503 and str(path) in got["error"]
    assert code_login(kept)


def test_get_on_a_ceremony_is_405(kept):
    r, _, _ = req(kept, "GET", "/login/passkey/begin")
    assert r.status == 405 and r.getheader("Allow") == "POST"


# --- without the extra --------------------------------------------------------------------

def test_without_the_extra_the_door_is_the_pairing_code_alone(kept, monkeypatch):
    monkeypatch.setattr(server, "passkey_module", lambda: None)
    r, page, _ = req(kept, "GET", "/login")
    assert r.status == 200 and "passkey" not in page and 'name="code"' in page
    r, _, _ = req(kept, "GET", server.PASSKEY_JS_PATH)
    assert r.status == 404
    r, _, _ = req(kept, "POST", "/login/passkey/begin", body={})
    assert r.status == 404
    token = code_login(kept)
    r, page, _ = req(kept, "GET", "/", cookie=token)
    assert "passkey" not in page


def test_the_door_and_the_command_never_import_cryptography():
    probe = ("import sys; import sessiontools.cli, sessiontools.web.server, "
             "sessiontools.web.auth, sessiontools.web.cli; "
             "print('cryptography' in sys.modules, 'sessiontools.web.passkey' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert out == "False False"


def test_the_extra_is_cryptography_and_nothing_else():
    import tomllib
    project = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml")
                            .read_text())["project"]
    assert project["optional-dependencies"] == {"passkey": ["cryptography>=42"]}


# --- the store and the challenges ---------------------------------------------------------

def test_passkey_ids_remove_by_prefix_and_refuse_an_ambiguous_one(tmp_path):
    store = auth.Store(tmp_path / "web")
    for i in range(17):  # 17 ids over 16 hex digits: two share a first digit
        store.add_passkey({"id": f"cred{i}", "rp_id": NAME, "spki": "x", "alg": -7,
                           "added": 1, "used": 0, "name": ""})
    ids = [auth.passkey_id(k) for k in store.passkeys()]
    shared = next(i[0] for i in ids if sum(j[0] == i[0] for j in ids) > 1)
    with pytest.raises(auth.AuthError, match="no passkey"):
        store.remove_passkeys(["zzzz"])
    with pytest.raises(auth.AuthError, match=r"matches \d+ passkeys"):
        store.remove_passkeys([ids[0], shared])
    with pytest.raises(auth.AuthError, match="no passkey"):
        store.remove_passkeys([""])
    assert len(store.passkeys()) == 17  # a refusal removes nothing, not even ids[0]
    assert store.remove_passkeys([ids[1]]) == [ids[1]]
    assert [auth.passkey_id(k) for k in store.passkeys()] == [ids[0], *ids[2:]]


@pytest.mark.parametrize("data", [{"passkeys": {}}, {"passkeys": [{"id": "x"}]},
                                  {"passkeys": [{"id": "x", "rp_id": NAME, "spki": "s",
                                                 "alg": "-7", "added": 1, "used": 0}]}])
def test_a_passkeys_file_of_another_shape_is_refused(tmp_path, data):
    store = auth.Store(tmp_path / "web")
    store.ensure()
    path = store.directory / auth.PASSKEYS_FILE
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    with pytest.raises(auth.AuthError, match="not a passkeys file"):
        store.passkeys()


def test_write_json_never_replaces_the_passkeys(tmp_path):
    with pytest.raises(ValueError):
        auth.Store(tmp_path / "web").write_json(auth.PASSKEYS_FILE, {})


def test_challenges_are_once_for_one_kind_and_bounded():
    now = [1000.0]
    ch = auth.Challenges(clock=lambda: now[0])
    c = ch.issue("login")
    key = passkey.b64u(c)
    assert ch.take(key, "enrol") is None  # another kind: refused, not spent
    assert ch.take(key, "login") == c
    assert ch.take(key, "login") is None
    e = passkey.b64u(ch.issue("enrol", "s1"))
    assert ch.take(e, "enrol", "s2") is None and ch.take(e, "enrol", "s1") is not None
    old = passkey.b64u(ch.issue("login"))
    now[0] += auth.CHALLENGE_TTL
    assert ch.take(old, "login") is None
    first = passkey.b64u(ch.issue("login"))
    for _ in range(auth.CHALLENGES_MAX):
        ch.issue("login")
    assert ch.take(first, "login") is None
    assert ch.take(None, "login") is None


# --- session web passkeys -----------------------------------------------------------------

def test_the_passkeys_verb_lists_and_removes(monkeypatch, capsys):
    with short_dir("st-pk-") as home:
        monkeypatch.setenv("HOME", str(home))
        store = auth.Store(settings.web_dir())
        assert cli.main(["web", "passkeys"]) == 0
        assert "no passkeys" in capsys.readouterr().out
        kid = store.add_passkey({"id": "cred", "rp_id": NAME, "spki": "x", "alg": -7,
                                 "added": 1, "used": 0, "name": "iPhone"})
        assert cli.main(["web", "passkeys"]) == 0
        out = capsys.readouterr().out
        assert out.startswith(kid) and "iPhone" in out and f"for {NAME}" in out
        assert "used never" in out and "cred" not in out.replace(kid, "")
        assert cli.main(["web", "passkeys", "--remove", kid[:6]]) == 0
        assert f"removed 1 passkey: {kid}" in capsys.readouterr().out
        assert store.passkeys() == []
        assert cli.main(["web", "passkeys", "--remove", "nope"]) == 1
        assert "matches no passkey" in capsys.readouterr().err


# --- the follow-up: the review minors on da13e82 ---------------------------------------

def test_es256_takes_a_p256_key_only(door):
    token = code_login(door)
    r, _, offer = req(door, "POST", "/passkey/enrol/begin", cookie=token, body={})
    device = Authenticator()
    device.key = ec.generate_private_key(ec.SECP384R1())
    r, _, got = req(door, "POST", "/passkey/enrol/finish", cookie=token, body=device.create(offer))
    assert r.status == 403 and got["error"] == "public key is not a P-256 key"
    assert door["store"].passkeys() == []


def test_rs256_takes_no_key_under_2048_bits(door):
    token = code_login(door)
    r, _, offer = req(door, "POST", "/passkey/enrol/begin", cookie=token, body={})
    device = Authenticator(alg=passkey.RS256)
    device.key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    r, _, got = req(door, "POST", "/passkey/enrol/finish", cookie=token, body=device.create(offer))
    assert r.status == 403 and "under 2048" in got["error"]


def test_a_kept_key_that_no_longer_fits_its_algorithm_signs_in_nothing(kept):
    path = kept["store"].directory / auth.PASSKEYS_FILE
    data = json.loads(path.read_text())
    weak = ec.generate_private_key(ec.SECP384R1())
    data["passkeys"][0]["spki"] = passkey.b64u(weak.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    path.write_text(json.dumps(data))
    kept["device"].key = weak
    offer = begin_login(kept)
    r, _, got = req(kept, "POST", "/login/passkey/finish", body=kept["device"].get(offer))
    assert r.status == 403 and "does not match its algorithm" in got["error"]


def test_a_label_is_kept_printable_and_printed_printable(door, monkeypatch, capsys):
    token = code_login(door)
    r, _, offer = req(door, "POST", "/passkey/enrol/begin", cookie=token, body={})
    msg = Authenticator().create(offer, name="iPhone\x1b]0;owned\x07\n" + "x" * 200)
    r, _, _ = req(door, "POST", "/passkey/enrol/finish", cookie=token, body=msg)
    assert r.status == 200
    label = door["store"].passkeys()[0]["name"]
    assert label.startswith("iPhone]0;owned") and label.isprintable()
    assert len(label) == passkey.NAME_LEN
    # one kept before this rule (written by hand) still prints clean
    path = door["store"].directory / auth.PASSKEYS_FILE
    data = json.loads(path.read_text())
    data["passkeys"][0]["name"] = "old\x1b[2Jlabel"
    path.write_text(json.dumps(data))
    assert cli.main(["web", "passkeys"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "old[2Jlabel" in out


def test_a_login_flood_never_evicts_an_enrol_in_flight():
    ch = auth.Challenges(clock=lambda: 1000.0)
    enrol_key = passkey.b64u(ch.issue("enrol", "s1"))
    first_login = passkey.b64u(ch.issue("login"))
    for _ in range(auth.CHALLENGES_MAX * 3):
        ch.issue("login")
    assert ch.take(first_login, "login") is None  # the oldest login went
    assert ch.take(enrol_key, "enrol", "s1") is not None


def test_a_chunked_body_is_not_read_and_the_connection_closes(kept):
    c = http.client.HTTPConnection("127.0.0.1", kept["port"], timeout=10)
    c.putrequest("POST", "/login/passkey/finish", skip_host=True)
    for k, v in (("Host", NAME), ("Origin", ORIGIN), ("Content-Type", "application/json"),
                 ("Transfer-Encoding", "chunked")):
        c.putheader(k, v)
    c.endheaders()
    c.send(b"2\r\n{}\r\n0\r\n\r\n")
    r = c.getresponse()
    r.read()
    assert r.status == 400 and r.getheader("Connection") == "close"
    c.close()
