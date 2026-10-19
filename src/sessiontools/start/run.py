"""The acts a plan leads to: the dry-run report, or the files and the exec.

`execute(plan)` is the point of no return: it writes the files the plan
names (0600 prompt and first message, the 0700 plugin dir), names the tmux
window it runs in if nobody has, says which context generators failed, and
execs `bash -c <script>` - bash establishes the environment and vanishes,
and the vendor inherits this pid. If anything fails before the exec, what
it wrote goes with it.

Own-window naming (an agent's window should carry the agent's name, not
a number): typed into a tmux window nobody named, the launch names it after the agent. Never a
window someone named. A caller who wants a particular name passes claude's
own `-n NAME` (tm: `-n <seat>+{shortsid}`); `--window-name` is gone.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from typing import NamedTuple

from sessiontools import tmux as _tmux
from sessiontools.start.agents import Agent
from sessiontools.start.context import PlannedFile, discard, materialize
from sessiontools.start.environment import host_environ
from sessiontools.start.errors import PROG, LaunchError
from sessiontools.start.identity import SessionIdentity
from sessiontools.start.plan import LaunchPlan
from sessiontools.start.record import RECORD_DIR, ensure_record_dir, record_path
from sessiontools.start.secrets import (
    is_private,
    report_inherited,
    resolve_or_abort,
    secret_file_text,
)
from sessiontools.start.vendor import render_argv_token


def report_generator_failures(plan: LaunchPlan) -> None:
    """A failed generator's absence from the injected block must be visible
    before the session starts - on a dry run too."""
    for result in plan.injected.results:
        if not result.ok:
            print(f"{PROG}: context generator failed ({result.source}): "
                  f"{result.error}", file=sys.stderr)


def write_files(plan: LaunchPlan) -> list:
    """Resolve the declared secrets and write the files the plan names (the
    0600 secret file first, carrying the secrets and any `inherit:` names, then the 0600 prompt and first message and the
    0700 plugin dir), and return them. Both endings call this - the exec
    and the tmux placement - and both discard what it returns if the launch
    then fails. If anything here fails, what it already wrote goes too, and
    it raises: a secret that will not resolve aborts the launch before any
    file exists."""
    if plan.dry_run:
        raise LaunchError("a dry-run plan names representative paths and is "
                          "never executed")
    written: list = []
    item = None
    try:
        written.extend(make_door_dirs(plan))
        if plan.secret_file:
            results = resolve_or_abort(plan.secrets, resume=plan.resume)
            # `inherit:` names ride the same file, by name in the
            # banner; one that was not set is said and skipped, never fatal.
            report_inherited(list(plan.inherited))
            item = PlannedFile(plan.secret_file,
                               secret_file_text([*results, *plan.inherited]))
            materialize(item)
            written.append(item)
            # The prelude will source this: a file anyone else can read is
            # a launch refused, not a warning (the old launcher's S3).
            if not is_private(item.path):
                raise LaunchError(
                    f"launch aborted: the secret file {item.path} is not 0600 -- refusing "
                    "to hand live credentials to a launch that cannot keep them private")
        for item in plan.files:
            materialize(item)
            written.append(item)
    except OSError as error:
        discard_files(written)
        if item is None:        # before any file: resolving, not writing
            raise LaunchError(f"launch aborted: could not resolve the declared "
                              f"secrets: {error}") from error
        raise LaunchError(f"could not write {item.path}: {error}") from error
    except BaseException:
        discard_files(written)
        raise
    return written


class MadeDir(NamedTuple):
    """A session's `launch/` that `write_files` made for this launch: on a
    failure it goes too, once the files in it have gone."""

    path: str


def make_door_dirs(plan: LaunchPlan) -> list[MadeDir]:
    """The session's `launch/` for every door file
    planned under one: 0700 with its `.gitignore` of `*`, made before the
    first write. A door planned anywhere else (a temp dir: a pane-minted
    or resumed launch) is left to its own directory. Returns the dirs
    THIS call made, so a launch that fails can take them with it."""
    paths = [plan.secret_file, *(item.path for item in plan.files)]
    made = []
    for parent in dict.fromkeys(os.path.dirname(path) for path in paths if path):
        if os.path.basename(parent) == RECORD_DIR and not os.path.isdir(parent):
            ensure_record_dir(os.path.dirname(parent))
            made.append(MadeDir(parent))
    return made


def discard_files(written: list) -> None:
    """Remove what `write_files` wrote, for a launch that will never happen:
    the files first, then any `launch/` this launch made."""
    for item in written:
        if not isinstance(item, MadeDir):
            discard(item)
    for item in written:
        if isinstance(item, MadeDir):
            shutil.rmtree(item.path, ignore_errors=True)


def execute(plan: LaunchPlan) -> None:
    """Write the plan's files, name the window, exec. Does not return on
    success; raises LaunchError (and leaves nothing behind) otherwise.

    ANYTHING that fails between the write and the exec takes the files with
    it, the secret file first among them. Once, only a failed exec
    did: an OSError out of the window naming
    left live credentials on disk. The old launcher had the same gap."""
    written = write_files(plan)
    try:
        name_own_window(plan.agent, plan.vendor)
        report_generator_failures(plan)
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execvpe("bash", ["bash", "-c", plan.script], host_environ())
        except OSError as error:
            raise LaunchError(f"exec failed: {error}") from error
        # A real exec never gets here. A stand-in that returns is a launch
        # that did not happen, and its files go like any other failure's.
        raise LaunchError("exec returned without replacing the process")
    except BaseException:
        discard_files(written)
        raise


# ---------- own-window naming ------------------------------------------------

def default_window_name(agent: Agent, vendor: str) -> str:
    """The agent's name alone (by design: no prefix or suffix), or the
    vendor's for a plain session."""
    return agent.name or vendor


def run_tmux(arguments: list[str], environ: Mapping[str, str] | None = None) -> str:
    """One tmux invocation on the ambient server (`sessiontools.tmux.run`),
    scrubbed env, loud failure. Returns stdout."""
    try:
        result = _tmux.run(arguments, env=host_environ(environ))
    except FileNotFoundError:
        raise LaunchError("tmux is not installed (or not on PATH)")
    if result.returncode != 0:
        detail = result.stderr.strip() or "(no stderr)"
        raise LaunchError(f"tmux {arguments[0]} failed: {detail}")
    return result.stdout


def name_own_window(agent: Agent, vendor: str,
                    environ: Mapping[str, str] | None = None) -> str | None:
    """Inside a tmux window nobody named, name it after the agent. "Nobody
    named it" is the tab bar's own test: window_name == the basename of
    default-shell (automatic-rename is off in the herd, so an unnamed shell
    window stays "bash"). Best-effort always - not in tmux, tmux missing,
    any refusal: None, and the launch proceeds. Returns the name it set."""
    environ = os.environ if environ is None else environ
    pane = environ.get("TMUX_PANE")
    if not environ.get("TMUX") or not pane:
        return None
    try:
        facts = run_tmux(["display-message", "-p", "-t", pane,
                          "#{window_name}\t#{b:default-shell}"], environ)
    except (LaunchError, OSError):
        return None
    parts = facts.rstrip("\n").split("\t")
    current = parts[0] if parts else ""
    shell = parts[1] if len(parts) > 1 else ""
    if not current or current != shell:
        return None
    name = default_window_name(agent, vendor)
    if not name:
        return None
    try:
        run_tmux(["rename-window", "-t", pane, name], environ)
    except (LaunchError, OSError):
        return None
    return name


# ---------- the dry-run report -----------------------------------------------

def render_dry_run_report(plan: LaunchPlan, tmux: tuple[str, str] | None = None,
                          switch: bool = False, holder: str | None = None) -> str:
    """The --dry-run output: who, where, the identity it WOULD mint, every
    token of the command with the source that put it there (straight off
    plan.provenance, which IS the argv), the secrets by name, and the launch
    script in full - for a tmux launch that script IS the trampoline a pane
    would run. `tmux` is (session, window) for a placement ("" = the
    caller's current session). `holder` is the front door's line
    (`holders.Door.report_line`), after the mode: a dry run answers what a
    live launch would do about a live holder, and still exits 0. Without
    one, byte-for-byte the old launcher's report, for both the direct and
    the tmux path."""
    lines = [f"{PROG}: DRY RUN -- nothing is launched, no secret is resolved"]
    if plan.agent.name:
        lines.append(f"agent:    {plan.agent.name}  ({plan.agent.file})")
    else:
        lines.append(f"agent:    (none -- a plain {plan.vendor} session: no agent file, "
                     "no key, no context)")
    lines.append(f"vendor:   {plan.vendor}")
    if plan.resume and plan.identity is not None:
        lines.append("mode:     resume (direct; FORKED into a fresh keyed identity -- the "
                     "original key died with its process, the fork carries a new one)")
    elif plan.resume:
        lines.append("mode:     resume (direct; the session keeps its own identity)")
    elif tmux is not None:
        session, window = tmux
        where = f"session {session!r}" if session else "the current session"
        then = ", then switch-client" if switch else ""
        lines.append(f"mode:     tmux window {window!r} in {where}{then}")
    else:
        lines.append("mode:     direct (exec in place)")
    if holder:
        lines.append(holder)
    identity = plan.identity
    if identity is None:
        if plan.pane_mint is not None:
            lines.append("session:  (keyed: minted inside the pane at boot)")
            lines.append(f"  dir:    {plan.pane_mint.sessions_root}/<id minted in the pane>")
    else:
        _identity_lines(lines, identity)
    lines.append(f"workdir:  {plan.work_dir}  ({plan.work_dir_source})")
    if plan.home is not None:
        place = (f"{plan.home.place}/<id minted in the pane>/home" if plan.home.per_session
                 else plan.home.place)
        lines.append(f"home:     {place}  (SESSION_HOME; session.home set by "
                     f"{plan.home.origin}, {plan.home.layer} layer)")
    if plan.record is not None:
        lines.append(f"record:   {record_path(plan.record.shown_dir)}/  (env with secrets "
                     "masked, command, startup-files: written once by the script, "
                     "before the vendor starts)")
    lines.append("command:")
    width = min(max(len(render_argv_token(token)) for token, _ in plan.provenance), 44)
    for token, source in plan.provenance:
        lines.append(f"  {render_argv_token(token):<{width}}  <- {source}")
    if plan.secrets:
        lines.append("secrets:  (dry run: backends not called, no file written)")
        for secret in plan.secrets:
            lines.append(f"  {secret.name}  <- {secret.backend}:{secret.ref}")
    if plan.inherit:
        lines.append("inherit:  (dry run: no value read, no file written; a live launch "
                     "carries each one set in the launcher's environment by the 0600 "
                     "secret file)")
        for name in plan.inherit:
            lines.append(f"  {name}  <- the launcher's environment")
    lines.append("launch script:")
    for line in plan.script.rstrip("\n").split("\n"):
        lines.append("  " + line)
    return "\n".join(lines) + "\n"


def _identity_lines(lines: list, identity: SessionIdentity) -> None:
    """The identity block: what a dry run WOULD mint, said out loud."""
    lines.append(f"session:  {identity.session_id}")
    unminted = "" if identity.minted else "   (not created: a dry run mints nothing)"
    lines.append(f"  dir:    {identity.session_dir}{unminted}")
    if identity.mint_command:
        mint = " ".join(shlex.quote(part) for part in identity.mint_command)
        lines.append(f"  mint:   {mint}")
        lines.append("          (NOT RUN: a live keyed launch runs this, "
                     "which spawns an ssh-agent and mints a key)")
        lines.append("  note:   the rest of the keyed env block "
                     "(CLAUDE_SESSION_AGENT_SOCK, CLAUDE_SESSION_PUB)")
        lines.append("          is born WITH the key, so a run that "
                     "mints nothing cannot show it")
