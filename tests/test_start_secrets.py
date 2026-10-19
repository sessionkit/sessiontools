"""Declared secrets at launch (R8 slice 4): the `pass` backend, the report,
the 0600 file, and the launch they belong to. Ported from the old suite
(`test_session_start.py` 1146-1372, 2958-3000, 4609), plus the end-to-end
run the old one never had: the vendor sees the value, argv and the script
never do, and the file is gone when it runs.

`pass` and `gpgconf` are ALWAYS stubs on PATH here. The real `pass show`
can raise a pinentry and wait for a YubiKey touch, and the card-error retry
runs `gpgconf --kill gpg-agent scdaemon`, which would kill the host's real
agent.
"""

from __future__ import annotations

import glob
import os
import stat
import subprocess

import pytest
from start_support import TIMEOUT_S, clean_env, make_agents_root, plan_for, session_cmd

from sessiontools import start
from sessiontools.start import context, run, secrets
from sessiontools.start.environment import DeclaredSecret
from sessiontools.start.errors import LaunchError

VALUE = "s3cr3t-'value' $HOME"      # quotes and a $ the shell must not expand
SECRETS = "environment:\n  secrets:\n    DEMO_TOKEN: pass:demo/token\n"
DECLARED = [DeclaredSecret("DEMO_TOKEN", "pass", "demo/token")]

# The stub `pass`: logs each call, then acts on the first line of $PASS_MODE
# (or of the file it names, for a sequence): ok, multi, card, fail, empty.
PASS_STUB = """#!/usr/bin/env bash
echo "pass $*" >> "%(log)s"
n=$(grep -c '^pass ' "%(log)s")
mode=$(sed -n "${n}p" "%(modes)s")
[ -n "$mode" ] || mode=$(tail -1 "%(modes)s")
case "$mode" in
  ok)    printf '%%s\\n' "%(value)s" ;;
  multi) printf '%%s\\nnotes: second line\\n' "%(value)s" ;;
  card)  echo "gpg: decryption failed: Card error" >&2; exit 2 ;;
  fail)  echo "Error: demo/token is not in the password store." >&2; exit 1 ;;
  empty) printf '\\n' ;;
esac
"""
GPGCONF_STUB = '#!/usr/bin/env bash\necho "gpgconf $*" >> "%(log)s"\n'


@pytest.fixture
def fake_pass(tmp_path, monkeypatch):
    """Stubs on PATH; returns (set_modes, calls)."""
    bin_dir, log, modes = tmp_path / "stub-bin", tmp_path / "calls", tmp_path / "modes"
    bin_dir.mkdir()
    log.write_text("")
    for name, body in (("pass", PASS_STUB), ("gpgconf", GPGCONF_STUB)):
        path = bin_dir / name
        path.write_text(body % {"log": log, "modes": modes, "value": VALUE.replace('"', '\\"')
                                .replace("$", "\\$")})
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def set_modes(*sequence):
        modes.write_text("\n".join(sequence) + "\n")

    set_modes("ok")
    return set_modes, lambda: log.read_text().splitlines()


# ---------- the pass backend --------------------------------------------------------------

def test_pass_resolves_through_the_pass_command_and_takes_the_first_line(fake_pass):
    set_modes, calls = fake_pass
    set_modes("multi")
    assert secrets.pass_backend("demo/token") == VALUE
    assert calls() == ["pass show demo/token"]


def test_pass_announces_the_pin_and_touch_wait_before_it_waits(fake_pass, capsys):
    secrets.pass_backend("demo/token")
    assert "pass:demo/token -- gpg may raise a PIN dialog and/or wait for a " \
           "security-key touch" in capsys.readouterr().err


def test_the_timeout_leaves_a_human_time_for_the_pin_and_the_touch():
    assert secrets.SECRET_TIMEOUT_S >= 60


def test_a_card_error_gets_one_scdaemon_restart_and_one_retry(fake_pass, capsys):
    set_modes, calls = fake_pass
    set_modes("card", "ok")
    assert secrets.pass_backend("demo/token") == VALUE
    assert calls() == ["pass show demo/token", "gpgconf --kill gpg-agent scdaemon",
                       "pass show demo/token"]
    assert "'Card error' (wedged scdaemon?) -- restarting it and retrying once" \
        in capsys.readouterr().err


def test_the_card_error_retry_is_single_not_a_loop(fake_pass):
    set_modes, calls = fake_pass
    set_modes("card")
    with pytest.raises(secrets.SecretError, match="Card error"):
        secrets.pass_backend("demo/token")
    assert [c for c in calls() if c.startswith("pass ")] == ["pass show demo/token"] * 2
    assert calls().count("gpgconf --kill gpg-agent scdaemon") == 1


def test_other_failures_are_not_retried(fake_pass):
    set_modes, calls = fake_pass
    set_modes("fail", "ok")
    with pytest.raises(secrets.SecretError, match="is not in the password store"):
        secrets.pass_backend("demo/token")
    assert calls() == ["pass show demo/token"]


def test_an_empty_value_is_a_failure_not_a_value(fake_pass):
    fake_pass[0]("empty")
    with pytest.raises(secrets.SecretError, match="returned an empty value"):
        secrets.pass_backend("demo/token")


def test_an_absent_pass_is_a_reported_failure_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))          # no pass anywhere
    with pytest.raises(secrets.SecretError, match="`pass` is not installed"):
        secrets.pass_backend("demo/token")


def test_a_hung_pass_is_a_timeout(fake_pass, monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))
    monkeypatch.setattr(secrets.subprocess, "run", hang)
    with pytest.raises(secrets.SecretError, match=r"timed out after \d+s"):
        secrets.pass_backend("demo/token")


# ---------- resolving and reporting ---------------------------------------------------------

def test_the_report_names_each_secret_and_never_its_value(fake_pass, capsys):
    ok = secrets.resolve_or_abort(DECLARED)
    out, err = capsys.readouterr()
    assert out == "secret: DEMO_TOKEN loaded from pass\n"
    assert VALUE not in out + err
    assert VALUE not in repr(ok) and ok[0].value == VALUE


def test_a_failed_secret_aborts_and_is_reported_by_name(fake_pass, capsys):
    fake_pass[0]("fail")
    with pytest.raises(LaunchError, match=r"launch aborted: 1 declared secret\(s\) failed "
                                          r"to resolve .* not a launched one"):
        secrets.resolve_or_abort(DECLARED)
    assert "secret: DEMO_TOKEN NOT SET -- Error: demo/token is not in the password store." \
        in capsys.readouterr().err


def test_a_resume_says_resumed(fake_pass):
    fake_pass[0]("fail")
    with pytest.raises(LaunchError, match="not a resumed one"):
        secrets.resolve_or_abort(DECLARED, resume=True)


def test_an_unknown_backend_calls_nothing_and_names_the_known_ones(fake_pass):
    [result] = secrets.resolve_secrets([DeclaredSecret("K", "keychain", "x")])
    assert not result.ok and result.error == "unknown backend 'keychain' (known: pass)"
    assert fake_pass[1]() == []


def test_the_file_text_quotes_the_value_for_the_shell(fake_pass):
    text = secrets.secret_file_text(secrets.resolve_or_abort(DECLARED))
    shell = subprocess.run(["bash", "-c", f'{text}printf %s "$DEMO_TOKEN"'],
                           capture_output=True, text=True, check=True)
    assert shell.stdout == VALUE


# ---------- the plan and the launch --------------------------------------------------------

def test_a_live_plan_names_a_fresh_private_path_and_resolves_nothing_yet(tmp_path, fake_pass):
    plan = plan_for(tmp_path, SECRETS)
    name = os.path.basename(plan.secret_file)
    assert name.startswith("session-start-secrets-") and name != "secrets.env"
    assert f"rm -f {plan.secret_file}" in plan.script
    assert not os.path.exists(plan.secret_file)
    assert fake_pass[1]() == []                        # planning is not an act


def test_a_dry_run_names_the_representative_path_and_calls_no_backend(tmp_path, fake_pass):
    plan = plan_for(tmp_path, SECRETS, flags=["--dry-run"])
    assert os.path.basename(plan.secret_file) == "secrets.env"
    with pytest.raises(LaunchError, match="never executed"):
        run.write_files(plan)
    assert fake_pass[1]() == []


def test_execute_hands_the_value_in_a_0600_file_never_argv_or_script(tmp_path, fake_pass,
                                                                     monkeypatch):
    plan = plan_for(tmp_path, SECRETS)
    seen = {}

    def fake_exec(file, argv, env):
        seen["mode"] = stat.S_IMODE(os.stat(plan.secret_file).st_mode)
        with open(plan.secret_file) as handle:
            seen["text"] = handle.read()
        seen["argv"] = argv
        raise SystemExit(0)
    monkeypatch.setattr(os, "execvpe", fake_exec)
    with pytest.raises(SystemExit):
        run.execute(plan)
    assert seen["mode"] == 0o600
    assert seen["text"].startswith("export DEMO_TOKEN=")
    assert VALUE not in " ".join(seen["argv"]) and VALUE not in plan.script


def test_a_failed_secret_writes_nothing_and_never_execs(tmp_path, fake_pass, monkeypatch):
    fake_pass[0]("fail")
    plan = plan_for(tmp_path, SECRETS, argv_tail=["x" * 5000])     # a first-message file too
    assert plan.files, "the plan should name a file for the long message"
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("exec'd without its secret"))
    with pytest.raises(LaunchError, match="launch aborted"):
        run.execute(plan)
    assert not os.path.exists(plan.secret_file)
    assert not any(os.path.exists(item.path) for item in plan.files)


def test_a_failed_exec_takes_the_secret_file_with_it(tmp_path, fake_pass, monkeypatch):
    plan = plan_for(tmp_path, SECRETS)

    def broken(*args):
        raise OSError("argv too long")
    monkeypatch.setattr(os, "execvpe", broken)
    with pytest.raises(LaunchError, match="exec failed"):
        run.execute(plan)
    assert not os.path.exists(plan.secret_file)


def test_a_secret_file_that_is_not_private_stops_the_launch(tmp_path, fake_pass, monkeypatch):
    plan = plan_for(tmp_path, SECRETS)
    monkeypatch.setattr(run, "is_private", lambda path: False)
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("exec'd with a readable file"))
    with pytest.raises(LaunchError, match="is not 0600"):
        run.execute(plan)
    assert not os.path.exists(plan.secret_file)


def test_a_file_that_fails_after_the_secret_takes_the_secret_with_it(tmp_path, fake_pass,
                                                                      monkeypatch):
    plan = plan_for(tmp_path, SECRETS, argv_tail=["x" * 5000])
    real = run.materialize

    def fail_after_secret(item):
        if item.path != plan.secret_file:
            raise OSError("disk full")
        return real(item)
    monkeypatch.setattr(run, "materialize", fail_after_secret)
    with pytest.raises(LaunchError, match="could not write"):
        run.execute(plan)
    assert not os.path.exists(plan.secret_file)


def test_main_no_longer_bridges_a_live_launch_that_declares_secrets(tmp_path, fake_pass,
                                                                     monkeypatch, capsys):
    monkeypatch.setenv("AGENTS_DIR", make_agents_root(tmp_path, front_matter=SECRETS))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("bridged"))
    seen = []
    monkeypatch.setattr(os, "execvpe", lambda f, argv, env: seen.append(argv) or (
        _ for _ in ()).throw(SystemExit(0)))
    for argv in (["scout", "hello"], ["scout", "--resume", "abc"]):
        with pytest.raises(SystemExit):
            start.main(argv)
    assert [argv[:2] for argv in seen] == [["bash", "-c"]] * 2
    out, err = capsys.readouterr()
    assert "using the bridge" not in err
    assert out.count("secret: DEMO_TOKEN loaded from pass") == 2


# ---------- end to end: the vendor's view ----------------------------------------------------

VENDOR_SEES = """#!/usr/bin/env bash
printf '%%s' "${DEMO_TOKEN-UNSET}" > "%(dir)s/token"
printf '%%s\\0' "$@" > "%(dir)s/argv"
ls %(tmp)s/session-start-secrets-* %(sessions)s/*/session-start-secrets-* \
   > "%(dir)s/left" 2>/dev/null
true
"""


@pytest.mark.parametrize("tail", [["hello"], ["--resume", "abc"]])
def test_the_vendor_gets_the_value_and_the_file_is_gone_before_it_runs(tmp_path, fake_pass,
                                                                         tail):
    record, tmpdir, bin_dir = tmp_path / "record", tmp_path / "tmpdir", tmp_path / "vbin"
    for path in (record, tmpdir, bin_dir):
        path.mkdir()
    root = make_agents_root(tmp_path, front_matter=SECRETS)
    (bin_dir / "claude").write_text(VENDOR_SEES % {
        "dir": record, "tmp": tmpdir, "sessions": os.path.join(root, "sessions")})
    (bin_dir / "claude").chmod(0o755)
    (tmp_path / "work").mkdir()
    env = clean_env(AGENTS_DIR=root, TMPDIR=str(tmpdir),
                    PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    result = subprocess.run(session_cmd("scout", *tail), capture_output=True, text=True,
                            timeout=TIMEOUT_S, cwd=str(tmp_path / "work"), env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert (record / "token").read_text() == VALUE
    assert VALUE not in (record / "argv").read_text()
    assert VALUE not in result.stdout + result.stderr
    assert (record / "left").read_text() == "", "the secret file outlived the prelude"
    assert glob.glob(str(tmpdir / "session-start-secrets-*")) == []


# ---------- review of c874019, lows 2-5 ---------------------------------------------------

def test_the_private_file_is_0600_before_its_first_byte(tmp_path, monkeypatch):
    """Low 2: the final mode alone let a mutant through - create it 0644,
    write, THEN chmod. The mode is read at the first write, under umask 000,
    whichever opener the writer uses. And at CREATION (round 2, low 1): a
    file born 0644 and chmodded before the write passed, yet anyone who
    opened it in that window keeps a descriptor that reads what follows."""
    path = str(tmp_path / "secrets.env")
    modes = []
    created = []
    real_open = os.open

    def watched_os_open(file, flags, mode=0o777, *args, **kwargs):
        if flags & os.O_CREAT and os.fspath(file) == path:
            created.append(mode)
        return real_open(file, flags, mode, *args, **kwargs)
    monkeypatch.setattr(context.os, "open", watched_os_open)

    def watched(opener):
        def wrapper(*args, **kwargs):
            stream = opener(*args, **kwargs)
            real_write = stream.write

            def write(text):
                modes.append(stat.S_IMODE(os.stat(path).st_mode))
                return real_write(text)
            stream.write = write
            return stream
        return wrapper
    monkeypatch.setattr(context.os, "fdopen", watched(os.fdopen))
    monkeypatch.setattr(context, "open", watched(open), raising=False)
    old = os.umask(0)
    try:
        context.write_private_file(path, "export T='x'\n")
    finally:
        os.umask(old)
    assert created == [0o600], "born with any other mode, it is readable before its chmod"
    assert modes and set(modes) == {0o600}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_an_exec_that_returns_takes_the_secret_file_with_it(tmp_path, fake_pass,
                                                            monkeypatch):
    """Round 2, low 2: a real exec never returns, but a stand-in that does
    must not leave live credentials behind a normal-looking return."""
    plan = plan_for(tmp_path, SECRETS)
    monkeypatch.setattr(run, "name_own_window", lambda *a, **k: None)
    monkeypatch.setattr(os, "execvpe", lambda *a: None)
    with pytest.raises(LaunchError, match="exec returned"):
        run.execute(plan)
    assert not os.path.exists(plan.secret_file)


def test_a_failure_naming_the_window_takes_the_secret_file_with_it(tmp_path, fake_pass,
                                                                   monkeypatch):
    """Low 3: between the write and the exec, not only the exec can fail."""
    plan = plan_for(tmp_path, SECRETS)

    def refused(*args, **kwargs):
        raise PermissionError(13, "stand-in for tmux refusing")
    monkeypatch.setattr(run, "name_own_window", refused)
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("exec'd after a failure"))
    with pytest.raises(PermissionError):
        run.execute(plan)
    assert not os.path.exists(plan.secret_file)


def test_naming_the_window_is_best_effort_against_an_os_refusal_too(monkeypatch):
    """Low 3: "any refusal: None, and the launch proceeds" - tmux that will
    not run is a refusal, not a crash."""
    def refused(*args, **kwargs):
        raise PermissionError(13, "tmux not executable")
    monkeypatch.setattr(run._tmux, "run", refused)
    agent = type("Agent", (), {"name": "scout"})()
    assert run.name_own_window(agent, "claude", {"TMUX": "/x,1,0", "TMUX_PANE": "%1"}) is None


def test_a_rename_the_os_refuses_is_best_effort_too(monkeypatch):
    """Round 2, low 3: the SECOND tmux call, the rename, after the window
    was found unnamed. Its swallow was unpinned."""
    calls = []

    def tmux(arguments, **kwargs):
        calls.append(arguments[0])
        if arguments[0] == "display-message":
            return subprocess.CompletedProcess(arguments, 0, "bash\tbash\n", "")
        raise PermissionError(13, "tmux refused the rename")
    monkeypatch.setattr(run._tmux, "run", tmux)
    agent = type("Agent", (), {"name": "scout"})()
    assert run.name_own_window(agent, "claude", {"TMUX": "/x,1,0", "TMUX_PANE": "%1"}) is None
    assert calls == ["display-message", "rename-window"]


def test_a_pass_that_will_not_run_is_a_reported_failure_not_a_crash(tmp_path, monkeypatch,
                                                                     capsys):
    """Low 4: a `pass` that is not executable raised PermissionError out of
    the resolve, and `write_files` died on `None.path`."""
    bin_dir = tmp_path / "broken-bin"
    bin_dir.mkdir()
    (bin_dir / "pass").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "pass").chmod(0o644)                     # found, but will not run
    plan = plan_for(tmp_path, SECRETS)
    # The ONLY pass on PATH: a lookup skips a file it cannot execute and
    # would find the wall's stub next, which runs.
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(os, "execvpe", lambda *a: pytest.fail("exec'd without its secret"))
    with pytest.raises(LaunchError, match="launch aborted"):
        run.execute(plan)
    assert "DEMO_TOKEN NOT SET -- `pass` could not run" in capsys.readouterr().err
    assert not os.path.exists(plan.secret_file)


def test_an_os_error_while_resolving_is_named_not_an_attribute_error(tmp_path, monkeypatch):
    """Low 4, the other half: whatever raises OSError before the first file
    exists is reported as resolving, never as `could not write None`."""
    plan = plan_for(tmp_path, SECRETS)

    def broken(*args, **kwargs):
        raise OSError(5, "stand-in I/O error")
    monkeypatch.setattr(run, "resolve_or_abort", broken)
    with pytest.raises(LaunchError, match="could not resolve the declared secrets"):
        run.execute(plan)
    assert not os.path.exists(plan.secret_file)


def test_a_planned_file_never_shows_its_text():
    """Low 5: the secret file's text is live credentials; its repr (in a
    traceback, a log line, a LaunchPlan's repr) names the path and size."""
    item = context.PlannedFile("/x/secrets.env", "export T='FAKE-SECRET-123'\n")
    assert "FAKE-SECRET-123" not in repr(item) and "FAKE-SECRET-123" not in str([item])
    assert "/x/secrets.env" in repr(item)
    assert item.text.endswith("'FAKE-SECRET-123'\n")        # the value itself is untouched
