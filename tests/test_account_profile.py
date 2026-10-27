"""The display profile: `~/.claude.json`'s `oauthAccount.emailAddress`, which
Claude Code's /status and `claude auth status` name the account from.

`profile.read_profile` only ever reads the file: size-capped, a regular
file, a JSON object, and only `emailAddress` leaves it, when it is a short
email-shaped string. `status` prints it as its own line and in `--json`.
The same walls as `test_account_status` (its autouse guard is imported):
no `security`, `pass` or `claude`, no real exporter. Every address is an
`example.com` one, and every token synthetic.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from test_account_status import (  # noqa: F401 - the autouse walls
    World,
    _no_keychain_no_pass_no_claude_no_exporter,
    make_world,
    synthetic_token,
)

from sessiontools.account import profile as pf

#: Fields `~/.claude.json` carries beside the address; none may be shown.
OTHER = {"accountUuid": "uuid-NOT-SHOWN-1", "organizationName": "org-NOT-SHOWN-2",
         "displayName": "name-NOT-SHOWN-3"}


def claude_json(home: Path, data: object = None, *, email: str | None = "carol@example.com",
                raw: bytes | None = None) -> Path:
    """Write HOME/.claude.json: RAW bytes, else DATA as JSON, else a file whose
    oauthAccount names EMAIL beside the OTHER fields (no oauthAccount when
    EMAIL is None)."""
    path = home / ".claude.json"
    if raw is None:
        if data is None:
            data = {"numStartups": 3, "projects": {"/x": {"allowedTools": []}}}
            if email is not None:
                data["oauthAccount"] = {"emailAddress": email, **OTHER}
        raw = json.dumps(data).encode()
    path.write_bytes(raw)
    return path


# --- the helper -------------------------------------------------------------------------


def test_the_address_is_read_and_nothing_else(tmp_path):
    claude_json(tmp_path)
    assert pf.read_profile(tmp_path) == pf.Profile("carol@example.com")


def test_no_file_or_no_account_is_none_not_a_reason(tmp_path):
    assert pf.read_profile(tmp_path) == pf.Profile(None)
    claude_json(tmp_path, email=None)
    assert pf.read_profile(tmp_path) == pf.Profile(None)
    claude_json(tmp_path, {"oauthAccount": {"accountUuid": "u"}})
    assert pf.read_profile(tmp_path) == pf.Profile(None)


def test_home_defaults_to_the_users_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_json(tmp_path, email="ai@example.com")
    assert pf.read_profile().email == "ai@example.com"


@pytest.mark.parametrize("raw, why", [
    (b"{not json", "not JSON"),
    (b"\xff\xfe{}", "not JSON"),
    (b"[1, 2]", "not a JSON object"),
    (b'{"oauthAccount": "x"}', "oauthAccount is not an object"),
    (b'{"oauthAccount": {"emailAddress": 7}}', "not an email address"),
    (b'{"oauthAccount": {"emailAddress": "no-at-sign"}}', "not an email address"),
    (b'{"oauthAccount": {"emailAddress": "a@b.com\\u001b[2J"}}', "not an email address"),
])
def test_a_malformed_file_is_a_reason_and_its_value_is_never_shown(tmp_path, raw, why):
    claude_json(tmp_path, raw=raw)
    found = pf.read_profile(tmp_path)
    assert found.email is None and why in found.reason
    assert "no-at-sign" not in found.reason and "\x1b" not in found.reason


def test_a_token_shaped_address_is_refused_unshown(tmp_path):
    token = synthetic_token()
    claude_json(tmp_path, email=f"{token}@example.com")
    found = pf.read_profile(tmp_path)
    assert found.email is None and token not in found.reason


def test_an_overlong_address_is_refused(tmp_path):
    claude_json(tmp_path, email="a" * 300 + "@example.com")
    assert "not an email address" in pf.read_profile(tmp_path).reason


def test_a_file_over_the_cap_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "MAX_BYTES", 64)
    claude_json(tmp_path, {"pad": "x" * 100, "oauthAccount": {"emailAddress": "c@example.com"}})
    found = pf.read_profile(tmp_path)
    assert found.email is None and "larger than" in found.reason


def test_the_real_cap_is_32_mb():
    assert pf.MAX_BYTES == 32 * 1024 * 1024


def test_a_fifo_is_refused_without_hanging(tmp_path):
    os.mkfifo(tmp_path / ".claude.json")
    found = pf.read_profile(tmp_path)
    assert found.email is None and "not a regular file" in found.reason


def test_a_folder_at_the_path_is_refused(tmp_path):
    (tmp_path / ".claude.json").mkdir()
    assert "not a regular file" in pf.read_profile(tmp_path).reason


def test_the_file_is_never_written(tmp_path):
    path = claude_json(tmp_path)
    before = (path.read_bytes(), os.stat(path).st_mtime_ns, os.stat(path).st_mode)
    pf.read_profile(tmp_path)
    assert (path.read_bytes(), os.stat(path).st_mtime_ns, os.stat(path).st_mode) == before


# --- status: the profile line -----------------------------------------------------------


@pytest.fixture
def world(tmp_path) -> World:
    """`test_account_status`'s World: the host is `c`, carol@example.com."""
    return make_world(tmp_path)


def _status(world: World) -> list[str]:
    world.out = io.StringIO()
    assert world.status() == 0, world.err.getvalue()
    return world.out.getvalue().splitlines()


def _profile_json(world: World) -> dict:
    world.out = io.StringIO()
    return world.json()["profile"]


def test_status_names_the_profile_when_it_is_the_host_account(world, capsys):
    claude_json(world.home)                     # the host is c, carol@example.com
    lines = _status(world)
    assert lines[-1] == "profile: carol@example.com (~/.claude.json, display only)"
    assert lines[2].startswith("usage: ")      # the usage line stays where it was
    assert _profile_json(world) == {"email": "carol@example.com", "source": "~/.claude.json",
                                    "matches_host": True, "reason": None}
    world.assert_no_leak(capsys)


def test_status_marks_a_profile_that_differs_from_the_host_account(world, capsys):
    claude_json(world.home, email="AI@example.com")
    assert _status(world)[-1] == ("profile: AI@example.com (~/.claude.json, display only)"
                                  "  <- differs from the host account")
    assert _profile_json(world)["matches_host"] is False
    world.assert_no_leak(capsys)


def test_a_profile_matches_whatever_the_case_of_the_address(world, capsys):
    claude_json(world.home, email="Carol@Example.com")
    assert "differs" not in _status(world)[-1]
    assert _profile_json(world)["matches_host"] is True
    world.assert_no_leak(capsys)


def test_status_with_no_profile_says_so(world, capsys):
    assert _status(world)[-1] == "profile: none (~/.claude.json names no account)"
    assert _profile_json(world) == {"email": None, "source": "~/.claude.json",
                                    "matches_host": None, "reason": None}
    world.assert_no_leak(capsys)


def test_status_with_a_malformed_profile_reports_it_exit_0(world, capsys):
    claude_json(world.home, raw=b"{oops")
    assert _status(world)[-1] == "profile: unreadable (~/.claude.json: not JSON)"
    data = _profile_json(world)
    assert data["email"] is None and data["matches_host"] is None
    assert data["reason"] == "not JSON"
    world.assert_no_leak(capsys)


def test_status_with_a_huge_profile_reports_it(world, monkeypatch, capsys):
    monkeypatch.setattr(pf, "MAX_BYTES", 64)
    claude_json(world.home, {"pad": "x" * 100})
    assert _status(world)[-1].startswith("profile: unreadable (~/.claude.json: larger than")
    world.assert_no_leak(capsys)


def test_with_the_host_unknown_a_profile_neither_matches_nor_differs(tmp_path, capsys):
    world = make_world(tmp_path, host="stranger")
    claude_json(world.home)
    assert _status(world)[-1] == "profile: carol@example.com (~/.claude.json, display only)"
    assert _profile_json(world)["matches_host"] is None
    world.assert_no_leak(capsys)


def test_status_shows_only_the_address_from_the_file(world, capsys):
    path = claude_json(world.home)
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    text = "\n".join(_status(world))
    world.out = io.StringIO()
    assert world.status(as_json=True) == 0
    for value in OTHER.values():
        assert value not in text and value not in world.out.getvalue()
    assert "numStartups" not in text + world.out.getvalue()
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before
    world.assert_no_leak(capsys)
