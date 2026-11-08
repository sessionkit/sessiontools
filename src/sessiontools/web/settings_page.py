"""The Settings page's drawing: one tool's settings as escaped HTML, laid
out by the tool's JSON Schema. Read-only.

PROPOSED (kit-web-shell, the built-in Settings page). `draw_tool` takes
what the cascade already computed for a tool (`settings.merge`: the
merged values, the file each leaf came from, the `_locked` map) and,
when the tool has one, its schema. It returns one `<section>`. It reads
no file, imports nothing outside the standard library and this package,
and changes nothing; the server hands it the data.

HOW A KEY IS DRAWN, by its schema:

- `boolean`: On or Off.
- `enum`, or an `anyOf` / `oneOf` whose branches carry `const`: the title
  of the branch the value matches (JSON equality, so `true` is not `1`).
  A value no `const` matches reads as "own value" when a branch without a
  `const` exists, and is shown as JSON when none does.
- `string`, `number`, `integer`: the value.
- `object` with `properties`, or with a schema as `additionalProperties`
  (a map): a group with its own heading. Named properties come first,
  then the map's entries in the order the values hold them.
- Anything else (an array, or a schema it cannot place): the value as
  JSON.

A key whose name starts with `_` or `$` is a note, never drawn. A key's
label is the schema's `title`, else its name; its hint is `x-hint`, else
the first sentence of `description`. An unset key reads "unset", with the
schema's `default` beside it when there is one. `x-widget` is ignored:
the key is drawn by its type.

Beside each value: the file that set it and, when a `_locked` path covers
it, "locked by <file>". The lock is looked up with the cascade's own
`settings._under_lock`, so an exact, ancestor or descendant lock and a map
key holding a dot all match as the readers match them.

A set key the schema does not name is listed under "Other keys", so
nothing that is set is hidden. A tool with no schema, or one whose schema
is not a JSON object, is drawn as a flat table of its keys.

All text drawn from a tool's schema or values is escaped first: other
packages own those files, and nothing in them may run on the page.

Stdlib only.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from sessiontools import settings

#: Shown for a key with no value in any layer.
UNSET = "unset"
#: The deepest heading a nested group gets.
MAX_HEADING = 6
#: A hint ends at the first `.`, `!` or `?` that ends the text or is
#: followed by space and a capital or a digit, so "e.g. this" is not an end.
_SENTENCE = re.compile(r"^(.+?[.!?])(?:\s+(?=[A-Z0-9])|$)", re.DOTALL)


# --- the schema ---------------------------------------------------------------

def is_note(key: Any) -> bool:
    """True for a key that is a note, never a setting: `_...` or `$...`."""
    return not isinstance(key, str) or key[:1] in ("_", "$")


def public(data: Mapping[str, Any]) -> dict[str, Any]:
    """DATA without its note keys (`is_note`), top level only."""
    return {k: v for k, v in data.items() if not is_note(k)}


def prop(schema: Any, keys: list[str] | tuple[str, ...]) -> dict[str, Any] | None:
    """Walk SCHEMA down KEYS and return the sub-schema found there. Each
    step takes the named property first; failing that, a map's schema for
    its entries (an `additionalProperties` that is itself an object). None
    if a step finds neither or meets something that is not an object."""
    s = schema
    for k in keys:
        if not isinstance(s, dict):
            return None
        props = s.get("properties")
        named = props.get(k) if isinstance(props, dict) else None
        extra = s.get("additionalProperties")
        s = named if named is not None else (extra if isinstance(extra, dict) else None)
    return s if isinstance(s, dict) else None


def _types(s: Mapping[str, Any]) -> set[str]:
    t = s.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _choices(s: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The branches a picker would offer, or None when S is no picker."""
    if isinstance(s.get("enum"), list):
        return [{"const": e} for e in s["enum"]]
    for word in ("anyOf", "oneOf"):
        branches = s.get(word)
        if isinstance(branches, list):
            branches = [b for b in branches if isinstance(b, dict)]
            if any("const" in b for b in branches):
                return branches
    return None


def _is_group(s: Mapping[str, Any]) -> bool:
    types = _types(s)
    if types and "object" not in types:
        return False
    return isinstance(s.get("properties"), dict) or isinstance(s.get("additionalProperties"), dict)


def members(s: Mapping[str, Any], value: Any) -> list[tuple[str, dict[str, Any]]]:
    """(key, schema) for each member of the object schema S to draw: its
    named properties, then each other key VALUE holds when S has a map
    schema. Note keys are left out."""
    out: list[tuple[str, dict[str, Any]]] = []
    props = s.get("properties") if isinstance(s.get("properties"), dict) else {}
    for k, sub in props.items():
        if not is_note(k) and isinstance(sub, dict):
            out.append((k, sub))
    extra = s.get("additionalProperties")
    if isinstance(extra, dict) and isinstance(value, dict):
        out.extend((k, extra) for k in value if k not in props and not is_note(k))
    return out


def label_of(s: Mapping[str, Any], key: str) -> str:
    title = s.get("title")
    return title if isinstance(title, str) and title.strip() else key


def hint_of(s: Mapping[str, Any]) -> str:
    """`x-hint`, else the first sentence of `description`, else ''."""
    hint = s.get("x-hint")
    if isinstance(hint, str) and hint.strip():
        return hint.strip()
    desc = s.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return ""
    m = _SENTENCE.match(desc.strip())
    return m.group(1) if m else desc.strip()


# --- the values -----------------------------------------------------------------

def _same(a: Any, b: Any) -> bool:
    """Equal as JSON values: a boolean never equals a number, and two maps
    holding the same pairs are equal in any order."""
    return (type(a) is bool) == (type(b) is bool) and \
        json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _text(v: Any) -> str:
    """V as the page shows it: a non-empty string as itself, else JSON."""
    if isinstance(v, str) and v:
        return v
    return json.dumps(v, ensure_ascii=False)


def _choice_text(branches: list[dict[str, Any]], v: Any) -> str:
    for b in branches:
        if "const" in b and _same(b["const"], v):
            title = b.get("title")
            return title if isinstance(title, str) and title.strip() else _text(b["const"])
    if any("const" not in b for b in branches):
        return f"own value: {_text(v)}"
    return _text(v)


def _leaves(data: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    """Each leaf of DATA with its path, notes left out. A map is walked; an
    empty map is no leaf, as in the cascade."""
    if not isinstance(data, dict):
        yield prefix, data
        return
    for k, v in data.items():
        if not is_note(k):
            yield from _leaves(v, prefix + (k,))


# --- the HTML -------------------------------------------------------------------

def _e(text: Any) -> str:
    return html.escape(str(text), quote=True)


class _Drawing:
    """One tool being drawn: its data, the paths drawn so far, the HTML."""

    def __init__(self, values: Mapping[str, Any], origins: Mapping[str, str],
                 locked: Mapping[str, str]) -> None:
        self.values = values
        self.origins = origins
        self.locked = locked
        self.drawn: set[tuple[str, ...]] = set()

    def where(self, path: tuple[str, ...]) -> str:
        """The `from` cell: the origin file, and the lock when one covers PATH."""
        joined = settings._joined(path)
        parts = []
        origin = self.origins.get(joined)
        if origin:
            parts.append(f"<code>{_e(origin)}</code>")
        lock = settings._under_lock(joined, self.locked)
        if lock:
            parts.append(f"locked by <code>{_e(lock)}</code>")
        return "<br>".join(parts)

    def row(self, path: tuple[str, ...], label: str, hint: str, shown: str) -> str:
        self.drawn.add(path)
        cell = f"{_e(label)}<br><code>{_e(settings._shown(path))}</code>"
        if hint:
            cell += f"<br><small>{_e(hint)}</small>"
        return (f"<tr><td>{cell}</td><td>{shown}</td><td>{self.where(path)}</td></tr>")

    def control(self, path: tuple[str, ...], s: dict[str, Any], v: Any, is_set: bool,
                depth: int) -> tuple[str, str]:
        """(a table row, or '') and (a group's HTML, or '') for one key."""
        label, hint = label_of(s, path[-1]), hint_of(s)
        if not is_set:
            if _is_group(s) and not _choices(s):
                return "", self.group(path, s, {}, depth)
            shown = _e(UNSET)
            if "default" in s:
                shown += f" (schema default: <code>{_e(_text(s['default']))}</code>)"
            return self.row(path, label, hint, shown), ""
        branches = _choices(s)
        types = _types(s)
        if branches is not None:
            shown = _e(_choice_text(branches, v))
        elif "boolean" in types and isinstance(v, bool):
            shown = "On" if v else "Off"
        elif _is_group(s) and isinstance(v, dict):
            return "", self.group(path, s, v, depth)
        elif types & {"string", "number", "integer"} and isinstance(v, (str, int, float)) \
                and not isinstance(v, bool):
            shown = _e(_text(v))
        else:
            shown = f"<code>{_e(json.dumps(v, ensure_ascii=False))}</code>"
        return self.row(path, label, hint, shown), ""

    def body(self, path: tuple[str, ...], s: Mapping[str, Any], value: Any, depth: int) -> str:
        """The members of the object schema S at PATH: rows in tables, each
        group in its own section, in schema order."""
        out, rows = [], []

        def flush() -> None:
            if rows:
                out.append(_table(rows))
                rows.clear()

        for k, sub in members(s, value):
            present = isinstance(value, dict) and k in value
            row, group = self.control(path + (k,), sub, value.get(k) if present else None,
                                      present, depth + 1)
            if row:
                rows.append(row)
            if group:
                flush()
                out.append(group)
        flush()
        return "\n".join(out)

    def group(self, path: tuple[str, ...], s: dict[str, Any], v: dict[str, Any],
              depth: int) -> str:
        # The group's own path is NOT marked drawn: only its rows are, so a
        # set key inside it that the schema does not name still reaches
        # "Other keys" (nothing set is hidden).
        level = min(2 + depth, MAX_HEADING)
        head = f"<h{level}>{_e(label_of(s, path[-1]))}</h{level}>"
        hint = hint_of(s)
        where = self.where(path)
        notes = "".join(f"<p>{x}</p>" for x in (_e(hint) if hint else "", where) if x)
        return f"<section>\n{head}\n{notes}\n{self.body(path, s, v, depth)}\n</section>"

    def others(self) -> list[str]:
        """Rows for each set leaf no drawn path covers."""
        out = []
        for path, v in _leaves(self.values):
            if not any(path[:len(p)] == p for p in self.drawn):
                out.append(self.row(path, settings._shown(path), "",
                                    f"<code>{_e(json.dumps(v, ensure_ascii=False))}</code>"))
        return out


def _table(rows: list[str]) -> str:
    return ("<table><tr><th>setting</th><th>value</th><th>from</th></tr>\n"
            + "\n".join(rows) + "\n</table>")


def flat_table(values: Mapping[str, Any], origins: Mapping[str, str],
               locked: Mapping[str, str]) -> str:
    """Every set leaf of VALUES in one table, by path: the page's view of a
    tool with no schema to draw from. '' when nothing is set."""
    d = _Drawing(values, origins, locked)
    rows = d.others()
    return _table(rows) if rows else ""


def draw_tool(tool: str, values: Mapping[str, Any], origins: Mapping[str, str],
              locked: Mapping[str, str], *, schema: Any = None, schema_file: str = "",
              schema_error: str | None = None, note: str = "") -> str:
    """TOOL's section of the Settings page.

    VALUES, ORIGINS and LOCKED are `settings.merge`'s first three results
    (the merged values; the origin file by joined path; the file that
    locks each dotted path). SCHEMA is the tool's parsed schema, or None
    when it has none; SCHEMA_FILE names it; SCHEMA_ERROR says why it could
    not be read. NOTE is a line under the heading. Reads nothing; every
    piece of text in the result is escaped."""
    d = _Drawing(values, origins, locked)
    drawable = isinstance(schema, dict) and schema_error is None
    title = label_of(schema, tool) if drawable else tool
    head = f"<h2>{_e(title)}</h2>"
    if title != tool:
        head = f"<h2>{_e(title)} <code>{_e(tool)}</code></h2>"
    parts = [f'<section id="tool-{_e(tool)}">', head]
    if note:
        parts.append(f"<p>{_e(note)}</p>")
    if schema_error is not None:
        parts.append(f"<p>unreadable schema: {_e(schema_error)}</p>")
    elif schema is not None and not drawable:
        parts.append("<p>unreadable schema: it is not a JSON object</p>")
    if drawable:
        hint = hint_of(schema)
        if hint:
            parts.append(f"<p>{_e(hint)}</p>")
        body = d.body((), schema, values, 0)
        if body:
            parts.append(body)
        rest = d.others()
        if rest:
            parts.append("<h3>Other keys</h3>")
            parts.append(_table(rest))
        if schema_file:
            parts.append(f"<p>Drawn from <code>{_e(schema_file)}</code>.</p>")
    else:
        table = flat_table(values, origins, locked)
        parts.append(table or "<p>nothing set</p>")
    parts.append("</section>")
    return "\n".join(parts)
