"""docs/default-model.md, pinned to the code and to `--help`.

The page says what `session default-model` does, how to install its unit
and what each exit means. Each of those is a fact the code also holds: the
unit names, the exit codes, the backups kept, the poll interval, the
aliases. The install commands are the ones `--help` prints, line for line,
so the two cannot drift apart. The page is public text: it names no
person, host, private path or tracker id.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sessiontools import cli, default_model_cli
from sessiontools import default_model as dm
from sessiontools import default_model_watch as dmw

ROOT = Path(__file__).resolve().parent.parent
PAGE = "docs/default-model.md"
TEXT = (ROOT / PAGE).read_text(encoding="utf-8")
FENCE = re.compile(r"^```[a-z]*\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _flat(text: str) -> str:
    """TEXT with every run of whitespace one space: a wrapped line reads
    as the sentence it is."""
    return " ".join(text.split())


def _blocks() -> str:
    """Every fenced block on the page, joined, continuations folded."""
    return "\n".join(FENCE.findall(TEXT)).replace("\\\n", " ")


def _section(title: str) -> str:
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |^\[[^\]]+\]: |\Z)", TEXT,
                      re.MULTILINE | re.DOTALL)
    assert match, f"no '## {title}' section"
    return match.group(1)


def _help_install_lines() -> list[str]:
    """The indented command lines of the help's epilog, continuations
    folded into the line they continue."""
    lines, held = [], ""
    for raw in default_model_cli.EPILOG.splitlines():
        if not raw.startswith("    "):
            held = ""
            continue
        line = held + raw.strip()
        if line.endswith("\\"):
            held = line[:-1]
            continue
        if line.endswith("&&"):
            held = line + " "
            continue
        held = ""
        if line.split()[0] in ("session", "mkdir", "launchctl", "systemctl"):
            lines.append(line)
    return lines


def test_the_help_has_install_lines_to_compare():
    lines = _help_install_lines()
    assert any(line.startswith("launchctl bootstrap") for line in lines), lines
    assert any("unit systemd | awk" in line for line in lines), lines
    assert any(line.startswith("session default-model unit launchd >") and "&& mv" in line
               for line in lines), lines


@pytest.mark.parametrize("line", _help_install_lines())
def test_each_install_line_of_the_help_is_on_the_page(line):
    assert _flat(line) in _flat(_blocks()), line


def test_the_launchd_lines_never_redirect_into_launch_agents():
    # A refused `unit` prints nothing: a redirect straight into the folder
    # launchd loads from would leave an empty plist there.
    for text in (_blocks(), default_model_cli.EPILOG):
        assert "unit launchd > ~/Library/LaunchAgents/" not in text


def test_the_page_names_the_units_the_code_renders():
    for name in (dmw.LABEL, dmw.SYSTEMD_PATH_UNIT, dmw.SYSTEMD_SERVICE_UNIT):
        assert name in TEXT, name
    fake = Path("/home-of-test")
    log = dmw.log_path({"HOME": str(fake)}).relative_to(fake)
    assert f"`~/{log}`" in TEXT


def test_the_page_says_run_one_pinner_and_installs_nothing():
    flat = _flat(TEXT)
    assert "Run one pinner." in flat
    assert "Unload any other tool that pins `\"model\"` before you load this one." in flat
    assert "It installs and loads nothing." in flat


def test_the_page_says_launchd_needs_a_login_session_and_watch_is_the_route():
    # `bootstrap gui/<uid>` fails (exit 125) for a user with no login
    # session, and the user domain's WatchPaths never fires: `watch` is
    # the route there.
    flat = _flat(_section("Keep it pinned: install one unit"))
    assert "The launchd agent needs a login session." in flat
    assert "exits 125" in flat
    assert "`WatchPaths` never fires again" in flat
    assert "run `session default-model watch` in the foreground" in flat
    assert "no login session" in _flat(_section("The verbs"))


def test_the_exit_codes_table_is_the_code():
    rows = dict(re.findall(r"^\| `(\d)` \| (.+?) \|$", _section("Exit codes"), re.MULTILINE))
    assert set(rows) == {"0", "2", str(dm.REFUSED), str(dm.FAILED), str(dm.UNREADABLE)}
    assert rows[str(dm.OK)].startswith("done, already in sync, or unset")
    assert rows[str(dm.REFUSED)] == "refused, nothing written"
    for code in ("0", "2", "3", "4", "5"):
        assert f"{code} " in default_model_cli.EPILOG, code


def test_the_numbers_on_the_page_are_the_code():
    flat = _flat(TEXT)
    assert f"the newest {dm.BACKUPS_KEPT} kept" in flat
    assert f"up to {['', 'one', 'two', 'three', 'four'][dm.PASSES]} times" in flat
    assert f"(default {dmw.INTERVAL:g})" in flat
    assert f"at least {dmw.THROTTLE} seconds" in flat


def test_the_list_section_names_the_aliases_and_says_it_is_not_live():
    section = _flat(_section("The list is what this release knows"))
    for alias in dm.ALIASES:
        assert f"`{alias}`" in section, alias
    assert "It is not a live check" in section
    assert "`no: never pinned (Fable)`" in section
    assert "never pinned (Fable)" in next(m.why for m in dm.models() if not m.pinnable)


def test_the_setting_on_the_page_is_the_one_read():
    assert '{"model": {"default": "opus"}}' in TEXT
    assert (dm.SETTINGS_TOOL, dm.SETTINGS_BLOCK, dm.SETTINGS_KEY) == ("session", "model",
                                                                        "default")


def test_every_subverb_is_on_the_page():
    for verb, _ in default_model_cli.SUBVERBS:
        assert f"`session default-model {verb}" in TEXT or (
            verb == "show" and "`session default-model [show" in TEXT), verb


def test_the_page_is_linked_from_the_readme_and_the_verb_page():
    assert "[default-model]: docs/default-model.md" in (ROOT / "README.md").read_text()
    for name in ("docs/session.md", "docs/settings.md", "docs/configuration.md"):
        assert "[default-model]: default-model.md" in (ROOT / name).read_text(), name


def test_the_verb_page_form_line_names_every_subverb():
    text = _flat((ROOT / "docs/session.md").read_text(encoding="utf-8"))
    for form in ("show [--json]", "list [--json]", "enforce [--dry-run]",
                 "watch [--interval S]", "unit launchd|systemd"):
        assert form in text, form
    assert "default-model" in cli.HANDLERS


#: A tracker id (`ABC-123`), a home under /Users or /home, or an email.
PRIVATE = re.compile(r"\b[A-Z]{2,}-\d+\b|/Users/|/home/[a-z]|[\w.+-]+@[\w-]+\.[\w.]+")
#: Words of that shape that are not ids.
NOT_IDS = frozenset({"UTF-8"})


def test_the_page_is_neutral_text():
    assert [m for m in PRIVATE.findall(TEXT) if m not in NOT_IDS] == []


def test_the_neutral_check_can_fail():
    assert PRIVATE.findall("see ABC-12 at /Users/x and a@b.example")
