# SPDX-License-Identifier: Apache-2.0

"""Polymorphic annotations - one user's rating of a piece of agent work.

A chat message carries a per-user thumbs rating
(``MessageFeedback``). The rest of what an agent produces could not be rated:
the result of an assignment run, of a background task, the answer another agent
returned to a delegation. ``Annotation`` is that rating for every target that is
not a chat message, addressed by a ``(target_type, target_id)`` pair, so a new
rateable surface is one more entry in the target registry of
``services/annotations.py`` rather than one more table.

Chat messages keep their own table on purpose. Moving the ``message_feedback``
rows in here cannot be done under a rolling deploy without writing both tables
for a while (replicas still on the previous version keep writing the old one),
and that dual write is what the first attempt could not make atomic.
The API is unified instead: ``/api/v1/annotations`` serves a ``message`` target
from ``message_feedback`` and every other target from this table.

``target_id`` carries no foreign key: it points at a different table per
``target_type``, and a key to ``assignment_runs`` or ``sub_agent_tasks`` would
make the migration lock those write-hot tables. What a key gives for free is
supplied by the service instead: every listing joins the live target, so the
rating of a deleted run is never shown, and the hourly sweep
(``services.annotations.purge_orphan_annotations``) deletes rows whose target is
gone. ``agent_id`` - the agent that did the rated work, resolved server-side
from the target, never taken from the client - is a real key, so deleting an
agent takes its annotations with it.

A rating comes from an XTM One user (``user_id``) or, for a reaction on an
agent's answer in a messaging platform, from a person of that platform
who may have no XTM One account: ``rater_ref``, a keyed digest of their
platform id (``services.reaction_feedback.rater_ref``), never the id itself.
Exactly one of the two is set; ``source`` says where the rating was given and
``emoji`` which reaction gave it.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Annotation(Base):
    """One user's rating (and optional comment) of one target.

    A user holds at most one annotation per target: rating again updates it,
    which is why ``(target_type, target_id, user_id)`` is unique and doubles as
    the upsert's conflict target and the per-target lookup index. A platform
    rater holds at most one too, through the partial unique index on
    ``rater_ref`` (a NULL ``user_id`` never conflicts in the first one).
    """

    __tablename__ = "annotations"
    __table_args__ = (
        UniqueConstraint(
            "target_type",
            "target_id",
            "user_id",
            name="uq_annotations_target_user",
        ),
        Index(
            "uq_annotations_target_rater",
            "target_type",
            "target_id",
            "rater_ref",
            unique=True,
            postgresql_where=sa.text("rater_ref IS NOT NULL"),
        ),
        CheckConstraint(
            "rating IN ('positive', 'negative')",
            name="ck_annotations_rating_valid",
        ),
        CheckConstraint(
            "num_nonnulls(user_id, rater_ref) = 1",
            name="ck_annotations_one_rater",
        ),
        # The lookup behind the users ON DELETE CASCADE.
        Index("ix_annotations_user_id", "user_id"),
        # An agent's feedback, newest first, and the lookup behind the agents
        # ON DELETE CASCADE.
        Index("ix_annotations_agent_updated", "agent_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: The XTM One user who rated; ``None`` for a platform rater (``rater_ref``).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    #: A platform rater's keyed digest (hex SHA-256), when no XTM One user rated.
    rater_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Where the rating was given: ``web``, or the platform a reaction came from
    #: (``services.reaction_feedback.SOURCES``).
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="web", server_default="web"
    )
    #: The reaction that gave a platform rating (normalized), ``None`` for ``web``.
    emoji: Mapped[str | None] = mapped_column(String(32), nullable=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    rating: Mapped[str] = mapped_column(String(20), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class AnnotationScore(Base):
    """One criterion's score inside one rating.

    Hangs off the rating it details - an ``Annotation`` or, for a chat message,
    its ``MessageFeedback`` row - exactly one of the two, so retracting the
    rating (or deleting the conversation, or the orphan sweep) removes its
    scores by cascade. The value is the raw input: ``value_text`` for a
    ``categorical`` option, ``value_numeric`` for ``numeric`` / ``scale``;
    ``services.feedback_criteria`` normalizes it to [0, 1] when it aggregates.
    """

    __tablename__ = "annotation_scores"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(annotation_id, message_feedback_id) = 1",
            name="ck_annotation_scores_one_rating",
        ),
        CheckConstraint(
            "num_nonnulls(value_numeric, value_text) = 1",
            name="ck_annotation_scores_one_value",
        ),
        # One score per criterion per rating; each also serves the lookup
        # behind its rating's ON DELETE CASCADE.
        UniqueConstraint(
            "annotation_id", "criterion_id", name="uq_annotation_scores_annotation_criterion"
        ),
        UniqueConstraint(
            "message_feedback_id",
            "criterion_id",
            name="uq_annotation_scores_feedback_criterion",
        ),
        # The lookup behind the feedback_criteria ON DELETE CASCADE.
        Index("ix_annotation_scores_criterion_id", "criterion_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    annotation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("annotations.id", ondelete="CASCADE"),
        nullable=True,
    )
    message_feedback_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("message_feedback.id", ondelete="CASCADE"),
        nullable=True,
    )
    criterion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feedback_criteria.id", ondelete="CASCADE"),
        nullable=False,
    )
    value_numeric: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    value_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=sa.text("now()"),
    )
