# SPDX-License-Identifier: Apache-2.0

"""App lock: the PIN that unlocks the mobile companion, and its phone sessions.

The companion (``frontend/src/components/companion``) can be locked behind a
PIN the person chooses (``AppLockPin``, one per person, hashed like a
password). Each phone's web session is recorded here once the companion
boots on it (``AppLockSession``, keyed by the session's ``sid``): from then on,
while the PIN applies, the platform answers that session nothing but the lock
endpoints until it is unlocked (``services/app_lock.py``). A computer's session
is never recorded, so the platform on a computer is not affected.

The state lives in the database, not in Redis: the lock must hold whenever the
platform answers at all.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AppLockPin(Base):
    __tablename__ = "app_lock_pins"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    #: bcrypt, like a password (``core.security.hash_password``).
    pin_hash: Mapped[str] = mapped_column(Text, nullable=False)
    #: The person's own choice; the ``companion_pin_required`` policy applies
    #: the lock whatever this says.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class AppLockSession(Base):
    """A web session the companion runs in: locked until the PIN is entered."""

    __tablename__ = "app_lock_sessions"

    #: The session JWT's ``sid`` claim.
    sid: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Unlocked until then; NULL or past: locked.
    unlocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Wrong PINs since the last good one.
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    #: Too many wrong PINs: the session is over, whatever its token says.
    ended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
