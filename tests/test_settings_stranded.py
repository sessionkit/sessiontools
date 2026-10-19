"""A STRANDED tool: its host files are still only at the old path.

The rule: when a tool has no host file in its new directory
(`settings.host_dir`, `<tool>.json` or `<tool>.d/`) and has one in its old
folder (`settings.settings_root() / <tool>`), the reader writes NOTHING,
reads NOTHING old (no fallback), and says ONE line on stderr naming both
folders, at most once per tool per process. Never on stdout: the status
line and every `--json` output stay byte-clean. The hooks (busy-sound,
settle-hook) stay SILENT, their fail-silent contract.

Every test runs on a scratch HOME. The old folder is always built from
`settings.settings_root(env)`; the one literal spelling of it is the
word-for-word pin of the line, `test_the_line_is_worded_as_pinned`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools import busy, cli, settings
from sessiontools import statusline as sl

SID = "11111111-2222-3333-4444-555555555555"
REPO = Path(__file__).resolve().parents[1]

# Every file this process opens while a test watches (`opened`): the proof
# that nothing under the old folder is read. An audit hook cannot be
# removed, so it is added once and records only while a list is set.
_WATCH: list[list[str]] = []


def _audit(event: str, args: tuple) -> None:
    if _WATCH and event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        _WATCH[-1].append(os.fsdecode(args[0]))


sys.addaudithook(_audit)


@contextlib.contextmanager
def opened():
    seen: list[str] = []
    _WATCH.append(seen)
    try:
        yield seen
    finally:
        _WATCH.pop()


def w(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


def child_env(home: Path, **named: str) -> dict[str, str]:
    """An environment of NAMED keys only: HOME, PATH and NAMED. Never a
    copy of os.environ, so a failing assert or a child process sees no
    variable this test did not choose."""
    return {"HOME": str(home), "PATH": os.environ.get("PATH", os.defpath), **named}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("SESSIONS_DIR", str(h / ".session" / "sessions"))
    return h


def old(home: Path, tool: str) -> Path:
    """TOOL's folder before the kit's home, from the deprecated root."""
    return settings.settings_root({"HOME": str(home)}) / tool


def new(home: Path, tool: str) -> Path:
    return settings.host_dir(tool, {"HOME": str(home)})[0]


def strand(home: Path, tool: str) -> Path:
    """TOOL's settings at the old path only: a file and a drop-in."""
    w(old(home, tool) / f"{tool}.d" / "10-old.json", {"from": "old-drop-in"})
    return w(old(home, tool) / f"{tool}.json", {"from": "old"})


def tree(root: Path) -> dict:
    """Every path under ROOT: kind, size, mtime and content, so nothing can
    be written, added or taken away unseen (a dir's mtime moves on both)."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in [".", *dirnames, *filenames]:
            p = Path(dirpath) / name
            st = os.lstat(p)
            body = p.read_bytes() if p.is_file() else None
            snap[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns, body)
    return snap


def line_for(home: Path, tool: str) -> str:
    return settings.STRANDED_LINE.format(
        old=f"~/{old(home, tool).relative_to(home)}/",
        new=f"~/{new(home, tool).relative_to(home)}/") + "\n"


# --- the three shapes: old only, new only, both ------------------------------

@pytest.mark.parametrize("tool", settings.TOOLS)
def test_old_only_says_one_line_on_stderr_reads_nothing_old_and_writes_nothing(
        home, tool, capsys):
    strand(home, tool)
    before = tree(home)
    with opened() as seen:
        res = settings.resolve(tool, None)
    out, err = capsys.readouterr()
    assert res.values == {} and res.sources == [] and not res.moved
    assert out == ""
    assert err == line_for(home, tool)
    assert err.count("\n") == 1
    assert [p for p in seen if p.startswith(str(old(home, tool)))] == []
    assert tree(home) == before


def test_new_only_says_nothing(home, capsys):
    w(new(home, "session") / "session.json", {"from": "new"})
    with opened() as seen:
        res = settings.resolve("session", None)
    assert res.values == {"from": "new"}
    assert capsys.readouterr() == ("", "")
    assert not any(p.startswith(str(settings.settings_root())) for p in seen)


def test_both_says_nothing_and_reads_only_the_new(home, capsys):
    strand(home, "session")
    w(new(home, "session") / "session.json", {"from": "new"})
    before = tree(home)
    with opened() as seen:
        res = settings.resolve("session", None)
    assert res.values == {"from": "new"}
    assert [s.path for s in res.sources] == [new(home, "session") / "session.json"]
    assert capsys.readouterr() == ("", "")
    assert [p for p in seen if p.startswith(str(old(home, "session")))] == []
    assert tree(home) == before


def test_a_new_drop_in_folder_alone_is_moved_too(home, capsys):
    strand(home, "session")
    w(new(home, "session") / "session.d" / "20.json", {"from": "new-drop-in"})
    assert not settings.stranded("session")
    assert settings.resolve("session", None).values == {"from": "new-drop-in"}
    assert capsys.readouterr() == ("", "")


def test_an_old_drop_in_folder_alone_is_stranded(home, capsys):
    w(old(home, "sounds") / "sounds.d" / "10.json", {"enabled": True})
    assert settings.stranded("sounds")
    settings.sources("sounds", None)
    assert capsys.readouterr().err == line_for(home, "sounds")


def test_an_old_folder_with_no_host_file_is_not_stranded(home, capsys):
    """A template or schema alone is not the tool's settings."""
    w(old(home, "session") / "session.template.json", {"x": 1})
    w(old(home, "session") / "session.schema.json", {"type": "object"})
    assert not settings.stranded("session")
    settings.resolve("session", None)
    assert capsys.readouterr() == ("", "")


def test_an_override_reads_its_own_file_and_says_nothing(home, monkeypatch, capsys):
    """With its settings-file variable set, the tool reads that file; the
    default directory is not its host, so nothing is stranded there."""
    strand(home, "ears")
    named = w(home / "elsewhere" / "ears.json", {"from": "override"})
    monkeypatch.setenv("EARS_CONF", str(named))
    assert settings.resolve("ears", None).values == {"from": "override"}
    assert capsys.readouterr().err == ""


def test_a_tool_the_map_does_not_name_is_never_stranded(home, capsys):
    strand(home, "not-a-tool")
    assert not settings.stranded("not-a-tool")
    settings.host_files("not-a-tool")
    assert capsys.readouterr().err == ""


# --- the words ---------------------------------------------------------------

def test_the_line_is_worded_as_pinned(home):
    """Word for word, as sessionmail's stranded line is worded."""
    strand(home, "session")
    assert settings.stranded_line("session") == (
        "sessiontools: the settings in ~/.settings/session/ have not been moved, so they "
        "were not read and nothing was written. Move the files in ~/.settings/session/ to "
        "~/.sessionkit/tools/settings/, then re-run.")


def test_the_old_folder_is_named_only_by_the_deprecated_root(home):
    """No second spelling of the old path in src: the line's old folder is
    settings_root(env) / tool, and settings_root is called from exactly
    one place, the stranded predicate (and the line built beside it)."""
    env = {"HOME": "/home-of-test"}
    root = settings.settings_root(env)
    assert settings.stranded_line("mail", env).count(
        f"~/{(root / 'mail').relative_to('/home-of-test')}/") == 2


# --- once per tool per process -----------------------------------------------

def test_the_line_is_said_at_most_once_per_tool(home, capsys):
    strand(home, "session")
    strand(home, "claude-code")
    for _ in range(3):
        settings.resolve("session", None)
        settings.sources("session", None)
        settings.host_files("session")
    settings.resolve("claude-code", None)
    settings.resolve("claude-code", None)
    err = capsys.readouterr().err
    assert err == line_for(home, "session") + line_for(home, "claude-code")


def test_a_fresh_process_says_it_once_whatever_it_reads(home):
    """The memory is the process's own: one process, many reads, one line."""
    strand(home, "session")
    code = ("from sessiontools import settings as s\n"
            "for _ in range(5): s.resolve('session', None)\n"
            "s.sources('session', None)\n")
    got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=child_env(home), check=True)
    assert got.stdout == ""
    assert got.stderr == line_for(home, "session")


# --- never stdout -------------------------------------------------------------

def _session_dir(home: Path) -> None:
    (home / ".session" / "sessions" / SID).mkdir(parents=True)


def _settings_json(capsys) -> tuple[str, str]:
    assert cli.main(["settings", SID, "session", "--json"]) == 0
    return capsys.readouterr()


def test_a_json_command_s_stdout_is_byte_identical_with_and_without_old_files(
        tmp_path, monkeypatch, capsys):
    outs = []
    for name, stranded in (("clean", False), ("stranded", True)):
        h = tmp_path / name
        h.mkdir()
        monkeypatch.setenv("HOME", str(h))
        monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "sessions"))
        if stranded:
            strand(h, "session")
        settings._said_stranded.clear()
        (tmp_path / "sessions" / SID).mkdir(parents=True, exist_ok=True)
        out, err = _settings_json(capsys)
        outs.append(out)
        assert err == (line_for(h, "session") if stranded else "")
    assert outs[0] == outs[1]
    assert json.loads(outs[0])["tools"]["session"]["sources"] == []


def _statusline(home: Path) -> subprocess.CompletedProcess:
    env = child_env(home, SESSIONTOOLS_STATUSLINE_TAP="off",
                    SESSIONTOOLS_STATUSLINE_LOG="off")
    return subprocess.run(["/bin/bash", str(sl.SCRIPT)], input=sl.SAMPLE.read_bytes(),
                          env=env, capture_output=True, check=False, timeout=20)


def test_the_status_line_is_byte_identical_with_and_without_old_files(tmp_path):
    clean, stranded = tmp_path / "clean", tmp_path / "stranded"
    clean.mkdir()
    stranded.mkdir()
    strand(stranded, "claude-code")
    strand(stranded, "session")
    a, b = _statusline(clean), _statusline(stranded)
    assert a.returncode == b.returncode
    assert a.stdout == b.stdout and a.stdout
    assert a.stderr == b.stderr


def test_statusline_check_says_it_on_stderr_and_its_stdout_is_unchanged(
        tmp_path, monkeypatch, capsys):
    outs = []
    for name in ("clean", "stranded"):
        h = tmp_path / name
        h.mkdir()
        monkeypatch.setenv("HOME", str(h))
        monkeypatch.chdir(h)
        if name == "stranded":
            strand(h, "claude-code")
        code = cli.main(["statusline", "check"])
        out, err = capsys.readouterr()
        outs.append((code, out.replace(str(h), "HOME")))
        assert (line_for(h, "claude-code") in err) == (name == "stranded")
    assert outs[0] == outs[1]


# --- nothing written ---------------------------------------------------------

@pytest.mark.parametrize("flags", [[], ["--force"], ["--dry-run"]])
def test_statusline_install_on_a_stranded_host_writes_nothing(home, monkeypatch, capsys,
                                                               flags):
    """A first install would make the new folder a host layer and hide the
    old files for good: refused, whatever the flags."""
    monkeypatch.chdir(home)
    strand(home, "claude-code")
    before = tree(home)
    code = cli.main(["statusline", "install", *flags])
    out, err = capsys.readouterr()
    assert code == sl.REFUSED
    assert err.startswith(line_for(home, "claude-code"))
    assert "Nothing written" in err and "--force does not override this" in err
    assert err.count("sessiontools: the settings in") == 1
    assert line_for(home, "claude-code") not in out
    assert tree(home) == before
    assert not new(home, "claude-code").exists()


# --- the hooks stay silent ---------------------------------------------------

def _arm(home: Path) -> tuple[str, str]:
    payload = io.StringIO(json.dumps({"session_id": SID}))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        assert busy.main(["arm"], stdin=payload,
                         env=child_env(home, CLAUDE_CODE_SESSION_ID=SID)) == 0
    return out.getvalue(), err.getvalue()


def test_the_busy_hook_says_nothing_about_a_stranded_tool(home, monkeypatch):
    strand(home, "sounds")
    _session_dir(home)
    before = tree(settings.settings_root())
    assert _arm(home) == ("", "")
    # The hook keeps its own state in the session dir; neither settings
    # tree is touched.
    assert tree(settings.settings_root()) == before
    assert not settings.kit_root().exists()
    # The control: the same hook with `quiet` switched off reaches the
    # reader, which says the line. So the silence above is quiet's.
    monkeypatch.setattr(settings, "quiet", contextlib.nullcontext)
    assert _arm(home) == ("", line_for(home, "sounds"))


def test_the_settle_hook_says_nothing_on_a_stranded_host(home):
    for tool in settings.TOOLS:
        strand(home, tool)
    env = child_env(home, SESSION_SETTLE_AT="1")
    got = subprocess.run([str(REPO / "hooks" / "settle-hook")],
                         input=json.dumps({"session_id": SID, "transcript_path": "/nonexistent"}),
                         env=env, capture_output=True, text=True, check=False, timeout=30)
    assert (got.returncode, got.stdout, got.stderr) == (0, "", "")


def test_quiet_nests_and_restores(home, capsys):
    strand(home, "session")
    with settings.quiet():
        with settings.quiet():
            settings.sources("session", None)
        settings.sources("session", None)
    assert capsys.readouterr().err == ""
    settings.sources("session", None)
    assert capsys.readouterr().err == line_for(home, "session")
