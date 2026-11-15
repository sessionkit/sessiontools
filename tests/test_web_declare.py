"""`session web declare`: web.json, the front door's service file (ST-289).

HOME is fresh per test (conftest), so the file lands under a scratch kit
root. The schema is sessionkit's (SK-253 design 1.2); the web row is
design.md 1.3's.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sessiontools import cli, service_file, settings
from sessiontools.web import cli as web_cli
from sessiontools.web import server

REPO = Path(__file__).resolve().parent.parent


def web_json() -> Path:
    return settings.services_dir() / "web.json"


def run(argv, capsys):
    rc = cli.main(["web", *argv])
    out, err = capsys.readouterr()
    return rc, out, err


# --- the file -------------------------------------------------------------------------

def test_declare_writes_every_key_of_the_web_row(capsys):
    rc, out, err = run(["declare"], capsys)
    assert rc == 0, out + err
    data = json.loads(web_json().read_text())
    assert data == {
        "service": 1,
        "name": "web",
        "run": ["session", "web", "serve"],
        "enabled": False,
        "install": "session web declare",
        "description": "the kit's web front door (PROPOSED)",
        "port": server.DEFAULT_PORT,
        "bind": "127.0.0.1",
    }
    assert list(data) == ["service", "name", "run", "enabled", "install",
                          "description", "port", "bind"]           # the written order
    assert service_file.check(data) == []
    assert out == f"service {web_json()} (enabled: false; run: session web serve)\n"


def test_web_json_has_no_label_no_uninstall_and_is_never_enabled(capsys):
    assert run(["declare"], capsys)[0] == 0
    data = json.loads(web_json().read_text())
    assert "label" not in data and "uninstall" not in data
    assert data["enabled"] is False


def test_the_port_is_the_one_serve_listens_on_and_bind_its_default_host():
    rec = web_cli.service_record()
    sp = web_cli.build_parser()._subparsers._group_actions[0].choices["serve"]
    defaults = {a.dest: a.default for a in sp._actions}
    assert rec["port"] == defaults["port"] == server.DEFAULT_PORT
    assert rec["bind"] == defaults["host"]
    assert rec["run"] == ["session", "web", "serve"]


def test_a_rerun_gives_the_same_bytes(capsys):
    assert run(["declare"], capsys)[0] == 0
    first = web_json().read_bytes()
    assert run(["declare"], capsys)[0] == 0
    assert web_json().read_bytes() == first
    assert sorted(p.name for p in settings.services_dir().iterdir()) == ["web.json"]


def test_a_hand_edited_file_is_rewritten_whole(capsys):
    settings.services_dir().mkdir(parents=True)
    web_json().write_text('{"service": 1, "name": "web", "enabled": true, "extra": 1}\n')
    assert run(["declare"], capsys)[0] == 0
    assert web_json().read_text() == service_file.render(web_cli.service_record())


# --- web/ is never touched ------------------------------------------------------------

def test_declare_never_creates_web_dir(capsys):
    assert run(["declare"], capsys)[0] == 0
    assert not settings.web_dir().exists()
    written = [p for p in settings.kit_root().rglob("*") if p.is_file()]
    assert written == [web_json()]


def test_declare_leaves_an_existing_web_dir_alone(capsys):
    web = settings.web_dir()
    web.mkdir(parents=True, mode=0o700)
    (web / "sessions.json").write_text("{}\n")
    os.chmod(web / "sessions.json", 0o600)
    before = {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
              for p in web.iterdir()}
    dir_mode = web.stat().st_mode
    assert run(["declare"], capsys)[0] == 0
    after = {p.name: (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
             for p in web.iterdir()}
    assert after == before and web.stat().st_mode == dir_mode


def test_declare_is_handled_before_the_auth_store(monkeypatch, capsys):
    from sessiontools.web import auth

    def boom(*a, **k):
        raise AssertionError("declare built auth.Store")
    monkeypatch.setattr(auth, "Store", boom)
    assert run(["declare"], capsys)[0] == 0


# --- --dry-run ------------------------------------------------------------------------

def test_dry_run_prints_the_file_and_writes_nothing(capsys):
    rc, out, err = run(["declare", "--dry-run"], capsys)
    assert rc == 0, out + err
    assert not settings.services_dir().exists()             # not even the folder
    assert not settings.kit_root().exists() or not any(settings.kit_root().rglob("*"))
    lines = out.splitlines()
    assert lines[0] == f"service would write {web_json()}:"
    assert lines[-1] == "dry run: nothing was written"
    shown = "\n".join(lines[1:-1]) + "\n"
    assert shown == service_file.render(web_cli.service_record())   # the bytes a write puts


def test_dry_run_prints_the_bytes_a_real_write_puts_on_disk(capsys):
    _, out, _ = run(["declare", "--dry-run"], capsys)
    shown = "\n".join(out.splitlines()[1:-1]) + "\n"
    assert run(["declare"], capsys)[0] == 0
    assert web_json().read_text() == shown


def test_dry_run_leaves_an_existing_file_alone(capsys):
    settings.services_dir().mkdir(parents=True)
    web_json().write_text("old\n")
    assert run(["declare", "--dry-run"], capsys)[0] == 0
    assert web_json().read_text() == "old\n"


# --- a failed write -------------------------------------------------------------------

def test_a_failed_write_prints_the_could_not_write_line_and_exits_1(capsys):
    settings.services_dir().parent.mkdir(parents=True, exist_ok=True)
    settings.services_dir().write_text("not a folder\n")       # every write fails, for real
    rc, out, err = run(["declare"], capsys)
    assert rc == 1, out + err
    line = out.strip()
    assert line.startswith(f"service could not write {web_json()}: ")
    assert line.endswith("; the previous file is unchanged")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through 0500")
def test_a_failed_write_leaves_the_previous_file_intact(capsys):
    assert run(["declare"], capsys)[0] == 0
    before = web_json().read_bytes()
    os.chmod(settings.services_dir(), 0o500)
    try:
        rc, out, _ = run(["declare"], capsys)
    finally:
        os.chmod(settings.services_dir(), 0o755)
    assert rc == 1 and "service could not write " in out
    assert web_json().read_bytes() == before
    assert sorted(p.name for p in settings.services_dir().iterdir()) == ["web.json"]


# --- help and docs --------------------------------------------------------------------

def _declare_parser():
    return web_cli.build_parser()._subparsers._group_actions[0].choices["declare"]


def test_the_help_says_plainly_it_installs_nothing_and_only_declares():
    sub = web_cli.build_parser()._subparsers._group_actions[0]
    assert "declare" in sub.choices and "declare" in sub.metavar.split("|")
    helps = {a.dest: a.help for a in sub._choices_actions}
    assert "installs nothing" in helps["declare"]
    desc = _declare_parser().description
    assert "installs nothing" in desc and "it only declares" in desc
    assert "~/.sessionkit/tools/services/web.json" in desc
    assert "--dry-run" in _declare_parser().format_help()


def test_the_epilog_names_the_file_and_quotes_the_failure_line():
    line = service_file.COULD_NOT_WRITE.format(path="PATH", reason="REASON")
    assert "~/.sessionkit/tools/services/web.json" in web_cli.WEB_EPILOG
    assert line in web_cli.WEB_EPILOG
    assert "installs nothing" in web_cli.WEB_EPILOG
    assert "session web declare" in web_cli.WEB_EPILOG


def test_the_session_verbs_line_usage_and_page_name_declare():
    assert "declare" in dict(cli.VERBS)["web"]
    assert "session web declare [--dry-run]" in cli.__doc__
    assert "declare" in cli._web.__doc__
    page = (REPO / "docs" / "session.md").read_text()
    assert "session web declare" in page
    assert "~/.sessionkit/tools/services/web.json" in page
