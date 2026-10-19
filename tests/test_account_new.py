"""`session account new NAME --email E` (ST-159 do-005, design §2, §6, §8).

HARD LIMITS (ST-159 slice notes): no real `claude setup-token`, no real
`passage`, `age` or `pass`, no store, no Keychain, no sign-in. Every token
is synthetic (`sk-ant-oat01-TEST-<random>`).

- The autouse guard refuses any subprocess whose argv[0] is `security`,
  `pass`, `passage`, `age` or `claude` unless it resolves (real path) inside
  this test's own tmp folder (design §8), and records every argv, so a test
  can assert the token reached none (SC2: all spawns).
- End to end (SC2, SC3): the REAL capture (`run_setup_token`, the stub
  `claude` on a pty, do-004's), the REAL `PassageCLI` over the stub
  `passage` and its scratch store (do-002's: it keeps base64, so plaintext
  on disk is a leak), the REAL preflight (the stub's `-p`, through
  `narrowing_runner`), and the real `add`. TMPDIR is a fresh folder, and is
  scanned after the run.
- Every failure path runs on fakes (`FakeTokenStore`, a fake capture) with
  the person's terminal a pty pair, so steps 1-9 are each pinned fast.

"No token character" is asserted as design §8 (round 3) states it: no 8
characters in a row of the token's body, anywhere.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest
from settings_support import write_session_settings
from test_account_setuptoken import (
    STUB,
    WATCHDOG_S,
    Person,
    Stub,
    b64,
    flow,
    paste,
    synthetic_token,
    windows,
)
from test_account_tokenstore import Passage

from sessiontools.account import cli
from sessiontools.account import new as nw
from sessiontools.account import registry as reg
from sessiontools.account import setuptoken as st
from sessiontools.account import tokenstore as ts
from sessiontools.account import use as u
from sessiontools.account.tokenstore import FakeTokenStore, StoreError

FORBIDDEN = ("security", "pass", "passage", "age", "claude")
EMAIL = "new@example.com"
ENTRY = nw.entry_for(EMAIL)
SOURCE = f"passage:{ENTRY}"

#: What the guard saw, this test: every argv any subprocess call was given.
ARGVS: list[list[str]] = []

# The stub `claude`'s `-p`, for the preflight: it leaves claude-like state in
# its config dir and cwd (as a real run would), then logs its argv, its env's
# NAMES, the tmp root it ran under, and a snapshot of every file in that root
# while it existed (R-4), and answers as the plan's `preflight` step says.
PREFLIGHT_BRANCH = '''if sys.argv[1:2] == ["-p"]:
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    root = os.path.dirname(cfg)
    with open(os.path.join(cfg, ".claude.json"), "w") as handle:
        handle.write("[1, 2, 3]")
    with open(os.path.join(os.getcwd(), "state"), "w") as handle:
        handle.write("cwd state")
    files = dict()
    for here, _, names in os.walk(root):
        for name in names:
            full = os.path.join(here, name)
            with open(full, "rb") as handle:
                files[os.path.relpath(full, root)] = base64.b64encode(handle.read()).decode()
    with open(PLAN) as handle:
        how = [s for s in json.load(handle) if s["op"] == "preflight"]
    how = how[0] if how else dict()
    log("preflight", argv=sys.argv, env=sorted(os.environ), root=root, cwd=os.getcwd(),
        files=files)
    print(how.get("reply", "accountok"))
    sys.exit(how.get("code", 0))
'''
NEW_STUB = STUB.replace('''if sys.argv[1:2] == ["-p"]:
    print("accountok")
    sys.exit(0)
''', PREFLIGHT_BRANCH)
assert NEW_STUB != STUB

#: Credential and provider names planted in the person's environment (L4):
#: none may reach either child.
PLANTED = {"ANTHROPIC_IDENTITY_TOKEN": "x", "CLAUDE_CODE_USE_BEDROCK": "1",
           "ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "http://x",
           "CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_CUSTOM_HEADERS": "x",
           "AWS_PROFILE": "x"}


# --- the guard -------------------------------------------------------------------------


def _argv(args, kwargs) -> list[str]:
    argv = args[0] if args else kwargs.get("args")
    if isinstance(argv, (list, tuple)):
        return [str(a) for a in argv]
    return str(argv).split() if argv else []


def _resolve(argv0: str, kwargs) -> str | None:
    if os.sep in argv0:
        return os.path.realpath(argv0) if os.path.exists(argv0) else None
    env = kwargs.get("env")
    found = shutil.which(argv0, path=(env if env is not None else os.environ).get("PATH"))
    return os.path.realpath(found) if found else None


@pytest.fixture(autouse=True)
def _only_this_tests_stubs(monkeypatch, tmp_path):
    """A subprocess naming a FORBIDDEN tool raises unless it resolves inside
    this test's tmp folder. Every argv is recorded in ARGVS."""
    ARGVS.clear()
    allowed = os.path.realpath(tmp_path) + os.sep

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, _name=name, **kwargs):
            argv = _argv(args, kwargs)
            ARGVS.append(argv)
            if argv and os.path.basename(argv[0]) in FORBIDDEN:
                found = _resolve(argv[0], kwargs)
                if found is not None and not found.startswith(allowed):
                    raise AssertionError(f"test tried to run {found!r} via subprocess.{_name}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("PASSAGE_DIR", raising=False)
    monkeypatch.delenv("PASSAGE_IDENTITIES_FILE", raising=False)
    yield
    ARGVS.clear()


def test_the_guard_refuses_the_hosts_tools():
    for name in FORBIDDEN:
        if shutil.which(name) is None:
            continue
        with pytest.raises(AssertionError, match="test tried to run"):
            subprocess.run([name, "--version"], capture_output=True, check=False)


# --- helpers ---------------------------------------------------------------------------


def home() -> Path:
    return Path(os.environ["HOME"])


def registry_path() -> Path:
    return reg.default_path(home())


def pieces(token: str) -> list[str]:
    return windows(token)


def assert_clean(token: str, *texts: str) -> None:
    """No 8 characters in a row of the token's body in any text."""
    for text in texts:
        for piece in pieces(token):
            assert piece not in text, "a piece of the token leaked"


def assert_no_argv_holds(token: str) -> None:
    assert ARGVS, "the guard saw no spawn at all"
    assert_clean(token, *(" ".join(argv) for argv in ARGVS))


def files_under(root: Path, *, skip: Path | None = None) -> list[Path]:
    found = []
    for path in root.rglob("*"):
        if skip is not None and path.is_relative_to(skip):
            continue
        if path.is_file() and not path.is_symlink():
            found.append(path)
    return found


def assert_no_file_holds(token: str, root: Path, *, skip: Path | None = None) -> None:
    for path in files_under(root, skip=skip):
        data = path.read_bytes().decode("utf-8", "replace")
        assert_clean(token, data)


@pytest.fixture
def term():
    """The person's terminal, for the fast tests: a pty pair, 80x30."""
    master, slave = os.openpty()
    st.set_size(slave, 80, 30)
    yield slave
    os.close(slave)
    os.close(master)


class Calls:
    def __init__(self) -> None:
        self.capture = 0
        self.preflight = 0


def fake_capture(token: str | None, calls: Calls, *, why: str = "setup-token exited 1",
                 screen: str = "Error: policy\n\nsetup-token's screen", ended: str | None = None,
                 hits: tuple[str, ...] = (), removed: bool = True,
                 raises: BaseException | None = None):
    def capture(*, tty_in, tty_out, claude, environ, config_root):
        calls.capture += 1
        assert os.isatty(tty_in) and os.isatty(tty_out)
        assert Path(config_root) == reg.accounts_dir(home())
        if raises is not None:
            raise raises
        return st.Run(st.Captured(token, 0 if token else 1, why, screen), ended, hits, removed)
    return capture


def fake_preflight(calls: Calls, *, raises: BaseException | None = None, then=None):
    def preflight(token: str) -> None:
        calls.preflight += 1
        assert st.is_setup_token(token)
        if then is not None:
            then()
        if raises is not None:
            raise raises
    return preflight


def run_fake(term, token: str | None = "default", *, store: FakeTokenStore | None = None,
             calls: Calls | None = None, name: str = "nu", email: str = EMAIL,
             capture=None, preflight=None, tty_in=None):
    """`new` on fakes. Returns (code, out, err, store, calls, token)."""
    token = synthetic_token() if token == "default" else token
    store = FakeTokenStore() if store is None else store
    calls = Calls() if calls is None else calls
    out, err = io.StringIO(), io.StringIO()
    code = nw.new(name, email=email, store=store,
                  capture=capture or fake_capture(token, calls),
                  preflight=preflight or fake_preflight(calls),
                  tty_in=term if tty_in is None else tty_in, tty_out=term,
                  out=out, err=err)
    return code, out.getvalue(), err.getvalue(), store, calls, token


# --- the pieces ------------------------------------------------------------------------


def test_the_exit_table_is_the_designs():
    assert nw.EXIT_CODES.keys() == {0, 2, 4, 5, 8, 9}
    assert (nw.EXIT_USAGE, nw.EXIT_NO_TOKEN, nw.EXIT_REFUSED, nw.EXIT_NOT_SAVED,
            nw.EXIT_NOT_REGISTERED) == (2, 4, 5, 8, 9)
    # 8 and 9 are new to the group: no code means two things across use and new
    assert not {8, 9} & set(u.EXIT_CODES)


def test_the_entry_is_the_neutral_default_under_no_setting():
    assert nw.DEFAULT_PREFIX == "claude/oauth-tokens"
    assert nw.entry_prefix() == nw.DEFAULT_PREFIX
    assert ENTRY == "claude/oauth-tokens/new@example.com"
    assert nw.entry_for("e@example.com", "a/b") == "a/b/e@example.com"


# --- the entry prefix: a `session` setting ---------------------------------------------


def test_the_prefix_is_read_from_the_session_host_layer(tmp_path):
    write_session_settings(tmp_path, {"account": {"passage_prefix": "vault/tokens/"}})
    assert nw.entry_prefix(home=tmp_path) == "vault/tokens"           # trailing / dropped
    assert nw.entry_prefix({"HOME": str(tmp_path)}) == "vault/tokens"


def test_a_later_host_file_overrides_an_earlier_one(tmp_path):
    write_session_settings(tmp_path, {"account": {"passage_prefix": "first"}})
    write_session_settings(tmp_path, {"account": {"passage_prefix": "second"}},
                           "session.d/50-account.json")
    assert nw.entry_prefix(home=tmp_path) == "second"


def test_other_session_settings_leave_the_default(tmp_path):
    write_session_settings(tmp_path, {"context": {"near_pct": 10}, "account": {}})
    assert nw.entry_prefix(home=tmp_path) == nw.DEFAULT_PREFIX


#: Stands for a fresh synthetic token in a parametrize (made per test).
TOKEN_SHAPED = object()


@pytest.mark.parametrize("value", ["/abs/path", "a/../b", "..", "a/./b", "a//b", "",
                                   "a b", "a\nb", "/", "-x", "-x/y", TOKEN_SHAPED,
                                   7, ["a"], {"x": "y"}, True])
def test_a_bad_prefix_is_refused_naming_the_key_and_file(tmp_path, value):
    token = synthetic_token()
    if value is TOKEN_SHAPED:
        value = f"vault/{token}"
    path = write_session_settings(tmp_path, {"account": {"passage_prefix": value}})
    with pytest.raises(nw.NewError) as raised:
        nw.entry_prefix(home=tmp_path)
    assert raised.value.code == 5
    assert nw.PREFIX_KEY in str(raised.value)
    if not isinstance(value, dict):
        assert str(path) in str(raised.value)
    assert_clean(token, str(raised.value))
    assert "sk-ant-" not in str(raised.value)


def test_a_settings_file_that_is_not_json_is_refused(tmp_path):
    path = write_session_settings(tmp_path, "{not json")
    with pytest.raises(nw.NewError) as raised:
        nw.entry_prefix(home=tmp_path)
    assert raised.value.code == 5 and str(path) in str(raised.value)


def test_new_saves_and_registers_under_the_configured_prefix(term, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_session_settings(tmp_path, {"account": {"passage_prefix": "vault/tokens"}})
    code, out, _err, store, calls, token = run_fake(term)
    entry = "vault/tokens/" + EMAIL
    assert code == 0, out
    assert calls.capture == 1 and calls.preflight == 1
    assert store.inserts == [entry] and store.read(entry) == token
    assert f"passage:{entry}" in out
    account = reg.load(reg.default_path(tmp_path)).find("nu")
    assert [str(s) for s in account.sources] == [f"passage:{entry}"]


class PathTestStore(FakeTokenStore):
    """A fake whose `exists` is a bare lookup, as `PassageCLI.exists` is a
    path test: it never runs `check_entry`, so only `new`'s own checks can
    stop a bad entry before the sign-in (do-008 D1)."""

    def exists(self, entry: str) -> bool:
        return entry in self.entries or any(name.startswith(entry + "/")
                                            for name in self.entries)


@pytest.mark.parametrize("value", ["-x", TOKEN_SHAPED])
def test_an_entry_passage_would_refuse_stops_new_before_the_sign_in(term, tmp_path,
                                                                    monkeypatch, value):
    """D1: with the real store's `exists`, a `-x` or token-shaped prefix is
    exit 5 before setup-token and the preflight, not exit 8 after them."""
    token = synthetic_token()
    if value is TOKEN_SHAPED:
        value = token
    monkeypatch.setenv("HOME", str(tmp_path))
    write_session_settings(tmp_path, {"account": {"passage_prefix": value}})
    store = PathTestStore()
    code, out, err, store, calls, _ = run_fake(term, store=store)
    assert code == 5, err
    assert calls.capture == 0 and calls.preflight == 0 and store.inserts == []
    assert nw.PREFIX_KEY in err
    assert_clean(token, out + err)
    assert not reg.default_path(tmp_path).exists()


def test_new_checks_the_whole_entry_before_the_sign_in(term, monkeypatch):
    """The step-2 check of the whole entry is a second wall: with the
    prefix check out of the way, `-x` still stops at exit 5."""
    monkeypatch.setattr(nw, "entry_prefix", lambda *_a, **_k: "-x")
    code, _out, err, store, calls, _ = run_fake(term, store=PathTestStore())
    assert code == 5 and calls.capture == 0 and store.inserts == []
    assert "starts with '-'" in err and nw.PREFIX_KEY in err


def test_a_bad_prefix_setting_stops_new_before_the_sign_in(term, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_session_settings(tmp_path, {"account": {"passage_prefix": "../out"}})
    code, _out, err, store, calls, _token = run_fake(term)
    assert code == 5 and calls.capture == 0 and store.inserts == []
    assert nw.PREFIX_KEY in err
    assert not reg.default_path(tmp_path).exists()


def test_the_help_names_the_setting_and_its_default():
    text = cli.EPILOG
    assert nw.PREFIX_KEY in text and nw.DEFAULT_PREFIX in text


@pytest.mark.parametrize("email", ["e@example.com", "a.b+c_d%e-f@x-y.example.org"])
def test_a_path_safe_email_passes(email):
    assert nw.check_email(email) == email


@pytest.mark.parametrize("email", ["a/b@x.com", "a@x..com", "..@x.com", "a b@x.com",
                                   "a@x/../y", "a@", "@x.com", "a@x.com\n", "aé@x.com"])
def test_an_email_that_is_not_path_safe_is_usage(email):
    with pytest.raises(nw.NewError) as raised:
        nw.check_email(email)
    assert raised.value.code == 2


def test_a_token_shaped_email_is_never_quoted():
    token = synthetic_token()
    with pytest.raises(nw.NewError) as raised:
        nw.check_email(token + "@x.com")
    assert_clean(token, str(raised.value))


def test_same_token_compares_digests():
    token = synthetic_token()
    assert reg.same_token(token, token[:])
    assert not reg.same_token(token, token[:-1] + ("X" if token[-1] != "X" else "Y"))
    assert not reg.same_token(token, token + "\n")
    assert not reg.same_token(token, "")


def test_r_b_warns_below_about_25_rows_at_40_columns_and_not_at_80x24():
    assert nw.short_warning(40, 24) is not None
    assert nw.short_warning(40, 25) is None
    assert nw.short_warning(80, 24) is None
    assert nw.short_warning(80, 20) is not None
    assert nw.short_warning(200, 18) is None
    assert "40x24" in nw.short_warning(40, 24)


def test_the_preflights_env_is_the_allowlist_plus_its_two_names():
    env = {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "HTTPS_PROXY": "p",
           "CLAUDE_CONFIG_DIR": "/c", "CLAUDE_CODE_OAUTH_TOKEN": "t", **PLANTED,
           "TMUX": "x", "SSH_AUTH_SOCK": "x", "CLAUDECODE": "1", "NODE_OPTIONS": "x"}
    assert nw.narrowed_env(env) == {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C",
                                    "HTTPS_PROXY": "p", "CLAUDE_CONFIG_DIR": "/c",
                                    "CLAUDE_CODE_OAUTH_TOKEN": "t"}


def test_the_narrowing_runner_hands_the_narrowed_env_to_its_inner_runner():
    seen = {}

    def inner(argv, env, cwd):
        seen.update(env)
        return subprocess.CompletedProcess(argv, 0, "accountok\n", "")

    token = synthetic_token()
    u.claude_preflight(token, runner=nw.narrowing_runner(inner))
    assert seen["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert seen["CLAUDE_CONFIG_DIR"].endswith("claude-config")
    assert not set(seen) - set(st.ENV_ALLOW) - set(nw.PREFLIGHT_SET) - {
        k for k in seen if k.startswith("LC_")}


# --- steps 1-3: refused before the sign-in ---------------------------------------------


def _bad_registry():
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    path.chmod(0o644)


def _taken():
    registry = reg.Registry()
    registry.add(reg.make_account("nu", "other@example.com", ["file:~/x"]))
    reg.save(registry, registry_path())


REFUSALS = {
    "a bad NAME": ({"name": "no/slash"}, None, 2),
    "a bad email": ({"email": "a/b@x.com"}, None, 2),
    "an email with ..": ({"email": "a@x..com"}, None, 2),
    "a refused registry": ({}, _bad_registry, 5),
    "NAME registered": ({}, _taken, 2),
    "passage not usable": ({"store": FakeTokenStore(fail_check=True)}, None, 5),
    "the entry exists": ({"store": FakeTokenStore({ENTRY: "x"})}, None, 2),
    "a folder by the entry's name": ({"store": FakeTokenStore({ENTRY + "/y": "x"})}, None, 2),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_a_refusal_in_steps_1_to_3_runs_no_setup_token_and_leaves_nothing(term, case):
    kwargs, prepare, code = REFUSALS[case]
    if prepare:
        prepare()
    before = registry_path().read_bytes() if registry_path().exists() else None
    stale = reg.accounts_dir(home()) / f"{st.CONFIG_PREFIX}old"
    stale.mkdir(parents=True)
    store = kwargs.pop("store", FakeTokenStore())
    entries = dict(store.entries)
    calls = Calls()
    got, out, err, store, calls, _ = run_fake(term, store=store, calls=calls, **kwargs)
    assert got == code
    assert calls.capture == 0 and calls.preflight == 0
    assert store.inserts == [] and store.entries == entries
    assert (registry_path().read_bytes() if registry_path().exists() else None) == before
    assert stale.is_dir()                         # nothing done: not even the sweep
    assert out == "" and err.startswith("session account new: ")


def test_stdin_that_is_not_a_terminal_is_usage(term):
    reader, writer = os.pipe()
    try:
        code, _out, err, _store, calls, _ = run_fake(term, tty_in=reader)
    finally:
        os.close(reader)
        os.close(writer)
    assert code == 2 and calls.capture == 0 and "terminal" in err


def test_stdio_with_no_file_descriptor_is_usage_not_a_traceback(monkeypatch):
    """With no `tty` seam, `new` reads stdin's and stdout's fds; a stream
    with none (captured, or closed) is no terminal: exit 2."""
    monkeypatch.setattr("sys.stdin", io.StringIO())
    calls = Calls()
    err = io.StringIO()
    code = cli.main(["new", "nu", "--email", EMAIL], store=FakeTokenStore(),
                    capture=fake_capture(synthetic_token(), calls),
                    preflight=fake_preflight(calls), out=io.StringIO(), err=err)
    assert code == 2 and calls.capture == 0 and "must be a terminal" in err.getvalue()


def test_a_terminal_under_40_columns_is_usage(term):
    st.set_size(term, 39, 30)
    code, _out, err, _store, calls, _ = run_fake(term)
    assert code == 2 and calls.capture == 0 and "39 columns" in err


def test_the_real_store_with_no_passage_store_is_refused_before_the_sign_in(term, monkeypatch,
                                                                              tmp_path):
    monkeypatch.setenv("PASSAGE_DIR", str(tmp_path / "no-store"))
    calls = Calls()
    out, err = io.StringIO(), io.StringIO()
    code = nw.new("nu", email=EMAIL, store=ts.PassageCLI(),
                  capture=fake_capture(synthetic_token(), calls),
                  preflight=fake_preflight(calls), tty_in=term, tty_out=term, out=out, err=err)
    assert code == 5 and calls.capture == 0 and "no passage store" in err.getvalue()


# --- step 1: R-b -----------------------------------------------------------------------


@pytest.mark.parametrize(("cols", "rows", "warned"), [(40, 24, True), (40, 25, False),
                                                       (80, 24, False)])
def test_a_short_terminal_is_warned_before_the_browser_opens(term, cols, rows, warned):
    st.set_size(term, cols, rows)
    code, _out, err, *_ = run_fake(term)
    assert code == 0
    assert ("warning: this terminal is" in err) is warned


# --- step 3: the stale setup-token folders ---------------------------------------------


def test_a_stale_setup_token_folder_is_scanned_for_a_token_shape_and_removed(term, tmp_path):
    root = reg.accounts_dir(home())
    dirty = root / f"{st.CONFIG_PREFIX}dirty"
    clean = root / f"{st.CONFIG_PREFIX}clean"
    (dirty / "sub").mkdir(parents=True)
    clean.mkdir()
    old = synthetic_token()
    (dirty / "sub" / "credentials.json").write_text(json.dumps({"accessToken": old}))
    (clean / "settings.json").write_text("{}")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("keep")
    (root / f"{st.CONFIG_PREFIX}link").symlink_to(elsewhere)
    (root / "accounts-other").mkdir()
    code, out, err, *_ = run_fake(term)
    assert code == 0
    assert not dirty.exists() and not clean.exists()
    assert (elsewhere / "keep").exists() and (root / "accounts-other").is_dir()
    assert f"{dirty}, was removed; it held a token's shape in: sub/credentials.json" in err
    assert f"{clean}, was removed" in err
    assert_clean(old, out, err)


# --- step 4: the capture ---------------------------------------------------------------


def test_no_token_prints_the_last_masked_screen_and_exits_4(term):
    calls = Calls()
    code, out, err, store, calls, _ = run_fake(
        term, None, calls=calls,
        capture=fake_capture(None, calls, why="setup-token exited 1",
                             screen="Error: your organization's policy does not allow this"))
    assert code == 4 and calls.preflight == 0 and store.inserts == []
    assert "setup-token's last screen, masked:\n  Error: your organization's policy" in out
    assert "no token: setup-token exited 1; nothing was saved" in err


def test_a_claude_that_cannot_run_is_no_token(term):
    calls = Calls()
    code, _out, err, store, *_ = run_fake(
        term, calls=calls, capture=fake_capture(
            None, calls, raises=st.SetupTokenError("could not run claude setup-token (x)")))
    assert code == 4 and store.inserts == [] and "could not run claude setup-token" in err


def test_a_signal_during_the_sign_in_is_128_plus_it_and_saves_nothing(term):
    calls = Calls()
    code, _out, err, store, *_ = run_fake(
        term, calls=calls,
        capture=fake_capture(None, calls, raises=st.Interrupted(signal.SIGTERM)))
    assert code == 128 + signal.SIGTERM and store.inserts == [] and calls.preflight == 0
    assert "not scanned" in err and "nothing was saved" in err


def test_config_hits_and_a_folder_left_behind_are_reported_by_path(term):
    calls = Calls()
    token = synthetic_token()
    code, out, err, *_ = run_fake(term, token, calls=calls, capture=fake_capture(
        token, calls, hits=("credentials.json",), removed=False))
    assert code == 0
    assert "into its config folder: credentials.json (not shown)" in err
    assert "could not be removed; the next `new` scans and removes it" in err
    assert_clean(token, out, err)


# --- steps 5-6: the shape and the preflight --------------------------------------------


def test_a_value_not_in_the_setup_token_shape_is_never_preflighted(term):
    calls = Calls()
    other = "sk-ant-api03-" + "A" * 90
    code, _out, err, store, calls, _ = run_fake(term, other, calls=calls)
    assert code == 4 and calls.preflight == 0 and store.inserts == []
    assert "not in the setup-token shape" in err and "A" * 8 not in err


def test_a_failed_preflight_is_4_and_nothing_is_saved(term):
    """N-a′ (do-003): a capture that kept a wrong value (a child that died
    mid-paint and reported exit 0) fails its preflight: exit 4, nothing
    saved."""
    calls = Calls()
    token = synthetic_token()
    code, out, err, store, calls, _ = run_fake(
        term, token, calls=calls,
        preflight=fake_preflight(calls, raises=u.PreflightError(
            f"claude exited 1 and did not reply 'accountok' {token[20:40]}")))
    assert code == 4 and store.inserts == [] and calls.preflight == 1
    assert "the preflight failed" in err and "nothing was saved" in err
    assert_clean(token, out, err)


def test_a_preflight_seam_that_raises_anything_is_4(term):
    calls = Calls()
    code, _out, err, store, *_ = run_fake(
        term, calls=calls, preflight=fake_preflight(calls, raises=RuntimeError("boom")))
    assert code == 4 and store.inserts == [] and "(RuntimeError)" in err and "boom" not in err


# --- steps 7-8: the insert and the read-back -------------------------------------------


def test_an_entry_that_appears_during_the_sign_in_is_never_overwritten(term):
    store = FakeTokenStore()
    calls = Calls()
    code, _out, err, store, *_ = run_fake(
        term, store=store, calls=calls,
        preflight=fake_preflight(calls, then=lambda: store.entries.update({ENTRY: "theirs"})))
    assert code == 8 and store.inserts == [] and store.entries == {ENTRY: "theirs"}
    assert "appeared during the sign-in" in err and not registry_path().exists()


def test_a_failed_insert_that_wrote_nothing_is_8_and_reads_nothing(term):
    code, _out, err, store, *_ = run_fake(term, store=FakeTokenStore(fail_insert=True))
    assert code == 8 and store.reads == [] and not registry_path().exists()
    assert "not saved: insert" in err and "nothing was registered" in err


def test_a_mismatch_is_8_names_the_entry_and_registers_nothing(term):
    code, out, err, store, _calls, token = run_fake(term, store=FakeTokenStore(corrupt=True))
    assert code == 8 and store.reads == [ENTRY] and not registry_path().exists()
    assert f"{SOURCE} did not read back as the token (digests differ)" in err
    assert f"passage rm {ENTRY}" in err
    assert_clean(token, out, err)


def test_a_failed_insert_that_wrote_the_entry_is_read_back_and_registered(term):
    """N2 (design §4, round 3): a timed-out insert can leave a good entry.
    It is read back, and a match goes on as an exit 0 does, with the note."""
    code, out, _err, store, _calls, token = run_fake(
        term, store=FakeTokenStore(fail_insert_written=True))
    assert code == 0 and store.reads == [ENTRY]
    assert "note: insert" in out and "the entry was written anyway, so it was read back" in out
    assert reg.load(registry_path()).get("nu").fp == reg.fingerprint(token)


def test_a_failed_insert_that_wrote_a_bad_entry_is_8(term):
    code, _out, err, _store, *_ = run_fake(
        term, store=FakeTokenStore(fail_insert_written=True, corrupt=True))
    assert code == 8 and "digests differ" in err and not registry_path().exists()


def test_a_failed_read_back_is_8_and_its_text_is_scrubbed_with_the_token(term):
    """do-002 review L1: `read` scrubs without the token, so a piece of it
    cut from its prefix would pass its shape scrub. `new` scrubs again with
    the token itself before quoting."""
    token = synthetic_token()

    class Leaky(FakeTokenStore):
        def read(self, entry):
            self.reads.append(entry)
            raise StoreError(f"read back {entry}: age said {token[20:60]}", entry=entry)

    code, out, err, _store, *_ = run_fake(term, token, store=Leaky())
    assert code == 8 and "read back" in err and f"passage rm {ENTRY}" in err
    assert_clean(token, out, err)


def test_an_insert_error_is_scrubbed_with_the_token_too(term):
    token = synthetic_token()

    class Leaky(FakeTokenStore):
        def insert(self, entry, value):
            raise StoreError(f"passage said {value[25:70]}", entry=entry)

    code, out, err, *_ = run_fake(term, token, store=Leaky())
    assert code == 8
    assert_clean(token, out, err)


def test_a_note_from_the_insert_is_printed_beside_the_entry(term):
    code, out, _err, *_ = run_fake(term, store=FakeTokenStore(note="commit may have failed"))
    assert code == 0 and "  note: commit may have failed" in out


# --- step 9: register ------------------------------------------------------------------


def test_success_registers_passage_entry_with_the_fp_known_and_prints_no_fp(term):
    code, out, err, store, _calls, token = run_fake(term)
    assert code == 0 and err == ""
    account = reg.load(registry_path()).get("nu")
    assert [str(s) for s in account.sources] == [SOURCE]
    assert account.email == EMAIL and account.fp == reg.fingerprint(token)
    assert store.reads == [ENTRY]                    # one read: the read-back
    assert out.splitlines() == [
        f"new nu ({EMAIL}): sign in AS {EMAIL} in the browser that opens",
        f"  token:  goes to {SOURCE}, on this host",
        "  screen: setup-token runs in a masked copy of its screen; the token is never shown",
        "  stop:   Ctrl-C twice ends the run; nothing is saved",
        "token captured (not shown); preflight: accountok",
        f"saved: {SOURCE} (read back: digests match)",
        f"added nu ({EMAIL}): passage (touch); fp known",
        f"  registry: {registry_path()}",
    ]
    assert account.fp not in out + err
    assert_clean(token, out, err)


def test_a_name_registered_during_the_sign_in_is_9_with_the_add_line(term):
    calls = Calls()
    code, out, err, store, calls, token = run_fake(
        term, calls=calls, preflight=fake_preflight(calls, then=_taken))
    assert code == 9 and store.entries.keys() == {ENTRY}
    assert "saved and verified, NOT registered" in err
    assert f"to finish: session account add nu --email {EMAIL} --source {SOURCE}" in err
    assert reg.load(registry_path()).get("nu").email == "other@example.com"
    assert_clean(token, out, err)


def test_a_token_already_registered_under_another_name_is_9(term):
    token = synthetic_token()
    registry = reg.Registry()
    registry.add(reg.make_account("old", "old@example.com", ["file:~/x"],
                                  reg.fingerprint(token)))
    reg.save(registry, registry_path())
    code, _out, err, *_ = run_fake(term, token)
    assert code == 9 and "already registered as 'old'" in err
    assert reg.load(registry_path()).names() == ["old"]


# --- cli: add(known_fp=), the parser, the dispatch -------------------------------------


def test_add_with_a_known_fp_resolves_nothing():
    def never(_ref):
        raise AssertionError("resolved")

    fp = reg.fingerprint(synthetic_token())
    out, err = io.StringIO(), io.StringIO()
    code = cli.add("k", email="k@example.com", sources=[SOURCE], known_fp=fp,
                   backends={"passage": never}, out=out, err=err)
    assert code == 0 and reg.load(registry_path()).get("k").fp == fp
    assert "fp known" in out.getvalue() and fp not in out.getvalue()


def test_add_with_a_bad_known_fp_is_usage():
    out, err = io.StringIO(), io.StringIO()
    code = cli.add("k", email="k@example.com", sources=[SOURCE], known_fp="XYZ",
                   out=out, err=err)
    assert code == 2 and not registry_path().exists()


def test_add_with_a_known_fp_still_refuses_a_twin():
    fp = reg.fingerprint(synthetic_token())
    assert cli.add("a", email="a@example.com", sources=["passage:other/a"], known_fp=fp,
                   out=io.StringIO(), err=io.StringIO()) == 0
    err = io.StringIO()
    assert cli.add("b", email="b@example.com", sources=[SOURCE], known_fp=fp,
                   out=io.StringIO(), err=err) == 2
    assert "one token, one name" in err.getvalue()


def test_add_without_a_known_fp_behaves_as_before():
    token = synthetic_token()
    out = io.StringIO()
    code = cli.add("k", email="k@example.com", sources=[SOURCE],
                   backends={"passage": lambda ref: token}, out=out, err=io.StringIO())
    assert code == 0 and reg.load(registry_path()).get("k").fp == reg.fingerprint(token)


def test_the_group_parses_new_and_dispatches_it_with_its_seams(term):
    calls = Calls()
    token = synthetic_token()
    store = FakeTokenStore()
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["new", "nu", "--email", EMAIL], store=store,
                    capture=fake_capture(token, calls), preflight=fake_preflight(calls),
                    tty=(term, term), out=out, err=err)
    assert code == 0 and calls.capture == 1 and calls.preflight == 1
    assert store.inserts == [ENTRY] and "added nu" in out.getvalue()


def test_new_needs_its_email_and_its_help_names_the_codes():
    err = io.StringIO()
    assert cli.main(["new", "nu"], out=io.StringIO(), err=err) == 2
    assert "--email" in err.getvalue()
    out = io.StringIO()
    assert cli.main(["--help"], out=out, err=io.StringIO()) == 0
    text = " ".join(out.getvalue().split())
    assert "new" in text and "8 `new` did not save the token" in text
    assert "9 `new` saved it but did not register it" in text


# --- end to end on stubs (SC2, SC3, R-4, L4) -------------------------------------------


class E2E:
    """The stubs, the scratch store and a fresh TMPDIR for one `new` run."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir()
        self.passage = Passage(stub_dir, home(), monkeypatch)
        self.tmpdir = tmp_path / "fresh-tmpdir"
        self.tmpdir.mkdir()
        monkeypatch.setenv("TMPDIR", str(self.tmpdir))
        monkeypatch.setattr(tempfile, "tempdir", str(self.tmpdir))
        for key, value in PLANTED.items():
            monkeypatch.setenv(key, value)

    def run(self, steps: list[dict], *, cols: int = 80, rows: int = 30, actor=None,
            store: ts.TokenStore | None = None, name: str = "nu"):
        stub = Stub(self.tmp_path, steps, NEW_STUB)
        person = Person(cols, rows)
        errors: list[BaseException] = []

        def act() -> None:
            try:
                actor(person, stub)
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        thread = threading.Thread(target=act, daemon=True) if actor else None
        if thread:
            thread.start()

        def kill_stub() -> None:
            for row in stub.events("start"):
                try:
                    os.killpg(row["pid"], signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

        watchdog = threading.Timer(WATCHDOG_S, kill_stub)
        watchdog.daemon = True
        watchdog.start()
        out, err = io.StringIO(), io.StringIO()
        try:
            code = nw.new(name, email=EMAIL, store=store, claude=str(stub.path),
                          tty_in=person.slave, tty_out=person.slave, out=out, err=err)
        finally:
            watchdog.cancel()
            if thread:
                thread.join(20)
            person.close()
        if errors:
            raise errors[0]
        self.stub = stub
        self.drawn = bytes(person.out).decode("utf-8", "replace")
        return code, out.getvalue(), err.getvalue()

    def assert_nothing_holds(self, token: str, *texts: str) -> None:
        """SC2's assertions: the token is on no argv (the guard's record, and
        both stubs' own logs of theirs), in nothing drawn or printed, in no
        file under HOME but the base64 store, and in nothing under TMPDIR."""
        assert_no_argv_holds(token)
        argv_logs = [" ".join(row["argv"]) for row in self.stub.events()
                     if "argv" in row]
        assert_clean(token, self.drawn, *texts, *argv_logs, *self.passage.calls())
        assert "sk-ant-" not in self.drawn
        assert_no_file_holds(token, home(), skip=self.passage.store)
        assert_no_file_holds(token, self.tmpdir)
        for path in self.passage.store.rglob("*.age"):
            assert token.encode() not in path.read_bytes()      # base64 only


@pytest.fixture
def e2e(tmp_path, monkeypatch) -> E2E:
    return E2E(tmp_path, monkeypatch)


def test_sc2_new_runs_all_three_steps_end_to_end_on_stubs(e2e, capsys):
    token = synthetic_token()
    code, out, err = e2e.run(flow(token, 80), actor=paste)
    captured = capsys.readouterr()
    assert code == 0, err
    # saved: the stub store holds it (base64), read back, registered
    assert e2e.passage.stored(ENTRY) == (token + "\n").encode()
    account = reg.load(registry_path()).get("nu")
    assert [str(s) for s in account.sources] == [SOURCE]
    assert account.fp == reg.fingerprint(token)
    assert e2e.passage.calls() == [f"passage insert -m {ENTRY}", f"passage show {ENTRY}"]
    assert f"saved: {SOURCE} (read back: digests match)" in out
    assert "added nu" in out and "fp known" in out
    # the token: nowhere (SC2)
    e2e.assert_nothing_holds(token, out, err, captured.out, captured.err)
    # no setup-token-* folder is left
    assert not list((reg.accounts_dir(home())).glob(f"{st.CONFIG_PREFIX}*"))


def test_r4_the_preflight_folder_is_gone_and_never_held_a_token_piece(e2e):
    token = synthetic_token()
    code, _out, err = e2e.run(flow(token, 80), actor=paste)
    assert code == 0, err
    [pre] = e2e.stub.events("preflight")
    root = Path(pre["root"])
    assert root.name.startswith("account-preflight-")
    assert root.parent == e2e.tmpdir                  # the fresh TMPDIR
    assert not root.exists()                          # gone after the run
    assert pre["files"], "the stub's preflight left no state to check"
    for content in pre["files"].values():            # while it existed
        assert_clean(token, base64.b64decode(content).decode("utf-8", "replace"))
    assert list(e2e.tmpdir.iterdir()) == []


def test_l4_the_planted_variables_reach_neither_child(e2e):
    token = synthetic_token()
    code, _out, err = e2e.run(flow(token, 80), actor=paste)
    assert code == 0, err
    start, [pre] = e2e.stub.start(), e2e.stub.events("preflight")
    assert not set(PLANTED) & set(start["env"])
    assert not set(PLANTED) & set(pre["env"])
    assert {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "PATH", "HOME"} <= set(pre["env"])
    assert pre["argv"][1:] == ["-p", u.PREFLIGHT_PROMPT, "--max-turns", "1"]


def test_sc3_a_read_back_mismatch_aborts_with_8_and_registers_nothing(e2e, capsys):
    registry = reg.Registry()
    registry.add(reg.make_account("other", "other@example.com", ["file:~/x"]))
    reg.save(registry, registry_path())
    before = registry_path().read_bytes()
    e2e.passage.mode("corrupt")
    token = synthetic_token()
    code, out, err = e2e.run(flow(token, 80), actor=paste)
    captured = capsys.readouterr()
    assert code == 8
    assert registry_path().read_bytes() == before                # nothing registered
    assert f"{SOURCE} did not read back as the token (digests differ)" in err
    assert f"passage rm {ENTRY}" in err
    assert e2e.passage.entry_file(ENTRY).exists()                # named, left for a person
    e2e.assert_nothing_holds(token, out, err, captured.out, captured.err)


def test_a_failed_commit_is_saved_with_a_scrubbed_note(e2e, capsys):
    """passage's insert exits 0 when its git commit fails (design §1.2);
    the stub puts the token in git's message, which is scrubbed."""
    e2e.passage.mode("commit-fail")
    token = synthetic_token()
    code, out, err = e2e.run(flow(token, 80), actor=paste)
    captured = capsys.readouterr()
    assert code == 0, err
    assert "  note: passage:" in out and "its git commit may have failed" in out
    e2e.assert_nothing_holds(token, out, err, captured.out, captured.err)


def test_a_failed_insert_is_8_with_passages_stderr_scrubbed(e2e, capsys):
    e2e.passage.mode("insert-fail")
    token = synthetic_token()
    code, out, err = e2e.run(flow(token, 80), actor=paste)
    captured = capsys.readouterr()
    assert code == 8 and "Password encryption aborted" in err
    assert not registry_path().exists()
    e2e.assert_nothing_holds(token, out, err, captured.out, captured.err)


def test_a_hung_insert_is_ended_and_its_good_entry_read_back_and_registered(e2e, capsys):
    """N2 (§4, round 3): the stub writes the entry, then hangs as a commit
    hook would. The timeout ends passage's process group, and the entry it
    left is read back and registered, with the note."""
    e2e.passage.mode("hang")
    token = synthetic_token()
    code, out, err = e2e.run(flow(token, 80), actor=paste, store=ts.PassageCLI(timeout=1.0))
    captured = capsys.readouterr()
    assert code == 0, err
    assert "timed out after 1s" in out and "the entry was written anyway" in out
    assert reg.load(registry_path()).get("nu").fp == reg.fingerprint(token)
    pid = int(e2e.passage.pidfile.read_text())
    end = time.monotonic() + 5
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("passage's hung child outlived the timeout")
    e2e.assert_nothing_holds(token, out, err, captured.out, captured.err)


def test_a_failed_preflight_on_stubs_saves_nothing(e2e):
    token = synthetic_token()
    code, _out, err = e2e.run(flow(token, 80) + [{"op": "preflight", "reply": "no", "code": 1}],
                             actor=paste)
    assert code == 4 and "the preflight failed" in err
    assert e2e.passage.calls() == [] and not registry_path().exists()
    assert list(e2e.tmpdir.iterdir()) == []


def test_new_s_own_exit_ends_the_sign_in_with_4_and_saves_nothing(e2e):
    from test_account_setuptoken import Ink, waiting_screen

    def cancel(person, stub):
        stub.wait_for("ready")
        person.wait_for(b"Paste code here")
        person.type(b"\x03")
        time.sleep(0.1)
        person.type(b"\x03")

    steps = [{"op": "write", "data": b64(Ink(80).frame(waiting_screen(80)))},
             {"op": "ready"}, {"op": "hang"}]
    code, out, err = e2e.run(steps, actor=cancel)
    assert code == 4 and "ended by new (Ctrl-C twice)" in err
    assert "setup-token's last screen, masked:" in out and "Paste code here" in out
    assert e2e.stub.events("preflight") == [] and e2e.passage.calls() == []
    assert not registry_path().exists()


def test_a_screen_taller_than_the_terminal_ends_the_run_with_4(e2e):
    steps = [{"op": "write", "data": b64("".join(f"row {i}\r\n" for i in range(40)))},
             {"op": "ready"}, {"op": "hang"}]
    code, out, _err = e2e.run(steps)
    assert code == 4 and st.TALL_LINE.split(",")[0] in out
    assert e2e.passage.calls() == [] and not registry_path().exists()
