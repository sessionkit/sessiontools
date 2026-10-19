"""context_parts' guards (ST-26 do-001): the committed fixtures are what
build.py writes; the fold is a new module that window.py and settle.py never
import, so the settle hook path cannot pull it in (design section 5's
Wed 2030-08-14 amendment, which replaced the 100 ms settle latency gate); and
the package is stdlib-only.

No guard here diffs a file against origin/master: that pins which files THIS
branch touched, and would fail every later task that edits them (do-004
review F3). What is pinned is the meaning: the dependency points one way."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "context_parts"
SETTLE_PATH = ("src/sessiontools/window.py", "src/sessiontools/settle.py")


def test_the_committed_fixtures_are_what_build_py_writes(tmp_path):
    out = subprocess.run([sys.executable, str(FIXTURES / "build.py"), str(tmp_path)],
                         capture_output=True, text=True, check=True)
    built = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    assert built == sorted(p.name for p in FIXTURES.glob("*.jsonl"))
    assert len(out.stdout.splitlines()) == len(built)
    for name in built:
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes(), (
            f"{name} differs from build.py's output: re-run build.py and commit")


def test_window_and_settle_never_import_context_parts():
    for rel in SETTLE_PATH:
        assert "context_parts" not in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_context_parts_imports_only_the_standard_library():
    code = ("import sys; before = set(sys.modules); "
            "import sessiontools.context_parts; "
            "new = {m.split('.')[0] for m in set(sys.modules) - before}; "
            "print(sorted(m for m in new if m != 'sessiontools' "
            "and m not in sys.stdlib_module_names))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == "[]"
