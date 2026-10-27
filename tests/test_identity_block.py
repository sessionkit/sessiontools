"""`session identity [SID]`: the reader of the identity block `session
start` writes, and the contract sessionmail's `memory wake` calls.

    exit 0   the block, byte for byte, on stdout; stderr empty
    exit 3   no block: one stderr line, stdout empty
    other    a fault: one stderr line, stdout empty

Every test builds its sessions root under tmp_path and passes it through
SESSIONS_DIR, in an environment built from nothing: a seat running the
suite carries its own CLAUDE_SESSION_ID, which must never leak in.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools import cli, identity_block, settings
from sessiontools.start import boot_identity

SID = "3f9c2a1e-0b0a-4908-8706-050403020100"
OTHER = "3f9c2a1e-ffff-4908-8706-050403020100"
UNRELATED = "aaaabbbb-0b0a-4908-8706-050403020100"

BLOCK = boot_identity.render(boot_identity.Facts(
    session_id=SID, seat="sessiontools+st-272.foreman",
    nickname="sessiontools+st-272.foreman.3f9c2a1e", pane="%271",
    holder="yes", mates="none", keyed=False))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "sessions"
    r.mkdir()
    return r


def env_for(root: Path, **extra: str) -> dict[str, str]:
    return {settings.SESSIONS_DIR_ENV: str(root), "HOME": str(root.parent), **extra}


def write(root: Path, sid: str, data: bytes | str = BLOCK) -> Path:
    d = root / sid
    d.mkdir(parents=True, exist_ok=True)
    path = d / identity_block.IDENTITY_FILE
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def run(argv: list[str], env: dict[str, str]) -> tuple[int, bytes, str]:
    out, err = io.BytesIO(), io.StringIO()
    code = identity_block.main(argv, env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def assert_one_line(err: str) -> None:
    assert err.endswith("\n") and err.count("\n") == 1, err
    assert err.startswith("session identity: "), err


# 1. exit 0: the block, byte for byte -----------------------------------------

def test_the_block_is_printed_byte_for_byte(root):
    write(root, SID)
    assert run([SID], env_for(root)) == (0, BLOCK.encode(), "")


@pytest.mark.parametrize("data", [b"# Who you are (at launch)\n\n- caf\xc3\xa9 \xe2\x9c\x93\n",
                                  b"no trailing newline", b"\r\nodd\r\n\n\n"])
def test_any_bytes_come_back_unchanged(root, data):
    write(root, SID, data)
    assert run([SID], env_for(root)) == (0, data, "")


def test_the_default_sid_is_claude_session_id(root):
    write(root, SID)
    assert run([], env_for(root, CLAUDE_SESSION_ID=SID)) == (0, BLOCK.encode(), "")


def test_an_explicit_sid_wins_over_the_env(root):
    write(root, SID, "mine\n")
    write(root, UNRELATED, "theirs\n")
    assert run([UNRELATED], env_for(root, CLAUDE_SESSION_ID=SID)) == (0, b"theirs\n", "")


def test_a_unique_short_id_resolves(root):
    write(root, SID)
    assert run([SID[:13]], env_for(root)) == (0, BLOCK.encode(), "")


def test_the_writer_and_reader_agree_on_the_path(root):
    """The writer's file is the reader's: one name, one resolver. The
    launcher makes the session dir before the writer runs."""
    (root / SID).mkdir()
    boot_identity.write_block(settings.session_dir(SID, env_for(root)), BLOCK)
    assert boot_identity.IDENTITY_FILE == identity_block.IDENTITY_FILE
    assert run([SID], env_for(root)) == (0, BLOCK.encode(), "")


# 2. exit 3: no block ------------------------------------------------------------

def test_a_session_dir_without_a_block_is_exit_3(root):
    (root / SID).mkdir()
    code, out, err = run([SID], env_for(root))
    assert (code, out) == (3, b"")
    assert_one_line(err)
    assert SID in err and "no identity block" in err


def test_a_session_with_no_dir_at_all_is_exit_3(root):
    code, out, err = run([SID], env_for(root))
    assert (code, out) == (3, b"")
    assert_one_line(err)


def test_no_sessions_root_at_all_is_exit_3(tmp_path):
    code, out, err = run([SID], env_for(tmp_path / "missing"))
    assert (code, out) == (3, b"")
    assert_one_line(err)


# 3. any other exit: a fault, one stderr line ------------------------------------

def test_no_sid_and_no_env_is_a_usage_fault(root):
    code, out, err = run([], env_for(root))
    assert (code, out) == (2, b"")
    assert_one_line(err)
    assert "CLAUDE_SESSION_ID" in err


@pytest.mark.parametrize("sid", ["..", ".", "a/b", "../etc", "x\0y"])
def test_a_sid_that_is_not_a_session_id_is_a_fault(root, sid):
    code, out, err = run([sid], env_for(root))
    assert code not in (0, 3) and out == b""
    assert_one_line(err)


def test_an_ambiguous_short_id_is_a_fault(root):
    write(root, SID)
    write(root, OTHER)
    code, out, err = run([SID[:8]], env_for(root))
    assert code not in (0, 3) and out == b""
    assert_one_line(err)
    assert "ambiguous" in err


def test_a_many_way_ambiguous_short_id_is_still_one_line(root):
    for n in range(settings.AMBIGUOUS_SHOW + 3):
        write(root, f"3f9c2a1e-{n:04d}-4908-8706-050403020100")
    code, out, err = run(["3f9c"], env_for(root))
    assert code not in (0, 3) and out == b""
    assert_one_line(err)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_block_is_a_fault_not_no_block(root):
    path = write(root, SID)
    path.chmod(0)
    try:
        code, out, err = run([SID], env_for(root))
    finally:
        path.chmod(0o600)
    assert code not in (0, 3) and out == b""
    assert_one_line(err)


def test_a_block_that_is_a_directory_is_a_fault(root):
    (root / SID / identity_block.IDENTITY_FILE).mkdir(parents=True)
    code, out, err = run([SID], env_for(root))
    assert code not in (0, 3) and out == b""
    assert_one_line(err)


def test_an_oversized_file_is_a_fault_never_a_partial_block(root):
    write(root, SID, b"x" * (identity_block.MAX_BYTES + 1))
    code, out, err = run([SID], env_for(root))
    assert code not in (0, 3) and out == b""
    assert_one_line(err)


def test_exactly_max_bytes_is_still_a_block(root):
    data = b"x" * identity_block.MAX_BYTES
    write(root, SID, data)
    assert run([SID], env_for(root)) == (0, data, "")


@pytest.mark.parametrize("argv", [[SID, "extra"], ["--bogus"]])
def test_a_usage_error_is_one_line_exit_2(root, argv, capsys):
    with pytest.raises(SystemExit) as e:
        identity_block.main(argv, env=env_for(root), stdout=io.BytesIO(), stderr=io.StringIO())
    assert e.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert_one_line(captured.err)


# 4. The dispatcher, and the real command -----------------------------------------

def test_the_verb_is_registered_with_the_dispatcher():
    assert "identity" in cli.HANDLERS and "identity" in cli.PARSERS
    assert "identity" in dict(cli.VERBS)
    assert "session identity" in cli.__doc__


def test_help_names_the_exit_codes(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["identity", "--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "exit 3" in out and "exit 0" in out


def _session(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """`session identity` as a separate process, as mail runs it."""
    full = {"PATH": "/usr/bin:/bin", **env}
    code = "import sys; from sessiontools.cli import main; sys.exit(main(sys.argv[1:]))"
    src = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run([sys.executable, "-c", code, "identity", *argv],
                          env={**full, "PYTHONPATH": src}, capture_output=True,
                          timeout=30, check=False)


def test_the_command_prints_the_same_bytes_and_loads_no_yaml(root):
    write(root, SID)
    probe = ("import sys; from sessiontools.cli import main; rc = main(sys.argv[1:]); "
             "sys.stdout.flush(); assert 'yaml' not in sys.modules, 'yaml loaded'; "
             "sys.exit(rc)")
    src = str(Path(__file__).resolve().parents[1] / "src")
    r = subprocess.run([sys.executable, "-c", probe, "identity", SID],
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": src, **env_for(root)},
                       capture_output=True, timeout=30, check=False)
    assert (r.returncode, r.stdout, r.stderr) == (0, BLOCK.encode(), b"")


def test_the_command_exit_3_with_no_block(root):
    r = _session([SID], env_for(root))
    assert (r.returncode, r.stdout) == (3, b"")
    assert r.stderr.count(b"\n") == 1
