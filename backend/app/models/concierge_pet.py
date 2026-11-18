# SPDX-License-Identifier: Apache-2.0

"""The Concierge companion's pet library (#8261).

A pet is a Codex-format package (``pet.json`` and an 8-column spritesheet)
an administrator uploads; the platform stores the spritesheet it decoded and
re-encoded itself, never the uploaded bytes (``services/concierge/pets.py``).
The built-in pets ship as files and have no row. A platform library, not a
shareable entity: every signed-in user may pick any pet, administrators add
and remove them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, SmallInteger, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConciergePet(Base):
    __tablename__ = "health_pets"
    __table_args__ = (UniqueConstraint("slug", name="uq_concierge_pets_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: ``pet.json``'s ``id``: lowercase letters, digits and dashes, unique across the library.
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(280), nullable=True)
    #: 1: 9 rows (1536x1872); 2: 11 rows, the look directions (1536x2288).
    sprite_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    #: The spritesheet as the platform re-encoded it, WebP.
    spritesheet: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    spritesheet_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=func.now()
    )
