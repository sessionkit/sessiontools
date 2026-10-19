"""ST-210: a settings folder that LISTS but cannot be SEARCHED (mode r--).

From 3.14, Path.is_file() / is_dir() / exists() return False for a path they
cannot stat; up to 3.13 they raised PermissionError. The settings reader
decided what to read from them, so on 3.14+ a set value fell to its default
without a word. Every check in the reader now asks one strict stat
(settings._stat_mode): ABSENT is False, UNREADABLE raises SettingsDenied,
which is a SettingsError (a command refuses it, naming the file) and the
PermissionError 3.13 raised (every caller's catch still catches it).

The folders here are really unsearchable (chmod in tmp), so these tests fail
on 3.14 at base and pass on every version after the fix. Root searches any
folder, so as root none can be built: those tests SKIP, loudly."""

from __future__ import annotations

import contextlib
import errno
import json
import os
from pathlib import Path

import pytest

from sessiontools import settings
from sessiontools.cli import main

SID = "5e7a1d0c-0000-4210-8000-000000000210"
VALUE = {"sendmail": "/usr/sbin/sendmail"}

needs_non_root = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="NOT TESTED as root: root searches a mode r-- folder, so none can be built")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / ".session" / "sessions"))
    return tmp_path


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def pkg(home: Path, tool: str = "mail") -> Path:
    return settings.host_dir(tool, {"HOME": str(home)})[0]


def session_layer(home: Path) -> Path:
    return home / ".session" / "sessions" / SID / "settings"


@contextlib.contextmanager
def unsearchable(d: Path):
    """D at mode r-- (it lists, it cannot be searched) for the body; its mode
    is restored after, pass or fail, so tmp can be cleaned."""
    d.chmod(0o444)
    try:
        yield d
    finally:
        d.chmod(0o755)


def assert_denied(info: pytest.ExceptionInfo, path: Path) -> None:
    """The raise is the strict one, naming PATH, and every 3.13 catch holds."""
    e = info.value
    assert isinstance(e, settings.SettingsDenied)
    assert isinstance(e, settings.SettingsError)
    assert isinstance(e, PermissionError) and isinstance(e, OSError)
    assert e.errno == errno.EACCES
    assert e.filename == str(path)
    assert str(path) in str(e)


# --- the three layouts the card names ----------------------------------------------------

@needs_non_root
def test_a_host_file_in_an_unsearchable_folder_raises_not_absent(home):
    d = pkg(home)
    w(d / "mail.json", VALUE)
    with unsearchable(d):
        with pytest.raises(settings.SettingsDenied) as info:
            settings.layer_files(d, "mail")
        assert_denied(info, d / "mail.json")
        with pytest.raises(settings.SettingsDenied):
            settings.host_files("mail")
        with pytest.raises(settings.SettingsDenied):
            settings.resolve("mail", None)


@needs_non_root
def test_a_drop_in_in_an_unsearchable_d_folder_raises_not_absent(home):
    d = pkg(home)
    w(d / "mail.d" / "10-mta.json", VALUE)
    with unsearchable(d / "mail.d"):
        with pytest.raises(settings.SettingsDenied) as info:
            settings.layer_files(d, "mail")
        assert_denied(info, d / "mail.d" / "10-mta.json")
        with pytest.raises(settings.SettingsDenied):
            settings.resolve("mail", None)


@needs_non_root
def test_a_template_in_an_unsearchable_folder_raises_not_absent(home):
    d = pkg(home)
    w(d / "mail.template.json", VALUE)
    with unsearchable(d):
        with pytest.raises(settings.SettingsDenied) as info:
            settings._host_files(d, "mail")
        assert_denied(info, d / "mail.template.json")
        with pytest.raises(settings.SettingsDenied):
            settings.resolve("mail", None)


@pytest.mark.parametrize("layout", ["host file", ".d entry", "template"])
@needs_non_root
def test_session_settings_refuses_naming_the_file_never_a_traceback(home, capsys, layout):
    d = pkg(home)
    if layout == ".d entry":
        named, locked = w(d / "mail.d" / "10-mta.json", VALUE), d / "mail.d"
    elif layout == "template":
        w(d / "mail.template.json", VALUE)
        named, locked = d / "mail.json", d       # the host file is stat-ed first
    else:
        named, locked = w(d / "mail.json", VALUE), d
    session_layer(home).mkdir(parents=True)
    with unsearchable(locked):
        rc = main(["settings", SID, "mail"])
    out, err = capsys.readouterr()
    assert rc == 2
    assert err.startswith("session settings: refused: ") and str(named) in err
    assert "sendmail" not in out


# --- every other check the reader makes ---------------------------------------------------

@needs_non_root
def test_stranded_raises_on_an_unsearchable_folder_not_old_path(home):
    """`_has_host` read False there on 3.14, so `stranded` looked at the OLD
    path and could say the stranded line on top of the lost value."""
    d = pkg(home)
    w(d / "mail.json", VALUE)
    w(settings.settings_root({"HOME": str(home)}) / "mail" / "mail.json", VALUE)
    with unsearchable(d), pytest.raises(settings.SettingsDenied):
        settings.stranded("mail")


@needs_non_root
def test_an_override_file_in_an_unsearchable_folder_raises(home, monkeypatch):
    conf = w(home / "conf" / "ears.json", {"x": 1})
    monkeypatch.setenv(settings.SETTINGS_FILE_ENV["ears"], str(conf))
    with unsearchable(conf.parent), pytest.raises(settings.SettingsDenied) as info:
        settings.host_files("ears")
    assert_denied(info, conf)


@needs_non_root
def test_a_session_layer_in_an_unsearchable_folder_is_refused(home, capsys):
    w(session_layer(home) / "mail.json", VALUE)
    with unsearchable(session_layer(home)):
        with pytest.raises(settings.SettingsDenied):
            settings.identity(SID)
        rc = main(["settings", SID, "mail"])
    assert rc == 2
    assert str(session_layer(home) / "session.json") in capsys.readouterr().err


@needs_non_root
def test_every_tool_listing_refuses_an_unsearchable_folder(home, capsys):
    """`session settings SID` (no tool): `tools()` read an unsearchable
    folder as one with no tool in it, so the tool was not listed."""
    d = pkg(home)
    w(d / "mail.d" / "10-mta.json", VALUE)
    session_layer(home).mkdir(parents=True)
    with unsearchable(d):
        with pytest.raises(settings.SettingsDenied):
            settings.tools(SID)
        rc = main(["settings", SID])
    assert rc == 2
    assert "refused" in capsys.readouterr().err


@needs_non_root
def test_a_session_layer_listing_that_cannot_be_searched_raises(home):
    w(session_layer(home) / "mail.json", VALUE)
    with unsearchable(session_layer(home)), pytest.raises(settings.SettingsDenied):
        settings.tools(SID)


@needs_non_root
def test_an_agent_file_in_an_unsearchable_folder_raises_not_no_layer(home):
    config = home / ".session" / "agent" / "config"
    w(config / "bob.md", "---\nsettings:\n  mail.sendmail: /usr/sbin/sendmail\n---\nbody\n")
    with unsearchable(config):
        with pytest.raises(settings.SettingsDenied):
            settings.agent_config("bob")
        with pytest.raises(settings.SettingsDenied):
            settings.agent_settings("bob", file=config / "bob.md")


@needs_non_root
def test_a_recorded_agent_file_that_cannot_be_stat_ed_is_not_called_gone(home):
    md = w(home / "agents" / "bob.md", "---\nsettings: {}\n---\n")
    with unsearchable(md.parent), pytest.raises(settings.SettingsDenied):
        settings.agent_file_for("bob", None, agent_file=str(md))


@needs_non_root
def test_a_schema_in_an_unsearchable_folder_raises_not_no_schema(home):
    d = pkg(home)
    w(d / "mail.schema.json", {"type": "object"})
    with unsearchable(d), pytest.raises(settings.SettingsDenied):
        settings.find_schema("mail")


@needs_non_root
def test_a_sessions_root_that_cannot_be_searched_refuses_the_id(home, capsys):
    root = home / ".session" / "sessions"
    (root / SID).mkdir(parents=True)
    with unsearchable(root):
        with pytest.raises(settings.SettingsDenied):
            settings.resolve_sid(SID)
        rc = main(["settings", SID, "mail"])
    assert rc == 2
    assert str(root / SID) in capsys.readouterr().err


@needs_non_root
def test_a_birth_into_an_unsearchable_drop_in_folder_is_not_written(home, capsys):
    w(pkg(home) / "mail.schema.json",
      {"type": "object", "properties": {"hooks": {"type": "boolean", "x-env": "SESSIONMAIL_HOOKS"}}})
    target = w(session_layer(home) / "mail.d" / settings.ENV_DROP_IN, {"hooks": False})
    with unsearchable(target.parent):
        with pytest.raises(settings.SettingsDenied):
            settings.write_env_at_birth(session_layer(home).parent, {"SESSIONMAIL_HOOKS": ""})
        assert settings.birth_main(["--dir", str(session_layer(home).parent)],
                                   environ={"HOME": str(home), "SESSIONMAIL_HOOKS": ""}) == 0
    assert "not written (SettingsDenied" in capsys.readouterr().err


@needs_non_root
def test_a_layer_file_that_cannot_be_read_is_refused_naming_it(home, capsys):
    """Mode 000 on the FILE (its folder searchable): every version raised
    PermissionError from the read, a traceback for `session settings`."""
    f = w(pkg(home) / "mail.json", VALUE)
    session_layer(home).mkdir(parents=True)
    f.chmod(0o000)
    try:
        with pytest.raises(settings.SettingsDenied) as info:
            settings.read_json_object(f)
        assert_denied(info, f)
        assert main(["settings", SID, "mail"]) == 2
    finally:
        f.chmod(0o644)
    assert str(f) in capsys.readouterr().err


@pytest.mark.parametrize("verb", [["check"], ["install", "--dry-run"], ["install"]])
@needs_non_root
def test_statusline_refuses_an_unsearchable_kit_folder_never_a_traceback(home, capsys, verb):
    """`session statusline` stats the kit layer (`stranded`, `_host_files`)
    before it writes: 3.13 raised a traceback there, and 3.14 read the
    folder as empty (state F). Now it is refused, exit 5 (UNREADABLE), the
    file named, and nothing is written."""
    kit = settings.host_dir("claude-code", {"HOME": str(home)})[0]
    w(kit / "claude-code.json", {"statusLine": {"type": "command", "command": "x"}})
    with unsearchable(kit):
        rc = main(["statusline", *verb])
    out, err = capsys.readouterr()
    assert rc == 5
    assert out == ""                       # no state report read off an empty-looking folder
    assert err.startswith(f"session statusline {verb[0]}: refused: [Errno 13]")
    assert f"'{kit}/claude-code." in err      # check stats the template first, install the host file
    assert "Traceback" not in err
    assert sorted(p.name for p in kit.iterdir()) == ["claude-code.json"]   # nothing written


# --- absent and readable are as they were, on every version -------------------------------

def test_an_absent_folder_has_no_files_and_raises_nothing(home):
    d = pkg(home)
    assert settings.layer_files(d, "mail") == []
    assert settings._host_files(d, "mail") == []
    assert settings._has_host(d, "mail") is False
    assert settings.stranded("mail") is False
    assert settings.resolve("mail", None).values == {}


def test_a_file_where_the_folder_should_be_is_absent(home):
    d = pkg(home)
    w(d, "not a folder")
    assert settings.layer_files(d, "mail") == []
    assert settings._host_files(d, "mail") == []


def test_a_readable_folder_is_read(home):
    d = pkg(home)
    w(d / "mail.json", {"a": 1})
    w(d / "mail.d" / "10-b.json", {"b": 2})
    w(d / "mail.d" / "README", "not json")
    w(d / "mail.template.json", {"c": 3})
    assert settings._host_files(d, "mail") == [
        d / "mail.template.json", d / "mail.json", d / "mail.d" / "10-b.json"]
    assert settings.resolve("mail", None).values == {"a": 1, "b": 2, "c": 3}


def test_a_dangling_symlink_is_absent_and_a_loop_too(home):
    d = pkg(home)
    d.mkdir(parents=True)
    (d / "mail.json").symlink_to(d / "nowhere.json")
    (d / "mail.d").symlink_to(d / "mail.d")            # ELOOP
    assert settings.layer_files(d, "mail") == []


# --- the helper's errno rule, version-independent -----------------------------------------

class _Raises:
    """A path whose stat() raises ERROR: no chmod, so this runs as root too."""

    def __init__(self, error: BaseException):
        self.error = error

    def stat(self):
        raise self.error


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP])
def test_the_errnos_3_13_read_as_absent_are_absent(code):
    p = _Raises(OSError(code, os.strerror(code), "/x"))
    assert settings._stat_mode(p) is None
    assert settings._is_file(p) is False and settings._is_dir(p) is False


def test_a_path_the_os_cannot_take_is_absent():
    assert settings._is_file(_Raises(ValueError("embedded null byte"))) is False


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
def test_a_permission_errno_raises_settings_denied(code):
    with pytest.raises(settings.SettingsDenied) as info:
        settings._is_file(_Raises(OSError(code, os.strerror(code), "/x/mail.json")))
    e = info.value
    assert isinstance(e, PermissionError) and isinstance(e, settings.SettingsError)
    assert (e.errno, e.filename) == (code, "/x/mail.json")
    assert str(e) == str(OSError(code, os.strerror(code), "/x/mail.json"))   # 3.13's words


def test_any_other_errno_raises_unreadable_and_is_not_a_permission_error():
    with pytest.raises(settings.SettingsUnreadable) as info:
        settings._is_dir(_Raises(OSError(errno.EIO, os.strerror(errno.EIO), "/x")))
    assert isinstance(info.value, OSError) and isinstance(info.value, settings.SettingsError)
    assert not isinstance(info.value, PermissionError)


def test_each_catch_a_caller_wrote_for_3_13_still_catches_it():
    """sessionmail `_layer.read` catches (SettingsError, OSError), its
    `_operator` callers Exception; sessiontools' account readers and band
    (SettingsError, OSError, ValueError), plan.py SettingsError."""
    raised = settings._unreadable(OSError(errno.EACCES, "Permission denied", "/x"), Path("/x"))
    for caught in (PermissionError, OSError, settings.SettingsError, Exception,
                   (settings.SettingsError, OSError, ValueError)):
        try:
            raise raised
        except caught:
            pass
