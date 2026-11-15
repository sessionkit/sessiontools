"""The user's own CLAUDE.md, carried to a seat whose launch leaves it out.

Mike's ruling (ST-302): every seat reads the user-level memory file. Claude
Code loads it only through its `user` setting source, and that one switch
loads the user `settings.json` too (hooks, plugins, permissions, env, the
agents registry). A launch that keeps the user settings out with
`--setting-sources ''` or `project,local` therefore loses the memory file as
well. So when a launch's own sources leave `user` out, `session start` reads
the file itself and hands it to the seat in the system-prompt file it already
writes. The caller's `--setting-sources` is never touched, and this module
opens exactly one path, the memory file: never a settings file.

THE ROW. What the vendor calls the file and which of its flags load it is
data, the vendor row's `user_memory` field (`vendor.VENDORS`). A row
without that field carries nothing: only claude has one.

WHEN IT IS CARRIED, decided here and nowhere else (`user_memory_block`):

    no `user_memory` field in the row          no: not this vendor's
    an off flag (--bare, --safe-mode, ...)      no: no customisations asked
    the vendor loads it itself                 no: never twice
      (no sources flag at all, or the LAST
       one's comma list names `user`)
    the file missing, unreadable, not UTF-8,   no: a quiet no-op, never a
      or empty                                 launch failure
    otherwise                                  yes

THE PATH follows the vendor's own rule: `$CLAUDE_CONFIG_DIR/CLAUDE.md` when
that variable is set, else `$HOME/.claude/CLAUDE.md`, `~` against the HOME of
the environment the VENDOR will get (`vendor_environ`: the launcher's own,
with the agent file's `environment: variables:` laid over it). A value only
an `environment: files:` or `secrets:` entry sets is not known before the
launch, so that case reads the launcher's own value.

THE ONE DOOR, SHARED (design 3.5). The block rides the system-prompt FILE
door, which the vendor keeps only once. When nothing else on a launch
computes that door and the agent file or the CLI opens it, the memory
YIELDS (`yield_line`): it is not carried, so the launch runs as it did
before, and the launcher says so in one line. An operator's INLINE system
prompt is another door: the memory rides beside it and never yields to it.

`@` IMPORTS. The vendor expands `@path` imports only on its own `user`
route, the one ruled out here. The block NAMES the imports it finds (an `@`
starting a word, outside a fenced or inline code span) and reads none of
them.

Standard library only, plus the sibling modules that hold the shared rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from sessiontools import settings
from sessiontools.argv_flags import last_value
from sessiontools.start.context import CONTEXT_MARKER
from sessiontools.start.vendor import VENDORS, door_is_opened_by

#: The vendor row's field (`vendor.VENDORS`). Neither `_door` nor `_flag`.
ROW_FIELD = "user_memory"

#: What the block's `<!-- source: ... -->` marker names.
SOURCE_LABEL = "user memory: %s"

#: The header, after the vendor's own label for this file, so a seat reads
#: the same words whichever route delivered it.
HEADER = ("Contents of {path} (user's private global instructions for all "
          "projects; carried by session start because this launch's "
          "{flag} leaves out `{source}`):")

#: The one line a launch says when the memory yields the door (design 3.5).
YIELD_LINE = ("user memory not carried: your own {door} owns the system-prompt "
              "door (put {path}'s text in it if you want it)")

#: The one line that follows the header when the file holds `@` imports.
IMPORTS_LINE = ("Its `@` imports are not expanded here ({names}): read those "
                "files if you need them.")

# An `@` import as the vendor spells one: `@` starting a word, then a path
# (a backslash-escaped space stays in it).
IMPORT = re.compile(r"(?:^|(?<=\s))@((?:[^\s\\]|\\ )+)")
# A fence opens with three or more backticks or tildes, indented up to 3.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# An inline code span: a run of backticks, then the same run closing it.
CODE_SPAN = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)", re.DOTALL)


class UserMemory(NamedTuple):
    """The memory file to carry: where it is, its text verbatim, the `@`
    imports it names (in order, each once, without the `@`), and the
    sources flag and source whose absence is why it is carried."""

    path: Path
    text: str
    imports: tuple[str, ...]
    sources_flag: str = "--setting-sources"
    user_source: str = "user"


def vendor_environ(environ: Mapping[str, str],
                   variables: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the vendor will get, as far as it is known before the
    launch: ENVIRON (the launcher's) with the agent file's `environment:
    variables:` laid over it. A declared CLAUDE_CONFIG_DIR moves the vendor's
    file, so it moves ours."""
    env = dict(environ)
    env.update({str(name): str(value) for name, value in (variables or {}).items()})
    return env


def vendor_loads_user_memory(table: Mapping, argv: list[str]) -> bool:
    """Does the vendor load the user memory file itself, given the ARGV it
    will see (front matter, then CLI passthrough)?

    The LAST sources flag decides (`argv_flags.last_value`: the vendor keeps
    the last). No sources flag at all: True, the vendor loads every source.
    A value whose comma-split, stripped names include the user source: True.
    Anything else, `''` and `project,local` included: False. A row with no
    `user_memory` field: True, since there is nothing for the launcher to
    add."""
    spec = table.get(ROW_FIELD)
    if not spec:
        return True
    value = last_value(argv, spec["sources_flag"])
    if value is None:
        return True
    return spec["user_source"] in {name.strip() for name in value.split(",")}


def user_memory_path(table: Mapping, env: Mapping[str, str]) -> Path | None:
    """Where the vendor reads the user memory file from, for a vendor
    getting ENV: `$CONFIG_DIR_ENV/<file>` when that is set and not empty,
    else `$HOME/<config_dir_default>/<file>`, `~` against ENV's HOME. None
    for a row with no `user_memory` field."""
    spec = table.get(ROW_FIELD)
    if not spec:
        return None
    config_dir = env.get(spec["config_dir_env"])
    base = (Path(settings._expand_tilde(config_dir, env)) if config_dir
            else settings.home(env) / spec["config_dir_default"])
    return base / spec["file"]


def _off(spec: Mapping, argv: list[str]) -> bool:
    return any(door_is_opened_by(flag, argv) for flag in spec.get("off_flags", ()))


def _read(path: Path) -> str | None:
    """PATH's text, or None: missing, unreadable, a directory, not UTF-8, or
    nothing but whitespace."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text if text.strip() else None


def find_imports(text: str) -> tuple[str, ...]:
    """The `@` imports TEXT names, in order, each once, without the `@`:
    outside fenced code blocks and inline code spans only. Loose by design:
    it only names what it finds and reads none of them."""
    prose, fence = [], None
    for line in text.splitlines():
        opened = FENCE.match(line)
        if fence is None and opened:
            fence = opened.group(1)
            continue
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            continue
        prose.append(line)
    found: list[str] = []
    for name in IMPORT.findall(CODE_SPAN.sub(" ", "\n".join(prose))):
        if name not in found:
            found.append(name)
    return tuple(found)


def user_memory_block(vendor: str, argv: list[str],
                      env: Mapping[str, str]) -> UserMemory | None:
    """The user memory to carry for a launch of VENDOR whose vendor sees
    ARGV and ENV, or None (the module doc's table): no row field, an off
    flag, the vendor loads it itself, or no readable, non-empty UTF-8 file.
    Never raises for a file it cannot use: R4's quiet no-op."""
    table = VENDORS.get(vendor, {})
    spec = table.get(ROW_FIELD)
    if not spec or _off(spec, argv) or vendor_loads_user_memory(table, argv):
        return None
    path = user_memory_path(table, env)
    text = _read(path) if path is not None else None
    if text is None:
        return None
    return UserMemory(path=path, text=text, imports=find_imports(text),
                      sources_flag=spec["sources_flag"], user_source=spec["user_source"])


def yield_line(vendor: str, memory: UserMemory, declared_argv: list[str],
               cli_argv: list[str]) -> str | None:
    """The line to say when MEMORY yields the system-prompt FILE door to the
    operator (design 3.5), or None when it does not yield: neither the agent
    file's rendered section (DECLARED_ARGV) nor the CLI passthrough opens
    that door. Only the FILE door counts: an inline system prompt is a door
    of its own, and the memory rides beside it. The caller asks only when
    nothing else on the launch computes the door; when something does, the
    duplicate-door guard refuses exactly as it did before."""
    door = VENDORS.get(vendor, {}).get("system_prompt_file_door")
    if door is None or not any(door_is_opened_by(door, args)
                               for args in (declared_argv, cli_argv)):
        return None
    return YIELD_LINE.format(door=door, path=memory.path)


def render_user_memory(memory: UserMemory) -> str:
    """The block, in the shape every injected block has: the source marker,
    the header naming the file and why it is here, the imports line when
    the file names any, a blank line, then the file verbatim (its outer
    blank lines trimmed, as `context.render_context` trims a block)."""
    lines = [CONTEXT_MARKER % (SOURCE_LABEL % memory.path),
             HEADER.format(path=memory.path, flag=memory.sources_flag,
                           source=memory.user_source)]
    if memory.imports:
        lines.append(IMPORTS_LINE.format(
            names=", ".join("@" + name for name in memory.imports)))
    return "\n".join(lines) + "\n\n" + memory.text.strip("\n") + "\n"


__all__ = [
    "HEADER",
    "IMPORTS_LINE",
    "ROW_FIELD",
    "SOURCE_LABEL",
    "YIELD_LINE",
    "UserMemory",
    "find_imports",
    "render_user_memory",
    "user_memory_block",
    "user_memory_path",
    "vendor_environ",
    "vendor_loads_user_memory",
    "yield_line",
]
