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

"""The CTEM attack surface passes read (#1498, #1778, #2443).

``list_ctem_attack_surface_passes`` serves ``GET /ctem-signals/attack-surface/passes``
to an agent: the latest passes of the outside lens, what each read covered and
the signals per path, for an administrator (the route's scope, re-read at each
call). The ``ctem`` group serves it; a built-in CTEM case agent carries it by
its slug. XTM One mode only.

One CTEM group (#2443): the former ``ctem_repair`` group is a [DEPRECATED] id of
``ctem`` (``RENAMED_BUILTIN_GROUPS``). Its two one-off data repairs (#1498,
#1547/#1552) left the agent surface; they stay administrator REST routes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

PASSES_TOOL = "list_ctem_attack_surface_passes"
CTEM_REPAIR_TOOL_NAMES: tuple[str, ...] = (PASSES_TOOL,)
_SOURCE = "builtin:ctem"
_NOT_ADMIN = (
    "Not run: these CTEM repairs are for an administrator, as their REST routes, and the "  # wording kept (#1778)
    "person this turn acts for is not one. Nothing was read or changed."
)


def offers_ctem_repair_tools(agent: Any, builtin_tool_ids: Any) -> bool:
    """Whether a turn or run wires the passes read by slug: a built-in CTEM case agent, XTM One mode only.

    An agent bound to ``ctem`` (or a former ``ctem_read`` / ``ctem_repair`` id) gets it from that group.
    """
    from app.core.config import settings
    from app.services.builtin_tool_aliases import current_group_id

    from .ctem import CTEM_GROUP
    from .ctem_cases import offers_ctem_case_tools

    if settings.is_copilot_like or CTEM_GROUP in {current_group_id(str(g)) for g in builtin_tool_ids or ()}:
        return False
    return offers_ctem_case_tools(getattr(agent, "slug", None), is_builtin=bool(getattr(agent, "is_builtin", False)))


def ctem_repair_tools_for(
    agent: Any, builtin_tool_ids: Any, *, user_id: str | None
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """The passes read for a built-in CTEM case agent, ``None`` when not wired."""
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
    """The attack surface passes read, acting for *user_id*: the route's administrator scope, re-read at each call."""
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
                answer: dict[str, Any] = {"passes": [surface.pass_to_dict(r) for r in rows], "count": len(rows)}
                if not rows:
                    answer["why_empty"] = await surface.why_no_pass(db, org_id)
                return _dump(answer)
        except Exception:
            logger.warning("CTEM attack surface pass read failed", exc_info=True)
            return "Error: the attack surface passes could not be read right now. Say so; do not guess them."

    executor = CallableToolExecutor()
    executor.register(PASSES_TOOL, _passes)
    definitions = [
        ToolDefinition(
            name=PASSES_TOOL,
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
    ]
    return definitions, executor


__all__ = [
    "CTEM_REPAIR_TOOL_NAMES",
    "PASSES_TOOL",
    "build_ctem_repair_tools",
    "ctem_repair_tools_for",
    "offers_ctem_repair_tools",
]
