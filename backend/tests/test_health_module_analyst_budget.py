"""Platform Health Module Analyst outside the agentic quota (#101 S14b, dev-docs/platform-health.md 12.5).

The exemption is decided by the server from the seeded rows only (agent role, built-in,
company-managed, the seeded assignment); the ceiling is the analyst's own, refused when
cleared or passed. The ledger itself runs on PostgreSQL in
``test_health_module_analyst_budget_postgres.py``.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import assignment_engine as assignment_engine_mod
from app.services.health import module_analyst_budget as mab
from app.services.health.fleet_seed import ASSIGNMENT_BY_KEY
from app.services.health.settings import HealthModuleAnalystSettings, HealthSettings

AGENT_ID = uuid.uuid4()
SEEDED_NAME = ASSIGNMENT_BY_KEY["opencti_checks"].name


def _agent(**kw: object) -> SimpleNamespace:
    values = {"id": AGENT_ID, "health_role": "opencti_checker", "is_builtin": True, "is_company_managed": True}
    values.update(kw)
    return SimpleNamespace(**values)


def _assignment(**kw: object) -> SimpleNamespace:
    values = {"agent_id": AGENT_ID, "name": SEEDED_NAME}
    values.update(kw)
    return SimpleNamespace(**values)


def test_the_seeded_module_analyst_run_is_exempt():
    assert mab.is_module_checker_run(_agent(), _assignment()) is True


@pytest.mark.parametrize(
    ("role", "key"),
    [
        ("opencti_checker", "opencti_checks"),
        ("openaev_checker", "openaev_checks"),
        ("opencrq_checker", "opencrq_checks"),
    ],
)
def test_each_checker_is_exempt_on_its_own_checks_assignment(role: str, key: str) -> None:
    assert mab.is_module_checker_run(_agent(health_role=role), _assignment(name=ASSIGNMENT_BY_KEY[key].name))


def test_a_checker_on_another_checkers_assignment_is_not_exempt() -> None:
    other = ASSIGNMENT_BY_KEY["openaev_checks"].name
    assert mab.is_module_checker_run(_agent(health_role="opencti_checker"), _assignment(name=other)) is False


def test_the_s14_role_and_assignment_name_stay_exempt_until_the_seed_renames_them() -> None:
    """Section 13.1: the row keeps ``module_analyst`` until the next boot rewrites it."""
    s14 = _assignment(name="Platform Health module investigation")
    assert mab.is_module_checker_run(_agent(health_role="module_analyst"), s14) is True


@pytest.mark.parametrize(
    "agent_kw",
    [
        {"health_role": "quality_analyst"},
        {"health_role": None},
        {"is_builtin": False},
        {"is_company_managed": False},
    ],
)
def test_any_other_agent_keeps_counting_in_the_tiers(agent_kw):
    assert mab.is_module_checker_run(_agent(**agent_kw), _assignment()) is False


def test_an_assignment_an_admin_added_to_the_analyst_is_not_exempt():
    assert mab.is_module_checker_run(_agent(), _assignment(name="My own assignment")) is False


def test_an_assignment_of_another_agent_is_not_exempt():
    assert mab.is_module_checker_run(_agent(), _assignment(agent_id=uuid.uuid4())) is False


def test_missing_rows_are_not_exempt():
    assert mab.is_module_checker_run(None, _assignment()) is False
    assert mab.is_module_checker_run(_agent(), None) is False


def test_a_trigger_context_cannot_turn_the_exemption_on():
    """The predicate takes no run: nothing a trigger or a payload carries is read."""
    assert list(inspect.signature(mab.is_module_checker_run).parameters) == ["agent", "assignment"]


def test_the_engine_reads_the_exemption_before_charging_the_quota():
    src = inspect.getsource(assignment_engine_mod.execute_assignment_run)
    exempt_at = src.index("is_module_checker_run(agent, assignment)")
    increment_at = src.index("await increment_agentic_usage(")
    assert exempt_at < increment_at
    assert "not _module_analyst_exempt and not _run_is_quota_exempt(agent, run)" in src


def test_the_provisional_ceilings_of_section_12_6_are_the_defaults():
    settings = HealthSettings().module_analyst
    assert settings.cycle_tokens == 80_000
    assert (settings.day_tokens, settings.day_cost_usd) == (500_000, Decimal("4"))
    assert (settings.month_tokens, settings.month_cost_usd) == (10_000_000, Decimal("60"))
    assert settings.ceilings_set()


def test_a_stored_value_written_before_s14b_still_reads_with_the_defaults():
    settings = HealthModuleAnalystSettings.model_validate({"enabled": True, "change_min_interval_minutes": 60})
    assert settings.ceilings_set()
    assert settings.day_tokens == 500_000


@pytest.mark.parametrize("field", ["cycle_tokens", "day_tokens", "day_cost_usd", "month_tokens", "month_cost_usd"])
async def test_a_cleared_ceiling_refuses_before_any_lock(field):
    settings = HealthModuleAnalystSettings(**{field: None})
    db = SimpleNamespace()  # any lock attempt would fail on this stub
    refusal = await mab.reserve(db, settings, now=datetime.now(UTC), tokens=1, cost=Decimal("0.01"))
    assert refusal == mab.REFUSED_CEILING_NOT_SET


async def test_a_call_past_the_cycle_cap_refuses_before_any_lock():
    settings = HealthModuleAnalystSettings(cycle_tokens=1_000)
    refusal = await mab.reserve(
        SimpleNamespace(), settings, now=datetime.now(UTC), tokens=600, cost=Decimal(0), cycle_tokens_used=500
    )
    assert refusal == mab.REFUSED_CYCLE_CEILING_REACHED


def test_periods_are_utc_day_and_month():
    now = datetime(2026, 11, 21, 23, 30, tzinfo=UTC)
    assert mab.day_period(now) == "D:2026-11-21"
    assert mab.month_period(now) == "M:2026-11"


def test_the_ledger_never_imports_the_quota_layer():
    src = inspect.getsource(mab)
    assert "services.quota" not in src
    assert "RunContext" not in src
