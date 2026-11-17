# SPDX-License-Identifier: Apache-2.0

"""OAuthDefault model – admin-managed OAuth credentials shared with all users.

Admins can pre-configure client_id and client_secret for OAuth-based providers
(Microsoft, Google, GitHub, Slack, etc.) so that regular users only need to
click "Connect" and go through the OAuth flow without setting up their own
app registration.

One row per provider.  All secrets are Fernet-encrypted before storage.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OAuthDefault(Base):
    __tablename__ = "oauth_defaults"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Provider key — matches PROVIDER_CATALOG keys (microsoft, google, github, slack, …)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)

    # Encrypted OAuth app credentials
    client_id_enc: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret_enc: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional extra config (JSON, e.g. {"tenant_id": "..."} for Microsoft)
    extra_config_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
