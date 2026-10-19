"""The launch record: what a session started with, written once.

When the session starts, it writes the environment out to a file, so
what goes there can be inspected; then the start command and the path of
each file read at startup. So every launch with a session
directory writes, beside its other files:

    <session dir>/launch/             0700
        .gitignore                    0600  `*`: the dir and itself, in any repo
        env                           0600  NAME=value, sorted, secrets masked
        command                       0600  the vendor argv, the `session start`
                                            command line, the working directory
        startup-files                 0600  TAG<TAB>PATH, one per line

WHO WRITES IT: the launch script, on the line before the vendor, by
running this module (`render_record_call`). Two reasons, both measured: a
keyed launch placed in tmux has no session
directory until its pane mints one, so only the script knows where to
write; and the env the script holds THERE is the env the vendor inherits -
after the identity block and the declared-environment prelude - which is
the whole point of recording it.

THE GIT GUARD, before the first record byte: `launch/` and its
`.gitignore` are made first, then `git check-ignore` must say `env` is
ignored, or that there is no repo at all. A real host's sessions root has
been a git repo that does not ignore session dirs, so this is not
hypothetical. If the answer is "tracked-able", nothing else is written and one loud line
says so.

WRITTEN ONCE, NEVER READ BACK: an existing `launch/` is left exactly as
it is, and every file is created O_EXCL. A record must never cost a
session: any failure here is one stderr line and exit 0, and the vendor
still starts.

MASKING (v1, deliberately simple): a NAME
that says secret (TOKEN, KEY, SECRET, PASSWORD, PASSWD, AUTH, CREDENTIAL,
the word PAT, any AWS_ prefix), a name the agent file DECLARED under
`environment: secrets:`, or a value that looks like a credential (32+
chars, no `/` or space, Shannon entropy over 4 bits) is masked as `*`
times its length, so set-versus-empty stays visible. Identity names stay
clear. A miss is a later bug, not a reason to gold-plate this one.
"""

from __future__ import annotations

import argparse
import math
import os
import shlex
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import NamedTuple

from sessiontools.settings import ensure_private_dir

RECORD_DIR = "launch"
GITIGNORE = ".gitignore"
ENV_FILE = "env"
COMMAND_FILE = "command"
STARTUP_FILE = "startup-files"
RECORD_FILES = (ENV_FILE, COMMAND_FILE, STARTUP_FILE)

PROG = "session start: launch record"
MODULE = "sessiontools.start.record"
#: How the script runs `main`: `-c`, not `-m`. Under `-m` runpy imports the
#: package first, the package imports this module, and every launch would
#: print runpy's "found in sys.modules" RuntimeWarning (measured).
ENTRY = f"import sys; from {MODULE} import main; sys.exit(main())"
GIT_TIMEOUT_S = 10

# ---------- masking --------------------------------------------------------

#: Substrings of a NAME that say "this holds a credential" (case-insensitive).
SECRET_SUBSTRINGS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "AUTH", "CREDENTIAL")
#: Whole `_`-separated words only: as a bare substring PAT is in PATH.
SECRET_WORDS = ("PAT",)
SECRET_PREFIXES = ("AWS_",)
#: Names that would otherwise match and are not credentials: AUTH in AUTHOR.
NOT_SECRET_SUBSTRINGS = ("AUTHOR",)

#: Identity stays clear, whatever a rule above or the backstop says.
CLEAR_NAMES = frozenset({"CLAUDE_CODE_AGENT", "TM_AGENT_ADDRESS", "EMAIL",
                         "CLAUDE_SESSION_ID", "PATH"})
CLEAR_SUFFIXES = ("_SESSION_ID",)

BACKSTOP_MIN_LENGTH = 32
BACKSTOP_MIN_ENTROPY = 4.0     # bits/char: hex tops out at 4.0, so a sha or uuid never trips it


def is_clear_name(name: str) -> bool:
    upper = name.upper()
    return upper in CLEAR_NAMES or upper.endswith(CLEAR_SUFFIXES)


def is_secret_name(name: str) -> bool:
    """Does the NAME say it holds a credential?"""
    upper = name.upper()
    if upper.startswith(SECRET_PREFIXES):
        return True
    probe = upper
    for benign in NOT_SECRET_SUBSTRINGS:
        probe = probe.replace(benign, "")
    if any(part in probe for part in SECRET_SUBSTRINGS):
        return True
    return any(word in upper.split("_") for word in SECRET_WORDS)


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def looks_secret_value(value: str) -> bool:
    """The backstop for a credential under an innocent name: long, one
    unbroken token (no path, no words), and random-looking."""
    if len(value) < BACKSTOP_MIN_LENGTH:
        return False
    if "/" in value or any(char.isspace() for char in value):
        return False
    return shannon_entropy(value) > BACKSTOP_MIN_ENTROPY


def should_mask(name: str, value: str, declared: Iterable[str] = ()) -> bool:
    if name in declared:
        return True
    if is_clear_name(name):
        return False
    return is_secret_name(name) or looks_secret_value(value)


def mask(value: str) -> str:
    """Length kept, content gone: an empty value stays empty."""
    return "*" * len(value)


def _escape(value: str) -> str:
    """One line per variable: a value's own newlines are written `\\n`."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def render_env(environ: Mapping[str, str], declared: Iterable[str] = ()) -> str:
    """`NAME=value` per line, sorted by name, secret values masked."""
    declared = frozenset(declared)
    lines = []
    for name in sorted(environ):
        value = environ[name]
        shown = mask(value) if should_mask(name, value, declared) else _escape(value)
        lines.append(f"{name}={shown}")
    return "\n".join(lines) + "\n" if lines else ""


# ---------- the startup files ----------------------------------------------

#: Per vendor: what it reads at startup that the launcher does not write.
#: (tag, path relative to HOME) and (tag, path relative to each directory
#: from the working directory up to /), recorded only if present at launch.
STARTUP_LAYERS: dict[str, dict] = {
    "claude": {
        "home": (("claude-md", ".claude/CLAUDE.md"),
                 ("settings", ".claude/settings.json"),
                 ("mcp", ".claude.json")),
        "chain": (("claude-md", "CLAUDE.md"),
                  ("claude-md", ".claude/CLAUDE.md"),
                  ("claude-md", "CLAUDE.local.md")),
        "workdir": (("settings", ".claude/settings.json"),
                    ("settings", ".claude/settings.local.json"),
                    ("mcp", ".mcp.json")),
    },
}


def vendor_startup_files(vendor: str, work_dir: str, home: str | None) -> list[tuple[str, str]]:
    """The vendor's own startup layer that exists right now: the user's
    files, then the CLAUDE.md chain from / down to the working directory
    (the order claude reads them), then the working directory's settings."""
    layers = STARTUP_LAYERS.get(vendor)
    if not layers:
        return []
    found: list[tuple[str, str]] = []

    def add(tag: str, path: str) -> None:
        if os.path.isfile(path) and (tag, path) not in found:
            found.append((tag, path))

    if home:
        for tag, rel in layers["home"]:
            add(tag, os.path.join(home, rel))
    chain = []
    directory = os.path.abspath(work_dir)
    while True:
        chain.append(directory)
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    for directory in reversed(chain):
        for tag, rel in layers["chain"]:
            add(tag, os.path.join(directory, rel))
    for tag, rel in layers["workdir"]:
        add(tag, os.path.join(work_dir, rel))
    return found


def render_startup_files(files: Iterable[tuple[str, str]]) -> str:
    lines = [f"{tag}\t{path}" for tag, path in files]
    return "\n".join(lines) + "\n" if lines else ""


# ---------- the plan's half: what the script will pass ----------------------

class LaunchRecord(NamedTuple):
    """What the launch script hands this module, planned launcher-side.

    session_dir   the session directory: a literal path, or for a keyed
                  launch placed in tmux a bash expression (the pane mints
                  the id), already rendered for the script
    shown_dir     the same, said in words, for the dry-run report
    start_command the `session start` command line that produced this
    work_dir      where the session runs
    files         ((tag, path), ...) known launcher-side
    secret_names  the agent file's declared secrets: masked by name always
    """

    session_dir: str
    shown_dir: str
    start_command: str
    work_dir: str
    files: tuple
    secret_names: tuple


def record_path(session_dir: str) -> str:
    return os.path.join(session_dir, RECORD_DIR)


def ensure_record_dir(session_dir: str) -> str:
    """`<session_dir>/launch/`, 0700, with a `.gitignore` of `*`, made by
    whichever writer gets there first: `run.write_files` puts the prompt
    file, the first message and the agent plugin here before the exec, and
    the script's record follows after it. The
    session dir must exist already; this never makes one. Returns the
    path."""
    return str(ensure_private_dir(record_path(session_dir)))


def render_record_call(record: LaunchRecord, vendor_argv: str,
                       python: str | None = None) -> str:
    """The script lines that write the record: this interpreter (pinned
    launcher-side, so no PATH lookup decides it), isolated (`-I`: the
    session's working directory is never on its import path; `-c`, see
    ENTRY), then the
    rendered vendor argv after `--`, so bash expands it exactly as it
    expands the line that runs the vendor. `|| :` - a record never stops a
    launch, even one whose interpreter has vanished."""
    python = python or sys.executable
    parts = [shlex.quote(python), "-I", "-c", shlex.quote(ENTRY), "--dir", record.session_dir,
             "--start-command", shlex.quote(record.start_command),
             "--cwd", shlex.quote(record.work_dir)]
    for tag, path in record.files:
        parts += ["--file", shlex.quote(f"{tag}={path}")]
    for name in record.secret_names:
        parts += ["--secret", shlex.quote(name)]
    parts += ["--", vendor_argv]
    return "\n".join([
        "# launch record: env (secrets masked), command, startup files",
        f"# -> {record.shown_dir}/{RECORD_DIR}/ (written once, 0600, git-ignored)",
        " ".join(parts) + " || :",
    ])


# ---------- the script's half: the guard and the write ----------------------

def git_ignore_state(path: str, environ: Mapping[str, str] | None = None) -> str:
    """'ignored', 'no-repo' or 'not-ignored' for `path` (which need not
    exist yet). `git check-ignore` exits 0 for ignored, 1 for not, 128
    outside any repo. No git at all is 'no-repo': nothing can commit it."""
    env = dict(os.environ if environ is None else environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(name, None)
    try:
        result = subprocess.run(
            ["git", "-C", os.path.dirname(path), "check-ignore", "-q", "--",
             os.path.basename(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            timeout=GIT_TIMEOUT_S, check=False)
    except FileNotFoundError:
        return "no-repo"
    if result.returncode == 0:
        return "ignored"
    if result.returncode == 1:
        return "not-ignored"
    return "no-repo"


def _write_new(path: str, text: str) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def render_command(vendor_argv: list[str], start_command: str, work_dir: str) -> str:
    """Three `key: value` lines; each value is shell-quoted text that
    `shlex.split` turns back into the argv (a respawn reads `start`)."""
    return (f"vendor: {shlex.join(vendor_argv)}\n"
            f"start: {start_command}\n"
            f"cwd: {shlex.quote(work_dir)}\n")


def write_record(session_dir: str, environ: Mapping[str, str], vendor_argv: list[str],
                 start_command: str = "", work_dir: str = "",
                 files: Iterable[tuple[str, str]] = (), secret_names: Iterable[str] = (),
                 ) -> str | None:
    """Write the record into `<session_dir>/launch/` once. Returns its path,
    or None when nothing was recorded (a record is there already, or the
    guard refused). The dir itself may exist: the launcher's own files
    went in before the exec (4.6), so "written once" is the record's
    files, not the dir. Raises OSError on a failed write; `main` turns
    that into a line."""
    directory = record_path(session_dir)
    present = [name for name in RECORD_FILES
               if os.path.lexists(os.path.join(directory, name))]
    if present:
        print(f"{PROG}: {directory} has a record ({', '.join(present)}); left as it is "
              "(written once)", file=sys.stderr)
        return None
    ensure_record_dir(session_dir)
    state = git_ignore_state(os.path.join(directory, ENV_FILE), environ)
    if state == "not-ignored":
        print(f"{PROG}: NOT WRITTEN -- {directory} is inside a git repo that does not "
              "ignore it (git check-ignore), so an env dump there is one `git add` "
              "from a commit", file=sys.stderr)
        return None
    _write_new(os.path.join(directory, ENV_FILE), render_env(environ, secret_names))
    _write_new(os.path.join(directory, COMMAND_FILE),
               render_command(vendor_argv, start_command, work_dir))
    _write_new(os.path.join(directory, STARTUP_FILE), render_startup_files(files))
    return directory


def parse_record_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=MODULE, add_help=True)
    parser.add_argument("--dir", required=True, help="the session directory")
    parser.add_argument("--start-command", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--file", action="append", default=[], metavar="TAG=PATH")
    parser.add_argument("--secret", action="append", default=[], metavar="NAME")
    parser.add_argument("vendor_argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.vendor_argv and args.vendor_argv[0] == "--":
        args.vendor_argv = args.vendor_argv[1:]
    return args


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """The script's entry. Always exit 0: a missing record is a stderr
    line, never a session that did not start."""
    try:
        args = parse_record_args(list(sys.argv[1:] if argv is None else argv))
        files = []
        for item in args.file:
            tag, _sep, path = item.partition("=")
            files.append((tag, path))
        write_record(args.dir, os.environ if environ is None else environ,
                     args.vendor_argv, start_command=args.start_command,
                     work_dir=args.cwd or os.getcwd(), files=files,
                     secret_names=args.secret)
    except SystemExit:
        print(f"{PROG}: bad arguments; nothing recorded", file=sys.stderr)
    except Exception as error:  # noqa: BLE001 - a record never costs a session
        print(f"{PROG}: not written ({type(error).__name__}: {error})", file=sys.stderr)
    return 0


__all__ = [
    "LaunchRecord",
    "ensure_record_dir",
    "git_ignore_state",
    "is_secret_name",
    "looks_secret_value",
    "main",
    "mask",
    "record_path",
    "render_command",
    "render_env",
    "render_record_call",
    "render_startup_files",
    "should_mask",
    "vendor_startup_files",
    "write_record",
]


if __name__ == "__main__":
    sys.exit(main())
