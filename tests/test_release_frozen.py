"""scripts/release.py never re-locks, and refuses before it writes (ST-243).

A plain `uv version X` re-locks the project, and the release commits
uv.lock, so a cut before the deps are on PyPI failed, or committed a
local-path lock. And a re-run for a version already tagged was refused only
after `uv version` had bumped pyproject and uv.lock, leaving the tree dirty.
These tests pin both fixes:

- `uv version` carries --frozen, on the real path and the dry path alike;
- a REAL release, in a throwaway git clone, leaves every line of uv.lock as
  it was except the root `sessiontools` version line, so every dependency
  pin is byte-identical, and `uv lock --check` (offline) passes after it;
- the root-line edit fails closed: no root block, two, or two version lines
  in it, and the release refuses before `uv version` writes anything;
- an existing vX.Y.Z tag is refused before anything is written, real or dry,
  and `git status` stays clean;
- `make release` runs the script with python3, never a re-locking `uv run`.

Every repo here is a clone in tmp_path with its origin REMOVED and no tags
but the ones a test makes: no test reads this checkout's tags or can reach
a real remote. A dry run cannot catch the re-lock (`uv version --dry-run`
never locks), so the release tests are real ones, in the clone. Every uv
call runs offline against an EMPTY cache of its own, so any resolve fails:
an offline re-lock from a warm cache writes the same bytes as the fix when
the deps are unchanged, and would pass unseen. One case also floors a
dependency at a version PyPI does not have, the family-cut case.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release.py"
NAME = "sessiontools"
UV = shutil.which("uv")
GIT = shutil.which("git")
MAKE = shutil.which("make")
# The working-tree files a release reads or writes: the clone gets these as
# they are on disk, so the test checks the code under test, not HEAD's.
OVERLAY = ("scripts/release.py", "pyproject.toml", "uv.lock", "CHANGELOG.md", ".claude-plugin")

needs_uv_git = pytest.mark.skipif(UV is None or GIT is None, reason="uv and git are needed")


def load_release():
    spec = importlib.util.spec_from_file_location("sessiontools_release_frozen", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env(cwd: Path) -> dict[str, str]:
    """Hermetic git; no UV_* from the host (a UV_FROZEN would hide the bug),
    offline, and an empty uv cache beside the clone."""
    out = {k: v for k, v in hermetic_env().items() if not k.startswith(("UV_", "VIRTUAL_ENV"))}
    out.update(
        UV_OFFLINE="1",
        UV_CACHE_DIR=str(cwd.parent / "uv-cache"),
        UV_PYTHON_DOWNLOADS="never",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="core.hooksPath",
        GIT_CONFIG_VALUE_0="/dev/null",
    )
    return out


def run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env(cwd), capture_output=True, text=True,
                          check=False, timeout=180)


def git(cwd: Path, *args: str) -> str:
    out = run(cwd, "git", *HOOKS_OFF, *args)
    assert out.returncode == 0, out.stderr
    return out.stdout


def release(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(cwd, sys.executable, "scripts/release.py", *args)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone of this repo with the working tree's release files laid over
    it and committed, so the tree is clean for release.py. No origin, no tags."""
    repo = tmp_path / "repo"
    git(tmp_path, "clone", "-q", "--no-hardlinks", "--no-tags", str(ROOT), str(repo))
    git(repo, "remote", "remove", "origin")
    for rel in OVERLAY:
        src, dst = ROOT / rel, repo / rel
        if src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)
    git(repo, "add", "--", *[r for r in OVERLAY if (repo / r).exists()])
    if git(repo, "status", "--porcelain").strip():
        git(repo, "commit", "-q", "-m", "test: the working tree's release files")
    for tag in git(repo, "tag", "-l").split():
        git(repo, "tag", "-d", tag)
    assert git(repo, "remote").strip() == ""
    return repo


def version_of(repo: Path) -> str:
    return tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]


def root_line(lock: str) -> str:
    m = re.search(rf'^\[\[package\]\]\nname = "{NAME}"\n(version = "[^"]*")\n', lock, re.MULTILINE)
    assert m, f"no root {NAME} block in uv.lock"
    return m.group(1)


def with_root(lock: str, old: str, new: str) -> str:
    """`lock` with the root version line, and only it, moved to `new`."""
    head = f'[[package]]\nname = "{NAME}"\nversion = '
    assert lock.count(f'{head}"{old}"\n') == 1
    return lock.replace(f'{head}"{old}"\n', f'{head}"{new}"\n')


def next_patch(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def snapshot(repo: Path) -> dict[str, bytes]:
    files = ("pyproject.toml", "uv.lock", "CHANGELOG.md",
             ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json")
    return {f: (repo / f).read_bytes() for f in files if (repo / f).exists()}


# --- the uv call carries --frozen --------------------------------------------


@pytest.mark.parametrize("spec", ["patch", "minor", "major", "9.8.7"])
@pytest.mark.parametrize("dry", [False, True])
def test_uv_version_is_frozen_on_both_paths(monkeypatch, spec: str, dry: bool) -> None:
    module = load_release()
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout=f"{NAME} 0.3.0 => 9.8.7\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    assert module.bump(spec, dry) == "9.8.7"
    (argv,) = calls
    assert argv[:2] == ["uv", "version"]
    assert "--frozen" in argv
    assert ("--dry-run" in argv) is dry
    # The dry argv is the real argv plus --dry-run: a rehearsal runs the same call.
    assert [a for a in argv if a != "--dry-run"] == (
        ["uv", "version", "--bump", spec, "--frozen"] if spec in ("patch", "minor", "major")
        else ["uv", "version", spec, "--frozen"]
    )


# --- a real release, in a temp clone ---------------------------------------------


@needs_uv_git
def test_real_release_changes_only_the_root_lock_line(clone: Path) -> None:
    before = (clone / "uv.lock").read_text()
    old = version_of(clone)
    new = next_patch(old)

    out = release(clone, "patch")
    assert out.returncode == 0, out.stdout + out.stderr

    assert version_of(clone) == new
    after = (clone / "uv.lock").read_text()
    a, b = before.splitlines(keepends=True), after.splitlines(keepends=True)
    assert len(a) == len(b), "uv.lock gained or lost lines: something re-locked"
    changed = [(x, y) for x, y in zip(a, b) if x != y]
    assert changed == [(f'version = "{old}"\n', f'version = "{new}"\n')]
    assert root_line(after) == f'version = "{new}"'  # the changed line IS the root line
    # Every dependency pin byte-identical: the whole file is the old one with
    # that one line moved.
    assert after == with_root(before, old, new)

    # The release committed it and tagged it, and left the tree clean.
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "log", "-1", "--format=%s").strip() == f"chore: release v{new}"
    assert git(clone, "tag", "-l").split() == [f"v{new}"]
    assert root_line(git(clone, "show", f"v{new}:uv.lock")) == f'version = "{new}"'
    assert not (clone / ".venv").exists(), "something synced an environment"


@needs_uv_git
def test_release_with_a_floor_pypi_does_not_have(clone: Path) -> None:
    """The family cut: a dependency floored at a release not yet on PyPI. A
    re-lock is unsatisfiable here; the release must not need one."""
    pyproject = clone / "pyproject.toml"
    text = pyproject.read_text()
    floored = re.sub(r'"jsonschema>=[^"]+"', '"jsonschema>=99.0.0"', text, count=1)
    assert floored != text
    pyproject.write_text(floored)
    git(clone, "commit", "-q", "-am", "test: floor a dep at an unreleased version")
    before = (clone / "uv.lock").read_text()
    old = version_of(clone)
    new = next_patch(old)

    out = release(clone, "patch")
    assert out.returncode == 0, out.stdout + out.stderr

    assert (clone / "uv.lock").read_text() == with_root(before, old, new)
    assert git(clone, "status", "--porcelain").strip() == ""
    assert not (clone / ".venv").exists(), "something synced an environment"


@needs_uv_git
def test_lock_check_passes_offline_after_the_release(clone: Path) -> None:
    pre = run(clone, "uv", "lock", "--check")
    assert pre.returncode == 0, f"uv lock --check fails offline BEFORE the release: {pre.stderr}"
    out = release(clone, "patch")
    assert out.returncode == 0, out.stdout + out.stderr
    check = run(clone, "uv", "lock", "--check")
    assert check.returncode == 0, check.stderr
    assert git(clone, "status", "--porcelain").strip() == ""


@needs_uv_git
def test_dry_run_reports_the_lock_edit_and_writes_nothing(clone: Path) -> None:
    before = snapshot(clone)
    old = version_of(clone)
    new = next_patch(old)

    out = release(clone, "patch", "--dry-run")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "--frozen" in out.stdout
    assert f"uv.lock: root version {old} -> {new}" in out.stdout
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "tag", "-l").strip() == ""


# --- B: an existing tag is refused before ANY write ----------------------------


@pytest.mark.parametrize("dry", [False, True])
@needs_uv_git
def test_an_existing_tag_is_refused_before_anything_is_written(clone: Path, dry: bool) -> None:
    new = next_patch(version_of(clone))
    git(clone, "tag", "-a", f"v{new}", "-m", "already cut")
    before = snapshot(clone)
    head = git(clone, "rev-parse", "HEAD")

    out = release(clone, "patch", *(["--dry-run"] if dry else []))

    assert out.returncode != 0
    assert f"tag v{new} already exists" in out.stderr
    assert "nothing was written" in out.stderr
    # The only uv call was the dry one that works out the version.
    uv_calls = [line for line in out.stdout.splitlines() if "$ uv version" in line]
    assert len(uv_calls) == 1 and "--dry-run" in uv_calls[0], out.stdout
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "tag", "-l").split() == [f"v{new}"]


@needs_uv_git
def test_a_re_run_of_a_cut_release_leaves_the_tree_clean(clone: Path) -> None:
    """ST-239's dry cut, problem 7: cut X.Y.Z, then ask for X.Y.Z again."""
    first = release(clone, "patch")
    assert first.returncode == 0, first.stdout + first.stderr
    cut = version_of(clone)
    before = snapshot(clone)

    again = release(clone, cut)
    assert again.returncode != 0
    assert f"tag v{cut} already exists" in again.stderr
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""


def no_unreleased(repo: Path) -> None:
    changelog = repo / "CHANGELOG.md"
    text = changelog.read_text()
    assert "## [Unreleased]" in text
    changelog.write_text(text.replace("## [Unreleased]", "## [Pending]"))


def no_changelog(repo: Path) -> None:
    git(repo, "rm", "-q", "--", "CHANGELOG.md")


def manifest_without_version(repo: Path) -> None:
    plugin = repo / ".claude-plugin" / "plugin.json"
    text = plugin.read_text()
    stripped = re.sub(r'"version"\s*:\s*"[^"]+",?', "", text, count=1)
    assert stripped != text
    plugin.write_text(stripped)


@pytest.mark.parametrize(
    "spoil, says",
    [
        (no_unreleased, "no '## [Unreleased]' section"),
        (no_changelog, "CHANGELOG.md not found"),
        (manifest_without_version, "no version field in .claude-plugin/plugin.json"),
    ],
)
@pytest.mark.parametrize("dry", [False, True])
@needs_uv_git
def test_a_changelog_or_manifest_refusal_comes_before_any_write(clone: Path, spoil, says: str,
                                                                dry: bool) -> None:
    """These used to be refused after `uv version`, uv.lock and the manifests
    were written, so the refused run left four files modified."""
    spoil(clone)
    git(clone, "commit", "-q", "-am", "test: a tree release.py must refuse")
    before = snapshot(clone)
    head = git(clone, "rev-parse", "HEAD")

    out = release(clone, "patch", *(["--dry-run"] if dry else []))

    assert out.returncode != 0
    assert says in out.stderr
    assert "nothing was written" in out.stderr
    uv_calls = [line for line in out.stdout.splitlines() if "$ uv version" in line]
    assert len(uv_calls) == 1 and "--dry-run" in uv_calls[0], out.stdout
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "tag", "-l").strip() == ""


# --- the root-line edit fails closed -------------------------------------------

ROOT_BLOCK = re.compile(rf'^\[\[package\]\]\nname = "{NAME}"\n.*?(?=^\[\[package\]\]$|\Z)', re.MULTILINE | re.DOTALL)


def no_root(lock: str) -> str:
    return ROOT_BLOCK.sub("", lock, count=1)


def two_roots(lock: str) -> str:
    block = ROOT_BLOCK.search(lock).group(0)
    return lock + "\n" + block


def two_versions(lock: str) -> str:
    return re.sub(rf'^(name = "{NAME}"\nversion = "[^"]*"\n)', r'\1version = "0.0.0"\n', lock,
                  count=1, flags=re.MULTILINE)


def not_the_root_source(lock: str) -> str:
    """The block present, but sourced from an index: not the project."""
    return re.sub(
        rf'^(name = "{NAME}"\nversion = "[^"]*"\n)source = \{{ editable = "\." \}}$',
        r'\1source = { registry = "https://pypi.org/simple" }',
        lock,
        count=1,
        flags=re.MULTILINE,
    )


@pytest.mark.parametrize(
    "mangle, says",
    [
        (no_root, "has 0 root [[package]] blocks"),
        (two_roots, "has 2 root [[package]] blocks"),
        (two_versions, "has 2 version lines"),
        (not_the_root_source, "has 0 root [[package]] blocks"),
    ],
)
@pytest.mark.parametrize("dry", [False, True])
@needs_uv_git
def test_a_bad_lock_is_refused_before_anything_is_written(clone: Path, mangle, says: str, dry: bool) -> None:
    lock = clone / "uv.lock"
    mangled = mangle(lock.read_text())
    assert mangled != lock.read_text()
    lock.write_text(mangled)
    git(clone, "commit", "-q", "-am", "test: a lock release.py must refuse")
    before = snapshot(clone)
    head = git(clone, "rev-parse", "HEAD")

    out = release(clone, "patch", *(["--dry-run"] if dry else []))

    assert out.returncode != 0
    assert says in out.stderr
    assert "nothing was written" in out.stderr
    uv_calls = [line for line in out.stdout.splitlines() if "$ uv version" in line]
    assert all("--dry-run" in line for line in uv_calls), out.stdout  # no real uv write
    assert snapshot(clone) == before
    assert git(clone, "status", "--porcelain").strip() == ""
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "tag", "-l").strip() == ""


def test_lock_root_finds_the_one_version_value() -> None:
    module = load_release()
    lock = (ROOT / "uv.lock").read_text()
    start, end = module.lock_root(lock, NAME)
    assert lock[start:end] == version_of(ROOT)
    assert lock[:start].endswith(f'[[package]]\nname = "{NAME}"\nversion = "')


def test_missing_lock_is_refused(tmp_path: Path, monkeypatch) -> None:
    module = load_release()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["release.py", "9.9.9"])

    def fake_run(args, **kw):
        if args[:2] == ["uv", "version"]:
            assert "--dry-run" in args, args  # only the dry call may run first
            return subprocess.CompletedProcess(args, 0, stdout=f"{NAME} 0.3.0 => 9.9.9\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="uv.lock not found"):
        module.main()


# --- make release ----------------------------------------------------------------


def release_recipe() -> list[str]:
    lines = (ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("release:"))
    recipe = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    return recipe


def test_make_release_recipe_has_no_relocking_uv_run() -> None:
    recipe = release_recipe()
    assert recipe, "Makefile has no release: recipe"
    for line in recipe:
        assert not re.search(r"\buv\s+run\b", line), line
    assert any("scripts/release.py" in line for line in recipe)


@pytest.mark.skipif(MAKE is None, reason="make is not installed")
def test_make_release_expands_to_python3() -> None:
    out = subprocess.run(
        [str(MAKE), "-C", str(ROOT), "--no-print-directory", "-n", "release", "VERSION=9.9.9"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "python3 scripts/release.py 9.9.9"
