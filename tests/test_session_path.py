"""`session path` (ST-266 do-002, READY A): a view onto the settings resolvers.

Each leaf prints the path `settings.leaf_path` (or, for log,
`settings.log_dir`) answers; NAME joins sessions and seats; `--json` gives
every leaf's path and source, and stranded only when the resolver says so.
The source strings are pinned: sessionmail's vmailbox-gen parses them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools import cli, completion, paths, settings, verbs
from sessiontools.start import agents, default_seat
from sessiontools.start.errors import LaunchError

ROOT = Path(__file__).resolve().parents[1]
#: The variables that move a leaf or the agents folder: a subprocess runs without them.
OVERRIDES = {*(v for v in settings.LEAF_OVERRIDE.values() if v), agents.AGENTS_DIR_ENV}

SID = "0b1c2d3e-0000-4000-8000-00000000abcd"


def run(capsys, *argv, env) -> tuple[int, str, str]:
    code = paths.main(list(argv), env)
    out, err = capsys.readouterr()
    return code, out, err


def env_for(home: Path, **extra) -> dict:
    return {"HOME": str(home), **extra}


# --- the words a peer parses ---------------------------------------------------------------

def test_the_source_strings_are_exactly_these():
    """sessionmail's vmailbox-gen parses `source`: these five words, no others."""
    assert paths.SOURCES == ("override", "old", "new", "fresh-old", "fresh-new")
    assert paths.SOURCES is settings.LEAF_SOURCES


def test_the_leaves_are_the_resolvers_and_log():
    assert paths.LEAVES == (*settings.LEAVES, "log")
    assert paths.LEAVES == ("state", "sessions", "seats", "agent", "backups", "sound", "log")


# --- each leaf prints what the resolver answers ---------------------------------------------

@pytest.mark.parametrize("leaf", settings.LEAVES)
@pytest.mark.parametrize("layout", ["fresh", "old", "new"])
def test_each_leaf_prints_the_resolvers_path(capsys, tmp_path, leaf, layout):
    env = env_for(tmp_path)
    if layout == "old":
        settings.leaf_old(leaf, env).mkdir(parents=True)
    if layout == "new":
        settings.leaf_new(leaf, env).mkdir(parents=True)
    code, out, err = run(capsys, leaf, env=env)
    assert (code, err) == (0, "")
    assert out == f"{settings.leaf_path(leaf, env)[0]}\n"
    assert Path(out.strip()).is_absolute()
    fresh = "fresh-new"                     # every leaf on a fresh host (ST-288)
    want = {"fresh": fresh, "old": "old", "new": "new"}[layout]
    assert paths.entry(leaf, env)["source"] == want


def test_log_is_tools_log_dir(capsys, tmp_path):
    env = env_for(tmp_path)
    log = settings.log_dir(settings.PACKAGES["session"], env)
    assert run(capsys, "log", env=env) == (0, f"{log}\n", "")
    assert paths.entry("log", env) == {"path": str(log), "source": "fresh-new"}
    log.mkdir(parents=True)
    assert paths.entry("log", env) == {"path": str(log), "source": "new"}


def test_a_read_makes_nothing(capsys, tmp_path):
    env = env_for(tmp_path)
    for argv in ([], ["--json"], *([leaf] for leaf in paths.LEAVES)):
        assert run(capsys, *argv, env=env)[0] == 0
    assert list(tmp_path.iterdir()) == []


# --- NAME ------------------------------------------------------------------------------------

def test_name_joins_a_session_and_a_seat(capsys, tmp_path):
    env = env_for(tmp_path)
    assert run(capsys, "sessions", SID, env=env) == (0, f"{settings.session_dir(SID, env)}\n", "")
    assert run(capsys, "seats", "scout", env=env) == (0, f"{settings.seat_dir('scout', env)}\n", "")
    assert settings.session_dir(SID, env) == tmp_path / ".sessionkit/tools/state/sessions" / SID


def test_name_is_a_literal_join_never_a_short_id_lookup(capsys, tmp_path):
    env = env_for(tmp_path)
    (tmp_path / ".session/sessions" / SID).mkdir(parents=True)
    code, out, _ = run(capsys, "sessions", SID[:8], env=env)
    assert (code, out) == (0, f"{tmp_path / '.session/sessions' / SID[:8]}\n")


@pytest.mark.parametrize("leaf", ["state", "backups", "sound", "log"])
def test_a_leaf_that_takes_no_name_refuses_one(capsys, tmp_path, leaf):
    code, out, err = run(capsys, leaf, "x", env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert f"{leaf} takes no NAME: only sessions, seats and agent do" in err
    assert "usage: session path" in err


@pytest.mark.parametrize("leaf,noun", [("sessions", "is not a session id"),
                                       ("seats", "is not a seat"),
                                       ("agent", "is not an agent")])
def test_each_named_leaf_refuses_in_its_own_noun(capsys, tmp_path, leaf, noun):
    """design review F1: a per-leaf noun, sessions and seats unchanged."""
    code, out, err = run(capsys, leaf, "..", env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert f"'..' {noun}" in err


@pytest.mark.parametrize("name", [".", "..", "a/b", "../x", ""])
def test_a_name_that_would_leave_the_leaf_is_refused(capsys, tmp_path, name):
    code, out, err = run(capsys, "sessions", name, env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert "is not a session id" in err


# --- agent NAME: the agent's home (ST-291) --------------------------------------------------

def test_agent_name_is_its_home(capsys, tmp_path):
    """SC1: NAME's home is settings.agent_home, and the same string as
    `$(session path agent)/home/NAME`, byte for byte."""
    env = env_for(tmp_path)
    (tmp_path / ".session/agent/home/alice").mkdir(parents=True)
    code, out, err = run(capsys, "agent", "alice", env=env)
    assert (code, err) == (0, "")
    assert out == f"{settings.agent_home('alice', env)}\n"
    leaf = run(capsys, "agent", env=env)[1]
    assert out == leaf.rstrip("\n") + "/home/alice\n"
    assert out == f"{tmp_path / '.session/agent/home/alice'}\n"


def test_agent_name_through_uv_run_session(tmp_path):
    """design review F2: SC1's literal words, `uv run session path agent
    alice`, in this checkout (never the installed binary)."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    (tmp_path / ".session/agent/home/alice").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k not in OVERRIDES}
    env.setdefault("UV_CACHE_DIR", str(Path.home() / ".cache/uv"))   # before HOME moves
    env["HOME"] = str(tmp_path)
    r = subprocess.run([uv, "run", "--project", str(ROOT), "session", "path", "agent", "alice"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"{settings.agent_home('alice', env_for(tmp_path))}\n"
    assert r.stdout == f"{tmp_path / '.session/agent/home/alice'}\n"


def test_an_absent_home_is_the_same_join_and_nothing_is_made(capsys, tmp_path):
    """SC2: no home/zed/ -- the path anyway, exit 0, nothing made."""
    env = env_for(tmp_path)
    (tmp_path / ".session/agent").mkdir(parents=True)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    code, out, err = run(capsys, "agent", "zed", env=env)
    assert (code, out, err) == (0, f"{settings.agent_home('zed', env)}\n", "")
    assert not settings.agent_home("zed", env).exists()
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before


def test_agents_dir_never_moves_an_agents_home(capsys, tmp_path):
    env = env_for(tmp_path, AGENTS_DIR=str(tmp_path / "taskmaster/agents"))
    code, out, _ = run(capsys, "agent", "alice", env=env)
    assert (code, out) == (0, f"{settings.agent_home('alice', env)}\n")
    assert "taskmaster" not in out


# --- agent [NAME] --config: the launcher's lookup (ST-291) -----------------------------------

def make_rung(rung: str, home: Path) -> dict:
    """An env whose agents_root answers from RUNG, with a dir agent `dee`
    and a flat agent `flo` in that folder."""
    if rung == "AGENTS_DIR":
        root = home / "elsewhere/agents"                 # absolute (F4)
        env = env_for(home, AGENTS_DIR=str(root))
    elif rung == "config":
        root = home / ".session/agent/config"
        env = env_for(home)
    else:
        root = home / ".session/agents"
        env = env_for(home)
    (root / "dee").mkdir(parents=True)
    (root / "dee/dee.md").write_text("---\n---\n")
    (root / "flo.md").write_text("---\n---\n")
    return env


@pytest.mark.parametrize("rung", ["AGENTS_DIR", "config", "agents"])
def test_config_prints_the_agents_folder_on_each_rung(capsys, tmp_path, rung):
    """SC3 (plain half): `agent --config` is agents_root, one test per rung."""
    env = make_rung(rung, tmp_path)
    code, out, err = run(capsys, "agent", "--config", env=env)
    assert (code, err) == (0, "")
    assert out == f"{os.path.abspath(agents.agents_root(env))}\n"
    assert agents.agents_rung(env) == (agents.agents_root(env), rung)


@pytest.mark.parametrize("rung", ["AGENTS_DIR", "config", "agents"])
def test_config_names_a_dir_agents_folder_and_nothing_for_a_flat_one(capsys, tmp_path, rung):
    """SC3: a directory agent -> resolve_agent(N).directory; a flat agent ->
    zero bytes (F3), exit 0."""
    env = make_rung(rung, tmp_path)
    code, out, err = run(capsys, "agent", "dee", "--config", env=env)
    assert (code, err) == (0, "")
    assert out == f"{agents.resolve_agent('dee', environ=env).directory}\n"
    assert run(capsys, "agent", "flo", "--config", env=env) == (0, "", "")
    assert agents.resolve_agent("flo", environ=env).directory == ""


def test_config_finds_the_old_flat_folder_and_config_wins_over_it(capsys, tmp_path):
    """SC7: only the old agents/bob/{bob.md,avatar.jpg} -> agents/bob; a flat
    agents/carol.md -> nothing; config/bob/bob.md as well -> config's bob."""
    env = env_for(tmp_path)
    old = tmp_path / ".session/agents"
    (old / "bob").mkdir(parents=True)
    (old / "bob/bob.md").write_text("---\n---\n")
    (old / "bob/avatar.jpg").write_bytes(b"jpg")
    (old / "carol.md").write_text("---\n---\n")
    assert run(capsys, "agent", "bob", "--config", env=env) == (0, f"{old / 'bob'}\n", "")
    assert run(capsys, "agent", "carol", "--config", env=env) == (0, "", "")
    config = tmp_path / ".session/agent/config"
    (config / "bob").mkdir(parents=True)
    (config / "bob/bob.md").write_text("---\n---\n")
    assert run(capsys, "agent", "bob", "--config", env=env) == (0, f"{config / 'bob'}\n", "")


def test_config_on_the_m5_layout(capsys, tmp_path):
    """SC8 (super 12:26): a real <leaf>/config/<n>/{<n>.md,avatar.jpg} with
    the old agents/<n> a SYMLINK to ../agent/config/<n> resolves to the
    config/ folder; an old-only real agents/<n>/ is agents/<n> while no
    config/ exists."""
    env = env_for(tmp_path)
    old = tmp_path / ".session/agents"
    (old / "olga").mkdir(parents=True)                   # old-only, before the move
    (old / "olga/olga.md").write_text("---\n---\n")
    assert run(capsys, "agent", "olga", "--config", env=env) == (0, f"{old / 'olga'}\n", "")
    config = tmp_path / ".session/agent/config"
    (config / "mia").mkdir(parents=True)
    (config / "mia/mia.md").write_text("---\n---\n")
    (config / "mia/avatar.jpg").write_bytes(b"jpg")
    (old / "mia").symlink_to("../agent/config/mia")
    assert (old / "mia/avatar.jpg").is_file()            # the link is live
    code, out, err = run(capsys, "agent", "mia", "--config", env=env)
    assert (code, out, err) == (0, f"{config / 'mia'}\n", "")


def test_config_for_the_built_in_seat_is_nothing(capsys, tmp_path):
    """The default seat's word resolves to the package's template: no
    folder, so zero bytes and exit 0."""
    word = default_seat.word()
    assert run(capsys, "agent", word, "--config", env=env_for(tmp_path)) == (0, "", "")


def test_config_for_an_unknown_agent_is_the_launchers_refusal(capsys, tmp_path):
    env = env_for(tmp_path)
    with pytest.raises(LaunchError) as launcher:
        agents.resolve_agent("nobody", environ=env)
    code, out, err = run(capsys, "agent", "nobody", "--config", env=env)
    assert (code, out) == (2, "")
    assert f"session path: {launcher.value}\n" in err
    assert "usage: session path" in err


def test_config_for_a_folder_without_its_md_is_the_launchers_refusal(capsys, tmp_path):
    env = env_for(tmp_path)
    (tmp_path / ".session/agents/hollow").mkdir(parents=True)
    with pytest.raises(LaunchError) as launcher:
        agents.resolve_agent("hollow", environ=env)
    assert "hollow.md is missing" in str(launcher.value)
    code, out, err = run(capsys, "agent", "hollow", "--config", env=env)
    assert (code, out) == (2, "")
    assert f"session path: {launcher.value}\n" in err
    assert "usage: session path" in err


BAD_NAMES = ["", ".", "..", "a/b", "../x", "/", "nu\0l"]


@pytest.mark.parametrize("config", [[], ["--config"]], ids=["plain", "config"])
@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_bad_agent_name_is_exit_2_with_usage(capsys, tmp_path, name, config):
    """SC4: a NAME that would leave the leaf, plain or --config."""
    code, out, err = run(capsys, "agent", name, *config, env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert f"{name!r} is not an agent" in err and "usage: session path" in err


@pytest.mark.parametrize("argv", [["sessions", "--config"], ["sessions", SID, "--config"],
                                  ["seats", "--config"], ["state", "--config"],
                                  ["log", "--config"], ["--config"]])
def test_config_on_any_other_leaf_is_exit_2(capsys, tmp_path, argv):
    """SC4: --config is the agent leaf's only; with no LEAF it is refused
    too (decompose-001 r2 advisory c)."""
    code, out, err = run(capsys, *argv, env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert "--config is for the agent leaf only" in err and "usage: session path" in err


def test_only_the_agent_branch_imports_the_launcher():
    """design §3: start.agents (and PyYAML behind it) is imported lazily, so
    `session path LEAF` keeps its import cost for every other leaf."""
    code = ("import sys, sessiontools.paths as p; p.main(['state'], {'HOME': '/nonexistent'}); "
            "assert 'sessiontools.start.agents' not in sys.modules, 'eager'; "
            "assert 'yaml' not in sys.modules, 'yaml'")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr


def test_config_reads_make_nothing(capsys, tmp_path):
    env = env_for(tmp_path)
    for argv in (["agent", "--config"], ["agent", "alice"], ["agent", "nobody", "--config"],
                 ["agent", default_seat.word(), "--config"]):
        run(capsys, *argv, env=env)
    assert list(tmp_path.iterdir()) == []


# --- an unknown leaf -------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["bogus"], ["bogus", "--json"], ["state/"], ["STATE"]])
def test_an_unknown_leaf_is_exit_2_with_usage(capsys, tmp_path, argv):
    code, out, err = run(capsys, *argv, env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert "unknown leaf" in err and "usage: session path" in err
    for leaf in paths.LEAVES:
        assert leaf in err


# --- --json ----------------------------------------------------------------------------------

def test_json_lists_every_leaf_with_path_and_source(capsys, tmp_path):
    env = env_for(tmp_path)
    code, out, err = run(capsys, "--json", env=env)
    assert (code, err) == (0, "")
    data = json.loads(out)
    assert list(data) == list(paths.LEAVES)
    for leaf, e in data.items():
        assert set(e) == {"path", "source"}, leaf
        assert e["source"] in paths.SOURCES
        assert Path(e["path"]).is_absolute()
    for leaf in settings.LEAVES:
        found, source = settings.leaf_path(leaf, env)
        assert data[leaf] == {"path": str(found), "source": source}


def test_json_of_one_leaf_is_its_entry(capsys, tmp_path):
    env = env_for(tmp_path)
    code, out, _ = run(capsys, "sessions", "--json", env=env)
    assert code == 0 and json.loads(out) == paths.entry("sessions", env)
    code, out, _ = run(capsys, "sessions", SID, "--json", env=env)
    assert json.loads(out) == {"path": str(settings.session_dir(SID, env)), "source": "fresh-new"}


@pytest.mark.parametrize("leaf,var", [("state", "SESSION_STATE_DIR"), ("sessions", "SESSIONS_DIR"),
                                      ("sound", "SESSIONTOOLS_SOUND_HOME")])
def test_an_override_says_override(capsys, tmp_path, leaf, var):
    settings.leaf_old(leaf, env_for(tmp_path)).mkdir(parents=True)
    env = env_for(tmp_path, **{var: str(tmp_path / "mine")})
    data = json.loads(run(capsys, "--json", env=env)[1])
    assert data[leaf] == {"path": str(tmp_path / "mine"), "source": "override"}
    assert run(capsys, leaf, env=env)[1] == f"{tmp_path / 'mine'}\n"


def test_agents_dir_is_not_the_agent_leafs_override(capsys, tmp_path):
    env = env_for(tmp_path, AGENTS_DIR=str(tmp_path / "taskmaster/agents"))
    assert json.loads(run(capsys, "agent", "--json", env=env)[1]) == {
        "path": str(tmp_path / ".sessionkit/tools/state/agent"), "source": "fresh-new"}


def test_a_migrated_leaf_says_new(capsys, tmp_path):
    env = env_for(tmp_path)
    new, old = settings.leaf_new("seats", env), settings.leaf_old("seats", env)
    new.mkdir(parents=True)
    old.parent.mkdir(parents=True)
    old.symlink_to(new)
    assert json.loads(run(capsys, "seats", "--json", env=env)[1]) == {"path": str(new), "source": "new"}


# --- stranded --------------------------------------------------------------------------------

def test_both_real_reports_the_new_folder_stranded(capsys, tmp_path):
    env = env_for(tmp_path)
    settings.leaf_old("sessions", env).mkdir(parents=True)
    new = settings.leaf_new("sessions", env)
    new.mkdir(parents=True)
    (new / SID).mkdir()
    data = json.loads(run(capsys, "--json", env=env)[1])
    assert data["sessions"] == {"path": str(settings.leaf_old("sessions", env)), "source": "old",
                                "stranded": str(new)}
    assert "stranded" in run(capsys, env=env)[1]


def test_stranded_ignores_nested_leaves(capsys, tmp_path):
    """The decompose-001 amendment: a real ~/.session/state and a
    tools/state/ holding only sound/ is state `old`, with no stranded key."""
    env = env_for(tmp_path)
    (tmp_path / ".session/state").mkdir(parents=True)
    (tmp_path / ".sessionkit/tools/state/sound").mkdir(parents=True)
    data = json.loads(run(capsys, "--json", env=env)[1])
    assert data["state"] == {"path": str(tmp_path / ".session/state"), "source": "old"}
    assert data["sound"] == {"path": str(tmp_path / ".sessionkit/tools/state/sound"), "source": "new"}
    (tmp_path / ".sessionkit/tools/state/holders").mkdir()            # one of state's own
    data = json.loads(run(capsys, "--json", env=env)[1])
    assert data["state"]["stranded"] == str(tmp_path / ".sessionkit/tools/state")


# --- agent NAME --json: the contract (ST-291 do-002, design §3) ---------------------------------

def json_of(capsys, *argv, env) -> dict:
    code, out, err = run(capsys, *argv, env=env)
    assert (code, err) == (0, ""), err
    return json.loads(out)


#: rung -> the source design §3 gives the agents folder in make_rung's layouts.
RUNG_SOURCE = {"AGENTS_DIR": "override", "config": "old", "agents": "old"}


@pytest.mark.parametrize("rung", ["AGENTS_DIR", "config", "agents"])
@pytest.mark.parametrize("name", ["dee", "flo"], ids=["dir", "flat"])
def test_agent_json_is_the_contract_on_each_rung(capsys, tmp_path, rung, name):
    """SC3 (json half): version, root, and items home/agents/config, for a
    directory and a flat agent, one test per rung; config's path and file
    are resolve_agent(N)'s, agents' rung is agents_rung's."""
    env = make_rung(rung, tmp_path)
    data = json_of(capsys, "agent", name, "--json", env=env)
    assert list(data) == ["version", "root", "items"]
    assert data["root"] == paths.entry("agent", env)
    assert list(data["items"]) == ["home", "agents", "config"]
    home, folder, config = (data["items"][k] for k in ("home", "agents", "config"))
    assert home == {"path": str(settings.agent_home(name, env)), "source": data["root"]["source"]}
    root, got_rung = agents.agents_rung(env)
    assert got_rung == rung
    assert folder == {"path": root, "source": RUNG_SOURCE[rung], "rung": rung}
    found = agents.resolve_agent(name, environ=env)
    assert config == {"path": found.directory, "source": RUNG_SOURCE[rung],
                      "file": found.file, "builtin": False}
    assert config["path"] == (str(Path(root) / name) if name == "dee" else "")
    assert config["file"] == str(Path(root) / ("dee/dee.md" if name == "dee" else "flo.md"))


def test_agent_json_version_is_the_packages(capsys, tmp_path, monkeypatch):
    """version is sessiontools.__version__, read at call time, so a checkout
    whose metadata is unreadable says '0+unknown'."""
    import sessiontools
    env = env_for(tmp_path)
    assert json_of(capsys, "agent", "alice", "--json", env=env)["version"] == sessiontools.__version__
    monkeypatch.setattr(sessiontools, "__version__", "0+unknown")
    assert json_of(capsys, "agent", "alice", "--json", env=env)["version"] == "0+unknown"
    assert paths.agent_report("alice", env)["version"] == "0+unknown"


def test_the_verb_prints_agent_report(capsys, tmp_path):
    """For Python peers: paths.agent_report is the document the verb prints."""
    env = make_rung("config", tmp_path)
    assert json_of(capsys, "agent", "dee", "--json", env=env) == paths.agent_report("dee", env)


def test_agents_folder_sources_on_a_bare_and_a_new_home(capsys, tmp_path):
    """No folder at all, a fresh host: the agent leaf's config/ rung,
    `fresh-new` (R8; it was the old flat rung, `fresh-old`). A new home's
    config/ takes the agent leaf's source, `new`."""
    bare, new = tmp_path / "bare", tmp_path / "new"
    bare.mkdir()
    folder = json_of(capsys, "agent", "zed", "--json", env=env_for(bare))["items"]["agents"]
    assert folder == {"path": str(bare / ".sessionkit/tools/state/agent/config"),
                      "source": "fresh-new", "rung": "config"}
    config = new / ".sessionkit/tools/state/agent/config"
    (config / "dee").mkdir(parents=True)
    (config / "dee/dee.md").write_text("---\n---\n")
    data = json_of(capsys, "agent", "dee", "--json", env=env_for(new))
    assert data["root"] == {"path": str(new / ".sessionkit/tools/state/agent"), "source": "new"}
    assert data["items"]["agents"] == {"path": str(config), "source": "new", "rung": "config"}
    assert data["items"]["config"]["path"] == str(config / "dee")


# --- R8: a fresh host's agents folder is the agent leaf's config/ (ST-291 do-004) ------------

#: The agents folder on a fresh host, under HOME: the agent leaf's config/.
FRESH_CONFIG = ".sessionkit/tools/state/agent/config"


def test_fresh_host_agents_folder_is_the_agent_leafs_config(capsys, tmp_path):
    """SC9: empty HOME, no AGENTS_DIR: `agent --config --json` names the
    agent leaf's config/, fresh-new, rung config - never ~/.session/agents.
    The plain form and the launcher (agents_root) say the same path."""
    env = env_for(tmp_path)
    assert not settings.old_root_real(env)
    want = str(tmp_path / FRESH_CONFIG)
    assert json_of(capsys, "agent", "--config", "--json", env=env) == {
        "path": want, "source": "fresh-new", "rung": "config"}
    assert run(capsys, "agent", "--config", env=env) == (0, f"{want}\n", "")
    assert agents.agents_rung(env) == (want, "config")
    assert agents.agents_root(env) == want
    assert not (tmp_path / ".session").exists()          # asking made nothing


def test_fresh_host_after_the_kit_writes_claude_md(capsys, tmp_path):
    """SC9: the kit's contract (SK-257) - mkdir the folder the verb names,
    write <path>/claude.md. Afterwards no leaf moves or turns old: every
    path is the one it was, nothing says old or fresh-old, the leaves the
    write did not make stay fresh-new, and resolve_agent finds claude."""
    env = env_for(tmp_path)
    before = json_of(capsys, "--json", env=env)
    assert {e["source"] for e in before.values()} == {"fresh-new"}
    folder = Path(json_of(capsys, "agent", "--config", "--json", env=env)["path"])
    folder.mkdir(parents=True)
    (folder / "claude.md").write_text("---\n---\n")
    after = json_of(capsys, "--json", env=env)
    assert {k: v["path"] for k, v in after.items()} == {k: v["path"] for k, v in before.items()}
    assert not settings.old_root_real(env)
    assert not (tmp_path / ".session").exists()
    made = {"state", "agent"}                 # the write made these two folders
    for leaf, e in after.items():
        assert e["source"] == ("new" if leaf in made else "fresh-new"), leaf
    assert json_of(capsys, "agent", "--config", "--json", env=env) == {
        "path": str(folder), "source": "new", "rung": "config"}
    found = agents.resolve_agent("claude", environ=env)
    assert (found.file, found.directory, found.builtin) == (str(folder / "claude.md"), "", False)


def test_live_host_chain_is_unchanged(capsys, tmp_path):
    """A host with a real ~/.session/ and no agent folders answers as
    before R8: the old flat rung, fresh-old. With the old folder there, it
    is `old`; with the agent leaf's config/ there, config/ wins (SC8)."""
    (tmp_path / ".session").mkdir()
    env = env_for(tmp_path)
    assert settings.old_root_real(env)
    legacy = str(settings.legacy_agents_dir(env))
    assert json_of(capsys, "agent", "--config", "--json", env=env) == {
        "path": legacy, "source": "fresh-old", "rung": "agents"}
    Path(legacy).mkdir()
    assert json_of(capsys, "agent", "--config", "--json", env=env) == {
        "path": legacy, "source": "old", "rung": "agents"}
    config = tmp_path / ".session/agent/config"
    config.mkdir(parents=True)
    assert agents.agents_rung(env) == (str(config), "config")


def test_moved_root_keeps_an_old_folder_that_is_there(capsys, tmp_path):
    """A host whose ~/.session links to tools' state folder is not
    old_root_real, but its old flat folder (reached through the link) is
    there: the chain still answers it, as before R8. Only a folder that is
    NOT there gives way to the agent leaf's config/."""
    state = tmp_path / ".sessionkit/tools/state"
    state.mkdir(parents=True)
    (tmp_path / ".session").symlink_to(state)
    env = env_for(tmp_path)
    assert not settings.old_root_real(env)
    assert agents.agents_rung(env) == (str(tmp_path / FRESH_CONFIG), "config")
    (state / "agents").mkdir()
    assert agents.agents_rung(env) == (str(settings.legacy_agents_dir(env)), "agents")


def test_agents_dir_still_wins_on_a_fresh_host(tmp_path):
    """R8 changes the third rung only: AGENTS_DIR answers first, fresh or not."""
    env = env_for(tmp_path, AGENTS_DIR=str(tmp_path / "elsewhere"))
    assert agents.agents_rung(env) == (str(tmp_path / "elsewhere"), "AGENTS_DIR")


def test_agent_json_for_the_built_in_seat(capsys, tmp_path):
    word = default_seat.word()
    env = env_for(tmp_path)
    config = json_of(capsys, "agent", word, "--json", env=env)["items"]["config"]
    found = agents.resolve_agent(word, environ=env)
    assert found.builtin
    assert config == {"path": "", "source": "fresh-new", "file": found.file, "builtin": True}


@pytest.mark.parametrize("name", ["nobody", "hollow"])
def test_a_refused_agent_still_gets_its_home(capsys, tmp_path, name):
    """An unknown NAME or a folder without its NAME.md: exit 0, config
    carries the launcher's message as `refused`, and home is a true answer."""
    env = env_for(tmp_path)
    (tmp_path / ".session/agents/hollow").mkdir(parents=True)
    with pytest.raises(LaunchError) as launcher:
        agents.resolve_agent(name, environ=env)
    data = json_of(capsys, "agent", name, "--json", env=env)
    assert data["items"]["config"] == {"path": "", "source": "old", "file": "", "builtin": False,
                                       "refused": str(launcher.value)}
    assert data["items"]["home"] == {"path": str(settings.agent_home(name, env)),
                                     "source": data["root"]["source"]}


@pytest.mark.parametrize("name", ["dee", "flo", "nobody", "hollow", "seat"])
def test_config_json_is_the_same_document(capsys, tmp_path, name):
    """`agent NAME --config --json` == `agent NAME --json`, byte for byte."""
    env = make_rung("agents", tmp_path)
    (tmp_path / ".session/agents/hollow").mkdir()
    name = default_seat.word() if name == "seat" else name
    plain = run(capsys, "agent", name, "--json", env=env)
    assert plain[0] == 0
    assert run(capsys, "agent", name, "--config", "--json", env=env) == plain
    assert run(capsys, "agent", "--json", name, "--config", env=env) == plain


@pytest.mark.parametrize("rung", ["AGENTS_DIR", "config", "agents"])
def test_config_json_without_a_name_is_the_agents_folder_alone(capsys, tmp_path, rung):
    """`agent --config --json`: items.agents, unwrapped, as LEAF --json is."""
    env = make_rung(rung, tmp_path)
    code, out, err = run(capsys, "agent", "--config", "--json", env=env)
    assert (code, err) == (0, "")
    assert out == json.dumps(paths.agents_entry(env)) + "\n"
    assert json.loads(out) == paths.agent_report("dee", env)["items"]["agents"]
    assert json.loads(out)["path"] == run(capsys, "agent", "--config", env=env)[1].rstrip("\n")


def test_the_root_is_agent_json_and_carries_stranded(capsys, tmp_path):
    """R2: root is exactly what `session path agent --json` prints in the
    same env, so `stranded` rides when it is set."""
    env = env_for(tmp_path)
    (tmp_path / ".session/agent/home/alice").mkdir(parents=True)
    (tmp_path / ".sessionkit/tools/state/agent/x").mkdir(parents=True)   # stranded
    leaf = json_of(capsys, "agent", "--json", env=env)
    assert leaf["stranded"] == str(tmp_path / ".sessionkit/tools/state/agent")
    assert json_of(capsys, "agent", "alice", "--json", env=env)["root"] == leaf
    shutil.rmtree(tmp_path / ".sessionkit")
    leaf = json_of(capsys, "agent", "--json", env=env)
    assert "stranded" not in leaf
    assert json_of(capsys, "agent", "alice", "--json", env=env)["root"] == leaf


@pytest.mark.parametrize("flags", [["--json"], ["--config", "--json"]], ids=["json", "config-json"])
@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_bad_agent_name_with_json_is_exit_2(capsys, tmp_path, name, flags):
    """SC4 (json half): a NAME that would leave the leaf, empty stdout."""
    code, out, err = run(capsys, "agent", name, *flags, env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert f"{name!r} is not an agent" in err and "usage: session path" in err


@pytest.mark.parametrize("argv", [["sessions", "--config", "--json"],
                                  ["seats", "s1", "--config", "--json"],
                                  ["state", "--config", "--json"], ["--config", "--json"]])
def test_config_json_on_any_other_leaf_is_exit_2(capsys, tmp_path, argv):
    code, out, err = run(capsys, *argv, env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert "--config is for the agent leaf only" in err and "usage: session path" in err


def test_agent_json_reads_make_nothing(capsys, tmp_path):
    env = env_for(tmp_path)
    for argv in (["agent", "alice", "--json"], ["agent", "--config", "--json"],
                 ["agent", "nobody", "--config", "--json"]):
        run(capsys, *argv, env=env)
    assert list(tmp_path.iterdir()) == []


#: Every JSON shape that existed before ST-291 do-002, byte for byte, captured
#: at f0a0359 in GOLDEN_LAYOUT ({H} is HOME). A change here breaks a reader.
GOLDEN_LAYOUT = (".session/agent/home/alice", ".session/sessions", ".sessionkit/tools/state/agent/x")
GOLDEN = {
    ("--json",): (
        '{\n "state": {\n  "path": "{H}/.sessionkit/tools/state",\n  "source": "new"\n },\n'
        ' "sessions": {\n  "path": "{H}/.session/sessions",\n  "source": "old"\n },\n'
        ' "seats": {\n  "path": "{H}/.session/seats",\n  "source": "fresh-old"\n },\n'
        ' "agent": {\n  "path": "{H}/.session/agent",\n  "source": "old",\n'
        '  "stranded": "{H}/.sessionkit/tools/state/agent"\n },\n'
        ' "backups": {\n  "path": "{H}/.session/backups",\n  "source": "fresh-old"\n },\n'
        ' "sound": {\n  "path": "{H}/.sessionkit/tools/state/sound",\n  "source": "fresh-new"\n },\n'
        ' "log": {\n  "path": "{H}/.sessionkit/tools/log",\n  "source": "fresh-new"\n }\n}\n'),
    ("state", "--json"): '{"path": "{H}/.sessionkit/tools/state", "source": "new"}\n',
    ("agent", "--json"): ('{"path": "{H}/.session/agent", "source": "old", '
                          '"stranded": "{H}/.sessionkit/tools/state/agent"}\n'),
    ("sessions", "--json"): '{"path": "{H}/.session/sessions", "source": "old"}\n',
    ("sessions", SID, "--json"): f'{{"path": "{{H}}/.session/sessions/{SID}", "source": "old"}}\n',
    ("seats", "s1", "--json"): '{"path": "{H}/.session/seats/s1", "source": "fresh-old"}\n',
    ("log", "--json"): '{"path": "{H}/.sessionkit/tools/log", "source": "fresh-new"}\n',
}


@pytest.mark.parametrize("argv", list(GOLDEN), ids=[" ".join(a)[:20] for a in GOLDEN])
def test_existing_json_shapes_are_byte_identical(capsys, tmp_path, argv):
    """SC4/R2: session path --json, LEAF --json, sessions ID --json and
    seats SEAT --json print exactly the bytes they printed before do-002."""
    for rel in GOLDEN_LAYOUT:
        (tmp_path / rel).mkdir(parents=True)
    code, out, err = run(capsys, *argv, env=env_for(tmp_path))
    assert (code, err) == (0, "")
    assert out == GOLDEN[argv].replace("{H}", str(tmp_path))


# --- the table, for a person -----------------------------------------------------------------

def test_bare_path_prints_one_line_per_leaf(capsys, tmp_path):
    env = env_for(tmp_path)
    code, out, err = run(capsys, env=env)
    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert [x.split()[0] for x in lines] == list(paths.LEAVES)
    for line, leaf in zip(lines, paths.LEAVES):
        e = paths.entry(leaf, env)
        assert line.split() == [leaf, e["source"], e["path"]]


# --- wired as the other verbs are ------------------------------------------------------------

def test_path_is_a_verb_with_a_handler_parser_and_usage_row(capsys):
    assert cli.HANDLERS["path"] is cli._path
    assert cli.PARSERS["path"] == "sessiontools.paths:GROUP"   # migrate is its sub (do-004)
    assert "path" in dict(cli.VERBS)
    assert cli.main(["--help"]) == 0
    assert "  path " in capsys.readouterr().out


def test_session_path_through_the_dispatcher(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("SESSION_STATE_DIR", "SESSIONS_DIR", "SESSIONTOOLS_SOUND_HOME"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["path", "state"]) == 0                    # fresh: the new folder
    assert capsys.readouterr().out == f"{tmp_path / '.sessionkit/tools/state'}\n"
    assert cli.main(["path", "nope"]) == 2


def test_help_names_every_leaf_and_source(capsys):
    with pytest.raises(SystemExit) as done:
        paths.main(["--help"])
    assert done.value.code == 0
    out = capsys.readouterr().out
    for word in (*paths.LEAVES, *paths.SOURCES):
        assert word in out


def test_help_rungs_are_the_launchers():
    """The help names agents_rung's words without importing start: the copy
    is held to the one rule here."""
    assert paths._RUNGS == agents.RUNGS


def test_help_documents_the_agent_surfaces(capsys):
    """do-003: every agent surface, its JSON keys and its exit codes are in
    the help."""
    with pytest.raises(SystemExit):
        paths.main(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    for words in ("agent --config prints the agents folder",
                  "with NAME, NAME's agent folder, or nothing for a flat agent",
                  "printed whether or not it exists",
                  'agent --config --json: the agents folder as {"path", "source", "rung"}',
                  "agent NAME --json (with or without --config)",
                  '{"version", "root", "items": {"home", "agents", "config"}}',
                  '"" for no folder', '"builtin"', '"refused"',
                  '"file", the agent file (set for a flat or built-in agent too)',
                  'a refused NAME has "path" and "file" both ""',
                  "--config on any leaf but agent",
                  "agent NAME --config also exits 2 when the launcher refuses NAME",
                  "agent NAME --json does not",
                  *agents.RUNGS):
        assert words in out, words


DOC = ROOT / "docs" / "tools-state-paths.md"
#: design.md's Summary table: each surface, as the page writes it.
AGENT_SURFACES = ("`session path agent`", "`session path agent NAME`",
                  "`session path agent --config`", "`session path agent NAME --config`",
                  "`session path agent NAME --json`", "`session path agent --config --json`")


def _slug(heading: str) -> str:
    """A heading's anchor, as the docs' renderer makes it."""
    kept = "".join(c for c in heading.lower() if c.isalnum() or c in " -")
    return kept.replace(" ", "-")


def test_the_page_documents_each_agent_surface_with_its_exit():
    text = DOC.read_text()
    rows = [line for line in text.splitlines() if line.startswith("| `session path agent")]
    assert [r.split(" | ")[0][2:] for r in rows] == list(AGENT_SURFACES)
    for row in rows:
        assert row.rstrip(" |").split(" | ")[-1].startswith(("0", "2")), row
    for words in ("paths.agent_report", "start.agents", "refused", "`rung`",
                  '`items.config.path` is `""` when NAME has no folder',
                  "Test it before joining `avatar.jpg`"):
        assert words in text, words


def test_the_agent_anchors_resolve():
    """session.md and configuration.md link the page's agent section."""
    headings = {_slug(line.lstrip("#").strip()) for line in DOC.read_text().splitlines()
                if line.startswith("#")}
    anchors = {"an-agents-home-and-folder-session-path-agent",
               "the-agent-contract-agent-name---json"}
    assert anchors <= headings
    for doc in ("session.md", "configuration.md"):
        text = (ROOT / "docs" / doc).read_text()
        assert "tools-state-paths.md#an-agents-home-and-folder-session-path-agent" in text
        assert "session path agent" in text or "agent NAME" in text


def _newest_changelog_entries(text: str) -> str:
    """The newest section of CHANGELOG.md that holds entries: [Unreleased]
    while it has any, else the release just below it. The release stamp
    (scripts/release.py) puts a fresh, empty [Unreleased] above the stamped
    section, so a release tip carries the entries one section down."""
    sections = text.split("\n## [")[1:]
    assert sections and sections[0].startswith("Unreleased]"), "no [Unreleased] on top"
    for section in sections:
        body = section.partition("\n")[2]
        if body.strip():
            return body
    return ""


def test_the_changelog_names_it_in_its_newest_entries():
    newest = _newest_changelog_entries((ROOT / "CHANGELOG.md").read_text())
    assert "`session path agent NAME`" in newest
    assert "paths.agent_report" in newest


def test_the_newest_entries_survive_the_release_stamp():
    """A stamped CHANGELOG (empty [Unreleased], then [x.y.z]) and an
    unstamped one give the same entries; an empty [Unreleased] never wins."""
    head = "# Changelog\n\nPreamble.\n\n## [Unreleased]\n\n"
    entries = "### Added\n\n- `session path agent NAME`\n"
    older = "\n## [0.0.1] - 2030-06-09\n\n- older\n"
    unstamped = head + entries + older
    stamped = head + "## [0.0.2] - 2030-12-08\n\n" + entries + older
    assert _newest_changelog_entries(unstamped).strip() == entries.strip()
    assert _newest_changelog_entries(stamped).strip() == entries.strip()
    assert "older" not in _newest_changelog_entries(stamped)


def test_completion_offers_the_leaves():
    action = next(a for a in paths.build_parser()._actions if a.dest == "leaf")
    assert getattr(action, verbs.COMPLETION_CHOICES) is paths.LEAVES
    assert action.choices is None
    assert "path" in completion.script("bash")
