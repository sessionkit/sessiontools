"""Reading a vendor's flags back off an argv, by the vendor's own rule.

claude parses a repeated scalar flag by keeping the LAST one, in either
spelling: `--flag value` or `--flag=value`. Two readers need that rule:
the settle hook (`compact_point`, reading a recorded launch line) and
`session start` (`start.user_memory`, reading the argv it is about to
render). One definition, here, so the two never disagree.

Standard library only, and nothing imported at all: the settle hook
imports `compact_point`, and so this, on every batch, maybe under a bare
python3 (it must not pull in `sessiontools.start` or PyYAML).
"""

from __future__ import annotations


def last_value(argv: list[str], flag: str) -> str | None:
    """The value of the LAST `FLAG value` or `FLAG=value` in ARGV, or None.

    The `=` is part of the prefix, so a longer flag that merely starts with
    FLAG's spelling (`--settings-file` for `--settings`) is not FLAG. A
    trailing FLAG with no value after it sets nothing."""
    found = None
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            found = argv[index + 1]
        elif token.startswith(flag + "="):
            found = token[len(flag) + 1:]
    return found


__all__ = ["last_value"]
