# SPDX-License-Identifier: Apache-2.0

"""A WhatsApp message received for a ``whatsapp`` integration.

Meta posts every notification of an app to one callback URL; the channel
adapter (``app/channels/whatsapp.py``) routes a message whose
``metadata.phone_number_id`` belongs to a connected ``whatsapp`` integration
here instead of to the ``whatsapp_bot`` channel bot. Written and read by
``services/whatsapp_inbound.py``; read by the integration's
``list_whatsapp_conversations`` / ``read_whatsapp_messages`` tools.

A row lives ``inbound_retention_days`` (extra field of the integration, 30 by
default): older rows of the integration are purged on every write.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WhatsAppInboundMessage(Base):
    __tablename__ = "whatsapp_inbound_messages"
    __table_args__ = (UniqueConstraint("integration_id", "wamid", name="uq_whatsapp_inbound_messages_wamid"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The integration whose business number received the message.
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: WhatsApp message id (``wamid...``): a redelivered notification is stored once.
    wamid: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The conversation: the group id for a group message, the sender's number otherwise.
    conversation_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    #: ``group`` or ``private``.
    chat_type: Mapped[str] = mapped_column(String(16), nullable=False, default="private")
    sender: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The Cloud API message type (``text``, ``image``, ``unsupported``...).
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The text, the caption, or the words recorded for an event WhatsApp gives no content for.
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: The media id to pass to ``download_whatsapp_media``, for a media message.
    media_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
