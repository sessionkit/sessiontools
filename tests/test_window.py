"""CONTEXT: the transcript fold, the window rules, the cell - pinned to the
behaviour of the session repo's `session-context` / `session-list`
(SESH-377), which this ports."""

from __future__ import annotations

import json

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


@pytest.mark.parametrize("model, used, env", [
    (None, 10, {}),                                   # no real model
    ("claude-opus-4-5", 10, {}),                      # legacy: publishes no window
    (OPUS, 10, {"CONTEXT_MAX": "lots"}),              # asserted but unreadable
    (OPUS, 10, {"CONTEXT_MAX": "0"}),
    ("claude-haiku-4-5", 250_000, {}),                # used above the window
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
        ["context_used", "context_peak", "context_window", "context_pct", "context_model"])


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
    (c.Fullness(used=10, peak=10, window=None, pct=None, model="claude-new"), "?"),
])
def test_the_cell_is_percent_or_a_placeholder(ctx, text):
    assert c.cell(ctx) == text
