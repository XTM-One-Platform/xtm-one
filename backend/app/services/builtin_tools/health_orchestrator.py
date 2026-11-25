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

"""The Platform Health orchestrator's tools (#8261): signals, cycles, digests, requests.

Group ``health_orchestrator``, agent-keyed, offered while Platform Health
is available. One rule on every path (``services/health/cycle_access.py``), decided at
each call from the database rows:

* as **Platform Health** (``identity.current_health_principal()``: a scheduled run, an
  administrator's chat) - every cycle, and the signals of the supervision scope;
* as **a person** (a non-administrator chatting with the orchestrator, the
  platform MCP server) - the cycles and signals of the targets that person
  manages, and requests on those targets only.

Names, digests and run reports are other people's words: returned inside
untrusted-content delimiters, never as instructions.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from sqlalchemy import func, select

from app.api.v1.helpers import agent_tool_admin_bypass
from app.services.builtin_tool_aliases import RENAMED_BUILTIN_FUNCTIONS
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

GROUP_ID: Final = "health_orchestrator"
TOOL_NAMES: Final = (
    "list_health_signals",
    "list_health_cycles",
    "get_health_cycle",
    "request_health_cycle",
    "get_platform_health",
)
_MAX_LIMIT: Final = 50
_GENERIC_ERROR: Final = "Platform Health could not answer this right now."
_NOT_FOUND: Final = "No such target you may manage."


def _former_names(current: str) -> tuple[str, ...]:
    """The [DEPRECATED] ``*_concierge_*`` names of *current*, declared as its aliases."""
    return tuple(former for former, name in RENAMED_BUILTIN_FUNCTIONS.items() if name == current)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _data(payload: Any, label: str) -> str:
    from app.services.content_safety import wrap_untrusted

    return wrap_untrusted(json.dumps(payload, default=str, ensure_ascii=False), label)


async def _caller(db, user_id: str | None):
    """``(principal, user)``: the Platform Health principal when the work acts as it, else the person."""
    from app.models.user import User
    from app.services.health.identity import current_health_principal

    principal = current_health_principal()
    user = None
    uid = _uuid(user_id)
    if uid is not None:
        user = await db.get(User, uid)
        if user is not None and not user.is_active:
            user = None
    return principal, user


async def _find_target(db, principal, user, group_ids, target_type: str, target: str):
    """The target named by id or by exact name, as the caller may reach it; ``None`` when not exactly one."""
    from app.models.agent import Agent
    from app.models.flow import Flow
    from app.services.health.consent import in_supervision_scope
    from app.services.health.cycle_access import resolve_target

    model = Flow if target_type == "flow" else Agent
    target_id = _uuid(target)
    if target_id is None:
        name = " ".join(str(target or "").split())[:200]
        if not name:
            return None
        ids = (
            (await db.execute(select(model.id).where(func.lower(model.name) == name.lower()).limit(3))).scalars().all()
        )
        if len(ids) != 1:
            return None
        target_id = ids[0]
    if principal is not None:
        row = await db.get(model, target_id)
        if row is None or not await in_supervision_scope(db, target_type, target_id):
            return None
        return target_type, target_id, row.name
    if user is None:
        return None
    found = await resolve_target(db, user, group_ids, target_type, target_id, is_admin=agent_tool_admin_bypass(user))
    if found is None or not found.can_manage:
        return None
    return target_type, target_id, found.name


def build_health_orchestrator_tools(
    user_id: str | None, *, allow_requests: bool = True
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The group's tools, acting for *user_id* unless the work acts as Platform Health.

    *allow_requests* False (an assignment run) leaves ``request_health_cycle``
    out: a cycle's own run never starts another cycle, so a run cannot fan out.
    """

    async def list_health_signals(target_type: str = "", target: str = "", limit: int = 20) -> str:
        from app.core.database import async_session
        from app.services.health.signals import list_signal_states

        try:
            async with async_session() as db:
                principal, user = await _caller(db, user_id)
                group_ids = await _group_ids(db, user)
                resolved = None
                if target:
                    resolved = await _find_target(db, principal, user, group_ids, target_type or "agent", target)
                    if resolved is None:
                        return _NOT_FOUND
                elif principal is None and not (user is not None and agent_tool_admin_bypass(user)):
                    return "Name the agent or flow whose signals to list."
                states = await list_signal_states(
                    db,
                    target_type=resolved[0] if resolved else None,
                    target_id=resolved[1] if resolved else None,
                    limit=max(1, min(int(limit or 20), _MAX_LIMIT)),
                )
                rows = [
                    {
                        "kind": s.kind,
                        "target_type": s.target_type,
                        "target_id": str(s.target_id),
                        "reading": s.last_value,
                        "fired_at": s.fired_at,
                        "cycle_id": s.cycle_id,
                        "not_started_because": s.refusal,
                    }
                    for s in states
                ]
        except Exception:
            logger.exception("list_health_signals failed")
            return _GENERIC_ERROR
        if not rows:
            return "No signal is crossed."
        return "Crossed Platform Health signals:\n" + _data(rows, "Platform Health signals")

    async def list_health_cycles(target_type: str = "", target: str = "", status: str = "", limit: int = 20) -> str:
        from app.core.database import async_session
        from app.models.health import HealthCycle
        from app.models.health_cycle import HealthCycleRun

        try:
            async with async_session() as db:
                principal, user = await _caller(db, user_id)
                group_ids = await _group_ids(db, user)
                query = select(HealthCycle, HealthCycleRun).outerjoin(
                    HealthCycleRun, HealthCycleRun.cycle_id == HealthCycle.id
                )
                if target:
                    resolved = await _find_target(db, principal, user, group_ids, target_type or "agent", target)
                    if resolved is None:
                        return _NOT_FOUND
                    query = query.where(HealthCycle.target_type == resolved[0], HealthCycle.target_id == resolved[1])
                elif principal is None and not (user is not None and agent_tool_admin_bypass(user)):
                    return "Name the agent or flow whose Platform Health cycles to list."
                if status:
                    query = query.where(HealthCycle.status == str(status)[:16])
                rows = (
                    await db.execute(
                        query.order_by(HealthCycle.opened_at.desc()).limit(max(1, min(int(limit or 20), _MAX_LIMIT)))
                    )
                ).all()
                items = [_cycle_item(cycle, record, full=False) for cycle, record in rows]
        except Exception:
            logger.exception("list_health_cycles failed")
            return _GENERIC_ERROR
        if not items:
            return "No Platform Health cycle matches."
        return "Platform Health cycles, newest first (statuses as recorded):\n" + _data(items, "Platform Health cycles")

    async def get_health_cycle(cycle_id: str = "") -> str:
        from app.core.database import async_session
        from app.models.health import HealthCycle
        from app.models.health_cycle import HealthCycleRun
        from app.services.health.cycle_access import may_read_cycle

        cid = _uuid(cycle_id)
        if cid is None:
            return "The `cycle_id` argument must be a cycle id from list_health_cycles."
        try:
            async with async_session() as db:
                principal, user = await _caller(db, user_id)
                group_ids = await _group_ids(db, user)
                cycle = await db.get(HealthCycle, cid)
                readable = cycle is not None and (
                    principal is not None
                    or (
                        user is not None
                        and await may_read_cycle(
                            db,
                            user,
                            group_ids,
                            cycle.target_type,
                            cycle.target_id,
                            is_admin=agent_tool_admin_bypass(user),
                        )
                    )
                )
                if not readable:
                    return "No such cycle."
                item = _cycle_item(cycle, await db.get(HealthCycleRun, cid), full=True)
        except Exception:
            logger.exception("get_health_cycle failed")
            return _GENERIC_ERROR
        return "Platform Health cycle and its digest (figures and statuses as recorded):\n" + _data(
            item, "Platform Health cycle"
        )

    async def request_health_cycle(target_type: str = "agent", target: str = "") -> str:
        from app.core.database import async_session
        from app.services.health.cycles import start_cycle
        from app.services.health.naming import DEFAULT_NAME

        if target_type not in ("agent", "flow"):
            return "`target_type` is `agent` or `flow`."
        try:
            async with async_session() as db:
                principal, user = await _caller(db, user_id)
                group_ids = await _group_ids(db, user)
                resolved = await _find_target(db, principal, user, group_ids, target_type, target)
                if resolved is None:
                    return _NOT_FOUND
                requester = principal.on_behalf_of_admin_id if principal is not None else (user.id if user else None)
                result = await start_cycle(
                    db,
                    key="investigation",
                    trigger="on_demand",
                    target_type=resolved[0],
                    target_id=resolved[1],
                    requested_by_user_id=requester,
                )
        except Exception:
            logger.exception("request_health_cycle failed")
            return _GENERIC_ERROR
        if not result.started:
            # The platform's own name for it: a model reads no administrator text here.
            return f"Not started: {result.message(DEFAULT_NAME)}"
        return f"Started Platform Health cycle {result.cycle_id} on the {target_type} {resolved[1]}."

    async def get_platform_health(module: str = "", limit: int = 20) -> str:
        """The Platform Health page as the person reads it: module cards, open findings, alert settings (#2199).

        The page's own reads (``api/v1/platform_health_findings.py``), so the role scope is the page's:
        every module for an administrator, the generic modules and the agents they manage otherwise.
        """
        from app.api.v1.platform_health_findings import health_overview, list_health_findings
        from app.core.database import async_session
        from app.services.health.settings import load_health_settings

        try:
            async with async_session() as db:
                _principal, user = await _caller(db, user_id)
                if user is None:
                    return "The Platform Health page is read for a signed-in person."
                overview = await health_overview(user=user, db=db)
                findings = await list_health_findings(
                    module=[str(module)[:64]] if module else None,
                    severity=None,
                    category=None,
                    finding_status=None,
                    agentic=None,
                    limit=max(1, min(int(limit or 20), _MAX_LIMIT)),
                    offset=0,
                    user=user,
                    db=db,
                )
                page: dict[str, Any] = {
                    "scope": overview.scope,
                    "overall": overview.overall,
                    "modules": [card.model_dump(mode="json") for card in overview.modules],
                    "open_findings": findings.total,
                    "findings": [finding.model_dump(mode="json") for finding in findings.items],
                }
                if user.is_admin:
                    alerts = (await load_health_settings(db)).alert_emails
                    page["alert_emails"] = {
                        "enabled": alerts.enabled,
                        "recipients": len(alerts.recipients),
                        "min_severity": alerts.min_severity,
                        "immediate": alerts.immediate,
                        "digest": alerts.digest,
                        "alert_after_minutes": alerts.alert_after_minutes,
                    }
        except Exception:
            logger.exception("get_platform_health failed")
            return _GENERIC_ERROR
        return "The Platform Health page as you may read it (scores and findings as recorded):\n" + _data(
            page, "Platform Health page"
        )

    executor = CallableToolExecutor()
    functions = [list_health_signals, list_health_cycles, get_health_cycle, get_platform_health]
    if allow_requests:
        functions.append(request_health_cycle)
    for fn in functions:
        executor.register(fn.__name__, fn)
        # The [DEPRECATED] ``*_concierge_*`` names run the same handlers for a
        # caller holding this executor directly; no definition is offered under them.
        for former in _former_names(fn.__name__):
            executor.register(former, fn)
    target_props = {
        "target_type": {
            "type": "string",
            "enum": ["agent", "flow"],
            "description": "What the target is (default agent).",
        },
        "target": {"type": "string", "description": "The target's id or exact name."},
    }
    limit_prop = {"limit": {"type": "integer", "description": f"At most this many, 1 to {_MAX_LIMIT}.", "default": 20}}
    definitions = [
        ToolDefinition(
            name="list_health_signals",
            description="List the Platform Health signals currently crossed (feedback drop, failure spike, cost drift, model change), for one target or all you may read. Read-only.",
            input_schema={"type": "object", "properties": {**target_props, **limit_prop}},
            aliases=_former_names("list_health_signals"),
        ),
        ToolDefinition(
            name="list_health_cycles",
            description="List Platform Health cycles with their status and digest summary, for one target or all you may read. Read-only.",
            input_schema={
                "type": "object",
                "properties": {
                    **target_props,
                    "status": {
                        "type": "string",
                        "description": "open, completed, failed, budget_reached or cancelled.",
                    },
                    **limit_prop,
                },
            },
            aliases=_former_names("list_health_cycles"),
        ),
        ToolDefinition(
            name="get_health_cycle",
            description="Read one Platform Health cycle and its digest: consolidations, findings, experiments, proposals, budget spent, what was skipped and why. Read-only.",
            input_schema={
                "type": "object",
                "properties": {"cycle_id": {"type": "string", "description": "From list_health_cycles."}},
                "required": ["cycle_id"],
            },
            aliases=_former_names("get_health_cycle"),
        ),
        ToolDefinition(
            name="request_health_cycle",
            description="Ask Platform Health to investigate and improve one agent or flow you may manage. Starts at most one cycle per target at a time.",
            input_schema={"type": "object", "properties": target_props, "required": ["target"]},
            aliases=_former_names("request_health_cycle"),
        ),
        ToolDefinition(
            name="get_platform_health",
            description="Read the Platform Health page as you may see it: module scores, open findings, alert email settings. Read-only.",
            input_schema={
                "type": "object",
                "properties": {
                    "module": {"type": "string", "description": "One module's findings only."},
                    **limit_prop,
                },
            },
        ),
    ]
    if not allow_requests:
        definitions = [d for d in definitions if d.name != "request_health_cycle"]
    return definitions, executor


async def _group_ids(db, user) -> list[str]:
    from app.api.v1.helpers import get_user_group_ids

    return await get_user_group_ids(db, user.id) if user is not None else []


def _cycle_item(cycle, record, *, full: bool) -> dict[str, Any]:
    digest = (record.digest if record else None) or {}
    item: dict[str, Any] = {
        "cycle_id": str(cycle.id),
        "specialist": cycle.specialist,
        "trigger": cycle.trigger,
        "target_type": cycle.target_type,
        "target_id": str(cycle.target_id) if cycle.target_id else None,
        "status": digest.get("status", cycle.status),
        "opened_at": cycle.opened_at,
        "closed_at": cycle.closed_at,
        "spent_tokens": int(cycle.spent_tokens),
        "spent_cost_usd": str(cycle.spent_cost_usd),
    }
    if full:
        item["digest"] = digest or None
    else:
        item["skipped"] = digest.get("skipped")
    return item
