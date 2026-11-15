"""md-lint: after a Write or Edit of a Markdown file, name the lines that
break two house rules. It answers; it never blocks.

    md-lint docs/guide.md:
    line 14 is 134 columns; a table line stays under 76 columns, wider lines wrap
    line 9 says 'shape': use the precise term

It runs as the plugin's PostToolUse hook (`hooks/md-lint`, a POSIX shim
that runs `main()`), matcher `Write|Edit|MultiEdit`, and acts only when
`tool_input.file_path` ends in `.md` (any case).

THE TWO RULES, outside YAML front matter (a `---` block at line 1),
fenced code (``` or ~~~) and HTML comments:

- TABLE: a line whose stripped text starts with `|` and is 76 or more
  DISPLAY columns wide. East Asian wide and fullwidth characters count 2,
  combining marks 0, everything else 1 (a tab included).
- SHAPE: the whole word `shape` or `shapes`, any case, in prose. Not in an
  inline code span, link text or target, an autolink, a bare URL, a
  reference definition, double-quoted text (straight or curly), single
  curly quotes, or a blockquote line. Word boundaries keep out `reshape`,
  `shaped`, `shaping` and `misshapen`. Table cells are prose.

One finding per offending line per rule.

WHAT IS REPORTED. A Write lints the whole file. An Edit or MultiEdit lints
the whole file as it is on disk after the edit, so fence and front-matter
state is known, but reports only lines where an edit's `new_string` now
sits (every occurrence of it). Problems elsewhere in the file are not
named. At most `CAP` finding lines, then `... and N more`.

IT NEVER BREAKS A TURN. `main()` swallows everything and returns 0, prints
only the hook JSON and only when there is a finding, and never writes to
stderr.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import IO

#: A table line this wide or wider is named.
TABLE_MAX = 76
#: At most this many finding lines; the rest are counted in one line.
CAP = 10
#: The tools whose result is linted.
TOOLS = ("Write", "Edit", "MultiEdit")

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_BLOCKQUOTE = re.compile(r"^ {0,3}>")
_REFDEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*\S")
_SHAPE = re.compile(r"\bshapes?\b", re.IGNORECASE)
_TICKS = re.compile(r"`+")
_BRACKETS = r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]"
#: Masked in this order, each on the text the ones before it left.
_SPANS = (
    re.compile(r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>"),     # <scheme:...>
    re.compile(r"<[^\s<>@]+@[^\s<>]+>"),                        # <a@b>
    re.compile(r"!?" + _BRACKETS + r"\((?:[^()]|\([^()]*\))*\)"),  # [t](u)
    re.compile(r"!?" + _BRACKETS + r"\[[^\[\]]*\]"),            # [t][r], [t][]
    re.compile(r"\bhttps?://\S+", re.IGNORECASE),               # bare URL
    re.compile(r'"[^"]*"'),                                     # "..."
    re.compile("“[^“”]*”"),                 # curly double
    re.compile("‘[^‘’]*’"),                 # curly single
)


@dataclass(frozen=True)
class Finding:
    """One rule broken on one line (1-based)."""

    line: int
    rule: str      # "table" or "shape"
    message: str


def width(text: str) -> int:
    """Display columns: wide/fullwidth 2, combining and zero-width 0, else 1."""
    cols = 0
    for ch in text:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
            continue
        cols += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return cols


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _mask_code(text: str) -> str:
    """Blank inline code spans: a backtick run closed by a run of the same
    length. An unclosed run is literal and left as it is."""
    runs = list(_TICKS.finditer(text))
    i = 0
    while i < len(runs):
        opener = runs[i]
        for j in range(i + 1, len(runs)):
            if len(runs[j].group()) == len(opener.group()):
                text = _blank(text, opener.start(), runs[j].end())
                i = j
                break
        i += 1
    return text


def _mask_comments(text: str, inside: bool) -> tuple[str, bool]:
    """Blank HTML comment text; `inside` is whether the line starts in one.
    Returns the masked line and whether it ends inside one."""
    pos = 0
    while True:
        if inside:
            close = text.find("-->", pos)
            if close < 0:
                return _blank(text, pos, len(text)), True
            text = _blank(text, pos, close + 3)
            pos, inside = close + 3, False
        opening = text.find("<!--", pos)
        if opening < 0:
            return text, False
        pos, inside = opening, True


def mask(text: str) -> str:
    """The line with every span the SHAPE rule skips blanked to spaces, so
    columns are kept. Code spans and comments are the caller's to blank first."""
    if _REFDEF.match(text):
        return " " * len(text)
    for pattern in _SPANS:
        text = pattern.sub(lambda m: " " * len(m.group()), text)
    return text


def _front_matter_end(lines: list[str]) -> int:
    """The 0-based index of the line closing front matter at line 1, or -1."""
    if not lines or lines[0].rstrip() != "---":
        return -1
    for i in range(1, len(lines)):
        if lines[i].rstrip() in ("---", "..."):
            return i
    return -1


def lint(text: str) -> list[Finding]:
    """Every finding in TEXT, in line order (table before shape on a line)."""
    # Lines end at "\n" only (a "\r" before it dropped), as an editor and
    # regions() count them: str.splitlines() also breaks at a form feed,
    # U+2028 and others, which would shift every line number after one.
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    findings: list[Finding] = []
    skip_to = _front_matter_end(lines)
    fence: str | None = None          # the opening run while inside a fence
    in_comment = False
    for i, raw in enumerate(lines):
        if i <= skip_to:
            continue
        number = i + 1
        match = _FENCE.match(raw)
        if fence is not None:
            if match and match.group(1)[0] == fence[0] \
                    and len(match.group(1)) >= len(fence) and not match.group(2).strip():
                fence = None
            continue
        if match and not in_comment and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            fence = match.group(1)
            continue
        started_in_comment = in_comment
        prose = _mask_code(raw) if not in_comment else raw
        prose, in_comment = _mask_comments(prose, in_comment)
        if started_in_comment:
            prose = _mask_code(prose)
        if not started_in_comment and raw.lstrip().startswith("|"):
            cols = width(raw)
            if cols >= TABLE_MAX:
                findings.append(Finding(number, "table",
                                        f"line {number} is {cols} columns; a table line"
                                        f" stays under {TABLE_MAX} columns, wider lines wrap"))
        if _BLOCKQUOTE.match(raw):
            continue
        hit = _SHAPE.search(mask(prose))
        if hit:
            findings.append(Finding(number, "shape",
                                    f"line {number} says '{hit.group()}': use the precise term"))
    return findings


def regions(text: str, new_strings: Iterable[str]) -> set[int]:
    """The 1-based lines where any of NEW_STRINGS sits in TEXT, every
    occurrence. An empty string marks nothing."""
    marked: set[int] = set()
    for new in new_strings:
        if not new:
            continue
        start = text.find(new)
        while start >= 0:
            first = text.count("\n", 0, start) + 1
            last = text.count("\n", 0, start + len(new) - 1) + 1
            marked.update(range(first, last + 1))
            start = text.find(new, start + len(new))
    return marked


def _new_strings(tool: str, tool_input: dict) -> list[str]:
    if tool == "Edit":
        edits = [tool_input]
    else:
        edits = tool_input.get("edits") or []
    return [e["new_string"] for e in edits
            if isinstance(e, dict) and isinstance(e.get("new_string"), str)]


def report(path: str, findings: list[Finding]) -> str | None:
    """The additionalContext text, or None when there is nothing to say."""
    if not findings:
        return None
    lines = [f"md-lint {path}:"] + [f.message for f in findings[:CAP]]
    if len(findings) > CAP:
        lines.append(f"... and {len(findings) - CAP} more")
    return "\n".join(lines)


def decide(body: object) -> str | None:
    """The hook's whole decision from its payload: the text to say, or None."""
    if not isinstance(body, dict):
        return None
    tool = body.get("tool_name")
    tool_input = body.get("tool_input")
    if tool not in TOOLS or not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path.lower().endswith(".md"):
        return None
    full = path
    if not os.path.isabs(full) and isinstance(body.get("cwd"), str):
        full = os.path.join(body["cwd"], full)
    with open(full, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    findings = lint(text)
    if tool != "Write":
        keep = regions(text, _new_strings(tool, tool_input))
        findings = [f for f in findings if f.line in keep]
    return report(path, findings)


def main(stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> int:
    """Read the PostToolUse payload, print the hook JSON when there is a
    finding. Always 0; never a word on stderr."""
    try:
        text = decide(json.load(stdin or sys.stdin))
        if text is not None:
            out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                          "additionalContext": text}}
            (stdout or sys.stdout).write(json.dumps(out) + "\n")
    except BaseException:  # noqa: BLE001, S110 - a hook must never break a turn
        pass
    return 0
