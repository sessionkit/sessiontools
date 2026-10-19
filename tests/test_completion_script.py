"""`session completion bash|zsh`: the scripts, run in the real shells.

Every behaviour test calls the script's completion function with PATH empty,
so a script that ran any command at TAB would fail with "command not found"
on stderr, and stderr must be empty. A shell that is missing skips its tests
by name, never passes them silently."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools import busy, cli, completion, get, settings, verbs


def _version(shell: str) -> str:
    try:
        return subprocess.run([shell, "-c", "echo $BASH_VERSION"], capture_output=True,
                              text=True, timeout=10, check=False).stdout.strip()
    except OSError:
        return ""


def _bash(major: tuple[int, ...]) -> str | None:
    """The first bash found whose major version is in MAJOR."""
    seen = [shutil.which("bash"), "/bin/bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash"]
    for b in dict.fromkeys(s for s in seen if s and os.path.exists(s)):
        v = _version(b)
        if v and int(v.split(".")[0]) in major:
            return b
    return None


BASH32 = "/bin/bash" if _version("/bin/bash").startswith("3.2") else None
BASH5 = _bash((5,))
ZSH = shutil.which("zsh")

BASHES = [pytest.param(BASH32, id="bash3.2",
                       marks=pytest.mark.skipif(BASH32 is None, reason="no /bin/bash 3.2 here")),
          pytest.param(BASH5, id="bash5",
                       marks=pytest.mark.skipif(BASH5 is None, reason="no bash 5 here"))]
needs_zsh = pytest.mark.skipif(ZSH is None, reason="no zsh here")


@pytest.fixture(scope="module")
def scripts(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("scripts")
    out = {}
    for shell in completion.SHELLS:
        out[shell] = d / f"session.{shell}"
        out[shell].write_text(completion.script(shell))
    return out


@pytest.fixture(scope="module")
def cwd(tmp_path_factory) -> Path:
    """A directory with two directories and a file in it, for DIR values."""
    d = tmp_path_factory.mktemp("cwd")
    (d / "adir").mkdir()
    (d / "bdir").mkdir()
    (d / "afile").write_text("")
    return d


# COMP_WORDS as bash fills it: the words up to and including the one being
# completed; COMP_CWORD is its index. PATH is emptied just before the call.
BASH_DRIVER = ('source "$1"; shift; COMP_WORDS=("$@"); COMP_CWORD=$(($# - 1)); PATH=; '
               '_session; printf "%s\\n" "${COMPREPLY[@]}"')

# The real `_session` adapter, with compadd and _files stubbed to print what
# they were given: outside a completion widget there is nothing to add to.
ZSH_DRIVER = ('autoload -Uz compinit; compinit -u -D; source "$1"; shift; '
              'compadd() { [[ $1 == -- ]] && shift; print -rl -- "$@"; }; '
              '_files() { print -r -- "<files $*>"; }; '
              'words=("$@"); CURRENT=$#; PATH=; _session')


def _run(argv: list[str], cwd: Path, home: Path) -> list[str]:
    env = {"HOME": str(home), "ZDOTDIR": str(home), "PATH": os.environ.get("PATH", "")}
    r = subprocess.run(argv, capture_output=True, text=True, cwd=cwd, env=env, timeout=30,
                       check=False)
    assert r.returncode == 0, r.stderr
    assert r.stderr == "", f"the script wrote to stderr (a command run at TAB?): {r.stderr}"
    return [line for line in r.stdout.splitlines() if line]


def bash_complete(bash: str, script: Path, cwd: Path, home: Path, *words: str) -> list[str]:
    return _run([bash, "--norc", "--noprofile", "-c", BASH_DRIVER, "_", str(script),
                 "session", *words], cwd, home)


def zsh_complete(script: Path, cwd: Path, home: Path, *words: str) -> list[str]:
    return _run([ZSH, "-f", "-c", ZSH_DRIVER, "_", str(script), "session", *words], cwd, home)


def _node(*path: str) -> completion.Node:
    n = completion.tree()
    for w in path:
        n = n.subs[w]
    return n


def _flags(*path: str) -> list[str]:
    return [f.name for f in _node(*path).flags]


#: (words typed after `session`, the last one being completed) -> candidates.
#: Expectations are read from the dispatcher's own tables where they can be.
CASES = [
    (("",), [*cli.HANDLERS, "--help"]),
    (("so",), ["sound"]),
    (("ma",), []),  # `mail` is retired: never offered
    (("sound", ""), ["engine", *busy.EVENTS, *_flags("sound")]),
    (("sound", "engine", ""), ["build", "on", "off", "status", "--help"]),
    (("sound", "engine", "b"), ["build"]),
    (("sound", "--session", "X", ""), ["engine", *busy.EVENTS, *_flags("sound")]),
    (("sound", "landed", "--"), _flags("sound")),
    (("account", ""), [*_node("account").subs, "--help"]),
    (("pane-state", ""), ["show", "watch", "agent", "--help"]),
    (("pane-state", "agent", ""), ["on", "off", "status", "--help"]),
    (("statusline", ""), ["install", "check", "sample", "--help"]),
    (("settings", "SID", ""), [*settings.TOOLS, *_flags("settings")]),
    (("settings", "--json", "SID", "m"), [t for t in settings.TOOLS if t.startswith("m")]),
    (("settings", "--", "SID", ""), list(settings.TOOLS)),  # after --, no flags
    (("get", ""), [name for name, _ in get.PROPS] + _flags("get")),
    (("get", "id", ""), _flags("get")),  # SEAT: free-form
    (("completion", ""), ["bash", "zsh", "--help"]),
    (("list", "--pane", ""), []),  # a value: nothing to offer
    (("list", "--pane=x"), []),  # a flag with its value is complete
    (("ledger", "--width", ""), []),
    (("ledger", "--sh"), ["--short"]),
    (("reap", "a", "b", "--"), _flags("reap")),
    (("start", "--dry-run", ""), _flags("start")),  # no agent names in this version
    (("start", "agent", ""), []),  # REMAINDER: the vendor's words
    (("start", "agent", "--"), []),  # ...and never a launcher flag there
    (("start", "agent", "--dry-run", ""), []),
    (("bogus", ""), []),  # an unknown verb: the dispatcher refuses the line
    (("bogus", "--"), []),
    (("list", "extra", "--"), []),  # list takes no positional
    (("pane-state", "bogus", ""), []),
    (("get", "id", "seat", ""), _flags("get")),  # both positionals given: flags only
    (("get", "id", "seat", "x", ""), []),  # one too many
]


@pytest.mark.parametrize("bash", BASHES)
@pytest.mark.parametrize("words,expected", CASES, ids=lambda v: " ".join(v) if isinstance(v, tuple) else None)
def test_bash_completes(bash, scripts, cwd, tmp_path, words, expected):
    assert bash_complete(bash, scripts["bash"], cwd, tmp_path, *words) == expected


@needs_zsh
@pytest.mark.parametrize("words,expected", CASES, ids=lambda v: " ".join(v) if isinstance(v, tuple) else None)
def test_zsh_offers_the_same_candidates(scripts, cwd, tmp_path, words, expected):
    """zsh's compadd filters by the typed prefix itself, so the adapter hands
    it every candidate at that position; filtered here, they are bash's."""
    got = zsh_complete(scripts["zsh"], cwd, tmp_path, *words)
    assert [w for w in got if w.startswith(words[-1])] == expected


@pytest.mark.parametrize("bash", BASHES)
def test_bash_completes_directories_for_a_dir_value(bash, scripts, cwd, tmp_path):
    assert sorted(bash_complete(bash, scripts["bash"], cwd, tmp_path,
                                "start", "--directory", "")) == ["adir", "bdir"]
    assert bash_complete(bash, scripts["bash"], cwd, tmp_path,
                         "start", "--directory", "a") == ["adir"]
    # bash splits `--dest=` at the "=": the value is completed after it.
    assert sorted(bash_complete(bash, scripts["bash"], cwd, tmp_path,
                                "sound", "engine", "build", "--dest", "=", "")) == ["adir", "bdir"]


@needs_zsh
def test_zsh_completes_directories_for_a_dir_value(scripts, cwd, tmp_path):
    assert zsh_complete(scripts["zsh"], cwd, tmp_path, "start", "--directory", "") == ["<files -/>"]


@pytest.mark.parametrize("bash", BASHES)
def test_bash_registers_the_function(bash, scripts, tmp_path):
    out = _run([bash, "--norc", "--noprofile", "-c", 'source "$1"; complete -p session',
                "_", str(scripts["bash"])], tmp_path, tmp_path)
    assert out == ["complete -o filenames -F _session session"]


@needs_zsh
def test_zsh_syntax(scripts):
    r = subprocess.run([ZSH, "-n", str(scripts["zsh"])], capture_output=True, text=True, timeout=30,
                       check=False)
    assert (r.returncode, r.stderr) == (0, "")


@needs_zsh
def test_zsh_registers_with_compdef_after_compinit(scripts, tmp_path):
    code = 'autoload -Uz compinit; compinit -u -D; source "$1"; print -r -- ${_comps[session]}'
    out = _run([ZSH, "-f", "-c", code, "_", str(scripts["zsh"])], tmp_path, tmp_path)
    assert out == ["_session"]


@needs_zsh
def test_zsh_without_compinit_says_so_and_defines_nothing(scripts, tmp_path):
    code = 'source "$1"; print -r -- $? $+functions[_session] $+functions[_session_engine]'
    r = subprocess.run([ZSH, "-f", "-c", code, "_", str(scripts["zsh"])],
                       capture_output=True, text=True, cwd=tmp_path, timeout=30, check=False,
                       env={"HOME": str(tmp_path), "ZDOTDIR": str(tmp_path)})
    assert r.stdout.split() == ["0", "0", "0"]
    assert "compinit" in r.stderr and "nothing was loaded" in r.stderr


def _zpty_tab(script: Path, cwd: Path, home: Path, line: str, want: str) -> str:
    """Type LINE and a TAB into a real interactive zsh (zsh/zpty), and read
    the terminal until WANT appears or ten seconds pass."""
    drive = r'''
zmodload zsh/zpty || exit 3
zpty z 'PS1="> " zsh -f -i'
zpty -w z "autoload -Uz compinit; compinit -u -D; source ${(q)1}; cd ${(q)2}"
zpty -w -n z "$3"$'\t'
out=
for i in {1..100}; do
    zpty -r -t z chunk && out+=$chunk
    [[ $out == *"$4"* ]] && break
    sleep 0.1
done
zpty -d z
print -r -- "$out"
'''
    r = subprocess.run([ZSH, "-f", "-c", drive, "_", str(script), str(cwd), line, want],
                       capture_output=True, text=True, timeout=60, check=False,
                       env={"HOME": str(home), "ZDOTDIR": str(home),
                            "PATH": os.environ.get("PATH", ""), "TERM": "xterm"})
    if r.returncode == 3:
        pytest.skip("zsh has no zsh/zpty module here")
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", r.stdout)


@needs_zsh
def test_a_real_zsh_tab_completes_sub_verbs(scripts, cwd, tmp_path):
    out = _zpty_tab(scripts["zsh"], cwd, tmp_path, "session sound engine ", "status")
    for word in ("build", "on", "off", "status"):
        assert word in out


@needs_zsh
def test_a_real_zsh_tab_completes_a_directory(scripts, cwd, tmp_path):
    """Inside a real completion widget: _files needs the options the
    completion system sets, so the adapter must not reset them."""
    out = _zpty_tab(scripts["zsh"], cwd, tmp_path, "session start --directory a", "adir/")
    assert "session start --directory adir/" in out
    assert "bad pattern" not in out


# --- the verb ---------------------------------------------------------------------------------

@pytest.mark.parametrize("shell", list(completion.SHELLS))
def test_the_verb_prints_the_script(shell, capsys):
    assert cli.main(["completion", shell]) == 0
    out = capsys.readouterr()
    assert out.out == completion.script(shell)
    assert out.err == ""


def test_an_unknown_shell_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["completion", "fish"])
    assert e.value.code == 2
    assert "invalid choice: 'fish'" in capsys.readouterr().err


def test_the_shells_are_the_parsers_choices():
    shell = next(a for a in cli._completion_parser()._actions if a.dest == "shell")
    assert tuple(shell.choices) == tuple(completion.SHELLS) == ("bash", "zsh")


def test_completion_is_a_verb_the_tree_offers():
    assert completion.tree().subs["completion"].choices_at(0) == ("bash", "zsh")


def test_generating_runs_no_side_effect(tmp_path, monkeypatch, capsys):
    """No process, no socket, nothing written under HOME or the working
    directory: generating the script builds parsers and nothing else."""
    def refuse(*_a, **_k):
        raise AssertionError("generating the script did more than build parsers")

    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    with monkeypatch.context() as m:
        m.setattr(subprocess, "Popen", refuse)
        m.setattr(subprocess, "run", refuse)
        m.setattr(os, "system", refuse)
        m.setattr(socket, "socket", refuse)
        for shell in completion.SHELLS:
            assert cli.main(["completion", shell]) == 0
    assert capsys.readouterr().out
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


def test_a_broken_install_prints_no_script_and_exits_1():
    """PyYAML missing: account's and start's builders cannot be built, so no
    script at all (one that silently lacked them would be drift), exit 1."""
    probe = ("import sys; sys.modules['yaml'] = None\n"
             "from sessiontools import cli\n"
             "sys.exit(cli.main(['completion', 'bash']))")
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                       check=False)
    assert r.returncode == 1
    assert r.stdout == ""
    assert "broken install - PyYAML will not import" in r.stderr
    assert "no script was written" in r.stderr


def test_a_word_a_script_cannot_hold_is_refused(monkeypatch):
    built = cli._completion_parser

    def odd():
        p = built()
        p.add_argument("extra", choices=("two words",))
        return p

    monkeypatch.setattr(cli, "_completion_parser", odd)
    with pytest.raises(ValueError, match="'two words' cannot go into a completion script"):
        completion.script("bash")


def test_no_short_option_takes_a_value():
    """The engine knows only long flags; a short one that took a value would
    have its value counted as a positional word."""
    def walk(p):
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for sp in a.choices.values():
                    yield from walk(sp)
            elif a.nargs != 0:
                yield from (o for o in a.option_strings if not o.startswith("--"))

    shorts = []
    for ref in cli.PARSERS.values():
        src = verbs.resolve(ref)
        refs = ([src.rest] if src.rest else []) + list(src.subs.values()) \
            if isinstance(src, verbs.Group) else [ref]
        for r in refs:
            shorts += walk(verbs.resolve(r)())
    assert shorts == []



# --- nothing stays behind in the user's shell ------------------------------------------------

def test_every_engine_variable_is_local_to_the_tab(scripts):
    """Each `_session_X=` the scripts assign is in the adapters' `local`."""
    for shell, path in scripts.items():
        assigned = set(re.findall(r"\b(_session_[a-z]+)=", path.read_text()))
        assert assigned == set(completion._LOCALS.split()), shell


@pytest.mark.parametrize("bash", BASHES)
def test_bash_leaves_no_variable_set_after_a_tab(bash, scripts, cwd, tmp_path):
    code = ('source "$1"; COMP_WORDS=(session sound ""); COMP_CWORD=2; PATH=; _session; '
            'set | while IFS= read -r line; do case $line in _session_*=*) echo "$line" ;; esac; done')
    assert _run([bash, "--norc", "--noprofile", "-c", code, "_", str(scripts["bash"])],
                cwd, tmp_path) == []


@needs_zsh
def test_zsh_leaves_no_variable_set_after_a_tab(scripts, cwd, tmp_path):
    code = ('autoload -Uz compinit; compinit -u -D; source "$1"; '
            'compadd() { :; }; words=(session sound ""); CURRENT=3; PATH=; _session; '
            'print -rl -- ${(k)parameters[(I)_session_*]}')
    assert _run([ZSH, "-f", "-c", code, "_", str(scripts["zsh"])], cwd, tmp_path) == []
