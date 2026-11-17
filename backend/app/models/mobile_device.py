# SPDX-License-Identifier: Apache-2.0

"""MobileDevice - a phone a person linked to XTM One (the mobile companion).

A phone is linked from itself, signed in, when the person turns on
notifications in the companion: its browser's Web Push subscription is stored
here, encrypted (invariant 2: the subscription's endpoint and keys are what let
anyone push to that phone). The person manages their phones from Profile >
Phones: test, rename, per-kind opt-outs, quiet hours, unlink.

Personal, never shared: only its owner sees, tests and uses a phone, and an
agent reaches the phones of the person it runs for only. An administrator can
list and unlink a person's phones (offboarding), audited.

``WebPushKeys`` is the platform's VAPID key pair (RFC 8292), generated on first
use, private key encrypted, never in the environment (invariant 4).
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MobileDevice(Base):
    __tablename__ = "mobile_devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: "Pixel 8 · Chrome": from the browser, renamable.
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: android, ios or other.
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    #: The Web Push subscription ``{endpoint, keys: {p256dh, auth}}``, Fernet-encrypted.
    push_subscription: Mapped[str] = mapped_column(Text, nullable=False)
    #: SHA-256 of the endpoint: finds the device again when the phone re-subscribes.
    endpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: Off when the person mutes the phone, or the push service said the
    #: subscription is gone (404 / 410).
    push_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=sa.true())
    #: Notification kinds the person does not want on this phone.
    muted_kinds: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: ``{"start": "22:00", "end": "07:00", "timezone": "Europe/Paris"}`` or NULL:
    #: nothing is pushed in between (an urgent approval included: it waits in the Inbox).
    quiet_hours: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_push_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class WebPushKeys(Base):
    """The platform's VAPID key pair: one row, created on first use."""

    __tablename__ = "web_push_keys"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    #: The application server key the browsers subscribe with (base64url, uncompressed P-256 point).
    public_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The private key (PEM), Fernet-encrypted.
    private_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
