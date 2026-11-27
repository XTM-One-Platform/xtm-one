"""``get_opencti_container_full`` pages a container past 1,000 objects.

A container larger than one page is read whole by cursor (``after``), the
closing line never asks for a ``limit`` above the page maximum, ``types``
narrows the objects and ``compact`` leaves their descriptions out (#2684).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_TOTAL = 2123


def _objects(start: int, count: int) -> list[dict]:
    return [
        {
            "id": f"ind-{i}",
            "entity_type": "Indicator",
            "name": f"Indicator {i}",
            "description": f"about indicator {i}",
        }
        for i in range(start, start + count)
    ]


async def _call(arguments: dict, total: int = _TOTAL) -> tuple[object, list[dict]]:
    """Run the tool on a fake OpenCTI holding ``total`` objects."""
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)
    sent: list[dict] = []

    async def _fake(self, method, url, **kwargs):
        body = kwargs.get("json") or json.loads(kwargs.get("content") or "{}")
        variables = body.get("variables") or {}
        sent.append(variables)
        start = int(variables.get("after") or 0)
        count = max(0, min(variables["first"], total - start))
        end = start + count
        container = {
            "id": "report-1",
            "entity_type": "Report",
            "name": "Large report",
            "objects": {
                "edges": [{"node": node} for node in _objects(start, count)],
                "pageInfo": {"endCursor": str(end), "hasNextPage": end < total, "globalCount": total},
            },
        }
        answer = {"data": {"stixDomainObject": container}}
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(
            ToolCall(id="1", name="get_opencti_container_full", arguments={"container_id": "report-1", **arguments})
        )
    return result, sent


@pytest.mark.asyncio
async def test_a_container_over_1000_objects_is_read_whole_by_cursor():
    seen: list[str] = []
    after = None
    pages = 0
    while True:
        arguments = {"limit": 1000}
        if after:
            arguments["after"] = after
        result, sent = await _call(arguments)
        pages += 1
        assert sent[-1]["first"] == 1000
        assert sent[-1].get("after") == after
        seen.extend(entity["id"] for entity in result.structured["entities"])
        after = result.structured["next_cursor"]
        assert result.structured["has_more"] is (after is not None)
        if after is None:
            break
        assert f'after="{after}"' in result.content
    assert pages == 3
    assert len(seen) == _TOTAL
    assert len(set(seen)) == _TOTAL
    assert "Next page" not in result.content
    assert "the last page of 2123" in result.content


@pytest.mark.asyncio
async def test_at_the_page_maximum_the_hint_never_asks_for_a_larger_limit():
    result, _ = await _call({"limit": 5000})
    assert "Increase limit" not in result.content
    assert "limit=" not in result.content.rsplit("\n", 1)[-1]
    assert 'after="1000"' in result.content


@pytest.mark.asyncio
async def test_under_the_page_maximum_the_hint_names_the_cursor_and_the_maximum():
    result, _ = await _call({"limit": 300})
    last = result.content.rsplit("\n", 1)[-1]
    assert "Increase limit" not in last
    assert 'after="300"' in last
    assert "limit=1000" in last


@pytest.mark.asyncio
async def test_types_are_passed_to_the_objects_connection_and_named():
    result, sent = await _call({"types": ["Indicator"], "limit": 10}, total=5)
    assert sent[-1]["types"] == ["Indicator"]
    assert "Total objects of type Indicator: 5" in result.content


@pytest.mark.asyncio
async def test_compact_leaves_the_descriptions_out():
    result, _ = await _call({"compact": True, "limit": 10}, total=3)
    assert "Description:" not in result.content
    assert "Indicator 2 (ID: ind-2)" in result.content
    full, _ = await _call({"limit": 10}, total=3)
    assert "Description: about indicator 2" in full.content


@pytest.mark.asyncio
async def test_a_call_with_the_id_alone_answers_as_before():
    result, sent = await _call({}, total=3)
    assert sent[-1]["first"] == 200
    assert sent[-1].get("after") is None
    assert sent[-1].get("types") is None
    assert result.content.rstrip().endswith("Total objects: 3")
    assert result.structured["has_more"] is False
