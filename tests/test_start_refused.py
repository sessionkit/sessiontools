"""A seat refused because a REQUIRED context generator failed (ST-307,
design.md §4-6, §8 items 4-8): `main` prints the refusal on stderr, writes
`<session dir>/launch/refused` on a live launch, never execs the vendor,
and exits 1; `--dry-run` says the same, writes nothing, prints nothing on
stdout and exits 1. An unmarked failure still launches as it always did.

The live launches here never exec the runner: the subprocess ones exec a
stub `claude` (`launch_e2e`), and every in-process one makes the exec
fatal first (`never_exec`) or a stand-in that raises (`Exec`)."""

from __future__ import annotations

import functools
import glob
import os
import stat
from pathlib import Path

import pytest
from start_support import launch_e2e, make_agents_root, plan_for, pretend_a_terminal

from sessiontools import start
from sessiontools.start import context, placement, plan, record, run
from sessiontools.start.errors import EXIT_LAUNCH_FAILED, EXIT_OK, ContextRefused

#: A required system-prompt entry that writes both streams, then exits 1.
FAILING = "printf 'out-line\\n'; echo err-line >&2; exit 1"
#: A required entry that writes, then hangs past the (injected) 1 s cap.
HANGING = "printf 'partial-out\\n'; exec sleep 30"


def context_front_matter(*entries: str, door: str = "system-prompt") -> str:
    """`start: context: DOOR:` with each entry as written (YAML lines)."""
    body = "".join(f"      {line}\n" for entry in entries for line in entry.split("\n"))
    return f"start:\n  context:\n    {door}:\n{body}"


def required(command: str, flag: str = "true") -> str:
    return f"- run: {command!r}\n  required: {flag}"


def plain(command: str) -> str:
    return f"- {command!r}"


class Exec(Exception):
    """A stand-in exec: the launch got as far as starting the vendor."""


def never_exec(monkeypatch) -> None:
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("a refused launch exec'd"))
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("a refused launch exec'd"))
    placed = lambda *a, **k: pytest.fail("a refused launch was placed in tmux")
    monkeypatch.setattr(start, "place_pane", placed)
    monkeypatch.setattr(placement, "place_pane", placed)


def short_cap(monkeypatch, seconds: int = 1) -> None:
    """The 60 s cap, made 1 s for the launch's own assemble."""
    monkeypatch.setattr(plan, "build_injected_context",
                        functools.partial(context.build_injected_context, timeout=seconds))


def in_process(tmp_path, monkeypatch, front_matter: str) -> str:
    """A disposable agent, a HOME, a cwd, a terminal: ready for `main`.
    Returns the sessions root."""
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path, front_matter=front_matter))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("SESSIONS_DIR", str(sessions))
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.chdir(tmp_path)
    pretend_a_terminal(monkeypatch)
    return str(sessions)


def refused_files(root) -> list[str]:
    return glob.glob(os.path.join(str(root), "**", record.REFUSED_FILE), recursive=True)


def refusal_block(err: str) -> str:
    """The stderr from the REFUSED line on (the announce lines go)."""
    return err[err.index("session start: REFUSED"):]


# ---------- 4. main, live: refused, recorded, never exec'd ------------------

def test_a_failing_required_entry_refuses_a_live_launch_end_to_end(tmp_path):
    """The subprocess form: a stub `claude` on PATH that would leave its argv
    if it were ever exec'd. Unkeyed: the record is not gated on --keyed."""
    result, stub_record, _work = launch_e2e(
        tmp_path, "scout", front_matter=context_front_matter(required(FAILING)))
    assert result.returncode == EXIT_LAUNCH_FAILED, result.stderr
    assert not (stub_record / "argv").exists(), "the vendor was exec'd"
    err = result.stderr
    assert f"REFUSED: required context generator `{FAILING}` exited 1 -- err-line" in err
    assert "out-line" in err and "err-line" in err
    assert "the vendor was not started." in err
    [path] = refused_files(tmp_path / "agents" / "sessions")
    assert f"record:  {path}" in err
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    text = Path(path).read_text()
    assert text.startswith("refused: required context generator failed\n")
    assert "door: system-prompt\n" in text and "entry: 1 of 1\n" in text
    assert "out-line" in text and "err-line" in text and "exited 1" in text
    assert "launch_command: session start scout\n" in text
    launch_dir = os.path.dirname(path)
    assert sorted(os.listdir(launch_dir)) == [".gitignore", record.REFUSED_FILE], \
        "a refusal writes no env, no command and no door file"


def test_a_failing_required_entry_refuses_in_process_and_never_execs(
        tmp_path, monkeypatch, capsys):
    sessions = in_process(tmp_path, monkeypatch,
                          context_front_matter(plain("exit 3"), required(FAILING),
                                               plain("touch later-ran")))
    never_exec(monkeypatch)
    assert start.main(["scout"]) == EXIT_LAUNCH_FAILED
    out, err = capsys.readouterr()
    assert out == ""
    assert not (tmp_path / "later-ran").exists(), "an entry after the refusal ran"
    # The earlier, unmarked failure is still said, in today's line, before it.
    assert err.index("context generator failed (exit 3): exited 3") < \
        err.index("REFUSED")
    assert "  list:    system-prompt, entry 2 of 3 (nothing after it ran)" in err
    [path] = refused_files(sessions)
    assert "context generator failed (exit 3)" in Path(path).read_text()


def test_a_timed_out_required_entry_refuses_with_its_partial_output(
        tmp_path, monkeypatch, capsys):
    sessions = in_process(tmp_path, monkeypatch, context_front_matter(required(HANGING)))
    short_cap(monkeypatch)
    never_exec(monkeypatch)
    assert start.main(["scout"]) == EXIT_LAUNCH_FAILED
    err = capsys.readouterr().err
    assert f"`{HANGING}` timed out after 1s" in err
    assert "output:  (12 bytes; partial: timed out)" in err
    assert "    partial-out" in err
    [path] = refused_files(sessions)
    text = Path(path).read_text()
    assert "partial-out" in text and "timed out after 1s" in text


def test_a_failing_required_initial_entry_refuses_through_main(tmp_path, monkeypatch, capsys):
    """design-001 review F3: the other door refuses the same way, and says
    which door it was."""
    sessions = in_process(tmp_path, monkeypatch,
                          context_front_matter(required(FAILING), door="initial"))
    never_exec(monkeypatch)
    assert start.main(["scout"]) == EXIT_LAUNCH_FAILED
    err = capsys.readouterr().err
    assert "  list:    initial, entry 1 of 1" in err
    [path] = refused_files(sessions)
    assert "door: initial\n" in Path(path).read_text()


def test_a_record_that_cannot_be_written_never_costs_the_verdict(
        tmp_path, monkeypatch, capsys):
    in_process(tmp_path, monkeypatch, context_front_matter(required(FAILING)))
    never_exec(monkeypatch)

    def broken(*_args, **_kwargs):
        raise PermissionError(13, "stand-in for a read-only sessions root")
    monkeypatch.setattr(run, "write_refusal", broken)
    assert start.main(["scout"]) == EXIT_LAUNCH_FAILED
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "(NOT WRITTEN: PermissionError:" in err


# ---------- 5. --dry-run: the same verdict, nothing written ------------------

@pytest.mark.parametrize("entry, why", [
    (required(FAILING), "exited 1 -- err-line"),
    (required(HANGING), "timed out after 1s"),
])
def test_a_dry_run_refuses_the_same_way_and_writes_nothing(
        tmp_path, monkeypatch, capsys, entry, why):
    sessions = in_process(tmp_path, monkeypatch, context_front_matter(entry))
    short_cap(monkeypatch)
    never_exec(monkeypatch)
    assert start.main(["--dry-run", "scout"]) == EXIT_LAUNCH_FAILED
    out, err = capsys.readouterr()
    assert out == "", "a refused dry run has no plan to report"
    assert "REFUSED: required context generator" in err and why in err
    assert "(dry run: not written)" in err
    assert not os.path.exists(sessions), "a dry run minted a session dir"
    assert refused_files(tmp_path) == []


def test_live_and_dry_run_print_one_block_but_the_record_line(tmp_path, monkeypatch, capsys):
    """One renderer, one path through assemble: the two blocks differ only
    in where the record went."""
    in_process(tmp_path, monkeypatch, context_front_matter(plain("exit 3"), required(FAILING)))
    never_exec(monkeypatch)
    assert start.main(["--dry-run", "scout"]) == EXIT_LAUNCH_FAILED
    dry = capsys.readouterr().err
    assert start.main(["scout"]) == EXIT_LAUNCH_FAILED
    live = capsys.readouterr().err

    def without_record(err: str) -> str:
        return "\n".join(line for line in err.splitlines() if "record:" not in line)
    assert without_record(dry) == without_record(live)
    assert "(dry run: not written)" in dry and "(dry run" not in live


# ---------- 6. unmarked failures: today's marker, the launch proceeds --------

def test_an_unmarked_failure_still_launches_with_todays_marker(tmp_path):
    result, stub_record, _work = launch_e2e(
        tmp_path, "scout", front_matter=context_front_matter(plain(FAILING)))
    assert result.returncode == EXIT_OK, result.stderr
    assert (stub_record / "argv").exists(), "an unmarked failure stopped the launch"
    assert f"context generator failed ({FAILING}): exited 1 -- err-line" in result.stderr
    assert "REFUSED" not in result.stderr
    assert refused_files(tmp_path / "agents" / "sessions") == []
    [prompt] = glob.glob(str(tmp_path / "agents" / "sessions" / "*" / "launch"
                             / "session-start-system-prompt-*.md"))
    assert (f"CONTEXT GENERATOR FAILED: `{FAILING}` exited 1 -- err-line."
            in Path(prompt).read_text())


@pytest.mark.parametrize("entry", [plain(HANGING), required(HANGING, "false")])
def test_an_unmarked_timeout_still_launches(tmp_path, monkeypatch, capsys, entry):
    in_process(tmp_path, monkeypatch, context_front_matter(entry))
    short_cap(monkeypatch)
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)

    def fake_exec(*_args):
        raise Exec
    monkeypatch.setattr(os, "execvpe", fake_exec)
    with pytest.raises(Exec):
        start.main(["scout"])
    err = capsys.readouterr().err
    assert "timed out after 1s" in err and "REFUSED" not in err


# ---------- 7. opt-in: no marking is today's plan, exactly -------------------

def test_a_file_with_no_marking_plans_exactly_as_required_false(tmp_path, monkeypatch):
    """Failing and timing-out entries, unmarked, against the same entries
    each marked `required: false`: the same injected context, argv and
    script (the projected session id aside: a dry run mints a fresh one)."""
    short_cap(monkeypatch)
    entries = ("exit 3", HANGING, "echo fine")

    def planned(name: str, marking):
        front = context_front_matter(*(marking(entry) for entry in entries))
        built = plan_for(tmp_path / name, front, flags=["--dry-run"])
        sid = built.identity.session_id
        root = str(tmp_path / name)

        def norm(text: str) -> str:
            return text.replace(sid, "<sid>").replace(root, "<root>")
        return (norm(built.injected.system_prompt), norm(built.injected.initial),
                [norm(token) for token in built.argv], norm(built.script))

    (tmp_path / "unmarked").mkdir()
    (tmp_path / "marked").mkdir()
    unmarked = planned("unmarked", plain)
    marked = planned("marked", lambda command: required(command, "false"))
    assert unmarked == marked
    assert "CONTEXT GENERATOR FAILED" in unmarked[0]


# ---------- 8. write_refusal: the guard and written-once ---------------------

def test_write_refusal_writes_one_private_ignored_file(tmp_path):
    session = tmp_path / "sid"
    session.mkdir()
    path = record.write_refusal(str(session), "why\n")
    assert path == str(session / "launch" / record.REFUSED_FILE)
    assert Path(path).read_text() == "why\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(session / "launch").st_mode) == 0o700
    assert (session / "launch" / ".gitignore").read_text().strip() == "*"


def test_write_refusal_leaves_an_existing_refusal_as_it_is(tmp_path, capsys):
    session = tmp_path / "sid"
    session.mkdir()
    assert record.write_refusal(str(session), "first\n")
    assert record.write_refusal(str(session), "second\n") is None
    assert (session / "launch" / record.REFUSED_FILE).read_text() == "first\n"
    assert "left as it is" in capsys.readouterr().err


def test_write_refusal_writes_nothing_where_git_would_track_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(record, "git_ignore_state", lambda *a, **k: "not-ignored")
    session = tmp_path / "sid"
    session.mkdir()
    assert record.write_refusal(str(session), "why\n") is None
    assert not (session / "launch" / record.REFUSED_FILE).exists()
    assert "NOT WRITTEN" in capsys.readouterr().err


def test_a_guarded_record_leaves_the_verdict_as_it_was(tmp_path, monkeypatch, capsys):
    sessions = in_process(tmp_path, monkeypatch, context_front_matter(required(FAILING)))
    monkeypatch.setattr(record, "git_ignore_state", lambda *a, **k: "not-ignored")
    never_exec(monkeypatch)
    assert start.main(["scout"]) == EXIT_LAUNCH_FAILED
    err = capsys.readouterr().err
    assert "NOT WRITTEN" in err and "(not written: see above)" in err
    assert "REFUSED" in err
    assert refused_files(sessions) == []


# ---------- the renderer -----------------------------------------------------

def test_an_empty_stream_says_none_and_no_record_line_without_one():
    result = context.ContextResult("x", "", "exited 2", required=True)
    text = run.render_refusal(ContextRefused(result, "system-prompt", (1, 1)))
    assert "  output:  (none)" in text and "  stderr:  (none)" in text
    assert "record:" not in text
    assert text.endswith("  the vendor was not started.\n")
