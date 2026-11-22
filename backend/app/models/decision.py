# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""One row per question a person is asked, whatever engine asked it (#842, S2).

``dev-docs/human-gates.md`` section 8 (G2): every human gate (a run paused by
``request_human_input``, a tool approval, a flow Ask-user park, a work item
waiting for a person) writes a :class:`Decision` in the transaction that opens
it, and :class:`DecisionEvent` keeps the append-only audit (asked, answered,
refused, cancelled, expired, notified).

The engines keep their own columns and still resume from them (dual write,
migration contract step 1): this table is what the router answers by
(``POST /decisions/{id}/answer``) and what the inbox, the bell and the channel
cards will read once the backfill (S2b) and the read switch land.

The public id of a decision is its source, ``run:<uuid>``, ``flow_run:<uuid>``
or ``work_item:<uuid>`` (:func:`public_id`); at most one decision per source is
``open`` (a partial unique index), a later pause of the same source cancels the
earlier one.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: The objects a decision is asked on; the prefix of its public id.
SOURCE_TYPES: tuple[str, ...] = ("run", "flow_run", "work_item")
#: The four gates (section 1 of the spec): which engine asked.
DECISION_KINDS: tuple[str, ...] = ("hitl", "tool_approval", "flow_ask", "work_item")
DECISION_STATUSES: tuple[str, ...] = ("open", "answered", "expired", "cancelled")
#: The audit trail of a decision, append only.
DECISION_EVENTS: tuple[str, ...] = ("asked", "notified", "answered", "refused", "cancelled", "expired")


def _now() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def public_id(source_type: str, source_id: uuid.UUID | str) -> str:
    """``run:<uuid>``: the id a card, a bell entry and the router name a decision by."""
    return f"{source_type}:{source_id}"


class Decision(Base):
    """A question waiting for (or answered by) a person."""

    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint(_in("source_type", SOURCE_TYPES), name="ck_decisions_source_type"),
        CheckConstraint(_in("kind", DECISION_KINDS), name="ck_decisions_kind"),
        CheckConstraint(_in("status", DECISION_STATUSES), name="ck_decisions_status"),
        # One open decision per source: the router resolves ``run:<uuid>`` to it.
        Index(
            "uq_decisions_open_source",
            "source_type",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        # The inbox and the bell: what waits for whom, soonest deadline first.
        Index("ix_decisions_recipient_status_deadline", "recipient", "status", "deadline"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The ask model of section 2 (``decision_ask.py``): question, context, options...
    ask: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: ``user:<uuid>``, ``group:<uuid>``, ``company`` (every member), ``admins``.
    recipient: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    #: ``{option?, label?, text?}`` once answered.
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    answered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Where the answer came from: web, inbox, bell, push, teams_card, router...
    surface: Mapped[str | None] = mapped_column(String(40), nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    @property
    def public_id(self) -> str:
        return public_id(self.source_type, self.source_id)


class DecisionEvent(Base):
    """One line of a decision's audit trail (append only)."""

    __tablename__ = "decision_events"
    __table_args__ = (CheckConstraint(_in("event", DECISION_EVENTS), name="ck_decision_events_event"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("decisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    surface: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: What happened: the option and text of an answer, the reason of a refusal.
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


# The dual write: registered with the model so every process that maps it writes it.
import app.models.decision_hooks  # noqa: F401
