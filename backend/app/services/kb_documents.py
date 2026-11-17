# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Adding a document to a knowledge base - the one implementation.

The knowledge-base routes, the ``store_knowledge`` tool and a workspace's
Sources all add documents. Each used to build its ``KnowledgeDocument`` inline;
a workspace source must obey exactly the formats, limits and URL rules of a
knowledge-base upload (spec 032 FR-006), so the steps live here and every
caller goes through them.

None of these functions authorizes anything: the caller has already decided
the knowledge base may be written (``helpers.knowledge_base_write_refusal`` for
the knowledge-base routes, the workspace's config tier for a workspace). None
commits or enqueues: the caller commits, THEN calls
``kb_helpers.enqueue_document_processing``, so a job never runs before its row
is visible.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

logger = logging.getLogger(__name__)

#: A crawled page with less text than this is a shell (a login wall, a
#: script-only page), not a document worth embedding.
MIN_CRAWLED_TEXT_CHARS = 20

BINARY_TEXT_REFUSED = (
    "Document content contains binary data and cannot be stored as "
    "text. Upload binary files (PDF, DOCX, PPTX, XLSX) via the file "
    "upload endpoint, which extracts their text automatically."
)
URL_FETCH_REFUSED = "Could not fetch that URL."
URL_NO_TEXT_REFUSED = "Could not extract meaningful text from URL"


class DocumentRefusedError(Exception):
    """A document the knowledge base cannot take, with the 400 detail to answer.

    The detail is safe to show: a fetch failure's precise reason goes to the
    log only, so the answer is no oracle on what the server can reach.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def insert_document(
    db: AsyncSession,
    kb: KnowledgeBase,
    *,
    filename: str,
    source_type: str,
    content: str,
    source_url: str | None = None,
    extra_metadata: dict | None = None,
    activity_details: str | None = None,
    doc_id: uuid.UUID | None = None,
    figure_reading: dict | None = None,
) -> KnowledgeDocument:
    """Add a pending document to *kb* and count it. Flushes; does not commit.

    *activity_details* records a ``document_added`` activity entry when given.
    *doc_id* is the id a caller minted before writing anything (the figure
    pass keeps its PDF under it), *figure_reading* what that pass starts from.
    """
    from app.services.kb_helpers import bump_kb_revision, log_kb_activity

    doc = KnowledgeDocument(
        id=doc_id or uuid.uuid4(),
        figure_reading=figure_reading,
        knowledge_base_id=kb.id,
        filename=filename,
        source_type=source_type,
        source_url=source_url,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        status="pending",
        extra_metadata=extra_metadata,
    )
    db.add(doc)
    await db.flush()
    kb.document_count = (kb.document_count or 0) + 1
    await bump_kb_revision(db, kb.id)
    if activity_details is not None:
        await log_kb_activity(
            db,
            kb.id,
            "document_added",
            details=activity_details,
            source_type=source_type,
            document_count=1,
        )
    return doc


async def add_text_document(
    db: AsyncSession,
    kb: KnowledgeBase,
    *,
    filename: str,
    content: str,
    source_type: str,
) -> KnowledgeDocument:
    """A pasted text or markdown document.

    PostgreSQL text columns cannot store NUL bytes: a binary file read as text
    on the client would otherwise raise a 500 on flush, so it is refused with a
    pointer to the upload, which extracts binary formats server-side.
    """
    if "\x00" in content:
        raise DocumentRefusedError(BINARY_TEXT_REFUSED)
    return await insert_document(
        db,
        kb,
        filename=filename,
        source_type=source_type,
        content=content,
        activity_details=f"Added text document '{filename}'",
    )


async def add_file_document(
    db: AsyncSession,
    kb: KnowledgeBase,
    *,
    filename: str,
    data: bytes,
    activity_details: str | None = None,
) -> KnowledgeDocument:
    """An uploaded file, its text extracted off the event loop.

    Parsing a large PDF or XLSX inline froze the event loop and failed the
    liveness probe, hence ``extract_text_from_file_async``. A PDF added to a
    knowledge base that describes figures is also kept for the figure pass of
    its document job (``kb_figure_pass.stash_source``).
    """
    from app.services.chunking import extract_text_from_file_async
    from app.services.kb_figure_pass import stash_source
    from app.services.kb_helpers import source_type_for_filename

    content = await extract_text_from_file_async(data, filename)
    # Kept before the row is written: no lock on the knowledge base row is
    # held across the upload.
    doc_id = uuid.uuid4()
    reading = await stash_source(kb, doc_id, content, data, filename)
    return await insert_document(
        db,
        kb,
        filename=filename,
        source_type=source_type_for_filename(filename),
        content=content,
        activity_details=(
            activity_details if activity_details is not None else f"Uploaded file '{filename}'"
        ),
        doc_id=doc_id,
        figure_reading=reading,
    )


async def add_url_document(
    db: AsyncSession, kb: KnowledgeBase, *, url: str
) -> KnowledgeDocument:
    """A web page, fetched through ``web_fetch`` (its SSRF guard included)."""
    from app.services.web_fetch import fetch_and_extract

    try:
        result = await fetch_and_extract(url)
    except Exception as exc:
        logger.info("Knowledge base URL fetch refused for %r: %r", url[:200], exc)
        raise DocumentRefusedError(URL_FETCH_REFUSED) from None
    text = result.text
    if not text or len(text.strip()) < MIN_CRAWLED_TEXT_CHARS:
        raise DocumentRefusedError(URL_NO_TEXT_REFUSED)
    title = result.title or url
    return await insert_document(
        db,
        kb,
        filename=f"{title[:100]}.md" if title else "crawled_page.md",
        source_type="url",
        source_url=url,
        content=text,
        activity_details=f"Crawled '{url[:200]}'",
    )
