"""ST-290: busy detection ASKS voice where the mouth is.

`busy.mouth_dir()` reads `$MOUTH_DIR` and voice's home variables first, as
before; then it asks voice (`ears home state-dir --json`, its stdlib twin
`python -m ears.home state-dir --json`) once, caches the answer in tools'
state, and the mouth is `<root>/mouth`. Only when no voice can be asked
does it fall back to voice's D1 order, copied, with a debug line.

Every test here runs against a STUB `ears`: a `/bin/sh` script on a PATH
that holds nothing else, printing the documented
`{"version", "root": {"path", "source"}, "items": {...}}` shape and counting
its own runs. HOME is a fresh folder, PATH is the stub's alone, and the
cache is under that HOME's tools state, so the host's real voice never
answers (conftest walls `ears` and the twin besides).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sessiontools import busy, paths, settings

#: The real twin lookup, taken at import, before conftest fences it per test.
TWIN = busy._ears_twin


def _heard(root: Path) -> Path:
    """Give ROOT a heard log, voice's POPULATED (a real `heard.lock`)."""
    d = root / "logs" / "conversations"
    d.mkdir(parents=True, exist_ok=True)
    (d / "heard.lock").write_text("")
    return root


class Voice:
    """A stub `ears` in its own bin folder. `answer(...)` (re)writes it;
    `runs` counts how often it ran."""

    def __init__(self, tmp: Path):
        self.home = tmp / "home"
        self.home.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.count = tmp / "runs"
        self.argv = tmp / "argv"
        self.exe = self.bin / "ears"
        self.new = self.home / ".sessionkit" / "voice" / "state"
        self.env = {"HOME": str(self.home), "PATH": str(self.bin)}

    def write(self, body: str) -> None:
        # a NEW file each time (an install replaces it), so its identity moves
        tmp = self.bin / ".ears.tmp"
        tmp.write_text("#!/bin/sh\n"
                       f"printf 'x' >> '{self.count}'\n"
                       f"printf '%s\\n' \"$*\" >> '{self.argv}'\n" + body)
        tmp.chmod(0o755)
        os.replace(tmp, self.exe)

    def answer(self, root: Path | str, source: str = "fresh-new", version: str = "0.3.3",
               **extra) -> None:
        doc = {"version": version, "root": {"path": str(root), "source": source},
               "items": {"mouth": {"path": f"{root}/mouth", "source": source}}, **extra}
        self.write(f"printf '%s\\n' '{json.dumps(doc)}'\n")

    @property
    def runs(self) -> int:
        try:
            return len(self.count.read_text())
        except FileNotFoundError:
            return 0

    def asked_with(self) -> list[str]:
        return self.argv.read_text().splitlines() if self.argv.exists() else []

    def debug(self) -> str:
        log = busy.log_path(self.env)
        assert log == self.home / ".sessionkit" / "tools" / "log" / "busy.log"
        return log.read_text() if log.exists() else ""


@pytest.fixture
def voice(tmp_path) -> Voice:
    return Voice(tmp_path)


# --- the ask -----------------------------------------------------------------------

def test_a_fresh_host_finds_the_mouth_under_the_kit(voice):
    """The repro (red on 6552ee0, which said ~/.sessionvoice/mouth): a fresh
    post-EARS-492 host keeps the mouth at ~/.sessionkit/voice/state/mouth."""
    voice.answer(voice.new, "fresh-new")
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert voice.asked_with() == ["home state-dir --json"]
    assert voice.debug() == ""


def test_the_asked_root_is_used_whatever_the_old_homes_hold(voice):
    """voice's answer wins over tools' copy of its rule: tools never
    recomputes. A root the copy would never pick is taken as said."""
    _heard(voice.home / ".voicemode")
    elsewhere = voice.home / "somewhere" / "else"
    voice.answer(elsewhere, "override")
    assert busy.mouth_dir(voice.env) == elsewhere / "mouth"


def test_a_live_host_with_a_populated_sessionvoice_resolves_unchanged(voice):
    sv = _heard(voice.home / ".sessionvoice")
    voice.answer(sv, "old")
    assert busy.mouth_dir(voice.env) == sv / "mouth"
    # and with no voice to ask, the fallback says the same
    assert busy.mouth_dir({"HOME": str(voice.home)}) == sv / "mouth"


@pytest.mark.parametrize(("name", "value", "want"), [
    ("MOUTH_DIR", "/m", "/m"),
    ("VOICEMODE_MOUTH_DIR", "/o", "/o"),
    ("SESSIONVOICE_HOME", "/s", "/s/mouth"),
    ("VOICEMODE_BASE_DIR", "/b", "/b/mouth"),
])
def test_the_env_overrides_still_win_and_ask_nothing(voice, name, value, want):
    voice.answer(voice.new)
    assert busy.mouth_dir({**voice.env, name: value}) == Path(want)
    assert voice.runs == 0
    assert not busy.voice_cache_path(voice.env).exists()


def test_an_empty_override_falls_through_to_the_ask(voice):
    voice.answer(voice.new)
    env = {**voice.env, "MOUTH_DIR": "", "SESSIONVOICE_HOME": "", "VOICEMODE_BASE_DIR": ""}
    assert busy.mouth_dir(env) == voice.new / "mouth"
    assert voice.runs == 1


# --- the cache ---------------------------------------------------------------------

def test_a_cache_hit_spawns_no_process(voice):
    voice.answer(voice.new)
    for _ in range(5):
        assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert voice.runs == 1


def test_the_cache_holds_across_processes(voice):
    """Hooks are short-lived: a second PROCESS reads the first one's answer."""
    voice.answer(voice.new)
    code = "from sessiontools import busy; print(busy.mouth_dir())"
    env = {**voice.env, "PYTHONPATH": os.pathsep.join(sys.path)}
    for _ in range(3):
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, check=True, timeout=60).stdout.strip()
        assert out == str(voice.new / "mouth")
    assert voice.runs == 1


def test_the_cache_is_tools_own_and_records_the_version(voice):
    voice.answer(voice.new, version="0.3.3")
    busy.mouth_dir(voice.env)
    cache = busy.voice_cache_path(voice.env)
    assert cache == settings.state_root(voice.env) / busy.VOICE_CACHE
    assert cache.is_relative_to(voice.home / ".sessionkit" / "tools")
    held = json.loads(cache.read_text())
    assert held["answer"] == {"version": "0.3.3",
                              "root": {"path": str(voice.new), "source": "fresh-new"}}
    assert cache.stat().st_mode & 0o777 == 0o600
    # nothing under any voice home, nor the mouth dir
    for root in (voice.new, voice.home / ".sessionvoice", voice.home / ".voicemode"):
        assert not root.exists()


def test_a_new_version_asks_again(voice):
    voice.answer(voice.new, version="0.3.3")
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    moved = voice.home / "moved"
    voice.answer(moved, "override", version="0.3.4")       # an upgrade replaces ears
    assert busy.mouth_dir(voice.env) == moved / "mouth"
    assert voice.runs == 2
    assert json.loads(busy.voice_cache_path(voice.env).read_text())["answer"]["version"] == "0.3.4"
    assert busy.mouth_dir(voice.env) == moved / "mouth"
    assert voice.runs == 2


def test_a_root_that_moves_asks_again(voice):
    """A migrate (the new root appears, an old root becomes a link) or a
    heard log appearing changes what voice would say: ask again."""
    voice.answer(voice.new)
    busy.mouth_dir(voice.env)
    _heard(voice.new)
    busy.mouth_dir(voice.env)
    assert voice.runs == 2
    busy.mouth_dir(voice.env)
    assert voice.runs == 2


@pytest.mark.parametrize("junk", ["", "{", "[]", '{"v": 1}', '{"v": 1, "probe": []}',
                                  '{"v": 1, "probe": {}, "answer": {"version": "x"}}',
                                  '{"v": 1, "probe": {}, "answer": 7}', "\xff"])
def test_a_corrupt_cache_is_a_miss_never_a_crash(voice, junk):
    voice.answer(voice.new)
    busy.mouth_dir(voice.env)
    cache = busy.voice_cache_path(voice.env)
    held = json.loads(cache.read_text())
    cache.write_text(junk) if junk != "\xff" else cache.write_bytes(b"\xff\xfe")
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert voice.runs == 2
    # a cached answer of a shape tools does not know is a miss too
    cache.write_text(json.dumps({**held, "answer": {**held["answer"],
                                                    "root": {"path": "rel", "source": "new"}}}))
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert voice.runs == 3


def test_an_unwritable_cache_still_answers(voice):
    voice.answer(voice.new)
    state = settings.state_root(voice.env)
    state.parent.mkdir(parents=True)
    state.write_text("a file where the state folder goes")
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert voice.runs == 2          # no cache: asked each time, and never raised


# --- voice could not be asked: the fallback ---------------------------------------

def test_no_ears_falls_back_with_a_debug_line(voice):
    voice.bin.rmdir()
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    line = voice.debug()
    assert "voice could not be asked" in line and "no `ears` on PATH" in line
    assert str(voice.new / "mouth") in line
    # the verdict is cached: the line is said once, not on every call
    busy.mouth_dir(voice.env)
    assert voice.debug() == line


def _says(doc: object) -> str:
    """A stub body that prints DOC as JSON, and exits 0."""
    return f"printf '%s\\n' '{json.dumps(doc)}'\n"


UNKNOWN = "a shape tools does not know"
ROOT = {"path": "/r", "source": "new"}


@pytest.mark.parametrize(("body", "why"), [
    ("exit 3\n", "exited 3"),
    ("echo 'usage: ears [-h]' >&2; exit 2\n", "exited 2"),       # an old voice
    ("printf 'not json\\n'\n", UNKNOWN),
    (_says([]), UNKNOWN),
    (_says({"root": ROOT, "items": {}}), UNKNOWN),                       # no version
    (_says({"version": "", "root": ROOT, "items": {}}), UNKNOWN),
    (_says({"version": 3, "root": ROOT, "items": {}}), UNKNOWN),
    (_says({"version": "1", "root": ROOT}), UNKNOWN),                    # no items
    (_says({"version": "1", "root": ROOT, "items": []}), UNKNOWN),
    (_says({"version": "1", "root": {"path": "/r", "source": "elsewhere"}, "items": {}}),
     UNKNOWN),
    (_says({"version": "1", "root": {"path": "r", "source": "new"}, "items": {}}), UNKNOWN),
    (_says({"version": "1", "root": "/r", "items": {}}), UNKNOWN),
])
def test_a_failing_ears_is_not_a_path(voice, body, why):
    """An old voice without the verb, a crash or an unknown shape is "could
    not be asked": the D1 fallback, never a wrong path."""
    voice.write(body)
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert why in voice.debug()
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert voice.runs == 1


def test_a_hung_ears_times_out_to_the_fallback(voice, monkeypatch):
    monkeypatch.setattr(busy, "ASK_TIMEOUT", 0.3)
    voice.write("exec /bin/sleep 30\n")
    started = time.monotonic()
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"
    assert time.monotonic() - started < 10
    assert "took over 0.3 s" in voice.debug()


@pytest.mark.parametrize(("heard", "want"), [
    ((".voicemode",), ".voicemode"),
    ((".sessionvoice",), ".sessionvoice"),
    ((".sessionvoice", ".voicemode"), ".sessionvoice"),
    ((".sessionkit/voice/state", ".voicemode"), ".sessionkit/voice/state"),
    ((".sessionkit/voice/state", ".sessionvoice"), ".sessionvoice"),
    ((), ".sessionkit/voice/state"),
])
def test_the_fallback_still_adopts_populated_old_homes(voice, heard, want):
    voice.write("exit 1\n")
    for root in heard:
        _heard(voice.home / root)
    assert busy.mouth_dir(voice.env) == voice.home / want / "mouth"


def test_the_fallback_reads_an_old_root_linked_to_the_new_one_as_the_new(voice):
    """After a migrate `~/.sessionvoice` is a link to the new root: the new
    root, by its own path (talk refuses a linked folder)."""
    _heard(voice.new)
    (voice.home / ".sessionvoice").symlink_to(voice.new)
    assert busy.fallback_root(voice.env) == voice.new
    assert busy.mouth_dir({"HOME": str(voice.home)}) == voice.new / "mouth"


def test_the_fallback_counts_only_real_heard_files(voice):
    """POPULATED counts a REAL heard file: a link into another root is not
    that root's own log (EARS-492 D1)."""
    _heard(voice.new)
    convo = voice.home / ".voicemode" / "logs" / "conversations"
    convo.mkdir(parents=True)
    (convo / "heard.lock").symlink_to(voice.new / "logs" / "conversations" / "heard.lock")
    assert busy.fallback_root(voice.env) == voice.new


def test_the_stdlib_twin_is_asked_when_ears_is_not_on_path(voice, tmp_path, monkeypatch):
    """No `ears` on PATH, but the package imports here: `python -m ears.home
    state-dir --json`, with this interpreter."""
    pkg = tmp_path / "site" / "ears"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    doc = {"version": "0.3.3", "root": {"path": str(voice.new), "source": "fresh-new"},
           "items": {}}
    (pkg / "home.py").write_text(
        "import json, sys\n"
        f"open({str(voice.count)!r}, 'a').write('x')\n"
        "assert sys.argv[1:] == ['state-dir', '--json'], sys.argv\n"
        f"print(json.dumps({doc!r}))\n")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.setattr(busy, "_ears_twin", TWIN)
    voice.bin.rmdir()
    env = {**voice.env, "PYTHONPATH": str(tmp_path / "site")}
    assert busy.mouth_dir(env) == voice.new / "mouth"
    assert busy.mouth_dir(env) == voice.new / "mouth"
    assert voice.runs == 1


# --- one reader, never raises ------------------------------------------------------

def test_one_reader_parses_tools_own_session_path_json():
    """The entry reader that reads voice's answer reads every entry tools'
    `session path --json` writes: the same shape and `source` words."""
    for leaf, entry in paths.report().items():
        found = settings.read_leaf_entry(entry)
        assert found == (Path(entry["path"]), entry["source"]), leaf
    for source in settings.LEAF_SOURCES:
        assert settings.read_leaf_entry({"path": "/x", "source": source}) == (Path("/x"), source)


@pytest.mark.parametrize("entry", [None, [], "x", {}, {"path": "/x"}, {"source": "new"},
                                   {"path": "x", "source": "new"},
                                   {"path": "/x", "source": "NEW"},
                                   {"path": "/x", "source": ["new"]},
                                   {"path": 1, "source": "new"},
                                   {"path": "/x\0y", "source": "new"}])
def test_the_reader_refuses_what_it_does_not_know(entry):
    assert settings.read_leaf_entry(entry) is None


def test_mouth_dir_never_raises(voice, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("probe broke")
    monkeypatch.setattr(busy, "_probe", boom)
    assert busy.mouth_dir(voice.env) == voice.new / "mouth"


def test_the_engines_record_carries_the_asked_mouth(voice):
    """write_record, the per-session hook path, names the mouth voice gave."""
    voice.answer(voice.new)
    wav = voice.home / "x.wav"
    wav.write_bytes(b"")
    sound = busy.Sound(file=wav, delay=1.0, auto_arm=True)
    rec = busy.write_record(voice.home / "slot", sound, 4242, time.time(), voice.env)
    assert json.loads(rec.read_text())["mouth"] == str(voice.new / "mouth")
