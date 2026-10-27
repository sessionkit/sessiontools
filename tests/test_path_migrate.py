"""`session path migrate` (ST-266 do-004, design/build-cut.md §4): the state
and sound leaves move once, by rename only, and every refusal moves nothing.

Every test is a tmp HOME. No test runs launchctl or systemctl (the conftest
fence; the probe is a double), and none runs the verb against a real HOME.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from sessiontools import cli, migrate, pane_state, settings


def env_for(home: Path) -> dict[str, str]:
    return {"HOME": str(home)}


def none_loaded(env):
    return [migrate.Agent("pane-state agent", "launchd x", migrate.NOT_LOADED)]


def tree_hash(root: Path, mtimes: bool = True) -> str:
    """Every entry under ROOT: its path, type, mode, mtime, and its bytes or
    link target. Links are read, never followed. MTIMES=False leaves the
    folders' mtimes out: a failed move that put everything back has still
    touched them."""
    h = hashlib.sha256()
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            p = Path(dirpath, name)
            st = os.lstat(p)
            when = st.st_mtime_ns if mtimes else 0
            h.update(f"{p.relative_to(root)}|{st.st_mode}|{when}|".encode())
            if os.path.islink(p):
                h.update(os.readlink(p).encode())
            elif p.is_file():
                h.update(p.read_bytes())
        if mtimes:
            h.update(f"{dirpath}|{os.lstat(dirpath).st_mtime_ns}".encode())
    return h.hexdigest()


def old_layout(home: Path) -> dict[str, Path]:
    """A host before the move: real old state and sound, with a file link,
    a folder link and a dangling link inside state."""
    state = home / ".session/state"
    (state / "list").mkdir(parents=True)
    (state / "list/snapshot.json").write_text('{"rows": []}')
    (state / "holders").mkdir()
    (state / "holders/scout.json").write_text('{"pid": 1}')
    (state / "default-model/backups").mkdir(parents=True)
    (state / "default-model/log.jsonl").write_text("{}\n")
    (state / "reap.log").write_text("one line\n")
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "f").write_text("x")
    os.symlink(elsewhere / "f", state / "file-link")
    os.symlink(elsewhere, state / "dir-link")
    os.symlink(home / "gone", state / "dangling")
    sound = home / ".local/share/sessiontools/sound"
    (sound / "bin").mkdir(parents=True)
    (sound / "bin/sound-event").write_text("#!/bin/sh\n")
    (home / ".session/sessions").mkdir()           # wave 4: never touched
    return {"state": state, "sound": sound, "elsewhere": elsewhere}


def run(env, dry_run=False, probe=none_loaded):
    return migrate.migrate(env, dry_run=dry_run, probe=probe)


# --- criterion 1: moves once, then a no-op; old paths link to new -------------------------

def test_moves_once_then_is_a_no_op(tmp_path):
    old = old_layout(tmp_path)
    env = env_for(tmp_path)
    new_state, new_sound = settings.leaf_new("state", env), settings.leaf_new("sound", env)
    r = run(env)
    assert (r["refused"], r["failed"]) == ([], None)
    assert [p.action for p in r["leaves"]] == ["rename", "rename"]
    for leaf, new in (("state", new_state), ("sound", new_sound)):
        assert os.path.islink(old[leaf]) and os.readlink(old[leaf]) == str(new)
        assert new.is_dir() and not new.is_symlink()
        assert settings.leaf_path(leaf, env) == (new, "new")
        assert settings.leaf_stranded(leaf, env) is None
    assert (new_state / "list/snapshot.json").read_text() == '{"rows": []}'
    assert (new_sound / "bin/sound-event").exists()
    assert (tmp_path / ".session/sessions").is_dir()             # wave 4 untouched
    before = tree_hash(tmp_path)
    again = run(env)
    assert [p.action for p in again["leaves"]] == ["done", "done"]
    assert "nothing to do" in migrate.render(again)
    assert tree_hash(tmp_path) == before


def test_symlinks_move_as_links_with_the_same_target(tmp_path):
    old = old_layout(tmp_path)
    env = env_for(tmp_path)
    want = {n: os.readlink(old["state"] / n) for n in ("file-link", "dir-link", "dangling")}
    r = run(env)
    new = settings.leaf_new("state", env)
    for n, target in want.items():
        assert os.path.islink(new / n) and os.readlink(new / n) == target
    assert not os.path.exists(new / "dangling")                  # still dangling
    assert (old["elsewhere"] / "f").read_text() == "x"           # the target never moved
    assert {d["path"]: d["target"] for d in r["leaves"][0].links} == want


def test_a_fresh_sound_folder_merges_state_child_by_child(tmp_path):
    """The nesting case: a fresh sound install made tools/state/sound/, so
    tools/state/ exists. Each old child is renamed in beside it."""
    old = old_layout(tmp_path)
    env = env_for(tmp_path)
    (tmp_path / ".local").rename(tmp_path / "no-local")          # no old sound
    new = settings.leaf_new("state", env)
    (new / "sound/bin").mkdir(parents=True)
    r = run(env)
    assert [p.action for p in r["leaves"]] == ["merge", "absent"]
    assert sorted(os.listdir(new)) == sorted(
        ["sound", "list", "holders", "default-model", "reap.log", "file-link", "dir-link", "dangling"])
    assert os.readlink(old["state"]) == str(new)
    assert settings.leaf_path("state", env) == (new, "new")


# --- criterion 2: collisions, EXDEV and the dry run move nothing ---------------------------

@pytest.mark.parametrize("case", ["state child", "sound folder", "sound inside old state"])
def test_a_collision_refuses_the_whole_run(tmp_path, case):
    old_layout(tmp_path)
    env = env_for(tmp_path)
    if case == "state child":
        (settings.leaf_new("state", env) / "list").mkdir(parents=True)
    elif case == "sound folder":
        settings.leaf_new("sound", env).mkdir(parents=True)
    else:
        (tmp_path / ".session/state/sound").mkdir()
    before = tree_hash(tmp_path)
    r = run(env)
    assert r["refused"] and "collision" in " ".join(r["refused"])
    assert not any(p.moved for p in r["leaves"])
    assert tree_hash(tmp_path) == before
    if case == "state child":
        assert "list" in r["refused"][0]


def test_exdev_found_before_the_move_refuses(tmp_path, monkeypatch):
    old_layout(tmp_path)
    env = env_for(tmp_path)
    monkeypatch.setattr(migrate, "_dev", lambda path: -1)        # new is on "another device"
    before = tree_hash(tmp_path)
    r = run(env)
    assert all("EXDEV" in why for why in r["refused"]) and len(r["refused"]) == 2
    assert tree_hash(tmp_path) == before


def test_exdev_from_rename_itself_moves_nothing(tmp_path, monkeypatch):
    old_layout(tmp_path)
    env = env_for(tmp_path)

    def exdev(src, dst):
        raise OSError(errno.EXDEV, "Cross-device link", str(src))

    monkeypatch.setattr(migrate, "_rename", exdev)
    before = tree_hash(tmp_path, mtimes=False)
    r = run(env)
    assert r["failed"] and "EXDEV" in r["failed"] and "no copy fallback" in r["failed"]
    assert tree_hash(tmp_path, mtimes=False) == before           # the made parents are gone too
    assert not (tmp_path / ".sessionkit").exists()


def test_a_merge_that_fails_part_way_puts_every_child_back(tmp_path, monkeypatch):
    old_layout(tmp_path)
    env = env_for(tmp_path)
    (tmp_path / ".local").rename(tmp_path / "no-local")
    (settings.leaf_new("state", env) / "sound").mkdir(parents=True)
    real, calls = os.rename, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 3:
            raise OSError(errno.EIO, "I/O error", str(src))
        real(src, dst)

    monkeypatch.setattr(migrate, "_rename", flaky)
    before = tree_hash(tmp_path / ".session", mtimes=False)
    r = run(env)
    assert "every child was put back" in r["failed"]
    assert sorted(os.listdir(settings.leaf_new("state", env))) == ["sound"]
    assert tree_hash(tmp_path / ".session", mtimes=False) == before
    assert not os.path.islink(tmp_path / ".session/state")       # no link left on a failure


@pytest.mark.parametrize("layout", ["old", "fresh", "merge"])
def test_the_dry_run_is_read_only(tmp_path, layout, monkeypatch):
    if layout != "fresh":
        old_layout(tmp_path)
    if layout == "merge":
        (settings.leaf_new("state", env_for(tmp_path)) / "sound").mkdir(parents=True)
        (tmp_path / ".local").rename(tmp_path / "no-local")
    env = env_for(tmp_path)
    monkeypatch.setattr(migrate, "_platform", lambda: "linux")
    monkeypatch.setattr(migrate, "_run", lambda argv: type("R", (), {"returncode": 3})())
    before = tree_hash(tmp_path)
    r = migrate.migrate(env, dry_run=True)                       # the real probe, doubled below it
    assert r["dry_run"] and not any(p.moved for p in r["leaves"])
    text = migrate.render(r)
    assert text.startswith("session path migrate: dry run: nothing was changed")
    assert tree_hash(tmp_path) == before
    if layout == "fresh":
        assert sorted(os.listdir(tmp_path)) == []


# --- criterion 3: the receipt, and the loaded-agent refusal ------------------------------------

def test_the_receipt_names_entries_sizes_links_and_agents(tmp_path):
    old_layout(tmp_path)
    env = env_for(tmp_path)
    r = run(env, dry_run=True)
    state = r["leaves"][0]
    whole = state.entries[0]
    assert (whole.name, whole.kind) == (".", "dir")
    assert whole.files == 7                                      # 4 files + 3 links
    assert whole.size >= len('{"rows": []}') + len('{"pid": 1}')
    text = migrate.render(r)
    assert "would move (the whole folder)  dir, 7 files" in text
    assert "link moved as a link: dangling ->" in text
    assert f"would leave a link: {tmp_path / '.session/state'} -> " in text
    assert "pane-state agent" in text and "not loaded" in text
    j = json.loads(migrate.to_json(r))
    assert j["leaves"][0]["entries"][0]["files"] == 7
    assert j["agents"][0]["state"] == "not loaded"


def test_the_receipt_after_a_live_run_says_moved_and_the_link_left(tmp_path):
    old_layout(tmp_path)
    env = env_for(tmp_path)
    text = migrate.render(run(env))
    assert text.startswith("session path migrate: done")
    assert "moved (the whole folder)" in text
    assert f"link left: {tmp_path / '.session/state'} -> {settings.leaf_new('state', env)}" in text


@pytest.mark.parametrize("state", [migrate.LOADED, migrate.UNKNOWN])
def test_a_live_run_refuses_while_an_agent_is_loaded_or_unknown(tmp_path, state):
    old_layout(tmp_path)
    env = env_for(tmp_path)
    before = tree_hash(tmp_path)

    def probe(env):
        return [migrate.Agent("sound engine", "launchd dev.sessionkit.tools.sound-engine", state)]

    r = run(env, probe=probe)
    assert r["refused"] and "sound engine" in r["refused"][0]
    assert tree_hash(tmp_path) == before
    assert "would refuse: sound engine" in migrate.render(run(env, dry_run=True, probe=probe))


def test_a_loaded_agent_does_not_refuse_when_nothing_would_move(tmp_path):
    env = env_for(tmp_path)

    def probe(env):
        return [migrate.Agent("pane-state agent", "launchd x", migrate.LOADED)]

    assert run(env, probe=probe)["refused"] == []


@pytest.mark.parametrize("var", ["SESSION_STATE_DIR", "SESSIONTOOLS_SOUND_HOME"])
def test_an_override_refuses_the_operator_owns_it(tmp_path, var):
    old_layout(tmp_path)
    env = env_for(tmp_path) | {var: str(tmp_path / "mine")}
    before = tree_hash(tmp_path)
    r = run(env)
    assert any(var in why and "operator owns" in why for why in r["refused"])
    assert tree_hash(tmp_path) == before


# --- the old leaf itself a link -----------------------------------------------------------------

def test_an_old_leaf_that_is_a_link_is_moved_as_the_link(tmp_path):
    env = env_for(tmp_path)
    real = tmp_path / "real-state"
    (real / "list").mkdir(parents=True)
    (tmp_path / ".session").mkdir()
    os.symlink(real, tmp_path / ".session/state")
    r = run(env)
    new = settings.leaf_new("state", env)
    assert r["leaves"][0].action == "move-link" and "link is moved" in r["leaves"][0].why
    assert os.path.islink(new) and os.readlink(new) == str(real)
    assert os.readlink(tmp_path / ".session/state") == str(new)
    assert (real / "list").is_dir()                              # its target never moved
    assert settings.leaf_path("state", env)[1] == "new"


def test_a_dangling_old_leaf_link_refuses(tmp_path):
    env = env_for(tmp_path)
    (tmp_path / ".session").mkdir()
    os.symlink(tmp_path / "gone", tmp_path / ".session/state")
    r = run(env)
    assert r["refused"] and "dangling" in r["refused"][0]


# --- the probes (doubles under them: no launchctl, no systemctl) ----------------------------------

class Ran:
    def __init__(self, code):
        self.returncode = code


@pytest.mark.parametrize(("code", "want"), [(0, migrate.LOADED), (113, migrate.NOT_LOADED)])
def test_the_launchd_probe_reads_launchctl_print(monkeypatch, code, want):
    seen = []
    monkeypatch.setattr(migrate, "_run", lambda argv: seen.append(argv) or Ran(code))
    assert migrate._launchd("dev.sessionkit.tools.pane-state") == want
    assert seen[0][:2] == ["launchctl", "print"]


def test_the_probes_never_run_unfenced(tmp_path):
    with pytest.raises(RuntimeError, match="fenced"):
        migrate._run(["launchctl", "print", "x"])


def test_on_darwin_every_label_is_probed(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(migrate, "_platform", lambda: "darwin")
    monkeypatch.setattr(migrate, "_run", lambda argv: seen.append(argv[-1]) or Ran(113))
    found = migrate.agents(env_for(tmp_path))
    labels = {s.rsplit("/", 1)[1] for s in seen}
    assert {"dev.sessionkit.tools.pane-state", "dev.sessionkit.tools.default-model",
            "dev.sessionkit.tools.sound-engine"} <= labels
    assert {a.name for a in found} >= {"pane-state agent", "default-model watch",
                                       "sound engine", "pane-state watch"}


def test_a_held_pane_state_lock_reads_loaded(tmp_path):
    env = env_for(tmp_path)
    lock = pane_state.lock_path(env)
    assert migrate._lock_held(lock) == migrate.NOT_LOADED and not lock.exists()
    fd = pane_state.acquire(lock)
    try:
        assert migrate._lock_held(lock) == migrate.LOADED
    finally:
        os.close(fd)
    assert migrate._lock_held(lock) == migrate.NOT_LOADED


# --- the verb, through the dispatcher -------------------------------------------------------------

def test_session_path_migrate_through_the_dispatcher(tmp_path, monkeypatch, capsys):
    old_layout(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("SESSION_STATE_DIR", "SESSIONTOOLS_SOUND_HOME", "SESSIONS_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(migrate, "_platform", lambda: "linux")
    monkeypatch.setattr(migrate, "_run", lambda argv: Ran(3))
    before = tree_hash(tmp_path)
    assert cli.main(["path", "migrate", "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("session path migrate: dry run")
    assert tree_hash(tmp_path) == before
    assert cli.main(["path", "state"]) == 0                      # LEAF still parses
    assert capsys.readouterr().out == f"{tmp_path / '.session/state'}\n"
    assert cli.main(["path", "migrate"]) == 0
    capsys.readouterr()
    assert cli.main(["path", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["state"]["source"], got["sound"]["source"]) == ("new", "new")
    assert cli.main(["path", "migrate", "--json"]) == 0
    assert [p["action"] for p in json.loads(capsys.readouterr().out)["leaves"]] == ["done", "done"]


def test_it_touches_nothing_outside_its_two_leaves(tmp_path):
    old_layout(tmp_path)
    for d in (".sessionmail/x", ".session/seats/s", ".session/agent/a", ".session/backups/b"):
        (tmp_path / d).mkdir(parents=True)
    env = env_for(tmp_path)
    keep = {d: tree_hash(tmp_path / d) for d in (".sessionmail", ".session/sessions",
                                                 ".session/seats", ".session/agent",
                                                 ".session/backups", "elsewhere")}
    run(env)
    assert {d: tree_hash(tmp_path / d) for d in keep} == keep


# --- relative link targets (do-004 review): a link moves with its target as written ---------

def test_an_old_leaf_link_with_a_relative_target_refuses(tmp_path):
    """`~/.session/state -> ../real`, moved to `~/.sessionkit/tools/state`,
    would resolve to `~/.sessionkit/real`: nothing there. Refused instead."""
    env = env_for(tmp_path)
    (tmp_path / "real/list").mkdir(parents=True)
    (tmp_path / ".session").mkdir()
    os.symlink("../real", tmp_path / ".session/state")
    before = tree_hash(tmp_path)
    r = run(env)
    assert r["refused"] and "relative target (../real)" in r["refused"][0]
    assert tree_hash(tmp_path) == before
    assert not (tmp_path / ".sessionkit").exists()


@pytest.mark.parametrize("layout", ["rename", "merge"])
def test_a_relative_link_leading_out_of_the_leaf_refuses(tmp_path, layout):
    """`state/esc -> ../agent` resolves to `~/.session/agent` today and to
    `~/.sessionkit/tools/agent` (nothing) once moved: refused, named."""
    old_layout(tmp_path)
    env = env_for(tmp_path)
    (tmp_path / ".session/agent").mkdir()
    os.symlink("../../agent", tmp_path / ".session/state/holders/esc")
    if layout == "merge":
        (tmp_path / ".local").rename(tmp_path / "no-local")
        (settings.leaf_new("state", env) / "sound").mkdir(parents=True)
    before = tree_hash(tmp_path)
    r = run(env)
    assert r["refused"] and "holders/esc -> ../../agent" in r["refused"][0]
    assert r["leaves"][0].action == "refuse"
    assert tree_hash(tmp_path) == before


def test_a_relative_link_inside_the_leaf_moves_and_still_resolves(tmp_path):
    """A relative target that stays inside the leaf moves with it, a
    dangling one included: same target, same meaning."""
    old = old_layout(tmp_path)
    env = env_for(tmp_path)
    os.symlink("list/snapshot.json", old["state"] / "inner")
    os.symlink("../list", old["state"] / "holders/up-in")
    os.symlink("missing", old["state"] / "inner-dangling")
    r = run(env)
    assert (r["refused"], r["failed"]) == ([], None)
    new = settings.leaf_new("state", env)
    assert os.readlink(new / "inner") == "list/snapshot.json"
    assert (new / "inner").read_text() == '{"rows": []}'
    assert (new / "holders/up-in/snapshot.json").exists()
    assert os.readlink(new / "inner-dangling") == "missing"
    assert not os.path.exists(new / "inner-dangling")
