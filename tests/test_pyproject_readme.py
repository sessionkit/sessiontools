"""pyproject.toml names the README, so the package page is never blank.

Without `readme`, the build backend writes no `Description` or
`Description-Content-Type` into the wheel's METADATA or the sdist's PKG-INFO,
`twine check --strict` fails on both, and the index page for that version is
empty for good: published metadata cannot be changed after upload."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_table() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_pyproject_names_the_readme():
    assert project_table().get("readme") == "README.md"


def test_the_named_readme_exists_and_is_markdown():
    readme = ROOT / project_table()["readme"]
    assert readme.is_file()
    assert readme.suffix == ".md"
    assert readme.read_text().strip(), "README.md is empty"
