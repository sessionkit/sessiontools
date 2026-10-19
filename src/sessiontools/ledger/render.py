"""The ledger's four views over `ledger.items()`.

    from sessiontools import ledger
    from sessiontools.ledger import render
    its = ledger.items(path)
    print(render.summary(its), end="")    # the default view
    render.short(its)                     # one line per item, largest first
    render.long(its)                      # one row per item, transcript order
    render.jsonl(its)                     # one JSON record per line

Every view renders from the items alone: nothing here reads a transcript or
counts a token, so a view never holds a figure the items do not.

THE UNIT IS NAMED ONCE, IN THE HEADER, NEVER PER LINE.
The short view names `tokens` in its first line; the long view and the
summary are tables whose column titles carry the unit (`read tokens`,
`file lines`, `not read bytes`, `items`), so no figure is ever a bare
number (README Requirements, "No bare number"), and a 200-item short view
does not pay for the word 200 times. The JSON carries the unit in every
field name instead (`read_tokens`, `file_lines`, `persisted_bytes`).

MARKS, each with its legend in the view's header:

- `?` unknown, never a guess and never a zero; `-` not applicable.
- `~` before a file's lines: Claude Code's `totalLines` as given, with no
  end-of-file evidence in the transcript, so it may count one line over
  (`file_lines_exact` False).
- `*` after a file's tokens: counted on disk at report time
  (`file_tokens_from` "disk"), so the file may have changed since.
- `name#k` (short) / `[name#k]` (long): the k-th distinct path with that
  base name, in order of first appearance.

A READ RANGE ENDS AT THE FILE'S LAST LINE. The data stays raw: `read_lines`
is Claude Code's `numLines`, which counts the empty segment after a final
newline, so a read from line 2,001 to the end of a 2,526-line file has
`read_lines` 527 and ends at 2,527 as given. The long view clamps the END
it displays to `file_lines` when that is exact, and prints `2,001-2,526`. The tokens
read are untouched: the window saw what it saw.

The short view cuts commands (and other tools' keys) to `width` characters
(default 16) with whitespace collapsed and no ellipsis: the header's word
"cut" carries it. Measured on one Mac: 16 leaves about 10% headroom
under the 2,000-token budget for 200 items; the gate test records its own.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence

from sessiontools.ledger import Item
from sessiontools.ledger import count as _count

#: The short view's default command width, in characters.
DEFAULT_WIDTH = 16

#: The summary's command width in its ten-largest list.
SUMMARY_WIDTH = 60

#: How many entries the summary's largest list holds.
TOP_N = 10

FILE_KINDS = ("Read", "nested", "boot")

#: The summary's rows, in order: (label, the kinds it sums).
KIND_ROWS = (("Read", ("Read",)), ("nested", ("nested",)), ("boot", ("boot",)),
             ("Bash", ("Bash",)), ("other tools", ("tool",)))

# The column titles of each table: the unit lives here, never on a row.
LONG_COLUMNS = ("#", "came in", "read tokens", "read lines", "file tokens", "file lines",
                "cmd tokens", "not read bytes", "not read tokens", "note", "item")
KIND_COLUMNS = ("came in", "items", "read tokens", "unknown items")
TOP_COLUMNS = ("read tokens", "items", "path or command")
REPEAT_COLUMNS = ("#", "came in", "read tokens", "repeat of", "path")

#: Left-aligned columns; every other column holds figures and is right-aligned,
#: so a figure always ends where its unit-naming title ends.
_TEXT = {"came in", "note", "item", "path or command", "path"}


def _sid(items: Sequence[Item], session: str | None) -> str:
    sid = session if session is not None else (items[0].session if items else "")
    return sid[:8] or "?"


def _label(items: Sequence[Item]) -> str:
    return items[0].counter if items else _count.LABEL


def _n(v: int | None, sep: bool = True) -> str:
    """A figure as printed: `?` when unknown, never a zero standing in."""
    if v is None:
        return "?"
    return f"{v:,}" if sep else str(v)


def _cut(text: str, width: int) -> str:
    return " ".join(text.split())[:max(width, 1)]


def _table(titles: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Columns two spaces apart; figures right-aligned, text left-aligned.
    The last cell may hold several lines: the rest are indented to its column."""
    last = len(titles) - 1
    firsts = [[*r[:last], r[last].split("\n")[0]] for r in rows]
    widths = [max([len(t), *(len(r[i]) for r in firsts)]) for i, t in enumerate(titles)]

    def fmt(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) if t in _TEXT else c.rjust(w)
                         for c, w, t in zip(cells, widths, titles, strict=True)).rstrip()

    indent = " " * (sum(widths[:last]) + 2 * last)
    out = [fmt(titles)]
    for row, first in zip(rows, firsts, strict=True):
        out.append(fmt(first))
        out.extend(indent + more for more in row[last].split("\n")[1:])
    return out


# --- short --------------------------------------------------------------------------

def short(items: Sequence[Item], *, session: str | None = None,
          width: int = DEFAULT_WIDTH) -> str:
    """One line per item, largest first (ties in transcript order; unknown
    last): its tokens and its name. A file is its base name (`#k` on a
    collision), a command or another tool's key is cut to WIDTH characters.
    A file loaded twice shows twice."""
    head = (f"ledger {_sid(items, session)} --short: tokens ({_label(items)}), "
            f"largest first; commands cut to {width} chars; ? unknown\n")
    ranked = sorted(items, key=lambda it: (it.read_tokens is None, -(it.read_tokens or 0)))
    lines = [f"{_n(it.read_tokens, sep=False)} "
             f"{it.label if it.kind in FILE_KINDS else _cut(it.label, width)}"
             for it in ranked]
    return head + "".join(line + "\n" for line in lines)


# --- long ---------------------------------------------------------------------------

def _read_range(it: Item) -> str:
    """A read's lines as displayed: `first-last`, the last clamped to the
    file's exact line count, or `0` when it held none of the file's lines."""
    first, last = it.read_start_line, it.read_start_line + it.read_lines - 1
    if it.file_lines_exact and it.file_lines is not None:
        last = min(last, it.file_lines)
    return f"{first:,}-{last:,}" if it.read_lines and last >= first else "0"


def long_cells(it: Item) -> dict[str, str]:
    """One item's row in the long view, cell by column title."""
    file_kind = it.kind in FILE_KINDS
    if it.read_start_line is not None and it.read_lines is not None:
        read_lines = _read_range(it)
    else:
        read_lines = "?" if file_kind else "-"
    if file_kind:
        file_tokens = _n(it.file_tokens) + ("*" if it.file_tokens_from == "disk" else "")
        file_lines = ("~" if it.file_lines_exact is False else "") + _n(it.file_lines)
    else:
        file_tokens = file_lines = "-"
    persisted = it.persisted_bytes is not None or "persisted" in (it.note or "")
    notes = [it.note] if it.note else []
    if it.repeat_of is not None:
        notes.append(f"repeat of #{it.repeat_of}")
    elif it.reread_of is not None:
        notes.append(f"re-read after edit of #{it.reread_of}")
    if it.kind == "Bash":
        cmd = it.key.split("\n")
        item = "\n".join([f"$ {cmd[0]}", *(f"  {c}" for c in cmd[1:])])
    elif file_kind and it.label != (os.path.basename(it.key) or it.key):
        item = f"{it.key}  [{it.label}]"
    else:
        item = it.key
    return {
        "#": f"#{it.seq}",
        "came in": it.kind,
        "read tokens": _n(it.read_tokens),
        "read lines": read_lines,
        "file tokens": file_tokens,
        "file lines": file_lines,
        "cmd tokens": _n(it.cmd_tokens) if it.kind == "Bash" else "-",
        "not read bytes": _n(it.persisted_bytes) if persisted else "-",
        "not read tokens": _n(it.persisted_tokens) if persisted else "-",
        "note": "; ".join(notes),
        "item": item,
    }


def long(items: Sequence[Item], *, session: str | None = None) -> str:
    """One row per item, in transcript order, every figure."""
    head = [
        (f"ledger {_sid(items, session)} --long: every item in transcript order; "
         f"{_label(items)}"),
        ("  read: what entered the window. file: the file itself. cmd: the command's own "
         "text. not read: a persisted output's full size, of which only the preview was read"),
        ("  ? unknown  - not applicable  ~ totalLines as Claude Code gave it, may count one "
         "line over  * counted on disk now  [name#k] the short view's name for the path"),
    ]
    rows = [[long_cells(it)[t] for t in LONG_COLUMNS] for it in items]
    return "".join(line + "\n" for line in [*head, *_table(LONG_COLUMNS, rows)])


# --- summary ------------------------------------------------------------------------

def _group_key(it: Item) -> str:
    """The ten-largest key: a file's normalised path (a file read in three
    ranges is one entry), a command's whitespace-collapsed text, a tool's key."""
    return it.key if it.kind in FILE_KINDS else " ".join(it.key.split())


def summary(items: Sequence[Item], *, session: str | None = None) -> str:
    """Counts and totals by kind, the ten largest by path or command with
    their items summed, and every repeat with the tokens of its later item."""
    out = [(f"ledger {_sid(items, session)} summary: {_label(items)}; "
            "? unknown, not in any sum"), ""]

    rows = []
    for name, kinds in (*KIND_ROWS, ("total", tuple(k for _, ks in KIND_ROWS for k in ks))):
        its = [it for it in items if it.kind in kinds]
        rows.append([name, _n(len(its)),
                     _n(sum(it.read_tokens for it in its if it.read_tokens is not None)),
                     _n(sum(it.read_tokens is None for it in its))])
    out += _table(KIND_COLUMNS, rows)

    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(_group_key(it), []).append(it)
    totals = {k: sum(it.read_tokens for it in its if it.read_tokens is not None)
              for k, its in groups.items()}
    known = [k for k, its in groups.items() if any(it.read_tokens is not None for it in its)]
    top = sorted(known, key=lambda k: -totals[k])[:TOP_N]   # stable: ties in first-seen order
    out += ["", "the ten largest, by path or command, items summed"]
    out += _table(TOP_COLUMNS, [
        [_n(totals[k]), _n(len(groups[k])),
         k if groups[k][0].kind in FILE_KINDS else _cut(k, SUMMARY_WIDTH)] for k in top])

    reps = [it for it in items if it.repeat_of is not None]
    out += ["", "repeats: the same lines entered the window again"]
    if reps:
        rows = [[f"#{it.seq}", it.kind, _n(it.read_tokens), f"#{it.repeat_of}", it.key]
                for it in reps]
        rows.append(["total", "", _n(sum(it.read_tokens for it in reps
                                         if it.read_tokens is not None)), "", ""])
        out += _table(REPEAT_COLUMNS, rows)
    else:
        out.append("  none")
    return "".join(line + "\n" for line in out)


# --- json ---------------------------------------------------------------------------

def jsonl(items: Sequence[Item], *, session: str | None = None) -> str:
    """One JSON object per line, one per item, and nothing else: no header,
    no summary record. So `cat a.jsonl b.jsonl` is a valid corpus with no
    other step; each record carries its own session id and counter."""
    return "".join(json.dumps(it.record(), ensure_ascii=False) + "\n" for it in items)


#: The views by name; `summary` is the default.
VIEWS: dict[str, Callable[..., str]] = {
    "summary": summary, "short": short, "long": long, "json": jsonl}
DEFAULT_VIEW = "summary"
