"""`listing.repo_name` finds the repo in-process (ST-111): every case is
checked against what `git rev-parse --show-toplevel` itself says, in real
repos built under tmp_path - a plain repo, a subdir, a linked worktree, a
submodule, a non-repo, a missing cwd, an unenterable cwd, and the `.git`
entries git refuses or passes over. None of them may fork git."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sessiontools import listing

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CEILING_DIRECTORIES": ""}


def git(*args, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | GIT_ENV
    subprocess.run(["git", "-c", "protocol.file.allow=always", "-c", "init.defaultBranch=main",
                    "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, env=env, check=True, capture_output=True)


def rev_parse(cwd) -> str:
    """The OLD repo_name, verbatim: what the answer must stay."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | GIT_ENV
    try:
        out = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=3, check=False, env=env)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(str(cwd)).name or str(cwd)


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    (path / "f").write_text("x")
    git("add", "f", cwd=path)
    git("commit", "-q", "-m", "one", cwd=path)
    return path


@pytest.fixture
def world(tmp_path):
    """outer-repo/ with a subdir, a linked worktree beside it, a submodule
    inside it, and a plain folder beside it."""
    listing.repo_name.cache_clear()
    outer = repo(tmp_path / "outer-repo")
    (outer / "sub" / "deeper").mkdir(parents=True)
    git("worktree", "add", "-q", str(tmp_path / "linked-tree"), cwd=outer)
    (tmp_path / "linked-tree" / "inside").mkdir()
    lib = repo(tmp_path / "lib-src")
    git("submodule", "add", "-q", str(lib), "vendor/lib-mod", cwd=outer)
    (tmp_path / "plain" / "folder").mkdir(parents=True)
    yield tmp_path
    listing.repo_name.cache_clear()


@pytest.fixture
def no_git_fork(monkeypatch):
    forks = []
    real = subprocess.run

    def counting(command, *a, **k):
        if os.path.basename(str(command[0])) == "git":
            forks.append(command)
        return real(command, *a, **k)

    monkeypatch.setattr(subprocess, "run", counting)
    return forks


CASES = {
    "plain repo": "outer-repo",
    "subdir of a repo": "outer-repo/sub/deeper",
    "linked worktree (.git file)": "linked-tree",
    "inside a linked worktree": "linked-tree/inside",
    "submodule (.git file)": "outer-repo/vendor/lib-mod",
    "non-repo folder": "plain/folder",
    "missing cwd inside a repo": "outer-repo/sub/gone",
}


@pytest.mark.parametrize("case", list(CASES))
def test_repo_name_matches_rev_parse(world, case):
    cwd = world / CASES[case]
    expected = rev_parse(cwd)
    listing.repo_name.cache_clear()
    assert listing.repo_name(str(cwd)) == expected


def test_the_expected_names_are_the_ones_meant(world):
    """Guards the oracle: rev-parse really did see each repo."""
    seen = {case: rev_parse(world / rel) for case, rel in CASES.items()}
    assert seen == {"plain repo": "outer-repo", "subdir of a repo": "outer-repo",
                    "linked worktree (.git file)": "linked-tree",
                    "inside a linked worktree": "linked-tree",
                    "submodule (.git file)": "lib-mod",
                    "non-repo folder": "folder", "missing cwd inside a repo": "gone"}


def test_repo_name_forks_no_git(world, no_git_fork):
    listing.repo_name.cache_clear()
    for rel in CASES.values():
        listing.repo_name(str(world / rel))
    assert no_git_fork == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters any folder")
def test_unenterable_cwd_inside_a_repo_is_its_basename(world):
    locked = world / "outer-repo" / "sub" / "locked"
    locked.mkdir()
    locked.chmod(0o600)
    try:
        assert listing.repo_name(str(locked)) == rev_parse(locked) == "locked"
    finally:
        locked.chmod(0o700)


def test_symlinked_cwd_names_the_real_repo(world):
    link = world / "alias"
    link.symlink_to(world / "outer-repo" / "sub")
    assert listing.repo_name(str(link)) == rev_parse(link) == "outer-repo"


def test_a_dot_git_folder_with_no_head_is_passed_over(world):
    """git does not accept an empty `.git` folder and keeps looking up."""
    fake = world / "outer-repo" / "sub" / "fake"
    (fake / ".git").mkdir(parents=True)
    assert listing.repo_name(str(fake)) == rev_parse(fake) == "outer-repo"


@pytest.mark.parametrize("content", ["gitdir: /nowhere/at/all\n", "not a gitfile\n", ""])
def test_a_broken_dot_git_file_is_no_repo(world, content):
    """git fails at a `.git` file it cannot follow; the cwd's name stands."""
    broken = world / "outer-repo" / "sub" / "broken"
    broken.mkdir()
    (broken / ".git").write_text(content)
    assert listing.repo_name(str(broken)) == rev_parse(broken) == "broken"


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="NOT TESTED as root: root searches a mode 000 folder, so none can be built")
def test_a_dot_git_file_whose_target_cannot_be_stat_ed_is_no_repo(world):
    """git fails at a `.git` file whose gitdir it cannot reach (rev-parse
    exits 128), so the walk stops there on every version. From 3.14
    `Path.is_dir()` returns False on it; 3.11 to 3.13 raised, and the walk
    climbed on to the repo above."""
    vault = world / "vault"
    (vault / "gd").mkdir(parents=True)
    wt = world / "outer-repo" / "sub" / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {vault / 'gd'}\n")
    vault.chmod(0o000)
    try:
        assert listing.repo_name(str(wt)) == rev_parse(wt) == "wt"
    finally:
        vault.chmod(0o700)


def test_empty_cwd_is_itself():
    listing.repo_name.cache_clear()
    assert listing.repo_name("") == ""
