# SPDX-License-Identifier: Apache-2.0

"""Report model – agent-generated executive reports.

A ``Report`` stores a validated ``ReportSpec`` (see ``app.services.report_spec``):
a structured JSON description of a document (header, ordered blocks with a
controlled ``block`` type each, footer). The frontend renders it with one React
component per block; the agent never authors HTML for a whole page, only the
structured spec plus optional per-block ``rich_text`` fragments that are
sanitized with DOMPurify at render time.

Why a dedicated model rather than a ``FileAttachment``
-----------------------------------------------------
Same reasoning as ``Presentation``, for the same reason: ``generate_file``
artifacts are immutable blobs in S3 addressed by object key, so they cannot keep
a **stable identity across turns**. A report is iterated on — the agent adds a
scenario, re-scores the callout, appends a source — and the rendered document has
to update in place rather than pile up as five near-identical HTML files. Keeping
the spec in a JSON column lets ``generate_report`` rewrite the same row (bumping
``revision``), so the chat marker ``[[REPORT:<id>]]`` stays valid and the React
component reconciles the block list instead of remounting. The row is also the
natural place to hold the optional link to the ``TemplateContainer`` the
report draws its brand palette from.

Nothing binary lives here: a report references no images of its own, and its
palette is resolved from the linked template at read time (visibility re-checked
on every read), so deleting a template never breaks a report built from it.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Report(Base):
    """One agent-generated report, editable in place across conversation turns."""

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Owner of the report. NULL only if the user is later deleted (SET NULL),
    # which matches ``Presentation`` / ``FileAttachment``: the artifact survives
    # its author, and the API then only serves it to admins.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Chat context the report was generated in (NULL for assignment runs).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Assignment run that produced the report (NULL in plain chat). A scheduled
    # weekly brief is the obvious use case, and without this stamp the document
    # it produces would be orphaned — neither conversation- nor run-scoped.
    assignment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Optional template the report draws its brand palette from. SET NULL
    # rather than CASCADE: deleting a template must never delete reports built
    # from it — they keep rendering with their built-in theme.
    template_container_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("template_containers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Denormalized from ``spec`` for list views and chat cards, so rendering a
    # report chip never requires parsing the whole spec.
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    theme: Mapped[str] = mapped_column(String(50), nullable=False, default="light")
    block_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=sa.text("0")
    )

    # Bumped on every in-place edit. Surfaced to the frontend so the report
    # component can tell "same report, new blocks" from "a different report".
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=sa.text("1")
    )

    # The validated ``ReportSpec`` (JSON-mode dump). Never raw agent input:
    # ``app.services.report_spec.validate_spec`` normalizes it first and rejects
    # unknown blocks, oversized documents, ragged tables, non-http source links
    # and rich-text fragments carrying scripts / event handlers.
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
