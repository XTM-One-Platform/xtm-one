# SPDX-License-Identifier: Apache-2.0

"""LangfuseConfig model – platform-wide Langfuse observability settings.

One row in this table.  All secret keys are Fernet-encrypted.
Admin configures via Settings UI; the tracing layer reads at runtime.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LangfuseConfig(Base):
    __tablename__ = "langfuse_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Langfuse connection details (encrypted)
    public_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Langfuse host (defaults to cloud, can be self-hosted)
    host: Mapped[str] = mapped_column(
        String(500), nullable=False, default="https://cloud.langfuse.com"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
