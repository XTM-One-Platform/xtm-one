# SPDX-License-Identifier: Apache-2.0

"""CtemSurfacePass: one committed attack surface pass of CTEM-26 (#1498, ``dev-docs/ctem-attack-surface.md``).

One row per committed pass: when it ran, which providers it read, what each
provider read per owned domain or range (pages, records, whether the read hit
its page bound), the status that read gives each scope (``complete``,
``truncated`` or ``empty``), the hosts observed and the signals per path. The
next pass reads the latest row to decide whether a host missing from the
baseline is really new (path 7) or only one the previous read did not cover;
an operator or an agent reads the rows to confirm that a pass ran and what it
compared with, including a pass that found nothing.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class CtemSurfacePass(Base):
    __tablename__ = "ctem_surface_passes"
    __table_args__ = (Index("ix_ctem_surface_passes_org_at", "organization_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    #: When the pass ran (the ``observed_at`` of its signals).
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    #: The provider label of the pass (``shodan``, ``shodan+gti``...).
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: ``[{"provider", "scope", "pages", "records", "truncated"}]``: every read of the pass.
    coverage: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: ``{"<provider>|<scope>": "complete" | "truncated" | "empty"}``: what the next pass compares with.
    statuses: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    #: Signals recorded per trigger path, plus ``new_host_withheld`` (hosts that joined the baseline silently).
    counts: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    #: When the pass this one compared with ran; ``None`` for the first recorded pass.
    previous_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
