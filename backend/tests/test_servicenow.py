"""Tests for the ServiceNow integration provider package.

Covers tool introspection (including the URL-shaped catalog dummy), the HTTP
Basic auth header + base-URL + token helpers, the encoded-query builder, and the
core Table API flows (list with the encoded query + X-Total-Count pagination
hint, get by number vs sys_id, create, resolve, the generic record escape hatch
delete, the Aggregate count, and the Service Catalog order_now). Every API call
routes through the shared ``safe_request`` helper (the single chokepoint inside
``_common.sn_request``), which is monkeypatched here so no network is touched.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers.servicenow import _common as common
from app.integrations.providers.servicenow import (
    build_servicenow_headers,
    build_tools,
    servicenow_api_base,
)
from app.integrations.providers.servicenow._common import (
    build_encoded_query,
    parse_servicenow_token,
)
from app.tools.base import CallableToolExecutor, ToolCall
from tests.integration_file_fakes import HOSTILE_FILE_NAMES, assert_name_only_inside, fenced_and_open

_TOKEN = "https://acme.service-now.com||svc||pw"
_SYS_ID = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"


class _StubResponse:
    def __init__(
        self, payload: Any, *, status_code: int = 200, headers=None, content: bytes = b""
    ):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls."""

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, _client, method, url, _provider, action, *, expected_statuses=None, **kwargs
    ):
        self.calls.append(
            {"method": method, "url": url, "action": action, **kwargs}
        )
        res = self._router(method, url)
        # Routers return a bare payload, ``(payload, headers)``, or
        # ``(payload, headers, content)`` (the last for raw-bytes endpoints such
        # as the attachment file download).
        if isinstance(res, tuple):
            payload = res[0]
            headers = res[1] if len(res) > 1 else {}
            content = res[2] if len(res) > 2 else b""
        else:
            payload, headers, content = res, {}, b""
        return _StubResponse(payload, headers=headers, content=content), None


def _build(
    monkeypatch, router, *, persist_file_fn=None
) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(common, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor, persist_file_fn=persist_file_fn)
    return executor, recorder


# ---------------------------------------------------------------------------
# Introspection + helpers
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_catalog_dummy():
    # The catalog introspects providers with a 2-part URL dummy; the tools must
    # still build (validation happens at call time, not in build_tools).
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    for expected in (
        "get_servicenow_status",
        "list_servicenow_incidents",
        "get_servicenow_incident",
        "create_servicenow_incident",
        "resolve_servicenow_incident",
        "list_servicenow_changes",
        "list_servicenow_problems",
        "list_servicenow_tasks",
        "search_servicenow_cmdb_ci",
        "search_servicenow_users",
        "search_servicenow_groups",
        "search_servicenow_knowledge",
        "list_servicenow_catalog_items",
        "order_servicenow_catalog_item",
        "query_servicenow_table",
        "manage_servicenow_record",
        "count_servicenow_records",
        "download_servicenow_attachment",
    ):
        assert expected in names, expected
    assert len(tools) >= 40


def test_no_base_url_returns_no_tools():
    # Without the instance URL the provider can't build request URLs, so it
    # returns no tools (the UI requires base_url; introspection passes a dummy).
    executor = CallableToolExecutor()
    assert build_tools(token="||svc||pw", executor=executor) == []


def test_tool_priorities_sort_reads_before_writes_before_destructive():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in build_tools(token=_TOKEN, executor=executor)}
    assert tools["list_servicenow_incidents"].priority == 0
    assert tools["create_servicenow_incident"].priority == 1
    assert tools["resolve_servicenow_incident"].priority == 1
    assert tools["get_servicenow_status"].priority == 2
    # The generic delete-capable escape hatch is destructive (sorts last).
    assert tools["manage_servicenow_record"].priority == 2


def test_headers_and_base_url_helpers():
    headers = build_servicenow_headers("svc", "pw")
    assert headers["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(headers["Authorization"].split(" ", 1)[1]).decode()
    assert decoded == "svc:pw"
    assert headers["Accept"] == "application/json"
    # Trailing slash stripped; blank base resolves to "" (no default instance).
    assert servicenow_api_base("https://acme.service-now.com/") == (
        "https://acme.service-now.com"
    )
    assert servicenow_api_base("") == ""


def test_parse_token_splits_and_strips():
    base, user, pw = parse_servicenow_token(" https://acme.service-now.com/ || svc || pw ")
    assert base == "https://acme.service-now.com"
    assert user == "svc"
    assert pw == "pw"


def test_build_encoded_query_composes_filters_text_and_order():
    q = build_encoded_query(
        {"state": "2", "priority": "1", "active": "true", "blank": ""},
        text="email down",
        order_by="sys_updated_on",
    )
    assert "state=2" in q
    assert "priority=1" in q
    assert "active=true" in q
    assert "blank=" not in q  # empty values skipped
    assert "short_descriptionLIKEemail down" in q
    assert q.endswith("ORDERBYDESCsys_updated_on")


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_incidents_builds_query_and_pagination(monkeypatch):
    def router(method, url):
        return (
            {
                "result": [
                    {
                        "number": "INC0010001",
                        "short_description": "Email down",
                        "state": "In Progress",
                        "priority": "1 - Critical",
                        "assigned_to": "Jane Doe",
                        "sys_id": _SYS_ID,
                    }
                ]
            },
            {"X-Total-Count": "50"},
        )

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_servicenow_incidents",
            arguments={"state": "2", "priority": "1", "active": True},
        )
    )
    assert not result.is_error
    assert "INC0010001" in result.content
    assert "Email down" in result.content
    # Filters fold into the encoded query; results are newest-first.
    q = recorder.calls[-1]["params"]["sysparm_query"]
    assert "state=2" in q and "priority=1" in q and "active=true" in q
    assert "ORDERBYDESCsys_updated_on" in q
    # X-Total-Count drives the pagination hint.
    assert "Showing 1 of 50" in result.content
    assert recorder.calls[-1]["url"].endswith("/api/now/table/incident")


@pytest.mark.asyncio
async def test_get_incident_by_number_queries_number_field(monkeypatch):
    def router(method, url):
        return {
            "result": [
                {
                    "number": "INC0010001",
                    "short_description": "Email down",
                    "sys_id": _SYS_ID,
                }
            ]
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="get_servicenow_incident",
            arguments={"incident": "INC0010001"},
        )
    )
    assert not result.is_error
    assert "INC0010001" in result.content
    # A non-sys_id identifier is resolved via a number= query (limit 1).
    assert recorder.calls[-1]["params"]["sysparm_query"] == "number=INC0010001"
    assert recorder.calls[-1]["params"]["sysparm_limit"] == 1
    # The instance host is surfaced as a deep link.
    assert "https://acme.service-now.com/incident.do?sys_id=" in result.content


@pytest.mark.asyncio
async def test_get_incident_by_sys_id_hits_record_endpoint(monkeypatch):
    def router(method, url):
        return {"result": {"number": "INC0010001", "sys_id": _SYS_ID}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c3", name="get_servicenow_incident", arguments={"incident": _SYS_ID}
        )
    )
    assert not result.is_error
    assert recorder.calls[-1]["url"].endswith(f"/api/now/table/incident/{_SYS_ID}")


@pytest.mark.asyncio
async def test_create_incident_posts_body(monkeypatch):
    def router(method, url):
        return {"result": {"number": "INC0010002", "sys_id": _SYS_ID}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="create_servicenow_incident",
            arguments={
                "short_description": "VPN outage",
                "caller_id": "user-sys-id",
                "urgency": "1",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/now/table/incident")
    assert call["json"]["short_description"] == "VPN outage"
    assert call["json"]["caller_id"] == "user-sys-id"
    assert call["json"]["urgency"] == "1"


@pytest.mark.asyncio
async def test_create_incident_requires_short_description(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"result": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="c5",
            name="create_servicenow_incident",
            arguments={"short_description": "  "},
        )
    )
    assert "short_description is required" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_resolve_incident_sets_state_and_close_fields(monkeypatch):
    def router(method, url):
        return {"result": {"number": "INC0010001", "sys_id": _SYS_ID}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="resolve_servicenow_incident",
            arguments={"incident": _SYS_ID, "close_notes": "Rebooted the gateway."},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "patch"
    assert call["url"].endswith(f"/api/now/table/incident/{_SYS_ID}")
    assert call["json"]["state"] == "6"
    assert call["json"]["close_notes"] == "Rebooted the gateway."
    assert call["json"]["close_code"]


@pytest.mark.asyncio
async def test_resolve_incident_requires_close_notes(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"result": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="resolve_servicenow_incident",
            arguments={"incident": _SYS_ID, "close_notes": ""},
        )
    )
    assert "close_notes" in result.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Generic record escape hatch + Aggregate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_record_delete_resolves_and_deletes(monkeypatch):
    def router(method, url):
        return {"result": {}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="manage_servicenow_record",
            arguments={"table": "incident", "action": "delete", "record": _SYS_ID},
        )
    )
    assert not result.is_error
    call = recorder.calls[-1]
    assert call["method"] == "delete"
    assert call["url"].endswith(f"/api/now/table/incident/{_SYS_ID}")


@pytest.mark.asyncio
async def test_manage_record_rejects_invalid_action(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"result": {}})
    result = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="manage_servicenow_record",
            arguments={"table": "incident", "action": "frobnicate"},
        )
    )
    assert "action must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_count_records_parses_stats_envelope(monkeypatch):
    def router(method, url):
        return {"result": {"stats": {"count": "7"}}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="count_servicenow_records",
            arguments={"table": "incident", "query": "active=true"},
        )
    )
    assert "7 incident record(s)" in result.content
    call = recorder.calls[-1]
    assert call["url"].endswith("/api/now/stats/incident")
    assert call["params"]["sysparm_count"] == "true"


# ---------------------------------------------------------------------------
# Service Catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_catalog_item_posts_order_now(monkeypatch):
    def router(method, url):
        return {"result": {"request_number": "REQ0010001", "request_id": "req-sys-id"}}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="order_servicenow_catalog_item",
            arguments={"item_sys_id": "item-1", "variables": {"laptop": "macbook"}},
        )
    )
    assert not result.is_error
    assert "REQ0010001" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/sn_sc/servicecatalog/items/item-1/order_now")
    assert call["json"]["sysparm_quantity"] == "1"
    assert call["json"]["variables"] == {"laptop": "macbook"}


@pytest.mark.asyncio
async def test_search_users_builds_or_query(monkeypatch):
    def router(method, url):
        return {"result": [{"name": "John Doe", "user_name": "jdoe", "sys_id": _SYS_ID}]}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c12", name="search_servicenow_users", arguments={"text": "john"}
        )
    )
    assert not result.is_error
    assert "John Doe" in result.content
    q = recorder.calls[-1]["params"]["sysparm_query"]
    assert "nameLIKEjohn" in q and "emailLIKEjohn" in q
    assert "active=true" in q


# ---------------------------------------------------------------------------
# Attachments - download (persist callback + inline fallback)
# ---------------------------------------------------------------------------


def _attachment_router(method, url):
    """Route the attachment metadata read vs the raw-bytes file download."""
    if url.endswith("/file"):
        return ("", {}, b"file-bytes")
    return {
        "result": {
            "file_name": "diagram.png",
            "content_type": "image/png",
            "size_bytes": "10",
        }
    }


@pytest.mark.asyncio
async def test_download_attachment_uses_persist_callback(monkeypatch):
    # With a persist callback injected (chat / assignment context, wired in
    # integration_tools for servicenow), the attachment becomes a download card.
    from app.llm.base import FileRef

    captured: dict[str, Any] = {}

    async def _persist(filename, raw_bytes, content_type):
        captured.update(name=filename, size=len(raw_bytes), ct=content_type)
        return FileRef.from_bytes(
            data=raw_bytes,
            filename=filename,
            content_type=content_type,
            file_id="persisted-sn-1",
        )

    executor, recorder = _build(
        monkeypatch, _attachment_router, persist_file_fn=_persist
    )
    result = await executor.execute_tool(
        ToolCall(
            id="d1",
            name="download_servicenow_attachment",
            arguments={"attachment_id": _SYS_ID},
        )
    )
    assert not result.is_error
    assert "[[FILE:persisted-sn-1]]" in result.content
    assert "verbatim" in result.content
    assert captured["name"] == "diagram.png"
    assert captured["ct"] == "image/png"
    # The raw file is fetched from the attachment file endpoint.
    assert recorder.calls[-1]["url"].endswith(f"/api/now/attachment/{_SYS_ID}/file")


@pytest.mark.asyncio
async def test_download_attachment_inline_fallback_includes_marker(monkeypatch):
    # Without a persist callback (e.g. the evaluation engine passes no scope),
    # the tool still returns the [[FILE:...]] marker via the inline FileRef.
    executor, _ = _build(monkeypatch, _attachment_router)
    result = await executor.execute_tool(
        ToolCall(
            id="d2",
            name="download_servicenow_attachment",
            arguments={"attachment_id": _SYS_ID},
        )
    )
    assert not result.is_error
    assert "[[FILE:" in result.content
    assert "verbatim" in result.content


# ---------------------------------------------------------------------------
# Attachments - upload (text content, attached file, size guard, validation)
# ---------------------------------------------------------------------------


def _upload_router(method, url):
    """Return a created-attachment envelope for the attachment POST."""
    return {"result": {"sys_id": "att-1"}}


@pytest.mark.asyncio
async def test_add_attachment_text_content_posts_file(monkeypatch):
    # A sys_id record skips resolution and posts the encoded text as a file.
    executor, recorder = _build(monkeypatch, _upload_router)
    handler = executor._handlers["add_servicenow_attachment"]
    result = await handler(table="incident", record=_SYS_ID, content="hello world")
    inside, outside = fenced_and_open(result)
    assert outside == f"Attached the text attachment (0.0 KB) to incident {_SYS_ID} (attachment sys_id: att-1).\n"
    assert "Name: note.txt" in inside.splitlines()
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/api/now/attachment/file")
    assert call["params"]["table_name"] == "incident"
    assert call["params"]["table_sys_id"] == _SYS_ID
    assert call["params"]["file_name"] == "note.txt"
    assert call["content"] == b"hello world"


@pytest.mark.asyncio
async def test_add_attachment_forwards_attached_file(monkeypatch):
    # The file_context path forwards the user's attached bytes byte-for-byte
    # with the file's own mime type (not application/json).
    executor, recorder = _build(monkeypatch, _upload_router)
    handler = executor._handlers["add_servicenow_attachment"]

    class _FakeFileContext:
        async def resolve_upload(self, file_id):
            assert file_id == "f1"
            return (b"\x89PNG-bytes", "logo.png", "image/png")

    result = await handler(
        table="incident",
        record=_SYS_ID,
        file_id="f1",
        file_context=_FakeFileContext(),
    )
    inside, outside = fenced_and_open(result)
    assert outside == f"Attached file 'f1' (0.0 KB) to incident {_SYS_ID} (attachment sys_id: att-1).\n"
    assert "Name: logo.png" in inside.splitlines()
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["content"] == b"\x89PNG-bytes"
    assert call["params"]["file_name"] == "logo.png"
    assert call["headers"]["Content-Type"] == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_add_attachment_names_a_forwarded_file_only_inside_the_block(monkeypatch, hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: {"result": {"sys_id": "att-1", "download_link": f"https://snow.example/{name}"}},
    )
    handler = executor._handlers["add_servicenow_attachment"]

    class _FakeFileContext:
        async def resolve_upload(self, file_id):
            return (b"bytes", name, "text/plain")

    result = await handler(table="incident", record=_SYS_ID, file_id="f1", file_context=_FakeFileContext())
    outside = assert_name_only_inside(result, name)
    assert "snow.example" not in outside
    assert recorder.calls[-1]["params"]["file_name"] == name


@pytest.mark.asyncio
async def test_add_attachment_enforces_size_limit(monkeypatch):
    # The max-size guard rejects oversized payloads before any request.
    monkeypatch.setattr(
        "app.integrations.providers.servicenow.records._MAX_ATTACHMENT_BYTES", 4
    )
    executor, recorder = _build(monkeypatch, _upload_router)
    handler = executor._handlers["add_servicenow_attachment"]
    result = await handler(table="incident", record=_SYS_ID, content="too large")
    assert "exceeds" in result
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_add_attachment_requires_file_or_content(monkeypatch):
    # Neither a file_id (with context) nor text content -> a clear error, no call.
    executor, recorder = _build(monkeypatch, _upload_router)
    handler = executor._handlers["add_servicenow_attachment"]
    result = await handler(table="incident", record=_SYS_ID)
    assert "file_id" in result and "content" in result
    assert recorder.calls == []
