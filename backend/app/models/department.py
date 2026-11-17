# SPDX-License-Identifier: Apache-2.0

"""Departments: the org units LLM analytics roll usage up to.

A department is not a group. A group is a membership set (an access list, a
distribution list, a SAML claim) and a person belongs to many; a person
belongs to ONE department, resolved by ``services/departments.py`` from an
ordered chain: the directory department their identity provider sends
(``User.directory_department``, matched against a department's name and its
``directory_values``), then the first ``DepartmentGroupRule`` by priority
whose group they are in, else none (``Unassigned``).

The department a trace counts for is resolved when the trace is written and
stored on it (``LLMTrace.department_id``), so a move between departments never
rewrites history.
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Department(Base):
    """An org unit of the platform's analytics."""

    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The directory department values (case-insensitive) that resolve to this
    # department besides its own name: "R&D" and "Research" for Engineering.
    directory_values: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class DepartmentGroupRule(Base):
    """A member of *group_id* counts for *department_id*, unless a rule of lower *priority* matched first."""

    __tablename__ = "department_group_rules"
    __table_args__ = (UniqueConstraint("group_id", name="uq_department_group_rules_group"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class TraceAttributionBackfill(Base):
    """Where the one-time stamping of the older traces stands (one row, ``id`` 1).

    ``services/tracing/attribution_backfill.py`` walks the traced days newest
    first, ``cursor_date`` the oldest one done; ``completed_at`` is set once
    the oldest day with data is done. ``department_rewrite_before`` is an
    administrator's "apply the mapping to history": every department resolved
    before that instant is resolved again.
    """

    __tablename__ = "trace_attribution_backfill"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    cursor_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    department_rewrite_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
