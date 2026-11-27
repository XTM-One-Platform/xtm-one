"""Notion Markdown layouts.

The Markdown writer (``formatting._text_to_blocks``) builds what an agent used
to need raw JSON for: tables whose first column is the header, callouts with an
icon, a colour and a heading inside, coloured blocks and column layouts. Notion
takes two levels of nesting per request, so a callout inside a column is
appended under the blocks the first request created (``NotionContext``). The
read side writes colours and callout markers in the same syntax, and an invalid
JSON payload names where it broke.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion._common import (
    NotionContext,
    _block_to_text,
    _parse_json_arg,
    _split_request_nesting,
)
from app.integrations.providers.notion.formatting import (
    MarkdownBlockError,
    _normalize_table_row_cells,
    _text_to_blocks,
)
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

SAFE_REQUEST = "app.integrations.providers.notion._common.safe_request"


def _plain(runs: list[dict]) -> str:
    return "".join(r["text"]["content"] for r in runs)


def _body(block: dict) -> dict:
    return block[block["type"]]


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def test_a_blank_first_row_makes_a_key_value_table_with_a_row_header():
    (table,) = _text_to_blocks("| | |\n|---|---|\n| **Date** | Friday |")
    body = table["table"]
    assert body["has_row_header"] is True
    assert body["has_column_header"] is False
    assert body["table_width"] == 2
    (row,) = body["children"]
    date, day = row["table_row"]["cells"]
    assert _plain(date) == "Date" and date[0]["annotations"] == {"bold": True}
    assert _plain(day) == "Friday"


def test_a_table_with_a_real_header_row_is_unchanged():
    (table,) = _text_to_blocks("| A | B |\n|---|---|\n| x | y |")
    body = table["table"]
    assert body["has_column_header"] is True
    assert body["has_row_header"] is False
    assert [_plain(r["table_row"]["cells"][0]) for r in body["children"]] == ["A", "x"]


def test_a_table_hint_sets_both_flags():
    (table,) = _text_to_blocks("<!-- table: row_header -->\n| A | B |\n|---|---|\n| x | y |")
    assert table["table"]["has_row_header"] is True
    assert table["table"]["has_column_header"] is True

    (table,) = _text_to_blocks(
        "<!-- table: row_header, no_column_header -->\n\n| Date | Friday |\n| Room | 4 |"
    )
    assert table["table"]["has_row_header"] is True
    assert table["table"]["has_column_header"] is False
    assert len(table["table"]["children"]) == 2


def test_a_table_hint_applies_to_the_next_table_only():
    blocks = _text_to_blocks(
        "<!-- table: no_column_header -->\n| a | b |\n\nbetween\n\n| c | d |\n|---|---|\n| e | f |"
    )
    first, _, second = blocks
    assert first["table"]["has_column_header"] is False
    assert second["table"]["has_column_header"] is True


@pytest.mark.parametrize(
    "markdown, fragment",
    [
        ("<!-- table: rowheader -->\n| a | b |", "`rowheader` is not a table flag"),
        ("<!-- table: row_header no_row_header -->\n| a | b |", "both on and off"),
        ("<!-- table: -->\n| a | b |", "names no flag"),
        ("<!-- table: row_header -->\nnot a table", "must be followed by a Markdown table"),
        ("text\n<!-- table: row_header -->", "must be followed by a Markdown table"),
    ],
)
def test_a_table_hint_that_cannot_be_honoured_is_refused(markdown, fragment):
    with pytest.raises(MarkdownBlockError, match=fragment):
        _text_to_blocks(markdown)


def test_an_empty_string_cell_is_an_empty_cell():
    assert _normalize_table_row_cells(["", "x"]) == [[], [{"type": "text", "text": {"content": "x"}}]]


# ---------------------------------------------------------------------------
# Block colours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "markdown, block_type",
    [
        ("## Title {color=blue}", "heading_2"),
        ("Some text {color=red_background}", "paragraph"),
        ("- item {color=green}", "bulleted_list_item"),
        ("1. item {color=green}", "numbered_list_item"),
        ("[x] done {color=gray}", "to_do"),
        ("> quoted {color=purple}", "quote"),
    ],
)
def test_a_trailing_colour_sets_the_block_colour(markdown, block_type):
    (block,) = _text_to_blocks(markdown)
    assert block["type"] == block_type
    body = _body(block)
    assert body["color"] in {"blue", "red_background", "green", "gray", "purple"}
    assert "{" not in _plain(body["rich_text"])


def test_braces_that_are_not_an_attribute_block_stay_in_the_text():
    (block,) = _text_to_blocks("use {x} and {color} here")
    assert _plain(block["paragraph"]["rich_text"]) == "use {x} and {color} here"
    assert "color" not in block["paragraph"]


@pytest.mark.parametrize(
    "markdown, fragment",
    [
        ("## Title {color=teal}", "'teal' is not a Notion colour"),
        ("## Title {icon=🎯}", "`icon` is set on a callout only"),
        ("## Title {color=red color=blue}", "`color` is set twice"),
    ],
)
def test_a_colour_that_cannot_be_set_is_refused(markdown, fragment):
    with pytest.raises(MarkdownBlockError, match=fragment):
        _text_to_blocks(markdown)


# ---------------------------------------------------------------------------
# Callouts
# ---------------------------------------------------------------------------


def test_a_callout_header_holds_its_heading():
    (block,) = _text_to_blocks("> [!icon=🎯 color=blue_background] ## Title {color=blue}")
    callout = block["callout"]
    assert callout["icon"] == {"type": "emoji", "emoji": "🎯"}
    assert callout["color"] == "blue_background"
    assert callout["rich_text"] == []
    (heading,) = callout["children"]
    assert heading["type"] == "heading_2"
    assert _plain(heading["heading_2"]["rich_text"]) == "Title"
    assert heading["heading_2"]["color"] == "blue"


def test_a_callout_takes_its_text_and_the_quoted_lines_after_it():
    (block,) = _text_to_blocks("> [!NOTE] Heads up\n> - one\n>   - nested\n>\n> - two\nafter")[:1]
    callout = block["callout"]
    assert callout["icon"] == {"type": "emoji", "emoji": "ℹ️"}
    assert callout["color"] == "blue_background"
    assert _plain(callout["rich_text"]) == "Heads up"
    one, two = callout["children"]
    assert one["bulleted_list_item"]["children"][0]["type"] == "bulleted_list_item"
    assert _plain(two["bulleted_list_item"]["rich_text"]) == "two"


def test_a_callout_ends_at_a_blank_line_and_a_new_marker():
    blocks = _text_to_blocks("> [!TIP] one\n> [!WARNING] two\n\n> just a quote")
    assert [b["type"] for b in blocks] == ["callout", "callout", "quote"]
    assert blocks[1]["callout"]["color"] == "yellow_background"
    assert "children" not in blocks[0]["callout"]


def test_an_attribute_overrides_the_kind_default_and_a_url_is_an_external_icon():
    (block,) = _text_to_blocks("> [!WARNING color=red icon=https://example.com/i.png] careful")
    assert block["callout"]["color"] == "red"
    assert block["callout"]["icon"] == {"type": "external", "external": {"url": "https://example.com/i.png"}}


@pytest.mark.parametrize(
    "markdown, fragment",
    [
        ("> [!] text", "names no callout"),
        ("> [!size=big] text", "`size` is not a callout attribute"),
        ("> [!FANCY] text", "`FANCY` in `\\[!FANCY\\]` is neither"),
        ("> [!color=sky] text", "'sky' is not a Notion colour"),
    ],
)
def test_a_callout_marker_that_cannot_be_honoured_is_refused(markdown, fragment):
    with pytest.raises(MarkdownBlockError, match=fragment):
        _text_to_blocks(markdown)


def test_plain_quotes_are_unchanged():
    blocks = _text_to_blocks("> one\n> two")
    assert [b["type"] for b in blocks] == ["quote", "quote"]


# ---------------------------------------------------------------------------
# Column layouts
# ---------------------------------------------------------------------------

TWO_COLUMNS = """\
::: columns
::: column
> [!icon=🎯 color=blue_background] ## Goals
- ship it
> [!icon=⚠️ color=yellow_background] ## Risks
::: column
> [!icon=📅 color=green_background] ## Dates
| | |
|---|---|
| **Kick-off** | Monday |
:::
Closing paragraph"""


def test_a_two_column_page_with_coloured_callout_headers():
    column_list, closing = _text_to_blocks(TWO_COLUMNS)
    assert closing["type"] == "paragraph"
    left, right = column_list["column_list"]["children"]
    assert [b["type"] for b in left["column"]["children"]] == ["callout", "bulleted_list_item", "callout"]
    goals = left["column"]["children"][0]["callout"]
    assert goals["color"] == "blue_background"
    assert _plain(goals["children"][0]["heading_2"]["rich_text"]) == "Goals"
    dates, table = right["column"]["children"]
    assert dates["callout"]["icon"]["emoji"] == "📅"
    assert table["table"]["has_row_header"] is True


def test_the_first_column_may_be_implicit_and_an_empty_one_gets_a_paragraph():
    (column_list,) = _text_to_blocks("::: columns\nleft\n::: column\n::: column\nright\n:::")
    columns = column_list["column_list"]["children"]
    assert len(columns) == 3
    assert columns[1]["column"]["children"] == [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}
    ]


def test_an_unclosed_layout_closes_at_the_end_and_code_fences_are_skipped():
    (column_list,) = _text_to_blocks("::: columns\n```\n:::\n```\n::: column\nright")
    left, right = column_list["column_list"]["children"]
    assert left["column"]["children"][0]["code"]["rich_text"][0]["text"]["content"] == ":::"
    assert _plain(right["column"]["children"][0]["paragraph"]["rich_text"]) == "right"


@pytest.mark.parametrize(
    "markdown, fragment",
    [
        ("::: columns\nonly one\n:::", "at least two columns"),
        ("::: columns\na\n::: column\n::: columns\n:::", "cannot hold another one"),
        (":::", "outside a column layout"),
        ("::: column\ntext", "outside a column layout"),
    ],
)
def test_a_column_layout_that_cannot_be_built_is_refused(markdown, fragment):
    with pytest.raises(MarkdownBlockError, match=fragment):
        _text_to_blocks(markdown)


# ---------------------------------------------------------------------------
# What one request carries
# ---------------------------------------------------------------------------


def _max_request_level(blocks: list, level: int = 0) -> int:
    deepest = level
    for block in blocks:
        children = _body(block).get("children") or []
        assert len(children) <= 100
        if children:
            deepest = max(deepest, _max_request_level(children, level + 1))
    return deepest


def test_a_callout_inside_a_column_is_cut_at_the_callout():
    blocks = _text_to_blocks(TWO_COLUMNS)
    original = copy.deepcopy(blocks)
    sendable, deferred = _split_request_nesting(blocks)
    assert blocks == original  # never mutated
    assert _max_request_level(sendable) <= 2
    paths = [entry.path for entry in deferred]
    assert (0, 0, 0) in paths and (0, 0, 2) in paths and (0, 1, 0) in paths
    goals = sendable[0]["column_list"]["children"][0]["column"]["children"][0]["callout"]
    assert "children" not in goals and goals["color"] == "blue_background"


def test_a_table_that_would_be_too_deep_is_cut_with_its_siblings_after_a_placeholder():
    (column_list,) = _text_to_blocks("::: columns\n| a | b |\n| c | d |\nafter\n::: column\nright\n:::")
    sendable, deferred = _split_request_nesting([column_list])
    first_column = sendable[0]["column_list"]["children"][0]["column"]
    assert first_column["children"] == [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}
    ]
    (entry,) = deferred
    assert entry.path == (0, 0)
    assert entry.drop_placeholder is True
    assert [b["type"] for b in entry.children] == ["table", "paragraph"]


def test_more_than_a_hundred_rows_are_appended_after_the_first_hundred():
    rows = "\n".join(f"| r{i} | v |" for i in range(130))
    (table,) = _text_to_blocks(f"| k | v |\n|---|---|\n{rows}")
    sendable, deferred = _split_request_nesting([table])
    assert len(sendable[0]["table"]["children"]) == 100
    (entry,) = deferred
    assert entry.path == (0,) and len(entry.children) == 31


# ---------------------------------------------------------------------------
# The tools, against a Notion that enforces the request limits
# ---------------------------------------------------------------------------


class FakeNotion:
    """Block children per parent; refuses a request Notion would refuse."""

    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self._next = 0

    def seed(self, block_id: str, block: dict) -> None:
        self.records[block_id] = {"id": block_id, "has_children": False, **block}

    def _store(self, parent: str, blocks: list) -> list[dict]:
        created = []
        if parent in self.records:
            self.records[parent]["has_children"] = True
        for block in blocks:
            self._next += 1
            block_id = f"blk-{self._next}"
            body = dict(_body(block))
            kids = body.pop("children", None) or []
            record = {"id": block_id, "type": block["type"], block["type"]: body, "has_children": False}
            self.records[block_id] = record
            self.children.setdefault(parent, []).append(block_id)
            if kids:
                self._store(block_id, kids)
            created.append(record)
        return created

    async def request(self, client, method, url, provider, action, **kwargs):
        path = urlparse(url).path.strip("/").split("/")[1:]
        payload = kwargs.get("json")
        self.calls.append((method, "/".join(path), payload))
        if method == "patch" and path[-1] == "children":
            children = payload["children"]
            if len(children) > 100 or _max_request_level(children) > 2:
                return None, "Error: Notion API returned 400 (body failed validation)"
            return httpx.Response(200, json={"results": self._store(path[1], children)}), None
        if method == "get" and path[-1] == "children":
            ids = self.children.get(path[1], [])
            return httpx.Response(200, json={"results": [self.records[i] for i in ids], "has_more": False}), None
        if method == "delete":
            for ids in self.children.values():
                if path[1] in ids:
                    ids.remove(path[1])
            return httpx.Response(200, json={}), None
        if method == "post" and path == ["pages"]:
            children = payload.get("children") or []
            if len(children) > 100 or _max_request_level(children) > 2:
                return None, "Error: Notion API returned 400 (body failed validation)"
            self._store("page-new", children)
            return httpx.Response(200, json={"id": "page-new", "url": "https://www.notion.so/page-new"}), None
        if method == "get" and path[0] in ("databases", "data_sources"):
            return httpx.Response(404, json={"code": "object_not_found"}), "Error: Notion resource not found"
        if method == "get" and path[0] in ("pages", "blocks") and len(path) == 2:
            record = self.records.get(path[1], {"id": path[1], "object": "page"})
            return httpx.Response(200, json=record), None
        raise AssertionError(f"unexpected call {method} {url}")


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments))


def _tree(fake: FakeNotion, parent: str) -> list:
    out = []
    for block_id in fake.children.get(parent, []):
        record = fake.records[block_id]
        out.append((record["type"], _tree(fake, block_id)))
    return out


@pytest.mark.asyncio
async def test_one_markdown_call_builds_the_two_column_page_and_reads_back_the_same_structure():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    fake = FakeNotion()
    with patch(SAFE_REQUEST, new=fake.request):
        result = await _call(executor, "append_notion_blocks", block_id="page-1", content=TWO_COLUMNS)
        assert not result.is_error, result.content
        assert "Appended 2 block(s)" in result.content
        assert _tree(fake, "page-1") == [
            (
                "column_list",
                [
                    (
                        "column",
                        [
                            ("callout", [("heading_2", [])]),
                            ("bulleted_list_item", []),
                            ("callout", [("heading_2", [])]),
                        ],
                    ),
                    ("column", [("callout", [("heading_2", [])]), ("table", [("table_row", [])])]),
                ],
            ),
            ("paragraph", []),
        ]
        listing = await _call(executor, "list_notion_block_children", block_id="page-1", depth=4)
    assert not listing.is_error, listing.content
    text = listing.content
    assert "1.1.1. [callout]" in text and "> [!icon=🎯 color=blue_background]" in text
    assert "1.1.1.1. [heading_2]" in text and "## Goals" in text
    assert "1.2.2. [table]" in text


@pytest.mark.asyncio
async def test_a_table_first_in_a_column_lands_in_order_and_the_placeholder_is_removed():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    fake = FakeNotion()
    markdown = "::: columns\n| a | b |\n|---|---|\n| c | d |\nafter\n::: column\nright\n:::"
    with patch(SAFE_REQUEST, new=fake.request):
        result = await _call(executor, "append_notion_blocks", block_id="page-1", content=markdown)
    assert not result.is_error, result.content
    assert _tree(fake, "page-1") == [
        (
            "column_list",
            [
                ("column", [("table", [("table_row", []), ("table_row", [])]), ("paragraph", [])]),
                ("column", [("paragraph", [])]),
            ],
        )
    ]
    assert any(method == "delete" for method, _, _ in fake.calls)


@pytest.mark.asyncio
async def test_append_notion_blocks_json_appends_deep_children_too():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    fake = FakeNotion()
    children = _text_to_blocks(TWO_COLUMNS)
    with patch(SAFE_REQUEST, new=fake.request):
        result = await _call(executor, "append_notion_blocks_json", block_id="page-1", children_json=children)
    assert not result.is_error, result.content
    goals = fake.children[fake.children[fake.children["page-1"][0]][0]][0]
    assert fake.records[goals]["has_children"] is True


@pytest.mark.asyncio
async def test_create_notion_page_builds_the_nested_layout_under_the_new_page():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    fake = FakeNotion()
    with patch(SAFE_REQUEST, new=fake.request):
        result = await _call(
            executor, "create_notion_page", parent_id="parent-page", title="Plan", content=TWO_COLUMNS
        )
    assert not result.is_error, result.content
    assert "Page created" in result.content and "but" not in result.content
    left_column = fake.children[fake.children["page-new"][0]][0]
    goals = fake.children[left_column][0]
    assert [fake.records[c]["type"] for c in fake.children[goals]] == ["heading_2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["append_notion_blocks", "create_notion_page"])
async def test_a_refused_directive_sends_nothing(tool):
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    arguments = {"content": "::: columns\nonly\n:::"}
    arguments |= {"block_id": "page-1"} if tool == "append_notion_blocks" else {"parent_id": "p", "title": "T"}
    with patch(SAFE_REQUEST, new_callable=AsyncMock) as mock_req:
        result = await _call(executor, tool, **arguments)
    assert result.is_error
    assert "at least two columns" in result.content
    assert "nothing has been" in result.content
    mock_req.assert_not_called()


def _live(block_type: str, text: str, **fields) -> dict:
    return {
        "id": "blk",
        "type": block_type,
        block_type: {
            "rich_text": [
                {"type": "text", "text": {"content": text}, "plain_text": text, "annotations": {}}
            ],
            **fields,
        },
    }


@pytest.mark.asyncio
async def test_update_notion_block_content_sets_the_colour_instead_of_writing_it():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    fake = FakeNotion()
    fake.seed("blk", _live("heading_2", "Old"))
    fake_patch = AsyncMock(return_value=(httpx.Response(200, json={}), None))

    async def route(client, method, url, provider, action, **kwargs):
        if method == "patch":
            return await fake_patch(url, json=kwargs["json"])
        return await fake.request(client, method, url, provider, action, **kwargs)

    with patch(SAFE_REQUEST, new=AsyncMock(side_effect=route)):
        result = await _call(executor, "update_notion_block", block_id="blk", content="## Title {color=red}")
    assert not result.is_error, result.content
    sent = fake_patch.call_args.kwargs["json"]["heading_2"]
    assert _plain(sent["rich_text"]) == "Title"
    assert sent["color"] == "red"


@pytest.mark.asyncio
async def test_update_notion_block_content_sets_a_callout_icon_and_colour():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    fake = FakeNotion()
    fake.seed("blk", _live("callout", "Old", icon={"type": "emoji", "emoji": "💡"}, color="gray_background"))
    fake_patch = AsyncMock(return_value=(httpx.Response(200, json={}), None))

    async def route(client, method, url, provider, action, **kwargs):
        if method == "patch":
            return await fake_patch(url, json=kwargs["json"])
        return await fake.request(client, method, url, provider, action, **kwargs)

    with patch(SAFE_REQUEST, new=AsyncMock(side_effect=route)):
        result = await _call(
            executor, "update_notion_block", block_id="blk", content="> [!icon=🎯 color=blue_background] New"
        )
    assert not result.is_error, result.content
    sent = fake_patch.call_args.kwargs["json"]["callout"]
    assert _plain(sent["rich_text"]) == "New"
    assert sent["icon"] == {"type": "emoji", "emoji": "🎯"}
    assert sent["color"] == "blue_background"


@pytest.mark.asyncio
async def test_get_notion_page_reads_a_callout_inside_a_column():
    fake = FakeNotion()
    fake._store("page-1", _text_to_blocks(TWO_COLUMNS))
    ctx = NotionContext("fake-token")
    with patch(SAFE_REQUEST, new=fake.request):
        async with ctx.client() as client:
            blocks = await ctx.fetch_all_blocks(client, "page-1")
    headings = [b for b in blocks if b["type"] == "heading_2"]
    assert len(headings) == 3
    assert {b["_depth"] for b in headings} == {3}


# ---------------------------------------------------------------------------
# The read side writes what the writer reads
# ---------------------------------------------------------------------------


def test_a_block_colour_and_a_callout_marker_read_in_the_writer_syntax():
    assert _block_to_text(_live("heading_2", "Title", color="blue")) == "## Title {color=blue}"
    assert _block_to_text(_live("to_do", "Task", color="red", checked=True)) == "☑ Task {color=red}"
    assert _block_to_text(_live("paragraph", "Plain", color="default")) == "Plain"
    callout = _live("callout", "Note", icon={"type": "emoji", "emoji": "🎯"}, color="blue_background")
    assert _block_to_text(callout) == "> [!icon=🎯 color=blue_background] Note"
    bare = {"type": "callout", "callout": {"rich_text": [], "color": "default"}}
    assert _block_to_text(bare) == "> [!callout]"


def test_a_heading_4_reads_its_text() -> None:
    block = {
        "type": "heading_4",
        "heading_4": {
            "rich_text": [{"type": "text", "text": {"content": "Test"}, "plain_text": "Test", "annotations": {}}]
        },
    }
    assert _block_to_text(block) == "#### Test"


def test_a_heading_4_written_read_and_written_again_round_trips() -> None:
    (written,) = _text_to_blocks("#### Title {color=blue}")
    assert written["type"] == "heading_4"
    read = _block_to_text(written)
    assert read == "#### Title {color=blue}"
    (again,) = _text_to_blocks(read)
    assert again == written


@pytest.mark.asyncio
async def test_update_notion_block_content_strips_the_heading_4_prefix() -> None:
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    fake = FakeNotion()
    fake.seed("blk", _live("heading_4", "Old"))
    fake_patch = AsyncMock(return_value=(httpx.Response(200, json={}), None))
    fake_get = AsyncMock(side_effect=fake.request)

    async def route(*args: Any, **kwargs: Any) -> Any:
        if args[1] == "patch":
            return await fake_patch(args[2], json=kwargs["json"])
        return await fake_get(*args, **kwargs)

    with patch(SAFE_REQUEST, new=AsyncMock(side_effect=route)):
        result = await _call(executor, "update_notion_block", block_id="blk", content="#### New")
    assert not result.is_error, result.content
    assert _plain(fake_patch.call_args.kwargs["json"]["heading_4"]["rich_text"]) == "New"


@pytest.mark.parametrize("value", ["none", "None", "null"])
def test_a_callout_marker_asking_for_no_icon_is_refused_with_the_notion_limit(value: str) -> None:
    with pytest.raises(MarkdownBlockError, match="Notion does not allow removing a callout icon"):
        _text_to_blocks(f"> [!icon={value}] text")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "path"),
    [
        ("update_notion_block", {"block_json": '{"callout": {"icon": null}}'}, "`callout.icon` is null"),
        (
            "append_notion_blocks_json",
            {"children_json": '[{"type": "callout", "callout": {"rich_text": [], "icon": null}}]'},
            "`children[0].callout.icon` is null",
        ),
    ],
)
async def test_a_null_callout_icon_is_refused_before_any_call(tool: str, arguments: dict[str, str], path: str) -> None:
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    with patch(SAFE_REQUEST, new_callable=AsyncMock) as mock_req:
        result = await _call(executor, tool, block_id="blk", **arguments)
    assert result.is_error
    assert path in result.content
    assert "Notion does not allow removing a callout icon through the API; remove it in the Notion UI" in result.content
    mock_req.assert_not_called()


@pytest.mark.parametrize(
    "block",
    [
        _live("heading_2", "Title", color="blue"),
        _live("heading_4", "Deep", color="blue"),
        _live("paragraph", "Body", color="red_background"),
        _live("callout", "Note", icon={"type": "emoji", "emoji": "🎯"}, color="blue_background"),
    ],
)
def test_a_block_read_and_written_back_keeps_its_colour_and_icon(block):
    (written,) = _text_to_blocks(_block_to_text(block))
    assert written["type"] == block["type"]
    for field in ("color", "icon"):
        assert _body(written).get(field) == _body(block).get(field)
    assert _plain(_body(written)["rich_text"]) == block[block["type"]]["rich_text"][0]["plain_text"]


# ---------------------------------------------------------------------------
# JSON errors name where the payload broke
# ---------------------------------------------------------------------------


def test_an_invalid_json_payload_names_the_offset_and_quotes_the_excerpt():
    _, err = _parse_json_arg('[{"type": "x"} {"type": "y"}]', what="children_json", expected="array")
    assert err.startswith("Error: Invalid children_json")
    assert "must be valid JSON (Expecting ',' delimiter at character 15, line 1 column 16" in err
    assert '[{"type": "x"} >>>{"type": "y"}]' in err


def test_the_excerpt_is_forty_characters_around_the_position_on_one_line():
    payload = "[" + '"a",' * 30 + '"b" "c",' + '"d",' * 30 + '"e"]'
    _, err = _parse_json_arg(payload, what="children_json")
    excerpt = err.split("in: ", 1)[1].removesuffix(").")
    assert excerpt.startswith("...") and excerpt.endswith("...")
    assert len(excerpt.replace("...", "").replace(">>>", "")) == 40

    _, err = _parse_json_arg('{"a": 1,\n\n "b" 2}', what="block_json")
    assert "line 3 column 6" in err and "\\n" in err
