"""The `claude-code` settings tool, rendered for Claude's `--settings` door.

`session start` may define default values (even just model and
effort) and sets them ahead of Claude's own settings. The launcher resolves the tool `claude-code` through the
settings cascade (template, host, agent, seat, session) and hands the
result to claude as ONE `--settings` file. Claude ranks that file above
`~/.claude/settings.json`, which a typed `/model` or `/effort` rewrites for
every later seat, and below a CLI `--model`/`--effort` (measured on Claude
Code 2.1.299).

v1 renders only RENDERED, under Claude's own key names: the file IS
Claude's settings, and a later full build renders the same file. Every
other key is said, once, naming the file that set it, and dropped.
Permissions, hooks, env and profiles are not rendered yet.

`statusLine` is the one object in RENDERED. The cascade merges it
key by key, like every map, so it arrives as `statusLine.*` leaves; render
reads it back as ONE value from the resolved map, checks its shape, and
keeps only the sub-keys Claude's status line names (STATUS_LINE_KEYS). A
bad shape is one note naming its file, never a refusal. The status line
lives in the kit's `claude-code` layer, so a person's own `~/.claude` is
never touched.

A launch whose CLI passthrough carries its own `--settings` (a crew's
spawn) gets that file FOLDED in (`fold`): it is the TOP layer, one Source
above the session layer, merged by the cascade's own rule BEFORE render.
So a host `_locked` path still holds against it, and its keys win over
every other layer. Render's v1 allowlist filters the cascade's leaves only:
a leaf whose origin is the folded file renders as given, as it reached
claude before. The launch then carries ONE `--settings`, the computed one.
A `--settings` in the agent file's `claude:` section is not folded: that
launch gets no door, as before (`skipped`).

THE LAUNCHER'S ONE DEFAULT: `tui`. Claude Code offers its fullscreen
renderer to every session while no setting names `tui`, so a launched
session opens on that offer. When nothing names `tui` (no cascade layer,
no folded CLI `--settings`, and none of Claude's own settings files a
launched claude reads: `claude_sets_tui`), the door carries
`"tui": "default"`, the renderer a launched session already runs, and
the offer is not made. The door outranks Claude's own user and project
files, so it never sets `tui` over one that names it: a user who runs
`/tui fullscreen` writes their own file, and from then on the launcher
steps aside. That is also why the door exists now with no `claude-code`
file anywhere (`with_tui_default`). Claude's files are only read, never
written.

Nothing here writes a file: `context.settings_argv` plans the 0600 file and
`run.execute` writes it. `~/.claude/settings.json` is never written.
Standard library only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple

from sessiontools import settings as _settings

#: The settings tool this module renders.
TOOL = "claude-code"
#: The keys v1 renders, Claude's own names, in file order.
RENDERED = ("model", "effortLevel", "statusLine", "tui")
#: The one RENDERED key that is an object, rendered whole.
STATUS_LINE = "statusLine"
#: statusLine's sub-keys beyond `type` and `command`, each with the check
#: its value must pass. `bool` is not an int here: `True` is no padding.
STATUS_LINE_KEYS = {
    "padding": lambda v: type(v) is int and v >= 0,
    "refreshInterval": lambda v: type(v) is int and v >= 1,
    "hideVimModeIndicator": lambda v: type(v) is bool,
}
#: Claude's renderer setting, and the value the launcher sets when nothing
#: else does: the classic renderer, the one a launched session runs today.
TUI = "tui"
TUI_DEFAULT = "default"
#: The "layer" `origins` names for that value: no file set it.
LAUNCHER_DEFAULT = "launcher default"
#: The layer of a CLI `--settings` folded on top of the cascade (`fold`).
PASSTHROUGH = "passthrough"
#: The origin an inline-JSON `--settings` is named by: it has no file.
INLINE = "--settings (inline JSON)"


class Rendered(NamedTuple):
    """What the `--settings` file holds, where each value came from, and
    what was said. `origins` is key -> (layer, file); `files` is every
    file the tool was resolved from, cascade order (the skip line names
    them). `folded` is the CLI `--settings` value folded in (`fold`), whose
    tokens the launch drops for the computed door; None when none was."""

    data: dict
    origins: dict
    notes: list
    files: tuple
    folded: str | None = None


def not_rendered(key: str, origin: str) -> str:
    return f"{TOOL}: {key} (from {origin}) not rendered in v1"


def _layer_of(res: _settings.Resolved, origin: str) -> str:
    """The layer of the LOWEST source that is ORIGIN: the one `merge` let win."""
    return next((s.layer for s in reversed(res.sources) if str(s.path) == origin), "?")


def bad_status_line(origin: str) -> str:
    return (f'{TOOL}: {STATUS_LINE} (from {origin}) not rendered: it needs '
            '{"type": "command", "command": "<a non-empty string>"}')


def _sub_origin(group: Mapping[str, str], sub: str) -> str:
    """The file behind statusLine's sub-key SUB: its leaf, or the first leaf
    under it when a layer nested a map there."""
    leaf = f"{STATUS_LINE}.{sub}"
    return group.get(leaf) or next(o for k, o in group.items() if k.startswith(leaf + "."))


def _status_line(res: _settings.Resolved, group: Mapping[str, str],
                 exempt: Callable[[str], bool] = lambda _origin: False,
                 ) -> tuple[dict | None, str | None, list[str]]:
    """(object, origin, notes) for the `statusLine` leaves in GROUP (leaf ->
    file). The object is read whole from `res.values`; a null sub-key is
    unset. It needs `type: command` and a non-empty `command`, else nothing
    of it is rendered and one note names the file of `command` (or of its
    first leaf). Each other sub-key must be in STATUS_LINE_KEYS and pass its
    check, else it alone is dropped with a note. The origin is the file of
    `command`: the value that picks the script. A leaf whose origin is
    EXEMPT (the folded CLI `--settings`) is never checked: a sub-key from
    it is kept as given, and a status line all of whose leaves are from it
    is rendered as given."""
    value = res.values.get(STATUS_LINE)
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if v is not None}
        if not value:
            return None, None, []
    elif value is None:
        return None, None, []
    origin = group.get(f"{STATUS_LINE}.command") or next(iter(group.values()))
    if all(exempt(o) for o in group.values()):
        return value, origin, []
    command = value.get("command") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("type") != "command"
            or not isinstance(command, str) or not command.strip()):
        return None, None, [bad_status_line(origin)]
    out: dict = {"type": "command", "command": command}
    notes: list[str] = []
    for sub, item in value.items():
        if sub in ("type", "command"):
            continue
        check = STATUS_LINE_KEYS.get(sub)
        where = _sub_origin(group, sub)
        if exempt(where) or (check is not None and check(item)):
            out[sub] = item
        else:
            notes.append(not_rendered(f"{STATUS_LINE}.{sub}", where))
    return out, origin, notes


def _nest(path: tuple, value: object, into: dict) -> None:
    """VALUE at PATH (one key per segment) inside INTO, making maps on the way."""
    node = into
    for part in path[:-1]:
        node = node.setdefault(part, {})
    node[path[-1]] = value


def _folded_origins(res: _settings.Resolved) -> set[str]:
    """The origin of every PASSTHROUGH source in RES: the leaves render exempts."""
    return {str(s.path) for s in res.sources if s.layer == PASSTHROUGH}


def render(res: _settings.Resolved, folded: str | None = None) -> Rendered:
    """RES (the resolved `claude-code` tool) as the v1 `--settings` data:
    `model` and `effortLevel` when each is a non-empty string, and
    `statusLine` as one checked object (`_status_line`); a `None` (JSON
    null) leaf is dropped without a word, as unset. Every other leaf, or a
    RENDERED key that is not a string, is one note naming its origin file.
    A leaf whose origin is a PASSTHROUGH source (`fold`) is exempt by LEAF:
    it renders as given, under its own path, and is never said. FOLDED is
    the CLI value that source came from, carried on for the launch. Empty
    data means no door."""
    data: dict = {}
    origins: dict = {}
    notes: list[str] = []
    passthrough = _folded_origins(res)

    def exempt(origin: str) -> bool:
        return origin in passthrough
    # A key written literally dotted (`{"statusLine.command": ...}` in a JSON
    # file) is a top-level key of its own, not part of the object: it keeps
    # its own "not rendered" note below, as before `statusLine` was rendered.
    literal = [k for k in res.values if k.startswith(STATUS_LINE + ".")]
    group = {key: origin for key, origin in res.origins.items()
             if (key == STATUS_LINE or key.startswith(STATUS_LINE + "."))
             and not any(key == t or key.startswith(t + ".") for t in literal)}
    for key, origin in res.origins.items():
        if key in group:
            if key == next(iter(group)):            # said once, where it first appears
                value, where, said = _status_line(res, group, exempt)
                if value is not None:
                    data[STATUS_LINE] = value
                    origins[STATUS_LINE] = (_layer_of(res, where), where)
                notes.extend(said)
            continue
        value = res.leaves.get(key)
        if value is None:
            continue
        if exempt(origin):
            path = res.paths.get(key, (key,))
            _nest(path, value, data)
            origins.setdefault(path[0], (PASSTHROUGH, origin))
        elif key in RENDERED and isinstance(value, str) and value:
            data[key] = value
            origins[key] = (_layer_of(res, origin), origin)
        else:
            notes.append(not_rendered(key, origin))
    ordered = {key: data[key] for key in RENDERED if key in data}
    ordered.update((key, item) for key, item in data.items() if key not in ordered)
    files = tuple(str(s.path) for s in res.sources if s.layer != PASSTHROUGH)
    return Rendered(ordered, origins, notes, files, folded)


def passthrough_source(value: str, cwd: str | None = None,
                       env: Mapping[str, str] | None = None) -> _settings.Source:
    """A CLI `--settings` VALUE as the PASSTHROUGH layer: inline JSON when it
    opens with `{`, as claude reads it, else a file path, read where claude
    would read it (`~` is the launch's HOME, a relative path is under CWD,
    the session's working directory). Raises SettingsError naming the value
    for a missing value, an unreadable file, or anything but one JSON
    object: the launch would hand claude a file it cannot fold."""
    if not value.strip():
        raise _settings.SettingsError("--settings has no value to fold")
    if value.lstrip().startswith("{"):
        try:
            data = json.loads(value)
        except json.JSONDecodeError as e:
            raise _settings.SettingsError(f"{INLINE}: not JSON ({e.msg})") from e
        if not isinstance(data, dict):
            raise _settings.SettingsError(f"{INLINE}: not a JSON object")
        return _settings.Source(PASSTHROUGH, Path(INLINE), data)
    path = Path(_settings._expand_tilde(value, env))
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    try:
        data = _settings.read_json_object(path)
    except OSError as e:
        raise _settings.SettingsError(
            f"{path}: the --settings to fold cannot be read ({e.strerror or e})") from e
    return _settings.Source(PASSTHROUGH, path, data)


def fold(res: _settings.Resolved, source: _settings.Source) -> _settings.Resolved:
    """RES with SOURCE (`passthrough_source`) on top of its cascade, merged
    again by the cascade's own rule, BEFORE render: its keys win over every
    layer, and a `_locked` path from any file below still holds against it
    (a lock ignores the key in every layer below the file that locked it).
    The reader's notes and schema problems are RES's own."""
    srcs = [*res.sources, source]
    leaves, origins, locked = _settings._fold(srcs)
    return _settings.Resolved(
        res.tool, _settings._unflatten(leaves),
        {_settings._shown(p): f for p, f in origins.items()}, dict(locked), srcs,
        list(res.problems), {_settings._shown(p): v for p, v in leaves.items()},
        list(res.notes), {_settings._shown(p): p for p in leaves})


def provenance(rendered: Rendered, env: Mapping[str, str] | None = None) -> str:
    """The door's source line: each value's layer and file (`~` for HOME),
    then, for a fold, the CLI `--settings` folded on top (named even when
    none of its keys survived)."""
    parts = [f"{key} from the {layer} (no settings file sets it)" if layer == LAUNCHER_DEFAULT
             else f"{key} from {layer} {_settings._short(origin, env)}"
             for key, (layer, origin) in ((k, rendered.origins[k]) for k in rendered.data)]
    line = f"computed: {TOOL} settings: {', '.join(parts)}"
    if rendered.folded is not None:
        what = (INLINE if rendered.folded.lstrip().startswith("{")
                else f"--settings {_settings._short(rendered.folded, env)}")
        line += f"; folded on top: the CLI {what}"
    return line


def claude_settings_files(work_dir: str | None,
                          env: Mapping[str, str] | None = None) -> list[Path]:
    """Claude Code's own settings files that a claude launched in WORK_DIR
    reads, and that the door outranks: the user's (`$CLAUDE_CONFIG_DIR/
    settings.json`, else `~/.claude/settings.json`, `~` the launch's HOME),
    then the project's and the local one under WORK_DIR."""
    env = os.environ if env is None else env
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    user = (Path(_settings._expand_tilde(config_dir, env)) if config_dir
            else _settings.home(env) / ".claude")
    files = [user / "settings.json"]
    if work_dir:
        files += [Path(work_dir) / ".claude" / "settings.json",
                  Path(work_dir) / ".claude" / "settings.local.json"]
    return files


def claude_sets_tui(work_dir: str | None, env: Mapping[str, str] | None = None) -> str | None:
    """The first of Claude's own settings files (`claude_settings_files`)
    that names `tui`, any value, or None. Read only. A file that is
    missing, unreadable or not one JSON object names nothing."""
    for path in claude_settings_files(work_dir, env):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and TUI in data:
            return str(path)
    return None


def with_tui_default(rendered: Rendered, claude_tui: str | None) -> Rendered:
    """RENDERED with the launcher's `"tui": "default"`, unless a layer or
    the folded CLI `--settings` already put `tui` in it, or CLAUDE_TUI (the
    file `claude_sets_tui` found) names it: the door outranks that file,
    so setting it would undo the user's own choice."""
    if TUI in rendered.data or claude_tui is not None:
        return rendered
    data = {**rendered.data, TUI: TUI_DEFAULT}
    origins = {**rendered.origins, TUI: (LAUNCHER_DEFAULT, "")}
    return rendered._replace(data=data, origins=origins)


def without_own_settings(args: list) -> list:
    """ARGS less every `--settings` and its value, either spelling: the
    tokens a fold replaces with the computed door. A bare flag at the end
    takes nothing after it."""
    out: list = []
    skip = False
    for token in args:
        if skip:
            skip = False
            continue
        if token == "--settings":
            skip = True
            continue
        if isinstance(token, str) and token.startswith("--settings="):
            continue
        out.append(token)
    return out


def own_settings_value(args: list) -> str | None:
    """The value of the first `--settings` in ARGS, either spelling, or
    None. The bare flag at the end of ARGS (no value follows) is `""`."""
    for index, token in enumerate(args):
        if not isinstance(token, str):
            continue
        if token == "--settings":
            nxt = args[index + 1] if index + 1 < len(args) else ""
            return nxt if isinstance(nxt, str) else str(nxt)
        if token.startswith("--settings="):
            return token[len("--settings="):]
    return None


def own_settings_values(args: list) -> list[str]:
    """The value of EVERY `--settings` in ARGS, either spelling, in order:
    the tokens `without_own_settings` drops. A bare flag at the end is `""`.
    More than one is a repeated door the fold refuses: folding the first and
    dropping the rest would lose a file without a word."""
    out: list[str] = []
    skip = False
    for index, token in enumerate(args):
        if skip:
            skip = False
            continue
        if not isinstance(token, str):
            continue
        if token == "--settings":
            nxt = args[index + 1] if index + 1 < len(args) else ""
            out.append(nxt if isinstance(nxt, str) else str(nxt))
            skip = True
        elif token.startswith("--settings="):
            out.append(token[len("--settings="):])
    return out


def skipped(files: tuple, own: str, env: Mapping[str, str] | None = None) -> str:
    """The one stderr line for a launch whose AGENT FILE's `claude:` section
    opens `--settings`: the cascade's files are not applied, the launch's own
    file is, and nothing is folded or refused. (A CLI `--settings` is folded
    instead: `fold`.)"""
    named = ", ".join(_settings._short(f, env) for f in files)
    return (f"{TOOL}: not applied ({named}): this launch opens its own --settings "
            f"{own or '(no value)'}, which claude reads alone; nothing is merged or "
            "refused")


__all__ = ["INLINE", "LAUNCHER_DEFAULT", "PASSTHROUGH", "RENDERED", "STATUS_LINE",
           "STATUS_LINE_KEYS", "TOOL", "TUI", "TUI_DEFAULT", "Rendered", "bad_status_line",
           "claude_sets_tui", "claude_settings_files", "fold", "not_rendered",
           "own_settings_value", "own_settings_values", "passthrough_source", "provenance",
           "render", "skipped", "with_tui_default", "without_own_settings"]
