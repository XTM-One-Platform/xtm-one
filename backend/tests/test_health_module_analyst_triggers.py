# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The Module Analyst's triggers (#101 S14a-2b, ``dev-docs/platform-health.md`` 12.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.services.health import module_analyst
from app.services.health.settings import HealthModuleAnalystSettings, HealthSettings

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
TWO_HOURS = timedelta(hours=2)


def _due(current: str, last: dict | None, fired_at: datetime | None, now: datetime = NOW) -> str | None:
    return module_analyst.due_trigger(
        current_hash=current, last_value=last, fired_at=fired_at, now=now, change_min_interval=TWO_HOURS
    )


def test_a_registration_never_looked_at_gets_its_daily_cycle() -> None:
    assert _due(module_analyst.empty_set_hash(), None, None) == "daily"


def test_a_new_finding_set_starts_a_change_cycle() -> None:
    assert _due("changed", None, None) == "change"
    assert _due("changed", {"hash": "before"}, NOW - TWO_HOURS) == "change"


def test_the_debounce_holds_a_change_inside_the_window() -> None:
    assert _due("changed", {"hash": "before"}, NOW - timedelta(minutes=30)) is None


def test_an_unchanged_set_waits_for_the_next_utc_day() -> None:
    assert _due("same", {"hash": "same"}, NOW - timedelta(hours=11)) is None
    assert _due("same", {"hash": "same"}, NOW - timedelta(hours=13)) == "daily"


def test_a_change_cycle_covers_the_day() -> None:
    """A registration a change-triggered cycle covered today gets no daily one (12.2)."""
    assert _due("same", {"hash": "same", "trigger": "change"}, NOW - timedelta(minutes=5)) is None


def test_the_settings_are_on_by_default_with_a_two_hour_debounce() -> None:
    settings = HealthSettings().module_analyst
    assert settings.enabled is True
    assert settings.change_min_interval_minutes == 120
    assert settings.excluded_registration_ids == []


def test_an_older_settings_value_reads_the_defaults() -> None:
    assert HealthSettings.model_validate({}).module_analyst == HealthModuleAnalystSettings()


@pytest.mark.parametrize("minutes", [0, 14, 24 * 60 + 1])
def test_the_debounce_is_bounded(minutes: int) -> None:
    with pytest.raises(ValidationError):
        HealthModuleAnalystSettings(change_min_interval_minutes=minutes)


class _Db:
    """Fails the test on any read: the gates answer before the first query."""

    async def get(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("no read expected")

    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("no read expected")


@pytest.mark.asyncio
async def test_nothing_starts_in_copilot_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config

    monkeypatch.setattr(type(config.settings), "is_xtm_one", property(lambda _self: False))
    assert await module_analyst.run_module_triggers(_Db(), now=NOW) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [{"enabled": False}, {"enabled": True, "module_analyst": {"enabled": False}}],
    ids=["platform-health-off", "module-analyst-off"],
)
async def test_nothing_starts_when_switched_off(monkeypatch: pytest.MonkeyPatch, value: dict) -> None:
    from app.core import config
    from app.services.health import settings as health_settings

    monkeypatch.setattr(type(config.settings), "is_xtm_one", property(lambda _self: True))

    async def _load(_db: Any) -> HealthSettings:
        return HealthSettings.model_validate(value)

    monkeypatch.setattr(health_settings, "load_health_settings", _load)
    assert await module_analyst.run_module_triggers(_Db(), now=NOW) == 0  # type: ignore[arg-type]
