"""The configuration docs, pinned to the README template and to the code.

docs/configuration.md is what sessiontools configures, one section per
part; docs/settings.md is every key it reads, with its default and the
layers it is read from today. The README's Configuration section links the
first, and its Logs section ends on the Health line. Inside these two
pages a link into this repo is relative or an #anchor, and a link to
another repo is a full URL on HEAD; the README's new links are relative
too. What a page claims about a key, a default, a path, a variable or
a layer is checked against the code here, so a change to one shows up as a
failure in the other.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sessiontools import (
    band,
    busy,
    default_model,
    default_model_watch,
    migrate,
    operator,
    pane_state,
    pane_state_agent,
    settings,
    snapshot,
)
from sessiontools.account import new as account_new
from sessiontools.account import status as account_status
from sessiontools.sound import service as sound_service
from sessiontools.start import claude_settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
CONFIG_DOC = "docs/configuration.md"
SETTINGS_DOC = "docs/settings.md"
NEW_DOCS = (CONFIG_DOC, SETTINGS_DOC)
REPO = "https://github.com/sessionkit/sessiontools"
KIT_GUIDE = "https://github.com/sessionkit/sessionkit/blob/HEAD/docs/settings.md"

INLINE_LINK = re.compile(r"\]\(([^)\s]+)\)")
REFERENCE_DEF = re.compile(r"^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
REFERENCE_USE = re.compile(r"\]\[([^\]]+)\]")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _section(text: str, title: str) -> str:
    """The body of the `## title` section, up to the next `## ` or the
    reference definitions at the end."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |^\[[^\]]+\]: |\Z)", text,
                      re.MULTILINE | re.DOTALL)
    assert match, f"no '## {title}' section"
    return match.group(1)


def _h2s(text: str) -> list[str]:
    return re.findall(r"^## (.+)$", FENCE.sub("", text), re.MULTILINE)


def _definitions(text: str) -> dict[str, str]:
    return dict(re.findall(r"^\[([^\]]+)\]:\s*(\S+)", text, re.MULTILINE))


def _targets(text: str) -> list[str]:
    """Every link target in TEXT: inline, and each reference it uses."""
    defs = _definitions(text)
    used = [defs[label] for label in REFERENCE_USE.findall(text) if label in defs]
    return INLINE_LINK.findall(text) + used


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped,
    spaces to hyphens."""
    return re.sub(r"[^a-z0-9 _-]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(text: str) -> set[str]:
    return {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", FENCE.sub("", text), re.MULTILINE)}


def _row(text: str, key: str) -> list[str]:
    """The cells of the table row whose first cell is `KEY`."""
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells and cells[0] in (f"`{key}`", key):
            return cells
    raise AssertionError(f"no table row for `{key}`")


# --- the README -------------------------------------------------------------------


def test_readme_sections_follow_the_template_order():
    # The template: Installation, Usage, Components, Configuration, Running,
    # Logs, Development. Here Docs stands where Components does, and the
    # two new sections come after it, in the template's order.
    h2s = _h2s(README)
    assert h2s[-3:] == ["Docs", "Configuration", "Logs"], h2s


def test_readme_configuration_links_the_page_relatively():
    section = _section(README, "Configuration")
    assert CONFIG_DOC in _targets(section + _defs_block(README))


def _defs_block(text: str) -> str:
    return "\n" + "\n".join(f"[{k}]: {v}" for k, v in _definitions(text).items()) + "\n"


@pytest.mark.parametrize("title", ["Configuration", "Logs"])
def test_readme_new_sections_link_this_repo_relatively(title):
    targets = _targets(_section(README, title) + _defs_block(README))
    for target in targets:
        assert not target.startswith((REPO + "/", "/")), target
        if not target.startswith("https://"):
            assert (ROOT / target.split("#")[0]).exists(), target


def _host_file(tool: str) -> str:
    """TOOL's host file as the docs spell it, `~/...`, derived from
    settings.host_dir: no test spells the directory's shape itself."""
    fake = Path("/home-of-test")
    return f"~/{(settings.host_dir(tool, {'HOME': str(fake)})[0] / f'{tool}.json').relative_to(fake)}"


def test_readme_configuration_names_each_settings_file():
    section = _section(README, "Configuration")
    for tool in ("session", busy.TOOL, claude_settings.TOOL):
        assert f"`{_host_file(tool)}`" in section, tool


def test_readme_logs_names_each_log_the_code_writes(tmp_path):
    section = _section(README, "Logs")
    env = {"HOME": str(tmp_path)}
    home = str(tmp_path)
    logs = [sound_service.log_path(env), pane_state_agent.log_path(env),
            settings.log_dir(settings.PACKAGES["session"], env) / "reap.log",
            default_model_watch.log_path(env),
            default_model.state_dir(env) / "log.jsonl"]
    for log in logs:
        shown = "~" + str(log)[len(home):]
        assert f"`{shown}`" in section, shown
    assert f"/log/{settings.SETTINGS_LOG}`" in section
    status_line = (ROOT / "src/sessiontools/statusline/statusline.sh").read_text(encoding="utf-8")
    assert "statusline-log.jsonl" in status_line
    assert "`~/.config/session/statusline-log.jsonl`" in section


def _flat(text: str) -> str:
    """TEXT with each run of whitespace as one space, so a phrase may wrap."""
    return " ".join(text.split())


def _tilde(path: Path, home: Path) -> str:
    return "~/" + str(path.relative_to(home))


def test_readme_logs_names_the_old_locations(tmp_path):
    # ST-263: the old logs stay where they were, and the README says where.
    section = _flat(_section(README, "Logs"))
    assert f"`{_tilde(settings.legacy_log_dir({'HOME': str(tmp_path)}), tmp_path)}/`" in section
    assert "`$SESSION_STATE_DIR/reap.log`" in section
    assert "left in place" in section
    # An installed plist loads again at every login with its old path: only a
    # rewritten plist moves a service's log, so "loaded again" is not enough.
    assert "until its plist is written again" in section


def test_readme_uninstall_never_deletes_from_library_logs(tmp_path):
    section = _section(README, "Uninstall")
    legacy = _tilde(settings.legacy_log_dir({"HOME": str(tmp_path)}), tmp_path)
    for line in section.splitlines():
        if line.lstrip().startswith("rm "):
            assert "Library/Logs" not in line, line
    flat = _flat(section)
    assert "The logs are left in place" in flat
    assert f"`{legacy}/`" in flat
    assert f"`{_tilde(settings.log_dir(settings.PACKAGES['session'], {'HOME': str(tmp_path)}), tmp_path)}/`" in flat


def _items(text: str) -> list[str]:
    """TEXT cut into paragraphs and bullets."""
    return [x for x in re.split(r"\n\s*\n|\n(?=\s*- )", text) if x.strip()]


def test_the_docs_name_library_logs_only_as_the_old_location():
    pages = [("README.md", README)] + [
        (str(p.relative_to(ROOT)), p.read_text(encoding="utf-8"))
        for p in sorted((ROOT / "docs").glob("*.md"))]
    for name, text in pages:
        for item in _items(text):
            if "Library/Logs" in item:
                assert "left in place" in _flat(item), (name, item)


def test_readme_logs_says_how_to_follow_one():
    assert "`tail -F" in _section(README, "Logs")


def test_readme_logs_ends_on_the_health_line():
    lines = [x for x in _section(README, "Logs").strip().splitlines() if x.startswith("- **")]
    assert lines[-1].startswith("- **Health:** `session list`"), lines[-1]


def test_the_health_command_is_a_real_verb():
    from sessiontools import cli
    assert "list" in cli.HANDLERS


# --- the two pages: links ---------------------------------------------------------


@pytest.mark.parametrize("name", NEW_DOCS)
def test_docs_link_this_repo_relatively_or_by_anchor(name):
    text = _read(name)
    targets = _targets(text) + REFERENCE_DEF.findall(text)
    assert targets, f"{name} has no links: the check would pass on nothing"
    for target in targets:
        assert not target.startswith((REPO + "/", "/")), f"{name}: {target}"
        if target.startswith("https://github.com/sessionkit/") and "/blob/" in target:
            assert "/blob/HEAD/" in target, f"{name}: another repo, on HEAD: {target}"


@pytest.mark.parametrize("name", NEW_DOCS)
def test_docs_use_no_wiki_links(name):
    assert "[[" not in _read(name)


@pytest.mark.parametrize("name", NEW_DOCS)
def test_every_reference_used_is_defined(name):
    text = _read(name)
    defs = _definitions(text)
    for label in REFERENCE_USE.findall(text):
        assert label in defs, f"{name}: [{label}] is not defined"


@pytest.mark.parametrize("name", NEW_DOCS)
def test_docs_anchors_resolve(name):
    text = _read(name)
    for target in REFERENCE_DEF.findall(text) + INLINE_LINK.findall(text):
        if target.startswith("#"):
            assert target[1:] in _anchors(text), f"{name}: {target}"
        elif "#" in target and not target.startswith("https://"):
            path, _, anchor = target.partition("#")
            doc = (ROOT / name).parent / path
            assert anchor in _anchors(doc.read_text(encoding="utf-8")), f"{name}: {target}"


@pytest.mark.parametrize("name", NEW_DOCS)
def test_docs_links_into_this_repo_name_real_files(name):
    relative = [t for t in REFERENCE_DEF.findall(_read(name))
                if not t.startswith(("https://", "#"))]
    assert relative, f"{name} has no relative links: the check would pass on nothing"
    for target in relative:
        path = (ROOT / name).parent / target.split("#")[0]
        assert path.is_file(), f"{name}: {target}"


def test_the_anchor_check_can_fail():
    # A zero is a receipt only when the same check is shown to hit.
    assert "no-such-heading" not in _anchors(_read(SETTINGS_DOC))
    assert _slug("The `session` tool's keys") == "the-session-tools-keys"
    assert "the-session-tools-keys" in _anchors(_read(SETTINGS_DOC))


def test_configuration_settings_h2_links_the_settings_page():
    text = _read(CONFIG_DOC)
    section = _section(text, "Settings")
    assert "[docs/settings.md][settings]" in section
    assert _definitions(text)["settings"] == "settings.md"


@pytest.mark.parametrize("name", NEW_DOCS)
def test_docs_link_the_kit_guide_absolutely(name):
    assert KIT_GUIDE in _definitions(_read(name)).values()


def test_settings_names_the_spec_in_words_and_does_not_link_it():
    text = _read(SETTINGS_DOC)
    assert "the session\nsettings specification" in text
    assert "openspec" not in text


# --- the two pages: facts, against the code -----------------------------------------


def test_every_variable_the_configuration_page_names_is_read_by_the_code():
    code = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for d in ("src", "hooks") for p in (ROOT / d).rglob("*") if p.is_file())
    names = set(re.findall(r"`\$?([A-Z][A-Z0-9_]{3,})`", _read(CONFIG_DOC)))
    names = {n for n in names if not n.endswith("_")}
    assert len(names) > 20, names
    for name in sorted(names):
        assert name in code, name


#: The package's own variables that are test fences, not configuration:
#: tests/conftest.py sets each one so no test touches launchd or a device.
TEST_FENCES = frozenset({
    pane_state_agent.FENCE_ENV,                # SESSIONTOOLS_PANE_STATE_NO_LAUNCHCTL
    sound_service.FENCE_ENV,                   # SESSIONTOOLS_SOUND_NO_LAUNCHCTL
    migrate.FENCE_ENV,                         # SESSIONTOOLS_MIGRATE_NO_PROBE
    "SESSIONTOOLS_SOUND_NO_DEVICE",            # engine.swift
})


def test_the_configuration_page_names_every_sessiontools_variable():
    # The page says each variable belongs to one part, below; the README
    # says the page lists each one. Pinned for the package's own
    # namespace: a SESSIONTOOLS_ variable the code reads and the page
    # leaves out fails here.
    code = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for d in ("src", "hooks") for p in (ROOT / d).rglob("*")
                     if p.is_file() and p.suffix != ".pyc")
    read = set(re.findall(r"\bSESSIONTOOLS_[A-Z0-9_]*[A-Z0-9]\b", code)) - TEST_FENCES
    assert "SESSIONTOOLS_STATUSLINE_LOG_MAX" in read, read
    page = _read(CONFIG_DOC)
    for name in sorted(read):
        assert f"`{name}`" in page, name


def test_the_pages_name_no_disabled_verb():
    # A disabled verb exits 2: a knob documented for it configures nothing.
    from sessiontools import cli
    assert "send" in cli.DISABLED
    for name in (*NEW_DOCS, "README.md"):
        text = _read(name)
        for verb in cli.DISABLED:
            assert f"`session {verb}" not in text, f"{name}: session {verb}"


def test_the_session_keys_match_the_code():
    text = _section(_read(SETTINGS_DOC), "The `session` tool's keys")
    assert _row(text, band.NEAR_KEY)[2] == f"`{band.DEFAULT_NEAR_PCT}`"
    assert _row(text, "list.max_age")[2] == f"`{snapshot.DEFAULT_MAX_AGE:g}`"
    listeners = _row(text, f"{pane_state.SETTINGS_BLOCK}.{pane_state.LISTENERS_KEY}")
    assert listeners[2] == f"`{json.dumps(list(pane_state.listeners({'HOME': '/nowhere'})))}`"
    interval = _row(text, f"{pane_state.SETTINGS_BLOCK}.{pane_state.INTERVAL_KEY}")
    assert interval[2] == f"`{pane_state.DEFAULT_INTERVAL}`"
    for key in (operator.OPERATOR_ADDRESS_KEY, operator.OPERATOR_MAILBOX_KEY,
                operator.OPERATOR_PRIVILEGED_KEY):
        _row(text, f"{operator.OPERATOR_SETTINGS_BLOCK}.{key}")
    assert _row(text, account_new.PREFIX_KEY)[2] == f"`{account_new.DEFAULT_PREFIX}`"
    from sessiontools.account import limits, tokenfile
    _row(text, limits.SETTING_KEY)
    _row(text, tokenfile.SETTING_KEY)
    _row(text, "home")
    _row(text, "home_dir")
    pin = _row(text, f"{default_model.SETTINGS_BLOCK}.{default_model.SETTINGS_KEY}")
    assert pin[2] == "none: nothing is pinned"
    assert pin[4] == "template and host"


def test_the_sounds_keys_are_the_schemas():
    schema = json.loads((ROOT / "settings/sounds.schema.json").read_text(encoding="utf-8"))
    keys = ["enabled"] + [f"{block}.{k}" for block in ("busy", "stop")
                          for k in schema["properties"][block]["properties"]]
    assert set(schema["properties"]) == {"enabled", "busy", "stop"}
    assert "stop.file" in keys
    text = _section(_read(SETTINGS_DOC), "The `sounds` tool's keys")
    for key in keys:
        _row(text, key)
    assert settings.X_ENV not in json.dumps(schema["properties"])
    trigger = schema["properties"]["busy"]["properties"]["trigger"]["enum"]
    assert all(f"`{word}`" in _row(text, "busy.trigger")[1] for word in trigger)


def test_the_claude_code_keys_are_the_ones_rendered():
    text = _section(_read(SETTINGS_DOC), "The `claude-code` tool's keys")
    for key in claude_settings.RENDERED:
        if key != claude_settings.STATUS_LINE:
            _row(text, key)
    for sub in ("type", "command", *claude_settings.STATUS_LINE_KEYS):
        _row(text, f"{claude_settings.STATUS_LINE}.{sub}")


def test_the_settings_files_the_page_names_are_the_readers():
    text = _section(_read(SETTINGS_DOC), "The files")
    for tool in ("session", busy.TOOL, claude_settings.TOOL):
        assert f"`{_host_file(tool)}`" in text, tool


def test_the_account_config_defaults_match_the_code():
    text = _section(_read(CONFIG_DOC), "`session account`")
    assert set(account_status.CONFIG_KEYS) == {"host_read", "usage_url"}
    assert f"`{account_status.USAGE_URL}`" in text
    assert f"`{account_status.CONFIG_NAME}`" in text


# --- the layers: what the page says is read today ------------------------------------

SID = "0b0e0c0a-0000-4000-8000-000000000001"


@pytest.fixture
def tree(tmp_path):
    """A HOME whose session layer sets keys the template-and-host readers
    must not see, and the cascade readers must."""
    env = {"HOME": str(tmp_path), "SESSIONS_DIR": str(tmp_path / ".session" / "sessions")}
    layer = tmp_path / ".session" / "sessions" / SID / "settings"
    layer.mkdir(parents=True)
    (layer / "session.json").write_text(json.dumps({
        "context": {"near_pct": 7},
        "list": {"max_age": 9},
        "pane_state": {"interval": 5},
        "operator": {"address": "someone@example.com"},
    }), encoding="utf-8")
    (layer / "sounds.json").write_text(json.dumps({"enabled": True}), encoding="utf-8")
    return env


def test_near_pct_is_read_from_the_session_layer(tree):
    assert band.near_pct(SID, tree) == 7


def test_sounds_are_read_from_the_session_layer(tree):
    assert busy.values(SID, tree).get("enabled") is True


def test_the_template_and_host_readers_ignore_the_session_layer(tree):
    layer = Path(tree["HOME"]) / ".session" / "sessions" / SID / "settings" / "session.json"
    data = json.loads(layer.read_text(encoding="utf-8"))
    layer.write_text(json.dumps({**data, "model": {"default": "opus"}}), encoding="utf-8")
    assert default_model.setting(tree).value is None
    assert snapshot.max_age(tree) == snapshot.DEFAULT_MAX_AGE
    assert pane_state.interval(tree) == pane_state.DEFAULT_INTERVAL
    assert operator.read_operator(tree, resolve_fqdn=lambda: "").address is None


def test_the_layers_table_says_which_readers_read_which_layers():
    text = _section(_read(SETTINGS_DOC), "Who reads which layers")
    assert _row(text, "`session`: `operator.*`, `list.max_age`, `pane_state.*`, "
                      "`account.*`, `model.default`")[1] == "template and host only"
    assert _row(text, "`session`: `context.near_pct`")[1] == "all five"
    assert _row(text, "`sounds`: every key")[1] == "all five"
    assert _row(text, "`claude-code`: `statusLine`")[1] == "template and host only"
