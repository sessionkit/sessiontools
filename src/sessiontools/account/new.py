"""`session account new NAME --email E`: make a setup token, save it to
passage, register it.

The three hand steps of adding an account, as one verb. Each step passes
or exits, every cheap failure comes first, and **nothing is written before
the token is proven**:

1. stdin and stdout are terminals, at least `MIN_COLS` wide (exit 2). A
   terminal shorter than setup-token's screens is WARNED about here, before
   the browser opens: the mirror stops at the pty's last row;
2. NAME and E are valid (`make_account`'s rules; E also path-safe), the
   registry loads (exit 5), and NAME is not registered (exit 2);
3. passage is usable (exit 5), and neither `<store>/<ENTRY>.age` nor a
   folder by that name exists (exit 2). ENTRY is `<PREFIX>/<E>`. PREFIX is
   the `session` tool's `account.passage_prefix` setting (PREFIX_KEY), read
   through the settings cascade's template and host layers before step 2.
   Unset, it is DEFAULT_PREFIX, `claude/oauth-tokens`; a value that is not
   a folder path inside the store, or a layer file that cannot be read, is
   exit 5. Then a `setup-token-*`
   folder left by a killed run is scanned for a token's shape, and removed;
4. `claude setup-token` runs on a private pty, the person signs in AS E,
   and the token is read off its masked mirror into memory (exit 4);
5. the token has the setup-token shape, leakscan's `TOKEN` (exit 4);
6. preflight: `use.claude_preflight(token)` answers `accountok` (exit 4).
   Its child gets setup-token's allowlist only, plus the two names the
   preflight sets (`narrowing_runner`), so a pass proves the token, not a
   credential or provider switch the person's environment held;
7. the entry is checked absent again, then `passage insert -m ENTRY` with
   the token on stdin (exit 8);
8. it is read back through `passage:`'s own resolver and compared by
   SHA-256 (`registry.same_token`) (exit 8, the entry named). The read-back
   runs whenever the entry exists, after a failed or timed-out insert too
   (a hung commit leaves a good entry);
9. `add`'s library call registers `passage:ENTRY` with the fingerprint
   already known, so it costs no second decrypt (exit 9: saved and
   verified, not registered; the `add` line to finish it is printed).

`new` never overwrites a passage entry and never deletes one: after exit
8 a person removes the bad entry by hand (`passage rm ENTRY`).

No message, exception or line here carries a token or a fingerprint. Text
quoted from passage is scrubbed against the token itself, not only its
shape, so a piece of the token cut from its prefix
cannot be quoted either.

Exit codes are `EXIT_CODES`: 0 saved, read back,
registered; 2 usage; 4 no token; 5 refused before the sign-in; 8 not
saved; 9 saved and verified, not registered. A signal (SIGTERM, SIGHUP)
during the sign-in is 128 + the signal, as a shell reports it.
"""

from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from sessiontools.account import cli
from sessiontools.account import registry as reg
from sessiontools.account import setuptoken as st
from sessiontools.account import tokenstore as ts
from sessiontools.account import use as u

PROG = "session account new"

EXIT_OK = 0
EXIT_USAGE = reg.EXIT_USAGE             # 2
EXIT_NO_TOKEN = reg.EXIT_UNRESOLVED     # 4
EXIT_REFUSED = reg.EXIT_REGISTRY        # 5
EXIT_NOT_SAVED = ts.EXIT_NOT_SAVED      # 8
EXIT_NOT_REGISTERED = 9

#: The exit-code table, the one place it is written whole.
EXIT_CODES: dict[int, str] = {
    EXIT_OK: "saved, read back, registered",
    EXIT_USAGE: "usage: a bad NAME or email, NAME registered, the entry exists, no terminal",
    EXIT_NO_TOKEN: "no token: setup-token gave none, or it failed its preflight",
    EXIT_REFUSED: "refused before the sign-in: the registry, passage, or the entry prefix setting",
    EXIT_NOT_SAVED: "not saved: the insert failed, or the entry did not read back",
    EXIT_NOT_REGISTERED: "saved and verified, not registered",
}
assert ts.EXIT_USAGE == EXIT_USAGE and ts.EXIT_STORE == EXIT_REFUSED

#: The settings tool and dotted key that name the passage folder `new` saves
#: a token in: `{"account": {"passage_prefix": "..."}}` in the `session`
#: tool's template or host layer. Point it at a store's existing layout.
PREFIX_TOOL = "session"
PREFIX_KEY = "account.passage_prefix"
#: The folder when no layer sets one. Neutral: it names the vendor, nothing
#: about whose store it is.
DEFAULT_PREFIX = "claude/oauth-tokens"
#: One part of the prefix (between slashes). `.` and `..` are refused apart.
_PREFIX_PART = re.compile(r"[A-Za-z0-9._@+-]+")
_PREFIX_WANTED = ("it must be a folder path inside the passage store, such as "
                  + DEFAULT_PREFIX)
#: Narrower than this, setup-token's screen is not worth mirroring.
MIN_COLS = 40
#: E goes into a passage entry name, so it must be path-safe as well as an
#: address. Matched with `fullmatch`.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")

#: Warn below about 25 rows at 40 columns. The
#: estimate is on an approximate layout of 2.1.299: setup-token's
#: screens are 21-22 rows at 40 columns, 9 of them its 346-character URL.
#: So a screen is about `SCREEN_ROWS + ceil(URL_CHARS / cols)` rows, and the
#: terminal needs `new`'s own row and two to spare beyond it. At 40 columns
#: that is 25; at 80, 21, so an 80x24 terminal is not warned.
URL_CHARS = 346
SCREEN_ROWS = 13
SPARE_ROWS = 3

#: The names the preflight sets in its child (`use.preflight_env`), kept
#: through the allowlist.
PREFLIGHT_SET = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")

#: `capture(*, tty_in, tty_out, claude, environ, config_root) -> Run`;
#: the production one is `setuptoken.run_setup_token`.
Capture = Callable[..., st.Run]


class NewError(Exception):
    """A step that refused or failed. The message names a step, a path or
    an entry, never a token or a fingerprint. `code` is the exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# --- the pieces ------------------------------------------------------------------------


def check_prefix(value: object, where: str = PREFIX_KEY) -> str:
    """VALUE as a passage folder path. Trailing slashes are dropped; then it
    must pass the store's own `tokenstore.check_entry` (relative, no empty,
    `.` or `..` part, printable, no leading `-`, not token-shaped), and each
    part must be made of `_PREFIX_PART`'s characters. Anything else is exit
    5, before the sign-in (`PassageCLI` runs `check_entry` only
    at the insert, after a token is made). The message names WHERE (the key
    and the file that set it); a token-shaped value is never quoted, and
    any other is quoted only when it is short text."""
    if not isinstance(value, str):
        raise NewError(f"{where} is a {type(value).__name__}: " + _PREFIX_WANTED,
                       EXIT_REFUSED)
    text = value.rstrip("/")
    try:
        ts.check_entry(text)
    except ts.StoreError as error:
        raise NewError(f"{where}: {error}; " + _PREFIX_WANTED, EXIT_REFUSED) from None
    if not all(_PREFIX_PART.fullmatch(part) for part in text.split("/")):
        shown = repr(value) if len(value) <= 80 else "a long string"
        raise NewError(f"{where} is {shown}: " + _PREFIX_WANTED, EXIT_REFUSED)
    return text


def entry_prefix(environ: Mapping[str, str] | None = None,
                 home: str | os.PathLike | None = None) -> str:
    """PREFIX_KEY through the `session` tool's template and host layers
    (as `operator` and `snapshot` read theirs), else DEFAULT_PREFIX. HOME,
    when given, is the home those layers are read under, as the registry's.
    A layer file the reader refuses, or a bad value, is exit 5: a token is
    never saved to a folder nobody chose."""
    from sessiontools import settings
    env = dict(os.environ if environ is None else environ)
    if home is not None:
        env["HOME"] = str(home)
    try:
        merged, origins, _locked, _leaves = settings.merge(
            settings.sources(PREFIX_TOOL, None, env=env))
    except (settings.SettingsError, OSError, ValueError) as error:
        raise NewError(f"settings: {error}", EXIT_REFUSED) from None
    node: object = merged
    for part in PREFIX_KEY.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    if node is None:
        return DEFAULT_PREFIX
    return check_prefix(node, f"{PREFIX_KEY} (in {origins.get(PREFIX_KEY, PREFIX_TOOL)})")


def entry_for(email: str, prefix: str = DEFAULT_PREFIX) -> str:
    """The passage entry `new` saves E's token in: `<PREFIX>/<E>`."""
    return f"{prefix}/{email}"


def check_email(email: str) -> str:
    """E, if it is an address that is also safe as the last part of a
    passage entry: `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+`, no `..`, and not
    token-shaped (a token's characters all fit the pattern). Else exit 2;
    a token-shaped value is never quoted."""
    if isinstance(email, str) and reg._TOKEN_SHAPE.search(email):
        raise NewError("email refused: it looks like a token (not shown)", EXIT_USAGE)
    if not isinstance(email, str) or not _EMAIL.fullmatch(email) or ".." in email:
        raise NewError(f"email {email!r} refused: it must be an address of letters, digits "
                       "and . _ % + - (it names the passage entry)", EXIT_USAGE)
    return email


def rows_wanted(cols: int) -> int:
    """About how tall a terminal `cols` wide must be for setup-token's
    screens to stay above the pty's last row (see `URL_CHARS`)."""
    return SCREEN_ROWS + math.ceil(URL_CHARS / max(cols, 1)) + SPARE_ROWS


def short_warning(cols: int, rows: int) -> str | None:
    """The short-terminal warning for a `cols` x `rows` terminal, or None."""
    wanted = rows_wanted(cols)
    if rows >= wanted:
        return None
    return (f"warning: this terminal is {cols}x{rows}. setup-token's screen may not fit "
            f"below about {wanted} rows; if it does not, new stops the run and you sign "
            "in again in a taller terminal")


def narrowed_env(env: Mapping[str, str]) -> dict[str, str]:
    """The preflight child's environment: setup-token's
    allowlist (`setuptoken.ENV_ALLOW`, `LC_*`), plus `PREFLIGHT_SET`."""
    keep = {key: value for key, value in env.items()
            if key in st.ENV_ALLOW or key.startswith(st.ENV_ALLOW_PREFIXES)}
    for key in PREFLIGHT_SET:
        if key in env:
            keep[key] = env[key]
    return keep


def narrowing_runner(inner: u.PreflightRunner | None = None) -> u.PreflightRunner:
    """A `use.claude_preflight` runner that narrows its child's environment
    to `narrowed_env`, then runs `inner` (`use.run_claude`, looked up when
    called, so a test's guard on subprocess holds)."""
    def run(argv, env, cwd):
        return (u.run_claude if inner is None else inner)(argv, narrowed_env(env), cwd)
    return run


def default_preflight(claude: str = "claude") -> u.Preflight:
    """`use.claude_preflight`, unchanged, through `narrowing_runner`."""
    def preflight(token: str) -> None:
        u.claude_preflight(token, runner=narrowing_runner(), claude=claude)
    return preflight


def sweep_stale(config_root: str | os.PathLike) -> list[str]:
    """Scan and remove every `setup-token-*` folder a killed run left under
    `config_root`. Its token is gone with that run, so the
    scan is for the token's SHAPE (leakscan's `TOKEN`). Returns one line per
    folder for the person, naming paths only."""
    lines: list[str] = []
    root = Path(config_root)
    try:
        found = sorted(root.glob(f"{st.CONFIG_PREFIX}*"))
    except OSError:
        return lines
    for folder in found:
        if folder.is_symlink() or not folder.is_dir():
            continue
        hits = st.scan_config_dir(folder, None)
        gone = st.remove_config_dir(folder)
        line = f"a setup-token folder left by an earlier run, {folder}, "
        line += "was removed" if gone else "could NOT be removed: remove it by hand"
        if hits:
            line += f"; it held a token's shape in: {', '.join(hits)} (not shown)"
        lines.append(line)
    return lines


# --- new -------------------------------------------------------------------------------


def new(name: str, *, email: str,
        store: ts.TokenStore | None = None,
        capture: Capture | None = None,
        preflight: u.Preflight | None = None,
        claude: str = "claude",
        environ: Mapping[str, str] | None = None,
        home: str | os.PathLike | None = None,
        registry_path: str | os.PathLike | None = None,
        tty_in: int | None = None, tty_out: int | None = None,
        out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`session account new NAME --email E` (module doc, steps 1-9).
    Returns the exit code. Every keyword is a seam; None is production's:
    `PassageCLI`, `run_setup_token`, `default_preflight`, `os.environ`, this
    process's stdin and stdout."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        if registry_path is None:
            reg.refuse_stranded(home)       # stranded: exit 5, before any sign-in
        return _new(name, email=email,
                    store=ts.PassageCLI() if store is None else store,
                    capture=st.run_setup_token if capture is None else capture,
                    preflight=default_preflight(claude) if preflight is None else preflight,
                    claude=claude, environ=os.environ if environ is None else environ,
                    home=home, registry_path=registry_path,
                    tty_in=_fileno(sys.stdin) if tty_in is None else tty_in,
                    tty_out=_fileno(sys.stdout) if tty_out is None else tty_out,
                    out=out, err=err)
    except (NewError, reg.RegistryError, ts.StoreError, cli.AccountError) as error:
        print(f"{PROG}: {error}", file=err)
        return error.code


def _new(name: str, *, email: str, store: ts.TokenStore, capture: Capture,
         preflight: u.Preflight, claude: str, environ: Mapping[str, str],
         home, registry_path, tty_in: int, tty_out: int,
         out: TextIO, err: TextIO) -> int:
    # 1. the terminal
    if not (os.isatty(tty_in) and os.isatty(tty_out)):
        raise NewError("stdin and stdout must be a terminal: setup-token signs in on "
                       "one, and new shows its screen there, masked", EXIT_USAGE)
    cols, rows = st.terminal_size(tty_out)
    if cols < MIN_COLS:
        raise NewError(f"the terminal is {cols} columns wide; new needs at least "
                       f"{MIN_COLS}", EXIT_USAGE)
    warning = short_warning(cols, rows)

    # 2. NAME, E, the registry (the entry's folder is a setting: exit 5)
    entry = entry_for(email, entry_prefix(environ, home))
    source = f"passage:{entry}"
    reg.make_account(name, email, [source])                    # exit 2
    check_email(email)                                          # exit 2
    try:                                                        # exit 5
        ts.check_entry(entry)       # the whole entry, before the sign-in
    except ts.StoreError as error:
        raise NewError(f"{error} ({PREFIX_KEY} names its folder)", EXIT_REFUSED) from None
    path = reg.default_path(home) if registry_path is None else Path(registry_path)
    if reg.load(path).find(name) is not None:                  # exit 5
        raise NewError(f"an account named {name!r} is already registered in {path}",
                       EXIT_USAGE)

    # 3. passage, and the entry absent
    store.check()                                               # exit 5
    if store.exists(entry):
        raise NewError(f"{source} already exists in {store.describe()}: new never "
                       "overwrites an entry (to register it, `session account add`)",
                       EXIT_USAGE)
    config_root = u.config_dir(home)
    for line in sweep_stale(config_root):
        print(line, file=err)

    # 4. the sign-in, captured
    print(f"new {name} ({email}): sign in AS {email} in the browser that opens\n"
          f"  token:  goes to {source}, on this host\n"
          f"  screen: setup-token runs in a masked copy of its screen; the token is "
          "never shown\n"
          f"  stop:   {st.EXIT_KEY} ends the run; nothing is saved", file=out)
    if warning:
        print(warning, file=err)
    out.flush()
    err.flush()
    try:
        run = capture(tty_in=tty_in, tty_out=tty_out, claude=claude, environ=environ,
                      config_root=config_root)
    except st.SetupTokenError as error:
        raise NewError(f"no token: {error}; nothing was saved", EXIT_NO_TOKEN) from None
    except st.Interrupted as stop:
        print(f"{PROG}: {stop}: setup-token was ended and its config folder removed "
              "(not scanned); nothing was saved", file=err)
        return 128 + stop.signum
    _report_config(run, config_root, err)
    token = run.captured.token
    if token is None:
        print(f"setup-token's last screen, masked:\n{_indent(run.captured.screen)}", file=out)
        raise NewError(f"no token: {run.captured.why}; nothing was saved", EXIT_NO_TOKEN)
    try:
        return _save(name, email=email, token=token, entry=entry, store=store,
                     preflight=preflight, path=path, out=out, err=err)
    finally:
        del token, run


def _save(name: str, *, email: str, token: str, entry: str, store: ts.TokenStore,
          preflight: u.Preflight, path: Path, out: TextIO, err: TextIO) -> int:
    source = f"passage:{entry}"
    # 5. the shape
    if not st.is_setup_token(token):
        raise NewError("no token: the value captured is not in the setup-token shape "
                       "(not shown); nothing was saved", EXIT_NO_TOKEN)
    # 6. the preflight
    try:
        preflight(token)
    except u.PreflightError as error:
        raise NewError(f"no token: the preflight failed: {ts.scrub(str(error), token)}; "
                       "nothing was saved", EXIT_NO_TOKEN) from None
    except Exception as error:  # noqa: BLE001 - an injected seam; its text is not ours
        raise NewError(f"no token: the preflight failed ({type(error).__name__}); "
                       "nothing was saved", EXIT_NO_TOKEN) from None
    print(f"token captured (not shown); preflight: {u.PREFLIGHT_REPLY}", file=out)

    # 7. the insert, the entry checked absent once more
    note = None
    try:
        if store.exists(entry):
            raise NewError(f"{source} appeared during the sign-in: not overwritten; "
                           "nothing was saved or registered", EXIT_NOT_SAVED)
        note = store.insert(entry, token)
    except ts.StoreError as error:
        said = ts.scrub(str(error), token)
        if not error.written:
            raise NewError(f"not saved: {said}; nothing was registered", EXIT_NOT_SAVED) \
                from None
        note = f"{said}; the entry was written anyway, so it was read back"

    # 8. the read-back
    try:
        same = reg.same_token(store.read(entry), token)
    except ts.StoreError as error:
        raise NewError(f"not saved: {ts.scrub(str(error), token)}; {_left(source)}",
                       EXIT_NOT_SAVED) from None
    if not same:
        raise NewError(f"not saved: {source} did not read back as the token (digests "
                       f"differ); {_left(source)}", EXIT_NOT_SAVED)
    print(f"saved: {source} (read back: digests match)", file=out)
    if note:
        print(f"  note: {note}", file=out)

    # 9. register, the fingerprint known
    try:
        account, where = cli._add(name, email=email, sources=[source], fp=True,
                                  known_fp=reg.fingerprint(token), path=path, backends=None)
    except (cli.AccountError, reg.RegistryError) as error:
        raise NewError(f"saved and verified, NOT registered: {error}\n"
                       f"  to finish: session account add {name} --email {email} "
                       f"--source {source} (under another NAME if {name!r} is taken)",
                       EXIT_NOT_REGISTERED) from None
    print(cli.added_line(account, where), file=out)
    return EXIT_OK


def _fileno(stream: object) -> int:
    """`stream`'s fd, or -1 when it has none (a captured or closed stream):
    step 1 then refuses it as no terminal, exit 2, not a traceback."""
    try:
        return stream.fileno()                                  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return -1


def _left(source: str) -> str:
    entry = source.partition(":")[2]
    return (f"nothing was registered, and {source} was left as it is: check it, or remove "
            f"it by hand (passage rm {entry}) before running new again")


def _report_config(run: st.Run, config_root: str | os.PathLike, err: TextIO) -> None:
    """What the config-dir scan found: paths, never values."""
    if run.config_hits:
        print(f"{PROG}: setup-token wrote the token, or a token's shape, into its config "
              f"folder: {', '.join(run.config_hits)} (not shown)", file=err)
    if not run.config_removed:
        print(f"{PROG}: setup-token's config folder under {config_root} could not be "
              "removed; the next `new` scans and removes it", file=err)


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())
