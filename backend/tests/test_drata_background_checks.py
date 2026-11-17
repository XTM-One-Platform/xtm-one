"""Tests for ``list_drata_background_checks``.

Drata's public API v2 serves ``/background-checks`` for POST only (Create Manual
Background Check): a GET answers "Cannot GET". A person's background-check state
is the ``BG_CHECK`` entry of the Personnel compliance checks, so the tool reads
``GET /personnel`` with ``expand[]=user,complianceChecks``. These tests pin that
route, the BG_CHECK filtering, the ``user_id`` scoping and the explicit empty answer.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"

_PERSONNEL = [
    {
        "id": 11,
        "employmentStatus": "CURRENT_EMPLOYEE",
        "user": {"id": 101, "email": "ada@example.com", "firstName": "Ada", "lastName": "Lovelace"},
        "complianceChecks": [
            {"type": "SECURITY_TRAINING", "status": "PASS"},
            {
                "type": "BG_CHECK",
                "status": "PASS",
                "completionDate": "2026-01-15T00:00:00Z",
                "lastCheckedAt": "2026-11-01T00:00:00Z",
                "expiresAt": None,
            },
        ],
    },
    {
        "id": 12,
        "employmentStatus": "CURRENT_CONTRACTOR",
        "user": {"id": 102, "email": "alan@example.com", "firstName": "Alan", "lastName": "Turing"},
        "complianceChecks": [
            {"type": "BG_CHECK", "status": "FAIL", "exclusion": {"reason": "Contractor via agency"}},
        ],
    },
]


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.content = b"{}"

    def json(self) -> dict:
        return self._payload


def _patch(monkeypatch, personnel: list[dict]) -> list[dict]:
    calls: list[dict] = []

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        calls.append({"method": method, "url": url, "params": dict(kwargs.get("params") or {})})
        return _FakeResp({"data": personnel, "pagination": {"cursor": None}}), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


async def _call(args: dict) -> str:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="list_drata_background_checks", arguments=args))
    return result.content


@pytest.mark.asyncio
async def test_reads_personnel_compliance_checks_not_the_post_only_route(monkeypatch):
    calls = _patch(monkeypatch, _PERSONNEL)
    out = await _call({})
    assert len(calls) == 1
    assert calls[0]["method"] == "get"
    assert calls[0]["url"] == "https://public-api.drata.com/public/v2/personnel"
    assert calls[0]["params"]["expand[]"] == ["user", "complianceChecks"]
    assert "Found 2 background check(s)" in out
    assert "Ada Lovelace (ada@example.com, user 101)" in out
    assert "Status: PASS" in out
    assert "Completed: 2026-01-15T00:00:00Z" in out
    assert "Excluded: Contractor via agency" in out
    assert "SECURITY_TRAINING" not in out


@pytest.mark.asyncio
async def test_user_id_scopes_to_one_person(monkeypatch):
    _patch(monkeypatch, _PERSONNEL)
    out = await _call({"user_id": 102})
    assert "Found 1 background check(s)" in out
    assert "Alan Turing" in out
    assert "Ada Lovelace" not in out


@pytest.mark.asyncio
async def test_no_bg_check_is_said_not_silent(monkeypatch):
    _patch(monkeypatch, [{**_PERSONNEL[0], "complianceChecks": [{"type": "SECURITY_TRAINING", "status": "PASS"}]}])
    out = await _call({})
    assert out.startswith("No background-check compliance found: 1 personnel read")


def test_description_names_the_fields_returned():
    executor = CallableToolExecutor()
    tools = {t.name: t for t in drata.build_tools(_TOKEN, executor)}
    description = tools["list_drata_background_checks"].description
    assert "BG_CHECK" in description
    assert len(description) <= 200
