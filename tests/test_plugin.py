"""The plugin and its `session` skill: what an agent is taught matches the CLI.

Why it ships: nothing taught agents `session list|get|context|start` once the
session repo retired (its skill went with it), so sessiontools ships the
skill as a Claude Code plugin, the way sessionmail ships its own.

A skill is prose, and prose drifts. These tests pin the parts that can be
checked: the verbs it teaches are the verbs `session --help` lists, the
props it names are the props `session get` lists, it fits the reader's fold,
and the plugin's version is the package's.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from sessiontools import band, cli, get

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "session" / "SKILL.md"
REFERENCES = SKILL.parent / "references"
CONTEXT_REF = REFERENCES / "context.md"
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKET = ROOT / ".claude-plugin" / "marketplace.json"

#: The fold sessionmail's skills and help pages keep: the first 40 lines and
#: 400 tokens are all an agent may read, so what it needs goes there.
FOLD_LINES, FOLD_TOKENS = 40, 400

#: Verbs `session --help` lists that the skill does NOT teach yet, each with
#: why. A held verb appears nowhere in the skill. Striking an entry is the
#: one-line follow-up when its question is ruled, with the skill line added.
HELD = {"ledger": "ST-26 OQ3: teaching agents the verb waits on the maintainer",
        "account": "ST-26 OQ3: the skill is at its 400-token cap; the line waits on the maintainer",
        "reap": "ST-146: a verb that closes seats; whether agents are taught it is the "
                "foreman's to put up, and the skill is at its 400-token cap",
        "park": "agents are taught it by the settle protocol (taskmaster's text), and the "
                "skill is at its 400-token cap",
        "completion": "a human's shell setup, not an agent's verb; the skill is at its "
                      "400-token cap",
        "default-model": "a host setting a human opts into, not an agent's verb; the skill "
                         "is at its 400-token cap"}


def front_matter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md has no front matter"
    fields, key = {}, None
    for line in m.group(1).splitlines():
        if re.match(r"^\w+:", line):
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        elif key:
            fields[key] += " " + line.strip()
    return fields


def taught_verbs(text: str) -> list[str]:
    """The verbs the skill's bullet list teaches, in order: `- `verb ...`:`.
    A verb may hold a hyphen (`pane-state`, ST-134)."""
    return re.findall(r"^- `(?:session )?([a-z]+(?:-[a-z]+)*)[ `]", text, re.MULTILINE)


def test_the_skill_is_named_session_and_says_when_to_use_it():
    fm = front_matter(SKILL.read_text())
    assert fm["name"] == "session"
    assert "Use when" in fm["description"]


def test_the_skill_teaches_exactly_the_verbs_session_lists():
    """Every verb but the HELD ones, exactly and in order (ST-2 do-003:
    ruling B)."""
    live = [verb for verb, _ in cli.VERBS if verb not in HELD]
    taught = taught_verbs(SKILL.read_text())
    assert taught[:len(live)] == live, (taught, live)
    # Anything after them is the "not here" list, and says so.
    for verb in taught[len(live):]:
        assert verb not in live


def test_a_held_verb_is_a_live_verb_the_skill_never_names():
    text = SKILL.read_text()
    for verb, why in HELD.items():
        assert why, verb
        assert verb in dict(cli.VERBS), f"{verb} is held but no longer a verb: strike it"
        assert not re.search(rf"\b{re.escape(verb)}\b", text), f"the skill names held {verb}"


def test_a_disabled_or_retired_verb_is_only_named_as_not_here():
    text = SKILL.read_text()
    # The section's own heading, not the words: the description says "is
    # not here" too, and splitting there left the whole body in the tail,
    # unguarded (R9 review, M1).
    head, sep, tail = text.partition("These are not here")
    assert sep, "the skill has no 'These are not here' section"
    for verb in (*cli.DISABLED, "mail"):
        assert f"`session {verb}" not in head, verb
        assert f"`session {verb}" in tail, verb
    # The old session CLI's verbs this one does not have: never taught.
    for verb in ("read", "recap"):
        assert f"`session {verb}" not in text, verb
    for verb in ("read", "recap"):
        assert cli.main([verb]) == 2, f"session {verb} answers now: teach it or keep it out"


def test_the_props_it_names_are_the_props_get_lists():
    text = SKILL.read_text()
    line = re.search(r"The props are (.+?)\.\s", text, re.DOTALL)
    assert line, "the get bullet no longer lists the props"
    named = re.findall(r"[a-z]+", line.group(1).replace(" and ", ", "))
    assert named == [p for p, _ in get.PROPS]


def _skill_tokens(path: Path) -> tuple[int | None, str]:
    """`skill tokens PATH` as (count, "") or (None, why it could not be measured).

    A missing measurement is not a measurement: no binary, a non-zero exit, a
    timeout, or output with no `tokens: N` line all give None, never a guess
    (the old len/3 fallback overshot the real count by a quarter, and an empty
    stdout read as 0)."""
    exe = shutil.which("skill")
    if not exe:
        return None, "no `skill` binary on PATH"
    try:
        run = subprocess.run([exe, "tokens", str(path)], capture_output=True, text=True,
                             timeout=60, check=False)
    except subprocess.TimeoutExpired:
        return None, "`skill tokens` timed out"
    if run.returncode != 0:
        return None, f"`skill tokens` exited {run.returncode}"
    found = re.search(r"\btokens:\s*(\d+)", run.stdout)
    if not found:
        return None, "`skill tokens` printed no `tokens: N` count"
    return int(found.group(1)), ""


def _check_fold(path: Path) -> None:
    """The fold check: the line cap everywhere, the token cap where `skill` can count."""
    assert len(path.read_text().splitlines()) <= FOLD_LINES
    tokens, why = _skill_tokens(path)
    if tokens is None:
        pytest.skip(f"cannot measure the fold's token cap: {why}")
    assert 0 < tokens <= FOLD_TOKENS, tokens


def test_the_skill_fits_the_fold():
    _check_fold(SKILL)


def _fake_skill(tmp_path: Path, monkeypatch, body: str) -> None:
    """Put a `skill` script running `body` alone on PATH."""
    exe = tmp_path / "skill"
    exe.write_text(f"#!/bin/sh\n{body}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))


def _doc(tmp_path: Path) -> Path:
    doc = tmp_path / "SKILL.md"
    doc.write_text("# a skill\n")
    return doc


def test_the_fold_skips_naming_the_binary_when_skill_is_not_on_path(tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("PATH", str(bare))
    with pytest.raises(pytest.skip.Exception, match="no `skill` binary"):
        _check_fold(_doc(tmp_path))


@pytest.mark.parametrize("body", ["exit 0", "echo tokens:", "echo 'no count here'",
                                  "echo 'tokens: 12' >&2; exit 3"])
def test_the_fold_skips_when_skill_gives_no_count(tmp_path, monkeypatch, body):
    sub = tmp_path / "bin"
    sub.mkdir()
    _fake_skill(sub, monkeypatch, body)
    with pytest.raises(pytest.skip.Exception, match="`skill tokens`"):
        _check_fold(_doc(tmp_path))


def test_the_fold_still_fails_over_the_cap_and_passes_at_or_under_it(tmp_path, monkeypatch):
    sub = tmp_path / "bin"
    sub.mkdir()
    doc = _doc(tmp_path)
    _fake_skill(sub, monkeypatch, f"echo 'tokens: {FOLD_TOKENS + 1}'")
    with pytest.raises(AssertionError):
        _check_fold(doc)
    for n in (FOLD_TOKENS - 2, FOLD_TOKENS):
        _fake_skill(sub, monkeypatch, f"echo 'tokens: {n}'")
        _check_fold(doc)


def test_the_fold_reads_the_tokens_line_not_every_digit(tmp_path, monkeypatch):
    sub = tmp_path / "bin"
    sub.mkdir()
    _fake_skill(sub, monkeypatch, "echo 'file 2030 v3'; echo 'tokens: 398'")
    assert _skill_tokens(_doc(tmp_path)) == (398, "")


def test_the_line_cap_holds_even_when_the_counter_is_missing(tmp_path, monkeypatch):
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("PATH", str(bare))
    long = tmp_path / "LONG.md"
    long.write_text("x\n" * (FOLD_LINES + 1))
    with pytest.raises(AssertionError):
        _check_fold(long)


def test_the_skill_links_every_reference_and_each_link_resolves():
    """Progressive disclosure: SKILL.md points at each page under references/,
    and every references/ link it holds is a page that exists."""
    text = SKILL.read_text()
    linked = set(re.findall(r"\]\((references/[^)#\s]+)\)", text))
    shipped = {f"references/{p.name}" for p in REFERENCES.glob("*.md")}
    assert shipped, "skills/session/references/ ships no pages"
    assert linked == shipped, (linked, shipped)


def _context_help(capsys) -> str:
    try:
        cli.main(["context", "--help"])
    except SystemExit as e:
        assert e.code == 0
    return capsys.readouterr().out


def test_every_flag_the_context_reference_names_is_in_its_help(capsys):
    """A flag the page teaches that `session context --help` lacks is prose
    drift: the reference names only flags the verb has."""
    text = CONTEXT_REF.read_text()
    named = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", text))
    assert {"--parts", "--startup", "--boot", "--hooks", "--tools", "--json"} <= named, named
    helped = _context_help(capsys)
    missing = sorted(f for f in named if not re.search(rf"(?<![\w-]){f}(?![\w-])", helped))
    assert not missing, missing


def test_the_context_reference_teaches_band_as_the_code_has_it():
    """The key, its default and the band values are band.py's, word for word."""
    text = CONTEXT_REF.read_text()
    assert f"`{band.NEAR_KEY}`" in text
    # The canonical statement, not the worked example that repeats the number.
    flat = " ".join(text.split())
    assert f"(default {band.DEFAULT_NEAR_PCT}) below the threshold" in flat
    assert flat.count("(default ") == 1
    for value in band.BANDS:
        assert f"`{value}`" in text, value
    assert "`null`" in text


def test_the_plugin_version_is_the_package_version():
    """scripts/release.py mirrors pyproject's version into both files."""
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    plugin = json.loads(PLUGIN.read_text())
    market = json.loads(MARKET.read_text())
    assert plugin["name"] == "sessiontools" and plugin["version"] == version
    assert market["metadata"]["version"] == version
    (entry,) = market["plugins"]
    assert entry["name"] == plugin["name"] and entry["source"] == "./"
