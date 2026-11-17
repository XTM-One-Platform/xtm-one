# SPDX-License-Identifier: Apache-2.0

"""A request someone made for what they lack on an entity.

Made from the card of a pasted XTM One link by a viewer who cannot see or
cannot answer what it points to (``dev-docs/teams-link-unfurl.md``, section 9):

* an **action** request asks whoever may answer an assignment run to act on it
  themselves. It grants nothing: runs are never shared (decision D7).
* a **share** request (not yet made anywhere) will ask an entity's owner to
  share it, granted through the entity's own sharing.

One open request per requester and entity, held by a partial unique index, so
a card cannot be used to flood whoever receives them.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

KIND_ACTION = "action"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
ENTITY_ASSIGNMENT_RUN = "assignment_run"


class AccessRequest(Base):
    __tablename__ = "access_requests"
    __table_args__ = (
        Index(
            "uq_access_requests_open",
            "requester_id",
            "entity_type",
            "entity_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_access_requests_entity", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requester_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_OPEN)
    # Where it was made: ``teams_card`` for now.
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Who dismissed it; NULL when the entity moved on (a run answered) or nobody did.
    closed_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
