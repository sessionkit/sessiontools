"""The plan: resolve, mint, assemble - and the launch script it carries.
Ported from the old suite's prepare_launch, launch-script, generator-env,
directory and slice-1b (tm spawn) tests."""

from __future__ import annotations

import os
import shlex
import subprocess
from types import SimpleNamespace

import pytest
from start_support import (
    STUB_PUB,
    STUB_SID,
    make_agents_root,
    plan_for,
    write_key_init_stub,
)

from sessiontools import claude_code
from sessiontools.start import plan as pl
from sessiontools.start.args import parse_args
from sessiontools.start.environment import (
    SESSION_IDENTITY_VARS,
    render_identity_unset,
    without_session_identity,
)
from sessiontools.start.errors import EXIT_USAGE, LaunchError
from sessiontools.start.identity import SessionIdentity
from sessiontools.start.policy import COMMITTER_KEEP, COMMITTER_UNSET, CommitterIdentity

# ---------- prepare ------------------------------------------------------------------


def test_prepare_assembles_a_direct_plan(tmp_path):
    plan = plan_for(tmp_path, "name: scout\nclaude:\n  effort: high\n",
                    argv_tail=["review", "the", "code"])
    assert plan.vendor == "claude" and not plan.dry_run
    assert plan.argv[:3] == ["claude", "--session-id", plan.identity.session_id]
    at = plan.argv.index("--effort")
    assert plan.argv[at:at + 2] == ["--effort", "high"]
    assert plan.argv[-1] == plan.injected.initial
    assert "review the code" in plan.injected.initial
    assert "exec claude" in plan.script
    assert f"cd {shlex.quote(plan.work_dir)} || exit 1" in plan.script
    # the live mint made the session directory and its anchor, as before
    assert os.path.isfile(os.path.join(plan.identity.session_dir, "session-id"))


def test_a_live_plan_names_its_files_and_has_written_none(tmp_path):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n      - echo hi\n")
    kinds = sorted(type(item).__name__ for item in plan.files)
    assert kinds == ["PlannedFile", "PlannedPlugin"]
    for item in plan.files:
        assert item.path in plan.argv
        assert os.path.dirname(item.path) == os.path.join(plan.identity.session_dir, "launch")   # 4.6
        assert not os.path.exists(item.path)


def test_a_dry_run_plan_acts_on_nothing(tmp_path):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n      - echo hi\n",
                    argv_tail=["w" * 10], flags=["--dry-run", "--first-message-file-over", "4"])
    assert plan.dry_run and plan.files == ()
    assert not plan.identity.minted
    assert not (tmp_path / "agents" / "sessions").exists()
    assert os.path.join(plan.identity.session_dir, "launch", "system-prompt.md") in plan.argv
    assert os.path.join(plan.identity.session_dir, "launch", "agent-plugin") in plan.argv
    assert os.path.join(plan.identity.session_dir, "launch", "first-message.md") in plan.argv[-1]


def test_prepare_keyed_by_front_matter(tmp_path):
    """`start: keyed: true` alone routes through the primitive."""
    stub, record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, "start:\n  keyed: true\n", extra_env={"SESSION_KEY_INIT": stub})
    assert plan.identity.keyed and plan.identity.session_id == STUB_SID
    assert os.path.exists(record)
    assert "export CLAUDE_SESSION_AGENT_SOCK=" in plan.script


def test_a_keyed_dry_run_projects_and_names_the_mint(tmp_path):
    stub, record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, "start:\n  keyed: true\n", flags=["--dry-run"],
                    extra_env={"SESSION_KEY_INIT": stub})
    assert not os.path.exists(record)
    assert plan.identity.session_id == "<keyed-session-id-minted-at-launch>"
    assert plan.identity.mint_command[0] == stub


@pytest.mark.parametrize("front_matter, words", [
    ("start:\n  keyd: true\n", "keyd"),
    ("start:\n  vendor: acme\n", "acme"),
    ("claude:\n  --effort: high\n", "must not start with '-'"),
])
def test_front_matter_errors_are_usage_errors(tmp_path, front_matter, words):
    with pytest.raises(LaunchError) as excinfo:
        plan_for(tmp_path, front_matter)
    assert excinfo.value.code == EXIT_USAGE
    assert words in str(excinfo.value)


def test_the_double_door_is_a_usage_error_at_the_plan_level(tmp_path):
    with pytest.raises(LaunchError) as excinfo:
        plan_for(tmp_path, "claude:\n  append-system-prompt-file: /tmp/mine.md\n"
                           "start:\n  context:\n    system-prompt:\n      - printf 'X\\n'\n")
    assert excinfo.value.code == EXIT_USAGE
    assert "--append-system-prompt-file" in str(excinfo.value)
    assert "scout.md" in str(excinfo.value)


# ---------- the keyed rules and slice 1b -------------------------------------------------

def test_key_label_without_a_key_refuses(tmp_path):
    with pytest.raises(LaunchError) as excinfo:
        plan_for(tmp_path, "", flags=["--dry-run", "--key-label", "voicemode+super"])
    assert excinfo.value.code == EXIT_USAGE
    assert "mints no key" in str(excinfo.value)


def test_keyed_with_no_agent_refuses_before_any_side_effect(tmp_path):
    root = make_agents_root(tmp_path)
    with pytest.raises(LaunchError) as excinfo:
        pl.prepare(parse_args(["--keyed"]), environ={"AGENTS_DIR": root})
    assert "--keyed needs an agent" in str(excinfo.value)
    assert not (tmp_path / "agents" / "sessions").exists()


def test_first_message_file_over_cli_zero_always_files(tmp_path):
    plan = plan_for(tmp_path, "", argv_tail=["hello"],
                    flags=["--dry-run", "--first-message-file-over", "0"])
    assert "materialised at" in plan.argv[-1] and "first-message.md" in plan.argv[-1]


def test_a_negative_first_message_limit_is_refused(tmp_path):
    with pytest.raises(LaunchError) as excinfo:
        plan_for(tmp_path, "", flags=["--first-message-file-over", "-1"])
    assert "non-negative" in str(excinfo.value)


def test_the_cli_limit_beats_the_agent_files(tmp_path):
    plan = plan_for(tmp_path, "start:\n  first-message-file-over: 0\n",
                    argv_tail=["hello"],
                    flags=["--dry-run", "--first-message-file-over", "1000"])
    assert plan.argv[-1] == "hello\n"


def test_tm_spawn_shape_keyed_label_pointer_and_shortsid(tmp_path):
    """taskmaster's spawn: --keyed --key-label <address>
    --first-message-file-over 0 <role> <brief> -n <seat>+{shortsid},
    with SESSIONS_DIR keeping sessions out of its checkout."""
    stub, _record = write_key_init_stub(tmp_path)
    sessions = tmp_path / "tm-sessions"
    plan = plan_for(tmp_path, "", argv_tail=["brief: probe", "--dangerously-skip-permissions",
                                             "-n", "voicemode+super.{shortsid}"],
                    flags=["--dry-run", "--keyed", "--key-label", "voicemode+super",
                           "--first-message-file-over", "0"],
                    extra_env={"SESSION_KEY_INIT": stub, "SESSIONS_DIR": str(sessions)})
    mint = plan.identity.mint_command
    assert mint[mint.index("--agent-name") + 1] == "voicemode+super"
    assert mint[mint.index("--session-dir") + 1] == str(sessions)
    assert plan.argv[plan.argv.index("-n") + 1] == "voicemode+super.<shortsid>"
    sources = dict(plan.provenance)
    assert "{shortsid} filled from the session identity" in sources["voicemode+super.<shortsid>"]
    assert "materialised at " + str(sessions) in plan.argv[-1]


def test_an_unkeyed_launch_fills_shortsid_from_the_minted_uuid(tmp_path):
    plan = plan_for(tmp_path, "", argv_tail=["-n", "scout+{shortsid}"], flags=["--dry-run"])
    assert plan.argv[plan.argv.index("-n") + 1] == "scout+" + plan.identity.session_id[:8]
    assert f"-n scout+{plan.identity.session_id[:8]}" in plan.script


# ---------- secrets: a dry run names them and resolves none ------------------------------

SECRETS = "environment:\n  secrets:\n    DEMO_TOKEN: pass:demo/token\n"


def test_a_dry_run_names_declared_secrets_and_resolves_none(tmp_path):
    plan = plan_for(tmp_path, SECRETS, flags=["--dry-run"])
    assert [(s.name, s.backend, s.ref) for s in plan.secrets] == [
        ("DEMO_TOKEN", "pass", "demo/token")]
    assert "trap __ss_shred EXIT HUP INT TERM" in plan.script
    assert os.path.join(plan.identity.session_dir, "launch", "secrets.env") in plan.script


# ---------- what a context generator sees ------------------------------------------------

def test_declared_variables_reach_the_context_generators(tmp_path):
    plan = plan_for(tmp_path, "environment:\n  variables:\n    ROLE: worker\n"
                              "start:\n  context:\n    system-prompt:\n"
                              "      - echo role=[$ROLE]\n", flags=["--dry-run"])
    assert "role=[worker]" in plan.injected.system_prompt
    assert "export ROLE=worker" in plan.script


def test_generators_see_the_agent_named_and_the_identity(tmp_path):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n"
                              "      - echo \"[$CLAUDE_CODE_AGENT] [$CLAUDE_SESSION_ID]\"\n",
                    flags=["--dry-run"], extra_env={"CLAUDE_CODE_AGENT": "somebody-else"})
    assert f"[scout] [{plan.identity.session_id}]" in plan.injected.system_prompt


def test_a_plain_session_drops_an_inherited_claude_code_agent(tmp_path):
    launch = pl.resolve(parse_args(["--dry-run"]),
                        environ={"AGENTS_DIR": str(tmp_path), "CLAUDE_CODE_AGENT": "x"},
                        work_dir=str(tmp_path))
    identity = pl.mint(launch)
    assert "CLAUDE_CODE_AGENT" not in pl.generator_environ(launch, identity)


# ST-113: a start from inside a live seat (the caller holds the PARENT's
# session identity) must hand its generators the NEW session's, never the
# parent's: the resolver reads CLAUDE_CODE_SESSION_ID first.
PARENT = "11111111-2222-4333-8444-555555555555"
PARENT_IDENTITY = {"CLAUDE_CODE_SESSION_ID": PARENT, "CLAUDE_SESSION_ID": PARENT,
                   "CLAUDE_SESSION_AGENT_SOCK": "/parent/agent.sock",
                   "CLAUDE_SESSION_AGENT_PID": "4242",
                   "CLAUDE_SESSION_PUB": "/parent/key.pub"}


def _launch_inside_a_seat(tmp_path, front_matter=None):
    environ = {"AGENTS_DIR": str(tmp_path), **PARENT_IDENTITY}
    if front_matter is not None:
        environ["AGENTS_DIR"] = make_agents_root(tmp_path, front_matter=front_matter)
    argv = ["--dry-run"] + (["scout"] if front_matter is not None else [])
    return pl.resolve(parse_args(argv), environ=environ, work_dir=str(tmp_path))


def test_generators_resolve_the_new_session_id_never_the_parents(tmp_path):
    launch = _launch_inside_a_seat(tmp_path)
    identity = pl.mint(launch)
    env = pl.generator_environ(launch, identity)
    assert identity.session_id != PARENT
    assert claude_code.session_id(env) == identity.session_id
    assert env["CLAUDE_SESSION_ID"] == identity.session_id
    for name in SESSION_IDENTITY_VARS:
        if name != "CLAUDE_SESSION_ID":
            assert name not in env, name


def test_a_pane_minted_launch_hands_its_generators_no_session_id(tmp_path):
    launch = _launch_inside_a_seat(tmp_path)
    env = pl.generator_environ(launch, None)   # the pane mints later
    assert claude_code.session_id(env) is None
    assert not set(SESSION_IDENTITY_VARS) & set(env)


def test_a_declared_variable_still_wins_over_the_scrub(tmp_path):
    launch = _launch_inside_a_seat(
        tmp_path, "environment:\n  variables:\n    CLAUDE_SESSION_PUB: /mine.pub\n")
    env = pl.generator_environ(launch, pl.mint(launch))
    assert env["CLAUDE_SESSION_PUB"] == "/mine.pub"
    assert "CLAUDE_SESSION_AGENT_SOCK" not in env


# ST-118: a KEYED seat also holds session-key-init's git signing block; the
# generators drop the parent's and, on a launcher-side keyed mint, get the
# child's own. GIT_CONFIG_GLOBAL is the agent's override file, never the
# session's identity, and stays.
PARENT_SIGNING = {"GIT_CONFIG_COUNT": "2",
                  "GIT_CONFIG_KEY_0": "user.signingkey",
                  "GIT_CONFIG_VALUE_0": "key::ssh-ed25519 AAAAparent parent",
                  "GIT_CONFIG_KEY_1": "gpg.ssh.program",
                  "GIT_CONFIG_VALUE_1": "/parent/ssh-keygen-session"}
NOT_THE_SESSIONS = {"GIT_CONFIG_GLOBAL": "/agent/gitconfig", "GIT_CONFIG_SYSTEM": "/etc/x",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_PARAMETERS": "'a.b'='c'"}


def test_generators_inside_a_keyed_seat_never_get_the_parents_signing_block(tmp_path):
    launch = pl.resolve(parse_args(["--dry-run"]),
                        environ={"AGENTS_DIR": str(tmp_path), **PARENT_IDENTITY,
                                 **PARENT_SIGNING, **NOT_THE_SESSIONS},
                        work_dir=str(tmp_path))
    for identity in (pl.mint(launch), None):     # launcher-minted, pane-minted
        env = pl.generator_environ(launch, identity)
        assert not set(PARENT_SIGNING) & set(env)
        assert {name: env.get(name) for name in NOT_THE_SESSIONS} == NOT_THE_SESSIONS


def test_a_keyed_launcher_side_mint_hands_its_generators_the_childs_own_block(tmp_path):
    stub, _record = write_key_init_stub(tmp_path, signing_block=True)
    root = make_agents_root(tmp_path)
    launch = pl.resolve(parse_args(["--keyed", "scout"]),
                        environ={"AGENTS_DIR": root, "SESSION_KEY_INIT": stub,
                                 "PATH": os.environ["PATH"], **PARENT_IDENTITY,
                                 **PARENT_SIGNING, "GIT_CONFIG_KEY_5": "parent.extra"},
                        work_dir=str(tmp_path))
    identity = pl.mint(launch)
    env = pl.generator_environ(launch, identity)
    assert identity.session_id == STUB_SID
    assert env["GIT_CONFIG_COUNT"] == "6"
    assert env["GIT_CONFIG_VALUE_1"] == f"key::{STUB_PUB}"
    assert env["GIT_CONFIG_VALUE_4"] == "/child/ssh-keygen-session"
    assert env["GIT_CONFIG_KEY_5"] == "gpg.ssh.allowedSignersFile"   # the child's, not parent.extra
    assert env["CLAUDE_SESSION_AGENT_SOCK"] == "/tmp/stub-agent.sock"


def test_an_agent_that_declares_indexed_git_config_still_gets_it(tmp_path):
    """The scrub runs BEFORE `variables:`: an agent that wants git env
    config says so in its own file, and wins over the caller's."""
    launch = pl.resolve(
        parse_args(["--dry-run", "scout"]),
        environ={"AGENTS_DIR": make_agents_root(
                     tmp_path, front_matter="environment:\n  variables:\n"
                     "    GIT_CONFIG_COUNT: '1'\n    GIT_CONFIG_KEY_0: core.pager\n"
                     "    GIT_CONFIG_VALUE_0: cat\n"),
                 **PARENT_SIGNING},
        work_dir=str(tmp_path))
    env = pl.generator_environ(launch, pl.mint(launch))
    assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) \
        == ("1", "core.pager", "cat")
    assert "GIT_CONFIG_KEY_1" not in env


def test_the_scrub_takes_the_indexed_family_and_none_of_gits_other_names():
    env = {**PARENT_SIGNING, **NOT_THE_SESSIONS, "GIT_CONFIG_KEY_17": "x",
           "GIT_CONFIG_VALUE_17": "y", "GIT_DIR": "/g", "PATH": "/bin"}
    assert without_session_identity(env) == {**NOT_THE_SESSIONS, "GIT_DIR": "/g",
                                             "PATH": "/bin"}


def test_the_scripts_unset_line_drops_exactly_what_the_python_scrub_drops(tmp_path):
    """The two halves of the ONE definition agree, measured in bash itself:
    the rendered line, run over an env holding a parent block and git's
    other GIT_CONFIG_* names, leaves exactly `without_session_identity`."""
    caller = {"PATH": os.environ["PATH"], **PARENT_IDENTITY, **PARENT_SIGNING,
              **NOT_THE_SESSIONS, "GIT_CONFIG_KEY_17": "x", "GIT_CONFIG_VALUE_17": "y"}
    script = render_identity_unset() + "\nset -u\nenv -0\n"
    out = subprocess.run(["bash", "-c", script], env=caller, capture_output=True,
                         check=True).stdout.decode()
    left = dict(item.split("=", 1) for item in out.split("\0") if "=" in item)
    left = {name: value for name, value in left.items()
            if name in caller}                   # bash adds PWD, SHLVL, _
    assert left == without_session_identity(caller)


def test_a_real_generator_inside_a_seat_sees_the_new_id(tmp_path):
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n"
                              "      - echo \"[${CLAUDE_CODE_SESSION_ID:-none}]"
                              " [$CLAUDE_SESSION_ID] [${CLAUDE_SESSION_PUB:-none}]\"\n",
                    flags=["--dry-run"], extra_env=PARENT_IDENTITY)
    assert (f"[none] [{plan.identity.session_id}] [none]"
            in plan.injected.system_prompt)
    assert PARENT not in plan.injected.system_prompt


def test_the_launch_says_one_thing_about_voice(tmp_path):
    declares = "start:\n  context:\n    system-prompt:\n      - launch-info\n"
    speaking = plan_for(tmp_path, declares, flags=["--dry-run", "--converse"])
    assert "**Voice**: ACTIVE" in speaking.injected.system_prompt
    assert "converse tool" in speaking.injected.initial
    typing = plan_for(tmp_path, declares, flags=["--dry-run"], work="w2")
    assert "**Voice**: inactive" in typing.injected.system_prompt
    assert "converse" not in typing.injected.initial


# ---------- where the session runs -------------------------------------------------------

def test_agent_file_directory_is_where_the_session_runs(tmp_path):
    home = tmp_path / "scout-home"
    home.mkdir()
    plan = plan_for(tmp_path, f"start:\n  directory: {home}\n", flags=["--dry-run"])
    assert (plan.work_dir, plan.work_dir_source) == (str(home), "agent file: start: directory:")
    assert f"cd {shlex.quote(str(home))}" in plan.script


def test_directory_flag_beats_the_agent_file(tmp_path):
    home = tmp_path / "scout-home"
    home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    plan = plan_for(tmp_path, f"start:\n  directory: {home}\n",
                    flags=["--dry-run", "--directory", str(proj)])
    assert (plan.work_dir, plan.work_dir_source) == (str(proj), "--directory")


def test_no_directory_anywhere_means_where_it_was_typed(tmp_path):
    plan = plan_for(tmp_path, "name: scout\n", flags=["--dry-run"])
    assert (plan.work_dir, plan.work_dir_source) == (str(tmp_path / "work"), "cwd")


@pytest.mark.parametrize("where", ["file", "flag"])
def test_a_directory_that_does_not_exist_is_refused_not_replaced(tmp_path, where):
    nowhere = str(tmp_path / "nowhere")
    front, flags = ((f"start:\n  directory: {nowhere}\n", []) if where == "file"
                    else ("", ["--directory", nowhere]))
    with pytest.raises(LaunchError) as caught:
        plan_for(tmp_path, front, flags=flags)
    assert "not a directory" in str(caught.value) and "refusing" in str(caught.value)
    assert caught.value.code == EXIT_USAGE
    assert not (tmp_path / "agents" / "sessions").exists()


# ---------- the launch script ------------------------------------------------------------

def test_launch_script_order_is_shed_identity_git24_cd_prelude_exec():
    identity = SessionIdentity("sid-1", "/tmp/sid-1", False, {"CLAUDE_SESSION_ID": "sid-1"})
    lines = pl.render_launch_script(
        ["vendor", "--flag"], "/work dir", identity=identity,
        committer=CommitterIdentity("override", "A Human", "h@example.test"),
        env_prelude="# prelude marker",
        vendor_env={"CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false"}).splitlines()
    def at(prefix):
        return next(i for i, line in enumerate(lines) if line.startswith(prefix))
    unset = render_identity_unset()
    assert (lines.index(unset) < at("export CLAUDE_SESSION_ID=")
            < at("export GIT_COMMITTER_NAME=") < lines.index("unset CLAUDE_CODE")
            < at("export CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION") < at("cd ")
            < lines.index("# prelude marker") < at("exec "))
    # ST-113: the unset line is rendered from the ONE tuple, so the next
    # name added to the family cannot be left out of the script
    assert set(SESSION_IDENTITY_VARS) <= set(unset.split()[1:])
    assert "CLAUDE_CODE_SESSION_ID" in SESSION_IDENTITY_VARS
    # ST-118: and the signing block, the open indexed family by prefix
    assert {"GIT_CONFIG_COUNT", "${!GIT_CONFIG_KEY_@}", "${!GIT_CONFIG_VALUE_@}"} \
        <= set(unset.split()[1:])
    assert lines[at("cd ")] == "cd '/work dir' || exit 1"
    assert lines[-1] == "exec vendor --flag"


def test_launch_script_git24_env_output_override_unset_keep():
    over = pl.render_launch_script(["v"], "/w", committer=CommitterIdentity(
        "override", "Ann O'Human", "a@ex.test"))
    assert "export GIT_COMMITTER_NAME='Ann O'\"'\"'Human'" in over
    assert "export GIT_COMMITTER_EMAIL=a@ex.test" in over
    assert "unset GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL" in pl.render_launch_script(
        ["v"], "/w", committer=COMMITTER_UNSET)
    assert "GIT_COMMITTER" not in pl.render_launch_script(["v"], "/w", committer=COMMITTER_KEEP)


def test_launch_script_quotes_argv_by_shlex_never_by_hand():
    text = pl.render_launch_script(["vendor", "--msg", "two words; $(rm -rf /)"], "/w")
    assert "exec vendor --msg 'two words; $(rm -rf /)'" in text


def test_launch_script_no_vendor_env_renders_nothing():
    assert "vendor env defaults" not in pl.render_launch_script(["v"], "/w", vendor_env=None)


def test_the_shred_trap_is_armed_before_anything_can_exit():
    text = pl.render_launch_script(["v"], "/w", secret_file="/tmp/secrets.env")
    assert text.index("trap __ss_shred") < text.index("cd /w || exit 1")


# ---------- session-settings 4.3: SESSION_HOME from the `session` tool's cascade -------------

def _agent_config(home, name, front):
    cfg = home / ".session" / "agent" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / f"{name}.md").write_text(f"---\n{front}\n---\n# {name}\n")


def test_home_true_in_the_agent_layer_exports_session_home_and_makes_it(tmp_path):
    # Open 15 (RULED): the agent layer is the file the launch uses.
    home = tmp_path / "home"
    plan = plan_for(tmp_path, "settings:\n  session:\n    home: true\n",
                    extra_env={"HOME": str(home)})
    place = str(home / ".session" / "agent" / "home" / "scout")
    assert plan.home is not None and (plan.home.place, plan.home.layer) == (place, "agent")
    assert f"export SESSION_HOME={place}" in plan.script
    assert 'mkdir -p "$SESSION_HOME"' in plan.script
    assert plan.script.index("export SESSION_HOME") < plan.script.index("exec ")
    assert not any("SESSION_HOME" in t for t in plan.argv if isinstance(t, str))
    assert not os.path.exists(place)                    # planning makes nothing


def test_no_home_by_default_means_no_session_home_line(tmp_path):
    plan = plan_for(tmp_path, "")
    assert plan.home is None
    assert "SESSION_HOME" not in plan.script


def test_a_home_dir_in_the_seat_layer_wins_and_is_expanded(tmp_path):
    home = tmp_path / "home"
    _agent_config(home, "scout", "settings:\n  session:\n    home: true")
    seat = home / ".session" / "seats" / "scout+test" / "settings"
    seat.mkdir(parents=True)
    (seat / "session.json").write_text('{"home_dir": "~/scout-home"}')
    plan = plan_for(tmp_path, "", extra_env={"HOME": str(home)}, flags=["--seat", "scout+test"])
    assert plan.home.layer == "dir"
    assert f"export SESSION_HOME={home / 'scout-home'}" in plan.script


def test_a_refused_settings_file_refuses_the_launch_and_names_it(tmp_path):
    home = tmp_path / "home"
    seat = home / ".session" / "seats" / "scout+bad" / "settings"
    seat.mkdir(parents=True)
    (seat / "session.json").write_text("{not json")
    with pytest.raises(LaunchError) as excinfo:
        plan_for(tmp_path, "", extra_env={"HOME": str(home)}, flags=["--seat", "scout+bad"])
    assert "session.json" in str(excinfo.value)


# ---------- session-settings 4.4: the variables at birth -----------------------

def test_a_new_session_script_reads_the_variables_at_birth_before_the_record(tmp_path):
    plan = plan_for(tmp_path, "")
    assert plan.record is not None
    (line,) = [x for x in plan.script.splitlines() if "birth_main" in x]
    assert line.endswith(" || :")                      # settings never stop a launch
    assert f"--dir {plan.record.session_dir}" in line
    assert plan.script.index("birth_main") < plan.script.index("# launch record: env")


def test_no_record_means_no_birth_line():
    assert pl.render_birth_call(None) == ""


# ---------- the schema install before the birth --------------------------------

def _install_line(script: str) -> str:
    (line,) = [x for x in script.splitlines() if "schema_install_main" in x]
    return line


def test_the_schema_install_runs_once_guarded_after_the_prelude_and_before_the_birth(tmp_path):
    plan = plan_for(tmp_path, "")
    line = _install_line(plan.script)
    assert line.startswith("if command -v pager >/dev/null 2>&1; then ")
    assert line.endswith(" -- pager settings install </dev/null || :; fi")   # never stops a launch
    script = plan.script
    assert script.index("end declared environment") < script.index("schema_install_main")
    assert script.index("schema_install_main") < script.index("birth_main")
    assert script.index("birth_main") < script.index("# launch record: env")


def test_the_schema_install_and_the_birth_name_the_same_dir_character_for_character():
    pane_minted = '/sessions/"$CLAUDE_SESSION_ID"'      # expands in the pane, in both lines
    record = SimpleNamespace(session_dir=pane_minted)
    text = pl.render_birth_call(record, python="/py")
    install = _install_line(text)
    (birth,) = [x for x in text.splitlines() if "birth_main" in x]
    assert f"--dir {pane_minted} -- " in install
    assert birth.endswith(f"--dir {pane_minted} || :")


def test_a_resume_script_has_no_schema_install_and_no_birth(tmp_path):
    plan = plan_for(tmp_path, "", flags=("--dry-run",), argv_tail=("--resume", "abc"))
    assert plan.resume
    assert "schema_install_main" not in plan.script
    assert "birth_main" not in plan.script


# ---------- Open 15: the agent layer is the file the launch uses (RULED) --------------

def test_the_agent_layer_comes_from_the_launched_file_not_agent_config(tmp_path):
    home = tmp_path / "home"
    _agent_config(home, "scout", "settings:\n  session:\n    home: true")   # not launched
    plan = plan_for(tmp_path, "", extra_env={"HOME": str(home)})              # AGENTS_DIR's scout
    assert plan.home is None


def test_the_birth_records_the_launched_agent_file():
    from sessiontools.start.agents import Agent
    from sessiontools.start.identity import birth_settings
    agent = Agent(name="scout", file="/plugins/agents/scout.md", directory="", home="")
    assert birth_settings(agent, "scout+x") == {
        "agent": "scout", "agent_file": "/plugins/agents/scout.md", "seat": "scout+x"}


def test_the_birth_records_the_real_file_not_a_link(tmp_path):
    from sessiontools.start.agents import Agent
    from sessiontools.start.identity import birth_settings
    real = tmp_path / "agent" / "config" / "scout.md"
    real.parent.mkdir(parents=True)
    real.write_text("You are.\n")
    link = tmp_path / "agents" / "scout.md"
    link.parent.mkdir()
    link.symlink_to(real)
    agent = Agent(name="scout", file=str(link), directory="", home="")
    assert birth_settings(agent)["agent_file"] == os.path.realpath(real)
