"""Every link from a doc into this repo is relative, and it resolves.

The docs are read in a checkout as often as on a web page: in an editor,
offline, or from an unpacked archive. A full URL into this repo opens a
browser there, so a link to a file in this repo is written relative to the
page it is on (`docs/x.md` from README.md, `x.md` or `../README.md` from a
page in docs/). Its target must name a real path, and its #anchor a real
heading. A link to another repo stays a full URL; external links and pure
#anchors are fine.

README.md and every page under docs/ are checked.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = [README, *sorted((ROOT / "docs").rglob("*.md"))]

REPO_URL = "https://github.com/sessionkit/sessiontools"
REPO_PATH = urlsplit(REPO_URL).path  # /sessionkit/sessiontools
RAW_HOST = "raw.githubusercontent.com"

# A fenced code block: its contents are text, not links.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[`~]*[ \t]*$",
                    re.MULTILINE | re.DOTALL)
# An inline code span: also text, not a link.
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1", re.DOTALL)
# [text](target) and ![alt](target); the target may be <bracketed>. A
# lookahead, so a link nested in another link's text is found too: the
# image in [![alt](img)](url) is a link of its own. A [ right after ! only
# starts a match at the !, so an image is not also counted as a link.
_INLINE = re.compile(
    r"(?=(?:!|(?<!!))\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*(<[^>]*>|[^\s)]*))")
# [label]: target, at most three spaces of indent; the target may start
# on the next line.
_REFDEF = re.compile(r"^ {0,3}\[[^\]]+\]:[ \t]*\n?[ \t]*(<[^>]*>|\S+)", re.MULTILINE)
# html href="..." / src='...'.
_HTML = re.compile(r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# A heading, for its anchor.
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)


def link_targets(text: str) -> list[str]:
    """Every link target in markdown TEXT: inline, reference definition, html."""
    text = _FENCE.sub("", text)
    text = _CODE_SPAN.sub("", text)
    targets = [m.group(1) for m in _INLINE.finditer(text)]
    targets += [m.group(1) for m in _REFDEF.finditer(text)]
    targets += [m.group(1) or m.group(2) for m in _HTML.finditer(text)]
    return [t[1:-1] if t.startswith("<") and t.endswith(">") else t for t in targets]


def problem(target: str) -> str | None:
    """Why TARGET breaks the rule, or None when it keeps it. Whether a
    relative target resolves is `unresolved`'s job."""
    if target.startswith("#"):
        return None  # an anchor on the same page
    if target.startswith("//") or _SCHEME.match(target):
        parts = urlsplit(target if not target.startswith("//") else "https:" + target)
        host = parts.netloc.lower()
        path = parts.path.rstrip("/").lower()
        if host == RAW_HOST and (path + "/").startswith(REPO_PATH.lower() + "/"):
            return f"full URL into this repo, write it relative: {target}"
        if parts.scheme not in ("http", "https") or host not in ("github.com", "www.github.com"):
            return None  # external
        if path == REPO_PATH.lower():
            return None  # the repo's own home page
        if path.startswith(REPO_PATH.lower() + "/"):
            return f"full URL into this repo, write it relative: {target}"
        return None  # another repo
    if not target:
        return "empty link target"
    if target.startswith("/"):
        return f"root-relative link, write it relative to the page: {target}"
    return None


def problems(text: str) -> list[str]:
    return [p for p in map(problem, link_targets(text)) if p]


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped,
    spaces to hyphens."""
    return re.sub(r"[^a-z0-9 _-]", "", heading.strip().lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    return {_slug(h) for h in _HEADING.findall(_FENCE.sub("", text))}


def unresolved(page: Path, text: str) -> list[str]:
    """Each relative target in TEXT (on PAGE) whose path or #anchor is missing."""
    missing = []
    for target in link_targets(text):
        if not target or target.startswith("//") or _SCHEME.match(target) or problem(target):
            continue
        path, _, fragment = target.partition("#")
        dest = (page.parent / unquote(path.split(" ")[0])) if path else page
        if not dest.exists():
            missing.append(f"{target} (no such path)")
        elif fragment and dest.suffix == ".md" and fragment not in anchors(
                text if dest == page else dest.read_text(encoding="utf-8")):
            missing.append(f"{target} (no such heading)")
    return missing


def _name(page: Path) -> str:
    return str(page.relative_to(ROOT))


# --- the rule, applied to the docs ----------------------------------------


@pytest.mark.parametrize("page", DOCS, ids=_name)
def test_doc_links_into_this_repo_are_relative(page):
    found = problems(page.read_text(encoding="utf-8"))
    assert not found, f"{_name(page)} links that need a browser:\n" + "\n".join(found)


@pytest.mark.parametrize("page", DOCS, ids=_name)
def test_doc_relative_links_resolve(page):
    missing = unresolved(page, page.read_text(encoding="utf-8"))
    assert not missing, f"{_name(page)} links to paths not in the repo:\n" + "\n".join(missing)


def test_readme_links_into_the_docs():
    """A check that read no relative link would pass: the README has them."""
    targets = link_targets(README.read_text(encoding="utf-8"))
    assert "docs/configuration.md" in targets
    assert "LICENSE" in targets


# --- the checker itself ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "[a](https://example.com/x)",
    "[a](http://example.com/x)",
    "[a](mailto:someone@example.com)",
    "[a](#install)",
    "[a](https://github.com/other/project/blob/dev/README.md)",
    "[a](https://github.com/sessionkit/sessionkit/blob/HEAD/docs/settings.md)",
    f"[a]({REPO_URL})",
    f"[a]({REPO_URL}/)",
    "[a](docs/x.md)",
    "[a](./docs/x.md)",
    "[a](../README.md)",
    "[a](docs/x.md#part)",
    "[a](LICENSE)",
    "[a](<docs/x.md>)",
    '[a](docs/x.md "a title")',
    "![logo](img/logo.png)",
    "[label]: docs/x.md",
    "[label]:\n  docs/x.md",
    "   [label]: CHANGELOG.md",
    "[![badge](img/badge.svg)](docs/x.md)",
    '<a href="docs/x.md">x</a>',
    "<img src='img/logo.png'>",
    f"```console\n[a]({REPO_URL}/blob/master/x.md)\n```",
    f"~~~\n[a]({REPO_URL}/blob/master/x.md)\n~~~",
    f"use `[a]({REPO_URL}/blob/master/x.md)` as written",
    "[a][label] with no definition",
    "plain text, no links at all",
])
def test_checker_passes(text):
    assert problems(text) == []


@pytest.mark.parametrize("text", [
    f"[a]({REPO_URL}/blob/master/docs/x.md)",
    f"[a]({REPO_URL}/blob/master/docs/x.md#part)",
    f"[a]({REPO_URL}/tree/master/docs)",
    f"[a]({REPO_URL}/raw/master/docs/x.md)",
    f"[a]({REPO_URL}/docs/x.md)",
    f"[a](<{REPO_URL}/blob/master/docs/x.md>)",
    f"[label]: {REPO_URL}/blob/master/LICENSE",
    f"[label]:\n  {REPO_URL}/blob/master/LICENSE",
    f"![logo]({REPO_URL}/blob/master/img/logo.png)",
    f"[![badge](img/badge.svg)]({REPO_URL}/blob/master/docs/x.md)",
    f'<a href="{REPO_URL}/blob/master/LICENSE">x</a>',
    f"[a](http://github.com{REPO_PATH}/blob/master/x.md)",
    f"[a](https://www.github.com{REPO_PATH}/blob/master/x.md)",
    f"[a](https://GitHub.com{REPO_PATH.upper()}/blob/master/x.md)",
    f"[a](https://{RAW_HOST}{REPO_PATH}/master/x.md)",
    "[a](/docs/x.md)",
    "[a]()",
    "text\n```\ncode\n```\n" + f"[a]({REPO_URL}/blob/master/x.md)",
])
def test_checker_flags(text):
    assert len(problems(text)) == 1, problems(text)


def test_unresolved_flags_a_missing_path_and_a_missing_heading(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("# Title\n\n## The `y` part\n")
    (tmp_path / "LICENSE").write_text("text\n")
    page = tmp_path / "README.md"
    text = ("# Readme\n\n[a](docs/x.md) [b](docs/x.md#the-y-part) [c](LICENSE)"
            " [d](#readme) [e](docs/nope.md) [f](docs/x.md#nope) [g](#nope)\n")
    page.write_text(text)
    assert unresolved(page, text) == [
        "docs/nope.md (no such path)",
        "docs/x.md#nope (no such heading)",
        "#nope (no such heading)",
    ]
    # From a page in docs/, ../ climbs to the repo root.
    sub = tmp_path / "docs" / "y.md"
    assert unresolved(sub, "[a](../LICENSE) [b](x.md#title) [c](../x.md)") == [
        "../x.md (no such path)",
    ]
