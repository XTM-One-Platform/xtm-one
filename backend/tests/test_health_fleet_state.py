"""The Platform Health fleet reads Platform Health's own state (#974).

Its assignments are started by the Settings cycle runner (trigger ``manual``),
so the generic automation count read "Not set up" forever on a fleet nobody
may switch. The fleet's badge now follows Settings > Platform Health; every
other fleet keeps the assignment count. Unit tests on doubles, no database.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import Fleet
from app.services.fleet_views import (
    HEALTH_FLEET_AUTOMATION,
    HEALTH_SETTINGS_ROUTE,
    AutomationCounts,
    automation_counts,
    fleet_automation_counts,
    fleet_automation_state,
    health_fleet_counts,
    health_fleet_starts,
    opt_in_state,
)
from app.services.fleets import ensure_fleet_health_check
from app.services.health.read_only import HEALTH_FLEET_SLUG
from app.services.health.settings import HealthSettings


def _health_fleet() -> Fleet:
    return cast(
        Fleet,
        SimpleNamespace(
            id=uuid.uuid4(),
            name="Platform Health",
            slug=HEALTH_FLEET_SLUG,
            is_builtin=True,
            orchestrator_agent_id=uuid.uuid4(),
            health_check=None,
        ),
    )


class _NoQueryDb:
    """A session that refuses any query: the health fleet's state never reads its rows."""

    async def execute(self, _stmt: Any) -> Any:  # pragma: no cover - reached only on a regression
        raise AssertionError("the Platform Health fleet's state must not count its assignments")

    def add(self, _row: Any) -> None:  # pragma: no cover - reached only on a regression
        raise AssertionError("no weekly fleet health check is seeded on the Platform Health fleet")


def _db() -> AsyncSession:
    return cast(AsyncSession, _NoQueryDb())


def test_platform_health_on_reads_on() -> None:
    counts = health_fleet_counts(True)
    assert opt_in_state(counts) == "on"
    assert counts.off_names == ()


def test_platform_health_off_reads_paused_naming_platform_health() -> None:
    counts = health_fleet_counts(False)
    assert opt_in_state(counts) == "paused"
    assert counts.off_names == (HEALTH_FLEET_AUTOMATION,)


@pytest.mark.asyncio
@pytest.mark.parametrize(("active", "state"), [(True, "on"), (False, "paused")])
async def test_every_reader_follows_the_settings_switch(
    monkeypatch: pytest.MonkeyPatch, active: bool, state: str
) -> None:
    async def _active(_db: AsyncSession) -> bool:
        return active

    monkeypatch.setattr("app.services.health.availability.health_active", _active)
    fleet = _health_fleet()
    assert opt_in_state(await fleet_automation_counts(_db(), fleet)) == state
    assert (await fleet_automation_state(_db(), fleet)).state == state


def test_other_fleets_keep_the_assignment_count() -> None:
    # A built-in fleet with only the weekly check, born off: still "Not set up".
    off: list[tuple[str | None, bool, str, bool, str | None]] = [
        ("ctem-desk", True, "Weekly fleet health check", False, "created_disabled")
    ]
    assert opt_in_state(automation_counts(off, builtin_fleet=True)) == "not_set_up"
    # A custom fleet with every assignment on: "On".
    on: list[tuple[str | None, bool, str, bool, str | None]] = [("desk", False, "Daily triage", True, None)]
    assert opt_in_state(automation_counts(on, builtin_fleet=False)) == "on"
    assert opt_in_state(AutomationCounts()) == "not_set_up"


def test_the_starts_line_reads_the_settings_schedules_and_signals() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)
    starts = health_fleet_starts(HealthSettings(), xtm_one=True, registered=["opencti", "openaev", "opencti"], now=now)
    assert starts.events is True
    assert starts.schedules == 4
    # Memory Steward daily at 02:00 UTC is the soonest.
    assert starts.next_run_at == datetime(2026, 11, 22, 2, 0, tzinfo=UTC)
    assert starts.modules == ("xtm_one", "openaev", "opencti")
    assert starts.settings_route == HEALTH_SETTINGS_ROUTE


def test_platform_health_off_promises_no_next_run() -> None:
    """UX r1 B2 on #980: a paused fleet keeps its schedules counted but never names a next run."""
    from datetime import UTC, datetime

    now = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)
    starts = health_fleet_starts(HealthSettings(), xtm_one=True, now=now, active=False)
    assert (starts.active, starts.schedules, starts.next_run_at) == (False, 4, None)
    assert health_fleet_starts(HealthSettings(), xtm_one=True, now=now).active is True


def test_schedules_off_and_copilot_mode() -> None:
    settings = HealthSettings()
    for key in ("memory", "quality", "flows", "digest"):
        setattr(settings.schedules, f"{key}_enabled", False)
    for key in ("feedback", "failure", "cost", "model_change"):
        setattr(settings.signals, f"{key}_enabled", False)
    starts = health_fleet_starts(settings, xtm_one=False, registered=["opencti"])
    assert (starts.events, starts.schedules, starts.next_run_at, starts.modules) == (False, 0, None, ())


def test_the_shipped_documentation_validates_and_names_no_fleet_action() -> None:
    from app.agents.builtin_fleet_docs import PLATFORM_HEALTH_DOC, PLATFORM_HEALTH_STRUCTURED
    from app.services.fleet_docs import DOC_SECTIONS, clean_sections, clean_structured

    assert set(clean_sections(PLATFORM_HEALTH_DOC)) == set(DOC_SECTIONS)
    parts = clean_structured(PLATFORM_HEALTH_STRUCTURED)
    assert parts and len(parts["triggers"]) == 6
    # The daily module round of the checkers (#101 S15f, section 13.8).
    assert "Module checks" in [t["name"] for t in parts["triggers"]]
    text = " ".join(PLATFORM_HEALTH_DOC.values()).lower()
    for phrase in ("switch each automation on", "from the fleet page", "handover", "manual only"):
        assert phrase not in text


@pytest.mark.asyncio
async def test_the_platform_health_fleet_gets_no_weekly_fleet_health_check() -> None:
    agent = cast(Agent, SimpleNamespace(id=uuid.uuid4(), user_id=None, is_company_managed=True))
    assert not await ensure_fleet_health_check(_db(), _health_fleet(), agent)
