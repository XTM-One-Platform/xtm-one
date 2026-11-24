"""Integration auth failures of an assignment run (#1454).

* A rejected credential (401) is recorded and its integration is not called
  again in the run; a 403 is recorded per tool and the integration's other
  tools stay callable; a clean run records nothing.
* The coverage notice says which integrations were readable.
* The report-only auth notice of a company-managed assignment with no owner
  is stored for the run's user.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.assignment_engine import _report_unquarantined_auth_failure
from app.services.integration_auth_guard import AuthGuardedExecutor, IntegrationAuthGuard
from app.tools.base import ToolCall, ToolDefinition, ToolResult


def _defn(name: str, provider: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=name, source=f"integration:{provider}")


class _Inner:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []
        self.refusals = {"some_refused": "no"}
        self.file_context: object = None

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        self.calls.append(call.name)
        return ToolResult(tool_call_id=call.id, content=self.answers[call.name])


def _guarded(
    answers: dict[str, str], defs: list[ToolDefinition]
) -> tuple[IntegrationAuthGuard, _Inner, AuthGuardedExecutor]:
    guard = IntegrationAuthGuard()
    inner = _Inner(answers)
    return guard, inner, AuthGuardedExecutor(inner, defs, guard)


@pytest.mark.asyncio
async def test_both_credentials_rejected_degrades_with_no_coverage_and_stops_calls() -> None:
    defs = [
        _defn("list_opencti_reports", "opencti"),
        _defn("list_opencti_indicators", "opencti"),
        _defn("list_openaev_scenarios", "openaev"),
    ]
    answers = {
        "list_opencti_reports": "Error: 401 Unauthorized - token rejected",
        "list_opencti_indicators": "never sent",
        "list_openaev_scenarios": "Error: authentication failed, the token is invalid",
    }
    guard, inner, executor = _guarded(answers, defs)

    await executor.execute_tool(ToolCall(id="1", name="list_opencti_reports", arguments={}))
    refused = await executor.execute_tool(ToolCall(id="2", name="list_opencti_indicators", arguments={}))
    await executor.execute_tool(ToolCall(id="3", name="list_openaev_scenarios", arguments={}))
    await executor.execute_tool(ToolCall(id="4", name="list_openaev_scenarios", arguments={}))

    # No call is sent to an integration after its credential was rejected.
    assert inner.calls == ["list_opencti_reports", "list_openaev_scenarios"]
    assert refused.is_error and refused.refused
    assert "not called" in refused.content
    assert guard.refused_calls == 2
    failures = guard.failures()
    assert [(f["integration"], f["status"], f["first_tool"]) for f in failures] == [
        ("opencti", "401", "list_opencti_reports"),
        ("openaev", "401", "list_openaev_scenarios"),
    ]
    assert guard.coverage() == {
        "readable": [],
        "partial": [],
        "forbidden": [],
        "rejected": ["openaev", "opencti"],
    }
    assert guard.unreadable()
    notice = guard.coverage_notice()
    assert "degraded" in notice
    assert "Readable: none." in notice


@pytest.mark.asyncio
async def test_forbidden_on_some_endpoints_keeps_the_rest_readable() -> None:
    defs = [_defn("list_openaev_findings", "openaev"), _defn("list_openaev_scenarios", "openaev")]
    answers = {
        "list_openaev_findings": "Error: OpenAEV API returned 403 Forbidden: permission denied for GET /api/findings",
        "list_openaev_scenarios": '[{"id": "s1"}]',
    }
    guard, inner, executor = _guarded(answers, defs)

    await executor.execute_tool(ToolCall(id="1", name="list_openaev_findings", arguments={}))
    ok = await executor.execute_tool(ToolCall(id="2", name="list_openaev_scenarios", arguments={}))

    assert not ok.is_error
    assert inner.calls == ["list_openaev_findings", "list_openaev_scenarios"]
    assert guard.failures() == [
        {
            "integration": "openaev",
            "status": "403",
            "first_tool": "list_openaev_findings",
            "tools": ["list_openaev_findings"],
            "readable": True,
        }
    ]
    assert guard.coverage()["partial"] == ["openaev"]
    assert not guard.unreadable()
    assert "forbidden (403) on list_openaev_findings" in guard.coverage_notice()


@pytest.mark.asyncio
async def test_valid_tokens_leave_no_flag() -> None:
    defs = [_defn("list_opencti_reports", "opencti")]
    guard, _inner, executor = _guarded({"list_opencti_reports": '{"reports": []}'}, defs)
    await executor.execute_tool(ToolCall(id="1", name="list_opencti_reports", arguments={}))
    assert guard.failures() == []
    assert guard.coverage_notice() == ""


@pytest.mark.asyncio
async def test_non_integration_tools_pass_through_and_attributes_are_the_inner_executor() -> None:
    guard, inner, executor = _guarded({"other": "Error: 401 Unauthorized"}, [])
    await executor.execute_tool(ToolCall(id="1", name="other", arguments={}))
    assert guard.failures() == []
    assert executor.refusals == {"some_refused": "no"}
    executor.file_context = "ctx"
    assert inner.file_context == "ctx"


@pytest.mark.asyncio
async def test_company_managed_no_owner_notice_is_stored_for_the_run_user() -> None:
    assignment = MagicMock()
    assignment.id = uuid.uuid4()
    assignment.name = "Weekly stage analyst"
    assignment.user_id = None
    assignment.is_company_managed = True
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Stage analyst"
    run_user = uuid.uuid4()

    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nested)

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.publish = AsyncMock(return_value=1)

    async def _fake_redis_client() -> AsyncMock:
        return fake_redis

    with (
        patch("app.core.redis.redis_client", new=_fake_redis_client),
        patch("app.services.mobile_push.queue_push"),
    ):
        await _report_unquarantined_auth_failure(
            db,
            assignment=assignment,
            agent=agent,
            run_id="run-fanout-1",
            reason="built-in assignment",
            recipient_id=run_user,
        )

    db.add.assert_called_once()
    stored = db.add.call_args[0][0]
    assert stored.user_id == run_user
    assert stored.activity_type == "assignment_auth_failure"
    # The notice is the run user's alone: never the company channel.
    assert [c.args[0] for c in fake_redis.publish.call_args_list] == [f"activity:{run_user}"]
