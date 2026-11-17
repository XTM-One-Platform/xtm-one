"""Tests for the Doppel integration provider.

Covers tool introspection, the alerts list flow (headers + params + metadata
envelope + zero-indexed pagination hint), the get-by-id/entity mutual
exclusion, the create/update alert write paths (body shape, enum guards,
paired-parameter guards, already-exists statuses), alert evidence file
management (upload from chat attachment / agent text, delete, size and
extension guards), the referrer log submission, URL scanning (submit +
poll), brands (list filters + archive), protected assets (list/create/
delete + path encoding), HRM phishing simulations (campaign filters +
default test-campaign exclusion, campaign detail, per-user events with
joined user context), the compound-token base URL / header construction,
and the V2 OAuth client-credentials mode (token mint + caching + bearer
auth on ``/v2/`` paths). Every API call routes through the shared
``safe_request`` helper, which is monkeypatched here so no network is
touched.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers import doppel as dp
from app.integrations.providers.doppel import (
    build_doppel_headers,
    build_tools,
    doppel_api_base,
    request_doppel_v2_token,
    validate_doppel_v2_connection,
)
from app.llm.base import FileRef
from app.tools.base import CallableToolExecutor, FileContext, ToolCall
from tests.integration_file_fakes import HOSTILE_FILE_NAMES, assert_name_only_inside, fenced_and_open

_TOKEN = "https://api.doppel.com||org-key-123||user-key-456||ACM"


class _StubResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x" if payload not in (None, "") else b""

    def json(self) -> Any:
        return self._payload


class _Recorder:
    """Fake ``safe_request`` that routes by (method, url) and records calls.

    The router may return either a payload or a ``(payload, status)`` tuple.
    Non-2xx statuses mimic the real contract: ``(resp, err)`` when the status
    is in ``expected_statuses``, ``(None, err)`` otherwise.
    """

    def __init__(self, router):
        self._router = router
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _client, method, url, _provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, "action": action, **kwargs})
        result = self._router(method, url)
        payload, status = (
            result if isinstance(result, tuple) else (result, 200)
        )
        resp = _StubResponse(payload, status)
        if 200 <= status < 300:
            return resp, None
        err = f"Error: HTTP {status}"
        expected = kwargs.get("expected_statuses")
        if expected and status in expected:
            return resp, err
        return None, err


def _build(monkeypatch, router) -> tuple[CallableToolExecutor, _Recorder]:
    recorder = _Recorder(router)
    monkeypatch.setattr(dp, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token=_TOKEN, executor=executor)
    return executor, recorder


def _build_v2(
    monkeypatch,
    router,
    client_id: str,
    client_secret: str = "secret-xyz",  # noqa: S107 - test fixture, not a secret
) -> tuple[CallableToolExecutor, _Recorder]:
    """Build in V2 mode (client credentials present, no V1 keys).

    Each test passes a UNIQUE client_id: the app-token cache is module-level
    (keyed by hashed credentials), so distinct credentials guarantee test
    isolation without touching the cache internals.
    """
    recorder = _Recorder(router)
    monkeypatch.setattr(dp, "safe_request", recorder)
    executor = CallableToolExecutor()
    token = "||".join(
        ["https://api.doppel.com", "", "", "", client_id, client_secret]
    )
    build_tools(token=token, executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Auth + URL helpers
# ---------------------------------------------------------------------------


def test_doppel_api_base_defaults_and_trims_version_suffix():
    assert doppel_api_base("") == "https://api.doppel.com"
    assert doppel_api_base("  https://api.doppel.com/  ") == "https://api.doppel.com"
    # A pasted versioned base never produces a doubled /v1/v1 (or /v2/v2) path.
    assert doppel_api_base("https://api.doppel.com/v1") == "https://api.doppel.com"
    assert doppel_api_base("https://api.doppel.com/v2") == "https://api.doppel.com"
    assert doppel_api_base("https://proxy.example.com/V1/") == (
        "https://proxy.example.com"
    )
    assert doppel_api_base("https://proxy.example.com/V2/") == (
        "https://proxy.example.com"
    )


def test_build_doppel_headers_strips_and_omits_optional():
    headers = build_doppel_headers(" org-key \n", "", "")
    assert headers["x-api-key"] == "org-key"
    assert "x-user-api-key" not in headers
    assert "x-organization-code" not in headers
    full = build_doppel_headers("org-key", " user-key ", " ACM ")
    assert full["x-user-api-key"] == "user-key"
    assert full["x-organization-code"] == "ACM"


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_build_tools_returns_definitions_with_dummy_token():
    executor = CallableToolExecutor()
    tools = build_tools(token="https://dummy.example.com||__dummy__", executor=executor)
    names = {t.name for t in tools}
    assert "list_doppel_alerts" in names
    assert "get_doppel_alert" in names
    assert "download_doppel_alert_screenshot" in names
    assert "create_doppel_alert" in names
    assert "update_doppel_alert" in names
    assert "manage_doppel_alert_files" in names
    assert "submit_doppel_referrer_log" in names
    assert "scan_doppel_url" in names
    assert "get_doppel_scan_result" in names
    assert "list_doppel_brands" in names
    assert "delete_doppel_brand" in names
    assert "list_doppel_protected_assets" in names
    assert "create_doppel_protected_asset" in names
    assert "delete_doppel_protected_asset" in names
    assert "list_doppel_hrm_campaigns" in names
    assert "get_doppel_hrm_campaign" in names
    assert "list_doppel_hrm_events" in names
    # Exact count so an accidentally dropped tool fails loudly.
    assert len(tools) == 17


def test_all_tools_use_doppel_source_and_sorted_priority():
    tools = build_tools(
        token="https://dummy.example.com||__dummy__", executor=CallableToolExecutor()
    )
    assert all(t.source == "integration:doppel" for t in tools)
    priorities = {t.name: t.priority for t in tools}
    assert priorities["list_doppel_alerts"] == 0
    assert priorities["get_doppel_alert"] == 0
    assert priorities["create_doppel_alert"] == 1
    assert priorities["update_doppel_alert"] == 1
    assert priorities["manage_doppel_alert_files"] == 1
    assert priorities["submit_doppel_referrer_log"] == 1
    assert priorities["scan_doppel_url"] == 1
    assert priorities["get_doppel_scan_result"] == 0
    assert priorities["list_doppel_brands"] == 0
    assert priorities["delete_doppel_brand"] == 2
    assert priorities["list_doppel_protected_assets"] == 0
    assert priorities["create_doppel_protected_asset"] == 1
    assert priorities["delete_doppel_protected_asset"] == 2
    assert priorities["list_doppel_hrm_campaigns"] == 0
    assert priorities["get_doppel_hrm_campaign"] == 0
    assert priorities["list_doppel_hrm_events"] == 0


# ---------------------------------------------------------------------------
# List flow: headers, params, metadata envelope, pagination hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_alerts_params_envelope_and_pagination(monkeypatch):
    def router(method, url):
        return {
            "alerts": [
                {
                    "id": "ACM-1234",
                    "entity": "https://suspicious-site.example.com",
                    "brand": "Acme Corp",
                    "queue_state": "doppel_review",
                    "entity_state": "active",
                    "severity": "high",
                    "score": 0.91,
                    "product": "domains",
                    "platform": "domains",
                    "created_at": "2026-06-15T10:30:00",
                    "doppel_link": "https://app.doppel.com/alerts/ACM-1234",
                    "tags": [{"name": "Credential Theft"}],
                }
            ],
            "metadata": {"count": 42, "page": 0, "total_pages": 3, "page_size": 20},
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="list_doppel_alerts",
            arguments={"queue_state": "doppel_review", "product": "domains"},
        )
    )
    assert not result.is_error
    assert "ACM-1234" in result.content
    assert "Found 42 alerts" in result.content
    assert "Credential Theft" in result.content
    # Zero-indexed pagination: page 0 of 3 -> hint points to page 1.
    assert 'page="1"' in result.content
    call = recorder.calls[0]
    assert call["method"] == "get"
    assert "/v1/alerts?" in call["url"]
    assert "queue_state=doppel_review" in call["url"]
    assert "product=domains" in call["url"]
    # Header auth: org key + user key + org code from the compound token.
    assert call["headers"]["x-api-key"] == "org-key-123"
    assert call["headers"]["x-user-api-key"] == "user-key-456"
    assert call["headers"]["x-organization-code"] == "ACM"


@pytest.mark.asyncio
async def test_list_alerts_no_hint_on_last_page(monkeypatch):
    def router(method, url):
        return {
            "alerts": [{"id": "ACM-1", "entity": "https://x.example.com"}],
            "metadata": {"count": 3, "page": 2, "total_pages": 3, "page_size": 1},
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c2", name="list_doppel_alerts", arguments={"page": 2})
    )
    assert not result.is_error
    assert "page=" not in result.content.replace("page_size", "")


@pytest.mark.asyncio
async def test_list_alerts_guards_enums_and_sort_pairing(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    bad_state = await executor.execute_tool(
        ToolCall(id="c3", name="list_doppel_alerts", arguments={"queue_state": "open"})
    )
    assert "queue_state must be one of" in bad_state.content
    lonely_sort = await executor.execute_tool(
        ToolCall(
            id="c4",
            name="list_doppel_alerts",
            arguments={"sort_type": "date_sourced"},
        )
    )
    assert "must be provided together" in lonely_sort.content
    bad_order = await executor.execute_tool(
        ToolCall(
            id="c4b",
            name="list_doppel_alerts",
            arguments={"sort_type": "date_sourced", "sort_order": "up"},
        )
    )
    assert "sort_order must be 'asc' or 'desc'" in bad_order.content
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Get alert: id XOR entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_alert_requires_exactly_one_identifier(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    neither = await executor.execute_tool(
        ToolCall(id="c5", name="get_doppel_alert", arguments={})
    )
    assert "exactly one" in neither.content
    both = await executor.execute_tool(
        ToolCall(
            id="c6",
            name="get_doppel_alert",
            arguments={"alert_id": "ACM-1", "entity": "https://x.example.com"},
        )
    )
    assert "exactly one" in both.content
    # Whitespace-only identifiers count as missing (get + update): the XOR
    # guard fires instead of sending an invalid identifier to the API.
    ws_get = await executor.execute_tool(
        ToolCall(id="c6b", name="get_doppel_alert", arguments={"alert_id": "   "})
    )
    assert "exactly one" in ws_get.content
    ws_update = await executor.execute_tool(
        ToolCall(
            id="c6c",
            name="update_doppel_alert",
            arguments={"alert_id": "   ", "comment": "x"},
        )
    )
    assert "exactly one" in ws_update.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_get_alert_formats_detail(monkeypatch):
    def router(method, url):
        return {
            "id": "ACM-1234",
            "entity": "https://suspicious-site.example.com",
            "brand": "Acme Corp",
            "queue_state": "monitoring",
            "entity_state": "active",
            "severity": "medium",
            "score": 0.55,
            "product": "domains",
            "platform": "domains",
            "source": "API Upload",
            "uploaded_by": "jane.doe@company.com",
            "created_at": "2026-06-15T10:30:00",
            "last_activity": "2026-06-20T14:00:00",
            "alert_summary": "Phishing domain impersonating Acme login pages.",
            "screenshot_url": "https://signed.example.com/shot.png",
            "doppel_link": "https://app.doppel.com/alerts/ACM-1234",
            "entity_content": {
                "root_domain": {
                    "domain": "suspicious-site.example.com",
                    "registrar": "Example Registrar",
                }
            },
            "audit_logs": [
                {
                    "timestamp": "2026-06-15T10:30:00",
                    "type": "alert_create",
                    "value": "needs_review",
                    "changed_by": "Doppel",
                }
            ],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c7", name="get_doppel_alert", arguments={"alert_id": "ACM-1234"})
    )
    assert not result.is_error
    assert "Alert: ACM-1234" in result.content
    assert "Phishing domain impersonating" in result.content
    assert "alert_create" in result.content
    assert "expires in 1 hour" in result.content
    call = recorder.calls[0]
    assert "/v1/alert?" in call["url"]
    assert "id=ACM-1234" in call["url"]
    assert "entity=" not in call["url"]


@pytest.mark.asyncio
async def test_get_alert_renders_queue_movement_justification(monkeypatch):
    # Queue-movement audit entries carry metadata.queue_movement.justification
    # (2026-05-27 API update) - the detail view surfaces it under the entry.
    def router(method, url):
        return {
            "id": "ACM-77",
            "entity": "https://phish.example.com",
            "audit_logs": [
                {
                    "timestamp": "2026-07-01T09:00:00",
                    "type": "queue_state_change",
                    "value": "taken_down",
                    "changed_by": "analyst@company.com",
                    "metadata": {
                        "queue_movement": {
                            "justification": (
                                "Registrar confirmed the takedown request."
                            )
                        }
                    },
                },
                # Entry without queue metadata: no justification line.
                {
                    "timestamp": "2026-06-15T10:30:00",
                    "type": "alert_create",
                    "value": "needs_review",
                    "changed_by": "Doppel",
                    "metadata": {"queue_movement": "not-a-dict"},
                },
            ],
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(id="c7c", name="get_doppel_alert", arguments={"alert_id": "ACM-77"})
    )
    assert not result.is_error
    assert (
        "Justification: Registrar confirmed the takedown request."
        in result.content
    )
    # Exactly one justification line: the malformed metadata is ignored.
    assert result.content.count("Justification:") == 1


@pytest.mark.asyncio
async def test_get_alert_404_reports_not_found(monkeypatch):
    # A 404 is an expected lookup miss (not a generic HTTP error): the tool
    # must return the friendly "not found" message.
    executor, recorder = _build(
        monkeypatch, lambda m, u: ({"message": "Alert not found."}, 404)
    )
    result = await executor.execute_tool(
        ToolCall(id="c7b", name="get_doppel_alert", arguments={"alert_id": "ACM-404"})
    )
    assert not result.is_error
    assert result.content == "Alert ACM-404 not found."
    assert recorder.calls[0]["expected_statuses"] == {404}


# ---------------------------------------------------------------------------
# Create alert (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_alert_posts_body_and_reports_created(monkeypatch):
    def router(method, url):
        return (
            {
                "id": "ACM-9",
                "entity": "https://phish.example.com",
                "doppel_link": "https://app.doppel.com/alerts/ACM-9",
            },
            201,
        )

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c8",
            name="create_doppel_alert",
            arguments={
                "entity": "https://phish.example.com",
                "brand": "Acme Corp",
                "tags": "Credential Theft, Malicious",
            },
        )
    )
    assert not result.is_error
    assert "Alert created" in result.content
    assert "ACM-9" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/v1/alert")
    assert call["json"] == {
        "entity": "https://phish.example.com",
        "brand": "Acme Corp",
        "tags": ["Credential Theft", "Malicious"],
    }


@pytest.mark.asyncio
async def test_create_alert_existing_and_conflict_statuses(monkeypatch):
    responses = iter(
        [
            ({"id": "ACM-9", "message": "Alert already exists."}, 200),
            ({"id": "ACM-9", "message": "Different brand."}, 409),
        ]
    )
    executor, _ = _build(monkeypatch, lambda m, u: next(responses))
    existing = await executor.execute_tool(
        ToolCall(
            id="c9",
            name="create_doppel_alert",
            arguments={"entity": "https://phish.example.com"},
        )
    )
    assert "already exists" in existing.content
    conflict = await executor.execute_tool(
        ToolCall(
            id="c10",
            name="create_doppel_alert",
            arguments={"entity": "https://phish.example.com"},
        )
    )
    assert "different brand" in conflict.content.lower()


# ---------------------------------------------------------------------------
# Update alert (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_alert_body_and_identifier_params(monkeypatch):
    def router(method, url):
        return {"id": "ACM-1234", "queue_state": "actioned", "entity_state": "down"}

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="c11",
            name="update_doppel_alert",
            arguments={
                "alert_id": "ACM-1234",
                "queue_state": "actioned",
                "entity_state": "down",
                "comment": "Confirmed phishing - proceeding with takedown.",
            },
        )
    )
    assert not result.is_error
    assert "Alert updated" in result.content
    call = recorder.calls[0]
    assert call["method"] == "put"
    assert "/v1/alert?" in call["url"]
    assert "id=ACM-1234" in call["url"]
    assert call["json"] == {
        "queue_state": "actioned",
        "entity_state": "down",
        "comment": "Confirmed phishing - proceeding with takedown.",
    }


@pytest.mark.asyncio
async def test_update_alert_guards(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    no_fields = await executor.execute_tool(
        ToolCall(
            id="c12", name="update_doppel_alert", arguments={"alert_id": "ACM-1"}
        )
    )
    assert "at least one field" in no_fields.content
    lonely_tag = await executor.execute_tool(
        ToolCall(
            id="c13",
            name="update_doppel_alert",
            arguments={"alert_id": "ACM-1", "tag_action": "add"},
        )
    )
    assert "must be provided together" in lonely_tag.content
    bad_state = await executor.execute_tool(
        ToolCall(
            id="c14",
            name="update_doppel_alert",
            arguments={"alert_id": "ACM-1", "entity_state": "gone"},
        )
    )
    assert "entity_state must be one of" in bad_state.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_update_alert_tag_pair_builds_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "ACM-1"})
    result = await executor.execute_tool(
        ToolCall(
            id="c15",
            name="update_doppel_alert",
            arguments={
                "entity": "https://phish.example.com",
                "tag_action": "add",
                "tag_name": "Credential Theft",
            },
        )
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert "entity=" in call["url"]
    assert call["json"] == {"tag_action": "add", "tag_name": "Credential Theft"}


# ---------------------------------------------------------------------------
# Alert evidence files (upload / delete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manage_alert_files_guards(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    bad_action = await executor.execute_tool(
        ToolCall(
            id="f1",
            name="manage_doppel_alert_files",
            arguments={"alert_id": "ACM-1", "action": "attach"},
        )
    )
    assert "action must be one of" in bad_action.content
    both_ids = await executor.execute_tool(
        ToolCall(
            id="f2",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "entity": "https://x.example.com",
                "action": "upload",
            },
        )
    )
    assert "exactly one" in both_ids.content
    delete_no_name = await executor.execute_tool(
        ToolCall(
            id="f3",
            name="manage_doppel_alert_files",
            arguments={"alert_id": "ACM-1", "action": "delete"},
        )
    )
    assert "'file_name'" in delete_no_name.content
    upload_no_source = await executor.execute_tool(
        ToolCall(
            id="f4",
            name="manage_doppel_alert_files",
            arguments={"alert_id": "ACM-1", "action": "upload"},
        )
    )
    assert "exactly one of" in upload_no_source.content
    upload_both_sources = await executor.execute_tool(
        ToolCall(
            id="f5",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "action": "upload",
                "file_id": "file-1",
                "content_text": "evidence",
            },
        )
    )
    assert "exactly one of" in upload_both_sources.content
    # Whitespace-only content_text counts as missing (it would upload an
    # empty file otherwise).
    ws_content = await executor.execute_tool(
        ToolCall(
            id="f6",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "action": "upload",
                "content_text": "   ",
            },
        )
    )
    assert "exactly one of" in ws_content.content
    bad_ext = await executor.execute_tool(
        ToolCall(
            id="f7",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "action": "upload",
                "content_text": "evidence",
                "file_name": "payload.bat",
            },
        )
    )
    inside, outside = fenced_and_open(bad_ext.content)
    assert outside.startswith("Error: the extension of the evidence file (its name is below) is not supported by Doppel.")
    assert "Name: payload.bat" in inside.splitlines()
    no_name_text = await executor.execute_tool(
        ToolCall(
            id="f8",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "action": "upload",
                "content_text": "evidence",
            },
        )
    )
    assert "'file_name' is required" in no_name_text.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_alert_files_upload_text_body(monkeypatch):
    def router(method, url):
        return {
            "id": "ACM-1",
            "files": [{"file_name": "evidence.txt", "status": "uploaded"}],
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="f9",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "action": "upload",
                "file_name": "evidence.txt",
                "content_text": "Phishing kit found at /kit.zip",
            },
        )
    )
    assert not result.is_error
    inside, outside = fenced_and_open(result.content)
    assert outside == "Uploaded the evidence file to alert ACM-1.\n"
    assert inside.splitlines()[2:-1] == [
        "[source: Doppel file details]",
        "Name: evidence.txt",
        "file_name: evidence.txt | status: uploaded",
    ]
    call = recorder.calls[0]
    assert call["method"] == "put"
    assert "/v1/alert?" in call["url"]
    assert "id=ACM-1" in call["url"]
    body = call["json"]
    assert body["file_action"] == "upload"
    entry = body["files"][0]
    assert entry["file_name"] == "evidence.txt"
    # The payload travels base64-encoded, decoding back to the exact text.
    assert base64.b64decode(entry["file_to_upload"]) == (
        b"Phishing kit found at /kit.zip"
    )


@pytest.mark.asyncio
async def test_manage_alert_files_upload_attachment_defaults_name(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "ACM-1"})
    ctx = FileContext()
    ctx.add(FileRef.from_bytes(b"\x89PNG-bytes", "screenshot.png", "image/png"))
    file_id = ctx.list_files()[0].file_id
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(
            id="f10",
            name="manage_doppel_alert_files",
            arguments={
                "entity": "https://phish.example.com",
                "action": "upload",
                "file_id": file_id,
            },
        )
    )
    assert not result.is_error
    # file_name defaults to the attachment's own name, named in the answer
    # only inside the quarantine block.
    inside, outside = fenced_and_open(result.content)
    assert outside == f"Uploaded file '{file_id}' to alert https://phish.example.com.\n"
    assert "Name: screenshot.png" in inside.splitlines()
    call = recorder.calls[0]
    assert "entity=" in call["url"]
    entry = call["json"]["files"][0]
    assert entry["file_name"] == "screenshot.png"
    # The attached bytes are forwarded verbatim (base64-encoded).
    assert base64.b64decode(entry["file_to_upload"]) == b"\x89PNG-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_manage_alert_files_upload_names_the_file_only_inside_the_block(monkeypatch, hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    # Doppel's per-file result echoes the name: it lands in the block too.
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"id": "ACM-1", "files": [{"file_name": name, "status": "uploaded"}]}
    )
    ctx = FileContext()
    ctx.add(FileRef.from_bytes(b"evidence", name, "text/plain"))
    file_id = ctx.list_files()[0].file_id
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(
            id="f12",
            name="manage_doppel_alert_files",
            arguments={"alert_id": "ACM-1", "action": "upload", "file_id": file_id},
        )
    )
    # The extension cut from a hostile name is not Doppel's: refused, and
    # the refusal names the file by its id too.
    outside = assert_name_only_inside(result.content, name)
    assert f"file '{file_id}'" in outside
    if result.is_error:
        assert recorder.calls == []
    else:
        assert recorder.calls[0]["json"]["files"][0]["file_name"] == name


@pytest.mark.asyncio
async def test_manage_alert_files_upload_missing_attachment(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    executor.file_context = FileContext()
    result = await executor.execute_tool(
        ToolCall(
            id="f11",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "action": "upload",
                "file_id": "missing",
            },
        )
    )
    assert "not available" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_alert_files_rejects_oversize(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    # Lower the cap instead of allocating a real 50 MB buffer.
    monkeypatch.setattr(dp, "_FILE_MAX_BYTES", 8)
    result = await executor.execute_tool(
        ToolCall(
            id="f12",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "action": "upload",
                "file_name": "evidence.txt",
                "content_text": "A" * 9,
            },
        )
    )
    assert "accepts uploads up to" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_manage_alert_files_delete_body(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"id": "ACM-1"})
    result = await executor.execute_tool(
        ToolCall(
            id="f13",
            name="manage_doppel_alert_files",
            arguments={
                "alert_id": "ACM-1",
                "action": "delete",
                "file_name": "evidence.txt",
            },
        )
    )
    assert not result.is_error
    assert "'evidence.txt' deleted from alert ACM-1" in result.content
    call = recorder.calls[0]
    assert call["method"] == "put"
    assert "id=ACM-1" in call["url"]
    assert call["json"] == {
        "file_action": "delete",
        "files": [{"file_name": "evidence.txt"}],
    }


# ---------------------------------------------------------------------------
# Referrer log (write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_referrer_log_body(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"message": "Successful referrer log submission."}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c16",
            name="submit_doppel_referrer_log",
            arguments={
                "referrer_url": "https://suspicious-referrer.example.com/phish",
                "destination_url": "https://your-company.com/login",
            },
        )
    )
    assert not result.is_error
    assert "Successful referrer log submission." in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/v1/alert/referrer")
    assert call["json"] == {
        "referrer_url": "https://suspicious-referrer.example.com/phish",
        "destination_url": "https://your-company.com/login",
    }


# ---------------------------------------------------------------------------
# URL scanning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_url_submits_and_returns_scan_id(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: ({"scan_id": "3f0e6d0a-1111"}, 201)
    )
    result = await executor.execute_tool(
        ToolCall(
            id="s1",
            name="scan_doppel_url",
            arguments={
                "url": "https://phish.example.com",
                "associated_urls": "https://a.example.com, https://b.example.com",
            },
        )
    )
    assert not result.is_error
    assert "3f0e6d0a-1111" in result.content
    call = recorder.calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/v1/scan")
    assert call["json"] == {
        "url": "https://phish.example.com",
        "associated_urls": ["https://a.example.com", "https://b.example.com"],
    }


@pytest.mark.asyncio
async def test_scan_result_pending_and_completed(monkeypatch):
    responses = iter(
        [
            {"status": "PENDING", "url": "https://x.example.com", "results": []},
            {
                "status": "COMPLETED",
                "url": "https://x.example.com",
                "scan_id": "3f0e6d0a-1111",
                "results": [
                    {
                        "suspiciousness": "high",
                        "category": "phishing",
                        "created_at": "2026-07-01T10:00:00",
                        "scan_data": {"verdict_reason": "login form clone"},
                    }
                ],
            },
        ]
    )
    executor, recorder = _build(monkeypatch, lambda m, u: next(responses))
    pending = await executor.execute_tool(
        ToolCall(
            id="s2",
            name="get_doppel_scan_result",
            arguments={"scan_id": "3f0e6d0a-1111"},
        )
    )
    assert "PENDING" in pending.content
    assert "still running" in pending.content
    done = await executor.execute_tool(
        ToolCall(
            id="s3",
            name="get_doppel_scan_result",
            arguments={"scan_id": "3f0e6d0a-1111"},
        )
    )
    assert "COMPLETED" in done.content
    assert "Suspiciousness: high" in done.content
    assert "scan_id=3f0e6d0a-1111" in recorder.calls[0]["url"]


@pytest.mark.asyncio
async def test_scan_result_404_reports_not_found(monkeypatch):
    # A 404 is an expected lookup miss (not a generic HTTP error): the tool
    # must return the friendly "not found" message.
    executor, recorder = _build(
        monkeypatch, lambda m, u: ({"message": "Scan not found."}, 404)
    )
    result = await executor.execute_tool(
        ToolCall(
            id="s4",
            name="get_doppel_scan_result",
            arguments={"scan_id": "missing-scan"},
        )
    )
    assert not result.is_error
    assert "Scan missing-scan not found" in result.content
    assert recorder.calls[0]["expected_statuses"] == {404}


# ---------------------------------------------------------------------------
# Brands
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_brands_envelope_and_offset_hint(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "123e4567-e89b-12d3-a456-426614174000",
                    "name": "Acme Corp",
                    "brand_type": "organization_brand",
                    "created_at": "2025-01-01T00:00:00",
                }
            ],
            "count": 5,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="b1",
            name="list_doppel_brands",
            arguments={"name": "acme", "limit": 1, "offset": 0},
        )
    )
    assert not result.is_error
    assert "Acme Corp" in result.content
    # Full page (1 of 1 requested) -> next-offset hint.
    assert 'offset="1"' in result.content
    call = recorder.calls[0]
    assert "/v1/brands?" in call["url"]
    assert "name=acme" in call["url"]


@pytest.mark.asyncio
async def test_list_brands_no_hint_on_exact_last_page(monkeypatch):
    # count == offset + len(rows): the page is full AND final - no hint.
    def router(method, url):
        return {
            "data": [{"id": "b-1", "name": "Only Brand"}],
            "count": 1,
        }

    executor, _ = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="b1b",
            name="list_doppel_brands",
            arguments={"limit": 1, "offset": 0},
        )
    )
    assert not result.is_error
    assert "offset=" not in result.content


@pytest.mark.asyncio
async def test_list_brands_rejects_bad_type(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    result = await executor.execute_tool(
        ToolCall(
            id="b2", name="list_doppel_brands", arguments={"brand_type": "personal"}
        )
    )
    assert "brand_type must be one of" in result.content
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_delete_brand_encodes_path(monkeypatch):
    executor, recorder = _build(
        monkeypatch, lambda m, u: {"id": "123e4567-e89b-12d3-a456-426614174000"}
    )
    result = await executor.execute_tool(
        ToolCall(
            id="b3",
            name="delete_doppel_brand",
            arguments={"brand_id": "123e4567-e89b-12d3-a456-426614174000"},
        )
    )
    assert not result.is_error
    assert "archived" in result.content
    call = recorder.calls[0]
    assert call["method"] == "delete"
    assert call["url"].endswith("/v1/brands/123e4567-e89b-12d3-a456-426614174000")
    # A malicious ID cannot rewrite the request path.
    await executor.execute_tool(
        ToolCall(
            id="b4",
            name="delete_doppel_brand",
            arguments={"brand_id": "abc/../../admin"},
        )
    )
    assert "abc%2F..%2F..%2Fadmin" in recorder.calls[-1]["url"]


# ---------------------------------------------------------------------------
# Protected assets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_protected_assets_filters_and_format(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "aaaa-bbbb",
                    "asset_value": "https://linkedin.com/company/example",
                    "platform": "linkedin",
                    "brand_ids": ["123e4567"],
                    "created_at": "2025-06-01T00:00:00",
                    "updated_by": "Doppel",
                }
            ],
            "count": 1,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="p1",
            name="list_doppel_protected_assets",
            arguments={"platform_name": "linkedin"},
        )
    )
    assert not result.is_error
    assert "linkedin.com/company/example" in result.content
    assert "Found 1 protected asset" in result.content
    call = recorder.calls[0]
    assert "/v1/protected-assets?" in call["url"]
    assert "platform_name=linkedin" in call["url"]
    bad = await executor.execute_tool(
        ToolCall(
            id="p2",
            name="list_doppel_protected_assets",
            arguments={"platform_name": "myspace"},
        )
    )
    assert "platform_name must be one of" in bad.content


@pytest.mark.asyncio
async def test_create_protected_asset_body_and_guards(monkeypatch):
    executor, recorder = _build(
        monkeypatch,
        lambda m, u: (
            {
                "data": {"id": "aaaa-bbbb", "platform": "linkedin"},
                "message": "Protected asset created successfully",
            },
            201,
        ),
    )
    guarded = await executor.execute_tool(
        ToolCall(
            id="p3",
            name="create_doppel_protected_asset",
            arguments={"asset_value": "https://linkedin.com/company/example"},
        )
    )
    assert "brand_ids" in guarded.content
    assert recorder.calls == []
    too_many = await executor.execute_tool(
        ToolCall(
            id="p3b",
            name="create_doppel_protected_asset",
            arguments={
                "asset_value": "https://linkedin.com/company/example",
                "brand_ids": ",".join(f"id-{i}" for i in range(101)),
            },
        )
    )
    assert "at most 100" in too_many.content
    assert recorder.calls == []
    result = await executor.execute_tool(
        ToolCall(
            id="p4",
            name="create_doppel_protected_asset",
            arguments={
                "asset_value": "https://linkedin.com/company/example",
                "brand_ids": "123e4567, 789e0123",
            },
        )
    )
    assert not result.is_error
    assert "aaaa-bbbb" in result.content
    call = recorder.calls[-1]
    assert call["method"] == "post"
    assert call["url"].endswith("/v1/protected-asset")
    assert call["json"] == {
        "asset_value": "https://linkedin.com/company/example",
        "brand_ids": ["123e4567", "789e0123"],
    }


@pytest.mark.asyncio
async def test_delete_protected_asset(monkeypatch):
    executor, recorder = _build(monkeypatch, lambda m, u: {"message": "deleted"})
    result = await executor.execute_tool(
        ToolCall(
            id="p5",
            name="delete_doppel_protected_asset",
            arguments={"asset_id": "aaaa-bbbb"},
        )
    )
    assert not result.is_error
    assert "no longer suppress" in result.content
    call = recorder.calls[0]
    assert call["method"] == "delete"
    assert call["url"].endswith("/v1/protected-asset/aaaa-bbbb")


# ---------------------------------------------------------------------------
# Human Risk Management (phishing simulations)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_hrm_campaigns_filters_and_test_flag(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "camp-1",
                    "name": "Q3 Credential Harvest Sim",
                    "status": "finished",
                    "campaign_type": "simulation",
                    "launch_date": "2026-06-01T09:00:00",
                    "end_date": "2026-06-15T17:00:00",
                    "is_test": False,
                }
            ],
            "count": 1,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="h1",
            name="list_doppel_hrm_campaigns",
            arguments={"status": "finished"},
        )
    )
    assert not result.is_error
    assert "Q3 Credential Harvest Sim" in result.content
    call = recorder.calls[0]
    assert "/v1/hrm/campaigns?" in call["url"]
    assert "status=finished" in call["url"]
    # Test campaigns excluded by default: is_test is not sent when false.
    assert "is_test" not in call["url"]
    await executor.execute_tool(
        ToolCall(
            id="h2",
            name="list_doppel_hrm_campaigns",
            arguments={"is_test": True},
        )
    )
    assert "is_test=true" in recorder.calls[-1]["url"]
    # Invalid sort field is refused with an actionable error, no API call.
    bad_sort = await executor.execute_tool(
        ToolCall(
            id="h2b",
            name="list_doppel_hrm_campaigns",
            arguments={"sort_by": "end_date"},
        )
    )
    assert "sort_by must be one of" in bad_sort.content
    # Invalid sort direction is refused the same way.
    bad_order = await executor.execute_tool(
        ToolCall(
            id="h2c",
            name="list_doppel_hrm_campaigns",
            arguments={"sort_order": "up"},
        )
    )
    assert "sort_order must be 'asc' or 'desc'" in bad_order.content
    assert len(recorder.calls) == 2


@pytest.mark.asyncio
async def test_get_hrm_campaign_detail_and_path_encoding(monkeypatch):
    def router(method, url):
        return {
            "id": "camp-1",
            "name": "Q3 Credential Harvest Sim",
            "status": "in_progress",
            "campaign_type": "simulation",
            "is_test": False,
            "launch_date": "2026-06-01T09:00:00",
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="h3",
            name="get_doppel_hrm_campaign",
            arguments={"campaign_id": "camp-1"},
        )
    )
    assert not result.is_error
    assert "Campaign: Q3 Credential Harvest Sim" in result.content
    assert "in_progress" in result.content
    assert recorder.calls[0]["url"].endswith("/v1/hrm/campaigns/camp-1")
    # A malicious ID cannot rewrite the request path.
    await executor.execute_tool(
        ToolCall(
            id="h4",
            name="get_doppel_hrm_campaign",
            arguments={"campaign_id": "x/../y"},
        )
    )
    assert "x%2F..%2Fy" in recorder.calls[-1]["url"]


@pytest.mark.asyncio
async def test_get_hrm_campaign_404_reports_not_found(monkeypatch):
    # A 404 is an expected lookup miss (not a generic HTTP error): the tool
    # must return the friendly "not found" message.
    executor, recorder = _build(
        monkeypatch, lambda m, u: ({"message": "Campaign not found."}, 404)
    )
    result = await executor.execute_tool(
        ToolCall(
            id="h4b",
            name="get_doppel_hrm_campaign",
            arguments={"campaign_id": "camp-404"},
        )
    )
    assert not result.is_error
    assert result.content == "Campaign camp-404 not found."
    assert recorder.calls[0]["expected_statuses"] == {404}


@pytest.mark.asyncio
async def test_list_hrm_events_filters_and_user_context(monkeypatch):
    def router(method, url):
        return {
            "data": [
                {
                    "id": "evt-1",
                    "campaign_id": "camp-1",
                    "action": "data_submitted",
                    "created_at": "2026-06-02T11:22:33",
                    "sim_user_id": "u-9",
                    "user_email": "john.doe@company.com",
                    "user_first_name": "John",
                    "user_last_name": "Doe",
                    "department": "Finance",
                    "country": "FR",
                    "device_type": "mobile",
                }
            ],
            "count": 1,
        }

    executor, recorder = _build(monkeypatch, router)
    result = await executor.execute_tool(
        ToolCall(
            id="h5",
            name="list_doppel_hrm_events",
            arguments={
                "campaign_id": "camp-1",
                "action": "data_submitted",
                "department": "Finance",
            },
        )
    )
    assert not result.is_error
    assert "data_submitted by John Doe" in result.content
    assert "Dept: Finance" in result.content
    call = recorder.calls[0]
    assert "/v1/hrm/events?" in call["url"]
    assert "campaign_id=camp-1" in call["url"]
    assert "action=data_submitted" in call["url"]
    assert "department=Finance" in call["url"]
    # Invalid sort direction is refused with an actionable error, no API call.
    bad_order = await executor.execute_tool(
        ToolCall(
            id="h6",
            name="list_doppel_hrm_events",
            arguments={"sort_order": "newest"},
        )
    )
    assert "sort_order must be 'asc' or 'desc'" in bad_order.content
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# Missing required arguments: friendly guard, never a TypeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_required_args_return_friendly_errors(monkeypatch):
    # Every "required" tool parameter defaults to "" so a tool call that
    # omits it reaches the validation guard instead of raising TypeError
    # inside CallableToolExecutor (which would surface as a tool execution
    # error, breaking the provider's never-raise contract).
    executor, recorder = _build(monkeypatch, lambda m, u: {})
    cases = {
        "create_doppel_alert": "'entity'",
        "submit_doppel_referrer_log": "'referrer_url'",
        "scan_doppel_url": "'url'",
        "get_doppel_scan_result": "'scan_id'",
        "delete_doppel_brand": "'brand_id'",
        "create_doppel_protected_asset": "'asset_value'",
        "delete_doppel_protected_asset": "'asset_id'",
        "get_doppel_hrm_campaign": "'campaign_id'",
    }
    for i, (tool, needle) in enumerate(cases.items()):
        result = await executor.execute_tool(
            ToolCall(id=f"m{i}", name=tool, arguments={})
        )
        assert result.is_error, f"{tool} raised instead of guarding"
        assert result.content.startswith("Error:"), tool
        assert needle in result.content, tool
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Missing API key guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_credentials_is_friendly_error(monkeypatch):
    # Neither a V1 API key nor V2 client credentials: friendly guard, no
    # API call.
    recorder = _Recorder(lambda m, u: {})
    monkeypatch.setattr(dp, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token="https://api.doppel.com||||||", executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="c17", name="list_doppel_alerts", arguments={})
    )
    assert "credentials are not configured" in result.content.lower()
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_client_id_without_secret_falls_back_to_v1(monkeypatch):
    # V2 mode requires BOTH client credentials; with only a client_id the
    # provider stays on V1 static-key auth.
    recorder = _Recorder(lambda m, u: {"alerts": [], "metadata": {}})
    monkeypatch.setattr(dp, "safe_request", recorder)
    executor = CallableToolExecutor()
    token = "||".join(
        ["https://api.doppel.com", "org-key-123", "", "", "cid-lonely", ""]
    )
    build_tools(token=token, executor=executor)
    result = await executor.execute_tool(
        ToolCall(id="c18", name="list_doppel_alerts", arguments={})
    )
    assert not result.is_error
    call = recorder.calls[0]
    assert "/v1/alerts?" in call["url"]
    assert call["headers"]["x-api-key"] == "org-key-123"
    assert "Authorization" not in call["headers"]


# ---------------------------------------------------------------------------
# V2 OAuth client-credentials mode
# ---------------------------------------------------------------------------


def _v2_router(mints: list[str]):
    def router(method, url):
        if url.endswith("/oauth/token"):
            mints.append(url)
            return {"access_token": f"tok-{len(mints)}", "expires_in": 86400}
        return {"alerts": [], "metadata": {}}

    return router


@pytest.mark.asyncio
async def test_v2_mode_mints_token_and_uses_bearer_on_v2_paths(monkeypatch):
    mints: list[str] = []
    executor, recorder = _build_v2(
        monkeypatch, _v2_router(mints), client_id="cid-mint"
    )
    result = await executor.execute_tool(
        ToolCall(id="v1", name="list_doppel_alerts", arguments={})
    )
    assert not result.is_error
    # First call: the client-credentials exchange at the host root (no
    # version prefix), with the documented audience and grant type.
    mint = recorder.calls[0]
    assert mint["url"] == "https://api.doppel.com/oauth/token"
    assert mint["json"] == {
        "client_id": "cid-mint",
        "client_secret": "secret-xyz",
        "audience": "doppel-external",
        "grant_type": "client_credentials",
    }
    # Second call: the actual API request - bearer auth on a /v2/ path,
    # with no V1 static-key headers.
    call = recorder.calls[1]
    assert "/v2/alerts?" in call["url"]
    assert call["headers"]["Authorization"] == "Bearer tok-1"
    assert "x-api-key" not in call["headers"]


@pytest.mark.asyncio
async def test_v2_token_is_cached_across_calls(monkeypatch):
    mints: list[str] = []
    executor, recorder = _build_v2(
        monkeypatch, _v2_router(mints), client_id="cid-cache"
    )
    for i in range(2):
        result = await executor.execute_tool(
            ToolCall(id=f"v2-{i}", name="list_doppel_alerts", arguments={})
        )
        assert not result.is_error
    # One mint only: the second tool call reuses the cached bearer.
    assert len(mints) == 1
    api_calls = [c for c in recorder.calls if "/v2/alerts" in c["url"]]
    assert len(api_calls) == 2
    assert all(
        c["headers"]["Authorization"] == "Bearer tok-1" for c in api_calls
    )


@pytest.mark.asyncio
async def test_v2_token_endpoint_without_access_token_is_friendly(monkeypatch):
    executor, recorder = _build_v2(
        monkeypatch, lambda m, u: {"unexpected": True}, client_id="cid-broken"
    )
    result = await executor.execute_tool(
        ToolCall(id="v3", name="list_doppel_alerts", arguments={})
    )
    assert result.is_error
    assert "no access_token" in result.content
    # The API request itself never fired.
    assert all("/v2/alerts" not in c["url"] for c in recorder.calls)


@pytest.mark.asyncio
async def test_request_v2_token_requires_credentials(monkeypatch):
    recorder = _Recorder(lambda m, u: {})
    monkeypatch.setattr(dp, "safe_request", recorder)
    token, ttl, err = await request_doppel_v2_token(
        "https://api.doppel.com", " ", ""
    )
    assert token is None
    assert ttl == 0
    assert "client ID / client secret missing" in (err or "")
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_request_v2_token_ttl_fallback(monkeypatch):
    # expires_in missing (or invalid) falls back to the conservative TTL.
    recorder = _Recorder(
        lambda m, u: {"access_token": "tok-nofallback", "expires_in": "soon"}
    )
    monkeypatch.setattr(dp, "safe_request", recorder)
    token, ttl, err = await request_doppel_v2_token(
        "https://api.doppel.com", "cid-ttl", "sec-ttl"
    )
    assert err is None
    assert token == "tok-nofallback"
    assert ttl == dp._V2_TOKEN_FALLBACK_TTL


@pytest.mark.asyncio
async def test_validate_v2_connection_token_plus_probe(monkeypatch):
    mints: list[str] = []
    recorder = _Recorder(_v2_router(mints))
    monkeypatch.setattr(dp, "safe_request", recorder)
    ok, message = await validate_doppel_v2_connection(
        "https://api.doppel.com", "cid-validate", "sec-validate"
    )
    assert ok
    assert "API V2" in message
    # Token exchange followed by a 1-record probe with the minted bearer.
    assert recorder.calls[0]["url"].endswith("/oauth/token")
    probe = recorder.calls[1]
    assert probe["url"].endswith("/v2/alerts?page_size=1")
    assert probe["headers"]["Authorization"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_validate_v2_connection_bad_credentials(monkeypatch):
    recorder = _Recorder(lambda m, u: ({"message": "unauthorized"}, 401))
    monkeypatch.setattr(dp, "safe_request", recorder)
    ok, message = await validate_doppel_v2_connection(
        "https://api.doppel.com", "cid-bad", "sec-bad"
    )
    assert not ok
    assert "Error" in message
