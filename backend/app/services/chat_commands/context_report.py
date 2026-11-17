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

"""The ``/context`` report (#6590): how a conversation uses its context window.

Read, never re-assembled. The agent loop publishes, with each reading, what
every bucket is made of (``agent_loop._context_detail``), and the provider's
count for the call it measured (``context_usage.provider_prompt_usage``); the
run stores both on its reply (``ContextUsageTracker.command_meta``). The report
reads the conversation's latest reading, adds what was written since, and
reconciles the estimate with the provider's count. Assembling the request
again would connect MCP servers and resolve skills, memory and knowledge for a
read-only command, and drift from what the loop really sends.

Before a conversation has a reading of its own, the report shows the overhead
(system prompt and tools) of the caller's latest reading with the same agent:
the cost of the agent before the first message. Without one there is nothing
to show, and the report says so.

Names and sizes only, never a prompt's, a memory's or a tool's text.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat import Conversation, Message
from app.services.context_usage import (
    CONTEXT_COMMAND_META_KEYS,
    CONTEXT_LEVEL_APPROACHING,
    CONTEXT_LEVEL_NORMAL,
    ContextUsage,
    read_context_usage,
    split_tokens,
)
from app.services.mention_dispatch import ANSWERED_BY_KEY
from app.services.prompt_budget import estimate_tokens_for_length

#: The categories of the report, in the order it lists them.
CATEGORY_KEYS: tuple[str, ...] = (
    "system_prompt",
    "memory",
    "knowledge",
    "skills",
    "builtin_tools",
    "mcp_tools",
    "integration_tools",
    "custom_tools",
    # A reading stored without its detail splits the tools in two only.
    "connected_tools",
    # The tools beyond what a reading keeps a row for.
    "other_tools",
    "conversation_summary",
    "tool_results",
    "messages",
    # What the provider counted beyond the estimate.
    "unattributed",
)

#: The system-prompt sections the assembler labels that are not instructions
#: (``agent_chat._prompt_sections``); every other label, and what no label
#: covers (``platform``), is the system prompt proper.
_SECTION_CATEGORIES: dict[str, str] = {
    "user_memory": "memory",
    "agent_memory": "memory",
    "workspace_instructions": "knowledge",
    "workspace_sources": "knowledge",
    "skills": "skills",
    "skills_catalog": "skills",
}

#: A tool row's ``kind`` (``agent_loop._context_detail``) to its category.
_TOOL_KIND_CATEGORIES: dict[str, str] = {
    "builtin": "builtin_tools",
    "mcp": "mcp_tools",
    "integration": "integration_tools",
    "channel_bot": "integration_tools",
    "custom": "custom_tools",
}

#: The buckets of a reading that measure the conversation rather than the
#: agent: left out of a reading borrowed from another conversation.
_CONVERSATION_BUCKETS = frozenset({"summary", "tool_results", "conversation"})

#: Assistant messages scanned, newest first, for the latest reading: a turn
#: stores one reading, so a handful of replies always reaches one.
_READING_SCAN_LIMIT = 50


class ContextItem(BaseModel):
    name: str
    tokens: int
    #: The MCP server or integration a tool belongs to.
    group: str | None = None


class ContextCategory(BaseModel):
    key: str
    tokens: int
    items: list[ContextItem] = []


class ContextCompaction(BaseModel):
    """Where the conversation stands against the loop's compaction gate.

    The gate measures the messages, the system prompt included, not the tool
    definitions: these figures are not shares of ``used_tokens``.
    """

    level: str
    gate_tokens: int
    approaching_tokens: int
    trigger_tokens: int
    target_tokens: int
    free_tokens: int


ReportState = Literal["measured", "estimated", "previous_conversation", "none"]


class ContextReport(BaseModel):
    kind: Literal["context"] = "context"
    #: ``measured``: this conversation's latest call, as its provider counted
    #: it. ``estimated``: the same call, estimated (no provider count).
    #: ``previous_conversation``: no reading here yet, the agent's overhead
    #: from the caller's latest conversation with it. ``none``: nothing read.
    state: ReportState
    detail: bool
    model: str | None = None
    window: int | None = None
    used_tokens: int | None = None
    cached_tokens: int | None = None
    #: Estimated tokens of the answer the measured call wrote and of the
    #: messages written since, which the next request carries too.
    new_message_tokens: int = 0
    compaction: ContextCompaction | None = None
    #: The agent was edited after the reading: its tools or prompt may differ.
    agent_changed: bool = False
    categories: list[ContextCategory] = []


# ── Reading the stored detail back ──────────────────────────────────────────


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _counts(raw: Any, expected: int) -> dict[str, int] | None:
    """A ``{name: tokens}`` map summing to *expected*, or ``None``.

    All-or-nothing, like the breakdown it details: rows that do not add up to
    their bucket read as broken numbers.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    if not all(isinstance(k, str) and _is_count(v) for k, v in raw.items()):
        return None
    return dict(raw) if sum(raw.values()) == expected else None


def _tool_rows(raw: Any, omitted: Any, expected: int) -> tuple[list[dict], int] | None:
    """The detail's tool rows and omitted tokens, summing to *expected*."""
    if not isinstance(raw, list):
        return None
    rows = []
    for row in raw:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            return None
        if row.get("kind") not in _TOOL_KIND_CATEGORIES or not _is_count(row.get("tokens")):
            return None
        group = row.get("group")
        rows.append({**row, "group": group if isinstance(group, str) and group else None})
    omitted_tokens = 0
    if omitted is not None:
        if not isinstance(omitted, dict) or not _is_count(omitted.get("tokens")):
            return None
        omitted_tokens = omitted["tokens"]
    if sum(row["tokens"] for row in rows) + omitted_tokens != expected:
        return None
    return rows, omitted_tokens


def _compaction_figures(raw: Any) -> dict[str, int] | None:
    keys = ("gate_tokens", "approaching_tokens", "trigger_tokens", "target_tokens")
    if not isinstance(raw, dict):
        return None
    values: dict[str, int] = {}
    for key in keys:
        value = raw.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        values[key] = value
    return values


class _Categories(NamedTuple):
    totals: dict[str, int]
    items: dict[str, list[ContextItem]]


def _categories(
    usage: ContextUsage, detail: Mapping[str, Any] | None, *, overhead_only: bool
) -> _Categories:
    """The reading's tokens by category, each category's items summing to it.

    A category whose detail is missing or does not add up keeps its total as
    one item-less line.
    """
    breakdown = usage.breakdown or {}
    detail = detail or {}
    items: dict[str, list[ContextItem]] = {}
    totals: dict[str, int] = {}

    def add(category: str, tokens: int, item: ContextItem | None = None) -> None:
        if tokens <= 0:
            return
        totals[category] = totals.get(category, 0) + tokens
        items.setdefault(category, [])
        if item is not None:
            items[category].append(item)

    system = _counts(detail.get("system"), breakdown.get("system", 0))
    if system:
        for label, tokens in system.items():
            add(_SECTION_CATEGORIES.get(label, "system_prompt"), tokens, ContextItem(name=label, tokens=tokens))
    else:
        add("system_prompt", breakdown.get("system", 0))

    tools = _tool_rows(
        detail.get("tools"),
        detail.get("tools_omitted"),
        breakdown.get("tools", 0) + breakdown.get("dynamic_tools", 0),
    )
    if tools:
        rows, omitted_tokens = tools
        for row in rows:
            add(
                _TOOL_KIND_CATEGORIES[row["kind"]],
                row["tokens"],
                ContextItem(name=row["name"], tokens=row["tokens"], group=row["group"]),
            )
        add("other_tools", omitted_tokens)
    else:
        add("builtin_tools", breakdown.get("tools", 0))
        add("connected_tools", breakdown.get("dynamic_tools", 0))

    if not overhead_only:
        add("conversation_summary", breakdown.get("summary", 0))
        results = _counts(detail.get("tool_results"), breakdown.get("tool_results", 0))
        if results:
            for name, tokens in results.items():
                add("tool_results", tokens, ContextItem(name=name, tokens=tokens))
        else:
            add("tool_results", breakdown.get("tool_results", 0))
        add("messages", breakdown.get("conversation", 0))

    # A reading with no breakdown at all is one line.
    if not totals and usage.tokens and not overhead_only:
        add("messages", usage.tokens)
    return _Categories(totals, items)


def _scaled(categories: _Categories, total: int) -> list[ContextCategory]:
    """*categories* as report lines in :data:`CATEGORY_KEYS` order, shrunk to
    *total* tokens when they are over it.

    Items follow their category's share, so every line still sums exactly.
    """
    totals = categories.totals
    shares = split_tokens(total, totals) if sum(totals.values()) > total else totals
    lines = []
    for key in CATEGORY_KEYS:
        tokens = shares.get(key, 0)
        if tokens <= 0:
            continue
        items = categories.items[key]
        if items and tokens != totals[key]:
            split = split_tokens(tokens, dict(enumerate(item.tokens for item in items)))
            items = [
                item.model_copy(update={"tokens": split[index]})
                for index, item in enumerate(items)
                if split.get(index)
            ]
        lines.append(ContextCategory(key=key, tokens=tokens, items=sorted(items, key=lambda i: -i.tokens)))
    return lines


# ── Finding the reading ─────────────────────────────────────────────────────


class _Reading:
    __slots__ = ("created_at", "detail", "meta", "sequence", "usage")

    def __init__(self, usage: ContextUsage, meta: dict[str, Any], sequence: int, created_at: datetime) -> None:
        self.usage = usage
        self.meta = meta
        self.detail = meta.get("context_detail") if isinstance(meta.get("context_detail"), dict) else None
        self.sequence = sequence
        self.created_at = created_at


def _answered_by_another_agent(meta: Mapping[str, Any], agent_id: uuid.UUID | None) -> bool:
    """Whether a reply was written by an agent the conversation @mentioned:
    its reading measures that agent's context, not the conversation agent's.

    A mentioned agent's reply carries the gauge reading of its own run but
    never the command keys, which only the conversation agent's run stores:
    an attributed reply without them is a mentioned agent's, whatever the
    conversation's agent (the General Assistant's included).
    """
    attribution = meta.get(ANSWERED_BY_KEY)
    if not isinstance(attribution, dict):
        return False
    if not any(key in meta for key in CONTEXT_COMMAND_META_KEYS):
        return True
    other = attribution.get("agent_id")
    return agent_id is not None and isinstance(other, str) and other != str(agent_id)


async def _latest_reading(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    excluded_conversation_id: uuid.UUID | None = None,
) -> _Reading | None:
    """The newest reading of *conversation_id*; without one, of any
    conversation *user_id* holds with *agent_id*, the excluded one aside."""
    query = select(Message.sequence, Message.created_at, Message.metadata_json).where(
        Message.role == "assistant",
        Message.metadata_json.is_not(None),
        Message.metadata_json.contains('"context_window"'),
    )
    if conversation_id is not None:
        query = query.where(Message.conversation_id == conversation_id).order_by(Message.sequence.desc())
    else:
        query = (
            query.join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.user_id == user_id,
                Conversation.agent_id == agent_id if agent_id is not None else Conversation.agent_id.is_(None),
            )
            .order_by(Message.created_at.desc())
        )
        if excluded_conversation_id is not None:
            query = query.where(Conversation.id != excluded_conversation_id)
    rows = await db.execute(query.limit(_READING_SCAN_LIMIT))
    for sequence, created_at, metadata_json in rows.all():
        try:
            meta = json.loads(metadata_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(meta, dict) or _answered_by_another_agent(meta, agent_id):
            continue
        usage = read_context_usage(meta)
        if usage is not None:
            return _Reading(usage, meta, sequence, created_at)
    return None


async def _messages_since(db: AsyncSession, conversation_id: uuid.UUID, sequence: int | None) -> int:
    """Estimated tokens of the conversation's messages from *sequence* on (all
    of them for ``None``).

    The reply that stores a reading counts: the reading was taken before the
    call that wrote it, and the next request carries that answer.
    """
    query = select(func.coalesce(func.sum(func.length(Message.content)), 0)).where(
        Message.conversation_id == conversation_id
    )
    if sequence is not None:
        query = query.where(Message.sequence >= sequence)
    length = await db.scalar(query)
    return estimate_tokens_for_length(int(length or 0))


# ── The report ──────────────────────────────────────────────────────────────


async def build_context_report(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation: Conversation | None,
    agent: Agent | None,
    detail: bool,
) -> ContextReport:
    """The ``/context`` report of *conversation*, or of *agent* before one.

    The caller has checked that *user_id* may send in *conversation* (or start
    one with *agent*); *agent* is the conversation's, ``None`` for the General
    Assistant.
    """
    agent_id = agent.id if agent is not None else None
    reading: _Reading | None = None
    state: ReportState = "none"
    if conversation is not None:
        reading = await _latest_reading(db, agent_id=agent_id, conversation_id=conversation.id)
        if reading is not None:
            state = "measured" if _is_count(reading.meta.get("context_provider_tokens")) else "estimated"
    if reading is None:
        reading = await _latest_reading(
            db,
            agent_id=agent_id,
            user_id=user_id,
            excluded_conversation_id=conversation.id if conversation is not None else None,
        )
        if reading is not None:
            state = "previous_conversation"

    if reading is None:
        new_tokens = await _messages_since(db, conversation.id, None) if conversation is not None else 0
        return ContextReport(state="none", detail=detail, new_message_tokens=new_tokens)

    usage = reading.usage
    overhead_only = state == "previous_conversation"
    categories = _categories(usage, reading.detail, overhead_only=overhead_only)
    estimate = sum(categories.totals.values())

    used = estimate
    cached: int | None = None
    if state == "measured":
        used = reading.meta["context_provider_tokens"]
        cached_raw = reading.meta.get("context_cached_tokens")
        cached = cached_raw if isinstance(cached_raw, int) and not isinstance(cached_raw, bool) else None
    lines = _scaled(categories, used)
    if state == "measured" and used > estimate:
        lines.append(ContextCategory(key="unattributed", tokens=used - estimate))

    if conversation is None:
        new_tokens = 0
    elif overhead_only:
        new_tokens = await _messages_since(db, conversation.id, None)
    else:
        new_tokens = await _messages_since(db, conversation.id, reading.sequence)

    compaction: ContextCompaction | None = None
    figures = _compaction_figures((reading.detail or {}).get("compaction"))
    if figures is not None:
        if overhead_only:
            # The other conversation's messages are not this one's: the gate
            # starts from the system prompt.
            base = sum(line.tokens for line in lines if line.key in ("system_prompt", "memory", "knowledge", "skills"))
        else:
            base = figures["gate_tokens"]
        gate = base + new_tokens
        level = usage.level or CONTEXT_LEVEL_NORMAL
        if overhead_only:
            level = CONTEXT_LEVEL_NORMAL
        if level == CONTEXT_LEVEL_NORMAL and gate >= figures["approaching_tokens"]:
            level = CONTEXT_LEVEL_APPROACHING
        compaction = ContextCompaction(
            level=level,
            gate_tokens=gate,
            approaching_tokens=figures["approaching_tokens"],
            trigger_tokens=figures["trigger_tokens"],
            target_tokens=figures["target_tokens"],
            free_tokens=max(0, figures["trigger_tokens"] - gate),
        )

    model = reading.meta.get("context_model")
    return ContextReport(
        state=state,
        detail=detail,
        model=model if isinstance(model, str) and model else None,
        window=usage.window,
        used_tokens=used,
        cached_tokens=cached,
        new_message_tokens=new_tokens,
        compaction=compaction,
        agent_changed=bool(agent is not None and agent.updated_at and agent.updated_at > reading.created_at),
        categories=[
            line if detail else line.model_copy(update={"items": []}) for line in lines
        ],
    )
