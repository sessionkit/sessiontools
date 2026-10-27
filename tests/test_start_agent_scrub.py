"""ST-270: the seat word, CLAUDE_CODE_AGENT, is session identity at the
launch boundary (sessionmail SM-971 option A1).

Claude Code's `--agent NAME` sets CLAUDE_CODE_AGENT=NAME in its own
process, and the vendor writes its sidecar's `agent` field from it.
sessionmail's vmailbox-gen merges every live sidecar carrying a word into
that word's row. So a plain session that merely INHERITED `claude` from the
seat it was started in (directly, or through a tmux server born inside the
seat) heard the seat's mail, and the `WILL_NOT_HEAR` line it printed was
false.

The fix: the name joins SESSION_IDENTITY_VARS, so the launch script's
first line unsets it and the tmux birth drops it. A launch that names an
agent still gets its word from the vendor's `--agent` door, never from the
environment: these tests pin that every holder road passes `--agent`.

Hermetic: tmp agents, sessions, state and sidecar dirs, a throwaway HOME,
a stub vendor and a stub `message`. No test touches the host's tmux
server (placement's `run_tmux` is a recorder) or its mail.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from start_support import VENDOR_STUB, launch_e2e, plan_for, recorded_argv
from test_start_singleton import SEAT, e2e_path, message_stub, write_seat_claim

from sessiontools.start import placement
from sessiontools.start import plan as pl
from sessiontools.start.args import parse_args
from sessiontools.start.environment import (
    SESSION_IDENTITY_VARS,
    render_identity_unset,
    without_session_identity,
)

AGENT_ENV = "CLAUDE_CODE_AGENT"

on_macos = pytest.mark.skipif(sys.platform != "darwin",
                              reason="the default seat is macOS-only until sessionmail SM-932")

#: The stub vendor, plus what the exec'd process holds in AGENT_ENV
#: (`<unset>` when absent). The real vendor sets it again from `--agent`;
#: the stub does not, so `<unset>` here means "nothing was INHERITED".
AGENT_VENDOR_STUB = VENDOR_STUB + (
    f'printf \'%%s\' "${{{AGENT_ENV}-<unset>}}" > "%(dir)s/env-agent"\n')

#: What a session started from inside the default seat holds.
INSIDE_THE_SEAT = {AGENT_ENV: SEAT}


def hermetic(tmp_path, **extra) -> dict:
    """A throwaway HOME and private state and sidecar dirs, so a seat check
    never reads the host's live sessions."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "SESSION_STATE_DIR": str(tmp_path / "state"),
            "SESSION_SIDECAR_DIR": str(tmp_path / "sidecars"), **extra}


def inherited(record) -> str:
    return (record / "env-agent").read_text()


# ---------- the one definition ------------------------------------------------------

def test_the_seat_word_is_session_identity():
    assert AGENT_ENV in SESSION_IDENTITY_VARS
    assert AGENT_ENV not in without_session_identity({AGENT_ENV: SEAT, "PATH": "/bin"})


def test_the_launch_scripts_first_line_unsets_the_seat_word():
    assert AGENT_ENV in render_identity_unset().split()[1:]


def test_the_unset_line_run_in_bash_drops_an_inherited_seat_word():
    """Measured in bash: a pane on a server something else birthed (a human's
    shell, `tm kick`, a server born in the seat before this fix) holds the
    word, and the script's first line is what takes it off."""
    script = render_identity_unset() + f'\nset -u\nprintf %s "${{{AGENT_ENV}-<unset>}}"\n'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True,
                         env={"PATH": os.environ["PATH"], **INSIDE_THE_SEAT}).stdout
    assert out == "<unset>"


def test_only_the_seat_word_joins_the_scrub():
    """Never EMAIL, TM_AGENT_ADDRESS or SESSIONMAIL_SUBSCRIBE: `tm kick` hands
    those to its seats through `session start`'s caller environment on
    purpose (SM-971 A1), and an unconditional unset would deafen them."""
    passed_through = ("EMAIL", "TM_AGENT_ADDRESS", "SESSIONMAIL_SUBSCRIBE")
    assert not set(passed_through) & set(SESSION_IDENTITY_VARS)
    assert not set(passed_through) & set(render_identity_unset().split())
    kept = without_session_identity({name: "x" for name in passed_through})
    assert set(kept) == set(passed_through)


# ---------- the tmux birth -----------------------------------------------------------

def test_a_server_born_inside_the_seat_never_gets_the_seat_word(tmp_path, monkeypatch):
    """`new-session` may birth the server, and a server keeps its starter's
    environment as its global one for every pane it later makes (SM-971
    RQ1 probe 2). Recorded, never run: the host's tmux is not touched."""
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n"
                              "      - \"echo placed\"\n", flags=("--in-tmux",))
    caller = {"PATH": "/usr/bin", "HOME": str(tmp_path), "TERM": "xterm",
              **INSIDE_THE_SEAT}
    envs = {}

    def recording(arguments, environ=None):
        envs[arguments[0]] = environ
        return "@9\n" if arguments[0] == "new-window" else ""

    monkeypatch.setattr(placement, "run_tmux", recording)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: False)
    placement.place_in_tmux(plan, "alpha", "scout", False, environ=caller)
    assert AGENT_ENV not in envs["new-session"]
    assert envs["new-session"] == {"PATH": "/usr/bin", "HOME": str(tmp_path),
                                   "TERM": "xterm"}
    # The pane itself runs the trampoline, whose first line unsets it too.
    assert render_identity_unset() in plan.script.splitlines()


# ---------- the context generators (SM-971 E2) ---------------------------------------

def test_a_named_launch_hands_its_generators_its_own_word(tmp_path):
    """generator_environ scrubs, then sets the NAMED agent: the scrub must
    not take a named launch's word away from its generators."""
    plan = plan_for(tmp_path, "start:\n  context:\n    system-prompt:\n"
                              "      - echo \"[$CLAUDE_CODE_AGENT]\"\n",
                    flags=["--dry-run"], extra_env=INSIDE_THE_SEAT)
    assert "[scout]" in plan.injected.system_prompt
    launch = pl.resolve(parse_args(["--dry-run", "scout"]),
                        environ={"AGENTS_DIR": str(tmp_path / "agents"), **INSIDE_THE_SEAT},
                        work_dir=str(tmp_path))
    assert pl.generator_environ(launch, pl.mint(launch))[AGENT_ENV] == "scout"


def test_a_plain_launch_hands_its_generators_no_word(tmp_path):
    launch = pl.resolve(parse_args(["--dry-run"]),
                        environ={"AGENTS_DIR": str(tmp_path), **INSIDE_THE_SEAT},
                        work_dir=str(tmp_path))
    assert AGENT_ENV not in pl.generator_environ(launch, pl.mint(launch))


# ---------- the vendor's environment, end to end ------------------------------------

def test_a_plain_dash_launch_from_inside_the_seat_inherits_no_word(tmp_path):
    """SM-971 RQ1 probe 1, inverted: `session start -` from the seat's shell
    execs a vendor that holds no CLAUDE_CODE_AGENT and gets no `--agent`."""
    result, record, _work = launch_e2e(tmp_path, "-", stub_body=AGENT_VENDOR_STUB,
                                       extra_env=hermetic(tmp_path, **INSIDE_THE_SEAT))
    assert result.returncode == 0, result.stderr
    assert "--agent" not in recorded_argv(record)
    assert inherited(record) == "<unset>"


@on_macos
def test_a_bare_start_beside_a_live_holder_inherits_no_word(tmp_path):
    """A second bare start, typed in the holder's own tree, runs plain and
    prints WILL_NOT_HEAR: that line is now true, because nothing hands it
    the word."""
    stubs = tmp_path / "stubs"
    message_stub(stubs)
    env = hermetic(tmp_path, PATH=e2e_path(tmp_path, stubs), **INSIDE_THE_SEAT)
    write_seat_claim(env)                    # this pytest process holds the seat
    result, record, _work = launch_e2e(tmp_path, stub_body=AGENT_VENDOR_STUB,
                                       extra_env=env)
    assert result.returncode == 0, result.stderr
    assert "starting plain" in result.stderr and "will NOT hear" in result.stderr
    assert "--agent" not in recorded_argv(record)
    assert inherited(record) == "<unset>"


@on_macos
def test_a_bare_start_that_takes_the_seat_gets_the_word_from_its_door(tmp_path):
    """The holder keeps its word: the bare start that TAKES the seat passes
    `--agent claude`, and the vendor sets the variable from that flag. It
    inherits nothing, even from a caller that held a stale word."""
    stubs = tmp_path / "stubs"
    message_stub(stubs)
    env = hermetic(tmp_path, PATH=e2e_path(tmp_path, stubs), **{AGENT_ENV: "somebody"})
    result, record, _work = launch_e2e(tmp_path, stub_body=AGENT_VENDOR_STUB,
                                       extra_env=env)
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert argv[argv.index("--agent") + 1] == SEAT
    assert inherited(record) == "<unset>"


@on_macos
def test_a_named_seat_launch_gets_the_word_from_its_door(tmp_path):
    stubs = tmp_path / "stubs"
    message_stub(stubs)
    env = hermetic(tmp_path, PATH=e2e_path(tmp_path, stubs), **INSIDE_THE_SEAT)
    result, record, _work = launch_e2e(tmp_path, SEAT, stub_body=AGENT_VENDOR_STUB,
                                       extra_env=env)
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert argv[argv.index("--agent") + 1] == SEAT
    assert inherited(record) == "<unset>"


def test_a_named_launch_gets_its_word_from_its_door_not_the_callers(tmp_path):
    """`tm kick`'s road: `session start <role>` from inside a seat. The agent
    gets `--agent <role>`; the caller's `claude` never reaches it."""
    result, record, _work = launch_e2e(tmp_path, "scout", stub_body=AGENT_VENDOR_STUB,
                                       extra_env=hermetic(tmp_path, **INSIDE_THE_SEAT))
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert argv[argv.index("--agent") + 1] == "scout"
    assert inherited(record) == "<unset>"
