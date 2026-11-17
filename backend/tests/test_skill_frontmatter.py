"""One SKILL.md frontmatter parser for the skills API and skill packages.

``skill_package_manager`` carried its own copy of the parser in
``services/skill_source.py``. The two agreed on everything but the delimiter
lines, and the shared one keeps what either accepted:

- a delimiter line is ``---`` followed by nothing but whitespace, a CR
  included: the package copy read a CRLF file's frontmatter, the API copy
  read none;
- a ``---something`` line inside the block does not close it: the API copy
  looked on for the real closing line, the package copy gave up.

The 1 MB limit stays the API's (``parse_skill_frontmatter``); a package file
keeps its own limit.

A folded (``>``) or literal (``|``) block scalar, with any chomping indicator,
ends at the first line indented no deeper than its key, and that line is the
next key: both copies used to drop it.
"""

from __future__ import annotations

import ast

import pytest
from fastapi import HTTPException

from app.services.skill_package_manager import (
    parse_skill_markdown,
    rewrite_frontmatter_name,
    validate_frontmatter_warnings,
)
from app.services.skill_source import (
    SKILL_CONTENT_MAX_BYTES,
    frontmatter_bounds,
    parse_skill_frontmatter,
    split_skill_frontmatter,
)
from tests import app_sources

_SKILL = (
    "---\n"
    "name: triage-helper\n"
    'description: "Sorts alerts"\n'
    "compatibility: >-\n"
    "  Needs the\n"
    "  OpenCTI tools\n"
    "\n"
    "  and a KB\n"
    "---\n"
    "# Body\n"
)


@pytest.mark.parametrize("parse", [split_skill_frontmatter, parse_skill_frontmatter])
def test_the_frontmatter_and_its_block_scalars(parse):
    meta, body = parse(_SKILL)
    assert meta == {
        "name": "triage-helper",
        "description": "Sorts alerts",
        "compatibility": "Needs the OpenCTI tools\nand a KB",
    }
    assert body == "# Body\n"
    literal = "---\nname: lit\nnotes: |\n  line one\n  line two\n---\nBody"
    assert parse(literal) == ({"name": "lit", "notes": "line one\nline two"}, "Body")


@pytest.mark.parametrize(
    "raw",
    [
        "# No frontmatter\n",
        "---\nname: open but never closed\n",
        "----\nname: not a delimiter\n---\nbody",
        "---",
    ],
)
def test_no_frontmatter_leaves_the_text_whole(raw):
    assert split_skill_frontmatter(raw) == ({}, raw)
    assert frontmatter_bounds(raw) is None


def test_a_crlf_file_has_frontmatter():
    raw = "---\r\nname: crlf-skill\r\ndescription: Written on Windows\r\n---\r\nBody\r\n"
    assert split_skill_frontmatter(raw) == (
        {"name": "crlf-skill", "description": "Written on Windows"},
        "Body\r\n",
    )


def test_delimiters_may_carry_trailing_spaces():
    raw = "---  \nname: spaced\n--- \t\nBody"
    assert split_skill_frontmatter(raw) == ({"name": "spaced"}, "Body")


def test_a_dashed_line_inside_the_block_does_not_close_it():
    raw = "---\nname: dashed\n---not a delimiter\ndescription: Read on\n---\nBody"
    assert split_skill_frontmatter(raw) == ({"name": "dashed", "description": "Read on"}, "Body")


def test_a_closing_line_at_the_end_of_the_file():
    assert split_skill_frontmatter("---\nname: last\n---") == ({"name": "last"}, "")


def test_the_size_limit_is_the_api_s_alone():
    big = "---\nname: big\ndescription: Large\n---\n" + "x" * SKILL_CONTENT_MAX_BYTES
    with pytest.raises(HTTPException) as exc:
        parse_skill_frontmatter(big)
    assert exc.value.status_code == 400
    assert split_skill_frontmatter(big)[0] == {"name": "big", "description": "Large"}
    assert validate_frontmatter_warnings(big) == []


def test_the_package_path_reads_a_crlf_skill():
    package = parse_skill_markdown(b"---\r\nname: crlf-skill\r\ndescription: Written on Windows\r\n---\r\nBody\r\n")
    assert package.name == "crlf-skill"


# --- Block scalars end where their key's indentation does --------------------

_FOLDED = (">", ">-", ">+")
_LITERAL = ("|", "|-", "|+")


def _doc(lines: list[str], eol: str = "\n") -> str:
    return eol.join(["---", *lines, "---", "Body"])


@pytest.mark.parametrize("parse", [split_skill_frontmatter, parse_skill_frontmatter])
@pytest.mark.parametrize("indicator", _FOLDED + _LITERAL)
def test_the_key_after_a_block_scalar_is_kept(parse, indicator):
    meta, body = parse(_doc(["name: blocks", f"description: {indicator}", "  line one", "  line two", "license: MIT"]))
    expected = "line one line two" if indicator.startswith(">") else "line one\nline two"
    assert meta == {"name": "blocks", "description": expected, "license": "MIT"}
    assert body == "Body"


@pytest.mark.parametrize("indicator", _FOLDED + _LITERAL)
def test_a_blank_line_inside_a_block(indicator):
    meta, _ = split_skill_frontmatter(
        _doc([f"notes: {indicator}", "  first", "  still first", "", "  second", "", "license: MIT"])
    )
    expected = "first still first\nsecond" if indicator.startswith(">") else "first\nstill first\n\nsecond"
    assert meta == {"notes": expected, "license": "MIT"}


def test_an_indented_continuation_stays_in_the_block():
    meta, _ = split_skill_frontmatter(
        _doc(["description: |", "  first", "    indented deeper", "  last", "license: MIT"])
    )
    # Each line is read stripped, as it always was.
    assert meta == {"description": "first\nindented deeper\nlast", "license": "MIT"}


def test_a_nested_block_ends_at_its_own_key_s_indentation():
    meta, _ = split_skill_frontmatter(
        _doc(["metadata:", "  summary: >", "    one", "    two", "  author: Ada", "name: nested"])
    )
    assert meta == {"summary": "one two", "author": "Ada", "name": "nested"}


@pytest.mark.parametrize("indicator", (">", "|-"))
def test_blocks_in_a_crlf_file(indicator):
    meta, body = split_skill_frontmatter(
        _doc(["name: crlf", f"description: {indicator}", "  one", "  two", "license: MIT"], eol="\r\n")
    )
    expected = "one two" if indicator.startswith(">") else "one\ntwo"
    assert meta == {"name": "crlf", "description": expected, "license": "MIT"}
    assert body == "Body"


def test_a_block_with_nothing_in_it_keeps_no_value_and_the_next_key():
    assert split_skill_frontmatter(_doc(["description: >", "license: MIT"]))[0] == {"license": "MIT"}


def test_a_skill_package_keeps_the_key_after_a_folded_description():
    package = parse_skill_markdown(
        _doc(["name: pkg", "description: >", "  Long", "  text", "compatibility: Needs OpenCTI"]).encode()
    )
    assert (package.name, package.description, package.compatibility) == ("pkg", "Long text", "Needs OpenCTI")


def test_rewriting_the_name_finds_the_same_block():
    assert rewrite_frontmatter_name("---\nname: old\n---\nBody", "new") == "---\nname: new\n---\nBody"
    # A name after a dashed line is still inside the block.
    assert rewrite_frontmatter_name("---\n---x\nname: old\n---\nBody", "new") == "---\n---x\nname: new\n---\nBody"
    # A block scalar name cannot be rewritten line-wise; no frontmatter, nothing to rewrite.
    assert rewrite_frontmatter_name("---\nname: >\n  old\n---\n", "new") == "---\nname: >\n  old\n---\n"
    assert rewrite_frontmatter_name("Body only", "new") == "Body only"


def _searches_a_delimiter(node: ast.AST) -> bool:
    """A ``text.find("\\n---")``-style search for a closing delimiter line."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"find", "rfind", "index", "rindex", "split", "partition", "rpartition"}
        and any(isinstance(arg, ast.Constant) and arg.value == "\n---" for arg in node.args)
    )


def test_only_skill_source_finds_the_frontmatter_delimiters():
    owner = app_sources.APP_DIR / "services" / "skill_source.py"
    spelled = [
        app_sources.relative(path)
        for path in app_sources.python_files()
        if path != owner and any(_searches_a_delimiter(node) for node in app_sources.walk(app_sources.tree(path)))
    ]
    assert any(_searches_a_delimiter(node) for node in app_sources.walk(app_sources.tree(owner)))
    assert not spelled, f"read frontmatter through skill_source.split_skill_frontmatter: {spelled}"
