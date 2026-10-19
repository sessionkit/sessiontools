"""The TokenStore seam (ST-159 design §4, do-002): `PassageCLI` and
`FakeTokenStore`.

HARD LIMITS (ST-159 slice notes): no real `passage`, `age` or `pass`, no
store, no Keychain, no `claude`. Every token is synthetic
(`sk-ant-oat01-TEST-<random>`).

The autouse guard refuses any subprocess whose argv[0] is `security`,
`pass`, `passage`, `age` or `claude` UNLESS it resolves (real path) inside
this test's own stub folder (design §8), and it RECORDS every argv any
subprocess call was given, so a test can assert the token never reached
one.

The stub `passage` keeps a scratch store under the test's HOME
(`PASSAGE_DIR`): `insert -m` stores the BASE64 of stdin in `<entry>.age`,
so a plaintext token on disk is a leak, and `show` decodes it. Its mode
file picks a failure: `insert-fail` (exit 1, echoing stdin on stderr),
`commit-fail` (writes the entry, then exits 0 with git's error, and the
token, on stderr: design §1.2), `hang` (writes the entry, then waits on a
child, as a commit hook or a signing prompt would), `corrupt` (stores
another value), `no-write` (exits 0 having written nothing) and
`read-fail` (`show` fails, with the value on stderr).
"""

from __future__ import annotations

import base64
import os
import secrets as pysecrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sessiontools.account import tokenstore as ts
from sessiontools.account.tokenstore import (
    EXIT_NOT_SAVED,
    EXIT_STORE,
    EXIT_USAGE,
    FakeTokenStore,
    PassageCLI,
    StoreError,
    TokenStore,
)

FORBIDDEN = ("security", "pass", "passage", "age", "claude")
ENTRY = "claude/oauth-tokens/new@example.com"

# The stub passage: logs its argv (never stdin), then acts on its mode file.
PASSAGE_STUB = """#!/usr/bin/env bash
echo "passage $*" >> "%(log)s"
mode=$(cat "%(modes)s" 2>/dev/null || echo ok)
store="${PASSAGE_DIR:-$HOME/.passage/store}"
case "$1" in
  insert)
    [ "$2" = "-m" ] && [ $# -eq 3 ] || { echo "stub: only insert -m ENTRY" >&2; exit 2; }
    file="$store/$3.age"
    echo "Enter contents of $3 and press Ctrl+D when finished:"
    case "$mode" in
      insert-fail)
        printf 'Password encryption aborted. %%s\\nsecond line\\n' "$(cat)" >&2; exit 1 ;;
      no-write) cat > /dev/null; exit 0 ;;
    esac
    mkdir -p "$(dirname "$file")"
    if [ "$mode" = corrupt ]; then
      cat > /dev/null; printf 'sk-ant-oat01-TEST-not-the-token\\n' | base64 > "$file"
    else
      base64 > "$file"
    fi
    case "$mode" in
      commit-fail)
        printf 'error: gpg failed to sign the data %%s\\nfatal: failed to write commit object\\n' \\
          "$(base64 --decode < "$file")" >&2
        exit 0 ;;
      hang)
        sleep 60 &
        echo $! > "%(pidfile)s.tmp" && mv "%(pidfile)s.tmp" "%(pidfile)s"
        wait ;;
    esac
    ;;
  show)
    case "$mode" in
      read-fail)
        printf 'age: error: no identity matched %%s\\n' "$(base64 --decode < "$store/$2.age")" >&2
        exit 1 ;;
    esac
    [ -f "$store/$2.age" ] || { echo "Error: $2 is not in the password store." >&2; exit 1; }
    base64 --decode < "$store/$2.age"
    ;;
  *) echo "stub: unknown verb $1" >&2; exit 2 ;;
esac
"""


def _argv(args, kwargs) -> list[str]:
    argv = args[0] if args else kwargs.get("args")
    if isinstance(argv, (list, tuple)):
        return [str(a) for a in argv]
    if isinstance(argv, bytes):
        return argv.decode().split()
    if isinstance(argv, str):
        return argv.split()
    return []


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


@pytest.fixture
def argvs() -> list[list[str]]:
    """Every argv any subprocess call was given, in this test."""
    return []


@pytest.fixture(autouse=True)
def _no_host_passage_age_pass_or_claude(monkeypatch, stub_dir, argvs, tmp_path_factory):
    """A subprocess naming a FORBIDDEN tool raises unless it resolves inside
    this test's stub folder. Every argv is recorded in `argvs`."""
    allowed = os.path.realpath(stub_dir) + os.sep

    def refused(argv, kwargs) -> str | None:
        if not argv or os.path.basename(argv[0]) not in FORBIDDEN:
            return None
        found = _resolve(argv[0], kwargs)
        if found is None or found.startswith(allowed):
            return None                 # nothing to run, or the test's own stub
        return found

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, _name=name, **kwargs):
            argv = _argv(args, kwargs)
            argvs.append(argv)
            found = refused(argv, kwargs)
            if found is not None:
                raise AssertionError(f"test tried to run {found!r} via subprocess.{_name}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)
    empty = tmp_path_factory.mktemp("claude-config-empty")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("PASSAGE_DIR", raising=False)
    monkeypatch.delenv("PASSAGE_IDENTITIES_FILE", raising=False)


def synthetic_token() -> str:
    return "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)


class Passage:
    """The stub passage on PATH over a scratch store in the test's HOME."""

    def __init__(self, stub_dir: Path, home: Path, monkeypatch) -> None:
        self.store = home / "scratch-passage" / "store"
        self.store.mkdir(parents=True)
        self.identities = home / "scratch-passage" / "identities"
        self.identities.write_text("AGE-SECRET-KEY-1TEST\n")
        self.log = stub_dir.parent / "passage-calls"
        self.modes = stub_dir.parent / "passage-mode"
        self.pidfile = stub_dir.parent / "passage-child-pid"
        self.log.write_text("")
        stub = stub_dir / "passage"
        stub.write_text(PASSAGE_STUB % {"log": self.log, "modes": self.modes,
                                        "pidfile": self.pidfile})
        stub.chmod(0o755)
        monkeypatch.setenv("PASSAGE_DIR", str(self.store))
        monkeypatch.setenv("PASSAGE_IDENTITIES_FILE", str(self.identities))
        monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")

    def mode(self, mode: str) -> None:
        self.modes.write_text(mode)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def entry_file(self, entry: str = ENTRY) -> Path:
        return self.store / f"{entry}.age"

    def stored(self, entry: str = ENTRY) -> bytes:
        return base64.b64decode(self.entry_file(entry).read_bytes())


@pytest.fixture
def passage(stub_dir, monkeypatch) -> Passage:
    return Passage(stub_dir, Path(os.environ["HOME"]), monkeypatch)


def assert_absent(token: str, capsys, argvs, *texts: str) -> None:
    """The token, and any 8 characters of its body in a row, are in no
    argv, no captured output and no text given; no file under HOME holds it
    in plaintext (the stub store holds base64)."""
    out, err = capsys.readouterr()
    body = token[len("sk-ant-oat01-"):]
    pieces = [token] + [body[i:i + 8] for i in range(len(body) - 7)]
    for text in (out, err, *texts, *(" ".join(a) for a in argvs)):
        for piece in pieces:
            assert piece not in text, "a token piece leaked"
    for path in Path(os.environ["HOME"]).rglob("*"):
        if path.is_file():
            assert token.encode() not in path.read_bytes(), f"token leaked into {path}"


# A script under test must signal readiness (a pid file, written first and
# atomically) before the runner's timeout can fire, or a loaded machine ends
# it before it has anything to kill and the test proves nothing (ST-171). So
# the timeout is long against a shell's start-up, and the test fails on a
# missing signal instead of passing without having proved the kill.
READY_TIMEOUT = 3


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# --- the guard ------------------------------------------------------------------------


def test_the_guard_refuses_the_hosts_tools_and_the_walled_stubs():
    for name in FORBIDDEN:
        if shutil.which(name) is None:
            continue
        with pytest.raises(AssertionError, match="test tried to run"):
            subprocess.run([name, "--version"], capture_output=True, check=False)


def test_the_guard_records_argv_and_lets_the_tests_own_stub_run(passage, argvs):
    PassageCLI().insert(ENTRY, synthetic_token())
    assert [ts.PASSAGE, "insert", "-m", ENTRY] in argvs
    assert passage.calls() == [f"passage insert -m {ENTRY}"]


# --- the protocol ---------------------------------------------------------------------


def test_both_stores_are_token_stores():
    assert isinstance(PassageCLI(), TokenStore)
    assert isinstance(FakeTokenStore(), TokenStore)


def test_describe_names_the_store_and_nothing_else(passage):
    assert PassageCLI().describe() == f"passage store {passage.store}"
    assert repr(PassageCLI()) == f"PassageCLI(store={str(passage.store)!r})"


def test_the_store_defaults_to_passages_own(monkeypatch):
    home = os.environ["HOME"]
    cli = PassageCLI()
    assert cli.store == os.path.join(home, ".passage", "store")
    assert cli.identities == os.path.join(home, ".passage", "identities")


# --- check ----------------------------------------------------------------------------


def test_check_passes_with_passage_a_store_and_identities(passage, argvs):
    PassageCLI().check()
    assert argvs == []                  # nothing is run to check


def test_check_refuses_a_host_with_no_passage(passage, monkeypatch, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(StoreError, match="passage is not installed") as caught:
        PassageCLI().check()
    assert caught.value.code == EXIT_STORE


def test_check_refuses_a_missing_store(passage, monkeypatch, tmp_path):
    monkeypatch.setenv("PASSAGE_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(StoreError, match="no passage store at .*nowhere") as caught:
        PassageCLI().check()
    assert caught.value.code == EXIT_STORE


def test_check_refuses_a_missing_identities_file(passage):
    passage.identities.unlink()
    with pytest.raises(StoreError, match="no passage identities file") as caught:
        PassageCLI().check()
    assert caught.value.code == EXIT_STORE


# --- entries --------------------------------------------------------------------------


@pytest.mark.parametrize("entry", [
    "", "/abs/entry", "a/../b", "..", "a//b", "a/./b", "./a", "a/", "-c", "--force",
    "a\nb", "a\x1bb",
])
def test_an_entry_outside_the_rules_is_refused_as_usage(entry, passage, argvs):
    with pytest.raises(StoreError) as caught:
        PassageCLI().exists(entry)
    assert caught.value.code == EXIT_USAGE
    with pytest.raises(StoreError):
        PassageCLI().insert(entry, synthetic_token())
    with pytest.raises(StoreError):
        FakeTokenStore().insert(entry, synthetic_token())
    assert argvs == []


def test_a_token_shaped_entry_is_refused_and_never_quoted(passage, argvs):
    token = synthetic_token()
    for entry in (token, f"oauth-tokens/{token}", "x/sk-ant-short"):
        with pytest.raises(StoreError, match="looks like a token") as caught:
            PassageCLI().insert(entry, synthetic_token())
        assert caught.value.code == EXIT_USAGE
        assert "sk-ant-" not in str(caught.value)
    assert argvs == []


def test_exists_sees_an_entry_or_a_folder_and_decrypts_nothing(passage, argvs):
    cli = PassageCLI()
    assert not cli.exists(ENTRY)
    passage.entry_file().parent.mkdir(parents=True)
    passage.entry_file().write_text("c2VjcmV0Cg==\n")
    assert cli.exists(ENTRY)
    assert cli.exists("claude")                   # a folder
    assert not cli.exists("claude/other")
    assert argvs == [] and passage.calls() == []


# --- insert ---------------------------------------------------------------------------


def test_insert_puts_the_token_on_stdin_and_never_on_argv(passage, argvs, capsys):
    token = synthetic_token()
    cli = PassageCLI()
    assert cli.insert(ENTRY, token) is None
    assert passage.calls() == [f"passage insert -m {ENTRY}"]       # never -f
    assert cli.insert_argv(ENTRY) == ["passage", "insert", "-m", ENTRY]
    assert passage.stored() == (token + "\n").encode()
    assert cli.read(ENTRY) == token
    assert_absent(token, capsys, argvs)


def test_insert_never_overwrites_an_entry_or_a_folder(passage, argvs):
    passage.entry_file().parent.mkdir(parents=True)
    passage.entry_file().write_text("b2xkCg==\n")
    with pytest.raises(StoreError, match="already exists") as caught:
        PassageCLI().insert(ENTRY, synthetic_token())
    assert caught.value.code == EXIT_NOT_SAVED
    assert caught.value.entry == ENTRY and not caught.value.written
    assert passage.entry_file().read_text() == "b2xkCg==\n"
    with pytest.raises(StoreError, match="already exists"):
        PassageCLI().insert("claude", synthetic_token())
    assert argvs == [] and passage.calls() == []


def test_exists_is_checked_again_just_before_the_insert(passage):
    """The entry appearing between `new`'s step-3 check and its insert (a
    second writer during the sign-in) is refused, never overwritten:
    passage would overwrite it silently with stdin on a pipe."""
    cli = PassageCLI()
    assert not cli.exists(ENTRY)        # step 3
    passage.entry_file().parent.mkdir(parents=True)
    passage.entry_file().write_text("b3RoZXIK\n")
    seen = []

    def runner(argv, stdin, timeout):
        seen.append(argv)
        raise AssertionError("passage must not run")

    with pytest.raises(StoreError, match="already exists"):
        PassageCLI(runner=runner).insert(ENTRY, synthetic_token())
    assert seen == [] and passage.entry_file().read_text() == "b3RoZXIK\n"


def test_the_runner_gets_the_token_on_stdin_only():
    token = synthetic_token()
    seen = []

    def runner(argv, stdin, timeout):
        seen.append((list(argv), stdin, timeout))
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(StoreError, match="timed out after 7s"):
        PassageCLI(runner=runner, timeout=7).insert(ENTRY, token)
    assert seen == [(["passage", "insert", "-m", ENTRY], (token + "\n").encode(), 7)]
    assert "-f" not in seen[0][0] and token not in " ".join(seen[0][0])


@pytest.mark.parametrize("token", ["", " sk-ant-oat01-TEST-x", "sk-ant-oat01-TEST-x\n",
                                   "a\nb", "a\rb", None])
def test_a_value_that_would_not_read_back_whole_is_refused_unquoted(token, passage):
    for store in (PassageCLI(), FakeTokenStore()):
        with pytest.raises(StoreError, match="value not shown") as caught:
            store.insert(ENTRY, token)
        assert "TEST-x" not in str(caught.value)
    assert passage.calls() == []


def test_a_failed_insert_names_the_entry_and_scrubs_passages_stderr(passage, argvs, capsys):
    passage.mode("insert-fail")       # echoes stdin, the token, on stderr
    token = synthetic_token()
    with pytest.raises(StoreError) as caught:
        PassageCLI().insert(ENTRY, token)
    error = caught.value
    assert error.code == EXIT_NOT_SAVED and error.entry == ENTRY and not error.written
    assert str(error).startswith(f"passage insert {ENTRY} exited 1: Password encryption aborted.")
    assert "second line" not in str(error)
    assert "sk-ant-" not in str(error) and "[token-shaped text removed]" in str(error)
    assert_absent(token, capsys, argvs, str(error), repr(error))


def test_an_exit_0_with_stderr_is_a_scrubbed_first_line_note(passage, argvs, capsys):
    """passage's insert exits 0 when its git commit fails (design §1.2):
    the entry is written, and stderr's first line is the person's note."""
    passage.mode("commit-fail")       # git's error, with the token in it
    token = synthetic_token()
    note = PassageCLI().insert(ENTRY, token)
    assert note is not None
    assert note.startswith(f"passage:{ENTRY} was written, but passage said: "
                           "error: gpg failed to sign the data")
    assert "may be uncommitted" in note
    assert "fatal" not in note                    # the first line only
    assert "sk-ant-" not in note
    assert passage.stored() == (token + "\n").encode()
    passage.mode("ok")
    assert PassageCLI().read(ENTRY) == token
    assert_absent(token, capsys, argvs, note)


def test_an_exit_0_that_wrote_nothing_is_not_saved(passage):
    passage.mode("no-write")
    with pytest.raises(StoreError, match="exited 0 but wrote no") as caught:
        PassageCLI().insert(ENTRY, synthetic_token())
    assert caught.value.code == EXIT_NOT_SAVED and not caught.value.written


def test_a_hung_insert_is_ended_with_its_group_and_its_entry_left_for_the_read_back(
        passage, argvs, capsys):
    """N2 (design §4, round 3): age writes the entry before the commit
    runs, so a commit that hangs past the timeout leaves a good entry. The
    timeout ends passage's whole process group, the hung child included,
    and the error says the entry was written, for `new` to read it back."""
    passage.mode("hang")
    token = synthetic_token()
    started = time.monotonic()
    # The stub must write the entry and its pid file before the timeout; a
    # short timeout lets a loaded machine end it first (ST-171).
    with pytest.raises(StoreError, match=f"passage insert {ENTRY} timed out after "
                                         f"{READY_TIMEOUT}s; "
                                         "its process group was ended") as caught:
        PassageCLI(timeout=READY_TIMEOUT).insert(ENTRY, token)
    assert time.monotonic() - started < READY_TIMEOUT + 10
    assert caught.value.code == EXIT_NOT_SAVED and caught.value.written
    child = int(passage.pidfile.read_text())
    deadline = time.monotonic() + 5
    while not _dead(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _dead(child), "the hung child outlived the timeout"
    passage.mode("ok")
    assert PassageCLI().read(ENTRY) == token       # the read-back do-005 runs
    assert_absent(token, capsys, argvs, str(caught.value))


def test_no_passage_at_insert_time_is_not_saved(passage, monkeypatch, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(StoreError, match="passage is not installed") as caught:
        PassageCLI().insert(ENTRY, synthetic_token())
    assert caught.value.code == EXIT_NOT_SAVED and not caught.value.written


def test_a_passage_that_will_not_run_is_not_saved():
    def runner(argv, stdin, timeout):
        raise PermissionError(13, "Permission denied")

    with pytest.raises(StoreError, match="could not run \\(Permission denied\\)") as caught:
        PassageCLI(runner=runner).insert(ENTRY, synthetic_token())
    assert caught.value.code == EXIT_NOT_SAVED


# --- read -----------------------------------------------------------------------------


def test_read_is_the_passage_backend(passage, capsys):
    token = synthetic_token()
    PassageCLI().insert(ENTRY, token)
    assert PassageCLI().read(ENTRY) == token
    assert passage.calls()[-1] == f"passage show {ENTRY}"
    assert "age may ask for Touch ID" in capsys.readouterr().err


def test_a_corrupted_entry_reads_back_as_another_value(passage):
    passage.mode("corrupt")
    token = synthetic_token()
    PassageCLI().insert(ENTRY, token)
    passage.mode("ok")
    assert PassageCLI().read(ENTRY) != token


def test_a_failed_read_names_the_entry_and_scrubs(passage, argvs, capsys):
    token = synthetic_token()
    PassageCLI().insert(ENTRY, token)
    passage.mode("read-fail")         # puts the value on stderr
    with pytest.raises(StoreError, match=f"read back passage:{ENTRY}: age: error") as caught:
        PassageCLI().read(ENTRY)
    assert caught.value.code == EXIT_NOT_SAVED and caught.value.entry == ENTRY
    assert "sk-ant-" not in str(caught.value)
    assert_absent(token, capsys, argvs, str(caught.value))


def test_reading_a_missing_entry_runs_nothing(passage):
    with pytest.raises(StoreError, match="no passage entry") as caught:
        PassageCLI().read(ENTRY)
    assert caught.value.code == EXIT_NOT_SAVED
    assert passage.calls() == []


# --- the default runner -----------------------------------------------------------------


def test_the_default_runner_feeds_stdin_and_captures(tmp_path):
    script = tmp_path / "echo-stdin"
    script.write_text("#!/bin/sh\ncat\necho err >&2\nexit 3\n")
    script.chmod(0o755)
    done = ts.run_in_own_group([str(script)], b"hello\n", 10)
    assert (done.returncode, done.stdout, done.stderr) == (3, b"hello\n", b"err\n")


def test_the_default_runner_ends_the_whole_group_at_the_timeout(tmp_path):
    pidfile = tmp_path / "pid"
    script = tmp_path / "hang"
    # ready first: the child's pid is published (atomically) before anything else
    script.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pidfile}.tmp && "
                      f"mv {pidfile}.tmp {pidfile}\necho partial >&2\nwait\n")
    script.chmod(0o755)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        ts.run_in_own_group([str(script)], b"", READY_TIMEOUT)
    assert caught.value.stderr is None and caught.value.output is None
    assert pidfile.exists(), "the script never signalled ready within the timeout"
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while not _dead(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _dead(child)


def test_a_child_that_ignores_sigterm_is_killed(tmp_path):
    ready = tmp_path / "ready"
    script = tmp_path / "stubborn"
    # ready only AFTER the trap is set, so the TERM cannot land before it
    script.write_text(f"#!/bin/sh\ntrap '' TERM\necho $$ > {ready}.tmp && "
                      f"mv {ready}.tmp {ready}\nwhile :; do sleep 0.1; done\n")
    script.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        ts.run_in_own_group([str(script)], b"", READY_TIMEOUT)
    assert time.monotonic() - started < READY_TIMEOUT + 10
    # a script that ignored TERM can only have ended by KILL: if it never
    # got as far as the trap, the TERM alone ended it and this proved nothing
    assert ready.exists(), "the script never set its trap within the timeout"
    shell = int(ready.read_text())
    deadline = time.monotonic() + 5
    while not _dead(shell) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _dead(shell), "the TERM-ignoring shell outlived the timeout"


# --- scrub ----------------------------------------------------------------------------


def test_scrub_removes_token_shapes_the_token_and_control_characters():
    token = synthetic_token()
    assert ts.scrub(f"a {token} b") == "a [token-shaped text removed] b"
    assert ts.scrub("cut sk-ant-") == "cut [token-shaped text removed]"
    bare = token[len("sk-ant-"):]
    assert ts.scrub(f"x {bare} y", token) == "x [token-shaped text removed] y"
    body = token[len("sk-ant-oat01-"):]
    assert ts.scrub(f"x {body} y", token) == "x [token-shaped text removed] y"
    assert ts.scrub("x x-AB y", "sk-ant-x-AB") == "x x-AB y"     # too short to replace
    assert ts.scrub("a\x1b[31mb\x07") == "a?[31mb?"
    assert ts.scrub("x" * 500).endswith("...") and len(ts.scrub("x" * 500)) == 203


def _no_piece(token: str, text: str) -> None:
    body = token[len("sk-ant-oat01-"):]
    for i in range(len(body) - 7):
        assert body[i:i + 8] not in text, f"a token piece leaked: {text!r}"


@pytest.mark.parametrize("cut", [
    lambda t: t[-25:],                            # a tail, cut from its prefix
    lambda t: t[len("sk-ant-oat01-") + 5:],       # a body, its first characters gone
    lambda t: t[:20] + "\x1b" + t[20:],           # split by a control byte
    lambda t: t[:30] + "\n" + t[30:],             # wrapped
    lambda t: "ab" + t[18:40] + "cd",             # a middle piece inside a longer run
], ids=["tail", "body", "control-byte", "wrapped", "middle"])
def test_scrub_removes_a_piece_of_the_token_without_its_prefix(cut):
    """The round-3 rule (design §8): no 8 characters of the token in a row
    in any printed line. A piece cut from `sk-ant-` is not token-SHAPED."""
    token = synthetic_token()
    text = ts.scrub(f"fatal: {cut(token)} end", token)
    _no_piece(token, text)
    assert text.startswith("fatal: ") and text.endswith(" end")
    assert "[token-shaped text removed]" in text


def test_scrub_leaves_ordinary_text_and_the_bare_prefix_alone():
    token = synthetic_token()
    line = "error: gpg failed to sign the data; fatal: failed to write commit object"
    assert ts.scrub(line, token) == line
    assert ts.scrub("unrelated-identifier_1234 here", token) == \
        "unrelated-identifier_1234 here"
    # a piece of the prefix every token shares is not this token
    assert ts.scrub("ant-oat01 log", token) == "ant-oat01 log"


def test_an_exit_0_note_holds_no_piece_of_the_token(passage, argvs, capsys, monkeypatch):
    """A passage whose stderr's first line holds the token's TAIL, with no
    prefix (a wrapped or cut echo of stdin), still gives a clean note."""
    token = synthetic_token()

    def runner(argv, stdin, timeout):
        passage.entry_file().parent.mkdir(parents=True, exist_ok=True)
        passage.entry_file().write_bytes(base64.b64encode(stdin))
        tail = stdin.decode().strip()[-30:]
        return subprocess.CompletedProcess(argv, 0, b"", f"warning: {tail}\n".encode())

    note = PassageCLI(runner=runner).insert(ENTRY, token)
    assert note is not None and "passage said: warning: [token-shaped text removed]" in note
    _no_piece(token, note)
    assert_absent(token, capsys, argvs, note)


def test_first_line_skips_blank_lines():
    assert ts.first_line(b"\n  \nfirst\nsecond\n") == "first"
    assert ts.first_line(b"") == "" and ts.first_line(None) == ""
    assert ts.first_line(b"\xff bad utf8") == "� bad utf8"


# --- the fake ---------------------------------------------------------------------------


def test_the_fake_round_trips_and_records_names_only():
    token = synthetic_token()
    store = FakeTokenStore()
    store.check()
    assert not store.exists(ENTRY)
    assert store.insert(ENTRY, token) is None
    assert store.exists(ENTRY) and store.exists("claude")
    assert store.read(ENTRY) == token
    assert store.inserts == [ENTRY] and store.reads == [ENTRY] and store.checks == 1
    assert token not in repr(store)


def test_the_fake_never_overwrites():
    store = FakeTokenStore({ENTRY: "old"})
    with pytest.raises(StoreError, match="already exists") as caught:
        store.insert(ENTRY, synthetic_token())
    assert caught.value.code == EXIT_NOT_SAVED
    assert store.entries[ENTRY] == "old" and store.inserts == []


def test_the_fake_fails_the_check():
    with pytest.raises(StoreError) as caught:
        FakeTokenStore(fail_check=True).check()
    assert caught.value.code == EXIT_STORE


def test_the_fake_fails_the_insert_and_stores_nothing():
    store = FakeTokenStore(fail_insert=True)
    with pytest.raises(StoreError) as caught:
        store.insert(ENTRY, synthetic_token())
    assert caught.value.code == EXIT_NOT_SAVED and not caught.value.written
    assert not store.exists(ENTRY)


def test_the_fake_can_fail_after_writing_as_a_hung_commit_does():
    token = synthetic_token()
    store = FakeTokenStore(fail_insert_written=True)
    with pytest.raises(StoreError) as caught:
        store.insert(ENTRY, token)
    assert caught.value.written and caught.value.entry == ENTRY
    assert store.read(ENTRY) == token


def test_the_fake_forces_a_read_back_mismatch():
    token = synthetic_token()
    store = FakeTokenStore(corrupt=True)
    store.insert(ENTRY, token)
    assert store.read(ENTRY) != token
    custom = FakeTokenStore(corrupt=lambda t: t[:20])
    custom.insert(ENTRY, token)
    assert custom.read(ENTRY) == token[:20]


def test_the_fake_fails_the_read():
    store = FakeTokenStore(fail_read=True)
    store.insert(ENTRY, synthetic_token())
    with pytest.raises(StoreError) as caught:
        store.read(ENTRY)
    assert caught.value.code == EXIT_NOT_SAVED and store.reads == [ENTRY]
    with pytest.raises(StoreError, match="no such entry"):
        FakeTokenStore().read(ENTRY)


def test_the_fake_returns_its_note():
    assert FakeTokenStore(note="passage said: x").insert(ENTRY, synthetic_token()) \
        == "passage said: x"
