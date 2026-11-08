"""`session web`: the kit's web front door. PROPOSED (kit-web-shell).

    session web pair            a one-time code for /login (10 minutes, once)
    session web logout ID...    end the sessions whose id starts with each ID
    session web logout --all    end every session
    session web logout          list the live sessions; ends none
    session web rescan          find the rooms again; record them in rooms.json
    session web passkeys        list the passkeys kept for the front door
    session web passkeys --remove ID...   remove the passkeys whose id starts with ID
    session web serve [--host H] [--port N] [--name NAME]...
                                the front door (127.0.0.1 by default)

`serve` checks the address before any socket opens: 127.0.0.1, ::1 or a
literal tailnet address only. A `--name` (or SESSION_WEB_NAMES) is a host
name the front door is ALSO reached by, over https through a TLS proxy; it
binds nothing. Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

#: Names the front door is also reached by, beside --name: comma- or
#: space-separated. Merged with every --name.
NAMES_ENV = "SESSION_WEB_NAMES"

WEB_EPILOG = """the files, in ~/.sessionkit/tools/web/ (0700; each file 0600):
  pair            the pending code's SHA-256, its expiry and wrong tries
  sessions.json   the SHA-256 of each live session token: created, seen, expires
  rooms.json      the rooms the last rescan found (no secret)
  passkeys.json   each passkey's public key, name, label, added and last used
No code or token is stored in clear. A session's id is a prefix of its hash.
A passkey needs the passkey extra (sessiontools[passkey]) and a --name; it
is added from the menu, within 10 minutes of a pairing-code login.

examples:
  session web pair
  session web logout              # list the sessions, end none
  session web logout 3f2a9c       # end one
  session web logout --all
  session web rescan
  session web passkeys
  session web passkeys --remove 7c1e04
  session web serve               # http://127.0.0.1:8750/
  session web serve --name host.example.net
                                  # and https://host.example.net/, behind a TLS
                                  # proxy that forwards to http://127.0.0.1:8750"""


def build_parser() -> argparse.ArgumentParser:
    """`session web`'s parser: pure, it builds and returns."""
    from sessiontools.web import auth
    p = argparse.ArgumentParser(prog="session web", epilog=WEB_EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                description="The kit's web front door: one address, one "
                                            "login, one menu (PROPOSED, a spike).")
    from sessiontools.web import server
    sub = p.add_subparsers(dest="verb", metavar="pair|logout|rescan|passkeys|serve")
    sub.add_parser("pair", help="print a one-time code for /login",
                   description=f"Print a one-time pairing code: {auth.CODE_TTL // 60} "
                               f"minutes, single use, burned by {auth.MAX_TRIES} wrong "
                               "tries. A new code replaces a pending one. Only its hash "
                               "is stored.")
    lp = sub.add_parser("logout", help="end sessions (--all, or by id); bare: list them",
                        description="End live sessions. With no ID and no --all, list "
                                    "them and end none.")
    lp.add_argument("ids", nargs="*", metavar="ID", help="a session id, or a unique prefix")
    lp.add_argument("--all", action="store_true", help="end every session")
    sub.add_parser("rescan", help="find the rooms again (entry points, then commands on PATH)",
                   description="Find the rooms: the sessionkit.web entry points in this "
                               "environment, then each catalog room's command on PATH, asked "
                               "with `web-room`. Each is checked by one validator. What is "
                               "found is recorded in rooms.json; an absent room's hint is "
                               "printed.")
    kp = sub.add_parser("passkeys", help="list the passkeys; --remove ID removes one",
                        description="List the passkeys kept for the front door: id, label, "
                                    "the name each is for, when it was added and last used. "
                                    "A passkey is added in the browser, from the menu, within "
                                    f"{auth.CODE_TTL // 60} minutes of a pairing-code login. "
                                    "Removing one ends no session: `session web logout` does.")
    kp.add_argument("--remove", nargs="+", default=[], metavar="ID",
                    help="remove the passkeys whose id starts with each ID")
    sp = sub.add_parser("serve", help="run the front door until SIGINT or SIGTERM",
                        description="Run the front door: the login, the menu, Settings "
                                    "(read-only) and the rooms. The address is checked before "
                                    "any socket opens: 127.0.0.1 (the default), ::1 or a literal "
                                    "tailnet address; 0.0.0.0, :: and host names are refused "
                                    "there. A host name goes in --name: reached by it over "
                                    "https, through a TLS proxy.")
    sp.add_argument("--host", default="127.0.0.1", metavar="H",
                    help="the address to listen on (default 127.0.0.1)")
    sp.add_argument("--port", type=int, default=server.DEFAULT_PORT, metavar="N",
                    help=f"the port (default {server.DEFAULT_PORT})")
    sp.add_argument("--name", action="append", default=[], metavar="NAME",
                    help="a host name the front door is also reached by, over https "
                         "through a TLS proxy; repeat it for more. "
                         f"{NAMES_ENV} adds more (comma- or space-separated). A name "
                         "binds nothing; localhost and addresses are refused")
    return p


def _clock(t: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(t))


def main(argv: list[str], env: dict[str, str] | None = None) -> int:
    from sessiontools import settings
    from sessiontools.web import auth
    p = build_parser()
    a = p.parse_args(argv)
    if a.verb is None:
        p.print_help(sys.stderr)
        return 2
    if a.verb == "serve":
        from sessiontools.web import bind, server, unixhttp
        if not 0 < a.port < 65536:
            print(f"session web serve: --port {a.port}: must be 1-65535", file=sys.stderr)
            return 2
        try:
            bind.check_bind(a.host)
        except bind.BindRefused as e:
            print(f"session web serve: {e}", file=sys.stderr)
            return 2
        listed = (env if env is not None else os.environ).get(NAMES_ENV, "")
        names = tuple(n for n in [*a.name, *re.split(r"[\s,]+", listed)] if n)
        try:
            auth.Front(port=a.port, names=names)
        except ValueError as e:
            print(f"session web serve: --name or {NAMES_ENV}: {e}", file=sys.stderr)
            return 2
        try:
            return server.serve(a.host, a.port, names=names, env=env)
        except unixhttp.SocketRefused as e:
            print(f"session web serve: {e}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"session web serve: cannot listen on {a.host}:{a.port}: {e.strerror}",
                  file=sys.stderr)
            return 1
    store = auth.Store(settings.web_dir(env))
    try:
        if a.verb == "pair":
            got = store.pair()
            print(f"pairing code: {got.code}")
            print(f"type it at the front door's /login before {_clock(got.expires)}; "
                  f"it works once, and {auth.MAX_TRIES} wrong tries burn it")
            return 0
        if a.verb == "rescan":
            from sessiontools.web import rooms
            found = rooms.discover()
            rooms.save(store.directory, found)
            for r in found.rooms.values():
                print(f"{r.name}  found ({r.transport}: {r.source})")
            for e in found.absent:
                print(f"{e.name}  absent: {e.hint}")
            for source, why in found.refused:
                print(f"refused {source}: {why}", file=sys.stderr)
            return 0
        if a.verb == "passkeys":
            if a.remove:
                gone = store.remove_passkeys(a.remove)
                print(f"removed {len(gone)} passkey{'s' if len(gone) != 1 else ''}: "
                      f"{', '.join(gone)}")
                return 0
            keys = store.passkeys()
            if not keys:
                print("no passkeys: add one from the front door's menu, by its --name, "
                      "just after a pairing-code login")
            for k in keys:
                used = _clock(k["used"]) if k["used"] else "never"
                label = "".join(c for c in k["name"] if c.isprintable()) or "(no label)"
                print(f"{auth.passkey_id(k)}  {label}  for {k['rp_id']}  "
                      f"added {_clock(k['added'])}  used {used}")
            return 0
        if a.all and a.ids:
            p.error("logout: give IDs or --all, not both")
        if not a.all and not a.ids:
            rows = store.sessions()
            if not rows:
                print("no live sessions")
            for s in rows:
                print(f"{s.id}  created {_clock(s.created)}  seen {_clock(s.seen)}  "
                      f"expires {_clock(s.expires)}")
            print("session web logout: name an ID, or --all; nothing was ended", file=sys.stderr)
            return 2
        ended = store.logout(None if a.all else a.ids)
        print(f"ended {len(ended)} session{'s' if len(ended) != 1 else ''}"
              + (f": {', '.join(ended)}" if ended else ""))
        return 0
    except auth.AuthError as e:
        print(f"session web {a.verb}: {e}", file=sys.stderr)
        return 1
