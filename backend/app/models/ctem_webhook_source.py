# SPDX-License-Identifier: Apache-2.0

"""Generic signed inbound webhook sources of the CTEM trigger paths (#2506).

A tool XTM One has no integration for posts JSON to a path through one of these:

- one row per source, attached to one trigger path; ``enabled`` starts false, so
  a source records nothing until an administrator switches it on;
- ``secret_enc`` is the per-source HMAC secret, encrypted at rest; it is shown
  once, when the source is created or its secret rotated, never read back;
- ``mapping`` names, per signal field (``id``, ``label``, ``severity``), the
  dotted path of the posted JSON it is read from;
- deliveries reach ``/api/hooks/ctem-webhook-sources/{id}``
  (``services/ctem_webhook_sources.receive``), signed ``X-XTM-Signature:
  sha256=<hex HMAC-SHA256 of the raw body>``; a wrong or missing signature is
  refused with 401 and records nothing.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class CtemWebhookSource(Base):
    __tablename__ = "ctem_webhook_sources"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: One of ``models.ctem_signal.TRIGGER_PATHS``.
    trigger_path: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: One of ``models.ctem_signal.SUBJECT_KINDS``: what the recorded signals are about.
    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    secret_enc: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``{"id": "dotted.path", "label": "...", "severity": "..."}`` read in the posted JSON.
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    secret_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why the last delivery recorded nothing (an unmapped id), cleared by the next recorded one.
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
