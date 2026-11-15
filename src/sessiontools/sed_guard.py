"""sed-guard: refuse a Bash call that runs `sed -i ''` against GNU sed.

`sed -i '' 's/a/b/' f` is BSD sed's in-place form. GNU sed's `-i` takes no
separate argument, so GNU reads `''` as the SCRIPT (or, after `-e`/`-f`, as
a file named ''), `s/a/b/` as a file, edits nothing it was meant to, and
exits 2. Where GNU sed is first on PATH, an agent writing the BSD form fails
every time.

It runs as the plugin's PreToolUse hook (`hooks/sed-guard`, matcher
`Bash`, a POSIX shim that runs `main()`). One variable, read from the
environment the hook inherits:

    SESSIONTOOLS_HOOK_SED   off / 0 / false / no (any case): never refuse.
                            Anything else, unset included, is ON, and the
                            shim exits before python starts when off.

IT PARSES, IT NEVER GREPS. Heredoc bodies are cut first, then the command
is tokenised with `shlex` (posix, newline a separator) and split into
simple commands. A quoted string that merely holds the text
(`echo "sed -i ''"`), a heredoc body or a comment never refuses. In each
simple command, a word whose basename is `sed` or `gsed` (anywhere in it,
so `xargs sed` and `find ... -exec sed` count) followed later by `-i`, a
short-flag cluster ending in `i` (`-Ei`) or `--in-place`, IMMEDIATELY
followed by an EMPTY word, is the BSD form.

ONLY AGAINST GNU. The sed the call names is asked `--version` (cap 1 s):
a word with a `/` at that path, a bare `sed` as the first `sed` on the
hook's PATH; `gsed` is GNU without asking. Only `GNU sed` in the answer
refuses, so a BSD-first host, or `/usr/bin/sed -i ''` on a GNU-first one,
is never refused.

THE GNU FORM IS CUT FROM THE ORIGINAL TEXT: the blanks and the empty
argument are deleted in place, every other byte kept (never rebuilt from
tokens: `shlex.join` would re-quote globs, `"$f"`, `~` and `$(...)`).

THE REFUSAL is Claude Code's PreToolUse JSON on stdout, exit 0:
`hookSpecificOutput.permissionDecision: "deny"` with the reason in
`permissionDecisionReason`. Measured on Claude Code 2.1.325 under
`bypassPermissions` (docs/sed-guard-hook.md).

IT NEVER BREAKS A TURN. Anything unexpected (bad JSON, an unbalanced
quote, a probe that fails or times out, a crash) allows, silently:
`main()` swallows everything, returns 0 and never writes to stderr.

Known misses, each failing open: `bash -c '...'` strings are not looked
into; shlex starts a comment at a mid-word `#` (`$#`, `${#a[@]}`) and drops
what follows; backticks and `$'...'` mean nothing to it.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping

SWITCH_ENV = "SESSIONTOOLS_HOOK_SED"

#: Exactly these (any case) switch it off. No whitespace is trimmed: the
#: shim's `case` decides first and trims nothing, so the two agree.
OFF_WORDS = frozenset({"off", "0", "false", "no"})

#: The words that name a sed, by basename. gsed is GNU sed by name.
SED_NAMES = frozenset({"sed", "gsed"})

#: How long `<sed> --version` may take before the call is allowed.
PROBE_TIMEOUT = 1.0

#: A token that splits simple commands: made only of these characters.
_SPLIT = frozenset(";&|()\n")

#: `-i`, a short-flag cluster ending in i (`-Ei`, `-ni`), or `--in-place`.
_IN_PLACE = re.compile(r"-[A-Za-z]*i|--in-place")

#: A heredoc operator and its delimiter word: `<<` or `<<-` (never `<<<`),
#: blanks, then a word in any quoting (`EOF`, `'EOF'`, `"EOF"`, `\EOF`).
_HEREDOC = re.compile(
    r"(?<!<)<<(?!<)(-?)[ \t]*((?:\\.|'[^'\n]*'|\"[^\"\n]*\"|[^\s;&|<>()'\"\\])+)")

#: Where the GNU form may cut: an in-place flag, blanks, an empty quote pair
#: ending the word. Between the flag and the pair may sit backslash-newlines
#: (sh joins those lines); they are kept, and only the blanks and the pair on
#: the pair's own line are cut. Each candidate is checked against the parser
#: before it is cut (`gnu_form`).
_CUT = re.compile(r"(?:-[A-Za-z]*i|--in-place)(?:[ \t]*\\\n)*"
                  r"((?:[ \t]+|(?<=\n)[ \t]*)(?:''|\"\"))(?=[\s;&|)]|$)")

#: A newline that ends a logical line: one not joined by a backslash.
_LINE_END = re.compile(r"(?<!\\)\n")

#: What the probe is told about the sed it found: GNU or not.
IsGnu = Callable[[str], bool]


def switched_off(env: Mapping[str, str]) -> bool:
    return env.get(SWITCH_ENV, "").lower() in OFF_WORDS


# --- parsing --------------------------------------------------------------------

def _unquote(word: str) -> str:
    """A heredoc delimiter word with its quotes and backslashes removed."""
    return re.sub(r"\\(.)|'([^']*)'|\"([^\"]*)\"",
                  lambda m: next(g for g in m.groups() if g is not None), word)


def strip_heredocs(command: str) -> str:
    """COMMAND with every heredoc BODY removed; the operator lines stay.

    A body is every line after its operator's line up to the first line
    equal to the delimiter (for `<<-`, after leading tabs are stripped).
    Several heredocs on one line are read in order. A body with no closing
    line runs to the end. A `<<` inside quotes is taken for an operator
    too: that strips too much, and only ever misses a refusal."""
    out: list[str] = []
    pending: list[tuple[bool, str]] = []
    for line in command.split("\n"):
        if pending:
            tabs, delim = pending[0]
            if (line.lstrip("\t") if tabs else line) == delim:
                pending.pop(0)
            continue
        out.append(line)
        pending = [(m.group(1) == "-", _unquote(m.group(2))) for m in _HEREDOC.finditer(line)]
    return "\n".join(out)


def simple_commands(command: str) -> list[list[str]]:
    """COMMAND as simple commands, each a list of words; ValueError on an
    unbalanced quote. Heredoc bodies are cut first, and backslash-newline
    joins lines, as sh does."""
    text = strip_heredocs(command).replace("\\\n", "")
    lexer = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"          # newline is a separator, not a blank
    tokens = list(lexer)                # the whole command, before deciding
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= _SPLIT:
            commands.append([])
        else:
            commands[-1].append(token)
    return [c for c in commands if c]


def bsd_sites(command: str) -> list[str]:
    """The sed word of each BSD-form `sed -i ''` in COMMAND, in order.
    ValueError on an unbalanced quote."""
    found: list[str] = []
    for words in simple_commands(command):
        for at, word in enumerate(words):
            if os.path.basename(word) not in SED_NAMES:
                continue
            if any(_IN_PLACE.fullmatch(flag) and nxt == ""
                   for flag, nxt in itertools.pairwise(words[at + 1:])):
                found.append(word)
    return found


# --- which sed ------------------------------------------------------------------

def probe(word: str, path: str | None = None, cwd: str | None = None) -> bool:
    """Is the sed WORD names GNU sed? Any doubt is False (allow)."""
    if os.path.basename(word) == "gsed":
        return True
    if "/" in word:
        exe = os.path.expanduser(word)
        if not os.path.isabs(exe) and cwd:
            exe = os.path.join(cwd, exe)
    else:
        exe = shutil.which(word, path=path)
    if not exe:
        return False
    try:
        # its own process group, so a timeout kills whatever it forked too: a
        # child left holding the pipe would keep us waiting past the cap
        proc = subprocess.Popen([exe, "--version"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except (OSError, ValueError):
        return False
    try:
        out, _ = proc.communicate(timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.stdout.close()
        proc.wait()
        return False
    return b"GNU sed" in out


# --- the GNU form and the message --------------------------------------------

def gnu_form(command: str) -> str:
    """COMMAND with each BSD-form empty argument, and the blanks before it,
    deleted IN PLACE. A candidate is cut only when the parser agrees it was
    one (the count of sites falls), so a `-i ''` inside quotes or a heredoc
    body is left as written."""
    fixed = command
    count = len(bsd_sites(fixed))
    for match in reversed(list(_CUT.finditer(command))):
        start, end = match.span(1)
        trial = fixed[:start] + fixed[end:]
        try:
            left = len(bsd_sites(trial))
        except ValueError:
            continue
        if left < count:
            fixed, count = trial, left
    return fixed


def shown(command: str, fixed: str) -> str:
    """The GNU form to show: a one-line command whole; a longer one, the
    logical lines that changed, a backslash-continued line whole (cutting
    never adds or removes a newline)."""
    if "\n" not in command:
        return fixed
    pairs = zip(_LINE_END.split(command), _LINE_END.split(fixed), strict=False)
    return "\n".join(new for old, new in pairs if old != new) or fixed


def message(command: str) -> str:
    form = "\n".join("    " + line for line in shown(command, gnu_form(command)).split("\n"))
    return (
        "sed -i '' is BSD sed's form. The sed this call runs is GNU sed, where -i takes "
        "no separate argument: '' becomes the script (or, after -e or -f, a file named ''), "
        "and the call fails with exit 2. Use the GNU form:\n"
        f"{form}\n"
        "(or a backup suffix joined to the flag: sed -i.bak ...). Refused by sessiontools' "
        f"sed-guard hook; {SWITCH_ENV}=off switches it off."
    )


# --- the decision --------------------------------------------------------------

def decide(command: str, *, path: str | None = None, cwd: str | None = None,
           is_gnu: IsGnu | None = None) -> str | None:
    """The refusal reason for COMMAND, or None to allow.

    Allows: no BSD-form site; a command that does not tokenise; every site's
    sed not GNU (IS_GNU, by default `probe` on PATH and CWD)."""
    try:
        sites = bsd_sites(command)
    except ValueError:
        return None
    if not sites:
        return None
    ask = is_gnu or (lambda word: probe(word, path, cwd))
    if not any(ask(word) for word in dict.fromkeys(sites)):
        return None
    return message(command)


def refusal(reason: str) -> str:
    """Claude Code's PreToolUse refusal, as measured on 2.1.325."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}})


def main(stdin=None, stdout=None, env: Mapping[str, str] | None = None) -> int:
    """Read the hook payload; print a refusal for a BSD-form sed. Always 0."""
    try:
        env = os.environ if env is None else env
        stdin = sys.stdin if stdin is None else stdin
        stdout = sys.stdout if stdout is None else stdout
        if switched_off(env):
            return 0
        payload = json.loads(stdin.read() or "null")
        if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
            return 0
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str):
            return 0
        cwd = payload.get("cwd")
        reason = decide(command, path=env.get("PATH"),
                        cwd=cwd if isinstance(cwd, str) else None)
        if reason:
            stdout.write(refusal(reason) + "\n")
            stdout.flush()
    except BaseException:  # noqa: BLE001, S110 - a hook must never break a turn
        pass
    return 0
