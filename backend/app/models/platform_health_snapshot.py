# SPDX-License-Identifier: Apache-2.0

"""PlatformHealthSnapshot model - the latest health snapshot a registered platform pushed.

``dev-docs/platform-health.md`` section 5 (push): an OpenCTI, OpenAEV or OpenCRQ adds a
bounded ``health`` object to the registration it sends every 5 minutes. One row per
registration (``registration_id`` is the key, ``ON DELETE CASCADE``), replaced by every
snapshot, kept apart from ``platform_registrations`` so the heartbeat and the settings page
never load it. Written only by ``POST /platform/register``
(``services/health/snapshots.record_snapshot``) and read by the module checks
(``services/health/opencti_checks.py``).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlatformHealthSnapshot(Base):
    __tablename__ = "platform_health_snapshots"

    registration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_registrations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: The snapshot format the platform sent (``services/health/snapshots.SCHEMA_VERSION``).
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The snapshot as kept: its known keys only, already bounded.
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    #: When the platform last restarted as its registration showed it (version changed, or
    #: heartbeat back after a gap): its availability checks open nothing for a grace after (#958).
    restarted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
