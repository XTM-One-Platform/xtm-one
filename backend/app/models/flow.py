# SPDX-License-Identifier: Apache-2.0

"""Flow + FlowVersion models – the visual flow builder.

A **Flow** is a user-composed automation graph: trigger, agent, condition,
action, integration and utility nodes wired together on a canvas.  The graph
itself lives in a single JSON column (``graph``) shaped exactly like the
React Flow document the builder renders — ``{nodes, edges, viewport}`` — so
the canvas can round-trip it without a translation layer.

A Flow follows the standard three-tier visibility model (own / group-shared /
company-managed), like Prompts and every other main entity.

A Flow is authored, saved, versioned, exported and imported here, and
executed through ``api/v1/flow_runs.py``: ``POST /flows/{id}/activate`` moves
``status`` from ``draft`` to ``active`` (``PUT /flows/{id}`` refuses that
write), ``/deactivate`` moves it back, and each execution is a
:class:`app.models.flow_run.FlowRun` row.

Every save snapshots the graph into a **FlowVersion**, an immutable record
modelled on :class:`app.models.prompt_version.PromptVersion` (same
``version_number`` / ``author_id`` / ``commit_message`` / ``is_locked``
shape, so `api/v1/versioning_helpers.py` serialises it unchanged).  History
is pruned to the most recent :data:`FLOW_VERSION_RETENTION` entries.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# How many versions we keep per flow.  The issue asks for "minimum 20";
# older entries are pruned on every save.
FLOW_VERSION_RETENTION = 20

# Lifecycle.  Phase 1 only ever writes DRAFT — ACTIVE/ARCHIVED exist so the
# activation work is a pure addition rather than a migration.
FLOW_STATUS_DRAFT = "draft"
FLOW_STATUS_ACTIVE = "active"
FLOW_STATUS_ARCHIVED = "archived"
FLOW_STATUSES = (FLOW_STATUS_DRAFT, FLOW_STATUS_ACTIVE, FLOW_STATUS_ARCHIVED)


class Flow(Base):
    """A visually composed automation graph."""

    __tablename__ = "flows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # draft | active | archived — see FLOW_STATUSES.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FLOW_STATUS_DRAFT, index=True
    )

    # The canvas document: {"nodes": [...], "edges": [...], "viewport": {...}}.
    # Validated by app.services.flow_graph.validate_graph before every write.
    graph: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Monotonic counter, incremented on every save.  Mirrors the newest
    # FlowVersion.version_number.
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Tags – free-form labels for filtering and organisation
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Company-managed flows are visible to all users; only admins can edit.
    # NOT an invariant that ``user_id IS NULL`` when True: only the create path
    # nulls it. ``toggle_company_managed_list_field`` deliberately preserves the
    # original creator when a private flow is turned company-managed, so code
    # must never use "owner is NULL" as a proxy for "company-managed" — read
    # this flag.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # A sandbox copy the Platform Health experiments on (#8255): hidden from every
    # library, picker, analytics view and map, and every run of it has its
    # writes intercepted (``services/health/sandbox``).
    is_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # Platform Health improvement opt-in (#8254): the agent's opt-in, for a
    # flow's run journals (``services/health/consent.py``). Off by default.
    health_enrolled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Platform Health content opt-in (dev-docs/platform-health.md 1.2),
    # carried from the former improvement opt-in by migration h118health01.
    health_content_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Group sharing: list of Group UUIDs this entity is shared with, plus the
    # per-group read/write map every other three-tier entity carries (the
    # shared helpers in api/v1/helpers.py read and clear both).
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user sharing + company-managed narrowing. Same shape as the
    # group columns above, keyed by User UUID; see ``app.models.agent`` for the
    # rationale and ``app.api.v1.helpers.user_visible_filter`` for enforcement.
    # Required, not optional: that helper is generic over every entity carrying
    # ``shared_group_ids`` and references these columns unconditionally, so a
    # shared entity without them raises ``AttributeError`` at query-build time
    # on the non-admin branch — which is how every flow endpoint 500ed for
    # non-admin users once the builder shipped unflagged.
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

    # ── Phase 2: execution ──────────────────────────────────────────────────
    # Cron cursor for `trigger.schedule`: stamped by the scheduler sweep
    # *before* firing (same overlap-safe order as Assignment.last_triggered_at).
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Shared secret for `trigger.webhook` deliveries, minted on activation
    # when the graph carries a webhook trigger.  Verified the same way as
    # assignment webhooks (HMAC or bearer); never serialized in list payloads.
    webhook_secret: Mapped[str | None] = mapped_column(String(128), nullable=True)


class FlowVersion(Base):
    """Immutable snapshot of a flow's graph, taken on every save."""

    __tablename__ = "flow_versions"
    __table_args__ = (
        UniqueConstraint("flow_id", "version_number", name="uq_flow_versions_flow_ver"),
        CheckConstraint("version_number > 0", name="ck_flow_versions_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    flow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # The snapshotted canvas document.  Named `graph` rather than the
    # `content` of PromptVersion because it is structured, not text — the
    # versioning_helpers serialiser reads it through an explicit accessor.
    graph: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    commit_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Locked versions are exempt from retention pruning.
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        index=True,
    )
