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

"""Platform wait policy: where it is stored, what the environment pins.

The bounds of the ``wait`` primitive used to live on the General Assistant
configuration although they bound EVERY run. They are platform AI settings
now (``PlatformSettings.maximum_wait_time`` / ``maximum_total_wait_time`` /
``wait_unbounded_with_until``, Settings > Agents and automations > Agent runs), with an optional
environment pin and per-agent / per-assignment overrides.

This module is the one reader every call site uses (chat, General Assistant,
assignments, evaluations), so the resolution order cannot drift:
assignment > agent > platform, the platform values being the ceilings.
The pure rules live in ``app.tools.wait_policy``; this module adds the
database and the environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.tools.wait_policy import (
    WaitPolicy,
    clamp_maximum_total_wait_time,
    clamp_maximum_wait_time,
    normalize_wait_policy_override,
    resolve_wait_policy,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

#: ``PlatformSettings`` field -> ``Settings`` (environment) field that pins it.
ENV_PINNED_WAIT_FIELDS: dict[str, str] = {
    "maximum_wait_time": "agent_maximum_wait_time",
    "maximum_total_wait_time": "agent_maximum_total_wait_time",
}


def env_locked_wait_fields() -> set[str]:
    """The wait fields the environment pins, hence read-only in Settings."""
    from app.core.config import settings

    return {field for field, env_field in ENV_PINNED_WAIT_FIELDS.items() if getattr(settings, env_field, None)}


def platform_wait_policy(ps: object | None) -> WaitPolicy:
    """The platform policy from a ``PlatformSettings`` row, the environment winning."""
    from app.core.config import settings

    policy = WaitPolicy.from_config(ps)
    env_per_call = getattr(settings, "agent_maximum_wait_time", None)
    env_total = getattr(settings, "agent_maximum_total_wait_time", None)
    return WaitPolicy(
        per_call_seconds=clamp_maximum_wait_time(env_per_call) if env_per_call else policy.per_call_seconds,
        total_seconds=clamp_maximum_total_wait_time(env_total) if env_total else policy.total_seconds,
        unbounded_with_until=policy.unbounded_with_until,
    )


#: The platform AI settings an administrator edits for agent runs.
WAIT_SETTINGS_FIELDS: tuple[str, ...] = (
    "maximum_wait_time",
    "maximum_total_wait_time",
    "wait_unbounded_with_until",
)


class WaitSettingsLockedError(PermissionError):
    """A write to a wait field the environment pins (read-only in Settings)."""


def serialize_wait_settings(ps: object | None) -> dict[str, object]:
    """The wait settings as Settings > AI shows them: effective values, ceilings, env locks."""
    from app.tools.wait_policy import MAXIMUM_TOTAL_WAIT_TIME_CEILING, MAXIMUM_WAIT_TIME_CEILING

    policy = platform_wait_policy(ps)
    return {
        "maximum_wait_time": policy.per_call_seconds,
        "maximum_total_wait_time": policy.total_seconds,
        "wait_unbounded_with_until": policy.unbounded_with_until,
        "maximum_wait_time_ceiling": MAXIMUM_WAIT_TIME_CEILING,
        "maximum_total_wait_time_ceiling": MAXIMUM_TOTAL_WAIT_TIME_CEILING,
        "wait_env_locked_fields": sorted(env_locked_wait_fields()),
    }


def apply_wait_settings(ps: object, body: dict[str, object]) -> dict[str, object]:
    """Write the wait fields present in *body* on *ps*; return what changed.

    Shared by ``PUT /settings/platform`` and the admin tool
    ``update_platform_ai_settings`` so both validate and clamp alike.  Raises
    :class:`WaitSettingsLockedError` for an env-pinned field and
    :class:`ValueError` for a malformed value (the caller maps them to 403 /
    400).  ``None`` resets a field to its default.
    """
    present = [field for field in WAIT_SETTINGS_FIELDS if field in body]
    locked = sorted(set(present) & env_locked_wait_fields())
    if locked:
        raise WaitSettingsLockedError(
            "Configured via environment variables and read-only: "
            f"{', '.join(locked)}. Update AGENT_MAXIMUM_WAIT_TIME / "
            "AGENT_MAXIMUM_TOTAL_WAIT_TIME and restart the backend."
        )
    changes: dict[str, object] = {}
    for field in present:
        raw = body[field]
        value: object
        if raw is None:
            value = None
        elif field == "wait_unbounded_with_until":
            if not isinstance(raw, bool):
                raise ValueError(f"{field}: must be true or false")
            value = raw
        else:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
                raise ValueError(f"{field}: must be a positive number of seconds")
            clamp = clamp_maximum_wait_time if field == "maximum_wait_time" else clamp_maximum_total_wait_time
            value = clamp(raw)
        if getattr(ps, field, None) != value:
            setattr(ps, field, value)
            changes[field] = value
    return changes


def checked_wait_policy_override(raw: object) -> dict[str, int | bool] | None:
    """Validate an agent's or an assignment's ``wait_policy`` for the API (422 on error)."""
    from fastapi import HTTPException

    try:
        return normalize_wait_policy_override(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def load_platform_wait_policy(db: AsyncSession) -> WaitPolicy:
    """Read the platform policy (a missing row resolves to the defaults)."""
    from app.models.platform_settings import PlatformSettings

    row = (
        await db.execute(
            select(
                PlatformSettings.maximum_wait_time,
                PlatformSettings.maximum_total_wait_time,
                PlatformSettings.wait_unbounded_with_until,
            ).limit(1)
        )
    ).first()
    return platform_wait_policy(row)


async def resolve_run_wait_policy(
    db: AsyncSession,
    *,
    agent: object | None = None,
    assignment: object | None = None,
) -> WaitPolicy:
    """The wait policy of one run: assignment > agent > platform."""
    platform = await load_platform_wait_policy(db)
    return resolve_wait_policy(
        platform,
        getattr(agent, "wait_policy", None),
        getattr(assignment, "wait_policy", None),
    )
