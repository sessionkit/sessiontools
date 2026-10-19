"""The settings cascade (openspec/changes/session-settings): the scenarios
of `specs/settings/spec.md` and `specs/tree/spec.md`, each against a tree
built under a private HOME."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from git_support import hermetic_env
from settings_support import host

from sessiontools import settings
from sessiontools.cli import main

SID = "11111111-2222-3333-4444-555555555555"


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # The conftest walls SESSIONS_DIR to a dir with no sessions; the reader
    # honours it (as the launcher does), so point it at the tree built here.
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / ".session" / "sessions"))
    return tmp_path


def seat(tree: Path, name: str) -> Path:
    return tree / ".session" / "seats" / name / "settings"


def session(tree: Path) -> Path:
    return tree / ".session" / "sessions" / SID / "settings"


def kit_short(tool: str) -> str:
    """TOOL's host file as the report spells it, `~/...`, derived from
    settings.host_dir (never a literal shape)."""
    fake = Path("/home-of-test")
    return f"~/{(host(fake, tool) / f'{tool}.json').relative_to(fake)}"


def agent_md(tree: Path, name: str, front: str) -> Path:
    return w(tree / ".session" / "agent" / "config" / f"{name}.md",
             f"---\n{front}\n---\n# {name}\n")


# --- specs/settings ------------------------------------------------------------

def test_three_layers_and_the_agent_md_and_no_other_file(tree):
    w(host(tree, "mail") / "mail.template.json", {"hooks": False})
    w(host(tree, "mail") / "mail.json", {"hooks": True})
    agent_md(tree, "alice", "settings:\n  mail:\n    page_age_seconds: 30")
    w(seat(tree, "alice") / "mail.json", {"remind_minutes": 5})
    w(session(tree) / "mail.json", {"idle_seconds": 1})
    w(tree / ".sessionmail" / "sessions" / SID / "rules", "wake to:$me\n")  # an OLD path
    res = settings.resolve("mail", SID, agent="alice", seat="alice")
    assert [s.layer for s in res.sources] == ["template", "host", "agent", "seat", "session"]
    assert res.values == {"hooks": True, "page_age_seconds": 30, "remind_minutes": 5, "idle_seconds": 1}
    assert not any(".sessionmail" in str(s.path) for s in res.sources)


def test_seat_voice_over_host_voice_names_the_seat_file(tree):
    w(host(tree, "mouth") / "mouth.json", {"voice": "af_sky"})
    f = w(seat(tree, "carol") / "mouth.json", {"voice": "en/narrator"})
    res = settings.resolve("mouth", SID, seat="carol")
    assert res.values["voice"] == "en/narrator"
    assert res.origins["voice"] == str(f)


def test_a_host_lock_holds_against_a_session(tree):
    h = w(host(tree, "mail") / "mail.json", {"hooks": True, "_locked": ["hooks"]})
    w(session(tree) / "mail.json", {"hooks": False, "idle_seconds": 2})
    res = settings.resolve("mail", SID)
    assert res.values == {"hooks": True, "idle_seconds": 2}
    assert res.origins["hooks"] == str(h)
    assert res.locked == {"hooks": str(h)}


def test_a_lock_on_one_map_entry_holds_and_the_rest_merge(tree):
    h = w(host(tree, "mail") / "mail.json",
          {"rules": {"ride-delta": "ride from:*@delta.chat"}, "_locked": ["rules.ride-delta"]})
    w(session(tree) / "mail.json", {"rules": {"ride-delta": False, "mine": "wake to:$me"}})
    res = settings.resolve("mail", SID)
    assert res.values["rules"] == {"ride-delta": "ride from:*@delta.chat", "mine": "wake to:$me"}
    assert res.origins["rules.ride-delta"] == str(h)


def test_dotted_and_nested_say_the_same_thing(tree):
    agent_md(tree, "a", "settings:\n  mouth.voice: af_sky")
    agent_md(tree, "b", "settings:\n  mouth:\n    voice: af_sky")
    ra = settings.resolve("mouth", SID, agent="a")
    rb = settings.resolve("mouth", SID, agent="b")
    assert ra.values == rb.values == {"voice": "af_sky"}


def test_both_forms_at_once_is_a_refusal_naming_the_key(tree):
    agent_md(tree, "c", "settings:\n  mouth.voice: a\n  mouth:\n    voice: b")
    with pytest.raises(settings.SettingsError) as e:
        settings.resolve("mouth", SID, agent="c")
    assert "mouth.voice" in str(e.value)


def test_a_dot_inside_a_map_key_stays_whole(tree):
    agent_md(tree, "d", "settings:\n  mail:\n    subscriptions:\n      cutroom+cut-634.foreman: true")
    res = settings.resolve("mail", SID, agent="d")
    assert res.values == {"subscriptions": {"cutroom+cut-634.foreman": True}}


def test_a_subscription_removed_stays_removed_and_the_other_stays(tree):
    w(session(tree) / "mail.d" / "50-env.json",
      {"subscriptions": {"heard+airpods": True, "heard+macbook": True}})
    w(session(tree) / "mail.d" / "70-live.json", {"subscriptions": {"heard+airpods": False}})
    res = settings.resolve("mail", SID)
    assert res.values["subscriptions"] == {"heard+airpods": False, "heard+macbook": True}


def test_three_writers_one_tool_each_key_has_its_file(tree):
    a = w(session(tree) / "mail.d" / "50-env.json", {"address": "alice", "subscriptions": {"x": True}})
    b = w(session(tree) / "mail.d" / "70-live.json", {"subscriptions": {"y": True}})
    c = w(session(tree) / "mail.d" / "90-local.json", {"address": "alice+quiet"})
    res = settings.resolve("mail", SID)
    assert res.values == {"address": "alice+quiet", "subscriptions": {"x": True, "y": True}}
    assert res.origins == {"address": str(c), "subscriptions.x": str(a), "subscriptions.y": str(b)}


def test_a_scalar_replaces_a_map_and_a_map_replaces_a_scalar(tree):
    w(host(tree, "view") / "view.json", {"a": {"b": 1}, "c": 1})
    w(session(tree) / "view.json", {"a": 2, "c": {"d": 3}})
    res = settings.resolve("view", SID)
    assert res.values == {"a": 2, "c": {"d": 3}}


def test_a_bad_key_is_refused_naming_the_file(tree):
    pytest.importorskip("jsonschema")
    w(host(tree, "mail") / "mail.schema.json",
      {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
       "properties": {"page_age_seconds": {"type": "integer"}}})
    bad = w(seat(tree, "alice") / "mail.json", {"page_age_seconds": "soon"})
    res = settings.resolve("mail", SID, seat="alice")
    assert len(res.problems) == 1
    assert "page_age_seconds" in res.problems[0] and str(bad) in res.problems[0]


def test_before_mail_moves_it_is_not_moved(tree, capsys):
    w(tree / ".sessionmail" / "sessions" / SID / "rules", "wake to:$me\n")
    res = settings.resolve("mail", SID)
    assert not res.moved
    assert main(["settings", SID, "mail"]) == 0
    assert "mail  not moved" in capsys.readouterr().out


def test_the_report_names_value_and_origin(tree, capsys):
    w(host(tree, "mouth") / "mouth.json", {"voice": "af_sky"})
    w(seat(tree, "carol") / "mouth.json", {"voice": "en/narrator"})
    w(session(tree) / "session.json", {"agent": "carol", "seat": "carol"})
    assert main(["settings", SID, "mouth"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "mouth  (host, seat)"
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("voice"))
    assert '"en/narrator"' in line and "~/.session/seats/carol/settings/mouth.json" in line


def test_identity_comes_from_the_session_layer(tree):
    assert settings.identity(SID) == (None, None)
    w(session(tree) / "session.json", {"agent": "alice", "seat": "alice"})
    assert settings.identity(SID) == ("alice", "alice")


def test_all_tools_when_none_named(tree, capsys):
    w(host(tree, "mouth") / "mouth.json", {"voice": "af_sky"})
    w(session(tree) / "mail.d" / "50-env.json", {"address": "x"})
    assert settings.tools(SID) == ["mail", "mouth"]
    assert main(["settings", SID]) == 0
    out = capsys.readouterr().out
    assert out.index("mail  (") < out.index("mouth  (")


def test_a_schema_problem_exits_two(tree, capsys):
    pytest.importorskip("jsonschema")
    w(host(tree, "mail") / "mail.schema.json", {"type": "object", "properties": {"hooks": {"type": "boolean"}}})
    w(host(tree, "mail") / "mail.json", {"hooks": "yes"})
    assert main(["settings", SID, "mail"]) == 2
    assert "REFUSED hooks:" in capsys.readouterr().out


def test_importing_the_module_reaches_no_yaml_and_no_jsonschema():
    probe = ("import sys; import sessiontools.settings; "
             "print('yaml' in sys.modules, 'jsonschema' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["False", "False"]


# --- review findings on the merge ---------------------------------------------

def test_a_lock_on_a_map_key_with_a_dot_holds_and_the_report_prints(tree, capsys):
    h = w(host(tree, "mail") / "mail.json",
          {"subscriptions": {"super.sessionmail": True}, "_locked": ["subscriptions.super.sessionmail"]})
    w(session(tree) / "mail.json", {"subscriptions": {"super.sessionmail": False, "cutroom+cut-634.foreman": True}})
    res = settings.resolve("mail", SID)
    assert res.values["subscriptions"] == {"super.sessionmail": True, "cutroom+cut-634.foreman": True}
    assert res.origins['subscriptions."super.sessionmail"'] == str(h)
    assert main(["settings", SID, "mail"]) == 0
    out = capsys.readouterr().out
    assert 'subscriptions."super.sessionmail"' in out and "[locked by" in out
    assert 'subscriptions."cutroom+cut-634.foreman"' in out


def test_an_empty_map_below_adds_nothing_and_clears_nothing(tree):
    w(host(tree, "mail") / "mail.json", {"subscriptions": {"heard+airpods": True}})
    w(session(tree) / "mail.json", {"subscriptions": {}})
    res = settings.resolve("mail", SID)
    assert res.values["subscriptions"] == {"heard+airpods": True}


def test_mail_subscriptions_are_listed_per_owner_never_merged(tree, capsys):
    """Routing reads each layer's subscriptions alone (the paper's Open 10):
    a session's `false` does not stop the seat's copy, so the report must not
    print one merged `false`."""
    w(seat(tree, "voicemode+super") / "mail.json",
      {"subscriptions": {"heard+airpods": True}})
    w(session(tree) / "mail.json", {"subscriptions": {"heard+airpods": False}, "hooks": True})
    w(session(tree) / "session.json", {"seat": "voicemode+super"})
    assert main(["settings", SID, "mail"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(ln.strip().startswith("hooks") for ln in out)        # merged as before
    head = next(i for i, ln in enumerate(out) if "per owner" in ln)
    rows = [ln for ln in out[head + 1:] if "subscriptions.heard+airpods" in ln]
    assert len(rows) == 2
    assert "true" in rows[0] and "seats/voicemode+super" in rows[0]
    assert "false" in rows[1] and f"sessions/{SID}" in rows[1]
    assert not any(ln.strip().startswith("subscriptions.heard+airpods")
                   for ln in out[:head])                             # never merged


# --- ensure_private_dir (4.6 / 3.3: launch/ and mail/) -----------------------

def test_ensure_private_dir_is_0700_with_a_gitignore_of_star(tmp_path):
    import os
    import stat
    made = settings.ensure_private_dir(tmp_path / "mail")
    assert made == tmp_path / "mail"
    assert stat.S_IMODE(os.stat(made).st_mode) == 0o700
    assert (made / ".gitignore").read_text() == "*\n"
    assert stat.S_IMODE(os.stat(made / ".gitignore").st_mode) == 0o600


def test_ensure_private_dir_leaves_an_existing_one_alone(tmp_path):
    first = settings.ensure_private_dir(tmp_path / "mail")
    (first / "pager-note.key").write_text("k")
    (first / ".gitignore").write_text("*\n!keep\n")
    again = settings.ensure_private_dir(tmp_path / "mail")
    assert again == first
    assert (again / "pager-note.key").read_text() == "k"
    assert (again / ".gitignore").read_text() == "*\n!keep\n"     # not rewritten


def test_ensure_private_dir_never_makes_the_parent(tmp_path):
    with pytest.raises(FileNotFoundError):
        settings.ensure_private_dir(tmp_path / "no-session" / "mail")


def test_a_private_dir_inside_a_repo_is_ignored_by_git(tmp_path):
    repo = tmp_path / "sessions"
    repo.mkdir()
    env = hermetic_env()     # ST-161: a global or XDG ignore would hide session-id
    assert subprocess.run(["git", "init", "-q"], cwd=repo, check=False, env=env).returncode == 0
    session = repo / "sid-1"
    session.mkdir()
    settings.ensure_private_dir(session / "mail")
    (session / "mail" / "pager-note.key").write_text("secret")
    (session / "session-id").write_text("sid-1")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=False, env=env)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, env=env,
                            capture_output=True, text=True, check=False).stdout.split()
    assert staged == ["sid-1/session-id"], staged


# --- SESSIONS_DIR: the reader and the launcher name the same dir --

def test_session_dir_honours_sessions_dir_like_the_launcher(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "SESSIONS_DIR": str(tmp_path / "crews")}
    assert settings.sessions_root(env) == tmp_path / "crews"
    assert settings.session_dir("sid-1", env) == tmp_path / "crews" / "sid-1"
    from sessiontools.start.agents import Agent, sessions_root
    agent = Agent(name="x", file="", directory="", home="")
    assert sessions_root(agent, environ=env) == str(settings.sessions_root(env))


def test_session_dir_defaults_under_home_dot_session(tmp_path):
    env = {"HOME": str(tmp_path / "home")}
    assert settings.session_dir("sid-1", env) == tmp_path / "home" / ".session" / "sessions" / "sid-1"
    assert settings.session_dir("sid-1", {**env, "SESSIONS_DIR": ""}) == \
        tmp_path / "home" / ".session" / "sessions" / "sid-1"      # empty = unset


# --- the agent layer's file (4.2; Open 12 RULED) -------------------------------

def test_an_agent_with_extras_is_a_folder_and_its_md_is_read_there(tree):
    folder = tree / ".session" / "agent" / "config" / "alice"
    w(folder / "avatar.jpg", "x")
    md = w(folder / "alice.md", "---\nsettings:\n  session:\n    home: true\n---\n# alice\n")
    assert settings.agent_config("alice") == md
    res = settings.resolve("session", SID, agent="alice")
    assert res.origins["home"] == str(md)


def test_an_agent_without_extras_is_the_flat_file(tree):
    md = agent_md(tree, "bnc", "settings:\n  session:\n    home: true")
    assert settings.agent_config("bnc") == md


# --- Open 15: the agent layer from the file the launch used (RULED) ------------

def test_the_reader_takes_the_agent_layer_from_the_recorded_file(tree):
    agent_md(tree, "scout", "settings:\n  session:\n    home: false")          # not launched
    plugin = w(tree / "plugin" / "agents" / "scout.md",
               "---\nsettings:\n  session:\n    home: true\n---\n# scout\n")
    w(session(tree) / "session.json", {"agent": "scout", "agent_file": str(plugin)})
    res = settings.resolve("session", SID, agent="scout")
    assert res.values["home"] is True
    assert res.origins["home"] == str(plugin)
    assert [s.layer for s in res.sources if str(s.path) == str(plugin)] == ["agent"]


def test_a_gone_recorded_file_falls_back_and_says_so(tree):
    md = agent_md(tree, "scout", "settings:\n  session:\n    home: true")
    gone = tree / "plugin" / "scout.md"
    w(session(tree) / "session.json", {"agent": "scout", "agent_file": str(gone)})
    res = settings.resolve("session", SID, agent="scout")
    assert res.origins["home"] == str(md)
    assert any(str(gone) in n and "gone" in n for n in res.notes), res.notes
    assert res.problems == []                    # said, never refused
    assert f"NOTE agent_file {gone} is gone" in settings.report(res)


def test_without_a_record_the_reader_takes_agent_config(tree):
    md = agent_md(tree, "scout", "settings:\n  session:\n    home: true")
    w(session(tree) / "session.json", {"agent": "scout"})
    assert settings.resolve("session", SID, agent="scout").origins["home"] == str(md)


def test_a_seat_still_beats_the_recorded_agent_file(tree):
    plugin = w(tree / "plugin" / "scout.md",
               "---\nsettings:\n  session:\n    home: true\n---\n# scout\n")
    w(session(tree) / "session.json", {"agent": "scout", "agent_file": str(plugin)})
    w(seat(tree, "s") / "session.json", {"home": False})
    res = settings.resolve("session", SID, agent="scout", seat="s")
    assert res.values["home"] is False


# --- homes (session-settings 4.3; tree spec "Homes") ----------------------------

def test_home_true_in_the_agent_md_places_it_under_agent_home(tree):
    md = agent_md(tree, "alice", "settings:\n  session:\n    home: true")
    res = settings.resolve("session", SID, agent="alice", seat="alice")
    h = settings.session_home(res, SID, "alice", "alice")
    assert h == settings.Home(str(tree / ".session" / "agent" / "home" / "alice"), "agent", str(md))


def test_home_true_in_the_seat_places_it_under_the_seat(tree):
    f = w(seat(tree, "voicemode+super") / "session.json", {"home": True})
    res = settings.resolve("session", SID, seat="voicemode+super")
    h = settings.session_home(res, SID, None, "voicemode+super")
    assert h == settings.Home(str(tree / ".session" / "seats" / "voicemode+super" / "home"),
                              "seat", str(f))


def test_home_true_in_the_session_layer_places_it_under_the_session(tree):
    f = w(session(tree) / "session.json", {"home": True})
    res = settings.resolve("session", SID)
    h = settings.session_home(res, SID, None, None)
    assert h == settings.Home(str(tree / ".session" / "sessions" / SID / "home"), "session", str(f))


def test_the_lowest_layer_that_sets_home_wins_so_false_below_true_is_no_home(tree):
    agent_md(tree, "alice", "settings:\n  session:\n    home: true")
    w(seat(tree, "alice") / "session.json", {"home": False})
    res = settings.resolve("session", SID, agent="alice", seat="alice")
    assert settings.session_home(res, SID, "alice", "alice") is None


def test_home_dir_overrides_the_place_and_implies_true(tree):
    f = w(seat(tree, "alice") / "session.json", {"home_dir": "~/.alice"})
    res = settings.resolve("session", SID, seat="alice")
    assert settings.session_home(res, SID, None, "alice") == settings.Home(
        str(tree / ".alice"), "dir", str(f))


# Open 13, RULED: between `home` and
# `home_dir`, the lowest file that sets either key wins.

def test_a_seats_home_false_turns_off_the_agents_home_dir(tree):
    agent_md(tree, "alice", "settings:\n  session:\n    home_dir: ~/somewhere")
    w(seat(tree, "alice") / "session.json", {"home": False})
    res = settings.resolve("session", SID, agent="alice", seat="alice")
    assert settings.session_home(res, SID, "alice", "alice") is None


def test_a_lower_home_dir_turns_a_home_back_on(tree):
    agent_md(tree, "alice", "settings:\n  session:\n    home: false")
    f = w(seat(tree, "alice") / "session.json", {"home_dir": "~/somewhere"})
    res = settings.resolve("session", SID, agent="alice", seat="alice")
    assert settings.session_home(res, SID, "alice", "alice") == settings.Home(
        str(tree / "somewhere"), "dir", str(f))


def test_a_lower_home_true_is_only_on_and_the_place_stays_the_home_dir(tree):
    # RULED: if it just says on, then it stays.
    md = agent_md(tree, "alice", "settings:\n  session:\n    home_dir: ~/somewhere")
    w(seat(tree, "alice") / "session.json", {"home": True})
    res = settings.resolve("session", SID, agent="alice", seat="alice")
    assert settings.session_home(res, SID, "alice", "alice") == settings.Home(
        str(tree / "somewhere"), "dir", str(md))


def test_a_home_false_above_a_home_dir_is_overridden(tree):
    agent_md(tree, "alice", "settings:\n  session:\n    home: false")
    w(session(tree) / "session.json", {"home": True})
    f = w(seat(tree, "alice") / "session.json", {"home_dir": "~/somewhere"})
    res = settings.resolve("session", SID, agent="alice", seat="alice")
    assert settings.session_home(res, SID, "alice", "alice") == settings.Home(
        str(tree / "somewhere"), "dir", str(f))


def test_home_true_and_home_dir_in_one_file_is_the_dir(tree):
    f = w(seat(tree, "alice") / "session.json", {"home": True, "home_dir": "~/somewhere"})
    res = settings.resolve("session", SID, seat="alice")
    assert settings.session_home(res, SID, None, "alice") == settings.Home(
        str(tree / "somewhere"), "dir", str(f))


def test_home_false_and_home_dir_in_one_file_is_refused_naming_it(tree):
    f = w(seat(tree, "alice") / "session.json", {"home": False, "home_dir": "~/somewhere"})
    res = settings.resolve("session", SID, seat="alice")
    with pytest.raises(settings.SettingsError, match=str(f)):
        settings.session_home(res, SID, None, "alice")


def test_a_later_drop_in_of_one_layer_wins_between_the_keys(tree):
    d = seat(tree, "alice") / "session.d"
    w(d / "10-dir.json", {"home_dir": "~/somewhere"})
    w(d / "20-off.json", {"home": False})
    res = settings.resolve("session", SID, seat="alice")
    assert settings.session_home(res, SID, None, "alice") is None


def test_no_home_by_default(tree):
    res = settings.resolve("session", SID, agent="worker", seat="x")
    assert settings.session_home(res, SID, "worker", "x") is None


def test_a_session_place_with_no_id_yet_is_the_root_to_finish_in_the_script(tree):
    w(host(tree, "session") / "session.json", {"home": True})
    res = settings.resolve("session", None)            # no session layer without an id
    assert [s.layer for s in res.sources] == ["host"]
    h = settings.session_home(res, None, None, None)
    assert h.per_session and h.place == str(tree / ".session" / "sessions") and h.layer == "host"


# --- the environment at birth (session-settings 4.4; Open 14 RULED) -------------

MAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "subscriptions": {"type": "object", "additionalProperties": {"type": "boolean"},
                          "x-env": "SESSIONMAIL_SUBSCRIBE"},
        "hooks": {"type": "boolean", "x-env": "SESSIONMAIL_HOOKS"},
        "page": {"type": "object", "properties": {
            "age": {"type": "integer", "x-env": "SESSIONMAIL_PAGE_AGE_SECONDS"}}},
        "address": {"type": "string"},
    },
}


def _birth(tree, environ, now="2030-12-08T12:34:56+00:00"):
    w(host(tree, "mail") / "mail.schema.json", MAIL_SCHEMA)
    return settings.write_env_at_birth(session(tree).parent, environ, now=now)


def _log_lines(tree):
    f = session(tree).parent / "log" / "settings.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_a_schema_names_its_variable_and_the_birth_writes_it(tree):
    _birth(tree, {"SESSIONMAIL_SUBSCRIBE": "heard+airpods, heard+macbook"})
    f = session(tree) / "mail.d" / "50-env.json"
    assert json.loads(f.read_text()) == {
        "subscriptions": {"heard+airpods": True, "heard+macbook": True}}
    (entry,) = _log_lines(tree)
    assert (entry["event"], entry["variable"], entry["key"], entry["tool"]) == (
        "env-at-birth", "SESSIONMAIL_SUBSCRIBE", "subscriptions", "mail")
    assert "heard+airpods" not in json.dumps(entry)          # the name, never the value


def test_a_space_separated_list_reads_as_tm_kick_writes_it(tree):
    """`tm kick` writes SESSIONMAIL_SUBSCRIBE="super.<p> <p>", space-separated,
    which is how sessionmail reads it; a comma-only split made one bogus box
    (the 3.4 blocker, 2030-12-08)."""
    _birth(tree, {"SESSIONMAIL_SUBSCRIBE": "super.p p"})
    assert json.loads((session(tree) / "mail.d" / "50-env.json").read_text()) == {
        "subscriptions": {"super.p": True, "p": True}}


def test_commas_and_spaces_mix(tree):
    _birth(tree, {"SESSIONMAIL_SUBSCRIBE": " a,b  c ,\td, "})
    assert json.loads((session(tree) / "mail.d" / "50-env.json").read_text()) == {
        "subscriptions": {"a": True, "b": True, "c": True, "d": True}}


def test_the_birth_file_is_what_the_reader_merges(tree):
    _birth(tree, {"SESSIONMAIL_SUBSCRIBE": "heard+airpods"})
    res = settings.resolve("mail", SID)
    assert res.values["subscriptions"] == {"heard+airpods": True}
    assert res.origins["subscriptions.heard+airpods"].endswith("mail.d/50-env.json")


def test_an_unnamed_variable_stays_environment(tree):
    assert _birth(tree, {"NOTIFY_CTX_WARN": "350000", "SESSIONMAIL_ADDRESS": "x"}) == []
    assert not (session(tree) / "mail.d").exists()
    assert _log_lines(tree) == []


def test_each_key_type_reads_its_string(tree):
    _birth(tree, {"SESSIONMAIL_HOOKS": "yes", "SESSIONMAIL_PAGE_AGE_SECONDS": "60"})
    assert json.loads((session(tree) / "mail.d" / "50-env.json").read_text()) == {
        "hooks": True, "page": {"age": 60}}


def test_a_value_that_cannot_be_read_is_refused_and_the_rest_written(tree):
    entries = _birth(tree, {"SESSIONMAIL_PAGE_AGE_SECONDS": "soon",
                            "SESSIONMAIL_SUBSCRIBE": "heard+airpods"})
    assert json.loads((session(tree) / "mail.d" / "50-env.json").read_text()) == {
        "subscriptions": {"heard+airpods": True}}
    refused = [e for e in entries if e["event"] == "env-refused"]
    assert [e["variable"] for e in refused] == ["SESSIONMAIL_PAGE_AGE_SECONDS"]


def test_an_empty_variable_is_logged_and_not_read(tree):
    entries = _birth(tree, {"SESSIONMAIL_SUBSCRIBE": ""})
    assert [(e["event"], e["variable"]) for e in entries] == [("env-empty", "SESSIONMAIL_SUBSCRIBE")]
    assert not (session(tree) / "mail.d" / "50-env.json").exists()


def test_the_birth_file_is_private(tree):
    _birth(tree, {"SESSIONMAIL_SUBSCRIBE": "heard+airpods"})
    f = session(tree) / "mail.d" / "50-env.json"
    assert f.stat().st_mode & 0o777 == 0o600
    assert f.parent.stat().st_mode & 0o777 == 0o700


def test_a_file_already_there_is_kept_read_once(tree):
    f = w(session(tree) / "mail.d" / "50-env.json", {"subscriptions": {"old": True}})
    entries = _birth(tree, {"SESSIONMAIL_SUBSCRIBE": "heard+airpods"})
    assert json.loads(f.read_text()) == {"subscriptions": {"old": True}}
    assert [e["event"] for e in entries] == ["env-kept"]


def test_no_installed_schema_logs_one_birth_no_schema_line(tree):
    """Case A: no settings tree at all."""
    entries = settings.write_env_at_birth(session(tree).parent,
                                          {"SESSIONMAIL_SUBSCRIBE": "heard+airpods"},
                                          now="2030-12-08T12:34:56+00:00")
    assert len(entries) == 1
    (line,) = _log_lines(tree)
    assert line == entries[0]
    assert line["event"] == "birth-no-schema"
    assert line["schemas"] == []
    assert line["root"] == str(settings.kit_root())
    assert line["ts"] == "2030-12-08T12:34:56+00:00"
    assert not (session(tree) / "mail.d").exists()


def test_a_config_without_a_schema_logs_birth_no_schema(tree):
    """Case B: mail.json only, no schema."""
    w(host(tree, "mail") / "mail.json", {"hooks": True})
    entries = settings.write_env_at_birth(session(tree).parent, {})
    (line,) = _log_lines(tree)
    assert entries == [line]
    assert line["event"] == "birth-no-schema" and line["schemas"] == []


def test_a_schema_with_no_x_env_logs_birth_no_schema(tree):
    """Case C: a schema, but no x-env anywhere."""
    w(host(tree, "mail") / "mail.schema.json",
      {"type": "object", "properties": {"hooks": {"type": "boolean"}}})
    entries = settings.write_env_at_birth(session(tree).parent,
                                          {"SESSIONMAIL_HOOKS": "yes"})
    (line,) = _log_lines(tree)
    assert entries == [line]
    assert line["event"] == "birth-no-schema" and line["schemas"] == ["mail"]
    assert not (session(tree) / "mail.d").exists()


def test_birth_main_on_no_schema_exits_0_silent_and_logs(tree):
    sdir = session(tree).parent
    env = {"HOME": str(tree), "PATH": os.environ.get("PATH", "")}
    done = subprocess.run([sys.executable, "-I", "-c", settings.BIRTH_ENTRY, "--dir", str(sdir)],
                          env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0
    assert done.stderr == ""
    (line,) = _log_lines(tree)
    assert line["event"] == "birth-no-schema"


def test_birth_main_runs_as_the_launch_script_runs_it(tree):
    w(host(tree, "mail") / "mail.schema.json", MAIL_SCHEMA)
    sdir = session(tree).parent
    env = {"HOME": str(tree), "PATH": os.environ.get("PATH", ""),
           "SESSIONMAIL_SUBSCRIBE": "heard+carcar"}
    done = subprocess.run([sys.executable, "-I", "-c", settings.BIRTH_ENTRY, "--dir", str(sdir)],
                          env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert json.loads((session(tree) / "mail.d" / "50-env.json").read_text()) == {
        "subscriptions": {"heard+carcar": True}}


def test_birth_main_with_bad_arguments_writes_nothing_and_exits_0(tree, capsys):
    assert settings.birth_main([], environ={"HOME": str(tree)}) == 0
    assert "usage" in capsys.readouterr().err


# --- the tool list in the help (ST-138 G4) ------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_settings_help_names_every_tool_sounds_included(capsys):
    """`session settings --help` named mail, session, ears and mouth, but not
    `sounds`, the busy sound's tool (ST-66). The list is settings.TOOLS now."""
    with pytest.raises(SystemExit) as exit_:
        main(["settings", "--help"])
    assert exit_.value.code == 0
    out = " ".join(capsys.readouterr().out.split())  # argparse wraps the help
    assert f"one tool ({', '.join(settings.TOOLS)})" in out
    for tool in ("mail", "session", "ears", "mouth", "sounds"):
        assert tool in settings.TOOLS


def test_every_tool_this_package_reads_is_in_the_list():
    """Each module constant that names a settings tool, and each schema the
    repo ships under settings/, is in settings.TOOLS: a new tool here cannot
    miss the help again."""
    from sessiontools import busy, operator, snapshot
    from sessiontools.start import claude_settings
    own = {busy.TOOL, snapshot.TOOL, operator.OPERATOR_SETTINGS_TOOL, claude_settings.TOOL}
    shipped = {p.name.removesuffix(".schema.json")
               for p in (ROOT / "settings").glob("*.schema.json")}
    assert shipped, "no schema found under settings/: the pin would prove nothing"
    assert sorted((own | shipped) - set(settings.TOOLS)) == []


# --- the kit's home: one map, one shape, no old path --------------------------
#
# Every tool's template, host layer and schema live in its package's settings
# directory: PACKAGES maps the tool (a command) to the package, package_dir
# spells the directory's shape once, host_dir joins them. Every other test
# derives its paths from host_dir; the pin below is the only literal shape.

def test_the_public_names_import_from_one_place():
    from sessiontools.settings import PACKAGES, host_dir, package_dir
    assert callable(host_dir) and callable(package_dir) and isinstance(PACKAGES, dict)


def test_packages_names_every_tool_and_no_other():
    """The map is pinned to TOOLS both ways: a tool on the cascade has a
    package, and the map names no tool the cascade does not read."""
    assert set(settings.PACKAGES) == set(settings.TOOLS)
    assert len(settings.PACKAGES) == len(settings.TOOLS)


def test_the_map_is_the_ruled_contract():
    assert settings.PACKAGES == {
        "session": "tools", "claude-code": "tools", "sounds": "tools",
        "mail": "mail", "ears": "voice", "mouth": "voice", "view": "view"}


def test_package_dir_is_package_first_under_the_kit_home():
    """The one test that spells the shape: ~/.sessionkit/<package>/settings/."""
    home_dir = Path("/home-of-test")
    assert settings.package_dir("tools", {"HOME": str(home_dir)}) == (
        home_dir / ".sessionkit" / "tools" / "settings")
    assert settings.kit_root({"HOME": str(home_dir)}) == home_dir / ".sessionkit"


def test_host_dir_is_its_packages_directory_and_a_pair_with_no_note():
    env = {"HOME": "/home-of-test"}
    for tool, package in settings.PACKAGES.items():
        assert settings.host_dir(tool, env) == (settings.package_dir(package, env), None)


def test_a_tool_the_map_does_not_name_has_no_directory_and_nothing_is_guessed(tree):
    with pytest.raises(settings.SettingsError, match="no package ships it"):
        settings.host_dir("not-a-tool")
    assert settings.schema_candidates("not-a-tool") == []
    found = settings.host_files("not-a-tool")
    assert found.template is None and found.host == []
    assert found.notes == [("not-a-tool: no package ships it (PACKAGES), so no template "
                            "or host layer is read")]
    res = settings.resolve("not-a-tool", SID)
    assert res.sources == [] and not res.moved
    assert "no package (PACKAGES)" in settings.report(res)


def test_no_two_tools_in_one_package_share_a_file_name():
    """Several tools share a package's directory, so every name a tool owns
    there (host file, drop-in folder, template, both schema names) must be
    its own; claude-code also owns statusline.sh (`session statusline`)."""
    extra = {"claude-code": {"statusline.sh"}}
    by_package: dict[str, dict[str, str]] = {}
    for tool, package in settings.PACKAGES.items():
        names = {f"{tool}.json", f"{tool}.d", f"{tool}.template.json",
                 f"{tool}.schema.json", f".{tool}.schema.json", *extra.get(tool, ())}
        owned = by_package.setdefault(package, {})
        clash = {n: owned[n] for n in names if n in owned}
        assert clash == {}, f"{tool} and {clash} share names in {package}"
        owned.update(dict.fromkeys(names, tool))
    assert {"session", "claude-code", "sounds"} <= set(by_package["tools"].values())


def test_every_tool_reads_and_writes_its_package_directory_and_never_the_old_path(
        tree, monkeypatch, capsys):
    """HOME is a temp dir. Every tool resolves its host directory under
    HOME/.sessionkit/<package>/settings/, its host file and schema are read
    from there, and nothing the reader, the report, the tool list or the
    birth does creates the pre-kit settings tree (settings_root, the
    deprecated name for it) or any directory of that name."""
    monkeypatch.delenv("EARS_CONF", raising=False)
    monkeypatch.delenv("VOICEMODE_MOUTH_SETTINGS", raising=False)
    env = {"HOME": str(tree)}
    for tool, package in settings.PACKAGES.items():
        directory = settings.host_dir(tool)[0]
        assert directory == tree / ".sessionkit" / package / "settings"
        assert directory == settings.package_dir(package, env)
        w(directory / f"{tool}.json", {"marker": tool})
    w(host(tree, "mail") / "mail.schema.json", MAIL_SCHEMA)
    for tool in settings.TOOLS:
        res = settings.resolve(tool, SID)
        assert res.values == {"marker": tool}
        assert [s.path for s in res.sources] == [host(tree, tool) / f"{tool}.json"]
        assert res.notes == []
        settings.report(res)
    assert settings.tools(SID) == sorted(settings.TOOLS)
    assert settings.schema_tools() == ["mail"]
    assert settings.schema_path("mail") == host(tree, "mail") / "mail.schema.json"
    _birth(tree, {"SESSIONMAIL_SUBSCRIBE": "lead+example"})
    assert (session(tree) / "mail.d" / "50-env.json").is_file()
    assert main(["settings", SID]) == 0
    capsys.readouterr()
    old = settings.settings_root()
    assert not old.exists()
    assert [p for p in tree.rglob("*") if p.name == old.name] == []


def test_settings_root_is_deprecated_and_no_kit_reader_uses_it():
    """Kept for callers outside this package, its contract unchanged; the
    package's own code calls it only to say a tool is stranded (the
    predicate and the line it builds), never to read or write."""
    import ast
    home_dir = Path("/home-of-test")
    assert settings.settings_root({"HOME": str(home_dir)}) == home_dir / ".settings"
    assert "DEPRECATED" in (settings.settings_root.__doc__ or "")
    callers = []
    for p in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and getattr(node.func, "id",
                        getattr(node.func, "attr", None)) == "settings_root"):
                    callers.append(f"{p.relative_to(ROOT)}:{fn.name}")
    assert sorted(set(callers)) == ["src/sessiontools/settings.py:stranded",
                                    "src/sessiontools/settings.py:stranded_line"]
    assert len(callers) == 2


# --- where a tool's schema is found (ST-147) ----------------------------------
#
# Since sessionvoice's install writes each schema it ships as the dot name
# `.<tool>.schema.json`, beside the tool's settings file, a schema is looked
# for under both names, in the tool's override directory first.

MOUTH_SCHEMA = {"type": "object", "properties": {
    "voice": {"type": "string", "x-env": "MOUTH_VOICE"}}}
MOUTH_SCHEMA_OLD = {"type": "object", "properties": {
    "voice": {"type": "string", "x-env": "MOUTH_VOICE_OLD"}}}


def _mouth_birth(tree, environ):
    """The birth, reading the override from the same ENVIRON it reads values
    from, as `birth_main` does."""
    env = {"HOME": str(tree), **environ}
    return settings.write_env_at_birth(session(tree).parent, env, env=env,
                                       now="2030-12-08T12:34:56+00:00")


def test_a_dot_named_schema_alone_is_found_listed_read_at_birth_and_checked(tree):
    """T1: `.mouth.schema.json` only, as `ears services install` writes it."""
    dot = w(host(tree, "mouth") / ".mouth.schema.json", MOUTH_SCHEMA)
    assert settings.schema_tools() == ["mouth"]
    assert settings.schema_path("mouth") == dot
    assert [k.variable for k in settings.env_keys("mouth")] == ["MOUTH_VOICE"]
    _mouth_birth(tree, {"MOUTH_VOICE": "af_sky"})
    f = session(tree) / "mouth.d" / "50-env.json"
    assert json.loads(f.read_text()) == {"voice": "af_sky"}
    pytest.importorskip("jsonschema")
    problems = settings.check_schema("mouth", {"voice": 3}, {("voice",): "x.json"})
    assert len(problems) == 1 and problems[0].startswith("voice: 3 is not of type 'string'")
    assert problems[0].endswith("(x.json)")


def test_both_names_the_dot_name_is_read_and_the_other_is_reported(tree):
    """T2: the dot name is the installed copy (the installer writes it); the visible
    one is not read, and is named, never merged."""
    dot = w(host(tree, "mouth") / ".mouth.schema.json", MOUTH_SCHEMA)
    visible = w(host(tree, "mouth") / "mouth.schema.json", MOUTH_SCHEMA_OLD)
    assert settings.find_schema("mouth") == (dot, [visible])
    assert [k.variable for k in settings.env_keys("mouth")] == ["MOUTH_VOICE"]
    w(host(tree, "mouth") / "mouth.json", {"voice": "af_sky"})
    res = settings.resolve("mouth", SID)
    (note,) = res.notes
    assert str(dot) in note and str(visible) in note and "not read" in note
    assert res.problems == []
    entries = _mouth_birth(tree, {"MOUTH_VOICE": "af_sky", "MOUTH_VOICE_OLD": "old"})
    shadowed = [e for e in entries if e["event"] == "schema-shadowed"]
    assert shadowed == [{"ts": "2030-12-08T12:34:56+00:00", "event": "schema-shadowed",
                         "tool": "mouth", "read": str(dot), "not_read": str(visible)}]
    assert shadowed[0] in _log_lines(tree)
    f = session(tree) / "mouth.d" / "50-env.json"
    assert json.loads(f.read_text()) == {"voice": "af_sky"}     # never the old name's


def test_the_override_directory_alone_is_found_listed_and_read_at_birth(tree, monkeypatch):
    """T3: `$VOICEMODE_MOUTH_SETTINGS` set, the schema only beside that file,
    no default directory (host_dir) at all."""
    elsewhere = tree / "voice" / "mouth"
    dot = w(elsewhere / ".mouth.schema.json", MOUTH_SCHEMA)
    monkeypatch.setenv("VOICEMODE_MOUTH_SETTINGS", str(elsewhere / "mouth.json"))
    assert not host(tree, "mouth").exists()
    assert settings.schema_tools() == ["mouth"]
    assert settings.find_schema("mouth") == (dot, [])
    _mouth_birth(tree, {"MOUTH_VOICE": "af_sky",
                        "VOICEMODE_MOUTH_SETTINGS": str(elsewhere / "mouth.json")})
    f = session(tree) / "mouth.d" / "50-env.json"
    assert json.loads(f.read_text()) == {"voice": "af_sky"}


def test_the_override_takes_a_tilde_against_the_env_home(tree):
    dot = w(tree / "voice" / ".ears.schema.json", MOUTH_SCHEMA)
    env = {"HOME": str(tree), "EARS_CONF": "~/voice/ears.json"}
    assert settings.find_schema("ears", env) == (dot, [])


def test_the_override_beats_the_default_and_the_default_is_reported(tree, monkeypatch):
    """T4: a schema beside the override and one in the default directory."""
    elsewhere = tree / "voice" / "mouth"
    over = w(elsewhere / ".mouth.schema.json", MOUTH_SCHEMA)
    old_dot = w(host(tree, "mouth") / ".mouth.schema.json", MOUTH_SCHEMA_OLD)
    old_visible = w(host(tree, "mouth") / "mouth.schema.json", MOUTH_SCHEMA_OLD)
    monkeypatch.setenv("VOICEMODE_MOUTH_SETTINGS", str(elsewhere / "mouth.json"))
    assert settings.find_schema("mouth") == (over, [old_dot, old_visible])
    assert [k.variable for k in settings.env_keys("mouth")] == ["MOUTH_VOICE"]
    # The host file sits beside the override, where the tool reads it (ST-174:
    # in the default directory it is not read, and is a note of its own).
    w(elsewhere / "mouth.json", {"voice": "af_sky"})
    notes = settings.resolve("mouth", SID).notes
    assert len(notes) == 2 and all(str(over) in n for n in notes)
    entries = _mouth_birth(tree, {"VOICEMODE_MOUTH_SETTINGS": str(elsewhere / "mouth.json")})
    assert [e["not_read"] for e in entries if e["event"] == "schema-shadowed"] == [
        str(old_dot), str(old_visible)]


def test_the_override_pointing_at_the_default_dir_reports_nothing_twice(tree, monkeypatch):
    dot = w(host(tree, "mouth") / ".mouth.schema.json", MOUTH_SCHEMA)
    monkeypatch.setenv("VOICEMODE_MOUTH_SETTINGS", str(host(tree, "mouth") / "mouth.json"))
    assert settings.find_schema("mouth") == (dot, [])


def test_one_file_reached_by_both_names_is_one_schema(tree):
    """A visible name symlinked to the dot name is the same file: no note."""
    dot = w(host(tree, "mouth") / ".mouth.schema.json", MOUTH_SCHEMA)
    (host(tree, "mouth") / "mouth.schema.json").symlink_to(dot.name)
    assert settings.find_schema("mouth") == (dot, [])


def test_an_empty_override_or_a_tool_outside_the_table_is_the_default_only(tree, monkeypatch):
    """T5 (a guard): set but empty is unset; a tool not in the table has no
    override, whatever variables are set."""
    elsewhere = tree / "voice"
    w(elsewhere / "mouth.schema.json", MOUTH_SCHEMA)
    w(elsewhere / "mail.schema.json", MAIL_SCHEMA)
    visible = w(host(tree, "mouth") / "mouth.schema.json", MOUTH_SCHEMA_OLD)
    monkeypatch.setenv("VOICEMODE_MOUTH_SETTINGS", "")
    assert settings.find_schema("mouth") == (visible, [])
    assert "mail" not in settings.SETTINGS_FILE_ENV
    monkeypatch.setenv("SESSIONMAIL_SETTINGS", str(elsewhere / "mail.json"))
    assert settings.find_schema("mail") == (None, [])
    assert settings.schema_tools() == ["mouth"]


def test_a_visible_name_alone_reads_as_before(tree):
    """T6 (a guard): the visible name only, as before ST-147."""
    visible = w(host(tree, "mail") / "mail.schema.json", MAIL_SCHEMA)
    assert settings.schema_path("mail") == visible
    assert settings.schema_tools() == ["mail"]
    w(host(tree, "mail") / "mail.json", {"hooks": True})
    assert settings.resolve("mail", SID).notes == []
    entries = settings.write_env_at_birth(session(tree).parent, {"SESSIONMAIL_HOOKS": "yes"})
    assert [e["event"] for e in entries] == ["env-at-birth"]


def test_no_schema_at_all_the_path_is_still_the_visible_default(tree):
    assert settings.schema_path("mouth") == host(tree, "mouth") / "mouth.schema.json"
    assert settings.find_schema("mouth") == (None, [])


def test_the_override_table_names_only_known_tools():
    """T7: a tool joins SETTINGS_FILE_ENV only once it is in TOOLS."""
    assert set(settings.SETTINGS_FILE_ENV) <= set(settings.TOOLS)


# --- the host layer follows the override too (ST-174) -------------------------
#
# Each variable in SETTINGS_FILE_ENV names the tool's own settings file, the
# one the tool reads. With it set, the template and host layer come from
# that file and its directory, never from host_dir(tool).

def test_override_file_of_any_name_is_the_host_layer(tree, monkeypatch):
    """ST-174 T1: a non-default file name, its value and its origin."""
    mine = w(tree / "elsewhere" / "my-ears.json", {"model": "small"})
    monkeypatch.setenv("EARS_CONF", str(mine))
    res = settings.resolve("ears", SID)
    assert res.values == {"model": "small"}
    assert res.origins == {"model": str(mine)}
    assert [(s.layer, s.path) for s in res.sources] == [("host", mine)]
    assert res.moved and res.notes == []


@pytest.mark.parametrize("tool, variable", sorted(settings.SETTINGS_FILE_ENV.items()))
def test_override_beats_the_default_dir_and_a_note_names_both(tree, monkeypatch,
                                                              tool, variable):
    """ST-174 T2: the default file is not a source, and is named."""
    default = w(host(tree, tool) / f"{tool}.json", {"voice": "old", "only_old": 1})
    mine = w(tree / "elsewhere" / f"{tool}.json", {"voice": "new"})
    monkeypatch.setenv(variable, str(mine))
    res = settings.resolve(tool, SID)
    assert res.values == {"voice": "new"}               # never only_old: no stack
    assert default not in [s.path for s in res.sources]
    (note,) = res.notes
    assert str(host(tree, tool)) in note and str(mine) in note and "not read" in note
    assert f"${variable}" in note


def test_an_empty_override_reads_the_default_dir_as_before(tree, monkeypatch):
    """ST-174 T3: set but empty is unset."""
    default = w(host(tree, "ears") / "ears.json", {"model": "base"})
    monkeypatch.setenv("EARS_CONF", "")
    assert settings.settings_file_override("ears") is None
    res = settings.resolve("ears", SID)
    assert res.values == {"model": "base"} and res.origins == {"model": str(default)}
    assert res.notes == []


def test_an_override_with_a_tilde_resolves_against_the_env_home(tree, tmp_path_factory):
    """ST-174 T4: the HOME of the env passed in, not the process's."""
    other = tmp_path_factory.mktemp("other-home")
    mine = w(other / "alt" / "ears.json", {"model": "tiny"})
    env = {"HOME": str(other), "EARS_CONF": "~/alt/ears.json"}
    assert settings.settings_file_override("ears", env) == mine
    res = settings.resolve("ears", None, env=env)
    assert res.origins == {"model": str(mine)}


def test_an_override_naming_no_file_reads_nothing_and_says_so(tree, monkeypatch, capsys):
    """ST-174 T5: no host source, no fallback to the default dir, a note."""
    w(host(tree, "ears") / "ears.json", {"model": "base"})
    gone = tree / "elsewhere" / "ears.json"
    monkeypatch.setenv("EARS_CONF", str(gone))
    res = settings.resolve("ears", SID)
    assert res.sources == [] and res.values == {} and not res.moved
    missing, shadowed = res.notes
    assert str(gone) in missing and "not a file" in missing
    assert str(host(tree, "ears")) in shadowed and "not read" in shadowed
    assert main(["settings", SID, "ears"]) == 0
    out = capsys.readouterr().out
    assert "ears  not moved" in out and "~/elsewhere/ears.json ($EARS_CONF)" in out
    assert kit_short("ears").rsplit("/", 1)[0] not in out.splitlines()[0]
    assert sum(line.startswith("  NOTE ") for line in out.splitlines()) == 2


def test_drop_ins_and_template_beside_the_override_are_read_and_birth_agrees(tree, monkeypatch):
    """ST-174 T6: the cascade's one-layer shape beside the override; birth
    reads its schema from there and its values merge over that host file."""
    elsewhere = tree / "voice" / "mouth"
    template = w(elsewhere / "mouth.template.json", {"voice": "t", "speed": 1})
    mine = w(elsewhere / "my-mouth.json", {"voice": "host"})
    drop = w(elsewhere / "mouth.d" / "20-x.json", {"speed": 2})
    w(elsewhere / "mouth.d" / "10-a.json", {"pitch": 3})
    w(elsewhere / "mouth.json", {"voice": "not-the-named-file"})
    w(elsewhere / ".mouth.schema.json", MOUTH_SCHEMA)
    w(host(tree, "mouth") / "mouth.json", {"voice": "default-dir"})
    environ = {"VOICEMODE_MOUTH_SETTINGS": str(mine), "MOUTH_VOICE": "af_sky"}
    monkeypatch.setenv("VOICEMODE_MOUTH_SETTINGS", str(mine))
    found = settings.host_files("mouth")
    assert found.template == template
    assert found.host == [mine, elsewhere / "mouth.d" / "10-a.json", drop]
    before = settings.resolve("mouth", SID)
    assert before.values == {"voice": "host", "speed": 2, "pitch": 3}
    _mouth_birth(tree, environ)
    after = settings.resolve("mouth", SID)
    assert after.values == {"voice": "af_sky", "speed": 2, "pitch": 3}
    assert [s.layer for s in after.sources] == ["template", "host", "host", "host", "session"]
    assert str(host(tree, "mouth")) not in json.dumps([str(s.path) for s in after.sources])


def test_tools_lists_an_override_tool_and_drops_one_with_nothing_there(tree, monkeypatch):
    """ST-174 T7: listed from the override file alone; a default dir that is
    not read lists nothing."""
    mine = w(tree / "elsewhere" / "my-ears.json", {"model": "small"})
    monkeypatch.setenv("EARS_CONF", str(mine))
    assert "ears" in settings.tools(SID)
    w(host(tree, "mouth") / "mouth.json", {"voice": "x"})
    assert "mouth" in settings.tools(SID)
    monkeypatch.setenv("VOICEMODE_MOUTH_SETTINGS", str(tree / "nowhere" / "mouth.json"))
    assert "mouth" not in settings.tools(SID)


def test_override_pointing_into_the_default_dir_is_one_file_no_note(tree, monkeypatch):
    """The override naming the default file is the same file: no note."""
    default = w(host(tree, "ears") / "ears.json", {"model": "base"})
    monkeypatch.setenv("EARS_CONF", str(default))
    res = settings.resolve("ears", SID)
    assert [s.path for s in res.sources] == [default] and res.notes == []


def test_the_schema_and_host_layer_read_one_override(tree, monkeypatch):
    """One read of the variable: the schema is looked for beside the file
    the host layer reads."""
    mine = tree / "elsewhere" / "my-ears.json"
    monkeypatch.setenv("EARS_CONF", str(mine))
    assert settings.schema_candidates("ears")[0].parent == settings.settings_file_override("ears").parent
    assert settings.settings_file_override("mail") is None


# --- two keys that join to one dotted path -----------------------------------
#
# A map key may hold a dot (a Claude Code permission rule: `Read(~/.zshrc)`),
# and the agent's dotted shorthand splits at every dot. Two different paths
# can then join to the same dotted string. The report spells each path its
# own way (a segment holding a dot is quoted) and names the clash in a NOTE.

RULE = "Read(~/.zshrc)"
QUOTED = 'permissions.rules."Read(~/.zshrc)"'
SPLIT = "permissions.rules.Read(~/.zshrc)"
HINT = 'write it nested to keep the key whole: claude-code.permissions.rules: {"Read(~/.zshrc)": ...}'


def rows(out: str) -> dict[str, list[str]]:
    """The report's key rows: first word -> the rest of the line's words."""
    found: dict[str, list[str]] = {}
    for line in out.splitlines():
        if line.startswith("  ") and not line.startswith(("  NOTE ", "  REFUSED ")):
            words = line.split()
            found[words[0]] = words[1:]
    return found


def notes(out: str) -> list[str]:
    return [line.strip()[len("NOTE "):] for line in out.splitlines() if line.strip().startswith("NOTE ")]


def test_a_split_agent_key_never_reports_its_value_under_the_rule_it_looks_like(tree):
    """The host allows the rule; the agent's dotted shorthand splits it at
    its dot. ALLOW resolves for the rule
    and is reported from the host; the agent's split leaf is reported as its
    own row, spelled apart; a NOTE names both paths, both files and the
    nested spelling, every time that row prints."""
    h = w(host(tree, "claude-code") / "claude-code.json",
          {"permissions": {"rules": {RULE: "allow"}}})
    md = agent_md(tree, "dotted-agent", f"settings:\n  claude-code.permissions.rules.{RULE}: deny")
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    assert res.values["permissions"]["rules"] == {RULE: "allow", "Read(~/": {"zshrc)": "deny"}}
    out = settings.report(res)
    got = rows(out)
    # (i) the rule resolves to allow, from the host file, and deny is never under it
    assert ['"allow"', kit_short("claude-code")] in got.values(), out
    assert got[QUOTED] == ['"allow"', kit_short("claude-code")]
    assert res.origins[QUOTED] == str(h) and res.leaves[QUOTED] == "allow"
    # (ii) the split leaf is true, so it prints, spelled apart from the rule
    assert got[SPLIT] == ['"deny"', "~/.session/agent/config/dotted-agent.md"]
    assert res.origins[SPLIT] == str(md)
    # (iii) and never without the NOTE: both paths, both files, the hint
    assert notes(out) == [(
        f"{QUOTED} ({kit_short('claude-code')}) and "
        f"{SPLIT} (~/.session/agent/config/dotted-agent.md) join to one dotted path: "
        f"two keys, neither overrides the other; {HINT}")]


def test_two_json_files_whose_paths_join_alike_both_print_and_a_note_names_them(tree):
    """No front matter at all: host `{"a.b": 1}` beside session
    `{"a": {"b": 2}}`. Both leaves stand, both rows print, one NOTE, and no
    hint (the hint is for the agent's shorthand)."""
    h = w(host(tree, "view") / "view.json", {"a.b": 1})
    s = w(session(tree) / "view.json", {"a": {"b": 2}})
    res = settings.resolve("view", SID)
    assert res.values == {"a.b": 1, "a": {"b": 2}}
    assert res.origins == {'"a.b"': str(h), "a.b": str(s)}
    out = settings.report(res)
    got = rows(out)
    assert got['"a.b"'] == ["1", kit_short("view")]
    assert got["a.b"] == ["2", f"~/.session/sessions/{SID}/settings/view.json"]
    assert notes(out) == [(
        f'"a.b" ({kit_short("view")}) and a.b (~/.session/sessions/{SID}/settings/view.json) '
        "join to one dotted path: two keys, neither overrides the other")]


def test_a_dotted_rule_key_from_host_json_round_trips(tree):
    h = w(host(tree, "claude-code") / "claude-code.json",
          {"permissions": {"rules": {RULE: "allow"}}})
    res = settings.resolve("claude-code", SID)
    assert res.values == {"permissions": {"rules": {RULE: "allow"}}}
    assert res.origins == {QUOTED: str(h)}
    out = settings.report(res)
    assert rows(out)[QUOTED] == ['"allow"', kit_short("claude-code")]
    assert notes(out) == []


def test_a_dotted_rule_key_from_nested_front_matter_round_trips(tree):
    w(host(tree, "claude-code") / "claude-code.json",
      {"permissions": {"rules": {RULE: "allow"}}})
    md = agent_md(tree, "nested-agent",
                  f'settings:\n  claude-code.permissions.rules:\n    "{RULE}": deny')
    res = settings.resolve("claude-code", SID, agent="nested-agent")
    assert res.values == {"permissions": {"rules": {RULE: "deny"}}}
    assert res.origins == {QUOTED: str(md)}
    out = settings.report(res)
    assert rows(out)[QUOTED] == ['"deny"', "~/.session/agent/config/nested-agent.md"]
    assert notes(out) == []


def test_a_lock_on_the_joined_path_holds_both_spellings(tree):
    """`_locked` stays dotted strings matched on the joined path: a lock on
    `permissions.rules.Read(~/.zshrc)` drops the agent's split key too (it
    over-locks, the safe way), so nothing survives to collide."""
    w(host(tree, "claude-code") / "claude-code.json",
      {"permissions": {"rules": {RULE: "allow"}}, "_locked": [SPLIT]})
    agent_md(tree, "dotted-agent", f"settings:\n  claude-code.permissions.rules.{RULE}: deny")
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    assert res.values == {"permissions": {"rules": {RULE: "allow"}}}
    out = settings.report(res)
    got = rows(out)
    assert SPLIT not in got
    assert got[QUOTED][:2] == ['"allow"', kit_short("claude-code")]
    assert "[locked by" in " ".join(got[QUOTED])
    assert notes(out) == []


def test_a_schema_problem_on_a_dotted_key_names_that_key_and_its_own_file(tree):
    pytest.importorskip("jsonschema")
    w(host(tree, "view") / "view.schema.json",
      {"type": "object", "properties": {"rules": {
          "type": "object", "properties": {RULE: {"type": "string"}}}}})
    h = w(host(tree, "view") / "view.json", {"rules": {RULE: 5}})
    w(session(tree) / "view.json", {"rules": {"Read(~/": {"zshrc)": "x"}}})
    res = settings.resolve("view", SID)
    assert len(res.problems) == 1
    assert res.problems[0].startswith('rules."Read(~/.zshrc)": ')
    assert res.problems[0].endswith(f"({h})")


def test_merge_still_returns_joined_keys():
    """Callers of merge() outside the report look its origins up by the
    joined path (`subscriptions.<box>`); that return is unchanged."""
    host_src = settings.Source("host", Path("/h/mail.json"),
                               {"subscriptions": {"super.sessionmail": True}})
    seat_src = settings.Source("session", Path("/s/mail.json"),
                               {"subscriptions": {"cutroom+cut-634.foreman": True}})
    values, origins, locked, leaves = settings.merge([host_src, seat_src])
    assert values == {"subscriptions": {"super.sessionmail": True, "cutroom+cut-634.foreman": True}}
    assert origins == {"subscriptions.super.sessionmail": "/h/mail.json",
                       "subscriptions.cutroom+cut-634.foreman": "/s/mail.json"}
    assert leaves == {"subscriptions.super.sessionmail": True,
                      "subscriptions.cutroom+cut-634.foreman": True}
    assert locked == {}


@pytest.mark.parametrize("path, shown", [
    (("permissions", "rules", RULE), QUOTED),
    (("permissions", "rules", "Read(~/", "zshrc)"), SPLIT),
    (("subscriptions", "super.sessionmail"), 'subscriptions."super.sessionmail"'),
    (("home",), "home"),
    (("a", ""), 'a.""'),
    (("a", 'say "hi"'), 'a."say \\"hi\\""'),
])
def test_the_report_spelling_quotes_a_segment_only_when_it_must(path, shown):
    assert settings._shown(path) == shown


def test_the_report_spelling_never_gives_two_paths_one_spelling():
    import itertools
    alphabet = ["", "a", ".", '"', "\\", "a.", ".a", 'a"', "a.b"]
    paths = [p for n in (1, 2, 3) for p in itertools.product(alphabet, repeat=n)]
    spelled = {settings._shown(p) for p in paths}
    assert len(spelled) == len(paths)


# --- an agent key the shorthand cuts inside brackets or quotes ----------------
#
# With no other key to collide with, the agent's split rule still prints a row
# that reads like the rule. A NOTE says so whenever an agent-layer leaf is cut
# at a dot inside brackets or quotes, and gives the nested spelling.

AGENT_MD = "~/.session/agent/config/dotted-agent.md"
SPLIT_NOTE = (f"{SPLIT} ({AGENT_MD}) is cut at a dot inside brackets or quotes: "
              f'"Read(~/" and "zshrc)" are two keys where one was likely meant; {HINT}')


@pytest.mark.parametrize("host_json", [{}, {"model": "opus"}])
def test_an_agent_only_split_rule_prints_beside_a_note_and_the_nested_spelling(tree, host_json):
    """No host key to collide with: the split row prints, and a NOTE names
    it, its file and the nested spelling that keeps the rule whole."""
    w(host(tree, "claude-code") / "claude-code.json", host_json)
    md = agent_md(tree, "dotted-agent", f"settings:\n  claude-code.permissions.rules.{RULE}: deny")
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    assert res.values["permissions"] == {"rules": {"Read(~/": {"zshrc)": "deny"}}}
    out = settings.report(res)
    assert rows(out)[SPLIT] == ['"deny"', AGENT_MD]
    assert res.origins[SPLIT] == str(md)
    assert notes(out) == [SPLIT_NOTE]
    assert res.notes == [SPLIT_NOTE]


def test_a_split_rule_with_no_host_file_is_not_moved_and_the_note_rides_in_notes(tree):
    """The agent layer alone is not moved: the report is its `not moved`
    line with the NOTE under it, as for every note, and the NOTE rides in
    `notes` (what --json prints)."""
    agent_md(tree, "dotted-agent", f"settings:\n  claude-code.permissions.rules.{RULE}: deny")
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    assert not res.moved
    out = settings.report(res)
    assert out.startswith("claude-code  not moved: ")
    assert notes(out) == [SPLIT_NOTE]
    assert res.notes == [SPLIT_NOTE]


def test_a_split_rule_in_the_json_report_carries_the_note(tree, capsys):
    w(host(tree, "claude-code") / "claude-code.json", {})
    agent_md(tree, "dotted-agent", f"settings:\n  claude-code.permissions.rules.{RULE}: deny")
    sd = tree / ".session" / "sessions" / SID
    sd.mkdir(parents=True, exist_ok=True)
    rc = main(["settings", SID, "claude-code", "--agent", "dotted-agent", "--json"])
    got = json.loads(capsys.readouterr().out)["tools"]["claude-code"]
    assert rc == 0
    assert got["notes"] == [SPLIT_NOTE]


def test_an_agent_that_locks_its_own_split_rule_still_gets_the_note(tree):
    """A layer's lock binds the layers below it, so the agent's split leaf
    survives its own lock: the row carries the lock and the NOTE prints."""
    w(host(tree, "claude-code") / "claude-code.json", {})
    agent_md(tree, "dotted-agent",
             f"settings:\n  claude-code.permissions.rules.{RULE}: deny\n"
             f'  claude-code._locked: ["{SPLIT}"]')
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    out = settings.report(res)
    assert rows(out)[SPLIT][:2] == ['"deny"', AGENT_MD]
    assert "[locked by" in " ".join(rows(out)[SPLIT])
    assert notes(out) == [SPLIT_NOTE]


def test_one_split_key_holding_a_map_is_one_note(tree):
    w(host(tree, "claude-code") / "claude-code.json", {})
    agent_md(tree, "dotted-agent",
             "settings:\n  claude-code.permissions.rules.WebFetch(domain:a.com):\n    a: 1\n    b: 2")
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    out = settings.report(res)
    assert {"permissions.rules.WebFetch(domain:a.com).a",
            "permissions.rules.WebFetch(domain:a.com).b"} <= set(rows(out))
    assert notes(out) == [(
        f"permissions.rules.WebFetch(domain:a.com) ({AGENT_MD}) is cut at a dot inside brackets "
        'or quotes: "WebFetch(domain:a" and "com)" are two keys where one was likely meant; '
        'write it nested to keep the key whole: '
        'claude-code.permissions.rules: {"WebFetch(domain:a.com)": ...}')]


def test_a_rule_cut_at_two_dots_names_three_keys(tree):
    w(host(tree, "claude-code") / "claude-code.json", {})
    agent_md(tree, "dotted-agent",
             "settings:\n  claude-code.permissions.rules.Bash(npm run test.unit.fast): allow")
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    assert res.notes == [(
        f"permissions.rules.Bash(npm run test.unit.fast) ({AGENT_MD}) is cut at a dot inside "
        'brackets or quotes: "Bash(npm run test", "unit" and "fast)" are 3 keys where one was '
        "likely meant; write it nested to keep the key whole: "
        'claude-code.permissions.rules: {"Bash(npm run test.unit.fast)": ...}')]


def test_a_rule_copied_from_the_report_quoted_is_cut_and_the_hint_unquotes_it(tree):
    """The report spells the whole rule `permissions.rules."Read(~/.zshrc)"`;
    written back as shorthand, the quotes do not keep it whole."""
    w(host(tree, "claude-code") / "claude-code.json", {})
    agent_md(tree, "dotted-agent", f'settings:\n  claude-code.permissions.rules."{RULE}": deny')
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    assert res.values["permissions"] == {"rules": {'"Read(~/': {'zshrc)"': "deny"}}}
    (note,) = res.notes
    assert note.endswith(f"two keys where one was likely meant; {HINT}")


def test_a_box_name_copied_from_the_report_quoted_is_cut_and_the_hint_unquotes_it(tree):
    w(host(tree, "mail") / "mail.json", {})
    agent_md(tree, "dotted-agent", 'settings:\n  mail.subscriptions."lead.example": true')
    res = settings.resolve("mail", SID, agent="dotted-agent")
    (note,) = notes(settings.report(res))
    assert note.endswith(
        'write it nested to keep the key whole: mail.subscriptions: {"lead.example": ...}')


@pytest.mark.parametrize("front", [
    "claude-code.model: opus",
    "claude-code.permissions.defaultMode: plan",
    "claude-code.env.FOO_BAR: x",
    "claude-code.permissions.rules.Read(x): deny",              # closes inside its own segment
    'claude-code.permissions.allow: ["Read(~/.zshrc)"]',        # a list is a value, never split
    f'claude-code.permissions.rules:\n    "{RULE}": deny',      # nested: one whole key
    "claude-code.env.A).B: 1",                                  # a stray closer: depth stays 0
    # A false negative: the brackets close inside `arr[0]`, so nothing is cut
    # inside them, though an index was likely meant. Front matter has no index.
    "claude-code.x.arr[0].y: 1",
])
def test_ordinary_shorthand_gets_no_split_note(tree, front):
    w(host(tree, "claude-code") / "claude-code.json", {})
    agent_md(tree, "plain-agent", f"settings:\n  {front}")
    res = settings.resolve("claude-code", SID, agent="plain-agent")
    assert len(res.origins) >= 1
    assert notes(settings.report(res)) == []


def test_a_json_key_that_opens_a_bracket_in_one_key_gets_no_split_note(tree):
    """A JSON layer has no shorthand: its keys are whole as written."""
    w(host(tree, "view") / "view.json", {"a(": {"b)": 1}})
    res = settings.resolve("view", SID)
    assert res.notes == []


def test_a_dotted_box_name_written_as_shorthand_gets_no_note(tree):
    """A false negative: `subscriptions.lead.example` cannot be told
    from a map `lead` holding `example`, so no NOTE fires. The
    per-owner row shows the map, and the nested spelling is the cure."""
    w(host(tree, "mail") / "mail.json", {})
    agent_md(tree, "dotted-agent", "settings:\n  mail.subscriptions.lead.example: true")
    res = settings.resolve("mail", SID, agent="dotted-agent")
    out = settings.report(res)
    assert rows(out)["subscriptions.lead"][:2] == ['{"example":', "true}"]
    assert notes(out) == []


def test_a_nested_map_that_opens_a_bracket_in_one_key_still_gets_the_note(tree):
    """A false positive, pinned: no shorthand cut here, the agent nested the
    two keys by hand. The note is still true, and so is the hint."""
    w(host(tree, "claude-code") / "claude-code.json", {})
    agent_md(tree, "dotted-agent",
             'settings:\n  claude-code.permissions.rules:\n    "Read(~/":\n      "zshrc)": deny')
    res = settings.resolve("claude-code", SID, agent="dotted-agent")
    assert res.notes == [SPLIT_NOTE]


@pytest.mark.parametrize("path, cuts", [
    (("permissions", "rules", "Read(~/", "zshrc)"), [2]),
    (("permissions", "rules", "Read(~/.zshrc)"), []),
    (("rules", "Bash(npm run test", "unit", "fast)"), [1, 2]),
    (("rules", 'Bash(echo "a', 'b")'), [1]),            # a quote inside brackets
    (("subscriptions", '"lead', 'example"'), [1]),
    (("a", "", "b"), []),                               # an empty segment
    (("env", "A)", "B"), []),
    (("x", "arr[0]", "y"), []),
    (("x", "a(", "b)", "c", "d(", "e)"), [1, 4]),
    (("model",), []),
])
def test_inside_cuts(path, cuts):
    assert settings._inside_cuts(path) == cuts


@pytest.mark.parametrize("run, whole", [
    (("Read(~/", "zshrc)"), "Read(~/.zshrc)"),
    (('"Read(~/', 'zshrc)"'), "Read(~/.zshrc)"),
    (('"lead', 'example"'), "lead.example"),
    (('"a', 'b" c"'), '"a.b" c"'),                     # not one JSON string: kept as it is
    (('"solo"',), '"solo"'),                            # one segment is never unquoted
])
def test_whole(run, whole):
    assert settings._whole(run) == whole
