"""`session path` (ST-266 do-002, READY A): a view onto the settings resolvers.

Each leaf prints the path `settings.leaf_path` (or, for log,
`settings.log_dir`) answers; NAME joins sessions and seats; `--json` gives
every leaf's path and source, and stranded only when the resolver says so.
The source strings are pinned: sessionmail's vmailbox-gen parses them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessiontools import cli, completion, paths, settings, verbs

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
    fresh = "fresh-new" if leaf in ("state", "sound") else "fresh-old"   # wave 3 (do-003)
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
    assert settings.session_dir(SID, env) == tmp_path / ".session/sessions" / SID


def test_name_is_a_literal_join_never_a_short_id_lookup(capsys, tmp_path):
    env = env_for(tmp_path)
    (tmp_path / ".session/sessions" / SID).mkdir(parents=True)
    code, out, _ = run(capsys, "sessions", SID[:8], env=env)
    assert (code, out) == (0, f"{tmp_path / '.session/sessions' / SID[:8]}\n")


@pytest.mark.parametrize("leaf", ["state", "agent", "backups", "sound", "log"])
def test_a_leaf_that_takes_no_name_refuses_one(capsys, tmp_path, leaf):
    code, out, err = run(capsys, leaf, "x", env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert "takes no NAME" in err and "usage: session path" in err


@pytest.mark.parametrize("name", [".", "..", "a/b", "../x", ""])
def test_a_name_that_would_leave_the_leaf_is_refused(capsys, tmp_path, name):
    code, out, err = run(capsys, "sessions", name, env=env_for(tmp_path))
    assert (code, out) == (2, "")
    assert "is not a session id" in err


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
    assert json.loads(out) == {"path": str(settings.session_dir(SID, env)), "source": "fresh-old"}


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
        "path": str(tmp_path / ".session/agent"), "source": "fresh-old"}


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


def test_completion_offers_the_leaves():
    action = next(a for a in paths.build_parser()._actions if a.dest == "leaf")
    assert getattr(action, verbs.COMPLETION_CHOICES) is paths.LEAVES
    assert action.choices is None
    assert "path" in completion.script("bash")
