"""`session account use NAME`: move the HOST to another account.

A host signs in by one of two routes, and the `session` setting
`account.token_file` says which (`host_route`; it has no default):

- **the Keychain item** (the setting unset), the one item every seat on
  the Mac reads, so a write moves every seat with no env token of its own
  within about 50 s, and cold-starts each one's cache;
- **a token file** (the setting set): the login shell exports
  `CLAUDE_CODE_OAUTH_TOKEN` from it, so a seat keeps the token it launched
  with until it restarts, and only new launches from a shell that reads the
  file read the new file. A seat `session start` launches does NOT follow
  the file: the variable is stripped unless its agent file says `inherit:
  [CLAUDE_CODE_OAUTH_TOKEN]`, and with no token it signs in from Claude's
  stored credentials. On this route `use` never runs `security` (the steps
  below, as "the file route").

Either way `use` runs in the order that makes
failure cheap: it shows the plan before it asks for anything, and nothing
is written, backed up or run before every gate has passed. Each step
passes or exits, and **the item is not written before step 8**:

0. a malformed `--witness` (too long, unprintable, token-shaped) is
   refused before any output, and never echoed (exit 2);
1. look NAME up in the registry (unknown: exit 2, naming the known ones);
2. resolve NAME's token in memory through its sources (all fail: exit 4);
3. read the current item and parse-check it. If the host is already on
   NAME, say so and exit 0. Refuse an item the keychain could not write
   back (`security -i`'s line cap), or a ledger that is not private
   (exit 5). An item that cannot be read, while the setting
   `account.token_file` is unset, is refused naming that setting: the host
   may sign in from a token file (`tokenfile`), and `use` does not guess.
   If this process has `CLAUDE_CODE_OAUTH_TOKEN`, the message says so
   (exit 5);
4. show the plan: from, to, a note on the display profile (below), the
   item, the token's source, the seats that move and the cache that
   cold-starts (an injected seat scan), the backup directory, and the
   witness (or MISSING);
5. the gates, after the plan: no `--witness` is exit 2, no `--yes` is
   exit 3. Neither writes a file or runs `claude`;
6. preflight the token before any write: one `claude -p` in a tmp cwd,
   with an EMPTY tmp `CLAUDE_CONFIG_DIR`, an allowlisted env
   (`preflight_env`) and the token only in the child's env (anything but
   `accountok`: exit 4). `Preflight` is injected;
7. back the current item up (0600, to `keychain-backups/<UTC>.json`,
   parsed back; a failure is exit 5);
8. write the new item, read it back and compare fingerprints. A failed
   write or a mismatch restores the backup, exit 6. A restore that fails
   is exit 7, LOUD, naming the backup and the manual restore line;
9. append `{at, from, to, by, witness, backup, route}` to
   `host-ledger.jsonl` (0600); `route` is `keychain` or `token-file`.

The file route, where it differs:

3. read the file (`TokenFile.read_bytes`: a private regular file of one
   token line, else exit 5); already on NAME is exit 0;
4. the plan names the file (and a link's target), says the Keychain is not
   read, and gives the restart report in place of the cost: every live seat
   (the seat scan) keeps the token it launched with until it restarts, a
   seat `session start` launches does not follow the file unless its agent
   file inherits the variable, and whether THIS process holds the file's
   current token (compared in memory with `same_token`, shown as yes or
   no);
6. the preflight, as on the Keychain route, BEFORE the backup and the
   stage: a failed one leaves nothing on disk, the host file byte for byte
   as it was;
7. back the file up (0600, to `token-file-backups/<UTC>.token`, `O_EXCL`,
   read back as one token line);
8. stage the new token beside the target (0600, fsynced), check the staged
   file back through the `file:` resolver (`same_token`), then commit it
   (`os.replace`, a directory fsync; a link is kept), then read the target
   back. A failed stage or check discards the temp file: exit 6, the file
   never changed. A failed commit or verify restores the backup's bytes:
   exit 6; a restore that fails is exit 7, naming the backup and the
   manual `install` line.

**The display profile is not changed.** Both plans carry one `note:` line
after the target: which account `~/.claude.json` names (its
`oauthAccount.emailAddress`, read by `profile.read_profile`: read only,
size-capped, only the address shown), that Claude's `/status` and `claude
auth status` read it, that `use` does not change it, and that the token
decides billing (`session account verify` proves which). A file that names
no account, or cannot be read, is said so on the same line. No gate, no
exit code: it is a caveat, and `use` never writes that file (Claude Code
owns it and rewrites it at runtime).

A `--witness` over 64 characters is refused naming its length (`--witness
is 65 characters; at most 64`), never its value.

Exit codes: 0 done or already there; 2 usage, unknown name, no witness
(after the plan); 3 plan shown, no `--yes`; 4 token unresolvable or
preflight failed; 5 backup, registry or ledger failure; 6 write or verify
failed, restored; 7 restore failed.

Two deliberate calls:

- **Exit codes stay where they live**. `registry` and
  `keychain` keep their own `EXIT_*`; this module imports them, adds the
  three only `use` has (0, 3, 7), and `EXIT_CODES` is the whole table in
  one place. A shared `errors.py` would mean editing both earlier modules
  for no behaviour change.
- **The write MERGES**. The new item is the current
  item with its `claudeAiOauth` object REPLACED whole by
  `render_item(token)`'s. Every other top-level key (an `mcpOAuth` map,
  say) is kept, so a switch does not log every seat out of its MCP
  servers. Nothing inside the old `claudeAiOauth` is kept: a refresh token
  or expiry belongs to the account being left.

Three more, reasoned here:

- The **already-on check runs before the plan**, and the backup only after
  every gate. A plan-only run, or one stopped for a missing witness, leaves
  no file: a backup would only be one more file on disk holding a token.
- **The plan comes before the preflight.** A plan costs no `claude` call
  and puts the token in no child's env; the preflight still runs before
  any write, which is what it is for. So the plan says the preflight is
  still to come, not that the token works.
- A registry `fp` that does not match the resolved token is **exit 4**:
  the source holds some other token than the one registered for NAME, and
  verifying the write against either would be a guess.

No message, ledger row or exception text here carries a token, a blob or
a token's fingerprint (only the registry holds fingerprints).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from sessiontools.account import keychain as kc
from sessiontools.account import profile as pf
from sessiontools.account import registry as reg
from sessiontools.account import setuptoken as st
from sessiontools.account import tokenfile as tf
from sessiontools.start.secrets import is_private

EXIT_OK = 0
EXIT_USAGE = reg.EXIT_USAGE              # 2
EXIT_PLAN = 3
EXIT_UNRESOLVED = reg.EXIT_UNRESOLVED    # 4
EXIT_BACKUP = reg.EXIT_REGISTRY          # 5: backup, registry or ledger
EXIT_RESTORED = kc.EXIT_WRITE            # 6
EXIT_RESTORE_FAILED = 7

#: The exit-code table, the one place it is written whole.
EXIT_CODES: dict[int, str] = {
    EXIT_OK: "done, or already there",
    EXIT_USAGE: "usage, unknown name, no witness (after the plan)",
    EXIT_PLAN: "plan shown, no --yes",
    EXIT_UNRESOLVED: "token unresolvable or preflight failed",
    EXIT_BACKUP: "backup, registry or ledger failure",
    EXIT_RESTORED: "write or verify failed, restored",
    EXIT_RESTORE_FAILED: "restore failed",
}
assert EXIT_BACKUP == kc.EXIT_KEYCHAIN   # a failed read is the backup step's

BACKUP_DIR = "keychain-backups"
#: The token-file route's backups, beside the Keychain's.
FILE_BACKUP_DIR = "token-file-backups"
FILE_BACKUP_SUFFIX = ".token"
#: The ledger's `route` values.
ROUTE_KEYCHAIN = "keychain"
ROUTE_TOKEN_FILE = "token-file"
LEDGER_NAME = "host-ledger.jsonl"

PREFLIGHT_REPLY = "accountok"
PREFLIGHT_PROMPT = f"Reply with exactly: {PREFLIGHT_REPLY}"
PREFLIGHT_TIMEOUT = 120
#: The two names the preflight sets in its child, over whatever it inherits.
PREFLIGHT_SET = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN")

_MAX_WITNESS = 64


class UseError(Exception):
    """A step that refused. The message names a step, a path or a name,
    never a token. `code` is the exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class PreflightError(Exception):
    """The preflight could not run or was not answered `accountok`. The
    message never carries the token or claude's own output."""


# --- the preflight seam (step 6) -----------------------------------------------------

#: `preflight(token) -> None`; raises PreflightError when the token fails.
Preflight = Callable[[str], None]
#: `runner(argv, env, cwd) -> CompletedProcess` (text stdout/stderr).
PreflightRunner = Callable[[Sequence[str], Mapping[str, str], str],
                           subprocess.CompletedProcess]


def run_claude(argv: Sequence[str], env: Mapping[str, str],
               cwd: str) -> subprocess.CompletedProcess:
    """The default runner. `subprocess.run` is looked up at call time, so a
    test's guard on it holds here too."""
    return subprocess.run(list(argv), env=dict(env), cwd=cwd, input="", text=True,
                          capture_output=True, check=False, timeout=PREFLIGHT_TIMEOUT)


def preflight_env(token: str, config_dir: str,
                  base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child's environment: an ALLOWLIST of ours, then the token as
    `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CONFIG_DIR` at the empty tmp dir
    (so the run cannot write the real settings).

    A name passes only if it is in `setuptoken.ENV_ALLOW` or starts with one
    of `setuptoken.ENV_ALLOW_PREFIXES` (`LC_`). Claude Code reads many more
    credential and provider variables than a denylist would name (an API
    key, a Bedrock or Vertex switch, a base URL), and any of them could
    answer the preflight in place of the token, so a pass would prove
    nothing about it. It is the same list `new` gives `claude setup-token`,
    on purpose: one list, so the two cannot drift. That list also passes
    `BROWSER`, `DISPLAY` and three `SSH_*` names, which carry no
    credential."""
    env = {key: value for key, value in (os.environ if base is None else base).items()
           if key in st.ENV_ALLOW or key.startswith(st.ENV_ALLOW_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def claude_preflight(token: str, *, runner: PreflightRunner | None = None,
                     claude: str = "claude") -> None:
    """Prove `token` works before anything is written: one
    `claude -p 'Reply with exactly: accountok' --max-turns 1`, in a fresh
    tmp cwd, with a fresh EMPTY `CLAUDE_CONFIG_DIR`, and the token in the
    child's env only (argv is world-readable through `ps`). Both tmp dirs
    are removed afterwards. Anything but `accountok` raises PreflightError."""
    run = run_claude if runner is None else runner
    argv = [claude, "-p", PREFLIGHT_PROMPT, "--max-turns", "1"]
    with tempfile.TemporaryDirectory(prefix="account-preflight-") as root:
        cwd = os.path.join(root, "cwd")
        config = os.path.join(root, "claude-config")
        os.mkdir(cwd, 0o700)
        os.mkdir(config, 0o700)
        try:
            done = run(argv, preflight_env(token, config), cwd)
        except subprocess.TimeoutExpired:
            raise PreflightError(f"claude did not answer within {PREFLIGHT_TIMEOUT}s") from None
        except OSError as error:
            raise PreflightError(f"could not run {claude} "
                                 f"({error.strerror or type(error).__name__})") from None
    reply = done.stdout if isinstance(done.stdout, str) else (done.stdout or b"").decode(
        "utf-8", "replace")
    if done.returncode != 0 or reply.strip() != PREFLIGHT_REPLY:
        raise PreflightError(f"claude exited {done.returncode} and did not reply "
                             f"{PREFLIGHT_REPLY!r} (its output is not shown)")


# --- the seat scan seam (step 4's cost) ----------------------------------------------


@dataclass(frozen=True)
class Seat:
    """One live seat. On the Keychain route it follows the host, and
    `tokens` is its context, the cache a switch cold-starts (None when it
    cannot be read). On the token-file route it keeps what it holds until
    it restarts, and only its label is shown; one that `session start`
    launches does not follow the file even then, unless its agent file
    inherits `CLAUDE_CODE_OAUTH_TOKEN`."""

    label: str
    tokens: int | None


SeatScan = Callable[[], Iterable[Seat]]


def scan_seats() -> list[Seat]:
    """Every live seat on this Mac, from `session list`'s rows and their
    context (`window.of`). The rows do not say how a seat signs in, so the
    Keychain route counts every one as moving, and the token-file route
    names every one as keeping its launch token until it restarts."""
    from sessiontools import listing

    return [Seat(row.name, row.fullness.used if row.fullness else None)
            for row in listing.with_context(listing.rows())]


@dataclass(frozen=True)
class Cost:
    seats: int
    tokens: int
    unknown: int

    def line(self) -> str:
        from sessiontools.window import fmt_tokens

        noun = "seat moves" if self.seats == 1 else "seats move"
        text = (f"{self.seats} {noun} within ~50 s; their caches cold-start: "
                f"{fmt_tokens(self.tokens)} tokens")
        if self.unknown:
            text += f" (+{self.unknown} seat{'s' if self.unknown != 1 else ''} unread)"
        return text


def cost_of(seats: Iterable[Seat]) -> Cost:
    found = list(seats)
    known = [seat.tokens for seat in found if seat.tokens is not None]
    return Cost(len(found), sum(known), len(found) - len(known))


# --- files: the backup and the ledger --------------------------------------------------


def config_dir(home: str | os.PathLike | None = None) -> Path:
    """`<home>/.sessionkit/tools/accounts/` (`registry.accounts_dir`), where
    the registry, the ledger and the backups live."""
    return reg.accounts_dir(home)


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def write_backup(blob: bytes, directory: Path, at: datetime, *, suffix: str = ".json",
                 check: Callable[[bytes], object] | None = None,
                 into: str = "the Keychain") -> Path:
    """`blob` to `directory/<UTC-stamp><suffix>`, mode 0600, never over an
    existing file (a second backup in the same second gets `-2`, `-3`...).
    The directory is made 0700. Read back, it must be byte-equal and pass
    CHECK (an item by default; one token line for a token file): else exit
    5. INTO names what was not written, for the message."""
    try:
        reg.make_private_dirs(directory)
        if not directory.is_dir():
            raise NotADirectoryError(str(directory))
        stamp, n = _stamp(at), 1
        while True:
            path = directory / (f"{stamp}{suffix}" if n == 1 else f"{stamp}-{n}{suffix}")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                n += 1
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        back = path.read_bytes()
    except OSError as error:
        raise UseError(f"backup to {directory} failed "
                       f"({error.strerror or type(error).__name__}); nothing was written "
                       f"to {into}", EXIT_BACKUP) from None
    if back != blob or not is_private(str(path)):
        raise UseError(f"backup {path} did not read back as written, 0600; nothing was "
                       f"written to {into}", EXIT_BACKUP)
    try:
        (kc.parse_item if check is None else check)(back)
    except (kc.KeychainError, tf.TokenFileError) as error:
        raise UseError(f"backup {path} refused: {error}; nothing was written to "
                       f"{into}", EXIT_BACKUP) from None
    return path


def check_ledger(path: Path) -> None:
    """Before any write: an existing ledger must be a private regular file,
    so step 9 cannot fail on something we could have seen first."""
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_file() or not is_private(str(path)):
        raise UseError(f"ledger {path} refused: not a private (0600) regular file; "
                       "nothing was written", EXIT_BACKUP)


def append_ledger(path: Path, row: Mapping[str, object]) -> None:
    """One JSON line, appended, the file created 0600 (its folder 0700)."""
    reg.make_private_dirs(path.parent)
    line = (json.dumps(dict(row), separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


# --- the item ------------------------------------------------------------------------


def merged_item(current: bytes, token: str) -> bytes:
    """The current item with `claudeAiOauth` replaced whole by
    `render_item(token)`'s; every other top-level key kept."""
    item = kc.parse_item(current)
    item[kc.ITEM_KEY] = kc.parse_item(kc.render_item(token))[kc.ITEM_KEY]
    return json.dumps(item, separators=(",", ":")).encode("utf-8")


def restore_line(keychain: kc.Keychain, backup: Path) -> str:
    """The manual restore, by hand: the backup goes to `security -i` as hex
    on stdin, as `SecurityCLI.write` does, so the token is never in argv."""
    service = getattr(keychain, "service", kc.SERVICE)
    account = getattr(keychain, "account", None) or os.environ.get("USER", "$USER")
    return ("{ printf 'add-generic-password -U -s \"%s\" -a \"%s\" -X ' "
            f"'{service}' '{account}'; xxd -p '{backup}' | tr -d '\\n'; echo; }} "
            f"| {kc.SECURITY} -i")


def check_restorable(keychain: kc.Keychain, current: bytes) -> None:
    """Before any write: the item we would restore must go back the way it
    came. `SecurityCLI` feeds one `security -i` line (`kc.MAX_LINE`); a
    logged-in item carries a refresh token the new item drops, so the new
    item can fit while the current one does not. A failed verify would then
    be exit 7, and the manual restore line would hit the same cap. A
    keychain with no `write_script` has no such limit."""
    script = getattr(keychain, "write_script", None)
    if script is None:
        return
    try:
        script(current)
    except kc.KeychainError as error:
        raise UseError(f"the current item could not be restored if the switch failed: "
                       f"{error}; nothing was written", EXIT_BACKUP) from None


def _restore(keychain: kc.Keychain, backup_blob: bytes) -> tuple[bool, str]:
    """Put the backup back. (restored, how): `how` says whether the item was
    unchanged, restored, or why the restore failed."""
    try:
        if keychain.read() == backup_blob:
            return True, "the item was left unchanged"
    except kc.KeychainError:
        pass                       # unreadable: restore it anyway
    try:
        keychain.write(backup_blob)
        if keychain.read() != backup_blob:
            return False, "the restored item did not read back as the backup"
    except kc.KeychainError as error:
        return False, str(error)
    return True, "the previous item was restored from the backup"


# --- use -------------------------------------------------------------------------------


def _witness(witness: str | None) -> str | None:
    """The witness, or None when there is none. A malformed one (too long,
    unprintable, token-shaped) is refused here, before any output, and its
    value is never shown. A missing one is the caller's to report, after
    the plan (step 5)."""
    who = (witness or "").strip()
    if not who:
        return None
    if len(who) > _MAX_WITNESS:
        raise UseError(f"--witness is {len(who)} characters; at most {_MAX_WITNESS} "
                       "(the value is not shown)", EXIT_USAGE)
    if (not who.isprintable()
            or reg._TOKEN_SHAPE.search(who)):
        raise UseError(f"--witness must be a short name (at most {_MAX_WITNESS} "
                       "printable characters; the value is not shown)", EXIT_USAGE)
    return who


# --- the route: which sign-in the host uses ---------------------------------------------


#: The host's route, by the setting (`tokenfile.host_route`): it lives with the
#: token file so that `status` reads it without loading this module.
host_route = tf.host_route


def _unread_item(error: kc.KeychainError, environ: Mapping[str, str],
                 home) -> Exception:
    """What a failed read of the Keychain item becomes, when
    `account.token_file` is unset: still exit 5, and the message names the
    setting and where it lives, since a host with no Keychain item may sign
    in from a token file. When this process also has
    `CLAUDE_CODE_OAUTH_TOKEN`, it says so: `use` does not guess the route
    from it (a seat `session start` launched has it stripped, so its
    absence proves nothing either). The variable's value is never read
    beyond being present. With the setting set, the error is as it is."""
    try:
        if tf.token_file_setting(environ, home) is not None:
            return error
    except tf.TokenFileError as bad:
        return bad
    if (environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        return UseError(f"{error}. This shell exports CLAUDE_CODE_OAUTH_TOKEN, so the host "
                        "may sign in from a token file, and use does not guess which. Name "
                        f"the file in the setting {tf.SETTING_KEY}, in {tf.SETTING_WHERE}. "
                        "Nothing was written", EXIT_BACKUP)
    return UseError(f"{error}. If this host signs in from a token file instead, name it in "
                    f"the setting {tf.SETTING_KEY}, in {tf.SETTING_WHERE}. Nothing was "
                    "written", EXIT_BACKUP)


def default_by(env: Mapping[str, str] | None = None) -> str:
    """Who ran `use`, for the ledger: this session, else this user."""
    env = os.environ if env is None else env
    sid = env.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        return f"session:{sid}"
    return f"user:{env.get('USER') or 'unknown'}"


def use(name: str, *, witness: str | None, yes: bool = False,
        keychain: kc.Keychain | None = None,
        preflight: Preflight | None = None,
        seat_scan: SeatScan | None = None,
        home: str | os.PathLike | None = None,
        registry_path: str | os.PathLike | None = None,
        backends: Mapping[str, reg.Backend] | None = None,
        clock: Callable[[], datetime] | None = None,
        by: str | None = None,
        environ: Mapping[str, str] | None = None,
        out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Move the host to account NAME (module doc, steps 0-9), by the route
    `host_route` picks. Returns the exit code; prints the plan and outcome
    to `out`, refusals to `err`."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    environ = os.environ if environ is None else environ
    try:
        if registry_path is None:
            reg.refuse_stranded(home)                          # stranded: 5, nothing read
        route = host_route(environ, home, keychain=keychain)   # bad setting/file: 5
        return _use(name, witness=witness, yes=yes, route=route,
                    preflight=claude_preflight if preflight is None else preflight,
                    seat_scan=scan_seats if seat_scan is None else seat_scan,
                    home=home, registry_path=registry_path, backends=backends,
                    clock=clock or (lambda: datetime.now(UTC)),
                    by=by or default_by(), environ=environ, out=out)
    except (UseError, reg.RegistryError, kc.KeychainError, tf.TokenFileError) as error:
        print(f"session account use: {error}", file=err)
        return error.code


@dataclass(frozen=True)
class _Target:
    """Steps 1-2's result, which both routes start from."""

    name: str
    account: reg.Account
    resolved: reg.Resolved
    fp: str
    registry: reg.Registry
    cdir: Path

    @property
    def token(self) -> str:
        return self.resolved.token


def _use(name: str, *, witness: str | None, yes: bool,
         route: kc.Keychain | tf.TokenFile,
         preflight: Preflight, seat_scan: SeatScan, home, registry_path, backends,
         clock: Callable[[], datetime], by: str, environ: Mapping[str, str],
         out: TextIO) -> int:
    # 0. a malformed witness, before any output (a missing one: step 5)
    who = _witness(witness)

    # 1. look NAME up
    path = reg.default_path(home) if registry_path is None else Path(registry_path)
    registry = reg.load(path)
    account = registry.get(name)                       # unknown: exit 2

    # 2. the token, in memory only
    resolved = reg.resolve(account, backends)           # every source fails: exit 4
    target_fp = reg.fingerprint(resolved.token)
    if account.fp is not None and account.fp != target_fp:
        raise UseError(f"account {name!r}: {resolved.source} holds a token that is not "
                       f"the one registered for {name!r} (fingerprints differ); nothing "
                       "was touched", EXIT_UNRESOLVED)
    target = _Target(name, account, resolved, target_fp, registry,
                     config_dir(home) if registry_path is None else path.parent)
    common = {"who": who, "yes": yes, "preflight": preflight, "seat_scan": seat_scan,
              "clock": clock, "by": by, "out": out}
    if isinstance(route, tf.TokenFile):
        return _use_token_file(target, route, environ=environ, home=home, **common)
    return _use_keychain(target, route, environ=environ, home=home, **common)


def _preflight(preflight: Preflight, target: _Target) -> None:
    """Step 6, the same on both routes: anything but a pass is exit 4."""
    try:
        preflight(target.token)
    except PreflightError as error:
        raise UseError(f"preflight of {target.name!r} failed: {error}; nothing was "
                       "touched", EXIT_UNRESOLVED) from None
    except Exception as error:  # noqa: BLE001 - an injected seam; its text is not ours
        raise UseError(f"preflight of {target.name!r} failed ({type(error).__name__}); "
                       "nothing was touched", EXIT_UNRESOLVED) from None


def _ledger(ledger: Path, row: Mapping[str, object], name: str) -> None:
    try:
        append_ledger(ledger, row)
    except OSError as error:
        raise UseError(f"the host IS now on {name}, but the ledger row was not written "
                       f"to {ledger} ({error.strerror or type(error).__name__})",
                       EXIT_BACKUP) from None


def _row(target: _Target, from_name: str, *, at: datetime, by: str, who: str,
         backup: Path, route: str) -> dict[str, object]:
    """The ledger row: names, the backup's path and the route; no token, no
    fingerprint, no path to the token file."""
    return {"at": at.astimezone(UTC).isoformat(timespec="seconds"), "from": from_name,
            "to": target.name, "by": by, "witness": who, "backup": str(backup),
            "route": route}


# --- the Keychain route ----------------------------------------------------------------


def _use_keychain(target: _Target, keychain: kc.Keychain, *, who: str | None, yes: bool,
                  preflight: Preflight, seat_scan: SeatScan,
                  clock: Callable[[], datetime], by: str, environ: Mapping[str, str],
                  home, out: TextIO) -> int:
    name, account = target.name, target.account

    # 3. the current item: parse-check, already there, restorable, the ledger
    try:
        current = keychain.read()                       # failure: exit 5
    except kc.KeychainError as error:
        raise _unread_item(error, environ, home) from None
    current_fp = kc.item_fp(current)                    # no claudeAiOauth: exit 5
    if current_fp == target.fp:
        print(f"already on {name} ({account.email}); nothing written", file=out)
        return EXIT_OK
    was = target.registry.by_fp(current_fp)
    from_name = was.name if was is not None else "unknown"
    ledger = target.cdir / LEDGER_NAME
    backups = target.cdir / BACKUP_DIR
    check_ledger(ledger)
    check_restorable(keychain, current)                 # too long to put back: exit 5
    new_item = merged_item(current, target.token)

    # 4. the plan
    try:
        cost = cost_of(seat_scan()).line()
    except Exception as error:  # noqa: BLE001 - the cost is shown, never a gate
        cost = f"unknown (the seat scan failed: {type(error).__name__})"
    print(f"use {name} ({account.email}): the host moves from {from_name} to {name}\n"
          f"{pf.note_line(pf.read_profile(home))}\n"
          f"  item:    {keychain.describe()}\n"
          f"  token:   {target.resolved.source} (preflight: runs before the write)\n"
          f"  cost:    {cost}\n"
          f"  backup:  {backups}{os.sep} (written before the write)\n"
          f"  witness: {who or 'MISSING'}", file=out)

    # 5. the gates, after the plan; neither writes a file or runs claude
    if who is None:
        raise UseError("use needs --witness WHO to write: the Keychain item is never "
                       "touched without a named witness. The plan above is all that "
                       "ran; nothing was written", EXIT_USAGE)
    if not yes:
        print("plan only: re-run with --yes to write the item", file=out)
        return EXIT_PLAN

    # 6. preflight, before any write
    _preflight(preflight, target)

    # 7. the backup
    backup = write_backup(current, backups, clock())
    print(f"backup:  {backup}", file=out)

    # 8. write, then verify
    failure = None
    try:
        keychain.write(new_item)
    except kc.KeychainError as error:
        failure = f"the write failed: {error}"
    if failure is None:
        try:
            if kc.item_fp(keychain.read()) != target.fp:
                failure = f"the item did not read back as {name}"
        except kc.KeychainError as error:
            failure = f"the read-back failed: {error}"
    if failure is not None:
        restored, how = _restore(keychain, current)
        if restored:
            raise UseError(f"{failure}; {how} ({backup}). The host is still on "
                           f"{from_name}.", EXIT_RESTORED)
        raise UseError(
            "!!! RESTORE FAILED: THE HOST'S KEYCHAIN ITEM MAY BE BROKEN !!!\n"
            f"  {failure}\n  then the restore failed: {how}\n"
            f"  backup:  {backup}\n"
            f"  restore it by hand NOW:\n    {restore_line(keychain, backup)}",
            EXIT_RESTORE_FAILED)

    # 9. the ledger
    _ledger(ledger, _row(target, from_name, at=clock(), by=by, who=who, backup=backup,
                         route=ROUTE_KEYCHAIN), name)
    print(f"done: the host is on {name} ({account.email}); ledger {ledger}", file=out)
    return EXIT_OK


# --- the token-file route --------------------------------------------------------------


#: The token-file plan's and done report's line on `session start` seats:
#: they do not read the file, unless the agent file carries the variable.
START_NOTICE = ("  session start: its seats do NOT follow the file unless their agent "
                "file has inherit: [CLAUDE_CODE_OAUTH_TOKEN]; without it they sign in "
                "from Claude's stored credentials")


def restart_report(seat_scan: SeatScan) -> str:
    """Which live seats keep the token they launched with: all of them, by
    label. `use` cannot see a seat's token (reading another process's env
    would show it), so it never claims to know one; a seat with no env
    token at all is named too, and the wording is true of it as well. A
    failed scan is reported, never a gate."""
    try:
        labels = [seat.label for seat in seat_scan()]
    except Exception as error:  # noqa: BLE001 - the report is shown, never a gate
        return f"unknown (the seat scan failed: {type(error).__name__})"
    if not labels:
        return "no live seats"
    if len(labels) == 1:
        return ("1 live seat keeps the token it launched with until it restarts: "
                f"{labels[0]}")
    return (f"{len(labels)} live seats keep the token they launched with until they "
            f"restart: {', '.join(labels)}")


def holds_token(environ: Mapping[str, str], token: str) -> bool:
    """Whether THIS process's `CLAUDE_CODE_OAUTH_TOKEN` is TOKEN, compared in
    memory with `same_token`: only the yes or no ever leaves here."""
    own = (environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    return bool(own) and reg.same_token(own, token)


def _restore_file(route: tf.TokenFile, backup_blob: bytes) -> tuple[bool, str]:
    """Put the file back as it was. (restored, how), as `_restore`."""
    try:
        if route.read_bytes() == backup_blob:
            return True, "the token file was left unchanged"
    except tf.TokenFileError:
        pass                       # unreadable: restore it anyway
    try:
        route.restore(backup_blob)
    except tf.TokenFileError as error:
        return False, str(error)
    return True, "the previous token file was restored from the backup"


def _use_token_file(target: _Target, route: tf.TokenFile, *, who: str | None, yes: bool,
                    preflight: Preflight, seat_scan: SeatScan,
                    clock: Callable[[], datetime], by: str, environ: Mapping[str, str],
                    home, out: TextIO) -> int:
    """The file route (module doc). Nothing here runs `security`."""
    name, account = target.name, target.account

    # 3. the current file: one private token line; already there; the ledger
    current_blob = route.read_bytes()                   # refused: exit 5
    current = tf.parse_token_line(current_blob, route.describe())
    if reg.same_token(current, target.token):
        print(f"already on {name} ({account.email}); nothing written", file=out)
        return EXIT_OK
    was = target.registry.by_fp(reg.fingerprint(current))
    from_name = was.name if was is not None else "unknown"
    ledger = target.cdir / LEDGER_NAME
    backups = target.cdir / FILE_BACKUP_DIR
    check_ledger(ledger)

    # 4. the plan, with the restart report
    seats = restart_report(seat_scan)
    this_seat = "yes" if holds_token(environ, current) else "no"
    report = (f"  seats:   {seats}; new launches from a shell that reads the file "
              "read the new file\n"
              f"{START_NOTICE}\n"
              f"  this seat holds the old token: {this_seat}")
    print(f"use {name} ({account.email}): the host moves from {from_name} to {name}\n"
          f"{pf.note_line(pf.read_profile(home))}\n"
          f"  file:    {route.describe()}\n"
          "  keychain: not read: a seat that signs in from it does not move\n"
          f"  token:   {target.resolved.source} (preflight: runs before the write)\n"
          f"{report}\n"
          f"  backup:  {backups}{os.sep} (written before the write)\n"
          f"  witness: {who or 'MISSING'}", file=out)

    # 5. the gates, after the plan; neither writes a file or runs claude
    if who is None:
        raise UseError("use needs --witness WHO to write: the host's token file is never "
                       "touched without a named witness. The plan above is all that "
                       "ran; nothing was written", EXIT_USAGE)
    if not yes:
        print("plan only: re-run with --yes to swap the file", file=out)
        return EXIT_PLAN

    # 6. preflight, before the backup and the stage: a failure leaves nothing
    _preflight(preflight, target)

    # 7. the backup
    backup = write_backup(current_blob, backups, clock(), suffix=FILE_BACKUP_SUFFIX,
                          check=lambda blob: tf.parse_token_line(blob, "the backup"),
                          into="the token file")
    print(f"backup:  {backup}", file=out)

    # 8. stage and check it; commit; verify
    staged = None
    try:
        staged = route.stage(target.token)
        route.check_staged(staged, target.token)
    except tf.TokenFileError as error:
        route.discard(staged)
        raise UseError(f"{error}; nothing was swapped: {route.target} is as it was. The "
                       f"host is still on {from_name}.", EXIT_RESTORED) from None
    failure = None
    try:
        route.commit(staged)
    except tf.TokenFileError as error:
        failure = f"the swap failed: {error}"
    route.discard(staged)                     # gone after a commit; a failed one's leftover
    if failure is None:
        try:
            if not reg.same_token(route.read(), target.token):
                failure = f"the token file did not read back as {name}"
        except tf.TokenFileError as error:
            failure = f"the read-back failed: {error}"
    if failure is not None:
        restored, how = _restore_file(route, current_blob)
        if restored:
            raise UseError(f"{failure}; {how} ({backup}). The host is still on "
                           f"{from_name}.", EXIT_RESTORED)
        raise UseError(
            "!!! RESTORE FAILED: THE HOST'S TOKEN FILE MAY BE BROKEN !!!\n"
            f"  {failure}\n  then the restore failed: {how}\n"
            f"  backup:  {backup}\n"
            f"  restore it by hand NOW:\n    {route.restore_line(backup)}",
            EXIT_RESTORE_FAILED)

    # 9. the ledger, then the restart report again
    _ledger(ledger, _row(target, from_name, at=clock(), by=by, who=who, backup=backup,
                         route=ROUTE_TOKEN_FILE), name)
    print(f"done: the host's token file is on {name} ({account.email}); ledger {ledger}\n"
          f"{report}", file=out)
    return EXIT_OK
