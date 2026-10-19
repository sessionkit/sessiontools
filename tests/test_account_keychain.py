"""The Keychain seam (ST-22 design-002 §3, do-002).

HARD LIMITS (by design): no Keychain read or write, no real
account switch, no /login, and no run of `security`, `pass` or `claude`.
The autouse fixture below makes any subprocess naming one of them raise,
gives every test an empty CLAUDE_CONFIG_DIR, and the conftest's tmp HOME
holds. `SecurityCLI` is only ever built with a RECORDING runner, which
captures the argv and stdin it would hand `security` and runs nothing;
the one test that leaves the default runner in place proves the guard
stops it before anything runs. Every token is synthetic
(`sk-ant-oat01-TEST-<random>`), and each test that handles one asserts it
is absent from all captured output and from every file the test wrote.
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import subprocess
from pathlib import Path

import pytest

from sessiontools.account import keychain as kc
from sessiontools.account.registry import fingerprint

FORBIDDEN = ("security", "pass", "claude")
FIXTURE = Path(__file__).parent / "fixtures" / "keychain-item.json"


def _argv0(args, kwargs) -> str:
    argv = args[0] if args else kwargs.get("args")
    if isinstance(argv, (list, tuple)) and argv:
        return os.path.basename(str(argv[0]))
    if isinstance(argv, (str, bytes)):
        text = argv.decode() if isinstance(argv, bytes) else argv
        return os.path.basename(text.split()[0]) if text.split() else ""
    return ""


@pytest.fixture(autouse=True)
def _no_keychain_no_pass_no_claude(monkeypatch, tmp_path_factory):
    """Any subprocess call to security, pass or claude raises, here and in
    the code under test."""
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, _name=name, **kwargs):
            if _argv0(args, kwargs) in FORBIDDEN:
                raise AssertionError(f"test tried to run {_argv0(args, kwargs)!r} "
                                     f"via subprocess.{_name}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)
    empty = tmp_path_factory.mktemp("claude-config-empty")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def test_the_guard_refuses_pass_security_and_claude():
    for name in FORBIDDEN:
        with pytest.raises(AssertionError, match="test tried to run"):
            subprocess.run([name, "--version"], capture_output=True, check=False)
    with pytest.raises(AssertionError, match="test tried to run"):
        subprocess.run([kc.SECURITY, "-h"], capture_output=True, check=False)


def synthetic_token() -> str:
    return "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)


def assert_absent(token: str, capsys, *roots: Path) -> None:
    """The token, and its hex, are in no captured output and in no file
    under any root."""
    out, err = capsys.readouterr()
    hexed = token.encode().hex()
    for text in (out, err):
        assert token not in text and hexed not in text
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert token.encode() not in data, f"token leaked into {path}"
                assert hexed.encode() not in data, f"token hex leaked into {path}"


class Recorder:
    """A runner that records what `SecurityCLI` would run and runs nothing.
    It answers with the queued CompletedProcess values, else exit 0."""

    def __init__(self, *replies: subprocess.CompletedProcess | BaseException) -> None:
        self.calls: list[tuple[list[str], bytes]] = []
        self.replies = list(replies)

    def __call__(self, argv, stdin):
        self.calls.append((list(argv), stdin))
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply
        return done(0)


def done(code: int, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["security"], code, stdout, stderr)


def cli(*replies, account: str = "admin") -> tuple[kc.SecurityCLI, Recorder]:
    recorder = Recorder(*replies)
    return kc.SecurityCLI(account, runner=recorder), recorder


# --- the item's JSON: render_item, item_fp, the OQ-B fixture -------------------------------


def test_render_item_matches_the_one_oq_b_fixture(capsys, tmp_path):
    """The field set is pinned by tests/fixtures/keychain-item.json: answer
    OQ-B by editing ITEM_TEMPLATE and that file together."""
    token = synthetic_token()
    expected = json.loads(FIXTURE.read_text())
    expected[kc.ITEM_KEY][kc.TOKEN_KEY] = token
    assert json.loads(kc.render_item(token)) == expected
    shape = json.loads(FIXTURE.read_text())
    assert shape[kc.ITEM_KEY].pop(kc.TOKEN_KEY) == "<TOKEN>"
    template = json.loads(json.dumps(kc.ITEM_TEMPLATE))
    assert template[kc.ITEM_KEY].pop(kc.TOKEN_KEY) is None
    assert template == shape
    assert_absent(token, capsys, tmp_path)


def test_render_item_does_not_mutate_the_template():
    before = json.dumps(kc.ITEM_TEMPLATE, sort_keys=True)
    kc.render_item(synthetic_token())
    assert json.dumps(kc.ITEM_TEMPLATE, sort_keys=True) == before


def test_render_then_item_fp_round_trips_to_the_registry_fingerprint(capsys, tmp_path):
    token = synthetic_token()
    blob = kc.render_item(token)
    assert isinstance(blob, bytes)
    assert kc.item_fp(blob) == fingerprint(token)
    assert kc.item_fp(kc.render_item(synthetic_token())) != fingerprint(token)
    assert_absent(token, capsys, tmp_path)


def test_item_fp_reads_an_item_with_more_fields_than_ours():
    token = synthetic_token()
    blob = json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": "r",
                                         "expiresAt": 1, "scopes": ["a", "b"],
                                         "subscriptionType": "max", "extra": True},
                       "other": {}}).encode()
    assert kc.item_fp(blob) == fingerprint(token)


@pytest.mark.parametrize("oauth", [{}, {"accessToken": None}, {"accessToken": ""},
                                   {"accessToken": 7}])
def test_item_fp_is_none_when_claudeaioauth_holds_no_token(oauth):
    assert kc.item_fp(json.dumps({"claudeAiOauth": oauth}).encode()) is None


@pytest.mark.parametrize("blob", [
    b"",
    b"not json",
    b"\xff\xfe",
    b"[]",
    b"null",
    b'{"accessToken": "x"}',
    b'{"claudeAiOauth": null}',
    b'{"claudeAiOauth": "sk-ant-oat01-TEST-flat"}',
    b'{"claudeAiOauth": ["x"]}',
])
def test_a_blob_without_a_claudeaioauth_object_is_refused(blob):
    with pytest.raises(kc.KeychainError) as caught:
        kc.item_fp(blob)
    assert caught.value.code == kc.EXIT_KEYCHAIN
    with pytest.raises(kc.KeychainError):
        kc.check_item(blob)


def test_a_refused_blob_is_never_quoted_in_the_error():
    token = synthetic_token()
    for blob in (f'{{"accessToken": "{token}"}}'.encode(),
                 f'{{"claudeAiOauth": "{token}"}}'.encode(),
                 f'{{"claudeAiOauth": {{"accessToken": "{token}"'.encode()):
        with pytest.raises(kc.KeychainError) as caught:
            kc.item_fp(blob)
        assert token not in str(caught.value)
        error = caught.value      # no chained exception may carry the content either
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__


def test_parse_item_refuses_a_str():
    with pytest.raises(kc.KeychainError, match="must be bytes"):
        kc.parse_item('{"claudeAiOauth": {}}')


@pytest.mark.parametrize("bad", ["", " sk-ant-oat01-TEST-x", "sk-ant-oat01-TEST-x\n", None, 3])
def test_render_item_refuses_an_empty_or_padded_token_without_showing_it(bad):
    with pytest.raises(kc.KeychainError) as caught:
        kc.render_item(bad)
    assert "TEST-x" not in str(caught.value)


# --- SecurityCLI: argv, stdin, never a secret in argv ---------------------------------------


def test_write_runs_security_dash_i_and_puts_the_blob_on_stdin_only(capsys, tmp_path):
    token = synthetic_token()
    blob = kc.render_item(token)
    keychain, recorder = cli()
    keychain.write(blob)
    assert len(recorder.calls) == 1
    argv, stdin = recorder.calls[0]
    assert argv == [kc.SECURITY, "-i"]
    for arg in argv:
        assert token not in arg
        assert blob.hex() not in arg and token.encode().hex() not in arg
    assert stdin == (b'add-generic-password -U -s "Claude Code-credentials" -a "admin" -X '
                     + blob.hex().encode() + b"\n")
    hex_arg = stdin.decode().split()[-1]
    assert bytes.fromhex(hex_arg) == blob
    assert token.encode() not in stdin            # only its hex rides stdin
    assert_absent(token, capsys, tmp_path)


def test_read_runs_find_generic_password_with_w_and_no_stdin_secret(capsys, tmp_path):
    token = synthetic_token()
    blob = kc.render_item(token)
    keychain, recorder = cli(done(0, blob + b"\n"))
    assert keychain.read() == blob
    argv, stdin = recorder.calls[0]
    assert argv == [kc.SECURITY, "find-generic-password", "-s", "Claude Code-credentials",
                    "-a", "admin", "-w"]
    assert stdin == b""
    assert kc.item_fp(blob) == fingerprint(token)
    assert_absent(token, capsys, tmp_path)


def test_read_decodes_the_hex_security_prints_for_non_text_data():
    blob = kc.render_item(synthetic_token())
    keychain, _ = cli(done(0, blob.hex().encode() + b"\n"))
    assert keychain.read() == blob


def test_read_returns_odd_length_text_as_it_is():
    keychain, _ = cli(done(0, b"abc\n"))
    assert keychain.read() == b"abc"


def test_no_argv_either_verb_builds_ever_carries_the_secret():
    token = synthetic_token()
    blob = kc.render_item(token)
    keychain, recorder = cli(done(0, blob))
    keychain.read()
    keychain.write(blob)
    assert [argv for argv, _ in recorder.calls] == [keychain.read_argv(), keychain.write_argv()]
    flat = " ".join(" ".join(argv) for argv, _ in recorder.calls)
    assert token not in flat and blob.hex() not in flat and "-X" not in flat


def test_the_account_defaults_to_user_read_when_built(monkeypatch):
    monkeypatch.setenv("USER", "admin")
    assert kc.SecurityCLI(runner=Recorder()).account == "admin"
    monkeypatch.setenv("USER", "alice")
    keychain = kc.SecurityCLI(runner=Recorder())
    assert keychain.account == "alice"
    assert keychain.read_argv()[5] == "alice"
    assert kc.SecurityCLI("admin", runner=Recorder()).account == "admin"


def test_no_user_and_no_account_is_refused(monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    with pytest.raises(kc.KeychainError, match="USER is unset"):
        kc.SecurityCLI(runner=Recorder())


@pytest.mark.parametrize("account", ['ad"min', "ad min", "a\nb", "a\\b", "", "x" * 65])
def test_an_account_the_stdin_line_could_not_quote_safely_is_refused(account):
    with pytest.raises(kc.KeychainError, match="refused"):
        kc.SecurityCLI(account, runner=Recorder())


@pytest.mark.parametrize("service", ['a"b', "a\nb", "a\\b", ""])
def test_a_service_the_stdin_line_could_not_quote_safely_is_refused(service):
    with pytest.raises(kc.KeychainError, match="refused"):
        kc.SecurityCLI("admin", service=service, runner=Recorder())


def test_describe_and_repr_name_the_item_never_its_content():
    keychain, _ = cli()
    assert keychain.describe() == "Keychain item 'Claude Code-credentials' (account admin)"
    assert "admin" in repr(keychain)
    assert isinstance(keychain, kc.Keychain)
    assert isinstance(kc.FakeKeychain(), kc.Keychain)


# --- SecurityCLI: failures never leak --------------------------------------------------------


def test_a_missing_item_is_named_as_missing():
    keychain, _ = cli(done(44, b"", b"The specified item could not be found in the keychain.\n"))
    with pytest.raises(kc.KeychainError, match="no such item") as caught:
        keychain.read()
    assert caught.value.code == kc.EXIT_KEYCHAIN


def test_a_failed_read_names_the_exit_and_hides_security_output():
    keychain, _ = cli(done(51, b"sk-ant-oat01-TEST-stdout", b"sk-ant-oat01-TEST-stderr"))
    with pytest.raises(kc.KeychainError, match="exited 51") as caught:
        keychain.read()
    assert "TEST-std" not in str(caught.value)


def test_an_empty_read_is_refused():
    keychain, _ = cli(done(0, b"\n"))
    with pytest.raises(kc.KeychainError, match="empty"):
        keychain.read()


def test_write_refuses_a_blob_without_claudeaioauth_and_runs_nothing():
    keychain, recorder = cli()
    for blob in (b"{}", b'{"accessToken": "sk-ant-oat01-TEST-x"}', b"junk"):
        with pytest.raises(kc.KeychainError) as caught:
            keychain.write(blob)
        assert caught.value.code == kc.EXIT_WRITE
        assert "TEST-x" not in str(caught.value)
    assert recorder.calls == []


@pytest.mark.parametrize("reply", [done(1), done(0, b"", b"security: SecKeychainItemCreate"
                                                         b"FromContent: 7b2263...\n")])
def test_a_failed_write_raises_exit_6_and_never_shows_securitys_output(reply, capsys, tmp_path):
    token = synthetic_token()
    blob = kc.render_item(token)
    reply.stderr = (reply.stderr or b"") + blob.hex().encode()
    keychain, _ = cli(reply)
    with pytest.raises(kc.KeychainError) as caught:
        keychain.write(blob)
    assert caught.value.code == kc.EXIT_WRITE
    message = str(caught.value)
    assert token not in message and blob.hex() not in message and "7b2263" not in message
    assert_absent(token, capsys, tmp_path)


@pytest.mark.parametrize("error, code", [
    (subprocess.TimeoutExpired(["security", "-i"], 30, output=b"x"), "timed out"),
    (FileNotFoundError(2, "No such file or directory"), "could not run"),
])
def test_a_runner_that_cannot_run_is_a_keychain_error(error, code):
    for verb, exit_code in (("read", kc.EXIT_KEYCHAIN), ("write", kc.EXIT_WRITE)):
        keychain, _ = cli(error)
        with pytest.raises(kc.KeychainError, match=code) as caught:
            if verb == "read":
                keychain.read()
            else:
                keychain.write(kc.render_item(synthetic_token()))
        assert caught.value.code == exit_code
        assert caught.value.__suppress_context__


def test_the_default_runner_is_stopped_by_the_guard_before_security_runs(monkeypatch):
    """No test runs the real binary: a SecurityCLI left on its default
    runner reaches subprocess.run, and the autouse guard raises there."""
    monkeypatch.setenv("USER", "admin")
    keychain = kc.SecurityCLI()
    assert keychain.runner is kc.run_security
    with pytest.raises(AssertionError, match="test tried to run 'security'"):
        keychain.read()
    with pytest.raises(AssertionError, match="test tried to run 'security'"):
        keychain.write(kc.render_item(synthetic_token()))


# --- FakeKeychain -----------------------------------------------------------------------------


def test_fake_round_trips_a_rendered_item(capsys, tmp_path):
    token = synthetic_token()
    fake = kc.FakeKeychain()
    with pytest.raises(kc.KeychainError, match="no such item"):
        fake.read()
    fake.write(kc.render_item(token))
    assert kc.item_fp(fake.read()) == fingerprint(token)
    assert fake.writes == [kc.render_item(token)]
    assert token not in repr(fake)
    assert_absent(token, capsys, tmp_path)


def test_fake_starts_from_a_given_item_and_replaces_it():
    first, second = synthetic_token(), synthetic_token()
    fake = kc.FakeKeychain(kc.render_item(first))
    assert kc.item_fp(fake.read()) == fingerprint(first)
    fake.write(kc.render_item(second))
    assert kc.item_fp(fake.read()) == fingerprint(second)
    assert fake.reads == 2


def test_fake_refuses_a_blob_without_claudeaioauth_and_keeps_the_old_item():
    old = kc.render_item(synthetic_token())
    fake = kc.FakeKeychain(old)
    with pytest.raises(kc.KeychainError) as caught:
        fake.write(b'{"nope": {}}')
    assert caught.value.code == kc.EXIT_WRITE
    assert fake.read() == old and fake.writes == []


def test_fake_failure_knobs():
    old = kc.render_item(synthetic_token())
    with pytest.raises(kc.KeychainError) as caught:
        kc.FakeKeychain(old, fail_read=True).read()
    assert caught.value.code == kc.EXIT_KEYCHAIN
    fake = kc.FakeKeychain(old, fail_write=True)
    with pytest.raises(kc.KeychainError) as caught:
        fake.write(kc.render_item(synthetic_token()))
    assert caught.value.code == kc.EXIT_WRITE
    assert fake.read() == old and fake.writes == []


def test_fake_mangle_makes_a_read_back_verify_fail():
    other = kc.render_item(synthetic_token())
    fake = kc.FakeKeychain(mangle=lambda blob: other)
    wanted = synthetic_token()
    fake.write(kc.render_item(wanted))
    assert fake.writes == [kc.render_item(wanted)]
    assert kc.item_fp(fake.read()) != fingerprint(wanted)



# --- review (do-002): the one `security -i` line must fit security's buffer ---------------


def _item_of_size(token: str, size: int) -> bytes:
    """A valid item of exactly `size` bytes, padded in a field of its own."""
    base = json.dumps({"claudeAiOauth": {"accessToken": token}, "pad": ""},
                      separators=(",", ":")).encode()
    blob = json.dumps({"claudeAiOauth": {"accessToken": token}, "pad": "x" * (size - len(base))},
                      separators=(",", ":")).encode()
    assert len(blob) == size
    return blob


def test_a_line_security_reads_whole_is_written(capsys, tmp_path):
    token = synthetic_token()
    keychain, recorder = cli()
    head = len(keychain.write_script(b""))   # the line around an empty hex
    biggest = (kc.MAX_LINE - head) // 2
    blob = _item_of_size(token, biggest)
    keychain.write(blob)
    assert len(recorder.calls[0][1]) <= kc.MAX_LINE
    assert bytes.fromhex(recorder.calls[0][1].decode().split()[-1]) == blob
    assert_absent(token, capsys, tmp_path)


def test_an_item_too_long_for_one_security_line_is_refused_and_nothing_runs(capsys, tmp_path):
    """security -i reads 4096 bytes a line (MAX_LINE_LEN, Apple's
    security.c): a longer line would write a truncated item and run its
    tail as a command. A backed-up item with an mcpOAuth map can be that
    big, so the restore path meets this too."""
    token = synthetic_token()
    keychain, recorder = cli()
    head = len(keychain.write_script(b""))
    blob = _item_of_size(token, (kc.MAX_LINE - head) // 2 + 1)
    with pytest.raises(kc.KeychainError, match="over the 4095") as caught:
        keychain.write(blob)
    assert caught.value.code == kc.EXIT_WRITE
    assert recorder.calls == []
    message = str(caught.value)
    assert token not in message and blob.hex() not in message
    assert_absent(token, capsys, tmp_path)
