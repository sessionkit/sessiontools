"""`session account status` reads the limits file when the exporter has no
answer.

The same walls as `test_account_status`: its autouse fixture is imported, so
no test runs `security`, `pass` or `claude`, or reaches a real exporter.
Every file lives in the test's tmp HOME; the setting is written into that
HOME's `session` settings layer; the clock is a fixed seam.
"""

from __future__ import annotations

import io
import json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ANCHOR
from settings_support import host
from test_account_status import (  # noqa: F401 - the autouse walls
    World,
    _no_keychain_no_pass_no_claude_no_exporter,
    make_world,
    synthetic_token,
)

from sessiontools import settings
from sessiontools.account import cli
from sessiontools.account import limits as lm
from sessiontools.account import status as st

NOW = ANCHOR.timestamp()
FIVE_RESET = NOW + 2 * 3600
WEEK_RESET = NOW + 3 * 86400


def clock() -> datetime:
    return datetime.fromtimestamp(NOW, UTC)


def gauge(*, age: float = 120, five: object = 3, week: object = 1,
          five_reset: object = FIVE_RESET, week_reset: object = WEEK_RESET,
          **extra) -> dict:
    """A limits file as the tap writes it, AGE seconds old at NOW. A window
    given as None is left out."""
    data: dict = {}
    if five is not None:
        data["five_hour"] = {"used_percentage": five, "resets_at": five_reset}
    if week is not None:
        data["seven_day"] = {"used_percentage": week, "resets_at": week_reset}
    data.update({"written_at": NOW - age, "account": "default"})
    data.update(extra)
    return data


class Gauge:
    """A World plus the limits file and the `session` setting naming it."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.path = world.home / "gauge" / "limits.json"
        self.path.parent.mkdir()

    def write(self, data) -> Path:
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))
        self.path.chmod(0o600)
        return self.path

    def setting(self, value: object = None, *, name: str = "session.json",
                folder: Path | None = None) -> Path:
        folder = host(self.world.home, "session") if folder is None else folder
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        value = str(self.path) if value is None else value
        target.write_text(json.dumps({"account": {"limits_file": value}}))
        return target

    def status(self, **overrides) -> int:
        overrides.setdefault("clock", clock)
        return self.world.status(**overrides)

    def json(self, **overrides) -> dict:
        overrides.setdefault("clock", clock)
        self.world.out = io.StringIO()
        return self.world.json(**overrides)

    def line(self, **overrides) -> str:
        self.world.out = io.StringIO()
        assert self.status(**overrides) == 0, self.world.err.getvalue()
        return self.world.out.getvalue().splitlines()[2]


@pytest.fixture
def down(tmp_path) -> Gauge:
    """The exporter is unreachable."""
    return Gauge(make_world(tmp_path, metrics=None))


NO_EXPORTER = f"no exporter at {st.USAGE_URL}"
#: Resets are shown by default; `--no-resets` hides them, and a window with no reset
#: time prints no clause.
R5 = f" (resets {st._iso(FIVE_RESET)})"
R7 = f" (resets {st._iso(WEEK_RESET)})"
FILE_LINE = f"usage: 5h 3%{R5}, 7d 1%{R7}  (usage of the host account, limits file, 2m old)"
FILE_LINE_NO_RESETS = "usage: 5h 3%, 7d 1%  (usage of the host account, limits file, 2m old)"


# --- 1. unset: today's output ----------------------------------------------------------


def test_unset_is_todays_text_and_json_adds_only_the_reason(down, capsys):
    down.write(gauge())                         # a file that nobody names is not read
    assert down.line() == f"usage: unavailable ({NO_EXPORTER})"
    data = down.json()
    assert list(data) == ["host", "seat", "usage", "usage_unavailable", "profile"]
    assert data["host"]["via"] == "Keychain, fp match"
    assert data["seat"] == {"name": data["host"]["name"], "via": "follows the host"}
    assert data["usage"] is None and data["usage_unavailable"] == NO_EXPORTER
    down.world.assert_no_leak(capsys)


def test_unset_with_other_session_settings_is_todays_output(down, capsys):
    folder = host(down.world.home, "session")
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"account": {"passage_prefix": "x/y"}}))
    assert down.line() == f"usage: unavailable ({NO_EXPORTER})"
    down.world.assert_no_leak(capsys)


# --- 2. the file answers ---------------------------------------------------------------


def test_the_file_answers_when_the_exporter_is_down(down, capsys):
    down.write(gauge())
    down.setting()
    assert down.line() == FILE_LINE
    data = down.json()
    assert "usage_unavailable" not in data
    assert data["usage"] == {
        "source": "limits_file", "file": str(down.path), "label": "usage of the host account",
        "written_at": st._iso(NOW - 120), "age_seconds": 120, "stale": False,
        "windows": [{"window": "5h", "account": None, "percent": 3.0, "expired": False,
                     "stale": False, "age_seconds": 120, "resets_at": st._iso(FIVE_RESET)},
                    {"window": "7d", "account": None, "percent": 1.0, "expired": False,
                     "stale": False, "age_seconds": 120, "resets_at": st._iso(WEEK_RESET)}]}
    down.world.assert_no_leak(capsys)


def test_a_tilde_path_expands_against_the_home(down, capsys):
    down.write(gauge(five=42.5))
    down.setting("~/gauge/limits.json")
    assert down.line().startswith(f"usage: 5h 42.5%{R5}, 7d 1%{R7}  (usage of the host account")
    assert down.json()["usage"]["file"] == str(down.path)
    down.world.assert_no_leak(capsys)


def test_an_account_field_absent_is_the_hosts(down, capsys):
    data = gauge()
    del data["account"]
    down.write(data)
    down.setting()
    assert down.line() == FILE_LINE
    down.world.assert_no_leak(capsys)


# --- 3, 4. the exporter first ----------------------------------------------------------


def test_the_exporter_answers_first_and_the_file_is_never_opened(tmp_path, monkeypatch,
                                                                 capsys):
    found = Gauge(make_world(tmp_path))                 # the exporter is up
    found.setting("relative/not/even/valid")            # would be refused if read

    def boom(*args, **kwargs):
        raise AssertionError("the limits file was read with the exporter up")

    monkeypatch.setattr(lm, "limits_setting", boom)
    monkeypatch.setattr(lm, "read_limits", boom)
    assert found.line(resets=False) == \
        "usage: 5h 75%, 7d 52.5%  (usage of the live account, age not reported by the exporter)"
    data = found.json()
    assert "usage_unavailable" not in data and "source" not in data["usage"]
    found.world.assert_no_leak(capsys)


def test_an_exporter_with_no_usage_series_falls_back_to_the_file(tmp_path, capsys):
    found = Gauge(make_world(tmp_path, metrics="# nothing\nup 1\n"))
    found.write(gauge())
    found.setting()
    assert found.line() == FILE_LINE
    found.world.assert_no_leak(capsys)


# --- 5, 6, 7. age, expiry, one window --------------------------------------------------


@pytest.mark.parametrize("age, stale, said", [
    (0, False, "0s old"), (59, False, "59s old"), (lm.STALE_AFTER, False, "15m old"),
    (lm.STALE_AFTER + 1, True, "STALE: 15m old"), (16 * 60, True, "STALE: 16m old"),
    (6 * 3600 + 5, True, "STALE: 6h old"), (2 * 86400, True, "STALE: 2d old")])
def test_the_files_age_is_always_said_and_stale_past_the_line(down, age, stale, said,
                                                               capsys):
    down.write(gauge(age=age, five_reset=NOW + 9 * 86400, week_reset=NOW + 9 * 86400))
    down.setting()
    r9 = f" (resets {st._iso(NOW + 9 * 86400)})"
    assert down.line() == \
        f"usage: 5h 3%{r9}, 7d 1%{r9}  (usage of the host account, limits file, {said})"
    usage = down.json()["usage"]
    assert usage["stale"] is stale and usage["age_seconds"] == age
    assert [w["percent"] for w in usage["windows"]] == [3.0, 1.0]     # still shown
    down.world.assert_no_leak(capsys)


def test_a_little_clock_jitter_reads_as_zero_seconds_old(down, capsys):
    down.write(gauge(age=-lm.FUTURE_SLACK))
    down.setting()
    assert down.json()["usage"]["age_seconds"] == 0
    down.world.assert_no_leak(capsys)


def leaves(node: object, at: tuple = ()):
    """Every scalar in parsed JSON, with the key path that reaches it."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, (*at, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaves(value, (*at, index))
    else:
        yield at, node


def test_a_window_whose_reset_has_passed_shows_no_percent(tmp_path, capsys):
    # The HOME sits under a folder named like the pytest basetemp that matched
    # (`pytest-5388`), so every run has the expired percent's digits in its path.
    (tmp_path / "pytest-5388").mkdir()
    down = Gauge(make_world(tmp_path / "pytest-5388", metrics=None))
    down.write(gauge(age=6 * 3600, five=88, five_reset=NOW - 3600))
    down.setting()
    # The expired window reads as the exporter's does: STALE, when it ended, and how
    # long ago; its figure is withheld and it takes no reset clause.
    assert down.line() == (f"usage: 5h STALE: window ended {st._iso(NOW - 3600)} (1h ago), "
                           f"figure not shown, 7d 1%{R7}  "
                           "(usage of the host account, limits file, STALE: 6h old)")
    data = down.json()
    windows = data["usage"]["windows"]
    assert windows[0] == {"window": "5h", "account": None, "percent": None, "expired": True,
                          "stale": True, "age_seconds": 6 * 3600,
                          "resets_at": st._iso(NOW - 3600)}
    assert windows[1]["percent"] == 1.0 and windows[1]["expired"] is False
    # The expired 88 is carried by no field. Checked leaf by leaf, never as a substring
    # of the whole output: `usage.file` is the tmp path, which pytest numbers, so
    # "88" in the text can match the path. A string leaf is checked with HOME
    # taken out, so no path under it can match either.
    assert data["usage"]["file"] == str(down.path)
    home = str(down.world.home)
    carried = [(at, value) for at, value in leaves(data)
               if value == 88 or (isinstance(value, str) and "88" in value.replace(home, "~"))]
    assert carried == []
    down.world.assert_no_leak(capsys)


def test_a_reset_exactly_now_has_passed(down, capsys):
    down.write(gauge(five_reset=NOW))
    down.setting()
    assert down.json()["usage"]["windows"][0]["expired"] is True
    down.world.assert_no_leak(capsys)


@pytest.mark.parametrize("missing", ["five", "week"])
def test_one_window_alone_is_shown_alone(down, missing, capsys):
    down.write(gauge(**{missing: None}))
    down.setting()
    rows = down.json()["usage"]["windows"]
    assert [w["window"] for w in rows] == (["7d"] if missing == "five" else ["5h"])
    down.world.assert_no_leak(capsys)


def test_a_window_with_no_reset_time_has_none(down, capsys):
    down.write(gauge(five_reset="soon", week_reset=None))
    down.setting()
    assert down.line() == FILE_LINE_NO_RESETS          # no clause, never `unknown`
    rows = down.json()["usage"]["windows"]
    assert [(w["resets_at"], w["expired"]) for w in rows] == [(None, False), (None, False)]
    down.world.assert_no_leak(capsys)


def test_other_keys_in_the_file_are_ignored(down, capsys):
    down.write(gauge(seven_day_opus={"used_percentage": 50}, note="x"))
    down.setting()
    assert down.line() == FILE_LINE
    down.world.assert_no_leak(capsys)


# --- 8. a file that cannot be used: null with a reason, exit 0 -------------------------


def _unusable(down: Gauge, reason: str, capsys, *, scan: bool = True) -> None:
    """Usage null with REASON after the exporter's, in text and JSON, exit 0.
    SCAN False leaves the leak scan to a caller that must first remove a
    file it planted (the scan reads every file under HOME)."""
    want = f"{NO_EXPORTER}; limits file: {reason}"
    assert down.line() == f"usage: unavailable ({want})"
    data = down.json()
    assert data["usage"] is None and data["usage_unavailable"] == want
    if scan:
        down.world.assert_no_leak(capsys)


def test_no_file_at_the_path(down, capsys):
    down.setting()
    _unusable(down, f"no file at {down.path}", capsys)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_file(down, capsys):
    down.write(gauge())
    down.path.chmod(0)
    down.setting()
    try:
        _unusable(down, f"{down.path} could not be read (Permission denied)", capsys,
                  scan=False)
    finally:
        down.path.chmod(0o600)
    down.world.assert_no_leak(capsys)


def test_a_directory_at_the_path(down, capsys):
    down.path.mkdir()
    down.setting()
    assert down.line().startswith(
        f"usage: unavailable ({NO_EXPORTER}; limits file: {down.path} could not be read (")
    down.world.assert_no_leak(capsys)


@pytest.fixture
def no_hang():
    """A test that would block fails after a few seconds instead of hanging
    the suite: SIGALRM raises out of the blocked call (PEP 475 retries an
    interrupted call only when the handler returns)."""
    def hung(signum, frame):
        raise AssertionError("blocked: status waited on the limits file")

    old = signal.signal(signal.SIGALRM, hung)
    signal.alarm(5)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


def test_a_fifo_at_the_path_is_refused_not_waited_on(down, no_hang, capsys):
    os.mkfifo(down.path)                        # no writer: a blocking open would hang
    down.setting()
    _unusable(down, f"{down.path} could not be read (not a regular file)", capsys,
              scan=False)
    down.path.unlink()                          # the scan reads every file under HOME
    down.world.assert_no_leak(capsys)


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


def test_read_limits_closes_its_fd_on_every_path(tmp_path, no_hang):
    """Read, not a regular file, too big, not JSON, missing: no fd is left."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps(gauge()))
    big = tmp_path / "big.json"
    big.write_text("x" * (lm.MAX_BYTES + 1))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    folder = tmp_path / "folder"
    folder.mkdir()
    before = _open_fds()
    for path in (good, big, bad, fifo, folder, tmp_path / "missing"):
        lm.read_limits(path, NOW)
    assert _open_fds() == before
    assert lm.read_limits(good, NOW).unavailable is None
    assert "larger than" in lm.read_limits(big, NOW).unavailable


@pytest.mark.parametrize("body, why", [
    ("{not json", "not JSON"),
    (b"\xff\xfe{}", "not UTF-8"),
    ("[]", "not a JSON object"),
    pytest.param("x" * (lm.MAX_BYTES + 1), f"larger than {lm.MAX_BYTES} bytes",
                 id="too-big"),
    (gauge(five="3"), "five_hour.used_percentage is not a number of 0 or more"),
    (gauge(five=True), "five_hour.used_percentage is not a number of 0 or more"),
    (gauge(week=-1), "seven_day.used_percentage is not a number of 0 or more"),
    ('{"five_hour": {"used_percentage": NaN}, "written_at": 1}',
     "five_hour.used_percentage is not a number of 0 or more"),
    (gauge(five_hour=[1, 2]), "five_hour is not an object"),
    (gauge(written_at=None), "written_at is missing or not a number"),
    (gauge(written_at="yesterday"), "written_at is missing or not a number"),
    (gauge(age=-(lm.FUTURE_SLACK + 1)), "written_at is in the future"),
])
def test_a_malformed_file_is_null_with_the_part_named(down, body, why, capsys):
    if isinstance(body, bytes):
        down.path.write_bytes(body)
    else:
        down.write(body)
    down.setting()
    _unusable(down, f"{down.path} is malformed ({why})", capsys)


def test_a_file_with_no_windows_says_so(down, capsys):
    down.write(gauge(five=None, week=None))
    down.setting()
    _unusable(down, f"{down.path} has no usage windows", capsys)


def test_another_accounts_window_is_not_the_hosts(down, capsys):
    down.write(gauge(account="ai@example.com"))
    down.setting()
    _unusable(down, f"{down.path} holds account ai@example.com's window, not the host's",
              capsys)


@pytest.mark.parametrize("account", ["token", "spaces and; stuff", 7])
def test_an_odd_account_is_refused_and_not_shown(down, account, capsys):
    secret = synthetic_token()
    down.world.tokens["file-account"] = secret          # the leak scan looks for it
    down.write(gauge(account=secret if account == "token" else account))
    down.setting()
    _unusable(down, f"{down.path} holds another account's window (its name is not shown), "
                    "not the host's", capsys, scan=False)
    down.path.unlink()                                  # the test wrote it; the scan is of ours
    down.world.assert_no_leak(capsys)


# --- 9, 10. the setting ----------------------------------------------------------------


@pytest.mark.parametrize("value, why", [
    ("gauge/limits.json", "is 'gauge/limits.json', not an absolute path"),
    (42, "is a int"),
    ("", "is empty"),
    ("/tmp/a\nb", "holds unprintable characters (not shown)"),
])
def test_a_refused_setting_is_reported_exit_0(down, value, why, capsys):
    where = down.setting(value)
    _unusable(down, f"{lm.SETTING_KEY} (in {where}) {why}: it must be the path of the "
                    "limits file, absolute or starting with ~", capsys)


def test_a_token_shaped_setting_is_never_quoted(down, capsys):
    secret = synthetic_token()
    down.world.tokens["setting"] = secret
    where = down.setting(f"/x/{secret}")
    _unusable(down, f"{lm.SETTING_KEY} (in {where}) looks like a token, not a path (the "
                    "value is not shown): it must be the path of the limits file, absolute "
                    "or starting with ~", capsys, scan=False)
    where.unlink()                                      # the test wrote it; the scan is of ours
    down.world.assert_no_leak(capsys)


def test_a_settings_layer_the_reader_refuses_is_reported_exit_0(down, capsys):
    folder = host(down.world.home, "session")
    folder.mkdir(parents=True)
    (folder / "session.json").write_text("{not json")
    assert down.line().startswith(
        f"usage: unavailable ({NO_EXPORTER}; limits file: settings: ")
    assert down.json()["usage"] is None
    down.world.assert_no_leak(capsys)


def test_the_setting_is_read_from_a_session_d_file(down, capsys):
    down.write(gauge())
    down.setting(name="session.d/50-limits.json")
    assert down.line() == FILE_LINE
    down.world.assert_no_leak(capsys)


def test_the_setting_is_read_from_the_template(down, capsys):
    down.write(gauge())
    down.setting(name="session.template.json")
    assert down.line() == FILE_LINE
    down.world.assert_no_leak(capsys)


def test_a_seat_or_session_layer_does_not_set_it(down, capsys):
    down.write(gauge())
    env = {"HOME": str(down.world.home)}
    down.setting(folder=settings.session_dir("0123abcd", env) / "settings")
    down.setting(folder=settings.seat_dir("someseat", env) / "settings")
    down.world.env = {"CLAUDE_CODE_SESSION_ID": "0123abcd", "SESSION_SEAT": "someseat"}
    assert down.line() == f"usage: unavailable ({NO_EXPORTER})"
    down.world.assert_no_leak(capsys)


def test_the_setting_reader_alone(tmp_path):
    assert lm.limits_setting({}, tmp_path) is None
    folder = host(tmp_path, "session")
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"account": {"limits_file": "~/x.json"}}))
    assert lm.limits_setting({"HOME": str(tmp_path)}) == tmp_path / "x.json"
    assert lm.limits_setting({}, tmp_path) == tmp_path / "x.json"


# --- 12. resets: shown by default, `--no-resets` hides them ----------------------------


def test_file_resets_are_shown_by_default_and_no_resets_hides_them(down, capsys):
    down.write(gauge())
    down.setting()
    assert down.line() == FILE_LINE
    assert [w["resets_at"] for w in down.json()["usage"]["windows"]] == \
        [st._iso(FIVE_RESET), st._iso(WEEK_RESET)]
    assert down.line(resets=False) == FILE_LINE_NO_RESETS
    assert all("resets_at" not in w for w in down.json(resets=False)["usage"]["windows"])
    down.world.assert_no_leak(capsys)


@pytest.mark.parametrize("flags, shown", [([], True), (["--resets"], True),
                                          (["--no-resets"], False)])
def test_the_cli_reset_flags_reach_the_file_windows(down, flags, shown, capsys):
    down.write(gauge())
    down.setting()
    for as_json in (False, True):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["status", *flags, *(["--json"] if as_json else [])],
                        keychain=down.world.keychain, fetch=down.world.fetch, env={},
                        home=down.world.home, clock=clock, out=out, err=err)
        assert code == 0, err.getvalue()
        if as_json:
            rows = json.loads(out.getvalue())["usage"]["windows"]
            assert all(("resets_at" in w) is shown for w in rows)
        else:
            line = out.getvalue().splitlines()[2]
            assert line == (FILE_LINE if shown else FILE_LINE_NO_RESETS)
    down.world.assert_no_leak(capsys)


# --- 13. through the CLI ---------------------------------------------------------------


def test_the_cli_prints_the_five_hour_percent_from_the_file(down, capsys):
    down.write(gauge(five=61))
    down.setting()
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["status", "--json"], keychain=down.world.keychain,
                    fetch=down.world.fetch, env={}, home=down.world.home, clock=clock,
                    out=out, err=err)
    assert code == 0, err.getvalue()
    usage = json.loads(out.getvalue())["usage"]
    assert [w["percent"] for w in usage["windows"] if w["window"] == "5h"] == [61.0]
    down.world.assert_no_leak(capsys)


def test_status_still_writes_no_file(down, capsys):
    down.write(gauge())
    down.setting()
    home = down.world.home
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert down.status() == 0 and down.status(as_json=True) == 0
    assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    down.world.assert_no_leak(capsys)


# --- the pieces ------------------------------------------------------------------------


@pytest.mark.parametrize("seconds, said", [(0, "0s"), (59, "59s"), (60, "1m"),
                                           (3599, "59m"), (3600, "1h"), (86399, "23h"),
                                           (86400, "1d"), (10 * 86400, "10d")])
def test_age_text(seconds, said):
    assert st.age_text(seconds) == said


def test_read_limits_never_raises_and_names_no_content(tmp_path):
    path = tmp_path / "l.json"
    secret = synthetic_token()
    path.write_text(json.dumps({"five_hour": {"used_percentage": secret}, "written_at": NOW}))
    usage = lm.read_limits(path, NOW)
    assert usage.unavailable is not None and secret not in usage.unavailable


def test_a_window_over_a_week_ago_reads_as_the_exporter_says_it(tmp_path, capsys):
    """One rendering for both sources: the limits file's ended window is the
    exporter's text, word for word, and `--json` marks it stale on both."""
    ended = NOW - 7 * 86400
    down = Gauge(make_world(tmp_path, metrics=None))
    down.write(gauge(five=None, week=97, week_reset=ended))
    down.setting()
    said = f"7d STALE: window ended {st._iso(ended)} (7d ago), figure not shown"
    assert down.line() == f"usage: {said}  (usage of the host account, limits file, 2m old)"
    [row] = down.json()["usage"]["windows"]
    assert (row["expired"], row["stale"], row["percent"]) == (True, True, None)

    (tmp_path / "up").mkdir()
    up = Gauge(make_world(tmp_path / "up", metrics=(
        'claude_plan_usage_percent{window="7d"} 97\n'
        f'claude_plan_window_reset_timestamp_seconds{{window="7d"}} {ended:.0f}\n')))
    assert up.line().startswith(f"usage: {said}  (usage of the live account")
    [row] = up.json()["usage"]["windows"]
    assert (row["expired"], row["stale"], row["percent"]) == (True, True, None)
    for gauge_ in (down, up):
        gauge_.world.assert_no_leak(capsys)
