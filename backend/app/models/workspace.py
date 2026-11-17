# SPDX-License-Identifier: Apache-2.0

"""Workspace model — a named environment that scopes tools and conversations.

A **Workspace** is the container the platform has been missing: a named,
user-visible environment that two agents can be pointed at so they share a
blackboard, a work queue and a working corpus, and that agents in different
workspaces cannot see across.

What a Workspace scopes:

* ``workspace`` is a value of the coordination scope vocabulary
  (``app.core.scopes``), alongside ``run`` / ``flow`` / ``global``;
* ``shared_states``, ``worksets`` and ``work_items`` carry ``workspace_id``,
  with partial unique indexes that let two *people* share one workspace's tool
  state rather than only two agents acting for one person;
* ``conversations`` carry ``workspace_id``;
* ``workspace_knowledge_bases`` (:class:`WorkspaceKnowledgeBase` below) binds
  knowledge bases to a workspace;
* ``instructions``, ``workspace_skills`` (:class:`WorkspaceSkill`) and the
  knowledge bases a workspace owns (``knowledge_bases.owner_workspace_id``)
  reach every agent turn in a conversation filed here
  (``services.workspace_knowledge``).

## Why the uniqueness is partial

Three invariants are enforced in the database rather than in application code,
because each can be violated by two requests racing on different replicas:

* at most one **default** workspace per owner;
* a **slug** that is unique per owner;
* the default is never **company-managed**
  (``ck_workspaces_default_not_company_managed``): an admin making a workspace
  company-managed while its owner makes it their default would otherwise each
  pass the service rule on the row they read, and both writes land.

The first two are PARTIAL indexes excluding ``user_id IS NULL``. That exclusion is
stated rather than relied upon: NULLs are distinct in Postgres, so an
ownerless row would never collide on a plain unique index anyway — writing the
predicate makes the gap visible instead of leaving a future reader to assume
ownerless rows are constrained when they are not.

## Visibility

The complete three-tier column set is present and required, not optional.
``app.api.v1.helpers.user_visible_filter`` is generic over every entity
carrying ``shared_group_ids`` and references ``shared_user_ids`` /
``access_restricted`` unconditionally on its **non-admin** branch — so an
entity queried through it and missing a column raises at query-build time for
ordinary users while admins see nothing wrong. That is how ``flows``,
``flow_event_types`` and ``nova_rules`` each shipped 500ing for every
non-admin caller. ``Workspace`` is registered in the ``CompanyManagedEntity``
union for the same reason, and its list endpoint is exercised as a non-admin
in the tests.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

#: Lucide icon used when the creator picks none.
WORKSPACE_DEFAULT_ICON = "LayoutGrid"

#: Name and slug of the workspace every user gets on first use.
WORKSPACE_DEFAULT_NAME = "General"
WORKSPACE_DEFAULT_SLUG = "general"


class Workspace(Base):
    """A named environment scoping an owner's tools, conversations and resources."""

    __tablename__ = "workspaces"
    __table_args__ = (
        Index("ix_workspaces_user_id", "user_id"),
        Index("ix_workspaces_is_company_managed", "is_company_managed"),
        Index("ix_workspaces_is_archived", "is_archived"),
        # At most one default per owner. Enforced here rather than in the
        # service so two concurrent first-use requests on different replicas
        # cannot both insert one — the loser gets an IntegrityError and reads
        # the winner's row (see `services.workspaces.ensure_default_workspace`).
        Index(
            "uq_workspaces_default_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_default IS TRUE AND user_id IS NOT NULL"),
        ),
        # A slug is the human-facing handle; nothing persisted references it
        # (the workspace is referenced by id everywhere), so uniqueness is per
        # owner rather than global — two people may each have a "research".
        Index(
            "uq_workspaces_slug_per_user",
            "user_id",
            "slug",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        # The default is where its owner lands; company-managed, everyone
        # would see it and only admins could change it. The service refuses
        # both directions (`ensure_can_be_company_managed`,
        # `make_default_workspace`) with a reason, on a row read FOR UPDATE;
        # this is what holds when a write reaches the table another way.
        CheckConstraint(
            "NOT (is_default AND is_company_managed)",
            name="ck_workspaces_default_not_company_managed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: Owner. NULL only for an ownerless/company-seeded row, which is outside
    #: both partial uniques above — see the module docstring.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Every column below that has a DDL default in the migration carries the
    # same `server_default` here, and that parity is load-bearing rather than
    # tidy. SQLAlchemy never emits a Python-side `default=` as DDL, so a
    # column with only one gets `NOT NULL` and NO default on a fresh install
    # (`create_all`) while an upgraded database has the default the migration
    # wrote. Any INSERT that skips the ORM — a later migration's backfill, a
    # seed script, plain SQL in a fixture — then succeeds on one and raises a
    # NOT NULL violation on the other. Environment-dependent, which is the
    # hardest kind to reproduce.
    #
    # Six columns drifted this way and only three were reported; the other
    # three (`description`, `icon`, `color`) came out of comparing the two
    # install paths against a real database rather than reading the files.
    # `test_migrated_schema_matches_create_all_including_indexes` now compares
    # column defaults too, so the drift cannot come back quietly.
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Derived from the name on create, then stable: renaming a workspace does
    #: NOT re-slug it. A slug that follows the name would break any link or
    #: bookmark pointing at it, and buys nothing — the id is what everything
    #: persisted refers to.
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    icon: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=WORKSPACE_DEFAULT_ICON,
        server_default=WORKSPACE_DEFAULT_ICON,
    )
    #: Free-form colour token for the UI (hex or a Tailwind name). Empty means
    #: "let the UI decide" rather than a stored default that would freeze one.
    color: Mapped[str] = mapped_column(
        String(30), nullable=False, default="", server_default=""
    )
    #: Instructions every agent answering in a conversation filed here
    #: receives. Bounded by the API schema, not the column.
    instructions: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    #: The one a user lands in when nothing else is chosen. Exactly one per
    #: owner, guaranteed by `uq_workspaces_default_per_user`.
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    #: Archived workspaces stay readable and keep their contents; they are
    #: hidden from pickers. Whether an archived workspace still serves its
    #: tool state is an open question and belongs to the increment
    #: that adds the scope — nothing here decides it.
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # ── Three-tier visibility (see the module docstring) ─────────────────
    is_company_managed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class WorkspaceKnowledgeBase(Base):
    """A knowledge base bound to a workspace.

    A binding, not ownership. The knowledge base keeps its own owner and its
    own three-tier visibility; this row only says "this workspace works with
    that corpus". So both sides CASCADE: deleting either end removes the
    binding and nothing else. Deleting a workspace must never delete somebody
    else's knowledge base, and deleting a knowledge base must not leave a
    binding pointing at nothing.

    The pair is unique. Binding the same corpus twice is a no-op the user
    expects to be idempotent, not a way to stack duplicate rows.
    """

    __tablename__ = "workspace_knowledge_bases"
    __table_args__ = (
        Index(
            "uq_workspace_knowledge_bases_pair",
            "workspace_id",
            "knowledge_base_id",
            unique=True,
        ),
        Index("ix_workspace_knowledge_bases_kb", "knowledge_base_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        # Matches the migration. See the note on `Workspace` above for why a
        # Python-side default alone leaves a fresh install with no DDL default.
        server_default=func.now(),
    )


class WorkspaceSkill(Base):
    """A library skill linked to a workspace.

    A link, like :class:`WorkspaceKnowledgeBase`: the skill keeps its owner and
    visibility, and each agent turn in the workspace offers it only to a person
    who can see it. Deleting either end removes the link and nothing else.
    """

    __tablename__ = "workspace_skills"
    __table_args__ = (
        Index("uq_workspace_skills_pair", "workspace_id", "skill_id", unique=True),
        Index("ix_workspace_skills_skill", "skill_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_skills.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class WorkspaceConversationIndex(Base):
    """Where a filed conversation stands in its workspace's conversation index.

    One row per conversation, kept here rather than on ``conversations``:
    every write to a conversation row bumps its ``updated_at`` and reorders the
    sidebar. The row names the workspace the conversation was indexed INTO, so
    a conversation moved to another workspace is re-indexed there and its old
    document removed (``services.workspace_conversation_index``).
    """

    __tablename__ = "workspace_conversation_indexes"
    __table_args__ = (
        Index("ix_workspace_conversation_indexes_workspace", "workspace_id"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The conversation's one document in the Conversations knowledge base.
    knowledge_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: ``MAX(messages.sequence)`` the document covers.
    indexed_through_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: ``indexed`` | ``skipped`` (nothing a person wrote) | ``failed``.
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Logged reason of a failure; never served to other members.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
