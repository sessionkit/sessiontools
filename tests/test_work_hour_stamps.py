"""No work-hour stamp in the release tree: a gate on the public release.

The public repo is built from `git archive`, so what that archive
writes is what ships. A dated weekday stamp whose time of day starts inside
working hours (09:00 up to, not including, 17:00) says when someone was at
work; the release keeps the date and drops the time. This guard runs the
same measure over `git archive HEAD` and fails with `path:line` for each
hit, so a new stamp cannot reopen it.

STAMP_RE is copied VERBATIM from the release tooling's `work-hour-stamps`
(the release scrub's own regex); `start_minute` is that tool's `start()`. The
tool lives outside this repo, so the port lives here. The stamps the tests
below plant are built at run time, so this file never matches its own
regex.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env

ROOT = Path(__file__).resolve().parents[1]

# --- the measure, verbatim -------------------------------------------------------------

STAMP_RE = re.compile(r"[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?(-[0-9]{1,2}:[0-9]{2})?"
                      r" (Mon|Tue|Wed|Thu|Fri) 20[0-9]{2}-[0-9]{2}-[0-9]{2}")

#: Working hours, in minutes after midnight: [WORK_START, WORK_END).
WORK_START, WORK_END = 540, 1020


def start_minute(m: re.Match) -> int:
    """The stamp's START time in minutes: a range counts from its first time."""
    hh, mm = m.group(0).split(" ", 1)[0].split("-", 1)[0].split(":")[:2]
    return int(hh) * 60 + int(mm)


def work_hour_lines(text: str) -> list[int]:
    """1-based line numbers holding at least one work-hour stamp."""
    return [n for n, line in enumerate(text.splitlines(), 1)
            if any(WORK_START <= start_minute(m) < WORK_END for m in STAMP_RE.finditer(line))]


def archive_hits(repo: Path, ref: str = "HEAD") -> list[str]:
    """`path:line` for every work-hour stamp in `git archive REF`, as the tool
    reads it: regular files only, a NUL in the first 8 KiB skips the file as
    binary, export-ignored paths are not in the archive at all."""
    data = subprocess.run(["git", *HOOKS_OFF, "-C", str(repo), "archive", ref],
                          check=True, capture_output=True, env=hermetic_env()).stdout
    hits = []
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            raw = tar.extractfile(member).read()
            if b"\0" in raw[:8192]:
                continue
            text = raw.decode("utf8", "replace")
            hits += [f"{member.name}:{n}" for n in work_hour_lines(text)]
    return hits


def stamp(clock: str, day: str, date: str = "2030-12-10") -> str:
    """A stamp built at run time from its parts (see the module docstring)."""
    return f"{clock} {day} {date}"


def _is_repo_root(path: Path) -> bool:
    if shutil.which("git") is None:
        return False
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, env=hermetic_env(), check=False)
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == path


# --- the guard -------------------------------------------------------------------------

@pytest.mark.skipif(not _is_repo_root(ROOT), reason="needs this repo's git checkout")
def test_the_release_tree_has_no_work_hour_stamp():
    hits = archive_hits(ROOT)
    assert hits == [], ("work-hour stamps in `git archive HEAD` (drop the time of day, "
                        "keep the date): " + ", ".join(hits))


# --- it bites: the measure itself --------------------------------------------------------

@pytest.mark.parametrize("clock", ["9:00", "09:00", "12:30", "16:59", "10:15:30", "13:02-13:05"])
@pytest.mark.parametrize("day", ["Mon", "Tue", "Wed", "Thu", "Fri"])
def test_an_in_hours_weekday_stamp_is_a_hit(clock, day):
    assert work_hour_lines("before\n(noted " + stamp(clock, day) + ")\n") == [2]


@pytest.mark.parametrize("clock", ["08:59", "17:00", "23:40", "00:12:29", "08:30-09:30"])
def test_an_out_of_hours_weekday_stamp_passes(clock):
    assert work_hour_lines(stamp(clock, "Tue")) == []


@pytest.mark.parametrize("day", ["Sat", "Sun"])
def test_a_weekend_stamp_passes_at_any_hour(day):
    assert work_hour_lines(stamp("12:30", day)) == []


def test_a_stripped_stamp_passes():
    """The fix the guard asks for: the date and day stay, the time goes."""
    assert work_hour_lines(stamp("12:30", "Tue").split(" ", 1)[1]) == []


def test_this_file_does_not_trip_itself():
    assert work_hour_lines(Path(__file__).read_text()) == []


# --- it bites: through a real archive ------------------------------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_archive_hits_names_path_and_line_and_honours_export_ignore(tmp_path):
    env = hermetic_env()

    def git(*args: str) -> None:
        subprocess.run(["git", *HOOKS_OFF, "-C", str(tmp_path), *args],
                       check=True, capture_output=True, env=env)

    git("init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text(
        "clean\n" + stamp("08:15", "Wed") + "\n" + stamp("14:05", "Thu") + "\n")
    (tmp_path / "weekend.md").write_text(stamp("11:00", "Sat") + "\n")
    (tmp_path / "diary.md").write_text(stamp("10:00", "Mon") + "\n")
    (tmp_path / "blob.bin").write_bytes(b"\0" + stamp("10:00", "Mon").encode())
    (tmp_path / ".gitattributes").write_text("/diary.md export-ignore\n")
    git("add", "-A")
    git("commit", "-q", "-m", "fixture")
    assert archive_hits(tmp_path) == ["docs/notes.md:3"]
