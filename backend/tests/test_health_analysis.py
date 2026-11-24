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
        module=kw.get("module", "agents"),
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
        module="opencti",
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


NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)


def test_the_prompt_snapshot_of_both_finding_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    monkeypatch.setattr(config.settings, "app_version", "1.261124.0-dev.f972e8c")
    payload = json.loads(analysis.build_prompt("opencti", [_row("a"), _agentic_row()], NOW))
    assert payload == {
        "module": "opencti",
        "open_findings": 2,
        "running_build": "1.261124.0-dev.f972e8c",
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
                "evidence_at": "2026-11-24T03:50:00+00:00",
                "check_build": None,
                "stale": True,
            },
        ],
    }


def _logic_row(build: str, *, read_at: str = "2026-11-24T05:00:00+00:00") -> Any:
    from app.services.health.check_reports import check_logic_version

    row = _agentic_row()
    row.evidence = {
        **row.evidence,
        "read_at": read_at,
        "check_logic": {**check_logic_version("opencti"), "build": build},
    }
    return row


def test_a_finding_of_another_build_is_stale_and_says_so_in_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1732: open finding from build A, instance on build B."""
    from app.core import config

    monkeypatch.setattr(config.settings, "app_version", "B")
    row = _logic_row("A")
    assert analysis.finding_is_stale(row, NOW) is True
    entry = json.loads(analysis.build_prompt("opencti", [row], NOW))["findings"][0]
    assert entry["stale"] is True and entry["check_build"] == "A"
    assert entry["evidence_at"] == "2026-11-24T05:00:00+00:00"
    assert "never recommend a rollback" in analysis.SYSTEM_PROMPT and "Run checks now" in analysis.SYSTEM_PROMPT


def test_a_finding_of_the_running_logic_is_fresh_until_a_day_old(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    monkeypatch.setattr(config.settings, "app_version", "B")
    assert analysis.finding_is_stale(_logic_row("B"), NOW) is False
    assert analysis.finding_is_stale(_logic_row("B", read_at="2026-11-23T05:00:00+00:00"), NOW) is True
    assert analysis.finding_is_stale(_row("a"), NOW) is False  # deterministic: never stale


def test_the_output_contract_drops_a_rollback_on_stale_evidence() -> None:
    summary = (
        "17 of 25 managers are not running.\n"
        "1. If unresolved, roll back to build f95a7a9.\n"
        "2. Check the platform queues."
    )
    guarded = analysis.guard_summary(summary, stale=True)
    assert "roll back" not in guarded and "f95a7a9" not in guarded
    assert "Check the platform queues." in guarded and guarded.endswith(analysis.STALE_ADVICE)
    assert analysis.guard_summary(summary, stale=False) == summary
    assert analysis.guard_summary("Nothing to act on.", stale=True) == "Nothing to act on."


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
