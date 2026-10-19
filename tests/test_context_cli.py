"""`session context` wired to its views (ST-26 do-005, design section 1).

The views themselves are `context_parts`' and are tested there; this pins
the verb: the bare one-liner is what it was and reads nothing new, every
option's help says what it does and gives an example, each view runs end to
end on a transcript found under `CLAUDE_PROJECTS_DIR`, and the flags that
make no sense together are refused (exit 2).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_context_parts_render import DATA, bare_numbers

from sessiontools import band as band_mod
from sessiontools import cli, listing, window
from sessiontools.claude_code import SESSION_ID_VARS
from sessiontools.context_parts import render

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
#: A fixture copied under a session id, so SEAT resolves it as an id prefix.
SID = "0f0f0f0f-1111-2222-3333-444455556666"
TOOLS = FIXTURES / "context_hooks" / "tools.jsonl"           # results, a compaction
HOOKS = FIXTURES / "context_hooks" / "hooks.jsonl"           # hook context, repeats
AGENT = FIXTURES / "context_boot" / "agent_pointer.jsonl"    # an agent, a pointer brief
VIEWS = ("--parts", "--startup", "--boot", "--hooks")
#: Each view's own first words: the label that says what the output is.
TITLES = {"--parts": "Context of 0f0f0f0f", "--startup": "Startup of 0f0f0f0f",
          "--boot": "Boot of 0f0f0f0f", "--hooks": "Hook context of 0f0f0f0f"}


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """A projects dir holding nothing yet, no live seats, no session in the
    environment and no settle threshold: SEAT resolves among transcripts."""
    root = tmp_path / "projects"
    (root / "-w").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.setattr(listing, "rows", lambda *a, **k: [])
    for var in (*SESSION_ID_VARS, "SESSION_SETTLE_AT", "CONTEXT_MAX"):
        monkeypatch.delenv(var, raising=False)
    return root


def _place(projects: Path, fixture: Path, sid: str = SID) -> Path:
    dest = projects / "-w" / f"{sid}.jsonl"
    shutil.copyfile(fixture, dest)
    return dest


def _run(argv, capsys):
    code = cli.main(["context", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------- the default stays the default (OQ1) ---------------------------------
def test_the_bare_one_liner_is_unchanged(projects, capsys):
    _place(projects, TOOLS)
    u = window.scan(projects / "-w" / f"{SID}.jsonl")
    size = window.resolve_window(u.model, u.used)
    fmt = window.fmt_tokens
    want = f"Context: {fmt(u.used)} / {fmt(size)} ({u.used * 100 // size}%) · {fmt(size - u.used)} free\n"
    assert _run([SID[:8]], capsys) == (0, want, "")


@pytest.mark.parametrize("argv, settle_at", [
    ([], None),                  # the text one-liner, no threshold (ST-26)
    (["--json"], None),          # the bare --json, band null (ST-155)
    (["--json"], "1k"),          # the bare --json, band really computed (ST-155)
], ids=["text", "json-unset", "json-set"])
def test_the_bare_one_liner_never_imports_the_views(projects, argv, settle_at):
    """Design section 5's amendment: the default pays nothing. A fresh
    interpreter, so no other test's import hides it. ST-155: the bare --json
    too, with SESSION_SETTLE_AT set, so `band` is computed, not skipped."""
    _place(projects, TOOLS)
    code = ("import sys; from sessiontools import cli, listing; "
            "listing.rows = lambda *a, **k: []; "
            f"rc = cli.main(['context', '{SID[:8]}', *{argv!r}]); "
            "assert rc == 0, rc; "
            "print(sorted(m for m in sys.modules if m.startswith('sessiontools.context_parts')))")
    env = {**os.environ, "CLAUDE_PROJECTS_DIR": str(projects)}
    env.pop("SESSION_SETTLE_AT", None)
    if settle_at is not None:
        env["SESSION_SETTLE_AT"] = settle_at
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          env=env, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[-1] == "[]"
    if argv:
        want = None if settle_at is None else band_mod.OVER     # 1k is far under used
        assert json.loads(lines[0])["band"] == want


# ---------- help: every option says what it does, with an example ------------------
def _help(capsys) -> str:
    with pytest.raises(SystemExit) as e:
        cli.main(["context", "--help"])
    assert e.value.code == 0
    return capsys.readouterr().out


def test_every_option_has_a_description_and_an_example(capsys):
    text = _help(capsys)
    named = re.findall(r"^  (SEAT|--[a-z]+)(?: N)? +\S", text, re.MULTILINE)
    assert set(named) == {"SEAT", "--peak", "--max", *VIEWS, "--tools", "--json"}
    assert set(cli.CONTEXT_HELP) == {"seat", *(n for n in named if n != "SEAT")}
    lines = text.splitlines()
    for name in named:
        i = next(k for k, line in enumerate(lines) if re.match(rf"^  {name}\b", line))
        description = lines[i].split(None, 1 + (" N " in lines[i]))[-1]
        assert len(description.split()) >= 4, (name, description)
        assert lines[i + 1].lstrip().startswith("session context"), (name, lines[i + 1])


def test_the_epilog_names_units_the_fitted_mark_and_settled(capsys):
    text = _help(capsys)
    epilog = text.split("Every number", 1)
    assert len(epilog) == 2, "the epilog no longer opens on the units"
    tail = epilog[1]
    assert "tokens (tok)" in tail and "chars" in tail
    assert '"~" marks a fitted' in tail
    assert '"?" is unknown' in tail
    assert "SETTLED:" in tail and "agent" in tail and "human session" in tail


def test_every_help_example_parses(capsys):
    """An example that does not run teaches the wrong thing."""
    for flag, line in cli.CONTEXT_HELP.items():
        example = line.splitlines()[-1].split("|")[0].split()
        assert example[:2] == ["session", "context"], flag
        with pytest.raises(SystemExit) as e:
            cli.main(["context", *example[2:], "--help"])
        assert e.value.code == 0, flag
    capsys.readouterr()


# ---------- end to end: each view on a transcript ----------------------------------
@pytest.mark.parametrize("fixture", [TOOLS, HOOKS, AGENT], ids=lambda p: p.name)
@pytest.mark.parametrize("view", VIEWS)
def test_each_view_runs_with_labelled_output(projects, capsys, monkeypatch, view, fixture):
    _place(projects, fixture)
    code, out, err = _run([SID[:8], view], capsys)
    assert (code, err) == (0, "")
    assert out.startswith(TITLES[view]), out.splitlines()[0]
    monkeypatch.setattr(render, "_data", lambda text: DATA)
    _code, blanked, _err = _run([SID[:8], view], capsys)
    assert bare_numbers(blanked) == [], view


@pytest.mark.parametrize("view", VIEWS)
def test_each_view_json_is_one_document_naming_its_view(projects, capsys, view):
    _place(projects, HOOKS if view == "--hooks" else TOOLS)
    code, out, err = _run([SID[:8], view, "--json"], capsys)
    assert (code, err) == (0, "")
    assert len(out.splitlines()) == 1
    doc = json.loads(out)
    assert doc["view"] == view[2:]
    assert doc["session"] == SID
    assert doc["rows"]


def test_parts_tools_splits_tool_results_by_tool(projects, capsys):
    _place(projects, TOOLS)
    _code, plain, _ = _run([SID[:8], "--parts"], capsys)
    code, out, err = _run([SID[:8], "--parts", "--tools"], capsys)
    assert (code, err) == (0, "")
    assert "largest single results, by chars:" in out
    assert "Read /w/big.md" in out and "Read /w/big.md" not in plain
    code, out, _ = _run([SID[:8], "--parts", "--tools", "--json"], capsys)
    tools = json.loads(out)["tools"]
    assert tools["rows"] and tools["largest"][0]["label"] == "Read /w/big.md"


def test_parts_window_is_the_models_or_max(projects, capsys):
    _place(projects, TOOLS)
    doc = json.loads(_run([SID[:8], "--parts", "--json"], capsys)[1])
    assert doc["window"] == window.resolve_window(doc["model"], doc["used"])
    doc = json.loads(_run([SID[:8], "--parts", "--json", "--max", "200000"], capsys)[1])
    assert doc["window"] == 200_000 and doc["free"] == 200_000 - doc["used"]


def test_the_views_find_the_transcript_the_one_liner_finds(projects, capsys):
    _place(projects, TOOLS)
    code, _out, err = _run(["feedface", "--parts"], capsys)
    assert code == 1 and "no transcript for 'feedface'" in err


def test_a_transcript_with_no_usage_is_exit_1_in_every_view(projects, capsys):
    (projects / "-w" / f"{SID}.jsonl").write_text("", encoding="utf-8")
    for view in VIEWS:
        code, out, err = _run([SID[:8], view], capsys)
        assert (code, out) == (1, ""), view
        assert "has no usage rows yet" in err


# ---------- band (design section 6) -------------------------------------------------
def test_band_is_null_without_a_settle_threshold(projects, capsys):
    _place(projects, TOOLS)
    code, out, err = _run([SID[:8], "--parts", "--json"], capsys)
    assert (code, err) == (0, "")
    assert json.loads(out)["band"] is None


def test_band_with_a_threshold_and_a_note_when_the_session_has_no_settings_dir(
        projects, capsys, monkeypatch):
    """The do-006 review's N3: a session with no settings dir reads the
    near cut from the host and template layers only, and stderr says so."""
    _place(projects, TOOLS)
    used = json.loads(_run([SID[:8], "--parts", "--json"], capsys)[1])["used"]
    monkeypatch.setenv("SESSION_SETTLE_AT", str(used))
    code, out, err = _run([SID[:8], "--parts", "--json"], capsys)
    assert code == 0 and json.loads(out)["band"] == band_mod.OVER
    assert err.startswith("session context: band: no session directory for " + SID)
    assert f"the near cut is {band_mod.DEFAULT_NEAR_PCT}% of the settle threshold" in err
    monkeypatch.setenv("SESSION_SETTLE_AT", str(used * 100))
    assert json.loads(_run([SID[:8], "--parts", "--json"], capsys)[1])["band"] == band_mod.OK


def test_band_reads_the_sessions_own_near_cut_without_a_note(projects, capsys, monkeypatch):
    _place(projects, TOOLS)
    used = json.loads(_run([SID[:8], "--parts", "--json"], capsys)[1])["used"]
    settings_dir = Path(os.environ["SESSIONS_DIR"]) / SID / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "session.json").write_text(json.dumps({"context": {"near_pct": 50}}))
    monkeypatch.setenv("SESSION_SETTLE_AT", str(int(used / 0.6)))   # used is 60% of it
    code, out, err = _run([SID[:8], "--parts", "--json"], capsys)
    assert (code, err) == (0, "")
    assert json.loads(out)["band"] == band_mod.NEAR                  # 20% says OK; 50% says near


# ---------- band on the bare --json (ST-155) ------------------------------------
#: The bare --json's keys before ST-155: it gains `band` and loses or renames none.
BARE_KEYS = {"used", "peak", "max", "pct", "free", "session", "model"}


def test_the_bare_json_is_the_old_keys_plus_band_with_the_old_values(projects, capsys):
    path = _place(projects, TOOLS)
    u = window.scan(path)
    size = window.resolve_window(u.model, u.used)
    code, out, err = _run([SID[:8], "--json"], capsys)
    assert (code, err) == (0, "")
    doc = json.loads(out)
    assert set(doc) == BARE_KEYS | {"band"}
    assert doc == {"used": u.used, "peak": u.peak, "max": size,
                   "pct": u.used * 100 // size, "free": size - u.used,
                   "session": SID, "model": u.model, "band": None}


#: The settle threshold as a function of `used`, for each band (near cut 20%).
THRESHOLDS = {
    "over": lambda used: used,                  # at the threshold is over
    "near": lambda used: int(used / 0.9),       # 90% of it: within 20% under
    "ok": lambda used: used * 100,              # 1% of it
    "null": lambda used: None,                  # no threshold: no band
}


@pytest.mark.parametrize("want", list(THRESHOLDS))
def test_the_bare_json_band_agrees_with_parts_json(projects, capsys, monkeypatch, want):
    """The SAME helper on both paths: same band, same stderr note."""
    _place(projects, TOOLS)
    used = json.loads(_run([SID[:8], "--json"], capsys)[1])["used"]
    assert used == json.loads(_run([SID[:8], "--parts", "--json"], capsys)[1])["used"]
    at = THRESHOLDS[want](used)
    if at is not None:
        monkeypatch.setenv("SESSION_SETTLE_AT", str(at))
    code, out, bare_err = _run([SID[:8], "--json"], capsys)
    assert code == 0
    bare = json.loads(out)["band"]
    code, out, parts_err = _run([SID[:8], "--parts", "--json"], capsys)
    assert code == 0
    assert bare == json.loads(out)["band"] == (None if want == "null" else want)
    assert bare_err == parts_err
    assert (bare_err == "") == (at is None)     # no threshold: no settings read, no note


def test_the_bare_json_band_reads_the_sessions_own_near_cut(projects, capsys, monkeypatch):
    _place(projects, TOOLS)
    used = json.loads(_run([SID[:8], "--json"], capsys)[1])["used"]
    settings_dir = Path(os.environ["SESSIONS_DIR"]) / SID / "settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / "session.json").write_text(json.dumps({"context": {"near_pct": 50}}))
    monkeypatch.setenv("SESSION_SETTLE_AT", str(int(used / 0.6)))   # used is 60% of it
    code, out, err = _run([SID[:8], "--json"], capsys)
    assert (code, err) == (0, "")
    assert json.loads(out)["band"] == band_mod.NEAR                  # 20% says OK; 50% says near


def test_the_bare_json_band_is_of_the_lines_own_used_the_peak_with_peak(
        projects, capsys, monkeypatch):
    path = _place(projects, TOOLS)
    u = window.scan(path)
    assert u.peak > u.used * 2                                       # the fixture's compaction
    monkeypatch.setenv("SESSION_SETTLE_AT", str(u.used * 2))         # under the peak, over used
    assert json.loads(_run([SID[:8], "--json"], capsys)[1])["band"] == band_mod.OK
    doc = json.loads(_run([SID[:8], "--json", "--peak"], capsys)[1])
    assert (doc["used"], doc["band"]) == (u.peak, band_mod.OVER)


def test_a_synthetic_zero_tail_has_no_band_even_with_a_threshold(projects, capsys, monkeypatch):
    """The bare line has no reading then (`pct` is null), so no band, and no
    settings are read. `--parts` bands the last REAL call instead: its `used`
    is a different reading, so its band may differ, and here it does."""
    path = _place(projects, TOOLS)
    with path.open("a") as f:
        f.write(json.dumps({"type": "assistant", "message": {
            "id": "k41", "model": window.SYNTHETIC_MODEL, "role": "assistant",
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n")
    monkeypatch.setenv("SESSION_SETTLE_AT", "1k")
    code, out, err = _run([SID[:8], "--json"], capsys)
    assert (code, err) == (0, "")
    doc = json.loads(out)
    assert (doc["used"], doc["pct"], doc["band"]) == (0, None, None)
    parts_doc = json.loads(_run([SID[:8], "--parts", "--json"], capsys)[1])
    assert parts_doc["used"] > 0 and parts_doc["band"] == band_mod.OVER


def test_band_lives_at_the_top_level_once():
    """ST-155: one module object, no shim left in context_parts."""
    import importlib.util

    import sessiontools.band as top
    assert band_mod is top
    assert importlib.util.find_spec("sessiontools.context_parts.band") is None
    assert not (ROOT / "src" / "sessiontools" / "context_parts" / "band.py").exists()


# ---------- refused combinations: usage errors, exit 2 ------------------------------
@pytest.mark.parametrize("argv, says", [
    (["--parts", "--hooks"], "not allowed with argument"),
    (["--startup", "--boot"], "not allowed with argument"),
    (["--tools"], "use it with --parts"),
    (["--hooks", "--tools"], "use it with --parts"),
    (["--parts", "--peak"], "--peak is the one-line view's"),
    (["--hooks", "--max", "1000"], "--max sizes the window"),
    (["--boot", "--max", "1000"], "--max sizes the window"),
    (["--parts", "--max", "0"], "--max must be a positive number"),
])
def test_a_combination_that_makes_no_sense_is_refused(projects, capsys, argv, says):
    _place(projects, TOOLS)
    with pytest.raises(SystemExit) as e:
        cli.main(["context", SID[:8], *argv])
    assert e.value.code == 2
    assert says in capsys.readouterr().err
