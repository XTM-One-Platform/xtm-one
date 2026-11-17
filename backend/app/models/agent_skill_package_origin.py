# SPDX-License-Identifier: Apache-2.0

"""AgentSkillPackageOrigin model — origin links for imported skill resources."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentSkillPackageOrigin(Base):
    """Persistent origin mapping between imported skill resources and created entities."""

    __tablename__ = "agent_skill_package_origins"
    __table_args__ = (
        UniqueConstraint(
            "skill_id",
            "resource_kind",
            "resource_path",
            name="uq_agent_skill_package_origins_skill_resource",
        ),
        CheckConstraint(
            "("
            "resource_kind = 'script' "
            "AND custom_tool_id IS NOT NULL "
            "AND knowledge_base_id IS NULL "
            "AND knowledge_document_id IS NULL"
            ") OR ("
            "resource_kind = 'reference' "
            "AND custom_tool_id IS NULL "
            "AND knowledge_base_id IS NOT NULL "
            "AND knowledge_document_id IS NOT NULL"
            ")",
            name="ck_agent_skill_package_origins_kind_target",
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
    resource_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    resource_path: Mapped[str] = mapped_column(Text, nullable=False)

    custom_tool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("custom_tools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    knowledge_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
