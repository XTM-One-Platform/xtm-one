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

"""``get_organization_context``: the organization profile, for every agent.

A ``core`` tool (``ToolDefinition.core``) built for every agent run and
turn, whatever the agent's built-in selection and deny list: an analyst ranks
by the crown jewels, the regions and the risk appetite it reads here, and
names the fields that are not filled instead of guessing them (#8987). The
one thing that removes it is an explicit ``organization_context_mode`` off on
the agent or the assignment (``organization_context_injection.tools_enabled``,
#11533). Read-only; the profile and its rules are
``services/organization_context.py``'s.

One tool rather than a sibling (#11533): the categories the prompt announces
are read with the same call the agent already knows. ``category`` names one
(``organization_context_categories.CATEGORIES``, generated from the model),
``query`` keeps the rows holding a text (a vendor, a product, a CPE, a crown
jewel), ``offset`` and ``limit`` page it so a list of any length is read
whole; ``category="*"`` lists the categories with their sizes.

``category="enterprise_knowledge"`` (#11543) is the organization's own
procedures: without a query it names the linked knowledge bases and which
ones the caller can search; with a query it answers the passages of the
linked knowledge bases THE CALLER can read (``user_id``), never others.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.organization_context import OrganizationProfile

ORGANIZATION_CONTEXT_TOOL = "get_organization_context"
_SOURCE = "builtin:organization_context"


def build_organization_context_tool(
    *, user_id: object = None, is_admin: bool = False, agent: object = None
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The one read tool of the organization profiles (no caller scope: platform-wide data).

    Without arguments, the primary organization's profile, as before #9157;
    ``organization="*"`` lists the tracked organizations; a name or an id
    reads that tracked organization's profile; ``category`` reads one
    category of the primary profile by page (#11533). *user_id* (and
    *is_admin*) is the person the turn acts for: the enterprise knowledge
    passages come only from the knowledge bases that person can read; no
    person, no passage. *agent* (the turn's agent) decides whether the reading
    names the CTEM-only fields and asks for the brief notice: only a run that
    is CTEM work does (``organization_context.ctem_work_for``, #1384).
    """
    caller = _uuid(user_id)
    # A snapshot of the columns the CTEM signal reads: the tool runs in its own session.
    run_agent = (
        None
        if agent is None
        else SimpleNamespace(
            id=getattr(agent, "id", None),
            slug=getattr(agent, "slug", None),
            is_builtin=bool(getattr(agent, "is_builtin", False)),
            builtin_tool_ids=list(getattr(agent, "builtin_tool_ids", None) or []),
        )
    )

    async def _get(
        organization: str = "",
        category: str = "",
        query: str = "",
        offset: int = 0,
        limit: int = 0,
    ) -> str:
        from app.core.database import async_session
        from app.services import organization_context_categories as cats
        from app.services import organizations as orgs
        from app.services.organization_context import asset_list_counts, current_version, hydrated_profile
        from app.services.organization_context_injection import category_counts

        ref = str(organization or "").strip()
        wanted = str(category or "").strip().lower()
        try:
            async with async_session() as db:
                if wanted and wanted != "*" and cats.get(wanted) is not None and not cats.available():
                    # Copilot mode (#1690): the profile is its identity alone, no category is read.
                    return cats.render_categories({})
                if wanted:
                    row = await current_version(db)
                    # #1543: the asset and group rows come from the asset records, paged like every category.
                    profile = await hydrated_profile(db, row)
                    tracked = [o for o in await orgs.list_organizations(db) if o.role == "tracked"]
                    if wanted == "*":
                        return cats.render_categories(
                            category_counts(
                                profile,
                                tracked_count=len(tracked),
                                asset_counts=await asset_list_counts(db, getattr(row, "organization_id", None)),
                            )
                        )
                    found_category = cats.get(wanted)
                    if found_category is None:
                        return (
                            f"No category {wanted!r}. One of: {', '.join(cats.available_names())} "
                            '(get_organization_context(category="*") lists them with their sizes).'
                        )
                    if found_category.name == cats.ENTERPRISE_KNOWLEDGE:
                        return await _enterprise_knowledge(db, profile, str(query or ""), _int(limit))
                    rows = (
                        cats.tracked_rows(tracked)
                        if not found_category.field
                        else cats.rows_of(profile, found_category)
                    )
                    org_id = getattr(row, "organization_id", None)
                    if found_category.name == "assets_exposure" and isinstance(org_id, uuid.UUID):
                        merged = await _inventory_assets(db, org_id, str(query or ""), _int(offset), _int(limit))
                        if merged is not None:
                            return merged
                    if found_category.name == cats.SOFTWARE and isinstance(org_id, uuid.UUID):
                        rows = await _software_rows(db, org_id) or rows
                    return cats.render_page(
                        found_category,
                        rows,
                        query=str(query or ""),
                        offset=_int(offset),
                        limit=_int(limit) or cats.DEFAULT_PAGE,
                    )
                if not ref:
                    return await _primary_block(db)
                if ref == orgs.EVERY_ORGANIZATION:
                    return orgs.render_tracked_list(await orgs.list_organizations(db))
                found = await orgs.resolve(db, ref)
                if found is None:
                    return (
                        f"No active organization is named {ref!r} (or several are). "
                        'Call get_organization_context(organization="*") for the list.'
                    )
                if found.role == "primary":
                    return await _primary_block(db)
                return orgs.render_tracked_for_agent(found, await current_version(db, found.id))
        except Exception:
            logger.warning("Organization context read failed", exc_info=True)
            return (
                "Error: the organization profile could not be read right now. Say so in your output; do not guess it."
            )

    async def _primary_block(db: AsyncSession) -> str:
        """The primary profile as an agent reads it, its asset and group counts from the records (#1543)."""
        from app.services.organization_context import (
            asset_list_counts,
            ctem_work_for,
            current_version,
            render_for_agent,
        )

        primary = await current_version(db)
        return render_for_agent(
            primary,
            ctem_work=await ctem_work_for(db, run_agent),
            asset_counts=await asset_list_counts(db, primary.organization_id if primary else None),
        )

    async def _inventory_assets(
        db: AsyncSession, organization_id: uuid.UUID, query: str, offset: int, limit: int
    ) -> str | None:
        """The merged CTEM Inventory assets, paged by the store, ``field:value`` terms as filters (#1774).

        ``None`` (the profile's rows answer) while the store is not built or cannot be read.
        """
        from app.services import organization_asset_store as store
        from app.services import organization_context_categories as cats

        try:
            async with db.begin_nested():
                built = await store.ensure_built(db, organization_id)
            if built is None:
                return None
            filters, text = cats.split_asset_query(query)
            size = max(1, min(limit or cats.DEFAULT_PAGE, cats.MAX_PAGE))
            page = await store.list_assets(db, built, q=text, offset=offset, limit=size, filters=filters)
        except Exception:
            logger.warning("Inventory asset read failed", exc_info=True)
            return None
        return cats.render_asset_page(list(page.items), total=page.total, query=query, offset=offset, limit=size)

    async def _software_rows(db: AsyncSession, organization_id: uuid.UUID) -> list[dict[str, object]]:
        """The organization's Software entities (#1495), synced and hand-typed; ``[]`` when it has none."""
        from app.services import organization_asset_store as store
        from app.services import organization_software_store as software_store

        try:
            async with db.begin_nested():
                await store.ensure_built(db, organization_id)
            page = await software_store.list_software(db, organization_id, limit=software_store.MAX_PAGE)
            items = list(page.items)
            while len(items) < page.total:
                more = await software_store.list_software(
                    db, organization_id, offset=len(items), limit=software_store.MAX_PAGE
                )
                if not more.items:
                    break
                items += more.items
            return [_software_row(item) for item in items]
        except Exception:  # the profile's typed rows still answer
            logger.warning("Software read failed", exc_info=True)
            return []

    def _software_row(item: dict[str, object]) -> dict[str, object]:
        """One Software as an agent reads it: what identifies it and where it runs, no internal field."""
        keep = ("name", "vendor", "product", "type", "cpe", "cpe_state", "purl", "origin", "sources")
        return {
            **{key: item.get(key) for key in keep},
            "version_count": item.get("version_count", 0),
            "asset_count": item.get("asset_count", 0),
        }

    async def _enterprise_knowledge(db: AsyncSession, profile: OrganizationProfile, query: str, limit: int) -> str:
        from app.services import organization_enterprise_knowledge as ek
        from app.services.vector import make_search_knowledge_fn

        links = await ek.linked_knowledge_bases(db, profile, user_id=caller, is_admin=is_admin)
        if not query.strip():
            return ek.render_links(links)
        readable = [link.knowledge_base_id for link in links if link.readable]
        search = make_search_knowledge_fn(db, str(caller or ""), readable)

        async def _search(text: str, count: int, kb_ids: list[str]) -> str:
            return await search(text, count, scope_kb_ids=kb_ids)

        return await ek.search_passages(links, query, limit=limit or ek.DEFAULT_PASSAGES, search=_search)

    executor = CallableToolExecutor()
    executor.register(ORGANIZATION_CONTEXT_TOOL, _get)
    definition = ToolDefinition(
        name=ORGANIZATION_CONTEXT_TOOL,
        description=(
            "Read the organization profile: sector, regions, size, primary assets and business "
            "services, software, PIRs, risk appetite, loaded hourly rate and "
            "currency, ticketing target, and the fields not filled. Read it before ranking, "
            "scoping or estimating; cite the fields you used and name the missing ones, never guess them. "
            'Long lists are categories: category="*" lists them with their sizes; category="software" (any listed name) '
            "reads one by page (query filters the rows, offset/limit page it, so a list of any length is read whole). "
            'category="tech_stack" and "crown_jewels" are deprecated aliases of "software" and "primary_assets".'
        ),
        input_schema={
            "type": "object",
            "properties": {
                "organization": {
                    "type": "string",
                    "description": "A tracked organization (name or id); '*' lists them. Omit for your own.",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "One category of your organization (e.g. software, primary_assets, assets_exposure, "
                        "prioritization_policy, enterprise_knowledge); '*' lists them."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "With category: keep the rows holding this text (assets_exposure also takes field:value, "
                        "e.g. os_family:windows category:host); with enterprise_knowledge, the passages' topic."
                    ),
                },
                "offset": {"type": "integer", "description": "With category: first row (default 0)."},
                "limit": {"type": "integer", "description": "With category: rows a page (default 50, max 200)."},
            },
            "required": [],
        },
        source=_SOURCE,
        priority=0,
        core=True,
    )
    return [definition], executor


def _uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _int(value: object) -> int:
    try:
        return max(0, int(str(value or 0)))
    except ValueError:
        return 0
