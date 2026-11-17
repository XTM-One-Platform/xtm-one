"""The approval kill switch is seeded from deployment config, once (FR-044).

Why a seed and not an override: the stored value is what the gate reads, so a
variable consulted on every boot would undo an administrator's choice at each
restart — the same reasoning ``TOOL_APPROVALS_PLATFORM_DEFAULT`` already follows.
What the tests below pin is therefore not "the variable works" but "the variable
stops mattering once the row exists".

The unrecognised-value case is the one worth a test of its own: it must leave
approvals ON. A typo in a deployment variable disabling an approval control is
the failure this product cannot have.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models as _models  # noqa: F401 — side-effect: register all models
from app.api.v1.platform_settings_helpers import (
    _seed_tool_approvals,
    get_platform_settings,
)
from app.core.config import settings
from tests.test_approval_upgrade_migration import _temporary_database


@pytest.mark.parametrize("value", ["false", "FALSE", " no ", "off", "0"])
def test_a_falsy_value_seeds_the_switch_off(monkeypatch, value):
    monkeypatch.setattr(settings, "tool_approvals_enabled", value)
    assert _seed_tool_approvals() == {"tool_approvals_enabled": False}


@pytest.mark.parametrize("value", [None, "", "true", "on", "1"])
def test_anything_else_leaves_the_column_default(monkeypatch, value):
    """No kwarg at all — the column's own default (approvals on) stands."""
    monkeypatch.setattr(settings, "tool_approvals_enabled", value)
    assert _seed_tool_approvals() == {}


def test_an_unrecognised_value_leaves_approvals_on_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(settings, "tool_approvals_enabled", "maybe")
    with caplog.at_level("WARNING"):
        assert _seed_tool_approvals() == {}
    assert "TOOL_APPROVALS_ENABLED" in caplog.text


@pytest.mark.integration
async def test_the_variable_seeds_the_row_then_stops_mattering(monkeypatch):
    async with _temporary_database() as url:
        engine = create_async_engine(url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            monkeypatch.setattr(settings, "tool_approvals_enabled", "false")
            async with sessionmaker() as session:
                created = await get_platform_settings(session)
                await session.commit()
                assert created.tool_approvals_enabled is False

            # An administrator's choice now lives in that row. A deployment
            # flipping the variable back must not reach it.
            monkeypatch.setattr(settings, "tool_approvals_enabled", "true")
            async with sessionmaker() as session:
                again = await get_platform_settings(session)
                assert again.tool_approvals_enabled is False
        finally:
            await engine.dispose()
