"""A recorded `/context` run, read back out of a transcript.

Claude Code writes each `/context` run into the transcript verbatim, once as
ANSI `local-command-stdout` and once as an `isMeta` user message holding the
markdown form:

    ## Context Usage

    **Model:** claude-opus-5-5
    **Tokens:** 19.5k / 1m (2%)

    ### Estimated usage by category

    | Category | Tokens | Percentage |
    |----------|--------|------------|
    | System prompt | 2.1k | 0.2% |
    | System tools | 11.9k | 1.2% |
    | System tools (deferred) | 19.9k | 2.0% |
    ...

Only the `isMeta` markdown form is read: a tool result that happens to quote
one (an agent reading a capture file) is not a snapshot. Every figure in it
is `/context`'s own ESTIMATE (one item was measured at 28 tokens against
680 on the wire), so a split taken from it is labelled as such.

Data only: nothing here renders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

#: The heading `/context`'s markdown form starts its report with.
HEADING = "## Context Usage"
#: The table the category rows sit in.
TABLE_HEADING = "### Estimated usage by category"

#: The `/context` rows that make up the core remainder's inside, in order:
#: the system prompt, the built-in tool schemas and the MCP tool schemas.
#: Everything else `/context` lists is either a named boot row here already
#: (Memory files, Skills, Custom agents, MCP server instructions), growth
#: (Messages), or not in the window at all (the deferred rows, Free space,
#: the compact buffers).
CORE_ROWS = ("System prompt", "System tools", "MCP tools")

_ROW = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([0-9][0-9.,]*)\s*([kKmM]?)\s*\|")
_SCALE = {"": 1, "k": 1_000, "m": 1_000_000}


@dataclass(frozen=True)
class Snapshot:
    """One recorded `/context` run: its category rows (tokens, `/context`'s
    estimates) and when it ran (`at`, timezone-aware; None if unrecorded)."""

    rows: dict[str, int]
    at: datetime | None = None
    line: int | None = None          # the transcript line it was read from

    def core_rows(self) -> dict[str, int]:
        """The CORE_ROWS this snapshot has, in CORE_ROWS order."""
        return {name: self.rows[name] for name in CORE_ROWS if name in self.rows}

    def hhmm(self, tz: tzinfo | None = None) -> str | None:
        """The run's time as HH:MM, local unless `tz` is given; None if
        the record carried no timestamp."""
        if self.at is None:
            return None
        return self.at.astimezone(tz).strftime("%H:%M")


def parse_tokens(text: str, suffix: str = "") -> int | None:
    """'2.1' + 'k' -> 2100; '54' -> 54; '1,234' -> 1234. None if unreadable."""
    try:
        value = float(text.replace(",", ""))
    except ValueError:
        return None
    return round(value * _SCALE[suffix.lower()])


def parse_report(text: str) -> dict[str, int] | None:
    """The category rows of a `/context` markdown report, or None when `text`
    is not one (no heading, or no category table)."""
    if not isinstance(text, str) or HEADING not in text or TABLE_HEADING not in text:
        return None
    table = text.split(TABLE_HEADING, 1)[1]
    rows: dict[str, int] = {}
    started = False
    for line in table.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if started:
                break           # the table ended; the per-item tables follow
            continue
        started = True
        m = _ROW.match(line)
        if m is None:
            continue            # the header and its |---| rule
        tokens = parse_tokens(m.group(2), m.group(3))
        if tokens is not None:
            rows[m.group(1)] = tokens
    return rows or None


def parse_timestamp(value: object) -> datetime | None:
    """A transcript record's ISO timestamp ('...Z' or an offset), aware."""
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def from_record(rec: dict, line: int | None = None) -> Snapshot | None:
    """A Snapshot if `rec` is an `isMeta` user message holding a `/context`
    report, else None."""
    if not isinstance(rec, dict) or rec.get("type") != "user" or not rec.get("isMeta"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    rows = parse_report(_text_of(msg.get("content")))
    if rows is None:
        return None
    return Snapshot(rows=rows, at=parse_timestamp(rec.get("timestamp")), line=line)
