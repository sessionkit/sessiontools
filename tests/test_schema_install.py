"""The schema install before the birth: the launch script installs
sessiontools' own schemas (the `session` schema, ST-78 do-002) and runs a
tool's own install command (`pager settings install`) before the birth reads
the installed schemas, so the first session on a fresh HOME reads its
variables. Each scenario runs the RENDERED lines under bash with a stub
`pager` on a PATH that holds nothing else of that name."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from settings_support import host

from sessiontools import settings, settle_settings
from sessiontools.start import plan as pl

SID = "11111111-2222-3333-4444-555555555555"
BASH = shutil.which("bash")

MAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "subscriptions": {"type": "object", "additionalProperties": {"type": "boolean"},
                          "x-env": "SESSIONMAIL_SUBSCRIBE"},
    },
}

#: mail's settings directory relative to HOME (settings.host_dir), where a
#: pager that installs its schema writes it.
MAIL_REL = host(Path("/home-of-test"), "mail").relative_to("/home-of-test")
#: A pager WITH the verb: installs the schema, says so on stdout, exit 0.
INSTALLS = f"""#!/bin/sh
mkdir -p "$HOME/{MAIL_REL}"
cat > "$HOME/{MAIL_REL}/mail.schema.json" <<'EOF'
{json.dumps(MAIL_SCHEMA)}
EOF
echo "pager settings install: installed $HOME/{MAIL_REL}/mail.schema.json"
"""
#: A pager WITHOUT the verb: the usage error a release before it gives.
NO_VERB = """#!/bin/sh
echo "Usage: pager settings migrate [--dry-run]" >&2
echo "Try 'pager settings --help' for help." >&2
echo "" >&2
echo "Error: No such command 'install'." >&2
exit 2
"""
CURRENT = f"""#!/bin/sh
echo "pager settings install: mail.schema.json and mail.template.json already current in $HOME/{MAIL_REL}"
"""
CANNOT_WRITE = f"""#!/bin/sh
echo "pager settings install: could not write $HOME/{MAIL_REL}/mail.schema.json" >&2
exit 2
"""


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def bin_dir(tmp_path) -> Path:
    """A PATH of its own: the few tools the stubs use, and no `pager`
    unless a test writes one (a host's /usr/bin/pager is not in it)."""
    d = tmp_path / "bin"
    d.mkdir()
    for tool in ("mkdir", "cat", "sleep"):
        (d / tool).symlink_to(shutil.which(tool))
    return d


def sdir(home: Path) -> Path:
    return home / ".session" / "sessions" / SID


def stub(bin_dir: Path, text: str, name: str = "pager") -> Path:
    p = bin_dir / name
    p.write_text(text, encoding="utf-8")
    p.chmod(0o755)
    return p


def log_lines(home: Path) -> list[dict]:
    f = sdir(home) / "log" / settings.SETTINGS_LOG
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def events(home: Path) -> list[str]:
    return [x["event"] for x in log_lines(home)]


def commands(home: Path) -> list[dict]:
    """The log's entries for the command (`pager settings install`), not
    sessiontools' own schema install."""
    return [x for x in log_lines(home) if x["event"] == "schema-install"]


OWN = settings.OWN_SCHEMA_EVENT


def own_schema(home: Path) -> Path:
    return host(home, "session") / ".session.schema.json"


def launch(home: Path, bin_dir: Path, **env) -> subprocess.CompletedProcess:
    """The install and birth lines exactly as `session start` renders them,
    then a marker standing in for the vendor: the launch carried on."""
    record = SimpleNamespace(session_dir=shlex.quote(str(sdir(home))))
    script = pl.render_birth_call(record, python=sys.executable) + "echo LAUNCHED\n"
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=60,
                          env={"HOME": str(home), "PATH": str(bin_dir), **env}, check=False)


# --- SC2: a pager with the verb, a fresh HOME ------------------------------------

def test_a_fresh_homes_first_session_reads_its_variables_and_says_nothing(home, bin_dir):
    stub(bin_dir, INSTALLS)
    done = launch(home, bin_dir, SESSIONMAIL_SUBSCRIBE="lead+example")
    assert done.stdout == "LAUNCHED\n" and done.stderr == ""
    env_file = sdir(home) / "settings" / "mail.d" / "50-env.json"
    assert json.loads(env_file.read_text()) == {"subscriptions": {"lead+example": True}}
    assert "birth-no-schema" not in events(home)
    assert events(home)[0] == OWN       # sessiontools' own first, then the command
    (install,) = commands(home)
    assert install["event"] == "schema-install" and install["exit"] == 0
    assert install["command"] == "pager settings install"
    assert install["path"] == str(bin_dir / "pager")
    installed = f"pager settings install: installed {host(home, 'mail')}/mail.schema.json"
    assert install["out"] == [installed]


# --- SC3: no pager, an old pager, quiet --------------------------------------------

def test_no_pager_on_path_skips_its_command_and_still_installs_sessiontools_own(home, bin_dir):
    done = launch(home, bin_dir, SESSIONMAIL_SUBSCRIBE="lead+example")
    assert done.stdout == "LAUNCHED\n" and done.stderr == ""
    assert events(home) == [OWN]        # no mail schema, and no settle variable set
    assert own_schema(home).is_file()
    assert not (sdir(home) / "settings").exists()


def test_a_pager_without_the_verb_is_logged_and_never_reaches_the_terminal(home, bin_dir):
    stub(bin_dir, NO_VERB)
    done = launch(home, bin_dir, SESSIONMAIL_SUBSCRIBE="lead+example")
    assert done.stdout == "LAUNCHED\n" and done.stderr == ""
    assert events(home) == [OWN, "schema-install"]
    (install,) = commands(home)
    assert install["exit"] == 2
    assert "Error: No such command 'install'." in install["err"]
    assert not (sdir(home) / "settings").exists()


def test_files_already_current_say_nothing(home, bin_dir):
    stub(bin_dir, CURRENT)
    w = host(home, "mail") / "mail.schema.json"
    w.parent.mkdir(parents=True)
    w.write_text(json.dumps(MAIL_SCHEMA))
    done = launch(home, bin_dir, SESSIONMAIL_SUBSCRIBE="lead+example")
    assert done.stdout == "LAUNCHED\n" and done.stderr == ""
    assert events(home) == [OWN, "schema-install", "env-at-birth"]


def test_a_file_not_written_is_one_line_naming_the_log_and_the_launch_carries_on(home, bin_dir):
    stub(bin_dir, CANNOT_WRITE)
    done = launch(home, bin_dir)
    assert done.stdout == "LAUNCHED\n"
    log = sdir(home) / "log" / settings.SETTINGS_LOG
    line = (f"{settings.SCHEMA_INSTALL_PROG}: pager settings install exit 2, could not write "
            f"{host(home, 'mail')}/mail.schema.json; recorded in {log}")
    assert done.stderr.splitlines() == [line]
    assert events(home) == [OWN, "schema-install"]


#: The timeouts a hang is given in turn, doubling. The first run whose stub
#: got as far as recording its child carries the proof; unloaded, that is
#: the first. A loaded host can kill the stub before it writes the pid.
HANG_TIMEOUTS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
#: How long the hang's child sleeps, and how long the test waits for it to
#: be gone. The sleep outlasts every run and the wait together, so a child
#: the kill missed can never pass by ending on its own.
HANG_SLEEP = 300
GONE_WITHIN = 30


def recorded_pid(pid_file: Path) -> int | None:
    """The child pid the stub wrote, or None when it was killed first: no
    file yet, or a file opened and not yet written."""
    try:
        text = pid_file.read_text()
    except FileNotFoundError:
        return None
    return int(text) if text.endswith("\n") and text.strip().isdigit() else None


def test_a_command_that_hangs_is_killed_with_what_it_started(home, bin_dir):
    pid_file = home / "child.pid"
    stub(bin_dir, f'#!/bin/sh\nsleep {HANG_SLEEP} &\n'
                  f'echo $! > {shlex.quote(str(pid_file))}\nwait\n')
    for attempt, timeout in enumerate(HANG_TIMEOUTS, 1):
        pid_file.unlink(missing_ok=True)
        started = time.monotonic()
        assert settings.schema_install_main(
            ["--dir", str(sdir(home)), "--", "pager", "settings", "install"],
            environ={"HOME": str(home), "PATH": str(bin_dir)}, timeout=timeout) == 0
        assert time.monotonic() - started < timeout + 10    # killed, not run out
        entries = commands(home)
        assert len(entries) == attempt
        assert entries[-1]["exit"] is None and entries[-1]["timeout"] == timeout
        child = recorded_pid(pid_file)
        if child is not None:
            break
    else:
        pytest.fail(f"the stub never recorded its child, last timeout {HANG_TIMEOUTS[-1]} s")
    deadline = time.monotonic() + GONE_WITHIN   # killed with its group; reaped by init
    while True:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        if time.monotonic() > deadline:
            pytest.fail(f"the command's child {child} outlived the timeout")
        time.sleep(0.05)


# --- the entry -------------------------------------------------------------------

def test_bad_arguments_run_nothing_and_exit_0(home, capsys):
    assert settings.schema_install_main([], environ={"HOME": str(home)}) == 0
    assert settings.schema_install_main(["--dir", str(sdir(home)), "pager"],
                                        environ={"HOME": str(home)}) == 0
    assert "usage" in capsys.readouterr().err
    assert log_lines(home) == []


def test_a_bare_dir_installs_sessiontools_own_and_runs_no_command(home, bin_dir, capsys):
    stub(bin_dir, INSTALLS)
    assert settings.schema_install_main(["--dir", str(sdir(home))],
                                        environ={"HOME": str(home), "PATH": str(bin_dir)}) == 0
    assert events(home) == [OWN]
    assert not host(home, "mail").exists()
    assert capsys.readouterr() == ("", "")


def test_a_command_not_on_path_is_logged_not_raised(home, bin_dir, capsys):
    assert settings.schema_install_main(["--dir", str(sdir(home)), "--", "pager", "x"],
                                        environ={"HOME": str(home), "PATH": str(bin_dir)}) == 0
    (install,) = commands(home)
    assert install["error"] == "pager: not found on PATH"
    assert capsys.readouterr() == ("", "")


def test_a_command_that_cannot_start_is_logged_not_raised(home, bin_dir, capsys):
    stub(bin_dir, "#!/nonexistent/interpreter\n")
    assert settings.schema_install_main(["--dir", str(sdir(home)), "--", "pager", "x"],
                                        environ={"HOME": str(home), "PATH": str(bin_dir)}) == 0
    (install,) = commands(home)
    assert install["error"].startswith(("FileNotFoundError", "OSError", "PermissionError"))
    assert capsys.readouterr() == ("", "")


def test_the_logged_output_is_capped_and_says_so(home, bin_dir):
    keep, chars = settings.SCHEMA_INSTALL_KEEP_LINES, settings.SCHEMA_INSTALL_KEEP_CHARS
    long_line = "x" * (chars + 10)
    stub(bin_dir, f'#!/bin/sh\necho {long_line}\ni=0\nwhile [ $i -lt 50 ]; do echo line $i; '
                  f'i=$((i+1)); done\necho short >&2\n')
    settings.schema_install_main(["--dir", str(sdir(home)), "--", "pager"],
                                 environ={"HOME": str(home), "PATH": str(bin_dir)})
    (install,) = commands(home)
    assert len(install["out"]) == keep and install["out"][0] == "x" * chars
    assert install["err"] == ["short"]
    assert install["truncated"] == ["out"]


# --- ST-78 do-002: sessiontools' own schema, the `session` one (design note S2) ----

def test_the_own_schema_is_the_rendered_one_with_the_settle_variables(home, bin_dir):
    launch(home, bin_dir)
    assert json.loads(own_schema(home).read_text()) == settle_settings.schema()
    env = {"HOME": str(home)}
    assert settings.find_schema("session", env)[0] == own_schema(home)
    named = {k.variable: ".".join(k.path) for k in settings.env_keys("session", env)}
    assert named == {"SESSION_SETTLE_AT": "settle.soft.at", "SESSION_SETTLE_SAY": "settle.soft.say"}


@pytest.mark.parametrize("at", ["250k", "250000"])
def test_settle_at_at_birth_lands_in_the_session_layer_as_settle_soft_at(home, bin_dir, at):
    """A fresh HOME, no pager: the launch installs the session schema, then the
    birth copies SESSION_SETTLE_AT, as written, into session.d/50-env.json."""
    done = launch(home, bin_dir, SESSION_SETTLE_AT=at, SESSION_SETTLE_SAY="past {at}")
    assert done.stdout == "LAUNCHED\n" and done.stderr == ""
    env_file = sdir(home) / "settings" / "session.d" / "50-env.json"
    assert json.loads(env_file.read_text()) == {
        "settle": {"soft": {"at": at, "say": "past {at}"}}}
    assert [(x["variable"], x["key"]) for x in log_lines(home) if x["event"] == "env-at-birth"] \
        == [("SESSION_SETTLE_AT", "settle.soft.at"), ("SESSION_SETTLE_SAY", "settle.soft.say")]
    got = settle_settings.read(SID, {"HOME": str(home)})
    assert got == settle_settings.Settle(settle_settings.Soft(250_000, None, "past {at}"), None)


def test_a_second_launch_leaves_a_current_schema_untouched(home, bin_dir):
    launch(home, bin_dir)
    before = own_schema(home).stat()
    launch(home, bin_dir)
    after = own_schema(home).stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert [x["state"] for x in log_lines(home) if x["event"] == OWN] == ["installed", "current"]
    assert stat_mode(own_schema(home)) == 0o644


def test_a_stale_schema_is_replaced(home, bin_dir):
    own_schema(home).parent.mkdir(parents=True)
    own_schema(home).write_text('{"title": "old"}')
    launch(home, bin_dir)
    assert json.loads(own_schema(home).read_text()) == settle_settings.schema()
    assert [p.name for p in own_schema(home).parent.iterdir()] == [".session.schema.json"]


def test_an_own_schema_not_written_is_one_line_and_the_launch_carries_on(home, bin_dir):
    blocker = home / ".sessionkit" / "tools"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a folder")
    done = launch(home, bin_dir, SESSION_SETTLE_AT="250k")
    assert done.stdout == "LAUNCHED\n"
    (line,) = done.stderr.splitlines()
    assert line.startswith(f"{settings.SCHEMA_INSTALL_PROG}: session schema not written to "
                           f"{own_schema(home)} (")
    assert line.endswith(f"recorded in {sdir(home) / 'log' / settings.SETTINGS_LOG}")
    (own,) = [x for x in log_lines(home) if x["event"] == OWN]
    assert own["state"] == "failed"
    assert not (sdir(home) / "settings" / "session.d").exists()   # no schema: no copy


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
