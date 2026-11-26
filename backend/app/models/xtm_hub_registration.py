# SPDX-License-Identifier: Apache-2.0

"""XtmHubRegistration model - this platform's registration with the XTM Hub.

One row at most (``id`` is always 1): no row means XTM One is not registered.
An administrator registers the platform from Settings - the XTM Hub page
``/redirect/register-xtmone`` creates the registration and hands back a platform token
- and the hourly heartbeat (``services/xtm_hub_registration.py``) asks the Hub
whether the registration is still active.

The token is the platform's credential on the Hub: Fernet-encrypted, never
served, and bound to the Hub it was issued by (``hub_url``) and to the platform
id it was issued for (``platform_id``). ``generation`` changes on every
registration, so a heartbeat that started before a new registration can never
overwrite or delete it (compare-and-set).

``key_enrolled_at`` is when the Hub enrolled this instance's public key
(``core/xtm_hub_key.py``) for the registration: NULL until then. It alone
decides which credential a call to the Hub carries: the token before, a fresh
signed assertion after, never both. A new registration resets it.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The registration is fine.
STATUS_REGISTERED = "registered"
#: The Hub answered that the registration is inactive, or was unreachable
#: when asked: kept, shown, retried by the next heartbeat.
STATUS_LOST_CONNECTIVITY = "lost_connectivity"
#: The platform id changed since the registration (``PLATFORM_ID`` set
#: later, a restored or cloned database): the token belongs to another
#: identity, so nothing is sent until an administrator registers again.
STATUS_NEEDS_REREGISTRATION = "needs_reregistration"

REGISTRATION_STATUSES = (
    STATUS_REGISTERED,
    STATUS_LOST_CONNECTIVITY,
    STATUS_NEEDS_REREGISTRATION,
)

#: The singleton row's primary key.
SINGLETON_ID = 1


class XtmHubRegistration(Base):
    __tablename__ = "xtm_hub_registrations"
    __table_args__ = (
        CheckConstraint(f"id = {SINGLETON_ID}", name="ck_xtm_hub_registrations_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=SINGLETON_ID)
    hub_url: Mapped[str] = mapped_column(Text, nullable=False)
    platform_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_REGISTERED)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    # The last heartbeat or manual check, and what the Hub answered
    # (``active`` / ``inactive`` / ``unreachable``).
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # When the Hub enrolled this instance's public key: NULL = calls carry the
    # token; set = every call is signed with the key instead.
    key_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
