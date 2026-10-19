#!/usr/bin/env python3
"""Measure `session list --json`: forks per run and where the time goes.

    python3 scripts/measure_list.py                       # the default matrix
    python3 scripts/measure_list.py --panes 16 --sockets 2 --dead 200 --runs 3
    python3 scripts/measure_list.py --matrix --json out.json

It never touches the live tmux server. Every run builds a
PRIVATE world under a fresh short directory in /tmp and points the code at it:

- TMUX_TMPDIR is that directory, so `tmux.sockets()` reads only its sockets.
  TMUX and TMUX_PANE are stripped. The live scratch servers are
  `tmux -L st111-scratch`, `-L st111-scratch-2`, ..., each with its share of
  the N fixture panes (one `sleep` per pane). DEAD socket files (bound, then
  closed, so the file stays and a connect is refused) stand in for the
  stale files a host collects: 1034 of them on one busy host (2030-08-11).
- SESSION_SIDECAR_DIR holds one sidecar per pane: its pid is the pane's pid,
  its procStart is that pid's `ps` start, so `live()` keeps every one.
- CLAUDE_PROJECTS_DIR holds one transcript per seat of --transcript-kb KB, so
  CONTEXT reads what a real seat costs (a busy host's median was about 1 MB).

Two measurements per world, both of the code under src/:

1. END TO END: `session list --json` as a child process, with a shim dir
   first on PATH. The shims for tmux, ps and git log their argv, then exec
   the real tool. Each run reports its fork count by tool, wall time, and
   the child's user and sys CPU (RUSAGE_CHILDREN). Every one of these runs
   COMPUTES (`SESSION_LIST_FRESH=1`); the snapshot lives in the world's own
   `state/`. Then the same number of SERVED runs read the snapshot the last
   one left: what a caller inside the freshness window pays.
2. BY PHASE, in-process: the same listing with timers wrapped round each
   phase (ps table, sockets, panes per socket, sidecar reads, liveness, repo
   names, tmux locate, context, render) and subprocess.run counted by tool.

Teardown kills each scratch server BY its socket with TMUX* stripped, then
removes the directory, whatever happened. Nothing is installed or restarted.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import socket as _socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRATCH = "st111-scratch"
SHIMMED = ("tmux", "ps", "git")
#: (panes, live sockets, dead socket files) - kept small: the host is loaded.
MATRIX = ((4, 1, 0), (16, 1, 0), (16, 2, 0), (32, 2, 0),
          (16, 2, 100), (16, 2, 300), (16, 2, 1034))


def scratch_names(count: int) -> list[str]:
    return [SCRATCH if i == 0 else f"{SCRATCH}-{i + 1}" for i in range(count)]


def walled_env(world: Path) -> dict[str, str]:
    """The environment every scratch call runs in: no route to the live
    server (TMUX* stripped, TMUX_TMPDIR private)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TMUX")}
    env["TMUX_TMPDIR"] = str(world)
    return env


def real_tmux() -> str:
    found = shutil.which("tmux")
    if not found:
        sys.exit("measure_list: no tmux on PATH")
    return found


def tmux(world: Path, name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([real_tmux(), "-L", name, *args], env=walled_env(world),
                          capture_output=True, text=True, timeout=10, check=False)


def start_servers(world: Path, names: list[str], panes: int) -> None:
    """Spread PANES over the servers, one window per pane, each a sleep."""
    share = [panes // len(names) + (1 if i < panes % len(names) else 0)
             for i in range(len(names))]
    for name, count in zip(names, share, strict=True):
        if count == 0:
            continue
        out = tmux(world, name, "new-session", "-d", "-s", "fx", "sleep 900")
        if out.returncode != 0:
            raise RuntimeError(f"scratch server {name}: {out.stderr.strip()}")
        for _ in range(count - 1):
            tmux(world, name, "new-window", "-d", "-t", "fx", "sleep 900")


def plant_dead(world: Path, count: int) -> None:
    """COUNT socket files no server answers: bound, then closed."""
    base = world / f"tmux-{os.getuid()}"
    base.mkdir(mode=0o700, exist_ok=True)
    for i in range(count):
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.bind(str(base / f"dead-{i:04d}"))
        s.close()


def pane_pids(world: Path, names: list[str]) -> list[tuple[str, int]]:
    found = []
    for name in names:
        out = tmux(world, name, "list-panes", "-a", "-F", "#{pane_pid}")
        found += [(name, int(p)) for p in out.stdout.split() if p.isdigit()]
    return found


def lstart(pids: list[int]) -> dict[int, str]:
    """pid -> its start as UTC ctime, the form a sidecar's procStart has."""
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    out = subprocess.run(["ps", "-o", "pid=,lstart=", "-p", ",".join(map(str, pids))],
                         capture_output=True, text=True, env=env, check=False)
    found = {}
    for line in out.stdout.splitlines():
        pid, _, start = line.strip().partition(" ")
        if pid.isdigit():
            found[int(pid)] = " ".join(start.split())
    return found


def encode_dir(path: str) -> str:
    for ch in "/._":
        path = path.replace(ch, "-")
    return path


def transcript(path: Path, kb: int) -> None:
    """About KB kilobytes of transcript: assistant turns with usage rows."""
    line = json.dumps({"type": "assistant", "message": {
        "model": "claude-opus-4-1", "role": "assistant",
        "content": [{"type": "text", "text": "x" * 600}],
        "usage": {"input_tokens": 12, "cache_creation_input_tokens": 400,
                  "cache_read_input_tokens": 90000, "output_tokens": 300}}}) + "\n"
    with open(path, "w") as f:
        f.write(line * max(1, kb * 1024 // len(line)))


def write_seats(world: Path, panes: list[tuple[str, int]], cwds: int, kb: int) -> None:
    sidecars, projects = world / "sidecars", world / "projects"
    sidecars.mkdir()
    projects.mkdir()
    starts = lstart([pid for _, pid in panes])
    now_ms = int(time.time() * 1000)
    for i, (_, pid) in enumerate(panes):
        cwd = world / "cwd" / f"repo-{i % cwds}"
        cwd.mkdir(parents=True, exist_ok=True)
        sid = f"{i:08x}-0000-4000-8000-{pid:012d}"
        (sidecars / f"{sid}.json").write_text(json.dumps({
            "sessionId": sid, "pid": pid, "cwd": str(cwd), "procStart": starts.get(pid),
            "status": "busy" if i % 3 == 0 else "idle", "updatedAt": now_ms - i * 1000,
            "startedAt": now_ms - 3_600_000, "name": f"seat-{i}", "version": "fixture"}))
        folder = projects / encode_dir(str(cwd))
        folder.mkdir(exist_ok=True)
        transcript(folder / f"{sid}.jsonl", kb)


def write_shims(world: Path) -> Path:
    """tmux, ps, git: log `<tool> <argv>` to $ST111_FORKLOG, exec the real one."""
    shims = world / "shim"
    shims.mkdir(exist_ok=True)
    for tool in SHIMMED:
        real = shutil.which(tool)
        if not real:
            continue
        shim = shims / tool
        shim.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "{tool} $*" >> "$ST111_FORKLOG"\n'
                        f'exec {real} "$@"\n')
        shim.chmod(0o755)
    return shims


def seat_env(world: Path) -> dict[str, str]:
    env = walled_env(world)
    env.update(SESSION_SIDECAR_DIR=str(world / "sidecars"),
               CLAUDE_PROJECTS_DIR=str(world / "projects"),
               PYTHONPATH=str(SRC), PYTHONDONTWRITEBYTECODE="1",
               # The snapshot lives in the world, never
               # the real state dir, and every timed run COMPUTES (--fresh's
               # variable), so the numbers stay the cost of one computation.
               SESSION_STATE_DIR=str(world / "state"), SESSION_LIST_FRESH="1")
    env.pop("CONTEXT_MAX", None)
    env.pop("SESSION_LIST_MAX_AGE", None)
    return env


def end_to_end(world: Path, runs: int, served: bool = False) -> list[dict]:
    """`session list --json` as a child, shims first on PATH; per run: forks
    by tool, wall seconds, the child's user and sys CPU, rows returned.
    SERVED: the runs read the snapshot the computing runs left (no FRESH,
    a long max age), so they show what a caller inside the window pays."""
    shims = write_shims(world)
    log = world / "forks.log"
    env = seat_env(world)
    env.update(PATH=f"{shims}{os.pathsep}{env.get('PATH', '')}", ST111_FORKLOG=str(log))
    if served:
        env.pop("SESSION_LIST_FRESH")
        env["SESSION_LIST_MAX_AGE"] = "3600"
    code = "import sys; from sessiontools.cli import main; sys.exit(main(['list', '--json']))"
    results = []
    for _ in range(runs):
        log.write_text("")
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.perf_counter()
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, timeout=600, check=False)
        wall = time.perf_counter() - t0
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        lines = log.read_text().splitlines()
        forks = {tool: sum(1 for x in lines if x.split(" ", 1)[0] == tool) for tool in SHIMMED}
        try:
            listed = json.loads(out.stdout)
        except ValueError:
            raise RuntimeError(f"session list failed: {out.stderr.strip()[-400:]}") from None
        results.append({"wall_s": round(wall, 3), "user_s": round(after.ru_utime - before.ru_utime, 3),
                        "sys_s": round(after.ru_stime - before.ru_stime, 3), "forks": forks,
                        "forks_total": sum(forks.values()), "rows": len(listed),
                        "rows_with_pane": sum(1 for r in listed if r.get("tmux_pane")),
                        "rows_with_context": sum(1 for r in listed if r.get("context_used") is not None)})
    return results


def by_phase(world: Path, runs: int) -> list[dict]:
    """The in-process profile, run in a child so the env and imports are clean."""
    env = seat_env(world)
    out = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child-profile",
                          "--runs", str(runs)], env=env, capture_output=True, text=True,
                         timeout=600, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"profile failed: {out.stderr.strip()[-400:]}")
    return json.loads(out.stdout)


def child_profile(runs: int) -> None:
    """Time each phase of `session list --json` in THIS process (the env is
    the world's). Prints a JSON list, one dict per run."""
    sys.path.insert(0, str(SRC))
    from sessiontools import claude_code, listing, procs, window
    from sessiontools import tmux as tmx

    spent: dict[str, float] = {}
    calls: dict[str, int] = {}
    forks: dict[str, int] = {}

    def timed(phase, fn):
        def wrapper(*a, **k):
            t0 = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                spent[phase] = spent.get(phase, 0.0) + time.perf_counter() - t0
                calls[phase] = calls.get(phase, 0) + 1
        return wrapper

    real_run = subprocess.run

    def counting_run(command, *a, **k):
        tool = os.path.basename(command[0]) if isinstance(command, list | tuple) else "?"
        forks[tool] = forks.get(tool, 0) + 1
        return real_run(command, *a, **k)

    subprocess.run = counting_run
    # Order matters: wrap the inner functions where their callers look them up.
    tmx.sockets = timed("tmux.sockets (dir scan)", tmx.sockets)
    tmx.panes = timed("tmux.panes (list-panes per socket)", tmx.panes)
    procs.table = timed("procs.table (ps)", procs.table)
    claude_code.sessions = timed("sidecar reads (sessions)", claude_code.sessions)
    raw_repo = listing.repo_name
    listing.repo_name = timed("repo_name (git rev-parse)", raw_repo)
    listing._tmux.locate = timed("tmux.locate", tmx.locate)
    listing.live = timed("live() incl. sidecar reads", listing.live)
    listing._window.of = timed("context (transcript scan)", window.of)
    results = []
    for _ in range(runs):
        spent.clear()
        calls.clear()
        forks.clear()
        raw_repo.cache_clear()
        t0 = time.perf_counter()
        found = listing.rows()
        t1 = time.perf_counter()
        found = listing.with_context(found)
        t2 = time.perf_counter()
        listing.render_json(found)
        t3 = time.perf_counter()
        phases = {k: round(v, 4) for k, v in spent.items()}
        phases["rows() total"] = round(t1 - t0, 4)
        phases["with_context() total"] = round(t2 - t1, 4)
        phases["render_json"] = round(t3 - t2, 4)
        results.append({"phases_s": phases, "calls": dict(calls), "forks": dict(forks),
                        "total_s": round(t3 - t0, 4), "rows": len(found)})
    print(json.dumps(results))


def kill_world(world: Path, names: list[str]) -> None:
    for name in names:
        try:
            tmux(world, name, "kill-server")
        except (OSError, subprocess.SubprocessError):
            pass
    shutil.rmtree(world, ignore_errors=True)


def measure(panes: int, live: int, dead: int, runs: int, cwds: int, kb: int) -> dict:
    world = Path(tempfile.mkdtemp(prefix="st111-", dir="/tmp"))
    names = scratch_names(live)
    try:
        start_servers(world, names, panes)
        plant_dead(world, dead)
        seats = pane_pids(world, names)
        write_seats(world, seats, cwds, kb)
        e2e = end_to_end(world, runs)
        served = end_to_end(world, runs, served=True)
        prof = by_phase(world, runs)
    finally:
        kill_world(world, names)

    def med(values):
        return round(statistics.median(values), 4)

    phase_keys = sorted({k for r in prof for k in r["phases_s"]})
    return {
        "panes": panes, "live_sockets": live, "dead_sockets": dead, "fixture_panes": len(seats),
        "runs": runs, "cwds": cwds, "transcript_kb": kb,
        "e2e": e2e,
        "e2e_median": {"wall_s": med([r["wall_s"] for r in e2e]),
                       "user_s": med([r["user_s"] for r in e2e]),
                       "sys_s": med([r["sys_s"] for r in e2e]),
                       "forks": e2e[-1]["forks"], "forks_total": e2e[-1]["forks_total"]},
        "e2e_served": served,
        "e2e_served_median": {"wall_s": med([r["wall_s"] for r in served]),
                              "user_s": med([r["user_s"] for r in served]),
                              "sys_s": med([r["sys_s"] for r in served]),
                              "forks_total": served[-1]["forks_total"]},
        "phase_median_s": {k: med([r["phases_s"].get(k, 0.0) for r in prof]) for k in phase_keys},
        "phase_calls": prof[-1]["calls"], "inproc_forks": prof[-1]["forks"],
        "profile": prof,
    }


def report(result: dict) -> str:
    m = result["e2e_median"]
    last = result["e2e"][-1]
    head = (f"panes={result['panes']} live={result['live_sockets']} dead={result['dead_sockets']}"
            f" | rows {last['rows']} pane {last['rows_with_pane']} ctx {last['rows_with_context']}"
            f" | forks {m['forks_total']} {m['forks']} | wall {m['wall_s']}s"
            f" user {m['user_s']}s sys {m['sys_s']}s")
    sv = result["e2e_served_median"]
    head += (f"\n    served from the snapshot: forks {sv['forks_total']} | wall {sv['wall_s']}s"
             f" user {sv['user_s']}s sys {sv['sys_s']}s")
    phases = "  ".join(f"{k}={v}" for k, v in result["phase_median_s"].items())
    return f"{head}\n    {phases}\n    calls {result['phase_calls']}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--panes", type=int, default=16, help="fixture panes (seats) in all")
    p.add_argument("--sockets", type=int, default=2, help="live scratch servers")
    p.add_argument("--dead", type=int, default=0, help="dead socket files planted")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--cwds", type=int, default=4, help="distinct seat cwds (git forks are per cwd)")
    p.add_argument("--transcript-kb", type=int, default=1024, help="each seat's transcript size")
    p.add_argument("--matrix", action="store_true", help="run the default matrix instead")
    p.add_argument("--json", metavar="FILE", help="write every result here as JSON")
    p.add_argument("--child-profile", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args(argv)
    if a.child_profile:
        child_profile(a.runs)
        return 0
    shapes = MATRIX if a.matrix else ((a.panes, a.sockets, a.dead),)
    results = []
    for panes, live, dead in shapes:
        result = measure(panes, live, dead, a.runs, a.cwds, a.transcript_kb)
        print(report(result), flush=True)
        results.append(result)
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
