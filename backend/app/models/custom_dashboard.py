# SPDX-License-Identifier: Apache-2.0

"""CustomDashboard model - a dashboard an agent built for one person.

A ``CustomDashboard`` stores a validated ``DashboardSpec`` (see
``app.services.custom_dashboard_spec``): a title and an ordered list of widgets,
each of a controlled ``kind``. The frontend draws one design-system component
per widget; the agent never authors markup, only the structured spec.

Same shape as ``Report``, for the same reason: a dashboard is iterated on (the
agent refreshes a figure, adds a widget, drops one), so it keeps a **stable id
across turns** and runs. The ``[[DASHBOARD:<id>]]`` marker already sitting in a
conversation stays valid, and the page a person bookmarked shows the latest
revision. A scheduled assignment can refresh the same dashboard every morning.

It is the person's own until they share it, as a deck is (three-tier
visibility, ``VisibilityColumns``): ``read_only`` grantees open it, ``config``
grantees also edit it, a company-managed dashboard is read by everyone and
edited by admins. Every write is kept as a ``CustomDashboardVersion``, so a
change by an agent, a colleague or a restore can be taken back.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.attributes import set_committed_value

from app.core.database import Base
from app.models.visibility import VisibilityColumns


class CustomDashboard(VisibilityColumns, Base):
    """One agent-built dashboard, editable in place across turns and runs."""

    __tablename__ = "custom_dashboards"
    __table_args__ = (Index("ix_custom_dashboards_is_company_managed", "is_company_managed"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Owner. NULL only once the user is deleted (SET NULL, as a report).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The conversation the dashboard was built in (NULL for an assignment run):
    # "Refine with the assistant" reopens it.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The assignment run that last wrote it (a scheduled refresh), NULL in chat.
    assignment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Denormalized from ``spec`` for list views and chat cards.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    widget_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))

    # Bumped on every write; an edit names the revision it read (optimistic
    # concurrency between the person and a refreshing agent).
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=sa.text("1"))

    # The validated ``DashboardSpec`` (JSON-mode dump), never raw agent input.
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # What the person files it under, lower-cased (``custom_dashboards.clean_tags``): the list sorts and filters by them.
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=sa.text("'[]'"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class CustomDashboardVersion(Base):
    """A dashboard's spec at one revision, as it was saved.

    The dashboard row's ``revision`` and this table's are the same counter, so
    the history, the 409 of an edit and the agent's tool result name the same
    number. Restoring writes the old spec as a NEW revision (``source =
    "restore"``): the history is append-only and a restore is undoable. The
    newest ``DASHBOARD_VERSION_RETENTION`` entries are kept per dashboard.
    """

    __tablename__ = "custom_dashboard_versions"
    __table_args__ = (
        UniqueConstraint("dashboard_id", "revision", name="uq_custom_dashboard_versions_revision"),
        Index("ix_custom_dashboard_versions_dashboard_created", "dashboard_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("custom_dashboards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # Denormalized for the history list, which shows headlines only.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    widget_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    # The complete validated spec as saved: what a restore puts back.
    spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # ``agent`` / ``manual`` / ``restore`` / ``import`` (``custom_dashboards.VERSION_SOURCES``).
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # One line saying what changed ("Restored revision 3", the touched widgets).
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    restored_from_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class CustomDashboardPublication(Base):
    """A dashboard sent to OpenCTI or OpenAEV as one of their own dashboards.

    What the platform answered (``remote_id``, and the address built from the
    integration's base URL) and which revision was sent, by whom: the
    dashboard page lists where it was published. The platform's copy is its
    own from then on - XTM One never writes to it again.
    """

    __tablename__ = "custom_dashboard_publications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("custom_dashboards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The integration it went through (no foreign key: the record outlives it).
    integration_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    #: ``opencti`` / ``openaev``.
    target: Mapped[str] = mapped_column(String(20), nullable=False)
    remote_id: Mapped[str] = mapped_column(String(200), nullable=False)
    remote_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class CustomDashboardPublicLink(Base):
    """The published link of one dashboard: anyone holding the URL sees it, without signing in.

    What the link shows is a snapshot (``snapshot``): each visualization's and
    data source's data, read on the server with the access of the person who
    published it (``created_by_user_id``), at most every ``refresh_minutes``
    - never live, never with a visitor's credentials. The link answers only
    while its publisher may still read the dashboard.

    The token is kept like a deck's public link: hashed for the lookup,
    encrypted so a manager can copy the link again. Regenerating rewrites it
    in place; turning the link off deletes the row.
    """

    __tablename__ = "custom_dashboard_public_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("custom_dashboards.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # ``security.hash_token`` of the URL token: what a public request is looked up by.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # ``security.encrypt_value`` of the same token (invariant 2), read back only for a manager.
    token_enc: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Who published it: the snapshot is read with their access.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    #: How old the snapshot may grow before a visit reads the data again.
    refresh_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default=sa.text("60"))
    #: The highest markings the snapshot shows (``TLP:AMBER``...): OpenCTI's data marked above them, or with a marking
    #: of a type not named, is left out; empty, no ceiling.
    marking_ceiling: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=sa.text("'[]'"))
    #: ``{"widgets": {id: data}, "sources": {id: data}}``, as the page draws them.
    snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The dashboard's revision the snapshot was read for: an edit reads it again.
    snapshot_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ── Dashboards stored before the native form ─────────────────────────────────


def _read_stored_spec(target: Any) -> None:
    """A row stored before today's form (version 1 or 2, rows of 36 px) loads as today's manifest.

    Committed, not dirty: loading a row never writes it back; its next save
    stores the manifest (``dashboard_native.current_form``).
    """
    from app.services.dashboard_native import current_form

    spec = target.__dict__.get("spec")
    read = current_form(spec)
    if read is not spec:
        set_committed_value(target, "spec", read)


@event.listens_for(CustomDashboard, "load")
@event.listens_for(CustomDashboardVersion, "load")
def _load_spec(target: Any, _context: Any) -> None:
    _read_stored_spec(target)


@event.listens_for(CustomDashboard, "refresh")
@event.listens_for(CustomDashboardVersion, "refresh")
def _refresh_spec(target: Any, _context: Any, attrs: Any) -> None:
    if attrs is None or "spec" in attrs:
        _read_stored_spec(target)
