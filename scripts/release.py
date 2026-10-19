#!/usr/bin/env python3
"""Cut a sessiontools release. The ONE version lives in pyproject.toml; uv bumps it.

    python3 scripts/release.py patch|minor|major     bump, then release
    python3 scripts/release.py X.Y.Z                 set, then release
    python3 scripts/release.py --current             print the version
    ... --dry-run                                    say what would happen

Steps: uv version (bump or set) -> mirror it into .claude-plugin/plugin.json
and marketplace.json when they exist (textual, formatting kept) -> stamp
CHANGELOG.md's [Unreleased] as [X.Y.Z] - date, with a fresh [Unreleased]
above -> commit "chore: release vX.Y.Z", the files by name -> annotated tag
vX.Y.Z. Never pushes: it prints the push line. Refuses a dirty tree, a
missing CHANGELOG, a missing [Unreleased], an existing tag.

By design: version in pyproject, release mirrors; sessionmail's script (version in plugin.json) is the ancestor.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PLUGIN = Path(".claude-plugin/plugin.json")
MARKET = Path(".claude-plugin/marketplace.json")
CHANGELOG = Path("CHANGELOG.md")
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
    if dry:
        args.append("--dry-run")
    out = sh(*args, capture=True)  # "name old => new"
    return out.split("=>")[-1].strip()


def mirror(path: Path, version: str, dry: bool) -> bool:
    if not path.exists():
        return False
    text = path.read_text()
    new, n = re.subn(r'("version"\s*:\s*")[^"]+(")', r"\g<1>" + version + r"\g<2>", text, count=1)
    if n != 1:
        sys.exit(f"release: no version field in {path}")
    print(f"  ~ {path}: version -> {version}")
    if not dry:
        path.write_text(new)
    return True


def stamp(version: str, dry: bool) -> None:
    if not CHANGELOG.exists():
        sys.exit("release: CHANGELOG.md not found - a release without a changelog is refused")
    text = CHANGELOG.read_text()
    if "## [Unreleased]" not in text:
        sys.exit("release: CHANGELOG.md has no '## [Unreleased]' section - nothing to stamp")
    today = datetime.now().astimezone().date().isoformat()
    new = text.replace("## [Unreleased]", f"## [Unreleased]\n\n## [{version}] - {today}", 1)
    print(f"  ~ CHANGELOG.md: [Unreleased] -> [{version}] - {today}")
    if not dry:
        CHANGELOG.write_text(new)


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
    version = bump(args[0], dry)
    tag = f"v{version}"
    if subprocess.run(["git", "tag", "-l", tag], capture_output=True, text=True, check=False).stdout.strip():
        sys.exit(f"release: tag {tag} already exists")
    files = ["pyproject.toml", "uv.lock", str(CHANGELOG)]
    for p in (PLUGIN, MARKET):
        if mirror(p, version, dry):
            files.append(str(p))
    stamp(version, dry)
    files = [f for f in files if Path(f).exists()]
    sh("git", "add", "--", *files, dry=dry)
    sh("git", "commit", "-m", f"chore: release {tag}", dry=dry)
    sh("git", "tag", "-a", tag, "-m", f"sessiontools {tag}", dry=dry)
    print(f"{'would release' if dry else 'released'} {tag}. Push is yours:")
    print("  git push && git push --tags")


if __name__ == "__main__":
    main()
