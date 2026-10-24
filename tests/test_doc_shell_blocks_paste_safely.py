"""No command line in a doc's shell block carries a `#` comment.

zsh, the Mac's own shell, leaves `interactivecomments` off at the prompt,
so a `#` typed or pasted there is a word, not a comment. A pasted
`session reap   # a dry run` runs `session reap '#' a dry run`: the
comment's words reach the command as arguments. An apostrophe in the
comment opens a quote that swallows the lines after it, and `(...)` is a
glob that fails the whole line. A comment on a line of its own is no safer:
zsh runs it as a command named `#`.

So what a command does is said in the prose around the block, never in it.
Every tracked Markdown file is checked. A shell block is a fenced block
marked bash, sh, zsh, shell or console. In a console block that shows
`$ ` prompts, only the prompted lines (and their continuations) are
commands; the rest is output. A `#` inside quotes, inside a word (`$#`,
`${#x}`, a URL's `#anchor`) or in a heredoc body is not a comment. A block
that opens with `#!` is a script to save and run, never to paste, so it
may carry comments on lines of their own.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHELL_LANGS = {"bash", "sh", "zsh", "shell", "console"}

_OPEN = re.compile(r"^(?P<indent>\s*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<lang>[\w+-]*)")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z_][\w]*)\2")
# A `#` starts a word after the line's start, a blank or a shell metacharacter.
_WORD_BREAK = set(" \t;|&()<>")


def tracked_markdown() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--", "*.md"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [ROOT / name for name in out.split("\0") if name]


def shell_blocks(text: str) -> list[tuple[int, str, list[str]]]:
    """(line number of the first body line, lang, body lines) for each
    fenced shell block in markdown TEXT."""
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        m = _OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        fence, lang = m.group("fence"), m.group("lang").lower()
        close = re.compile(rf"^\s*{re.escape(fence[0])}{{{len(fence)},}}\s*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            j += 1
        if lang in SHELL_LANGS:
            indent = len(m.group("indent"))
            # The fence's own indent is the list item's, not the code's.
            body = [line[indent:] if not line[:indent].strip() else line.lstrip()
                    for line in lines[i + 1:j]]
            blocks.append((i + 2, lang, body))
        i = j + 1
    return blocks


def _scan(line: str, quote: str | None) -> tuple[bool, str | None, bool]:
    """Walk one LINE from quote state QUOTE. Returns (a comment word was
    found outside quotes, the quote state at the line's end, the line ends
    in a backslash continuation)."""
    prev = " "
    k = 0
    while k < len(line):
        c = line[k]
        if quote == "'":
            if c == "'":
                quote = None
        elif quote == '"':
            if c == "\\":
                k += 1
            elif c == '"':
                quote = None
        elif c == "\\":
            if k == len(line) - 1:
                return False, None, True
            k += 1
            c = "x"
        elif c in "'\"":
            quote = c
        elif c == "#" and prev in _WORD_BREAK:
            return True, quote, False
        prev = c
        k += 1
    return False, quote, False


def commented_lines(lang: str, body: list[str]) -> list[tuple[int, str]]:
    """(index, line) for each command line in a shell block's BODY that
    carries a comment."""
    script = bool(body) and body[0].startswith("#!")
    prompted = lang == "console" and any(line.startswith("$ ") or line == "$"
                                         for line in body)
    found = []
    quote: str | None = None
    continued = False
    heredoc: tuple[str, bool] | None = None
    for n, line in enumerate(body):
        if heredoc:
            word, dash = heredoc
            if (line.lstrip("\t") if dash else line) == word:
                heredoc = None
            continue
        if script and (n == 0 or line.lstrip().startswith("#")):
            continue
        command = line
        if prompted and not (continued or quote):
            if not (line.startswith("$ ") or line == "$"):
                continue
            command = line[2:]
        hit, quote, continued = _scan(command, quote)
        if hit:
            found.append((n, line))
        m = _HEREDOC.search(command) if not quote else None
        if m:
            heredoc = (m.group(3), m.group(1) == "-")
    return found


def doc_offences(text: str) -> list[str]:
    """`<line number>: <line>` for each commented command line in TEXT."""
    return [f"{start + n}: {line}" for start, lang, body in shell_blocks(text)
            for n, line in commented_lines(lang, body)]


@pytest.mark.parametrize("path", tracked_markdown(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_shell_block_line_carries_a_comment(path):
    assert doc_offences(path.read_text(encoding="utf-8")) == []


def test_the_docs_hold_shell_blocks_to_check():
    # The check above means nothing if the block finder finds no blocks.
    count = sum(len(shell_blocks(p.read_text(encoding="utf-8"))) for p in tracked_markdown())
    assert count > 20, count


# --- the check bites: planted lines --------------------------------------------------


def _block(lang: str, *lines: str) -> str:
    return "Some prose.\n\n```" + lang + "\n" + "\n".join(lines) + "\n```\n\nMore prose.\n"


@pytest.mark.parametrize("lang", sorted(SHELL_LANGS))
def test_a_planted_trailing_comment_is_caught(lang):
    text = _block(lang, "session list", "session reap   # a dry run", "session --help")
    assert doc_offences(text) == ["5: session reap   # a dry run"]


@pytest.mark.parametrize("line", [
    "# zsh, in ~/.zshrc after compinit:",
    "make;# then test",
    "session park --promise 'one' # it's open",
    "  # indented, on its own",
])
def test_comment_shapes_are_caught(line):
    assert doc_offences(_block("console", line)) == [f"4: {line}"]


def test_a_prompted_console_block_checks_only_the_prompted_lines():
    text = _block("console",
                  "$ session operator   # nothing configured",
                  '{"note": "# in output is output"}',
                  "#3 also output")
    assert doc_offences(text) == ["4: $ session operator   # nothing configured"]


def test_a_continued_line_is_checked():
    text = _block("console", "$ session park --no-promises \\", "  --why x   # the reason")
    assert doc_offences(text) == ["5:   --why x   # the reason"]


def test_an_indented_fence_in_a_list_item_is_checked():
    text = "- Install:\n\n  ```console\n  uv tool install x   # on the PATH\n  ```\n"
    assert doc_offences(text) == ["4: uv tool install x   # on the PATH"]


@pytest.mark.parametrize("line", [
    'echo "a # inside double quotes"',
    "echo 'a # inside single quotes'",
    "echo $# ${#name} ${name#prefix}",
    "open https://example.com/page#anchor",
    "printf 'x\\n' | grep -c x#y",
    "echo a\\ \\#b",
    "grep -c x <<< 'one # two'",
])
def test_a_hash_that_is_not_a_comment_passes(line):
    assert doc_offences(_block("bash", line)) == []


def test_a_quote_spanning_lines_hides_its_hash():
    text = _block("bash", 'session park --why "first line', '  # still inside the quote"')
    assert doc_offences(text) == []


def test_a_heredoc_body_is_data():
    text = _block("bash", "cat > note.md <<'EOF'", "# a heading in the file", "EOF",
                  "echo done   # but this is a comment")
    assert doc_offences(text) == ["7: echo done   # but this is a comment"]


def test_a_script_block_may_comment_on_its_own_lines_only():
    text = _block("bash", "#!/bin/sh", "# what it does", "echo hi", "echo bye   # trailing")
    assert doc_offences(text) == ["7: echo bye   # trailing"]


@pytest.mark.parametrize("lang", ["python", "text", "json", "yaml", ""])
def test_a_block_that_is_not_shell_is_not_checked(lang):
    assert doc_offences(_block(lang, "x = 1   # a comment in its own language")) == []
