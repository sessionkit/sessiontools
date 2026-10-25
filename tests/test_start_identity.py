"""Agent resolution (and the new default folder), where sessions live, and
the identity a launch mints or projects. Ported from the old suite's launch
core and do-009 (a dry run mints nothing) sections."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from start_support import STUB_SID, make_agents_root, write_key_init_stub

from sessiontools.start import agents as ag
from sessiontools.start import default_seat
from sessiontools.start import identity as idn
from sessiontools.start.errors import EXIT_USAGE, LaunchError

ROOT = Path(__file__).resolve().parent.parent

# ---------- resolution ----------------------------------------------------------


def test_the_default_agents_folder_is_session_agents(monkeypatch, tmp_path):
    """The default by design. (The old launcher's was ~/.claude/agents.) AGENTS_DIR still overrides."""
    assert ag.DEFAULT_AGENTS_DIR == "~/.session/agents"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert ag.agents_root({}) == str(tmp_path / ".session" / "agents")
    assert ag.agents_root({"AGENTS_DIR": "/elsewhere"}) == "/elsewhere"
    (tmp_path / ".session" / "agents" / "alice").mkdir(parents=True)
    (tmp_path / ".session" / "agents" / "alice" / "alice.md").write_text("You are.\n")
    assert ag.resolve_agent("alice", environ={}).file == str(
        tmp_path / ".session" / "agents" / "alice" / "alice.md")


def test_unknown_agent_error_names_both_candidate_paths(tmp_path):
    root = str(tmp_path / "agents")
    os.makedirs(root)
    with pytest.raises(LaunchError) as excinfo:
        ag.resolve_agent("nosuch", root=root)
    assert os.path.join(root, "nosuch") in str(excinfo.value)
    assert os.path.join(root, "nosuch.md") in str(excinfo.value)
    assert excinfo.value.code == EXIT_USAGE


def test_file_agent_resolves(tmp_path):
    root = make_agents_root(tmp_path, layout="file")
    agent = ag.resolve_agent("scout", root=root)
    assert agent.file == os.path.join(root, "scout.md")
    assert agent.directory == "" and agent.home == ""


def test_directory_agent_resolves_with_home(tmp_path):
    root = make_agents_root(tmp_path, layout="dir")
    os.makedirs(os.path.join(root, "scout", "home"))
    agent = ag.resolve_agent("scout", root=root)
    assert agent.file == os.path.join(root, "scout", "scout.md")
    assert agent.home == os.path.join(root, "scout", "home")


def test_directory_without_markdown_is_its_own_error(tmp_path):
    root = str(tmp_path / "agents")
    os.makedirs(os.path.join(root, "broken"))
    with pytest.raises(LaunchError) as excinfo:
        ag.resolve_agent("broken", root=root)
    assert "broken.md is missing" in str(excinfo.value)


def test_resolve_agent_or_bare_covers_none_and_dash(tmp_path):
    assert ag.resolve_agent_or_bare(None) is ag.BARE_AGENT
    assert ag.resolve_agent_or_bare(ag.NO_AGENT) is ag.BARE_AGENT
    root = make_agents_root(tmp_path)
    assert ag.resolve_agent_or_bare("scout", root=root).name == "scout"
    with pytest.raises(LaunchError):
        ag.resolve_agent_or_bare("nosuch", root=root)


def test_list_agents_mirrors_resolution(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    (root / "flat.md").write_text("x")
    (root / "dir").mkdir()
    (root / "dir" / "dir.md").write_text("x")
    (root / "broken").mkdir()               # no broken.md: resolve refuses it
    (root / ".hidden.md").write_text("x")
    # The default seat's word resolves to the package's built-in whatever the
    # folder holds, so it is always listed (ST-256).
    seat = default_seat.word()
    assert ag.list_agents(root=str(root)) == sorted(["dir", "flat", seat])
    assert ag.list_agents(root=str(tmp_path / "nowhere")) == [seat]


# ---------- where sessions live -----------------------------------------------

def test_every_session_lands_in_the_one_root_under_home(tmp_path):
    """Task 4.1: `$HOME/.session/sessions`, whatever the agent and its layout
    (it was `<agents dir>/sessions`, or `<agent>/home/sessions`)."""
    home = str(tmp_path / "home")
    for layout in ("file", "dir"):
        (tmp_path / layout).mkdir()
        root = make_agents_root(tmp_path / layout, layout=layout)
        if layout == "dir":
            os.makedirs(os.path.join(root, "scout", "home"))
        agent = ag.resolve_agent("scout", root=root)
        assert ag.sessions_root(agent, root=root, environ={"HOME": home}) == \
            os.path.join(home, ".session", "sessions")
    assert ag.sessions_root(ag.BARE_AGENT, environ={"HOME": home}) == \
        os.path.join(home, ".session", "sessions")


def test_the_launcher_and_the_settings_reader_agree_on_the_dir(tmp_path):
    from sessiontools import settings
    env = {"HOME": str(tmp_path)}
    assert os.path.join(ag.sessions_root(ag.BARE_AGENT, environ=env), "abc") == \
        str(settings.session_dir("abc", env))


def test_sessions_dir_env_overrides_the_sessions_root(tmp_path):
    root = make_agents_root(tmp_path, layout="dir")
    agent = ag.resolve_agent("scout", root=root)
    assert ag.sessions_root(agent, root=root, environ={"SESSIONS_DIR": "~/elsewhere/s"}) \
        == os.path.expanduser("~/elsewhere/s")


# ---------- the uuid path -------------------------------------------------------

def test_uuid_identity_mints_dir_and_persists_the_id(tmp_path):
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    identity = idn.mint_identity(agent, root=str(tmp_path / "sessions"))
    assert not identity.keyed and identity.minted
    assert re.fullmatch(r"[0-9a-f-]{36}", identity.session_id)
    with open(os.path.join(identity.session_dir, "session-id")) as handle:
        assert handle.read() == identity.session_id
    assert identity.env == {"CLAUDE_SESSION_ID": identity.session_id}


def test_mint_identity_dry_run_projects_the_uuid_path_without_touching_disk(tmp_path):
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    sessions = tmp_path / "sessions"
    identity = idn.mint_identity(agent, root=str(sessions), dry_run=True)
    assert re.fullmatch(r"[0-9a-f-]{36}", identity.session_id)
    assert identity.session_dir == str(sessions / identity.session_id)
    assert identity.minted is False
    assert not sessions.exists()


# ---------- the keyed path: session-key-init as a subprocess --------------------

def test_keyed_identity_drives_the_primitive_and_parses_its_block(tmp_path):
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    stub, record = write_key_init_stub(tmp_path)
    identity = idn.mint_identity(agent, root=str(tmp_path / "sessions"), keyed=True,
                                 environ={"SESSION_KEY_INIT": stub,
                                          "PATH": os.environ["PATH"]},
                                 reap_pid=4242)
    assert identity.keyed and identity.session_id == STUB_SID
    assert identity.env["CLAUDE_SESSION_AGENT_SOCK"] == "/tmp/stub-agent.sock"
    argv = Path(record).read_text().split()
    assert argv[argv.index("--agent-name") + 1] == "scout"
    assert argv[argv.index("--reap-pid") + 1] == "4242"
    assert "--session-dir" in argv
    with open(os.path.join(identity.session_dir, "session-id")) as handle:
        assert handle.read() == STUB_SID


def test_the_primitive_never_runs_under_the_callers_identity_or_signing_block(tmp_path):
    """ST-118 site 4: a keyed launcher-side mint (keyed direct path, keyed
    resume) ran session-key-init with the caller's whole env. Inside a keyed
    seat that is the PARENT's git signing block, and lamp-check's registry
    commit then signs with the parent's key through the CHILD's socket and
    fails (rca-001 K1: `Couldn't find key in agent`, exit 128). The
    primitive gets the env minus the session identity; the rest stays."""
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    stub, record = write_key_init_stub(tmp_path)
    caller = {"SESSION_KEY_INIT": stub, "PATH": os.environ["PATH"],
              "CLAUDE_CODE_SESSION_ID": "parent", "CLAUDE_SESSION_ID": "parent",
              "CLAUDE_SESSION_AGENT_SOCK": "/parent/agent.sock",
              "CLAUDE_SESSION_AGENT_PID": "4242", "CLAUDE_SESSION_PUB": "/parent/key.pub",
              "GIT_CONFIG_COUNT": "2",
              "GIT_CONFIG_KEY_0": "user.signingkey", "GIT_CONFIG_VALUE_0": "key::parent",
              "GIT_CONFIG_KEY_1": "commit.gpgsign", "GIT_CONFIG_VALUE_1": "true",
              "GIT_CONFIG_GLOBAL": "/agent/gitconfig", "KEEP_ME": "yes"}
    idn.mint_identity(agent, root=str(tmp_path / "sessions"), keyed=True,
                      environ=caller, reap_pid=4242)
    seen = dict(line.split("=", 1) for line in
                Path(record + ".env").read_text().splitlines() if "=" in line)
    assert not [name for name in seen
                if name.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_",
                                    "GIT_CONFIG_VALUE_", "CLAUDE_SESSION_"))
                or name == "CLAUDE_CODE_SESSION_ID"]
    assert seen["GIT_CONFIG_GLOBAL"] == "/agent/gitconfig"
    assert seen["KEEP_ME"] == "yes"


def test_key_label_is_what_the_primitive_labels_the_key_with(tmp_path):
    """Ruling 1b."""
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    stub, record = write_key_init_stub(tmp_path)
    idn.mint_identity(agent, root=str(tmp_path / "sessions"), keyed=True,
                      environ={"SESSION_KEY_INIT": stub, "PATH": os.environ["PATH"]},
                      reap_pid=4242, key_label="voicemode+super")
    argv = Path(record).read_text().split()
    assert argv[argv.index("--agent-name") + 1] == "voicemode+super"


def test_key_label_defaults_to_the_agent_name_and_a_dry_run_mints_nothing(tmp_path):
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    stub, record = write_key_init_stub(tmp_path)
    identity = idn.mint_identity(agent, root=str(tmp_path / "sessions"), keyed=True,
                                 environ={"SESSION_KEY_INIT": stub}, reap_pid=4242,
                                 dry_run=True)
    assert not os.path.exists(record), "session-key-init ran under --dry-run"
    assert identity.keyed and identity.minted is False
    assert identity.session_id == idn.DRY_RUN_KEYED_ID
    assert identity.mint_command[0] == stub
    assert identity.mint_command[identity.mint_command.index("--agent-name") + 1] == "scout"
    assert not (tmp_path / "sessions").exists()


def test_a_keyed_dry_run_of_a_broken_primitive_is_still_loud(tmp_path):
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    with pytest.raises(LaunchError) as excinfo:
        idn.mint_identity(agent, root=str(tmp_path / "sessions"), keyed=True,
                          environ={"SESSION_KEY_INIT": str(tmp_path / "no")}, dry_run=True)
    assert "SESSION_KEY_INIT" in str(excinfo.value)


def test_keyed_failure_is_fatal_never_a_silent_unkeyed_fallback(tmp_path):
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    stub, _record = write_key_init_stub(tmp_path, exit_code=3)
    with pytest.raises(LaunchError) as excinfo:
        idn.mint_identity(agent, root=str(tmp_path / "sessions"), keyed=True,
                          environ={"SESSION_KEY_INIT": stub, "PATH": os.environ["PATH"]})
    assert "exit 3" in str(excinfo.value)


def test_the_default_key_init_is_the_claude_agents_primitive():
    """A subprocess contract (CA-83): driven, never absorbed."""
    assert idn.key_init_path({}) == os.path.expanduser(
        "~/.claude/agents/lib/session-keys/session-key-init")


# ---------- {shortsid} ------------------------------------------------------------

def test_expand_shortsid_fills_from_the_identity():
    """Ruling 2b: tm writes the token,
    the launcher fills it once the id exists."""
    identity = idn.SessionIdentity(STUB_SID, "/x", True, {})
    assert idn.expand_shortsid("super.voicemode+{shortsid}", identity) \
        == "super.voicemode+" + STUB_SID[:8]
    assert idn.expand_shortsid("plain", identity) == "plain"
    assert idn.expand_shortsid("x+{shortsid}", None) == "x+{shortsid}"


def test_keyed_dry_run_shows_the_shortsid_slot_not_a_sliced_placeholder():
    identity = idn.SessionIdentity(idn.DRY_RUN_KEYED_ID, "/x", True, {}, minted=False)
    assert idn.expand_shortsid("scout+{shortsid}", identity) == "scout+<shortsid>"


def test_identity_never_reads_the_callers_session_id():
    """identity.py's note, pinned: the resolver answers "who am I"; the
    launcher decides who the CHILD will be and sheds its caller's ids."""
    source = Path(idn.__file__).read_text(encoding="utf-8")
    assert "from sessiontools import" not in source
    assert "session_id()" in source          # the note naming what NOT to use


# ---------- session-settings 4.2: agent/config/ (Open 12 RULED) ------------------------------

def test_agent_config_is_the_default_once_it_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".session" / "agent" / "config"
    (config / "alice").mkdir(parents=True)
    (config / "alice" / "alice.md").write_text("You are.\n")       # extras: a folder
    (config / "bnc.md").write_text("You are.\n")                 # none: the flat file
    assert ag.agents_root({}) == str(config)
    assert ag.resolve_agent("alice", environ={}).file == str(config / "alice" / "alice.md")
    assert ag.resolve_agent("bnc", environ={}).file == str(config / "bnc.md")
    assert ag.agents_root({"AGENTS_DIR": "/elsewhere"}) == "/elsewhere"


def test_without_agent_config_the_old_folder_still_answers(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".session" / "agent").mkdir(parents=True)        # agent/ but no config/
    assert ag.agents_root({}) == str(tmp_path / ".session" / "agents")


def test_both_branches_read_the_same_home(tmp_path):
    env = {"HOME": str(tmp_path / "x")}
    assert ag.agents_root(env) == str(tmp_path / "x" / ".session" / "agents")
    (tmp_path / "x" / ".session" / "agent" / "config").mkdir(parents=True)
    assert ag.agents_root(env) == str(tmp_path / "x" / ".session" / "agent" / "config")


# ---------- ST-112: the mint runs detached, so its reaper outlives the pane ----

SID_STUB = """#!/usr/bin/env bash
# records its own pid and session id, then answers like the primitive
echo "$$" > "%(dir)s/pid"
python3 -c 'import os; print(os.getsid(os.getppid()))' > "%(dir)s/sid"
mkdir -p "%(dir)s/sessions/%(sid)s"
printf 'CLAUDE_SESSION_ID=%(sid)s\\n'
"""

INTERRUPT_STUB = """#!/usr/bin/env bash
# the primitive's shape: a backgrounded, disowned reaper; an EXIT trap that
# cleans up a half-born keyholder; then a slow foreground step
( while :; do sleep 0.2; done ) >/dev/null 2>&1 &
echo "$!" > "%(dir)s/reaper.pid"
disown
trap 'echo "trapped $?" > "%(dir)s/exit-trap"' EXIT
echo "$$" > "%(dir)s/pid"
# `ready` comes from the foreground step itself: a SIGINT that arrives
# after it can only land on that step, never in bash's fork window
sh -c ': > "%(dir)s/ready"; exec sleep 30'
printf 'CLAUDE_SESSION_ID=never\\n'
"""


def write_stub(tmp_path: Path, template: str) -> str:
    stub = tmp_path / "session-key-init-stub"
    stub.write_text(template % {"dir": tmp_path, "sid": STUB_SID})
    stub.chmod(0o755)
    return str(stub)


def mint_with(tmp_path: Path, stub: str, **extra: str):
    agent = ag.resolve_agent("scout", root=make_agents_root(tmp_path))
    return idn.mint_identity(agent, root=str(tmp_path / "sessions"), keyed=True,
                             environ={"SESSION_KEY_INIT": stub,
                                      "PATH": os.environ["PATH"], **extra},
                             reap_pid=4242)


def test_the_keyed_mint_runs_in_its_own_session(tmp_path):
    """ST-112: born in the launcher's session, the primitive's reaper was
    SIGHUPed with the seat and the keyholder leaked."""
    identity = mint_with(tmp_path, write_stub(tmp_path, SID_STUB))
    assert identity.session_id == STUB_SID
    stub_pid = int((tmp_path / "pid").read_text())
    stub_sid = int((tmp_path / "sid").read_text())
    assert stub_sid != os.getsid(0)
    assert stub_sid == stub_pid, "the mint should lead a session of its own"


def test_a_blessed_launch_mints_in_the_launchers_session_as_before(tmp_path):
    """The desk-only bless ceremony may need /dev/tty, which a detached mint
    cannot open: LAMP_BLESS keeps the old, undetached path."""
    mint_with(tmp_path, write_stub(tmp_path, SID_STUB), LAMP_BLESS="1")
    assert int((tmp_path / "sid").read_text()) == os.getsid(0)


def test_ctrl_c_during_a_detached_mint_is_forwarded_not_sigkilled(tmp_path):
    """Detached, the tty's SIGINT reaches only the launcher. The mint must
    still get it, so that its EXIT trap kills a half-born keyholder. The
    reaper ignores it, as an async list without job control does, and
    keeps watching.

    The Ctrl-C has to be able to reach this process at all (ST-214). A
    runner started as an async list by a non-interactive shell (`pytest &`
    in a script, through `env -i` or `uv run` alike; `make` resets it)
    inherits SIGINT IGNORED, and Python then never installs its
    KeyboardInterrupt handler. The SIGINT below vanished, the stub slept
    its full 30s and printed `never`, and the mint failed on
    `sessions/never`. So the test installs the handler a launcher started
    at a tty has, and puts the old disposition back after."""
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        _ctrl_c_during_a_detached_mint(tmp_path)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _ctrl_c_during_a_detached_mint(tmp_path: Path) -> None:
    stub = write_stub(tmp_path, INTERRUPT_STUB)
    ready = tmp_path / "ready"

    def interrupt_when_ready():
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    timer = threading.Thread(target=interrupt_when_ready, daemon=True)
    timer.start()
    reaper = None
    try:
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            mint_with(tmp_path, stub)
        assert time.monotonic() - started < 10, "the 30s step was not interrupted"
        reaper = int((tmp_path / "reaper.pid").read_text())
        assert (tmp_path / "exit-trap").read_text().startswith("trapped "), \
            "the mint's EXIT trap never ran - it was killed, not interrupted"
        with pytest.raises(ProcessLookupError):  # the mint itself is gone...
            os.kill(int((tmp_path / "pid").read_text()), 0)
        os.kill(reaper, 0)  # ...and the reaper survived the forwarded SIGINT
    finally:
        timer.join(timeout=15)
        if reaper is None and (tmp_path / "reaper.pid").exists():
            reaper = int((tmp_path / "reaper.pid").read_text())
        if reaper is not None:
            try:  # the stand-in reaper loops forever; it is ours to end
                os.kill(reaper, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_the_ctrl_c_test_holds_when_its_runner_inherits_sigint_ignored(tmp_path):
    """ST-214: the ctrl-c test went red whenever its pytest was launched as
    an async list by a non-interactive shell, which is how gate scripts run
    the suite in the background. That launch hands SIGINT down ignored.
    `trap '' INT` then `exec` hands it down the same way. Run the test in a
    child pytest under it: it must pass, and in well under the stub's 30s."""
    node = ("tests/test_start_identity.py::"
            "test_ctrl_c_during_a_detached_mint_is_forwarded_not_sigkilled")
    started = time.monotonic()
    run = subprocess.run(
        ["bash", "-c", "trap '' INT; exec \"$@\"", "bash", sys.executable, "-m", "pytest",
         "-q", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "child"), node],
        cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    tail = run.stdout[-3000:] + run.stderr[-2000:]
    assert run.returncode == 0, tail
    assert "1 passed" in run.stdout, tail
    assert time.monotonic() - started < 25, "the child's SIGINT was ignored: " + tail


def test_forward_interrupt_on_a_finished_child_is_a_no_op():
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    idn.forward_interrupt(proc, grace=0.1)  # must not raise
