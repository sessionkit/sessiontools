"""The default seat's name and its built-in agent (ST-256 do-002).

The seat's word is the vendor's own key in the vendor table, never a literal;
the package ships the seat's agent file; `resolve_agent` falls back to it for
the seat word only, and a user's file of that name shadows it; and
`--list-agents` lists it, marked `built in` in a TAB column (Q12).

Hermetic: a temp agents folder, the conftest's walled HOME, and a stub vendor
on PATH for the one real launch. The singleton itself is do-003's.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import stat
import zipfile
from pathlib import Path

import pytest
from start_support import launch_e2e, make_agents_root, plan_for, recorded_argv
from wheel_support import build_wheel_offline

from sessiontools import cli, start
from sessiontools.start import agents as ag
from sessiontools.start import default_seat, holders
from sessiontools.start.agentfile import DEFAULT_VENDOR, parse_agent_markdown
from sessiontools.start.args import parse_args
from sessiontools.start.context import PlannedFile, PlannedPlugin
from sessiontools.start.errors import EXIT_OK, EXIT_USAGE, AuthoringError, LaunchError
from sessiontools.start.identity import SessionIdentity
from sessiontools.start.plan import build_vendor_argv, prepare
from sessiontools.start.vendor import VENDORS, vendor_doors

ROOT = Path(__file__).resolve().parents[1]
SEAT = default_seat.word()

# ---------- the name ---------------------------------------------------------


def test_the_word_is_the_default_vendors_key_in_the_vendor_table():
    assert SEAT == DEFAULT_VENDOR
    assert SEAT in VENDORS
    # Each vendor's seat is its own word, read from its row.
    assert all(default_seat.word(key) == key for key in VENDORS)


def test_a_vendor_with_no_row_has_no_seat_word():
    with pytest.raises(AuthoringError, match="not a supported vendor"):
        default_seat.word("no-such-vendor")


# ---------- the built-in template ----------------------------------------------


def test_the_template_is_one_description_and_an_empty_body():
    """An empty body changes nothing in the system prompt (F7); a non-empty
    one would replace Claude Code's default prompt (F6)."""
    text = Path(default_seat.template_path()).read_text(encoding="utf-8")
    front_matter, body = parse_agent_markdown(text)
    assert list(front_matter) == ["description"]
    assert front_matter["description"].endswith("Not for delegation: use general-purpose.")
    assert body.strip() == ""


def test_the_template_ships_in_the_package_beside_the_module():
    path = Path(default_seat.template_path())
    assert path.name == default_seat.TEMPLATE
    assert path.parent == Path(default_seat.__file__).parent
    assert path.is_file()


def test_the_wheel_ships_the_default_seat_template(tmp_path):
    wheel = build_wheel_offline(ROOT, tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
        member = f"sessiontools/start/{default_seat.TEMPLATE}"
        assert member in names
        shipped = zf.read(member)
    assert shipped == Path(default_seat.template_path()).read_bytes()


# ---------- resolution: the fallback, and shadowing ---------------------------


def test_the_seat_word_resolves_to_the_builtin_with_no_agent_file(tmp_path):
    root = make_agents_root(tmp_path)                    # holds scout only
    agent = ag.resolve_agent(SEAT, root=root)
    assert agent == ag.Agent(name=SEAT, file=default_seat.template_path(),
                             directory="", home="", builtin=True)


def test_the_seat_word_resolves_from_a_missing_agents_folder(tmp_path):
    agent = ag.resolve_agent(SEAT, root=str(tmp_path / "nowhere"))
    assert agent.builtin and agent.name == SEAT


def test_the_fallback_is_for_the_seat_word_only(tmp_path):
    root = make_agents_root(tmp_path)
    with pytest.raises(LaunchError, match="not found"):
        ag.resolve_agent("nosuch", root=root)


def test_resolve_agent_or_bare_reaches_the_builtin_by_name(tmp_path):
    root = make_agents_root(tmp_path)
    assert ag.resolve_agent_or_bare(SEAT, root=root).builtin
    assert ag.resolve_agent_or_bare(None, root=root) is ag.BARE_AGENT
    assert ag.resolve_agent_or_bare(ag.NO_AGENT, root=root) is ag.BARE_AGENT


@pytest.mark.parametrize("layout", ["file", "dir"])
def test_a_user_file_shadows_the_builtin_in_resolution(tmp_path, layout):
    root = make_agents_root(tmp_path, name=SEAT, layout=layout)
    agent = ag.resolve_agent(SEAT, root=root)
    assert not agent.builtin
    want = (os.path.join(root, SEAT, f"{SEAT}.md") if layout == "dir"
            else os.path.join(root, f"{SEAT}.md"))
    assert agent.file == want


def test_a_seat_directory_without_its_file_is_refused_not_passed_over(tmp_path):
    """A broken shadowing file is the user's, and `session start -` is the
    escape: the built-in never silently stands in for it."""
    root = tmp_path / "agents"
    (root / SEAT).mkdir(parents=True)
    with pytest.raises(LaunchError, match=f"{SEAT}.md is missing"):
        ag.resolve_agent(SEAT, root=str(root))


def test_the_builtin_is_no_named_agents_lookalike():
    assert ag.builtin_agent("scout") is None
    assert ag.BARE_AGENT.builtin is False


# ---------- --list-agents --------------------------------------------------------


def test_list_agents_marks_the_builtin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    assert start.main(["--list-agents"]) == EXIT_OK
    out = capsys.readouterr().out
    assert sorted(out.splitlines()) == sorted([f"{SEAT}\t{default_seat.BUILT_IN_MARK}",
                                               "scout"])
    # Field 1 is always a name `resolve_agent` accepts (the picker's rule).
    for line in out.splitlines():
        ag.resolve_agent(line.split("\t", 1)[0])


@pytest.mark.parametrize("layout", ["file", "dir"])
def test_shadowed_seat_word_is_listed_once_unmarked(tmp_path, monkeypatch, capsys, layout):
    root = make_agents_root(tmp_path, name=SEAT, layout=layout)
    monkeypatch.setenv("AGENTS_DIR", root)
    assert start.main(["--list-agents"]) == EXIT_OK
    assert capsys.readouterr().out == f"{SEAT}\n"
    assert ag.list_agent_rows(root=root) == [(SEAT, "")]


def test_empty_agents_dir_lists_the_builtin(tmp_path, monkeypatch, capsys):
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setenv("AGENTS_DIR", str(root))
    assert start.main(["--list-agents"]) == EXIT_OK
    assert capsys.readouterr().out == f"{SEAT}\t{default_seat.BUILT_IN_MARK}\n"
    assert ag.list_agents(root=str(tmp_path / "nowhere")) == [SEAT]


def test_a_broken_seat_directory_is_not_listed(tmp_path):
    """`resolve_agent` refuses it, so a picker must not offer it, marked or not."""
    root = make_agents_root(tmp_path)
    (Path(root) / SEAT).mkdir()
    assert ag.list_agent_rows(root=root) == [("scout", "")]


# ---------- a launch of the seat word, with no agent file ---------------------


def test_session_start_seat_word_works_with_no_agent_file(tmp_path):
    """A real subprocess launch, a stub vendor on PATH, a temp HOME, and an
    agents folder holding no file of the seat's name."""
    home = tmp_path / "home"
    home.mkdir()
    result, record, _work = launch_e2e(tmp_path, SEAT, extra_env={"HOME": str(home)})
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    assert argv[argv.index("--agent") + 1] == SEAT
    # Nothing was written into the user's agents folder for the seat.
    agents = tmp_path / "agents"
    assert not (agents / f"{SEAT}.md").exists() and not (agents / SEAT).exists()


def test_a_dry_run_of_the_seat_word_names_the_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert start.main(["--dry-run", SEAT]) == EXIT_OK
    assert f"--agent {SEAT}" in capsys.readouterr().out


# ---------- no literal vendor name in the new code -----------------------------


def _new_code() -> dict[str, str]:
    """ST-256 do-002's code, by name: the default_seat module and template
    whole, and each function or text it added to an existing module."""
    template = Path(default_seat.template_path())
    return {
        "default_seat.py": Path(default_seat.__file__).read_text(encoding="utf-8"),
        "default_seat.md": template.read_text(encoding="utf-8"),
        "agents.builtin_agent": inspect.getsource(ag.builtin_agent),
        "agents.list_agent_rows": inspect.getsource(ag.list_agent_rows),
        "agents._folder_agents": inspect.getsource(ag._folder_agents),
        "cli._agent": inspect.getsource(cli._agent),
        "cli._agent_default_parser": inspect.getsource(cli._agent_default_parser),
        "cli.AGENT_USAGE": cli.AGENT_USAGE,
        "cli.AGENT_DEFAULT_EPILOG": cli.AGENT_DEFAULT_EPILOG,
        # do-003: the singleton's wiring and the shared per-agent helpers.
        **{f"start.{f.__name__}": inspect.getsource(f)
           for f in (start.bare_start, start._claim, start._finish, start._plain,
                     # do-004: the named road, the tmux re-point, RESUMED.
                     start.named_seat_start, start._respect, start._named_dry_run,
                     start._record_id, start._resumed, start._repoint,
                     # do-006: the home-folder offer on the seat's roads.
                     start._offer, start._no_offer,
                     default_seat.named_gate_line, default_seat.named_unsure_line,
                     default_seat.door_for, default_seat.resumed_line,
                     default_seat.born_as_seat, default_seat.repoint,
                     default_seat.repoint_failed_line)},
        **{f"holders.{f.__name__}": inspect.getsource(f)
           for f in (holders.replacing, holders.claim_paths, holders.locked,
                     holders.read_claim, holders.claim_is_live, holders.live_claim,
                     holders.proc_start_of, holders.write_claim)},
    }


def test_no_vendor_literal_in_the_new_code():
    """The seat's word comes from the vendor row: no vendor's name is spelled
    in the code that names the seat, in any case."""
    names = re.compile("|".join(re.escape(v) for v in VENDORS), re.IGNORECASE)
    found = {where: names.findall(text) for where, text in _new_code().items()}
    assert {where: hits for where, hits in found.items() if hits} == {}


def test_the_literal_check_sees_a_planted_name():
    names = re.compile("|".join(re.escape(v) for v in VENDORS), re.IGNORECASE)
    assert names.search(f"return '{SEAT.capitalize()}'")


# ---------- do-001: the agents door ----------------------------------------------
# The seat's definition rides the vendor's agents door inline (Q1): the word,
# the SHIPPED description and an empty prompt, and no plugin dir. A shadowing
# file's body rides the 0600 system-prompt file door, first in that one file
# (Q2); its description is not carried (Q18); of its native keys only name:
# (the seat word), description: and settings: are accepted (Q15, R3).

SHADOW_BODY = "You are this host's own seat.\n"
CONTEXT_LINE = "seat-context-output"


def _seat_plan(tmp_path, front_matter=None, body=SHADOW_BODY, layout="file",
               flags=(), argv_tail=(), extra_env=None):
    """A LaunchPlan for the seat word: the built-in when FRONT_MATTER is None,
    else a shadowing file of the seat's name carrying FRONT_MATTER and BODY."""
    if front_matter is None:
        root = make_agents_root(tmp_path)
    else:
        root = make_agents_root(tmp_path, name=SEAT, front_matter=front_matter,
                                body=body, layout=layout)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    environ = {"AGENTS_DIR": root, "PATH": os.environ.get("PATH", ""),
               "SESSIONS_DIR": os.path.join(root, "sessions"),
               "SESSION_KEY_INIT": os.environ["SESSION_KEY_INIT"]}
    environ.update(extra_env or {})
    return prepare(parse_args([*flags, SEAT, *argv_tail]), environ=environ,
                   work_dir=str(work))


def _after(argv, flag):
    return argv[argv.index(flag) + 1]


def _prompt_file(plan):
    door = VENDORS[DEFAULT_VENDOR]["system_prompt_file_door"]
    path = _after(plan.argv, door)
    [item] = [f for f in plan.files if isinstance(f, PlannedFile) and f.path == path]
    return item


def test_the_vendor_row_has_an_agents_door_and_the_guard_sees_it():
    door = VENDORS[DEFAULT_VENDOR]["agents_door"]
    assert door == "--agents"
    assert vendor_doors(DEFAULT_VENDOR)[door] == "agents_door"


def test_the_seat_definition_is_the_shipped_description_and_an_empty_prompt():
    door, spec = default_seat.definition_argv(DEFAULT_VENDOR, ag.builtin_agent(SEAT))
    assert door == VENDORS[DEFAULT_VENDOR]["agents_door"]
    assert json.loads(spec) == {SEAT: {"description": default_seat.shipped_description(),
                                       "prompt": ""}}
    text = Path(default_seat.template_path()).read_text(encoding="utf-8")
    assert default_seat.shipped_description() == parse_agent_markdown(text)[0]["description"]


def test_seat_argv_is_the_bare_argv_plus_the_agents_door():
    """The unit pin of the measured shape (F7, Q10): the bare argv, then the
    agents door, then the identity door. Nothing else changes."""
    identity = SessionIdentity("sid-1", "", False, {}, minted=False)
    bare = build_vendor_argv(DEFAULT_VENDOR, {}, agent=None, identity=identity)
    seat = build_vendor_argv(DEFAULT_VENDOR, {}, agent=ag.builtin_agent(SEAT),
                             identity=identity)
    assert seat == [*bare, "--agents", default_seat.definition_json(DEFAULT_VENDOR),
                    "--agent", SEAT]
    assert json.loads(seat[seat.index("--agents") + 1])[SEAT]["prompt"] == ""


def test_the_builtin_launches_with_no_plugin_dir_and_no_prompt_door(tmp_path):
    plan = _seat_plan(tmp_path)
    table = VENDORS[DEFAULT_VENDOR]
    assert table["agent_plugin_dir_arg"] not in plan.argv
    assert table["system_prompt_file_door"] not in plan.argv
    assert not any(isinstance(item, PlannedPlugin) for item in plan.files)
    assert _after(plan.argv, "--agent") == SEAT


def test_a_named_agent_other_than_the_seat_keeps_its_plugin_dir(tmp_path):
    plan = plan_for(tmp_path, "description: a scout\n")
    assert "--plugin-dir" in plan.argv and "--agents" not in plan.argv


@pytest.mark.parametrize("layout", ["file", "dir"])
def test_user_file_shadows_the_builtin(tmp_path, layout):
    """Its launcher and vendor sections apply; the JSON carries the SHIPPED
    description, not the file's (Q18); its body is never in argv."""
    plan = _seat_plan(tmp_path, "description: my own words\nclaude:\n  effort: high\n",
                      layout=layout)
    assert not plan.agent.builtin
    assert _after(plan.argv, "--effort") == "high"
    spec = json.loads(_after(plan.argv, "--agents"))
    assert spec == {SEAT: {"description": default_seat.shipped_description(), "prompt": ""}}
    assert not any("my own words" in token or SHADOW_BODY.strip() in token
                   for token in plan.argv)
    assert "--plugin-dir" not in plan.argv


def test_seat_json_carries_only_the_shipped_description(tmp_path, monkeypatch, capsys):
    """Q1's condition: no user text in argv, and the dry run says why."""
    root = make_agents_root(tmp_path, name=SEAT, front_matter="description: mine\n")
    monkeypatch.setenv("AGENTS_DIR", root)
    monkeypatch.chdir(tmp_path)
    assert start.main(["--dry-run", SEAT]) == EXIT_OK
    out = capsys.readouterr().out
    [line] = [line for line in out.splitlines() if line.strip().startswith("'{")]
    assert "mine" not in line.split("<-")[0]
    assert "description: is not carried" in line
    assert "inline" in line


def test_shadowing_body_rides_the_append_file_door(tmp_path):
    """Q2: one file, the body first and then the context output, never argv."""
    plan = _seat_plan(tmp_path, "start:\n  context:\n    system-prompt:\n"
                                f"      - printf '{CONTEXT_LINE}\\n'\n")
    item = _prompt_file(plan)
    assert item.text.startswith(SHADOW_BODY + "\n")
    assert CONTEXT_LINE in item.text[len(SHADOW_BODY):]
    assert not any(SHADOW_BODY.strip() in str(token) for token in plan.argv)
    sources = {str(t): s for t, s in plan.provenance}
    assert "file body, then injected context" in sources[item.path]


def test_shadowing_body_rides_with_idle_too(tmp_path):
    plan = _seat_plan(tmp_path, "start:\n  context:\n    system-prompt:\n"
                                f"      - printf '{CONTEXT_LINE}\\n'\n", flags=["--idle"])
    assert _prompt_file(plan).text == SHADOW_BODY


def test_the_body_file_is_written_0600(tmp_path):
    """A real launch with a stub vendor: the file the door names exists,
    0600, and holds the body; argv holds only its path."""
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / f"{SEAT}.md").write_text(f"---\ndescription: x\n---\n{SHADOW_BODY}")
    result, record, _work = launch_e2e(tmp_path, SEAT)
    assert result.returncode == 0, result.stderr
    argv = recorded_argv(record)
    path = Path(_after(argv, VENDORS[DEFAULT_VENDOR]["system_prompt_file_door"]))
    assert path.read_text() == SHADOW_BODY
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert SHADOW_BODY.strip() not in "\0".join(argv)


def test_a_bodyless_shadowing_file_opens_no_prompt_door(tmp_path):
    plan = _seat_plan(tmp_path, "description: x\n", body="\n\n")
    assert VENDORS[DEFAULT_VENDOR]["system_prompt_file_door"] not in plan.argv


def test_cli_append_file_beside_a_shadowing_body_is_refused_before_the_mint(tmp_path):
    """The rehearsal counts the body as opening the door: refused, and no
    session directory is left behind."""
    with pytest.raises(LaunchError) as stop:
        _seat_plan(tmp_path, "description: x\n",
                   argv_tail=["--append-system-prompt-file", "/tmp/mine.md"])
    assert stop.value.code == EXIT_USAGE
    assert "--append-system-prompt-file is opened twice" in str(stop.value)
    assert not (tmp_path / "agents" / "sessions").exists()


def test_cli_append_file_beside_a_bodyless_seat_is_the_users(tmp_path):
    plan = _seat_plan(tmp_path, argv_tail=["--append-system-prompt-file", "/tmp/mine.md"])
    assert _after(plan.argv, "--append-system-prompt-file") == "/tmp/mine.md"


@pytest.mark.parametrize("where", ["cli", "front matter"])
def test_the_agents_door_opened_twice_is_refused(tmp_path, where):
    if where == "cli":
        kwargs = {"argv_tail": ["--agents", "{}"]}
    else:
        kwargs = {"front_matter": "claude:\n  agents: '{}'\n"}
    with pytest.raises(LaunchError) as stop:
        _seat_plan(tmp_path, **kwargs)
    assert stop.value.code == EXIT_USAGE
    assert "--agents is opened twice" in str(stop.value)
    assert not (tmp_path / "agents" / "sessions").exists()


@pytest.mark.parametrize("key", ["tools", "model", "skills", "voice"])
def test_shadowing_native_key_is_refused(tmp_path, key):
    with pytest.raises(LaunchError) as stop:
        _seat_plan(tmp_path, f"description: x\n{key}: anything\n")
    assert stop.value.code == EXIT_USAGE
    message = str(stop.value)
    assert f"`{key}:`" in message and f"{SEAT}.md" in message


def test_every_refused_native_key_is_named_at_once(tmp_path):
    with pytest.raises(LaunchError) as stop:
        _seat_plan(tmp_path, f"name: {SEAT}\ntools: a\nmodel: b\n")
    assert "`model:`, `tools:`" in str(stop.value)


def test_shadowing_name_key_is_accepted_when_it_is_the_seat_word(tmp_path):
    plan = _seat_plan(tmp_path, f"name: {SEAT}\ndescription: x\n")
    assert _after(plan.argv, "--agent") == SEAT


def test_shadowing_name_key_naming_another_agent_is_refused(tmp_path):
    with pytest.raises(LaunchError) as stop:
        _seat_plan(tmp_path, "name: scout\n")
    assert stop.value.code == EXIT_USAGE
    assert "`name: scout`" in str(stop.value)


def test_shadowing_settings_key_is_accepted(tmp_path):
    """R3: `settings:` is this launcher's own layer, so it applies; `skills:`
    beside it is still refused."""
    plan = _seat_plan(tmp_path, "settings:\n  claude-code:\n    model: seat-model-x\n")
    path = _after(plan.argv, VENDORS[DEFAULT_VENDOR]["settings_door"])
    [item] = [f for f in plan.files if isinstance(f, PlannedFile) and f.path == path]
    assert json.loads(item.text)["model"] == "seat-model-x"
    again = tmp_path / "again"
    again.mkdir()
    with pytest.raises(LaunchError, match="`skills:`"):
        _seat_plan(again,
                   "settings:\n  claude-code:\n    model: m\nskills: [a]\n")


def test_the_named_agents_native_keys_are_not_the_seats_rule(tmp_path):
    plan = plan_for(tmp_path, "description: x\ntools: Read\nmodel: opus\n")
    assert "--plugin-dir" in plan.argv


def test_a_seat_resume_carries_the_definition_and_opens_no_prompt_door(tmp_path):
    """A resume keeps the prompt the session was born with (f02 G2), so a
    shadowing body opens no door there; the agents door still rides."""
    plan = _seat_plan(tmp_path, "description: x\n",
                      argv_tail=["--resume", "0123abcd-0000-4000-8000-000000000000"])
    assert VENDORS[DEFAULT_VENDOR]["system_prompt_file_door"] not in plan.argv
    assert "--agents" in plan.argv and _after(plan.argv, "--agent") == SEAT


def _tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*")) if "sessions" not in p.parts}


@pytest.mark.parametrize("shadow", [False, True])
def test_nothing_is_written_to_the_agents_folder(tmp_path, shadow):
    """A real launch leaves the agents folder byte-identical, built-in or
    shadowed (the sessions root the test puts beside it aside)."""
    agents = tmp_path / "agents"
    agents.mkdir()
    if shadow:
        (agents / f"{SEAT}.md").write_text(f"---\ndescription: x\n---\n{SHADOW_BODY}")
    (agents / "scout.md").write_text("---\nclaude:\n  effort: high\n---\nYou are.\n")
    before = _tree(agents)
    result, _record, _work = launch_e2e(tmp_path, SEAT)
    assert result.returncode == 0, result.stderr
    assert _tree(agents) == before
