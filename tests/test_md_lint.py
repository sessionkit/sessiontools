"""md-lint: the PostToolUse hook that names wide table lines and the word
`shape` in a Markdown file just written or edited. It answers; it never
blocks (ST-304).

The pure `lint` gets most of the cases; `main` gets the payload handling
(Write, Edit, MultiEdit, the edited region, the cap, silence); and the real
shim, `sh hooks/md-lint`, is run as `hooks/hooks.json` runs it, from an
environment built from nothing, for the switch, the birth-only gate, the
pre-filter and its silent failures.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sessiontools import md_lint
from sessiontools.md_lint import lint

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "md-lint"
HOOKS = ROOT / "hooks" / "hooks.json"
SID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


def row(cols: int) -> str:
    """A table row exactly COLS display columns wide."""
    body = "| " + "x" * (cols - 4) + " |"
    assert len(body) == cols
    return body


def rules(text: str) -> list[tuple[int, str]]:
    return [(f.line, f.rule) for f in lint(text)]


# 1. TABLE --------------------------------------------------------------------

def test_a_90_column_table_row_is_one_finding_naming_line_and_width():
    text = "# T\n\n| a | b |\n| - | - |\n" + row(90) + "\n"
    found = lint(text)
    assert len(found) == 1
    assert found[0].line == 5 and found[0].rule == "table"
    assert found[0].message.startswith("line 5 is 90 columns; ")
    assert "under 76 columns" in found[0].message


def test_a_75_column_row_is_nothing_and_76_is_flagged():
    assert lint(row(75) + "\n") == []
    assert rules(row(76) + "\n") == [(1, "table")]


def test_an_indented_table_line_counts_too():
    assert rules("  " + row(80)) == [(1, "table")]


def test_a_wide_line_that_is_not_a_table_is_not_flagged():
    assert lint("x" * 200) == []


def test_width_counts_display_columns_not_bytes():
    assert md_lint.width("abc") == 3
    assert md_lint.width("日本") == 4                 # wide: 2 each
    assert md_lint.width("ＡＢ") == 4                 # fullwidth: 2 each
    assert md_lint.width("é") == 1             # combining mark: 0
    assert md_lint.width("\t") == 1
    # 38 wide characters + "| " + " |" = 80 columns, 42 characters
    wide = "| " + "日" * 38 + " |"
    assert len(wide) == 42 and rules(wide) == [(1, "table")]
    accented = "| " + "é" * 70 + " |"         # 74 columns, 144 characters
    assert lint(accented) == []


@pytest.mark.parametrize("text", [
    "```\n" + row(90) + "\n```\n",
    "~~~md\n" + row(90) + "\n~~~\n",
    "---\nx: " + row(90) + "\n" + row(90) + "\n---\n",
    "<!--\n" + row(90) + "\n-->\n",
])
def test_a_wide_table_line_in_code_front_matter_or_a_comment_is_skipped(text):
    assert lint(text) == []


# 2. SHAPE --------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "The shape of it.", "Shapes differ.", "SHAPE", "a shape.", "| a shape | b |",
])
def test_shape_in_prose_is_flagged(text):
    assert rules(text) == [(1, "shape")]


def test_the_message_names_the_line_and_the_word():
    found = lint("one\n\nthe shape here\n")
    assert [f.message for f in found] == ["line 3 says 'shape': use the precise term"]


def test_two_on_one_line_are_one_finding():
    assert rules("shape and shape") == [(1, "shape")]


@pytest.mark.parametrize("word", ["reshape", "shaped", "shaping", "misshapen",
                                  "shapely", "reshaped", "shape_id", "shapefile"])
def test_other_words_are_not_flagged(word):
    assert lint(f"we {word} it") == []


@pytest.mark.parametrize("text", [
    "```\nshape\n```",                             # backtick fence
    "````md\nshape\n```\nshape\n````",             # a shorter run does not close it
    "~~~\nshape\n~~~",                             # tilde fence
    "---\ntitle: shape\n---\nbody",                # front matter
    "use `shape` here",                            # code span
    "use ``a ` shape`` here",                      # double-tick code span
    "see [the shape](x.md)",                       # link text
    "see [x](docs/shape.md)",                      # link target
    "see ![shape](img.png)",                       # image
    "see [the shape][ref]",                        # reference link
    "see [the shape][]",                           # collapsed reference
    "[shape]: https://example.com/shape",          # reference definition
    "see <https://example.com/shape>",             # autolink
    "see https://example.com/shape now",           # bare URL
    'he said "the shape" once',                    # straight quotes
    "he said “the shape” once",          # curly double quotes
    "he said ‘the shape’ once",          # curly single quotes
    "> the shape of it",                           # blockquote
    "   > the shape of it",                        # indented blockquote
    "a <!-- shape --> b",                          # inline comment
    "<!--\nshape\n-->",                            # comment block
])
def test_shape_in_skipped_places_is_not_flagged(text):
    assert lint(text) == []


def test_prose_after_a_closed_fence_or_comment_is_linted_again():
    assert rules("```\nx\n```\nshape") == [(4, "shape")]
    assert rules("<!--\nx\n--> shape") == [(3, "shape")]
    assert rules("---\na: b\n---\nshape") == [(4, "shape")]


def test_a_dashed_line_that_is_not_line_1_is_not_front_matter():
    assert rules("intro\n---\nshape\n---") == [(3, "shape")]


def test_unclosed_front_matter_is_not_front_matter():
    assert rules("---\nshape") == [(2, "shape")]


def test_prose_beside_a_skipped_span_is_still_linted():
    assert rules('`code` and a shape and "quote"') == [(1, "shape")]


def test_an_unclosed_backtick_is_literal():
    assert rules("a ` shape") == [(1, "shape")]


@pytest.mark.parametrize("sep", ["\f", "\v", "\x85", " ", " ", "\x1c", "\r"])
def test_lines_end_at_newline_only(sep):
    # An editor and regions() count "\n"; a form feed or U+2028 inside a
    # line must not shift the line numbers after it.
    assert rules(f"a{sep}b\nshape\n") == [(2, "shape")]


def test_crlf_lines_number_as_lf_and_width_drops_the_cr():
    assert rules("---\r\na: shape\r\n---\r\nshape\r\n") == [(4, "shape")]
    assert rules("| " + "x" * 71 + " |\r\n") == []          # 75 columns


# 3. main: the payload ----------------------------------------------------------

def run_main(body: object) -> str:
    out = io.StringIO()
    stdin = io.StringIO(body if isinstance(body, str) else json.dumps(body))
    assert md_lint.main(stdin, out) == 0
    return out.getvalue()


def said(out: str) -> list[str]:
    lines = out.splitlines()
    assert len(lines) == 1, out
    hso = json.loads(lines[0])["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert set(json.loads(lines[0])) == {"hookSpecificOutput"}   # never a decision
    return hso["additionalContext"].split("\n")


def write(path: Path, text: str) -> dict:
    path.write_text(text, encoding="utf-8")
    return {"session_id": SID, "hook_event_name": "PostToolUse", "tool_name": "Write",
            "tool_input": {"file_path": str(path), "content": text},
            "tool_response": {}}


def edit(path: Path, text: str, old: str, new: str, *, replace_all: bool = False) -> dict:
    path.write_text(text, encoding="utf-8")
    return {"session_id": SID, "hook_event_name": "PostToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": str(path), "old_string": old, "new_string": new,
                           "replace_all": replace_all}}


def test_a_write_with_a_90_column_row_says_one_finding_under_a_header(tmp_path):
    path = tmp_path / "doc.md"
    lines = said(run_main(write(path, "# T\n\n" + row(90) + "\n" + row(75) + "\n")))
    assert lines == [f"md-lint {path}:",
                     "line 3 is 90 columns; a table line stays under 76 columns, wider lines wrap"]


def test_a_clean_write_says_nothing(tmp_path):
    assert run_main(write(tmp_path / "doc.md", "# fine\n" + row(75) + "\n")) == ""


@pytest.mark.parametrize("name", ["doc.MD", "doc.Md"])
def test_the_suffix_is_any_case(tmp_path, name):
    assert said(run_main(write(tmp_path / name, "shape\n")))[1].startswith("line 1 says")


@pytest.mark.parametrize("name", ["doc.txt", "doc.markdown", "doc.mdx", "md"])
def test_a_file_that_is_not_md_is_silent(tmp_path, name):
    assert run_main(write(tmp_path / name, "shape\n" + row(90))) == ""


@pytest.mark.parametrize("tool", ["Read", "Bash", "NotebookEdit", None])
def test_other_tools_are_silent(tmp_path, tool):
    body = write(tmp_path / "doc.md", "shape\n")
    body["tool_name"] = tool
    assert run_main(body) == ""


def test_a_relative_path_is_read_against_cwd(tmp_path):
    body = write(tmp_path / "doc.md", "shape\n")
    body["tool_input"]["file_path"] = "doc.md"
    body["cwd"] = str(tmp_path)
    assert said(run_main(body)) == ["md-lint doc.md:", "line 1 says 'shape': use the precise term"]


def test_an_edit_reports_only_its_region(tmp_path):
    text = "shape old\n\nkeep\n\nnew shape here\nand " + row(90)[4:] + "\n" + row(90) + "\n"
    path = tmp_path / "doc.md"
    lines = said(run_main(edit(path, text, "x", "new shape here\nand ")))
    # line 1's shape and line 7's wide row predate the edit: not named
    assert lines[1:] == ["line 5 says 'shape': use the precise term"]


def test_an_edit_inside_a_line_reports_that_line(tmp_path):
    path = tmp_path / "doc.md"
    lines = said(run_main(edit(path, "a\n" + row(90) + "\nshape\n", "y", "xxx")))
    assert lines[1:] == [("line 2 is 90 columns; a table line stays under 76 columns,"
                          " wider lines wrap")]


def test_an_edit_sees_the_fence_around_it(tmp_path):
    path = tmp_path / "doc.md"
    text = "```\nthe shape\n```\n"
    assert run_main(edit(path, text, "x", "the shape")) == ""


def test_an_edit_that_deletes_reports_nothing(tmp_path):
    assert run_main(edit(tmp_path / "doc.md", "shape\n", "gone", "")) == ""


def test_replace_all_reports_every_occurrence(tmp_path):
    path = tmp_path / "doc.md"
    text = "the shape A\n\nthe shape A\n\nshape B\n"
    lines = said(run_main(edit(path, text, "x", "the shape A", replace_all=True)))
    assert [line.split(" says")[0] for line in lines[1:]] == ["line 1", "line 3"]


def test_multiedit_reports_each_edit_region(tmp_path):
    path = tmp_path / "doc.md"
    path.write_text("shape one\nshape two\nshape three\n", encoding="utf-8")
    body = {"tool_name": "MultiEdit", "tool_input": {"file_path": str(path), "edits": [
        {"old_string": "a", "new_string": "shape one"},
        {"old_string": "b", "new_string": "shape three"},
        {"old_string": "c"},                                  # no new_string: ignored
    ]}}
    lines = said(run_main(body))
    assert [line.split(" says")[0] for line in lines[1:]] == ["line 1", "line 3"]


def test_the_report_is_capped_at_ten_lines_and_counts_the_rest(tmp_path):
    lines = said(run_main(write(tmp_path / "doc.md", "shape\n" * 13)))
    assert len(lines) == 1 + 10 + 1
    assert lines[-1] == "... and 3 more"
    assert lines[10] == "line 10 says 'shape': use the precise term"


def test_exactly_ten_has_no_more_line(tmp_path):
    lines = said(run_main(write(tmp_path / "doc.md", "shape\n" * 10)))
    assert len(lines) == 11 and not lines[-1].startswith("...")


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "null", '{"tool_name": "Write"}',
                                   '{"tool_name": "Write", "tool_input": []}',
                                   '{"tool_name": "Write", "tool_input": {"file_path": 7}}',
                                   ('{"tool_name": "Edit", "tool_input": '
                                    '{"file_path": "/nonexistent/x.md", "new_string": "a"}}')])
def test_bad_input_is_silent(stdin, capsys):
    assert run_main(stdin) == ""
    assert capsys.readouterr().err == ""


def test_an_unreadable_file_is_silent(tmp_path):
    path = tmp_path / "doc.md"
    body = write(path, "shape\n")
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that reads mode-0 files")
        assert run_main(body) == ""
    finally:
        path.chmod(0o644)


def test_a_crash_in_lint_is_silent(tmp_path, monkeypatch):
    def boom(_text):
        raise RuntimeError("boom")
    monkeypatch.setattr(md_lint, "lint", boom)
    assert run_main(write(tmp_path / "doc.md", "shape\n")) == ""


# 4. The shim, as hooks.json runs it ----------------------------------------------

@pytest.fixture
def pybin(tmp_path: Path) -> Path:
    """A bin dir whose `python3` is the interpreter running this suite."""
    bindir = tmp_path / "pybin"
    bindir.mkdir()
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    return bindir


@pytest.fixture
def seat(tmp_path: Path) -> dict:
    """The environment of a seat `session start` launched: its session dir exists."""
    sessions = tmp_path / "sessions"
    (sessions / SID).mkdir(parents=True)
    return {"HOME": str(tmp_path), "SESSIONS_DIR": str(sessions),
            "CLAUDE_CODE_SESSION_ID": SID}


def run_shim(stdin: str | dict, env: dict, bindir: Path | None,
             shim: Path = SHIM) -> subprocess.CompletedProcess:
    path = ([str(bindir)] if bindir else []) + ["/usr/bin", "/bin"]
    full = {"PATH": os.pathsep.join(path), "HOME": "/nonexistent", **env}
    data = stdin if isinstance(stdin, str) else json.dumps(stdin)
    return subprocess.run(["/bin/sh", str(shim)], input=data, env=full,
                          capture_output=True, text=True, timeout=30, check=False)


def assert_silent(result: subprocess.CompletedProcess) -> None:
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_the_shim_says_the_finding_for_a_seat(tmp_path, pybin, seat):
    path = tmp_path / "doc.md"
    result = run_shim(write(path, row(90) + "\nthe shape\n"), seat, pybin)
    assert result.returncode == 0 and result.stderr == ""
    assert said(result.stdout) == [
        f"md-lint {path}:",
        "line 1 is 90 columns; a table line stays under 76 columns, wider lines wrap",
        "line 2 says 'shape': use the precise term"]


def test_the_shim_reads_a_payload_with_backslashes_intact(tmp_path, pybin, seat):
    path = tmp_path / "doc.md"
    body = write(path, "shape\n")
    body["tool_input"]["content"] = "a \\n b \\\\ c\n"         # backslashes printf must keep
    assert said(run_shim(body, seat, pybin).stdout)[1].startswith("line 1 says")


@pytest.mark.parametrize("word", ["off", "OFF", "0", "false", "False", "no", "NO"])
def test_the_off_switch_is_silent(tmp_path, pybin, seat, word):
    body = write(tmp_path / "doc.md", "shape\n")
    assert_silent(run_shim(body, {**seat, "SESSIONTOOLS_HOOK_MD_LINT": word}, pybin))


@pytest.mark.parametrize("word", ["", "on", "1", "yes", "anything"])
def test_anything_else_is_on(tmp_path, pybin, seat, word):
    body = write(tmp_path / "doc.md", "shape\n")
    result = run_shim(body, {**seat, "SESSIONTOOLS_HOOK_MD_LINT": word}, pybin)
    assert said(result.stdout)[1].startswith("line 1 says")


def test_a_session_start_did_not_launch_is_silent(tmp_path, pybin, seat):
    body = write(tmp_path / "doc.md", "shape\n")
    no_id = {k: v for k, v in seat.items() if k != "CLAUDE_CODE_SESSION_ID"}
    assert_silent(run_shim(body, no_id, pybin))
    assert_silent(run_shim(body, {**seat, "CLAUDE_CODE_SESSION_ID": "other"}, pybin))
    assert_silent(run_shim(body, {**seat, "CLAUDE_CODE_SESSION_ID": "../x"}, pybin))


def test_a_payload_with_no_md_never_starts_python(tmp_path, seat):
    """A python that would SAY something proves the pre-filter stops first."""
    bindir = tmp_path / "loud"
    bindir.mkdir()
    loud = bindir / "python3"
    loud.write_text("#!/bin/sh\necho LOUD\n")
    loud.chmod(0o755)
    body = write(tmp_path / "doc.txt", "shape\n")
    assert_silent(run_shim(body, {**seat, "SESSIONTOOLS_PYTHON": str(loud)}, bindir))


def test_the_shim_is_silent_on_bad_json_and_a_crash(tmp_path, pybin, seat):
    assert_silent(run_shim('{"file_path": "x.md", ', seat, pybin))
    crash = tmp_path / "crash"
    crash.mkdir()
    bad = crash / "python3"
    bad.write_text("#!/bin/sh\nkill -9 $$\n")
    bad.chmod(0o755)
    body = write(tmp_path / "doc.md", "shape\n")
    assert_silent(run_shim(body, {**seat, "SESSIONTOOLS_PYTHON": str(bad)}, crash))


def test_the_shim_is_silent_with_no_python(tmp_path, seat):
    body = write(tmp_path / "doc.md", "shape\n")
    result = subprocess.run(["/bin/sh", str(SHIM)], input=json.dumps(body),
                            env={**seat, "PATH": str(tmp_path / "empty")},
                            capture_output=True, text=True, timeout=30, check=False)
    assert_silent(result)


OLD_PYTHON = Path("/usr/bin/python3")


def _old_system_python() -> bool:
    """A clean Mac's /usr/bin/python3 is 3.9: the shim's own guard must silence it."""
    if not OLD_PYTHON.is_file():
        return False
    try:
        out = subprocess.run([str(OLD_PYTHON), "-c",
                              "import sys; print(sys.version_info >= (3, 11))"],
                             capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "False"


@pytest.mark.skipif(not _old_system_python(), reason="no python3 older than 3.11 at /usr/bin")
def test_the_shim_is_silent_on_a_python_older_than_3_11(tmp_path, seat):
    body = write(tmp_path / "doc.md", "shape\n")
    assert_silent(run_shim(body, {**seat, "SESSIONTOOLS_PYTHON": str(OLD_PYTHON)}, None))


def test_the_shim_without_its_helpers_is_silent(tmp_path, pybin, seat):
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "md-lint").write_text(SHIM.read_text())
    body = write(tmp_path / "doc.md", "shape\n")
    assert_silent(run_shim(body, seat, pybin, shim=lone / "md-lint"))


# 5. Wiring -------------------------------------------------------------------------

def test_hooks_json_wires_post_tool_use_on_write_and_edit():
    hooks = json.loads(HOOKS.read_text())
    entries = hooks["hooks"]["PostToolUse"]
    assert entries == [{"matcher": "Write|Edit|MultiEdit", "hooks": [{
        "type": "command", "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/md-lint"', "timeout": 5}]}, {"matcher": "Bash", "hooks": [{
        "type": "command", "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/hidden-failure" post', "timeout": 5}]}]
    description = hooks["description"]
    for word in ("md-lint", "SESSIONTOOLS_HOOK_MD_LINT", "NEVER BLOCKS", "BIRTH-ONLY"):
        assert word in description, word


def test_the_shim_is_executable_and_the_docs_exist():
    assert os.access(SHIM, os.X_OK)
    assert (ROOT / "docs" / "md-lint-hook.md").is_file()
    assert (ROOT / "openspec" / "changes" / "md-lint" / "proposal.md").is_file()
