"""How full a seat's context window is: `session list`'s CONTEXT column
and `session context`.

Named `window`, not `context`: `sessiontools.start.context` is the launch's
injected prompt, a different thing (a review finding).

Ported from the session repo (retired 2030-06-09): `session-context`'s
`scan()` and `resolve_window()`, and `session-list`'s `context_fields()`
and `_fmt_context()`. The fold and the window rules are theirs,
unchanged; the long reasoning behind each rule lives in that repo's history
(`session-20300609`), and the tests here pin the behaviour.

    from sessiontools import window
    f = window.of(session_id, cwd)   # a Fullness: used, peak, window, pct, model
    window.cell(f)                   # '37%', '?' or '-'

THE NUMBER. Tokens in the window now = the LAST usage row's input_tokens +
cache_creation_input_tokens + cache_read_input_tokens - the sum the
statusline shows. `peak` is the largest such sum this session.

NONE IS "NOT KNOWN", NEVER ZERO. No transcript, an unreadable one, or one
with zero usage rows gives five Nones. A window is never guessed: an
unmapped model, an unusable `$CONTEXT_MAX`, or `used` above the window gives
`window` and `pct` None, rendered `?`. A wrong percentage is worse than a
question mark.

The transcript is Claude Code's: `<projects>/<encoded cwd>/<session>.jsonl`,
`$CLAUDE_PROJECTS_DIR` overriding `~/.claude/projects`.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Claude Code's `message.model` for messages it made itself. Not a model:
#: no window, and its usage sums to zero.
SYNTHETIC_MODEL = "<synthetic>"

#: Context-window sizes by model id. DELIBERATELY INCOMPLETE: an absent id
#: resolves to None and renders `?`, never a guessed window. Source a new row
#: from the Anthropic model catalog (the Models API's `max_input_tokens`),
#: not from memory. Legacy ids publish no window and are absent on purpose.
#:
#: NO FAMILY OR PREFIX RULE. A family does not keep its window across
#: versions: claude-haiku-4-5 is 200K and claude-haiku-5-5 is 1M, so a rule
#: that read a new id's window off its family would have guessed wrong, and
#: a wrong percentage is worse than `?`. A new id gets a row, from the
#: catalog, or stays `?`.
WINDOWS: dict[str, int] = {
    "claude-fable-5": 1_000_000,
    "claude-fable-5-1": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-mythos-5-1": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-5-5": 1_000_000,
    "claude-haiku-4-5": 200_000,    # the one 200K model in the catalog
}

_DATE_SUFFIX = re.compile(r"-20\d{6}$")   # claude-haiku-4-5-20251001

PROJECTS_DIR_ENV = "CLAUDE_PROJECTS_DIR"
CONTEXT_MAX_ENV = "CONTEXT_MAX"


@dataclass(frozen=True)
class Usage:
    """One pass over a transcript.

    `used == 0` hides three states: `usage_rows == 0` (told us nothing),
    `peak == 0` (genuinely ~0 tokens), and `peak > 0` - a `<synthetic>` tail
    row summed to zero on a seat that had reached `peak`, so `used` is not a
    reading. `cell()` renders that last one `?`.
    """

    used: int = 0
    peak: int = 0
    model: str | None = None       # the last REAL model id, never <synthetic>
    usage_rows: int = 0


@dataclass(frozen=True)
class Fullness:
    """The five fields `session list --json` carries. None = not known."""

    used: int | None = None
    peak: int | None = None
    window: int | None = None
    pct: int | None = None
    model: str | None = None

    def fields(self) -> dict[str, int | str | None]:
        return {"context_used": self.used, "context_peak": self.peak,
                "context_window": self.window, "context_pct": self.pct,
                "context_model": self.model}


UNKNOWN = Fullness()


def projects_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$CLAUDE_PROJECTS_DIR` when SET (even empty: a test that isolated
    itself stays isolated), else `~/.claude/projects`."""
    env = os.environ if env is None else env
    override = env.get(PROJECTS_DIR_ENV)
    return Path(override) if override is not None else Path.home() / ".claude" / "projects"


# Claude Code caps a project directory name at this many characters and
# appends '-' + a hash of the raw cwd (measured on a real host).
MAX_DIR_NAME = 200


_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _sanitize(path: str) -> str:
    """Claude Code's `replace(/[^a-zA-Z0-9]/g, "-")`, a JS regex WITHOUT the
    `u` flag: every UTF-16 code unit outside ASCII [a-zA-Z0-9] becomes one
    '-'. So a space, '+', '@', 'é' each give one dash and an emoji (a
    surrogate pair, outside the BMP) gives TWO (measured on a real host
    with claude 2.1.296). The result is pure ASCII, one character per UTF-16 unit,
    so its len() and slices agree with JS's."""
    return "".join(ch if ch in _ALNUM else ("--" if ord(ch) > 0xFFFF else "-")
                   for ch in path)


def _cwd_hash(path: str) -> str:
    """Claude Code's suffix for a capped name: Java's String.hashCode over
    the RAW cwd (int32, wrapping), its absolute value, in lowercase base 36.
    Measured, not guessed: it reproduces a real host's one capped entry exactly
    (`...-registration-qu-cm1hix`). The hash runs over UTF-16
    code units, as JS charCodeAt does; for a BMP-only cwd that is the code
    points."""
    data = path.encode("utf-16-le", "surrogatepass")
    h = 0
    for i in range(0, len(data), 2):
        h = (h * 31 + int.from_bytes(data[i:i + 2], "little")) & 0xFFFFFFFF
    n = abs(h - (1 << 32) if h >= 1 << 31 else h)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def encode_dir(path: str) -> str:
    """Claude Code's project directory name: every character outside ASCII
    [a-zA-Z0-9] becomes '-' (see _sanitize); a name over MAX_DIR_NAME
    characters is cut to MAX_DIR_NAME and gets '-<hash of the raw cwd>' (see
    _cwd_hash). Short names are unchanged. The sanitized name is ASCII, so
    its length and the cut count UTF-16 units, as JS does."""
    name = _sanitize(path)
    if len(name) <= MAX_DIR_NAME:
        return name
    return f"{name[:MAX_DIR_NAME]}-{_cwd_hash(path)}"


def _project_dirs(cwd: str, env: Mapping[str, str] | None = None) -> list[Path]:
    """The directories that may hold CWD's transcripts. The exact name when
    it exists. For a capped name that does NOT exist - a Claude Code build
    whose hash differs from the one measured - every directory named
    '<the first MAX_DIR_NAME characters>-<anything>' instead: a prefix match,
    not a glob pattern, so a '[' or '*' in the cwd is only a character."""
    root = projects_dir(env)
    exact = root / encode_dir(cwd)
    if exact.is_dir():
        return [exact]
    name = _sanitize(cwd)
    if len(name) <= MAX_DIR_NAME:
        return []
    prefix = name[:MAX_DIR_NAME] + "-"
    try:
        return sorted(p for p in root.iterdir() if p.name.startswith(prefix) and p.is_dir())
    except OSError:
        return []


def transcript_path(cwd: str | None, session_id: str,
                    env: Mapping[str, str] | None = None) -> Path | None:
    """The direct path first (a stat per project directory - one, unless a
    capped name fell back to its prefix); a glob over every project only
    when it misses - a session whose cwd no longer encodes to its directory.
    Regular files only: a FIFO named like a transcript would block the read."""
    if not session_id:
        return None
    root = projects_dir(env)
    if cwd:
        for d in _project_dirs(cwd, env):
            direct = d / f"{session_id}.jsonl"
            if direct.is_file():
                return direct
    hits = [p for p in root.glob(f"*/{session_id}*.jsonl") if p.is_file()]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def newest_transcript(cwd: str, env: Mapping[str, str] | None = None) -> Path | None:
    """The most recently written transcript for CWD's project - the old
    `session-context`'s answer when no session is named or known."""
    hits = [p for d in _project_dirs(cwd, env) for p in d.glob("*.jsonl") if p.is_file()]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def why_unknown(model: str | None, used: int | None,
                env: Mapping[str, str] | None = None) -> str:
    """Why resolve_window() said None, in words: the four ways it can."""
    env = os.environ if env is None else env
    if not model:
        return "no real model in the transcript"
    mapped = WINDOWS.get(normalize_model(model))
    if mapped is None:
        return f"no known window for {model}"
    if env.get(CONTEXT_MAX_ENV) is not None and context_max(env) is None:
        return f"${CONTEXT_MAX_ENV} is set but is not a positive number"
    return f"over the {fmt_tokens(mapped)} window mapped for {model}: the map is wrong here"


def fmt_tokens(n: int) -> str:
    """'306K', '1.00M', '812' - the old `session-context`'s figures."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1000:
        return f"{n // 1000}K"
    return str(n)


def _usage_sum(u: dict) -> object:
    return ((u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            + (u.get("cache_read_input_tokens") or 0))


def scan(path: Path) -> Usage:
    """Fold a transcript: the last usage row's sum is `used`, the largest is
    `peak`, the last real model id is `model`.

    A line the fold cannot read is SKIPPED, whatever the reason: a
    half-written tail, undecodable bytes, JSON that is not an object, a
    `message` or `usage` that is not an object, token counts that are not
    numbers, a count that is NaN or infinite (json accepts both). One bad
    line never costs a live seat its number. A falsy `usage`
    (`{}`) is skipped WITHOUT counting a row: `usage_rows` is what separates
    "no data" from "~0 tokens".
    """
    used = peak = rows = 0
    model: str | None = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"usage"' not in line and '"model"' not in line:
                continue            # a JSON key is always quoted: cheap pre-check
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict):
                continue
            m = msg.get("model")
            if isinstance(m, str) and m.strip() and m.strip() != SYNTHETIC_MODEL:
                model = m.strip()
            u = msg.get("usage")
            if not u or not isinstance(u, dict):
                continue
            try:
                s = _usage_sum(u)
            except TypeError:
                continue
            if not isinstance(s, int | float) or not math.isfinite(s):
                continue            # all-string counts concatenate; NaN/Infinity parse
            used = int(s)
            rows += 1
            peak = max(peak, used)
    return Usage(used=used, peak=peak, model=model, usage_rows=rows)


def normalize_model(model: str) -> str:
    """An exact WINDOWS key wins; else drop a dated-snapshot suffix."""
    model = model.strip()
    if model in WINDOWS:
        return model
    stripped = _DATE_SUFFIX.sub("", model)
    return stripped if stripped in WINDOWS else model


def context_max(env: Mapping[str, str] | None = None) -> int | None:
    """`$CONTEXT_MAX` as a positive int, else None. No default: unset means
    the deployment asserted nothing."""
    env = os.environ if env is None else env
    raw = env.get(CONTEXT_MAX_ENV)
    try:
        value = int(raw.strip()) if raw is not None else 0
    except ValueError:
        return None
    return value if value > 0 else None


def resolve_window(model: str | None, used: int | None,
                   env: Mapping[str, str] | None = None) -> int | None:
    """The seat's window in tokens, or None when it cannot be honestly known:
    no real model, an unmapped id, a set-but-unusable `$CONTEXT_MAX`, or
    `used` above the window (the map is wrong for this seat).

    `$CONTEXT_MAX` only TIGHTENS (min): it never makes a window for an
    unmapped model. The overflow check runs against the tightened window.
    """
    env = os.environ if env is None else env
    if not model:
        return None
    window = WINDOWS.get(normalize_model(model))
    if window is None:
        return None
    if env.get(CONTEXT_MAX_ENV) is not None:
        ceiling = context_max(env)
        if ceiling is None:
            return None
        window = min(window, ceiling)
    if used is not None and used > window:
        return None
    return window


def of(session_id: str, cwd: str | None,
       env: Mapping[str, str] | None = None) -> Fullness:
    """The Fullness of one session. Never raises: a seat whose transcript
    cannot be found or read is UNKNOWN, and the listing carries on."""
    try:
        path = transcript_path(cwd, session_id, env)
        if path is None:
            return UNKNOWN
        u = scan(path)
        if u.usage_rows == 0:
            return UNKNOWN
        window = resolve_window(u.model, u.used, env)
        return Fullness(used=u.used, peak=u.peak, window=window,
                       pct=(u.used * 100 // window) if window else None, model=u.model)
    # Broad on purpose: the failures a transcript produces are not one family
    # (OSError, a directory where a file was, ...), and the listing is what
    # people run when things are already wrong.
    except Exception:  # noqa: BLE001
        return UNKNOWN


def cell(f: Fullness) -> str:
    """The CONTEXT cell: `NN%`, `?` or `-`. PERCENT ONLY - a raw count reads
    small until you know the window. `-` is nothing measurable; `?` is a
    reading whose fullness cannot be honestly claimed: no window, or the
    `<synthetic>` zero tail (used 0 on a seat that reached peak)."""
    if f.used is None:
        return "-"
    if f.used == 0 and f.peak:
        return "?"
    if f.pct is None:
        return "?"
    return f"{f.pct}%"
