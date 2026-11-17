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

"""Sandbox copies of agents and flows (#8255, PRD 6.6).

A copy is a hidden clone (``is_sandbox``): ``user_id`` NULL, not
company-managed, shared with no one, so the shared visibility helpers
(``helpers.hidden_rows_excluded``) keep it out of every library, picker and
fetch by id, admins included; analytics leave its traces out. It carries its
source's configuration as an in-place duplicate does (the agent copy manifest,
``models/agent_copy.py``), minus web and channel exposure, the Concierge opt-in
and role, and the tool groups that read with no consent rule
(:func:`sandbox_tool_ids`).

Lifetime (PRD section 13): ``expires_at`` is 14 days after creation. A proposal
referencing the copy keeps it (:func:`retain_sandbox_copy`, and the reference
checks level 3 registers); :func:`purge_expired_sandbox_copies` deletes the
rest, with their traces.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sandbox_copy import SOURCE_AGENT, SOURCE_FLOW, SandboxCopy

logger = logging.getLogger(__name__)

SANDBOX_COPY_TTL = timedelta(days=14)
#: Copies one purge pass deletes at most.
_PURGE_BATCH = 200

SourceType = Literal["agent", "flow"]
#: Copy ids among the candidates that something still references (level 3:
#: an open proposal's experiment).
SandboxReferenceCheck = Callable[[AsyncSession, list[uuid.UUID]], Awaitable[set[uuid.UUID]]]
_reference_checks: list[SandboxReferenceCheck] = []


class SandboxCopyError(Exception):
    """A copy could not be made; the message is safe to return to a caller."""


@dataclass(frozen=True)
class SandboxOverrides:
    """What the copy changes from its source: the variant under test."""

    persona: str | None = None
    model_id: str | None = None
    graph: dict[str, Any] | None = None


def is_sandbox_copy(row: Any) -> bool:
    """Whether an agent or flow row is a sandbox copy."""
    return bool(getattr(row, "is_sandbox", False))


def sandbox_tool_ids(tool_ids: Sequence[str] | None) -> list[str]:
    """*tool_ids* without the groups that read as the runner with no consent
    rule (``fleet_seed.NON_CONSENT_TOOL_GROUPS``): never on a copy, at its
    creation and at each of its turns. Defence in depth: a copy's turn keeps
    the Concierge's reach whatever it carries (``identity.in_concierge_reach``).
    """
    from app.services.concierge.fleet_seed import NON_CONSENT_TOOL_GROUPS

    return [tool_id for tool_id in tool_ids or () if tool_id not in NON_CONSENT_TOOL_GROUPS]


def register_sandbox_reference_check(check: SandboxReferenceCheck) -> None:
    """Keep a copy past its expiry while *check* reports it referenced."""
    if check not in _reference_checks:
        _reference_checks.append(check)


def _ids(values: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        try:
            out.append(str(uuid.UUID(str(value))))
        except (TypeError, ValueError):
            continue
    return out


async def _copy_agent(db: AsyncSession, source_id: uuid.UUID, overrides: SandboxOverrides) -> uuid.UUID:
    from app.api.v1.helpers import generate_unique_agent_slug
    from app.core.utils import fit_copy_name
    from app.models.agent import Agent
    from app.models.agent_copy import build_agent_copy_kwargs
    from app.services.agent_config_versioning import seed_initial_agent_versions

    source = await db.get(Agent, source_id)
    if source is None or source.is_sandbox:
        raise SandboxCopyError("The agent to copy was not found.")
    name = fit_copy_name(source.name, Agent.__table__.c.name, prefix="Sandbox of ")
    kwargs = build_agent_copy_kwargs(
        source,
        user_id=None,
        is_company_managed=False,
        name=name,
        slug=await generate_unique_agent_slug(db, name),
        web_enabled=False,
        # Reachable from no channel: a copy would answer there for real.
        channel_config=None,
    )
    for consent_field in ("concierge_improvement_opt_in", "concierge_role"):
        kwargs.pop(consent_field, None)
    kwargs["builtin_tool_ids"] = sandbox_tool_ids(kwargs.get("builtin_tool_ids"))
    copy = Agent(**kwargs, is_sandbox=True)
    if overrides.persona is not None:
        copy.persona = overrides.persona
    if overrides.model_id:
        copy.llm_model = overrides.model_id
    db.add(copy)
    await db.flush()
    await seed_initial_agent_versions(db, agent=copy, author_id=None)
    return copy.id


async def _copy_flow(db: AsyncSession, source_id: uuid.UUID | None, overrides: SandboxOverrides) -> uuid.UUID:
    from app.core.utils import fit_copy_name
    from app.models.flow import FLOW_STATUS_DRAFT, Flow
    from app.services.flow_graph import empty_graph
    from app.services.flow_versioning import create_flow_version

    source = None
    if source_id is not None:
        source = await db.get(Flow, source_id)
        if source is None or source.is_sandbox:
            raise SandboxCopyError("The flow to copy was not found.")
    graph = deepcopy(overrides.graph if overrides.graph is not None else (source.graph if source else empty_graph()))
    copy = Flow(
        user_id=None,
        name=fit_copy_name(source.name if source else "New flow", Flow.__table__.c.name, prefix="Sandbox of "),
        description=(source.description or "") if source else "",
        status=FLOW_STATUS_DRAFT,
        graph=graph or empty_graph(),
        version_number=1,
        tags=list(source.tags or []) if source else [],
        is_company_managed=False,
        shared_group_ids=None,
        is_sandbox=True,
    )
    db.add(copy)
    await db.flush()
    await create_flow_version(
        db, flow_id=copy.id, graph=copy.graph, author_id=None, commit_message="Sandbox copy"
    )
    return copy.id


async def create_sandbox_copy(
    db: AsyncSession,
    *,
    source_type: SourceType,
    source_id: uuid.UUID | str | None,
    created_for: str,
    created_by_agent_id: uuid.UUID | None = None,
    overrides: SandboxOverrides | None = None,
    source_trace_ids: Sequence[Any] = (),
    source_run_ids: Sequence[Any] = (),
) -> SandboxCopy:
    """Clone *source_id* into the sandbox (flushed; the caller commits).

    Access is the caller's to check (``tools.sandbox_access_refusal``): this is
    the service every surface shares. ``source_id`` ``None`` makes a new, empty
    flow prototype (flows only).
    """
    overrides = overrides or SandboxOverrides()
    parsed = None
    if source_id is not None:
        try:
            parsed = uuid.UUID(str(source_id))
        except (TypeError, ValueError):
            raise SandboxCopyError("The source id is not valid.") from None
    if source_type == SOURCE_AGENT:
        if parsed is None:
            raise SandboxCopyError("An agent copy needs the agent to copy.")
        copy_agent_id, copy_flow_id = await _copy_agent(db, parsed, overrides), None
    elif source_type == SOURCE_FLOW:
        copy_agent_id, copy_flow_id = None, await _copy_flow(db, parsed, overrides)
    else:
        raise SandboxCopyError("A sandbox copy is of an agent or a flow.")
    row = SandboxCopy(
        source_type=source_type,
        source_id=parsed,
        copy_agent_id=copy_agent_id,
        copy_flow_id=copy_flow_id,
        created_for=(created_for or "")[:255],
        created_by_agent_id=created_by_agent_id,
        source_trace_ids=_ids(source_trace_ids) or None,
        source_run_ids=_ids(source_run_ids) or None,
        expires_at=datetime.now(UTC) + SANDBOX_COPY_TTL,
    )
    db.add(row)
    await db.flush()
    logger.info("Sandbox copy %s of %s %s created", row.id, source_type, parsed)
    return row


async def get_sandbox_copy(db: AsyncSession, copy_id: uuid.UUID | str) -> SandboxCopy | None:
    try:
        parsed = uuid.UUID(str(copy_id))
    except (TypeError, ValueError):
        return None
    return await db.get(SandboxCopy, parsed)


async def sandbox_copy_of(
    db: AsyncSession, *, agent_id: Any = None, flow_id: Any = None
) -> SandboxCopy | None:
    """The copy row whose copied agent or flow is *agent_id* / *flow_id*."""
    column, value = (SandboxCopy.copy_agent_id, agent_id) if agent_id is not None else (SandboxCopy.copy_flow_id, flow_id)
    try:
        parsed = uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
    return (await db.execute(select(SandboxCopy).where(column == parsed))).scalar_one_or_none()


async def retain_sandbox_copy(db: AsyncSession, copy_id: uuid.UUID | str, *, until: datetime) -> None:
    """Keep the copy at least until *until* (never shortens its life)."""
    row = await get_sandbox_copy(db, copy_id)
    if row is not None and row.expires_at < until:
        row.expires_at = until
        await db.flush()


async def _delete_copy_traces(db: AsyncSession, agent_ids: list[uuid.UUID]) -> None:
    """The traces of purged agent copies, with their offloaded payloads: a
    deleted agent's traces would otherwise surface to its run's user."""
    if not agent_ids:
        return
    from app.models.llm_trace import LLMDailyStats, LLMTrace
    from app.services.tracing.payload_offload import delete_trace_objects

    trace_ids = list((await db.execute(select(LLMTrace.id).where(LLMTrace.agent_id.in_(agent_ids)))).scalars())
    if trace_ids:
        try:
            await delete_trace_objects(trace_ids)
        except Exception:
            logger.warning("Sandbox purge: offloaded payloads of %d trace(s) were not deleted", len(trace_ids))
        await db.execute(delete(LLMTrace).where(LLMTrace.id.in_(trace_ids)))
    await db.execute(delete(LLMDailyStats).where(LLMDailyStats.agent_id.in_(agent_ids)))


async def purge_expired_sandbox_copies_tick() -> None:
    """The scheduler's step: one purge pass in a transaction of its own."""
    from app.core.database import async_session

    async with async_session() as db:
        await purge_expired_sandbox_copies(db)
        await db.commit()


async def purge_expired_sandbox_copies(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete expired copies nothing references, with their agent or flow,
    runs and traces, and retain the referenced ones another TTL (flushed; the
    caller commits). Returns how many went."""
    from app.models.agent import Agent
    from app.models.flow import Flow

    expired = list(
        (
            await db.execute(
                select(SandboxCopy)
                .where(SandboxCopy.expires_at < (now or datetime.now(UTC)))
                .order_by(SandboxCopy.expires_at)
                .limit(_PURGE_BATCH)
            )
        ).scalars()
    )
    if not expired:
        return 0
    referenced: set[uuid.UUID] = set()
    for check in _reference_checks:
        referenced |= await check(db, [row.id for row in expired])
    # Retained, so the next pass reaches the expired copies queued behind them.
    for row in expired:
        if row.id in referenced:
            row.expires_at = (now or datetime.now(UTC)) + SANDBOX_COPY_TTL
    doomed = [row for row in expired if row.id not in referenced]
    agent_ids = [row.copy_agent_id for row in doomed if row.copy_agent_id]
    flow_ids = [row.copy_flow_id for row in doomed if row.copy_flow_id]
    await _delete_copy_traces(db, agent_ids)
    if agent_ids:
        from app.api.v1.helpers import purge_agent_references

        for agent_id in agent_ids:
            await purge_agent_references(db, agent_id)
    # The copy rows and their runs follow by ON DELETE CASCADE. Not through
    # ``agent_deletion.delete_agent``: that path audits a person's deletion and
    # hands the agent's conversations, runs and memory on, and a copy has none
    # of them (its runs are evaluation turns: no conversation, no memory, no
    # assignment), nor a person who deleted it.
    if agent_ids:
        await db.execute(delete(Agent).where(Agent.id.in_(agent_ids), Agent.is_sandbox.is_(True)))
    if flow_ids:
        await db.execute(delete(Flow).where(Flow.id.in_(flow_ids), Flow.is_sandbox.is_(True)))
    await db.flush()
    if doomed:
        logger.info("Sandbox purge: %d expired copies deleted", len(doomed))
    return len(doomed)

