"""ST-143: a seat's credentials must not travel down to the seats it launches.

The module's rule (`environment.py` header): nothing reaches an agent's
environment that its own file did not declare. Three places broke it, and
`findings/rca.md` names them: `host_environ()` copies the launcher's whole
environment; `ensure_session()` can BIRTH a tmux server from it, and a server
keeps its starter's environment for every pane it ever makes; the launch
script's first line unsets identity names only.

These began as the reproduction (repro-001, `xfail(strict=True)`), and pass
since fix-001: `host_environ` drops CREDENTIAL_VARS, so the in-place exec and
the server birth hold none, and the script's first line unsets them. The
opt-in that carries one on purpose, `environment: inherit:`, is tested in
`test_start_inherit.py`.

FAKE values only, set by `monkeypatch`. Nothing here reads, prints or asserts
on a real token.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from start_support import plan_for

from sessiontools import tmux as _tmux
from sessiontools.start import run
from sessiontools.start.environment import (
    CREDENTIAL_VARS,
    host_environ,
    render_identity_unset,
)
from sessiontools.start.placement import ensure_session

#: A FAKE value for each name in the credential set (rca.md D1).
FAKES = {
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-FAKE-ST143",
    "ANTHROPIC_API_KEY": "sk-ant-api03-FAKE-ST143",
    "ANTHROPIC_AUTH_TOKEN": "FAKE-ST143-auth-token",
}
CREDENTIALS = {name: FAKES[name] for name in CREDENTIAL_VARS}


def test_the_credential_set_is_the_one_rca_named():
    """D1: at least the two SK-80 named, plus ST-22's ANTHROPIC_AUTH_TOKEN.
    A name added later needs a FAKE here, or this file cannot test it."""
    assert set(CREDENTIAL_VARS) == set(FAKES)


def fake_credentials(monkeypatch) -> None:
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)


# ---------- (a) host_environ: the source every child starts from -------------

@pytest.mark.parametrize("name", sorted(CREDENTIALS))
def test_host_environ_drops_a_credential(name):
    env = host_environ({name: CREDENTIALS[name], "PATH": "/usr/bin"})
    assert name not in env
    assert env["PATH"] == "/usr/bin"          # an ordinary name still passes


# ---------- (b) the server birth: the leak that outlives the launch ----------

def _pane_sees(socket_dir: str, client_env: dict, name: str) -> str:
    """Open a window through a token-less client on the server `ensure_session`
    born, and read what the pane's shell holds for NAME (`<unset>` if absent)."""
    out = Path(socket_dir) / f"seen-{name}"
    command = f'printf %s "${{{name}-<unset>}}" > {out}'
    result = _tmux.run(["new-window", "-t", "st143:", command], env=client_env)
    assert result.returncode == 0, result.stderr
    for _ in range(100):
        if out.exists() and out.stat().st_size:
            return out.read_text()
        time.sleep(0.05)
    pytest.fail("the pane never wrote what it held")


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
@pytest.mark.parametrize("name", sorted(CREDENTIALS))
def test_a_server_born_by_ensure_session_hands_no_credential_to_a_later_pane(
        monkeypatch, name):
    """SK-80 investigate-003 probe B, as a test: the launcher holds the fake
    credential, `ensure_session` births the server, and a LATER window opened
    by a client that holds no credential must not find it in its pane."""
    # a short private dir: a unix socket path is capped near 104 bytes
    private = tempfile.mkdtemp(prefix="st143-")
    try:
        monkeypatch.setenv("TMUX_TMPDIR", private)
        for var in ("TMUX", "TMUX_PANE"):
            monkeypatch.delenv(var, raising=False)
        launcher = {"PATH": os.environ["PATH"], "TMUX_TMPDIR": private,
                    name: CREDENTIALS[name]}
        assert ensure_session("st143", launcher) is True
        client = {"PATH": os.environ["PATH"], "TMUX_TMPDIR": private}
        assert name not in client
        assert _pane_sees(private, client, name) == "<unset>"
    finally:
        _tmux.run(["kill-server"], env={"PATH": os.environ["PATH"],
                                        "TMUX_TMPDIR": private})
        shutil.rmtree(private, ignore_errors=True)


# ---------- (c) the launch script's first line --------------------------------

@pytest.mark.parametrize("name", sorted(CREDENTIALS))
def test_the_launch_scripts_first_line_unsets_a_credential(tmp_path, name):
    """A pane on a server sessiontools did not birth (a human's shell, `tm kick`)
    holds whatever that server holds; only the script's own first `unset` can
    take it out before the vendor runs."""
    first = render_identity_unset().split()
    assert first[0] == "unset"
    assert name in first
    plan = plan_for(tmp_path, "claude:\n  effort: high\n")
    unset_line = next(line for line in plan.script.splitlines()
                      if line.startswith("unset "))
    assert name in unset_line.split()


# ---------- (d) the in-place exec ---------------------------------------------

class Exec(Exception):
    pass


def test_the_in_place_exec_env_carries_no_credential(tmp_path, monkeypatch):
    """`run.execute` execs the script with `host_environ()` of the CALLER."""
    fake_credentials(monkeypatch)
    plan = plan_for(tmp_path, "claude:\n  effort: high\n")
    seen = []

    def fake_exec(file, argv, env):
        seen.append(dict(env))
        raise Exec

    monkeypatch.setattr(os, "execvpe", fake_exec)
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)
    with pytest.raises(Exec):
        run.execute(plan)
    leaked = sorted(name for name in CREDENTIALS if name in seen[0])
    assert leaked == []
