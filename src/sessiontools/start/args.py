"""`session start`'s own flags and the position trap.

ARGUMENT SHAPE. Launcher flags go BEFORE the agent name; everything
AFTER it is handed on verbatim - bare words become the first message,
vendor flags reach the vendor. Past the agent name the launcher competes
for no part of the vendor's flag namespace, which is what makes "the
launcher never overloads a vendor flag" structural. `allow_abbrev=False`
keeps argparse from quietly claiming `--dry` for `--dry-run`.

Two flags of the old launcher are gone:
`--identity-from-env` and `--window-name`,
neither with a caller. `session start alice --window-name x` is therefore
vendor passthrough now, not the position trap.

The tmux flags are parsed here, so the position trap and the
`--switch`-without-tmux refusal are exactly the old ones; the placement
itself is `placement`. A resume token is claude's own flag, passed on
verbatim; `resume.detect_resume` observes it.
"""

from __future__ import annotations

import argparse
import sys

from sessiontools.start.agents import NO_AGENT
from sessiontools.start.errors import PROG

# claude's own resume spellings (`-r, --resume [value]`, `-c, --continue`):
# passthrough the launcher OBSERVES to add what claude cannot do for itself
# (`resume`).
RESUME_TOKENS = frozenset({"--resume", "-r", "--continue", "-c"})


def build_parser() -> argparse.ArgumentParser:
    """The launcher's own flag surface - deliberately tiny: every
    flag here is a name taken OUT of the vendor's namespace."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        allow_abbrev=False,
        description="Launch a coding-agent session from an agent markdown "
                    "file -- or, with no agent named, a plain vendor session.",
        epilog=(
            "Launcher flags go BEFORE the agent name. Everything after the "
            "agent name is passed on verbatim: bare words become the initial "
            "message, vendor flags reach the vendor. With no agent at all, "
            "`session start` launches a plain vendor session (a minted "
            "session id, no agent file, no key); write the agent as `-` "
            "when a message or vendor flags must follow it."
        ),
    )
    parser.add_argument(
        "agent", nargs="?", default=None,
        help="agent name, resolved to <agents-dir>/<name>.md or "
             "<agents-dir>/<name>/<name>.md (agents-dir: $AGENTS_DIR, else "
             "~/.session/agents); omit it (or write `-`) for a plain vendor "
             "session with no agent file")
    parser.add_argument(
        "rest", nargs=argparse.REMAINDER, metavar="...",
        help="initial message words and/or vendor flags, passed on verbatim")
    parser.add_argument(
        "--directory", metavar="DIR", default=None,
        help="run the session in DIR (the launch script cd's there); beats "
             "the agent's own `start: directory:`, which beats the "
             "directory the command was typed in")
    parser.add_argument(
        "--list-agents", action="store_true",
        help="print the agent names this launcher can resolve, one per "
             "line, and exit -- the same <agents-dir> `agent` resolves "
             "from, so a picker cannot offer a name that will not start")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the full vendor command with per-flag provenance and "
             "execute nothing: no launch, no session directory, no key "
             "minted, no secret resolved (the agent file's own context "
             "generators do run: their output is the command)")
    parser.add_argument(
        "--idle", action="store_true",
        help="inject no context: the agent boots self-directed and waits "
             "(front matter: start: idle: true)")
    parser.add_argument(
        "--converse", action="store_true",
        help="voice mode: instruct the agent to SPEAK its first response "
             "(front matter: start: converse: true)")
    parser.add_argument(
        "--keyed", action="store_true",
        help="mint a fresh Ed25519 session key and derive the session ID "
             "from it (front matter: start: keyed: true)")
    parser.add_argument(
        "--key-label", metavar="NAME", default=None,
        help="label the minted key with NAME instead of the agent name: it is "
             "session-key-init's --agent-name -- the principal in the signers "
             "registry (git's allowed_signers), the name printed beside a "
             "verified signature. A spawner passes the seat's mail address "
             "(voicemode+super) so signature and author agree. Only with --keyed")
    parser.add_argument(
        "--seat", metavar="SEAT", default=None,
        help="the seat this session takes (project+role, e.g. voicemode+super): "
             "written with the agent's name to <session dir>/settings/session.json "
             "at birth, so `session settings` reads the seat's layer. A spawner "
             "passes it; an agent's name is not a seat")
    parser.add_argument(
        "--first-message-file-over", metavar="BYTES", type=int, default=None,
        help="carry an initial message longer than BYTES in a 0600 file and "
             "pass a pointer instead, keeping it out of world-readable `ps` "
             "(0 = always; front matter: start: first-message-file-over:)")
    parser.add_argument(
        "--allow-second-holder", action="store_true",
        help="launch even though a live session already holds this agent: "
             "without it, a hand launch (no claude -n/--name) of "
             "an agent a live Claude Code session runs is refused, exit 3, "
             "naming the holder and its age")
    # NOT --tmux: claude ships a --tmux flag, and the launcher never shadows
    # a vendor flag (tests/test_start_args.py holds the collision guard).
    parser.add_argument(
        "--in-tmux", action="store_true",
        help="place the session in a tmux window instead of exec-ing in "
             "place (the old launcher's --tmux, renamed: claude owns --tmux)")
    parser.add_argument(
        "--tmux-target", metavar="SESSION[:WINDOW]",
        help="tmux session (matched by EXACT name) and optional window name "
             "to place into; implies --in-tmux. The window is named after "
             "the agent unless WINDOW is given; SESSION:WINDOW replaces the "
             "old launcher's window-name flag, which is gone")
    parser.add_argument(
        "--switch", action="store_true",
        help="switch the tmux client to the new window after placing it")
    parser.add_argument(
        "--no-switch", action="store_true",
        help="never switch the tmux client, even from an interactive "
             "terminal inside tmux")
    return parser


def launcher_option_strings(parser: argparse.ArgumentParser | None = None) -> set[str]:
    """Every option string the launcher owns, read off the parser - derived,
    never listed, so a new flag inherits the position trap for free."""
    parser = build_parser() if parser is None else parser
    return {option for action in parser._actions for option in action.option_strings}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse a launcher command line; pure. Two refusals live HERE, exit 2,
    because their failure mode is an ACTION:

    THE POSITION TRAP. `session start alice
    --dry-run` would pass --dry-run to the vendor and perform the LIVE
    launch the operator asked not to have. A launcher flag after the agent
    name is refused, naming the corrected form. -h/--help are exempt: every
    vendor owns them too, and `session start alice --help` reaching `claude
    --help` is harmless.

    --switch/--no-switch WITHOUT A TMUX LAUNCH: accepted-and-inert is the
    forbidden shape.
    """
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    # An empty argv is a launch, not an error: bare `session start` is a
    # plain vendor session, by design.
    args = parser.parse_args(argv)
    owned = launcher_option_strings(parser) - {"-h", "--help"}
    for token in args.rest:
        if not token.startswith("-"):
            continue
        name = token.split("=", 1)[0]
        if name in owned:
            agent = args.agent or NO_AGENT
            parser.error(
                f"{token!r} after the agent name {agent!r} is vendor passthrough, "
                "not a launcher flag -- the launch would proceed with it handed "
                "to the vendor (for --dry-run: a LIVE launch you asked not to "
                "have).\n"
                f"  Did you mean:  {PROG} {name} {agent}\n"
                "Launcher flags go BEFORE the agent name; everything after "
                "it reaches the vendor verbatim.")
    if (args.switch or args.no_switch) and not (args.in_tmux or args.tmux_target):
        parser.error(
            "--switch/--no-switch decide tmux client focus after a tmux "
            "placement; without --in-tmux/--tmux-target there is nothing "
            "to switch. Accepted-and-inert is this launcher's forbidden "
            "shape -- add --in-tmux (or --tmux-target) or drop the flag.")
    return args

