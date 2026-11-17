"""Tests for ``list_drata_audit_events`` (Drata ``GET /public/v2/events``).

Observed live on 2026-11-07: every event rendered as
``[<id>] ? — Actor: N/A | Resource: N/A#? | At: <timestamp>`` because the tool
read ``eventType`` / ``actorEmail`` / ``resourceType`` / ``resourceId`` and sent
``eventType`` / ``actorId``, none of which exist on the endpoint. The fixture
below follows ``EventResponsePublicV2Dto`` of Drata's published v2 OpenAPI
(``EventsPublicV2Controller_listEvents``) with invented values: no customer
or person data.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.integrations.providers.drata.account import _format_event
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"
_BASE = "https://public-api.drata.com/public/v2"

_USER_EVENT = {
    "id": "aaaaaaaa-bbbb-0000-cccc-000000000001",
    "type": "COMPANY_DATA_UPDATED",
    "category": "COMPANY",
    "source": "APP",
    "description": "Example User updated the company info.",
    "status": None,
    "createdAt": "2026-11-07T06:40:00.000Z",
    "userId": 7,
    "user": {"id": 7, "email": "user@example.com", "firstName": "Example", "lastName": "User"},
    "connectionId": None,
    "connection": None,
    "requestDescription": None,
    "metadata": {"targetEntity": {"id": 12, "name": "Example Company"}},
}

_CONNECTION_EVENT = {
    "id": "aaaaaaaa-bbbb-0000-cccc-000000000002",
    "type": "AUTOPILOT_TEST_RUN",
    "category": "AUTOPILOT",
    "source": "AUTOPILOT",
    "description": "Autopilot ran the test.",
    "status": "PASSED",
    "createdAt": "2026-11-07T06:30:00.000Z",
    "userId": None,
    "user": None,
    "connectionId": 3,
    "connection": {"id": 3, "clientType": "AWS", "clientAlias": "prod-account", "state": "ACTIVE"},
    "requestDescription": "Data was fetched from https://example.com/api",
    "metadata": {"targetEntity": None, "targetEntities": [{"id": 1}, {"id": 2}]},
}

_SYSTEM_EVENT = {
    "id": "aaaaaaaa-bbbb-0000-cccc-000000000003",
    "type": "REMINDER_EMAIL_SENT",
    "category": "PERSONNEL",
    "source": "SCHEDULED",
    "description": "",
    "status": None,
    "createdAt": "2026-11-07T06:00:00.000Z",
    "userId": None,
    "connectionId": None,
    "requestDescription": None,
}


class _Resp:
    is_success = True
    status_code = 200

    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


def _patch(monkeypatch, pages: list[dict]) -> list[dict]:
    calls: list[dict] = []
    queue = list(pages)

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        calls.append({"method": method, "url": url, "params": dict(kwargs.get("params") or {})})
        return _Resp(queue.pop(0) if len(queue) > 1 else queue[0]), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)
    return calls


async def _call(args: dict) -> str:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="list_drata_audit_events", arguments=args))
    assert result.is_error is False, result.content
    return result.content


def test_format_maps_type_actor_target_and_description():
    line = _format_event(_USER_EVENT)
    assert "COMPANY_DATA_UPDATED (COMPANY/APP)" in line
    assert "Actor: Example User <user@example.com> (user #7)" in line
    assert "Target: Example Company (#12)" in line
    assert "At: 2026-11-07T06:40:00.000Z" in line
    assert "Example User updated the company info." in line
    assert "?" not in line.split("\n")[0]
    assert "N/A" not in line


def test_format_connection_actor_and_result():
    line = _format_event(_CONNECTION_EVENT)
    assert "AUTOPILOT_TEST_RUN (AUTOPILOT/AUTOPILOT)" in line
    assert "Actor: AWS prod-account (connection #3)" in line
    assert "Target: 2 entities" in line
    assert "Result: PASSED" in line


def test_format_system_event_without_actor_or_expansion():
    line = _format_event(_SYSTEM_EVENT)
    assert "Actor: system (SCHEDULED)" in line
    assert "Target:" not in line
    assert "\n" not in line


def test_format_unexpanded_ids_and_long_description():
    event = {**_SYSTEM_EVENT, "userId": 9, "description": "x " * 300}
    line = _format_event(event)
    assert "Actor: user #9" in line
    assert line.endswith("...")
    assert len(line.split("\n    ")[1]) <= 200


@pytest.mark.asyncio
async def test_no_args_sends_real_params_and_renders_types(monkeypatch):
    calls = _patch(monkeypatch, [{"data": [_USER_EVENT, _CONNECTION_EVENT], "pagination": {"cursor": None}}])
    out = await _call({})
    assert calls[0]["url"] == f"{_BASE}/events"
    params = calls[0]["params"]
    assert params["expand[]"] == ["user", "connection", "metadata"]
    assert params["sortDir"] == "DESC" and params["sort"] == "createdAt"
    assert params["size"] == 50
    assert "Found 2 audit events" in out
    assert "COMPANY_DATA_UPDATED" in out and "AUTOPILOT_TEST_RUN" in out
    assert "] ? " not in out


@pytest.mark.asyncio
async def test_filters_use_the_api_parameter_names(monkeypatch):
    calls = _patch(monkeypatch, [{"data": [_USER_EVENT], "pagination": {"cursor": None}}])
    await _call(
        {
            "event_type": "company_data_updated",
            "actor_id": 7,
            "category": "company",
            "source": "app",
            "connection_id": 3,
            "since": "2026-11-01T00:00:00Z",
            "until": "2026-11-07T00:00:00Z",
            "limit": 10,
        }
    )
    params = calls[0]["params"]
    assert params["type"] == "COMPANY_DATA_UPDATED"
    assert params["userId"] == 7
    assert params["category"] == "COMPANY"
    assert params["source"] == "APP"
    assert params["connectionId"] == 3
    assert params["createdAtStartDate"] == "2026-11-01T00:00:00Z"
    assert params["createdAtEndDate"] == "2026-11-07T00:00:00Z"
    assert params["size"] == 10
    for legacy in ("eventType", "actorId"):
        assert legacy not in params


@pytest.mark.asyncio
async def test_limit_is_capped_and_truncation_is_said(monkeypatch):
    calls = _patch(monkeypatch, [{"data": [_USER_EVENT, _SYSTEM_EVENT], "pagination": {"cursor": "next"}}])
    out = await _call({"limit": 2})
    assert len(calls) == 1
    assert "More events match: showing the newest 2" in out
    calls = _patch(monkeypatch, [{"data": [], "pagination": {"cursor": None}}])
    out = await _call({"limit": 100000})
    assert calls[0]["params"]["size"] == 500
    assert out == "No audit events found."
