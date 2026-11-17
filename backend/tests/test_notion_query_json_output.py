"""``query_notion_database(output_format="json")`` answers with rows as data.

A flow or a script that syncs a database (a roadmap kept in step with a GitHub
Project) reads property values, not a summary for a reader: the text format
keeps only the start of a date range, joins values with ``|`` and fetches page
content. The JSON format keeps every value whole and fetches nothing else.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion.formatting import _property_value_json
from app.services.integration_tools import _SafeIntegrationExecutor

_PROPERTIES = {
    "EPIC name": {"type": "title", "title": [{"plain_text": "MSSP "}, {"plain_text": "Multi-Tenant | A"}]},
    "Description": {"type": "rich_text", "rich_text": []},
    "Size": {"type": "number", "number": 3},
    "Quarter": {"type": "select", "select": {"name": "Q1 2027"}},
    "Edition Type": {"type": "select", "select": None},
    "Status": {"type": "status", "status": {"name": "In progress"}},
    "Tags": {"type": "multi_select", "multi_select": [{"name": "The Big Move"}, {"name": "B"}]},
    "Dates": {"type": "date", "date": {"start": "2027-01-04", "end": "2027-02-26", "time_zone": None}},
    "Done": {"type": "checkbox", "checkbox": False},
    "Planning": {"type": "url", "url": "https://github.com/o/r/issues/1"},
    "Owner": {"type": "people", "people": [{"id": "u1", "name": "Ann"}, {"id": "u2"}]},
    "Blocked By": {"type": "relation", "relation": [{"id": "r1"}, {"id": "r2"}]},
    "Due": {"type": "formula", "formula": {"type": "date", "date": {"start": "2027-03-01"}}},
    "Score": {"type": "formula", "formula": {"type": "number", "number": 0.5}},
    "Child status": {
        "type": "rollup",
        "rollup": {"type": "array", "array": [{"type": "status", "status": {"name": "Done"}}]},
    },
    "Ref": {"type": "unique_id", "unique_id": {"prefix": "EP", "number": 7}},
    "Seq": {"type": "unique_id", "unique_id": {"prefix": None, "number": 12}},
    "Created by": {"type": "created_by", "created_by": {"id": "u3", "name": "Bo"}},
    "Edited": {"type": "last_edited_time", "last_edited_time": "2026-11-05T08:00:00.000Z"},
}

_EXPECTED_PROPERTIES = {
    "EPIC name": "MSSP Multi-Tenant | A",
    "Description": "",
    "Size": 3,
    "Quarter": "Q1 2027",
    "Edition Type": None,
    "Status": "In progress",
    "Tags": ["The Big Move", "B"],
    "Dates": {"start": "2027-01-04", "end": "2027-02-26", "time_zone": None},
    "Done": False,
    "Planning": "https://github.com/o/r/issues/1",
    "Owner": [{"id": "u1", "name": "Ann"}, {"id": "u2", "name": None}],
    "Blocked By": ["r1", "r2"],
    "Due": {"start": "2027-03-01", "end": None, "time_zone": None},
    "Score": 0.5,
    "Child status": ["Done"],
    "Ref": "EP-7",
    "Seq": 12,
    "Created by": {"id": "u3", "name": "Bo"},
    "Edited": "2026-11-05T08:00:00.000Z",
}


def _page(page_id: str, properties: dict | None = None) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "created_time": "2026-11-01T00:00:00.000Z",
        "last_edited_time": "2026-11-05T08:00:00.000Z",
        "in_trash": False,
        "properties": properties if properties is not None else {},
    }


def _handler():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    return executor._handlers["query_notion_database"]


def _fake_notion(pages: list[list[dict]], calls: list[tuple[str, str]]):
    """Notion answering the query with *pages*, one list per result page."""

    async def fake_safe_request(
        _client: Any, method: str, url: str, _provider: str, _action: str, **kwargs: Any
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append((method, url))
        if method == "get" and "/databases/" in url:
            return httpx.Response(200, json={"data_sources": [{"id": "ds-1"}]}), None
        if method == "post" and url.endswith("/data_sources/ds-1/query"):
            index = int((kwargs.get("json") or {}).get("start_cursor") or 0)
            more = index + 1 < len(pages)
            return httpx.Response(
                200,
                json={
                    "results": pages[index],
                    "has_more": more,
                    "next_cursor": str(index + 1) if more else None,
                },
            ), None
        raise AssertionError(f"Unexpected Notion call: {method} {url}")

    return fake_safe_request


@pytest.mark.parametrize(("name", "prop"), list(_PROPERTIES.items()))
def test_each_property_type_keeps_its_whole_value(name: str, prop: dict) -> None:
    assert _property_value_json(prop) == _EXPECTED_PROPERTIES[name]


@pytest.mark.asyncio
async def test_json_rows_carry_every_property_and_fetch_no_page_content() -> None:
    calls: list[tuple[str, str]] = []
    fake = _fake_notion([[_page("p1", _PROPERTIES)]], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        result = await _handler()(database_id="db-1", output_format="json")

    assert json.loads(result) == {
        "results": [
            {
                "id": "p1",
                "url": "https://www.notion.so/p1",
                "created_time": "2026-11-01T00:00:00.000Z",
                "last_edited_time": "2026-11-05T08:00:00.000Z",
                "in_trash": False,
                "properties": _EXPECTED_PROPERTIES,
                "truncated": [],
            }
        ],
        "has_more": False,
        "note": None,
    }
    # One row is below the text format's expansion threshold: JSON reads no page.
    assert calls == [
        ("get", "https://api.notion.com/v1/databases/db-1"),
        ("post", "https://api.notion.com/v1/data_sources/ds-1/query"),
    ]


@pytest.mark.asyncio
async def test_json_follows_pages_up_to_count_and_says_more_remain() -> None:
    calls: list[tuple[str, str]] = []
    fake = _fake_notion([[_page("p1"), _page("p2")], [_page("p3"), _page("p4")]], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        result = json.loads(await _handler()(database_id="db-1", output_format="json", count=3))

    assert [row["id"] for row in result["results"]] == ["p1", "p2", "p3"]
    assert result["has_more"] is True


@pytest.mark.asyncio
async def test_json_reads_every_page_when_count_covers_them() -> None:
    calls: list[tuple[str, str]] = []
    fake = _fake_notion([[_page("p1")], [_page("p2")]], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        result = json.loads(await _handler()(database_id="db-1", output_format="json", count=10))

    assert [row["id"] for row in result["results"]] == ["p1", "p2"]
    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_json_answers_an_empty_database_with_an_empty_list() -> None:
    calls: list[tuple[str, str]] = []
    fake = _fake_notion([[]], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        result = await _handler()(database_id="db-1", output_format="JSON")

    assert json.loads(result) == {"results": [], "has_more": False, "note": None}


@pytest.mark.asyncio
async def test_a_property_notion_cut_at_25_references_is_named() -> None:
    """Notion sends 25 references of a relation and flags the rest: the row
    says so instead of passing the partial list off as the whole one."""
    relation = {"type": "relation", "relation": [{"id": f"r{i}"} for i in range(25)], "has_more": True}
    people = {"type": "people", "people": [{"id": "u1", "name": "Ann"}], "has_more": False}
    calls: list[tuple[str, str]] = []
    fake = _fake_notion([[_page("p1", {"Blocked By": relation, "Owner": people})]], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        row = json.loads(await _handler()(database_id="db-1", output_format="json"))["results"][0]

    assert row["truncated"] == ["Blocked By"]
    assert row["properties"]["Blocked By"] == [f"r{i}" for i in range(25)]


@pytest.mark.asyncio
async def test_a_failure_answers_with_the_error_text_a_flow_sees_as_an_error() -> None:
    """JSON mode keeps the shared failure contract: an ``Error: ...`` text, which
    the integration executor flags so a flow takes its error path."""
    from app.integrations.http import format_http_error
    from app.tools.base import is_error_shaped_result

    async def fake_safe_request(_c: Any, method: str, url: str, _p: str, _a: str, **kwargs: Any):
        if method == "get" and "/databases/" in url:
            return httpx.Response(200, json={"data_sources": [{"id": "ds-1"}]}), None
        if method == "post":
            resp = httpx.Response(403, json={"message": "no access"})
            return resp, format_http_error(resp, "Notion", "query database")
        raise AssertionError(f"Unexpected Notion call: {method} {url}")

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await _handler()(database_id="db-1", output_format="json")

    assert result.startswith("Error: Notion access denied while trying to query database")
    assert is_error_shaped_result(result) is True


@pytest.mark.asyncio
async def test_a_withheld_name_keeps_the_json_readable() -> None:
    """Notion withholding a name adds a note for a reader to a text answer;
    after JSON it would break the parse, so the row says it with a null name."""
    unnamed = {"object": "user", "id": "u9"}
    mention = {"type": "mention", "plain_text": "@Anonymous", "mention": {"type": "user", "user": unnamed}}
    properties = {
        "Owner": {"type": "people", "people": [unnamed]},
        "Notes": {"type": "rich_text", "rich_text": [mention]},
    }
    calls: list[tuple[str, str]] = []
    fake = _fake_notion([[_page("p1", properties)]], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        result = await _handler()(database_id="db-1", output_format="json")

    assert json.loads(result)["results"][0]["properties"] == {
        "Owner": [{"id": "u9", "name": None}],
        "Notes": "@Anonymous",
    }


@pytest.mark.asyncio
async def test_the_text_format_still_notes_a_withheld_name() -> None:
    unnamed = {"object": "user", "id": "u9"}
    rows = [_page(f"p{i}", {"Owner": {"type": "people", "people": [unnamed]}}) for i in range(21)]
    calls: list[tuple[str, str]] = []
    fake = _fake_notion([rows], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        result = await _handler()(database_id="db-1", count=21)

    assert "Owner: @Anonymous" in result
    assert result.count("[Notion withheld the name of 1 person in this result") == 1


@pytest.mark.asyncio
async def test_an_unknown_format_is_refused_before_any_call() -> None:
    calls: list[tuple[str, str]] = []
    fake = _fake_notion([[_page("p1")]], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        result = await _handler()(database_id="db-1", output_format="yaml")

    assert result == "Error: output_format must be one of text, json, got 'yaml'."
    assert calls == []


@pytest.mark.asyncio
async def test_the_default_text_format_is_unchanged() -> None:
    calls: list[tuple[str, str]] = []
    rows = [_page(f"p{i}", {"Name": {"type": "title", "title": [{"plain_text": f"Row {i}"}]}}) for i in range(21)]
    fake = _fake_notion([rows], calls)
    with patch("app.integrations.providers.notion._common.safe_request", new=fake):
        result = await _handler()(database_id="db-1", count=21)

    assert result.startswith("• Row 0\n  ID: p0\n  URL: https://www.notion.so/p0\n  Name: Row 0")
    assert result.count("• Row ") == 21


def test_the_tool_declares_the_output_format() -> None:
    executor = _SafeIntegrationExecutor()
    definition = next(d for d in build_tools("fake-token", executor) if d.name == "query_notion_database")
    prop = definition.input_schema["properties"]["output_format"]

    assert prop["enum"] == ["text", "json"]
    assert prop["default"] == "text"
