"""The home-folder warning: one stderr line when a session starts in the
home folder on macOS, silence everywhere else, and a launch otherwise
unchanged. The pure function first (a temp HOME, the platform passed or
patched), then through `main()` on a dry run, which is the only ending a
test may drive in-process (a live `main` execs the runner).

Then the offer: at a terminal in the home folder, the question to start in
the work folder instead. The pure decision, the question against fake
terminal streams, and `main()` with `execute` replaced by a spy."""

from __future__ import annotations

import io
import json
import os
import re
import sys
from types import SimpleNamespace

import pytest
from start_support import make_agents_root

from sessiontools import start
from sessiontools.start import homefolder
from sessiontools.start.environment import Environment
from sessiontools.start.errors import (
    EXIT_HELD,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    LaunchError,
)
from sessiontools.start.homefolder import (
    HOME_FOLDER_NOTICE,
    MAKE,
    MOVE,
    OFFER_SOURCE,
    REFUSE,
    WORK_FOLDER,
    Offer,
    home_folder_notice,
    home_folder_offer,
    offer_work_folder,
)

WORDING = ("session start: starting in your home folder; macOS may ask for access "
           "to Desktop, Downloads, Photos and Media Library. Start in a project "
           "folder to avoid this.")


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


# ---------- the pure function ------------------------------------------------


def test_the_home_folder_on_darwin_is_the_one_line(home):
    assert home_folder_notice(str(home), {"HOME": str(home)}, "darwin") == HOME_FOLDER_NOTICE
    assert f"session start: {HOME_FOLDER_NOTICE}" == WORDING


@pytest.mark.parametrize("where", ["sub", "other"])
def test_a_subfolder_or_another_folder_is_silent(home, tmp_path, where):
    work = home / "project" if where == "sub" else tmp_path / "elsewhere"
    work.mkdir()
    assert home_folder_notice(str(work), {"HOME": str(home)}, "darwin") is None


@pytest.mark.parametrize("platform", ["linux", "win32", "freebsd14"])
def test_the_home_folder_off_macos_is_silent(home, platform):
    assert home_folder_notice(str(home), {"HOME": str(home)}, platform) is None


@pytest.mark.parametrize("environ", [{}, {"HOME": ""}])
def test_home_unset_or_empty_is_silent(home, environ):
    assert home_folder_notice(str(home), environ, "darwin") is None


def test_a_symlinked_home_matches_the_real_work_dir(home, tmp_path):
    link = tmp_path / "home-link"
    link.symlink_to(home)
    assert home_folder_notice(str(home), {"HOME": str(link)}, "darwin") == HOME_FOLDER_NOTICE


def test_a_symlinked_work_dir_matches_the_real_home(home, tmp_path):
    link = tmp_path / "home-link"
    link.symlink_to(home)
    assert home_folder_notice(str(link), {"HOME": str(home)}, "darwin") == HOME_FOLDER_NOTICE


def test_a_trailing_slash_still_matches(home):
    assert home_folder_notice(str(home) + "/", {"HOME": str(home)}, "darwin") == HOME_FOLDER_NOTICE
    assert home_folder_notice(str(home), {"HOME": str(home) + "/"}, "darwin") == HOME_FOLDER_NOTICE


def test_the_platform_is_read_at_call_time(home, monkeypatch):
    environ = {"HOME": str(home)}
    monkeypatch.setattr(sys, "platform", "darwin")
    assert home_folder_notice(str(home), environ) == HOME_FOLDER_NOTICE
    monkeypatch.setattr(sys, "platform", "linux")
    assert home_folder_notice(str(home), environ) is None


def test_an_unresolvable_path_is_silent_not_a_raise(home):
    # A NUL byte makes realpath raise ValueError: said as nothing.
    assert home_folder_notice("/no\0where", {"HOME": str(home)}, "darwin") is None
    assert home_folder_notice(str(home), {"HOME": "/no\0where"}, "darwin") is None


def test_a_missing_home_dir_still_compares_by_path(tmp_path):
    # Not resolvable on disk is not an error: realpath is non-strict.
    gone = str(tmp_path / "gone")
    assert home_folder_notice(gone, {"HOME": gone}, "darwin") == HOME_FOLDER_NOTICE
    assert home_folder_notice(gone + "/x", {"HOME": gone}, "darwin") is None


# ---------- through main(), dry run ------------------------------------------


_SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def dry_run_in(tmp_path, monkeypatch, cwd, *argv, front_matter=""):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path, front_matter=front_matter))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.chdir(cwd)
    return start.main(["--dry-run", *argv])


def test_main_dry_run_in_the_home_folder_warns_once(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert dry_run_in(tmp_path, monkeypatch, home, "scout") == EXIT_OK
    captured = capsys.readouterr()
    assert captured.err.splitlines().count(WORDING) == 1
    assert "home folder" not in captured.out
    assert "DRY RUN" in captured.out


def test_main_dry_run_report_is_unchanged_by_the_line(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert dry_run_in(tmp_path, monkeypatch, home, "scout") == EXIT_OK
    warned = capsys.readouterr()
    monkeypatch.setattr(start, "home_folder_notice", lambda *_a, **_k: None)
    assert dry_run_in(tmp_path, monkeypatch, home, "scout") == EXIT_OK
    silent = capsys.readouterr()
    assert WORDING in warned.err and WORDING not in silent.err
    # The minted session id differs run to run; nothing else may.
    assert _SID.sub("SID", warned.out) == _SID.sub("SID", silent.out)
    assert warned.err.replace(WORDING + "\n", "") == silent.err


def test_main_dry_run_elsewhere_is_silent(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    sub = home / "project"
    sub.mkdir()
    assert dry_run_in(tmp_path, monkeypatch, sub, "scout") == EXIT_OK
    assert "home folder" not in capsys.readouterr().err
    assert dry_run_in(tmp_path, monkeypatch, tmp_path, "scout") == EXIT_OK
    assert "home folder" not in capsys.readouterr().err


def test_main_compares_the_session_directory_not_the_callers(tmp_path, home, monkeypatch,
                                                             capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    # Standing in the home folder, launching into a project: silent.
    project = tmp_path / "project"
    project.mkdir()
    assert dry_run_in(tmp_path, monkeypatch, home, "--directory", str(project),
                      "scout") == EXIT_OK
    assert "home folder" not in capsys.readouterr().err
    # Standing elsewhere, `start: directory:` the home folder: one line.
    assert dry_run_in(tmp_path, monkeypatch, project, "scout",
                      front_matter=f"start:\n  directory: {home}\n") == EXIT_OK
    assert capsys.readouterr().err.splitlines().count(WORDING) == 1


def test_main_in_tmux_dry_run_warns_too(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("TMUX", "/tmp/fake,1,0")
    assert dry_run_in(tmp_path, monkeypatch, home, "--in-tmux", "scout") == EXIT_OK
    assert capsys.readouterr().err.splitlines().count(WORDING) == 1


def test_main_off_macos_is_silent_in_the_home_folder(tmp_path, home, monkeypatch, capsys):
    monkeypatch.setattr(start, "home_folder_notice",
                        lambda work_dir, environ: home_folder_notice(work_dir, environ, "linux"))
    assert dry_run_in(tmp_path, monkeypatch, home, "scout") == EXIT_OK
    assert "home folder" not in capsys.readouterr().err


RESUME_SID = "11111111-2222-3333-4444-555555555555"


def write_transcript(home, cwd):
    project = home / ".claude" / "projects" / "-proj"
    project.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"type": "user", "cwd": str(cwd)})
    (project / f"{RESUME_SID}.jsonl").write_text(line + "\n")


def test_main_a_resume_compares_the_recorded_cwd(tmp_path, home, monkeypatch, capsys):
    """A resume runs in the session's recorded cwd (plan.work_dir), not the
    caller's: born in the home folder, resumed from a project, it warns;
    born in a project, resumed from the home folder, it is silent."""
    monkeypatch.setattr(sys, "platform", "darwin")
    project = tmp_path / "project"
    project.mkdir()
    write_transcript(home, home)
    assert dry_run_in(tmp_path, monkeypatch, project, "scout", "--resume", RESUME_SID) == EXIT_OK
    assert capsys.readouterr().err.splitlines().count(WORDING) == 1
    write_transcript(home, project)
    assert dry_run_in(tmp_path, monkeypatch, home, "scout", "--resume", RESUME_SID) == EXIT_OK
    err = capsys.readouterr().err
    assert "the session's recorded cwd" in err and WORDING not in err


# ---------- the offer: the pure decision -------------------------------------
#
# In the home folder, at a terminal, the launcher offers the work folder:
# MOVE when it is a folder, MAKE when nothing is there, REFUSE when
# something else is. `home_folder_offer` decides from the disk; it asks
# and writes nothing.


def test_the_work_folder_is_one_constant():
    assert WORK_FOLDER == "work"


def test_no_work_folder_is_an_offer_to_make_it(home):
    offer = home_folder_offer(str(home), {"HOME": str(home)}, "darwin")
    assert offer == Offer(str(home / "work"), "~/work", MAKE)
    assert not (home / "work").exists()        # deciding writes nothing


def test_an_existing_work_folder_is_an_offer_to_move(home):
    (home / "work").mkdir()
    assert home_folder_offer(str(home), {"HOME": str(home)}, "darwin").kind == MOVE


def test_a_symlink_to_a_folder_counts_as_a_folder(home, tmp_path):
    (tmp_path / "real").mkdir()
    (home / "work").symlink_to(tmp_path / "real")
    offer = home_folder_offer(str(home), {"HOME": str(home)}, "darwin")
    assert offer.kind == MOVE
    assert offer.path == str(home / "work")    # named as the user names it


def test_a_file_named_work_is_refused(home):
    (home / "work").write_text("notes\n")
    assert home_folder_offer(str(home), {"HOME": str(home)}, "darwin").kind == REFUSE


def test_a_broken_symlink_named_work_is_refused(home, tmp_path):
    (home / "work").symlink_to(tmp_path / "nowhere")
    assert home_folder_offer(str(home), {"HOME": str(home)}, "darwin").kind == REFUSE


def test_a_trailing_slash_on_home_names_the_same_folder(home):
    offer = home_folder_offer(str(home), {"HOME": str(home) + "/"}, "darwin")
    assert offer.path == str(home / "work")


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_no_offer_off_macos(home, platform):
    assert home_folder_offer(str(home), {"HOME": str(home)}, platform) is None


@pytest.mark.parametrize("environ", [{}, {"HOME": ""}])
def test_no_offer_with_home_unset(home, environ):
    assert home_folder_offer(str(home), environ, "darwin") is None


def test_no_offer_in_a_subfolder(home):
    (home / "project").mkdir()
    assert home_folder_offer(str(home / "project"), {"HOME": str(home)}, "darwin") is None


# ---------- the offer: the question ------------------------------------------


class Terminal(io.StringIO):
    """A stream that says it is (or is not) a terminal."""

    def __init__(self, text: str = "", tty: bool = True) -> None:
        super().__init__(text)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class Interrupting(Terminal):
    """A terminal whose reader presses Ctrl-C."""

    def readline(self, *_a):
        raise KeyboardInterrupt


PREFIX = "session start: "
ASK_MOVE = PREFIX + "start in ~/work instead? [Y/n] "
ASK_MAKE = PREFIX + "make ~/work and start there? [Y/n] "


def fake_launch(work_dir, home, *, source="cwd", resume=None, dry_run=False, files=()):
    """The parts of a Launch the offer reads, and `_replace`."""

    class FakeLaunch(SimpleNamespace):
        def _replace(self, **changes):
            return FakeLaunch(**{**vars(self), **changes})

    return FakeLaunch(
        work_dir=str(work_dir), work_dir_source=source, resume=resume, dry_run=dry_run,
        environ={"HOME": str(home)},
        agent_file=SimpleNamespace(environment=Environment({}, list(files), {}, ("TOKEN",))))


def ask(launch, answer="", *, stdin_tty=True, stderr_tty=True, platform="darwin"):
    stdin = answer if isinstance(answer, Terminal) else Terminal(answer, stdin_tty)
    stderr = Terminal(tty=stderr_tty)
    result, noticed = offer_work_folder(launch, stdin=stdin, stderr=stderr, platform=platform)
    return result, noticed, stderr.getvalue()


@pytest.mark.parametrize("answer", ["\n", "y\n", "Y\n", "yes\n", "YES\n", "  Yes \n"])
def test_yes_makes_the_folder_and_starts_there(home, answer):
    launch, noticed, err = ask(fake_launch(home, home), answer)
    assert (home / "work").is_dir()
    assert launch.work_dir == str(home / "work")
    assert launch.work_dir_source == OFFER_SOURCE
    assert noticed
    assert err == f"{WORDING}\n{ASK_MAKE}{PREFIX}made ~/work; starting there\n"


def test_the_made_folder_has_the_umask_mode(home):
    old = os.umask(0o027)
    try:
        ask(fake_launch(home, home), "\n")
    finally:
        os.umask(old)
    assert (home / "work").stat().st_mode & 0o777 == 0o750


def test_yes_with_the_folder_there_moves_and_makes_nothing(home, monkeypatch):
    (home / "work").mkdir()
    (home / "work" / "keep").write_text("x")
    monkeypatch.setattr(os, "mkdir", lambda *_a, **_k: pytest.fail("mkdir on a move"))
    launch, noticed, err = ask(fake_launch(home, home), "y\n")
    assert launch.work_dir == str(home / "work") and noticed
    assert err == f"{WORDING}\n{ASK_MOVE}{PREFIX}starting in ~/work\n"
    assert (home / "work" / "keep").read_text() == "x"


@pytest.mark.parametrize("answer", ["n\n", "N\n", "no\n", "No\n", "yep\n", "sure\n", "q\n"])
def test_no_or_anything_else_stays_home_and_says_nothing_more(home, answer):
    launch, noticed, err = ask(fake_launch(home, home), answer)
    assert launch.work_dir == str(home)
    assert launch.work_dir_source == "cwd"
    assert noticed
    assert err == f"{WORDING}\n{ASK_MAKE}"
    assert not (home / "work").exists()


def test_end_of_input_is_a_no(home):
    launch, noticed, err = ask(fake_launch(home, home), "")
    assert launch.work_dir == str(home) and noticed
    assert err == f"{WORDING}\n{ASK_MAKE}\n"
    assert not (home / "work").exists()


@pytest.mark.parametrize("stdin_tty, stderr_tty", [(False, True), (True, False), (False, False)])
def test_no_terminal_no_question(home, stdin_tty, stderr_tty):
    launch, noticed, err = ask(fake_launch(home, home), "y\n",
                               stdin_tty=stdin_tty, stderr_tty=stderr_tty)
    assert (launch.work_dir, noticed, err) == (str(home), False, "")
    assert not (home / "work").exists()


def test_a_stream_without_isatty_is_no_terminal(home):
    launch, noticed = offer_work_folder(
        fake_launch(home, home), stdin=object(), stderr=Terminal(), platform="darwin")
    assert (launch.work_dir, noticed) == (str(home), False)


@pytest.mark.parametrize("change", [
    {"source": "--directory"},
    {"source": "agent file: start: directory:"},
    {"resume": object()},
    {"dry_run": True},
])
def test_exemptions_ask_nothing(home, change):
    launch, noticed, err = ask(fake_launch(home, home, **change), "y\n")
    assert (launch.work_dir, noticed, err) == (str(home), False, "")


def test_a_subfolder_asks_nothing(home):
    (home / "project").mkdir()
    launch, noticed, err = ask(fake_launch(home / "project", home), "y\n")
    assert (launch.work_dir, noticed, err) == (str(home / "project"), False, "")


def test_off_macos_asks_nothing(home):
    launch, noticed, err = ask(fake_launch(home, home), "y\n", platform="linux")
    assert (launch.work_dir, noticed, err) == (str(home), False, "")


def test_home_unset_asks_nothing(home):
    launch = fake_launch(home, home)
    launch.environ = {}
    result, noticed, err = ask(launch, "y\n")
    assert (result.work_dir, noticed, err) == (str(home), False, "")


@pytest.mark.parametrize("make", [
    lambda p, t: p.write_text("notes\n"),
    lambda p, t: p.symlink_to(t / "nowhere"),
])
def test_a_work_that_is_not_a_folder_is_refused_without_a_question(home, tmp_path, make):
    make(home / "work", tmp_path)
    before = os.lstat(home / "work")
    launch, noticed, err = ask(fake_launch(home, home), "y\n")
    assert launch.work_dir == str(home)
    assert noticed
    assert err == (f"{WORDING}\n{PREFIX}~/work is not a folder; "
                   "starting in your home folder\n")
    assert os.lstat(home / "work") == before        # touched nothing


def test_a_failed_mkdir_says_why_and_stays_home(home, monkeypatch):
    def refuse(path, *_a, **_k):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(os, "mkdir", refuse)
    launch, noticed, err = ask(fake_launch(home, home), "\n")
    assert launch.work_dir == str(home) and noticed
    assert launch.work_dir_source == "cwd"
    assert err.endswith(f"{PREFIX}could not make ~/work (Permission denied); "
                        "starting in your home folder\n")


def test_ctrl_c_at_the_question_propagates_and_makes_nothing(home):
    with pytest.raises(KeyboardInterrupt):
        ask(fake_launch(home, home), Interrupting())
    assert not (home / "work").exists()


def test_a_relative_env_file_is_checked_again_in_the_new_folder(home, capsys):
    (home / "work").mkdir()
    (home / "work" / ".env").write_text("trap 'rm -rf x' EXIT\n")
    (home / "abs.env").write_text("trap 'echo' EXIT\n")
    launch = fake_launch(home, home, files=["./.env", str(home / "abs.env")])
    ask(launch, "y\n")
    notes = capsys.readouterr().err.splitlines()
    # The relative file is new in ~/work: one note. The absolute one was
    # already checked against the home folder: not said again.
    assert [line for line in notes if "env file ./.env" in line] != []
    assert not any("abs.env" in line for line in notes)


def test_a_no_checks_no_env_file_again(home, capsys):
    (home / ".env").write_text("trap 'rm' EXIT\n")
    ask(fake_launch(home, home, files=["./.env"]), "n\n")
    assert capsys.readouterr().err == ""


# ---------- the offer through main() -----------------------------------------
#
# A live main() execs, so `execute` is replaced by a spy that keeps the plan:
# what the session would have been launched with, nothing launched.


@pytest.fixture
def at_a_terminal(tmp_path, home, monkeypatch):
    """main() in the home folder, on macOS, at a fake terminal; returns a
    runner taking the answer and argv, giving (code, plan or None, stderr)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.chdir(home)
    plans = []
    monkeypatch.setattr(start, "execute", plans.append)

    def run(stdin, *argv, cwd=None, err_tty=True):
        if cwd is not None:
            monkeypatch.chdir(cwd)
        err = Terminal(tty=err_tty)
        monkeypatch.setattr(sys, "stdin", stdin if isinstance(stdin, Terminal)
                            else Terminal(stdin))
        monkeypatch.setattr(sys, "stdout", Terminal())
        monkeypatch.setattr(sys, "stderr", err)
        plans.clear()
        code = start.main(list(argv) or ["scout"])
        return code, (plans[0] if plans else None), err.getvalue()

    return run


def test_main_yes_reaches_the_minted_record_as_the_work_folder(at_a_terminal, home):
    code, plan, err = at_a_terminal("\n")
    assert code == EXIT_OK
    assert plan.work_dir == str(home / "work")
    assert plan.work_dir_source == OFFER_SOURCE
    assert plan.record.work_dir == str(home / "work")
    assert f"cd {home / 'work'}" in plan.script
    assert err.count(WORDING) == 1
    assert f"{PREFIX}made ~/work; starting there" in err


def test_main_no_starts_home_and_warns_exactly_once(at_a_terminal, home):
    code, plan, err = at_a_terminal("n\n")
    assert code == EXIT_OK
    assert plan.work_dir == str(home)
    assert err.count(WORDING) == 1
    assert err.count(ASK_MAKE) == 1
    assert not (home / "work").exists()


def test_main_refusal_warns_once(at_a_terminal, home):
    (home / "work").write_text("x")
    code, plan, err = at_a_terminal("\n")
    assert (code, plan.work_dir) == (EXIT_OK, str(home))
    assert err.count(WORDING) == 1
    assert "is not a folder" in err


def test_main_ctrl_c_exits_130_with_nothing_minted(at_a_terminal, home, tmp_path,
                                                   monkeypatch):
    minted = []
    monkeypatch.setattr(start, "mint", lambda launch: minted.append(launch))
    code, plan, err = at_a_terminal(Interrupting())
    assert code == EXIT_INTERRUPTED == 130
    assert (plan, minted) == (None, [])
    assert not (tmp_path / "sessions").exists()
    assert "Traceback" not in err
    assert not (home / "work").exists()


def test_main_no_terminal_for_the_question_is_the_tip_behaviour(at_a_terminal, home):
    """stdin and stdout a terminal (the launch can run) but stderr not:
    there is nowhere to ask, so it says the tip and starts at home."""
    code, plan, err = at_a_terminal("y\n", err_tty=False)
    assert (code, plan.work_dir) == (EXIT_OK, str(home))
    assert err.count(WORDING) == 1
    assert "[Y/n]" not in err


def test_main_no_terminal_at_all_is_refused_before_the_question(at_a_terminal, home):
    """stdin not a terminal: the in-place launch could not run, so it is
    refused before anything is asked, minted or made."""
    code, plan, err = at_a_terminal(Terminal("y\n", tty=False))
    assert (code, plan) == (EXIT_USAGE, None)
    assert "no terminal here" in err
    assert "[Y/n]" not in err and WORDING not in err
    assert not (home / "work").exists()


def test_main_dry_run_never_asks(at_a_terminal, home, monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    code, plan, err = at_a_terminal("y\n", "--dry-run", "scout")
    assert code == EXIT_OK and plan is None
    assert err.count(WORDING) == 1 and "[Y/n]" not in err
    assert not (home / "work").exists()


def test_main_directory_home_skips_the_question(at_a_terminal, home):
    code, plan, err = at_a_terminal("y\n", "--directory", str(home), "scout")
    assert (code, plan.work_dir) == (EXIT_OK, str(home))
    assert "[Y/n]" not in err and err.count(WORDING) == 1


def test_main_offer_runs_after_the_front_door(at_a_terminal, home, monkeypatch):
    def held(*_a, **_k):
        raise LaunchError("scout is held", code=EXIT_HELD)

    monkeypatch.setattr(start, "enforce", held)
    code, plan, err = at_a_terminal("y\n")
    assert (code, plan) == (EXIT_HELD, None)
    assert "[Y/n]" not in err and not (home / "work").exists()


def test_homefolder_module_exports_the_offer():
    assert {"home_folder_offer", "offer_work_folder", "WORK_FOLDER"} <= set(homefolder.__all__)


def test_main_start_directory_naming_home_skips_the_question(at_a_terminal, home, tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(
        tmp_path, front_matter=f"start:\n  directory: {home}\n"))
    code, plan, err = at_a_terminal("y\n")
    assert (code, plan.work_dir) == (EXIT_OK, str(home))
    assert "[Y/n]" not in err and err.count(WORDING) == 1
