"""What a launch injects into a session's head, and how it gets there.

THE GENERATORS. An agent file names its own context under `start:
context:` - two ordered lists of shell command lines. `system-prompt:`
output rides the vendor's system-prompt door (standing configuration);
`initial:` output leads the first user message (a turn-one instruction).
The launcher only runs a list and labels what came back: every block says
`<!-- source: CMD -->`, and a failed generator leaves a LOUD note (a
silently missing briefing reads exactly like an empty one), while the
launch proceeds. One built-in, `launch-info`, is produced in-process: the
facts only the launcher knows. Idle is a short circuit, not a filter: no
generator runs at all.

Each shelled-out generator is announced on stderr before it runs - one
agent's wake takes ~55 s, and a dry run that prints nothing for a minute looks
hung. The 60 s per-generator cap is the old contract, kept.

THE DOORS, planned here and written by `run.execute`:

    system prompt   ALWAYS a 0600 file (--append-system-prompt-file): it
                    runs to tens of kilobytes, and argv is world-readable
    first message   inline up to `first-message-file-over` bytes, else a
                    0600 file and a one-line pointer in argv
    agent plugin    a 0700 one-agent plugin dir holding a COPY of the agent
                    file, so `--agent NAME` resolves from any directory
                    (a symlink is silently not followed - measured)
    settings        a 0600 JSON file (--settings): the `claude-code` tool's
                    model and effortLevel from the settings cascade

A dry run names a representative path for each and writes nothing. A live
plan names the real path - `session-start-<kind>-XXXXXXXX` in the session
directory, the shape the old mkstemp names had (a `startup-report`
script globs them) - and `execute` creates it exclusively.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from typing import NamedTuple

from sessiontools.start.errors import PROG, AuthoringError
from sessiontools.start.vendor import VENDORS

BUILTIN_LAUNCH_INFO = "launch-info"

# A bound on any one generator. The old launcher's own predecessor had
# none, and a hung `git` in a context script hung the launch silently.
CONTEXT_TIMEOUT_S = 60

CONTEXT_MARKER = "<!-- source: %s -->"

# Idle's boot line, injected only when an agent file is in play. Wording
# kept from the old `--slim`: existing agent manifests are written against
# it. A bare launch has no manifest to boot from, so it gets no boot line.
IDLE_BOOT_TRIGGER = (
    "Boot from your agent MD now: run your startup manifest (self-gather "
    "role, task context, date, machine -- nothing is injected)."
)

# A launch-only fact: nothing in the agent's own file can tell it whether
# this session has a voice, so it is injected - and survives idle. It
# states the capability, never an order: whether to speak first is the
# seat's letter's call. A seat with ears speaks through the mouth.
CONVERSE_SIGNAL = (
    "Voice: AVAILABLE -- this session can speak. Whether to speak first is "
    "your letter's call (its VOICE line); when you do speak, a seat with "
    "ears (a heard.state in its home) speaks through the mouth (`mouth "
    "say`), so Talk and the other seats hear it; any other seat uses the "
    "VoiceMode converse tool."
)

# The same launch-only fact for a launch with no agent file. CONVERSE_SIGNAL
# is written for agent files (it points at their wording); a bare launch has
# none, so it is told the capability and the tool, nothing more.
BARE_CONVERSE_SIGNAL = (
    "Voice: AVAILABLE -- this session can speak, through the VoiceMode "
    "converse tool."
)


class ContextEntry(NamedTuple):
    """A `start: context:` entry the agent file wrote as a MAPPING: the
    command, and the exit codes this agent file declares informational for
    it. The launcher never guesses which codes are benign; it honours only
    the codes written here. A plain string entry is the same
    command with no declared codes."""

    run: str
    informational_exit: frozenset = frozenset()


# The informational header's fixed tail. True whether or not stdout follows:
# an informational exit with nothing on stdout renders this header alone.
INFORMATIONAL_EXIT_NOTE = ("This agent file declares exit %d informational "
                           "for this generator, so this is its answer, not a "
                           "failure.")


class ContextResult(NamedTuple):
    """One generator's outcome. `source` is the entry exactly as declared;
    `output` is kept even on failure (half a briefing, said to be half);
    `error` is None on success, else a short reason, never the output.
    `note` is set only for a non-zero exit the agent file declared
    informational: it did not fail (`ok`), but the exit is still shown."""

    source: str
    output: str
    error: str | None
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class InjectedContext(NamedTuple):
    """system_prompt ("" = the door stays shut), initial ("" = no first
    message), results (system-prompt list then initial list, in run order)."""

    system_prompt: str
    initial: str
    results: list


def generate_launch_info(agent: str | None = None, session_id: str | None = None,
                         command: object = None, converse: bool = False,
                         now: object = None, host: str | None = None) -> str:
    """The launch-info block: date, machine, agent, session id, voice, and
    the command when given. A field not supplied is OMITTED - "Session ID:
    None" would be a fabricated fact. `now` and `host` are injectable."""
    # Naive local time, as the old launcher printed it (its %Z is empty).
    stamp = now if now is not None else datetime.datetime.now()  # noqa: DTZ005
    if not isinstance(stamp, str):
        stamp = stamp.strftime("%A, %Y-%m-%d %H:%M:%S %Z").strip()
    lines = ["# Launch Info", ""]
    lines.append(f"- **Date/time**: {stamp}")
    lines.append("- **Machine**: %s" % (host if host is not None
                                        else socket.gethostname()))
    if agent is not None:
        lines.append(f"- **Agent**: {agent}")
    if session_id is not None:
        lines.append(f"- **Session ID**: {session_id}")
    if converse:
        lines.append(f"- **Voice**: ACTIVE -- {CONVERSE_SIGNAL}")
    else:
        lines.append("- **Voice**: inactive (type mode)")
    if command:
        rendered = command if isinstance(command, str) else shlex.join(command)
        lines += ["", "Launched as:", "```", rendered, "```"]
    return "\n".join(lines) + "\n"


def context_builtins(**launch_info: object) -> dict[str, Callable[[], str]]:
    """name -> zero-argument generator; one entry today, a table so a second
    built-in is an entry, not a special case in the runner."""
    return {BUILTIN_LAUNCH_INFO: lambda: generate_launch_info(**launch_info)}


def announce_to_stderr(entry: str) -> None:
    print(f"{PROG}: context: running `{entry}`", file=sys.stderr, flush=True)


def run_context_entry(entry: str | ContextEntry, env: dict | None = None,
                      cwd: str | None = None,
                      builtins: dict | None = None, timeout: int = CONTEXT_TIMEOUT_S,
                      announce: Callable[[str], None] | None = announce_to_stderr,
                      ) -> ContextResult:
    """Run one generator; never raises. An entry is a COMMAND LINE, run
    through the shell on purpose (`taskmaster context --role $ROLE` expands
    a declared variable; pipes make one-line generators possible). The
    agent file dictates the whole launch, so naming a command is no new
    privilege. A `ContextEntry` is reduced to its command string HERE, so the
    built-in lookup, the announcement and the `source` label all see the
    command, never the tuple."""
    if isinstance(entry, ContextEntry):
        entry, informational = entry.run, entry.informational_exit
    else:
        informational = frozenset()
    table = {} if builtins is None else builtins
    name = entry.strip()
    if name in table:
        try:
            return ContextResult(entry, table[name]() or "", None)
        except Exception as exc:  # noqa: BLE001 - a built-in never takes a launch down
            return ContextResult(entry, "", f"{type(exc).__name__}: {exc}")
    if announce is not None:
        announce(entry)
    try:
        result = subprocess.run(entry, shell=True, capture_output=True, text=True,
                                timeout=timeout, cwd=cwd, env=env, check=False)
    except subprocess.TimeoutExpired:
        return ContextResult(entry, "", f"timed out after {timeout}s")
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        reason = f"exited {result.returncode}"
        if detail:
            reason += f" -- {detail[-1]}"
        if result.returncode in informational:
            note = f"{reason}. {INFORMATIONAL_EXIT_NOTE % result.returncode}"
            return ContextResult(entry, result.stdout, None, note)
        return ContextResult(entry, result.stdout, reason)
    return ContextResult(entry, result.stdout, None)


def run_context_entries(entries: list, **kwargs: object) -> list[ContextResult]:
    """IN ORDER, one after another: a later generator may read a file an
    earlier one wrote."""
    return [run_context_entry(entry, **kwargs) for entry in entries]


def render_context(results: list[ContextResult]) -> str:
    """One labelled block per entry - success, failure, a declared
    informational exit, or silence."""
    blocks = []
    for result in results:
        block = [CONTEXT_MARKER % result.source]
        if not result.ok:
            block.append(
                f"CONTEXT GENERATOR FAILED: `{result.source}` {result.error}. This "
                "section is missing or incomplete -- do not read its absence as "
                "an empty result.")
        elif result.note is not None:
            block.append(f"CONTEXT GENERATOR NOTE: `{result.source}` {result.note}")
        text = (result.output or "").strip("\n")
        if text:
            block.append(text)
        elif result.ok and result.note is None:
            block.append("(this generator produced no output)")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def converse_signal(agent_file: bool) -> str:
    """The voice line a converse launch injects: CONVERSE_SIGNAL when an
    agent file is in play, BARE_CONVERSE_SIGNAL when none is."""
    return CONVERSE_SIGNAL if agent_file else BARE_CONVERSE_SIGNAL


def build_injected_context(start: object, idle: bool = False, converse: bool = False,
                           message: str = "", builtins: dict | None = None,
                           env: dict | None = None, cwd: str | None = None,
                           timeout: int = CONTEXT_TIMEOUT_S,
                           announce: Callable[[str], None] | None = announce_to_stderr,
                           agent_file: bool = False,
                           ) -> InjectedContext:
    """A launch's injected context from its parsed `start:` block. `idle`
    and `converse` are the EFFECTIVE values (CLI or front matter). The first
    message is the briefing, then the converse signal, then the user's own
    words - last, because they are the request the session serves.

    `agent_file` says an agent file is in play. Only then does an idle
    launch open with IDLE_BOOT_TRIGGER; a bare idle launch carries just the
    converse signal and the user's words, or no first message at all. It
    also picks the converse signal, idle or not: see converse_signal."""
    if idle:
        parts = [IDLE_BOOT_TRIGGER] if agent_file else []
        if converse:
            parts.append(converse_signal(agent_file))
        if message:
            parts.append(message)
        return InjectedContext("", ("\n\n".join(parts) + "\n") if parts else "", [])
    runner = {"builtins": builtins, "env": env, "cwd": cwd, "timeout": timeout,
              "announce": announce}
    system_results = run_context_entries(start.context.system_prompt, **runner)
    initial_results = run_context_entries(start.context.initial, **runner)
    parts = []
    briefing = render_context(initial_results).strip("\n")
    if briefing:
        parts.append(briefing)
    if converse:
        parts.append(converse_signal(agent_file))
    if message:
        parts.append(message)
    return InjectedContext(
        system_prompt=render_context(system_results),
        initial=("\n\n".join(parts) + "\n") if parts else "",
        results=system_results + initial_results,
    )


# ---------- the doors: planned here, written by run.execute ----------------

# Above this many bytes the first user message goes to a 0600 file and argv
# carries a pointer (`start: first-message-file-over:` overrides, and so
# does the CLI flag). By design, argv is the harness's NORMAL door, so the
# default is very large - Linux's per-argument cap, 128 KiB - and tm lowers
# it to 0 deliberately.
DEFAULT_FIRST_MESSAGE_FILE_OVER = 131072

DRY_RUN_PROMPT_NAME = "system-prompt.md"
DRY_RUN_MESSAGE_NAME = "first-message.md"
DRY_RUN_PLUGIN_NAME = "agent-plugin"
DRY_RUN_SETTINGS_NAME = "settings.json"

PROMPT_PREFIX = "session-start-system-prompt-"
MESSAGE_PREFIX = "session-start-first-message-"
PLUGIN_PREFIX = "session-start-agent-plugin-"
SETTINGS_PREFIX = "session-start-settings-"


class PlannedFile(NamedTuple):
    """A 0600 file a live launch writes before its exec. Its repr names the
    path and the size, never the text: the secret file's text is live
    credentials, and a prompt file's is the agent's private wake."""

    path: str
    text: str

    def __repr__(self) -> str:
        return f"PlannedFile(path={self.path!r}, text=<{len(self.text)} chars>)"


class PlannedPlugin(NamedTuple):
    """The 0700 one-agent plugin dir a live launch writes before its exec."""

    path: str
    agent_name: str
    agent_file: str


def planned_path(directory: str | None, prefix: str, suffix: str,
                 dry_run_name: str, dry_run: bool) -> str:
    """Where a door's file goes: the representative name under a dry run,
    else a fresh `<prefix>XXXXXXXX<suffix>` (the mkstemp shape; `execute`
    creates it with O_EXCL, so a name that exists is a failure, never an
    overwrite)."""
    base = directory or tempfile.gettempdir()
    if dry_run:
        return os.path.join(base, dry_run_name)
    return os.path.join(base, f"{prefix}{secrets.token_hex(4)}{suffix}")


def system_prompt_argv(vendor: str, text: str, directory: str | None = None,
                       dry_run: bool = False) -> tuple[list[str], PlannedFile | None]:
    """(argv, file to write) handing `text` to the vendor. The FILE door wins
    where a vendor has one; a vendor with neither door and a non-empty block
    is an ERROR, never a quiet drop. A live plan names the file; nothing is
    written here."""
    if not text:
        return [], None
    table = VENDORS[vendor]
    file_door = table["system_prompt_file_door"]
    if file_door is not None:
        path = planned_path(directory, PROMPT_PREFIX, ".md",
                            DRY_RUN_PROMPT_NAME, dry_run)
        return [file_door, path], None if dry_run else PlannedFile(path, text)
    inline_door = table["system_prompt_door"]
    if inline_door is not None:
        return [inline_door, text], None
    raise AuthoringError(
        f"vendor {vendor!r} has no system-prompt door, so declared `context: "
        "system-prompt:` output cannot be delivered -- move it to `context: "
        "initial:`")


def settings_argv(vendor: str, data: dict | None, directory: str | None = None,
                  dry_run: bool = False) -> tuple[list[str], PlannedFile | None]:
    """(argv, file to write) handing DATA to the vendor's settings door as
    a 0600 JSON file; ([], None) with no data or no door. A dry
    run names the representative path and plans no file; nothing is
    written here."""
    door = VENDORS[vendor].get("settings_door")
    if not data or door is None:
        return [], None
    path = planned_path(directory, SETTINGS_PREFIX, ".json", DRY_RUN_SETTINGS_NAME, dry_run)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    return [door, path], None if dry_run else PlannedFile(path, text)


#: How the pointer that replaces a long first message begins: the launch
#: record knows a message went by file from it.
FIRST_MESSAGE_POINTER_LEAD = "Your first message is materialised at "


def first_message_pointer(path: str, size: int) -> str:
    """The positional that replaces a long first message."""
    return (f"{FIRST_MESSAGE_POINTER_LEAD}{path} ({size} bytes): "
            "`cat` it now and read it as this message. It is the launcher's "
            "injected context, kept out of argv because `ps` is world-readable.")


def initial_message_argv(text: str, directory: str | None = None,
                         dry_run: bool = False, limit: int | None = None,
                         ) -> tuple[str, PlannedFile | None]:
    """(positional, file to write): the message itself when it fits `limit`
    bytes, else a pointer to a 0600 file (0 = always a file)."""
    if not text:
        return "", None
    if limit is None:
        limit = DEFAULT_FIRST_MESSAGE_FILE_OVER
    size = len(text.encode("utf-8"))
    if size <= limit:
        return text, None
    path = planned_path(directory, MESSAGE_PREFIX, ".md", DRY_RUN_MESSAGE_NAME,
                        dry_run)
    return first_message_pointer(path, size), None if dry_run else PlannedFile(path, text)


def agent_plugin_argv(vendor: str, agent: object, directory: str | None = None,
                      dry_run: bool = False) -> tuple[list[str], PlannedPlugin | None]:
    """([flag, dir], dir to write) carrying the agent's definition, or
    ([], None) when the vendor has no such mechanism or there is no agent.

    `--agent NAME` is a NAME LOOKUP the vendor performs across its settings
    sources, so a launch from a neutral cwd died with "--agent 'alice' not
    found". Not `--setting-sources user`
    (ruled out: it loads a 49-agent registry into every wake) and not
    `--agents '<json>'` (argv is world-readable): a plugin dir, path-based."""
    flag = VENDORS[vendor].get("agent_plugin_dir_arg")
    if flag is None or agent is None or not getattr(agent, "file", None):
        return [], None
    path = planned_path(directory, PLUGIN_PREFIX, "", DRY_RUN_PLUGIN_NAME, dry_run)
    return [flag, path], None if dry_run else PlannedPlugin(path, agent.name, agent.file)


# ---------- writing (run.execute's half) -------------------------------------

def write_private_file(path: str, text: str) -> str:
    """Create `path` 0600, exclusively, and write `text`."""
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
    except BaseException:
        os.unlink(path)
        raise
    return path


def write_agent_plugin_dir(path: str, agent_name: str, agent_file: str) -> str:
    """A one-agent Claude plugin at `path` (0700) with a COPY of the agent
    file. Measured from /private/tmp with
    `--setting-sources project,local`: a real file in agents/ resolves; the
    same file as a SYMLINK does not; no plugin dir does not."""
    os.mkdir(path, 0o700)
    try:
        os.chmod(path, 0o700)
        os.makedirs(os.path.join(path, ".claude-plugin"))
        os.makedirs(os.path.join(path, "agents"))
        with open(os.path.join(path, ".claude-plugin", "plugin.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"name": f"session-agent-{agent_name}",
                       "version": "0.0.1",
                       "description": f"{agent_name}, carried by session start so --agent "
                                      "resolves from any directory"},
                      handle)
            handle.write("\n")
        shutil.copyfile(agent_file, os.path.join(path, "agents", f"{agent_name}.md"))
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def materialize(item: PlannedFile | PlannedPlugin) -> str:
    if isinstance(item, PlannedPlugin):
        return write_agent_plugin_dir(item.path, item.agent_name, item.agent_file)
    return write_private_file(item.path, item.text)


def discard(item: PlannedFile | PlannedPlugin) -> None:
    """Best-effort removal of something written for a launch that will not
    happen: this runs on failure paths, where raising again would replace
    the real error with a worse one."""
    if isinstance(item, PlannedPlugin):
        shutil.rmtree(item.path, ignore_errors=True)
        return
    try:
        os.unlink(item.path)
    except OSError:
        pass
