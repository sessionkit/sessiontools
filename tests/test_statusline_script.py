"""The status-line script: stdin JSON in, one rendered line out (ST-136).

`src/sessiontools/statusline/statusline.sh` is what Claude Code runs. These
tests drive it the way Claude Code does: a JSON payload on stdin, the line
on stdout, and the exit code. They pin, from design.md sections 5 and 7:

- the RENDER, ANSI bytes exact. The `.out` goldens were rendered by the
  reconciled merge (findings/statusline.merged.sh, = the maintainer's live line
  minus R5), so they hold this script to the status line as it was in use;
- SKB-992: a missing, slow or failing helper still leaves a line, exit 0
  and nothing on stderr. Claude Code shows output only after exit 0, and a
  non-zero exit is a BLANK line, not a degraded one;
- NO NETWORK, three ways: a PATH allowlist, a static grep, and a kernel
  sandbox where one exists;
- the TIMING, with generous bounds;
- the WHEEL carries the script and its sample payload.

The script runs as `/bin/bash <script>` on purpose. On macOS that is bash
3.2, the shell its `#!/bin/bash` gets. The `bash` on PATH may be 5.x, and
it would hide a 3.2-only break.

Every run gets a PATH that holds ONLY the tools the script may call (the
allowlist), plus whatever fakes the test adds. A helper the script starts
calling without being added here fails every test that renders.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest
from wheel_support import build_wheel_offline

import sessiontools

BASH = "/bin/bash"
HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "statusline"
PACKAGE = Path(sessiontools.__file__).parent / "statusline"
SCRIPT = PACKAGE / "statusline.sh"
SAMPLE = PACKAGE / "sample.json"

# The tools the script may run. bash builtins (printf, echo, [, case, read)
# need no entry. A new helper fails the suite until it is added here, on
# purpose, after asking: is it local, fast, and present on a bare laptop?
ALLOWED = ("cat", "cut", "awk", "date", "jq", "wc", "mv")

ESC = "\x1b"
GREEN, YELLOW, RED = f"{ESC}[32m", f"{ESC}[33m", f"{ESC}[31m"
RESET, DIM = f"{ESC}[0m", f"{ESC}[2m"
SEP = f"{DIM}|{RESET}"

JQ = shutil.which("jq")
# The fakes run under the render's PATH, which is the allowlist: `sleep` and
# `touch` are not on it, so a fake names them by absolute path. (A fake that
# said bare `sleep 3` failed with "command not found" and returned at once:
# the slow-helper tests passed without anything being slow.)
SLEEP = shutil.which("sleep") or "/bin/sleep"
TOUCH = shutil.which("touch") or "/usr/bin/touch"
pytestmark = pytest.mark.skipif(JQ is None or not os.path.exists(BASH),
                                reason="the status line needs /bin/bash and jq")


# --- the harness ---------------------------------------------------------

def allow_bin(root: Path, *, without: tuple[str, ...] = ()) -> Path:
    """A directory of links to the allowlisted tools, minus `without`."""
    path = root / "allow-bin"
    path.mkdir(exist_ok=True)
    for name in ALLOWED:
        if name in without:
            continue
        real = shutil.which(name)
        assert real, f"{name} is on the allowlist but not on this machine"
        link = path / name
        if not link.exists():
            link.symlink_to(real)
    return path


def fake(root: Path, name: str, body: str) -> Path:
    """A one-file bin directory holding an executable `name`."""
    path = root / f"fake-{name}"
    path.mkdir(exist_ok=True)
    tool = path / name
    tool.write_text(f"#!/bin/bash\n{body}\n")
    tool.chmod(0o755)
    return path


def assert_fake_marks(tool: Path, marker: Path, env: dict) -> None:
    """The positive control: the fake, run under the render's own env,
    does leave its marker. Without it, a marker that never appears proves
    nothing (the fake might simply be broken)."""
    subprocess.run([str(tool)], env={**env, "SLEEP_FOR": "0"}, timeout=30,
                   capture_output=True, stdin=subprocess.DEVNULL, check=False)
    assert marker.exists(), f"the fake {tool.name} cannot leave its marker"
    marker.unlink()


def env_for(root: Path, *bins: Path, **extra: str) -> dict:
    """A from-nothing environment: HOME under the test's tmp, PATH = the
    fakes first, then the allowlist; no SESSIONTOOLS_STATUSLINE_* leaks in
    from the developer's shell."""
    home = root / "home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join(str(b) for b in bins),
        "LC_ALL": "C",
    }
    env.update(extra)
    return env


def render(payload: str | bytes, env: dict, *, script: Path = SCRIPT,
           prefix: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    data = payload.encode() if isinstance(payload, str) else payload
    return subprocess.run([*prefix, BASH, str(script)], input=data, env=env,
                          capture_output=True, timeout=30, check=False)


def fixture(name: str, cwd: Path | None = None) -> str:
    text = (FIXTURES / name).read_text()
    if cwd is not None:
        text = text.replace("@CWD@", str(cwd))
    return text


def repo(root: Path, name: str = "demo-repo") -> Path:
    """A directory that looks like a git checkout: it holds `.git`."""
    path = root / name
    (path / ".git").mkdir(parents=True)
    return path


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def payload(**cw) -> str:
    """A minimal payload with a context_window built from keyword args."""
    return json.dumps({"model": {"display_name": "M"}, "context_window": cw})


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """The log write is backgrounded: poll for it, never sleep blind."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def base(tmp_path):
    """(tmp root, a default env: allowlist only, no tap, no log dir)."""
    return tmp_path, env_for(tmp_path, allow_bin(tmp_path))


# --- fixtures in, line out -------------------------------------------------

@pytest.mark.parametrize("name", ["full", "empty", "early"])
def test_fixture_renders_the_golden_line_byte_for_byte(base, name):
    root, env = base
    cwd = repo(root)
    got = render(fixture(f"{name}.json", cwd), env)
    assert got.returncode == 0
    assert got.stderr == b""
    assert got.stdout == (FIXTURES / f"{name}.out").read_bytes()


def test_the_full_line_reads_as_the_operators(base):
    root, env = base
    got = render(fixture("full.json", repo(root)), env)
    assert strip_ansi(got.stdout.decode()) == (
        "[worker] | demo-repo | 421K/1.0M (42%) | $12 | 5h:12% wk:63% | Opus 5.5\n")


def test_sample_payload_renders_the_preview_line(base):
    """The refusal's preview (design 4.5) renders sample.json with both
    switches off; it has no cwd, so no repo segment."""
    _root, env = base
    env.update(SESSIONTOOLS_STATUSLINE_TAP="off", SESSIONTOOLS_STATUSLINE_LOG="off")
    got = render(SAMPLE.read_text(), env)
    assert got.returncode == 0 and got.stderr == b""
    assert got.stdout == (FIXTURES / "sample.out").read_bytes()
    assert strip_ansi(got.stdout.decode()) == (
        "Claude | 421K/1.0M (42%) | $12 | 5h:12% wk:63% | Opus 5.5\n")


@pytest.mark.parametrize("pct,colour", [
    (0, GREEN), (49, GREEN), (49.9, GREEN), (50, YELLOW), (79, YELLOW),
    (80, RED), (100, RED)])
def test_percentage_colour_edges(base, pct, colour):
    _root, env = base
    got = render(payload(used_percentage=pct), env)
    shown = int(pct)
    assert f"{colour}({shown}%){RESET}".encode() in got.stdout


def test_null_percentage_reads_as_zero(base):
    _root, env = base
    got = render(payload(used_percentage=None, current_usage=None), env)
    assert f"0/200K {GREEN}(0%){RESET}".encode() in got.stdout


@pytest.mark.parametrize("size,shown", [(200000, "200K"), (1000000, "1.0M"), (999, "999")])
def test_window_size_formats(base, size, shown):
    _root, env = base
    got = render(payload(context_window_size=size), env)
    assert f"/{shown} ".encode() in got.stdout


@pytest.mark.parametrize("cost,shown", [(0, "$0.00"), (9.994, "$9.99"), (10, "$10"),
                                        (123.4, "$123")])
def test_cost_formats_under_and_over_ten(base, cost, shown):
    _root, env = base
    got = render(json.dumps({"cost": {"total_cost_usd": cost}}), env)
    assert f"{DIM}{shown}{RESET}".encode() in got.stdout


def test_only_five_hour_shows_a_question_mark_for_the_week(base):
    _root, env = base
    got = render(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 33.3}}}), env)
    assert "| 5h:33% wk:?% |" in strip_ansi(got.stdout.decode())


def test_no_rate_limits_hides_the_segment(base):
    _root, env = base
    got = render("{}", env)
    assert "5h:" not in got.stdout.decode()


def test_no_agent_says_claude_and_an_agent_is_bracketed(base):
    _root, env = base
    assert render("{}", env).stdout.decode().startswith("Claude ")
    got = render(json.dumps({"agent": {"name": "scout"}}), env)
    assert got.stdout.decode().startswith("[scout] ")


# --- the repo segment (A4: builtins, no git fork) ----------------------------

def test_cwd_missing_or_not_a_directory_has_no_repo_segment(base):
    root, env = base
    for cwd in (root / "gone", root / "home"):
        if cwd.name == "home":
            cwd = root / "a-file"
            cwd.write_text("x")
        got = render(json.dumps({"cwd": str(cwd)}), env)
        assert got.returncode == 0 and got.stderr == b""
        assert strip_ansi(got.stdout.decode()).startswith("Claude | 0/200K")


def test_cwd_in_a_subdir_names_the_repo(base):
    root, env = base
    sub = repo(root) / "src" / "deep"
    sub.mkdir(parents=True)
    got = render(json.dumps({"workspace": {"current_dir": str(sub)}}), env)
    assert strip_ansi(got.stdout.decode()).startswith("Claude | demo-repo | ")


def test_a_worktree_dot_git_file_names_the_worktree(base):
    root, env = base
    wt = root / "my-worktree"
    (wt / "pkg").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/my-worktree\n")
    got = render(json.dumps({"cwd": str(wt / "pkg")}), env)
    assert strip_ansi(got.stdout.decode()).startswith("Claude | my-worktree | ")


def test_no_repo_falls_back_to_the_cwd_name(base):
    root, env = base
    plain = root / "just-a-dir"
    plain.mkdir()
    got = render(json.dumps({"cwd": str(plain) + "/"}), env)
    assert strip_ansi(got.stdout.decode()).startswith("Claude | just-a-dir | ")


def test_the_nearest_repo_wins(base):
    root, env = base
    inner = repo(repo(root, "outer"), "inner")
    got = render(json.dumps({"cwd": str(inner)}), env)
    assert "| inner |" in strip_ansi(got.stdout.decode())


def test_the_script_never_runs_git(base):
    """A4: a `git` first on PATH that would hang is never reached."""
    root, env = base
    marker = root / "git-ran"
    slow_git = fake(root, "git", f"{TOUCH} '{marker}'; {SLEEP} ${{SLEEP_FOR:-5}}")
    env["PATH"] = f"{slow_git}{os.pathsep}{env['PATH']}"
    assert_fake_marks(slow_git / "git", marker, env)
    got = render(json.dumps({"cwd": str(repo(root))}), env)
    assert got.returncode == 0
    assert not marker.exists()


# --- A5: the line is data, never a printf format ------------------------------

def test_percent_and_backslash_in_names_print_as_themselves(base):
    root, env = base
    odd = repo(root, r"100%-sure\n")
    got = render(json.dumps({"cwd": str(odd),
                             "model": {"display_name": r"Opus %s 5\t5"}}), env)
    assert got.returncode == 0 and got.stderr == b""
    line = strip_ansi(got.stdout.decode())
    assert r"| 100%-sure\n |" in line
    assert line.endswith("| Opus %s 5\\t5\n")


# --- A2 / A3: a degraded line, never a wrong one -----------------------------

@pytest.mark.parametrize("stdin", [b"not json", b"", b"[1, 2]", b'"a string"', b"null",
                                   b'{"half": '])
def test_bad_input_says_so(base, stdin):
    _root, env = base
    got = render(stdin, env)
    assert (got.returncode, got.stdout, got.stderr) == (
        0, b"Claude | statusline: bad input\n", b"")


def test_jq_missing_says_so(tmp_path):
    env = env_for(tmp_path, allow_bin(tmp_path, without=("jq",)))
    got = render(fixture("full.json", tmp_path), env)
    assert (got.returncode, got.stdout, got.stderr) == (
        0, b"Claude | statusline: jq not found\n", b"")


def test_odd_numbers_degrade_to_zero_not_to_an_error(base):
    _root, env = base
    got = render(json.dumps({"context_window": {
        "used_percentage": "lots", "context_window_size": "big",
        "current_usage": {"input_tokens": "x"}}}), env)
    assert got.returncode == 0 and got.stderr == b""
    assert "0/0 (0%)" in strip_ansi(got.stdout.decode())


# --- SKB-992: helpers fail soft ----------------------------------------------

def tap(root: Path, body: str) -> Path:
    return fake(root, "ccusage-limits-tap", body)


def test_tap_missing_is_silent(base):
    root, env = base
    got = render(fixture("full.json", repo(root)), env)
    assert got.returncode == 0 and got.stderr == b""


def test_tap_gets_the_payload(base):
    root, env = base
    seen = root / "tap-saw.json"
    fake_tap = tap(root, f"cat > '{seen}'")
    env["PATH"] = f"{fake_tap}{os.pathsep}{env['PATH']}"
    body = fixture("full.json", repo(root))
    render(body, env)

    def complete():
        try:
            return json.loads(seen.read_text()) == json.loads(body)
        except (OSError, ValueError):
            return False
    assert wait_for(complete)


@pytest.mark.parametrize("body", [
    "echo boom >&2; exit 1",
    "exit 0",            # exits without reading: the writer must not complain
    "echo 'noise on stdout'; exit 3",
])
def test_tap_failing_still_renders(base, body):
    root, env = base
    env["PATH"] = f"{tap(root, body)}{os.pathsep}{env['PATH']}"
    big = json.loads(fixture("full.json", repo(root)))
    big["padding"] = "x" * 200_000   # bigger than a pipe buffer
    got = render(json.dumps(big), env)
    assert got.returncode == 0 and got.stderr == b""
    assert got.stdout.startswith(b"[worker] ")
    assert b"noise" not in got.stdout and b"boom" not in got.stdout


@pytest.mark.parametrize("word", ["off", "OFF", "0", "false", "no"])
def test_tap_switch_off_never_runs_it(base, word):
    root, env = base
    marker = root / "tap-ran"
    fake_tap = tap(root, f"{TOUCH} '{marker}'")
    env["PATH"] = f"{fake_tap}{os.pathsep}{env['PATH']}"
    assert_fake_marks(fake_tap / "ccusage-limits-tap", marker, env)
    env["SESSIONTOOLS_STATUSLINE_TAP"] = word
    render(fixture("full.json", repo(root)), env)
    time.sleep(0.3)
    assert not marker.exists()


def log_dir(env: dict) -> Path:
    path = Path(env["HOME"]) / ".config" / "session"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_log_dir_missing_writes_nothing_and_creates_nothing(base):
    root, env = base
    got = render(fixture("full.json", repo(root)), env)
    assert got.returncode == 0 and got.stderr == b""
    time.sleep(0.3)
    assert list(Path(env["HOME"]).iterdir()) == []


def test_log_record_schema_is_unchanged(base):
    """The fleet reads this log (a seat's model, by sid): the keys are fixed."""
    root, env = base
    log = log_dir(env) / "statusline-log.jsonl"
    render(fixture("full.json", repo(root)), env)
    assert wait_for(lambda: log.exists() and log.read_text().endswith("\n"))
    record = json.loads(log.read_text())
    assert list(record) == ["t", "sid", "model", "cost", "ctx", "pct", "size", "tin", "tout"]
    assert record["sid"] == "11111111-2222-3333-4444-555555555555"
    assert record["model"] == "claude-opus-5-5"
    assert record["ctx"] == {"input_tokens": 1200, "cache_creation_input_tokens": 20000,
                             "cache_read_input_tokens": 400000}
    assert (record["pct"], record["size"], record["tin"], record["tout"]) == (
        42.7, 1000000, 2400000, 61000)
    assert abs(record["t"] - time.time()) < 60


@pytest.mark.parametrize("word", ["off", "No", "0", "false"])
def test_log_switch_off_writes_nothing(base, word):
    root, env = base
    log = log_dir(env) / "statusline-log.jsonl"
    env["SESSIONTOOLS_STATUSLINE_LOG"] = word
    render(fixture("full.json", repo(root)), env)
    time.sleep(0.3)
    assert not log.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through a read-only dir")
def test_log_dir_read_only_still_renders(base):
    root, env = base
    d = log_dir(env)
    d.chmod(0o555)
    try:
        got = render(fixture("full.json", repo(root)), env)
        assert got.returncode == 0 and got.stderr == b""
        assert got.stdout.startswith(b"[worker] ")
    finally:
        d.chmod(0o755)


def test_rotation_moves_a_full_log_and_starts_a_fresh_one(base):
    root, env = base
    d = log_dir(env)
    log = d / "statusline-log.jsonl"
    old = '{"old": true}\n' * 20
    log.write_text(old)
    env["SESSIONTOOLS_STATUSLINE_LOG_MAX"] = "100"
    render(fixture("full.json", repo(root)), env)
    rotated = d / "statusline-log.jsonl.1"
    assert wait_for(lambda: rotated.exists() and log.exists()
                    and log.read_text().endswith("\n"))
    assert rotated.read_text() == old
    lines = log.read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["model"] == "claude-opus-5-5"


def test_a_log_under_the_limit_is_appended_not_rotated(base):
    root, env = base
    d = log_dir(env)
    log = d / "statusline-log.jsonl"
    log.write_text('{"old": true}\n')
    render(fixture("full.json", repo(root)), env)
    assert wait_for(lambda: len(log.read_text().splitlines()) == 2)
    assert not (d / "statusline-log.jsonl.1").exists()


def slow_log_jq(root: Path) -> Path:
    """A jq that sleeps 3 s only when given -c (the log's call)."""
    real = shutil.which("jq")
    return fake(root, "jq",
                f'case " $* " in *" -c "*) {SLEEP} 3;; esac; exec "{real}" "$@"')


def eof_seconds(env: dict, body: str) -> tuple[float, subprocess.CompletedProcess]:
    """How long a reader that waits for EOF on stdout AND stderr waits.
    `run` returns only when both pipes close, so a backgrounded child still
    holding either one shows up here as the time it holds it."""
    start = time.monotonic()
    got = render(body, env)
    return time.monotonic() - start, got


def test_eof_reader_is_not_held_by_a_slow_tap_or_a_slow_log(base):
    """R1, R6 and A6: with a 3 s tap and a 3 s log jq both on, the line
    still arrives, and both pipes close, in under 1 s."""
    root, env = base
    log = log_dir(env) / "statusline-log.jsonl"
    tapped = root / "tap-done"
    env["PATH"] = os.pathsep.join([
        str(slow_log_jq(root)), str(tap(root, f"{SLEEP} 3; {TOUCH} '{tapped}'")),
        env["PATH"]])
    start = time.monotonic()
    seconds, got = eof_seconds(env, fixture("full.json", repo(root)))
    assert got.returncode == 0 and got.stderr == b""
    assert got.stdout == (FIXTURES / "full.out").read_bytes()
    assert seconds < 1.0, f"the reader waited {seconds:.2f}s for EOF"
    # ...and both helpers really were slow, and really did finish after it:
    assert wait_for(lambda: tapped.exists() and log.exists(), timeout=10)
    assert time.monotonic() - start >= 2.5


def test_slow_log_jq_still_writes_its_record(base):
    root, env = base
    log = log_dir(env) / "statusline-log.jsonl"
    env["PATH"] = f"{slow_log_jq(root)}{os.pathsep}{env['PATH']}"
    render(fixture("full.json", repo(root)), env)
    assert wait_for(lambda: log.exists() and log.read_text().endswith("\n"), timeout=8)


# --- no network ----------------------------------------------------------

def test_allowlist_is_all_the_script_needs(base):
    """Every render in this module runs with PATH = the allowlist; this one
    names it: the full fixture, a fake tap, the log on. A tool outside the
    list would print `command not found` on stderr."""
    root, env = base
    log_dir(env)
    env["PATH"] = f"{tap(root, 'cat >/dev/null')}{os.pathsep}{env['PATH']}"
    got = render(fixture("full.json", repo(root)), env)
    assert (got.returncode, got.stderr) == (0, b"")
    assert got.stdout == (FIXTURES / "full.out").read_bytes()


def test_the_script_text_names_no_network_tool():
    text = SCRIPT.read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for word in ("curl", "wget", "nc ", "ncat", "ssh", "scp", "/dev/tcp", "/dev/udp",
                 "http", "telnet", "socat"):
        assert word not in code, f"statusline.sh mentions {word!r}"


def _sandbox_prefix() -> tuple[str, ...] | None:
    # Absolute paths: the render's PATH is the allowlist, not the host's.
    if sys.platform == "darwin" and (tool := shutil.which("sandbox-exec")):
        return (tool, "-p", "(version 1)(allow default)(deny network*)")
    if sys.platform.startswith("linux") and (tool := shutil.which("unshare")):
        return (tool, "-rn")
    return None


def test_renders_inside_a_no_network_sandbox(base):
    """The kernel says no to every socket, and the line still renders. A
    canary proves the sandbox really denies before the render counts."""
    import socket

    prefix = _sandbox_prefix()
    if prefix is None:
        pytest.skip("no sandbox-exec or unshare here")
    root, env = base
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        canary = subprocess.run([*prefix, BASH, "-c", f"exec 3<>/dev/tcp/127.0.0.1/{port}"],
                                capture_output=True, timeout=30, check=False)
        unsandboxed = canary.returncode == 0
        usable = b"Operation not permitted" in canary.stderr or canary.returncode != 0
    if unsandboxed or not usable:
        pytest.skip(f"{prefix[0]} did not deny a local connect here")
    if b"sandbox_apply" in canary.stderr or b"unshare:" in canary.stderr:
        pytest.skip(f"{prefix[0]} cannot start here: {canary.stderr[:100]!r}")
    log_dir(env)
    env["PATH"] = f"{tap(root, 'cat >/dev/null')}{os.pathsep}{env['PATH']}"
    got = render(fixture("full.json", repo(root)), env, prefix=prefix)
    assert (got.returncode, got.stderr) == (0, b"")
    assert got.stdout == (FIXTURES / "full.out").read_bytes()


# --- timing --------------------------------------------------------------

def test_render_is_fast(base):
    """Measured 0.07-0.09 s on a real host. The bound is generous on purpose: the
    load guard is 14, and a timing test that flakes teaches people to
    ignore it. The real guard against a stall is the EOF test above."""
    root, env = base
    body = fixture("full.json", repo(root))
    times = []
    for _ in range(5):
        start = time.monotonic()
        got = render(body, env)
        times.append(time.monotonic() - start)
        assert got.returncode == 0
    assert statistics.median(times) < 0.25, f"renders took {times}"


# --- the package ---------------------------------------------------------

def test_script_is_executable_and_bash_3_clean():
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().startswith("#!/bin/bash\n")
    got = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, check=False)
    assert got.returncode == 0, got.stderr


def test_sample_is_a_json_object_with_no_cwd():
    data = json.loads(SAMPLE.read_text())
    assert isinstance(data, dict)
    assert "cwd" not in data and "workspace" not in data


def test_the_wheel_carries_the_script_and_the_sample(tmp_path):
    import zipfile

    root = Path(sessiontools.__file__).resolve().parents[2]
    if not (root / "pyproject.toml").exists():
        pytest.skip("not a source checkout")
    wheel = build_wheel_offline(root, tmp_path)
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        assert "sessiontools/statusline/statusline.sh" in names
        assert "sessiontools/statusline/sample.json" in names
        mode = zf.getinfo("sessiontools/statusline/statusline.sh").external_attr >> 16
        assert mode & 0o111, "the shipped script lost its exec bit"
        assert zf.read("sessiontools/statusline/statusline.sh") == SCRIPT.read_bytes()
