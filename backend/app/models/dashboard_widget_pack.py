# SPDX-License-Identifier: Apache-2.0

"""DashboardWidgetPack - a library entry of ready-made dashboard widgets.

A pack is a named set of widgets of every kind a custom dashboard takes -
XTM One widgets and native OpenCTI / OpenAEV / OpenCRQ ones - imported from a
file (an XTM One pack, an OpenCTI dashboard or widget export, an OpenAEV
dashboard export), saved from a dashboard, or shipped with the platform
(built-ins, tagged ``builtin`` and ``slug:<slug>``, company-managed, refreshed
on startup and never edited: they are duplicated). Its widgets are stored as
``dashboard_interop.pack_format`` keeps them: checked like a dashboard's, a
platform widget WITHOUT an ``integration_id`` - which integration it reads is
chosen when it is inserted into a dashboard, among the person's own.

Three-tier visibility like every library entity (``VisibilityColumns``).
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.visibility import VisibilityColumns

#: Where a pack came from.
PACK_ORIGINS: tuple[str, ...] = ("upload", "opencti", "openaev", "dashboard", "builtin")


class DashboardWidgetPack(VisibilityColumns, Base):
    """A named set of ready-made widgets, inserted into dashboards."""

    __tablename__ = "dashboard_widget_packs"
    __table_args__ = (Index("ix_dashboard_widget_packs_is_company_managed", "is_company_managed"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: ``PACK_ORIGINS``.
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="upload")
    #: The platforms its widgets read (``opencti`` / ``openaev`` / ``opencrq``), for filters.
    sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: The widgets, unbound (no ``integration_id``), at most ``MAX_PACK_WIDGETS``.
    widgets: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    widget_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    #: The entity inputs its widgets read (an OpenCTI custom view's "current entity"), unbound.
    inputs: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=sa.text("'[]'"))
    #: What the import said: the platform version of an export, the conversion notes.
    source_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
