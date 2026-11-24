"""Platform Health analysis layer without a database (#101 S9, dev-docs/platform-health.md 6):
the finding-set hash, the prompt carries no free text, unset ceilings refuse, the new trace
source is accepted, and the module imports nothing metered."""

from __future__ import annotations

import ast
import json
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


def _agentic_row(values: dict[str, Any] | None = None):
    row = _row(
        "g",
        "critical",
        check_id="opencti.agentic.managers_running",
        subject={"registration_id": "923854c5-4292-4eca-aa47-1ae2ea78842f"},
        evidence={
            "source": "module_checker",
            "read_at": "2026-11-24T03:50:00+00:00",
            "checks": [
                {
                    "tool": "get_opencti_managers",
                    "arguments": {},
                    "object_ids": ["PLAYBOOK_MANAGER"],
                    "values": values
                    if values is not None
                    else {"PLAYBOOK_MANAGER": {"enabled": True, "running": False}},
                    "read_at": "2026-11-24T03:50:00+00:00",
                }
            ],
        },
    )
    row.recommendation = "Restart the platform node that should run PLAYBOOK_MANAGER."
    return row


def test_an_agentic_finding_carries_its_reads_and_recommendation():
    prompt = analysis.build_prompt("opencti", [_agentic_row()])
    assert '"check":"opencti.managers_running"' in prompt
    assert "opencti.agentic." not in prompt and "agentic manager" not in prompt
    assert '"checked_by":"module_checker"' in prompt
    assert "get_opencti_managers" in prompt and "PLAYBOOK_MANAGER" in prompt
    assert '"running":false' in prompt
    assert "Restart the platform node that should run PLAYBOOK_MANAGER." in prompt
    assert '"threshold"' not in prompt and '"read_at"' not in prompt


def test_the_prompt_snapshot_of_both_finding_kinds():
    payload = json.loads(analysis.build_prompt("opencti", [_row("a"), _agentic_row()]))
    assert payload == {
        "module": "opencti",
        "open_findings": 2,
        "findings": [
            {
                "check": "agents.tool_errors",
                "severity": "warning",
                "category": "performance",
                "subject": {"agent_id": "a1"},
                "values": {"rate": 0.3},
                "threshold": 0.1,
            },
            {
                "check": "opencti.managers_running",
                "severity": "critical",
                "category": "performance",
                "subject": {"registration_id": "923854c5-4292-4eca-aa47-1ae2ea78842f"},
                "checked_by": "module_checker",
                "reads": [
                    {
                        "tool": "get_opencti_managers",
                        "object_ids": ["PLAYBOOK_MANAGER"],
                        "values": {"PLAYBOOK_MANAGER": {"enabled": True, "running": False}},
                    }
                ],
                "recommendation": "Restart the platform node that should run PLAYBOOK_MANAGER.",
            },
        ],
    }


def test_agentic_values_are_bounded():
    big = {f"MANAGER_{i}": "x" * 50 for i in range(100)}
    prompt = analysis.build_prompt("opencti", [_agentic_row(big)])
    reads = json.loads(prompt)["findings"][0]["reads"]
    assert set(reads[0]["values"]) == {"cut"}
    assert len(reads[0]["values"]["cut"]) == analysis.MAX_VALUES_CHARS


def test_the_system_prompt_forbids_reading_a_check_id_as_a_component():
    assert "never the name of a component" in analysis.SYSTEM_PROMPT
    assert "only when it appears in the values" in analysis.SYSTEM_PROMPT


def test_the_prompt_is_capped():
    rows = [_row(str(i)) for i in range(analysis.MAX_FINDINGS + 20)]
    assert analysis.build_prompt("agents", rows).count('"check"') == analysis.MAX_FINDINGS


def test_analysis_is_off_by_default_with_the_measured_ceilings() -> None:
    settings = HealthSettings().analysis
    assert settings.enabled is False
    assert settings.ceilings_set() is True
    assert (settings.day_tokens, settings.month_tokens) == (100_000, 3_100_000)
    assert (settings.day_cost_usd, settings.month_cost_usd) == (Decimal("1"), Decimal("31"))
    cleared = HealthAnalysisSettings(enabled=True, day_tokens=None)
    assert cleared.ceilings_set() is False


def test_the_reservation_covers_the_measured_prompt() -> None:
    # Lab measurement (dev-docs section 6): 33 findings, 8,709 prompt characters, 3,963 input tokens billed.
    assert analysis.estimate_input_tokens("x" * 8_709) >= 3_963


def test_periods_are_utc_day_and_month():
    now = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)
    assert analysis.day_period(now) == "D:2026-11-20"
    assert analysis.month_period(now) == "M:2026-11"


def test_the_tracing_sink_keeps_the_platform_health_source():
    from app.services.langfuse_tracing import _resolve_local_trace_source

    assert _resolve_local_trace_source("platform-health-analysis:agents", analysis.TRACE_SOURCE) == "platform_health"


@pytest.mark.asyncio
async def test_unset_ceilings_refuse_before_any_lock():
    settings = HealthAnalysisSettings(enabled=True, month_cost_usd=None)
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
