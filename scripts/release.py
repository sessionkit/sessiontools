#!/usr/bin/env python3
"""Cut a sessiontools release. The ONE version lives in pyproject.toml; uv bumps it.

    python3 scripts/release.py patch|minor|major     bump, then release
    python3 scripts/release.py X.Y.Z                 set, then release
    python3 scripts/release.py --current             print the version
    ... --dry-run                                    say what would happen

Steps: work out the new version (uv version --frozen --dry-run, which writes
nothing) and refuse an existing vX.Y.Z tag -> check uv.lock's root block, and
work out the edits to .claude-plugin/plugin.json and marketplace.json (when
they exist) and to CHANGELOG.md -> uv version --frozen (bump or set) ->
record the new version on uv.lock's own root line (textual) -> mirror it into
the manifests (textual, formatting kept) -> stamp CHANGELOG.md's [Unreleased]
as [X.Y.Z] - date, with a fresh [Unreleased] above -> commit "chore: release
vX.Y.Z", the files by name -> annotated tag vX.Y.Z. Never pushes: it prints
the push line. Refuses a dirty tree, a missing CHANGELOG, a missing
[Unreleased], a manifest with no version field, an existing tag, and a
uv.lock without exactly one root sessiontools block holding one version line.

EVERY REFUSAL COMES BEFORE THE FIRST WRITE. The tag check runs on the
version a --dry-run works out, and every other file's edit is worked out
before `uv version` writes, so a refused run (a re-run of a cut that already
happened, a changelog with no [Unreleased]) leaves `git status` clean, not a
bumped pyproject and uv.lock. One stop comes after it: the real `uv version`
writing a different version from the dry run's, which should not happen.

A RELEASE NEVER RE-LOCKS. A plain `uv version X` (or --bump) is a
project write: it re-locks against the index, and this script commits
uv.lock. Cut before the deps are on PyPI, that fails (unsatisfiable); with
UV_FIND_LINKS set, it writes a local-path lock and the release commits it.
So `uv version` always runs with --frozen, which writes pyproject only and
resolves nothing, on the dry path too, so a rehearsal runs the same uv call
as the release. --frozen leaves uv.lock's own `[[package]] name =
"sessiontools"` version line on the old version, and the next `uv run` would
rewrite it, so the script edits that one line as text, as mirror() edits the
manifests. The result is the bytes uv's own re-lock writes when the deps are
unchanged, so `uv lock --check` passes after a cut. The lock is checked
BEFORE `uv version` writes anything, so a refusal leaves pyproject
untouched. It is stdlib-only, and `make release` runs it with python3, never
a re-locking `uv run`.

By design: version in pyproject, release mirrors; sessionmail's script (version in plugin.json) is the ancestor.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

PLUGIN = Path(".claude-plugin/plugin.json")
MARKET = Path(".claude-plugin/marketplace.json")
CHANGELOG = Path("CHANGELOG.md")
LOCK = Path("uv.lock")
PYPROJECT = Path("pyproject.toml")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def sh(*args: str, dry: bool = False, capture: bool = False) -> str:
    print("  $", " ".join(args))
    if dry and not capture:
        return ""
    out = subprocess.run(args, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        sys.exit(f"release: {args[0]} failed: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def current() -> str:
    return sh("uv", "version", "--short", capture=True)


def bump(spec: str, dry: bool) -> str:
    if spec not in ("patch", "minor", "major") and not SEMVER.match(spec):
        sys.exit(f"release: '{spec}' is not patch|minor|major or X.Y.Z")
    args = ["uv", "version", "--bump", spec] if spec in ("patch", "minor", "major") else ["uv", "version", spec]
    args.append("--frozen")  # never re-lock: see the docstring
    if dry:
        args.append("--dry-run")
    out = sh(*args, capture=True)  # "name old => new"
    return out.split("=>")[-1].strip()


def lock_root(text: str, name: str) -> tuple[int, int]:
    """The (start, end) span of the version VALUE on uv.lock's root line.

    The root is the one [[package]] block named `name` whose source is the
    project itself (editable or virtual "."), and it must hold exactly one
    top-level `version = "..."` line. Anything else is refused: no guess, no
    write.
    """
    starts = [m.start() for m in re.finditer(r"^\[\[package\]\]$", text, re.MULTILINE)]
    roots = []
    for i, start in enumerate(starts):
        block = text[start : starts[i + 1] if i + 1 < len(starts) else len(text)]
        if re.search(rf'^name = "{re.escape(name)}"$', block, re.MULTILINE) and re.search(
            r'^source = \{ (?:editable|virtual) = "\." \}$', block, re.MULTILINE
        ):
            roots.append((start, block))
    if len(roots) != 1:
        sys.exit(
            f"release: {LOCK} has {len(roots)} root [[package]] blocks for {name} (editable or virtual "
            f'source "."), not one - refusing; nothing was written'
        )
    start, block = roots[0]
    versions = list(re.finditer(r'^version = "([^"]*)"$', block, re.MULTILINE))
    if len(versions) != 1:
        sys.exit(
            f"release: {LOCK}'s root {name} block has {len(versions)} version lines, not one - refusing; "
            "nothing was written"
        )
    return start + versions[0].start(1), start + versions[0].end(1)


def set_lock_root(text: str, span: tuple[int, int], version: str, dry: bool) -> None:
    """Write `version` onto the root line found by lock_root(), and nothing else."""
    old = text[span[0] : span[1]]
    print(f"  ~ {LOCK}: root version {old} -> {version} (textual; nothing resolved)")
    if not dry:
        LOCK.write_text(text[: span[0]] + version + text[span[1] :])


def mirror(path: Path, version: str) -> tuple[str, str] | None:
    """The manifest's new text and its report line, or None when it is absent.

    Works it out and writes nothing, so a refusal comes before any write."""
    if not path.exists():
        return None
    text = path.read_text()
    new, n = re.subn(r'("version"\s*:\s*")[^"]+(")', r"\g<1>" + version + r"\g<2>", text, count=1)
    if n != 1:
        sys.exit(f"release: no version field in {path} - refusing; nothing was written")
    return new, f"  ~ {path}: version -> {version}"


def stamp(version: str) -> tuple[str, str]:
    """CHANGELOG.md's new text and its report line. Works it out, writes nothing."""
    if not CHANGELOG.exists():
        sys.exit("release: CHANGELOG.md not found - a release without a changelog is refused; nothing was written")
    text = CHANGELOG.read_text()
    if "## [Unreleased]" not in text:
        sys.exit("release: CHANGELOG.md has no '## [Unreleased]' section - nothing to stamp; nothing was written")
    today = datetime.now().astimezone().date().isoformat()
    new = text.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{version}] - {today}", 1)
    return new, f"  ~ CHANGELOG.md: [Unreleased] -> [{version}] - {today}"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    if args == ["--current"]:
        print(current())
        return
    if len(args) != 1:
        sys.exit(__doc__)
    if subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=False).stdout.strip():
        sys.exit("release: the working tree is not clean - commit or stash first")
    # Every check before the first write: the version a dry run works out, its
    # tag, the lock, the manifests, the changelog. Only then does `uv version`
    # touch pyproject.
    version = bump(args[0], dry=True)
    tag = f"v{version}"
    if subprocess.run(["git", "tag", "-l", tag], capture_output=True, text=True, check=False).stdout.strip():
        sys.exit(f"release: tag {tag} already exists - refusing; nothing was written")
    if not LOCK.exists():
        sys.exit(f"release: {LOCK} not found - the release records its version there; nothing was written")
    lock = LOCK.read_text()
    span = lock_root(lock, tomllib.loads(PYPROJECT.read_text())["project"]["name"])
    edits = [(p, m) for p in (PLUGIN, MARKET) if (m := mirror(p, version)) is not None]
    edits.append((CHANGELOG, stamp(version)))
    if not dry:
        written = bump(args[0], dry=False)
        if written != version:
            sys.exit(f"release: uv version wrote {written}, but the dry run said {version} - stopping")
    set_lock_root(lock, span, version, dry)
    for path, (new, report) in edits:
        print(report)
        if not dry:
            path.write_text(new)
    files = [str(PYPROJECT), str(LOCK), *(str(p) for p, _ in edits)]
    sh("git", "add", "--", *files, dry=dry)
    sh("git", "commit", "-m", f"chore: release {tag}", dry=dry)
    sh("git", "tag", "-a", tag, "-m", f"sessiontools {tag}", dry=dry)
    print(f"{'would release' if dry else 'released'} {tag}. Push is yours:")
    print(f"  {push_line(tag)}")


def push_line(tag: str) -> str:
    """The push to run after a release: the branch, then THIS tag by name.

    Never `--tags`: that sends every local tag, the ready/ and approved/ merge-queue
    tags among them. `make push` runs the same two pushes.
    """
    return f"git push origin HEAD && git push origin {tag}"


if __name__ == "__main__":
    main()
