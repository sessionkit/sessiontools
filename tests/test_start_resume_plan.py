"""Resume (R8 slice 3, part 2), wired: `session start ... --resume <id>` runs
the PORT now, not the bridge. Each dry run runs as a subprocess under a
test-owned HOME (CA-62 never globs the real ~/.claude/projects),
AGENTS_DIR and TMPDIR, with TZ=UTC. The acts are checked with stubs: a stub
`claude` on PATH, a stub session-key-init.

The dry-run tests take `port_alone`, one param named `port`, so their node
ids are the ones they had when a manual run also set them beside the old
launcher (dropped from the release tree, ST-102).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from start_support import (
    STUB_SID,
    TIMEOUT_S,
    clean_env,
    launch_e2e,
    make_agents_root,
    plan_for,
    recorded_argv,
    session_cmd,
    write_key_init_stub,
)

from sessiontools.start.args import parse_args
from sessiontools.start.context import (
    BARE_CONVERSE_SIGNAL,
    CONVERSE_SIGNAL,
    PlannedFile,
    PlannedPlugin,
)
from sessiontools.start.errors import EXIT_USAGE, LaunchError
from sessiontools.start.plan import RESUME_UNDER_TMUX, prepare

SID = "11111111-2222-3333-4444-555555555555"
EFFORT = "claude:\n  effort: high\n"
KEYED = "start:\n  keyed: true\n"
SECRETS = "environment:\n  secrets:\n    T: pass:x/y\n"
PROMPTED = 'start:\n  context:\n    system-prompt:\n      - "echo NEVER-ON-RESUME"\n'


def write_transcript(home: Path, sid: str, cwd: str) -> None:
    project = home / ".claude" / "projects" / "-proj"
    project.mkdir(parents=True, exist_ok=True)
    (project / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "cwd": cwd}) + "\n")


@pytest.fixture(params=["port"])
def port_alone(request) -> None:
    """One param, `port`: the run these tests have always had in the
    default suite, kept so their node ids do not move."""


def dry_run(tmp_path: Path, front_matter: str, *verb_args: str,
            extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """The port's dry run of `session start` on these inputs."""
    root = make_agents_root(tmp_path, front_matter=front_matter)
    for sub in ("home", "work", "tmpdir"):
        (tmp_path / sub).mkdir(exist_ok=True)
    env = clean_env(AGENTS_DIR=root, SESSIONS_DIR=os.path.join(root, "sessions"),
                    HOME=str(tmp_path / "home"),
                    TMPDIR=str(tmp_path / "tmpdir"), TZ="UTC")
    env.update(extra_env or {})
    port = subprocess.run(session_cmd(*verb_args), capture_output=True, text=True,
                          timeout=TIMEOUT_S, cwd=str(tmp_path / "work"), env=env, check=False)
    assert "using the bridge" not in port.stderr        # the port ran it
    return port


# ---------- the dry run ------------------------------------------------------------

@pytest.mark.parametrize("front_matter, verb_args", [
    (EFFORT, ["--dry-run", "scout", "--resume", SID]),
    (EFFORT, ["--dry-run", "scout", f"--resume={SID}", "carry", "on"]),
    (EFFORT, ["--dry-run", "--converse", "scout", "-r", SID, "hello"]),
    ("start:\n  converse: true\n  idle: true\n", ["--dry-run", "scout", "--resume", SID]),
    (EFFORT, ["--dry-run", "scout", "-c"]),
    (EFFORT, ["--dry-run", "scout", "--resume"]),
    (PROMPTED, ["--dry-run", "scout", "--resume", SID]),
    (SECRETS, ["--dry-run", "scout", "--resume", SID]),
    (EFFORT, ["--dry-run", "--first-message-file-over", "0", "scout", "--resume", SID]),
    (None, ["--dry-run", "-", "--resume", SID]),
    (None, ["--dry-run", "-", "--model", "opus", "-c", "go"]),
    # No door is computed that these reopen: the resumed session keeps its
    # own id, and a resume opens no system-prompt door.
    (EFFORT, ["--dry-run", "scout", "--resume", SID, "--session-id", "abc"]),
    (PROMPTED, ["--dry-run", "scout", "--resume", SID, "--append-system-prompt-file", "x"]),
    # What `tm session resume` sends (review of 75f4b88, low 5).
    (None, ["--dry-run", "-", "--resume", SID, "--name", "X"]),
])
def test_a_resume_dry_run_is_the_bridges_byte_for_byte(tmp_path, port_alone, front_matter,
                                                       verb_args):
    port = dry_run(tmp_path, front_matter or "", *verb_args)
    assert port.returncode == 0, port.stderr
    assert "mode:     resume (direct; the session keeps its own identity)" in port.stdout
    assert "NEVER-ON-RESUME" not in port.stdout          # no generator runs on a resume


def test_a_fable_resume_with_the_override_is_the_bridges_byte_for_byte(tmp_path, port_alone):
    """A Fable agent resumed with AGENT_ALLOW_FABLE=1: the gate lets it
    through (review of 75f4b88, low 5). Without the override the
    port refuses with its ask-the-operator line, `message send`, which
    test_start_roads pins."""
    port = dry_run(tmp_path, "claude:\n  model: fable\n", "--dry-run", "scout",
                   "--resume", SID, extra_env={"AGENT_ALLOW_FABLE": "1"})
    assert port.returncode == 0, port.stderr
    assert "mode:     resume (direct; the session keeps its own identity)" in port.stdout
    assert "fable" in port.stdout


def test_a_keyed_resume_dry_run_forks_as_the_bridge_does(tmp_path, port_alone):
    stub, record = write_key_init_stub(tmp_path)
    port = dry_run(tmp_path, KEYED, "--dry-run", "scout", "--resume", SID,
                   extra_env={"SESSION_KEY_INIT": stub, **home_of(tmp_path)})
    assert port.returncode == 0, port.stderr
    assert "FORKED into a fresh keyed identity" in port.stdout
    assert port.stdout.count("\n  --fork-session ") == 1    # one token in the command
    assert f"keyed resume = FORK: {SID} continues as" in port.stderr
    assert not os.path.exists(record), "a dry run ran the primitive"


def test_a_keyed_resume_that_already_forks_is_not_forked_twice(tmp_path, port_alone):
    stub, _record = write_key_init_stub(tmp_path)
    port = dry_run(tmp_path, "", "--dry-run", "--keyed", "scout",
                   "--resume", SID, "--fork-session",
                   extra_env={"SESSION_KEY_INIT": stub, **home_of(tmp_path)})
    assert port.stdout.count("\n  --fork-session ") == 1    # the caller's, not a second


@pytest.mark.parametrize("recorded_exists", [True, False])
def test_ca62_the_recorded_cwd_wins_as_in_the_bridge(tmp_path, port_alone, recorded_exists):
    recorded = tmp_path / "born-here"
    if recorded_exists:
        recorded.mkdir()
    (tmp_path / "home").mkdir()
    write_transcript(tmp_path / "home", SID, str(recorded))
    port = dry_run(tmp_path, EFFORT, "--dry-run", "scout", "--resume", SID)
    expected = recorded if recorded_exists else tmp_path / "work"
    assert f"workdir:  {expected}  (cwd)" in port.stdout
    assert f"cd {expected} || exit 1" in port.stdout
    assert ("(the session's recorded cwd)" in port.stderr) is recorded_exists
    assert ("no longer exists" in port.stderr) is not recorded_exists


def test_a_resume_reads_neither_directory_setting(tmp_path, port_alone):
    """claude scopes a session to its birth directory: --directory and
    `start: directory:` are not read, exactly as the bridge ignores them."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    port = dry_run(tmp_path, f"start:\n  directory: {elsewhere}\n",
                   "--dry-run", "--directory", str(elsewhere), "scout", "--resume", SID)
    assert f"workdir:  {tmp_path / 'work'}  (cwd)" in port.stdout


def test_an_unkeyed_resume_of_a_keyed_session_warns_as_the_bridge_does(tmp_path, port_alone):
    pub = tmp_path / "agents" / "sessions" / SID / "session.pub"
    pub.parent.mkdir(parents=True)
    pub.write_text("ssh-ed25519 AAAA test\n")
    port = dry_run(tmp_path, EFFORT, "--dry-run", "scout", "--resume", SID)
    assert "session was keyed; its key died with its process -- resuming UNKEYED" in port.stderr


# ---------- refusals: before any act ---------------------------------------------

def home_of(tmp_path: Path) -> dict:
    """The in-process plans' HOME: CA-62 reads HOME from the launch's
    environ, and without one it would glob the real ~/.claude/projects."""
    (tmp_path / "home").mkdir(exist_ok=True)
    return {"HOME": str(tmp_path / "home")}


def test_a_resume_under_tmux_is_refused_in_the_old_words(tmp_path):
    with pytest.raises(LaunchError) as refused:
        plan_for(tmp_path, EFFORT, argv_tail=["--resume", SID], flags=["--dry-run", "--in-tmux"],
                 extra_env=home_of(tmp_path))
    assert str(refused.value) == RESUME_UNDER_TMUX
    assert refused.value.code == EXIT_USAGE


def test_a_keyed_bare_resume_is_refused_and_mints_nothing(tmp_path):
    stub, record = write_key_init_stub(tmp_path)
    with pytest.raises(LaunchError) as refused:
        plan_for(tmp_path, KEYED, argv_tail=["-c"], extra_env={"SESSION_KEY_INIT": stub, **home_of(tmp_path)})
    assert "a keyed resume is a FORK" in str(refused.value)
    assert not os.path.exists(record)
    assert not (tmp_path / "agents" / "sessions").exists()


def test_a_door_opened_twice_on_a_keyed_resume_is_refused_before_the_mint(tmp_path):
    stub, record = write_key_init_stub(tmp_path)
    with pytest.raises(LaunchError):
        plan_for(tmp_path, KEYED, argv_tail=["--resume", SID, "--session-id", "x"],
                 extra_env={"SESSION_KEY_INIT": stub, **home_of(tmp_path)})
    assert not os.path.exists(record), "minted, then refused"


def test_key_label_on_an_unkeyed_resume_is_refused(tmp_path):
    """The one deliberate difference from the old launcher, which ignored it."""
    with pytest.raises(LaunchError) as refused:
        plan_for(tmp_path, EFFORT, argv_tail=["--resume", SID], flags=["--key-label", "x+y"],
                 extra_env=home_of(tmp_path))
    assert "--key-label" in str(refused.value)


# ---------- the plan and the acts ---------------------------------------------------

def test_an_unkeyed_resume_plan_keeps_the_sessions_own_id(tmp_path):
    plan = plan_for(tmp_path, EFFORT, argv_tail=["go", "on", "--resume", SID],
                    extra_env=home_of(tmp_path))
    assert plan.resume and plan.identity is None
    assert "--session-id" not in plan.argv
    assert plan.argv[plan.argv.index("--resume") + 1] == SID
    assert plan.argv[-1].startswith("Session restarted. Current date and time: ")
    assert plan.argv[-1].endswith("\n\ngo on")
    assert not (tmp_path / "agents" / "sessions").exists()


def bare_resume_plan(tmp_path: Path, *flags: str):
    """A resume plan for the bare agent `-`: no agent file in play."""
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    environ = {"AGENTS_DIR": make_agents_root(tmp_path), "PATH": os.environ.get("PATH", ""),
               "SESSIONS_DIR": str(tmp_path / "sessions"),
               "SESSION_KEY_INIT": os.environ["SESSION_KEY_INIT"], **home_of(tmp_path)}
    return prepare(parse_args([*flags, "-", "go", "--resume", SID]), environ=environ,
                   work_dir=str(work))


def test_a_bare_converse_resume_carries_the_neutral_voice_line(tmp_path):
    message = bare_resume_plan(tmp_path, "--converse").argv[-1]
    assert message.startswith("Session restarted. Current date and time: ")
    assert message.endswith(f"\n\n{BARE_CONVERSE_SIGNAL}\n\ngo")
    assert CONVERSE_SIGNAL not in message


def test_an_agent_file_converse_resume_keeps_the_fleet_voice_line(tmp_path):
    for front_matter, flags in ((EFFORT, ["--converse"]), ("start:\n  converse: true\n", [])):
        plan = plan_for(tmp_path, front_matter, argv_tail=["go", "--resume", SID],
                        flags=flags, extra_env=home_of(tmp_path))
        assert plan.argv[-1].endswith(f"\n\n{CONVERSE_SIGNAL}\n\ngo")
        assert BARE_CONVERSE_SIGNAL not in plan.argv[-1]


def test_a_keyed_live_resume_mints_through_the_primitive_and_forks(tmp_path, capsys):
    stub, record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, KEYED, argv_tail=["--resume", SID],
                    extra_env={"SESSION_KEY_INIT": stub, **home_of(tmp_path)})
    assert plan.identity.keyed and plan.identity.session_id == STUB_SID
    assert plan.argv[plan.argv.index("--session-id") + 1] == STUB_SID
    assert plan.argv.count("--fork-session") == 1
    assert "--agent-name scout" in Path(record).read_text()
    assert f"keyed resume = FORK: {SID} continues as {STUB_SID}" in capsys.readouterr().err


def test_a_live_resume_execs_claude_in_the_recorded_cwd(tmp_path):
    """End to end through the verb, a stub `claude` on PATH: the resume
    reaches claude verbatim, from the directory the session was born in."""
    born = tmp_path / "born"
    born.mkdir()
    home = tmp_path / "home"
    write_transcript(home, SID, str(born))
    result, record, _work = launch_e2e(tmp_path, "scout", "hi", "--resume", SID,
                                       extra_env={"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    assert "using the bridge" not in result.stderr
    argv = recorded_argv(record)
    assert argv[argv.index("--resume") + 1] == SID
    assert "--session-id" not in argv
    assert argv[-1].endswith("\n\nhi")
    assert (record / "cwd").read_text() == str(born)
    assert not (tmp_path / "agents" / "sessions").exists()


def test_ca62_reads_the_launchs_own_home_not_the_processs(tmp_path):
    """In-process, the environ passed in is the launch's: its HOME, not
    this process's, is where the transcript is looked for."""
    born = tmp_path / "born"
    born.mkdir()
    home = home_of(tmp_path)
    write_transcript(Path(home["HOME"]), SID, str(born))
    plan = plan_for(tmp_path, EFFORT, argv_tail=["--resume", SID], extra_env=home)
    assert plan.work_dir == str(born)
    assert f"cd {born} || exit 1" in plan.script


def test_a_live_resume_plans_its_files_like_a_fresh_launch(tmp_path):
    """The carried agent definition (the plugin dir) and, over the limit,
    the first message's 0600 file: execute writes what the plan names."""
    plan = plan_for(tmp_path, EFFORT, argv_tail=["--resume", SID],
                    flags=["--first-message-file-over", "0"], extra_env=home_of(tmp_path))
    kinds = sorted(type(item).__name__ for item in plan.files)
    # beside the settings door's file (the launcher's tui default)
    assert kinds == sorted([PlannedPlugin.__name__, PlannedFile.__name__, PlannedFile.__name__])
    message = next(item for item in plan.files if isinstance(item, PlannedFile)
                   and "first-message" in os.path.basename(item.path))
    assert message.text.startswith("Session restarted. Current date and time: ")
