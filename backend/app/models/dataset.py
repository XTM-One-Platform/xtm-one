# SPDX-License-Identifier: Apache-2.0

"""Dataset models – reusable, shareable test case collections for evaluations."""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Dataset(Base):
    """A reusable collection of test cases that can be linked to multiple evaluations."""

    __tablename__ = "datasets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Three-tier visibility
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
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # Display
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set only on a built-in dataset, by the startup seed (app/evaluations/seed.py),
    # which matches its rows by it; NULL on every user-created dataset.
    slug: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)

    # Global eval instruction for all entries; per-entry evaluation_prompt overrides it
    evaluation_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The family the entries are shaped for, from the catalog
    # ``Evaluation.family`` uses (``family_is_valid``). NULL is a flagged problem
    # (``needs_family``), never a steady state, and is never inferred.
    family: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # CLASSIFICATION only: every label an answer may hold, possibly more
    # than the references use. Entries are checked against it on write, so the
    # engine takes it as the complete label set.
    label_vocabulary: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # The shape of every expected answer, ``{"type": "json_schema",
    # "definition": ...}``; ``regex`` and ``tool_sequence`` are refused
    # (``validate_output_schema``).
    # On the dataset, as it describes the ground truth every evaluation reusing it
    # shares; per-field scoring intent stays on the evaluation. An entry's own
    # ``output_schema`` wins.
    output_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # SHA-256 of canonical entry content (sorted by position).
    # NULL while dataset is being actively edited. UNIQUE constraint.
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )

    # Blocks entry mutations for non-admins
    is_read_only: Mapped[bool] = mapped_column(Boolean, default=False)

    # Built by the Platform Health from production (#8257): owned by no user,
    # visible to administrators; its entries' sources are in
    # ``concierge_dataset_entry_sources``. Set only by server code.
    concierge_built: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )

    # The latest ``DatasetVersion`` (``services/evaluation/dataset_versioning.py``);
    # 0 = never versioned.
    current_version: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa.text("0"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class DatasetEntry(Base):
    """A single test case within a dataset."""

    __tablename__ = "dataset_entries"
    # An evaluation package names an entry by its position.
    __table_args__ = (
        Index("uq_dataset_entries_position", "dataset_id", "position", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_prompt: Mapped[str | None] = mapped_column(Text, nullable=True, default="")
    expected_answer: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)

    # This entry's answer shape, as ``Dataset.output_schema``. Resolved entry,
    # then dataset version, then none; never from the evaluation.
    output_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class EvaluationDataset(Base):
    """Join table linking datasets to evaluations (ordered)."""

    __tablename__ = "evaluation_datasets"
    __table_args__ = (
        UniqueConstraint("evaluation_id", "dataset_id", name="uq_eval_dataset"),
        # An evaluation package names a dataset by its position.
        Index("uq_evaluation_datasets_position", "evaluation_id", "position", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    evaluation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Display order when multiple datasets are linked to one evaluation
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
