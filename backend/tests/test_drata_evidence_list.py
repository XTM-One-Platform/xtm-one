"""list_drata_evidence reads Drata v2 List Evidence Items with a real status filter.

The tool used to call the legacy ``/evidence-library`` list with ``status=``:
Drata ignored the unknown parameter (the legacy list only knows ``statuses[]``),
returned the first 500 items unfiltered, and every row read
``Type: N/A | Status: N/A | Renewal: N/A`` because that list carries no
``status`` and nests the renewal under ``renewalSchema``.

The fixture is the documented response sample of
``GET /workspaces/{workspaceId}/evidence`` (Drata v2 reference), with the
person replaced by a placeholder.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.integrations.providers.drata import evidence as drata_evidence
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"


def _item(item_id: int, status: str, renewal: str | None = "2026-12-01") -> dict:
    """One evidence item in the documented List Evidence Items shape."""
    return {
        "id": item_id,
        "name": f"Evidence {item_id}",
        "description": "Placeholder description.",
        "evidenceTemplateCode": "REQ-12",
        "createdAt": "2025-07-01T16:45:55.246Z",
        "updatedAt": "2025-07-01T16:45:55.246Z",
        "renewalDate": renewal,
        "renewalScheduleType": "ONE_YEAR" if renewal else None,
        "status": status,
        "safeBaseSyncStatus": "COMPLETE",
        "owners": {
            "data": [
                {
                    "id": 1,
                    "email": "owner@example.com",
                    "firstName": "Test",
                    "lastName": "Owner",
                }
            ],
            "totalCount": 1,
        },
        "artifacts": {
            "data": [
                {
                    "id": 10 + item_id,
                    "artifactName": "artifact.pdf",
                    "artifactType": "S3_FILE",
                    "filedAt": "2025-07-01T16:45:55.246Z",
                }
            ],
            "totalCount": 1,
        },
    }


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _patch_pages(monkeypatch, pages: list[dict]) -> list[dict]:
    """Serve *pages* in order to ``safe_request`` and record each call."""
    calls: list[dict] = []

    async def _fake(client, method, url, provider, action, *, expected_statuses=None, **kwargs):
        calls.append({"method": method, "url": url, "params": dict(kwargs.get("params") or {})})
        return _Resp(pages[len(calls) - 1]), None

    monkeypatch.setattr(drata_common, "safe_request", _fake)
    return calls


async def _list(args: dict) -> str:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="list_drata_evidence", arguments=args))
    return result.content


@pytest.mark.asyncio
async def test_status_filter_is_sent_as_evidence_statuses_to_list_evidence_items(monkeypatch):
    calls = _patch_pages(
        monkeypatch,
        [{"data": [_item(1, "EXPIRING_SOON"), _item(2, "EXPIRED")], "pagination": {"cursor": None}}],
    )

    out = await _list({"status": "NEEDS_RENEWAL"})

    assert calls[0]["url"].endswith("/workspaces/1/evidence")
    params = calls[0]["params"]
    assert params["evidenceStatuses[]"] == ["EXPIRING_SOON", "EXPIRED"]
    assert "status" not in params
    assert set(params["expand[]"]) == {"owners", "artifacts"}
    assert out.startswith("Found 2 evidence items with status EXPIRING_SOON or EXPIRED (complete")


@pytest.mark.asyncio
async def test_rows_map_status_renewal_owner_and_artifact_type(monkeypatch):
    _patch_pages(monkeypatch, [{"data": [_item(7, "READY")], "pagination": {}}])

    out = await _list({})

    row = out.splitlines()[1]
    assert row == (
        "[7] Evidence 7 | Status: READY | Renewal: 2026-12-01 (ONE_YEAR) | Owner: Test Owner | Artifacts: S3_FILE"
    )
    assert "N/A" not in row


@pytest.mark.asyncio
async def test_every_page_is_read_past_500_and_the_count_is_exact(monkeypatch):
    first = [_item(i, "READY") for i in range(500)]
    second = [_item(500 + i, "READY") for i in range(120)]
    calls = _patch_pages(
        monkeypatch,
        [
            {"data": first, "pagination": {"cursor": "next"}},
            {"data": second, "pagination": {"cursor": None}},
        ],
    )

    out = await _list({})

    assert len(calls) == 2
    assert calls[0]["params"]["size"] == 500
    assert calls[1]["params"]["cursor"] == "next"
    assert out.startswith("Found 620 evidence items (complete: every page read).")


@pytest.mark.asyncio
async def test_a_cut_read_says_incomplete(monkeypatch):
    monkeypatch.setattr(drata_evidence, "_EVIDENCE_LIST_MAX_ITEMS", 2)
    _patch_pages(monkeypatch, [{"data": [_item(1, "READY"), _item(2, "READY")], "pagination": {"cursor": "more"}}])

    out = await _list({})

    assert out.startswith("Found 2 evidence items (INCOMPLETE")


@pytest.mark.asyncio
async def test_unknown_status_is_refused_before_any_request(monkeypatch):
    calls = _patch_pages(monkeypatch, [])

    out = await _list({"status": "STALE"})

    assert calls == []
    assert "unknown evidence status 'STALE'" in out
    assert "EXPIRING_SOON" in out


@pytest.mark.asyncio
async def test_renewal_window_filters_over_all_items(monkeypatch):
    _patch_pages(
        monkeypatch,
        [
            {
                "data": [
                    _item(1, "READY", "2026-11-20"),
                    _item(2, "READY", "2027-03-01"),
                    _item(3, "NEEDS_ARTIFACT", None),
                ],
                "pagination": {},
            }
        ],
    )

    out = await _list({"renewal_before": "2026-12-31", "renewal_after": "2026-11-01"})

    assert out.startswith("Found 1 evidence item (complete")
    assert "[1] Evidence 1" in out
    assert "[2]" not in out and "[3]" not in out


@pytest.mark.asyncio
async def test_bad_renewal_date_is_refused(monkeypatch):
    calls = _patch_pages(monkeypatch, [])

    out = await _list({"renewal_before": "next month"})

    assert calls == []
    assert "renewal_before must be a date YYYY-MM-DD" in out


@pytest.mark.asyncio
async def test_empty_result_names_the_filter(monkeypatch):
    _patch_pages(monkeypatch, [{"data": [], "pagination": {}}])

    out = await _list({"status": "expired"})

    assert out == "No evidence items found with status EXPIRED (complete)."
