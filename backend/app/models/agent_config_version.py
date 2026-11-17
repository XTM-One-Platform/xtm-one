# SPDX-License-Identifier: Apache-2.0

"""AgentConfigVersion model – immutable snapshots for selected agent config fields."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Canonical list of agent config fields that participate in versioning.
# Adding a field here:
#   1) updates the SQLAlchemy `CheckConstraint` below automatically;
#   2) MUST be paired with an Alembic migration that ALTERs the SQL
#      `ck_agent_config_versions_field_name_valid` constraint to match —
#      otherwise inserts for the new field will fail at the DB level.
TRACKED_AGENT_CONFIG_FIELDS: tuple[str, ...] = (
    "guardrails",
    "temperature",
    "reasoning_effort",
    "max_iterations",
)


def _tracked_field_check_constraint() -> str:
    """Render the SQL ``IN (...)`` clause for the tracked-field constraint.

    Kept in sync with the migration's hard-coded string by construction:
    if a new field is added to ``TRACKED_AGENT_CONFIG_FIELDS`` the model
    constraint follows automatically — but the matching Alembic migration
    is still required to update the persisted DB constraint.
    """
    joined = ",".join(f"'{name}'" for name in TRACKED_AGENT_CONFIG_FIELDS)
    return f"field_name IN ({joined})"


class AgentConfigVersion(Base):
    """Immutable snapshot for one tracked agent config field."""

    __tablename__ = "agent_config_versions"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "field_name",
            "version_number",
            name="uq_agent_config_versions_agent_field_ver",
        ),
        CheckConstraint(
            "version_number > 0",
            name="ck_agent_config_versions_version_positive",
        ),
        CheckConstraint(
            _tracked_field_check_constraint(),
            name="ck_agent_config_versions_field_name_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    field_name: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stored as PostgreSQL ``JSONB`` to match the migration's DDL
    # (``postgresql.JSONB(astext_type=sa.Text())``) — the ORM column type
    # must match the actual DB type or Alembic autogenerate will produce
    # spurious "alter column" churn on every run and SQLAlchemy will emit
    # an implicit cast on every read.
    value_json: Mapped[dict | list | str | float | int | bool | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # ``Text`` (unbounded) to match the migration's ``sa.Text()`` column
    # type and the sibling ``PromptVersion.commit_message`` column; using
    # an unbounded ``String`` here would trigger Alembic autogenerate
    # churn against the persisted DDL.
    commit_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
    )
