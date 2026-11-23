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

"""Whom the module checkers' reads act as: the "Run checks as" user (dev-docs/platform-health.md 12.3).

A Platform Health cycle starts its run with nobody triggering it, and the XTM One
administrator generally has no access to OpenCTI or OpenAEV. The module reads
(the platform-managed JWT included) are therefore minted for one user an
administrator picks, ``HealthSettings.module_run_as_user_id``. There is no other
fallback: no service account, no seed owner, no administrator by default. Not set,
or the user lost access to a module, the checker wires no read for it and the
module card asks for a run-as user (:func:`run_as_state`).

The access check follows the fleet run-as rule (``fleet_run_as.missing_providers``,
#9188): the user must reach the registration's own integration (grants included)
and that integration must be connected for them.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: Display names of the module providers, for the messages people read.
MODULE_LABELS = {"opencti": "OpenCTI", "openaev": "OpenAEV", "opencrq": "OpenCRQ"}

RunAsState = Literal["ok", "not_set", "no_access"]


def _label(provider: str) -> str:
    return MODULE_LABELS.get(provider, provider)


def run_as_needed_reason(provider: str) -> str:
    """What a checker run without a usable run-as user gives its checks."""
    return (
        f"Checks need a run-as user with {_label(provider)} access: set one in "
        "Settings > Platform Health > Run checks as."
    )


#: The reason codes of a refused "Run checks as" user: the client words them from its catalogs (13bis.5),
#: the English ``message`` is kept for a client that does not know the code.
RUN_AS_MISSING_MODULE_ACCESS = "run_as_missing_module_access"
RUN_AS_INACTIVE = "run_as_inactive"


def run_as_refusal(missing: list[str]) -> str:
    """The English line of a run-as user who cannot reach a registered module."""
    names = ", ".join(_label(p) for p in missing)
    return (
        f"This user has no access to {names}: Platform Health runs the module checks as this user. "
        f"Give them access to {names}, or pick a user who has it."
    )


def run_as_refusal_detail(missing: list[str]) -> dict[str, object]:
    """The 400 ``detail`` of that refusal: a reason code, the module names and the English line."""
    return {
        "code": RUN_AS_MISSING_MODULE_ACCESS,
        "modules": [_label(p) for p in missing],
        "message": run_as_refusal(missing),
    }


def run_as_inactive_detail() -> dict[str, object]:
    """The 400 ``detail`` of an unknown or deactivated run-as user."""
    return {"code": RUN_AS_INACTIVE, "message": "The run-as user does not exist or is deactivated."}


async def registered_module_integrations(db: AsyncSession) -> dict[str, list[uuid.UUID]]:
    """``{provider: [integration id]}`` of the active module registrations the checks read."""
    from app.models.platform_registration import PlatformRegistration
    from app.services.health.module_analyst import MODULE_PROVIDERS

    rows = (
        await db.execute(
            select(PlatformRegistration.platform_identifier, PlatformRegistration.integration_id)
            .where(
                PlatformRegistration.platform_identifier.in_(MODULE_PROVIDERS),
                PlatformRegistration.status == "active",
                PlatformRegistration.integration_id.is_not(None),
            )
            .order_by(PlatformRegistration.id)
        )
    ).all()
    found: dict[str, list[uuid.UUID]] = {}
    for row in rows:
        found.setdefault(str(row.platform_identifier), []).append(row.integration_id)
    return {p: found[p] for p in MODULE_PROVIDERS if p in found}


async def user_is_active(db: AsyncSession, user_id: uuid.UUID) -> bool:
    from app.models.user import User

    active = (await db.execute(select(User.is_active).where(User.id == user_id))).scalar_one_or_none()
    return bool(active)


async def reaches_integration(db: AsyncSession, user_id: uuid.UUID, integration_id: uuid.UUID) -> bool:
    """Whether *user_id* may act through *integration_id*: granted to them and connected for them."""
    from app.models.integration import Integration
    from app.services.integration_tools import template_candidate_filter
    from app.services.integration_user_credentials import connected_for_clause

    hit = (
        await db.execute(
            select(Integration.id).where(
                Integration.id == integration_id,
                await template_candidate_filter(db, user_id, company_automation=True),
                connected_for_clause(user_id),
            )
        )
    ).first()
    return hit is not None


async def missing_module_access(
    db: AsyncSession, user_id: uuid.UUID, integrations: dict[str, list[uuid.UUID]] | None = None
) -> list[str]:
    """The registered modules *user_id* cannot read (every module when the user is unknown or inactive)."""
    if integrations is None:
        integrations = await registered_module_integrations(db)
    if not integrations:
        return []
    if not await user_is_active(db, user_id):
        return list(integrations)
    missing = []
    for provider, ids in integrations.items():
        for integration_id in ids:
            if not await reaches_integration(db, user_id, integration_id):
                missing.append(provider)
                break
    return missing


async def configured_run_as(db: AsyncSession) -> uuid.UUID | None:
    """The "Run checks as" user stored in the Platform Health settings; ``None`` when not set."""
    from app.services.health.settings import load_health_settings

    return (await load_health_settings(db)).module_run_as_user_id


async def run_as_state(db: AsyncSession, provider: str) -> RunAsState:
    """Whether *provider*'s checks have a usable run-as user (the module card's state, 13.6)."""
    run_as = await configured_run_as(db)
    if run_as is None:
        return "not_set"
    integrations = await registered_module_integrations(db)
    if provider not in integrations:
        return "ok"
    missing = await missing_module_access(db, run_as, {provider: integrations[provider]})
    return "no_access" if missing else "ok"
