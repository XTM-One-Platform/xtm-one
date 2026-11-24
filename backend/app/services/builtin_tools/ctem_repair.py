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

"""The ``ctem_repair`` built-in group (#1778): the CTEM administrator repairs from an agent.

Three administrator endpoints were REST only, so no agent could run them and a
deploy retest of a repair ended inconclusive:

- ``GET /ctem-signals/attack-surface/passes`` (#1498): the latest passes of the
  outside lens, what each read covered and the signals per path;
- ``POST /ctem-signals/attack-surface/repair-new-hosts`` (#1498): discard the
  path 7 new-host signals raised without a complete previous read;
- ``POST /ctem-cases/threat-packages/repair`` (#1547, #1552): correct the
  packages stored with a Grouping page whose Grouping is not live.

The tools call the same service functions as the routes and answer the same
shapes. They act for the person the turn or run acts for, with the routes'
scope: an administrator. A repair defaults to a dry run, which writes nothing
and answers the counts and the ids; ``dry_run`` false applies it. No gate, no
approval card: the agent reads the dry run and decides (Sam, 24 Nov 2026).

Carried by the built-in CTEM case agents (``CTEM_CASE_AGENT_SLUGS``) and by any
agent the group is bound to. XTM One mode only.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

CTEM_REPAIR_GROUP = "ctem_repair"
CTEM_REPAIR_TOOL_NAMES: tuple[str, ...] = (
    "list_ctem_attack_surface_passes",
    "repair_ctem_attack_surface_new_hosts",
    "repair_ctem_threat_packages",
)
_SOURCE = f"builtin:{CTEM_REPAIR_GROUP}"
_NOT_ADMIN = (
    "Not run: these CTEM repairs are for an administrator, as their REST routes, and the "
    "person this turn acts for is not one. Nothing was read or changed."
)


def offers_ctem_repair_tools(agent: Any, builtin_tool_ids: Any) -> bool:
    """Whether a turn or run wires the group: a built-in CTEM case agent or the group bound, XTM One mode only."""
    from app.core.config import settings

    from .ctem_cases import offers_ctem_case_tools

    if settings.is_copilot_like:
        return False
    if CTEM_REPAIR_GROUP in (builtin_tool_ids or ()):
        return True
    return offers_ctem_case_tools(getattr(agent, "slug", None), is_builtin=bool(getattr(agent, "is_builtin", False)))


def ctem_repair_tools_for(
    agent: Any, builtin_tool_ids: Any, *, user_id: str | None
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """The group's tools for one turn or run, ``None`` when not wired (no person acted for, or not offered)."""
    if not user_id or not offers_ctem_repair_tools(agent, builtin_tool_ids):
        return None
    return build_ctem_repair_tools(user_id=user_id)


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip()) if value else None
    except ValueError:
        return None


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_ctem_repair_tools(*, user_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The three tools, acting for *user_id*: the routes' administrator scope, re-read at each call."""
    scope = _uuid(user_id)

    async def _admin(db: Any) -> tuple[Any, uuid.UUID | None, str]:
        """The acting administrator and the organization, or the fact that stops the call."""
        from app.models.user import User
        from app.services.organizations import get_primary

        user = await db.get(User, scope) if scope else None
        if user is None or not getattr(user, "is_admin", False):
            return None, None, _NOT_ADMIN
        primary = await get_primary(db)
        if primary is None:
            return user, None, "No organization is set up yet: there is nothing to read or repair."
        return user, primary.id, ""

    async def _passes(limit: int = 10) -> str:
        """The latest attack surface passes, as GET /ctem-signals/attack-surface/passes."""
        from app.core.database import async_session
        from app.services import ctem_attack_surface as surface

        try:
            bounded = max(1, min(int(limit or 10), surface.MAX_PASSES))
        except (TypeError, ValueError):
            return f"limit is a whole number from 1 to {surface.MAX_PASSES}."
        try:
            async with async_session() as db:
                _user, org_id, fact = await _admin(db)
                if fact or org_id is None:
                    return fact
                rows = await surface.list_passes(db, org_id, limit=bounded)
                return _dump({"passes": [surface.pass_to_dict(r) for r in rows], "count": len(rows)})
        except Exception:
            logger.warning("CTEM attack surface pass read failed", exc_info=True)
            return "Error: the attack surface passes could not be read right now. Say so; do not guess them."

    async def _repair_hosts(dry_run: bool = True) -> str:
        """POST /ctem-signals/attack-surface/repair-new-hosts: a dry run (default) writes nothing."""
        from app.core.database import async_session
        from app.services import ctem_attack_surface as surface

        try:
            async with async_session() as db:
                _user, org_id, fact = await _admin(db)
                if fact or org_id is None:
                    return fact
                result = await surface.repair_partial_read_new_hosts(db, org_id, dry_run=bool(dry_run))
                await db.commit()
                return _dump({**result, "dry_run": bool(dry_run)})
        except Exception:
            logger.warning("CTEM attack surface repair failed", exc_info=True)
            return "Error: the new-host repair could not run right now. Say so; nothing was changed."

    async def _repair_packages(dry_run: bool = True) -> str:
        """POST /ctem-cases/threat-packages/repair: a dry run (default) writes nothing."""
        from app.core.database import async_session
        from app.services import ctem_threat_package_live as package_live

        try:
            async with async_session() as db:
                user, org_id, fact = await _admin(db)
                if fact or org_id is None:
                    return fact
                result = await package_live.repair_stored_packages(db, org_id, str(user.id), dry_run=bool(dry_run))
                await db.commit()
                return _dump({**result, "dry_run": bool(dry_run)})
        except Exception:
            logger.warning("CTEM threat package repair failed", exc_info=True)
            return "Error: the threat package repair could not run right now. Say so; nothing was changed."

    executor = CallableToolExecutor()
    executor.register("list_ctem_attack_surface_passes", _passes)
    executor.register("repair_ctem_attack_surface_new_hosts", _repair_hosts)
    executor.register("repair_ctem_threat_packages", _repair_packages)
    dry_run = {"type": "boolean", "description": "true (default): answer what would change, write nothing."}
    definitions = [
        ToolDefinition(
            name="list_ctem_attack_surface_passes",
            description=(
                "Administrator: the latest CTEM attack surface passes, newest first: providers, what each read "
                "covered per domain or range (pages, records, truncated), hosts observed, signals per path."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="repair_ctem_attack_surface_new_hosts",
            description=(
                "Administrator: discard the path 7 new-host signals raised without a complete previous read; "
                "hosts stay in the baseline. Answers counts and ids."
            ),
            input_schema={"type": "object", "properties": {"dry_run": dry_run}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="repair_ctem_threat_packages",
            description=(
                "Administrator: re-read each case's newest threat package stored with a Grouping page and "
                "correct the ones whose Grouping is not live. Answers counts and case ids."
            ),
            input_schema={"type": "object", "properties": {"dry_run": dry_run}, "required": []},
            source=_SOURCE,
        ),
    ]
    return definitions, executor


__all__ = [
    "CTEM_REPAIR_GROUP",
    "CTEM_REPAIR_TOOL_NAMES",
    "build_ctem_repair_tools",
    "ctem_repair_tools_for",
    "offers_ctem_repair_tools",
]
