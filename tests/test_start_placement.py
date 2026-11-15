"""Tmux placement: `--in-tmux` / `--tmux-target`, the keyed mint inside the
pane's own `session start`, and the handover at `new-window`. Ported from the old
suite's "Tmux placement + keyed-in-pane" section (CA-209 do-006, old tests
3232-4050), its tmux dry-run and golden (4586, 4748), the keyed-tmux dry run
(4972), the shred-before-mint pin (5073) and the Fable refusal on the tmux
side (5808). Old tests on declared SECRETS are ported onto the files the
port writes for every launch (prompt file, plugin dir); the secret file
itself is pinned in test_start_secrets.py (slice 4).

The e2e tests run a PRIVATE tmux server. `conftest.py` walls every test off
(its own `TMUX_TMPDIR`, no `TMUX`, an empty `AGENTS_DIR`), and `private_env`
refuses to start anything if that wall is missing: on 2030-12-08 an
`--in-tmux` test that inherited the developer's `$TMUX` placed two live
keyed agents in the real server. The stub vendor records from INSIDE the
pane - the only place the venv scrub, the identity env and the reap wiring
can be verified honestly.
"""

from __future__ import annotations

import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from start_support import (
    FIXTURES,
    STUB_SID,
    TIMEOUT_S,
    VENDOR_STUB,
    clean_env,
    make_agents_root,
    plan_for,
    recorded_argv,
    refuse_unwalled_path,
    session_cmd,
    walled_bin,
    write_key_init_stub,
)
from test_start_run import (
    GOLDEN_FRONT_MATTER,
    HERMETIC_GIT,
    PARENT_PUB,
    PARENT_SIGNING,
    SIGNING_KEYS,
    dry_run_main,
    normalize_golden,
)

from sessiontools import claude_code, start
from sessiontools.start import pane, placement
from sessiontools.start.environment import (
    SESSION_IDENTITY_VARS,
    render_identity_unset,
)
from sessiontools.start.errors import EXIT_OK, EXIT_USAGE, LaunchError
from sessiontools.start.identity import SessionIdentity, expand_shortsid
from sessiontools.start.plan import render_launch_script

REAL_KEY_INIT = os.path.expanduser("~/.claude/agents/lib/session-keys/session-key-init")
KEYED = "start:\n  keyed: true\n"
WITH_CONTEXT = "start:\n  context:\n    system-prompt:\n      - \"echo placed prompt\"\n"


def wait_for(predicate, timeout=30, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------- units: targets and the switch doctrine -----------------------------------------

def test_parse_tmux_target_forms():
    assert placement.parse_tmux_target(None) == ("", "")
    assert placement.parse_tmux_target("") == ("", "")
    assert placement.parse_tmux_target("eng") == ("eng", "")
    assert placement.parse_tmux_target("eng:win") == ("eng", "win")
    assert placement.parse_tmux_target(":win") == ("", "win")
    assert placement.parse_tmux_target("eng:a:b") == ("eng", "a:b")


def test_should_switch_doctrine():
    """TM-2414 at launcher scale: conditioned on the CALLER. An interactive
    human inside tmux keeps the old auto-switch; an agent (no tty) never
    moves anyone's view; explicit flags override both ways."""
    inside = {"TMUX": "/x/y,1,2"}
    assert placement.should_switch(False, False, inside, interactive=True) is True
    assert placement.should_switch(False, False, inside, interactive=False) is False
    assert placement.should_switch(False, False, {}, interactive=True) is False
    assert placement.should_switch(True, False, {}, interactive=False) is True
    assert placement.should_switch(False, True, inside, interactive=True) is False
    with pytest.raises(LaunchError) as refused:
        placement.should_switch(True, True, {}, interactive=True)
    assert refused.value.code == EXIT_USAGE
    assert "pick one" in str(refused.value)


def test_default_session_fills_in_a_name_only_outside_tmux():
    """No target typed and no tmux around it: the default session, one
    fixed name. A typed SESSION always wins; inside tmux an empty one stays
    empty (the caller's own session)."""
    assert placement.DEFAULT_TMUX_SESSION == "agents"
    assert placement.default_session("", environ={}) == "agents"
    assert placement.default_session("eng", environ={}) == "eng"
    assert placement.default_session("", environ={"TMUX": "/x,1,2"}) == ""
    assert placement.default_session("eng", environ={"TMUX": "/x,1,2"}) == "eng"


def test_check_placeable_passes_whatever_default_session_returns():
    """The library guard stays for a caller that skips `default_session`; a
    caller that asks it first is never refused."""
    placement.check_placeable("eng", environ={})
    placement.check_placeable("", environ={"TMUX": "/x,1,2"})
    placement.check_placeable(placement.default_session("", environ={}), environ={})
    with pytest.raises(LaunchError) as refused:
        placement.check_placeable("", environ={})
    assert refused.value.code == EXIT_USAGE
    assert "nowhere to place the window" in str(refused.value)


# ---------- units: the launch script text -------------------------------------------------------

def test_launch_script_sheds_the_launchers_identity_first():
    """First thing after the header: the launched session must never inherit
    the LAUNCHER's session identity (the friendly-fire bug - see the canary
    test below)."""
    identity = SessionIdentity("sid-1", "/d", False, {"CLAUDE_SESSION_ID": "sid-1"})
    lines = render_launch_script(["v"], "/w", identity=identity).splitlines()
    shed = lines.index(render_identity_unset())
    # ST-113: the vendor's name too - a start from inside a seat handed
    # the parent's CLAUDE_CODE_SESSION_ID straight through to the agent
    assert {"CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_AGENT_PID"} <= set(lines[shed].split())
    first_use = next(i for i, line in enumerate(lines)
                     if line.startswith("export CLAUDE_SESSION_ID"))
    assert shed < first_use


def test_shortsid_with_no_identity_is_left_alone():
    """Nothing to fill it from (an unkeyed resume): the token passes through."""
    assert expand_shortsid("seat+{shortsid}", None) == "seat+{shortsid}"


# ---------- units: a tmux launch plans IN PLACE (ST-285) ---------------------------------------

def keyed_tmux_plan(tmp_path, *flags, front_matter=KEYED, stub=None):
    if stub is None:
        stub, _record = write_key_init_stub(tmp_path)
    return plan_for(tmp_path, front_matter, flags=("--in-tmux", *flags),
                    extra_env={"SESSION_KEY_INIT": stub})


def test_a_keyed_tmux_plan_is_an_in_place_plan(tmp_path):
    """No trampoline: a placed pane runs `session start` again, in place, so
    the plan of a tmux launch IS the direct plan. Its keyed mint is the
    direct one, reaped by the pid that execs the vendor."""
    plan = keyed_tmux_plan(tmp_path)
    assert plan.identity is not None
    assert not plan.script.startswith("#!")
    assert "\nexec claude" in plan.script


def test_an_unkeyed_tmux_plan_mints_launcher_side(tmp_path):
    plan = plan_for(tmp_path, "", flags=("--in-tmux",))
    assert plan.identity is not None and os.path.isdir(plan.identity.session_dir)
    assert not plan.script.startswith("#!")
    assert "\nexec claude" in plan.script


def test_a_direct_plan_has_no_shebang():
    """The shebang marked the old trampoline; the direct path runs `bash -c`."""
    assert not render_launch_script(["v"], "/w").startswith("#!")


def test_keyed_tmux_missing_primitive_is_loud_at_prepare_time(tmp_path):
    with pytest.raises(LaunchError) as refused:
        keyed_tmux_plan(tmp_path, stub=str(tmp_path / "absent"))
    assert "SESSION_KEY_INIT" in str(refused.value)


def test_door_guard_still_fires_on_a_keyed_tmux_session_id(tmp_path):
    """A declared `claude: session-id:` still collides with the door the
    launcher computed."""
    with pytest.raises(LaunchError) as refused:
        keyed_tmux_plan(tmp_path, front_matter=KEYED + "claude:\n  session-id: mine\n")
    assert refused.value.code == EXIT_USAGE
    assert "--session-id" in str(refused.value)


# ---------- place_pane against a fake tmux: the handover ---------------------------------------

def no_tmux(*_args, **_kwargs):
    pytest.fail("this placement must never reach tmux")


def a_message_file(tmp_path) -> str:
    """A message file as `pane.write_message_file` leaves it."""
    return pane.write_message_file("review the auth", "t0k",
                                   {"SESSION_STATE_DIR": str(tmp_path / "state")})


class FakeTmux:
    """`placement._tmux.run` for the ONE placement call, and `run_tmux` for
    the rest: records both, answers `new-window` with PRINTED."""

    def __init__(self, monkeypatch, printed="@9 %3 4242\n", returncode=0, stderr="",
                 fail=()):
        self.calls: list[list[str]] = []
        self.envs: dict = {}
        self.printed, self.returncode, self.stderr, self.fail = printed, returncode, stderr, fail
        monkeypatch.setattr(placement._tmux, "run", self.sequence)
        monkeypatch.setattr(placement, "run_tmux", self.single)

    def sequence(self, arguments, env=None):
        self.calls.append(list(arguments))
        self.envs[arguments[0]] = env
        return subprocess.CompletedProcess(arguments, self.returncode, self.printed, self.stderr)

    def single(self, arguments, environ=None):
        self.calls.append(list(arguments))
        self.envs[arguments[0]] = environ
        if arguments[0] in self.fail:
            raise LaunchError(f"tmux {arguments[0]} failed: nope")
        return ""


def place(tmp_path, session="eng", switch=False, environ=None, message_file=None,
          handoff=None, cwd=None):
    return placement.place_pane(
        "session start scout", cwd or str(tmp_path), session, "scout", "t0k", switch,
        {"SESSION_START_ENTERED": "session start --in-tmux scout"} if handoff is None
        else handoff, message_file=message_file, environ={} if environ is None else environ)


def test_place_pane_keeps_its_own_placement_guard(tmp_path, monkeypatch):
    """`main` asks first, but place_pane is public and must still refuse a
    placement it cannot make, taking the message file with it."""
    message = a_message_file(tmp_path)
    monkeypatch.setattr(placement, "run_tmux", no_tmux)
    monkeypatch.setattr(placement._tmux, "run", no_tmux)
    with pytest.raises(LaunchError) as refused:
        place(tmp_path, session="", message_file=message)
    assert refused.value.code == EXIT_USAGE
    assert not os.path.exists(os.path.dirname(message))


def test_a_failure_before_placement_takes_the_message_file(tmp_path, monkeypatch):
    """No `@id` printed: the window never opened, and its file goes."""
    message = a_message_file(tmp_path)
    FakeTmux(monkeypatch, printed="", returncode=1, stderr="no server")
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    with pytest.raises(LaunchError) as refused:
        place(tmp_path, message_file=message)
    assert "tmux new-window failed: no server" in str(refused.value)
    assert not os.path.exists(os.path.dirname(message))


def test_a_failure_after_placement_never_robs_the_live_pane(tmp_path, monkeypatch, capsys):
    """`new-window` is the handover: after it the pane is ALIVE and about to
    read its file. A switch-client failure is reported, and nothing is
    unwound - a window that exists is not a failure."""
    message = a_message_file(tmp_path)
    tmux = FakeTmux(monkeypatch, fail=("set-window-option", "switch-client"))
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    placed = place(tmp_path, switch=True, message_file=message)
    assert placed == placement.TmuxPlacement("eng", "scout", "@9", False, "%3", 4242)
    assert [call[0] for call in tmux.calls] == ["new-window", "set-window-option",
                                                "switch-client"]
    assert os.path.exists(message)
    err = capsys.readouterr().err
    assert "placed, but could not automatic-rename off" in err
    assert "placed, but could not switch the client to it" in err


def test_one_sequence_places_names_and_keeps_a_failed_pane(tmp_path, monkeypatch):
    """ONE tmux call: new-window (anchored, in CWD, the hand-off in its
    env, the line as one argument), then remain-on-exit failed and the real
    name, both by the unique temporary name. After it, the window is its
    `@id` (TM-1658: every `-t` anchored, by `=` or `@`)."""
    tmux = FakeTmux(monkeypatch)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: False)
    work = str(tmp_path / "work dir")
    placed = place(tmp_path, session="alpha", cwd=work,
                   handoff={"SESSION_START_ENTERED": "session start --in-tmux scout",
                            "SESSION_START_ENTERED_DIR": work})
    assert placed.created_session and placed.window_id == "@9"
    assert tmux.calls[0] == ["new-session", "-d", "-s", "alpha"]
    assert tmux.calls[1] == [
        "new-window", "-d", "-P", "-F", placement.NEW_WINDOW_FORMAT, "-c", work,
        "-t", "=alpha:", "-n", "ssp-t0k",
        "-e", "SESSION_START_ENTERED=session start --in-tmux scout",
        "-e", f"SESSION_START_ENTERED_DIR={work}",
        "session start scout",
        ";", "set-option", "-p", "-t", "=alpha:ssp-t0k", "remain-on-exit", "failed",
        ";", "rename-window", "-t", "=alpha:ssp-t0k", "scout"]
    assert tmux.calls[2] == ["set-window-option", "-t", "@9", "automatic-rename", "off"]
    # a CREATED session's window 0 is a bare shell: the agent's window is
    # made current, so an attach lands on it
    assert tmux.calls[3] == ["select-window", "-t", "@9"]
    assert len(tmux.calls) == 4                               # no switch asked for
    for call in tmux.calls:
        for at, word in enumerate(call):
            if word == "-t":
                assert call[at + 1][0] in "=@", call


def test_in_the_current_session_the_sequence_names_no_session(tmp_path, monkeypatch):
    tmux = FakeTmux(monkeypatch)
    monkeypatch.setattr(placement, "session_exists", no_tmux)
    placed = place(tmp_path, session="", environ={"TMUX": "/x,1,2"})
    assert placed.session == "" and not placed.created_session
    sequence = tmux.calls[0]
    assert sequence[0] == "new-window"
    assert sequence[sequence.index("set-option") + 3] == "ssp-t0k"
    assert sequence.count("-t") == 2          # set-option's and rename-window's only


def test_a_tmux_without_remain_on_exit_failed_is_degraded_not_failed(
        tmp_path, monkeypatch, capsys):
    """Measured on tmux 3.8: an invalid value in the sequence still leaves
    the window (its `@id` printed) and exits 1. So the window is placed, the
    line says the pane will not stay, and the name is set by `@id`."""
    message = a_message_file(tmp_path)
    tmux = FakeTmux(monkeypatch, printed="@4 %2 77\n", returncode=1,
                    stderr="unknown value: failed")
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    placed = place(tmp_path, message_file=message)
    assert placed.window_id == "@4" and os.path.exists(message)
    assert tmux.calls[1] == ["rename-window", "-t", "@4", "scout"]
    assert "could not set remain-on-exit failed (unknown value: failed)" in \
        capsys.readouterr().err


def test_a_server_born_by_new_session_never_gets_the_callers_identity(
        tmp_path, monkeypatch):
    """ST-113 (c): `new-session` may birth the server, and a server keeps its
    starter's environment for every pane it makes. Measured in repro-001 S4:
    a server born inside a seat held the PARENT's ids. Only `new-session` is
    scrubbed: `new-window` builds its pane from the server's environment."""
    caller = {"PATH": "/usr/bin", "HOME": "/home/x", "TERM": "xterm",
              "GIT_CONFIG_GLOBAL": "/agent/gitconfig",
              **{name: "parent-" + name for name in SESSION_IDENTITY_VARS},
              # ST-118: and a keyed parent's signing block, the indexed family
              "GIT_CONFIG_KEY_0": "user.signingkey", "GIT_CONFIG_VALUE_0": "key::parent"}
    tmux = FakeTmux(monkeypatch)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: False)
    place(tmp_path, session="alpha", environ=caller)
    born = tmux.envs["new-session"]
    assert not set(SESSION_IDENTITY_VARS) & set(born)
    assert claude_code.session_id(born) is None
    assert born == {"PATH": "/usr/bin", "HOME": "/home/x", "TERM": "xterm",
                    "GIT_CONFIG_GLOBAL": "/agent/gitconfig"}
    # narrow: only new-session is scrubbed; a new pane's env is the server's
    assert tmux.envs["new-window"]["CLAUDE_CODE_SESSION_ID"] == "parent-CLAUDE_CODE_SESSION_ID"
    assert caller["CLAUDE_CODE_SESSION_ID"] == "parent-CLAUDE_CODE_SESSION_ID"


def test_a_reused_session_selects_no_window(tmp_path, monkeypatch):
    """Someone may be looking at a session that already existed: its
    current window is theirs, so placement leaves it alone."""
    tmux = FakeTmux(monkeypatch)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    placed = place(tmp_path, session="agents")
    assert not placed.created_session
    assert [call[0] for call in tmux.calls] == ["new-window", "set-window-option"]


def test_a_failed_select_window_is_degraded_not_failed(tmp_path, monkeypatch, capsys):
    FakeTmux(monkeypatch, fail=("select-window",))
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: False)
    placed = place(tmp_path, session="agents")
    assert placed.created_session and placed.window_id == "@9"
    assert "placed, but could not select the new window" in capsys.readouterr().err


def test_session_exists_asks_has_session_anchored(monkeypatch):
    asked = []

    def fake_run(arguments, env=None):
        asked.append(arguments)
        return subprocess.CompletedProcess(arguments, 1, "", "")

    monkeypatch.setattr(placement._tmux, "run", fake_run)
    assert placement.session_exists("alpha", environ={}) is False
    assert asked == [["has-session", "-t", "=alpha"]]


def test_session_exists_with_no_tmux_installed_is_false(monkeypatch):
    def missing(arguments, env=None):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(placement._tmux, "run", missing)
    assert placement.session_exists("alpha", environ={}) is False


def test_tmux_runs_under_the_host_environ_never_the_venv(tmp_path, monkeypatch):
    """The SERVER may be born from this very call, and a server born in the
    package's venv hands that venv to every pane it ever creates."""
    seen = []

    def fake_run(arguments, env=None):
        seen.append(env)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(placement._tmux, "run", fake_run)
    venv = str(tmp_path / "venv")
    placement.session_exists("x", environ={"VIRTUAL_ENV": venv,
                                           "PATH": f"{venv}/bin:/usr/bin"})
    assert "VIRTUAL_ENV" not in seen[0] and seen[0]["PATH"] == "/usr/bin"


def test_the_receipt_names_who_where_the_window_id_and_that_the_pane_mints():
    """A caller INSIDE tmux. The id is the pane's to mint (ST-285): the
    launcher that places it mints nothing."""
    inside = {"TMUX": "/x,1,2"}
    created = placement.receipt("scout", placement.TmuxPlacement("eng", "scout", "@5", True),
                                environ=inside)
    assert created == ("session start: placed scout in tmux window 'scout' (@5) of "
                       "session 'eng' (created) -- session id minted in the pane")
    here = placement.receipt("scout", placement.TmuxPlacement("", "w", "@6", False),
                             environ=inside)
    assert here.endswith("of the current session -- session id minted in the pane")


def test_a_receipt_outside_tmux_says_how_to_attach():
    """A caller outside tmux is never switched to the window, so the line
    names the session it made (or reused) and how to reach it."""
    created = placement.receipt("scout",
                                placement.TmuxPlacement("agents", "scout", "@5", True),
                                environ={})
    assert created == ("session start: placed scout in tmux window 'scout' (@5) of "
                       "session 'agents' (created) -- session id minted in the pane; "
                       "attach: tmux attach -t agents")
    reused = placement.receipt("scout",
                               placement.TmuxPlacement("agents", "scout", "@6", False),
                               environ={})
    assert "of session 'agents' -- session" in reused
    assert reused.endswith("; attach: tmux attach -t agents")
    odd = placement.receipt("scout",
                            placement.TmuxPlacement("my work", "scout", "@7", False),
                            environ={})
    assert odd.endswith("; attach: tmux attach -t 'my work'")


# ---------- main, in-process: the outer launcher ------------------------------------------------

def outer(tmp_path, monkeypatch, *argv, front_matter="claude:\n  effort: high\n"):
    """`start.main(ARGV)` as the outer launcher, place_pane recorded, never
    run. Returns (exit code, the recorded call or None, the state dir)."""
    state = tmp_path / "state"
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path, front_matter=front_matter))
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "agents" / "sessions"))
    monkeypatch.setenv("SESSION_STATE_DIR", str(state))
    monkeypatch.setenv("TMUX", "/x,1,2")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    monkeypatch.chdir(work)
    monkeypatch.setenv("PWD", str(work))
    calls = []

    def recorded(line, cwd, session, window, token, switch, handoff, message_file=None,
                 environ=None):
        calls.append(SimpleNamespace(line=line, cwd=cwd, session=session, window=window,
                                     token=token, handoff=dict(handoff),
                                     message_file=message_file))
        return placement.TmuxPlacement(session, window, "@7", False, "%9", 4242)

    monkeypatch.setattr(start, "place_pane", recorded)
    code = start.main(list(argv))
    return code, (calls[0] if calls else None), state


def test_the_outer_launcher_mints_nothing_and_hands_off_the_entered_line(
        tmp_path, monkeypatch, capsys):
    code, call, _state = outer(tmp_path, monkeypatch, "--in-tmux", "--no-switch", "scout",
                               "--model", "opus")
    assert code == EXIT_OK, capsys.readouterr().err
    assert call.line == "session start scout --model opus"
    assert call.cwd == str(tmp_path / "work")
    assert call.handoff == {
        "SESSION_START_ENTERED": "session start --in-tmux --no-switch scout --model opus",
        "SESSION_START_ENTERED_DIR": str(tmp_path / "work")}
    assert call.message_file is None
    assert re.fullmatch(r"[0-9a-f]{16}", call.token)
    assert no_session_dirs(tmp_path), "the outer launcher minted"
    assert capsys.readouterr().out.strip().endswith("-- session id minted in the pane")


def test_bare_words_go_to_a_0600_file_never_the_pane_line(tmp_path, monkeypatch, capsys):
    """Ruling 7: `ps` is world-readable and a pane_start_command lives as
    long as its pane. The words move to `<state>/launches/<token>/`, a 0700
    dir and a 0600 file; the line names the file and forces `-over 0`."""
    code, call, state = outer(tmp_path, monkeypatch, "--tmux-target", "eng", "scout",
                              "review", "the", "auth", "--model", "opus")
    assert code == EXIT_OK, capsys.readouterr().err
    path = Path(call.message_file)
    assert path == state / "launches" / call.token / "first-message.md"
    assert path.read_text() == "review the auth"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert call.line == shlex.join(["session", "start", "--first-message-file", str(path),
                                    "--first-message-file-over", "0", "scout",
                                    "--model", "opus"])
    for word in ("review", "auth"):
        assert word not in call.line
    # the hand-off carries the line EXACTLY as entered: the record keeps it
    assert call.handoff["SESSION_START_ENTERED"] == (
        "session start --tmux-target eng scout review the auth --model opus")


def test_a_held_agent_is_refused_at_the_caller_and_nothing_is_written(
        tmp_path, monkeypatch, capsys):
    """The read of the front door refuses a hand placement of a held agent
    (exit 3): no window, no message file."""
    monkeypatch.setattr(start, "front_door", lambda *a, **k: SimpleNamespace(
        unmeasured="", held=True, refusal=lambda: "scout is held by session x"))
    code, call, state = outer(tmp_path, monkeypatch, "--in-tmux", "scout", "hello")
    assert code == 3 and call is None
    assert "scout is held by session x" in capsys.readouterr().err
    assert not (state / "launches").exists()


def test_a_bare_keyed_placement_is_refused_before_anything(tmp_path, monkeypatch, capsys):
    code, call, _state = outer(tmp_path, monkeypatch, "--in-tmux", "--keyed")
    assert code == EXIT_USAGE and call is None
    assert "--keyed needs an agent" in capsys.readouterr().err


def test_a_home_folder_yes_opens_the_window_in_the_work_folder(tmp_path, monkeypatch):
    """The home-folder offer is the OUTER launcher's (ruled 4c): a yes is
    the window's `-c`; the hand-off still names where it was typed."""
    monkeypatch.setattr(start, "_offer", lambda launch: (
        launch._replace(work_dir=str(tmp_path / "elsewhere")), True))
    code, call, _state = outer(tmp_path, monkeypatch, "--in-tmux", "scout")
    assert code == EXIT_OK
    assert call.cwd == str(tmp_path / "elsewhere")
    assert call.handoff["SESSION_START_ENTERED_DIR"] == str(tmp_path / "work")


# ---------- main, in-process: dry runs and the usage refusals ----------------------------------

def test_dry_run_of_a_tmux_launch_shows_the_pane_line(tmp_path, monkeypatch, capsys):
    """The pane's line, then the in-place plan of that line: no trampoline."""
    monkeypatch.setenv("PWD", str(tmp_path))
    assert dry_run_main(tmp_path, monkeypatch, "--tmux-target", "gold:win", "scout",
                        "hello") == EXIT_OK
    out = capsys.readouterr().out
    assert "mode:     tmux window 'win' in session 'gold'\n" in out
    pane_line = next(line for line in out.splitlines() if line.startswith("pane:"))
    assert pane_line.startswith("pane:     (cd ")
    assert "session start --first-message-file " in pane_line
    assert "/launches/<token>/first-message.md" in pane_line
    assert "--first-message-file-over 0 scout" in pane_line
    assert "--dry-run" not in pane_line and "hello" not in pane_line
    assert "#!/usr/bin/env bash" not in out


def test_a_tmux_dry_run_defaults_the_window_to_the_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMUX", "/x,1,2")
    assert dry_run_main(tmp_path, monkeypatch, "--in-tmux", "scout") == EXIT_OK
    assert "mode:     tmux window 'scout' in the current session\n" in capsys.readouterr().out


def test_a_forced_switch_is_said_in_the_dry_run(tmp_path, monkeypatch, capsys):
    assert dry_run_main(tmp_path, monkeypatch, "--tmux-target", "eng", "--switch",
                        "scout") == EXIT_OK
    assert "in session 'eng', then switch-client\n" in capsys.readouterr().out


def test_no_agent_in_tmux_names_the_window_after_the_vendor(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.chdir(tmp_path)
    assert start.main(["--dry-run", "--in-tmux", "--tmux-target", "nosuch-session"]) == EXIT_OK
    assert "tmux window 'claude'" in capsys.readouterr().out


def test_the_tmux_dry_run_touches_no_tmux(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(placement, "run_tmux", no_tmux)
    monkeypatch.setattr(placement._tmux, "run", no_tmux)
    assert dry_run_main(tmp_path, monkeypatch, "--tmux-target", "eng", "scout") == EXIT_OK


def test_in_tmux_outside_tmux_dry_run_says_the_session_it_would_create(
        tmp_path, monkeypatch, capsys):
    """No target and no tmux: the dry run names the default session the
    live launch would create (or reuse), and touches no tmux."""
    monkeypatch.setattr(placement, "run_tmux", no_tmux)
    monkeypatch.setattr(placement._tmux, "run", no_tmux)
    assert dry_run_main(tmp_path, monkeypatch, "--in-tmux", "scout") == EXIT_OK
    captured = capsys.readouterr()
    assert "mode:     tmux window 'scout' in session 'agents'\n" in captured.out
    assert "nowhere" not in captured.err


def test_in_tmux_with_only_a_window_outside_tmux_uses_the_default_session(
        tmp_path, monkeypatch, capsys):
    assert dry_run_main(tmp_path, monkeypatch, "--tmux-target", ":win", "scout") == EXIT_OK
    assert "mode:     tmux window 'win' in session 'agents'\n" in capsys.readouterr().out


def test_contradictory_switch_flags_are_refused_outside_tmux(tmp_path, monkeypatch, capsys):
    """`--switch --no-switch` is wrong wherever it was typed: refused
    outside tmux too, where a bare `--in-tmux` would otherwise place."""
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    assert start.main(["--in-tmux", "--switch", "--no-switch", "scout"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "pick one" in err and "nowhere" not in err


# ---------- the tmux golden ----------------------------------------------------------------

def test_golden_dry_run_tmux(tmp_path):
    """The tmux golden, run by the PORT under the same harness as the direct
    golden. Byte-equal to the committed fixture. It is NO LONGER the old
    launcher's (ST-285): the trampoline is gone, so it shows the pane's
    line and then the in-place plan of that line."""
    root = make_agents_root(tmp_path, front_matter=GOLDEN_FRONT_MATTER)
    for sub in ("bin", "home", "work", "tmpdir"):
        (tmp_path / sub).mkdir()
    env = clean_env(AGENTS_DIR=root, HOME=str(tmp_path / "home"),
                    TMPDIR=str(tmp_path / "tmpdir"), TZ="UTC", PWD=str(tmp_path / "work"),
                    PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    env.pop("SESSIONS_DIR")          # the default root: the sessions leaf (fresh: <HOME>/.sessionkit/tools/state/sessions)
    result = subprocess.run(session_cmd("--dry-run", "--tmux-target", "gold:win", "scout"),
                            capture_output=True, text=True, timeout=TIMEOUT_S,
                            cwd=str(tmp_path / "work"), env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert "using the bridge" not in result.stderr
    actual = normalize_golden(result.stdout, tmp_path)
    assert actual == (FIXTURES / "dry-run-tmux.golden").read_text(encoding="utf-8")
    assert "/Users/" not in actual


def test_a_keyed_tmux_dry_run_subprocess_mints_nothing(tmp_path):
    """Keyed x tmux under --dry-run, through the verb: no pane is placed, so
    neither the sessions root nor anything under it exists, and the
    primitive never ran. The pane would run `--keyed` itself."""
    stub, record = write_key_init_stub(tmp_path)
    root = make_agents_root(tmp_path, front_matter=KEYED)
    (tmp_path / "work").mkdir()
    result = subprocess.run(session_cmd("--dry-run", "--tmux-target", "sess:win", "scout"),
                            capture_output=True, text=True, timeout=TIMEOUT_S,
                            cwd=str(tmp_path / "work"), check=False,
                            env=clean_env(AGENTS_DIR=root, SESSION_KEY_INIT=stub,
                                          SESSIONS_DIR=os.path.join(root, "sessions")))
    assert result.returncode == 0, result.stderr
    sessions = tmp_path / "agents" / "sessions"
    assert not sessions.exists(), "the sessions root was created"
    assert not os.path.exists(record), "the primitive ran"
    assert "pane:     (cd " in result.stdout and "session start scout\n" in result.stdout
    assert "--no-register" not in result.stdout


# ---------- e2e: placement on a private server -------------------------------------------------

#: How a pane stub stays alive after `done`: until the test touches
#: `release` (or conftest's kill-server ends the pane), never for a fixed
#: span. A stub that slept 45 s and exited let a slow test read a pane that
#: had already closed (`ps` saw nothing, the window was gone). The 600 s
#: bound is an orphan bound only, so a stub cannot outlive a crashed run.
HOLD_LINE = ('i=0; while [ ! -e "%(dir)s/release" ] && [ "$i" -lt 600 ]; '
             'do sleep 1; i=$((i+1)); done\n')

PANE_STUB = VENDOR_STUB + """\
printf '%%s' "$$"      > "%(dir)s/pid"
printf '%%s' "$PPID"   > "%(dir)s/ppid"
printf '%%s' "$PATH"   > "%(dir)s/path"
printf '%%s' "${CLAUDE_SESSION_AGENT_SOCK:-}" > "%(dir)s/agent-sock"
printf '%%s' "${CA209_HYGIENE:-<unset>}"      > "%(dir)s/declared-var"
: > "%(dir)s/done"
""" + HOLD_LINE


def private_env(extra: dict | None = None, tmux: str | None = None) -> dict:
    """The environment of a process that may start tmux: conftest's private
    server, or nothing starts at all. `tmux` sets a TMUX that points INTO
    the private server - the one exception, and only for a socket under
    this test's own TMUX_TMPDIR."""
    env = dict(os.environ)
    if extra:
        env.update(extra)
    sockets = env.get("TMUX_TMPDIR", "")
    if not sockets.startswith("/tmp/st-tmux-") or env.get("TMUX") or env.get("TMUX_PANE"):
        pytest.fail("not walled off from the live tmux server: refusing to start anything")
    if tmux is not None:
        if not os.path.realpath(tmux.split(",")[0]).startswith(
                os.path.realpath(sockets) + "/"):
            pytest.fail(f"TMUX {tmux!r} is not this test's private server")
        env["TMUX"] = tmux
    refuse_unwalled_path(env.get("PATH", ""))
    return env


def e2e_path(bin_dir) -> str:
    """The tmux e2e's PATH, built from scratch so the pane sees no venv:
    the test's own stubs, then the wall, then the system. Without the wall,
    `/opt/homebrew/bin` handed a secrets-declaring e2e the REAL `pass` and
    `gpgconf` (review of c874019, low 1)."""
    uv = shutil.which("uv")
    return os.pathsep.join([str(bin_dir), walled_bin(), os.path.join(sys.prefix, "bin"),
                            *([os.path.dirname(uv)] if uv else []),
                            "/opt/homebrew/bin", "/usr/bin", "/bin"])


def tmux_in(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *arguments], env=private_env(), capture_output=True,
                          text=True, timeout=TIMEOUT_S, check=False)


#: For the e2e tests that drive the private server THEMSELVES, through
#: `tmux_in`, which runs `tmux` on this process's own PATH. The launch under
#: test finds tmux through `e2e_path` (which always names /opt/homebrew/bin),
#: so a test that only launches still runs where tmux is off this PATH (the
#: export shape: env -i, PATH=/usr/bin:/bin:/usr/sbin:/sbin); one that calls
#: `tmux_in` would raise FileNotFoundError there, so it skips instead.
needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not on PATH: tmux is a prerequisite for session start placement")


def window_id_of(stdout: str) -> str:
    return re.search(r"\((@\d+)\)", stdout).group(1)


def write_session_shim(bin_dir: Path) -> None:
    """`session` on the pane's PATH: the pane runs `session start ...`
    again, and the tmux server's PATH must find it (ST-285). This checkout's
    own cli, through the venv's interpreter by its absolute path, so the
    pane's PATH and environment still name no venv."""
    shim = bin_dir / "session"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m sessiontools.cli "$@"\n')
    shim.chmod(0o755)


def e2e_env(tmp_path, front_matter="claude:\n  effort: high\n", extra_env=None,
            agent="scout", stub_body=PANE_STUB, shim=True) -> tuple[dict, Path, Path]:
    """The environment a tmux e2e launches with, its stub vendor recording
    into RECORD from inside the pane. Returns (env, record, work)."""
    record = tmp_path / "record"
    record.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(stub_body % {"dir": record})
    stub.chmod(0o755)
    if shim:
        write_session_shim(bin_dir)
    root = make_agents_root(tmp_path, name=agent, front_matter=front_matter)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    launcher_tmp = tmp_path / "tmpdir"
    launcher_tmp.mkdir(exist_ok=True)
    env = {key: value for key, value in clean_env().items()
           if key not in ("PYTHONPATH", "PYTHONHOME") and not key.startswith("UV_")}
    env.update(AGENTS_DIR=root, SESSIONS_DIR=os.path.join(root, "sessions"),
               HOME=str(home), TMPDIR=str(launcher_tmp), PWD=str(work),
               VIRTUAL_ENV=sys.prefix, PATH=e2e_path(bin_dir))
    env.update(extra_env or {})
    return env, record, work


def tmux_launch_e2e(tmp_path, *verb_args, front_matter="claude:\n  effort: high\n",
                    extra_env=None, tmux=None, agent="scout", stub_body=PANE_STUB,
                    shim=True):
    """The verb as a subprocess, placing into the private server, the stub
    vendor recording from inside the pane. The launcher runs AS IF from its
    own venv (VIRTUAL_ENV set, its bin on PATH), so the scrub the pane test
    checks is not vacuous; HOME is the test's, so the private server reads
    no user tmux.conf. A server this launch births holds this environment
    (less identity and credentials), and the pane's own `session start`
    runs under it. Returns (result, record, work)."""
    env, record, work = e2e_env(tmp_path, front_matter, extra_env, agent, stub_body, shim)
    result = subprocess.run(session_cmd(*verb_args), capture_output=True, text=True,
                            timeout=TIMEOUT_S, cwd=str(work), env=private_env(env, tmux),
                            check=False)
    return result, record, work


def pane_ran(record: Path, window_id: str) -> bool:
    """True once the stub vendor in WINDOW_ID's pane has written `done`.

    An EVENT ends the wait, not a guess at how long a pane takes to start:
    `done` exists, or the pane is dead (`remain-on-exit failed` keeps a
    pane that failed) or gone (a tmux without that option closes it). A
    loaded host only makes the pane slower, and the wait lasts as long as
    the pane is alive and has not got there.

    The one clock left is TIMEOUT_S, and it is a HANG bound: a pane that is
    alive and never gets there cannot be told from a slow one without some
    clock, so one has to stay. It is the suite's "a launcher must never
    hang" bound, not a start-up budget (a fixed 30 s here went red under
    load while the pane was still starting)."""
    done = record / "done"

    def settled() -> bool:
        if done.exists():
            return True
        pane = tmux_in("display-message", "-p", "-t", window_id, "#{pane_dead}")
        return pane.returncode != 0 or pane.stdout.strip() == "1"

    wait_for(settled, timeout=TIMEOUT_S)
    return done.exists()


def no_session_dirs(tmp_path) -> bool:
    sessions = tmp_path / "agents" / "sessions"
    return not sessions.exists() or list(sessions.iterdir()) == []


def test_tmux_placement_lands_and_the_pane_sees_no_venv(tmp_path):
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "scout")
    assert result.returncode == 0, result.stderr
    assert "placed scout in tmux window 'scout'" in result.stdout
    assert "of session 'eng' (created)" in result.stdout
    assert pane_ran(record, window_id_of(result.stdout)), f"the pane never ran the vendor: {result.stdout}"
    argv = recorded_argv(record)
    sid = (record / "session-id-env").read_text()
    assert sid and argv[argv.index("--session-id") + 1] == sid
    root = tmp_path / "agents"
    assert (root / "sessions" / sid / "session-id").read_text() == sid
    # the venv scrub holds IN THE PANE, not just in the harness
    assert (record / "virtual-env").read_text() == ""
    assert os.path.join(sys.prefix, "bin") not in (record / "path").read_text().split(":")
    # no trampoline anywhere: the pane ran `session start` itself (ST-285)
    assert not (root / "sessions" / sid / "launch.sh").exists()
    assert not list((tmp_path / "tmpdir").glob("session-start-pane-*"))


def test_a_space_in_the_agents_dir_still_lands_the_pane(tmp_path):
    """tmux hands the pane's line to `sh -c`: a space in a path the pane
    reaches (AGENTS_DIR, the shim's dir) must not split it."""
    spaced = tmp_path / "agents home"
    spaced.mkdir()
    result, record, _work = tmux_launch_e2e(spaced, "--tmux-target", "eng", "scout")
    assert result.returncode == 0, result.stderr
    assert pane_ran(record, window_id_of(result.stdout)), f"the pane never ran the vendor: {result.stdout}"
    sid = (record / "session-id-env").read_text()
    assert (spaced / "agents" / "sessions" / sid / "session-id").read_text() == sid


@needs_tmux
def test_a_declared_variable_lives_in_the_pane_and_nowhere_else(tmp_path):
    """The live half of tmux hygiene. One variable, three places: PRESENT in
    the pane's own process, ABSENT from the tmux SERVER environment (global
    and session - the server was born from this very launch), and ABSENT
    from a sibling pane in the same session."""
    result, record, _work = tmux_launch_e2e(
        tmp_path, "--tmux-target", "eng", "scout",
        front_matter="environment:\n  variables:\n    CA209_HYGIENE: pane-only\n")
    assert result.returncode == 0, result.stderr
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    assert (record / "declared-var").read_text() == "pane-only"
    for scope in (["-g"], ["-t", "=eng"]):
        dump = tmux_in("show-environment", *scope).stdout
        assert "CA209_HYGIENE" not in dump, f"reached the tmux SERVER ({scope}): {dump}"
    probe = tmp_path / "sibling-env"
    sibling = tmp_path / "sibling.sh"
    sibling.write_text(f'#!/usr/bin/env bash\nprintf "%s" "${{CA209_HYGIENE:-<unset>}}" '
                       f'> "{probe}"\nsleep 30\n')
    sibling.chmod(0o755)
    assert tmux_in("new-window", "-d", "-t", "=eng:", str(sibling)).returncode == 0
    assert wait_for(probe.exists), "the sibling pane never ran"
    assert probe.read_text() == "<unset>"


@needs_tmux
def test_tm1658_target_never_prefix_matches_an_existing_session(tmp_path):
    """Target `alpha` while `alpha-beta` exists creates/uses exactly
    `alpha`: unanchored matching would land in `alpha-beta` (TM-1658)."""
    assert tmux_in("new-session", "-d", "-s", "alpha-beta").returncode == 0
    result, _record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "alpha", "scout")
    assert result.returncode == 0, result.stderr
    sessions = tmux_in("list-sessions", "-F", "#{session_name}").stdout.split()
    assert "alpha" in sessions and "alpha-beta" in sessions
    in_alpha = tmux_in("list-windows", "-t", "=alpha", "-F", "#{window_name}").stdout
    in_beta = tmux_in("list-windows", "-t", "=alpha-beta", "-F", "#{window_name}").stdout
    assert "scout" in in_alpha.splitlines()
    assert "scout" not in in_beta.splitlines()


@needs_tmux
def test_an_existing_session_is_reused_not_created(tmp_path):
    assert tmux_in("new-session", "-d", "-s", "eng").returncode == 0
    result, _record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng:named", "scout")
    assert result.returncode == 0, result.stderr
    assert "(created)" not in result.stdout
    names = tmux_in("list-windows", "-t", "=eng", "-F", "#{window_name}").stdout.splitlines()
    assert "named" in names and len(names) == 2


@needs_tmux
def test_window_is_named_and_automatic_rename_is_off(tmp_path):
    result, _record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "scout")
    assert result.returncode == 0, result.stderr
    window_id = window_id_of(result.stdout)
    option = tmux_in("show-window-options", "-t", window_id, "automatic-rename").stdout
    assert "automatic-rename off" in option
    name = tmux_in("display-message", "-p", "-t", window_id, "#{window_name}").stdout.strip()
    assert name == "scout"


# SESH-1114, measured 2030-12-08 on a private server (tmux 3.7c,
# default options; the herd's ~/.tmux.conf leaves allow-rename off):
# `--tmux-target bob bob` gave window `bob` (automatic-rename 0,
# allow-rename 0), still `bob` after the vendor printed both title escapes.
# With allow-rename ON, the `\033k` escape DID rename it: an option the herd
# does not set, so this pins the herd's configuration, not that one.
TITLE_STUB = """#!/usr/bin/env bash
printf '\\033kVENDOR-RENAMED\\033\\\\'
printf '\\033]2;VENDOR-TITLE\\007'
: > "%(dir)s/done"
""" + HOLD_LINE


@needs_tmux
def test_sesh1114_the_window_is_exactly_the_agent_name_after_the_vendor_runs(tmp_path):
    """`session start --tmux-target bob bob`: the session and the agent share
    a name (the card's case), and the window is `bob` - not a prefix, not
    the cwd, not the vendor's title. Read AFTER the pane has printed a
    window-name escape and a title escape: the pane title changing proves
    tmux parsed past both before the window name is read."""
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "bob", "bob",
                                            agent="bob", stub_body=TITLE_STUB)
    assert result.returncode == 0, result.stderr
    assert "placed bob in tmux window 'bob'" in result.stdout
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    window_id = window_id_of(result.stdout)

    def facts():
        return tmux_in("display-message", "-p", "-t", window_id,
                       "#{window_name}\t#{automatic-rename}\t#{pane_title}").stdout.strip()

    assert wait_for(lambda: facts().endswith("\tVENDOR-TITLE")), \
        f"tmux never parsed the vendor's output: {facts()!r}"
    name, automatic, _title = facts().split("\t")
    assert name == "bob"
    assert automatic == "0"


@needs_tmux
def test_in_tmux_places_into_the_callers_current_session(tmp_path):
    """No target: the window opens in the session the caller is in - here a
    private one, reached through a TMUX value naming its socket."""
    # The pane's `session start` runs under the SERVER's environment, so the
    # server is born the way the caller's own would be: with `session` and
    # the agents dir in it.
    env, _record, _work = e2e_env(tmp_path)
    born = subprocess.run(["tmux", "new-session", "-d", "-s", "home"], env=private_env(env),
                          capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    assert born.returncode == 0, born.stderr
    facts = tmux_in("display-message", "-p", "-t", "=home:",
                    "#{socket_path},#{pid},#{session_id}").stdout.strip()
    socket, pid, session_id = facts.split(",")
    result, record, _work = tmux_launch_e2e(
        tmp_path, "--in-tmux", "scout", tmux=f"{socket},{pid},{session_id.lstrip('$')}")
    assert result.returncode == 0, result.stderr
    assert "of the current session" in result.stdout
    names = tmux_in("list-windows", "-t", "=home", "-F", "#{window_name}").stdout.splitlines()
    assert "scout" in names
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout


def test_keyed_in_pane_reaper_watches_the_panes_own_pid(tmp_path):
    """The CA-152 wiring, measured in the pane (design (vii)): the pane runs
    `session start --keyed` in place, which mints, then execs the vendor in
    its own pid, so the pid handed to --reap-pid IS the vendor's: the
    process whose life is the pane's."""
    stub, key_record = write_key_init_stub(tmp_path)
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "--keyed",
                                            "scout", extra_env={"SESSION_KEY_INIT": stub})
    assert result.returncode == 0, result.stderr
    assert "-- session id minted in the pane" in result.stdout
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    key_argv = Path(key_record).read_text().split()
    assert key_argv[:4] == ["--agent-name", "scout", "--session-dir",
                            str(tmp_path / "agents" / "sessions")]
    assert key_argv[key_argv.index("--reap-pid") + 1] == (record / "pid").read_text()
    # the vendor really got the id the pane's own launch minted
    argv = recorded_argv(record)
    assert argv[argv.index("--session-id") + 1] == STUB_SID
    assert (record / "session-id-env").read_text() == STUB_SID
    anchor = tmp_path / "agents" / "sessions" / STUB_SID / "session-id"
    assert wait_for(anchor.exists) and anchor.read_text() == STUB_SID


@needs_tmux
def test_inherited_agent_pid_is_never_the_traps_target(tmp_path):
    """The friendly-fire regression, with a canary. A KEYED LAUNCHER's own
    CLAUDE_SESSION_AGENT_PID rides into the pane through the tmux server
    env; if the script did not shed it, the pane-lifetime trap would kill
    the LAUNCHER's keyholder (measured the hard way in the old launcher).
    The canary stands in for that keyholder and must outlive the pane."""
    canary = subprocess.Popen(["sleep", "300"])
    try:
        stub, _key_record = write_key_init_stub(tmp_path)
        result, record, _work = tmux_launch_e2e(
            tmp_path, "--tmux-target", "eng", "--keyed", "scout",
            extra_env={"SESSION_KEY_INIT": stub,
                       "CLAUDE_SESSION_AGENT_PID": str(canary.pid),
                       "CLAUDE_SESSION_ID": "the-launchers-own-session"})
        assert result.returncode == 0, result.stderr
        assert pane_ran(record, window_id_of(result.stdout)), result.stdout
        assert (record / "session-id-env").read_text() == STUB_SID
        assert tmux_in("kill-window", "-t", window_id_of(result.stdout)).returncode == 0
        time.sleep(2)   # give a wrong trap its chance to fire
        assert canary.poll() is None, "the pane's reap killed the pid it INHERITED"
    finally:
        canary.kill()
        canary.wait()


DONE_LINE = ': > "%(dir)s/done"\n'
IDENTITY_STUB = PANE_STUB.replace(
    DONE_LINE,
    "printf '%%s' \"${CLAUDE_CODE_SESSION_ID:-<unset>}\" > \"%(dir)s/code-session-id-env\"\n"
    + DONE_LINE)
assert IDENTITY_STUB != PANE_STUB


@needs_tmux
def test_a_start_inside_a_seat_hands_nobody_the_parents_session_id(tmp_path):
    """ST-113, end to end, as repro-001 S4 measured it: the caller holds the
    PARENT's identity and this launch BIRTHS the (private) server. The
    generator, the pane's agent and the server's global environment must
    all be free of the parent's ids."""
    parent = "11111111-2222-4333-8444-555555555555"
    seen = tmp_path / "generator-saw"
    front = ("claude:\n  effort: high\nstart:\n  context:\n    system-prompt:\n"
             f"      - printf '%s %s' \"${{CLAUDE_CODE_SESSION_ID:-<unset>}}\""
             f" \"$CLAUDE_SESSION_ID\" > {seen}\n")
    result, record, _work = tmux_launch_e2e(
        tmp_path, "--tmux-target", "eng", "scout", front_matter=front,
        stub_body=IDENTITY_STUB,
        extra_env={"CLAUDE_CODE_SESSION_ID": parent, "CLAUDE_SESSION_ID": parent})
    assert result.returncode == 0, result.stderr
    assert "(created)" in result.stdout                # this launch birthed the server
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    new_id = (record / "session-id-env").read_text()
    assert new_id and new_id != parent
    assert seen.read_text() == f"<unset> {new_id}"     # the generator
    assert (record / "code-session-id-env").read_text() == "<unset>"   # the agent
    server = tmux_in("show-environment", "-g").stdout
    assert parent not in server, "the server was born holding the parent's ids"
    assert tmux_in("kill-window", "-t", window_id_of(result.stdout)).returncode == 0


SIGNING_PANE_STUB = PANE_STUB.replace(
    DONE_LINE,
    "".join(f'printf \'%%s\' "$(git config --get {key} || echo \'<unset>\')" '
            f'> "%(dir)s/git-{key}"\n' for key in SIGNING_KEYS) + DONE_LINE)
assert SIGNING_PANE_STUB != PANE_STUB


@needs_tmux
def test_an_unkeyed_start_inside_a_keyed_seat_hands_no_pane_the_parents_signing_config(
        tmp_path):
    """ST-118, end to end, as repro-001 S2 measured it: the caller holds a
    keyed seat's GIT_CONFIG_* block and this unkeyed launch BIRTHS the
    (private) server. The pane's git and the server's global environment
    (which every later pane starts from) must both be free of it."""
    result, record, _work = tmux_launch_e2e(
        tmp_path, "--tmux-target", "eng", "scout", stub_body=SIGNING_PANE_STUB,
        extra_env={**PARENT_SIGNING, **HERMETIC_GIT,
                   "CLAUDE_SESSION_AGENT_SOCK": "/parent/agent.sock"})
    assert result.returncode == 0, result.stderr
    assert "(created)" in result.stdout                # this launch birthed the server
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    seen = {key: (record / f"git-{key}").read_text() for key in SIGNING_KEYS}
    server = tmux_in("show-environment", "-g").stdout
    assert tmux_in("kill-window", "-t", window_id_of(result.stdout)).returncode == 0
    assert seen == dict.fromkeys(SIGNING_KEYS, "<unset>")                 # the agent
    assert PARENT_PUB not in server, "the server was born holding the parent's signing key"


def test_contradictory_switch_flags_are_refused_before_anything_is_minted(tmp_path):
    result, _record, _work = tmux_launch_e2e(tmp_path, "--in-tmux", "--switch", "--no-switch",
                                             "scout")
    assert result.returncode == EXIT_USAGE, result.stdout
    assert "pick one" in result.stderr
    assert no_session_dirs(tmp_path), "minted an identity before a usage question"


@needs_tmux
def test_in_tmux_outside_tmux_creates_the_named_default_session(tmp_path):
    """A plain terminal, no server, no target: `--in-tmux` creates session
    `agents` (detached), places the agent there with its window current,
    and the receipt says so and how to attach. A second launch reuses it."""
    assert tmux_in("list-sessions").returncode != 0, "the private server already runs"
    # launched from inside a seat that holds a credential: the server this
    # launch births must hold neither (born the ST-270 way)
    inherited = {"CLAUDE_CODE_AGENT": "claude", "ANTHROPIC_API_KEY": "not-a-real-key"}
    result, record, _work = tmux_launch_e2e(tmp_path, "--in-tmux", "scout",
                                            extra_env=inherited)
    assert result.returncode == 0, result.stderr
    assert "placed scout in tmux window 'scout'" in result.stdout
    assert "of session 'agents' (created)" in result.stdout
    assert result.stdout.rstrip().endswith("; attach: tmux attach -t agents")
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    window_id = window_id_of(result.stdout)
    sessions = tmux_in("list-sessions", "-F", "#{session_name}").stdout.split()
    assert sessions == ["agents"]
    active = tmux_in("display-message", "-p", "-t", "=agents:", "#{window_id}").stdout.strip()
    assert active == window_id, "an attach would land on window 0, not the agent"
    server = tmux_in("show-environment", "-g").stdout
    for name in inherited:
        assert name not in server, f"the server was born holding {name}"
    second = tmp_path / "second"
    second.mkdir()
    # The first pane still holds scout (its own launch claimed it), so the
    # second launch is a second holder on purpose: it says so.
    again, _record, _work = tmux_launch_e2e(second, "--in-tmux", "--allow-second-holder",
                                            "scout")
    assert again.returncode == 0, again.stderr
    assert "of session 'agents' -- session" in again.stdout     # reused, not created
    names = tmux_in("list-windows", "-t", "=agents", "-F", "#{window_name}").stdout.split()
    assert names.count("scout") == 2
    assert tmux_in("list-sessions", "-F", "#{session_name}").stdout.split() == ["agents"]


@needs_tmux
def test_tmux_placement_refuses_fable_before_any_window_or_trampoline(tmp_path):
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "scout",
                                            "--model", "fable")
    assert result.returncode == EXIT_USAGE, (result.returncode, result.stderr)
    assert not (record / "argv").exists(), "the pane ran the vendor"
    assert no_session_dirs(tmp_path)
    assert "eng" not in tmux_in("list-sessions", "-F", "#{session_name}").stdout.split()


@pytest.mark.skipif(not os.access(REAL_KEY_INIT, os.X_OK),
                    reason="session-key-init not present on this machine")
def test_pane_lifetime_keyed_agent_and_reaper_die_with_the_pane(tmp_path):
    """The acceptance criterion against the REAL primitive: a keyed pane
    mints a live ssh-agent whose key answers while the pane lives, and
    killing the pane kills the agent. --no-register via a test-owned
    wrapper, so the registry sees nothing."""
    # The real HOME goes to the primitive alone, inside its wrapper. The
    # launch keeps the test's HOME, so the private server it starts reads no
    # user ~/.tmux.conf, as tmux_launch_e2e promises (review of 71d755f,
    # low 5: with HOME real for the whole launch, the server read the
    # developer's tmux.conf and ran its theme script).
    wrapper = tmp_path / "key-init-no-register"
    real_home = pwd.getpwuid(os.getuid()).pw_dir    # the wall sets HOME to a tmp dir
    wrapper.write_text(f'#!/usr/bin/env bash\nHOME={shlex.quote(real_home)} '
                       f'exec "{REAL_KEY_INIT}" "$@" --no-register\n')
    wrapper.chmod(0o755)
    result, record, _work = tmux_launch_e2e(
        tmp_path, "--tmux-target", "life", "--keyed", "scout",
        extra_env={"SESSION_KEY_INIT": str(wrapper)})
    assert result.returncode == 0, result.stderr
    assert tmux_in("show-environment", "-g", "HOME").stdout.strip() == \
        f"HOME={tmp_path / 'home'}", "the private server was started with the real HOME"
    sock_file = record / "agent-sock"
    assert wait_for(lambda: sock_file.exists() and sock_file.read_text(), timeout=45), \
        "the keyed pane never booted: key birth failed in the pane?"
    sock = sock_file.read_text()

    def agent_answers():
        probe = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, check=False,
                               env=dict(os.environ, SSH_AUTH_SOCK=sock))
        return probe.returncode == 0

    assert wait_for(agent_answers, timeout=30), "the minted key never answered"
    assert tmux_in("kill-window", "-t", window_id_of(result.stdout)).returncode == 0
    assert wait_for(lambda: not agent_answers(), timeout=60), \
        "the pane died but the key's ssh-agent survived it"


# ---------- ST-285: the pane runs the ENTERED line, on a private server ----------------------

HANDOFF_STUB = PANE_STUB.replace(
    DONE_LINE,
    "printf '%%s' \"${SESSION_START_ENTERED:-<unset>}\" > \"%(dir)s/entered-env\"\n"
    "printf '%%s' \"${SESSION_RECORD_LAUNCH_COMMAND:-<unset>}\" > \"%(dir)s/record-env\"\n"
    + DONE_LINE)
assert HANDOFF_STUB != PANE_STUB

#: Exits 0 at once: the window must close behind it.
CLEAN_EXIT_STUB = VENDOR_STUB + ': > "%(dir)s/done"\nexit 0\n'


def require_remain_on_exit_failed() -> None:
    """Skip unless this tmux takes `remain-on-exit failed`. Probes the
    VALUE, not `tmux -V`, on a throwaway server inside the test's wall:
    tmux 3.8 accepts it (measured); a tmux without it answers `unknown
    value`, and the placement then degrades loudly (see the fake-tmux test)."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not on PATH")
    probe = subprocess.run(
        ["tmux", "-L", "st285-probe", "-f", "/dev/null", "new-session", "-d", "sleep 5",
         ";", "set-option", "-g", "remain-on-exit", "failed"],
        env=private_env(), capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    subprocess.run(["tmux", "-L", "st285-probe", "kill-server"], env=private_env(),
                   capture_output=True, timeout=TIMEOUT_S, check=False)
    if probe.returncode != 0:
        pytest.skip(f"this tmux has no `remain-on-exit failed`: {probe.stderr.strip()}")


def pane_fact(target: str, fmt: str) -> str:
    return tmux_in("display-message", "-p", "-t", target, fmt).stdout.rstrip("\n")


def the_record(tmp_path, sid: str) -> dict:
    from sessiontools.start.record import parse_command
    return parse_command((tmp_path / "agents" / "sessions" / sid / "launch" / "command")
                         .read_text())


def message_file_in(start_command: str) -> Path:
    """The message file a pane line names (tmux's rendering: no space in
    the test's paths, so the word after the flag is the path)."""
    path = Path(re.search(r"--first-message-file (\S+)", start_command).group(1))
    assert path.name == "first-message.md" and path.parent.parent.name == "launches"
    return path


def captured(window_id: str) -> str:
    """The pane's whole text, history included, wrapped lines joined."""
    return tmux_in("capture-pane", "-p", "-J", "-S", "-", "-t", window_id).stdout


def dead_with(window_id: str, status: int) -> bool:
    return wait_for(lambda: pane_fact(window_id, "#{pane_dead} #{pane_dead_status}")
                    == f"1 {status}")


@needs_tmux
def test_i_the_pane_start_command_is_the_entered_line_less_placement(tmp_path):
    """(i): `#{pane_start_command}` is tmux's rendering of the pane line
    (`"` + line + `"`: it has a space and no `$`, `"` or `\\`), with no
    placement flag in it, and no `session-start-pane-*` anywhere."""
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "--no-switch",
                                            "scout", "--effort", "low")
    assert result.returncode == 0, result.stderr
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    window_id = window_id_of(result.stdout)
    line = "session start scout --effort low"
    assert pane_fact(window_id, "#{pane_start_command}") == f'"{line}"'
    assert not list((tmp_path / "tmpdir").glob("session-start-pane-*"))
    assert not list((tmp_path / "agents").rglob("launch.sh"))


@needs_tmux
def test_ii_no_first_message_word_in_the_pane_line_or_the_panes_argv(tmp_path):
    """(ii), ruling 7: entered words reach the vendor by a 0600 file; no
    word is in `#{pane_start_command}` or the pane process's argv (read with
    `ps` after the exec); the record keeps the line exactly as entered; the
    hand-off never reaches the vendor's environment."""
    result, record, work = tmux_launch_e2e(
        tmp_path, "--tmux-target", "eng", "scout", "zebrafish", "quokka", "--effort", "low",
        stub_body=HANDOFF_STUB)
    assert result.returncode == 0, result.stderr
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    window_id = window_id_of(result.stdout)
    start_command = pane_fact(window_id, "#{pane_start_command}")
    pid = (record / "pid").read_text()
    argv = subprocess.run(["ps", "-ww", "-o", "args=", "-p", pid], capture_output=True,
                          text=True, check=False).stdout
    assert argv.strip(), "ps saw no pane process"
    for word in ("zebrafish", "quokka"):
        assert word not in start_command
        assert word not in argv
        assert word not in " ".join(recorded_argv(record))
    assert "--first-message-file" in start_command and "--first-message-file-over 0" in \
        start_command
    message = message_file_in(start_command)
    assert message.read_text() == "zebrafish quokka"
    assert stat.S_IMODE(message.stat().st_mode) == 0o600
    assert stat.S_IMODE(message.parent.stat().st_mode) == 0o700
    sid = (record / "session-id-env").read_text()
    # RESIDUAL, named: the pane writes this record only after a `git
    # check-ignore` guard with a fixed 10 s budget (record.GIT_TIMEOUT_S). If
    # that one git call takes longer, the pane writes no record by design (a
    # record never costs a session; test_start_record covers that path in
    # process) and `the_record` below raises FileNotFoundError. That needs a
    # single exec of git (12 ms on an idle host) to stall for 10 s.
    fields = the_record(tmp_path, sid)
    assert shlex.split(fields["launch_command"]) == [
        "session", "start", "--tmux-target", "eng", "scout", "zebrafish", "quokka",
        "--effort", "low"]
    assert shlex.split(fields["launch_dir"]) == [str(work)]
    assert (record / "entered-env").read_text() == "<unset>"
    assert (record / "record-env").read_text() == "<unset>"    # F2: unset after the record
    # the vendor still got the message, by its pointer file
    pointed = [path for path in (tmp_path / "agents" / "sessions" / sid).rglob("*")
               if path.is_file() and "zebrafish quokka" in path.read_text(errors="ignore")]
    assert pointed, "the first message never reached the session's files"


@needs_tmux
def test_iii_a_refused_pane_stays_dead_with_its_reason(tmp_path):
    """(iii), ruling 3: the race the outer launcher accepts. Its read says
    free (here: it reads another state dir), the pane's own launch finds
    the holder and refuses; the window stays, status 3, the line readable."""
    require_remain_on_exit_failed()
    first, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "scout")
    assert first.returncode == 0, first.stderr
    assert pane_ran(record, window_id_of(first.stdout)), first.stdout
    elsewhere = tmp_path / "second"
    elsewhere.mkdir()
    second, _record, _work = tmux_launch_e2e(
        elsewhere, "--tmux-target", "eng", "scout",
        extra_env={"SESSION_STATE_DIR": str(elsewhere / "state"),
                   "SESSION_SIDECAR_DIR": str(elsewhere / "sidecars")})
    assert second.returncode == 0, second.stderr
    window_id = window_id_of(second.stdout)
    assert dead_with(window_id, 3), pane_fact(window_id, "#{pane_dead} #{pane_dead_status}")
    shown = captured(window_id)
    assert "refusing a second holder" in shown and "session start:" in shown, shown


@needs_tmux
def test_iv_a_pane_whose_path_has_no_session_fails_loudly(tmp_path):
    """(iv), ruling 4d: the pane cannot find `session`. Measured on tmux
    3.8: a new pane's PATH is the CREATING client's (the launcher's, less
    its venv: `host_environ`), not the server's global one, so this is a
    launcher whose PATH has no `session` (a venv-only install). The window
    stays, status 127, `command not found` readable."""
    require_remain_on_exit_failed()
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "bare", "scout",
                                            shim=False)
    assert result.returncode == 0, result.stderr
    window_id = window_id_of(result.stdout)
    assert dead_with(window_id, 127)
    assert "command not found" in captured(window_id)
    assert not (record / "argv").exists()


@needs_tmux
def test_v_a_clean_exit_closes_the_window(tmp_path):
    """(v): remain-on-exit FAILED keeps only a pane that failed."""
    require_remain_on_exit_failed()
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "scout",
                                            stub_body=CLEAN_EXIT_STUB)
    assert result.returncode == 0, result.stderr
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    window_id = window_id_of(result.stdout)
    assert wait_for(lambda: window_id not in tmux_in(
        "list-windows", "-a", "-F", "#{window_id}").stdout.split()), "the window stayed"


@needs_tmux
def test_vi_a_respawn_rereads_the_file_and_records_the_pane_line(tmp_path):
    """(vi): `respawn-pane -k` runs the pane line again: a FRESH seat, the
    same first message from the file, and (no hand-off survives a respawn)
    its record holds the pane line. With the file gone, the respawn is
    refused (exit 2) and its reason stays readable."""
    require_remain_on_exit_failed()
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "scout",
                                            "zebrafish")
    assert result.returncode == 0, result.stderr
    assert pane_ran(record, window_id_of(result.stdout)), result.stdout
    window_id = window_id_of(result.stdout)
    first_sid = (record / "session-id-env").read_text()
    start_command = pane_fact(window_id, "#{pane_start_command}")
    (record / "done").unlink()
    # A respawned pane's PATH is the respawning client's (measured, tmux
    # 3.8): respawn from one that can find `session`, as ST-30's would.
    env, _record, _work = e2e_env(tmp_path)
    respawn = subprocess.run(["tmux", "respawn-pane", "-k", "-t", window_id],
                             env=private_env(env), capture_output=True, text=True,
                             timeout=TIMEOUT_S, check=False)
    assert respawn.returncode == 0, respawn.stderr
    assert pane_ran(record, window_id), "the respawned pane never ran the vendor"
    second_sid = (record / "session-id-env").read_text()
    assert second_sid and second_sid != first_sid
    fields = the_record(tmp_path, second_sid)
    message = message_file_in(start_command)
    assert shlex.split(fields["launch_command"]) == [
        "session", "start", "--first-message-file", str(message),
        "--first-message-file-over", "0", "scout"]
    assert pane_fact(window_id, "#{pane_start_command}") == start_command
    message.unlink()
    respawn = subprocess.run(["tmux", "respawn-pane", "-k", "-t", window_id],
                             env=private_env(env), capture_output=True, text=True,
                             timeout=TIMEOUT_S, check=False)
    assert respawn.returncode == 0, respawn.stderr
    assert dead_with(window_id, 2)
    assert "no such file" in captured(window_id)


# ---------- review F1: a placed pane resolves in tmux's environment ---------------------------

class FakeShowEnvironment:
    """`placement._tmux.run` answering `show-environment`: GLOBAL for `-g`,
    SESSION for a session read (None: no such session), every call kept."""

    def __init__(self, monkeypatch, global_env: str | None, session_env: str | None = None):
        self.calls: list[list[str]] = []
        self.global_env, self.session_env = global_env, session_env
        monkeypatch.setattr(placement._tmux, "run", self.run)

    def run(self, arguments, env=None, timeout=None):
        self.calls.append(list(arguments))
        assert arguments[0] == "show-environment" and timeout
        text = self.global_env if arguments[1:] == ["-g"] else self.session_env
        if text is None:
            return subprocess.CompletedProcess(arguments, 1, "", "no server running")
        return subprocess.CompletedProcess(arguments, 0, text, "")


SERVER = "AGENTS_DIR=/srv/agents\nSESSION_STATE_DIR=/srv/state\nSESSIONS_DIR=/srv/sessions\n"


@pytest.mark.parametrize("name", placement.RESOLUTION_VARS)
def test_a_differing_resolution_var_gives_one_line_naming_it(monkeypatch, name):
    FakeShowEnvironment(monkeypatch, SERVER)
    environ = {"AGENTS_DIR": "/srv/agents", "SESSION_STATE_DIR": "/srv/state",
               "SESSIONS_DIR": "/srv/sessions", name: "/mine"}
    lines = placement.environment_warnings("eng", environ)
    assert len(lines) == 1
    assert lines[0].startswith(f"warning: {name} is /mine here but /srv/")
    assert "a placed pane resolves in tmux's environment" in lines[0]
    assert "\n" not in lines[0]


def test_each_differing_var_gets_its_own_line(monkeypatch):
    FakeShowEnvironment(monkeypatch, SERVER)
    environ = {name: "/mine" for name in placement.RESOLUTION_VARS}
    lines = placement.environment_warnings("eng", environ)
    assert [line.split()[1] for line in lines] == list(placement.RESOLUTION_VARS)


def test_equal_or_unset_gives_none(monkeypatch):
    fake = FakeShowEnvironment(monkeypatch, SERVER)
    assert placement.environment_warnings("eng", {"AGENTS_DIR": "/srv/agents"}) == []
    assert placement.environment_warnings("eng", {"SESSIONS_DIR": ""}) == []
    calls = len(fake.calls)
    assert placement.environment_warnings("eng", {"PATH": "/bin"}) == []
    assert len(fake.calls) == calls           # nothing set: tmux is not even asked


def test_a_var_tmux_does_not_hold_is_named_as_unset_there(monkeypatch):
    FakeShowEnvironment(monkeypatch, "HOME=/h\n-AGENTS_DIR\n")
    lines = placement.environment_warnings("eng", {"AGENTS_DIR": "/mine",
                                                   "SESSIONS_DIR": "/s d"})
    assert lines[0].startswith("warning: AGENTS_DIR is /mine here but unset in tmux's")
    assert lines[1].startswith("warning: SESSIONS_DIR is '/s d' here but unset in tmux's")


def test_the_sessions_own_environment_wins_over_the_global_one(monkeypatch):
    """tmux builds a pane from the global environment with the session's over
    it: a session-level value equal to the caller's is no difference."""
    fake = FakeShowEnvironment(monkeypatch, SERVER, session_env="AGENTS_DIR=/mine\n")
    assert placement.environment_warnings("eng", {"AGENTS_DIR": "/mine"}) == []
    assert ["show-environment", "-t", "=eng"] in fake.calls
    FakeShowEnvironment(monkeypatch, SERVER, session_env="-AGENTS_DIR\n")
    assert "unset in tmux's" in placement.environment_warnings(
        "eng", {"AGENTS_DIR": "/srv/agents"})[0]


def test_the_callers_current_session_is_read_with_no_target(monkeypatch):
    fake = FakeShowEnvironment(monkeypatch, SERVER, session_env="")
    placement.environment_warnings("", {"AGENTS_DIR": "/mine"})
    assert ["show-environment"] in fake.calls


def test_no_server_or_no_tmux_gives_none(monkeypatch):
    """A server this placement births is born from the caller's environment."""
    FakeShowEnvironment(monkeypatch, None)
    assert placement.environment_warnings("eng", {"AGENTS_DIR": "/mine"}) == []

    def absent(*_args, **_kwargs):
        raise FileNotFoundError("tmux")
    monkeypatch.setattr(placement._tmux, "run", absent)
    assert placement.environment_warnings("eng", {"AGENTS_DIR": "/mine"}) == []

    def hangs(arguments, env=None, timeout=None):
        raise subprocess.TimeoutExpired(arguments, timeout)
    monkeypatch.setattr(placement._tmux, "run", hangs)
    assert placement.environment_warnings("eng", {"AGENTS_DIR": "/mine"}) == []


@needs_tmux
def test_a_placement_whose_agents_dir_differs_from_tmux_warns_once_and_proceeds(tmp_path):
    """End to end on the private server: the first launch births it (its
    environment is the server's), the second sets its own AGENTS_DIR. One
    stderr line names AGENTS_DIR; the window still opens (exit 0) and its
    pane resolves the agent from tmux's AGENTS_DIR, so the stub still runs."""
    first, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "scout")
    assert first.returncode == 0, first.stderr
    assert "warning:" not in first.stderr               # born from this env: equal
    assert pane_ran(record, window_id_of(first.stdout)), first.stdout
    (record / "done").unlink()
    elsewhere = tmp_path / "elsewhere-agents"
    shutil.copytree(tmp_path / "agents", elsewhere, ignore=shutil.ignore_patterns("sessions"))
    second, record, _work = tmux_launch_e2e(
        tmp_path, "--tmux-target", "eng", "--allow-second-holder", "scout",
        extra_env={"AGENTS_DIR": str(elsewhere)})
    assert second.returncode == 0, second.stderr
    warned = [line for line in second.stderr.splitlines() if "warning:" in line]
    assert len(warned) == 1, second.stderr
    assert "AGENTS_DIR" in warned[0] and str(elsewhere) in warned[0]
    assert "SESSION_STATE_DIR" not in second.stderr and "SESSIONS_DIR" not in second.stderr
    assert pane_ran(record, window_id_of(second.stdout)), second.stdout
