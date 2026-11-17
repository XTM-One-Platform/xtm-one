# SPDX-License-Identifier: Apache-2.0

"""The tally of a WhatsApp poll seen by a linked-device gateway integration.

The gateway backend of the ``whatsapp`` integration receives the polls the
paired number sends or receives and every vote on them through its inbound
webhook (``services/whatsapp_gateway_inbound.py``). One row per poll holds
its question and, per option, the option id, its name and the voters, so
``get_whatsapp_poll_results`` answers from the store without calling the
gateway. A vote replaces the voter's previous choice.

A row lives as long as the integration's received messages
(``inbound_retention_days``, 30 by default): older polls are purged on write.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WhatsAppPollTally(Base):
    __tablename__ = "whatsapp_poll_tallies"
    __table_args__ = (UniqueConstraint("integration_id", "poll_id", name="uq_whatsapp_poll_tallies_poll"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The poll's WhatsApp message id.
    poll_id: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The group id, or the other party's number for a 1:1 poll.
    conversation_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: JSON list of ``{"id", "name", "voters": [...]}``, one per option, in the poll's order.
    options: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
