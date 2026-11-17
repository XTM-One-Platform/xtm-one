"""A OneDrive / SharePoint knowledge-base sync keeps a legacy Word ``.doc``
(an OLE2 container) searchable by default.

The sync lists ``.doc`` files and indexes their extracted text. Extracting
one to nothing - ``STRICT_BINARY_TEXT_EXTRACTION=true``, once the only
behaviour - makes the sync skip it, and the stale sweep then deletes
the document it had indexed.
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import Delete

from app.services import kb_sync

_DOC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00Board minutes: renew the SOC contract\x00" * 40
_BASELINE_TEXT = _DOC.decode("utf-8", errors="replace").replace("\x00", "")
_DOWNLOAD_URL = "https://download.example/minutes.doc"
_ITEM = {
    "id": "item-1",
    "name": "minutes.doc",
    "size": len(_DOC),
    "@microsoft.graph.downloadUrl": _DOWNLOAD_URL,
    "parentReference": {"path": "/drive/root:/Board"},
    "webUrl": "https://contoso.sharepoint.com/Board/minutes.doc",
}


class _Response:
    def __init__(self, *, listing: dict | None = None, content: bytes = b""):
        self._listing = listing
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._listing


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, url, **_kwargs):
        if url == _DOWNLOAD_URL:
            return _Response(content=_DOC)
        return _Response(listing={"value": [_ITEM]})


def _session(indexed: SimpleNamespace | None = None) -> MagicMock:
    """A session holding *indexed* as the drive source's one document: the
    upsert's lookup finds it and the stale sweep lists it. An added document
    gets the id its flush would give it."""
    result = MagicMock()
    result.scalars.return_value.first.return_value = indexed
    result.scalars.return_value.all.return_value = [indexed.id] if indexed else []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock(side_effect=lambda doc: setattr(doc, "id", uuid.uuid4()))
    return db


def _indexed_on_the_baseline() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_url=_ITEM["webUrl"],
        content_hash=hashlib.sha256(_BASELINE_TEXT.encode()).hexdigest(),
    )


def _deleted_document_ids(db: MagicMock) -> list[uuid.UUID]:
    deleted: list[uuid.UUID] = []
    for call in db.execute.await_args_list:
        statement = call.args[0]
        if isinstance(statement, Delete) and statement.table.name == kb_sync.KnowledgeDocument.__tablename__:
            for ids in statement.compile().params.values():
                deleted.extend(ids)
    return deleted


@pytest.fixture
def drive(monkeypatch):
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kw: _Client())
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "_delete_embeddings_for_source_ids", AsyncMock())
    cancel = MagicMock()
    monkeypatch.setattr(kb_sync, "cancel_document_jobs_after_commit", cancel)
    enqueue = AsyncMock()
    monkeypatch.setattr(kb_sync, "enqueue_documents", enqueue)
    return SimpleNamespace(cancel=cancel, enqueue=enqueue)


async def _sync(db: MagicMock) -> list[str]:
    kb = SimpleNamespace(id=uuid.uuid4(), name="Board")
    return await kb_sync.sync_microsoft_drive(db, kb, user_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_a_legacy_doc_is_ingested_with_the_text_the_baseline_indexed(drive):
    db = _session()

    ids = await _sync(db)

    doc = db.add.call_args.args[0]
    assert doc.filename == "Board/minutes.doc"
    assert doc.content == _BASELINE_TEXT
    assert ids == [str(doc.id)]
    drive.enqueue.assert_awaited_once_with([str(doc.id)])
    assert _deleted_document_ids(db) == []


@pytest.mark.asyncio
async def test_a_legacy_doc_indexed_on_the_baseline_is_kept_and_not_re_embedded(drive):
    """A deployment upgrading from the baseline syncs the same text again: the
    content hash matches, so the document is kept as it is."""
    indexed = _indexed_on_the_baseline()
    db = _session(indexed)

    ids = await _sync(db)

    assert ids == [str(indexed.id)]
    db.add.assert_not_called()
    drive.enqueue.assert_awaited_once_with([])
    assert _deleted_document_ids(db) == []


@pytest.mark.asyncio
async def test_strict_binary_text_extraction_deletes_the_indexed_legacy_doc(drive, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "strict_binary_text_extraction", True)
    indexed = _indexed_on_the_baseline()
    db = _session(indexed)

    assert await _sync(db) == []
    db.add.assert_not_called()
    drive.cancel.assert_called_once_with(db, [str(indexed.id)])
    assert _deleted_document_ids(db) == [indexed.id]
