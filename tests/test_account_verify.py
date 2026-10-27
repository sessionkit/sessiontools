"""`session account verify [NAME] [--json]`: which account a token bills.

HARD LIMITS: no test reaches the network. Every call goes through a stub
transport (`post=`); the autouse fixture makes `urllib.request.urlopen`
fail the test, and any subprocess naming `security`, `pass` or `claude`
raise. The Keychain is a `FakeKeychain`. Every token is synthetic
(`sk-ant-oat01-TEST-<random>`), and the no-leak tests assert the token is
in none of stdout, stderr, the `--json`, or any raised message.
"""

from __future__ import annotations

import io
import json
import os
import secrets as pysecrets
import subprocess
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

import pytest
from settings_support import write_session_settings

from sessiontools.account import cli
from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.account import verify as vf

FORBIDDEN = ("security", "pass", "claude")
NOW = datetime(2030, 6, 9, 5, 0, tzinfo=UTC)
RESET_5H = int(datetime(2030, 6, 9, 8, 0, tzinfo=UTC).timestamp())    # in 3h
RESET_7D = int(datetime(2030, 6, 9, 20, 0, tzinfo=UTC).timestamp())    # in 15h
HEADERS = {
    "anthropic-ratelimit-unified-5h-utilization": "0.12",
    "anthropic-ratelimit-unified-5h-reset": str(RESET_5H),
    "anthropic-ratelimit-unified-7d-utilization": "0.06",
    "anthropic-ratelimit-unified-7d-reset": str(RESET_7D),
    "anthropic-ratelimit-unified-status": "allowed",
    "request-id": "req_test_0001",
}


def _argv0(args, kwargs) -> str:
    argv = args[0] if args else kwargs.get("args")
    if isinstance(argv, (list, tuple)) and argv:
        return os.path.basename(str(argv[0]))
    if isinstance(argv, (str, bytes)):
        text = argv.decode() if isinstance(argv, bytes) else argv
        return os.path.basename(text.split()[0]) if text.split() else ""
    return ""


@pytest.fixture(autouse=True)
def _no_network_no_security(monkeypatch, tmp_path_factory):
    """urlopen fails the test; security, pass and claude raise; no env token."""
    def no_network(*args, **kwargs):
        raise AssertionError("test reached urllib.request.urlopen")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, _name=name, **kwargs):
            if _argv0(args, kwargs) in FORBIDDEN:
                raise AssertionError(f"test tried to run {_argv0(args, kwargs)!r}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-cfg")))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def synthetic_token() -> str:
    return "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)


class Stub:
    """The transport seam: records each call, answers STATUS/HEADERS/BODY,
    or raises RAISES."""

    def __init__(self, status: int = 200, headers: dict | None = None,
                 body: bytes = b'{"type": "message"}', raises: BaseException | None = None):
        self.status = status
        self.headers = dict(HEADERS) if headers is None else headers
        self.body = body
        self.raises = raises
        self.calls: list[tuple[str, dict, bytes, float]] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body, timeout))
        if self.raises is not None:
            raise self.raises
        return self.status, self.headers, self.body


class World:
    """A tmp HOME with accounts a (file), c (file), p (pass only, fp
    unknown) registered, a FakeKeychain holding HOST's token, a stub."""

    def __init__(self, home: Path, *, host: str | None = "c") -> None:
        self.home = home
        self.tokens_dir = home / "tokens"
        self.tokens_dir.mkdir(mode=0o700)
        self.tokens = {n: synthetic_token() for n in ("a", "c", "p", "stranger")}
        registry = reg.Registry()
        for name, email in (("a", "ai@example.com"), ("c", "carol@example.com")):
            path = self.tokens_dir / name
            path.write_text(self.tokens[name] + "\n")
            path.chmod(0o600)
            registry.add(reg.make_account(name, email, [f"file:{path}"],
                                          reg.fingerprint(self.tokens[name])))
        registry.add(reg.make_account("p", "peggy@example.com",
                                      ["pass:anthropic/peggy@example.com"], None))
        reg.save(registry, reg.default_path(home))
        self.keychain = kc.FakeKeychain(None if host is None
                                        else kc.render_item(self.tokens[host]))
        self.env: dict[str, str] = {}
        self.backends = {"pass": self.pass_backend, "file": reg.file_backend}
        self.stub = Stub()
        self.out = io.StringIO()
        self.err = io.StringIO()

    def pass_backend(self, ref: str) -> str:
        return self.tokens["p"]

    def verify(self, *argv: str, **overrides) -> int:
        kwargs = {"keychain": self.keychain, "backends": self.backends, "env": self.env,
                  "home": self.home, "post": self.stub, "clock": lambda: NOW,
                  "out": self.out, "err": self.err}
        kwargs.update(overrides)
        return cli.main(["verify", *argv], **kwargs)

    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def token_file(self, holder: str = "a") -> Path:
        path = self.tokens_dir / "host-oauth"
        path.write_text(self.tokens[holder] + "\n")
        path.chmod(0o600)
        write_session_settings(self.home, {"account": {"token_file": str(path)}})
        self.keychain = kc.FakeKeychain(None)
        return path

    def assert_no_leak(self, *extra: str) -> None:
        for token in self.tokens.values():
            for text in (self.text(), *extra):
                assert token not in text
                assert token.encode().hex() not in text
                assert reg.fingerprint(token) not in text


@pytest.fixture
def world(tmp_path) -> World:
    home = tmp_path / "home"
    home.mkdir()
    return World(home)


# --- the call ----------------------------------------------------------------------------


def test_one_post_with_the_token_as_bearer_and_a_one_token_haiku_body(world):
    assert world.verify() == 0, world.text()
    assert len(world.stub.calls) == 1
    url, headers, body, timeout = world.stub.calls[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers == {"Authorization": f"Bearer {world.tokens['c']}",
                       "anthropic-version": "2023-06-01",
                       "anthropic-beta": "oauth-2025-04-20",
                       "content-type": "application/json"}
    assert json.loads(body) == {"model": "claude-haiku-4-5", "max_tokens": 1,
                                "messages": [{"role": "user", "content": "."}]}
    assert timeout == 20
    assert vf.MODEL == "claude-haiku-4-5"


# --- success ------------------------------------------------------------------------------


def test_success_prints_source_account_both_windows_and_status(world):
    assert world.verify() == 0, world.text()
    lines = world.out.getvalue().splitlines()
    assert lines[0] == "verify: token from Keychain item; matches registry account c"
    assert lines[1] == "  5h  12% used  (resets 2030-06-09T08:00+00:00, in 3h)"
    assert lines[2] == ("  7d  6% used  (resets 2030-06-09T20:00+00:00, in 15h)"
                        "  <- the 7d reset identifies the account")
    assert lines[3] == ("  status: allowed (HTTP 200, model claude-haiku-4-5, "
                        "max_tokens 1)")
    assert len(lines) == 4
    assert world.err.getvalue() == ""
    assert world.keychain.reads == 1 and world.keychain.writes == []
    assert "req_test_0001" not in world.text()      # no other header is shown
    world.assert_no_leak()


def test_json_shape_is_the_design(world):
    assert world.verify("--json") == 0, world.text()
    data = json.loads(world.out.getvalue())
    assert data == {
        "source": "Keychain item",
        "account": "c",
        "http_status": 200,
        "model": "claude-haiku-4-5",
        "windows": {"5h": {"utilization": 12.0, "resets_at": "2030-06-09T08:00+00:00"},
                    "7d": {"utilization": 6.0, "resets_at": "2030-06-09T20:00+00:00"}},
        "status": "allowed"}
    world.assert_no_leak()


def test_a_missing_header_is_not_reported_never_zero(world):
    world.stub.headers = {"anthropic-ratelimit-unified-7d-utilization": "0.5",
                          "anthropic-ratelimit-unified-7d-reset": str(RESET_7D)}
    assert world.verify() == 0, world.text()
    lines = world.out.getvalue().splitlines()
    assert lines[1] == "  5h  not reported"
    assert lines[2].startswith("  7d  50% used  (resets ")
    assert "status: not reported (HTTP 200" in lines[3]
    world.out = io.StringIO()
    assert world.verify("--json") == 0
    data = json.loads(world.out.getvalue())
    assert data["windows"]["5h"] == {"utilization": None, "resets_at": None}
    assert data["status"] is None


def test_a_utilization_above_one_is_already_a_percent(world):
    world.stub.headers = dict(HEADERS, **{
        "anthropic-ratelimit-unified-5h-utilization": "37.5"})
    assert world.verify() == 0
    assert world.out.getvalue().splitlines()[1].startswith("  5h  37.5% used")


def test_a_fraction_prints_cleanly(world):
    world.stub.headers = dict(HEADERS, **{
        "anthropic-ratelimit-unified-5h-utilization": "0.07"})
    assert world.verify() == 0
    assert world.out.getvalue().splitlines()[1].startswith("  5h  7% used")


def test_header_names_are_case_insensitive(world):
    world.stub.headers = {k.title(): v for k, v in HEADERS.items()}
    assert world.verify() == 0, world.text()
    assert "12% used" in world.out.getvalue()


def test_a_reset_in_the_past_says_ago(world):
    past = int(datetime(2030, 6, 9, 3, 0, tzinfo=UTC).timestamp())
    world.stub.headers = dict(HEADERS, **{"anthropic-ratelimit-unified-5h-reset": str(past)})
    assert world.verify() == 0
    assert "(resets 2030-06-09T03:00+00:00, 2h ago)" in world.out.getvalue()


def test_a_429_with_headers_is_exit_0_and_says_rate_limited(world):
    world.stub.status = 429
    world.stub.headers = dict(HEADERS, **{"anthropic-ratelimit-unified-status": "rejected"})
    world.stub.body = b'{"type":"error","error":{"type":"rate_limit_error","message":"x"}}'
    assert world.verify() == 0, world.text()
    out = world.out.getvalue()
    assert "rate limited" in out
    assert "status: rejected (HTTP 429, rate limited, model claude-haiku-4-5" in out
    assert "12% used" in out


def test_a_header_value_that_is_not_plain_is_not_shown(world):
    token = world.tokens["a"]
    world.stub.headers = dict(HEADERS, **{"anthropic-ratelimit-unified-status": token})
    assert world.verify() == 0
    assert "status: (not shown)" in world.out.getvalue()
    world.assert_no_leak()


# --- the token sources --------------------------------------------------------------------


def test_the_env_token_wins_and_is_labelled(world):
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["a"]}
    assert world.verify() == 0, world.text()
    assert world.out.getvalue().splitlines()[0] == \
        "verify: token from env token; matches registry account a"
    assert world.stub.calls[0][1]["Authorization"] == f"Bearer {world.tokens['a']}"
    assert world.keychain.reads == 0
    world.assert_no_leak()


def test_the_token_file_route_reads_the_file_and_says_it_checks_the_file(world):
    path = world.token_file("a")
    assert world.verify() == 0, world.text()
    lines = world.out.getvalue().splitlines()
    assert lines[0] == f"verify: token from token file {path}; matches registry account a"
    assert lines[1] == ("  note: no env token here: a seat with none on this host signs in "
                        "from Claude's stored credentials, so this checks the FILE, not "
                        "necessarily this seat")
    assert world.stub.calls[0][1]["Authorization"] == f"Bearer {world.tokens['a']}"
    assert world.keychain.reads == 0
    world.out = io.StringIO()
    assert world.verify("--json") == 0
    assert json.loads(world.out.getvalue())["source"] == f"token file {path}"
    world.assert_no_leak()


def test_the_env_token_on_a_token_file_host_has_no_file_note(world):
    world.token_file("a")
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["c"]}
    assert world.verify() == 0
    assert "note:" not in world.out.getvalue()
    assert "env token; matches registry account c" in world.out.getvalue()


def test_a_name_resolves_through_its_registry_source(world):
    assert world.verify("a") == 0, world.text()
    first = world.out.getvalue().splitlines()[0]
    assert first == (f"verify: token from registry account a "
                     f"(file:{world.tokens_dir / 'a'}); matches registry account a")
    assert world.stub.calls[0][1]["Authorization"] == f"Bearer {world.tokens['a']}"
    assert world.keychain.reads == 0
    world.assert_no_leak()


def test_a_name_whose_fp_is_unknown_is_not_in_registry(world):
    assert world.verify("p") == 0, world.text()
    first = world.out.getvalue().splitlines()[0]
    assert first == ("verify: token from registry account p "
                     "(pass:anthropic/peggy@example.com); not in registry")
    world.out = io.StringIO()
    assert world.verify("p", "--json") == 0
    assert json.loads(world.out.getvalue())["account"] is None
    world.assert_no_leak()


def test_a_token_no_account_holds_is_not_in_registry(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    world = World(home, host="stranger")
    assert world.verify() == 0
    assert world.out.getvalue().splitlines()[0] == \
        "verify: token from Keychain item; not in registry"


# --- the failures -------------------------------------------------------------------------


def test_a_401_is_exit_6_token_refused(world):
    world.stub.status = 401
    world.stub.headers = {"request-id": "req_x"}
    world.stub.body = (b'{"type":"error","error":{"type":"authentication_error",'
                       b'"message":"invalid bearer token"}}')
    assert world.verify() == 6
    assert world.out.getvalue() == ""
    err = world.err.getvalue()
    assert "token refused (HTTP 401: authentication_error)" in err
    assert "invalid bearer token" in err
    world.assert_no_leak()


def test_a_403_is_exit_6_too(world):
    world.stub.status = 403
    world.stub.headers = {}
    world.stub.body = b"not json"
    assert world.verify() == 6
    assert "token refused (HTTP 403" in world.err.getvalue()


def test_a_token_shaped_error_message_is_withheld(world):
    world.stub.status = 401
    token = world.tokens["c"]
    world.stub.body = json.dumps({"type": "error", "error": {
        "type": "authentication_error", "message": f"bad token {token}"}}).encode()
    assert world.verify() == 6
    assert "bad token" not in world.err.getvalue()
    world.assert_no_leak()


def test_a_long_error_message_is_cut_to_200(world):
    world.stub.status = 401
    world.stub.body = json.dumps({"type": "error", "error": {
        "type": "authentication_error", "message": "y" * 500}}).encode()
    assert world.verify() == 6
    assert "y" * 200 in world.err.getvalue()
    assert "y" * 201 not in world.err.getvalue()


def test_a_transport_exception_is_exit_7_named_by_type_only(world):
    token = world.tokens["c"]
    world.stub.raises = ConnectionResetError(54, f"reset while sending {token}")
    assert world.verify() == 7
    err = world.err.getvalue()
    assert "no answer" in err
    assert "ConnectionResetError" in err
    assert "https://api.anthropic.com/v1/messages" in err
    assert "reset while sending" not in err
    world.assert_no_leak()


def test_an_answer_with_no_window_headers_is_exit_7(world):
    world.stub.headers = {"anthropic-ratelimit-unified-status": "allowed"}
    assert world.verify() == 7
    assert "no unified rate-limit window headers" in world.err.getvalue()
    assert world.out.getvalue() == ""


def test_a_5xx_is_exit_7(world):
    world.stub.status = 529
    world.stub.headers = {}
    world.stub.body = b'{"type":"error","error":{"type":"overloaded_error","message":"o"}}'
    assert world.verify() == 7
    assert "HTTP 529: overloaded_error" in world.err.getvalue()


def test_no_token_anywhere_is_exit_4(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    world = World(home, host=None)
    assert world.verify() == 4
    assert "no token" in world.err.getvalue()
    assert world.stub.calls == []


def test_an_item_with_no_access_token_is_exit_4(world):
    world.keychain.blob = json.dumps({kc.ITEM_KEY: {"refreshToken": None}}).encode()
    assert world.verify() == 4
    assert world.stub.calls == []


def test_a_keychain_read_failure_is_exit_4(world):
    world.keychain.fail_read = True
    assert world.verify() == 4
    assert world.stub.calls == []


def test_an_item_that_is_not_an_item_is_refused_exit_5(world):
    world.keychain.blob = b"not json"
    assert world.verify() == 5
    assert world.stub.calls == []


def test_a_token_file_that_does_not_exist_is_exit_4(world):
    path = world.token_file("a")
    path.unlink()
    assert world.verify() == 4
    assert str(path) in world.err.getvalue()
    assert world.stub.calls == []


def test_a_token_file_that_is_not_private_is_refused_exit_5(world):
    path = world.token_file("a")
    path.chmod(0o644)
    assert world.verify() == 5
    assert world.stub.calls == []


def test_an_unknown_name_is_exit_2(world):
    assert world.verify("nobody") == 2
    assert "no account named 'nobody'" in world.err.getvalue()
    assert world.stub.calls == []


def test_a_name_with_no_source_that_resolves_is_exit_4(world):
    world.backends = {"pass": world.pass_backend}       # no file backend
    assert world.verify("a") == 4
    assert world.stub.calls == []


def test_a_refused_registry_is_exit_5(world):
    path = reg.default_path(world.home)
    path.write_text("{not json")
    assert world.verify() == 5
    assert world.stub.calls == []


def test_a_bad_flag_is_usage_exit_2(world):
    assert world.verify("--nope") == 2


# --- never a token ------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["success", "401", "exception"])
@pytest.mark.parametrize("as_json", [False, True])
def test_the_token_is_in_no_output_json_or_raised_message(world, case, as_json):
    token = world.tokens["c"]
    if case == "401":
        world.stub.status = 401
        world.stub.body = b'{"type":"error","error":{"type":"authentication_error"}}'
    elif case == "exception":
        world.stub.raises = RuntimeError(f"Bearer {token}")
    raised: list[str] = []
    try:
        world.verify(*(["--json"] if as_json else []))
    except BaseException as error:  # noqa: BLE001 - recorded, then asserted on
        raised.append(repr(error) + str(error))
    assert raised == []
    world.assert_no_leak(*raised)


def test_the_token_is_not_in_any_file_under_home(world):
    world.verify()
    for path in world.home.rglob("*"):
        if path.is_file() and path.parent != world.tokens_dir:
            data = path.read_bytes()
            for token in world.tokens.values():
                assert token.encode() not in data


def test_verify_source_holds_no_token_shaped_literal():
    text = Path(vf.__file__).read_text(encoding="utf-8")
    assert reg._TOKEN_SHAPE.search(text) is None


# --- the default transport, never reached -------------------------------------------------


def test_the_default_transport_is_not_reached_when_a_stub_is_given(world, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("the default transport reached urlopen")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    assert world.verify() == 0
    assert world.verify("--json") == 0
    assert len(world.stub.calls) == 2


class _Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _message(headers: dict) -> Message:
    msg = Message()
    for key, value in headers.items():
        msg[key] = value
    return msg


def test_urllib_post_returns_status_headers_body(monkeypatch):
    seen = {}

    def fake(request, timeout):
        seen["request"], seen["timeout"] = request, timeout
        return _Response(200, _message({"anthropic-ratelimit-unified-status": "allowed"}),
                         b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    status, headers, body = vf.urllib_post("https://example.com/v1/messages",
                                           {"x-test": "1"}, b"{}", 20)
    assert (status, body) == (200, b"{}")
    assert headers["anthropic-ratelimit-unified-status"] == "allowed"
    assert seen["request"].get_method() == "POST"
    assert seen["request"].data == b"{}"
    assert seen["timeout"] == 20


def test_urllib_post_returns_a_4xx_with_its_headers(monkeypatch):
    def fake(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests",
            _message({"anthropic-ratelimit-unified-5h-utilization": "1.0"}),
            io.BytesIO(b'{"type":"error"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    status, headers, body = vf.urllib_post("https://example.com/v1/messages", {}, b"{}", 20)
    assert status == 429
    assert headers["anthropic-ratelimit-unified-5h-utilization"] == "1.0"
    assert body == b'{"type":"error"}'


# --- the CLI ------------------------------------------------------------------------------


def test_exit_codes_are_the_design_table_and_in_the_epilog():
    assert set(vf.EXIT_CODES) == {0, 2, 4, 5, 6, 7}
    assert "verify" in cli.EPILOG
    for code in vf.EXIT_CODES:
        assert str(code) in vf.EPILOG_LINE


def test_help_names_verify(capsys):
    out = io.StringIO()
    assert cli.main(["--help"], out=out) == 0
    assert "verify" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["verify", "--help"], out=out) == 0
    assert "--json" in out.getvalue()
