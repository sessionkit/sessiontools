"""The release tree leaves out the dev diary: `.gitattributes` export-ignore.

D7, ruled (a) on 8 Dec 2030: the diary stays in tree and history and is
export-ignored, and the public repo is built from `git archive` (SK-84).
ST-149 carries sessiontools' share of SK-81's list: `/openspec` with
`/openspec/**`, and the plugin-root `/CLAUDE.md`.

The pattern form is load-bearing, and both openspec lines are needed.
`/openspec/**` is the contract: gitattributes says a directory pattern does
not match the paths inside it, so only `**` sets export-ignore on each file
(`git check-attr`). Alone it leaves an empty `openspec/` entry in the archive
(measured, git 2.56); `/openspec` prunes that. So these tests carry the REAL
`.gitattributes` into a scratch repo and check both what `git archive` writes
and what `git check-attr` says per file, rather than reading the file's text.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env

ROOT = Path(__file__).resolve().parents[1]
ATTRS = ROOT / ".gitattributes"

#: Paths a release must ship: the ones next to the ignored ones that a too-wide
#: pattern would catch first.
KEPT = [
    "src/sessiontools/__init__.py",
    "tests/test_x.py",
    "skills/session/SKILL.md",
    "docs/guide.md",
    "hooks/hooks.json",
    ".claude-plugin/plugin.json",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "pyproject.toml",
    "AGENTS.md",
    ".gitattributes",
    # anchored: only the ROOT openspec/ and the ROOT CLAUDE.md are ignored
    "skills/session/CLAUDE.md",
    "docs/openspec/notes.md",
]

#: Paths a release must NOT ship.
DROPPED = [
    "CLAUDE.md",
    "openspec/config.yaml",
    "openspec/RATIFICATION.md",
    "openspec/changes/session-settings/tasks.md",
    "openspec/specs/x/spec.md",
]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *HOOKS_OFF, *args],
        check=True, capture_output=True, env=hermetic_env(),     # ST-161
    ).stdout


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """A scratch repo holding KEPT + DROPPED and our real `.gitattributes`."""
    repo = tmp_path_factory.mktemp("export-ignore")
    git(repo, "init", "-q")
    for rel in KEPT + DROPPED:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{rel}\n")
    shutil.copyfile(ATTRS, repo / ".gitattributes")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fixture")
    return repo


@pytest.fixture(scope="module")
def archived(repo: Path) -> list[str]:
    """Every entry `git archive HEAD` writes for that repo."""
    with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "HEAD"))) as tar:
        return tar.getnames()


def test_the_attributes_file_is_at_the_repo_root():
    assert ATTRS.is_file(), ".gitattributes is gone: git archive ships the dev diary"


def test_the_archive_has_no_openspec_entry_at_all(archived):
    # not even the empty directory `/openspec/**` would leave behind
    assert [n for n in archived if n.rstrip("/") == "openspec" or n.startswith("openspec/")] == []


@pytest.mark.parametrize("rel", DROPPED)
def test_the_diary_and_root_claude_md_are_left_out(archived, rel):
    assert rel not in archived


@pytest.mark.parametrize("rel", KEPT)
def test_everything_else_ships(archived, rel):
    assert rel in archived


def export_ignore(repo: Path, rel: str) -> str:
    """`git check-attr export-ignore` for one path: set, unset or unspecified."""
    out = git(repo, "check-attr", "export-ignore", "--", rel).decode()
    return out.rsplit(": ", 1)[1].strip()


@pytest.mark.parametrize("rel", DROPPED)
def test_check_attr_sets_export_ignore_on_each_dropped_file(repo, rel):
    # the contract, per file: not git archive's directory pruning
    assert export_ignore(repo, rel) == "set"


@pytest.mark.parametrize("rel", KEPT)
def test_check_attr_leaves_each_kept_file_unspecified(repo, rel):
    assert export_ignore(repo, rel) == "unspecified"


#: A backticked span or a Markdown link target in AGENTS.md that reads as a
#: repository path: it has a slash, or ends in a file suffix.
PATHLIKE = re.compile(r"/|\.(md|py|toml|ya?ml|json|txt)$")


def agents_md_paths() -> list[str]:
    text = (ROOT / "AGENTS.md").read_text()
    spans = re.findall(r"`([^`\n]+)`", text)
    links = re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r"^\[[^\]]+\]:\s*(\S+)", text, re.MULTILINE)
    return [s.strip() for s in spans + links if PATHLIKE.search(s.strip()) and " " not in s.strip()]


def test_agents_md_names_no_path_the_archive_leaves_out(repo):
    """AGENTS.md ships in the release archive and must stand alone there, so
    every path it names is one `git archive` keeps. The spec map, which names
    openspec/, lives in CLAUDE.md below the import: CLAUDE.md never ships.
    Checked with `git check-attr` under the real `.gitattributes`."""
    paths = agents_md_paths()
    assert "tests/test_cli.py" in paths, "the path scan found nothing: the regex is broken"
    dropped = [p for p in paths if export_ignore(repo, p.rstrip("/")) == "set"]
    assert dropped == [], f"AGENTS.md names paths the archive leaves out: {dropped}"


def test_agents_md_says_the_specs_in_words():
    # the rule stays in AGENTS.md; only the directory names move out
    text = (ROOT / "AGENTS.md").read_text()
    assert "REFER TO THE SPECS" in text
    assert "openspec" not in text.lower()
    assert "RATIFIED" in text and "PROPOSED" in text


# A release archive has no CLAUDE.md: .gitattributes export-ignores it, which
# is what this module pins. There the spec map has nothing to read and skips; in
# a checkout it still asserts. `lexists`: a dangling symlink must still fail.
@pytest.mark.skipif(
    not os.path.lexists(ROOT / "CLAUDE.md"),
    reason="no CLAUDE.md: .gitattributes export-ignores it, so a release archive leaves it out",
)
def test_claude_md_carries_the_spec_map():
    # the map AGENTS.md no longer names lives below the import, not nowhere
    lines = (ROOT / "CLAUDE.md").read_text().splitlines()
    assert lines[0] == "@AGENTS.md"
    body = "\n".join(lines[1:])
    for path in ("openspec/specs/", "openspec/changes/", "openspec/RATIFICATION.md"):
        assert path in body, f"CLAUDE.md lost the spec map entry {path}"
