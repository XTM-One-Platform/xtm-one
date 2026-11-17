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

"""The identity a seeded fleet automation runs as (#9188, epic #8975).

The 12 seeded fleet automations (``builtin_agents.FLEET_OPT_IN_ASSIGNMENTS``)
are owned by the company-managed seed owner, who generally has no OpenCTI or
OpenAEV access. A run resolves its identity as triggering user, then
``run_as_user_id``, then the owner, and the OpenCTI / OpenAEV trigger
provisioning takes its token the same way, so an automation switched on with
no run-as would run and provision as the seed owner. Hence:

* switching one on with no run-as stores the administrator who switches it on
  as ``run_as_user_id`` in the same write; an explicit run-as in the request
  wins (:func:`apply_fleet_run_as`);
* the switch, and a run-as change on an enabled one, is refused (400) when the
  run-as user has no connected integration for a provider the automation
  needs: its trigger provider and its agent's per-user template bindings
  among OpenCTI, OpenAEV and OpenCRQ (:func:`missing_providers`);
* the trigger provisioning never falls back to the seed owner for one
  (:func:`provisioning_user_id`): no run-as, no provisioning, the reason in
  its state.

The hourly indicator decay re-check is not a fleet automation and keeps its
own rule (the administrator picks its run-as).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: Providers whose per-user connection a fleet automation's run-as must hold.
MODULE_PROVIDERS: tuple[str, ...] = ("opencti", "openaev", "opencrq")

_PROVIDER_LABELS = {"opencti": "OpenCTI", "openaev": "OpenAEV", "opencrq": "OpenCRQ"}

#: Provisioning state error of a fleet automation with no run-as user.
NO_RUN_AS_REASON = (
    "no run-as user: a fleet automation never provisions as the seed owner; "
    "switch it on again as an administrator with the module connected"
)


def is_fleet_automation(agent: Any, assignment: Any) -> bool:
    """Whether *assignment* is one of the seeded fleet automations."""
    from app.agents.builtin_agents import is_fleet_opt_in_assignment

    return is_fleet_opt_in_assignment(agent, assignment)


def required_providers(agent: Any, assignment: Any) -> list[str]:
    """The module providers the automation acts through, in a stable order."""
    config = getattr(assignment, "trigger_config", None)
    wanted = {str(config.get("provider") or "")} if isinstance(config, dict) else set()
    wanted |= {str(p) for p in (getattr(agent, "integration_template_bindings", None) or [])}
    return [p for p in MODULE_PROVIDERS if p in wanted]


async def missing_providers(db: AsyncSession, user_id: uuid.UUID, providers: list[str]) -> list[str]:
    """The *providers* for which *user_id* has no connected integration a run would pick."""
    if not providers:
        return []
    from app.models.integration import Integration
    from app.services.integration_tools import template_candidate_filter
    from app.services.integration_user_credentials import connected_for_clause

    rows = await db.execute(
        select(Integration.provider)
        .where(
            Integration.provider.in_(providers),
            await template_candidate_filter(db, user_id, company_automation=True),
            connected_for_clause(user_id),
        )
        .distinct()
    )
    found = {str(p) for p in rows.scalars().all()}
    return [p for p in providers if p not in found]


def missing_refusal(missing: list[str]) -> str:
    names = ", ".join(_PROVIDER_LABELS.get(p, p) for p in missing)
    return (
        f"The run-as user has no connected {names} integration: a fleet automation runs as that user. "
        f"Connect {names} for that user, or pick a run-as user who has it."
    )


async def apply_fleet_run_as(
    db: AsyncSession,
    *,
    agent: Any,
    assignment: Any,
    actor_id: uuid.UUID,
    was_enabled: bool,
    run_as_changed: bool,
) -> None:
    """Default the run-as to *actor_id* on a switch-on, then check its connections (400).

    Called after the update wrote ``enabled`` and ``run_as_user_id``; a no-op
    for any assignment that is not a seeded fleet automation, and for a write
    that neither switches it on nor changes the run-as of an enabled one.
    """
    if not is_fleet_automation(agent, assignment) or not assignment.enabled:
        return
    switched_on = not was_enabled
    if not switched_on and not run_as_changed:
        return
    if switched_on and assignment.run_as_user_id is None:
        assignment.run_as_user_id = actor_id
    run_as = assignment.run_as_user_id
    if run_as is None:
        raise HTTPException(status_code=400, detail="A fleet automation needs a run-as user to be on.")
    missing = await missing_providers(db, run_as, required_providers(agent, assignment))
    if missing:
        raise HTTPException(status_code=400, detail=missing_refusal(missing))


def provisioning_user_id(assignment: Any, agent: Any) -> Any | None:
    """The user whose token provisions *assignment*'s triggers; ``None`` = do not provision.

    A fleet automation provisions as its run-as user only, never as the seed
    owner; every other assignment keeps run-as, then owner.
    """
    run_as = getattr(assignment, "run_as_user_id", None)
    if run_as is not None:
        return run_as
    if is_fleet_automation(agent, assignment):
        return None
    return getattr(assignment, "user_id", None)
