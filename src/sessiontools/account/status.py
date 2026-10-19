"""`session account status` and `session account list`.

`status` answers three questions, each from one source:

- **Which account is the host on?** By the host's route, as `use` picks it
  (`tokenfile.host_route`, the `session` setting `account.token_file`):
  - unset, `item_fp(Keychain.read())` turned back into a name through the
    registry, labelled `(Keychain, fp match)`;
  - set, the token file read in memory (`TokenFile.read`), fingerprinted
    and named the same way, labelled `(token file, fp match)`. `security`
    is never run on this route.

  An fp the registry does not hold is `unknown (not in registry)`, never a
  guess (a re-minted account's token is one). With `{"host_read": false}`
  in the config, neither is read: the last ledger row `use` wrote is the
  answer, labelled `(from ledger, unverified)`.
- **Which account is THIS seat on?** `CLAUDE_CODE_OAUTH_TOKEN` in this
  process's own env, fingerprinted and named. On the Keychain route it is
  labelled `env token: immune to use`; on the token-file route `env token:
  from launch; moves at restart`, since the shell reads the swapped file at
  the next launch. Absent, the seat `follows the host`. No other seat's
  environment is read.
- **How much of the plan is used?** The exporter at `usage_url` (default
  `http://localhost:9103/metrics`, a local Claude exporter;
  `claude_plan_usage_percent{account,window}`), labelled `usage of the live
  account`: the exporter gauges whichever account is live. Unreachable is
  `usage: unavailable (no exporter at URL)`, exit 0. Reset times
  (`claude_plan_window_reset_timestamp_seconds`) are shown
  by default, and `--no-resets` (the one `resets` flag, off) hides them. A
  window with no reset metric prints no reset clause in text and a null
  `resets_at` in `--json`; a time is never guessed.

  When the exporter has no answer, the host may name a second source: the
  limits file, the `session` setting `account.limits_file` (see
  `limits`). Unset, nothing changes. Set, its windows are shown with the
  file's age, `STALE` past `limits.STALE_AFTER`, labelled `usage of the host
  account`; a file that cannot be used adds its reason to the exporter's.

`--json` is `{host: {name, email, via}, seat: {name, via}, usage: {...} |
null}`. When `usage` is null, `usage_unavailable` carries the reason.
From the limits file, `usage` is `{source: "limits_file", file, label,
written_at, age_seconds, stale, windows}`, each window with `expired`.

`list` prints the registry in its order: name, email, source kinds and
their cost, whether the fp is known, and `<- host` on the account `status`
says the host is on. It RESOLVES NOTHING: no source is opened, no pass
entry decrypted, no key touched. A missing registry prints its path and the
`add` line, exit 0.

Three deliberate calls:

- **The config is its own file**, `~/.sessionkit/tools/accounts/account-config.json`,
  with exactly two keys, `host_read` (bool, default true) and `usage_url`
  (string). The registry refuses unknown keys, so the switches cannot live
  there; a missing config is the defaults, and a bad one is exit 5, never
  rewritten (as the registry).
- **A host read that fails is reported, not fatal**: `host: unreadable
  (...)`, exit 0. `status` is a report; the failure is the finding. The
  registry or config being refused is still exit 5.
- **A seat that follows the host carries the host's name** in `--json`
  (`seat.name` == `host.name`), so a reader need not join the two.

Nothing here prints, stores or raises with a token, an item or a
fingerprint: an fp is turned into a name or `unknown`, and that is all that
leaves this module.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.account import tokenfile as tf

EXIT_OK = 0
EXIT_CONFIG = reg.EXIT_REGISTRY         # 5: a refused registry or config

CONFIG_NAME = "account-config.json"
#: `use.LEDGER_NAME`, restated rather than imported, so `status` never
#: loads `use` and its seams (the preflight, the seat scan).
LEDGER_NAME = "host-ledger.jsonl"
USAGE_URL = "http://localhost:9103/metrics"
USAGE_METRIC = "claude_plan_usage_percent"
RESET_METRIC = "claude_plan_window_reset_timestamp_seconds"
USAGE_TIMEOUT = 2.0
CONFIG_KEYS = ("host_read", "usage_url")

VIA_KEYCHAIN = "Keychain, fp match"
VIA_TOKEN_FILE = "token file, fp match"
VIA_LEDGER = "from ledger, unverified"
VIA_ENV = "env token: immune to use"
VIA_ENV_FILE = "env token: from launch; moves at restart"
VIA_FOLLOWS = "follows the host"
USAGE_LABEL = "usage of the live account"
FILE_LABEL = "usage of the host account"
SOURCE_FILE = "limits_file"
HOST_MARK = "<- host"


class StatusError(Exception):
    """A config `status` refuses. Names a path, never a value read from a
    token source. `code` is the exit code."""

    def __init__(self, message: str, code: int = EXIT_CONFIG) -> None:
        super().__init__(message)
        self.code = code


# --- config ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    host_read: bool = True
    usage_url: str = USAGE_URL


def config_path(home: str | os.PathLike | None = None) -> Path:
    """`<home>/.sessionkit/tools/accounts/account-config.json`."""
    return reg.accounts_dir(home) / CONFIG_NAME


def load_config(path: str | os.PathLike | None = None) -> Config:
    """The config, or the defaults when there is none. A file that is not
    a JSON object of exactly the known keys, rightly typed, is exit 5."""
    target = config_path() if path is None else Path(path)
    if not os.path.lexists(target):
        return Config()
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise StatusError(f"config {target} could not be read "
                          f"({getattr(error, 'strerror', None) or type(error).__name__})"
                          ) from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise StatusError(f"config {target} is not valid JSON "
                          f"(line {error.lineno}, column {error.colno})") from None
    if not isinstance(data, dict):
        raise StatusError(f"config {target}: top level must be an object")
    unknown = sorted(set(data) - set(CONFIG_KEYS))
    if unknown:
        raise StatusError(f"config {target}: unknown key(s) {', '.join(map(repr, unknown))} "
                          f"(known: {', '.join(CONFIG_KEYS)})")
    host_read = data.get("host_read", True)
    if type(host_read) is not bool:
        raise StatusError(f"config {target}: host_read must be true or false")
    usage_url = data.get("usage_url", USAGE_URL)
    if (not isinstance(usage_url, str) or not usage_url
            or reg._TOKEN_SHAPE.search(usage_url)):
        raise StatusError(f"config {target}: usage_url must be a URL string")
    return Config(host_read=host_read, usage_url=usage_url)


# --- the host --------------------------------------------------------------------------


@dataclass(frozen=True)
class Who:
    """An account as `status` names it. `name` is None when it is not
    known; `via` says how it was found (or why not)."""

    name: str | None
    email: str | None
    via: str

    def to_json(self) -> dict:
        return {"name": self.name, "email": self.email, "via": self.via}


def _named(registry: reg.Registry, name: str | None, via: str) -> Who:
    account = registry.find(name) if name else None
    return Who(name, account.email if account else None, via)


def host_from_keychain(registry: reg.Registry, keychain: kc.Keychain) -> Who:
    """The host by its item's fingerprint. Never a guess: an unknown fp,
    an item with no token, or a failed read each say so."""
    try:
        fp = kc.item_fp(keychain.read())
    except kc.KeychainError as error:
        return Who(None, None, f"Keychain: unreadable ({error})")
    if fp is None:
        return Who(None, None, "Keychain: no token in the item")
    account = registry.by_fp(fp)
    if account is None:
        return Who(None, None, "Keychain: unknown (not in registry)")
    return Who(account.name, account.email, VIA_KEYCHAIN)


def host_from_token_file(registry: reg.Registry, route: tf.TokenFile) -> Who:
    """The host by its token file's fingerprint, the token read in memory
    only. Never a guess: an unknown fp or a refused file each say so, and
    the file's content is never shown."""
    try:
        token = route.read()
    except tf.TokenFileError as error:
        return Who(None, None, f"token file: unreadable ({error})")
    account = registry.by_fp(reg.fingerprint(token))
    if account is None:
        return Who(None, None, "token file: unknown (not in registry)")
    return Who(account.name, account.email, VIA_TOKEN_FILE)


def last_ledger_row(path: Path) -> dict | None:
    """The last row of `use`'s ledger that names a `to`, or None. A line
    that does not parse is skipped, not trusted."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("to"), str) and row["to"]:
            return row
    return None


def host_from_ledger(registry: reg.Registry, ledger: Path) -> Who:
    """The host as the last `use` left it. Unverified: a swap by hand (or
    by the account-swap scripts) writes no row."""
    row = last_ledger_row(ledger)
    if row is None:
        return Who(None, None, f"{VIA_LEDGER}: no ledger row at {ledger}")
    name = row["to"]
    # `use` writes only a registry name here; anything else (a hand edit) is
    # refused unshown, so a token pasted into the ledger never reaches output.
    if not reg._NAME.fullmatch(name) or reg._TOKEN_SHAPE.search(name):
        return Who(None, None, f"{VIA_LEDGER}: the last row at {ledger} names no "
                               "registry name (its value is not shown)")
    return _named(registry, name, VIA_LEDGER)


def host_of(registry: reg.Registry, config: Config, *, keychain: kc.Keychain | None,
            ledger: Path, environ: Mapping[str, str] | None = None,
            home: str | os.PathLike | None = None) -> Who:
    """The host's account. `host_read: false` wins: the ledger, and nothing
    is read. Else by the route (`tokenfile.host_route`): the token file when
    `account.token_file` is set, the Keychain item when it is not. A refused
    setting or file is reported, as a failed Keychain read is."""
    if not config.host_read:
        return host_from_ledger(registry, ledger)
    try:
        route = tf.host_route(environ, home, keychain=keychain)
    except tf.TokenFileError as error:
        return Who(None, None, f"token file: unreadable ({error})")
    if isinstance(route, tf.TokenFile):
        return host_from_token_file(registry, route)
    return host_from_keychain(registry, route)


def on_token_file(environ: Mapping[str, str] | None = None,
                  home: str | os.PathLike | None = None) -> bool:
    """Whether the host signs in from a token file: `account.token_file` is
    set. Settings only; no file or item is read. A settings layer that
    cannot be read answers False here (the host line reports it)."""
    try:
        return tf.token_file_setting(environ, home) is not None
    except tf.TokenFileError:
        return False


# --- this seat -------------------------------------------------------------------------


def seat_of(registry: reg.Registry, host: Who, env: Mapping[str, str], *,
            token_file: bool = False) -> Who:
    """This process's own route. An env token is fingerprinted in memory and
    named; the value goes nowhere. On a TOKEN_FILE host the env token came
    from that file at launch, so `use` moves it at the next restart: it is
    not immune to `use` there."""
    token = (env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if not token:
        return Who(host.name, host.email, VIA_FOLLOWS)
    via = VIA_ENV_FILE if token_file else VIA_ENV
    account = registry.by_fp(reg.fingerprint(token))
    if account is None:
        return Who(None, None, f"{via}; unknown (not in registry)")
    return Who(account.name, account.email, via)


# --- usage -----------------------------------------------------------------------------

#: `fetch(url) -> text`. Raises (anything) when the exporter is unreachable.
Fetcher = Callable[[str], str]

_SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?\s*$")
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*,?')


def fetch_url(url: str) -> str:
    """The default fetcher: one GET, a short timeout, UTF-8 text."""
    with urllib.request.urlopen(url, timeout=USAGE_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def _labels(text: str | None) -> dict[str, str] | None:
    if not text:
        return {}
    labels, pos = {}, 0
    while pos < len(text):
        match = _LABEL.match(text, pos)
        if match is None or match.end() == pos:
            return None
        value = re.sub(r"\\(.)", lambda m: {"n": "\n"}.get(m.group(1), m.group(1)),
                       match.group(2))
        labels[match.group(1)] = "(redacted)" if reg._TOKEN_SHAPE.search(value) else value
        pos = match.end()
    return labels


def parse_metrics(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Prometheus text exposition -> (name, labels, value), in order. A
    comment, or a line that does not parse, is skipped."""
    samples = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if match is None:
            continue
        labels = _labels(match.group(2))
        if labels is None:
            continue
        try:
            value = float(match.group(3))
        except ValueError:
            continue
        samples.append((match.group(1), labels, value))
    return samples


@dataclass(frozen=True)
class Window:
    window: str
    account: str | None
    #: None only for an `expired` window: the window it measured is over.
    percent: float | None
    resets_at: float | None = None
    expired: bool = False


@dataclass(frozen=True)
class Usage:
    """The plan usage: from the exporter (`source` None, `url`), or from the
    limits file (`source` SOURCE_FILE, `file`, `written_at`, `age_s`,
    `stale`). `unavailable` is the reason there is none."""

    url: str | None
    windows: tuple[Window, ...] = ()
    unavailable: str | None = None
    source: str | None = None
    file: str | None = None
    written_at: float | None = None
    age_s: int | None = None
    stale: bool = False

    def to_json(self, resets: bool = True) -> dict | None:
        if self.unavailable is not None:
            return None
        from_file = self.source == SOURCE_FILE
        rows = []
        for w in self.windows:
            row: dict = {"window": w.window, "account": w.account, "percent": w.percent}
            if from_file:
                row["expired"] = w.expired
            if resets:
                row["resets_at"] = _iso(w.resets_at)
            rows.append(row)
        if not from_file:
            return {"url": self.url, "label": USAGE_LABEL, "windows": rows}
        return {"source": SOURCE_FILE, "file": self.file, "label": FILE_LABEL,
                "written_at": _iso(self.written_at), "age_seconds": self.age_s,
                "stale": self.stale, "windows": rows}


def usage_of(url: str, fetch: Fetcher | None = None) -> Usage:
    """The plan usage the exporter reports. Unreachable, or reachable with
    no usage series, is `unavailable` with the reason; never an exit."""
    fetch = fetch_url if fetch is None else fetch
    try:
        text = fetch(url)
    except Exception:  # noqa: BLE001 - any failure to fetch is "no exporter"
        return Usage(url, unavailable=f"no exporter at {url}")
    if not isinstance(text, str):
        return Usage(url, unavailable=f"no exporter at {url}")
    # NaN and +-Inf are legal exposition values, but no percent or time: a
    # sample carrying one is skipped (it would crash the text and make
    # `--json` emit NaN, which is not JSON).
    samples = [(name, labels, value) for name, labels, value in parse_metrics(text)
               if math.isfinite(value)]
    resets = {(labels.get("account"), labels.get("window")): value
              for name, labels, value in samples if name == RESET_METRIC}
    windows = tuple(
        Window(labels.get("window") or "?", labels.get("account"), value,
               resets.get((labels.get("account"), labels.get("window"))))
        for name, labels, value in samples if name == USAGE_METRIC)
    if not windows:
        return Usage(url, unavailable=f"no {USAGE_METRIC} at {url}")
    return Usage(url, windows)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, UTC).isoformat(timespec="minutes")
    except (OverflowError, OSError, ValueError):
        return None


def _pct(value: float) -> str:
    return f"{value:.0f}%" if value == int(value) else f"{value:.1f}%"


def age_text(seconds: int) -> str:
    """`45s`, `12m`, `3h`, `2d`: the largest whole unit."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def usage_line(usage: Usage, resets: bool = True) -> str:
    if usage.unavailable is not None:
        return f"usage: unavailable ({usage.unavailable})"
    accounts = {w.account for w in usage.windows}
    parts = []
    for w in usage.windows:
        if w.percent is None:
            text = f"{w.window} reset since written"
        else:
            text = f"{w.window} {_pct(w.percent)}"
        if len(accounts) > 1 and w.account:
            text = f"{w.account} {text}"
        # An expired window's reset is already said (`reset since written`).
        if resets and not w.expired and (when := _iso(w.resets_at)) is not None:
            text += f" (resets {when})"
        parts.append(text)
    if usage.source != SOURCE_FILE:
        return f"usage: {', '.join(parts)}  ({USAGE_LABEL})"
    age = f"{age_text(usage.age_s or 0)} old"
    return (f"usage: {', '.join(parts)}  ({FILE_LABEL}, limits file, "
            f"{'STALE: ' + age if usage.stale else age})")


# --- status ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    host: Who
    seat: Who
    usage: Usage
    resets: bool = field(default=True)

    def to_json(self) -> dict:
        data = {"host": self.host.to_json(),
                "seat": {"name": self.seat.name, "via": self.seat.via},
                "usage": self.usage.to_json(self.resets)}
        if self.usage.unavailable is not None:
            data["usage_unavailable"] = self.usage.unavailable
        return data

    def text(self) -> str:
        return "\n".join((f"host:  {_who_text(self.host)}",
                          f"seat:  {_seat_text(self.seat)}",
                          usage_line(self.usage, self.resets)))


def _who_text(who: Who) -> str:
    if who.name is None:
        return who.via
    email = f" ({who.email})" if who.email else ""
    return f"{who.name}{email}  ({who.via})"


def _seat_text(seat: Who) -> str:
    if seat.via == VIA_FOLLOWS:
        return VIA_FOLLOWS
    return _who_text(seat)


def _paths(home, registry_path, config_file) -> tuple[Path, Path, Path]:
    rpath = reg.default_path(home) if registry_path is None else Path(registry_path)
    cpath = (config_path(home) if registry_path is None else rpath.parent / CONFIG_NAME
             ) if config_file is None else Path(config_file)
    return rpath, cpath, rpath.parent / LEDGER_NAME


#: `clock() -> datetime`, timezone-aware. The limits file's age is told by it.
Clock = Callable[[], datetime]


def gather(*, keychain: kc.Keychain | None = None, fetch: Fetcher | None = None,
           env: Mapping[str, str] | None = None, resets: bool = True,
           home: str | os.PathLike | None = None,
           registry_path: str | os.PathLike | None = None,
           config_file: str | os.PathLike | None = None,
           clock: Clock | None = None) -> Status:
    """The three answers. Raises RegistryError or StatusError (exit 5) on a
    refused registry or config; nothing else stops it. The limits file is
    asked only when the exporter has no answer (`limits.fallback`)."""
    from sessiontools.account import limits

    rpath, cpath, ledger = _paths(home, registry_path, config_file)
    registry = reg.load(rpath)
    config = load_config(cpath)
    environ = os.environ if env is None else env
    host = host_of(registry, config, keychain=keychain, ledger=ledger, environ=environ,
                   home=home)
    seat = seat_of(registry, host, environ, token_file=on_token_file(environ, home))
    usage = usage_of(config.usage_url, fetch)
    if usage.unavailable is not None:
        now = (datetime.now(UTC) if clock is None else clock()).timestamp()
        usage = limits.fallback(usage, now, environ, home)
    return Status(host, seat, usage, resets)


def status(*, as_json: bool = False, resets: bool = True,
           keychain: kc.Keychain | None = None, fetch: Fetcher | None = None,
           env: Mapping[str, str] | None = None,
           home: str | os.PathLike | None = None,
           registry_path: str | os.PathLike | None = None,
           config_file: str | os.PathLike | None = None,
           clock: Clock | None = None,
           out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`session account status [--json]`. Returns the exit code."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        if registry_path is None:
            reg.say_stranded(home, err)     # stranded: one line, then the new folder only
        found = gather(keychain=keychain, fetch=fetch, env=env, resets=resets, home=home,
                       registry_path=registry_path, config_file=config_file, clock=clock)
    except (reg.RegistryError, StatusError) as error:
        print(f"session account status: {error}", file=err)
        return error.code
    print(json.dumps(found.to_json(), indent=2) if as_json else found.text(), file=out)
    return EXIT_OK


# --- list ------------------------------------------------------------------------------


def sources_text(account: reg.Account) -> str:
    """Each source kind once, in registry order, with its cost:
    `file (free), pass (key touch)`. No ref is opened."""
    kinds: list[str] = []
    for source in account.sources:
        if source.backend not in kinds:
            kinds.append(source.backend)
    return ", ".join(f"{kind} ({reg.COST.get(kind, 'unknown cost')})" for kind in kinds)


def list_rows(registry: reg.Registry, host: Who) -> list[tuple[str, str, str, str, str]]:
    return [(account.name, account.email, sources_text(account),
             "fp known" if account.fp else "fp unknown",
             HOST_MARK if host.name is not None and account.name == host.name else "")
            for account in registry.accounts]


def render_list(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
                     for row in rows)


def list_accounts(*, keychain: kc.Keychain | None = None,
                  env: Mapping[str, str] | None = None,
                  home: str | os.PathLike | None = None,
                  registry_path: str | os.PathLike | None = None,
                  config_file: str | os.PathLike | None = None,
                  out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`session account list`. Resolves no source. Returns the exit code."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    rpath, cpath, ledger = _paths(home, registry_path, config_file)
    try:
        if registry_path is None:
            reg.say_stranded(home, err)     # stranded: one line, then the new folder only
        if not reg.exists(rpath):
            print(f"no accounts registered: {rpath} does not exist\n"
                  "  add one: session account add NAME --email E --source BACKEND:REF",
                  file=out)
            return EXIT_OK
        registry = reg.load(rpath)
        config = load_config(cpath)
    except (reg.RegistryError, StatusError) as error:
        print(f"session account list: {error}", file=err)
        return error.code
    if not registry.accounts:
        print(f"no accounts registered in {rpath}\n"
              "  add one: session account add NAME --email E --source BACKEND:REF", file=out)
        return EXIT_OK
    host = host_of(registry, config, keychain=keychain, ledger=ledger, environ=env,
                   home=home)
    print(render_list(list_rows(registry, host)), file=out)
    if host.name is None:
        print(f"host: {host.via}", file=out)
    return EXIT_OK
