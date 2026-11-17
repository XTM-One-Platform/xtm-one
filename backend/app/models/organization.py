# SPDX-License-Identifier: Apache-2.0

"""Organization model - the primary organization and the organizations it tracks (#9157).

The CTEM covers the platform's own organization (``role`` ``primary``,
exactly one, kept by a partial unique index) and the organizations it
depends on or serves (``role`` ``tracked``: a provider, a customer, a
partner, a subsidiary). Each one owns its versioned profile in
``organization_context_versions`` (``organization_id``). Design:
``dev-docs/ctem-fleets.md`` section 9.

A tracked organization starts with ``monitoring`` ``off``: nothing is
provisioned and no run is metered until an administrator enables it
(section 9.7). Archiving keeps the row and its versions (``archived_at``).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ORGANIZATION_ROLES: tuple[str, ...] = ("primary", "tracked")
#: What a tracked organization is to the primary one (``None`` for the primary).
ORGANIZATION_RELATIONSHIPS: tuple[str, ...] = ("provider", "customer", "partner", "subsidiary")
ORGANIZATION_CRITICALITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
ORGANIZATION_MONITORING: tuple[str, ...] = ("off", "on")


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("role IN ('primary', 'tracked')", name="ck_organizations_role_valid"),
        CheckConstraint(
            "(role = 'primary' AND relationship IS NULL) OR "
            "(role = 'tracked' AND relationship IS NOT NULL"
            " AND relationship IN ('provider', 'customer', 'partner', 'subsidiary'))",
            name="ck_organizations_relationship_valid",
        ),
        CheckConstraint(
            "criticality IN ('low', 'medium', 'high', 'critical')",
            name="ck_organizations_criticality_valid",
        ),
        CheckConstraint("monitoring IN ('off', 'on')", name="ck_organizations_monitoring_valid"),
        CheckConstraint(
            "daily_run_cap >= 1 AND daily_run_cap <= 100", name="ck_organizations_daily_run_cap_valid"
        ),
        Index(
            "uq_organizations_one_primary",
            "role",
            unique=True,
            postgresql_where=text("role = 'primary'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="tracked")
    relationship: Mapped[str | None] = mapped_column(String(16), nullable=True)
    criticality: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium", server_default=text("'medium'")
    )
    #: The OpenCTI ``Organization`` entity it is linked to (M3), when linked.
    opencti_organization_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    monitoring: Mapped[str] = mapped_column(
        String(8), nullable=False, default="off", server_default=text("'off'")
    )
    #: Runs its OpenCTI events may start per day (UTC, M6); the extra events
    #: are held and run the next day as one digest (``services/organization_run_cap``).
    daily_run_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default=text("5"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=text("now()")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
