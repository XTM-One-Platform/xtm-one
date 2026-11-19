"""Microsoft drive upkeep: rename, move, delete and export to PDF.

The tools run against a fake Microsoft Graph served through
``httpx.MockTransport`` (documented response shapes, no network, no token).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.integrations import file_delivery
from app.integrations.providers import microsoft as ms
from app.llm.base import FileRef
from app.services.integration_read_only import withhold_write_tools
from app.services.tool_classification import infer_tool_type
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

DRIVE = "/v1.0/drives/D1"
ME_ITEM = "/v1.0/me/drive/root:/Legal/Terms.docx"
TOOLS = (
    "rename_microsoft_drive_file",
    "move_microsoft_drive_file",
    "delete_microsoft_drive_file",
    "convert_microsoft_drive_file_to_pdf",
)


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
    return fake


def _json(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


def _item(
    name: str = "Terms.docx", item_id: str = "I1", parent: str = "P1", folder: str = "Legal", **extra: Any
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "size": 10,
        "file": {"mimeType": "x"},
        "webUrl": f"https://contoso.sharepoint.com/{folder}/{name}",
        "parentReference": {"driveId": "D1", "id": parent, "path": f"/drives/D1/root:/{folder}"},
        "lastModifiedDateTime": "2026-09-09T10:00:00Z",
        "lastModifiedBy": {"user": {"displayName": "Legal Ops"}},
        **extra,
    }


def _executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    ms.build_tools("tok", executor)
    return executor


async def _call(executor: CallableToolExecutor, name: str, **arguments: Any) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def _not_found(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "The resource could not be found."}})


# ── rename ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_patches_the_name_and_reads_it_back(graph: FakeGraph) -> None:
    state = {"name": "Terms.docx"}
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/items/I1", lambda _r: httpx.Response(200, json=_item(state["name"])))
    graph.on("GET", f"{DRIVE}/items/P1:/Terms EN.docx", _not_found)

    def _patch(request: httpx.Request) -> httpx.Response:
        state["name"] = json.loads(request.content)["name"]
        return httpx.Response(200, json=_item(state["name"]))

    graph.on("PATCH", f"{DRIVE}/items/I1", _patch)

    result = await _call(
        _executor(), "rename_microsoft_drive_file", item_path="Legal/Terms.docx", new_name="Terms EN.docx"
    )

    assert not result.is_error, result.content
    assert result.content.startswith("Renamed the item; read back")
    assert "Name: Terms EN.docx" in result.content
    patch = graph.calls("PATCH")[0]
    assert patch.url.params["@microsoft.graph.conflictBehavior"] == "fail"
    assert json.loads(patch.content) == {"name": "Terms EN.docx"}


@pytest.mark.asyncio
async def test_rename_refuses_a_taken_name_without_overwrite(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/items/P1:/Other.docx", _json({"id": "I2", "name": "Other.docx"}))

    result = await _call(
        _executor(), "rename_microsoft_drive_file", item_path="Legal/Terms.docx", new_name="Other.docx"
    )

    assert "already holds an item with the requested name" in result.content
    assert "overwrite=true" in result.content
    assert graph.calls("PATCH") == []


@pytest.mark.asyncio
async def test_rename_with_overwrite_replaces(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/items/P1:/Other.docx", _json({"id": "I2", "name": "Other.docx"}))
    graph.on("PATCH", f"{DRIVE}/items/I1", _json(_item("Other.docx")))
    graph.on("GET", f"{DRIVE}/items/I1", _json(_item("Other.docx")))

    result = await _call(
        _executor(), "rename_microsoft_drive_file", item_path="Legal/Terms.docx", new_name="Other.docx", overwrite=True
    )

    assert result.content.startswith("Renamed the item")
    assert graph.calls("PATCH")[0].url.params["@microsoft.graph.conflictBehavior"] == "replace"


@pytest.mark.asyncio
async def test_rename_returns_the_graph_error_as_is(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/items/P1:/New.docx", _not_found)
    graph.on(
        "PATCH",
        f"{DRIVE}/items/I1",
        _json(
            {"error": {"code": "resourceLocked", "message": "The resource you are attempting to access is locked"}}, 423
        ),
    )

    result = await _call(_executor(), "rename_microsoft_drive_file", item_path="Legal/Terms.docx", new_name="New.docx")

    assert result.is_error
    assert "locked" in result.content


@pytest.mark.asyncio
async def test_rename_by_url_resolves_the_share(graph: FakeGraph) -> None:
    url = "https://contoso.sharepoint.com/sites/legal/Shared%20Documents/Terms.docx"
    share = ms._encode_share_id(url)
    state = {"name": "Terms.docx"}
    graph.on("GET", f"/v1.0/shares/{share}/driveItem", _json(_item()))
    graph.on("GET", f"{DRIVE}/items/I1", lambda _r: httpx.Response(200, json=_item(state["name"])))
    graph.on("GET", f"{DRIVE}/items/P1:/B.docx", _not_found)

    def _patch(request: httpx.Request) -> httpx.Response:
        state["name"] = json.loads(request.content)["name"]
        return httpx.Response(200, json=_item(state["name"]))

    graph.on("PATCH", f"{DRIVE}/items/I1", _patch)

    result = await _call(_executor(), "rename_microsoft_drive_file", url=url, new_name="B.docx")

    assert result.content.startswith("Renamed the item"), result.content


@pytest.mark.asyncio
async def test_a_location_is_url_or_item_path_never_both() -> None:
    result = await _call(_executor(), "rename_microsoft_drive_file", url="https://x/y", item_path="a", new_name="b")
    assert "not both" in result.content


@pytest.mark.asyncio
async def test_a_name_with_a_slash_points_at_move() -> None:
    result = await _call(_executor(), "rename_microsoft_drive_file", item_path="a.docx", new_name="b/c.docx")
    assert "move_microsoft_drive_file" in result.content


# ── move ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_move_sets_the_parent_and_reads_it_back(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/root:/Legal/Archive", _json({"id": "P2", "name": "Archive", "folder": {"childCount": 0}}))
    graph.on("GET", f"{DRIVE}/items/P2:/Terms.docx", _not_found)
    graph.on("PATCH", f"{DRIVE}/items/I1", _json(_item(parent="P2", folder="Legal/Archive")))
    graph.on("GET", f"{DRIVE}/items/I1", _json(_item(parent="P2", folder="Legal/Archive")))

    result = await _call(_executor(), "move_microsoft_drive_file", item_path="Legal/Terms.docx", folder="Legal/Archive")

    assert result.content.startswith("Moved the item to folder 'Legal/Archive'"), result.content
    assert "Path: Legal/Archive/Terms.docx" in result.content
    assert json.loads(graph.calls("PATCH")[0].content) == {"parentReference": {"id": "P2"}}


@pytest.mark.asyncio
async def test_move_to_a_missing_folder_changes_nothing(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/root:/Nope", _not_found)

    result = await _call(_executor(), "move_microsoft_drive_file", item_path="Legal/Terms.docx", folder="Nope")

    assert "has no folder 'Nope'" in result.content
    assert graph.calls("PATCH") == []


@pytest.mark.asyncio
async def test_move_refuses_a_name_taken_at_the_destination(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/root:/Archive", _json({"id": "P2", "folder": {}}))
    graph.on("GET", f"{DRIVE}/items/P2:/Terms.docx", _json({"id": "I9", "name": "Terms.docx"}))

    result = await _call(_executor(), "move_microsoft_drive_file", item_path="Legal/Terms.docx", folder="Archive")

    assert "already holds an item with the requested name" in result.content
    assert graph.calls("PATCH") == []


# ── delete ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_sends_to_the_recycle_bin_and_names_the_item(graph: FakeGraph) -> None:
    deleted = {"done": False}
    graph.on("GET", ME_ITEM, _json(_item("Subprocessors June.pdf")))

    def _delete(_request: httpx.Request) -> httpx.Response:
        deleted["done"] = True
        return httpx.Response(204)

    graph.on("DELETE", f"{DRIVE}/items/I1", _delete)
    graph.on(
        "GET", f"{DRIVE}/items/I1", lambda r: _not_found(r) if deleted["done"] else httpx.Response(200, json=_item())
    )

    result = await _call(_executor(), "delete_microsoft_drive_file", item_path="Legal/Terms.docx")

    assert result.content.startswith("Moved the file to the recycle bin"), result.content
    assert "Name: Subprocessors June.pdf" in result.content
    assert "Path: Legal/Subprocessors June.pdf" in result.content
    assert "Last modified: 2026-09-09T10:00:00Z" in result.content
    assert "Modified by: Legal Ops" in result.content


@pytest.mark.asyncio
async def test_delete_refuses_a_folder_unless_recursive(graph: FakeGraph) -> None:
    folder = _item("Old", folder="Legal")
    folder.pop("file")
    folder["folder"] = {"childCount": 3}
    graph.on("GET", ME_ITEM, _json(folder))

    refused = await _call(_executor(), "delete_microsoft_drive_file", item_path="Legal/Terms.docx")

    assert "is a folder holding 3 item(s)" in refused.content
    assert graph.calls("DELETE") == []

    graph.on("DELETE", f"{DRIVE}/items/I1", lambda _r: httpx.Response(204))
    graph.on("GET", f"{DRIVE}/items/I1", _not_found)
    done = await _call(_executor(), "delete_microsoft_drive_file", item_path="Legal/Terms.docx", recursive=True)

    assert done.content.startswith("Moved the folder and its content to the recycle bin")


@pytest.mark.asyncio
async def test_delete_returns_a_checked_out_refusal_as_is(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on(
        "DELETE",
        f"{DRIVE}/items/I1",
        _json({"error": {"code": "accessDenied", "message": "The file is checked out for editing by Legal Ops"}}, 403),
    )

    result = await _call(_executor(), "delete_microsoft_drive_file", item_path="Legal/Terms.docx")

    assert result.is_error
    assert "checked out for editing" in result.content


# ── export to PDF ─────────────────────────────────────────────────────────


class Persisted:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str]] = []

    async def __call__(self, filename: str, data: bytes, mime: str) -> FileRef:
        self.calls.append((filename, data, mime))
        return FileRef.from_bytes(data=data, filename=filename, content_type=mime)


PDF = b"%PDF-1.7 terms"


@pytest.mark.asyncio
async def test_export_keeps_the_pdf_as_a_conversation_file(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/items/I1/content", lambda _r: httpx.Response(200, content=PDF))
    persisted = Persisted()
    executor = CallableToolExecutor()
    ms.build_tools("tok", executor, persist_file_fn=persisted)

    result = await _call(executor, "convert_microsoft_drive_file_to_pdf", item_path="Legal/Terms.docx")

    assert result.content.startswith("Exported the file to PDF"), result.content
    assert persisted.calls == [("Terms.pdf", PDF, "application/pdf")]
    assert result.files and result.files[0].filename == "Terms.pdf"
    assert graph.calls("GET")[-1].url.params["format"] == "pdf"
    assert graph.calls("PUT") == []


@pytest.mark.asyncio
async def test_export_with_upload_writes_the_pdf_beside_the_source_and_reads_it_back(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item()))
    graph.on("GET", f"{DRIVE}/items/I1/content", lambda _r: httpx.Response(200, content=PDF))
    graph.on("GET", f"{DRIVE}/items/P1:/AI Terms.pdf", _not_found)
    graph.on("PUT", f"{DRIVE}/items/P1:/AI Terms.pdf:/content", _json({"id": "I7"}, 201))
    graph.on("GET", f"{DRIVE}/items/I7", _json(_item("AI Terms.pdf", item_id="I7", size=len(PDF))))

    result = await _call(
        _executor(),
        "convert_microsoft_drive_file_to_pdf",
        item_path="Legal/Terms.docx",
        upload=True,
        pdf_name="AI Terms",
    )

    assert "Uploaded the PDF beside the source; read back" in result.content, result.content
    assert "Name: AI Terms.pdf" in result.content
    put = graph.calls("PUT")[0]
    assert put.content == PDF
    assert put.headers["content-type"] == "application/pdf"
    assert put.headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_export_refuses_a_format_graph_does_not_convert(graph: FakeGraph) -> None:
    graph.on("GET", ME_ITEM, _json(_item("photo.png")))

    result = await _call(_executor(), "convert_microsoft_drive_file_to_pdf", item_path="Legal/Terms.docx")

    assert "converts only Word, PowerPoint and Excel files" in result.content
    assert graph.calls("PUT") == []


# ── read-only binding and classification ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_a_read_only_binding_withholds_the_upkeep_tools(tool: str) -> None:
    executor = CallableToolExecutor()
    definitions = withhold_write_tools(ms.build_tools("tok", executor), executor, integration_label="Microsoft")

    names = {definition.name for definition in definitions}
    assert tool not in names
    assert "list_microsoft_folder_by_url" in names
    result = await _call(executor, tool, item_path="Legal/Terms.docx", new_name="x.docx", folder="A")
    assert result.is_error
    assert result.content.startswith(f"Refused: {tool}")


@pytest.mark.parametrize("tool", TOOLS)
def test_every_upkeep_tool_is_classified_as_a_write(tool: str) -> None:
    assert infer_tool_type(tool) == "write"
