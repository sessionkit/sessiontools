"""An in-place launch needs a terminal: `check_terminal`.

The vendor is exec'd with this process's stdio, and an interactive claude
needs stdin AND stdout to be terminals: with stdout piped it switches
itself to print mode and fails asking for a prompt, and with stdin
redirected it cannot read the keyboard. So `session start` with no terminal
is refused up front, exit 2, before the default seat's gates and claim and
before anything is minted. The message names what to do instead. A dry run,
a typed print, help or version flag, and a tmux placement (the pane has its
own terminal) are not refused.

The suite's own stdio is pipes, which is exactly the no-terminal case: the
e2e tests here run the verb with them on purpose (`terminal=False`).
"""

from __future__ import annotations

import io

import pytest
from start_support import launch_e2e
from test_start_singleton import SEAT, e2e_path, message_stub, on_macos

from sessiontools import start
from sessiontools.start import run
from sessiontools.start.errors import EXIT_USAGE, LaunchError

NO_TERMINAL = "no terminal here"


class Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


# ---------- units -------------------------------------------------------------------

def test_no_terminal_is_a_usage_refusal_naming_what_to_do():
    with pytest.raises(LaunchError) as refused:
        run.check_terminal([], dry_run=False, interactive=False)
    assert refused.value.code == EXIT_USAGE
    message = str(refused.value)
    assert message.startswith(NO_TERMINAL)
    assert "stdin and stdout" in message
    assert "run it from a terminal" in message.lower()
    assert "--in-tmux" in message


def test_at_a_terminal_nothing_is_refused():
    run.check_terminal([], dry_run=False, interactive=True)
    run.check_terminal(["hello", "--model", "opus"], dry_run=False, interactive=True)


@pytest.mark.parametrize("stdin, stdout", [(False, True), (True, False), (False, False)])
def test_both_stdin_and_stdout_must_be_terminals(monkeypatch, stdin, stdout):
    """Measured on the vendor: either half missing fails it, so either
    half missing is refused."""
    monkeypatch.setattr("sys.stdin", Stream(stdin))
    monkeypatch.setattr("sys.stdout", Stream(stdout))
    with pytest.raises(LaunchError):
        run.check_terminal([], dry_run=False)


def test_stdin_and_stdout_terminals_pass_whatever_stderr_is(monkeypatch):
    monkeypatch.setattr("sys.stdin", Stream(True))
    monkeypatch.setattr("sys.stdout", Stream(True))
    monkeypatch.setattr("sys.stderr", Stream(False))
    run.check_terminal([], dry_run=False)


def test_a_stream_that_cannot_answer_is_no_terminal(monkeypatch):
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr("sys.stdin", closed)          # isatty on a closed stream raises
    monkeypatch.setattr("sys.stdout", Stream(True))
    with pytest.raises(LaunchError):
        run.check_terminal([], dry_run=False)
    monkeypatch.setattr("sys.stdin", object())        # no isatty at all
    with pytest.raises(LaunchError):
        run.check_terminal([], dry_run=False)


def test_a_dry_run_needs_no_terminal():
    run.check_terminal([], dry_run=True, interactive=False)


@pytest.mark.parametrize("flag", ["-p", "--print", "-h", "--help", "-v", "--version"])
def test_a_typed_print_help_or_version_flag_needs_no_terminal(flag):
    """The caller chose print mode (`echo q | session start - -p`), or asked
    the vendor a question it answers and exits."""
    run.check_terminal(["hello", flag], dry_run=False, interactive=False)


def test_a_word_that_only_looks_like_an_exempt_flag_is_still_refused():
    for rest in (["--printer"], ["print"], ["--model", "opus"]):
        with pytest.raises(LaunchError):
            run.check_terminal(rest, dry_run=False, interactive=False)


def test_main_refuses_before_anything_is_resolved(monkeypatch, capsys, tmp_path):
    """In main, the refusal is the direct road's first act: an agent that
    does not exist is not even looked up."""
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(run, "_isatty", lambda _stream: False)
    monkeypatch.setattr(start, "resolve", lambda *_a, **_k: pytest.fail("resolved"))
    monkeypatch.setattr(start, "bare_start", lambda *_a, **_k: pytest.fail("seat road"))
    assert start.main(["nosuch"]) == EXIT_USAGE
    assert start.main([]) == EXIT_USAGE
    assert capsys.readouterr().err.count(f"session start: {NO_TERMINAL}") == 2


# ---------- end to end: the verb with pipes for stdio -------------------------------

def test_a_launch_with_no_terminal_is_refused_and_nothing_runs(tmp_path):
    result, record, _work = launch_e2e(tmp_path, "scout", "hello", terminal=False)
    assert result.returncode == EXIT_USAGE, result.stderr
    assert result.stderr.startswith(f"session start: {NO_TERMINAL}")
    assert "--in-tmux" in result.stderr
    assert result.stdout == ""
    assert not (record / "argv").exists(), "the vendor ran"
    sessions = tmp_path / "agents" / "sessions"
    assert not sessions.exists() or not list(sessions.iterdir()), "minted an identity"


def test_the_same_launch_at_a_terminal_runs(tmp_path):
    result, record, _work = launch_e2e(tmp_path, "scout", "hello")
    assert result.returncode == 0, result.stderr
    assert (record / "argv").exists()


def test_a_typed_print_flag_launches_with_no_terminal(tmp_path):
    """`--print` is the caller's choice, and reaches the vendor as typed."""
    result, record, _work = launch_e2e(tmp_path, "scout", "--print", "hello",
                                       terminal=False)
    assert result.returncode == 0, result.stderr
    argv = (record / "argv").read_text().split("\0")[:-1]
    assert "--print" in argv


def test_a_dry_run_with_no_terminal_reports(tmp_path):
    result, record, _work = launch_e2e(tmp_path, "--dry-run", "scout", terminal=False)
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert not (record / "argv").exists()


@on_macos
def test_a_bare_start_with_no_terminal_takes_no_seat(tmp_path):
    """The default seat's road comes after the refusal: no gate line, no
    claim. (At a terminal, the same launch takes the seat:
    test_start_singleton.test_first_bare_start_takes_the_seat_end_to_end.)"""
    stubs = tmp_path / "stubs"
    message_stub(stubs)
    state = tmp_path / "state"
    result, record, _work = launch_e2e(
        tmp_path, terminal=False,
        extra_env={"SESSION_STATE_DIR": str(state), "PATH": e2e_path(tmp_path, stubs)})
    assert result.returncode == EXIT_USAGE, result.stderr
    [line] = result.stderr.splitlines()                 # no gate line before it
    assert NO_TERMINAL in line
    assert not (state / "holders" / f"{SEAT}.json").exists(), "claimed the seat"
    assert not (record / "argv").exists()
