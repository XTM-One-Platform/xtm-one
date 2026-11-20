"""Platform Health analysis layer without a database (#101 S9, dev-docs/platform-health.md 6):
the finding-set hash, the prompt carries no free text, unset ceilings refuse, the new trace
source is accepted, and the module imports nothing metered."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.health import analysis
from app.services.health.settings import HealthAnalysisSettings, HealthSettings

SOURCE = Path(analysis.__file__).read_text(encoding="utf-8")


def _row(id_: str, severity: str = "warning", **kw):
    return SimpleNamespace(
        id=id_,
        severity=severity,
        check_id=kw.get("check_id", "agents.tool_errors"),
        category="performance",
        subject=kw.get("subject", {"agent_id": "a1"}),
        evidence=kw.get("evidence", {"values": {"rate": 0.3}, "threshold": 0.1, "source": "x"}),
        recommendation="Fix the tool",
    )


def test_the_finding_set_hash_ignores_order_and_follows_severity():
    a, b = _row("a"), _row("b", "critical")
    assert analysis.finding_set_hash([a, b]) == analysis.finding_set_hash([b, a])
    assert analysis.finding_set_hash([a, b]) != analysis.finding_set_hash([a, _row("b", "warning")])
    assert analysis.finding_set_hash([a]) != analysis.finding_set_hash([a, b])


def test_the_prompt_carries_ids_and_values_only():
    prompt = analysis.build_prompt("agents", [_row("a")])
    assert "agents.tool_errors" in prompt and '"rate":0.3' in prompt
    assert "Fix the tool" not in prompt  # no recommendation text
    assert '"source"' not in prompt


def test_the_prompt_is_capped():
    rows = [_row(str(i)) for i in range(analysis.MAX_FINDINGS + 20)]
    assert analysis.build_prompt("agents", rows).count('"check"') == analysis.MAX_FINDINGS


def test_analysis_is_off_with_no_ceiling_by_default():
    settings = HealthSettings().analysis
    assert settings.enabled is False
    assert settings.ceilings_set() is False
    full = HealthAnalysisSettings(
        enabled=True, day_tokens=1, day_cost_usd=Decimal("1"), month_tokens=1, month_cost_usd=Decimal("1")
    )
    assert full.ceilings_set() is True


def test_periods_are_utc_day_and_month():
    now = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    assert analysis.day_period(now) == "D:2026-11-20"
    assert analysis.month_period(now) == "M:2026-11"


def test_the_tracing_sink_keeps_the_platform_health_source():
    from app.services.langfuse_tracing import _resolve_local_trace_source

    assert _resolve_local_trace_source("platform-health-analysis:agents", analysis.TRACE_SOURCE) == "platform_health"


@pytest.mark.asyncio
async def test_unset_ceilings_refuse_before_any_lock():
    settings = HealthAnalysisSettings(enabled=True)
    db: Any = None  # never reached: unset ceilings refuse before any lock
    now = datetime(2026, 11, 20, tzinfo=UTC)
    assert await analysis.reserve(db, settings, now=now, tokens=1, cost=Decimal(0)) == "ceiling_not_set"


def test_the_analysis_never_imports_the_quota_layer_or_a_run_context():
    imported: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    forbidden = ("app.services.quota", "app.services.agentic_execution", "app.agents.run_context")
    assert not {m for m in imported if m.startswith(forbidden)}
    assert not names & {"increment_agentic_usage", "check_agentic_quota", "RunContext", "AssignmentRun"}
