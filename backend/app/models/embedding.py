# SPDX-License-Identifier: Apache-2.0

"""Embedding model for pgvector – stores document chunks with vector embeddings."""

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Default vector dimension used as a fallback when no explicit
# embedding_dimensions is stored in PlatformSettings.
# Matches OpenAI text-embedding-3-large; override via Settings UI.
EMBEDDING_DIMENSIONS = 3072

# Supported vector dimensions for embedding providers that allow configurable
# output size (Matryoshka/shortening). Keep this in sync with frontend options.
ALLOWED_EMBEDDING_DIMENSIONS: tuple[int, int, int, int] = (768, 1024, 1536, 3072)


class Embedding(Base):
    __tablename__ = "embeddings"
    __table_args__ = (
        Index(
            "ix_embeddings_kb_dim",
            "knowledge_base_id",
            "embedding_dimensions",
        ),
        Index(
            "ix_embeddings_kb_model_dim",
            "knowledge_base_id",
            "embedding_model",
            "embedding_dimensions",
        ),
        # Document dedup / deletion by (kb, source).
        Index("ix_embeddings_kb_source", "knowledge_base_id", "source_id"),
        # Full-text GIN index backing hybrid (vector + keyword) RAG search
        # (``services/hybrid_search.py`` — ``to_tsvector('english', content)``).
        Index(
            "ix_embeddings_content_fts",
            text("to_tsvector('english', content)"),
            postgresql_using="gin",
        ),
        # ANN (HNSW) indexes for cosine-distance semantic search
        # (assessment finding ARCH-2).  The ``embedding`` column is untyped
        # (dimension-flexible), and pgvector requires a FIXED dimension for an
        # HNSW index — so we build one partial expression index per supported
        # dimension (``ALLOWED_EMBEDDING_DIMENSIONS``), each scoped by a
        # ``WHERE embedding_dimensions = N`` predicate.  The search queries in
        # ``services/vector.py`` / ``services/hybrid_search.py`` cast the
        # column identically (``ann_cosine_distance``) so the planner matches
        # the index expression.  3072 dims exceed pgvector's 2000-dim limit
        # for ``vector`` opclasses, so that index goes through ``halfvec``
        # (half precision, supported up to 4000 dims; pgvector >= 0.7 — always
        # true on the supported PostgreSQL 17 + pgvector stack).
        Index(
            "ix_embeddings_embedding_hnsw_768",
            text("(embedding::vector(768)) vector_cosine_ops"),
            postgresql_using="hnsw",
            postgresql_where=text("embedding_dimensions = 768"),
        ),
        Index(
            "ix_embeddings_embedding_hnsw_1024",
            text("(embedding::vector(1024)) vector_cosine_ops"),
            postgresql_using="hnsw",
            postgresql_where=text("embedding_dimensions = 1024"),
        ),
        Index(
            "ix_embeddings_embedding_hnsw_1536",
            text("(embedding::vector(1536)) vector_cosine_ops"),
            postgresql_using="hnsw",
            postgresql_where=text("embedding_dimensions = 1536"),
        ),
        Index(
            "ix_embeddings_embedding_hnsw_3072",
            text("(embedding::halfvec(3072)) halfvec_cosine_ops"),
            postgresql_using="hnsw",
            postgresql_where=text("embedding_dimensions = 3072"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Source classification – references a knowledge base or agent
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # email | pr_comment | message | document
    source_id: Mapped[str | None] = mapped_column(
        String(500), nullable=True, index=True
    )
    # Provider identity – string name kept for display/compat, UUID is the true identity key
    embedding_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # No FK: an ON DELETE SET NULL cascade ran inside the provider
    # deletion, under its lock on every provider config row, over every chunk
    # the config embedded. The id of a deleted config is nulled afterwards, in
    # bounded batches, by ``clear_deleted_embedding_provider_references``.
    embedding_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
