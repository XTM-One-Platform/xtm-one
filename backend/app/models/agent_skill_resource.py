# SPDX-License-Identifier: Apache-2.0

"""AgentSkillResource model — persisted files inside a packaged skill.

Stores non-root skill files from ``scripts/``, ``references/``, and
``assets/`` so package imports/exports are lossless.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Where a skill package bundles its licence: ``LICENSE.md`` at its root. The
#: skill's Catalog tab shows it under "License and attribution" next to the
#: ``AgentSkill.license`` notice, and the agent's skill catalog does not list it.
SKILL_LICENSE_PATH = "LICENSE.md"


class AgentSkillResource(Base):
    """A persisted file belonging to an :class:`AgentSkill` package."""

    __tablename__ = "agent_skill_resources"
    __table_args__ = (
        UniqueConstraint("skill_id", "path", name="uq_agent_skill_resources_skill_path"),
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

    # Relative path inside skill root, e.g. ``scripts/extract.py``.
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


async def copy_skill_resources(db, source_skill_id: uuid.UUID, target_skill_id: uuid.UUID) -> int:
    """Copy every resource of one skill onto another (a clone). Returns the count.

    A skill's bundled files are part of it: its ``references/`` the agent reads,
    and a licence that must travel with every copy of a third-party skill. A
    clone that dropped them would still say "see references/x.md" and would no
    longer carry the licence its body points to.
    """
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(AgentSkillResource)
            .where(AgentSkillResource.skill_id == source_skill_id)
            .order_by(AgentSkillResource.path.asc())
        )
    ).scalars().all()
    for row in rows:
        db.add(
            AgentSkillResource(
                skill_id=target_skill_id,
                path=row.path,
                content=row.content,
                content_type=row.content_type,
                size=row.size,
                sha256=row.sha256,
            )
        )
    return len(rows)
