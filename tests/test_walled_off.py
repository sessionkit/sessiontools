"""The conftest wall, pinned: what every test runs inside.

A wall no test checks is a wall a refactor removes. Three parts are pinned
here: the environment a test sees, the dead key-init path a launch under
test reaches (in-process and as a subprocess), and the teardown's kill,
which must hit the private servers and nothing a test pointed `$TMUX` at
(review of R8 slice 2, 2030-12-08: the old `tmux kill-server`
passed os.environ through, and tmux follows `$TMUX` first).

The kill tests start their servers the way the code under test does - no
`-S`, just `TMUX_TMPDIR` - so the sockets sit where real ones do,
`<dir>/tmux-<uid>/<name>`. A socket at the top of the folder let a kill
that skipped `default`, didn't recurse, or stopped at the first socket
pass (review of the slice-2 LOWs, 2030-12-08).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import conftest
import pytest
from conftest import kill_private_servers
from start_support import WALLED_STUBS, clean_env, launch_e2e, plan_for, walled_bin

from sessiontools.start.environment import SESSION_IDENTITY_VARS
from sessiontools.start.errors import LaunchError

NO_TMUX = pytest.mark.skipif(shutil.which("tmux") is None, reason="no tmux")
KEYED = "start:\n  keyed: true\n"


def no_tmux_env() -> dict:
    return {key: value for key, value in os.environ.items() if not key.startswith("TMUX")}


def start_server(tmpdir: str, name: str = "default") -> str:
    """A detached server under `tmpdir` exactly as tmux lays one out
    unprompted: `TMUX_TMPDIR` and `-L`, never `-S`. Returns its socket."""
    subprocess.run(["tmux", "-L", name, "-f", "/dev/null", "new-session", "-d", "sleep 60"],
                   env=dict(no_tmux_env(), TMUX_TMPDIR=tmpdir), capture_output=True, check=True)
    socket = os.path.join(tmpdir, f"tmux-{os.getuid()}", name)
    assert os.path.exists(socket), f"tmux put its socket somewhere else than {socket}"
    return socket


def kill_decoy(other: str) -> None:
    """Stop the second wall's server by its own socket, then drop the wall."""
    subprocess.run(["tmux", "-S", os.path.join(other, f"tmux-{os.getuid()}", "default"),
                    "kill-server"], env=no_tmux_env(), capture_output=True, check=False)
    shutil.rmtree(other, ignore_errors=True)


def alive(socket: str) -> bool:
    return subprocess.run(["tmux", "-S", socket, "has-session"], env=no_tmux_env(),
                          capture_output=True, check=False).returncode == 0


def test_no_live_sessions_dir_or_key_init_reaches_a_test():
    walled = os.environ["SESSIONS_DIR"]          # SET by the wall (task 4.1)
    assert walled and not walled.startswith(os.path.expanduser("~/.session"))
    assert not os.path.exists(os.environ["SESSION_KEY_INIT"])
    assert os.environ["TMUX_TMPDIR"].startswith("/tmp/st-tmux-")
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    assert clean_env()["SESSION_KEY_INIT"] == os.environ["SESSION_KEY_INIT"]


def test_a_launch_test_never_inherits_the_runners_session_identity(monkeypatch):
    """Run inside a seat, the runner holds a real session identity. clean_env
    takes off every SESSION_IDENTITY_VARS name (the old hand list missed
    CLAUDE_CODE_SESSION_ID: ST-113 do-002), so a launch test sees only what
    the test itself hands in."""
    for name in SESSION_IDENTITY_VARS:
        monkeypatch.setenv(name, "runner-" + name)
    assert not set(SESSION_IDENTITY_VARS) & set(clean_env())
    assert clean_env(CLAUDE_SESSION_ID="given")["CLAUDE_SESSION_ID"] == "given"


def test_a_launch_test_never_inherits_the_runners_git_signing_block(monkeypatch):
    """ST-118: run inside a KEYED seat, the runner also holds session-key-init's
    GIT_CONFIG_* signing block, and the tuple alone left it in every launch
    test. clean_env drops the indexed family by `is_session_identity`, keeps
    the runner's GIT_CONFIG_GLOBAL, and a test can still hand a block in."""
    runner = {"GIT_CONFIG_COUNT": "2",
              "GIT_CONFIG_KEY_0": "user.signingkey", "GIT_CONFIG_VALUE_0": "key::runner",
              "GIT_CONFIG_KEY_1": "commit.gpgsign", "GIT_CONFIG_VALUE_1": "true"}
    for name, value in runner.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/runner/gitconfig")
    env = clean_env()
    assert not set(runner) & set(env)
    assert env["GIT_CONFIG_GLOBAL"] == "/runner/gitconfig"
    assert clean_env(GIT_CONFIG_COUNT="0")["GIT_CONFIG_COUNT"] == "0"


def test_a_keyed_subprocess_without_its_stub_refuses_at_the_dead_path(tmp_path):
    """A dry run, so even a broken wall mints nothing: it would only find
    the real primitive, and say so."""
    result, record, _work = launch_e2e(tmp_path, "--dry-run", "--keyed", "scout")
    assert result.returncode != 0, result.stdout
    assert f"not found/executable at {os.environ['SESSION_KEY_INIT']}" in result.stderr
    assert not (record / "argv").exists()


def test_a_keyed_plan_without_its_stub_refuses_at_the_dead_path(tmp_path):
    with pytest.raises(LaunchError) as refused:
        plan_for(tmp_path, KEYED, flags=["--dry-run"])
    assert f"not found/executable at {os.environ['SESSION_KEY_INIT']}" in str(refused.value)


@NO_TMUX
def test_the_teardown_kills_every_private_server_where_tmux_puts_them():
    mine = os.environ["TMUX_TMPDIR"]
    ours = [start_server(mine), start_server(mine, "second")]
    kill_private_servers(mine)
    assert [socket for socket in ours if alive(socket)] == [], "a private server survived"


@NO_TMUX
def test_the_teardown_kill_never_follows_a_tmux_a_test_set(monkeypatch):
    mine = os.environ["TMUX_TMPDIR"]
    other = tempfile.mkdtemp(prefix="st-tmux-", dir="/tmp")    # a second PRIVATE wall
    try:
        ours, theirs = start_server(mine), start_server(other)
        monkeypatch.setenv("TMUX", f"{theirs},1,0")    # what a careless test leaves
        kill_private_servers(mine)
        assert not alive(ours), "the private server survived its teardown"
        assert alive(theirs), "the teardown followed $TMUX to another server"
    finally:
        kill_decoy(other)


@NO_TMUX
def test_the_teardown_kill_never_follows_a_hard_link_out():
    """A hard link resolves in place, so realpath alone passes it."""
    mine = os.environ["TMUX_TMPDIR"]
    other = tempfile.mkdtemp(prefix="st-tmux-", dir="/tmp")    # the decoy's own wall
    try:
        theirs = start_server(other)
        try:
            os.link(theirs, os.path.join(mine, "linked"))
        except OSError as refused:
            pytest.skip(f"this filesystem will not hard-link a socket: {refused}")
        kill_private_servers(mine)
        assert alive(theirs), "the teardown killed a server through a hard link"
    finally:
        kill_decoy(other)


@NO_TMUX
def test_the_teardown_kill_never_follows_a_symlink_to_a_hard_link_out():
    """Both links in the wall: the symlink resolves in place to the hard
    link, and the hard link's own count was all an lstat saw (review of
    71d755f, low 1)."""
    mine = os.environ["TMUX_TMPDIR"]
    other = tempfile.mkdtemp(prefix="st-tmux-", dir="/tmp")
    try:
        theirs = start_server(other)
        hard = os.path.join(mine, "linked")
        try:
            os.link(theirs, hard)
        except OSError as refused:
            pytest.skip(f"this filesystem will not hard-link a socket: {refused}")
        os.symlink(hard, os.path.join(mine, "pointer"))
        kill_private_servers(mine)
        assert alive(theirs), "the teardown killed a server through a symlink to a hard link"
    finally:
        kill_decoy(other)


@NO_TMUX
def test_the_teardown_kills_our_server_even_when_it_is_hard_linked_from_outside():
    """A link made OUTSIDE the wall to our socket must not make the teardown
    leak our own server (review of 71d755f, low 2)."""
    mine = os.environ["TMUX_TMPDIR"]
    outside = tempfile.mkdtemp(prefix="st-outside-", dir="/tmp")
    ours = start_server(mine)
    try:
        try:
            os.link(ours, os.path.join(outside, "linked"))
        except OSError as refused:
            pytest.skip(f"this filesystem will not hard-link a socket: {refused}")
        kill_private_servers(mine)
        assert not alive(ours), "our own server leaked because a link to it sat outside"
    finally:
        subprocess.run(["tmux", "-S", ours, "kill-server"], env=no_tmux_env(),
                       capture_output=True, check=False)
        shutil.rmtree(outside, ignore_errors=True)


@NO_TMUX
def test_a_socket_that_cannot_be_asked_stops_nothing(monkeypatch):
    """Whatever the OS says about one file, the walk goes on: a plain file,
    a dangling symlink, and a query that raises are nobody's, and the server
    after them is still killed (review of 71d755f, low 3)."""
    mine = os.environ["TMUX_TMPDIR"]
    ours = start_server(mine)
    refused = os.path.join(mine, "refused")
    with open(refused, "w"):
        pass
    with open(os.path.join(mine, "plain"), "w"):
        pass
    os.symlink(os.path.join(mine, "nowhere"), os.path.join(mine, "dangling"))
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[:3] == ["tmux", "-S", refused]:
            raise PermissionError(13, "stand-in for any OS refusal", refused)
        return real_run(argv, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(conftest.subprocess, "run", run)
        kill_private_servers(mine)
    assert not alive(ours), "the teardown stopped before the server it should have killed"


def test_no_test_reaches_the_hosts_pass_or_gpgconf():
    """R8 slice 4: a live launch with declared secrets now calls `pass`."""
    for name in ("pass", "gpgconf"):
        found = shutil.which(name)
        assert found and found.startswith(os.environ["TMUX_TMPDIR"]), found
        run = subprocess.run([name, "show", "x"], capture_output=True, text=True, check=False)
        assert run.returncode == 97 and "walled off in tests" in run.stderr


def test_the_tmux_e2e_path_finds_only_the_walled_stubs(tmp_path):
    """The tmux e2e builds its PATH from scratch, so the pane sees no venv.
    Before the review of c874019 (low 1) it had no wall, and
    `/opt/homebrew/bin` would have handed a secrets-declaring e2e the host's
    real `pass` and `gpgconf`."""
    from test_start_placement import e2e_path
    path = e2e_path(tmp_path / "bin")
    for name in WALLED_STUBS:
        assert shutil.which(name, path=path).startswith(walled_bin() + os.sep)


def test_a_tmux_capable_env_that_reaches_the_hosts_pass_is_refused(tmp_path):
    """`private_env` is the door every tmux-starting subprocess goes through;
    a PATH that finds a `pass` outside the wall stops the test there."""
    from test_start_placement import private_env
    host = tmp_path / "host-bin"
    host.mkdir()
    for name in WALLED_STUBS:
        (host / name).write_text("#!/bin/sh\nexit 0\n")
        (host / name).chmod(0o755)
    with pytest.raises(pytest.fail.Exception, match="not the walled stub"):
        private_env({"PATH": f"{host}{os.pathsep}{walled_bin()}"})
    assert private_env({"PATH": f"{walled_bin()}{os.pathsep}{host}"})["PATH"]


def test_the_wall_is_compared_by_real_path(tmp_path):
    """Round 2, low 4: a string prefix let `<wall>/../../host-bin` through,
    and refused the wall's own real path (macOS's /tmp is /private/tmp)."""
    from test_start_placement import private_env
    host = tmp_path / "host-bin"
    host.mkdir()
    for name in WALLED_STUBS:
        (host / name).write_text("#!/bin/sh\nexit 0\n")
        (host / name).chmod(0o755)
    # `..` resolves physically (from /private/tmp/...), so count from there
    real = os.path.realpath
    sneaky = os.path.join(walled_bin(), os.path.relpath(real(host), real(walled_bin())))
    assert shutil.which("pass", path=sneaky), "the dot-dot PATH must really find a pass"
    assert sneaky.startswith(walled_bin() + os.sep)          # the string says "inside"
    with pytest.raises(pytest.fail.Exception, match="not the walled stub"):
        private_env({"PATH": sneaky})
    assert private_env({"PATH": os.path.realpath(walled_bin())})["PATH"]
