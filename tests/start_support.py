"""Shared helpers for the `session start` tests - ported from the old suite
(session-20300729 `skills/session/scripts/test_session_start.py`), which
drove the launcher in-process and through a subprocess with a stub vendor.

NEVER call `main()` in-process WITHOUT --dry-run and with a real agent: the
direct path execs, and an in-process exec replaces the TEST RUNNER with a
live vendor (measured in the old suite: three pytest runs exec'd into
claude). Live launches go through `launch_e2e`, a subprocess with a stub
`claude` on PATH, where an exec can only replace the child.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools.start.args import parse_args
from sessiontools.start.environment import is_session_identity
from sessiontools.start.plan import prepare

ROOT = Path(__file__).resolve().parents[1]

#: conftest's refusing stubs: a real `pass show` can raise a pinentry and
#: wait for a YubiKey touch, and the card-error retry runs `gpgconf --kill
#: gpg-agent scdaemon`. A real `passage show` (ST-159) runs `age`, which can
#: ask for Touch ID or a key touch. They live in `$TMUX_TMPDIR/walled-bin`.
WALLED_BIN = "walled-bin"
WALLED_STUBS = ("pass", "gpgconf", "passage", "age")
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TIMEOUT_S = 120      # a bound, and an assertion: a launcher must never hang here


def quiet(_entry: str) -> None:
    """An `announce` that says nothing (the stderr line is tested on its own)."""


def make_agents_root(tmp_path: Path, name: str = "scout", front_matter: str = "",
                     body: str = "You are.\n", layout: str = "file") -> str:
    """A disposable agents dir holding one agent, either layout."""
    root = tmp_path / "agents"
    root.mkdir(exist_ok=True)
    text = f"---\n{front_matter}---\n{body}" if front_matter else body
    if layout == "dir":
        (root / name).mkdir()
        (root / name / f"{name}.md").write_text(text)
    else:
        (root / f"{name}.md").write_text(text)
    return str(root)


def plan_for(tmp_path: Path, front_matter: str, argv_tail=(), work: str = "work",
             extra_env: dict | None = None, flags=()):
    """A LaunchPlan for one disposable agent. `flags` go BEFORE the agent
    name, `argv_tail` after it - the CA-60 rule, kept visible so a test
    cannot assert on a passthrough by accident. A live (non --dry-run) plan
    mints, exactly as the old prepare_launch did; it writes no file. The
    environ carries the conftest's dead SESSION_KEY_INIT, so a keyed plan
    that forgets its stub refuses instead of minting through the real
    primitive."""
    root = make_agents_root(tmp_path, front_matter=front_matter)
    work_dir = tmp_path / work
    work_dir.mkdir(exist_ok=True)
    environ = {"AGENTS_DIR": root, "PATH": os.environ.get("PATH", ""),
               # the test's own sessions root, beside its agents (task 4.1
               # moved the default to the real $HOME/.session/sessions)
               "SESSIONS_DIR": os.path.join(root, "sessions"),
               "SESSION_KEY_INIT": os.environ["SESSION_KEY_INIT"]}
    if extra_env:
        environ.update(extra_env)
    args = parse_args([*flags, "scout", *argv_tail])
    return prepare(args, environ=environ, work_dir=str(work_dir))


# ---------- the verb as a subprocess -----------------------------------------

def session_cmd(*args: str) -> list[str]:
    """`session start ...` through this checkout's own cli, no install needed."""
    return [sys.executable, "-m", "sessiontools.cli", "start", *args]


def walled_bin() -> str:
    """This test's walled-bin. A test that builds its own PATH puts it
    ahead of every system directory (its own stubs may go first)."""
    return os.path.join(os.environ["TMUX_TMPDIR"], WALLED_BIN)


def refuse_unwalled_path(path: str) -> None:
    """Fail the test, before anything starts, if `path` finds a `pass` or
    `gpgconf` outside the wall. `os.environ`'s PATH has the wall first; a
    PATH a test builds from scratch does not, unless it asks for it.
    Real paths on both sides: `<wall>/../../opt/homebrew/bin` is not the
    wall, and `/private/tmp/...` (macOS's /tmp) is."""
    wall = os.path.realpath(walled_bin()) + os.sep
    for name in WALLED_STUBS:
        found = shutil.which(name, path=path)
        if found and not os.path.realpath(found).startswith(wall):
            pytest.fail(f"PATH reaches the host's {found}, not the walled stub: "
                        "refusing to start anything")


def clean_env(**extra: str) -> dict:
    """The test process's environment with the parts a launch must not see:
    no tmux (a live launch would rename the window the tests run in), no
    caller identity, no agent committer. SESSION_KEY_INIT is KEPT: it is the
    conftest's path that does not exist, so a keyed subprocess that forgets
    its stub refuses. Popped, the child fell back to the REAL primitive,
    which commits births to the agent registry (review of the slice-2
    LOWs)."""
    env = dict(os.environ)
    # The caller's session identity comes off by the ONE definition, not a
    # hand list: the hand list missed CLAUDE_CODE_SESSION_ID, so every launch
    # test run inside a seat inherited the seat's real id (ST-113 do-002),
    # and the tuple alone left a keyed seat's GIT_CONFIG_* signing block in
    # every launch test run there (ST-118).
    for name in [n for n in env if is_session_identity(n)]:
        env.pop(name)
    for noise in ("TMUX", "TMUX_PANE",
                  "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "AGENT_ALLOW_FABLE"):
        env.pop(noise, None)
    for name in [n for n in env if n.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
                 and "MODEL" in n]:
        env.pop(name)
    env.update(extra)
    return env


# NUL-separated argv record: a first message legitimately CONTAINS newlines.
VENDOR_STUB = """#!/usr/bin/env bash
printf '%%s\\0' "$@"      > "%(dir)s/argv"
printf '%%s'   "$CLAUDE_SESSION_ID" > "%(dir)s/session-id-env"
printf '%%s'   "$PWD"     > "%(dir)s/cwd"
printf '%%s'   "${VIRTUAL_ENV:-}"   > "%(dir)s/virtual-env"
printf '%%s'   "${CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION:-}" > "%(dir)s/suggestion-env"
"""


def recorded_argv(record: Path) -> list[str]:
    return (record / "argv").read_text().split("\0")[:-1]


#: what `without_known_paths` writes in place of each path it neutralises
KNOWN_PATH = "<known-path>"


def without_known_paths(text: str, tmp_path: Path) -> str:
    """`text` with each path PREFIX the run did not mint swapped for
    KNOWN_PATH: the interpreter (`sys.executable`, as `session_cmd` puts it
    in the report), its venv, this checkout, `tmp_path` and $HOME, each in
    its literal and resolved form (macOS reports /var as /private/var).

    Why: a dry-run report is full of paths, and any of them can sit under a
    uuid-named directory (a Claude scratchpad is `.../<session-uuid>/...`), so
    a "no uuid anywhere" assertion failed on WHERE the suite ran (ST-110). Only
    the prefix goes: whatever a launch appends after it (`sessions/<id>`) is
    still in the text, so a minted id in a path is still caught. Longest
    first, so a checkout holding its own venv is replaced whole."""
    known = {sys.executable, sys.prefix, str(ROOT), str(tmp_path),
             os.path.expanduser("~")}
    known |= {os.path.realpath(path) for path in known}
    for path in sorted((p for p in known if len(p) > 1), key=len, reverse=True):
        text = text.replace(path, KNOWN_PATH)
    return text


def launch_e2e(tmp_path: Path, *verb_args: str,
               front_matter: str = "claude:\n  effort: high\n",
               extra_env: dict | None = None, stub_body: str = VENDOR_STUB):
    """Run the verb as a real subprocess against a stub `claude` on PATH.
    Returns (result, record_dir, work_dir)."""
    record = tmp_path / "record"
    record.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(stub_body % {"dir": record})
    stub.chmod(0o755)
    root = make_agents_root(tmp_path, front_matter=front_matter)
    work = tmp_path / "work"
    work.mkdir()
    env = clean_env(AGENTS_DIR=root, SESSIONS_DIR=os.path.join(root, "sessions"),
                    PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(session_cmd(*verb_args), capture_output=True, text=True,
                            timeout=TIMEOUT_S, cwd=str(work), env=env, check=False)
    return result, record, work


# ---------- session-key-init stand-in ----------------------------------------
# It proves OUR side of the CA-83 subprocess contract: the arguments we pass,
# the env block we parse. The primitive's own behaviour has its own suite.

KEY_INIT_STUB = """#!/usr/bin/env bash
echo "$@" > "%(record)s"
env > "%(record)s.env"
while [ $# -gt 0 ]; do
  case "$1" in
    --session-dir) dir="$2"; shift 2 ;;
    *) shift ;;
  esac
done
sid="%(sid)s"
mkdir -p "$dir/$sid"
touch "$dir/$sid/session.pub"
printf 'CLAUDE_SESSION_ID=%%s\\n' "$sid"
printf 'CLAUDE_SESSION_AGENT_SOCK=/tmp/stub-agent.sock\\n'
printf 'CLAUDE_SESSION_PUB=%%s/%%s/session.pub\\n' "$dir" "$sid"
"""

#: The child's OWN signing block, as the real primitive prints it
#: (session-key-init:305-317), keyed to the stub's made-up pub.
STUB_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIChildChildChildChildChildChildChildChild child"
KEY_INIT_SIGNING_BLOCK = """printf 'GIT_CONFIG_COUNT=6\\n'
printf 'GIT_CONFIG_KEY_0=gpg.format\\nGIT_CONFIG_VALUE_0=ssh\\n'
printf 'GIT_CONFIG_KEY_1=user.signingkey\\nGIT_CONFIG_VALUE_1=key::%(pub)s\\n'
printf 'GIT_CONFIG_KEY_2=commit.gpgsign\\nGIT_CONFIG_VALUE_2=true\\n'
printf 'GIT_CONFIG_KEY_3=tag.gpgsign\\nGIT_CONFIG_VALUE_3=true\\n'
printf 'GIT_CONFIG_KEY_4=gpg.ssh.program\\nGIT_CONFIG_VALUE_4=/child/ssh-keygen-session\\n'
printf 'GIT_CONFIG_KEY_5=gpg.ssh.allowedSignersFile\\nGIT_CONFIG_VALUE_5=/child/allowed_signers\\n'
"""

STUB_SID = "deadbeef00000000"


def write_key_init_stub(tmp_path: Path, exit_code: int = 0,
                        signing_block: bool = False) -> tuple[str, str]:
    """The stand-in primitive: records its argv at the returned path and the
    env it runs with at `<path>.env` (ST-118: the caller's identity, signing
    block included, must not reach it); with `signing_block`, prints the child's own git
    signing block after the handles, as the real one does."""
    record = tmp_path / "key-init-argv"
    stub = tmp_path / "session-key-init-stub"
    body = KEY_INIT_STUB % {"record": record, "sid": STUB_SID}
    if signing_block:
        body += KEY_INIT_SIGNING_BLOCK % {"pub": STUB_PUB}
    if exit_code:
        body += f"exit {exit_code}\n"
    stub.write_text(body)
    stub.chmod(0o755)
    return str(stub), str(record)
