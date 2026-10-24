"""The `session` command is sessiontools' now, and that is the test.

Since the 2030-06-09 cutover sessiontools provides `session`, same verbs,
ported as package code. This replaces
the 2030-06-09 pin that no console script was declared."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import sessiontools
from sessiontools import cli, start

ROOT = Path(__file__).resolve().parents[1]

# .gitattributes export-ignores /CLAUDE.md, so a release archive (`git archive`,
# the public repo built from one) has no CLAUDE.md. There the tests of its shape
# skip; in a checkout, where it exists, they still assert. `lexists`, not
# `exists`: a dangling CLAUDE.md symlink is still a CLAUDE.md, and must fail.
needs_claude_md = pytest.mark.skipif(
    not os.path.lexists(ROOT / "CLAUDE.md"),
    reason="no CLAUDE.md: .gitattributes export-ignores it, so a release archive leaves it out",
)


def test_the_session_command_is_declared_and_is_the_only_one():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["scripts"] == {"session": "sessiontools.cli:main"}
    assert "gui-scripts" not in project


def runtime_dependencies() -> list[str]:
    """The names in pyproject.toml's `[project] dependencies`, lowercased."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    return [re.split(r"[<>=!~ \[;]", spec, maxsplit=1)[0].lower()
            for spec in project.get("dependencies", [])]


def test_the_runtime_dependencies_are_pyyaml_and_jsonschema():
    """sessionmail depends on this; a stranger installing sessionmail must be
    happy to pull them. PyYAML for agent front matter (`session start`
    pulls in the YAML library for itself alone). jsonschema since
    2030-12-08, for `session settings` (no configuration library, jsonschema
    for the schema check, by design). Both imported lazily; the
    library stays stdlib. Dev tools live in the dev group."""
    assert runtime_dependencies() == ["pyyaml", "jsonschema"]


def test_the_library_and_the_command_import_neither_dependency():
    """...and only the verbs that need them import them: a fresh interpreter
    that imports the library and the CLI has neither yaml nor jsonschema
    loaded."""
    probe = ("import sys; import sessiontools; import sessiontools.cli; "
             "print('yaml' in sys.modules, 'jsonschema' in sys.modules, "
             "'sessiontools.account' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                            text=True, check=True)
    # sessiontools.account reads start.secrets, and importing start loads
    # yaml: the account group is imported by its handler only (ST-22 do-006).
    assert result.stdout.strip() == "False False False"


#: A count of runtime dependencies, written out: "One runtime dependency",
#: "TWO runtime dependencies". "no runtime dependency without his word" is
#: a rule, not a count, so "no" and "zero" are not counted words here.
COUNT_CLAIM = re.compile(
    r"\b(one|two|three|four|five|single)\s+runtime\s+dependenc", re.IGNORECASE)
NUMBER_WORDS = {1: ("one", "single"), 2: ("two",), 3: ("three",), 4: ("four",),
                5: ("five",)}


def current_docs() -> list[Path]:
    """The files that state the package as it is now: AGENTS.md, CLAUDE.md,
    the README, pyproject.toml, docs/ and every Python source. CHANGELOG.md's past
    sections and openspec/ are records of their day and are left alone
    (ST-138 G2). Only the ones present: a release archive has no CLAUDE.md
    (.gitattributes export-ignores it), and a dangling symlink still counts."""
    found = [ROOT / name for name in ("AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml")
             if os.path.lexists(ROOT / name)]
    found += sorted((ROOT / "docs").rglob("*.md"))
    found += python_sources()
    return found


def test_every_dependency_count_in_the_docs_matches_pyproject():
    """ST-138 G2: jsonschema came in at 414bad3 (2030-12-08) and CLAUDE.md,
    cli.py, pyproject.toml and this file went on saying ONE for ten days.
    Any count of runtime dependencies in a current doc must be the number
    pyproject.toml declares."""
    want = NUMBER_WORDS[len(runtime_dependencies())]
    wrong = []
    for path in current_docs():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for m in COUNT_CLAIM.finditer(line):
                if m.group(1).lower() not in want:
                    wrong.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not wrong, "a dependency count that pyproject.toml contradicts:\n" + "\n".join(wrong)


def test_agents_md_names_every_runtime_dependency():
    """AGENTS.md's rule 3 is the one an agent reads first: it names each
    dependency pyproject.toml declares. (It was CLAUDE.md's until ST-138
    do-003 moved the content to AGENTS.md.)"""
    text = (ROOT / "AGENTS.md").read_text().lower()
    assert [name for name in runtime_dependencies() if name not in text] == []


@needs_claude_md
def test_claude_md_is_the_agents_md_import():
    """ST-138 do-003 (SK-36 design): AGENTS.md holds the instructions, and
    CLAUDE.md is a real file whose FIRST line is `@AGENTS.md`. Not a
    symlink: Edit and Write refuse to write through one, and a Windows
    clone gets a one-line text file instead. Claude Code reads AGENTS.md
    on its own only when no CLAUDE.md sits in the cwd or above it, so the
    import is what reaches an agent working in a nested checkout."""
    claude, agents = ROOT / "CLAUDE.md", ROOT / "AGENTS.md"
    assert not claude.is_symlink() and not agents.is_symlink()
    assert claude.read_text().splitlines()[0] == "@AGENTS.md"


def python_sources() -> list[Path]:
    """Every Python file under src/: the *.py modules, and any file whose
    shebang runs Python (`python`, or `uv run --script`)."""
    found = []
    for path in sorted((ROOT / "src").rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix == ".py":
            found.append(path)
            continue
        with path.open("rb") as handle:
            first = handle.readline()
        if first.startswith(b"#!") and (b"python" in first or b"uv run" in first):
            found.append(path)
    return found


def test_only_the_agentfile_module_imports_yaml():
    """One module of the package imports yaml: `start/agentfile.py`. The
    scan covers every Python file under src/, not just *.py, so a script
    without a suffix counts too."""
    offenders = [str(path.relative_to(ROOT)) for path in python_sources()
                 if re.search(r"^\s*(import yaml|from yaml)", path.read_text(), re.MULTILINE)]
    assert offenders == ["src/sessiontools/start/agentfile.py"]


def test_only_the_settings_module_imports_jsonschema():
    """One module of the package imports jsonschema: `settings.py`, inside
    `check_schema`. AGENTS.md rule 3 says this file pins it (ST-138 G2
    review: the yaml half had a pin, the jsonschema half did not)."""
    offenders = [str(path.relative_to(ROOT)) for path in python_sources()
                 if re.search(r"^\s*(import jsonschema|from jsonschema)", path.read_text(),
                              re.MULTILINE)]
    assert offenders == ["src/sessiontools/settings.py"]


def test_the_package_ships_no_old_launcher():
    """`session start` is the port; the old launcher is not in the package,
    as a file or as a path `start` keeps. Carried over from the pins on the
    old launcher's copy when that copy left the release tree (ST-102)."""
    package = Path(sessiontools.__file__).resolve().parent
    assert not [path for path in package.rglob("session-start") if path.is_file()]
    assert not hasattr(start, "SCRIPT")


def test_no_verb_prints_the_verbs(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for verb in ("list", "get", "start"):
        assert f"  {verb} " in out
    assert "send" not in out          # disabled: not even named (2030-12-08)


def test_session_mail_is_retired_and_says_where_each_verb_went(capsys):
    assert cli.main(["mail", "run", "/BOOT/WAKE"]) == 2
    err = capsys.readouterr().err
    assert "memory ls|tree|cat|run PATH" in err
    assert "message send" in err and "pager listen" in err


def test_an_unknown_verb_is_exit_2_with_the_list(capsys):
    assert cli.main(["nope"]) == 2
    err = capsys.readouterr().err
    assert "unknown verb 'nope'" in err and "Verbs:" in err


def test_start_calls_a_failed_import_a_broken_install(capsys, monkeypatch):
    """The port has landed: a failed import is a broken install, exit 1."""
    import sys
    monkeypatch.setitem(sys.modules, "sessiontools.start", None)   # import fails
    assert cli.main(["start", "--dry-run", "bob"]) == 1
    err = capsys.readouterr().err
    assert "session start: broken install" in err and "reinstall sessiontools" in err
    assert "not ported" not in err


def test_session_send_is_disabled_not_runnable(capsys, monkeypatch):
    """By ruling (2030-12-08): not in the help, not runnable; the code
    kept. Nothing may be typed into any pane."""
    from sessiontools import send
    typed = []
    monkeypatch.setattr(send, "send_keys", lambda *a: typed.append(a) or True)
    for argv in (["send", "bob", "-v"], ["send", "--list"], ["send"]):
        assert cli.main(argv) == 2
        assert "disabled" in capsys.readouterr().err
    assert typed == []
    assert "send" not in cli.HANDLERS


def test_the_send_code_is_kept_for_bringing_it_back():
    from sessiontools import send
    assert callable(cli._send) and callable(send.send_keys) and callable(send.still_belongs)


def test_bringing_send_back_takes_both_steps_the_comment_names(monkeypatch, capsys):
    """A review: HANDLERS alone is not enough - DISABLED is checked first."""
    from sessiontools import send
    monkeypatch.setattr(send, "send_keys", lambda *a: True)
    monkeypatch.setitem(cli.HANDLERS, "send", cli._send)
    assert cli.main(["send", "--list"]) == 2          # still disabled
    monkeypatch.delitem(cli.DISABLED, "send")
    assert cli.main(["send", "--help"]) == 0          # now it runs


def test_start_names_pyyaml_when_yaml_is_what_is_missing(capsys, monkeypatch):
    """start -> plan -> agentfile imports yaml at module load: PyYAML is named."""
    import sys
    for mod in [m for m in sys.modules if m.startswith("sessiontools.start")]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setitem(sys.modules, "yaml", None)                 # PyYAML gone
    assert cli.main(["start", "--dry-run", "bob"]) == 1
    assert "session start: broken install - PyYAML will not import" in capsys.readouterr().err


# --- session account (ST-22 do-006) ---------------------------------------------------
#
# HARD LIMITS (by design): no Keychain read or write, no real
# switch, no run of `security`, `pass` or `claude`. The guard below makes
# any subprocess naming one of them raise, and the usage exporter raise.
# The group itself is tested on fakes in tests/test_account_*.py; these
# pin the wiring: the verb, its usage row, the delegation, the lazy import.

ACCOUNT_FORBIDDEN = ("security", "pass", "claude")


@pytest.fixture
def no_keychain(monkeypatch, tmp_path):
    """Any subprocess call to security, pass or claude raises; the usage
    fetcher is unreachable; CLAUDE_CONFIG_DIR is empty; no env token. HOME
    is the conftest's tmp home."""
    import os

    def argv0(args, kwargs) -> str:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, (list, tuple)) and argv:
            return os.path.basename(str(argv[0]))
        if isinstance(argv, str) and argv.split():
            return os.path.basename(argv.split()[0])
        return ""

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, **kwargs):
            if argv0(args, kwargs) in ACCOUNT_FORBIDDEN:
                raise AssertionError(f"test tried to run {argv0(args, kwargs)!r}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)
    from sessiontools.account import status

    def unreachable(url):
        import urllib.error
        raise urllib.error.URLError("no exporter in a test")

    monkeypatch.setattr(status, "fetch_url", unreachable)
    empty = tmp_path / "claude-config-empty"
    empty.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return Path(os.path.expanduser("~"))


def test_account_is_a_verb_with_a_handler_and_a_usage_row(capsys):
    assert "account" in dict(cli.VERBS)
    assert cli.HANDLERS["account"] is cli._account
    assert cli.main([]) == 0
    assert "\n  account " in capsys.readouterr().out


def test_account_hands_its_argv_to_the_group_verbatim(monkeypatch):
    import sessiontools.account
    seen = []
    monkeypatch.setattr(sessiontools.account, "main", lambda argv: seen.append(argv) or 3)
    argv = ["use", "c", "--witness", "alice"]
    assert cli.main(["account", *argv]) == 3          # the group's exit code, unchanged
    assert seen == [argv]


def test_account_help_is_the_groups_and_names_every_verb(capsys, no_keychain):
    assert cli.main(["account", "--help"]) == 0      # argparse's exit, returned, not raised
    out = capsys.readouterr().out
    assert out.startswith("usage: session account")
    for verb in ("list", "status", "use", "add", "remove"):
        assert f"    {verb} " in out, verb
    assert "every seat with no env token within about 50 s" in out


def test_account_with_an_unknown_verb_is_exit_2(capsys, no_keychain):
    assert cli.main(["account", "nope"]) == 2
    assert "invalid choice: 'nope'" in capsys.readouterr().err


def test_account_list_with_no_registry_says_where_and_how(capsys, no_keychain):
    assert cli.main(["account", "list"]) == 0
    out = capsys.readouterr().out
    assert str(no_keychain / ".sessionkit/tools/accounts/accounts.json") in out
    assert "session account add NAME --email E --source BACKEND:REF" in out


def test_account_end_to_end_through_session_add_list_status_remove(capsys, no_keychain):
    """The real group, through `session`, on a tmp HOME: no Keychain (the
    config turns the host read off, OQ2), no pass (a 0600 file source),
    no exporter. The synthetic token is never printed."""
    import json
    import secrets as pysecrets
    token = "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)
    source = no_keychain / "token-a"
    source.write_text(token + "\n")
    source.chmod(0o600)
    config_dir = no_keychain / ".sessionkit/tools/accounts"
    config_dir.mkdir(parents=True)
    (config_dir / "account-config.json").write_text(json.dumps({"host_read": False}))

    assert cli.main(["account", "add", "a", "--email", "ai@example.com",
                     "--source", f"file:{source}"]) == 0
    assert cli.main(["account", "list"]) == 0
    assert cli.main(["account", "status"]) == 0
    assert cli.main(["account", "status", "--json"]) == 0
    assert cli.main(["account", "remove", "a"]) == 0
    out, err = capsys.readouterr()
    assert "added a (ai@example.com)" in out and "fp known" in out
    list_row = r"^a\s+ai@example\.com\s+file \(free\)\s+fp known$"
    assert re.search(list_row, out, re.MULTILINE)
    assert "follows the host" in out                    # status: this seat
    assert "usage: unavailable" in out                  # no exporter
    assert token not in out and token not in err
    for path in config_dir.rglob("*"):
        if path.is_file():
            assert token not in path.read_text(), path


def test_account_calls_a_failed_import_a_broken_install(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "sessiontools.account", None)   # import fails
    assert cli.main(["account", "list"]) == 1
    err = capsys.readouterr().err
    assert "session account: broken install" in err and "reinstall sessiontools" in err


def test_account_names_pyyaml_when_yaml_is_what_is_missing(capsys, monkeypatch):
    """account -> registry -> start.secrets; importing start loads yaml."""
    for mod in [m for m in sys.modules
                if m.startswith(("sessiontools.start", "sessiontools.account"))]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setitem(sys.modules, "yaml", None)                 # PyYAML gone
    assert cli.main(["account", "list"]) == 1
    assert "session account: broken install - PyYAML will not import" in capsys.readouterr().err


def test_current_docs_leaves_out_an_absent_claude_md(tmp_path, monkeypatch):
    """A release archive has no CLAUDE.md; the docs scan reads what is there
    and does not trip on what the archive left out."""
    for name in ("AGENTS.md", "README.md", "pyproject.toml"):
        (tmp_path / name).write_text(f"{name}\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert [p.name for p in current_docs()] == ["AGENTS.md", "README.md", "pyproject.toml"]
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "nowhere.md")
    assert "CLAUDE.md" in [p.name for p in current_docs()], "a dangling CLAUDE.md must be read, and fail"


# The release-archive case: the suite in a `git archive` tree must not go red
# on the missing CLAUDE.md. Run the three tests that read it in a copy of the
# tree with CLAUDE.md left out, as the archive does, and with a dangling one.
CLAUDE_MD_READERS = [
    "tests/test_cli.py::test_every_dependency_count_in_the_docs_matches_pyproject",
    "tests/test_cli.py::test_claude_md_is_the_agents_md_import",
    "tests/test_export_ignore.py::test_claude_md_carries_the_spec_map",
]


def run_claude_md_readers(tree: Path) -> subprocess.CompletedProcess:
    shutil.copytree(ROOT / "tests", tree / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("src", "docs"):
        shutil.copytree(ROOT / name, tree / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("AGENTS.md", "README.md", "pyproject.toml", ".gitattributes"):
        shutil.copy(ROOT / name, tree / name)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *CLAUDE_MD_READERS],
        cwd=tree, capture_output=True, text=True, check=False,
    )


def test_claude_md_readers_skip_or_pass_in_a_tree_without_it(tmp_path):
    result = run_claude_md_readers(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 2 skipped" in result.stdout, result.stdout
    assert result.stdout.count(".gitattributes export-ignores it") == 2, result.stdout


def test_claude_md_readers_still_fail_on_a_dangling_symlink(tmp_path):
    (tmp_path / "CLAUDE.md").symlink_to(tmp_path / "nowhere.md")
    result = run_claude_md_readers(tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "3 failed" in result.stdout, result.stdout
