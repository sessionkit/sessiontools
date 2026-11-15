"""The hidden-failure note: tell an agent when a gate piped into head or
tail exits 0 while its output shows a failure.

    pytest -q 2>&1 | tail -20

reports tail's status, not pytest's: the call exits 0 with `3 failed` on
the screen. This module says so, in ONE line of `additionalContext`, and
does nothing else. It never blocks, never rewrites a command and changes no
exit code (ST-319, from ST-305's measured week).

It runs as two plugin hooks, through the POSIX shim `hooks/hidden-failure`:

    pre    PreToolUse, matcher Bash. A gate piped into `head` (never `tail`):
           head may cut the failure off, and the pipe reports head's status.
    post   PostToolUse, matcher Bash. A gate piped into `head` or `tail`
           that exited 0 and whose output (stdout + stderr) matches a
           failure marker.

A GATE is a stage named in `GATES` (pytest, make, `message send`,
`git commit`, `git push`, and `python -m pytest` / `uv run pytest`). The
pipeline must be the one whose status reaches the call's exit: the LAST
top-level statement's, through its and-or list (`andor_status`). `||` is
never a pipe; heredoc bodies and quoted text are not code. A command that
names `pipefail` gets nothing: the pipe keeps the gate's status there.

"EXITED 0" (do-001's measurement, Claude Code 2.1.325): PostToolUse fires
only for a Bash call Claude Code counts as succeeded; a failed one fires
PostToolUseFailure, which this hook never registers. The success path still
carries a non-zero exit in two cases, and a background call has no status
yet, so the note is REFUSED when `tool_response` is not an object, when
`interrupted` is true, when `returnCodeInterpretation` is a non-empty
string, or when `backgroundTaskId` is set. A key of an unexpected type is
an unknown status, and unknown is not 0. No `Exit code N` line is read.

SEATS ONLY. The shim exits before python starts unless `TASKMASTER_ROLE` or
`CLAUDE_CODE_AGENT` is set and non-empty; `SESSIONTOOLS_HOOK_HIDDEN_FAILURE`
= off/0/false/no silences it on a seat. `main()` checks both again.

PRIVACY. The note names the gate (its stage name, e.g. `pytest`), the
reader (`head` or `tail`) and the marker's label, and quotes nothing else:
no output, no argument. Nothing is logged or written.

SELF-CONTAINED, ON PURPOSE. The shim loads this FILE by path, never
`import sessiontools`: the package's `__init__` costs ~40 ms of imports, and
the hook's budget is 50 ms on a 10 KB output. So this module imports the
standard library only (`tests/test_hidden_failure_hook.py` pins it).

The parser below, from `GATE_CORE` to `andor_status`, is a PORT of ST-305
`findings/scripts/scan.py` (store f85819f869), unchanged in behaviour; its
17 tests are ported beside it (`tests/test_hidden_failure_scan.py`). The
corpus scan, the shapes and the report are not ported.
"""
# ruff: noqa: FURB167, RUF023, PIE810, SIM103 - the ported parser stays as scan.py wrote it

from __future__ import annotations

import json
import os
import re
import sys

# ---------------------------------------------------------------- vocabulary

GATE_CORE = {"pytest", "make", "message send", "git commit"}
GATE_NO_MSG = GATE_CORE - {"message send"}
GATE_EXTENDED = {
    "pytest", "make", "message send", "git commit", "git push", "tox", "nox",
    "npm test", "npm run", "pnpm", "yarn", "cargo", "go test", "go build",
    "go vet", "ruff", "mypy", "pyright", "shellcheck", "bats", "pre-commit",
    "just", "uv run pytest", "python -m pytest", "tm", "gmake",
}

WRAPPERS = {"nice", "time", "timeout", "env", "command", "exec", "sudo",
            "nohup", "stdbuf", "caffeinate", "gtimeout"}

FAIL_MARKERS = {
    "pytest_failed": re.compile(r"\b[1-9]\d* (failed|errors?)\b|^FAILED |^ERROR ", re.M),
    "make_error": re.compile(r"make(\[\d+\])?: \*\*\*"),
    "traceback": re.compile(r"Traceback \(most recent call last\)"),
    "fatal_or_error_prefix": re.compile(r"^(fatal|error|ERROR|Error)(:| )", re.M),
    "not_found": re.compile(r"command not found|No such file or directory"),
    "permission_denied": re.compile(r"Permission denied"),
    "git_nothing_to_commit": re.compile(r"nothing to commit|no changes added to commit"),
}


# ------------------------------------------------------------------ tokenizer

class Tok:
    __slots__ = ("kind", "text", "nested_pipes", "has_subst")

    def __init__(self, kind, text="", nested_pipes=0, has_subst=False):
        self.kind = kind          # 'word' | 'op'
        self.text = text
        self.nested_pipes = nested_pipes
        self.has_subst = has_subst


OPS = ["&&", "||", "|&", ";;", "|", ";", "&", "\n", "(", ")"]


def _skip_subst(s, i, opener, closer):
    """From s[i] just after `opener`, return (end_index, inner_text)."""
    depth, j, n = 1, i, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = s.find("'", j + 1)
            j = n if k < 0 else k + 1
            continue
        if c == '"':
            j = _skip_dq(s, j + 1)
            continue
        if s.startswith(opener, j) and opener != closer:
            depth += 1
            j += len(opener)
            continue
        if s.startswith(closer, j):
            depth -= 1
            if depth == 0:
                return j + len(closer), s[i:j]
            j += len(closer)
            continue
        j += 1
    return n, s[i:]


def _skip_dq(s, j):
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            return j + 1
        if s.startswith("$(", j):
            j, _ = _skip_subst(s, j + 2, "(", ")")
            continue
        j += 1
    return n


def tokenize(s):
    """Return a list of Tok. Heredoc bodies are dropped; quotes kept in words."""
    toks, i, n = [], 0, len(s)
    pending_heredocs = []  # (delimiter, strip_tabs)
    word, nested, subst = [], 0, False

    def flush():
        nonlocal word, nested, subst
        if word:
            toks.append(Tok("word", "".join(word), nested, subst))
        word, nested, subst = [], 0, False

    while i < n:
        c = s[i]
        if c == "\n":
            flush()
            toks.append(Tok("op", "\n"))
            i += 1
            # consume heredoc bodies queued on the line just ended
            for delim, strip in pending_heredocs:
                while i < n:
                    e = s.find("\n", i)
                    line = s[i:] if e < 0 else s[i:e]
                    i = n if e < 0 else e + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            pending_heredocs = []
            continue
        if c in " \t":
            flush()
            i += 1
            continue
        if c == "#" and not word:
            e = s.find("\n", i)
            i = n if e < 0 else e
            continue
        if c == "\\":
            word.append(s[i:i + 2])
            i += 2
            continue
        if c == "'":
            k = s.find("'", i + 1)
            k = n if k < 0 else k + 1
            word.append(s[i:k])
            i = k
            continue
        if c == '"':
            k = _skip_dq(s, i + 1)
            inner = s[i:k]
            if "$(" in inner or "`" in inner:
                subst = True
            word.append(inner)
            i = k
            continue
        if s.startswith("$((", i):
            k, inner = _skip_subst(s, i + 3, "((", "))")
            word.append("$((" + inner + "))")
            i = k
            continue
        if s.startswith("$(", i) or s.startswith("<(", i) or s.startswith(">(", i):
            k, inner = _skip_subst(s, i + 2, "(", ")")
            nested += count_pipes(inner)
            subst = True
            word.append(s[i:i + 2] + "…)")
            i = k
            continue
        if c == "`":
            k = s.find("`", i + 1)
            k = n if k < 0 else k + 1
            nested += count_pipes(s[i + 1:k - 1])
            subst = True
            word.append("`…`")
            i = k
            continue
        if s.startswith("<<", i) and not s.startswith("<<<", i):
            flush()
            j = i + 2
            strip = False
            if j < n and s[j] == "-":
                strip, j = True, j + 1
            while j < n and s[j] in " \t":
                j += 1
            m = re.match(r"""(['"]?)([^\s'";|&<>()]+)\1""", s[j:])
            if m:
                pending_heredocs.append((m.group(2), strip))
                j += m.end()
            toks.append(Tok("word", "<<HEREDOC"))
            i = j
            continue
        # redirections like 2>&1, >|, &> are words, not operators
        if c in "<>" or (c.isdigit() and i + 1 < n and s[i + 1] in "<>" and not word):
            j = i
            while j < n and s[j] in "0123456789<>&|-" and not (s[j] == "|" and s[j - 1] not in "><"):
                j += 1
            word.append(s[i:j])
            i = j
            continue
        if c == "&" and i + 1 < n and s[i + 1] == ">":
            word.append("&>")
            i += 2
            continue
        for op in OPS:
            if s.startswith(op, i):
                flush()
                toks.append(Tok("op", op))
                i += len(op)
                break
        else:
            word.append(c)
            i += 1
    flush()
    return toks


def count_pipes(inner):
    try:
        return sum(1 for t in tokenize(inner) if t.kind == "op" and t.text in ("|", "|&"))
    except RecursionError:
        return 0


# --------------------------------------------------------------------- parser

COMPOUND_OPEN = {"for", "while", "until", "if", "case", "{", "select"}
COMPOUND_CLOSE = {"done", "fi", "esac", "}"}


def parse(command):
    """-> list of statements; statement = list of (connector, pipeline);
    pipeline = dict(stages=[[words]], depth=int)."""
    toks = tokenize(command)
    statements, cur_stmt, cur_pipe, cur_stage = [], [], [], []
    connector = None
    depth = 0
    nested_total = sum(t.nested_pipes for t in toks)

    def end_stage():
        nonlocal cur_stage
        cur_pipe.append(cur_stage)
        cur_stage = []

    def end_pipe():
        nonlocal cur_pipe, connector
        end_stage()
        stages = [st for st in cur_pipe if st]
        if stages:
            cur_stmt.append((connector, {"stages": stages, "depth": depth_at_start[0]}))
        cur_pipe = []

    depth_at_start = [0]

    def end_stmt():
        nonlocal cur_stmt, connector
        end_pipe()
        if cur_stmt:
            statements.append(cur_stmt)
        cur_stmt, connector = [], None
        depth_at_start[0] = depth

    for t in toks:
        if t.kind == "word":
            w = t.text
            if not cur_stage and w in COMPOUND_OPEN:
                depth += 1
            elif w in COMPOUND_CLOSE:
                depth = max(0, depth - 1)
            if not cur_stage and not cur_pipe and not cur_stmt:
                depth_at_start[0] = depth
            cur_stage.append(w)
            continue
        op = t.text
        if op in ("|", "|&"):
            end_stage()
        elif op in ("&&", "||"):
            end_pipe()
            connector = op
            depth_at_start[0] = depth
        elif op in (";", "&", "\n", ";;"):
            end_stmt()
        elif op == "(":
            depth += 1
            cur_stage.append("(")
        elif op == ")":
            depth = max(0, depth - 1)
            cur_stage.append(")")
    end_stmt()
    return statements, nested_total


# ------------------------------------------------------------ stage semantics

def stage_words(words):
    """Drop leading env assignments, redirections and wrappers."""
    out = list(words)
    while out:
        w = out[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) or w in ("(", "{", "!", "then", "do", "else", "elif"):
            out.pop(0)
            continue
        if re.match(r"^\d*[<>]", w) or w.startswith("&>"):
            out.pop(0)
            # a bare operator (`>`, `2>`, `<`) takes the next word as its target
            if re.match(r"^(\d*[<>]+|&>>?)$", w) and out:
                out.pop(0)
            continue
        base = os.path.basename(w.strip("'\""))
        if base in WRAPPERS:
            out.pop(0)
            # drop wrapper options and their numeric args (nice -n 19, timeout 60)
            while out and (out[0].startswith("-") or re.match(r"^[\d.]+[smhd]?$", out[0])):
                out.pop(0)
            continue
        break
    return out


def stage_name(words):
    w = stage_words(words)
    if not w:
        return ""
    base = os.path.basename(w[0].strip("'\""))
    rest = [x for x in w[1:] if not x.startswith("-")]
    if base in ("python", "python3") and len(w) > 2 and w[1] == "-m":
        return f"python -m {w[2]}"
    if base == "uv" and len(w) > 2 and w[1] == "run":
        return "uv run " + os.path.basename(w[2])
    if base in ("git", "message", "go", "npm", "tm", "session") and rest:
        return f"{base} {rest[0]}"
    return base


def is_gate(name, vocab):
    if name in vocab:
        return True
    first = name.split(" ")[0]
    if name.startswith("python -m pytest") or name.startswith("uv run pytest"):
        return True
    if vocab is GATE_EXTENDED and first in ("make", "gmake", "pytest", "tox", "nox",
                                             "cargo", "ruff", "mypy", "pyright",
                                             "shellcheck", "bats", "pre-commit", "just", "tm"):
        return True
    if first in ("make", "pytest"):
        return True
    return False


def reader_kind(words):
    """Classify a downstream stage: 'head', 'tail', 'grep-q', 'grep-m', 'sed-q',
    'awk-exit', 'other-early' or None (reads to EOF)."""
    w = stage_words(words)
    if not w:
        return None
    base = os.path.basename(w[0])
    args = " ".join(w[1:])
    if base in ("head", "ghead"):
        return "head"
    if base in ("tail", "gtail"):
        return "tail"
    if base in ("grep", "egrep", "fgrep", "rg", "ggrep"):
        flags = "".join(x[1:] for x in w[1:] if x.startswith("-") and not x.startswith("--"))
        if "q" in flags or "--quiet" in args or "--silent" in args:
            return "grep-q"
        if "m" in flags or "--max-count" in args:
            return "grep-m"
        if base == "rg" and ("-l" in w or "--files-with-matches" in args):
            return None
        return None
    if base in ("sed", "gsed") and re.search(r"(^|[^a-z])\d*q\b|\bQ\b", args):
        return "sed-q"
    if base in ("awk", "gawk") and re.search(r"\bexit\b", args):
        return "awk-exit"
    if base == "read" or (base == "python3" and "readline" in args):
        return "other-early"
    return None


def head_limit(words):
    w = stage_words(words)
    if not w or os.path.basename(w[0]) not in ("head", "ghead"):
        return None
    if any(x.startswith("-c") for x in w[1:]):
        return None
    for i, x in enumerate(w[1:], 1):
        m = re.match(r"^-n?(\d+)$", x)
        if m:
            return int(m.group(1))
        if x in ("-n", "--lines") and i + 1 < len(w) and w[i + 1].isdigit():
            return int(w[i + 1])
        m = re.match(r"^--lines=(\d+)$", x)
        if m:
            return int(m.group(1))
    return 10


def andor_status(pipes, target_index):
    """Evaluate the and-or list with every pipeline succeeding except
    pipes[target_index], which fails. Return True if the list ends non-zero."""
    status = 0
    ran_any = False
    for i, (conn, _p) in enumerate(pipes):
        if conn == "&&" and status != 0:
            continue
        if conn == "||" and status == 0:
            continue
        ran_any = True
        status = 1 if i == target_index else 0
    return ran_any and status != 0


# -------------------------------------------------------- the hook (ST-319)

#: The gates the note watches: ST-305's core set plus `git push` (design.md
#: decision 1). One constant, so it can be tuned.
GATES = frozenset(GATE_CORE | {"git push"})

SEAT_ENV = ("TASKMASTER_ROLE", "CLAUDE_CODE_AGENT")
SWITCH_ENV = "SESSIONTOOLS_HOOK_HIDDEN_FAILURE"
#: Exactly these (any case) switch it off on a seat; the shim agrees.
OFF_WORDS = frozenset({"off", "0", "false", "no"})

TAG = "[hidden-failure]"
KEEP = "Keep the gate's status: cmd > log 2>&1; s=$?; tail log; exit $s"

EVENTS = {"pre": "PreToolUse", "post": "PostToolUse"}


def active(env) -> bool:
    """A seat (a marker set and non-empty) that has not switched it off."""
    if not any(env.get(name) for name in SEAT_ENV):
        return False
    return env.get(SWITCH_ENV, "").lower() not in OFF_WORDS


def find(command: str, readers) -> tuple[str, str] | None:
    """(gate name, reader kind) for the first gate piped into a reader in
    `readers`, in a pipeline whose status reaches the call's exit: top
    level, in the LAST statement, carried by its and-or list. Else None."""
    if "pipefail" in command:
        return None
    statements, _nested = parse(command)
    if not statements:
        return None
    stmt = statements[-1]
    for pi, (_conn, p) in enumerate(stmt):
        stages = p["stages"]
        if p["depth"] != 0 or len(stages) < 2 or not andor_status(stmt, pi):
            continue
        names = [stage_name(s) for s in stages]
        gates = [i for i, n in enumerate(names) if is_gate(n, GATES)]
        if not gates:
            continue
        first = min(gates)
        for i in range(first + 1, len(stages)):
            kind = reader_kind(stages[i])
            if kind in readers:
                return names[first], kind
    return None


def _command(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("tool_name", "Bash") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    return command if isinstance(command, str) and command else None


def pre(payload) -> str | None:
    """The PreToolUse line for a gate piped into head, or None."""
    command = _command(payload)
    hit = command and find(command, ("head",))
    if not hit:
        return None
    gate, reader = hit
    return (f"{TAG} {gate} | {reader} reports {reader}'s status, not {gate}'s, "
            f"and {reader} may cut off the failure. {KEEP}")


def output(response) -> str | None:
    """The call's output when its status is PROVEN 0, else None.

    Refused (do-001): not an object; `interrupted` true; a non-empty
    `returnCodeInterpretation` (the last command exited 1 and was judged
    no error); `backgroundTaskId` set (not finished). Any of these keys of
    an unexpected type is an unknown status, and unknown is not 0."""
    if not isinstance(response, dict):
        return None
    interrupted = response.get("interrupted")
    if interrupted is not None and interrupted is not False:
        return None
    interp = response.get("returnCodeInterpretation")
    if interp is not None and interp != "":
        return None
    if response.get("backgroundTaskId") not in (None, ""):
        return None
    parts = []
    for key in ("stdout", "stderr"):
        value = response.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return None
        parts.append(value)
    return "\n".join(parts)


def marker(text: str) -> str | None:
    """The label of the first FAIL_MARKERS pattern the text matches."""
    for label, rx in FAIL_MARKERS.items():
        if rx.search(text):
            return label
    return None


def post(payload) -> str | None:
    """The PostToolUse note for a gate piped into head/tail that exited 0
    with failure text in its output, or None."""
    command = _command(payload)
    hit = command and find(command, ("head", "tail"))
    if not hit:
        return None
    text = output(payload.get("tool_response"))
    label = text and marker(text)
    if not label:
        return None
    gate, reader = hit
    return (f"{TAG} {gate} | {reader} exited 0 but its output shows a failure "
            f"({label}): the pipe reported {reader}'s status, not {gate}'s. {KEEP}")


def hook_output(verb: str, line: str) -> str:
    """The hook JSON: additionalContext only. Never a `decision`, a
    `permissionDecision` or an `updatedInput`."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": EVENTS[verb], "additionalContext": line}})


def decide(verb: str, env, payload) -> str | None:
    """The line to say for VERB, or None for silence."""
    if verb not in EVENTS or not active(env):
        return None
    return pre(payload) if verb == "pre" else post(payload)


def main(verb: str = "", stdin=None, stdout=None, env=None) -> int:
    """Read the hook payload, maybe print one line of JSON. Always 0."""
    try:
        env = os.environ if env is None else env
        if verb not in EVENTS or not active(env):
            return 0            # before reading stdin: nothing asked, nothing done
        stdin = sys.stdin if stdin is None else stdin
        stdout = sys.stdout if stdout is None else stdout
        payload = json.loads(stdin.read() or "null")
        line = decide(verb, env, payload)
        if line:
            stdout.write(hook_output(verb, line) + "\n")
            stdout.flush()
    except BaseException:  # noqa: BLE001, S110 - a hook must never break a turn
        pass
    return 0
