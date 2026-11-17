"""Tests for the Drata control external-evidence tooling (v1 API).

External evidence is a SEPARATE object family from the v2 Evidence Library:
records mapped directly to a control. These tools are the cleanup path for
evidence left dangling on controls after the Evidence Library was purged. The
endpoints live on the Drata v1 API host, derived
from the configured v2 base URL.
"""

from __future__ import annotations

import base64

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"


class _Resp:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.content = b"{}"
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def _patch(monkeypatch, payload: dict, status_code: int = 200) -> list[dict]:
    calls: list[dict] = []

    async def _fake_safe_request(
        client, method, url, provider, action, *, expected_statuses=None, **kwargs
    ):
        calls.append({"method": method, "url": url, "kwargs": kwargs})
        return _Resp(payload, status_code), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


def _build_executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    return executor


async def _call(executor, name, args):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))


# ---------------------------------------------------------------------------
# v1 base-URL derivation
# ---------------------------------------------------------------------------


def test_to_v1_base_url_drops_version_segment():
    # Drata's v1 API is the UNVERSIONED /public base (NOT /public/v1, which 404s).
    # The /v2 segment is dropped, not replaced.
    assert (
        drata_common.to_v1_base_url("https://public-api.drata.com/public/v2")
        == "https://public-api.drata.com/public"
    )
    # Trailing slash + EU host preserved.
    assert (
        drata_common.to_v1_base_url("https://public-api.eu.drata.com/public/v2/")
        == "https://public-api.eu.drata.com/public"
    )
    # Unknown shape -> returned unchanged (just stripped).
    assert drata_common.to_v1_base_url("https://x.example/api") == "https://x.example/api"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_external_evidence_hits_v1_and_surfaces_ids(monkeypatch):
    calls = _patch(
        monkeypatch,
        {
            "data": [
                {
                    "id": 454,
                    "name": "Filigran Entra ID Security - Password protection",
                    "file": "/path/to/file.pdf",
                    "renewalDate": "2024-09-01",
                    "renewalScheduleType": "ONE_YEAR",
                    "isExpired": True,
                }
            ],
            "total": 1,
        },
    )
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_control_external_evidence", {"control_id": 339}
    )

    assert result.is_error is False
    # v1 host + control-scoped path, page/limit pagination.
    assert calls[0]["method"] == "get"
    assert calls[0]["url"].endswith("/public/controls/339/external-evidence")
    params = calls[0]["kwargs"]["params"]
    assert params["page"] == 1 and params["limit"] == 50
    assert "[454]" in result.content
    assert "EXPIRED" in result.content


@pytest.mark.asyncio
async def test_list_external_evidence_requires_control_id(monkeypatch):
    calls = _patch(monkeypatch, {"data": [], "total": 0})
    executor = _build_executor()
    result = await _call(
        executor, "list_drata_control_external_evidence", {"control_id": 0}
    )
    assert "control_id" in result.content
    assert not calls


# ---------------------------------------------------------------------------
# delete (the purge path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_external_evidence_hits_v1(monkeypatch):
    calls = _patch(monkeypatch, {"id": 454})
    executor = _build_executor()
    result = await _call(
        executor, "delete_drata_control_external_evidence", {"external_evidence_id": 454}
    )

    assert result.is_error is False
    assert calls[0]["method"] == "delete"
    assert calls[0]["url"].endswith("/public/external-evidence/454")
    assert "deleted" in result.content.lower()


@pytest.mark.asyncio
async def test_delete_external_evidence_requires_id(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor, "delete_drata_control_external_evidence", {"external_evidence_id": 0}
    )
    assert "external_evidence_id" in result.content
    assert not calls


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_external_evidence_download_returns_signed_url(monkeypatch):
    calls = _patch(monkeypatch, {"signedUrl": "https://files.drata.example/abc"})
    executor = _build_executor()
    result = await _call(
        executor, "get_drata_external_evidence_download", {"external_evidence_id": 454}
    )

    assert result.is_error is False
    assert calls[0]["url"].endswith("/public/external-evidence/454/download")
    assert "https://files.drata.example/abc" in result.content


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_external_evidence_url_artifact(monkeypatch):
    calls = _patch(monkeypatch, {"id": 339})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_control_external_evidence",
        {
            "workspace_id": 1,
            "control_id": 339,
            "name": "Pentest report",
            "source_url": "https://example.com/report.pdf",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    assert call["url"].endswith("/public/workspaces/1/controls/339/external-evidence")
    # URL artifacts post as JSON (the v1 endpoint accepts application/json),
    # not a form body, and carry no binary file part.
    body = call["kwargs"]["json"]
    assert body["url"] == "https://example.com/report.pdf"
    assert body["renewalScheduleType"] == "ONE_YEAR"
    # creationDate + a derived renewalDate are always sent (v1 requires them).
    assert body["creationDate"] and body["renewalDate"]
    assert "files" not in call["kwargs"]
    assert "data" not in call["kwargs"]


@pytest.mark.asyncio
async def test_upload_external_evidence_file_artifact_multipart(monkeypatch):
    calls = _patch(monkeypatch, {"id": 339})
    executor = _build_executor()
    payload = base64.b64encode(b"orphan cleanup evidence").decode()
    result = await _call(
        executor,
        "upload_drata_control_external_evidence",
        {
            "workspace_id": 1,
            "control_id": 339,
            "name": "evidence",
            "file_content": payload,
            "file_name": "evidence.pdf",
            "file_encoding": "base64",
            "renewal_schedule_type": "SIX_MONTHS",
        },
    )

    assert result.is_error is False
    call = calls[0]
    assert call["method"] == "post"
    files = call["kwargs"]["files"]
    assert "file" in files
    assert call["kwargs"]["data"]["renewalScheduleType"] == "SIX_MONTHS"


@pytest.mark.asyncio
async def test_upload_external_evidence_rejects_multiple_sources(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_control_external_evidence",
        {
            "workspace_id": 1,
            "control_id": 339,
            "source_url": "https://example.com/a.pdf",
            "file_content": "abc",
        },
    )
    assert "exactly one artifact source" in result.content
    assert not calls


@pytest.mark.asyncio
async def test_upload_external_evidence_custom_schedule_requires_renewal_date(monkeypatch):
    calls = _patch(monkeypatch, {})
    executor = _build_executor()
    result = await _call(
        executor,
        "upload_drata_control_external_evidence",
        {
            "workspace_id": 1,
            "control_id": 339,
            "source_url": "https://example.com/a.pdf",
            "renewal_schedule_type": "CUSTOM",
        },
    )
    assert "renewal_date is required" in result.content
    assert not calls


# ---------------------------------------------------------------------------
# get_drata_control now surfaces the external-evidence IDs (not just counts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_control_surfaces_external_evidence_ids(monkeypatch):
    _patch(
        monkeypatch,
        {
            "id": 339,
            "code": "DCF-339",
            "name": "Account Lockout after Failed Logins",
            "archivedAt": None,
            "flags": {},
            "evidenceIds": {
                "validExternalEvidenceIds": [454],
                "invalidExternalEvidenceIds": [12, 99],
            },
        },
    )
    executor = _build_executor()
    result = await _call(
        executor, "get_drata_control", {"workspace_id": 1, "control_id": 339}
    )

    assert result.is_error is False
    assert "Valid external evidence IDs: 454" in result.content
    assert "Invalid external evidence IDs" in result.content
    assert "12, 99" in result.content
