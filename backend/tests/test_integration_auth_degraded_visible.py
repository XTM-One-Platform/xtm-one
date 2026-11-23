"""A run degraded by integration auth failures is visible everywhere (#1589).

* An integration whose every called tool answered 403 is unreadable, and its
  calls are refused after FORBIDDEN_FAIL_FAST distinct forbidden tools.
* A JSON-output agent's result carries the coverage in a ``_coverage`` key.
* ``get_run`` / ``list_runs`` lines name the failures and the degraded marker.
* The CTEM stage and briefing reads never take an unreadable run as the
  artifact, and answer ``not_assessable`` when nothing else can be shown.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.api.v1.ctem_metrics import _artifact_stale, _artifact_status, _run_unreadable
from app.services.builtin_tools.agent_builder import _run_degraded_lines
from app.services.integration_auth_guard import (
    FORBIDDEN_FAIL_FAST,
    UNREADABLE_STOP_REASON,
    AuthGuardedExecutor,
    IntegrationAuthGuard,
    auth_failures_line,
    with_coverage_key,
)
from app.tools.base import ToolCall, ToolDefinition, ToolResult

_FORBIDDEN = "Error: OpenAEV access denied: 403 Forbidden"


def _defn(name: str, provider: str) -> ToolDefinition:
    return ToolDefinition(name=name, description=name, source=f"integration:{provider}")


class _Inner:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        self.calls.append(call.name)
        return ToolResult(tool_call_id=call.id, content=self.answers[call.name])


def _guarded(answers: dict[str, str], provider: str) -> tuple[IntegrationAuthGuard, _Inner, AuthGuardedExecutor]:
    guard = IntegrationAuthGuard()
    inner = _Inner(answers)
    defs = [_defn(name, provider) for name in answers]
    return guard, inner, AuthGuardedExecutor(inner, defs, guard)


@pytest.mark.asyncio
async def test_forbidden_on_every_tool_is_unreadable_and_bounded() -> None:
    names = [f"openaev_tool_{i}" for i in range(FORBIDDEN_FAIL_FAST + 2)]
    guard, inner, executor = _guarded({n: _FORBIDDEN for n in names}, "openaev")

    for i, name in enumerate(names):
        await executor.execute_tool(ToolCall(id=str(i), name=name, arguments={}))

    # Calls stop once FORBIDDEN_FAIL_FAST distinct tools answered 403.
    assert inner.calls == names[:FORBIDDEN_FAIL_FAST]
    assert guard.refused_calls == 2
    assert guard.coverage()["forbidden"] == ["openaev"]
    assert guard.unreadable()
    assert guard.failures()[0]["readable"] is False
    assert "Forbidden (403) on every call, no data read: openaev." in guard.coverage_notice()


@pytest.mark.asyncio
async def test_rejected_and_forbidden_everywhere_is_unreadable() -> None:
    guard = IntegrationAuthGuard()
    inner = _Inner(
        {
            "opencti_reports": "Error: 401 Unauthorized - token rejected",
            "openaev_scenarios": _FORBIDDEN,
        }
    )
    executor = AuthGuardedExecutor(
        inner, [_defn("opencti_reports", "opencti"), _defn("openaev_scenarios", "openaev")], guard
    )
    await executor.execute_tool(ToolCall(id="1", name="opencti_reports", arguments={}))
    await executor.execute_tool(ToolCall(id="2", name="openaev_scenarios", arguments={}))

    assert guard.unreadable()
    record = guard.coverage_record()
    assert record["degraded"] and record["unreadable"]
    assert record["rejected"] == ["opencti"] and record["forbidden"] == ["openaev"]


@pytest.mark.asyncio
async def test_forbidden_on_one_tool_with_others_readable_stays_partial() -> None:
    guard, inner, executor = _guarded(
        {"openaev_findings": _FORBIDDEN, "openaev_scenarios": '[{"id": "s1"}]', "openaev_other": _FORBIDDEN},
        "openaev",
    )
    await executor.execute_tool(ToolCall(id="1", name="openaev_findings", arguments={}))
    await executor.execute_tool(ToolCall(id="2", name="openaev_scenarios", arguments={}))
    await executor.execute_tool(ToolCall(id="3", name="openaev_other", arguments={}))

    assert inner.calls == ["openaev_findings", "openaev_scenarios", "openaev_other"]
    assert guard.coverage()["partial"] == ["openaev"]
    assert guard.coverage()["forbidden"] == []
    assert not guard.unreadable()
    assert guard.failures()[0]["readable"] is True


@pytest.mark.asyncio
async def test_valid_tokens_unchanged() -> None:
    guard, _inner, executor = _guarded({"opencti_reports": '{"reports": []}'}, "opencti")
    await executor.execute_tool(ToolCall(id="1", name="opencti_reports", arguments={}))
    assert not guard.unreadable()
    assert guard.coverage_record()["degraded"] is False


def test_json_output_gets_a_coverage_key() -> None:
    record = {"degraded": True, "unreadable": True, "readable": [], "rejected": ["opencti"]}
    merged = with_coverage_key('{"headline": "x", "score": 40}', record)
    assert merged is not None
    assert json.loads(merged) == {"headline": "x", "score": 40, "_coverage": record}
    # Not a JSON object: left to the caller as it came.
    assert with_coverage_key("plain text", record) is None
    assert with_coverage_key("[1, 2]", record) is None
    assert with_coverage_key(None, record) is None


def test_get_run_and_list_runs_lines() -> None:
    failures = [
        {"integration": "opencti", "status": "401", "first_tool": "a", "tools": ["a"], "readable": False},
        {"integration": "openaev", "status": "403", "first_tool": "b", "tools": ["b", "c"], "readable": False},
    ]
    unreadable = SimpleNamespace(integration_auth_failures=failures, stop_reason=UNREADABLE_STOP_REASON)
    text = _run_degraded_lines(unreadable, prefix="\n")
    assert "Degraded: needs attention, no integration was readable" in text
    assert "opencti 401 (no data read; a)" in text
    assert "openaev 403 (no data read; b, c)" in text

    partial_failures = [dict(failures[1], readable=True)]
    partial = SimpleNamespace(integration_auth_failures=partial_failures, stop_reason=None)
    assert "Degraded: partial integration coverage" in _run_degraded_lines(partial, prefix="\n")

    clean = SimpleNamespace(integration_auth_failures=None, stop_reason=None)
    assert _run_degraded_lines(clean, prefix="\n") == ""
    assert auth_failures_line(None) == ""


def test_ctem_read_path_skips_unreadable_runs() -> None:
    assert _run_unreadable(SimpleNamespace(stop_reason=UNREADABLE_STOP_REASON))
    assert not _run_unreadable(SimpleNamespace(stop_reason=None))
    assert not _run_unreadable(SimpleNamespace(stop_reason="max_iterations"))

    failures = [{"integration": "opencti", "status": "401"}]
    assert _artifact_status(generating=True, artifact=None, not_assessable=failures) == "generating"
    # An earlier readable artifact still shows.
    assert _artifact_status(generating=False, artifact={"headline": "h"}, not_assessable=failures) == "ready"
    assert _artifact_status(generating=False, artifact=None, not_assessable=failures) == "not_assessable"
    assert _artifact_status(generating=False, artifact=None, not_assessable=None) is None


def test_ctem_stale_suppressed_while_newest_run_unreadable() -> None:
    """An older readable artifact past its window is not marked stale while the
    newest run read nothing, so landing never starts another blind run."""

    def always_stale(_generated_at: object) -> bool:
        return True

    failures = [{"integration": "opencti", "status": "401"}]
    artifact = {"headline": "h"}
    assert not _artifact_stale(artifact=artifact, generated_at=None, not_assessable=failures, is_stale=always_stale)
    assert _artifact_stale(artifact=artifact, generated_at=None, not_assessable=None, is_stale=always_stale)
    assert not _artifact_stale(artifact=None, generated_at=None, not_assessable=None, is_stale=always_stale)
    assert not _artifact_stale(artifact=artifact, generated_at=None, not_assessable=None, is_stale=lambda _g: False)
