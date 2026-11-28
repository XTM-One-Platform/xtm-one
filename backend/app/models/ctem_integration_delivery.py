# SPDX-License-Identifier: Apache-2.0

"""When a verified signal-source delivery last reached XTM One, per integration (#2736).

The signal sources drawer names each integration a source reads with its last
delivery: "On, no delivery yet" and "On" say different things to a person.
``services/ctem_signal_sources.note_delivery`` writes one row per ``(provider,
integration_id)`` from every verified delivery ``ctem_signal_listeners.note_event``
sees, at most once a minute per integration; ``provider_integrations`` reads it.
``integration_id`` is the integration's id, or ``oauth_default`` for the
provider-level route (``ctem_signal_sources.OAUTH_DEFAULT_INTEGRATION``).
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CtemIntegrationDelivery(Base):
    __tablename__ = "ctem_integration_deliveries"

    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    integration_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_delivery_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
