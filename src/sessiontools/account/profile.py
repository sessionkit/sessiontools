"""The display profile: which account `~/.claude.json` NAMES.

Claude Code keeps `oauthAccount` in `HOME/.claude.json`, and its `/status`
and `claude auth status` name the account from it (`emailAddress`). It is
a display profile, not the billing token: the token (the Keychain item, a
token file, or `CLAUDE_CODE_OAUTH_TOKEN`) decides which account a call
bills. `session account use` changes the token and never this file, so
after a switch those two keep naming the old account until Claude Code
rewrites it. `use` says so in its plan, and `status` prints the profile as
its own line beside the host and the seat, so each source is named for
what it is.

`read_profile(home)` is the one reader:

- **Read only.** The file is opened read-only and without blocking, and
  checked to be a regular file before any read (a FIFO would hang). It is
  never written, created or locked; Claude Code owns it and rewrites it at
  runtime, so a second writer would race it.
- **Size-capped** at `MAX_BYTES` (32 MB), checked before the read.
- **Only `oauthAccount.emailAddress` leaves this module**, and only when it
  is a short, printable, email-shaped string that is not token-shaped.
  Nothing else in the file (projects, history, other account fields) is
  read into any output.
- No file, no `oauthAccount`, or no `emailAddress`: `Profile(None)`, the file
  names no account. Anything else that stops the read (not a regular file,
  too big, not JSON, not an object, a value that is not an address) is
  `Profile(None, reason)`; the reason names the problem, never a value.

`HOME` is the seam (`home`, a keyword everywhere in `account`); None is the
user's home.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from sessiontools.account import registry as reg

FILE_NAME = ".claude.json"
#: How the file is named in output: the same words for every user.
SOURCE = "~/.claude.json"
#: The file holds a project history and grows; this far is not it.
MAX_BYTES = 32 * 1024 * 1024
#: The longest address shown (RFC 5321's path limit).
MAX_EMAIL = 254

_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_NOT_ADDRESS = "oauthAccount.emailAddress is not an email address (not shown)"


@dataclass(frozen=True)
class Profile:
    """The account the file names: `email`, or None. `reason` is set only
    when the file could not be read for one."""

    email: str | None
    reason: str | None = None


def profile_path(home: str | os.PathLike | None = None) -> Path:
    """`HOME/.claude.json`."""
    return (Path.home() if home is None else Path(home)) / FILE_NAME


def _address(value: object) -> str | None:
    if (isinstance(value, str) and len(value) <= MAX_EMAIL and value.isprintable()
            and _EMAIL.fullmatch(value) and "sk-ant-" not in value
            and not reg._TOKEN_SHAPE.search(value)):
        return value
    return None


def read_profile(home: str | os.PathLike | None = None) -> Profile:
    """The account HOME/.claude.json names (module doc). Never raises for
    the file, never writes it."""
    path = profile_path(home)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return Profile(None)
    except OSError as error:
        return Profile(None, error.strerror or type(error).__name__)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return Profile(None, "not a regular file")
        if info.st_size > MAX_BYTES:
            return Profile(None, f"larger than {MAX_BYTES // (1024 * 1024)} MB")
        blob = b""
        while len(blob) <= MAX_BYTES:
            chunk = os.read(fd, MAX_BYTES + 1 - len(blob))
            if not chunk:
                break
            blob += chunk
    except OSError as error:
        return Profile(None, error.strerror or type(error).__name__)
    finally:
        os.close(fd)
    if len(blob) > MAX_BYTES:
        return Profile(None, f"larger than {MAX_BYTES // (1024 * 1024)} MB")
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return Profile(None, "not JSON")
    if not isinstance(data, dict):
        return Profile(None, "not a JSON object")
    account = data.get("oauthAccount")
    if account is None:
        return Profile(None)
    if not isinstance(account, dict):
        return Profile(None, "oauthAccount is not an object")
    value = account.get("emailAddress")
    if value is None:
        return Profile(None)
    email = _address(value)
    return Profile(None, _NOT_ADDRESS) if email is None else Profile(email)


def subject(profile: Profile) -> str:
    """`~/.claude.json names EMAIL`, `... names no account`, or `... could
    not be read: REASON`."""
    if profile.email is not None:
        return f"{SOURCE} names {profile.email}"
    if profile.reason is not None:
        return f"{SOURCE} could not be read: {profile.reason}"
    return f"{SOURCE} names no account"


def note_line(profile: Profile) -> str:
    """`use`'s plan line: what the display profile names, that `use` leaves
    it, and how to prove which account the token bills."""
    return (f"  note:    {subject(profile)} (Claude's /status and 'claude auth status' "
            "read it); use does not change it. The token decides billing: check with "
            "'session account verify'")


def matches(profile: Profile, email: str | None) -> bool | None:
    """Whether the profile names EMAIL (case-insensitive), or None when
    either is unknown."""
    if profile.email is None or not email:
        return None
    return profile.email.casefold() == email.casefold()
