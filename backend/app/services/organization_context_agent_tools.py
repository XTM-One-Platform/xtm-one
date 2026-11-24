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

"""The agent tools the organization profile's platform services declare (#1676).

The ``organization_context`` built-in group (``builtin_tools/organization_context_write``)
is a generic layer and names no platform (invariant 18). A platform-specific action on
the profile that an administrator's agent must be able to run, today the OpenCRQ
crown-jewel reconcile of Settings > Organization (``POST /organization-context/opencrq-sync``,
``organization_context_opencrq.run_sync``), is declared here: its definition and its
handler, which the group registers as they are.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.tools.base import ToolDefinition

logger = logging.getLogger(__name__)

OPENCRQ_RECONCILE_TOOL = "reconcile_organization_with_opencrq"
_CRITICALITIES = ("low", "medium", "high", "very_high")

#: One declared tool: its definition and its async handler.
DeclaredTool = tuple[ToolDefinition, Callable[..., Awaitable[str]]]


def declared_organization_tools(
    *, admin: Callable[[Any], Awaitable[Any]], refusal: str, shared_context_clause: str
) -> list[DeclaredTool]:
    """The declared tools, run for the administrator *admin* reads again at the call (else *refusal*)."""

    async def _reconcile(apply: bool = False, tenant_id: str = "", min_criticality: str = "") -> str:
        from sqlalchemy.exc import IntegrityError

        from app.core.database import async_session
        from app.services import organization_context as org_context
        from app.services.organization_context_opencrq import run_sync

        level = str(min_criticality or "")
        if level and level not in _CRITICALITIES:
            return f"Not run: min_criticality is one of {', '.join(_CRITICALITIES)} (empty: the saved sync's)."
        try:
            async with async_session() as db:
                person = await admin(db)
                if person is None:
                    return refusal
                # The same call and commit as the Settings > Organization route.
                plan, errors = await run_sync(
                    db, user_id=person.id, apply=bool(apply), tenant_id=str(tenant_id or ""), min_criticality=level
                )
                if plan is None:
                    return f"Not run: {'; '.join(errors)}"
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    return "Not run: another version was saved at the same time. Run the reconcile again."
                current = await org_context.current_version(db)
                return json.dumps(
                    {
                        "applied": bool(apply),
                        "plan": plan.summary(),
                        "errors": errors,
                        "version": current.version if current else None,
                    },
                    default=str,
                )
        except Exception:
            logger.warning("Organization OpenCRQ reconcile failed", exc_info=True)
            return "Error: the OpenCRQ reconcile could not run right now. Say so; nothing was changed."

    definition = ToolDefinition(
        name=OPENCRQ_RECONCILE_TOOL,
        description=(
            "Reconcile the crown jewels with OpenCRQ's primary assets: apply false answers the "
            "plan, true saves a version of the profile (nothing is written to OpenCRQ)." + shared_context_clause
        ),
        input_schema={
            "type": "object",
            "properties": {
                "apply": {"type": "boolean"},
                "tenant_id": {"type": "string"},
                "min_criticality": {"type": "string", "enum": list(_CRITICALITIES)},
            },
        },
    )
    return [(definition, _reconcile)]
