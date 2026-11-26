"""SharePoint writes (#2187): list item fields written and read back, and a file
moved between two document libraries of one site with its columns kept.

A fake Microsoft Graph is served through ``httpx.MockTransport`` (documented
response shapes, no network, no token).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.integrations import file_delivery
from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import drive_ops
from app.integrations.providers.microsoft.sharepoint import parse_field_values, unwritten_fields
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

SITE = "/v1.0/sites/SITE1"
ITEM = f"{SITE}/drive/root:/Contracts/A.docx:"
SRC = "/v1.0/drives/D1"
DST = "/v1.0/drives/D2"


class FakeGraph:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, response: Any) -> None:
        self.routes.insert(0, (method.upper(), path, response))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, path, response in self.routes:
            if request.method == method and request.url.path == path:
                answer: httpx.Response = response(request) if callable(response) else response
                return answer
        raise AssertionError(f"no fake route for {request.method} {request.url}")

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method]


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    fake = FakeGraph()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _public(url: str) -> str:
        return url

    monkeypatch.setattr(file_delivery, "validate_outbound_url_async", _public)
    monkeypatch.setattr(drive_ops, "_COPY_POLL_SECONDS", 0.0)
    return fake


def _json(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


async def _call(name: str, **arguments: Any) -> ToolResult:
    executor = CallableToolExecutor()
    ms.build_tools("tok", executor)
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


# ── field values ──────────────────────────────────────────────────────────


def test_parse_field_values_takes_an_object_or_its_json() -> None:
    assert parse_field_values({"Status": "Signed"}) == ({"Status": "Signed"}, None)
    assert parse_field_values('{"Status": null}') == ({"Status": None}, None)
    assert parse_field_values("{}")[1]
    assert parse_field_values("not json")[1]


def test_unwritten_fields_compares_dates_numbers_and_clears() -> None:
    stored = {"Renewal": "2027-01-31T00:00:00Z", "Amount": 12, "Notes": None, "Status": "Draft"}
    written = {"Renewal": "2027-01-31", "Amount": 12.0, "Notes": None, "Status": "Signed"}
    assert unwritten_fields(written, stored) == ["Status"]


# ── update_microsoft_list_item_fields ─────────────────────────────────────


@pytest.mark.asyncio
async def test_field_write_patches_fields_and_reads_them_back(graph: FakeGraph) -> None:
    state: dict[str, Any] = {"FileLeafRef": "A.docx", "Status": "Draft", "Renewal": None}

    def _patch(request: httpx.Request) -> httpx.Response:
        state.update(json.loads(request.content))
        return httpx.Response(200, json=state)

    graph.on("PATCH", f"{ITEM}/listItem/fields", _patch)
    graph.on("GET", f"{ITEM}/listItem", lambda _r: httpx.Response(200, json={"id": "7", "fields": dict(state)}))

    result = await _call(
        "update_microsoft_list_item_fields",
        site_id="SITE1",
        item_path="Contracts/A.docx",
        fields={"Status": "Signed", "Renewal": "2027-01-31T00:00:00Z"},
    )

    assert not result.is_error, result.content
    assert result.content.startswith("Wrote 2 field(s); read back")
    assert "Status: Signed" in result.content
    assert json.loads(graph.calls("PATCH")[0].content) == {"Status": "Signed", "Renewal": "2027-01-31T00:00:00Z"}


@pytest.mark.asyncio
async def test_field_write_reports_a_value_graph_did_not_keep(graph: FakeGraph) -> None:
    graph.on("PATCH", f"{ITEM}/listItem/fields", _json({}))
    graph.on("GET", f"{ITEM}/listItem", _json({"id": "7", "fields": {"Status": "Draft"}}))

    result = await _call(
        "update_microsoft_list_item_fields", site_id="SITE1", item_path="Contracts/A.docx", fields={"Status": "Signed"}
    )

    assert "read back with another value: Status" in result.content


@pytest.mark.asyncio
async def test_field_write_refused_names_the_write_permission(graph: FakeGraph) -> None:
    graph.on("PATCH", f"{ITEM}/listItem/fields", _json({"error": {"message": "Access denied"}}, 403))

    result = await _call(
        "update_microsoft_list_item_fields", site_id="SITE1", item_path="Contracts/A.docx", fields={"Status": "x"}
    )

    assert "Sites.ReadWrite.All" in result.content


# ── move between libraries ────────────────────────────────────────────────


def _source() -> dict[str, Any]:
    return {
        "id": "I1",
        "name": "A.docx",
        "file": {"mimeType": "x"},
        "webUrl": "https://contoso.sharepoint.com/Contracts/A.docx",
        "parentReference": {"driveId": "D1", "id": "P1", "siteId": "SITE1", "path": "/drives/D1/root:/Contracts"},
    }


@pytest.mark.asyncio
async def test_cross_library_move_copies_to_the_target_drive_keeps_columns_and_deletes(graph: FakeGraph) -> None:
    copy_fields: dict[str, Any] = {"FileLeafRef": "A.docx"}
    copied = {"done": False}
    graph.on("GET", f"{SITE}/drive/root:/Contracts/A.docx", _json(_source()))
    graph.on("GET", f"{SITE}/lists/Archive/drive", _json({"id": "D2"}))
    graph.on("GET", f"{DST}/root:/2026", _json({"id": "F2", "folder": {}}))
    graph.on(
        "GET",
        f"{DST}/items/F2:/A.docx",
        lambda _r: (
            httpx.Response(200, json={"id": "C1", "name": "A.docx"})
            if copied["done"]
            else httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        ),
    )
    graph.on("GET", f"{SRC}/items/I1/listItem", _json({"id": "1", "fields": {"Status": "Expired", "Owner": "Legal"}}))
    graph.on(
        "GET",
        f"{SITE}/lists/Archive/columns",
        _json({"value": [{"name": "Status"}, {"name": "Owner"}, {"name": "Modified", "readOnly": True}]}),
    )

    def _copy(request: httpx.Request) -> httpx.Response:
        copied["done"] = True
        return httpx.Response(202, headers={"Location": "https://monitor"})

    def _patch(request: httpx.Request) -> httpx.Response:
        copy_fields.update(json.loads(request.content))
        return httpx.Response(200, json=copy_fields)

    graph.on("POST", f"{SRC}/items/I1/copy", _copy)
    graph.on("PATCH", f"{DST}/items/C1/listItem/fields", _patch)
    graph.on("GET", f"{DST}/items/C1/listItem", lambda _r: httpx.Response(200, json={"fields": dict(copy_fields)}))
    graph.on("DELETE", f"{SRC}/items/I1", httpx.Response(204))
    graph.on("GET", f"{DST}/items/C1", _json({**_source(), "id": "C1", "parentReference": {"driveId": "D2"}}))

    result = await _call(
        "move_microsoft_drive_file", site_id="SITE1", item_path="Contracts/A.docx", folder="2026", library="Archive"
    )

    assert not result.is_error, result.content
    assert "Moved the file to library 'Archive'" in result.content
    assert "2 column(s) carried over" in result.content
    body = json.loads(graph.calls("POST")[0].content)
    assert body["parentReference"] == {"driveId": "D2", "id": "F2"}
    assert json.loads(graph.calls("PATCH")[0].content) == {"Owner": "Legal", "Status": "Expired"}
    assert len(graph.calls("DELETE")) == 1


@pytest.mark.asyncio
async def test_cross_library_move_keeps_the_source_when_columns_are_lost(graph: FakeGraph) -> None:
    graph.on("GET", f"{SITE}/drive/root:/Contracts/A.docx", _json(_source()))
    graph.on("GET", f"{SITE}/lists/Archive/drive", _json({"id": "D2"}))
    graph.on("GET", f"{DST}/root", _json({"id": "R2", "folder": {}}))
    state = {"copied": False}
    graph.on(
        "GET",
        f"{DST}/items/R2:/A.docx",
        lambda _r: (
            httpx.Response(200, json={"id": "C1"})
            if state["copied"]
            else httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        ),
    )
    graph.on("GET", f"{SRC}/items/I1/listItem", _json({"fields": {"Status": "Expired"}}))
    graph.on("GET", f"{SITE}/lists/Archive/columns", _json({"value": [{"name": "Status"}]}))

    def _copy(_request: httpx.Request) -> httpx.Response:
        state["copied"] = True
        return httpx.Response(202)

    graph.on("POST", f"{SRC}/items/I1/copy", _copy)
    graph.on("PATCH", f"{DST}/items/C1/listItem/fields", _json({}))
    graph.on("GET", f"{DST}/items/C1/listItem", _json({"fields": {}}))

    result = await _call(
        "move_microsoft_drive_file", site_id="SITE1", item_path="Contracts/A.docx", folder="", library="Archive"
    )

    assert "source was kept" in result.content
    assert graph.calls("DELETE") == []
