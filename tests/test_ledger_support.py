"""Shared helpers for the ledger's tests (ST-2), and the ONE skip object.

Not a test module, though pytest collects it (it holds no tests): the
slice's file list allows new files only as `tests/test_ledger_*.py`, and
`tests/conftest.py` is not ours to edit (the no-file-overlap rule).

`requires_counter` is a shared `pytest.mark.skipif` OBJECT, the project's
own pattern (`NO_TMUX` in test_walled_off.py), never a custom marker:
pyproject sets `--strict-markers`, and registering one means editing a file
this slice may not touch (decompose-001 review). Its
reason is loud on purpose: a skipped gate is a gate that did not run, and
verify-001 asserts zero skips on the build Mac.

THE REAL CACHES ARE CAPTURED AT IMPORT. `conftest.py`'s autouse wall sets
`HOME` to an empty tmp dir for every test, which would leave uv's package
cache and tiktoken's vocabulary cold. Collection happens before that
fixture runs, so the real paths are read here, once, and `real_counter()`
passes them into the child explicitly: `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`
and `TIKTOKEN_CACHE_DIR`. Nothing else about the wall changes.

THE VOCABULARY IS FOUND UNDER ANY HOME (ST-178). A check-runner gate runs
the whole suite under a scratch `HOME` too, before collection, so
`$HOME/.cache/tiktoken` is empty there and all 64 gates skipped. `find_vocab`
looks in `$TIKTOKEN_CACHE_DIR`, then `$HOME/.cache/tiktoken`, then the PASSWD
home's `.cache/tiktoken` (`getpwuid` still reaches the real home under a
HOME redirect), and takes the first that holds the file. It only looks:
nothing is created and nothing is downloaded. Production `count.cache_dir`
keeps its HOME semantics (it shares one file with `tokens`); this is the
tests' search alone. A skip that still happens is loud: see
`ledger_skip_support`.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from ledger_skip_support import TAG

from sessiontools.ledger import count

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ledger"
AT = "@FIXTURES@"


def _uv_dir(uv: str, *args: str) -> str | None:
    try:
        p = subprocess.run([uv, *args], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (p.stdout.strip() or None) if p.returncode == 0 else None


def passwd_home() -> Path | None:
    """This uid's home from the passwd database: where `HOME` pointed before
    any redirect. None when the uid has no entry."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return None


def vocab_dirs(env: Mapping[str, str], home_of_passwd: Path | None) -> list[Path]:
    """Where the vocabulary may be, in search order, each once:
    `$TIKTOKEN_CACHE_DIR`, `$HOME/.cache/tiktoken`, the passwd home's."""
    found = []
    if env.get(count.CACHE_ENV):
        found.append(Path(env[count.CACHE_ENV]))
    if env.get("HOME"):
        found.append(Path(env["HOME"]) / ".cache" / "tiktoken")
    if home_of_passwd is not None:
        found.append(home_of_passwd / ".cache" / "tiktoken")
    return list(dict.fromkeys(found))


def find_vocab(env: Mapping[str, str],
               home_of_passwd: Path | None) -> tuple[Path | None, list[Path]]:
    """(the first dir holding `count.VOCAB_FILE` or None, every dir looked in).
    Read-only: a dir that does not exist is skipped, never made."""
    looked = vocab_dirs(env, home_of_passwd)
    return next((d for d in looked if (d / count.VOCAB_FILE).is_file()), None), looked


def _probe(environ: Mapping[str, str] | None = None,
           home_of_passwd: Path | None = None) -> tuple[dict[str, str], str | None]:
    """(the env additions a real counter needs, why it cannot run or None).
    `environ` defaults to `os.environ`, `home_of_passwd` to `passwd_home()`."""
    environ = os.environ if environ is None else environ
    home_of_passwd = passwd_home() if home_of_passwd is None else home_of_passwd
    vocab, looked = find_vocab(environ, home_of_passwd)
    if vocab is None:
        where = ", ".join(str(d / count.VOCAB_FILE) for d in looked)
        return {}, f"no cl100k_base vocabulary in any of: {where} (run `tokens` once)"
    uv = shutil.which("uv", path=environ.get("PATH"))
    if uv is None:
        return {}, "no `uv` on the PATH"
    env = {count.CACHE_ENV: str(vocab)}
    for var, args in (("UV_CACHE_DIR", ("cache", "dir")),
                      ("UV_PYTHON_INSTALL_DIR", ("python", "dir"))):
        got = environ.get(var) or _uv_dir(uv, *args)
        if got:
            env[var] = got
    try:
        counted = count.TiktokenCounter(env={**environ, **env}).count(["hello world"])
    except count.CounterError as e:
        return env, f"the counter child does not run here: {e}"
    if counted != [2]:
        return env, f"the counter child counted 'hello world' as {counted}, not [2]"
    return env, None


REAL_ENV, WHY_NOT = _probe()

#: The ONE named skip for every test that needs the real counter.
requires_counter = pytest.mark.skipif(
    WHY_NOT is not None,
    reason=f"{TAG}: {WHY_NOT}. "
           "These are ST-2's gating tests; verify-001 requires zero skips on the build Mac.")


def real_counter() -> count.TiktokenCounter:
    """The production counter, pointed at the host's real caches."""
    return count.TiktokenCounter(env={**os.environ, **REAL_ENV})


def materialise(tmp_path: Path) -> Path:
    """Copy every fixture into TMP_PATH with `@FIXTURES@` set to it: the
    transcripts name the POLICY and partial files by absolute path."""
    for p in FIXTURES.iterdir():
        if p.suffix == ".jsonl":
            (tmp_path / p.name).write_text(p.read_text(encoding="utf-8").replace(AT, str(tmp_path)),
                                           encoding="utf-8")
        elif p.is_file() and p.suffix != ".py":
            shutil.copyfile(p, tmp_path / p.name)
    return tmp_path


def records(path: Path) -> list[dict]:
    """A transcript's parseable records, raw (for oracles built apart from the reader)."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def oracle(path) -> list[dict]:
    """Each item's source texts, from the raw records, in transcript order:
    what entered the window (`read`, pieces), the command, the persisted
    file, and every full read's raw text by path. Written apart from
    `transcript.py` on purpose: it is the check, not the thing checked."""
    uses, out, by_id, meta, seen = {}, [], {}, {}, set()
    for r in records(path):
        if not isinstance(r, dict) or r.get("isSidechain"):
            continue
        if r.get("type") == "assistant":
            for b in r["message"]["content"]:
                if b.get("type") == "tool_use":
                    uses.setdefault(b["id"], b)
        elif r.get("type") == "attachment":
            if r["uuid"] in seen:
                continue
            seen.add(r["uuid"])
            a = r["attachment"]
            if a["type"] == "nested_memory":
                out.append({"read": [a["content"]["content"]], "full": a["content"]["content"]})
            elif a["type"] == "instructions":
                out += [{"read": [f["content"]], "full": f["content"]} for f in a["files"]]
        elif r.get("type") == "user" and r.get("isMeta") and r.get("sourceToolUseID"):
            meta.setdefault(r["sourceToolUseID"], []).extend(
                b["text"] for b in r["message"]["content"])
        elif r.get("type") == "user" and isinstance(r["message"]["content"], list):
            for b in r["message"]["content"]:
                if b.get("type") != "tool_result":
                    continue
                use, tur = uses[b["tool_use_id"]], r.get("toolUseResult")
                c = b["content"]
                read = ([c] if isinstance(c, str) else
                        None if any(x.get("type") != "text" for x in c) else [x["text"] for x in c])
                item = {"read": read, "name": use["name"]}
                if use["name"] == "Bash":
                    item["cmd"] = use["input"]["command"]
                    if isinstance(tur, dict) and tur.get("persistedOutputPath"):
                        item["persisted"] = tur["persistedOutputPath"]
                if use["name"] == "Read" and isinstance(tur, dict) and tur.get("type") == "text":
                    f = tur["file"]
                    if f["startLine"] == 1 and f["numLines"] == f["totalLines"]:
                        item["full"] = f["content"]
                by_id[b["tool_use_id"]] = item
                out.append(item)
    for tid, texts in meta.items():
        by_id[tid]["read"] = by_id[tid]["read"] + texts
    return out


class FakeCounter:
    """A deterministic stand-in for logic tests: one token per whitespace
    word, plus one. NOT the real counter, so it can run on any host."""

    label = "fake: words + 1"

    def __init__(self):
        self.calls: list[list[str]] = []

    def count(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        return [len(t.split()) + 1 for t in texts]
