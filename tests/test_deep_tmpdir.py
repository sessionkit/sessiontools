"""The suite holds under a deep TMPDIR (ST-119).

A Claude scratchpad TMPDIR runs past 200 bytes. Seven tests built a transcript
dir as `encode_dir(str(tmp_path))` - the whole tmp path flattened into ONE
component - or bounded a pointer that embeds tmp_path at 400 characters, so
under such a TMPDIR they failed: ENAMETOOLONG (one component over NAME_MAX,
255 bytes) or a length assert. They are run here, in a child pytest, under a
TMPDIR deeper than the one that broke them, so the fix cannot quietly undo.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from test_listing import CWD

from sessiontools import window

ROOT = Path(__file__).resolve().parent.parent
NAME_MAX = 255
#: The seven that failed under a 216-byte TMPDIR (ST-110 run4, ST-119 repro).
ONCE_BROKEN = (
    "tests/test_listing.py::test_with_context_fills_the_keys_and_the_column",
    "tests/test_listing.py::test_session_list_fills_context_unless_told_not_to",
    "tests/test_listing.py::test_session_context_reports_a_live_seat_like_the_old_tool",
    "tests/test_listing.py::test_get_model_reads_the_transcript",
    "tests/test_snapshot.py::test_the_live_verbs_never_open_the_snapshot",
    "tests/test_start_vendor.py::test_long_initial_message_goes_to_a_planned_0600_file",
)
#: Deeper than the 216 bytes that broke them; every component well under NAME_MAX.
DEPTH = 240


def deep_dir(base: Path, depth: int = DEPTH) -> Path:
    """A fresh directory at least one level under BASE whose path is at least
    DEPTH bytes long, built from components of 1 to 60 bytes. BASE may already
    be deeper than DEPTH: the outer run may itself be under a deep TMPDIR."""
    d = base / "d"
    while len(os.fsencode(d)) < depth:
        d = d / ("d" * min(60, depth - len(os.fsencode(d))))
    d.mkdir(parents=True)
    return d


def test_deep_dir_is_deep_and_every_added_component_fits(tmp_path):
    d = deep_dir(tmp_path)
    assert d.is_dir() and len(os.fsencode(d)) >= DEPTH
    added = d.relative_to(tmp_path).parts
    assert added and all(0 < len(part) <= 60 for part in added)
    assert deep_dir(d, depth=10) == d / "d"          # a base already deep: one level, fresh


def test_the_fleet_transcript_dir_is_one_short_component():
    name = window.encode_dir(CWD)
    assert "/" not in name and len(os.fsencode(name)) < NAME_MAX


def test_the_once_broken_tests_pass_under_a_deep_tmpdir(tmp_path):
    deep = deep_dir(tmp_path / "t")
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_DEBUG_TEMPROOT"}
    env["TMPDIR"] = str(deep)
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *ONCE_BROKEN],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False)
    tail = run.stdout[-3000:] + run.stderr[-2000:]
    assert run.returncode == 0, tail
    assert "7 passed" in run.stdout, tail          # 5 + 1 + 1: the parametrized one is two
    # the child's tmp_path really lived under the deep TMPDIR, not a default
    assert list(deep.glob("pytest-of-*/pytest-*/test_with_context_fills*")), tail
