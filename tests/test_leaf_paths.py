"""The per-leaf resolvers of tools' state (ST-266 do-001, READY A = wave 2;
do-003, READY B = wave 3: the writers switch).

THE RULE (settings.leaf_path, design/build-cut.md §2-3): an override wins
outright; else OLD while it is a directory that is not NEW's; else NEW when
it is a directory; else the fresh default: NEW for state and sound (wave 3),
OLD for sessions, seats, agent and backups (they move in wave 4). Each leaf
below is pinned for its default, its override and each fallback case; then
every writer is pinned to the place it wrote at 7ef0782 on an old-layout
HOME, and to the new state folder on a fresh one; then the nesting hazard
and the fresh-HOME walk; then AGENTS_DIR's split; then the bash copy in
hooks/busy-sound against python; then the grep.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sessiontools import default_model, pane_state, settings, snapshot, sound
from sessiontools.start import agents as ag
from sessiontools.start import holders

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "busy-sound"
SID = "0b1c2d3e-0000-4000-8000-00000000abcd"

#: Each leaf's OLD path under HOME, as 7ef0782 spelled it.
OLD = {"state": ".session/state", "sessions": ".session/sessions", "seats": ".session/seats",
       "agent": ".session/agent", "backups": ".session/backups",
       "sound": ".local/share/sessiontools/sound"}
#: Each leaf's NEW path under HOME (Q1: tools/state/<leaf>).
NEW = {"state": ".sessionkit/tools/state", "sessions": ".sessionkit/tools/state/sessions",
       "seats": ".sessionkit/tools/state/seats", "agent": ".sessionkit/tools/state/agent",
       "backups": ".sessionkit/tools/state/backups",
       "sound": ".sessionkit/tools/state/sound"}
OVERRIDE = {"state": "SESSION_STATE_DIR", "sessions": "SESSIONS_DIR",
            "sound": "SESSIONTOOLS_SOUND_HOME"}
#: The public *_root of each leaf, the names peers (SM-969) import.
ROOTS = {"state": settings.state_root, "sessions": settings.sessions_root,
         "seats": settings.seats_root, "agent": settings.agent_root,
         "backups": settings.backups_root, "sound": settings.sound_root}


def env_for(home: Path, **extra) -> dict:
    return {"HOME": str(home), **extra}


def tree(home: Path) -> list[str]:
    """Every path under HOME, relative, links named as links (not followed)."""
    out = []
    for dirpath, dirs, files in os.walk(home):
        for name in dirs + files:
            p = Path(dirpath, name)
            out.append(str(p.relative_to(home)) + ("@" if p.is_symlink() else ""))
    return sorted(out)


# --- the table itself ------------------------------------------------------------------------

def test_the_leaves_and_their_paths_are_the_layout(tmp_path):
    env = env_for(tmp_path)
    assert settings.LEAVES == tuple(OLD) == tuple(NEW)
    assert settings.NESTED_LEAVES == settings.LEAVES[1:]
    for leaf in settings.LEAVES:
        assert settings.leaf_old(leaf, env) == tmp_path / OLD[leaf], leaf
        assert settings.leaf_new(leaf, env) == tmp_path / NEW[leaf], leaf
    assert settings.tools_state_dir(env) == tmp_path / ".sessionkit/tools/state"
    assert settings.tools_state_dir(env).parent == settings.package_dir("tools", env).parent


#: Each leaf's fresh default (wave 3): the new folder for state and sound.
FRESH = {leaf: ("fresh-new" if leaf in ("state", "sound") else "fresh-old")
         for leaf in settings.LEAVES}


def test_wave_3_makes_state_and_sound_fresh_new_and_nothing_else():
    """READY B: state and sound start NEW on a fresh host; the wave-4 leaves
    (sessions, seats, agent, backups) keep OLD."""
    assert settings.FRESH_NEW == frozenset({"state", "sound"})
    assert {leaf for leaf, src in FRESH.items() if src == "fresh-old"} == {
        "sessions", "seats", "agent", "backups"}


def test_an_unknown_leaf_is_refused():
    with pytest.raises(ValueError, match="unknown leaf"):
        settings.leaf_path("log")


def test_agents_dir_is_no_leafs_override():
    assert "AGENTS_DIR" not in settings.LEAF_OVERRIDE.values()
    assert {k: v for k, v in settings.LEAF_OVERRIDE.items() if v} == OVERRIDE


# --- each leaf: default, override, every fallback case ---------------------------------------

@pytest.mark.parametrize("leaf", settings.LEAVES)
def test_fresh_home_is_the_fresh_default_and_makes_nothing(tmp_path, leaf):
    env = env_for(tmp_path)
    want = tmp_path / (NEW if FRESH[leaf] == "fresh-new" else OLD)[leaf]
    assert settings.leaf_path(leaf, env) == (want, FRESH[leaf])
    assert ROOTS[leaf](env) == want
    assert settings.leaf_stranded(leaf, env) is None
    assert tree(tmp_path) == []                                   # a read makes nothing


@pytest.mark.parametrize("leaf", settings.LEAVES)
def test_old_only_is_old(tmp_path, leaf):
    env = env_for(tmp_path)
    (tmp_path / OLD[leaf]).mkdir(parents=True)
    assert settings.leaf_path(leaf, env) == (tmp_path / OLD[leaf], "old")
    assert settings.leaf_stranded(leaf, env) is None


@pytest.mark.parametrize("leaf", settings.LEAVES)
def test_new_only_is_new(tmp_path, leaf):
    env = env_for(tmp_path)
    (tmp_path / NEW[leaf]).mkdir(parents=True)
    assert settings.leaf_path(leaf, env) == (tmp_path / NEW[leaf], "new")


@pytest.mark.parametrize("leaf", settings.LEAVES)
def test_old_a_link_to_new_is_new(tmp_path, leaf):
    """The state after a host's migration: one directory, answered as new."""
    env = env_for(tmp_path)
    new, old = tmp_path / NEW[leaf], tmp_path / OLD[leaf]
    new.mkdir(parents=True)
    old.parent.mkdir(parents=True, exist_ok=True)
    old.symlink_to(new)
    assert settings.leaf_path(leaf, env) == (new, "new")
    assert settings.leaf_stranded(leaf, env) is None


@pytest.mark.parametrize("leaf", settings.LEAVES)
def test_old_a_link_elsewhere_is_followed_as_old(tmp_path, leaf):
    env = env_for(tmp_path)
    elsewhere, old = tmp_path / "elsewhere", tmp_path / OLD[leaf]
    elsewhere.mkdir()
    old.parent.mkdir(parents=True, exist_ok=True)
    old.symlink_to(elsewhere)
    (tmp_path / NEW[leaf]).mkdir(parents=True)
    assert settings.leaf_path(leaf, env) == (old, "old")


@pytest.mark.parametrize("leaf", settings.LEAVES)
def test_both_real_old_wins_and_new_is_stranded(tmp_path, leaf):
    env = env_for(tmp_path)
    old, new = tmp_path / OLD[leaf], tmp_path / NEW[leaf]
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    assert settings.leaf_stranded(leaf, env) is None             # empty: nothing stranded
    (new / "something").write_text("x")
    assert settings.leaf_path(leaf, env) == (old, "old")
    assert ROOTS[leaf](env) == old
    assert settings.leaf_stranded(leaf, env) == new


@pytest.mark.parametrize("nested", settings.NESTED_LEAVES)
def test_tools_state_holding_only_nested_leaves_is_not_stranded_state(tmp_path, nested):
    """The Q1 nesting hazard (decompose-001 review, gap 2): a fresh nested
    write makes tools/state/, which must neither flip the state leaf to new
    nor report the live old state as stranded."""
    env = env_for(tmp_path)
    (tmp_path / OLD["state"] / "list").mkdir(parents=True)
    (tmp_path / NEW[nested]).mkdir(parents=True)
    assert settings.leaf_path("state", env) == (tmp_path / OLD["state"], "old")
    assert settings.leaf_stranded("state", env) is None
    (tmp_path / NEW["state"] / "holders").mkdir()                 # one of state's own
    assert settings.leaf_stranded("state", env) == tmp_path / NEW["state"]


@pytest.mark.parametrize("leaf", sorted(OVERRIDE))
def test_an_override_wins_outright_with_no_fallback(tmp_path, leaf):
    old, new = tmp_path / OLD[leaf], tmp_path / NEW[leaf]
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    env = env_for(tmp_path, **{OVERRIDE[leaf]: str(tmp_path / "mine")})
    assert settings.leaf_path(leaf, env) == (tmp_path / "mine", "override")
    assert settings.leaf_stranded(leaf, env) is None
    empty = env_for(tmp_path, **{OVERRIDE[leaf]: ""})            # empty is unset
    assert settings.leaf_path(leaf, empty)[1] == "old"


def test_the_overrides_expand_as_they_always_did(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert settings.state_root({"SESSION_STATE_DIR": "~/s"}) == tmp_path / "s"
    assert settings.sessions_root({"SESSIONS_DIR": "~/x"}) == tmp_path / "x"
    assert settings.sound_root({"SESSIONTOOLS_SOUND_HOME": "~/snd"}) == Path("~/snd")  # as written


def test_a_path_that_cannot_be_searched_reads_as_absent(tmp_path):
    env = env_for(tmp_path)
    (tmp_path / ".session" / "state").mkdir(parents=True)
    (tmp_path / ".session").chmod(0o000)
    try:
        assert settings.leaf_path("state", env)[1] == "fresh-new"
    finally:
        (tmp_path / ".session").chmod(0o755)


# --- the old-only HOME reads everything; the views agree -------------------------------------

def old_layout(home: Path) -> None:
    for leaf in settings.LEAVES:
        (home / OLD[leaf]).mkdir(parents=True)
    (home / OLD["sessions"] / SID / "settings").mkdir(parents=True)
    (home / OLD["seats"] / "scout" / "settings").mkdir(parents=True)
    (home / OLD["agent"] / "config" / "alice").mkdir(parents=True)
    (home / OLD["agent"] / "config" / "alice" / "alice.md").write_text("---\n---\n")
    (home / OLD["agent"] / "home" / "alice").mkdir(parents=True)


def test_a_home_with_only_the_old_layout_reads_everything(tmp_path):
    old_layout(tmp_path)
    before = tree(tmp_path)
    env = env_for(tmp_path)
    for leaf in settings.LEAVES:
        assert settings.leaf_path(leaf, env) == (tmp_path / OLD[leaf], "old"), leaf
    assert settings.session_dir(SID, env) == tmp_path / OLD["sessions"] / SID
    assert settings.resolve_sid(SID[:8], env) == (SID, None)
    assert settings.seat_dir("scout", env) == tmp_path / OLD["seats"] / "scout"
    assert settings.agent_config("alice", env) == tmp_path / OLD["agent"] / "config/alice/alice.md"
    assert settings.agent_home("alice", env) == tmp_path / OLD["agent"] / "home/alice"
    assert ag.agents_root(env) == str(tmp_path / OLD["agent"] / "config")
    assert ag.sessions_root(ag.BARE_AGENT, environ=env) == str(tmp_path / OLD["sessions"])
    assert snapshot.state_dir(env) == tmp_path / OLD["state"]
    assert sound.home(env) == tmp_path / OLD["sound"]
    assert tree(tmp_path) == before                               # nothing new was made
    assert not (tmp_path / ".sessionkit").exists()


def test_a_migrated_home_reads_through_the_new_paths(tmp_path):
    """Every leaf moved, each old path a link to its new one."""
    old_layout(tmp_path)
    for leaf in ("sessions", "seats", "agent", "backups", "sound"):
        new = tmp_path / NEW[leaf]
        new.parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / OLD[leaf]).rename(new)
    (tmp_path / OLD["state"]).rmdir()
    for leaf in settings.LEAVES:
        (tmp_path / OLD[leaf]).symlink_to(tmp_path / NEW[leaf])
    env = env_for(tmp_path)
    for leaf in settings.LEAVES:
        assert settings.leaf_path(leaf, env) == (tmp_path / NEW[leaf], "new"), leaf
    assert settings.agent_home("alice", env) == tmp_path / NEW["agent"] / "home/alice"
    assert ag.agents_root(env) == str(tmp_path / NEW["agent"] / "config")


def test_snapshot_and_sound_keep_their_names_as_views(tmp_path):
    env = env_for(tmp_path)
    assert snapshot.STATE_DIR_ENV == settings.SESSION_STATE_DIR_ENV
    assert sound.HOME_ENV == settings.SOUND_HOME_ENV
    assert snapshot.state_dir(env) == settings.state_root(env)
    assert sound.home(env) == settings.sound_root(env)
    assert sound.sender(env) == settings.sound_root(env) / "bin" / sound.SENDER


def test_the_deprecated_root_is_still_the_old_one(tmp_path):
    assert settings.session_root(env_for(tmp_path)) == tmp_path / ".session"
    assert ag.DEFAULT_AGENTS_DIR == "~/.session/agents"
    assert ag.DEFAULT_SESSIONS_DIR == (".session", "sessions")


# --- every writer writes where it did at 7ef0782 ---------------------------------------------

def writer_paths(env: dict) -> dict[str, Path]:
    return {"snapshot": snapshot.snapshot_path(env),
            "holders": holders.holders_dir(env),
            "default-model": default_model.state_dir(env),
            "pane-state lock": pane_state.lock_path(env),
            "session dir": settings.session_dir(SID, env),
            "seat dir": settings.seat_dir("scout", env),
            "agent home": settings.agent_home("alice", env),
            "sound sender": sound.sender(env)}


def at_7ef0782(home: Path) -> dict[str, Path]:
    s = home / ".session"
    return {"snapshot": s / "state/list/snapshot.json",
            "holders": s / "state/holders",
            "default-model": s / "state" / default_model.STATE_SUBDIR,
            "pane-state lock": s / "state" / pane_state.LOCK_DIR / pane_state.LOCK_NAME,
            "session dir": s / "sessions" / SID,
            "seat dir": s / "seats/scout",
            "agent home": s / "agent/home/alice",
            "sound sender": home / ".local/share/sessiontools/sound/bin/sound-event"}


def write_state(env: dict) -> None:
    """Real writes, in process, by the state leaf's writers: the snapshot,
    default-model's backup and log, the pane signal's lock, a holder's claim."""
    snapshot.rows(False, fresh=True, env=env, compute=lambda want: [])
    default_model._backup(b"{}", env, 1_800_000_000.0)
    default_model._log(env, {"event": "test"})
    os.close(pane_state.acquire(pane_state.lock_path(env)))
    holders.write_claim("scout", {"pid": 1}, env)


def test_on_an_old_layout_home_every_writer_writes_where_it_did(tmp_path):
    """A host that has the old folders: wave 3 changes nothing there. Every
    writer keeps its 7ef0782 place, and nothing makes tools/state/."""
    old_layout(tmp_path)
    env = env_for(tmp_path)
    assert writer_paths(env) == at_7ef0782(tmp_path)
    write_state(env)
    assert snapshot.snapshot_path(env).exists()
    assert (default_model.state_dir(env) / "log.jsonl").exists()
    assert not (tmp_path / ".sessionkit" / "tools" / "state").exists()
    made = [p for p in tree(tmp_path) if p.startswith(".sessionkit")]
    assert made == []


def test_on_a_fresh_home_state_and_sound_writers_write_new(tmp_path):
    """A fresh host (neither folder): the state leaf's writers and the sound
    engine's home go under tools/state/; sessions, seats and agent stay at
    their 7ef0782 places until wave 4."""
    env = env_for(tmp_path)
    new_state, s = tmp_path / NEW["state"], tmp_path / ".session"
    assert writer_paths(env) == {
        "snapshot": new_state / "list/snapshot.json",
        "holders": new_state / "holders",
        "default-model": new_state / default_model.STATE_SUBDIR,
        "pane-state lock": new_state / pane_state.LOCK_DIR / pane_state.LOCK_NAME,
        "session dir": s / "sessions" / SID,
        "seat dir": s / "seats/scout",
        "agent home": s / "agent/home/alice",
        "sound sender": tmp_path / NEW["sound"] / "bin" / sound.SENDER}
    write_state(env)
    assert snapshot.snapshot_path(env).exists()
    assert (default_model.state_dir(env) / "log.jsonl").exists()
    assert not (tmp_path / ".session").exists()
    assert settings.leaf_path("state", env) == (new_state, "new")


def test_a_fresh_sound_write_beside_a_real_old_state_strands_nothing(tmp_path):
    """The Q1 nesting hazard, closed (do-003 criterion 2): a HOME with a real
    ~/.session/state and no sound folder. Installing the sound engine makes
    tools/state/sound/, and so tools/state/. The state leaf must still
    resolve OLD, with no `stranded` key, here and in `session path --json`."""
    from sessiontools import paths
    old_state = tmp_path / OLD["state"]
    (old_state / "list").mkdir(parents=True)
    env = env_for(tmp_path)
    assert sound.home(env) == tmp_path / NEW["sound"]              # sound is fresh: new
    sound.sender(env).parent.mkdir(parents=True)                  # the install writes bin/
    assert (tmp_path / NEW["state"]).is_dir()                     # ...so tools/state/ exists
    assert settings.leaf_path("state", env) == (old_state, "old")
    assert settings.leaf_stranded("state", env) is None
    assert snapshot.state_dir(env) == old_state
    assert paths.entry("state", env) == {"path": str(old_state), "source": "old"}
    assert paths.entry("sound", env) == {"path": str(tmp_path / NEW["sound"]), "source": "new"}
    write_state(env)                                              # state writers stay old
    assert sorted(os.listdir(tmp_path / NEW["state"])) == ["sound"]


def top_level(home: Path) -> set[str]:
    return {p.name for p in home.iterdir()}


def test_a_start_list_reap_cycle_on_a_fresh_home_makes_no_old_state(tmp_path, monkeypatch):
    """The success criterion's walk (do-003), in process, tmp HOME, no host
    step: a start (its holder claim, its session dir and launch record), a
    list (the snapshot), default-model and pane-state writes, a sound engine
    install folder, and a reap (its log). tools writes no ~/.session/state
    and no ~/.local/share; its only other top-level dot-dir is ~/.session
    holding the sessions leaf, which moves in wave 4."""
    from sessiontools import reap
    from sessiontools.start import record
    monkeypatch.setenv("HOME", str(tmp_path))
    env = env_for(tmp_path)
    # start: the default seat's claim, the session dir and its launch record.
    holders.write_claim("scout", {"pid": 1}, env)
    session = settings.session_dir(SID, env)
    session.mkdir(parents=True)                                   # the launcher's `mkdir -p`
    assert record.write_record(str(session), env, ["claude", "--session-id", SID]) is not None
    # list, and the state leaf's other writers.
    write_state(env)
    # the sound engine's home, as `session sound engine build` makes it.
    sound.sender(env).parent.mkdir(parents=True)
    # reap: one act, logged.
    row = SimpleNamespace(short=SID[:8], name="scout", tmux_socket="s", tmux_pane="%1")
    act = reap.Act(SimpleNamespace(row=row, task=None), reap.CLOSED, "a test act")
    reap.log_act(act, now=1_800_000_000.0, env=env)
    assert (settings.log_dir(settings.PACKAGES["session"], env) / "reap.log").exists()

    assert top_level(tmp_path) <= {".sessionkit", ".session"}
    assert not (tmp_path / OLD["state"]).exists()
    assert not (tmp_path / ".local").exists()
    assert top_level(tmp_path / ".session") == {"sessions"}      # the wave-4 leaf only
    assert (tmp_path / NEW["state"] / "list" / "snapshot.json").exists()
    assert (tmp_path / NEW["state"] / "holders" / "scout.json").exists()
    assert (tmp_path / NEW["sound"] / "bin").is_dir()


# --- AGENTS_DIR moves the agent-file lookup, and nothing else ---------------------------------

@pytest.mark.parametrize("layout", ["fresh", "old"])
def test_agents_dir_moves_only_the_launchers_lookup(tmp_path, layout):
    """decompose-001 review, gap 1: live seats set AGENTS_DIR to a
    taskmaster checkout. A seat's home and the settings reader's agent file
    must stay where they resolved at 7ef0782."""
    if layout == "old":
        old_layout(tmp_path)
    elsewhere = tmp_path / "taskmaster" / "agents"
    elsewhere.mkdir(parents=True)
    env = env_for(tmp_path, AGENTS_DIR=str(elsewhere))
    assert ag.agents_root(env) == str(elsewhere)
    assert settings.agent_root(env) == tmp_path / ".session/agent"
    assert settings.agent_home("alice", env) == tmp_path / ".session/agent/home/alice"
    assert settings.agent_config("alice", env).parent.parent in (
        tmp_path / ".session/agent", tmp_path / ".session/agent/config")
    assert str(elsewhere) not in str(settings.agent_config("alice", env))
    assert settings.leaf_path("agent", env)[1] != "override"


def test_agents_root_falls_back_to_the_pre_4_2_folder(tmp_path):
    env = env_for(tmp_path)
    assert ag.agents_root(env) == str(tmp_path / ".session/agents")
    (tmp_path / NEW["agent"] / "config").mkdir(parents=True)        # a moved agent leaf
    assert ag.agents_root(env) == str(tmp_path / NEW["agent"] / "config")


# --- the bash copy in hooks/busy-sound agrees with python ------------------------------------

def fresh_word(leaf: str) -> str:
    """The hook's third `leaf` word for LEAF: python's fresh default."""
    return "new" if leaf in settings.FRESH_NEW else "old"


def bash_leaf(old: Path, new: Path, fresh: str) -> str:
    """Run hooks/busy-sound's own `leaf` function, cut from the file."""
    text = SHIM.read_text()
    m = re.search(r"^leaf\(\) \{.*?^\}$", text, re.MULTILINE | re.DOTALL)
    assert m, "hooks/busy-sound has no leaf() function"
    r = subprocess.run(["/bin/sh", "-c",
                        m.group(0) + '\nleaf "$1" "$2" "$3"; printf %s "$at"',
                        "sh", str(old), str(new), fresh],
                       capture_output=True, text=True, check=True, timeout=10)
    return r.stdout


LAYOUTS = ["fresh", "old-only", "new-only", "migrated", "both-real", "old-elsewhere"]


def make(layout: str, old: Path, new: Path, tmp_path: Path) -> None:
    if layout in ("old-only", "both-real"):
        old.mkdir(parents=True)
    if layout in ("new-only", "both-real", "migrated", "old-elsewhere"):
        new.mkdir(parents=True)
    if layout == "migrated":
        old.parent.mkdir(parents=True, exist_ok=True)
        old.symlink_to(new)
    if layout == "old-elsewhere":
        (tmp_path / "elsewhere").mkdir()
        old.parent.mkdir(parents=True, exist_ok=True)
        old.symlink_to(tmp_path / "elsewhere")


@pytest.mark.parametrize("leaf", ["sessions", "sound"])
@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_hooks_bash_rule_is_pythons(tmp_path, leaf, layout):
    env = env_for(tmp_path)
    old, new = settings.leaf_old(leaf, env), settings.leaf_new(leaf, env)
    make(layout, old, new, tmp_path)
    assert bash_leaf(old, new, fresh_word(leaf)) == str(settings.leaf_path(leaf, env)[0])


def test_the_hook_spells_each_leaf_as_settings_does():
    text = SHIM.read_text()
    for leaf in ("sessions", "sound"):
        old = settings.leaf_old(leaf, {"HOME": "/H"}).as_posix().replace("/H", "$HOME", 1)
        new = settings.leaf_new(leaf, {"HOME": "/H"}).as_posix().replace("/H", "$HOME", 1)
        assert f'leaf "{old}" "{new}" {fresh_word(leaf)}\n' in text, leaf


@pytest.mark.parametrize("layout", ["old-only", "migrated", "new-only"])
def test_the_turn_mark_lands_where_python_resolves_with_no_sessions_dir(tmp_path, layout):
    """End to end through the real hook, SESSIONS_DIR unset: the shim finds
    the session dir by the bash rule, python writes the mark by its own."""
    env = env_for(tmp_path)
    old, new = settings.leaf_old("sessions", env), settings.leaf_new("sessions", env)
    make(layout, old, new, tmp_path)
    session = settings.session_dir(SID, env)
    session.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    run_env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path),
               "CLAUDE_CODE_SESSION_ID": SID, "SESSIONTOOLS_HOOK_TURN": "on",
               "SESSIONTOOLS_HOOK_BUSY": "off"}
    body = json.dumps({"session_id": SID, "hook_event_name": "Stop"})
    r = subprocess.run(["/bin/sh", str(SHIM), "stop"], input=body, env=run_env,
                       capture_output=True, text=True, timeout=30, check=False)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert (session / "turn-ended").exists()


# --- no module spells an old path but settings (the criterion's grep) ------------------------

SPELLING = re.compile(r"\.session/|\"\.session\"|'\.session'|\.local/share")
#: The places allowed to spell an old path, each with its reason.
ALLOWED = {
    "src/sessiontools/settings.py": "the ONE resolver",
    "src/sessiontools/cli.py": "the user's bash-completion folder, not a tools path",
    "hooks/busy-sound": "the one guarded bash copy of the rule (no python on the hot path)",
    "src/sessiontools/sound/engine.swift": "the compat default for a pre-0.3.2 plist; the "
                                          "plist passes --sessions (service.engine_sessions)",
}
#: Docstrings that name an OLD path as old.
AS_OLD = re.compile(r"\bold\b")


def test_no_module_spells_an_old_path_but_the_allowed():
    bad = []
    for base in ("src", "hooks"):
        for path in sorted((ROOT / base).rglob("*")):
            if not path.is_file() or path.suffix in (".pyc",) or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED:
                continue
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if SPELLING.search(line) and not AS_OLD.search(line):
                    bad.append(f"{rel}:{n}: {line.strip()}")
    assert bad == []


def test_the_allowed_spellings_are_only_where_they_are_said_to_be():
    cli = (ROOT / "src/sessiontools/cli.py").read_text().splitlines()
    assert [s for s in cli if SPELLING.search(s)] == [
        "    session completion bash > ~/.local/share/bash-completion/completions/session"]
    swift = (ROOT / "src/sessiontools/sound/engine.swift").read_text().splitlines()
    assert [s for s in swift if SPELLING.search(s) and not s.lstrip().startswith("//")] == [
        'var sessions = "~/.session/sessions"']
