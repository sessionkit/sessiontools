"""Tmux placement: `--in-tmux` / `--tmux-target`, the trampoline, the keyed
mint inside the pane, and the handover at `new-window`. Ported from the old
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

import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
from sessiontools.start import placement
from sessiontools.start.environment import (
    SESSION_IDENTITY_VARS,
    parse_environment,
    render_env_prelude,
    render_identity_unset,
)
from sessiontools.start.errors import EXIT_OK, EXIT_USAGE, LaunchError
from sessiontools.start.identity import (
    PaneMint,
    SessionIdentity,
    expand_shortsid,
    render_pane_mint,
)
from sessiontools.start.plan import render_launch_script
from sessiontools.start.vendor import ShellExpr, ShellVar, render_argv_token

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


def test_check_placeable_needs_a_session_or_tmux():
    placement.check_placeable("eng", environ={})
    placement.check_placeable("", environ={"TMUX": "/x,1,2"})
    with pytest.raises(LaunchError) as refused:
        placement.check_placeable("", environ={})
    assert refused.value.code == EXIT_USAGE
    assert "nowhere to place the window" in str(refused.value)


# ---------- units: the trampoline text -------------------------------------------------------

def test_launch_script_sheds_the_launchers_identity_first():
    """Both endings, first thing after the header: the launched session must
    never inherit the LAUNCHER's session identity (the friendly-fire bug -
    see the canary test below)."""
    identity = SessionIdentity("sid-1", "/d", False, {"CLAUDE_SESSION_ID": "sid-1"})
    direct = render_launch_script(["v"], "/w", identity=identity)
    pane = render_launch_script(["v"], "/w", shebang=True,
                                pane_mint=PaneMint("/k/session-key-init", "a", "/r"))
    for text in (direct, pane):
        lines = text.splitlines()
        shed = lines.index(render_identity_unset())
        # ST-113: the vendor's name too - a start from inside a seat handed
        # the parent's CLAUDE_CODE_SESSION_ID straight through to the agent
        assert {"CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_AGENT_PID"} <= set(lines[shed].split())
        first_use = next(i for i, line in enumerate(lines)
                         if "session-key-init" in line
                         or line.startswith("export CLAUDE_SESSION_ID"))
        assert shed < first_use


def test_shellvar_slot_expands_instead_of_quoting():
    text = render_launch_script(["vendor", "--session-id", ShellVar("CLAUDE_SESSION_ID")], "/w")
    assert 'exec vendor --session-id "$CLAUDE_SESSION_ID"' in text


def test_trampoline_is_the_launch_script_plus_a_shebang():
    """Extend, don't fork: shebang aside, the trampoline IS the direct text."""
    kwargs = {"argv": ["v"], "work_dir": "/w", "env_prelude": "# marker"}
    direct = render_launch_script(**kwargs)
    assert render_launch_script(shebang=True, **kwargs) == "#!/usr/bin/env bash\n" + direct


def test_pane_mint_block_mints_before_everything_and_anchors_the_id():
    mint = PaneMint(key_init="/kl/session-key-init", agent_name="scout",
                    sessions_root="/roots/sessions")
    text = render_launch_script(["vendor", "--session-id", ShellVar("CLAUDE_SESSION_ID")],
                                "/w", pane_mint=mint, env_prelude="# prelude marker")
    lines = text.splitlines()
    mint_at = next(i for i, line in enumerate(lines) if "session-key-init" in line)
    cd_at = next(i for i, line in enumerate(lines) if line.startswith("cd "))
    assert mint_at < cd_at < lines.index("# prelude marker")
    assert '--reap-pid "$$"' in text                   # the pane's OWN pid (CA-152)
    assert "--agent-name scout --session-dir /roots/sessions" in text
    assert '/roots/sessions/"$CLAUDE_SESSION_ID"/session-id' in text   # the anchor
    assert "--no-register" not in text                 # the live text, always
    # the pane-lifetime trap: reap on the way out, and NO exec - the trap
    # needs a shell to fire in, so the vendor runs as a child
    assert "trap __ss_reap EXIT HUP INT TERM" in text
    assert "/kl/session-death" in text
    assert "\nexec " not in text
    assert text.rstrip().endswith('exit "$?"')


def test_the_pane_mint_quotes_what_it_is_handed():
    mint = PaneMint(key_init="/key dir/session-key-init", agent_name="it's",
                    sessions_root="/a b/sessions")
    block = render_pane_mint(mint)
    assert "'/key dir/session-key-init'" in block
    assert "'/key dir/session-death'" in block
    assert "'/a b/sessions'/\"$CLAUDE_SESSION_ID\"/session-id" in block
    assert "'it'\"'\"'s'" in block


def test_the_keyed_pane_trampoline_is_byte_unchanged_by_st112():
    """ST-112 detaches the DIRECT path's mint only. The pane path mints
    inside the pane, under its own reap trap, and ST-112 must not move its
    text. The golden was captured at 82d1bff, before ST-112, and
    re-captured on ST-112's tip (21063f0) for ST-113, which moves exactly
    one thing: the opening `unset` also names CLAUDE_CODE_SESSION_ID, so
    the pane never inherits the caller's id. ST-143 moves that same line
    once more: it unsets CREDENTIAL_VARS too. It is stored
    WITHOUT its shebang line: a tracked file that opens with a bash shebang
    is shellchecked by `make lint`, and generated text is not a source
    script. The shebang is asserted here, so the whole text is still exact."""
    mint = PaneMint("/kbin/session-key-init", "voicemode+super", "/tmp/s")
    text = render_launch_script(["v", "--flag"], "/w", shebang=True, pane_mint=mint)
    shebang = "#!/usr/bin/env bash\n"
    golden = os.path.join(FIXTURES, "pane-trampoline-keyed.golden")
    with open(golden, encoding="utf-8") as handle:
        assert text == shebang + handle.read()


def test_identity_and_pane_mint_are_mutually_exclusive():
    identity = SessionIdentity("s", "/d", True, {"CLAUDE_SESSION_ID": "s"})
    with pytest.raises(ValueError):
        render_launch_script(["v"], "/w", identity=identity,
                             pane_mint=PaneMint("/k", "a", "/r"))


def test_shred_trap_is_armed_before_the_pane_mint():
    """The docstring's load-bearing word is armed FIRST; this source-level
    pin holds even where the tmux e2e cannot run."""
    script = render_launch_script(["v"], "/w", shebang=True,
                                  pane_mint=PaneMint("/kbin/session-key-init", "a", "/tmp/s"),
                                  secret_file="/tmp/secrets.env")
    assert script.index("trap __ss_shred") < script.index("keyed session: minted INSIDE the pane")


def test_the_pane_reaper_inherits_the_shred_trap_it_replaces(tmp_path):
    """`trap` is LAST-ONE-WINS per signal, and the pane mint arms its own
    EXIT/HUP/INT/TERM trap - which silently replaces the shred trap armed
    above it. So the reaper carries the shred too. Deterministic seam: a
    `cd` that cannot succeed sits AFTER the mint (the reaper trap has taken
    over) and BEFORE the prelude (the secret file is still on disk). No
    tmux: the trampoline is this text, and bash runs it directly."""
    secret = tmp_path / "secrets-file"
    secret.write_text("export TOKEN=LIVE-CREDENTIAL\n")
    secret.chmod(0o600)
    stub, _record = write_key_init_stub(tmp_path)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    script = render_launch_script(
        ["claude"], str(tmp_path / "no-such-work-dir"),
        pane_mint=PaneMint(key_init=stub, agent_name="scout", sessions_root=str(sessions)),
        env_prelude=render_env_prelude(parse_environment({"secrets": {"TOKEN": "pass:a/b"}}),
                                       secret_file=str(secret)),
        secret_file=str(secret))
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            timeout=TIMEOUT_S, env={"PATH": "/usr/bin:/bin"}, check=False)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (sessions / STUB_SID / "session-id").read_text() == STUB_SID   # the mint ran
    assert not secret.exists(), "the reaper trap replaced the shred trap and dropped its duty"


def test_a_failed_pane_mint_is_loud_and_never_reaches_the_vendor(tmp_path):
    stub, _record = write_key_init_stub(tmp_path, exit_code=1)
    marker = tmp_path / "vendor-ran"
    script = render_launch_script(["touch", str(marker)], str(tmp_path),
                                  pane_mint=PaneMint(stub, "scout", str(tmp_path / "s")))
    script = script.replace("sleep 15", "sleep 0")     # the hold is for eyes, not tests
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            timeout=TIMEOUT_S, env={"PATH": "/usr/bin:/bin"}, check=False)
    assert result.returncode == 1
    assert "session-key-init failed inside the pane" in result.stderr
    assert not marker.exists()


# ---------- units: {shortsid} when only the pane knows the id --------------------------------

def test_shortsid_in_the_pane_is_a_shell_expression():
    filled = expand_shortsid("seat+{shortsid}", None, pane_minted=True)
    assert filled == ShellExpr("seat+${CLAUDE_SESSION_ID:0:8}")
    assert render_argv_token(filled) == '"seat+${CLAUDE_SESSION_ID:0:8}"'
    # without a pane mint and without an identity, nothing to fill
    assert expand_shortsid("seat+{shortsid}", None) == "seat+{shortsid}"


def test_the_shortsid_expression_expands_to_the_minted_id_and_keeps_the_literal_safe():
    """Measured in bash, not read off the text: the literal parts survive
    `$`, `"`, backquote and backslash, and the slot becomes 8 hex."""
    token = 'a$HOME"`x`\\{shortsid}-{shortsid}'
    rendered = render_argv_token(expand_shortsid(token, None, pane_minted=True))
    result = subprocess.run(["bash", "-c", f"printf '%s' {rendered}"], capture_output=True,
                            text=True, timeout=TIMEOUT_S, check=False,
                            env={"PATH": "/usr/bin:/bin", "CLAUDE_SESSION_ID": "0123456789abcdef"})
    assert result.stdout == 'a$HOME"`x`\\01234567-01234567'


# ---------- units: the keyed-tmux plan ---------------------------------------------------------

def keyed_tmux_plan(tmp_path, *flags, front_matter=KEYED, stub=None):
    if stub is None:
        stub, _record = write_key_init_stub(tmp_path)
    return plan_for(tmp_path, front_matter, flags=("--in-tmux", *flags),
                    extra_env={"SESSION_KEY_INIT": stub})


def test_a_keyed_tmux_plan_defers_the_mint_to_the_pane(tmp_path):
    """The old launcher HARD-ERRORED on keyed x tmux once. The plan for it:
    no launcher-side identity, a PaneMint instead, the session-id slot a
    shell expansion, the sessions root made (the pane's landing ground) -
    and the primitive NOT invoked launcher-side (the stub records every
    invocation; there must be none)."""
    stub, record = write_key_init_stub(tmp_path)
    plan = keyed_tmux_plan(tmp_path, stub=stub)
    assert plan.identity is None
    assert plan.pane_mint == PaneMint(key_init=stub, agent_name="scout",
                                      sessions_root=str(tmp_path / "agents" / "sessions"),
                                      birth=json.dumps({  # Open 15: the launched file
                                          "agent": "scout",
                                          "agent_file": os.path.realpath(tmp_path / "agents" / "scout.md"),
                                      }, indent=2) + "\n")
    assert not os.path.exists(record), "the primitive ran launcher-side"
    assert os.path.isdir(plan.pane_mint.sessions_root)
    at = plan.argv.index("--session-id")
    assert plan.argv[at + 1] == ShellVar("CLAUDE_SESSION_ID")
    assert plan.script.startswith("#!/usr/bin/env bash\n")
    assert '"$CLAUDE_SESSION_ID"' in plan.script


def test_a_key_label_is_what_the_pane_mint_labels_the_key_with(tmp_path):
    plan = keyed_tmux_plan(tmp_path, "--key-label", "scout-ci")
    assert plan.pane_mint.agent_name == "scout-ci"
    assert "--agent-name scout-ci" in plan.script


def test_a_keyed_tmux_dry_run_creates_nothing_and_prints_the_live_script(tmp_path):
    """Same text both ways, so the report cannot drift from the pane - but
    for the one thing a dry run may not know: a live file's random name
    (`session-start-agent-plugin-XXXXXXXX`), which it spells by its
    representative name (`agent-plugin`)."""
    stub, record = write_key_init_stub(tmp_path)
    for sub in ("dry", "live"):
        (tmp_path / sub).mkdir()
    dry = plan_for(tmp_path / "dry", KEYED, flags=("--dry-run", "--in-tmux"),
                   extra_env={"SESSION_KEY_INIT": stub})
    assert not (tmp_path / "dry" / "agents" / "sessions").exists()
    live = plan_for(tmp_path / "live", KEYED, flags=("--in-tmux",),
                    extra_env={"SESSION_KEY_INIT": stub})
    assert not os.path.exists(record)
    live_text = re.sub(r"session-start-([a-z-]+)-[0-9a-f]{8}", r"\1",
                       live.script.replace(str(tmp_path / "live"), "<T>"))
    assert dry.script.replace(str(tmp_path / "dry"), "<T>") == live_text
    assert "--no-register" not in dry.script


def test_an_unkeyed_tmux_plan_mints_launcher_side(tmp_path):
    plan = plan_for(tmp_path, "", flags=("--in-tmux",))
    assert plan.pane_mint is None
    assert plan.identity is not None and os.path.isdir(plan.identity.session_dir)
    assert plan.script.startswith("#!/usr/bin/env bash\n")
    assert "\nexec claude" in plan.script


def test_a_direct_plan_has_no_shebang():
    """The shebang marks the trampoline; the direct path runs `bash -c`."""
    assert not render_launch_script(["v"], "/w").startswith("#!")


def test_keyed_tmux_shortsid_in_the_passthrough_is_filled_in_the_pane(tmp_path):
    stub, _record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, KEYED, flags=("--in-tmux",),
                    argv_tail=("--name", "seat+{shortsid}"),
                    extra_env={"SESSION_KEY_INIT": stub})
    assert ShellExpr("seat+${CLAUDE_SESSION_ID:0:8}") in plan.argv
    assert '"seat+${CLAUDE_SESSION_ID:0:8}"' in plan.script
    # ...and the provenance says so: no identity exists launcher-side, the
    # pane mints it (the old launcher's own words, its line 2897)
    (source,) = [src for token, src in plan.provenance
                 if token == ShellExpr("seat+${CLAUDE_SESSION_ID:0:8}")]
    assert source == "CLI passthrough ({shortsid} filled in the pane from the minted id)"


def test_keyed_tmux_missing_primitive_is_loud_at_prepare_time(tmp_path):
    with pytest.raises(LaunchError) as refused:
        keyed_tmux_plan(tmp_path, stub=str(tmp_path / "absent"))
    assert "SESSION_KEY_INIT" in str(refused.value)
    assert not (tmp_path / "agents" / "sessions").exists()


def test_door_guard_still_fires_on_the_pane_minted_session_id(tmp_path):
    """The ShellVar slot must not smuggle the session-id door past the
    guard: a declared `claude: session-id:` still collides with the door
    the launcher computed, spelled as an expansion or not."""
    with pytest.raises(LaunchError) as refused:
        keyed_tmux_plan(tmp_path, front_matter=KEYED + "claude:\n  session-id: mine\n")
    assert refused.value.code == EXIT_USAGE
    assert "--session-id" in str(refused.value)


# ---------- place_in_tmux against a fake tmux: the handover ------------------------------------

def live_plan(tmp_path):
    """A live unkeyed plan with files to write: the 0600 prompt, the plugin dir."""
    plan = plan_for(tmp_path, WITH_CONTEXT, flags=("--in-tmux",))
    assert plan.files and plan.identity is not None
    return plan


def paths(plan):
    return [item.path for item in plan.files]


def trampoline_of(plan):
    return os.path.join(plan.identity.session_dir, placement.TRAMPOLINE)


def no_tmux(*_args, **_kwargs):
    pytest.fail("this placement must never reach tmux")


def test_write_trampoline_is_the_plan_script_0700(tmp_path):
    plan = live_plan(tmp_path)
    path = placement.write_trampoline(plan)
    assert path == trampoline_of(plan)
    assert Path(path).read_text() == plan.script
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


def test_place_in_tmux_keeps_its_own_placement_guard(tmp_path, monkeypatch):
    """`main` asks first, but place_in_tmux is public and must still refuse
    a placement it cannot make - taking the files it wrote with it."""
    plan = live_plan(tmp_path)
    monkeypatch.setattr(placement, "run_tmux", no_tmux)
    with pytest.raises(LaunchError) as refused:
        placement.place_in_tmux(plan, "", "scout", False, environ={})
    assert refused.value.code == EXIT_USAGE
    assert not any(os.path.exists(p) for p in paths(plan))
    assert not os.path.exists(trampoline_of(plan))


def test_a_trampoline_that_cannot_be_written_takes_the_files(tmp_path, monkeypatch):
    """write_trampoline opens a file (and may mkdtemp): a full disk raises
    there, with the plan's files already written and no pane to own them."""
    plan = live_plan(tmp_path)

    def full_disk(_plan):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(placement, "write_trampoline", full_disk)
    monkeypatch.setattr(placement, "run_tmux", no_tmux)
    with pytest.raises(OSError):
        placement.place_in_tmux(plan, "eng", "scout", False, environ={"TMUX": "/x,1,2"})
    assert not any(os.path.exists(p) for p in paths(plan))


def test_a_failure_before_placement_takes_the_files_and_the_trampoline(tmp_path, monkeypatch):
    plan = live_plan(tmp_path)

    def refusing(arguments, environ=None):
        raise LaunchError(f"tmux {arguments[0]} failed: nope")

    monkeypatch.setattr(placement, "run_tmux", refusing)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    with pytest.raises(LaunchError):
        placement.place_in_tmux(plan, "eng", "scout", False, environ={})
    assert not any(os.path.exists(p) for p in paths(plan))
    assert not os.path.exists(trampoline_of(plan))


def test_a_failure_after_placement_never_robs_the_live_pane(tmp_path, monkeypatch, capsys):
    """`new-window` is the handover: after it the pane is ALIVE and about to
    read its files. A switch-client failure (no attached client, reachable
    with a forced --switch from outside tmux) is reported, and nothing is
    unwound - a window that exists is not a failure."""
    plan = live_plan(tmp_path)
    calls = []

    def after_fails(arguments, environ=None):
        calls.append(arguments[0])
        if arguments[0] == "new-window":
            return "@7\n"
        raise LaunchError(f"tmux {arguments[0]} failed: no current client")

    monkeypatch.setattr(placement, "run_tmux", after_fails)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    placed = placement.place_in_tmux(plan, "eng", "scout", True, environ={})
    assert placed == placement.TmuxPlacement("eng", "scout", "@7", False)
    assert calls == ["new-window", "set-window-option", "switch-client"]
    assert all(os.path.exists(p) for p in paths(plan))
    assert os.path.exists(trampoline_of(plan))
    err = capsys.readouterr().err
    assert "placed, but could not automatic-rename off" in err
    assert "placed, but could not switch the client to it" in err


def test_every_target_is_anchored_and_the_window_is_addressed_by_its_id(tmp_path, monkeypatch):
    """TM-1658: tmux matches `-t` by PREFIX, so every name is `=`-anchored;
    after new-window the window is its `@id`, which cannot collide."""
    plan = live_plan(tmp_path)
    calls = []

    def recording(arguments, environ=None):
        calls.append(list(arguments))
        return "@9\n" if arguments[0] == "new-window" else ""

    monkeypatch.setattr(placement, "run_tmux", recording)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: False)
    placed = placement.place_in_tmux(plan, "alpha", "scout", False, environ={})
    assert placed.created_session and placed.window_id == "@9"
    assert calls[0] == ["new-session", "-d", "-s", "alpha"]
    new_window = calls[1]
    assert new_window[:5] == ["new-window", "-d", "-P", "-F", placement.NEW_WINDOW_FORMAT]
    assert new_window[new_window.index("-t") + 1] == "=alpha:"
    assert new_window[new_window.index("-n") + 1] == "scout"
    assert new_window[-1] == trampoline_of(plan)
    assert calls[2] == ["set-window-option", "-t", "@9", "automatic-rename", "off"]
    assert len(calls) == 3                               # no switch asked for
    for call in calls:
        if "-t" in call:
            assert call[call.index("-t") + 1][0] in "=@", call


def test_a_server_born_by_new_session_never_gets_the_callers_identity(
        tmp_path, monkeypatch):
    """ST-113 (c): `new-session` may birth the server, and a server keeps its
    starter's environment for every pane it makes. Measured in repro-001 S4:
    a server born inside a seat held the PARENT's ids. Only `new-session` is
    scrubbed: `new-window` builds its pane from the server's environment."""
    plan = live_plan(tmp_path)
    caller = {"PATH": "/usr/bin", "HOME": "/home/x", "TERM": "xterm",
              "GIT_CONFIG_GLOBAL": "/agent/gitconfig",
              **{name: "parent-" + name for name in SESSION_IDENTITY_VARS},
              # ST-118: and a keyed parent's signing block, the indexed family
              "GIT_CONFIG_KEY_0": "user.signingkey", "GIT_CONFIG_VALUE_0": "key::parent"}
    envs = {}

    def recording(arguments, environ=None):
        envs[arguments[0]] = environ
        return "@9\n" if arguments[0] == "new-window" else ""

    monkeypatch.setattr(placement, "run_tmux", recording)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: False)
    placement.place_in_tmux(plan, "alpha", "scout", False, environ=caller)
    born = envs["new-session"]
    assert not set(SESSION_IDENTITY_VARS) & set(born)
    assert claude_code.session_id(born) is None
    assert born == {"PATH": "/usr/bin", "HOME": "/home/x", "TERM": "xterm",
                    "GIT_CONFIG_GLOBAL": "/agent/gitconfig"}
    assert envs["new-window"] is caller                  # narrow: new-session only
    assert caller["CLAUDE_CODE_SESSION_ID"] == "parent-CLAUDE_CODE_SESSION_ID"


def test_in_the_current_session_new_window_takes_no_target(tmp_path, monkeypatch):
    plan = live_plan(tmp_path)
    calls = []
    monkeypatch.setattr(placement, "run_tmux",
                        lambda arguments, environ=None: calls.append(list(arguments)) or "@3\n")
    monkeypatch.setattr(placement, "session_exists", no_tmux)
    placed = placement.place_in_tmux(plan, "", "scout", False, environ={"TMUX": "/x,1,2"})
    assert placed.session == "" and not placed.created_session
    assert calls[0][0] == "new-window" and "-t" not in calls[0]


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


def test_the_trampoline_path_is_one_shell_word_for_tmux(tmp_path, monkeypatch):
    """tmux runs a lone command argument through `sh -c`: a space, `$` or
    quote in TMPDIR (or AGENTS_DIR) must reach the pane as the same path."""
    odd = tmp_path / "tmp dir $HOME it's"
    odd.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(odd))
    plan = keyed_tmux_plan(tmp_path)
    calls = []

    def recording(arguments, environ=None):
        calls.append(list(arguments))
        return "@4\n" if arguments[0] == "new-window" else ""

    monkeypatch.setattr(placement, "run_tmux", recording)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    placement.place_in_tmux(plan, "eng", "scout", False, environ={})
    (pane_dir,) = odd.glob("session-start-pane-*")
    command = calls[0][-1]
    assert shlex.split(command) == [str(pane_dir / placement.TRAMPOLINE)]


def test_a_pane_minted_placement_uses_its_own_0700_temp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    plan = keyed_tmux_plan(tmp_path)
    monkeypatch.setattr(placement, "run_tmux",
                        lambda arguments, environ=None: "@4\n" if arguments[0] == "new-window" else "")
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    placed = placement.place_in_tmux(plan, "eng", "scout", False, environ={})
    assert placed.window_id == "@4"
    (pane_dir,) = (tmp_path / "tmp").glob("session-start-pane-*")
    assert stat.S_IMODE(pane_dir.stat().st_mode) == 0o700
    assert (pane_dir / placement.TRAMPOLINE).read_text() == plan.script


def test_a_pane_minted_placement_that_fails_leaves_no_temp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    plan = keyed_tmux_plan(tmp_path)

    def refusing(arguments, environ=None):
        raise LaunchError("tmux new-window failed: nope")

    monkeypatch.setattr(placement, "run_tmux", refusing)
    monkeypatch.setattr(placement, "session_exists", lambda name, environ=None: True)
    with pytest.raises(LaunchError):
        placement.place_in_tmux(plan, "eng", "scout", False, environ={})
    assert list((tmp_path / "tmp").iterdir()) == []


def test_the_receipt_names_who_where_the_window_id_and_the_session(tmp_path):
    plan = live_plan(tmp_path)
    sid = plan.identity.session_id
    created = placement.receipt(plan, placement.TmuxPlacement("eng", "scout", "@5", True))
    assert created == (f"session start: placed scout in tmux window 'scout' (@5) of "
                       f"session 'eng' (created) -- session {sid}")
    here = placement.receipt(plan, placement.TmuxPlacement("", "w", "@6", False))
    assert "of the current session -- session" in here
    (tmp_path / "k").mkdir()
    pane = placement.receipt(keyed_tmux_plan(tmp_path / "k"),
                             placement.TmuxPlacement("eng", "scout", "@7", False))
    assert pane.endswith("-- session id minted in the pane")


# ---------- main, in-process: dry runs and the usage refusals ----------------------------------

def test_dry_run_of_a_tmux_launch_shows_the_trampoline(tmp_path, monkeypatch, capsys):
    assert dry_run_main(tmp_path, monkeypatch, "--tmux-target", "gold:win", "scout") == EXIT_OK
    out = capsys.readouterr().out
    assert "mode:     tmux window 'win' in session 'gold'\n" in out
    assert "  #!/usr/bin/env bash\n" in out           # the trampoline, shebang and all


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


def test_nowhere_to_place_is_a_usage_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    assert start.main(["--in-tmux", "scout"]) == EXIT_USAGE
    assert "nowhere to place the window" in capsys.readouterr().err
    assert not (tmp_path / "agents" / "sessions").exists()


def test_contradictory_switch_flags_win_over_nowhere_to_place(tmp_path, monkeypatch, capsys):
    """Both refusals at once: `--switch --no-switch` is wrong wherever it
    was typed, so the more specific complaint is the one said."""
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    assert start.main(["--in-tmux", "--switch", "--no-switch", "scout"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "pick one" in err and "nowhere" not in err


# ---------- the tmux golden (parity check 1, tmux half) ----------------------------------------

def test_golden_dry_run_tmux(tmp_path):
    """The old suite's tmux golden, run by the PORT under the same harness
    as the direct golden. Byte-equal to the committed fixture, the old
    launcher's own, copied verbatim, plus the ST-18 launch-record lines."""
    root = make_agents_root(tmp_path, front_matter=GOLDEN_FRONT_MATTER)
    for sub in ("bin", "home", "work", "tmpdir"):
        (tmp_path / sub).mkdir()
    env = clean_env(AGENTS_DIR=root, HOME=str(tmp_path / "home"),
                    TMPDIR=str(tmp_path / "tmpdir"), TZ="UTC",
                    PATH=f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    env.pop("SESSIONS_DIR")          # the default root: <HOME>/.session/sessions
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
    neither the sessions root nor anything under it exists, the primitive
    never ran, and the report says where the pane WOULD land."""
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
    assert "session:  (keyed: minted inside the pane at boot)" in result.stdout
    assert f"  dir:    {sessions}/<id minted in the pane>" in result.stdout
    assert "<- computed: session identity (minted in the pane at boot)" in result.stdout
    assert "--no-register" not in result.stdout


# ---------- e2e: placement on a private server -------------------------------------------------

PANE_STUB = VENDOR_STUB + """\
printf '%%s' "$$"      > "%(dir)s/pid"
printf '%%s' "$PPID"   > "%(dir)s/ppid"
printf '%%s' "$PATH"   > "%(dir)s/path"
printf '%%s' "${CLAUDE_SESSION_AGENT_SOCK:-}" > "%(dir)s/agent-sock"
printf '%%s' "${CA209_HYGIENE:-<unset>}"      > "%(dir)s/declared-var"
: > "%(dir)s/done"
sleep 45
"""


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


def tmux_launch_e2e(tmp_path, *verb_args, front_matter="claude:\n  effort: high\n",
                    extra_env=None, tmux=None, agent="scout", stub_body=PANE_STUB):
    """The verb as a subprocess, placing into the private server, the stub
    vendor recording from inside the pane. The launcher runs AS IF from its
    own venv (VIRTUAL_ENV set, its bin on PATH), so the scrub the pane test
    checks is not vacuous; HOME is the test's, so the private server reads
    no user tmux.conf. Returns (result, record, work)."""
    record = tmp_path / "record"
    record.mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(stub_body % {"dir": record})
    stub.chmod(0o755)
    root = make_agents_root(tmp_path, name=agent, front_matter=front_matter)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    launcher_tmp = tmp_path / "tmpdir"     # a pane-minted launch's files land in TMPDIR
    launcher_tmp.mkdir(exist_ok=True)
    env = {key: value for key, value in clean_env().items()
           if key not in ("PYTHONPATH", "PYTHONHOME") and not key.startswith("UV_")}
    env.update(AGENTS_DIR=root, SESSIONS_DIR=os.path.join(root, "sessions"),
               HOME=str(home), TMPDIR=str(launcher_tmp),
               VIRTUAL_ENV=sys.prefix, PATH=e2e_path(bin_dir))
    result = subprocess.run(session_cmd(*verb_args), capture_output=True, text=True,
                            timeout=TIMEOUT_S, cwd=str(work), env=private_env({**env, **(extra_env or {})}, tmux),
                            check=False)
    return result, record, work


def pane_ran(record: Path) -> bool:
    return wait_for(lambda: (record / "done").exists())


def no_session_dirs(tmp_path) -> bool:
    sessions = tmp_path / "agents" / "sessions"
    return not sessions.exists() or list(sessions.iterdir()) == []


def test_tmux_placement_lands_and_the_pane_sees_no_venv(tmp_path):
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "scout")
    assert result.returncode == 0, result.stderr
    assert "placed scout in tmux window 'scout'" in result.stdout
    assert "of session 'eng' (created)" in result.stdout
    assert pane_ran(record), f"the pane never ran the vendor: {result.stdout}"
    argv = recorded_argv(record)
    sid = (record / "session-id-env").read_text()
    assert sid and argv[argv.index("--session-id") + 1] == sid
    root = tmp_path / "agents"
    assert (root / "sessions" / sid / "session-id").read_text() == sid
    # the venv scrub holds IN THE PANE, not just in the harness
    assert (record / "virtual-env").read_text() == ""
    assert os.path.join(sys.prefix, "bin") not in (record / "path").read_text().split(":")
    # the trampoline lives in the session dir, 0700, shebang first
    trampoline = root / "sessions" / sid / placement.TRAMPOLINE
    assert stat.S_IMODE(trampoline.stat().st_mode) == 0o700
    assert trampoline.read_text().startswith("#!/usr/bin/env bash\n")


def test_a_space_in_the_agents_dir_still_lands_the_pane(tmp_path):
    """The trampoline lives in the session dir under AGENTS_DIR; tmux hands
    its path to `sh -c`, so unquoted it split at the space and the pane
    never ran."""
    spaced = tmp_path / "agents home"
    spaced.mkdir()
    result, record, _work = tmux_launch_e2e(spaced, "--tmux-target", "eng", "scout")
    assert result.returncode == 0, result.stderr
    assert pane_ran(record), f"the pane never ran the vendor: {result.stdout}"
    sid = (record / "session-id-env").read_text()
    assert (spaced / "agents" / "sessions" / sid / placement.TRAMPOLINE).exists()


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
    assert pane_ran(record), result.stdout
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
sleep 45
"""


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
    assert pane_ran(record), result.stdout
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
    assert tmux_in("new-session", "-d", "-s", "home").returncode == 0
    facts = tmux_in("display-message", "-p", "-t", "=home:",
                    "#{socket_path},#{pid},#{session_id}").stdout.strip()
    socket, pid, session_id = facts.split(",")
    result, record, _work = tmux_launch_e2e(
        tmp_path, "--in-tmux", "scout", tmux=f"{socket},{pid},{session_id.lstrip('$')}")
    assert result.returncode == 0, result.stderr
    assert "of the current session" in result.stdout
    names = tmux_in("list-windows", "-t", "=home", "-F", "#{window_name}").stdout.splitlines()
    assert "scout" in names
    assert pane_ran(record), result.stdout


def test_keyed_in_pane_reaper_watches_the_panes_own_pid(tmp_path):
    """The CA-152 wiring, measured in the pane: the pid handed to --reap-pid
    is the pid whose life IS the pane's. The keyed trampoline does not exec,
    so that pid is the pane's bash - the vendor's PARENT."""
    stub, key_record = write_key_init_stub(tmp_path)
    result, record, _work = tmux_launch_e2e(tmp_path, "--tmux-target", "eng", "--keyed",
                                            "scout", extra_env={"SESSION_KEY_INIT": stub})
    assert result.returncode == 0, result.stderr
    assert "-- session id minted in the pane" in result.stdout
    assert pane_ran(record), result.stdout
    key_argv = Path(key_record).read_text().split()
    assert key_argv[:4] == ["--agent-name", "scout", "--session-dir",
                            str(tmp_path / "agents" / "sessions")]
    assert key_argv[key_argv.index("--reap-pid") + 1] == (record / "ppid").read_text()
    # the pane expanded the ShellVar: the vendor really got the minted id
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
        assert pane_ran(record), result.stdout
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
    assert pane_ran(record), result.stdout
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
    assert pane_ran(record), result.stdout
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


def test_a_placement_with_nowhere_to_go_is_refused_before_anything_is_minted(tmp_path):
    result, _record, _work = tmux_launch_e2e(tmp_path, "--in-tmux", "scout")
    assert result.returncode == EXIT_USAGE, result.stdout
    assert "nowhere to place the window" in result.stderr
    assert no_session_dirs(tmp_path), "minted an identity before a usage question"


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
