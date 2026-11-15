"""ST-301 repro: a named agent's own launch writes `Seat: none` into its
identity block, while `session list` shows it holding that agent's seat.

`session respawn --go` replays the seat's recorded line, `session start
<agent>`, with no `--seat` and no `-n`. That launch passes the front door
(`start.door_start`), which writes the agent's claim, `holders/<agent>.json`.
`session list` reads that claim and shows the seat (`listing.seats`). But
the identity block's Seat comes from `plan.launch_seat`, which knows only
`--seat` and the default seat word. Then `boot_identity.gather` falls back
to `TM_AGENT_ADDRESS`, which a respawn does not hand down. So the block
says `none (no seat)`.

CLAUDE_CODE_AGENT does not help the writer. The launch script unsets it
(`environment.SESSION_IDENTITY_VARS`), and the vendor sets it from `--agent`
only after the block has been written.

Hermetic: a temp HOME, state dir, sidecar dir and sessions root, so no live
seat or claim can answer. A stub `claude` on PATH records what the vendor
would get.

THE FIX (fix-001): the block's Seat follows the claim this launch took,
the same claim `session list` reads. `start.main` hands the seat its
`SeatStart` claimed to `plan.assemble`, and `plan.launch_seat` orders it:
`--seat`, else that claimed seat, else the default seat word, else (in the
pane) the address handed down. A `-n` spawn takes no claim, so it stays
`none`. `boot_identity.seat_of` (Seat-mates) reads the same claim.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from start_support import launch_e2e

from sessiontools import procs
from sessiontools.start import boot_identity as bi
from sessiontools.start import default_seat, holders
from sessiontools.start.plan import launch_seat

AGENT = "scout"

#: Records the identity block as it stands when the vendor starts, the
#: vendor's argv, and whether CLAUDE_CODE_AGENT reached the vendor's env.
PROBE_STUB = """#!/usr/bin/env bash
printf '%%s\\0' "$@" > "%(dir)s/argv"
printf '%%s' "${CLAUDE_CODE_AGENT-<unset>}" > "%(dir)s/agent-env"
cat "$SESSIONS_DIR/$CLAUDE_SESSION_ID/identity.md" > "%(dir)s/identity-before-vendor"
"""


def _respawned_launch(tmp_path: Path, *flags: str, before: tuple[str, ...] = ()):
    """`session start scout` the way a respawn replays it: an agent named,
    no `--seat`, no `-n`, and no TM_AGENT_ADDRESS or CLAUDE_CODE_AGENT from
    the caller. FLAGS go after the agent (the vendor's), BEFORE ahead of it
(the launcher's)."""
    home = tmp_path / "home"
    home.mkdir()
    sessions = tmp_path / "minted"
    state = tmp_path / "state"
    env = {"HOME": str(home), "SESSIONS_DIR": str(sessions),
           "SESSION_SIDECAR_DIR": str(tmp_path / "sidecars"),
           "SESSION_STATE_DIR": str(state)}
    result, record, _work = launch_e2e(
        tmp_path, *before, AGENT, *flags, extra_env=env, stub_body=PROBE_STUB,
        drop_env=("TM_AGENT_ADDRESS", "CLAUDE_CODE_AGENT"))
    assert result.returncode == 0, result.stderr
    [session] = list(sessions.iterdir())
    return session, record, env


def test_the_launch_claims_the_agents_seat_by_the_rule_session_list_reads(tmp_path):
    """The other half of the disagreement, and green on master: the front
    door wrote `holders/scout.json`, naming this launch's session. That
    claim is what `session list` shows as the seat."""
    session, _record, env = _respawned_launch(tmp_path)
    claim = holders.read_claim(AGENT, env)
    assert claim is not None, "the front door took no claim"
    assert claim.get("sessionId") == session.name


def test_the_writer_never_sees_claude_code_agent(tmp_path):
    """Green on master, and it pins the measurement behind the fix: when the
    vendor starts, CLAUDE_CODE_AGENT is unset (the script unsets it), and
    the agent reaches the vendor only as `--agent`. A writer that reads
    CLAUDE_CODE_AGENT before the vendor runs would read nothing."""
    _session, record, _env = _respawned_launch(tmp_path)
    assert (record / "agent-env").read_text() == "<unset>"
    argv = (record / "argv").read_text().split("\0")
    assert "--agent" in argv and argv[argv.index("--agent") + 1] == AGENT


def test_a_named_agents_own_launch_says_its_seat(tmp_path):
    """THE REPRO. The block, as the agent reads it at startup, names the
    seat the launch just claimed, the same seat `session list` shows."""
    session, record, _env = _respawned_launch(tmp_path)
    block = (session / bi.IDENTITY_FILE).read_text()
    assert block == (record / "identity-before-vendor").read_text()
    assert f"- **Seat**: {AGENT}\n" in block, block


# ---------- the fix's edges ----------------------------------------------------------

def test_a_spawners_n_launch_takes_no_claim_and_stays_none(tmp_path):
    """A `-n` launch passes no front door and writes no claim, so `session
    list` shows `-` and the block says none: the two still agree."""
    session, _record, env = _respawned_launch(tmp_path, "-n", "scout+x")
    assert holders.read_claim(AGENT, env) is None
    block = (session / bi.IDENTITY_FILE).read_text()
    assert "- **Seat**: none (no seat)\n" in block, block


def test_seat_typed_on_the_line_wins_over_the_claim(tmp_path):
    """`--seat` names the seat outright; the front-door claim does not
    override it."""
    session, _record, _env = _respawned_launch(tmp_path, before=("--seat", "other+super"))
    block = (session / bi.IDENTITY_FILE).read_text()
    assert "- **Seat**: other+super\n" in block, block


def _launch(agent: str, seat: str | None = None):
    return SimpleNamespace(args=SimpleNamespace(seat=seat), agent=SimpleNamespace(name=agent))


def test_launch_seat_orders_seat_then_claim_then_the_default_word():
    word = default_seat.word()
    assert launch_seat(_launch(AGENT, seat="typed"), claimed=AGENT) == "typed"
    assert launch_seat(_launch(AGENT), claimed=AGENT) == AGENT
    assert launch_seat(_launch(AGENT)) is None
    assert launch_seat(_launch(word)) == word
    assert launch_seat(_launch(word), claimed=word) == word


# ---------- Seat-mates: seat_of reads the same claim ----------------------------------

SID = "3f9c2a1e-0000-4000-8000-000000000001"
MATE_SID = "bbbbbbbb-0000-4000-8000-000000000003"
OTHER_SID = "cccccccc-0000-4000-8000-000000000004"
STARTED = 1_790_000_000.0
SELF, MATE = 4242, 6161


def _env(tmp_path: Path) -> dict:
    return {"SESSION_SIDECAR_DIR": str(tmp_path / "sidecars"),
            "SESSIONS_DIR": str(tmp_path / "sessions"),
            "SESSION_STATE_DIR": str(tmp_path / "state")}


def _proc_start() -> str:
    return time.strftime(procs.CTIME, time.gmtime(STARTED))


def _born_live(env: dict, sid: str, pid: int, agent: str) -> None:
    settings = Path(env["SESSIONS_DIR"]) / sid / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "session.json").write_text(json.dumps({"agent": agent}))
    sidecars = Path(env["SESSION_SIDECAR_DIR"])
    sidecars.mkdir(parents=True, exist_ok=True)
    (sidecars / f"{pid}-{sid[:4]}.json").write_text(json.dumps({
        "pid": pid, "sessionId": sid, "status": "idle",
        "updatedAt": int(time.time() * 1000), "startedAt": int(STARTED * 1000),
        "procStart": _proc_start(), "cwd": "/nowhere", "agent": agent}))


def _claim(env: dict, sid: str | None, pid: int) -> None:
    holders.write_claim(AGENT, {"sessionId": sid, "pid": pid, "procStart": _proc_start(),
                                "pane": None, "via": default_seat.VIA_DOOR}, env)


def test_seat_of_names_the_front_door_holder(tmp_path):
    env = _env(tmp_path)
    _born_live(env, MATE_SID, MATE, AGENT)
    _claim(env, MATE_SID, MATE)
    assert bi.seat_of(MATE_SID, env) == AGENT


def test_seat_of_a_second_session_of_the_agent_is_none(tmp_path):
    """Born as the agent, but the claim names another session: no seat,
    as `session list` shows `-` for it."""
    env = _env(tmp_path)
    _born_live(env, MATE_SID, MATE, AGENT)
    _claim(env, OTHER_SID, MATE + 1)
    assert bi.seat_of(MATE_SID, env) is None
    holders.claim_paths(AGENT, env)[1].unlink()
    assert bi.seat_of(MATE_SID, env) is None


def test_a_session_seated_by_hand_lists_the_front_door_holder_as_a_mate(tmp_path):
    """This session took the seat by `--seat scout`; the live session whose
    front-door claim holds `scout` is its seat-mate."""
    env = _env(tmp_path)
    _born_live(env, MATE_SID, MATE, AGENT)
    _claim(env, MATE_SID, MATE)
    table = {pid: procs.Proc(pid, 1, STARTED) for pid in (1, SELF, MATE)}
    assert [m.session_id for m in bi.seat_mates(AGENT, SID, table, env)] == [MATE_SID]
