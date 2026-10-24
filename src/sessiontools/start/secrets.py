"""Declared secrets, resolved at launch.

An agent file names a secret, `NAME: backend:ref`, and never holds its
value. A LIVE launch resolves each one through its backend, reports it BY
NAME, and hands the values to the launched shell in a private 0600 file
the prelude sources last and deletes (`environment.render_env_prelude`),
with the shred trap as the backstop if the shell dies first
(`plan.render_launch_script`). A value is never in argv, which `ps` shows
to every user, and never in the prelude text, which a dry run prints.

A dry run resolves nothing: calling a backend is an act (a subprocess, a
PIN dialog, a key touch), and a dry run performs none
(`environment.declared_secrets`).

Any secret that fails ABORTS the launch. A session missing a credential it
declared is a broken session, and the failure this replaces was a launcher
that quietly found the value somewhere else.

An `environment: inherit:` name rides the same file: its value
is the launcher's own, read at plan time on a live launch only, reported by
name, and a name that is not set is said and skipped, never an abort.

Ported from the old launcher (`bridge/session-start`, lines 662-961 and
3115-3140), with one change: a result's value never appears in its repr.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from sessiontools.start.environment import DeclaredSecret, check_env_name
from sessiontools.start.errors import PROG, LaunchError

# Long enough for a human to find a PIN dialog and touch a key: a
# card-backed `pass` entry waits on both.
SECRET_TIMEOUT_S = 120

# The wedged-scdaemon signature. After a smartcard is unplugged and
# replugged (2030-06-09), every decrypt fails INSTANTLY with this in gpg's
# stderr. Restarting scdaemon is the proven remedy; retrying without the
# restart just fails again.
CARD_ERROR_MARKER = "Card error"

# The live file's name: `<prefix>XXXXXXXX` (the mkstemp shape).
SECRET_FILE_PREFIX = "session-start-secrets-"


class SecretError(Exception):
    """A backend could not produce a value. Carries a REASON, never a value."""


@dataclass(frozen=True)
class SecretResult:
    """One secret's outcome. The report reads name, backend and error, and
    never value, so "reported by name" holds by construction; the value is
    kept out of the repr too, so no traceback or log line can print it."""

    name: str
    backend: str | None
    ref: str | None
    value: str | None = field(default=None, repr=False)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _pass_show(ref: str) -> subprocess.CompletedProcess:
    """One `pass show REF`. The failures no retry can help - no `pass`, a
    `pass` that will not run, and nobody answering the PIN or the touch -
    are SecretErrors, so each is reported by name and aborts the launch."""
    try:
        return subprocess.run(["pass", "show", ref], capture_output=True, text=True,
                              timeout=SECRET_TIMEOUT_S, check=False)
    except FileNotFoundError:
        raise SecretError("`pass` is not installed") from None
    except OSError as error:            # not executable, a bad interpreter, ...
        raise SecretError(f"`pass` could not run: {error.strerror or error}") from None
    except subprocess.TimeoutExpired:
        raise SecretError(f"`pass` timed out after {SECRET_TIMEOUT_S}s") from None


def _restart_scdaemon() -> None:
    try:
        subprocess.run(["gpgconf", "--kill", "gpg-agent", "scdaemon"],
                       capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass    # the retry reports the true state


def pass_backend(ref: str) -> str:
    """`pass:REF`: the first line of `pass show REF` (the password-store
    convention; later lines are notes).

    It says so on stderr BEFORE the wait, because gpg may raise a PIN
    dialog and the key may wait for a touch: the measured failure
    (2030-06-09) was a pinentry behind a fullscreen window while the key
    blinked unseen. A "Card error" gets ONE scdaemon restart and ONE retry,
    loudly; any other failure is reported as it is."""
    print(f"{PROG}: pass:{ref} -- gpg may raise a PIN dialog and/or wait for a "
          "security-key touch", file=sys.stderr)
    result = _pass_show(ref)
    if result.returncode != 0 and CARD_ERROR_MARKER in result.stderr:
        print(f"{PROG}: gpg reported a smartcard 'Card error' (wedged scdaemon?) -- "
              "restarting it and retrying once", file=sys.stderr)
        _restart_scdaemon()
        result = _pass_show(ref)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise SecretError(detail[0] if detail else f"`pass` exited {result.returncode}")
    value = result.stdout.split("\n", 1)[0].strip()
    if not value:
        raise SecretError("`pass` returned an empty value")
    return value


def passage_store(environ: Mapping[str, str] | None = None) -> str:
    """passage's store: `$PASSAGE_DIR`, else `~/.passage/store` (passage's
    own default, `src/password-store.sh`). Read when called, so a test's
    HOME and PASSAGE_DIR hold."""
    env = os.environ if environ is None else environ
    return env.get("PASSAGE_DIR") or os.path.join(os.path.expanduser("~"), ".passage", "store")


def passage_entry_file(ref: str, store: str | None = None) -> str:
    """`<store>/<ref>.age`, the file passage keeps REF in. Refuses a ref
    that could name a file outside the store (absolute, or a `..` part:
    passage's own `check_sneaky_paths`) or that names nothing."""
    parts = ref.split("/")
    if not ref or ref.startswith("/") or ".." in parts or not parts[-1]:
        raise SecretError(f"passage:{ref} refused: not an entry name inside the store")
    return os.path.join(passage_store() if store is None else store, ref + ".age")


def _passage_show(ref: str) -> subprocess.CompletedProcess:
    """One `passage show REF`. As `_pass_show`: no `passage`, a `passage`
    that will not run, and nobody answering Touch ID or the key touch are
    SecretErrors."""
    try:
        return subprocess.run(["passage", "show", ref], capture_output=True, text=True,
                              timeout=SECRET_TIMEOUT_S, check=False)
    except FileNotFoundError:
        raise SecretError("`passage` is not installed") from None
    except OSError as error:            # not executable, a bad interpreter, ...
        raise SecretError(f"`passage` could not run: {error.strerror or error}") from None
    except subprocess.TimeoutExpired:
        raise SecretError(f"`passage` timed out after {SECRET_TIMEOUT_S}s") from None


def passage_backend(ref: str) -> str:
    """`passage:REF`: the first line of `passage show REF` (age, not gpg).
    `pass:` is a different store and is untouched.

    It REFUSES a missing entry before it decrypts: with no
    `<store>/<ref>.age` there is nothing to show, so a typo costs no touch,
    and a folder (which `passage show` prints as a tree, exit 0) never
    resolves to its own name. Then it says on stderr, BEFORE the wait, that
    age may ask for Touch ID or a key touch. No scdaemon restart: that is
    gpg's. A failure is reported as it is; the reason never holds a value."""
    store = passage_store()
    entry = passage_entry_file(ref, store)
    if not os.path.isfile(entry):
        raise SecretError(f"no passage entry {ref} in {store}")
    print(f"{PROG}: passage:{ref} -- age may ask for Touch ID or a key touch",
          file=sys.stderr)
    result = _passage_show(ref)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise SecretError(detail[0] if detail else f"`passage` exited {result.returncode}")
    value = result.stdout.split("\n", 1)[0].strip()
    if not value:
        raise SecretError("`passage` returned an empty value")
    return value


Backend = Callable[[str], str]

#: What `session start` resolves through. `passage` is NOT here: it is the
#: account registry's only.
BACKENDS: dict[str, Backend] = {"pass": pass_backend}


def resolve_secrets(declared: list[DeclaredSecret],
                    backends: Mapping[str, Backend] | None = None) -> list[SecretResult]:
    """One result per declared secret, in declared order. A failure is
    recorded, never retried elsewhere and never filled in from somewhere
    else."""
    table = BACKENDS if backends is None else backends
    results = []
    for name, backend, ref in declared:
        resolver = table.get(backend)
        if resolver is None:
            results.append(SecretResult(name, backend, ref, error=(
                f"unknown backend {backend!r} (known: {', '.join(sorted(table))})")))
            continue
        try:
            value = resolver(ref)
        except SecretError as error:
            results.append(SecretResult(name, backend, ref, error=str(error)))
        else:
            results.append(SecretResult(name, backend, ref, value=value))
    return results


def report_lines(results: list[SecretResult]) -> list[str]:
    """NAMES ONLY: which credentials this session carries is a question the
    launch output answers without anyone reading the agent file."""
    return [f"secret: {r.name} loaded from {r.backend}" if r.ok
            else f"secret: {r.name} NOT SET -- {r.error}" for r in results]


def report_secrets(results: list[SecretResult]) -> None:
    """Successes to stdout, as part of the launch banner; failures to
    stderr, so they survive a piped stdout."""
    for result, line in zip(results, report_lines(results)):
        print(line, file=sys.stdout if result.ok else sys.stderr)


# ---------- environment: inherit: -------------------------------------------

#: The `backend` an inherited name's result carries: where its value came from.
INHERIT_BACKEND = "inherit"
INHERIT_ABSENT = "not set in the launcher's environment"


def read_inherited(names: tuple | list,
                   environ: Mapping[str, str]) -> list[SecretResult]:
    """One result per `environment: inherit:` name, in declared order: the
    value the LAUNCHER's own environment holds (raw, before `host_environ`
    drops any credential), or INHERIT_ABSENT. Absence is NOT an abort, unlike
    a secret: inheriting nothing is a valid outcome of an opt-in, and the
    banner says so. The value rides in the result's repr-less field, to the
    0600 file and nowhere else. A dry run never calls this: it reads no
    value."""
    results = []
    for name in names:
        check_env_name(name, "environment: inherit:")
        if name in environ:
            results.append(SecretResult(name, INHERIT_BACKEND, None, value=environ[name]))
        else:
            results.append(SecretResult(name, INHERIT_BACKEND, None, error=INHERIT_ABSENT))
    return results


def inherit_report_lines(results: list[SecretResult]) -> list[str]:
    """NAMES ONLY, as the secrets' lines are: which of the launcher's
    variables this session carries, said at launch."""
    return [f"inherit: {r.name} carried from the launcher's environment" if r.ok
            else f"inherit: {r.name} {INHERIT_ABSENT} -- not carried" for r in results]


def report_inherited(results: list[SecretResult]) -> None:
    """Carried names to stdout, in the banner beside the secrets; a name
    that was not set to stderr, so it survives a piped stdout. Neither
    stops the launch."""
    for result, line in zip(results, inherit_report_lines(results)):
        print(line, file=sys.stdout if result.ok else sys.stderr)


def secret_file_text(results: list[SecretResult]) -> str:
    """The file the prelude sources: one `export NAME=VALUE` per resolved
    secret or carried `inherit:` name, the value shell-quoted."""
    lines = []
    for result in results:
        if result.ok:
            check_env_name(result.name, "secret")
            lines.append(f"export {result.name}={shlex.quote(str(result.value))}\n")
    return "".join(lines)


def is_private(path: str) -> bool:
    """True when only the owner can read or write it: mode 0600."""
    return stat.S_IMODE(os.stat(path).st_mode) == 0o600


def resolve_or_abort(declared: list[DeclaredSecret], *, resume: bool = False,
                     backends: Mapping[str, Backend] | None = None) -> list[SecretResult]:
    """Resolve, report, and refuse the launch if any secret failed."""
    results = resolve_secrets(declared, backends)
    report_secrets(results)
    failed = [result for result in results if not result.ok]
    if failed:
        what = "a resumed" if resume else "a launched"
        raise LaunchError(
            f"launch aborted: {len(failed)} declared secret(s) failed to resolve "
            f"(a session missing a credential it declared is a broken session, "
            f"not {what} one)")
    return results
