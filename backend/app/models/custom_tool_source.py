# SPDX-License-Identifier: Apache-2.0

"""CustomToolSource - a GitHub or GitLab repository synced into custom tools.

One row per repository (and sub-directory). Each directory of the repository
holding a ``tool.yaml`` manifest becomes one :class:`CustomTool` whose
``source_id`` points here and whose ``source_path`` is that directory: the
tool's identity within the source, so a renamed tool keeps the agents bound
to it. The sync (``services/custom_tool_sources.py``) activates a new version
of a tool only when its declared tests pass.

Visibility: the owner (``user_id``) manages a personal source; a
company-managed source (``is_company_managed``) is managed by admins only and
produces company-managed tools. The source itself is not shared: the tools it
produces are, like any custom tool.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Repository hosts a source can read.
CUSTOM_TOOL_SOURCE_PROVIDERS = ("github", "gitlab")

#: Sync cadences, the same vocabulary as KB and Nova rule sources.
CUSTOM_TOOL_SOURCE_INTERVALS = ("manual", "6h", "12h", "daily", "weekly")


class CustomToolSource(Base):
    __tablename__ = "custom_tool_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # "github" or "gitlab".
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    # ``owner/repo`` on GitHub, ``group/subgroup/project`` on GitLab.
    repository: Mapped[str] = mapped_column(String(300), nullable=False)
    # Branch, tag or commit SHA. ``None`` tracks the default branch.
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Sub-directory scanned for tool manifests ("" = the whole repository).
    path: Mapped[str] = mapped_column(
        String(300), nullable=False, default="", server_default=""
    )
    # The GitHub / GitLab integration whose credential reads the repository
    # (a private one, or a self-managed GitLab). ``None`` reads it anonymously
    # (public github.com / gitlab.com repositories). No foreign key: the sync
    # reports a deleted integration instead.
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    sync_interval: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual", server_default="manual"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_company_managed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Tool directories detached from the source: the sync leaves them alone
    # instead of creating them again.
    detached_paths: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The commit the last sync read.
    last_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "ok", "partial" (some tools rejected or unreadable) or "error".
    last_sync_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the last sync did, tool by tool (``SyncReport.as_dict``).
    last_sync_report: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    # Exponential backoff of the scheduled sync after a failure.
    sync_failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    sync_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
