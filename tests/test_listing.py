"""`session list`, `session get`, `session send` over a fake fleet: sidecars
under tmp_path, the process table and the tmux panes injected."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessiontools import get as get_mod
from sessiontools import listing, procs, send, tmux, window
from sessiontools.procs import Proc

START = "Wed Jan  2 17:00:00 2030"
T0 = procs.parse_ctime(START)
NOW = T0 + 3600
# The seats' cwd: short, absolute, and naming no directory. A transcript dir
# is encode_dir(cwd) - the WHOLE path flattened into ONE component - so a
# tmp_path cwd under a deep TMPDIR passes NAME_MAX (255 bytes) and mkdir fails
# ENAMETOOLONG (ST-119). A cwd that is gone is a case repo_name() documents:
# no repo, so the basename.
CWD = "/st119-fleet/seat"
REPO = Path(CWD).name


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """Three sidecars: bob (live, in pane %11 on default), alice (live, on
    server `work`), and a crash-left carol whose pid is gone."""
    monkeypatch.setenv("SESSION_SIDECAR_DIR", str(tmp_path))
    listing.repo_name.cache_clear()

    def car(pid, sid, agent, status, name):
        (tmp_path / f"{pid}.json").write_text(json.dumps({
            "pid": pid, "sessionId": sid, "agent": agent, "name": name, "status": status,
            "updatedAt": (NOW - 600) * 1000, "startedAt": T0 * 1000, "procStart": START,
            "cwd": CWD, "version": "2.1.283"}))
    car(100, "73f2f616-aaaa", "bob", "busy", "bob-f1")
    car(200, "a907dd33-bbbb", "alice", "shell", "alice-c5")
    car(300, "ff903591-cccc", "carol", "shell", "carol-8e")
    table = {100: Proc(100, 10, T0), 10: Proc(10, 1, T0),     # bob's claude under pane pid 10
             200: Proc(200, 20, T0), 20: Proc(20, 1, T0), 1: Proc(1, 0, T0)}
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(tmux, "pane_index", lambda: {10: ("default", "%11"), 20: ("work", "%4")})
    return tmp_path


def test_rows_are_the_live_sessions_with_their_panes(fleet):
    rows = listing.rows()
    assert [(r.short, r.tmux_socket, r.tmux_pane) for r in rows] == [
        ("73f2f616", "default", "%11"), ("a907dd33", "work", "%4")]


def test_json_carries_every_key_callers_read_and_nulls_what_is_not_ported(fleet):
    row = json.loads(listing.render_json(listing.rows(), now=NOW))[0]
    assert list(row) == ["session_id", "short", "pid", "name", "agent", "status", "state", "repo",
                         "project", "cwd", "version", "tmux_pane", "tmux_socket",
                         "runtime_s", "idle_s", "recap", "context_used", "context_peak",
                         "context_window", "context_pct", "context_model"]
    assert (row["agent"], row["status"], row["pid"], row["tmux_pane"]) == ("bob", "busy", 100, "%11")
    assert (row["runtime_s"], row["idle_s"]) == (3600, 600)
    assert row["repo"] == row["project"] == REPO
    assert all(row[k] is None for k in listing.NOT_PORTED_KEYS)


def test_the_table_parses_the_way_seat_hearing_reads_it(fleet):
    out = listing.render_table(listing.rows(), now=NOW)
    parsed = {ln.split()[0]: ln.split()[1] for ln in out.splitlines()[2:]}
    assert parsed == {"73f2f616": "bob-f1", "a907dd33": "alice-c5"}
    assert "1h00m" in out and "10m" in out


def test_context_keys_are_null_until_asked_for(fleet):
    row = json.loads(listing.render_json(listing.rows(), now=NOW))[0]
    assert all(row[k] is None for k in ("context_used", "context_peak", "context_window",
                                        "context_pct", "context_model"))


def test_with_context_fills_the_keys_and_the_column(fleet, monkeypatch):
    projects = fleet / "projects"
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    d = projects / window.encode_dir(CWD)
    d.mkdir(parents=True)
    (d / "73f2f616-aaaa.jsonl").write_text(json.dumps({"message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": 250_000}}}) + "\n")
    rows = listing.with_context(listing.rows())
    bob = json.loads(listing.render_json(rows, now=NOW))[0]
    assert (bob["context_used"], bob["context_window"], bob["context_pct"],
            bob["context_model"]) == (250_000, 1_000_000, 25, "claude-opus-5-5")
    out = listing.render_table(rows, now=NOW).splitlines()
    assert out[0].split()[-1] == "CONTEXT"
    assert [ln.split()[-1] for ln in out[2:]] == ["25%", "-"]      # alice has no transcript
    assert {ln.split()[0]: ln.split()[1] for ln in out[2:]} == {    # seat-hearing still parses
        "73f2f616": "bob-f1", "a907dd33": "alice-c5"}


@pytest.mark.parametrize("flags, pct", [([], 25), (["--no-context"], None)])
def test_session_list_fills_context_unless_told_not_to(fleet, monkeypatch, capsys, flags, pct):
    from sessiontools import cli
    projects = fleet / "projects"
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    d = projects / window.encode_dir(CWD)
    d.mkdir(parents=True)
    (d / "73f2f616-aaaa.jsonl").write_text(json.dumps({"message": {
        "model": "claude-opus-5-5", "usage": {"input_tokens": 250_000}}}) + "\n")
    assert cli.main(["list", "--json", *flags]) == 0
    assert json.loads(capsys.readouterr().out)[0]["context_pct"] == pct


def _transcript(fleet, monkeypatch, sid, cwd, *rows):
    projects = fleet / "projects"
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    d = projects / window.encode_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps({"message": m}) for m in rows) + "\n")


def _run(argv, capsys):
    from sessiontools import cli
    code = cli.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_session_context_reports_a_live_seat_like_the_old_tool(fleet, monkeypatch, capsys):
    _transcript(fleet, monkeypatch, "73f2f616-aaaa", CWD,
                {"model": "claude-opus-5-5", "usage": {"input_tokens": 400_000}},
                {"model": "claude-opus-5-5", "usage": {"input_tokens": 306_188}})
    assert _run(["context", "bob"], capsys)[:2] == (0, "Context: 306K / 1.00M (30%) · 693K free\n")
    assert _run(["context", "bob", "--peak"], capsys)[1].endswith("(40%) · 600K free [peak]\n")
    code, out, _ = _run(["context", "bob", "--json"], capsys)
    assert code == 0 and json.loads(out) == {"used": 306_188, "peak": 400_000, "max": 1_000_000, "pct": 30,
                               "free": 693_812, "session": "73f2f616-aaaa",
                               "model": "claude-opus-5-5", "band": None}   # ST-155: no threshold


SONNET_5_5 = "claude-sonnet-5-5"


def test_sonnet_5_5_has_a_window_in_context_and_in_list(fleet, monkeypatch, capsys):
    _transcript(fleet, monkeypatch, "73f2f616-aaaa", CWD,
                {"model": SONNET_5_5, "usage": {"input_tokens": 78_000}})
    code, out, _ = _run(["context", "bob"], capsys)
    assert (code, out) == (0, "Context: 78K / 1.00M (7%) · 922K free\n")
    assert json.loads(_run(["context", "bob", "--json"], capsys)[1])["max"] == 1_000_000
    code, out, _ = _run(["list", "--json"], capsys)
    bob = json.loads(out)[0]
    assert code == 0 and (bob["context_model"], bob["context_window"], bob["context_pct"]) == (
        SONNET_5_5, 1_000_000, 7)
    code, out, _ = _run(["list"], capsys)
    assert code == 0 and [ln.split()[-1] for ln in out.splitlines()[2:]] == ["7%", "-"]


def test_session_context_never_guesses_a_window(fleet, monkeypatch, capsys):
    _transcript(fleet, monkeypatch, "dead-1234", "/gone",
                {"model": "claude-unmapped", "usage": {"input_tokens": 5000}})
    code, out, _ = _run(["context", "dead-12"], capsys)        # not live: an id prefix
    assert code == 0 and out.startswith("Context: 5K / ? - no known window for claude-unmapped")
    assert _run(["context", "dead-12", "--max", "10000"], capsys)[1].startswith(
        "Context: 5K / 10K (50%)")                            # asserted, not guessed
    assert json.loads(_run(["context", "dead-12", "--json"], capsys)[1])["pct"] is None


def test_session_context_says_why_a_window_is_unknown(fleet, monkeypatch, capsys):
    _transcript(fleet, monkeypatch, "over-1", "/w",
                {"model": "claude-haiku-4-5", "usage": {"input_tokens": 250_000}})
    assert "over the 200K window mapped for claude-haiku-4-5" in _run(["context", "over-1"], capsys)[1]
    monkeypatch.setenv("CONTEXT_MAX", "lots")
    assert "$CONTEXT_MAX is set but is not a positive number" in _run(
        ["context", "over-1"], capsys)[1]


def test_session_context_synthetic_zero_tail_is_a_question_not_zero(fleet, monkeypatch, capsys):
    _transcript(fleet, monkeypatch, "tail-1", "/w",
                {"model": "claude-opus-5-5", "usage": {"input_tokens": 277_000}},
                {"model": "<synthetic>", "usage": {"input_tokens": 0}})
    out = _run(["context", "tail-1"], capsys)[1]
    assert out.startswith("Context: ? - the last reading is a <synthetic> zero; the peak was 277K")
    assert json.loads(_run(["context", "tail-1", "--json"], capsys)[1])["pct"] is None
    assert "(27%)" in _run(["context", "tail-1", "--peak"], capsys)[1]


@pytest.mark.parametrize("n", ["0", "-5"])
def test_session_context_refuses_a_max_that_is_not_positive(fleet, capsys, n):
    with pytest.raises(SystemExit) as e:
        _run(["context", "bob", "--max", n], capsys)
    assert e.value.code == 2 and "--max must be a positive number" in capsys.readouterr().err


def test_an_id_prefix_beats_a_name_that_contains_it():
    from sessiontools.claude_code import Session
    def row(sid, name):
        return listing.Row(Session(id=sid, harness="claude-code", busy=False, updated_at=0,
                                   name=name), repo="r", tmux_socket=None, tmux_pane=None,
                           started=None)
    bob50, other = row("09d75b79-aaaa", "bob-50"), row("50c009c5-bbbb", "alice")
    assert listing.match([bob50, other], "50") is bob50            # the get rule: name first
    assert listing.match_id_first([bob50, other], "50") is other   # context: the id first
    assert listing.match_id_first([bob50, other], "bob") is bob50  # then the name rule


def test_session_context_without_a_transcript_is_exit_1(fleet, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(fleet / "empty"))
    code, _, err = _run(["context", "nobody-at-all"], capsys)
    assert code == 1 and "no transcript" in err


def test_no_one_live_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_SIDECAR_DIR", str(tmp_path))
    assert listing.render_table(listing.rows()) == "No running sessions found."


@pytest.mark.parametrize("secs, text", [(45, "45s"), (3540, "59m"), (38940, "10h49m"), (123062, "1d10h")])
def test_durations(secs, text):
    assert listing.fmt_duration(secs) == text


def test_match_is_name_or_repo_or_id_prefix_first_wins(fleet):
    rows = listing.rows()
    assert listing.match(rows, "ALICE").short == "a907dd33"
    assert listing.match(rows, "73f2").short == "73f2f616"
    assert listing.match(rows, "f616") is None          # an id matches by PREFIX only
    assert listing.match(rows, REPO).short == "73f2f616"   # repo: first wins


def test_get_idle_is_zero_while_busy_and_json_names_the_seat(fleet, monkeypatch):
    monkeypatch.setattr("time.time", lambda: NOW)
    rows = listing.rows()
    assert get_mod.get(rows, "idle", "bob", False) == "0"
    assert get_mod.get(rows, "idle", "alice", False) == "600"
    assert json.loads(get_mod.get(rows, "idle", "alice", True)) == {
        "session_id": "a907dd33-bbbb", "name": "alice-c5", "idle": 600, "status": "shell",
        "state": "idle"}
    assert get_mod.get(rows, "pane", "bob", False) == "%11"


def test_get_model_reads_the_transcript(fleet, monkeypatch):
    projects = fleet / "projects"
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    d = projects / window.encode_dir(CWD)
    d.mkdir(parents=True)
    (d / "73f2f616-aaaa.jsonl").write_text("\n".join(json.dumps({"message": m}) for m in (
        {"model": "claude-opus-5-5", "usage": {"input_tokens": 9}},
        {"model": "<synthetic>", "usage": {"input_tokens": 0}})) + "\n")
    assert get_mod.get(listing.rows(), "model", "bob", False) == "claude-opus-5-5"


def test_get_without_a_seat_is_this_session(fleet):
    assert get_mod.get(listing.rows(), "agent", None, False,
                       env={"CLAUDE_CODE_SESSION_ID": "a907dd33-bbbb"}) == "alice"


@pytest.mark.parametrize("prop, seat, code", [("bogus", "bob", 1), ("idle", "nobody", 2),
                                             ("model", "bob", 2)])   # no transcript: no model
def test_get_refusals_keep_their_exit_codes(fleet, prop, seat, code):
    with pytest.raises(get_mod.GetError) as e:
        get_mod.get(listing.rows(), prop, seat, False)
    assert e.value.code == code


def test_send_types_literally_then_enters_on_the_right_server(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(send, "_keys", lambda sock, pane, *k: calls.append((sock, pane, k)) or 0)
    monkeypatch.setenv(send.DELAY_VAR, "0")
    assert send.send_keys("work", "%4", "check PR #26")
    assert calls == [("work", "%4", ("-l", "--", "check PR #26")), ("work", "%4", ("Enter",))]
    err = capsys.readouterr().err
    assert "pane-send: target=work:%4 text_rc=0 enter_rc=0" in err and "PR #26" not in err


def test_send_clears_a_stranded_line_when_enter_fails(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(send, "_keys", lambda s, p, *k: calls.append(k) or (1 if k == ("Enter",) else 0))
    monkeypatch.setenv(send.DELAY_VAR, "0")
    assert not send.send_keys("default", "%1", "rm -rf")
    assert calls[-1] == ("C-u",)
    assert "STRANDED-CLEARED" in capsys.readouterr().err


# ── review of 6aded2d: -v, dash text, the occupant check ──

@pytest.fixture
def sent(fleet, monkeypatch):
    """Capture what `session send` would type, and whether it checked."""
    from sessiontools import cli
    got = {"checked": [], "typed": []}
    monkeypatch.setattr(send, "still_belongs",
                        lambda sock, pane, pid: got["checked"].append((sock, pane, pid)) or True)
    monkeypatch.setattr(send, "send_keys",
                        lambda sock, pane, text: got["typed"].append((sock, pane, text)) or True)
    # `session send` is disabled (2030-12-08) but its code is
    # kept for bringing back; these tests drive the kept handler directly.
    got["run"] = lambda argv: cli._send(argv[1:])
    return got


def test_send_dash_v_sends_v_not_the_flag(sent):
    assert sent["run"](["send", "bob", "-v"]) == 0
    assert sent["typed"] == [("default", "%11", "v")]


@pytest.mark.parametrize("argv, text", [
    (["send", "alice", "-x", "then", "--help"], "-x then --help"),   # dashes are TEXT
    (["send", "alice", "check", "PR", "#26"], "check PR #26"),
    (["send", "alice", "-v", "loud"], "-v loud"),                      # only a LONE -v is the flag
])
def test_everything_after_name_is_text(sent, argv, text):
    assert sent["run"](argv) == 0
    assert sent["typed"] == [("work", "%4", text)]


def test_send_checks_the_occupant_before_typing(sent):
    sent["run"](["send", "alice", "hi"])
    assert sent["checked"] == [("work", "%4", 200)]


def test_send_refuses_a_recycled_pane(fleet, monkeypatch, capsys):
    from sessiontools import cli
    typed = []
    monkeypatch.setattr(send, "still_belongs", lambda *a: False)
    monkeypatch.setattr(send, "send_keys", lambda *a: typed.append(a) or True)
    assert cli._send(["alice", "hi"]) == 1
    assert typed == []
    assert "no longer belongs" in capsys.readouterr().err


def test_still_belongs_walks_from_the_session_to_the_pane_occupant(monkeypatch):
    table = {100: Proc(100, 10, T0), 10: Proc(10, 1, T0), 1: Proc(1, 0, T0)}
    monkeypatch.setattr(procs, "table", lambda: table)
    monkeypatch.setattr(tmux, "pane_pid", lambda sock, pane: 10)
    assert send.still_belongs("default", "%11", 100)
    monkeypatch.setattr(tmux, "pane_pid", lambda sock, pane: 999)    # respawned
    assert not send.still_belongs("default", "%11", 100)
    monkeypatch.setattr(tmux, "pane_pid", lambda sock, pane: None)   # pane gone
    assert not send.still_belongs("default", "%11", 100)
    assert not send.still_belongs("default", "%11", None)


def test_get_a_property_with_no_value_is_exit_2(fleet):
    import dataclasses
    rows = [dataclasses.replace(listing.rows()[0], tmux_pane=None)]
    with pytest.raises(get_mod.GetError) as e:
        get_mod.get(rows, "pane", "bob", False)
    assert e.value.code == 2 and "no pane known" in str(e.value)
