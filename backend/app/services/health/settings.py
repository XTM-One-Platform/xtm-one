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

"""Settings > Agents and automations > Platform Health: the one reader and writer of ``PlatformSettings.health_settings`` (#8254).

One JSON column, one model. Later levels add fields with defaults to
:class:`HealthSettings` (schedule, signal thresholds, shadow and watch
durations, dry-run), so a stored value written before them still reads and no
migration is needed. A stored value that does not validate reads as the
defaults: Platform Health off, which fails closed.
"""

from __future__ import annotations

import logging
import unicodedata
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

logger = logging.getLogger(__name__)

#: The most agents or flows a "selected" scope may list.
MAX_SCOPE_IDS = 500
#: Ceiling of every cap, so a typo cannot store an absurd budget.
MAX_TOKENS = 10_000_000_000
MAX_COST_USD = Decimal("1000000")


class HealthBudgetCaps(BaseModel):
    """Caps per cycle and per month, in tokens and in dollars. ``None`` sets no cap on that dimension."""

    model_config = ConfigDict(extra="forbid")

    cycle_tokens: int | None = Field(default=500_000, ge=0, le=MAX_TOKENS)
    cycle_cost_usd: Decimal | None = Field(default=Decimal("5"), ge=0, le=MAX_COST_USD, decimal_places=6)
    month_tokens: int | None = Field(default=10_000_000, ge=0, le=MAX_TOKENS)
    month_cost_usd: Decimal | None = Field(default=Decimal("100"), ge=0, le=MAX_COST_USD, decimal_places=6)


class HealthAnalysisSettings(BaseModel):
    """The analysis layer (#101 S9, ``dev-docs/platform-health.md`` section 6): off by default.

    The ceilings are hard: an analysis reserves its worst case on the day and the
    month before the model call and is refused over any of them. A ceiling an
    administrator clears (``None``) refuses every analysis (``ceiling_not_set``).

    The defaults come from the lab measurement of section 6.3: one analysis of a
    module at the most findings it reads costs at most 8,000 tokens; 6 modules
    twice a day is 96,000 tokens, rounded to 100,000 a day and 31 days of it a
    month. The dollar ceilings price that at the measured blended rate of a
    top-tier model (about 7 USD per million tokens), rounded up.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    day_tokens: int | None = Field(default=100_000, ge=0, le=MAX_TOKENS)
    day_cost_usd: Decimal | None = Field(default=Decimal("1"), ge=0, le=MAX_COST_USD, decimal_places=6)
    month_tokens: int | None = Field(default=3_100_000, ge=0, le=MAX_TOKENS)
    month_cost_usd: Decimal | None = Field(default=Decimal("31"), ge=0, le=MAX_COST_USD, decimal_places=6)

    def ceilings_set(self) -> bool:
        return None not in (self.day_tokens, self.day_cost_usd, self.month_tokens, self.month_cost_usd)


#: The longest any Platform Health duration (watch, shadow, signal window) may be set to.
MAX_DURATION_DAYS = 90


def _valid_cron(value: str) -> str:
    from app.core.utils import next_cron_run

    value = value.strip()
    if next_cron_run(value, datetime.now(UTC)) is None:
        raise ValueError("not a valid cron expression")
    return value


class HealthSchedules(BaseModel):
    """When each specialist's fleet-wide cycle starts (#8261), cron in ``timezone``."""

    model_config = ConfigDict(extra="forbid")

    memory: str = Field(default="0 2 * * *", max_length=100)
    quality: str = Field(default="0 3 * * 1", max_length=100)
    flows: str = Field(default="0 4 * * 1", max_length=100)
    digest: str = Field(default="0 7 * * 1", max_length=100)
    memory_enabled: bool = True
    quality_enabled: bool = True
    flows_enabled: bool = True
    digest_enabled: bool = True
    timezone: str = Field(default="UTC", max_length=64)

    @field_validator("memory", "quality", "flows", "digest")
    @classmethod
    def _cron(cls, value: str) -> str:
        return _valid_cron(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("not a known time zone") from exc
        return value


class HealthSignalSettings(BaseModel):
    """The signal thresholds (#8261, PRD 6.3); the defaults are decision B18's."""

    model_config = ConfigDict(extra="forbid")

    feedback_enabled: bool = True
    feedback_drop_points: float = Field(default=10, gt=0, le=100)
    feedback_window_days: int = Field(default=3, ge=1, le=MAX_DURATION_DAYS)
    feedback_min_ratings: int = Field(default=10, ge=1, le=10_000)
    failure_enabled: bool = True
    failure_rate_pct: float = Field(default=5, gt=0, lt=100)
    failure_last_runs: int = Field(default=20, ge=5, le=500)
    cost_enabled: bool = True
    cost_increase_pct: float = Field(default=20, gt=0, le=10_000)
    cost_window_days: int = Field(default=7, ge=1, le=MAX_DURATION_DAYS)
    cost_min_runs: int = Field(default=10, ge=1, le=10_000)
    model_change_enabled: bool = True


#: The longest name Platform Health may be given.
MAX_NAME_LENGTH = 40


def _valid_name(value: str | None) -> str | None:
    """A trimmed name of 1 to 40 characters, no control, format or line-separator one; blank is ``None``."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > MAX_NAME_LENGTH:
        raise ValueError(f"at most {MAX_NAME_LENGTH} characters")
    if any(unicodedata.category(char) in _REFUSED_NAME_CATEGORIES for char in value):
        raise ValueError("no control characters or line breaks")
    return value


#: Control, format (bidi overrides, zero-width) and line or paragraph separator characters.
_REFUSED_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


class HealthSettings(BaseModel):
    """What an administrator sets in Settings > Agents and automations > Platform Health."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    #: The name Platform Health goes by with people; ``None`` is the platform mode's default
    #: (``naming.default_health_agent_name``).
    name: str | None = Field(default=None, max_length=200)
    #: ``all``: every agent and flow but the Platform Health fleet; ``selected``: the lists below.
    scope_mode: Literal["all", "selected"] = "all"
    scope_agent_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SCOPE_IDS)
    scope_flow_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SCOPE_IDS)
    budget: HealthBudgetCaps = Field(default_factory=HealthBudgetCaps)
    #: Every write of every Platform Health agent is logged, not executed (#8255).
    dry_run: bool = False
    # Level 8 (#8261): schedule, signals and the durations levels 3 and 7 read.
    schedules: HealthSchedules = Field(default_factory=HealthSchedules)
    signals: HealthSignalSettings = Field(default_factory=HealthSignalSettings)
    #: Watch window of an applied proposal (decision B18), ``ProposalSpec.watch_days``.
    proposal_watch_days: int = Field(default=7, ge=1, le=MAX_DURATION_DAYS)
    #: Agent to flow after approval (decision B12): shadow, then watch.
    flow_shadow_days: int = Field(default=3, ge=1, le=MAX_DURATION_DAYS)
    flow_watch_days: int = Field(default=7, ge=1, le=MAX_DURATION_DAYS)
    flow_shadow_min_pairs: int = Field(default=5, ge=1, le=1_000)
    #: The analysis layer and its ceiling (#101 S9).
    analysis: HealthAnalysisSettings = Field(default_factory=HealthAnalysisSettings)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _valid_name(value)

    @field_validator("scope_agent_ids", "scope_flow_ids")
    @classmethod
    def _unique(cls, ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(ids))


def _read(stored: object) -> HealthSettings:
    if not isinstance(stored, dict):
        return HealthSettings()
    # A key no field reads any more (a removed setting) is dropped, not refused.
    known = {key: value for key, value in stored.items() if key in HealthSettings.model_fields}
    for key, field in HealthSettings.model_fields.items():
        nested = field.annotation
        if isinstance(known.get(key), dict) and isinstance(nested, type) and issubclass(nested, BaseModel):
            known[key] = {k: v for k, v in known[key].items() if k in nested.model_fields}
    try:
        return HealthSettings.model_validate(known)
    except ValidationError as exc:
        # A value a later level's field no longer accepts, or a hand edit: the
        # defaults, Platform Health off. The reason stays in the logs.
        logger.warning("Stored Platform Health settings do not validate, using the defaults: %s", exc.errors()[:3])
        return HealthSettings()


async def load_health_settings(db: AsyncSession) -> HealthSettings:
    """The stored settings, their defaults when none (read on every call: no cache)."""
    from app.models.platform_settings import PlatformSettings

    stored = (
        await db.execute(
            select(PlatformSettings.health_settings)
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _read(stored)


async def save_health_settings(
    db: AsyncSession, new: HealthSettings, *, actor: User, request: Request | None = None
) -> tuple[HealthSettings, HealthSettings]:
    """Store *new*; ``(before, after)``. Audited, flushed, not committed.

    The caller applies the revocation a narrowed scope implies, in the same
    transaction. The name is a display text only: the built-in fleet's rows
    keep the platform's name (``read_only``, #101).
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.services.audit import log_audit

    row = await get_platform_settings(db)
    before = _read(row.health_settings)
    row.health_settings = new.model_dump(mode="json")
    await log_audit(
        db,
        user=actor,
        action="configure",
        entity_type="health_settings",
        summary="Updated the Platform Health settings",
        details={"before": before.model_dump(mode="json"), "after": new.model_dump(mode="json")},
        request=request,
    )
    return before, new


def scope_narrowed(before: HealthSettings, after: HealthSettings) -> bool:
    """Whether *after* lets Platform Health read less than *before*.

    Switching Platform Health off is not one: it stops every read (the consent
    rule reads nothing while it is off) and takes back no consent, so what was
    derived stays for when it is switched on again.
    """
    if not after.enabled:
        return False
    if after.scope_mode == "all":
        return False
    if before.scope_mode == "all":
        return True
    return bool(
        set(before.scope_agent_ids) - set(after.scope_agent_ids)
        or set(before.scope_flow_ids) - set(after.scope_flow_ids)
    )
