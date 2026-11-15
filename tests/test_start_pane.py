"""The pane's line (ST-285 do-003): `pane_argv`, the first-message file and
the hand-off guard. Pure: no tmux, no launch. The real-tmux half is in
test_start_placement.py, tests (i)-(vii)."""

from __future__ import annotations

import itertools
import os
import shlex
import stat

import pytest
from start_support import make_agents_root

from sessiontools import start
from sessiontools.start import pane
from sessiontools.start.args import build_parser, launcher_option_strings, parse_args
from sessiontools.start.errors import EXIT_USAGE, LaunchError
from sessiontools.start.record import (
    LAUNCH_COMMAND_ENV,
    LAUNCH_DIR_ENV,
    LAUNCH_FROM_ENV_FLAG,
)
from sessiontools.start.vendor import split_rest

F = "/state/launches/t0k/first-message.md"
MOVED = ["--first-message-file", F, "--first-message-file-over", "0"]


# ---------- pane_argv: a table ------------------------------------------------------------

@pytest.mark.parametrize("entered, expected", [
    (["--in-tmux", "reviewer"], ["reviewer"]),
    (["--tmux-target", "eng", "reviewer"], ["reviewer"]),
    (["--tmux-target=eng:w", "reviewer"], ["reviewer"]),
    # the target's value IS the agent's name: the value goes, the agent stays
    (["--tmux-target", "reviewer", "reviewer"], ["reviewer"]),
    (["--in-tmux", "--switch", "reviewer"], ["reviewer"]),
    (["--in-tmux", "--no-switch", "--keyed", "reviewer"], ["--keyed", "reviewer"]),
    (["--directory", "/x", "--in-tmux", "--seat", "s+r", "reviewer", "--model", "opus"],
     ["--directory", "/x", "--seat", "s+r", "reviewer", "--model", "opus"]),
    # vendor passthrough is never touched, a lookalike flag included
    (["--in-tmux", "reviewer", "--model", "opus", "--in-tmux"],
     ["reviewer", "--model", "opus", "--in-tmux"]),
    # words: moved to the file, flags first, vendor flags kept after the agent
    (["--in-tmux", "reviewer", "review", "the", "auth"], [*MOVED, "reviewer"]),
    (["--in-tmux", "reviewer", "hi", "--model", "opus"], [*MOVED, "reviewer", "--model", "opus"]),
    (["--in-tmux", "reviewer", "hi", "--", "x;y"], [*MOVED, "reviewer", "--", "x;y"]),
    # a typed threshold is replaced, not doubled
    (["--first-message-file-over", "999", "--in-tmux", "reviewer", "hi"], [*MOVED, "reviewer"]),
    (["--first-message-file-over=9", "--in-tmux", "reviewer", "hi"], [*MOVED, "reviewer"]),
    # ... and kept when there are no words to move
    (["--first-message-file-over", "9", "--in-tmux", "reviewer"],
     ["--first-message-file-over", "9", "reviewer"]),
    (["--in-tmux", "-", "hello", "--model", "opus"], [*MOVED, "-", "--model", "opus"]),
    (["--in-tmux"], []),
])
def test_pane_argv(entered, expected):
    assert pane.pane_argv(entered, F) == expected


def test_words_need_a_file():
    with pytest.raises(ValueError):
        pane.pane_argv(["--in-tmux", "reviewer", "hi"], None)


def test_the_pane_line_is_session_start_and_the_argv():
    assert pane.pane_line(["reviewer", "--model", "o p"]) == "session start reviewer --model 'o p'"


# ---------- pane_argv: the property ---------------------------------------------------------

PREFIXES = [[], ["--keyed"], ["--directory", "/d"], ["--seat=a+b"], ["--idle", "--converse"],
            ["--first-message-file-over", "7"]]
PLACEMENTS = [["--in-tmux"], ["--tmux-target", "eng"], ["--tmux-target=eng:w"],
              ["--in-tmux", "--switch"], ["--tmux-target", "reviewer", "--no-switch"]]
AGENTS = [["reviewer"], ["-"]]
TAILS = [[], ["hi"], ["review", "the", "auth"], ["hi", "--model", "opus"], ["--model", "opus"],
         ["--resume", "x"]]

PLACEMENT_FIELDS = {"in_tmux": False, "tmux_target": None, "switch": False, "no_switch": False}


def parsed(argv):
    fields = vars(parse_args(argv))
    fields.pop("entered_argv")
    return fields


@pytest.mark.parametrize("prefix, placement, first, agent, tail",
                         list(itertools.product(PREFIXES, PLACEMENTS, [True, False], AGENTS,
                                                TAILS)))
def test_parse_of_the_pane_argv_is_the_entered_parse_less_placement(prefix, placement, first,
                                                                     agent, tail):
    """parse_args(pane_argv(x)) == parse_args(x) with the placement fields
    cleared, the rest's leading bare words removed and the two message
    flags set (when there were words); every other field equal."""
    flags = [*placement, *prefix] if first else [*prefix, *placement]
    entered = [*flags, *agent, *tail]
    want = parsed(entered)
    want.update(PLACEMENT_FIELDS)
    words, vendor = split_rest(want["rest"])
    if words:
        want.update(rest=vendor, first_message_file=F, first_message_file_over=0)
    assert parsed(pane.pane_argv(entered, F)) == want


# ---------- the hidden flag -----------------------------------------------------------------

def test_the_flag_is_hidden_from_help_but_owned():
    assert "--first-message-file " not in build_parser().format_help()
    assert "--first-message-file-over" in build_parser().format_help()
    assert "--first-message-file" in launcher_option_strings()


def test_the_flag_after_the_agent_is_the_position_trap(capsys):
    with pytest.raises(SystemExit) as stop:
        parse_args(["reviewer", "--first-message-file", "/x"])
    assert stop.value.code == EXIT_USAGE
    assert "'--first-message-file' after the agent name" in capsys.readouterr().err


# ---------- the message file ----------------------------------------------------------------

def env_of(tmp_path):
    return {"SESSION_STATE_DIR": str(tmp_path / "state")}


def test_the_file_is_0600_in_a_0700_dir_and_never_overwritten(tmp_path):
    path = pane.write_message_file("review the auth", "t0k", env_of(tmp_path))
    assert path == pane.message_file_path("t0k", env_of(tmp_path))
    assert path == str(tmp_path / "state" / "launches" / "t0k" / "first-message.md")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(tmp_path / "state" / "launches").st_mode) == 0o700
    assert pane.read_message_file(path) == "review the auth"
    with pytest.raises(FileExistsError):
        pane.write_message_file("other", "t0k", env_of(tmp_path))


def test_discard_takes_the_file_and_its_token_dir(tmp_path):
    path = pane.write_message_file("x", "t0k", env_of(tmp_path))
    pane.discard_message_file(path)
    assert not os.path.exists(os.path.dirname(path))
    pane.discard_message_file(path)          # twice is harmless
    pane.discard_message_file(None)


def refusal(path) -> str:
    with pytest.raises(LaunchError) as refused:
        pane.read_message_file(str(path))
    assert refused.value.code == EXIT_USAGE
    return str(refused.value)


def test_a_missing_file_is_refused(tmp_path):
    assert "no such file" in refusal(tmp_path / "gone.md")


def test_a_directory_is_not_a_regular_file(tmp_path):
    assert "not a regular file" in refusal(tmp_path)


def test_a_symlink_is_not_a_regular_file(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("x")
    real.chmod(0o600)
    link = tmp_path / "link.md"
    link.symlink_to(real)
    assert "not a regular file" in refusal(link)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602])
def test_a_file_open_to_group_or_other_is_refused(tmp_path, mode):
    path = tmp_path / "m.md"
    path.write_text("x")
    path.chmod(mode)
    assert "must be 0600" in refusal(path)


def test_a_file_that_is_not_ours_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "m.md"
    path.write_text("x")
    path.chmod(0o600)
    monkeypatch.setattr(pane.os, "getuid", lambda: os.stat(path).st_uid + 1)
    assert "not ours" in refusal(path)


def test_the_file_with_bare_words_is_refused_at_main(tmp_path, monkeypatch, capsys):
    path = pane.write_message_file("x", "t0k", env_of(tmp_path))
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert start.main(["--dry-run", "--first-message-file", path, "scout", "hello"]) == \
        EXIT_USAGE
    assert "bare first-message words together" in capsys.readouterr().err


def test_a_missing_file_is_refused_at_main(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    monkeypatch.chdir(tmp_path)
    gone = str(tmp_path / "gone.md")
    assert start.main(["--dry-run", "--first-message-file", gone, "scout"]) == EXIT_USAGE
    assert "no such file" in capsys.readouterr().err


def test_the_file_is_the_message_as_if_typed(tmp_path, monkeypatch, capsys):
    path = pane.write_message_file("review the auth", "t0k", env_of(tmp_path))
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert start.main(["--dry-run", "--first-message-file", path, "scout"]) == 0
    assert "review the auth" in capsys.readouterr().out


# ---------- the hand-off guard --------------------------------------------------------------

def handed(entered, entered_dir="/typed/here") -> dict:
    return {pane.ENTERED_ENV: pane.pane_line(entered), pane.ENTERED_DIR_ENV: entered_dir,
            "OTHER": "kept"}


def test_a_matching_hand_off_is_taken_and_popped():
    environ = handed(["--in-tmux", "reviewer", "--model", "o"])
    taken = pane.take_handoff(environ, ["reviewer", "--model", "o"], None)
    assert taken == (["--in-tmux", "reviewer", "--model", "o"], "/typed/here")
    assert environ == {"OTHER": "kept"}


def test_a_matching_hand_off_with_words_checks_the_file_text(tmp_path):
    path = pane.write_message_file("review the auth", "t0k", env_of(tmp_path))
    entered = ["--tmux-target", "eng", "reviewer", "review", "the", "auth"]
    own = pane.pane_argv(entered, path)
    assert pane.take_handoff(handed(entered), own, path) == (entered, "/typed/here")
    other = pane.write_message_file("something else", "t1k", env_of(tmp_path))
    assert pane.take_handoff(handed(entered), pane.pane_argv(entered, other), other) is None


@pytest.mark.parametrize("environ, own", [
    (handed(["--in-tmux", "reviewer"]), ["builder"]),                       # another line
    (handed(["--in-tmux", "reviewer"]), ["--keyed", "reviewer"]),            # a changed flag
    (handed(["--in-tmux", "reviewer"], "relative"), ["reviewer"]),           # a relative dir
    (handed(["--in-tmux", "reviewer"], ""), ["reviewer"]),
    ({pane.ENTERED_ENV: "other start reviewer", pane.ENTERED_DIR_ENV: "/d"}, ["reviewer"]),
    ({pane.ENTERED_ENV: "session start 'unclosed", pane.ENTERED_DIR_ENV: "/d"}, ["reviewer"]),
    ({pane.ENTERED_DIR_ENV: "/d"}, ["reviewer"]),
    ({}, ["reviewer"]),
    # words entered, but this pane names no file
    (handed(["--in-tmux", "reviewer", "hi"]), ["reviewer", "hi"]),
])
def test_anything_else_is_refused_and_still_popped(environ, own):
    assert pane.take_handoff(environ, own, None) is None
    assert pane.ENTERED_ENV not in environ and pane.ENTERED_DIR_ENV not in environ


def test_main_pops_the_hand_off_on_every_launch(tmp_path, monkeypatch):
    """No vendor inherits it, whatever the launch: popped at entry."""
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(pane.ENTERED_ENV, "session start --in-tmux builder")
    monkeypatch.setenv(pane.ENTERED_DIR_ENV, "/d")
    assert start.main(["--dry-run", "scout"]) == 0
    assert pane.ENTERED_ENV not in os.environ and pane.ENTERED_DIR_ENV not in os.environ


def test_a_valid_hand_off_is_what_the_record_keeps(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(pane.ENTERED_ENV, "session start --in-tmux --dry-run scout")
    monkeypatch.setenv(pane.ENTERED_DIR_ENV, "/where/typed")
    assert start.main(["--dry-run", "scout"]) == 0
    out = capsys.readouterr().out
    line = shlex.quote("session start --in-tmux --dry-run scout")
    assert f"    {LAUNCH_COMMAND_ENV}={line}\n" in out
    assert f"    {LAUNCH_DIR_ENV}=/where/typed\n" in out


def launch_script_of(report: str) -> str:
    """The `launch script:` section of a dry-run report: the `bash -c` text."""
    return report.split("\nlaunch script:\n", 1)[1]


def test_a_handed_line_with_words_never_reaches_the_record_calls_argv(tmp_path, monkeypatch,
                                                                    capsys):
    """F2 (ruling 7): a pane whose hand-off carries first-message words
    renders a `bash -c` script with none of them, the record call's argv
    included; the record still gets the line exactly as entered, by the
    script's environment."""
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    monkeypatch.setenv("SESSION_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    entered = ["--in-tmux", "--dry-run", "scout", "zebrafish", "quokka"]
    path = pane.write_message_file("zebrafish quokka", "t0k", env_of(tmp_path))
    own = pane.pane_argv(entered, path)
    monkeypatch.setenv(pane.ENTERED_ENV, pane.pane_line(entered))
    monkeypatch.setenv(pane.ENTERED_DIR_ENV, "/where/typed")
    assert start.main(own) == 0
    out = capsys.readouterr().out
    script = launch_script_of(out)
    record_call = next(line for line in script.splitlines() if "record import main" in line)
    for word in ("zebrafish", "quokka", "/where/typed"):
        assert word not in script, word
    assert f" {LAUNCH_FROM_ENV_FLAG} " in record_call
    assert f"    {LAUNCH_COMMAND_ENV}={shlex.quote(pane.pane_line(entered))}\n" in out
