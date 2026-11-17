# SPDX-License-Identifier: Apache-2.0

"""Persistent agent memory model – durable memory stored in PostgreSQL.

Extends the Redis-based agent memory with a durable persistence layer.
Consolidated memories and key facts are stored with vector embeddings
so agents can semantically search their own accumulated knowledge.

This complements (not replaces) the Redis layer:
  - Redis: fast short-term memory, recent activity (24h TTL)
  - DB: durable long-term memory, semantic search, survives restarts
"""

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AgentMemoryEntry(Base):
    """A durable memory entry for an agent, stored with vector embedding."""

    __tablename__ = "agent_memory_entries"
    # ``agent_id``, ``user_id`` and ``memory_type`` are indexed inline. The
    # full-text GIN index backs hybrid keyword search over memory content; the
    # partial subject index backs supersession and the newest-state lookup;
    # the (agent_id, created_at) index backs the per-turn recency read of
    # ``load_persistent_entries`` (#8836).
    __table_args__ = (
        Index(
            "ix_agent_memory_entries_content_fts",
            text("to_tsvector('english', content)"),
            postgresql_using="gin",
        ),
        Index(
            "ix_agent_memory_entries_agent_subject",
            "agent_id",
            "subject_key",
            postgresql_where=text("subject_key IS NOT NULL"),
        ),
        Index("ix_agent_memory_entries_agent_created", "agent_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Optional user scope (NULL = agent-level / assignment memories)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Memory content
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Type of memory: "consolidated" (medium-term summary), "fact" (extracted key fact),
    # "style" (communication style note), "preference" (user preference),
    # "state" (what an evolving object looked like when the entry was stored)
    memory_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="consolidated",
        index=True,
    )

    # Source that produced this memory ("chat", "assignment", "compaction", "learning")
    source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="chat",
    )

    # The object the entry describes ("presentation:<id>", "doc:X"); NULL for
    # an entry about nothing in particular. A newer "state" entry on the same
    # subject supersedes the older one (``persistent_memory.persist_memory``).
    subject_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Set once the entry no longer holds: superseded by a newer entry
    # (``superseded_by_id``) or retired with no successor. A superseded entry
    # is kept and searchable, never injected into a prompt. The successor is
    # referenced by id without a key: a self-referencing key would make every
    # bulk delete of this table look its referrers up.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Vector embedding for semantic search. Dimension is configurable via
    # platform settings, so this column remains untyped.
    embedding: Mapped[list | None] = mapped_column(Vector(), nullable=True)

    # Embedding provenance — which model produced the vector and how many
    # dimensions it has.  Mirrors the KB ``embeddings`` table so stale
    # entries can be detected and re-embedded when the platform embedding
    # model changes.  NULL on both = unknown/legacy (treated as stale by
    # the memory maintenance re-embed task and excluded from cosine
    # searches whose query vector has a known dimension).
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
