# SPDX-License-Identifier: Apache-2.0

"""SkillVersion model — immutable snapshots for agent skills.

Mirrors :class:`PromptVersion` for skills: every edit to an
:class:`AgentSkill` that mutates ``content`` or one of the frontmatter
fields (``description`` / ``compatibility``) creates a new version row.
Admins can then roll back to any prior version, which restores the
skill's content from that snapshot. There is no active-version pointer
or activation API: the highest ``version_number`` is implicitly the
live version, and a rollback is recorded as a new forward version
rather than flipping an "active" flag.

Design choices intentionally kept close to ``PromptVersion``:

* ``version_number`` is per-skill (monotonic from 1, computed by the
  service helper, gap-tolerant on rollback so a rollback never reuses
  an existing number).
* ``is_locked`` lets admins pin a known-good version so a routine
  retention sweep never prunes it.
* ``author_id`` is ``SET NULL`` on user delete so the version's
  attribution softens to "originally an account that no longer
  exists" rather than the whole snapshot vanishing.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SkillVersion(Base):
    """Immutable content snapshot for an :class:`AgentSkill`."""

    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint(
            "skill_id", "version_number", name="uq_skill_versions_skill_ver"
        ),
        CheckConstraint(
            "version_number > 0", name="ck_skill_versions_version_positive"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_skills.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Frontmatter-style fields are versioned alongside ``content`` so
    # rolling back a skill also restores any description / compatibility
    # change that landed at the same time as the content edit.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    compatibility: Mapped[str | None] = mapped_column(Text, nullable=True)

    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    commit_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
    )
