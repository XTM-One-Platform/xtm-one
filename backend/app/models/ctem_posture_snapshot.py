# SPDX-License-Identifier: Apache-2.0

"""CtemPostureSnapshot model - per-user, per-vertical daily exposure posture history.

One row per (user, vertical, day): the composite Exposure Posture Score plus, for
EVERY CTEM stage, its score and the key numeric metrics that drive stage readiness
and efficiency (e.g. attack_surface_pct, threat_informed_coverage_pct,
prevention_success_pct, case_closure_pct). This lets the command center chart the
historical evolution of all five phases - not just the headline score - and show
period-over-period deltas. Upserted on each live posture build (one row per day).
The row also keeps the per-object values behind the briefing's risk movers.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CtemPostureSnapshot(Base):
    __tablename__ = "ctem_posture_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "vertical",
            "snapshot_date",
            name="uq_ctem_snapshot_user_vertical_date",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vertical: Mapped[str] = mapped_column(String(20), nullable=False, default="cyber")
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Composite Exposure Posture Score (0-100) or NULL when nothing was scorable.
    posture_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # {stage_key: {"score": int|null, "<numeric metric>": value, ...}} for the five
    # CTEM stages - the score plus the readiness/efficiency metrics that drive it.
    stage_metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # {"<kind>:<id>": {"label": str, "value": float}}: the per-object values the
    # risk movers compare between two days (services/ctem_movers.py); NULL on the
    # rows recorded before it existed and on a day whose first build measured
    # nothing. none_as_null: a Python None is stored as SQL NULL, never as the
    # JSON value null, so the movers' IS NOT NULL filter skips that row.
    objects: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
