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

"""The ``ctem`` built-in group (#1748): the CTEM signal and case tools on any agent.

The built-in CTEM agents carry the signal tools (``ctem_signals.py``) and the
case tools (``ctem_cases.py``) by their slug. This group offers the same
functions to any agent it is bound to, plus ``triage_ctem_signals`` (dismiss,
restore or re-check signals, as ``POST /ctem-signals/triage``). Every call acts
for the person the turn or run acts for, with the rights of the CTEM pages
(``services/ctem_access.py``): an administrator or a ``decide`` right reads and
acts on the organization's signals and cases, a ``view`` right reads them and
every write is refused, anyone else reads and acts on the signals they recorded
and the cases their own read shows. An agent that already carries a function
keeps its own, so the group never offers the same function twice.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

CTEM_GROUP = "ctem"
TRIAGE_TOOL = "triage_ctem_signals"
_SOURCE = f"builtin:{CTEM_GROUP}"


def _names() -> tuple[str, ...]:
    from .ctem_cases import CTEM_CASE_TOOL_NAMES
    from .ctem_signals import CTEM_SIGNAL_TOOL_NAMES

    return (*CTEM_SIGNAL_TOOL_NAMES, TRIAGE_TOOL, *CTEM_CASE_TOOL_NAMES)


def offers_ctem_tools(builtin_tool_ids: Any) -> bool:
    """Whether a turn or run wires the group: bound to the agent, XTM One mode only."""
    from app.core.config import settings

    if settings.is_copilot_like:
        return False
    return CTEM_GROUP in (builtin_tool_ids or ())


def _carried(agent: Any, builtin_tool_ids: Any) -> set[str]:
    """The functions the agent already holds by its slug or through the read group."""
    from .ctem_cases import CTEM_CASE_TOOL_NAMES, offers_ctem_case_tools
    from .ctem_read import CTEM_READ_TOOL_NAMES, offers_ctem_read_tools
    from .ctem_signals import CTEM_SIGNAL_TOOL_NAMES, offers_ctem_signal_tools

    slug, builtin = getattr(agent, "slug", None), bool(getattr(agent, "is_builtin", False))
    carried: set[str] = set()
    if offers_ctem_signal_tools(slug, is_builtin=builtin):
        carried.update(CTEM_SIGNAL_TOOL_NAMES)
    if offers_ctem_case_tools(slug, is_builtin=builtin):
        carried.update(CTEM_CASE_TOOL_NAMES)
    if offers_ctem_read_tools(builtin_tool_ids):
        carried.update(CTEM_READ_TOOL_NAMES)
    return carried


def ctem_tools_for(
    agent: Any, builtin_tool_ids: Any, *, user_id: str | None
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """The group's tools for one turn or run, ``None`` when not wired or nothing is left to add.

    ``ctem_read`` is wired before this group, so the reads it carries stay its own.
    """
    if not user_id or not offers_ctem_tools(builtin_tool_ids):
        return None
    carried = _carried(agent, builtin_tool_ids) & set(_names())
    if carried >= set(_names()):
        return None
    defs, executor = build_ctem_tools(user_id=user_id, agent_id=str(getattr(agent, "id", "") or ""))
    for name in carried:
        executor.unregister(name)
    return [d for d in defs if d.name not in carried], executor


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_ctem_tools(*, user_id: str, agent_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Every function of the group, acting for *user_id*; case writes are recorded with *agent_id* as actor."""
    from .ctem_cases import build_ctem_case_tools
    from .ctem_signals import build_ctem_signal_tools

    signal_defs, signal_exec = build_ctem_signal_tools(user_id=user_id)
    case_defs, case_exec = build_ctem_case_tools(user_id=user_id, agent_id=agent_id)
    try:
        scope: uuid.UUID | None = uuid.UUID(str(user_id))
    except ValueError:
        scope = None

    async def _triage(
        action: str = "", ids: list[str] | None = None, state: str = "", trigger_path: str = "", dry_run: bool = False
    ) -> str:
        """Dismiss, restore or re-check signals, as ``POST /ctem-signals/triage`` (#1505), for the acting person."""
        from app.core.database import async_session
        from app.services import ctem_access
        from app.services import ctem_scope_gate_store as scope_gate
        from app.services import ctem_signals as signals
        from app.services.organizations import get_primary

        if scope is None:
            return "No user: there are no signals to triage."
        if action not in scope_gate.TRIAGE_ACTIONS:
            return f"action must be one of: {', '.join(scope_gate.TRIAGE_ACTIONS)}."
        wanted: list[uuid.UUID] = []
        for raw in ids or []:
            try:
                wanted.append(uuid.UUID(str(raw)))
            except ValueError:
                return "ids must be the signal ids list_ctem_signals answers."
        if bool(wanted) == bool(state or trigger_path):
            return "Give either ids or a filter (state, trigger_path)."
        if len(wanted) > scope_gate.MAX_BULK:
            return f"At most {scope_gate.MAX_BULK} signals at once."
        try:
            async with async_session() as db:
                primary = await get_primary(db)
                if primary is None:
                    return "No organization is set up yet: there are no signals."
                reason = await ctem_access.refusal(db, scope, write=True)
                if reason is not None:
                    # Checked at call time for the person the turn or run acts for (#1748, #2441).
                    return f"Refused: {reason} Nothing was changed."
                from app.models.user import User

                user = await db.get(User, scope)
                if user is None:
                    return "No user: there are no signals to triage."
                # The reach of list_ctem_signals (reader_scope, agent reach): an administrator under
                # strict admin access for agent tools or the Platform Health reach triages their own.
                from .ctem_signals import reader_scope

                whole, _block = await reader_scope(db, scope, agent_reach=True)
                rows = await signals.select_signals(
                    db,
                    organization_id=primary.id,
                    recorded_by=None if whole else str(user.id),
                    ids=wanted or None,
                    state=state or None,
                    trigger_path=trigger_path or None,
                    limit=scope_gate.MAX_BULK,
                )
                actor = (getattr(user, "name", None) or getattr(user, "email", None) or str(user.id))[:200]
                if dry_run:
                    # Same selection and decisions, nothing written (#2341).
                    outcomes: list[dict[str, Any]] = []
                    counts = await scope_gate.triage(
                        db, rows, action=action, actor=actor, evaluator_id=user.id, outcomes=outcomes
                    )
                    await db.rollback()
                    return _dump({"dry_run": True, "matched": len(rows), "counts": counts, "signals": outcomes})
                counts = await scope_gate.triage(db, rows, action=action, actor=actor, evaluator_id=user.id)
                await db.commit()
                return _dump({"matched": len(rows), "counts": counts})
        except Exception:
            logger.warning("CTEM signal triage failed", exc_info=True)
            return "Error: the signals could not be triaged right now. Say so; do not guess the outcome."

    executor = CallableToolExecutor()
    for name in (d.name for d in signal_defs):
        executor.register(name, signal_exec.handler(name))  # type: ignore[arg-type]
    executor.register(TRIAGE_TOOL, _triage)
    for name in (d.name for d in case_defs):
        executor.register(name, case_exec.handler(name))  # type: ignore[arg-type]

    from app.models.ctem_signal import SIGNAL_STATES, TRIGGER_PATHS
    from app.services.ctem_scope_gate_store import TRIAGE_ACTIONS

    triage = ToolDefinition(
        name=TRIAGE_TOOL,
        description=(
            "Dismiss, restore or re-check CTEM signals by ids or filter; case-opening signals "
            "are skipped. dry_run: preview only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(TRIAGE_ACTIONS)},
                "ids": {"type": "array", "items": {"type": "string"}},
                "state": {"type": "string", "enum": list(SIGNAL_STATES)},
                "trigger_path": {"type": "string", "enum": list(TRIGGER_PATHS)},
                "dry_run": {"type": "boolean"},
            },
            "required": ["action"],
        },
        source=_SOURCE,
    )
    definitions = [
        *(dataclasses.replace(d, source=_SOURCE) for d in signal_defs),
        triage,
        *(dataclasses.replace(d, source=_SOURCE) for d in case_defs),
    ]
    return definitions, executor


__all__ = [
    "CTEM_GROUP",
    "TRIAGE_TOOL",
    "build_ctem_tools",
    "ctem_tools_for",
    "offers_ctem_tools",
]
