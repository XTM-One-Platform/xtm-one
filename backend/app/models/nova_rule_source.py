# SPDX-License-Identifier: Apache-2.0

"""NovaRuleSource — a GitHub repo of ``.nov`` rules synced on a schedule.

Mirrors the row-driven design of ``KBSyncSource``: one row per source, the
scheduler picks rows whose ``last_synced_at + sync_interval`` is due, pulls the
repo's ``.nov`` files, and upserts them as company-managed :class:`NovaRule`s.
Created by the Nova integration's ``on_connect`` hook (defaulting to the public
Nova-Hunting/nova-rules repo) and removed when it disconnects.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Sync cadences (kept in step with KBSyncSource's vocabulary).
NOVA_SYNC_INTERVALS = ("manual", "6h", "12h", "daily", "weekly")


class NovaRuleSource(Base):
    __tablename__ = "nova_rule_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The Nova integration this source belongs to; deleting the integration
    # removes its source (and the on_disconnect hook disables it first).
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # GitHub coordinates. ``repo`` is "owner/name"; ``path`` scopes to a subdir.
    repo: Mapped[str] = mapped_column(String(200), nullable=False)
    branch: Mapped[str] = mapped_column(
        String(100), nullable=False, default="main", server_default="main"
    )
    path: Mapped[str] = mapped_column(
        String(300), nullable=False, default="", server_default=""
    )

    sync_interval: Mapped[str] = mapped_column(
        String(20), nullable=False, default="daily", server_default="daily"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sync_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_rules: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Simple exponential backoff on failure.
    sync_failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sync_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
