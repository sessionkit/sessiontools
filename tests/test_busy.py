"""The busy sound's settings (ST-66 do-002): `config` through the cascade, the
batch trigger's reading, and the session-long OFF cache.

Every test builds its own tree under a private HOME and SESSIONS_DIR, as
test_settings does. Nothing reads the live machine's settings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import ANCHOR_SUMMER
from settings_support import host as host_dir

from sessiontools import busy, settings

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "settings" / "sounds.schema.json"
SID = "11111111-2222-3333-4444-555555555555"


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


@pytest.fixture
def wav(tree: Path) -> Path:
    return w(tree / "sounds" / "dance.wav", "RIFF")


def host(tree: Path) -> Path:
    return host_dir(tree, "sounds") / "sounds.json"


def seat_file(tree: Path, name: str) -> Path:
    return tree / ".session" / "seats" / name / "settings" / "sounds.json"


def session_settings(tree: Path) -> Path:
    return tree / ".session" / "sessions" / SID / "settings"


def session_file(tree: Path) -> Path:
    return session_settings(tree) / "sounds.json"


def identity(tree: Path, **record) -> Path:
    return w(session_settings(tree) / "session.json", record)


def on(wav: Path, **busy_keys) -> dict:
    block = {"auto_arm": True, "file": str(wav), "delay": 8}
    block.update(busy_keys)
    return {"enabled": True, "busy": block}


# --- config: the ON case and every silent case of decision 1 -------------------

def test_all_set_is_the_sound(tree, wav):
    w(session_file(tree), on(wav))
    assert busy.config(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True)
    assert busy.batch_sound(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True)


def test_nothing_set_is_silent(tree):
    assert busy.config(SID) is None
    assert busy.batch_sound(SID) is None


@pytest.mark.parametrize("enabled", [False, None, "true", 1, "yes"])
def test_enabled_must_be_the_boolean_true(tree, wav, enabled):
    data = on(wav)
    if enabled is None:
        del data["enabled"]
    else:
        data["enabled"] = enabled
    w(session_file(tree), data)
    assert busy.config(SID) is None


@pytest.mark.parametrize("block", [None, "on", ["file"], True])
def test_busy_must_be_a_map(tree, block):
    data = {"enabled": True}
    if block is not None:
        data["busy"] = block
    w(session_file(tree), data)
    assert busy.config(SID) is None


@pytest.mark.parametrize("file", ["", "   ", 7, None, "relative/dance.wav", "/no/such.wav"])
def test_file_must_name_a_readable_file(tree, wav, file):
    data = on(wav)
    if file is None:
        del data["busy"]["file"]
    else:
        data["busy"]["file"] = file
    w(session_file(tree), data)
    assert busy.config(SID) is None


def test_a_directory_is_not_a_sound_file(tree, wav):
    w(session_file(tree), on(wav, file=str(wav.parent)))
    assert busy.config(SID) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 file")
def test_an_unreadable_file_is_silent(tree, wav):
    w(session_file(tree), on(wav))
    wav.chmod(0)
    try:
        assert busy.config(SID) is None
    finally:
        wav.chmod(0o644)


def test_tilde_expands_against_home(tree, wav):
    w(session_file(tree), on(wav, file="~/sounds/dance.wav"))
    assert busy.config(SID).file == tree / "sounds" / "dance.wav"


@pytest.mark.parametrize("delay", [-1, "8", True, False, [8], {"s": 8}])
def test_a_delay_that_is_set_must_be_a_number_at_or_over_zero(tree, wav, delay):
    w(session_file(tree), on(wav, delay=delay))
    assert busy.config(SID) is None


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_a_delay_that_is_not_finite_is_silent(tree, wav, text):
    # json.loads accepts these words; a layer file can carry them.
    raw = json.dumps(on(wav)).replace('"delay": 8', f'"delay": {text}')
    w(session_file(tree), raw)
    assert busy.config(SID) is None


@pytest.mark.parametrize("delay, want", [(0, 0.0), (0.25, 0.25), (8, 8.0)])
def test_delay_zero_and_fractions_are_valid(tree, wav, delay, want):
    w(session_file(tree), on(wav, delay=delay))
    assert busy.config(SID).delay == want


def test_an_unset_delay_is_a_sound_for_st90_but_off_for_the_batch(tree, wav):
    data = on(wav)
    del data["busy"]["delay"]
    w(session_file(tree), data)
    assert busy.config(SID) == busy.Sound(file=wav, delay=None, auto_arm=True)
    assert busy.batch_sound(SID) is None


@pytest.mark.parametrize("auto_arm", [False, None, "true", 1])
def test_config_reports_auto_arm_and_does_not_act_on_it(tree, wav, auto_arm):
    data = on(wav)
    if auto_arm is None:
        del data["busy"]["auto_arm"]
    else:
        data["busy"]["auto_arm"] = auto_arm
    w(session_file(tree), data)
    assert busy.config(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=False)
    assert busy.batch_sound(SID) is None


def test_a_layer_that_is_not_json_is_silence_never_an_error(tree, wav):
    w(host(tree), on(wav))
    w(session_file(tree), "{not json")
    assert busy.config(SID) is None
    assert busy.batch_sound(SID) is None
    assert busy.resolve_for_batch(SID) is None


def test_an_empty_sid_is_silent(tree, wav):
    w(host(tree), on(wav))
    assert busy.config("") is None
    assert busy.resolve_for_batch("") is None


# --- the cascade: layers ---------------------------------------------------------

def test_the_host_layer_alone_turns_it_on(tree, wav):
    w(host(tree), on(wav))
    assert busy.batch_sound(SID).delay == 8.0


def test_the_seat_layer_beats_the_host(tree, wav):
    identity(tree, agent=None, seat="crew+st-66.worker")
    w(host(tree), on(wav, delay=8))
    w(seat_file(tree, "crew+st-66.worker"), {"busy": {"delay": 2}})
    assert busy.config(SID).delay == 2.0


def test_the_seat_layer_can_switch_the_host_off(tree, wav):
    identity(tree, seat="crew+st-66.worker")
    w(host(tree), on(wav))
    w(seat_file(tree, "crew+st-66.worker"), {"enabled": False})
    assert busy.config(SID) is None


def test_a_seat_the_session_does_not_hold_is_not_read(tree, wav):
    identity(tree, seat="mine")
    w(seat_file(tree, "someone-else"), on(wav))
    assert busy.config(SID) is None


def test_the_session_layer_beats_the_seat(tree, wav):
    identity(tree, seat="s1")
    w(seat_file(tree, "s1"), on(wav, delay=2))
    w(session_settings(tree) / "sounds.d" / "10-test.json", {"busy": {"delay": 0.5}})
    assert busy.config(SID).delay == 0.5


def test_the_agent_layer_is_read_from_the_recorded_agent_file(tree, wav):
    md = w(tree / "agents" / "alice.md",
           f"---\nname: alice\nsettings:\n  sounds.enabled: true\n"
           f"  sounds.busy.file: {wav}\n  sounds.busy.delay: 8\n"
           f"  sounds.busy.auto_arm: true\n---\n# alice\n")
    identity(tree, agent="alice", agent_file=str(md))
    assert busy.batch_sound(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True)


def test_without_yaml_the_agent_layer_drops_and_the_rest_still_count(tree, wav, monkeypatch):
    identity(tree, agent="alice", seat="s1")

    def no_yaml(*_a, **_k):
        raise ModuleNotFoundError("No module named 'yaml'", name="yaml")

    monkeypatch.setattr(settings, "agent_settings", no_yaml)
    w(seat_file(tree, "s1"), on(wav, delay=3))
    assert busy.batch_sound(SID).delay == 3.0


def test_without_yaml_for_real_a_bare_interpreter_still_resolves(tree, wav):
    """The hook's `python3 -I` may have no PyYAML (homebrew 3.14.7 on a real
    host has none, measured). Block `yaml` in a fresh interpreter, as that one is."""
    md = w(tree / "agents" / "alice.md", "---\nname: alice\nsettings:\n  sounds.enabled: false\n---\n")
    identity(tree, agent="alice", agent_file=str(md))
    w(session_file(tree), on(wav, delay=4))
    code = (
        "import sys; sys.modules['yaml'] = None; sys.path.insert(0, sys.argv[1])\n"
        "from sessiontools import busy\n"
        "s = busy.batch_sound(sys.argv[2]); print(s.delay if s else 'OFF')\n"
    )
    env = {"HOME": str(tree), "SESSIONS_DIR": os.environ["SESSIONS_DIR"], "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "src"), SID],
                         env=env, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    # The agent layer (enabled: false) could not be read; the session layer's ON stands.
    assert out.stdout.strip() == "4.0"


def test_importing_busy_reaches_no_yaml_and_no_jsonschema():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import sessiontools.busy; "
            "print(sorted(m for m in ('yaml', 'jsonschema') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "src")],
                         capture_output=True, text=True, timeout=30, check=False)
    assert out.stdout.strip() == "[]", out.stderr


def test_config_does_not_check_the_schema(tree, wav, monkeypatch):
    """`resolve` would import jsonschema when a schema is installed; the hook
    must not pay that, so `config` merges without it."""
    w(host_dir(tree, "sounds") / "sounds.schema.json", SCHEMA.read_text())
    w(host(tree), on(wav))

    def boom(*_a, **_k):
        raise AssertionError("check_schema was called")

    monkeypatch.setattr(settings, "check_schema", boom)
    assert busy.batch_sound(SID) is not None


# --- the trigger (ST-90 do-001, design decision 1) ---------------------------------

def test_an_unset_trigger_is_batch_and_the_batch_sound_is_unchanged(tree, wav):
    w(session_file(tree), on(wav))
    assert busy.config(SID).trigger == "batch"
    assert busy.batch_sound(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True,
                                               trigger="batch")


def test_trigger_batch_set_explicitly_is_the_batch_sound(tree, wav):
    w(session_file(tree), on(wav, trigger="batch"))
    assert busy.batch_sound(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True)


@pytest.mark.parametrize("mode", ["landed", "finished"])
def test_landed_and_finished_are_a_sound_but_off_for_the_batch(tree, wav, mode):
    w(session_file(tree), on(wav, trigger=mode))
    assert busy.config(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True, trigger=mode)
    assert busy.batch_sound(SID) is None


@pytest.mark.parametrize("mode", ["landed", "finished"])
def test_landed_and_finished_make_the_arm_stamp_off(tree, wav, mode):
    w(session_file(tree), on(wav, trigger=mode))
    assert busy.resolve_for_batch(SID) is None
    assert off(tree).is_file()


@pytest.mark.parametrize("bad", ["landd", "Batch", "LANDED", " landed", "", 1, True,
                                 None, ["landed"], {"mode": "landed"}])
def test_a_bad_trigger_is_silent_in_every_mode_never_a_fallback_to_batch(tree, wav, bad):
    w(session_file(tree), on(wav, trigger=bad))
    assert busy.config(SID) is None
    assert busy.batch_sound(SID) is None
    assert busy.resolve_for_batch(SID) is None
    assert off(tree).is_file()


@pytest.mark.parametrize("mode", ["landed", "finished"])
def test_delay_is_optional_outside_batch_and_defaults_to_zero(tree, wav, mode):
    data = on(wav, trigger=mode)
    del data["busy"]["delay"]
    del data["busy"]["auto_arm"]
    w(session_file(tree), data)
    assert busy.config(SID) == busy.Sound(file=wav, delay=0.0, auto_arm=False, trigger=mode)


@pytest.mark.parametrize("mode", ["landed", "finished"])
def test_a_delay_set_outside_batch_is_honoured_and_a_bad_one_is_silent(tree, wav, mode):
    w(session_file(tree), on(wav, trigger=mode, delay=1.5))
    assert busy.config(SID).delay == 1.5
    w(session_file(tree), on(wav, trigger=mode, delay="1"))
    assert busy.config(SID) is None


def test_an_unset_delay_under_batch_stays_unset(tree, wav):
    data = on(wav, trigger="batch")
    del data["busy"]["delay"]
    w(session_file(tree), data)
    assert busy.config(SID).delay is None
    assert busy.batch_sound(SID) is None


def alice_md(tree: Path, wav: Path, trigger: str) -> Path:
    return w(tree / "agents" / "alice.md",
             f"---\nname: alice\nsettings:\n  sounds.enabled: true\n"
             f"  sounds.busy.file: {wav}\n  sounds.busy.trigger: {trigger}\n---\n# alice\n")


def test_the_agent_layer_trigger_is_the_default(tree, wav):
    pytest.importorskip("yaml")
    identity(tree, agent="alice", agent_file=str(alice_md(tree, wav, "landed")))
    assert busy.config(SID) == busy.Sound(file=wav, delay=0.0, auto_arm=False,
                                          trigger="landed")
    assert busy.batch_sound(SID) is None


def test_a_session_layer_trigger_beats_the_agents(tree, wav):
    pytest.importorskip("yaml")
    identity(tree, agent="alice", agent_file=str(alice_md(tree, wav, "landed")))
    w(session_file(tree), {"busy": {"trigger": "batch", "auto_arm": True, "delay": 8}})
    assert busy.batch_sound(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True,
                                               trigger="batch")


def test_a_session_layer_typo_silences_an_agents_good_trigger(tree, wav):
    pytest.importorskip("yaml")
    identity(tree, agent="alice", agent_file=str(alice_md(tree, wav, "batch")))
    w(session_file(tree), {"busy": {"trigger": "landd"}})
    assert busy.config(SID) is None


# --- the OFF cache -----------------------------------------------------------------

def off(tree: Path) -> Path:
    return tree / ".session" / "sessions" / SID / "sounds" / "busy" / "off"


def test_off_is_stamped_one_second_before_the_resolve_started(tree):
    started = (ANCHOR_SUMMER + timedelta(days=1)).timestamp() + 0.25
    assert busy.resolve_for_batch(SID, now=lambda: started) is None
    assert off(tree).is_file()
    assert off(tree).stat().st_mtime == pytest.approx(started - 1.0, abs=1e-3)


def test_off_lives_in_private_dirs(tree):
    busy.resolve_for_batch(SID)
    slot = off(tree).parent
    assert (slot.stat().st_mode & 0o777) == 0o700
    assert (slot.parent.stat().st_mode & 0o777) == 0o700
    assert (slot / ".gitignore").read_text() == "*\n"


def test_off_is_re_stamped_on_every_off_resolve(tree):
    busy.resolve_for_batch(SID, now=lambda: ANCHOR_SUMMER.timestamp())
    assert off(tree).stat().st_mtime == pytest.approx(
        (ANCHOR_SUMMER - timedelta(seconds=1)).timestamp(), abs=1e-3)
    busy.resolve_for_batch(SID, now=lambda: (ANCHOR_SUMMER + timedelta(days=1)).timestamp())
    assert off(tree).stat().st_mtime == pytest.approx(
        (ANCHOR_SUMMER + timedelta(days=1, seconds=-1)).timestamp(), abs=1e-3)


def test_on_removes_off(tree, wav):
    busy.resolve_for_batch(SID)
    assert off(tree).is_file()
    w(session_file(tree), on(wav))
    assert busy.resolve_for_batch(SID) == busy.Sound(file=wav, delay=8.0, auto_arm=True)
    assert not off(tree).exists()


def test_on_with_no_off_writes_nothing(tree, wav):
    w(session_file(tree), on(wav))
    assert busy.resolve_for_batch(SID) is not None
    assert not off(tree).parent.exists()


def test_auto_arm_false_is_off_for_the_batch_and_stamps_off(tree, wav):
    w(session_file(tree), on(wav, auto_arm=False))
    assert busy.resolve_for_batch(SID) is None
    assert off(tree).is_file()


def test_no_session_dir_writes_nothing_and_does_not_raise(tree, wav):
    other = "99999999-0000-0000-0000-000000000000"
    assert busy.resolve_for_batch(other) is None
    assert not (tree / ".session" / "sessions" / other).exists()


def test_a_layer_file_written_right_after_the_resolve_is_newer_to_sh(tree, wav):
    """The point of the back-date: `/bin/sh -nt` compares whole seconds, so a
    switch-on written in the same second as `off` must still read newer."""
    busy.resolve_for_batch(SID)
    w(session_file(tree), on(wav))      # same second, almost always
    newer = subprocess.run(["/bin/sh", "-c", '[ "$1" -nt "$2" ]', "sh",
                            str(session_file(tree)), str(off(tree))], check=False)
    assert newer.returncode == 0


def test_a_layer_file_older_than_the_resolve_does_not_read_newer(tree, wav):
    w(session_file(tree), {"enabled": False})
    past = time.time() - 5
    os.utime(session_file(tree), (past, past))
    busy.resolve_for_batch(SID)
    newer = subprocess.run(["/bin/sh", "-c", '[ "$1" -nt "$2" ]', "sh",
                            str(session_file(tree)), str(off(tree))], check=False)
    assert newer.returncode == 1


# --- the schema ------------------------------------------------------------------

def test_the_schema_accepts_the_design_example_and_refuses_bad_keys(wav):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    assert list(validator.iter_errors(on(wav))) == []
    assert list(validator.iter_errors({"busy": {"delay": 0.5}})) == []
    for mode in ("batch", "landed", "finished"):
        assert list(validator.iter_errors({"busy": {"trigger": mode}})) == [], mode
    for bad in ({"enabled": "yes"}, {"busy": {"delay": -1}}, {"busy": {"delay": "8"}},
                {"busy": {"file": ""}}, {"busy": {"auto_arm": 1}},
                {"busy": {"trigger": "landd"}}, {"busy": {"trigger": "Batch"}},
                {"busy": {"trigger": ["landed"]}}, {"busy": {"trigger": None}}):
        assert list(validator.iter_errors(bad)), bad


def test_the_schema_names_no_environment_variable(tree):
    """No `x-env` key: the birth must read no variable into `sounds`."""
    w(host_dir(tree, "sounds") / "sounds.schema.json", SCHEMA.read_text())
    assert settings.env_keys("sounds") == []


# --- the stop sound's settings (ST-236 design D2, D8) ------------------------------

def stop_on(path, **extra) -> dict:
    data = {"enabled": True, "stop": {"file": str(path)}}
    data.update(extra)
    return data


def stop_off(tree: Path) -> Path:
    return tree / ".session" / "sessions" / SID / "sounds" / "stop" / "off"


def test_stop_config_is_the_file_when_set(tree, wav):
    w(session_file(tree), stop_on(wav))
    assert busy.stop_config(SID) == wav


def test_stop_config_unset_is_silent(tree, wav):
    assert busy.stop_config(SID) is None
    w(session_file(tree), on(wav))                    # a busy sound alone
    assert busy.stop_config(SID) is None


@pytest.mark.parametrize("enabled", [False, None, "true", 1, "yes"])
def test_stop_config_needs_enabled_the_boolean_true(tree, wav, enabled):
    data = stop_on(wav)
    if enabled is None:
        del data["enabled"]
    else:
        data["enabled"] = enabled
    w(session_file(tree), data)
    assert busy.stop_config(SID) is None


@pytest.mark.parametrize("block", [None, "on", ["file"], True, 7])
def test_stop_must_be_a_map(tree, block):
    w(session_file(tree), {"enabled": True, "stop": block})
    assert busy.stop_config(SID) is None


@pytest.mark.parametrize("file", ["", "   ", 7, None, ["x"], "relative/dance.wav",
                                  "/no/such.wav"])
def test_stop_file_must_name_a_readable_file(tree, wav, file):
    w(session_file(tree), {"enabled": True, "stop": {"file": file}})
    assert busy.stop_config(SID) is None


def test_stop_file_may_not_be_a_directory(tree, wav):
    w(session_file(tree), stop_on(wav.parent))
    assert busy.stop_config(SID) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 file")
def test_an_unreadable_stop_file_is_silent(tree, wav):
    w(session_file(tree), stop_on(wav))
    wav.chmod(0o000)
    try:
        assert busy.stop_config(SID) is None
    finally:
        wav.chmod(0o644)


def test_stop_file_tilde_expands_against_home(tree, wav):
    w(session_file(tree), {"enabled": True, "stop": {"file": "~/sounds/dance.wav"}})
    assert busy.stop_config(SID) == wav


def test_stop_ignores_unknown_keys_and_the_busy_block(tree, wav):
    w(session_file(tree), {"enabled": True, "busy": {"trigger": "landd"},
                           "stop": {"file": str(wav), "delay": "x", "device": 3}})
    assert busy.stop_config(SID) == wav
    assert busy.config(SID) is None                   # the busy typo stays the busy's


def test_stop_config_layers_like_busy(tree, wav):
    w(host(tree), stop_on(wav))
    assert busy.stop_config(SID) == wav
    w(session_file(tree), {"enabled": False})
    assert busy.stop_config(SID) is None


@pytest.mark.parametrize("bad", ["{", "[1, 2]"])
def test_stop_config_on_a_bad_layer_is_silence_never_an_error(tree, wav, bad):
    w(host(tree), stop_on(wav))
    w(session_file(tree), bad)
    assert busy.stop_config(SID) is None


def test_stop_config_with_no_sid_is_silent(tree, wav):
    w(host(tree), stop_on(wav))
    assert busy.stop_config("") is None


# --- D8: the arm's resolve keeps the stop sound's OFF stamp too ----------------------

def test_the_arms_resolve_stamps_stop_off_back_dated_when_the_stop_sound_is_unset(tree):
    started = (ANCHOR_SUMMER + timedelta(days=1)).timestamp() + 0.25
    busy.resolve_for_batch(SID, now=lambda: started)
    assert stop_off(tree).is_file()
    assert stop_off(tree).stat().st_mtime == pytest.approx(started - 1.0, abs=1e-3)
    assert (stop_off(tree).parent.stat().st_mode & 0o777) == 0o700


def test_the_arms_resolve_clears_stop_off_when_the_stop_sound_is_set(tree, wav):
    busy.resolve_for_batch(SID)
    assert stop_off(tree).is_file()
    w(session_file(tree), {**on(wav), **stop_on(wav)})
    assert busy.resolve_for_batch(SID) is not None
    assert not stop_off(tree).exists()
    assert not off(tree).exists()


def test_the_busy_and_stop_stamps_are_independent(tree, wav):
    w(session_file(tree), stop_on(wav))               # stop on, busy off
    assert busy.resolve_for_batch(SID) is None
    assert off(tree).is_file() and not stop_off(tree).exists()


def test_the_resolve_writes_no_stop_stamp_without_a_session_dir(tree):
    other = "99999999-0000-0000-0000-000000000000"
    busy.resolve_for_batch(other)
    assert not (tree / ".session" / "sessions" / other).exists()


def test_stop_is_not_a_slot_end_and_clear_stop():
    assert busy.STOP == "stop" and busy.STOP not in busy.SLOTS


# --- the schema's stop block ---------------------------------------------------------

def test_the_schema_accepts_a_stop_file_and_refuses_bad_ones(wav):
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    assert list(validator.iter_errors(stop_on(wav))) == []
    assert list(validator.iter_errors({**on(wav), **stop_on(wav)})) == []
    for bad in ({"stop": {"file": ""}}, {"stop": {"file": 7}}, {"stop": "on"},
                {"stop": ["file"]}):
        assert list(validator.iter_errors(bad)), bad
