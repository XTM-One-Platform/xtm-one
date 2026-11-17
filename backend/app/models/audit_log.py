# SPDX-License-Identifier: Apache-2.0

"""Audit log model – tracks user actions across the platform."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuditLog(Base):
    """Stores audit trail entries for user actions (CRUD, config changes, bindings)."""

    __tablename__ = "audit_logs"
    # ``user_id``, ``action``, ``entity_type`` and ``created_at`` are indexed
    # inline. ``entity_id`` and the ``(entity_type, entity_id)`` composite
    # support entity-specific audit-trail lookups. ``ix_audit_logs_sign_ins``
    # holds the completed sign-ins only, for the Security Audit's last sign-in
    # reads: its predicate is ``security_audit._completed_sign_in()``.
    __table_args__ = (
        Index("ix_audit_logs_entity_id", "entity_id"),
        Index("ix_audit_logs_entity_type_id", "entity_type", "entity_id"),
        Index(
            "ix_audit_logs_sign_ins",
            "user_id",
            "created_at",
            postgresql_where=text("action = 'login' AND entity_type = 'user'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_email: Mapped[str] = mapped_column(String(320), nullable=False)

    # Action performed: create, update, delete, login, logout, toggle, trigger, etc.
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Entity affected: agent, assignment, integration, knowledge_base, mcp_server, custom_tool, user, settings, etc.
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # ID + name of the affected entity (nullable for global actions like login)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    entity_name: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Human-readable summary of what happened
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # Extra structured data (old/new values, config changes, etc.)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Request metadata
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
