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

"""Agent chat endpoints – list and inspect all conversations for an agent.

All channel conversations (web, Slack, Teams, WhatsApp, Telegram, Discord, Google Chat,
web_embed) scoped to an agent are surfaced here.  These endpoints power the **Chats**
tab on the agent detail page.

Access control:
- All authenticated users can see stats (counts) for agents they can view.
- Full conversation content follows ``helpers.can_view_agent_chats``: an admin,
  the agent's owner, or a ``config_with_chats`` grantee (group or direct) of a
  personal agent.
"""

import json
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1 import platform as platform_api
from app.api.v1.chat import MAX_MESSAGE_PAGE_SIZE
from app.api.v1.helpers import (
    can_manage,
    can_view_agent_chats,
    get_user_agent,
    get_user_group_ids,
    parse_conversation_id,
)
from app.channels.base import strip_artifact_markers
from app.core.database import get_db
from app.models.chat import Conversation, Message, MessageFeedback
from app.models.user import User
from app.services.audit import log_audit

logger = logging.getLogger(__name__)
router = APIRouter()

# Max characters shown in the message preview column of the chat list.
MESSAGE_PREVIEW_LENGTH = 150


def _parse_msg_attachments(
    metadata_json: str | None,
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
) -> list[dict] | None:
    """Extract attachment metadata from a message's metadata_json field.

    Defensive against malformed metadata: if the JSON parses to a non-dict
    (e.g. ``json.loads("[]")`` → ``[]``), if ``meta["attachments"]`` is not
    a list, or if individual entries are not dicts, those values are
    silently skipped instead of letting the broad ``except`` below drop
    *all* attachments — that mode used to surface as an empty Chats tab
    every time a single legacy or hand-crafted metadata row was malformed.
    Matches the gold-standard pattern in ``backend/app/channels/router.py``
    (the two ``isinstance(meta, dict)`` / ``isinstance(attachments, list)``
    guards there) so the chat-list and channel-router decode paths agree
    on what a "well-formed attachments block" looks like.
    """
    if not metadata_json:
        return None
    try:
        meta = json.loads(metadata_json)
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    attachments = meta.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return None
    # Skip non-dict entries (string, int, None) so one bad row inside an
    # otherwise valid list cannot drop every attachment from the response.
    valid: list[dict] = [att for att in attachments if isinstance(att, dict)]
    if not valid:
        return None
    for att in valid:
        file_id = att.get("file_id")
        if file_id and conversation_id and message_id:
            att["download_url"] = (
                f"/api/v1/chat/conversations/{conversation_id}/messages/"
                f"{message_id}/files/{file_id}/download"
            )
    return valid


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChannelBreakdown(BaseModel):
    channel: str
    count: int


class AgentChatStats(BaseModel):
    total_conversations: int
    total_messages: int
    channels: list[ChannelBreakdown]
    positive_feedback: int = 0
    negative_feedback: int = 0
    positive_feedback_ratio: float | None = None


class AgentChatItem(BaseModel):
    id: str
    channel: str
    title: str
    user_name: str | None = None
    message_count: int
    last_message_preview: str | None = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class AgentChatListResponse(BaseModel):
    items: list[AgentChatItem]
    total: int
    stats: AgentChatStats


class AgentChatMessage(BaseModel):
    id: str
    role: str
    content: str
    channel: str
    sequence: int
    created_at: str
    attachments: list[dict] | None = None


class AgentChatDetailResponse(BaseModel):
    id: str
    channel: str
    title: str
    user_name: str | None = None
    message_count: int
    created_at: str
    updated_at: str
    messages: list[AgentChatMessage]
    has_more: bool = False


class AgentFeedbackCommentItem(BaseModel):
    id: str
    message_id: str
    conversation_id: str
    channel: str
    user_name: str | None = None
    user_email: str | None = None
    # Mirrors the DB check constraint (ck_message_feedback_rating_valid) and
    # the query-param validation so the OpenAPI contract matches the
    # frontend's strict "positive" | "negative" union.
    rating: Literal["positive", "negative"]
    comment: str
    created_at: str
    updated_at: str


class AgentFeedbackCommentListResponse(BaseModel):
    items: list[AgentFeedbackCommentItem]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{agent_id}/chats", response_model=AgentChatListResponse)
async def list_agent_chats(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query("", max_length=200),
    channel: str | None = Query(None, max_length=30),
):
    """List all conversations for an agent (web, external channels, embedded).

    Only a viewer :func:`can_view_agent_chats` admits sees the conversation
    list; anyone else who can see the agent gets the stats with an empty list.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(
        db, user.id, agent_id, user_group_ids, is_admin=user.is_admin
    )

    is_restricted = not can_view_agent_chats(user, agent, user_group_ids)

    agent_uuid = agent.id

    # --- Stats (always returned, even for restricted users) ---
    # Combine the four scalar counts into a single DB round-trip via scalar
    # subqueries, then fetch the per-channel breakdown in a second query
    # (it returns multiple rows and cannot be collapsed into the same SELECT).
    _conv_filter = (
        Conversation.agent_id == agent_uuid,
        Conversation.is_archived.is_(False),
    )
    _total_conv_sq = select(func.count()).where(*_conv_filter).scalar_subquery()
    _total_msg_sq = (
        select(func.count())
        .select_from(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*_conv_filter)
        .scalar_subquery()
    )
    _pos_fb_sq = (
        select(func.count())
        .select_from(MessageFeedback)
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*_conv_filter, MessageFeedback.rating == "positive")
        .scalar_subquery()
    )
    _neg_fb_sq = (
        select(func.count())
        .select_from(MessageFeedback)
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*_conv_filter, MessageFeedback.rating == "negative")
        .scalar_subquery()
    )
    stats_row = (
        await db.execute(
            select(
                _total_conv_sq.label("total_conversations"),
                _total_msg_sq.label("total_messages"),
                _pos_fb_sq.label("positive_feedback"),
                _neg_fb_sq.label("negative_feedback"),
            )
        )
    ).one()
    total_conversations = stats_row.total_conversations or 0
    total_messages = stats_row.total_messages or 0
    positive_feedback = stats_row.positive_feedback or 0
    negative_feedback = stats_row.negative_feedback or 0

    channel_q = (
        select(Conversation.channel, func.count())
        .where(*_conv_filter)
        .group_by(Conversation.channel)
    )
    channel_rows = (await db.execute(channel_q)).all()
    channels = [ChannelBreakdown(channel=row[0], count=row[1]) for row in channel_rows]

    total_feedback = positive_feedback + negative_feedback
    stats = AgentChatStats(
        total_conversations=total_conversations,
        total_messages=total_messages,
        channels=channels,
        positive_feedback=positive_feedback,
        negative_feedback=negative_feedback,
        positive_feedback_ratio=(
            round((positive_feedback / total_feedback) * 100, 1)
            if total_feedback > 0
            else None
        ),
    )

    if is_restricted:
        return AgentChatListResponse(items=[], total=total_conversations, stats=stats)

    # --- Conversation list (with optional search and channel filter) ---
    list_q = (
        select(Conversation)
        .where(
            Conversation.agent_id == agent_uuid,
            Conversation.is_archived.is_(False),
        )
        .order_by(Conversation.updated_at.desc())
    )

    if channel:
        list_q = list_q.where(Conversation.channel == channel)

    if search.strip():
        list_q = list_q.where(Conversation.title.ilike(f"%{search.strip()}%"))

    # Total for pagination (after filters)
    count_q = select(func.count()).select_from(list_q.subquery())
    filtered_total = await db.scalar(count_q) or 0

    list_q = list_q.offset(offset).limit(limit)
    result = await db.execute(list_q)
    convs = result.scalars().all()

    # Pre-fetch user names for display
    user_ids = {c.user_id for c in convs if c.user_id}
    user_map: dict[uuid.UUID, str] = {}
    if user_ids:
        user_result = await db.execute(
            select(User.id, User.full_name).where(User.id.in_(user_ids))
        )
        for row in user_result:
            user_map[row.id] = row.full_name

    # Batch-fetch message counts and last messages to avoid N+1 queries
    conv_ids = [c.id for c in convs]

    msg_count_map: dict[uuid.UUID, int] = {}
    last_preview_map: dict[uuid.UUID, str | None] = {}

    if conv_ids:
        # Message counts grouped by conversation
        count_result = await db.execute(
            select(Message.conversation_id, func.count())
            .where(Message.conversation_id.in_(conv_ids))
            .group_by(Message.conversation_id)
        )
        for cid, cnt in count_result:
            msg_count_map[cid] = cnt

        # Last user/assistant message per conversation via window function
        row_num = (
            func.row_number()
            .over(
                partition_by=Message.conversation_id,
                order_by=Message.sequence.desc(),
            )
            .label("rn")
        )
        sub = (
            select(Message.conversation_id, Message.content, row_num)
            .where(
                Message.conversation_id.in_(conv_ids),
                Message.role.in_(["user", "assistant"]),
            )
            .subquery()
        )
        last_msg_result = await db.execute(
            select(sub.c.conversation_id, sub.c.content).where(sub.c.rn == 1)
        )
        for cid, content in last_msg_result:
            # Strip render markers before the cut (see strip_artifact_markers).
            content = strip_artifact_markers(content or "")
            last_preview_map[cid] = (
                (content[:MESSAGE_PREVIEW_LENGTH] + "...")
                if len(content) > MESSAGE_PREVIEW_LENGTH
                else content
            )

    items = []
    for c in convs:
        items.append(
            AgentChatItem(
                id=str(c.id),
                channel=c.channel,
                title=c.title,
                user_name=user_map.get(c.user_id) if c.user_id else None,
                message_count=msg_count_map.get(c.id, 0),
                last_message_preview=last_preview_map.get(c.id),
                created_at=c.created_at.isoformat(),
                updated_at=c.updated_at.isoformat(),
            )
        )

    return AgentChatListResponse(items=items, total=filtered_total, stats=stats)


async def _viewable_agent_conversation(
    db: AsyncSession, user: User, agent_id: str, conversation_id: str
) -> Conversation:
    """The agent's conversation *conversation_id*, when :func:`can_view_agent_chats` admits *user*.

    403 when the viewer may not read the agent's chats, 404 when the agent has
    no such conversation.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(
        db, user.id, agent_id, user_group_ids, is_admin=user.is_admin
    )

    if not can_view_agent_chats(user, agent, user_group_ids):
        raise HTTPException(
            status_code=403,
            detail="Chat content requires owner access or config_with_chats permission.",
        )

    result = await db.execute(
        select(Conversation).where(
            Conversation.id == parse_conversation_id(conversation_id),
            Conversation.agent_id == agent.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


class AgentChatPanelSessionRequest(BaseModel):
    """The panel's restore call."""

    conversation_id: str | None = None
    supports_artifacts: bool = False


@router.post("/{agent_id}/chats/sessions")
async def restore_agent_chat_panel_session(
    agent_id: str,
    body: AgentChatPanelSessionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The latest messages of one of the agent's conversations, as the chat panel restores a session.

    The agent detail's read-only view: the access rule of
    :func:`get_agent_chat`, and never a new conversation.
    """
    if not body.conversation_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv = await _viewable_agent_conversation(db, user, agent_id, body.conversation_id)
    # XTM One's own panel draws a dashboard's marker as the dashboard: kept, not made a link.
    restored, has_more = await platform_api.restore_chat_session_window(
        db, user, conv, supports_artifacts=body.supports_artifacts, keep_dashboards=True
    )
    return platform_api.ChatPanelSessionResponse(**restored.model_dump(), has_more=has_more)


class AgentChatPanelAgentOut(BaseModel):
    """The agent as the chat panel's agent list carries it."""

    id: str
    name: str
    slug: str | None = None
    icon: str | None = None


@router.get("/{agent_id}/chats/panel-agents", response_model=list[AgentChatPanelAgentOut])
async def list_agent_chat_panel_agents(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AgentChatPanelAgentOut]:
    """The agent itself, as the read-only panel's one-entry agent list.

    So an empty conversation read from the agent's page shows the agent's own
    mark and name, as ``/chat`` does, instead of a generic greeting. The
    access rule of :func:`_viewable_agent_conversation`. Declared before
    ``/{agent_id}/chats/{conversation_id}``, which would otherwise take the path.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, agent_id, user_group_ids, is_admin=user.is_admin)
    if not can_view_agent_chats(user, agent, user_group_ids):
        raise HTTPException(
            status_code=403,
            detail="Chat content requires owner access or config_with_chats permission.",
        )
    return [AgentChatPanelAgentOut(id=str(agent.id), name=agent.name, slug=agent.slug, icon=agent.icon)]


@router.get("/{agent_id}/chats/conversations/{conversation_id}/messages")
async def list_agent_chat_panel_messages(
    agent_id: str,
    conversation_id: str,
    before: str | None = Query(default=None, description="Messages strictly older than this message id."),
    limit: int = Query(default=50, ge=1, le=MAX_MESSAGE_PAGE_SIZE),
    supports_artifacts: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One page of one of the agent's conversations, older than ``before``, as the chat panel pages a thread."""
    conv = await _viewable_agent_conversation(db, user, agent_id, conversation_id)
    return await platform_api.read_message_page(
        db, user, conv, before=before, limit=limit, supports_artifacts=supports_artifacts, keep_dashboards=True
    )


@router.get(
    "/{agent_id}/chats/{conversation_id}", response_model=AgentChatDetailResponse
)
async def get_agent_chat(
    agent_id: str,
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    before_sequence: int | None = Query(None, ge=0),
):
    """Get conversation detail with paginated messages (newest first).

    Returns up to ``limit`` messages ordered by sequence descending.
    Pass ``before_sequence`` to load older messages (cursor-based pagination).
    The response includes ``has_more`` to indicate if more messages exist.

    Refused (403) unless :func:`can_view_agent_chats` admits the viewer.
    """
    conv = await _viewable_agent_conversation(db, user, agent_id, conversation_id)
    conv_uuid = conv.id

    # Resolve user name
    user_name = None
    if conv.user_id:
        user_result = await db.execute(
            select(User.full_name).where(User.id == conv.user_id)
        )
        user_name = user_result.scalar_one_or_none()

    # Total visible message count
    msg_count = (
        await db.scalar(
            select(func.count()).where(
                Message.conversation_id == conv_uuid,
                Message.role.in_(["user", "assistant"]),
            )
        )
        or 0
    )

    # Paginated messages (newest first)
    msg_q = (
        select(Message)
        .where(
            Message.conversation_id == conv_uuid,
            Message.role.in_(["user", "assistant", "system"]),
        )
        .order_by(Message.sequence.desc())
    )
    if before_sequence is not None:
        msg_q = msg_q.where(Message.sequence < before_sequence)

    # Fetch limit + 1 to determine has_more
    msg_q = msg_q.limit(limit + 1)
    msg_result = await db.execute(msg_q)
    messages_raw = list(msg_result.scalars().all())

    has_more = len(messages_raw) > limit
    if has_more:
        messages_raw = messages_raw[:limit]

    # Reverse so messages are returned in chronological order (oldest first within the page)
    messages_raw.reverse()

    return AgentChatDetailResponse(
        id=str(conv.id),
        channel=conv.channel,
        title=conv.title,
        user_name=user_name,
        message_count=msg_count,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        has_more=has_more,
        messages=[
            AgentChatMessage(
                id=str(m.id),
                role=m.role,
                content=m.content,
                channel=m.channel,
                sequence=m.sequence,
                created_at=m.created_at.isoformat(),
                attachments=_parse_msg_attachments(
                    m.metadata_json,
                    conversation_id=str(conv.id),
                    message_id=str(m.id),
                ),
            )
            for m in messages_raw
        ],
    )


@router.get(
    "/{agent_id}/feedback-comments",
    response_model=AgentFeedbackCommentListResponse,
)
async def list_agent_feedback_comments(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query("", max_length=200),
    rating: str | None = Query(None, pattern="^(positive|negative)$"),
):
    """List user feedback comments for one agent.

    Only non-empty comments are returned so the UI list focuses on actionable
    qualitative feedback.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(
        db, user.id, agent_id, user_group_ids, is_admin=user.is_admin
    )

    from app.api.v1.helpers import can_manage

    # Aligned with the agent Observability gate: owner, admin, or a group
    # member with config permission may read qualitative feedback (the same
    # audience that already sees chat content through agent traces).
    if not can_manage(user, agent, user_group_ids):
        raise HTTPException(
            status_code=403,
            detail="Feedback comments require agent owner, admin, or config group permission.",
        )

    base_q = (
        select(
            MessageFeedback,
            Message.id.label("message_id"),
            Conversation.id.label("conversation_id"),
            Conversation.channel.label("channel"),
            User.full_name.label("user_name"),
            User.email.label("user_email"),
        )
        .join(Message, MessageFeedback.message_id == Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .outerjoin(User, MessageFeedback.user_id == User.id)
        .where(
            # agent.id comes from get_user_agent() — no need to re-parse the
            # raw path parameter (a non-UUID would raise ValueError -> 500).
            Conversation.agent_id == agent.id,
            MessageFeedback.comment.is_not(None),
            func.length(func.trim(MessageFeedback.comment)) > 0,
        )
    )

    if rating:
        base_q = base_q.where(MessageFeedback.rating == rating)
    if search.strip():
        base_q = base_q.where(MessageFeedback.comment.ilike(f"%{search.strip()}%"))

    count_q = select(func.count()).select_from(base_q.subquery())
    total = int((await db.scalar(count_q)) or 0)

    rows = (
        await db.execute(
            base_q
            .order_by(MessageFeedback.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    items = [
        AgentFeedbackCommentItem(
            id=str(row[0].id),
            message_id=str(row.message_id),
            conversation_id=str(row.conversation_id),
            channel=row.channel,
            user_name=row.user_name,
            user_email=row.user_email,
            rating=row[0].rating,
            comment=row[0].comment or "",
            created_at=row[0].created_at.isoformat(),
            updated_at=row[0].updated_at.isoformat(),
        )
        for row in rows
    ]
    return AgentFeedbackCommentListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete("/{agent_id}/chats", status_code=status.HTTP_204_NO_CONTENT)
async def clear_agent_chats(
    agent_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete all conversations for an agent.

    Only users who can manage the agent (owner or admin) may clear chats.
    Messages are cascade-deleted via the FK relationship.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(
        db, user.id, agent_id, user_group_ids, is_admin=user.is_admin
    )

    if not can_manage(user, agent, user_group_ids):
        raise HTTPException(
            status_code=403,
            detail="Only the agent owner or an administrator can clear chats.",
        )

    agent_uuid = agent.id

    # Delete all conversations for this agent (messages cascade-deleted via FK)
    result = await db.execute(
        delete(Conversation).where(
            Conversation.agent_id == agent_uuid,
        ).returning(Conversation.id)
    )
    deleted_ids = list(result.scalars().all())
    deleted_count = len(deleted_ids)
    # The audit row commits with the delete: neither lands without the other.
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="conversation",
        entity_id=agent_uuid,
        entity_name=agent.name,
        summary=f"Cleared {deleted_count} conversation(s) of agent '{agent.name}'",
        details={"deleted_count": deleted_count},
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    # Their sandbox workspaces go with them, off the request.
    from app.services.sandbox_workspace.gc import collect_conversations_soon

    collect_conversations_soon(deleted_ids, reason="conversation_deleted")

    logger.info(
        "Cleared %d conversation(s) for agent %s (user=%s)",
        deleted_count,
        agent_id,
        user.id,
    )
