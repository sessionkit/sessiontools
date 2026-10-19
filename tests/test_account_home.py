"""ST-212: the account folder is `~/.sessionkit/tools/accounts/`, derived in
ONE place, made 0700 / 0600, and the old `~/.config/sessiontools/` is
never read or written: while it holds the only accounts, a reading verb
says so once on stderr and a writing verb refuses, exit 5.

Every HOME here is a scratch one under tmp_path.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import sessiontools.account
from sessiontools import settings
from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.account import status as st
from sessiontools.account import use as u

ROOT = Path(__file__).resolve().parents[1]
MOVER = f"{shlex.quote(sys.executable)} -m sessiontools.account.movehome"


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def registry_text(name: str) -> str:
    return json.dumps({"version": 1, "accounts": [
        {"name": name, "email": f"{name}@example.com",
         "sources": [f"pass:anthropic/{name}"], "fp": None}]}) + "\n"


def put_registry(folder: Path, name: str) -> Path:
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = folder / reg.REGISTRY_NAME
    path.write_text(registry_text(name))
    path.chmod(0o600)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return h


def old(home: Path) -> Path:
    return home / ".config" / "sessiontools"


def new(home: Path) -> Path:
    return home / ".sessionkit" / "tools" / "accounts"


def snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every path under ROOT: its mode and, for a file, its bytes."""
    if not root.exists():
        return {}
    found = {}
    for path in [root, *sorted(root.rglob("*"))]:
        found[str(path.relative_to(root))] = (
            mode(path), path.read_bytes() if path.is_file() else None)
    return found


def run(home: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = sessiontools.account.main(
        list(argv), keychain=kc.FakeKeychain(None), home=home, env={},
        fetch=lambda url: "", preflight=lambda token: None, seat_scan=list,
        backends={}, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# --- the one helper -------------------------------------------------------------------


def test_every_account_path_derives_from_the_one_helper(home):
    folder = settings.accounts_dir({"HOME": str(home)})
    assert folder == new(home)
    assert reg.accounts_dir(home) == folder
    assert reg.accounts_dir() == folder                      # HOME, read when called
    assert reg.default_path(home) == folder / "accounts.json"
    assert st.config_path(home) == folder / "account-config.json"
    assert u.config_dir(home) == folder
    rpath, cpath, ledger = st._paths(home, None, None)
    assert (rpath, cpath, ledger) == (folder / "accounts.json",
                                      folder / "account-config.json",
                                      folder / "host-ledger.jsonl")


def test_the_helper_takes_the_package_from_the_map_not_a_literal(home, monkeypatch):
    monkeypatch.setitem(settings.PACKAGES, "session", "elsewhere")
    assert reg.default_path(home) == home / ".sessionkit/elsewhere/accounts/accounts.json"


def test_the_accounts_folder_is_not_under_the_settings_tree(home):
    settings_dir = settings.package_dir(settings.PACKAGES["session"], {"HOME": str(home)})
    assert settings_dir not in reg.accounts_dir(home).parents
    assert reg.accounts_dir(home).parent == settings_dir.parent


def test_src_names_the_old_folder_once_the_stranded_constant():
    hits = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ".config/sessiontools" in line:
                hits.append((path.relative_to(ROOT).as_posix(), n, line.strip()))
    assert [(p, line) for p, _, line in hits] == [
        ("src/sessiontools/account/registry.py",
         'OLD_ACCOUNTS_SUBDIR = Path(".config/sessiontools")')]


# --- modes ------------------------------------------------------------------------------


def test_a_fresh_registry_makes_every_missing_folder_0700_and_the_file_0600(home):
    previous = os.umask(0o022)
    try:
        reg.save(reg.Registry(), reg.default_path(home))
    finally:
        os.umask(previous)
    for folder in (home / ".sessionkit", home / ".sessionkit/tools", new(home)):
        assert mode(folder) == 0o700, folder
    assert mode(reg.default_path(home)) == 0o600


def test_an_existing_folder_keeps_its_mode(home):
    (home / ".sessionkit").mkdir(mode=0o755)
    os.chmod(home / ".sessionkit", 0o755)
    reg.save(reg.Registry(), reg.default_path(home))
    assert mode(home / ".sessionkit") == 0o755                # never chmodded
    assert mode(home / ".sessionkit/tools") == 0o700
    assert mode(new(home)) == 0o700


def test_the_ledger_and_a_backup_make_their_folders_0700_and_files_0600(home):
    from datetime import UTC, datetime
    u.append_ledger(u.config_dir(home) / u.LEDGER_NAME, {"to": "a"})
    assert mode(new(home)) == 0o700 and mode(home / ".sessionkit") == 0o700
    assert mode(new(home) / u.LEDGER_NAME) == 0o600
    backup = u.write_backup(b"line\n", u.config_dir(home) / u.FILE_BACKUP_DIR,
                            datetime(2030, 6, 9, tzinfo=UTC), suffix=".token",
                            check=lambda blob: None, into="the token file")
    assert mode(backup.parent) == 0o700 and mode(backup) == 0o600


def test_make_private_dirs_refuses_a_file_where_a_folder_belongs(home):
    (home / ".sessionkit").write_text("not a folder")
    with pytest.raises(NotADirectoryError):
        reg.make_private_dirs(new(home))


def test_a_registry_whose_folder_cannot_be_made_is_exit_5_not_a_traceback(home):
    (home / ".sessionkit").write_text("not a folder")
    with pytest.raises(reg.RegistryError) as raised:
        reg.save(reg.Registry(), reg.default_path(home))
    assert raised.value.code == 5 and "could not be made" in str(raised.value)


# --- the stranded line: absent / old-only / new-only / both -----------------------------


@pytest.mark.parametrize("verb", [("list",), ("status",), ("status", "--json")])
def test_neither_folder_says_no_stranded_line(home, verb):
    code, _, err = run(home, *verb)
    assert code == 0 and err == ""
    assert not new(home).exists()                             # a read makes nothing


@pytest.mark.parametrize("verb", [("list",), ("status",), ("status", "--json")])
def test_old_only_says_one_line_reads_nothing_old_and_writes_nothing(home, verb):
    put_registry(old(home), "oldone")
    before = snapshot(old(home))
    code, out, err = run(home, *verb)
    assert code == 0
    assert err.splitlines() == [reg.stranded_line(home)]      # exactly one stderr line
    assert "oldone" not in out                                # the old registry: unread
    assert snapshot(old(home)) == before                      # untouched
    assert not (home / ".sessionkit").exists()                # nothing written
    if verb == ("list",):
        assert f"no accounts registered: {reg.default_path(home)} does not exist" in out


def test_the_stranded_line_names_both_folders_and_the_runnable_mover(home):
    put_registry(old(home), "oldone")
    line = reg.stranded_line(home)
    assert "~/.config/sessiontools/" in line and "~/.sessionkit/tools/accounts/" in line
    assert f"`{MOVER}`" in line and f"`{MOVER} --apply`" in line
    assert "python -m" not in line.replace(MOVER, "")        # never a bare python
    assert "\n" not in line


def test_the_stranded_line_is_said_once_per_process(home):
    put_registry(old(home), "oldone")
    assert run(home, "list")[2].count("have not been moved") == 1
    assert run(home, "list")[2] == ""
    assert run(home, "status")[2] == ""


def test_new_only_says_nothing_and_reads_the_new_folder(home):
    put_registry(new(home), "newone")
    code, out, err = run(home, "list")
    assert code == 0 and err == ""
    assert "newone" in out


def test_both_says_nothing_reads_new_and_ignores_old(home):
    put_registry(old(home), "oldone")
    put_registry(new(home), "newone")
    before = snapshot(old(home))
    code, out, err = run(home, "list")
    assert code == 0 and err == ""
    assert "newone" in out and "oldone" not in out
    assert snapshot(old(home)) == before


def test_an_empty_old_folder_is_not_stranded(home):
    old(home).mkdir(parents=True)
    assert not reg.stranded(home)
    assert run(home, "list")[2] == ""


def test_an_explicit_registry_path_skips_the_check(home, capsys):
    put_registry(old(home), "oldone")
    out, err = io.StringIO(), io.StringIO()
    assert st.list_accounts(keychain=kc.FakeKeychain(None), home=home,
                            registry_path=home / "elsewhere.json", out=out, err=err) == 0
    assert err.getvalue() == ""


# --- writing verbs refuse while stranded ------------------------------------------------


@pytest.mark.parametrize("argv", [
    ("use", "oldone", "--witness", "alice", "--yes"),
    ("add", "a", "--email", "a@example.com", "--source", "pass:anthropic/a", "--no-fp"),
    ("remove", "oldone"),
    ("new", "a", "--email", "a@example.com"),
])
def test_a_writing_verb_refuses_exit_5_with_one_line_and_writes_nothing(home, argv):
    put_registry(old(home), "oldone")
    before = snapshot(old(home))
    code, out, err = run(home, *argv)
    assert code == 5
    assert out == ""
    lines = err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"session account {argv[0]}: refused: ")
    assert "~/.config/sessiontools/" in lines[0] and "~/.sessionkit/tools/accounts/" in lines[0]
    assert f"`{MOVER}`" in lines[0] and f"`{MOVER} --apply`" in lines[0]
    assert snapshot(old(home)) == before
    assert not (home / ".sessionkit").exists()


def test_add_writes_to_the_new_folder_when_nothing_is_stranded(home):
    code, out, err = run(home, "add", "a", "--email", "a@example.com",
                         "--source", "pass:anthropic/a", "--no-fp")
    assert (code, err) == (0, "")
    assert str(reg.default_path(home)) in out
    assert reg.load(reg.default_path(home)).names() == ["a"]
    assert not old(home).exists()


# --- the strict stat: unreadable is never absent ----------------------------------------


@pytest.fixture
def locked(home):
    """chmod 000 on folders; restored before tmp_path's cleanup."""
    held: list[Path] = []

    def lock(path: Path) -> None:
        path.chmod(0)
        held.append(path)

    yield lock
    for path in reversed(held):
        path.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a mode")
@pytest.mark.parametrize("verb", ["list", "status", "add"])
def test_an_unsearchable_old_folder_is_exit_5_naming_it_never_absent(home, locked, verb):
    put_registry(old(home), "oldone")
    locked(home / ".config")
    argv = (verb,) if verb != "add" else ("add", "a", "--email", "a@example.com",
                                          "--source", "pass:anthropic/a", "--no-fp")
    code, out, err = run(home, *argv)
    assert code == 5
    assert str(old(home)) in err and "never read as absent" in err
    assert "no accounts registered" not in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through a mode")
def test_an_unsearchable_new_parent_is_exit_5_naming_it(home, locked):
    put_registry(new(home), "newone")
    locked(home / ".sessionkit" / "tools")
    code, _, err = run(home, "list")
    assert code == 5 and str(new(home)) in err


# --- the mover command is runnable as printed --------------------------------------------


def test_the_printed_mover_command_runs(home):
    put_registry(old(home), "oldone")
    command = reg.stranded_line(home).split("`")[1]
    done = subprocess.run(shlex.split(command), capture_output=True, text=True, check=False,
                          env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "dry run" in done.stdout
    assert old(home).exists() and not new(home).exists()
