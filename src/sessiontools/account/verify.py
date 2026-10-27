"""`session account verify [NAME] [--json]`: which account a token bills.

`status` names an account by matching a token's fingerprint against the
registry. That proves which token a seat holds, not which account the API
bills for it. `verify` asks the API: ONE `POST /v1/messages` with the token,
a one-token request to the cheapest model, and reads the account-wide
rate-limit headers off the answer. The 7d window's reset time is the
account's own, so it identifies the account.

**Which token.**

- `NAME` given: the registry account's first source that works
  (`registry.resolve`; a `pass` source costs its one touch). Unknown NAME:
  exit 2.
- No NAME: the token this seat bills, in the order Claude Code uses:
  1. `CLAUDE_CODE_OAUTH_TOKEN` in this process's env (`env token`);
  2. else the host route (`tokenfile.host_route`): the token file's one
     line (`token file PATH`), or the Keychain item's
     `claudeAiOauth.accessToken` (`Keychain item`).

  On the token-file route with no env token, one more line says what is
  checked: a seat with no env token on such a host signs in from Claude's
  stored credentials, so `verify` checks the FILE, not necessarily this
  seat.

The token's fingerprint is turned into a registry name, as `status` does:
`matches registry account NAME`, or `not in registry`. The fingerprint is
never printed.

**The call.** `POST API_URL` with `Authorization: Bearer <token>`,
`anthropic-version: 2023-06-01`, `anthropic-beta: oauth-2025-04-20`,
`content-type: application/json`, and the body `{"model": MODEL,
"max_tokens": 1, "messages": [{"role": "user", "content": "."}]}`. Timeout
TIMEOUT seconds. The reply body is read only for an error's `type` and
`message`; a success body is ignored.

**What is printed.** The source and the registry match, then one line per
window from `anthropic-ratelimit-unified-{5h,7d}-utilization` (a fraction,
shown as a percent; a value above 1 is taken as a percent already) and
`-reset` (epoch seconds, shown as ISO UTC and relative to now), then
`anthropic-ratelimit-unified-status`. A missing header is `not reported`,
never 0. No other response header is shown, and never a request header. A
header value that is not short printable text, or looks like a token, is
`(not shown)`.

`--json` is `{source, account, http_status, model, windows: {5h:
{utilization, resets_at}, 7d: {...}}, status}`; `account` is the registry
name or null, `utilization` a percent or null, `resets_at` ISO or null.

**Exit codes** (`EXIT_CODES`): 0 the API answered with at least one window
header (a 429 included: the headers are the proof, and the text says `rate
limited`); 2 usage, unknown NAME; 4 no token resolved (no env token, no
item or no token in it, no token file, no source of NAME); 5 a refused
registry, setting, token file or Keychain item; 6 the API refused the token
(401 or 403); 7 no answer (a network error, a timeout, a 5xx, or an answer
with none of the window headers).

**Never a token.** The token lives in one local and goes only into the
`Authorization` header handed to the transport. A transport failure is
described by its exception's type and the URL, never its text. An error
body's `message` is shown only when it is not token-shaped, cut to 200
characters.

Seams, keywords of `verify` and `cli.main`: `post` (the transport,
`Post`; default `urllib_post`), `keychain`, `backends`, `env`, `home` and
`clock`. Every test injects `post`; the default is never reached in tests.
"""

from __future__ import annotations

import json
import math
import os
import sys
import textwrap
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from sessiontools.account import keychain as kc
from sessiontools.account import registry as reg
from sessiontools.account import status as st
from sessiontools.account import tokenfile as tf

PROG = "session account verify"

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5"
TIMEOUT = 20
API_VERSION = "2023-06-01"
API_BETA = "oauth-2025-04-20"
ENV_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"

WINDOWS = ("5h", "7d")
HEADER_PREFIX = "anthropic-ratelimit-unified-"
STATUS_HEADER = HEADER_PREFIX + "status"
#: An error body's message is shown cut to this many characters.
MAX_MESSAGE = 200
#: A header value longer than this is not shown.
MAX_HEADER_VALUE = 64

LABEL_ENV = "env token"
LABEL_KEYCHAIN = "Keychain item"
FILE_NOTE = ("note: no env token here: a seat with none on this host signs in from "
             "Claude's stored credentials, so this checks the FILE, not necessarily "
             "this seat")
NOT_SHOWN = "(not shown)"

EXIT_OK = 0
EXIT_USAGE = reg.EXIT_USAGE             # 2
EXIT_NO_TOKEN = reg.EXIT_UNRESOLVED     # 4
EXIT_REFUSED = reg.EXIT_REGISTRY        # 5
EXIT_TOKEN_REFUSED = 6
EXIT_NO_ANSWER = 7

EXIT_CODES = {
    EXIT_OK: "the API answered with at least one usage window header (a 429 too)",
    EXIT_USAGE: "usage, unknown NAME",
    EXIT_NO_TOKEN: "no token resolved",
    EXIT_REFUSED: "a refused registry, setting, token file or Keychain item",
    EXIT_TOKEN_REFUSED: "the API refused the token (401, 403)",
    EXIT_NO_ANSWER: "no answer (network error, timeout, 5xx, or no window headers)",
}

#: The verify exit codes, for the group's epilog (wrapped: the epilog is raw).
EPILOG_LINE = textwrap.fill("`verify` exit codes: " + "; ".join(
    f"{code} {text}" for code, text in EXIT_CODES.items()) + ".", width=72)

#: `post(url, headers, body, timeout) -> (status, headers, body)`. Returns the
#: status and headers of a 4xx/5xx answer too; raises only when there is no
#: answer at all.
Post = Callable[[str, Mapping[str, str], bytes, float], tuple[int, Mapping[str, str], bytes]]


class VerifyError(Exception):
    """A refusal before or after the call. The message names a path, a
    source, a URL or an HTTP status, never a token or a fingerprint."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# --- the transport ---------------------------------------------------------------------


def urllib_post(url: str, headers: Mapping[str, str], body: bytes,
                timeout: float) -> tuple[int, Mapping[str, str], bytes]:
    """The default transport: one POST by urllib. An HTTP error status is
    an answer (its headers carry the rate limits on a 429), so it is
    returned, not raised."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        try:
            payload = error.read()
        except OSError:
            payload = b""
        found = error.headers
        return error.code, dict(found.items()) if found is not None else {}, payload


def request_body() -> bytes:
    return json.dumps({"model": MODEL, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "."}]}).encode("utf-8")


# --- which token -----------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    """A token and its source's label. The token is kept out of the repr."""

    label: str
    value: str = field(repr=False)
    note: str | None = None


def named_token(registry: reg.Registry, name: str,
                backends: Mapping[str, reg.Backend] | None) -> Token:
    """NAME's first source that works. Unknown NAME: exit 2; no source: 4."""
    account = registry.get(name)                                # unknown: exit 2
    resolved = reg.resolve(account, backends)                   # none: exit 4
    return Token(f"registry account {account.name} ({resolved.source})", resolved.token)


def keychain_token(keychain: kc.Keychain) -> str:
    try:
        blob = keychain.read()
    except kc.KeychainError as error:
        raise VerifyError(f"no token: {error}", EXIT_NO_TOKEN) from None
    try:
        item = kc.parse_item(blob)                              # refused: exit 5
    except kc.KeychainError as error:
        raise VerifyError(f"{keychain.describe()}: {error}", EXIT_REFUSED) from None
    token = item[kc.ITEM_KEY].get(kc.TOKEN_KEY)
    if not isinstance(token, str) or not token.strip():
        raise VerifyError(f"no token: {keychain.describe()} holds no "
                          f"{kc.ITEM_KEY}.{kc.TOKEN_KEY}", EXIT_NO_TOKEN)
    return token.strip()


def seat_token(environ: Mapping[str, str], home: str | os.PathLike | None,
               keychain: kc.Keychain | None) -> Token:
    """The token this seat bills: the env token, else the host route."""
    env_token = (environ.get(ENV_TOKEN) or "").strip()
    if env_token:
        return Token(LABEL_ENV, env_token)
    found = tf.token_file_setting(environ, home)                # bad setting: exit 5
    if found is not None and not os.path.lexists(os.path.realpath(found[0])):
        raise VerifyError(f"no token: token file {found[0]} does not exist "
                          f"(named by {tf.SETTING_KEY})", EXIT_NO_TOKEN)
    route = tf.host_route(environ, home, keychain=keychain)     # refused file: exit 5
    if isinstance(route, tf.TokenFile):
        return Token(route.describe(), route.read(), FILE_NOTE)
    return Token(LABEL_KEYCHAIN, keychain_token(route))


def match_text(name: str | None) -> str:
    """The registry match, as text. Never the fingerprint."""
    return f"matches registry account {name}" if name else "not in registry"


# --- the answer ------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    window: str
    #: A percent, or None when the header is missing or unreadable.
    utilization: float | None
    resets_at: float | None

    @property
    def reported(self) -> bool:
        return self.utilization is not None or self.resets_at is not None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value.strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def windows_of(headers: Mapping[str, str]) -> tuple[Window, ...]:
    rows = []
    for name in WINDOWS:
        raw = _number(headers.get(f"{HEADER_PREFIX}{name}-utilization"))
        percent = None if raw is None else round(raw if raw > 1 else raw * 100, 1)
        reset = _number(headers.get(f"{HEADER_PREFIX}{name}-reset"))
        rows.append(Window(name, percent, reset))
    return tuple(rows)


def plain(value: str | None) -> str | None:
    """VALUE if it is short printable text that does not look like a token;
    NOT_SHOWN otherwise; None when there is none."""
    if value is None:
        return None
    value = value.strip()
    if (not value or len(value) > MAX_HEADER_VALUE or not value.isprintable()
            or "sk-ant-" in value or reg._TOKEN_SHAPE.search(value)):
        return NOT_SHOWN
    return value


def error_of(body: bytes) -> tuple[str | None, str | None]:
    """(type, message) of an API error body. The message is None when it
    looks like a token; else cut to MAX_MESSAGE."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return None, None
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None, None
    kind = plain(error.get("type")) if isinstance(error.get("type"), str) else None
    message = error.get("message")
    if (not isinstance(message, str) or "sk-ant-" in message
            or reg._TOKEN_SHAPE.search(message)):
        return kind, None
    message = " ".join(message.split())[:MAX_MESSAGE]
    return kind, message or None


@dataclass(frozen=True)
class Result:
    source: str
    account: str | None
    http_status: int
    windows: tuple[Window, ...]
    status: str | None
    note: str | None = None

    def to_json(self) -> dict:
        return {"source": self.source, "account": self.account,
                "http_status": self.http_status, "model": MODEL,
                "windows": {w.window: {"utilization": w.utilization,
                                       "resets_at": st._iso(w.resets_at)}
                            for w in self.windows},
                "status": self.status}

    def text(self, now: float) -> str:
        lines = [f"verify: token from {self.source}; {match_text(self.account)}"]
        if self.note:
            lines.append(f"  {self.note}")
        for w in self.windows:
            lines.append(f"  {window_text(w, now)}")
        limited = ", rate limited" if self.http_status == 429 else ""
        lines.append(f"  status: {self.status or 'not reported'} (HTTP {self.http_status}"
                     f"{limited}, model {MODEL}, max_tokens 1)")
        return "\n".join(lines)


def window_text(w: Window, now: float) -> str:
    if not w.reported:
        return f"{w.window}  not reported"
    used = "utilization not reported" if w.utilization is None else \
        f"{st._pct(w.utilization)} used"
    text = f"{w.window}  {used}"
    when = st._iso(w.resets_at)
    if when is not None:
        delta = int(w.resets_at - now)
        rel = f"in {st.age_text(delta)}" if delta >= 0 else f"{st.age_text(-delta)} ago"
        text += f"  (resets {when}, {rel})"
        if w.window == "7d":
            text += "  <- the 7d reset identifies the account"
    return text


def judge(token: Token, account: str | None, status: int, headers: Mapping[str, str],
          body: bytes) -> Result:
    """The answer as a Result, or VerifyError (exit 6 or 7)."""
    if status in (401, 403):
        kind, message = error_of(body)
        text = f"token refused (HTTP {status}: {kind or 'no error type'})"
        raise VerifyError(text + (f": {message}" if message else ""), EXIT_TOKEN_REFUSED)
    windows = windows_of(headers)
    if not any(w.reported for w in windows):
        kind, message = error_of(body)
        if status >= 500:
            text = f"no answer (HTTP {status}: {kind or 'no error type'})"
        else:
            text = (f"no answer: the API replied HTTP {status} with no unified rate-limit "
                    f"window headers" + (f" ({kind})" if kind else ""))
        raise VerifyError(text + (f": {message}" if message else ""), EXIT_NO_ANSWER)
    return Result(token.label, account, status, windows,
                  plain(headers.get(STATUS_HEADER)), token.note)


# --- verify ----------------------------------------------------------------------------


def _lower(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in dict(headers).items()}


def call(token: Token, post: Post) -> tuple[int, dict[str, str], bytes]:
    """The one POST. A transport failure is exit 7, named by its type and
    the URL only: an exception's text might carry what it was sent."""
    headers = {"Authorization": f"Bearer {token.value}",
               "anthropic-version": API_VERSION,
               "anthropic-beta": API_BETA,
               "content-type": "application/json"}
    try:
        status, found, body = post(API_URL, headers, request_body(), TIMEOUT)
    except Exception as error:  # noqa: BLE001 - any failure to answer is "no answer"
        raise VerifyError(f"no answer: {type(error).__name__} reaching {API_URL}",
                          EXIT_NO_ANSWER) from None
    try:
        return int(status), _lower(found or {}), bytes(body or b"")
    except (TypeError, ValueError):
        raise VerifyError(f"no answer: the transport returned no status from {API_URL}",
                          EXIT_NO_ANSWER) from None


def verify(name: str | None = None, *, as_json: bool = False,
           keychain: kc.Keychain | None = None,
           backends: Mapping[str, reg.Backend] | None = None,
           env: Mapping[str, str] | None = None,
           home: str | os.PathLike | None = None,
           registry_path: str | os.PathLike | None = None,
           post: Post | None = None,
           clock: st.Clock | None = None,
           out: TextIO | None = None, err: TextIO | None = None) -> int:
    """`session account verify [NAME] [--json]`. Returns the exit code."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    post = urllib_post if post is None else post
    environ = os.environ if env is None else env
    rpath = reg.default_path(home) if registry_path is None else Path(registry_path)
    try:
        if registry_path is None:
            reg.say_stranded(home, err)
        registry = reg.load(rpath)                              # refused: exit 5
        if name is not None:
            token = named_token(registry, name, backends)
        else:
            token = seat_token(environ, home, keychain)
        found = registry.by_fp(reg.fingerprint(token.value))
        status, headers, body = call(token, post)
        result = judge(token, found.name if found else None, status, headers, body)
    except (VerifyError, reg.RegistryError, tf.TokenFileError) as error:
        print(f"{PROG}: {error}", file=err)
        return error.code
    now = (datetime.now(UTC) if clock is None else clock()).timestamp()
    print(json.dumps(result.to_json(), indent=2) if as_json else result.text(now), file=out)
    return EXIT_OK
