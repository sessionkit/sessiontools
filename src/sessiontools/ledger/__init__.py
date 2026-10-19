"""The ledger: every file one session read and every command it ran, each
with the tokens it put into the window.

    from sessiontools import ledger
    for it in ledger.items(path):        # one Item per item, transcript order
        print(it.kind, it.read_tokens, it.key)
    it.record()                          # the JSON record, one per line

`transcript` reads (no counting), `count` counts (no reading); this module
joins them. ONE counter call per report: every text is gathered first and
counted in a single child. The views are `render`'s.

THE FIGURES, each named for its unit:

- `read_tokens`: the counter over the text that entered the window (the
  `tool_result` text, prefixes included; a load's own text).
- `read_start_line`, `read_lines`: the range as the window saw it.
- `file_lines`: the file's TRUE line count (`wc -l`). Claude Code's
  `totalLines` counts the empty segment after a final newline (67 of 67
  full reads, measured), so it is
  `totalLines - 1` when a Read in this transcript reaches the file's end and
  that segment is empty.
  With no such evidence it is `totalLines` as given, and
  `file_lines_exact` is False: never printed as if certain.
- `file_tokens`: over a full read's raw `file.content` in this transcript
  (`file_tokens_from` "transcript"), else the file on disk now ("disk"),
  else None. ONE ROW NEVER MIXES TWO VERSIONS OF A FILE (a
  tightening of the first design): a
  Read row takes a full read's tokens only when that read's `totalLines`
  equals the row's own, and the disk's only when the disk text splits into
  that many lines too. Otherwise the file changed between the two, and the
  figure is None (note `file changed`), rendered '?'.
- `cmd_tokens`: a command's own tokens, apart from its output.
- `persisted_bytes`, `persisted_tokens`: a persisted output, NOT read: its
  size, and the counter over the file at `persistedOutputPath`.

THE LINKS, each the `seq` of an earlier item of the same path whose lines
overlap this one's (README Scope's "repeat"):

- `repeat_of`: the first such item of the SAME VERSION of the file, so the
  same lines entered the window again.
- `reread_of`: when there is none, the first such item of ANOTHER version:
  a re-read after an edit, never a repeat. Two items are one version when their line
  counts agree and so do the lines they both hold, as far as the
  transcript shows them (`_one_version`).

None is unknown or not applicable, never a zero or a guess.
"""

from __future__ import annotations

import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from sessiontools.ledger import count as _count
from sessiontools.ledger import transcript as _transcript
from sessiontools.ledger.transcript import FILE_KINDS, Raw


@dataclass(frozen=True)
class Item:
    """One item, counted. Field order is the JSON record's."""

    session: str
    seq: int                         # 1-based, transcript order
    kind: str                        # Read | nested | boot | Bash | tool
    key: str                         # the normalised path, the command, or "<Tool>: <input>"
    label: str                       # base name (#k on a collision), or the collapsed command
    ts: str | None
    is_error: bool
    read_tokens: int | None
    read_start_line: int | None
    read_lines: int | None
    file_tokens: int | None
    file_tokens_from: str | None     # "transcript" | "disk" | None
    file_lines: int | None
    file_lines_exact: bool | None    # False: totalLines as given, no end-of-file evidence
    cmd_tokens: int | None
    persisted_bytes: int | None
    persisted_tokens: int | None
    repeat_of: int | None            # the seq of the earlier item whose lines it repeats
    reread_of: int | None            # the seq of an earlier item of another version it re-reads
    note: str | None
    counter: str

    def record(self) -> dict:
        return asdict(self)


def items(path: Path, counter: _count.Counter | None = None) -> list[Item]:
    """Every item in the transcript at PATH, counted. Raises
    `count.CounterError` (code 2) when the counter cannot count."""
    counter = _count.TiktokenCounter() if counter is None else counter
    t = _transcript.read(Path(path))
    return build(t, counter)


def build(t: _transcript.Transcript, counter: _count.Counter) -> list[Item]:
    raws = t.raws
    full = _full_reads(raws)
    eof = _eof_evidence(raws)
    # Per Read row, the full read that describes the same version, if any;
    # the disk is read only for a path some row still needs it for.
    chosen = {r.pos: _nearest_full(full.get(r.key, []), r.pos, r.total_lines)
              for r in raws if r.kind == "Read"}
    disk = {r.key: _disk_text(r.key) for r in raws
            if r.kind == "Read" and chosen[r.pos] is None and r.key != "?"}
    persisted = {r.persisted_path: _disk_text(r.persisted_path) for r in raws
                 if r.persisted_path}

    # Every text, counted once in ONE child.
    pool: dict[str, int] = {}

    def want(text: str | None) -> None:
        if text is not None and text not in pool:
            pool[text] = len(pool)

    for r in raws:
        for piece in r.texts or []:
            want(piece)
        want(r.cmd)
    for text in chosen.values():
        want(text)
    for r in raws:
        if r.kind == "Read" and chosen[r.pos] is None:
            want(_same_version(disk.get(r.key), r.total_lines))
    for text in persisted.values():
        want(text)
    order = list(pool)
    counts = dict(zip(order, counter.count(order), strict=True))

    labels = _labels(raws)
    repeats, rereads = _repeats(raws, eof)
    out = []
    for seq, r in enumerate(raws, 1):
        notes = list(r.notes)
        read_tokens = sum(counts[p] for p in r.texts) if r.texts is not None else None
        file_tokens = file_from = file_lines = exact = None
        if r.kind in ("nested", "boot"):
            file_tokens, file_from = read_tokens, "transcript"
            file_lines, exact = r.total_lines, True
        elif r.kind == "Read":
            text = chosen[r.pos]
            now = disk.get(r.key)
            if text is not None:
                file_tokens, file_from = counts[text], "transcript"
            elif _same_version(now, r.total_lines) is not None:
                file_tokens, file_from = counts[now], "disk"
            elif now is not None:
                notes.append("file changed")
            else:
                notes.append("file gone" if not os.path.lexists(r.key) else "file not text")
            if r.total_lines is not None:
                exact = (r.key, r.total_lines) in eof
                phantom = eof.get((r.key, r.total_lines), False)
                file_lines = r.total_lines - 1 if phantom else r.total_lines
        persisted_tokens = None
        if r.persisted_path:
            ptext = persisted.get(r.persisted_path)
            if ptext is not None:
                persisted_tokens = counts[ptext]
            else:
                notes.append("persisted file gone")
        out.append(Item(
            session=t.session, seq=seq, kind=r.kind, key=r.key, label=labels[seq - 1],
            ts=r.ts, is_error=r.is_error, read_tokens=read_tokens,
            read_start_line=r.start_line, read_lines=r.num_lines,
            file_tokens=file_tokens, file_tokens_from=file_from,
            file_lines=file_lines, file_lines_exact=exact,
            cmd_tokens=counts[r.cmd] if r.cmd is not None else None,
            persisted_bytes=r.persisted_bytes, persisted_tokens=persisted_tokens,
            repeat_of=repeats[seq - 1], reread_of=rereads[seq - 1],
            note="; ".join(notes) or None,
            counter=counter.label))
    return out


def _is_full(r: Raw) -> bool:
    return (r.kind == "Read" and r.raw_content is not None and r.start_line == 1
            and r.total_lines is not None and r.num_lines == r.total_lines)


def _full_reads(raws: list[Raw]) -> dict[str, list[tuple[int, int, str]]]:
    """Per path, every full read's (position, totalLines, raw text), in order."""
    out: dict[str, list[tuple[int, int, str]]] = {}
    for r in raws:
        if _is_full(r):
            out.setdefault(r.key, []).append((r.pos, r.total_lines, r.raw_content))
    return out


def _nearest_full(reads: list[tuple[int, int, str]], pos: int,
                  total_lines: int | None) -> str | None:
    """Among the full reads of the row's own version (the same `totalLines`;
    any, when the row has none), the latest at or before POS, else the first
    after it: the file as it was nearest that row."""
    same = [(p, text) for p, total, text in reads
            if total_lines is None or total == total_lines]
    before = [text for p, text in same if p <= pos]
    if before:
        return before[-1]
    return same[0][1] if same else None


def _same_version(text: str | None, total_lines: int | None) -> str | None:
    """TEXT (the file on disk now) if it is the row's version as far as the
    row can tell: it splits into the row's `totalLines` lines, as Claude Code
    splits them. None if it is gone, or another version."""
    if text is None or (total_lines is not None and len(text.split("\n")) != total_lines):
        return None
    return text


def _eof_evidence(raws: list[Raw]) -> dict[tuple[str, int], bool]:
    """(path, totalLines) -> whether totalLines counted a phantom empty last
    line, for every pair a Read in the transcript reaches the end of. The
    raw content is the slice's lines joined by newlines, so its last segment
    is the file's last line as Claude Code split it: empty iff the file
    ends in a newline."""
    out: dict[tuple[str, int], bool] = {}
    for r in raws:
        if (r.kind == "Read" and r.raw_content is not None and r.total_lines
                and r.num_lines and r.start_line + r.num_lines - 1 == r.total_lines):
            out[(r.key, r.total_lines)] = r.raw_content.split("\n")[-1] == ""
    return out


def _labels(raws: list[Raw]) -> list[str]:
    """Base names, with `#k` when two distinct paths share one (k in order of
    first appearance); a command's label is its whitespace-collapsed text."""
    paths: dict[str, list[str]] = {}
    for r in raws:
        if r.kind in FILE_KINDS:
            names = paths.setdefault(os.path.basename(r.key) or r.key, [])
            if r.key not in names:
                names.append(r.key)
    out = []
    for r in raws:
        if r.kind in FILE_KINDS:
            base = os.path.basename(r.key) or r.key
            names = paths[base]
            out.append(f"{base}#{names.index(r.key) + 1}" if len(names) > 1 else base)
        else:
            out.append(" ".join(r.key.split()))
    return out


def _range(r: Raw) -> tuple[float, float] | None:
    """The lines an item put in the window: a load covers the whole file; a
    Read its own range; a Read with no range figures, none (never a repeat)."""
    if r.kind in ("nested", "boot"):
        return (1, float("inf"))
    if r.kind == "Read" and r.start_line is not None and r.num_lines:
        return (r.start_line, r.start_line + r.num_lines - 1)
    return None


def _repeats(raws: list[Raw], eof: dict[tuple[str, int], bool]
             ) -> tuple[list[int | None], list[int | None]]:
    """For each item, (`repeat_of`, `reread_of`): the seq of the FIRST
    earlier item of the same path whose lines overlap its own and which is
    the same version of the file; failing that, the first such item of
    another version (a re-read after an edit). A repeat wins: a file read,
    edited, then reverted and read again repeats its first read."""
    seen: dict[str, list[tuple[int, Raw, tuple[float, float]]]] = {}
    reps: list[int | None] = []
    rereads: list[int | None] = []
    for seq, r in enumerate(raws, 1):
        rng = _range(r) if r.kind in FILE_KINDS else None
        rep = reread = None
        if rng is not None:
            for earlier, e, (lo, hi) in seen.get(r.key, []):
                if lo <= rng[1] and rng[0] <= hi:
                    if _one_version(e, r, eof):
                        rep = earlier
                        break
                    reread = earlier if reread is None else reread
            seen.setdefault(r.key, []).append((seq, r, rng))
        reps.append(rep)
        rereads.append(None if rep is not None else reread)
    return reps, rereads


def _one_version(a: Raw, b: Raw, eof: dict[tuple[str, int], bool]) -> bool:
    """Whether A and B saw one version of their file, as far as the
    transcript shows: their line counts agree, and so does every line they
    both hold. What the transcript does not hold cannot tell them apart."""
    if a.total_lines is not None and b.total_lines is not None:
        if a.kind == b.kind == "Read":
            if a.total_lines != b.total_lines:     # one convention: compare as given
                return False
        elif not _true_lines(a, eof) & _true_lines(b, eof):
            return False
    la, lb = _lines_held(a), _lines_held(b)
    if la is not None and lb is not None:
        return all(la[k] == lb[k] for k in la.keys() & lb.keys())
    return True


def _true_lines(r: Raw, eof: dict[tuple[str, int], bool]) -> set[int]:
    """The file's true line count (`wc -l`) as R can tell it. A load's is
    its own; a Read's `totalLines` may count an empty segment after the
    final newline, so without end-of-file evidence it is one of two."""
    if r.kind != "Read":
        return {r.total_lines}
    phantom = eof.get((r.key, r.total_lines))
    if phantom is None:
        return {r.total_lines - 1, r.total_lines}
    return {r.total_lines - 1 if phantom else r.total_lines}


def _lines_held(r: Raw) -> dict[int, str] | None:
    """The file's lines R holds, by line number, or None if it holds none.
    A Read's raw content is its range's lines joined by newlines; a load's
    text is the whole file (a boot load's has its final newline stripped,
    so it is cut at its own line count, never compared on that segment)."""
    if r.raw_content is None or r.start_line is None:
        return None
    segs = r.raw_content.split("\n")
    if r.kind != "Read":
        segs = segs[:r.total_lines]
    return {r.start_line + i: seg for i, seg in enumerate(segs)}


def _disk_text(path: str | None) -> str | None:
    """The file's text now: a regular file in UTF-8, else None. A FIFO is
    never opened (the `window.transcript_path` precedent)."""
    if not path:
        return None
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, "rb") as f:
            return f.read().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
