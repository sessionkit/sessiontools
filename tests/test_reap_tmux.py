"""`session reap --yes` end to end, on a PRIVATE tmux server (ST-146 do-002).

The server is `-L st146-<rand>` under conftest's scratch TMUX_TMPDIR, and
the sidecar folder, the state dir and HOME are conftest's scratch ones.
`listing.rows()` reads only the servers under that folder, so the real fleet
is out of reach. The first thing each test does is check that. Nothing here
runs `--yes` against a real seat (R5).

A FAKE CLAUDE runs in each pane. It writes its own sidecar (idle for 2h, a
cwd and a name). It starts a pager monitor and, by mode, one tool shell
running a listener or work. Each child is in a SESSION OF ITS OWN, the shape
measured live (design.md §5): the pane's hangup does not reach them. With
CLEANUP it ends its children on SIGHUP, as claude does, and without it it
leaves them, which is the survivor case. Every child's pid is recorded, so
the teardown kills exactly those, by process group.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from test_reap import git, task_dir

from sessiontools import listing, pane_state, procs, reap, turn

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

FAKE_CLAUDE = textwrap.dedent("""\
    import json, os, signal, subprocess, sys, time
    sidecars, mode, bindir, cwd, name, status, cleanup, idle = sys.argv[1:9]
    pid = os.getpid()
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], env=env,
                           capture_output=True, text=True).stdout.strip()
    kids = [subprocess.Popen([bindir + "/pager", "listen", "--monitor"], start_new_session=True)]
    if mode == "listener":
        script = "eval '" + bindir + "/pager listen --timeout 300' < /dev/null && pwd -P >| /dev/null"
        kids.append(subprocess.Popen(["bash", "-c", script], start_new_session=True))
    elif mode == "work":
        kids.append(subprocess.Popen(["bash", "-c", "eval 'sleep 300' < /dev/null && pwd -P >| /dev/null"],
                                     start_new_session=True))
    def hup(signum, frame):
        if cleanup == "1":
            for k in kids:
                try:
                    os.killpg(k.pid, signal.SIGTERM)
                except OSError:
                    pass
        os._exit(0)
    signal.signal(signal.SIGHUP, hup)
    data = {"pid": pid, "sessionId": "%08x-0000-4000-8000-000000000146" % pid,
            "status": status, "updatedAt": int((time.time() - float(idle)) * 1000),
            "procStart": start, "cwd": cwd, "name": name,
            "kids": [k.pid for k in kids]}
    tmp = os.path.join(sidecars, "." + str(pid) + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.rename(tmp, os.path.join(sidecars, str(pid) + ".json"))
    time.sleep(300)
""")

PAGER = "import time\ntime.sleep(300)\n"

#: A fake `tm task work` launcher (an allowed holder, ST-146 do-004 R1): it
#: runs the seat after `--` as its child. FAKE_TM_EXTRA gives it one more
#: process, its pid written to FAKE_TM_MARKER: `child` (a job of its own, on
#: the terminal), `letgo` (a job let go: ppid 1, on the terminal) or `setsid`
#: (a child in a session of its own: in its tree, off the terminal).
FAKE_TM = textwrap.dedent("""\
    import os, subprocess, sys
    extra, marker = os.environ.get("FAKE_TM_EXTRA", ""), os.environ.get("FAKE_TM_MARKER", "")
    seat = sys.argv[sys.argv.index("--") + 1:]
    if extra == "child":
        open(marker, "w").write(str(subprocess.Popen(["sleep", "301"]).pid))
    elif extra == "letgo":
        subprocess.run(["sh", "-c", "sleep 302 & echo $! > " + marker])
    elif extra == "setsid":
        p = subprocess.Popen(["sleep", "304"], start_new_session=True)
        open(marker, "w").write(str(p.pid))
    elif extra == "after":
        open(marker, "w").write(str(os.getpid()))
    subprocess.run(seat)
    if extra == "after":
        open(marker + ".rest", "w").write("ran")
""")
IDLE = 2 * 3600


@pytest.fixture(autouse=True)
def _walled(monkeypatch):
    monkeypatch.delenv(turn.SWITCH_ENV, raising=False)
    assert os.path.basename(os.environ["TMUX_TMPDIR"]).startswith("st-tmux-")
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    assert "no-sidecars" in os.environ["SESSION_SIDECAR_DIR"]
    assert ".claude" not in os.environ["SESSION_SIDECAR_DIR"]


class Server:
    def __init__(self, tmp: Path):
        self.socket = f"st146-{secrets.token_hex(3)}"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "pager").write_text(f"#!{sys.executable}\n{PAGER}")
        (self.bin / "pager").chmod(0o755)
        (self.bin / "tm").write_text(f"#!{sys.executable}\n{FAKE_TM}")
        (self.bin / "tm").chmod(0o755)
        self.fake = tmp / "fake_claude.py"
        self.fake.write_text(FAKE_CLAUDE)
        self.sidecars = Path(os.environ["SESSION_SIDECAR_DIR"])
        out = self.tmux("new-session", "-d", "-x", "200", "-y", "50", "-P", "-F", "#{pane_id}",
                        "sleep 300")
        assert out.returncode == 0, out.stderr
        self.bystander = out.stdout.strip()       # keeps the server up, never a seat

    def tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.socket, *args],
                              capture_output=True, text=True, check=False, timeout=10)

    def command(self, cwd, name, mode="nothing", status="idle", cleanup=True, idle=IDLE) -> str:
        return (f"{sys.executable} {self.fake} {self.sidecars} {mode} {self.bin} {cwd} {name} "
                f"{status} {int(cleanup)} {idle}")

    def launched(self, cwd, name, task="ST-1", extra="", marker="", **kw) -> str:
        """The seat under a fake `tm task work` that holds the pane."""
        return (f"env FAKE_TM_EXTRA={extra} FAKE_TM_MARKER={marker} {self.bin}/tm task work "
                f"{task} -y -- {self.command(cwd, name, **kw)}")

    def window(self, command: str) -> str:
        out = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}", command)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def split(self, pane: str, command: str) -> str:
        out = self.tmux("split-window", "-d", "-t", pane, "-P", "-F", "#{pane_id}", command)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    def seat(self, cwd, name, **kw) -> str:
        return self.window(self.command(cwd, name, **kw))

    def panes(self) -> dict[str, str]:
        """pane id -> window id, every pane on the server."""
        out = self.tmux("list-panes", "-a", "-F", "#{pane_id} #{window_id}")
        return dict(line.split() for line in out.stdout.splitlines())

    def sidecar(self, pane: str) -> dict:
        """The sidecar of the fake in PANE, waiting until it is written."""
        t0 = time.monotonic()
        while True:
            for path in self.sidecars.glob("*.json"):
                data = json.loads(path.read_text())
                if self.pane_of(data["pid"]) == pane:
                    return data
            assert time.monotonic() - t0 < 10, f"no sidecar from {pane}"
            time.sleep(0.05)

    def pane_of(self, pid: int) -> str | None:
        table = procs.table()
        index = {p: pane for p, pane in _panes(self).items()}
        for a in procs.ancestors(pid, table):
            if a in index:
                return index[a]
        return None

    def rewrite(self, pid: int, **changes) -> None:
        path = self.sidecars / f"{pid}.json"
        data = {**json.loads(path.read_text()), **changes}
        path.write_text(json.dumps(data))

    def every_kid(self) -> list[int]:
        out = []
        for path in self.sidecars.glob("*.json"):
            out += json.loads(path.read_text()).get("kids", [])
        return out


def _panes(server: Server) -> dict[int, str]:
    out = server.tmux("list-panes", "-a", "-F", "#{pane_pid} #{pane_id}")
    return {int(a): b for a, b in (line.split() for line in out.stdout.splitlines())}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def gone_soon(pid: int, bound: float = 5.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < bound:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def server(tmp_path, monkeypatch):
    s = Server(tmp_path)
    # The fake `tm` is the launcher INSTALLED here (`installed_launchers`
    # reads PATH), after the server is up, so the server's env is untouched.
    monkeypatch.setenv("PATH", f"{s.bin}{os.pathsep}{os.environ['PATH']}")
    yield s
    # Our own children only: each kid led a session of its own, so its
    # process group is its pid. conftest then kills the private server.
    for pid in s.every_kid():
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.fixture
def store(tmp_path):
    """A scratch store: ST-1 completed, ST-2 active, ST-3 completed but dirty."""
    root = tmp_path / "store"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("**/worktree/\n")
    dirs = {t: task_dir(root, t, s) for t, s in
            (("ST-1", "completed"), ("ST-2", "active"), ("ST-3", "completed"))}
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    (dirs["ST-3"] / "unsaved.md").write_text("draft\n")
    (root / "nowhere").mkdir()
    dirs["none"] = root / "nowhere"
    return dirs


def world(**kw) -> reap.World:
    def table():
        return procs.table(commands=True)
    return reap.World(rows=listing.rows, table=table, listeners=pane_state.listeners, **kw)


def run_reap(argv, capsys):
    rc = reap.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_yes_closes_only_the_done_free_panes_on_a_private_server(server, store, capsys):
    d = store
    seats = {
        "done": server.seat(d["ST-1"], "p+st-1.worker.done"),
        "listener": server.seat(d["ST-1"], "p+st-1.worker.listener", mode="listener"),
        "busy": server.seat(d["ST-1"], "p+st-1.worker.busy", status="busy"),
        "waiting": server.seat(d["ST-1"], "p+st-1.worker.waiting", status="waiting"),
        "work": server.seat(d["ST-1"], "p+st-1.worker.work", mode="work"),
        "open": server.seat(d["ST-2"], "p+st-2.foreman.open"),
        "notask": server.seat(d["none"], "p+super.notask"),
        "dirty": server.seat(d["ST-3"], "p+st-3.worker.dirty"),
    }
    # ONE window: a busy seat and two idle done seats beside it
    seats["w-busy"] = server.seat(d["ST-1"], "p+st-1.foreman.wbusy", mode="work")
    seats["w-done1"] = server.split(seats["w-busy"], server.command(d["ST-1"], "p+st-1.worker.w1"))
    seats["w-done2"] = server.split(seats["w-busy"], server.command(d["ST-1"], "p+st-1.worker.w2",
                                                                    mode="listener"))
    # TWO seats in one pane, both done and FREE
    two = (f"{server.command(d['ST-1'], 'p+st-1.worker.twoa')} & "
           f"{server.command(d['ST-1'], 'p+st-1.worker.twob')} & wait")
    seats["two"] = server.window(f"sh -c {json.dumps(two)}")

    info = {k: server.sidecar(p) for k, p in seats.items() if k != "two"}
    pair = []
    t0 = time.monotonic()
    while len(pair) < 2:
        pair = [json.loads(p.read_text()) for p in server.sidecars.glob("*.json")
                if "two" in json.loads(p.read_text())["name"]]
        assert time.monotonic() - t0 < 10
        time.sleep(0.05)
    windows = server.panes()
    listener_kids = info["listener"]["kids"] + info["w-done2"]["kids"]

    rc, out, err = run_reap(["--yes"], capsys)
    print(out, err)
    assert rc == 0, out + err

    closed = {"done", "listener", "w-done1", "w-done2"}
    left = server.panes()
    for key, pane in seats.items():
        if key in closed:
            assert pane not in left, f"{key} survived"
        else:
            assert pane in left, f"{key} was closed"
    for key in closed:
        assert gone_soon(info[key]["pid"]), key
    for pid in listener_kids:                       # the listeners went with the seat
        assert gone_soon(pid), pid
    for key in set(info) - closed:
        assert alive(info[key]["pid"]), key
    for data in pair:
        assert alive(data["pid"])
    # the shared window and its busy seat survive; only the idle panes went
    assert left[seats["w-busy"]] == windows[seats["w-busy"]]
    assert server.bystander in left

    by_name = {line.split()[1]: line for line in out.splitlines()
               if line.split() and line.split()[0] in {s["sessionId"][:8] for s in info.values()}
               | {p["sessionId"][:8] for p in pair}}
    def why(key):
        return by_name[info[key]["name"][:40]] if key in info else None
    assert "seat BUSY (waiting)" in why("waiting")
    assert "seat BUSY (work: sleep 300)" in why("work")
    assert "task ST-2 is active (not done)" in why("open")
    assert "no task (a seat with no task is never closed)" in why("notask")
    assert "store dirty: ?? projects/p/tasks/ST-3_feat_x/unsaved.md" in why("dirty")
    assert "p+st-1.worker.busy" not in out           # busy: idle 0, never listed
    assert out.count("pane shared with") == 2
    for key in closed:
        assert f"closed {info[key]['sessionId'][:8]} ({info[key]['name']})" in out

    log = (Path(os.environ["SESSION_STATE_DIR"]) / "reap.log").read_text().splitlines()
    assert sorted(line.split("\t")[5] for line in log) == ["closed"] * 4
    assert all(f"\t{server.socket}:%" in line for line in log)


def test_a_seat_turned_busy_between_the_list_and_the_act_is_kept(server, store, capsys):
    pane = server.seat(store["ST-1"], "p+st-1.worker.x")
    data = server.sidecar(pane)
    w = world()
    [v] = reap.plan(w.rows(), w.table(), w.now(), reap.DEFAULT_IDLE_SECONDS, w.listeners())
    assert v.verdict == reap.CLOSE, v.why
    server.rewrite(data["pid"], status="busy", updatedAt=int(time.time() * 1000))
    a = reap.act(v, reap.DEFAULT_IDLE_SECONDS, w)
    assert a.outcome == reap.SKIPPED and "no longer idle past 1h00m" in a.why
    assert pane in server.panes() and alive(data["pid"])


def test_a_seat_turned_busy_during_the_tail_check_is_kept(server, store, capsys):
    pane = server.seat(store["ST-1"], "p+st-1.worker.x")
    data = server.sidecar(pane)
    calls = []
    def tail(d):
        calls.append(d)
        if len(calls) == 2:                     # the act's own tail check
            server.rewrite(data["pid"], status="busy", updatedAt=int(time.time() * 1000))
        return reap.tail(d)
    w = world(tail_check=tail)
    [v] = reap.plan(w.rows(), w.table(), w.now(), reap.DEFAULT_IDLE_SECONDS, w.listeners(),
                    w.tail_check)
    assert v.verdict == reap.CLOSE, v.why
    a = reap.act(v, reap.DEFAULT_IDLE_SECONDS, w)
    assert (a.outcome, a.why) == (reap.SKIPPED, "turned BUSY (turn) before the kill")
    assert pane in server.panes() and alive(data["pid"])


def test_a_listener_that_outlives_its_seat_is_named_and_left_alone(server, store, capsys):
    pane = server.seat(store["ST-1"], "p+st-1.worker.x", mode="listener", cleanup=False)
    data = server.sidecar(pane)
    rc, out, _ = run_reap(["--yes"], capsys)
    assert rc == 1, out
    assert pane not in server.panes() and gone_soon(data["pid"])
    assert "kill-pane sent, but not closed: survivors" in out
    for pid in data["kids"]:                         # named, and never killed
        assert alive(pid)
        assert f"{pid} (" in out
    line = (Path(os.environ["SESSION_STATE_DIR"]) / "reap.log").read_text()
    assert "\tfailed\t" in line


def test_an_ambiguous_short_exits_2_and_closes_nothing(server, store, capsys):
    a = server.seat(store["ST-1"], "p+st-1.worker.a")
    b = server.seat(store["ST-1"], "p+st-1.worker.b")
    ids = [server.sidecar(p)["sessionId"] for p in (a, b)]
    common = os.path.commonprefix(ids)
    assert common
    with pytest.raises(SystemExit) as e:
        reap.main(["--yes", common])
    assert e.value.code == 2
    assert {a, b} <= set(server.panes())


def _marked(marker: Path) -> int:
    t0 = time.monotonic()
    while not (marker.exists() and marker.read_text().strip()):
        assert time.monotonic() - t0 < 10, f"no {marker.name}"
        time.sleep(0.05)
    return int(marker.read_text())


def test_a_pane_that_holds_another_process_beside_the_seat_is_kept(server, store, capsys):
    """kill-pane hangs up every process in the pane, not just the seat. A
    launcher that also runs a job of its own (a hand-launched claude beside
    a dev server) is KEPT, and the bystander is never touched."""
    marker = store["ST-1"].parent / "bystander.pid"
    pane = server.window(server.launched(store["ST-1"], "p+st-1.worker.hand", extra="child",
                                         marker=marker))
    data = server.sidecar(pane)
    bystander = _marked(marker)
    try:
        rc, out, _ = run_reap(["--yes"], capsys)
        assert rc == 1, out
        assert pane in server.panes() and alive(data["pid"]) and alive(bystander)
        assert f"kept {data['sessionId'][:8]}" in out
        assert f"pane {pane} also holds {bystander} (sleep 301)" in out
    finally:
        os.kill(bystander, signal.SIGKILL)


def test_a_job_let_go_on_the_panes_terminal_is_never_killed(server, store, capsys):
    """A job let go (`sleep &` from a shell that exits) has ppid 1: in no
    process tree under the pane, but on its terminal, so kill-pane would take
    it. The seat is KEPT and the job lives (ST-146 do-002 review 2: at
    91f507f reap printed 'closed', exit 0, and the job was dead)."""
    marker = store["ST-1"].parent / "orphan.pid"
    pane = server.window(server.launched(store["ST-1"], "p+st-1.worker.hand", extra="letgo",
                                         marker=marker))
    data = server.sidecar(pane)
    orphan = _marked(marker)
    try:
        holder = next(pid for pid, p in _panes(server).items() if p == pane)
        assert orphan not in reap.descendants(holder, procs.table())   # in no tree under it
        rc, out, _ = run_reap(["--yes"], capsys)
        assert rc == 1, out
        assert pane in server.panes() and alive(data["pid"]) and alive(orphan)
        assert f"kept {data['sessionId'][:8]}" in out
        assert f"pane {pane} also holds {orphan} (sleep 302)" in out
    finally:
        os.kill(orphan, signal.SIGKILL)


def test_a_setsid_child_of_the_holder_is_kept_by_the_tree_half(server, store, capsys):
    """m10 (ST-146 do-004): a child of the holder in a session of its own is
    in the pane's TREE but not on its terminal, so only the tree half sees it.
    Pinned: KEEP, named, and it lives."""
    marker = store["ST-1"].parent / "setsid.pid"
    pane = server.window(server.launched(store["ST-1"], "p+st-1.worker.hand", extra="setsid",
                                         marker=marker))
    data = server.sidecar(pane)
    child = _marked(marker)
    try:
        holder = next(pid for pid, p in _panes(server).items() if p == pane)
        assert child in reap.descendants(holder, procs.table())
        assert child not in reap._pane_tty_pids(server.socket, pane)    # off its terminal
        rc, out, _ = run_reap(["--yes"], capsys)
        assert rc == 1, out
        assert pane in server.panes() and alive(data["pid"]) and alive(child)
        assert f"pane {pane} also holds {child} (sleep 304)" in out
    finally:
        os.kill(child, signal.SIGKILL)


def test_a_wrapper_between_the_pane_and_the_seat_is_kept_and_lives(server, store, capsys):
    """R1 (ST-146 do-004): `sh -c '<wrapper> <seat>; true'`. The wrapper has
    work of its own after the seat; it is not a launcher, so the seat is
    KEPT and the wrapper lives (at 2c6e183: 'closed', rc 0, wrapper dead)."""
    marker = store["ST-1"].parent / "wrapper.pid"
    wrapper = (f"import os, subprocess, sys; open({str(marker)!r}, 'w').write(str(os.getpid())); "
               "subprocess.run(sys.argv[1:])")
    seat = server.command(store["ST-1"], "p+st-1.worker.wrapped")
    pane = server.window(f"sh -c {shlex.quote(f'{sys.executable} -c {shlex.quote(wrapper)} {seat}; true')}")
    data = server.sidecar(pane)
    wrapped = _marked(marker)
    rc, out, _ = run_reap(["--yes"], capsys)
    assert rc == 1, out
    assert pane in server.panes() and alive(data["pid"]) and alive(wrapped)
    assert f"kept {data['sessionId'][:8]}" in out
    assert f"{wrapped} (" in out and "is not a launcher" in out


def test_a_seat_under_its_launcher_is_closed_and_the_launcher_goes_too(server, store, capsys):
    """The allowed shape (seen live: claude <- tm task work): CLOSE, and verify
    watches the line, so the launcher is gone as well."""
    pane = server.window(server.launched(store["ST-1"], "p+st-1.worker.tm"))
    data = server.sidecar(pane)
    holder = next(pid for pid, p in _panes(server).items() if p == pane)
    assert holder != data["pid"] and reap.is_launcher(procs.table(commands=True)[holder].command,
                                                      reap.installed_launchers())
    rc, out, _ = run_reap(["--yes"], capsys)
    assert rc == 0, out
    assert f"closed {data['sessionId'][:8]}" in out
    assert pane not in server.panes() and gone_soon(data["pid"]) and gone_soon(holder)


def test_the_last_pane_on_a_server_is_kept_and_its_job_completes(server, store, capsys):
    """R2 (ST-146 do-004): closing a server's last pane ends the server,
    which SIGTERMs its `run-shell -b` jobs. The seat is KEPT, and the job
    runs to its end."""
    pane = server.seat(store["ST-1"], "p+st-1.worker.alone")
    data = server.sidecar(pane)
    assert server.tmux("kill-pane", "-t", server.bystander).returncode == 0
    assert list(server.panes()) == [pane]
    done = store["ST-1"].parent / "job.done"
    server.tmux("run-shell", "-b", f"sleep 1; echo done > {done}")
    rc, out, _ = run_reap(["--yes"], capsys)
    assert rc == 1, out
    assert f"pane {pane} is the last on {server.socket}" in out
    assert pane in server.panes() and alive(data["pid"])
    t0 = time.monotonic()
    while not done.exists():
        assert time.monotonic() - t0 < 10, "the run-shell job never completed"
        time.sleep(0.05)


def test_a_process_merely_called_tm_is_not_the_launcher(server, store, tmp_path, capsys):
    """The review of do-004: a script CALLED `tm` and run as `tm task work`,
    but not the `tm` installed on PATH, with work of its own after the seat.
    KEEP, named, and it lives (at ed19b31: 'closed', rc 0, the rest never
    ran)."""
    impostor = tmp_path / "elsewhere" / "tm"
    impostor.parent.mkdir()
    impostor.write_text((server.bin / "tm").read_text())
    impostor.chmod(0o755)
    marker = store["ST-1"].parent / "impostor.pid"
    command = server.launched(store["ST-1"], "p+st-1.worker.impostor", extra="after",
                              marker=marker).replace(f"{server.bin}/tm", str(impostor))
    pane = server.window(command)
    data = server.sidecar(pane)
    fake = _marked(marker)
    rc, out, _ = run_reap(["--yes"], capsys)
    assert rc == 1, out
    assert pane in server.panes() and alive(data["pid"]) and alive(fake)
    assert f"kept {data['sessionId'][:8]}" in out
    assert f"pane held by {fake} (" in out and "not a launcher" in out
