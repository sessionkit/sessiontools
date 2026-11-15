"""`session commands [TARGET]`: the parser, the four views and the exits.

TARGET is a seat (every recorded session of it) or a session id or prefix;
with none, this session. Rows go to stdout and the one summary line to
stderr, so a pipe gets rows only.

Exit 0: read, whatever it found. Exit 1: no seat and no session matches
TARGET. Exit 2: the line is wrong (a WHEN or a pattern that cannot be read).
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys

EPILOG = """\
TARGET: a seat, for every session recorded as that seat on this host
(`session list --past SEAT` lists them), or a session id or its prefix.
WHEN: 7d, 48h, 30m, 2030-06-09 or 2030-06-09T05:00 (local time).

What is cut from a command: the VAR=value settings in front of it, a
here-document's body, redirections, bare `cd`, and the shell's own words
(if, for, case). `bash -c`, `uv run` and `ssh HOST` are followed to what
they run, marked [bash -c], [uv run], [ssh:HOST]. A `*` after the session
marks a subagent's call.

Exit 1: nothing matches TARGET. Exit 2: a usage error."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="session commands", epilog=EPILOG,
        formatter_class=argparse.RawTextHelpFormatter,
        description="The Bash commands a seat's sessions ran, cut down to the programs.")
    p.add_argument("target", nargs="?", metavar="TARGET",
                   help="a seat, or a session id / prefix; default: this session.\n"
                        "  session commands doctor --since 24h")
    views = p.add_mutually_exclusive_group()
    views.add_argument("--count", dest="view", action="store_const", const="count",
                       help="one line per program and verb: how often it ran.\n"
                            "  session commands cora --since 7d --count")
    views.add_argument("--raw", dest="view", action="store_const", const="raw",
                       help="each command exactly as it was written.\n"
                            "  session commands 3ce1 --raw")
    views.add_argument("--json", dest="view", action="store_const", const="json",
                       help="one JSON record per program run, one per line.")
    p.add_argument("--since", metavar="WHEN", help="only calls at or after WHEN.")
    p.add_argument("--until", metavar="WHEN", help="only calls before WHEN.")
    p.add_argument("--grep", metavar="RE",
                   help="only calls with a program whose 'program verb' matches\n"
                        "RE (--count, --json: only those programs).\n"
                        "  session commands wimo --count --grep '^(pager|ears)'")
    p.add_argument("--width", type=int, default=110, metavar="N",
                   help="default view: cut each line to N characters (110; 0: whole).")
    return p


def main(argv: list[str]) -> int:
    from sessiontools import commands
    from sessiontools.claude_code import session_id
    from sessiontools.commands import shell
    p = build_parser()
    a = p.parse_args(argv)
    view = a.view or "lines"
    target = a.target or session_id()
    if not target:
        p.error("no TARGET, and this is not a session: name a seat or a session id")
    try:
        grep = re.compile(a.grep) if a.grep else None
        since = commands.utc(a.since) if a.since else None
        until = commands.utc(a.until) if a.until else None
    except re.error as e:
        p.error(f"--grep {a.grep!r} is not a pattern: {e}")
    except ValueError:
        p.error("WHEN is 7d, 48h, 30m, 2030-06-09 or 2030-06-09T05:00")
    if a.width < 0:
        p.error(f"--width must be 0 or more, not {a.width}")
    label, rows = commands.resolve(target)
    if not rows:
        print(f"session commands: no seat and no session matches {target!r}", file=sys.stderr)
        return 1
    total = shown = 0
    tally: collections.Counter[str] = collections.Counter()
    out = sys.stdout
    try:
        for call in commands.calls(rows, since, until):
            total += 1
            progs = commands.kept(call, grep)
            if not progs:
                continue
            shown += 1
            mark = "*" if call.subagent else " "
            if view == "raw":
                out.write(f"--- {commands.local(call.ts)} {call.session[:8]}{mark}\n"
                          f"{call.command}\n")
            elif view == "lines":
                line = " ; ".join((f"[{q.via}] " if q.via else "") + shell.show(q.words, argmax=48)
                                  for q in progs)
                if a.width and len(line) > a.width:
                    line = line[:a.width - 1] + "…"
                out.write(f"{commands.local(call.ts)} {call.session[:8]}{mark} {line}\n")
            else:
                for q in progs:
                    if grep is not None and not grep.search(q.label):
                        continue
                    if view == "count":
                        tally[q.label] += 1
                    else:
                        out.write(json.dumps({
                            "seat": label, "session": call.session, "ts": call.ts,
                            "program": q.name, "verb": q.verb, "via": q.via,
                            "words": list(q.words), "call": call.id,
                            "subagent": call.subagent}) + "\n")
        for name, n in tally.most_common():
            out.write(f"{n:7}  {name}\n")
        out.flush()
    except BrokenPipeError:               # `| head`: not an error
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    counted = f"{sum(tally.values())} programs in " if view == "count" else ""
    print(f"{counted}{shown} of {total} Bash calls, {len(rows)} "
          f"session{'' if len(rows) == 1 else 's'} of {label}", file=sys.stderr)
    return 0
