"""`make lint` fails when uv.lock disagrees with pyproject.toml (ST-122).

010616c (chore: release v0.1.0) bumped pyproject.toml's version without a
re-lock, so uv.lock still pinned 0.0.5 and every `uv run` rewrote uv.lock in
each checkout it touched. `make lock-check` (a prerequisite of `make lint`)
runs `uv lock --check --offline`. These tests run that target against copies
of pyproject.toml + uv.lock in tmp_path, pointed there by UV_PROJECT, so the
real tree is never written and nothing reaches the network.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("uv") is None,
    reason="make and uv are both needed to run the lock-check target",
)


def run_lock_check(project: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run `make lock-check` from the repo root, checking `project` if given."""
    env = dict(os.environ)
    # A caller's UV_FROZEN/UV_LOCKED would change what `uv lock` does; drop them.
    for var in ("UV_PROJECT", "UV_FROZEN", "UV_LOCKED", "UV_OFFLINE"):
        env.pop(var, None)
    if project is not None:
        env["UV_PROJECT"] = str(project)
    # A copy in tmp_path has no .venv, so offline uv must find a Python >=3.11
    # on its own; a bare system PATH has none, and the check then failed for
    # that alone. Hand it this test's own interpreter.
    env["UV_PYTHON"] = sys.executable
    return subprocess.run(
        ["make", "--no-print-directory", "lock-check"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def copy_project(dst: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(ROOT / name, dst / name)
    return dst


def edit_pyproject(project: Path, pattern: str, repl: str) -> None:
    path = project / "pyproject.toml"
    text, n = re.subn(pattern, repl, path.read_text(), count=1, flags=re.MULTILINE)
    assert n == 1, f"pattern {pattern!r} not found in the copied pyproject.toml"
    path.write_text(text)


def bump_version(project: Path) -> None:
    edit_pyproject(project, r'^(version\s*=\s*")([^"]+)(")',
                   lambda m: f"{m[1]}{m[2]}.post1{m[3]}")


def test_lint_runs_lock_check():
    """The guard sits on `make lint`, not only on its own target."""
    result = subprocess.run(["make", "-n", "--no-print-directory", "lint"], cwd=ROOT,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "uv lock --check --offline" in result.stdout


def test_passes_on_the_real_tree():
    before = (ROOT / "uv.lock").read_bytes()
    result = run_lock_check()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "uv.lock agrees with pyproject.toml" in result.stdout
    assert (ROOT / "uv.lock").read_bytes() == before


def test_passes_on_an_unchanged_copy(tmp_path):
    """The copy itself agrees, so a failure below comes from the edit alone."""
    result = run_lock_check(copy_project(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr


def test_fails_naming_uv_lock_when_version_bumped_without_relock(tmp_path):
    project = copy_project(tmp_path)
    bump_version(project)
    lock_before = (project / "uv.lock").read_bytes()
    result = run_lock_check(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "lock-check: uv.lock does not agree with pyproject.toml" in result.stderr
    assert "run 'uv lock' and commit uv.lock" in result.stderr
    # A check, not a fix: uv.lock is left exactly as it was.
    assert (project / "uv.lock").read_bytes() == lock_before


def test_fails_when_a_dependency_is_dropped_without_relock(tmp_path):
    """A dependency change is caught too; dropping one needs nothing from the index."""
    project = copy_project(tmp_path)
    edit_pyproject(project, r',\s*"jsonschema>=4"', "")
    result = run_lock_check(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "uv.lock does not agree with pyproject.toml" in result.stderr


def test_fails_when_uv_lock_is_missing(tmp_path):
    project = copy_project(tmp_path)
    (project / "uv.lock").unlink()
    result = run_lock_check(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "uv.lock" in result.stderr
    assert not (project / "uv.lock").exists(), "the check must not write a lockfile"
