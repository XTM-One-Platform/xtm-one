# SPDX-License-Identifier: Apache-2.0

"""PlatformBuild model - the builds this platform has booted with.

One row per distinct ``(version, git_sha)``: the version the About dialog shows
(``APP_VERSION``) and the commit it was built from (``GIT_SHA``). Written at
startup by ``services/platform_builds.record_running_build`` and never
backfilled, so the history starts at the first boot of a release that records
it. ``first_seen_at`` orders the releases: a build is the deployed one from its
first boot until the next build's.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlatformBuild(Base):
    __tablename__ = "platform_builds"
    __table_args__ = (UniqueConstraint("version", "git_sha", name="uq_platform_builds_version_sha"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Empty when the image was built without ``VITE_GIT_SHA`` (a local build):
    #: never NULL, so the unique constraint holds one row per such version.
    git_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    #: The built-in tools this build ships, ``{group id: [function, ...]}``,
    #: written once by the first process of the build that reads its catalog
    #: (``services/builtin_tool_release_notice.py``); NULL until then. The
    #: next build diffs against it to tell the owners of agents scoped to a
    #: group which new functions their allowlist leaves out (#12192).
    builtin_tools: Mapped[dict[str, list[str]] | None] = mapped_column(JSON, nullable=True, default=None)
    #: When ``platform.release.deployed`` was raised for the build: once its rollout
    #: completed (``services/platform_builds.mark_released``), never at a first boot a
    #: newer build may supersede within minutes. NULL until then.
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
