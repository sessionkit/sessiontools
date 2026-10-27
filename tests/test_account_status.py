"""`session account status` and `session account list` (ST-22 design-002
§5, §6, do-004).

HARD LIMITS (by design): no Keychain read or write, no real
account switch, no /login, and no run of `security`, `pass` or `claude`.
The autouse fixture makes any subprocess naming one of them raise, gives
every test an empty CLAUDE_CONFIG_DIR (the conftest's tmp HOME holds), and
swaps the default usage fetcher for one that raises, so no test reaches a
real exporter by accident. The Keychain is always a `FakeKeychain`, the
fetcher a fake (or the real one against a server this file starts on
127.0.0.1). Every token is synthetic (`sk-ant-oat01-TEST-<random>`), and
every test asserts no token, and no token's fingerprint, is in any captured
output or in any file the test wrote outside the token files themselves.
"""

from __future__ import annotations

import http.server
import io
import json
import os
import secrets as pysecrets
import socket
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import ANCHOR
from settings_support import write_session_settings

from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.account import status as st
from sessiontools.account import tokenfile as tf

FORBIDDEN = ("security", "pass", "claude")
REAL_FETCH = st.fetch_url

METRICS = """\
# HELP claude_plan_usage_percent Percent of the plan window used.
# TYPE claude_plan_usage_percent gauge
claude_plan_usage_percent{account="default",window="5h"} 75
claude_plan_usage_percent{account="default",window="7d"} 52.5
# HELP claude_plan_window_reset_timestamp_seconds When the window resets.
claude_plan_window_reset_timestamp_seconds{account="default",window="5h"} 1907254800
claude_plan_window_reset_timestamp_seconds{account="default",window="7d"} 1922979600
claude_session_context_tokens{session="x"} 123456
"""


def _argv0(args, kwargs) -> str:
    argv = args[0] if args else kwargs.get("args")
    if isinstance(argv, (list, tuple)) and argv:
        return os.path.basename(str(argv[0]))
    if isinstance(argv, (str, bytes)):
        text = argv.decode() if isinstance(argv, bytes) else argv
        return os.path.basename(text.split()[0]) if text.split() else ""
    return ""


@pytest.fixture(autouse=True)
def _no_keychain_no_pass_no_claude_no_exporter(monkeypatch, tmp_path_factory):
    """Any subprocess call to security, pass or claude raises; the default
    usage fetcher raises; CLAUDE_CONFIG_DIR is empty; no env token."""
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, _name=name, **kwargs):
            if _argv0(args, kwargs) in FORBIDDEN:
                raise AssertionError(f"test tried to run {_argv0(args, kwargs)!r} "
                                     f"via subprocess.{_name}")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)

    def no_exporter(url):
        raise AssertionError(f"test reached the default fetcher for {url}")

    monkeypatch.setattr(st, "fetch_url", no_exporter)
    empty = tmp_path_factory.mktemp("claude-config-empty")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


def test_the_guard_refuses_pass_security_claude_and_the_exporter():
    for name in FORBIDDEN:
        with pytest.raises(AssertionError, match="test tried to run"):
            subprocess.run([name, "--version"], capture_output=True, check=False)
    with pytest.raises(AssertionError, match="default fetcher"):
        st.fetch_url(st.USAGE_URL)


def synthetic_token() -> str:
    return "sk-ant-oat01-TEST-" + pysecrets.token_urlsafe(32)


# --- the world -------------------------------------------------------------------------


class World:
    """A tmp HOME with accounts a, c, p registered (a and c with fps, p
    with none; c has a file and a pass source), a FakeKeychain on `host`,
    and a fetcher that records the URLs it was asked for."""

    def __init__(self, home: Path, *, host: str | None = "c",
                 metrics: str | None = METRICS) -> None:
        self.home = home
        self.tokens_dir = home / "tokens"
        self.tokens_dir.mkdir(mode=0o700)
        self.tokens = {name: synthetic_token() for name in ("a", "c", "p", "stranger")}
        registry = reg.Registry()
        for name, email, extra, with_fp in (
                ("a", "ai@example.com", [], True),
                ("c", "carol@example.com", ["pass:anthropic/carol@example.com"], True),
                ("p", "peggy@example.com", ["pass:anthropic/peggy@example.com"], False)):
            path = self.tokens_dir / name
            path.write_text(self.tokens[name] + "\n")
            path.chmod(0o600)
            sources = [f"file:{path}", *extra] if name != "p" else extra
            registry.add(reg.make_account(
                name, email, sources, reg.fingerprint(self.tokens[name]) if with_fp else None))
        self.registry_path = reg.save(registry, reg.default_path(home))
        blob = None if host is None else kc.render_item(self.tokens[host])
        self.keychain = kc.FakeKeychain(blob)
        self.metrics = metrics
        self.urls: list[str] = []
        self.env: dict[str, str] = {}
        self.out = io.StringIO()
        self.err = io.StringIO()

    @property
    def cfg(self) -> Path:
        return reg.accounts_dir(self.home)

    def config(self, **data) -> None:
        (self.cfg / st.CONFIG_NAME).write_text(json.dumps(data))

    def ledger(self, *rows: object) -> None:
        path = self.cfg / st.LEDGER_NAME
        lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
        path.write_text("".join(line + "\n" for line in lines))
        path.chmod(0o600)

    def fetch(self, url: str) -> str:
        self.urls.append(url)
        if self.metrics is None:
            raise ConnectionRefusedError(61, "Connection refused")
        return self.metrics

    def status(self, **overrides) -> int:
        # A fixed clock: whether a window's reset has passed is told by it, so
        # the fixtures' resets (ANCHOR-relative) never go stale against the real one.
        kwargs = {"keychain": self.keychain, "fetch": self.fetch, "env": self.env,
                  "home": self.home, "clock": lambda: ANCHOR,
                  "out": self.out, "err": self.err}
        kwargs.update(overrides)
        return st.status(**kwargs)

    def json(self, **overrides) -> dict:
        assert self.status(as_json=True, **overrides) == 0, self.err.getvalue()
        return json.loads(self.out.getvalue())

    def list(self, **overrides) -> int:
        kwargs = {"keychain": self.keychain, "home": self.home,
                  "out": self.out, "err": self.err}
        kwargs.update(overrides)
        return st.list_accounts(**kwargs)

    def text(self) -> str:
        return self.out.getvalue() + self.err.getvalue()

    def assert_no_leak(self, capsys) -> None:
        """No token, its hex or its fp in any output; no token or its hex in
        any file under HOME outside the token files."""
        captured = capsys.readouterr()
        outputs = (self.text(), captured.out, captured.err)
        for token in self.tokens.values():
            fp, hexed = reg.fingerprint(token), token.encode().hex()
            for text in outputs:
                assert token not in text and hexed not in text and fp not in text
            for path in self.home.rglob("*"):
                if path.is_file() and path.parent != self.tokens_dir:
                    data = path.read_bytes()
                    assert token.encode() not in data, f"token leaked into {path}"
                    assert hexed.encode() not in data, f"token hex leaked into {path}"


@pytest.fixture
def world(tmp_path) -> World:
    home = tmp_path / "home"
    home.mkdir()
    return World(home)


def make_world(tmp_path, **kwargs) -> World:
    home = tmp_path / "home"
    home.mkdir()
    return World(home, **kwargs)


# --- status: the host ------------------------------------------------------------------


def test_status_names_the_host_by_its_item_fp(world, capsys):
    assert world.status() == 0
    lines = world.out.getvalue().splitlines()
    assert lines[0] == "host:  c (carol@example.com)  (Keychain, fp match)"
    assert lines[1] == "seat:  follows the host"
    assert lines[2] == (
        "usage: 5h 75% (resets 2030-06-09T17:00+00:00), "
        "7d 52.5% (resets 2030-12-08T17:00+00:00)  "
        "(usage of the live account, age not reported by the exporter)")
    assert world.keychain.reads == 1 and world.keychain.writes == []
    assert world.err.getvalue() == ""
    world.assert_no_leak(capsys)


def test_status_json_shape_is_design_section_5(world, capsys):
    data = world.json()
    assert set(data) == {"host", "seat", "usage", "profile"}
    assert data["host"] == {"name": "c", "email": "carol@example.com",
                            "via": "Keychain, fp match"}
    assert data["seat"] == {"name": "c", "via": "follows the host"}
    assert data["usage"] == {
        "url": st.USAGE_URL, "label": "usage of the live account",
        "windows": [{"window": "5h", "account": "default", "percent": 75.0,
                     "expired": False, "stale": False, "age_seconds": None,
                     "resets_at": "2030-06-09T17:00+00:00"},
                    {"window": "7d", "account": "default", "percent": 52.5,
                     "expired": False, "stale": False, "age_seconds": None,
                     "resets_at": "2030-12-08T17:00+00:00"}]}
    world.assert_no_leak(capsys)


def test_an_fp_not_in_the_registry_is_unknown_never_a_guess(tmp_path, capsys):
    world = make_world(tmp_path, host="stranger")
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[0] == \
        "host:  Keychain: unknown (not in registry)"
    world.out = io.StringIO()
    data = world.json()
    assert data["host"] == {"name": None, "email": None,
                            "via": "Keychain: unknown (not in registry)"}
    assert data["seat"] == {"name": None, "via": "follows the host"}
    world.assert_no_leak(capsys)


def test_the_account_registered_without_an_fp_is_unknown_too(tmp_path, capsys):
    """p has no fp, so the host on p's token cannot be named."""
    world = make_world(tmp_path, host="p")
    assert world.json()["host"]["name"] is None
    world.assert_no_leak(capsys)


def test_an_item_with_no_token_says_so(world, capsys):
    world.keychain.blob = json.dumps({kc.ITEM_KEY: {"refreshToken": None}}).encode()
    assert world.json()["host"] == {"name": None, "email": None,
                                    "via": "Keychain: no token in the item"}
    world.assert_no_leak(capsys)


@pytest.mark.parametrize("keychain", [kc.FakeKeychain(None), kc.FakeKeychain(b"{}"),
                                      kc.FakeKeychain(b"x", fail_read=True)])
def test_an_unreadable_host_is_reported_not_fatal(world, keychain, capsys):
    assert world.status(keychain=keychain) == 0
    first = world.out.getvalue().splitlines()[0]
    assert first.startswith("host:  Keychain: unreadable (")
    assert "usage: 5h 75%" in world.out.getvalue()
    world.assert_no_leak(capsys)


def test_host_read_false_reads_the_ledger_and_never_the_keychain(world, capsys):
    world.config(host_read=False)
    world.ledger({"at": "x", "from": "a", "to": "c", "by": "s", "witness": "carol"},
                 "not json at all",
                 {"at": "y", "from": "c", "to": "a", "by": "s", "witness": "carol"},
                 "{\"truncated")
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[0] == \
        "host:  a (ai@example.com)  (from ledger, unverified)"
    assert world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_host_read_false_never_builds_the_real_keychain(world, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise AssertionError("SecurityCLI built with host_read false")

    monkeypatch.setattr(kc, "SecurityCLI", boom)
    world.config(host_read=False)
    world.ledger({"to": "c"})
    data = world.json(keychain=None)
    assert data["host"] == {"name": "c", "email": "carol@example.com",
                            "via": "from ledger, unverified"}
    world.assert_no_leak(capsys)


def test_host_read_false_with_no_ledger_says_so(world, capsys):
    world.config(host_read=False)
    data = world.json()
    assert data["host"]["name"] is None
    assert data["host"]["via"].startswith("from ledger, unverified: no ledger row at ")
    assert world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_a_ledger_name_not_in_the_registry_is_named_without_an_email(world, capsys):
    world.config(host_read=False)
    world.ledger({"to": "gone"})
    assert world.json()["host"] == {"name": "gone", "email": None,
                                    "via": "from ledger, unverified"}
    world.assert_no_leak(capsys)


@pytest.mark.parametrize("which", ["c", "a"])
def test_a_ledger_row_naming_no_registry_name_is_refused_unshown(world, which, capsys):
    """Review fix: a token pasted into the ledger's `to` was printed as the
    host's name. `use` writes only registry names there."""
    world.config(host_read=False)
    world.ledger({"to": "a"}, {"to": world.tokens[which]})
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[0].startswith(
        "host:  from ledger, unverified: the last row at ")
    assert "its value is not shown" in world.out.getvalue()
    world.out = io.StringIO()
    assert world.json()["host"]["name"] is None
    world.out = io.StringIO()
    assert world.list() == 0
    assert "<- host" not in world.out.getvalue()
    (world.cfg / st.LEDGER_NAME).unlink()           # the test wrote it; the scan is of ours
    world.assert_no_leak(capsys)


# --- status: this seat -----------------------------------------------------------------


def test_an_env_token_names_this_seat_immune_to_use(world, capsys):
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["a"]}
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[1] == \
        "seat:  a (ai@example.com)  (env token: immune to use)"
    world.out = io.StringIO()
    assert world.json()["seat"] == {"name": "a", "via": "env token: immune to use"}
    world.assert_no_leak(capsys)


def test_an_unknown_env_token_is_unknown(world, capsys):
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["stranger"]}
    assert world.json()["seat"] == {"name": None,
                                    "via": "env token: immune to use; unknown (not in registry)"}
    world.assert_no_leak(capsys)


def test_the_seat_reads_this_process_env_by_default(world, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", world.tokens["c"])
    assert world.json(env=None)["seat"] == {"name": "c", "via": "env token: immune to use"}
    world.assert_no_leak(capsys)


def test_a_blank_env_token_follows_the_host(world, capsys):
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": "  "}
    assert world.json()["seat"] == {"name": "c", "via": "follows the host"}
    world.assert_no_leak(capsys)


# --- status: usage ---------------------------------------------------------------------


def test_usage_unreachable_is_unavailable_exit_0(tmp_path, capsys):
    world = make_world(tmp_path, metrics=None)
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[2] == \
        f"usage: unavailable (no exporter at {st.USAGE_URL})"
    assert world.urls == [st.USAGE_URL]
    world.out = io.StringIO()
    data = world.json()
    assert data["usage"] is None
    assert data["host"]["name"] == "c"
    world.assert_no_leak(capsys)


def test_usage_reachable_with_no_series_is_unavailable(tmp_path, capsys):
    world = make_world(tmp_path, metrics="# nothing here\nup 1\n")
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[2] == \
        f"usage: unavailable (no claude_plan_usage_percent at {st.USAGE_URL})"
    world.assert_no_leak(capsys)


def test_a_fetcher_returning_non_text_is_unavailable(world, capsys):
    usage = st.usage_of("http://x/metrics", lambda url: b"bytes")
    assert usage.unavailable == "no exporter at http://x/metrics"
    world.assert_no_leak(capsys)


def test_usage_url_comes_from_the_config(world, capsys):
    world.config(usage_url="http://127.0.0.1:9999/metrics")
    assert world.status() == 0
    assert world.urls == ["http://127.0.0.1:9999/metrics"]
    world.assert_no_leak(capsys)


def test_reset_times_are_shown_by_default_and_one_flag_hides_them(world, capsys):
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[2] == (
        "usage: 5h 75% (resets 2030-06-09T17:00+00:00), "
        "7d 52.5% (resets 2030-12-08T17:00+00:00)  "
        "(usage of the live account, age not reported by the exporter)")
    world.out = io.StringIO()
    data = world.json()
    assert [row["resets_at"] for row in data["usage"]["windows"]] == \
        ["2030-06-09T17:00+00:00", "2030-12-08T17:00+00:00"]
    world.out = io.StringIO()
    assert world.status(resets=False) == 0
    assert "resets" not in world.out.getvalue()
    world.out = io.StringIO()
    data = world.json(resets=False)
    assert all("resets_at" not in row for row in data["usage"]["windows"])
    world.assert_no_leak(capsys)


def test_a_window_with_no_reset_series_shows_no_reset(tmp_path, capsys):
    world = make_world(tmp_path, metrics='claude_plan_usage_percent{window="5h"} 10\n')
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[2] == \
        "usage: 5h 10%  (usage of the live account, age not reported by the exporter)"
    assert "resets" not in world.out.getvalue()
    world.out = io.StringIO()
    assert world.json()["usage"]["windows"] == \
        [{"window": "5h", "account": None, "percent": 10.0, "expired": False, "stale": False,
          "age_seconds": None, "resets_at": None}]
    world.assert_no_leak(capsys)


def test_two_accounts_in_the_exporter_are_told_apart(tmp_path, capsys):
    world = make_world(tmp_path, metrics=(
        'claude_plan_usage_percent{account="ai***@example.com",window="5h"} 1\n'
        'claude_plan_usage_percent{account="default",window="5h"} 2\n'))
    assert world.status() == 0
    assert "usage: ai***@example.com 5h 1%, default 5h 2%" in world.out.getvalue()
    world.assert_no_leak(capsys)


def test_parse_metrics_skips_what_it_cannot_read():
    text = ('# HELP x y\n'
            '\n'
            'claude_plan_usage_percent{window="5h",note="a \\"quoted\\" \\\\ b"} 3 1922963696\n'
            'claude_plan_usage_percent{window="7d"} not-a-number\n'
            'claude_plan_usage_percent{window=5h} 4\n'
            'garbage line with spaces\n'
            'plain_metric 7\n')
    assert st.parse_metrics(text) == [
        ("claude_plan_usage_percent", {"window": "5h", "note": 'a "quoted" \\ b'}, 3.0,
         1922963696),
        ("plain_metric", {}, 7.0, None)]


@pytest.mark.parametrize("bad", ["NaN", "+Inf", "-Inf"])
def test_a_non_finite_sample_is_skipped_never_a_crash_or_bad_json(tmp_path, bad, capsys):
    """Review fix: `int(nan)` crashed the text and `--json` emitted `NaN`."""
    world = make_world(tmp_path, metrics=(
        f'claude_plan_usage_percent{{account="default",window="5h"}} {bad}\n'
        'claude_plan_usage_percent{account="default",window="7d"} 40\n'
        f'claude_plan_window_reset_timestamp_seconds{{account="default",window="7d"}} {bad}\n'))
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[2] == \
        "usage: 7d 40%  (usage of the live account, age not reported by the exporter)"
    world.out = io.StringIO()
    assert world.status(as_json=True) == 0
    data = json.loads(world.out.getvalue(), parse_constant=lambda c: pytest.fail(c))
    assert data["usage"]["windows"] == [
        {"window": "7d", "account": "default", "percent": 40.0, "expired": False,
         "stale": False, "age_seconds": None, "resets_at": None}]
    world.assert_no_leak(capsys)


def test_only_non_finite_usage_is_unavailable(tmp_path, capsys):
    world = make_world(tmp_path, metrics='claude_plan_usage_percent{window="5h"} NaN\n')
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[2] == \
        f"usage: unavailable (no claude_plan_usage_percent at {st.USAGE_URL})"
    world.assert_no_leak(capsys)


def test_a_token_shaped_label_value_is_redacted():
    token = synthetic_token()
    text = f'claude_plan_usage_percent{{account="{token}",window="5h"}} 9\n'
    [(_, labels, _, _)] = st.parse_metrics(text)
    assert labels["account"] == "(redacted)"
    usage = st.usage_of("http://x", lambda url: text)
    assert token not in st.usage_line(usage) and token not in json.dumps(usage.to_json())


class _Metrics(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = METRICS.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_the_real_fetcher_reads_a_local_exporter():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Metrics)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/metrics"
        usage = st.usage_of(url, REAL_FETCH)
    finally:
        server.shutdown()
        server.server_close()
    assert usage.unavailable is None
    assert [(w.window, w.percent) for w in usage.windows] == [("5h", 75.0), ("7d", 52.5)]


def test_the_real_fetcher_on_a_closed_port_is_unavailable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/metrics"
    assert st.usage_of(url, REAL_FETCH).unavailable == f"no exporter at {url}"


# --- status: STALE past a reset, and each figure's age ---------------------------------

NOW = ANCHOR.timestamp()


def fixed_clock() -> datetime:
    return datetime.fromtimestamp(NOW, UTC)


#: Two account-labelled series, as one exporter reported them: `work`'s 7d
#: window ended a week before the clock, and the exporter sent no sample
#: timestamps. Its 97 is a used-percent of a window that is over.
TWO_ACCOUNTS_ONE_WINDOW_OVER = f"""\
claude_plan_usage_percent{{account="work",window="5h"}} 12
claude_plan_usage_percent{{account="work",window="7d"}} 97
claude_plan_window_reset_timestamp_seconds{{account="work",window="5h"}} {NOW + 3 * 3600:.0f}
claude_plan_window_reset_timestamp_seconds{{account="work",window="7d"}} {NOW - 7 * 86400:.0f}
claude_plan_usage_percent{{account="default",window="5h"}} 30
claude_plan_usage_percent{{account="default",window="7d"}} 40
claude_plan_window_reset_timestamp_seconds{{account="default",window="5h"}} {NOW + 3600:.0f}
claude_plan_window_reset_timestamp_seconds{{account="default",window="7d"}} {NOW + 2 * 86400:.0f}
"""


def _stale_world(tmp_path, metrics: str) -> World:
    return make_world(tmp_path, metrics=metrics)


def _line(world: World, **overrides) -> str:
    world.out = io.StringIO()
    overrides.setdefault("clock", fixed_clock)
    assert world.status(**overrides) == 0, world.err.getvalue()
    return world.out.getvalue().splitlines()[2]


def _json(world: World, **overrides) -> dict:
    world.out = io.StringIO()
    overrides.setdefault("clock", fixed_clock)
    return world.json(**overrides)


def test_an_exporter_window_whose_reset_passed_is_stale_and_its_figure_withheld(
        tmp_path, capsys):
    world = _stale_world(tmp_path, TWO_ACCOUNTS_ONE_WINDOW_OVER)
    line = _line(world)
    assert line == (
        f"usage: work 5h 12% (resets {st._iso(NOW + 3 * 3600)}), "
        f"work 7d STALE: window ended {st._iso(NOW - 7 * 86400)} (7d ago), "
        "figure not shown, "
        f"default 5h 30% (resets {st._iso(NOW + 3600)}), "
        f"default 7d 40% (resets {st._iso(NOW + 2 * 86400)})  "
        "(usage of the live account, age not reported by the exporter)")
    assert "97" not in line
    windows = _json(world)["usage"]["windows"]
    over = windows[1]
    assert over == {"window": "7d", "account": "work", "percent": None, "expired": True,
                    "stale": True, "age_seconds": None,
                    "resets_at": st._iso(NOW - 7 * 86400)}
    assert all(w["expired"] is False and w["stale"] is False
               for i, w in enumerate(windows) if i != 1)
    assert [w["percent"] for w in windows] == [12.0, None, 30.0, 40.0]
    world.assert_no_leak(capsys)


def test_a_stale_window_is_said_even_with_no_resets(tmp_path, capsys):
    world = _stale_world(tmp_path, TWO_ACCOUNTS_ONE_WINDOW_OVER)
    line = _line(world, resets=False)
    assert "work 7d STALE: window ended" in line and "figure not shown" in line
    assert "(resets" not in line and "97" not in line
    over = _json(world, resets=False)["usage"]["windows"][1]
    assert over["stale"] is True and over["percent"] is None and "resets_at" not in over
    world.assert_no_leak(capsys)


def test_an_exporter_reset_exactly_now_has_passed(tmp_path, capsys):
    world = _stale_world(tmp_path, (
        'claude_plan_usage_percent{window="5h"} 9\n'
        f'claude_plan_window_reset_timestamp_seconds{{window="5h"}} {NOW:.0f}\n'))
    assert _line(world).startswith("usage: 5h STALE: window ended ")
    assert _json(world)["usage"]["windows"][0]["expired"] is True
    world.assert_no_leak(capsys)


def test_usage_of_uses_the_now_it_is_given():
    text = ('claude_plan_usage_percent{window="7d"} 5\n'
            f'claude_plan_window_reset_timestamp_seconds{{window="7d"}} {NOW:.0f}\n')
    [before] = st.usage_of("http://x", lambda url: text, NOW - 1).windows
    [after] = st.usage_of("http://x", lambda url: text, NOW + 1).windows
    assert (before.expired, before.percent) == (False, 5.0)
    assert (after.expired, after.percent) == (True, None)


def test_exporter_sample_timestamps_give_each_figure_its_age(tmp_path, capsys):
    ms = int((NOW - 180) * 1000)
    world = _stale_world(tmp_path, (
        f'claude_plan_usage_percent{{account="default",window="5h"}} 75 {ms}\n'
        f'claude_plan_usage_percent{{account="default",window="7d"}} 52.5 {ms - 3_600_000}\n'
        f'claude_plan_window_reset_timestamp_seconds{{account="default",window="5h"}} '
        f'{NOW + 3600:.0f} {ms}\n'))
    assert _line(world) == (
        f"usage: 5h 75% (resets {st._iso(NOW + 3600)}, 3m old), 7d 52.5% (1h old)  "
        "(usage of the live account)")
    assert _line(world, resets=False) == \
        "usage: 5h 75% (3m old), 7d 52.5% (1h old)  (usage of the live account)"
    windows = _json(world)["usage"]["windows"]
    assert [w["age_seconds"] for w in windows] == [180, 3780]
    world.assert_no_leak(capsys)


def test_a_figure_with_no_timestamp_beside_one_with_says_so_on_its_own(tmp_path, capsys):
    ms = int((NOW - 120) * 1000)
    world = _stale_world(tmp_path, (
        f'claude_plan_usage_percent{{window="5h"}} 75 {ms}\n'
        'claude_plan_usage_percent{window="7d"} 50\n'))
    assert _line(world) == \
        "usage: 5h 75% (2m old), 7d 50% (age not reported)  (usage of the live account)"
    assert [w["age_seconds"] for w in _json(world)["usage"]["windows"]] == [120, None]
    world.assert_no_leak(capsys)


def test_no_timestamps_say_age_not_reported_once_never_a_guess(tmp_path, capsys):
    world = _stale_world(tmp_path, METRICS)
    line = _line(world)
    assert line.count("age not reported") == 1
    assert line.endswith("(usage of the live account, age not reported by the exporter)")
    assert " old" not in line
    assert [w["age_seconds"] for w in _json(world)["usage"]["windows"]] == [None, None]
    world.assert_no_leak(capsys)


@pytest.mark.parametrize("stamp", ["soon", "1.5e12", str(int((NOW + 3600) * 1000))])
def test_a_timestamp_that_is_not_a_time_or_is_ahead_is_unknown(tmp_path, stamp, capsys):
    world = _stale_world(tmp_path, f'claude_plan_usage_percent{{window="5h"}} 75 {stamp}\n')
    assert _line(world) == \
        "usage: 5h 75%  (usage of the live account, age not reported by the exporter)"
    assert _json(world)["usage"]["windows"][0]["age_seconds"] is None
    world.assert_no_leak(capsys)


def test_a_little_exporter_clock_jitter_reads_as_zero_seconds_old(tmp_path, capsys):
    world = _stale_world(tmp_path,
                         f'claude_plan_usage_percent{{window="5h"}} 75 {int((NOW + 30) * 1000)}\n')
    assert _line(world) == "usage: 5h 75% (0s old)  (usage of the live account)"
    world.assert_no_leak(capsys)


# --- status: refusals ------------------------------------------------------------------


@pytest.mark.parametrize("body, why", [
    ("{not json", "is not valid JSON"),
    ("[]", "top level must be an object"),
    ('{"host_read": false, "colour": 1}', "unknown key(s) 'colour'"),
    ('{"host_read": "no"}', "host_read must be true or false"),
    ('{"host_read": 0}', "host_read must be true or false"),
    ('{"usage_url": 9103}', "usage_url must be a URL string"),
    ('{"usage_url": ""}', "usage_url must be a URL string"),
])
def test_a_bad_config_is_exit_5_and_left_alone(world, body, why, capsys):
    path = world.cfg / st.CONFIG_NAME
    path.write_text(body)
    assert world.status() == 5
    assert why in world.err.getvalue()
    assert path.read_text() == body
    assert world.keychain.reads == 0 and world.urls == []
    assert world.list() == 5
    world.assert_no_leak(capsys)


def test_a_token_in_the_config_url_is_refused_and_not_echoed(world, capsys):
    world.config(usage_url=f"http://x/?t={world.tokens['c']}")
    assert world.status() == 5
    assert "usage_url must be a URL string" in world.err.getvalue()
    assert world.urls == []
    (world.cfg / st.CONFIG_NAME).unlink()           # the test wrote it; the scan is of ours
    world.assert_no_leak(capsys)


def test_a_refused_registry_is_exit_5(world, capsys):
    world.registry_path.chmod(0o644)
    assert world.status() == 5
    assert "not 0600" in world.err.getvalue()
    assert world.list() == 5
    world.assert_no_leak(capsys)


def test_status_with_no_registry_still_reports(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    out = io.StringIO()
    keychain = kc.FakeKeychain(kc.render_item(synthetic_token()))
    assert st.status(keychain=keychain, fetch=lambda url: "", env={}, home=home,
                     out=out, err=io.StringIO()) == 0
    assert out.getvalue().splitlines()[0] == "host:  Keychain: unknown (not in registry)"
    assert not (home / ".config").exists()


def test_status_never_writes_a_file(world, capsys):
    before = {path: path.read_bytes() for path in world.home.rglob("*") if path.is_file()}
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["a"]}
    assert world.status() == 0
    assert world.status(as_json=True) == 0
    after = {path: path.read_bytes() for path in world.home.rglob("*") if path.is_file()}
    assert after == before
    world.assert_no_leak(capsys)


# --- list ------------------------------------------------------------------------------


@pytest.fixture
def no_resolve(monkeypatch):
    """Any attempt to resolve a source raises: a backend, or `resolve`."""
    calls = []

    def refuse(ref):
        calls.append(ref)
        raise AssertionError(f"list resolved a source ({ref})")

    monkeypatch.setattr(reg, "BACKENDS", {"file": refuse, "pass": refuse})

    def no(*args, **kwargs):
        raise AssertionError("list called registry.resolve")

    monkeypatch.setattr(reg, "resolve", no)
    return calls


def test_list_is_registry_order_with_sources_cost_fp_and_the_host_mark(world, no_resolve,
                                                                       capsys):
    assert world.list() == 0
    assert world.out.getvalue().splitlines() == [
        "a  ai@example.com     file (free)                    fp known",
        "c  carol@example.com  file (free), pass (key touch)  fp known    <- host",
        "p  peggy@example.com  pass (key touch)               fp unknown",
    ]
    assert no_resolve == []
    assert world.keychain.writes == []
    world.assert_no_leak(capsys)


def test_list_resolves_nothing_even_when_every_source_would_raise(world, no_resolve, capsys):
    for path in world.tokens_dir.iterdir():
        path.unlink()                                    # every file source is gone
    assert world.list() == 0
    assert "<- host" in world.out.getvalue()
    assert no_resolve == []
    world.assert_no_leak(capsys)


def test_list_marks_the_host_from_the_ledger_when_host_read_is_false(world, no_resolve,
                                                                     capsys):
    world.config(host_read=False)
    world.ledger({"to": "a"})
    assert world.list() == 0
    lines = world.out.getvalue().splitlines()
    assert lines[0].endswith("<- host") and "<- host" not in "".join(lines[1:])
    assert world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_list_with_an_unknown_host_marks_none_and_says_why(tmp_path, no_resolve, capsys):
    world = make_world(tmp_path, host="stranger")
    assert world.list() == 0
    out = world.out.getvalue()
    assert "<- host" not in out
    assert out.splitlines()[-1] == "host: Keychain: unknown (not in registry)"
    world.assert_no_leak(capsys)


def test_list_with_no_registry_prints_the_path_and_the_add_line(tmp_path, no_resolve):
    home = tmp_path / "home"
    home.mkdir()
    out = io.StringIO()
    keychain = kc.FakeKeychain(None)
    assert st.list_accounts(keychain=keychain, home=home, out=out, err=io.StringIO()) == 0
    assert out.getvalue() == (
        f"no accounts registered: {reg.default_path(home)} does not exist\n"
        "  add one: session account add NAME --email E --source BACKEND:REF\n")
    assert keychain.reads == 0
    assert not (home / ".config").exists()


def test_list_with_an_empty_registry_says_so(tmp_path, no_resolve):
    home = tmp_path / "home"
    home.mkdir()
    reg.save(reg.Registry(), reg.default_path(home))
    out = io.StringIO()
    assert st.list_accounts(keychain=kc.FakeKeychain(None), home=home, out=out,
                            err=io.StringIO()) == 0
    assert out.getvalue().startswith(f"no accounts registered in {reg.default_path(home)}\n")


def test_list_takes_an_explicit_registry_path(world, tmp_path, no_resolve, capsys):
    moved = tmp_path / "elsewhere" / "accounts.json"
    moved.parent.mkdir(mode=0o700)
    world.registry_path.rename(moved)
    assert world.list(home=None, registry_path=moved) == 0
    assert len(world.out.getvalue().splitlines()) == 3
    world.assert_no_leak(capsys)


# --- a token-file host (account.token_file set) -------------------------------------------


def on_file(world: World, holder: str = "c", mode: int = 0o600) -> Path:
    """The host signs in from a token file holding HOLDER's token, named by
    `account.token_file` in a `session` host layer. The Keychain is a fake
    with no item: the file route must never read it."""
    path = world.tokens_dir / "host-oauth"
    path.write_text(world.tokens[holder] + "\n")
    path.chmod(mode)
    write_session_settings(world.home, {"account": {"token_file": str(path)}})
    world.keychain = kc.FakeKeychain(None)
    return path


def no_shape_in(world: World, capsys) -> None:
    captured = capsys.readouterr()
    for text in (world.text(), captured.out, captured.err):
        assert "sk-ant-" not in text
    world.out, world.err = io.StringIO(), io.StringIO()


def test_a_token_file_host_is_named_by_the_file_fp(world, capsys):
    on_file(world)
    assert world.status() == 0
    lines = world.out.getvalue().splitlines()
    assert lines[0] == "host:  c (carol@example.com)  (token file, fp match)"
    assert lines[1].startswith("seat:  NOT on the token file")
    assert world.keychain.reads == 0 and world.keychain.writes == []
    world.out = io.StringIO()
    assert world.json()["host"] == {"name": "c", "email": "carol@example.com",
                                    "via": "token file, fp match"}
    world.assert_no_leak(capsys)


def test_a_token_file_host_never_builds_or_runs_security(world, monkeypatch, capsys):
    on_file(world, "a")

    def no_security(*_args, **_kwargs):
        raise AssertionError("status built a SecurityCLI on the token-file route")

    monkeypatch.setattr(kc.SecurityCLI, "__init__", no_security)
    assert world.json(keychain=None)["host"]["name"] == "a"
    world.out = io.StringIO()
    assert world.list(keychain=None) == 0
    world.assert_no_leak(capsys)


def test_a_token_file_fp_not_in_the_registry_is_unknown(world, capsys):
    """A re-minted account's token is one: its fp is new, so it is unknown."""
    on_file(world, "stranger")
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[0] == \
        "host:  token file: unknown (not in registry)"
    world.out = io.StringIO()
    assert world.json()["host"] == {"name": None, "email": None,
                                    "via": "token file: unknown (not in registry)"}
    world.assert_no_leak(capsys)


@pytest.mark.parametrize("case", ["not-private", "missing", "two-lines", "relative"])
def test_a_refused_token_file_is_reported_not_fatal(world, case, capsys):
    path = on_file(world, "c", 0o644 if case == "not-private" else 0o600)
    if case == "missing":
        path.unlink()
    elif case == "two-lines":
        path.write_text(world.tokens["c"] + "\n" + world.tokens["a"] + "\n")
    elif case == "relative":
        write_session_settings(world.home, {"account": {"token_file": "relative/oauth"}})
    assert world.status() == 0
    first = world.out.getvalue().splitlines()[0]
    assert first.startswith("host:  token file: unreadable (")
    assert "usage: 5h 75%" in world.out.getvalue()
    assert world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_host_read_false_wins_over_the_token_file(world, monkeypatch, capsys):
    on_file(world, "c")
    world.config(host_read=False)
    world.ledger({"to": "a"})

    def no_read(self):
        raise AssertionError("status read the token file with host_read false")

    monkeypatch.setattr(tf.TokenFile, "read", no_read)
    monkeypatch.setattr(tf.TokenFile, "read_bytes", no_read)
    assert world.json()["host"] == {"name": "a", "email": "ai@example.com",
                                    "via": "from ledger, unverified"}
    assert world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_on_a_token_file_host_an_env_token_moves_at_restart(world, capsys):
    on_file(world, "c")
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["a"]}
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[1] == \
        "seat:  a (ai@example.com)  (env token: from launch; moves at restart)"
    world.out = io.StringIO()
    assert world.json()["seat"] == {"name": "a",
                                    "via": "env token: from launch; moves at restart"}
    world.out = io.StringIO()
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["stranger"]}
    assert world.json()["seat"] == {
        "name": None, "via": "env token: from launch; moves at restart; unknown (not in "
                             "registry)"}
    world.assert_no_leak(capsys)


def test_the_seat_label_follows_the_setting_even_with_host_read_false(world, capsys):
    on_file(world, "c")
    world.config(host_read=False)
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["c"]}
    assert world.json()["seat"]["via"] == "env token: from launch; moves at restart"
    world.assert_no_leak(capsys)


def test_list_marks_the_token_file_host(world, no_resolve, capsys):
    on_file(world, "a")
    assert world.list() == 0
    lines = world.out.getvalue().splitlines()
    assert lines[0].endswith("<- host") and lines[0].startswith("a ")
    assert not any(line.endswith("<- host") for line in lines[1:])
    assert no_resolve == [] and world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_remove_refuses_the_token_file_host_account(world, capsys):
    from sessiontools.account import cli

    on_file(world, "c")
    assert cli.remove("c", keychain=world.keychain, home=world.home,
                      out=world.out, err=world.err) == cli.EXIT_HOST
    assert "token file, fp match" in world.err.getvalue()
    assert reg.load(world.registry_path).find("c") is not None
    assert world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_no_token_shape_reaches_any_token_file_output(world, capsys):
    on_file(world, "c")
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["a"]}
    for run in (world.status, lambda: world.status(as_json=True), world.list):
        assert run() == 0
        no_shape_in(world, capsys)


# --- this seat on a token-file host with no env token ----------------------------------------


def test_on_a_token_file_host_no_env_token_is_not_on_the_file(world, capsys):
    """No env token on a token-file host: the seat signs in from Claude's
    stored credentials, not the file. It does NOT follow the host, and its
    name is never the host's."""
    on_file(world, "c")
    world.env = {}
    assert world.status() == 0
    lines = world.out.getvalue().splitlines()
    assert lines[0] == "host:  c (carol@example.com)  (token file, fp match)"
    assert lines[1] == ("seat:  NOT on the token file: no CLAUDE_CODE_OAUTH_TOKEN here, so "
                        "this seat signs in from Claude's stored credentials")
    assert lines[2] == ("       an agent file with inherit: [CLAUDE_CODE_OAUTH_TOKEN] "
                        "carries the token; a tool shell may strip the variable from a "
                        "seat that has it")
    assert lines[3].startswith("usage: 5h 75%")
    assert "follows the host" not in world.out.getvalue()
    assert world.keychain.reads == 0
    world.out = io.StringIO()
    data = world.json()
    assert data["host"]["name"] == "c"
    assert data["seat"] == {"name": None, "via": st.VIA_NOT_FILE}
    assert st.VIA_NOT_FILE == "no env token: not on the token file"
    assert st.VIA_NOT_FILE not in (st.VIA_FOLLOWS, st.VIA_ENV, st.VIA_ENV_FILE)
    world.assert_no_leak(capsys)


def test_on_a_token_file_host_a_blank_env_token_is_not_on_the_file(world, capsys):
    on_file(world, "c")
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": "  "}
    assert world.json()["seat"] == {"name": None, "via": st.VIA_NOT_FILE}
    world.assert_no_leak(capsys)


def test_not_on_the_file_holds_with_host_read_false_and_an_unknown_host(world, capsys):
    """The seat's route follows the setting, not the host read: the ledger
    names the host, and the seat is still not given that name."""
    on_file(world, "c")
    world.config(host_read=False)
    world.ledger({"to": "a"})
    world.env = {}
    data = world.json()
    assert data["host"]["name"] == "a"
    assert data["seat"] == {"name": None, "via": st.VIA_NOT_FILE}
    world.out = io.StringIO()
    on_file(world, "stranger")
    world.config(host_read=True)
    assert world.json()["seat"] == {"name": None, "via": st.VIA_NOT_FILE}
    world.assert_no_leak(capsys)


def test_on_a_token_file_host_an_env_token_is_unchanged(world, capsys):
    on_file(world, "c")
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["c"]}
    assert world.json()["seat"] == {"name": "c", "via": st.VIA_ENV_FILE}
    world.out = io.StringIO()
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[1] == \
        "seat:  c (carol@example.com)  (env token: from launch; moves at restart)"
    assert "NOT on the token file" not in world.out.getvalue()
    world.assert_no_leak(capsys)


def test_on_a_keychain_host_no_env_token_still_follows_the_host(world, capsys):
    world.env = {}
    assert world.status() == 0
    lines = world.out.getvalue().splitlines()
    assert lines[1] == "seat:  follows the host"
    assert "token file" not in world.out.getvalue()
    world.out = io.StringIO()
    assert world.json()["seat"] == {"name": "c", "via": st.VIA_FOLLOWS}
    world.assert_no_leak(capsys)


def test_seat_of_names_no_one_on_a_token_file_host_with_no_token():
    host = st.Who("c", "carol@example.com", st.VIA_TOKEN_FILE)
    registry = reg.Registry()
    assert st.seat_of(registry, host, {}, token_file=True) == \
        st.Who(None, None, st.VIA_NOT_FILE)
    assert st.seat_of(registry, host, {}, token_file=False) == \
        st.Who("c", "carol@example.com", st.VIA_FOLLOWS)


@pytest.mark.parametrize("case", ["not-private", "missing", "two-lines", "relative"])
def test_a_refused_token_file_with_no_env_token_is_still_not_on_the_file(world, case,
                                                                          capsys):
    """The seat's route follows the setting, not the file read: a token file
    that is refused leaves the host unreadable, and a seat with no env token
    is still NOT on the file. It never falls back to `follows the host`."""
    path = on_file(world, "c", 0o644 if case == "not-private" else 0o600)
    if case == "missing":
        path.unlink()
    elif case == "two-lines":
        path.write_text(world.tokens["c"] + "\n" + world.tokens["a"] + "\n")
    elif case == "relative":
        write_session_settings(world.home, {"account": {"token_file": "relative/oauth"}})
    world.env = {}
    assert world.status() == 0
    lines = world.out.getvalue().splitlines()
    assert lines[0].startswith("host:  token file: unreadable (")
    assert lines[1].startswith("seat:  NOT on the token file: no CLAUDE_CODE_OAUTH_TOKEN")
    assert "follows the host" not in world.out.getvalue()
    world.out = io.StringIO()
    assert world.json()["seat"] == {"name": None, "via": st.VIA_NOT_FILE}
    assert world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_no_token_shape_reaches_any_output_with_no_env_token_on_a_token_file_host(
        world, capsys):
    on_file(world, "c")
    world.env = {}
    for run in (world.status, lambda: world.status(as_json=True), world.list):
        assert run() == 0
        no_shape_in(world, capsys)
    world.assert_no_leak(capsys)


def test_not_on_the_file_text_is_two_lines_under_the_seat_label(world):
    """The second line is indented under the first line's text, past `seat:  `."""
    first, second = st.NOT_FILE_TEXT.split("\n")
    assert first.startswith("NOT on the token file")
    assert second.startswith(" " * len("seat:  ")) and not second.startswith(" " * 8)
    assert "inherit: [CLAUDE_CODE_OAUTH_TOKEN]" in second
    assert "tool shell may strip" in second
    on_file(world, "c")
    world.env = {}
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[1:3] == ["seat:  " + first, second]


@pytest.mark.parametrize("value", ["", 42, ["/a"]])
def test_any_refused_setting_value_with_no_env_token_is_not_on_the_file(world, value,
                                                                         capsys):
    write_session_settings(world.home, {"account": {"token_file": value}})
    world.keychain = kc.FakeKeychain(None)
    world.env = {}
    assert world.status() == 0
    lines = world.out.getvalue().splitlines()
    assert lines[0].startswith("host:  token file: unreadable (")
    assert lines[1].startswith("seat:  NOT on the token file")
    world.out = io.StringIO()
    assert world.json()["seat"] == {"name": None, "via": st.VIA_NOT_FILE}
    assert world.keychain.reads == 0
    world.assert_no_leak(capsys)


def test_a_refused_setting_value_with_an_env_token_moves_at_restart(world, capsys):
    """A set but refused value is still the token-file route: an env token
    there came from the file at launch, so it is never `immune to use`."""
    write_session_settings(world.home, {"account": {"token_file": "relative/oauth"}})
    world.keychain = kc.FakeKeychain(None)
    world.env = {"CLAUDE_CODE_OAUTH_TOKEN": world.tokens["a"]}
    assert world.json()["seat"] == {"name": "a", "via": st.VIA_ENV_FILE}
    world.assert_no_leak(capsys)


def test_an_unreadable_settings_layer_is_reported_and_still_exit_0(world, capsys):
    """A layer the reader refuses leaves the route unknown; on_token_file
    answers False, as documented, and the host line carries the refusal.
    The seat line on this path is not pinned here: the route is unknown."""
    write_session_settings(world.home, "{not json")
    world.env = {}
    assert st.on_token_file({}, world.home) is False
    assert world.status() == 0
    assert world.out.getvalue().splitlines()[0].startswith(
        "host:  token file: unreadable (settings:")
    world.assert_no_leak(capsys)


# --- the docs say what the code says ---------------------------------------------------------

DOCS_ROOT = Path(__file__).resolve().parent.parent


def _flat(path: Path) -> str:
    return " ".join(path.read_text().split())


def test_the_account_doc_example_plan_carries_the_session_start_notice():
    """docs/account.md's token-file plan block is the code's line, verbatim."""
    from sessiontools.account import use as u

    lines = (DOCS_ROOT / "docs" / "account.md").read_text().splitlines()
    assert u.START_NOTICE in lines
    assert "The `seats:`, `session start:` and `this seat` lines are printed again" in \
        _flat(DOCS_ROOT / "docs" / "account.md")


@pytest.mark.parametrize("doc", ["README.md", "docs/account.md"])
def test_each_doc_says_a_session_start_seat_does_not_follow_the_file(doc):
    text = _flat(DOCS_ROOT / doc)
    assert "does not follow the file unless its agent file says `inherit: " \
           "[CLAUDE_CODE_OAUTH_TOKEN]`" in text.replace("does NOT", "does not")


def test_the_account_doc_names_the_new_seat_reading_and_its_json():
    text = _flat(DOCS_ROOT / "docs" / "account.md")
    assert "`status` says `NOT on the token file` for it, never `follows the host`" in text
    assert f"`seat.via` is `{st.VIA_NOT_FILE}`, `seat.name` null" in text


def test_the_account_doc_says_a_refused_setting_is_still_the_token_file_route():
    text = _flat(DOCS_ROOT / "docs" / "account.md")
    assert ("A host whose `account.token_file` is set to a value that is refused is "
            "still a token-file host for this line") in text
