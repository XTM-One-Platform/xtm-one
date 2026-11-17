# SPDX-License-Identifier: Apache-2.0

"""PlatformRegistration model — tracks OpenCTI/OpenAEV instances registered with XTM One."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# A platform re-registers periodically (its de-facto heartbeat — there is no
# separate heartbeat endpoint, ``/register`` refreshes ``last_heartbeat_at``).
# Treat a registration as healthy while it is active AND its last heartbeat is
# within this window. The window is deliberately several times the typical
# re-registration cadence (~5 min) so normal jitter / a single missed cycle never
# flips the connectivity indicator red. Single source of truth for "is this
# platform connected right now", shared by the /platform/registrations endpoint
# and the CTEM posture payload.
HEARTBEAT_HEALTHY_WINDOW = timedelta(minutes=15)


class PlatformRegistration(Base):
    __tablename__ = "platform_registrations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    platform_identifier: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )
    platform_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The platform's identity: the URL it registers with, which is the ``iss``
    # of the tokens it signs and the ``aud`` of the tokens addressed to it.
    platform_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Where XTM One reaches the platform when that is not ``platform_url``: the
    # ``{PROVIDER}_API_URL`` override of XTM One's own environment, captured by
    # the registration, never sent by the platform nor editable from the UI.
    # ``NULL`` = the platform is reached on ``platform_url``.
    api_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform_title: Mapped[str] = mapped_column(String(255), nullable=False)
    platform_version: Mapped[str] = mapped_column(String(50), nullable=False)

    contract: Mapped[str] = mapped_column(String(10), nullable=False, default="CE")
    license_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    license_validated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Fernet-encrypted enterprise license PEM sent by the platform at
    # registration. Persisted ONLY when ``license_validated`` is True, and
    # cleared when a re-registration no longer validates, so a stale PEM can
    # never outlive its entitlement. When Enterprise Edition is inherited
    # from this platform (a connected EE OpenCTI/OpenAEV, no Filigran license
    # of its own) this is the sole EE credential available: the Filigran AI
    # extraction path forwards it so import-doc AI is entitled to the included
    # allowance exactly like chat is. See ``filigran_ai._resolve_pem``.
    enterprise_license_pem_enc: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")

    # The fingerprint of the procedure manifest the platform (OpenCRQ) reported
    # on its last registration, whether or not XTM One holds that manifest
    # (``PlatformProcedureManifest``). ``NULL`` = it reported none. A stored
    # manifest decides only while its fingerprint is this one.
    procedure_manifest_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Tool approval for this platform's tools ───────────────────
    # Two separate things, deliberately not collapsed into one column.
    #
    # ``supports_approval_prompts`` is a FACT the platform reports about itself:
    # can its embedded assistant render an approval prompt and return a
    # decision? Only the platform knows. NULL means it has never said - every
    # build predating the capability, which is most of the fleet today.
    #
    # ``approval_policy`` is a POLICY only an operator can set: what should
    # happen to this platform's tools when a prompt cannot be shown. Gate them
    # (agents explain themselves instead of acting) or let them run. Seeded
    # once from ``TOOL_APPROVALS_PLATFORM_DEFAULT`` when the row is first created,
    # never re-seeded afterwards - a heartbeat re-registration that reapplied
    # the deployment default would silently revert an administrator's choice
    # minutes after they made it.
    #
    # ``approval_policy_set_by`` records provenance so the UI can say whether a
    # setting is protecting the operator or merely inherited from an
    # install-time decision nobody remembers - and so an explicit human choice
    # is never overwritten by a later automatic one.
    supports_approval_prompts: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    approval_policy: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="require", default="require"
    )
    approval_policy_set_by: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="default", default="default"
    )

    # When a client of this platform last declared, on a chat message, that it
    # will answer an approval prompt (``supports_tool_approval``).
    #
    # Observation, not declaration - deliberately a separate column from
    # ``supports_approval_prompts`` above, which only registration writes. One
    # client answering prompts does not make the platform capable: an
    # unattended caller of the same platform may send nothing, and that
    # combination is legitimate. So this never decides whether tools gate; it
    # only lets the UI offer a choice an operator would otherwise never be
    # prompted to reconsider, because a platform that never declared the
    # capability produces no nudge however many capable clients it has.
    #
    # A timestamp rather than a flag: "a client said so once, in 2024" and "a
    # client said so this morning" deserve different confidence, and it is the
    # only way to notice the evidence going stale.
    approval_support_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Business vertical of the registering platform (e.g. "fimi", "cti", "fraud").
    # Used for intent binding resolution (vertical-specific agents).
    business_vertical: Mapped[str | None] = mapped_column(String(50), nullable=True)

    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    def is_healthy(self, now: datetime | None = None) -> bool:
        """Whether the platform is currently connected (live connectivity check).

        Healthy = ``status`` active AND the last heartbeat is within
        ``HEARTBEAT_HEALTHY_WINDOW``. This is the authoritative, real-time health
        signal; it must never be read from a stale cache, otherwise a recovered
        platform stays red until the cache expires.
        """
        if self.status != "active" or self.last_heartbeat_at is None:
            return False
        now = now or datetime.now(UTC)
        last = self.last_heartbeat_at
        # ``last_heartbeat_at`` is stored tz-aware, but guard against a naive
        # value (e.g. some drivers / SQLite) to keep the subtraction safe.
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (now - last) <= HEARTBEAT_HEALTHY_WINDOW
