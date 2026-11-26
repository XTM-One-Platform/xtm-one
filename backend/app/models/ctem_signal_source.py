# SPDX-License-Identifier: Apache-2.0

"""Platform signal sources: the objects a source platform calls XTM One through (#2434).

A source of a trigger path (``services/ctem_signal_sources.PATH_SOURCES``) that
XTM One provisions in the source platform (the OpenAEV listener, the OpenCTI
PIR playbook, the OpenCTI live listener) is one row per ``(provider, kind,
integration_id)``. It is a platform object, never held by a fleet assignment:

- ``enabled`` starts false: a fresh platform starts with every source off;
- ``run_as_user_id`` is the identity whose connection provisions it, chosen
  when it is switched on (``services/ctem_platform_sources.enable``);
- ``state`` is the provisioning state the provider module writes (object ids,
  fingerprint, ``status`` provisioned / error, ``error``, ``checked_at``): the
  Sources page reads Connected only from a provisioned state whose last check
  passed;
- deliveries reach ``/api/hooks/integrations/{integration_id}/signal-sources/{id}``,
  which records the signal; fleets subscribe through ``ctem_signal.recorded``.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _now() -> datetime:
    return datetime.now(UTC)


class CtemSignalSource(Base):
    __tablename__ = "ctem_signal_sources"
    __table_args__ = (
        UniqueConstraint("provider", "kind", "integration_id", name="uq_ctem_signal_sources_provider_kind_integration"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    #: ``listener`` or ``pir_playbook`` (``ctem_signal_sources.SOURCE_KINDS``).
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    run_as_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
