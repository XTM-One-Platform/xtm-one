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

"""Who may read a Platform Health cycle, ask for one, and follow a proposal's evidence (#8261).

One rule for every surface - REST (``api/v1/platform_health_cycles.py``), the
orchestrator's tools and the platform MCP server - checked on the database
rows at every call:

* **A person** reads the cycles and signals of a target they may manage
  (``helpers.can_manage``: company-managed targets for admins, a personal one
  for its owner, an admin or a ``config`` grantee); administrators read every
  cycle. Asking for an improvement needs the same right on the target; a
  target the person cannot see is "not found", never "forbidden".
* **Platform Health** (``identity.current_health_principal()``) reads its own cycles
  and signals within its supervision scope.
* **Evidence** is linked only where the viewer may open it: a trace or a
  rating where they may read the agent's chats (``may_read_agent_traces``), a
  run where its agent's chats are theirs to read, a flow run where they may see
  the flow, an evaluation run for an administrator while the evaluation pages
  are on. Anything else is shown as hidden, without its id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

TargetKind = Literal["agent", "flow", "assignment"]

TARGET_NOT_FOUND: Final = "Target not found"


@dataclass(frozen=True)
class Target:
    target_type: str
    target_id: uuid.UUID
    name: str
    #: The agent behind the target (the agent itself, or the assignment's), ``None`` for a flow.
    agent_id: uuid.UUID | None
    can_manage: bool


async def resolve_target(
    db: AsyncSession,
    user: Any,
    user_group_ids: list[str],
    target_type: str,
    target_id: uuid.UUID,
    *,
    is_admin: bool | None = None,
) -> Target | None:
    """The target as *user* sees it; ``None`` when it does not exist or they cannot see it.

    *is_admin* is whether the admin bypass applies: the role on REST, an agent
    tool's ``agent_tool_admin_bypass()`` (strict admin access keeps it to the
    admin's three tiers).
    """
    admin = user.is_admin if is_admin is None else is_admin
    from app.api.v1.helpers import can_manage, user_visible_filter
    from app.models.agent import Agent
    from app.models.assignment import Assignment
    from app.models.flow import Flow

    if target_type == "flow":
        query = select(Flow).where(Flow.id == target_id)
        if not admin:
            query = query.where(user_visible_filter(Flow, user.id, user_group_ids))
        flow = (await db.execute(query)).scalar_one_or_none()
        if flow is None:
            return None
        return Target("flow", flow.id, flow.name, None, can_manage(user, flow, user_group_ids))
    if target_type == "assignment":
        assignment = (await db.execute(select(Assignment).where(Assignment.id == target_id))).scalar_one_or_none()
        if assignment is None:
            return None
        agent_id = assignment.agent_id
        name = assignment.name
    elif target_type == "agent":
        agent_id, name = target_id, None
    else:
        return None
    query = select(Agent).where(Agent.id == agent_id)
    if not admin:
        query = query.where(user_visible_filter(Agent, user.id, user_group_ids))
    agent = (await db.execute(query)).scalar_one_or_none()
    if agent is None:
        return None
    return Target(target_type, target_id, name or agent.name, agent.id, can_manage(user, agent, user_group_ids))


async def may_read_cycle(
    db: AsyncSession, user: Any, user_group_ids: list[str], target_type, target_id, *, is_admin: bool | None = None
) -> bool:
    """Whether *user* may read a cycle on this target (a fleet-wide cycle: administrators only).

    *is_admin*: as :func:`resolve_target`.
    """
    admin = user.is_admin if is_admin is None else is_admin
    if admin:
        return True
    if target_id is None:
        return False
    target = await resolve_target(db, user, user_group_ids, target_type, target_id, is_admin=admin)
    return target is not None and target.can_manage


async def may_read_cycle_narrative(
    db: AsyncSession, user: Any, user_group_ids: list[str], target_type, target_id
) -> bool:
    """Whether *user* may read a cycle's digest narrative, free text that may quote conversations.

    The evidence rule: an agent's or assignment's narrative needs its agent's chats
    (``may_read_agent_traces``), a flow's needs the flow; administrators read all.
    """
    if user.is_admin:
        return True
    if target_id is None:
        return False
    target = await resolve_target(db, user, user_group_ids, target_type, target_id)
    if target is None:
        return False
    if target.agent_id is None:
        return target.can_manage
    from app.models.agent import Agent
    from app.services.builtin_tools.traces import may_read_agent_traces

    agent = await db.get(Agent, target.agent_id)
    return agent is not None and await may_read_agent_traces(db, user, agent, user_group_ids)


# ── Evidence ────────────────────────────────────────────────────────────────

#: Evidence kinds a viewer can open, and the page that shows them.
_LINKED_KINDS: Final = frozenset({"trace", "rating", "assignment_run", "flow_run", "evaluation_run"})


@dataclass(frozen=True)
class EvidenceLink:
    kind: str
    id: str | None
    href: str | None
    hidden: bool


async def evidence_links(
    db: AsyncSession, user: Any, user_group_ids: list[str], refs: list[dict]
) -> list[EvidenceLink]:
    """*refs* (``[{"kind", "id"}]``, values from a proposal: hostile) as the viewer may follow them."""
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.flow_run import FlowRun
    from app.models.llm_trace import LLMTrace
    from app.services.builtin_tools.traces import may_read_agent_traces
    from app.services.evaluation.availability import evaluation_enabled

    agents: dict[uuid.UUID, Any] = {}

    async def agent_chats_readable(agent_id: uuid.UUID | None) -> bool:
        if agent_id is None:
            return False
        if agent_id not in agents:
            agents[agent_id] = await db.get(Agent, agent_id)
        agent = agents[agent_id]
        return agent is not None and await may_read_agent_traces(db, user, agent, user_group_ids)

    links: list[EvidenceLink] = []
    for ref in refs[:200]:
        kind = str(ref.get("kind") or "")[:32] if isinstance(ref, dict) else ""
        try:
            ref_id = uuid.UUID(str(ref.get("id"))) if isinstance(ref, dict) else None
        except ValueError:
            ref_id = None
        if kind not in _LINKED_KINDS or ref_id is None:
            links.append(EvidenceLink(kind or "unknown", None, None, True))
            continue
        href: str | None = None
        if kind == "trace":
            agent_id = (await db.execute(select(LLMTrace.agent_id).where(LLMTrace.id == ref_id))).scalar_one_or_none()
            if await agent_chats_readable(agent_id):
                href = f"/observability/traces/{ref_id}"
        elif kind == "rating":
            agent_id = await _rating_agent_id(db, ref_id)
            if await agent_chats_readable(agent_id):
                href = f"/agents/{agent_id}/feedback"
        elif kind == "assignment_run":
            agent_id = (
                await db.execute(select(AssignmentRun.agent_id).where(AssignmentRun.id == ref_id))
            ).scalar_one_or_none()
            if await agent_chats_readable(agent_id):
                href = f"/operations/executions/{ref_id}"
        elif kind == "flow_run":
            flow_id = (await db.execute(select(FlowRun.flow_id).where(FlowRun.id == ref_id))).scalar_one_or_none()
            target = await resolve_target(db, user, user_group_ids, "flow", flow_id) if flow_id else None
            if target is not None:
                href = f"/flows/library/{flow_id}"
        elif kind == "evaluation_run" and user.is_admin and evaluation_enabled():
            href = f"/evaluations/runs/{ref_id}"
        links.append(EvidenceLink(kind, str(ref_id) if href else None, href, href is None))
    return links


async def _rating_agent_id(db: AsyncSession, rating_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.annotation import Annotation
    from app.models.chat import Conversation, Message, MessageFeedback

    agent_id = (await db.execute(select(Annotation.agent_id).where(Annotation.id == rating_id))).scalar_one_or_none()
    if agent_id is not None:
        return agent_id
    return (
        await db.execute(
            select(Conversation.agent_id)
            .join(Message, Message.conversation_id == Conversation.id)
            .join(MessageFeedback, MessageFeedback.message_id == Message.id)
            .where(MessageFeedback.id == rating_id)
        )
    ).scalar_one_or_none()


async def readable_proposals(db: AsyncSession, user: Any, group_ids: list[str], rows: list) -> list[tuple]:
    """``(proposal, target, handler)`` for each row *user* may read: level 3's rule (``can_read``) on the target row."""
    from app.services import improvement_proposals as proposals

    readable = []
    targets: dict[tuple[str, uuid.UUID], object] = {}
    for proposal in rows:
        handler = proposals.handler_for(proposal.proposal_type)
        if handler is None or not proposals.surface_enabled(proposal):
            continue
        # Keyed by the handler's type, not the target's: two types on one target (an assignment's
        # prompt rewrite and its flow replacement) load it as different objects (#10401).
        key = (proposal.proposal_type, proposal.target_id)
        if key not in targets:
            targets[key] = await handler.load_target(db, proposal.target_id, lock=False)
        target = targets[key]
        if target is not None and proposals.can_read(handler, target, user, group_ids):
            readable.append((proposal, target, handler))
    return readable
