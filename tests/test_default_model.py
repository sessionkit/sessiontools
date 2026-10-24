"""`sessiontools.default_model`: the pin's core writer.

Every test runs under the conftest's temporary HOME and state dir, with
CLAUDE_CONFIG_DIR scrubbed; a guard below asserts the target settings.json
lies under the test's temp tree, so no test reaches a real one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from settings_support import write_session_settings

from sessiontools import default_model as dm
from sessiontools.window import WINDOWS

SAMPLE = (
    '{\n'
    '    "env": {\n'
    '      "ANTHROPIC_MODEL": "claude-sonnet-5",\n'
    '      "X": "caf\\u00e9 été"\n'
    '    },\n'
    '  "model":   "sonnet" ,\n'
    '  "modelSettings": {"claude-opus-5-5": {"model": "keep"}},\n'
    '\t"permissions": {"allow": ["Bash(ls:*)"]}\n'
    '}\n'
)


@pytest.fixture(autouse=True)
def _under_tmp(tmp_path_factory):
    """The guard: whatever the core would write lies under the temp tree."""
    base = tmp_path_factory.getbasetemp().resolve()
    assert dm.settings_path().resolve().is_relative_to(base)
    assert dm.state_dir().resolve().is_relative_to(base)


@pytest.fixture
def home() -> Path:
    return Path(os.environ["HOME"])


@pytest.fixture
def claude(home) -> Path:
    d = home / ".claude"
    d.mkdir()
    return d


def pin(home: Path, value) -> None:
    write_session_settings(home, {"model": {"default": value}})


def put(claude: Path, text: str | bytes) -> Path:
    p = claude / "settings.json"
    p.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
    return p


def state_entries() -> list[str]:
    d = dm.state_dir()
    return sorted(os.listdir(d)) if d.exists() else []


def log_lines() -> list[dict]:
    p = dm.state_dir() / "log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# --- unset is a no-op ---------------------------------------------------------

UNSET = [
    pytest.param(None, id="no-session-json"),
    pytest.param({}, id="empty"),
    pytest.param({"model": {}}, id="empty-block"),
    pytest.param({"model": {"default": None}}, id="null"),
    pytest.param({"model": {"default": ""}}, id="empty-string"),
    pytest.param({"model": {"default": 7}}, id="number"),
    pytest.param({"model": "opus"}, id="block-not-a-map"),
    pytest.param('{"model": {"default": "opus"', id="does-not-parse"),
]


@pytest.mark.parametrize("data", UNSET)
def test_unset_leaves_a_rewritten_model_alone(home, claude, data, monkeypatch):
    if data is not None:
        write_session_settings(home, data)
    p = put(claude, '{"model": "fable"}\n')
    os.utime(p, ns=(1_000_000_000, 1_000_000_000))
    before = p.read_bytes(), p.stat().st_mtime_ns

    def no_read(*_a, **_k):
        raise AssertionError("settings.json was looked at while unset")
    monkeypatch.setattr(dm, "settings_path", no_read)

    out = dm.enforce()
    assert (out.code, out.state) == (0, "unset")
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before
    assert not dm.state_dir().exists()          # no backup, no log


def test_unset_says_what_it_found(home):
    write_session_settings(home, {"model": {"default": 7}})
    assert dm.setting().value is None
    assert "7" in dm.setting().why


# --- restore a present key ----------------------------------------------------

def test_a_rewritten_model_is_put_back_byte_identical_outside_the_value(home, claude):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    old = p.read_bytes()
    out = dm.enforce()
    assert (out.code, out.state, out.before) == (0, "replaced", "sonnet")
    new = p.read_bytes()
    start = old.index(b'"sonnet" ,')
    assert new[:start] == old[:start]
    assert new[start:start + len(b'"opus"')] == b'"opus"'
    assert new[start + len(b'"opus"'):] == old[start + len(b'"sonnet"'):]
    data = json.loads(new)
    assert data["model"] == "opus"
    assert data["env"]["ANTHROPIC_MODEL"] == "claude-sonnet-5"
    assert data["modelSettings"]["claude-opus-5-5"]["model"] == "keep"
    [b] = dm.backups()
    assert b.read_bytes() == old
    [line] = log_lines()
    assert line["before"] == "sonnet" and line["after"] == "opus"
    assert line["pathway"] == "replaced" and line["backup"] == str(b)
    assert line["file"] == str(p)
    assert "backup" in out.message and '"sonnet" -> "opus"' in out.message


@pytest.mark.parametrize("value", ["null", "7", "true"])
def test_a_scalar_model_is_replaced_whole(home, claude, value):
    pin(home, "haiku")
    p = put(claude, f'{{"a": 1, "model": {value}}}')
    assert dm.enforce().state == "replaced"
    assert p.read_text() == '{"a": 1, "model": "haiku"}'


def test_in_sync_writes_nothing(home, claude):
    pin(home, "opus")
    p = put(claude, SAMPLE.replace('"sonnet"', '"opus"'))
    os.utime(p, ns=(1_000_000_000, 1_000_000_000))
    before = p.read_bytes(), p.stat().st_mtime_ns
    out = dm.enforce()
    assert (out.code, out.state) == (0, "in-sync")
    assert (p.read_bytes(), p.stat().st_mtime_ns) == before
    assert state_entries() == []


def test_dry_run_plans_and_writes_nothing(home, claude):
    pin(home, "opus")
    p = put(claude, SAMPLE)
    old = p.read_bytes()
    out = dm.enforce(dry_run=True)
    assert (out.code, out.state) == (0, "planned")
    assert "would set" in out.message
    assert p.read_bytes() == old
    assert not dm.state_dir().exists()


# --- insert a missing key -----------------------------------------------------

def test_a_missing_model_is_inserted_first_in_the_files_own_style(home, claude):
    pin(home, "opus")
    old = '{\n    "env": {"A": "1"},\n    "theme": "dark"\n}\n'
    p = put(claude, old)
    out = dm.enforce()
    assert (out.state, out.before) == ("inserted", None)
    assert p.read_text() == '{\n    "model": "opus",\n    "env": {"A": "1"},\n    "theme": "dark"\n}\n'
    assert log_lines()[0]["pathway"] == "inserted"
    assert "absent" in out.message


def test_a_compact_one_line_object_gets_a_compact_insert(home, claude):
    pin(home, "opus")
    p = put(claude, '{"a":1}')
    dm.enforce()
    assert p.read_text() == '{"model":"opus","a":1}'


def test_crlf_is_preserved(home, claude):
    pin(home, "opus")
    p = put(claude, '{\r\n  "a": 1\r\n}\r\n')
    dm.enforce()
    assert p.read_bytes() == b'{\r\n  "model": "opus",\r\n  "a": 1\r\n}\r\n'


@pytest.mark.parametrize("old,new", [
    ("{}", '{\n  "model": "opus"\n}'),
    ("{}\n", '{\n  "model": "opus"\n}\n'),
    ("  { }\n", '  {\n  "model": "opus"\n}\n'),
    ("{\r\n}\r\n", '{\r\n  "model": "opus"\r\n}\r\n'),
])
def test_an_empty_object_gets_the_documented_text(home, claude, old, new):
    pin(home, "opus")
    p = put(claude, old)
    assert dm.enforce().state == "inserted"
    assert p.read_text(newline="") == new


def test_an_absent_file_in_an_existing_dir_is_created(home, claude):
    pin(home, "sonnet")
    out = dm.enforce()
    p = claude / "settings.json"
    assert (out.code, out.state, out.backup) == (0, "created", None)
    assert p.read_text() == '{\n  "model": "sonnet"\n}\n'
    assert (p.stat().st_mode & 0o777) == 0o600
    assert dm.backups() == []
    assert log_lines()[0]["pathway"] == "created"


# --- refusals: exit 3, nothing written ---------------------------------------

@pytest.mark.parametrize("value", ["fable", "Fable", "claude-fable-5-1", "FABLE-6", "claude-fable-5"])
def test_a_fable_pin_is_refused(home, claude, value, monkeypatch):
    monkeypatch.setenv("AGENT_ALLOW_FABLE", "1")     # no override for a pin
    pin(home, value)
    p = put(claude, '{"model": "sonnet"}')
    out = dm.enforce()
    assert (out.code, out.state) == (3, "refused")
    assert "Fable" in out.message
    assert p.read_text() == '{"model": "sonnet"}'
    assert not dm.state_dir().exists()


@pytest.mark.parametrize("value", ["opsu", " opus", "default", "claude-opus-9"])
def test_a_pin_off_the_list_is_refused(home, claude, value):
    pin(home, value)
    p = put(claude, '{"model": "sonnet"}')
    out = dm.enforce()
    assert (out.code, out.state) == (3, "refused")
    assert "default-model list" in out.message
    assert p.read_text() == '{"model": "sonnet"}'


@pytest.mark.parametrize("text,why", [
    ('{"model": "sonnet",', "does not parse"),
    ('// c\n{"model": "sonnet"}', "does not parse"),
    ('{"model": "sonnet", "model": "haiku"}', "more than one"),
    ('{"model": {"default": "x"}}', "object or array"),
    ('{"model": ["x"]}', "object or array"),
    ('["model"]', "not a JSON object"),
    (b'\xef\xbb\xbf{"model": "sonnet"}', "byte-order mark"),
    (b'{"model": "s\xff"}', "not UTF-8"),
])
def test_a_file_we_cannot_vouch_for_is_refused(home, claude, text, why):
    pin(home, "opus")
    p = put(claude, text)
    old = p.read_bytes()
    out = dm.enforce()
    assert (out.code, out.state) == (3, "refused")
    assert why in out.message
    assert p.read_bytes() == old
    assert not dm.state_dir().exists()


def test_a_nested_model_alone_is_not_the_top_level_one(home, claude):
    pin(home, "opus")
    p = put(claude, '{"env": {"model": "x"}}')
    dm.enforce()
    assert json.loads(p.read_text()) == {"model": "opus", "env": {"model": "x"}}


def test_an_absent_config_dir_is_refused_and_not_created(home):
    pin(home, "opus")
    out = dm.enforce()
    assert (out.code, out.state) == (3, "refused")
    assert "no Claude Code config dir" in out.message
    assert not (home / ".claude").exists()


def test_claude_config_dir_moves_the_target(home, tmp_path, monkeypatch):
    other = tmp_path / "cfg"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    pin(home, "opus")
    assert dm.enforce().state == "created"
    assert json.loads((other / "settings.json").read_text()) == {"model": "opus"}


def test_unreadable_is_exit_5(home, claude):
    pin(home, "opus")
    (claude / "settings.json").mkdir()
    out = dm.enforce()
    assert (out.code, out.state) == (5, "unreadable")


# --- changed under us ---------------------------------------------------------

def test_a_file_changed_once_under_us_is_retried_and_put_back(home, claude):
    pin(home, "opus")
    p = put(claude, '{"model": "sonnet"}')
    calls, slept = [], []

    def writer(target):
        calls.append(target)
        if len(calls) == 1:
            target.write_text('{"model": "haiku", "b": 2}')
    out = dm.enforce(between=writer, sleep=slept.append)
    assert (out.code, out.state, out.before) == (0, "replaced", "haiku")
    assert json.loads(p.read_text()) == {"model": "opus", "b": 2}
    assert len(calls) == 2 and slept == [dm.BACKOFF[0]]


def test_a_file_that_keeps_changing_is_refused_after_the_passes(home, claude):
    pin(home, "opus")
    p = put(claude, '{"model": "sonnet"}')
    n = []

    def writer(target):
        n.append(1)
        target.write_text('{"model": "haiku"' + " " * len(n) + "}")
    slept = []
    out = dm.enforce(between=writer, sleep=slept.append)
    assert (out.code, out.state) == (3, "refused")
    assert "changed while we read it" in out.message
    assert len(n) == dm.PASSES and len(slept) == dm.PASSES - 1
    assert json.loads(p.read_text()) == {"model": "haiku"}     # theirs, not ours
    assert log_lines() == []


# --- symlink, backups ---------------------------------------------------------

def test_a_symlinked_settings_json_is_written_at_its_target(home, claude, tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text('{"model": "sonnet"}\n')
    link = claude / "settings.json"
    link.symlink_to(real)
    pin(home, "opus")
    assert dm.enforce().state == "replaced"
    assert link.is_symlink() and os.readlink(link) == str(real)
    assert real.read_text() == '{"model": "opus"}\n'
    assert dm.backups()[0].read_text() == '{"model": "sonnet"}\n'


def test_the_mode_is_kept(home, claude):
    pin(home, "opus")
    p = put(claude, '{"model": "sonnet"}')
    p.chmod(0o640)
    dm.enforce()
    assert (p.stat().st_mode & 0o777) == 0o640


def test_the_backup_ring_keeps_the_newest_ten(home, claude):
    pin(home, "opus")
    p = claude / "settings.json"
    stray = dm.state_dir() / "backups" / "notes.txt"
    for i in range(12):
        p.write_text(f'{{"model": "sonnet", "i": {i}}}')
        assert dm.enforce(clock=lambda: 2_000_000_000.0).state == "replaced"
        if i == 0:
            stray.write_text("not ours")
    kept = dm.backups()
    assert len(kept) == dm.BACKUPS_KEPT
    assert [json.loads(b.read_text())["i"] for b in kept] == list(range(2, 12))
    assert kept[-1].name.endswith("-12")
    assert stray.exists()
    assert all((b.stat().st_mode & 0o777) == 0o600 for b in kept)
    assert len(log_lines()) == 12


def test_a_backup_that_cannot_land_writes_nothing(home, claude):
    pin(home, "opus")
    p = put(claude, '{"model": "sonnet"}')
    dm.state_dir().mkdir(parents=True)
    (dm.state_dir() / "backups").write_text("a file, not a dir")
    out = dm.enforce()
    assert (out.code, out.state) == (4, "failed")
    assert p.read_text() == '{"model": "sonnet"}'


# --- the list and Fable ------------------------------------------------------

def test_the_list_is_the_aliases_then_every_window_id():
    names = [m.name for m in dm.models()]
    assert names == ["opus", "sonnet", "haiku", *WINDOWS]
    for m in dm.models():
        assert m.pinnable is (not dm.is_fable(m.name))
        assert (m.why == "") is m.pinnable
    assert any(not m.pinnable for m in dm.models())


def test_the_fable_rule_is_the_start_gates():
    from sessiontools.start.policy import _is_fable_model
    for v in ["fable", "Fable", "claude-fable-5-1", "FABLE-6", "opus", "claude-opus-5-5", "", None, 7]:
        assert dm.is_fable(v) is _is_fable_model(v)


# --- the edit itself ------------------------------------------------------------

@pytest.mark.parametrize("text", [
    SAMPLE, '{"a":1}', "{}", '{"model": null}', ' \n{ "x" : [1, {"model": 2}] , "y":"}" }\n',
])
def test_plan_edit_changes_only_model(text):
    edit = dm.plan_edit(text, "opus")
    want = json.loads(text)
    want["model"] = "opus"
    assert json.loads(edit.new) == want


def test_the_core_does_not_load_yaml():
    probe = ("import sys, os, tempfile; d = tempfile.mkdtemp(); os.environ['HOME'] = d; "
             "from sessiontools import default_model as dm; dm.setting(); dm.models(); "
             "dm.plan_edit('{\"a\": 1}', 'opus'); dm.settings_path(); dm.state_dir(); "
             "print('yaml' in sys.modules, 'jsonschema' in sys.modules, "
             "'sessiontools.start' in sys.modules)")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            check=True, env=env)
    assert result.stdout.strip() == "False False False"
