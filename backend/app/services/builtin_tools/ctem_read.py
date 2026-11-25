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

"""The ``ctem_read`` built-in group (#1337): read the CTEM signals and cases from any agent.

The signal and case tools (``ctem_signals.py``, ``ctem_cases.py``) are wired
only for the built-in CTEM agents that work the loop. This group offers their
three reads to any agent it is bound to, read-only, with the same rules: it
acts for the person the turn or run acts for, signals are that person's own
unless the administrator reach applies (``agent_tool_admin_bypass``), and a
case renders through that person's own source read (``ctem_cases_view``), so
a case they see none of does not exist. No write function is in the group.
XTM One mode only.

``get_ctem_case`` here also answers who opened the case (``opened_by``, the
actor of its ``created`` event) and why each empty section is empty
(``empty_sections``), so an agent reports a blank as a fact, not a guess.

``list_ctem_assets`` (#1782) reads the live asset inventory the CTEM loop
works on (the canonical assets every connected source imported), the same
rows and counts as the inventory screen: a page of
assets with their sources, and the counts by source and by category for the
same filters, so a claim about the inventory is checked live, never read from
code.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

CTEM_READ_GROUP = "ctem_read"
CTEM_READ_TOOL_NAMES: tuple[str, ...] = ("list_ctem_signals", "list_ctem_cases", "get_ctem_case", "list_ctem_assets")
#: The asset filters ``list_ctem_assets`` takes, as the asset list of the UI (``organization_asset_store``).
_ASSET_FILTERS: tuple[str, ...] = ("source", "category", "subcategory", "criticality", "internet_facing")
_SOURCE = f"builtin:{CTEM_READ_GROUP}"
#: History events one ``get_ctem_case`` answer carries (as the case tools).
_HISTORY_PAGE = 20


def offers_ctem_read_tools(builtin_tool_ids: Any) -> bool:
    """Whether a turn or run wires the group: bound to the agent, XTM One mode only."""
    from app.core.config import settings

    if settings.is_copilot_like:
        return False
    return CTEM_READ_GROUP in (builtin_tool_ids or ())


def ctem_read_tools_for(
    agent: Any, builtin_tool_ids: Any, *, user_id: str | None
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """The group's tools for one turn or run, ``None`` when not wired.

    Wired when the group is bound and a person is acted for. A built-in CTEM
    agent already carrying the signal or case tools keeps its own, and this
    group adds only the reads it lacks, never the same function twice.
    """
    from .ctem_cases import offers_ctem_case_tools
    from .ctem_signals import offers_ctem_signal_tools

    if not user_id or not offers_ctem_read_tools(builtin_tool_ids):
        return None
    slug, builtin = getattr(agent, "slug", None), bool(getattr(agent, "is_builtin", False))
    carried: set[str] = set()
    if offers_ctem_signal_tools(slug, is_builtin=builtin):
        carried.add("list_ctem_signals")
    if offers_ctem_case_tools(slug, is_builtin=builtin):
        carried.update({"list_ctem_cases", "get_ctem_case"})
    if carried >= set(CTEM_READ_TOOL_NAMES):
        return None
    defs, executor = build_ctem_read_tools(user_id=user_id, agent_id=str(getattr(agent, "id", "") or ""))
    for name in carried:
        executor.unregister(name)
    return [d for d in defs if d.name not in carried], executor


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def opened_by(events: list[Any]) -> dict[str, Any] | None:
    """Who opened the case: the actor and time of its oldest ``created`` event, ``None`` without one."""
    created = [e for e in events if getattr(e, "kind", None) == "created" and getattr(e, "at", None) is not None]
    if not created:
        return None
    first = min(created, key=lambda e: e.at)
    return {"actor": first.actor, "at": first.at.isoformat(), "how": first.to_value}


def empty_sections(
    rendered: dict[str, Any], running_versions: Any, inventory_sources: list[str] | None = None
) -> dict[str, str]:
    """Why each empty section of a rendered case is empty, keyed by the section.

    *inventory_sources*, the imports the asset software came from, are named when an
    upgrade's product runs on no asset: the loop then reads the deployment or asks the
    asset owner with those sources listed, never asks what they already answered (#1736).
    """
    reasons: dict[str, str] = {}
    if not rendered.get("exploitation"):
        reasons["exploitation"] = "no exploitation reading recorded on this case yet"
    if rendered.get("attack_path") is None:
        reasons["attack_path"] = "the attack-path reach of this case has not been read yet"
    if rendered.get("treatment_plan") is None:
        reasons["treatment_plan"] = "no treatment plan attached to this case"
    if rendered.get("threat_package") is None:
        reasons["threat_package"] = "no threat package drafted for this case"
    if not rendered.get("tickets"):
        reasons["tickets"] = "no ticket recorded on this case"
    if not rendered.get("validations"):
        reasons["validations"] = "no validation recorded on this case"
    if running_versions is None:
        if rendered.get("remediation_kind") != "upgrade":
            reasons["running_versions"] = "only an upgrade case has running versions"
        elif inventory_sources:
            reasons["running_versions"] = (
                "no asset of the inventory runs this product; inventory sources checked: "
                + ", ".join(inventory_sources)
                + ". Read the deployment (image tags, Helm values) before asking the asset owner,"
                " and list these sources in the ask"
            )
        else:
            reasons["running_versions"] = "no running version read for this upgrade"
    if not rendered.get("history"):
        reasons["history"] = "no event recorded on this case"
    if rendered.get("details_unavailable"):
        reasons["issues"] = "these source products did not answer for you: " + ", ".join(
            rendered["details_unavailable"]
        )
    return reasons


def build_ctem_read_tools(*, user_id: str, agent_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The three read tools, acting for *user_id*; nothing is written."""
    from .ctem_cases import _uuid, build_ctem_case_tools
    from .ctem_signals import build_ctem_signal_tools

    signal_defs, signal_exec = build_ctem_signal_tools(user_id=user_id)
    case_defs, case_exec = build_ctem_case_tools(user_id=user_id, agent_id=agent_id)
    scope = _uuid(user_id)

    async def _get(case_id: str = "") -> str:
        from app.core.database import async_session
        from app.models.user import User
        from app.services import ctem_case_versions as case_versions
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of
        from app.services.ctem_verticals import normalize_vertical
        from app.services.organizations import get_primary

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        if scope is None:
            return f"No case {case_id}."
        try:
            async with async_session() as db:
                primary = await get_primary(db)
                if primary is None:
                    return f"No case {case_id}."
                user = await db.get(User, scope)
                vertical = normalize_vertical(getattr(user, "ctem_vertical", None))
                found = await store.get_case(db, wanted, organization_id=primary.id)
                if found is None:
                    return f"No case {case_id}."
                case, rows, events = found
                # This case's members only (#2092): never a whole view build on the call during a drain.
                members = [f"{r.source}:{r.external_id}" for r in rows]
                view = await view_of.viewer_view(db, str(scope), vertical, members=members)
                rendered = view_of.hydrate(case, rows, events[:_HISTORY_PAGE], view)
                if rendered is None:
                    return f"No case {case_id}."
                from app.services import ctem_case_version_history as version_history

                # The list's asset-count rule on the single read (#1704): list and case read agree.
                version_history.with_asset_count(rendered, events)
                running = await case_versions.for_case(db, case)
                sources = (
                    await case_versions.inventory_sources(db, primary.id)
                    if running is None and rendered.get("remediation_kind") == "upgrade"
                    else None
                )
                from .ctem_signals import case_signal_scope

                return _dump(
                    {
                        **rendered,
                        "running_versions": running,
                        "opened_by": opened_by(events),
                        "history_total": len(events),
                        "empty_sections": empty_sections(rendered, running, sources),
                        "scope": await case_signal_scope(db, scope, view.signals, case_id=case.id),
                    }
                )
        except Exception:
            logger.warning("CTEM case read failed", exc_info=True)
            return "Error: the case could not be read right now. Say so; do not guess it."

    async def _assets(
        source: str = "",
        category: str = "",
        subcategory: str = "",
        criticality: str = "",
        internet_facing: str = "",
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> str:
        from app.core.database import async_session
        from app.services import organization_asset_store as asset_store

        picked = dict(zip(_ASSET_FILTERS, (source, category, subcategory, criticality, internet_facing), strict=True))
        filters = {
            name: [v.strip() for v in str(value or "").split(",") if v.strip()] for name, value in picked.items()
        }
        try:
            async with async_session() as db:
                org = await asset_store.ensure_built(db)
                if org is None:
                    return _dump(
                        {"total": 0, "assets": [], "by_source": {}, "by_category": {}, "note": "no inventory yet"}
                    )
                page = await asset_store.list_assets(
                    db, org, q=str(query or ""), offset=_int(offset), limit=_int(limit) or 50, filters=filters
                )
                facets = await asset_store.asset_facets(db, org, q=str(query or ""), filters=filters)
                await db.commit()
        except Exception:
            logger.warning("CTEM asset inventory read failed", exc_info=True)
            return "Error: the asset inventory could not be read right now. Say so; do not guess it."
        return _dump(asset_page(vars(page), facets))

    executor = CallableToolExecutor()
    executor.register("list_ctem_signals", signal_exec.handler("list_ctem_signals"))  # type: ignore[arg-type]
    executor.register("list_ctem_cases", case_exec.handler("list_ctem_cases"))  # type: ignore[arg-type]
    executor.register("get_ctem_case", _get)
    executor.register("list_ctem_assets", _assets)

    by_name = {d.name: d for d in (*signal_defs, *case_defs)}
    definitions = [
        dataclasses.replace(by_name["list_ctem_signals"], source=_SOURCE),
        dataclasses.replace(by_name["list_ctem_cases"], source=_SOURCE),
        dataclasses.replace(
            by_name["get_ctem_case"],
            description=(
                "Read one CTEM case: its fix, member issues (source product and id), assets, primary assets, "
                "latest history, who opened it, and why each empty section is empty."
            ),
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_ctem_assets",
            description=(
                "The live asset inventory: a page of assets and the counts by source and category for the same filters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Comma-separated."},
                    "category": {"type": "string"},
                    "subcategory": {"type": "string"},
                    "criticality": {"type": "string"},
                    "internet_facing": {"type": "string"},
                    "query": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
    ]
    return definitions, executor


def asset_page(page: dict[str, Any], facets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """What ``list_ctem_assets`` answers: the page, then the counts by source and by category of the same filters."""
    from app.services.ctem_signals import provider_name

    by_source = {
        str(f["value"]): {"name": provider_name(str(f["value"])), "assets": int(f["count"])}
        for f in facets.get("source", [])
    }
    by_category = {str(f["value"]): int(f["count"]) for f in facets.get("category", [])}
    items = page.get("items", [])
    total = int(page.get("total", 0))
    offset = int(page.get("offset", 0))
    return {
        "total": total,
        "offset": offset,
        "limit": int(page.get("limit", 0)),
        "next_offset": offset + len(items) if offset + len(items) < total else None,
        "by_source": by_source,
        "by_category": by_category,
        "assets": [{k: v for k, v in item.items() if k != "records"} for item in items],
    }


def _int(value: Any) -> int:
    try:
        return max(0, int(str(value or 0)))
    except ValueError:
        return 0


__all__ = [
    "CTEM_READ_GROUP",
    "CTEM_READ_TOOL_NAMES",
    "asset_page",
    "build_ctem_read_tools",
    "ctem_read_tools_for",
    "empty_sections",
    "offers_ctem_read_tools",
    "opened_by",
]
