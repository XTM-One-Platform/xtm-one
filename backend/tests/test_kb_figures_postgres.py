"""A PDF's figures reach the index of a knowledge base that describes them.

The ingestion path on PostgreSQL: the shared upload (``add_file_document``)
keeps the PDF, the document job (``process_document``) runs the figure pass
before chunking and embeds the text with the descriptions, and the hourly
sweep deletes the kept PDFs no pass will read. The vision model, the embedding
provider and object storage are fakes; the background gate and the model
resolution are the platform's own functions with their answers stubbed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.embedding import Embedding
from app.models.knowledge_base import KBActivityLog, KnowledgeBase, KnowledgeDocument
from app.models.user import User
from app.services import document_processor, kb_documents, kb_figure_pass, kb_figures
from tests.kb_figure_pdfs import BODY, build_pdf
from tests.scratch_database import ALL_TABLES, ScratchDatabase, scratch_database

pytestmark = pytest.mark.integration

GRAPH = "- Narrative -> Channel: amplified by\n- Channel -> Audience: reaches"
FIGURE_PDF = build_pdf(
    [
        [("text", BODY), ("text", BODY)],
        [("text", BODY), ("text", BODY), ("image", 0.25)],
        [("text", BODY), ("text", BODY)],
    ]
)
SCANNED_PDF = build_pdf([[("image", 0.9)]])


class _Embedder:
    provider_name = "fake"

    def __init__(self):
        self.inputs: list[str] = []

    async def embed(self, text, model=None):
        self.inputs.append(text)
        return [0.1 * (1 + len(text) % 7)] * 8


class _Vision:
    def __init__(self, answer: str = GRAPH, *, fail: bool = False):
        self.answer = answer
        self.fail = fail
        self.calls = 0

    async def chat(self, messages, model=None, **_kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model refused the image")
        return SimpleNamespace(content=self.answer, stop_reason="end_turn")


@pytest.fixture
def platform(monkeypatch):
    """Object storage over a dict, the embedding provider and the vision model."""
    import app.llm.router as router
    import app.services.agentic_gate as gate
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    objects: dict[str, bytes] = {}
    state = SimpleNamespace(objects=objects, embedder=_Embedder(), vision=_Vision(), refusal=None)

    async def _download(name):
        if name not in objects:
            raise FileNotFoundInStorageError(name)
        return objects[name]

    async def _upload_to(data, name, content_type=None):
        objects[name] = data
        return name

    async def _delete(name):
        objects.pop(name, None)

    async def _modified(prefix):
        return [(name, state.modified.get(name)) for name in objects if name.startswith(prefix)]

    state.modified = {}

    async def _embedding_provider(_db, user_id=None):
        return state.embedder, "fake-embed", None

    async def _llm(_db, _user_id, *args, **kwargs):
        return state.vision, "vision-model"

    async def _refusal(_db):
        return state.refusal

    monkeypatch.setattr(file_storage, "download", _download)
    monkeypatch.setattr(file_storage, "upload_to", _upload_to)
    monkeypatch.setattr(file_storage, "delete", _delete)
    monkeypatch.setattr(file_storage, "list_objects_modified", _modified)
    monkeypatch.setattr(router, "get_embedding_provider", _embedding_provider)
    monkeypatch.setattr(router, "get_llm_for_user", _llm)
    monkeypatch.setattr(gate, "agentic_refusal", _refusal)
    return state


async def _upload(scratch: ScratchDatabase, data: bytes, *, describe_figures: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    """A knowledge base and one PDF added through the shared upload, committed."""
    async with scratch.sessions() as db:
        user = User(email=f"fig-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Owner")
        db.add(user)
        await db.flush()
        kb = KnowledgeBase(name=f"kb-{uuid.uuid4().hex[:8]}", user_id=user.id, describe_figures=describe_figures)
        db.add(kb)
        await db.flush()
        doc = await kb_documents.add_file_document(db, kb, filename="method.pdf", data=data)
        await db.commit()
        return kb.id, doc.id


async def _document(scratch: ScratchDatabase, doc_id: uuid.UUID) -> KnowledgeDocument:
    async with scratch.sessions() as db:
        return await db.get(KnowledgeDocument, doc_id)


async def _state(scratch: ScratchDatabase, doc_id: uuid.UUID) -> tuple[str | None, str | None, int]:
    """``(figure status, note, pages described)`` of the document as stored."""
    doc = await _document(scratch, doc_id)
    return kb_figures.figure_state(doc.figure_reading, doc.content)


async def _chunks(scratch: ScratchDatabase, doc_id: uuid.UUID) -> list[str]:
    async with scratch.sessions() as db:
        rows = await db.execute(select(Embedding.content).where(Embedding.source_id == str(doc_id)))
        return list(rows.scalars().all())


async def _activities(scratch: ScratchDatabase, kb_id: uuid.UUID) -> list[str]:
    async with scratch.sessions() as db:
        rows = await db.execute(
            select(KBActivityLog.details).where(
                KBActivityLog.knowledge_base_id == kb_id, KBActivityLog.event == "embedding_completed"
            )
        )
        return [row for row in rows.scalars().all() if row]


async def test_an_uploaded_pdf_is_indexed_with_its_figure_descriptions(platform):
    async with scratch_database("kbfigs", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        kb_id, doc_id = await _upload(scratch, FIGURE_PDF)
        name = kb_figure_pass.source_object_name(kb_id, doc_id)
        assert list(platform.objects) == [name]

        await document_processor.process_document(str(doc_id))

        doc = await _document(scratch, doc_id)
        assert doc.status == "ready"
        assert platform.vision.calls == 1
        assert doc.figure_reading["status"] == kb_figures.STATUS_COMPLETE
        assert kb_figures.figure_state(doc.figure_reading, doc.content) == (kb_figures.STATUS_COMPLETE, None, 1)
        # The stored source is still the text layer; the index has the graph.
        assert GRAPH not in doc.content
        chunks = await _chunks(scratch, doc_id)
        assert len(chunks) == doc.chunk_count
        assert any("Narrative -> Channel: amplified by" in chunk for chunk in chunks)
        assert any("[Figures, page 2 - described from the page image by a model" in chunk for chunk in chunks)
        # The PDF is gone once the pass is final.
        assert platform.objects == {}
        assert await _activities(scratch, kb_id) == [
            "Embedded 'method.pdf' — "
            f"{doc.chunk_count} chunk(s), figures described on 1 page(s)"
        ]
        assert await _state(scratch, doc_id) == (kb_figures.STATUS_COMPLETE, None, 1)


async def test_a_failed_pass_indexes_the_text_says_so_and_a_reprocess_retries_it(platform):
    platform.vision = _Vision(fail=True)
    async with scratch_database("kbfigs", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        kb_id, doc_id = await _upload(scratch, FIGURE_PDF)

        await document_processor.process_document(str(doc_id))

        doc = await _document(scratch, doc_id)
        assert doc.status == "ready"  # searchable by its text...
        assert doc.chunk_count >= 1
        status, note, _pages = await _state(scratch, doc_id)
        assert status == kb_figures.STATUS_FAILED  # ...and never shown as fully processed
        assert note == (
            "Figures could not be described on 1 of 1 page(s) (the model could not read the page image); "
            "those pages are indexed as text only. Re-embed the document to try again."
        )
        assert not any("[Figures," in chunk for chunk in await _chunks(scratch, doc_id))
        assert await _activities(scratch, kb_id) == [
            f"Embedded 'method.pdf' — {doc.chunk_count} chunk(s), {note}"
        ]
        assert list(platform.objects) == [kb_figure_pass.source_object_name(kb_id, doc_id)]

        # A reprocess runs the pass again, for the failed page only.
        platform.vision = _Vision()
        async with scratch.sessions() as db:
            row = await db.get(KnowledgeDocument, doc_id)
            row.status = "pending"
            await db.commit()
        await document_processor.process_document(str(doc_id))

        doc = await _document(scratch, doc_id)
        assert platform.vision.calls == 1
        assert (await _state(scratch, doc_id))[0] == kb_figures.STATUS_COMPLETE
        assert any("Narrative -> Channel" in chunk for chunk in await _chunks(scratch, doc_id))
        assert platform.objects == {}


async def test_a_refused_gate_stops_the_job_at_its_entry_and_keeps_the_pdf(platform):
    """The background gate is asked where the document job starts: a refused
    job leaves the document pending with its PDF kept, and no model is called."""
    from app import worker

    platform.refusal = "license_required"
    async with scratch_database("kbfigs", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        kb_id, doc_id = await _upload(scratch, FIGURE_PDF)

        await worker.process_document_job({}, document_id=str(doc_id))

        doc = await _document(scratch, doc_id)
        assert platform.vision.calls == 0
        assert platform.embedder.inputs == []
        assert doc.status == "pending"
        assert doc.figure_reading["status"] == kb_figures.STATUS_PENDING
        assert list(platform.objects) == [kb_figure_pass.source_object_name(kb_id, doc_id)]


async def test_a_pdf_without_text_layer_is_indexed_from_its_transcription(platform):
    platform.vision = _Vision("SITUATION REPORT\nThe campaign reused 14 domains.")
    async with scratch_database("kbfigs", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        _kb_id, doc_id = await _upload(scratch, SCANNED_PDF)
        assert (await _document(scratch, doc_id)).content == ""

        await document_processor.process_document(str(doc_id))

        doc = await _document(scratch, doc_id)
        assert doc.status == "ready"
        chunks = await _chunks(scratch, doc_id)
        assert any("The campaign reused 14 domains." in chunk for chunk in chunks)
        assert any("[Page 1, transcribed from its image by a model" in chunk for chunk in chunks)


async def test_a_kb_that_does_not_describe_figures_keeps_and_describes_nothing(platform):
    async with scratch_database("kbfigs", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        _kb_id, doc_id = await _upload(scratch, FIGURE_PDF, describe_figures=False)
        assert platform.objects == {}

        await document_processor.process_document(str(doc_id))

        doc = await _document(scratch, doc_id)
        assert doc.status == "ready"
        assert doc.figure_reading is None
        assert platform.vision.calls == 0
        assert await _state(scratch, doc_id) == (None, None, 0)


async def test_turning_the_setting_off_embeds_the_text_layer_alone(platform):
    async with scratch_database("kbfigs", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        kb_id, doc_id = await _upload(scratch, FIGURE_PDF)
        await document_processor.process_document(str(doc_id))
        async with scratch.sessions() as db:
            (await db.get(KnowledgeBase, kb_id)).describe_figures = False
            (await db.get(KnowledgeDocument, doc_id)).status = "pending"
            await db.commit()

        await document_processor.process_document(str(doc_id))

        assert platform.vision.calls == 1  # the reading is kept, never bought again
        assert not any("[Figures," in chunk for chunk in await _chunks(scratch, doc_id))



async def test_the_sweep_deletes_the_pdfs_no_pass_will_read(platform):
    async with scratch_database("kbfigs", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        kb_id, kept_id = await _upload(scratch, FIGURE_PDF)
        kept = kb_figure_pass.source_object_name(kb_id, kept_id)
        orphan = kb_figure_pass.source_object_name(kb_id, uuid.uuid4())
        fresh_orphan = kb_figure_pass.source_object_name(kb_id, uuid.uuid4())
        unaged = kb_figure_pass.source_object_name(kb_id, uuid.uuid4())
        for name in (orphan, fresh_orphan, unaged):
            platform.objects[name] = b"%PDF-"
        now = datetime.now(UTC)
        platform.modified.update(
            {kept: now - timedelta(days=2), orphan: now - timedelta(days=2), fresh_orphan: now - timedelta(minutes=5)}
        )

        async with scratch.sessions() as db:
            assert await kb_figure_pass.sweep_sources(db, now=now) == 1
        assert sorted(platform.objects) == sorted([kept, fresh_orphan, unaged])

        # Past the retention, a PDF goes even while its document names it;
        # one the store cannot age is never deleted on a guess.
        async with scratch.sessions() as db:
            assert await kb_figure_pass.sweep_sources(db, now=now + kb_figure_pass.SOURCE_RETENTION) == 2
        assert platform.objects == {unaged: b"%PDF-"}
