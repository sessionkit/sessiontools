"""Every test runs walled off from the live machine's sessions.

Measured 2030-12-08, the night tmux placement was ported: tests
that once reached the bridge through `--in-tmux` now ran the port's own
placement, inherited the developer's `$TMUX`, and opened windows in the
LIVE tmux server - one of them, with no `AGENTS_DIR` of its own, launched
the real agent file of a running agent, keyed. So, for every test:

- a PRIVATE tmux server: `TMUX_TMPDIR` is a fresh short directory (a unix
  socket path is capped near 104 bytes on macOS, so never pytest's deep
  tmp_path), `TMUX` and `TMUX_PANE` are unset, and the server, if a test
  started one, is killed afterwards;
- an EMPTY agents folder: `AGENTS_DIR` points at a fresh directory, so a
  test that names an agent and forgets to build one finds nothing;
- an EMPTY sidecar folder: `SESSION_SIDECAR_DIR` points at a fresh
  directory, so `session start`'s front door (SESH-497, ST-46) never reads
  the live fleet's `~/.claude/sessions` - a live `scout` there would refuse
  a test's launch, and an empty one is the hermetic default;
- SESSION_STATE_DIR SET to a fresh directory, so `session list`'s snapshot
  (ST-111) is never the real one and never shared between tests, with
  SESSION_LIST_FRESH and SESSION_LIST_MAX_AGE unset;
- SESSIONS_DIR SET to a fresh directory (since task 4.1 the default root
  is the real ~/.session/sessions, and a taskmaster pane exports its own;
  either would send a test's session dirs into the live fleet's), and
  `SESSION_KEY_INIT` at a path that does not exist, so a keyed test that forgets its stub fails
  loudly instead of minting a real key. That holds in-process, in
  `plan_for`'s environ and in `clean_env`'s subprocesses; an environ a
  test builds from nothing does not carry it, and reaches the default;
- `SESSIONTOOLS_SOUND_NO_DEVICE` set, so the sound engine (ST-132) refuses
  to open the audio device: a test that forgets `--null-output`, `--script`
  or `--offline` exits 2 instead of holding the speakers;
- `SESSIONTOOLS_SOUND_NO_LAUNCHCTL` set, so `session sound engine` never runs
  a real launchctl: a test that forgets the double fails loudly instead of
  loading an agent into the real gui domain;
- `SESSIONTOOLS_PANE_STATE_NO_LAUNCHCTL` set, the same fence for
  `session pane-state agent` (ST-166);
- refusing `pass` and `gpgconf` stubs FIRST on PATH (R8 slice 4: a live
  launch that declares secrets calls `pass`, which can raise a pinentry
  and wait for a YubiKey touch; the card-error retry kills the gpg-agent).
  A PATH a test builds for itself asks `start_support.walled_bin()` for
  them, and `private_env` refuses one that reaches the host's;
- the teardown kills the private server BY ITS SOCKETS, with every TMUX*
  variable stripped: tmux follows `$TMUX` before `$TMUX_TMPDIR`, so a test
  that set one would otherwise have its teardown kill THAT server. A
  socket counts only if it resolves under the private folder and has one
  link: a hard link to another server's socket would pass the first test.

A test that needs either sets its own, as before.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime

# The private-word guards read their list at run time and skip loudly without it.
import _scanlist

# A skipped ledger token-count gate ends the run with ONE loud banner (ST-178).
import ledger_skip_support
import pytest
from start_support import WALLED_BIN, WALLED_STUBS

from sessiontools import settings
from sessiontools.account import registry as account_registry
from sessiontools.settings import SETTINGS_FILE_ENV

# Every typed test time is ANCHOR-relative (shell fixtures use ANCHOR=2030-06-09T12:34:56Z / ANCHOR_SUMMER=2030-12-08T12:34:56Z).
ANCHOR = datetime(2030, 6, 9, 12, 34, 56, tzinfo=UTC)  # Sun; mid-year
ANCHOR_SUMMER = datetime(2030, 12, 8, 12, 34, 56, tzinfo=UTC)  # Sun; year-end, the other side of any DST change


def pytest_terminal_summary(terminalreporter) -> None:
    """The loud ends of the run's legal skips: the ledger gates' banner, then the
    private-word guards' count when the private scan list is not set
    (tests/_scanlist.py). A guard that cannot run says so, never passes quietly."""
    ledger_skip_support.pytest_terminal_summary(terminalreporter)
    _scanlist.summary(terminalreporter)


@pytest.fixture(autouse=True)
def _walled_off_from_the_live_sessions(monkeypatch, tmp_path_factory):
    sockets = tempfile.mkdtemp(prefix="st-tmux-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", sockets)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("AGENTS_DIR", str(tmp_path_factory.mktemp("no-agents")))
    monkeypatch.setenv("SESSION_SIDECAR_DIR", str(tmp_path_factory.mktemp("no-sidecars")))
    # Every session dir in a test lands here: since task 4.1 the default
    # root is the REAL $HOME/.session/sessions, so the wall is SET, never
    # merely cleared (a taskmaster pane exports its own; this replaces it).
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path_factory.mktemp("no-sessions")))
    # HOME too: a helper that builds its own environ with neither falls back
    # to HOME, and the real one is where the live tree lives. 29 test dirs
    # reached the real ~/.session/sessions on 2030-12-08 that way
    # (sessionmail's conftest does the same).
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("no-home")))
    # Claude Code's config dir follows HOME only while this is unset: a
    # caller's own would point a test at a live settings.json.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("SESSION_KEY_INIT", os.path.join(sockets, "no-session-key-init"))
    # No test opens an audio device (ST-132 do-003): the sound engine's
    # device mode refuses, exit 2, while this is set. Tests drive it with
    # --null-output, --script or --script --offline, which never open one.
    monkeypatch.setenv("SESSIONTOOLS_SOUND_NO_DEVICE", "1")
    # No test runs launchctl (ST-132 do-004): `session sound engine`'s
    # `_launchctl` raises while this is set. Tests replace it with a double.
    monkeypatch.setenv("SESSIONTOOLS_SOUND_NO_LAUNCHCTL", "1")
    # The same fence for `session pane-state agent` (ST-166).
    monkeypatch.setenv("SESSIONTOOLS_PANE_STATE_NO_LAUNCHCTL", "1")
    # And for `session path migrate`'s agent probe (ST-266 do-004).
    monkeypatch.setenv("SESSIONTOOLS_MIGRATE_NO_PROBE", "1")
    # `session list`'s snapshot (ST-111) lands in a fresh state dir per test,
    # SET rather than left to HOME, and no test inherits a caller's knobs.
    monkeypatch.setenv("SESSION_STATE_DIR", str(tmp_path_factory.mktemp("state")))
    monkeypatch.delenv("SESSION_LIST_FRESH", raising=False)
    monkeypatch.delenv("SESSION_LIST_MAX_AGE", raising=False)
    # A tool's settings-file variable moves where its schema is read from
    # (ST-147): a caller's own would point a test at the live one.
    # Every name a tool reads, the old ones included.
    for names in SETTINGS_FILE_ENV.values():
        for variable in names:
            monkeypatch.delenv(variable, raising=False)
    # The stranded line is said once per tool per PROCESS, and every test
    # shares this one: each test starts with none said.
    monkeypatch.setattr(settings, "_said_stranded", set())
    # The same for the stranded accounts (ST-212), said once per process.
    monkeypatch.setattr(account_registry, "_said_stranded", False)
    # No test reaches the host's `pass` or `gpgconf`: a real `pass show` can
    # raise a pinentry and wait for a YubiKey touch, and the card-error retry
    # runs `gpgconf --kill gpg-agent scdaemon` (R8 slice 4). A test that
    # wants a backend puts its own stub ahead of these.
    walled = os.path.join(sockets, WALLED_BIN)
    os.mkdir(walled)
    for name in WALLED_STUBS:
        stub = os.path.join(walled, name)
        with open(stub, "w") as handle:
            handle.write(f"#!/bin/sh\necho '{name} is walled off in tests' >&2\nexit 97\n")
        os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", walled + os.pathsep + os.environ.get("PATH", ""))
    yield
    kill_private_servers(sockets)
    shutil.rmtree(sockets, ignore_errors=True)


def kill_private_servers(sockets: str) -> None:
    """Kill every tmux server whose socket is under `sockets`, named by `-S`,
    in an environment with no TMUX* at all: nothing a test left in
    os.environ can point the kill anywhere else."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("TMUX")}
    for directory, _dirs, names in os.walk(sockets):
        for name in names:
            socket = os.path.join(directory, name)
            if is_private(socket, sockets, env):
                subprocess.run(["tmux", "-S", socket, "kill-server"], capture_output=True,
                               check=False, env=env)


def is_private(socket: str, sockets: str, env: dict | None = None) -> bool:
    """True if the server behind `socket` is this wall's own: asked for its
    own `#{socket_path}`, it names a path under `sockets`.

    The SERVER is asked, not the file, because a file can be a link. A
    symlink or a hard link in the wall to another server's socket reaches
    THAT server, which names its own path, outside. A hard link made outside
    the wall to OUR socket changes nothing our server says, so it is still
    killed. (Review of 71d755f, lows 1 and 2: the
    lstat link count let a symlink to an in-wall hard link through, and
    skipped, so leaked, our own server once linked from outside.)

    No answer - the server exited, the file is no socket, the OS refused,
    the query hung - means nobody's to kill. Nothing here raises past the
    teardown, which still has the folder to delete (low 3)."""
    try:
        asked = subprocess.run(["tmux", "-S", socket, "display-message", "-p", "#{socket_path}"],
                               capture_output=True, text=True, check=False, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    own = asked.stdout.strip()
    return (asked.returncode == 0 and own != ""
            and os.path.realpath(own).startswith(os.path.realpath(sockets) + os.sep))
