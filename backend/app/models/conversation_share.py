# SPDX-License-Identifier: Apache-2.0

"""ConversationShare – per-conversation sharing grants.

Conversations are owner-scoped by default. This model is what lets an owner
widen that scope *without* dropping RBAC: a grant names a principal that
already exists on the platform, so every reader is an authenticated,
identifiable account that can be audited and revoked.

Why a dedicated table rather than the ``shared_group_ids`` JSON column that
agents / integrations / knowledge bases use (see
``app.api.v1.helpers.get_group_permission``):

* Sharing a conversation is **per-person** first and per-group second. The
  JSON column can only carry group ids, and a peer-to-peer "share this chat
  with Alice" is the primary use case.
* Conversations are high-cardinality and short-lived compared to those config
  entities. A join table keeps the grants out of the hot ``conversations`` row
  and lets a share be deleted without rewriting the conversation.
* Real foreign keys mean a deleted user or group takes its grants with it.
  A JSON array of ids silently accumulates orphans that then have to be
  filtered on read, which is exactly the kind of fail-open drift an access
  control table must not have.

The principal is modelled as two nullable FKs with an "exactly one" CHECK
rather than a ``(principal_type, principal_id)`` pair, for the same reason:
a polymorphic id column cannot carry a foreign key, so it cannot cascade.

Link-level access ("anyone on the platform with the link") is deliberately
*not* a row here — it grants nothing to a specific principal, so it lives as
``Conversation.link_access`` instead. See that column's comment.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The only permission a share can currently carry. Writing to someone else's
#: conversation is deliberately out of scope: a conversation has one
#: ``user_id``, one quota bucket, one compaction state and one agent-routing
#: history, and two people driving that concurrently is not a permission
#: level — it is a different feature. A recipient who wants to continue forks
#: the thread into their own conversation instead.
#:
#: Kept as a column (rather than implied by the row's existence) because the
#: API surfaces it and the tier list is expected to grow; adding a NOT NULL
#: column to a populated access-control table later is strictly more work
#: than carrying it from the start.
SHARE_PERMISSION_READ = "read"

CONVERSATION_SHARE_PERMISSIONS = (SHARE_PERMISSION_READ,)


class ConversationShare(Base):
    """One grant of access to a conversation, to a user or to a group."""

    __tablename__ = "conversation_shares"
    __table_args__ = (
        # Exactly one principal. Without this a row could name both (ambiguous)
        # or neither (a grant to nobody that still widens nothing but shows up
        # in the owner's share list).
        CheckConstraint(
            "num_nonnulls(shared_with_user_id, shared_with_group_id) = 1",
            name="ck_conversation_shares_exactly_one_principal",
        ),
        CheckConstraint(
            "permission IN ('read')",
            name="ck_conversation_shares_permission_valid",
        ),
        # Re-sharing with the same principal updates the existing grant rather
        # than stacking duplicates. Two *partial* uniques rather than one
        # composite: the unused principal column is NULL on every row, and
        # NULLs never conflict in a plain UNIQUE, so a composite would let the
        # same (conversation, user) pair be inserted without limit.
        Index(
            "uq_conversation_shares_user",
            "conversation_id",
            "shared_with_user_id",
            unique=True,
            postgresql_where=text("shared_with_user_id IS NOT NULL"),
        ),
        Index(
            "uq_conversation_shares_group",
            "conversation_id",
            "shared_with_group_id",
            unique=True,
            postgresql_where=text("shared_with_group_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- principal: exactly one of the two is set (see the CHECK above) ----
    shared_with_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    shared_with_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    permission: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=SHARE_PERMISSION_READ,
        server_default=SHARE_PERMISSION_READ,
    )

    # Who granted it. ``SET NULL`` rather than ``CASCADE``: an offboarded
    # employee's shares must not silently evaporate — the grant belongs to the
    # conversation, and revoking it stays the owner's decision.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
