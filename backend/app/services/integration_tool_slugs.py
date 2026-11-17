# SPDX-License-Identifier: Apache-2.0

"""Changing the tool-name suffix of an integration instance.

The suffix (``Integration.tool_slug``, rules in ``app.core.integration_tool_slug``)
names the tools of an instance when an agent binds several integrations of one
provider. It is unique per provider within an owner scope - the company-managed
integrations, or one user's own - so two accounts of one provider an agent may
bind together never ask for the same name; the tool build keeps its ``_N``
counter as the safety net for an agent binding integrations of two scopes.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.integration_tool_slug import tool_slug_error
from app.models.integration import Integration


class ToolSlugError(ValueError):
    """A ``tool_slug`` that cannot be stored on this integration."""


async def validate_tool_slug_change(db: AsyncSession, integration: Integration, value: str) -> str:
    """The normalized *value* to store as *integration*'s ``tool_slug``.

    Raises :class:`ToolSlugError` when the value breaks the pattern or another
    integration of the same provider and owner scope already holds it.
    """
    slug = value.strip().lower() if isinstance(value, str) else value
    error = tool_slug_error(slug)
    if error is not None:
        raise ToolSlugError(error)
    # access_restricted: bypass - a uniqueness count of tool names in the owner scope, reads no credential.
    scope = (
        Integration.is_company_managed.is_(True)
        if integration.is_company_managed
        else (Integration.is_company_managed.is_not(True) & (Integration.user_id == integration.user_id))
    )
    taken = await db.scalar(
        select(func.count())
        .select_from(Integration)
        .where(
            Integration.provider == integration.provider,
            Integration.tool_slug == slug,
            Integration.id != integration.id,
            scope,
        )
    )
    if taken:
        raise ToolSlugError(
            f"another {integration.provider} integration of the same owner already uses the tool suffix '{slug}'."
        )
    return slug
