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

THE CACHE CLOCK. `last_request_at` is the `timestamp` of the LAST counted
usage row that carries a REAL model id: when the seat last spoke to the
API. A `<synthetic>` row (or one with no model) still sets `used` but is
not an API request, so it never moves the clock: letting it would
understate the cache's age (ST-326's ruling, Sun 2026-11-15). `cache_age_s` is now minus that, in whole seconds, floored at 0. It is
the idle clock the prompt cache keeps; the sidecar's `updatedAt` is not
(SM-1047). The timestamp is the response's END, while the cache TTL runs
from the REQUEST, so the cache is older than `cache_age_s` says: a caller
keeps a margin. The prompt cache measured on ms2 is 1 h. NOTHING ACTS ON
THIS CLOCK: it is a reading only (ST-326), and reap does not read it.

THE NUMBER. Tokens in the window now = the LAST usage row's input_tokens +
cache_creation_input_tokens + cache_read_input_tokens - the sum the
statusline shows. `peak` is the largest such sum this session.

NONE IS "NOT KNOWN", NEVER ZERO. No transcript, an unreadable one, or one
with zero usage rows gives every field None. A window is never guessed: an
unmapped model, an unusable `$CONTEXT_MAX`, or `used` above the window gives
`window` and `pct` None. The CELL then shows an unmapped model against
ASSUMED_WINDOW, marked `NN%?`, so a new model still shows usage; anything
else renders `?`. A wrong percentage is worse than a question mark, so a
guess always carries one.

The transcript is Claude Code's: `<projects>/<encoded cwd>/<session>.jsonl`,
`$CLAUDE_PROJECTS_DIR` overriding `~/.claude/projects`.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
    "claude-fable-5-2": 1_000_000,  # catalog, cached 2026-11-03 (claude-api skill)
    "claude-mythos-5": 1_000_000,
    "claude-mythos-5-1": 1_000_000,
    "claude-mythos-5-2": 1_000_000,  # catalog, cached 2026-11-03 (claude-api skill)
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

#: The window a CELL assumes for a model the map does not know yet, so a new
#: model (the next Fable) still shows its usage on day one. Display only:
#: the cell marks it `NN%?`, and `Fullness.window`/`pct` (the JSON) stay None.
#: Every current model but claude-haiku-4-5 is 1M (the maintainer's ruling:
#: "if it gets a model it doesn't know, it should still show the context ...
#: maybe it assumes one million and puts a question mark").
ASSUMED_WINDOW = 1_000_000

_DATE_SUFFIX = re.compile(r"-20\d{6}$")   # claude-haiku-4-5-20251001

#: The cache clock's caveat, word for word in `session list --help` and
#: `session get --help` (and docs/session.md).
CACHE_CLOCK_HELP = (
    "last_request_at is the last real-model usage row's timestamp (ISO-8601 UTC, ending Z) "
    "and cache_age_s the whole seconds since it (never negative); neither is known "
    "without a usage row carrying a timestamp. That timestamp is the response's "
    "END, but the prompt cache's TTL "
    "(1 h, measured on ms2) runs from the REQUEST, so the cache is older than "
    "cache_age_s says: keep a margin. A reading only: nothing acts on it.")

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
    #: The `timestamp` of the LAST counted usage row with a REAL model id
    #: (never `<synthetic>`), as `iso_utc` writes it; None when there is no
    #: such row, or that row's timestamp is missing or unparseable (never an
    #: earlier row's: that would be a guess).
    last_request_at: str | None = None


@dataclass(frozen=True)
class Fullness:
    """What `session list --json` carries from the transcript. None = not
    known.

    `cache_age_s` is not stored: it is computed against NOW each time it is
    read, so a Fullness served from the list snapshot never carries a stale
    age."""

    used: int | None = None
    peak: int | None = None
    window: int | None = None
    pct: int | None = None
    model: str | None = None
    last_request_at: str | None = None   # ISO-8601 UTC, ending `Z`

    def cache_age_s(self, now: float | None = None) -> int | None:
        """Whole seconds from `last_request_at` to NOW (epoch seconds; None:
        the clock now), floored at 0; None when `last_request_at` is."""
        return cache_age_s(self.last_request_at, now)

    def fields(self, now: float | None = None) -> dict[str, int | str | None]:
        """The `session list --json` keys. NOW (epoch seconds; None: the
        clock now) is what `cache_age_s` is measured against."""
        return {"context_used": self.used, "context_peak": self.peak,
                "context_window": self.window, "context_pct": self.pct,
                "context_model": self.model,
                "last_request_at": self.last_request_at,
                "cache_age_s": self.cache_age_s(now)}


UNKNOWN = Fullness()


def iso_utc(raw: object) -> str | None:
    """A transcript `timestamp` as ISO-8601 UTC with a `Z`, milliseconds
    kept (`2030-06-09T02:45:11.553Z`); None for anything that is not a
    string carrying a date, a time AND an offset. A naive time is None, not
    a guess at its zone."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        at = datetime.fromisoformat(raw.strip())
        if at.tzinfo is None or at.utcoffset() is None:
            return None
        return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):      # year 1 at +01:00 has no UTC
        return None


def cache_age_s(last_request_at: str | None, now: float | None = None) -> int | None:
    """Whole seconds from LAST_REQUEST_AT (as `iso_utc` writes it) to NOW
    (epoch seconds; None: the clock now), floored at 0: a timestamp ahead of
    the clock is age 0, never negative. None when it is None or unreadable."""
    at = iso_utc(last_request_at)
    if at is None:
        return None
    now = time.time() if now is None else now
    return max(0, math.floor(now - datetime.fromisoformat(at).timestamp()))


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
    `peak`, the last real model id is `model`, and the timestamp of the last
    usage row with a REAL model id is `last_request_at` (a `<synthetic>`
    row counts toward used/peak/rows but never moves the clock).

    A line the fold cannot read is SKIPPED, whatever the reason: a
    half-written tail, undecodable bytes, JSON that is not an object, a
    `message` or `usage` that is not an object, token counts that are not
    numbers, a count that is NaN or infinite (json accepts both). One bad
    line never costs a live seat its number. A falsy `usage`
    (`{}`) is skipped WITHOUT counting a row: `usage_rows` is what separates
    "no data" from "~0 tokens". A bad `timestamp` never skips its row: the
    row still sets `used`, and `last_request_at` is None until a later row
    carries a good one.
    """
    used = peak = rows = 0
    model: str | None = None
    last: str | None = None
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
            real = isinstance(m, str) and bool(m.strip()) and m.strip() != SYNTHETIC_MODEL
            if real:
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
            if real:                # a <synthetic> row is no API request
                last = iso_utc(obj.get("timestamp"))
    return Usage(used=used, peak=peak, model=model, usage_rows=rows, last_request_at=last)


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
                       pct=(u.used * 100 // window) if window else None, model=u.model,
                       last_request_at=u.last_request_at)
    # Broad on purpose: the failures a transcript produces are not one family
    # (OSError, a directory where a file was, ...), and the listing is what
    # people run when things are already wrong.
    except Exception:  # noqa: BLE001
        return UNKNOWN


def cell(f: Fullness) -> str:
    """The CONTEXT cell: `NN%`, `NN%?`, `?` or `-`. PERCENT ONLY - a raw
    count reads small until you know the window. `-` is nothing measurable;
    `NN%?` is a model the map does not know, against ASSUMED_WINDOW; `?` is
    a reading whose fullness cannot be honestly claimed: the `<synthetic>`
    zero tail (used 0 on a seat that reached peak), a known model over its
    mapped window, or an unknown one over the assumed window."""
    if f.used is None:
        return "-"
    if f.used == 0 and f.peak:
        return "?"
    if f.pct is None:
        assumed = assumed_pct(f)
        return "?" if assumed is None else f"{assumed}%?"
    return f"{f.pct}%"


def assumed_pct(f: Fullness) -> int | None:
    """The percent against ASSUMED_WINDOW, for a model the map does not
    know; None for a mapped model (its None has a real reason) or a
    reading over the assumed window."""
    if f.used is None or not f.model or normalize_model(f.model) in WINDOWS:
        return None
    if f.used > ASSUMED_WINDOW:
        return None
    return f.used * 100 // ASSUMED_WINDOW
