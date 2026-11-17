"""Schema contract guards for ``knowledge_documents.source_url``.

KB sync writes ``source_url`` from sources whose URLs have no predictable
upper bound (Notion page URL, Microsoft Graph ``web_url``, GitHub blob
URL, Drata policy URL, website crawl URL). A long Notion page title
overflowed the previous ``VARCHAR(500)`` and 500'd KB sync with
``StringDataRightTruncationError``.

The fix makes the column an unbounded ``TEXT`` (every fixed cap just
defers the same bug to the next long-URL source). These tests pin the
model end of the fix so a regression — reverting the model to a bounded
``String(n)`` — trips loudly without needing a live database.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.knowledge_base import KnowledgeDocument

# ── Model contract ──────────────────────────────────────────────────────


def test_source_url_model_column_is_unbounded_text() -> None:
    """The ORM column must be unbounded ``TEXT`` — not a capped VARCHAR.

    A regression to ``String(2000)`` (or any ``String(n)``) sets a finite
    ``length`` and re-opens the truncation bug class; this asserts both the
    ``Text`` type and the absence of a length cap.
    """
    col_type = KnowledgeDocument.__table__.c.source_url.type
    assert isinstance(col_type, sa.Text), (
        f"source_url must be Text/unbounded, got {col_type!r}"
    )
    # ``Text`` carries ``length is None``; a bounded ``String(n)`` would not.
    assert getattr(col_type, "length", None) is None, (
        "source_url must not declare a length cap"
    )


def test_source_url_column_still_nullable() -> None:
    """``source_url`` is optional (text/upload docs have none) — the widening
    must not accidentally flip nullability."""
    assert KnowledgeDocument.__table__.c.source_url.nullable is True
