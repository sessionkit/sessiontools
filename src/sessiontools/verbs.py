"""Where each `session` verb's parser comes from: the references the
dispatcher and shell completion both read.

A verb's parser is built by a pure BUILDER: a function that builds and
returns its `argparse.ArgumentParser` and does nothing else (no settings
read, no subprocess, no tmux). The verb's handler parses with it, and so
does anything that needs the verb's surface without running the verb.

`sessiontools.cli.PARSERS` maps each verb to a reference, a
`"module:attr"` string, so naming a parser imports nothing. The attr is a
builder, or a `Group` for a verb whose first word is dispatched by hand.

A leaf module: stdlib only, and nothing from sessiontools, so the
dispatcher and the verbs that hold a `Group` can import it at load.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # argparse is the builders' import, never this module's
    import argparse
    from collections.abc import Callable

    Builder = Callable[[], argparse.ArgumentParser]


@dataclass(frozen=True)
class Group:
    """A verb whose first word is dispatched by hand, before any parser.

    SUBS maps each sub-verb dispatched that way to its builder's reference.
    REST is the reference of the parser for everything else, or None when
    there is none. A SUBS entry replaces any placeholder of the same name in
    the REST parser. The dispatcher reads SUBS to decide, so the sub-verbs
    it accepts and the ones a reader is shown are the same table."""

    subs: Mapping[str, str] = field(default_factory=dict)
    rest: str | None = None


def resolve(ref: str) -> Builder | Group:
    """The builder or `Group` that REF (`"package.module:attr"`) names.
    Imports the module, so call it only where that import is wanted."""
    module, sep, attr = ref.partition(":")
    if not sep or not module or not attr:
        raise ValueError(f"not a 'module:attr' reference: {ref!r}")
    return getattr(importlib.import_module(module), attr)


#: The attribute a builder sets on a positional's action to say which words
#: complete it, when the verb checks them itself rather than through
#: argparse's `choices` (whose refusal would replace the verb's own message).
#: Read by `sessiontools.completion`; the parser never looks at it.
COMPLETION_CHOICES = "completion_choices"


def offer(action: argparse.Action,
          table: Sequence[str] | Sequence[tuple[str, ...]]) -> argparse.Action:
    """Mark ACTION as completed by TABLE, the very table the verb validates
    against: its words, or (word, ...) rows whose first item is the word.
    Returns ACTION. Nothing changes in how the parser parses."""
    setattr(action, COMPLETION_CHOICES, table)
    return action
