"""`session`: the session command, owned by sessiontools.

At the cutover the session repo moved aside as `session-20300609`:
sessiontools provides `session`, same verbs, and the verbs are PORTED -
package code, well structured, in the right place - not copied.

    session list [--json] [--no-recap] [--no-context] [--pane NAME] [--fresh]
    session get [PROP [SEAT]] [--json]
    session context [SEAT] [--peak] [--max N] [--parts [--tools] | --startup | --boot | --hooks] [--json]
    session start ...        launch a session from an agent file
    session identity [SID]   the identity block `session start` wrote (exit 3: none)
    session agent default    the default seat's name (the vendor's word), one line
    session sound landed|given --session SID [--ref ID] [--json]
    session sound engine build|on|off|status   the sound engine
    session operator         the operator and the privileged seats, as JSON
    session statusline install [--dry-run] [--force] | check [--diff] | sample
    session pane-state show [--json] | watch [--interval S] [--once] [--dry-run] [--stats]
    session pane-state agent on|off|status   the publisher's launchd agent
    session default-model [show [--json] | list [--json] | enforce [--dry-run]]
    session default-model watch [--interval S] | unit launchd|systemd
                             pin the default every NEW claude starts on
    session ledger [SEAT] [--summary|--short|--long|--json] [--width N]
    session account list | status | use NAME | add NAME | remove NAME
    session reap [--idle DURATION] [--json] [--yes] [SHORT...]   idle seats, CLOSE or KEEP
    session park [--letter REF] [--no-promises | --promise TEXT... [--handed-on REF] | --handed-on REF]
                 [--keep-until T --why R] [--json]   this seat has settled: the record
    session park --check [SEAT] [--json]   read it back: parked, clean, and why not
    session path [LEAF [NAME]] [--json]   where tools keeps each leaf of its state
    session completion bash|zsh   a shell completion script, on stdout
    session mail ...         (retired: says where each verb went)

Two runtime dependencies, each imported lazily by the verb that needs it:
PyYAML for agent front matter (`session start`) and jsonschema for
`session settings`. The rest is argparse and the library underneath.
"""

from __future__ import annotations

import argparse
import json
import sys

from sessiontools.verbs import Group, offer

MAIL_MOVED = """session mail: retired with the session repo (2030-06-09). Its verbs live in sessionmail:
  session mail ls|tree|cat|run PATH  ->  memory ls|tree|cat|run PATH
  session mail send ...              ->  message send ...
  session mail arm / wait            ->  pager listen"""

VERBS = (
    ("list", "the live sessions and their tmux panes"),
    ("get", "read one property of a live seat"),
    ("context", "how full a seat's context window is"),
    ("start", "launch a session from an agent file"),
    ("identity", "who a launched session is, as at launch (the block session start wrote)"),
    ("agent", "the default seat's name, for scripts (default)"),
    ("settings", "a session's settings, each key with the file that set it"),
    ("sound", "signal the busy sound (landed|given); `sound engine` switches the engine"),
    ("operator", "who seats ask, and who is privileged (one line of JSON)"),
    ("statusline", "install or check Claude Code's status line, or print its sample payload"),
    ("pane-state", "each seat's BUSY / FREE / UNKNOWN, on its tmux pane (show | watch | agent)"),
    ("default-model", "pin the default every NEW claude starts on (show | list | enforce | watch | unit)"),
    ("ledger", "every file a session read and command it ran, with the tokens of each"),
    ("account", "the Claude account this Mac is on, and the switch (list | status | use)"),
    ("reap", "every seat idle past a limit, CLOSE or KEEP and why (--yes closes)"),
    ("park", "say this seat has settled, and what the checks found (--check reads it back)"),
    ("path", "where tools keeps each leaf of its state (state, sessions, ...), for peers"),
    ("completion", "a completion script for bash or zsh, on stdout"),
)


def usage() -> str:
    rows = "\n".join(f"  {v:14} {d}" for v, d in VERBS)
    return (f"Usage: session <verb> [args...]\n\nVerbs:\n{rows}\n\n"
            "Run `session <verb> --help` for that verb's options.\n")


def _list_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="session list",
                                description="The live sessions and their tmux panes.")
    p.add_argument("--json", action="store_true", help="machine-readable")
    p.add_argument("--no-recap", action="store_true", help="accepted; recaps are not ported yet")
    p.add_argument("--no-context", action="store_true",
                   help="skip CONTEXT (a transcript read per seat): '-' cells, null keys")
    p.add_argument("--pane", metavar="NAME", help="print the pane id of the session matching NAME")
    p.add_argument("--fresh", action="store_true",
                   help="compute now, not from the shared snapshot (still refreshes it); "
                        "or set SESSION_LIST_FRESH=1")
    return p


def _list(argv: list[str]) -> int:
    from sessiontools import listing, snapshot
    a = _list_parser().parse_args(argv)
    # One computation, many readers: a young snapshot serves, and
    # callers that arrive together share one computation. --pane needs no
    # CONTEXT, so any young snapshot serves it.
    found = snapshot.rows(want_context=not (a.no_context or a.pane is not None), fresh=a.fresh)
    if a.pane is not None:
        row = listing.match(found, a.pane)
        if row is None or not row.tmux_pane:
            print(f"session list: no live session with a pane matches {a.pane!r}", file=sys.stderr)
            return 1
        print(row.tmux_pane)
        return 0
    print(listing.render_json(found) if a.json else listing.render_table(found))
    return 0


def _get_parser() -> argparse.ArgumentParser:
    """`session get`'s parser; PROP completes from `get.PROPS`, the table
    `get` checks it against."""
    from sessiontools import get as _g
    p = argparse.ArgumentParser(prog="session get", description="Read one property of a live seat.")
    offer(p.add_argument("prop", nargs="?"), _g.PROPS)
    p.add_argument("seat", nargs="?")
    p.add_argument("--json", action="store_true")
    return p


def _get(argv: list[str]) -> int:
    from sessiontools import get as _g
    from sessiontools import listing
    a = _get_parser().parse_args(argv)
    if a.prop is None:
        print(_g.table())
        return 0
    try:
        print(_g.get(listing.rows(), a.prop, a.seat, a.json))
    except _g.GetError as e:
        print(e, file=sys.stderr)
        return e.code
    return 0


def _send(argv: list[str]) -> int:
    from sessiontools import listing
    from sessiontools import send as _s
    # Parsed by hand, not argparse: everything after NAME is TEXT, verbatim,
    # dashes included - except a lone `-v`, the voice-mode trigger
    # (sessionview's `speak` runs `session send "$name" -v`).
    if argv[:1] == ["--list"]:
        return _list([])
    if not argv or argv[0] in ("-h", "--help"):
        print(SEND_USAGE)
        return 0 if argv else 2
    name, rest = argv[0], argv[1:]
    text = "v" if rest == ["-v"] else " ".join(rest)
    if not text:
        print(SEND_USAGE, file=sys.stderr)
        return 2
    found = listing.rows()
    row = listing.match(found, name)
    if row is None:
        print(f"No session matching '{name}'", file=sys.stderr)
        if found:
            print("Available sessions:", file=sys.stderr)
            for r in found:
                print(f"  {r.name:30} ({r.repo})", file=sys.stderr)
        return 1
    if not row.tmux_pane or not row.tmux_socket:
        print(f"Session '{row.name}' found but has no tmux pane", file=sys.stderr)
        return 1
    if not _s.still_belongs(row.tmux_socket, row.tmux_pane, row.session.pid):
        print(f"Pane {row.tmux_pane} no longer belongs to session '{row.name}' "
              "(stale/recycled pane) - refusing to send", file=sys.stderr)
        return 1
    if _s.send_keys(row.tmux_socket, row.tmux_pane, text):
        print(f"Sent to {row.name} (pane {row.tmux_pane}): {text}")
        return 0
    print(f"Failed to send to pane {row.tmux_pane}", file=sys.stderr)
    return 1


CONTEXT_EPILOG = """\
Every number is in tokens (tok) unless it says chars. "~" marks a fitted
figure: an exact total split by a per-session fit, or counted at a rate.
"?" is unknown, never a guess. Exact rows carry no mark.
SETTLED: for an agent, the last call before its first act on work (its
first mail read or slice claim); for a human session, the last call
before the first typed input.

The views (--parts, --startup, --boot, --hooks) read the transcript a
second time; the one-line default never does, and settle never calls them.

Unlike the old session-context (retired 2030-06-09):
  - the window is the model's; unknown or exceeded prints '?' and the JSON
    carries max/pct/free as null (the old CLI assumed 1M, the confidently
    wrong number). $CONTEXT_MAX still tightens a known window; it is no
    longer --max's default.
  - a transcript with no usage rows is exit 1, not '0 / 1.00M (0%)'.
  - a <synthetic> zero reading on a seat that reached its peak is '?', as in
    session list's CONTEXT; --peak shows the peak.
  - --dir is gone: with no SEAT and no session in the environment, the newest
    transcript of the current directory's project is read.
  - SEAT is a live seat's id prefix, then the `session get` name rule, then
    any session id prefix among the transcripts."""

#: Each `session context` option: (flags, help). The help says what the
#: option does, then an example on its own line (help with no option
#: descriptions is no help). A test pins
#: that every option has both.
CONTEXT_HELP = {
    "seat": ("a live seat or a session id / prefix; default: this session.\n"
             "  session context c3a7"),
    "--peak": ("the session's high-water mark instead of now.\n"
               "  session context --peak"),
    "--max": ("assert the window size in tokens, when the model's is unknown.\n"
              "  session context --max 1000000"),
    "--parts": ("what filled the window, by category: boot, then growth since boot.\n"
                "  session context --parts"),
    "--startup": ("only the boot: what was in the window before the first reply.\n"
                  "  session context --startup c3a7"),
    "--boot": ('boot plus the reads up to "settled" (see SETTLED below).\n'
               "  session context --boot"),
    "--hooks": ("hook context by hook command: chars, share, and how much repeats.\n"
                "  session context --hooks"),
    "--tools": ('with --parts: split "Tool results" by tool, with the 5 largest.\n'
                "  session context --parts --tools"),
    "--json": ('machine-readable: the same rows, with "exact" or "fitted" on each.\n'
               "  session context --parts --json | jq '.rows[] | select(.fitted)'"),
}

#: The views, each a flag: at most one per run.
CONTEXT_VIEWS = ("parts", "startup", "boot", "hooks")


def _seat_transcript(seat: str | None):
    """`(ident, path)`: SEAT's transcript, resolved as `session context` and
    `session ledger` both resolve it. A live seat's id prefix, then the
    `session get` name rule, then any session id prefix among the
    transcripts; with no SEAT, this session; with no session either, the
    newest transcript of the current directory's project. PATH is None when
    there is no transcript."""
    from pathlib import Path

    from sessiontools import listing, window
    from sessiontools.claude_code import session_id
    ident = seat or session_id()
    live = listing.rows() if ident else []
    row = listing.match_id_first(live, ident) if ident else None
    if row is not None:
        return ident, window.transcript_path(row.session.cwd, row.session.id)
    if ident:
        return ident, window.transcript_path(None, ident)
    return ident, window.newest_transcript(str(Path.cwd()))


def _context_parser() -> argparse.ArgumentParser:
    """`session context`'s parser."""
    p = argparse.ArgumentParser(prog="session context", epilog=CONTEXT_EPILOG,
                                formatter_class=argparse.RawTextHelpFormatter,
                                description="How full a seat's context window is, and "
                                            "what filled it.")
    h = CONTEXT_HELP
    p.add_argument("seat", nargs="?", metavar="SEAT", help=h["seat"])
    p.add_argument("--peak", action="store_true", help=h["--peak"])
    p.add_argument("--max", type=int, metavar="N", help=h["--max"])
    views = p.add_mutually_exclusive_group()
    for view in CONTEXT_VIEWS:
        views.add_argument(f"--{view}", action="store_true", help=h[f"--{view}"])
    p.add_argument("--tools", action="store_true", help=h["--tools"])
    p.add_argument("--json", action="store_true", help=h["--json"])
    return p


def _context(argv: list[str]) -> int:
    """The old `session-context` report, on sessiontools.window: the window
    is the MODEL's (never a guessed 1M), or --max N when you assert one.
    With a view flag, what filled it, from `context_parts`."""
    from sessiontools import window
    p = _context_parser()
    a = p.parse_args(argv)
    if a.max is not None and a.max <= 0:
        p.error(f"--max must be a positive number of tokens, not {a.max}")
    view = next((v for v in CONTEXT_VIEWS if getattr(a, v)), None)
    if a.tools and view != "parts":
        p.error('--tools splits --parts\' "Tool results" row: use it with --parts')
    if view and a.peak:
        p.error(f"--peak is the one-line view's; --{view} reads the whole session")
    if view not in (None, "parts") and a.max is not None:
        p.error(f"--max sizes the window, which --{view} does not print; "
                "use it alone or with --parts")
    ident, path = _seat_transcript(a.seat)
    if path is None:
        print(f"session context: no transcript for {ident or 'this directory'!r} "
              f"(under {window.projects_dir()})", file=sys.stderr)
        return 1
    u = window.scan(path)
    if u.usage_rows == 0:
        print(f"session context: {path.name} has no usage rows yet", file=sys.stderr)
        return 1
    if view:
        return _context_view(view, path, tools=a.tools, json_out=a.json, max_=a.max)
    used = u.peak if a.peak else u.used
    size = a.max if a.max else window.resolve_window(u.model, used)
    zero_tail = not a.peak and u.used == 0 and u.peak > 0
    pct = used * 100 // size if size and not zero_tail else None
    free = size - used if size and not zero_tail else None
    if a.json:
        # band: the same helper as --parts --json, on this line's own used
        # (the peak with --peak); a synthetic-zero tail has no band.
        band = None if zero_tail else _context_band(used, path.stem)
        print(json.dumps({"used": used, "peak": u.peak, "max": size, "pct": pct,
                          "free": free, "session": path.stem, "model": u.model,
                          "band": band}))
        return 0
    tag = " [peak]" if a.peak else ""
    fmt = window.fmt_tokens
    if zero_tail:
        print(f"Context: ? - the last reading is a <synthetic> zero; the peak was "
              f"{fmt(u.peak)} (--peak)")
    elif size is None:
        print(f"Context: {fmt(used)} / ? - {window.why_unknown(u.model, used)} "
              f"(--max N asserts one){tag}")
    else:
        print(f"Context: {fmt(used)} / {fmt(size)} ({pct}%) · {fmt(free)} free{tag}")
    return 0


def _context_band(used: int | None, sid: str) -> str | None:
    """`band` for `--parts --json` and the bare `--json`: None (JSON
    null) with no settle threshold, so no settings are read then. With one,
    the near cut comes from the settings cascade; a session with no settings
    directory reads only the host and template layers, and stderr says so
    (otherwise "no such session" and "the default" look alike). `sessiontools.band` sits outside `context_parts`, so the
    bare path never imports the fold."""
    from sessiontools import band, settings
    if used is None or band.threshold() is None:
        return None
    try:
        _sid, note = settings.resolve_sid(sid)
    except settings.SettingsError as e:
        note = str(e).splitlines()[0]
    if note:
        print(f"session context: band: {note}; the near cut is "
              f"{band.near_pct(sid):g}% of the settle threshold", file=sys.stderr)
    return band.band_for(used, sid)


def _context_view(view: str, path, *, tools: bool, json_out: bool,
                  max_: int | None) -> int:
    """One of `--parts` (with `--tools`), `--startup`, `--boot`, `--hooks`:
    computed by `context_parts`, rendered by `context_parts.render`. Text
    names the session by its first 8 characters; JSON carries the full id."""
    from sessiontools import window
    from sessiontools.context_parts import boot as boot_mod
    from sessiontools.context_parts import fold, parts, render
    from sessiontools.context_parts import hooks as hooks_mod
    from sessiontools.context_parts import tools as tools_mod
    sid = path.stem
    who = sid if json_out else sid[:8]
    if view == "startup":
        s = boot_mod.startup(path)
        out = render.startup_json(s, session=who) if json_out else render.startup_text(s, session=who)
    elif view == "boot":
        b = boot_mod.boot(path)
        out = render.boot_json(b, session=who) if json_out else render.boot_text(b, session=who)
    elif view == "hooks":
        hk, pt = hooks_mod.hooks(path), parts(path)
        out = (render.hooks_json(hk, session=who, parts=pt) if json_out
               else render.hooks_text(hk, session=who, parts=pt))
    else:
        f = fold(path)
        t = tools_mod.tools(f) if tools else None
        pt = t.parts if t is not None else parts(f)
        used = pt.anchors.used if pt.anchors.calls else None
        size = max_ if max_ else window.resolve_window(pt.model, used)
        s = boot_mod.startup(path)
        if json_out:
            out = render.parts_json(pt, session=who, window=size, startup=s, tools=t,
                                    band=_context_band(used, sid))
        else:
            out = render.parts_text(pt, session=who, window=size, startup=s, tools=t)
    print(json.dumps(out) if json_out else out)
    return 0


def _start(argv: list[str]) -> int:
    try:
        from sessiontools.start import main as start_main  # the ported launcher
    except ImportError as e:
        # The port has landed, so a failed import here is a broken install,
        # never an unported verb. PyYAML is the runtime dependency start
        # needs (jsonschema is settings') and the likeliest thing missing.
        # Exit 1: the launch failed (2 is usage or a refusal, and this is
        # neither).
        missing = "PyYAML" if e.name == "yaml" else (e.name or "a module")
        print(f"session start: broken install - {missing} will not import ({e}); "
              "reinstall sessiontools with its dependencies: "
              "uv tool install --editable --reinstall CHECKOUT", file=sys.stderr)
        return 1
    return start_main(argv)


SEND_USAGE = """Usage:
  session send NAME TEXT...   type TEXT into the session matching NAME, then Enter
  session send NAME -v        send 'v' + Enter (the voice-mode trigger)
  session send --list         the live sessions (session list)

NAME matches part of a session's name or repo, or a prefix of its id; the
first match wins. TEXT is joined with spaces and typed literally."""


#: `session send` is DISABLED (2030-12-08): run by accident, it typed into an
#: agent's pane. Kept in case it is wanted back, but not in the help and not
#: available as a command: if it is there, it is too inviting. So it is in neither the help
#: nor HANDLERS; `_send` and send.py stay, unwired. Bringing it back: delete
#: its DISABLED entry (checked FIRST in main, so it wins), add "send": _send
#: to HANDLERS, and restore its VERBS row.
DISABLED = {
    "send": ("session send: disabled (2030-12-08: it typed into an agent's pane "
             "by accident). To reach an agent, mail it: message send ADDRESS -s SUBJECT --no-body"),
}

def _settings_parser() -> argparse.ArgumentParser:
    """`session settings`'s parser; TOOL's help names `settings.TOOLS`, and
    TOOL completes from it."""
    from sessiontools import settings
    p = argparse.ArgumentParser(prog="session settings",
                                description="A session's settings, merged down the cascade "
                                            "(template, host, agent, seat, session), each key "
                                            "with the file that set it.")
    p.add_argument("sid", nargs="?", metavar="SID", help="a session id; default: this session")
    offer(p.add_argument("tool", nargs="?", metavar="TOOL",
                         help=f"one tool ({', '.join(settings.TOOLS)}); default: every tool found"),
          settings.TOOLS)
    p.add_argument("--agent", metavar="NAME", help="the agent layer to read; default: the session's own record")
    p.add_argument("--seat", metavar="NAME", help="the seat layer to read; default: the session's own record")
    p.add_argument("--json", action="store_true", help="machine-readable: values, origins, locked, problems per tool")
    return p


def _settings(argv: list[str]) -> int:
    """`session settings <sid> [tool]`: the cascade computed, never on disk.
    New paths only: a tool that has not
    moved reads "not moved"."""
    from sessiontools import settings
    p = _settings_parser()
    a = p.parse_args(argv)
    sid = a.sid
    if not sid:
        from sessiontools.claude_code import session_id
        sid = session_id()
    if not sid:
        p.error("no session id: name one, or run inside a session")
    try:
        sid, note = settings.resolve_sid(sid)
    except settings.SettingsError as e:
        print(f"session settings: refused: {e}", file=sys.stderr)
        return 2
    if note:
        print(f"session settings: {note}", file=sys.stderr)
    try:
        agent, seat = settings.identity(sid)
        agent = a.agent or agent
        seat = a.seat or seat
        names = [a.tool] if a.tool else settings.tools(sid, agent, seat)
    except settings.SettingsError as e:
        print(f"session settings: refused: {e}", file=sys.stderr)
        return 2
    rc = 0
    out: dict[str, dict] = {}
    for name in names:
        try:
            res = settings.resolve(name, sid, agent, seat)
        except settings.SettingsError as e:
            print(f"session settings: refused: {e}", file=sys.stderr)
            return 2
        if res.problems:
            rc = 2
        if a.json:
            out[name] = {"moved": res.moved, "values": res.values, "origins": res.origins,
                         "locked": res.locked, "problems": res.problems, "notes": res.notes,
                         "sources": [{"layer": s.layer, "path": str(s.path)} for s in res.sources]}
        else:
            sys.stdout.write(settings.report(res))
    if a.json:
        print(json.dumps({"session": sid, "agent": agent, "seat": seat, "tools": out}, indent=1))
    elif not names:
        print(f"no settings at any new path for session {sid}")
    return rc


SOUND_EPILOG = """\
The pager says the moment; sessiontools decides whether anything plays.
Exit 0 always, 2 for a usage error; nothing on
stdout unless --json, which prints {"event", "decision", "why", "ref"} for
debugging. `finished` (the ears) is held and not built yet.

`session sound engine build|on|off|status` builds and switches the resident
sound engine; `session sound engine --help` says more."""


def _sound_parser() -> argparse.ArgumentParser:
    """`session sound`'s parser for the signals; EVENT's help names
    `busy.EVENTS`, and EVENT completes from it (a held event is in neither).
    `engine` never reaches it (see `SOUND`)."""
    from sessiontools import busy
    p = argparse.ArgumentParser(prog="session sound", epilog=SOUND_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Signal the busy sound: `landed` (the operator's line is "
                                            "held, unread, for this busy seat) plays it when "
                                            "the session's busy.trigger is `landed`; `given` "
                                            "stops it.")
    offer(p.add_argument("event", metavar="EVENT", help=f"one of: {', '.join(busy.EVENTS)}"),
          busy.EVENTS)
    p.add_argument("--session", required=True, metavar="SID", help="the seat's session id")
    p.add_argument("--ref", metavar="ID", help="the line's Message-ID, for the trail only")
    p.add_argument("--json", action="store_true", help="print the decision and why")
    return p


#: `session sound`: `engine` is dispatched by hand, before the signals'
#: parser, which has a positional EVENT and so cannot hold a subparser.
SOUND = Group(subs={"engine": "sessiontools.sound.service:build_parser"},
              rest="sessiontools.cli:_sound_parser")


def _sound(argv: list[str]) -> int:
    """`session sound landed|given --session SID`: the busy sound's signals.
    Always 0, except 2 for a usage error: the sender is a hook-like caller
    (the pager), and a signal must never break it.

    `session sound engine build|on|off|status` is dispatched first, by
    `SOUND.subs`, so the signals' parser never sees it."""
    if argv[:1] and argv[0] in SOUND.subs:
        from sessiontools.sound import service
        return service.main(argv[1:])
    from sessiontools import busy
    p = _sound_parser()
    a = p.parse_args(argv)
    try:
        decision, why = busy.decide(a.event, a.session, a.ref)
    except ValueError as e:
        p.error(str(e))
    except Exception as e:  # noqa: BLE001 - exit 0 always, but say so under --json
        decision, why = busy.IGNORED, f"failed: {type(e).__name__}"
    if a.json:
        print(json.dumps({"event": a.event, "decision": decision, "why": why, "ref": a.ref}))
    return 0


OPERATOR_EPILOG = """\
Read from ~/.sessionkit/tools/settings/session.json (template and host layers):
  {"operator": {"address": ..., "mailbox": ..., "privileged": [...]}}
Prints exactly {"operator", "mailbox", "privileged"}; keys may be added,
never renamed or removed. Nothing configured prints nulls and []. Exit 0
always, 2 for a usage error. AGENT_OPERATOR_ADDR moves "operator" and
grants no privilege."""


def _operator_parser() -> argparse.ArgumentParser:
    """`session operator`'s parser: no options."""
    p = argparse.ArgumentParser(prog="session operator", epilog=OPERATOR_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="The operator (who seats ask) and the privileged "
                                            "seats (who may act as administrators), as one "
                                            "line of JSON.")
    return p


def _operator(argv: list[str]) -> int:
    """`session operator`: the cross-kit read of sessiontools.read_operator,
    so a tool in another language needs no import."""
    from sessiontools import operator
    _operator_parser().parse_args(argv)
    print(json.dumps(operator.as_json_object(operator.read_operator())))
    return 0


def _statusline(argv: list[str]) -> int:
    """`session statusline install|check|sample`: stdlib only, imported
    here and nowhere else, so the other verbs never pay for it."""
    from sessiontools import statusline
    return statusline.main(argv)


def _pane_state(argv: list[str]) -> int:
    """`session pane-state show|watch|agent`: the pane
    signal's reader, its publisher, and the launchd agent that runs it.
    Imported here and nowhere else."""
    from sessiontools import pane_state
    return pane_state.main(argv)


def _default_model(argv: list[str]) -> int:
    """`session default-model show|list|enforce|watch|unit`: the pin of settings.json's
    "model". Imported here and nowhere else."""
    from sessiontools import default_model_cli
    return default_model_cli.main(argv)


LEDGER_EPILOG = """\
Figures are tokens unless marked: tiktoken cl100k_base, a ±10% estimate of
Claude's tokenizer, counted over the text that entered the window. '?' is
unknown, never a guess; '-' is not applicable.

SEAT resolves as `session context`'s does. Exit 1: no transcript. Exit 2: a
usage error, or the counter cannot run (no uv, or no cl100k_base vocabulary:
it is never downloaded here; run `tokens` once on this host)."""


def _ledger_parser() -> argparse.ArgumentParser:
    """`session ledger`'s parser; --width's help names the render default."""
    from sessiontools.ledger import render
    p = argparse.ArgumentParser(prog="session ledger", epilog=LEDGER_EPILOG,
                                formatter_class=argparse.RawTextHelpFormatter,
                                description="Every file read and command run by one session, "
                                            "each with the tokens it put\nin the window.")
    p.add_argument("seat", nargs="?", metavar="SEAT",
                   help="a live seat or a session id / prefix; default: this session.\n"
                        "  session ledger c3a7")
    views = p.add_mutually_exclusive_group()
    views.add_argument("--summary", dest="view", action="store_const", const="summary",
                       help="(default) counts and totals by kind, the 10 largest, every repeat.\n"
                            "  session ledger c3a7")
    views.add_argument("--short", dest="view", action="store_const", const="short",
                       help="one line per item, largest first: tokens and name. Cheap to read.\n"
                            "  session ledger --short")
    views.add_argument("--long", dest="view", action="store_const", const="long",
                       help="one row per item in transcript order, every figure.\n"
                            "  session ledger --long c3a7")
    views.add_argument("--json", dest="view", action="store_const", const="json",
                       help="one JSON record per line, one per item; "
                            "concatenates across sessions.\n"
                            "  session ledger --json a1 >> all.jsonl; "
                            "session ledger --json b2 >> all.jsonl")
    p.add_argument("--width", type=int, metavar="N",
                   help=f"--short only: cut commands to N characters "
                        f"(default {render.DEFAULT_WIDTH}).\n"
                        "  session ledger --short --width 40 c3a7")
    return p


def _ledger(argv: list[str]) -> int:
    """`session ledger`: every file one session read and every
    command it ran, each with the tokens it put in the window. The views
    are `ledger.render`'s; the counter is `ledger.count`'s, run once."""
    from sessiontools import ledger, window
    from sessiontools.ledger import count, render
    p = _ledger_parser()
    a = p.parse_args(argv)
    view = a.view or render.DEFAULT_VIEW
    if a.width is not None:
        if view != "short":
            p.error("--width applies to --short only")
        if a.width <= 0:
            p.error(f"--width must be a positive number of characters, not {a.width}")
    ident, path = _seat_transcript(a.seat)
    if path is None:
        print(f"session ledger: no transcript for {ident or 'this directory'!r} "
              f"(under {window.projects_dir()})", file=sys.stderr)
        return 1
    try:
        its = ledger.items(path)
    except count.CounterError as e:
        print(f"session ledger: {e}", file=sys.stderr)
        return e.code
    except OSError as e:
        print(f"session ledger: cannot read {path}: {e}", file=sys.stderr)
        return 1
    kw = {"width": a.width} if a.width is not None else {}
    _write_out(render.VIEWS[view](its, session=path.stem, **kw))
    return 0


def _write_out(text: str) -> None:
    """Write TEXT to stdout. A reader that stops early (`| head`) is not an
    error: no traceback, and not exit 1, which means "no transcript". The
    rest goes to devnull, so the interpreter's own flush at exit stays quiet
    (the recipe in Python's signal docs)."""
    import os
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def _identity(argv: list[str]) -> int:
    """`session identity [SID]`: the identity block, byte for byte; exit 3
    when the session has none. Stdlib only (`sessiontools.identity_block`),
    so it never loads PyYAML."""
    from sessiontools import identity_block
    return identity_block.main(argv)


def _account(argv: list[str]) -> int:
    """`session account list|status|use|add|remove`: the group is
    `sessiontools.account.main`. Imported here and nowhere else: the
    package's registry reads `start.secrets`, and importing `start` loads
    PyYAML, which `import sessiontools.cli` must never do (AGENTS.md rule
    3; tests/test_cli.py pins both). A failed import is a broken install,
    exit 1, as `session start`'s is."""
    try:
        from sessiontools.account import main as account_main
    except ImportError as e:
        missing = "PyYAML" if e.name == "yaml" else (e.name or "a module")
        print(f"session account: broken install - {missing} will not import ({e}); "
              "reinstall sessiontools with its dependencies: "
              "uv tool install --editable --reinstall CHECKOUT", file=sys.stderr)
        return 1
    return account_main(argv)


def _reap(argv: list[str]) -> int:
    """`session reap`: idle seats, CLOSE or KEEP. Imported here and
    nowhere else: reap reads task front matter through `start.agentfile`,
    and `import sessiontools.cli` must never load yaml (AGENTS.md rule 3;
    tests/test_cli.py pins it)."""
    from sessiontools import reap
    return reap.main(argv)


def _park(argv: list[str]) -> int:
    """`session park`: a seat records that it has settled; `--check` reads it
    back. Imported here and nowhere else: its checks read task front matter
    through `session reap`, and `import sessiontools.cli` must never load
    yaml (AGENTS.md rule 3; tests/test_cli.py pins it)."""
    from sessiontools import park
    return park.main(argv)


AGENT_USAGE = """Usage: session agent default

  default   print the default seat's name, the vendor's own word, and nothing
            else: one line on stdout, nothing on stderr, exit 0. Safe in a
            shell substitution: message setup --box "$(session agent default)"
"""

AGENT_DEFAULT_EPILOG = """\
Reads no host state: no ps, no box, no lock, no settings. The word comes
from the launcher's vendor table, so it always agrees with the name a bare
`session start` gives its seat. Exit 0, or 2 for a usage error."""


def _agent_default_parser() -> argparse.ArgumentParser:
    """`session agent default`'s parser: no options."""
    return argparse.ArgumentParser(prog="session agent default", epilog=AGENT_DEFAULT_EPILOG,
                                   formatter_class=argparse.RawDescriptionHelpFormatter,
                                   description="Print the default seat's name (the vendor's "
                                               "own word) and a newline, and nothing else.")


#: `session agent`: its one sub-verb is dispatched by hand, as statusline's
#: are, so completion and the dispatcher read the same table.
AGENT = Group(subs={"default": "sessiontools.cli:_agent_default_parser"})


def _agent(argv: list[str]) -> int:
    """`session agent default`: the default seat's word, for the readers that
    cannot import sessiontools (phone, the kit's setup, the wake word). The
    word is imported here and nowhere else: `sessiontools.start` loads
    PyYAML, and `import sessiontools.cli` never may (tests/test_cli.py)."""
    if argv[:1] and argv[0] in AGENT.subs:
        _agent_default_parser().parse_args(argv[1:])
        try:
            from sessiontools.start.default_seat import word
        except ImportError as e:
            missing = "PyYAML" if e.name == "yaml" else (e.name or "a module")
            print(f"session agent: broken install - {missing} will not import ({e}); "
                  "reinstall sessiontools with its dependencies: "
                  "uv tool install --editable --reinstall CHECKOUT", file=sys.stderr)
            return 1
        sys.stdout.write(f"{word()}\n")
        return 0
    if argv[:1] in (["-h"], ["--help"]):
        sys.stdout.write(AGENT_USAGE)
        return 0
    if argv:
        print(f"session agent: unknown subcommand {argv[0]!r}", file=sys.stderr)
    sys.stderr.write(AGENT_USAGE)
    return 2


COMPLETION_EPILOG = """install, one line per shell:
  zsh, in ~/.zshrc after compinit:
    eval "$(session completion zsh)"
  bash, in ~/.bashrc (any bash, 3.2 included):
    eval "$(session completion bash)"
  bash 4.2 or later with the bash-completion package, once:
    session completion bash > ~/.local/share/bash-completion/completions/session
The script is static: TAB runs no command. Regenerate the file form after
upgrading sessiontools; the eval form regenerates at every shell start."""


def _completion_parser() -> argparse.ArgumentParser:
    """`session completion`'s parser; SHELL's choices are the shells
    `sessiontools.completion` writes a script for."""
    from sessiontools import completion
    p = argparse.ArgumentParser(prog="session completion", epilog=COMPLETION_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="Print a completion script for SHELL on stdout, "
                                            "built from session's own verbs, sub-verbs and "
                                            "options.")
    p.add_argument("shell", metavar="SHELL", choices=tuple(completion.SHELLS),
                   help=f"one of: {', '.join(completion.SHELLS)}")
    return p


def _completion(argv: list[str]) -> int:
    """`session completion bash|zsh`: the script, from every verb's parser.
    Building those parsers imports account's and start's packages, which
    load PyYAML, so a failed import is a broken install: exit 1, the module
    named, and no script at all. A script that silently lacked a verb would
    be worse than none."""
    a = _completion_parser().parse_args(argv)
    from sessiontools import completion
    try:
        text = completion.script(a.shell)
    except ImportError as e:
        missing = "PyYAML" if e.name == "yaml" else (e.name or "a module")
        print(f"session completion: broken install - {missing} will not import ({e}); "
              "no script was written; reinstall sessiontools with its dependencies: "
              "uv tool install --editable --reinstall CHECKOUT", file=sys.stderr)
        return 1
    _write_out(text)
    return 0


def _path(argv: list[str]) -> int:
    """`session path [LEAF [NAME]] [--json]`, and `session path migrate`
    (sessiontools.migrate, by paths.GROUP): a view onto the settings
    resolvers (sessiontools.paths), so a peer in any language asks rather
    than copies a path."""
    from sessiontools import paths
    return paths.main(argv)


HANDLERS = {"list": _list, "get": _get, "context": _context, "start": _start, "agent": _agent,
            "identity": _identity,
            "settings": _settings, "sound": _sound, "operator": _operator,
            "statusline": _statusline, "pane-state": _pane_state,
            "default-model": _default_model, "ledger": _ledger,
            "account": _account, "reap": _reap, "park": _park,
            "path": _path, "completion": _completion}

#: Each verb's parser, as a `sessiontools.verbs` reference: a builder, or a
#: `Group` for a verb whose first word is dispatched by hand. Each handler
#: parses with the parser its entry builds. The values are strings, so this
#: table imports nothing (account's and start's builders load PyYAML, and
#: `import sessiontools.cli` never may). A test pins its keys to HANDLERS'.
PARSERS = {"list": "sessiontools.cli:_list_parser",
           "get": "sessiontools.cli:_get_parser",
           "context": "sessiontools.cli:_context_parser",
           "start": "sessiontools.start.args:build_parser",
           "identity": "sessiontools.identity_block:build_parser",
           "agent": "sessiontools.cli:AGENT",
           "settings": "sessiontools.cli:_settings_parser",
           "sound": "sessiontools.cli:SOUND",
           "operator": "sessiontools.cli:_operator_parser",
           "statusline": "sessiontools.statusline:GROUP",
           "pane-state": "sessiontools.pane_state:GROUP",
           "default-model": "sessiontools.default_model_cli:build_parser",
           "ledger": "sessiontools.cli:_ledger_parser",
           "account": "sessiontools.account.cli:parser",
           "reap": "sessiontools.reap:build_parser",
           "park": "sessiontools.park:build_parser",
           "path": "sessiontools.paths:GROUP",
           "completion": "sessiontools.cli:_completion_parser"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    verb = args.pop(0) if args else ""
    if verb in ("", "-h", "--help"):
        sys.stdout.write(usage())
        return 0
    if verb == "mail":
        print(MAIL_MOVED, file=sys.stderr)
        return 2
    if verb in DISABLED:
        print(DISABLED[verb], file=sys.stderr)
        return 2
    handler = HANDLERS.get(verb)
    if handler is None:
        print(f"session: unknown verb {verb!r}\n", file=sys.stderr)
        sys.stderr.write(usage())
        return 2
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
