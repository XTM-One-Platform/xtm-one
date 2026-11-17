# SPDX-License-Identifier: Apache-2.0

"""What the platform remembers about one Security Audit finding.

One row per stable finding id (``<category>.<name>``), written only for
a finding that was notified to the administrators or dismissed by one - the
audit's findings themselves are computed on every run and never stored.

- **Notification memory**: ``notified_at`` is when the administrators were last
  told about the finding (``services/security_audit_notifications.py``). A
  finding is notified once, ever; the one exception is a dismissal re-opened
  by a higher severity (``reopened_at`` later than ``notified_at``).
- **Dismissal**: an administrator accepted the finding (``dismissed_at`` set):
  it leaves every count and the overall score and never notifies. It is
  re-opened - ``dismissed_at`` cleared, ``reopened_at`` set, the dismissal's
  who / severity / reason kept as its history - once the finding is reported
  at a higher severity than it was dismissed at. A restore clears
  ``dismissed_at`` too, keeps that history and records ``restored_at`` /
  ``restored_by_user_id``.

The rules live in ``services/security_audit_finding_states.py``.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Longest finding id stored; the ids the audit builds are far shorter.
FINDING_ID_MAX_CHARS = 200
#: Longest dismissal reason an administrator may write.
DISMISSAL_REASON_MAX_CHARS = 1000


class SecurityAuditFindingState(Base):
    __tablename__ = "security_audit_finding_states"

    finding_id: Mapped[str] = mapped_column(String(FINDING_ID_MAX_CHARS), primary_key=True)

    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notified_severity: Mapped[str | None] = mapped_column(String(20), nullable=True)

    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL once the account is deleted; the audit log's dismiss row still
    # names who it was.
    dismissed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    dismissed_severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    dismissal_reason: Mapped[str | None] = mapped_column(String(DISMISSAL_REASON_MAX_CHARS), nullable=True)

    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopened_severity: Mapped[str | None] = mapped_column(String(20), nullable=True)

    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restored_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
    )
    # No `onupdate`: every write is a Core statement, which would not fire it,
    # so each one sets `updated_at` itself.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
    )
