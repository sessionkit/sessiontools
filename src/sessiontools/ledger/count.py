"""Texts in, token counts out: the ledger's counter.

    from sessiontools.ledger import count
    c = count.TiktokenCounter()
    c.count(["some text", "more"])   # -> [2, 1], one child for the whole list
    c.label                          # 'tiktoken cl100k_base, ±10% estimate'

THE METHOD. tiktoken `cl100k_base`, the backend of the skillbox `tokens`
command, run over the RAW text (front matter
included, never `tokens`' YAML rewrite). Every figure is the encoder run
over the text it describes: never chars ÷ N, never a fitted rate, never a
zero standing in for "could not count".

HOW IT RUNS: no declared
dependency. ONE child per report,

    uv run --no-project --offline --quiet --python <this interpreter> \
        --with tiktoken python -I _tiktoken_child.py

with every text written to its stdin as one JSON array. `--offline` keeps
the default path free of any network act (a cold uv cache makes uv fail,
not fetch); `--no-project` keeps uv from syncing whatever project the
caller's cwd sits in (it wrote a `.venv` there without it). `--python`
pins the child to the running interpreter (`sys.executable`), so neither
the cwd's `.python-version` nor the host's default Python chooses it.

EXIT 2, NEVER A PARTIAL REPORT. No `uv`, no vocabulary file, a cold uv
cache, a child that fails or replies with the wrong shape: each raises
`CounterError` (code 2) naming the one-time fix. The vocabulary is checked
HERE, before any child starts, because tiktoken would download it; and
`TIKTOKEN_CACHE_DIR` is passed into the child's environment, because a seat
shell leaves it unset and tiktoken's own default (`$TMPDIR/data-gym-cache`)
is not where `tokens` keeps it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

#: Printed in every view's header and carried on every JSON record.
LABEL = "tiktoken cl100k_base, ±10% estimate"

#: tiktoken's cache file for cl100k_base (sha1 of its URL), 1,681,126 bytes.
VOCAB_FILE = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"

CACHE_ENV = "TIKTOKEN_CACHE_DIR"

#: The child script, shipped inside the package and run, never imported.
CHILD = Path(__file__).with_name("_tiktoken_child.py")

#: The one-time fix every refusal names.
FIX = ("run `tokens` once on this host (it fills the uv and tiktoken caches), "
       f"or set {CACHE_ENV} to a directory holding {VOCAB_FILE}")

#: A child counting a whole transcript takes well under a second (0.17 s
#: for a policy file); this only bounds a hung one.
TIMEOUT_S = 300


class CounterError(Exception):
    """The counter could not count. `code` is the exit status: always 2."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


class Counter(Protocol):
    """Anything that turns texts into token counts, one per text, in order.
    An exact counter (count_tokens) would be one more of these."""

    label: str

    def count(self, texts: Sequence[str]) -> list[int]: ...


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$TIKTOKEN_CACHE_DIR`, else `~/.cache/tiktoken`: the `setdefault` the
    `tokens` command makes, so the two share one vocabulary file."""
    env = os.environ if env is None else env
    if env.get(CACHE_ENV):
        return Path(env[CACHE_ENV])
    home = env.get("HOME") or os.path.expanduser("~")
    return Path(home) / ".cache" / "tiktoken"


def child_argv(uv: str, python: str | None = None) -> list[str]:
    """The child's command line; a test pins `--no-project` and `--offline`.
    `--python` names `python`, else this interpreter: the running one, so
    the cwd or the host default never chooses the child's Python, and a
    probe run from one directory agrees with a count run from another. An
    embedded interpreter may have no `sys.executable`; then uv chooses."""
    python = python or sys.executable
    pin = ["--python", python] if python else []
    return [uv, "run", "--no-project", "--offline", "--quiet", *pin, "--with", "tiktoken",
            "python", "-I", str(CHILD)]


def child_env(env: Mapping[str, str], cache: Path) -> dict[str, str]:
    """The caller's environment, with the vocabulary's directory SET (never
    left to tiktoken's default) and no active virtualenv for uv to adopt."""
    out = dict(env)
    out[CACHE_ENV] = str(cache)
    out.pop("VIRTUAL_ENV", None)
    return out


class TiktokenCounter:
    """`cl100k_base` in one `uv run` child per `count()` call.

    `run` and `which` are the seams the tests stub: `run` is called exactly
    once per non-empty `count()`, and not at all when a check refuses first.
    """

    label = LABEL

    def __init__(self, env: Mapping[str, str] | None = None,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 which: Callable[..., str | None] = shutil.which):
        self.env = dict(os.environ if env is None else env)
        self._run = run
        self._which = which

    def check(self) -> tuple[str, Path]:
        """`(uv, cache)`, or CounterError before any child is started."""
        cache = cache_dir(self.env)
        if not (cache / VOCAB_FILE).is_file():
            raise CounterError(f"token counter: no cl100k_base vocabulary at "
                               f"{cache / VOCAB_FILE}; it is never downloaded here. {FIX}")
        uv = self._which("uv", path=self.env.get("PATH"))
        if not uv:
            raise CounterError(f"token counter: no `uv` on the PATH; the counter runs "
                               f"tiktoken through it. Install uv, then {FIX}")
        return uv, cache

    def count(self, texts: Sequence[str]) -> list[int]:
        texts = list(texts)
        if not texts:
            return []
        uv, cache = self.check()
        payload = json.dumps(texts).encode("ascii")   # ensure_ascii: no locale in the way
        try:
            proc = self._run(child_argv(uv), input=payload, capture_output=True,
                             env=child_env(self.env, cache), timeout=TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            raise CounterError(f"token counter: could not run {uv}: {e}. {FIX}") from e
        if proc.returncode != 0:
            err = _text(proc.stderr)
            lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
            why = " / ".join(lines[:3]) if lines else "(no stderr)"
            cold = (" (a cold uv cache: tiktoken is not cached, and --offline never fetches it)"
                    if "not found in the cache" in err or "network was disabled" in err else "")
            raise CounterError(f"token counter: the tiktoken child exited "
                               f"{proc.returncode}{cold}: {why}. {FIX}")
        try:
            counts = json.loads(_text(proc.stdout))
        except ValueError:
            counts = None
        if (not isinstance(counts, list) or len(counts) != len(texts)
                or not all(type(n) is int and n >= 0 for n in counts)):
            raise CounterError(f"token counter: a bad reply from the tiktoken child "
                               f"(expected {len(texts)} counts): {_text(proc.stdout)[:200]!r}")
        return counts


def _text(b: bytes | str | None) -> str:
    if b is None:
        return ""
    return b if isinstance(b, str) else b.decode("utf-8", "replace")
