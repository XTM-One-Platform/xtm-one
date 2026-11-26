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

"""The inventory query functions every agent reads the organization's assets and software with (#2443, S1).

Core read functions, built next to ``get_organization_context`` for every agent
run and turn the profile is offered to (``organization_context_injection.tools_enabled_for``),
none in Copilot mode (no inventory). Each answers one question with SQL paging,
filtering and grouping (``services/organization_inventory_query.py``): an
inventory of thousands of assets is queried, never listed into the prompt or
paged whole.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_SOURCE = "builtin:organization_context"

SEARCH_ASSETS = "search_assets"
GET_ASSET = "get_asset"
LOOKUP_ASSETS = "lookup_assets"
SEARCH_SOFTWARE = "search_software"
GET_SOFTWARE = "get_software"
FIND_ASSETS_RUNNING = "find_assets_running"
AGGREGATE_INVENTORY = "aggregate_inventory"
LIST_ASSET_GROUPS = "list_asset_groups"
LIST_PRIMARY_ASSETS = "list_primary_assets"
LIST_INVENTORY_CHANGES = "list_inventory_changes"

#: Every function name, in the order the prompt line names them.
INVENTORY_QUERY_TOOLS: tuple[str, ...] = (
    SEARCH_ASSETS,
    GET_ASSET,
    LOOKUP_ASSETS,
    SEARCH_SOFTWARE,
    GET_SOFTWARE,
    FIND_ASSETS_RUNNING,
    AGGREGATE_INVENTORY,
    LIST_ASSET_GROUPS,
    LIST_PRIMARY_ASSETS,
    LIST_INVENTORY_CHANGES,
)

_PAGE = {
    "offset": {"type": "integer", "description": "First row (default 0); a page names the next one."},
    "limit": {"type": "integer", "description": "Rows a page (default 50, max 200)."},
}
_LIST = {"type": "array", "items": {"type": "string"}}
_ASSET_FILTERS = {
    "category": {**_LIST, "description": "host, application, network_device, cloud_resource... (any of)."},
    "criticality": {**_LIST, "description": "critical, high, medium, low, unknown (any of)."},
    "internet_facing": {**_LIST, "description": "yes, no, unknown."},
    "os_family": {**_LIST, "description": "windows, linux, macos... (any of)."},
    "source": {**_LIST, "description": "Sources that report the asset (any of)."},
    "group": {**_LIST, "description": "Asset group ids (any of)."},
}


async def _organization(db: AsyncSession) -> uuid.UUID | None:
    """The primary organization with its asset store built, ``None`` when it has no profile yet."""
    from app.services import organization_asset_store as store
    from app.services.organization_context import current_version

    row = await current_version(db)
    org_id = getattr(row, "organization_id", None)
    if not isinstance(org_id, uuid.UUID):
        return None
    async with db.begin_nested():
        built = await store.ensure_built(db, org_id)
    return built or org_id


def build_inventory_query_tools() -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The inventory query functions; none in Copilot mode, where the profile is its identity alone."""
    from app.services.organization_context import identity_only

    executor = CallableToolExecutor()
    if identity_only():
        return [], executor

    def _run(
        what: str, call: Callable[[AsyncSession, uuid.UUID], Awaitable[dict[str, Any] | None]]
    ) -> Callable[[], Awaitable[str]]:
        async def _answer() -> str:
            from app.core.database import async_session
            from app.services import organization_inventory_query as q

            try:
                async with async_session() as db:
                    org_id = await _organization(db)
                    if org_id is None:
                        return "The organization has no profile yet, so no inventory. Do not guess it."
                    return q.render(await call(db, org_id), what=what)
            except q.QueryError as exc:
                return f"Error: {exc}."
            except Exception:
                logger.warning("Inventory query %s failed", what, exc_info=True)
                return f"Error: the inventory could not be read right now ({what}). Say so; do not guess it."

        return _answer

    async def _search_assets(query: str = "", offset: int = 0, limit: int = 0, **filters: Any) -> str:
        from app.services import organization_inventory_query as q

        known = {k: v for k, v in filters.items() if k in _ASSET_FILTERS}
        return await _run(
            "Assets",
            lambda db, org: q.search_assets(db, org, query=query, offset=offset, limit=limit, **known),
        )()

    async def _get_asset(asset: str = "", software_offset: int = 0, software_limit: int = 0) -> str:
        from app.services import organization_inventory_query as q

        return await _run(
            "Asset",
            lambda db, org: q.get_asset(db, org, asset, software_offset=software_offset, software_limit=software_limit),
        )()

    async def _lookup_assets(values: list[str] | None = None) -> str:
        from app.services import organization_inventory_query as q

        return await _run("Lookup", lambda db, org: q.lookup_assets(db, org, list(values or [])))()

    async def _search_software(
        query: str = "",
        software_type: list[str] | None = None,
        vendor: str = "",
        unknown_version: bool | None = None,
        offset: int = 0,
        limit: int = 0,
    ) -> str:
        from app.services import organization_inventory_query as q

        return await _run(
            "Software",
            lambda db, org: q.search_software(
                db,
                org,
                query=query,
                software_type=software_type or (),
                vendor=vendor,
                unknown_version=unknown_version,
                offset=offset,
                limit=limit,
            ),
        )()

    async def _get_software(software: str = "", installations_offset: int = 0, installations_limit: int = 0) -> str:
        from app.services import organization_inventory_query as q

        return await _run(
            "Software detail",
            lambda db, org: q.get_software(
                db,
                org,
                software,
                installations_offset=installations_offset,
                installations_limit=installations_limit,
            ),
        )()

    async def _find_assets_running(
        product: str = "",
        vendor: str = "",
        cpe: str = "",
        version: str = "",
        min_version: str = "",
        max_version: str = "",
        min_inclusive: bool = True,
        max_inclusive: bool = False,
        through_dependencies: bool = True,
        offset: int = 0,
        limit: int = 0,
    ) -> str:
        from app.services import organization_inventory_query as q

        return await _run(
            "Assets running it",
            lambda db, org: q.find_assets_running(
                db,
                org,
                product=product,
                vendor=vendor,
                cpe=cpe,
                version=version,
                min_version=min_version,
                max_version=max_version,
                min_inclusive=min_inclusive,
                max_inclusive=max_inclusive,
                through_dependencies=through_dependencies,
                offset=offset,
                limit=limit,
            ),
        )()

    async def _aggregate(entity: str = "assets", group_by: str = "category", query: str = "", **filters: Any) -> str:
        from app.services import organization_inventory_query as q

        known = {k: v for k, v in filters.items() if k in _ASSET_FILTERS}
        return await _run(
            "Counts",
            lambda db, org: q.aggregate_inventory(db, org, entity=entity, group_by=group_by, query=query, **known),
        )()

    async def _groups(query: str = "", offset: int = 0, limit: int = 0) -> str:
        from app.services import organization_inventory_query as q

        return await _run(
            "Asset groups", lambda db, org: q.list_asset_groups(db, org, query=query, offset=offset, limit=limit)
        )()

    async def _primary(query: str = "", offset: int = 0, limit: int = 0) -> str:
        from app.services import organization_inventory_query as q

        return await _run(
            "Primary assets",
            lambda db, org: q.list_primary_assets(db, org, query=query, offset=offset, limit=limit),
        )()

    async def _changes(since: str = "", before_id: int | None = None, limit: int = 0) -> str:
        from app.services import organization_inventory_query as q

        return await _run(
            "Inventory changes",
            lambda db, org: q.list_inventory_changes(db, org, since=since, before_id=before_id, limit=limit),
        )()

    specs: tuple[tuple[str, Callable[..., Awaitable[str]], str, dict[str, Any], list[str]], ...] = (
        (
            SEARCH_ASSETS,
            _search_assets,
            "Search the organization's assets (inventory, thousands of rows): text on name, OS, owner or id "
            "prefix, plus filters; one page with the total. Count with aggregate_inventory, not by paging.",
            {"query": {"type": "string", "description": "Text to match."}, **_ASSET_FILTERS, **_PAGE},
            [],
        ),
        (
            GET_ASSET,
            _get_asset,
            "Read one asset: fields and their sources, its software (paged), groups and primary assets.",
            {
                "asset": {"type": "string", "description": "Asset id, 8+ hex prefix, hostname, IP or FQDN."},
                "software_offset": {"type": "integer", "description": "First software row."},
                "software_limit": {"type": "integer", "description": "Software rows (max 200)."},
            },
            ["asset"],
        ),
        (
            LOOKUP_ASSETS,
            _lookup_assets,
            "Resolve up to 200 hostnames, FQDNs, IPs, MACs or source:external_id references to assets.",
            {"values": {**_LIST, "description": "Identifiers to resolve."}},
            ["values"],
        ),
        (
            SEARCH_SOFTWARE,
            _search_software,
            "Search the software the organization runs (name, vendor, product, CPE), with version and asset counts.",
            {
                "query": {"type": "string", "description": "Text to match."},
                "software_type": {**_LIST, "description": "application, operating_system, library... (any of)."},
                "vendor": {"type": "string", "description": "Vendor."},
                "unknown_version": {"type": "boolean", "description": "true: only software with an unknown version."},
                **_PAGE,
            },
            [],
        ),
        (
            GET_SOFTWARE,
            _get_software,
            "Read one software: its versions with asset counts, installations (paged), dependencies and dependants.",
            {
                "software": {"type": "string", "description": "Software id or exact name or product."},
                "installations_offset": {"type": "integer", "description": "First installation row."},
                "installations_limit": {"type": "integer", "description": "Installation rows (max 200)."},
            },
            ["software"],
        ),
        (
            FIND_ASSETS_RUNNING,
            _find_assets_running,
            "Which assets run a software, in an exact version or a range (semantic: 1.10 > 1.9), directly or "
            "through an SBOM dependency (via names what they run). Use it for 'who is affected by'.",
            {
                "product": {"type": "string", "description": "Product (or give cpe)."},
                "vendor": {"type": "string", "description": "Vendor."},
                "cpe": {"type": "string", "description": "Product CPE 2.3."},
                "version": {"type": "string", "description": "Exact version."},
                "min_version": {"type": "string", "description": "Range start (inclusive by default)."},
                "max_version": {"type": "string", "description": "Range end (exclusive by default)."},
                "min_inclusive": {"type": "boolean"},
                "max_inclusive": {"type": "boolean"},
                "through_dependencies": {"type": "boolean", "description": "Default true."},
                **_PAGE,
            },
            [],
        ),
        (
            AGGREGATE_INVENTORY,
            _aggregate,
            "Count assets or software per value of one field (assets: category, subcategory, criticality, "
            "internet_facing, os_family, environment, owner, source; software: software_type, vendor, origin, "
            "cpe_state), with the asset filters.",
            {
                "entity": {"type": "string", "enum": ["assets", "software"]},
                "group_by": {"type": "string", "description": "Field to count by."},
                "query": {"type": "string", "description": "Text to match first."},
                **_ASSET_FILTERS,
            },
            ["group_by"],
        ),
        (
            LIST_ASSET_GROUPS,
            _groups,
            "List the asset groups with their member counts; filter assets by group with search_assets(group=[id]).",
            {"query": {"type": "string", "description": "Text in the name."}, **_PAGE},
            [],
        ),
        (
            LIST_PRIMARY_ASSETS,
            _primary,
            "List the primary assets the inventory supports, with the records and groups linked to each.",
            {"query": {"type": "string", "description": "Text in the name."}, **_PAGE},
            [],
        ),
        (
            LIST_INVENTORY_CHANGES,
            _changes,
            "The inventory change log, newest first: what was created, updated or deleted, by whom, when.",
            {
                "since": {"type": "string", "description": "ISO date or time."},
                "before_id": {"type": "integer", "description": "Next page: the last change id read."},
                "limit": {"type": "integer", "description": "Rows (max 200)."},
            },
            [],
        ),
    )
    definitions: list[ToolDefinition] = []
    for name, handler, description, properties, required in specs:
        executor.register(name, handler)
        definitions.append(
            ToolDefinition(
                name=name,
                description=description,
                input_schema={"type": "object", "properties": properties, "required": required},
                source=_SOURCE,
                priority=0,
                core=True,
            )
        )
    return definitions, executor
