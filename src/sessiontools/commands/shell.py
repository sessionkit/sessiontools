"""A Bash command cut down to the programs it runs. Standard library only.

`programs(src)` reads one command as the Bash tool was given it and returns
every program it runs, in reading order: a command, then the commands inside
its `$(...)`. What is NOT a program is gone:

    VAR=value in front of a command    the settings, not the program
    a here-document's body             input, not commands
    a redirection and its target       `2>&1`, `> file`, `<<< word`
    if/then/for/while/case/{ }         the words that join commands
    a case pattern, a for list         data
    a function the call defines        its body is read once; its calls are not

Three wrappers are followed to what they run, and VIA says how:
`bash -c '...'` ("bash -c"), `uv run ... PROGRAM` ("uv run") and
`ssh HOST COMMAND` ("ssh:HOST": the ssh itself is kept too). `timeout`,
`env`, `nohup` and their kind are stepped over, each with its options and
the values those take (`sudo -u root ls` runs `ls`). `command -v NAME` is a
lookup, so it stays `command`. A program that is a variable the same call
set (`g='git -C x'; $g status`) is read through it.

Which option takes a value is never guessed: it is in a table below, by
program (`tmux -L server ls` is `tmux ls` because -L is in tmux's row). An
option that is in no table takes none.

This is a reader, not a shell: nothing is expanded, globbed or run. It
reads what Bash would parse and guesses nowhere: what it cannot follow
(`eval`, `xargs CMD`, `find -exec CMD`, an alias) is reported as written.
Measured against bashlex over 36,904 Bash calls of four seats in one week
(ST-320): see tests/test_commands_shell.py for the cases that came of it.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass

#: Words that open, join or close a compound command: never a program.
KEYWORDS = frozenset({"if", "then", "else", "elif", "fi", "while", "until", "do",
                      "done", "{", "}", "!", "time", "coproc"})
#: Builtins that take NAME=value words: those are read as settings too.
DECLARES = frozenset({"export", "local", "declare", "readonly", "typeset"})
#: Commands that run another command: stepped over, with their own options.
WRAPPERS = frozenset({"timeout", "gtimeout", "time", "nohup", "env", "command", "exec",
                      "sudo", "nice", "caffeinate", "builtin", "stdbuf", "noglob"})
SHELLS = frozenset({"bash", "sh", "zsh"})
#: `uv run` options that take a value, so the value is not the program.
UV_VALUE_OPTS = frozenset({
    "--with", "--with-requirements", "--with-editable", "--project", "--directory",
    "-p", "--python", "--from", "--script-path", "--env-file", "--group", "--extra",
    "--index", "--default-index", "-c", "--config-file"})
SSH_VALUE_OPTS = frozenset({"-o", "-p", "-i", "-l", "-F", "-J", "-S", "-E", "-L", "-R",
                            "-D", "-W", "-b", "-c", "-m", "-e"})
_TIMEOUT_OPTS = frozenset({"-s", "--signal", "-k", "--kill-after"})
#: A wrapper's own options that take a value, so the value is not the program.
WRAPPER_VALUE_OPTS: dict[str, frozenset[str]] = {
    "timeout": _TIMEOUT_OPTS, "gtimeout": _TIMEOUT_OPTS,
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
                       "-C", "--close-from", "-U", "--other-user", "-D", "--chdir",
                       "-R", "--chroot", "-T", "--command-timeout", "-r", "--role",
                       "-t", "--type"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "exec": frozenset({"-a"}),
    "caffeinate": frozenset({"-t", "-w"}),
}
#: A VERBED program's options that take a value and may sit before its verb,
#: so the value is not the verb.
VALUE_OPTS: dict[str, frozenset[str]] = {
    "git": frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"}),
    "tmux": frozenset({"-L", "-S", "-f", "-c", "-T"}),
    "make": frozenset({"-C", "--directory", "-f", "--file", "--makefile", "-I",
                       "--include-dir", "-o", "--old-file", "--assume-old", "-W",
                       "--what-if", "--new-file", "--assume-new", "-E", "--eval"}),
    "kubectl": frozenset({"-n", "--namespace", "--context", "--cluster", "-s", "--server",
                          "--kubeconfig", "--user", "--token", "--as", "--as-group",
                          "--request-timeout", "-v", "--v", "--cache-dir",
                          "--certificate-authority", "--client-certificate",
                          "--client-key", "--tls-server-name"}),
    "docker": frozenset({"-H", "--host", "-c", "--context", "--config", "-l",
                         "--log-level", "--tlscacert", "--tlscert", "--tlskey"}),
    "podman": frozenset({"-c", "--connection", "--url", "--identity", "--log-level",
                         "--root", "--runroot", "--storage-driver", "--storage-opt",
                         "--tmpdir", "--runtime", "--namespace"}),
    "npm": frozenset({"--prefix", "-C", "-w", "--workspace", "--registry", "--userconfig",
                      "--cache", "--loglevel"}),
    "cargo": frozenset({"--explain", "--color", "-C", "--config", "-Z"}),
    "systemctl": frozenset({"-H", "--host", "-M", "--machine", "-t", "--type", "-p",
                            "--property", "-s", "--signal", "-n", "--lines", "-o",
                            "--output", "--state", "--root", "--job-mode",
                            "--kill-whom", "--kill-who"}),
    "apt": frozenset({"-o", "--option", "-c", "--config-file", "-t", "--target-release"}),
    "notmuch": frozenset({"--config", "--uuid"}),
    "uv": frozenset({"--directory", "--project", "--config-file", "--cache-dir", "--color"}),
    "atom": frozenset({"--store"}),
    "tm": frozenset({"-c", "--config", "-r", "--root"}),
}
#: Options whose value is optional and a number: `make -j 8 all`, `make -j all`.
NUMBER_OPTS: dict[str, frozenset[str]] = {
    "make": frozenset({"-j", "--jobs", "-l", "--load-average", "--max-load"}),
}
#: Programs that run a script file: the script is the program.
INTERPRETERS = frozenset({"python", "python3", "node", "bash", "sh", "zsh", "perl",
                          "ruby", "bats", "source", "."})
#: Programs whose first plain argument is a verb (`git log`, `session list`).
#: The value: the verbs that take a second word (`git worktree add`).
VERBED: dict[str, frozenset[str]] = {
    "git": frozenset({"worktree", "stash", "remote", "config", "notes", "ready", "egg"}),
    "session": frozenset({"default-model", "sound", "account", "web", "pane-state",
                          "settings"}),
    "gh": frozenset({"pr", "issue", "api", "repo", "release", "run", "auth"}),
    "uv": frozenset({"tool", "pip"}), "message": frozenset({"box", "subscription"}),
    "pager": frozenset({"rule", "hook", "focus"}), "atom": frozenset({"store", "type"}),
    "tm": frozenset({"task", "slice"}),
    **{name: frozenset() for name in (
        "memory", "mouth", "note", "skill", "notmuch", "brew", "tmux", "launchctl",
        "docker", "podman", "npm", "make", "cargo", "kubectl", "systemctl", "apt")},
}

_ASSIGN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?\+?=")
_REDIR = re.compile(r"\d*(?:<<<|<<-?|<>|>>|>&|<&|>\||&>>|&>|>|<)")
_FUNC_NAME = re.compile(r"[A-Za-z_][\w.:-]*")
_VARHEAD = re.compile(r"\$(?:\{(\w+)\}|(\w+))(.*)", re.DOTALL)
_DURATION = re.compile(r"\d+(\.\d+)?[smhd]?")
_SHELL_C = re.compile(r"-[A-Za-z]*c")     # -c, -lc, -xc: the next word is the script
_NUMBER = re.compile(r"\d+(\.\d+)?")


class _Scan:
    """One pass over SRC. `out` is every simple command's words, a command
    before the commands inside its substitutions; `env` the NAME=value
    settings (the first of each name), `funcs` the functions it defines."""

    def __init__(self, src: str):
        self.s = src
        self.n = len(src)
        self.i = 0
        self.out: list[list[str] | None] = []
        self.env: dict[str, str] = {}
        self.funcs: set[str] = set()
        self.pending: list[str] = []      # here-documents whose bodies are still ahead
        self.cond = False                 # inside [[ ... ]]: operators are words

    def run(self) -> list[list[str]]:
        while self.i < self.n:            # a stray ")" or "esac" never stops the read
            self._list(None)
            self.i += 1
        return [w for w in self.out if w]

    # ------------------------------------------------------------- lists

    def _list(self, close: str | None, case: bool = False) -> None:
        """Commands up to CLOSE, the end, or (CASE) the end of a case clause.
        The closing character itself is left for the caller."""
        s = self.s
        while self.i < self.n:
            if self._command(close, case) or self.i >= self.n:
                return
            c = s[self.i]
            if c == close:
                return
            if c == "\n":
                self.i += 1
                self._bodies()
            elif c == ";":
                if case and s.startswith((";;", ";&"), self.i):
                    return
                self.i += 1
            elif c in "&|":
                self.i += 2 if s[self.i + 1:self.i + 2] in ("&", "|") else 1
            elif c == ")" and close is None and not case:
                return                    # not ours: `run` steps over it
            else:
                self.i += 1               # always move on

    def _command(self, close: str | None, case: bool) -> bool:
        """One simple command into its slot. True: it met `esac` (left unread)."""
        s = self.s
        slot = len(self.out)
        self.out.append(None)
        words: list[str] = []
        try:
            while self.i < self.n:
                self._blanks()
                if self.i >= self.n:
                    break
                if self.cond:
                    w = self._word()
                    if not w:
                        self.cond = False
                        break
                    words.append(w)
                    self.cond = w != "]]"
                    continue
                c = s[self.i]
                if c == "#":
                    self._to_eol()
                    break
                if c in "\n;|)" or c == close or (c == "&" and not s.startswith("&>", self.i)):
                    break
                if c == "(":
                    if words:
                        self.i += 1       # not shell we know: step over
                    elif s.startswith("((", self.i):
                        self._balanced()  # (( arithmetic ))
                    else:
                        self.i += 1       # ( a subshell )
                        self._nested()
                    continue
                if not s.startswith(("<(", ">("), self.i):
                    m = _REDIR.match(s, self.i)
                    if m:
                        self._redirect(m)
                        continue
                start = self.i
                w = self._word()
                raw = s[start:self.i]
                if self.i == start:       # nothing read: never loop in place
                    self.i += 1
                    continue
                a = _ASSIGN.match(s, start)
                if a and a.end() <= self.i and (not words or words[0] in DECLARES):
                    self.env.setdefault(a.group(1), w[a.end() - start:] if raw == w
                                        else w.partition("=")[2])
                    if s.startswith("(", self.i) and s[self.i - 1] == "=":
                        self._array()
                    if words:
                        words.append(w)
                    continue
                if words:
                    words.append(w)
                    continue
                if raw in KEYWORDS:
                    if raw == "time":     # the keyword's one option
                        self._blanks()
                        self.i += 2 if s.startswith("-p ", self.i) else 0
                    continue
                if raw in ("for", "select"):
                    self._header()
                elif raw == "case":
                    self._case(close)
                elif raw == "esac" and case:
                    self.i = start
                    return True
                elif raw == "function":
                    self._blanks()
                    self.funcs.add(self._word())
                    self._blanks()
                    if s.startswith("()", self.i):
                        self.i += 2
                elif _FUNC_NAME.fullmatch(raw) and self._parens_follow():
                    self.funcs.add(raw)
                else:
                    words.append(w)
                    self.cond = raw == "[["
            return False
        finally:
            if words:
                self.out[slot] = words

    # ------------------------------------------------------ compound parts

    def _header(self) -> None:
        """`for NAME in WORDS` / `for (( ... ))`: data to the end of the line.
        A substitution among the words is still read."""
        self._blanks()
        if self.s.startswith("((", self.i):
            self._balanced()
            return
        while self.i < self.n and self.s[self.i] not in "\n;":
            before = self.i
            self._blanks()
            self._word()
            if self.i == before:
                self.i += 1

    def _case(self, close: str | None) -> None:
        """After `case`: the subject and `in`, then each clause: its pattern
        skipped, its commands read, to `esac`."""
        s = self.s
        self._blanks()
        self._word()
        self._blanks(newlines=True)
        if s.startswith("in", self.i):
            self.i += 2
        while self.i < self.n:
            self._blanks(newlines=True)
            if s.startswith("esac", self.i):
                self.i += 4
                return
            if self.i >= self.n or s[self.i] == close:
                return
            self._pattern()
            self._list(close, case=True)
            while self.i < self.n and s[self.i] in ";&":
                self.i += 1

    def _pattern(self) -> None:
        s = self.s
        while self.i < self.n:
            c = s[self.i]
            if c == "\\":
                self.i += 2
            elif c in "'\"":
                j = s.find(c, self.i + 1)
                self.i = self.n if j < 0 else j + 1
            elif c == ")":
                self.i += 1
                return
            else:
                self.i += 1

    def _array(self) -> None:
        """`NAME=( words )`: data, though a substitution inside is read."""
        self.i += 1
        while self.i < self.n and self.s[self.i] != ")":
            before = self.i
            self._blanks(newlines=True)
            self._word()
            if self.i == before:
                self.i += 1
        self._eat(")")

    def _redirect(self, m: re.Match) -> None:
        op = m.group().lstrip("0123456789")
        self.i = m.end()
        self._blanks()
        target = self._word()
        if op in ("<<", "<<-"):
            self.pending.append(target)

    def _bodies(self) -> None:
        """Past the body of every here-document opened on the line just ended."""
        s = self.s
        for delim in self.pending:
            while self.i < self.n:
                j = s.find("\n", self.i)
                line, self.i = (s[self.i:], self.n) if j < 0 else (s[self.i:j], j + 1)
                if line.strip() == delim:
                    break
        self.pending.clear()

    # -------------------------------------------------------------- words

    def _word(self) -> str:
        """One word from here, its quotes gone; a substitution stays as it
        was written, and the commands inside it are read."""
        s, n = self.s, self.n
        if s.startswith(("<(", ">("), self.i):
            b = self.i
            self.i += 2
            self._nested()
            return s[b:self.i]
        buf: list[str] = []
        while self.i < n:
            c = s[self.i]
            if c == "\\":
                if s[self.i + 1:self.i + 2] != "\n":
                    buf.append(s[self.i + 1:self.i + 2])
                self.i += 2
            elif c == "'":
                j = s.find("'", self.i + 1)
                j = n if j < 0 else j
                buf.append(s[self.i + 1:j])
                self.i = j + 1
            elif c == '"':
                self.i += 1
                self._dquote(buf)
            elif c == "$" and s[self.i + 1:self.i + 2] == "'":
                j = self.i + 2
                while j < n and s[j] != "'":
                    j += 2 if s[j] == "\\" else 1
                buf.append(s[self.i + 2:j])
                self.i = j + 1
            elif c == "$" and s[self.i + 1:self.i + 2] in ("(", "{"):
                buf.append(self._dollar())
            elif c == "`":
                buf.append(self._backtick())
            elif c in " \t\n" or (c == ";" and not s.startswith(";", self.i + 1) and self.cond) or not self.cond and c in ";&|()<>":
                break
            else:
                buf.append(c)
                self.i += 1
        return "".join(buf)

    def _dquote(self, buf: list[str]) -> None:
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\":
                nxt = s[self.i + 1:self.i + 2]
                if nxt != "\n":
                    buf.append(nxt if nxt in '$`"\\' else "\\" + nxt)
                self.i += 2
            elif c == "$" and s[self.i + 1:self.i + 2] in ("(", "{"):
                buf.append(self._dollar())
            elif c == "`":
                buf.append(self._backtick())
            else:
                buf.append(c)
                self.i += 1

    def _dollar(self) -> str:
        """`$(command)`, `$((arithmetic))` or `${parameter}`, as written."""
        s, b = self.s, self.i
        if s.startswith("$((", b):
            self.i += 1
            self._balanced()
        elif s.startswith("$(", b):
            self.i += 2
            self._nested()
        else:
            self.i += 1
            self._balanced("{", "}")
        return s[b:self.i]

    def _backtick(self) -> str:
        s, b = self.s, self.i
        j = b + 1
        while j < self.n and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        inner = _Scan(s[b + 1:j].replace("\\`", "`"))
        self.out.extend(inner.run())
        for k, v in inner.env.items():
            self.env.setdefault(k, v)
        self.funcs |= inner.funcs
        self.i = min(j + 1, self.n)
        return s[b:self.i]

    # ------------------------------------------------------------ helpers

    def _nested(self) -> None:
        """The list inside `$( )`, `<( )` or `( )`, and its closing paren.
        `[[ ]]` mode does not reach inside it."""
        cond, self.cond = self.cond, False
        self._list(")")
        self._eat(")")
        self.cond = cond

    def _blanks(self, newlines: bool = False) -> None:
        s = self.s
        while self.i < self.n:
            c = s[self.i]
            if c in " \t" or (c == "\\" and s.startswith("\n", self.i + 1)):
                self.i += 2 if c == "\\" else 1
            elif newlines and c == "\n":
                self.i += 1
                self._bodies()
            elif newlines and c == "#":
                self._to_eol()
            else:
                return

    def _to_eol(self) -> None:
        j = self.s.find("\n", self.i)
        self.i = self.n if j < 0 else j

    def _eat(self, char: str) -> None:
        if self.s.startswith(char, self.i):
            self.i += 1

    def _parens_follow(self) -> bool:
        """`name()` or `name ()`: a function definition; steps past the ()."""
        j = self.i
        while self.s[j:j + 1] in (" ", "\t") and j < self.n:
            j += 1
        if self.s.startswith("()", j):
            self.i = j + 2
            return True
        return False

    def _balanced(self, opener: str = "(", closer: str = ")") -> None:
        """From an OPENER to the CLOSER that matches it, quotes respected."""
        s, depth = self.s, 0
        while self.i < self.n:
            c = s[self.i]
            if c == "\\":
                self.i += 1
            elif c in "'\"":
                j = s.find(c, self.i + 1)
                self.i = self.n - 1 if j < 0 else j
            elif c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return
            self.i += 1


# ------------------------------------------------------------- programs

@dataclass(frozen=True)
class Program:
    """One program a command runs. `name` is the program's basename, or the
    script when an interpreter runs a file; `verb` its sub-command when the
    program is known to take one, else ""; `via` how it was reached."""

    words: tuple[str, ...]
    name: str
    verb: str = ""
    via: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} {self.verb}".strip()


def split(src: str) -> list[list[str]]:
    """Every simple command in SRC as its words, nothing followed."""
    return _Scan(src).run()


def programs(src: str) -> list[Program]:
    """Every program SRC runs, in reading order (the module docstring)."""
    return [Program(tuple(words), *_head(words), via) for words, via in _follow(src, 0, "", None)]


def _follow(src: str, depth: int, via: str, scope) -> list[tuple[list[str], str]]:
    scan = _Scan(src)
    cmds = scan.run()
    env, funcs = scope or (_chained(scan.env), scan.funcs)
    out: list[tuple[list[str], str]] = []
    for words in cmds:
        words = _unwrap(_subst(_unwrap(words), env))
        if not words or words[0] in funcs:
            continue
        head = os.path.basename(words[0])
        flag = next((w for w in words[1:3] if _SHELL_C.fullmatch(w)), None)
        if head in SHELLS and flag and depth < 3:
            i = words.index(flag)
            if i + 1 < len(words):
                out += _follow(words[i + 1], depth + 1, via or f"{head} -c", (env, funcs))
                continue
        if head in ("uv", "uvx") and (head == "uvx" or words[1:2] == ["run"]):
            rest = words[1:] if head == "uvx" else words[2:]
            while rest and rest[0].startswith("-") and rest[0] != "--":
                rest = rest[2:] if rest[0] in UV_VALUE_OPTS else rest[1:]
            rest = rest[1:] if rest[:1] == ["--"] else rest
            if rest:
                out.append((rest, via or ("uvx" if head == "uvx" else "uv run")))
                continue
        out.append((words, via))
        if head == "ssh" and depth < 3:
            rest = words[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[2:] if rest[0] in SSH_VALUE_OPTS else rest[1:]
            if len(rest) > 1:
                out += _follow(" ".join(rest[1:]), depth + 1, f"ssh:{rest[0]}", None)
    return out


def _chained(env: dict[str, str]) -> dict[str, str]:
    """B=$A/x read through A, three deep at most."""
    env = dict(env)
    for _ in range(3):
        for k, v in list(env.items()):
            m = _VARHEAD.fullmatch(v)
            name = m and (m.group(1) or m.group(2))
            if name and name != k and name in env:
                env[k] = env[name] + m.group(3)
    return env


def _takes_next(word: str, valued: frozenset[str]) -> bool:
    """Is the word after option WORD its value? As getopt reads it: `-u root`
    and `--user root` yes; `-uroot` and `--user=root` carry their own; in a
    cluster (`-iu root`) the first letter that takes a value decides."""
    if word in valued:
        return True
    if not word.startswith("-") or word.startswith("--") or len(word) < 3:
        return False                      # `30s` and `A=1` are not options
    for i, letter in enumerate(word[1:], 1):
        if "-" + letter in valued:
            return i == len(word) - 1
    return False


def _number_next(word: str, valued: frozenset[str], numbered: frozenset[str]) -> bool:
    """May the word after option WORD be its number? `-j` and `--jobs` yes;
    in a cluster (`-sj 4`) only when the number option is the last letter
    and no letter before it takes the rest of the word (`-fj`: a file, `j`)."""
    if word in numbered:
        return True
    if not word.startswith("-") or word.startswith("--") or len(word) < 3:
        return False
    for i, letter in enumerate(word[1:], 1):
        if "-" + letter in valued:
            return False
        if "-" + letter in numbered:
            return i == len(word) - 1
    return False


def _is_lookup(args: list[str]) -> bool:
    """`command`'s own options hold a -v or a -V: alone, in a cluster (`-pv`)
    or after `-p`. Then it looks a name up and runs nothing."""
    for word in args:
        if word == "--" or not word.startswith("-") or word.startswith("--"):
            return False
        if "v" in word or "V" in word:
            return True
    return False


def _unwrap(words: list[str]) -> list[str]:
    """Past timeout/env/nohup, their options and those options' values."""
    words = list(words)
    while words and os.path.basename(words[0]) in WRAPPERS:
        base = os.path.basename(words[0])
        if base == "command" and _is_lookup(words[1:]):
            break                         # a lookup: it runs nothing
        valued = WRAPPER_VALUE_OPTS.get(base, frozenset())
        words = words[1:]
        while words and (words[0].startswith("-") or _ASSIGN.match(words[0])
                         or _DURATION.fullmatch(words[0])):
            words = words[2:] if _takes_next(words[0], valued) else words[1:]
    return words


def _subst(words: list[str], env: dict[str, str]) -> list[str]:
    """A leading $VAR this call set, read through; a program that IS a
    variable (`$g status`, g='git -C x') becomes its words."""
    out: list[str] = []
    for i, w in enumerate(words):
        m = _VARHEAD.fullmatch(w)
        name = m and (m.group(1) or m.group(2))
        if name and env.get(name) and "$(" not in env[name] and "`" not in env[name]:
            val = env[name]
            if i == 0 and not m.group(3) and " " in val.strip():
                try:
                    out += shlex.split(val)
                    continue
                except ValueError:
                    pass
            w = val + m.group(3)
        out.append(w)
    return out


def _head(words: list[str]) -> tuple[str, str]:
    """(name, verb) of one program's words."""
    base, rest = os.path.basename(words[0]), words[1:]
    if base in INTERPRETERS or re.fullmatch(r"python3\.\d+", base):
        args = list(rest)
        while args and args[0].startswith("-") and args[0] not in ("-c", "-m", "-"):
            args = args[1:]
        if not args or args[0] == "-":
            return base, "(inline)"
        if args[0] in ("-c", "-m"):
            return base, args[0] if args[0] == "-c" else "-m " + (args[1] if args[1:] else "?")
        return os.path.basename(args[0]), ""
    if base in VERBED:                    # its own options, up to the verb
        valued = VALUE_OPTS.get(base, frozenset())
        numbered = NUMBER_OPTS.get(base, frozenset())
        while rest and rest[0].startswith("-"):
            number = (_number_next(rest[0], valued, numbered) and rest[1:2]
                      and _NUMBER.fullmatch(rest[1]))
            rest = rest[2:] if number or _takes_next(rest[0], valued) else rest[1:]
    plain = [w for w in rest if not w.startswith("-")]
    verb = ""
    if base in VERBED and plain:
        verb = plain[0]
        if verb in VERBED[base] and len(plain) > 1:
            verb += " " + plain[1]
        if len(verb) > 30 or re.search(r"[/$=<>]", verb):
            verb = "(arg)"
    return base, verb


def show(words, width: int = 0, argmax: int = 0) -> str:
    """WORDS as one line, quoted where a shell would need it. ARGMAX cuts
    each long argument (inline code, a spoken sentence); WIDTH the line."""
    home = os.path.expanduser("~")

    def one(w: str) -> str:
        w = w.replace(home, "~")
        if argmax and len(w) > argmax:
            w = w[:argmax - 1].split("\n")[0] + "…"
        return shlex.quote(w) if re.search(r"[\s;|&$`'\"<>()*?]", w) else w

    line = " ".join(one(w) for w in words).replace("\n", "\\n")
    return line if not width or len(line) <= width else line[:width - 1] + "…"
