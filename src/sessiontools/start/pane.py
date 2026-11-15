"""The pane's line: a tmux placement runs the ENTERED line again.

    session start --in-tmux pip review the auth
        the window runs:   session start --first-message-file F
                               --first-message-file-over 0 pip
        F holds:           review the auth          (0600, in a 0700 dir)

A placed window runs `session start` again, in place, with the placement
flags taken off: no launch script frozen to disk, so a `respawn-pane -k`
starts a fresh seat from the line. Three things live here:

- `pane_argv`: the entered argv less `--in-tmux`, `--tmux-target[=]V`,
  `--switch` and `--no-switch`, which `parse_args` would refuse (or act on)
  in a pane. Only the launcher-flag prefix and the message words after the
  agent are touched; vendor passthrough never is.
- THE FIRST-MESSAGE FILE (ruling 7): `ps` is world-readable and a
  `pane_start_command` lives as long as its pane, so bare first-message
  words never ride the pane's line. The outer launcher writes them to
  `<state root>/launches/<token>/first-message.md` and the line carries the
  hidden `--first-message-file PATH` plus `--first-message-file-over 0`
  (the vendor's half: a pointer, never the text). The pane never deletes
  it, so a respawn reads it again; a missing file refuses loudly.
- THE HAND-OFF: the outer launcher passes the line as typed and the
  directory it was typed in through the new window's environment
  (`ENTERED_ENV`, `ENTERED_DIR_ENV`), so the pane's launch record keeps
  what the human entered. The pane pops both at entry, so its vendor never
  inherits them, and trusts them only when they derive its own argv
  (`take_handoff`). A respawn has none (tmux drops `-e` on `respawn-pane
  -k`) and records its own line.
"""

from __future__ import annotations

import os
import secrets
import shlex
import stat
from collections.abc import MutableMapping

from sessiontools import settings as _settings
from sessiontools.start.errors import EXIT_USAGE, LaunchError
from sessiontools.start.vendor import split_rest

#: The hand-off: the line as entered, and the directory it was entered from.
#: Internal plumbing between one launcher and the pane it places.
ENTERED_ENV = "SESSION_START_ENTERED"
ENTERED_DIR_ENV = "SESSION_START_ENTERED_DIR"

#: Taken off the launcher prefix: a pane is already placed. `--switch` and
#: `--no-switch` go too: without a placement `parse_args` refuses them.
PLACEMENT_FLAGS = frozenset({"--in-tmux", "--switch", "--no-switch"})
PLACEMENT_VALUE_FLAGS = frozenset({"--tmux-target"})

MESSAGE_FILE_FLAG = "--first-message-file"
MESSAGE_LIMIT_FLAG = "--first-message-file-over"
LAUNCHES = "launches"
MESSAGE_FILE = "first-message.md"


def new_token() -> str:
    """One placement's token: its launches/ dir and its temporary window name."""
    return secrets.token_hex(8)


def temp_window_name(token: str) -> str:
    return f"ssp-{token}"


def _value_options() -> frozenset[str]:
    """The launcher's options that take a value, read off the parser."""
    from sessiontools.start.args import build_parser
    return frozenset(option for action in build_parser()._actions
                     if action.option_strings and action.nargs != 0
                     for option in action.option_strings)


def split_prefix(argv: list[str]) -> tuple[list[str], list[str]]:
    """argv -> (the launcher-flag prefix, the agent and everything after it).
    Launcher flags go before the agent name (the position trap), so the
    prefix ends at the first token that is neither an option nor an
    option's value."""
    takes_value = _value_options()
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return list(argv[:index + 1]), list(argv[index + 1:])
        if not token.startswith("-") or token == "-":
            break
        if token in takes_value:
            index += 2
        else:
            index += 1
    return list(argv[:index]), list(argv[index:])


def message_words(argv: list[str]) -> list[str]:
    """The bare first-message words the entered argv carries."""
    _prefix, tail = split_prefix(argv)
    return split_rest(tail[1:])[0] if tail else []


def message_text(words: list[str]) -> str:
    """The message a launch makes of its words (`plan.assemble`'s own join)."""
    return " ".join(words)


def pane_argv(argv: list[str], message_file: str | None) -> list[str]:
    """The argv a placed pane runs: ARGV less the placement flags, and with
    any bare first-message words replaced by `--first-message-file
    MESSAGE_FILE --first-message-file-over 0` in the launcher prefix (a typed
    `--first-message-file-over` is replaced, not doubled). With no words,
    MESSAGE_FILE is not used. Pure."""
    prefix, tail = split_prefix(argv)
    kept: list[str] = []
    words = message_words(argv)
    index = 0
    while index < len(prefix):
        token = prefix[index]
        name = token.split("=", 1)[0]
        if token in PLACEMENT_FLAGS:
            index += 1
            continue
        if name in PLACEMENT_VALUE_FLAGS or (words and name == MESSAGE_LIMIT_FLAG):
            index += 1 if "=" in token else 2
            continue
        kept.append(token)
        index += 1
    if not words:
        return kept + tail
    if message_file is None:
        raise ValueError("bare first-message words need a message file")
    separator = kept[-1:] == ["--"]
    head = kept[:-1] if separator else kept
    moved = [MESSAGE_FILE_FLAG, message_file, MESSAGE_LIMIT_FLAG, "0"]
    return head + moved + (["--"] if separator else []) + tail[:1] + tail[1 + len(words):]


def pane_line(argv: list[str]) -> str:
    """The shell line a pane runs for ARGV (`pane_argv`'s result)."""
    return shlex.join(["session", "start", *argv])


# ---------- the first-message file ----------------------------------------------------------

def launches_root(environ=None) -> str:
    return os.path.join(str(_settings.state_root(environ)), LAUNCHES)


def message_file_path(token: str, environ=None) -> str:
    """Where TOKEN's message file lives; a dry run shows this path."""
    return os.path.join(launches_root(environ), token, MESSAGE_FILE)


def write_message_file(text: str, token: str, environ=None) -> str:
    """TEXT into `<state root>/launches/<token>/first-message.md`: the dir
    0700 and new, the file 0600 and O_EXCL. Returns its path."""
    root = launches_root(environ)
    os.makedirs(os.path.dirname(root), mode=0o700, exist_ok=True)
    _settings.ensure_private_dir(root)
    directory = os.path.join(root, token)
    os.mkdir(directory, 0o700)
    os.chmod(directory, 0o700)
    path = os.path.join(directory, MESSAGE_FILE)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def discard_message_file(path: str | None) -> None:
    """Before the handover only: the file and its token dir go (the pane
    never ran, so nothing will read them)."""
    if not path:
        return
    for remove, target in ((os.unlink, path), (os.rmdir, os.path.dirname(path))):
        try:
            remove(target)
        except OSError:
            pass


def read_message_file(path: str) -> str:
    """The first message, read whole from PATH, or a refusal (exit 2) that
    says why: missing, not a regular file (a symlink included), not owned by
    this user, or open to group or other. Never a fallback."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise LaunchError(f"{MESSAGE_FILE_FLAG} {path}: no such file -- the first message "
                          "is gone, so this launch is refused rather than started "
                          "without it", code=EXIT_USAGE) from None
    except OSError as error:
        raise LaunchError(f"{MESSAGE_FILE_FLAG} {path}: cannot be read "
                          f"({error.strerror or error})", code=EXIT_USAGE) from None
    if not stat.S_ISREG(info.st_mode):
        raise LaunchError(f"{MESSAGE_FILE_FLAG} {path}: not a regular file",
                          code=EXIT_USAGE)
    if info.st_uid != os.getuid():
        raise LaunchError(f"{MESSAGE_FILE_FLAG} {path}: not ours (owned by uid "
                          f"{info.st_uid})", code=EXIT_USAGE)
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise LaunchError(f"{MESSAGE_FILE_FLAG} {path}: readable or writable by group or "
                          f"other (mode {stat.S_IMODE(info.st_mode):04o}); it must be 0600",
                          code=EXIT_USAGE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError) as error:
        raise LaunchError(f"{MESSAGE_FILE_FLAG} {path}: cannot be read ({error})",
                          code=EXIT_USAGE) from None


# ---------- the hand-off --------------------------------------------------------------------

def handoff_env(entered_argv: list[str], entered_dir: str) -> dict[str, str]:
    """The two names the outer launcher sets on the new window."""
    return {ENTERED_ENV: pane_line(entered_argv), ENTERED_DIR_ENV: entered_dir}


def take_handoff(environ: MutableMapping[str, str], own_argv: list[str],
                 message_file: str | None) -> tuple[list[str], str] | None:
    """Pop the hand-off from ENVIRON, ALWAYS (the vendor never inherits it),
    and return (the entered argv, the entered dir) when it is this pane's:
    the entered line derives OWN_ARGV (`pane_argv` with this pane's own
    MESSAGE_FILE), and when words were entered, the file holds exactly
    them. Anything else is None: the launch records its own line."""
    entered = environ.pop(ENTERED_ENV, None)
    entered_dir = environ.pop(ENTERED_DIR_ENV, None)
    if not entered or not entered_dir or not os.path.isabs(entered_dir):
        return None
    try:
        words = shlex.split(entered)
    except ValueError:
        return None
    if words[:2] != ["session", "start"]:
        return None
    entered_argv = words[2:]
    typed = message_words(entered_argv)
    if typed and message_file is None:
        return None
    try:
        if pane_argv(entered_argv, message_file) != list(own_argv):
            return None
    except ValueError:
        return None
    if typed:
        try:
            if read_message_file(message_file) != message_text(typed):
                return None
        except LaunchError:
            return None
    return entered_argv, entered_dir


__all__ = [
    "ENTERED_DIR_ENV",
    "ENTERED_ENV",
    "MESSAGE_FILE",
    "MESSAGE_FILE_FLAG",
    "discard_message_file",
    "handoff_env",
    "launches_root",
    "message_file_path",
    "message_text",
    "message_words",
    "new_token",
    "pane_argv",
    "pane_line",
    "read_message_file",
    "split_prefix",
    "take_handoff",
    "temp_window_name",
    "write_message_file",
]
