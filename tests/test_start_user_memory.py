"""The user's CLAUDE.md, carried when a launch's sources leave `user` out
(ST-302 do-001): the sources scan, the path, the quiet no-ops, the off
flags, the `@` imports it names and the block it renders.

Unit tests only. Every HOME is a temp dir built here (and the conftest walls
the process's own HOME off besides): the real `~/.claude/` is never read,
written or named.
"""

from __future__ import annotations

import builtins
import io
import os
import pathlib
from pathlib import Path

import pytest

from sessiontools import argv_flags, compact_point
from sessiontools.start import user_memory as um
from sessiontools.start.vendor import VENDORS, vendor_doors

CLAUDE = VENDORS["claude"]
SOURCES = "--setting-sources"
MEMORY = "# Mike's rules\n\nSay what you are doing.\n"


def home_with(tmp_path: Path, text: str | bytes | None = MEMORY,
              config_dir: str = ".claude") -> Path:
    """A temp HOME, with `<config_dir>/CLAUDE.md` holding TEXT (none: absent)."""
    home = tmp_path / "home"
    (home / config_dir).mkdir(parents=True, exist_ok=True)
    if text is not None:
        path = home / config_dir / "CLAUDE.md"
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return home


# ---------- the shared last-wins scan ------------------------------------------

def test_the_flag_scan_has_one_definition():
    """Lifted out of compact_point, not copied: the settle hook and session
    start read a vendor flag by one rule."""
    assert compact_point._flag is argv_flags.last_value


@pytest.mark.parametrize(("argv", "value"), [
    ([], None),
    (["--x", "a"], "a"),
    (["--x=a"], "a"),
    (["--x", "a", "--x=b"], "b"),
    (["--x=a", "--x", "b"], "b"),
    (["--x="], ""),
    (["--x"], None),                       # a trailing flag sets nothing
    (["--x-longer", "a"], None),            # a longer flag is not this one
    (["--x-longer=a"], None),
])
def test_last_value(argv, value):
    assert argv_flags.last_value(argv, "--x") == value


# ---------- the sources scan ----------------------------------------------------

@pytest.mark.parametrize(("argv", "loads"), [
    ([], True),                                          # absent: all three sources
    ([SOURCES, ""], False),                              # Cora's, project agents
    ([SOURCES, "project,local"], False),                 # tm kick, tm task work
    ([SOURCES, "user"], True),
    ([SOURCES, "user,project"], True),
    ([f"{SOURCES}=user"], True),
    ([f"{SOURCES}="], False),
    ([f"{SOURCES}=project"], False),
    ([SOURCES, "user", SOURCES, "project,local"], False),  # the last wins
    ([SOURCES, "", f"{SOURCES}=user,local"], True),
    ([SOURCES, "project, user"], True),                  # spaces in the list
    ([SOURCES, " user , local "], True),
    ([SOURCES, "users"], False),                         # a name, not a substring
    (["--model", "opus", SOURCES, "local", "--effort", "high"], False),
])
def test_vendor_loads_user_memory(argv, loads):
    assert um.vendor_loads_user_memory(CLAUDE, argv) is loads


def test_a_row_without_the_field_has_nothing_to_add():
    assert um.vendor_loads_user_memory({"cmd": "acme"}, [SOURCES, ""]) is True


# ---------- the path ------------------------------------------------------------

def test_the_path_is_home_dot_claude(tmp_path):
    assert um.user_memory_path(CLAUDE, {"HOME": str(tmp_path)}) == \
        tmp_path / ".claude" / "CLAUDE.md"


def test_claude_config_dir_moves_it(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}
    assert um.user_memory_path(CLAUDE, env) == tmp_path / "cfg" / "CLAUDE.md"


@pytest.mark.parametrize("value", ["~", "~/cfg"])
def test_a_tilde_in_claude_config_dir_is_the_launch_home(tmp_path, value):
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": value}
    assert um.user_memory_path(CLAUDE, env) == \
        Path(str(tmp_path) + value[1:]) / "CLAUDE.md"


def test_an_empty_claude_config_dir_falls_back_to_home(tmp_path):
    env = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": ""}
    assert um.user_memory_path(CLAUDE, env) == tmp_path / ".claude" / "CLAUDE.md"


def test_a_declared_variable_moves_it(tmp_path):
    """The agent file's `environment: variables:` lay over the launcher's
    own environ: a declared CLAUDE_CONFIG_DIR moves the vendor's file, so
    it moves ours."""
    launcher = {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(tmp_path / "mine")}
    env = um.vendor_environ(launcher, {"CLAUDE_CONFIG_DIR": str(tmp_path / "declared")})
    assert um.user_memory_path(CLAUDE, env) == tmp_path / "declared" / "CLAUDE.md"
    assert launcher["CLAUDE_CONFIG_DIR"] == str(tmp_path / "mine")   # not mutated


def test_a_declared_home_moves_it(tmp_path):
    env = um.vendor_environ({"HOME": "/nonexistent"}, {"HOME": str(tmp_path)})
    assert um.user_memory_path(CLAUDE, env) == tmp_path / ".claude" / "CLAUDE.md"


def test_no_variables_is_the_launcher_environ(tmp_path):
    assert um.vendor_environ({"HOME": str(tmp_path)}) == {"HOME": str(tmp_path)}


def test_a_row_without_the_field_has_no_path(tmp_path):
    assert um.user_memory_path({"cmd": "acme"}, {"HOME": str(tmp_path)}) is None


def test_carried_from_a_declared_config_dir(tmp_path):
    home = home_with(tmp_path, config_dir="declared")
    env = um.vendor_environ({"HOME": str(home)},
                            {"CLAUDE_CONFIG_DIR": str(home / "declared")})
    memory = um.user_memory_block("claude", [SOURCES, ""], env)
    assert memory is not None and memory.path == home / "declared" / "CLAUDE.md"


# ---------- when it is carried --------------------------------------------------

@pytest.mark.parametrize("argv", [
    [SOURCES, ""], [SOURCES, "project,local"], [f"{SOURCES}=project"],
    [SOURCES, "user", SOURCES, "local"],
])
def test_carried_when_the_sources_leave_user_out(tmp_path, argv):
    home = home_with(tmp_path)
    memory = um.user_memory_block("claude", argv, {"HOME": str(home)})
    assert memory == um.UserMemory(path=home / ".claude" / "CLAUDE.md", text=MEMORY,
                                   imports=(), sources_flag=SOURCES, user_source="user")


@pytest.mark.parametrize("argv", [[], [SOURCES, "user"], [f"{SOURCES}=user,project"],
                                  [SOURCES, "", SOURCES, "local, user"]])
def test_never_twice_when_the_vendor_loads_it(tmp_path, argv):
    """R3: no sources flag, or `user` in the last one: the vendor loads it."""
    home = home_with(tmp_path)
    assert um.user_memory_block("claude", argv, {"HOME": str(home)}) is None


@pytest.mark.parametrize("flag", ["--bare", "--safe-mode", "--restricted"])
def test_an_off_flag_carries_nothing(tmp_path, flag):
    home = home_with(tmp_path)
    for argv in ([SOURCES, "", flag], [flag, SOURCES, "project,local"],
                 [SOURCES, "", f"{flag}=true"]):
        assert um.user_memory_block("claude", argv, {"HOME": str(home)}) is None, argv


def test_the_off_flags_are_the_rows(tmp_path):
    assert CLAUDE["user_memory"]["off_flags"] == ("--bare", "--safe-mode", "--restricted")


def test_a_vendor_without_the_field_carries_nothing(tmp_path, monkeypatch):
    home = home_with(tmp_path)
    row = {key: value for key, value in CLAUDE.items() if key != "user_memory"}
    monkeypatch.setitem(VENDORS, "acme", row)
    assert um.user_memory_block("acme", [SOURCES, ""], {"HOME": str(home)}) is None
    assert um.user_memory_block("nonesuch", [SOURCES, ""], {"HOME": str(home)}) is None


# ---------- R4: a file it cannot use is a quiet no-op ----------------------------

def test_missing_is_none(tmp_path):
    home = home_with(tmp_path, text=None)
    assert um.user_memory_block("claude", [SOURCES, ""], {"HOME": str(home)}) is None


def test_no_config_dir_at_all_is_none(tmp_path):
    assert um.user_memory_block("claude", [SOURCES, ""],
                                {"HOME": str(tmp_path / "nowhere")}) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_unreadable_is_none(tmp_path):
    home = home_with(tmp_path)
    path = home / ".claude" / "CLAUDE.md"
    path.chmod(0)
    try:
        assert um.user_memory_block("claude", [SOURCES, ""], {"HOME": str(home)}) is None
    finally:
        path.chmod(0o600)


def test_not_utf8_is_none(tmp_path):
    home = home_with(tmp_path, text=b"caf\xe9 \xff\xfe\n")
    assert um.user_memory_block("claude", [SOURCES, ""], {"HOME": str(home)}) is None


@pytest.mark.parametrize("text", ["", "\n", "  \n\t\n"])
def test_empty_is_none(tmp_path, text):
    home = home_with(tmp_path, text=text)
    assert um.user_memory_block("claude", [SOURCES, ""], {"HOME": str(home)}) is None


def test_a_directory_in_its_place_is_none(tmp_path):
    home = home_with(tmp_path, text=None)
    (home / ".claude" / "CLAUDE.md").mkdir()
    assert um.user_memory_block("claude", [SOURCES, ""], {"HOME": str(home)}) is None


# ---------- the `@` imports: named, never read ----------------------------------

def test_imports_are_named_in_order_once():
    text = ("See @extra.md first.\n@~/notes.md\nand @extra.md again,\n"
            "mail me at mike@example.com, or say @ alone.\n")
    assert um.find_imports(text) == ("extra.md", "~/notes.md")


def test_an_import_in_an_inline_code_span_is_not_named():
    text = "Run `@inline.md` or ``x @double.md y`` but read @real.md\n"
    assert um.find_imports(text) == ("real.md",)


def test_an_import_in_a_fenced_block_is_not_named():
    text = ("@before.md\n```bash\n@fenced.md\n```\n~~~~\n@tilde.md\n```\n@still.md\n~~~~\n"
            "@after.md\n")
    assert um.find_imports(text) == ("before.md", "after.md")


def test_an_escaped_space_stays_in_the_name():
    assert um.find_imports("@my\\ notes.md\n") == ("my\\ notes.md",)


def test_the_block_names_imports_and_reads_none(tmp_path):
    home = home_with(tmp_path, text="Read @extra.md and `@not-this.md`.\n")
    (home / ".claude" / "extra.md").write_text("EXTRA-SENTINEL\n")
    memory = um.user_memory_block("claude", [SOURCES, ""], {"HOME": str(home)})
    assert memory.imports == ("extra.md",)
    assert "EXTRA-SENTINEL" not in um.render_user_memory(memory)


# ---------- the rendered block, pinned ------------------------------------------

def test_the_block_is_pinned():
    memory = um.UserMemory(path=Path("/h/.claude/CLAUDE.md"), text="\n\nLine one.\nLine two.\n\n",
                           imports=())
    assert um.render_user_memory(memory) == (
        "<!-- source: user memory: /h/.claude/CLAUDE.md -->\n"
        "Contents of /h/.claude/CLAUDE.md (user's private global instructions for all "
        "projects; carried by session start because this launch's --setting-sources "
        "leaves out `user`):\n"
        "\n"
        "Line one.\nLine two.\n")


def test_the_block_with_imports_is_pinned():
    memory = um.UserMemory(path=Path("/h/.claude/CLAUDE.md"), text="@extra.md\n@~/notes.md\n",
                           imports=("extra.md", "~/notes.md"))
    assert um.render_user_memory(memory) == (
        "<!-- source: user memory: /h/.claude/CLAUDE.md -->\n"
        "Contents of /h/.claude/CLAUDE.md (user's private global instructions for all "
        "projects; carried by session start because this launch's --setting-sources "
        "leaves out `user`):\n"
        "Its `@` imports are not expanded here (@extra.md, @~/notes.md): read those "
        "files if you need them.\n"
        "\n"
        "@extra.md\n@~/notes.md\n")


def test_the_block_carries_the_file_verbatim(tmp_path):
    text = "  indented first line\n\n| a | b |\n\ttab\n"
    home = home_with(tmp_path, text=text)
    memory = um.user_memory_block("claude", [SOURCES, ""], {"HOME": str(home)})
    assert memory.text == text
    assert um.render_user_memory(memory).endswith("\n\n" + text)


# ---------- R2: the settings stay out --------------------------------------------

SETTINGS_SENTINEL = ('{"env": {"SENTINEL_ENV": "1"}, "permissions": {"allow": '
                     '["Bash(sentinel-perm)"]}, "hooks": {"Stop": [{"hooks": '
                     '[{"type": "command", "command": "sentinel-hook"}]}]}}\n')


def test_the_module_never_opens_settings_json(tmp_path, monkeypatch):
    """Only the memory file is opened: a sentinel settings.json beside it is
    never read, and nothing it holds reaches the block."""
    home = home_with(tmp_path)
    (home / ".claude" / "settings.json").write_text(SETTINGS_SENTINEL)
    (home / ".claude" / "settings.local.json").write_text(SETTINGS_SENTINEL)
    opened: list[str] = []

    def spy(real):
        def wrapper(file, *args, **kwargs):
            opened.append(str(file if isinstance(file, int) else os.fspath(file)))
            return real(file, *args, **kwargs)
        return wrapper

    real_bytes = pathlib.Path.read_bytes
    with monkeypatch.context() as spying:
        spying.setattr(builtins, "open", spy(builtins.open))
        spying.setattr(io, "open", spy(io.open))
        spying.setattr(os, "open", spy(os.open))
        spying.setattr(pathlib.Path, "read_bytes",
                       lambda self: (opened.append(str(self)), real_bytes(self))[1])
        memory = um.user_memory_block("claude", [SOURCES, ""], {"HOME": str(home)})
        block = um.render_user_memory(memory)
    assert opened and set(opened) == {str(home / ".claude" / "CLAUDE.md")}
    for sentinel in ("SENTINEL_ENV", "sentinel-perm", "sentinel-hook", "settings"):
        assert sentinel not in block


def test_the_callers_sources_are_never_widened(tmp_path):
    argv = [SOURCES, "project,local"]
    um.user_memory_block("claude", argv, {"HOME": str(home_with(tmp_path))})
    assert argv == [SOURCES, "project,local"]


# ---------- the row field is data, not a door -----------------------------------

def test_the_row_field_is_not_a_door():
    assert "user_memory" in CLAUDE
    assert "user_memory" not in vendor_doors("claude").values()
    assert not um.ROW_FIELD.endswith(("_door", "_flag"))


def test_the_row_spells_the_vendor_rule():
    spec = CLAUDE["user_memory"]
    assert (spec["file"], spec["config_dir_env"], spec["config_dir_default"],
            spec["sources_flag"], spec["user_source"]) == \
        ("CLAUDE.md", compact_point.CONFIG_DIR_ENV, ".claude",
         compact_point.SOURCES_FLAG, "user")
    assert "user" in compact_point.SOURCE_NAMES


# ---------- yield_line: the shared FILE door (design 3.5) --------------------

def _memory(path="/h/.claude/CLAUDE.md"):
    return um.UserMemory(path=Path(path), text="x\n", imports=())


@pytest.mark.parametrize("declared,cli", [
    (["--append-system-prompt-file", "a.md"], []),
    ([], ["--append-system-prompt-file", "a.md"]),
    ([], ["--append-system-prompt-file=a.md"]),
])
def test_the_operator_file_door_takes_it(declared, cli):
    line = um.yield_line("claude", _memory(), declared, cli)
    assert line == um.YIELD_LINE.format(door="--append-system-prompt-file",
                                        path="/h/.claude/CLAUDE.md")
    assert line.startswith("user memory not carried: your own --append-system-prompt-file")


@pytest.mark.parametrize("declared,cli", [
    ([], []),
    ([], ["--append-system-prompt", "inline words"]),          # design-001 review N1
    (["--append-system-prompt", "x"], ["--setting-sources", ""]),
    ([], ["--append-system-prompt-file-extra", "a.md"]),       # a longer flag is not the door
])
def test_nothing_else_makes_it_yield(declared, cli):
    assert um.yield_line("claude", _memory(), declared, cli) is None


def test_a_vendor_with_no_file_door_never_yields():
    assert um.yield_line("no-such-vendor", _memory(), [],
                         ["--append-system-prompt-file", "a.md"]) is None
