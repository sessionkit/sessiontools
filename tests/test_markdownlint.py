"""CHANGELOG.md passes markdownlint at the repo root (ST-117).

The global pre-commit hook lints every staged markdown file, merging the
repo-root `.markdownlint.json` over the global config. Keep a Changelog
repeats `### Added` under every version (MD024 `siblings_only`), so a lint
failure here means a CHANGELOG commit would be refused.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_changelog_passes_markdownlint():
    markdownlint = shutil.which("markdownlint")
    if markdownlint is None:
        pytest.skip("markdownlint not installed")
    result = subprocess.run([markdownlint, "CHANGELOG.md"], cwd=ROOT,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
