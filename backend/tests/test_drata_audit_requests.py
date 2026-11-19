"""Tests for ``list_drata_audit_requests`` and ``get_drata_audit_request``.

Drata public API v2, tag "Audit Requests": ``GET /workspaces/{workspaceId}/audits/{auditId}/requests``
and ``.../requests/{requestId}``. Before these tools an agent tracking an audit to closure read
each request's status and the auditor's messages through the Drata UI on a runner. The fixtures
follow the published response samples with invented values: no customer or person data.
"""

from __future__ import annotations

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"
_BASE = "https://public-api.drata.com/public/v2"
_AUDIT = "aaaaaaaa-bbbb-0000-cccc-dddddddddddd"

_REQUEST = {
    "id": 1,
    "code": "REQ-001",
    "title": "Provide SOC 2 report",
    "description": "Please provide the latest SOC 2 Type II report.",
    "status": "CHANGES_REQUESTED",
    "auditId": _AUDIT,
    "createdAt": "2026-11-01T10:00:00.000Z",
    "updatedAt": "2026-11-02T10:00:00.000Z",
    "owners": [{"id": 7, "email": "owner@example.com", "firstName": "Example", "lastName": "Owner"}],
    "messages": [
        {
            "id": 2,
            "message": "Uploaded the report.",
            "sentByEmail": "owner@example.com",
            "createdAt": "2026-11-02T09:00:00.000Z",
        },
        {
            "id": 1,
            "message": "Please provide the latest report.",
            "sentByEmail": "auditor@example.com",
            "createdAt": "2026-11-01T11:00:00.000Z",
        },
    ],
    "controls": [{"id": 3, "code": "DCF-1001", "name": "Vulnerability Management", "controlNumber": 1}],
}

_OTHER = {
    "id": 2,
    "code": "REQ-002",
    "title": "Provide the access review",
    "status": "ACCEPTED",
    "auditId": _AUDIT,
    "updatedAt": "2026-11-03T10:00:00.000Z",
    "owners": [],
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


async def _call(name: str, args: dict) -> str:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))
    assert result.is_error is False, result.content
    return result.content


async def test_list_sends_the_audit_path_filters_and_expand(monkeypatch):
    calls = _patch(monkeypatch, [{"data": [_REQUEST], "pagination": {"cursor": None}}])
    await _call(
        "list_drata_audit_requests",
        {
            "audit_id": _AUDIT,
            "status": ["changes_requested", "OUTSTANDING"],
            "owner_ids": [7],
            "include_messages": True,
        },
    )
    assert calls[0]["method"] == "get"
    assert calls[0]["url"] == f"{_BASE}/workspaces/1/audits/{_AUDIT}/requests"
    params = calls[0]["params"]
    assert params["status[]"] == ["CHANGES_REQUESTED", "OUTSTANDING"]
    assert params["userIds[]"] == [7]
    assert params["expand[]"] == ["owners", "messages"]


async def test_list_renders_status_owners_counts_and_ordered_messages(monkeypatch):
    _patch(monkeypatch, [{"data": [_REQUEST, _OTHER], "pagination": {"cursor": None}}])
    out = await _call("list_drata_audit_requests", {"audit_id": _AUDIT, "include_messages": True})
    assert "Found 2 audit request(s): ACCEPTED 1, CHANGES_REQUESTED 1" in out
    assert "[1] REQ-001 | Status: CHANGES_REQUESTED | Provide SOC 2 report" in out
    assert "Owners: Example Owner <owner@example.com>" in out
    assert "Owners: none" in out
    first = out.index("Please provide the latest report.")
    second = out.index("Uploaded the report.")
    assert first < second


async def test_list_without_messages_asks_only_for_owners(monkeypatch):
    calls = _patch(monkeypatch, [{"data": [_REQUEST], "pagination": {"cursor": None}}])
    out = await _call("list_drata_audit_requests", {"audit_id": _AUDIT})
    assert calls[0]["params"]["expand[]"] == ["owners"]
    assert "status[]" not in calls[0]["params"]
    assert "Messages" not in out


async def test_list_cuts_long_messages(monkeypatch):
    long = dict(_REQUEST, messages=[{"message": "x" * 1000, "sentByEmail": "a@example.com", "createdAt": "t"}])
    _patch(monkeypatch, [{"data": [long], "pagination": {"cursor": None}}])
    out = await _call("list_drata_audit_requests", {"audit_id": _AUDIT, "include_messages": True})
    assert "x" * 300 + "..." in out
    assert "x" * 301 not in out


async def test_list_refuses_an_unknown_status_before_any_request(monkeypatch):
    calls = _patch(monkeypatch, [{"data": [], "pagination": {"cursor": None}}])
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_drata_audit_requests", arguments={"audit_id": _AUDIT, "status": ["PREPARED"]})
    )
    assert result.is_error is True
    out = result.content
    assert "unknown audit request status PREPARED" in out
    assert "OUTSTANDING" in out
    assert calls == []


async def test_list_follows_the_cursor(monkeypatch):
    calls = _patch(
        monkeypatch,
        [
            {"data": [_REQUEST], "pagination": {"cursor": "next"}},
            {"data": [_OTHER], "pagination": {"cursor": None}},
        ],
    )
    out = await _call("list_drata_audit_requests", {"audit_id": _AUDIT})
    assert len(calls) == 2
    assert calls[1]["params"]["cursor"] == "next"
    assert "Found 2 audit request(s)" in out


async def test_list_empty(monkeypatch):
    _patch(monkeypatch, [{"data": [], "pagination": {"cursor": None}}])
    assert await _call("list_drata_audit_requests", {"audit_id": _AUDIT}) == "No audit requests found."


async def test_get_reads_one_request_with_messages_and_controls(monkeypatch):
    calls = _patch(monkeypatch, [_REQUEST])
    out = await _call("get_drata_audit_request", {"audit_id": _AUDIT, "request_id": 1, "workspace_id": 3})
    assert calls[0]["url"] == f"{_BASE}/workspaces/3/audits/{_AUDIT}/requests/1"
    assert calls[0]["params"]["expand[]"] == ["owners", "messages", "controls"]
    assert "Audit request REQ-001 (ID: 1)" in out
    assert "Status: CHANGES_REQUESTED" in out
    assert "Please provide the latest SOC 2 Type II report." in out
    assert "Messages (2):" in out
    assert "auditor@example.com: Please provide the latest report." in out
    assert "[DCF-1001] Vulnerability Management" in out
