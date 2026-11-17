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

"""A conversation's history belongs to the conversation, not to its agent.

The agent of a conversation changes: the user continues it with another agent
(``PUT /chat/conversations/{id}/agent``), a deletion hands it to a successor,
a transfer moves it. Whoever answers next reads the whole history, and must
read the earlier turns of another agent as that agent's words, not its own.
This module is the ONE owner of:

* who wrote an assistant message: :data:`AUTHOR_AGENT_KEY`, stamped on every
  assistant message by ``channels.router.store_message`` (:func:`stamp_author`);
* how an earlier turn of another agent is read back (:func:`history_labels`,
  called by ``get_conversation_history``): ``[Reply from <agent>]``, the label
  the @mention replies already carry (``mention_dispatch.attributed_reply``);
* the names of agents, deleted ones included (:func:`agent_labels`: the
  ``agents`` row, else its ``agent_tombstones`` row);
* continuing a conversation with another agent (:func:`continue_with_agent`):
  the rebind and its note "Continued by <agent> on <date>";
* what an agent deletion leaves (:func:`record_agent_deletion`, called by
  ``agent_deletion`` in the deletion's transaction): the tombstone, the
  conversations it held marked ``deleted_agent_id``, and those of the owners
  who can use the successor handed to it, each with its note.

A conversation that never changed agent reads exactly as before: every
message is its current agent's, nothing is labelled.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, func, insert, literal, or_, select, update

from app.core.utils import safe_uuid

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.models.chat import Conversation, Message

logger = logging.getLogger(__name__)

#: Message metadata naming the agent that wrote an assistant message; ``None``
#: (JSON null) is the General Assistant.
AUTHOR_AGENT_KEY = "author_agent_id"
#: Message metadata of the note written when a conversation changes agent:
#: ``{"from": {...} | None, "to": {...}, "reason": ...}``, each side
#: ``{"agent_id", "agent_name"}`` or ``{"general_assistant": true}``.
AGENT_SWITCH_KEY = "agent_switch"
#: Why a conversation changed agent, as the note records it.
SWITCH_BY_USER = "continued"
SWITCH_BY_DELETION = "agent_deleted"

GENERAL_ASSISTANT_NAME = "General Assistant"
#: Characters of an agent name a label keeps (``Agent.name`` holds 200).
_LABEL_NAME_CHARS = 80
#: Conversations whose notes one statement writes, when a deletion hands
#: them to a successor (bounded well below the driver's parameter limit).
_NOTE_BATCH = 2_000


@dataclass(frozen=True)
class AgentLabel:
    """An agent as a reader names it: its name, its icon, and whether it was deleted."""

    name: str
    icon: str | None = None
    deleted: bool = False


def stamp_author(metadata_json: str | None, agent_id: uuid.UUID | None) -> str | None:
    """*metadata_json* naming the agent that wrote the assistant message.

    Left as it is when it already names who wrote it - a reply of an @mention
    turn (``answered_by``), a note of this module, an explicit stamp - and when
    it is not a JSON object (never rewritten into something it was not).
    """
    from app.services.mention_dispatch import ANSWERED_BY_KEY, MENTION_FAILED_KEY

    if metadata_json:
        try:
            meta = json.loads(metadata_json)
        except (TypeError, ValueError):
            return metadata_json
        if not isinstance(meta, dict):
            return metadata_json
    else:
        meta = {}
    if {ANSWERED_BY_KEY, MENTION_FAILED_KEY, AGENT_SWITCH_KEY, AUTHOR_AGENT_KEY} & meta.keys():
        return metadata_json
    meta[AUTHOR_AGENT_KEY] = str(agent_id) if agent_id else None
    return json.dumps(meta)


def message_author(meta: Mapping[str, Any]) -> tuple[bool, uuid.UUID | None]:
    """``(known, agent_id)`` of an assistant message from its metadata alone.

    ``agent_id`` ``None`` with ``known`` is the General Assistant; an @mention
    reply names its agent in ``answered_by``. ``known`` False: a message
    stored before authors were recorded.
    """
    from app.services.mention_dispatch import ANSWERED_BY_KEY

    attribution = meta.get(ANSWERED_BY_KEY)
    if isinstance(attribution, dict) and attribution.get("agent_id"):
        return True, safe_uuid(str(attribution["agent_id"]))
    if AUTHOR_AGENT_KEY in meta:
        raw = meta.get(AUTHOR_AGENT_KEY)
        return True, safe_uuid(str(raw)) if raw else None
    return False, None


async def agent_labels(db: AsyncSession, agent_ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, AgentLabel]:
    """The name of each agent of *agent_ids*: its row, else its tombstone.

    An id with neither (an agent deleted before tombstones were written) is
    left out; the caller words it.
    """
    from app.models.agent import Agent
    from app.models.agent_tombstone import AgentTombstone

    wanted = {a for a in agent_ids if a is not None}
    if not wanted:
        return {}
    labels: dict[uuid.UUID, AgentLabel] = {
        row.id: AgentLabel(name=row.name, icon=row.icon)
        for row in (await db.execute(select(Agent.id, Agent.name, Agent.icon).where(Agent.id.in_(wanted)))).all()
    }
    gone = wanted - labels.keys()
    if gone:
        for row in (
            await db.execute(
                select(AgentTombstone.agent_id, AgentTombstone.name, AgentTombstone.icon).where(
                    AgentTombstone.agent_id.in_(gone)
                )
            )
        ).all():
            labels[row.agent_id] = AgentLabel(name=row.name, icon=row.icon, deleted=True)
    return labels


def label_name(agent_id: uuid.UUID | None, labels: Mapping[uuid.UUID, AgentLabel], *, known: bool = True) -> str:
    """How an author is named in a prompt: one safe line, the General Assistant's
    name for ``None``, a neutral phrase for an agent nothing names any more."""
    from app.services.content_safety import prompt_label

    if not known:
        return "an earlier agent"
    if agent_id is None:
        return GENERAL_ASSISTANT_NAME
    label = labels.get(agent_id)
    if label is None:
        return "an agent since deleted"
    return prompt_label(label.name, limit=_LABEL_NAME_CHARS, fallback="Agent")


async def history_labels(
    db: AsyncSession,
    messages: Sequence[Message],
    metas: Mapping[uuid.UUID, Mapping[str, Any]],
    *,
    answering_agent_id: uuid.UUID | None,
    deleted_agent_id: uuid.UUID | None,
) -> dict[uuid.UUID, str]:
    """How the assistant messages of *messages* another agent wrote are read back.

    The agent answering now (*answering_agent_id*, the conversation's own;
    ``None`` for the General Assistant) reads a message stamped with another
    author as ``[Reply from <name>]``, and a note of a change of agent as a
    ``[Note]``. A message stored before authors were stamped is attributed
    through the trace of the run that wrote it, but only in a conversation
    that changed agent - a note in view, or a deleted agent (*deleted_agent_id*)
    - so a conversation that never changed agent costs no query and reads as
    before. The @mention replies keep the label ``mention_dispatch`` gives them;
    *metas* holds each message's parsed metadata.
    """
    from app.services.mention_dispatch import (
        ANSWERED_BY_KEY,
        MENTION_FAILED_KEY,
        attributed_reply,
        mention_note,
    )

    labels: dict[uuid.UUID, str] = {}
    authors: dict[uuid.UUID, uuid.UUID | None] = {}
    legacy: list[Message] = []
    changed = deleted_agent_id is not None
    for msg in messages:
        if msg.role != "assistant":
            continue
        meta = metas.get(msg.id) or {}
        if AGENT_SWITCH_KEY in meta:
            labels[msg.id] = mention_note(None, msg.content or "")
            changed = True
        elif ANSWERED_BY_KEY in meta or MENTION_FAILED_KEY in meta:
            continue
        elif AUTHOR_AGENT_KEY in meta:
            authors[msg.id] = message_author(meta)[1]
        else:
            legacy.append(msg)
    traced = await authors_from_traces(db, legacy) if legacy and changed else {}
    authors.update({msg_id: author.agent_id for msg_id, author in traced.items()})
    others = {msg_id: author for msg_id, author in authors.items() if author != answering_agent_id}
    if not others:
        return labels
    names = with_traced_names(await agent_labels(db, others.values()), traced.values())
    by_id = {msg.id: msg for msg in messages}
    for msg_id, author in others.items():
        labels[msg_id] = attributed_reply(label_name(author, names), by_id[msg_id].content or "")
    return labels


@dataclass(frozen=True)
class TracedAuthor:
    """The agent of the run that wrote a message, as its trace recorded it."""

    agent_id: uuid.UUID | None
    #: The agent's name when the trace was written (``agent_name_snapshot``).
    agent_name: str | None


async def authors_from_traces(db: AsyncSession, messages: Sequence[Message]) -> dict[uuid.UUID, TracedAuthor]:
    """Who wrote each of *messages* stored before authors were stamped, by its
    run's trace, while the trace is kept: what attributes the turns of an agent
    deleted before tombstones were written."""
    from app.models.llm_trace import LLMTrace

    by_trace = {msg.llm_trace_id: msg.id for msg in messages if msg.llm_trace_id}
    if not by_trace:
        return {}
    try:
        rows = (
            await db.execute(
                select(LLMTrace.id, LLMTrace.agent_id, LLMTrace.agent_name_snapshot).where(
                    LLMTrace.id.in_(by_trace)
                )
            )
        ).all()
    except Exception:
        # A label is an aid: a read that fails leaves the messages unlabelled,
        # as they always were, never the turn without its history.
        logger.warning("Could not read the traces attributing %d message(s)", len(by_trace), exc_info=True)
        return {}
    return {by_trace[row.id]: TracedAuthor(row.agent_id, row.agent_name_snapshot) for row in rows}


def with_traced_names(
    labels: dict[uuid.UUID, AgentLabel], traced: Iterable[TracedAuthor]
) -> dict[uuid.UUID, AgentLabel]:
    """*labels* completed by the names traces recorded for agents nothing else
    names any more: deleted, before tombstones were written."""
    for author in traced:
        if author.agent_id is not None and author.agent_id not in labels and author.agent_name:
            labels[author.agent_id] = AgentLabel(name=author.agent_name, deleted=True)
    return labels


# ── Continuing a conversation with another agent ──────────────────────────


def _side(agent: Agent | None) -> dict:
    if agent is not None:
        return {"agent_id": str(agent.id), "agent_name": agent.name}
    return {"general_assistant": True}


def switch_note(to_name: str, when: datetime) -> str:
    """The note's text, as the transcript, an export and the next agent read it."""
    return f"Continued by {to_name} on {when:%Y-%m-%d}."


def _note_metadata(previous: dict | None, target: dict, reason: str, when: datetime) -> str:
    return json.dumps(
        {AGENT_SWITCH_KEY: {"from": previous, "to": target, "reason": reason, "at": when.isoformat()}}
    )


async def continue_with_agent(
    db: AsyncSession,
    conversation: Conversation,
    agent: Agent | None,
    *,
    when: datetime | None = None,
) -> Message | None:
    """Continue *conversation* with *agent* (``None``: the General Assistant), in place.

    The conversation keeps every message; the agent answering from the next
    turn is *agent*, which reads the earlier turns labelled with who wrote
    them. A note "Continued by <agent> on <date>" marks the change in the
    transcript (:data:`AGENT_SWITCH_KEY`). A transfer the conversation was in
    ends with it (``transfer_source_agent_id``). Who may do it is the caller's
    rule (the route: the owner, an agent they may chat with). Returns the note,
    or ``None`` when *agent* already holds the conversation.
    """
    from app.channels.router import store_message

    target_id = agent.id if agent is not None else None
    if conversation.agent_id == target_id:
        return None
    when = when or datetime.now(UTC)
    previous: dict | None = None
    held_by = conversation.agent_id or conversation.deleted_agent_id
    if held_by is not None:
        label = (await agent_labels(db, [held_by])).get(held_by)
        previous = {"agent_id": str(held_by), "agent_name": label.name if label else None}
        if conversation.agent_id is None:
            previous["deleted"] = True
    # Otherwise nothing records who held it: a General Assistant
    # conversation, or one whose agent was deleted before deletions were
    # recorded. The note then names only who continues it.
    target = _side(agent)
    conversation.agent_id = target_id
    conversation.transfer_source_agent_id = None
    await db.flush()
    return await store_message(
        db,
        conversation.id,
        role="assistant",
        content=switch_note(agent.name if agent is not None else GENERAL_ASSISTANT_NAME, when),
        channel="web",
        metadata_json=_note_metadata(previous, target, SWITCH_BY_USER, when),
    )


# ── Agent deletion ────────────────────────────────────────────────────────


async def successor_refusal(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    agent_id: uuid.UUID,
    successor_id: uuid.UUID,
) -> str | None:
    """Why *successor_id* cannot take the conversations of agent *agent_id*, or ``None``.

    The successor is an agent the person deleting can use themselves - their
    three tiers, never an admin bypass - and that answers chats: enabled.
    Which of the conversations it then takes is decided per owner
    (:func:`record_agent_deletion`).
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent

    if successor_id == agent_id:
        return "An agent cannot be its own successor: choose another agent, or none."
    group_ids = await get_user_group_ids(db, actor_id)
    successor = (
        await db.execute(
            select(Agent).where(Agent.id == successor_id, user_visible_filter(Agent, actor_id, group_ids))
        )
    ).scalar_one_or_none()
    if successor is None:
        return f"Successor agent `{successor_id}` not found, or it is not an agent you can use."
    if not successor.enabled:
        return f"The successor agent **{successor.name}** is disabled: enable it first, or choose another one."
    return None


async def record_agent_deletion(
    db: AsyncSession,
    agent: Agent,
    *,
    actor_id: uuid.UUID | None,
    successor: Agent | None,
) -> int:
    """What deleting *agent* leaves for its work, in the deletion's transaction.

    Writes its tombstone, marks every conversation it held (``agent_id`` or
    ``original_agent_id``, both about to be ``SET NULL``) with
    ``deleted_agent_id``, and hands to *successor* the web conversations whose
    owner can use it (``visible_to_user_column_clause``: their own tiers,
    row by row), each with its note. A conversation whose owner cannot use the
    successor stays with nobody, for the owner to continue with an agent of
    theirs. Nothing is copied or moved: the messages stay where they are, and
    ``updated_at`` too, so the sidebar's order does not change. Called right
    before the agent row goes, where the ``SET NULL`` cascade would lock the
    same conversation rows. Returns how many conversations the successor took.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.api.v1.helpers import visible_to_user_column_clause
    from app.models.agent import Agent
    from app.models.agent_tombstone import AgentTombstone
    from app.models.chat import Conversation, Message

    now = datetime.now(UTC)
    values = {
        "name": agent.name,
        "slug": agent.slug,
        "icon": agent.icon,
        "user_id": agent.user_id,
        "is_company_managed": bool(agent.is_company_managed),
        "successor_agent_id": successor.id if successor is not None else None,
        "deleted_by": actor_id,
        "deleted_at": now,
    }
    await db.execute(
        pg_insert(AgentTombstone)
        .values(agent_id=agent.id, **values)
        .on_conflict_do_update(index_elements=[AgentTombstone.agent_id], set_=values)
    )
    await db.execute(
        update(Conversation)
        .where(or_(Conversation.agent_id == agent.id, Conversation.original_agent_id == agent.id))
        .values(deleted_agent_id=agent.id, updated_at=Conversation.updated_at)
    )
    if successor is None:
        return 0
    rebound = list(
        (
            await db.execute(
                update(Conversation)
                .where(
                    Conversation.agent_id == agent.id,
                    Conversation.channel == "web",
                    Conversation.user_id.is_not(None),
                    select(Agent.id)
                    .where(Agent.id == successor.id, visible_to_user_column_clause(Agent, Conversation.user_id))
                    .exists(),
                )
                .values(agent_id=successor.id, transfer_source_agent_id=None, updated_at=Conversation.updated_at)
                .returning(Conversation.id)
            )
        )
        .scalars()
        .all()
    )
    if not rebound:
        return 0
    meta = _note_metadata(
        {"agent_id": str(agent.id), "agent_name": agent.name, "deleted": True},
        _side(successor),
        SWITCH_BY_DELETION,
        now,
    )
    content = switch_note(successor.name, now)
    # Each conversation's row lock is this transaction's since the UPDATE
    # above, the lock ``store_message`` allocates a sequence under.
    next_sequence = (
        select(func.coalesce(func.max(Message.sequence), 0) + 1)
        .where(Message.conversation_id == Conversation.id)
        .scalar_subquery()
    )
    for start in range(0, len(rebound), _NOTE_BATCH):
        await db.execute(
            insert(Message).from_select(
                ["id", "conversation_id", "role", "content", "channel", "metadata_json", "sequence", "created_at"],
                select(
                    func.gen_random_uuid(),
                    Conversation.id,
                    literal("assistant"),
                    literal(content),
                    literal("web"),
                    literal(meta),
                    next_sequence,
                    literal(now, DateTime(timezone=True)),
                ).where(Conversation.id.in_(rebound[start : start + _NOTE_BATCH])),
            )
        )
    return len(rebound)


def deleted_agent_owner_clause(agent_id_column: Any, user_id: uuid.UUID):
    """``agent_id_column`` names an agent *user_id* owned when it was deleted.

    What keeps a deleted agent's traces readable by its former owner
    (``tracing.read_store``), next to the trace's own user.
    """
    from app.models.agent_tombstone import AgentTombstone

    return agent_id_column.in_(select(AgentTombstone.agent_id).where(AgentTombstone.user_id == user_id))
