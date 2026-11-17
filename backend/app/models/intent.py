# SPDX-License-Identifier: Apache-2.0

"""Intent, Vertical, PlatformIntentDeclaration, and IntentBinding models.

Intents are self-namespaced AI capabilities declared by platforms (e.g.
``opencti.report.summarize``).  Verticals represent business colorations
(e.g. ``fimi``, ``cti``, ``fraud``).  IntentBindings map agents to intents.
Intents and verticals are orthogonal — verticals are assigned directly on agents.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Intent(Base):
    """Global intent registry — self-namespaced dotted names."""

    __tablename__ = "intents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True, index=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class PlatformIntentDeclaration(Base):
    """Tracks which platforms declared which intents (many-to-many)."""

    __tablename__ = "platform_intent_declarations"
    __table_args__ = (
        UniqueConstraint("platform_identifier", "intent_id", name="uq_platform_intent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    platform_identifier: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )
    intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Vertical(Base):
    """Business vertical registry (e.g. fimi, cti, fraud)."""

    __tablename__ = "verticals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )
    # "platform" (auto-discovered from registration) or "manual" (admin-created)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    # Which platform declared this vertical (if source=platform)
    platform_identifier: Mapped[str | None] = mapped_column(String(50), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class IntentBinding(Base):
    """Binds an agent to an intent × vertical pair."""

    __tablename__ = "intent_bindings"
    __table_args__ = (
        UniqueConstraint(
            "intent_id",
            "vertical",
            "user_id",
            "agent_id",
            name="uq_intent_binding",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("intents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Vertical scoping — which business vertical this binding applies to
    vertical: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # NULL = global default (admin-set). If set = user personal override.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # Per-vertical priority overrides: {"cti": 5, "aev": 2}
    # Effective priority for a vertical = priority_overrides.get(vertical, priority)
    priority_overrides: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
