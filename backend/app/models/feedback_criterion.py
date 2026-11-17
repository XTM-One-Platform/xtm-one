# SPDX-License-Identifier: Apache-2.0

"""Feedback criteria - the dimensions a rating of agent work is scored on.

A thumbs up or down says whether the work was good; a criterion says in what
way: accuracy, tone, completeness, whatever an administrator wants measured. A
criterion is platform-wide configuration, like the LLM providers: admins define
it in Settings > Agents and automations > Quality, every user sees the enabled ones in the rating dialog
and may score each one or leave it blank.

Three shapes, fixed at creation (``type`` never changes, or stored scores would
stop meaning anything):

- ``categorical``: pick one of ``options`` (``[{"value", "label", "score"}]``),
  each option carrying the ``score`` in [0, 1] it contributes;
- ``numeric``: any number in ``[min_value, max_value]``;
- ``scale``: an integer step in ``[min_value, max_value]`` (a Likert scale).

``weight`` is the criterion's share of the weighted score
(``services.feedback_criteria``); a set of enabled criteria whose weights sum to
zero is refused, since it would weigh nothing.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FeedbackCriterion(Base):
    """One scored dimension of a rating."""

    __tablename__ = "feedback_criteria"
    __table_args__ = (
        UniqueConstraint("key", name="uq_feedback_criteria_key"),
        CheckConstraint(
            "type IN ('categorical', 'numeric', 'scale')",
            name="ck_feedback_criteria_type_valid",
        ),
        CheckConstraint("weight >= 0", name="ck_feedback_criteria_weight_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: Stable identifier, the slug of the name at creation. Never changes, so a
    #: rename keeps every stored score attached to the same dimension.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    weight: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default=sa.text("1.0")
    )
    #: ``categorical`` only: ``[{"value": str, "label": str, "score": float}]``.
    options: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: ``numeric`` / ``scale`` only; both bounds are required for those types.
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    #: A disabled criterion leaves the rating dialog and the weighted score but
    #: keeps its history: disabling is how a dimension is retired.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=sa.text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
    )
