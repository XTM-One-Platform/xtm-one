# SPDX-License-Identifier: Apache-2.0

"""Shared state - a generic cross-run state store agents read/write.

A SharedState row is a simple, durable, fleet-scoped key -> value record that
survives runs, so agents can share "the state of things" instead of cold-starting
every time. It is intentionally generic (not tied to any domain): the key is an
exact, caller-chosen identifier (e.g. ``order-12345``, ``deploy-pipeline``,
``ticket-42``), the value is a free-form JSON bag, and notes are an append-only
annotation log.

It complements WorkItem: a WorkItem is a unit of *work* to be done and picked up,
while a SharedState entry is the durable shared *state* that any number of agents
and runs read and update (the classic shared-blackboard pattern).

Lifecycle: an entry is ``active`` while the thing it tracks is live and
``resolved`` once that thing concludes. Resolution is a first-class status (not a
field baked into the free-form ``value`` bag), so resolved entries drop out of the
agent's active reads, keep a short audit trail, and are eventually purged by a
retention sweep. ``owner_agent_id`` names the agent responsible for keeping the
entry tidy - resolving it when done and purging it when it is no longer useful -
so shared state never grows unbounded with stale records.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.utils import CORRELATION_KEY_MAX_LENGTH

# Canonical lifecycle. ``active`` = the tracked thing is live; ``resolved`` = it
# has concluded (kept briefly for audit, then purged by the retention sweep).
# ``stale`` is intentionally NOT a stored status - it is derived from inactivity
# at read time (mirroring WorkItem's derived ``stale``).
SHARED_STATE_STATUSES: tuple[str, ...] = ("active", "resolved")
SHARED_STATE_ACTIVE_STATUS = "active"
SHARED_STATE_RESOLVED_STATUS = "resolved"
SHARED_STATE_TERMINAL_STATUSES: tuple[str, ...] = ("resolved",)


class SharedState(Base):
    """A generic, fleet-scoped, cross-run shared state entry (key -> value)."""

    __tablename__ = "shared_states"
    __table_args__ = (
        # Identity is per-USER for an unfiled row and per-WORKSPACE for a
        # filed one, which a single unique constraint cannot express.
        # Two PARTIAL uniques do.
        #
        # The trap this replaces: the old constraint was
        # `UniqueConstraint(user_id, key)` with the note that "NULLs are
        # distinct in Postgres so anonymous rows never collide". That
        # property is benign for anonymous rows and a HAZARD here — setting
        # `user_id = NULL` to make a workspace row user-agnostic would have
        # disabled uniqueness entirely, so every write would insert instead
        # of update. The predicates key on `workspace_id` instead, which is
        # never NULL on the rows it governs.
        # RENAMED, not reused: the old `uq_shared_states_user_key` CONSTRAINT owns an
        # index of that name, and the partial replacements have to be built
        # CONCURRENTLY *before* it is dropped — otherwise there is a window with
        # no uniqueness at all, which on a rolling deploy is a window for an old
        # replica to insert a duplicate. Two objects cannot share a name, so the
        # unfiled index takes a new one and the constraint keeps its own until it
        # goes.
        Index(
            "uq_shared_states_user_key_unfiled",
            "user_id",
            "key",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        Index(
            "uq_shared_states_workspace_key",
            "workspace_id",
            "key",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index("ix_shared_states_user", "user_id"),
        Index("ix_shared_states_key", "key"),
        Index("ix_shared_states_company_managed", "is_company_managed"),
        # Supports the active-only / resolved reads and the retention sweep.
        Index("ix_shared_states_user_status", "user_id", "status"),
        Index("ix_shared_states_workspace", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # The workspace this entry belongs to, when the capability that wrote it
    # was scoped to one. NULL for every `run` / `flow` / `global`
    # entry, which is all of them before this column existed.
    #
    # A TYPED FK rather than the generic `(scope, scope_ref)` pair the
    # worksets carry: a workspace is the first scope referent that is a
    # first-class, long-lived, user-visible entity, so it earns referential
    # integrity and a delete behaviour expressed in DDL.
    #
    # CASCADE is safe HERE and nowhere else among the three tool tables: a
    # shared-state row owns no heavy resource and is already retention-swept,
    # so nothing durable is lost with the workspace. `worksets` and
    # `work_items` are RESTRICT for reasons written on their own models.
    #
    # This column does NOT make the row's isolation. The key is still
    # namespaced (`workspace:<id>:<key>`), so a query that forgets to filter
    # on `workspace_id` MISSES rather than crossing the boundary. Two
    # mechanisms doing different jobs: the FK gives integrity, the namespace
    # means isolation does not depend on every call site remembering a filter.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Exact-match key, unique per user scope (see the UniqueConstraint).
    key: Mapped[str] = mapped_column(
        String(CORRELATION_KEY_MAX_LENGTH), nullable=False
    )

    # Optional human-friendly label for the entry.
    label: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The agent that owns / is responsible for this state: it keeps the entry
    # tidy (resolves it when the tracked thing concludes, purges it when it is no
    # longer useful). Defaulted to the creating agent by the write path so every
    # entry has a responsible janitor.
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Lifecycle status: "active" (live) or "resolved" (concluded). Resolved
    # entries drop out of active reads and are purged after a retention window.
    status: Mapped[str] = mapped_column(
        String(20), default=SHARED_STATE_ACTIVE_STATUS, nullable=False
    )
    # When the entry was resolved (drives the retention sweep). NULL while active.
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The run holding the entry as a lease: an ``AssignmentRun`` or,
    # for a background task with no run of its own, its ``SubAgentTask``,
    # stamped by ``set_shared_state(hold="take")``. While that run is alive a
    # take by another run is refused; once it ended the next take takes the
    # entry over and the tick's release (``release_ended_run_holds``) clears
    # it, each with a dated note. No key, like ``work_items.claimed_by_run_id``:
    # the run is read by id and a purged run reads as ended.
    held_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Lifecycle (#11696): what ends the entry, so the platform retires it.
    # ``is_lease``: created by a ``hold="take"`` to hold a lease; resolved when
    # the hold is released (by its holder or by the tick once the holder ended).
    is_lease: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # When the VALUE last changed: a note bumps ``updated_at``, not this, so
    # staleness reads how long the value has stood still.
    value_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The work item the entry tracks: resolved once it is done or cancelled.
    # No key, like ``held_by_run_id``: the item is read by id.
    linked_work_item_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Resolved by the tick at this date.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Free text naming what ends the entry, shown in every read.
    terminal_condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Retention policy of the entry's lists (#11839): ``{keep_last, max_chars,
    # archive_key, lists}``, applied on every write so a list at its cap rolls
    # its oldest items into ``archive_key`` instead of refusing the write.
    retention: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Free-form shared state bag (the actual key/value content agents share).
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    # Append-only annotation log: [{"at": iso, "agent_id": str|None, "note": str}].
    notes: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
