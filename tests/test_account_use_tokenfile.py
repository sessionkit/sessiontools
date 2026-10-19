"""`session account use NAME` on the token-file route, end to end.

Every test runs in the conftest's scratch HOME, under `test_account_use`'s
guard (any subprocess naming `security`, `pass` or `claude` raises) and
under this module's OPEN FENCE: `open`, `io.open`, `os.open` and
`os.replace` refuse any path outside the test's temp tree, the interpreter
and this checkout, so "no real token file is read or written" is an
assertion, not a hope. The Keychain is a `FakeKeychain` that must end with
no read and no write. The host's token file is `.tokens/oauth` under the
scratch HOME, named through a `session` host-layer file, and every token
is synthetic.

Every test ends with `assert_no_leak`, the one leak grep: neither token,
neither token's fingerprint and no `sk-ant-` shape (nor a `leakscan` hit)
in out, err, captured output or the ledger, which holds the outcome rows.
The backup is the one file that holds a token by design: tests check its
mode and bytes, and that its content is never printed.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from conftest import ANCHOR_SUMMER
from settings_support import write_session_settings
from test_account_use import (  # noqa: F401 - the autouse guard is imported to apply here
    _no_keychain_no_pass_no_claude,
    synthetic_token,
)

from sessiontools.account import keychain as kc
from sessiontools.account import leakscan as ls
from sessiontools.account import registry as reg
from sessiontools.account import tokenfile as tf
from sessiontools.account import use as u

AT = ANCHOR_SUMMER
CHECKOUT = Path(__file__).resolve().parents[1]


# --- the open fence --------------------------------------------------------------------


class Fence:
    """Paths outside ALLOWED that anything tried to open or replace."""

    def __init__(self, allowed: list[str]) -> None:
        self.allowed = [os.path.realpath(root) for root in allowed]
        self.outside: list[str] = []

    def check(self, path) -> None:
        if isinstance(path, int):                       # an fd already opened
            return
        real = os.path.realpath(os.fspath(path))
        if any(real == root or real.startswith(root + os.sep) for root in self.allowed):
            return
        self.outside.append(real)
        raise AssertionError(f"the open fence refused a path outside the test: {real}")


@pytest.fixture(autouse=True)
def fence(monkeypatch, tmp_path_factory):
    """`open`, `io.open`, `os.open` and `os.replace` refuse a path outside the
    session's temp tree (every tmp_path and the conftest's scratch HOME),
    the interpreter's prefixes and this checkout. A refusal is recorded too,
    so one swallowed by a broad `except` still fails the test at teardown."""
    walls = Fence([str(tmp_path_factory.getbasetemp()), sys.prefix, sys.base_prefix,
                   sys.exec_prefix, str(CHECKOUT), os.devnull])
    real_open, real_os_open, real_replace = builtins.open, os.open, os.replace

    def fenced_open(file, *args, **kwargs):
        walls.check(file)
        return real_open(file, *args, **kwargs)

    def fenced_os_open(path, *args, **kwargs):
        walls.check(path)
        return real_os_open(path, *args, **kwargs)

    def fenced_replace(src, dst, *args, **kwargs):
        walls.check(src)
        walls.check(dst)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fenced_open)
    monkeypatch.setattr(io, "open", fenced_open)
    monkeypatch.setattr(os, "open", fenced_os_open)
    monkeypatch.setattr(os, "replace", fenced_replace)
    yield walls
    monkeypatch.undo()
    assert walls.outside == [], f"paths outside the test were touched: {walls.outside}"


def test_the_fence_refuses_a_path_outside_the_test(fence):
    outside = os.path.join(os.sep, "fence-probe-not-a-file")
    for attempt in (lambda: open(outside),  # noqa: SIM115 - the fence refuses; nothing opens
                    lambda: Path(outside).read_bytes(),
                    lambda: os.open(outside, os.O_RDONLY),
                    lambda: os.replace(outside, outside + ".2")):
        with pytest.raises(AssertionError, match="open fence"):
            attempt()
    # one record per attempt; os.replace stops at its source
    assert fence.outside == [os.path.realpath(outside)] * 4
    assert not os.path.lexists(outside)
    fence.outside.clear()                  # the refusals were this test's point


# --- the world -------------------------------------------------------------------------


class FileWorld:
    """A scratch HOME on the token-file route: accounts `a` (the host's) and
    `b` (the target), each a private `file:` source with a synthetic token,
    registered with its fp; the host's token file holding `a`'s token, named
    by `account.token_file`; a FakeKeychain that must never be used; a fake
    preflight; a seat scan."""

    def __init__(self, home: Path, *, link: bool = False) -> None:
        self.home = home
        self.old = synthetic_token()
        self.token = synthetic_token()
        sources = home / "sources"
        sources.mkdir(mode=0o700)
        registry = reg.Registry()
        for name, email, token in (("a", "a@example.com", self.old),
                                   ("b", "b@example.com", self.token)):
            path = sources / name
            path.write_text(token + "\n")
            path.chmod(0o600)
            registry.add(reg.make_account(name, email, [f"file:{path}"],
                                          reg.fingerprint(token)))
        reg.save(registry, reg.default_path(home))
        self.folder = home / ".tokens"
        self.folder.mkdir(mode=0o700)
        self.path = self.folder / "oauth"
        self.target = self.path
        if link:
            store = home / ".store"
            store.mkdir(mode=0o700)
            self.target = store / "oauth-real"
            self.path.symlink_to(self.target)
        self.target.write_text(self.old + "\n")
        self.target.chmod(0o600)
        self.blob = self.target.read_bytes()
        self.ino = os.stat(self.target).st_ino
        write_session_settings(home, {"account": {"token_file": str(self.path)}})
        self.keychain = kc.FakeKeychain()
        self.preflights: list[str] = []
        self.preflight_fails = False
        self.seats = [u.Seat("s1", 1_000_000), u.Seat("s2", None)]
        self.environ: dict[str, str] = {"HOME": str(home), "USER": "tester"}
        self.out = io.StringIO()
        self.err = io.StringIO()

    def preflight(self, token: str) -> None:
        self.preflights.append(reg.fingerprint(token))
        if self.preflight_fails:
            raise u.PreflightError("claude exited 1 and did not reply (fake)")

    def run(self, name: str = "b", *, witness: str | None = "wit", yes: bool = True,
            **overrides) -> int:
        kwargs = {"witness": witness, "yes": yes, "keychain": self.keychain,
                  "preflight": self.preflight, "seat_scan": lambda: self.seats,
                  "home": self.home, "clock": lambda: AT, "by": "session:test-sid",
                  "environ": self.environ, "out": self.out, "err": self.err}
        kwargs.update(overrides)
        return u.use(name, **kwargs)

    @property
    def cfg(self) -> Path:
        return reg.accounts_dir(self.home)

    @property
    def ledger(self) -> Path:
        return self.cfg / u.LEDGER_NAME

    def backups(self) -> list[Path]:
        folder = self.cfg / u.FILE_BACKUP_DIR
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def leftovers(self) -> list[str]:
        """Staged temp files beside the target: there must never be one left."""
        return sorted(p.name for p in self.target.parent.iterdir()
                      if tf.STAGE_INFIX in p.name)

    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def assert_unchanged(self) -> None:
        """The host's file byte for byte, the same inode, 0600; no temp file."""
        assert self.target.read_bytes() == self.blob
        assert os.stat(self.target).st_ino == self.ino
        assert stat.S_IMODE(os.stat(self.target).st_mode) == 0o600
        assert self.leftovers() == []

    def assert_keychain_untouched(self) -> None:
        assert self.keychain.reads == 0
        assert self.keychain.writes == []


def assert_no_leak(world: FileWorld, capsys) -> None:
    """SC3's one leak grep, over out, err, captured output and the ledger
    (where the outcome rows are): neither token, neither fingerprint, no
    `sk-ant-` shape and no `leakscan` hit. The backup's bytes are never in
    any of them either."""
    captured = capsys.readouterr()
    texts = {"out": world.out.getvalue(), "err": world.err.getvalue(),
             "captured out": captured.out, "captured err": captured.err,
             "ledger": world.ledger.read_text() if world.ledger.exists() else ""}
    fps = {reg.fingerprint(world.old): "a", reg.fingerprint(world.token): "b"}
    for where, text in texts.items():
        for token in (world.old, world.token):
            assert token not in text, f"a token leaked into {where}"
            assert reg.fingerprint(token) not in text, f"a fingerprint leaked into {where}"
            assert token.encode().hex() not in text, f"a token's hex leaked into {where}"
        assert "sk-ant-" not in text, f"a token shape leaked into {where}"
        assert ls.scan_text(text, fps, where) == []


@pytest.fixture
def world(tmp_path) -> FileWorld:
    home = tmp_path / "home"
    home.mkdir()
    return FileWorld(home)


# --- detect -----------------------------------------------------------------------------


def test_the_setting_picks_the_file_route_and_never_builds_a_security_cli(
        world, monkeypatch, capsys):
    def no_security(*_args, **_kwargs):
        raise AssertionError("the file route built a SecurityCLI")

    monkeypatch.setattr(kc.SecurityCLI, "__init__", no_security)
    assert world.run(keychain=None, yes=False) == 3      # the default keychain: none built
    assert f"file:    token file {world.path}" in world.out.getvalue()
    assert "keychain: not read" in world.out.getvalue()
    world.assert_unchanged()
    assert_no_leak(world, capsys)


@pytest.mark.parametrize("case", ["missing", "not-private", "two-lines", "relative", "fifo"])
def test_a_set_but_unusable_token_file_exits_5_before_anything(world, case, capsys):
    if case == "missing":
        world.target.unlink()
    elif case == "not-private":
        world.target.chmod(0o644)
    elif case == "two-lines":
        world.target.write_text(world.old + "\nsecond\n")
    elif case == "relative":
        write_session_settings(world.home, {"account": {"token_file": "relative/oauth"}})
    elif case == "fifo":
        world.target.unlink()
        os.mkfifo(world.target, 0o600)
    assert world.run() == 5
    message = world.err.getvalue()
    assert "session account use:" in message
    assert world.out.getvalue() == ""                     # refused before the plan
    assert world.preflights == [] and world.backups() == []
    world.assert_keychain_untouched()
    if case == "missing":
        assert "does not create" in message and not world.target.exists()
    if case == "relative":
        assert "account.token_file (in " in message
    assert world.leftovers() == []
    assert_no_leak(world, capsys)


def test_unset_with_no_item_and_an_env_token_names_the_setting(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    world = FileWorld(home)
    write_session_settings(home, {"account": {}})          # the setting unset
    world.environ["CLAUDE_CODE_OAUTH_TOKEN"] = world.old
    assert world.run() == 5
    message = world.err.getvalue()
    assert tf.SETTING_KEY in message and "CLAUDE_CODE_OAUTH_TOKEN" in message
    assert world.keychain.writes == []                     # the Keychain route: read only
    world.assert_unchanged()
    assert_no_leak(world, capsys)


# --- the plan and the gates --------------------------------------------------------------


def test_the_plan_without_yes_writes_nothing_and_runs_no_claude(world, capsys):
    assert world.run(yes=False) == 3
    out = world.out.getvalue()
    assert "use b (b@example.com): the host moves from a to b" in out
    assert f"file:    token file {world.path}" in out
    assert "keychain: not read: a seat that signs in from it does not move" in out
    assert "preflight: runs before the write" in out
    assert ("seats:   2 live seats keep the token they launched with until they restart: "
            "s1, s2; new launches read the new file") in out
    assert "this seat holds the old token: no" in out
    assert f"backup:  {world.cfg / u.FILE_BACKUP_DIR}{os.sep} (written before" in out
    assert "witness: wit" in out
    assert "plan only: re-run with --yes to swap the file" in out
    assert world.preflights == [] and world.backups() == []
    assert not world.ledger.exists()
    world.assert_unchanged()
    world.assert_keychain_untouched()
    assert_no_leak(world, capsys)


def test_a_missing_witness_is_reported_after_the_plan_with_nothing_written(world, capsys):
    assert world.run(witness=None) == 2
    assert "witness: MISSING" in world.out.getvalue()
    message = world.err.getvalue()
    assert "--witness WHO" in message and "token file is never touched" in message
    assert world.preflights == [] and world.backups() == []
    assert not world.ledger.exists()
    world.assert_unchanged()
    world.assert_keychain_untouched()
    assert_no_leak(world, capsys)


def test_already_on_the_target_is_exit_0_with_nothing_written(world, capsys):
    world.target.write_text(world.token + "\n")
    world.blob, world.ino = world.target.read_bytes(), os.stat(world.target).st_ino
    assert world.run() == 0
    assert "already on b (b@example.com); nothing written" in world.out.getvalue()
    assert world.preflights == [] and world.backups() == []
    assert not world.ledger.exists()
    world.assert_unchanged()
    world.assert_keychain_untouched()
    assert_no_leak(world, capsys)


# --- the swap ----------------------------------------------------------------------------


def test_the_swap_backs_up_stages_commits_verifies_and_ledgers(world, capsys):
    assert world.run() == 0
    # the swap: the new token, 0600, a new inode (a rename, not a rewrite)
    assert world.target.read_bytes() == (world.token + "\n").encode()
    assert stat.S_IMODE(os.stat(world.target).st_mode) == 0o600
    assert os.stat(world.target).st_ino != world.ino
    assert world.leftovers() == []
    assert world.preflights == [reg.fingerprint(world.token)]
    # the backup: the old bytes exactly, 0600, in a 0700 folder
    [backup] = world.backups()
    assert backup.name == "20301208T123456Z.token"
    assert backup.read_bytes() == world.blob
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    # the ledger row, with its route
    [line] = world.ledger.read_text().splitlines()
    assert json.loads(line) == {"at": "2030-12-08T12:34:56+00:00", "from": "a", "to": "b",
                                "by": "session:test-sid", "witness": "wit",
                                "backup": str(backup), "route": "token-file"}
    assert stat.S_IMODE(world.ledger.stat().st_mode) == 0o600
    # the outcome repeats the restart report
    out = world.out.getvalue()
    assert f"backup:  {backup}" in out
    assert f"done: the host's token file is on b (b@example.com); ledger {world.ledger}" in out
    assert out.count("2 live seats keep the token they launched with") == 2
    world.assert_keychain_untouched()
    assert_no_leak(world, capsys)


def test_a_second_swap_in_the_same_second_gets_its_own_backup(world, capsys):
    assert world.run() == 0
    assert world.run("a") == 0
    assert [p.name for p in world.backups()] == ["20301208T123456Z-2.token",
                                                 "20301208T123456Z.token"]
    assert world.target.read_bytes() == world.blob
    assert [json.loads(row)["route"] for row in world.ledger.read_text().splitlines()] \
        == ["token-file", "token-file"]
    assert_no_leak(world, capsys)


def test_a_link_is_kept_and_its_target_swapped(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    world = FileWorld(home, link=True)
    assert world.run(yes=False) == 3
    assert f"token file {world.path} (a link to {world.target})" in world.out.getvalue()
    assert world.run() == 0
    assert world.path.is_symlink()
    assert os.readlink(world.path) == str(world.target)
    assert world.target.read_bytes() == (world.token + "\n").encode()
    assert world.path.read_bytes() == (world.token + "\n").encode()
    assert os.stat(world.target).st_ino != world.ino
    assert world.leftovers() == [] and list(world.folder.iterdir()) == [world.path]
    assert_no_leak(world, capsys)


# --- failures ---------------------------------------------------------------------------


def test_a_failed_preflight_leaves_nothing_on_disk(world, capsys):
    """SC2's rollback on a failed preflight: it runs before the backup and
    the stage (review N1), so the host's file is byte-identical and there
    is no backup and no temp file."""
    world.preflight_fails = True
    assert world.run() == 4
    assert world.preflights == [reg.fingerprint(world.token)]
    assert "preflight of 'b' failed" in world.err.getvalue()
    assert "nothing was touched" in world.err.getvalue()
    world.assert_unchanged()
    assert world.backups() == []
    assert not (world.cfg / u.FILE_BACKUP_DIR).exists()
    assert not world.ledger.exists()
    world.assert_keychain_untouched()
    assert_no_leak(world, capsys)


def test_a_failed_stage_check_discards_the_temp_file_exit_6(world, monkeypatch, capsys):
    def bad_check(self, staged, token):
        assert Path(staged).exists()                     # staged, then refused
        raise tf.TokenFileError(f"the staged file {staged} did not read back as the new "
                                "token")

    monkeypatch.setattr(tf.TokenFile, "check_staged", bad_check)
    assert world.run() == 6
    message = world.err.getvalue()
    assert "nothing was swapped" in message and "still on a" in message
    world.assert_unchanged()
    assert len(world.backups()) == 1                     # the backup came first
    assert not world.ledger.exists()
    assert_no_leak(world, capsys)


def test_a_failed_stage_is_exit_6_with_the_file_unchanged(world, monkeypatch, capsys):
    def no_room(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tf.tempfile, "mkstemp", no_room)
    assert world.run() == 6
    assert "could not stage" in world.err.getvalue()
    world.assert_unchanged()
    assert_no_leak(world, capsys)


def test_a_failed_commit_leaves_the_file_unchanged_exit_6(world, monkeypatch, capsys):
    real = os.replace

    def refuse(src, dst, *args, **kwargs):
        if Path(dst) == world.target:
            raise OSError(13, "Permission denied")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse)
    assert world.run() == 6
    message = world.err.getvalue()
    assert "the swap failed" in message and "left unchanged" in message
    world.assert_unchanged()
    assert not world.ledger.exists()
    assert_no_leak(world, capsys)


def test_a_failed_verify_restores_the_backup_exit_6(world, monkeypatch, capsys):
    stranger = synthetic_token()
    monkeypatch.setattr(tf.TokenFile, "read", lambda self: stranger)
    assert world.run() == 6
    message = world.err.getvalue()
    assert "did not read back as b" in message
    assert "restored from the backup" in message and "still on a" in message
    assert world.target.read_bytes() == world.blob      # back, byte for byte
    assert stat.S_IMODE(os.stat(world.target).st_mode) == 0o600
    assert world.leftovers() == []
    assert not world.ledger.exists()
    assert stranger not in world.text()
    assert_no_leak(world, capsys)


def test_a_failed_restore_is_exit_7_loud_with_the_manual_line(world, monkeypatch, capsys):
    monkeypatch.setattr(tf.TokenFile, "read", lambda self: synthetic_token())

    def no_restore(self, backup):
        raise tf.TokenFileError(f"could not replace {self.target} (Permission denied)")

    monkeypatch.setattr(tf.TokenFile, "restore", no_restore)
    assert world.run() == 7
    message = world.err.getvalue()
    [backup] = world.backups()
    assert "RESTORE FAILED: THE HOST'S TOKEN FILE MAY BE BROKEN" in message
    assert f"backup:  {backup}" in message
    assert f"install -m 600 {backup} {world.target}" in message
    assert backup.read_bytes() == world.blob             # the restore point is intact
    assert not world.ledger.exists()
    assert_no_leak(world, capsys)


def test_a_backup_that_is_not_one_token_line_is_exit_5_unswapped(world, monkeypatch,
                                                                  capsys):
    real = Path.read_bytes

    def short(self):
        return b"" if u.FILE_BACKUP_DIR in self.parts else real(self)

    monkeypatch.setattr(Path, "read_bytes", short)
    assert world.run() == 5
    assert "nothing was written to the token file" in world.err.getvalue()
    world.assert_unchanged()
    assert_no_leak(world, capsys)


# --- the restart report ----------------------------------------------------------------


@pytest.mark.parametrize(("own", "said"), [("old", "yes"), ("other", "no"), (None, "no")])
def test_the_restart_report_says_whether_this_seat_holds_the_old_token(world, own, said,
                                                                       capsys):
    if own == "old":
        world.environ["CLAUDE_CODE_OAUTH_TOKEN"] = world.old
    elif own == "other":
        world.environ["CLAUDE_CODE_OAUTH_TOKEN"] = world.token
    assert world.run() == 0
    out = world.out.getvalue()
    assert out.count(f"this seat holds the old token: {said}") == 2   # plan and outcome
    assert_no_leak(world, capsys)


def test_one_seat_no_seats_and_a_failed_scan(world, capsys):
    world.seats = [u.Seat("only", 5)]
    assert world.run(yes=False) == 3
    assert ("1 live seat keeps the token it launched with until it restarts: only"
            in world.out.getvalue())
    world.seats = []
    assert world.run(yes=False) == 3
    assert "seats:   no live seats; new launches read the new file" in world.out.getvalue()

    def broken():
        raise RuntimeError("no session list")

    assert world.run(seat_scan=broken) == 0               # reported, never a gate
    assert "unknown (the seat scan failed: RuntimeError)" in world.out.getvalue()
    assert_no_leak(world, capsys)


def test_the_restart_report_helpers_name_seats_and_never_a_token():
    assert u.restart_report(lambda: [u.Seat("x", None), u.Seat("y", 3)]) == (
        "2 live seats keep the token they launched with until they restart: x, y")
    token = synthetic_token()
    assert u.holds_token({"CLAUDE_CODE_OAUTH_TOKEN": f" {token}\n"}, token) is True
    assert u.holds_token({"CLAUDE_CODE_OAUTH_TOKEN": synthetic_token()}, token) is False
    assert u.holds_token({}, token) is False
