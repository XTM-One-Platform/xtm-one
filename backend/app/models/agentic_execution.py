# SPDX-License-Identifier: Apache-2.0

"""AgenticExecutionDaily model – durable per-day count of agentic executions.

One row per (UTC day, source): how many **top-level agentic executions** the
install performed that day.  An agentic execution is one logical request that
runs the agentic loop — a chat turn, a General Assistant turn, an assignment
run, a background sub-agent task, a browser-extension task — counted exactly
once regardless of how many agents it fans out to (nested consult / transfer
handovers are part of their parent execution, not executions of their own).

Counters are incremented in Redis on the hot path and periodically flushed
here by the autonomous scheduler (see ``services/agentic_executions.py``).
This table is the durable source read by the anonymous usage telemetry
(``xtm_one_agentic_executions_count`` / ``xtm_one_agentic_executions_by_day``).

Rows are never purged: the cumulative gauge is an all-time total and the grain
is tiny (one row per day per source, a handful per day).
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Storage width of the ``source`` column.  Exported so the writer can normalize
# a source to this width BEFORE it becomes part of a storage key — two sources
# that only differ past the cut must land in the SAME row, or the flush's
# ``ON CONFLICT DO UPDATE`` would touch one row twice and abort.
SOURCE_MAX_LENGTH = 30


class AgenticExecutionDaily(Base):
    __tablename__ = "agentic_execution_daily"
    __table_args__ = (
        UniqueConstraint(
            "day",
            "source",
            name="uq_agentic_execution_daily_day_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # UTC day the executions were performed on.
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Entry point that produced the executions – one of the ``SOURCE_*``
    # constants in ``services/agentic_executions.py``.
    source: Mapped[str] = mapped_column(String(SOURCE_MAX_LENGTH), nullable=False)

    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
