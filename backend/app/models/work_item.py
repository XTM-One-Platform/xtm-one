# SPDX-License-Identifier: Apache-2.0

"""Work-item model - the durable agentic task / work-item primitive.

A WorkItem is a first-class, durable unit of work that **survives runs** - the
shared *task queue* half of the agentic coordination layer (its sibling is
``SharedState``, the shared *state store*). Unlike an "Action Register" line
emitted as run text, a work item is an owned, trackable object that lives in a
queryable queue and can be atomically claimed and completed.

It is pure shared **data**: it does NOT itself drive control flow. Agents pick
work up explicitly through the agentic workflow - a target agent's scheduled
sweep (``list_work_items`` + ``claim_work_item``), or an explicit handover /
``delegate_to_assignment`` - which keeps the agentic workflow the single way
control passes between agents.

Design highlights:
  - **Survives runs**: persistent row, not run-scoped.
  - **Owner label**: ``owner_agent_id`` records which agent is expected to act so
    that agent can filter ``list_work_items(owner_agent_id=me)``; it is a routing
    label, not an automatic trigger.
  - **Atomic claim**: ``claim_work_item`` flips ``open -> claimed`` with a single
    conditional UPDATE so two agents can never both pick up the same item.
  - **Exact-match queries**: ``correlation_key`` (e.g. ``order-12345``,
    ``ticket-42``) is an exact filter, not semantic retrieval.
  - **Status lifecycle**: ``open -> claimed -> in_progress -> blocked -> done``
    (plus ``cancelled``); ``stale`` is *derived* by age / due date, never stored.

Visibility is two-tier (own + company-managed): an item created by a
company-managed agent is ``is_company_managed = True`` and visible fleet-wide;
otherwise it is private to the acting user.

**Scheduled work items** are the one exception to "pure data": an item whose
``payload.action`` is an executable action carries ``action_mode``
(``exact`` / ``instruction``), and the platform runs it at ``due_at`` with no
assignment and no flow (``services/scheduled_work_items.py``). ``action_mode``
is written by that module alone, so the generic paths can never turn an
ordinary item into a scheduled one, and neither consumer takes the other's
items: a work-item assignment or flow never receives an item with an
``action_mode``, the scheduler never receives one without.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, event, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.utils import CORRELATION_KEY_MAX_LENGTH

# Canonical status lifecycle. ``stale`` is intentionally NOT a stored status -
# it is derived from ``due_at`` / ``last_activity_at`` at read time.
WORK_ITEM_STATUSES: tuple[str, ...] = (
    "open",
    "claimed",
    "in_progress",
    "blocked",
    "done",
    "cancelled",
)
WORK_ITEM_OPEN_STATUSES: tuple[str, ...] = ("open", "claimed", "in_progress", "blocked")
WORK_ITEM_TERMINAL_STATUSES: tuple[str, ...] = ("done", "cancelled")
WORK_ITEM_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "urgent")
# The two forms of ``payload.action`` the platform runs at ``due_at``.
WORK_ITEM_ACTION_MODES: tuple[str, ...] = ("exact", "instruction")


class WorkItem(Base):
    """A durable, owned, trackable unit of work in the fleet's work queue."""

    __tablename__ = "work_items"
    __table_args__ = (
        Index("ix_work_items_user_status", "user_id", "status"),
        Index("ix_work_items_owner_status", "owner_agent_id", "status"),
        Index("ix_work_items_correlation_key", "correlation_key"),
        Index("ix_work_items_company_managed", "is_company_managed"),
        Index("ix_work_items_due_at", "due_at"),
        Index("ix_work_items_workspace", "workspace_id"),
        # The scheduler's tick reads exactly this slice every minute.
        Index(
            "ix_work_items_scheduled_due",
            "due_at",
            postgresql_where=text("action_mode IS NOT NULL AND status = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Acting / owning user, retained for provenance even on company-managed
    # items: a company-managed item created by a user keeps that ``user_id`` and
    # simply flips ``is_company_managed`` for fleet-wide visibility (mirrors
    # AssignmentRun, where company-managed is a flag, not a NULL owner). It is
    # NULL only when the creator is anonymous. Covered by the composite
    # ``ix_work_items_user_status`` (leftmost prefix), so no separate FK index.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Fleet-wide visibility flag. True when created by a company-managed agent
    # so every fleet member sees the item.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # The workspace this item belongs to. Until this column existed
    # `work_items` had NO scope, namespace or key column at ANY layer -- model
    # or tool -- which is why it needed a real column rather than a key prefix
    # like the other two primitives. Reusing `correlation_key` would have been
    # an abuse: that is a business identifier (``order-12345``), and
    # exact-match queries already depend on it.
    #
    # RESTRICT: deleting a workspace must NEVER delete tracked work. The
    # delete is refused while items remain, and the caller is pointed at
    # archiving the workspace instead -- failing loudly suits an operation
    # that is otherwise irreversible.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # The agent that OWNS the item (expected to act) - a routing label agents
    # filter on (``list_work_items(owner_agent_id=me)``), not an auto-trigger.
    # Covered by the composite ``ix_work_items_owner_status`` (leftmost prefix).
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Provenance: the agent that created the item (if any).
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Arbitrary structured payload carried with the item (free-form context).
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    # ``file_attachments`` ids the creator attached, checked against
    # the creating turn when the item is created and never changed after:
    # whoever may read the item may read them (``services/work_item_files.py``).
    # Ids, not keys: the file belongs to the conversation or run it came
    # from, and one whose row is gone simply reads as no longer there.
    attachment_file_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    priority: Mapped[str] = mapped_column(String(10), default="medium", nullable=False)

    # Exact-match correlation key (e.g. ``order-12345``, ``ticket-42``). NOT
    # semantic - callers query it with an equality filter.
    correlation_key: Mapped[str | None] = mapped_column(
        String(CORRELATION_KEY_MAX_LENGTH), nullable=True
    )

    # The deadline: when the work should be done. It defers nothing (#8753):
    # an overdue item is a late item, never a pickup trick.
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The earliest time an inbox may pick the item up ("act from then");
    # NULL = now. What ``list_dispatchable_work_items`` reads, apart from the
    # deadline above.
    not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Atomic claim bookkeeping (lost-update / double-pickup protection).
    claimed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The run that holds the claim: an ``AssignmentRun`` id - an
    # assignment run, or the child run of a background task - written by the
    # platform from the context the claiming tool runs in, never by an agent.
    # NULL for a claim made outside a run (a chat turn). When that run ends
    # (any terminal status, or dead-lettered after its lease expired) the
    # claim is released (``release_ended_run_claims``) and another run may
    # take the item over. No key, like ``run_id``: the run is read by id and a
    # purged run reads as ended.
    claimed_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Dated system notes the platform writes about the claim:
    # ``[{"at": iso, "text": str}]``, oldest first - a release after the
    # holding run ended, a take-over. Never the agent's own progress, which
    # stays in the description; never cleared.
    claim_notes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # The work items this one waits for: a list of ids, written by
    # ``create_work_item`` / ``update_work_item`` (``blocked_on``). Setting it
    # moves the item to ``blocked``; once every listed item is done the
    # scheduler puts it back in the queue (``release_satisfied_dependencies``),
    # and a cancelled one keeps it blocked with a note. SQL NULL when the item
    # waits for nothing (``none_as_null``), never an empty list, so "has a
    # dependency" is ``blocked_on IS NOT NULL``. No key: a blocker purged by
    # the retention sweep reads as gone.
    blocked_on: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # The agents its completion concerns: ``{"targets": [agent ids],
    # "kind", "instructions"}``, normalized by
    # ``services.work_item_fleet_impact``. Its ``work_item.completed`` event is
    # routed to each target too. SQL NULL when it impacts no one.
    fleet_impact: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # The pull requests and issues it waits for:
    # ``[{repo, kind, number, release_on, state?, at?}]``. A code-host delivery
    # closing one marks it satisfied; once every one is, and it waits for no
    # work item, the item goes back to open
    # (``services/work_item_blocked_refs.py``). SQL NULL when none.
    blocked_on_refs: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # What a blocked item waits for in words: who releases it, since
    # when. A ``blocked`` status needs this, ``blocked_on``, ``blocked_on_refs``
    # or ``needs_human``.
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Blocked on a person's decision, shown as such.
    needs_human: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # The person a ``needs_human`` item waits for when it is not ``user_id``
    # (``services.work_item_recipient``, #11893): an explicit recipient, or the
    # agent's owner when the run acts for nobody. NULL reads as ``user_id``;
    # with both NULL the item waits for every administrator.
    waits_for_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The groups a ``needs_human`` item waits for instead of one person (#1748):
    # UUID strings; any member at answer time answers, the first answer closes
    # it for all. SQL NULL when it waits for a person or the administrators.
    waits_for_group_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    # The decision a person is asked for, structured for the views:
    # ``{question, options?: [{key, label, effect?}], recommendation?,
    # deadline?}`` (``services.work_item_ask``). SQL NULL when none; a legacy
    # item shows ``blocked_reason`` as its question. Leaving ``blocked``
    # clears it with the other why fields.
    ask: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)

    # How the item came to exist: agent | manual | action_register.
    source: Mapped[str] = mapped_column(String(20), default="agent", nullable=False)

    # Completion result (set by complete_work_item). For a scheduled item, the
    # proof of what was done or the reason it was not.
    result: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Scheduled work items (``services/scheduled_work_items.py``) ──────
    # ``exact`` / ``instruction`` when ``payload.action`` is an action the
    # platform runs at ``due_at``; NULL for every other item. The scheduler's
    # only filter, so a generic write can never make an item executable.
    action_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # The run that executed it (an assignment-less ``AssignmentRun``). No key:
    # adding one would lock ``assignment_runs`` in the migration, and the run
    # is read by id - a purged run reads as gone.
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The conversation it was created in; a scheduled item posts its
    # outcome there. No key, for the same reason on ``conversations``.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # ── Origin (``services/work_item_context.py``) ────────────────
    # The assignment run and the flow run the creating tool ran in, written
    # by the platform at creation and never by an agent (``payload`` is the
    # agent's). No key, like ``run_id``.
    origin_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    origin_flow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # Why a scheduled item is ``blocked`` (``missed``, ``permission``, ...),
    # what the views read to show "missed" rather than a bare "blocked".
    outcome_code: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # When the item last reached ``done`` or ``cancelled``; NULL while it is
    # open and cleared when it is reopened. Written by the mapper listener
    # below on every ORM status change, and by the bulk UPDATEs that move
    # items to a terminal status themselves (the retention sweep). NULL on
    # an item that finished before the column existed.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Refreshed on every meaningful update; drives the derived ``stale`` flag.
    last_activity_at: Mapped[datetime] = mapped_column(
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


def stamp_completion(item: WorkItem, now: datetime | None = None) -> None:
    """Keep ``completed_at`` in step with ``status``.

    A terminal status without a completion time gets *now*; a status that is
    not terminal has none. A terminal item written again (its result edited,
    ``cancelled`` after ``done``) keeps the time it first finished.
    """
    if item.status in WORK_ITEM_TERMINAL_STATUSES:
        if item.completed_at is None:
            item.completed_at = now or datetime.now(UTC)
    elif item.completed_at is not None:
        item.completed_at = None


@event.listens_for(WorkItem, "before_insert")
@event.listens_for(WorkItem, "before_update")
def _stamp_completion(_mapper: object, _connection: object, target: WorkItem) -> None:
    stamp_completion(target)
