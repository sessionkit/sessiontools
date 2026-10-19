"""The Keychain seam: the one place that touches the shared credential.

Claude Code on a Mac keeps its login in ONE generic-password item, service
`Claude Code-credentials`, whose data is JSON:

    {"claudeAiOauth": {"accessToken": "sk-ant-oat01-...", ...}}

Every seat on the Mac that has no `CLAUDE_CODE_OAUTH_TOKEN` of its own reads
it, so a write here moves every seat within about 50 s. Nothing else in
`sessiontools.account` calls `security`: it goes through a `Keychain`.

- `Keychain` is the protocol: `read() -> bytes`, `write(blob)`, `describe()`.
- `SecurityCLI` is the production implementation. `read` runs
  `security find-generic-password -s SERVICE -a ACCOUNT -w`. `write` runs
  `security -i` and feeds it `add-generic-password -U ... -X <hex>` on
  STDIN, as the account-swap scripts do: the blob holds the token, and argv is
  world-readable through `ps`, so the blob is never in argv, hex or not.
  Its runner is injectable, so a test records the argv and stdin it WOULD
  use and the real `security` binary is never run.
- `FakeKeychain` is an in-memory item; every test uses it.
- `render_item(token)` builds the item's JSON from `ITEM_TEMPLATE`, and
  `item_fp(blob)` turns an item back into the registry's fingerprint.

`ITEM_TEMPLATE` is the field set the item gets beside the access token. The
set the account-swap scripts write is still open (those scripts live
outside this repo). It lives in this one constant, pinned by the one fixture
`tests/fixtures/keychain-item.json`: settling that set is an edit to both,
side by side, and the pin test says if they drift.

A blob with no `claudeAiOauth` object is REFUSED, by `item_fp` and by every
`write`: an item Claude Code cannot read logs out every seat on the Mac.

No message, repr or exception text in this module carries a blob, a token
or `security`'s own output (which may echo the command it was fed).
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from sessiontools.account.registry import fingerprint

SERVICE = "Claude Code-credentials"
#: An absolute path, so a `security` earlier on PATH is never the one run.
SECURITY = "/usr/bin/security"
ITEM_KEY = "claudeAiOauth"
TOKEN_KEY = "accessToken"

#: The fields the item gets beside `accessToken` (still open; see the module doc).
#: A setup token carries no refresh token and no expiry.
ITEM_TEMPLATE: dict = {
    ITEM_KEY: {
        TOKEN_KEY: None,
        "refreshToken": None,
        "expiresAt": None,
        "scopes": ["user:inference"],
        "subscriptionType": None,
    }
}

#: Exit codes: a read or a refused blob is the
#: backup step's failure, 5; a write that failed is 6.
EXIT_KEYCHAIN = 5
EXIT_WRITE = 6

#: `security`'s exit status for "The specified item could not be found".
_NOT_FOUND = 44
TIMEOUT = 30
#: A Keychain account name we will quote into a `security -i` line: no
#: whitespace, no quote, no backslash, nothing the line parser could split.
_ACCOUNT = re.compile(r"[A-Za-z0-9._@+-]{1,64}")
_HEX = re.compile(rb"(?:[0-9a-fA-F]{2})+")
#: `security -i` reads each line into a buffer of MAX_LINE_LEN, 4096 bytes
#: (Apple's SecurityTool/macOS/security.c). A longer line would reach it
#: cut in two: a truncated item written, the rest run as a command. So the
#: one line `write` feeds, newline included, must fit, or nothing runs. The
#: blob rides as hex, so this caps an item near 2 KB, which a backed-up item
#: carrying more than `claudeAiOauth` (an `mcpOAuth` map) can pass.
MAX_LINE = 4095


class KeychainError(Exception):
    """A read, write or blob the seam refuses. The message names the item
    and the step, never a blob, a token or `security`'s output. `code` is
    the exit code a caller should leave with."""

    def __init__(self, message: str, code: int = EXIT_KEYCHAIN) -> None:
        super().__init__(message)
        self.code = code


@runtime_checkable
class Keychain(Protocol):
    """The shared credential, behind a seam."""

    def read(self) -> bytes:
        """The item's data. Raises KeychainError if there is none."""
        ...

    def write(self, blob: bytes) -> None:
        """Replace the item's data with `blob`, which must be an item
        (`check_item`). Raises KeychainError on refusal or failure."""
        ...

    def describe(self) -> str:
        """Which item this is, for a plan or an error. Never its content."""
        ...


# --- the item's JSON -----------------------------------------------------------------


def parse_item(blob: bytes) -> dict:
    """The item as a dict. REFUSES (exit 5) anything that is not a JSON
    object holding a `claudeAiOauth` object. The reason never quotes it."""
    if not isinstance(blob, (bytes, bytearray)):
        raise KeychainError(f"an item must be bytes, not {type(blob).__name__}")
    try:
        data = json.loads(bytes(blob).decode("utf-8"))
    except UnicodeDecodeError:
        raise KeychainError("item refused: not UTF-8 (content not shown)") from None
    except json.JSONDecodeError as error:
        raise KeychainError(f"item refused: not JSON (line {error.lineno}, column "
                            f"{error.colno}; content not shown)") from None
    if not isinstance(data, dict) or not isinstance(data.get(ITEM_KEY), dict):
        raise KeychainError(f"item refused: no {ITEM_KEY} object (content not shown)")
    return data


def check_item(blob: bytes) -> bytes:
    """`blob`, if it is an item Claude Code can read; else KeychainError."""
    parse_item(blob)
    return bytes(blob)


def item_fp(blob: bytes) -> str | None:
    """The registry fingerprint of the item's `accessToken`, or None when
    the `claudeAiOauth` object holds no token string. A blob with no
    `claudeAiOauth` is refused (KeychainError), never guessed at."""
    token = parse_item(blob)[ITEM_KEY].get(TOKEN_KEY)
    if not isinstance(token, str) or not token:
        return None
    return fingerprint(token)


def render_item(token: str) -> bytes:
    """The item's data for `token`: `ITEM_TEMPLATE` with the token in
    `accessToken`, compact JSON, UTF-8."""
    if not isinstance(token, str) or not token or token != token.strip():
        raise KeychainError("render_item: the token must be a non-empty string "
                            "with no surrounding whitespace (value not shown)")
    item = copy.deepcopy(ITEM_TEMPLATE)
    item[ITEM_KEY][TOKEN_KEY] = token
    return json.dumps(item, separators=(",", ":")).encode("utf-8")


# --- the production seam: `security` ---------------------------------------------------

#: `runner(argv, stdin) -> CompletedProcess` with bytes stdout/stderr.
Runner = Callable[[Sequence[str], bytes], subprocess.CompletedProcess]


def run_security(argv: Sequence[str], stdin: bytes) -> subprocess.CompletedProcess:
    """The default runner. `subprocess.run` is looked up at call time, so a
    test's guard on it holds here too."""
    return subprocess.run(list(argv), input=stdin, capture_output=True,
                          check=False, timeout=TIMEOUT)


def default_account() -> str:
    """The Keychain account Claude Code's item lives under: `$USER`, read
    when called (`admin` on a typical host)."""
    user = os.environ.get("USER", "")
    if not user:
        raise KeychainError("no Keychain account: $USER is unset and none was configured")
    return user


class SecurityCLI:
    """The real item, through `/usr/bin/security`. The blob goes on stdin,
    never argv."""

    def __init__(self, account: str | None = None, *, service: str = SERVICE,
                 runner: Runner | None = None, binary: str = SECURITY) -> None:
        self.account = default_account() if account is None else account
        if not isinstance(self.account, str) or not _ACCOUNT.fullmatch(self.account):
            raise KeychainError(f"Keychain account {self.account!r} refused "
                                "(letters, digits, . _ @ + -; at most 64)")
        if not service or '"' in service or "\\" in service or "\n" in service:
            raise KeychainError(f"Keychain service {service!r} refused")
        self.service = service
        self.runner = run_security if runner is None else runner
        self.binary = binary

    def describe(self) -> str:
        return f"Keychain item {self.service!r} (account {self.account})"

    def __repr__(self) -> str:
        return f"SecurityCLI(service={self.service!r}, account={self.account!r})"

    def _run(self, argv: list[str], stdin: bytes, step: str, code: int):
        try:
            return self.runner(argv, stdin)
        except subprocess.TimeoutExpired:
            raise KeychainError(f"{step} {self.describe()}: security timed out "
                                f"after {TIMEOUT}s", code) from None
        except OSError as error:
            raise KeychainError(f"{step} {self.describe()}: could not run {self.binary} "
                                f"({error.strerror or type(error).__name__})", code) from None

    def read_argv(self) -> list[str]:
        return [self.binary, "find-generic-password", "-s", self.service,
                "-a", self.account, "-w"]

    def read(self) -> bytes:
        done = self._run(self.read_argv(), b"", "read", EXIT_KEYCHAIN)
        if done.returncode == _NOT_FOUND:
            raise KeychainError(f"read {self.describe()}: no such item")
        if done.returncode != 0:
            raise KeychainError(f"read {self.describe()}: security exited "
                                f"{done.returncode} (its output is not shown)")
        out = done.stdout or b""
        if out.endswith(b"\n"):
            out = out[:-1]
        # `-w` prints data that is not printable text as hex, without a
        # marker; an item is JSON, so text that is not `{...}` and is all
        # hex digits is that encoding.
        if out[:1] != b"{" and _HEX.fullmatch(out):
            out = bytes.fromhex(out.decode("ascii"))
        if not out:
            raise KeychainError(f"read {self.describe()}: the item is empty")
        return out

    def write_argv(self) -> list[str]:
        """What `write` runs: `security -i`, and nothing else. The command,
        and the blob in it, go on stdin."""
        return [self.binary, "-i"]

    def write_script(self, blob: bytes) -> bytes:
        """The one `security -i` line `write` feeds on stdin. It holds the
        blob, hex-encoded: never log it, never put it in argv. A line
        `security -i` could not read whole (`MAX_LINE`) is refused."""
        line = (f'add-generic-password -U -s "{self.service}" -a "{self.account}" '
                f"-X {bytes(blob).hex()}\n").encode("ascii")
        if len(line) > MAX_LINE:
            raise KeychainError(f"the item is {len(blob)} bytes: its security -i line "
                                f"would be {len(line)} bytes, over the {MAX_LINE} that "
                                "security reads in one line (content not shown)", EXIT_WRITE)
        return line

    def write(self, blob: bytes) -> None:
        """Replace the item's data. The blob must be an item (`check_item`),
        or nothing runs. `security -i` can exit 0 when a command inside it
        failed, so a non-zero exit OR any stderr is a failure here, and the
        caller still verifies by reading back."""
        try:
            script = self.write_script(check_item(blob))
        except KeychainError as error:  # refused, or too long for one line
            raise KeychainError(f"write {self.describe()}: {error}", EXIT_WRITE) from None
        done = self._run(self.write_argv(), script, "write", EXIT_WRITE)
        if done.returncode != 0 or (done.stderr or b"").strip():
            raise KeychainError(f"write {self.describe()}: security -i exited "
                                f"{done.returncode} or reported an error (its output "
                                "is not shown: it may echo the command)", EXIT_WRITE)


# --- the test seam -------------------------------------------------------------------


class FakeKeychain:
    """An in-memory item. `writes` records every blob accepted, in order.

    Knobs, for `use`'s failure paths: `fail_read` / `fail_write` make the
    next and every later read or write raise; `mangle` stores
    `mangle(blob)` instead of `blob`, so a read-back verify sees something
    other than what was written. A write `check_item` refuses raises and
    changes nothing, as `SecurityCLI` does."""

    def __init__(self, blob: bytes | None = None, *, fail_read: bool = False,
                 fail_write: bool = False,
                 mangle: Callable[[bytes], bytes] | None = None) -> None:
        self.blob = None if blob is None else bytes(blob)
        self.fail_read = fail_read
        self.fail_write = fail_write
        self.mangle = mangle
        self.writes: list[bytes] = []
        self.reads = 0

    def describe(self) -> str:
        return "fake Keychain item (in memory)"

    def __repr__(self) -> str:
        state = "empty" if self.blob is None else f"{len(self.blob)} bytes"
        return f"FakeKeychain({state}, writes={len(self.writes)})"

    def read(self) -> bytes:
        self.reads += 1
        if self.fail_read:
            raise KeychainError(f"read {self.describe()}: failed (fake)")
        if self.blob is None:
            raise KeychainError(f"read {self.describe()}: no such item")
        return self.blob

    def write(self, blob: bytes) -> None:
        if self.fail_write:
            raise KeychainError(f"write {self.describe()}: failed (fake)", EXIT_WRITE)
        try:
            accepted = check_item(blob)
        except KeychainError as error:
            raise KeychainError(f"write {self.describe()}: {error}", EXIT_WRITE) from None
        self.writes.append(accepted)
        self.blob = accepted if self.mangle is None else bytes(self.mangle(accepted))
