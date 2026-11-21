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

"""Platform Health Module Analyst (#101 S14, ``dev-docs/platform-health.md`` section 12).

The member that investigates a registered OpenCTI, OpenAEV or OpenCRQ through the
integration its registration created, read-only, and attaches recommendations to
that module's findings. This module holds what decides its reach, so the rule lives
in code and never in a prompt sentence:

* :data:`MODULE_ANALYST_TOOLS`, the explicit allow-list of read tools per provider
  (section 12.3, lock 2). A read tool added later to a module integration is not
  reachable until it is listed here; a name outside the list is refused.
* :func:`allowed_module_tools` and :func:`refuse_module_tool`, applied where the
  member's tools are built and where they execute.
* :func:`registration_in_scope`, the ``module`` target check of ``cycles.start_cycle``.

S14a-1 ships the store, the role, the target type and the allow-list; the member's
seed, triggers and tools follow in S14a-2, the quota exemption and its ceiling in S14b.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tool_classification import infer_tool_type

#: The modules the analyst investigates: ``PlatformRegistration.platform_identifier``,
#: also the provider key of the integration the registration created.
MODULE_PROVIDERS: Final[tuple[str, ...]] = ("opencti", "openaev", "opencrq")

#: The read tools the analyst may call, per provider (section 12.3, lock 2). Every
#: entry must classify as a read (``tool_classification.infer_tool_type``):
#: ``tests/test_health_module_analyst.py`` fails on one that does not.
MODULE_ANALYST_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    # Connectors, feeds, playbooks and tasks are read through the read tools S14a-2
    # adds: today's OpenCTI tools reach them only through ``manage_*`` actions.
    "opencti": ("get_opencti_platform_version",),
    "openaev": ("query_openaev_connectors",),
    "opencrq": (
        "get_opencrq_platform_version",
        "list_opencrq_connectors",
        "list_opencrq_connector_syncs",
        "list_opencrq_jobs",
        "get_opencrq_job",
    ),
}

#: The refusal a tool outside the allow-list answers, the read-only reason.
REFUSAL_REASON: Final = (
    "The Module Analyst reads modules only through its allow-listed read tools; this tool is not one of them."
)


def allowed_module_tools(provider: str) -> frozenset[str]:
    """The allow-listed read tools of *provider*; empty for a provider the analyst does not read."""
    return frozenset(MODULE_ANALYST_TOOLS.get(provider, ()))


def filter_module_tools(provider: str, tool_names: Iterable[str]) -> list[str]:
    """*tool_names* the member may see for *provider*: allow-listed and read, in order."""
    allowed = allowed_module_tools(provider)
    return [name for name in tool_names if name in allowed and infer_tool_type(name) == "read"]


def refuse_module_tool(provider: str, tool_name: str) -> str | None:
    """The refusal for a call of *tool_name* on *provider*, ``None`` when it may run.

    Both locks of section 12.3 are checked: the name is on the allow-list, and the
    shared classifier reads it as a read (so a mislisted write is still refused).
    """
    if tool_name not in allowed_module_tools(provider) or infer_tool_type(tool_name) != "read":
        return REFUSAL_REASON
    return None


async def registration_in_scope(db: AsyncSession, registration_id: uuid.UUID) -> bool | None:
    """Whether a ``module`` target may get a cycle; ``None`` when it is no module registration.

    In scope: an active registration of a module the analyst reads, with the
    integration it reads through (section 12.2: an inactive one gets no cycle).
    """
    from app.models.platform_registration import PlatformRegistration

    row = (
        await db.execute(
            select(
                PlatformRegistration.platform_identifier,
                PlatformRegistration.status,
                PlatformRegistration.integration_id,
            ).where(PlatformRegistration.id == registration_id)
        )
    ).one_or_none()
    if row is None or row.platform_identifier not in MODULE_PROVIDERS:
        return None
    return row.status == "active" and row.integration_id is not None
