# SPDX-License-Identifier: Apache-2.0

"""The key/value conditions a run executed under (model, mode, scoring, dataset version...).

On a key logged twice, ``manual`` wins over ``evaluation``, which wins over ``auto``, at upsert.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EvaluationRunParam(Base):
    """A single key/value experimental condition logged for a run."""

    __tablename__ = "evaluation_run_params"
    __table_args__ = (
        UniqueConstraint("run_id", "key", name="uq_eval_run_param_key"),
        CheckConstraint(
            "source IN ('auto', 'evaluation', 'manual')",
            name="ck_eval_run_params_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("evaluation_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    # Scalar or JSON-encoded value.
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
