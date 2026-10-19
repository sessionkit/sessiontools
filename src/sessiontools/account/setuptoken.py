"""The capture for `session account new`: `claude setup-token`'s
screen, read into memory and never shown.

`claude setup-token` has no option that writes its token anywhere. It draws
the token on its own screen, once. So `new` runs it on a private pty and
reads the token off a VIRTUAL copy of that screen. The person sees a masked
copy of the screen, drawn by `new` itself; no byte the child writes ever
reaches their terminal.

Most of the module is pure code over byte streams: the screen, the
mask and the mirror have no pty, no process and no terminal. The last part
is the run itself: `run_setup_token`.

- `VirtualScreen` is a small VT emulator: the text, cursor moves, erases and
  scrolls an Ink renderer writes. Strings (OSC, DCS, APC, PM, SOS) are dropped,
  so a clipboard write (OSC 52) or a title never gets anywhere. It answers the
  two queries a TUI may block on, the cursor position and the device
  attributes. It records the three facts the last-row latch needs: a
  character written on the last row, any scroll, and a resize that made it
  shorter.
- `mask_rows` replaces every `sk-ant-` run on a row, and the rows a token
  wrapped onto, with `•` (blanks at either end of a row are ignored,
  because 2.1.299 indents the token one column).
- `find_tokens` reads the same runs, joined with their wrapped rows.
- `Mirror` decides what is drawn. It draws only complete frames: at the end
  of a synchronized-output block, or when the caller says the child has been
  quiet (about 30 ms). It closes for good, and draws nothing more, at the
  first frame that:
  - shows `sk-ant-` anywhere, in view or in the kept scrollback (the
    backstop: a renderer paints top to bottom and left to right, so a
    token's prefix reaches the screen before its body); or
  - follows a character on the pty's last row, a scroll, or a shorter
    resize (the last-row latch: once its screen is as tall as the pty,
    2.1.299 can paint a token's later rows without its first).
  The frames it does draw are masked as well, as a second guard.
- `Mirror.finish` returns `Captured`. Its `screen` is the LAST FRAME THE
  MIRROR DREW, plus the mirror's own line, and never a fresh read of the
  virtual screen. After a cut, a fresh read can hold a
  token's body with no `sk-ant-` row anywhere, which the mask cannot see.

- `run_setup_token` runs the child on a private pty. Its environment
  is an allowlist, and its config dir is a fresh 0700 folder that is scanned
  for the token and removed afterwards. The person's terminal is put in raw
  mode, and the mirror's frames are drawn on its alternate screen. The pty
  takes the person's width, and their height less one row, never more than
  29 rows. `new` has an exit of its own, Ctrl-C twice within a second. It
  also ends the child when the last-row latch closes before any
  token is seen, and after the grace once a token has been seen.

No message, repr or exception text here carries a token. `Captured.token`
is kept out of the repr, and so is everything `Mirror` holds.
"""

from __future__ import annotations

import codecs
import dataclasses
import fcntl
import os
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import termios
import textwrap
import threading
import time
import tty
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Self

from sessiontools import settings
from sessiontools.account import leakscan
from sessiontools.account import registry as reg

#: The prefix every Anthropic token starts with. The backstop, the mask
#: and the capture all key on it (the one assumption left).
PREFIX = "sk-ant-"
#: A token run on a row, as the mask reads it: the prefix and every
#: token character after it, however short.
TOKEN_RUN = re.compile(r"sk-ant-[A-Za-z0-9_-]*")
_TOKEN_CHAR = re.compile(r"[A-Za-z0-9_-]")
_ALL_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]+")
MASK = "•"

#: The quiet that ends a frame drawn without synchronized-output marks.
#: The caller times it: `Mirror` has no clock.
QUIET_S = 0.030
#: How long the child gets to exit after the token appears, before `new`
#: ends it and keeps the token. The caller times it.
GRACE_S = 10.0
#: The rows of scrollback the screen keeps (the capture reads them).
SCROLLBACK_MAX = 2000

#: `new`'s own exit, named in the latch's line. This is the one place the
#: line takes it from.
EXIT_KEY = "Ctrl-C twice"
TOKEN_LINE = "setup-token showed a token: captured, not shown"
#: The last-row latch's line. `new` ends the child as
#: this latch closes, so the line says the run is over, not how to end it.
TALL_LINE = ("setup-token's screen is taller than this terminal, so nothing more is "
             "shown and new has ended this run: make the terminal taller, and run "
             "new again")
LINES = {"token": TOKEN_LINE, "tall": TALL_LINE}

#: The answer to a primary device-attributes query (`ESC[c`): a VT220 with
#: ANSI colour. 2.1.299 sends `ESC[c` last, as a sentinel; the answer tells
#: it the queries before it went unanswered.
DA_REPLY = b"\x1b[?62;22c"

_STRING_STARTS = {"]": "osc", "P": "dcs", "_": "apc", "^": "pm", "X": "sos"}
_C1_STRINGS = {0x9D: "osc", 0x90: "dcs", 0x9F: "apc", 0x9E: "pm", 0x98: "sos"}
_ALT_MODES = {47, 1047, 1049}
_SYNC_MODE = 2026
_AUTOWRAP_MODE = 7

Frame = tuple[str, ...]


# --- the virtual screen -----------------------------------------------------------------


def _width(ch: str) -> int:
    """Cells `ch` takes: 2 for East Asian wide or full-width, 0 for a
    combining mark or a format character (zero width), else 1."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class VirtualScreen:
    """The child's screen, as an xterm would hold it: `rows` rows of `cols`
    cells, a cursor, and a kept scrollback.

    It handles what an Ink renderer writes:
    - text, with autowrap deferred as xterm defers it (a character in the
      last column leaves the cursor there until the next character); CR,
      LF (scrolling into the scrollback), BS, TAB;
    - CSI CUU, CUD, CUF, CUB, CNL, CPL, CHA, CUP/HVP, VPA, ED, EL, SU, SD,
      and IL, DL, ICH, DCH and ECH; ESC 7/8 and CSI s/u (save and restore
      the cursor); ESC D, E and M (index, next line, reverse index);
      ESC c (reset). SGR is ignored: cells hold characters only;
    - the private modes 2026 (synchronized output: its end calls
      `on_sync_end`), 7 (autowrap) and 47/1047/1049 (the alternate screen);
    - OSC, DCS, APC, PM and SOS strings, 7-bit or 8-bit, are consumed and
      dropped, ended by ST, or by BEL for OSC;
    - the cursor-position query (`ESC[6n`) and the primary device
      attributes (`ESC[c`) are answered into `take_replies()`. Every other
      query goes unanswered, `ESC[?6n` included (it needs no
      reply; 2.1.299's setup-token never sends it).

    The scroll region (DECSTBM) is not kept: Ink never sets one.

    `wrote_last_row`, `scrolled` and `shrunk` record, once and for good,
    the three events that close the mirror's last-row latch. IL and
    DL count as a scroll: they move rows within the screen, so they could
    carry a row onto the last one, or take a token's first row away while
    its later rows stay. Ink never sends them; a later version that did
    would close the latch, which is safe.
    Zero-width characters are dropped, so no mark can hide inside a run of
    token characters. The repr shows the size only, never a cell.
    """

    def __init__(self, cols: int, rows: int, *, scrollback_max: int = SCROLLBACK_MAX) -> None:
        if cols < 2 or rows < 1:
            raise ValueError(f"a virtual screen needs 2+ columns and 1+ rows, not {cols}x{rows}")
        self.cols = cols
        self.rows = rows
        self.scrollback_max = scrollback_max
        self.grid: list[list[str]] = [self._blank() for _ in range(rows)]
        self.scrollback: list[str] = []
        self.row = 0
        self.col = 0
        self.pending_wrap = False
        self.autowrap = True
        self.in_sync = False
        self.wrote_last_row = False
        self.scrolled = False
        self.shrunk = False
        #: Called at the end of every synchronized-output block, at its
        #: exact place in the stream.
        self.on_sync_end: Callable[[], None] | None = None
        self._saved = (0, 0)
        self._main: tuple[list[list[str]], int, int] | None = None   # set on the alt screen
        self._replies = bytearray()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "ground"
        self._params = ""
        self._intermediates = ""
        self._string = ""

    def __repr__(self) -> str:
        return f"VirtualScreen({self.cols}x{self.rows})"

    # -- reading -------------------------------------------------------------------------

    def lines(self) -> list[str]:
        """The rows in view, each `cols` cells (a wide character's second
        cell adds nothing)."""
        return ["".join(row) for row in self.grid]

    def all_lines(self) -> list[str]:
        """The kept scrollback, then the rows in view."""
        return self.scrollback + self.lines()

    def take_replies(self) -> bytes:
        """The answers owed to the child since the last call."""
        out = bytes(self._replies)
        self._replies.clear()
        return out

    # -- writing -------------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Apply the child's bytes. A sequence or a UTF-8 character split
        across two calls is joined."""
        for ch in self._decoder.decode(data):
            self._char(ch)

    def resize(self, cols: int, rows: int) -> None:
        """The pty's new size. Fewer rows sets `shrunk`. Rows are cut or
        padded; a shorter screen drops blank rows below the cursor first,
        then moves its top rows into the scrollback, as xterm does."""
        if cols < 2 or rows < 1:
            raise ValueError(f"a virtual screen needs 2+ columns and 1+ rows, not {cols}x{rows}")
        if rows < self.rows:
            self.shrunk = True
        if cols != self.cols:
            self.grid = [(row + [" "] * cols)[:cols] for row in self.grid]
            for row in self.grid:
                if row[-1] == "":
                    row[-1] = " "       # half of a wide character cut off
            self.cols = cols
        excess = len(self.grid) - rows
        while excess > 0 and self.row < len(self.grid) - 1 and not "".join(self.grid[-1]).strip():
            self.grid.pop()
            excess -= 1
        while excess > 0:
            self._keep(self.grid.pop(0))
            self.row -= 1
            excess -= 1
        while len(self.grid) < rows:
            self.grid.append(self._blank())
        self.rows = rows
        self.row = min(max(self.row, 0), rows - 1)
        self.col = min(self.col, cols - 1)
        self.pending_wrap = False

    # -- the parser ----------------------------------------------------------------------

    def _char(self, ch: str) -> None:
        state = self._state
        code = ord(ch)
        if state == "string":
            if ch == "\x1b":
                self._state = "string_esc"
            elif ch in "\x18\x1a\x9c" or (ch == "\x07" and self._string == "osc"):
                self._state = "ground"
            return                                  # the string's content is dropped
        if state == "string_esc":
            if ch == "\\":
                self._state = "ground"              # ST
                return
            self._state = "escape"                  # ESC ended the string; it starts anew
            self._escape(ch)
            return
        if ch == "\x1b":
            self._state = "escape"
            return
        if ch in "\x18\x1a":
            self._state = "ground"
            return
        if code < 0x20 or code == 0x7F:
            self._control(ch)                       # C0 runs even inside a CSI
            return
        if 0x80 <= code <= 0x9F:
            self._c1(code)
            return
        if state == "escape":
            self._escape(ch)
        elif state == "skip":
            self._state = "ground"
        elif state == "csi":
            self._csi_char(ch)
        else:
            self._print(ch)

    def _c1(self, code: int) -> None:
        self._state = "ground"
        if code == 0x9B:
            self._start_csi()
        elif code in _C1_STRINGS:
            self._state, self._string = "string", _C1_STRINGS[code]
        elif code == 0x84:
            self._index()
        elif code == 0x85:
            self.col = 0
            self._index()
        elif code == 0x8D:
            self._reverse_index()

    def _start_csi(self) -> None:
        self._state = "csi"
        self._params = ""
        self._intermediates = ""

    def _escape(self, ch: str) -> None:
        self._state = "ground"
        if ch == "[":
            self._start_csi()
        elif ch in _STRING_STARTS:
            self._state, self._string = "string", _STRING_STARTS[ch]
        elif ch in "()*+-./#% ":
            self._state = "skip"                    # one more character: a charset, DECALN
        elif ch == "7":
            self._saved = (self.row, self.col)
        elif ch == "8":
            self._restore()
        elif ch == "D":
            self._index()
        elif ch == "E":
            self.col = 0
            self._index()
        elif ch == "M":
            self._reverse_index()
        elif ch == "c":
            self._reset()

    def _csi_char(self, ch: str) -> None:
        code = ord(ch)
        if 0x30 <= code <= 0x3F:
            self._params += ch
        elif 0x20 <= code <= 0x2F:
            self._intermediates += ch
        elif 0x40 <= code <= 0x7E:
            self._state = "ground"
            self._csi(ch)
        else:
            self._state = "ground"                  # not a CSI after all

    def _control(self, ch: str) -> None:
        if ch == "\r":
            self.col = 0
            self.pending_wrap = False
        elif ch in "\n\x0b\x0c":
            self._index()
        elif ch == "\b":
            self.col = max(0, self.col - 1)
            self.pending_wrap = False
        elif ch == "\t":
            self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
            self.pending_wrap = False
        # BEL, NUL, SO, SI and the rest are ignored

    # -- CSI -----------------------------------------------------------------------------

    def _csi(self, final: str) -> None:
        params = self._params
        private = params[:1] if params[:1] in ("<", "=", ">", "?") else ""
        numbers: list[int | None] = []
        for part in params[len(private):].split(";") if params[len(private):] else []:
            head = part.split(":")[0]
            numbers.append(int(head) if head.isdigit() else None)
        if self._intermediates:
            return                                  # DECSCUSR and friends: no cells change

        def arg(i: int = 0, default: int = 1) -> int:
            value = numbers[i] if i < len(numbers) else None
            return default if not value else value

        if private == "?":
            if final in "hl":
                self._private_modes(numbers, final == "h")
            # ESC[?6n (DECXCPR), ESC[?u and the rest: no reply
            return
        if private:
            return                                  # ESC[>c, ESC[>q (XTVERSION): no reply
        moves = {
            "A": lambda: self._move(self.row - arg(), self.col),
            "B": lambda: self._move(self.row + arg(), self.col),
            "e": lambda: self._move(self.row + arg(), self.col),
            "C": lambda: self._move(self.row, self.col + arg()),
            "a": lambda: self._move(self.row, self.col + arg()),
            "D": lambda: self._move(self.row, self.col - arg()),
            "E": lambda: self._move(self.row + arg(), 0),
            "F": lambda: self._move(self.row - arg(), 0),
            "G": lambda: self._move(self.row, arg() - 1),
            "`": lambda: self._move(self.row, arg() - 1),
            "H": lambda: self._move(arg(0) - 1, arg(1) - 1),
            "f": lambda: self._move(arg(0) - 1, arg(1) - 1),
            "d": lambda: self._move(arg() - 1, self.col),
        }
        if final in moves:
            moves[final]()
        elif final == "J":
            self._erase_display(arg(0, 0))
        elif final == "K":
            self._erase_line(arg(0, 0))
        elif final == "S":
            self._scroll_up(arg())
        elif final == "T" and len(numbers) <= 1:
            self._scroll_down(arg())
        elif final == "L":
            self._insert_lines(arg())
        elif final == "M":
            self._delete_lines(arg())
        elif final == "@":
            self._insert_cells(arg())
        elif final == "P":
            self._delete_cells(arg())
        elif final == "X":
            self._erase_cells(arg())
        elif final == "n" and arg(0, 0) == 6:
            self._replies += f"\x1b[{self.row + 1};{self.col + 1}R".encode("ascii")
        elif final == "c" and arg(0, 0) == 0:
            self._replies += DA_REPLY
        elif final == "s" and not numbers:
            self._saved = (self.row, self.col)
        elif final == "u":
            self._restore()
        # SGR (m), DECSTBM (r) and the rest change no cell

    def _private_modes(self, numbers: Sequence[int | None], on: bool) -> None:
        for mode in numbers:
            if mode == _SYNC_MODE:
                self.in_sync = on
                if not on and self.on_sync_end is not None:
                    self.on_sync_end()
            elif mode == _AUTOWRAP_MODE:
                self.autowrap = on
                self.pending_wrap = False
            elif mode in _ALT_MODES:
                self._alternate(on, keep_cursor=mode == 1049)

    # -- the cells -----------------------------------------------------------------------

    def _blank(self) -> list[str]:
        return [" "] * self.cols

    def _keep(self, cells: list[str]) -> None:
        if self._main is not None:
            return                                  # the alternate screen keeps no scrollback
        self.scrollback.append("".join(cells))
        if len(self.scrollback) > self.scrollback_max:
            del self.scrollback[: len(self.scrollback) - self.scrollback_max]

    def _move(self, row: int, col: int) -> None:
        self.row = min(max(row, 0), self.rows - 1)
        self.col = min(max(col, 0), self.cols - 1)
        self.pending_wrap = False

    def _restore(self) -> None:
        self._move(*self._saved)

    def _put(self, row: int, col: int, ch: str) -> None:
        cells = self.grid[row]
        if cells[col] == "" and col > 0:
            cells[col - 1] = " "                    # half of a wide character overwritten
        if col + 1 < self.cols and cells[col + 1] == "":
            cells[col + 1] = " "
        cells[col] = ch

    def _print(self, ch: str) -> None:
        width = _width(ch)
        if width == 0:
            return
        if self.pending_wrap and self.autowrap:
            self.col = 0
            self._index()
        self.pending_wrap = False
        if width == 2 and self.col == self.cols - 1:
            if not self.autowrap:
                return
            self._put(self.row, self.col, " ")
            self.col = 0
            self._index()
        self._put(self.row, self.col, ch)
        if width == 2:
            self._put(self.row, self.col + 1, "")
        if self.row == self.rows - 1:
            self.wrote_last_row = True
        if self.col + width >= self.cols:
            self.col = self.cols - 1
            self.pending_wrap = self.autowrap
        else:
            self.col += width

    def _index(self) -> None:
        self.pending_wrap = False
        if self.row == self.rows - 1:
            self._scroll_up(1)
        else:
            self.row += 1

    def _reverse_index(self) -> None:
        self.pending_wrap = False
        if self.row == 0:
            self._scroll_down(1)
        else:
            self.row -= 1

    def _scroll_up(self, n: int) -> None:
        self.scrolled = True
        for _ in range(min(n, self.rows)):
            self._keep(self.grid.pop(0))
            self.grid.append(self._blank())

    def _scroll_down(self, n: int) -> None:
        self.scrolled = True
        for _ in range(min(n, self.rows)):
            self.grid.pop()
            self.grid.insert(0, self._blank())

    def _insert_lines(self, n: int) -> None:
        self.scrolled = True
        for _ in range(min(n, self.rows - self.row)):
            self.grid.pop()
            self.grid.insert(self.row, self._blank())
        self.col = 0
        self.pending_wrap = False

    def _delete_lines(self, n: int) -> None:
        self.scrolled = True
        for _ in range(min(n, self.rows - self.row)):
            self.grid.pop(self.row)
            self.grid.append(self._blank())
        self.col = 0
        self.pending_wrap = False

    def _insert_cells(self, n: int) -> None:
        cells = self.grid[self.row]
        n = min(n, self.cols - self.col)
        cells[self.col:self.col] = [" "] * n
        del cells[self.cols:]
        self.pending_wrap = False

    def _delete_cells(self, n: int) -> None:
        cells = self.grid[self.row]
        n = min(n, self.cols - self.col)
        del cells[self.col:self.col + n]
        cells.extend([" "] * n)
        self.pending_wrap = False

    def _erase_cells(self, n: int) -> None:
        cells = self.grid[self.row]
        for col in range(self.col, min(self.cols, self.col + n)):
            cells[col] = " "
        self.pending_wrap = False

    def _erase_line(self, how: int) -> None:
        cells = self.grid[self.row]
        span = {0: range(self.col, self.cols), 1: range(self.col + 1),
                2: range(self.cols)}.get(how, range(0))
        for col in span:
            cells[col] = " "
        self.pending_wrap = False

    def _erase_display(self, how: int) -> None:
        if how == 3:
            self.scrollback.clear()
            return
        if how == 2:
            self.grid = [self._blank() for _ in range(self.rows)]
            return
        self._erase_line(how)
        rows = range(self.row + 1, self.rows) if how == 0 else range(self.row)
        for row in rows:
            self.grid[row] = self._blank()

    def _alternate(self, on: bool, *, keep_cursor: bool) -> None:
        if on and self._main is None:
            self._main = (self.grid, self.row, self.col)
            self.grid = [self._blank() for _ in range(self.rows)]
            if not keep_cursor:
                self.row = self.col = 0
        elif not on and self._main is not None:
            grid, row, col = self._main
            self._main = None
            self.grid = [(cells + [" "] * self.cols)[:self.cols] for cells in grid[:self.rows]]
            while len(self.grid) < self.rows:
                self.grid.append(self._blank())
            self._move(row, col)
        self.pending_wrap = False

    def _reset(self) -> None:
        self.grid = [self._blank() for _ in range(self.rows)]
        self._main = None
        self.row = self.col = 0
        self.pending_wrap = False
        self.autowrap = True
        self.in_sync = False
        self._saved = (0, 0)


# --- the mask and the capture ----------------------------------------------------------


def _wrap_rows(lines: Sequence[str], i: int, run: re.Match[str]) -> list[int]:
    """The rows after `lines[i]` that a token run wrapped onto.
    Only a row's LAST run can wrap, and only when nothing but blanks follows
    it on its row. A following row joins while its cells, blanks at either
    end ignored, are all token characters; a blank row ends it."""
    if lines[i][run.end():].strip():
        return []
    out = []
    for j in range(i + 1, len(lines)):
        if not _ALL_TOKEN_CHARS.fullmatch(lines[j].strip()):
            break
        out.append(j)
    return out


def mask_rows(lines: Sequence[str]) -> list[str]:
    """`lines` with every `sk-ant-` run, and every row a token wrapped onto,
    replaced cell for cell by `•`."""
    rows = list(lines)
    i = 0
    while i < len(rows):
        runs = list(TOKEN_RUN.finditer(rows[i]))
        if not runs:
            i += 1
            continue
        wrapped = _wrap_rows(rows, i, runs[-1])
        rows[i] = TOKEN_RUN.sub(lambda m: MASK * len(m.group()), rows[i])
        for j in wrapped:
            rows[j] = "".join(MASK if _TOKEN_CHAR.fullmatch(c) else c for c in rows[j])
        i = (wrapped[-1] if wrapped else i) + 1
    return rows


def find_tokens(lines: Sequence[str]) -> set[str]:
    """Every `sk-ant-` run in `lines`, joined with the rows it wrapped onto
    by the mask's rule (blanks at either end of a row are not
    part of the token)."""
    found: set[str] = set()
    for i, line in enumerate(lines):
        runs = list(TOKEN_RUN.finditer(line))
        for run in runs[:-1]:
            found.add(run.group())
        if runs:
            last = runs[-1]
            found.add(last.group() + "".join(lines[j].strip()
                                             for j in _wrap_rows(lines, i, last)))
    return found


def shows_token(lines: Sequence[str]) -> bool:
    """`sk-ant-` is on a row, or across the end of one row and the start of
    the next (a prefix hard-wrapped mid-word). The backstop's test."""
    if any(PREFIX in line for line in lines):
        return True
    return any(PREFIX in lines[i].rstrip() + lines[i + 1].lstrip()
               for i in range(len(lines) - 1))


def distinct_values(candidates: Iterable[str]) -> set[str]:
    """The distinct values among `candidates`, a value that is the start of
    a longer one folded into it. A frame read while the token was still
    being painted holds its start; that is the same token, not a second."""
    values = set(candidates)
    return {v for v in values if not any(o != v and o.startswith(v) for o in values)}


def is_setup_token(value: str) -> bool:
    """`value` has the setup-token shape, leakscan's `TOKEN`."""
    return leakscan.TOKEN.fullmatch(value.encode("utf-8", "replace")) is not None


def _exited(exit_code: int | None) -> str:
    if exit_code is None:
        return "did not exit"
    if exit_code < 0:
        return f"was ended by signal {-exit_code}"
    return f"exited {exit_code}"


@dataclass(frozen=True)
class Captured:
    """What the capture got. `token` is None unless exactly
    one setup-token-shaped value was on the screen and the child exited 0,
    or was ended by `new` after the grace with it captured. `why` says what
    happened, never a value. `screen` is the last frame the mirror drew,
    with its own line: what `new` prints when there is no token."""

    token: str | None = field(repr=False)
    exit_code: int | None
    why: str
    screen: str

    @property
    def ok(self) -> bool:
        return self.token is not None


# --- the mirror -------------------------------------------------------------------------


class Mirror:
    """What the person sees of setup-token's screen.

    Feed it the child's bytes (`feed`), say when the child has been quiet
    for `QUIET_S` (`quiet`), pass on resizes (`resize`), and write
    `replies()` back to the child. `feed` and `quiet` return the frames to
    draw, already masked: the caller draws each in full, on the alternate
    screen, and nothing else of the child's.

    `latch` is None until the mirror closes, then "token" or "tall", for
    good. From then on nothing is drawn, and `line` is the mirror's own
    line to show beneath the last frame. `finish` reads the screen a last
    time and returns `Captured`.
    """

    def __init__(self, cols: int, rows: int) -> None:
        self.screen = VirtualScreen(cols, rows)
        self.screen.on_sync_end = self._sync_end
        self.latch: str | None = None
        self.last_frame: Frame = ()
        self.frames_drawn = 0
        self._ready: list[Frame] = []
        self._candidates: set[str] = set()

    def __repr__(self) -> str:
        return (f"Mirror({self.screen.cols}x{self.screen.rows}, latch={self.latch!r}, "
                f"frames_drawn={self.frames_drawn})")

    @property
    def line(self) -> str | None:
        """The mirror's own line once it has closed, else None."""
        return LINES.get(self.latch) if self.latch else None

    @property
    def token_seen(self) -> bool:
        """A value was read off the screen (it may yet prove not one token)."""
        return bool(self._candidates)

    def feed(self, data: bytes) -> list[Frame]:
        """Apply the child's bytes; the frames that synchronized-output
        blocks completed in them, to draw in order."""
        self.screen.feed(data)
        ready, self._ready = self._ready, []
        return ready

    def quiet(self) -> Frame | None:
        """The child has written nothing for `QUIET_S`: the frame to draw,
        if any. Inside an open synchronized-output block there is none."""
        if self.screen.in_sync:
            return None
        return self._frame()

    def resize(self, cols: int, rows: int) -> None:
        """The pty's new size. A shorter pty closes the latch at the next
        frame."""
        self.screen.resize(cols, rows)

    def replies(self) -> bytes:
        """The answers to write back to the child."""
        return self.screen.take_replies()

    def end_screen(self) -> str:
        """The ONLY screen `new` prints of the child's: the last frame the
        mirror drew, then its own line. Never a fresh read."""
        rows = list(self.last_frame)
        while rows and not rows[-1]:
            rows.pop()
        if self.line:
            rows.append(self.line)
        return "\n".join(rows)

    def finish(self, exit_code: int | None, *, grace_ended: bool = False) -> Captured:
        """The child is gone. `exit_code` is its status (negative for a
        signal); `grace_ended` says `new` ended it after `GRACE_S` with a
        token on its screen. Exactly one setup-token-shaped value, and an
        exit 0 or the grace, give the token; anything else gives none.

        A child that has exited is quiet for good, so `finish` first takes
        the frame the quiet rule would take, under both latches, as `quiet`
        does. Without it, a last screen painted with no sync marks (a
        refusal, then exit) would never be the end print, and the person
        would not see why. Inside an open sync block there is no such
        frame: the child died mid-paint.

        A run whose last-row latch closed before any token was seen keeps
        no token either. Its screen outgrew the pty, so a value read off it
        may be cut short (a row painted off the bottom, or never painted),
        so the person ends the run and starts again; the run
        ends the child when that latch closes."""
        if not self.screen.in_sync:
            self._frame()
        lines = self.screen.all_lines()
        if shows_token(lines):
            self._candidates |= find_tokens(lines)
        values = distinct_values(self._candidates)
        token: str | None = None
        if self.latch == "tall":
            why = (f"setup-token {_exited(exit_code)}; its screen grew taller than this "
                   "terminal, so no token is kept: make the terminal taller and run new "
                   "again")
        elif len(values) > 1:
            why = f"setup-token showed {len(values)} different tokens: none is kept"
        elif not values:
            why = f"setup-token {_exited(exit_code)} and showed no token"
        elif not is_setup_token(next(iter(values))):
            why = (f"setup-token {_exited(exit_code)}; what it showed is not a setup token "
                   "(sk-ant-oat01-...)")
        elif exit_code == 0 or grace_ended:
            token = next(iter(values))
            why = ("token captured" if exit_code == 0
                   else f"token captured; setup-token {_exited(exit_code)} after the grace")
        else:
            why = f"setup-token {_exited(exit_code)}: its token is not kept"
        return Captured(token, exit_code, why, self.end_screen())

    def _sync_end(self) -> None:
        frame = self._frame()
        if frame is not None:
            self._ready.append(frame)

    def _frame(self) -> Frame | None:
        """The frame to draw now, or None. The latches are checked first,
        and close for good."""
        lines = self.screen.all_lines()
        shown = shows_token(lines)
        if shown:
            self._candidates |= find_tokens(lines)
        if self.latch is not None:
            return None
        screen = self.screen
        if shown:
            self.latch = "token"
            return None
        if screen.wrote_last_row or screen.scrolled or screen.shrunk:
            self.latch = "tall"
            return None
        frame = tuple(row.rstrip() for row in mask_rows(screen.lines()))
        if frame == self.last_frame:
            return None
        self.last_frame = frame
        self.frames_drawn += 1
        return frame


# --- the run ------------------------------------------------------------------------

#: The pty is never taller than this. From 30 rows up, 2.1.299 draws
#: its 17-row art header, which could carry a screen down to the last row.
PTY_ROWS_MAX = 29
#: A second Ctrl-C within this many seconds of the first is `new`'s own exit.
#: The first still goes to the child.
CANCEL_WINDOW_S = 1.0
#: After SIGTERM, how long the child's process group gets before SIGKILL.
TERM_WAIT_S = 2.0
#: How long the run reads the pty after the child has gone, for its last bytes.
DRAIN_S = 0.5
CTRL_C = 0x03
#: The child's config dir: `<config_root>/setup-token-XXXXXX`.
CONFIG_PREFIX = "setup-token-"
#: `new`'s own row, above the mirror.
HEADER_LINE = "session account new: setup-token's screen, masked (the token is never shown)"

#: The only names the child inherits. An allowlist, because
#: 2.1.299 reads more credential and provider variables than a denylist
#: would name. `LC_*` is allowed as a prefix. The three `SSH_*` names tell
#: it that the session is remote (no credential, so they pass).
#: `TMUX` does not pass, so `CLAUDE_CODE_FORCE_SYNC_OUTPUT` counts.
ENV_ALLOW = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "BROWSER", "DISPLAY",
    "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY",
})
ENV_ALLOW_PREFIXES = ("LC_",)


class SetupTokenError(Exception):
    """The run could not start. The message names a path or a step, never
    a value."""


class Interrupted(Exception):
    """`new` itself got SIGTERM or SIGHUP during the run. The terminal was
    restored, the child ended and its config dir removed first."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"interrupted by signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class Run:
    """What one run of setup-token gave. `ended` is None when the child
    exited by itself, else why `new` ended it: "cancel" (its own exit),
    "tall" (the last-row latch closed before a token) or "grace".
    `config_hits` names, never quotes, the files in the child's config dir
    that held the token or a token shape."""

    captured: Captured
    ended: str | None
    config_hits: tuple[str, ...]
    config_removed: bool


def child_env(environ: Mapping[str, str], config_dir: str) -> dict[str, str]:
    """The child's environment: the allowlist, then the two names
    `new` sets. A missing `TERM` becomes `xterm-256color`."""
    env = {key: value for key, value in environ.items()
           if key in ENV_ALLOW or key.startswith(ENV_ALLOW_PREFIXES)}
    env.setdefault("TERM", "xterm-256color")
    env["CLAUDE_CONFIG_DIR"] = config_dir
    env["CLAUDE_CODE_FORCE_SYNC_OUTPUT"] = "1"
    return env


def pty_size(cols: int, rows: int) -> tuple[int, int]:
    """The pty's size for a terminal of `cols` x `rows`: the same
    width; the height less `new`'s own row, never more than 29."""
    return cols, max(1, min(rows - 1, PTY_ROWS_MAX))


def terminal_size(fd: int) -> tuple[int, int]:
    """`(cols, rows)` of the terminal on `fd`; 80x24 when it cannot say."""
    try:
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    except OSError:
        return 80, 24
    return (cols or 80), (rows or 24)


def set_size(fd: int, cols: int, rows: int) -> None:
    """Set the pty's size; the kernel sends the child SIGWINCH."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def make_config_dir(root: str | os.PathLike[str]) -> str:
    """A fresh, empty 0700 folder `<root>/setup-token-XXXXXX`. It is
    not under /tmp: if a later version wrote the token into its config, the
    token would land in a private folder of ours, and be found there. ROOT
    and each missing parent are made 0700 (`registry.make_private_dirs`)."""
    reg.make_private_dirs(root)
    return tempfile.mkdtemp(prefix=CONFIG_PREFIX, dir=root)


def _safe_name(name: str, token: str | None) -> str:
    """A path to report, with any token or token run in it masked."""
    if token:
        name = name.replace(token, MASK * len(token))
    return mask_rows([name])[0]


def scan_config_dir(path: str | os.PathLike[str], token: str | None) -> tuple[str, ...]:
    """The files under `path` (relative, in order) that hold `token` as
    bytes, or any token shape (leakscan's `TOKEN`, raw or hex). Links are
    not followed. A file is named, never quoted."""
    hits: list[str] = []
    needle = token.encode("utf-8") if token else None
    for here, dirs, files in os.walk(path, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(here, name)
            try:
                if not stat.S_ISREG(os.lstat(full).st_mode):
                    continue
                with open(full, "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            if (needle and needle in data) or leakscan.scan_bytes(data, {}):
                hits.append(_safe_name(os.path.relpath(full, path), token))
    return tuple(hits)


def remove_config_dir(path: str | os.PathLike[str]) -> bool:
    """Remove the child's config dir; True when it is gone."""
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def _printable(text: str) -> str:
    """`text` with anything that is not a printable character replaced by
    `?`: a frame can never carry a control to the person's terminal."""
    return "".join(ch if ch == " " or ch.isprintable() else "?" for ch in text)


def _fill(text: str, cols: int) -> str:
    """One row: `text` made printable and cut to `cols`, then erased to
    the end of the row. A row that fills the last column gets no erase: the
    cursor waits on that column for the wrap, and an erase there would take
    the row's last character (xterm, and `VirtualScreen`)."""
    text = _printable(text)[:cols]
    return text if len(text) >= cols else text + "\x1b[K"


def render(frame: Sequence[str], *, cols: int, rows: int | None = None,
           line: str | None = None, cursor: tuple[int, int] | None = None) -> bytes:
    """The bytes that draw one frame on the person's alternate screen:
    `new`'s own row, then the frame's rows, then the mirror's line (wrapped
    to `cols`) once it has closed. `cursor` is the child's, in the frame's
    rows. In one colour: no SGR. Nothing here comes from the child except
    the frame's masked text, and its controls are replaced.

    `rows` is the person's terminal height. The frame alone always fits it
    (the pty is a row shorter), but frame and line may not: the line goes
    below the frame's last non-blank row, and the frame loses rows from the
    bottom until the whole line is inside the terminal. Drawn past the last
    row, each wrapped piece would land on that row over the one before, and
    only the line's tail would show."""
    wrapped = (textwrap.wrap(line, max(cols - 1, 1)) or [""]) if line else []
    body = list(frame)
    if wrapped:
        while body and not body[-1].strip():
            body.pop()
        if rows is not None:
            del body[max(rows - 1 - len(wrapped), 0):]
    out = ["\x1b[?2026h\x1b[H", _fill(HEADER_LINE, cols)]
    row = 2
    for text in body:
        out.append(f"\x1b[{row};1H{_fill(text, cols)}")
        row += 1
    out.append(f"\x1b[{row};1H\x1b[J")
    if wrapped:
        for text in wrapped:
            out.append(f"\x1b[{row};1H{_printable(text)}\x1b[K")
            row += 1
        out.append("\x1b[?25l")
    elif cursor is not None:
        out.append(f"\x1b[?25h\x1b[{cursor[0] + 2};{cursor[1] + 1}H")
    out.append("\x1b[?2026l")
    return "".join(out).encode("utf-8")


ENTER_ALT = b"\x1b[?1049h\x1b[H\x1b[2J"
LEAVE_ALT = b"\x1b[?2026l\x1b[0m\x1b[?25h\x1b[?1049l"


def split_keys(keys: bytes, now: float, first_ctrl_c: float | None,
               window_s: float = CANCEL_WINDOW_S) -> tuple[bytes, bool, float | None]:
    """`new`'s own exit, over one read of keystrokes at time `now`.
    Returns what to pass to the child, whether this is the exit, and the
    time of the last first Ctrl-C. The first Ctrl-C goes to the child; a
    second within `window_s` is the exit, and neither it nor anything after
    it in the same read reaches the child."""
    out = bytearray()
    for byte in keys:
        if byte == CTRL_C:
            if first_ctrl_c is not None and now - first_ctrl_c <= window_s:
                return bytes(out), True, first_ctrl_c
            first_ctrl_c = now
        out.append(byte)
    return bytes(out), False, first_ctrl_c


def _write_all(fd: int, data: bytes) -> None:
    while data:
        try:
            done = os.write(fd, data)
        except InterruptedError:
            continue
        except BlockingIOError:
            select.select([], [fd], [], 1.0)
            continue
        data = data[done:]


def _take_ctty() -> None:
    """In the child, after setsid: the pty becomes its controlling
    terminal, so its `/dev/tty` is the pty."""
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _end(proc: subprocess.Popen, wait_s: float) -> None:
    """End the child's process group: SIGTERM, then SIGKILL after `wait_s`."""
    if proc.poll() is not None:
        _killpg(proc.pid, signal.SIGTERM)       # anything it left in its group
        return
    _killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        _killpg(proc.pid, signal.SIGKILL)
        proc.wait()


class _Signals:
    """SIGWINCH sets a flag; SIGTERM and SIGHUP raise `Interrupted`, so the
    run unwinds through its `finally`. Installed only on the main thread;
    the old handlers come back on exit."""

    WATCHED = (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP)

    def __init__(self) -> None:
        self.winch = False
        self._old: dict[int, object] = {}

    def __enter__(self) -> Self:
        if threading.current_thread() is threading.main_thread():
            for sig in self.WATCHED:
                self._old[sig] = signal.signal(sig, self._handle)
        return self

    def __exit__(self, *exc: object) -> None:
        for sig, old in self._old.items():
            signal.signal(sig, old)    # type: ignore[arg-type]

    def _handle(self, signum: int, _frame: object) -> None:
        if signum == signal.SIGWINCH:
            self.winch = True
            return
        raise Interrupted(signum)


class _Session:
    """One run's moving parts: the pty, the child, the mirror and the
    person's terminal. `loop` returns why `new` ended the child, or None."""

    def __init__(self, *, proc: subprocess.Popen, master: int, mirror: Mirror,
                 tty_in: int, tty_out: int, size: tuple[int, int], term_rows: int,
                 signals: _Signals, grace_s: float, cancel_window_s: float, term_wait_s: float,
                 quiet_s: float) -> None:
        self.proc = proc
        self.master = master
        self.mirror = mirror
        self.tty_in = tty_in
        self.tty_out = tty_out
        self.size = size
        self.term_rows = term_rows                  # the person's height, for render
        self.signals = signals
        self.grace_s = grace_s
        self.cancel_window_s = cancel_window_s
        self.term_wait_s = term_wait_s
        self.quiet_s = quiet_s
        self.eof = False
        self.dirty = False
        self.last_out = 0.0
        self.first_ctrl_c: float | None = None
        self.line_drawn = False
        self.keys_open = True

    # -- drawing -------------------------------------------------------------------------

    def draw(self, frame: Frame | None = None) -> None:
        mirror = self.mirror
        shown = mirror.last_frame if frame is None else frame
        screen = mirror.screen
        _write_all(self.tty_out, render(shown, cols=self.size[0], rows=self.term_rows,
                                        line=mirror.line, cursor=(screen.row, screen.col)))
        if mirror.line:
            self.line_drawn = True

    # -- the child's side ----------------------------------------------------------------

    def read_child(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except OSError:
            data = b""
        if not data:
            self.eof = True
            return
        for frame in self.mirror.feed(data):
            self.draw(frame)
        replies = self.mirror.replies()
        if replies:
            self.send(replies)
        self.dirty = True
        self.last_out = time.monotonic()

    def send(self, data: bytes) -> None:
        try:
            _write_all(self.master, data)
        except OSError:
            pass                                    # the child has gone

    def settle(self) -> None:
        """The quiet rule: no output for `quiet_s` ends a frame."""
        if self.dirty and time.monotonic() - self.last_out >= self.quiet_s:
            self.dirty = False
            frame = self.mirror.quiet()
            if frame is not None:
                self.draw(frame)

    def drain(self) -> None:
        """The child has gone or been ended: its last bytes, for a while."""
        deadline = time.monotonic() + DRAIN_S
        while not self.eof and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                if self.proc.poll() is not None:
                    break
                continue
            self.read_child()

    # -- the person's side ---------------------------------------------------------------

    def read_keys(self) -> bool:
        """Pass keystrokes to the child. True when the second of two
        Ctrl-C presses within `cancel_window_s` arrived: `new`'s own exit.
        The first goes to the child; the second, and anything after it in
        the same read, does not."""
        try:
            keys = os.read(self.tty_in, 4096)
        except OSError:
            keys = b""
        if not keys:
            # The person's input has gone (EOF or an error). Stop watching
            # it: select would report it ready on every turn, and the loop
            # would spin at full CPU until the child exits.
            self.keys_open = False
            return False
        forward, cancel, self.first_ctrl_c = split_keys(
            keys, time.monotonic(), self.first_ctrl_c, self.cancel_window_s)
        if forward:
            self.send(forward)
        return cancel

    def resize(self) -> None:
        self.signals.winch = False
        term_cols, self.term_rows = terminal_size(self.tty_out)
        cols, rows = pty_size(term_cols, self.term_rows)
        if cols < 2 or (cols, rows) == (self.mirror.screen.cols, self.mirror.screen.rows):
            return
        shorter = rows < self.mirror.screen.rows
        set_size(self.master, cols, rows)
        self.mirror.resize(cols, rows)
        self.size = (cols, rows)
        _write_all(self.tty_out, b"\x1b[H\x1b[2J")
        if shorter:
            frame = self.mirror.quiet()             # the latch closes now, not at the next output
            if frame is not None:
                self.draw(frame)
                return
        self.draw()

    # -- the loop ------------------------------------------------------------------------

    def loop(self) -> str | None:
        grace_until: float | None = None
        self.draw(())
        while True:
            if self.signals.winch:
                self.resize()
            mirror = self.mirror
            if mirror.line and not self.line_drawn:
                self.draw()
            if mirror.latch == "tall" and not mirror.token_seen:
                return "tall"
            now = time.monotonic()
            if mirror.token_seen and grace_until is None:
                grace_until = now + self.grace_s
            if grace_until is not None and now >= grace_until:
                return "grace"
            if self.eof or self.proc.poll() is not None:
                return None
            timeout = self.quiet_s if self.dirty else 0.1
            if grace_until is not None:
                timeout = max(0.0, min(timeout, grace_until - now))
            watched = [self.master] + ([self.tty_in] if self.keys_open else [])
            ready, _, _ = select.select(watched, [], [], timeout)
            if self.master in ready:
                self.read_child()
            if self.tty_in in ready and self.read_keys():
                return "cancel"
            self.settle()


def run_setup_token(*, tty_in: int, tty_out: int, claude: str = "claude",
                    environ: Mapping[str, str] | None = None,
                    config_root: str | os.PathLike[str] | None = None,
                    grace_s: float = GRACE_S, cancel_window_s: float = CANCEL_WINDOW_S,
                    term_wait_s: float = TERM_WAIT_S, quiet_s: float = QUIET_S) -> Run:
    """Run `claude setup-token` on a private pty and read its token off the
    mirror. `tty_in` and `tty_out` are the person's terminal.

    - The child gets `child_env(environ, <its config dir>)`, a new session
      with the pty as its controlling terminal, and the config dir as its
      cwd (so no project settings in the person's cwd apply).
    - The person's terminal is in raw mode and on its alternate screen for
      the run, and restored in a `finally`, on SIGTERM or SIGHUP too.
    - The child is ended (SIGTERM, then SIGKILL) on `new`'s own exit, when
      the last-row latch closes before any token is seen, and after
      `grace_s` once a token has been seen.
    - Afterwards the config dir is scanned for the token, by path, and
      removed.

    Nothing the child writes reaches `tty_out`: only `render`'s output does.
    The caller prints `Run.captured.screen` when there is no token."""
    environ = os.environ if environ is None else environ
    if config_root is None:
        config_root = settings.accounts_dir(environ)
    term_cols, term_rows = terminal_size(tty_out)
    cols, rows = pty_size(term_cols, term_rows)
    if cols < 2:
        raise SetupTokenError(f"the terminal is {cols} columns wide")
    config_dir = make_config_dir(config_root)
    master, slave = os.openpty()
    try:
        set_size(slave, cols, rows)
        try:
            proc = subprocess.Popen([claude, "setup-token"], stdin=slave, stdout=slave,
                                    stderr=slave, env=child_env(environ, config_dir),
                                    cwd=config_dir, start_new_session=True,
                                    # one ioctl after setsid; `new` starts no threads.
                                    # macOS gives a new session no controlling tty
                                    # without it (measured): no /dev/tty at all.
                                    preexec_fn=_take_ctty,  # noqa: PLW1509
                                    close_fds=True)
        except OSError as error:
            raise SetupTokenError(f"could not run {claude} setup-token "
                                  f"({error.strerror or type(error).__name__})") from None
    except BaseException:
        os.close(master)
        remove_config_dir(config_dir)
        raise
    finally:
        os.close(slave)
    mirror = Mirror(cols, rows)
    ended: str | None = None
    saved = termios.tcgetattr(tty_in) if os.isatty(tty_in) else None
    finished = False
    try:
        with _Signals() as signals:
            try:
                if saved is not None:
                    tty.setraw(tty_in, termios.TCSANOW)
                _write_all(tty_out, ENTER_ALT)
                session = _Session(proc=proc, master=master, mirror=mirror, tty_in=tty_in,
                                   tty_out=tty_out, size=(cols, rows), term_rows=term_rows,
                                   signals=signals,
                                   grace_s=grace_s, cancel_window_s=cancel_window_s,
                                   term_wait_s=term_wait_s, quiet_s=quiet_s)
                ended = session.loop()
                if ended is not None:
                    _end(proc, term_wait_s)
                session.drain()
                if proc.poll() is None:
                    try:
                        proc.wait(timeout=term_wait_s)
                    except subprocess.TimeoutExpired:
                        _end(proc, term_wait_s)
                finished = True
            finally:
                try:
                    _write_all(tty_out, LEAVE_ALT)
                except OSError:
                    pass
                if saved is not None:
                    termios.tcsetattr(tty_in, termios.TCSADRAIN, saved)
                if not finished:
                    _end(proc, term_wait_s)
    finally:
        os.close(master)
        if not finished:
            remove_config_dir(config_dir)
    captured = mirror.finish(proc.returncode, grace_ended=ended == "grace")
    if ended == "cancel":
        captured = dataclasses.replace(
            captured, token=None,
            why=f"ended by new ({EXIT_KEY}); setup-token {_exited(proc.returncode)}")
    hits = scan_config_dir(config_dir, captured.token)
    return Run(captured, ended, hits, remove_config_dir(config_dir))
