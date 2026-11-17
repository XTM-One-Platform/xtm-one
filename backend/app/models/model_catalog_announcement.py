# SPDX-License-Identifier: Apache-2.0

"""ModelCatalogAnnouncement - the outbox of "this model is now yours" emails.

``env`` rows: one per recorded state of the env-managed model catalog. The
latest one is the catalog the platform last knew about; a boot that finds the
env seed moved it records the new state here. ``admin`` rows: an admin
chose to email the users about the default model from Settings. The scheduler
emails the users from the rows left ``pending``. Who was emailed for a row is
one ``ModelCatalogAnnouncementRecipient`` per user, written BEFORE the send.
Both halves live in ``services/model_catalog_announcements.py``.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ModelCatalogAnnouncement(Base):
    __tablename__ = "model_catalog_announcements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=text("now()")
    )
    # ``env`` (the env seed changed the catalog) | ``admin`` (an admin chose to
    # announce the default model). An ``admin`` row carries no catalog, only the
    # default before and after.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="env", server_default="env")

    # The catalog before and after the change: ``[{"id": ..., "provider": ...}]``
    # and the default model id. The email is derived from the pair, never
    # stored, so a folded change (a second one before the first was sent)
    # recomputes one consistent diff.
    previous_models: Mapped[list | None] = mapped_column(JSON, nullable=True)
    previous_default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    models: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The image, video, speech, music and decision models the environment pins,
    # before and after: ``[{"capability": ..., "model": ..., "provider": ...}]``.
    # NULL on a row recorded before they were tracked, and on an admin row.
    previous_capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    capabilities: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # baseline | recorded | pending | sending | sent | skipped | superseded |
    # expired | failed - see the module docstring of the service.
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Consecutive batches that got no email out and either stopped on an SMTP
    # failure or met only addresses refused for now: the addresses a stopped
    # batch refused do not break the streak, an email out does. The row gives up
    # at ``MAX_ATTEMPTS``.
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # The claim of the worker sending a ``sending`` row: every write it makes
    # while it holds the claim names it, so a worker whose lease ran out cannot
    # overwrite the row another worker claimed since.
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # A ``pending`` row is not claimed before this instant (back-off); a
    # ``sending`` row's claim expires at it (a worker died mid-batch).
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The last failed batch's error, or the server's last reply to an address it
    # refused for now; on a ``failed`` row, why it failed. Cleared when the row
    # is ``sent``.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ModelCatalogAnnouncementRecipient(Base):
    """One user an announcement was (or is being) emailed to.

    Written before the send and never removed while the send may have happened,
    so a user is emailed at most once per announcement whatever crashes: a row
    left ``sending`` (the worker died between the reservation and the outcome)
    is settled ``unknown``, never sent again. No foreign key to ``users``: the
    row records an email already gone, and a key would lock ``users`` in the
    migration.
    """

    __tablename__ = "model_catalog_announcement_recipients"

    announcement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("model_catalog_announcements.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # sending | delivered | refused | unknown | deferred (the server refused the
    # address for now: retried once, while the announcement is still open)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), server_default=text("now()")
    )
