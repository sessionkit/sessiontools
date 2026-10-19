"""`session statusline install | check | sample` (ST-136; ST-165 design.md sections 1-3, 7, 9).

Install writes the kit's claude-code layer, K = the tool's settings directory
(settings.host_dir, under ~/.sessionkit/):
the script at K/statusline.sh, the entry in the drop-in D =
K/claude-code.d/50-statusline.json. It never writes ~/.claude. `check` still
reads ~/.claude until ST-165 do-003, so its tests seed the ST-136 install
shape there by hand (seed_claude).

Every test runs on a scratch HOME (and, where it says so, a scratch
CLAUDE_CONFIG_DIR) under tmp_path. Managed settings are pointed at a scratch
directory too, and the cwd is a scratch dir, so no test reads or writes the
machine's real ~/.claude, ~/.sessionkit, its managed settings or
a project's settings.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest
from git_support import HOOKS_OFF, hermetic_env
from settings_support import host

from sessiontools import cli
from sessiontools import statusline as sl

ANSI = re.compile(r"\x1b\[[0-9;]*m")


# --- the harness ---------------------------------------------------------------

class Env:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.cdir = self.home / ".claude"                       # read by check; never written
        self.kdir = host(self.home, "claude-code")              # K
        self.managed = root / "managed"
        self.work = root / "work"

    @property
    def settings(self) -> Path:
        """D: the drop-in install owns."""
        return self.kdir / "claude-code.d" / "50-statusline.json"

    @property
    def script(self) -> Path:
        return self.kdir / "statusline.sh"

    def write_settings(self, data) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps(data, indent=2) + "\n")

    def read_settings(self):
        return json.loads(self.settings.read_text())

    def write_kit(self, name: str, data) -> Path:
        """Another kit host file: `claude-code.json` or `claude-code.d/<name>`."""
        path = self.kdir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        return path

    @property
    def claude_settings(self) -> Path:
        return self.cdir / "settings.json"

    @property
    def claude_script(self) -> Path:
        return self.cdir / "statusline.sh"

    def write_claude_settings(self, data) -> None:
        self.cdir.mkdir(parents=True, exist_ok=True)
        self.claude_settings.write_text(json.dumps(data, indent=2) + "\n")

    def read_claude_settings(self):
        return json.loads(self.claude_settings.read_text())


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    e.home.mkdir()
    e.work.mkdir()
    monkeypatch.setenv("HOME", str(e.home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(sl, "MANAGED_DIRS", {sys.platform: e.managed})
    monkeypatch.chdir(e.work)
    # The real ~/.claude and ~/.sessionkit are never the target: prove the fixture took.
    assert sl.config_dir() == e.cdir
    assert sl.kit_dir() == e.kdir
    return e


def run(capsys, *args: str) -> tuple[int, str, str]:
    code = cli.main(["statusline", *args])
    out, err = capsys.readouterr()
    return code, out, err


def snapshot(root: Path) -> dict:
    """Every path under root, and root itself: kind, mode, size, mtime_ns,
    inode and content. A dir's own mtime moves when an entry is created or
    removed in it, so nothing can be added or taken away unseen."""
    snap = {}
    if os.path.lexists(root):
        st = os.lstat(root)
        snap["."] = (st.st_mode, st.st_mtime_ns, st.st_ino)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = os.lstat(p)
            if os.path.islink(p):
                body = ("link", os.readlink(p))
            elif p.is_file():
                body = ("file", p.read_bytes())
            else:
                body = ("dir", None)
            snap[str(p.relative_to(root))] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino, body)
    return snap


#: K relative to HOME, derived from settings.host_dir (never a literal shape).
KIT_REL = host(Path("/home-of-test"), "claude-code").relative_to("/home-of-test")
#: Our entry, as install writes it into D: the tilde form of K/statusline.sh.
OURS = {"type": "command", "command": f"~/{KIT_REL}/statusline.sh", "padding": 1}
#: The entry an ST-136 install wrote into ~/.claude/settings.json.
OURS_CLAUDE = {"type": "command", "command": "~/.claude/statusline.sh", "padding": 1}
FOREIGN = b"#!/bin/bash\necho 'my own line'\n"


def seed_claude(e: Env) -> None:
    """What an ST-136 install left in ~/.claude: `check` reads it until do-003."""
    e.write_claude_settings({"statusLine": OURS_CLAUDE})
    e.claude_script.write_bytes(sl.stamped())
    os.chmod(e.claude_script, 0o755)


def pristine_other(version: str) -> bytes:
    """A pristine copy of some other shipped version: its stamp matches its body."""
    body = sl.shipped() + b"# a line another version had\n"
    return sl.stamped(body, version)


def backups(e: Env) -> list[Path]:
    """Every backup in K and K/claude-code.d: each sits beside its file."""
    dirs = [d for d in (e.kdir, e.kdir / "claude-code.d") if d.is_dir()]
    return sorted(p for d in dirs for p in d.iterdir() if ".bak-" in p.name)


# --- E: the empty slot -----------------------------------------------------------

def test_empty_slot_writes_both_and_reads_back(env, capsys):
    code, out, err = run(capsys, "install")
    assert (code, err) == (0, "")
    assert env.read_settings() == {"_about": sl.ABOUT, "statusLine": OURS}
    assert list(env.read_settings()) == ["_about", "statusLine"]
    data = env.script.read_bytes()
    assert data == sl.stamped()
    assert data.splitlines()[1].startswith(b"# sessiontools-statusline ")
    assert os.stat(env.script).st_mode & 0o777 == 0o755
    assert "state E" in out and "read back: both match" in out
    assert "session start seats show it from their next launch" in out
    assert f"statusLine runs {OURS['command']}" in out
    # The plan prints first, then the result.
    assert out.index("plan:") < out.index("installed:")
    assert out.index(f"create {env.kdir}/\n") < out.index(f"create {env.kdir}/claude-code.d/\n")
    assert f"create {env.script}" in out
    assert f"{env.settings} is created holding _about and statusLine" in out
    assert not env.cdir.exists()                     # ~/.claude never made


def test_the_installed_script_renders(env, capsys):
    run(capsys, "install")
    got = subprocess.run(["/bin/bash", str(env.script)], input=sl.SAMPLE.read_bytes(),
                         env={"HOME": str(env.home), "PATH": os.environ["PATH"],
                              "SESSIONTOOLS_STATUSLINE_TAP": "off"},
                         capture_output=True, check=False, timeout=30)
    assert got.returncode == 0
    assert ANSI.sub("", got.stdout.decode()).endswith("| Opus 5.5\n")


def test_an_existing_settings_json_keeps_its_keys_order_and_mode(env, capsys):
    env.write_settings({"zeta": 1, "model": "opus", "hooks": {"Stop": []}, "alpha": [1, 2]})
    os.chmod(env.settings, 0o600)
    code, _out, _err = run(capsys, "install")
    assert code == 0
    data = env.read_settings()
    assert list(data) == ["zeta", "model", "hooks", "alpha", "statusLine"]
    assert data["statusLine"] == OURS
    assert os.stat(env.settings).st_mode & 0o777 == 0o600
    assert env.settings.read_text().endswith("}\n")


def test_a_symlinked_settings_json_stays_a_link(env, capsys):
    dotfiles = env.root / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "claude-settings.json"
    real.write_text('{"model": "opus"}\n')
    env.settings.parent.mkdir(parents=True)
    env.settings.symlink_to(real)
    code, out, _err = run(capsys, "install")
    assert code == 0
    assert env.settings.is_symlink() and os.readlink(env.settings) == str(real)
    assert json.loads(real.read_text()) == {"model": "opus", "statusLine": OURS}
    assert f"written at {real}" in out


# --- C: idempotent -------------------------------------------------------------------

def test_a_second_run_changes_nothing(env, capsys):
    run(capsys, "install")
    before = snapshot(env.home)
    time.sleep(0.01)
    code, out, err = run(capsys, "install")
    assert (code, err) == (0, "")
    assert "state C" in out and "nothing to do" in out
    assert snapshot(env.home) == before


def test_force_over_current_changes_nothing(env, capsys):
    run(capsys, "install")
    before = snapshot(env.home)
    assert run(capsys, "install", "--force")[0] == 0
    assert snapshot(env.home) == before


def test_ours_entry_extra_fields_are_kept(env, capsys):
    """refreshInterval and an unknown key in an ours-entry survive every run."""
    entry = {"type": "command", "command": OURS["command"], "padding": 0,
             "refreshInterval": 5, "x-mine": True}
    env.write_settings({"statusLine": entry})
    env.script.write_bytes(pristine_other("0.0.1"))   # U: the file is replaced
    assert run(capsys, "install")[0] == 0
    assert env.read_settings()["statusLine"] == entry


# --- dry run -----------------------------------------------------------------------------

@pytest.mark.parametrize("setup,args", [
    ("empty", ()),
    ("other", ("--force",)),
    ("edited", ("--force",)),
    ("older", ()),
])
def test_dry_run_writes_nothing(env, capsys, setup, args):
    if setup == "other":
        env.write_settings({"statusLine": "echo hi", "k": 1})
        env.script.write_bytes(FOREIGN)
    elif setup == "edited":
        env.write_settings({"statusLine": OURS})
        env.script.write_bytes(sl.stamped() + b"# mine\n")
    elif setup == "older":
        env.write_settings({"statusLine": OURS})
        env.script.write_bytes(pristine_other("0.0.1"))
    before = snapshot(env.home)
    code, out, _err = run(capsys, "install", "--dry-run", *args)
    assert code == 0
    assert "plan:" in out and "dry run: nothing written" in out
    assert snapshot(env.home) == before


def test_dry_run_of_a_refusal_says_so_and_writes_nothing(env, capsys):
    env.write_settings({"statusLine": "echo hi"})
    before = snapshot(env.home)
    code, _out, err = run(capsys, "install", "--dry-run")
    assert code == 3 and "already got one" in err
    assert snapshot(env.home) == before


# --- F: a file, no entry ---------------------------------------------------------------

def test_F_pristine_current_writes_only_the_entry(env, capsys):
    env.kdir.mkdir(parents=True)
    env.script.write_bytes(sl.stamped())
    os.chmod(env.script, 0o755)
    mtime = os.stat(env.script).st_mtime_ns
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state F" in out
    assert env.read_settings() == {"_about": sl.ABOUT, "statusLine": OURS}
    assert os.stat(env.script).st_mtime_ns == mtime
    assert backups(env) == []


def test_F_pristine_older_upgrades_and_wires(env, capsys):
    env.kdir.mkdir(parents=True)
    env.script.write_bytes(pristine_other("0.0.1"))
    code, out, _err = run(capsys, "install")
    assert code == 0
    assert env.script.read_bytes() == sl.stamped()
    assert env.read_settings() == {"_about": sl.ABOUT, "statusLine": OURS}
    assert f"v0.0.1 → v{sl.version()}" in out
    assert backups(env) == []


def test_F_foreign_refuses_and_names_the_path(env, capsys):
    env.kdir.mkdir(parents=True)
    env.script.write_bytes(FOREIGN)
    before = snapshot(env.home)
    code, _out, err = run(capsys, "install")
    assert code == 3
    assert f"you've already got one: {env.script}" in err
    assert "not wired into settings" in err
    assert "Nothing written" in err
    assert snapshot(env.home) == before


def test_F_foreign_force_backs_up_and_writes_both(env, capsys):
    env.kdir.mkdir(parents=True)
    env.script.write_bytes(FOREIGN)
    code, out, _err = run(capsys, "install", "--force")
    assert code == 0
    (bak,) = backups(env)
    assert bak.read_bytes() == FOREIGN
    assert f"backed up: {bak}" in out
    assert env.script.read_bytes() == sl.stamped()
    assert env.read_settings() == {"_about": sl.ABOUT, "statusLine": OURS}


def test_F_force_over_pristine_ours_makes_no_backup(env, capsys):
    env.kdir.mkdir(parents=True)
    env.script.write_bytes(pristine_other("0.0.1"))
    assert run(capsys, "install", "--force")[0] == 0
    assert backups(env) == []


# --- U: another shipped version ---------------------------------------------------------

@pytest.mark.parametrize("old", ["0.0.1", "9.9.9"])
def test_U_replaces_an_older_or_newer_pristine_copy(env, capsys, old):
    env.write_settings({"statusLine": OURS})
    settings_before = env.settings.read_bytes()
    env.script.write_bytes(pristine_other(old))
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state U" in out
    assert f"v{old} → v{sl.version()}" in out
    assert env.script.read_bytes() == sl.stamped()
    assert env.settings.read_bytes() == settings_before   # entry untouched
    assert backups(env) == []


def test_U_same_version_other_bytes_names_the_hashes(env, capsys):
    env.write_settings({"statusLine": OURS})
    env.script.write_bytes(pristine_other(sl.version()))
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state U" in out
    assert sl.sha(sl.shipped())[:8] in out


# --- M: entry ours, file missing --------------------------------------------------------

def test_M_writes_the_script_and_leaves_the_entry(env, capsys):
    env.write_settings({"statusLine": OURS, "other": 1})
    settings_before = env.settings.read_bytes()
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state M" in out
    assert env.script.read_bytes() == sl.stamped()
    assert env.settings.read_bytes() == settings_before


# --- D: entry ours, file edited or not ours ---------------------------------------------

def test_D_edited_refuses_and_points_at_the_diff(env, capsys):
    env.write_settings({"statusLine": OURS})
    env.script.write_bytes(sl.stamped() + b"# my edit\n")
    before = snapshot(env.home)
    code, _out, err = run(capsys, "install")
    assert code == 3
    assert f"you've already got one: {env.script}" in err
    assert "edited" in err and "check --diff" in err
    assert snapshot(env.home) == before


def test_D_known_hand_copy_is_named(env, capsys, monkeypatch):
    monkeypatch.setitem(sl.KNOWN, sl.sha(FOREIGN), "an earlier hand copy of this status line (test)")
    env.write_settings({"statusLine": OURS})
    env.script.write_bytes(FOREIGN)
    code, _out, err = run(capsys, "install")
    assert code == 3 and "an earlier hand copy of this status line (test)" in err


def test_the_two_legacy_hashes_are_known():
    assert set(sl.KNOWN) >= {
        "a19e82a30f205cca95d8bc38747175b98e01bdf2091305171144d5186cc18333",
        "68f55691a4eb23e88c9e8beb4d175ce2fc0234334b9a29ed665b7953082ff55f"}


def test_D_force_backs_up_then_replaces(env, capsys):
    env.write_settings({"statusLine": OURS})
    settings_before = env.settings.read_bytes()
    edited = sl.stamped() + b"# my edit\n"
    env.script.write_bytes(edited)
    code, out, _err = run(capsys, "install", "--force")
    assert code == 0
    (bak,) = backups(env)
    assert bak.read_bytes() == edited and str(bak) in out
    assert env.script.read_bytes() == sl.stamped()
    assert env.settings.read_bytes() == settings_before


def test_a_backup_name_is_never_reused(env):
    env.kdir.mkdir(parents=True)
    env.script.write_bytes(b"x")
    now = time.time()
    first = sl.backup_name(env.script, now)
    first.write_bytes(b"old backup")
    second = sl.backup_name(env.script, now)
    second.write_bytes(b"older backup")
    third = sl.backup_name(env.script, now)
    assert first.name.startswith("statusline.sh.bak-")
    assert second.name == first.name + "-2" and third.name == first.name + "-3"


# --- O: another status line ---------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "echo hi",
    None,
    {"type": "other"},
    {"type": "command", "command": f"bash ~/{KIT_REL}/statusline.sh"},
    {"type": "command", "command": "echo $(whoami)", "padding": 2},
])
def test_O_refuses_and_prints_the_value_verbatim(env, capsys, value):
    env.write_settings({"statusLine": value})
    before = snapshot(env.home)
    code, _out, err = run(capsys, "install")
    assert code == 3
    assert f"inline in {env.settings}: {json.dumps(value)}" in err
    assert "`--force` replaces it, backing up first" in err
    assert snapshot(env.home) == before


def test_O_another_path_is_named(env, capsys):
    theirs = env.root / "their-line.sh"
    theirs.write_text("#!/bin/sh\necho theirs\n")
    env.write_settings({"statusLine": {"type": "command", "command": str(theirs)}})
    code, _out, err = run(capsys, "install")
    assert code == 3 and f"you've already got one: {theirs}" in err


def test_a_refusal_shows_our_line_from_the_sample(env, capsys):
    env.write_settings({"statusLine": "echo hi"})
    _code, _out, err = run(capsys, "install")
    assert "sessiontools' status line would show (sample payload):" in err
    assert "Claude | 421K/1.0M (42%) | $12 | 5h:12% wk:63% | Opus 5.5" in ANSI.sub("", err)


def test_O_force_backs_up_both_and_never_touches_their_file(env, capsys):
    theirs = env.root / "their-line.sh"
    theirs.write_text("#!/bin/sh\necho theirs\n")
    their_stat = os.stat(theirs)
    old_entry = {"type": "command", "command": str(theirs), "padding": 0,
                 "refreshInterval": 3, "x-mine": "kept"}
    env.write_settings({"a": 1, "statusLine": old_entry, "z": 2})
    settings_before = env.settings.read_bytes()
    env.script.write_bytes(FOREIGN)               # a stale /statusline leftover
    code, out, _err = run(capsys, "install", "--force")
    assert code == 0
    baks = backups(env)
    assert len(baks) == 2
    by_name = {b.name.split(".bak-")[0]: b for b in baks}
    assert by_name["50-statusline.json"].read_bytes() == settings_before
    assert by_name["50-statusline.json"].parent == env.settings.parent    # beside its file
    assert by_name["statusline.sh"].read_bytes() == FOREIGN
    data = env.read_settings()
    assert list(data) == ["a", "statusLine", "z"]
    assert data["statusLine"] == {"type": "command", "command": OURS["command"],
                                  "padding": 0, "refreshInterval": 3, "x-mine": "kept"}
    assert theirs.read_text() == "#!/bin/sh\necho theirs\n"
    assert os.stat(theirs).st_mtime_ns == their_stat.st_mtime_ns
    assert f"(was: {json.dumps(old_entry)})" in out


def test_O_force_with_a_symlinked_settings_json_backs_up_its_bytes(env, capsys):
    """settings.json is written THROUGH its link, so a link backup would
    point at the new content. The backup is a plain copy of the old bytes."""
    dotfiles = env.root / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "claude-settings.json"
    old = json.dumps({"model": "opus", "statusLine": {"type": "command",
                                                      "command": "echo theirs"}}) + "\n"
    real.write_text(old)
    env.settings.parent.mkdir(parents=True)
    env.settings.symlink_to(real)
    assert run(capsys, "install", "--force")[0] == 0
    (bak,) = [b for b in backups(env) if b.name.startswith("50-statusline.json")]
    assert not bak.is_symlink() and bak.read_text() == old
    assert env.settings.is_symlink() and os.readlink(env.settings) == str(real)
    assert json.loads(real.read_text())["statusLine"]["command"] == OURS["command"]


def test_O_bash_of_our_path_under_force_backs_that_file_up(env, capsys):
    env.write_settings({"statusLine": {"type": "command", "command": f"bash {OURS['command']}"}})
    env.script.write_bytes(FOREIGN)
    assert run(capsys, "install", "--force")[0] == 0
    assert any(b.name.startswith("statusline.sh.bak-") and b.read_bytes() == FOREIGN
               for b in backups(env))
    assert env.read_settings()["statusLine"]["command"] == OURS["command"]


@pytest.mark.parametrize("value", ["echo hi", None])
def test_O_force_over_a_non_object_writes_a_fresh_entry(env, capsys, value):
    env.write_settings({"statusLine": value})
    assert run(capsys, "install", "--force")[0] == 0
    assert env.read_settings()["statusLine"] == OURS


def test_O_force_with_a_symlink_backs_up_the_link_as_a_link(env, capsys):
    target = env.root / "elsewhere.sh"
    target.write_bytes(FOREIGN)
    mtime = os.stat(target).st_mtime_ns
    env.write_settings({"statusLine": "echo hi"})
    env.script.symlink_to(target)
    assert run(capsys, "install", "--force")[0] == 0
    (link_bak,) = [b for b in backups(env) if b.name.startswith("statusline.sh")]
    assert link_bak.is_symlink() and os.readlink(link_bak) == str(target)
    assert target.read_bytes() == FOREIGN and os.stat(target).st_mtime_ns == mtime
    assert not env.script.is_symlink() and env.script.read_bytes() == sl.stamped()


@pytest.mark.parametrize("layout", ["kit-dir-is-a-link", "drop-in-dir-is-a-link"])
def test_force_through_a_linked_kit_dir_backs_up_real_bytes(env, capsys, layout):
    """Dotfiles layouts: K (or K/claude-code.d) is itself a symlink, and D
    inside it is a relative link to a file that is not a drop-in (`.data`).
    Every --force backup holds the OLD bytes as a plain file, beside its
    file, and the links survive."""
    real = env.root / "dotfiles" / "claude-code"
    if layout == "kit-dir-is-a-link":
        (real / "claude-code.d").mkdir(parents=True)
        env.kdir.parent.mkdir(parents=True)
        env.kdir.symlink_to(real)
        linked, drops, script_dir = env.kdir, real / "claude-code.d", real
    else:
        drops = real
        drops.mkdir(parents=True)
        env.kdir.mkdir(parents=True)
        (env.kdir / "claude-code.d").symlink_to(drops)
        linked, script_dir = env.kdir / "claude-code.d", env.kdir
    old = b'{\n  "statusLine": "echo old",\n  "keep": 1\n}\n'
    (drops / "d.data").write_bytes(old)
    (drops / "50-statusline.json").symlink_to("d.data")
    (script_dir / "statusline.sh").write_bytes(FOREIGN)
    assert run(capsys, "install", "--force")[0] == 0
    baks = {p.name.split(".bak-")[0]: p for d in {drops, script_dir} for p in d.iterdir()
            if ".bak-" in p.name}
    assert not baks["50-statusline.json"].is_symlink()
    assert baks["50-statusline.json"].read_bytes() == old
    assert baks["statusline.sh"].read_bytes() == FOREIGN
    assert linked.is_symlink() and os.readlink(drops / "50-statusline.json") == "d.data"
    assert json.loads((drops / "d.data").read_text())["keep"] == 1
    assert "state C" in run(capsys, "install")[1]


# --- L: a symlinked statusline.sh -------------------------------------------------------------

@pytest.mark.parametrize("entry", [None, OURS])
def test_L_refuses_and_names_the_target(env, capsys, entry):
    target = env.root / "dotfiles-statusline.sh"
    target.write_bytes(FOREIGN)
    env.write_settings({"statusLine": entry} if entry else {})
    env.script.symlink_to(target)
    before = snapshot(env.home)
    code, out, err = run(capsys, "install")
    assert code == 3 and "state L" in out
    assert f"a symlink to {target}" in err
    assert snapshot(env.home) == before


@pytest.mark.parametrize("entry", [None, OURS])
def test_L_force_replaces_the_link_and_spares_its_target(env, capsys, entry):
    target = env.root / "dotfiles-statusline.sh"
    target.write_bytes(FOREIGN)
    mtime = os.stat(target).st_mtime_ns
    env.write_settings({"statusLine": entry} if entry else {})
    env.script.symlink_to(target)
    assert run(capsys, "install", "--force")[0] == 0
    (bak,) = backups(env)
    assert bak.is_symlink() and os.readlink(bak) == str(target)
    assert not env.script.is_symlink() and env.script.read_bytes() == sl.stamped()
    assert target.read_bytes() == FOREIGN and os.stat(target).st_mtime_ns == mtime
    assert env.read_settings()["statusLine"] == OURS     # set when there was no key


# --- X and the non-regular script: refused, --force included ---------------------------------

@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '"a string"', ""])
@pytest.mark.parametrize("force", [(), ("--force",)])
def test_X_unreadable_settings_refuses(env, capsys, text, force):
    env.settings.parent.mkdir(parents=True)
    env.settings.write_text(text)
    before = snapshot(env.home)
    code, _out, err = run(capsys, "install", *force)
    assert code == 5
    assert str(env.settings) in err and "unreadable" in err
    assert snapshot(env.home) == before


@pytest.mark.parametrize("force", [(), ("--force",)])
def test_a_directory_where_the_script_goes_is_refused(env, capsys, force):
    env.script.mkdir(parents=True)
    before = snapshot(env.home)
    code, _out, err = run(capsys, "install", *force)
    assert code == 5 and "not a file or a symlink" in err
    assert snapshot(env.home) == before


# --- CLAUDE_CONFIG_DIR ------------------------------------------------------------------------

def test_claude_config_dir_does_not_move_the_install(env, capsys, monkeypatch):
    """CLAUDE_CONFIG_DIR names the dir a bare claude reads. Install writes K
    whatever it says, and creates neither it nor ~/.claude."""
    cfg = env.root / "cfg" / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    code, out, _err = run(capsys, "install")
    assert code == 0
    assert f"session statusline install: {env.kdir}" in out
    assert env.read_settings()["statusLine"] == OURS
    assert not cfg.exists() and not env.cdir.exists()
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state C" in out


def test_the_command_is_the_tilde_form_and_its_absolute_twin_is_ours(env, capsys):
    """command_for(K) is `~/...`: the shell claude runs it in expands it.
    An entry naming K's script by its absolute path is ours too (is_ours
    expands `~`), so a person who wrote it that way is C, not O."""
    assert sl.command_for(env.kdir) == OURS["command"]
    assert sl.command_for(env.root / "elsewhere") == str(env.root / "elsewhere" / "statusline.sh")
    env.write_settings({"statusLine": {"type": "command", "command": str(env.script)}})
    env.script.write_bytes(sl.stamped())
    os.chmod(env.script, 0o755)
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state C" in out


def test_kit_dir_is_the_settings_cascade_s_own():
    from sessiontools import settings
    assert sl.kit_dir() == settings.host_dir("claude-code")[0]
    assert sl.drop_in_path(sl.kit_dir()).parent.name == "claude-code.d"


# --- failures: exit 4, never a traceback, never a lost file --------------------------------------

def test_read_back_mismatch_is_loud(env, capsys, monkeypatch):
    real = sl._write_settings

    def drops_the_key(path, data):
        real(path, {k: v for k, v in data.items() if k != "statusLine"})
    monkeypatch.setattr(sl, "_write_settings", drops_the_key)
    code, _out, err = run(capsys, "install")
    assert code == 4
    assert "but read back nothing: the status line is NOT installed" in err


def test_read_back_catches_a_script_that_did_not_land(env, capsys, monkeypatch):
    real = sl._write_bytes

    def truncates(path, data, mode):
        real(path, data[:100], mode)
    monkeypatch.setattr(sl, "_write_bytes", truncates)
    code, _out, err = run(capsys, "install")
    assert code == 4 and "NOT installed" in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only dir")
def test_a_backup_that_cannot_land_replaces_nothing(env, capsys):
    env.write_settings({"statusLine": OURS})
    edited = sl.stamped() + b"# my edit\n"
    env.script.write_bytes(edited)
    os.chmod(env.kdir, 0o555)
    try:
        code, _out, err = run(capsys, "install", "--force")
    finally:
        os.chmod(env.kdir, 0o755)
    assert code == 4
    assert "did not land" in err and "nothing replaced" in err
    assert "Traceback" not in err
    assert env.script.read_bytes() == edited


def test_a_backup_that_reads_back_different_replaces_nothing(env, capsys, monkeypatch):
    """The backup is read back before its original is replaced (design 4.3)."""
    def short_copy(path, dest, data):
        dest.write_bytes(data[:-1])
    monkeypatch.setattr(sl, "_copy_for_backup", short_copy)
    env.write_settings({"statusLine": OURS})
    edited = sl.stamped() + b"# my edit\n"
    env.script.write_bytes(edited)
    code, _out, err = run(capsys, "install", "--force")
    assert code == 4 and "reads back different" in err and "nothing replaced" in err
    assert env.script.read_bytes() == edited


def test_a_settings_write_that_fails_is_finished_by_the_next_run(env, capsys, monkeypatch):
    def fails(path, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(sl, "_write_settings", fails)
    code, _out, err = run(capsys, "install")
    assert code == 4
    assert str(env.settings) in err and "No space left" in err
    assert "Traceback" not in err
    assert env.script.read_bytes() == sl.stamped()     # the script landed first
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(sl, "MANAGED_DIRS", {sys.platform: env.managed})
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state F" in out
    assert env.read_settings() == {"_about": sl.ABOUT, "statusLine": OURS}


# --- K as a git repo (dotfiles): left modified ---------------------------------------------

def _git(cwd: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(cwd), *HOOKS_OFF, *args], check=True,
                          capture_output=True, env=hermetic_env()).stdout   # ST-161


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True,
                                   check=False).returncode != 0,
                    reason="git is not installed")
def test_a_git_repo_kit_dir_is_left_modified_never_committed(env, capsys):
    """A kit dir kept in dotfiles: statusline.sh committed, then staged AND
    modified again (MM), and an unrelated staged file. HEAD and the index are
    byte-identical after a forced install."""
    env.kdir.mkdir(parents=True)
    _git(env.kdir, "init", "-q")
    _git(env.kdir, "config", "user.email", "t@example.invalid")
    _git(env.kdir, "config", "user.name", "t")
    env.script.write_bytes(FOREIGN)
    env.write_settings({"model": "opus"})
    _git(env.kdir, "add", "statusline.sh", "claude-code.d/50-statusline.json")
    _git(env.kdir, "commit", "-q", "-m", "base")
    env.script.write_bytes(FOREIGN + b"# staged\n")
    (env.kdir / "NOTES.md").write_text("unrelated staged work\n")
    _git(env.kdir, "add", "statusline.sh", "NOTES.md")
    env.script.write_bytes(FOREIGN + b"# staged\n# and modified\n")
    head = _git(env.kdir, "rev-parse", "HEAD")
    index = (env.kdir / ".git" / "index").read_bytes()

    code, out, _err = run(capsys, "install", "--force")
    assert code == 0
    assert _git(env.kdir, "rev-parse", "HEAD") == head
    assert (env.kdir / ".git" / "index").read_bytes() == index
    assert (f"{env.kdir} is a git repo: statusline.sh and 50-statusline.json are now "
            "modified there") in out
    assert "(and 1 backup untracked)" in out and "Nothing was committed." in out


# --- check: the kit layer, read as session start reads it (design section 6) ----------------

def test_check_pristine_current(env, capsys):
    run(capsys, "install")
    code, out, _err = run(capsys, "check")
    assert code == 0
    assert f"session statusline check: {env.kdir}" in out
    assert f"statusLine = {json.dumps(OURS)} (ours), set in {env.settings}" in out
    assert f"script: {env.script}: pristine" in out and "current" in out
    assert ("a --settings typed on a launch's command line is folded on top of this (its keys "
            "win, except a key a lower file locks with _locked); one in the agent file's "
            "claude: section gets none of this; an agent, seat or session layer may override "
            "it: session settings SID claude-code shows a seat's") in out
    assert "the line also needs workspace trust" in out


def test_check_an_older_copy_says_install_upgrades(env, capsys):
    env.write_settings({"statusLine": OURS})
    env.script.write_bytes(pristine_other("0.0.1"))
    code, out, _err = run(capsys, "check")
    assert code == 0 and "sessiontools 0.0.1" in out and "upgrades it" in out


def test_check_edited_and_diff_shows_the_edit(env, capsys):
    """check --diff reads the kit's copy (design review F6)."""
    run(capsys, "install")
    text = env.script.read_text().replace('agent_part="Claude"', 'agent_part="Alice"')
    env.script.write_text(text)
    code, out, _err = run(capsys, "check")
    assert code == 0 and "edited" in out and "check --diff" in out
    code, out, _err = run(capsys, "check", "--diff")
    assert '-  agent_part="Claude"' in out and '+  agent_part="Alice"' in out
    assert f"+++ {env.script} (stamp line dropped)" in out
    assert "sessiontools-statusline" not in out.split("+++")[1]   # the stamp is not an edit


def test_check_diff_with_no_edits_says_so(env, capsys):
    run(capsys, "install")
    _code, out, _err = run(capsys, "check", "--diff")
    assert "no edits" in out


def test_check_not_ours(env, capsys, monkeypatch):
    monkeypatch.setitem(sl.KNOWN, sl.sha(FOREIGN), "an earlier hand copy of this status line (test)")
    env.write_settings({"statusLine": OURS})
    env.script.write_bytes(FOREIGN)
    code, out, _err = run(capsys, "check")
    assert code == 1 and "not ours" in out and "earlier hand copy" in out


def test_check_nothing_installed(env, capsys):
    code, out, _err = run(capsys, "check")
    assert code == 1
    assert f"no kit file sets statusLine ({env.settings} does not exist)" in out
    assert f"script: {env.script} does not exist" in out


def test_check_another_entry(env, capsys):
    env.write_settings({"statusLine": "echo hi"})
    code, out, _err = run(capsys, "check")
    assert code == 1 and f'"echo hi" (not ours), set in {env.settings}' in out


@pytest.mark.parametrize("name", ["claude-code.d/50-statusline.json", "claude-code.json"])
def test_check_unreadable_kit_settings(env, capsys, name):
    bad = env.kdir / name
    bad.parent.mkdir(parents=True)
    bad.write_text("{nope")
    code, out, _err = run(capsys, "check")
    assert code == 5 and f"{bad} is unreadable" in out


def test_check_says_which_file_wins_and_what_still_applies(env, capsys):
    """After install --force over theirs in claude-code.json: ours wins by
    order, their refreshInterval still applies, and check says both."""
    theirs = env.write_kit("claude-code.json", {"statusLine": MINE})
    run(capsys, "install", "--force")
    early = env.write_kit("claude-code.d/10-pad.json", {"statusLine": {"padding": 3}})
    code, out, _err = run(capsys, "check")
    assert code == 0
    assert f"(ours), set in {env.settings}" in out
    assert f"settings: statusLine.refreshInterval comes from {theirs}" in out
    assert f"settings: {early} also sets statusLine; none of it applies" in out   # D's padding wins
    assert f"{theirs} also sets" not in out


def test_check_a_later_drop_in_that_wins_is_not_healthy(env, capsys):
    run(capsys, "install")
    late = env.write_kit("claude-code.d/60-mine.json", {"statusLine": MINE})
    code, out, _err = run(capsys, "check")
    assert code == 1
    assert f"(not ours), set in {late}, which is read after 50-statusline.json, so it wins" in out


def test_check_our_entry_in_another_kit_file_is_healthy(env, capsys):
    """design review F5, from check's side."""
    env.write_kit("claude-code.json", {"statusLine": OURS})
    env.script.write_bytes(sl.stamped())
    code, out, _err = run(capsys, "check")
    assert code == 0 and f"(ours), set in {env.kdir / 'claude-code.json'}" in out


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_check_reports_a_project_status_line_without_failing(env, capsys, name):
    """A project statusLine wins only for a bare claude: a session start
    seat's --settings file outranks it (design section 5)."""
    run(capsys, "install")
    proj = env.work / ".claude"
    proj.mkdir()
    (proj / name).write_text('{"statusLine": {"type": "command", "command": "echo proj"}}')
    code, out, _err = run(capsys, "check")
    assert code == 0
    assert (f"project: {proj / name} sets statusLine: it wins over ~/.claude's for a bare claude "
            "here; a session start seat shows the kit's") in out


def test_check_a_project_disable_all_hooks_blocks(env, capsys):
    run(capsys, "install")
    proj = env.work / ".claude"
    proj.mkdir()
    (proj / "settings.json").write_text('{"disableAllHooks": true}')
    code, out, _err = run(capsys, "check")
    assert code == 1 and f"project: {proj / 'settings.json'} sets disableAllHooks" in out


def test_check_reports_allow_managed_hooks_only(env, capsys):
    run(capsys, "install")
    env.managed.mkdir()
    (env.managed / "managed-settings.json").write_text('{"allowManagedHooksOnly": true}')
    code, out, _err = run(capsys, "check")
    assert code == 1
    assert "allowManagedHooksOnly" in out and "does not run on this machine" in out


def test_check_reads_managed_drop_ins(env, capsys):
    run(capsys, "install")
    drop = env.managed / "managed-settings.d"
    drop.mkdir(parents=True)
    (drop / "20-line.json").write_text('{"statusLine": {"type": "command", "command": "corp"}}')
    (drop / ".hidden.json").write_text('{"allowManagedHooksOnly": true}')
    (drop / "notes.txt").write_text('{"allowManagedHooksOnly": true}')
    code, out, _err = run(capsys, "check")
    assert code == 1
    assert "20-line.json sets statusLine, which wins" in out
    assert "allowManagedHooksOnly" not in out


def test_check_disable_all_hooks_in_the_kit_file_is_not_a_blocker(env, capsys):
    """The drop-in is not claude's settings: only statusLine is rendered."""
    run(capsys, "install")
    data = env.read_settings()
    data["disableAllHooks"] = True
    env.write_settings(data)
    code, out, _err = run(capsys, "check")
    assert code == 0 and "disableAllHooks" not in out


def test_check_writes_nothing(env, capsys):
    run(capsys, "install")
    seed_claude(env)
    env.script.write_bytes(env.script.read_bytes() + b"# edit\n")
    before = snapshot(env.home)
    run(capsys, "check", "--diff")
    assert snapshot(env.home) == before


# --- the ~/.claude report: both verbs, read only (design section 6; SC4) --------------------

def _claude_lines(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines() if line.strip().startswith("claude: ")]


@pytest.mark.parametrize("verb", [("install",), ("install", "--dry-run"), ("check",)])
def test_an_st136_install_in_claude_is_reported_and_left(env, capsys, verb):
    """SC4: the statusLine there is named with its value and with what wins
    where; the script is judged; the dir is byte-identical afterwards."""
    seed_claude(env)
    before = snapshot(env.cdir)
    code, out, _err = run(capsys, *verb)
    assert code == (1 if verb == ("check",) else 0)       # check: nothing in the kit yet
    assert _claude_lines(out) == [
        (f"claude: {env.claude_settings} sets statusLine {json.dumps(OURS_CLAUDE)} (sessiontools' "
         "own, from an install before the kit layer): a session start seat shows the kit's; a "
         "bare claude shows this one. Left as it is."),
        (f"claude: {env.claude_script}: pristine sessiontools {sl.version()}, from an install "
         "before the kit layer. Left as it is."),
    ]
    assert snapshot(env.cdir) == before


def test_the_report_runs_on_a_refusal_too(env, capsys):
    env.write_claude_settings({"statusLine": "echo mine"})
    env.write_settings({"statusLine": "echo hi"})
    code, out, _err = run(capsys, "install")
    assert code == 3
    assert _claude_lines(out) == [
        (f'claude: {env.claude_settings} sets statusLine "echo mine": a session start seat shows '
         "the kit's; a bare claude shows this one. Left as it is.")]


def test_a_known_hand_copy_in_claude_is_named_by_its_label(env, capsys, monkeypatch):
    monkeypatch.setitem(sl.KNOWN, sl.sha(FOREIGN), "an earlier hand copy (test label)")
    env.cdir.mkdir()
    env.claude_script.write_bytes(FOREIGN)
    _code, out, _err = run(capsys, "check")
    assert f"claude: {env.claude_script}: an earlier hand copy (test label). Left as it is." in out


@pytest.mark.parametrize("data,line", [
    (sl.stamped() + b"# mine\n", "edited (from sessiontools {v})"),
    (b"#!/bin/sh\necho x\n", "not ours"),
])
def test_the_claude_script_is_judged(env, capsys, data, line):
    env.cdir.mkdir()
    env.claude_script.write_bytes(data)
    _code, out, _err = run(capsys, "check")
    assert f"claude: {env.claude_script}: {line.format(v=sl.version())}. Left as it is." in out


def test_a_claude_script_link_is_named_and_left(env, capsys):
    env.cdir.mkdir()
    env.claude_script.symlink_to(env.root / "elsewhere.sh")
    _code, out, _err = run(capsys, "install")
    assert (f"claude: {env.claude_script}: a symlink to {env.root / 'elsewhere.sh'}. "
            "Left as it is.") in out
    assert env.claude_script.is_symlink()


def test_an_unreadable_claude_settings_is_said_and_never_blocks_install(env, capsys):
    env.cdir.mkdir()
    env.claude_settings.write_text("{nope")
    code, out, _err = run(capsys, "install")
    assert code == 0
    assert f"claude: {env.claude_settings} is unreadable (not JSON" in out
    assert "not ours to fix" in out
    assert env.claude_settings.read_text() == "{nope"


def test_claude_disable_all_hooks_blocks_check_but_not_install(env, capsys):
    env.write_claude_settings({"disableAllHooks": True})
    assert run(capsys, "install")[0] == 0
    code, out, _err = run(capsys, "check")
    assert code == 1
    assert (f"claude: {env.claude_settings} sets disableAllHooks: no status line runs, a session "
            "start seat's included") in out


@pytest.mark.parametrize("verb", [("install",), ("check",)])
def test_claude_code_s_own_status_line_is_not_called_ours(env, capsys, verb):
    """Claude Code's /statusline and its docs write the same entry an ST-136
    install did, `~/.claude/statusline.sh`, with a script of the person's own.
    The entry alone does not make it sessiontools': the script must carry our
    stamp (do-003 review)."""
    env.write_claude_settings({"statusLine": OURS_CLAUDE})
    env.claude_script.write_bytes(FOREIGN)
    before = snapshot(env.cdir)
    _code, out, _err = run(capsys, *verb)
    assert _claude_lines(out) == [
        (f"claude: {env.claude_settings} sets statusLine {json.dumps(OURS_CLAUDE)}: a session "
         "start seat shows the kit's; a bare claude shows this one. Left as it is."),
        f"claude: {env.claude_script}: not ours. Left as it is.",
    ]
    assert "sessiontools' own" not in out
    assert snapshot(env.cdir) == before


def test_a_claude_status_line_never_fails_check(env, capsys):
    run(capsys, "install")
    env.write_claude_settings({"statusLine": "echo mine"})
    assert run(capsys, "check")[0] == 0


def test_claude_config_dir_is_the_dir_reported(env, capsys, monkeypatch):
    """A bare claude reads $CLAUDE_CONFIG_DIR, not ~/.claude: that is the
    dir reported, and ~/.claude is not."""
    seed_claude(env)
    cfg = env.root / "cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text('{"statusLine": "echo cfg"}')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _code, out, _err = run(capsys, "check")
    assert _claude_lines(out) == [
        (f'claude: {cfg / "settings.json"} sets statusLine "echo cfg": a session start seat shows '
         "the kit's; a bare claude shows this one. Left as it is.")]


def test_nothing_in_claude_says_nothing(env, capsys):
    _code, out, _err = run(capsys, "check")
    assert _claude_lines(out) == []
    assert not env.cdir.exists()


# --- sample and the command line -----------------------------------------------------------

def test_sample_prints_the_sample_payload(capsys):
    code, out, _err = run(capsys, "sample")
    assert code == 0
    assert out == sl.SAMPLE.read_text()
    assert json.loads(out)["model"]["display_name"] == "Opus 5.5"


def test_bare_statusline_lists_the_three(capsys):
    code, out, _err = run(capsys)
    assert code == 0
    for sub in ("install", "check", "sample"):
        assert f"  {sub} " in out


def test_an_unknown_subcommand_is_usage(capsys):
    code, _out, err = run(capsys, "uninstall")
    assert code == 2 and "unknown subcommand 'uninstall'" in err


def test_session_lists_the_statusline_verb(capsys):
    cli.main([])
    assert "  statusline " in capsys.readouterr().out


# --- no network, lazy imports, timing --------------------------------------------------------

def test_install_and_check_make_no_network_call(env, capsys, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a network connect was attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    assert run(capsys, "install")[0] == 0
    seed_claude(env)                                  # check reads ~/.claude until do-003
    assert run(capsys, "check", "--diff")[0] == 0
    env.write_settings({"statusLine": "echo hi"})
    assert run(capsys, "install")[0] == 3            # the refusal renders the preview too


def _fresh_python(env: Env, code: str) -> subprocess.CompletedProcess:
    child = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    child["HOME"] = str(env.home)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=child, cwd=env.work, timeout=60, check=False)


@pytest.mark.parametrize("args,light", [
    (["check"], False), (["install", "--dry-run"], False), (["install"], False), (["sample"], True)])
def test_the_verb_imports_nothing_heavy(env, args, light):
    """Deterministic, so load cannot flake it: after the verb, no yaml and no
    jsonschema are loaded. sample loads no sessiontools.settings either;
    install and check do (ST-165: they read the kit layer with the cascade's
    own helpers, stdlib only, and resolve without the schema check)."""
    heavy = ("yaml", "jsonschema") + (("sessiontools.settings",) if light else ())
    got = _fresh_python(env, (
        "import io, contextlib, sys\n"
        "from sessiontools import cli\n"
        f"with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):\n"
        f"    cli.main(['statusline', *{args!r}])\n"
        f"print(sorted(m for m in {heavy!r} if m in sys.modules))\n"))
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "[]"


def test_check_is_fast(env):
    """median of 3 under 0.5 s; generous on purpose (load guard 14)."""
    child = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    child["HOME"] = str(env.home)
    times = []
    for _ in range(3):
        start = time.monotonic()
        got = subprocess.run([sys.executable, "-m", "sessiontools.cli", "statusline", "check"],
                             capture_output=True, env=child, cwd=env.work, timeout=60, check=False)
        times.append(time.monotonic() - start)
        assert got.returncode in (0, 1), got.stderr
    assert statistics.median(times) < 0.5, f"check took {times}"


# --- ST-165: the kit layer's own states (design.md sections 2, 3, 7) --------------------------

MINE = {"type": "command", "command": "~/mine.sh", "refreshInterval": 5}


def test_a_kit_dir_holding_only_the_script_is_no_host_layer(env, capsys):
    """design section 3: the reader ignores statusline.sh. K holding only
    the script is no host layer: no source is read from K, and the tool is
    not listed, so a run cut short after the script leaves nothing behind
    that the cascade reads; the next run writes the entry."""
    from sessiontools import settings
    env.kdir.mkdir(parents=True)
    env.script.write_bytes(sl.stamped())
    assert settings.host_dir("claude-code") == (env.kdir, None)
    assert settings.sources("claude-code", None) == []
    assert "claude-code" not in settings.tools("no-such-sid")
    assert "state F" in run(capsys, "install")[1]


@pytest.mark.parametrize("name", ["claude-code.json", "claude-code.template.json",
                                  "claude-code.d/10-mine.json"])
def test_O_before_D_refuses_and_names_the_file(env, capsys, name):
    theirs = env.write_kit(name, {"statusLine": MINE})
    before = snapshot(env.home)
    code, out, err = run(capsys, "install")
    assert code == 3 and "state O" in out
    assert f"inline in {theirs}: {json.dumps(MINE)}" in err
    assert f"`--force` sets ours in {env.settings}, which is read after {theirs}" in err
    assert snapshot(env.home) == before


def test_O_before_D_force_wins_by_order_and_says_what_still_applies(env, capsys):
    """design review F1: D wins only the sub-keys it sets. The read-back
    requires the resolved statusLine to CONTAIN D's entry, so a correct
    --force exits 0; the report names the sub-key still read from their file,
    which is never edited, backed up or touched."""
    theirs = env.write_kit("claude-code.json", {"model": "opus", "statusLine": MINE})
    their_bytes, their_stat = theirs.read_bytes(), os.stat(theirs)
    code, out, err = run(capsys, "install", "--force")
    assert (code, err) == (0, "")
    assert theirs.read_bytes() == their_bytes
    assert (os.stat(theirs).st_mtime_ns, os.stat(theirs).st_ino) == (their_stat.st_mtime_ns,
                                                                     their_stat.st_ino)
    assert backups(env) == []
    assert env.read_settings() == {"_about": sl.ABOUT, "statusLine": OURS}
    assert f"(was: {json.dumps(MINE)}, set in {theirs}, which is left as it is)" in out
    assert f"  statusLine.refreshInterval comes from {theirs}" in out
    assert (f"yours in {theirs} is still there; 50-statusline.json now wins over it "
            "(its refreshInterval still applies). Delete 50-statusline.json to get yours back."
            ) in out
    from sessiontools import settings
    resolved = settings.resolve("claude-code", None).values["statusLine"]
    assert resolved == {**OURS, "refreshInterval": 5}
    # ...and it is ours now: the next plain run is C, and still names the sub-key.
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state C" in out
    assert f"statusLine.refreshInterval comes from {theirs}" in out


@pytest.mark.parametrize("value", [MINE, None, OURS])
@pytest.mark.parametrize("force", [(), ("--force",)])
def test_O_after_D_refuses_even_under_force(env, capsys, value, force):
    """A drop-in read after D sets statusLine (even ours, even null): making
    ours win would mean editing their file. Refused, nothing written."""
    if value == OURS:
        value = {"type": "command", "command": "~/elsewhere/statusline.sh"}
    theirs = env.write_kit("claude-code.d/60-mine.json", {"statusLine": value})
    before = snapshot(env.home)
    code, out, err = run(capsys, "install", *force)
    assert code == 3 and "state O" in out
    assert (f"{theirs} sets statusLine and is read after 50-statusline.json, so it wins: "
            f"{json.dumps(value)}") in err
    assert err.rstrip().endswith("Nothing written. --force does not override this.")
    assert snapshot(env.home) == before


def test_F5_our_entry_hand_written_in_another_kit_file_is_ours(env, capsys):
    """design review F5: our entry in claude-code.json is ours, not "you've
    already got one". With the script there it is C; D is never created."""
    env.write_kit("claude-code.json", {"model": "opus", "statusLine": OURS})
    env.script.write_bytes(sl.stamped())
    os.chmod(env.script, 0o755)
    before = snapshot(env.home)
    code, out, err = run(capsys, "install")
    assert (code, err) == (0, "")
    assert "state C" in out
    assert f"note: statusLine (ours) is set in {env.kdir / 'claude-code.json'}, not " \
           "50-statusline.json; left there" in out
    assert snapshot(env.home) == before


def test_F5_with_no_script_writes_only_the_script(env, capsys):
    path = env.write_kit("claude-code.json", {"statusLine": OURS})
    settings_before = path.read_bytes()
    code, out, _err = run(capsys, "install")
    assert code == 0 and "state M" in out
    assert env.script.read_bytes() == sl.stamped()
    assert path.read_bytes() == settings_before
    assert not env.settings.exists()


@pytest.mark.parametrize("name", ["claude-code.json", "claude-code.template.json",
                                  "claude-code.d/70-late.json"])
@pytest.mark.parametrize("force", [(), ("--force",)])
def test_X_any_unreadable_kit_host_file_refuses(env, capsys, name, force):
    bad = env.kdir / name
    bad.parent.mkdir(parents=True)
    bad.write_text("{nope")
    before = snapshot(env.home)
    code, out, err = run(capsys, "install", *force)
    assert code == 5 and "state X" in out
    assert f"{bad} is unreadable (not JSON" in err
    assert snapshot(env.home) == before


def test_a_bad_lock_in_the_layer_is_X(env, capsys):
    env.write_kit("claude-code.template.json", {"_locked": "statusLine"})
    code, _out, err = run(capsys, "install")
    assert code == 5 and "_locked is not a list" in err


def test_read_back_of_the_layer_is_loud(env, capsys, monkeypatch):
    """D landed, but the layer does not resolve to it (a lock, a reader
    that lost it): exit 4, NOT installed."""
    from sessiontools import settings
    monkeypatch.setattr(settings, "sources", lambda *_a, **_k: [])
    code, _out, err = run(capsys, "install")
    assert code == 4
    assert "session start would resolve statusLine to nothing, not ours" in err
    assert "NOT installed" in err and "Traceback" not in err


@pytest.mark.parametrize("force", [(), ("--force",), ("--dry-run", "--force")])
def test_a_template_lock_that_keeps_theirs_is_refused_at_plan_time(env, capsys, force):
    """do-002 review N1: a lock read before D would keep D's entry from
    applying. The layer is merged at plan time as if D were written: refused,
    exit 3, --force included, nothing written (it used to write D and then
    fail the read-back, exit 4)."""
    tmpl = env.write_kit("claude-code.template.json", {"statusLine": MINE, "_locked": ["statusLine"]})
    before = snapshot(env.home)
    code, out, err = run(capsys, "install", *force)
    assert code == 3 and "state O" in out
    assert (f"{tmpl} locks statusLine, and is read before 50-statusline.json, so the "
            "statusLine install would set there would not be read") in err
    assert err.rstrip().endswith("Nothing written. --force does not override this.")
    assert "would show" not in err and "plan:" not in out
    assert snapshot(env.home) == before


@pytest.mark.parametrize("files", [
    {"claude-code.template.json": {"_locked": ["statusLine"]}},                  # E: no value
    {"claude-code.json": {"statusLine": {"padding": 3}, "_locked": ["statusLine.padding"]}},
    {"claude-code.d/10-lock.json": {"_locked": ["statusLine.command"]}},
])
def test_any_lock_read_before_D_that_drops_part_of_ours_is_refused(env, capsys, files):
    for name, data in files.items():
        env.write_kit(name, data)
    before = snapshot(env.home)
    code, _out, err = run(capsys, "install", "--force")
    assert code == 3 and " locks statusLine" in err
    assert snapshot(env.home) == before


@pytest.mark.parametrize("files", [
    {"claude-code.d/60-lock.json": {"_locked": ["statusLine"]}},                 # read after D
    {"claude-code.json": {"statusLine": {"padding": 1}, "_locked": ["statusLine.padding"]}},
    {"claude-code.json": {"_locked": ["model"], "model": "opus"}},               # another key
])
def test_a_lock_that_keeps_nothing_of_ours_out_is_no_refusal(env, capsys, files):
    """A lock read after D, a locked sub-key whose value is ours already,
    or a lock on another key: D's entry still applies, so install runs and
    the read-back passes (--force: a partial statusLine of theirs is O)."""
    for name, data in files.items():
        env.write_kit(name, data)
    code, out, err = run(capsys, "install", "--force")
    assert (code, err) == (0, ""), err
    assert "read back: both match" in out


# --- ST-165 R1: a person's own symlink never leads install into the config dir --------------

def _into_claude(env: Env, layout: str, target_root: Path) -> None:
    """Point K, claude-code.d, D or the script into TARGET_ROOT by a symlink."""
    target_root.mkdir(parents=True, exist_ok=True)
    if layout == "kit-dir":
        (target_root / "kit").mkdir()
        env.kdir.parent.mkdir(parents=True)
        env.kdir.symlink_to(target_root / "kit")
    elif layout == "drop-in-dir":
        (target_root / "drops").mkdir()
        env.kdir.mkdir(parents=True)
        (env.kdir / "claude-code.d").symlink_to(target_root / "drops")
    elif layout == "drop-in":
        (target_root / "mine.json").write_text('{"statusLine": "echo mine"}\n')
        env.settings.parent.mkdir(parents=True)
        env.settings.symlink_to(target_root / "mine.json")
    elif layout == "script":
        (target_root / "statusline.sh").write_bytes(FOREIGN)
        env.kdir.mkdir(parents=True)
        env.script.symlink_to(target_root / "statusline.sh")


@pytest.mark.parametrize("layout", ["kit-dir", "drop-in-dir", "drop-in", "script"])
@pytest.mark.parametrize("args", [(), ("--force",), ("--dry-run",), ("--dry-run", "--force")])
@pytest.mark.parametrize("config_dir", [False, True])
def test_a_symlink_into_the_config_dir_is_refused(env, capsys, monkeypatch, guard, layout, args,
                                                  config_dir):
    """do-002 review N2: K, claude-code.d, D or the script resolving inside
    ~/.claude (or $CLAUDE_CONFIG_DIR) is refused, exit 3, --force included.
    The guard records no write there and every byte, mode, mtime and inode
    under the whole scratch root is unchanged."""
    cfg = env.cdir
    if config_dir:
        cfg = env.root / "cfg"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _into_claude(env, layout, cfg)
    guard.watch(cfg)
    before = snapshot(env.root)
    code, out, err = run(capsys, "install", *args)
    assert code == 3, (out, err)
    assert f"inside {os.path.realpath(cfg)}, the dir a bare claude reads: install never writes there" in err
    assert err.rstrip().endswith("Nothing written. --force does not override this.")
    assert "plan:" not in out
    assert guard.hits == []
    assert snapshot(env.root) == before


def _case_alias(cfg: Path) -> Path | None:
    """cfg's name upper-cased, when this FS folds case (default APFS), else None."""
    alias = cfg.with_name(cfg.name.upper())
    return alias if os.path.exists(alias) and os.path.samefile(alias, cfg) else None


def _firmlink_alias(cfg: Path) -> Path | None:
    """cfg reached through macOS's /System/Volumes/Data firmlink, when it is the same dir."""
    alias = Path("/System/Volumes/Data" + os.path.realpath(cfg))
    try:
        return alias if os.path.samefile(alias, cfg) else None
    except OSError:
        return None


@pytest.mark.parametrize("route", ["case", "firmlink"])
@pytest.mark.parametrize("layout", ["kit-dir", "drop-in-dir", "drop-in", "script"])
@pytest.mark.parametrize("args", [(), ("--force",)])
def test_an_alias_of_the_config_dir_is_inside_it(env, capsys, guard, route, layout, args):
    """do-004 review D1: a symlink into ~/.claude by another NAME of the same
    dir (a case variant on a case-folding FS; /System/Volumes/Data/... on
    macOS) is inside it. Decided by identity, not by string: exit 3, the
    guard sees no write under either name, the whole root is unchanged."""
    env.cdir.mkdir()
    alias = (_case_alias if route == "case" else _firmlink_alias)(env.cdir)
    if alias is None:
        pytest.skip(f"no {route} alias of the config dir on this filesystem")
    _into_claude(env, layout, alias)
    guard.watch(env.cdir, alias)
    before = snapshot(env.root)
    code, out, err = run(capsys, "install", *args)
    assert code == 3, (out, err)
    assert f"inside {os.path.realpath(env.cdir)}, the dir a bare claude reads" in err
    assert err.rstrip().endswith("Nothing written. --force does not override this.")
    assert guard.hits == []
    assert snapshot(env.root) == before


def test_a_link_into_a_config_dir_not_made_yet_is_inside_it(env, capsys):
    """No ~/.claude yet, nothing to compare by identity: the realpath prefix
    still refuses a kit dir linked to where it would be."""
    env.kdir.parent.mkdir(parents=True)
    env.kdir.symlink_to(env.cdir / "kit")
    code, _out, err = run(capsys, "install", "--force")
    assert code == 3 and "the dir a bare claude reads" in err
    assert not env.cdir.exists()


def test_a_symlink_beside_the_config_dir_is_no_refusal(env, capsys):
    """Only INSIDE counts: a dotfiles dir named like it, ~/.claude-kit, is fine."""
    _into_claude(env, "kit-dir", env.home / ".claude-kit")
    code, _out, err = run(capsys, "install")
    assert (code, err) == (0, ""), err
    assert (env.home / ".claude-kit" / "kit" / "statusline.sh").is_file()


# --- ST-165 section 9: install never writes ~/.claude, proved twice -------------------------

#: Every write-mode call the guard wraps: (module, name, which args are paths).
_GUARDED = [
    (os, "replace", (0, 1)), (os, "rename", (0, 1)), (os, "symlink", (0, 1)), (os, "link", (0, 1)),
    (os, "unlink", (0,)), (os, "remove", (0,)), (os, "rmdir", (0,)), (os, "mkdir", (0,)),
    (os, "makedirs", (0,)), (os, "chmod", (0,)), (os, "utime", (0,)), (os, "truncate", (0,)),
    (shutil, "copymode", (0, 1)), (shutil, "copy2", (1,)), (shutil, "copyfile", (1,)),
    (shutil, "rmtree", (0,)),
]
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


class Guard:
    """Records, then refuses with PermissionError, every write-mode call whose
    target is under a watched dir. A target matches by its lexical absolute
    path AND by the realpath of the full target (design review F2: a
    symlinked ~/.claude/settings.json is written at its target, outside the
    dir), and every file a watched dir links to is watched too.

    A subprocess is not seen: `preview()` runs the shipped script with TAP
    and LOG off on a refusal (design review F3). The byte+mtime+inode
    snapshot is what covers it."""

    def __init__(self):
        self.hits: list[tuple[str, str]] = []
        self.dirs: set[str] = set()
        self.files: set[str] = set()

    def watch(self, *roots: Path) -> None:
        for root in roots:
            for p in (os.path.abspath(root), os.path.realpath(root)):
                self.dirs.add(p)
            for dirpath, dirnames, filenames in os.walk(root):
                for name in dirnames + filenames:
                    real = os.path.realpath(Path(dirpath) / name)
                    (self.dirs if os.path.isdir(real) else self.files).add(real)

    def matches(self, target) -> bool:
        if isinstance(target, int) or target is None:
            return False
        t = os.fsdecode(os.fspath(target))
        for cand in (os.path.abspath(t), os.path.realpath(t)):
            if cand in self.files or any(cand == d or cand.startswith(d + os.sep) for d in self.dirs):
                return True
        return False

    def check(self, call: str, *targets) -> None:
        for t in targets:
            if self.matches(t):
                self.hits.append((call, os.fsdecode(os.fspath(t))))
                raise PermissionError(f"guard: {call} on {t}")


@pytest.fixture
def guard(monkeypatch):
    import builtins
    import io
    g = Guard()
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file, mode="r", *a, **k):
        if any(c in mode for c in "wax+"):
            g.check("open", file)
        return real_open(file, mode, *a, **k)

    def guarded_os_open(path, flags, *a, **k):
        if flags & _WRITE_FLAGS:
            g.check("os.open", path)
        return real_os_open(path, flags, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    for mod, name, which in _GUARDED:
        real = getattr(mod, name)

        def wrapped(*a, _real=real, _name=f"{mod.__name__}.{name}", _which=which, **k):
            g.check(_name, *(a[i] for i in _which if i < len(a)))
            return _real(*a, **k)
        monkeypatch.setattr(mod, name, wrapped)
    return g


def _seed(where: Path, kind: str, e: Env) -> list[Path]:
    """A config dir as a person might have it. Returns any extra dir to snapshot."""
    where.mkdir(parents=True, exist_ok=True)
    settings = where / "settings.json"
    if kind == "no-entry":
        settings.write_text('{"model": "opus", "hooks": {}}\n')
    elif kind == "st136":
        settings.write_text(json.dumps({"statusLine": {"type": "command",
                                                       "command": str(where / "statusline.sh")}}))
        (where / "statusline.sh").write_bytes(sl.stamped())
    elif kind == "known":
        settings.write_text('{"statusLine": {"type": "command", "command": "echo mine"}}\n')
        (where / "statusline.sh").write_bytes(FOREIGN)      # a KNOWN hand copy (patched in)
    elif kind == "linked":
        dotfiles = e.root / f"dotfiles-{where.name}"
        dotfiles.mkdir()
        (dotfiles / "settings.json").write_text('{"statusLine": "echo linked"}\n')
        settings.symlink_to(dotfiles / "settings.json")
        return [dotfiles]
    return []


@pytest.mark.parametrize("kind", ["no-entry", "st136", "known", "linked"])
@pytest.mark.parametrize("config_dir", [False, True])
def test_install_never_writes_the_claude_config_dir(env, capsys, monkeypatch, guard, kind,
                                                    config_dir):
    """SC1, SC2 (design section 9). ~/.claude is seeded; with config_dir,
    CLAUDE_CONFIG_DIR names a second scratch dir, seeded the same way. Every
    verb runs: install --dry-run, install, a second install, --force, check,
    and a refusal (which runs the preview subprocess). The guard records no
    write-mode call under either dir or at a link's target, and every byte,
    mode, mtime and inode there, the dirs' own included, is unchanged.
    Against 7fcba78's install the guard records tempfile.mkstemp's os.open
    in ~/.claude."""
    monkeypatch.setitem(sl.KNOWN, sl.sha(FOREIGN), "an earlier hand copy of this status line (test)")
    watched = [env.cdir, *_seed(env.cdir, kind, env)]
    if config_dir:
        cfg = env.root / "cfg"
        watched += [cfg, *_seed(cfg, kind, env)]
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    guard.watch(*watched)
    before = {str(w): snapshot(w) for w in watched}

    results = [run(capsys, "install", "--dry-run")[0], run(capsys, "install")[0]]
    assert guard.hits == []
    results += [run(capsys, "install")[0], run(capsys, "install", "--force")[0]]
    run(capsys, "check")
    env.write_settings({"statusLine": "echo hi"})          # a refusal: the preview runs
    results.append(run(capsys, "install")[0])

    assert guard.hits == []
    assert {str(w): snapshot(w) for w in watched} == before
    assert results == [0, 0, 0, 0, 3]
    assert env.script.read_bytes() == sl.stamped()


def test_the_guard_bites(env, guard):
    """The guard is not vacuous: a write at the target of a linked
    settings.json, or a temp file in ~/.claude, is caught and refused."""
    import tempfile
    (dotfiles,) = _seed(env.cdir, "linked", env)
    guard.watch(env.cdir)
    with pytest.raises(PermissionError), open(dotfiles / "settings.json", "w") as f:
        f.write("{}")
    with pytest.raises(PermissionError):
        tempfile.mkstemp(dir=env.cdir)
    with pytest.raises(PermissionError):
        os.replace(env.root / "nowhere", env.cdir / "settings.json")
    assert [h[0] for h in guard.hits] == ["open", "os.open", "os.replace"]


# --- SC8: the docs never send anyone into ~/.claude, and state the limit --------------------

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "docs/status-line-script.md", "docs/session-start-launcher.md")
#: A shell write aimed at the config dir, in prose or a code block.
SHELL_WRITE = re.compile(r"(?:\b(?:cp|mv|ln|tee|touch|mkdir|install|vi|vim|nano|sed -i)\b|>>?)"
                         r"[^\n`]*(?:~/\.claude|\$CLAUDE_CONFIG_DIR)")
_ACTS = r"(?:edit|add|put|set|write|copy|install|change|create|paste|point|delete|remove|run)"
#: An instruction: a sentence (or list item) that opens with an imperative
#: verb, or tells "you" to; or one that says something copies, writes,
#: installs or puts a file there.
IMPERATIVE = re.compile(rf"^(?:[-*]\s+|\d+\.\s+)?(?:\*\*)?(?:then |or |and )?{_ACTS}\b"
                        rf"|\byou (?:can |should |must |may |need to ){_ACTS}\b", re.IGNORECASE)
WRITES_THERE = re.compile(r"\b(?:copies|writes|installs|puts|points)\b[^.]*"
                          r"(?:~/\.claude|\$CLAUDE_CONFIG_DIR)")
NEGATED = re.compile(r"\b(?:never|not|no|nothing|left as it is|read only|reported?)\b|n't\b",
                     re.IGNORECASE)
CLAUDE_DIR = re.compile(r"~/\.claude|\$CLAUDE_CONFIG_DIR")


def _sends_into_claude(sentence: str) -> bool:
    return bool(CLAUDE_DIR.search(sentence) and not NEGATED.search(sentence)
                and (IMPERATIVE.search(sentence) or WRITES_THERE.search(sentence)))


def _prose_sentences(text: str) -> list[str]:
    """Sentences outside code blocks: each heading, list item or paragraph
    is a block, and a block splits at a sentence end."""
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    blocks = re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |\d+\. |#|\|))", prose)
    out = []
    for block in blocks:
        flat = re.sub(r"\s+", " ", block).strip()
        out += [x for x in re.split(r"(?<=[.!?])\s+(?=[A-Z*`(\[])", flat) if x]
    return out


@pytest.mark.parametrize("name", DOCS)
def test_no_doc_says_to_edit_or_install_into_claude(name):
    """SC8 (R4): no sentence that tells a reader to write, edit or install
    into ~/.claude (or $CLAUDE_CONFIG_DIR) without saying never/not, and no
    shell command that writes there."""
    text = (ROOT / name).read_text(encoding="utf-8")
    assert not SHELL_WRITE.findall(text)
    assert [s for s in _prose_sentences(text) if _sends_into_claude(s)] == []


@pytest.mark.parametrize("name", DOCS)
def test_every_doc_states_the_bare_claude_limit_and_the_alias(name):
    text = re.sub(r"\s+", " ", (ROOT / name).read_text(encoding="utf-8"))
    assert "alias claude='session start -'" in text
    assert re.search(r"bare `claude`[^.]*\b(?:does not|doesn't)\b", text) or \
        re.search(r"Only a seat launched by `session start` gets the kit's line", text)


def test_the_sc8_checks_bite():
    """The two checks above catch the lines the docs held before ST-165."""
    assert SHELL_WRITE.findall("cp statusline.sh ~/.claude/statusline.sh")
    old = ("It copies the script to `~/.claude/statusline.sh` (or into "
           "`$CLAUDE_CONFIG_DIR`), points `statusLine` in `settings.json` at it, and reads "
           "both back. Delete the `statusLine` key from `settings.json`, then delete "
           "`~/.claude/statusline.sh`.")
    flagged = [s for s in _prose_sentences(old) if _sends_into_claude(s)]
    assert [f.split()[0] for f in flagged] == ["It", "Delete"]
    assert _sends_into_claude("- You can edit `~/.claude/settings.json` to add it.")
    assert not _sends_into_claude("Never edit `~/.claude` to get it.")
    assert not _sends_into_claude("A bare `claude` reads `~/.claude/settings.json`.")
