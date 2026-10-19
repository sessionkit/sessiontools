"""`band` (ST-26 do-004, design section 6): ok / near / over / null against
the settle threshold, with the near cut read through the settings cascade
under one key, a named default of 20, and no environment variable.

Every settings tree here is a temp HOME; nothing reads the real one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from settings_support import host

from sessiontools import band as BAND
from sessiontools.context_parts import render as R
from sessiontools.context_parts.breakdown import parts

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "context_parts" / "synthetic.jsonl"
SID = "0d4c1e5a-1111-4222-8333-444455556666"
SETTLE = "SESSION_SETTLE_AT"


def nested(dotted: str, value) -> dict:
    """{"a": {"b": value}} for "a.b": the key exactly as NEAR_KEY names it."""
    out: dict = value
    for part in reversed(dotted.split(".")):
        out = {part: out}
    return out


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path):
    """A temp HOME: (env, host file, session file) for the `session` tool."""
    env = {"HOME": str(tmp_path)}
    hostfile = host(tmp_path, BAND.NEAR_TOOL) / f"{BAND.NEAR_TOOL}.json"
    session = (tmp_path / ".session" / "sessions" / SID / "settings"
               / f"{BAND.NEAR_TOOL}.json")
    return env, hostfile, session


# ---------- the constants -------------------------------------------------------
def test_the_key_and_the_default_are_named_once():
    assert BAND.NEAR_KEY == "context.near_pct"
    assert BAND.NEAR_TOOL == "session"
    assert BAND.DEFAULT_NEAR_PCT == 20
    src = (ROOT / "src" / "sessiontools" / "band.py").read_text(
        encoding="utf-8")
    assert src.count('"context.near_pct"') == 1             # the key, in one constant
    assert src.count("DEFAULT_NEAR_PCT = 20\n") == 1        # the default, in one line


# ---------- band(): each band, from a fixture's own window -----------------------
def used() -> int:
    return parts(FIXTURE).anchors.used


@pytest.mark.parametrize("threshold, want", [
    (lambda u: u // 2, BAND.OVER),                 # well past it
    (lambda u: u, BAND.OVER),                      # exactly at it
    (lambda u: int(u / 0.9), BAND.NEAR),           # 10% under: inside the 20% cut
    (lambda u: int(u / 0.8), BAND.NEAR),           # exactly 20% under: the edge is near
    (lambda u: u * 2, BAND.OK),                    # 50% under
])
def test_each_band_from_the_fixture_through_parts_json(threshold, want, tree):
    env, _host, _session = tree
    u = used()
    env = {**env, SETTLE: str(threshold(u))}
    got = BAND.band_for(u, SID, env)
    assert got == want
    doc = R.parts_json(parts(FIXTURE), band=got)
    assert doc["band"] == want and doc["used"] == u


def test_no_threshold_is_null(tree):
    env, _host, _session = tree
    assert BAND.band_for(used(), SID, env) is None
    doc = R.parts_json(parts(FIXTURE), band=BAND.band_for(used(), SID, env))
    assert doc["band"] is None and json.loads(json.dumps(doc))["band"] is None


@pytest.mark.parametrize("u, t", [(None, 1000), (500, None), (500, 0), (500, -5)])
def test_unknown_used_or_no_usable_threshold_is_null(u, t):
    assert BAND.band(u, t) is None


def test_the_threshold_is_settles_own_reading(tree):
    env, _host, _session = tree
    for raw, want in (("250k", 250_000), ("1.5M", 1_500_000), ("nope", None), ("", None)):
        assert BAND.threshold({**env, SETTLE: raw}) == want


# ---------- the near cut through the cascade -------------------------------------
def test_no_layer_sets_it_so_the_default(tree):
    env, _host, _session = tree
    assert BAND.near_pct(SID, env) == BAND.DEFAULT_NEAR_PCT == 20
    assert BAND.near_pct(None, env) == 20


def test_a_session_layer_file_overrides_the_default(tree):
    env, _host, session = tree
    write(session, nested(BAND.NEAR_KEY, 5))
    assert BAND.near_pct(SID, env) == 5
    # 10% under the threshold: near at the default 20, ok at a 5% cut.
    u = used()
    env = {**env, SETTLE: str(int(u / 0.9))}
    assert BAND.band(u, BAND.threshold(env), BAND.DEFAULT_NEAR_PCT) == BAND.NEAR
    assert BAND.band_for(u, SID, env) == BAND.OK


def test_the_session_layer_beats_the_host_layer(tree):
    env, host, session = tree
    write(host, nested(BAND.NEAR_KEY, 30))
    assert BAND.near_pct(SID, env) == 30
    assert BAND.near_pct(None, env) == 30            # no session: template + host
    write(session, nested(BAND.NEAR_KEY, 12.5))
    assert BAND.near_pct(SID, env) == 12.5
    assert BAND.near_pct(None, env) == 30


def test_a_seat_layer_named_by_the_session_counts(tree):
    env, _host, session = tree
    home = Path(env["HOME"])
    write(session.with_name("session.d") / "00-birth.json", {"seat": "st-26-worker"})
    write(home / ".session" / "seats" / "st-26-worker" / "settings" / "session.json",
          nested(BAND.NEAR_KEY, 7))
    assert BAND.near_pct(SID, env) == 7


def test_the_sessions_dir_root_is_the_cascades_own(tree, tmp_path):
    env, _host, _session = tree
    moved = tmp_path / "elsewhere"
    write(moved / SID / "settings" / "session.json", nested(BAND.NEAR_KEY, 3))
    assert BAND.near_pct(SID, {**env, "SESSIONS_DIR": str(moved)}) == 3


@pytest.mark.parametrize("value", ["10", True, -1, 100.5, None, [10], {"x": 1}])
def test_a_value_that_is_not_a_percentage_is_the_default(tree, value):
    env, _host, session = tree
    write(session, nested(BAND.NEAR_KEY, value))
    assert BAND.near_pct(SID, env) == BAND.DEFAULT_NEAR_PCT


@pytest.mark.parametrize("value", [0, 100, 33])
def test_the_edges_are_percentages(tree, value):
    env, _host, session = tree
    write(session, nested(BAND.NEAR_KEY, value))
    assert BAND.near_pct(SID, env) == value


def test_zero_means_no_near_band():
    assert BAND.band(999, 1000, near=0) == BAND.OK
    assert BAND.band(1000, 1000, near=0) == BAND.OVER


def test_an_unreadable_layer_file_is_the_default_not_an_error(tree):
    env, _host, session = tree
    write(session, "{not json")
    assert BAND.near_pct(SID, env) == BAND.DEFAULT_NEAR_PCT


def test_a_literal_dotted_key_is_not_the_setting(tree):
    """Files nest; the dotted spelling is how the constant names the path."""
    env, _host, session = tree
    write(session, {BAND.NEAR_KEY: 5})
    assert BAND.near_pct(SID, env) == BAND.DEFAULT_NEAR_PCT


# ---------- no environment variable -----------------------------------------------
class Recording(dict):
    """An environment that records every name read from it."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.read: list[str] = []

    def get(self, key, default=None):
        self.read.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.append(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.append(key)
        return super().__contains__(key)


def test_no_environment_variable_is_read_for_the_near_cut(tree):
    env, _host, session = tree
    write(session, nested(BAND.NEAR_KEY, 5))
    rec = Recording({**env, "SESSION_CONTEXT_NEAR_PCT": "50", "CONTEXT_NEAR_PCT": "50",
                     "NEAR_PCT": "50"})
    assert BAND.near_pct(SID, rec) == 5
    # Only the cascade's own roots are read: where HOME and the sessions are.
    assert set(rec.read) <= {"HOME", "SESSIONS_DIR"}, rec.read
    assert not [k for k in rec.read if "NEAR" in k.upper()]


def test_an_environment_value_alone_changes_nothing(tree):
    env, _host, _session = tree
    rec = Recording({**env, "SESSION_CONTEXT_NEAR_PCT": "1"})
    assert BAND.near_pct(SID, rec) == BAND.DEFAULT_NEAR_PCT


# ---------- the session id may be a prefix ------------------------------------------
def test_a_short_prefix_reads_the_same_session_layer_as_the_full_id(tree):
    """`session_dir` is a literal join; band resolves a prefix first, so an
    8-char sid is not silently the default (do-004 review F4)."""
    env, _host, session = tree
    write(session, nested(BAND.NEAR_KEY, 5))
    short = SID[:8]
    assert BAND.near_pct(short, env) == BAND.near_pct(SID, env) == 5
    assert BAND.near_pct(short.upper(), env) == 5    # as resolve_sid: case-blind
    u = used()
    env = {**env, SETTLE: str(int(u / 0.9))}         # near at 20, ok at 5
    assert BAND.band_for(u, short, env) == BAND.band_for(u, SID, env) == BAND.OK


def test_a_short_prefix_finds_the_seat_its_session_names(tree):
    env, _host, session = tree
    home = Path(env["HOME"])
    write(session.with_name("session.d") / "00-birth.json", {"seat": "st-26-worker"})
    write(home / ".session" / "seats" / "st-26-worker" / "settings" / "session.json",
          nested(BAND.NEAR_KEY, 7))
    assert BAND.near_pct(SID[:8], env) == 7


def test_an_ambiguous_prefix_is_the_default_never_a_guess(tree):
    env, _host, session = tree
    write(session, nested(BAND.NEAR_KEY, 5))
    twin = session.parents[2] / (SID[:8] + "-0000-4000-8000-000000000000") / "settings"
    write(twin / f"{BAND.NEAR_TOOL}.json", nested(BAND.NEAR_KEY, 9))
    assert BAND.near_pct(SID[:8], env) == BAND.DEFAULT_NEAR_PCT
    assert BAND.near_pct(SID, env) == 5                # the full id is still itself


def test_a_prefix_with_no_session_keeps_the_host_layer(tree):
    env, host, _session = tree
    write(host, nested(BAND.NEAR_KEY, 30))
    assert BAND.near_pct("ffffffff", env) == 30


# ---------- guards: what the band must not do, not which files a branch touched ----
def test_no_schema_names_the_near_key():
    """No schema is shipped for the key: the cascade reads it without one.
    Any other schema may come and go; none may claim `context.near_pct`."""
    leaf = BAND.NEAR_KEY.rsplit(".", 1)[1]
    for schema in sorted(ROOT.rglob("*.schema.json")):
        if ".venv" in schema.parts or "node_modules" in schema.parts:
            continue
        assert leaf not in schema.read_text(encoding="utf-8"), (
            f"{schema.relative_to(ROOT)} names {BAND.NEAR_KEY}")


def test_settings_settle_and_window_never_import_the_band():
    """The band reads settle and the settings cascade; neither (nor window)
    reaches back into context_parts."""
    for rel in ("settings.py", "settle.py", "window.py"):
        src = (ROOT / "src" / "sessiontools" / rel).read_text(encoding="utf-8")
        assert "context_parts" not in src, rel


def test_the_band_reads_no_settle_variable_of_its_own():
    """The threshold is `settle.threshold`'s reading, never re-read here."""
    src = (ROOT / "src" / "sessiontools" / "band.py").read_text(
        encoding="utf-8")
    assert f'"{SETTLE}"' not in src and f"'{SETTLE}'" not in src   # docs may name it
    assert "environ.get" not in src and "environ[" not in src
    assert "settle.threshold(" in src


@pytest.mark.parametrize("u, want", [(799, BAND.OK), (800, BAND.NEAR), (999, BAND.NEAR),
                                     (1000, BAND.OVER), (1001, BAND.OVER)])
def test_the_edges_of_the_bands_exactly(u, want):
    """At the default 20% cut of a 1000-token threshold, near starts AT 800."""
    assert BAND.band(u, 1000) == want
