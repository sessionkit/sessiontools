"""Completion cannot drift from the dispatcher.

The candidates are taken by RUNNING the script `session completion bash`
prints, in a real bash with PATH empty at TAB. They are never read from
`completion.tree()`. The oracle walks the dispatcher's own tables
(`cli.HANDLERS`, `cli.PARSERS`, each builder's parser and each `Group`)
on its own, so a verb, sub-verb or flag that the dispatcher gains fails
here until the script offers it.

The checks:

1. `session <TAB>` offers exactly the HANDLERS verbs and `--help`; `mail`
   and the DISABLED verbs are never offered.
2. Every sub-verb and every long flag a parser accepts is offered at its
   path.
3. Nothing is offered that the dispatcher refuses. Each word offered at a
   path is probed with `session PATH WORD --help`, which must exit 0;
   each flag offered must appear in its path's own `--help` output.
4. `cli.PARSERS` and `cli.HANDLERS` hold the same verbs.
5. The sub-verbs named in the request, literally.

"Refuses" here is the parser's refusal: an unknown verb, sub-verb, flag or
choice. Constraints between flags that a handler checks after parsing
(`ledger --width` without `--short`, two members of a mutually exclusive
group) are not completion's to encode, and are not checked. A positional
completed from a verb's own table (`sound EVENT`, `settings TOOL`,
`get PROP`) is checked against that table by identity in
`tests/test_completion_tree.py`, since argparse does not enforce it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from sessiontools import cli, completion, verbs

BASH = shutil.which("bash") or ("/bin/bash" if os.path.exists("/bin/bash") else None)
pytestmark = pytest.mark.skipif(BASH is None, reason="no bash here")

#: The real `subprocess.run`, held before any probe's guard replaces it.
_RUN = subprocess.run

#: Printed after each query's candidates, one per line.
END = "--end-of-candidates--"


def complete(script: Path, queries: list[tuple[str, ...]]) -> list[list[str]]:
    """Run SCRIPT's `_session` once per query, in one bash. A query is the
    words after `session`, the last being the one completed. PATH is empty
    at TAB, and anything on stderr fails the run."""
    lines = ['source "$1"', "PATH="]
    for q in queries:
        words = " ".join(shlex.quote(w) for w in ("session", *q))
        lines.append(f"COMP_WORDS=({words}); COMP_CWORD={len(q)}; _session; "
                     f"printf '%s\\n' \"${{COMPREPLY[@]}}\" {END}")
    r = _RUN([BASH, "--norc", "--noprofile", "-c", "\n".join(lines), "_", str(script)],
                       capture_output=True, text=True, timeout=120, check=False)
    assert (r.returncode, r.stderr) == (0, ""), r.stderr
    out, cur = [], []
    for line in r.stdout.splitlines():
        if line == END:
            out.append(cur)
            cur = []
        elif line:
            cur.append(line)
    assert len(out) == len(queries)
    return out


@pytest.fixture(scope="module")
def script(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("drift") / "session.bash"
    p.write_text(completion.script("bash"))
    return p


# --- the oracle: the dispatcher's own tables, walked without completion.py ---------------------

def _accepted(parser: argparse.ArgumentParser, path: tuple[str, ...]):
    """(path, sub-verbs, long flags) for PARSER and each subparser under it."""
    subs, longs, nested = set(), set(), []
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            subs |= set(a.choices)
            nested += [((*path, s), sp) for s, sp in a.choices.items()]
        longs |= {o for o in a.option_strings if o.startswith("--")}
    yield path, subs, longs
    for p, sp in nested:
        yield from _accepted(sp, p)


def accepted() -> dict[tuple[str, ...], tuple[set[str], set[str]]]:
    """Each path the dispatcher runs: the sub-verbs and long flags it accepts."""
    out: dict[tuple[str, ...], tuple[set[str], set[str]]] = {}

    def add(rows):
        for path, subs, longs in rows:
            s, f = out.setdefault(path, (set(), set()))
            s |= subs
            f |= longs

    for verb in cli.HANDLERS:
        src = verbs.resolve(cli.PARSERS[verb])
        if not isinstance(src, verbs.Group):
            add(_accepted(src(), (verb,)))
            continue
        if src.rest is not None:
            # A Group sub is dispatched by hand before the rest parser sees
            # it, so it replaces a placeholder subparser of the same name.
            add(row for row in _accepted(verbs.resolve(src.rest)(), (verb,))
                if row[0][1:2] not in [(s,) for s in src.subs])
        add([((verb,), set(src.subs), set())])
        for sub, ref in src.subs.items():
            add(_accepted(verbs.resolve(ref)(), (verb, sub)))
    return out


# --- 1. the verbs ------------------------------------------------------------------------------

def missing_verbs(script: Path) -> set[str]:
    [got] = complete(script, [("",)])
    return set(cli.HANDLERS) - set(got)


def unknown_verbs(script: Path) -> set[str]:
    [got] = complete(script, [("",)])
    return set(got) - set(cli.HANDLERS) - {"--help"}


def test_every_verb_and_nothing_else(script):
    assert missing_verbs(script) == set()
    assert unknown_verbs(script) == set()


def test_mail_and_the_disabled_verbs_are_never_offered(script):
    [top, ma, *disabled] = complete(script, [("",), ("ma",), *((v,) for v in cli.DISABLED)])
    assert "mail" not in top and ma == []
    assert not set(cli.DISABLED) & set(top)
    assert disabled == [[] for _ in cli.DISABLED]


# --- 2. every sub-verb and long flag a parser accepts ------------------------------------------

def missing_words(script: Path) -> dict[tuple[str, ...], set[str]]:
    """Path -> the sub-verbs and long flags the parser there accepts and the
    script does not offer there."""
    table = accepted()
    paths = list(table)
    got = complete(script, [(*p, "") for p in paths])
    return {p: gap for p, words in zip(paths, got)
            if (gap := (table[p][0] | table[p][1]) - set(words))}


def test_every_sub_verb_and_flag_a_parser_accepts_is_offered(script):
    assert missing_words(script) == {}


def test_the_oracle_reaches_the_hand_dispatched_sub_verbs():
    table = accepted()
    assert {"engine"} <= table[("sound",)][0]
    assert {"build", "on", "off", "status"} <= table[("sound", "engine")][0]
    assert {"agent"} <= table[("pane-state",)][0]
    assert {"on", "off", "status"} <= table[("pane-state", "agent")][0]
    assert {"install", "check", "sample"} <= table[("statusline",)][0]


# --- 3. nothing the dispatcher refuses ---------------------------------------------------------

def _refuse(*_a, **_k):
    raise AssertionError("a --help probe tried to start a process")


def _run(argv: list[str]) -> tuple[int, str]:
    """`session ARGV`, in process: its exit code (a returned code, or
    argparse's SystemExit) and its stdout. Every probe is --help, so no verb
    runs; should one try, it cannot start a process (every `subprocess`
    call goes through Popen)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
            mock.patch.object(subprocess, "Popen", _refuse), mock.patch.object(os, "system", _refuse):
        try:
            code = cli.main(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return code, out.getvalue()


def offered(script: Path, depth: int = 4) -> dict[tuple[str, ...], list[str]]:
    """Path -> the words the script offers there, walked from `session` by
    the script alone: every non-flag word it offers is followed, as far as
    DEPTH words."""
    seen: dict[tuple[str, ...], list[str]] = {}
    frontier: list[tuple[str, ...]] = [()]
    while frontier:
        got = complete(script, [(*p, "") for p in frontier])
        nxt = []
        for p, words in zip(frontier, got):
            seen[p] = words
            if len(p) < depth:
                nxt += [(*p, w) for w in words if not w.startswith("-") and (*p, w) not in seen]
        frontier = nxt
    return seen


def refused(script: Path) -> list[str]:
    """Each word the script offers that the dispatcher refuses there."""
    bad = []
    for path, words in offered(script).items():
        code, help_text = _run([*path, "--help"])
        if code != 0:
            bad.append(f"session {' '.join(path)} --help exits {code}")
            continue
        for w in words:
            if w.startswith("--"):
                if w != "--help" and not re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", help_text):
                    bad.append(f"{w} is offered at `session {' '.join(path)}` and its --help "
                               "does not name it")
            elif _run([*path, w, "--help"])[0] != 0:
                bad.append(f"session {' '.join((*path, w))} --help does not exit 0")
    return bad


@pytest.fixture
def no_side_effects(tmp_path, monkeypatch):
    """Should a --help probe run a verb, it finds an empty HOME and cwd."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


def test_nothing_offered_is_refused(script, no_side_effects):
    assert refused(script) == []


def test_the_walk_covers_every_verb_and_the_named_sub_verbs(script):
    paths = set(offered(script))
    assert {(v,) for v in cli.HANDLERS} <= paths
    assert {("sound", "engine", "build"), ("pane-state", "agent", "on"),
            ("account", "use"), ("statusline", "sample")} <= paths


def test_the_help_probe_reads_both_exit_paths():
    """`session list --help` raises argparse's SystemExit(0); `session
    account --help` returns 0. An unknown word is 2 either way."""
    assert _run(["list", "--help"])[0] == 0
    assert _run(["account", "--help"])[0] == 0
    assert _run(["bogus", "--help"])[0] == 2
    assert _run(["completion", "fish", "--help"])[0] == 2


# --- 4. one table each ---------------------------------------------------------------------------

def test_parsers_and_handlers_hold_the_same_verbs():
    assert set(cli.PARSERS) == set(cli.HANDLERS)


# --- 5. the named sub-verbs, literally -----------------------------------------------------------

@pytest.mark.parametrize("path,subs", [
    (("sound", "engine"), ["build", "on", "off", "status"]),
    (("account",), ["list", "status", "use"]),
    (("pane-state",), ["show", "watch", "agent"]),
    (("default-model",), ["show", "list", "enforce", "watch", "unit"]),
    (("agent",), ["default"]),
])
def test_the_named_sub_verbs(script, path, subs):
    [got] = complete(script, [(*path, "")])
    assert set(subs) <= set(got)


# --- the checks catch drift ------------------------------------------------------------------------

def _mutated(script: Path, tmp_path: Path, old: str, new: str) -> Path:
    text = script.read_text()
    assert old in text
    p = tmp_path / "mutated.bash"
    p.write_text(text.replace(old, new, 1))
    return p


def test_a_dropped_verb_is_caught(script, tmp_path):
    p = _mutated(script, tmp_path, " park path completion'", " path completion'")
    assert missing_verbs(p) == {"park"}


def test_a_dropped_sub_verb_is_caught(script, tmp_path):
    p = _mutated(script, tmp_path, "'session sound engine') _session_r='build on off status'",
                 "'session sound engine') _session_r='build on off'")
    gaps = missing_words(p)
    assert gaps[("sound", "engine")] == {"status"}


def test_an_offered_verb_the_dispatcher_refuses_is_caught(script, tmp_path, no_side_effects):
    p = _mutated(script, tmp_path, " park path completion'", " park path completion mail'")
    assert unknown_verbs(p) == {"mail"}
    assert refused(p) == ["session mail --help does not exit 0", "session mail --help exits 2"]


def test_an_offered_flag_the_dispatcher_refuses_is_caught(script, tmp_path, no_side_effects):
    p = _mutated(script, tmp_path, "'session list') _session_r='--help",
                 "'session list') _session_r='--bogus --help")
    assert refused(p) == ["--bogus is offered at `session list` and its --help does not name it"]
