"""What a shipped file may link: this repo relatively, others on HEAD (ST-215).

Two rules, each a pattern here.

- A link to another sessionkit repo names no branch. In a
  github.com/sessionkit/<repo>/blob|tree|raw/<ref>/ URL (or its
  raw.githubusercontent.com/sessionkit/<repo>/<ref>/ form) the ref is
  `HEAD` or a `v` tag. GitHub resolves HEAD to the default branch, so a
  rename cannot break the link; a branch name such as main does not
  exist on a repo published with one branch, and master would break on a
  rename. A `-b main` / `--branch main` beside a sessionkit repo is
  refused too: branch instructions say master.
- A link to a file in THIS repo is relative, so a checkout reads it
  offline. A full github.com/sessionkit/sessiontools/blob|tree|raw/ URL
  (or its raw.githubusercontent.com form) is refused, whatever its ref.

A repo's bare home URL, with no blob/tree/raw, is not a file link and is
allowed.

The scan covers what ships. That is every tracked file except the ones
`.gitattributes` export-ignores, because the public tree is built from
`git archive`. It reads the working tree, so an edit is seen before it is
committed. In a tree that is not a git checkout (an unpacked archive), it
walks every file instead.

The scanner is proved, not trusted. `test_scanner_catches_*` plant each
shape of both rules in a fixture string and assert it is caught, and
`test_scan_of_a_planted_file_fails` plants lines in a scratch tree.
The needles below are built from pieces, so this file does not flag itself.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env

ROOT = Path(__file__).resolve().parent.parent

OWNER = "sessionkit"
THIS_REPO = "session" + "tools"
HEAD = "HE" + "AD"  # the ref a cross-repo link names
MAIN = "ma" + "in"  # the branch the public repos do not have
MASTER = "mas" + "ter"  # the branch they have, which a link still does not name

# A repo name, and a ref's characters, on GitHub: letters, digits, `.`, `-`, `_`.
_NAME = r"[A-Za-z0-9_.-]+"
# The start of a file URL in a sessionkit repo, up to its ref. Host, owner and
# kind match in any case; the ref does not, since HEAD is case-sensitive.
_FILE_URL = (rf"(?i:github\.com/{OWNER}/{_NAME}/(?:blob|tree|raw|edit|blame|commits)/"
             rf"|raw\.githubusercontent\.com/{OWNER}/{_NAME}/)")
# An allowed ref, as a whole path segment: HEAD, or a v tag (v0.1.0).
_ALLOWED_REF = r"(?:HEAD(?![A-Za-z0-9_.-])|v[0-9])"

#: A file URL in a sessionkit repo whose ref is a branch name, not HEAD or a tag.
BRANCH_REF = re.compile(rf"{_FILE_URL}(?={_NAME})(?!{_ALLOWED_REF})")
# `-b main` / `--branch main` / `--branch=main`, flagged on a line that names
# a sessionkit repo (an https or an ssh remote).
BRANCH_FLAG = re.compile(rf"(?<![\w-])(?:-b|--branch)(?:=|[ \t]+){MAIN}(?![A-Za-z0-9_.-])")
SESSIONKIT_REMOTE = re.compile(rf"github\.com[/:]{OWNER}/", re.IGNORECASE)
#: A full URL to a file in this repo, which a doc writes relative instead.
THIS_REPO_FILE = re.compile(
    rf"github\.com/{OWNER}/{THIS_REPO}/(?:blob|tree|raw)/"
    rf"|raw\.githubusercontent\.com/{OWNER}/{THIS_REPO}/",
    re.IGNORECASE,
)

# Directories a checkout may hold that never ship (the archive fallback only).
_NOT_SHIPPED = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
                "build", "dist", "wheels", ".claude"}


def names_a_branch(line: str) -> bool:
    """LINE links a sessionkit repo by a branch name, or checks one out by main."""
    return bool(BRANCH_REF.search(line)
                or (BRANCH_FLAG.search(line) and SESSIONKIT_REMOTE.search(line)))


def links_this_repo_in_full(line: str) -> bool:
    return bool(THIS_REPO_FILE.search(line))


def hits_in(text: str, name: str = "<fixture>") -> list[str]:
    """Each `name:line: text` in TEXT that breaks either rule."""
    return [f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(text.splitlines(), 1)
            if names_a_branch(line) or links_this_repo_in_full(line)]


def _git(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(["git", *HOOKS_OFF, "-C", str(root), *args], input=stdin,
                          env=hermetic_env(), capture_output=True, check=True).stdout


def shipped_files(root: Path) -> list[Path]:
    """The files a release of ROOT ships, as paths under ROOT."""
    if shutil.which("git") and (root / ".git").exists():
        tracked = [p for p in _git(root, "ls-files", "-z").decode().split("\0") if p]
        attrs = _git(root, "check-attr", "-z", "--stdin", "export-ignore",
                     stdin="\0".join(tracked).encode()).decode().split("\0")
        # -z output: path, attribute, value, repeated.
        ignored = {attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "set"}
        return [root / p for p in tracked if p not in ignored and (root / p).is_file()]
    return sorted(p for p in root.rglob("*") if p.is_file()
                  and not _NOT_SHIPPED.intersection(p.relative_to(root).parts))


def scan(root: Path) -> list[str]:
    found = []
    for path in shipped_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # a binary file names no link
        found += hits_in(text, str(path.relative_to(root)))
    return found


# --- the rules, applied to what ships -----------------------------------------


def test_no_shipped_file_names_a_branch_or_links_this_repo_in_full():
    found = scan(ROOT)
    assert not found, (f"a link to another {OWNER} repo names {HEAD} or a tag, and a "
                       f"link into {THIS_REPO} is relative; these break that:\n"
                       + "\n".join(found))


def test_the_scan_reads_the_shipped_docs():
    """A scan that read nothing would pass: the docs that carry links are in it."""
    names = {str(p.relative_to(ROOT)) for p in shipped_files(ROOT)}
    assert {"README.md", "docs/configuration.md", "docs/settings.md"} <= names


# --- the scanner itself -------------------------------------------------------

_URL = f"https://github.com/{OWNER}"


@pytest.mark.parametrize("line", [
    f"see {_URL}/sessionkit/blob/{MAIN}/docs/settings.md#part",
    f"see {_URL}/sessionkit/blob/{MASTER}/docs/settings.md",
    f"[a]({_URL}/sessionkit/tree/{MAIN}/docs)",
    f"[a]({_URL}/sessionkit/tree/{MASTER}/docs)",
    f"{_URL}/sessionmail/raw/{MAIN}/README.md",
    f"{_URL}/sessionkit/tree/{MAIN}",
    f"{_URL}/sessionkit/blob/dev/x.md",
    f"{_URL}/sessionkit/blob/{HEAD.lower()}/x.md",  # HEAD is case-sensitive
    f"{_URL}/sessionkit/blob/{HEAD}s/x.md",  # a branch that starts with HEAD
    f"{_URL}/SessionKit/BLOB/{MAIN.upper()}/x",
    f"{_URL}/sessionkit/edit/{MAIN}/README.md",
    f"curl -fsSL https://raw.githubusercontent.com/{OWNER}/sessionkit/{MAIN}/x.sh",
    f"curl -fsSL https://raw.githubusercontent.com/{OWNER}/sessionkit/{MASTER}/x.sh",
    f"git clone -b {MAIN} {_URL}/sessionkit.git",
    f"git clone --branch {MAIN} {_URL}/{THIS_REPO}",
    f"git clone --branch={MAIN} git@github.com:{OWNER}/{THIS_REPO}.git",
])
def test_scanner_catches_a_branch_name(line):
    assert names_a_branch(line) and not links_this_repo_in_full(line), line
    assert len(hits_in(line)) == 1, line


@pytest.mark.parametrize("line", [
    f"[a]: {_URL}/{THIS_REPO}/blob/{HEAD}/docs/x.md",
    f"[a]({_URL}/{THIS_REPO}/tree/{HEAD}/docs)",
    f"{_URL}/{THIS_REPO}/raw/{HEAD}/README.md",
    f"{_URL}/{THIS_REPO}/blob/v0.1.0/LICENSE",
    f"{_URL}/{THIS_REPO.upper()}/Blob/{HEAD}/LICENSE",
    f"https://raw.githubusercontent.com/{OWNER}/{THIS_REPO}/{HEAD}/x.sh",
])
def test_scanner_catches_a_full_url_into_this_repo(line):
    assert links_this_repo_in_full(line) and not names_a_branch(line), line
    assert len(hits_in(line)) == 1, line


def test_a_line_breaking_both_rules_is_one_hit():
    line = f"[a]: {_URL}/{THIS_REPO}/blob/{MASTER}/docs/x.md"
    assert names_a_branch(line) and links_this_repo_in_full(line)
    assert len(hits_in(line)) == 1


@pytest.mark.parametrize("line", [
    f"[a]: {_URL}/sessionkit/blob/{HEAD}/docs/settings.md",
    f"[a]: {_URL}/sessionkit/blob/{HEAD}/docs/settings.md#part",
    f"{_URL}/sessionmail/tree/{HEAD}",
    f"{_URL}/sessionmail/tree/{HEAD}/docs",
    f"{_URL}/sessionkit/blob/v0.1.0/docs/settings.md",
    f"https://raw.githubusercontent.com/{OWNER}/sessionkit/{HEAD}/x.sh",
    f"{_URL}/sessionkit/blob/{HEAD}/src/{MAIN}/x.py",  # a path holding /main/
    f"git clone -b {MASTER} {_URL}/sessionkit.git",  # a branch instruction says master
    f"git clone -b {MAIN}line {_URL}/sessionkit.git",  # another branch name
    f'"homepage": "{_URL}/{THIS_REPO}",',  # the bare home page, not a file
    f"git clone {_URL}/{THIS_REPO}.git",
    f"{_URL}/{THIS_REPO}/issues",
    f"{_URL}/sessionkit",
    "[a]: docs/x.md",  # relative, as a link into this repo is written
    f"https://github.com/other/project/blob/{MAIN}/README.md",  # not a sessionkit repo
    f"{_URL}/<repo>/blob/<ref>/path",  # a placeholder in prose
    f"git -c init.defaultBranch={MAIN} init",  # a scratch fixture's local branch
    f"git checkout -b {MAIN}",  # no sessionkit repo on the line
    f"def {MAIN}(argv):",
    f'if __name__ == "__{MAIN}__":',
])
def test_scanner_passes(line):
    assert hits_in(line) == [], line


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_scan_of_a_planted_file_fails(tmp_path):
    """Planted /blob/main and /blob/master links to another repo, and a planted
    full /blob/HEAD URL into this repo, each fail the scan in a shipped file;
    the same lines in an export-ignored file do not, and the fixed lines pass."""
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "openspec").mkdir()
    (repo / ".gitattributes").write_text("/openspec/** export-ignore\n")
    kit = f"{_URL}/sessionkit/blob"
    planted = [f"[x]: {kit}/{MAIN}/docs/x.md",
               f"[y]: {kit}/{MASTER}/docs/y.md",
               f"[z]: {_URL}/{THIS_REPO}/blob/{HEAD}/docs/z.md"]
    (repo / "docs" / "x.md").write_text("\n".join(planted) + "\n")
    (repo / "openspec" / "notes.md").write_text("\n".join(planted) + "\n")
    env = hermetic_env() | {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    for args in (("init", "-q"), ("add", "-A")):
        subprocess.run(["git", *HOOKS_OFF, *args], cwd=repo, env=env, check=True,
                       capture_output=True)
    assert scan(repo) == [f"docs/x.md:{n}: {line}" for n, line in enumerate(planted, 1)]
    fixed = [f"[x]: {kit}/{HEAD}/docs/x.md", f"[y]: {kit}/{HEAD}/docs/y.md", "[z]: z.md"]
    (repo / "docs" / "x.md").write_text("\n".join(fixed) + "\n")
    assert scan(repo) == []


def test_scan_of_an_unpacked_tree_fails(tmp_path):
    """Outside a git checkout every file is scanned; a cache folder is not."""
    planted = f"see {_URL}/sessionkit/tree/{MAIN}/docs\n"
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text(planted)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "y.md").write_text(planted)
    assert scan(tmp_path) == [f"docs/x.md:1: {planted.strip()}"]
