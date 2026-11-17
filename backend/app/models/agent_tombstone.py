# SPDX-License-Identifier: Apache-2.0

"""What a deleted agent leaves behind for the work it did.

Deleting an agent ``SET NULL``s ``conversations.agent_id`` and
``original_agent_id``, and the agent's traces keep a bare ``agent_id`` with no
row left to name it. The tombstone is the soft reference that survives: the
agent's id, its last name and icon, who owned it and the agent its
conversations were handed to. Written once by ``agent_deletion.delete_agent``,
in the deletion's own transaction; read by ``services/conversation_continuity``
(history labels, conversation tools) and by the trace read stores (visibility
and search of a deleted agent's traces).

No foreign key anywhere: the agent is gone, its successor and its owner may go
too, and a name is all a reader needs from them.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentTombstone(Base):
    __tablename__ = "agent_tombstones"

    #: The deleted agent's own id, the one its traces and messages carry.
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    #: Who owned the agent when it was deleted: they keep reading its traces.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    is_company_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The agent its conversations were handed to, when the deletion named one.
    successor_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
