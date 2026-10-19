"""The launch, planned: argv in, a LaunchPlan out, and nothing launched.

Three stages, and why there are three:

    resolve(args)          PURE. The agent, its file, the Fable gate, where
                           it runs, the keyed rules - every refusal that
                           needs no act, all of them before any act.
    mint(launch)           THE ONE ACT BEFORE A PLAN, live launches only: a
                           keyed session's id is born with its key, and the
                           context generators must see it (launch-info says
                           it; the wake runs under it). Under --dry-run it
                           projects and does nothing. A keyed launch placed
                           in tmux mints IN THE PANE instead, so
                           here it only plans that mint: a PaneMint.
    assemble(launch, id)   PURE but for the agent file's own generators,
                           which run by the old contract - their stdout IS
                           argv. Builds the argv with per-token provenance,
                           the door guard, and the launch script. It names
                           the files a live launch writes; it writes none.

`prepare(args)` is the three in a row. `run.execute(plan)` is the rest of
the acts: the 0600 prompt and first-message files, the 0700 plugin dir,
the window name, the exec. `placement.place_in_tmux(plan, ...)` is the
other ending: the same files, then the plan's script as a 0700 trampoline
in a new tmux window.
"""

from __future__ import annotations

import argparse
import itertools
import os
import shlex
import sys
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools import settings as _settings
from sessiontools.start import agentfile, claude_settings
from sessiontools.start.agentfile import AgentFile, StartOptions, switch_enabled
from sessiontools.start.agents import (
    Agent,
    agent_subject,
    resolve_agent_or_bare,
    sessions_root,
)
from sessiontools.start.context import (
    DRY_RUN_MESSAGE_NAME,
    FIRST_MESSAGE_POINTER_LEAD,
    MESSAGE_PREFIX,
    InjectedContext,
    PlannedFile,
    PlannedPlugin,
    agent_plugin_argv,
    build_injected_context,
    context_builtins,
    initial_message_argv,
    planned_path,
    settings_argv,
    system_prompt_argv,
)
from sessiontools.start.environment import (
    DRY_RUN_SECRET_NAME,
    RELATIVE_PREFIX,
    DeclaredSecret,
    declared_secrets,
    host_environ,
    render_env_prelude,
    render_identity_unset,
    warn_env_file_traps,
    without_session_identity,
)
from sessiontools.start.errors import EXIT_USAGE, PROG, LaunchError
from sessiontools.start.identity import (
    PaneMint,
    SessionIdentity,
    expand_shortsid,
    mint_identity,
    pane_mint_for,
    render_pane_mint,
)
from sessiontools.start.policy import (
    CommitterIdentity,
    check_fable_gate,
    resolve_committer_identity,
)
from sessiontools.start.record import (
    LaunchRecord,
    record_path,
    render_record_call,
    vendor_startup_files,
)
from sessiontools.start.resume import (
    ResumeRequest,
    detect_resume,
    keyed_resume_warning,
    recover_resume_cwd,
    resume_message,
)
from sessiontools.start.secrets import SECRET_FILE_PREFIX, SecretResult, read_inherited
from sessiontools.start.vendor import (
    VENDORS,
    ShellExpr,
    ShellVar,
    door_is_opened_by,
    guard_computed_doors,
    render_argv_token,
    render_vendor_args,
    split_rest,
    vendor_front_matter_argv,
)


class Launch(NamedTuple):
    """Everything resolved from argv, the agent file and the environment,
    before any act."""

    args: argparse.Namespace
    environ: Mapping[str, str]
    agent: Agent
    agent_file: AgentFile
    message_words: list
    cli_args: list
    work_dir: str
    work_dir_source: str
    keyed: bool
    first_message_limit: int
    in_tmux: bool = False
    resume: ResumeRequest | None = None

    @property
    def start(self) -> StartOptions:
        return self.agent_file.start

    @property
    def dry_run(self) -> bool:
        return bool(self.args.dry_run)


class LaunchPlan(NamedTuple):
    """Everything a launch will do, inspectable. `argv` is derived from
    `provenance` (the same tokens, annotated), so what --dry-run prints and
    what a launch execs cannot drift apart. `files` is what `execute`
    writes before its exec - empty for a dry run, which writes nothing.
    `identity` is None when `pane_mint` is set (a keyed launch placed in
    tmux, whose identity is born in the pane) and on an unkeyed resume (the
    resumed session keeps its own id; minting one would lie). `resume` is
    True for a claude resume: direct path, no generators, the restart
    message as the first message."""

    vendor: str
    argv: list
    script: str
    work_dir: str
    work_dir_source: str
    agent: Agent
    identity: SessionIdentity
    injected: InjectedContext
    provenance: list
    secrets: list
    files: tuple
    dry_run: bool
    pane_mint: PaneMint | None = None
    resume: bool = False
    # Where the prelude sources the 0600 secret file from: None without
    # declared secrets, the representative `secrets.env` under a dry run,
    # else a fresh random name that `run.write_files` creates, O_EXCL, once
    # the secrets resolve. The script names it; nothing is in it
    # until the launch acts.
    secret_file: str | None = None
    # The launch record the script writes before the vendor starts
    # (`record`); None on a resume, which writes none.
    record: LaunchRecord | None = None
    # The session's home from the `session` tool's
    # cascade, exported to the launched process as SESSION_HOME; None is no
    # home, the default.
    home: _settings.Home | None = None
    # The `environment: inherit:` names, and on a live launch what
    # the launcher's own environment held for each (`secrets.read_inherited`:
    # a value never in a repr). They ride the secret file; a dry run names
    # them and reads nothing, so `inherited` is empty there.
    inherit: tuple = ()
    inherited: tuple = ()


# The old launcher's refusal, word for word: nothing can place a resumed
# session, so nothing is honoured, and it says so (accepted-and-ignored was
# the launcher before it).
RESUME_UNDER_TMUX = ("resume is a direct launch in v1: --in-tmux/--tmux-target "
                     "cannot place a resumed session (drop the tmux flag, or start "
                     "a fresh session in tmux instead)")

FORK_SESSION = "--fork-session"


def vendor_cli_args(launch: Launch) -> list:
    """The CLI passthrough the vendor gets: as typed, plus `--fork-session`
    for a keyed resume that did not already say it. A keyed resume is a
    FORK - the original key died with its process, so the resumed turns
    continue under a fresh keyed id (claude: `--resume A --fork-session
    --session-id B` creates B carrying A's turns, A untouched)."""
    if launch.resume is not None and launch.keyed and FORK_SESSION not in launch.cli_args:
        return [*launch.cli_args, FORK_SESSION]
    return list(launch.cli_args)


# ---------- stage 1: resolve --------------------------------------------------

def choose_work_dir(args: argparse.Namespace, start: StartOptions,
                    work_dir: str | None = None) -> tuple[str, str]:
    """WHERE THE SESSION RUNS, and why: `--directory` (the operator, this
    launch), then `start: directory:` (the agent's home), then where it was typed. A NAMED directory that does
    not exist is a refusal, never a fallback."""
    if args.directory:
        chosen, source = os.path.expanduser(args.directory), "--directory"
    elif start.directory:
        chosen, source = start.directory, "agent file: start: directory:"
    else:
        return os.path.abspath(work_dir or os.getcwd()), "cwd"
    chosen = os.path.abspath(chosen)
    if not os.path.isdir(chosen):
        raise LaunchError(f"{source} names {chosen}, which is not a directory -- refusing "
                          "to launch somewhere else",
                          code=EXIT_USAGE)
    return chosen, source


def resolve(args: argparse.Namespace, environ: Mapping[str, str] | None = None,
            work_dir: str | None = None) -> Launch:
    """argv + the agent file + the environment -> a Launch, or a refusal.
    Acts on nothing."""
    environ = os.environ if environ is None else environ
    agent = resolve_agent_or_bare(args.agent, environ=environ)
    agent_file = agentfile.load(agent)
    start = agent_file.start
    message_words, cli_args = split_rest(args.rest)
    # THE FABLE GATE: the earliest point the vendor, the argv and the
    # environment all exist, and before anything below mints or writes.
    fable_gate(agent_file, cli_args, environ)
    in_tmux = bool(args.in_tmux or args.tmux_target)
    resume = detect_resume(start.vendor, cli_args)
    if resume is None:
        chosen, source = choose_work_dir(args, start, work_dir)
        warn_env_file_traps(agent_file.environment, chosen)
    else:
        # A resume never reads --directory or `start: directory:`: claude
        # scopes a session to the directory it was born in, so the only
        # directory that matters is that one (`assemble_resume`),
        # else where the caller stands - the old launcher's order exactly.
        if in_tmux:
            raise LaunchError(RESUME_UNDER_TMUX, code=EXIT_USAGE)
        chosen, source = os.path.abspath(work_dir or os.getcwd()), "cwd"
    keyed = bool(args.keyed or start.keyed)
    limit = (start.first_message_file_over if args.first_message_file_over is None
             else args.first_message_file_over)
    if limit < 0:
        raise LaunchError("--first-message-file-over must be a non-negative byte "
                          f"count (0 = always a file), got {limit}", code=EXIT_USAGE)
    if args.key_label and not keyed:
        raise LaunchError(
            "--key-label names what the minted key is labelled with, and this "
            "launch mints no key: add --keyed (or start: keyed: true in the "
            "agent file), or drop --key-label.", code=EXIT_USAGE)
    if keyed and not agent.name:
        # session-key-init labels the key, birth cert and registry entry
        # with --agent-name; a plain session has none to give. Refuse
        # rather than invent one.
        raise LaunchError(
            "--keyed needs an agent: the key, its birth certificate and the "
            "registry entry carry the agent name, and a plain session has "
            "none. Name an agent, or launch unkeyed.", code=EXIT_USAGE)
    if keyed and resume is not None and not resume.session_id:
        raise LaunchError(
            "a keyed resume is a FORK into a fresh identity, and a fork "
            "needs the session named: `--resume <id>` (a bare --resume/"
            "--continue picker has nothing to fork from). Name the "
            "session, or resume unkeyed.", code=EXIT_USAGE)
    launch = Launch(args=args, environ=environ, agent=agent, agent_file=agent_file,
                    message_words=message_words, cli_args=cli_args, work_dir=chosen,
                    work_dir_source=source, keyed=keyed, first_message_limit=limit,
                    in_tmux=in_tmux, resume=resume)
    rehearse_doors(launch)
    return launch


def fable_gate(agent_file: AgentFile, cli_args: list,
               environ: Mapping[str, str]) -> None:
    """THE FABLE GATE, fed: the vendor this file launches, its `claude:`
    section, the CLI passthrough, the environment, and the file's declared
    `variables:`. `resolve` feeds it for every road - direct, tmux, resume,
    declared secrets - before anything is minted, written or resolved."""
    check_fable_gate(agent_file.start.vendor, agent_file.vendor_section, cli_args,
                     environ, agent_file.environment.variables)


def rehearse_doors(launch: Launch) -> None:
    """Refuse a door opened twice BEFORE the mint. Which doors the launcher
    computes is knowable now - the session id always, the identity door
    for a named agent, the system-prompt door whenever a `system-prompt:`
    list will run - so the one assembly runs here with stand-in values,
    as a dry run (it writes nothing), and its guard answers. The real
    assembly repeats the same guard; there is no second copy of which doors
    exist. (The old launcher minted first, and a refusal left the minted
    session directory behind.)"""
    start = launch.start
    resuming = launch.resume is not None
    injects = (not resuming and bool(start.context.system_prompt)
               and not switch_enabled(launch.args.idle, start.idle))
    # An unkeyed resume computes no session id: the resumed one is claude's.
    stand_in = (None if resuming and not launch.keyed
                else SessionIdentity("<rehearsal>", "", launch.keyed, {}, minted=False))
    # The settings door's stand-in: computed exactly when the real assembly
    # could compute it, which is never for a launch whose agent file opens
    # its own. A CLI one is folded into the door, so its tokens go with it.
    settings_stand_in = (None if declared_settings(launch) is not None
                         else {"model": "<rehearsal>"})
    rehearsed_cli = vendor_cli_args(launch)
    if settings_stand_in is not None and typed_settings(launch) is not None:
        rehearsed_cli = claude_settings.without_own_settings(rehearsed_cli)
        rehearse_fold(launch)
    try:
        build_annotated_argv(start.vendor, launch.agent_file.vendor_section,
                             agent=launch.agent, identity=stand_in,
                             cli_args=rehearsed_cli,
                             system_prompt_text="<rehearsal>" if injects else "",
                             claude_settings=settings_stand_in,
                             dry_run=True)
    except ValueError as error:
        raise LaunchError(f"{agent_subject(launch.agent)}: {error}",
                          code=EXIT_USAGE) from error


def rehearse_fold(launch: Launch) -> None:
    """Refuse a CLI `--settings` the fold cannot take BEFORE the mint: the
    same check the real assembly makes (`typed_source`), so a missing,
    unreadable or non-object file, or `--settings` given twice, leaves no
    session directory behind. It runs exactly when the real assembly would
    fold: a `claude-code` file exists in a layer known now (template, host,
    agent, seat; a session about to be minted has no session layer yet).
    A layer file the reader refuses is left to the real assembly, which
    refuses it as before."""
    try:
        found = _settings.sources(claude_settings.TOOL, None, launch.agent.name or None,
                                  getattr(launch.args, "seat", None), launch.environ,
                                  launch.agent.file or None)
    except _settings.SettingsError:
        return
    if not found:
        return
    cwd = launch.work_dir if launch.resume is None else resume_cwd(launch)[0]
    typed_source(launch, cwd)


# ---------- stage 2: mint -----------------------------------------------------

def mint(launch: Launch) -> SessionIdentity | PaneMint | None:
    """The launch's identity: minted (live) or projected (--dry-run). A
    keyed launch placed in tmux gets a PaneMint instead - the plan for a
    mint its pane runs at boot, so the key and its reaper live as long as
    the pane, not as long as this launcher. A resume mints only
    when keyed (the fork); otherwise None, after saying so if the session
    it resumes was keyed."""
    if launch.resume is not None:
        return mint_for_resume(launch)
    if launch.keyed and launch.in_tmux:
        return pane_mint_for(launch.agent, environ=launch.environ,
                             key_label=launch.args.key_label, dry_run=launch.dry_run,
                             seat=launch.args.seat)
    return mint_identity(launch.agent, keyed=launch.keyed, environ=launch.environ,
                         dry_run=launch.dry_run, key_label=launch.args.key_label,
                         seat=launch.args.seat)


def mint_for_resume(launch: Launch) -> SessionIdentity | None:
    """A keyed resume forks into a fresh keyed identity and says so on
    stderr. An unkeyed resume of a named session keeps that session's id,
    and warns when the session WAS keyed: its key died with its process
    (a keyed resume degrades honestly)."""
    resume = launch.resume
    if launch.keyed:
        identity = mint_identity(launch.agent, keyed=True, environ=launch.environ,
                                 dry_run=launch.dry_run, key_label=launch.args.key_label,
                                 seat=launch.args.seat)
        print(f"{PROG}: keyed resume = FORK: {resume.session_id} continues as "
              f"{identity.session_id} (the original key died with its process; "
              "the fork carries a fresh one)", file=sys.stderr)
        return identity
    if resume.session_id:
        warning = keyed_resume_warning(os.path.join(
            sessions_root(launch.agent, environ=launch.environ), resume.session_id))
        if warning:
            print(f"{PROG}: {warning}", file=sys.stderr)
    return None


# ---------- stage 3: assemble --------------------------------------------------

def build_annotated_argv(vendor: str, vendor_section: dict, agent: Agent | None = None,
                         identity: SessionIdentity | None = None,
                         cli_args: list | tuple = (), initial_message: str = "",
                         system_prompt_text: str = "", directory: str | None = None,
                         dry_run: bool = False, message_source: str = "initial message",
                         first_message_file_over: int | None = None,
                         pane_minted: bool = False,
                         claude_settings: dict | None = None,
                         claude_settings_source: str = "computed: claude-code settings",
                         ) -> tuple[list, list]:
    """THE ONE ASSEMBLY: ([(token, source), ...], files a live launch writes).

    Order: the command; the computed doors (session id, system prompt, the
    carried definition); the identity door; the `claude:` section, one key
    at a time through the one rendering rule; the CLI passthrough with
    `{shortsid}` filled; then the separator and the first message. The
    settings door is the last computed door, and only when
    `claude_settings` holds data: none, and the argv is as before. A door
    the launcher computed and the agent file or CLI opens again is refused
    (guard_computed_doors) - and since nothing is written here, a refusal
    leaves nothing behind. `pane_minted` (no identity launcher-side): the
    session id's slot is `"$CLAUDE_SESSION_ID"`, expanded in the pane.
    """
    table = VENDORS[vendor]
    computed: list = []
    if table["session_id_flag"] is not None:
        if identity is not None:
            source = "computed: session identity (minted by the launcher)"
            computed += [(table["session_id_flag"], source),
                         (identity.session_id, source)]
        elif pane_minted:
            source = "computed: session identity (minted in the pane at boot)"
            computed += [(table["session_id_flag"], source),
                         (ShellVar("CLAUDE_SESSION_ID"), source)]
    prompt_argv, prompt_file = system_prompt_argv(
        vendor, system_prompt_text, directory=directory, dry_run=dry_run)
    computed += [(token, "computed: injected context (system prompt)")
                 for token in prompt_argv]
    plugin_argv, plugin_dir = agent_plugin_argv(vendor, agent, directory=directory,
                                                dry_run=dry_run)
    carried = ("computed: the agent's own definition, so --agent resolves "
               "from any directory")
    computed += [(token, carried) for token in plugin_argv]
    door_argv, settings_file = settings_argv(vendor, claude_settings, directory=directory,
                                             dry_run=dry_run)
    computed += [(token, claude_settings_source) for token in door_argv]
    agent_ref = agent.name if agent is not None and agent.name else None
    front: list = []
    if agent_ref is not None and table["identity_door"] is not None:
        source = "computed: identity door (the agent named on the command line)"
        front += [(table["identity_door"], source), (agent_ref, source)]
    for key, value in vendor_section.items():
        front += [(token, f"front matter: {vendor}: {key}")
                  for token in render_vendor_args(vendor, {key: value})]
    launcher_args = [token for token, _ in computed]
    if agent_ref is not None and table["identity_door"] is not None:
        launcher_args.append(table["identity_door"])
    guard_computed_doors(vendor, launcher_args,
                         vendor_front_matter_argv(vendor, vendor_section),
                         list(cli_args))
    annotated = [(table["cmd"], f"computed: vendor table ({vendor})")]
    annotated += computed + front
    for token in cli_args:
        filled = expand_shortsid(token, identity, pane_minted)
        source = "CLI passthrough"
        if filled is not token:
            # A keyed tmux launch has no id yet: the pane mints it, and the
            # slot is a ShellExpr that expands there (the old launcher's words).
            source += (" ({shortsid} filled in the pane from the minted id)"
                       if isinstance(filled, ShellExpr)
                       else " ({shortsid} filled from the session identity)")
        annotated.append((filled, source))
    message_file = None
    if initial_message:
        positional, message_file = initial_message_argv(
            initial_message, directory=directory, dry_run=dry_run,
            limit=first_message_file_over)
        if positional != initial_message:
            message_source += (" -> pointer; the text is in a 0600 file "
                               "(over start: first-message-file-over)")
        separator = table["prompt_separator"]
        if separator is not None:
            annotated.append((separator, "computed: prompt separator (vendor table)"))
        annotated.append((positional, f"computed: {message_source}"))
    files = [item for item in (prompt_file, plugin_dir, settings_file, message_file)
             if item is not None]
    return annotated, files


def build_vendor_argv(vendor: str, vendor_section: dict, **kwargs: object) -> list:
    """The plain argv: build_annotated_argv with the annotations dropped."""
    annotated, _files = build_annotated_argv(vendor, vendor_section, **kwargs)
    return [token for token, _source in annotated]


def render_launch_script(argv: list, work_dir: str,
                         identity: SessionIdentity | None = None,
                         committer: CommitterIdentity | None = None,
                         env_prelude: str = "", secret_file: str | None = None,
                         vendor_env: Mapping[str, str] | None = None,
                         shebang: bool = False, pane_mint: PaneMint | None = None,
                         record: LaunchRecord | None = None) -> str:
    """The bash a launch runs - ONE text for both endings: `bash -c` on the
    direct path, and with `shebang` the 0700 trampoline a tmux pane runs.
    In this order - the order is the contract:

        unset the caller's identity        (ids, key handles, signing
                                             block: never hand them down)
        shred trap                          (when a secret file exists)
        pane mint                           (keyed in tmux: mint FIRST, so
                                             every later line sees the id)
        export this session's identity
        committer identity                  (export the human, or unset)
        unset CLAUDE_CODE                   (the child is not nested)
        vendor env defaults                 (before the prelude: an agent's
                                             own declaration wins)
        cd WORKDIR || exit 1                (before the prelude: `./` files
                                             resolve in the session's dir)
        the declared-environment prelude
        the launch record                   (the env the vendor gets,
                                             so after everything that sets it)
        exec argv                           (shlex-quoted; exec, so the
                                             vendor owns this pid - which is
                                             what makes --reap-pid true)

    Under a pane mint the argv is NOT exec'd: the pane-lifetime trap needs a
    shell to fire in, so bash stays the pane's process (its `$$` is what the
    reaper watches) and the vendor runs as its child.
    """
    if identity is not None and pane_mint is not None:
        raise ValueError("an identity minted launcher-side and a pane mint are "
                         "mutually exclusive -- one session, one mint")
    lines = ["#!/usr/bin/env bash"] if shebang else []
    lines.append("# session start: launch script (generated; do not edit)")
    lines.append(render_identity_unset())
    if secret_file:
        lines.append(f"__ss_shred() {{ rm -f {shlex.quote(secret_file)}; }}")
        lines.append("trap __ss_shred EXIT HUP INT TERM")
    if pane_mint is not None:
        lines.append(render_pane_mint(pane_mint))
    if identity is not None and identity.env:
        lines.append("# session identity")
        for name, value in identity.env.items():
            lines.append(f"export {name}={shlex.quote(value)}")
    if committer is not None and committer.mode == "override":
        lines.append("# committer identity resolved to the human")
        lines.append(f"export GIT_COMMITTER_NAME={shlex.quote(committer.name)}")
        lines.append(f"export GIT_COMMITTER_EMAIL={shlex.quote(committer.email)}")
    elif committer is not None and committer.mode == "unset":
        lines.append("# no human identity resolvable; unsetting")
        lines.append("unset GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL")
    lines.append("unset CLAUDE_CODE")
    if vendor_env:
        lines.append("# vendor env defaults (launcher-owned; an "
                     "`environment:` declaration overrides)")
        for name, value in sorted(vendor_env.items()):
            lines.append(f"export {name}={shlex.quote(str(value))}")
    lines.append(f"cd {shlex.quote(work_dir)} || exit 1")
    if env_prelude:
        lines.append(env_prelude.rstrip("\n"))
    rendered = " ".join(render_argv_token(arg) for arg in argv)
    if record is not None:
        lines.append(render_record_call(record, rendered))
    if pane_mint is not None:
        lines.append(rendered)
        lines.append('exit "$?"')
    else:
        lines.append("exec " + rendered)
    return "\n".join(lines) + "\n"


def generator_environ(launch: Launch, identity: SessionIdentity | None) -> dict[str, str]:
    """What a context generator SEES: the user's environment with the
    caller's session identity taken out (a start from inside a live
    seat handed the generators the PARENT's CLAUDE_CODE_SESSION_ID, and
    `memory wake` read the parent's box), the agent named on the command
    line as CLAUDE_CODE_AGENT (never inherited from whoever is typing: a
    dry run of one agent from another agent's shell once rendered the
    caller's mailbox into the new agent's wake), the declared `variables:`, and the identity. Not `files:` (shell,
    sourced in the child) and not secrets (least privilege, a recorded
    choice).

    CLAUDE_CODE_SESSION_ID is never SET here: it is the vendor's name, and
    no vendor is running yet. CLAUDE_SESSION_ID is the new id when the
    launcher minted one, and absent under a pane mint (no id exists yet).
    The scrub comes BEFORE `variables:`, so an agent that declares one of
    these names on purpose still wins."""
    env = without_session_identity(host_environ(launch.environ))
    if launch.agent.name:
        env["CLAUDE_CODE_AGENT"] = launch.agent.name
    else:
        env.pop("CLAUDE_CODE_AGENT", None)
    env.update(launch.agent_file.environment.variables)
    if identity is not None:         # a pane-minted id does not exist yet
        env.update(identity.env)
    return env


#: Vendor flags whose value is a file (or dir) the vendor reads at startup,
#: whoever put them on the line - the launcher's doors or the agent's own.
STARTUP_FILE_FLAGS = {
    "--append-system-prompt-file": "system-prompt",
    "--system-prompt-file": "system-prompt",
    "--plugin-dir": "plugin",
    "--settings": "settings",
}


def startup_files(launch: Launch, work_dir: str, argv: list, files: list,
                  door_dir: str | None, secret_file: str | None) -> tuple:
    """((tag, path), ...) of what this launch hands the vendor at startup:
    the agent file; every file-valued flag on the vendor's line (the
    launcher's doors and the agent's own); the first message's file when
    it went by pointer; the secret file the prelude sources and removes;
    the declared env files that exist; then the vendor's own startup layer
    (`record.vendor_startup_files`). Read off the argv, so a dry run names
    the same files a live launch does, by their representative names."""
    found: list[tuple[str, str]] = []
    if launch.agent.name and launch.agent.file:
        found.append(("agent-file", launch.agent.file))
    for flag, value in itertools.pairwise(argv):
        tag = STARTUP_FILE_FLAGS.get(flag) if isinstance(flag, str) else None
        if tag and isinstance(value, str):
            found.append((tag, value))
    messages = [item.path for item in files if isinstance(item, PlannedFile)
                and os.path.basename(item.path).startswith(MESSAGE_PREFIX)]
    if messages:
        found.append(("first-message", messages[0]))
    elif any(isinstance(token, str) and token.startswith(FIRST_MESSAGE_POINTER_LEAD)
             for token in argv):
        found.append(("first-message", planned_path(
            door_dir, MESSAGE_PREFIX, ".md", DRY_RUN_MESSAGE_NAME, dry_run=True)))
    if secret_file:
        found.append(("secrets", secret_file))
    for path in launch.agent_file.environment.files:
        resolved = (os.path.join(work_dir, path) if path.startswith(RELATIVE_PREFIX)
                    else os.path.expanduser(path))
        if os.path.isfile(resolved):
            found.append(("sourced", resolved))
    found += vendor_startup_files(launch.start.vendor, work_dir, launch.environ.get("HOME"))
    return tuple(found)


def plan_record(launch: Launch, work_dir: str, identity: SessionIdentity | None,
                pane_mint: PaneMint | None, argv: list, files: list,
                door_dir: str | None, secret_file: str | None,
                secrets: list, inherit: tuple = ()) -> LaunchRecord | None:
    """The launch record this launch's script writes: into the
    launcher-minted session dir, or the one the pane mints. Its start
    command is this interpreter (`sys.executable`, the one the record
    call runs, never a macOS framework build's re-exec'd `Python.app`
    binary, which has none of the venv's packages), then the rest of
    `sys.orig_argv`, flags included, so it re-runs as written whether
    `session` or `python -m` started it.
    The declared secrets and `inherit:` names are masked by name: the
    record lists them, never their values."""
    if identity is not None:
        directory = shlex.quote(identity.session_dir)
        shown = identity.session_dir
    elif pane_mint is not None:
        directory = shlex.quote(pane_mint.sessions_root) + '/"$CLAUDE_SESSION_ID"'
        shown = f"{pane_mint.sessions_root}/<id minted in the pane>"
    else:
        return None     # no session directory of its own to write it in
    return LaunchRecord(
        session_dir=directory, shown_dir=shown,
        start_command=shlex.join([sys.executable, *sys.orig_argv[1:]]),
        work_dir=work_dir,
        files=startup_files(launch, work_dir, argv, files, door_dir, secret_file),
        secret_names=(*(secret.name for secret in secrets), *inherit))


def resolve_home(launch: Launch, identity: SessionIdentity | None) -> _settings.Home | None:
    """The session's home from the `session` tool's
    cascade, read launcher-side: template, host, the agent's MD at
    `agent/config/<name>.md`, the seat, and the session layer when the id
    is already minted. A pane-minted launch has no id yet: a per-session
    place comes back as the root, finished in the script with
    `$CLAUDE_SESSION_ID`. A layer file the reader refuses is an authoring
    error: said, and the launch refused."""
    sid = identity.session_id if identity is not None else None
    agent = launch.agent.name or None
    seat = getattr(launch.args, "seat", None)
    try:
        res = _settings.resolve("session", sid, agent, seat, env=launch.environ,
                                agent_file=launch.agent.file or None)
        return _settings.session_home(res, sid, agent, seat, env=launch.environ)
    except _settings.SettingsError as error:
        raise LaunchError(f"{agent_subject(launch.agent)}: settings: {error}",
                          code=EXIT_USAGE) from error


def _opened_settings(launch: Launch, args: list) -> str | None:
    """The value of the `--settings` ARGS open (either spelling; `""` for a
    bare flag), or None; None too for a vendor with no settings door."""
    door = VENDORS[launch.start.vendor].get("settings_door")
    if door is None or not door_is_opened_by(door, args):
        return None
    return claude_settings.own_settings_value(args) or ""


def declared_settings(launch: Launch) -> str | None:
    """The `--settings` the agent file's vendor section opens, or None. That
    launch gets no computed door (claude_settings.skipped): it is not folded."""
    return _opened_settings(launch, vendor_front_matter_argv(
        launch.start.vendor, launch.agent_file.vendor_section))


def typed_settings(launch: Launch) -> str | None:
    """The `--settings` the CLI passthrough opens (a crew's spawn), or None:
    the one a computed door FOLDS in as its top layer (claude_settings.fold)."""
    return _opened_settings(launch, vendor_cli_args(launch))


def folded_cli_args(args: list | tuple, rendered: claude_settings.Rendered | None) -> list:
    """The CLI passthrough the vendor gets beside RENDERED's door: less its
    own `--settings` when that file was folded into the door, so the launch
    carries ONE; as given otherwise."""
    if rendered is None or rendered.folded is None:
        return list(args)
    return claude_settings.without_own_settings(list(args))


def typed_source(launch: Launch, cwd: str) -> _settings.Source:
    """The CLI `--settings` (`typed_settings`) as the layer the fold puts on
    top, read under CWD (claude_settings.passthrough_source). Refused, exit
    2, when the command line opens `--settings` more than once (the fold
    takes one, and dropping the rest would lose a file without a word) or
    when the value cannot be folded. ONE check: the rehearsal before the
    mint (rehearse_fold) and the real assembly both make it here."""
    every = claude_settings.own_settings_values(vendor_cli_args(launch))
    if len(every) > 1:
        raise LaunchError(
            f"{agent_subject(launch.agent)}: settings: the command line opens --settings "
            f"{len(every)} times ({', '.join(v or '(no value)' for v in every)}); the "
            "launcher folds one into its settings door, so pass one",
            code=EXIT_USAGE)
    try:
        return claude_settings.passthrough_source(typed_settings(launch) or "", cwd,
                                                  launch.environ)
    except _settings.SettingsError as error:
        raise LaunchError(f"{agent_subject(launch.agent)}: settings: {error}",
                          code=EXIT_USAGE) from error


def resolve_claude_settings(launch: Launch, identity: SessionIdentity | None,
                            cwd: str | None = None) -> claude_settings.Rendered | None:
    """The `claude-code` tool's model and effortLevel for the settings door,
    read launcher-side as `resolve_home` reads `session`: template,
    host (`~/.sessionkit/tools/settings/`, settings.host_dir), the agent's MD, the
    seat, and the session layer once the id is minted (a pane-minted launch
    has no id yet: its session layer is not read). None, and nothing said,
    when no `claude-code` file exists anywhere or the vendor has no door.

    A CLI passthrough `--settings` (a crew's spawn) is FOLDED in as the top
    layer before render (claude_settings.fold): its keys win, a host
    `_locked` path still holds, and the caller drops its tokens for the one
    computed door (folded_cli_args). A relative path is read under CWD, the
    session's working directory (default: the launch's). With no
    `claude-code` file anywhere it is not read, and the argv is as before.
    A CLI that opens `--settings` more than once is refused (exit 2): the
    fold takes one, and dropping the rest would lose a file without a word.

    Said on stderr, never refused: the reader's notes, each key v1
    does not render, and, for a launch whose AGENT FILE opens its own
    `--settings`, the one line naming both files; that launch gets no door
    and no file. A layer file the reader refuses, or a CLI `--settings` it
    cannot fold, is an authoring error, as for the home."""
    if VENDORS[launch.start.vendor].get("settings_door") is None:
        return None
    sid = identity.session_id if identity is not None else None
    agent = launch.agent.name or None
    seat = getattr(launch.args, "seat", None)
    try:
        res = _settings.resolve(claude_settings.TOOL, sid, agent, seat, env=launch.environ,
                                agent_file=launch.agent.file or None)
    except _settings.SettingsError as error:
        raise LaunchError(f"{agent_subject(launch.agent)}: settings: {error}",
                          code=EXIT_USAGE) from error
    if not res.sources:
        return None
    declared = declared_settings(launch)
    if declared is not None:
        files = claude_settings.render(res).files
        print(f"{PROG}: {claude_settings.skipped(files, declared, launch.environ)}",
              file=sys.stderr)
        return None
    typed = typed_settings(launch)
    if typed is not None:
        res = claude_settings.fold(res, typed_source(
            launch, launch.work_dir if cwd is None else cwd))
    rendered = claude_settings.render(res, typed)
    for note in [*res.notes, *rendered.notes]:
        print(f"{PROG}: {note}", file=sys.stderr)
    return rendered if rendered.data else None


def settings_door_kwargs(rendered: claude_settings.Rendered | None,
                         environ: Mapping[str, str]) -> dict:
    """build_annotated_argv's settings arguments for RENDERED: none at all
    without data, so the call is the one it was before the settings door."""
    if rendered is None:
        return {}
    return {"claude_settings": rendered.data,
            "claude_settings_source": claude_settings.provenance(rendered, environ)}


def render_home_prelude(home: _settings.Home | None) -> str:
    """The bash that hands the launched process its home as SESSION_HOME
    (never HOME) and makes the directory. A
    per-session place is finished here, after the pane mint, with the id
    the pane minted. Empty when there is no home."""
    if home is None:
        return ""
    var = _settings.SESSION_HOME_ENV
    if home.per_session:
        place = shlex.quote(home.place) + '/"$CLAUDE_SESSION_ID"/home'
    else:
        place = shlex.quote(home.place)
    return "\n".join([
        (f"# settings: session.home from {home.origin} ({home.layer} layer) -> "
         f"{var}, never HOME"),
        f"export {var}={place}",
        'mkdir -p "$' + var + '" || printf \'%s\\n\' "session start: could not make '
        + var + ' $' + var + '" >&2',
    ]) + "\n"


def render_schema_install(record: LaunchRecord, python: str) -> str:
    """The script line that runs a tool's schema install before the birth
    line, so the birth finds the schema on a HOME no hook has run under
    yet. Only when the command is on PATH; its output goes to the session's
    `log/settings.jsonl`, never the terminal; stdin is /dev/null; `|| :`.
    `--dir` is the birth line's, character for character (a pane mint's
    `$CLAUDE_SESSION_ID` expands in both)."""
    program = shlex.quote(_settings.SCHEMA_INSTALL[0])
    call = " ".join([shlex.quote(python), "-I", "-c", shlex.quote(_settings.SCHEMA_INSTALL_ENTRY),
                     "--dir", record.session_dir, "--",
                     *(shlex.quote(x) for x in _settings.SCHEMA_INSTALL)])
    return "\n".join([
        ("# schema install: each tool installs its settings schema before the birth "
         "reads them; logged in log/settings.jsonl"),
        f"if command -v {program} >/dev/null 2>&1; then {call} </dev/null || :; fi",
    ]) + "\n"


def render_birth_call(record: LaunchRecord | None, python: str | None = None) -> str:
    """The script line that reads the variables each installed schema names
    into the session's `settings/<tool>.d/50-env.json`, once. It runs after
    the prelude, so it
    reads the environment the vendor will get, and in the session dir the
    record names (a pane mint's `$CLAUDE_SESSION_ID` is set by then). `|| :`:
    settings never stop a launch. The schema install comes first
    (render_schema_install). Empty with no session dir to write in."""
    if record is None:
        return ""
    python = python or sys.executable
    return render_schema_install(record, python) + "\n".join([
        ("# settings at birth: each schema's x-env variables -> "
         "settings/<tool>.d/50-env.json, logged in log/settings.jsonl"),
        " ".join([shlex.quote(python), "-I", "-c", shlex.quote(_settings.BIRTH_ENTRY),
                  "--dir", record.session_dir]) + " || :",
    ]) + "\n"


def inherited_for(launch: Launch) -> tuple[SecretResult, ...]:
    """What the agent's `environment: inherit:` names hold in the LAUNCHER's
    own environment (`launch.environ`, raw: before `host_environ` drops any
    credential), read on a live launch only. A dry run reads no value, so it
    gets nothing here and names the list instead."""
    names = launch.agent_file.environment.inherit
    if launch.dry_run or not names:
        return ()
    return tuple(read_inherited(names, launch.environ))


def assemble(launch: Launch, minted: SessionIdentity | PaneMint | None) -> LaunchPlan:
    """The LaunchPlan for `launch` under what `mint` returned: an identity,
    the PaneMint of a keyed launch placed in tmux, or None for an unkeyed
    resume."""
    if launch.resume is not None:
        return assemble_resume(launch, minted)
    pane_mint = minted if isinstance(minted, PaneMint) else None
    identity = None if pane_mint is not None else minted
    session_dir = identity.session_dir if identity is not None else None
    # The launcher's own files go under the session's `launch/`, beside the
    # record; `run.write_files` makes it.
    door_dir = record_path(session_dir) if session_dir is not None else None
    start = launch.start
    converse = switch_enabled(launch.args.converse, start.converse)
    injected = build_injected_context(
        start, idle=switch_enabled(launch.args.idle, start.idle),
        converse=converse, message=" ".join(launch.message_words),
        # A pane-minted id does not exist yet: launch-info omits the line
        # rather than print an empty one.
        builtins=context_builtins(agent=launch.agent.name or None,
                                  session_id=identity.session_id if identity else None,
                                  converse=converse),
        env=generator_environ(launch, identity), cwd=launch.work_dir)
    declared = launch.agent_file.environment
    secrets: list[DeclaredSecret] = declared_secrets(declared.secrets)
    inherited = inherited_for(launch)
    # A pane-minted launch has no session dir yet: the file goes to temp.
    secret_path = (planned_path(door_dir, SECRET_FILE_PREFIX, "", DRY_RUN_SECRET_NAME,
                                launch.dry_run) if secrets or declared.inherit else None)
    rendered = resolve_claude_settings(launch, identity)
    door_settings = settings_door_kwargs(rendered, launch.environ)
    try:
        annotated, files = build_annotated_argv(
            start.vendor, launch.agent_file.vendor_section, agent=launch.agent,
            identity=identity, cli_args=folded_cli_args(launch.cli_args, rendered),
            initial_message=injected.initial,
            system_prompt_text=injected.system_prompt,
            directory=door_dir, dry_run=launch.dry_run,
            message_source="initial message (words after the agent name + "
                           "injected context)",
            first_message_file_over=launch.first_message_limit,
            pane_minted=pane_mint is not None, **door_settings)
    except ValueError as error:
        # A door opened twice, or context a vendor cannot take: authoring
        # errors, each the launcher's own loud line and a usage exit.
        raise LaunchError(f"{agent_subject(launch.agent)}: {error}",
                          code=EXIT_USAGE)
    argv = [token for token, _source in annotated]
    record = plan_record(launch, launch.work_dir, identity, pane_mint, argv, files,
                         door_dir, secret_path, secrets, declared.inherit)
    home = resolve_home(launch, identity)
    script = render_launch_script(
        argv, launch.work_dir, identity=identity,
        committer=resolve_committer_identity(launch.work_dir, environ=launch.environ),
        env_prelude=render_env_prelude(declared, secret_file=secret_path)
        + render_home_prelude(home) + render_birth_call(record),
        secret_file=secret_path, vendor_env=VENDORS[start.vendor].get("env"),
        shebang=launch.in_tmux, pane_mint=pane_mint, record=record)
    return LaunchPlan(
        vendor=start.vendor, argv=argv, script=script, work_dir=launch.work_dir,
        work_dir_source=launch.work_dir_source, agent=launch.agent,
        identity=identity, injected=injected, provenance=annotated,
        secrets=secrets, secret_file=secret_path, files=tuple(files),
        dry_run=launch.dry_run,
        pane_mint=pane_mint, record=record, home=home,
        inherit=declared.inherit, inherited=inherited)


def resume_cwd(launch: Launch) -> tuple[str, str | None]:
    """The directory a resume runs in, and the line that says why (None when
    there is nothing to say), said by nobody: resume_work_dir says it, the
    rehearsal (rehearse_fold) only reads the directory."""
    recovered = recover_resume_cwd(launch.resume.session_id, home=launch.environ.get("HOME"))
    if recovered and os.path.isdir(recovered):
        return recovered, f"{PROG}: resuming from {recovered} (the session's recorded cwd)"
    if recovered:
        return launch.work_dir, (
            f"{PROG}: the session's recorded cwd {recovered} no longer exists -- "
            "resuming from the current directory (claude may fall to its picker)")
    return launch.work_dir, None


def resume_work_dir(launch: Launch) -> str:
    """Claude scopes a session to the directory it was born in, so
    `--resume <id>` from anywhere else silently falls to the picker. The
    recorded directory wins when it still exists; otherwise the caller's,
    said out loud either way."""
    work_dir, said = resume_cwd(launch)
    if said is not None:
        print(said, file=sys.stderr)
    return work_dir


def assemble_resume(launch: Launch, identity: SessionIdentity | None) -> LaunchPlan:
    """A claude resume's plan. What it deliberately does NOT do: run the
    context generators or open the system-prompt door - the original
    session carries both - so `--idle` is already honoured by the mode.
    What it DOES carry: the declared environment, the identity door, the
    `claude:` section, the committer identity, and the restart message (the date, then the
    voice signal when converse is on, then the user's words). `--converse`
    is honoured through that message: the resumed transcript is a typed
    one, so it is the only channel."""
    start = launch.start
    work_dir = resume_work_dir(launch)
    declared = launch.agent_file.environment
    warn_env_file_traps(declared, work_dir)
    message = resume_message(launch.message_words,
                             converse=switch_enabled(launch.args.converse, start.converse))
    # In temp, as the old launcher put a resume's: even a keyed resume's
    # fresh session dir is not where it looked.
    secrets: list[DeclaredSecret] = declared_secrets(declared.secrets)
    inherited = inherited_for(launch)
    secret_path = (planned_path(None, SECRET_FILE_PREFIX, "", DRY_RUN_SECRET_NAME,
                                launch.dry_run) if secrets or declared.inherit else None)
    # The settings door, an ASSUMPTION: claude reads --settings at
    # process start, so a resume carries the door too; the file goes to
    # temp beside the resume's other files. A live probe checks it.
    rendered = resolve_claude_settings(launch, identity, cwd=work_dir)
    door_settings = settings_door_kwargs(rendered, launch.environ)
    try:
        annotated, files = build_annotated_argv(
            start.vendor, launch.agent_file.vendor_section, agent=launch.agent,
            identity=identity, cli_args=folded_cli_args(vendor_cli_args(launch), rendered),
            initial_message=message,
            dry_run=launch.dry_run, first_message_file_over=launch.first_message_limit,
            message_source="resume restart message (date injection)", **door_settings)
    except ValueError as error:
        raise LaunchError(f"{agent_subject(launch.agent)}: {error}",
                          code=EXIT_USAGE) from error
    argv = [token for token, _source in annotated]
    # No launch record on a resume, by design: an unkeyed
    # resume has no session directory of its own, and a keyed fork's would
    # part the dry run from the old launcher's.
    home = resolve_home(launch, identity)
    script = render_launch_script(
        argv, work_dir, identity=identity,
        committer=resolve_committer_identity(work_dir, environ=launch.environ),
        env_prelude=render_env_prelude(declared, secret_file=secret_path)
        + render_home_prelude(home),
        secret_file=secret_path, vendor_env=VENDORS[start.vendor].get("env"))
    return LaunchPlan(
        vendor=start.vendor, argv=argv, script=script, work_dir=work_dir,
        work_dir_source=launch.work_dir_source, agent=launch.agent, identity=identity,
        injected=InjectedContext(system_prompt="", initial=message, results=[]),
        provenance=annotated, secrets=secrets, secret_file=secret_path, files=tuple(files),
        dry_run=launch.dry_run, resume=True, home=home,
        inherit=declared.inherit, inherited=inherited)


def prepare(args: argparse.Namespace, environ: Mapping[str, str] | None = None,
            work_dir: str | None = None) -> LaunchPlan:
    """resolve, mint, assemble: args in, LaunchPlan out. Under --dry-run it
    acts on nothing (the agent's generators still run)."""
    launch = resolve(args, environ=environ, work_dir=work_dir)
    return assemble(launch, mint(launch))


__all__ = [
    "Launch",
    "LaunchPlan",
    "PlannedFile",
    "PlannedPlugin",
    "assemble",
    "assemble_resume",
    "build_annotated_argv",
    "build_vendor_argv",
    "choose_work_dir",
    "declared_settings",
    "fable_gate",
    "folded_cli_args",
    "generator_environ",
    "inherited_for",
    "mint",
    "mint_for_resume",
    "plan_record",
    "prepare",
    "render_launch_script",
    "resolve",
    "resolve_claude_settings",
    "resume_work_dir",
    "startup_files",
    "typed_settings",
    "vendor_cli_args",
]
