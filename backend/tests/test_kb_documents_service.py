"""Adding a document to a knowledge base has one implementation.

The knowledge-base routes, ``store_knowledge`` and a workspace's Sources go
through ``services.kb_documents`` (spec 032 FR-006: a workspace source obeys
the knowledge-base rules). These pin what each entry point stores, refuses and
records - and that nothing commits or enqueues, which stays the caller's job
so no job runs before its row is visible.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import kb_documents


class _Db:
    def __init__(self) -> None:
        self.added: list = []
        self.flushes = 0
        self.commit = AsyncMock()

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushes += 1


@pytest.fixture
def activity(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", spy)
    return spy


@pytest.fixture(autouse=True)
def revision(monkeypatch):
    """Adding a document moves its knowledge base's content revision."""
    spy = AsyncMock()
    monkeypatch.setattr("app.services.kb_helpers.bump_kb_revision", spy)
    return spy


def _kb(count: int = 2) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), document_count=count)


async def test_a_text_document_is_stored_pending_counted_and_logged(activity, revision):
    db, kb = _Db(), _kb()

    doc = await kb_documents.add_text_document(
        db, kb, filename="notes.md", content="# Notes", source_type="markdown"
    )

    assert db.added == [doc]
    assert (doc.knowledge_base_id, doc.filename, doc.source_type, doc.content, doc.status) == (
        kb.id,
        "notes.md",
        "markdown",
        "# Notes",
        "pending",
    )
    assert doc.content_hash == hashlib.sha256(b"# Notes").hexdigest()
    assert kb.document_count == 3
    activity.assert_awaited_once_with(
        db,
        kb.id,
        "document_added",
        details="Added text document 'notes.md'",
        source_type="markdown",
        document_count=1,
    )
    revision.assert_awaited_once_with(db, kb.id)
    db.commit.assert_not_awaited()


async def test_text_with_a_nul_byte_is_refused_before_anything_is_stored(activity, revision):
    db, kb = _Db(), _kb()

    with pytest.raises(kb_documents.DocumentRefusedError) as exc:
        await kb_documents.add_text_document(
            db, kb, filename="x.txt", content="a\x00b", source_type="text"
        )

    assert exc.value.detail == kb_documents.BINARY_TEXT_REFUSED
    assert db.added == []
    assert kb.document_count == 2
    activity.assert_not_awaited()
    revision.assert_not_awaited()


async def test_an_uploaded_file_is_extracted_off_loop_and_typed_by_its_name(monkeypatch, activity):
    extract = AsyncMock(return_value="extracted text")
    monkeypatch.setattr("app.services.chunking.extract_text_from_file_async", extract)
    db, kb = _Db(), _kb()

    doc = await kb_documents.add_file_document(db, kb, filename="report.pdf", data=b"%PDF")

    extract.assert_awaited_once_with(b"%PDF", "report.pdf")
    assert (doc.content, doc.source_type) == ("extracted text", "pdf")
    assert activity.await_args.kwargs["details"] == "Uploaded file 'report.pdf'"


async def test_a_crawled_page_records_its_url_and_title(monkeypatch, activity):
    fetch = AsyncMock(return_value=SimpleNamespace(text="A page with plenty of text.", title="Page"))
    monkeypatch.setattr("app.services.web_fetch.fetch_and_extract", fetch)
    db, kb = _Db(), _kb()

    doc = await kb_documents.add_url_document(db, kb, url="https://example.com/a")

    fetch.assert_awaited_once_with("https://example.com/a")
    assert (doc.filename, doc.source_type, doc.source_url) == ("Page.md", "url", "https://example.com/a")


async def test_a_refused_fetch_answers_a_generic_message_and_stores_nothing(monkeypatch, activity):
    """The precise reason (a blocked private address, a DNS failure) goes to
    the log: echoing it would make the endpoint an oracle on what the server
    can reach."""
    monkeypatch.setattr(
        "app.services.web_fetch.fetch_and_extract",
        AsyncMock(side_effect=RuntimeError("blocked: 10.0.0.5 is a private address")),
    )
    db, kb = _Db(), _kb()

    with pytest.raises(kb_documents.DocumentRefusedError) as exc:
        await kb_documents.add_url_document(db, kb, url="http://10.0.0.5/")

    assert exc.value.detail == "Could not fetch that URL."
    assert db.added == []


@pytest.mark.parametrize("text", ["", "   ", "too short"])
async def test_a_page_without_meaningful_text_is_refused(monkeypatch, activity, text):
    monkeypatch.setattr(
        "app.services.web_fetch.fetch_and_extract",
        AsyncMock(return_value=SimpleNamespace(text=text, title="T")),
    )
    db, kb = _Db(), _kb()

    with pytest.raises(kb_documents.DocumentRefusedError) as exc:
        await kb_documents.add_url_document(db, kb, url="https://example.com")

    assert exc.value.detail == kb_documents.URL_NO_TEXT_REFUSED
    assert db.added == []


async def test_insert_without_activity_details_logs_nothing(activity):
    db, kb = _Db(), _kb()

    doc = await kb_documents.insert_document(
        db, kb, filename="t", source_type="agent", content="c", extra_metadata={"k": "v"}
    )

    assert doc.extra_metadata == {"k": "v"}
    activity.assert_not_awaited()
