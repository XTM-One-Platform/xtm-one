# SPDX-License-Identifier: Apache-2.0

"""Channel bot model – standalone entity for messaging platform bots.

Each channel bot stores credentials for a single messaging platform connection
(Slack, Teams, WhatsApp, Telegram, Google Chat, Discord).  Unlike
integrations, channel bots never use OAuth — they always authenticate via bot
tokens or app credentials.

Channel bots are always company-managed (is_company_managed=True): shared with
all users, only admin-editable.  user_id keeps the creating admin as owner;
visibility comes from is_company_managed.

All secrets are Fernet-encrypted before storage.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChannelBot(Base):
    __tablename__ = "channel_bots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Channel bots are always company-managed (platform-wide resources).
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=True)

    # Group sharing: list of Group UUIDs this entity is shared with.
    # Members of these groups can see (but not edit) the entity.
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user sharing + company-managed narrowing. Same shape as the
    # group columns above, keyed by User UUID; see ``app.models.agent`` for the
    # rationale and ``app.api.v1.helpers.user_visible_filter`` for enforcement.
    #
    # Present for schema uniformity (the visibility helper is generic over
    # every shared entity) but deliberately NOT exposed on the channel-bot API:
    # bots are platform-wide resources, and ``app.api.v1.schemas.channel_bot``
    # already dropped the group-sharing fields for being accepted but never
    # persisted. Exposing a restriction with no way to grant would recreate
    # exactly that trap.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # slack_bot | teams_bot | whatsapp_bot | telegram_bot | discord_bot | googlechat_bot
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    # Human-readable name
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="Default")

    # --- Bot credentials (encrypted) ---
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Extra config (JSON, encrypted) for provider-specific settings
    # e.g. {"signing_secret": "..."} for Slack
    # e.g. {"tenant_id": "..."} for Teams
    # e.g. {"phone_number_id": "...", "verify_token": "..."} for WhatsApp
    extra_config_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Channel whitelists: restrict which channels agents can receive/publish on.
    # Format: list of {id, name, group} objects.
    # NULL = all channels allowed for that direction.
    allowed_receive_channels: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allowed_publish_channels: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Allow anonymous (unauthenticated) users on external channels.
    # When True, incoming messages from users without a Copilot account are
    # still processed.  The channel user's metadata (name, phone, platform ID)
    # is forwarded as context to the agent.  Defaults to False (require account).
    # Can be overridden per-channel at the agent level via
    # channel_config[channel].allow_anonymous (null = inherit from bot).
    allow_anonymous: Mapped[bool] = mapped_column(Boolean, default=False)

    # Allow private/DM conversations even when channel restrictions are set.
    # When True (default), private conversations (Slack DMs, Teams personal
    # chats, Telegram private chats) bypass receive channel whitelists so
    # users can always message the bot directly.  Defaults to True.
    # Can be overridden per-channel at the agent level via
    # channel_config[channel].allow_private (null = inherit from bot).
    allow_private: Mapped[bool] = mapped_column(Boolean, default=True)

    # Only respond when the bot is explicitly @mentioned in group channels.
    # When True, messages in group channels (Teams channels, Slack channels,
    # Telegram groups) are silently ignored unless they contain an explicit
    # @mention of the bot.  Private/DM conversations are unaffected.
    # Defaults to False (respond to all messages).
    # Can be overridden per-channel at the agent level via
    # channel_config[channel].only_mention (null = inherit from bot).
    only_mention: Mapped[bool] = mapped_column(Boolean, default=False)

    # Whether anything this bot is involved in pauses for human approval.
    # One statement — "this bot is trusted" — with two consequences,
    # because both are needed for it to mean anything:
    #
    # * a conversation routed by this bot runs uninterrupted, whatever tools the
    #   agent reaches for (``channels/router`` passes ``hitl_off_outcome``);
    # * its own send / edit / delete tools stop pausing wherever they are
    #   called, including from a scheduled run (the gate's exempt sources, via
    #   the ``channel_bot:<id>`` stamped on each definition).
    #
    # The first is the one that makes gated tools usable here at all: this
    # surface cannot collect a decision — the router replies with a refusal and
    # the turn ends — so without it a gated tool asked for through a bot is not
    # delayed, it is unavailable.
    #
    # Defaults TRUE, and unlike an integration's reads a bot's are never
    # exempted by the classifier either (``hitl_whitelist_derived``): reading a
    # channel is reading people's conversations, so nothing about a bot stops
    # asking unless a person says so here.
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # connected | disconnected | error
    status: Mapped[str] = mapped_column(String(20), default="disconnected")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
