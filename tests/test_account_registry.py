"""The account registry (ST-22 design-002 §2, do-001).

HARD LIMITS (by design): no Keychain read or write, no real
account switch, no /login, and no run of `security`, `pass` or `claude`.
The autouse fixture below makes any subprocess naming one of them raise,
gives every test an empty CLAUDE_CONFIG_DIR, and the conftest's tmp HOME
holds. Every token is synthetic (`sk-ant-oat01-TEST-<random>`), and each
test that handles one asserts it is absent from all captured output and
from every file the test wrote.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets as pysecrets
import subprocess
from pathlib import Path

import pytest

from sessiontools.account import registry as reg
from sessiontools.start import secrets as start_secrets
from sessiontools.start.secrets import SecretError

FORBIDDEN = ("security", "pass", "claude")


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


def synthetic_token() -> str:
    return "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)


def assert_absent(token: str, capsys, *roots: Path) -> None:
    """The token is in no captured output and in no file under any root."""
    out, err = capsys.readouterr()
    assert token not in out and token not in err
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file():
                assert token.encode() not in path.read_bytes(), f"token leaked into {path}"


def private_file(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, mode)
    return path


def three() -> reg.Registry:
    r = reg.Registry()
    r.add(reg.make_account("a", "ai@example.com",
                           ["file:~/.config/tokens/oauth-token-ai@example.com"]))
    r.add(reg.make_account("c", "carol@example.com",
                           ["file:~/.config/tokens/oauth-token-carol@example.com",
                            "pass:anthropic/oauth-tokens/carol@example.com"],
                           fp="0123456789abcdef"))
    r.add(reg.make_account("p", "peggy@example.com",
                           ["pass:anthropic/oauth-tokens/peggy@example.com"]))
    return r


# --- the path ------------------------------------------------------------------


def test_the_default_path_is_under_the_kits_home_and_follows_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert reg.default_path() == tmp_path / ".sessionkit/tools/accounts/accounts.json"
    assert reg.default_path(tmp_path / "x") == tmp_path / "x/.sessionkit/tools/accounts/accounts.json"


def test_load_and_save_default_to_the_default_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert reg.save(three()) == reg.default_path()
    assert reg.exists()
    assert reg.load().names() == ["a", "c", "p"]


# --- round trip, mode, atomicity ------------------------------------------------------


def test_round_trip_keeps_order_and_every_field(tmp_path):
    path = tmp_path / "cfg" / "accounts.json"
    original = three()
    reg.save(original, path)
    loaded = reg.load(path)
    assert loaded.names() == ["a", "c", "p"]
    assert loaded.to_json() == original.to_json()
    assert loaded.get("c").fp == "0123456789abcdef"
    assert [str(s) for s in loaded.get("c").sources] == [
        "file:~/.config/tokens/oauth-token-carol@example.com",
        "pass:anthropic/oauth-tokens/carol@example.com"]


def test_order_is_kept_whatever_it_is(tmp_path):
    path = tmp_path / "accounts.json"
    r = reg.Registry()
    for name in ("p", "a", "c"):
        r.add(reg.make_account(name, f"{name}@x.com", [f"pass:{name}"]))
    reg.save(r, path)
    assert reg.load(path).names() == ["p", "a", "c"]
    on_disk = json.loads(path.read_text())
    assert [row["name"] for row in on_disk["accounts"]] == ["p", "a", "c"]


def test_the_file_is_0600_and_a_new_parent_is_0700(tmp_path):
    path = tmp_path / "new" / "dir" / "accounts.json"
    reg.save(three(), path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_the_file_is_0600_even_under_a_permissive_umask(tmp_path):
    old = os.umask(0)
    try:
        path = tmp_path / "accounts.json"
        reg.save(three(), path)
        reg.save(three(), path)          # the replace path, too
    finally:
        os.umask(old)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_the_on_disk_shape_is_the_designs(tmp_path):
    path = tmp_path / "accounts.json"
    reg.save(three(), path)
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["accounts"][0] == {
        "name": "a", "email": "ai@example.com",
        "sources": ["file:~/.config/tokens/oauth-token-ai@example.com"], "fp": None}


def test_save_is_a_rename_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    reg.save(three(), path)
    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src).parent, Path(dst)))
        return real(src, dst)

    monkeypatch.setattr(reg.os, "replace", spy)
    reg.save(three(), path)
    assert seen == [(tmp_path, path)]               # temp file in the SAME dir
    assert sorted(p.name for p in tmp_path.iterdir()) == ["accounts.json"]


def test_a_failed_write_leaves_the_old_file_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    reg.save(three(), path)
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(reg.os, "replace", boom)
    r = three()
    r.remove("p")
    with pytest.raises(OSError, match="disk full"):
        reg.save(r, path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["accounts.json"]


def test_a_missing_registry_is_empty_or_refused_when_asked(tmp_path):
    path = tmp_path / "nope.json"
    assert reg.load(path).accounts == []
    assert not reg.exists(path)
    with pytest.raises(reg.RegistryError) as caught:
        reg.load(path, missing_ok=False)
    assert caught.value.code == 5 and str(path) in str(caught.value)
    assert not path.exists()                         # load never writes


# --- refusals: never rewritten ------------------------------------------------------


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o400, 0o700, 0o666])
def test_a_wrong_mode_is_refused_exit_5_and_never_rewritten(tmp_path, mode):
    path = tmp_path / "accounts.json"
    reg.save(three(), path)
    os.chmod(path, mode)
    before = path.read_bytes()
    with pytest.raises(reg.RegistryError) as caught:
        reg.load(path)
    assert caught.value.code == 5
    assert f"{mode:04o}" in str(caught.value) and str(path) in str(caught.value)
    if mode & 0o200:                                 # writable by us: save must still refuse
        with pytest.raises(reg.RegistryError) as again:
            reg.save(reg.Registry(), path)
        assert again.value.code == 5
    assert path.read_bytes() == before
    assert os.stat(path).st_mode & 0o777 == mode


@pytest.mark.parametrize("text", [
    "{not json",
    "",
    "[]",
    '{"version": 2, "accounts": []}',
    '{"version": true, "accounts": []}',
    '{"version": 1.0, "accounts": []}',
    '{"version": 1}',
    '{"version": 1, "accounts": {}}',
    '{"version": 1, "accounts": [], "extra": 1}',
    '{"version": 1, "accounts": ["a"]}',
    '{"version": 1, "accounts": [{"name": "a", "email": "a@x", "sources": "pass:a"}]}',
    '{"version": 1, "accounts": [{"name": "a", "email": "a@x", "sources": []}]}',
    '{"version": 1, "accounts": [{"name": "a", "email": "a@x", "sources": ["pass:a"], "token": 1}]}',
    '{"version": 1, "accounts": [{"name": "a", "email": "a@x", "sources": ["nope:a"]}]}',
    '{"version": 1, "accounts": [{"name": "a", "email": "a@x", "sources": ["pass:a"], "fp": "XYZ"}]}',
    '{"version": 1, "accounts": [{"name": "a b", "email": "a@x", "sources": ["pass:a"]}]}',
    '{"version": 1, "accounts": [{"name": "a\\n", "email": "a@x", "sources": ["pass:a"]}]}',
    ('{"version": 1, "accounts": [{"name": "a", "email": "a@x", "sources": ["pass:a"],'
     ' "fp": "0123456789abcdef\\n"}]}'),
    '{"version": 1, "accounts": [{"name": "a", "email": "no-at", "sources": ["pass:a"]}]}',
    ('{"version": 1, "accounts": [{"name": "a", "email": "a@x", "sources": ["pass:a"]},'
     ' {"name": "a", "email": "b@x", "sources": ["pass:b"]}]}'),
])
def test_bad_content_is_refused_exit_5_and_never_rewritten(tmp_path, text):
    path = private_file(tmp_path / "accounts.json", text)
    before = path.read_bytes()
    with pytest.raises(reg.RegistryError) as caught:
        reg.load(path)
    assert caught.value.code == 5 and str(path) in str(caught.value)
    with pytest.raises(reg.RegistryError) as again:
        reg.save(three(), path)
    assert again.value.code == 5
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["accounts.json"]


def test_bad_json_names_the_line_and_column(tmp_path):
    path = private_file(tmp_path / "accounts.json", '{\n  "version": 1,\n  oops\n}')
    with pytest.raises(reg.RegistryError, match=r"not valid JSON \(line 3, column 3\)"):
        reg.load(path)


def test_a_directory_or_a_dangling_link_is_refused(tmp_path):
    folder = tmp_path / "accounts.json"
    folder.mkdir()
    with pytest.raises(reg.RegistryError, match="not a regular file") as caught:
        reg.load(folder)
    assert caught.value.code == 5
    link = tmp_path / "dangling.json"
    link.symlink_to(tmp_path / "missing")
    with pytest.raises(reg.RegistryError, match="no such file"):
        reg.load(link)
    with pytest.raises(reg.RegistryError):
        reg.save(three(), link)
    assert link.is_symlink() and not (tmp_path / "missing").exists()


# --- names, never tokens ------------------------------------------------------------


def test_a_token_pasted_as_a_source_is_refused_and_not_echoed(capsys, tmp_path):
    token = synthetic_token()
    for text in (token, f"file:{token}", f"pass:x/{token}"):
        with pytest.raises(reg.RegistryError) as caught:
            reg.parse_source(text)
        assert token not in str(caught.value) and token not in repr(caught.value)
        assert caught.value.code == 2
    with pytest.raises(reg.RegistryError) as caught:
        reg.make_account("c", "carol@example.com", [f"pass:{token}"])
    assert token not in str(caught.value)
    assert_absent(token, capsys, tmp_path)


def test_a_registry_file_holding_a_token_is_refused_and_not_echoed(capsys, tmp_path):
    token = synthetic_token()
    data = {"version": 1, "accounts": [
        {"name": "c", "email": "c@x.com", "sources": [f"pass:{token}"], "fp": None}]}
    path = private_file(tmp_path / "accounts.json", json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(reg.RegistryError) as caught:
        reg.load(path)
    assert caught.value.code == 5 and "token-shaped" in str(caught.value)
    assert token not in str(caught.value)
    assert path.read_bytes() == before
    out, err = capsys.readouterr()
    assert token not in out and token not in err


def test_no_token_value_is_ever_stored(capsys, tmp_path, monkeypatch):
    """Register, resolve and fingerprint a real-shaped token: the file
    holds the path and the fp, never the value."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    token = synthetic_token()
    private_file(tmp_path / "home/.config/tokens/oauth-token-c@x.com", token + "\n")
    account = reg.make_account("c", "c@x.com", ["file:~/.config/tokens/oauth-token-c@x.com"])
    resolved = reg.resolve(account)
    assert resolved.token == token
    assert token not in repr(resolved)
    r = reg.Registry()
    r.add(reg.make_account(account.name, account.email, account.sources,
                           fp=reg.fingerprint(resolved.token)))
    written = reg.save(r, tmp_path / "out" / "accounts.json")
    text = written.read_text()
    assert reg.fingerprint(token) in text
    assert "oauth-token-c@x.com" in text
    # the token file itself is the one place it lives; every OTHER file is clean
    assert_absent(token, capsys, tmp_path / "out")


# --- the fingerprint ----------------------------------------------------------------


def test_fp_is_the_first_16_hex_of_sha256():
    token = synthetic_token()
    fp = reg.fingerprint(token)
    assert fp == hashlib.sha256(token.encode()).hexdigest()[:16]
    assert len(fp) == 16 and int(fp, 16) >= 0 and fp == fp.lower()
    assert reg.fingerprint(token) == fp                       # stable
    assert reg.fingerprint(synthetic_token()) != fp           # distinct


def test_by_fp_names_the_account_or_none_never_a_guess():
    r = three()
    assert r.by_fp("0123456789abcdef").name == "c"
    assert r.by_fp("fedcba9876543210") is None
    assert r.by_fp(None) is None                 # a and p have no fp: None never matches them


# --- registry mutators ----------------------------------------------------------------


def test_get_names_the_known_names_and_exits_2():
    with pytest.raises(reg.RegistryError) as caught:
        three().get("z")
    assert caught.value.code == 2 and "a, c, p" in str(caught.value)
    with pytest.raises(reg.RegistryError, match="none registered"):
        reg.Registry().get("z")


def test_add_refuses_a_duplicate_and_remove_and_replace_keep_order():
    r = three()
    with pytest.raises(reg.RegistryError) as caught:
        r.add(reg.make_account("c", "other@x.com", ["pass:x"]))
    assert caught.value.code == 2
    assert r.names() == ["a", "c", "p"]
    r.replace(reg.make_account("c", "carol@example.com", ["pass:y"], fp="ffffffffffffffff"))
    assert r.names() == ["a", "c", "p"] and r.get("c").fp == "ffffffffffffffff"
    assert r.remove("c").name == "c"
    assert r.names() == ["a", "p"]
    with pytest.raises(reg.RegistryError):
        r.remove("c")


@pytest.mark.parametrize("name", ["", "a b", "-a", "a/b", "x" * 33, None, 3, "a\n", "c\n"])
def test_a_bad_name_is_refused(name):
    with pytest.raises(reg.RegistryError) as caught:
        reg.make_account(name, "a@x.com", ["pass:a"])
    assert caught.value.code == 2


@pytest.mark.parametrize("fp", ["", "0123", "0123456789ABCDEF", "0123456789abcdeg",
                                "0123456789abcdef0", 12, "0123456789abcdef\n"])
def test_a_bad_fp_is_refused(fp):
    with pytest.raises(reg.RegistryError):
        reg.make_account("a", "a@x.com", ["pass:a"], fp=fp)


# --- sources ------------------------------------------------------------------------


def test_parse_source_splits_at_the_first_colon():
    assert reg.parse_source("pass:a/b:c") == reg.Source("pass", "a/b:c")
    assert str(reg.parse_source("file:~/x")) == "file:~/x"


@pytest.mark.parametrize("text", ["pass", "pass:", ":ref", "", "keychain:x", "PASS:x", 7])
def test_parse_source_refuses_a_bad_source_exit_2(text):
    with pytest.raises(reg.RegistryError) as caught:
        reg.parse_source(text)
    assert caught.value.code == 2


def test_the_backends_are_file_and_session_starts_own_pass():
    assert set(reg.BACKENDS) == {"file", "pass", "passage"}
    assert reg.BACKENDS["pass"] is start_secrets.pass_backend
    assert reg.COST == {"file": "free", "pass": "key touch", "passage": "touch"}


# --- the file backend ----------------------------------------------------------------


def test_the_file_backend_reads_the_first_line_of_a_private_file(tmp_path, capsys):
    token = synthetic_token()
    path = private_file(tmp_path / "tok", f"  {token}  \nnotes\n")
    assert reg.file_backend(str(path)) == token
    assert_absent(token, capsys)


def test_the_file_backend_expands_the_tilde(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    token = synthetic_token()
    private_file(tmp_path / ".config/tokens/oauth-token-a@x.com", token)
    assert reg.file_backend("~/.config/tokens/oauth-token-a@x.com") == token
    assert_absent(token, capsys)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o700, 0o400])
def test_the_file_backend_refuses_a_non_private_file_without_the_value(tmp_path, mode, capsys):
    token = synthetic_token()
    path = private_file(tmp_path / "tok", token, mode)
    with pytest.raises(SecretError) as caught:
        reg.file_backend(str(path))
    assert "refused" in str(caught.value) and f"{mode:04o}" in str(caught.value)
    assert token not in str(caught.value)
    assert_absent(token, capsys)


def test_the_file_backend_refuses_missing_dir_and_empty(tmp_path):
    with pytest.raises(SecretError, match="no such file"):
        reg.file_backend(str(tmp_path / "missing"))
    (tmp_path / "d").mkdir()
    with pytest.raises(SecretError, match="not a regular file"):
        reg.file_backend(str(tmp_path / "d"))
    empty = private_file(tmp_path / "empty", "\n\n")
    with pytest.raises(SecretError, match="is empty"):
        reg.file_backend(str(empty))


def test_the_file_backend_refuses_a_file_owned_by_someone_else(tmp_path, monkeypatch):
    path = private_file(tmp_path / "tok", synthetic_token())
    monkeypatch.setattr(reg.os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(SecretError, match="not owned by this user"):
        reg.file_backend(str(path))


def test_the_file_backend_reuses_start_secrets_is_private(tmp_path, monkeypatch):
    path = private_file(tmp_path / "tok", synthetic_token())
    asked = []
    monkeypatch.setattr(reg, "is_private", lambda p: asked.append(p) or False)
    with pytest.raises(SecretError):
        reg.file_backend(str(path))
    assert asked == [str(path)]
    assert reg.is_private is not start_secrets.is_private      # patched here...
    monkeypatch.undo()
    assert reg.is_private is start_secrets.is_private          # ...the same function


def test_a_symlink_to_a_private_file_is_followed(tmp_path, capsys):
    token = synthetic_token()
    real = private_file(tmp_path / "real", token)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert reg.file_backend(str(link)) == token
    assert_absent(token, capsys)


# --- resolve: first source wins, in memory -------------------------------------------


def test_resolve_takes_the_first_source_that_works_in_order(tmp_path, capsys):
    token = synthetic_token()
    calls = []

    def fake_pass(ref):
        calls.append(ref)
        return token

    tok = private_file(tmp_path / "tok", token)
    account = reg.make_account("c", "c@x.com", [f"file:{tok}", "pass:c"])
    got = reg.resolve(account, {"file": reg.file_backend, "pass": fake_pass})
    assert (got.account, str(got.source), got.token) == ("c", f"file:{tok}", token)
    assert calls == []                               # the cheaper source won; pass untouched
    os.chmod(tok, 0o644)                             # the file is now refused...
    got = reg.resolve(account, {"file": reg.file_backend, "pass": fake_pass})
    assert str(got.source) == "pass:c" and calls == ["c"]   # ...so pass is next
    assert token not in repr(got)
    assert_absent(token, capsys, tmp_path / "nothing-else")


def test_resolve_failing_everywhere_is_exit_4_naming_each_source(tmp_path, capsys):
    token = synthetic_token()
    tok = private_file(tmp_path / "tok", token, 0o644)

    def no_pass(ref):
        raise SecretError("`pass` is not installed")

    account = reg.make_account("p", "p@x.com", [f"file:{tok}", "pass:p"])
    with pytest.raises(reg.RegistryError) as caught:
        reg.resolve(account, {"file": reg.file_backend, "pass": no_pass})
    message = str(caught.value)
    assert caught.value.code == 4
    assert f"file:{tok} refused: mode 0644, not 0600" in message    # the mode, by its path
    assert "pass:p: `pass` is not installed" in message
    assert token not in message
    assert_absent(token, capsys)


def test_resolve_names_a_backend_the_table_lacks(tmp_path):
    account = reg.make_account("p", "p@x.com", ["pass:p"])
    with pytest.raises(reg.RegistryError, match="pass:p: unknown backend") as caught:
        reg.resolve(account, {"file": reg.file_backend})
    assert caught.value.code == 4


def test_nothing_here_imports_a_keychain_or_runs_a_process():
    """do-001 is the registry alone: no `security`, no subprocess."""
    source = Path(reg.__file__).read_text()
    assert "subprocess" not in source and "security" not in source


# --- do-005: the review notes O1 and O3 ---------------------------------------------------


def test_a_symlinked_registry_is_refused_by_load_and_save(tmp_path):
    """do-001 review O1: `save` renames a temp file over the target, which
    would replace the link with a file; so both refuse it, exit 5."""
    real = reg.save(reg.Registry(), tmp_path / "real.json")
    link = tmp_path / "accounts.json"
    link.symlink_to(real)
    before = real.read_bytes()
    for act in (lambda: reg.load(link),
                lambda: reg.save(reg.Registry([reg.make_account("a", "a@x", ["pass:y"])]),
                                 link)):
        with pytest.raises(reg.RegistryError, match="is a symlink") as caught:
            act()
        assert caught.value.code == reg.EXIT_REGISTRY
    assert link.is_symlink() and real.read_bytes() == before


def test_the_file_backend_checks_the_file_it_opened(tmp_path, monkeypatch):
    """do-001 review O3: the path check passing is not enough; the fd that
    is read must pass the same rule (a swap between check and open)."""
    token = synthetic_token()
    loose = private_file(tmp_path / "loose", token, 0o644)
    monkeypatch.setattr(reg, "_private_check", lambda path: None)     # the race "won"
    with pytest.raises(SecretError, match="mode 0644, not 0600") as caught:
        reg.file_backend(str(loose))
    assert token not in str(caught.value)


def test_the_file_backend_does_not_hang_on_a_fifo(tmp_path, monkeypatch):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    monkeypatch.setattr(reg, "_private_check", lambda path: None)
    with pytest.raises(SecretError, match="not a regular file"):
        reg.file_backend(str(fifo))
