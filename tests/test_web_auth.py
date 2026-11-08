"""`sessiontools.web.auth`: the pairing code, sessions kept as hashes, the
cookie, the Host and Origin checks (kit-web-shell design §3), and `session
web pair|logout`. Every test runs under a temp HOME (conftest); no port is
opened and no real settings or keys are touched."""

from __future__ import annotations

import email.message
import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ANCHOR

from sessiontools import cli, settings
from sessiontools.web import auth
from sessiontools.web.auth import AuthError, Front, LoginRefused, Store

T0 = ANCHOR.timestamp()


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> Store:
    return Store(tmp_path / "kit" / "tools" / "web", clock=clock)


def _files(store: Store) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in store.directory.iterdir() if p.is_file()}


# --- the web dir --------------------------------------------------------------

def test_the_web_dir_is_beside_settings_and_accounts_never_inside_settings(tmp_path):
    env = {"HOME": str(tmp_path)}
    d = settings.web_dir(env)
    assert d == tmp_path / ".sessionkit" / "tools" / "web"
    assert d.parent == settings.accounts_dir(env).parent
    assert settings.package_dir("tools", env) not in (d, *d.parents)


def test_the_dir_is_0700_and_every_file_0600(store):
    store.pair()
    with pytest.raises(LoginRefused):
        store.login("x")  # a wrong try rewrites pair
    st = os.stat(store.directory)
    assert stat.S_IMODE(st.st_mode) == 0o700
    for p in store.directory.iterdir():
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, p.name


def test_a_too_open_dir_is_tightened(store):
    store.directory.mkdir(parents=True)
    os.chmod(store.directory, 0o755)
    store.pair()
    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700


def test_a_file_readable_by_others_is_refused_never_read(store):
    store.pair()
    os.chmod(store.directory / auth.PAIR_FILE, 0o644)
    with pytest.raises(AuthError, match="0644"):
        store.login("ABCDEFGH")


def test_a_symlinked_file_is_refused(store, tmp_path):
    store.pair()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text('{"sessions": {}}')
    os.chmod(elsewhere, 0o600)
    (store.directory / auth.SESSIONS_FILE).symlink_to(elsewhere)
    with pytest.raises(AuthError, match="not a regular file"):
        store.sessions()


def test_a_malformed_sessions_file_is_refused(store):
    store.ensure()
    path = store.directory / auth.SESSIONS_FILE
    path.write_text('{"sessions": {"abc": {"expires": "soon"}}}')
    os.chmod(path, 0o600)
    with pytest.raises(AuthError, match="not a sessions file"):
        store.sessions()


@pytest.mark.parametrize("bad", [
    {"hash": "x", "expires": 1, "tries": 0},
    {"hash": "0" * 64, "expires": "soon", "tries": 0},
    {"hash": "0" * 64, "expires": 1, "tries": "0"},
    {"expires": 1, "tries": 0},
])
def test_a_malformed_pair_file_is_refused(store, bad):
    store.ensure()
    path = store.directory / auth.PAIR_FILE
    path.write_text(json.dumps(bad))
    os.chmod(path, 0o600)
    with pytest.raises(AuthError, match="not a pair file"):
        store.login("ABCDEFGH")


# --- the pairing code ---------------------------------------------------------

def test_a_code_is_8_base32_characters_and_only_its_hash_is_stored(store):
    got = store.pair()
    assert len(got.code) == 8 and set(got.code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert got.expires == int(T0) + auth.CODE_TTL == int(T0) + 600
    data = json.loads((store.directory / auth.PAIR_FILE).read_text())
    assert data == {"hash": hashlib.sha256(got.code.encode()).hexdigest(),
                    "expires": got.expires, "tries": 0}
    for name, raw in _files(store).items():
        assert got.code.encode() not in raw, name
    assert got.code not in repr(got)


def test_pair_then_in_and_the_code_works_once(store):
    code = store.pair().code
    token = store.login(code)
    assert store.check(token)
    assert not (store.directory / auth.PAIR_FILE).exists()
    with pytest.raises(LoginRefused, match="no pairing code is pending"):
        store.login(code)


def test_case_spaces_and_hyphens_do_not_matter(store):
    code = store.pair().code
    assert store.check(store.login(f" {code[:4].lower()}-{code[4:]} "))


def test_a_code_expires_after_10_minutes(store, clock):
    code = store.pair().code
    clock.t += auth.CODE_TTL - 1
    store.pair()  # a new pair replaces the old code
    with pytest.raises(LoginRefused, match="wrong code"):
        store.login(code)
    code = store.pair().code
    clock.t += auth.CODE_TTL
    with pytest.raises(LoginRefused, match="expired"):
        store.login(code)
    assert not (store.directory / auth.PAIR_FILE).exists()


def test_five_wrong_tries_burn_the_code(store):
    code = store.pair().code
    wrong = "AAAAAAAA" if code != "AAAAAAAA" else "BBBBBBBB"
    for n in range(1, auth.MAX_TRIES):
        with pytest.raises(LoginRefused, match=f"{n} of 5"):
            store.login(wrong)
    with pytest.raises(LoginRefused, match="burned"):
        store.login("not even a code")  # a malformed try counts too
    assert not (store.directory / auth.PAIR_FILE).exists()
    with pytest.raises(LoginRefused, match="no pairing code is pending"):
        store.login(code)  # the right code, too late


def test_a_refusal_never_carries_the_code(store):
    code = store.pair().code
    near = code[:-1] + ("A" if code[-1] != "A" else "B")
    with pytest.raises(LoginRefused) as e:
        store.login(near)
    assert code not in str(e.value) and near not in str(e.value)


# --- sessions -----------------------------------------------------------------

def test_a_session_is_stored_only_as_its_hash(store):
    token = store.login(store.pair().code)
    assert len(token) >= 43  # 32 random bytes, urlsafe
    data = json.loads((store.directory / auth.SESSIONS_FILE).read_text())
    key = hashlib.sha256(token.encode()).hexdigest()
    assert data == {"sessions": {key: {"created": int(T0), "seen": int(T0),
                                       "expires": int(T0) + auth.MAX_AGE,
                                       "via": auth.VIA_CODE}}}
    for name, raw in _files(store).items():
        assert token.encode() not in raw, name


def test_a_session_lasts_30_days(store, clock):
    token = store.login(store.pair().code)
    clock.t += auth.MAX_AGE - 1
    assert store.check(token)
    clock.t += 1
    assert not store.check(token)
    assert store.sessions() == []


def test_seen_is_brought_up_to_date_but_not_on_every_request(store, clock):
    token = store.login(store.pair().code)
    before = (store.directory / auth.SESSIONS_FILE).read_bytes()
    clock.t += auth.SEEN_EVERY - 1
    assert store.check(token)
    assert (store.directory / auth.SESSIONS_FILE).read_bytes() == before
    clock.t += 1
    assert store.check(token)
    assert store.sessions()[0].seen == int(clock.t)


@pytest.mark.parametrize("bad", ["", "short", "x" * 200, "a b c d e f g h i j k l m n o",
                                 None, 42])
def test_a_malformed_token_is_not_a_session(store, bad):
    store.login(store.pair().code)
    assert not store.check(bad)


def test_an_unknown_token_is_not_a_session(store):
    store.login(store.pair().code)
    assert not store.check("A" * 43)


def test_revoke_ends_one_session(store):
    a = store.login(store.pair().code)
    b = store.login(store.pair().code)
    assert store.revoke(a)
    assert not store.check(a) and store.check(b)
    assert not store.revoke(a)


def test_logout_by_id_prefix_and_all(store, clock):
    a = store.login(store.pair().code)
    clock.t += 1
    b = store.login(store.pair().code)
    ids = [s.id for s in store.sessions()]
    assert ids == [hashlib.sha256(t.encode()).hexdigest()[:auth.ID_LEN] for t in (a, b)]
    assert store.logout([ids[0][:8]]) == [ids[0]]
    assert not store.check(a) and store.check(b)
    assert store.logout(None) == [ids[1]]
    assert not store.check(b)


def test_logout_refuses_an_id_that_matches_nothing_and_ends_nothing(store):
    a = store.login(store.pair().code)
    with pytest.raises(AuthError, match="matches no session"):
        store.logout(["zz", store.sessions()[0].id])
    assert store.check(a)


# --- the cookie ---------------------------------------------------------------

def test_the_cookie_attributes():
    c = auth.session_cookie("TOKEN", secure=False)
    assert c == "kit_session=TOKEN; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
    assert "Domain" not in c and "Secure" not in c
    assert auth.session_cookie("TOKEN", secure=True).endswith("; Secure")


def test_the_clearing_cookie():
    c = auth.clear_cookie(secure=True)
    assert c == "kit_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0; Secure"


def test_cookie_tokens_reads_every_kit_session_and_nothing_else():
    msg = email.message.Message()
    msg["Cookie"] = "a=1; kit_session=one; talk_token=x"
    msg["Cookie"] = 'kit_session="two"'
    assert auth.cookie_tokens(msg) == ["one", "two"]
    assert auth.cookie_tokens({"cookie": "kit_session=three"}) == ["three"]
    assert auth.cookie_tokens({"Cookie": "kit_sessionx=no; xkit_session=no; kit_session="}) == []


# --- Host and Origin ----------------------------------------------------------

FRONT = Front(port=8750)


@pytest.mark.parametrize("host", ["127.0.0.1:8750", "127.0.0.1:8750".upper()])
def test_loopback_with_the_port_is_answered(host):
    assert auth.host_allowed(host, FRONT)


@pytest.mark.parametrize("host", [
    None, "", "localhost:8750", "LOCALHOST:8750", "localhost", "127.0.0.1:8751",
    "127.0.0.1", "evil.example:8750", "evil.example", "127.0.0.1.nip.io:8750",
    "[::1]:8750", "0.0.0.0:8750", "127.0.0.1:8750@evil", "127.0.0.1:8750/x",
    "127.0.0.1:0", "127.0.0.1:99999", "127.0.0.1:port", "[::1", "a:b:c",
])
def test_anything_else_is_refused_dns_rebinding_included(host):
    assert not auth.host_allowed(host, FRONT)


def test_the_bound_address_and_configured_names_are_answered():
    front = Front(port=8750, bound="::1", names=("HOST.tail1234.example.net.",))
    assert auth.host_allowed("[::1]:8750", front)
    assert auth.host_allowed("[0:0:0:0:0:0:0:1]:8750", front)
    assert auth.host_allowed("127.0.0.1:8750", front)
    assert auth.host_allowed("host.tail1234.example.net", front)
    assert auth.host_allowed("host.tail1234.example.net:443", front)
    assert not auth.host_allowed("tail1234.example.net", front)
    tail = Front(port=8750, bound="100.101.102.103")
    assert auth.host_allowed("100.101.102.103:8750", tail)
    assert not auth.host_allowed("100.101.102.104:8750", tail)


@pytest.mark.parametrize("name", ["localhost", "a.localhost", "127.0.0.1", "::1", "bad name",
                                  "-x.example", ""])
def test_localhost_an_address_or_a_non_name_is_never_a_name(name):
    with pytest.raises(ValueError):
        Front(port=8750, names=(name,))


@pytest.mark.parametrize("port", [0, 65536, "8750", None])
def test_a_front_needs_a_real_port(port):
    with pytest.raises(ValueError):
        Front(port=port)  # type: ignore[arg-type]


def test_the_origin_must_be_the_front_doors_own():
    host = "127.0.0.1:8750"
    assert auth.origin_allowed("http://127.0.0.1:8750", host, FRONT)
    assert auth.origin_allowed("http://127.0.0.1:8750/", host, FRONT)
    for origin in (None, "", "null", "https://127.0.0.1:8750", "http://127.0.0.1:8751",
                   "http://localhost:8750", "http://evil.example", "http://127.0.0.1:8750/x",
                   "http://u@127.0.0.1:8750", "http://127.0.0.1:8750?q", "ftp://127.0.0.1:8750"):
        assert not auth.origin_allowed(origin, host, FRONT), origin
    # Origin and Host must agree, and Host must be allowed
    assert not auth.origin_allowed("http://127.0.0.1:8750", "evil.example:8750", FRONT)
    named = Front(port=8750, names=("host.tail1234.example.net",))
    assert auth.origin_allowed("https://host.tail1234.example.net", "host.tail1234.example.net", named)
    assert not auth.origin_allowed("http://host.tail1234.example.net", "host.tail1234.example.net", named)
    assert not auth.origin_allowed("https://host.tail1234.example.net", "127.0.0.1:8750", named)


def test_a_name_is_https_and_the_address_http_on_one_front_door():
    named = Front(port=8750, names=("host.tail1234.example.net",))
    assert auth.origin_allowed("http://127.0.0.1:8750", "127.0.0.1:8750", named)
    assert not auth.origin_allowed("https://127.0.0.1:8750", "127.0.0.1:8750", named)
    assert named.scheme_for("host.tail1234.example.net") == "https"
    assert named.scheme_for("HOST.tail1234.example.net.:443") == "https"
    assert named.scheme_for("127.0.0.1:8750") == "http"
    assert named.scheme_for(None) == "http"
    assert named.secure_for("host.tail1234.example.net") is True
    assert named.secure_for("127.0.0.1:8750") is False
    assert named.name_of("host.tail1234.example.net:8443") == "host.tail1234.example.net"
    assert named.name_of("other.tail1234.example.net") is None
    assert named.name_of("[::1") is None
    # an address with no port is plain http's 80, never https's 443
    at80 = Front(port=80)
    assert auth.host_allowed("127.0.0.1", at80)
    assert not auth.host_allowed("127.0.0.1", Front(port=443))


# --- the gate -----------------------------------------------------------------

def _headers(**h: str) -> email.message.Message:
    msg = email.message.Message()
    for k, v in h.items():
        msg[k.replace("_", "-")] = v
    return msg


OK_HOST = {"Host": "127.0.0.1:8750"}


@pytest.mark.parametrize("path", ["/", "/settings/", "/talk/", "/talk/events", "/loginx"])
def test_the_wall_without_a_cookie(store, path):
    assert auth.gate(store, FRONT, "GET", path, _headers(**OK_HOST)).status == 401


@pytest.mark.parametrize("path", ["/login", "/login/", "/login/style.css", "/login?next=/"])
def test_only_the_login_is_open(store, path):
    assert auth.gate(store, FRONT, "GET", path, _headers(**OK_HOST)).ok


@pytest.mark.parametrize("path", [
    "/login/../settings/", "/login/./../talk/", "/login/..", "/login/.", "/login//etc",
    "/login/%2e%2e/talk/", "/login/%2E%2E/settings/", "/login/..%2fsettings",
    "/login/%2e%2e%2fsettings", "/login/%252e%252e/talk/", "/login/..\\settings",
    "/login/%5c..%5csettings",
])
def test_a_path_that_climbs_out_of_the_login_is_not_open(store, path):
    """The open set is /login and what lies under it, however a router,
    a static file server or a room later normalises the path."""
    assert not auth.is_open(path)
    assert auth.gate(store, FRONT, "GET", path, _headers(**OK_HOST)).status == 401


def test_a_live_cookie_opens_every_path(store):
    token = store.login(store.pair().code)
    v = auth.gate(store, FRONT, "GET", "/settings/",
                  _headers(**OK_HOST, Cookie=f"other=1; kit_session={token}"))
    assert v.ok and v.token == token
    assert token not in repr(v)


def test_a_stale_same_name_cookie_beside_a_live_one_still_opens(store):
    token = store.login(store.pair().code)
    hdr = _headers(**OK_HOST, Cookie=f"kit_session={'Z' * 43}; kit_session={token}")
    assert auth.gate(store, FRONT, "GET", "/", hdr).ok


def test_a_token_in_the_query_string_opens_nothing(store):
    token = store.login(store.pair().code)
    for path in (f"/?kit_session={token}", f"/settings/?token={token}", f"/{token}"):
        assert auth.gate(store, FRONT, "GET", path, _headers(**OK_HOST)).status == 401


def test_the_host_check_comes_first_even_with_a_live_cookie(store):
    token = store.login(store.pair().code)
    for host in ("localhost:8750", "evil.example:8750"):
        v = auth.gate(store, FRONT, "GET", "/", _headers(Host=host, Cookie=f"kit_session={token}"))
        assert v.status == 403 and v.reason == "host not allowed"
    v = auth.gate(store, FRONT, "GET", "/login", _headers(Host="evil.example:8750"))
    assert v.status == 403
    no_host = auth.gate(store, FRONT, "GET", "/", _headers(Cookie=f"kit_session={token}"))
    assert no_host.status == 403
    two = _headers(Host="127.0.0.1:8750", Cookie=f"kit_session={token}")
    two["Host"] = "evil.example"
    assert auth.gate(store, FRONT, "GET", "/", two).status == 403


def test_a_post_needs_the_front_doors_own_origin(store):
    token = store.login(store.pair().code)
    cookie = f"kit_session={token}"
    ok = auth.gate(store, FRONT, "POST", "/settings/",
                   _headers(**OK_HOST, Cookie=cookie, Origin="http://127.0.0.1:8750"))
    assert ok.ok
    for origin in (None, "null", "http://evil.example"):
        h = _headers(**OK_HOST, Cookie=cookie)
        if origin:
            h["Origin"] = origin
        v = auth.gate(store, FRONT, "POST", "/settings/", h)
        assert v.status == 403 and v.reason == "origin not allowed", origin
    # the login POST too: it is open, but not to another origin
    v = auth.gate(store, FRONT, "POST", "/login", _headers(**OK_HOST, Origin="http://evil.example"))
    assert v.status == 403


# --- nothing secret is logged -------------------------------------------------

def test_no_code_or_token_reaches_a_log_or_a_message(store, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    code = store.pair().code
    token = store.login(code)
    store.check(token)
    auth.gate(store, FRONT, "GET", f"/?kit_session={token}",
              _headers(**OK_HOST, Cookie=f"kit_session={token}"))
    store.revoke(token)
    out = capsys.readouterr()
    for secret in (code, token):
        assert secret not in caplog.text
        assert secret not in out.out and secret not in out.err
        assert secret not in repr(store)


# --- session web pair|logout --------------------------------------------------

def _web(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli.main(["web", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_session_web_pair_prints_a_code_that_logs_in(capsys):
    rc, out, _ = _web(capsys, "pair")
    assert rc == 0
    code = out.split("pairing code: ", 1)[1].split()[0]
    store = Store(settings.web_dir())
    assert Path(os.environ["HOME"]) in store.directory.parents
    assert store.check(store.login(code))


def test_session_web_logout_lists_bare_and_ends_by_id_or_all(capsys):
    store = Store(settings.web_dir())
    rc, out, err = _web(capsys, "logout")
    assert (rc, out.strip()) == (2, "no live sessions") and "nothing was ended" in err
    a = store.login(store.pair().code)
    b = store.login(store.pair().code)
    rc, out, err = _web(capsys, "logout")
    assert rc == 2 and len(out.splitlines()) == 2
    assert a not in out and b not in out
    first = store.sessions()[0].id
    rc, out, _ = _web(capsys, "logout", first)
    assert rc == 0 and out.strip() == f"ended 1 session: {first}"
    rc, out, _ = _web(capsys, "logout", "--all")
    assert rc == 0 and out.startswith("ended 1 session")
    assert store.sessions() == []


def test_session_web_logout_refusals(capsys):
    rc, _, err = _web(capsys, "logout", "deadbeef")
    assert rc == 1 and "matches no session" in err
    with pytest.raises(SystemExit) as e:
        _web(capsys, "logout", "--all", "deadbeef")
    assert e.value.code == 2


def test_session_web_bare_prints_help_and_exits_2(capsys):
    rc, _, err = _web(capsys)
    assert rc == 2 and "pair|logout" in err


def test_session_web_imports_stdlib_only():
    probe = ("import sys; import sessiontools.cli; from sessiontools.web import auth, bind, cli, proxy, rooms, supervisor, unixhttp; "
             "print('yaml' in sys.modules, 'jsonschema' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "False False"
