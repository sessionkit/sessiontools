"""Resume: what a claude `--resume`/`--continue` launch needs
from the launcher, ported from the old launcher's resume block.

The spellings are claude's OWN flags (`-r, --resume [value]`, `-c,
--continue`) and reach claude verbatim like any passthrough. The launcher
never claims them; it OBSERVES them, to add what claude cannot do for
itself:

- cwd recovery: claude scopes a session to the directory it was born
  in, so `--resume <id>` from anywhere else silently drops to the picker.
- The date injection: a resumed transcript believes whatever time it ends
  at, so the restart message says what time it is now.
- The honest keyed degrade: the key died with its process, so a resume
  either forks into a fresh keyed identity or says it is resuming UNKEYED.

These are the pure pieces. `detect_resume` is claude-only in v1 (resume for
codex or pi is out of scope): for another vendor the
same spellings are meaningless passthrough, because deciding what another
vendor's flag MEANS is knowledge the launcher refuses to carry.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
from typing import NamedTuple

from sessiontools.start.args import RESUME_TOKENS
from sessiontools.start.context import converse_signal

#: The file a keyed session's directory holds, and an unkeyed one does not.
KEYED_PUB_FILE = "session.pub"

RESUME_STAMP_FORMAT = "%A, %Y-%m-%d %H:%M:%S %Z"


class ResumeRequest(NamedTuple):
    """A claude resume asked for in the passthrough. `session_id` is "" for
    a bare `--continue`/`--resume`: claude's own most-recent/picker rules
    apply, cwd recovery has nothing to recover, and the cwd stays the caller's,
    which keeps the picker scoped to where the caller stands."""

    tokens: tuple
    session_id: str


def detect_resume(vendor: str, cli_args: list | tuple) -> ResumeRequest | None:
    """The resume in a passthrough list, or None. The id is the token after
    `--resume`/`-r` when it is not itself a flag, or the `=` form; the first
    one named wins."""
    if vendor != "claude":
        return None
    tokens, session_id = [], ""
    for index, token in enumerate(cli_args):
        name = token.split("=", 1)[0]
        if name not in RESUME_TOKENS:
            continue
        tokens.append(token)
        if name in ("--resume", "-r") and not session_id:
            if "=" in token:
                session_id = token.split("=", 1)[1]
            else:
                following = cli_args[index + 1] if index + 1 < len(cli_args) else ""
                if following and not following.startswith("-"):
                    session_id = following
    if not tokens:
        return None
    return ResumeRequest(tokens=tuple(tokens), session_id=session_id)


_CWD = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def recover_resume_cwd(session_id: str, home: str | None = None) -> str | None:
    """The directory `session_id` was recorded in, or None (cwd recovery).

    The record is the session's transcript, `<home>/.claude/projects/*/
    <id>.jsonl`. When several projects hold one, the newest file wins (the
    old launcher's `ls -t | head -1`), and the FIRST "cwd" in it is the
    record (its `grep -m1 -o`). One deliberate upgrade, kept from the old
    launcher: the value is JSON-decoded, so an escaped path decodes
    honestly. `home` defaults to the user's; tests always pass their own."""
    if not session_id:
        return None
    pattern = os.path.join(os.path.expanduser(home or "~"), ".claude", "projects", "*",
                           f"{session_id}.jsonl")
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    newest = max(candidates, key=os.path.getmtime)
    with open(newest, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _CWD.search(line)
            if match:
                try:
                    return json.loads(f'"{match.group(1)}"')
                except ValueError:
                    return match.group(1)
    return None


def resume_message(message_words: list | tuple = (), now: datetime.datetime | None = None,
                   converse: bool = False, agent_file: bool = False) -> str:
    """The restart message a resume injects: the date first, the voice
    signal when converse is on, the user's own words LAST.

    The words ride along rather than being dropped (the launcher before the
    old one silently discarded them). Voice is a launch-only fact: the
    resumed transcript is a typed one, so this message is the
    only channel that can tell a voice agent it can speak. `converse` is
    the effective value, CLI flag OR front matter; the caller merges.
    `agent_file` says an agent file is in play, and picks the voice line
    as a fresh launch does."""
    stamp = (now or datetime.datetime.now().astimezone()).strftime(RESUME_STAMP_FORMAT)
    parts = [f"Session restarted. Current date and time: {stamp}"]
    if converse:
        parts.append(converse_signal(agent_file))
    words = " ".join(message_words)
    if words:
        parts.append(words)
    return "\n\n".join(parts)


def keyed_resume_warning(session_dir: str) -> str | None:
    """The warning for an unkeyed resume of a session that WAS keyed, or
    None. Its key died with its process, so the resume proceeds UNKEYED
    and says so: a keyed resume degrades honestly."""
    if os.path.isfile(os.path.join(session_dir, KEYED_PUB_FILE)):
        return ("session was keyed; its key died with its process -- "
                "resuming UNKEYED (new work will not sign as the original "
                "identity; everything it signed remains verifiable)")
    return None
