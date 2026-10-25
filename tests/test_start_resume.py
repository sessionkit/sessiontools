"""Resume (R8 slice 3), the pure pieces: detection, CA-62 cwd recovery, the
dated restart message, the keyed-resume warning. Ported from the old suite
(session-20300609 `test_session_start.py` 2384-2395, 4052-4121, 4222-4236).

CA-62 tests always pass their own `home`: nothing here reads the
developer's ~/.claude.
"""

from __future__ import annotations

import datetime
import json
import os

import pytest
from conftest import ANCHOR

from sessiontools.start import resume
from sessiontools.start.context import BARE_CONVERSE_SIGNAL, CONVERSE_SIGNAL

RESUME_SID = "11111111-2222-3333-4444-555555555555"
FROZEN = ANCHOR.astimezone(datetime.UTC)

RESUME_CASES = [
    (["--resume", "abc"], "abc"),
    (["--resume=abc"], "abc"),
    (["-r", "abc"], "abc"),
    (["--continue"], ""),             # claude's most-recent semantics
    (["--resume"], ""),               # bare: claude's picker
    (["--resume", "--effort"], ""),   # the next token is a flag, not an id
    (["--effort", "max", "-c"], ""),
    (["-r", "first", "--resume", "second"], "first"),
    (["-c", "--resume=later"], "later"),
]


# ---------- detect_resume ------------------------------------------------------

@pytest.mark.parametrize("cli, expected", RESUME_CASES)
def test_detect_resume_reads_claudes_own_spellings(cli, expected):
    request = resume.detect_resume("claude", cli)
    assert request is not None
    assert request.session_id == expected


def test_detect_resume_is_claude_only_and_needs_a_resume_token():
    assert resume.detect_resume("claude", ["--effort", "max"]) is None
    # Another vendor's --resume means whatever that vendor says (CA-60).
    assert resume.detect_resume("codex", ["--resume", "abc"]) is None
    assert resume.detect_resume("pi", ["--continue"]) is None


# ---------- recover_resume_cwd (CA-62) ------------------------------------------

def write_transcript(home, sid, cwd, project="proj", mtime=None):
    """A minimal claude transcript holding the recorded cwd."""
    directory = home / ".claude" / "projects" / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sid}.jsonl"
    path.write_text(f'{{"type":"x","cwd":{json.dumps(str(cwd))}}}\n')
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_recover_resume_cwd_reads_the_recorded_directory(tmp_path):
    write_transcript(tmp_path, RESUME_SID, "/somewhere/original")
    assert resume.recover_resume_cwd(RESUME_SID, home=str(tmp_path)) == "/somewhere/original"


def test_recover_resume_cwd_newest_transcript_wins(tmp_path):
    """The old launcher's `ls -t | head -1`: several projects can hold one
    id's file; the most recently touched is the record."""
    write_transcript(tmp_path, RESUME_SID, "/stale", project="old", mtime=1000)
    write_transcript(tmp_path, RESUME_SID, "/fresh", project="new", mtime=2000)
    assert resume.recover_resume_cwd(RESUME_SID, home=str(tmp_path)) == "/fresh"


def test_recover_resume_cwd_takes_the_first_cwd_in_the_file(tmp_path):
    path = write_transcript(tmp_path, RESUME_SID, "/first")
    with path.open("a") as handle:
        handle.write('{"type":"y","cwd":"/later"}\n')
    assert resume.recover_resume_cwd(RESUME_SID, home=str(tmp_path)) == "/first"


def test_recover_resume_cwd_decodes_json_escapes(tmp_path):
    write_transcript(tmp_path, RESUME_SID, "/tmp/café")      # written as é
    assert resume.recover_resume_cwd(RESUME_SID, home=str(tmp_path)) == "/tmp/café"


def test_recover_resume_cwd_absent_or_bare_is_none(tmp_path):
    assert resume.recover_resume_cwd("no-such-sid", home=str(tmp_path)) is None
    assert resume.recover_resume_cwd("", home=str(tmp_path)) is None


# ---------- resume_message: the date injection ------------------------------------

def test_resume_message_is_the_dated_restart_line():
    text = resume.resume_message(now=FROZEN)
    assert text == ("Session restarted. Current date and time: "
                    + FROZEN.strftime(resume.RESUME_STAMP_FORMAT))
    assert resume.resume_message(["carry", "on"], now=FROZEN) == text + "\n\ncarry on"


def test_resume_message_carries_the_speak_signal_when_converse_is_on():
    """Date line, THEN the voice signal, THEN the user's words last: the
    order the fresh path uses."""
    speaking = resume.resume_message(["carry", "on"], now=FROZEN, converse=True,
                                     agent_file=True)
    assert speaking == ("Session restarted. Current date and time: "
                        + FROZEN.strftime(resume.RESUME_STAMP_FORMAT)
                        + "\n\n" + CONVERSE_SIGNAL + "\n\ncarry on")
    for agent_file in (True, False):
        quiet = resume.resume_message(["carry", "on"], now=FROZEN, agent_file=agent_file)
        assert CONVERSE_SIGNAL not in quiet and BARE_CONVERSE_SIGNAL not in quiet


def test_a_bare_resume_carries_the_neutral_voice_line():
    """No agent file: the neutral line, in the same place. It is the
    default, as on the fresh path."""
    speaking = resume.resume_message(["carry", "on"], now=FROZEN, converse=True)
    assert speaking == ("Session restarted. Current date and time: "
                        + FROZEN.strftime(resume.RESUME_STAMP_FORMAT)
                        + "\n\n" + BARE_CONVERSE_SIGNAL + "\n\ncarry on")
    assert CONVERSE_SIGNAL not in speaking


# ---------- keyed_resume_warning -----------------------------------------------

def test_keyed_resume_warning_fires_on_the_pub_artifact(tmp_path):
    keyed_dir = tmp_path / "was-keyed"
    keyed_dir.mkdir()
    (keyed_dir / resume.KEYED_PUB_FILE).write_text("ssh-ed25519 AAAA stub\n")
    warning = resume.keyed_resume_warning(str(keyed_dir))
    assert "UNKEYED" in warning


def test_unkeyed_sessions_resume_without_a_warning(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert resume.keyed_resume_warning(str(plain)) is None
