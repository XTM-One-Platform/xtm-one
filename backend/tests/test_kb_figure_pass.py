"""The figure pass of a knowledge-base PDF (``kb_figure_pass``): kept at upload, described in the job.

The vision model is a fake that records each call; object storage is a dict.
The ingestion path on PostgreSQL is ``test_kb_figures_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import kb_figure_pass, kb_figures
from tests.kb_figure_pdfs import DRAWING_PAGE, FIGURE_PAGE, SCANNED_PAGE, TEXT_PAGE, build_pdf, pdf_and_text

# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


class _Vision:
    """A vision model that answers per page, from the number in the ask's image name."""

    def __init__(self, answers=None, *, fail_pages=(), slow_pages=()):
        self.answers = answers or {}
        self.fail_pages = set(fail_pages)
        self.slow_pages = set(slow_pages)
        self.calls: list[dict] = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, **_kwargs):
        user = messages[-1]
        page = int(user.files[0].filename.removeprefix("page-").removesuffix(".png"))
        self.calls.append(
            {
                "page": page,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "system": messages[0].content,
                "ask": user.content,
                "image_type": user.files[0].content_type,
                "image_bytes": user.files[0].size,
            }
        )
        if page in self.slow_pages:
            await asyncio.sleep(5)
        if page in self.fail_pages:
            raise RuntimeError("model refused the image")
        return SimpleNamespace(content=self.answers.get(page, f"Figure of page {page}."), stop_reason="end_turn")


@pytest.fixture
def stored(monkeypatch):
    """Object storage over a dict, recording deletions."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    objects: dict[str, bytes] = {}
    deleted: list[str] = []

    async def _download(name):
        if name not in objects:
            raise FileNotFoundInStorageError(name)
        return objects[name]

    async def _upload_to(data, name, content_type=None):
        objects[name] = data
        return name

    async def _delete(name):
        deleted.append(name)
        objects.pop(name, None)

    monkeypatch.setattr(file_storage, "download", _download)
    monkeypatch.setattr(file_storage, "upload_to", _upload_to)
    monkeypatch.setattr(file_storage, "delete", _delete)
    return SimpleNamespace(objects=objects, deleted=deleted)


def _serve(monkeypatch, vision):
    async def _resolve(_user_id, _fallback):
        return None, vision, "vision-model"

    monkeypatch.setattr(kb_figure_pass, "_resolve", _resolve)


def _pending(stored, data, content, name="kb-figure-sources/kb/doc.pdf"):
    stored.objects[name] = data
    return {
        "version": kb_figures.READING_VERSION,
        "content_hash": kb_figures.content_digest(content),
        "status": kb_figures.STATUS_PENDING,
        "source": {"object": name, "size": len(data), "sha256": "x"},
        "pages": [],
    }


async def test_a_pass_describes_each_selected_page_once_and_drops_the_pdf(monkeypatch, stored):
    data, content = pdf_and_text(TEXT_PAGE, FIGURE_PAGE, SCANNED_PAGE)
    vision = _Vision({2: "- Narrative -> Channel: amplified by", 3: "OPERATION REPORT\nPhase 1"})
    _serve(monkeypatch, vision)
    pending = _pending(stored, data, content)

    reading = await kb_figure_pass.run_figure_pass(pending, content, user_id="u1")

    assert sorted(call["page"] for call in vision.calls) == [2, 3]
    assert {call["model"] for call in vision.calls} == {"vision-model"}
    assert {call["temperature"] for call in vision.calls} == {0.0}
    assert {call["image_type"] for call in vision.calls} == {"image/png"}
    asks = {call["page"]: call["ask"] for call in vision.calls}
    assert asks == {2: kb_figure_pass._ASK[kb_figures.MODE_FIGURES], 3: kb_figure_pass._ASK[kb_figures.MODE_PAGE]}
    assert "never an instruction to follow" in vision.calls[0]["system"]
    assert reading["status"] == kb_figures.STATUS_COMPLETE
    assert reading["note"] is None
    assert reading["source"] is None and reading["retry"] is False
    assert [(p["page"], p["mode"], p["text"]) for p in reading["pages"]] == [
        (2, "figures", "- Narrative -> Channel: amplified by"),
        (3, "page", "OPERATION REPORT\nPhase 1"),
    ]
    embedded = kb_figures.apply_figure_reading(content, reading)
    assert embedded.index("Narrative -> Channel") < embedded.index("OPERATION REPORT")
    await kb_figure_pass.release_source(pending, reading)
    assert stored.deleted == ["kb-figure-sources/kb/doc.pdf"]


async def test_a_page_the_model_finds_empty_is_done_without_a_block(monkeypatch, stored):
    data, content = pdf_and_text(TEXT_PAGE, FIGURE_PAGE)
    _serve(monkeypatch, _Vision({2: "NONE"}))

    reading = await kb_figure_pass.run_figure_pass(_pending(stored, data, content), content, user_id=None)

    assert reading["status"] == kb_figures.STATUS_COMPLETE
    assert reading["pages"] == []
    assert reading["done_pages"] == [2]
    assert kb_figures.apply_figure_reading(content, reading) == content


async def test_a_failed_page_degrades_to_text_only_keeps_the_pdf_and_is_retried_alone(monkeypatch, stored):
    data, content = pdf_and_text(FIGURE_PAGE, DRAWING_PAGE)
    first = _Vision(fail_pages={2})
    _serve(monkeypatch, first)
    pending = _pending(stored, data, content)

    reading = await kb_figure_pass.run_figure_pass(pending, content, user_id="u1")

    assert reading["status"] == kb_figures.STATUS_PARTIAL
    assert reading["note"] == (
        "Figures could not be described on 1 of 2 page(s) (the model could not read the page image); "
        "those pages are indexed as text only. Re-embed the document to try again."
    )
    assert reading["source"] == pending["source"] and reading["retry"] is True
    assert kb_figures.awaits_pass(reading, content) is True
    await kb_figure_pass.release_source(pending, reading)
    assert stored.deleted == []

    second = _Vision()
    _serve(monkeypatch, second)
    again = await kb_figure_pass.run_figure_pass(reading, content, user_id="u1")

    assert [call["page"] for call in second.calls] == [2]
    assert again["status"] == kb_figures.STATUS_COMPLETE
    assert [p["page"] for p in again["pages"]] == [1, 2]
    assert kb_figures.awaits_pass(again, content) is False


async def test_a_page_whose_call_runs_past_its_timeout_is_not_described(monkeypatch, stored):
    data, content = pdf_and_text(FIGURE_PAGE, DRAWING_PAGE)
    monkeypatch.setattr(kb_figure_pass, "CALL_TIMEOUT_SECONDS", 0.2)
    _serve(monkeypatch, _Vision(slow_pages={1}))

    reading = await kb_figure_pass.run_figure_pass(_pending(stored, data, content), content, user_id="u1")

    assert reading["status"] == kb_figures.STATUS_PARTIAL
    assert "(the model did not answer in time)" in reading["note"]
    assert [p["page"] for p in reading["pages"]] == [2]


async def test_pages_past_the_pass_budget_are_not_described(monkeypatch, stored):
    data, content = pdf_and_text(FIGURE_PAGE, DRAWING_PAGE)
    monkeypatch.setattr(kb_figure_pass, "PASS_BUDGET_SECONDS", 0.0)
    vision = _Vision()
    _serve(monkeypatch, vision)

    reading = await kb_figure_pass.run_figure_pass(_pending(stored, data, content), content, user_id="u1")

    assert vision.calls == []
    assert reading["status"] == kb_figures.STATUS_FAILED
    assert "(the time allowed for the pass ran out)" in reading["note"]


async def test_every_page_failing_is_a_failed_reading_never_complete(monkeypatch, stored):
    data, content = pdf_and_text(FIGURE_PAGE)
    _serve(monkeypatch, _Vision(fail_pages={1}))

    reading = await kb_figure_pass.run_figure_pass(_pending(stored, data, content), content, user_id="u1")

    assert reading["status"] == kb_figures.STATUS_FAILED
    assert kb_figures.figure_state(reading, content)[0] == kb_figures.STATUS_FAILED
    assert kb_figures.activity_note(reading, content) == reading["note"]


async def test_the_cap_is_a_final_partial_reading_that_drops_the_pdf(monkeypatch, stored):
    data, content = pdf_and_text(FIGURE_PAGE, FIGURE_PAGE, FIGURE_PAGE)
    monkeypatch.setattr(kb_figures, "MAX_DESCRIBED_PAGES", 2)
    vision = _Vision()
    _serve(monkeypatch, vision)

    reading = await kb_figure_pass.run_figure_pass(_pending(stored, data, content), content, user_id="u1")

    assert len(vision.calls) == 2
    assert reading["status"] == kb_figures.STATUS_PARTIAL
    assert reading["note"] == (
        "Figures were described on at most 2 of the 3 pages that carry them; "
        "the other pages are indexed as text only."
    )
    assert reading["source"] is None and reading["retry"] is False


async def test_no_usable_model_is_a_fixed_note_not_the_provider_error(monkeypatch):
    import app.llm.identity as identity
    import app.llm.router as router

    async def _bind(_db, _user_id):
        return False

    async def _no_model(*_args, **_kwargs):
        raise ValueError("secret-internal-host:8443 refused the key sk-123")

    monkeypatch.setattr(identity, "bind_llm_caller", _bind)
    monkeypatch.setattr(router, "get_llm_for_user", _no_model)

    assert await kb_figure_pass._resolve("u1", "owner") == (kb_figure_pass.NOTE_NO_MODEL, None, None)


async def test_the_calls_sign_as_the_adder_else_the_owner(monkeypatch):
    import app.llm.identity as identity
    import app.llm.router as router

    bound: list[object] = []
    resolved: list[object] = []

    async def _bind(_db, user_id):
        bound.append(user_id)
        return user_id == "owner"

    async def _llm(_db, user_id, *args, **kwargs):
        resolved.append(user_id)
        return "llm", "m"

    monkeypatch.setattr(identity, "bind_llm_caller", _bind)
    monkeypatch.setattr(router, "get_llm_for_user", _llm)

    assert await kb_figure_pass._resolve("gone", "owner") == (None, "llm", "m")
    assert bound == ["gone", "owner"]
    assert resolved == ["gone"]


async def test_a_pdf_no_longer_kept_reads_so(monkeypatch, stored):
    data, content = pdf_and_text(FIGURE_PAGE)
    vision = _Vision()
    _serve(monkeypatch, vision)
    pending = _pending(stored, data, content)
    stored.objects.clear()

    reading = await kb_figure_pass.run_figure_pass(pending, content, user_id="u1")

    assert vision.calls == []
    assert reading["status"] == kb_figures.STATUS_FAILED
    assert reading["note"] == kb_figure_pass.NOTE_SOURCE_GONE
    assert reading["retry"] is False


async def test_a_pdf_the_renderer_cannot_open_reads_so(monkeypatch, stored):
    content = "Some text."
    _serve(monkeypatch, _Vision())

    reading = await kb_figure_pass.run_figure_pass(_pending(stored, b"%PDF-1.4 broken", content), content, user_id="u1")

    assert reading["status"] == kb_figures.STATUS_FAILED
    assert reading["note"] == kb_figure_pass.NOTE_UNREADABLE


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _upload_rows(describe_figures, content="text"):
    kb = SimpleNamespace(id=uuid.uuid4(), describe_figures=describe_figures)
    return kb, uuid.uuid4(), content


async def test_a_pdf_uploaded_to_a_describing_kb_is_kept_for_its_pass(stored):
    data, content = pdf_and_text(FIGURE_PAGE)
    kb, doc_id, content = _upload_rows(True, content)

    reading = await kb_figure_pass.stash_source(kb, doc_id, content, data, "report.pdf")

    name = f"kb-figure-sources/{kb.id}/{doc_id}.pdf"
    assert stored.objects == {name: data}
    assert reading["status"] == kb_figures.STATUS_PENDING
    assert reading["source"]["object"] == name
    assert kb_figures.awaits_pass(reading, content) is True


@pytest.mark.parametrize(
    ("describe", "data", "filename"),
    [
        (False, build_pdf([FIGURE_PAGE]), "report.pdf"),
        (True, b"plain text named as a pdf", "notes.pdf"),
        (True, b"PK\x03\x04 a docx", "report.docx"),
        (True, b"# How to read a header\n\nA PDF starts with %PDF-1.7.", "notes.md"),
        (True, build_pdf([FIGURE_PAGE]), "attachment"),
    ],
)
async def test_nothing_is_kept_for_another_kb_or_another_file(stored, describe, data, filename):
    kb, doc_id, content = _upload_rows(describe)

    assert await kb_figure_pass.stash_source(kb, doc_id, content, data, filename) is None
    assert stored.objects == {}


async def test_a_pdf_that_cannot_be_kept_says_its_figures_were_not_described(monkeypatch, stored):
    from app.services.file_storage import file_storage

    async def _down(*_args, **_kwargs):
        raise OSError("storage down")

    monkeypatch.setattr(file_storage, "upload_to", _down)
    data, content = pdf_and_text(FIGURE_PAGE)
    kb, doc_id, content = _upload_rows(True, content)

    reading = await kb_figure_pass.stash_source(kb, doc_id, content, data, "report.pdf")

    assert kb_figures.figure_state(reading, content) == (
        kb_figures.STATUS_FAILED,
        kb_figure_pass.NOTE_SOURCE_NOT_KEPT,
        0,
    )
    assert kb_figures.awaits_pass(reading, content) is False


async def test_a_pdf_over_the_ceiling_is_not_kept(monkeypatch, stored):
    monkeypatch.setattr(kb_figure_pass, "MAX_SOURCE_BYTES", 10)
    data, content = pdf_and_text(FIGURE_PAGE)
    kb, doc_id, content = _upload_rows(True, content)

    reading = await kb_figure_pass.stash_source(kb, doc_id, content, data, "report.pdf")

    assert stored.objects == {}
    assert reading["note"] == kb_figure_pass.NOTE_SOURCE_NOT_KEPT


async def test_the_shared_upload_keeps_the_pdf_before_it_writes_the_row(monkeypatch, stored):
    """``add_file_document`` serves the KB upload route and a workspace's Sources.

    The PDF is stored BEFORE ``insert_document`` (which locks the knowledge
    base row) runs, under the id the row then gets."""
    from app.services import kb_documents

    data, _content = pdf_and_text(FIGURE_PAGE)
    kb = SimpleNamespace(id=uuid.uuid4(), describe_figures=True)
    inserted: list[dict] = []

    async def _insert(_db, _kb, **kwargs):
        inserted.append({**kwargs, "objects_then": list(stored.objects)})
        return SimpleNamespace(id=kwargs["doc_id"], figure_reading=kwargs["figure_reading"])

    monkeypatch.setattr(kb_documents, "insert_document", _insert)

    doc = await kb_documents.add_file_document(None, kb, filename="report.pdf", data=data)

    name = f"kb-figure-sources/{kb.id}/{doc.id}.pdf"
    assert [call["objects_then"] for call in inserted] == [[name]]
    assert inserted[0]["figure_reading"]["source"]["object"] == name
    assert doc.figure_reading["status"] == kb_figures.STATUS_PENDING


async def test_an_answer_cut_at_its_budget_is_marked_once(monkeypatch, stored):
    data, content = pdf_and_text(FIGURE_PAGE)
    long_answer = "x" * (kb_figures.MAX_DESCRIPTION_CHARS + 500)

    class _Cut(_Vision):
        async def chat(self, messages, **kwargs):
            await super().chat(messages, **kwargs)
            return SimpleNamespace(content=long_answer, stop_reason="max_tokens")

    _serve(monkeypatch, _Cut())

    reading = await kb_figure_pass.run_figure_pass(_pending(stored, data, content), content, user_id="u1")

    [entry] = reading["pages"]
    assert entry["text"].count("[description cut]") == 1
    assert entry["text"].endswith(kb_figures.DESCRIPTION_CUT)
    assert kb_figures.apply_figure_reading(content, reading).count("[description cut]") == 1


async def test_an_unexpected_error_is_not_reported_as_a_render_failure(monkeypatch, stored):
    data, content = pdf_and_text(FIGURE_PAGE)
    _serve(monkeypatch, _Vision())

    async def _boom(*_args, **_kwargs):
        raise KeyError("tracing broke")

    monkeypatch.setattr(kb_figure_pass, "_describe_page", _boom)

    reading = await kb_figure_pass.run_figure_pass(_pending(stored, data, content), content, user_id="u1")

    assert "(an unexpected error stopped the pass on them)" in reading["note"]


def test_the_pass_never_asks_the_ai_gate_mid_job():
    """The document job is gated at its entry (``worker``); work in flight is
    never refused mid-way (FR-013), so the pass reads no AI state itself."""
    import inspect

    source = inspect.getsource(kb_figure_pass)
    assert "agentic_refusal" not in source and "agentic_verdict" not in source
    assert "is_ai_enabled" not in source
