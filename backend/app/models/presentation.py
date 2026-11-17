# SPDX-License-Identifier: Apache-2.0

"""Presentation model – agent-generated interactive slide decks.

A ``Presentation`` stores a validated ``PresentationSpec`` (see
``app.services.presentation_spec``): a structured JSON description of a deck
(title, theme, ordered slides with a controlled ``layout`` each). The frontend
renders it with ``@revealjs/react``; the agent never authors HTML — every
human-readable string is flat inline markup rendered through a tokenizer.

The ``spec`` column of the deck AND of its versions is a :class:`SpecJSON`: a
plain JSON column whose READ hook upgrades a slide still wearing a retired v1
layout (``app.services.presentation_legacy``) to the current catalog.
The schema is unchanged and there is no migration — a stored v1-era row is
served upgraded and written back as v2 on its next intentional save, the same
way the v1 ``filigran`` theme alias is read as ``dark``.

Why a dedicated model rather than a ``FileAttachment``
-----------------------------------------------------
``generate_file`` artifacts are immutable blobs in S3 addressed by object key.
A deck must instead keep a **stable identity across turns**: the acceptance
criteria require an agent to add, remove or reorder slides and the
rendered deck to update in place rather than be replaced by a second deck.
Keeping the spec in a JSON column lets ``generate_presentation`` update the
same row (bumping ``revision``), so the chat marker ``[[PRESENTATION:<id>]]``
stays valid and the React component reconciles the slide list instead of
remounting. The row is also the natural place to hold the optional link to the
``TemplateContainer`` the deck draws its theme and assets from.

Slide images are NOT copied in here: a slide references an existing
``FileAttachment`` by id (a ``template_asset`` from a container, or an artifact
the agent produced earlier) and the frontend fetches it through the regular
authenticated file-download endpoint, so file visibility rules keep applying.

Sharing
---------------
A deck carries the platform's three-tier visibility column set — the same one
every shareable entity has — so it is queried through ``user_visible_filter``
like a template container is: own + user-shared + group-shared +
company-managed. That is what lets one person ask their assistant to draft a
deck, share the link, and let a colleague keep working on it with *their own*
agent: a ``config``-tier grant makes the deck writable for the grantee, both on
the REST surface and through the ``generate_presentation`` tool. A
``read_only`` grant lets them present and export it, not change it. A
company-managed deck is the published, read-only-for-everyone form (the
all-hands deck once it is final).

Collaboration
---------------------
Sharing alone makes a deck *reachable*; working on it together needs three
more things, each its own record so the deck row stays a deck:

* ``PresentationVersion`` — an immutable snapshot of the spec taken on EVERY
  save (agent turn, manual edit, restore), so "what did the assistant just
  change?" and "put slide 5 back the way it was" are answerable. Modelled on
  ``FlowVersion``: numbered by ``revision`` (the same counter the deck row
  bumps, so a version and a 409 speak about the same number), pruned past a
  fixed retention, restore is non-destructive (it is a new revision).
* ``PresentationComment`` — a review note anchored to a SLIDE, not an index:
  slides carry server-assigned stable ids precisely so a reorder cannot move a
  comment onto the wrong slide. Anyone who can READ the deck can comment (that
  is what a reviewer does); resolving and deleting follow the author / manager
  rule. The ``get_presentation`` tool surfaces the open ones with their ids,
  and the ``list_`` / ``add_`` / ``resolve_presentation_comment`` tools let
  the user's assistant read, leave and close notes AS THE USER under the same
  rule, so "implement Ada's comment and close it" is a sentence it
  can act on end to end. There is no agent author: a note the assistant posts
  is the user's note.
* ``Presentation.updated_by_user_id`` — who saved last, for the "edited by …"
  line on the card and in the viewer. Presence (who has the deck open now) is
  ephemeral and lives in Redis, not here.

Public link
-------------------
``PresentationPublicLink`` is the one exception to "the link grants nothing":
a capability URL that opens the deck read-only for anyone who has it, without
an account. Its own record, at most one per deck, so the deck row stays a
deck and a duplicate or a package never carries it. The token is looked up by
its SHA-256 (``token_hash``) and kept Fernet-encrypted (``token_enc``)
so a manager can copy the link again; ``services/presentation_public_links``
owns every read and write of it. ``allow_pdf_download`` lets its
visitors also download the PDF of what the link shows.
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.database import Base


class SpecJSON(TypeDecorator):
    """A deck spec column: JSON on the wire, upgraded to the current layouts on read.

    Every reader of ``Presentation.spec`` / ``PresentationVersion.spec`` — the
    REST serializer, the studio prompt, the tool outline, the exporter, a
    package, a version preview, the patch baseline — goes through this hook, so
    none of them can meet a retired v1 slide and none needs to know the
    upgrader exists. Writes are untouched: ``validate_spec`` is the one write
    funnel and upgrades before validating, so what lands in the column is v2.
    Copy-on-write inside the upgrader means a v2 row costs one scan of the
    layouts and no allocation. A logo grid stored before ``show_labels``
    reads with its labels on, as it was drawn, and a title slide
    stored with one ``speaker`` reads as its ``speakers`` list.
    """

    impl = JSON
    cache_ok = True

    def process_result_value(self, value, dialect):
        if not isinstance(value, dict):
            return value
        # Lazy: the model module must stay importable without the services.
        from app.services.presentation_legacy import fold_title_speakers, keep_stored_logo_labels, upgrade_spec

        return fold_title_speakers(keep_stored_logo_labels(upgrade_spec(value)))


#: How many revisions of one deck are kept. Fixed, like ``FLOW_VERSION_RETENTION``:
#: an agent rewriting a deck ten times in a conversation must not evict the
#: version the presenter wants back, and there is no operator reason to tune it.
PRESENTATION_VERSION_RETENTION = 30

#: Where a revision came from — shown as a badge in the history panel.
VERSION_SOURCE_AGENT = "agent"
VERSION_SOURCE_MANUAL = "manual"
VERSION_SOURCE_RESTORE = "restore"
VERSION_SOURCE_DUPLICATE = "duplicate"
#: The deck arrived as a portable package (``presentation_package``) — the
#: first revision of a deck that lived on another install or in a backup.
VERSION_SOURCE_IMPORT = "import"
VERSION_SOURCES = (
    VERSION_SOURCE_AGENT,
    VERSION_SOURCE_MANUAL,
    VERSION_SOURCE_RESTORE,
    VERSION_SOURCE_DUPLICATE,
    VERSION_SOURCE_IMPORT,
)

#: A review comment is a note, not a document.
COMMENT_MAX_CHARS = 4000


class Presentation(Base):
    """One agent-generated deck, editable in place across conversation turns."""

    __tablename__ = "presentations"
    __table_args__ = (Index("ix_presentations_is_company_managed", "is_company_managed"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Owner of the deck. NULL only if the user is later deleted (SET NULL), which
    # matches ``FileAttachment`` / ``ChatImage``: the artifact survives its
    # author, and the API then only serves it to admins.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── Three-tier visibility ─────────────────────────────────────
    # Same column set as every other shareable entity, and REQUIRED in full:
    # ``user_visible_filter`` reads ``shared_user_ids`` and ``access_restricted``
    # unconditionally on its non-admin branch, so a partial set is a 500 for
    # every ordinary user (``tests/test_visibility_helpers.py`` pins it).
    is_company_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)
    shared_group_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    shared_group_permissions: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    shared_user_ids: Mapped[list | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    shared_user_permissions: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)
    access_restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false", default=False)

    # Chat context the deck was generated in (NULL for assignment runs).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Assignment run that produced the deck (NULL in plain chat). Mirrors the
    # ``assignment_run_id`` stamping ``generate_file`` does so a deck generated
    # by a delegated / scheduled run is still attributable to that run.
    assignment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # The deck's TEMPLATE - its design: a LIVE link, not a copy.
    # The palette is resolved from the container on every read
    # (``services.presentations.resolve_template_theme``), so editing a
    # template restyles every deck that wears it, and a deck can be relinked
    # or unlinked at any time (Design panel, ``PATCH template_container_id``,
    # the ``template_id`` of the deck tools). Content, slides, the deck's own
    # uploaded images and the chosen ``spec.theme`` belong to the deck.
    # SET NULL rather than CASCADE: deleting a template must never delete the
    # decks that wear it — they become free decks (the renderer's built-in
    # look in their saved theme) and can be dressed with another template.
    template_container_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("template_containers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Denormalized from ``spec`` for list views and chat cards, so rendering a
    # deck chip never requires parsing the whole spec.
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    theme: Mapped[str] = mapped_column(String(50), nullable=False, default="dark")
    slide_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))

    # Bumped on every in-place edit. Surfaced to the frontend so the deck
    # component can tell "same deck, new slides" from "a different deck".
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=sa.text("1"))

    # The validated ``PresentationSpec`` (JSON-mode dump). Never raw agent
    # input: ``app.services.presentation_spec.validate_spec`` normalizes it
    # first and rejects unknown layouts, oversized decks and tag-like text.
    # Read through ``SpecJSON`` so a v1-era row is served upgraded.
    spec: Mapped[dict] = mapped_column(SpecJSON, nullable=False, default=dict)

    # Who saved the current revision — the agent's caller, the manual editor,
    # whoever restored a version. NULL for decks written before this column
    # existed and for a run with no acting user. SET NULL on user deletion:
    # the deck outlives its last editor.
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class PresentationVersion(Base):
    """An immutable snapshot of a deck's spec at one revision.

    Written by ``save_presentation`` on every save — the deck row's
    ``revision`` and this table's ``revision`` are the SAME counter, so the
    history panel, the 409 conflict message and the agent's tool result all
    name the same number. Restoring a version does not rewind: it saves the
    old spec as a NEW revision (``source = "restore"``), so the history is
    append-only and a restore is itself undoable.
    """

    __tablename__ = "presentation_versions"
    __table_args__ = (
        UniqueConstraint("presentation_id", "revision", name="uq_presentation_versions_revision"),
        Index(
            "ix_presentation_versions_presentation_created",
            "presentation_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    presentation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("presentations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    # Denormalized for the history list: a spec is large and the panel only
    # needs the headline of each entry until one is previewed.
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    slide_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=sa.text("0"))
    # The complete PresentationSpec as saved — what a restore puts back. Read
    # through ``SpecJSON`` like the deck's own: an older snapshot
    # is previewed and restored as v2 too.
    spec: Mapped[dict] = mapped_column(SpecJSON, nullable=False, default=dict)
    # ``agent`` / ``manual`` / ``restore`` / ``duplicate`` / ``import``
    # (``VERSION_SOURCES``).
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # One line saying what changed — the agent's own ``change_summary`` when it
    # gave one, the touched fields for a manual edit, "Restored revision N".
    summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # ``restore`` entries record which revision they brought back.
    restored_from_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    # An EDIT SESSION: the editor autosaves, and a run of manual saves
    # by one person is one entry, moved forward to each new revision. Set on an
    # entry a later save may continue - the latest save it holds - and NULL on
    # every other entry (an agent edit, a restore, a template change, any entry
    # written before sessions existed), which is never continued.
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PresentationComment(Base):
    """A review note anchored to one slide of a deck.

    ``slide_id`` is the slide's stable id (``PresentationSpec.slides[].id``,
    assigned server-side and preserved across edits), NOT its position: a
    reorder or a deleted slide must not silently move a comment onto another
    slide. A comment whose slide no longer exists is kept and shown as
    "detached" rather than dropped — the reviewer's words still matter.

    Flat threads: a reply is a comment with a ``parent_id``. ``resolved_at``
    is the review state; only the root of a thread is resolved, replies follow
    their root.
    """

    __tablename__ = "presentation_comments"
    __table_args__ = (
        Index(
            "ix_presentation_comments_presentation_created",
            "presentation_id",
            "created_at",
        ),
        CheckConstraint("char_length(body) > 0", name="ck_presentation_comments_body_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    presentation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("presentations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The anchored slide's stable id. NULL = a comment on the deck as a whole.
    slide_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("presentation_comments.id", ondelete="CASCADE"),
        nullable=True,
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class PresentationPublicLink(Base):
    """The public link of one deck: anyone holding the URL may view it.

    Regenerating rewrites the token in place (the old URL stops answering on
    the next request); turning the link off deletes the row. ``created_at`` is
    when the CURRENT token was issued and ``created_by_user_id`` who issued
    it, so a regenerated link reads as new.
    """

    __tablename__ = "presentation_public_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    presentation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("presentations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # ``security.hash_token`` of the URL token: what a public request is
    # looked up by. The raw token never touches the database.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # ``security.encrypt_value`` of the same token (invariant 2), read back
    # only for a manager who copies the link again.
    token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # Whether a visitor may also download the deck as a PDF. Off on
    # every new link; regenerating the token keeps it, turning the link off
    # (deleting the row) forgets it.
    allow_pdf_download: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class PresentationPoll(Base):
    """The live state of one ``poll`` block of a deck: its short code and what the presenter opened.

    Created the first time an editor's viewer asks for the deck's polls
    (``services/presentation_polls``), keyed by the block's own ``id``, so
    moving the block keeps its code and votes. Deleting the deck deletes it
    and its votes (``ON DELETE CASCADE``). The short ``code`` is the only
    credential of the audience page ``/v/CODE``: it reads this poll and
    nothing else of the deck.
    """

    __tablename__ = "presentation_polls"
    __table_args__ = (UniqueConstraint("presentation_id", "poll_key", name="uq_presentation_polls_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    presentation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("presentations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    poll_key: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(12), nullable=False, unique=True)
    active_question: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.text("false"))
    revealed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=sa.text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class PresentationPollVote(Base):
    """One audience answer to one question of a poll: anonymous, one per device and question.

    ``voter_hash`` is the SHA-256 of a random token the audience page keeps
    in the browser: no account, no fingerprint, nothing that names a person.
    """

    __tablename__ = "presentation_poll_votes"
    __table_args__ = (
        UniqueConstraint("poll_id", "question_key", "voter_hash", name="uq_presentation_poll_votes_voter"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    poll_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("presentation_polls.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question_key: Mapped[str] = mapped_column(String(32), nullable=False)
    voter_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
