# SPDX-License-Identifier: Apache-2.0

"""Relay models - pull-based on-premise egress for SaaS deployments.

A **Relay** is a small headless daemon (``apps/relay/``, the ``xtm-one-relay``
binary) a customer installs inside their own network. It connects *outbound*
to XTM One (multiplexed WebSocket, HTTP long-poll fallback) and *pulls* HTTP
requests the platform needs to send to tools that are only reachable from
inside that network (Splunk, Tanium, an on-premise OpenCTI, ...). The customer
never opens an inbound flow: the relay is the only side that dials.

Four tables live here:

* :class:`RelayGroup` - a network zone ("Paris DC", "OT network"). An
  integration, an MCP server or a flow node is routed to a GROUP, never to a
  single machine: every relay of the group pulls from the same queue, which is
  the load balancing and the high availability. Three-tier visibility, and an
  optional platform-side allow-list of destinations on top of what each relay
  declares locally.
* :class:`Relay` - one enrolled relay process (a VM, a container, a pod).
  Hashed ``rly-`` credential (mirrors :class:`~app.models.runner.Runner`), the
  destinations it declares it may reach (its own local allow-list, reported on
  connect - the HARD boundary is enforced by the relay itself), its version and
  liveness.
* :class:`RelayEnrollmentKey` - a hashed ``rlenr-`` enrollment credential bound
  to ONE group. Single-use or reusable (a container image rolled out to many
  replicas uses one reusable key).
* :class:`RelayRequestLog` - metadata of each relayed request (method, host,
  path, status, sizes, duration). NEVER a header value or a body: the frames
  themselves only ever transit through Redis, encrypted, with a short TTL.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# ── Canonical vocabularies (single source of truth) ─────────────────────────

# Credential prefixes. ``rly-`` authenticates a relay on every call after
# enrollment; ``rlenr-`` is the enrollment credential a relay presents ONCE to
# ``POST /relays/enroll`` in exchange for an ``rly-``. Distinct from the
# runner's ``run-`` / ``renr-`` so a credential pasted into the wrong binary is
# refused by its prefix before any database lookup.
RELAY_KEY_PREFIX = "rly-"
RELAY_ENROLLMENT_KEY_PREFIX = "rlenr-"

# Platforms a relay can report (the OS family; a container reports ``linux``).
RELAY_PLATFORMS: tuple[str, ...] = ("windows", "macos", "linux")

# Wire protocol version spoken on ``/relays/connect`` and the long-poll
# endpoints. Mirrors ``PROTOCOL_VERSION`` in ``apps/relay/src/protocol.rs``.
RELAY_PROTOCOL_VERSION = 1

# Stream kinds a relay can serve. Only ``http`` is implemented; ``tcp``
# (database drivers, LDAP, syslog) and ``process`` (a CLI on the relay host)
# are reserved in the protocol so they can be added without a new transport.
RELAY_CAPABILITIES: tuple[str, ...] = ("http",)
RELAY_RESERVED_CAPABILITIES: tuple[str, ...] = ("tcp", "process", "secrets")

# Statuses a relay row may hold. ``revoked`` is final.
RELAY_STATUSES: tuple[str, ...] = ("online", "offline", "revoked")

# Transports a relay connects over.
RELAY_TRANSPORTS: tuple[str, ...] = ("ws", "long_poll")

# Kinds of platform resources that can be routed to a relay group.
RELAY_SOURCE_TYPES: tuple[str, ...] = (
    "integration",
    "mcp_server",
    "flow",
    "test",
)


class RelayGroup(Base):
    """A network zone that integrations, MCP servers and flow nodes route to."""

    __tablename__ = "relay_groups"
    __table_args__ = (
        Index("ix_relay_groups_is_company_managed", "is_company_managed"),
        Index("ix_relay_groups_slug", "slug", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # NULL when company-managed (shared with all users; admin-only edit).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Stable identifier (``paris-dc``), unique platform-wide: what an operator
    # types in scripts and what the installers print.
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    # Free-text network zone label ("Paris DC - OT network").
    zone: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional platform-side allow-list (host patterns: ``splunk.corp.local``,
    # ``*.corp.local``, ``10.0.0.0/8``, ``host:8089``). NULL = no platform-side
    # restriction: what each relay declares locally is the boundary. When set,
    # a request whose host matches no entry is refused BEFORE it is queued.
    allowed_destinations: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # --- Three-tier visibility (mirror Runner / MCPServer) ---
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    # Required, not optional: ``user_visible_filter`` references these columns
    # unconditionally for every entity carrying ``shared_group_ids``.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class Relay(Base):
    """One enrolled relay process. Its visibility is its group's."""

    __tablename__ = "relays"
    __table_args__ = (
        # The relay authenticates with its key on every connect / long-poll;
        # the prefix column is the indexed narrowing lookup before bcrypt.
        Index("ix_relays_key_prefix", "key_prefix"),
        # Enrollment converges a re-enrolled machine on its existing row.
        Index("ix_relays_group_machine", "group_id", "machine_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("relay_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Who enrolled it (audit). SET NULL: the relay belongs to its group.
    enrolled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Salted SHA-256 of the OS machine id (or of the container's persistent
    # volume id): opaque to the server, the re-enrollment dedup key.
    machine_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- Credential (mirror Runner) ---
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Reported on every connect (hello frame) ---
    host_facts: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    agent_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    protocol_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Stream kinds this relay serves (``["http"]`` today).
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # The relay's LOCAL allow-list as it reported it (display + audit). The
    # relay enforces it itself, after DNS resolution: this copy is never what
    # decides whether a request is sent.
    declared_destinations: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    # True when the relay runs with an allow-all local policy (``*``), which
    # the Security Audit reports.
    allows_all_destinations: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    max_concurrent_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="32", default=32
    )

    # --- Liveness ---
    # ``last_seen_at`` recency is the authoritative online signal (multi
    # replica safe); ``status`` is the cache the UI and the sweep keep.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="offline")
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transport: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Instant revocation: flip to False to reject the credential immediately.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    request_count: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )

    enrolled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class RelayEnrollmentKey(Base):
    """A hashed ``rlenr-`` enrollment credential bound to one relay group."""

    __tablename__ = "relay_enrollment_keys"
    __table_args__ = (Index("ix_relay_enrollment_keys_key_prefix", "key_prefix"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("relay_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Who created it (audit). SET NULL: a key belongs to its group, and a
    # company-managed group's keys must outlive the admin who minted them.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # NULL = unlimited reusable key. 1 = single-use.
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class RelayRequestLog(Base):
    """Metadata of one relayed request. Never a header value, never a body."""

    __tablename__ = "relay_request_logs"
    __table_args__ = (
        Index("ix_relay_request_logs_group_created", "group_id", "created_at"),
        Index("ix_relay_request_logs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("relay_groups.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The relay that answered (NULL when none did: no relay online, refused
    # by the platform-side allow-list, timed out in the queue).
    relay_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("relays.id", ondelete="SET NULL"),
        nullable=True,
    )
    # What sent it (see ``RELAY_SOURCE_TYPES``). Plain UUID, no FK: it names
    # several tables and must survive their deletion.
    source_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    method: Mapped[str] = mapped_column(String(10), nullable=False)
    scheme: Mapped[str] = mapped_column(String(10), nullable=False, default="https")
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Path only - the query string may carry a credential and is dropped.
    path: Mapped[str] = mapped_column(String(1000), nullable=False, default="/")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A relay or platform error code (``destination_denied``, ``timeout``,
    # ``no_relay_online``, ...), NULL when the destination answered.
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    bytes_sent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_received: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
