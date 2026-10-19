"""ST-212: `python -m sessiontools.account.movehome`, the one-time move of
the account folder into the kit's home. Dry run by default; sha256
verified before and after; refuses when both folders exist; never prints a
token or a digest; keeps the modes.

SCRATCH HOMES ONLY: every HOME is under tmp_path, and every test passes
it explicitly. No test touches a real ~/.config/sessiontools or ~/.sessionkit.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import secrets as pysecrets
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import sessiontools.account
from sessiontools.account import keychain as kc
from sessiontools.account import leakscan as ls
from sessiontools.account import movehome as mh
from sessiontools.account import registry as reg


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def synthetic_token() -> str:
    return "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))   # the conftest wall's HOME, made explicit
    return h


def old(home: Path) -> Path:
    return home / ".config" / "sessiontools"


def new(home: Path) -> Path:
    return home / ".sessionkit" / "tools" / "accounts"


def write(path: Path, data: str, file_mode: int = 0o600) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(data)
    path.chmod(file_mode)
    return path


def populate(home: Path) -> str:
    """The old folder as a host holds it: all five kinds, 0700 / 0600, with
    a token in a backup. Returns the token."""
    token = synthetic_token()
    root = old(home)
    root.mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    write(root / "accounts.json", '{"version": 1, "accounts": [{"name": "crew", '
          '"email": "crew@example.com", "sources": ["pass:anthropic/crew"], "fp": null}]}\n')
    write(root / "account-config.json", '{"host_read": false}\n')
    write(root / "host-ledger.jsonl", '{"to":"crew"}\n')
    (root / "token-file-backups").mkdir(mode=0o700)
    write(root / "token-file-backups" / "20300609T123456Z.token", token + "\n")
    (root / "keychain-backups").mkdir(mode=0o700)
    write(root / "keychain-backups" / "20300609T123456Z.json", '{"claudeAiOauth": {}}')
    return token


def tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    if not os.path.lexists(root):
        return {}
    found = {".": (mode(root), None)}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            data = b"-> " + os.readlink(path).encode()
        else:
            data = None if path.is_dir() else path.read_bytes()
        found[str(path.relative_to(root))] = (mode(path), data)
    return found


def move(home: Path, *, apply: bool = False, **kwargs) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = mh.move(home, apply=apply, out=out, err=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


def assert_no_secret(token: str, home: Path, *texts: str) -> None:
    """No token, and no digest of any file, in any text; leakscan finds
    nothing token-shaped in it either."""
    digests = [hashlib.sha256(p.read_bytes()).hexdigest()
               for root in (old(home), new(home)) if root.exists()
               for p in root.rglob("*") if p.is_file()]
    for text in texts:
        assert token not in text
        assert not ls.scan_text(text, {}), "token-shaped text in the output"
        for digest in digests:
            assert digest not in text and digest[:16] not in text


# --- the move -------------------------------------------------------------------------


def test_the_dry_run_is_the_default_and_writes_nothing(home):
    token = populate(home)
    before = tree(old(home))
    code, out, err = move(home)
    assert (code, err) == (0, "")
    assert tree(old(home)) == before
    assert not (home / ".sessionkit").exists()
    assert "dry run: nothing was written" in out and "--apply" in out
    assert "  accounts.json  0600  " in out
    assert "  token-file-backups/  0700  -" in out
    assert "5 file(s)" in out
    assert "warning" not in out
    assert_no_secret(token, home, out, err)


def test_apply_moves_every_file_keeps_modes_and_verifies(home):
    token = populate(home)
    before = tree(old(home))
    code, out, err = move(home, apply=True)
    assert (code, err) == (0, ""), err
    assert not os.path.lexists(old(home))
    assert (home / ".config").is_dir()                       # ~/.config left alone
    assert tree(new(home)) == before                         # bytes and modes, all of it
    assert mode(home / ".sessionkit") == 0o700
    assert mode(home / ".sessionkit" / "tools") == 0o700
    total = sum(len(data) for _, data in before.values() if data is not None)
    assert f"verified: 5 file(s), {total} byte(s), sha256 equal" in out
    assert_no_secret(token, home, out, err)


def test_after_the_move_session_account_reads_the_moved_registry(home):
    populate(home)
    assert move(home, apply=True)[0] == 0
    out, err = io.StringIO(), io.StringIO()
    assert sessiontools.account.main(["list"], keychain=kc.FakeKeychain(None), home=home,
                                     env={}, out=out, err=err) == 0
    assert err.getvalue() == ""
    assert "crew" in out.getvalue()


def test_a_rerun_after_the_move_has_nothing_to_move(home):
    populate(home)
    assert move(home, apply=True)[0] == 0
    after = tree(new(home))
    code, out, err = move(home, apply=True)
    assert (code, err) == (0, "")
    assert "nothing to move" in out
    assert tree(new(home)) == after


def test_no_old_folder_or_an_empty_one_has_nothing_to_move(home):
    assert move(home)[:2] == (0, "nothing to move: ~/.config/sessiontools/ does not exist\n")
    old(home).mkdir(parents=True)
    assert move(home, apply=True)[:2] == (0, "nothing to move: ~/.config/sessiontools/ is empty\n")
    assert not (home / ".sessionkit").exists()


def test_an_existing_kit_home_keeps_its_mode_and_its_other_folders(home):
    populate(home)
    mail = write(home / ".sessionkit" / "mail" / "settings" / "mail.json", "{}\n", 0o644)
    (home / ".sessionkit").chmod(0o755)
    assert move(home, apply=True)[0] == 0
    assert mode(home / ".sessionkit") == 0o755
    assert mail.read_text() == "{}\n" and mode(mail) == 0o644          # mail untouched


def test_an_odd_mode_is_a_warning_and_is_moved_as_it_is(home):
    populate(home)
    (old(home) / "account-config.json").chmod(0o644)
    code, out, _ = move(home, apply=True)
    assert code == 0
    assert "warning: account-config.json is mode 0644, not 0600 (moved as it is" in out
    assert mode(new(home) / "account-config.json") == 0o644


# --- refusals -------------------------------------------------------------------------


@pytest.mark.parametrize("what", ["full", "empty", "file", "dangling-link"])
def test_both_folders_existing_is_refused_exit_3_and_nothing_is_written(home, what):
    populate(home)
    target = new(home)
    target.parent.mkdir(parents=True)
    if what == "full":
        write(target / "accounts.json", "{}\n")
    elif what == "empty":
        target.mkdir()
    elif what == "file":
        target.write_text("x")
    else:
        target.symlink_to(home / "nowhere")
    old_before, new_before = tree(old(home)), os.lstat(target)
    code, out, err = move(home, apply=True)
    assert code == 3 and out == ""
    assert "both folders exist" in err and "nothing was written" in err
    assert tree(old(home)) == old_before
    assert os.lstat(target).st_ino == new_before.st_ino


def test_a_symlink_inside_is_refused_exit_3(home):
    populate(home)
    (old(home) / "linked.json").symlink_to(home / "elsewhere.json")
    before = tree(old(home))
    code, _, err = move(home, apply=True)
    assert code == 3 and "is a symlink" in err
    assert tree(old(home)) == before and not (home / ".sessionkit").exists()


def test_an_old_folder_that_is_a_symlink_is_refused_exit_3(home):
    real = home / "real-accounts"
    write(real / "accounts.json", "{}\n")
    old(home).parent.mkdir(parents=True)
    old(home).symlink_to(real)
    code, _, err = move(home, apply=True)
    assert code == 3 and "is a symlink" in err
    assert old(home).is_symlink() and not (home / ".sessionkit").exists()


def test_a_fifo_inside_is_refused_exit_3(home):
    populate(home)
    os.mkfifo(old(home) / "pipe")
    code, _, err = move(home, apply=True)
    assert code == 3 and "not a file or a folder" in err
    assert not (home / ".sessionkit").exists()


def test_a_rename_across_filesystems_is_refused_and_leaves_no_parents(home):
    populate(home)
    before = tree(old(home))

    def exdev(src, dst):
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

    code, _, err = move(home, apply=True, rename=exdev)
    assert code == 3 and "another filesystem" in err and "nothing was copied" in err
    assert tree(old(home)) == before
    assert not (home / ".sessionkit").exists()                 # the parents it made: gone


def test_a_failed_verify_renames_back_exit_4_naming_only_the_path(home):
    token = populate(home)

    def rename_and_corrupt(src, dst):
        os.rename(src, dst)
        if Path(dst) == new(home):
            (Path(dst) / "host-ledger.jsonl").write_text('{"to":"other"}\n')

    code, out, err = move(home, apply=True, rename=rename_and_corrupt)
    assert code == 4
    assert "the verify failed on host-ledger.jsonl" in err
    assert "renamed back to ~/.config/sessiontools/" in err
    assert old(home).is_dir() and not new(home).exists()
    assert_no_secret(token, home, out, err)


# --- the strict stat: unreadable is never "nothing to move" -----------------------------


@pytest.fixture
def locked():
    held: list[Path] = []

    def lock(path: Path, how: int = 0) -> None:
        path.chmod(how)
        held.append(path)

    yield lock
    for path in reversed(held):
        path.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a mode")
def test_an_unsearchable_config_folder_is_exit_6_never_nothing_to_move(home, locked):
    populate(home)
    locked(home / ".config")
    code, out, err = move(home, apply=True)
    assert code == 6 and out == ""
    assert str(old(home)) in err and "never read as absent" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a mode")
def test_an_unlistable_old_folder_is_exit_6(home, locked):
    populate(home)
    locked(old(home), 0o300)                                     # search, no read
    code, _, err = move(home)
    assert code == 6 and "could not be listed" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a mode")
def test_an_unreadable_file_is_exit_6_and_nothing_moves(home, locked):
    populate(home)
    locked(old(home) / "host-ledger.jsonl")
    code, _, err = move(home, apply=True)
    assert code == 6 and "host-ledger.jsonl could not be read" in err
    assert old(home).is_dir() and not (home / ".sessionkit").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a mode")
def test_an_unsearchable_new_parent_is_exit_6(home, locked):
    populate(home)
    (home / ".sessionkit" / "tools").mkdir(parents=True)
    locked(home / ".sessionkit" / "tools")
    code, _, err = move(home, apply=True)
    assert code == 6 and str(new(home)) in err
    assert old(home).is_dir()


# --- the command line -------------------------------------------------------------------


def test_the_module_runs_as_a_command_with_home(tmp_path):
    h = tmp_path / "scratch"
    h.mkdir()
    token = populate(h)
    env = {"HOME": str(tmp_path / "not-this-one"), "PATH": "/usr/bin:/bin"}
    argv = [sys.executable, "-m", "sessiontools.account.movehome", "--home", str(h)]
    dry = subprocess.run(argv, capture_output=True, text=True, env=env, check=False,
                         timeout=60)
    assert dry.returncode == 0 and "dry run" in dry.stdout
    done = subprocess.run([*argv, "--apply"], capture_output=True, text=True, env=env,
                          check=False, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "verified: 5 file(s)" in done.stdout
    assert new(h).is_dir() and not old(h).exists()
    assert not (tmp_path / "not-this-one").exists()
    assert_no_secret(token, h, dry.stdout, dry.stderr, done.stdout, done.stderr)


def test_a_bad_flag_is_exit_2():
    err = io.StringIO()
    assert mh.main(["--bogus"], out=io.StringIO(), err=err) == 2


def test_main_defaults_to_home(home):
    out = io.StringIO()
    assert mh.main([], out=out, err=io.StringIO()) == 0
    assert "nothing to move" in out.getvalue()


def test_the_mover_command_is_the_one_registry_prints():
    assert reg.mover_command().endswith(" -m sessiontools.account.movehome")
    assert mh.PROG == "python -m sessiontools.account.movehome"
