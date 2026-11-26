# SPDX-License-Identifier: Apache-2.0

"""The OpenCTI PIRs stored in XTM One, with the organization's policy on each (#2435).

One row per PIR of each connected OpenCTI: what OpenCTI says about it (name,
description, type, a criteria summary), refreshed in the background
(``services/organization_pirs.py``), and what the organization says about it
(the question it answers, its owner, review cadence, the primary assets it
serves, its own minimum score and whether it drives signals). A PIR OpenCTI
no longer answers keeps its row with ``gone_at`` set: a policy never loses a
PIR silently.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The review cadences a PIR may carry (the profile's former ``PirDetail`` values).
PIR_REVIEW_CADENCES: tuple[str, ...] = ("", "weekly", "monthly", "quarterly", "half_yearly", "yearly")


class OrganizationPir(Base):
    """One OpenCTI PIR and the organization's policy on it (#2435)."""

    __tablename__ = "organization_pirs"
    __table_args__ = (
        UniqueConstraint("integration_id", "pir_id", name="uq_organization_pirs_integration_pir"),
        Index("ix_organization_pirs_enabled", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: The OpenCTI integration the PIR was read from; ``None`` for a PIR carried from a profile before
    #: the first refresh named its source (the refresh adopts it).
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="SET NULL"), nullable=True
    )
    #: The PIR's OpenCTI internal id.
    pir_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default=text("''"))
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    #: OpenCTI's ``pir_type`` (``THREAT_LANDSCAPE``, ``THREAT_ORIGIN``, ``THREAT_CUSTOM``).
    pir_type: Mapped[str] = mapped_column(String(40), nullable=False, default="", server_default=text("''"))
    #: The criteria count and weights, summarised for the table (never the raw filters).
    criteria_summary: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default=text("''"))
    question: Mapped[str] = mapped_column(String(1000), nullable=False, default="", server_default=text("''"))
    owner: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default=text("''"))
    review_cadence: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default=text("''"))
    primary_assets: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: The PIR score an entity must reach in this PIR before it records a signal (0 to 100).
    min_score: Mapped[int] = mapped_column(Integer, nullable=False, default=75, server_default=text("75"))
    #: Whether the PIR drives signals: the PIR playbook listens to it and agents read it as ``pir_ids``.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set when the PIR is no longer answered by its OpenCTI; cleared when it comes back.
    gone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=text("now()")
    )
