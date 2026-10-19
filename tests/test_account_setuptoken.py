"""ST-159 do-003: the virtual screen, the mask, both latches, finding the token,
and the last drawn frame as the only screen `new` prints (design §3.2-3.3).

Pure code over byte streams: nothing here runs a process (the autouse guard
refuses a subprocess started from the module), and every token is synthetic
(`sk-ant-oat01-TEST-<random>`). The streams are shaped like Claude Code
2.1.299's setup-token, as the design read it from the binary (§1.1): an Ink
flow inside a box with `paddingLeft: 1`, the token hard-wrapped at the box's
inner width, so every token row starts with a blank; rows painted top to
bottom with CR LF; optional synchronized-output marks. The layouts follow
the design reviews' `mask_sim.py`, `cut_sim.py` and `endprint_sim.py`.

"No token character" is asserted as design §8 (round 3) states it: no 8
characters in a row of the token, past its fixed `sk-ant-oat01-` prefix
(windows that run into the body included), in any drawn frame, the end
print, the mirror's line or a repr. Also: no `sk-ant-` in any drawn frame
(the backstop), and no base64 of the token.
"""

from __future__ import annotations

import base64
import json
import os
import random
import secrets as pysecrets
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import threading
import time
from pathlib import Path

import pytest

from sessiontools.account import registry as reg
from sessiontools.account import setuptoken as st
from sessiontools.account.setuptoken import (
    DA_REPLY,
    MASK,
    TALL_LINE,
    TOKEN_LINE,
    Captured,
    Mirror,
    VirtualScreen,
    distinct_values,
    find_tokens,
    is_setup_token,
    mask_rows,
    shows_token,
)

ESC = "\x1b"
SYNC_ON = f"{ESC}[?2026h"
SYNC_OFF = f"{ESC}[?2026l"


#: The stub folders the current test made. A subprocess `setuptoken` starts
#: must resolve inside one of them (design §8): the rule is the resolved
#: path, not the name, so a stub may be called `claude`.
STUB_DIRS: list[Path] = []
#: The binaries no test may reach, from any caller, outside a stub folder.
WALLED = ("security", "pass", "passage", "age", "claude")


def _inside_a_stub(argv) -> bool:
    first = str(argv[0] if isinstance(argv, (list, tuple)) else argv)
    found = first if os.sep in first else shutil.which(first)
    if not found:
        return False
    resolved = Path(found).resolve()
    return any(resolved.is_relative_to(folder.resolve()) for folder in STUB_DIRS)


@pytest.fixture(autouse=True)
def _no_subprocess_from_the_capture(monkeypatch):
    """The screen, the mask and the latches are pure code. The run (do-004)
    may start a subprocess only when its argv[0] resolves inside this
    test's own stub folder; anything else `setuptoken` starts is a test
    failure. From any caller, the walled names are refused outside a stub
    folder. (The suite's own fixtures may still run theirs.)"""
    STUB_DIRS.clear()
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def guard(*args, _real=real, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            from_capture = sys._getframe(1).f_globals.get("__name__") == st.__name__
            walled = Path(str(argv[0] if isinstance(argv, (list, tuple)) else argv)).name in WALLED
            if (from_capture or walled) and not _inside_a_stub(argv):
                raise AssertionError("the capture ran a subprocess outside the test's stubs")
            return _real(*args, **kwargs)

        monkeypatch.setattr(subprocess, name, guard)
    yield
    STUB_DIRS.clear()


def synthetic_token(length: int = 108) -> str:
    """A synthetic setup token of `length` characters (2.1.299's is about 108)."""
    head = "sk-ant-oat01-TEST-"
    return head + pysecrets.token_urlsafe(length)[: length - len(head)]


def windows(token: str) -> list[str]:
    """Every 8 characters in a row of `token` that hold a body character."""
    start = len("sk-ant-oat01-") - 7
    return [token[i:i + 8] for i in range(start, len(token) - 7)]


def assert_no_token(token: str, *texts: str) -> None:
    pieces = windows(token)
    b64 = {base64.b64encode(token.encode()).decode(),
           base64.urlsafe_b64encode(token.encode()).decode()}
    for text in texts:
        for piece in pieces:
            assert piece not in text, "a piece of the token reached the output"
        for encoded in b64:
            assert encoded[:16] not in text, "the token's base64 reached the output"


def assert_frames_clean(token: str, frames, mirror: Mirror) -> None:
    """Nothing drawn, printed or shown by the mirror holds the token."""
    for frame in frames:
        text = "\n".join(frame)
        assert "sk-ant-" not in text, "the backstop drew a frame showing sk-ant-"
        assert_no_token(token, text)
    assert_no_token(token, mirror.end_screen(), mirror.line or "", repr(mirror),
                    repr(mirror.screen))


# --- 2.1.299-shaped screens (approximate layout, design §1.1) ---------------------------

HEADER = "Welcome to Claude Code v2.1.299"
INTRO = ("This will guide you through long-lived (1-year) auth token setup for your "
         "Claude account. Claude subscription required.")


def block(text: str, width: int, indent: int) -> list[str]:
    """Ink's hard wrap at the box's inner width (wrap-ansi: breaks at spaces,
    cuts a longer word at the width)."""
    lines = textwrap.wrap(text, width - indent, break_long_words=True,
                          break_on_hyphens=False) or [""]
    return [" " * indent + line for line in lines]


def waiting_screen(width: int, url_len: int = 346) -> list[str]:
    url = "https://claude.com/cai/oauth/authorize?" + "q=1&" * ((url_len - 39) // 4)
    rows = block(HEADER, width, 0) + [""]
    rows += block(INTRO, width, 1) + [""]
    rows += block("Browser didn't open? Use the url below to sign in", width, 0)
    rows += block(url, width, 0) + [""]
    rows += block("Paste code here if prompted > ", width, 1)
    return rows


def success_screen(token: str | list[str], width: int) -> list[str]:
    tokens = [token] if isinstance(token, str) else list(token)
    rows = block(HEADER, width, 0) + [""]
    rows += block(INTRO, width, 1) + [""]
    rows += block("✓ Long-lived authentication token created successfully!", width, 1) + [""]
    rows += block("Your OAuth token (valid for 1 year):", width, 1) + [""]
    for value in tokens:
        rows += block(value, width, 1) + [""]
    rows += block("Store this token securely. You won't be able to see it again.", width, 1)
    rows += [""]
    rows += block("Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>", width, 1)
    return rows



def refusal_screen(width: int) -> list[str]:
    return block(HEADER, width, 0) + [""] + block(
        "Error: your organization's policy does not allow this login method.", width, 1)


class Ink:
    """Paints screens as 2.1.299's renderer does while its screen fits the
    pty: rows top to bottom, cells left to right, each row ended CR LF.

    - `cells=False`: each frame moves up over the last (CUU), and erases and
      rewrites every row (a full repaint);
    - `cells=True`: each frame writes only the cells that changed, by CUP,
      skipping the rest (the selective diff);
    - `sync`: frames bracketed in `ESC[?2026h` ... `ESC[?2026l`;
    - `colour`: every non-blank run in SGR colour (the token in the warning
      colour).
    """

    def __init__(self, cols: int, *, sync: bool = True, cells: bool = False,
                 colour: bool = False) -> None:
        self.cols = cols
        self.sync = sync
        self.cells = cells
        self.colour = colour
        self.prev: list[str] = []

    def _text(self, row: str) -> str:
        if not self.colour or not row.strip():
            return row
        lead = len(row) - len(row.lstrip())
        return row[:lead] + f"{ESC}[33m" + row[lead:] + f"{ESC}[39m"

    def frame(self, rows: list[str]) -> bytes:
        out: list[str] = []
        if self.cells:
            old = [r.ljust(self.cols) for r in self.prev]
            for r, row in enumerate(rows):
                new = row.ljust(self.cols)
                before = old[r] if r < len(old) else " " * self.cols
                for c, ch in enumerate(new):
                    if before[c] != ch:
                        out.append(f"{ESC}[{r + 1};{c + 1}H{ch}")
            for r in range(len(rows), len(old)):
                out.append(f"{ESC}[{r + 1};1H{ESC}[2K")
            out.append(f"{ESC}[{len(rows) + 1};1H")
        else:
            if self.prev:
                out.append(f"{ESC}[{len(self.prev)}A\r")
            for row in rows:
                out.append(f"{ESC}[2K{self._text(row)}\r\n")
            if len(rows) < len(self.prev):
                out.append(f"{ESC}[J")
        self.prev = list(rows)
        body = "".join(out)
        if self.sync:
            body = SYNC_ON + body + SYNC_OFF
        return body.encode("utf-8")


def drive(mirror: Mirror, frames: list[bytes], *, sync: bool, chunk: int | None = None,
          rng: random.Random | None = None) -> list[tuple[str, ...]]:
    """Feed each painted frame (in pieces of `chunk` bytes, or random pieces
    with `rng`), then say the child went quiet. Returns every frame drawn."""
    drawn: list[tuple[str, ...]] = []
    for data in frames:
        pieces = [data]
        if chunk:
            pieces = [data[i:i + chunk] for i in range(0, len(data), chunk)]
        elif rng is not None:
            cuts = sorted(rng.sample(range(1, len(data)), min(12, len(data) - 1)))
            pieces = [data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)])]
        for piece in pieces:
            drawn += mirror.feed(piece)
        quiet = mirror.quiet()
        if quiet is not None:
            drawn.append(quiet)
        if not sync:
            assert mirror.screen.in_sync is False
    return drawn


# --- the virtual screen ------------------------------------------------------------------


def screen(cols: int = 20, rows: int = 5) -> VirtualScreen:
    return VirtualScreen(cols, rows)


def test_text_lands_at_the_cursor_and_rows_keep_their_width():
    s = screen()
    s.feed(b"hello")
    assert s.lines()[0] == "hello".ljust(20)
    assert (s.row, s.col) == (0, 5)
    assert all(len(line) == 20 for line in s.lines())


def test_autowrap_is_deferred_so_a_full_row_then_cr_lf_adds_no_blank_row():
    """2.1.299 writes a full token row into the last column, then CR LF
    (design §3.2): the deferred wrap must not add a row."""
    s = screen(10, 4)
    s.feed(b"0123456789")
    assert (s.row, s.col, s.pending_wrap) == (0, 9, True)
    s.feed(b"\r\nabc")
    assert s.lines()[:2] == ["0123456789", "abc".ljust(10)]


def test_a_character_after_a_full_row_wraps_to_the_next():
    s = screen(10, 4)
    s.feed(b"0123456789X")
    assert s.lines()[:2] == ["0123456789", "X".ljust(10)]


def test_cursor_moves_clamp_to_the_screen():
    s = screen(10, 4)
    s.feed(b"\x1b[99;99Hx")
    assert s.lines()[3][9] == "x"
    s.feed(b"\x1b[1;1H\x1b[5A\x1b[5Dy")
    assert s.lines()[0][0] == "y"
    s.feed(b"\x1b[2;3Hz\x1b[2Ga\x1b[3dc\x1b[Eq\x1b[Fw")
    lines = s.lines()
    assert lines[1][2] == "z" and lines[1][1] == "a" and lines[2][2] == "c"
    assert lines[3][0] == "q" and lines[2][0] == "w"


def test_erase_in_line_and_display():
    s = screen(6, 3)
    s.feed(b"abcdef\r\nghijkl\r\nmnopqr")
    s.feed(b"\x1b[2;3H\x1b[K")
    assert s.lines()[1] == "gh    "
    s.feed(b"\x1b[1K")
    assert s.lines()[1] == "      "
    s.feed(b"\x1b[1;4H\x1b[J")
    assert s.lines() == ["abc   ", "      ", "      "]
    s.feed(b"\x1b[2J")
    assert s.lines() == ["      "] * 3


def test_lf_at_the_bottom_scrolls_into_the_kept_scrollback_and_is_recorded():
    s = screen(5, 2)
    s.feed(b"one\r\ntwo")
    assert not s.scrolled
    s.feed(b"\r\nsix")
    assert s.scrolled
    assert s.scrollback == ["one  "]
    assert s.all_lines() == ["one  ", "two  ", "six  "]


def test_su_sd_and_reverse_index_scroll_and_are_recorded():
    for seq in (b"\x1b[S", b"\x1b[2T", b"\x1bM"):
        s = screen(5, 3)
        s.feed(seq)
        assert s.scrolled, seq


def test_cursor_down_never_scrolls():
    """2.1.299's clears move with CSI B, which never scrolls (§1.1)."""
    s = screen(5, 3)
    s.feed(b"\x1b[10B\x1b[10e")
    assert not s.scrolled and s.row == 2


def test_a_character_on_the_last_row_is_recorded_and_moving_there_is_not():
    s = screen(5, 3)
    s.feed(b"\r\n\r\n")
    assert s.row == 2 and not s.wrote_last_row
    s.feed(b" ")
    assert s.wrote_last_row


def test_strings_are_dropped_osc_52_and_titles_included():
    s = screen(30, 3)
    payload = base64.b64encode(b"secret-clipboard").decode()
    s.feed(f"{ESC}]52;c;{payload}\x07a{ESC}]0;a title{ESC}\\b".encode())
    s.feed(f"{ESC}P1$r0m{ESC}\\c{ESC}_apc{ESC}\\d{ESC}^pm{ESC}\\e{ESC}Xsos{ESC}\\f".encode())
    s.feed(b"\xc2\x9d52;c;" + payload.encode() + b"\x07g")   # 8-bit OSC, as UTF-8
    assert s.lines()[0].rstrip() == "abcdefg"


def test_a_string_split_across_feeds_is_still_dropped():
    s = screen(30, 3)
    s.feed(b"\x1b]52;c;QUJD")
    s.feed(b"REVG\x1b")
    s.feed(b"\\ok")
    assert s.lines()[0].rstrip() == "ok"


def test_a_csi_and_a_utf8_character_split_across_feeds_are_joined():
    s = screen(10, 3)
    data = "\x1b[2;3H✓".encode()
    for i in range(len(data)):
        s.feed(data[i:i + 1])
    assert s.lines()[1][2] == "✓"


def test_it_answers_the_cursor_position_and_device_attributes_only():
    s = screen(10, 5)
    s.feed(b"\x1b[3;4H\x1b[6n")
    assert s.take_replies() == b"\x1b[3;4R"
    s.feed(b"\x1b[c\x1b[0c")
    assert s.take_replies() == DA_REPLY * 2
    # XTVERSION, the kitty keyboard query, DA2, DECRQM 2026, DECXCPR, DSR 5:
    s.feed(b"\x1b[>0q\x1b[?u\x1b[>c\x1b[?2026$p\x1b[?6n\x1b[5n\x1b]7501\x07")
    assert s.take_replies() == b""
    assert s.take_replies() == b""


def test_sync_end_calls_back_at_its_place_in_the_stream():
    s = screen(10, 3)
    seen: list[str] = []
    s.on_sync_end = lambda: seen.append(s.lines()[0].rstrip())
    s.feed(b"\x1b[?2026hone\x1b[?2026l\x1b[?2026h\rtwo")
    assert seen == ["one"] and s.in_sync
    s.feed(b"\x1b[?2026l")
    assert seen == ["one", "two"]


def test_the_alternate_screen_keeps_the_main_one_and_no_scrollback():
    s = screen(5, 2)
    s.feed(b"main")
    s.feed(b"\x1b[?1049h")
    assert s.lines() == ["     ", "     "]
    s.feed(b"a\r\nb\r\nc")
    assert s.scrolled and s.scrollback == []
    s.feed(b"\x1b[?1049l")
    assert s.lines()[0] == "main "


def test_tab_backspace_and_wide_characters():
    s = screen(20, 2)
    s.feed(b"a\tb\bc")
    assert s.lines()[0][:9] == "a       c"
    s = screen(4, 2)
    s.feed("ab界".encode())
    assert s.lines()[0] == "ab界" and s.pending_wrap
    s.feed("x界".encode())                 # a wide character never splits across rows
    assert s.lines()[1] == "x界 "             # x, then the two cells of 界, then a blank


def test_zero_width_characters_are_dropped():
    s = screen(20, 2)
    s.feed("sk-\u200bant-\u0301x".encode())
    assert s.lines()[0].rstrip() == "sk-ant-x"


def test_insert_delete_and_erase_cells_and_lines():
    s = screen(6, 3)
    s.feed(b"abcdef\x1b[1;3H\x1b[2@")
    assert s.lines()[0] == "ab  cd"
    s.feed(b"\x1b[2P")
    assert s.lines()[0] == "abcd  "
    s.feed(b"\x1b[1;1H\x1b[2X")
    assert s.lines()[0] == "  cd  "
    s.feed(b"\x1b[1;1H\x1b[L")
    assert s.lines()[:2] == ["      ", "  cd  "]
    s.feed(b"\x1b[M")
    assert s.lines()[0] == "  cd  "


def test_save_and_restore_the_cursor():
    s = screen(10, 3)
    s.feed(b"\x1b[2;5H\x1b7\x1b[1;1H\x1b8x\x1b[3;1H\x1b[s\x1b[1;1H\x1b[uy")
    assert s.lines()[1][4] == "x" and s.lines()[2][0] == "y"


def test_a_shorter_resize_is_recorded_and_keeps_the_rows_above_the_cursor():
    s = screen(10, 4)
    s.feed(b"r0\r\nr1\r\nr2\r\nr3")
    s.resize(10, 2)
    assert s.shrunk and s.lines() == ["r2".ljust(10), "r3".ljust(10)]
    assert s.scrollback == ["r0".ljust(10), "r1".ljust(10)]
    s = screen(10, 4)
    s.feed(b"top")
    s.resize(10, 2)                         # blank rows below the cursor go first
    assert s.lines()[0].rstrip() == "top" and s.scrollback == []
    s.resize(8, 3)
    assert s.lines()[0] == "top".ljust(8) and len(s.lines()) == 3


def test_reset_clears_the_screen():
    s = screen(10, 3)
    s.feed(b"abc\x1bc")
    assert s.lines() == [" " * 10] * 3 and (s.row, s.col) == (0, 0)


def test_the_screen_repr_shows_no_cell():
    s = screen(30, 2)
    token = synthetic_token()
    s.feed(token.encode())
    assert_no_token(token, repr(s))


def test_sizes_too_small_are_refused():
    with pytest.raises(ValueError):
        VirtualScreen(1, 5)
    with pytest.raises(ValueError):
        screen().resize(5, 0)


# --- the mask (design §3.2; mask_sim.py's cases) ----------------------------------------


@pytest.mark.parametrize("width", [40, 80, 41, 79])
@pytest.mark.parametrize("indent", [0, 1])
def test_the_mask_leaves_no_token_character_on_a_wrapped_token(width, indent):
    """The peer review's H1: 2.1.299 indents the token one column, so a
    literal rule draws its tail. The blank-tolerant mask leaves nothing."""
    token = synthetic_token()
    rows = ["Your OAuth token (valid for 1 year):".ljust(width), " " * width]
    rows += [line.ljust(width) for line in block(token, width, indent)]
    rows += [" " * width, " Store this token securely.".ljust(width)]
    masked = mask_rows(rows)
    assert_no_token(token, "\n".join(masked))
    assert "sk-ant-" not in "\n".join(masked)
    assert masked[0] == rows[0] and masked[-1] == rows[-1]     # the rest is untouched
    assert all(len(a) == len(b) for a, b in zip(masked, rows))   # cell for cell


def test_a_blank_row_ends_the_masked_run_and_text_after_the_run_stops_the_wrap():
    rows = ["sk-ant-abc", "   ", "plainword", "x sk-ant-def tail", "nextword"]
    assert mask_rows(rows) == [MASK * 10, "   ", "plainword",
                               f"x {MASK * 10} tail", "nextword"]


def test_every_run_on_a_row_is_masked():
    assert mask_rows(["sk-ant-a and sk-ant-b"]) == [f"{MASK * 8} and {MASK * 8}"]


# --- finding the token (design §3.3) ----------------------------------------------------


@pytest.mark.parametrize("width", [40, 80])
def test_find_joins_the_wrapped_rows_ignoring_the_indent(width):
    token = synthetic_token()
    rows = [line.ljust(width) for line in success_screen(token, width)]
    assert find_tokens(rows) == {token}


def test_find_returns_each_distinct_run():
    a, b = synthetic_token(60), synthetic_token(60)
    rows = [f" {a}".ljust(80), "", f" {b}".ljust(80)]
    assert find_tokens(rows) == {a, b}


def test_a_partly_painted_token_folds_into_the_whole_one():
    token = synthetic_token()
    assert distinct_values({token[:30], token, "sk-ant-"}) == {token}
    other = synthetic_token()
    assert distinct_values({token, other}) == {token, other}


def test_shows_token_sees_a_prefix_on_a_row_or_split_across_two():
    assert shows_token(["x", "  sk-ant-oat01-x"])
    assert shows_token(["text ending sk-a", "nt-oat01-body"])
    assert not shows_token(["sk-an", "", "t-"])
    assert not shows_token(["nothing here"])


def test_is_setup_token_is_leakscans_shape():
    assert is_setup_token(synthetic_token())
    assert not is_setup_token("sk-ant-oat01-short")
    assert not is_setup_token("sk-ant-api03-" + "a" * 40)
    assert not is_setup_token(synthetic_token() + " ")


# --- the mirror over 2.1.299-shaped runs ------------------------------------------------

PTY_ROWS = 29       # do-004's cap (design §3.1)


def run_flow(token, width: int, *, sync=True, cells=False, colour=False, chunk=None,
             rng=None, rows: int = PTY_ROWS, repaints: int = 2, exit_code: int = 0):
    """setup-token's flow on a `width` x `rows` pty: the waiting screen
    (repainted), then the success screen, then exit."""
    ink = Ink(width, sync=sync, cells=cells, colour=colour)
    mirror = Mirror(width, rows)
    frames = [ink.frame(waiting_screen(width)) for _ in range(repaints)]
    frames.append(ink.frame(success_screen(token, width)))
    drawn = drive(mirror, frames, sync=sync, chunk=chunk, rng=rng)
    return mirror, drawn, mirror.finish(exit_code)


VARIANTS = {
    "plain": {},
    "coloured": {"colour": True},
    "no-sync-marks": {"sync": False},
    "cell-by-cell": {"cells": True},
    "cell-by-cell-no-sync": {"cells": True, "sync": False},
    "byte-by-byte": {"chunk": 1},
    "byte-by-byte-no-sync": {"chunk": 1, "sync": False},
    "random-chunks": {"rng": random.Random(159)},
}


@pytest.mark.parametrize("width", [40, 80])
@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_the_token_is_captured_and_never_drawn(variant, width):
    token = synthetic_token()
    mirror, drawn, captured = run_flow(token, width, **VARIANTS[variant])
    assert captured.token == token and captured.ok and captured.exit_code == 0
    assert captured.why == "token captured"
    assert drawn, "the waiting screen was never drawn"
    assert any("Paste code here if prompted" in "\n".join(f) for f in drawn)
    assert mirror.latch == "token" and mirror.line == TOKEN_LINE
    assert_frames_clean(token, drawn, mirror)
    assert_no_token(token, captured.screen, captured.why, repr(captured))
    assert captured.screen.endswith(TOKEN_LINE)


def test_the_frame_showing_the_token_and_every_later_one_are_never_drawn():
    token = synthetic_token()
    ink = Ink(80)
    mirror = Mirror(80, PTY_ROWS)
    assert mirror.feed(ink.frame(waiting_screen(80)))
    assert mirror.feed(ink.frame(success_screen(token, 80))) == []
    assert mirror.latch == "token"
    assert mirror.feed(ink.frame(waiting_screen(80))) == []      # closed for good
    assert mirror.quiet() is None


def test_a_frame_cut_short_by_the_quiet_rule_still_draws_no_body():
    """A partial frame (the 30 ms rule, no marks): the prefix is painted
    before the body, so the first partial frame holding a body character
    already shows sk-ant- (design §3.2). Quiet after EVERY byte."""
    for cells in (False, True):
        token = synthetic_token()
        ink = Ink(40, sync=False, cells=cells)
        mirror = Mirror(40, PTY_ROWS)
        drawn = []
        for data in (ink.frame(waiting_screen(40)), ink.frame(success_screen(token, 40))):
            for i in range(len(data)):
                drawn += mirror.feed(data[i:i + 1])
                frame = mirror.quiet()
                if frame is not None:
                    drawn.append(frame)
        assert mirror.latch == "token"
        assert_frames_clean(token, drawn, mirror)
        assert_no_token(token, mirror.finish(0).screen)


def test_no_quiet_frame_is_drawn_inside_an_open_sync_block():
    mirror = Mirror(20, 5)
    assert mirror.feed(b"\x1b[?2026hhalf") == []
    assert mirror.quiet() is None
    assert mirror.feed(b" done\x1b[?2026l") == [("half done", "", "", "", "")]


def test_an_unchanged_frame_is_not_drawn_twice():
    mirror = Mirror(20, 5)
    assert mirror.feed(b"\x1b[?2026hsame\x1b[?2026l") == [("same", "", "", "", "")]
    assert mirror.feed(b"\x1b[?2026h\x1b[1;1Hsame\x1b[?2026l") == []
    assert mirror.frames_drawn == 1


def test_osc_52_with_the_token_reaches_nothing():
    token = synthetic_token()
    ink = Ink(80)
    mirror = Mirror(80, PTY_ROWS)
    clip = base64.b64encode(token.encode()).decode()
    drawn = drive(mirror, [ink.frame(waiting_screen(80)),
                           f"{ESC}]52;c;{clip}\x07".encode(),
                           ink.frame(success_screen(token, 80))], sync=True)
    assert mirror.finish(0).token == token
    assert_frames_clean(token, drawn, mirror)
    assert not any(clip[:16] in line for line in mirror.screen.all_lines())


def test_a_policy_refusal_with_no_token_shows_its_screen():
    ink = Ink(60)
    mirror = Mirror(60, PTY_ROWS)
    drive(mirror, [ink.frame(refusal_screen(60))], sync=True)
    captured = mirror.finish(1)
    assert captured.token is None and not captured.ok
    assert captured.why == "setup-token exited 1 and showed no token"
    assert "policy does not allow" in captured.screen
    assert mirror.latch is None and mirror.line is None


def test_finish_takes_the_last_unmarked_screen_so_a_refusal_is_printed():
    """A refusal painted with no sync marks, and the child exits before the
    caller's next `quiet()`: the end print still shows why (§3.3)."""
    ink = Ink(60, sync=False)
    mirror = Mirror(60, PTY_ROWS)
    drive(mirror, [ink.frame(waiting_screen(60))], sync=False)
    mirror.feed(ink.frame(refusal_screen(60)))          # no quiet() before the exit
    captured = mirror.finish(1)
    assert "policy does not allow" in captured.screen
    assert "Paste code here" not in captured.screen


def test_finish_with_an_unmarked_token_screen_still_draws_nothing_of_it():
    """The same flush is under the latches: a success screen painted with
    no sync marks and no quiet() closes the token latch at `finish`."""
    token = synthetic_token()
    ink = Ink(80, sync=False)
    mirror = Mirror(80, PTY_ROWS)
    drawn = drive(mirror, [ink.frame(waiting_screen(80))], sync=False)
    mirror.feed(ink.frame(success_screen(token, 80)))
    captured = mirror.finish(0)
    assert captured.token == token and mirror.latch == "token"
    assert captured.screen.endswith(TOKEN_LINE)
    assert_frames_clean(token, drawn, mirror)
    assert_no_token(token, captured.screen)


def test_finish_inside_an_open_sync_block_takes_no_new_frame():
    mirror = Mirror(20, 5)
    assert mirror.feed(b"\x1b[?2026hdone\x1b[?2026l")
    mirror.feed(b"\x1b[?2026h\x1b[1;1Hhalf-painted")
    assert mirror.finish(1).screen == "done"


def test_two_different_tokens_give_none():
    a, b = synthetic_token(), synthetic_token()
    ink = Ink(80)
    mirror = Mirror(80, PTY_ROWS)
    drawn = drive(mirror, [ink.frame(waiting_screen(80)),
                           ink.frame(success_screen([a, b], 80))], sync=True)
    captured = mirror.finish(0)
    assert captured.token is None
    assert captured.why == "setup-token showed 2 different tokens: none is kept"
    for token in (a, b):
        assert_frames_clean(token, drawn, mirror)
        assert_no_token(token, captured.screen, repr(captured))


def test_two_tokens_in_two_frames_give_none():
    a, b = synthetic_token(), synthetic_token()
    ink = Ink(80)
    mirror = Mirror(80, PTY_ROWS)
    drive(mirror, [ink.frame(success_screen(a, 80)), ink.frame(success_screen(b, 80))],
          sync=True)
    assert mirror.finish(0).token is None


def test_a_non_zero_exit_keeps_no_token_unless_new_ended_it_after_the_grace():
    token = synthetic_token()
    _, _, captured = run_flow(token, 80, exit_code=1)
    assert captured.token is None
    assert captured.why == "setup-token exited 1: its token is not kept"
    mirror, _, _ = run_flow(token, 80)
    graced = mirror.finish(-15, grace_ended=True)
    assert graced.token == token
    assert graced.why == "token captured; setup-token was ended by signal 15 after the grace"


def test_a_value_not_in_the_setup_token_shape_is_not_kept():
    mirror = Mirror(80, 10)
    mirror.feed(b"\x1b[?2026h sk-ant-api03-" + b"a" * 40 + b"\x1b[?2026l")
    captured = mirror.finish(0)
    assert captured.token is None
    assert "not a setup token" in captured.why


def test_captured_never_shows_its_token():
    token = synthetic_token()
    captured = Captured(token, 0, "token captured", "")
    assert_no_token(token, repr(captured), str(captured))


# --- the last-row latch (design §3.2, re-verify) and the end print (§3.3, round 3) ------


def tall_run(token: str, width: int, rows: int, *, sync: bool = True):
    """setup-token's waiting screen grows past the pty's last row; 2.1.299
    then repaints the shorter success screen from a cut row (its offscreen
    reset, `cut_sim.py`): the token's first row is never painted, and its
    body lands on the screen with no `sk-ant-` anywhere."""
    wait = waiting_screen(width)
    succ = success_screen(token, width)
    first = next(i for i, row in enumerate(succ) if "sk-ant-" in row)
    assert len(wait) >= rows, "the waiting screen must reach the pty's last row"
    ink = Ink(width, sync=sync)
    mirror = Mirror(width, rows)
    frames = [ink.frame(wait[: rows - 2]), ink.frame(wait)]
    cut = first + 1                                   # the reset repaints from below row `first`
    reset = f"{ESC}[H{ESC}[2J" + "".join(f"{ESC}[2K{row}\r\n" for row in succ[cut:cut + rows - 1])
    if sync:
        reset = SYNC_ON + reset + SYNC_OFF
    frames.append(reset.encode())
    drawn = drive(mirror, frames, sync=sync)
    return mirror, drawn


@pytest.mark.parametrize("sync", [True, False])
@pytest.mark.parametrize("width,rows", [(40, 8), (40, 9), (60, 8), (80, 6)])
def test_a_body_cut_from_its_prefix_is_never_drawn_or_printed(width, rows, sync):
    """H1 and N1: after a cut, the virtual screen holds the token's body
    with no sk-ant-. The mask cannot see it; the latch closed at the frame
    that reached the last row, and the end print is the frame from before."""
    token = synthetic_token()
    mirror, drawn = tall_run(token, width, rows, sync=sync)
    assert mirror.latch == "tall" and mirror.line == TALL_LINE
    body_on_screen = any(piece in "\n".join(mirror.screen.lines()) for piece in windows(token))
    assert body_on_screen, "the stream did not cut the token as 2.1.299 can"
    assert not shows_token(mirror.screen.all_lines())
    leak = "\n".join(mask_rows(mirror.screen.lines()))
    assert any(piece in leak for piece in windows(token)), "a fresh read WOULD leak"
    captured = mirror.finish(0)
    assert captured.token is None
    assert "taller than this terminal, so no token is kept" in captured.why
    assert captured.screen.endswith(TALL_LINE)
    assert len(drawn) == 1                            # only the first, short frame
    assert_frames_clean(token, drawn, mirror)
    assert_no_token(token, captured.screen, captured.why, repr(captured))


def test_the_latch_closes_on_a_character_on_the_last_row():
    mirror = Mirror(20, 3)
    assert mirror.feed(b"\x1b[?2026hrow0\r\nrow1\r\n\x1b[?2026l") == [("row0", "row1", "")]
    assert mirror.feed(b"\x1b[?2026hrow2\x1b[?2026l") == []
    assert mirror.latch == "tall"
    assert mirror.end_screen() == "row0\nrow1\n" + TALL_LINE


def test_the_latch_closes_on_any_scroll():
    for seq in (b"\x1b[S", b"\x1b[T", b"\x1b[3;1H\n", b"\x1bM"):
        mirror = Mirror(20, 3)
        assert mirror.feed(b"\x1b[?2026hhi\x1b[?2026l")
        assert mirror.feed(b"\x1b[?2026h" + seq + b"\x1b[?2026l") == []
        assert mirror.latch == "tall", seq


def test_the_latch_closes_on_inserted_or_deleted_lines():
    """IL can carry a row onto the last one without a character written
    there, and DL can take a token's first row away inside one frame,
    leaving its body with no sk-ant- anywhere. Both count as a scroll."""
    for seq in (b"\x1b[1;1H\x1b[L", b"\x1b[1;1H\x1b[M"):
        mirror = Mirror(20, 4)
        assert mirror.feed(b"\x1b[?2026ha\r\nb\r\nc\x1b[?2026l")
        assert mirror.feed(b"\x1b[?2026h" + seq + b"\x1b[?2026l") == []
        assert mirror.latch == "tall", seq
    token = synthetic_token()
    mirror = Mirror(40, 20)
    rows = block(token, 40, 1)
    frame = (SYNC_ON + f"{ESC}[3;1H" + "\r\n".join(rows) + f"{ESC}[3;1H{ESC}[M"
             + SYNC_OFF).encode()
    drawn = mirror.feed(frame)
    assert drawn == [] and mirror.latch == "tall"
    assert_no_token(token, mirror.finish(0).screen)


def test_the_latch_closes_on_a_shorter_resize_and_not_on_a_taller_one():
    mirror = Mirror(20, 6)
    mirror.feed(b"\x1b[?2026hhi\x1b[?2026l")
    mirror.resize(30, 8)
    assert mirror.feed(b"\x1b[?2026hho\x1b[?2026l")
    assert mirror.latch is None
    mirror.resize(30, 7)
    assert mirror.feed(b"\x1b[?2026hhu\x1b[?2026l") == [] and mirror.latch == "tall"


def test_a_resize_shorter_then_a_cut_repaint_prints_nothing_of_the_token():
    """The other way 2.1.299's screen outgrows the pty: the pty is made
    shorter, and its resize reset repaints from a cut row (§3.2)."""
    token = synthetic_token()
    succ = success_screen(token, 40)
    first = next(i for i, row in enumerate(succ) if "sk-ant-" in row)
    ink = Ink(40)
    mirror = Mirror(40, 20)
    drawn = drive(mirror, [ink.frame(waiting_screen(40)[:12])], sync=True)
    mirror.resize(40, 6)
    reset = SYNC_ON + f"{ESC}[H{ESC}[2J" + "".join(
        f"{row}\r\n" for row in succ[first + 1:first + 4]) + SYNC_OFF
    drawn += mirror.feed(reset.encode())
    assert mirror.latch == "tall"
    captured = mirror.finish(0)
    assert captured.token is None
    assert_frames_clean(token, drawn, mirror)
    assert_no_token(token, captured.screen)


def test_a_screen_exactly_one_row_short_of_the_pty_does_not_close_the_latch():
    """A frame as tall as the pty less one: each row ends CR LF, so the
    cursor rests on the last row without writing there (§1.1)."""
    rows = 6
    ink = Ink(20)
    mirror = Mirror(20, rows)
    drawn = drive(mirror, [ink.frame([f"row{i}" for i in range(rows - 1)])], sync=True)
    assert mirror.latch is None and len(drawn) == 1
    drive(mirror, [ink.frame([f"row{i}" for i in range(rows)])], sync=True)
    assert mirror.latch == "tall"


def test_with_the_token_seen_first_a_later_scroll_keeps_it():
    """The token latch closed first: the value was read whole at that
    frame, and a later scroll does not throw it away."""
    token = synthetic_token()
    mirror, _, _ = run_flow(token, 80)
    mirror.feed(b"\x1b[?2026h" + b"\r\n" * 40 + b"\x1b[?2026l")
    assert mirror.latch == "token" and mirror.screen.scrolled
    assert mirror.finish(0).token == token


def test_a_token_painted_after_the_tall_latch_is_not_kept():
    """Its screen outgrew the pty before any token: a value read off it may
    be cut short, so none is kept, even a whole one (design §3.2, R-a)."""
    token = synthetic_token()
    mirror = Mirror(80, 6)
    ink = Ink(80)
    drive(mirror, [ink.frame(["row"] * 6), ink.frame(block(token, 80, 1) + [""])],
          sync=True)
    assert mirror.latch == "tall" and mirror.token_seen
    captured = mirror.finish(0)
    assert captured.token is None and "no token is kept" in captured.why
    assert_no_token(token, captured.screen, captured.why)


@pytest.mark.parametrize("seed", range(6))
def test_property_no_layout_draws_or_prints_a_token_character(seed):
    """Random tokens, widths and pty heights, every painter: nothing the
    mirror draws or prints holds 8 of the token's characters in a row."""
    rng = random.Random(seed)
    for _ in range(25):
        token = synthetic_token(rng.randint(60, 140))
        width = rng.randint(40, 120)
        rows = rng.randint(6, PTY_ROWS)
        options = rng.choice(list(VARIANTS.values()))
        options = {k: v for k, v in options.items() if k != "rng"}
        mirror, drawn, captured = run_flow(token, width, rows=rows, **options)
        assert_frames_clean(token, drawn, mirror)
        assert_no_token(token, captured.screen, captured.why, repr(captured))
        if captured.token is not None:
            assert captured.token == token


def test_the_module_constants_match_the_design():
    assert st.QUIET_S == 0.030 and st.GRACE_S == 10.0
    assert "captured, not shown" in TOKEN_LINE
    # R-a: `new` ends the child as the last-row latch closes, so the line says
    # the run is over, not how to end it (do-004).
    assert "taller" in TALL_LINE and "new has ended this run" in TALL_LINE
    assert st.EXIT_KEY == "Ctrl-C twice" and st.PTY_ROWS_MAX == 29


# --- the run on a pty (do-004, design §3.1, §3.4) ---------------------------------------
#
# The person's terminal is a pty pair: the run gets its slave as tty_in and
# tty_out, and the test reads everything `new` drew off its master, and types
# into it. The child is a stub `claude`, a Python script in the test's own
# stub folder, that follows a plan of steps (paint, read keys, query, write
# its config dir, exit) and logs what it saw as JSON lines. Every token is
# synthetic. Nothing here runs a real `claude`.

STUB = r'''#!{python} -I
import base64, fcntl, json, os, select, signal, struct, sys, termios, time

PLAN = {plan!r}
LOG = {log!r}


def log(event, **fields):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(dict(event=event, **fields)) + "\n")


def size():
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
    return [cols, rows]


if sys.argv[1:2] == ["-p"]:
    print("accountok")
    sys.exit(0)

try:
    os.close(os.open("/dev/tty", os.O_WRONLY))
    dev_tty = True
except OSError:
    dev_tty = False
log("start", argv=sys.argv, pid=os.getpid(), env=sorted(os.environ),
    config=os.environ.get("CLAUDE_CONFIG_DIR"),
    sync=os.environ.get("CLAUDE_CODE_FORCE_SYNC_OUTPUT"), cwd=os.getcwd(),
    size=size(), isatty=os.isatty(0), dev_tty=dev_tty, leader=os.getsid(0) == os.getpid())
WINCH = []
signal.signal(signal.SIGWINCH, lambda *_: (WINCH.append(1), log("winch", size=size())))
# raw mode, as Ink sets it (libuv's raw mode keeps output processing)
attrs = termios.tcgetattr(0)
attrs[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
attrs[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
termios.tcsetattr(0, termios.TCSANOW, attrs)


def read_for(seconds, want=None):
    got, end = b"", time.monotonic() + seconds
    while time.monotonic() < end and (want is None or len(got) < want):
        ready, _, _ = select.select([0], [], [], max(0.0, end - time.monotonic()))
        if ready:
            data = os.read(0, 1024)
            if not data:
                break
            got += data
    return got


with open(PLAN) as handle:
    steps = json.load(handle)
for step in steps:
    op = step["op"]
    data = base64.b64decode(step.get("data", ""))
    if op == "write":
        n = step.get("chunk") or max(len(data), 1)
        for i in range(0, len(data), n):
            os.write(1, data[i:i + n])
    elif op == "stderr":
        os.write(2, data)
    elif op == "tty":
        fd = os.open("/dev/tty", os.O_WRONLY)
        os.write(fd, data)
        os.close(fd)
    elif op == "ready":
        log("ready")
    elif op == "sleep":
        time.sleep(step["s"])
    elif op == "query":
        os.write(1, data)
        log("reply", data=list(read_for(step["s"])))
    elif op == "read":
        log("keys", data=list(read_for(step["s"], step.get("n"))))
    elif op == "until_winch":
        end = time.monotonic() + step["s"]
        while not WINCH and time.monotonic() < end:
            time.sleep(0.01)
    elif op == "config":
        with open(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], step["name"]), "wb") as out:
            out.write(data)
    elif op == "hang":
        while True:
            ready, _, _ = select.select([0], [], [], 1.0)
            if ready:
                log("keys", data=list(os.read(0, 1024)))
    elif op == "exit":
        log("exit", code=step["code"])
        sys.exit(step["code"])
log("end")
'''


def b64(data: bytes | str) -> str:
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


class Stub:
    """A stub `claude` in `<tmp>/stub/`, following `steps`."""

    def __init__(self, tmp_path: Path, steps: list[dict], source: str = STUB) -> None:
        folder = tmp_path / "stub"
        folder.mkdir(parents=True, exist_ok=True)
        STUB_DIRS.append(folder)
        self.plan = folder / "plan.json"
        self.log_path = folder / "log.jsonl"
        self.plan.write_text(json.dumps(steps))
        self.path = folder / "claude"
        self.path.write_text(source.format(python=sys.executable, plan=str(self.plan),
                                         log=str(self.log_path)))
        self.path.chmod(0o755)

    def events(self, name: str | None = None) -> list[dict]:
        if not self.log_path.exists():
            return []
        rows = [json.loads(line) for line in self.log_path.read_text().splitlines() if line]
        return [row for row in rows if name is None or row["event"] == name]

    def start(self) -> dict:
        return self.events("start")[0]

    def keys(self) -> bytes:
        return bytes(b for row in self.events("keys") for b in row["data"])

    def wait_for(self, name: str, timeout: float = 15.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = self.events(name)
            if found:
                return found[-1]
            time.sleep(0.01)
        raise AssertionError(f"the stub never logged {name!r}")


class Person:
    """The person's terminal: a pty pair. Everything drawn is collected."""

    def __init__(self, cols: int, rows: int) -> None:
        self.master, self.slave = os.openpty()
        st.set_size(self.slave, cols, rows)
        self.out = bytearray()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        import select
        while not self._stop.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.02)
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                self.out += data

    def type(self, data: bytes) -> None:
        os.write(self.master, data)

    def wait_for(self, text: bytes, timeout: float = 15.0) -> None:
        end = time.monotonic() + timeout
        while text not in self.out:
            if time.monotonic() > end:
                raise AssertionError(f"{text!r} was never drawn")
            time.sleep(0.01)

    def close(self) -> None:
        time.sleep(0.05)
        self._stop.set()
        self._reader.join(5)
        os.close(self.slave)
        os.close(self.master)


def modes(attrs: list) -> list:
    """termios attributes, without PENDIN: BSD's tty sets it on its own
    whenever ICANON comes back on (pending input is to be retyped), so a
    restored terminal reads with it until the next read."""
    attrs = list(attrs)
    attrs[3] &= ~getattr(termios, "PENDIN", 0)
    return attrs


class Result:
    def __init__(self, run, out: bytes, stub: Stub, before, after, home: Path) -> None:
        self.run = run
        self.out = out
        self.text = out.decode("utf-8", "replace")
        self.stub = stub
        self.before = modes(before)
        self.after = modes(after)
        self.home = home


#: No stub run in these tests takes more than a few seconds.
WATCHDOG_S = 20.0


def run_stub(tmp_path: Path, steps: list[dict], *, cols: int = 80, rows: int = 30,
             actor=None, environ: dict | None = None, source: str = STUB,
             **kwargs) -> Result:
    """Run the stub on a pty, as `new` would, with the person at a pty pair.
    `actor(person, stub)` runs on a thread beside the run (it types keys)."""
    stub = Stub(tmp_path, steps, source)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "xterm-256color",
           "TMPDIR": str(tmp_path)}
    env.update(environ or {})
    person = Person(cols, rows)
    before = termios.tcgetattr(person.slave)
    errors: list[BaseException] = []

    def act() -> None:
        try:
            actor(person, stub)
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    thread = threading.Thread(target=act, daemon=True) if actor else None
    if thread:
        thread.start()

    def kill_stub() -> None:
        """A regression must fail, never hang: a run that has not ended
        after WATCHDOG_S loses its stub (its own process group)."""
        for row in stub.events("start"):
            try:
                os.killpg(row["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    watchdog = threading.Timer(WATCHDOG_S, kill_stub)
    watchdog.daemon = True
    watchdog.start()
    try:
        run = st.run_setup_token(tty_in=person.slave, tty_out=person.slave,
                                 claude=str(stub.path), environ=env,
                                 config_root=reg.accounts_dir(home), **kwargs)
        after = termios.tcgetattr(person.slave)
    finally:
        watchdog.cancel()
        if thread:
            thread.join(20)
        person.close()
    if errors:
        raise errors[0]
    return Result(run, bytes(person.out), stub, before, after, home)


def flow(token: str, width: int, *, sync: bool = True, cells: bool = False,
         colour: bool = False, chunk: int | None = None, extra: bytes = b"") -> list[dict]:
    """2.1.299's setup-token, as a plan: its start-up queries, the waiting
    screen, a pasted code, the success screen, then exit 0 about 500 ms
    later (design §1.1)."""
    ink = Ink(width, sync=sync, cells=cells, colour=colour)
    return [
        {"op": "query", "data": b64("\x1b[>0q\x1b[?u\x1b[c"), "s": 0.3},
        {"op": "write", "data": b64(ink.frame(waiting_screen(width))), "chunk": chunk},
        {"op": "ready"},
        {"op": "read", "n": 8, "s": 10},
        {"op": "write", "data": b64(extra + ink.frame(success_screen(token, width))),
         "chunk": chunk},
        {"op": "sleep", "s": 0.5},
        {"op": "exit", "code": 0},
    ]


def paste(person: Person, stub: Stub) -> None:
    """Type the code once the prompt is on the person's screen, as a
    person would."""
    stub.wait_for("ready")
    person.wait_for(b"Paste code here")
    person.type(b"code-123\r")


def assert_terminal_clean(token: str, result: Result) -> None:
    """Nothing of the token on the person's terminal, in the end print, the
    why or a repr; the terminal restored and back on its normal screen."""
    assert "sk-ant-" not in result.text
    assert_no_token(token, result.text, result.run.captured.screen, result.run.captured.why,
                    repr(result.run), "\n".join(result.run.config_hits))
    assert result.after == result.before
    assert result.out.startswith(b"\x1b[?1049h")        # the alternate screen
    assert result.out.endswith(b"\x1b[?1049l")


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


PTY_VARIANTS = {
    "synced, 80": {"width": 80},
    "synced, 40": {"width": 40},
    "no sync marks, 80": {"width": 80, "sync": False},
    "no sync marks, 40": {"width": 40, "sync": False},
    "cell by cell": {"width": 80, "cells": True},
    "cell by cell, no sync": {"width": 40, "cells": True, "sync": False},
    "coloured": {"width": 80, "colour": True},
    "byte by byte": {"width": 40, "sync": False, "chunk": 1},
}


@pytest.mark.parametrize("variant", PTY_VARIANTS)
def test_a_stub_run_on_a_pty_yields_the_token_and_nothing_of_it_reaches_the_terminal(
        tmp_path, variant):
    options = dict(PTY_VARIANTS[variant])
    width = options.pop("width")
    token = synthetic_token()
    result = run_stub(tmp_path, flow(token, width, **options), cols=width, actor=paste)
    run = result.run
    assert run.captured.token == token and run.captured.exit_code == 0
    assert run.ended is None and run.config_hits == () and run.config_removed
    assert result.stub.keys() == b"code-123\r"          # the paste reached the child
    assert "Paste code here" in result.text              # its waiting screen was drawn
    assert st.HEADER_LINE[:width] in result.text
    assert_terminal_clean(token, result)


def test_an_osc_52_clipboard_write_of_the_token_never_reaches_the_terminal(tmp_path):
    token = synthetic_token()
    osc = f"\x1b]52;c;{b64(token)}\x07\x1b]0;{token}\x1b\\".encode()
    result = run_stub(tmp_path, flow(token, 80, extra=osc), actor=paste)
    assert result.run.captured.token == token
    assert b"\x1b]" not in result.out
    assert_terminal_clean(token, result)


def test_the_child_writes_through_dev_tty_and_that_too_goes_only_to_the_mirror(tmp_path):
    """The pty is the child's controlling terminal (§3.1): its /dev/tty is
    the pty, never the person's terminal."""
    token = synthetic_token()
    steps = [{"op": "tty", "data": b64(f"\x1b[?2026h\r\n {token}\r\n\x1b[?2026l")},
             {"op": "sleep", "s": 0.2}, {"op": "exit", "code": 0}]
    result = run_stub(tmp_path, steps)
    start = result.stub.start()
    assert start["dev_tty"] and start["leader"] and start["isatty"]
    assert result.run.captured.token == token            # read off the mirror, not shown
    assert_terminal_clean(token, result)


def test_the_queries_get_the_cursor_and_device_attributes_and_no_reply_to_dec_6n(tmp_path):
    """R-5: one stub variant sends ESC[?6n, and the guard holds: no reply,
    so no row-1 answer that would make 2.1.299 repaint (§3.2)."""
    token = synthetic_token()
    steps = [{"op": "query", "data": b64("\x1b[c"), "s": 0.5},
             {"op": "write", "data": b64("\x1b[3;5H")},
             {"op": "query", "data": b64("\x1b[6n"), "s": 0.5},
             {"op": "query", "data": b64("\x1b[?6n"), "s": 0.4},
             *flow(token, 80)[1:]]
    result = run_stub(tmp_path, steps, actor=paste)
    replies = [bytes(row["data"]) for row in result.stub.events("reply")]
    assert replies[0] == DA_REPLY
    assert replies[1] == b"\x1b[3;5R"
    assert replies[2] == b""                             # ESC[?6n: no reply
    assert result.run.captured.token == token
    assert_terminal_clean(token, result)


def test_the_child_env_is_an_allowlist(tmp_path):
    """L4: a planted credential or provider switch never reaches the stub
    `claude setup-token`; the two names `new` sets do (§3.1)."""
    planted = {"ANTHROPIC_IDENTITY_TOKEN": "x", "ANTHROPIC_IDENTITY_TOKEN_FILE": "x",
               "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1",
               "ANTHROPIC_API_KEY": "x", "ANTHROPIC_BASE_URL": "http://x",
               "CLAUDE_CODE_OAUTH_TOKEN": "x", "ANTHROPIC_CUSTOM_HEADERS": "x",
               "TMUX": "/tmp/tmux-1/default,1,0", "SSH_AUTH_SOCK": "/x", "AWS_PROFILE": "x"}
    allowed = {"LANG": "en_AU.UTF-8", "LC_ALL": "en_AU.UTF-8", "HTTPS_PROXY": "http://p",
               "SSH_TTY": "/dev/ttys1", "BROWSER": "true"}
    steps = [{"op": "exit", "code": 1}]
    result = run_stub(tmp_path, steps, environ={**planted, **allowed})
    start = result.stub.start()
    names = set(start["env"])
    assert not names & set(planted)
    assert set(allowed) <= names and {"PATH", "HOME", "TERM", "TMPDIR"} <= names
    assert start["sync"] == "1"
    config = Path(start["config"])
    assert config.name.startswith(st.CONFIG_PREFIX)
    assert config.parent == reg.accounts_dir(result.home)
    assert os.path.realpath(start["cwd"]) == os.path.realpath(config)
    assert start["argv"][1:] == ["setup-token"]
    assert not config.exists() and result.run.config_removed


def test_child_env_keeps_only_the_allowlist():
    env = st.child_env({"PATH": "/bin", "LC_CTYPE": "UTF-8", "ANTHROPIC_FOUNDRY_API_KEY": "x",
                        "CLAUDE_CODE_USE_FOUNDRY": "1", "ALL_INPUTS": "x"}, "/c")
    assert env == {"PATH": "/bin", "LC_CTYPE": "UTF-8", "TERM": "xterm-256color",
                   "CLAUDE_CONFIG_DIR": "/c", "CLAUDE_CODE_FORCE_SYNC_OUTPUT": "1"}


def test_a_token_written_into_the_config_dir_is_reported_by_path_and_removed(tmp_path):
    """§3.1: a stub that saves the token into its CLAUDE_CONFIG_DIR is
    reported by path, never by value, and the folder is removed."""
    token = synthetic_token()
    steps = [{"op": "config", "name": "credentials.json",
              "data": b64(json.dumps({"claudeAiOauth": {"accessToken": token}}))},
             {"op": "config", "name": "settings.json", "data": b64("{}")},
             *flow(token, 80)]
    result = run_stub(tmp_path, steps, actor=paste)
    run = result.run
    assert run.captured.token == token
    assert run.config_hits == ("credentials.json",)
    assert run.config_removed
    assert not list((reg.accounts_dir(result.home)).glob(f"{st.CONFIG_PREFIX}*"))
    assert_terminal_clean(token, result)


def test_the_config_scan_finds_a_token_shape_and_masks_a_token_in_a_name(tmp_path):
    token = synthetic_token()
    folder = tmp_path / "cfg"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "shape").write_bytes(b"x " + synthetic_token().encode())
    (folder / f"{token}.txt").write_text("nothing")
    (folder / "hex").write_text(token.encode().hex())
    (folder / "link").symlink_to(folder / "sub" / "shape")
    hits = st.scan_config_dir(folder, token)
    assert len(hits) == 2 and "sub/shape" in hits and "hex" in hits
    assert st.scan_config_dir(folder, None) == hits
    assert_no_token(token, "\n".join(hits))
    assert st.remove_config_dir(folder) and not folder.exists()


def test_the_pty_takes_the_width_and_the_height_less_one_never_over_29_rows(tmp_path):
    assert st.pty_size(80, 40) == (80, 29)
    assert st.pty_size(80, 30) == (80, 29)
    assert st.pty_size(80, 24) == (80, 23)
    assert st.pty_size(120, 2) == (120, 1) and st.pty_size(40, 1) == (40, 1)
    result = run_stub(tmp_path, [{"op": "exit", "code": 1}], cols=100, rows=50)
    assert result.stub.start()["size"] == [100, 29]
    result = run_stub(tmp_path / "b", [{"op": "exit", "code": 1}], cols=60, rows=20)
    assert result.stub.start()["size"] == [60, 19]


def test_the_persons_terminal_is_raw_for_the_run_and_restored_after(tmp_path):
    seen = {}

    def look(person, stub):
        stub.wait_for("ready")
        seen["lflag"] = termios.tcgetattr(person.slave)[3]
        person.type(b"x")

    result = run_stub(tmp_path, [{"op": "ready"}, {"op": "read", "n": 1, "s": 10},
                                 {"op": "exit", "code": 1}], actor=look)
    assert not seen["lflag"] & (termios.ICANON | termios.ECHO | termios.ISIG)
    assert result.before[3] & termios.ICANON
    assert result.after == result.before
    assert result.out.endswith(st.LEAVE_ALT)


def test_a_wider_and_taller_resize_reaches_the_child(tmp_path):
    token = synthetic_token()

    def grow(person, stub):
        stub.wait_for("ready")
        st.set_size(person.slave, 70, 26)
        os.kill(os.getpid(), signal.SIGWINCH)

    steps = [{"op": "ready"}, {"op": "until_winch", "s": 10},
             {"op": "write", "data": b64(Ink(70).frame(success_screen(token, 70)))},
             {"op": "sleep", "s": 0.3}, {"op": "exit", "code": 0}]
    result = run_stub(tmp_path, steps, cols=60, rows=20, actor=grow)
    assert result.stub.start()["size"] == [60, 19]
    assert result.stub.events("winch")[-1]["size"] == [70, 25]
    assert result.run.captured.token == token and result.run.ended is None
    assert_terminal_clean(token, result)


def test_a_shorter_resize_closes_the_latch_and_ends_the_child(tmp_path):
    """§3.2: a shorter pty closes the last-row latch; R-a: no token was
    seen, so `new` ends the child at once."""
    def shrink(person, stub):
        stub.wait_for("ready")
        st.set_size(person.slave, 80, 20)
        os.kill(os.getpid(), signal.SIGWINCH)

    steps = [{"op": "write", "data": b64(Ink(80).frame(waiting_screen(80)))},
             {"op": "ready"}, {"op": "hang"}]
    result = run_stub(tmp_path, steps, cols=80, rows=24, actor=shrink)
    run = result.run
    assert run.ended == "tall" and run.captured.token is None
    assert run.captured.exit_code == -signal.SIGTERM
    assert gone(result.stub.start()["pid"])
    assert "new has ended this run" in result.text.replace("\x1b[K", "")
    assert result.after == result.before


def test_r_a_the_child_is_ended_when_the_last_row_latch_closes_before_a_token(tmp_path):
    """R-a (round 3): a screen that grows past the pty's last row closes the
    latch; no token was seen, so `new` ends the child (it would wait for a
    paste for ever) and keeps none."""
    tall = "".join(f"row {i}\r\n" for i in range(40))
    steps = [{"op": "write", "data": b64(SYNC_ON + tall + SYNC_OFF)},
             {"op": "ready"}, {"op": "hang"}]
    started = time.monotonic()
    result = run_stub(tmp_path, steps)
    run = result.run
    assert run.ended == "tall" and run.captured.token is None
    assert result.run.captured.screen.endswith(TALL_LINE)
    assert gone(result.stub.start()["pid"])
    assert time.monotonic() - started < 8
    assert "row 0" not in result.text                  # that frame was never drawn
    assert result.after == result.before


def test_a_tall_screen_then_a_cut_token_body_draws_and_prints_nothing_of_it(tmp_path):
    """2.1.299's offscreen reset: after the screen outgrew the pty, a token's
    later rows are painted without its first, and the child exits 0
    (§3.3). Nothing of the token is drawn or printed, and none is kept."""
    token = synthetic_token()
    rows = block(token, 40, 1)
    tall = "".join(f"row {i}\r\n" for i in range(40))
    cut = f"{ESC}[H" + "\r\n".join(rows[1:]) + "\r\n"
    steps = [{"op": "write", "data": b64(SYNC_ON + tall + SYNC_OFF + SYNC_ON + cut + SYNC_OFF)},
             {"op": "exit", "code": 0}]
    result = run_stub(tmp_path, steps, cols=40)
    assert result.run.captured.token is None
    assert_terminal_clean(token, result)


def test_a_policy_refusal_on_stderr_is_in_the_end_print(tmp_path):
    """2.1.299 exits 1 under a managed policy, with its message on stderr
    (§1.1). The pty is its stderr too, so the person sees why."""
    steps = [{"op": "write", "data": b64(Ink(80, sync=False).frame(waiting_screen(80)))},
             {"op": "stderr", "data": b64("\r\nError: your organization's policy does not "
                                          "allow this login method.\r\n")},
             {"op": "exit", "code": 1}]
    result = run_stub(tmp_path, steps)
    run = result.run
    assert run.captured.token is None and run.captured.exit_code == 1
    assert "policy does not allow" in run.captured.screen
    assert run.ended is None and result.after == result.before


def test_two_different_tokens_on_a_pty_give_none(tmp_path):
    a, b = synthetic_token(), synthetic_token()
    ink = Ink(80)
    steps = [{"op": "write", "data": b64(ink.frame(success_screen([a, b], 80)))},
             {"op": "sleep", "s": 0.2}, {"op": "exit", "code": 0}]
    result = run_stub(tmp_path, steps)
    assert result.run.captured.token is None
    assert_terminal_clean(a, result)
    assert_terminal_clean(b, result)


def test_after_the_grace_the_child_is_ended_and_the_token_kept(tmp_path):
    token = synthetic_token()
    steps = [{"op": "write", "data": b64(Ink(80).frame(success_screen(token, 80)))},
             {"op": "hang"}]
    result = run_stub(tmp_path, steps, grace_s=0.3)
    run = result.run
    assert run.ended == "grace" and run.captured.token == token
    assert run.captured.exit_code == -signal.SIGTERM
    assert gone(result.stub.start()["pid"])
    assert_terminal_clean(token, result)


def test_the_first_of_two_ctrl_c_reaches_the_child_and_the_second_ends_it(tmp_path):
    """§3.4 (round 4's pin): the first Ctrl-C goes to the child as a byte;
    a second within the window is `new`'s own exit. It ends a stub that
    ignores both, keeps no token, and restores the terminal."""
    def twice(person, stub):
        stub.wait_for("ready")
        person.type(b"\x03")
        end = time.monotonic() + 5
        while not stub.events("keys") and time.monotonic() < end:
            time.sleep(0.01)
        person.type(b"\x03")

    steps = [{"op": "write", "data": b64(Ink(80).frame(waiting_screen(80)))},
             {"op": "ready"}, {"op": "hang"}]
    result = run_stub(tmp_path, steps, actor=twice)
    run = result.run
    assert run.ended == "cancel" and run.captured.token is None
    assert result.stub.keys() == b"\x03"                 # the first only
    assert run.captured.exit_code == -signal.SIGTERM
    assert st.EXIT_KEY in run.captured.why
    assert gone(result.stub.start()["pid"])
    assert result.after == result.before and result.out.endswith(st.LEAVE_ALT)


def test_a_ctrl_c_after_a_token_was_seen_still_cancels_and_keeps_none(tmp_path):
    token = synthetic_token()

    def twice(person, stub):
        stub.wait_for("ready")
        person.type(b"\x03\x03")

    steps = [{"op": "write", "data": b64(Ink(80).frame(success_screen(token, 80)))},
             {"op": "ready"}, {"op": "hang"}]
    result = run_stub(tmp_path, steps, actor=twice)
    assert result.run.ended == "cancel" and result.run.captured.token is None
    assert result.stub.keys() in (b"", b"\x03")        # the first, if it was read in time
    assert_terminal_clean(token, result)


def test_split_keys_passes_the_first_ctrl_c_and_holds_back_the_second():
    assert st.split_keys(b"ab\x03cd", 10.0, None) == (b"ab\x03cd", False, 10.0)
    assert st.split_keys(b"x\x03yz", 10.5, 10.0) == (b"x", True, 10.0)
    assert st.split_keys(b"\x03\x03tail", 3.0, None) == (b"\x03", True, 3.0)
    assert st.split_keys(b"\x03", 12.0, 10.0) == (b"\x03", False, 12.0)       # too late
    assert st.split_keys(b"\x03", 10.2, 10.0, window_s=0.1) == (b"\x03", False, 10.2)
    assert st.split_keys(b"plain", 1.0, 0.5) == (b"plain", False, 0.5)


def test_a_cancel_keeps_no_token_even_when_the_child_exits_0(tmp_path):
    """The cancel is `new`'s: a child that answers SIGTERM with exit 0, a
    token on its screen, still gives none."""
    token = synthetic_token()
    exits_0 = STUB.replace("WINCH = []", "WINCH = []\nsignal.signal(signal.SIGTERM, "
                                         "lambda *_: os._exit(0))")

    def twice(person, stub):
        stub.wait_for("ready")
        person.type(b"\x03\x03")

    steps = [{"op": "write", "data": b64(Ink(80).frame(success_screen(token, 80)))},
             {"op": "ready"}, {"op": "hang"}]
    result = run_stub(tmp_path, steps, actor=twice, source=exits_0)
    assert result.run.ended == "cancel" and result.run.captured.exit_code == 0
    assert result.run.captured.token is None
    assert_terminal_clean(token, result)


def test_two_ctrl_c_further_apart_than_the_window_both_reach_the_child(tmp_path):
    def slow(person, stub):
        stub.wait_for("ready")
        person.type(b"\x03")
        time.sleep(0.5)
        person.type(b"\x03")

    steps = [{"op": "ready"}, {"op": "read", "n": 2, "s": 10}, {"op": "exit", "code": 1}]
    result = run_stub(tmp_path, steps, actor=slow, cancel_window_s=0.2)
    assert result.run.ended is None
    assert result.stub.keys() == b"\x03\x03"


def test_a_child_that_ignores_sigterm_is_killed(tmp_path):
    ignores = STUB.replace("WINCH = []",
                           "WINCH = []\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)")

    def twice(person, stub):
        stub.wait_for("ready")
        person.type(b"\x03\x03")

    started = time.monotonic()
    result = run_stub(tmp_path, [{"op": "ready"}, {"op": "hang"}], actor=twice,
                      source=ignores, term_wait_s=0.3)
    assert result.run.ended == "cancel"
    assert result.run.captured.exit_code == -signal.SIGKILL
    assert gone(result.stub.start()["pid"])
    assert time.monotonic() - started < 8


@pytest.fixture
def watched(monkeypatch):
    """The person's terminal as it was when made, and as it was, and what
    was drawn on it, when closed: for runs that raise."""
    seen: dict = {}
    made, closed = Person.__init__, Person.close

    def init(self, cols, rows):
        made(self, cols, rows)
        seen["before"] = modes(termios.tcgetattr(self.slave))

    def close(self):
        seen["after"] = modes(termios.tcgetattr(self.slave))
        seen["out"] = bytes(self.out)
        closed(self)

    monkeypatch.setattr(Person, "__init__", init)
    monkeypatch.setattr(Person, "close", close)
    return seen


def test_the_terminal_is_restored_and_the_child_ended_after_a_crash(tmp_path, monkeypatch,
                                                                   watched):
    def boom(self, data):
        raise RuntimeError("a crash in the mirror")

    def ready(person, stub):
        stub.wait_for("ready")
        watched["pid"] = stub.start()["pid"]
        person.type(b"x")

    monkeypatch.setattr(st.Mirror, "feed", boom)
    steps = [{"op": "ready"}, {"op": "read", "n": 1, "s": 10},
             {"op": "write", "data": b64("hello")}, {"op": "hang"}]
    with pytest.raises(RuntimeError, match="a crash in the mirror"):
        run_stub(tmp_path, steps, actor=ready)
    assert watched["after"] == watched["before"]
    assert watched["out"].endswith(st.LEAVE_ALT)
    assert gone(watched["pid"])
    assert not list((reg.accounts_dir(tmp_path / "home")).glob(f"{st.CONFIG_PREFIX}*"))


def test_sigterm_to_new_restores_the_terminal_and_raises_interrupted(tmp_path, watched):
    handler = signal.getsignal(signal.SIGTERM)

    def term(person, stub):
        stub.wait_for("ready")
        watched["pid"] = stub.start()["pid"]
        os.kill(os.getpid(), signal.SIGTERM)

    with pytest.raises(st.Interrupted) as raised:
        run_stub(tmp_path, [{"op": "ready"}, {"op": "hang"}], actor=term)
    assert raised.value.signum == signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == handler
    assert watched["after"] == watched["before"]
    assert watched["out"].endswith(st.LEAVE_ALT)
    assert gone(watched["pid"])
    assert not list((reg.accounts_dir(tmp_path / "home")).glob(f"{st.CONFIG_PREFIX}*"))


def test_a_claude_that_cannot_run_is_a_setup_token_error_and_leaves_nothing(tmp_path):
    folder = tmp_path / "stub"
    folder.mkdir()
    STUB_DIRS.append(folder)
    person = Person(80, 30)
    try:
        with pytest.raises(st.SetupTokenError, match="could not run"):
            st.run_setup_token(tty_in=person.slave, tty_out=person.slave,
                               claude=str(folder / "missing"), environ={"PATH": "/bin"},
                               config_root=tmp_path / "cfg")
    finally:
        person.close()
    assert list((tmp_path / "cfg").iterdir()) == []


def test_the_guard_refuses_a_claude_outside_the_stub_folder(tmp_path):
    person = Person(80, 30)
    try:
        with pytest.raises(AssertionError, match="outside the test's stubs"):
            st.run_setup_token(tty_in=person.slave, tty_out=person.slave,
                               environ={"PATH": os.environ.get("PATH", "")},
                               config_root=tmp_path / "cfg")
    finally:
        person.close()


def test_render_replaces_controls_and_draws_nothing_but_the_frame():
    out = st.render(("ok\x1b]52;c;x\x07", "\x9b2J tail"), cols=20, line=None,
                    cursor=(1, 3)).decode()
    body = out.replace("\x1b[?2026h", "").replace("\x1b[?2026l", "")
    for seq in ("\x1b]", "\x07", "\x9b"):
        assert seq not in body
    assert "ok?]52;c;x?" in body and "?2J tail" in body
    assert body.endswith("\x1b[?25h\x1b[3;4H")
    lined = st.render(("a",), cols=30, line=TALL_LINE).decode()
    assert "\x1b[?25l" in lined and "setup-token's screen is" in lined
    assert st.HEADER_LINE[:30] in lined


@pytest.mark.parametrize("cols,term_rows", [(80, 24), (40, 24), (40, 12), (80, 30), (80, 40)])
@pytest.mark.parametrize("filled", [5, None])
def test_render_draws_the_whole_line_inside_the_terminal(cols, term_rows, filled):
    """do-004 review: the frame alone fits the terminal (the pty is a row
    shorter), so a line drawn after it went past the last row. Each wrapped
    piece landed on that row over the one before, and only the tail of the
    latch's line showed. Now the whole line is inside the terminal, the
    frame giving up rows from the bottom when it must."""
    _, prows = st.pty_size(cols, term_rows)
    count = prows if filled is None else filled
    frame = tuple([f"row {i}" for i in range(count)] + [""] * (prows - count))
    for line in (TALL_LINE, TOKEN_LINE):
        screen = st.VirtualScreen(cols, term_rows)
        screen.feed(st.render(frame, cols=cols, rows=term_rows, line=line))
        shown = [row.rstrip() for row in screen.lines()]
        assert not screen.scrolled
        assert shown[0] == st.HEADER_LINE[:cols]
        drawn = [row for row in shown[1:] if row and not row.startswith("row ")]
        assert " ".join(drawn) == line
        kept = [row for row in shown[1:] if row.startswith("row ")]
        assert kept == [f"row {i}" for i in range(len(kept))]   # cut from the bottom only
        if filled is not None and term_rows >= 12:
            assert len(kept) == filled                          # blank rows go first
            assert shown[1 + filled] == drawn[0]                # the line under the frame


def test_render_without_rows_or_line_draws_every_frame_row():
    frame = ("a", "", "b", "")
    body = st.render(frame, cols=10, cursor=(0, 0)).decode()
    for row in range(2, 2 + len(frame)):
        assert f"\x1b[{row};1H" in body
    assert "\x1b[6;1H\x1b[J" in body


def test_render_keeps_the_last_character_of_a_row_that_fills_the_width():
    """An erase after a row that fills the last column would take its last
    character (the cursor waits on that column for the wrap)."""
    screen = st.VirtualScreen(10, 4)
    screen.feed(st.render(("abcdefghij", "short"), cols=10, cursor=(1, 5)))
    assert [row.rstrip() for row in screen.lines()] == [
        st.HEADER_LINE[:10], "abcdefghij", "short", ""]


def test_input_at_eof_is_read_once_and_then_no_longer_watched(tmp_path, monkeypatch):
    """do-004 review: a read of b"" from the person's input (EOF, or an
    error) left it in the select set, so the loop spun at full CPU until
    the child exited. Now it is read once, and dropped."""
    stub = Stub(tmp_path, [{"op": "sleep", "s": 0.8}, {"op": "exit", "code": 1}])
    gone_in, writer = os.pipe()
    os.close(writer)
    reads = []
    real_read = os.read

    def counting(fd, n):
        if fd == gone_in:
            reads.append(fd)
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", counting)
    person = Person(80, 24)
    try:
        run = st.run_setup_token(tty_in=gone_in, tty_out=person.slave, claude=str(stub.path),
                                 environ={"PATH": os.environ.get("PATH", ""),
                                          "HOME": str(tmp_path)},
                                 config_root=tmp_path / "cfg")
    finally:
        person.close()
        os.close(gone_in)
    assert run.captured.exit_code == 1 and run.ended is None
    assert len(reads) == 1


def test_on_a_pty_the_latch_line_is_drawn_whole_under_a_full_waiting_screen(tmp_path):
    """The run passes the person's height to `render`: at 40x24 the waiting
    screen is about 21 rows of a 23-row pty, and the latch's line wraps to
    4 rows, so drawn under the frame it would run off the terminal."""
    tall = "".join(f"row {i}\r\n" for i in range(40))
    steps = [{"op": "write", "data": b64(Ink(40).frame(waiting_screen(40)))},
             {"op": "sleep", "s": 0.3},
             {"op": "write", "data": b64(SYNC_ON + tall + SYNC_OFF)},
             {"op": "ready"}, {"op": "hang"}]
    result = run_stub(tmp_path, steps, cols=40, rows=24)
    assert result.run.ended == "tall"
    assert result.out.endswith(st.LEAVE_ALT)
    screen = st.VirtualScreen(40, 24)
    screen.feed(result.out[:-len(st.LEAVE_ALT)])
    shown = [row.rstrip() for row in screen.lines()]
    assert " ".join(shown[-4:]) == TALL_LINE
    assert not screen.scrolled


def test_a_shorter_terminal_under_the_29_row_cap_still_gets_the_whole_line(tmp_path):
    """do-004 round 2: from 40 rows to 30 the pty stays at its 29-row cap, so
    the child is not resized and the latch stays open. But `render` must use
    the new height, or the line drawn under a full frame runs off the
    terminal's last row (each wrapped piece over the one before)."""
    rows28 = "".join(f"row {i}\r\n" for i in range(28))

    def shrink(person, stub):
        stub.wait_for("ready")
        person.wait_for(b"row 27")
        st.set_size(person.slave, 80, 30)
        os.kill(os.getpid(), signal.SIGWINCH)

    steps = [{"op": "write", "data": b64(SYNC_ON + rows28 + SYNC_OFF)},
             {"op": "ready"}, {"op": "sleep", "s": 1.0},
             {"op": "write", "data": b64(f"{SYNC_ON}{ESC}[29;1HX{SYNC_OFF}")},
             {"op": "hang"}]
    result = run_stub(tmp_path, steps, cols=80, rows=40, actor=shrink)
    assert result.run.ended == "tall"
    assert result.stub.events("winch") == []             # the pty kept its size
    screen = st.VirtualScreen(80, 30)
    screen.feed(result.out[:-len(st.LEAVE_ALT)])
    shown = [row.rstrip() for row in screen.lines()]
    assert " ".join(shown[-2:]) == TALL_LINE
    assert shown[1] == "row 0" and not screen.scrolled
