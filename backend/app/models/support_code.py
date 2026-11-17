# SPDX-License-Identifier: Apache-2.0

"""SupportCodeRedemption – the burn ledger for Filigran-issued support codes.

A support code is a compact, Filigran-signed token an admin pastes into the
platform to grant something the platform cannot grant itself (today: a recharge
of the per-agent failure quota pools).  Verification is fully offline — the
signature is checked against the license trust anchor already embedded in
``app/enterprise/xtm_ca.py`` — which means a signature alone is replayable
forever.  This table is what makes a code **single-use**: the ``code_id`` claim
carried inside the token is stored here on redemption, under a unique
constraint, so a second attempt (including two concurrent ones) is refused by
the database rather than by a check that could race.

It doubles as the admin-facing history of "who recharged what, when, with which
support reference" — the reason the payload's free-text ``note`` is persisted
alongside.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SupportCodeRedemption(Base):
    __tablename__ = "support_code_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # The ``id`` claim from the signed payload — the code's identity, and the
    # single-use guarantee (unique). Opaque to the platform: whatever
    # filigran-licenses minted, capped at a sane length before it ever reaches
    # a query.
    code_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    # What the code granted, e.g. "agent_failure_pool_recharge". Stored so the
    # history stays readable once more code kinds exist.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # The code's ``platform`` claim: this platform's id, or "global" for a code
    # any platform may redeem. Kept for the audit trail — the match itself is
    # enforced at redemption time.
    platform_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Validity window carried by the code itself (informational after the fact).
    issued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # SET NULL, not CASCADE: deleting the admin who redeemed a code must not
    # delete the proof that the code was burned — that record is what keeps it
    # single-use.
    redeemed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Free-text reference from the payload (support ticket, customer name).
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
