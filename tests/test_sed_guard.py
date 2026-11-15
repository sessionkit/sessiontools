"""sed-guard (ST-303): `hooks/sed-guard`, a PreToolUse Bash hook that refuses
`sed -i ''` (BSD sed's form) when the sed the call runs is GNU, and names
the GNU form.

The README's success criteria are tested THROUGH THE REAL SHIM, as
`hooks/hooks.json` runs it (`/bin/sh hooks/sed-guard`, the payload on
stdin, SESSIONTOOLS_PYTHON a python that counts its starts). The sed it
probes is a FAKE on PATH, one answering `--version` as GNU and one as BSD,
so no test depends on this host's sed. The parser table runs in process
(`sed_guard.decide`, the GNU probe stubbed).

The refusal shape (JSON on stdout, permissionDecision deny, exit 0) is the
one measured on Claude Code 2.1.325 under bypassPermissions
(docs/sed-guard-hook.md).
"""

from __future__ import annotations

import io
import json
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sessiontools import sed_guard as G

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "hooks" / "sed-guard"
HOOKS = ROOT / "hooks" / "hooks.json"
MODULE = ROOT / "src" / "sessiontools" / "sed_guard.py"

GNU_VERSION = "sed (GNU sed) 4.10\nPackaged by Homebrew\n"


# --- the fakes -------------------------------------------------------------------

def w(path: Path, text: str, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def fake_sed(where: Path, kind: str) -> Path:
    """A `sed` that answers `--version` as KIND (gnu, bsd, slow) and edits nothing."""
    body = {
        "gnu": f"case \"$1\" in --version) printf '{GNU_VERSION}' ;; esac\nexit 0\n",
        "bsd": "echo 'sed: illegal option -- -' >&2\nexit 1\n",
        "slow": "sleep 5\nprintf 'GNU sed\\n'\n",
    }[kind]
    return w(where / "sed", "#!/bin/sh\n" + body)


@pytest.fixture
def bins(tmp_path) -> dict[str, Path]:
    """Three dirs, each holding one fake `sed`: gnu, bsd, slow."""
    return {kind: fake_sed(tmp_path / kind, kind).parent for kind in ("gnu", "bsd", "slow")}


def marked_python(tmp_path: Path, real: str = sys.executable) -> tuple[Path, Path]:
    """A `python` that logs each start to MARK, then runs REAL."""
    mark = tmp_path / "python-started"
    py = w(tmp_path / "marked" / "python", f'#!/bin/sh\necho x >> "{mark}"\nexec "{real}" "$@"\n')
    return py, mark


def starts(mark: Path) -> int:
    try:
        return len(mark.read_text().split())
    except FileNotFoundError:
        return 0


def bash(command: str, **more) -> dict:
    """A PreToolUse payload for the Bash tool, as Claude Code 2.1.325 sends it
    (measured: the description and cwd ride along)."""
    return {"session_id": "5a5a5a5a-1b1b-4c4c-8d8d-9e9e9e9e9e9e", "cwd": "/nowhere",
            "permission_mode": "bypassPermissions", "hook_event_name": "PreToolUse",
            "tool_name": "Bash", "tool_input": {"command": command, "description": "Edit f"},
            "tool_use_id": "toolu_x", **more}


def shim(body, py: Path, path: str, **env_more: str) -> subprocess.CompletedProcess:
    env = {"PATH": path, "SESSIONTOOLS_PYTHON": str(py), "HOME": "/nowhere", **env_more}
    text = body if isinstance(body, str) else json.dumps(body)
    return subprocess.run(["/bin/sh", str(SHIM)], input=text, env=env, capture_output=True,
                          text=True, timeout=30, check=False)


@pytest.fixture
def gnu_first(tmp_path, bins):
    """Run the shim with GNU sed first on PATH: returns run(command_or_body)."""
    py, mark = marked_python(tmp_path)
    path = f"{bins['gnu']}:/usr/bin:/bin"

    def run(body, raw=False, **env_more):
        if isinstance(body, str) and not raw:
            body = bash(body)
        return shim(body, py, path, **env_more)
    run.mark = mark
    run.bins = bins
    return run


def refused(result: subprocess.CompletedProcess) -> str:
    """The refusal reason, after checking the measured contract's shape."""
    assert (result.returncode, result.stderr) == (0, "")
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def allowed(result: subprocess.CompletedProcess) -> bool:
    return (result.returncode, result.stdout, result.stderr) == (0, "", "")


def gnu_form_in(reason: str) -> str:
    """The indented GNU-form lines of a refusal reason."""
    return "\n".join(line[4:] for line in reason.split("\n") if line.startswith("    "))


# --- success criterion 1: REFUSED, and the reason carries the GNU form ------------

@pytest.mark.parametrize(("command", "gnu"), [
    ("sed -i '' 's/a/b/' f", "sed -i 's/a/b/' f"),
    ('sed -i "" \'s/a/b/\' f', "sed -i 's/a/b/' f"),
    ("cd d && sed -i '' -e 's/a/b/' f", "cd d && sed -i -e 's/a/b/' f"),
    ("sed -Ei '' 's/a/b/' f", "sed -Ei 's/a/b/' f"),
], ids=["single", "double", "cd-and-e", "cluster"])
def test_the_bsd_form_is_refused_with_the_gnu_form(gnu_first, command, gnu):
    reason = refused(gnu_first(command))
    assert gnu_form_in(reason) == gnu
    assert "GNU sed" in reason and "SESSIONTOOLS_HOOK_SED=off" in reason


# --- success criterion 2: PASSED ------------------------------------------------

@pytest.mark.parametrize("command", [
    "sed -i 's/a/b/' f",
    "sed -i.bak 's/a/b/' f",
    "sed -i -e 's/a/b/' f",
    "sed -n '1p' f",
    "echo \"sed -i ''\"",
    "cat <<'EOF' > note.md\nrun sed -i '' 's/a/b/' f to break it\nEOF",
    "sed -i '' 's/a/b/ f",
], ids=["plain", "suffix", "dash-e", "dash-n", "quoted", "heredoc", "unparseable"])
def test_the_gnu_forms_and_mentions_pass(gnu_first, command):
    assert allowed(gnu_first(command))


def test_a_non_bash_payload_passes(gnu_first):
    body = {"hook_event_name": "PreToolUse", "tool_name": "Write",
            "tool_input": {"file_path": "x.md", "content": "sed -i '' 's/a/b/' f"}}
    assert allowed(gnu_first(body))


@pytest.mark.parametrize("text", [
    "not json: sed -i '' x f",
    "[\"sed -i '' x f\"]",
    "{\"tool_name\": \"Bash\", \"note\": \"sed -i '' x f\"}",
    "{\"tool_name\": \"Bash\", \"tool_input\": {\"command\": 7}, \"note\": \"sed -i '' x\"}",
    "{\"tool_name\": \"Bash\", \"tool_input\": \"sed -i '' x f\"}",
    "{\"tool_input\": {\"command\": \"sed -i '' x f\"}}",
], ids=["garbage", "list", "no-input", "not-a-string", "input-not-object", "no-tool-name"])
def test_a_payload_python_cannot_use_passes_silently(gnu_first, text):
    # each holds `sed` and an empty pair, so it gets past the shim to python
    assert allowed(gnu_first(text, raw=True))
    assert starts(gnu_first.mark) == 1


# --- only GNU is refused: the sed the call names is probed ------------------------

def test_a_path_qualified_bsd_sed_passes_while_path_sed_is_gnu(gnu_first):
    """`/usr/bin/sed -i ''` on a Mac is BSD and right (design step 4)."""
    command = f"{gnu_first.bins['bsd']}/sed -i '' 's/a/b/' f"
    assert allowed(gnu_first(command))


def test_a_path_qualified_gnu_sed_is_refused_whatever_path_holds(tmp_path, bins):
    py, _ = marked_python(tmp_path)
    command = f"{bins['gnu']}/sed -i '' 's/a/b/' f"
    reason = refused(shim(bash(command), py, f"{bins['bsd']}:/usr/bin:/bin"))
    assert gnu_form_in(reason) == f"{bins['gnu']}/sed -i 's/a/b/' f"


def test_a_bsd_first_host_is_never_refused(tmp_path, bins):
    py, _ = marked_python(tmp_path)
    assert allowed(shim(bash("sed -i '' 's/a/b/' f"), py, f"{bins['bsd']}:/usr/bin:/bin"))


def test_gsed_is_gnu_without_a_probe(tmp_path, bins):
    py, _ = marked_python(tmp_path)
    reason = refused(shim(bash("gsed -i '' 's/a/b/' f"), py, f"{bins['bsd']}:/usr/bin:/bin"))
    assert gnu_form_in(reason) == "gsed -i 's/a/b/' f"


def test_a_relative_sed_is_probed_from_the_payload_cwd(gnu_first):
    body = bash("./sed -i '' 's/a/b/' f", cwd=str(gnu_first.bins["gnu"]))
    assert refused(gnu_first(body))
    body = bash("./sed -i '' 's/a/b/' f", cwd=str(gnu_first.bins["bsd"]))
    assert allowed(gnu_first(body))


def test_no_sed_on_path_passes(tmp_path):
    py, _ = marked_python(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert allowed(shim(bash("sed -i '' 's/a/b/' f"), py, str(empty)))


def test_a_probe_past_its_cap_passes(tmp_path, bins):
    py, _ = marked_python(tmp_path)
    begun = time.monotonic()
    assert allowed(shim(bash("sed -i '' 's/a/b/' f"), py, f"{bins['slow']}:/usr/bin:/bin"))
    assert time.monotonic() - begun < 4.5


# --- the GNU form is cut from the original text ----------------------------------

@pytest.mark.parametrize(("command", "gnu"), [
    ("sed -i '' 's/a/b/' *.md", "sed -i 's/a/b/' *.md"),
    ('for f in a b; do sed -i \'\' \'s/a/b/\' "$f"; done',
     'for f in a b; do sed -i \'s/a/b/\' "$f"; done'),
    ("sed -i '' 's/a/b/' ~/notes.md", "sed -i 's/a/b/' ~/notes.md"),
    ('sed -i "" "s/x/$(date +%F)/" f', 'sed -i "s/x/$(date +%F)/" f'),
    ("sed -i  ''\t's/a/b/' f", "sed -i\t's/a/b/' f"),
    ("sed -i '' 's/a/b/' f && sed -i '' 's/c/d/' g", "sed -i 's/a/b/' f && sed -i 's/c/d/' g"),
], ids=["glob", "quoted-var", "tilde", "subst", "blanks", "twice"])
def test_the_gnu_form_keeps_every_other_byte(gnu_first, command, gnu):
    assert gnu_form_in(refused(gnu_first(command))) == gnu


def test_a_multi_line_command_shows_only_the_lines_it_cut(gnu_first):
    command = "cd d\ncat <<'EOF'\nsed -i '' kept in the body\nEOF\nsed -i '' 's/a/b/' f\necho done"
    assert gnu_form_in(refused(gnu_first(command))) == "sed -i 's/a/b/' f"


@pytest.mark.parametrize(("command", "gnu"), [
    ("sed -i \\\n '' 's/a/b/' f", "sed -i \\\n 's/a/b/' f"),
    ("sed -i \\\n'' 's/a/b/' f", "sed -i \\\n 's/a/b/' f"),
    ("cd d &&\\\n  sed -Ei \\\n    \"\" \\\n    's/a/b/' f\necho done",
     "cd d &&\\\n  sed -Ei \\\n \\\n    's/a/b/' f"),
], ids=["continued", "continued-no-blank", "continued-long"])
def test_a_continued_line_is_cut_and_shown_whole(gnu_first, command, gnu):
    # review fix: a backslash-newline between the flag and '' left the GNU
    # form shown with the '' still in it
    assert gnu_form_in(refused(gnu_first(command))) == gnu
    assert not G.bsd_sites(G.gnu_form(command))


def test_a_quoted_mention_beside_a_real_call_is_left_as_written():
    command = "echo \"sed -i '' x\" && sed -i '' 's/a/b/' f"
    assert G.gnu_form(command) == "echo \"sed -i '' x\" && sed -i 's/a/b/' f"


# --- heredocs: every delimiter quoting --------------------------------------------

@pytest.mark.parametrize("command", [
    "cat <<EOF\nsed -i '' x\nEOF",
    "cat <<'EOF'\nsed -i '' x\nEOF",
    "cat << 'EOF'\nsed -i '' x\nEOF",
    'cat <<"EOF"\nsed -i \'\' x\nEOF',
    "cat <<\\EOF\nsed -i '' x\nEOF",
    "cat <<-EOF\n\tsed -i '' x\n\tEOF",
    "cat <<'A' <<'B'\nsed -i '' x\nA\nsed -i '' y\nB",
    "message send x -s 's' -f /dev/stdin <<'EOF'\nUse sed -i '' and it breaks\nEOF\necho sent",
], ids=["bare", "single", "spaced", "double", "backslash", "dash-tabs", "two", "mail-body"])
def test_a_heredoc_body_holding_the_bsd_form_passes(gnu_first, command):
    assert allowed(gnu_first(command))


def test_a_here_string_is_not_a_heredoc(gnu_first):
    command = "cat <<< \"$x\"\nsed -i '' 's/a/b/' f"
    assert gnu_form_in(refused(gnu_first(command))) == "sed -i 's/a/b/' f"


def test_a_real_call_after_a_heredoc_is_refused(gnu_first):
    command = "cat <<'EOF' > n.md\nsed -i '' in a body\nEOF\nsed -i '' 's/a/b/' f"
    assert refused(gnu_first(command))


def test_strip_heredocs_keeps_the_operator_lines_and_drops_the_bodies():
    text = "a <<-'X' && b <<\"Y\"\n\tbody x\n\tX\nbody y\nY\nc"
    assert G.strip_heredocs(text) == "a <<-'X' && b <<\"Y\"\nc"
    assert G.strip_heredocs("a <<< x\nb") == "a <<< x\nb"
    assert G.strip_heredocs("cat <<E\nnever closed\nsed -i '' x") == "cat <<E"


# --- the parser table, in process -------------------------------------------------

def gnu(word: str) -> bool:
    return True


@pytest.mark.parametrize("command", [
    "sed -i '' 's/a/b/' f",
    "/usr/local/bin/sed -i '' x f",
    "xargs sed -i '' 's/a/b/'",
    "find . -name '*.md' -exec sed -i '' 's/a/b/' {} +",
    "env LC_ALL=C sed -i '' x f",
    "sed --in-place '' x f",
    "sed -ni '' x f",
    "sed 's/a/b/' -i '' f",
    "echo hi\nsed -i '' x f",
    "(sed -i '' x f)",
    "true; sed -i '' x f",
    "a | sed -i '' x f",
    "sed -i \\\n '' x f",
    "sed -i '' x f # a comment after",
    "echo sed -i '' x",
], ids=["plain", "path", "xargs", "find-exec", "env", "long", "cluster-n", "after-script",
        "newline", "subshell", "semicolon", "pipe", "continued", "trailing-comment",
        "unquoted-echo-accepted"])
def test_decide_refuses(command):
    assert G.decide(command, is_gnu=gnu)


@pytest.mark.parametrize("command", [
    "sed -i'' x f",
    'sed -i"" x f',
    "sed -i x f",
    "sed -e '' f",
    "sed -n '' f",
    "grep -i '' f",
    "echo \"sed -i ''\"",
    "echo 'sed -i \"\" f'",
    "# sed -i '' x f",
    "sed -i x f; grep -i '' g",
    "sed -i 'unbalanced",
    "",
    "ls",
], ids=["glued-single", "glued-double", "suffixless", "dash-e-empty", "dash-n-empty",
        "grep", "quoted", "quoted-double", "comment", "other-command-split", "unbalanced",
        "empty", "no-sed"])
def test_decide_allows(command):
    assert G.decide(command, is_gnu=gnu) is None


def test_decide_allows_when_the_sed_is_not_gnu():
    assert G.decide("sed -i '' x f", is_gnu=lambda word: False) is None


def test_decide_probes_each_sed_word_once():
    asked: list[str] = []
    G.decide("sed -i '' x f; sed -i '' y g", is_gnu=lambda word: asked.append(word) or False)
    assert asked == ["sed"]


# --- known misses: each fails open (design, Rejected options) ---------------------

def test_not_caught_a_mid_word_hash_starts_a_comment():
    """shlex starts a comment at `$#` and drops the rest: missed, and allowed."""
    assert G.decide("echo $#; sed -i '' x f", is_gnu=gnu) is None


def test_not_caught_a_bash_dash_c_string():
    """`bash -c '...'` is not looked into (design step 3, nice to have)."""
    assert G.decide("bash -c \"sed -i '' x f\"", is_gnu=gnu) is None


# --- the off switch ---------------------------------------------------------------

@pytest.mark.parametrize("value", ["off", "OFF", "Off", "0", "false", "False", "no", "NO"])
def test_the_switch_off_passes_and_starts_no_python(gnu_first, value):
    assert allowed(gnu_first("sed -i '' 's/a/b/' f", SESSIONTOOLS_HOOK_SED=value))
    assert starts(gnu_first.mark) == 0


@pytest.mark.parametrize("value", ["", "on", "1", "yes", " off", "anything"])
def test_any_other_switch_value_leaves_it_on(gnu_first, value):
    assert refused(gnu_first("sed -i '' 's/a/b/' f", SESSIONTOOLS_HOOK_SED=value))


def test_main_honours_the_switch_in_process():
    out = io.StringIO()
    body = json.dumps(bash("gsed -i '' x f"))
    assert G.main(io.StringIO(body), out, {"SESSIONTOOLS_HOOK_SED": "off"}) == 0
    assert out.getvalue() == ""
    assert G.main(io.StringIO(body), out, {}) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- cheap: no python start without sed or an empty quote pair --------------------

@pytest.mark.parametrize("command", ["ls -la", "git status", "echo ''", 'printf ""'],
                         ids=["plain", "git", "empty-single", "empty-double"])
def test_a_payload_with_no_sed_starts_no_python(tmp_path, bins, command):
    py, mark = marked_python(tmp_path)
    body = bash(command, cwd="/tmp/work")
    body["tool_input"]["description"] = "List files"
    assert allowed(shim(body, py, f"{bins['gnu']}:/usr/bin:/bin"))
    assert starts(mark) == 0


@pytest.mark.parametrize("command", ["sed -i 's/a/b/' f", "sed -n '1p' f", "sed -i.bak 's/x/y/' f"])
def test_a_sed_with_no_empty_quote_pair_starts_no_python(tmp_path, bins, command):
    py, mark = marked_python(tmp_path)
    assert allowed(shim(bash(command), py, f"{bins['gnu']}:/usr/bin:/bin"))
    assert starts(mark) == 0


def test_a_bsd_form_starts_python_once(gnu_first):
    assert refused(gnu_first("sed -i '' 's/a/b/' f"))
    assert starts(gnu_first.mark) == 1


# --- fails open and silent --------------------------------------------------------

@pytest.mark.parametrize("body", ["#!/bin/sh\nexit 1\n", "#!/bin/sh\nkill -9 $$\n",
                                  "#!/bin/sh\necho 'Unknown option: -I' >&2\nexit 2\n"],
                         ids=["crash", "killed", "python2"])
def test_a_failing_python_passes_silently(tmp_path, bins, body):
    fake = w(tmp_path / "bad" / "python", body)
    assert allowed(shim(bash("sed -i '' 's/a/b/' f"), fake, f"{bins['gnu']}:/usr/bin:/bin"))


def test_no_python_at_all_passes_silently(tmp_path, bins):
    result = shim(bash("sed -i '' 's/a/b/' f"), tmp_path / "missing" / "python",
                  f"{bins['gnu']}:{tmp_path / 'nothing'}")
    assert allowed(result)


def test_main_swallows_a_crash(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(G, "decide", boom)
    out = io.StringIO()
    assert G.main(io.StringIO(json.dumps(bash("sed -i '' x f"))), out, {}) == 0
    assert out.getvalue() == ""


def test_the_payload_reaches_python_byte_for_byte(gnu_first):
    """Backslashes, `$` and a multi-line JSON payload survive the shim's
    read loop and heredoc."""
    command = "sed -i '' 's/\\\\n/$HOME `x`/' f"
    body = json.dumps(bash(command), indent=2)
    assert gnu_form_in(refused(gnu_first(body, raw=True))) == "sed -i 's/\\\\n/$HOME `x`/' f"


# --- hooks.json registers it and says what it does --------------------------------

def test_hooks_json_registers_the_guard_on_bash_pretooluse():
    entries = json.loads(HOOKS.read_text())["hooks"]["PreToolUse"]
    assert {"matcher": "Bash", "hooks": [{
        "type": "command",
        "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/sed-guard"',
        "timeout": 5}]} in entries
    assert [e["matcher"] for e in entries] == ["*", "Bash", "Bash"]   # busy-sound's entry untouched


def test_hooks_json_says_what_the_guard_does():
    description = json.loads(HOOKS.read_text())["description"]
    guard = description[description.index("sed-guard, the sed guard"):]
    guard = guard[:guard.index("(hooks/sed-guard, sessiontools.sed_guard)")]
    for word in ("ON BY DEFAULT", "sed -i ''", "GNU", "PARSES", "heredoc", "--version",
                 "/usr/bin/sed", "permissionDecision: deny", "2.1.325", "bypassPermissions",
                 "original text", f"{G.SWITCH_ENV}=off", "starts no python", "fails OPEN"):
        assert word in guard, word


# --- the shim, statically --------------------------------------------------------

def test_the_shim_exists_is_executable_and_posix_sh():
    assert SHIM.is_file()
    assert SHIM.stat().st_mode & stat.S_IXUSR
    assert SHIM.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run(["/bin/sh", "-n", str(SHIM)], check=False).returncode == 0


def test_the_shim_sources_the_one_picker_and_keeps_no_copy():
    text = SHIM.read_text()
    assert '. "$here/pick-python.sh"' in text
    assert "pick() {" not in text
    assert "SESSIONTOOLS_PYTHON:-" not in text


def test_the_shim_never_runs_a_bare_python3():
    code = [line for line in SHIM.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert not any(re.search(r"(^|[\s;|&(])python3\b", line) for line in code), code


def test_the_variable_names_agree_between_the_shim_and_the_module():
    text = SHIM.read_text()
    assert G.SWITCH_ENV in text and "sessiontools.sed_guard" in text


def test_the_module_never_writes_a_settings_file():
    """Plugin only: nothing here touches ~/.claude/settings.json or any
    global file (README Requirement 1)."""
    for source in (MODULE, SHIM):
        text = source.read_text()
        assert "settings.json" not in text
        assert not re.search(r"(?<![A-Za-z])open\(|write_text|write_bytes", text)
