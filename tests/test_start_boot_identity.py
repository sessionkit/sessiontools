"""The identity block (ST-272 do-001): who a launched session is, written
once in the pane before the vendor starts, to `<session dir>/identity.md`.

Hermetic: the process table is a fake (pid -> Proc), the sidecars, the
sessions root and the state dir (the seat's claim) are this test's own. The
e2e test runs `session start` against a stub `claude` on PATH.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import time
from pathlib import Path

import pytest
from start_support import launch_e2e, plan_for, write_key_init_stub

from sessiontools import procs
from sessiontools.start import boot_identity as bi
from sessiontools.start import default_seat, holders
from sessiontools.start.args import parse_args
from sessiontools.start.plan import prepare

SID = "3f9c2a1e-0000-4000-8000-000000000001"
HOLDER_SID = "aaaaaaaa-0000-4000-8000-000000000002"
MATE_SID = "bbbbbbbb-0000-4000-8000-000000000003"
STARTED = 1_790_000_000.0
SELF, HOLDER, MATE, LAUNCHER = 4242, 5151, 6161, 7171
SEAT = "sessiontools+st-272.foreman"


def proc_start(at: float = STARTED) -> str:
    return time.strftime(procs.CTIME, time.gmtime(at))


def table(*pids: int) -> dict:
    return {pid: procs.Proc(pid, 1, STARTED) for pid in (1, *pids)}


@pytest.fixture
def env(tmp_path) -> dict:
    return {"SESSION_SIDECAR_DIR": str(tmp_path / "sidecars"),
            "SESSIONS_DIR": str(tmp_path / "sessions"),
            "SESSION_STATE_DIR": str(tmp_path / "state")}


def claim(env: dict, seat: str, pid: int, sid: str | None = None, pane: str | None = None):
    holders.write_claim(seat, {"sessionId": sid, "pid": pid, "procStart": proc_start(),
                               "pane": pane, "via": default_seat.VIA_TMUX}, env)


def sidecar(env: dict, sid: str, pid: int, tmux: str | None = None,
            agent: str | None = None) -> None:
    directory = Path(env["SESSION_SIDECAR_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid, "sessionId": sid, "status": "idle",
            "updatedAt": int(time.time() * 1000), "startedAt": int(STARTED * 1000),
            "procStart": proc_start(), "cwd": "/nowhere"}
    if tmux:
        data["tmux"] = tmux
    if agent:
        data["agent"] = agent
    (directory / f"{pid}-{sid[:4]}.json").write_text(json.dumps(data))


def born(env: dict, sid: str, agent: str | None = None, seat: str | None = None) -> None:
    settings = Path(env["SESSIONS_DIR"]) / sid / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    data = {k: v for k, v in (("agent", agent), ("seat", seat)) if v}
    (settings / "session.json").write_text(json.dumps(data))


def block(seat: str, nickname: str, pane: str, holder: str, mates: str, key: str) -> str:
    return (
        "# Who you are (at launch)\n"
        "\n"
        f"- **Session ID**: {SID}\n"
        f"- **Seat**: {seat}\n"
        f"- **Nickname**: {nickname}\n"
        f"- **Pane**: {pane}\n"
        f"- **Call holder** (at launch): {holder}\n"
        f"- **Seat-mates** (at launch): {mates}\n"
        f"- **Key**: {key}\n"
        "\n"
        "Seat-mates and call holder change after launch: re-check with `session list`.\n")


# ---------- the four pinned renders ------------------------------------------------

def test_a_plain_session_says_what_it_has_not_got(env):
    """No seat, no name, not in tmux: every line says so, nothing guessed,
    and no process table is read for a seat that is not there."""
    def no_table():
        raise AssertionError("a plain session reads no process table")
    facts = bi.gather(SID, None, None, keyed=False, self_pids=[SELF], environ=env,
                      table=no_table)
    assert bi.render(facts) == block(
        "none (no seat)", "none (no name given)", "none (not in tmux)",
        "n/a (no seat)", "n/a (no seat)",
        "unknown (signing-key default pending a decision)")


def test_the_seat_holder_says_yes(env):
    word = default_seat.word()
    claim(env, word, SELF, None, "%271")
    facts = bi.gather(SID, word, f"{word}.3f9c2a1e", keyed=False, self_pids=[SELF],
                      environ={**env, "TMUX_PANE": "%271"}, table=lambda: table(SELF))
    assert bi.render(facts) == block(
        word, f"{word}.3f9c2a1e", "%271", "yes", "none",
        "unknown (signing-key default pending a decision)")


def test_a_second_session_on_a_held_seat_names_the_holder_and_its_mates(env):
    word = default_seat.word()
    claim(env, word, HOLDER, HOLDER_SID, "%12")
    sidecar(env, HOLDER_SID, HOLDER, tmux="s:@1.%12", agent=word)
    born(env, HOLDER_SID, agent=word)
    # Live, but on another seat: not a mate.
    sidecar(env, MATE_SID, MATE, tmux="s:@2.%13", agent="scout")
    born(env, MATE_SID, agent="scout", seat="other+super")
    facts = bi.gather(SID, word, None, keyed=False, self_pids=[SELF],
                      environ={**env, "TMUX_PANE": "%40"},
                      table=lambda: table(SELF, HOLDER, MATE))
    assert bi.render(facts) == block(
        word, "none (no name given)", "%40", "no (held by session aaaaaaaa)",
        "1: aaaaaaaa (%12)", "unknown (signing-key default pending a decision)")


def test_a_keyed_session_says_its_key_was_minted(env):
    """A keyed launch on a role seat nobody claims: the key line says yes,
    the holder line says why it cannot say, and a mate found by its birth
    record's seat is listed."""
    sidecar(env, MATE_SID, MATE)
    born(env, MATE_SID, agent="foreman", seat=SEAT)
    facts = bi.gather(SID, SEAT, f"{SEAT}.3f9c2a1e", keyed=True, self_pids=[SELF],
                      environ={**env, "TMUX_PANE": "%271"}, table=lambda: table(SELF, MATE))
    assert bi.render(facts) == block(
        SEAT, f"{SEAT}.3f9c2a1e", "%271", "unknown (no live claim names a holder)",
        "1: bbbbbbbb (no pane)", "yes (minted at launch)")


# ---------- the line rules -------------------------------------------------------------

def test_the_block_is_short_plain_text():
    facts = bi.Facts(SID, "s\x1b[31m", "n\nm", "%1", "yes", "none", False)
    text = bi.render(facts)
    assert len(text.splitlines()) <= bi.MAX_LINES
    assert "\x1b" not in text and "n\nm" not in text
    assert "- **Seat**: s?[31m\n" in text


def test_the_seat_falls_back_to_the_address_handed_down(env):
    facts = bi.gather(SID, None, None, keyed=False, environ={**env, "TM_AGENT_ADDRESS": SEAT},
                      table=lambda: table(SELF))
    assert facts.seat == SEAT


def test_a_mate_found_by_the_address_its_launch_handed_down(env):
    sidecar(env, MATE_SID, MATE)
    launch = Path(env["SESSIONS_DIR"]) / MATE_SID / "launch"
    launch.mkdir(parents=True)
    (launch / "env").write_text(f"HOME=/x\nTM_AGENT_ADDRESS={SEAT}\n")
    facts = bi.gather(SID, SEAT, None, keyed=False, environ=env, table=lambda: table(MATE))
    assert facts.mates == "1: bbbbbbbb (no pane)"


def test_a_dead_sidecar_is_no_mate_and_a_dead_claim_holds_nothing(env):
    word = default_seat.word()
    claim(env, word, HOLDER, HOLDER_SID)
    sidecar(env, HOLDER_SID, HOLDER, agent=word)
    born(env, HOLDER_SID, agent=word)
    facts = bi.gather(SID, word, None, keyed=False, self_pids=[SELF], environ=env,
                      table=lambda: table(SELF))
    assert facts.holder == "unknown (no live claim names a holder)"
    assert facts.mates == "none"


def test_a_process_table_that_fails_is_said_not_guessed(env):
    def broken():
        raise OSError("no ps")
    facts = bi.gather(SID, SEAT, None, keyed=False, environ=env, table=broken)
    assert facts.holder == facts.mates == "unknown (the process table could not be read (no ps))"


def test_a_holder_with_no_id_yet_is_named_by_pid():
    holder = default_seat.Holder(None, HOLDER, "%12", None, "id minted in the pane")
    assert (bi.holder_value("claude", holder, mine=False)
            == f"no (held by a session whose id minted in the pane, pid {HOLDER})")


# ---------- the tmux path: the claim moves from the launcher to the pane ----------

def test_the_writer_waits_for_the_launcher_to_repoint_the_claim(env):
    """The launcher (alive: this test's pid) still holds the claim when the
    pane's writer runs; it moves to the pane, and the writer says yes."""
    word = default_seat.word()
    launcher = os.getpid()
    claim(env, word, launcher)
    sleeps = []

    def sleep(_s):
        sleeps.append(_s)
        claim(env, word, SELF, None, "%271")     # the launcher's re-point lands

    facts = bi.gather(SID, word, None, keyed=False, self_pids=[SELF], launcher_pid=launcher,
                      environ=env, table=lambda: table(SELF), sleep=sleep)
    assert sleeps and facts.holder == "yes"


def test_a_repoint_that_never_lands_is_unknown_not_a_guess(env):
    word = default_seat.word()
    launcher = os.getpid()
    claim(env, word, launcher)
    now = [0.0]

    def clock():
        now[0] += 1.0
        return now[0]

    facts = bi.gather(SID, word, None, keyed=False, self_pids=[SELF], launcher_pid=launcher,
                      environ=env, table=lambda: table(SELF, launcher),
                      sleep=lambda _s: None, clock=clock)
    assert facts.holder == ("unknown (the seat's claim had not moved from the launcher to "
                            "this pane after 3s)")


# ---------- the write -----------------------------------------------------------------

def test_main_writes_the_block_0600_atomically(tmp_path, env):
    session = Path(env["SESSIONS_DIR"]) / SID
    session.mkdir(parents=True)
    assert bi.main(["--dir", str(session), "--name", "scout.3f9c2a1e", "--self-pid", "1"],
                   environ={**env, "CLAUDE_SESSION_ID": SID}) == 0
    path = session / bi.IDENTITY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().startswith("# Who you are (at launch)\n")
    assert "- **Nickname**: scout.3f9c2a1e\n" in path.read_text()
    assert [p.name for p in session.iterdir()] == [bi.IDENTITY_FILE]


def test_a_failed_write_never_stops_the_launch(tmp_path, capsys):
    assert bi.main(["--dir", str(tmp_path / "missing")], environ={}) == 0
    assert "identity block: not written" in capsys.readouterr().err


@pytest.mark.parametrize("argv,expected", [
    (["claude", "-n", "a+b"], "a+b"),
    (["claude", "-na+b"], "a+b"),
    (["claude", "--name", "a+b"], "a+b"),
    (["claude", "--name=a+b"], "a+b"),
    (["claude", "--effort", "high"], None),
    (["claude", "--", "-n x"], None),
])
def test_the_name_in_every_spelling(argv, expected):
    assert bi.name_token(argv) == expected


# ---------- the script line ---------------------------------------------------------

def _lines_of(plan):
    lines = plan.script.splitlines()
    record = next(i for i, line in enumerate(lines)
                  if "from sessiontools.start.record import main" in line)
    call = next(i for i, line in enumerate(lines) if bi.MODULE in line)
    vendor = next(i for i, line in enumerate(lines)
                  if line.startswith(("exec ", shlex.quote(plan.argv[0]))))
    return lines, record, call, vendor


def test_the_script_writes_the_block_after_the_record_and_before_the_vendor(tmp_path):
    plan = plan_for(tmp_path, "", argv_tail=["-n", "scout+{shortsid}"],
                    flags=("--seat", SEAT))
    lines, record, call, vendor = _lines_of(plan)
    assert record < call < vendor
    assert lines[call].endswith(" || :")
    assert f"--dir {shlex.quote(plan.identity.session_dir)} " in lines[call]
    assert f"--seat {shlex.quote(SEAT)}" in lines[call]
    assert f"--name={shlex.quote('scout+' + plan.identity.session_id[:8])}" in lines[call]
    assert '--self-pid "$$"' in lines[call] and "$PPID" not in lines[call]
    assert "--keyed" not in lines[call] and "--launcher-pid" not in lines[call]


def test_the_block_is_never_in_argv(tmp_path):
    plan = plan_for(tmp_path, "", argv_tail=["-n", "scout+{shortsid}"], flags=("--seat", SEAT))
    text = " ".join(str(a) for a in plan.argv)
    assert bi.IDENTITY_FILE not in text and "Who you are" not in text
    assert bi.MODULE not in text


def test_a_pane_mint_writes_into_the_dir_the_pane_mints(tmp_path):
    stub, _record = write_key_init_stub(tmp_path)
    plan = plan_for(tmp_path, "", argv_tail=["-n", "scout+{shortsid}"],
                    flags=("--in-tmux", "--keyed"), extra_env={"SESSION_KEY_INIT": stub})
    assert plan.pane_mint is not None
    lines, _record, call, vendor = _lines_of(plan)
    mint = next(i for i, line in enumerate(lines) if "session-key-init failed" in line)
    assert mint < call < vendor
    root = shlex.quote(plan.pane_mint.sessions_root)
    assert f'--dir {root}/"$CLAUDE_SESSION_ID" ' in lines[call]
    assert '--name="scout+${CLAUDE_SESSION_ID:0:8}"' in lines[call]
    assert "--keyed" in lines[call] and '--self-pid "$PPID"' in lines[call]


def test_a_default_seat_tmux_launch_hands_the_pane_its_launcher(tmp_path, monkeypatch):
    """Only a default-seat launch placed in tmux waits for its claim to move."""
    work = tmp_path / "work"
    work.mkdir()
    environ = {"AGENTS_DIR": str(tmp_path / "agents"), "PATH": os.environ.get("PATH", ""),
               "SESSIONS_DIR": str(tmp_path / "sessions"),
               "SESSION_KEY_INIT": os.environ["SESSION_KEY_INIT"]}
    plan = prepare(parse_args(["--dry-run", "--in-tmux", default_seat.word()]),
                   environ=environ, work_dir=str(work))
    call = next(line for line in plan.script.splitlines() if bi.MODULE in line)
    assert f"--seat {default_seat.word()}" in call
    assert f"--launcher-pid {os.getpid()}" in call


def test_a_resume_writes_no_block(tmp_path):
    plan = plan_for(tmp_path, "", argv_tail=["--resume", "0f0f0f0f-0000-4000-8000-000000000000"],
                    flags=("--dry-run",))
    assert bi.MODULE not in plan.script


# ---------- end to end: written in the pane, before the vendor --------------------

PROBE_STUB = """#!/usr/bin/env bash
printf '%%s\\0' "$@" > "%(dir)s/argv"
cat "$SESSIONS_DIR/$CLAUDE_SESSION_ID/identity.md" > "%(dir)s/identity-before-vendor"
"""


def test_a_live_launch_writes_the_block_before_the_vendor_starts(tmp_path):
    sessions = tmp_path / "minted"
    result, record, _work = launch_e2e(
        tmp_path, "scout", "-n", "scout+{shortsid}",
        extra_env={"SESSIONS_DIR": str(sessions),
                   "SESSION_SIDECAR_DIR": str(tmp_path / "sidecars"),
                   "SESSION_STATE_DIR": str(tmp_path / "state")},
        stub_body=PROBE_STUB)
    assert result.returncode == 0, result.stderr
    seen = (record / "identity-before-vendor").read_text()
    [session] = list(sessions.iterdir())
    path = session / bi.IDENTITY_FILE
    assert seen == path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert f"- **Session ID**: {session.name}\n" in seen
    assert f"- **Nickname**: scout+{session.name[:8]}\n" in seen
    assert "Who you are" not in "\0".join((record / "argv").read_text().split("\0"))
