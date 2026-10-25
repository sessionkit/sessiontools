"""Who the CHILD will be: the session identity a launch mints, or projects.

NOT `sessiontools.session_id()` / `current()`. Those answer "who am I" -
the running session's own id, read from its environment - and they are the
obvious thing to reach for here, which is why this note exists. The
launcher decides who the session it STARTS will be, and it deliberately
sheds its caller's identity: the launch script's first line unsets
CLAUDE_SESSION_ID and the key block. A launcher running inside a session
must never hand its own id down; the old launcher's author lost a live
signing key to exactly that leak (a keyed launcher's keyholder, inherited
by its child, killed by the child's reaper).

Two paths, both from the old launcher:

    unkeyed  a uuid4; its session directory and a `session-id` anchor
    keyed    `session-key-init` (driven as a subprocess; it
             lives in claude-agents and is never absorbed here) mints an
             Ed25519 key, derives the id from it, creates the directory and
             prints an env block, which this module parses

A dry run PROJECTS instead: the unkeyed id is computable without acting (a
uuid4), the keyed one is not (it is born with the key), so its slot reads
`<keyed-session-id-minted-at-launch>` - deliberately not uuid-shaped. The
suppression sits at the lines that touch the world, never in a second path
a report could drift from.

`{shortsid}`: the one token a spawner may leave for the launcher to fill,
the first 8 hex of the id. tm writes
`-n <seat>+{shortsid}` and the launcher fills it in vendor passthrough.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import uuid
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools.start.agents import Agent, sessions_root
from sessiontools.start.environment import host_environ, without_session_identity
from sessiontools.start.errors import LaunchError
from sessiontools.start.vendor import ShellExpr

KEY_INIT_ENV = "SESSION_KEY_INIT"
DEFAULT_KEY_INIT = "~/.claude/agents/lib/session-keys/session-key-init"
# How long a detached mint gets, after a forwarded Ctrl-C, to run its own
# EXIT trap (which kills a half-born keyholder) before the launcher re-raises.
INTERRUPT_GRACE_SECONDS = 2.0

DRY_RUN_KEYED_ID = "<keyed-session-id-minted-at-launch>"

SHORTSID_TOKEN = "{shortsid}"
SHORTSID_LEN = 8
SHORTSID_DRY_RUN = "<shortsid>"


class SessionIdentity(NamedTuple):
    """A session identity and the env that carries it into the child.

    env        exactly what the launched process inherits: CLAUDE_SESSION_ID,
               and for a keyed session the key-init block (socket and pubkey
               PATHS - the private key lives only in the ssh-agent)
    minted     False for a dry run's projection: nothing created, nothing
               spawned
    mint_command  the keyed primitive's argv - run, or under a dry run the
               command that WOULD run - recorded where it is built so the
               report never reconstructs it
    """

    session_id: str
    session_dir: str
    keyed: bool
    env: dict
    minted: bool = True
    mint_command: tuple = ()


# The session tool's own settings file, written at birth: the identity
# (agent, seat) in the session layer's session.json. `session settings` reads the
# agent and the seat from it to find the agent and seat layers.
BIRTH_SETTINGS = os.path.join("settings", "session.json")


def birth_settings(agent: Agent, seat: str | None = None) -> dict:
    """What the birth knows of who this session is: the agent's name, the
    agent FILE it was launched from, and the seat it takes, each only when
    there is one. A bare session with no seat gets {} and no file (no empty
    dirs). With `agent_file`, the settings reader takes the agent layer from
    the file the launch used (a plugin's agent MD, say), never a copy of its
    values."""
    data = {}
    if agent.name:
        data["agent"] = agent.name
    if agent.name and agent.file and not agent.builtin:
        # Resolved, so the record names the real file, not a link a later
        # tidy-up may remove. Never the default seat's built-in: its path is
        # the package's, which moves with the install, so a later read would
        # call it gone; its settings layer is read by name.
        data["agent_file"] = os.path.realpath(agent.file)
    if seat:
        data["seat"] = seat
    return data


def write_birth_settings(session_dir: str, data: dict) -> str | None:
    """`<session_dir>/settings/session.json`, written once, atomically.
    Nothing to say, nothing written. Returns the path, or None."""
    if not data:
        return None
    path = os.path.join(session_dir, BIRTH_SETTINGS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def key_init_path(environ: Mapping[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ
    return os.path.expanduser(environ.get(KEY_INIT_ENV) or DEFAULT_KEY_INIT)


def mint_identity(agent: Agent, root: str | None = None, keyed: bool = False,
                  environ: Mapping[str, str] | None = None,
                  reap_pid: int | None = None, no_register: bool = False,
                  dry_run: bool = False, key_label: str | None = None,
                  seat: str | None = None,
                  ) -> SessionIdentity:
    """This launch's identity: minted, or under `dry_run` projected.

    key_label is session-key-init's `--agent-name` - the key comment, the
    birth certificate, the registry principal a verified signature prints
    (a spawner passes the seat's mail address). Default: the agent name.
    `--reap-pid` defaults to THIS
    process: the direct path execs into the vendor, so this pid IS the
    session's lifetime. A failed keyed mint is fatal - a session that fell
    back to an unkeyed id would defeat asking for a key. A keyed DRY run
    still checks the primitive exists: a report of a launch that could not
    happen is worse than an error.
    """
    root = sessions_root(agent, environ=environ) if root is None else root
    if not keyed:
        session_id = str(uuid.uuid4())
        session_dir = os.path.join(root, session_id)
        if not dry_run:
            os.makedirs(session_dir, exist_ok=True)
        identity = SessionIdentity(
            session_id=session_id, session_dir=session_dir, keyed=False,
            env={"CLAUDE_SESSION_ID": session_id}, minted=not dry_run)
    else:
        tool = key_init_path(environ)
        if not os.access(tool, os.X_OK):
            raise LaunchError(
                f"keyed launch: session-key-init not found/executable at {tool} "
                f"(set {KEY_INIT_ENV} to point at it)")
        command = [tool, "--agent-name", key_label or agent.name,
                   "--session-dir", root,
                   "--reap-pid", str(reap_pid or os.getpid())]
        if no_register:
            command.append("--no-register")
        if dry_run:
            return SessionIdentity(
                session_id=DRY_RUN_KEYED_ID,
                session_dir=os.path.join(root, DRY_RUN_KEYED_ID),
                keyed=True, env={"CLAUDE_SESSION_ID": DRY_RUN_KEYED_ID},
                minted=False, mint_command=tuple(command))
        # The CALLER's identity stays out of the primitive too. Its
        # lamp-check commits the birth to the registry under whatever git
        # signing block it inherits: a keyed parent's block with this
        # child's socket cannot sign (`Couldn't find key in agent`), and the
        # birth went unregistered. key-init reads none of these names; it
        # hands its own socket to lamp-check and session-death by name.
        result = run_key_init(command, without_session_identity(host_environ(environ)))
        if result.returncode != 0:
            raise LaunchError("keyed launch: session-key-init failed "
                              f"(exit {result.returncode})")
        env = {}
        for line in result.stdout.splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip():
                env[name.strip()] = value
        session_id = env.get("CLAUDE_SESSION_ID", "")
        if not session_id:
            raise LaunchError("keyed launch: session-key-init returned no "
                              "CLAUDE_SESSION_ID")
        identity = SessionIdentity(
            session_id=session_id, session_dir=os.path.join(root, session_id),
            keyed=True, env=env, mint_command=tuple(command))
    if identity.minted:
        # The anchor is a WRITE into a directory a dry run did not create -
        # gated on the same flag, not on the branch above.
        with open(os.path.join(identity.session_dir, "session-id"), "w",
                  encoding="utf-8") as handle:
            handle.write(identity.session_id)
        write_birth_settings(identity.session_dir, birth_settings(agent, seat))
    return identity


def run_key_init(command: list[str], env: Mapping[str, str]) -> subprocess.CompletedProcess:
    """Run session-key-init: stdout (the env block) captured, stderr flowing.

    Detached into its OWN session (start_new_session) so the
    primitive's reaper outlives the pane. The reaper is a `( ... ) & disown`
    subshell from a non-interactive bash: no job control, so it inherits the
    process group and session it was born in. Born in the launcher's, it sat
    in the pane tty's foreground group and was SIGHUPed with the seat on
    every death (kill-window, SIGKILL, a normal exit), before it could reap;
    the daemonized ssh-agent lived on. In its own
    session it keeps polling `--reap-pid` (this pid, which execs into the
    vendor) and reaps within REAP_POLL_SECONDS of any death.

    stdin stays INHERITED, so session-bless's `[[ -t 0 ]]` gate reads as
    before. But a detached mint cannot open /dev/tty, which a desk blessing
    with a PIN or passphrase would need. So a launch carrying LAMP_BLESS
    (the desk-only root-blessing ceremony; a kick never sets it) is NOT
    detached and runs exactly as before. RESIDUAL: a blessed direct-path
    launch therefore still leaks its keyholder when it dies, as every
    launch once did.

    Detached, a Ctrl-C at the pane reaches only the launcher. Left to
    `subprocess.run`, the child would be SIGKILLed and its EXIT trap (which
    kills a half-born keyholder) would never run. So the interrupt is
    forwarded to the mint's process group, as the tty would have delivered
    it, and the mint gets INTERRUPT_GRACE_SECONDS to clean up. Its
    reaper ignores SIGINT (an async list without job control) and still
    watches this pid, which exits on the re-raise.
    """
    if env.get("LAMP_BLESS"):
        return subprocess.run(command, stdout=subprocess.PIPE, text=True,
                              env=env, check=False)  # stderr flows
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=env,
                          start_new_session=True) as proc:  # stderr flows
        try:
            stdout, _ = proc.communicate()
        except KeyboardInterrupt:
            forward_interrupt(proc)
            raise
    return subprocess.CompletedProcess(command, proc.returncode, stdout)


def forward_interrupt(proc: subprocess.Popen,
                      grace: float = INTERRUPT_GRACE_SECONDS) -> None:
    """SIGINT a detached child's process group, then wait at most `grace`
    seconds. Never escalate to SIGKILL: a SIGKILL skips the child's EXIT trap,
    and so loses the cleanup the forward exists to give it."""
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        return  # the whole group is already gone
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _dq_escape(literal: str) -> str:
    """`literal` made safe inside a double-quoted bash string."""
    return (literal.replace("\\", "\\\\").replace('"', '\\"')
            .replace("$", "\\$").replace("`", "\\`"))


def expand_shortsid(token: object, identity: SessionIdentity | None,
                    pane_minted: bool = False) -> object:
    """`token` with `{shortsid}` filled from the identity: the first 8 hex
    of the id, or `<shortsid>` in a keyed dry run (no id exists yet). A
    keyed launch placed in tmux has no id launcher-side - only the pane
    will know it - so there the slot becomes a ShellExpr over
    `${CLAUDE_SESSION_ID:0:8}`, expanded when the trampoline runs.
    Anything that is not a str carrying the token passes through as is."""
    if not isinstance(token, str) or SHORTSID_TOKEN not in token:
        return token
    if identity is not None:
        sid = identity.session_id
        short = SHORTSID_DRY_RUN if sid == DRY_RUN_KEYED_ID else sid[:SHORTSID_LEN]
        return token.replace(SHORTSID_TOKEN, short)
    if pane_minted:
        parts = [_dq_escape(part) for part in token.split(SHORTSID_TOKEN)]
        return ShellExpr(f"${{CLAUDE_SESSION_ID:0:{SHORTSID_LEN}}}".join(parts))
    return token


# ---------- the keyed mint, inside a tmux pane ----------------------------------------------

class PaneMint(NamedTuple):
    """A keyed launch placed in tmux mints INSIDE THE PANE, so the key's
    ssh-agent and its reaper belong to the pane's lifetime. Minting here and
    reaping the launcher would be a false death certificate seconds
    after placement, because the launcher exits once the window is up.

    `--reap-pid "$$"` is right in the pane for a reason of its own: that
    trampoline does NOT exec (the pane-lifetime trap needs a shell to fire
    in), so `$$` stays the pane's bash, the vendor runs as its child, and the
    pid the reaper watches is the one whose life IS the pane's."""

    key_init: str        # the primitive, resolved launcher-side
    agent_name: str      # its --agent-name: the key label, else the agent
    sessions_root: str   # the primitive creates <root>/<sid>/
    birth: str = ""      # settings/session.json's text, written in the pane; "" = none


def pane_mint_for(agent: Agent, environ: Mapping[str, str] | None = None,
                  key_label: str | None = None, dry_run: bool = False,
                  seat: str | None = None) -> PaneMint:
    """The PaneMint for a keyed tmux launch. Checks the primitive exists
    now, launcher-side, where the refusal can still be read: a pane that
    only finds out at boot shows its error for 15 seconds and closes. A
    live launch creates the sessions root (the pane's landing ground); a
    dry run creates nothing, and mints nothing, but prints the trampoline a
    live launch would run - the same text, so the report cannot drift."""
    tool = key_init_path(environ)
    if not os.access(tool, os.X_OK):
        raise LaunchError(
            f"keyed launch: session-key-init not found/executable at {tool} "
            f"(set {KEY_INIT_ENV} to point at it)")
    root = sessions_root(agent, environ=environ)
    if not dry_run:
        os.makedirs(root, exist_ok=True)
    data = birth_settings(agent, seat)
    return PaneMint(key_init=tool, agent_name=key_label or agent.name,
                    sessions_root=root,
                    birth=json.dumps(data, indent=2) + "\n" if data else "")


def render_pane_mint(mint: PaneMint) -> str:
    """The trampoline block that mints the keyed identity inside the pane.

    It drives the same contract `mint_identity` drives launcher-side:
    an env block on stdout, applied line by line and split on the first
    `=`. A failed mint is loud and HOLDS THE PANE OPEN for 15 seconds,
    because a pane that dies at once eats its own error. The session-id
    anchor is written here, since only the pane knows the id.

    THE PANE-LIFETIME TRAP, measured rather than designed on paper: the
    primitive's own reaper is a backgrounded subshell in the pane's process
    group, so `tmux kill-window` HUPs it dead in the same stroke as the
    vendor, while the keyholder ssh-agent, which daemonized into its own
    session, survives - an orphaned live key with no reaper. So the pane's
    own bash, whose death IS the pane's, does the reaper's two acts: signs
    the death certificate through the primitive's `session-death`, then
    kills the keyholder by the pid the env block handed over. The internal
    reaper stays armed as well (the vendor can crash while the pane lives);
    the flag makes the reap idempotent when both HUP and EXIT arrive.

    `trap` is last-one-wins per signal, so this trap REPLACES the shred trap
    the script armed first, and inherits its duty: it shreds the secret
    file too, when the script defined `__ss_shred`."""
    root = shlex.quote(mint.sessions_root)
    death = shlex.quote(os.path.join(os.path.dirname(mint.key_init), "session-death"))
    command = " ".join(shlex.quote(part) for part in (
        mint.key_init, "--agent-name", mint.agent_name, "--session-dir", mint.sessions_root))
    session = root + '/"$CLAUDE_SESSION_ID"'
    return "\n".join([
        "# keyed session: minted INSIDE the pane -- the key's ssh-agent and",
        "# reaper must belong to the pane's lifetime, not to the",
        "# launcher's.",
        f'__ss_env_block="$({command} --reap-pid "$$")" || {{',
        "  echo 'session start: session-key-init failed inside the pane' >&2",
        "  sleep 15   # hold the pane so the failure can actually be read",
        "  exit 1",
        "}",
        "while IFS= read -r __ss_line; do",
        '  case "$__ss_line" in *=*) export "$__ss_line";; esac',
        'done <<<"$__ss_env_block"',
        f"printf '%s' \"$CLAUDE_SESSION_ID\" > {session}/session-id",
        *([(f"mkdir -p {session}/settings && printf '%s' {shlex.quote(mint.birth)} "
            f"> {session}/{BIRTH_SETTINGS}")] if mint.birth else []),
        "__ss_reaped=''",
        "__ss_reap() {",
        '  [ -n "$__ss_reaped" ] && return',
        "  __ss_reaped=1",
        "  declare -F __ss_shred >/dev/null 2>&1 && __ss_shred",
        f"  if [ -x {death} ]; then",
        (f'    CLAUDE_SESSION_AGENT_SOCK="$CLAUDE_SESSION_AGENT_SOCK" {death} '
         f"{session} {shlex.quote(mint.agent_name)} >> {session}/death.log 2>&1 || true"),
        "  fi",
        '  [ -n "${CLAUDE_SESSION_AGENT_PID:-}" ] && kill "$CLAUDE_SESSION_AGENT_PID" 2>/dev/null',
        "}",
        "trap __ss_reap EXIT HUP INT TERM",
    ])
