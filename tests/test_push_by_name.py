"""`make push` and release.py's push line push the release tag BY NAME (ST-243).

Both once pushed `--tags`: every local tag, the ready/ and approved/
merge-queue tags among them, went to whatever origin was. Now the branch goes,
then `vX.Y.Z` alone, the version read from pyproject.toml; a missing tag is
refused before anything is pushed.

The behavioural tests run the REAL Makefile in a throwaway repo whose origin
is a bare repo in tmp_path. Nothing here pushes anywhere else.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
RELEASE = ROOT / "scripts" / "release.py"
VERSION = "1.2.3"
FORBIDDEN = ("--tags", "--follow-tags")

needs_make_uv = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("uv") is None,
    reason="make and uv are both needed to run the push target",
)


def push_recipe() -> str:
    """The `push:` rule and its recipe lines, as the Makefile holds them."""
    lines = MAKEFILE.read_text().splitlines()
    (start,) = [i for i, line in enumerate(lines) if line.startswith("push:")]
    recipe = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    assert len(recipe) > 1, "the push rule has no recipe"
    return "\n".join(recipe)


def env() -> dict[str, str]:
    e = hermetic_env()
    for var in ("UV_PROJECT", "UV_FROZEN", "UV_LOCKED", "MAKEFLAGS", "MAKELEVEL"):
        e.pop(var, None)
    # No hook runs in the throwaway repos (make's git cannot take HOOKS_OFF).
    e |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
          "GIT_CONFIG_VALUE_0": os.devnull, "UV_OFFLINE": "1", "UV_PYTHON": sys.executable}
    return e


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *HOOKS_OFF, *args], cwd=cwd, env=env(),
                         capture_output=True, text=True, check=True, timeout=60)
    return out.stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A one-commit repo at VERSION whose origin is an empty bare repo in tmp_path."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", *HOOKS_OFF, "init", "-q", "--bare", str(origin)],
                   env=env(), check=True, timeout=60)
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{VERSION}"\nrequires-python = ">=3.11"\n')
    (work / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    # release.py records the version on uv.lock's root line, so it needs one.
    (work / "uv.lock").write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.11"\n\n'
        f'[[package]]\nname = "x"\nversion = "{VERSION}"\nsource = {{ editable = "." }}\n')
    git(work, "add", "--", "pyproject.toml", "CHANGELOG.md", "uv.lock")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(origin))
    return work, origin


def make_push(work: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["make", "--no-print-directory", "-f", str(MAKEFILE), "push"],
                          cwd=work, env=env(), capture_output=True, text=True,
                          timeout=120, check=False)


def remote_refs(origin: Path) -> set[str]:
    out = git(origin, "for-each-ref", "--format=%(refname)")
    return set(out.split())


# --- static: no recipe, no printed line, ever says --tags ---------------------


def test_the_push_recipe_never_pushes_every_tag():
    recipe = push_recipe()
    for flag in FORBIDDEN:
        assert flag not in recipe, recipe
    assert 'git push origin "$$tag"' in recipe, recipe


def load_release():
    spec = importlib.util.spec_from_file_location("sessiontools_release_script", RELEASE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_py_prints_no_every_tag_line():
    """No print, and no returned push line, in release.py says --tags."""
    code = [line for line in RELEASE.read_text().splitlines()
            if "print(" in line or "git push" in line and "return" in line]
    assert any("push_line(tag)" in line for line in code), code
    for line in code:
        for flag in FORBIDDEN:
            assert flag not in line, line


def test_release_py_push_line_names_the_tag():
    line = load_release().push_line("v9.8.7")
    assert line == "git push origin HEAD && git push origin v9.8.7"
    assert not any(flag in line for flag in FORBIDDEN)


@needs_make_uv
def test_release_dry_run_prints_the_by_name_line_with_the_real_version(repo):
    work, origin = repo
    out = subprocess.run([sys.executable, str(RELEASE), "patch", "--dry-run"], cwd=work,
                         env=env(), capture_output=True, text=True, timeout=120, check=False)
    assert out.returncode == 0, out.stderr
    last = out.stdout.rstrip().splitlines()[-1].strip()
    assert last == "git push origin HEAD && git push origin v1.2.4", out.stdout
    assert not any(flag in out.stdout for flag in FORBIDDEN), out.stdout
    assert remote_refs(origin) == set()


# --- behaviour: the real Makefile against a bare origin in tmp_path -------------


@needs_make_uv
def test_make_push_refuses_a_missing_tag_and_pushes_nothing(repo):
    work, origin = repo
    git(work, "tag", "ready/fix/x")  # a merge-queue tag that must never travel
    out = make_push(work)
    assert out.returncode != 0
    assert f"tag v{VERSION} is not in this repository" in out.stderr, out.stderr
    assert "nothing pushed" in out.stderr
    assert remote_refs(origin) == set(), "a refused push sent something"


@needs_make_uv
def test_make_push_sends_the_branch_and_only_the_release_tag(repo):
    work, origin = repo
    git(work, "tag", "-a", f"v{VERSION}", "-m", f"x v{VERSION}")
    for other in ("ready/fix/x", "approved/fix/x", "v0.0.1"):
        git(work, "tag", other)
    out = make_push(work)
    assert out.returncode == 0, out.stderr
    assert remote_refs(origin) == {"refs/heads/main", f"refs/tags/v{VERSION}"}
    assert git(origin, "rev-parse", "main").strip() == git(work, "rev-parse", "HEAD").strip()


@needs_make_uv
def test_make_push_refuses_when_the_version_cannot_be_read(repo):
    work, origin = repo
    (work / "pyproject.toml").write_text("not toml [\n")
    out = make_push(work)
    assert out.returncode != 0
    assert "could not read the version" in out.stderr, out.stderr
    assert remote_refs(origin) == set()


def test_the_push_docs_say_by_name():
    text = (ROOT / "docs" / "building-and-testing.md").read_text()
    flat = " ".join(text.split())
    assert "`git push origin HEAD && git push origin vX.Y.Z`" in flat
    assert not re.search(r"pushes the branch and the tags", flat)
