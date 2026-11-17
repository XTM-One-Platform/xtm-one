"""The KB activity search matches what is served, on a real PostgreSQL.

An ``embedding_failed`` entry written by an older build may still hold raw exception
text, which the activity list never serves (``public_activity_details``). The
search and its count run in SQL, so they must match that same served text: a
match on the stored column would answer whether a hidden term is in it. The
SQL string functions the filter relies on can only be checked against a real
database, so this runs in an isolated temporary one.
"""

from __future__ import annotations

import uuid

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import knowledge_bases as kb_mod
from app.models.knowledge_base import KBActivityLog, KnowledgeBase
from app.models.user import User
from app.services.kb_document_errors import (
    EMBEDDING_FAILED_EVENT,
    TIMEOUT_MESSAGE,
    dimension_mismatch_message,
    embedding_failed_details,
    public_activity_details,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

# What an older row holds: the raw driver text.
_LEGACY_RAW = (
    "(sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) <class "
    "'asyncpg.exceptions.UndefinedColumnError'>: column "
    "platform_settings.guard_exempt_tools does not exist "
    "[SQL: SELECT platform_settings.id FROM platform_settings]"
)

_ENTRIES = [
    (EMBEDDING_FAILED_EVENT, f"Failed to embed 'report.pdf': {_LEGACY_RAW}"),
    (EMBEDDING_FAILED_EVENT, embedding_failed_details("notes.md", TIMEOUT_MESSAGE)),
    # A filename holding the separator: served, and searched, as written.
    (EMBEDDING_FAILED_EVENT, embedding_failed_details("plan': draft.md", TIMEOUT_MESSAGE)),
    (EMBEDDING_FAILED_EVENT, f"worker crashed: {_LEGACY_RAW}"),
    (EMBEDDING_FAILED_EVENT, f"Failed to embed 'cut {_LEGACY_RAW}"),
    (
        EMBEDDING_FAILED_EVENT,
        embedding_failed_details("dims.txt", dimension_mismatch_message("m", "768", 1536)),
    ),
    (
        EMBEDDING_FAILED_EVENT,
        "Failed to embed 'old.txt': Embedding model 'm' produced 768-dimension "
        f"vector(s) but the platform is configured for 1536 dimensions. {_LEGACY_RAW}",
    ),
    # Older wording, value written with a leading zero: served rebuilt as 600s.
    (
        EMBEDDING_FAILED_EVENT,
        "Failed to embed 'w.txt': Worker did not finish embedding within 0600s \u2014 "
        f"re-enqueued. {_LEGACY_RAW}",
    ),
    # A valued opening the reader does not parse: served as the generic sentence.
    (EMBEDDING_FAILED_EVENT, f"Failed to embed 'bad.txt': Embedding model 'm' produced lots {_LEGACY_RAW}"),
    ("document_added", "Uploaded file 'platform_settings.csv'"),
    (EMBEDDING_FAILED_EVENT, None),
]

# Terms whose presence in the SERVED text the search must reproduce exactly.
_TERMS = [
    "guard_exempt_tools",
    "platform_settings",
    "does not exist",
    "asyncpg",
    "produced lots",
    "report.pdf",
    "timed out",
    "internal error",
    "failed to embed",
    "failed to embed a document",
    "dims.txt",
    "vector dimensions",
    "outputs 1536 dimensions",
    "embedding model 'm' produced 768",
    "within 600s - re-enqueued",
    "within 0600s",
    "bad.txt",
    "draft.md",
    "embedding_failed",
    "document_added",
]


def _expected(rows: list[KBActivityLog], term: str) -> set[str]:
    needle = term.lower()
    return {
        str(row.id)
        for row in rows
        if needle in row.event.lower()
        or needle in (public_activity_details(row.event, row.details) or "").lower()
    }


async def test_the_activity_search_and_its_count_match_only_the_served_text():
    async with (
        scratch_database(
            "kbactsearch", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
        ) as db,
        db.sessions() as session,
    ):
        # A plain owner, not an admin: the admin predicate short-circuits the
        # visibility filter the endpoints run first.
        owner = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Owner",
        )
        session.add(owner)
        await session.flush()
        kb = KnowledgeBase(name="Docs", user_id=owner.id)
        session.add(kb)
        await session.flush()
        rows = [
            KBActivityLog(knowledge_base_id=kb.id, event=event, details=details)
            for event, details in _ENTRIES
        ]
        session.add_all(rows)
        await session.commit()
        assert not owner.is_admin

        kb_id = str(kb.id)
        for term in _TERMS:
            listed = await kb_mod.list_activity_logs(
                kb_id, limit=200, offset=0, search=term, user=owner, db=session
            )
            counted = await kb_mod.count_activity_logs(kb_id, search=term, user=owner, db=session)
            expected = _expected(rows, term)
            assert {entry.id for entry in listed} == expected, term
            assert counted["count"] == len(expected), term

        # The hidden driver text is never found, whatever the term.
        for hidden in ("guard_exempt_tools", "does not exist", "asyncpg", "SELECT", "produced lots"):
            assert _expected(rows, hidden) == set(), hidden
            counted = await kb_mod.count_activity_logs(kb_id, search=hidden, user=owner, db=session)
            assert counted["count"] == 0, hidden
