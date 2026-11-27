# SPDX-License-Identifier: Apache-2.0

"""A verified signal-source delivery waiting to be recorded (#2570).

The signal-source webhook (``api/v1/webhooks.py:receive_signal_source_webhook``)
writes one row per verified delivery and answers at once: OpenCTI never retries a
webhook, so a delivery answered 429/503 under load is lost, and one held while the
platform records it holds a request slot and a pool connection for the whole
recording. ``services/ctem_signal_inbox.py`` drains the rows off the request path
(the autonomous tick), oldest first, and deletes each once recorded.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CtemSignalInbox(Base):
    __tablename__ = "ctem_signal_inbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: ``opencti`` or ``openaev``: the recorder the delivery goes to.
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    integration_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The normalised event key (``_extract_event_key`` / ``_normalize_openaev_payload``).
    event_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    #: The normalised payload, as the recorders read it.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: Signed with the integration's webhook secret.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Drain attempts that raised; a row past ``MAX_ATTEMPTS`` is dropped with a log line.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
