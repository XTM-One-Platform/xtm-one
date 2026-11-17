# SPDX-License-Identifier: Apache-2.0

"""CTEM Cases: open exposures grouped by the one remediation that closes them (#11545).

A case is one fix (upgrade a product to a fixed version, apply a course of
action) and the open issues it closes, read from OpenCTI (vulnerabilities on
the organization's tech stack) and OpenAEV (CVE findings on its assets). The
grouping and the state rules live in ``services/ctem_cases.py``; the design is
``dev-docs/ctem-cases.md``.

- ``ctem_cases``: the case, its fix (kind, title, product, fixed version),
  its state (needs attention, in progress, resolved, accepted risk), owner,
  due date, the accepted-risk reason and expiry. Shared by the organization.
- ``ctem_case_issues``: one row per member issue, its source id and whether it
  is still open; ``open`` false once a source read confirmed it closed.
- ``ctem_case_events``: the workflow history; an event about one issue names
  its member id only.

No source content is stored (#11613): the name, CVE, scores, KEV, assets,
crown jewels, priority and counts of a case are read at display time from the
viewer's own source read (``services/ctem_cases_view.py``), so each viewer
sees exactly what the source products show them, and an issue they cannot see
is a "Restricted" row.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The four states of a case, in board order.
CASE_STATES: tuple[str, ...] = ("needs_attention", "in_progress", "resolved", "accepted_risk")
#: What the fix is.
REMEDIATION_KINDS: tuple[str, ...] = ("upgrade", "patch", "config", "course_of_action", "manual")
CASE_PRIORITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
ISSUE_SOURCES: tuple[str, ...] = ("opencti", "openaev")


def _now() -> datetime:
    return datetime.now(UTC)


class CtemCase(Base):
    __tablename__ = "ctem_cases"
    __table_args__ = (
        CheckConstraint(
            "state IN ('needs_attention', 'in_progress', 'resolved', 'accepted_risk')",
            name="ck_ctem_cases_state_valid",
        ),
        CheckConstraint(
            "state <> 'accepted_risk' OR (owner_user_id IS NOT NULL"
            " AND accepted_reason IS NOT NULL AND accepted_until IS NOT NULL)",
            name="ck_ctem_cases_accepted_risk_complete",
        ),
        Index("ix_ctem_cases_org_vertical_state", "organization_id", "vertical", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    vertical: Mapped[str] = mapped_column(String(20), nullable=False, default="cyber")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    remediation_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    product_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: ``None`` when the data names the product but no fixed version.
    fixed_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: Every remediation key mapped to this case (several after a manual merge).
    keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: ``source:external_id`` of the issues a manual split or a manual case owns.
    pinned_issues: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="needs_attention")
    state_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    accepted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CtemCaseIssue(Base):
    __tablename__ = "ctem_case_issues"
    __table_args__ = (
        UniqueConstraint("case_id", "source", "external_id", name="uq_ctem_case_issues_member"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ctem_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CtemCaseEvent(Base):
    __tablename__ = "ctem_case_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ctem_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    #: A user id, an agent id, or ``system``.
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    #: created | state | owner | due | issue_added | issue_closed | issue_reopened | merge | split | note
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The member id (``source:external_id``) an issue event is about, else ``None``.
    issue: Mapped[str | None] = mapped_column(String(220), nullable=True)
    from_value: Mapped[str | None] = mapped_column(String(300), nullable=True)
    to_value: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
