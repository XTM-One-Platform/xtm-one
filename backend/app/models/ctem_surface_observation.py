# SPDX-License-Identifier: Apache-2.0

"""CtemSurfaceObservation: the outside lens baseline of CTEM-26 (``dev-docs/ctem-attack-surface.md``).

One row per (organization, host): what the last attack surface pass saw of a
public name or address through a connected provider (the ports that answer,
the certificate names, the DNS target), when it was first and last seen, and
the signal keys already raised for it, so two identical passes record no new
signal. The pass diffs a new observation against these rows in code.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class CtemSurfaceObservation(Base):
    __tablename__ = "ctem_surface_observations"
    __table_args__ = (UniqueConstraint("organization_id", "host", name="uq_ctem_surface_observations_org_host"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The public name (lower-case, no trailing dot) or, for an unnamed address, the address.
    host: Mapped[str] = mapped_column(String(300), nullable=False)
    #: The address the host answered on at the last sighting (empty: none).
    ip: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: ``[{"port": int, "transport": str, "product": str}]`` that answered at the last sighting.
    ports: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    #: Certificate subject and alternative names seen on the host.
    cert_names: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: What the name resolves to (an address or a CNAME target); empty when it resolves to nothing.
    dns_target: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    source_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    #: The signal dedupe keys already raised for this host and still holding.
    signalled: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
