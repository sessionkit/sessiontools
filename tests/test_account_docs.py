"""The account docs say what the code says, in words a stranger can use.

`docs/account.md`, the README's account lines, the `session account`
epilog and `verify --help` are public: they name no person, host, real
address or tracker id. Where the docs quote the code (an exit-code table,
a plan line, the STALE wording) the quote is checked against the code, so
the two cannot drift. Nothing here runs a verb against a real account: the
help text comes from the parsers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from sessiontools.account import cli
from sessiontools.account import keychain as kc
from sessiontools.account import profile as pf
from sessiontools.account import registry as reg
from sessiontools.account import status as st
from sessiontools.account import tokenfile as tf
from sessiontools.account import use as u
from sessiontools.account import verify as vf

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "account.md"


def _text(path: Path = DOC) -> str:
    return path.read_text(encoding="utf-8")


def _flat(path: Path = DOC) -> str:
    return " ".join(_text(path).split())


def _section(title: str) -> str:
    """The body of the `## TITLE` section of docs/account.md, flattened."""
    lines = _text().splitlines()
    start = lines.index(f"## {title}")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    return " ".join(" ".join(lines[start + 1:end]).split())


def _help(*argv: str) -> str:
    parser = cli.parser()
    if not argv:
        return parser.format_help()
    verbs = next(a for a in parser._actions if a.dest == "verb")
    return verbs.choices[argv[0]].format_help()


# --- the sections -----------------------------------------------------------------------


def test_the_modes_section_names_both_modes_and_what_moves():
    text = _section("Which mode is this host in?")
    assert "Keychain host" in text and "token-file host" in text.lower()
    assert "/login" in text
    assert "live seats keep" in text.lower() or "keep the token they launched with" in text
    assert "account.token_file" in text


def test_the_proof_section_names_both_proofs_and_the_display_profile():
    text = _section("Proving which account bills")
    assert "session account verify" in text and "status line" in text
    assert "one 1-token" in text.lower() or "one-token" in text
    assert "7d" in text and "reset" in text
    assert "`~/.claude.json`" in text and "`oauthAccount`" in text
    assert "`claude auth status`" in text and "/status" in text
    assert "does not change" in text


def test_the_verify_section_tabulates_the_codes_exit_table_verbatim():
    lines = _text().splitlines()
    for code, meaning in vf.EXIT_CODES.items():
        assert f"| {code} | {meaning} |" in lines, (code, meaning)


def test_the_verify_section_shows_the_codes_model_and_headers():
    text = _section("`verify [NAME]`: which account a token bills")
    assert f"`{vf.MODEL}`" in text
    for window in vf.WINDOWS:
        for part in ("utilization", "reset"):
            assert f"{vf.HEADER_PREFIX}{window}-{part}" in text
    assert "<- the 7d reset identifies the account" in text
    assert "never" in text and "token" in text


def test_the_docs_use_the_stale_wording_and_drop_the_old_one():
    text = _flat()
    assert "reset since written" not in text
    assert "STALE: window ended" in text and "figure not shown" in text
    assert "age not reported by the exporter" in text
    assert "`stale`" in text and "`age_seconds`" in text
    assert "m old)" in text                       # the per-figure age, as printed


def test_the_docs_show_the_profile_line_and_its_json():
    text = _flat()
    assert "profile: work@example.com (~/.claude.json, display only)" in text
    assert st.DIFFERS_MARK in text
    assert "`matches_host`" in text
    assert "CLAUDE_CONFIG_DIR" in _section("Proving which account bills")


def test_the_token_file_plan_example_carries_the_note_line_verbatim():
    lines = _text().splitlines()
    assert pf.note_line(pf.Profile("work@example.com")) in lines
    assert u.START_NOTICE in lines


def test_the_readme_lists_verify_with_the_account_verbs():
    assert "`session account verify`" in _flat(ROOT / "README.md")


def test_the_epilog_names_the_display_profile_and_verify():
    epilog = " ".join(cli.EPILOG.split())
    assert "~/.claude.json" in epilog and "claude auth status" in epilog
    assert "verify" in epilog


# --- the lead: available, in use, why /login moves every seat ---------------------------


def _home_form(path: Path, home: Path) -> str:
    """PATH as the docs spell it: `~/...`, from a HOME the test chose."""
    return "~/" + path.relative_to(home).as_posix()


def test_the_help_leads_with_where_the_accounts_come_from_and_which_is_in_use(tmp_path):
    from sessiontools import settings

    folder = _home_form(settings.accounts_dir({"HOME": str(tmp_path)}), tmp_path)
    help_text = _help()
    lead = help_text.split("\n\n")[1]                  # the first block after usage
    assert lead == cli.DESCRIPTION.rstrip("\n")
    lines = lead.splitlines()
    assert len(lines) <= 5 and all(len(line) <= 80 for line in lines)
    flat = " ".join(lead.split())
    assert "Never prints a token" in flat
    assert f"Available: the accounts `new` or `add` put in {folder}/." in flat
    assert "In use: the host's token, fingerprinted and looked up in that list" in flat
    assert "Keychain item" in flat and tf.SETTING_KEY in flat
    assert "/login moves every seat on a Keychain host" in flat
    assert "no env token" in flat
    # the facts come before the verbs and the epilog
    assert help_text.index("Available:") < help_text.index("positional arguments")


def test_the_doc_leads_with_the_same_three_facts(tmp_path):
    from sessiontools import settings

    registry = _home_form(settings.accounts_dir({"HOME": str(tmp_path)}), tmp_path)
    registry += f"/{reg.REGISTRY_NAME}"
    lines = _text().splitlines()
    assert lines[0].startswith("# ") and lines[1] == ""
    body = lines[2:lines.index("", 2)]                  # the first block under the title
    bullets = " ".join(" ".join(body).split()).split("- **")[1:]
    assert len(bullets) == 3, bullets
    available, in_use, login = bullets
    assert available.startswith("Available:**") and "`new` or `add`" in available
    assert f"`{registry}`" in available and "Never the token" in available
    assert in_use.startswith("In use:**") and "fingerprint" in in_use
    assert f"`{tf.SETTING_KEY}`" in in_use and "Keychain item" in in_use
    assert "`unknown (not in registry)`" in in_use
    assert login.startswith("Why `/login` moves every seat on a Keychain host:**")
    assert f"`{kc.SERVICE}`" in login and "no env token" in login
    assert "token-file host" in login and "only new seats move" in login


def test_the_status_unknown_wording_the_doc_quotes_is_the_codes():
    registry = reg.Registry()
    assert st.host_from_keychain(registry, kc.FakeKeychain(kc.render_item("tok-x"))).via \
        .endswith("unknown (not in registry)")


# --- neutral public text ----------------------------------------------------------------

#: A host name from a real fleet, a tracker id, an address not at example.com.
#: A host name is matched by its SHAPE, so this file spells none: one or two
#: lowercase letters and a number (a version such as `v1` excepted), or a
#: MacBook short name (`mb` and the model letter, Air or Pro).
_HOST = re.compile(r"\b(?:(?!v[0-9])[a-z]{1,2}[0-9]+|mb[ap])\b")
#: Tracker codes only: `SHA-256` and `UTF-8` are not ids.
_TRACKER = re.compile(r"\b(?:ST|TM|SK|SM|SESH|GIT|PHONE)-[0-9]+\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)")


@pytest.mark.parametrize("planted", ["signed on zq7", "built on z7", "from qz042",
                                     "on an mbp", "on an mb" + "a"])
def test_the_host_guard_catches_a_planted_host_shape(planted):
    """A planted sample of each shape fires, so a clean public text is a real
    pass: two letters or one and a number, a long number, both MacBook names
    (the Air's is joined at run time, so this file spells no host)."""
    assert _HOST.findall(planted)


@pytest.mark.parametrize("word", ["POST /v1/messages", "Not in v1", "sha256", "utf8",
                                  "sk-ant-oat01-xxx", "mbox", "abc12"])
def test_the_host_guard_passes_words_that_are_not_hosts(word):
    assert _HOST.findall(word) == []


def _readme_account_lines() -> str:
    return "\n".join(line for line in _text(ROOT / "README.md").splitlines()
                     if "account" in line.lower())


@pytest.mark.parametrize("where", ["docs/account.md", "README account lines", "epilog",
                                   "account -h", "verify --help", "status --help",
                                   "use --help"])
def test_public_text_names_no_person_host_or_tracker_id(where):
    text = {"docs/account.md": lambda: _text(),
            "README account lines": _readme_account_lines,
            "epilog": lambda: cli.EPILOG,
            "account -h": _help,
            "verify --help": lambda: _help("verify"),
            "status --help": lambda: _help("status"),
            "use --help": lambda: _help("use")}[where]()
    assert _HOST.findall(text) == []
    assert _TRACKER.findall(text) == []
    assert {domain for domain in _EMAIL.findall(text)} <= {"example.com"}
