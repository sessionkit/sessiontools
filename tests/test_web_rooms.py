"""`sessiontools.web.rooms`: the catalog, ONE validator, and both transports
(kit-web-shell design §4): a fake room package with a `sessionkit.web`
entry point in the test env, and a fake `web-room` command on a tmp PATH.
Present, absent and refused are each tested. No socket is opened here."""

from __future__ import annotations

import importlib
import json
import os
import stat
import sys

import pytest
from web_fakes import TALK, install_command, install_entry_point_package

from sessiontools import cli, settings
from sessiontools.web import rooms
from sessiontools.web.rooms import RoomRefused, validate_room


def _same_fields(room: rooms.Room) -> dict:
    return {"name": room.name, "title": room.title, "package": room.package,
            "command": list(room.command)}


def _no_which(name: str):
    raise AssertionError(f"a command was looked for ({name}) though an entry point gave it")


@pytest.fixture
def ep_package(tmp_path, monkeypatch):
    """The fake room package, installed into this test's import path."""
    root = install_entry_point_package(tmp_path / "site")
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    yield root
    sys.modules.pop("fake_talk_room", None)


@pytest.fixture
def empty_path(tmp_path, monkeypatch):
    d = tmp_path / "empty-bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))
    return d


# --- the catalog ----------------------------------------------------------------

def test_the_catalog_knows_talk_from_sessionvoice_with_its_hint():
    talk = {e.name: e for e in rooms.CATALOG}["talk"]
    assert (talk.title, talk.package, talk.command) == ("Talk", "sessionvoice", "sessionvoice-web")
    assert talk.hint == "install sessionvoice to get Talk"


# --- the one validator ------------------------------------------------------------

def test_a_good_descriptor_is_a_room():
    r = validate_room(TALK)
    assert _same_fields(r) == TALK
    assert r.argv("/tmp/x/talk.sock")[-1] == "/tmp/x/talk.sock"


@pytest.mark.parametrize(("change", "why"), [
    ({"host": "127.0.0.1"}, "unknown key"),
    ({"port": 8080}, "unknown key"),
    ({"url": "http://127.0.0.1:9"}, "unknown key"),
    ({"name": "Talk"}, "must match"),
    ({"name": "9talk"}, "must match"),
    ({"name": "../x"}, "must match"),
    ({"name": "settings"}, "front door's own"),
    ({"name": "login"}, "front door's own"),
    ({"name": "static"}, "front door's own"),
    ({"title": ""}, "title"),
    ({"package": None}, "package"),
    ({"command": "sessionvoice-web serve"}, "list of strings"),
    ({"command": []}, "list of strings"),
    ({"command": ["sessionvoice-web", 3]}, "list of strings"),
    ({"command": ["sessionvoice-web", "serve"]}, "{socket}"),
    ({"command": ["sessionvoice-web", "--port", "8080", "{socket}"]}, "TCP"),
    ({"command": ["sessionvoice-web", "--host=0.0.0.0", "{socket}"]}, "TCP"),
    ({"command": ["sessionvoice-web", "--bind", "::", "{socket}"]}, "TCP"),
])
def test_the_validator_refuses(change, why):
    with pytest.raises(RoomRefused, match=why):
        validate_room({**TALK, **change})


@pytest.mark.parametrize("bad", [None, [], "talk", 3])
def test_a_descriptor_must_be_an_object(bad):
    with pytest.raises(RoomRefused, match="JSON object"):
        validate_room(bad)


# --- transport 1: the entry point ------------------------------------------------

def test_same_venv_an_entry_point_finds_talk_and_runs_no_command(ep_package):
    found = rooms.discover(which=_no_which)
    talk = found.rooms["talk"]
    assert _same_fields(talk) == TALK
    assert talk.transport == "entry-point" and talk.source == "talk = fake_talk_room:room"
    assert found.absent == [] and found.refused == []


def test_a_broken_entry_point_is_refused_and_the_command_is_then_asked(tmp_path, monkeypatch):
    root = install_entry_point_package(tmp_path / "site", body="def room():\n    raise OSError\n")
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    install_command(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    try:
        found = rooms.discover()
    finally:
        sys.modules.pop("fake_talk_room", None)
    assert found.refused == [("talk = fake_talk_room:room", "loading it failed (OSError)")]
    assert found.rooms["talk"].transport == "command"


def test_an_entry_point_with_a_bad_descriptor_is_refused(tmp_path, monkeypatch, empty_path):
    root = install_entry_point_package(tmp_path / "site", descriptor={**TALK, "port": 1})
    monkeypatch.syspath_prepend(str(root))
    importlib.invalidate_caches()
    try:
        found = rooms.discover()
    finally:
        sys.modules.pop("fake_talk_room", None)
    assert "talk" not in found.rooms
    assert [e.name for e in found.absent] == ["talk"]
    assert "unknown key" in found.refused[0][1]


# --- transport 2: the command on PATH ----------------------------------------------

def test_separate_venv_the_web_room_command_finds_talk_with_the_same_fields(tmp_path,
                                                                            monkeypatch):
    path = install_command(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    found = rooms.discover(entry_points=list)
    talk = found.rooms["talk"]
    assert _same_fields(talk) == TALK == _same_fields(validate_room(TALK))
    assert talk.transport == "command" and talk.source == f"{path} web-room"
    assert found.absent == [] and found.refused == []


def test_both_transports_hand_over_the_same_room(tmp_path, monkeypatch, ep_package):
    by_ep = rooms.discover(which=_no_which).rooms["talk"]
    install_command(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    by_cmd = rooms.discover(entry_points=list).rooms["talk"]
    assert _same_fields(by_ep) == _same_fields(by_cmd)


@pytest.mark.parametrize(("script", "why"), [
    ("import sys; sys.exit(3)\n", "exited 3"),
    ("print('not json')\n", "no JSON"),
    (("import json; print(json.dumps({'name': 'phone', 'title': 'P', 'package': 'p', "
      "'command': ['p', '{socket}']}))\n"), "not 'talk'"),
    (("import json; print(json.dumps({'name': 'talk', 'title': 'T', 'package': 'p', "
      "'command': ['t', '--port', '1', '{socket}']}))\n"), "TCP"),
])
def test_a_command_that_answers_badly_is_refused_and_the_room_is_absent(tmp_path, monkeypatch,
                                                                        script, why):
    install_command(tmp_path / "bin", script=script)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    found = rooms.discover(entry_points=list)
    assert "talk" not in found.rooms and [e.name for e in found.absent] == ["talk"]
    assert why in found.refused[0][1]


def test_a_command_that_hangs_is_cut_off(tmp_path, monkeypatch):
    install_command(tmp_path / "bin", script="import time; time.sleep(30)\n")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(rooms, "WEB_ROOM_TIMEOUT", 0.5)
    found = rooms.discover(entry_points=list)
    assert "took over 0.5 s" in found.refused[0][1]


# --- absent --------------------------------------------------------------------------

def test_absent_neither_transport_finds_talk_and_the_hint_is_given(empty_path):
    found = rooms.discover(entry_points=list)
    assert found.rooms == {} and found.refused == []
    assert [(e.name, e.hint) for e in found.absent] == [("talk", "install sessionvoice to get Talk")]


# --- Talk's command is sessionvoice-web, never `talk` ----------------------------------

def test_discovery_looks_up_sessionvoice_web_and_never_talk():
    looked: list[str] = []
    found = rooms.discover(entry_points=list, which=lambda n: looked.append(n) or None,
                           run=_no_which)
    assert looked == ["sessionvoice-web"]
    assert [e.name for e in found.absent] == ["talk"]


def test_a_rescan_never_runs_a_talk_on_path(tmp_path, monkeypatch, capsys):
    """macOS ships `/usr/bin/talk` (the BSD chat program). A `talk` on PATH
    that would answer `web-room` perfectly is neither looked up nor run:
    with no `sessionvoice-web`, Talk is absent and the hint is shown."""
    ran = tmp_path / "talk-ran"
    install_command(tmp_path / "bin", name="talk", script=(
        f"import json, pathlib, sys\npathlib.Path({str(ran)!r}).write_text('ran')\n"
        f"print(json.dumps({TALK!r}))\n"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(rooms, "_entry_points", list)
    assert cli.main(["web", "rescan"]) == 0
    assert capsys.readouterr().out == "talk  absent: install sessionvoice to get Talk\n"
    assert not ran.exists()
    assert rooms.load(settings.web_dir()).rooms == {}


# --- rooms.json and `session web rescan` ------------------------------------------------

def test_save_and_load_round_trip_in_a_private_file(tmp_path, monkeypatch):
    install_command(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    found = rooms.discover(entry_points=list)
    d = tmp_path / "web"
    path = rooms.save(d, found)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    back = rooms.load(d)
    assert back is not None and back.rooms == found.rooms and back.absent == []
    assert rooms.load(tmp_path / "nothing-yet") is None


def test_load_revalidates_what_it_reads(tmp_path):
    d = tmp_path / "web"
    rooms.save(d, rooms.Discovery())
    data = {"rooms": [{**TALK, "command": ["sessionvoice-web", "--port", "1", "{socket}"]}]}
    (d / rooms.ROOMS_FILE).write_text(json.dumps(data))
    back = rooms.load(d)
    assert back.rooms == {} and [e.name for e in back.absent] == ["talk"]
    assert "TCP" in back.refused[0][1]


def test_session_web_rescan_finds_then_shows_the_hint(tmp_path, monkeypatch, capsys):
    install_command(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(rooms, "_entry_points", list)
    assert cli.main(["web", "rescan"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("talk  found (command: ") and "web-room)" in out
    saved = rooms.load(settings.web_dir())
    assert list(saved.rooms) == ["talk"]
    (tmp_path / "bin" / "sessionvoice-web").unlink()  # the package is removed
    assert cli.main(["web", "rescan"]) == 0
    assert capsys.readouterr().out == "talk  absent: install sessionvoice to get Talk\n"
    assert rooms.load(settings.web_dir()).rooms == {}
