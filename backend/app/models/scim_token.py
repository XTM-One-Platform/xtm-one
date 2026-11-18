# SPDX-License-Identifier: Apache-2.0

"""ScimToken: the bearer credential an identity provider (Microsoft Entra)
provisions accounts with, on ``/api/v1/scim/v2`` (``api/v1/scim.py``).

An administrator generates one in Settings; the raw token (``scim-...``) is
shown once and only its SHA-256 is stored (``core.security.hash_token``: the
token carries 256 bits of entropy and is looked up by exact value). A token
reaches the SCIM surface alone: it is never a session, an API key or an MCP
credential. Revoking it (``revoked_at``) refuses it on the next call.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The prefix every raw SCIM token starts with, so a leaked one is recognisable.
SCIM_TOKEN_PREFIX = "scim-"  # noqa: S105 - a public prefix, not a secret


class ScimToken(Base):
    __tablename__ = "scim_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: SHA-256 hex of the raw token; unique.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: The first characters of the raw token, for display.
    token_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The administrator who generated it (NULL once that account is deleted).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
