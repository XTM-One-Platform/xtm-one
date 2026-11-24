"""JSM approvals: each approver's own decision, and the step's state after an answer.

A step whose approvers come from several groups (IT and Legal) stays pending
until every group has answered; ``answer_approval`` used to answer
"approved" on any 2xx and ``list_approvals`` printed the final decision alone,
so an approver could not see that the step still waited for another group.
"""

from typing import Any

import pytest

from app.integrations.providers.jira import _common as jira_common
from app.integrations.providers.jira import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall


class _Resp:
    def __init__(self, payload: Any):
        self._payload = payload
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


def _route(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []

    async def _fake(
        client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[_Resp | None, str | None]:
        seen.append((method, url))
        for suffix, payload in routes.items():
            if url.endswith(suffix):
                return _Resp(payload), None
        return None, f"Error: unexpected call {url}"

    monkeypatch.setattr(jira_common, "safe_request", _fake)
    return seen


async def _call(**arguments: Any) -> str:
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-123||token", executor)
    result = await executor.execute_tool(ToolCall(id="t", name="manage_jira_service_request", arguments=arguments))
    return result.content


def _step(final: str | None, decisions: list[tuple[str, str, str | None]]) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": "7",
        "name": "IT and Legal approval",
        "canAnswerApprovalDecision": final is None,
        "approvers": [
            {"approver": {"accountId": acc, "displayName": name}, "approverDecision": dec}
            for acc, name, dec in decisions
        ],
    }
    if final:
        step["finalDecision"] = final
    return step


@pytest.mark.asyncio
async def test_list_approvals_shows_each_approver_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    step = _step(None, [("a-it", "IT Lead", "approved"), ("a-legal", "Legal", "pending")])
    _route(monkeypatch, {"/request/SD-1/approval": {"values": [step]}})
    content = await _call(action="list_approvals", issue_key="SD-1")
    assert "#7 IT and Legal approval - final decision: pending" in content
    assert "IT Lead (a-it): approved" in content
    assert "Legal (a-legal): pending" in content
    assert "you can answer: yes" in content


@pytest.mark.asyncio
async def test_answer_approval_reports_a_step_still_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    step = _step(None, [("a-it", "IT Lead", "pending"), ("a-legal", "Legal", "approved")])
    seen = _route(monkeypatch, {"/request/SD-1/approval/7": step})
    content = await _call(action="answer_approval", issue_key="SD-1", approval_id="7", decision="approve")
    assert [method for method, _ in seen] == ["post"]
    assert "your decision 'approve' was recorded" in content
    assert "final decision is pending" in content
    assert "still waits for other approvers" in content
    assert "IT Lead (a-it): pending" in content
    assert "approved." not in content.splitlines()[0]


@pytest.mark.asyncio
async def test_answer_approval_reports_the_final_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    step = _step("approved", [("a-legal", "Legal", "approved")])
    _route(monkeypatch, {"/request/SD-1/approval/7": step})
    content = await _call(action="answer_approval", issue_key="SD-1", approval_id="7", decision="approve")
    assert "the step's final decision is approved" in content
    assert "still waits" not in content


@pytest.mark.asyncio
async def test_answer_approval_without_a_step_body_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _route(monkeypatch, {"/request/SD-1/approval/7": {}})
    content = await _call(action="answer_approval", issue_key="SD-1", approval_id="7", decision="decline")
    assert "your decision 'decline' was sent" in content
    assert "list_approvals" in content
