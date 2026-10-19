"""The wheel, built offline, for the tests that read what it ships.

`uv build --offline` needs `uv` on the PATH and, offline, an interpreter
for the checkout's `.python-version` that uv can find. A host that has uv
but not that interpreter (an empty HOME and a system-only PATH, say) cannot
build the wheel at all. That is a missing prerequisite, not a packaging
fault, so each test that needs the wheel skips LOUDLY with uv's own words,
and runs for real wherever the build works.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

#: A cold build is well under this; it only bounds a hung one.
TIMEOUT_S = 120


def build_wheel_offline(repo: Path, dest: Path) -> Path:
    """`uv build --wheel --offline` of REPO into DEST; the one wheel it made.
    Skips, naming why, when uv is missing or cannot build offline."""
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("no uv")
    r = subprocess.run([uv, "build", "--wheel", "--offline", "-o", str(dest), str(repo)],
                       capture_output=True, text=True, check=False, timeout=TIMEOUT_S)
    if r.returncode != 0:
        pytest.skip(f"uv build unavailable offline: {r.stderr.strip()[-200:]}")
    (wheel,) = Path(dest).glob("*.whl")
    return wheel
