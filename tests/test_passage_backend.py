"""The `passage:` source backend (ST-159 design §5, do-001).

HARD LIMITS (ST-159 slice notes): no real `passage`, `age` or `pass`, no
store, no Keychain, no `claude`. Every token is synthetic
(`sk-ant-oat01-TEST-<random>`).

The autouse guard refuses any subprocess whose argv[0] is `security`,
`pass`, `passage`, `age` or `claude` UNLESS it resolves (real path) inside
this test's own stub folder: the rule is the resolved path, not the name
(design §8), so the stub may be called `passage` while the host's binary,
and the conftest's walled stub, stay refused.

The stub `passage` keeps a scratch store under the test's HOME
(`PASSAGE_DIR`): `insert -m` stores the BASE64 of stdin in `<entry>.age`,
so a plaintext token on disk is a leak, and `show` decodes it.
"""

from __future__ import annotations

import base64
import io
import json
import os
import secrets as pysecrets
import shutil
import subprocess
from pathlib import Path

import pytest
from settings_support import host
from start_support import WALLED_STUBS, walled_bin

import sessiontools.account
from sessiontools.account import registry as reg
from sessiontools.account import status as st
from sessiontools.start import secrets
from sessiontools.start.environment import DeclaredSecret
from sessiontools.start.secrets import SecretError

FORBIDDEN = ("security", "pass", "passage", "age", "claude")
ENTRY = "claude/oauth-tokens/new@example.com"

# The stub passage: logs its argv (never stdin), then acts on $STUB_MODE's
# file: ok (a real round trip over the scratch store), fail, empty.
PASSAGE_STUB = """#!/usr/bin/env bash
echo "passage $*" >> "%(log)s"
mode=$(cat "%(modes)s" 2>/dev/null || echo ok)
store="${PASSAGE_DIR:-$HOME/.passage/store}"
case "$1" in
  insert)
    [ "$2" = "-m" ] || { echo "stub: only insert -m" >&2; exit 2; }
    mkdir -p "$(dirname "$store/$3.age")"
    base64 > "$store/$3.age"
    ;;
  show)
    case "$mode" in
      fail)  echo "age: error: no identity matched any of the recipients" >&2
             echo "second line" >&2; exit 1 ;;
      silent) exit 3 ;;
      empty) printf '\\n'; exit 0 ;;
    esac
    [ -f "$store/$2.age" ] || { echo "Error: $2 is not in the password store." >&2; exit 1; }
    base64 --decode < "$store/$2.age"
    ;;
  *) echo "stub: unknown verb $1" >&2; exit 2 ;;
esac
"""


def _argv0(args, kwargs) -> str:
    argv = args[0] if args else kwargs.get("args")
    if isinstance(argv, (list, tuple)) and argv:
        return str(argv[0])
    if isinstance(argv, (str, bytes)):
        text = argv.decode() if isinstance(argv, bytes) else argv
        return text.split()[0] if text.split() else ""
    return ""


def _resolve(argv0: str, kwargs) -> str | None:
    """Where argv0 would run from: its real path, or None if nothing does."""
    if os.sep in argv0:
        found = argv0 if os.path.exists(argv0) else None
    else:
        env = kwargs.get("env")
        path = (env if env is not None else os.environ).get("PATH", os.defpath)
        found = shutil.which(argv0, path=path)
    return os.path.realpath(found) if found else None


@pytest.fixture
def stub_dir(tmp_path) -> Path:
    path = tmp_path / "stub-bin"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _no_host_passage_age_pass_or_claude(monkeypatch, stub_dir, tmp_path_factory):
    """A subprocess naming a FORBIDDEN tool raises unless it resolves inside
    this test's stub folder."""
    allowed = os.path.realpath(stub_dir) + os.sep

    def refused(args, kwargs) -> str | None:
        argv0 = _argv0(args, kwargs)
        if os.path.basename(argv0) not in FORBIDDEN:
            return None
        found = _resolve(argv0, kwargs)
        if found is None or found.startswith(allowed):
            return None                 # nothing to run, or the test's own stub
        return found

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, _name=name, **kwargs):
            found = refused(args, kwargs)
            if found is not None:
                raise AssertionError(f"test tried to run {found!r} via subprocess.{_name}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)
    empty = tmp_path_factory.mktemp("claude-config-empty")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("PASSAGE_DIR", raising=False)


def synthetic_token() -> str:
    return "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)


class Passage:
    """The stub passage on PATH over a scratch store in the test's HOME."""

    def __init__(self, stub_dir: Path, home: Path, monkeypatch) -> None:
        self.store = home / "scratch-passage" / "store"
        self.log = stub_dir.parent / "passage-calls"
        self.modes = stub_dir.parent / "passage-mode"
        self.log.write_text("")
        stub = stub_dir / "passage"
        stub.write_text(PASSAGE_STUB % {"log": self.log, "modes": self.modes})
        stub.chmod(0o755)
        monkeypatch.setenv("PASSAGE_DIR", str(self.store))
        monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")

    def insert(self, entry: str, text: str) -> None:
        """Through the stub's own `insert -m`, the value on stdin."""
        subprocess.run(["passage", "insert", "-m", entry], input=text, text=True,
                       capture_output=True, check=True)

    def mode(self, mode: str) -> None:
        self.modes.write_text(mode)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def forget(self) -> None:
        self.log.write_text("")


@pytest.fixture
def passage(stub_dir, monkeypatch) -> Passage:
    return Passage(stub_dir, Path(os.environ["HOME"]), monkeypatch)


def assert_absent(token: str, capsys, *texts: str) -> None:
    """The token is in no captured output, no text given, and no file under
    HOME in plaintext (the stub store holds base64)."""
    out, err = capsys.readouterr()
    for text in (out, err, *texts):
        assert token not in text
    for path in Path(os.environ["HOME"]).rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), f"token leaked into {path}"


# --- the guard and the walls ----------------------------------------------------------


def test_the_guard_refuses_the_hosts_tools_and_the_walled_stubs():
    for name in FORBIDDEN:
        if shutil.which(name) is None:
            continue                    # nothing on PATH to refuse
        with pytest.raises(AssertionError, match="test tried to run"):
            subprocess.run([name, "--version"], capture_output=True, check=False)


def test_the_guard_lets_the_tests_own_stub_run(passage):
    passage.insert("x", "y\n")
    assert passage.calls() == ["passage insert -m x"]


def test_passage_and_age_are_walled_off_for_every_test():
    """conftest puts refusing `passage` and `age` stubs first on PATH, as
    for `pass` and `gpgconf` (a real `passage show` runs age, which can ask
    for Touch ID or a key touch)."""
    assert {"passage", "age"} <= set(WALLED_STUBS)
    wall = os.path.realpath(walled_bin()) + os.sep
    for name in ("passage", "age"):
        found = shutil.which(name)
        assert found and os.path.realpath(found).startswith(wall), found
        with open(found) as handle:
            assert "walled off in tests" in handle.read()


# --- the backend ------------------------------------------------------------------------


def test_a_passage_source_resolves_an_entry_on_a_scratch_store(passage, capsys):
    """SC1: the first line of `passage show ENTRY`, stripped."""
    token = synthetic_token()
    passage.insert(ENTRY, f"  {token}  \nnotes: a second line\n")
    passage.forget()
    assert secrets.passage_backend(ENTRY) == token
    assert passage.calls() == [f"passage show {ENTRY}"]       # the value is never argv
    stored = (passage.store / f"{ENTRY}.age").read_bytes()
    assert base64.b64decode(stored).decode().split("\n")[0].strip() == token
    assert_absent(token, capsys)


def test_it_says_age_may_ask_for_a_touch_before_it_waits(passage, capsys):
    passage.insert(ENTRY, "value\n")
    secrets.passage_backend(ENTRY)
    err = capsys.readouterr().err
    assert f"passage:{ENTRY} -- age may ask for Touch ID or a key touch" in err


def test_the_timeout_leaves_a_human_time_for_the_touch(passage, monkeypatch):
    passage.insert(ENTRY, "value\n")
    seen = {}
    real = subprocess.run

    def run(argv, **kwargs):
        seen.update(kwargs)
        return real(argv, **kwargs)

    monkeypatch.setattr(secrets.subprocess, "run", run)
    secrets.passage_backend(ENTRY)
    assert seen["timeout"] == secrets.SECRET_TIMEOUT_S == 120


def test_a_missing_entry_is_refused_before_passage_runs(passage, capsys):
    with pytest.raises(SecretError) as caught:
        secrets.passage_backend(ENTRY)
    assert str(caught.value) == f"no passage entry {ENTRY} in {passage.store}"
    assert passage.calls() == []                        # a typo costs no touch
    assert "Touch ID" not in capsys.readouterr().err    # and no wait was announced


def test_a_folder_never_resolves_to_its_own_name(passage):
    """`passage show FOLDER` prints a tree and exits 0 (design §1.2)."""
    passage.insert(ENTRY, "value\n")
    passage.forget()
    with pytest.raises(SecretError, match="no passage entry"):
        secrets.passage_backend("claude/oauth-tokens")
    assert passage.calls() == []


@pytest.mark.parametrize("ref", ["", "/etc/passwd", "../outside", "a/../../b", "a/..",
                                 "personal/", "a//"])
def test_a_ref_that_is_not_an_entry_inside_the_store_is_refused(passage, ref):
    outside = passage.store.parent / "outside.age"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("not an entry\n")
    with pytest.raises(SecretError, match="refused: not an entry name inside the store"):
        secrets.passage_backend(ref)
    assert passage.calls() == []


def test_a_failed_show_is_a_failure_with_passages_first_stderr_line(passage):
    passage.insert(ENTRY, "value\n")
    passage.mode("fail")
    with pytest.raises(SecretError) as caught:
        secrets.passage_backend(ENTRY)
    assert str(caught.value) == "age: error: no identity matched any of the recipients"


def test_a_silent_failure_names_the_exit_status(passage):
    passage.insert(ENTRY, "value\n")
    passage.mode("silent")
    with pytest.raises(SecretError, match=r"^`passage` exited 3$"):
        secrets.passage_backend(ENTRY)


def test_an_empty_value_is_a_failure_not_a_value(passage):
    passage.insert(ENTRY, "value\n")
    passage.mode("empty")
    with pytest.raises(SecretError, match="empty value"):
        secrets.passage_backend(ENTRY)


def test_an_absent_passage_is_a_reported_failure_not_a_crash(passage, tmp_path, monkeypatch):
    passage.insert(ENTRY, "value\n")
    nothing = tmp_path / "no-bin"
    nothing.mkdir()
    monkeypatch.setenv("PATH", str(nothing))
    with pytest.raises(SecretError, match="`passage` is not installed"):
        secrets.passage_backend(ENTRY)


def test_a_passage_that_will_not_run_is_a_reported_failure(passage, stub_dir, monkeypatch):
    passage.insert(ENTRY, "value\n")
    (stub_dir / "passage").chmod(0o644)              # there, but not executable
    monkeypatch.setenv("PATH", str(stub_dir))        # and no other passage to find
    with pytest.raises(SecretError, match="`passage` could not run"):
        secrets.passage_backend(ENTRY)


def test_a_hung_passage_is_a_timeout(passage, monkeypatch):
    passage.insert(ENTRY, "value\n")

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(secrets.subprocess, "run", hang)
    with pytest.raises(SecretError, match=r"`passage` timed out after 120s"):
        secrets.passage_backend(ENTRY)


def test_the_store_is_passage_dir_else_passages_own_default(monkeypatch):
    assert secrets.passage_store({"PASSAGE_DIR": "/x/store"}) == "/x/store"
    home = os.environ["HOME"]
    assert secrets.passage_store({}) == os.path.join(home, ".passage", "store")
    assert secrets.passage_store({"PASSAGE_DIR": ""}) == os.path.join(home, ".passage", "store")
    monkeypatch.setenv("PASSAGE_DIR", "/y/store")
    assert secrets.passage_store() == "/y/store"           # read when called
    assert secrets.passage_entry_file("a/b") == "/y/store/a/b.age"


def test_with_no_passage_dir_it_looks_in_the_default_store(passage, monkeypatch):
    monkeypatch.delenv("PASSAGE_DIR")
    with pytest.raises(SecretError) as caught:
        secrets.passage_backend(ENTRY)
    assert str(caught.value) == (f"no passage entry {ENTRY} in "
                                 f"{os.path.join(os.environ['HOME'], '.passage', 'store')}")


def test_no_failure_reason_carries_the_value(passage, capsys):
    token = synthetic_token()
    passage.insert(ENTRY, f"{token}\n")
    reasons = []
    for mode in ("fail", "silent", "empty"):
        passage.mode(mode)
        with pytest.raises(SecretError) as caught:
            secrets.passage_backend(ENTRY)
        reasons.append(str(caught.value) + repr(caught.value))
    assert_absent(token, capsys, *reasons)


def test_pass_is_untouched(passage):
    """`pass:` still runs `pass` (here the walled stub), never passage."""
    passage.insert(ENTRY, "value\n")
    passage.forget()
    with pytest.raises(AssertionError, match="test tried to run"):
        secrets.pass_backend(ENTRY)                     # the guard: the walled `pass`
    assert passage.calls() == []


# --- where it is registered -------------------------------------------------------------


def test_the_account_registry_has_passage_at_the_cost_of_a_touch():
    assert reg.BACKENDS["passage"] is secrets.passage_backend
    assert reg.COST["passage"] == "touch"
    assert reg.parse_source(f"passage:{ENTRY}") == reg.Source("passage", ENTRY)
    account = reg.make_account("n", "new@example.com", [f"passage:{ENTRY}", "file:~/t"])
    assert st.sources_text(account) == "passage (touch), file (free)"


def test_session_start_does_not_get_passage():
    """OQ-6 default: the account registry only (design §5)."""
    assert set(secrets.BACKENDS) == {"pass"}
    [result] = secrets.resolve_secrets([DeclaredSecret("T", "passage", ENTRY)])
    assert result.error == "unknown backend 'passage' (known: pass)"


def test_the_add_help_names_passage():
    out = io.StringIO()
    assert sessiontools.account.main(["add", "-h"], out=out, err=io.StringIO()) == 0
    assert "passage:ENTRY" in " ".join(out.getvalue().split())


def test_add_registers_a_passage_source_end_to_end_fp_known(passage, capsys):
    """SC1, design §8: `session account add NAME --source passage:ENTRY`
    through the real backend table and the stub passage."""
    token = synthetic_token()
    passage.insert(ENTRY, f"{token}\n")
    passage.forget()
    home = Path(os.environ["HOME"])
    out, err = io.StringIO(), io.StringIO()
    code = sessiontools.account.main(["add", "n", "--email", "new@example.com",
                                      "--source", f"passage:{ENTRY}"],
                                     home=home, out=out, err=err)
    assert code == 0, err.getvalue()
    [account] = reg.load(reg.default_path(home)).accounts
    assert account == reg.Account("n", "new@example.com", (reg.Source("passage", ENTRY),),
                                  reg.fingerprint(token))
    assert out.getvalue().startswith("added n (new@example.com): passage (touch); fp known")
    assert passage.calls() == [f"passage show {ENTRY}"]       # one decrypt
    assert_absent(token, capsys, out.getvalue(), err.getvalue())


@pytest.mark.parametrize("prefix", [None, "vault/tokens"])
def test_the_entry_new_names_resolves_as_a_passage_source(passage, prefix, capsys,
                                                          tmp_path, monkeypatch):
    """do-008: `new` names its entry `<account.passage_prefix>/<E>`, the
    neutral default or a host setting, and `passage:` resolves that entry as
    given: the backend takes the whole entry, so any prefix reaches it."""
    from sessiontools.account import new as nw
    home = tmp_path / "home"
    if prefix is not None:
        folder = host(home, "session")
        folder.mkdir(parents=True)
        (folder / "session.json").write_text(
            json.dumps({"account": {"passage_prefix": prefix}}))
    entry = nw.entry_for("new@example.com", nw.entry_prefix(home=home))
    assert entry == f"{prefix or nw.DEFAULT_PREFIX}/new@example.com"
    token = synthetic_token()
    passage.insert(entry, f"{token}\n")
    passage.forget()
    out, err = io.StringIO(), io.StringIO()
    code = sessiontools.account.main(["add", "n", "--email", "new@example.com",
                                      "--source", f"passage:{entry}"],
                                     home=home, out=out, err=err)
    assert code == 0, err.getvalue()
    [account] = reg.load(reg.default_path(home)).accounts
    assert account.sources == (reg.Source("passage", entry),)
    assert account.fp == reg.fingerprint(token)
    assert passage.calls() == [f"passage show {entry}"]
    assert_absent(token, capsys, out.getvalue(), err.getvalue())


def test_add_of_a_missing_passage_entry_exits_4_and_registers_nothing(passage):
    home = Path(os.environ["HOME"])
    out, err = io.StringIO(), io.StringIO()
    code = sessiontools.account.main(["add", "n", "--email", "new@example.com",
                                      "--source", f"passage:{ENTRY}"],
                                     home=home, out=out, err=err)
    assert code == 4
    assert not reg.default_path(home).exists()
    assert passage.calls() == []
    assert f"no passage entry {ENTRY}" in err.getvalue()
