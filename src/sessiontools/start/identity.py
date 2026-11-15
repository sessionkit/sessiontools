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
import signal
import subprocess
import uuid
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools.start.agents import Agent, sessions_root
from sessiontools.start.environment import host_environ, without_session_identity
from sessiontools.start.errors import LaunchError

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


def expand_shortsid(token: object, identity: SessionIdentity | None) -> object:
    """`token` with `{shortsid}` filled from the identity: the first 8 hex
    of the id, or `<shortsid>` in a keyed dry run (no id exists yet).
    Anything that is not a str carrying the token passes through as is."""
    if not isinstance(token, str) or SHORTSID_TOKEN not in token:
        return token
    if identity is not None:
        sid = identity.session_id
        short = SHORTSID_DRY_RUN if sid == DRY_RUN_KEYED_ID else sid[:SHORTSID_LEN]
        return token.replace(SHORTSID_TOKEN, short)
    return token
