"""Schema contract guards for ``knowledge_documents.filename``.

KB sync writes ``filename`` from sources whose values have no predictable
upper bound (Notion page title, Microsoft Graph document name, GitHub blob
path, website crawl URL). A long Notion page title overflowed the previous
``VARCHAR(500)`` and 500'd KB sync with ``StringDataRightTruncationError`` —
the same bug class that hit ``source_url``.

The fix makes the column an unbounded ``TEXT`` (every fixed cap just defers
the same bug to the next long-name source). These tests pin the model end of
the fix so a regression — reverting the model to a bounded ``String(n)`` —
trips loudly without needing a live database.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.knowledge_base import KnowledgeDocument

# ── Model contract ──────────────────────────────────────────────────────


def test_filename_model_column_is_unbounded_text() -> None:
    """The ORM column must be unbounded ``TEXT`` — not a capped VARCHAR.

    A regression to ``String(500)`` (or any ``String(n)``) sets a finite
    ``length`` and re-opens the truncation bug class; this asserts both the
    ``Text`` type and the absence of a length cap.
    """
    col_type = KnowledgeDocument.__table__.c.filename.type
    assert isinstance(col_type, sa.Text), (
        f"filename must be Text/unbounded, got {col_type!r}"
    )
    # ``Text`` carries ``length is None``; a bounded ``String(n)`` would not.
    assert getattr(col_type, "length", None) is None, (
        "filename must not declare a length cap"
    )


def test_filename_column_is_not_nullable() -> None:
    """``filename`` is required for every document — the widening must not
    accidentally flip nullability."""
    assert KnowledgeDocument.__table__.c.filename.nullable is False
