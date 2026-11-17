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

"""Continue a conversation in Slack or Microsoft Teams.

Mounted under ``/api/v1/chat`` next to the conversation routes. Every route is
the conversation OWNER's, like sending in it: a share is read-only, and an id
the caller does not own is answered exactly like an unknown one (404).

``POST /conversations/{id}/channel-bindings`` posts an opening message in the
owner's direct messages with the bot and binds that thread to the
conversation; the router then continues the conversation from it. The rules
it applies live in ``services/channel_continuation.py``.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_chat_enabled
from app.api.v1.helpers import owned_conversation
from app.channels.base import CHANNEL_TO_BOT_PROVIDER, ChannelType, get_channel_bot
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.models.chat import Conversation
from app.models.user import User
from app.services.audit import log_audit
from app.services.branding import get_cached_branding
from app.services.channel_continuation import (
    CONTINUABLE_CHANNELS,
    REASON_AGENT_UNAVAILABLE,
    REASON_DIRECT_MESSAGES_OFF,
    REASON_NOT_CONFIGURED,
    REASON_NOT_CONTINUABLE,
    REASON_STARTED_THERE,
    REASON_TEAMS_CHAT_UNKNOWN,
    ChannelContinuationError,
    audit_released_threads,
    bind_thread,
    channel_label,
    continuable_on_web,
    conversation_link,
    list_bindings,
    not_continuable_detail,
    unbind_conversation,
)
from app.services.channel_notices import notice, notice_language

logger = logging.getLogger(__name__)

router = APIRouter()

ContinuableChannel = Literal["slack", "teams"]

#: Per user. Every continuation looks the user up and posts in their direct
#: messages through the deployment's one Slack or Teams bot, whose platform
#: rate limits every other user of the bot shares.
CHANNEL_BINDINGS_PER_MINUTE = 10


class ChannelBindingResponse(BaseModel):
    channel: str
    created_at: str
    updated_at: str


class ContinuationChannel(BaseModel):
    channel: str
    available: bool
    #: Why it is not available, as a code (``not_continuable``,
    #: ``not_configured``, ``direct_messages_off``, ``agent_unavailable``,
    #: ``teams_chat_unknown``, ``started_there``).
    reason: str | None = None
    detail: str | None = None


class ChannelBindingsResponse(BaseModel):
    bindings: list[ChannelBindingResponse]
    channels: list[ContinuationChannel]


class ChannelBindingCreate(BaseModel):
    channel: ContinuableChannel


def _binding_response(binding) -> ChannelBindingResponse:
    return ChannelBindingResponse(
        channel=binding.channel,
        created_at=binding.created_at.isoformat(),
        updated_at=binding.updated_at.isoformat(),
    )


async def _channel_availability(
    db: AsyncSession, conv: Conversation, user: User, channel: str
) -> ContinuationChannel:
    """Whether *conv* can be continued in *channel* right now, and why not."""
    label = channel_label(channel)
    if not continuable_on_web(conv):
        return ContinuationChannel(
            channel=channel,
            available=False,
            reason=REASON_NOT_CONTINUABLE,
            detail=not_continuable_detail(conv),
        )
    if await get_channel_bot(CHANNEL_TO_BOT_PROVIDER[channel]) is None:
        return ContinuationChannel(
            channel=channel,
            available=False,
            reason=REASON_NOT_CONFIGURED,
            detail=f"No {label} bot is connected. An administrator connects one in Settings > Integrations > Channels & Bots.",
        )
    # Asked of the router itself: the gates a message of the continued thread
    # meets, so the option is offered exactly where its replies are answered.
    from app.channels.router import direct_message_refusal

    refusal = await direct_message_refusal(
        db, ChannelType(channel), agent_id=conv.agent_id, user=user
    )
    if refusal == "bot":
        return ContinuationChannel(
            channel=channel,
            available=False,
            reason=REASON_DIRECT_MESSAGES_OFF,
            detail=(
                f"The {label} bot does not answer direct messages. An administrator "
                "changes it in Settings > Integrations > Channels & Bots."
            ),
        )
    if refusal is not None:
        return ContinuationChannel(
            channel=channel,
            available=False,
            reason=REASON_AGENT_UNAVAILABLE,
            detail=f"The agent of this conversation does not answer on {label}.",
        )
    if channel == "teams":
        from app.channels.teams import get_personal_conversation_reference

        reference = await get_personal_conversation_reference(user.email)
        if reference is None:
            return ContinuationChannel(
                channel=channel,
                available=False,
                reason=REASON_TEAMS_CHAT_UNKNOWN,
                detail="Send a message to the bot in Microsoft Teams once, then try again.",
            )
        if conv.channel == channel and conv.channel_thread_id == reference.get("conversation_id"):
            # The conversation IS that personal chat: a binding would point the
            # chat at itself, and /new there archives the conversation.
            return ContinuationChannel(
                channel=channel,
                available=False,
                reason=REASON_STARTED_THERE,
                detail="This conversation is your chat with the bot in Microsoft Teams: continue it there.",
            )
    return ContinuationChannel(channel=channel, available=True)


def _opening_text(conv: Conversation, channel: str, language: str | None = None) -> str:
    """The first message of the continued thread, in its owner's *language*."""
    return notice(
        "continuation_opening",
        language,
        title=conv.title,
        product=get_cached_branding().name,
        where=notice(
            "continuation_where_slack" if channel == "slack" else "continuation_where_teams",
            language,
        ),
        link=conversation_link(conv.id),
    )


@router.get(
    "/conversations/{conversation_id}/channel-bindings",
    response_model=ChannelBindingsResponse,
)
async def get_channel_bindings(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Where the conversation is continued, and where it can be."""
    conv = await owned_conversation(db, user, conversation_id, include_archived=False)
    bindings = await list_bindings(db, conv.id)
    channels = [
        await _channel_availability(db, conv, user, channel) for channel in CONTINUABLE_CHANNELS
    ]
    return ChannelBindingsResponse(
        bindings=[_binding_response(b) for b in bindings],
        channels=channels,
    )


@router.post(
    "/conversations/{conversation_id}/channel-bindings",
    response_model=ChannelBindingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_channel_binding(
    conversation_id: str,
    body: ChannelBindingCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Continue the conversation in the owner's direct messages with the bot."""
    await enforce_rate_limit(
        None,
        "chat-channel-binding",
        limit=CHANNEL_BINDINGS_PER_MINUTE,
        window_seconds=60,
        bucket=str(user.id),
    )
    conv = await owned_conversation(db, user, conversation_id, include_archived=False)
    availability = await _channel_availability(db, conv, user, body.channel)
    if not availability.available:
        raise HTTPException(status_code=409, detail=availability.detail)

    conv_id, conv_title = conv.id, conv.title
    try:
        if body.channel == "slack":
            from app.channels.slack import open_slack_dm_thread

            thread_id = await open_slack_dm_thread(
                user.email, _opening_text(conv, "slack", notice_language(user))
            )
        else:
            from app.channels.teams import post_to_teams_personal_chat

            thread_id = await post_to_teams_personal_chat(
                user.email, _opening_text(conv, "teams", notice_language(user))
            )
    except ChannelContinuationError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from None

    try:
        bound = await bind_thread(
            db,
            conversation_id=conv_id,
            user_id=user.id,
            channel=body.channel,
            channel_thread_id=thread_id,
        )
    except ChannelContinuationError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from None
    if bound.previous_conversation_id is not None:
        # A Teams personal chat follows the conversation continued last.
        await audit_released_threads(
            db,
            user=user,
            conversation_ids=[bound.previous_conversation_id],
            channel=body.channel,
            how=f"its thread now continues '{conv_title}'",
        )
    await log_audit(
        db,
        user=user,
        action="bind",
        entity_type="conversation",
        entity_id=conv_id,
        entity_name=conv_title,
        summary=(
            f"'{user.email}' continued conversation '{conv_title}' in "
            f"{channel_label(body.channel)}"
        ),
        details={"conversation_id": str(conv_id), "channel": body.channel},
    )
    response = _binding_response(bound.binding)
    await db.commit()
    logger.info("Conversation %s continued in %s", conv_id, body.channel)
    return response


@router.delete(
    "/conversations/{conversation_id}/channel-bindings/{channel}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_channel_binding(
    conversation_id: str,
    channel: ContinuableChannel,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop continuing the conversation in *channel*: its thread starts afresh."""
    conv = await owned_conversation(db, user, conversation_id, include_archived=False)
    removed = await unbind_conversation(db, conv.id, channel)
    if removed:
        await log_audit(
            db,
            user=user,
            action="unbind",
            entity_type="conversation",
            entity_id=conv.id,
            entity_name=conv.title,
            summary=(
                f"'{user.email}' stopped continuing conversation '{conv.title}' in "
                f"{channel_label(channel)}"
            ),
            details={"conversation_id": str(conv.id), "channel": channel},
        )
    await db.commit()
