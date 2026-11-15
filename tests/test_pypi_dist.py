"""`make pypi-dist`: the dist PyPI gets has no relative README link (ST-312).

PyPI shows README.md, and the README's relative links (reference definitions
such as `[verbs]: docs/session.md`) 404 there: no repo around them. `make
publish` used to upload a plain `uv build` of the checkout. Now it depends on
`pypi-dist`, which builds from the release tag in a scratch stage, pins the
stage README's links to that tag with the KIT's one `scripts/pypi_readme.py`
(never a copy here), and checks both the wheel and the sdist. It refuses
unless the kit's `tag-check` (SK-289) finds the tag on PUBLIC_REMOTE at the
same commit as here (ST-323), and refuses loudly when the kit's script is not
under KIT_DIR (default: the sibling ../sessionkit).

The behavioural tests run the REAL Makefile in a throwaway repo whose
PUBLIC_REMOTE is a bare repo in tmp_path. Nothing here pushes anywhere else,
and nothing reaches the network (UV_OFFLINE=1).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = ROOT / "Makefile"
VERSION = "1.2.3"
TAG = f"v{VERSION}"
WHEEL = f"sessiontools-{VERSION}-py3-none-any.whl"
SDIST = f"sessiontools-{VERSION}.tar.gz"

needs_make_uv = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("uv") is None,
    reason="make and uv are both needed to run the pypi-dist target",
)

# A stand-in for the kit's pypi_readme.py: it logs each call. `rewrite` does
# what the real one does to the fixture README's one relative link;
# `tag-check` refuses as the real one does (SK-289) unless the local and the
# public tag are one commit. test_end_to_end_with_the_real_kit_script and
# test_the_real_kit_refuses_a_moved_tag hold the stand-in to the real verb.
STUB = r'''
import json, os, subprocess, sys
with open(os.environ["PYPI_STUB_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1] == "tag-check":
    a = sys.argv[1:]
    tag, remote = a[a.index("--tag") + 1], a[a.index("--remote") + 1]
    def run(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True).stdout
    here = run("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").strip()
    refs = dict(reversed(line.split("\t")) for line in run(
        "ls-remote", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}").splitlines())
    there = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if not here:
        sys.exit(f"pypi_readme tag-check: tag {tag} is not in this repo; tag the release first")
    if there is None:
        sys.exit(f"pypi_readme tag-check: tag {tag} is not on {remote}; "
                 "push the tag to the public repo first")
    if here != there:
        sys.exit(f"pypi_readme tag-check: tag {tag} differs: local {here}, public {there} "
                 f"on {remote}; the local tag must be the commit the public repo tagged")
    print(f"pypi_readme tag-check: {tag} is {here} here and on {remote}")
if sys.argv[1] == "rewrite":
    a = sys.argv[1:]
    repo, ref, readme = a[a.index("--repo") + 1], a[a.index("--ref") + 1], a[-1]
    text = open(readme).read()
    open(readme, "w").write(text.replace(
        "]: docs/guide.md", f"]: https://github.com/{repo}/blob/{ref}/docs/guide.md"))
    print("1 link(s) rewritten")
'''


def recipe(target: str) -> str:
    """The `target:` rule and its recipe lines, as the Makefile holds them."""
    lines = MAKEFILE.read_text().splitlines()
    (start,) = [i for i, line in enumerate(lines) if line.startswith(f"{target}:")]
    out = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        out.append(line)
    return "\n".join(out)


def env(log: Path | None = None) -> dict[str, str]:
    e = hermetic_env()
    for var in ("UV_PROJECT", "UV_FROZEN", "UV_LOCKED", "MAKEFLAGS", "MAKELEVEL",
                "KIT_DIR", "PYPI_README", "PUBLIC_REMOTE"):
        e.pop(var, None)
    # No hook runs in the throwaway repos (make's git cannot take HOOKS_OFF).
    e |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
          "GIT_CONFIG_VALUE_0": os.devnull, "UV_OFFLINE": "1", "UV_PYTHON": sys.executable}
    if log is not None:
        e["PYPI_STUB_LOG"] = str(log)
    return e


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *HOOKS_OFF, *args], cwd=cwd, env=env(),
                         capture_output=True, text=True, check=True, timeout=60)
    return out.stdout


def build_system() -> str:
    """The real [build-system] table, so an offline build finds its backend cached."""
    text = (ROOT / "pyproject.toml").read_text()
    start = text.index("[build-system]")
    end = text.find("\n[", start + 1)
    return text[start:] if end == -1 else text[start:end]


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    """A one-commit `sessiontools` at VERSION, an empty bare PUBLIC_REMOTE, a stub kit."""
    public = tmp_path / "public.git"
    work = tmp_path / "work"
    kit = tmp_path / "kit"
    subprocess.run(["git", *HOOKS_OFF, "init", "-q", "--bare", str(public)],
                   env=env(), check=True, timeout=60)
    (work / "src" / "sessiontools").mkdir(parents=True)
    (work / "docs").mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "pyproject.toml").write_text(
        f'[project]\nname = "sessiontools"\nversion = "{VERSION}"\n'
        f'readme = "README.md"\nrequires-python = ">=3.11"\n\n{build_system()}\n')
    (work / "README.md").write_text("# sessiontools\n\nSee [the guide][guide].\n\n"
                                    "[guide]: docs/guide.md\n")
    (work / "docs" / "guide.md").write_text("# guide\n")
    (work / "src" / "sessiontools" / "__init__.py").write_text("")
    git(work, "add", "--", ".")
    git(work, "commit", "-q", "-m", "init")
    (kit / "scripts").mkdir(parents=True)
    (kit / "scripts" / "pypi_readme.py").write_text(STUB)
    return {"work": work, "public": public, "kit": kit, "log": tmp_path / "calls.jsonl"}


def make_pypi_dist(r: dict[str, Path], **overrides: str) -> subprocess.CompletedProcess[str]:
    args = {"PUBLIC_REMOTE": str(r["public"]), "KIT_DIR": str(r["kit"])} | overrides
    return subprocess.run(
        ["make", "--no-print-directory", "-f", str(MAKEFILE), "pypi-dist",
         *(f"{k}={v}" for k, v in args.items())],
        cwd=r["work"], env=env(r["log"]), capture_output=True, text=True,
        timeout=300, check=False)


def calls(r: dict[str, Path]) -> list[list[str]]:
    log = r["log"]
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def refused(out: subprocess.CompletedProcess[str], r: dict[str, Path]) -> None:
    """A refusal says pypi-dist refused, builds nothing, and never rewrites or checks.

    The kit's `tag-check` is the one call a refusal may make: it IS the check.
    """
    assert out.returncode != 0, out.stdout + out.stderr
    assert any(line.startswith("pypi-dist: ") for line in out.stderr.splitlines()), out.stderr
    assert not (r["work"] / "dist").exists(), "a refused pypi-dist built something"
    assert [c[0] for c in calls(r)] in ([], ["tag-check"]), calls(r)


def move_the_local_tag(work: Path) -> tuple[str, str]:
    """Re-point TAG at a new commit after it was pushed: (public sha, local sha)."""
    public = git(work, "rev-parse", f"{TAG}^{{commit}}").strip()
    (work / "docs" / "guide.md").write_text("# guide, after the tag\n")
    git(work, "commit", "-q", "-am", "after the tag")
    git(work, "tag", "-f", "-a", TAG, "-m", TAG)
    local = git(work, "rev-parse", f"{TAG}^{{commit}}").strip()
    assert local != public
    return public, local


# --- static: the release path and the one script ------------------------------


def test_publish_depends_on_pypi_dist_not_build():
    assert recipe("publish").splitlines()[0].startswith("publish: pypi-dist "), recipe("publish")


def test_pypi_dist_is_phony_and_listed():
    phony = [line for line in MAKEFILE.read_text().splitlines() if line.startswith(".PHONY:")]
    assert any("pypi-dist" in line.split() for line in phony), phony
    assert "## " in recipe("pypi-dist").splitlines()[0], "pypi-dist has no help line"


def test_the_script_is_the_kits_never_a_copy():
    """PYPI_README runs the kit's one script; sessiontools carries no fork of it."""
    text = MAKEFILE.read_text()
    assert "KIT_DIR ?= ../sessionkit" in text
    assert "PYPI_README ?= python3 $(KIT_DIR)/scripts/pypi_readme.py" in text
    assert not list(ROOT.rglob("pypi_readme.py")), "a copy of the kit's script is in the repo"


def test_the_public_tag_is_checked_by_the_kits_tag_check():
    """SK-289's verb, not a bare existence check: same commit here and public (ST-323)."""
    body = recipe("pypi-dist")
    assert '$(PYPI_README) tag-check --tag "$$v" --remote "$(PUBLIC_REMOTE)"' in body
    assert "ls-remote" not in body, "the bare ls-remote existence check is back"


def test_the_recipe_pins_this_repo_and_checks_both_dists():
    body = recipe("pypi-dist")
    assert "--repo sessionkit/sessiontools" in body
    assert 'git archive "$$v"' in body
    assert '"$$dist"-*.whl "$$dist".tar.gz' in body
    assert "PUBLIC_REMOTE ?= https://github.com/sessionkit/sessiontools.git" in MAKEFILE.read_text()


# --- behaviour: the real Makefile against a bare PUBLIC_REMOTE in tmp_path ----


@needs_make_uv
def test_refuses_when_the_kit_script_is_absent(repo):
    git(repo["work"], "tag", "-a", TAG, "-m", TAG)
    git(repo["work"], "push", "-q", str(repo["public"]), TAG)
    missing = repo["work"].parent / "no-kit"
    out = make_pypi_dist(repo, KIT_DIR=str(missing))
    refused(out, repo)
    assert f"the kit's README rewriter is not at {missing}/scripts/pypi_readme.py" in out.stderr
    assert "KIT_DIR=/path/to/sessionkit" in out.stderr


@needs_make_uv
def test_the_kit_check_comes_first(repo):
    """No kit and no public tag: the kit is what it names, before any tag check."""
    out = make_pypi_dist(repo, KIT_DIR=str(repo["work"].parent / "no-kit"))
    refused(out, repo)
    assert "README rewriter is not at" in out.stderr
    assert "is not on" not in out.stderr


@needs_make_uv
def test_refuses_when_the_tag_is_not_on_public_remote(repo):
    git(repo["work"], "tag", "-a", TAG, "-m", TAG)  # here, but never pushed
    out = make_pypi_dist(repo)
    refused(out, repo)
    assert f"tag {TAG} is not on {repo['public']}" in out.stderr, out.stderr


@needs_make_uv
def test_refuses_when_the_local_tag_is_not_the_public_one(repo):
    """The tag is on PUBLIC_REMOTE, but moved here since: the links would name
    one commit and the dist would be built from another (ST-323, SK-289)."""
    work = repo["work"]
    git(work, "tag", "-a", TAG, "-m", TAG)
    git(work, "push", "-q", str(repo["public"]), TAG)
    public, local = move_the_local_tag(work)
    out = make_pypi_dist(repo)
    refused(out, repo)
    assert f"local {local}, public {public}" in out.stderr, out.stderr


@needs_make_uv
def test_builds_from_the_tag_rewrites_and_checks_both_dists(repo):
    work = repo["work"]
    git(work, "tag", "-a", TAG, "-m", TAG)
    git(work, "push", "-q", str(repo["public"]), TAG)
    # A change after the tag must not reach the dist: it is built from the tag.
    (work / "README.md").write_text("# not the tag\n\n[x]: docs/nowhere.md\n")
    out = make_pypi_dist(repo)
    assert out.returncode == 0, out.stdout + out.stderr

    tag_check, rewrite, check = calls(repo)
    assert tag_check == ["tag-check", "--tag", TAG, "--remote", str(repo["public"])]
    assert rewrite[:7] == ["rewrite", "--repo", "sessionkit/sessiontools", "--ref", TAG,
                           "--root", rewrite[6]]
    stage = rewrite[6]
    assert rewrite[7] == f"{stage}/README.md"
    assert not Path(stage).exists(), "the scratch stage was left behind"
    assert check == ["check", f"dist/{WHEEL}", f"dist/{SDIST}"]

    # Built from pieces: a whole URL into this repo would trip test_shipped_links.
    url = "https://github.com/sessionkit/" + f"sessiontools/blob/{TAG}/docs/guide.md"
    with zipfile.ZipFile(work / "dist" / WHEEL) as whl:
        meta = whl.read(f"sessiontools-{VERSION}.dist-info/METADATA").decode()
    with tarfile.open(work / "dist" / SDIST) as sdist:
        pkg = sdist.extractfile(f"sessiontools-{VERSION}/PKG-INFO").read().decode()
    for desc in (meta, pkg):
        assert f"[guide]: {url}" in desc
        assert "not the tag" not in desc
    # The checkout's own README is never touched.
    assert "not the tag" in (work / "README.md").read_text()


def kit_script() -> Path | None:
    """The real kit script: $KIT_DIR's, else the sibling ../sessionkit's."""
    base = os.environ.get("KIT_DIR") or str(ROOT.parent / "sessionkit")
    path = Path(base) / "scripts" / "pypi_readme.py"
    return path if path.is_file() else None


@needs_make_uv
@pytest.mark.skipif(kit_script() is None, reason="no sessionkit checkout with scripts/pypi_readme.py")
def test_end_to_end_with_the_real_kit_script(repo):
    work = repo["work"]
    git(work, "tag", "-a", TAG, "-m", TAG)
    git(work, "push", "-q", str(repo["public"]), TAG)
    out = make_pypi_dist(repo, KIT_DIR=str(kit_script().parent.parent))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 link(s) rewritten" in out.stdout + out.stderr
    assert (work / "dist" / WHEEL).is_file() and (work / "dist" / SDIST).is_file()


@needs_make_uv
@pytest.mark.skipif(kit_script() is None, reason="no sessionkit checkout with scripts/pypi_readme.py")
def test_the_real_kit_refuses_a_moved_tag(repo):
    """The kit's own tag-check (SK-289) refuses the mismatch and names both shas."""
    work = repo["work"]
    git(work, "tag", "-a", TAG, "-m", TAG)
    git(work, "push", "-q", str(repo["public"]), TAG)
    public, local = move_the_local_tag(work)
    out = make_pypi_dist(repo, KIT_DIR=str(kit_script().parent.parent))
    assert out.returncode != 0, out.stdout + out.stderr
    assert f"local {local}, public {public}" in out.stderr, out.stderr
    assert any(line.startswith("pypi-dist: ") for line in out.stderr.splitlines()), out.stderr
    assert not (work / "dist").exists(), "a refused pypi-dist built something"
