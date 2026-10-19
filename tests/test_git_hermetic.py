"""The fixtures' git is hermetic against a hostile caller config (ST-161).

repro-001's recipe, made a test: a hooks dir whose pre-commit exits 1, named
by core.hooksPath in every place the caller's git could find it (the global
file by GIT_CONFIG_GLOBAL, ~/.gitconfig, GIT_CONFIG_COUNT), plus a default
XDG ignore that hides every file. The control proves the config bites: a
plain `git commit` fails under it. Then each fixture helper in the suite
still inits, adds and commits, and sees the file it added.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env
from test_export_ignore import git as export_ignore_git
from test_reap import git as reap_git
from test_statusline_install import _git as statusline_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def hostile(tmp_path, monkeypatch):
    """The caller's environ, made hostile to any git that reads it."""
    hooks = tmp_path / "hostile-hooks"
    hooks.mkdir()
    pre_commit = hooks / "pre-commit"
    pre_commit.write_text("#!/bin/sh\necho hostile hook ran >&2\nexit 1\n")
    pre_commit.chmod(0o755)
    config = f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgsign = true\n"
    gitconfig = tmp_path / "hostile.gitconfig"
    gitconfig.write_text(config)
    home = tmp_path / "hostile-home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text(config)
    (home / ".config" / "git" / "ignore").write_text("*\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hooks))
    return tmp_path


def test_the_hostile_config_breaks_a_plain_commit(hostile):
    """The control: without the hermetic env, the caller's hook runs and fails."""
    repo = hostile / "plain"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / "f.txt").write_text("x\n")
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            check=True, capture_output=True, text=True).stdout
    assert status == ""                                   # the XDG ignore hides it
    subprocess.run(["git", "-C", str(repo), "add", "-f", "f.txt"], check=True)
    got = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                          "-c", "commit.gpgsign=false", "commit", "-qm", "x"],
                         capture_output=True, text=True, check=False)
    assert got.returncode != 0 and "hostile hook ran" in got.stderr


def _hermetic_git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *HOOKS_OFF, *args], check=True,
                          capture_output=True, text=True, env=hermetic_env()).stdout


@pytest.mark.parametrize("git", [export_ignore_git, reap_git, statusline_git, _hermetic_git],
                         ids=["test_export_ignore", "test_reap", "test_statusline_install",
                              "git_support"])
def test_a_fixture_helper_commits_under_a_hostile_config(hostile, git):
    repo = hostile / "fixture"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "f.txt").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "fixture")
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], check=True,
                             capture_output=True, text=True, env=hermetic_env()).stdout
    assert tracked.split() == ["f.txt"]


def test_hermetic_env_drops_every_inherited_git_variable():
    env = hermetic_env({"PATH": "/bin", "GIT_DIR": "/elsewhere/.git", "GIT_CONFIG_COUNT": "6",
                        "GIT_AUTHOR_NAME": "A Human", "XDG_CONFIG_HOME": "/home/x/.config"})
    assert env["PATH"] == "/bin"
    assert "GIT_DIR" not in env and "GIT_CONFIG_COUNT" not in env
    assert env["GIT_AUTHOR_NAME"] == "t"
    assert (env["GIT_CONFIG_GLOBAL"], env["GIT_CONFIG_NOSYSTEM"]) == (os.devnull, "1")
    assert env["XDG_CONFIG_HOME"] == os.devnull


def test_hermetic_env_defaults_to_the_live_environ(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    monkeypatch.setenv("ST161_PROBE", "kept")
    env = hermetic_env()
    assert "GIT_DIR" not in env and env["ST161_PROBE"] == "kept"
