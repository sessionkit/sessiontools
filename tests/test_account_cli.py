"""`session account add | remove`, the group's `main(argv)`, and `leakscan`
(ST-22 design-002 §1, §7, §9, do-005).

HARD LIMITS (by design): no Keychain read or write, no real
account switch, no /login, and no run of `security`, `pass` or `claude`.
The autouse fixture makes any subprocess naming one of them raise, makes
the default usage fetcher raise, gives every test an empty
CLAUDE_CONFIG_DIR (the conftest's tmp HOME holds) and drops any env token.
The Keychain is always a `FakeKeychain`, a `pass` source a fake backend,
the preflight a fake. Every token is synthetic (`sk-ant-oat01-TEST-<random>`)
and every test asserts no token is in any captured output or in any file
the test wrote, outside the token files themselves (and `use`'s Keychain
backups, which are restore points by design: they hold the item).

The end-to-end test at the bottom drives every verb through
`sessiontools.account.main([...])` and runs `leakscan` over all of it.
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
from settings_support import write_session_settings

import sessiontools.account
from sessiontools.account import cli
from sessiontools.account import keychain as kc
from sessiontools.account import leakscan as ls
from sessiontools.account import registry as reg
from sessiontools.account import status as st
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
def _no_keychain_no_pass_no_claude_no_exporter(monkeypatch, tmp_path_factory):
    """Any subprocess call to security, pass or claude raises; the default
    usage fetcher raises; CLAUDE_CONFIG_DIR is empty; no env token."""
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, _name=name, **kwargs):
            if _argv0(args, kwargs) in FORBIDDEN:
                raise AssertionError(f"test tried to run {_argv0(args, kwargs)!r} "
                                     f"via subprocess.{_name}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)

    def no_exporter(url):
        raise AssertionError(f"test reached the default fetcher for {url}")

    monkeypatch.setattr(st, "fetch_url", no_exporter)
    empty = tmp_path_factory.mktemp("claude-config-empty")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def test_the_guard_refuses_pass_security_claude_and_the_exporter():
    for name in FORBIDDEN:
        with pytest.raises(AssertionError, match="test tried to run"):
            subprocess.run([name, "--version"], capture_output=True, check=False)
    with pytest.raises(AssertionError, match="default fetcher"):
        st.fetch_url(st.USAGE_URL)


def synthetic_token() -> str:
    return "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)


def private_file(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text + "\n")
    path.chmod(mode)
    return path


# --- the world -------------------------------------------------------------------------


class World:
    """A tmp HOME, three synthetic tokens (`a` and `c` in private files,
    `p` behind a fake `pass` backend that counts its calls), an empty
    registry, a FakeKeychain on `start`'s token, and captured streams."""

    def __init__(self, home: Path, *, start: str = "a") -> None:
        self.home = home
        self.tokens_dir = home / "tokens"
        self.tokens_dir.mkdir(mode=0o700)
        self.tokens = {name: synthetic_token() for name in ("a", "c", "p", "stranger")}
        self.files = {name: private_file(self.tokens_dir / name, self.tokens[name])
                      for name in ("a", "c", "stranger")}
        self.pass_store = {"anthropic/peggy": self.tokens["p"],
                           "anthropic/carol": self.tokens["c"]}
        self.pass_calls: list[str] = []
        self.backends = {"file": reg.file_backend, "pass": self.fake_pass}
        self.keychain = kc.FakeKeychain(kc.render_item(self.tokens[start]))
        self.preflights: list[str] = []
        self.out = io.StringIO()
        self.err = io.StringIO()

    def fake_pass(self, ref: str) -> str:
        self.pass_calls.append(ref)
        if ref not in self.pass_store:
            raise SecretError(f"pass:{ref} is not in the store (fake)")
        return self.pass_store[ref]

    def preflight(self, token: str) -> None:
        self.preflights.append(reg.fingerprint(token))

    def fetch(self, url: str) -> str:
        return ('claude_plan_usage_percent{account="default",window="5h"} 40\n'
                'claude_plan_window_reset_timestamp_seconds{account="default",'
                'window="5h"} 1907254800\n')

    @property
    def cfg(self) -> Path:
        return reg.accounts_dir(self.home)

    @property
    def registry_path(self) -> Path:
        return reg.default_path(self.home)

    def run(self, *argv: str, **overrides) -> int:
        kwargs = {"keychain": self.keychain, "preflight": self.preflight,
                  "seat_scan": lambda: [u.Seat("s1", 900_000), u.Seat("s2", 100_000)],
                  "fetch": self.fetch, "backends": self.backends, "env": {},
                  "home": self.home, "clock": lambda: AT, "by": "session:test-sid",
                  "out": self.out, "err": self.err}
        kwargs.update(overrides)
        return sessiontools.account.main(list(argv), **kwargs)

    def add(self, name: str, *extra: str) -> int:
        email = {"a": "ai@example.com", "c": "carol@example.com",
                 "p": "peggy@example.com"}.get(name, f"{name}@example.com")
        return self.run("add", name, "--email", email, *extra)

    def add_three(self) -> None:
        assert self.add("a", "--source", f"file:{self.files['a']}") == 0
        assert self.add("c", "--source", f"file:{self.files['c']}",
                        "--source", "pass:anthropic/carol") == 0
        assert self.add("p", "--source", "pass:anthropic/peggy", "--no-fp") == 0

    def take(self) -> str:
        """Everything printed to the captured streams so far; then clear."""
        text = self.out.getvalue() + self.err.getvalue()
        self.out.seek(0), self.out.truncate()
        self.err.seek(0), self.err.truncate()
        return text

    def registry(self) -> reg.Registry:
        return reg.load(self.registry_path)

    def assert_no_leak(self, *texts: str) -> None:
        """No token, its hex, or its fingerprint in any text given, or
        anywhere in the captured streams; no token or hex in any file under
        HOME but the token files and `use`'s backups."""
        texts = (*texts, self.out.getvalue(), self.err.getvalue())
        for token in self.tokens.values():
            hexed = token.encode().hex()
            for text in texts:
                assert token not in text and hexed not in text
                assert reg.fingerprint(token) not in text
            for path in self.home.rglob("*"):
                if path.is_file() and not self._exempt(path):
                    data = path.read_bytes()
                    assert token.encode() not in data, f"token leaked into {path}"
                    assert hexed.encode() not in data, f"token hex leaked into {path}"

    def _exempt(self, path: Path) -> bool:
        return path.parent in (self.tokens_dir, self.cfg / u.BACKUP_DIR)

    def written(self) -> list[Path]:
        """Every file under HOME this world's verbs wrote."""
        return sorted(path for path in self.home.rglob("*")
                      if path.is_file() and not self._exempt(path))


@pytest.fixture
def world(tmp_path) -> World:
    home = tmp_path / "home"
    home.mkdir()
    return World(home)


# --- add ---------------------------------------------------------------------------------


def test_add_a_file_source_registers_it_with_its_fp_mode_0600(world, capsys):
    assert world.add("a", "--source", f"file:{world.files['a']}") == 0
    [account] = world.registry().accounts
    assert account == reg.Account("a", "ai@example.com",
                                  (reg.Source("file", str(world.files["a"])),),
                                  reg.fingerprint(world.tokens["a"]))
    assert stat.S_IMODE(world.registry_path.stat().st_mode) == 0o600
    out = world.out.getvalue()
    assert out.startswith("added a (ai@example.com): file (free); fp known")
    assert str(world.registry_path) in out
    world.assert_no_leak(*capsys.readouterr())


def test_add_resolves_once_through_the_first_source_that_works(world, capsys):
    """c lists a missing file first, then two pass entries: the file fails,
    the first pass entry resolves, the second is never asked (one touch)."""
    world.pass_store["anthropic/spare"] = world.tokens["c"]
    missing = world.tokens_dir / "absent"
    assert world.add("c", "--source", f"file:{world.files['c']}",
                     "--source", "pass:anthropic/carol",
                     "--source", "pass:anthropic/spare") == 0
    assert world.pass_calls == []                      # the free file won
    assert world.registry().get("c").fp == reg.fingerprint(world.tokens["c"])
    world.run("remove", "c")
    # a pass source behind a file that is not there yet: refused before any touch
    assert world.add("c", "--source", f"file:{missing}",
                     "--source", "pass:anthropic/carol") == 2
    assert world.pass_calls == []
    assert world.add("c", "--source", "pass:anthropic/carol",
                     "--source", "pass:anthropic/spare") == 0
    assert world.pass_calls == ["anthropic/carol"]
    world.assert_no_leak(*capsys.readouterr())


def test_add_no_fp_resolves_nothing_and_registers_fp_unknown(world, capsys):
    calls = []
    backends = {"file": lambda ref: calls.append(ref) or "x",
                "pass": lambda ref: calls.append(ref) or "x"}
    assert world.run("add", "p", "--email", "peggy@example.com",
                     "--source", "pass:anthropic/peggy", "--no-fp", backends=backends) == 0
    assert calls == []
    assert world.registry().get("p").fp is None
    assert "pass (key touch); fp unknown" in world.out.getvalue()
    world.assert_no_leak(*capsys.readouterr())


def test_add_keeps_registry_order(world):
    world.add_three()
    assert world.registry().names() == ["a", "c", "p"]


def test_a_duplicate_name_exits_2_untouched_and_resolves_nothing(world, capsys):
    assert world.add("c", "--source", "pass:anthropic/carol") == 0
    before = world.registry_path.read_bytes()
    world.pass_calls.clear()
    assert world.add("c", "--source", f"file:{world.files['c']}") == 2
    assert "already registered" in world.err.getvalue()
    assert world.registry_path.read_bytes() == before
    assert world.pass_calls == []
    world.assert_no_leak(*capsys.readouterr())


@pytest.mark.parametrize("mode, why", [(0o644, "mode 0644, not 0600"),
                                       (0o640, "mode 0640, not 0600")])
def test_a_file_source_that_is_not_private_exits_2(world, mode, why, capsys):
    loose = private_file(world.tokens_dir / "loose", synthetic_token(), mode)
    assert world.add("a", "--source", f"file:{loose}", "--no-fp") == 2
    assert why in world.err.getvalue()
    assert not world.registry_path.exists()
    world.assert_no_leak(*capsys.readouterr())


def test_a_file_source_that_is_missing_or_a_directory_exits_2(world, capsys):
    assert world.add("a", "--source", f"file:{world.tokens_dir / 'nope'}") == 2
    assert "no such file" in world.err.getvalue()
    assert world.add("a", "--source", f"file:{world.tokens_dir}", "--no-fp") == 2
    assert "not a regular file" in world.err.getvalue()
    assert not world.registry_path.exists()


def test_a_file_source_owned_by_someone_else_exits_2(world, monkeypatch):
    uid = os.getuid()
    monkeypatch.setattr(reg.os, "getuid", lambda: uid + 1)
    assert world.add("a", "--source", f"file:{world.files['a']}") == 2
    assert "not owned by this user" in world.err.getvalue()


@pytest.mark.parametrize("source, says", [
    ("gpg:thing", "unknown backend 'gpg'"),
    ("nocolon", "is not `backend:ref`"),
    ("file:", "is not `backend:ref`"),
])
def test_a_bad_source_exits_2(world, source, says):
    assert world.add("a", "--source", source) == 2
    assert says in world.err.getvalue()
    assert not world.registry_path.exists()


def test_a_token_pasted_as_a_source_or_email_exits_2_and_is_not_echoed(world, capsys):
    token = world.tokens["a"]
    assert world.add("a", "--source", f"file:{token}") == 2
    assert world.run("add", "a", "--email", token, "--source", "pass:x") == 2
    assert world.run("add", "a", "--email", f"me@{token}", "--source", "pass:x") == 2
    assert not world.registry_path.exists()
    world.assert_no_leak(*capsys.readouterr())


@pytest.mark.parametrize("name", ["", "-x", "a b", "x" * 33, "a\n"])
def test_a_bad_name_exits_2(world, name):
    assert world.run("add", name, "--email", "e@x", "--source", "pass:x", "--no-fp") == 2
    assert not world.registry_path.exists()


def test_add_with_no_source_resolving_exits_4_and_writes_nothing(world, capsys):
    assert world.add("p", "--source", "pass:anthropic/gone",
                     "--source", "pass:anthropic/also-gone") == 4
    err = world.err.getvalue()
    assert "no source resolved" in err and "pass:anthropic/gone" in err
    assert world.pass_calls == ["anthropic/gone", "anthropic/also-gone"]
    assert not world.registry_path.exists()
    world.assert_no_leak(*capsys.readouterr())


def test_one_token_under_two_names_exits_2_naming_the_other(world, capsys):
    assert world.add("c", "--source", f"file:{world.files['c']}") == 0
    before = world.registry_path.read_bytes()
    assert world.run("add", "c2", "--email", "carol@example.com",
                     "--source", "pass:anthropic/carol") == 2
    assert "already registered as 'c'" in world.err.getvalue()
    assert world.registry_path.read_bytes() == before
    world.assert_no_leak(*capsys.readouterr())


def test_the_one_token_message_names_the_source_that_resolved(world, capsys):
    """The first source (an empty private file) fails; the pass entry
    resolves to c's token. The refusal names the pass entry, not the file."""
    assert world.add("c", "--source", f"file:{world.files['c']}") == 0
    empty = private_file(world.tokens_dir / "empty", "")
    assert world.run("add", "c2", "--email", "carol@example.com",
                     "--source", f"file:{empty}", "--source", "pass:anthropic/carol") == 2
    err = world.err.getvalue()
    assert "the token at pass:anthropic/carol is already registered as 'c'" in err
    assert str(empty) not in err
    world.assert_no_leak(*capsys.readouterr())


def test_add_to_a_refused_registry_exits_5_and_leaves_it_byte_identical(world):
    world.cfg.mkdir(parents=True)
    private_file(world.registry_path, "{not json")
    before = world.registry_path.read_bytes()
    assert world.add("a", "--source", f"file:{world.files['a']}") == 5
    assert "not valid JSON" in world.err.getvalue()
    assert world.registry_path.read_bytes() == before


def test_add_refuses_a_symlinked_registry_and_leaves_the_link(world):
    """do-001 review O1: `save`'s rename would replace the link with a file."""
    world.cfg.mkdir(parents=True)
    real = world.home / "elsewhere.json"
    reg.save(reg.Registry(), real)
    world.registry_path.symlink_to(real)
    assert world.add("a", "--source", f"file:{world.files['a']}") == 5
    assert "symlink" in world.err.getvalue()
    assert world.registry_path.is_symlink()
    assert reg.load(real).accounts == []


def test_add_and_remove_default_to_the_users_home(world, monkeypatch):
    monkeypatch.setenv("HOME", str(world.home))
    out = io.StringIO()
    assert cli.add("a", email="ai@example.com", sources=[f"file:{world.files['a']}"],
                   out=out) == 0
    assert reg.load(world.registry_path).names() == ["a"]
    assert cli.remove("a", keychain=world.keychain, out=out) == 3   # the host is on a


# --- remove ------------------------------------------------------------------------------


def test_remove_deletes_the_entry_only(world, capsys):
    world.add_three()
    world.take()
    assert world.run("remove", "c") == 0
    assert world.registry().names() == ["a", "p"]
    assert world.files["c"].read_text().strip() == world.tokens["c"]   # untouched
    out = world.take()
    assert "removed c (carol@example.com)" in out and "no token" in out
    assert world.pass_calls == []                                      # nothing resolved
    world.assert_no_leak(out, *capsys.readouterr())


def test_remove_refuses_the_hosts_account_exit_3_untouched(world, capsys):
    world.add_three()
    before = world.registry_path.read_bytes()
    assert world.run("remove", "a") == 3
    err = world.err.getvalue()
    assert "'a' is the account the host is on" in err and st.VIA_KEYCHAIN in err
    assert world.registry_path.read_bytes() == before
    world.assert_no_leak(*capsys.readouterr())


def test_remove_reads_the_ledger_when_host_read_is_off(world):
    world.add_three()
    private_file(st.config_path(world.home), json.dumps({"host_read": False}))
    private_file(world.cfg / st.LEDGER_NAME, json.dumps({"to": "c"}))
    reads = world.keychain.reads
    assert world.run("remove", "c") == 3
    assert st.VIA_LEDGER in world.err.getvalue()
    assert world.keychain.reads == reads                # the item was not read
    assert world.run("remove", "a") == 0


def test_remove_when_the_host_is_not_known_removes_and_says_so(world, capsys):
    world.add_three()
    world.keychain.fail_read = True
    world.take()
    assert world.run("remove", "a") == 0
    out = world.take()
    assert "the host account was not known (Keychain: unreadable" in out
    assert world.registry().names() == ["c", "p"]
    world.keychain.fail_read = False
    world.keychain.blob = kc.render_item(world.tokens["stranger"])
    assert world.run("remove", "c") == 0
    assert "not in registry" in world.take()
    world.assert_no_leak(*capsys.readouterr())


def test_remove_refuses_an_fp_unknown_account_the_ledger_says_the_host_is_on(world, capsys):
    """p is registered --no-fp; `use p` moves the host there and writes the
    ledger, but the Keychain cannot name an unknown fp. The ledger can."""
    world.add_three()
    assert world.run("use", "p", "--witness", "carol", "--yes") == 0
    before = world.registry_path.read_bytes()
    world.take()
    assert world.run("remove", "p") == 3
    err = world.take()
    assert "'p' is the account the host is on" in err and st.VIA_LEDGER in err
    assert "fp is unknown" in err
    assert world.registry_path.read_bytes() == before
    # a known-fp account is ruled out by the Keychain itself: removable
    assert world.run("remove", "c") == 0
    # the ledger naming ANOTHER account does not block p's neighbours either
    assert world.run("use", "a", "--witness", "carol", "--yes") == 0
    assert world.run("remove", "p") == 0
    world.assert_no_leak(*capsys.readouterr())


def test_remove_an_fp_unknown_account_with_no_ledger_row_removes_and_says_so(world):
    """The Keychain item is a stranger's and nothing pins it on p: removed."""
    world.add_three()
    world.keychain.blob = kc.render_item(world.tokens["stranger"])
    world.take()
    assert world.run("remove", "p") == 0
    assert "the host account was not known" in world.take()


def test_remove_an_unknown_name_exits_2_naming_the_known(world):
    world.add_three()
    assert world.run("remove", "z") == 2
    assert "no account named 'z' (known: a, c, p)" in world.err.getvalue()


def test_remove_from_a_refused_registry_or_config_exits_5(world):
    assert world.run("remove", "a") == 2                # no registry: nothing to remove
    world.add_three()
    private_file(st.config_path(world.home), "[]")
    before = world.registry_path.read_bytes()
    assert world.run("remove", "c") == 5
    assert "top level must be an object" in world.err.getvalue()
    assert world.registry_path.read_bytes() == before


# --- the group ---------------------------------------------------------------------------


def test_main_is_exported_from_the_package():
    assert sessiontools.account.main is cli.main
    assert sessiontools.account.__all__ == ["main"]


def test_help_is_exit_0_and_names_every_verb(world, capsys):
    assert world.run("--help") == 0
    out = world.out.getvalue()
    for verb in ("list", "status", "use", "add", "remove"):
        assert verb in out
    assert "cold-start" in out and "Exit codes" in out
    assert capsys.readouterr() == ("", "")              # the out seam, not sys.stdout


@pytest.mark.parametrize("argv", [[], ["bogus"], ["use"], ["remove"],
                                  ["add", "a", "--source", "pass:x"],
                                  ["add", "a", "--email", "e@x"],
                                  ["status", "--nope"]])
def test_usage_errors_are_exit_2(world, argv, capsys):
    assert world.run(*argv) == 2
    assert "usage: session account" in world.err.getvalue()
    assert capsys.readouterr() == ("", "")              # the err seam, not sys.stderr


def test_help_names_the_token_file_route(world, capsys):
    """The group's help says a token-file host is named by the setting,
    has no default, and keeps its live seats on their launch token."""
    assert world.run("--help") == 0
    out = " ".join(world.out.getvalue().split())
    assert "account.token_file (no default)" in out
    assert "never runs `security`" in out
    assert "keep the token they launched with until they restart" in out
    assert "restored the item or file" in out
    assert capsys.readouterr() == ("", "")


def test_help_says_a_session_start_seat_does_not_follow_the_token_file(world, capsys):
    """The group's help says what a `session start` seat signs in as on a
    token-file host, and how to carry the token."""
    assert world.run("--help") == 0
    out = " ".join(world.out.getvalue().split())
    assert ("A seat `session start` launches does NOT follow the file unless its agent "
            "file says inherit: [CLAUDE_CODE_OAUTH_TOKEN]") in out
    assert "signs in from Claude's stored credentials, and `status` says so" in out
    assert capsys.readouterr() == ("", "")


def test_status_through_main_on_a_token_file_host_with_no_env_token(world, capsys):
    """End to end through `main`: the setting puts the host on the file, the
    env seam holds no token, and both renderings say the seat is not on it."""
    world.add_three()
    world.take()
    write_session_settings(world.home, {"account": {"token_file": str(world.files["c"])}})
    assert world.run("status") == 0
    text = world.take()
    assert text.splitlines()[0] == "host:  c (carol@example.com)  (token file, fp match)"
    assert text.splitlines()[1].startswith("seat:  NOT on the token file")
    assert "follows the host" not in text
    assert world.run("status", "--json") == 0
    data = json.loads(world.take())
    assert data["host"]["name"] == "c"
    assert data["seat"] == {"name": None, "via": st.VIA_NOT_FILE}
    assert world.keychain.reads == 0
    world.assert_no_leak(text, *capsys.readouterr())


def test_use_without_a_witness_is_exit_2_through_main(world, capsys):
    world.add_three()
    assert world.run("use", "c") == 2
    assert "witness" in world.err.getvalue()
    assert "the host moves from a to c" in world.out.getvalue()   # the plan came first
    assert world.keychain.writes == [] and world.preflights == []
    assert not (world.cfg / u.BACKUP_DIR).exists()
    world.assert_no_leak(*capsys.readouterr())


def test_use_help_names_the_witness_in_the_usage_line(world, capsys):
    """--witness is optional to argparse (so `use` can show the plan
    first), but the usage line says it is needed, and the description
    names both sign-in routes."""
    assert world.run("use", "--help") == 0
    out = world.out.getvalue()
    usage = out.splitlines()[0]
    assert usage == "usage: session account use NAME --witness WHO [--yes]"
    assert "[--witness" not in out
    assert "Keychain item" in out and "account.token_file" in out
    assert "--witness WHO" in out and "shows the plan and exits 2" in " ".join(out.split())
    assert capsys.readouterr() == ("", "")


def test_status_flags_reach_status(world, capsys):
    world.add_three()
    world.take()
    assert world.run("status", "--json") == 0
    found = json.loads(world.take())
    assert found["host"] == {"name": "a", "email": "ai@example.com",
                             "via": st.VIA_KEYCHAIN}
    assert found["seat"]["via"] == st.VIA_FOLLOWS
    assert "2030-06-09" in json.dumps(found["usage"])       # resets are the default
    assert world.run("status", "--json", "--no-resets") == 0
    assert "resets" not in json.dumps(json.loads(world.take())["usage"])
    assert world.run("status", "--no-resets") == 0
    assert "reset" not in world.take()
    assert world.run("status") == 0
    default_text = world.take()
    assert "reset" in default_text
    assert world.run("status", "--resets") == 0             # a no-op, still accepted
    assert world.take() == default_text
    world.assert_no_leak(*capsys.readouterr())


def test_status_names_an_env_token_seat_from_the_env_seam(world, capsys):
    world.add_three()
    world.take()
    assert world.run("status", env={"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["c"]}) == 0
    text = world.take()
    assert st.VIA_ENV in text and "carol@example.com" in text
    world.assert_no_leak(text, *capsys.readouterr())


# --- leakscan ----------------------------------------------------------------------------


def test_leakscan_reports_a_registry_token_by_path_line_never_value():
    token, other = synthetic_token(), synthetic_token()
    fps = {reg.fingerprint(token): "c"}
    text = f"line one\nAuthorization: {token}\nok\nx={other} y\n"
    hits = ls.scan_text(text, fps, "out.txt")
    assert hits == [ls.Hit("out.txt", 2, "c"), ls.Hit("out.txt", 4, None)]
    rendered = ls.render(ls.Report(tuple(hits), (), 1))
    assert "out.txt:2: token of account 'c' (registry fp match)" in rendered
    assert "out.txt:4: token-shaped string (not in registry)" in rendered
    for value in (token, other):
        assert value not in rendered and reg.fingerprint(value) not in rendered


def test_leakscan_finds_a_hex_encoded_token():
    """`security -i` is fed the item as hex: a leaked write script holds it so."""
    token = synthetic_token()
    blob = kc.render_item(token).hex()
    [hit] = ls.scan_text(f"first\nadd-generic-password -X {blob}\n",
                         {reg.fingerprint(token): "a"})
    assert (hit.line, hit.account) == (2, "a")
    assert ls.scan_text(f"-X {blob.upper()}", {reg.fingerprint(token): "a"})[0].account == "a"


def test_leakscan_shape_needs_the_kind_digits_and_twenty_chars():
    assert ls.scan_text('"sk-ant-oat01-TEST-" + suffix', {}) == []
    assert ls.scan_text("sk-ant-oat01-" + "x" * 19, {}) == []
    assert len(ls.scan_text("sk-ant-oat01-" + "x" * 20, {})) == 1
    assert len(ls.scan_text("sk-ant-ort01-" + "y" * 40, {})) == 1      # a refresh token
    assert ls.scan_text("sk-ant-api03-" + "z" * 40, {}) == []          # an API key, not ours


def test_leakscan_walks_dirs_skips_vcs_and_symlinks_reads_binary(tmp_path):
    token = synthetic_token()
    fps = {reg.fingerprint(token): "p"}
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "bin.dat").write_bytes(b"\x00\xff\n\n" + token.encode() + b"\x00")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "obj").write_text(token)
    (tmp_path / "link").symlink_to(tmp_path / "a" / "bin.dat")
    (tmp_path / "clean.txt").write_text("nothing here\n")
    report = ls.scan_paths([tmp_path], fps)
    assert report.hits == (ls.Hit(str(tmp_path / "a" / "bin.dat"), 3, "p"),)
    assert report.scanned == 2 and report.unreadable == ()
    single = ls.scan_paths([tmp_path / ".git" / "obj"], fps)         # named: scanned
    assert single.known == [ls.Hit(str(tmp_path / ".git" / "obj"), 1, "p")]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_leakscan_reports_an_unreadable_file_and_goes_on(tmp_path):
    shut = tmp_path / "shut"
    shut.write_text("x")
    shut.chmod(0)
    (tmp_path / "open").write_text("y")
    try:
        report = ls.scan_paths([tmp_path], {})
    finally:
        shut.chmod(0o600)
    assert report.scanned == 1
    assert [path for path, _ in report.unreadable] == [str(shut)]
    assert f"{shut}: unreadable" in ls.render(report)


def test_leakscan_main_exit_codes(tmp_path, capsys):
    token, other = synthetic_token(), synthetic_token()
    rpath = tmp_path / "accounts.json"
    reg.save(reg.Registry([reg.make_account("c", "c@example.com", ["pass:x"],
                                            reg.fingerprint(token))]), rpath)
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "f").write_text(f"only a stranger: {other}\n")
    out, err = io.StringIO(), io.StringIO()
    assert ls.main(["--registry", str(rpath), str(clean)], out=out, err=err) == 0
    assert ls.main(["--registry", str(rpath), "--strict", str(clean)], out=out, err=err) == 1
    leaky = tmp_path / "leaky"
    leaky.mkdir()
    (leaky / "log").write_text(f"a\nb\n{token}\n")
    assert ls.main(["--registry", str(rpath), str(leaky)], out=out, err=err) == 1
    assert f"{leaky / 'log'}:3: token of account 'c'" in out.getvalue()
    bad = tmp_path / "bad.json"
    private_file(bad, "{")
    assert ls.main(["--registry", str(bad), str(clean)], out=out, err=err) == 5
    assert ls.main([], out=out, err=err) == 2
    text = out.getvalue() + err.getvalue() + "".join(capsys.readouterr())
    for value in (token, other):
        assert value not in text and reg.fingerprint(value) not in text


def test_the_repo_holds_no_token_shaped_string():
    """What verify-001 runs, strict: no token-shaped string at all in the
    source or the tests (the tests build theirs at run time)."""
    root = Path(__file__).resolve().parent.parent
    report = ls.scan_paths([root / "src", root / "tests"], {})
    assert report.hits == (), ls.render(report)


# --- end to end: every verb through sessiontools.account.main ----------------------------


def test_every_verb_end_to_end_on_fakes_leaks_no_token(world, capsys):
    """list, add (fp, multi-source, --no-fp, duplicate), list, status
    (text, --json, --no-resets), use (plan, --yes, back), remove (the host's,
    refused; another's, done), then list. All output and every file written
    are leakscanned, strict, against every synthetic token's fingerprint."""
    transcript: list[tuple[list[str], int, str]] = []

    def step(*argv: str, expect: int) -> str:
        code = world.run(*argv)
        text = world.take()
        transcript.append((list(argv), code, text))
        assert code == expect, (argv, code, text)
        return text

    assert "no accounts registered" in step("list", expect=0)
    step("add", "a", "--email", "ai@example.com", "--source", f"file:{world.files['a']}",
         expect=0)
    step("add", "c", "--email", "carol@example.com", "--source", f"file:{world.files['c']}",
         "--source", "pass:anthropic/carol", expect=0)
    step("add", "p", "--email", "peggy@example.com", "--source", "pass:anthropic/peggy",
         "--no-fp", expect=0)
    assert "already registered" in step("add", "a", "--email", "ai@example.com",
                                        "--source", f"file:{world.files['a']}", expect=2)
    assert world.pass_calls == []                       # every resolve so far was free

    listed = step("list", expect=0)
    rows = listed.splitlines()
    assert [row.split()[0] for row in rows] == ["a", "c", "p"]
    assert rows[0].endswith(st.HOST_MARK)
    assert "file (free), pass (key touch)" in rows[1] and "fp unknown" in rows[2]

    assert "host:  a (ai@example.com)  (Keychain, fp match)" in step("status", expect=0)
    found = json.loads(step("status", "--json", expect=0))
    assert found["host"]["name"] == "a" and found["seat"]["name"] == "a"

    plan = step("use", "c", "--witness", "carol", expect=3)
    assert "the host moves from a to c" in plan and "2 seats move" in plan
    assert world.keychain.writes == []
    done = step("use", "c", "--witness", "carol", "--yes", expect=0)
    assert "done: the host is on c" in done
    assert kc.item_fp(world.keychain.blob) == reg.fingerprint(world.tokens["c"])
    assert "host:  c (carol@example.com)  (Keychain, fp match)" in step("status", expect=0)
    assert step("list", expect=0).splitlines()[1].endswith(st.HOST_MARK)

    assert "the account the host is on" in step("remove", "c", expect=3)
    step("use", "a", "--witness", "carol", "--yes", expect=0)
    assert kc.item_fp(world.keychain.blob) == reg.fingerprint(world.tokens["a"])
    assert "removed c" in step("remove", "c", expect=0)
    assert "removed p" in step("remove", "p", expect=0)
    assert [row.split()[0] for row in step("list", expect=0).splitlines()] == ["a"]
    step("add", "p", "--email", "peggy@example.com", "--source", "pass:anthropic/peggy",
         expect=0)
    assert world.pass_calls == ["anthropic/peggy"]        # one touch, for p's fp

    # the ledger: two rows, witnessed, with names, never a token or an fp
    ledger = [json.loads(line) for line in
              (world.cfg / u.LEDGER_NAME).read_text().splitlines()]
    assert [(row["from"], row["to"], row["witness"]) for row in ledger] == [
        ("a", "c", "carol"), ("c", "a", "carol")]
    # the plan-only `use c` made no claude call: the preflight is after the gates
    assert world.preflights == [reg.fingerprint(world.tokens["c"]),
                                reg.fingerprint(world.tokens["a"])]

    # leakscan, strict, over all output and every file written
    fps = {reg.fingerprint(token): name for name, token in world.tokens.items()}
    printed = "\n".join(text for _, _, text in transcript) + "".join(capsys.readouterr())
    assert ls.scan_text(printed, fps, "<output>") == []
    written = world.written()
    assert world.registry_path in written and world.cfg / u.LEDGER_NAME in written
    report = ls.scan_paths(written, fps)
    assert report.hits == () and report.scanned == len(written), ls.render(report)
    world.assert_no_leak(printed)
    assert stat.S_IMODE(world.registry_path.stat().st_mode) == 0o600

    # positive control: the backups hold the item by design (0600 restore
    # points), and leakscan finds those tokens there, by place and name only
    # (two: the plan-only `use c` leaves no backup, only the two switches do)
    backups = sorted((world.cfg / u.BACKUP_DIR).iterdir())
    assert len(backups) == 2
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups)
    found_in_backups = ls.scan_paths(backups, fps)
    assert sorted(hit.account for hit in found_in_backups.known) == ["a", "c"]
    assert {Path(hit.path) for hit in found_in_backups.hits} == set(backups)
    rendered = ls.render(found_in_backups)
    for token in world.tokens.values():
        assert token not in rendered and reg.fingerprint(token) not in rendered
