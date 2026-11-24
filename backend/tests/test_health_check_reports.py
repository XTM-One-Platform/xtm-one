"""Module checker check reports and their agentic findings (#101 S15c, dev-docs/platform-health.md 13.6).

What a report accepts, and how each verdict opens, refreshes or resolves the agentic
finding ``<module>.agentic.<check>`` of the registration, on an in-memory session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health_check import STATUS_DISMISSED, STATUS_OPEN, STATUS_RESOLVED, HealthCheckFinding
from app.models.health_module_analyst import HealthModuleCheckReport
from app.services.health import check_reports
from app.services.health.check_reports import CheckReportError, apply_verdicts, clean_checks, counts
from app.services.health.engine import finding_key

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
REG = uuid.UUID("00000000-0000-4000-8000-000000000001")


class _Session:
    """The two calls apply_verdicts makes: get by key, add, flush."""

    def __init__(self, rows: list[HealthCheckFinding] | None = None) -> None:
        self.rows = {row.id: row for row in rows or []}

    async def get(self, _model: object, key: str) -> HealthCheckFinding | None:
        return self.rows.get(key)

    def add(self, row: HealthCheckFinding) -> None:
        self.rows[row.id] = row

    async def flush(self) -> None:
        return None


def _key(check: str) -> str:
    return finding_key(f"opencrq.agentic.{check}", {"registration_id": str(REG)})


def _row(check: str, status: str, **extra: Any) -> HealthCheckFinding:
    return HealthCheckFinding(
        id=_key(check),
        check_id=f"opencrq.agentic.{check}",
        module="opencrq",
        subject={"registration_id": str(REG)},
        category="configuration",
        severity="warning",
        status=status,
        evidence={},
        recommendation="",
        doc_link="",
        pass_streak=0,
        fail_streak=0,
        first_seen=NOW - timedelta(days=1),
        last_seen=NOW - timedelta(days=1),
        **extra,
    )


def test_a_report_is_bounded() -> None:
    with pytest.raises(CheckReportError):
        clean_checks([])
    with pytest.raises(CheckReportError):
        clean_checks([{"id": "x", "verdict": "pass"}] * (check_reports.MAX_CHECKS + 1))
    with pytest.raises(CheckReportError):
        clean_checks([{"id": "Jobs State", "verdict": "pass"}])
    with pytest.raises(CheckReportError):
        clean_checks([{"id": "jobs_state", "verdict": "ok"}])
    with pytest.raises(CheckReportError):
        clean_checks([{"id": "jobs_state", "verdict": "pass"}, {"id": "jobs_state", "verdict": "warn"}])
    with pytest.raises(CheckReportError):
        clean_checks([{"id": "jobs_state", "verdict": "pass", "category": "mood"}])


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "jobs_state", "verdict": "warn"},
        {"id": "jobs_state", "verdict": "fail", "recommendation": "  "},
        {"id": "jobs_state", "verdict": "not_run"},
        {"id": "jobs_state", "verdict": "pass", "evidence": [{"values": {}}]},
    ],
)
def test_a_failing_check_says_what_to_do_and_a_skipped_one_why(entry: dict[str, Any]) -> None:
    with pytest.raises(CheckReportError):
        clean_checks([entry])


def test_counts_follow_the_verdicts() -> None:
    verdicts = clean_checks(
        [
            {"id": "a", "verdict": "pass"},
            {"id": "b", "verdict": "pass"},
            {"id": "c", "verdict": "warn", "recommendation": "r"},
            {"id": "d", "verdict": "fail", "recommendation": "r"},
            {"id": "e", "verdict": "not_run", "reason": "tool not on the allow-list"},
        ]
    )
    assert counts(verdicts) == {"passed": 2, "warned": 1, "failed": 1, "not_run": 1}


async def test_warn_and_fail_open_their_finding_at_once() -> None:
    db = _Session()
    verdicts = clean_checks(
        [
            {
                "id": "jobs_state",
                "verdict": "fail",
                "recommendation": "Restart the stuck job.",
                "category": "availability",
            },
            {"id": "crq_run_fresh", "verdict": "warn", "recommendation": "Schedule a run."},
            {"id": "module_link", "verdict": "pass"},
        ]
    )
    ids, tally = await apply_verdicts(
        cast(AsyncSession, db), module="opencrq", registration_id=REG, verdicts=verdicts, now=NOW
    )
    assert tally == {"opened": 2, "refreshed": 0, "resolved": 0}
    assert ids == [_key("jobs_state"), _key("crq_run_fresh"), None]
    failed = db.rows[_key("jobs_state")]
    assert (failed.status, failed.severity, failed.category) == (STATUS_OPEN, "critical", "availability")
    assert failed.opened_at == NOW
    assert failed.evidence["source"] == "module_checker"
    assert db.rows[_key("crq_run_fresh")].severity == "warning"


async def test_a_pass_resolves_the_open_finding_in_the_same_cycle() -> None:
    db = _Session([_row("jobs_state", STATUS_OPEN)])
    verdicts = clean_checks([{"id": "jobs_state", "verdict": "pass"}])
    ids, tally = await apply_verdicts(
        cast(AsyncSession, db), module="opencrq", registration_id=REG, verdicts=verdicts, now=NOW
    )
    assert tally["resolved"] == 1
    row = db.rows[_key("jobs_state")]
    assert (row.status, row.resolved_at) == (STATUS_RESOLVED, NOW)
    assert ids == [row.id]


async def test_not_run_leaves_the_finding_as_it_is() -> None:
    db = _Session([_row("jobs_state", STATUS_OPEN)])
    verdicts = clean_checks([{"id": "jobs_state", "verdict": "not_run", "reason": "read failed"}])
    _ids, tally = await apply_verdicts(
        cast(AsyncSession, db), module="opencrq", registration_id=REG, verdicts=verdicts, now=NOW
    )
    assert tally == {"opened": 0, "refreshed": 0, "resolved": 0}
    assert db.rows[_key("jobs_state")].status == STATUS_OPEN


async def test_a_live_dismissal_is_refreshed_never_reopened() -> None:
    until = (NOW + timedelta(days=3)).isoformat()
    db = _Session([_row("jobs_state", STATUS_DISMISSED, dismissal={"reason": "known", "until": until})])
    verdicts = clean_checks([{"id": "jobs_state", "verdict": "fail", "recommendation": "r"}])
    _ids, tally = await apply_verdicts(
        cast(AsyncSession, db), module="opencrq", registration_id=REG, verdicts=verdicts, now=NOW
    )
    assert tally["refreshed"] == 1
    row = db.rows[_key("jobs_state")]
    assert row.status == STATUS_DISMISSED
    assert row.last_seen == NOW


async def test_a_resolved_finding_reopens_on_a_new_failure() -> None:
    db = _Session([_row("jobs_state", STATUS_RESOLVED, resolved_at=NOW - timedelta(hours=5))])
    verdicts = clean_checks([{"id": "jobs_state", "verdict": "warn", "recommendation": "r"}])
    _ids, tally = await apply_verdicts(
        cast(AsyncSession, db), module="opencrq", registration_id=REG, verdicts=verdicts, now=NOW
    )
    assert tally["opened"] == 1
    row = db.rows[_key("jobs_state")]
    assert (row.status, row.resolved_at, row.opened_at) == (STATUS_OPEN, None, NOW)


def test_the_report_store_has_one_row_per_cycle_and_no_foreign_key() -> None:
    table = HealthModuleCheckReport.metadata.tables["health_module_check_reports"]
    assert not table.foreign_keys
    unique = {c.name for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_health_module_check_reports_cycle" in unique


def test_the_agentic_check_id_is_namespaced_by_module() -> None:
    assert check_reports.agentic_check_id("opencti", "platform_errors") == "opencti.agentic.platform_errors"
