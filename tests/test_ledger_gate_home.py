"""The ledger gates find their vocabulary under any HOME, and a skip is loud.

A check-runner gate runs the suite under a scratch HOME; the 64 token-count
gates looked only at `$HOME/.cache/tiktoken` and skipped, quietly. These pin
the search order (fabricated env and passwd homes, never the host's) and the
banner (its pieces, and a real pytest run in a subprocess).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import _scanlist
import conftest
import pytest
import test_ledger_support as support
from ledger_skip_support import TAG, banner_lines, skip_reason
from test_ledger_support import find_vocab, passwd_home, requires_counter, vocab_dirs

from sessiontools.ledger import count

TESTS = Path(__file__).resolve().parent


def cache(home: Path, *, vocab: bool) -> Path:
    """`home/.cache/tiktoken`, made, holding the vocabulary file or not."""
    d = home / ".cache" / "tiktoken"
    d.mkdir(parents=True)
    if vocab:
        (d / count.VOCAB_FILE).write_bytes(b"fake vocabulary")
    return d


# --- (A) the search -------------------------------------------------------


def test_the_search_order_is_env_then_home_then_passwd(tmp_path):
    env = {count.CACHE_ENV: str(tmp_path / "env"), "HOME": str(tmp_path / "home")}
    assert vocab_dirs(env, tmp_path / "pw") == [
        tmp_path / "env", tmp_path / "home" / ".cache" / "tiktoken",
        tmp_path / "pw" / ".cache" / "tiktoken"]


def test_TIKTOKEN_CACHE_DIR_wins_when_it_holds_the_file(tmp_path):
    own = tmp_path / "own"
    own.mkdir()
    (own / count.VOCAB_FILE).write_bytes(b"x")
    cache(tmp_path / "home", vocab=True)
    cache(tmp_path / "pw", vocab=True)
    env = {count.CACHE_ENV: str(own), "HOME": str(tmp_path / "home")}
    assert find_vocab(env, tmp_path / "pw")[0] == own


def test_an_empty_TIKTOKEN_CACHE_DIR_falls_through_to_HOME(tmp_path):
    (tmp_path / "own").mkdir()
    home = cache(tmp_path / "home", vocab=True)
    cache(tmp_path / "pw", vocab=True)
    env = {count.CACHE_ENV: str(tmp_path / "own"), "HOME": str(tmp_path / "home")}
    assert find_vocab(env, tmp_path / "pw")[0] == home


def test_a_scratch_HOME_without_the_file_finds_the_passwd_homes(tmp_path):
    """The gate's case: HOME redirected to an empty scratch dir, the vocabulary
    where `tokens` put it, under the real (passwd) home."""
    cache(tmp_path / "scratch", vocab=False)
    real = cache(tmp_path / "pw", vocab=True)
    found, looked = find_vocab({"HOME": str(tmp_path / "scratch")}, tmp_path / "pw")
    assert found == real
    assert looked == [tmp_path / "scratch" / ".cache" / "tiktoken", real]


def test_no_vocabulary_anywhere_names_every_place_looked(tmp_path):
    env = {count.CACHE_ENV: str(tmp_path / "env"), "HOME": str(tmp_path / "home"),
           "PATH": os.environ["PATH"]}
    found, looked = find_vocab(env, tmp_path / "pw")
    assert found is None and len(looked) == 3
    extra, why = support._probe(env, tmp_path / "pw")
    assert extra == {}
    for d in looked:
        assert str(d / count.VOCAB_FILE) in why
    assert why.index(str(tmp_path / "env")) < why.index(str(tmp_path / "home")) \
        < why.index(str(tmp_path / "pw"))


def test_the_search_only_looks(tmp_path):
    """Read-only: no candidate dir is made, nothing lands anywhere."""
    env = {count.CACHE_ENV: str(tmp_path / "env"), "HOME": str(tmp_path / "home")}
    assert find_vocab(env, tmp_path / "pw")[0] is None
    assert list(tmp_path.iterdir()) == []


def test_a_place_named_twice_is_looked_in_once(tmp_path):
    """HOME unredirected is the passwd home: one dir, listed once."""
    assert vocab_dirs({"HOME": str(tmp_path)}, tmp_path) == [tmp_path / ".cache" / "tiktoken"]


def test_unset_and_empty_variables_are_not_places(tmp_path):
    assert vocab_dirs({count.CACHE_ENV: "", "HOME": ""}, tmp_path) == [
        tmp_path / ".cache" / "tiktoken"]
    assert vocab_dirs({}, None) == []
    assert find_vocab({}, None) == (None, [])


def test_passwd_home_is_this_uids_entry_and_none_without_one(monkeypatch):
    import pwd
    assert passwd_home() == Path(pwd.getpwuid(os.getuid()).pw_dir)

    def no_entry(uid):
        raise KeyError(uid)

    monkeypatch.setattr(support.pwd, "getpwuid", no_entry)
    assert passwd_home() is None


def test_production_cache_dir_keeps_its_HOME_semantics(tmp_path):
    """The fix is the tests' search only: `count.cache_dir` (shared with
    `tokens`) still reads `$TIKTOKEN_CACHE_DIR`, else `$HOME`, never passwd."""
    cache(tmp_path / "pw", vocab=True)
    assert count.cache_dir({"HOME": str(tmp_path / "scratch")}) == \
        tmp_path / "scratch" / ".cache" / "tiktoken"


@requires_counter
def test_a_check_runner_env_finds_the_passwd_vocabulary_and_runs_the_child():
    """The gate's env, as taskmaster's check runner builds it: HOME a scratch
    dir (conftest's own empty one here), no TIKTOKEN_CACHE_DIR, uv's two dirs
    passed through. The probe finds the passwd home's vocabulary, runs the
    real child, and hands the child that dir."""
    real = passwd_home() / ".cache" / "tiktoken"
    if not (real / count.VOCAB_FILE).is_file():
        pytest.skip("this host keeps its vocabulary only in TIKTOKEN_CACHE_DIR")
    scratch = Path(os.environ["HOME"])
    assert not (scratch / ".cache" / "tiktoken" / count.VOCAB_FILE).exists()
    env = {k: v for k, v in os.environ.items() if k != count.CACHE_ENV}
    env.update({k: v for k, v in support.REAL_ENV.items() if k.startswith("UV_")})
    extra, why = support._probe(env)
    assert why is None
    assert extra[count.CACHE_ENV] == str(real)


# --- (B) the banner -------------------------------------------------------


def skipped(reason: str):
    return SimpleNamespace(longrepr=("tests/x.py", 1, f"Skipped: {reason}"))


def test_every_requires_counter_reason_carries_the_tag():
    assert requires_counter.kwargs["reason"].startswith(TAG)


def test_skip_reason_reads_the_text_and_tolerates_odd_reports():
    assert skip_reason(skipped("why")) == "why"
    assert skip_reason(SimpleNamespace(longrepr=("p", 1, "bare"))) == "bare"
    assert skip_reason(SimpleNamespace(longrepr="a string")) is None
    assert skip_reason(SimpleNamespace(longrepr=None)) is None
    assert skip_reason(object()) is None


def test_no_ledger_skip_no_banner():
    assert banner_lines([]) == []
    assert banner_lines([skipped("no tmux here"), skipped("macOS only")]) == []


def test_the_banner_counts_ledger_skips_only_and_names_the_reason():
    why = f"{TAG}: no cl100k_base vocabulary in any of: /a, /b"
    lines = banner_lines([skipped(why)] * 3 + [skipped("no tmux here")])
    assert lines == [f"{TAG}: 3 ledger token-count tests SKIPPED", f"3 x {why}"]
    assert banner_lines([skipped(why)])[0] == f"{TAG}: 1 ledger token-count test SKIPPED"


def test_two_reasons_are_both_named_most_first():
    a, b = f"{TAG}: one", f"{TAG}: two"
    assert banner_lines([skipped(a), skipped(b), skipped(b)]) == [
        f"{TAG}: 3 ledger token-count tests SKIPPED", f"2 x {b}", f"1 x {a}"]


class _Reporter:
    """The two calls a terminal-summary hook makes, recorded."""

    def __init__(self, skipped_reports):
        self.stats = {"skipped": skipped_reports}
        self.out: list[str] = []

    def write_sep(self, sep, title, **_markup):
        self.out.append(title)

    def write_line(self, line, **_markup):
        self.out.append(line)


def test_conftest_wires_the_hook():
    """conftest's hook writes the ledger banner, then the private-word guards'
    count: both loud skips reach every run's summary."""
    reporter = _Reporter([skipped(f"{TAG}: no vocabulary"), skipped(_scanlist.SKIP)])
    conftest.pytest_terminal_summary(reporter)
    text = "\n".join(reporter.out)
    assert f"{TAG}: 1 ledger token-count test SKIPPED" in text
    assert "private scan list: 1 guard tests skipped" in text
    assert text.index(TAG) < text.index("private scan list: 1")
    quiet = _Reporter([skipped("no tmux here")])
    conftest.pytest_terminal_summary(quiet)
    assert quiet.out == []


def inner_env() -> dict[str, str]:
    """The caller's env with every `PYTEST_*` dropped, so the inner run's output
    is pytest's own: an outer `PYTEST_ADDOPTS=-rs` would add a short-summary
    line, and a runner's `--junitxml` would point both runs at one file. Plugin
    autoload is off, so an installed plugin cannot reshape it either."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def run_pytest(where: Path, body: str) -> subprocess.CompletedProcess:
    """A real `pytest -q` over one test file, with the real hook in its conftest."""
    (where / "conftest.py").write_text(textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {str(TESTS)!r})
        from ledger_skip_support import pytest_terminal_summary  # noqa: F401
        """), encoding="utf-8")
    (where / "test_inner.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--rootdir", str(where), str(where)],
        cwd=where, env=inner_env(), capture_output=True, text=True, timeout=120,
        check=False)


@pytest.mark.parametrize("addopts", [None, "-rs", "-ra"])
def test_a_run_with_ledger_skips_ends_with_one_banner(tmp_path, monkeypatch, addopts):
    """The same banner whatever `PYTEST_ADDOPTS` the caller runs under."""
    if addopts is None:
        monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    else:
        monkeypatch.setenv("PYTEST_ADDOPTS", addopts)
    got = run_pytest(tmp_path, f"""\
        import pytest
        gate = pytest.mark.skipif(True, reason={TAG + ": no vocabulary at /x"!r})

        @gate
        @pytest.mark.parametrize("n", range(3))
        def test_gate(n): pass

        @pytest.mark.skip(reason="no tmux here")
        def test_other(): pass

        def test_ok(): pass
        """)
    assert got.returncode == 0, got.stdout + got.stderr
    tail = got.stdout.strip().splitlines()
    assert tail[-1].startswith("1 passed, 4 skipped")
    banner = [line for line in tail if TAG in line]
    assert len(banner) == 2, tail
    assert f"{TAG}: 3 ledger token-count tests SKIPPED" in banner[0]
    assert banner[0].startswith("!")
    assert banner[1] == f"3 x {TAG}: no vocabulary at /x"
    assert "a ledger regression would NOT fail this run" in tail[-2]
    assert "no tmux here" not in got.stdout


def test_a_run_without_ledger_skips_has_no_banner(tmp_path):
    got = run_pytest(tmp_path, """\
        import pytest

        @pytest.mark.skip(reason="no tmux here")
        def test_other(): pass

        def test_ok(): pass
        """)
    assert got.returncode == 0, got.stdout + got.stderr
    assert TAG not in got.stdout
    assert "ledger regression" not in got.stdout


@pytest.mark.parametrize("outcome", ["fail", "pass"])
def test_the_banner_does_not_change_the_exit_status(tmp_path, outcome):
    """Loud, not failing: a host with no vocabulary anywhere may still pass."""
    got = run_pytest(tmp_path, f"""\
        import pytest

        @pytest.mark.skipif(True, reason={TAG + ": none"!r})
        def test_gate(): pass

        def test_it(): assert {outcome == "pass"}
        """)
    assert got.returncode == (0 if outcome == "pass" else 1), got.stdout
    assert f"{TAG}: 1 ledger token-count test SKIPPED" in got.stdout
