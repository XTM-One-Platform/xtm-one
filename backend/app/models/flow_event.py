# SPDX-License-Identifier: Apache-2.0

"""FlowEventType + FlowEvent — typed custom events for the flow builder.

The Unreal-Blueprint pattern: an **event type** is declared once — a name plus
a typed field list, the dispatcher's *signature* — then ``util.emit_event``
fires it and ``trigger.event`` / ``util.wait_event`` bind to the SAME
declaration.  Emitter and listeners reference the declaration's id, so they
can never silently drift apart on the payload shape.

**FlowEventType** is the declaration.  ``fields`` is a JSON list of
``{name, type, required, description}`` with ``type`` one of
:data:`EVENT_FIELD_TYPES` — deliberately shaped like a struct definition so
the same table can later back a generic user-defined type registry
(Cast/Break targets), not just event signatures.

**FlowEvent** is one emission: an audit row that also *drives* dispatch.
Listener runs (``trigger.event``) are started synchronously at emit time;
parked ``util.wait_event`` nodes poll this table on resume.  ``scope`` bounds
the reach — ``run`` (heard only inside the emitting run), ``flow`` (any run
of the same flow) or ``global`` (every active flow of the same owner) — and
``chain_depth`` is the runaway guard: a run started by an event inherits the
emitter's depth + 1 and refuses past the cap, UE's recursion guard.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.scopes import (  # noqa: F401  (re-exported vocabulary)
    EVENT_SCOPE_FLOW,
    EVENT_SCOPE_GLOBAL,
    EVENT_SCOPE_OPTIONS,
    EVENT_SCOPE_RUN,
    EVENT_SCOPES,
)

# The declaration's field vocabulary. `json` is the escape hatch for nested
# structures; the other three keep simple payloads legible and coercible.
EVENT_FIELD_TYPES = ("string", "number", "boolean", "json")

# Emission reach — how far an emission carries, i.e. which waiters hear it.
# Re-exported from `app.core.scopes`, which also holds the coordination
# vocabulary and explains why the two stay separate despite coinciding today.
# Kept under these names: `flow_events` and `runner_flow_events` import them.

# An event that triggers a flow that emits again inherits depth+1; past this
# the emit still succeeds for waiters but starts no further runs.
EVENT_CHAIN_MAX_DEPTH = 8


class FlowEventType(Base):
    """A declared, typed event signature (the UE Event Dispatcher)."""

    __tablename__ = "flow_event_types"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # [{"name": str, "type": string|number|boolean|json,
    #   "required": bool, "description": str}]
    fields: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Standard three-tier visibility (own / group-shared / company-managed),
    # same columns as Flow so `user_visible_query` applies unchanged.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)
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


class FlowEvent(Base):
    """One emission of a declared event — audit row and dispatch record."""

    __tablename__ = "flow_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    event_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_event_types.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Emitter coordinates — what scope matching selects on.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    flow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    # run | flow | global — see EVENT_SCOPES.
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="flow")

    # The rendered, type-coerced fields ({name: value}), validated against
    # the declaration at emit time.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Emit-chain depth (event → run → emit → …), capped at
    # EVENT_CHAIN_MAX_DEPTH when starting listener runs.
    chain_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    __table_args__ = (
        CheckConstraint("chain_depth >= 0", name="ck_flow_events_chain_depth"),
        CheckConstraint(
            "scope IN ('run','flow','global')", name="ck_flow_events_scope"
        ),
        # Supersede lookup for platform events that re-fire for the SAME
        # subject — today the memory events, whose emitter deletes an
        # entry's older emissions when it writes a newer one. Without this the
        # delete degrades to a scan of every emission of that type on every
        # durable memory write, which is the cost the supersede exists to
        # avoid. Harmless for event types whose payload carries no
        # ``memory_id``: they index a single NULL.
        Index(
            "ix_flow_events_type_memory_id",
            "event_type_id",
            text("(payload ->> 'memory_id')"),
        ),
    )
