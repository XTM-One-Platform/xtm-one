"""OpenCRQ module health reads (#101 S15b-2d, ``dev-docs/platform-health.md`` 13.5)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.integrations.providers import opencrq
from app.integrations.providers.opencrq import _common as opencrq_common
from app.integrations.providers.opencrq import health_reads
from app.services.health.module_analyst import MODULE_CHECKER_TOOLS
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://crq.example||token"
TENANT = "tenant-1"
NAMES = ("get_opencrq_data_health", "get_opencrq_platform_health")


class _Fake:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, Any, bool]] = []

    async def __call__(
        self,
        base_url: str,
        headers: dict[str, str],
        procedure: str,
        action: str,
        *,
        input: Any = None,
        mutation: bool = False,
        tenant_id: str = "",
        client: Any = None,
    ) -> tuple[Any, str | None]:
        self.calls.append((procedure, input, mutation))
        answer = self.answers.get(procedure)
        if callable(answer):
            answer = answer(input)
        if isinstance(answer, str):
            return None, answer
        return answer, None


async def _call(fake: _Fake, name: str) -> str:
    executor = CallableToolExecutor()
    opencrq.build_tools(TOKEN, executor)
    with patch.object(health_reads, "_trpc", fake), patch.object(opencrq_common, "_trpc", fake):
        result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments={"tenant_id": TENANT}))
    assert not result.is_error, result.content
    return result.content


def test_the_reads_are_offered_and_allow_listed() -> None:
    executor = CallableToolExecutor()
    names = {tool.name for tool in opencrq.build_tools(TOKEN, executor)}
    assert set(NAMES) <= names
    assert set(NAMES) <= set(MODULE_CHECKER_TOOLS["opencrq"])


@pytest.mark.parametrize("name", NAMES)
async def test_every_call_is_a_query(name: str) -> None:
    fake = _Fake(
        {
            "jobs.listAllRuns": {"items": [], "total": 0},
            "riskScenarios.getRiskScenariosOverview": [],
            "connectors.listConnectors": [],
        }
    )
    await _call(fake, name)
    assert fake.calls and not any(mutation for _, _, mutation in fake.calls)
    assert all(i["limit"] == 1 for p, i, _ in fake.calls if p == "jobs.listAllRuns")


async def test_data_health_counts_readiness_and_simulations() -> None:
    scenarios = [
        {
            "readiness": {"status": "ready", "missing": [], "outdated": []},
            "latestRun": {"createdAt": "2026-11-01T10:00:00Z"},
        },
        {
            "readiness": {"status": "outdated", "missing": [], "outdated": ["tef"]},
            "latestRun": {"createdAt": "2026-10-01T10:00:00Z"},
        },
        {"readiness": {"status": "missing", "missing": ["financial_profile"], "outdated": []}, "latestRun": None},
    ]

    def runs(i: dict[str, Any]) -> dict[str, Any]:
        if i.get("statuses") == ["succeeded"]:
            return {"items": [{"finishedAt": "2026-11-20T06:01:00Z"}], "total": 9}
        return {"items": [], "total": 4 if i["enqueuedTo"] and i["enqueuedFrom"] and i["enqueuedTo"] > "2026-11" else 2}

    out = await _call(
        _Fake({"riskScenarios.getRiskScenariosOverview": scenarios, "jobs.listAllRuns": runs}),
        "get_opencrq_data_health",
    )
    assert "Risk scenarios: total 3 (missing 1, outdated 1, ready 1)" in out
    assert "Inputs missing: financial_profile 1; inputs outdated since the last score: tef 1" in out
    assert "Never scored: 1; oldest last score 2026-10-01T10:00, newest 2026-11-01T10:00" in out
    assert "Last successful CRQ simulation: finished 2026-11-20T06:01" in out
    assert "CRQ simulations enqueued: last 30 days" in out


async def test_data_health_keeps_going_past_a_refused_read() -> None:
    out = await _call(
        _Fake(
            {"riskScenarios.getRiskScenariosOverview": "403 forbidden", "jobs.listAllRuns": {"items": [], "total": 0}}
        ),
        "get_opencrq_data_health",
    )
    assert "Risk scenarios: 403 forbidden" in out
    assert "Last successful CRQ simulation: none" in out


async def test_platform_health_counts_failures_stuck_runs_and_links() -> None:
    connectors = [
        {
            "source": "opencti",
            "state": "healthy",
            "platformConfigured": True,
            "tenantConfigured": True,
            "lastRun": None,
        },
        {
            "source": "openaev",
            "state": "failing",
            "platformConfigured": True,
            "tenantConfigured": True,
            "lastRun": {"finishedAt": "2026-11-22T09:00:00Z"},
        },
        {"source": "drata", "state": "not_configured", "platformConfigured": True, "tenantConfigured": False},
    ]
    seen: list[dict[str, Any]] = []

    def runs(i: dict[str, Any]) -> dict[str, Any]:
        seen.append(i)
        return {"items": [], "total": 5 if i["statuses"] == ["running"] else 1}

    out = await _call(
        _Fake({"jobs.listAllRuns": runs, "connectors.listConnectors": connectors}), "get_opencrq_platform_health"
    )
    assert len([i for i in seen if i["statuses"] == ["dlq"]]) == 7
    assert "1, 1, 1, 1, 1, 1, 1; 7-day daily mean 1.0; today 1" in out
    assert "Runs running for over 6 h: 5" in out
    assert "Connectors: failing 1, healthy 1, not_configured 1" in out
    assert "Connectors failing: openaev" in out
    assert "OpenCTI link: configured true, state healthy" in out
    assert "OpenAEV link: configured true, state failing, last run finished 2026-11-22T09:00" in out
    assert "not exposed by the OpenCRQ API" in out


async def test_platform_health_answers_a_refusal_per_part() -> None:
    out = await _call(
        _Fake({"jobs.listAllRuns": "403 forbidden", "connectors.listConnectors": []}), "get_opencrq_platform_health"
    )
    assert "Runs failed (dead-letter queue): 403 forbidden" in out
    assert "OpenCTI link: not offered by this OpenCRQ" in out
