"""Tests for Notion Markdown -> native block formatting.

Before this change the Notion tools wrapped each line in a single PLAIN-text
rich_text run, so inline markdown (``**bold**``, ``*italic*``, ``code``,
``~~strike~~``, ``[label](url)``) and fenced code blocks were written to the
page as literal characters. These tests pin the new behavior:

- ``_parse_inline_markdown`` -> proper Notion rich_text with annotations/links
- ``_text_to_blocks`` -> headings (incl. #####+ -> h4), nested lists, fenced
  code blocks, tables with inline cells, and inline-formatted paragraphs
- end-to-end ``append_notion_blocks`` no longer leaks literal markers

It also pins the agentic-loop fix: the ``code_interpreter`` tool description
documents that interpreter state is not persisted between calls (the
"state is not persisted between iterations" reasoning the user observed).
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import (
    _NOTION_RICH_TEXT_LIMIT,
    _build_nested_list,
    _normalize_notion_language,
    _normalize_table_row_cells,
    _parse_inline_markdown,
    _text_to_blocks,
    build_tools,
)
from app.integrations.providers.notion.formatting import _sanitize_blocks
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _content(run: dict) -> str:
    return run["text"]["content"]


def _ann(run: dict) -> dict:
    return run.get("annotations", {})


def _joined(runs: list[dict]) -> str:
    return "".join(_content(r) for r in runs)


# ---------------------------------------------------------------------------
# _parse_inline_markdown
# ---------------------------------------------------------------------------


def test_inline_plain_text_is_single_run_without_annotations():
    runs = _parse_inline_markdown("hello world")
    assert len(runs) == 1
    assert _content(runs[0]) == "hello world"
    assert "annotations" not in runs[0]


def test_inline_empty_string_yields_no_runs():
    assert _parse_inline_markdown("") == []


def test_inline_bold():
    runs = _parse_inline_markdown("**Status:** Green")
    assert _joined(runs) == "Status: Green"
    bold = [r for r in runs if _ann(r).get("bold")]
    assert len(bold) == 1
    assert _content(bold[0]) == "Status:"
    # the trailing text must NOT inherit bold
    assert any(_content(r) == " Green" and not _ann(r).get("bold") for r in runs)


def test_inline_italic_both_markers():
    for src in ("*soon*", "_soon_"):
        runs = _parse_inline_markdown(f"due {src}")
        italic = [r for r in runs if _ann(r).get("italic")]
        assert len(italic) == 1, src
        assert _content(italic[0]) == "soon"


def test_inline_code_is_terminal_and_not_reparsed():
    runs = _parse_inline_markdown("use `a ** b` now")
    code = [r for r in runs if _ann(r).get("code")]
    assert len(code) == 1
    # the asterisks inside a code span survive verbatim, never become bold
    assert _content(code[0]) == "a ** b"
    assert not _ann(code[0]).get("bold")


def test_inline_strikethrough():
    runs = _parse_inline_markdown("~~old~~ new")
    strike = [r for r in runs if _ann(r).get("strikethrough")]
    assert len(strike) == 1
    assert _content(strike[0]) == "old"


def test_inline_bold_italic_triple():
    runs = _parse_inline_markdown("***wow***")
    assert len(runs) == 1
    assert _ann(runs[0]).get("bold") and _ann(runs[0]).get("italic")
    assert _content(runs[0]) == "wow"


def test_inline_link():
    runs = _parse_inline_markdown("see [docs](https://example.com/x) now")
    link = [r for r in runs if r["text"].get("link")]
    assert len(link) == 1
    assert link[0]["text"]["link"] == {"url": "https://example.com/x"}
    assert _content(link[0]) == "docs"
    assert _joined(runs) == "see docs now"


def test_inline_bold_link_nesting():
    runs = _parse_inline_markdown("[**Docs**](https://e.com)")
    assert len(runs) == 1
    assert _content(runs[0]) == "Docs"
    assert _ann(runs[0]).get("bold")
    assert runs[0]["text"]["link"] == {"url": "https://e.com"}


def test_inline_no_false_positive_on_multiplication():
    runs = _parse_inline_markdown("compute 2 * 3 * 4 here")
    assert len(runs) == 1
    assert _content(runs[0]) == "compute 2 * 3 * 4 here"
    assert "annotations" not in runs[0]


def test_inline_no_false_positive_on_snake_case():
    runs = _parse_inline_markdown("call my_func_name now")
    assert len(runs) == 1
    assert _content(runs[0]) == "call my_func_name now"


def test_inline_many_sequential_markers_all_parsed():
    """Regression: sequential markers must not exhaust a recursion-depth budget;
    every bold span on a long line has to be parsed (not just the first few)."""
    src = " ".join(f"**b{i}**" for i in range(12))
    runs = _parse_inline_markdown(src)
    bold = [r for r in runs if _ann(r).get("bold")]
    assert len(bold) == 12
    assert _joined(runs) == " ".join(f"b{i}" for i in range(12))


def test_inline_long_run_split_at_2000_chars():
    runs = _parse_inline_markdown("a" * 2500)
    assert len(runs) == 2
    assert len(_content(runs[0])) == _NOTION_RICH_TEXT_LIMIT
    assert len(_content(runs[1])) == 500


# ---------------------------------------------------------------------------
# _text_to_blocks: block-level structure
# ---------------------------------------------------------------------------


def test_blocks_heading_levels_and_deep_collapse():
    blocks = _text_to_blocks("# H1\n## H2\n### H3\n#### H4\n###### H6")
    types = [b["type"] for b in blocks]
    assert types == [
        "heading_1",
        "heading_2",
        "heading_3",
        "heading_4",
        "heading_4",  # ###### collapses to h4
    ]


def test_blocks_hash_without_space_is_paragraph():
    blocks = _text_to_blocks("#nospace stays text")
    assert blocks[0]["type"] == "paragraph"
    assert _content(blocks[0]["paragraph"]["rich_text"][0]) == "#nospace stays text"


def test_blocks_heading_has_inline_formatting():
    blocks = _text_to_blocks("## **Status:** Green")
    body = blocks[0]["heading_2"]["rich_text"]
    assert any(_ann(r).get("bold") and _content(r) == "Status:" for r in body)


def test_blocks_paragraph_has_inline_formatting():
    blocks = _text_to_blocks("The **deadline** is `today`")
    runs = blocks[0]["paragraph"]["rich_text"]
    assert any(_ann(r).get("bold") and _content(r) == "deadline" for r in runs)
    assert any(_ann(r).get("code") and _content(r) == "today" for r in runs)
    assert "**" not in _joined(runs) and "`" not in _joined(runs)


def test_blocks_bullets_numbered_and_todos():
    blocks = _text_to_blocks(
        "- bullet\n* star bullet\n1. first\n2) second\n[] open\n[x] done"
    )
    types = [b["type"] for b in blocks]
    assert types == [
        "bulleted_list_item",
        "bulleted_list_item",
        "numbered_list_item",
        "numbered_list_item",
        "to_do",
        "to_do",
    ]
    assert blocks[4]["to_do"]["checked"] is False
    assert blocks[5]["to_do"]["checked"] is True


def test_blocks_nested_list():
    blocks = _text_to_blocks("- Parent\n  - Child A\n  - Child B\n- Parent 2")
    # two top-level bullets; first has two nested children
    assert len(blocks) == 2
    children = blocks[0]["bulleted_list_item"].get("children", [])
    assert len(children) == 2
    assert _content(children[0]["bulleted_list_item"]["rich_text"][0]) == "Child A"
    assert _content(blocks[1]["bulleted_list_item"]["rich_text"][0]) == "Parent 2"


def test_blocks_quote_and_divider():
    blocks = _text_to_blocks("> a quote\n---")
    assert blocks[0]["type"] == "quote"
    assert _content(blocks[0]["quote"]["rich_text"][0]) == "a quote"
    assert blocks[1]["type"] == "divider"


def test_blocks_fenced_code_block():
    text = "```python\nx = 1  # **not bold**\nprint(x)\n```"
    blocks = _text_to_blocks(text)
    assert len(blocks) == 1
    code = blocks[0]
    assert code["type"] == "code"
    assert code["code"]["language"] == "python"
    body = code["code"]["rich_text"][0]["text"]["content"]
    assert body == "x = 1  # **not bold**\nprint(x)"
    # markdown inside a code fence must remain literal
    assert "annotations" not in code["code"]["rich_text"][0]


def test_blocks_fenced_code_block_unknown_language_falls_back():
    blocks = _text_to_blocks("```fancylang\nhi\n```")
    assert blocks[0]["code"]["language"] == "plain text"


def test_blocks_table_cells_have_inline_formatting():
    text = "| **A** | B |\n| --- | --- |\n| x | `y` |"
    blocks = _text_to_blocks(text)
    assert blocks[0]["type"] == "table"
    rows = blocks[0]["table"]["children"]
    assert blocks[0]["table"]["table_width"] == 2
    header_first_cell = rows[0]["table_row"]["cells"][0]
    assert _ann(header_first_cell[0]).get("bold")
    assert _content(header_first_cell[0]) == "A"
    data_second_cell = rows[1]["table_row"]["cells"][1]
    assert _ann(data_second_cell[0]).get("code")
    assert _content(data_second_cell[0]) == "y"


@pytest.mark.parametrize("sep", ["| --- | --- |", "|---|---|", "| :-- | --: |"])
def test_blocks_table_drops_multicolumn_separator_row(sep):
    """The alignment/separator row must not become a row of literal --- cells."""
    blocks = _text_to_blocks(f"| A | B |\n{sep}\n| x | y |")
    rows = blocks[0]["table"]["children"]
    assert len(rows) == 2  # header + one data row, separator dropped
    assert _content(rows[0]["table_row"]["cells"][0][0]) == "A"
    assert _content(rows[1]["table_row"]["cells"][0][0]) == "x"


def test_blocks_blank_lines_skipped():
    blocks = _text_to_blocks("para one\n\n\npara two")
    assert [b["type"] for b in blocks] == ["paragraph", "paragraph"]


# ---------------------------------------------------------------------------
# _build_nested_list / _normalize_notion_language
# ---------------------------------------------------------------------------


def test_build_nested_list_caps_depth_to_two_levels():
    items = [
        (0, "bulleted_list_item", "L1", False),
        (2, "bulleted_list_item", "L2", False),
        (4, "bulleted_list_item", "L3 deep", False),
    ]
    result = _build_nested_list(items)
    assert len(result) == 1
    children = result[0]["bulleted_list_item"]["children"]
    # the L3 item folds onto level 2 (no third level of nesting)
    assert len(children) == 2
    assert _content(children[0]["bulleted_list_item"]["rich_text"][0]) == "L2"
    assert _content(children[1]["bulleted_list_item"]["rich_text"][0]) == "L3 deep"
    assert "children" not in children[0]["bulleted_list_item"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("py", "python"),
        ("js", "javascript"),
        ("ts", "typescript"),
        ("sh", "shell"),
        ("yml", "yaml"),
        ("python", "python"),
        ("", "plain text"),
        ("totally-made-up", "plain text"),
    ],
)
def test_normalize_notion_language(raw, expected):
    assert _normalize_notion_language(raw) == expected


# ---------------------------------------------------------------------------
# end-to-end: append_notion_blocks must not leak literal markdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_notion_blocks_emits_annotations_not_literal_markdown():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json={}), None)
        result = await executor.execute_tool(
            ToolCall(
                id="call-1",
                name="append_notion_blocks",
                arguments={
                    "block_id": "page-1",
                    "content": "**Status:** Green and `code`",
                },
            )
        )

    assert not result.is_error
    body = mock_req.call_args.kwargs["json"]
    runs = body["children"][0]["paragraph"]["rich_text"]
    joined = _joined(runs)
    assert "**" not in joined and "`" not in joined
    assert any(_ann(r).get("bold") and _content(r) == "Status:" for r in runs)
    assert any(_ann(r).get("code") and _content(r) == "code" for r in runs)


# ---------------------------------------------------------------------------
# _normalize_table_row_cells
# ---------------------------------------------------------------------------


def test_normalize_cells_bare_strings():
    """Each bare string cell must be wrapped in a rich_text list."""
    result = _normalize_table_row_cells(["Alice", "Manager", "Berlin"])
    assert result == [
        [{"type": "text", "text": {"content": "Alice"}}],
        [{"type": "text", "text": {"content": "Manager"}}],
        [{"type": "text", "text": {"content": "Berlin"}}],
    ]


def test_normalize_cells_strings_inside_list():
    """Cells that are lists containing strings must have each string wrapped."""
    result = _normalize_table_row_cells([["Alice"], ["Manager"]])
    assert result == [
        [{"type": "text", "text": {"content": "Alice"}}],
        [{"type": "text", "text": {"content": "Manager"}}],
    ]


def test_normalize_cells_already_rich_text_objects_pass_through():
    """Cells that are already well-formed rich_text lists pass through unchanged."""
    cells = [
        [{"type": "text", "text": {"content": "Alice"}}],
        [{"type": "text", "text": {"content": "Manager"}}],
    ]
    result = _normalize_table_row_cells(cells)
    assert result == cells


def test_normalize_cells_bare_dict_wrapped():
    """A bare rich_text dict (instead of a list) is wrapped in a list."""
    cell = {"type": "text", "text": {"content": "Alice"}}
    result = _normalize_table_row_cells([cell])
    assert result == [[{"type": "text", "text": {"content": "Alice"}}]]


def test_normalize_cells_empty_list_cells_preserved():
    """Empty cell lists are valid (Notion allows empty cells) and preserved."""
    result = _normalize_table_row_cells([[], []])
    assert result == [[], []]


def test_normalize_cells_mixed_annotations_sanitized():
    """Misplaced annotations inside text are moved to a sibling during normalization."""
    cells = [
        [{"type": "text", "text": {"content": "Hi", "annotations": {"bold": True}}}]
    ]
    result = _normalize_table_row_cells(cells)
    assert len(result) == 1
    item = result[0][0]
    assert "annotations" not in item["text"]
    assert item["annotations"] == {"bold": True}


def test_normalize_cells_numeric_scalars_stringified():
    """Numeric scalar cells (the LLM sends numbers for numeric columns) are
    stringified and wrapped rather than dropped."""
    result = _normalize_table_row_cells([42, 3.14, [7]])
    assert result == [
        [{"type": "text", "text": {"content": "42"}}],
        [{"type": "text", "text": {"content": "3.14"}}],
        [{"type": "text", "text": {"content": "7"}}],
    ]


def test_normalize_cells_invalid_type_yields_empty_cell():
    """Unrecognised cell types (None) emit an empty cell rather than raising."""
    result = _normalize_table_row_cells([None])
    assert result == [[]]


def test_normalize_cells_long_string_cell_truncated():
    """A bare string cell longer than Notion's 2000-char rich_text limit must be
    truncated after wrapping — every branch runs _sanitize_rich_text so wrapped
    scalars get the same hardening as object cells."""
    result = _normalize_table_row_cells(["x" * 2500])
    content = result[0][0]["text"]["content"]
    assert len(content) == _NOTION_RICH_TEXT_LIMIT
    assert content.endswith("…")


def test_sanitize_blocks_normalises_table_row_cells_nested_in_table_children():
    """append_notion_blocks_json sends table rows NESTED inside a table block's
    ``children`` — the sanitizer must recurse into children so those rows get
    the same cell normalisation as top-level table_row payloads."""
    blocks = [
        {
            "type": "table",
            "table": {
                "table_width": 2,
                "has_column_header": True,
                "children": [
                    {"type": "table_row", "table_row": {"cells": ["Alice", "Manager"]}},
                    {"type": "table_row", "table_row": {"cells": [["Bob"], ["Dev"]]}},
                ],
            },
        }
    ]
    _sanitize_blocks(blocks)
    rows = blocks[0]["table"]["children"]
    assert rows[0]["table_row"]["cells"] == [
        [{"type": "text", "text": {"content": "Alice"}}],
        [{"type": "text", "text": {"content": "Manager"}}],
    ]
    assert rows[1]["table_row"]["cells"] == [
        [{"type": "text", "text": {"content": "Bob"}}],
        [{"type": "text", "text": {"content": "Dev"}}],
    ]
