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

"""Tool usage tracking – increments call_count / last_called_at on entities.

After every agentic loop (chat or assignment), call :func:`track_tool_usage` with
the conversation messages and the :class:`CompositeToolExecutor` to attribute
each tool call to the owning Integration, MCPServer, or CustomTool.

Uses an independent short-lived DB session so that row locks from the UPDATE
statements are released immediately, avoiding contention with the long-lived
agentic-loop transactions that can hold the caller's session open for minutes.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _build_tool_lookup(
    composite: object,
) -> dict[str, tuple[str, str | None]]:
    """Build a name→(source, entity_id) map that covers both original and
    sanitised tool names.

    MCP servers often have tool names containing dots / slashes that get
    sanitised by :func:`sanitize_tool_name` before being sent to the LLM.
    The conversation messages use sanitised names, while the composite stores
    original names.  This helper maps **both** variants so tracking never
    misses a call.
    """
    from app.tools.base import CompositeToolExecutor, sanitize_tool_name

    if not isinstance(composite, CompositeToolExecutor):
        return {}

    lookup: dict[str, tuple[str, str | None]] = {}
    for d in composite.all_tools:
        entry = (d.source, d.entity_id)
        lookup[d.name] = entry
        sanitised = sanitize_tool_name(d.name)
        if sanitised != d.name:
            lookup[sanitised] = entry
    return lookup


def _budget_cut_call_ids(messages: list, index: int) -> set[str]:
    """Ids of the calls of ``messages[index]`` the tool-call budget cut:
    the loop answered them without running them and tagged its answer
    (``not_run="budget"``), never inferred from the text a tool could return.
    Read from the results that follow the turn, since a call id is only
    unique within its turn."""
    cut: set[str] = set()
    for position in range(index + 1, len(messages)):
        follower = messages[position]
        if getattr(follower, "role", None) != "tool":
            break
        if getattr(follower, "not_run", None) == "budget":
            cut.add(follower.tool_call_id)
    return cut


async def track_tool_usage(
    db: AsyncSession,
    messages: list,
    composite: object,
) -> None:
    """Extract tool calls from *messages* and bump counters on owning entities.

    Opens its own short-lived session so UPDATE row locks are held for
    milliseconds rather than the full duration of the caller's transaction.

    Parameters
    ----------
    db:
        Caller's session — **not used for writes** (kept for signature
        compatibility; may be removed in a future refactor).
    messages:
        The full conversation produced by ``run_agent_loop``.  Assistant
        messages with ``tool_calls`` are inspected.
    composite:
        A :class:`CompositeToolExecutor` used during the loop so we can
        resolve ``entity_id`` and ``source`` per tool name.
    """
    from app.tools.base import CompositeToolExecutor

    if not isinstance(composite, CompositeToolExecutor):
        return

    lookup = _build_tool_lookup(composite)
    if not lookup:
        return

    integration_counts: Counter[str] = Counter()
    mcp_counts: Counter[str] = Counter()
    custom_counts: Counter[str] = Counter()

    for i, m in enumerate(messages):
        if getattr(m, "role", None) != "assistant":
            continue
        tool_calls = getattr(m, "tool_calls", None)
        if not tool_calls:
            continue
        cut = _budget_cut_call_ids(messages, i)
        for tc in tool_calls:
            if tc.id in cut:
                continue
            entry = lookup.get(tc.name)
            if not entry:
                continue
            source, entity_id = entry
            if not entity_id:
                continue
            if source.startswith("integration:"):
                integration_counts[entity_id] += 1
            elif source.startswith("mcp:"):
                mcp_counts[entity_id] += 1
            elif source.startswith("custom:"):
                custom_counts[entity_id] += 1

    if not integration_counts and not mcp_counts and not custom_counts:
        return

    now = datetime.now(UTC)

    from app.core.database import async_session

    async with async_session() as tracking_db:
        try:
            if integration_counts:
                await _bump_integrations(tracking_db, integration_counts, now)
            if mcp_counts:
                await _bump_mcp_servers(tracking_db, mcp_counts, now)
            if custom_counts:
                await _bump_custom_tools(tracking_db, custom_counts, now)
            await tracking_db.commit()
        except Exception:
            await tracking_db.rollback()
            raise


def calls_already_in(messages) -> set[str]:
    """Tool-call ids present before a turn starts.

    Handed to :func:`record_chat_gate_outcomes` so it can tell this turn's calls
    from the conversation it inherited — ``messages`` is the whole restored
    history, and without this every turn would re-list every earlier turn's
    calls, each with a null outcome because this turn's gate never saw them.

    Captured by the caller before its loop rather than inferred afterwards: a
    turn's own messages cannot be identified by position, since steering injects
    user messages mid-turn and compaction rewrites the list underneath.
    """
    return {
        tc.id
        for m in messages
        for tc in (getattr(m, "tool_calls", None) or [])
        if getattr(tc, "id", None)
    }


async def record_chat_gate_outcomes(
    messages: list,
    composite: object,
    *,
    outcomes: dict[str, str],
    user_id: object,
    hitl_enabled: bool,
    #: What to record when ``hitl_enabled`` is false. Defaults to the
    #: agent-level switch's outcome; the channel path names the bot instead.
    hitl_off_outcome: str | None = None,
    conversation_id: object = None,
    exclude_call_ids: set[str] | None = None,
) -> None:
    """Record why each of a chat turn's tool calls ran, or what a human decided.

    FR-020 asks for every tool call and its gate outcome, and gating covers
    chat as much as runs (FR-002a) — but only the run path was recording it, in
    ``AssignmentRun.actions_taken``. A conversation has no such row, so "why did
    this run without asking me?" was answerable for a schedule and not for a
    chat, which is exactly the asymmetry FR-002a exists to remove.

    One audit record per turn, listing every call, rather than one per call:
    that matches the density of the run's action list and keeps the audit screen
    readable, while still naming each call and its outcome in ``details``.

    Called after the loop, alongside :func:`track_tool_usage`, and shares its
    short-lived-session discipline. Best-effort throughout: a missing record is
    bad, a chat turn failing because a record could not be written is worse.

    ``outcomes`` maps ``tool_call_id`` to the outcome the gate decided. What to
    record when a call is absent from it belongs to
    :func:`app.tools.approval.resolve_log_outcome`, shared with the run path so
    the two cannot answer the same question differently — they had the same
    expression copied into both, and so the same blank in both.

    A call that *paused* is stamped by whoever handled the pause (see
    :func:`app.tools.approval.mark_paused`), not inferred here. Inferring it
    would mislabel the one case that must stay a blank: a gate that failed to
    install lets calls run with no outcome, and calling those "paused" would
    claim a tool did not run when it did.

    ``exclude_call_ids`` names the calls already in the conversation when this
    turn began. ``messages`` is the whole restored history, so without it every
    turn would re-list every earlier turn's calls — each with a null outcome,
    since this turn's gate never saw them — and FR-020's "why did this run
    without asking me?" would answer null for most rows. Passed by the caller
    rather than inferred here: a turn's own messages cannot be identified by
    position, because steering injects user messages mid-turn and compaction
    rewrites the list underneath.

    A turn whose gate was off and decided nothing records nothing: that is
    every turn of an agent that does not ask - every agent of an upgraded
    install - and a row per turn saying only "the gate was off" would flood
    the audit log with no answer anyone asked for.
    """
    from app.tools.base import CompositeToolExecutor

    if not hitl_enabled and not outcomes:
        return
    if not isinstance(composite, CompositeToolExecutor):
        return

    from app.tools.approval import resolve_log_outcome

    lookup = _build_tool_lookup(composite)
    _already_recorded = exclude_call_ids or set()
    calls: list[dict] = []
    for m in messages:
        if getattr(m, "role", None) != "assistant":
            continue
        for tc in getattr(m, "tool_calls", None) or []:
            if tc.id in _already_recorded:
                continue
            entry = lookup.get(tc.name)
            calls.append(
                {
                    "tool": tc.name,
                    "source": entry[0] if entry else "",
                    "gate_outcome": resolve_log_outcome(
                        tc.name,
                        tc.id,
                        outcomes=outcomes,
                        hitl_enabled=hitl_enabled,
                        **(
                            {"hitl_off_outcome": hitl_off_outcome}
                            if hitl_off_outcome
                            else {}
                        ),
                    ),
                }
            )

    if not calls:
        return

    # An anonymous surface (the public embed) has nobody to attribute the record
    # to, and ``audit_logs.user_id`` is NOT NULL. Rather than drop the
    # information, emit it to the application log — the only durable place left.
    if not user_id:
        logger.info(
            "chat_tool_gate_outcomes",
            extra={"conversation_id": str(conversation_id or ""), "calls": calls},
        )
        return

    from app.core.database import async_session
    from app.models.user import User
    from app.services.audit import log_audit

    try:
        async with async_session() as audit_db:
            user = await audit_db.scalar(select(User).where(User.id == user_id))
            if user is None:
                logger.info(
                    "chat_tool_gate_outcomes",
                    extra={
                        "conversation_id": str(conversation_id or ""),
                        "calls": calls,
                    },
                )
                return
            await log_audit(
                audit_db,
                user=user,
                action="execute",
                entity_type="chat_tool_calls",
                entity_id=conversation_id,
                summary=(
                    f"{len(calls)} tool "
                    f"{'call' if len(calls) == 1 else 'calls'} in a conversation"
                ),
                details={"calls": calls},
            )
            await audit_db.commit()
    except Exception:
        logger.warning(
            "Could not record chat gate outcomes", exc_info=True
        )


async def _bump_integrations(
    db: AsyncSession, counts: Counter[str], now: datetime
) -> None:
    from app.models.integration import Integration

    for eid, cnt in counts.items():
        try:
            uid = uuid.UUID(eid)
        except ValueError:
            continue
        await db.execute(
            update(Integration)
            .where(Integration.id == uid)
            .values(
                call_count=Integration.call_count + cnt,
                last_called_at=now,
            )
        )


async def _bump_mcp_servers(
    db: AsyncSession, counts: Counter[str], now: datetime
) -> None:
    from app.models.mcp_server import MCPServer

    for eid, cnt in counts.items():
        try:
            uid = uuid.UUID(eid)
        except ValueError:
            continue
        await db.execute(
            update(MCPServer)
            .where(MCPServer.id == uid)
            .values(
                call_count=MCPServer.call_count + cnt,
                last_called_at=now,
            )
        )


async def _bump_custom_tools(
    db: AsyncSession, counts: Counter[str], now: datetime
) -> None:
    from app.models.custom_tool import CustomTool

    for eid, cnt in counts.items():
        try:
            uid = uuid.UUID(eid)
        except ValueError:
            continue
        await db.execute(
            update(CustomTool)
            .where(CustomTool.id == uid)
            .values(
                call_count=CustomTool.call_count + cnt,
                last_called_at=now,
            )
        )
