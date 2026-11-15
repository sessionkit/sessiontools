"""The settle warning's settings (ST-78 do-002): the keys through the
cascade's host, agent, seat and session layers, `settle: null` as off, a
host `_locked`, the token reading, the rendered `session` schema, and ONE
home for every key name and default message.

Every test builds its own tree under a private HOME and SESSIONS_DIR, as
test_busy does. Nothing reads the live machine's settings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from settings_support import host as host_dir

from sessiontools import settings, settle, settle_settings
from sessiontools.settle_settings import Hard, Settle, Soft

ROOT = Path(__file__).resolve().parents[1]
SID = "11111111-2222-3333-4444-555555555555"
SS = settle_settings


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / ".session" / "sessions"))
    (tmp_path / ".session" / "sessions" / SID).mkdir(parents=True)
    return tmp_path


def host(tree: Path) -> Path:
    return host_dir(tree, "session") / "session.json"


def seat_file(tree: Path, name: str) -> Path:
    return tree / ".session" / "seats" / name / "settings" / "session.json"


def session_settings(tree: Path) -> Path:
    return tree / ".session" / "sessions" / SID / "settings"


def identity(tree: Path, **record) -> Path:
    """The birth record (session layer of `session`): agent, seat, agent_file."""
    return w(session_settings(tree) / "session.json", record)


def agent_md(tree: Path, settle_yaml: str) -> Path:
    """An agent file whose `settings:` front matter sets SETTLE_YAML under
    `session.settle`, indented two spaces per level from `settle:`."""
    body = "\n".join("      " + x for x in settle_yaml.strip("\n").splitlines())
    return w(tree / "agents" / "alice.md",
             f"---\nname: alice\nsettings:\n  session:\n    settle:\n{body}\n---\n# alice\n")


# --- the four layers ----------------------------------------------------------------

def test_no_layer_sets_it_is_off(tree):
    assert SS.read(SID) is None
    assert SS.values(SID) is None


def test_the_host_layer_alone_turns_the_soft_line_on_with_the_default_words(tree):
    w(host(tree), {"settle": {"soft": {"at": "250k", "every": 25000}}})
    assert SS.read(SID) == Settle(Soft(250_000, 25_000, SS.SOFT_SAY), None)


def test_host_agent_seat_and_session_each_override_the_one_above(tree):
    """Every key through every layer: the agent overrides the host's values
    AND words, the seat the agent's, the session the seat's."""
    w(host(tree), {"settle": {"soft": {"at": 200000, "every": 10000, "say": "host"},
                              "hard": {"at": 300000, "before_compact": 50000,
                                       "every": 5000, "say": "host hard"}}})
    md = agent_md(tree, """
soft:
  at: 250k
  say: agent {at}
hard:
  at: 350000
  say: agent now {left}
""")
    identity(tree, agent="alice", agent_file=str(md), seat="crew+st-78.worker")
    w(seat_file(tree, "crew+st-78.worker"), {"settle": {"soft": {"every": "25k"},
                                                        "hard": {"before_compact": 25000}}})
    w(session_settings(tree) / "session.d" / "10-test.json", {"settle": {"hard": {"every": 1000}}})
    assert SS.read(SID) == Settle(Soft(250_000, 25_000, "agent {at}"),
                                  Hard(350_000, 25_000, 1_000, "agent now {left}"))


def test_the_agent_layer_overrides_the_host_words_only(tree):
    w(host(tree), {"settle": {"hard": {"at": 350000}}})
    md = agent_md(tree, "hard:\n  say: 'vera: now, {used}.'")
    identity(tree, agent="alice", agent_file=str(md))
    assert SS.read(SID) == Settle(None, Hard(350_000, None, None, "vera: now, {used}."))


def test_a_seat_the_session_does_not_hold_is_not_read(tree):
    identity(tree, seat="mine")
    w(seat_file(tree, "someone-else"), {"settle": {"soft": {"at": 1}}})
    assert SS.read(SID) is None


def test_the_birth_copy_is_the_session_layer_and_beats_the_seat(tree):
    """SESSION_SETTLE_AT at launch lands in session.d/50-env.json (the birth):
    there, it wins over every seat, as the docs must say."""
    identity(tree, seat="s1")
    w(seat_file(tree, "s1"), {"settle": {"soft": {"at": 400000}}})
    w(session_settings(tree) / "session.d" / settings.ENV_DROP_IN,
      {"settle": {"soft": {"at": "250k"}}})
    assert SS.read(SID).soft.at == 250_000


# --- null is off; _locked pins ---------------------------------------------------

def test_null_in_a_lower_layer_turns_it_off(tree):
    identity(tree, seat="s1")
    w(host(tree), {"settle": {"soft": {"at": 250000}, "hard": {"at": 350000}}})
    w(seat_file(tree, "s1"), {"settle": None})
    assert SS.values(SID) is None
    assert SS.read(SID) is None


def test_after_null_a_layer_below_sets_it_again_from_scratch(tree):
    """null clears the block, so the host's other keys do not come back."""
    identity(tree, seat="s1")
    w(host(tree), {"settle": {"soft": {"at": 250000, "every": 25000}}})
    w(seat_file(tree, "s1"), {"settle": None})
    w(session_settings(tree) / "session.d" / "10-test.json", {"settle": {"soft": {"at": "300k"}}})
    assert SS.read(SID) == Settle(Soft(300_000, None, SS.SOFT_SAY), None)


def test_an_empty_block_clears_nothing(tree):
    identity(tree, seat="s1")
    w(host(tree), {"settle": {"soft": {"at": 250000}}})
    w(seat_file(tree, "s1"), {"settle": {}})
    assert SS.read(SID).soft.at == 250_000


def test_a_host_lock_pins_the_block_against_every_layer_below(tree):
    w(host(tree), {"settle": {"hard": {"at": 350000}}, "_locked": ["settle"]})
    md = agent_md(tree, "hard:\n  at: 100000")
    identity(tree, agent="alice", agent_file=str(md), seat="s1")
    w(seat_file(tree, "s1"), {"settle": None})
    w(session_settings(tree) / "session.d" / "10-test.json", {"settle": {"soft": {"at": 1}}})
    assert SS.read(SID) == Settle(None, Hard(350_000, None, None, SS.HARD_SAY))


def test_a_host_lock_on_null_is_off_for_good(tree):
    identity(tree, seat="s1")
    w(host(tree), {"settle": None, "_locked": ["settle"]})
    w(seat_file(tree, "s1"), {"settle": {"soft": {"at": 250000}}})
    assert SS.read(SID) is None


def test_a_lock_on_one_key_leaves_the_others_to_the_layers(tree):
    identity(tree, seat="s1")
    w(host(tree), {"settle": {"hard": {"at": 350000}}, "_locked": ["settle.hard.at"]})
    w(seat_file(tree, "s1"), {"settle": {"hard": {"at": 1, "before_compact": 25000}}})
    assert SS.read(SID).hard == Hard(350_000, 25_000, None, SS.HARD_SAY)


def test_a_layer_that_is_not_json_is_off_never_an_error(tree):
    w(host(tree), "{not json")
    assert SS.read(SID) is None


def test_without_yaml_the_agent_layer_drops_and_the_rest_still_count(tree, monkeypatch):
    identity(tree, agent="alice", seat="s1")

    def no_yaml(*_a, **_k):
        raise ModuleNotFoundError("No module named 'yaml'", name="yaml")

    monkeypatch.setattr(settings, "agent_settings", no_yaml)
    w(seat_file(tree, "s1"), {"settle": {"soft": {"at": 3}}})
    assert SS.read(SID).soft.at == 3


# --- parse: what turns a line on ---------------------------------------------------

@pytest.mark.parametrize("block", [None, [], "250k", 250000, {}, {"soft": {}},
                                   {"soft": {"every": 25000}}, {"hard": {"every": 1}},
                                   {"soft": {"at": "lots"}}, {"soft": None, "hard": None}])
def test_off_shapes(block):
    assert SS.parse(block) is None


def test_a_hard_line_from_before_compact_alone():
    assert SS.parse({"hard": {"before_compact": "25k"}}) == Settle(
        None, Hard(None, 25_000, None, SS.HARD_SAY))


@pytest.mark.parametrize("say", ["", "   ", 7, None, ["x"]])
def test_words_that_are_not_text_are_the_default(say):
    assert SS.parse({"soft": {"at": 1, "say": say}}).soft.say == SS.SOFT_SAY


@pytest.mark.parametrize(("raw", "count"), [
    (250000, 250_000), ("250000", 250_000), ("250k", 250_000), ("250K", 250_000),
    (" 300k ", 300_000), ("1.5M", 1_500_000), ("1.5 m", 1_500_000), (250000.0, 250_000)])
def test_tokens_read_numbers_and_strings_alike(raw, count):
    assert SS.tokens(raw) == count


@pytest.mark.parametrize("raw", [None, "", " ", "k", "lots", 0, -5, "0", "-1k", True, False,
                                 float("inf"), "nan", [250000], {"at": 1}])
def test_tokens_refuse_what_is_not_an_amount(raw):
    assert SS.tokens(raw) is None


def test_no_env_threshold_reader_is_left_in_settle():
    """ST-78 do-007 retired `settle.threshold`, the last reader of
    SESSION_SETTLE_AT outside the birth copy: the variable is spelled only
    here (AT_ENV), and read only at birth."""
    assert not hasattr(settle, "threshold") and not hasattr(settle, "AT_ENV")
    assert SS.AT_ENV == "SESSION_SETTLE_AT"


# --- the schema --------------------------------------------------------------------

def validator():
    jsonschema = pytest.importorskip("jsonschema")
    schema = SS.schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.parametrize("data", [
    {}, {"settle": None}, {"settle": {}},
    {"settle": {"soft": {"at": 250000, "every": "25k", "say": "x {at}"}}},
    {"settle": {"soft": {"at": "250k"}, "hard": {"at": "350000", "before_compact": 25000}}},
    {"settle": {"soft": {"at": "1.5M"}}},
    {"operator": {"mailbox": "alice"}, "context": {"near_pct": 10}}])     # other keys: not refused
def test_the_schema_takes_250k_and_250000_alike_and_null(data):
    assert list(validator().iter_errors(data)) == []


@pytest.mark.parametrize("data", [
    {"settle": "250k"}, {"settle": {"soft": {"at": 0}}}, {"settle": {"soft": {"at": "lots"}}},
    {"settle": {"soft": {"at": True}}}, {"settle": {"hard": {"say": ""}}},
    {"settle": {"hard": {"every": -1}}}, {"settle": {"soft": []}}])
def test_the_schema_names_a_bad_value(data):
    assert list(validator().iter_errors(data)) != []


def test_the_schema_pattern_agrees_with_the_reading():
    """What the schema's string form accepts, `tokens` reads (above zero)."""
    pattern = re.compile(SS.TOKENS_PATTERN)
    for raw in ("250000", "250k", " 300K ", "1.5M", "1.5 m"):
        assert pattern.match(raw) and SS.tokens(raw)


def test_the_schema_names_the_two_variables_on_the_soft_keys(tree):
    w(host_dir(tree, "session") / ".session.schema.json", SS.schema())
    named = {k.variable: k.path for k in settings.env_keys("session")}
    assert named == {SS.AT_ENV: SS.path(SS.SOFT, SS.AT), SS.SAY_ENV: SS.path(SS.SOFT, SS.SAY)}


def test_the_schema_carries_the_default_words_from_the_constants():
    props = SS.schema()["properties"][SS.ROOT]["properties"]
    assert props[SS.SOFT]["properties"][SS.SAY]["default"] == SS.SOFT_SAY
    assert props[SS.HARD]["properties"][SS.SAY]["default"] == SS.HARD_SAY
    assert SS.schema()["title"] == SS.TOOL == "session"


def test_the_two_default_messages_carry_their_figures():
    assert "{at}" in SS.SOFT_SAY and "{used}" in SS.SOFT_SAY
    assert "{used}" in SS.HARD_SAY and "{left}" in SS.HARD_SAY
    for say in (SS.SOFT_SAY, SS.HARD_SAY):
        assert "\n" not in say and "`" not in say and "respawn" not in say


# --- ONE home: each name and each message spelled once --------------------------------

def _code_lines() -> list[tuple[Path, str]]:
    return [(p, line) for p in sorted((ROOT / "src").rglob("*.py"))
            for line in p.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("name", [SS.ROOT, SS.SOFT, SS.HARD, SS.BEFORE_COMPACT, SS.EVERY,
                                  SS.SAY, SS.AT_ENV, SS.SAY_ENV])
def test_each_key_name_is_spelled_once_in_the_source(name):
    """A rename is one line: no other line in src/ writes the name as a string."""
    hits = [(p.name, line.strip()) for p, line in _code_lines() if f'"{name}"' in line]
    assert len(hits) == 1, hits
    assert hits[0][0] == "settle_settings.py"


@pytest.mark.parametrize("words", ["You're past {at}", "before compaction"])
def test_each_default_message_is_spelled_once_in_the_source(words):
    hits = [p.name for p, line in _code_lines() if words in line]
    assert hits == ["settle_settings.py"]


def test_the_tool_is_on_the_cascade():
    assert SS.TOOL in settings.TOOLS
    assert settings.OWN_SCHEMAS[SS.TOOL] == SS.__name__
