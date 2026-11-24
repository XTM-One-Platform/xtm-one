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
OPENCTI_LINK_TOOL = "link_tracked_organization_to_opencti"
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

    async def _link_opencti(organization: str = "", identity_id: str = "", create: bool = False) -> str:
        from app.core.database import async_session
        from app.services import organizations as orgs
        from app.services.organization_opencti import link_identity, opencti_gql

        if not str(organization or "").strip():
            return "Not linked: name the tracked organization (name or id)."
        wanted = str(identity_id or "").strip()
        if not wanted and not create:
            return "Not linked: give the OpenCTI Organization id, or create true to create one named like it."
        try:
            async with async_session() as db:
                person = await admin(db)
                if person is None:
                    return refusal
                target = await orgs.resolve(db, str(organization))
                if target is None or target.role != "tracked":
                    return f"Not linked: no tracked organization is named {organization!r} (or several are)."
                gql = await opencti_gql(db, str(person.id))
                if isinstance(gql, str):
                    return f"Not linked: {gql}."
                # The same service as Settings > Organization (POST /organizations/ID/opencti-identity).
                try:
                    linked, type_set = await link_identity(
                        gql, name=target.name, relationship=target.relationship or "", identity_id=wanted, create=create
                    )
                except (LookupError, RuntimeError, ValueError) as exc:
                    return f"Not linked: {str(exc)[:300]}"
                target.opencti_organization_id = linked
                await db.commit()
                return json.dumps(
                    {
                        "organization": target.name,
                        "organization_id": str(target.id),
                        "opencti_organization_id": linked,
                        "organization_type_set": type_set,
                    }
                )
        except Exception:
            logger.warning("Tracked organization OpenCTI link failed", exc_info=True)
            return "Error: the OpenCTI link could not be saved right now. Say so; nothing was changed."

    link_definition = ToolDefinition(
        name=OPENCTI_LINK_TOOL,
        description="Link a tracked organization to an OpenCTI Organization: id or create.",
        input_schema={
            "type": "object",
            "properties": {
                "organization": {"type": "string"},
                "identity_id": {"type": "string"},
                "create": {"type": "boolean"},
            },
            "required": ["organization"],
        },
    )

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
    return [(definition, _reconcile), (link_definition, _link_opencti)]
