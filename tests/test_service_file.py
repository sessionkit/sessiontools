"""`sessiontools.service_file`: the one writer of tools' service files (ST-289).

HOME is fresh per test (conftest), so every file lands under a scratch
kit root. The schema is sessionkit's (SK-253 design 1.2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sessiontools import service_file as sf
from sessiontools import settings

LABEL = "dev.sessionkit.tools.example"


def rec(**kw) -> dict:
    base = {"label": LABEL, "enabled": True, "install": "session example on"}
    base.update(kw)
    return sf.record(**base)


def env_at(home: Path) -> dict:
    return {**os.environ, "HOME": str(home)}


# --- where the file goes --------------------------------------------------------------

def test_the_folder_is_the_tools_package_home_beside_settings():
    assert settings.SERVICES_DIR == "services"
    root = settings.kit_root()
    assert settings.services_dir() == root / settings.PACKAGES["session"] / "services"
    assert settings.services_dir().parent == settings.package_dir("tools").parent
    assert sf.path("pane-state") == settings.services_dir() / "pane-state.json"


def test_a_fresh_home_writes_under_sessionkit_tools_services_only(tmp_path):
    # ST-288: a fresh host (no ~/.session/) starts every tools leaf under
    # ~/.sessionkit. The services folder is no leaf: it is the same place on a
    # fresh HOME and a live one, never under tools/state/, and a write on a
    # fresh HOME makes ~/.sessionkit and nothing else.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    env = env_at(fresh)
    assert not settings.old_root_real(env)
    want = fresh / ".sessionkit" / "tools" / "services"
    assert settings.services_dir(env) == want
    assert want != settings.tools_state_dir(env) / "services"
    assert sf.write(rec(), env) == want / "example.json"
    assert sorted(p.name for p in fresh.iterdir()) == [".sessionkit"]
    assert sorted(p.name for p in (fresh / ".sessionkit").iterdir()) == ["tools"]
    assert sorted(p.name for p in want.parent.iterdir()) == ["services"]

    live = tmp_path / "live"
    (live / ".session").mkdir(parents=True)
    assert settings.old_root_real(env_at(live))
    assert settings.services_dir(env_at(live)) == live / ".sessionkit/tools/services"


def test_a_moved_kit_root_moves_the_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    pa = sf.write(rec(), env_at(a))
    pb = sf.write(rec(), env_at(b))
    assert pa == a / ".sessionkit/tools/services/example.json" and pa.is_file()
    assert pb == b / ".sessionkit/tools/services/example.json" and pb.is_file()
    assert pa.read_bytes() == pb.read_bytes()


def test_nothing_lands_under_settings(tmp_path):
    env = env_at(tmp_path)
    sf.write(rec(), env)
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written == [sf.path("example", env)]
    settings_dir = settings.package_dir("tools", env)
    assert not settings_dir.exists()
    assert settings_dir not in sf.path("example", env).parents


def test_the_help_path_is_built_from_the_constants():
    assert sf.shown("pane-state") == "~/.sessionkit/tools/services/pane-state.json"


# --- the record ------------------------------------------------------------------------

def test_the_record_is_in_the_written_key_order():
    r = rec(uninstall="session example off", note="n", description="d", port=1)
    assert list(r) == ["service", "name", "label", "enabled", "install", "uninstall",
                       "description", "port", "note"]
    assert r["service"] == sf.SCHEMA == 1 and r["name"] == "example"


def test_a_run_record_has_no_label():
    r = sf.record(name="web", run=["session", "web", "serve"], enabled=False,
                  install="session web declare")
    assert list(r) == ["service", "name", "run", "enabled", "install"]
    assert sf.check(r) == []


def test_the_name_is_the_label_with_the_prefix_stripped_dots_and_all():
    """SK-253 1.2: the name is the label after `dev.sessionkit.<package>.`, so
    `bot.<agent>` stays whole (never the last dotted word)."""
    assert sf.name_of("dev.sessionkit.tools.pane-state") == "pane-state"
    assert sf.name_of("dev.sessionkit.tools.bot.alice") == "bot.alice"
    assert rec(label="dev.sessionkit.tools.bot.alice")["name"] == "bot.alice"
    with pytest.raises(ValueError):
        sf.name_of("dev.sessionkit.voice.talk")
    with pytest.raises(ValueError):
        sf.name_of(sf.LABEL_PREFIX)


def test_optional_none_is_left_out():
    assert "note" not in rec(note=None)


GOOD = {"service": 1, "name": "example", "label": LABEL, "enabled": True,
        "install": "session example on"}


@pytest.mark.parametrize("key", ["service", "name", "enabled", "install"])
def test_check_refuses_a_missing_required_key(key):
    bad = {k: v for k, v in GOOD.items() if k != key}
    assert f"missing required key {key!r}" in sf.check(bad)


def test_check_passes_a_good_record():
    assert sf.check(GOOD) == []


def test_check_refuses_label_and_run_both_or_neither():
    both = {**GOOD, "run": ["x"]}
    assert any("both 'label' and 'run'" in p for p in sf.check(both))
    neither = {k: v for k, v in GOOD.items() if k != "label"}
    assert any("'label' (or 'run')" in p for p in sf.check(neither))


@pytest.mark.parametrize("label", ["dev.sessionkit.voice.example", "com.example.tools.example",
                                   "dev.sessionkit.tools.", "example"])
def test_check_refuses_a_bad_label_prefix(label):
    assert any("does not start" in p for p in sf.check({**GOOD, "label": label}))


def test_check_refuses_a_name_that_is_not_the_label_tail():
    problems = sf.check({**GOOD, "name": "other"})
    assert any("not the label's tail" in p for p in problems)
    # the last dotted word is not the tail of a dotted name
    dotted = {**GOOD, "label": "dev.sessionkit.tools.bot.alice", "name": "alice"}
    assert any("not the label's tail" in p for p in sf.check(dotted))
    assert sf.check({**dotted, "name": "bot.alice"}) == []


@pytest.mark.parametrize("key,value", [("service", 2), ("service", True), ("enabled", "yes"),
                                       ("install", ""), ("name", "")])
def test_check_refuses_a_wrong_type(key, value):
    assert sf.check({**GOOD, key: value})


def test_check_refuses_a_bad_run():
    no_label = {k: v for k, v in GOOD.items() if k != "label"}
    for run in ([], "session web serve", ["session", 1]):
        assert any("'run'" in p for p in sf.check({**no_label, "run": run}))


def test_record_raises_on_its_own_bug():
    with pytest.raises(ValueError, match="does not start"):
        sf.record(label="com.example.x", enabled=True, install="x")
    with pytest.raises(ValueError, match="both 'label' and 'run'"):
        sf.record(label=LABEL, run=["x"], enabled=True, install="x")
    with pytest.raises(ValueError, match="not the label's tail"):
        sf.record(label=LABEL, name="other", enabled=True, install="x")
    with pytest.raises(ValueError, match="unknown key"):
        sf.record(label=LABEL, enabled=True, install="x", prot="udp")


# --- the write -------------------------------------------------------------------------

def test_render_is_two_space_json_with_one_newline():
    r = rec()
    text = sf.render(r)
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert json.loads(text) == r
    assert text.splitlines()[1] == '  "service": 1,'


def test_write_makes_the_folder_and_a_0644_file():
    target = sf.write(rec())
    assert target == sf.path("example") and json.loads(target.read_text()) == rec()
    assert oct(target.stat().st_mode & 0o777) == "0o644"


def test_the_temp_file_is_in_the_same_folder_and_gone_after(monkeypatch):
    """The commit is one rename on one filesystem; nothing else is left."""
    seen = {}
    real = os.replace

    def replace(src, dst):
        seen["src"], seen["dst"] = Path(src), Path(dst)
        return real(src, dst)

    monkeypatch.setattr(sf.os, "replace", replace)
    target = sf.write(rec())
    assert seen["dst"] == target and seen["src"].parent == target.parent
    assert seen["src"].name.startswith(".example.json.") and seen["src"].name.endswith(".tmp")
    assert sorted(p.name for p in target.parent.iterdir()) == ["example.json"]


def test_no_partial_file_is_ever_visible(monkeypatch):
    """While the new bytes are being written and synced, the target still
    holds the whole OLD file; at the rename it holds the whole new one."""
    sf.write(rec(enabled=False))
    target = sf.path("example")
    old = target.read_bytes()
    looks = []
    real_fsync = os.fsync

    def fsync(fd):
        looks.append(target.read_bytes())               # mid-write: what a reader sees
        return real_fsync(fd)

    monkeypatch.setattr(sf.os, "fsync", fsync)
    sf.write(rec(enabled=True))
    assert looks == [old]
    assert json.loads(target.read_text())["enabled"] is True


@pytest.mark.parametrize("where", ["write", "fsync", "replace"])
def test_a_failed_write_leaves_the_previous_file_intact(monkeypatch, where):
    sf.write(rec(enabled=True))
    target = sf.path("example")
    before = target.read_bytes()

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    if where == "write":
        monkeypatch.setattr(sf, "render", lambda r: "x" * 10)          # a different body
        real_fdopen = os.fdopen

        class Half:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

            def fileno(self):
                return self.f.fileno()

            def write(self, data):
                self.f.write(data[:3])                   # part of it reaches the temp file
                self.f.flush()
                boom()

        monkeypatch.setattr(sf.os, "fdopen", lambda fd, mode: Half(real_fdopen(fd, mode)))
    else:
        monkeypatch.setattr(sf.os, where, boom)
    with pytest.raises(OSError):
        sf.write(rec(enabled=False))
    assert target.read_bytes() == before
    assert sorted(p.name for p in target.parent.iterdir()) == ["example.json"]


def test_a_hand_edited_or_broken_file_is_replaced_whole():
    target = sf.path("example")
    target.parent.mkdir(parents=True)
    target.write_text('{"service": 1, "extra": "kept?"')                # not JSON
    sf.write(rec())
    assert json.loads(target.read_text()) == rec()
    target.write_text(json.dumps({**rec(), "extra": "by hand"}))
    sf.write(rec())
    assert "extra" not in json.loads(target.read_text())


def test_a_symlink_is_replaced_not_followed(tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("untouched\n")
    target = sf.path("example")
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere)
    sf.write(rec())
    assert not target.is_symlink() and json.loads(target.read_text()) == rec()
    assert elsewhere.read_text() == "untouched\n"


# --- declare and read ------------------------------------------------------------------

def test_declare_writes_and_says_where():
    lines, ok = sf.declare(rec(), dry_run=False)
    assert ok and lines == [f"service {sf.path('example')} (enabled: true)"]
    assert sf.read("example") == rec()


def test_declare_names_the_run_of_a_run_service():
    r = sf.record(name="web", run=["session", "web", "serve"], enabled=False,
                  install="session web declare")
    lines, ok = sf.declare(r, dry_run=False)
    assert ok and lines == [f"service {sf.path('web')} (enabled: false; run: session web serve)"]


def test_a_dry_run_prints_the_same_bytes_and_creates_nothing():
    r = rec()
    lines, ok = sf.declare(r, dry_run=True)
    assert ok and lines[0] == f"service would write {sf.path('example')}:"
    assert "\n".join(lines[1:]) + "\n" == sf.render(r)
    assert not settings.services_dir().exists()
    assert not settings.kit_root().exists()


def test_a_failed_declare_gives_the_line_and_never_raises(monkeypatch):
    sf.write(rec(enabled=True))
    before = sf.path("example").read_bytes()
    os.chmod(settings.services_dir(), 0o500)
    try:
        lines, ok = sf.declare(rec(enabled=False), dry_run=False)
    finally:
        os.chmod(settings.services_dir(), 0o755)
    assert not ok
    assert lines == [sf.COULD_NOT_WRITE.format(path=sf.path("example"),
                                               reason="Permission denied")]
    assert lines[0].startswith("service could not write ")
    assert lines[0].endswith(": Permission denied; the previous file is unchanged")
    assert sf.path("example").read_bytes() == before


def test_a_failed_declare_where_the_folder_cannot_be_made():
    settings.services_dir().parent.mkdir(parents=True)
    settings.services_dir().write_text("a file where the folder goes\n")
    lines, ok = sf.declare(rec(), dry_run=False)
    assert not ok and lines[0].startswith(f"service could not write {sf.path('example')}: ")


def test_read_never_raises():
    assert sf.read("example") is None                                   # absent
    target = sf.path("example")
    target.parent.mkdir(parents=True)
    target.write_text("not json")
    assert sf.read("example") is None
    target.write_text("[1, 2]")
    assert sf.read("example") is None                                   # not an object
    target.write_bytes(b"\xff\xfe")
    assert sf.read("example") is None
    target.write_text(sf.render(rec()))
    assert sf.read("example") == rec()


def test_the_module_imports_nothing_heavier_than_settings():
    src = Path(sf.__file__).read_text()
    imports = [line for line in src.splitlines() if line.startswith(("import ", "from "))]
    assert imports == ["from __future__ import annotations", "import contextlib",
                       "import json", "import os", "import tempfile",
                       "from collections.abc import Mapping", "from pathlib import Path",
                       "from sessiontools import settings"]
