"""Every road refuses the same way. The port reads the agent file on every
road - direct, tmux placement (slice 2), resume (slice 3), declared
secrets (slice 4) - so its refusals hold on all of them in its own words:
a vendor it does not ship, a file it cannot parse, an agent it cannot
find, and THE FABLE GATE. Moved here from test_start_bridge.py when the
bridge road went (slice 4 part 2): they were written against the road to
the old launcher, and they pin what the port does in its place."""

from __future__ import annotations

import os
import subprocess

import pytest
from start_support import TIMEOUT_S, clean_env, make_agents_root, session_cmd

from sessiontools import start
from sessiontools.start import placement
from sessiontools.start.errors import EXIT_USAGE

ASK = "you@example.invalid"
RESUME_ID = "0f0f0f0f-0000-4000-8000-000000000000"
FABLE = "claude:\n  model: fable\n"
SECRETS = "environment:\n  secrets:\n    T: pass:x/y\n"


@pytest.fixture
def fable_denied(monkeypatch):
    """The runner's environment with Fable left to the gate: no override,
    no model variable of the caller's own, and the ask-the-operator
    address named outright (one expected line, no FQDN lookup)."""
    monkeypatch.delenv("AGENT_ALLOW_FABLE", raising=False)
    for name in [n for n in os.environ
                 if n.startswith(("ANTHROPIC_", "CLAUDE_CODE_")) and "MODEL" in n]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AGENT_OPERATOR_ADDR", ASK)


def never_exec(monkeypatch):
    """Every way main could launch, made fatal: any exec (the direct path's
    `execvpe`, and `execve`, which nothing should reach) and tmux placement
    (a mutant that drops the Fable gate on `--tmux-target` would otherwise
    boot the real vendor in a pane)."""
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("a refused launch exec'd"))
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("a refused launch exec'd"))
    placed = lambda *a, **k: pytest.fail("a refused launch was placed in tmux")
    monkeypatch.setattr(start, "place_in_tmux", placed)
    monkeypatch.setattr(placement, "place_in_tmux", placed)


def assert_the_ports_refusal(err: str) -> None:
    assert "REFUSING to launch model" in err
    assert f"message send {ASK}" in err
    assert "-s 'Fable request: <what for>' -f <file>" in err
    assert "session mail" not in err            # the old launcher's retired ask
    assert "security-key touch" not in err      # refused before any secret resolves


@pytest.mark.parametrize("front_matter, argv", [
    # tmux placement (the port's own road since slice 2: refused before any
    # window), Fable in the agent file's `claude:`
    (FABLE, ["--tmux-target", "proj", "scout"]),
    (FABLE, ["--tmux-target", "proj", "--no-switch", "scout"]),
    # ... in the CLI passthrough, both spellings
    ("claude:\n  effort: high\n", ["--tmux-target", "proj", "scout", "--model", "fable"]),
    ("claude:\n  effort: high\n", ["--tmux-target", "proj", "scout", "--model=claude-fable-5"]),
    # ... in the agent's declared environment
    ("environment:\n  variables:\n    ANTHROPIC_MODEL: claude-fable-5\n",
     ["--tmux-target", "proj", "scout"]),
    # resume (slice 3)
    (FABLE, ["scout", "--resume", RESUME_ID]),
    ("claude:\n  effort: high\n", ["scout", "--model", "fable", "-c"]),
    # declared secrets (slice 4): the gate refuses before `pass` is asked
    (FABLE + SECRETS, ["scout", "hello"]),
    (SECRETS, ["scout", "--model", "fable"]),
    # BARE launches: no agent file, so the CLI is the only door
    (None, ["--tmux-target", "proj", "-", "--model", "fable"]),
    (None, ["--tmux-target", "proj", "-", "hello", "--model", "claude-fable-5"]),
    (None, ["-", "--model", "fable", "--resume", RESUME_ID]),
])
def test_a_fable_launch_is_refused_on_every_road(
        monkeypatch, tmp_path, capsys, fable_denied, front_matter, argv):
    root = (make_agents_root(tmp_path, front_matter=front_matter)
            if front_matter is not None else str(tmp_path / "agents"))
    monkeypatch.setenv("AGENTS_DIR", root)
    monkeypatch.chdir(tmp_path)
    never_exec(monkeypatch)
    assert start.main(argv) == EXIT_USAGE
    assert_the_ports_refusal(capsys.readouterr().err)
    assert not (tmp_path / "agents" / "sessions").exists()


def test_a_bare_fable_launch_is_refused_on_the_hosts_environment_too(
        monkeypatch, tmp_path, capsys, fable_denied):
    """No agent, no --model: the caller's own ANTHROPIC_*MODEL* is a door."""
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-fable-5")
    never_exec(monkeypatch)
    assert start.main(["--tmux-target", "proj"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert_the_ports_refusal(err)
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL=claude-fable-5" in err


@pytest.mark.parametrize("argv_tail", [
    (["--tmux-target", "proj"], []),          # the port's own tmux road
    ([], ["--resume", "0f0f0f0f-0000-4000-8000-000000000000"]),
    ([], ["-c"]),
])
def test_a_vendor_the_port_does_not_ship_is_refused_whatever_the_flags(
        monkeypatch, tmp_path, capsys, argv_tail):
    """`start: vendor: codex` is refused by the port with one line, the same
    on every road: a tmux flag or a resume token is not a way round it.
    (The old launcher still has a codex row; nothing reaches it now.)"""
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(
        tmp_path, front_matter="start:\n  vendor: codex\n"))
    monkeypatch.chdir(tmp_path)
    never_exec(monkeypatch)
    assert start.main(["--dry-run", "scout"]) == EXIT_USAGE
    ported = capsys.readouterr().err
    assert "start: vendor: 'codex' is not a supported vendor (known: claude)" in ported
    before, after = argv_tail
    assert start.main([*before, "scout", *after]) == EXIT_USAGE
    assert capsys.readouterr().err == ported


def test_a_file_the_port_cannot_parse_is_refused_on_the_resume_road(
        monkeypatch, tmp_path, capsys):
    """Before resume was ported, `-c` sent this file to the old launcher
    unread. The port reads the file on every road, so it refuses it itself,
    naming it."""
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(
        tmp_path, front_matter="start: [not, a, mapping]\n"))
    never_exec(monkeypatch)
    assert start.main(["scout", "-c"]) == EXIT_USAGE
    assert "scout" in capsys.readouterr().err


def test_an_agent_that_cannot_be_resolved_is_refused_by_the_port(
        monkeypatch, tmp_path, capsys, fable_denied):
    """The port does not guess at an agent it cannot find, on the resume
    road too (before slice 3, `-c` sent this to the old launcher)."""
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path))
    never_exec(monkeypatch)
    assert start.main(["nosuch", "--model", "fable", "-c"]) == EXIT_USAGE
    assert "nosuch" in capsys.readouterr().err


def test_the_real_verb_answers_in_the_ports_words(tmp_path):
    """End to end, nothing stubbed: the verb as a subprocess, a dry run the
    gate refuses either way. Before C8 this bridged, and printed the old
    launcher's `session mail send ... --from <you>`."""
    env = clean_env(AGENTS_DIR=str(tmp_path), AGENT_OPERATOR_ADDR=ASK)
    result = subprocess.run(session_cmd("--dry-run", "-", "--model", "fable",
                                        "--resume", RESUME_ID),
                            capture_output=True, text=True, timeout=TIMEOUT_S,
                            cwd=str(tmp_path), env=env, check=False)
    assert result.returncode == EXIT_USAGE
    assert result.stdout == ""
    assert_the_ports_refusal(result.stderr)


@pytest.mark.parametrize("unset", ["absent", "empty"])
@pytest.mark.parametrize("tail", [[], ["--resume", RESUME_ID], ["-c"]])
def test_every_road_resolves_from_the_ruled_agents_default(monkeypatch, tmp_path, capsys,
                                                           unset, tail):
    """The default-agents-folder ruling holds on the resume road too. Measured before it: `--dry-run
    --idle alice` resolved, and the same line with `--resume <id>` went to
    the old launcher, which refused "alice.md is missing" under
    ~/.claude/agents. An empty AGENTS_DIR counts as unset."""
    monkeypatch.setenv("HOME", str(tmp_path))           # ~ is the test's own
    monkeypatch.chdir(tmp_path)
    home_agents = tmp_path / ".session" / "agents"
    home_agents.mkdir(parents=True)
    (home_agents / "alice.md").write_text(f"---\n{SECRETS}---\nYou are.\n")
    if unset == "absent":
        monkeypatch.delenv("AGENTS_DIR", raising=False)
    else:
        monkeypatch.setenv("AGENTS_DIR", "")
    never_exec(monkeypatch)
    assert start.main(["--dry-run", "--idle", "alice", *tail]) == 0
    out = capsys.readouterr().out
    assert f"agent:    alice  ({home_agents / 'alice.md'})" in out
