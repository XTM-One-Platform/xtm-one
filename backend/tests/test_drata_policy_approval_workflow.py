"""get_drata_policy names the approval workflow, list_drata_policy_versions the approval date.

Regression: get_drata_policy answered the policy's ``approvedAt`` alone, so an
agent could not tell who approves a policy or whether a newer version waits for
approval; list_drata_policy_versions showed no approval date. The policy now
reads its versions and its approval configuration (the approver tiers), and a
read that fails is named in the answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"

_POLICY = {
    "id": 18,
    "name": "Access Control Policy",
    "status": "ACTIVE",
    "version": "1",
    "subVersion": "2",
    "approvedAt": None,
    "owner": {"firstName": "Sally", "lastName": "Smith"},
}
_VERSIONS = {
    "data": [
        {
            "id": 40,
            "version": 1,
            "subVersion": 2,
            "policyVersionStatus": "PUBLISHED",
            "current": True,
            "approvedAt": "2026-10-01T10:00:00Z",
            "updatedAt": "2026-10-01T10:00:00Z",
        },
        {
            "id": 41,
            "version": 1,
            "subVersion": 3,
            "policyVersionStatus": "NEEDS_APPROVAL",
            "current": False,
            "approvedAt": None,
            "updatedAt": "2026-11-20T09:00:00Z",
        },
    ],
    "pagination": {"cursor": None},
}
_CONFIG = {
    "reviewGroups": [
        {
            "name": "Legal Review",
            "tier": 1,
            "consensusRule": "ALL",
            "timeline": 7,
            "approvers": [{"firstName": "Ada", "lastName": "Lovelace", "email": "ada@example.com"}],
        },
    ]
}


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status
        self.headers = {"content-type": "application/json"}

    def json(self) -> Any:
        return self._payload


def _patch(monkeypatch, routes: dict[str, Any]) -> None:
    async def _fake_safe_request(client, method, url, provider, action, *, expected_statuses=None, **kwargs):
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                if isinstance(payload, str):
                    return None, payload
                return _Resp(payload), None
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)


async def _call(name: str, args: dict) -> str:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))
    assert result.is_error is False
    return result.content if isinstance(result.content, str) else str(result.content)


@pytest.mark.asyncio
async def test_policy_names_pending_version_and_approvers(monkeypatch):
    _patch(
        monkeypatch,
        {
            "/policies/18": _POLICY,
            "/policies/18/policy-versions": _VERSIONS,
            "/policies/18/approval-configuration": _CONFIG,
        },
    )
    out = await _call("get_drata_policy", {"policy_id": 18})
    assert "Approval workflow:" in out
    assert "Latest version: 1.3 (ID: 41) status NEEDS_APPROVAL" in out
    assert "approved: not yet" in out
    assert "Versions awaiting approval: 1" in out
    assert "Tier 1 Legal Review: ALL of Ada Lovelace <ada@example.com>, 7 day(s)" in out


@pytest.mark.asyncio
async def test_policy_names_unreadable_approval_configuration(monkeypatch):
    _patch(
        monkeypatch,
        {
            "/policies/18": _POLICY,
            "/policies/18/policy-versions": _VERSIONS,
            "/policies/18/approval-configuration": "Drata 403: missing permission",
        },
    )
    out = await _call("get_drata_policy", {"policy_id": 18})
    assert "Approvers: not readable (Drata 403: missing permission)" in out
    assert "Latest version: 1.3" in out


@pytest.mark.asyncio
async def test_policy_without_tiers_says_so(monkeypatch):
    _patch(
        monkeypatch,
        {
            "/policies/18": _POLICY,
            "/policies/18/policy-versions": {"data": [], "pagination": {}},
            "/policies/18/approval-configuration": {"reviewGroups": []},
        },
    )
    out = await _call("get_drata_policy", {"policy_id": 18})
    assert "no approval tier configured" in out
    assert "Versions awaiting approval: 0" in out


@pytest.mark.asyncio
async def test_versions_show_approval_date(monkeypatch):
    _patch(monkeypatch, {"/policies/18/policy-versions": _VERSIONS})
    out = await _call("list_drata_policy_versions", {"policy_id": 18})
    assert "Approved: 2026-10-01T10:00:00Z" in out
    assert "Approved: not yet" in out
