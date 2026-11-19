"""OpenCTI listing tools resolve ``order_by`` against OpenCTI's *Ordering enums.

OpenCTI refuses an ``orderBy`` outside its GraphQL enum with a raw
BAD_USER_INPUT error. The listing tools map common aliases (``value`` on
observables) and refuse anything else before the call, listing the valid keys.

A keyword search with no ``order_by`` ranks its matches by relevance
(``_score``), as OpenCTI's own search page does: ordering thousands of keyword
matches by ``created_at`` returns the newest, not the one the keyword names.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

LISTS = {
    "search_opencti": {"data": {"stixCoreObjects": {"edges": [], "pageInfo": {"globalCount": 0}}}},
    "list_opencti_observables": {"data": {"stixCyberObservables": {"edges": [], "pageInfo": {"globalCount": 0}}}},
    "list_opencti_entities": {"data": {"stixDomainObjects": {"edges": [], "pageInfo": {"globalCount": 0}}}},
}


async def _call(tool: str, arguments: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)
    sent: list[dict[str, Any]] = []

    async def _fake(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=LISTS[tool], request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(ToolCall(id="1", name=tool, arguments=arguments))
    return str(result.content), sent


def _order_by(sent: list[dict[str, Any]]) -> list[str]:
    return [body.get("variables", {}).get("orderBy") for body in sent if "orderBy" in body.get("variables", {})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "given", "expected"),
    [
        ("list_opencti_observables", "value", "observable_value"),
        ("list_opencti_observables", "Value", "observable_value"),
        ("list_opencti_observables", "observable_value", "observable_value"),
        ("list_opencti_observables", "updated", "updated_at"),
        ("list_opencti_entities", "title", "name"),
        ("list_opencti_entities", "author", "createdBy"),
        ("list_opencti_entities", "confidence", "confidence"),
    ],
)
async def test_an_alias_or_a_valid_key_is_sent_as_the_enum_key(tool: str, given: str, expected: str) -> None:
    _, sent = await _call(tool, {"order_by": given})
    assert _order_by(sent) == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_opencti_observables", "list_opencti_entities"])
async def test_the_default_stays_created_at(tool: str) -> None:
    _, sent = await _call(tool, {})
    assert _order_by(sent) == ["created_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "enum_name", "a_valid_key"),
    [
        ("list_opencti_observables", "StixCyberObservablesOrdering", "observable_value"),
        ("list_opencti_entities", "StixDomainObjectsOrdering", "published"),
    ],
)
async def test_an_invalid_value_is_refused_before_the_call_with_the_valid_keys(
    tool: str, enum_name: str, a_valid_key: str
) -> None:
    content, sent = await _call(tool, {"order_by": "colour"})
    assert sent == []
    assert enum_name in content
    assert "Valid values:" in content
    assert a_valid_key in content
    assert "BAD_USER_INPUT" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["search_opencti", "list_opencti_observables", "list_opencti_entities"])
async def test_a_keyword_search_ranks_by_relevance(tool: str) -> None:
    _, sent = await _call(tool, {"search": "an incident keyword"})
    assert _order_by(sent) == ["_score"]


@pytest.mark.asyncio
async def test_search_opencti_without_a_keyword_lists_the_newest() -> None:
    _, sent = await _call("search_opencti", {"search": "  "})
    assert _order_by(sent) == ["created_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_opencti_observables", "list_opencti_entities"])
async def test_an_order_the_caller_names_wins_over_relevance(tool: str) -> None:
    _, sent = await _call(tool, {"search": "an incident keyword", "order_by": "created_at"})
    assert _order_by(sent) == ["created_at"]
