# SPDX-License-Identifier: Apache-2.0

"""CustomIntegrationDefinition model.

Stores metadata-only integration providers installed from admin-imported
bundles (no executable code).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CustomIntegrationDefinition(Base):
    __tablename__ = "custom_integration_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Canonical provider key exposed to the integration catalog, always
    # namespaced as "custom:<slug>". unique+index yields a UNIQUE INDEX named
    # ix_... so create_all (fresh installs) and the Alembic migration
    # (existing installs) converge on the exact same schema objects.
    provider_key: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )

    # Bundle slug without namespace.
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Full metadata.json payload from the imported bundle.
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Cached settings schema extracted from metadata_json.settings.
    settings_schema: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Optional data URL for display in provider catalog cards.
    logo_data_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    supports_polling: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    supports_webhook: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
