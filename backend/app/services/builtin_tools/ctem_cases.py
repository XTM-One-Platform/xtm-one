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

"""CTEM Cases tools (#11545, slice S2; #891): list, read, create and update cases from an agent.

Offered to the built-in CTEM agents that work a case (``dev-docs/ctem-cases.md``
section 8): the two loop orchestrators, the Remediation Coordinator and the
Mobilization Analyst, in XTM One mode, as the person
the turn or run acts for. The rules are ``services/ctem_cases.py``'s, the
writes ``services/ctem_cases_store.py``'s: an agent never accepts risk,
resolves only with evidence, and every write is a case event with the agent
as actor. The answers are the API's own shapes (``api/v1/ctem_cases.py``).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import CASE_STATES
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

CTEM_CASE_TOOL_NAMES: tuple[str, ...] = (
    "list_ctem_cases",
    "get_ctem_case",
    "create_ctem_case",
    "update_ctem_case",
    "record_ctem_case_validation",
    "record_ctem_case_ticket",
)
_SOURCE = "builtin:ctem_cases"
#: The built-in agents that carry the tools (design section 8): the two loop
#: orchestrators run a case from ``ctem_case.opened`` to its proof (#891).
CTEM_CASE_AGENT_SLUGS: frozenset[str] = frozenset(
    {
        "ctem-vuln-prioritization-orchestrator",
        "ctem-threat-response-orchestrator",
        "ctem-remediation-coordinator",
        "ctem-mobilization-analyst",
    }
)
#: Every case state, read from the model so a new state reaches the counts and the enum (#857).
_STATES: tuple[str, ...] = CASE_STATES
#: History events one ``get_ctem_case`` answer carries.
_HISTORY_PAGE = 20


def offers_ctem_case_tools(agent_slug: str | None, *, is_builtin: bool) -> bool:
    """Whether a turn or run of this agent is offered the case tools (XTM One mode only)."""
    from app.core.config import settings

    if settings.is_copilot_like:
        return False
    return bool(is_builtin and (agent_slug or "") in CTEM_CASE_AGENT_SLUGS)


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value).strip()) if value else None
    except ValueError:
        return None


def _when(value: object) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def build_ctem_case_tools(*, user_id: str, agent_id: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The four case tools, acting for *user_id*; every write is recorded with *agent_id* as actor.

    They render every case through the acting user's own source read (#11613,
    ``services/ctem_cases_view.py``): a case with no issue that user sees does
    not exist for the agent, an issue they cannot see is a Restricted row, and
    a new owner must see the case too. Without a user there is no case.
    """
    scope = _uuid(user_id)

    async def _context(db: AsyncSession) -> tuple[uuid.UUID | None, str]:
        from app.services.ctem_verticals import normalize_vertical
        from app.services.organizations import get_primary

        if scope is None:
            return None, ""
        primary = await get_primary(db)
        if primary is None:
            return None, ""
        from app.models.user import User

        user = await db.get(User, scope)
        return primary.id, normalize_vertical(getattr(user, "ctem_vertical", None))

    async def _shown(db: AsyncSession, case_id: uuid.UUID, org_id: uuid.UUID, vertical: str) -> Any:
        """(stored case, members, rendering) for the acting user, ``None`` when they see none of it."""
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        found = await store.get_case(db, case_id, organization_id=org_id)
        if found is None:
            return None
        case, rows, events = found
        rendered = view_of.hydrate(
            case, rows, events[:_HISTORY_PAGE], await view_of.viewer_view(db, str(scope), vertical)
        )
        return None if rendered is None else (case, rows, rendered)

    async def _list(
        state: str = "", priority: str = "", owner_user_id: str = "", limit: int = 20, offset: int = 0
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        if state and state not in _STATES:
            return f"Unknown state {state!r}: one of {', '.join(_STATES)}."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return "No organization is set up yet: there are no cases."
                cases = await store.list_cases(db, organization_id=org_id, vertical=vertical)
                owner = _uuid(owner_user_id)
                rows, counts = view_of.list_for_viewer(
                    cases,
                    await view_of.viewer_view(db, str(scope), vertical),
                    state=state or None,
                    priority=priority or None,
                    owner_user_id=str(owner) if owner else None,
                    limit=max(1, min(int(limit or 20), 50)),
                    offset=max(0, int(offset or 0)),
                )
                return _dump({"vertical": vertical, "counts": {s: counts.get(s, 0) for s in _STATES}, "cases": rows})
        except Exception:
            logger.warning("CTEM case list failed", exc_info=True)
            return "Error: the cases could not be read right now. Say so; do not guess them."

    async def _get(case_id: str = "") -> str:
        from app.core.database import async_session

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                shown = await _shown(db, wanted, org_id, vertical) if org_id and scope else None
                if shown is None:
                    return f"No case {case_id}."
                return _dump(shown[2])
        except Exception:
            logger.warning("CTEM case read failed", exc_info=True)
            return "Error: the case could not be read right now. Say so; do not guess it."

    async def _create(title: str = "", issue_ids: list[str] | None = None, reason: str = "") -> str:
        from app.core.database import async_session
        from app.services import ctem_cases as rules
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                if org_id is None or scope is None:
                    return "No organization is set up yet: no case can be created."
                view = await view_of.viewer_view(db, str(scope), vertical)
                case = await store.create_manual_case(
                    db,
                    organization_id=org_id,
                    vertical=vertical,
                    title=title,
                    member_ids=[str(i) for i in (issue_ids or [])],
                    visible=set(view.open_ids),
                    actor=agent_id,
                    reason=(reason or "").strip() or None,
                )
                await db.commit()
                shown = await _shown(db, case.id, org_id, vertical)
                return _dump({"created": shown[2] if shown else {"id": str(case.id), "title": case.title}})
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}."
        except Exception:
            logger.warning("CTEM case create failed", exc_info=True)
            return "Error: the case could not be created right now. Say so; nothing was changed."

    async def _update(
        case_id: str = "",
        state: str = "",
        owner_user_id: str = "",
        due_at: str = "",
        reason: str = "",
        evidence: str = "",
        note: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_cases as rules
        from app.services import ctem_cases_store as store
        from app.services import ctem_cases_view as view_of

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        if state and state not in _STATES:
            return f"Unknown state {state!r}: one of {', '.join(_STATES)}."
        if owner_user_id and _uuid(owner_user_id) is None:
            return "owner_user_id must be a user id."
        try:
            due = _when(due_at)
        except ValueError:
            return "due_at must be an ISO-8601 date and time."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                shown = await _shown(db, wanted, org_id, vertical) if org_id and scope else None
                if shown is None:
                    return f"No case {case_id}."
                case, rows, _ = shown
                owner = _uuid(owner_user_id)
                if owner and owner != case.owner_user_id:
                    owner_view = await view_of.viewer_view(db, str(owner), vertical)
                    if not view_of.owner_sees(rows, owner_view):
                        raise rules.CaseRuleError(
                            "the owner must be able to see this case's issues in the source products"
                        )
                await store.update_case(
                    db,
                    case,
                    actor=agent_id,
                    actor_kind="agent",
                    state=state or None,
                    owner_user_id=_uuid(owner_user_id),
                    due_at=due,
                    reason=reason or None,
                    evidence=evidence or None,
                    note=note or None,
                )
                await db.commit()
                refreshed = await _shown(db, wanted, org_id, vertical)
                return _dump({"updated": refreshed[2] if refreshed else {"id": str(wanted)}})
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case update failed", exc_info=True)
            return "Error: the case could not be changed right now. Say so; nothing was changed."

    async def _validation(
        case_id: str,
        provider: str = "",
        ref: str = "",
        result: str = "",
        kind: str = "retest",
        mitigation_only: bool = False,
        note: str = "",
    ) -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        pending = case_loop.PendingEvents()
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                shown = await _shown(db, wanted, org_id, vertical) if org_id and scope else None
                if shown is None:
                    return f"No case {case_id}."
                await case_loop.record_validation(
                    db,
                    shown[0],
                    provider=provider,
                    kind=kind,
                    ref=ref,
                    result=result,
                    mitigation_only=bool(mitigation_only),
                    note=note or None,
                    actor=agent_id,
                    events=pending,
                )
                await db.commit()
                await case_loop.publish_pending(pending)
                refreshed = await _shown(db, wanted, org_id, vertical)
                return _dump({"updated": refreshed[2] if refreshed else {"id": str(wanted)}})
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case validation failed", exc_info=True)
            return "Error: the validation could not be recorded right now. Say so; nothing was changed."

    async def _ticket(case_id: str, integration: str = "", key: str = "", url: str = "", status: str = "open") -> str:
        from app.core.database import async_session
        from app.services import ctem_case_loop as case_loop
        from app.services import ctem_cases as rules

        wanted = _uuid(case_id)
        if wanted is None:
            return "case_id must be the id list_ctem_cases answers."
        try:
            async with async_session() as db:
                org_id, vertical = await _context(db)
                shown = await _shown(db, wanted, org_id, vertical) if org_id and scope else None
                if shown is None:
                    return f"No case {case_id}."
                case_loop.record_ticket(
                    db, shown[0], integration=integration, key=key, url=url or None, status=status, actor=agent_id
                )
                await db.commit()
                refreshed = await _shown(db, wanted, org_id, vertical)
                return _dump({"updated": refreshed[2] if refreshed else {"id": str(wanted)}})
        except rules.CaseRuleError as exc:
            return f"Refused: {exc}. Nothing was changed."
        except Exception:
            logger.warning("CTEM case ticket failed", exc_info=True)
            return "Error: the ticket reference could not be recorded right now. Say so; nothing was changed."

    executor = CallableToolExecutor()
    executor.register("list_ctem_cases", _list)
    executor.register("get_ctem_case", _get)
    executor.register("create_ctem_case", _create)
    executor.register("update_ctem_case", _update)
    executor.register("record_ctem_case_validation", _validation)
    executor.register("record_ctem_case_ticket", _ticket)
    state_enum = {"type": "string", "enum": list(_STATES)}
    definitions = [
        ToolDefinition(
            name="list_ctem_cases",
            description=(
                "List the organization's CTEM cases (open exposures grouped by the fix that closes them), "
                "highest priority first, with the counts by state. Filters: state, priority, owner."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "state": state_enum,
                    "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "owner_user_id": {"type": "string", "description": "Only the cases this user owns."},
                    "limit": {"type": "integer", "description": "Cases a page (default 20, max 50)."},
                    "offset": {"type": "integer"},
                },
                "required": [],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_ctem_case",
            description=(
                "Read one CTEM case: its fix, its member issues with their source product and id, "
                "assets, crown jewels and its latest history."
            ),
            input_schema={
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_ctem_case",
            description=(
                "Split open issues out of their cases into a new manual case. issue_ids are the "
                "'source:external_id' of issues get_ctem_case lists as open."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "issue_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string", "description": "Why they are handled apart."},
                },
                "required": ["title", "issue_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_ctem_case",
            description=(
                "Change a CTEM case: owner, due date, a note, or the state with its reason. in_progress needs "
                "an owner; resolved needs evidence (a ticket, a change or a validation run); reopening needs a "
                "reason; accepted_risk is refused to an agent (a person accepts risk)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "state": state_enum,
                    "owner_user_id": {"type": "string"},
                    "due_at": {"type": "string", "description": "ISO-8601; without an offset it is read as UTC."},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="record_ctem_case_validation",
            description=(
                "Write a validation or re-test result on a CTEM case: the OpenAEV simulation, scenario or "
                "atomic test id and its result. passed or not_exposed resolves the case (mitigation_only: "
                "mitigated, watching, the re-test stays on); failed reopens a resolved case; pending only records."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "provider": {"type": "string", "description": "Where the test ran, e.g. openaev."},
                    "ref": {"type": "string", "description": "The run, simulation or scenario id."},
                    "result": {"type": "string", "enum": ["passed", "not_exposed", "failed", "pending"]},
                    "kind": {"type": "string", "description": "validation or retest (default retest)."},
                    "mitigation_only": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["case_id", "provider", "ref", "result"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="record_ctem_case_ticket",
            description=(
                "Write or update the reference of a remediation ticket on a CTEM case (integration, key, "
                "url, status). One entry per ticket; never changes the case state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "integration": {"type": "string", "description": "e.g. jira or servicenow."},
                    "key": {"type": "string", "description": "The ticket key or number."},
                    "url": {"type": "string"},
                    "status": {"type": "string", "enum": ["open", "in_progress", "done", "cancelled"]},
                },
                "required": ["case_id", "integration", "key"],
            },
            source=_SOURCE,
        ),
    ]
    return definitions, executor
