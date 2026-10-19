"""Shell completion for `session`: the verb tree, read from the dispatcher's
own parsers.

`tree()` walks `cli.HANDLERS`, resolving each verb's entry in `cli.PARSERS`
(a builder, or a `verbs.Group` for sub-verbs dispatched by hand), and reads
every parser's actions into a `Node`: its sub-verbs, its long flags and its
positionals. Nothing here is a second list of verbs, sub-verbs or flags, and
nothing reads `--help` text, so the tree cannot offer a word the parsers do
not accept, or miss one they do.

Building the tree builds every verb's parser, and nothing else: no verb
runs, and no tmux, transcript or network is read. Account's and start's
builders load PyYAML (their packages do at import); that happens here, in
the process that builds the tree, and never on `import sessiontools.cli`.

`script(shell)` prints that tree as a static completion script for bash or
zsh: the tree as shell `case` arms, and one engine that walks the words
already typed. At TAB the script runs no command: no Python, no tmux, no
transcript, no network.

Stdlib only.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sessiontools import cli, verbs

#: The flag that every parser built with argparse's default help answers.
HELP = "--help"


@dataclass(frozen=True)
class Flag:
    """One long option. TAKES_VALUE: the next word is its value (argparse
    nargs is not 0). VALUE says what completes that value: "dir", "file",
    or None for nothing (a number, a name, an id)."""

    name: str
    takes_value: bool = False
    value: str | None = None


@dataclass(frozen=True)
class Positional:
    """One positional argument. CHOICES are the words that complete it:
    argparse's `choices`, or the verb's own table named by
    `verbs.offer`; empty when it is free-form (a seat, a session id, a
    name). REPEATS: it takes every remaining positional word (nargs * or +)."""

    name: str
    choices: tuple[str, ...] = ()
    repeats: bool = False


@dataclass(frozen=True)
class Node:
    """A verb or sub-verb: the words that may follow it.

    SUBS are its sub-verbs. FLAGS are its long options. POSITIONALS are its
    positional arguments in order. STOP_AFTER: after this many positional
    words, everything that follows is passed through verbatim (argparse
    REMAINDER), so nothing more is offered, flags included; None when there
    is no such point."""

    name: str
    subs: Mapping[str, Node] = field(default_factory=dict)
    flags: tuple[Flag, ...] = ()
    positionals: tuple[Positional, ...] = ()
    stop_after: int | None = None

    def choices_at(self, index: int) -> tuple[str, ...]:
        """The words that complete the positional at INDEX (0-based, counting
        only positional words): its own choices, the last positional's when it
        repeats, or none."""
        if self.stop_after is not None and index >= self.stop_after:
            return ()
        if index < len(self.positionals):
            return self.positionals[index].choices
        if self.positionals and self.positionals[-1].repeats:
            return self.positionals[-1].choices
        return ()

    def first_words(self) -> tuple[str, ...]:
        """The words offered as this node's first positional word: its
        sub-verbs, then its first positional's choices. Both, when a verb has
        both: `session sound` dispatches `engine` by hand and otherwise reads
        EVENT, so `engine`, `landed` and `given` are all accepted there."""
        words = list(self.subs)
        words += [w for w in self.choices_at(0) if w not in self.subs]
        return tuple(words)


def tree() -> Node:
    """The whole `session` tree: every verb the dispatcher runs (exactly
    `cli.HANDLERS`, so `mail` and the DISABLED verbs are never in it), each
    from its `cli.PARSERS` entry, plus the top level's `--help`."""
    subs = {verb: _verb(verb, cli.PARSERS[verb]) for verb in cli.HANDLERS
            if verb != "mail" and verb not in cli.DISABLED}
    return Node(name="session", subs=subs, flags=(Flag(HELP),))


def _verb(name: str, ref: str) -> Node:
    src = verbs.resolve(ref)
    if not isinstance(src, verbs.Group):
        return node(name, src())
    subs = {sub: node(sub, verbs.resolve(sub_ref)()) for sub, sub_ref in src.subs.items()}
    if src.rest is None:
        # Every first word is dispatched by hand, and the dispatcher answers
        # --help with its own usage (`session statusline --help`).
        return Node(name=name, subs=subs, flags=(Flag(HELP),))
    rest = node(name, verbs.resolve(src.rest)())
    # A Group sub replaces the REST parser's placeholder of the same name
    # (`pane-state agent`): the hand dispatch is what runs.
    return Node(name=name, subs={**rest.subs, **subs}, flags=rest.flags,
                positionals=rest.positionals, stop_after=rest.stop_after)


def node(name: str, parser: argparse.ArgumentParser) -> Node:
    """PARSER's actions, read into a Node named NAME."""
    subs: dict[str, Node] = {}
    flags: list[Flag] = []
    positionals: list[Positional] = []
    stop_after = None
    for action in parser._actions:  # the same private read start.args makes
        if isinstance(action, argparse._SubParsersAction):
            subs.update((sub, node(sub, sp)) for sub, sp in action.choices.items())
        elif action.option_strings:
            flags += [_flag(opt, action) for opt in action.option_strings
                      if opt.startswith("--")]
        elif action.nargs == argparse.REMAINDER:
            stop_after = len(positionals)
        elif isinstance(action.nargs, int):
            # nargs=N takes N words, each one positional word in its own right.
            names = (action.metavar if isinstance(action.metavar, tuple)
                     else (action.metavar or action.dest,) * action.nargs)
            positionals += [Positional(name=n, choices=_choices(action)) for n in names]
        else:
            positionals.append(Positional(name=action.metavar or action.dest,
                                          choices=_choices(action),
                                          repeats=action.nargs in ("*", "+")))
    return Node(name=name, subs=subs, flags=tuple(flags), positionals=tuple(positionals),
                stop_after=stop_after)


def _flag(name: str, action: argparse.Action) -> Flag:
    takes_value = action.nargs != 0
    return Flag(name=name, takes_value=takes_value,
                value=_value_kind(action) if takes_value else None)


def _value_kind(action: argparse.Action) -> str | None:
    """What completes a flag's value: "dir" when its metavar is DIR, "file"
    when its metavar is PATH or FILE or its type is Path; otherwise None."""
    metavar = action.metavar if isinstance(action.metavar, str) else ""
    if metavar == "DIR":
        return "dir"
    if metavar in ("PATH", "FILE") or action.type is Path:
        return "file"
    return None


def _choices(action: argparse.Action) -> tuple[str, ...]:
    """The words that complete a positional: argparse's `choices` when the
    parser enforces them, else the table a builder named with `verbs.offer`
    (words, or rows whose first item is the word)."""
    table: Sequence | None = action.choices
    if table is None:
        table = getattr(action, verbs.COMPLETION_CHOICES, None)
    if table is None:
        return ()
    return tuple(str(row[0] if isinstance(row, tuple) else row) for row in table)


# --- the scripts ------------------------------------------------------------------------------

#: A word the scripts may hold: the tree's words are verb, sub-verb, flag and
#: choice names, and each goes into a `case` arm and a space-separated list.
#: Anything else is refused when the script is generated, never quoted into
#: a script that would break at TAB.
_SAFE = re.compile(r"[A-Za-z0-9_.:+/@%-]+")


def _word(w: str) -> str:
    if not _SAFE.fullmatch(w):
        raise ValueError(f"session completion: {w!r} cannot go into a completion script")
    return w


def _paths(n: Node, path: tuple[str, ...] = ("session",)):
    """(key, node) for N and every node under it; the key is the path of
    words from `session`, space-separated, as the engine builds it."""
    yield " ".join(map(_word, path)), n
    for name, sub in n.subs.items():
        yield from _paths(sub, (*path, name))


def _case(fn: str, subject: str, arms: list[tuple[str, str]], default: str) -> str:
    lines = [f"{fn}() {{", f"    case {subject} in"]
    lines += [f"        '{key}') {body} ;;" for key, body in arms]
    lines += [f"        *) {default} ;;", "    esac", "}"]
    return "\n".join(lines)


def _words(ws) -> str:
    return "'" + " ".join(_word(w) for w in ws) + "'"


def _tables(root: Node) -> str:
    """The tree, as five shell functions of `case` arms. Each sets
    `_session_r` (and `_session_pos` three more) for the path in $1."""
    subs, flags, values, pos, choice = [], [], [], [], []
    for key, n in _paths(root):
        if n.subs:
            subs.append((key, f"_session_r={_words(n.subs)}"))
        if n.flags:
            flags.append((key, f"_session_r={_words(f.name for f in n.flags)}"))
        values += [(f"{key}|{_word(f.name)}", f"_session_r={f.value or 'value'}")
                   for f in n.flags if f.takes_value]
        if n.positionals or n.stop_after is not None:
            rep = int(bool(n.positionals) and n.positionals[-1].repeats)
            stop = "" if n.stop_after is None else n.stop_after
            pos.append((key, (f"_session_np={len(n.positionals)} _session_rep={rep} "
                              f"_session_stopat={stop}")))
        choice += [(f"{key}|{i}", f"_session_r={_words(p.choices)}")
                   for i, p in enumerate(n.positionals) if p.choices]
    return "\n\n".join([
        "# The sub-verbs, long flags, value-taking flags and positionals at each path.",
        _case("_session_subs", "$1", subs, "_session_r="),
        _case("_session_flags", "$1", flags, "_session_r="),
        "# A value-taking flag: value (complete nothing), dir or file.",
        _case("_session_value", '"$1|$2"', values, "_session_r="),
        ("# How many positionals, whether the last repeats, and after how many\n"
         "# positional words everything is passed through (offer nothing)."),
        _case("_session_pos", "$1", pos, "_session_np=0 _session_rep=0 _session_stopat="),
        _case("_session_choice", '"$1|$2"', choice, "_session_r="),
    ])


#: The engine, the same text in both shells (bash 3.2 and later, and zsh):
#: plain variables and `case`, no arrays, no external command.
_ENGINE = r'''# _session_engine CUR WORD...: CUR is the word being completed, WORD... the
# words typed between `session` and it. Sets _session_kind (words, dir, file or
# none) and _session_cands (for words: the candidates, space-separated).
_session_engine() {
    _session_cur=$1
    shift
    local _s_at=session _s_n=0 _s_skip=0 _s_vk='' _s_dd=0 _s_w
    _session_kind=none
    _session_cands=
    for _s_w in "$@"; do
        if [ "$_s_skip" = 1 ]; then
            # bash splits --flag=value at the "=": the value is still to come.
            [ "$_s_w" = "=" ] || _s_skip=0
            continue
        fi
        _session_stopped "$_s_at" "$_s_n" && return 0
        if [ "$_s_dd" = 0 ]; then
            case $_s_w in
                --) _s_dd=1; continue ;;
                -?*=*) continue ;;
                -?*)
                    _session_value "$_s_at" "$_s_w"
                    if [ -n "$_session_r" ]; then _s_skip=1; _s_vk=$_session_r; fi
                    continue ;;
            esac
        fi
        if [ "$_s_n" = 0 ] && [ -n "$_s_w" ]; then
            _session_subs "$_s_at"
            case " $_session_r " in
                *" $_s_w "*) _s_at="$_s_at $_s_w"; continue ;;
            esac
        fi
        _s_n=$((_s_n + 1))
    done
    if [ "$_s_skip" = 1 ]; then
        case $_s_vk in dir|file) _session_kind=$_s_vk ;; esac
        return 0
    fi
    _session_stopped "$_s_at" "$_s_n" && return 0
    # More positional words than the parser takes (an unknown sub-verb among
    # them): the line is refused already, so nothing is offered.
    if [ "$_s_n" -gt "$_session_np" ] && [ "$_session_rep" != 1 ]; then
        return 0
    fi
    if [ "$_s_dd" = 0 ]; then
        case $_session_cur in -?*=*) return 0 ;; esac
    fi
    if [ "$_s_n" = 0 ]; then
        _session_subs "$_s_at"
        _session_cands=$_session_r
    fi
    _session_choices "$_s_at" "$_s_n"
    _session_cands="$_session_cands $_session_r"
    if [ "$_s_dd" = 0 ]; then
        _session_flags "$_s_at"
        _session_cands="$_session_cands $_session_r"
    fi
    _session_kind=words
}

# True when, at path $1 after $2 positional words, everything that follows is
# passed through verbatim (argparse REMAINDER): no word, flag or file is offered.
_session_stopped() {
    _session_pos "$1"
    [ -n "$_session_stopat" ] && [ "$2" -ge "$_session_stopat" ]
}

# The choices for the positional word at index $2 (0-based) at path $1.
_session_choices() {
    local _s_i=$2
    _session_pos "$1"
    if [ "$_s_i" -ge "$_session_np" ]; then
        if [ "$_session_rep" != 1 ]; then
            _session_r=
            return 0
        fi
        _s_i=$((_session_np - 1))
    fi
    _session_choice "$1" "$_s_i"
}'''

_BASH = '''# bash completion for `session`, generated by `session completion bash` from
# session's own verbs and options. Regenerate it after upgrading sessiontools.
# Works in bash 3.2 and later, with or without the bash-completion package.

{tables}

{engine}

_session() {{
    local IFS=$' \\t\\n' _s_cur=${{COMP_WORDS[COMP_CWORD]}} _s_c {locals}
    COMPREPLY=()
    _session_engine "$_s_cur" "${{COMP_WORDS[@]:1:COMP_CWORD-1}}"
    # bash splits --flag=value at the "=": a bare "=" is an empty value.
    [ "$_s_cur" = "=" ] && _s_cur=
    # compgen's lines are split by IFS, one candidate each: bash 3.2 has no mapfile.
    # shellcheck disable=SC2207
    case $_session_kind in
        words)
            for _s_c in $_session_cands; do
                case $_s_c in "$_s_cur"*) COMPREPLY[${{#COMPREPLY[@]}}]=$_s_c ;; esac
            done ;;
        dir) IFS=$'\\n'; COMPREPLY=($(compgen -d -- "$_s_cur")) ;;
        file) IFS=$'\\n'; COMPREPLY=($(compgen -f -- "$_s_cur")) ;;
    esac
}}

complete -o filenames -F _session session
'''

_ZSH = '''# zsh completion for `session`, generated by `session completion zsh` from
# session's own verbs and options. Regenerate it after upgrading sessiontools.
# Load it after compinit; without compdef it defines nothing.

if (( $+functions[compdef] )); then

{tables}

{engine}

_session() {{
    # No `emulate` here: the completion system has set its own options, and
    # _files needs them (extendedglob among them).
    local {locals}
    _session_engine "${{(Q)words[CURRENT]}}" "${{(@Q)words[2,CURRENT-1]}}"
    case $_session_kind in
        words) compadd -- ${{=_session_cands}} ;;
        dir) _files -/ ;;
        file) _files ;;
    esac
}}

compdef _session session

else
    print -u2 "session completion: compdef is not defined, so nothing was loaded;" \\
        "load this after compinit (autoload -Uz compinit && compinit)"
fi
'''

#: The engine's variables, local to each TAB: nothing it sets stays in the
#: user's shell (both shells scope `local` dynamically, into the engine).
_LOCALS = "_session_cur _session_kind _session_cands _session_r _session_np _session_rep _session_stopat"

#: Each shell `session completion` prints a script for, and its template.
SHELLS = {"bash": _BASH, "zsh": _ZSH}


def script(shell: str) -> str:
    """The completion script for SHELL ("bash" or "zsh"), from `tree()`.
    Building it builds every verb's parser, so it imports account's and
    start's packages, which load PyYAML: a broken install fails here with
    an ImportError, before a word of the script exists."""
    return SHELLS[shell].format(tables=_tables(tree()), engine=_ENGINE, locals=_LOCALS)
