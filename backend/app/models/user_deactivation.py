# SPDX-License-Identifier: Apache-2.0

"""One deactivation of an account: what the cascade did, and what awaits review.

Written by ``services/user_deactivation.py`` whatever the path that deactivated
the account (the administrator toggle, SCIM ``active=false`` or DELETE, an SSO
lockout). One row per episode; reactivation closes it (``reactivated_at``) and
restores what ``snapshot`` recorded, except what an administrator reviewed or
reassigned since.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: ``source`` values: who deactivated the account.
DEACTIVATION_SOURCES = ("admin", "scim", "sso", "system")


class UserDeactivation(Base):
    __tablename__ = "user_deactivations"
    __table_args__ = (
        Index("ix_user_deactivations_user_id", "user_id"),
        # At most one OPEN episode per account: a second deactivation of an
        # account already deactivated is a no-op, never a second cascade.
        Index(
            "uq_user_deactivations_open_user",
            "user_id",
            unique=True,
            postgresql_where=text("reactivated_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: One of :data:`DEACTIVATION_SOURCES`.
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The administrator who acted; NULL for SCIM, SSO and the system.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Who acted, as shown: an administrator's name, ``SCIM (<token name>)``...
    actor_label: Mapped[str] = mapped_column(String(320), nullable=False)
    #: The administrator the shared and company-managed objects went to; NULL
    #: when no other active administrator exists (nothing was transferred).
    successor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: The prior state of every object the cascade changed and can put back:
    #: ``{"agents": [{id, enabled, web_enabled}], "assignments": [id],
    #: "transfers": [{table, id, slug, is_default}]}``.
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    #: What an administrator must look at: ``[{key, kind, id, name, action,
    #: detail, reviewed_at, reviewed_by, resolution}]``.
    review_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Per-step counts, for the notification and the audit row.
    counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deactivated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    reactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
