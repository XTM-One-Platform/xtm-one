# SPDX-License-Identifier: Apache-2.0

"""Immutable dataset snapshots that runs score against (``services/evaluation/dataset_versioning.py``)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DatasetVersion(Base):
    """Immutable snapshot header of a dataset at a point in time."""

    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "version_number", name="uq_dataset_version_number"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Matches ``datasets.current_version`` at snapshot time.
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # SHA-256 of canonical entry content (same canonicalization as Dataset).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ``Dataset.output_schema`` as the version was cut. Runs read the
    # snapshot, never the live dataset, which would silently re-describe every
    # past run (pinned by ``tests/test_dataset_versioning.py``).
    output_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # ``Dataset.label_vocabulary`` as the version was cut. No family
    # snapshot: a run takes its family from the evaluation.
    label_vocabulary: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class DatasetVersionEntry(Base):
    """Immutable per-entry copy within a version."""

    __tablename__ = "dataset_version_entries"
    __table_args__ = (
        # Deleting a live entry sets ``entry_id`` NULL on its copies: the index finds them.
        Index(
            "ix_dataset_version_entries_entry_id",
            "entry_id",
            postgresql_where=text("entry_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The live entry; the snapshot outlives its deletion.
    entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dataset_entries.id", ondelete="SET NULL"),
        nullable=True,
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_answer: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_ids: Mapped[list | None] = mapped_column(JSON, nullable=True, default=None)
    output_schema: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
