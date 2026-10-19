"""The host's token file (`account/tokenfile.py`) and `use.host_route`.

Every test runs in the conftest's scratch HOME, under `test_account_use`'s
guard: any subprocess naming `security`, `pass` or `claude` raises. The
token file is always `.tokens/oauth` under the scratch HOME, named
through a `session` host-layer file in that HOME, and every token is
synthetic. No real token
file is ever opened: `account.token_file` has no default, so no test can
reach a real path.
"""

from __future__ import annotations

import io
import os
import shlex
import stat
from pathlib import Path

import pytest
from settings_support import shown, write_session_settings
from test_account_use import (  # noqa: F401 - the autouse guard is imported to apply here
    World,
    _no_keychain_no_pass_no_claude,
    synthetic_token,
)

from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.account import tokenfile as tf
from sessiontools.account import use as u


@pytest.fixture
def home(tmp_path) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


def token_file(home: Path, content: str | bytes, mode: int = 0o600) -> Path:
    folder = home / ".tokens"
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / "oauth"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(mode)
    return path


def setting(home: Path, value, name: str = "session.json") -> Path:
    return write_session_settings(home, {"account": {"token_file": value}}, name)


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def no_token_in(text: str, *tokens: str) -> None:
    for token in tokens:
        assert token not in text
        assert reg.fingerprint(token) not in text
    assert "sk-ant-" not in text


# --- the setting: no default -------------------------------------------------------------


def test_there_is_no_default():
    assert tf.SETTING_KEY == "account.token_file"
    assert not any(name.startswith("DEFAULT") for name in vars(tf))


def test_unset_is_the_keychain_route_and_runs_nothing(home):
    keychain = kc.FakeKeychain(b"{}")
    assert tf.token_file_setting(home=home) is None
    assert u.host_route(home=home, keychain=keychain) is keychain
    assert keychain.reads == 0                     # the route is chosen without a read


def test_unset_with_no_keychain_given_is_the_real_item_unrun(home, monkeypatch):
    # The account is the PROCESS's $USER (keychain.default_account), not the
    # env handed in: that env only finds the settings layers. Pin it here so
    # the test does not lean on the runner's own $USER (ST-197).
    monkeypatch.setenv("USER", "someone")
    route = u.host_route({"HOME": str(home)})
    assert isinstance(route, kc.SecurityCLI)       # constructed; the guard proves no run
    assert route.account == "someone"


def test_set_and_valid_is_the_token_file_route(home):
    path = token_file(home, synthetic_token() + "\n")
    origin = setting(home, str(path))
    route = u.host_route(home=home, keychain=kc.FakeKeychain(b"{}"))
    assert isinstance(route, tf.TokenFile)
    assert route.path == path and route.target == path
    assert route.origin == str(origin)
    assert route.describe() == f"token file {path}"


def test_a_tilde_path_expands_against_the_home_the_layers_are_read_under(home):
    path = token_file(home, synthetic_token())
    setting(home, "~/.tokens/oauth")
    found, _origin = tf.token_file_setting({"HOME": "/nowhere"}, home)
    assert found == path


def test_a_later_host_file_overrides_an_earlier_one(home):
    token_file(home, synthetic_token())
    setting(home, "/first/oauth")
    setting(home, str(home / ".tokens" / "oauth"), "session.d/50-account.json")
    found, origin = tf.token_file_setting(home=home)
    assert found == home / ".tokens" / "oauth"
    assert origin.endswith("50-account.json")


def test_set_and_valid_but_missing_is_refused_never_created(home):
    missing = home / ".tokens" / "oauth"
    setting(home, str(missing))
    with pytest.raises(tf.TokenFileError) as caught:
        u.host_route(home=home)
    assert caught.value.code == 5
    assert "does not exist" in str(caught.value) and "does not create" in str(caught.value)
    assert not missing.exists()


@pytest.mark.parametrize("perm", [0o644, 0o640, 0o400])
def test_a_file_that_is_not_0600_is_refused_by_stat_alone(home, perm):
    token = synthetic_token()
    setting(home, str(token_file(home, token, perm)))
    with pytest.raises(tf.TokenFileError) as caught:
        u.host_route(home=home)
    assert caught.value.code == 5 and f"{perm:04o}" in str(caught.value)
    no_token_in(str(caught.value), token)


def test_a_fifo_or_a_directory_is_refused(home):
    folder = home / ".tokens"
    folder.mkdir(mode=0o700)
    fifo = folder / "oauth"
    os.mkfifo(fifo, 0o600)
    setting(home, str(fifo))
    with pytest.raises(tf.TokenFileError, match="not a regular file"):
        u.host_route(home=home)
    with pytest.raises(tf.TokenFileError, match="not a regular file"):
        tf.TokenFile(fifo).read()                  # O_NONBLOCK: it cannot hang
    setting(home, str(folder))
    with pytest.raises(tf.TokenFileError, match="not a regular file"):
        u.host_route(home=home)


@pytest.mark.parametrize("value, says", [
    (42, "is a int"),
    (None, None),                                  # JSON null: as if unset
    ("", "is empty"),
    ("relative/oauth", "not an absolute path"),
    ("/a\x00b", "unprintable"),
    ("/tokens/\x1b[2J", "unprintable"),
    (["/a"], "is a list"),
])
def test_a_bad_value_names_the_key_and_the_layer(home, value, says):
    origin = setting(home, value)
    if says is None:
        assert tf.token_file_setting(home=home) is None
        return
    with pytest.raises(tf.TokenFileError) as caught:
        tf.token_file_setting(home=home)
    message = str(caught.value)
    assert caught.value.code == 5
    assert says in message
    assert f"account.token_file (in {origin})" in message


def test_a_token_shaped_value_is_refused_and_never_echoed(home):
    token = synthetic_token()
    for value in (token, f"/tokens/{token}", "/x/sk-ant-short"):
        setting(home, value)
        with pytest.raises(tf.TokenFileError) as caught:
            tf.token_file_setting(home=home)
        message = str(caught.value)
        assert "looks like a token" in message and "account.token_file (in " in message
        no_token_in(message, token)
        assert value not in message


def test_a_settings_file_the_reader_refuses_is_exit_5(home):
    write_session_settings(home, "{not json")
    with pytest.raises(tf.TokenFileError) as caught:
        tf.token_file_setting(home=home)
    assert caught.value.code == 5 and str(caught.value).startswith("settings:")


# --- a symlink is followed -----------------------------------------------------------------


def test_a_link_is_followed_to_its_target(home):
    target = token_file(home, synthetic_token())
    link = home / "link-to-oauth"
    link.symlink_to(target)
    setting(home, str(link))
    route = u.host_route(home=home)
    assert route.path == link and route.target == target
    assert route.describe() == f"token file {link} (a link to {target})"


def test_a_dangling_link_is_refused_as_missing(home):
    link = home / "link-to-nothing"
    link.symlink_to(home / "gone")
    setting(home, str(link))
    with pytest.raises(tf.TokenFileError, match="does not exist"):
        u.host_route(home=home)


# --- the one-token-line rule ---------------------------------------------------------------


def test_one_token_line_with_or_without_a_newline_reads():
    token = synthetic_token()
    assert tf.parse_token_line((token + "\n").encode()) == token
    assert tf.parse_token_line(token.encode()) == token


@pytest.mark.parametrize("shape", [
    "{t}\n{t}\n", "{t}\nexport X=1\n", "{t}\n\n", "\n{t}\n", " {t}\n", "{t} \n",
    "{t}\r\n", "\t{t}", "", "\n", "{t}\x07\n",
])
def test_anything_but_one_bare_token_line_is_refused_unquoted(shape):
    token = synthetic_token()
    blob = shape.format(t=token).encode()
    with pytest.raises(tf.TokenFileError) as caught:
        tf.parse_token_line(blob)
    assert caught.value.code == 5
    no_token_in(str(caught.value), token)


def test_not_utf8_and_too_long_are_refused():
    with pytest.raises(tf.TokenFileError, match="not UTF-8"):
        tf.parse_token_line(b"\xff\xfe\n")
    with pytest.raises(tf.TokenFileError, match="too long"):
        tf.parse_token_line(b"a" * (tf.MAX_BYTES + 1))


def test_read_gives_the_token_and_read_bytes_the_file(home):
    token = synthetic_token()
    path = token_file(home, token + "\n")
    file = tf.TokenFile(path)
    assert file.read() == token
    assert file.read_bytes() == (token + "\n").encode()


def test_read_refuses_a_two_line_file_without_quoting_it(home):
    token = synthetic_token()
    file = tf.TokenFile(token_file(home, f"{token}\n{token}\n"))
    with pytest.raises(tf.TokenFileError, match="more than one line") as caught:
        file.read()
    no_token_in(str(caught.value), token)


# --- stage, check_staged, commit, discard --------------------------------------------------


def staged_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if tf.STAGE_INFIX in p.name)


def test_stage_writes_0600_beside_the_target_and_leaves_the_target(home):
    old, new = synthetic_token(), synthetic_token()
    path = token_file(home, old + "\n")
    file = tf.TokenFile(path)
    staged = file.stage(new)
    assert staged.parent == path.parent
    assert staged.name.startswith(".oauth" + tf.STAGE_INFIX)
    assert mode(staged) == 0o600
    assert staged.read_bytes() == (new + "\n").encode()
    assert path.read_bytes() == (old + "\n").encode()
    file.check_staged(staged, new)


def test_stage_refuses_a_token_that_is_not_one_bare_line(home):
    file = tf.TokenFile(token_file(home, synthetic_token()))
    for bad in ("", " x", "a\nb"):
        with pytest.raises(tf.TokenFileError):
            file.stage(bad)
    assert staged_files(home / ".tokens") == []


def test_check_staged_refuses_another_token_unquoted(home):
    new, other = synthetic_token(), synthetic_token()
    file = tf.TokenFile(token_file(home, synthetic_token()))
    staged = file.stage(other)
    with pytest.raises(tf.TokenFileError, match="did not read back") as caught:
        file.check_staged(staged, new)
    no_token_in(str(caught.value), new, other)


def test_check_staged_refuses_a_staged_file_that_is_not_private(home):
    new = synthetic_token()
    file = tf.TokenFile(token_file(home, synthetic_token()))
    staged = file.stage(new)
    staged.chmod(0o644)
    with pytest.raises(tf.TokenFileError, match="did not read back"):
        file.check_staged(staged, new)


def test_commit_replaces_the_target_atomically_and_syncs_the_directory(home, monkeypatch):
    old, new = synthetic_token(), synthetic_token()
    path = token_file(home, old + "\n")
    before = os.stat(path).st_ino
    file = tf.TokenFile(path)
    staged = file.stage(new)
    calls = []
    real_replace, real_fsync = os.replace, os.fsync
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append(("replace", Path(a), Path(b))),
                                                     real_replace(a, b))[1])

    def fsync(fd):
        calls.append(("fsync", stat.S_ISDIR(os.fstat(fd).st_mode)))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    file.commit(staged)
    assert calls == [("replace", staged, path), ("fsync", True)]
    assert file.read() == new
    assert mode(path) == 0o600 and os.stat(path).st_ino != before
    assert not staged.exists() and staged_files(path.parent) == []


def test_commit_through_a_link_keeps_the_link(home):
    old, new = synthetic_token(), synthetic_token()
    target = token_file(home, old)
    link = home / "link-to-oauth"
    link.symlink_to(target)
    file = tf.TokenFile(link)
    staged = file.stage(new)
    assert staged.parent == target.parent
    file.commit(staged)
    assert link.is_symlink() and os.readlink(link) == str(target)
    assert tf.TokenFile(link).read() == new and target.read_text() == new + "\n"


def test_commit_refuses_a_file_staged_elsewhere(home, tmp_path):
    path = token_file(home, synthetic_token())
    stray = tmp_path / "elsewhere"
    stray.write_text(synthetic_token())
    with pytest.raises(tf.TokenFileError, match="not beside"):
        tf.TokenFile(path).commit(stray)
    assert stray.exists()


def test_discard_removes_and_never_raises(home, monkeypatch):
    file = tf.TokenFile(token_file(home, synthetic_token()))
    staged = file.stage(synthetic_token())
    file.discard(staged)
    assert not staged.exists()
    file.discard(staged)                           # already gone
    file.discard(None)

    def boom(self, missing_ok=False):
        raise PermissionError(1, "Operation not permitted")

    staged = file.stage(synthetic_token())
    monkeypatch.setattr(Path, "unlink", boom)
    file.discard(staged)                           # swallowed


# --- restore and the restore line ---------------------------------------------------------


def test_restore_puts_the_backup_back_byte_for_byte(home):
    old, new = synthetic_token(), synthetic_token()
    path = token_file(home, old + "\n")
    file = tf.TokenFile(path)
    backup = file.read_bytes()
    file.commit(file.stage(new))
    file.restore(backup)
    assert path.read_bytes() == backup and mode(path) == 0o600
    assert staged_files(path.parent) == []


def test_restore_refuses_a_backup_that_is_not_one_token_line(home):
    path = token_file(home, synthetic_token())
    before = path.read_bytes()
    with pytest.raises(tf.TokenFileError):
        tf.TokenFile(path).restore(b"two\nlines\n")
    assert path.read_bytes() == before


def test_restore_line_quotes_both_paths_and_holds_no_token(tmp_path):
    folder = tmp_path / "it's here"
    folder.mkdir()
    target = folder / "oauth"
    target.write_text("x")
    backup = tmp_path / "backups" / "20301208T123456Z.token"
    line = tf.TokenFile(target).restore_line(backup)
    assert line == f"install -m 600 {shlex.quote(str(backup))} {shlex.quote(str(target))}"
    assert "'\"'\"'" in line                      # the quote in the folder name is escaped
    assert "sk-ant-" not in line


# --- the super's ruling: an env-token host with the setting unset -------------------------


def test_an_unread_item_with_an_env_token_and_no_setting_refuses_naming_the_setting(
        tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    world = World(home, keychain=kc.FakeKeychain())          # no Keychain item
    env_token = synthetic_token()
    err = io.StringIO()
    code = world.run(environ={"CLAUDE_CODE_OAUTH_TOKEN": env_token, "HOME": str(home)},
                     err=err)
    assert code == 5
    message = err.getvalue()
    assert "no such item" in message
    assert "account.token_file" in message and shown("session") in message
    assert "Nothing was written" in message
    no_token_in(message + world.out.getvalue(), env_token, world.token, world.old)
    assert world.keychain.writes == [] and world.preflights == []
    assert world.backups() == [] and not world.ledger.exists()
    world.assert_no_leak(capsys)


def test_an_unread_item_with_no_env_token_still_names_the_setting(tmp_path, capsys):
    """Design sec 1, row 1: unset, an absent item is exit 5 and the message
    names the setting. A seat `session start` launched has no env token
    even on a token-file host, so the hint cannot hang on the variable."""
    home = tmp_path / "home"
    home.mkdir()
    world = World(home, keychain=kc.FakeKeychain())
    assert world.run(environ={"HOME": str(home)}) == 5
    message = world.err.getvalue()
    assert "no such item" in message
    assert "account.token_file" in message and shown("session") in message
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in message and "Nothing was written" in message
    assert world.keychain.writes == [] and world.preflights == []
    assert world.backups() == [] and not world.ledger.exists()
    world.assert_no_leak(capsys)


def test_an_unread_item_with_a_bad_setting_is_the_settings_refusal(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    world = World(home, keychain=kc.FakeKeychain())
    setting(home, "relative/path")
    assert world.run(environ={"HOME": str(home)}) == 5
    message = world.err.getvalue()
    assert "account.token_file (in " in message and "not an absolute path" in message
    world.assert_no_leak(capsys)


def test_with_the_setting_set_the_keychain_item_is_never_read(tmp_path, capsys):
    """The setting set is the file route: an absent Keychain item is never
    seen, so it is never named (the flow itself: test_account_use_tokenfile)."""
    home = tmp_path / "home"
    home.mkdir()
    world = World(home, keychain=kc.FakeKeychain())
    path = token_file(home, synthetic_token())
    setting(home, str(path))
    assert world.run(environ={"CLAUDE_CODE_OAUTH_TOKEN": synthetic_token(),
                              "HOME": str(home)}) == 0
    assert world.keychain.reads == 0 and world.keychain.writes == []
    assert "Name the file in the setting" not in world.err.getvalue()
    assert reg.same_token(tf.TokenFile(path).read(), world.token)
    captured = capsys.readouterr()
    no_token_in(world.text() + captured.out + captured.err, world.token, world.old)
