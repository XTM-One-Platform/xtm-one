"""Tests for the read-only SharePoint list item field tools of the Microsoft integration.

- ``get_microsoft_list_item_fields`` returns a file's custom column values
  (Graph ``.../listItem?$expand=fields``), by site + path or by URL.
- ``list_microsoft_list_items`` lists a library's items with their fields,
  filters on one column value across pages and hands back a page_token.
- A refused read (401/403) names the Sites.Read.All permission.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

_GRAPH = "https://graph.microsoft.com/v1.0"


class _StubResponse:
    def __init__(self, payload: dict[str, Any] | None = None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._payload


class _Router:
    """Routes ``safe_request`` calls to canned responses by URL substring, in order."""

    def __init__(self, routes: list[tuple[str, tuple[Any, Any]]]):
        self._routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, provider, op, **kwargs):
        self.calls.append({"method": method, "url": url, "op": op, **kwargs})
        for substring, result in self._routes:
            if substring in url:
                return result
        raise AssertionError(f"No stub route for {method.upper()} {url}")


async def _run(monkeypatch, tool_name, routes, **arguments):
    router = _Router(routes)
    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", router)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=tool_name, arguments=arguments))
    return router, result


def _item(item_id: str, name: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": item_id,
        "webUrl": f"https://contoso.sharepoint.com/sites/lib/{name}",
        "fields": {"@odata.etag": '"1"', "FileLeafRef": name, **fields},
    }


def test_tools_are_registered_read_only():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(token="dummy", executor=executor)}
    for name in ("get_microsoft_list_item_fields", "list_microsoft_list_items"):
        assert name in tools
        assert "Read-only" in tools[name].description
        assert "Sites.Read.All" in tools[name].description
    # Found by a tool search on the words an agent uses.
    text = tools["get_microsoft_list_item_fields"].description.lower()
    for word in ("sharepoint", "list item", "fields", "columns"):
        assert word in text


@pytest.mark.asyncio
async def test_item_fields_returns_custom_choice_column(monkeypatch):
    router, result = await _run(
        monkeypatch,
        "get_microsoft_list_item_fields",
        routes=[("/listItem", (_StubResponse(_item("7", "a.docx", Region="EMEA", Owner="x")), None))],
        site_id="SITE1",
        item_path="Folder/a.docx",
    )
    assert not result.is_error
    assert "Region: EMEA" in result.content
    assert "Owner: x" in result.content
    assert "@odata" not in result.content
    call = router.calls[0]
    assert call["url"] == f"{_GRAPH}/sites/SITE1/drive/root:/Folder/a.docx:/listItem"
    assert call["params"]["$expand"] == "fields"


@pytest.mark.asyncio
async def test_item_fields_in_named_library_and_selected_columns(monkeypatch):
    router, result = await _run(
        monkeypatch,
        "get_microsoft_list_item_fields",
        routes=[("/listItem", (_StubResponse(_item("7", "a.pdf", Region="APAC")), None))],
        site_id="SITE1",
        item_path="a.pdf",
        library="My Library",
        columns="region, Missing",
    )
    assert "/sites/SITE1/lists/My%20Library/drive/root:/a.pdf:/listItem" in router.calls[0]["url"]
    assert "Region: APAC" in result.content
    assert "Missing: (not set on this item)" in result.content
    assert "FileLeafRef" not in result.content.split("Fields:")[1]


@pytest.mark.asyncio
async def test_item_fields_by_url_uses_shares(monkeypatch):
    router, result = await _run(
        monkeypatch,
        "get_microsoft_list_item_fields",
        routes=[("/shares/", (_StubResponse(_item("9", "b.docx", Region="NA")), None))],
        url="https://contoso.sharepoint.com/:w:/s/lib/AABB",
    )
    assert "/driveItem/listItem" in router.calls[0]["url"]
    assert "Region: NA" in result.content


@pytest.mark.asyncio
async def test_item_fields_needs_a_file(monkeypatch):
    router, result = await _run(monkeypatch, "get_microsoft_list_item_fields", routes=[], site_id="SITE1")
    assert "item_path" in result.content
    assert router.calls == []


@pytest.mark.asyncio
async def test_item_fields_403_names_the_scope(monkeypatch):
    _router, result = await _run(
        monkeypatch,
        "get_microsoft_list_item_fields",
        routes=[("/listItem", (_StubResponse(status_code=403), "Error: access denied (403)."))],
        site_id="SITE1",
        item_path="a.docx",
    )
    assert "Sites.Read.All" in result.content
    assert "refused" in result.content


@pytest.mark.asyncio
async def test_list_items_pages_and_filters(monkeypatch):
    page_2 = f"{_GRAPH}/sites/SITE1/lists/Docs/items?$skiptoken=P2"
    first = _StubResponse(
        {
            "value": [_item("1", "a.docx", Region="EMEA"), _item("2", "b.docx", Region="APAC")],
            "@odata.nextLink": page_2,
        }
    )
    second = _StubResponse({"value": [_item("3", "c.docx", Region=["emea", "NA"]), _item("4", "d.docx")]})
    router, result = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[("skiptoken=P2", (second, None)), ("/lists/Docs/items", (first, None))],
        site_id="SITE1",
        library="Docs",
        filter_column="region",
        filter_value="EMEA",
    )
    assert not result.is_error
    assert len(router.calls) == 2
    assert router.calls[0]["params"]["$expand"] == "fields"
    assert "a.docx" in result.content and "c.docx" in result.content
    assert "b.docx" not in result.content and "d.docx" not in result.content
    assert "2 item(s)" in result.content and "4 scanned" in result.content
    assert "End of the list." in result.content


@pytest.mark.asyncio
async def test_list_items_filter_caps_at_count_and_resumes_in_page(monkeypatch):
    page = _StubResponse(
        {
            "value": [
                _item("1", "a.docx", Region="EMEA"),
                _item("2", "b.docx", Region="APAC"),
                _item("3", "c.docx", Region="EMEA"),
                _item("4", "d.docx", Region="EMEA"),
            ]
        }
    )
    router, result = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[("/lists/Docs/items", (page, None))],
        site_id="SITE1",
        library="Docs",
        filter_column="Region",
        filter_value="EMEA",
        count=2,
    )
    assert "2 item(s)" in result.content
    assert "a.docx" in result.content and "c.docx" in result.content
    assert "d.docx" not in result.content
    assert "#skip=3" in result.content
    token = result.content.split('page_token="')[1].split('"')[0]
    assert token.startswith(f"{_GRAPH}/sites/SITE1/lists/Docs/items?")

    router, result = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[("/lists/Docs/items", (page, None))],
        site_id="SITE1",
        library="Docs",
        filter_column="Region",
        filter_value="EMEA",
        count=2,
        page_token=token,
    )
    assert "#skip" not in router.calls[0]["url"]
    assert "1 item(s)" in result.content and "d.docx" in result.content
    assert "a.docx" not in result.content and "c.docx" not in result.content
    assert "End of the list." in result.content


@pytest.mark.asyncio
async def test_list_items_hands_back_page_token_and_follows_it(monkeypatch):
    next_link = f"{_GRAPH}/sites/SITE1/lists/Docs/items?$skiptoken=P2"
    first = _StubResponse({"value": [_item("1", "a.docx")], "@odata.nextLink": next_link})
    router, result = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[("/lists/Docs/items", (first, None))],
        site_id="SITE1",
        library="Docs",
        count=1,
    )
    assert router.calls[0]["params"]["$top"] == "1"
    assert f'page_token="{next_link}"' in result.content

    second = _StubResponse({"value": [_item("2", "b.docx")]})
    router, result = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[("skiptoken=P2", (second, None))],
        site_id="SITE1",
        library="Docs",
        page_token=next_link,
    )
    assert router.calls[0]["url"] == next_link
    assert "b.docx" in result.content


@pytest.mark.asyncio
async def test_list_items_refuses_foreign_page_token_and_half_filter(monkeypatch):
    router, result = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[],
        site_id="SITE1",
        library="Docs",
        page_token="https://evil.example/x",
    )
    assert "page_token" in result.content
    router2, result2 = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[],
        site_id="SITE1",
        library="Docs",
        filter_column="Region",
    )
    assert "go together" in result2.content
    assert router.calls == [] and router2.calls == []


@pytest.mark.asyncio
async def test_list_items_column_absent_matches_nothing(monkeypatch):
    page = _StubResponse({"value": [_item("1", "a.docx", Region="EMEA")]})
    _router, result = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[("/lists/Docs/items", (page, None))],
        site_id="SITE1",
        library="Docs",
        filter_column="Country",
        filter_value="FR",
    )
    assert "No matching item." in result.content


@pytest.mark.asyncio
async def test_list_items_403_names_the_scope(monkeypatch):
    _router, result = await _run(
        monkeypatch,
        "list_microsoft_list_items",
        routes=[("/lists/Docs/items", (_StubResponse(status_code=403), "Error: access denied (403)."))],
        site_id="SITE1",
        library="Docs",
    )
    assert "Sites.Read.All" in result.content
