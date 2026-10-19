"""`session account list | status | use | new | add | remove`: the group's
argparse `main(argv)`.

`list`, `status`, `use` and `new` live in their own modules (`status`,
`use`, `new`); this module parses their flags and calls them. `add` and
`remove` live here.

    session account list
    session account status [--json] [--no-resets]
    session account use NAME --witness WHO [--yes]
    session account new NAME --email E
    session account add NAME --email E --source BACKEND:REF [--source ...] [--no-fp]
    session account remove NAME

**`add`** registers token sources that ALREADY EXIST. It writes no
token anywhere; `new` is the verb that makes and stores one, and
registers it through `_add`. In order, each step
passes or exits, and nothing is written before the last:

1. load the registry (refused: exit 5);
2. check NAME, the email and each `--source` (`pass`, `passage` or
   `file`), and that NAME is not taken (exit 2). Nothing has been
   resolved yet, so a typo costs no key touch;
3. check every `file:` source is a private regular file of ours, mode 0600
   (exit 2). A token another user could read is already leaked;
4. unless `--no-fp`: resolve the account ONCE, through its first source
   that works (a `pass` source costs one touch), and fingerprint the token
   in memory (every source fails: exit 4). A token already registered
   under another name is refused (exit 2): two names for one token would
   make `status` guess. A caller that already holds the token's
   fingerprint passes `known_fp` (`new`, after its
   read-back), and the resolve is skipped; the twin check still runs;
5. write the registry atomically, 0600 (exit 5 on failure).

**`remove NAME`** deletes the registry entry and nothing else: no pass
entry, no file, no ledger row. It refuses (exit 3) to remove the account
the host is on, as `status` finds it. One more case refuses: the Keychain
names no account, NAME's fp is unknown (`add --no-fp`), and the ledger's
last `use` moved the host to NAME. `use` will move to an fp-unknown
account, so the item may hold its token and only the ledger knows. When
nothing can tell (the item unreadable, an unknown fp the ledger does not
pin on NAME, no ledger row with `host_read` off), the entry is removed and
the output says the host was not known: a registry entry is one `add`
away, and refusing every `remove` on a Mac with no item would make the
verb useless there.

Two deliberate calls:

- **A symlinked registry is refused**. `save` renames a
  temp file over the target, which would replace the link with a file and
  leave the link's target stale. `add` and `remove` are the first writers,
  so the check sits in `registry` itself (`load` and `save`).
- **`add` prints no fingerprint.** The registry is the one place an fp is
  kept (as `use` and `status`); `add` says only `fp known` or `fp unknown`.

Exit codes: 0 done; 2 usage, unknown or duplicate name, a bad source; 3
`remove` of the host's account; 4 (`add`) no source resolved; 5 a refused
registry or config. `use` and `new` keep their own tables
(`use.EXIT_CODES`, `new.EXIT_CODES`).

Seams: every external is a keyword of `main` (the Keychain, the preflight,
the seat scan, the usage fetcher, the backends, the environment, HOME, and
`new`'s token store, capture and terminal), so
the end-to-end test drives every verb on fakes. Left as None, each is the
production default, chosen by the module that owns it.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.account import status as st
from sessiontools.account import use as u

if TYPE_CHECKING:
    from sessiontools.account.setuptoken import Run
    from sessiontools.account.tokenstore import TokenStore

PROG = "session account"

EXIT_OK = 0
EXIT_USAGE = reg.EXIT_USAGE             # 2
EXIT_HOST = 3                           # remove: the host is on it
EXIT_UNRESOLVED = reg.EXIT_UNRESOLVED   # 4
EXIT_REGISTRY = reg.EXIT_REGISTRY       # 5

EPILOG = """\
By default the host signs in from the Keychain item all seats read: `use`
moves every seat with no env token within about 50 s, and each one's
cache cold-starts. `use` shows that cost first, and writes only with
--witness WHO and --yes.

A host whose login shell exports CLAUDE_CODE_OAUTH_TOKEN from a file
names that file in the `session` setting account.token_file (no
default). `use` then swaps the file and never runs `security`: live
seats keep the token they launched with until they restart, and the
plan names them.

`new` runs `claude setup-token` in a masked copy of its screen, saves the
token to passage at <PREFIX>/<E>, reads it back, and registers it. PREFIX
is the `session` setting account.passage_prefix (default
claude/oauth-tokens). The token is never shown, and never on argv.

Exit codes: 0 done; 2 usage, unknown or duplicate name (or `use`: no
--witness, after the plan; or `new`: the passage entry exists, no
terminal); 3 `use` plan shown without --yes, or
`remove` of the host's account; 4 no source resolved (or `use`'s
preflight failed, or `new` got no token); 5 a refused registry, config,
backup or ledger (or `use`: a bad account.token_file or token file; or
`new`: passage not usable, or a bad prefix setting); 6 `use` failed and
restored the item or file; 7 `use` could not restore it; 8 `new` did not save the
token; 9 `new` saved it but did not register it.
"""


class AccountError(Exception):
    """`add` or `remove` refused. The message names a path, a name or a
    source; never a token or a fingerprint."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _registry_path(home, registry_path) -> Path:
    return reg.default_path(home) if registry_path is None else Path(registry_path)


# --- add -------------------------------------------------------------------------------


def add(name: str, *, email: str, sources: Sequence[str], fp: bool = True,
        known_fp: str | None = None,
        home: str | os.PathLike | None = None,
        registry_path: str | os.PathLike | None = None,
        backends: Mapping[str, reg.Backend] | None = None,
        out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`session account add NAME --email E --source B:R ...` (module doc,
    steps 1-5). `known_fp` is the token's fingerprint when the caller
    already holds it: nothing is resolved. Returns the exit code."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        if registry_path is None:
            reg.refuse_stranded(home)                           # stranded: exit 5
        account, path = _add(name, email=email, sources=sources, fp=fp, known_fp=known_fp,
                             path=_registry_path(home, registry_path), backends=backends)
    except (AccountError, reg.RegistryError) as error:
        print(f"{PROG} add: {error}", file=err)
        return error.code
    print(added_line(account, path), file=out)
    return EXIT_OK


def added_line(account: reg.Account, path: Path) -> str:
    """What `add` (and `new`) print once an account is registered: its
    sources' kinds and costs, and whether its fp is known. Never the fp."""
    return (f"added {account.name} ({account.email}): {st.sources_text(account)}; "
            f"{'fp known' if account.fp else 'fp unknown'}\n  registry: {path}")


def _add(name: str, *, email: str, sources: Sequence[str], fp: bool, path: Path,
         backends: Mapping[str, reg.Backend] | None,
         known_fp: str | None = None) -> tuple[reg.Account, Path]:
    # 1. the registry
    registry = reg.load(path)                                   # refused: exit 5
    # 2. the name, the email, the sources; the name not taken
    if not sources:
        raise AccountError("at least one --source BACKEND:REF is required", EXIT_USAGE)
    account = reg.make_account(name, email, sources)            # exit 2
    if registry.find(account.name) is not None:
        raise AccountError(f"an account named {account.name!r} is already registered "
                           f"in {path} (remove it first to change it)", EXIT_USAGE)
    # 3. a file source must be a private regular file of ours
    for source in account.sources:
        if source.backend == "file":
            why = reg._private_check(os.path.expanduser(source.ref))
            if why is not None:
                raise AccountError(f"source {source} refused: {why}", EXIT_USAGE)
    # 4. one resolve, for the fingerprint (unless the caller holds it); the
    # token stays in memory
    if known_fp is not None or fp:
        if known_fp is not None:                                # no resolve
            account = reg.make_account(account.name, account.email, account.sources,
                                       known_fp)                # a bad fp: exit 2
            source = account.sources[0]
        else:
            resolved = reg.resolve(account, backends)           # every source fails: exit 4
            account = reg.make_account(account.name, account.email, account.sources,
                                       reg.fingerprint(resolved.token))
            source = resolved.source
            del resolved
        twin = registry.by_fp(account.fp)
        if twin is not None:
            raise AccountError(f"the token at {source} is already registered "
                               f"as {twin.name!r}: one token, one name", EXIT_USAGE)
    # 5. write
    registry.add(account)
    return account, reg.save(registry, path)                    # exit 5


# --- remove ----------------------------------------------------------------------------


def remove(name: str, *, keychain: kc.Keychain | None = None,
           home: str | os.PathLike | None = None,
           registry_path: str | os.PathLike | None = None,
           config_file: str | os.PathLike | None = None,
           out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`session account remove NAME`: the registry entry only. Refuses
    (exit 3) the account the host is on. Returns the exit code."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    rpath, cpath, ledger = st._paths(home, registry_path, config_file)
    try:
        if registry_path is None:
            reg.refuse_stranded(home)                           # stranded: exit 5
        registry = reg.load(rpath)                              # exit 5
        account = registry.get(name)                            # unknown: exit 2
        config = st.load_config(cpath)                          # exit 5
        host = st.host_of(registry, config, keychain=keychain, ledger=ledger, home=home)
        if host.name is None and config.host_read and account.fp is None:
            # The Keychain named no account, and this one's fp is unknown, so
            # the item may well hold ITS token (`use` moves to an fp-unknown
            # account and records it only in the ledger). Ask the ledger.
            last = st.host_from_ledger(registry, ledger)
            if last.name == account.name:
                host = st.Who(last.name, last.email,
                              f"{last.via}; the Keychain cannot rule it out: "
                              f"{account.name}'s fp is unknown")
        if host.name == account.name:
            raise AccountError(f"{name!r} is the account the host is on ({host.via}); "
                               "`use` another account first, then remove it", EXIT_HOST)
        registry.remove(account.name)
        reg.save(registry, rpath)                               # exit 5
    except (AccountError, reg.RegistryError, st.StatusError) as error:
        print(f"{PROG} remove: {error}", file=err)
        return error.code
    print(f"removed {account.name} ({account.email}) from {rpath}; "
          "no token, pass entry or file was touched", file=out)
    if host.name is None:
        print(f"  the host account was not known ({host.via})", file=out)
    return EXIT_OK


# --- the group --------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=PROG, epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Which of the Claude accounts this Mac is on, and the switch "
                    "between them. Never prints a token.")
    verbs = p.add_subparsers(dest="verb", metavar="VERB")
    verbs.required = True

    verbs.add_parser("list", help="the registered accounts, in order; resolves nothing",
                     description="The registered accounts in order, their sources and "
                                 "cost, and which one the host is on. Resolves nothing.")

    s = verbs.add_parser("status", help="the host's account, this seat's, plan usage",
                         description="Which account the host is on, this seat's route, "
                                     "and the plan usage of the live account.")
    s.add_argument("--json", action="store_true", help="one JSON object")
    s.add_argument("--no-resets", action="store_true",
                   help="hide when each usage window resets (shown by default)")
    s.add_argument("--resets", action="store_true",
                   help="accepted and ignored: resets are shown by default")

    # The usage line is written by hand: --witness is required to write, but
    # argparse's `required=True` would exit before `use` shows the plan.
    w = verbs.add_parser("use", help="move the HOST's sign-in to NAME",
                         usage=f"{PROG} use NAME --witness WHO [--yes]",
                         description="Move the host's sign-in to NAME: the Keychain item, "
                                     "or the token file that the `session` setting "
                                     "account.token_file names. Shows the plan first; "
                                     "writes only with --witness WHO and --yes.")
    w.add_argument("name", metavar="NAME")
    w.add_argument("--witness", metavar="WHO",
                   help="who witnesses the switch (required to write; without it, use "
                        "shows the plan and exits 2)")
    w.add_argument("--yes", action="store_true", help="write the item or swap the token file (else: plan only)")

    n = verbs.add_parser("new", help="make a setup token, save it to passage, register it",
                         description="Run `claude setup-token` in a masked copy of its "
                                     "screen (sign in AS E in the browser), save the "
                                     "token to passage at PREFIX/E, read it back, and "
                                     "register NAME with it. PREFIX is the `session` "
                                     "setting account.passage_prefix, default "
                                     "claude/oauth-tokens. The token is never shown. "
                                     "Ctrl-C twice ends the run.")
    n.add_argument("name", metavar="NAME")
    n.add_argument("--email", required=True, metavar="E",
                   help="the account to sign in as; it names the passage entry")

    a = verbs.add_parser("add", help="register an existing token source as NAME",
                         description="Register token sources that already exist. "
                                     "Stores no token.")
    a.add_argument("name", metavar="NAME")
    a.add_argument("--email", required=True, metavar="E")
    a.add_argument("--source", action="append", required=True, metavar="BACKEND:REF",
                   dest="sources", help="pass:ENTRY, passage:ENTRY or file:PATH (0600); "
                                        "repeat in "
                                        "cost order, cheapest first")
    a.add_argument("--no-fp", action="store_true",
                   help="do not resolve the token now (no key touch); fp unknown")

    r = verbs.add_parser("remove", help="unregister NAME; deletes no token",
                         description="Delete NAME's registry entry. No pass entry or "
                                     "file is touched. Refuses the host's account.")
    r.add_argument("name", metavar="NAME")
    return p


def main(argv: Sequence[str] | None = None, *,
         keychain: kc.Keychain | None = None,
         preflight: u.Preflight | None = None,
         seat_scan: u.SeatScan | None = None,
         fetch: st.Fetcher | None = None,
         backends: Mapping[str, reg.Backend] | None = None,
         env: Mapping[str, str] | None = None,
         home: str | os.PathLike | None = None,
         clock: Callable[[], datetime] | None = None,
         by: str | None = None,
         store: TokenStore | None = None,
         capture: Callable[..., Run] | None = None,
         tty: tuple[int, int] | None = None,
         out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`session account VERB ...`. Returns the exit code. Every keyword is
    a seam; None is the production default. `store`, `capture` and `tty`
    (stdin's fd, stdout's fd) are `new`'s; so is `preflight`, as `use`'s."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        # argparse prints usage and help to sys.stdout/sys.stderr itself:
        # point those at the out/err seams, so every byte goes where asked.
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            args = parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as stop:          # argparse: usage (2) or -h (0)
        return stop.code if isinstance(stop.code, int) else EXIT_USAGE
    if args.verb == "list":
        return st.list_accounts(keychain=keychain, home=home, out=out, err=err)
    if args.verb == "status":
        return st.status(as_json=args.json, resets=not args.no_resets, keychain=keychain,
                         fetch=fetch, env=env, home=home, clock=clock, out=out, err=err)
    if args.verb == "use":
        return u.use(args.name, witness=args.witness, yes=args.yes, keychain=keychain,
                     preflight=preflight, seat_scan=seat_scan, home=home,
                     backends=backends, clock=clock, by=by, out=out, err=err)
    if args.verb == "new":
        from sessiontools.account import new as nw  # it imports this module

        tty_in, tty_out = (None, None) if tty is None else tty
        return nw.new(args.name, email=args.email, store=store, capture=capture,
                      preflight=preflight, environ=env, home=home,
                      tty_in=tty_in, tty_out=tty_out, out=out, err=err)
    if args.verb == "add":
        return add(args.name, email=args.email, sources=args.sources, fp=not args.no_fp,
                   home=home, backends=backends, out=out, err=err)
    return remove(args.name, keychain=keychain, home=home, out=out, err=err)
