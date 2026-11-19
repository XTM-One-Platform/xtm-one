# SPDX-License-Identifier: Apache-2.0

"""FileAttachment model – tracks files stored in S3/MinIO.

Every uploaded file (chat attachments, assignment outputs, etc.) gets a
record here. The actual binary is stored in S3; this table holds metadata
and the S3 object key for retrieval.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FileAttachment(Base):
    """A file stored in S3-compatible object storage."""

    __tablename__ = "file_attachments"
    __table_args__ = (
        # Conversation-scoped content lookup for tool-produced files: a tool
        # persisting bytes whose digest already exists in the SAME
        # conversation reuses that row instead of creating another (see
        # ``builtin_tools._shared._persist_generated_file``). Partial on
        # ``sha256`` — user uploads and pre-existing rows carry NULL and never
        # take part.
        Index(
            "ix_file_attachments_conversation_sha256",
            "conversation_id",
            "sha256",
            postgresql_where=text("sha256 IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Who uploaded the file (NULL for system/assignment uploads)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Optional association to a conversation
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Optional association to a message
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Optional association to an assignment run. Indexed because runs are
    # deleted by the thousand (agent, assignment, purge) and each deleted run
    # makes PostgreSQL null this column with a query on it.
    assignment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assignment_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Optional association to a template container. ``CASCADE`` (not
    # ``SET NULL``) because a template asset has no meaning outside its
    # container: deleting the container must take its assets' rows with it.
    # The S3 objects themselves are removed by ``delete_file_attachments()``
    # in the delete endpoint — the FK only guards against orphaned rows if a
    # container is ever removed outside that path.
    template_container_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("template_containers.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Structured role of a template asset ("font", "image", "logo",
    # "background", "icon", …). NULL for every non-template attachment. Lets a
    # consumer feature pick "the heading font" out of a container without
    # sniffing content types or filenames.
    asset_role: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # What the file DEPICTS, for a reader who cannot see it. On a template
    # asset it is the caption an author wrote - "OpenAEV architecture:
    # agents, injectors and collectors around the platform" says what
    # "openaev-architecture.svg" cannot - surfaced by ``get_template`` next to
    # the file id the ``image`` block takes. On a conversation / run file
    # it is the agent's OWN 1-3 line account of an image it viewed,
    # written with ``annotate_file`` and read back through ``FileRef.notes``:
    # the ``Available Files`` block, a dropped-files note and a
    # ``read_file_content`` answered without pixels all quote it, so an image
    # is looked at once and remembered. NULL until someone wrote one.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # File metadata
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(200), nullable=False, default="application/octet-stream"
    )
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # S3 storage key (bucket-relative object name)
    s3_object_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # Hex SHA-256 of the stored bytes. Set by the tool-file persistence path
    # (``_persist_generated_file``) so a tool that downloads or generates the
    # same bytes again in one conversation reuses the existing row (trace
    # 722375f1: 84 byte-identical PNGs from one turn), and by every template
    # asset write, which the presentation package import trusts to tell a
    # template's same-name assets apart without reading them. NULL
    # for user uploads and for rows written before their path recorded it -
    # those never match, by design. It must stay the digest of the CURRENT
    # object: every write of ``s3_object_name`` sets it too (the bytes just
    # stored, or None where the writer does not hold them), which
    # ``tests/test_file_attachment_digest.py`` enforces.
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The geometry of an IMAGE a deck needs before drawing it: the
    # size it was read at, the box of its non-transparent pixels as fractions
    # (``trim``) and the mean opacity inside it (``ink``), read once when the
    # bytes are stored (``services/image_geometry.py``) so a logo wall sizes
    # the mark, not the file's transparent padding. NULL until read; a file
    # read as unreadable holds ``{"v": 1}`` alone and is drawn as the file.
    image_meta: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, default=None)

    # Source context: "chat", "assignment", "knowledge_base", "agent",
    # "dataset", "template_asset" (see
    # ``app.models.template_container.TEMPLATE_ASSET_SOURCE``)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="chat")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )

    # Last time a person or an agent used the file (download, preview,
    # ``read_file_content``, an attach). NULL reads as ``created_at``: the file
    # retention (``services/file_retention.py``) counts its days from here.
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Tombstone: the bytes are gone, the row stays so every place that shows
    # the file draws a "deleted" card instead of breaking. ``deleted_reason``
    # is one of ``DELETED_REASONS`` (``services/file_retention.py``).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
