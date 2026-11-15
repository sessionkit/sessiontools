"""ST-302 do-002: the user's CLAUDE.md, wired into the launch plan.

`start.user_memory` decides what to carry; these tests drive it through
`resolve` and `assemble` (`plan_for`, `prepare`) in a TEMP HOME, never the
real `~/.claude/` (R5): the block leads the one system-prompt file and the
provenance names it (design 3.3), the cases that carry nothing (3.4, R3,
R4), the yield to an operator's own file door (3.5), and the user
`settings.json` that never comes along (3.6, R2).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from start_support import make_agents_root, plan_for

from sessiontools.start import default_seat
from sessiontools.start import plan as plan_module
from sessiontools.start.args import parse_args
from sessiontools.start.context import PlannedFile
from sessiontools.start.errors import EXIT_USAGE, LaunchError
from sessiontools.start.plan import prepare
from sessiontools.start.run import render_dry_run_report
from sessiontools.start.user_memory import HEADER, YIELD_LINE
from sessiontools.start.vendor import VENDORS

FILE_DOOR = VENDORS["claude"]["system_prompt_file_door"]
INLINE_DOOR = VENDORS["claude"]["system_prompt_door"]
SOURCES = "--setting-sources"
MEMORY = "OTTER-USER-MEMORY: the user's own standing words.\n"
CONTEXT_LINE = "generator-context-output"
GENERATOR = f"start:\n  context:\n    system-prompt:\n      - printf '{CONTEXT_LINE}\\n'\n"
SEAT = default_seat.word()
SEAT_BODY = "You are this host's own seat.\n"
# The sentinel user settings (R2): a hook, an env var, a permission. Not one
# of its words may reach the argv, a planned file or the provenance.
SENTINEL_SETTINGS = {
    "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                           "command": "echo SENTINEL-HOOK"}]}]},
    "env": {"SENTINEL_ENV": "SENTINEL-ENV-VALUE"},
    "permissions": {"allow": ["Bash(SENTINEL-PERMISSION:*)"]},
}
SENTINEL_WORDS = ("SENTINEL", ".claude/settings.json")


def home(tmp_path: Path, memory: str | None = MEMORY, settings: bool = True) -> Path:
    """A temp HOME: `.claude/CLAUDE.md` holding MEMORY (none when None), and
    the sentinel `.claude/settings.json` beside it."""
    root = tmp_path / "home"
    (root / ".claude").mkdir(parents=True, exist_ok=True)
    if memory is not None:
        (root / ".claude" / "CLAUDE.md").write_text(memory, encoding="utf-8")
    if settings:
        (root / ".claude" / "settings.json").write_text(json.dumps(SENTINEL_SETTINGS))
    return root


def memory_plan(tmp_path, front_matter="name: scout\n", argv_tail=(), flags=(),
                memory=MEMORY, extra_env=None):
    """A live plan (it mints, writes nothing) in a temp HOME holding MEMORY."""
    env = {"HOME": str(home(tmp_path, memory))}
    env.update(extra_env or {})
    return plan_for(tmp_path, front_matter, argv_tail=argv_tail, flags=flags,
                    extra_env=env)


def after(argv, flag):
    return argv[argv.index(flag) + 1]


def prompt_file(plan) -> PlannedFile:
    path = after(plan.argv, FILE_DOOR)
    [item] = [f for f in plan.files if isinstance(f, PlannedFile) and f.path == path]
    return item


def memory_path(tmp_path) -> Path:
    return tmp_path / "home" / ".claude" / "CLAUDE.md"


def provenance_of(plan, token):
    return {str(t): s for t, s in plan.provenance}[token]


# ---------- carried: the sources leave `user` out ---------------------------

@pytest.mark.parametrize("sources", ["", "project,local", "project", "local, project"])
def test_sources_without_user_carry_it_first_in_the_prompt_file(tmp_path, sources):
    plan = memory_plan(tmp_path, argv_tail=[SOURCES, sources])
    item = prompt_file(plan)
    path = memory_path(tmp_path)
    assert item.text.startswith(f"<!-- source: user memory: {path} -->\n")
    assert HEADER.format(path=path, flag=SOURCES, source="user") in item.text
    assert item.text.endswith(MEMORY)
    # R2: the caller's sources token is passed through as typed, never widened.
    assert after(plan.argv, SOURCES) == sources
    assert plan.argv.count(SOURCES) == 1
    assert plan.notes == ()


def test_the_provenance_names_the_user_memory_file(tmp_path):
    plan = memory_plan(tmp_path, argv_tail=[SOURCES, ""])
    source = provenance_of(plan, prompt_file(plan).path)
    assert source == f"computed: user memory ({memory_path(tmp_path)}) (system prompt)"


def test_the_equals_spelling_without_user_carries_it(tmp_path):
    plan = memory_plan(tmp_path, argv_tail=[f"{SOURCES}=project,local"])
    assert MEMORY in prompt_file(plan).text
    assert f"{SOURCES}=project,local" in plan.argv


def test_the_last_sources_flag_decides(tmp_path):
    carried = memory_plan(tmp_path, argv_tail=[SOURCES, "user", SOURCES, ""])
    assert MEMORY in prompt_file(carried).text


def test_front_matter_sources_count_as_the_vendor_sees_them(tmp_path):
    """`claude: setting-sources:` renders before the CLI: it decides alone."""
    plan = memory_plan(tmp_path, "name: scout\nclaude:\n  setting-sources: project,local\n")
    assert MEMORY in prompt_file(plan).text


def test_a_cli_user_after_front_matter_without_it_is_not_carried(tmp_path):
    plan = memory_plan(tmp_path, "name: scout\nclaude:\n  setting-sources: project\n",
                       argv_tail=[SOURCES, "user"])
    assert FILE_DOOR not in plan.argv


# ---------- not carried (R3, R4, off flags) ---------------------------------

@pytest.mark.parametrize("tail", [[SOURCES, "user"], [SOURCES, "user,project"],
                                  [f"{SOURCES}=user"], [SOURCES, "project, user"],
                                  [SOURCES, "", SOURCES, "user"], []])
def test_the_vendor_loads_it_itself_so_it_is_not_carried(tmp_path, tail):
    """R3: `user` in the last sources flag, or no sources flag at all."""
    plan = memory_plan(tmp_path, argv_tail=tail)
    assert FILE_DOOR not in plan.argv
    assert not any(MEMORY.strip() in getattr(f, "text", "") for f in plan.files)
    # R2: the sources tokens are the caller's, unchanged.
    assert [t for t in plan.argv if t.startswith(SOURCES)] == \
        [t for t in tail if t.startswith(SOURCES)]


@pytest.mark.parametrize("flag", ["--bare", "--safe-mode", "--restricted"])
def test_an_off_flag_carries_nothing(tmp_path, flag):
    plan = memory_plan(tmp_path, argv_tail=[SOURCES, "", flag])
    assert FILE_DOOR not in plan.argv


def _masked(plan):
    sid = plan.identity.session_id
    return [token.replace(sid, "<sid>") for token in plan.argv]


@pytest.mark.parametrize("kind", ["missing", "empty", "blank", "not-utf-8"])
def test_no_usable_file_leaves_the_argv_as_it_was(tmp_path, monkeypatch, kind):
    """R4: missing, empty, blank or not UTF-8: the argv equals the launch
    with the module stubbed out."""
    memory = {"missing": None, "empty": "", "blank": "  \n\n", "not-utf-8": None}[kind]
    if kind == "not-utf-8":
        home(tmp_path, memory=None)
        memory_path(tmp_path).write_bytes(b"\xff\xfe not utf-8")
    tail = [SOURCES, ""]
    with_change = memory_plan(tmp_path, GENERATOR, argv_tail=tail, flags=["--dry-run"],
                              memory=memory)
    monkeypatch.setattr(plan_module, "user_memory_block", lambda *_a, **_k: None)
    without = memory_plan(tmp_path, GENERATOR, argv_tail=tail, flags=["--dry-run"],
                          memory=memory)
    assert _masked(with_change) == _masked(without)
    assert with_change.provenance and [s for _t, s in with_change.provenance] == \
        [s for _t, s in without.provenance]


def test_an_unreadable_file_is_a_quiet_no_op(tmp_path):
    root = home(tmp_path)
    path = memory_path(tmp_path)
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user who reads a 0000 file")
        plan = plan_for(tmp_path, "name: scout\n", argv_tail=[SOURCES, ""],
                        extra_env={"HOME": str(root)})
    finally:
        path.chmod(0o600)
    assert FILE_DOOR not in plan.argv


def test_a_resume_is_unchanged(tmp_path):
    """Q3: a resume keeps the prompt it was born with."""
    sid = "6f1c1f6e-6b0d-4b6e-9a3c-2d3f4e5a6b7c"
    plan = memory_plan(tmp_path, argv_tail=["--resume", sid, SOURCES, ""])
    assert plan.resume
    assert FILE_DOOR not in plan.argv
    assert after(plan.argv, SOURCES) == ""


# ---------- the path the vendor reads ----------------------------------------

def test_a_declared_config_dir_moves_the_file(tmp_path):
    """The agent file's `environment: variables:` CLAUDE_CONFIG_DIR is the
    vendor's, so it is ours."""
    other = tmp_path / "config"
    other.mkdir()
    (other / "CLAUDE.md").write_text("DECLARED-DIR-MEMORY\n")
    plan = memory_plan(tmp_path, "name: scout\nenvironment:\n  variables:\n"
                                 f"    CLAUDE_CONFIG_DIR: {other}\n",
                       argv_tail=[SOURCES, ""])
    text = prompt_file(plan).text
    assert "DECLARED-DIR-MEMORY" in text and MEMORY not in text


def test_the_launchers_config_dir_moves_the_file(tmp_path):
    other = tmp_path / "config"
    other.mkdir()
    (other / "CLAUDE.md").write_text("LAUNCHER-DIR-MEMORY\n")
    plan = memory_plan(tmp_path, argv_tail=[SOURCES, ""],
                       extra_env={"CLAUDE_CONFIG_DIR": str(other)})
    assert "LAUNCHER-DIR-MEMORY" in prompt_file(plan).text


# ---------- order: memory first, then the body, then generators --------------

def test_generators_keep_their_place_after_the_block(tmp_path):
    plan = memory_plan(tmp_path, GENERATOR, argv_tail=[SOURCES, ""])
    text = prompt_file(plan).text
    assert text.index(MEMORY) < text.index(CONTEXT_LINE)
    # one blank line, then the generator's own block
    assert f"{MEMORY}\n<!-- source: printf" in text
    assert provenance_of(plan, prompt_file(plan).path) == (
        f"computed: user memory ({memory_path(tmp_path)}), then injected context "
        "(system prompt)")


def test_without_the_memory_the_generator_provenance_is_as_before(tmp_path):
    plan = memory_plan(tmp_path, GENERATOR, argv_tail=[SOURCES, "user"])
    assert provenance_of(plan, prompt_file(plan).path) == \
        "computed: injected context (system prompt)"
    assert MEMORY not in prompt_file(plan).text


def _seat_plan(tmp_path, front_matter, argv_tail=(), flags=()):
    root = make_agents_root(tmp_path, name=SEAT, front_matter=front_matter, body=SEAT_BODY)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    environ = {"AGENTS_DIR": root, "PATH": os.environ.get("PATH", ""),
               "SESSIONS_DIR": os.path.join(root, "sessions"),
               "SESSION_KEY_INIT": os.environ["SESSION_KEY_INIT"],
               "HOME": str(home(tmp_path))}
    return prepare(parse_args([*flags, SEAT, *argv_tail]), environ=environ,
                   work_dir=str(work))


def test_the_default_seat_body_and_generators_follow_the_block(tmp_path):
    plan = _seat_plan(tmp_path, GENERATOR.replace("start:", f"name: {SEAT}\nstart:"),
                      argv_tail=[SOURCES, ""])
    text = prompt_file(plan).text
    assert text.index(MEMORY) < text.index(SEAT_BODY) < text.index(CONTEXT_LINE)
    assert provenance_of(plan, prompt_file(plan).path) == (
        f"computed: user memory ({memory_path(tmp_path)}), then the default seat's "
        "file body, then injected context (system prompt)")


def test_the_default_seat_keeps_the_door_so_an_operator_file_still_refuses(tmp_path):
    """The seat's body computes the door, so nothing yields: the guard
    refuses exactly as it did before ST-302."""
    with pytest.raises(LaunchError) as refused:
        _seat_plan(tmp_path, f"name: {SEAT}\n",
                   argv_tail=[SOURCES, "", FILE_DOOR, str(tmp_path / "mine.md")])
    assert refused.value.code == EXIT_USAGE
    assert "opened twice" in str(refused.value)


# ---------- 3.5: the yield to the operator's own file door -------------------

def test_idle_carries_it_too(tmp_path):
    """`--idle` stops the generators, not the user's own file (3.4)."""
    plan = memory_plan(tmp_path, GENERATOR, argv_tail=[SOURCES, ""], flags=["--idle"])
    text = prompt_file(plan).text
    assert MEMORY in text and CONTEXT_LINE not in text


@pytest.mark.parametrize("flags", [["--idle"], []])
def test_an_operator_file_door_under_idle_takes_the_door_and_says_so(tmp_path, capsys, flags):
    mine = str(tmp_path / "mine.md")
    front = GENERATOR if flags else "name: scout\n"
    plan = memory_plan(tmp_path, front, argv_tail=[SOURCES, "", FILE_DOOR, mine],
                       flags=flags)
    # no refusal; the operator's door is the only one, and the launch is as before
    assert plan.argv.count(FILE_DOOR) == 1 and after(plan.argv, FILE_DOOR) == mine
    assert not any(MEMORY.strip() in getattr(f, "text", "") for f in plan.files)
    line = YIELD_LINE.format(door=FILE_DOOR, path=memory_path(tmp_path))
    assert plan.notes == (line,)
    err = capsys.readouterr().err
    assert err.count("user memory not carried") == 1
    assert f"session start: {line}\n" in err


def test_an_operator_file_door_in_front_matter_yields_too(tmp_path, capsys):
    mine = str(tmp_path / "mine.md")
    plan = memory_plan(tmp_path, f"name: scout\nclaude:\n  append-system-prompt-file: {mine}\n",
                       argv_tail=[SOURCES, ""])
    assert plan.argv.count(FILE_DOOR) == 1 and after(plan.argv, FILE_DOOR) == mine
    assert len(plan.notes) == 1
    assert "user memory not carried" in capsys.readouterr().err


def test_the_equals_spelling_of_the_operator_file_door_yields(tmp_path):
    plan = memory_plan(tmp_path, argv_tail=[SOURCES, "", f"{FILE_DOOR}=x.md"])
    assert FILE_DOOR not in plan.argv and len(plan.notes) == 1


def test_an_inline_system_prompt_does_not_make_it_yield(tmp_path, capsys):
    """design-001 review N1: only the FILE door is shared; the memory rides
    beside an operator's inline --append-system-prompt."""
    plan = memory_plan(tmp_path, argv_tail=[SOURCES, "", INLINE_DOOR, "operator words"])
    assert MEMORY in prompt_file(plan).text
    assert after(plan.argv, INLINE_DOOR) == "operator words"
    assert plan.notes == ()
    assert "user memory not carried" not in capsys.readouterr().err


def test_generators_that_compute_the_door_still_refuse_an_operator_file(tmp_path):
    """No new refusal, and no lost one: the generators compute the door, so
    the guard refuses as it did before."""
    with pytest.raises(LaunchError) as refused:
        memory_plan(tmp_path, GENERATOR,
                    argv_tail=[SOURCES, "", FILE_DOOR, str(tmp_path / "mine.md")])
    assert "opened twice" in str(refused.value)


def test_no_yield_line_when_there_is_no_memory_to_yield(tmp_path, capsys):
    plan = memory_plan(tmp_path, argv_tail=[SOURCES, "", FILE_DOOR, "x.md"], memory=None)
    assert plan.notes == ()
    assert "user memory" not in capsys.readouterr().err


def test_the_dry_run_report_says_the_yield(tmp_path):
    plan = memory_plan(tmp_path, argv_tail=[SOURCES, "", FILE_DOOR, "x.md"],
                       flags=["--dry-run"])
    report = render_dry_run_report(plan)
    line = YIELD_LINE.format(door=FILE_DOOR, path=memory_path(tmp_path))
    assert f"note:     {line}\n" in report
    assert "note:" not in render_dry_run_report(
        memory_plan(tmp_path, argv_tail=[SOURCES, ""], flags=["--dry-run"]))


def test_the_dry_run_report_names_the_source(tmp_path):
    report = render_dry_run_report(
        memory_plan(tmp_path, argv_tail=[SOURCES, ""], flags=["--dry-run"]))
    assert f"computed: user memory ({memory_path(tmp_path)})" in report


# ---------- R2: the user settings never come along ---------------------------

@pytest.mark.parametrize("tail", [[SOURCES, ""], [SOURCES, "project,local"]])
def test_the_sentinel_settings_are_never_named(tmp_path, tail):
    plan = memory_plan(tmp_path, GENERATOR, argv_tail=tail)
    assert MEMORY in prompt_file(plan).text      # the memory IS carried
    texts = [str(t) for t in plan.argv]
    texts += [s for _t, s in plan.provenance]
    texts += [getattr(f, "text", "") or "" for f in plan.files]
    texts += [str(getattr(f, "path", "")) for f in plan.files]
    for word in (*SENTINEL_WORDS, str(tmp_path / "home" / ".claude" / "settings.json")):
        assert not any(word in text for text in texts), word
    # The launch script's record call lists the vendor's startup files that
    # EXIST, by path (`record.vendor_startup_files`, before ST-302): an
    # audit list that reads none. Not one word of the settings is in it.
    assert "SENTINEL" not in plan.script
    assert "user" not in {name.strip() for name in after(plan.argv, SOURCES).split(",")}
