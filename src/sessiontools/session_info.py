"""The session-info hooks: at SessionStart, a session title that carries the
short session id, and a short block of the session's facts as context.

All run as the plugin's SessionStart hooks (`hooks/session-info`, a POSIX
shim that runs `main([verb])`), one registration per verb:

    title     emits `sessionTitle`: the first 8 characters of `session_id`.
    context   emits `additionalContext`: session id, working directory,
              source and transcript path, one line each.
    identity  emits `additionalContext`: the identity block `session start`
              wrote for this session (`identity.md`), byte for byte, the
              same text `session identity` prints.

Title and context are DEFAULT OFF. Each has its own switch, read from the
environment the hook inherits:

    SESSIONTOOLS_HOOK_TITLE          on / 1 / true / yes: the title verb runs.
    SESSIONTOOLS_HOOK_SESSION_INFO   on / 1 / true / yes: the context verb runs.

Identity is ON: its switch is the file. A session `session start` did not
launch has no block, so the verb is silent there; it costs one python
start and one failed open. SESSIONTOOLS_HOOK_IDENTITY=off (or 0 / false /
no) switches it off. It fires on every source: after `resume`, `clear` or
`compact` the payload's session id names the block, and a session with a
new id (a `clear`) has none, so it says nothing. Every fact in the block
that can change is labelled "at launch".

The shim checks the switch before python starts; `main` checks it again, so
the module is safe to call directly.

The title is never written over a title the session already has: a payload
`session_title` (set by `--name`, `/rename` or another hook) keeps the hook
silent. It is emitted on `startup`, `resume` and `fork`, or when the payload
names no source; Claude Code does not apply `sessionTitle` on `clear` or
`compact`, so the hook says nothing there.

The context block omits a line whose value is absent or empty, and a block
left with no line at all is not emitted: no bare heading. Its Session
ID line is also omitted when the session id is already in context from the
launcher: `session start` exports `SESSIONTOOLS_LAUNCH_INFO=<id>` when it
injects launch-info carrying that id. The values are compared, never the
presence, and only on `startup` (or no source): after `compact` the
launcher's first message may have been summarised away while the id stays
the same, and after `clear` or in a child session the id differs.

A payload with `agent_id` is a subagent's, and both verbs stay silent.

IT NEVER BREAKS A SESSION. `main()` swallows everything and returns 0, and
it never writes to stderr. Bad JSON, a payload that is not an object, or a
missing `session_id` give no output. Stdlib only: nothing here imports a
runtime dependency.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence

TITLE_ENV = "SESSIONTOOLS_HOOK_TITLE"
CONTEXT_ENV = "SESSIONTOOLS_HOOK_SESSION_INFO"
IDENTITY_ENV = "SESSIONTOOLS_HOOK_IDENTITY"
LAUNCH_INFO_ENV = "SESSIONTOOLS_LAUNCH_INFO"

#: Any of these, in any case, switches a verb on. The shim matches the word
#: exactly, so a value with surrounding space stays off through the hook;
#: `switched_on` also strips it, which only a direct caller sees.
ON_WORDS = frozenset({"on", "1", "true", "yes"})

#: Any of these, in any case, switches a DEFAULT-ON verb (identity) off.
OFF_WORDS = frozenset({"off", "0", "false", "no"})

EVENT = "SessionStart"

#: The sources on which Claude Code applies `sessionTitle`.
TITLE_SOURCES = frozenset({"startup", "resume", "fork"})

#: How many characters of the session id the title carries.
SHORT = 8

HEADING = "# Session Info"

#: The block's lines, in order: (label, payload key).
FIELDS = (("Session ID", "session_id"), ("Working directory", "cwd"),
          ("Source", "source"), ("Transcript", "transcript_path"))

SWITCHES = {"title": TITLE_ENV, "context": CONTEXT_ENV}


def switched_on(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in ON_WORDS


def switched_off(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in OFF_WORDS


def enabled(verb: str, env: Mapping[str, str]) -> bool:
    """VERB may run: identity unless switched off, the others only when on."""
    if verb == "identity":
        return not switched_off(env, IDENTITY_ENV)
    return verb in SWITCHES and switched_on(env, SWITCHES[verb])


def _text(payload: Mapping[str, object], key: str) -> str:
    """The payload's string at KEY, or "" when it is absent, empty or not a
    string."""
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _session_id(payload: object) -> str:
    """The payload's session id, or "" when the payload cannot be used: not
    an object, a subagent's, or no non-empty `session_id`."""
    if not isinstance(payload, Mapping) or payload.get("agent_id"):
        return ""
    return _text(payload, "session_id")


def _is_startup(payload: Mapping[str, object]) -> bool:
    """`source` is `startup`, or the payload names no source."""
    return _text(payload, "source") in ("", "startup")


def _output(**fields: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": EVENT, **fields}}


def title_output(payload: object, env: Mapping[str, str]) -> dict | None:
    """The title hook's JSON, or None when it should say nothing."""
    if not switched_on(env, TITLE_ENV):
        return None
    sid = _session_id(payload)
    if not sid or not isinstance(payload, Mapping):
        return None
    source = _text(payload, "source")
    if source and source not in TITLE_SOURCES:
        return None
    if payload.get("session_title") not in (None, ""):
        return None             # a custom title is never overwritten
    return _output(sessionTitle=sid[:SHORT])


def _field_lines(payload: Mapping[str, object], env: Mapping[str, str]) -> list[str]:
    """One line per non-empty field. The Session ID line is left out when
    launch-info already carries this id (the marker equals it) and this is
    the startup start."""
    sid = _text(payload, "session_id")
    deduped = _is_startup(payload) and env.get(LAUNCH_INFO_ENV) == sid
    lines = []
    for label, key in FIELDS:
        value = _text(payload, key)
        if not value or (key == "session_id" and deduped):
            continue
        lines.append(f"- **{label}**: {value}")
    return lines


def context_block(payload: Mapping[str, object], env: Mapping[str, str]) -> str:
    """The session-info block: a heading, then one line per non-empty field
    (see `_field_lines`)."""
    return "\n".join([HEADING, "", *_field_lines(payload, env)])


def context_output(payload: object, env: Mapping[str, str]) -> dict | None:
    """The context hook's JSON, or None when it should say nothing: also when
    the block would be a bare heading, every field deduped or absent."""
    if not switched_on(env, CONTEXT_ENV):
        return None
    if not _session_id(payload) or not isinstance(payload, Mapping):
        return None
    if not _field_lines(payload, env):
        return None
    return _output(additionalContext=context_block(payload, env))


def identity_output(payload: object, env: Mapping[str, str]) -> dict | None:
    """The identity hook's JSON: the session's identity block as context, or
    None when it should say nothing (switched off, a subagent's payload, no
    block, or any fault reading it). The text is the file's bytes, decoded
    as UTF-8 and never re-worded: what `session identity` prints."""
    if switched_off(env, IDENTITY_ENV):
        return None
    sid = _session_id(payload)
    if not sid:
        return None
    from sessiontools import identity_block
    try:
        data = identity_block.read_block(sid, env)
    except identity_block.IdentityError:
        return None
    text = data.decode("utf-8")
    if not text.strip():
        return None
    return _output(additionalContext=text)


VERBS = {"title": title_output, "context": context_output, "identity": identity_output}


def main(argv: Sequence[str] | None = None, stdin=None, stdout=None,
         env: Mapping[str, str] | None = None) -> int:
    """Run one verb on the hook payload; maybe print one line of JSON.
    Always 0."""
    try:
        argv = sys.argv[1:] if argv is None else argv
        env = os.environ if env is None else env
        stdin = sys.stdin if stdin is None else stdin
        stdout = sys.stdout if stdout is None else stdout
        verb = argv[0] if argv else ""
        decide = VERBS.get(verb)
        if decide is None or not enabled(verb, env):
            return 0            # before reading stdin: nothing asked, nothing done
        result = decide(json.loads(stdin.read() or "null"), env)
        if result is not None:
            stdout.write(json.dumps(result) + "\n")
            stdout.flush()
    except BaseException:  # noqa: BLE001, S110 - a hook must never break a session
        pass
    return 0
