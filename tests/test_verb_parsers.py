"""Each `session` verb's parser has one source: `cli.PARSERS` names a pure
builder (or a `verbs.Group` for hand-dispatched sub-verbs), and the verb's
handler parses with exactly the parser that builder returns.

Anything that reads a verb's surface without running the verb (shell
completion) reads it through `PARSERS`, so these tests are what keep that
reading true to the dispatcher."""

from __future__ import annotations

import argparse
import subprocess
import sys

import pytest

from sessiontools import cli, statusline, verbs

#: The verbs whose builders load PyYAML where they are built: account's
#: registry and start's package both reach `start.agentfile`.
YAML_BUILDERS = {"account", "start"}


def sources() -> list[tuple[tuple[str, ...], str]]:
    """Every (path, reference) a verb's parsers come from: the verb's own
    builder, or its Group's REST parser at the verb and each SUBS builder at
    `verb sub`."""
    out = []
    for verb, ref in cli.PARSERS.items():
        src = verbs.resolve(ref)
        if isinstance(src, verbs.Group):
            if src.rest is not None:
                out.append(((verb,), src.rest))
            out.extend(((verb, sub), sub_ref) for sub, sub_ref in src.subs.items())
        else:
            out.append(((verb,), ref))
    return out


def shape(p: argparse.ArgumentParser) -> tuple:
    """What a parser accepts, comparable across two builds: its prog, each
    action's option strings, dest and nargs, and each subparser's shape."""
    rows, subs = [], {}
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            subs = {name: shape(sp) for name, sp in a.choices.items()}
            continue
        rows.append((tuple(a.option_strings), a.dest, str(a.nargs)))
    return (p.prog, tuple(sorted(rows)), tuple(sorted(subs.items())))


def test_parsers_names_exactly_the_handlers_verbs():
    assert set(cli.PARSERS) == set(cli.HANDLERS)


def test_parsers_offers_nothing_main_refuses():
    assert "mail" not in cli.PARSERS
    for verb in cli.DISABLED:
        assert verb not in cli.PARSERS


@pytest.mark.parametrize("path,ref", sources(), ids=lambda v: " ".join(v) if isinstance(v, tuple) else v)
def test_every_reference_builds_a_parser(path, ref):
    built = verbs.resolve(ref)()
    assert isinstance(built, argparse.ArgumentParser), (path, ref)


def test_a_bad_reference_is_refused_by_name():
    with pytest.raises(ValueError, match="module:attr"):
        verbs.resolve("sessiontools.cli")


def test_statusline_group_is_its_subverbs_table():
    assert list(statusline.GROUP.subs) == [name for name, _ in statusline.SUBVERBS]
    assert statusline.GROUP.rest is None


def test_hand_dispatched_groups():
    """The card's hand-dispatched sub-verbs are each a Group sub, so the
    dispatcher and any reader of PARSERS see the same word."""
    assert set(verbs.resolve(cli.PARSERS["sound"]).subs) == {"engine"}
    assert set(verbs.resolve(cli.PARSERS["pane-state"]).subs) == {"agent"}
    assert set(verbs.resolve(cli.PARSERS["statusline"]).subs) == {"install", "check", "sample"}
    assert set(verbs.resolve(cli.PARSERS["path"]).subs) == {"migrate"}


class _Captured(BaseException):
    """Raised from a patched `parse_args`, carrying the parser. A
    BaseException, so a handler's `except Exception` (park's) or
    `except SystemExit` (account's, start's) cannot swallow it."""

    def __init__(self, parser: argparse.ArgumentParser):
        super().__init__(parser.prog)
        self.parser = parser


def _no_process(*_a, **_k):
    raise AssertionError("a process was started before the verb parsed its arguments")


@pytest.mark.parametrize("path,ref", sources(), ids=lambda v: " ".join(v) if isinstance(v, tuple) else v)
def test_each_handler_parses_with_its_registered_parser(path, ref, monkeypatch):
    """The interception oracle: run the real dispatcher on PATH with
    `parse_args` patched to raise, so the verb never runs, and compare the
    parser it was about to use with the one its PARSERS entry builds. Nothing
    may start a process before that point."""
    expected = shape(verbs.resolve(ref)())

    def capture(self, *_a, **_k):
        raise _Captured(self)

    # A context, not the test-wide patch: the suite's autouse teardown runs
    # tmux through subprocess, after this test, and must not trip the guard.
    with monkeypatch.context() as m, pytest.raises(_Captured) as got:
        m.setattr(argparse.ArgumentParser, "parse_args", capture)
        m.setattr(subprocess, "Popen", _no_process)
        m.setattr(subprocess, "run", _no_process)
        cli.main(list(path))
    assert shape(got.value.parser) == expected


def test_building_a_parser_starts_no_process(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(subprocess, "Popen", _no_process)
        m.setattr(subprocess, "run", _no_process)
        for _path, ref in sources():
            verbs.resolve(ref)()


def _probe(code: str) -> str:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          check=True).stdout.strip()


def test_import_cli_imports_no_builder_module():
    """PARSERS holds strings: importing the dispatcher imports none of the
    modules it names, other than itself."""
    modules = sorted({ref.partition(":")[0] for ref in cli.PARSERS.values()} - {"sessiontools.cli"})
    loaded = _probe("import sys, sessiontools.cli; "
                    f"print([m for m in {modules!r} if m in sys.modules])")
    assert loaded == "[]"


def test_every_builder_but_account_and_start_loads_no_yaml():
    """Building the parsers of every verb except account and start loads
    neither PyYAML nor jsonschema; those two load PyYAML only in the process
    that builds them."""
    code = ("import sys; from sessiontools import cli, verbs\n"
            f"skip = {sorted(YAML_BUILDERS)!r}\n"
            "for verb, ref in cli.PARSERS.items():\n"
            "    if verb in skip: continue\n"
            "    src = verbs.resolve(ref)\n"
            "    refs = ([src.rest] if src.rest else []) + list(src.subs.values()) "
            "if isinstance(src, verbs.Group) else [ref]\n"
            "    for r in refs: verbs.resolve(r)()\n"
            "print('yaml' in sys.modules, 'jsonschema' in sys.modules)")
    assert _probe(code) == "False False"
