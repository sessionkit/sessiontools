"""CONTEXT: the transcript fold, the window rules, the cell - pinned to the
behaviour of the session repo's `session-context` / `session-list`
(SESH-377), which this ports."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from sessiontools import window as c

OPUS = "claude-opus-5-5"


def row(used=None, model=OPUS, **usage):
    msg = {"model": model}
    if used is not None:
        msg["usage"] = {"input_tokens": used, **usage}
    return json.dumps({"type": "assistant", "message": msg})


def transcript(tmp_path, *lines, name="t.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------- the fold --------------------------------------------------------

def test_used_is_the_last_row_summed_and_peak_the_largest(tmp_path):
    p = transcript(tmp_path, row(100), row(500, cache_read_input_tokens=300,
                                          cache_creation_input_tokens=200), row(400))
    assert c.scan(p) == c.Usage(used=400, peak=1000, model=OPUS, usage_rows=3)


def test_model_is_the_last_real_id_never_synthetic(tmp_path):
    p = transcript(tmp_path, row(10, model="claude-sonnet-5"), row(20),
                   row(0, model=c.SYNTHETIC_MODEL))
    u = c.scan(p)
    assert (u.model, u.used, u.peak) == (OPUS, 0, 20)   # the synthetic zero tail


@pytest.mark.parametrize("bad", [
    '{"message": {"usage": {"input_tokens": 9',             # half-written tail
    '["usage", "model"]',                                    # JSON, not an object
    '{"message": "usage and model in a string"}',           # message not an object
    '{"message": {"usage": "lots"}}',                        # usage not an object
    '{"message": {"usage": {"input_tokens": NaN}}}',         # json accepts NaN
    '{"message": {"usage": {"input_tokens": Infinity}}}',
    '{"message": {"usage": {"input_tokens": "12", "cache_read_input_tokens": 1}}}',
    ('{"message": {"usage": {"input_tokens": "1", "cache_read_input_tokens": "2",'
     ' "cache_creation_input_tokens": "3"}}}'),              # all strings: concatenate
])
def test_a_line_the_fold_cannot_read_is_skipped(tmp_path, bad):
    p = transcript(tmp_path, row(700), bad)
    assert c.scan(p) == c.Usage(used=700, peak=700, model=OPUS, usage_rows=1)


def test_undecodable_bytes_cost_one_line_not_the_seat(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(row(300).encode() + b"\n\xff\xfe\"usage\"\n")
    assert c.scan(p).used == 300


def test_an_empty_usage_is_not_a_row(tmp_path):
    p = transcript(tmp_path, json.dumps({"message": {"model": OPUS, "usage": {}}}))
    assert c.scan(p).usage_rows == 0


# ---------- the window ------------------------------------------------------

def test_windows_by_model_with_dated_snapshots():
    assert c.resolve_window(OPUS, 10, env={}) == 1_000_000
    assert c.resolve_window("claude-haiku-4-5-20251001", 10, env={}) == 200_000


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-5-5"])
def test_the_5_5_ids_have_their_catalog_window(model):
    assert c.resolve_window(model, 10, env={}) == 1_000_000


def test_a_family_does_not_carry_its_window_across_versions():
    # The reason there is no prefix rule: one family, two windows.
    assert c.WINDOWS["claude-haiku-4-5"] == 200_000
    assert c.WINDOWS["claude-haiku-5-5"] == 1_000_000


@pytest.mark.parametrize("model, used, env", [
    (None, 10, {}),                                   # no real model
    ("claude-opus-4-5", 10, {}),                      # legacy: publishes no window
    (OPUS, 10, {"CONTEXT_MAX": "lots"}),              # asserted but unreadable
    (OPUS, 10, {"CONTEXT_MAX": "0"}),
    ("claude-haiku-4-5", 250_000, {}),                # used above the window
    ("claude-sonnet-5-6", 10, {}),                    # a known family is not a window
    ("claude-haiku-5", 10, {}),
])
def test_a_window_is_never_guessed(model, used, env):
    assert c.resolve_window(model, used, env=env) is None


def test_context_max_only_tightens():
    assert c.resolve_window(OPUS, 10, env={"CONTEXT_MAX": "200000"}) == 200_000
    assert c.resolve_window("claude-haiku-4-5", 10, env={"CONTEXT_MAX": "1000000"}) == 200_000
    assert c.resolve_window("claude-unmapped", 10, env={"CONTEXT_MAX": "1000000"}) is None
    assert c.resolve_window(OPUS, 300_000, env={"CONTEXT_MAX": "200000"}) is None


# ---------- one session -----------------------------------------------------

@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    return root, {"CLAUDE_PROJECTS_DIR": str(root)}


def put(root, cwd, sid, *lines):
    d = root / c.encode_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")


def test_of_reads_the_transcript_under_the_encoded_cwd(projects):
    root, env = projects
    put(root, "/Users/someone/.work", "abc-1", row(125_000))
    assert c.encode_dir("/Users/someone/.work") == "-Users-someone--work"
    assert c.of("abc-1", "/Users/someone/.work", env) == c.Fullness(
        used=125_000, peak=125_000, window=1_000_000, pct=12, model=OPUS)


def test_of_falls_back_to_a_glob_when_the_cwd_moved(projects):
    root, env = projects
    put(root, "/old/home", "abc-2", row(50_000))
    assert c.of("abc-2", "/new/home", env).used == 50_000


def test_the_glob_takes_regular_files_only(projects):
    import os
    root, env = projects
    (root / "moved").mkdir(parents=True)
    os.mkfifo(root / "moved" / "fifo-1.jsonl")     # a read here would block forever
    assert c.transcript_path("/elsewhere", "fifo-1", env) is None
    assert c.of("fifo-1", "/elsewhere", env) == c.UNKNOWN


def test_unknowable_is_five_nones_never_zero(projects, tmp_path):
    root, env = projects
    put(root, "/w", "no-rows", json.dumps({"type": "user", "message": {"content": "hi"}}))
    (root / c.encode_dir("/w") / "dir-1.jsonl").mkdir()
    assert c.of("missing", "/w", env) == c.UNKNOWN
    assert c.of("no-rows", "/w", env) == c.UNKNOWN
    assert c.of("dir-1", "/w", env) == c.UNKNOWN        # unreadable: never raises
    assert c.UNKNOWN.fields() == dict.fromkeys(
        ["context_used", "context_peak", "context_window", "context_pct", "context_model",
         "last_request_at", "cache_age_s"])                   # ST-326: additive


# ---------- long cwds: Claude Code's 200-character cap (ST-125) -------------

# A neutral 239-char cwd, the same length as the real launch cwd ST-125
# measured, and the 207-char directory Claude Code names for it: 200 chars,
# '-', hash. The suffixes (here and -at1c45 below) were derived outside this
# code, in node, by the rule ST-125 measured against the real directory.
LONG_CWD = ("/Users/someone/src/example.org/team/notes-store/projects/demo-project/"
            "item-42_docs_documentation-the-install-story-plugins-vs-manual-remote-servers_"
            "url-setup-install-script-registration-question-changelog-link-"
            "item-40-and-item-41-follow-up")
LONG_DIR = ("-Users-someone-src-example-org-team-notes-store-projects-demo-project-"
            "item-42-docs-documentation-the-install-story-plugins-vs-manual-remote-servers-"
            "url-setup-install-script-registration-question-chang-lzn9wp")


def test_a_long_cwd_encodes_to_claude_codes_capped_name():
    assert len(LONG_CWD) == 239 and len(LONG_DIR) == 207
    assert c.encode_dir(LONG_CWD) == LONG_DIR
    # the hash is over the RAW cwd: a different tail, same first 200 chars
    assert c.encode_dir(LONG_CWD + "/worktree")[:201] == LONG_DIR[:201]
    assert c.encode_dir(LONG_CWD + "/worktree").endswith("-at1c45")


def test_the_cap_is_exactly_two_hundred():
    at = "/" + "a" * 199
    assert c.encode_dir(at) == "-" + "a" * 199          # 200: untouched
    over = c.encode_dir(at + "b")
    assert over.startswith("-" + "a" * 199 + "-") and len(over) > 201


def test_a_short_cwd_encodes_as_before():
    assert c.encode_dir("/Users/someone/.work") == "-Users-someone--work"
    assert c.encode_dir("/a_b/c.d") == "-a-b-c-d"


# ---------- the charset: every non-alnum UTF-16 unit is '-' (ST-126) -------

# Two cwds and the directories claude 2.1.296 made for them (ST-126
# findings/charset.md): a space and + @ ~ = , each give one dash, 'é' one,
# and the emoji - a surrogate pair - TWO. The measured repro dir is swapped
# for a neutral one of plain alnum, '/' and '-', which encode one for one.
REPRO = "/Users/someone/src/notes-store/projects/tools/charset-fix/repro"
REPRO_DIR = "-Users-someone-src-notes-store-projects-tools-charset-fix-repro"
PUNCT_CWD, PUNCT_DIR = REPRO + "/st126 a+b@c~d=e,f", REPRO_DIR + "-st126-a-b-c-d-e-f"
WIDE_CWD, WIDE_DIR = REPRO + "/st126u \u00e9\U0001F600x", REPRO_DIR + "-st126u----x"


def test_the_measured_names():
    assert c.encode_dir(PUNCT_CWD) == PUNCT_DIR
    assert c.encode_dir(WIDE_CWD) == WIDE_DIR
    assert c.encode_dir("/a b") == "-a-b"


def test_an_emoji_is_two_dashes_and_a_lone_surrogate_one():
    assert c.encode_dir("/\U0001F600") == "---"
    assert c.encode_dir("/\udcff") == "--"            # os.fsdecode's escape: one JS unit
    assert c.encode_dir("/\u00e9") == c.encode_dir("/\u4e2d") == "--"


@pytest.mark.parametrize("cwd", [
    "/Users/someone/.work", "/a_b/c.d", "/", "", "/x--y__z..w", "/" + "aZ09._-/" * 20,
    "/Users/someone/src/sessiontools", LONG_CWD, LONG_CWD + "/worktree",
])
def test_a_cwd_of_alnum_and_slash_dot_underscore_dash_encodes_as_before(cwd):
    # ST-125's encoding, before this fix: only / . _ were replaced
    old = cwd.replace("/", "-").replace(".", "-").replace("_", "-")
    old = old if len(old) <= 200 else f"{old[:200]}-{c._cwd_hash(cwd)}"
    assert c.encode_dir(cwd) == old


def test_newest_transcript_finds_a_dir_named_the_measured_way(projects):
    root, env = projects
    for cwd, name in ((PUNCT_CWD, PUNCT_DIR), (WIDE_CWD, WIDE_DIR)):
        (root / name).mkdir(parents=True)
        (root / name / "s.jsonl").write_text(row(10) + "\n")
        assert c.newest_transcript(cwd, env) == root / name / "s.jsonl"
        assert c.transcript_path(cwd, "s", env) == root / name / "s.jsonl"


def test_a_capped_cwd_with_a_non_alnum_char(projects):
    root, env = projects
    cwd = "/" + "a+" * 110 + "\U0001F600"           # 222 code points, 223 UTF-16 units
    name = c.encode_dir(cwd)
    assert name.isascii()
    assert name[:200] == "-" + "a-" * 99 + "a"        # the '+'s are dashes, inside the cut
    assert name == name[:200] + "-" + c._cwd_hash(cwd)  # hash over the RAW cwd
    # a capped emoji: the cut counts its TWO units, as JS does
    wide = "/" + "a" * 198 + "\U0001F600" + "b"
    assert c.encode_dir(wide)[:201] == "-" + "a" * 198 + "-" + "-"
    (root / name).mkdir(parents=True)
    (root / name / "s.jsonl").write_text(row(10) + "\n")
    assert c.newest_transcript(cwd, env) == root / name / "s.jsonl"
    # another build's hash: the prefix fallback uses the same sanitized cut
    other = name[:200] + "-zzzz"
    (root / name / "s.jsonl").unlink()
    (root / name).rmdir()
    (root / other).mkdir()
    (root / other / "t.jsonl").write_text(row(10) + "\n")
    assert c.newest_transcript(cwd, env) == root / other / "t.jsonl"


def test_both_lookups_find_a_long_cwds_transcript(projects):
    root, env = projects
    (root / LONG_DIR).mkdir(parents=True)
    (root / LONG_DIR / "abc-9.jsonl").write_text(row(70_000) + "\n")
    assert c.newest_transcript(LONG_CWD, env) == root / LONG_DIR / "abc-9.jsonl"
    assert c.transcript_path(LONG_CWD, "abc-9", env) == root / LONG_DIR / "abc-9.jsonl"
    assert c.of("abc-9", LONG_CWD, env).used == 70_000


def test_a_capped_name_with_another_hash_falls_back_to_the_prefix(projects):
    root, env = projects
    other = LONG_DIR[:200] + "-zzzzzzzzzzzzz"              # another build's hash
    (root / other).mkdir(parents=True)
    (root / other / "abc-8.jsonl").write_text(row(10) + "\n")
    (root / (LONG_DIR[:199] + "X-lzn9wp")).mkdir()       # differs inside the prefix: not ours
    (root / (LONG_DIR[:199] + "X-lzn9wp") / "abc-7.jsonl").write_text(row(10) + "\n")
    assert c.newest_transcript(LONG_CWD, env) == root / other / "abc-8.jsonl"
    # the direct lookup uses the prefix too, before the every-project glob,
    # which would take this newer namesake elsewhere instead
    import os
    (root / "elsewhere").mkdir()
    (root / "elsewhere" / "abc-8.jsonl").write_text(row(10) + "\n")
    os.utime(root / other / "abc-8.jsonl", (1, 1))
    assert c.transcript_path(LONG_CWD, "abc-8", env) == root / other / "abc-8.jsonl"
    assert c.transcript_path("/moved", "abc-8", env) == root / "elsewhere" / "abc-8.jsonl"


def test_the_exact_name_wins_over_a_prefix_match(projects):
    root, env = projects
    for d in (LONG_DIR, LONG_DIR[:200] + "-zzzz"):
        (root / d).mkdir(parents=True)
        (root / d / f"{d[-4:]}.jsonl").write_text(row(10) + "\n")
    assert c.newest_transcript(LONG_CWD, env) == root / LONG_DIR / "n9wp.jsonl"


def test_the_prefix_is_a_string_not_a_glob_pattern(projects):
    root, env = projects
    cwd = "/" + "[x]*" * 60                             # glob metacharacters, 241 chars
    name = c.encode_dir(cwd)[:200] + "-other"
    (root / name).mkdir(parents=True)
    (root / name / "s.jsonl").write_text(row(10) + "\n")
    assert c.newest_transcript(cwd, env) == root / name / "s.jsonl"


def test_a_short_cwd_never_falls_back_to_a_prefix(projects):
    root, env = projects
    (root / "-w-elsewhere").mkdir(parents=True)
    (root / "-w-elsewhere" / "s.jsonl").write_text(row(10) + "\n")
    assert c.newest_transcript("/w", env) is None
    assert c.newest_transcript("/missing", {"CLAUDE_PROJECTS_DIR": str(root / "none")}) is None


def test_a_set_but_empty_projects_dir_stays_isolated(monkeypatch):
    assert str(c.projects_dir({"CLAUDE_PROJECTS_DIR": ""})) == "."
    assert c.projects_dir({}).parts[-2:] == (".claude", "projects")


# ---------- the cell --------------------------------------------------------

@pytest.mark.parametrize("ctx, text", [
    (c.UNKNOWN, "-"),
    (c.Fullness(used=370_000, peak=370_000, window=1_000_000, pct=37, model=OPUS), "37%"),
    (c.Fullness(used=0, peak=0, window=1_000_000, pct=0, model=OPUS), "0%"),       # a reading
    (c.Fullness(used=0, peak=277_000, window=1_000_000, pct=0, model=OPUS), "?"),  # synthetic tail
    (c.Fullness(used=10, peak=10, window=None, pct=None, model="claude-new"), "0%?"),
    # an unknown model shows its usage against the assumed 1M, marked
    (c.Fullness(used=370_000, peak=370_000, window=None, pct=None, model="claude-fable-5-3"), "37%?"),
    # ...but never over the assumed window
    (c.Fullness(used=1_200_000, peak=1_200_000, window=None, pct=None, model="claude-new"), "?"),
    # a KNOWN model with no window (over its map) keeps a bare ?: its None is real
    (c.Fullness(used=250_000, peak=250_000, window=None, pct=None, model="claude-haiku-4-5"), "?"),
])
def test_the_cell_is_percent_or_a_placeholder(ctx, text):
    assert c.cell(ctx) == text


def test_fable_and_mythos_5_2_have_a_window():
    """Fable 5.2 showed `?` while 5.1 showed a percent; the map had
    no 5.2 row."""
    assert c.resolve_window("claude-fable-5-2", 10, env={}) == 1_000_000
    assert c.resolve_window("claude-mythos-5-2", 10, env={}) == 1_000_000


def test_the_assumed_window_is_display_only():
    """The JSON stays honest: an unknown model's window and pct are None."""
    assert c.resolve_window("claude-fable-5-3", 10, env={}) is None
    f = c.Fullness(used=370_000, peak=370_000, window=None, pct=None, model="claude-fable-5-3")
    assert f.fields()["context_pct"] is None
    assert c.cell(f) == "37%?"


# ---------- the cache clock: last_request_at / cache_age_s (ST-326) ---------
#
# Synthetic transcripts only: timestamps and token counts, never a real
# transcript's text (they hold secrets).

NOW = datetime(2030, 6, 9, 3, 0, 0, tzinfo=UTC)
NOW_S = NOW.timestamp()
#: Midnight on 0001-01-01 at +01:00, the stamp of an instant before UTC's
#: year 1. Built, not written, so no clock sits apart from a 2030 date.
BEFORE_YEAR_ONE = datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))).isoformat()


def at(used=None, ts=None, model=OPUS, **usage):
    """A row like `row()`, with a top-level `timestamp` when TS is given."""
    obj = json.loads(row(used, model=model, **usage))
    if ts is not None:
        obj["timestamp"] = ts
    return json.dumps(obj)


def test_scan_records_the_last_usage_rows_timestamp(tmp_path):
    p = transcript(tmp_path, at(100, "2030-06-09T01:00:00.000Z"),
                   at(500, "2030-06-09T02:45:11.553Z"))
    u = c.scan(p)
    assert u.last_request_at == "2030-06-09T02:45:11.553Z"
    assert (u.used, u.peak, u.usage_rows) == (500, 500, 2)


def test_last_request_at_is_the_row_that_sets_used(tmp_path):
    """Not the largest, not the first: the LAST counted usage row's, the
    same row `used` comes from."""
    p = transcript(tmp_path, at(900, "2030-06-09T02:00:00Z"), at(10, "2030-06-09T02:30:00Z"))
    u = c.scan(p)
    assert (u.used, u.peak, u.last_request_at) == (10, 900, "2030-06-09T02:30:00.000Z")


@pytest.mark.parametrize("raw, want", [
    ("2030-06-09T02:45:11.553Z", "2030-06-09T02:45:11.553Z"),
    ("2030-06-09T02:45:11Z", "2030-06-09T02:45:11.000Z"),
    ("2030-06-09T02:45:11.553+00:00", "2030-06-09T02:45:11.553Z"),
    ("2030-06-09T13:45:11.553+11:00", "2030-06-09T02:45:11.553Z"),     # +11:00 -> UTC
    ("2030-06-09T02:45:11.553999Z", "2030-06-09T02:45:11.553Z"),      # micro -> milli
])
def test_iso_utc_writes_utc_ending_z(raw, want):
    assert c.iso_utc(raw) == want


@pytest.mark.parametrize("raw", [
    None, "", "   ", 1907203511, 1907203511.5, ["2030-06-09T02:45:11Z"], {"t": 1},
    "yesterday", "2030-06-09T99:00:00Z",
    "2030-06-09T02:45:11",        # naive: no zone, so no guess at one
    "2030-06-09",                 # a date alone
    BEFORE_YEAR_ONE,              # no UTC instant exists for it
])
def test_iso_utc_is_none_for_anything_not_a_zoned_instant(raw):
    assert c.iso_utc(raw) is None


@pytest.mark.parametrize("bad", [
    None, "not a time", 1907203511, "2030-06-09T02:45:11", BEFORE_YEAR_ONE])
def test_a_bad_timestamp_gives_none_and_never_costs_the_row(tmp_path, bad):
    """The last usage row's timestamp is missing or unreadable: None, never
    an earlier row's (that would be a guess), and used/peak/rows/model are
    exactly what they are without any timestamp at all."""
    p = transcript(tmp_path, at(100, "2030-06-09T01:00:00Z"), at(700, bad))
    u = c.scan(p)
    assert u.last_request_at is None
    assert u == c.Usage(used=700, peak=700, model=OPUS, usage_rows=2)


def test_a_later_good_timestamp_recovers_the_clock(tmp_path):
    p = transcript(tmp_path, at(100, "garbage"), at(200, "2030-06-09T02:00:00Z"))
    assert c.scan(p).last_request_at == "2030-06-09T02:00:00.000Z"


def test_a_trailing_line_that_is_not_a_usage_row_does_not_move_it(tmp_path):
    """User turns, tool results, a model-only line, an empty usage and a
    line the fold cannot read all carry or sit after a later timestamp;
    none of them is a counted usage row, so none moves the clock."""
    later = "2030-06-09T02:59:00Z"
    p = transcript(
        tmp_path,
        at(400, "2030-06-09T02:10:00Z"),
        json.dumps({"type": "user", "timestamp": later, "message": {"content": "hi"}}),
        json.dumps({"type": "assistant", "timestamp": later, "message": {"model": OPUS}}),
        json.dumps({"timestamp": later, "message": {"model": OPUS, "usage": {}}}),
        json.dumps({"timestamp": later, "message": {"usage": {"input_tokens": "x", "cache_read_input_tokens": 1}}}),
        '{"timestamp": "' + later + '", "message": {"usage": {"input_tokens": 9',
    )
    u = c.scan(p)
    assert (u.last_request_at, u.used, u.usage_rows) == ("2030-06-09T02:10:00.000Z", 400, 1)


def test_no_usage_row_is_no_clock(tmp_path):
    p = transcript(tmp_path, json.dumps({"type": "user", "timestamp": "2030-06-09T02:00:00Z",
                                         "message": {"content": "hi"}}))
    assert c.scan(p).last_request_at is None


@pytest.mark.parametrize("last, now, age", [
    ("2030-06-09T02:00:00.000Z", NOW_S, 3600),
    ("2030-06-09T02:59:59.400Z", NOW_S, 0),             # 0.6 s: whole seconds, floored
    ("2030-06-09T02:58:59.999Z", NOW_S, 60),
    ("2030-06-09T03:00:05.000Z", NOW_S, 0),             # ahead of the clock: 0, never negative
    ("2030-06-09T13:00:00.000+11:00", NOW_S, 3600),     # any offset reads the same instant
    (None, NOW_S, None),
    ("unreadable", NOW_S, None),
])
def test_cache_age_s_is_whole_seconds_since_floored_at_zero(last, now, age):
    assert c.cache_age_s(last, now) == age
    assert c.Fullness(last_request_at=last).cache_age_s(now) == age


def test_cache_age_s_with_no_now_reads_the_clock(monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: NOW_S + 90)
    assert c.cache_age_s("2030-06-09T03:00:00Z") == 90
    assert c.Fullness(last_request_at="2030-06-09T03:00:00Z").fields()["cache_age_s"] == 90


PRE_ST326_KEYS = ["context_used", "context_peak", "context_window", "context_pct",
                  "context_model"]


def test_fields_is_additive_the_old_keys_first_and_unchanged():
    f = c.Fullness(used=1, peak=2, window=3, pct=4, model=OPUS,
                   last_request_at="2030-06-09T02:00:00.000Z")
    got = f.fields(NOW_S)
    assert list(got)[:len(PRE_ST326_KEYS)] == PRE_ST326_KEYS
    assert [got[k] for k in PRE_ST326_KEYS] == [1, 2, 3, 4, OPUS]
    assert list(got)[len(PRE_ST326_KEYS):] == ["last_request_at", "cache_age_s"]
    assert (got["last_request_at"], got["cache_age_s"]) == ("2030-06-09T02:00:00.000Z", 3600)
    assert c.UNKNOWN.fields(NOW_S) == dict.fromkeys(PRE_ST326_KEYS + ["last_request_at",
                                                                      "cache_age_s"])


def test_of_carries_the_clock_and_fields_measures_it_against_now(projects):
    root, env = projects
    put(root, "/w", "abc-9", at(125_000, "2030-06-09T02:30:00.000Z"))
    f = c.of("abc-9", "/w", env)
    assert f == c.Fullness(used=125_000, peak=125_000, window=1_000_000, pct=12, model=OPUS,
                           last_request_at="2030-06-09T02:30:00.000Z")
    assert f.fields(NOW_S)["cache_age_s"] == 1800
    assert f.fields(NOW_S + 1800)["cache_age_s"] == 3600     # the same reading, later


# The shape of SM-1047's 156 measured wake gaps (wake-origin-7d.tsv: gap_min
# from 60.0 to 381.9, the cache going cold between 60.1 and 60.8). Built here
# from numbers alone, nothing copied from the file, and widened to cross the
# hour from BELOW too: the clock must read true either side of 60 min.
SM1047_EDGE_MIN = (59.0, 59.5, 59.9, 59.99, 60.0, 60.0, 60.05, 60.1, 60.1, 60.8, 61.4, 63.0)


def sm1047_shaped_gaps() -> list[float]:
    rest = 156 - len(SM1047_EDGE_MIN)
    spread = [round(61.0 * (381.9 / 61.0) ** (i / (rest - 1)), 2) for i in range(rest)]
    return [*SM1047_EDGE_MIN, *spread]


def test_the_sm1047_shaped_gaps_read_true_either_side_of_the_hour(tmp_path):
    gaps = sm1047_shaped_gaps()
    assert len(gaps) == 156 and min(gaps) < 60 < max(gaps) and max(gaps) == 381.9
    for i, gap_min in enumerate(gaps):
        last = NOW - timedelta(minutes=gap_min)
        earlier = last - timedelta(minutes=5)
        p = transcript(
            tmp_path,
            at(1000, earlier.isoformat().replace("+00:00", "Z")),
            at(2000, last.isoformat().replace("+00:00", "Z")),
            json.dumps({"type": "user", "timestamp": NOW.isoformat(),   # the wake itself
                        "message": {"content": "page"}}),
            name=f"gap-{i}.jsonl")
        u = c.scan(p)
        age = c.Fullness(used=u.used, last_request_at=u.last_request_at).cache_age_s(NOW_S)
        want = int((timedelta(minutes=gap_min) // timedelta(milliseconds=1)) // 1000)
        assert age == want, (gap_min, u.last_request_at)
        assert (age >= 3600) == (gap_min >= 60.0), gap_min       # the hour, read true


# ---------- a <synthetic> row never moves the clock (ST-326 ruling 14:11) ----

def test_a_synthetic_tail_after_a_real_row_keeps_the_real_rows_timestamp(tmp_path):
    """A `<synthetic>` usage row is Claude Code's own message, not an API
    request: it still sets used (to 0, as today) and counts a row, but the
    clock stays on the last REAL row. Moving it would understate the cache's
    age, the failure SM-1047 found."""
    p = transcript(tmp_path, at(900, "2030-06-09T02:00:00Z"),
                   at(0, "2030-06-09T02:50:00Z", model=c.SYNTHETIC_MODEL))
    u = c.scan(p)
    assert u.last_request_at == "2030-06-09T02:00:00.000Z"
    assert (u.used, u.peak, u.usage_rows, u.model) == (0, 900, 2, OPUS)   # today's fold
    assert c.cache_age_s(u.last_request_at, NOW_S) == 3600


@pytest.mark.parametrize("model", [c.SYNTHETIC_MODEL, " <synthetic> ", None, "", 7])
def test_only_a_real_model_usage_row_sets_the_clock(tmp_path, model):
    """No real row at all: no clock, though used/rows count the row."""
    p = transcript(tmp_path, at(5, "2030-06-09T02:50:00Z", model=model))
    u = c.scan(p)
    assert u.last_request_at is None
    assert (u.used, u.usage_rows) == (5, 1)


def test_a_real_row_after_a_synthetic_one_moves_the_clock(tmp_path):
    p = transcript(tmp_path, at(900, "2030-06-09T01:00:00Z"),
                   at(0, "2030-06-09T01:30:00Z", model=c.SYNTHETIC_MODEL),
                   at(950, "2030-06-09T02:10:00Z"))
    assert c.scan(p).last_request_at == "2030-06-09T02:10:00.000Z"


def test_a_bad_timestamp_on_the_last_real_row_is_none_past_a_synthetic_tail(tmp_path):
    """The rule for a bad timestamp is unchanged: the last REAL row's is
    unreadable, so None, and a synthetic tail does not paper over it."""
    p = transcript(tmp_path, at(100, "2030-06-09T01:00:00Z"), at(700, "garbage"),
                   at(0, "2030-06-09T02:50:00Z", model=c.SYNTHETIC_MODEL))
    assert c.scan(p).last_request_at is None
