# SPDX-License-Identifier: Apache-2.0

"""PlatformProcedureManifest model - the procedures a registered OpenCRQ serves.

One row per registration (``registration_id`` is the key, ``ON DELETE CASCADE``), kept
apart from ``platform_registrations`` so the procedure list, which runs to hundreds of KB,
is never loaded with the registration rows the heartbeat and the settings page list.

``procedures`` is ``path -> {"type": "query" | "mutation", "input": <reduced JSON Schema> | None}``,
already reduced by ``providers/opencrq/procedure_manifest.reduce_schema`` at intake. It is
written only by ``POST /platform/register`` (``api/v1/platform._record_procedure_manifest``)
and read by ``core/platform_identity.registered_procedure_manifest``, which hands it to a tool
build only while ``fingerprint`` is the one the registration last reported
(``PlatformRegistration.procedure_manifest_fingerprint``).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlatformProcedureManifest(Base):
    __tablename__ = "platform_procedure_manifests"

    registration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("platform_registrations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    #: The instance's own fingerprint of the document (sha256 hex) the procedures were read from.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    procedures: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
