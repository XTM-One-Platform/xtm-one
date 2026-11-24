"""Platform Health spend ceilings locked by default (#1202).

Platform Health spends outside the agentic quota; its own ceilings are the only
bound. They resolve to the built-in defaults unless
``PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED`` is true, a write that changes one
while locked is refused, and a stored value is kept for when they unlock.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import settings as app_settings
from app.services.health import settings as health_settings
from app.services.health.settings import (
    SPEND_FIELDS,
    HealthAnalysisSettings,
    HealthBudgetCaps,
    HealthModuleAnalystSettings,
    HealthSettings,
    SpendLimitsLockedError,
    apply_spend_lock,
    changed_spend_fields,
    describe_spend_limits,
    save_health_settings,
    spend_limits_unlocked,
)


def _raised() -> HealthSettings:
    """A row an administrator raised before the lock: Module Analyst 100 USD a day, and the rest."""
    return HealthSettings(
        enabled=True,
        budget=HealthBudgetCaps(cycle_tokens=900_000, month_cost_usd=Decimal("500")),
        analysis=HealthAnalysisSettings(enabled=True, day_tokens=1_000_000, month_cost_usd=None),
        module_analyst=HealthModuleAnalystSettings(enabled=False, day_cost_usd=Decimal("100"), cycle_tokens=None),
    )


@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_health_spend_limits_unlocked", False)


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(app_settings, "platform_health_spend_limits_unlocked", True)


def test_locked_by_default() -> None:
    assert type(app_settings).model_fields["platform_health_spend_limits_unlocked"].default is False


def test_locked_a_stored_ceiling_reads_as_its_default_and_the_rest_is_kept(locked) -> None:
    assert not spend_limits_unlocked()
    applied = apply_spend_lock(_raised())
    defaults = HealthSettings()
    for block, keys in SPEND_FIELDS.items():
        for key in keys:
            assert getattr(getattr(applied, block), key) == getattr(getattr(defaults, block), key), f"{block}.{key}"
    assert applied.module_analyst.day_cost_usd == Decimal("4")
    # Not spend ceilings: the switches and the rest stay as stored.
    assert applied.enabled is True
    assert applied.analysis.enabled is True
    assert applied.module_analyst.enabled is False


def test_locked_nothing_stored_reads_the_defaults(locked) -> None:
    assert apply_spend_lock(HealthSettings()) == HealthSettings()


def test_unlocked_the_stored_values_apply(unlocked) -> None:
    raised = _raised()
    applied = apply_spend_lock(raised)
    assert applied == raised
    assert applied.module_analyst.day_cost_usd == Decimal("100")
    # A cleared ceiling still refuses (ceiling_not_set): the lock leaves that rule alone.
    assert not applied.module_analyst.ceilings_set()
    assert not applied.analysis.ceilings_set()


def test_unlocked_nothing_stored_reads_the_defaults(unlocked) -> None:
    assert apply_spend_lock(HealthSettings()) == HealthSettings()


def test_changed_spend_fields_names_each_ceiling_and_ignores_the_switches() -> None:
    current = HealthSettings()
    new = current.model_copy(
        update={
            "enabled": True,
            "analysis": current.analysis.model_copy(update={"enabled": True, "day_cost_usd": Decimal("2")}),
            "module_analyst": current.module_analyst.model_copy(update={"enabled": False}),
        }
    )
    assert changed_spend_fields(new, current) == ["analysis.day_cost_usd"]
    assert changed_spend_fields(current, current) == []


def test_describe_reports_value_default_stored_and_source(locked, monkeypatch) -> None:
    rows = {(row["block"], row["key"]): row for row in describe_spend_limits(_raised())}
    assert len(rows) == sum(len(keys) for keys in SPEND_FIELDS.values())
    row = rows[("module_analyst", "day_cost_usd")]
    assert (row["value"], row["default"], row["stored"], row["source"]) == (
        Decimal("4"),
        Decimal("4"),
        Decimal("100"),
        "default",
    )
    monkeypatch.setattr(app_settings, "platform_health_spend_limits_unlocked", True)
    rows = {(row["block"], row["key"]): row for row in describe_spend_limits(_raised())}
    assert rows[("module_analyst", "day_cost_usd")]["value"] == Decimal("100")
    assert rows[("module_analyst", "day_cost_usd")]["source"] == "database"
    assert rows[("analysis", "day_cost_usd")]["source"] == "default"


# ── The write ─────────────────────────────────────────────────────────────


class _Db:
    async def flush(self) -> None:  # pragma: no cover - the audit may flush
        return None


@pytest.fixture
def row(monkeypatch):
    stored = SimpleNamespace(health_settings=_raised().model_dump(mode="json"))

    async def _get_platform_settings(_db):
        return stored

    async def _log_audit(*_args, **_kwargs):
        return None

    import app.api.v1.platform_settings_helpers as helpers
    import app.services.audit as audit

    monkeypatch.setattr(helpers, "get_platform_settings", _get_platform_settings)
    monkeypatch.setattr(audit, "log_audit", _log_audit)
    return stored


async def test_locked_a_changed_ceiling_is_refused_and_nothing_is_written(locked, row) -> None:
    before_row = dict(row.health_settings)
    new = HealthSettings(enabled=True, module_analyst=HealthModuleAnalystSettings(day_cost_usd=Decimal("50")))
    with pytest.raises(SpendLimitsLockedError) as refused:
        await save_health_settings(_Db(), new, actor=SimpleNamespace())
    assert refused.value.fields == ["module_analyst.day_cost_usd"]
    assert "PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED" in str(refused.value)
    assert row.health_settings == before_row


async def test_locked_other_fields_save_and_the_stored_ceilings_are_kept(locked, row) -> None:
    # What the page sends back: the values in force (the defaults) and a changed name.
    new = apply_spend_lock(health_settings._read(row.health_settings)).model_copy(update={"name": "Jeeves"})
    before, after = await save_health_settings(_Db(), new, actor=SimpleNamespace())
    assert after.name == "Jeeves"
    assert after.module_analyst.day_cost_usd == Decimal("4")
    assert before.module_analyst.day_cost_usd == Decimal("4")
    kept = health_settings._read(row.health_settings)
    assert kept.name == "Jeeves"
    assert kept.module_analyst.day_cost_usd == Decimal("100")
    assert kept.budget.cycle_tokens == 900_000


async def test_unlocked_a_changed_ceiling_saves(unlocked, row) -> None:
    new = _raised().model_copy(
        update={"module_analyst": _raised().module_analyst.model_copy(update={"day_cost_usd": Decimal("7")})}
    )
    _before, after = await save_health_settings(_Db(), new, actor=SimpleNamespace())
    assert after.module_analyst.day_cost_usd == Decimal("7")
    assert health_settings._read(row.health_settings).module_analyst.day_cost_usd == Decimal("7")
