"""The ledger's counter (ST-2 design §2): one uv child per report, exit 2
for every way it cannot count, no declared dependency, no network act.

The refusals are tested with `run` stubbed to RAISE, so a test that wrongly
starts a child fails loudly instead of fetching anything. The real child is
tested under `requires_counter`.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from test_ledger_support import FIXTURES, REAL_ENV, real_counter, requires_counter

from sessiontools.ledger import count

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "sessiontools"


def _no_child(*a, **k):
    raise AssertionError("a child was started")


def _cache(tmp_path: Path) -> Path:
    d = tmp_path / "tiktoken"
    d.mkdir(exist_ok=True)
    (d / count.VOCAB_FILE).write_bytes(b"stand-in: the stubbed child never reads it")
    return d


def _done(stdout: str | bytes, rc: int = 0, stderr: bytes = b""):
    out = stdout.encode() if isinstance(stdout, str) else stdout
    return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, out, stderr)


def _counter(tmp_path, run, which=lambda name, path=None: "/stub/uv", **env):
    base = {"PATH": "/stub", "HOME": str(tmp_path / "home"), count.CACHE_ENV: str(_cache(tmp_path))}
    return count.TiktokenCounter(env={**base, **env}, run=run, which=which)


# --- the argv and the environment ------------------------------------------------

def test_the_child_argv_is_no_project_offline_quiet_isolated():
    argv = count.child_argv("/x/uv")
    assert argv[:2] == ["/x/uv", "run"]
    assert "--no-project" in argv and "--offline" in argv and "--quiet" in argv
    assert argv[argv.index("--with") + 1] == "tiktoken"
    assert argv[-3:] == ["python", "-I", str(count.CHILD)]
    assert count.CHILD.name == "_tiktoken_child.py" and count.CHILD.is_file()


def test_the_child_is_pinned_to_the_running_interpreter(monkeypatch):
    """`--python sys.executable`: neither the cwd's `.python-version` nor the
    host's default Python chooses the child, so the probe (run from the
    repo root) and a count run from any other directory agree. A named
    interpreter wins; an embedded one with no `sys.executable` gets no
    `--python` at all (`--python ""` is a uv usage error)."""
    argv = count.child_argv("/x/uv")
    assert argv[argv.index("--python") + 1] == sys.executable
    assert argv.index("--python") < argv.index("--with")
    named = count.child_argv("/x/uv", python="/y/bin/python")
    assert named[named.index("--python") + 1] == "/y/bin/python"
    monkeypatch.setattr(count.sys, "executable", "")
    assert "--python" not in count.child_argv("/x/uv")


def test_the_counter_runs_the_pinned_child(tmp_path):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"[1]", b"")

    assert _counter(tmp_path, run).count(["x"]) == [1]
    assert seen[0][seen[0].index("--python") + 1] == sys.executable


def test_the_parent_passes_TIKTOKEN_CACHE_DIR_into_the_child(tmp_path):
    """Set, never left to tiktoken's own default ($TMPDIR/data-gym-cache),
    even when the caller's environment leaves it unset (second review)."""
    home = tmp_path / "home"
    (home / ".cache" / "tiktoken").mkdir(parents=True)
    (home / ".cache" / "tiktoken" / count.VOCAB_FILE).write_bytes(b"x")
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, b"[1, 2]", b"")

    c = count.TiktokenCounter(env={"PATH": "/stub", "HOME": str(home), "VIRTUAL_ENV": "/v"},
                              run=run, which=lambda n, path=None: "/stub/uv")
    assert c.count(["a", "b c"]) == [1, 2]
    assert seen["env"][count.CACHE_ENV] == str(home / ".cache" / "tiktoken")
    assert "VIRTUAL_ENV" not in seen["env"]
    assert json.loads(seen["input"]) == ["a", "b c"]


def test_one_child_for_the_whole_list_and_none_for_an_empty_one(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(json.loads(kw["input"]))
        return subprocess.CompletedProcess(argv, 0, b"[3, 4, 5]", b"")

    c = _counter(tmp_path, run)
    assert c.count(["x", "y", "z"]) == [3, 4, 5]
    assert calls == [["x", "y", "z"]]
    assert _counter(tmp_path, _no_child).count([]) == []


def test_the_payload_is_ascii_so_no_locale_can_mangle_it(tmp_path):
    seen = {}

    def run(argv, **kw):
        seen["input"] = kw["input"]
        return subprocess.CompletedProcess(argv, 0, b"[1]", b"")

    _counter(tmp_path, run).count(["café \U0001f98a"])
    seen["input"].decode("ascii")


# --- exit 2, never zeros, never chars/4 -------------------------------------------

def test_no_vocabulary_is_exit_2_with_no_child_started(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    c = count.TiktokenCounter(env={"PATH": "/stub", count.CACHE_ENV: str(empty)},
                              run=_no_child, which=lambda n, path=None: "/stub/uv")
    with pytest.raises(count.CounterError) as e:
        c.count(["anything"])
    assert e.value.code == 2
    assert "no cl100k_base vocabulary" in str(e.value) and "never downloaded" in str(e.value)
    assert "run `tokens` once" in str(e.value)


def test_no_uv_is_exit_2_with_no_child_started(tmp_path):
    c = _counter(tmp_path, _no_child, which=lambda n, path=None: None)
    with pytest.raises(count.CounterError) as e:
        c.count(["anything"])
    assert e.value.code == 2 and "no `uv` on the PATH" in str(e.value)


def test_a_cold_uv_cache_is_exit_2_named(tmp_path):
    err = (b"error: No solution found when resolving `--with` dependencies\n"
           b"  cause: Because tiktoken was not found in the cache and you require tiktoken\n")
    with pytest.raises(count.CounterError) as e:
        _counter(tmp_path, _done("", rc=1, stderr=err)).count(["x"])
    assert e.value.code == 2 and "cold uv cache" in str(e.value)
    assert "run `tokens` once" in str(e.value)


def test_a_child_that_cannot_start_is_exit_2(tmp_path):
    def run(argv, **kw):
        raise FileNotFoundError(argv[0])

    with pytest.raises(count.CounterError) as e:
        _counter(tmp_path, run).count(["x"])
    assert e.value.code == 2


def test_a_hung_child_is_exit_2(tmp_path):
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    with pytest.raises(count.CounterError):
        _counter(tmp_path, run).count(["x"])


@pytest.mark.parametrize("reply", ["[1]", "[1, 2, 3]", "not json", "{}", "[1, -2]",
                                   "[1, true]", "[1, 2.0]", ""])
def test_a_bad_child_reply_is_exit_2_never_a_partial_report(tmp_path, reply):
    with pytest.raises(count.CounterError) as e:
        _counter(tmp_path, _done(reply)).count(["a", "b"])
    assert e.value.code == 2 and "bad reply" in str(e.value)


def test_a_child_exit_is_exit_2_with_its_stderr(tmp_path):
    with pytest.raises(count.CounterError) as e:
        _counter(tmp_path, _done("", rc=3, stderr=b"no cl100k_base vocabulary in X\n")).count(["a"])
    assert e.value.code == 2 and "exited 3" in str(e.value) and "vocabulary in X" in str(e.value)


# --- no declared dependency; tiktoken only in the child -----------------------------

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_no_package_module_but_the_child_imports_tiktoken():
    importers = sorted(p.relative_to(PKG).as_posix() for p in PKG.rglob("*.py")
                       if "tiktoken" in _imports(p))
    assert importers == ["ledger/_tiktoken_child.py"]


def test_the_child_imports_nothing_from_sessiontools_and_names_the_same_vocabulary():
    assert "sessiontools" not in _imports(count.CHILD)
    tree = ast.parse(count.CHILD.read_text(encoding="utf-8"))
    consts = {t.id: n.value.value for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Constant)}
    assert consts["VOCAB_FILE"] == count.VOCAB_FILE


def test_the_ledger_does_not_import_window_scan():
    """ST-2's own reader: it does not import, extend or wrap window.scan (design §1)."""
    for p in (PKG / "ledger").glob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert "window" not in _imports(p) and "sessiontools.window" not in src, p.name
        assert "from sessiontools import window" not in src, p.name


def test_import_sessiontools_and_the_ledger_stay_stdlib_only():
    # Count only what the import ADDS: a venv's own startup modules (a uv
    # _virtualenv.pth, setuptools' _distutils_hack) are loaded by site before
    # -c runs, so they sit in the snapshot and are not the package's doing.
    code = ("import sys\n"
            "before = set(sys.modules)\n"
            "import sessiontools, sessiontools.ledger, sessiontools.ledger.count, "
            "sessiontools.ledger.transcript\n"
            "added = {m.split('.')[0] for m in set(sys.modules) - before}\n"
            "print(sorted(added - set(sys.stdlib_module_names) - {'sessiontools'}))\n")
    got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60, check=True)
    assert got.stdout.strip() == "[]", got.stdout + got.stderr


def test_no_dependency_is_declared_and_uv_lock_has_no_tiktoken():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == ["PyYAML>=6", "jsonschema>=4"]
    assert 'name = "tiktoken"' not in (ROOT / "uv.lock").read_text(encoding="utf-8")


# --- the real child -------------------------------------------------------------

@requires_counter
def test_the_real_child_counts_POLICY_at_26878_and_its_prefixed_form_at_32591():
    text = (FIXTURES / "POLICY-synthetic.md").read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    prefixed = "".join(f"{i}\t{line}" for i, line in enumerate(lines, 1))
    assert real_counter().count([text, prefixed]) == [26878, 32591]


@requires_counter
def test_the_real_child_counts_endoftext_as_text_and_survives_a_lone_surrogate():
    got = real_counter().count(["<|endoftext|>", "half \ud83e an emoji", ""])
    assert got[0] == 7 and got[1] > 0 and got[2] == 0


@requires_counter
def test_a_run_from_a_project_dir_writes_nothing_there(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\n'
                                         'dependencies = ["not-a-real-package-zz"]\n')
    before = sorted(p.name for p in proj.iterdir())
    monkeypatch.chdir(proj)
    assert real_counter().count(["hello world"]) == [2]
    assert sorted(p.name for p in proj.iterdir()) == before


@requires_counter
def test_a_real_cold_uv_cache_is_exit_2_not_a_fetch(tmp_path):
    env = {**REAL_ENV, "UV_CACHE_DIR": str(tmp_path / "cold-uv")}
    c = count.TiktokenCounter(env={**__import__("os").environ, **env})
    with pytest.raises(count.CounterError) as e:
        c.count(["x"])
    assert e.value.code == 2 and "cold uv cache" in str(e.value)
