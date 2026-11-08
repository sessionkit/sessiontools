"""`sessiontools.web.settings_page`: one tool drawn from its schema, as
escaped HTML, read-only. Pure: every test hands the draw function its data
and reads the HTML back through a small parser; no file is read."""

from __future__ import annotations

import builtins
import io
import pathlib
from html.parser import HTMLParser

import pytest

from sessiontools import settings
from sessiontools.web import settings_page as page

ORIGIN = "/home/u/.sessionkit/alpha/settings/alpha.json"
TEMPLATE = "/home/u/.sessionkit/alpha/settings/alpha.template.json"


class Cells(HTMLParser):
    """The page's tables as rows of cell text, and every heading's text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.headings: list[str] = []
        self.paras: list[str] = []
        self._cell: list[str] | None = None
        self._head: list[str] | None = None
        self._para: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self._cell = []
        elif tag in ("h2", "h3", "h4", "h5", "h6"):
            self._head = []
        elif tag == "p":
            self._para = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self.rows[-1].append("".join(self._cell).strip())
            self._cell = None
        elif tag in ("h2", "h3", "h4", "h5", "h6") and self._head is not None:
            self.headings.append("".join(self._head).strip())
            self._head = None
        elif tag == "p" and self._para is not None:
            self.paras.append("".join(self._para).strip())
            self._para = None

    def handle_data(self, data):
        for buf in (self._cell, self._head, self._para):
            if buf is not None:
                buf.append(data)


def parse(html: str) -> Cells:
    c = Cells()
    c.feed(html)
    c.close()
    return c


def by_path(html: str) -> dict[str, tuple[str, str, str]]:
    """Each data row keyed by the key path in its first cell:
    (label and hint cell, value cell, from cell)."""
    out = {}
    for r in parse(html).rows:
        if len(r) == 3 and r[0] != "setting":
            bits = r[0].split(" | ")
            out[bits[1] if len(bits) > 1 else bits[0]] = (r[0], r[1], r[2])
    return out


def draw(schema, values, origins=None, locked=None, **kw) -> str:
    return page.draw_tool("alpha", values, origins or {}, locked or {}, schema=schema, **kw)


def obj(**props) -> dict:
    return {"type": "object", "properties": props}


# --- U1: switches and pickers -------------------------------------------------------

def test_u1_a_boolean_shows_on_or_off():
    s = obj(on={"type": "boolean"}, off={"type": "boolean"})
    rows = by_path(draw(s, {"on": True, "off": False}))
    assert rows["on"][1] == "On" and rows["off"][1] == "Off"


def test_u1_an_enum_shows_its_value():
    rows = by_path(draw(obj(mode={"enum": ["fast", "slow"]}), {"mode": "slow"}))
    assert rows["mode"][1] == "slow"


def test_u1_titled_consts_show_the_chosen_title():
    s = obj(level={"anyOf": [{"const": 1, "title": "Low"}, {"const": 2, "title": "High"}]})
    assert by_path(draw(s, {"level": 2}))["level"][1] == "High"


def test_u1_a_const_matches_by_json_equality_so_true_is_not_one():
    s = obj(x={"oneOf": [{"const": 1, "title": "One"}, {"const": True, "title": "Yes"}]})
    assert by_path(draw(s, {"x": True}))["x"][1] == "Yes"
    assert by_path(draw(s, {"x": 1}))["x"][1] == "One"


def test_u1_a_branch_without_const_reads_as_own_value():
    s = obj(out={"anyOf": [{"const": "auto", "title": "Automatic"},
                           {"type": "string", "title": "Pick one"}]})
    assert by_path(draw(s, {"out": "speaker-b"}))["out"][1] == "own value: speaker-b"


def test_u1_a_value_no_branch_matches_is_shown_raw():
    s = obj(level={"enum": [1, 2]})
    assert by_path(draw(s, {"level": 7}))["level"][1] == "7"


# --- U2: lines, and unset keys -------------------------------------------------------

def test_u2_strings_and_numbers_show_their_value():
    s = obj(name={"type": "string"}, count={"type": "integer"}, ratio={"type": "number"})
    rows = by_path(draw(s, {"name": "kit", "count": 3, "ratio": 0.5}))
    assert (rows["name"][1], rows["count"][1], rows["ratio"][1]) == ("kit", "3", "0.5")


def test_u2_an_empty_string_is_shown_as_json_so_it_is_visible():
    assert by_path(draw(obj(name={"type": "string"}), {"name": ""}))["name"][1] == '""'


def test_u2_an_unset_key_reads_unset_with_the_schema_default():
    s = obj(delay={"type": "number", "default": 2}, file={"type": "string"})
    rows = by_path(draw(s, {}))
    assert rows["delay"][1] == "unset (schema default: 2)"
    assert rows["file"][1] == "unset"


def test_u2_a_value_of_the_wrong_type_is_shown_as_json():
    rows = by_path(draw(obj(on={"type": "boolean"}, n={"type": "integer"}), {"on": "yes", "n": True}))
    assert rows["on"][1] == '"yes"' and rows["n"][1] == "true"


# --- U3: groups and maps -------------------------------------------------------------

def test_u3_an_object_is_a_nested_group_with_its_own_heading():
    s = obj(busy={"type": "object", "title": "Busy sound",
                  "properties": {"file": {"type": "string"},
                                 "inner": obj(depth={"type": "integer"})}})
    html = draw(s, {"busy": {"file": "/s.wav", "inner": {"depth": 4}}})
    c = parse(html)
    assert "Busy sound" in c.headings and "inner" in c.headings
    assert "<h3>Busy sound</h3>" in html and "<h4>inner</h4>" in html
    rows = by_path(html)
    assert rows["busy.file"][1] == "/s.wav" and rows["busy.inner.depth"][1] == "4"


def test_u3_map_entries_come_after_the_named_properties():
    s = obj(sounds={"type": "object", "properties": {"default": {"type": "string"}},
                    "additionalProperties": {"type": "string", "title": "A sound"}})
    html = draw(s, {"sounds": {"zeta": "z.wav", "default": "d.wav", "beta": "b.wav"}})
    order = [p for p in by_path(html)]
    assert order == ["sounds.default", "sounds.zeta", "sounds.beta"]


def test_u3_an_unset_group_still_shows_its_keys_as_unset():
    s = obj(busy=obj(file={"type": "string"}))
    assert by_path(draw(s, {}))["busy.file"][1] == "unset"


def test_u3_prop_walks_named_properties_and_map_schemas():
    s = obj(a=obj(b={"type": "string"}),
            m={"type": "object", "additionalProperties": {"type": "integer"}})
    assert page.prop(s, ["a", "b"]) == {"type": "string"}
    assert page.prop(s, ["m", "anything"]) == {"type": "integer"}
    assert page.prop(s, ["a", "nope"]) is None
    assert page.prop(s, ["a", "b", "deeper"]) is None


# --- U4: notes are never drawn -------------------------------------------------------

def test_u4_note_keys_are_never_drawn_from_schema_or_values():
    s = {"type": "object", "properties": {"_about": {"type": "string"},
                                          "$comment": {"type": "string"},
                                          "on": {"type": "boolean"}},
         "additionalProperties": {"type": "string"}}
    html = draw(s, {"on": True, "_about": "a note", "$x": "y", "_n": {"deep": 1}})
    assert list(by_path(html)) == ["on"]
    assert "a note" not in html and "_about" not in html and "$x" not in html


def test_u4_public_drops_note_keys():
    assert page.public({"_about": 1, "$schema": 2, "on": True}) == {"on": True}


# --- U5: labels and hints ------------------------------------------------------------

def test_u5_the_label_is_the_title_else_the_key():
    rows = by_path(draw(obj(a={"type": "boolean", "title": "Alpha switch"},
                            b={"type": "boolean"}), {"a": True, "b": True}))
    assert rows["a"][0].startswith("Alpha switch") and rows["b"][0].startswith("b")


def test_u5_the_hint_is_x_hint_else_the_first_sentence_of_the_description():
    s = obj(a={"type": "boolean", "x-hint": "Short.", "description": "Long one. More."},
            b={"type": "boolean", "description": "First sentence here. Second one."},
            c={"type": "boolean", "description": "No stop at all"})
    rows = by_path(draw(s, {}))
    assert rows["a"][0].endswith("Short.")
    assert rows["b"][0].endswith("First sentence here.") and "Second" not in rows["b"][0]
    assert rows["c"][0].endswith("No stop at all")


def test_u5_an_abbreviation_does_not_end_the_first_sentence():
    s = obj(a={"type": "string", "description": "A path, e.g. under the home folder. Then more."},
            b={"type": "string", "description": "Ends here. 2 more follow."})
    rows = by_path(draw(s, {}))
    assert rows["a"][0].endswith("A path, e.g. under the home folder.")
    assert rows["b"][0].endswith("Ends here.")


def test_u5_the_tool_heading_is_the_schema_title_with_the_tool_name():
    c = parse(draw({"type": "object", "title": "Alpha", "description": "The alpha tool. Etc.",
                    "properties": {}}, {}))
    assert c.headings[0] == "Alpha alpha" and "The alpha tool." in c.paras


# --- U6: origins and locks -----------------------------------------------------------

def test_u6_each_key_shows_its_origin_file():
    html = draw(obj(on={"type": "boolean"}), {"on": True}, origins={"on": ORIGIN})
    assert by_path(html)["on"][2] == ORIGIN


@pytest.mark.parametrize("lock", ["busy.file", "busy", "busy.file.part"],
                         ids=["exact", "ancestor", "descendant"])
def test_u6_a_lock_on_the_path_its_ancestor_or_its_descendant_shows(lock):
    s = obj(busy=obj(file={"type": "string"}))
    html = draw(s, {"busy": {"file": "/s.wav"}}, origins={"busy.file": ORIGIN},
                locked={lock: TEMPLATE})
    assert by_path(html)["busy.file"][2] == f"{ORIGIN} | locked by {TEMPLATE}"


def test_u6_a_map_key_with_a_dot_matches_at_dot_boundaries():
    s = obj(subs={"type": "object", "additionalProperties": {"type": "boolean"}})
    values = {"subs": {"box.one": True, "box": False, "box.onex": True}}
    origins = {"subs.box.one": ORIGIN, "subs.box": ORIGIN, "subs.box.onex": ORIGIN}
    html = draw(s, values, origins=origins, locked={"subs.box.one": TEMPLATE})
    rows = by_path(html)
    assert rows['subs."box.one"'][2].endswith(f"locked by {TEMPLATE}")
    assert "locked" not in rows['subs."box.onex"'][2]
    # `subs.box` is an ancestor of the joined lock string, so the cascade's
    # own rule counts it as locked too: the page matches the readers.
    assert rows["subs.box"][2].endswith(f"locked by {TEMPLATE}")


def test_u6_the_lookup_is_the_cascades_own(monkeypatch):
    seen = []

    def spy(joined, locked):
        seen.append(joined)

    monkeypatch.setattr(settings, "_under_lock", spy)
    draw(obj(on={"type": "boolean"}), {"on": True}, locked={"x": TEMPLATE})
    assert seen == ["on"]


# --- U7: keys the schema does not name -----------------------------------------------

def test_u7_a_set_key_the_schema_does_not_name_lands_in_other_keys():
    s = {"type": "object", "properties": {"on": {"type": "boolean"}},
         "additionalProperties": False}
    html = draw(s, {"on": True, "stray": {"deep": 3}}, origins={"stray.deep": ORIGIN})
    c = parse(html)
    assert "Other keys" in c.headings
    assert by_path(html)["stray.deep"][1:] == ("3", ORIGIN)


def test_u7_an_unnamed_key_inside_a_named_group_lands_in_other_keys():
    s = obj(busy=obj(file={"type": "string"}), flags={"type": "object",
                                                      "properties": {"a": True}})
    values = {"busy": {"file": "/s.wav", "stray": 7}, "flags": {"a": 1}}
    origins = {"busy.file": ORIGIN, "busy.stray": ORIGIN, "flags.a": ORIGIN}
    html = draw(s, values, origins=origins)
    rows = by_path(html)
    assert "Other keys" in parse(html).headings
    assert rows["busy.stray"][1:] == ("7", ORIGIN)
    # A member whose schema is not an object is not drawn in the group, so
    # it is listed too.
    assert rows["flags.a"][1:] == ("1", ORIGIN)
    assert rows["busy.file"][1] == "/s.wav"


def test_u7_no_other_keys_heading_when_every_key_is_named():
    assert "Other keys" not in parse(draw(obj(on={"type": "boolean"}), {"on": True})).headings


# --- U8: hostile text is escaped -----------------------------------------------------

class Markup(HTMLParser):
    """Every tag name and attribute name a browser would see."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.attrs: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attrs |= {name for name, _ in attrs}


def markup(html: str) -> tuple[set[str], set[str]]:
    m = Markup()
    m.feed(html)
    m.close()
    return m.tags, m.attrs


HOSTILE = ['<script>alert(1)</script>', '"><img src=x onerror=alert(1)>']


@pytest.mark.parametrize("bad", HOSTILE)
def test_u8_hostile_text_anywhere_comes_out_escaped(bad):
    s = {"type": "object", "title": bad, "description": bad,
         "properties": {"a": {"type": "string", "title": bad, "x-hint": bad},
                        "b": {"anyOf": [{"const": 1, "title": bad}]},
                        "c": {"type": "object", "title": bad,
                              "additionalProperties": {"type": "string"}}}}
    values = {"a": bad, "b": 1, "c": {bad: bad}, bad: bad}
    html = page.draw_tool(bad, values, {"a": bad}, {"a": bad}, schema=s, schema_file=bad,
                          note=bad)
    tags, attrs = markup(html)
    assert not tags & {"script", "img"} and not any(a.startswith("on") for a in attrs)
    assert attrs <= {"id"}
    assert html.count("&lt;") >= 8  # every copy of the text is there, escaped


@pytest.mark.parametrize("bad", HOSTILE)
def test_u8_the_flat_table_escapes_too(bad):
    html = page.draw_tool("alpha", {bad: bad}, {bad: bad}, {}, schema=None)
    tags, attrs = markup(html)
    assert not tags & {"script", "img"} and attrs <= {"id"}


# --- U9: a schema that cannot be drawn -----------------------------------------------

def test_u9_an_unreadable_schema_says_so_and_falls_back_to_the_flat_table():
    html = draw(None, {"on": True}, origins={"on": ORIGIN}, schema_error="JSONDecodeError: bad")
    c = parse(html)
    assert "unreadable schema: JSONDecodeError: bad" in c.paras
    assert by_path(html)["on"][1:] == ("true", ORIGIN)


@pytest.mark.parametrize("schema", [["not", "an", "object"], True, "text"])
def test_u9_a_schema_that_is_not_an_object_falls_back_to_the_flat_table(schema):
    html = draw(schema, {"on": True})
    assert "unreadable schema: it is not a JSON object" in parse(html).paras
    assert by_path(html)["on"][1] == "true"


def test_u9_no_schema_keeps_the_flat_table_and_nothing_set_says_so():
    assert by_path(draw(None, {"a": {"b": 1}}))["a.b"][1] == "1"
    assert "nothing set" in parse(draw(None, {})).paras


# --- U10: x-widget is ignored --------------------------------------------------------

def test_u10_an_x_widget_is_ignored_and_the_key_is_drawn_by_its_type():
    s = obj(device={"type": "string", "x-widget": "audio-input"},
            out={"type": "array", "items": {"type": "string"}, "x-widget": "audio-output"})
    rows = by_path(draw(s, {"device": "default", "out": ["b"]}))
    assert rows["device"][1] == "default" and rows["out"][1] == '["b"]'


# --- U11: pure -----------------------------------------------------------------------

def test_u11_drawing_reads_and_writes_no_file(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the draw function touched a file")

    s = obj(on={"type": "boolean"}, g=obj(x={"type": "integer"}))
    with monkeypatch.context() as m:  # only around the draw: pytest itself opens files
        m.setattr(builtins, "open", refuse)
        m.setattr(io, "open", refuse)
        for name in ("read_text", "read_bytes", "write_text", "write_bytes", "open", "stat",
                     "exists", "is_file", "is_dir", "iterdir"):
            m.setattr(pathlib.Path, name, refuse)
        html = draw(s, {"on": True, "g": {"x": 1}, "z": 2}, origins={"on": ORIGIN},
                    locked={"g": TEMPLATE}, schema_file="alpha.schema.json")
        flat = page.draw_tool("alpha", {"a": 1}, {}, {})
    assert "On" in html and "Other keys" in html and "<table>" in flat


def test_u11_the_module_imports_nothing_outside_the_stdlib_and_this_package():
    import ast
    import sys

    tree = ast.parse(pathlib.Path(page.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots - set(sys.stdlib_module_names) == {"sessiontools"}
