"""Full source-document access for knowledge bases.

``search_knowledge`` returns embedding chunks, which is lossy by design: an
agent could neither reconstruct a document verbatim nor even tell which document
a fragment came from. These tests cover the three capabilities that close that
gap:

1. ``get_knowledge_document`` — the complete, verbatim source plus the embedding
   status, readable even while the document is still ``pending`` (the
   write-then-read case), and windowed explicitly rather than silently truncated
   for large documents.
2. ``update_knowledge_document`` — replace/append the stored source and fire a
   re-chunk + re-embed so the vector layer converges on the new content.
3. ``search_knowledge`` provenance — every chunk is tagged with its source
   document's title and id, which is what makes the chunk -> document handoff
   possible at all.

Plus the REST equivalents (``GET`` / ``PUT
/knowledge-bases/{kb_id}/documents/{doc_id}``) and a route-ordering guard: a
bare ``{doc_id}`` path declared before ``documents/count`` would shadow it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import knowledge_bases as kb_api
from app.core.database import get_db
from app.services.builtin_tools import (
    build_kb_document_read_tools,
    build_kb_document_update_tool,
)
from app.tools.base import ToolCall

KB_A = str(uuid.uuid4())
KB_B = str(uuid.uuid4())
DOC_ID = str(uuid.uuid4())

# PostgreSQL text columns cannot store this byte, which is why both write paths
# reject it up front rather than letting asyncpg fail mid-flush.
_NUL = "\x00"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    """Minimal stand-in for a SQLAlchemy ``Result``."""

    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Routes statements by target table so the tools' query sequence works.

    ``doc_results`` is consumed in order, one entry per ``knowledge_documents``
    SELECT — which is exactly how the lookup escalates: id lookups issue one
    query, title lookups issue an exact match, then a case-insensitive retry,
    then a hint listing.
    """

    def __init__(self, *, doc_results=(), kb=None, doc_total=None):
        self.doc_results = [list(r) for r in doc_results]
        self.kb = kb
        self.doc_total = doc_total
        self.statements: list[str] = []
        self.raw_statements: list = []
        self.deletes: list[str] = []
        self.added: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        self.raw_statements.append(statement)
        if sql.lstrip().upper().startswith("DELETE"):
            self.deletes.append(sql)
            return _Result([])
        if "count(" in sql:
            total = (
                self.doc_total
                if self.doc_total is not None
                else sum(len(r) for r in self.doc_results)
            )
            return _Result([total])
        if "knowledge_bases" in sql and "knowledge_documents" not in sql:
            return _Result([self.kb] if self.kb is not None else [])
        return _Result(self.doc_results.pop(0) if self.doc_results else [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def _kb(
    kb_id=KB_A,
    name="Docs",
    *,
    is_builtin=False,
    is_read_only=False,
    chunk_count=12,
):
    return SimpleNamespace(
        id=uuid.UUID(kb_id),
        name=name,
        is_builtin=is_builtin,
        is_read_only=is_read_only,
        is_company_managed=False,
        user_id=uuid.uuid4(),
        chunk_count=chunk_count,
    )


def _doc(
    *,
    doc_id=DOC_ID,
    kb_id=KB_A,
    filename="baseline.md",
    content="the complete verbatim source",
    status="ready",
    chunk_count=3,
):
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.UUID(doc_id),
        knowledge_base_id=uuid.UUID(kb_id),
        filename=filename,
        source_type="markdown",
        source_url=None,
        status=status,
        chunk_count=chunk_count,
        error_message=None,
        content=content,
        content_hash="old-hash",
        extra_metadata=None,
        embedding_retry_count=2,
        embedding_next_retry_at=now,
        created_at=now,
        updated_at=now,
    )


def _doc_row(
    *,
    doc_id=DOC_ID,
    kb_id=KB_A,
    filename="baseline.md",
    status="ready",
    chunk_count=3,
    content_length=28,
    extra_metadata=None,
):
    """One row of the ``list_knowledge_documents`` column select."""
    return (
        uuid.UUID(doc_id),
        uuid.UUID(kb_id),
        filename,
        "markdown",
        status,
        chunk_count,
        content_length,
        extra_metadata,
    )


def _read_executor(kbs=None):
    result = build_kb_document_read_tools(
        None, "user-1", kbs if kbs is not None else [(KB_A, "Docs")]
    )
    assert result is not None
    return result


def _update_executor(kbs=None):
    result = build_kb_document_update_tool(
        None, "user-1", kbs if kbs is not None else [(KB_A, "Docs")]
    )
    assert result is not None
    return result


async def _call(executor, name, arguments):
    out = await executor.execute_tool(
        ToolCall(id="call-1", name=name, arguments=arguments)
    )
    return out.content


def _patch_session(monkeypatch, session):
    monkeypatch.setattr("app.core.database.async_session", lambda: session)


# ---------------------------------------------------------------------------
# Builder contracts
# ---------------------------------------------------------------------------


def test_builders_return_none_without_scope():
    """No readable / writable KB means the tools must not be offered at all."""
    assert build_kb_document_read_tools(None, "user-1", []) is None
    assert build_kb_document_update_tool(None, "user-1", []) is None


def test_single_kb_hides_the_knowledge_base_argument():
    """With one KB in scope the target argument is noise — omit it."""
    read_defs, _ = _read_executor()
    update_defs, _ = _update_executor()
    for definition in [*read_defs, *update_defs]:
        assert "knowledge_base" not in definition.input_schema["properties"], (
            definition.name
        )


def test_multi_kb_exposes_the_knowledge_base_argument():
    read_defs, _ = _read_executor([(KB_A, "Docs"), (KB_B, "Notes")])
    update_defs, _ = _update_executor([(KB_A, "Docs"), (KB_B, "Notes")])
    for definition in [*read_defs, *update_defs]:
        assert "knowledge_base" in definition.input_schema["properties"], (
            definition.name
        )


def test_update_tool_description_names_the_writable_kbs_with_ids():
    """KB names are not unique, so the id must be offered for disambiguation."""
    definitions, _ = _update_executor([(KB_A, "Docs"), (KB_B, "Docs")])
    description = definitions[0].input_schema["properties"]["knowledge_base"][
        "description"
    ]
    assert KB_A in description and KB_B in description


# ---------------------------------------------------------------------------
# get_knowledge_document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_document_returns_the_full_verbatim_source(monkeypatch):
    content = "# Baseline\n\nline one\nline two\n"
    session = _FakeSession(doc_results=[[_doc(content=content)]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    assert content in out
    assert "Source (complete" in out
    assert f"id: {DOC_ID}" in out
    assert "Embedding status: ready" in out
    assert f"Source length: {len(content)} characters" in out


@pytest.mark.asyncio
async def test_get_document_scopes_the_lookup_to_the_readable_kbs(monkeypatch):
    """The ``knowledge_base_id IN (...)`` predicate is the access boundary."""
    session = _FakeSession(doc_results=[[_doc()]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor([(KB_A, "Docs"), (KB_B, "Notes")])

    await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    doc_query = session.statements[0]
    assert "knowledge_documents.knowledge_base_id IN" in doc_query


@pytest.mark.asyncio
async def test_get_document_reads_a_pending_document(monkeypatch):
    """Write-then-read: the source lives on the row, not in the vector index."""
    session = _FakeSession(
        doc_results=[[_doc(status="pending", chunk_count=0)]], kb=_kb()
    )
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    assert "Embedding status: pending" in out
    assert "not fully indexed yet" in out
    assert "the complete verbatim source" in out


@pytest.mark.asyncio
async def test_get_document_windows_large_content_and_reports_the_next_offset(
    monkeypatch,
):
    """A large document must page explicitly, never lose its tail silently."""
    from app.services.builtin_tools import kb as kb_tools

    body = "x" * 120
    session = _FakeSession(doc_results=[[_doc(content=body)]], kb=_kb())
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(kb_tools, "_DOC_WINDOW_DEFAULT_CHARS", 50)
    _, executor = _read_executor()

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    assert "characters 0-50 of 120" in out
    assert "70 characters remain" in out
    assert "offset=50" in out


@pytest.mark.asyncio
async def test_get_document_continues_from_an_offset(monkeypatch):
    body = "".join(str(i % 10) for i in range(120))
    session = _FakeSession(doc_results=[[_doc(content=body)]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(
        executor,
        "get_knowledge_document",
        {"document_id": DOC_ID, "offset": 100, "max_chars": 20},
    )

    assert "characters 100-120 of 120" in out
    assert body[100:120] in out
    assert "remain" not in out


@pytest.mark.asyncio
async def test_get_document_rejects_an_offset_past_the_end(monkeypatch):
    session = _FakeSession(doc_results=[[_doc(content="short")]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(
        executor, "get_knowledge_document", {"document_id": DOC_ID, "offset": 500}
    )

    assert "past the end" in out


@pytest.mark.asyncio
async def test_get_document_distinguishes_empty_content_from_no_content(
    monkeypatch,
):
    """An intentionally empty document must round-trip as empty, not missing."""
    empty = _FakeSession(doc_results=[[_doc(content="")]], kb=_kb())
    _patch_session(monkeypatch, empty)
    _, executor = _read_executor()
    out_empty = await _call(
        executor, "get_knowledge_document", {"document_id": DOC_ID}
    )
    assert "Source (complete, 0 characters)" in out_empty

    missing = _FakeSession(doc_results=[[_doc(content=None)]], kb=_kb())
    _patch_session(monkeypatch, missing)
    _, executor = _read_executor()
    out_missing = await _call(
        executor, "get_knowledge_document", {"document_id": DOC_ID}
    )
    assert "no stored text" in out_missing


@pytest.mark.asyncio
async def test_get_document_by_title_falls_back_to_case_insensitive(monkeypatch):
    session = _FakeSession(doc_results=[[], [_doc()]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(executor, "get_knowledge_document", {"title": "BASELINE.MD"})

    assert "the complete verbatim source" in out
    assert "lower(" in session.statements[1]


@pytest.mark.asyncio
async def test_get_document_by_missing_title_lists_candidates(monkeypatch):
    hints = [("baseline.md", uuid.UUID(DOC_ID))]
    session = _FakeSession(doc_results=[[], [], hints], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(executor, "get_knowledge_document", {"title": "basline.md"})

    assert "no document titled 'basline.md'" in out
    assert "baseline.md" in out and DOC_ID in out


@pytest.mark.asyncio
async def test_get_document_by_ambiguous_title_is_rejected_with_ids(monkeypatch):
    """A title shared by several documents must never resolve arbitrarily."""
    other = str(uuid.uuid4())
    session = _FakeSession(
        doc_results=[[_doc(), _doc(doc_id=other, kb_id=KB_B)]], kb=_kb()
    )
    _patch_session(monkeypatch, session)
    _, executor = _read_executor([(KB_A, "Docs"), (KB_B, "Notes")])

    out = await _call(executor, "get_knowledge_document", {"title": "baseline.md"})

    assert "matches 2 documents" in out
    assert DOC_ID in out and other in out


@pytest.mark.asyncio
async def test_get_document_requires_an_identifier(monkeypatch):
    _, executor = _read_executor()
    out = await _call(executor, "get_knowledge_document", {})
    assert "provide either 'document_id'" in out


@pytest.mark.asyncio
async def test_get_document_rejects_a_malformed_id(monkeypatch):
    session = _FakeSession(kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(executor, "get_knowledge_document", {"document_id": "nope"})

    assert "not a valid document id" in out


@pytest.mark.asyncio
async def test_get_document_rejects_an_out_of_scope_knowledge_base(monkeypatch):
    _, executor = _read_executor([(KB_A, "Docs"), (KB_B, "Notes")])
    out = await _call(
        executor,
        "get_knowledge_document",
        {"document_id": DOC_ID, "knowledge_base": "Elsewhere"},
    )
    assert "not one of the knowledge bases this tool reads" in out


# ---------------------------------------------------------------------------
# list_knowledge_documents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_documents_reports_ids_status_and_remaining_count(monkeypatch):
    rows = [
        _doc_row(),
        _doc_row(doc_id=str(uuid.uuid4()), filename="notes.md", status="pending"),
    ]
    session = _FakeSession(doc_results=[rows], kb=_kb(), doc_total=7)
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(executor, "list_knowledge_documents", {})

    assert "2 of 7 document(s)" in out
    assert f"id: {DOC_ID}" in out
    assert "knowledge base: Docs" in out
    assert "status: ready" in out and "status: pending" in out
    assert "28 characters" in out
    assert "5 more" in out


@pytest.mark.asyncio
async def test_list_documents_does_not_load_document_content(monkeypatch):
    """Listing must size documents server-side, not stream every body over.

    ``select(KnowledgeDocument)`` would pull each document's full ``content`` —
    megabytes for exactly the large-document knowledge bases this feature exists
    to serve.
    """
    session = _FakeSession(doc_results=[[_doc_row()]], kb=_kb(), doc_total=1)
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    await _call(executor, "list_knowledge_documents", {})

    listing_sql = [s for s in session.statements if "count(" not in s][-1]
    # The column is referenced exactly once, and only inside ``length()`` — a
    # selected ``knowledge_documents.content`` would push the count past one.
    assert "length(knowledge_documents.content)" in listing_sql
    assert listing_sql.count("knowledge_documents.content") == 1


@pytest.mark.asyncio
async def test_list_documents_filters_by_status(monkeypatch):
    session = _FakeSession(doc_results=[[]], kb=_kb(), doc_total=0)
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    out = await _call(executor, "list_knowledge_documents", {"status": "error"})

    assert "No documents" in out and "'error'" in out
    assert any("status" in sql for sql in session.statements)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "expected"),
    [(10_000, 200), (0, 50), (-5, 50), ("not-a-number", 50), (None, 50)],
)
async def test_list_documents_clamps_the_limit(monkeypatch, requested, expected):
    """An unbounded (or nonsense) limit must never reach the query.

    Non-positive and unparseable values fall back to the default page rather
    than clamping to 1 — a single row would misrepresent the knowledge base as
    nearly empty.
    """
    session = _FakeSession(doc_results=[[_doc_row()]], kb=_kb(), doc_total=1)
    _patch_session(monkeypatch, session)
    _, executor = _read_executor()

    await _call(executor, "list_knowledge_documents", {"limit": requested})

    listing = [
        s for s in session.raw_statements if "count(" not in str(s)
    ][-1]
    compiled = listing.compile(compile_kwargs={"literal_binds": True})
    assert f"LIMIT {expected}" in str(compiled)


# ---------------------------------------------------------------------------
# update_knowledge_document
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_update_deps(monkeypatch):
    """Patch the write-path collaborators the update tool imports lazily."""
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.kb_helpers.enqueue_document_processing", enqueue
    )
    monkeypatch.setattr(
        "app.services.kb_helpers.log_kb_activity", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.api.v1.helpers.is_package_origin_knowledge_base",
        AsyncMock(return_value=False),
    )
    return enqueue


@pytest.mark.asyncio
async def test_update_replaces_the_source_and_queues_a_reembed(
    monkeypatch, _stub_update_deps
):
    doc = _doc()
    kb = _kb(chunk_count=12)
    session = _FakeSession(doc_results=[[doc]], kb=kb)
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "brand new source"},
    )

    assert doc.content == "brand new source"
    assert doc.content_hash != "old-hash"
    # ``pending`` is load-bearing: the processor's duplicate-job guard drops any
    # enqueued document that is not pending on its first attempt.
    assert doc.status == "pending"
    assert doc.chunk_count == 0
    assert doc.error_message is None
    assert doc.embedding_retry_count == 0
    assert doc.embedding_next_retry_at is None
    # Stale vectors dropped up front, and the KB's denormalised count adjusted.
    assert session.deletes
    assert kb.chunk_count == 9
    assert session.commits == 1
    _stub_update_deps.assert_awaited_once_with(DOC_ID)
    assert "Replaced 'baseline.md'" in out
    assert "queued" in out


@pytest.mark.asyncio
async def test_update_append_concatenates_onto_the_existing_source(
    monkeypatch, _stub_update_deps
):
    doc = _doc(content="first")
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "second", "mode": "append"},
    )

    assert doc.content == "first\n\nsecond"
    assert "Appended to" in out


@pytest.mark.asyncio
async def test_update_with_identical_ready_content_is_a_no_op(
    monkeypatch, _stub_update_deps
):
    """Re-embedding unchanged, already-indexed content only burns provider calls."""
    doc = _doc(content="same", status="ready")
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "same"},
    )

    assert "No change" in out
    assert not session.deletes
    assert session.commits == 0
    _stub_update_deps.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_with_identical_errored_content_still_reembeds(
    monkeypatch, _stub_update_deps
):
    """Re-submitting the same content is how an agent retries a failed embed."""
    doc = _doc(content="same", status="error")
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "same"},
    )

    assert doc.status == "pending"
    _stub_update_deps.assert_awaited_once_with(DOC_ID)


@pytest.mark.asyncio
async def test_update_warns_when_the_reembed_could_not_be_queued(
    monkeypatch, _stub_update_deps
):
    """A dropped vector set with no queued re-embed must not read as success."""
    _stub_update_deps.return_value = False
    doc = _doc()
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "new"},
    )

    assert "could NOT be queued" in out
    assert "NOT searchable" in out


@pytest.mark.asyncio
async def test_update_refuses_a_builtin_knowledge_base(
    monkeypatch, _stub_update_deps
):
    doc = _doc()
    session = _FakeSession(doc_results=[[doc]], kb=_kb(is_builtin=True))
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "new"},
    )

    assert "built-in knowledge base" in out
    assert doc.content == "the complete verbatim source"
    _stub_update_deps.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_refuses_a_skill_package_knowledge_base(
    monkeypatch, _stub_update_deps
):
    monkeypatch.setattr(
        "app.api.v1.helpers.is_package_origin_knowledge_base",
        AsyncMock(return_value=True),
    )
    doc = _doc()
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "new"},
    )

    assert "skill package" in out
    _stub_update_deps.assert_not_awaited()


# Every failure of the write names its cause and whether a retry helps; none
# answers the old catch-all "Please try again later" (issue #10698).


class _DriverError(Exception):
    """A DBAPI error carrying a SQLSTATE, the way asyncpg's errors do."""

    def __init__(self, sqlstate):
        super().__init__("driver detail that must not reach the agent")
        self.sqlstate = sqlstate


class _FailingCommitSession(_FakeSession):
    def __init__(self, exc, **kwargs):
        super().__init__(**kwargs)
        self.exc = exc

    async def commit(self):
        raise self.exc


async def _update_failing_on_commit(monkeypatch, exc, content="new"):
    doc = _doc()
    session = _FailingCommitSession(exc, doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()
    return await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": content},
    )


def _db_error(cls, sqlstate):
    return cls("UPDATE knowledge_documents ...", {}, _DriverError(sqlstate))


@pytest.mark.asyncio
async def test_update_names_a_read_only_knowledge_base_as_final(
    monkeypatch, _stub_update_deps
):
    doc = _doc()
    session = _FakeSession(doc_results=[[doc]], kb=_kb(is_read_only=True))
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "new"},
    )

    assert "READ-ONLY" in out
    assert "not something you can retry" in out
    assert doc.content == "the complete verbatim source"
    _stub_update_deps.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_names_a_document_outside_the_writable_knowledge_bases(
    monkeypatch, _stub_update_deps
):
    session = _FakeSession(doc_results=[[]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": str(uuid.uuid4()), "content": "new"},
    )

    assert "no document with id" in out
    assert "list_knowledge_documents" in out
    assert "try again" not in out.lower()


@pytest.mark.asyncio
async def test_update_names_an_oversized_source_as_failing_again(
    monkeypatch, _stub_update_deps
):
    from sqlalchemy.exc import OperationalError

    out = await _update_failing_on_commit(
        monkeypatch, _db_error(OperationalError, "54000"), content="x" * 1234
    )

    assert "too large" in out
    assert "1234 characters" in out
    assert "fails again" in out
    assert "Nothing was changed" in out
    assert "try again" not in out.lower()


@pytest.mark.asyncio
async def test_update_names_a_transient_database_failure_as_retryable(
    monkeypatch, _stub_update_deps
):
    from sqlalchemy.exc import OperationalError

    out = await _update_failing_on_commit(
        monkeypatch, _db_error(OperationalError, "40P01")
    )

    assert "transient" in out
    assert "call again with the same arguments" in out
    assert "SQLSTATE 40P01" in out
    assert "committing the new source" in out
    assert "driver detail" not in out


@pytest.mark.asyncio
async def test_update_names_invalid_data_as_failing_again(
    monkeypatch, _stub_update_deps
):
    from sqlalchemy.exc import DataError

    out = await _update_failing_on_commit(monkeypatch, _db_error(DataError, "22021"))

    assert "invalid data" in out
    assert "fails again" in out
    assert "driver detail" not in out
    assert "UPDATE knowledge_documents" not in out


@pytest.mark.asyncio
async def test_update_names_an_unknown_failure_with_its_stage_and_no_retry(
    monkeypatch, _stub_update_deps
):
    out = await _update_failing_on_commit(monkeypatch, RuntimeError("secret detail"))

    assert "RuntimeError" in out
    assert "committing the new source" in out
    assert "not a known transient failure" in out
    assert "secret detail" not in out
    assert "Please try again later" not in out


@pytest.mark.asyncio
async def test_update_says_the_source_is_saved_when_the_reembed_queue_is_down(
    monkeypatch, _stub_update_deps
):
    """A queue failure after the commit is not a failed update: the source landed."""
    _stub_update_deps.side_effect = ConnectionError("queue down")
    doc = _doc()
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)
    _, executor = _update_executor()

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "saved source"},
    )

    assert doc.content == "saved source"
    assert session.commits == 1
    assert "Replaced 'baseline.md'" in out
    assert "could NOT be queued" in out
    assert "Failed to update" not in out


@pytest.mark.asyncio
async def test_update_rejects_an_invalid_mode():
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "x", "mode": "patch"},
    )
    assert "not a valid mode" in out


@pytest.mark.asyncio
async def test_update_rejects_nul_bytes():
    """PostgreSQL text columns cannot store NUL — fail with an actionable message."""
    _, executor = _update_executor()
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": f"a{_NUL}b"},
    )
    assert "NUL bytes" in out


@pytest.mark.asyncio
async def test_update_requires_an_identifier():
    _, executor = _update_executor()
    out = await _call(executor, "update_knowledge_document", {"content": "x"})
    assert "provide either 'document_id'" in out


@pytest.mark.asyncio
async def test_update_rejects_a_non_writable_knowledge_base():
    _, executor = _update_executor([(KB_A, "Docs"), (KB_B, "Notes")])
    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "x", "knowledge_base": "Elsewhere"},
    )
    assert "not a writable knowledge base" in out


# ---------------------------------------------------------------------------
# search_knowledge provenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_results_are_tagged_with_their_source_document(monkeypatch):
    """Untagged chunks are anonymous — the id is the handoff to the full read."""
    from app.services import vector

    async def _titles(_db, source_ids):
        assert source_ids == {DOC_ID}
        return {DOC_ID: "baseline.md"}

    monkeypatch.setattr(vector, "_load_document_titles", _titles)
    results = [
        SimpleNamespace(content="fragment one", source_type="document", source_id=DOC_ID)
    ]

    out = await vector._render_search_results(None, results)

    assert out.startswith(f"[1] from 'baseline.md' (document id: {DOC_ID})")
    assert "fragment one" in out


@pytest.mark.asyncio
async def test_search_results_without_provenance_keep_the_bare_index(monkeypatch):
    """A non-document embedding (or an unresolvable id) must still render."""
    from app.services import vector

    async def _titles(_db, _source_ids):
        return {}

    monkeypatch.setattr(vector, "_load_document_titles", _titles)
    results = [
        SimpleNamespace(content="orphan", source_type="document", source_id=DOC_ID),
        SimpleNamespace(content="other", source_type="memory", source_id=None),
    ]

    out = await vector._render_search_results(None, results)

    assert out == "[1]\norphan\n\n[2]\nother"


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


class _ApiDb:
    """Session double for the document detail / update routes."""

    def __init__(self, doc, *, count=0):
        self.doc = doc
        self.count = count
        self.statements: list[str] = []
        self.deletes: list[str] = []
        self.added: list = []
        self.commits = 0

    async def execute(self, statement):
        sql = str(statement)
        self.statements.append(sql)
        if sql.lstrip().upper().startswith("DELETE"):
            self.deletes.append(sql)
            return _Result([])
        if "count(" in sql:
            return _Result([self.count])
        return _Result([self.doc] if self.doc is not None else [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass


def _api_client(api_db, kb, monkeypatch):
    async def _get_kb(_db, _user_id, _kb_id, is_admin=False):
        return kb

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(kb_api, "get_user_knowledge_base", _get_kb)
    monkeypatch.setattr(kb_api, "assert_knowledge_base_not_package_origin", _noop)
    monkeypatch.setattr(kb_api, "_assert_kb_manageable", _noop)
    monkeypatch.setattr(kb_api, "log_kb_activity", _noop)
    monkeypatch.setattr(
        kb_api, "enqueue_document_processing", AsyncMock(return_value=True)
    )

    app = FastAPI()
    app.include_router(kb_api.router, prefix="/api/v1/knowledge-bases")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(), is_admin=True, full_name="Admin"
    )

    async def _get_db_override():
        yield api_db

    app.dependency_overrides[get_db] = _get_db_override
    # Document replace re-embeds, so the route now carries the AI gate;
    # this suite asserts the replace semantics behind it.
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    return TestClient(app)


def test_get_document_endpoint_returns_the_full_source(monkeypatch):
    doc = _doc(content="# Full\n\nverbatim body\n")
    api_db = _ApiDb(doc)

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.get(f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "# Full\n\nverbatim body\n"
    assert body["content_length"] == len(doc.content)
    assert body["status"] == "ready"
    assert body["knowledge_base_id"] == KB_A


def test_get_document_endpoint_404s_on_a_malformed_id(monkeypatch):
    api_db = _ApiDb(None)

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.get(f"/api/v1/knowledge-bases/{KB_A}/documents/not-a-uuid")

    assert resp.status_code == 404
    # A malformed id is rejected before any query runs.
    assert api_db.statements == []


def test_documents_count_route_is_not_shadowed(monkeypatch):
    """``/documents/count`` must still resolve despite the bare ``{doc_id}`` route.

    FastAPI matches in declaration order, so a bare
    ``/{kb_id}/documents/{doc_id}`` declared above the literal ``count`` path
    would swallow it and return a 404 "Document not found" instead of a count.
    """
    api_db = _ApiDb(None, count=4)

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.get(f"/api/v1/knowledge-bases/{KB_A}/documents/count")

    assert resp.status_code == 200
    assert resp.json() == {"count": 4}


def test_put_document_endpoint_replaces_and_requeues(monkeypatch):
    doc = _doc()
    kb = _kb(chunk_count=12)
    api_db = _ApiDb(doc)

    with _api_client(api_db, kb, monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "replacement source"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "replacement source"
    assert body["status"] == "pending"
    assert doc.chunk_count == 0
    assert kb.chunk_count == 9
    assert api_db.deletes
    assert kb_api.enqueue_document_processing.await_count == 1


def test_put_document_endpoint_appends(monkeypatch):
    doc = _doc(content="head")
    api_db = _ApiDb(doc)

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "tail", "mode": "append"},
        )

    assert resp.status_code == 200
    assert resp.json()["content"] == "head\n\ntail"


def test_put_document_endpoint_renames(monkeypatch):
    doc = _doc()
    api_db = _ApiDb(doc)

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "x", "filename": "renamed.md"},
        )

    assert resp.status_code == 200
    assert resp.json()["filename"] == "renamed.md"


def test_put_document_endpoint_is_a_no_op_when_nothing_changes(monkeypatch):
    """A PUT that changes nothing must not drop the vectors.

    Re-embedding unchanged, already-indexed content spends provider calls and
    opens an unsearchable window for no gain — the same contract the agent tool
    gives on an identical write.
    """
    doc = _doc(content="unchanged", status="ready")
    kb = _kb(chunk_count=12)
    api_db = _ApiDb(doc)

    with _api_client(api_db, kb, monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "unchanged"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert doc.status == "ready"
    assert doc.chunk_count == 3
    assert kb.chunk_count == 12
    assert api_db.deletes == []
    assert api_db.commits == 0
    assert kb_api.enqueue_document_processing.await_count == 0


def test_put_document_endpoint_rename_alone_does_not_reembed(monkeypatch):
    """Embeddings are built from the content, so a rename cannot invalidate them."""
    doc = _doc(content="unchanged", status="ready")
    kb = _kb(chunk_count=12)
    api_db = _ApiDb(doc)

    with _api_client(api_db, kb, monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "unchanged", "filename": "renamed.md"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "renamed.md"
    # Still indexed and searchable throughout the rename.
    assert body["status"] == "ready"
    assert body["chunk_count"] == 3
    assert kb.chunk_count == 12
    assert api_db.deletes == []
    assert kb_api.enqueue_document_processing.await_count == 0
    # The rename itself is persisted.
    assert api_db.commits == 1


def test_put_document_endpoint_reembeds_unchanged_content_when_not_ready(
    monkeypatch,
):
    """Re-submitting identical content is how a failed embed is retried."""
    doc = _doc(content="unchanged", status="error")
    api_db = _ApiDb(doc)

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "unchanged"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert doc.error_message is None
    assert api_db.deletes
    assert kb_api.enqueue_document_processing.await_count == 1


def test_put_document_endpoint_rejects_an_empty_filename(monkeypatch):
    api_db = _ApiDb(_doc())

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "x", "filename": "   "},
        )

    assert resp.status_code == 400


def test_put_document_endpoint_rejects_nul_bytes(monkeypatch):
    api_db = _ApiDb(_doc())

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": f"a{_NUL}b"},
        )

    assert resp.status_code == 400
    assert "binary" in resp.json()["detail"]


def test_put_document_endpoint_rejects_a_builtin_kb(monkeypatch):
    api_db = _ApiDb(_doc())

    with _api_client(api_db, _kb(is_builtin=True), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "x"},
        )

    assert resp.status_code == 403


def test_put_document_endpoint_rejects_an_invalid_mode(monkeypatch):
    api_db = _ApiDb(_doc())

    with _api_client(api_db, _kb(), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_A}/documents/{DOC_ID}",
            json={"content": "x", "mode": "patch"},
        )

    assert resp.status_code == 422
