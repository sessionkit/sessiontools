"""`session start`'s argument surface: the position trap, the refusals at
parse time, the two dropped flags, and the roads the command line names.
Ported from the old suite's "pure argument surface" and collision-guard
sections."""

from __future__ import annotations

import re

import pytest
from start_support import FIXTURES, ROOT

import sessiontools.start
from sessiontools.start import args as a
from sessiontools.start.agents import NO_AGENT
from sessiontools.start.errors import EXIT_USAGE
from sessiontools.start.vendor import VENDORS


def test_parser_defaults():
    args = a.parse_args(["bob"])
    assert args.agent == "bob"
    assert args.dry_run is False and args.idle is False
    assert args.rest == []


def test_launcher_flags_are_parsed_before_the_agent_name():
    args = a.parse_args(["--dry-run", "--idle", "bob"])
    assert (args.agent, args.dry_run, args.idle, args.rest) == ("bob", True, True, [])


def test_trailing_words_reach_rest_verbatim():
    args = a.parse_args(["scout", "review", "the", "auth", "code"])
    assert args.rest == ["review", "the", "auth", "code"]


def test_a_launcher_flag_in_passthrough_position_is_refused(capsys):
    """THE POSITION TRAP (foreman ruling, do-007): `session start bob
    --dry-run` would otherwise be a LIVE launch the operator asked not to
    have. Exit 2, naming the corrected form."""
    with pytest.raises(SystemExit) as excinfo:
        a.parse_args(["bob", "--effort", "max", "--dry-run", "hello"])
    assert excinfo.value.code == EXIT_USAGE
    err = capsys.readouterr().err
    assert "session start --dry-run bob" in err
    assert "vendor passthrough" in err


def test_every_launcher_flag_inherits_the_passthrough_guard(capsys):
    """Derived from the parser itself, so a future flag inherits the trap."""
    owned = a.launcher_option_strings() - {"-h", "--help"}
    assert "--dry-run" in owned and "--in-tmux" in owned
    for flag in sorted(owned):
        with pytest.raises(SystemExit) as excinfo:
            a.parse_args(["bob", flag])
        assert excinfo.value.code == EXIT_USAGE, flag
        assert flag in capsys.readouterr().err


def test_the_equals_form_of_a_launcher_flag_is_also_refused(capsys):
    with pytest.raises(SystemExit) as excinfo:
        a.parse_args(["bob", "--tmux-target=main"])
    assert excinfo.value.code == EXIT_USAGE
    assert "--tmux-target" in capsys.readouterr().err


def test_the_trap_names_the_dash_agent_for_a_plain_session(capsys):
    with pytest.raises(SystemExit):
        a.parse_args(["-", "--dry-run"])
    assert f"session start --dry-run {NO_AGENT}" in capsys.readouterr().err


def test_the_refusal_is_discriminating_vendor_flags_still_pass():
    args = a.parse_args(["bob", "--effort", "max", "--model=opus", "hello"])
    assert args.rest == ["--effort", "max", "--model=opus", "hello"]
    args = a.parse_args(["--dry-run", "bob", "--effort", "max"])
    assert args.dry_run is True and args.rest == ["--effort", "max"]


def test_help_in_passthrough_is_exempt_and_reaches_the_vendor():
    assert a.parse_args(["bob", "--help"]).rest == ["--help"]


def test_switch_without_a_tmux_launch_is_refused_at_parse_time(capsys):
    for flag in ("--switch", "--no-switch"):
        with pytest.raises(SystemExit) as excinfo:
            a.parse_args([flag, "bob"])
        assert excinfo.value.code == EXIT_USAGE, flag
        assert "--in-tmux" in capsys.readouterr().err
    assert a.parse_args(["--in-tmux", "--switch", "bob"]).switch is True
    assert a.parse_args(["--tmux-target", "m", "--no-switch", "bob"]).no_switch is True


def test_abbreviations_are_not_silently_claimed():
    with pytest.raises(SystemExit) as excinfo:
        a.parse_args(["--dry", "bob"])
    assert excinfo.value.code == EXIT_USAGE


def test_missing_agent_parses_to_none_and_dash_to_the_sentinel():
    assert a.parse_args([]).agent is None
    assert a.parse_args(["--dry-run"]).agent is None
    parsed = a.parse_args(["-", "hello", "--model", "x"])
    assert parsed.agent == NO_AGENT
    assert parsed.rest == ["hello", "--model", "x"]


def test_the_converse_flag_is_a_launcher_flag_before_the_agent_name():
    args = a.parse_args(["--converse", "bob"])
    assert args.converse is True and args.agent == "bob"
    with pytest.raises(SystemExit) as excinfo:
        a.parse_args(["bob", "--converse"])
    assert excinfo.value.code == EXIT_USAGE


def _converse_help() -> str:
    (action,) = [x for x in a.build_parser()._actions if "--converse" in x.option_strings]
    return " ".join(action.help.split())


def _converse_doc_item() -> str:
    text = (ROOT / "docs" / "session-start-launcher.md").read_text()
    (item,) = re.findall(r"^- `--converse`:.*?(?=^- |^$)", text, re.MULTILINE | re.DOTALL)
    return " ".join(item.split())


def _converse_usage_line() -> str:
    (line,) = [x for x in sessiontools.start.__doc__.splitlines() if "--converse" in x]
    return " ".join(line.split())


@pytest.mark.parametrize(("text", "says"), [
    (_converse_help, ("can speak", "not an order")),
    (_converse_doc_item, ("can speak", "not an order")),
    (_converse_usage_line, ("can speak",)),
], ids=["help", "launcher-doc", "module-doc"])
def test_the_converse_flag_is_described_as_a_capability_not_an_order(text, says):
    # The line the flag injects tells the agent the session can speak; it
    # never orders a spoken first response. Help, doc and the package's own
    # usage block say the same; the one-line usage entry has no room for
    # "not an order", so it states the capability alone.
    line = text()
    for phrase in says:
        assert phrase in line
    for order in ("SPEAK", "instruct", "speak the first response", "speak its first"):
        assert order not in line


def test_directory_flag_after_the_agent_name_is_the_position_trap():
    with pytest.raises(SystemExit) as caught:
        a.parse_args(["scout", "--directory", "/tmp"])
    assert caught.value.code == EXIT_USAGE


# ---------- the two dropped flags (by ruling) ----------------------------------

@pytest.mark.parametrize("flag", ["--identity-from-env", "--window-name"])
def test_the_dropped_flags_are_gone(flag, capsys):
    """Before the agent name: an unknown option, exit 2. After it: vendor
    passthrough like any flag the launcher does not own."""
    assert flag not in a.launcher_option_strings()
    with pytest.raises(SystemExit) as excinfo:
        a.parse_args([flag, "x", "bob"])
    assert excinfo.value.code == EXIT_USAGE
    assert "unrecognized arguments" in capsys.readouterr().err
    assert a.parse_args(["bob", flag, "x"]).rest == [flag, "x"]


# ---------- the command line names roads; the plan takes them ---------------

@pytest.mark.parametrize("argv, agent, rest", [
    (["-", "--resume", "abc"], "-", ["--resume", "abc"]),
    (["--in-tmux", "alice", "--resume", "abc"], "alice", ["--resume", "abc"]),
    (["alice", "-r", "abc"], "alice", ["-r", "abc"]),
    (["alice", "--continue"], "alice", ["--continue"]),
    (["alice", "hello", "-c"], "alice", ["hello", "-c"]),
    (["alice", "--resume=abc"], "alice", ["--resume=abc"]),
    (["alice"], "alice", []),
    (["--dry-run", "--keyed", "--key-label", "x+y", "alice"], "alice", []),
    (["-", "resume", "the", "work"], "-", ["resume", "the", "work"]),  # WORDS, not a token
    (["alice", "--effort", "max"], "alice", ["--effort", "max"]),
    (["--tmux-target", "", "alice"], "alice", []),   # empty: the old launcher's direct path
    (["--in-tmux", "alice"], "alice", []),
    (["--tmux-target", "proj", "--no-switch", "alice"], "alice", []),
    (["--in-tmux", "--switch", "alice"], "alice", []),
])
def test_every_road_parses_and_the_plan_reads_the_rest(argv, agent, rest):
    """The command line alone decides no road. Resume tokens, tmux flags
    and first-message words all parse, and `plan.resolve` reads them from
    `rest`. (`not_ported` stood here as the fallthrough to the bridge until
    slice 4; with every slice ported, it went with the bridge road.)"""
    args = a.parse_args(argv)
    assert (args.agent, args.rest) == (agent, rest)


# ---------- the collision guard (CA-60), claude's real --help as the oracle --

FLAG_TOKEN = re.compile(r"(?<![\w-])(--[a-zA-Z][\w-]*|-[a-zA-Z])(?![\w-])")


def vendor_help_flags(vendor: str) -> set[str]:
    return set(FLAG_TOKEN.findall((FIXTURES / f"vendor-help-{vendor}.txt").read_text()))


def launcher_flags() -> set[str]:
    return a.launcher_option_strings() - {"-h", "--help"}


def collisions(flags: set[str]) -> dict:
    return {vendor: sorted(flags & vendor_help_flags(vendor))
            for vendor in VENDORS if flags & vendor_help_flags(vendor)}


def test_every_vendor_in_the_table_has_a_help_fixture():
    for vendor in VENDORS:
        assert len(vendor_help_flags(vendor)) >= 5, vendor


def test_no_launcher_flag_shadows_any_vendor_flag():
    assert not collisions(launcher_flags())


def test_the_guard_actually_catches_a_collision():
    assert collisions(launcher_flags() | {"--model"})
