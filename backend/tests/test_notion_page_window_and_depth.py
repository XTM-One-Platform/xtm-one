"""``get_notion_page`` windows and ``list_notion_block_children`` descends.

``get_notion_page`` cut at 8,000 characters with no way to continue, so the
second half of a long strategy page (objectives 1.5 onward) was unreachable
in any number of calls; ``list_notion_block_children`` listed the direct
children only, so a column layout came back as a row of empty ``column``
containers and each column cost another call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion._common import (
    _LIST_CHILDREN_MAX_DEPTH,
    _READ_PAGE_MAX_CHARS,
    _READ_PAGE_WINDOW_MAX_CHARS,
)
from app.integrations.providers.notion.pages import _window_page_content
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

_PATCH = "app.integrations.providers.notion._common.safe_request"


# ── The pure windowing rule ──────────────────────────────────────────────


def test_a_short_page_is_returned_whole_and_unchanged():
    page = "# Strategy\n" + "\n".join(f"line {i}" for i in range(50))
    assert _window_page_content(page, title="Strategy") == page
    assert _window_page_content(page, title="Strategy", offset=0, max_chars=None) == page


def test_a_long_page_is_cut_at_a_line_boundary_and_says_where_to_continue():
    lines = [f"objective 1.{i}: " + "detail " * 30 for i in range(80)]
    page = "# Filigran Strategy FY2027\n" + "\n".join(lines)
    assert len(page) > 2 * _READ_PAGE_MAX_CHARS
    first = _window_page_content(page, title="Filigran Strategy FY2027")
    assert first.startswith("# Filigran Strategy FY2027\n")
    body, footer = first.rsplit("\n\n[", 1)
    assert footer.startswith("characters remain. Call get_notion_page again with offset=") is False
    assert "characters remain. Call get_notion_page again with offset=" in footer
    next_offset = int(footer.split("offset=")[1].split(" ")[0])
    assert next_offset <= _READ_PAGE_MAX_CHARS
    assert page[next_offset] == "\n", "the cut is at a line boundary"
    assert body == page[:next_offset]
    assert len(body) > _READ_PAGE_MAX_CHARS // 2

    second = _window_page_content(page, title="Filigran Strategy FY2027", offset=next_offset)
    assert second.startswith(f"# Filigran Strategy FY2027 (characters {next_offset:,}-")
    assert "objective 1.2" not in second or "objective 1.20" in second
    # Walking the offsets covers the whole page exactly once.
    covered = ""
    offset = 0
    for _ in range(20):
        chunk = _window_page_content(page, title="T", offset=offset)
        if offset == 0:
            text = chunk.split("\n\n[")[0]
        else:
            text = chunk.split("\n", 1)[1].split("\n\n[")[0] if "\n\n[" in chunk else chunk.split("\n", 1)[1]
        covered += text
        if "characters remain" not in chunk:
            break
        offset = int(chunk.rsplit("offset=", 1)[1].split(" ")[0])
    assert covered == page


def test_max_chars_widens_the_window_up_to_the_cap():
    page = "x\n" * 30_000
    wide = _window_page_content(page, title="T", max_chars=_READ_PAGE_WINDOW_MAX_CHARS)
    assert "characters remain" in wide
    assert len(wide.split("\n\n[")[0]) > _READ_PAGE_WINDOW_MAX_CHARS - 10
    over = _window_page_content(page, title="T", max_chars=10 ** 9)
    assert len(over.split("\n\n[")[0]) <= _READ_PAGE_WINDOW_MAX_CHARS
    # A nonsense window falls back to the default; a bad offset to 0.
    assert _window_page_content(page, title="T", max_chars="lots") == _window_page_content(page, title="T")
    assert _window_page_content(page, title="T", offset="soon") == _window_page_content(page, title="T")


def test_an_offset_past_the_end_is_a_tool_error_not_an_empty_page():
    """A refusal is flagged as the failure it is (the rule): the trace,
    the tool_done event and the per-tool error counters see it."""
    page = "# T\nshort"
    out = _window_page_content(page, title="T", offset=5_000)
    assert out.is_error is True
    assert "past the end of the page 'T'" in out.content and f"({len(page):,} characters)" in out.content


# ── Through the tool ─────────────────────────────────────────────────────


def _page_meta() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "page-1",
            "parent": {"type": "workspace"},
            "properties": {"title": {"type": "title", "title": [{"plain_text": "Strategy"}]}},
        },
    )


def _blocks(texts: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "id": f"b{i}",
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {"rich_text": [{"plain_text": t}]},
                }
                for i, t in enumerate(texts)
            ],
            "has_more": False,
        },
    )


@pytest.mark.asyncio
async def test_get_notion_page_accepts_offset_and_max_chars():
    executor = _SafeIntegrationExecutor()
    definitions = build_tools("dummy-token", executor)
    tool = next(t for t in definitions if t.name == "get_notion_page")
    props = tool.input_schema["properties"]
    assert props["offset"]["minimum"] == 0
    assert props["max_chars"]["maximum"] == _READ_PAGE_WINDOW_MAX_CHARS
    assert tool.input_schema["required"] == ["page_id"]
    assert "offset" in tool.description

    texts = [f"objective 1.{i}: " + "detail " * 40 for i in range(60)]
    with patch(_PATCH, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [(_page_meta(), None), (_blocks(texts), None)]
        first = await executor.execute_tool(
            ToolCall(id="c1", name="get_notion_page", arguments={"page_id": "page-1"})
        )
    assert not first.is_error
    assert first.content.startswith("# Strategy\n")
    assert "objective 1.0" in first.content
    assert "objective 1.59" not in first.content
    assert "Call get_notion_page again with offset=" in first.content
    next_offset = int(first.content.rsplit("offset=", 1)[1].split(" ")[0])

    with patch(_PATCH, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [(_page_meta(), None), (_blocks(texts), None)]
        second = await executor.execute_tool(
            ToolCall(
                id="c2",
                name="get_notion_page",
                arguments={"page_id": "page-1", "offset": next_offset, "max_chars": _READ_PAGE_WINDOW_MAX_CHARS},
            )
        )
    assert not second.is_error
    assert second.content.startswith(f"# Strategy (characters {next_offset:,}-")
    assert "objective 1.59" in second.content
    assert "characters remain" not in second.content, "the wide window reaches the end"


def _block(
    block_id: str, block_type: str, text: str | None = None, *, has_children: bool = False
) -> dict:
    payload: dict = {"id": block_id, "type": block_type, "has_children": has_children}
    if text is not None:
        payload[block_type] = {"rich_text": [{"plain_text": text}]}
    else:
        payload[block_type] = {}
    return payload


def _children(*blocks: dict) -> tuple[httpx.Response, None]:
    return httpx.Response(200, json={"results": list(blocks), "has_more": False}), None


@pytest.mark.asyncio
async def test_list_notion_block_children_depth_descends_into_column_layouts():
    executor = _SafeIntegrationExecutor()
    definitions = build_tools("dummy-token", executor)
    tool = next(t for t in definitions if t.name == "list_notion_block_children")
    assert tool.input_schema["properties"]["depth"]["maximum"] == _LIST_CHILDREN_MAX_DEPTH

    def _routes(url: str):
        if "/blocks/page-1/children" in url:
            return _children(
                _block("h1", "heading_1", "Objectives"),
                _block("cl", "column_list", has_children=True),
            )
        if "/blocks/cl/children" in url:
            return _children(
                _block("col-a", "column", has_children=True),
                _block("col-b", "column", has_children=True),
            )
        if "/blocks/col-a/children" in url:
            return _children(_block("pa", "paragraph", "Objective 1.5: expand NA"))
        if "/blocks/col-b/children" in url:
            return _children(_block("pb", "paragraph", "Objective 1.6: hire AEs"))
        raise AssertionError(url)

    async def _fake(client, method, url, *args, **kwargs):
        return _routes(url)

    # Depth 1: the old shape - a heading and an empty container with the marker.
    with patch(_PATCH, new=_fake):
        shallow = await executor.execute_tool(
            ToolCall(id="c1", name="list_notion_block_children", arguments={"block_id": "page-1"})
        )
    assert not shallow.is_error
    assert "1. [heading_1]   # Objectives" in shallow.content
    assert "2. [column_list] ▸ (empty)" in shallow.content
    assert "Objective 1.5" not in shallow.content

    # Depth 3: the columns' content, indented and numbered by path.
    with patch(_PATCH, new=_fake):
        deep = await executor.execute_tool(
            ToolCall(
                id="c2", name="list_notion_block_children", arguments={"block_id": "page-1", "depth": 3}
            )
        )
    assert not deep.is_error
    assert "2. [column_list]   (empty)" in deep.content, "descended into: no marker"
    assert "  2.1. [column]   (empty)" in deep.content
    assert "    2.1.1. [paragraph]   Objective 1.5: expand NA" in deep.content
    assert "    2.2.1. [paragraph]   Objective 1.6: hire AEs" in deep.content
    assert "   ID: pb" in deep.content

    # Depth 2 stops at the columns, which keep the marker for their children.
    with patch(_PATCH, new=_fake):
        mid = await executor.execute_tool(
            ToolCall(
                id="c3", name="list_notion_block_children", arguments={"block_id": "page-1", "depth": 2}
            )
        )
    assert "  2.1. [column] ▸ (empty)" in mid.content
    assert "Objective 1.5" not in mid.content
    # Out-of-range depths are clamped, not refused.
    with patch(_PATCH, new=_fake):
        clamped = await executor.execute_tool(
            ToolCall(
                id="c4", name="list_notion_block_children", arguments={"block_id": "page-1", "depth": 99}
            )
        )
    assert "Objective 1.6" in clamped.content


@pytest.mark.asyncio
async def test_list_notion_block_children_bounds_count_and_reports_a_failed_later_page():
    """A huge ``count`` never pages past the listing cap, and a pagination
    error after the first page is said out loud instead of a shorter listing
    that reads complete."""
    from app.integrations.providers.notion._common import _LIST_CHILDREN_MAX_BLOCKS

    executor = _SafeIntegrationExecutor()
    definitions = build_tools("dummy-token", executor)
    tool = next(t for t in definitions if t.name == "list_notion_block_children")
    assert tool.input_schema["properties"]["count"]["maximum"] == _LIST_CHILDREN_MAX_BLOCKS

    requests: list[dict] = []

    async def _paged(client, method, url, *args, **kwargs):
        params = kwargs.get("params") or {}
        requests.append(dict(params))
        if "start_cursor" in params:
            return None, "Error: Notion API rate limit (429)"
        return (
            httpx.Response(
                200,
                json={
                    "results": [_block(f"p{i}", "paragraph", f"para {i}") for i in range(100)],
                    "has_more": True,
                    "next_cursor": "cursor-2",
                },
            ),
            None,
        )

    with patch(_PATCH, new=_paged):
        result = await executor.execute_tool(
            ToolCall(
                id="c1",
                name="list_notion_block_children",
                arguments={"block_id": "page-1", "count": 10_000_000},
            )
        )
    assert not result.is_error
    assert requests[0]["page_size"] == "100", "the per-page size is Notion's own cap"
    assert len(requests) == 2, "the second page failed; no third request"
    assert "para 99" in result.content
    assert "[Listing incomplete - a later page of children failed: Error: Notion API rate limit (429)]" in result.content


@pytest.mark.asyncio
async def test_a_listing_cut_at_count_says_the_parent_has_more():
    """``count`` is per level: a 120-block page listed at the default 50 used
    to read as the whole page. The listing now says there is more."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    page = [_block(f"p{i}", "paragraph", f"para {i}") for i in range(120)]

    async def _paged(client, method, url, *args, **kwargs):
        params = kwargs.get("params") or {}
        start = int(params.get("start_cursor") or 0)
        size = int(params["page_size"])
        chunk = page[start : start + size]
        more = start + size < len(page)
        return (
            httpx.Response(
                200, json={"results": chunk, "has_more": more, "next_cursor": str(start + size) if more else None}
            ),
            None,
        )

    with patch(_PATCH, new=_paged):
        cut = await executor.execute_tool(
            ToolCall(id="c1", name="list_notion_block_children", arguments={"block_id": "page-1"})
        )
        whole = await executor.execute_tool(
            ToolCall(id="c2", name="list_notion_block_children", arguments={"block_id": "page-1", "count": 200})
        )
    assert "50. [paragraph]" in cut.content and "51. [paragraph]" not in cut.content
    assert cut.content.endswith(
        "[More children than the 50 listed per level - raise count (max 400) or pass a block's ID as block_id to list the rest.]"
    )
    assert "120. [paragraph]" in whole.content and "More children" not in whole.content


@pytest.mark.asyncio
async def test_a_listing_that_fails_outright_is_a_tool_error():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    async def _down(client, method, url, *args, **kwargs):
        return None, "Error: Notion API returned 503"

    with patch(_PATCH, new=_down):
        result = await executor.execute_tool(
            ToolCall(id="c1", name="list_notion_block_children", arguments={"block_id": "page-1", "depth": 3})
        )
    assert result.is_error is True
    assert "503" in result.content


@pytest.mark.asyncio
async def test_a_deep_listing_stops_descending_at_the_fetch_budget():
    """Every container a deep listing descends into is one more paginated
    Notion request: the fan-out of one call is bounded, and the containers
    past the budget keep their marker with a line saying why - never
    hundreds of requests against a 3 req/s limit."""
    from app.integrations.providers.notion._common import _LIST_CHILDREN_MAX_FETCHES

    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    fetched: list[str] = []

    async def _wide(client, method, url, *args, **kwargs):
        parent = url.split("/blocks/", 1)[1].split("/", 1)[0]
        fetched.append(parent)
        # Every block is a container with children: an unbounded walk would
        # fetch every one of them, level after level (5 + 25 + 125 containers
        # at depth 4 - far fewer blocks than the 400-block cap).
        return _children(*[_block(f"{parent}-{i}", "toggle", f"t {i}", has_children=True) for i in range(5)])

    with patch(_PATCH, new=_wide):
        result = await executor.execute_tool(
            ToolCall(id="c1", name="list_notion_block_children", arguments={"block_id": "page-1", "depth": 4})
        )
    assert not result.is_error
    assert len(fetched) == _LIST_CHILDREN_MAX_FETCHES, "one fetch per container, stopped at the budget"
    assert f"[Stopped descending after {_LIST_CHILDREN_MAX_FETCHES} containers" in result.content
    assert "▸" in result.content, "the containers not descended into keep their marker"
