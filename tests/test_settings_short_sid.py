"""ST-86: a SHORT session id given to `session settings` once read no session
dir, so the agent and seat layers vanished, exit 0, nothing said. Now a
unique prefix resolves to the full id, an ambiguous one is refused naming the
candidates, and an id with no dir is kept but said on stderr. Each case runs
against a tree built under a private HOME, as test_settings does."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessiontools import settings
from sessiontools.cli import main

SID = "cdc867da-02ef-0f27-3370-61dc5f06753f"
TWIN = "cdc8ffff-0000-0000-0000-000000000000"


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = tmp_path / ".session" / "sessions"
    monkeypatch.setenv("SESSIONS_DIR", str(r))
    w(r / SID / "settings" / "session.json", {"agent": "agent-a", "seat": "seat-a"})
    return r


def test_a_full_id_whose_dir_exists_is_itself(root):
    assert settings.resolve_sid(SID) == (SID, None)


def test_a_unique_prefix_resolves_to_the_full_id(root):
    assert settings.resolve_sid("cdc867da") == (SID, None)


def test_the_prefix_is_case_insensitive(root):
    assert settings.resolve_sid("CDC867DA") == (SID, None)


def test_an_ambiguous_prefix_is_refused_naming_both(root):
    (root / TWIN).mkdir()
    with pytest.raises(settings.SettingsError) as e:
        settings.resolve_sid("cdc8")
    assert SID in str(e.value) and TWIN in str(e.value)


def test_a_file_under_the_root_is_not_a_candidate(root):
    (root / "cdc8-not-a-dir").write_text("x")
    assert settings.resolve_sid("cdc8") == (SID, None)


def test_no_match_keeps_the_id_and_says_so(root):
    sid, note = settings.resolve_sid("deadbeef")
    assert sid == "deadbeef"
    assert note and "deadbeef" in note and str(root) in note


def test_a_missing_root_keeps_the_id_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "nowhere"))
    sid, note = settings.resolve_sid("cdc867da")
    assert sid == "cdc867da" and note


# --- the command: the reported repro, fixed -----------------------------------

def test_cli_short_id_reads_the_agent_and_seat(root, capsys):
    assert main(["settings", "cdc867da", "session", "--json"]) == 0
    io = capsys.readouterr()
    out = json.loads(io.out)
    assert (out["session"], out["agent"], out["seat"]) == (SID, "agent-a", "seat-a")
    assert io.err == ""


def test_cli_ambiguous_prefix_exits_two_naming_both(root, capsys):
    (root / TWIN).mkdir()
    assert main(["settings", "cdc8", "session", "--json"]) == 2
    io = capsys.readouterr()
    assert io.out == ""
    assert io.err.startswith("session settings: refused:")
    assert SID in io.err and TWIN in io.err


def test_cli_unknown_id_says_so_and_keeps_its_exit(root, capsys):
    assert main(["settings", "deadbeef", "session", "--json"]) == 0
    io = capsys.readouterr()
    out = json.loads(io.out)
    assert (out["session"], out["agent"], out["seat"]) == ("deadbeef", None, None)
    assert "no session directory for deadbeef" in io.err


# --- ST-123: an ambiguous prefix names at most AMBIGUOUS_SHOW candidates ------

def many(root: Path, n: int) -> list[str]:
    """`n` session dirs under `root` sharing the prefix `ab`, in sorted order."""
    sids = [f"ab{i:06x}-0000-0000-0000-000000000000" for i in range(n)]
    for s in sids:
        (root / s).mkdir(parents=True)
    return sids


def refusal(prefix: str) -> str:
    with pytest.raises(settings.SettingsError) as e:
        settings.resolve_sid(prefix)
    return str(e.value)


def test_over_the_cap_names_the_first_cap_and_the_total(root):
    cap = settings.AMBIGUOUS_SHOW
    sids = many(root, cap + 5)
    msg = refusal("ab")
    assert all(s in msg for s in sids[:cap])
    assert not any(s in msg for s in sids[cap:])
    last = msg.splitlines()[-1]
    assert last == (f"{cap + 5} sessions match ab, the first {cap} shown; "
                    f"name more of it")


def test_at_the_cap_names_every_candidate_as_before(root):
    cap = settings.AMBIGUOUS_SHOW
    sids = many(root, cap)
    assert refusal("ab") == (f"session id ab is ambiguous under {root}: "
                             f"{', '.join(sids)}; name more of it")


def test_under_the_cap_names_every_candidate_as_before(root):
    sids = many(root, 3)
    assert refusal("ab") == (f"session id ab is ambiguous under {root}: "
                             f"{', '.join(sids)}; name more of it")


def test_the_cap_follows_the_constant(root, monkeypatch):
    monkeypatch.setattr(settings, "AMBIGUOUS_SHOW", 2)
    sids = many(root, 3)
    msg = refusal("ab")
    assert sids[0] in msg and sids[1] in msg and sids[2] not in msg
    assert "3 sessions match ab, the first 2 shown" in msg


def test_cli_over_the_cap_exits_two_with_the_total(root, capsys):
    cap = settings.AMBIGUOUS_SHOW
    sids = many(root, cap + 1)
    assert main(["settings", "ab", "session", "--json"]) == 2
    io = capsys.readouterr()
    assert io.out == ""
    assert io.err.startswith("session settings: refused:")
    assert sids[cap - 1] in io.err and sids[cap] not in io.err
    assert f"{cap + 1} sessions match ab, the first {cap} shown" in io.err
