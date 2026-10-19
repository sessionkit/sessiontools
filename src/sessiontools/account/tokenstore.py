"""The TokenStore seam: where `session account new` saves a token it minted
(the seam the account group set aside for it).

It follows `keychain.py`'s shape: a protocol, a CLI with an injectable
runner, and a fake.

- `TokenStore` is the protocol: `describe()`, `check()`, `exists(entry)`,
  `insert(entry, token)` and `read(entry)`.
- `PassageCLI` is the production store, passage (age), and only passage:
  there is no fallback to `pass` or to a file (/RULES/320).
  - `insert` runs `passage insert -m ENTRY` with `token + "\\n"` on STDIN.
    The token is never in argv (`ps` shows argv to every user) and `-f` is
    never passed. passage overwrites without asking when stdin is a pipe,
    so `insert` checks again, just before it runs, that the entry is absent.
  - passage runs in its OWN session, so `SECRET_TIMEOUT_S` ends its whole
    process group, the git under it included: a commit hook or a signing
    prompt cannot hang `new`.
  - passage's `insert` exits 0 even when its git commit fails (the
    script sets no `errexit`, and its dispatcher ends in `exit 0`). So
    an exit 0 that left text on stderr returns that text's first line,
    scrubbed, as a NOTE for the caller to print beside the entry: it is how
    the person learns the entry is saved but maybe not committed.
  - A failed insert (a non-zero exit, a timeout, no passage) raises
    `StoreError` naming the entry and passage's first stderr line, scrubbed.
    Its `written` says whether `<entry>.age` exists after the attempt: age
    writes the entry before the commit runs, so a hung commit ended by the
    timeout leaves a good entry, and `new` reads it back.
  - `read` is `secrets.passage_backend(entry)`, the resolver `use` and
    `add` go through, so the read-back proves what a later `use` will read.
- `FakeTokenStore` keeps everything in memory, for `new`'s tests. It can
  fail the check, fail the insert (with or without leaving the entry),
  corrupt the stored value (a forced read-back mismatch), fail the read,
  and return a note.

No message, repr or exception text in this module carries a token: they
name a store, an entry or a step, and passage's own stderr is scrubbed of
token-shaped text before it is quoted (`scrub`).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, runtime_checkable

from sessiontools.account import registry as reg
from sessiontools.start.secrets import (
    SECRET_TIMEOUT_S,
    SecretError,
    passage_backend,
    passage_store,
)

PASSAGE = "passage"

#: `new`'s exit codes for the store's failures: a bad
#: entry name is usage, 2; a store that cannot be used is refused before the
#: sign-in, 5; an insert or a read-back that failed is "not saved", 8.
EXIT_USAGE = 2
EXIT_STORE = 5
EXIT_NOT_SAVED = 8

#: After the timeout, SIGTERM to passage's process group, then this long
#: before SIGKILL.
KILL_GRACE_S = 2.0
#: The most of passage's stderr line a message quotes.
MAX_DETAIL = 200

#: Token-shaped text, as `new`'s mask reads it: the prefix and
#: every token character after it, however short.
_TOKEN_RUN = re.compile(r"sk-ant-[A-Za-z0-9_-]*")
_HIDDEN = "[token-shaped text removed]"
#: A run of token characters long enough to hold a piece of a token.
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]{8,}")
#: The "no token character" rule: 8 characters of the
#: token in a row are a piece of it, with or without its prefix.
PIECE = 8


class StoreError(Exception):
    """A store refusal or failure. The message names the store, the entry
    and the step, never a token. `code` is the exit code `new` leaves with;
    `entry` is the entry concerned, if any; `written` is True when an insert
    failed but `<entry>.age` exists after it (a read-back can still prove
    it)."""

    def __init__(self, message: str, code: int = EXIT_NOT_SAVED, *,
                 entry: str | None = None, written: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.entry = entry
        self.written = written


@runtime_checkable
class TokenStore(Protocol):
    """An archive of named tokens that never overwrites."""

    def describe(self) -> str:
        """Which store this is, for a plan or an error. Never a value."""
        ...

    def check(self) -> None:
        """Usable: installed, a store and its identities. Else StoreError
        (exit 5)."""
        ...

    def exists(self, entry: str) -> bool:
        """An entry or a folder by that name exists. Decrypts nothing."""
        ...

    def insert(self, entry: str, token: str) -> str | None:
        """Save `token` as a NEW entry; refuses one that exists. Returns a
        note for the person (already scrubbed), or None."""
        ...

    def read(self, entry: str) -> str:
        """The entry's value, through the resolver a source would use."""
        ...


# --- shared checks -------------------------------------------------------------------


def scrub(text: str, token: str | None = None) -> str:
    """`text` fit to quote: token-shaped runs replaced, and, if `token` is
    given, every run of token characters that holds a piece of it (`PIECE`
    of its characters in a row); control characters made `?`; cut to
    `MAX_DETAIL`.

    A piece is caught without its `sk-ant-` prefix: a body quoted on its
    own, the tail of a token wrapped or cut, or the part after a control
    byte that split it. None of those is token-SHAPED, so the pattern
    alone would pass them (a check on the token's shape cannot see a
    body cut from its prefix)."""
    if token:
        text = text.replace(token, _HIDDEN)
        pieces = _pieces(token)
        if pieces:
            text = _TOKEN_CHARS.sub(
                lambda run: _HIDDEN if _holds_piece(run.group(0), pieces)
                else run.group(0), text)
    text = _TOKEN_RUN.sub(_HIDDEN, text)
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    if len(text) > MAX_DETAIL:
        text = text[:MAX_DETAIL] + "..."
    return text


def _pieces(token: str) -> set[str]:
    """Every `PIECE` characters in a row of `token`, past its fixed
    prefix: `sk-ant-oat01-` is the same in every token, so a piece of it
    alone is ordinary text, not this token."""
    match = re.match(r"sk-ant-[A-Za-z0-9]+-", token)
    start = match.end() - PIECE + 1 if match else 0
    return {token[i:i + PIECE] for i in range(max(start, 0), len(token) - PIECE + 1)}


def _holds_piece(run: str, pieces: set[str]) -> bool:
    return any(run[i:i + PIECE] in pieces for i in range(len(run) - PIECE + 1))


def first_line(data: bytes | str | None, token: str | None = None) -> str:
    """The first non-blank line of a child's output, scrubbed; "" if none."""
    if not data:
        return ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    for line in text.splitlines():
        if line.strip():
            return scrub(line.strip(), token)
    return ""


def check_entry(entry: object) -> str:
    """`entry`, if it is a name passage can keep inside its store: relative,
    `/`-separated, no empty, `.` or `..` part, printable, not starting with
    `-` (passage would read it as an option), and not token-shaped. Else
    StoreError (exit 2); a token-shaped entry is never quoted."""
    if not isinstance(entry, str) or not entry:
        raise StoreError("passage entry refused: empty", EXIT_USAGE)
    if _TOKEN_RUN.search(entry) or reg._TOKEN_SHAPE.search(entry):
        raise StoreError("passage entry refused: it looks like a token (not shown)",
                         EXIT_USAGE)
    if not entry.isprintable():
        raise StoreError(f"passage entry {scrub(entry)!r} refused: not printable",
                         EXIT_USAGE)
    if entry.startswith("-"):
        raise StoreError(f"passage entry {entry!r} refused: it starts with '-'",
                         EXIT_USAGE)
    parts = entry.split("/")
    if entry.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise StoreError(f"passage entry {entry!r} refused: not a relative name "
                         "inside the store (no empty, '.' or '..' part)", EXIT_USAGE)
    return entry


def _check_token(token: object) -> str:
    """A value `insert` can store and `read` give back whole: one line, no
    surrounding whitespace. The value is never quoted."""
    if (not isinstance(token, str) or not token or token != token.strip()
            or "\n" in token or "\r" in token):
        raise StoreError("insert refused: the token must be one non-empty line with "
                         "no surrounding whitespace (value not shown)", EXIT_NOT_SAVED)
    return token


# --- the production store: passage --------------------------------------------------

#: `runner(argv, stdin, timeout) -> CompletedProcess` with bytes stdout and
#: stderr. On timeout it ends the child's whole process group and raises
#: `subprocess.TimeoutExpired`.
Runner = Callable[[Sequence[str], bytes, float], subprocess.CompletedProcess]


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass                            # already gone


def end_group(proc: subprocess.Popen, grace: float = KILL_GRACE_S) -> None:
    """End `proc` and every process in its group (it leads its own
    session): SIGTERM, then SIGKILL after `grace`. Reaps `proc`, and gives
    up on the pipes rather than hang if something outside the group still
    holds them."""
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.communicate(timeout=grace)
        _signal_group(proc.pid, signal.SIGKILL)     # a child that ignored TERM
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        proc.wait()


def run_in_own_group(argv: Sequence[str], stdin: bytes,
                     timeout: float) -> subprocess.CompletedProcess:
    """The default runner: `argv` in a NEW session (its own process group),
    `stdin` fed and closed, stdout and stderr captured. At `timeout` the
    whole group is ended (`end_group`) and `TimeoutExpired` raised, with
    none of the child's output in it. `subprocess.Popen` is looked up at
    call time, so a test's guard on it holds here too."""
    proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        end_group(proc)
        raise subprocess.TimeoutExpired(list(argv), timeout) from None
    except BaseException:
        end_group(proc)
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


class PassageCLI:
    """passage's store, through the `passage` on PATH. The store and the
    identities file are read from this process's environment when used
    (`PASSAGE_DIR`, `PASSAGE_IDENTITIES_FILE`, passage's own defaults), the
    environment passage inherits and `passage_backend` reads, so the
    insert, the read-back and passage itself always agree."""

    def __init__(self, *, runner: Runner | None = None,
                 timeout: float = SECRET_TIMEOUT_S) -> None:
        self.runner = run_in_own_group if runner is None else runner
        self.timeout = timeout

    @property
    def store(self) -> str:
        return passage_store()

    @property
    def identities(self) -> str:
        return (os.environ.get("PASSAGE_IDENTITIES_FILE")
                or os.path.join(os.path.expanduser("~"), ".passage", "identities"))

    def describe(self) -> str:
        return f"passage store {self.store}"

    def __repr__(self) -> str:
        return f"PassageCLI(store={self.store!r})"

    def entry_file(self, entry: str) -> str:
        return os.path.join(self.store, check_entry(entry) + ".age")

    def check(self) -> None:
        """passage is on PATH, and the store folder and the identities file
        exist. Each miss is refused with exit 5, before anyone signs in."""
        if shutil.which(PASSAGE) is None:
            raise StoreError("passage is not installed (no `passage` on PATH): "
                             "new tokens go to passage only", EXIT_STORE)
        if not os.path.isdir(self.store):
            raise StoreError(f"no passage store at {self.store} "
                             "(PASSAGE_DIR, else ~/.passage/store)", EXIT_STORE)
        if not os.path.isfile(self.identities):
            raise StoreError(f"no passage identities file at {self.identities} "
                             "(PASSAGE_IDENTITIES_FILE, else ~/.passage/identities)",
                             EXIT_STORE)

    def exists(self, entry: str) -> bool:
        """`<store>/<entry>.age`, or a folder `<store>/<entry>`, exists.
        Nothing is decrypted and nothing is run."""
        return (os.path.lexists(self.entry_file(entry))
                or os.path.isdir(os.path.join(self.store, entry)))

    def insert_argv(self, entry: str) -> list[str]:
        """What `insert` runs. The token is not in it, and neither is `-f`."""
        return [PASSAGE, "insert", "-m", check_entry(entry)]

    def insert(self, entry: str, token: str) -> str | None:
        """`passage insert -m ENTRY`, the token on stdin. Refuses an entry
        that exists, checked again just before the run. Returns a note when
        passage exited 0 but said something on stderr (a failed git commit
        exits 0). Raises StoreError (exit 8) on any failure,
        with `written` set when `<entry>.age` exists after it."""
        entry = check_entry(entry)
        token = _check_token(token)
        if self.exists(entry):
            raise StoreError(f"passage:{entry} already exists in {self.store}: not "
                             "overwritten (a person removes it: passage rm ENTRY)",
                             EXIT_NOT_SAVED, entry=entry)
        argv = self.insert_argv(entry)
        try:
            done = self.runner(argv, (token + "\n").encode("utf-8"), self.timeout)
        except subprocess.TimeoutExpired:
            raise StoreError(f"passage insert {entry} timed out after "
                             f"{self.timeout:g}s; its process group was ended",
                             EXIT_NOT_SAVED, entry=entry,
                             written=self._written(entry)) from None
        except FileNotFoundError:
            raise StoreError(f"passage insert {entry}: passage is not installed",
                             EXIT_NOT_SAVED, entry=entry,
                             written=self._written(entry)) from None
        except OSError as error:
            raise StoreError(f"passage insert {entry}: passage could not run "
                             f"({error.strerror or type(error).__name__})",
                             EXIT_NOT_SAVED, entry=entry,
                             written=self._written(entry)) from None
        said = first_line(done.stderr, token)
        if done.returncode != 0:
            raise StoreError(f"passage insert {entry} exited {done.returncode}: "
                             f"{said or '(nothing on stderr)'}", EXIT_NOT_SAVED,
                             entry=entry, written=self._written(entry))
        if not self._written(entry):
            raise StoreError(f"passage insert {entry} exited 0 but wrote no "
                             f"{self.entry_file(entry)}", EXIT_NOT_SAVED, entry=entry)
        if said:
            return (f"passage:{entry} was written, but passage said: {said} "
                    "(its git commit may have failed: the entry may be uncommitted)")
        return None

    def _written(self, entry: str) -> bool:
        return os.path.isfile(self.entry_file(entry))

    def read(self, entry: str) -> str:
        """The entry's first line through `secrets.passage_backend`, the
        resolver a `passage:ENTRY` source uses. Raises StoreError (exit 8)."""
        entry = check_entry(entry)
        try:
            return passage_backend(entry)
        except SecretError as error:
            raise StoreError(f"read back passage:{entry}: {scrub(str(error))}",
                             EXIT_NOT_SAVED, entry=entry) from None


# --- the test seam -------------------------------------------------------------------


def _corrupted(token: str) -> str:
    """A value that is not `token` (its last character changed)."""
    return token[:-1] + ("X" if token[-1:] != "X" else "Y")


class FakeTokenStore:
    """An in-memory store. `inserts` and `reads` record entry NAMES, in
    order, never values.

    Knobs, for `new`'s failure paths:
    - `fail_check`: `check` raises (exit 5);
    - `fail_insert`: `insert` raises and stores nothing;
    - `fail_insert_written`: `insert` stores the value, then raises with
      `written` set, as a passage whose commit hung until the timeout;
    - `corrupt`: the stored value is not the one inserted, so a read-back
      sees a mismatch. True changes the last character; a callable is
      applied to the token;
    - `fail_read`: `read` raises;
    - `note`: `insert` returns it, as a passage that exited 0 with stderr.
    `insert` refuses an entry that exists, as `PassageCLI` does."""

    def __init__(self, entries: Mapping[str, str] | None = None, *,
                 fail_check: bool = False, fail_insert: bool = False,
                 fail_insert_written: bool = False,
                 corrupt: bool | Callable[[str], str] = False,
                 fail_read: bool = False, note: str | None = None) -> None:
        self.entries: dict[str, str] = dict(entries or {})
        self.fail_check = fail_check
        self.fail_insert = fail_insert
        self.fail_insert_written = fail_insert_written
        self.corrupt = corrupt
        self.fail_read = fail_read
        self.note = note
        self.checks = 0
        self.inserts: list[str] = []
        self.reads: list[str] = []

    def describe(self) -> str:
        return "fake token store (in memory)"

    def __repr__(self) -> str:
        return f"FakeTokenStore({len(self.entries)} entries, inserts={len(self.inserts)})"

    def check(self) -> None:
        self.checks += 1
        if self.fail_check:
            raise StoreError(f"{self.describe()}: not usable (fake)", EXIT_STORE)

    def exists(self, entry: str) -> bool:
        entry = check_entry(entry)
        return entry in self.entries or any(name.startswith(entry + "/")
                                            for name in self.entries)

    def insert(self, entry: str, token: str) -> str | None:
        entry = check_entry(entry)
        token = _check_token(token)
        if self.exists(entry):
            raise StoreError(f"{entry} already exists in {self.describe()}: not "
                             "overwritten", EXIT_NOT_SAVED, entry=entry)
        self.inserts.append(entry)
        if self.fail_insert:
            raise StoreError(f"insert {entry}: failed (fake)", EXIT_NOT_SAVED, entry=entry)
        if self.corrupt:
            stored = self.corrupt(token) if callable(self.corrupt) else _corrupted(token)
        else:
            stored = token
        self.entries[entry] = stored
        if self.fail_insert_written:
            raise StoreError(f"insert {entry}: timed out after writing (fake)",
                             EXIT_NOT_SAVED, entry=entry, written=True)
        return self.note

    def read(self, entry: str) -> str:
        entry = check_entry(entry)
        self.reads.append(entry)
        if self.fail_read:
            raise StoreError(f"read back {entry}: failed (fake)", EXIT_NOT_SAVED,
                             entry=entry)
        if entry not in self.entries:
            raise StoreError(f"read back {entry}: no such entry in {self.describe()}",
                             EXIT_NOT_SAVED, entry=entry)
        return self.entries[entry]
