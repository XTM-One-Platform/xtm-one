# SPDX-License-Identifier: Apache-2.0

"""NotificationReadState - what a person has read of their notifications.

One row per person, on the server, so the bell on a computer and the Inbox on
a phone agree (``services/notification_feed.py``): everything created up to
``read_at`` is read, and so is every id in ``dismissed_ids`` (the ones marked
one by one since). A run awaiting the person stays unread until answered.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class NotificationReadState(Base):
    __tablename__ = "notification_read_states"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    #: Everything created up to here is read. NULL: nothing marked yet.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Ids marked read one by one after ``read_at`` (capped, newest kept).
    dismissed_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
