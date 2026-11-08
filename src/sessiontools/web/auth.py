"""The front door's login: a pairing code, sessions kept as hashes, one cookie,
and the Host and Origin checks.

PROPOSED (kit-web-shell design §3; spec "Nothing is served before the
login" and "A pairing code printed on the host opens one session").

THE PAIRING CODE. `session web pair` on the host prints a one-time code: 8
base32 characters (40 random bits), good for 10 minutes, single use. The
person types it into `/login`. Only its SHA-256 is stored, in `pair`,
with its expiry and the count of wrong tries. Every attempt that is not the
code counts as a wrong try, and the fifth burns the code. A new `pair`
replaces a pending code.

THE SESSIONS. A correct code mints a session token: 32 random bytes,
urlsafe. The browser holds the token in the `kit_session` cookie; the host
holds only its SHA-256, in `sessions.json`, with its created, seen and
expires times. A session's id, the one `session web logout` names, is the
first 12 hex characters of that hash: it says nothing about the token.

WHERE THEY LIVE. `~/.sessionkit/tools/web/` (`settings.web_dir`), 0700,
beside `settings/` and never inside it, because keys are not settings.
Each file in it is 0600 and owned by this user; one that is not is refused,
never read. Writes go through a temp file and `os.replace`, under an flock
on `.lock`, so two logins never interleave.

NO TOKEN IN CLEAR, ANYWHERE BUT THE COOKIE. No token or code is stored in
clear, none is ever read from a query string (`gate` drops the query before
it looks at anything), and no message, exception or repr here carries one.

THE COOKIE. `kit_session=<token>; Path=/; HttpOnly; SameSite=Strict;
Max-Age=2592000` (30 days), plus `Secure` when the request came by a
configured name (over https). No `Domain`, so the cookie is host-only.

THE HOST AND ORIGIN CHECKS. A request is answered only when its Host is
`127.0.0.1:<port>`, the bound address with the port, or a configured name;
anything else (a DNS-rebinding page, `localhost`) gets a 403. A request
that is not GET, HEAD or OPTIONS is refused unless its Origin is the front
door's own: the Host's scheme, and exactly the request's (allowed) Host.

PASSKEYS. With the `passkey` extra, a browser that reached the front door
by a name may keep a passkey for it (`passkey.py` does the WebAuthn
checks; this module only keeps them). A passkey is added ONLY from a
session a pairing code opened less than CODE_TTL ago, so the code is the
enrol step and a stolen cookie never becomes a key. A passkey login opens
the same kind of session, but one that cannot add a passkey. They live in
`passkeys.json`: each credential's id, RP, public key, algorithm, label,
and when it was added and last used. No secret.

TWO SCHEMES, BY HOST. The front door itself only ever speaks plain http.
A configured name is how it is reached through a TLS proxy (one serving
https on PORT), so a name's scheme is `https`, and an address's
(`127.0.0.1`, the bound one) is `http`. One front door answers both at
once: the Mac's own browser at the address, a phone at the name.

Stdlib only.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

#: The session cookie's name. Rooms never see it (the proxy strips it).
COOKIE_NAME = "kit_session"
#: A session's life, and the cookie's Max-Age: 30 days.
MAX_AGE = 30 * 24 * 3600
#: How long a pairing code is good for: 10 minutes.
CODE_TTL = 10 * 60
#: Wrong tries that burn a pairing code.
MAX_TRIES = 5
#: A pairing code's random bytes: 5 bytes are exactly 8 base32 characters.
CODE_BYTES = 5
#: A session token's random bytes.
TOKEN_BYTES = 32
#: A session's `seen` is rewritten at most this often, in seconds.
SEEN_EVERY = 60
#: The files in the web dir.
PAIR_FILE = "pair"
SESSIONS_FILE = "sessions.json"
PASSKEYS_FILE = "passkeys.json"
LOCK_FILE = ".lock"
#: How a session was opened: by a pairing code, or by a passkey.
VIA_CODE, VIA_PASSKEY = "code", "passkey"
#: How long a passkey ceremony's challenge is good for, and how many of
#: each kind may wait at once (the oldest of that kind goes first): the
#: login's are asked for by anyone who reaches the door, so they are
#: bounded, and in a pool of their own, so a flood of them never evicts an
#: enrol in flight.
CHALLENGE_TTL = 120
CHALLENGES_MAX = 64
#: Characters in a session id (a prefix of the token's SHA-256).
ID_LEN = 12

#: The paths answered without a session: the login page and its assets.
OPEN_PATHS = ("/login",)
OPEN_PREFIXES = ("/login/",)
#: Methods that need no Origin check (they change nothing).
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

_CODE = re.compile(r"[A-Z2-7]{8}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
_NAME = re.compile(r"(?=.{1,253}\Z)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


class AuthError(Exception):
    """The web dir or one of its files is refused (owner, mode, type or
    content). The message names the path, never a secret."""


class LoginRefused(Exception):
    """A pairing code that opens nothing: none pending, expired, wrong, or
    burned. The message says which; it never carries the code."""


def _sha256(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def normalize_code(text: str) -> str:
    """TEXT as a person may have typed the code: case, spaces and hyphens
    do not matter."""
    return re.sub(r"[\s-]", "", text).upper()


@dataclass(frozen=True)
class Pairing:
    """A code just issued, and when it stops working (epoch seconds)."""

    code: str = field(repr=False)
    expires: int

    def __repr__(self) -> str:
        return f"Pairing(code=<hidden>, expires={self.expires})"


@dataclass(frozen=True)
class SessionInfo:
    """One live session, as `session web logout` lists it: no secret."""

    id: str
    created: int
    seen: int
    expires: int


class Store:
    """The web dir: the pending pairing code and the live sessions.

    CLOCK returns epoch seconds; tests pass a fixed one."""

    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self.directory = Path(directory)
        self.clock = clock

    def __repr__(self) -> str:
        return f"Store({str(self.directory)!r})"

    def _now(self) -> int:
        return int(self.clock())

    # --- the directory and its files ---------------------------------------

    def ensure(self) -> None:
        """Make the web dir (0700) if it is missing; refuse one that is not a
        directory or not this user's, and tighten one that is too open."""
        d = self.directory
        d.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            d.mkdir(mode=0o700)
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode):
            raise AuthError(f"{d}: not a directory; refusing it")
        if st.st_uid != os.getuid():
            raise AuthError(f"{d}: owned by uid {st.st_uid}, not this user; refusing it")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(d, 0o700)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.ensure()
        fd = os.open(self.directory / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self, name: str) -> dict[str, Any] | None:
        """NAME's JSON object, or None when it is absent. A file that is not
        a private regular file of this user's is refused."""
        path = self.directory / name
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode):
            raise AuthError(f"{path}: not a regular file; refusing it")
        if st.st_uid != os.getuid():
            raise AuthError(f"{path}: owned by uid {st.st_uid}, not this user; refusing it")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise AuthError(f"{path}: mode {stat.S_IMODE(st.st_mode):04o}, readable by others; "
                            "refusing it (it must be 0600)")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise AuthError(f"{path}: unreadable ({type(e).__name__}); refusing it") from None
        if not isinstance(data, dict):
            raise AuthError(f"{path}: not a JSON object; refusing it")
        return data

    def _write(self, name: str, data: dict[str, Any]) -> None:
        """Replace NAME with DATA: a 0600 temp file beside it, fsynced, then
        `os.replace`."""
        path = self.directory / name
        tmp = self.directory / f".{name}.{os.getpid()}.tmp"
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def read_json(self, name: str) -> dict[str, Any] | None:
        """Another file of the web dir (e.g. `rooms.json`), read under the
        same rules: a private regular file of this user's, a JSON object."""
        self.ensure()
        return self._read(name)

    def write_json(self, name: str, data: dict[str, Any]) -> None:
        """Replace another file of the web dir, 0600, under the lock."""
        if (name in (PAIR_FILE, SESSIONS_FILE, PASSKEYS_FILE, LOCK_FILE) or "/" in name
                or name.startswith(".")):
            raise ValueError(f"{name!r}: not a file write_json may replace")
        with self._locked():
            self._write(name, data)

    def _remove(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.directory / name)

    def _sessions(self) -> dict[str, dict[str, int]]:
        data = self._read(SESSIONS_FILE) or {}
        rows = data.get("sessions", {})
        bad = f"{self.directory / SESSIONS_FILE}: not a sessions file; refusing it"
        if not isinstance(rows, dict):
            raise AuthError(bad)
        for key, row in rows.items():
            if not (isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)
                    and isinstance(row, dict)
                    and all(type(row.get(f)) is int for f in ("created", "seen", "expires"))
                    and row.get("via", VIA_CODE) in (VIA_CODE, VIA_PASSKEY)):
                raise AuthError(bad)
        return rows

    def _passkeys(self) -> list[dict[str, Any]]:
        data = self._read(PASSKEYS_FILE) or {}
        rows = data.get("passkeys", [])
        bad = f"{self.directory / PASSKEYS_FILE}: not a passkeys file; refusing it"
        if not isinstance(rows, list):
            raise AuthError(bad)
        for row in rows:
            if not (isinstance(row, dict)
                    and all(isinstance(row.get(f), str) and row[f]
                            for f in ("id", "rp_id", "spki"))
                    and isinstance(row.get("name", ""), str)
                    and all(type(row.get(f)) is int for f in ("alg", "added", "used"))):
                raise AuthError(bad)
        return rows

    def _pending(self) -> dict[str, Any] | None:
        """The pending code's row (`hash`, `expires`, `tries`), or None when
        no code is pending. A pair file of any other shape is refused."""
        data = self._read(PAIR_FILE)
        if data is None:
            return None
        if not (isinstance(data.get("hash"), str) and re.fullmatch(r"[0-9a-f]{64}", data["hash"])
                and type(data.get("expires")) is int and type(data.get("tries")) is int):
            raise AuthError(f"{self.directory / PAIR_FILE}: not a pair file; refusing it")
        return data

    def _put_sessions(self, rows: dict[str, dict[str, int]]) -> None:
        now = self._now()
        live = {h: r for h, r in rows.items() if int(r.get("expires", 0)) > now}
        self._write(SESSIONS_FILE, {"sessions": live})

    # --- the pairing code ---------------------------------------------------

    def pair(self) -> Pairing:
        """A new one-time code, good for CODE_TTL seconds. Only its hash is
        stored; any pending code is replaced."""
        code = base64.b32encode(secrets.token_bytes(CODE_BYTES)).decode("ascii")
        expires = self._now() + CODE_TTL
        with self._locked():
            self._write(PAIR_FILE, {"hash": _sha256(code), "expires": expires, "tries": 0})
        return Pairing(code=code, expires=expires)

    def login(self, code: str) -> str:
        """The session token CODE opens, or LoginRefused. A correct code is
        spent at once; a wrong one counts, and the MAX_TRIES-th burns it."""
        typed = normalize_code(code) if isinstance(code, str) else ""
        with self._locked():
            pending = self._pending()
            if pending is None:
                raise LoginRefused("no pairing code is pending: run `session web pair` "
                                   "on the host")
            now = self._now()
            if now >= pending["expires"]:
                self._remove(PAIR_FILE)
                raise LoginRefused("the pairing code has expired: run `session web pair` again")
            want = pending["hash"]
            if not (_CODE.fullmatch(typed) and hmac.compare_digest(_sha256(typed), want)):
                tries = pending["tries"] + 1
                if tries >= MAX_TRIES:
                    self._remove(PAIR_FILE)
                    raise LoginRefused(f"wrong code, {tries} tries: the code is burned; "
                                       "run `session web pair` again")
                self._write(PAIR_FILE, {**pending, "tries": tries})
                raise LoginRefused(f"wrong code ({tries} of {MAX_TRIES} tries)")
            self._remove(PAIR_FILE)
            return self._mint(now, VIA_CODE)

    def _mint(self, now: int, via: str) -> str:
        """A new session opened VIA a code or a passkey; under the lock."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        rows = self._sessions()
        rows[_sha256(token)] = {"created": now, "seen": now, "expires": now + MAX_AGE,
                                "via": via}
        self._put_sessions(rows)
        return token

    # --- sessions -----------------------------------------------------------

    def check(self, token: str) -> bool:
        """True when TOKEN is a live session. Its `seen` is brought up to
        date, at most every SEEN_EVERY seconds."""
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return False
        key = _sha256(token)
        now = self._now()
        row = self._sessions().get(key)
        if row is None or now >= int(row.get("expires", 0)):
            return False
        if now - int(row.get("seen", 0)) >= SEEN_EVERY:
            with self._locked():
                rows = self._sessions()
                if key in rows:
                    rows[key]["seen"] = now
                    self._put_sessions(rows)
        return True

    def may_enrol(self, token: str | None) -> bool:
        """True when TOKEN's session may add a passkey: a pairing code
        opened it, less than CODE_TTL seconds ago. A passkey's session, an
        older one, and one from before sessions said how they were opened
        may not."""
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return False
        row = self._sessions().get(_sha256(token))
        now = self._now()
        return (row is not None and now < int(row["expires"])
                and row.get("via") == VIA_CODE and now - int(row["created"]) < CODE_TTL)

    # --- passkeys -----------------------------------------------------------

    def passkeys(self, rp_id: str | None = None) -> list[dict[str, Any]]:
        """The kept passkeys, oldest first; only RP_ID's when it is given."""
        rows = self._passkeys()
        return [dict(r) for r in rows if rp_id is None or r["rp_id"] == rp_id]

    def add_passkey(self, cred: dict[str, Any]) -> str:
        """Keep CRED (what `passkey.register` returns); its short id. One
        already kept under the same id and RP is replaced."""
        with self._locked():
            rows = [r for r in self._passkeys()
                    if not (r["id"] == cred["id"] and r["rp_id"] == cred["rp_id"])]
            rows.append(dict(cred))
            self._write(PASSKEYS_FILE, {"passkeys": rows})
        return passkey_id(cred)

    def passkey_login(self, cred_id: str, rp_id: str) -> str:
        """A new session for the passkey CRED_ID of RP_ID, which the caller
        has checked; its `used` is brought up to date. AuthError when it is
        no longer kept (removed in between)."""
        with self._locked():
            rows = self._passkeys()
            hit = next((r for r in rows if r["id"] == cred_id and r["rp_id"] == rp_id), None)
            if hit is None:
                raise AuthError("that passkey is no longer kept")
            now = self._now()
            hit["used"] = now
            self._write(PASSKEYS_FILE, {"passkeys": rows})
            return self._mint(now, VIA_PASSKEY)

    def remove_passkeys(self, ids: list[str]) -> list[str]:
        """Remove the passkeys whose short id starts with one of IDS; the
        ids removed. A prefix that matches none, or more than one, is
        refused (AuthError) and nothing is removed. Sessions stay open:
        `logout` ends those."""
        with self._locked():
            rows = self._passkeys()
            gone = []
            for prefix in ids:
                p = prefix.lower()
                hits = [r for r in rows if p and passkey_id(r).startswith(p)]
                if len(hits) != 1:
                    what = "no passkey" if not hits else f"{len(hits)} passkeys"
                    raise AuthError(f"passkey id {prefix!r} matches {what}; nothing removed")
                gone.append(passkey_id(hits[0]))
            keep = [r for r in rows if passkey_id(r) not in gone]
            self._write(PASSKEYS_FILE, {"passkeys": keep})
        return sorted(set(gone))

    def revoke(self, token: str) -> bool:
        """End TOKEN's session (the browser's own logout). True if it was live."""
        if not isinstance(token, str):
            return False
        key = _sha256(token)
        with self._locked():
            rows = self._sessions()
            found = rows.pop(key, None) is not None
            if found:
                self._put_sessions(rows)
        return found

    def sessions(self) -> list[SessionInfo]:
        """The live sessions, oldest first. No secret: an id is a hash prefix."""
        now = self._now()
        rows = [(h, r) for h, r in self._sessions().items() if int(r.get("expires", 0)) > now]
        return sorted((SessionInfo(id=h[:ID_LEN], created=int(r.get("created", 0)),
                                   seen=int(r.get("seen", 0)), expires=int(r["expires"]))
                       for h, r in rows), key=lambda s: (s.created, s.id))

    def logout(self, ids: list[str] | None = None) -> list[str]:
        """End the sessions whose id starts with one of IDS, or every session
        when IDS is None. Returns the ids ended. An id prefix that matches
        no session, or more than one, is refused (AuthError) and nothing is
        ended."""
        with self._locked():
            rows = self._sessions()
            if ids is None:
                ended = sorted(h[:ID_LEN] for h in rows)
                rows = {}
            else:
                keys = []
                for prefix in ids:
                    p = prefix.lower()
                    hits = [h for h in rows if p and h.startswith(p)]
                    if len(hits) != 1:
                        what = "no session" if not hits else f"{len(hits)} sessions"
                        raise AuthError(f"session id {prefix!r} matches {what}; nothing ended")
                    keys.append(hits[0])
                ended = sorted({k[:ID_LEN] for k in keys})
                for k in keys:
                    rows.pop(k, None)
            self._put_sessions(rows)
        return ended


def session_key(token: str) -> str:
    """The key a session is kept under (its token's SHA-256): what an enrol
    challenge is bound to, never the token."""
    return _sha256(token)


def passkey_id(cred: Mapping[str, Any]) -> str:
    """A passkey's short id, the one `session web passkeys` names: a prefix
    of the SHA-256 of its RP and credential id."""
    return _sha256(f"{cred['rp_id']}\n{cred['id']}")[:ID_LEN]


class Challenges:
    """The challenges of passkey ceremonies in flight, in the front door's
    memory: each good once, for CHALLENGE_TTL seconds, for one KIND
    (`login` or `enrol`), an enrol's for the one session that asked. At most
    CHALLENGES_MAX of each kind wait; the oldest of that kind goes first."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._live: dict[str, tuple[float, str, str]] = {}

    def issue(self, kind: str, bound: str = "") -> bytes:
        """32 new random bytes for a KIND ceremony, bound to BOUND."""
        chal = secrets.token_bytes(32)
        key = base64.urlsafe_b64encode(chal).rstrip(b"=").decode()
        now = self.clock()
        with self._lock:
            self._live = {k: v for k, v in self._live.items() if v[0] > now}
            same = [k for k, v in self._live.items() if v[1] == kind]
            for k in same[:max(0, len(same) - CHALLENGES_MAX + 1)]:
                del self._live[k]
            self._live[key] = (now + CHALLENGE_TTL, kind, bound)
        return chal

    def take(self, key: Any, kind: str, bound: str = "") -> bytes | None:
        """The challenge KEY names (unpadded base64url), spent now, when it
        is live, for KIND and BOUND; else None (and nothing is spent)."""
        if not isinstance(key, str):
            return None
        with self._lock:
            row = self._live.get(key)
            if row is None or row[1] != kind or not hmac.compare_digest(row[2], bound):
                return None
            del self._live[key]
        if row[0] <= self.clock():
            return None
        return base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))


# --- the cookie ---------------------------------------------------------------

def session_cookie(token: str, *, secure: bool) -> str:
    """The `Set-Cookie` value that hands TOKEN to the browser."""
    return (f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={MAX_AGE}"
            + ("; Secure" if secure else ""))


def clear_cookie(*, secure: bool) -> str:
    """The `Set-Cookie` value that takes the session cookie back."""
    return (f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            + ("; Secure" if secure else ""))


def _header_values(headers: Mapping[str, str] | Any, name: str) -> list[str]:
    """Every value of header NAME, case-insensitively: from an
    `email.message.Message` (what `http.server` hands a handler) or a plain
    mapping."""
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        return [str(v) for v in (get_all(name) or [])]
    lname = name.lower()
    return [str(v) for k, v in headers.items() if k.lower() == lname]


def cookie_tokens(headers: Mapping[str, str] | Any) -> list[str]:
    """Every `kit_session` value in the request's Cookie headers. Another
    `127.0.0.1` app may have set a cookie of the same name, so all are
    returned and any live one counts."""
    out = []
    for header in _header_values(headers, "Cookie"):
        for part in header.split(";"):
            k, sep, v = part.strip().partition("=")
            if sep and k == COOKIE_NAME and v:
                out.append(v.strip('"'))
    return out


# --- Host and Origin ----------------------------------------------------------

def _split_hostport(value: str) -> tuple[str, int | None] | None:
    """(host, port or None) from a Host header or a URL's netloc, the host
    lowercased and unbracketed; None when it is malformed."""
    v = value.strip().lower()
    if not v or any(c in v for c in "/@?#\\ \t"):
        return None
    if v.startswith("["):
        host, sep, rest = v[1:].partition("]")
        if not sep:
            return None
        if rest and not rest.startswith(":"):
            return None
        port_s = rest[1:] if rest else None
    elif v.count(":") > 1:
        return None
    else:
        host, sep, port_s = v.partition(":")
        port_s = port_s if sep else None
    if not host:
        return None
    if port_s is None:
        return host, None
    if not port_s.isdigit() or not 0 < int(port_s) < 65536:
        return None
    return host, int(port_s)


def _same_addr(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return False


@dataclass(frozen=True)
class Front:
    """Who the front door is, for the Host and Origin checks: the port it
    listens on, the address it bound (already through `check_bind`), and
    the names it answers to. A name (e.g. a host name behind a TLS
    proxy) is reached over https; the address over plain http."""

    port: int
    bound: str = "127.0.0.1"
    names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.port, int) or not 0 < self.port < 65536:
            raise ValueError(f"port must be 1-65535, not {self.port!r}")
        clean = []
        for n in self.names:
            name = n.strip().lower().rstrip(".")
            if name == "localhost" or name.endswith(".localhost"):
                raise ValueError(f"{n!r}: localhost is never a front-door name")
            try:
                ipaddress.ip_address(name)
            except ValueError:
                pass
            else:
                raise ValueError(f"{n!r}: an address, not a name (the bound address is "
                                 "answered already)")
            if not _NAME.fullmatch(name):
                raise ValueError(f"{n!r}: not a host name")
            clean.append(name)
        object.__setattr__(self, "names", tuple(clean))

    def name_of(self, host_header: str | None) -> str | None:
        """The configured name HOST_HEADER names (any port), or None: an
        address, a name not configured, or a malformed Host."""
        parts = _split_hostport(host_header) if host_header else None
        if parts is None:
            return None
        host = parts[0].rstrip(".")
        return host if host in self.names else None

    def scheme_for(self, host_header: str | None) -> str:
        """`https` for a request that came by a name, else `http`."""
        return "https" if self.name_of(host_header) else "http"

    def secure_for(self, host_header: str | None) -> bool:
        """Whether a cookie set on this request gets `Secure`: by a name."""
        return self.name_of(host_header) is not None


def host_allowed(host_header: str | None, front: Front) -> bool:
    """True when HOST_HEADER names the front door: `127.0.0.1:<port>`, the
    bound address with the port, or one of its names (any port: behind
    a TLS proxy the name's port is the proxy's). `localhost`, any
    other name or address, a wrong port and a missing Host are refused."""
    if not host_header:
        return False
    parts = _split_hostport(host_header)
    if parts is None:
        return False
    host, port = parts
    if host.rstrip(".") in front.names:
        return True
    if port is None:
        port = 80  # an address is plain http
    if port != front.port:
        return False
    return _same_addr(host, "127.0.0.1") or _same_addr(host, front.bound)


def origin_allowed(origin: str | None, host_header: str | None, front: Front) -> bool:
    """True when ORIGIN is the front door's own: the Host's scheme (`https`
    for a name, `http` for an address) and exactly the request's Host,
    which must itself be allowed. A missing or `null` Origin is refused."""
    if not origin or not host_header or origin.strip() == "null":
        return False
    try:
        u = urlsplit(origin.strip())
    except ValueError:
        return False
    if u.scheme != front.scheme_for(host_header) or u.path not in ("", "/") or u.query or u.fragment:
        return False
    if u.username is not None or u.password is not None:
        return False
    if not host_allowed(host_header, front):
        return False
    return _split_hostport(u.netloc) == _split_hostport(host_header)


# --- the gate -----------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """What the gate makes of one request. STATUS is 200 (go on), 401 (no
    live session: the server sends a page to `/login`, anything else a
    401) or 403 (wrong Host or Origin). TOKEN is the live session's token,
    for the browser's own logout; it is never shown."""

    status: int
    reason: str
    token: str | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.status == 200


def is_open(path: str) -> bool:
    """True for the paths answered without a session: the login and its
    assets. PATH is the request target; its query is ignored.

    A path is open only as it is written. One that a router, a static file
    server or a room could read as another path is not open: a `.` or `..`
    segment (`/login/../settings/`), an empty segment (`//`), a backslash,
    and any of these percent-encoded (`/login/%2e%2e/talk/`, `%2f`). So the
    open set is exactly `/login` and what lies under it, however the path is
    later normalised."""
    p = path.split("?", 1)[0].split("#", 1)[0]
    if not (p in OPEN_PATHS or any(p.startswith(x) for x in OPEN_PREFIXES)):
        return False
    decoded = unquote(p)
    if "\\" in decoded or "%" in decoded:  # a % left after one decode: encoded twice
        return False
    segments = decoded.split("/")[1:]
    if decoded.endswith("/"):
        segments = segments[:-1]
    return all(seg not in ("", ".", "..") for seg in segments)


def gate(store: Store, front: Front, method: str, path: str,
         headers: Mapping[str, str] | Any) -> Verdict:
    """The checks every request passes before anything is served: Host,
    then Origin (for a method that changes something), then the session.
    The session comes from the Cookie header ONLY: the query string is
    dropped unread, so a token in a URL opens nothing."""
    hosts = _header_values(headers, "Host")
    host = hosts[0] if len(hosts) == 1 else None
    if not host_allowed(host, front):
        return Verdict(403, "host not allowed")
    if method.upper() not in SAFE_METHODS:
        origins = _header_values(headers, "Origin")
        origin = origins[0] if len(origins) == 1 else None
        if not origin_allowed(origin, host, front):
            return Verdict(403, "origin not allowed")
    if is_open(path):
        return Verdict(200, "open path")
    for token in cookie_tokens(headers):
        if store.check(token):
            return Verdict(200, "session", token)
    return Verdict(401, "no live session")
