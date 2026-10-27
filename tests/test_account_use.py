"""`session account use NAME` (ST-22 design-002 §4, do-003).

HARD LIMITS (by design): no Keychain read or write, no real
account switch, no /login, and no run of `security`, `pass` or `claude`.
The autouse fixture below makes any subprocess naming one of them raise,
gives every test an empty CLAUDE_CONFIG_DIR, and the conftest's tmp HOME
holds. The Keychain is always a `FakeKeychain`, the preflight a fake (or
`claude_preflight` with a RECORDING runner that runs nothing), the seat
scan a list. Every token is synthetic (`sk-ant-oat01-TEST-<random>`), and
every test asserts the target token is absent from all captured output and
from every file the test wrote, and that the ledger holds no fingerprint.

Every refusal path also asserts `keychain.writes == []`: nothing reaches
the item before step 6.
"""

from __future__ import annotations

import io
import json
import os
import secrets as pysecrets
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import ANCHOR_SUMMER

from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.account import setuptoken as st
from sessiontools.account import use as u
from sessiontools.start.secrets import SecretError

FORBIDDEN = ("security", "pass", "claude")
AT = ANCHOR_SUMMER


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


# --- the world: a tmp HOME with a registry, token files, a fake Keychain ---------------


class World:
    """A tmp HOME holding accounts `a` (the host's) and `c` (the target),
    each a private `file:` source with a synthetic token, registered with
    its fp; a FakeKeychain on `a`; a fake preflight; a seat scan."""

    def __init__(self, home: Path, *, register_fp: bool = True,
                 keychain: kc.FakeKeychain | None = None) -> None:
        self.home = home
        self.old = synthetic_token()
        self.token = synthetic_token()
        self.tokens = home / "tokens"
        self.tokens.mkdir(mode=0o700)
        registry = reg.Registry()
        for name, email, token in (("a", "ai@example.com", self.old),
                                   ("c", "carol@example.com", self.token)):
            path = self.tokens / name
            path.write_text(token + "\n")
            path.chmod(0o600)
            registry.add(reg.make_account(name, email, [f"file:{path}"],
                                          reg.fingerprint(token) if register_fp else None))
        self.registry_path = reg.save(registry, reg.default_path(home))
        self.item = self.item_for(self.old, extra={"mcpOAuth": {"srv": {"k": "v"}}})
        self.keychain = kc.FakeKeychain(self.item) if keychain is None else keychain
        self.preflights: list[str] = []
        self.seats = [u.Seat("s1", 1_000_000), u.Seat("s2", 70_000)]
        self.out = io.StringIO()
        self.err = io.StringIO()

    @staticmethod
    def item_for(token: str, extra: dict | None = None) -> bytes:
        item = json.loads(kc.render_item(token))
        item[kc.ITEM_KEY]["refreshToken"] = "old-refresh-" + token[-6:]
        item.update(extra or {})
        return json.dumps(item).encode()

    def preflight(self, token: str) -> None:
        self.preflights.append(reg.fingerprint(token))

    def run(self, name: str = "c", *, witness: str | None = "carol", yes: bool = True,
            **overrides) -> int:
        kwargs = {"witness": witness, "yes": yes, "keychain": self.keychain,
                  "preflight": self.preflight, "seat_scan": lambda: self.seats,
                  "home": self.home, "clock": lambda: AT, "by": "session:test-sid",
                  "out": self.out, "err": self.err}
        kwargs.update(overrides)
        return u.use(name, **kwargs)

    @property
    def cfg(self) -> Path:
        return reg.accounts_dir(self.home)

    @property
    def ledger(self) -> Path:
        return self.cfg / u.LEDGER_NAME

    def backups(self) -> list[Path]:
        folder = self.cfg / u.BACKUP_DIR
        return sorted(folder.iterdir()) if folder.is_dir() else []

    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def assert_no_leak(self, capsys) -> None:
        """The target token (and its hex) is in no output and no file under
        HOME; neither token's fp is in the ledger."""
        captured = capsys.readouterr()
        hexed = self.token.encode().hex()
        for text in (self.text(), captured.out, captured.err):
            assert self.token not in text and hexed not in text
            assert self.old not in text
        for path in self.home.rglob("*"):
            if path.is_file() and path.parent != self.tokens:
                data = path.read_bytes()
                assert self.token.encode() not in data, f"token leaked into {path}"
                assert hexed.encode() not in data, f"token hex leaked into {path}"
        if self.ledger.exists():
            body = self.ledger.read_text()
            for token in (self.token, self.old):
                assert reg.fingerprint(token) not in body

    def assert_untouched(self) -> None:
        assert self.keychain.writes == []
        assert self.keychain.blob == self.item


@pytest.fixture
def world(tmp_path) -> World:
    home = tmp_path / "home"
    home.mkdir()
    return World(home)


# --- exit 0: done -------------------------------------------------------------------


def test_use_writes_verifies_and_ledgers(world, capsys):
    assert world.run() == 0
    assert len(world.keychain.writes) == 1
    assert kc.item_fp(world.keychain.blob) == reg.fingerprint(world.token)
    assert world.preflights == [reg.fingerprint(world.token)]
    assert "done: the host is on c" in world.out.getvalue()
    [backup] = world.backups()
    assert backup.read_bytes() == world.item
    assert backup.name == "20301208T123456Z.json"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    [line] = world.ledger.read_text().splitlines()
    assert json.loads(line) == {"at": "2030-12-08T12:34:56+00:00", "from": "a", "to": "c",
                                "by": "session:test-sid", "witness": "carol",
                                "backup": str(backup), "route": "keychain"}
    assert stat.S_IMODE(world.ledger.stat().st_mode) == 0o600
    world.assert_no_leak(capsys)


def test_the_write_merges_keeping_sibling_keys_and_dropping_the_old_oauth(world, capsys):
    """do-002 N1: an `mcpOAuth` map survives a switch; nothing of the old
    account's `claudeAiOauth` (its refresh token) does."""
    assert world.run() == 0
    item = json.loads(world.keychain.blob)
    assert item["mcpOAuth"] == {"srv": {"k": "v"}}
    assert item[kc.ITEM_KEY] == json.loads(kc.render_item(world.token))[kc.ITEM_KEY]
    world.assert_no_leak(capsys)


def test_the_plan_shows_cost_backup_witness_and_source_before_the_write(world, capsys):
    assert world.run() == 0
    out = world.out.getvalue()
    plan, _, after = out.partition("  witness: carol\n")
    assert after, out
    assert "the host moves from a to c" in plan
    assert "2 seats move within ~50 s; their caches cold-start: 1.07M tokens" in plan
    assert f"file:{world.tokens / 'c'} (preflight: runs before the write)" in plan
    # the plan names the backup DIRECTORY; the file is named once it exists
    assert f"backup:  {world.cfg / u.BACKUP_DIR}{os.sep} (written before the write)" in plan
    [backup] = world.backups()
    assert str(backup) not in plan and f"backup:  {backup}" in after
    world.assert_no_leak(capsys)


def test_the_preflight_runs_after_the_gates_and_before_the_backup(world, capsys):
    """Plan, then gates, then preflight, then backup, then write: the
    preflight sees no backup and no write yet, and the plan already shown."""
    seen = []

    def preflight(token):
        seen.append((world.backups(), list(world.keychain.writes),
                     "witness: carol" in world.out.getvalue()))

    assert world.run(preflight=preflight) == 0
    assert seen == [([], [], True)]
    assert len(world.backups()) == 1 and len(world.keychain.writes) == 1
    world.assert_no_leak(capsys)


def test_already_on_name_exits_0_with_no_write_no_backup_no_ledger(world, capsys):
    world.keychain = kc.FakeKeychain(world.item_for(world.token))
    world.item = world.keychain.blob
    assert world.run() == 0
    assert "already on c" in world.out.getvalue()
    world.assert_untouched()
    assert world.backups() == [] and not world.ledger.exists()
    world.assert_no_leak(capsys)


def test_a_second_use_appends_a_second_ledger_row_and_backup(world, capsys):
    assert world.run() == 0
    assert world.run("a") == 0
    rows = [json.loads(line) for line in world.ledger.read_text().splitlines()]
    assert [(row["from"], row["to"]) for row in rows] == [("a", "c"), ("c", "a")]
    assert {path.name for path in world.backups()} == {   # same second: `-2` suffix
        "20301208T123456Z.json", "20301208T123456Z-2.json"}
    assert kc.item_fp(world.keychain.blob) == reg.fingerprint(world.old)
    assert json.loads(world.keychain.blob)["mcpOAuth"] == {"srv": {"k": "v"}}
    # this test's world deliberately put the target token back in a backup
    # (the second backup is of the c item), so check output only
    out = world.text()
    assert world.token not in out and world.old not in out
    capsys.readouterr()


def test_from_is_unknown_when_the_host_item_is_not_in_the_registry(world, capsys):
    stranger = synthetic_token()
    world.keychain = kc.FakeKeychain(world.item_for(stranger))
    assert world.run() == 0
    assert json.loads(world.ledger.read_text())["from"] == "unknown"
    assert stranger not in world.text()
    world.assert_no_leak(capsys)


def test_an_unregistered_fp_verifies_against_the_resolved_token(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    world = World(home, register_fp=False)
    assert world.run() == 0
    assert kc.item_fp(world.keychain.blob) == reg.fingerprint(world.token)
    world.assert_no_leak(capsys)


# --- exit 2: usage --------------------------------------------------------------------


def test_unknown_name_exits_2_naming_the_known_ones(world, capsys):
    assert world.run("z") == 2
    assert "no account named 'z' (known: a, c)" in world.err.getvalue()
    assert world.preflights == []
    world.assert_untouched()
    world.assert_no_leak(capsys)


@pytest.mark.parametrize("yes", [True, False])
@pytest.mark.parametrize("witness", [None, "", "   "])
def test_no_witness_exits_2_after_the_plan_with_nothing_written(world, capsys, witness, yes):
    """The plan is shown first, then the missing witness is named: no
    preflight (no claude call), no backup, no write, no ledger. A missing
    witness outranks a missing --yes."""
    both = io.StringIO()
    assert world.run(witness=witness, yes=yes, out=both, err=both) == 2
    text = both.getvalue()
    plan = text.index("the host moves from a to c")
    missing = text.index("use needs --witness WHO to write")
    assert plan < text.index("  witness: MISSING") < missing
    assert "nothing was written" in text[missing:]
    assert "plan only" not in text
    assert world.preflights == []
    assert world.backups() == [] and not world.ledger.exists()
    world.assert_untouched()
    world.out = both
    world.assert_no_leak(capsys)


@pytest.mark.parametrize("bad", ["token", "x" * 65, "ann\x1b[2J", "an\nn"])
def test_a_malformed_witness_is_refused_before_any_output_and_not_echoed(world, capsys,
                                                                         bad):
    """Review N2: unlike a missing witness, a malformed one (token-shaped,
    over-long, unprintable) is refused before anything runs or prints, so
    its value never reaches the plan."""
    value = world.token if bad == "token" else bad
    calls = []

    def backend(ref):
        calls.append(ref)
        return world.token

    assert world.run(witness=value, backends={"file": backend}) == 2
    assert world.out.getvalue() == ""
    err = world.err.getvalue()
    assert "value is not shown" in err and value.strip() not in err
    assert calls == [] and world.preflights == [] and world.keychain.reads == 0
    assert world.backups() == []
    world.assert_untouched()
    world.assert_no_leak(capsys)


# --- exit 3: the plan, no --yes -----------------------------------------------------------


def test_without_yes_the_plan_is_shown_and_nothing_is_written(world, capsys):
    """A plan-only run writes nothing, not even a backup, and makes no
    claude call (the preflight comes after the gates)."""
    assert world.run(yes=False) == 3
    out = world.out.getvalue()
    assert "plan only: re-run with --yes" in out
    assert "cold-start" in out and "witness: carol" in out
    assert f"backup:  {world.cfg / u.BACKUP_DIR}{os.sep}" in out
    world.assert_untouched()
    assert world.preflights == []
    assert world.backups() == [] and not world.ledger.exists()
    world.assert_no_leak(capsys)


def test_the_keychain_plan_and_done_say_nothing_of_session_start(world, capsys):
    """The `session start` notice is the token-file route's alone: on a
    Keychain host every seat follows the one item, so neither the plan nor
    the done report says a seat might not."""
    assert world.run(yes=False) == 3
    plan = world.out.getvalue()
    assert "plan only: re-run with --yes" in plan
    world.out = io.StringIO()
    assert world.run() == 0
    done = world.out.getvalue()
    assert "done: the host is on c" in done
    for out in (plan, done):
        assert u.START_NOTICE not in out
        assert "session start:" not in out
        assert "inherit: [CLAUDE_CODE_OAUTH_TOKEN]" not in out
        assert "2 seats move within ~50 s" in out
    world.assert_no_leak(capsys)


def test_a_failing_seat_scan_shows_unknown_cost_and_never_gates(world, capsys):
    def boom():
        raise RuntimeError(world.token)         # its text is never shown

    assert world.run(yes=False, seat_scan=boom) == 3
    assert "unknown (the seat scan failed: RuntimeError)" in world.out.getvalue()
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_scan_seats_reads_session_list_rows_and_their_context(monkeypatch):
    """The real seat scan: one Seat per `session list` row, its context
    `used` the cache cost, None where the context is unknown."""
    from types import SimpleNamespace

    from sessiontools import listing
    from sessiontools.window import Fullness

    found = [SimpleNamespace(name="peggy-50", fullness=None),
             SimpleNamespace(name="carol", fullness=None)]
    monkeypatch.setattr(listing, "rows", lambda: found)
    monkeypatch.setattr(listing, "with_context", lambda rows: [
        SimpleNamespace(name=rows[0].name, fullness=Fullness(used=306_000)),
        SimpleNamespace(name=rows[1].name, fullness=Fullness())])
    assert u.scan_seats() == [u.Seat("peggy-50", 306_000), u.Seat("carol", None)]


def test_cost_counts_unread_seats():
    cost = u.cost_of([u.Seat("x", 500), u.Seat("y", None)])
    assert cost == u.Cost(2, 500, 1)
    assert cost.line() == "2 seats move within ~50 s; their caches cold-start: 500 tokens (+1 seat unread)"
    assert u.cost_of([u.Seat("x", 2000)]).line().startswith("1 seat moves")


# --- exit 4: the token --------------------------------------------------------------------


def test_every_source_failing_exits_4_untouched(world, capsys):
    def backend(ref):
        raise SecretError(f"file:{ref} refused: no such file")

    assert world.run(backends={"file": backend}) == 4
    assert "no source resolved" in world.err.getvalue()
    assert world.preflights == [] and world.keychain.reads == 0
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_a_failed_preflight_exits_4_untouched(world, capsys):
    def preflight(token):
        raise u.PreflightError("claude exited 1 and did not reply 'accountok'")

    assert world.run(preflight=preflight) == 4
    assert "preflight of 'c' failed" in world.err.getvalue()
    assert "the host moves from a to c" in world.out.getvalue()   # after the plan
    assert world.backups() == [] and not world.ledger.exists()
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_a_preflight_that_raises_anything_exits_4_without_its_text(world, capsys):
    def preflight(token):
        raise ValueError(token)

    assert world.run(preflight=preflight) == 4
    assert "(ValueError)" in world.err.getvalue()
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_a_source_holding_another_token_than_registered_exits_4(world, capsys):
    (world.tokens / "c").write_text(synthetic_token() + "\n")
    assert world.run() == 4
    assert "fingerprints differ" in world.err.getvalue()
    assert world.preflights == []
    world.assert_untouched()
    world.assert_no_leak(capsys)


# --- exit 5: registry, read, backup, ledger -----------------------------------------------


def test_a_refused_registry_exits_5(world, capsys):
    world.registry_path.chmod(0o644)
    assert world.run() == 5
    assert "not 0600" in world.err.getvalue()
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_a_failed_keychain_read_exits_5_untouched(world, capsys):
    world.keychain.fail_read = True
    assert world.run() == 5
    assert world.backups() == []
    assert world.keychain.writes == []
    world.assert_no_leak(capsys)


def test_a_current_item_with_no_claudeaioauth_exits_5_untouched(world, capsys):
    world.keychain = kc.FakeKeychain(b'{"mcpOAuth": {}}')
    world.item = world.keychain.blob
    assert world.run() == 5
    assert "no claudeAiOauth" in world.err.getvalue()
    assert world.backups() == []
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_a_backup_that_cannot_be_written_exits_5_untouched(world, capsys):
    (world.cfg / u.BACKUP_DIR).write_text("not a dir")
    assert world.run() == 5
    assert "backup to" in world.err.getvalue()
    world.assert_untouched()
    world.assert_no_leak(capsys)


@pytest.mark.parametrize("reread", [b"{}", lambda real: real])
def test_a_backup_that_reads_back_wrong_exits_5_untouched(world, capsys, monkeypatch, reread):
    """Read back short (`{}`), or read back right but parse-check refused:
    exit 5, and the item is never written."""
    real_read = Path.read_bytes

    def read_bytes(self):
        data = real_read(self)
        if u.BACKUP_DIR in self.parts:
            return reread(data) if callable(reread) else reread
        return data

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    if callable(reread):                        # bytes match; the parse-check refuses
        real_parse = kc.parse_item
        monkeypatch.setattr(u.kc, "parse_item", lambda blob: (
            real_parse(blob) if blob != world.item or not world.backups()
            else (_ for _ in ()).throw(kc.KeychainError("item refused (fake)"))))
    assert world.run() == 5
    message = world.err.getvalue()
    assert message.startswith(f"session account use: backup {world.cfg / u.BACKUP_DIR}/")
    assert message.endswith("; nothing was written to the Keychain\n")
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_an_unprivate_ledger_exits_5_before_any_write(world, capsys):
    world.cfg.mkdir(parents=True, exist_ok=True)
    world.ledger.write_text("")
    world.ledger.chmod(0o644)
    assert world.run() == 5
    assert world.err.getvalue() == (f"session account use: ledger {world.ledger} refused: "
                                    "not a private (0600) regular file; nothing was written\n")
    assert world.backups() == []
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_a_ledger_append_that_fails_after_the_switch_says_the_host_moved(world, capsys,
                                                                         monkeypatch):
    def fail(path, row):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(u, "append_ledger", fail)
    assert world.run() == 5
    assert "the host IS now on c" in world.err.getvalue()
    assert kc.item_fp(world.keychain.blob) == reg.fingerprint(world.token)
    world.assert_no_leak(capsys)


# --- exit 6: write or verify failed, restored ---------------------------------------------


def test_a_failed_write_exits_6_and_leaves_the_item_unchanged(world, capsys):
    world.keychain.fail_write = True
    assert world.run() == 6
    message = world.err.getvalue()
    assert "the write failed" in message and "left unchanged" in message
    assert "still on a" in message
    assert world.keychain.blob == world.item
    assert not world.ledger.exists()
    world.assert_no_leak(capsys)


class FlakyKeychain(kc.FakeKeychain):
    """The first write lands mangled (so verify fails); later writes are as
    configured: clean, failing, or mangled too."""

    def __init__(self, blob, *, then: str = "clean") -> None:
        super().__init__(blob)
        self.then = then

    def write(self, blob):
        if not self.writes:
            self.mangle = lambda b: b.replace(b"sk-ant-oat01-TEST-", b"sk-ant-oat01-XXXX-")
        elif self.then == "clean":
            self.mangle = None
        elif self.then == "fail":
            raise kc.KeychainError(f"write {self.describe()}: failed (fake)", kc.EXIT_WRITE)
        super().write(blob)


def test_a_verify_mismatch_restores_the_backup_and_exits_6(world, capsys):
    world.keychain = FlakyKeychain(world.item)
    assert world.run() == 6
    message = world.err.getvalue()
    assert "did not read back as c" in message and "restored from the backup" in message
    assert world.keychain.blob == world.item                 # restored, byte for byte
    assert len(world.keychain.writes) == 2 and world.keychain.writes[1] == world.item
    assert not world.ledger.exists()
    world.assert_no_leak(capsys)


def test_a_failed_read_back_restores_and_exits_6(world, capsys):
    class ReadBackFails(kc.FakeKeychain):
        def read(self):
            if len(self.writes) == 1 and not getattr(self, "_failed", False):
                self._failed = True
                raise kc.KeychainError(f"read {self.describe()}: failed (fake)")
            return super().read()

    world.keychain = ReadBackFails(world.item)
    assert world.run() == 6
    assert "the read-back failed" in world.err.getvalue()
    assert world.keychain.blob == world.item
    world.assert_no_leak(capsys)


def _fits(cli: kc.SecurityCLI, blob: bytes) -> bool:
    try:
        cli.write_script(blob)
    except kc.KeychainError:
        return False
    return True


def _padded(world: World, cli: kc.SecurityCLI, *, current_fits: bool, merged_fits: bool,
            oauth: dict | None = None) -> bytes:
    """A current item, padded with an `mcpOAuth` map until it and the item
    `use` would write sit on the requested sides of `security -i`'s cap."""
    for pad in range(1000, 2100):
        item = {kc.ITEM_KEY: dict(oauth or {kc.TOKEN_KEY: world.old}),
                "mcpOAuth": {"pad": "x" * pad}}
        blob = json.dumps(item, separators=(",", ":")).encode()
        if (_fits(cli, blob), _fits(cli, u.merged_item(blob, world.token))) == (
                current_fits, merged_fits):
            return blob
    raise AssertionError("no pad puts the items on those sides of the cap")


def _recording_cli(calls: list, blob_of) -> kc.SecurityCLI:
    return kc.SecurityCLI("admin", runner=lambda argv, stdin: calls.append(list(argv)) or
                          subprocess.CompletedProcess(argv, 0, blob_of(), b""))


def test_an_item_too_long_for_security_is_never_half_written(world, capsys):
    """A merged item over `security -i`'s line cap (the current one fits)
    is refused before anything runs: left unchanged, exit 6 (not 7)."""
    calls = []
    cli = _recording_cli(calls, lambda: world.item)
    world.item = _padded(world, cli, current_fits=True, merged_fits=False)
    world.keychain = cli
    assert world.run() == 6
    assert "left unchanged" in world.err.getvalue()
    assert all(argv[1:2] != ["-i"] for argv in calls)       # no write ever ran
    world.assert_no_leak(capsys)


def test_a_current_item_too_long_to_restore_exits_5_before_the_backup(world, capsys):
    """A logged-in item carries a refresh token the new item drops, so the
    new item can fit `security -i`'s line while the current one cannot. A
    failed verify could then never restore it (and the manual line would
    hit the same cap): refused at step 5, nothing written, no backup."""
    calls = []
    cli = _recording_cli(calls, lambda: world.item)
    logged_in = {kc.TOKEN_KEY: world.old, "refreshToken": "r" * 108, "expiresAt": 1,
                 "scopes": ["user:inference", "user:profile"], "subscriptionType": "max"}
    world.item = _padded(world, cli, current_fits=False, merged_fits=True, oauth=logged_in)
    world.keychain = cli
    assert world.run() == 5
    message = world.err.getvalue()
    assert "could not be restored" in message and "nothing was written" in message
    assert all(argv[1:2] != ["-i"] for argv in calls)       # no write ever ran
    assert world.backups() == [] and not world.ledger.exists()
    world.assert_no_leak(capsys)


def test_check_restorable_passes_a_keychain_with_no_line_cap(world):
    u.check_restorable(world.keychain, world.item)          # FakeKeychain: no cap


# --- exit 7: restore failed, loud ---------------------------------------------------------


@pytest.mark.parametrize("then", ["fail", "mangled"])
def test_a_failed_restore_exits_7_loud_with_the_backup_and_manual_line(world, capsys, then):
    world.keychain = FlakyKeychain(world.item, then=then)
    assert world.run() == 7
    message = world.err.getvalue()
    [backup] = world.backups()
    assert "RESTORE FAILED" in message
    assert str(backup) in message
    assert f"xxd -p '{backup}'" in message and "security -i" in message
    assert backup.read_bytes() == world.item
    assert not world.ledger.exists()
    world.assert_no_leak(capsys)


def test_the_manual_restore_line_names_the_real_item_and_keeps_the_hex_off_argv(tmp_path):
    line = u.restore_line(kc.SecurityCLI("admin", runner=lambda a, s: None),
                          tmp_path / "b.json")
    assert "'Claude Code-credentials' 'admin'" in line
    assert line.endswith("| /usr/bin/security -i")
    assert "-X ' 'Claude Code-credentials' 'admin'; xxd -p '" in line


# --- the real preflight, on a recording runner ----------------------------------------------


class ClaudeRecorder:
    """Records what `claude_preflight` would run, and runs nothing."""

    def __init__(self, stdout="accountok\n", code=0, raises=None):
        self.stdout, self.code, self.raises = stdout, code, raises
        self.calls = []

    def __call__(self, argv, env, cwd):
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd,
                           "config_listing": os.listdir(env["CLAUDE_CONFIG_DIR"]),
                           "cwd_listing": os.listdir(cwd)})
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.code, self.stdout, "")


def test_preflight_runs_claude_in_a_tmp_cwd_with_an_empty_config_dir(monkeypatch, capsys):
    token = synthetic_token()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-TEST-other")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-TEST-inherited")
    real_config = os.environ["CLAUDE_CONFIG_DIR"]
    recorder = ClaudeRecorder()
    u.claude_preflight(token, runner=recorder)
    [call] = recorder.calls
    assert call["argv"] == ["claude", "-p", "Reply with exactly: accountok", "--max-turns", "1"]
    assert all(token not in arg for arg in call["argv"])
    env = call["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"] != real_config
    assert call["config_listing"] == [] and call["cwd_listing"] == []
    assert call["cwd"] != os.getcwd()
    for path in (env["CLAUDE_CONFIG_DIR"], call["cwd"]):
        assert not os.path.exists(path)           # both tmp dirs removed
    assert token not in os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    out = capsys.readouterr()
    assert token not in out.out + out.err


@pytest.mark.parametrize("recorder", [
    ClaudeRecorder(stdout="something else\n"),
    ClaudeRecorder(code=1),
    ClaudeRecorder(stdout=""),
    ClaudeRecorder(raises=subprocess.TimeoutExpired(["claude"], 120)),
    ClaudeRecorder(raises=FileNotFoundError(2, "No such file or directory")),
])
def test_preflight_refuses_anything_but_accountok(recorder):
    token = synthetic_token()
    with pytest.raises(u.PreflightError) as caught:
        u.claude_preflight(token, runner=recorder)
    assert token not in str(caught.value)
    assert "something else" not in str(caught.value)     # claude's output is not shown


# Claude Code 2.1.299's own lists, read from its binary with `strings -n 6`
# (sha256 ba5ab7b8c195fa03ab8d5bf3915c76153b576a2c40e79a5c46e265e7ed9744b3).
# `oxo` is its subprocess credential scrub; `sq` its provider switches.
CLAUDE_OXO = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_ARTIFACTS_API_TOKEN", "CLAUDE_CODE_MEMORY_API_TOKEN",
    "CLAUDE_CODE_SLACK_TAG_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ALL_INPUTS",
)
CLAUDE_SQ = (
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_GATEWAY", "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_AWS_WORKSPACE_ID",
    "ANTHROPIC_GOOGLE_CLOUD_PROJECT", "ANTHROPIC_GOOGLE_CLOUD_LOCATION",
    "ANTHROPIC_GOOGLE_CLOUD_WORKSPACE_ID", "CLOUD_ML_REGION",
)
#: Also honoured by 2.1.299 (a base URL, the federation path, Bedrock's bearer
#: token), plus the two a parent Claude Code seat sets in its children.
CLAUDE_OTHERS = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
    "AWS_BEARER_TOKEN_BEDROCK", "CLAUDECODE", "TMUX",
)


def test_the_lists_are_the_binarys_counts():
    assert len(set(CLAUDE_OXO)) == 14 and len(set(CLAUDE_SQ)) == 14


def _allowed(key: str) -> bool:
    return (key in st.ENV_ALLOW or key.startswith(st.ENV_ALLOW_PREFIXES)
            or key in u.PREFLIGHT_SET)


def test_no_credential_or_provider_variable_reaches_the_preflight_child(monkeypatch, capsys):
    token = synthetic_token()
    names = CLAUDE_OXO + CLAUDE_SQ + CLAUDE_OTHERS
    for name in names:
        monkeypatch.setenv(name, f"SENTINEL-{name}")
    recorder = ClaudeRecorder()
    u.claude_preflight(token, runner=recorder)
    [call] = recorder.calls
    env = call["env"]
    assert [name for name in names if name in env and name not in u.PREFLIGHT_SET] == []
    assert not any(value.startswith("SENTINEL-") for value in env.values())
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert env["CLAUDE_CONFIG_DIR"] != "SENTINEL-CLAUDE_CONFIG_DIR"
    assert [key for key in env if not _allowed(key)] == []
    out = capsys.readouterr()
    assert token not in out.out + out.err


def test_preflight_env_keeps_the_allowlist_and_lc_and_drops_the_rest():
    base = {"PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C", "LC_CTYPE": "UTF-8",
            "HTTPS_PROXY": "http://proxy", "SSL_CERT_FILE": "/ca.pem",
            "AWS_SECRET_ACCESS_KEY": "x", "VAULT_TOKEN": "x", "GOOGLE_APPLICATION_CREDENTIALS": "x",
            "SOMETHING_ELSE": "x", "CLAUDE_CONFIG_DIR": "/real", "CLAUDE_CODE_OAUTH_TOKEN": "old"}
    env = u.preflight_env("the-token", "/tmp/empty", base)
    assert env == {"PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C",
                   "LC_CTYPE": "UTF-8", "HTTPS_PROXY": "http://proxy",
                   "SSL_CERT_FILE": "/ca.pem", "CLAUDE_CONFIG_DIR": "/tmp/empty",
                   "CLAUDE_CODE_OAUTH_TOKEN": "the-token"}


def test_the_preflight_env_is_an_allowlist_not_a_denylist():
    assert not hasattr(u, "PREFLIGHT_SCRUB")
    assert u.PREFLIGHT_SET == ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")


def test_use_with_the_real_preflight_on_a_recorder_reaches_the_write(world, capsys):
    recorder = ClaudeRecorder()
    assert world.run(preflight=lambda token: u.claude_preflight(token, runner=recorder)) == 0
    assert recorder.calls[0]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == world.token
    world.assert_no_leak(capsys)


def test_the_default_preflight_is_stopped_by_the_guard(world, capsys):
    """Leaving the real runner in place runs nothing: the guard refuses
    `claude`, and `use` turns that into exit 4 with the item untouched."""
    assert world.run(preflight=u.claude_preflight) == 4
    world.assert_untouched()
    world.assert_no_leak(capsys)


# --- the table and the ledger's by -------------------------------------------------------


def test_the_exit_table_is_design_002s():
    assert sorted(u.EXIT_CODES) == [0, 2, 3, 4, 5, 6, 7]
    assert (u.EXIT_USAGE, u.EXIT_UNRESOLVED, u.EXIT_BACKUP, u.EXIT_RESTORED) == (2, 4, 5, 6)


def test_default_by_names_the_session_else_the_user():
    assert u.default_by({"CLAUDE_CODE_SESSION_ID": "abc", "USER": "x"}) == "session:abc"
    assert u.default_by({"USER": "x"}) == "user:x"
    assert u.default_by({}) == "user:unknown"


# --- the oauthAccount note, and the witness length --------------------------------------


def _note(world) -> str:
    lines = world.out.getvalue().splitlines()
    assert lines[1].startswith("  note:    "), lines
    return lines[1]


def test_the_keychain_plan_says_the_display_profile_is_not_changed(world, capsys):
    (world.home / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "ai@example.com", "accountUuid": "uuid-NOT-SHOWN"}}))
    before = (world.home / ".claude.json").read_bytes()
    assert world.run(yes=False) == 3
    assert _note(world) == (
        "  note:    ~/.claude.json names ai@example.com (Claude's /status and 'claude auth "
        "status' read it); use does not change it. The token decides billing: check with "
        "'session account verify'")
    assert "uuid-NOT-SHOWN" not in world.text()
    assert world.run() == 0                         # and a real switch leaves it alone
    assert (world.home / ".claude.json").read_bytes() == before
    world.assert_no_leak(capsys)


def test_the_keychain_plan_with_no_profile_says_it_names_none(world, capsys):
    assert world.run(yes=False) == 3
    assert _note(world).startswith("  note:    ~/.claude.json names no account (Claude's")
    assert "use does not change it" in _note(world)
    world.assert_no_leak(capsys)


def test_the_keychain_plan_with_an_unreadable_profile_says_why(world, capsys):
    (world.home / ".claude.json").write_text("{oops")
    assert world.run(yes=False) == 3
    assert _note(world).startswith("  note:    ~/.claude.json could not be read: not JSON "
                                   "(Claude's")
    world.assert_no_leak(capsys)


def test_a_witness_over_64_characters_is_refused_naming_its_length(world, capsys):
    value = "w" * 65
    assert world.run(witness=value) == 2
    err = world.err.getvalue()
    assert ("--witness is 65 characters; at most 64 (the value is not shown)" in err)
    assert value not in err and world.out.getvalue() == ""
    world.assert_untouched()
    world.assert_no_leak(capsys)


def test_a_witness_of_64_characters_is_accepted(world, capsys):
    assert world.run(witness="w" * 64, yes=False) == 3
    world.assert_no_leak(capsys)
