"""Read-only knowledge bases and the oversized-document work-item offload.

Two guards added on top of the full-source-document tools:

1. **Read-only knowledge bases** — ``KnowledgeBase.is_read_only`` marks a
   collection as ground truth. It stays fully searchable and readable; what is
   frozen is its content. The lock lives on the KB (not on the agent) precisely
   because per-agent write grants are granted from the agent side, so a lock that
   could be granted away from the other end would be no lock at all. Enforcement
   is two-layered: read-only KBs are stripped from every agent's WRITE scope
   before the write tools are built, and the write tools refuse them anyway if a
   stale scope reaches them. Configured import sources and re-embedding stay
   allowed on purpose — a KB whose authority is an external system of record must
   keep refreshing.

2. **Oversized documents** — a document above
   ``_DOC_CONTEXT_LIMIT_CHARS`` is never injected into the context. It is
   registered as a durable work item instead (idempotent per document version),
   because paging a multi-megabyte document would consume the whole context
   window and still not leave the agent holding it. An explicit
   ``offset``/``max_chars`` window is always served — that request is bounded and
   deliberate.
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
    build_kb_store_tool,
)
from app.services.builtin_tools import kb as kb_tools
from app.tools.base import ToolCall

KB_ID = str(uuid.uuid4())
DOC_ID = str(uuid.uuid4())
AGENT_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Routes statements by target table (see test_kb_full_document_access)."""

    def __init__(self, *, doc_results=(), kb=None):
        self.doc_results = [list(r) for r in doc_results]
        self.kb = kb
        self.statements: list[str] = []
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
        if sql.lstrip().upper().startswith("DELETE"):
            self.deletes.append(sql)
            return _Result([])
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


def _kb(*, is_read_only=False, name="Policies", chunk_count=9):
    return SimpleNamespace(
        id=uuid.UUID(KB_ID),
        name=name,
        is_builtin=False,
        is_read_only=is_read_only,
        is_company_managed=False,
        user_id=uuid.UUID(USER_ID),
        chunk_count=chunk_count,
        chunk_size=400,
        chunk_overlap=40,
        document_count=3,
        embedding_model=None,
    )


def _doc(*, content="baseline", status="ready", content_hash="hash-abc"):
    now = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    return SimpleNamespace(
        id=uuid.UUID(DOC_ID),
        knowledge_base_id=uuid.UUID(KB_ID),
        filename="policy.md",
        source_type="markdown",
        source_url=None,
        status=status,
        chunk_count=3,
        error_message=None,
        content=content,
        content_hash=content_hash,
        extra_metadata=None,
        embedding_retry_count=0,
        embedding_next_retry_at=None,
        created_at=now,
        updated_at=now,
    )


async def _call(executor, name, arguments):
    out = await executor.execute_tool(
        ToolCall(id="call-1", name=name, arguments=arguments)
    )
    return out.content


def _patch_session(monkeypatch, session):
    monkeypatch.setattr("app.core.database.async_session", lambda: session)


# ---------------------------------------------------------------------------
# Read-only: the write tools refuse a locked knowledge base
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_document_refuses_a_read_only_kb(monkeypatch):
    """Defence in depth: a stale write scope must still not mutate ground truth."""
    monkeypatch.setattr(
        "app.services.kb_helpers.enqueue_document_processing",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.kb_helpers.log_kb_activity", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.api.v1.helpers.is_package_origin_knowledge_base",
        AsyncMock(return_value=False),
    )
    doc = _doc()
    session = _FakeSession(doc_results=[[doc]], kb=_kb(is_read_only=True))
    _patch_session(monkeypatch, session)
    result = build_kb_document_update_tool(None, USER_ID, [(KB_ID, "Policies")])
    assert result is not None
    _, executor = result

    out = await _call(
        executor,
        "update_knowledge_document",
        {"document_id": DOC_ID, "content": "rewritten"},
    )

    assert "READ-ONLY" in out
    assert "ground truth" in out
    # Nothing was touched: content intact, no vectors dropped, no commit.
    assert doc.content == "baseline"
    assert not session.deletes
    assert session.commits == 0


@pytest.mark.asyncio
async def test_store_knowledge_refuses_a_read_only_kb(monkeypatch):
    """The refusal happens before any embedding call is spent."""
    kb = _kb(is_read_only=True)
    session = _FakeSession(kb=kb)
    embed = AsyncMock(return_value=[0.1, 0.2])
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider",
        AsyncMock(
            return_value=(
                SimpleNamespace(provider_name="test", embed=embed),
                "test-model",
                "pid-1",
            )
        ),
    )
    result = build_kb_store_tool(None, USER_ID, [(KB_ID, "Policies")])
    assert result is not None
    _, executor = result

    out = await _call(
        executor, "store_knowledge", {"title": "new.md", "content": "hello world"}
    )

    assert "READ-ONLY" in out
    embed.assert_not_awaited()
    assert session.commits == 0


@pytest.mark.asyncio
async def test_store_knowledge_refuses_a_built_in_kb(monkeypatch):
    """The shared content-write gate: a built-in KB is re-synced from its seed,
    so an agent document there could be purged - refused like on every other
    write surface, before any embedding call."""
    kb = _kb()
    kb.is_builtin = True
    session = _FakeSession(kb=kb)
    embed = AsyncMock(return_value=[0.1, 0.2])
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider",
        AsyncMock(
            return_value=(
                SimpleNamespace(provider_name="test", embed=embed),
                "test-model",
                "pid-1",
            )
        ),
    )
    result = build_kb_store_tool(None, USER_ID, [(KB_ID, "Policies")])
    assert result is not None
    _, executor = result

    out = await _call(
        executor, "store_knowledge", {"title": "new.md", "content": "hello world"}
    )

    assert "is a built-in knowledge base" in out
    embed.assert_not_awaited()
    assert session.commits == 0


@pytest.mark.asyncio
async def test_read_only_refusal_tells_the_agent_not_to_retry():
    """A model that reads the refusal as transient will loop on it."""
    message = kb_tools._read_only_kb_error("Policies")
    assert "not a transient failure" in message
    assert "do not attempt another write" in message.lower()


@pytest.mark.asyncio
async def test_reads_still_work_on_a_read_only_kb(monkeypatch):
    """Read-only freezes content, NOT access — the collection stays readable."""
    session = _FakeSession(
        doc_results=[[_doc(content="the authoritative text")]],
        kb=_kb(is_read_only=True),
    )
    _patch_session(monkeypatch, session)
    result = build_kb_document_read_tools(None, USER_ID, [(KB_ID, "Policies")])
    assert result is not None
    _, executor = result

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    assert "the authoritative text" in out
    assert "READ-ONLY" not in out


# ---------------------------------------------------------------------------
# Read-only: locked KBs are stripped from the write scope up front
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_scope_loader_selects_only_locked_kbs():
    """The scope filter: the KB rows, then one origin lookup (the package and
    built-in locks are covered in test_kb_write_scope_content_lock)."""
    locked = _kb(is_read_only=True)
    open_kb = _kb()
    open_kb.id = uuid.uuid4()
    captured: list[str] = []

    class _Db:
        async def execute(self, statement):
            captured.append(str(statement))
            if "agent_skill_package_origin" in captured[-1]:
                return _Result([])
            return _Result([locked, open_kb])

    found = await kb_tools.load_write_locked_kb_ids(_Db(), [KB_ID, str(open_kb.id)])

    assert found == {KB_ID}
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_write_scope_loader_short_circuits_without_ids():
    """No ids, no query — the common case must not hit the DB."""

    class _Db:
        async def execute(self, _statement):  # pragma: no cover - must not run
            raise AssertionError("no query expected")

    assert await kb_tools.load_write_locked_kb_ids(_Db(), []) == set()
    assert await kb_tools.load_write_locked_kb_ids(_Db(), ["not-a-uuid"]) == set()


# ---------------------------------------------------------------------------
# Oversized documents -> work item
# ---------------------------------------------------------------------------


@pytest.fixture
def _oversized(monkeypatch):
    """A document just over the context limit, with the work-item calls stubbed."""
    monkeypatch.setattr(kb_tools, "_DOC_CONTEXT_LIMIT_CHARS", 500)
    monkeypatch.setattr(kb_tools, "_DOC_WINDOW_DEFAULT_CHARS", 100)
    doc = _doc(content="y" * 900)
    session = _FakeSession(doc_results=[[doc]], kb=_kb())
    _patch_session(monkeypatch, session)

    created: list[dict] = []

    async def _create_work_item(**kwargs):
        created.append(kwargs)
        return {
            "id": "wi-1",
            "title": kwargs["title"],
            "status": "open",
            "correlation_key": kwargs.get("correlation_key"),
        }

    listed: list[dict] = []

    async def _list_work_items(**kwargs):
        listed.append(kwargs)
        return []

    monkeypatch.setattr("app.services.work_item.create_work_item", _create_work_item)
    monkeypatch.setattr("app.services.work_item.list_work_items", _list_work_items)
    return SimpleNamespace(
        doc=doc, session=session, created=created, listed=listed
    )


def _read_tools():
    result = build_kb_document_read_tools(
        None,
        USER_ID,
        [(KB_ID, "Policies")],
        agent_id=AGENT_ID,
        is_company_managed=False,
    )
    assert result is not None
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("company_managed_agent", "is_company_managed"),
    [
        (True, True),  # a company-managed agent's visible run or chat turn
        (True, False),  # a company-managed agent's hidden per-user run
        (False, False),  # a personal agent
    ],
)
async def test_the_offload_item_is_written_as_the_agent_itself(
    _oversized, company_managed_agent, is_company_managed
):
    """The item's writer is the acting agent's own tier, never the tier the
    new item is asked to have."""
    _, executor = build_kb_document_read_tools(
        None,
        USER_ID,
        [(KB_ID, "Policies")],
        agent_id=AGENT_ID,
        is_company_managed=is_company_managed,
        company_managed_agent=company_managed_agent,
    )

    await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    item = _oversized.created[0]
    assert item["is_company_managed"] is is_company_managed
    assert item["writer"].may_write is company_managed_agent


@pytest.mark.asyncio
async def test_oversized_document_is_registered_not_injected(_oversized):
    _, executor = _read_tools()

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    # The content itself never reaches the context.
    assert "y" * 200 not in out
    assert "NOT LOADED" in out
    assert "work item" in out
    assert "wi-1" in out
    # And the agent is pointed at the right alternative.
    assert "search_knowledge" in out

    assert len(_oversized.created) == 1
    item = _oversized.created[0]
    assert item["owner_agent_id"] == AGENT_ID
    assert item["created_by_agent_id"] == AGENT_ID
    assert item["user_id"] == USER_ID
    payload = item["payload"]
    assert payload["reason"] == "oversized_knowledge_document"
    assert payload["document_id"] == DOC_ID
    assert payload["content_length"] == 900
    assert payload["knowledge_base_id"] == KB_ID


@pytest.mark.asyncio
async def test_oversized_offload_is_idempotent_per_document_version(_oversized):
    """Re-reading must converge on one item, not spawn one per attempt."""
    _, executor = _read_tools()

    await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})
    key = _oversized.created[0]["correlation_key"]

    assert DOC_ID in key
    # The version component keeps a genuinely new revision from reusing the old
    # item, which described different text.
    assert "hash-abc"[:16] in key
    # The lookup that enforces idempotency queries that exact key, open only.
    assert _oversized.listed[0]["correlation_key"] == key
    assert _oversized.listed[0]["open_only"] is True


@pytest.mark.asyncio
async def test_oversized_offload_reuses_an_existing_open_item(monkeypatch):
    monkeypatch.setattr(kb_tools, "_DOC_CONTEXT_LIMIT_CHARS", 500)
    session = _FakeSession(doc_results=[[_doc(content="y" * 900)]], kb=_kb())
    _patch_session(monkeypatch, session)

    async def _create_work_item(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("a second work item must not be created")

    async def _list_work_items(**_kwargs):
        return [
            {
                "id": "wi-existing",
                "title": "Process oversized knowledge document: policy.md",
                "status": "in_progress",
                "correlation_key": "kb-doc-x",
            }
        ]

    monkeypatch.setattr("app.services.work_item.create_work_item", _create_work_item)
    monkeypatch.setattr("app.services.work_item.list_work_items", _list_work_items)
    _, executor = _read_tools()

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    assert "already tracked by" in out
    assert "wi-existing" in out


@pytest.mark.asyncio
async def test_explicit_window_is_served_even_when_oversized(_oversized):
    """A bounded, deliberate slice request must never be redirected."""
    _, executor = _read_tools()

    out = await _call(
        executor,
        "get_knowledge_document",
        {"document_id": DOC_ID, "offset": 10, "max_chars": 20},
    )

    assert "NOT LOADED" not in out
    assert "characters 10-30 of 900" in out
    assert _oversized.created == []


@pytest.mark.asyncio
async def test_explicit_max_chars_alone_is_served_when_oversized(_oversized):
    """``max_chars`` with no offset is still an explicit bounded request."""
    _, executor = _read_tools()

    out = await _call(
        executor,
        "get_knowledge_document",
        {"document_id": DOC_ID, "max_chars": 50},
    )

    assert "NOT LOADED" not in out
    assert "characters 0-50 of 900" in out
    assert _oversized.created == []


@pytest.mark.asyncio
async def test_document_under_the_limit_is_returned_whole(monkeypatch):
    monkeypatch.setattr(kb_tools, "_DOC_CONTEXT_LIMIT_CHARS", 500)
    body = "z" * 400
    session = _FakeSession(doc_results=[[_doc(content=body)]], kb=_kb())
    _patch_session(monkeypatch, session)

    async def _fail(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("no work item expected below the limit")

    monkeypatch.setattr("app.services.work_item.create_work_item", _fail)
    monkeypatch.setattr("app.services.work_item.list_work_items", _fail)
    _, executor = _read_tools()

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    assert "Source (complete, 400 characters)" in out
    assert body in out


@pytest.mark.asyncio
async def test_oversized_falls_back_to_a_warned_window_without_an_identity(
    monkeypatch,
):
    """An anonymous, non-company-managed caller cannot own a work item.

    Degrading to the windowed read beats failing the call, but the result must
    say plainly that this is one slice of something far larger.
    """
    monkeypatch.setattr(kb_tools, "_DOC_CONTEXT_LIMIT_CHARS", 500)
    monkeypatch.setattr(kb_tools, "_DOC_WINDOW_DEFAULT_CHARS", 100)
    session = _FakeSession(doc_results=[[_doc(content="y" * 900)]], kb=_kb())
    _patch_session(monkeypatch, session)

    async def _fail(**_kwargs):  # pragma: no cover - must not run
        raise AssertionError("create_work_item must not be attempted")

    monkeypatch.setattr("app.services.work_item.create_work_item", _fail)
    monkeypatch.setattr("app.services.work_item.list_work_items", _fail)

    result = build_kb_document_read_tools(
        None, "anonymous", [(KB_ID, "Policies")], agent_id=AGENT_ID
    )
    assert result is not None
    _, executor = result

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    assert "WARNING" in out
    assert "could not be registered as a work item" in out
    assert "characters 0-100 of 900" in out


@pytest.mark.asyncio
async def test_oversized_falls_back_when_the_work_item_write_fails(monkeypatch):
    """A work-queue failure must not make a readable document unreadable."""
    monkeypatch.setattr(kb_tools, "_DOC_CONTEXT_LIMIT_CHARS", 500)
    monkeypatch.setattr(kb_tools, "_DOC_WINDOW_DEFAULT_CHARS", 100)
    session = _FakeSession(doc_results=[[_doc(content="y" * 900)]], kb=_kb())
    _patch_session(monkeypatch, session)

    async def _boom(**_kwargs):
        raise RuntimeError("work queue unavailable")

    monkeypatch.setattr("app.services.work_item.create_work_item", _boom)
    monkeypatch.setattr("app.services.work_item.list_work_items", _boom)
    _, executor = _read_tools()

    out = await _call(executor, "get_knowledge_document", {"document_id": DOC_ID})

    assert "WARNING" in out
    assert "characters 0-100 of 900" in out


# ---------------------------------------------------------------------------
# REST: read-only blocks content mutation but not reads
# ---------------------------------------------------------------------------


class _ApiDb:
    def __init__(self, doc, *, count=0):
        self.doc = doc
        self.count = count
        self.statements: list[str] = []
        self.deletes: list[str] = []
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

    def add(self, _obj):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass

    async def delete(self, _obj):
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
    # Reprocessing enqueues on the SAQ queue: a unit test has no Redis to hold it.
    monkeypatch.setattr(kb_api, "enqueue_documents", AsyncMock(return_value=None))
    monkeypatch.setattr(kb_api, "enqueue_reembedding", AsyncMock(return_value=None))

    app = FastAPI()
    app.include_router(kb_api.router, prefix="/api/v1/knowledge-bases")
    app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
        id=uuid.UUID(USER_ID), is_admin=True, full_name="Admin"
    )

    async def _get_db_override():
        yield api_db

    app.dependency_overrides[get_db] = _get_db_override
    # Upload and reprocess enqueue embedding work, so both now carry the AI
    # gate; this suite asserts the read-only rules behind it.
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("put", "/documents/{doc}", {"content": "x"}),
        ("post", "/documents/text", {"filename": "f.md", "content": "x"}),
        ("delete", "/documents/{doc}", None),
    ],
)
def test_read_only_kb_rejects_content_mutation(monkeypatch, method, path, payload):
    api_db = _ApiDb(_doc())
    url = f"/api/v1/knowledge-bases/{KB_ID}" + path.format(doc=DOC_ID)

    with _api_client(api_db, _kb(is_read_only=True), monkeypatch) as client:
        resp = getattr(client, method)(url, **({"json": payload} if payload else {}))

    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"]
    assert api_db.deletes == []
    assert api_db.commits == 0


def test_read_only_kb_rejects_deletion_of_the_collection(monkeypatch):
    """Ground truth must not be destroyable without unlocking it first."""
    api_db = _ApiDb(None)

    with _api_client(api_db, _kb(is_read_only=True), monkeypatch) as client:
        resp = client.delete(f"/api/v1/knowledge-bases/{KB_ID}")

    assert resp.status_code == 403
    assert "read-only" in resp.json()["detail"]


def test_read_only_kb_still_serves_the_document_read(monkeypatch):
    """The lock is on content, not on access."""
    api_db = _ApiDb(_doc(content="authoritative"))

    with _api_client(api_db, _kb(is_read_only=True), monkeypatch) as client:
        resp = client.get(f"/api/v1/knowledge-bases/{KB_ID}/documents/{DOC_ID}")

    assert resp.status_code == 200
    assert resp.json()["content"] == "authoritative"


def test_read_only_kb_still_allows_reprocessing(monkeypatch):
    """Re-embedding rebuilds the index from unchanged source — not a mutation."""
    api_db = _ApiDb(_doc())

    with _api_client(api_db, _kb(is_read_only=True), monkeypatch) as client:
        resp = client.post(
            f"/api/v1/knowledge-bases/{KB_ID}/documents/{DOC_ID}/reprocess"
        )

    assert resp.status_code == 200
    # Re-embedding existing content, attributed to no one.
    kb_api.enqueue_reembedding.assert_awaited_once_with([DOC_ID])


def test_unlocked_kb_still_accepts_content_mutation(monkeypatch):
    """The guard must be inert when the flag is off."""
    doc = _doc()
    api_db = _ApiDb(doc)

    with _api_client(api_db, _kb(is_read_only=False), monkeypatch) as client:
        resp = client.put(
            f"/api/v1/knowledge-bases/{KB_ID}/documents/{DOC_ID}",
            json={"content": "rewritten"},
        )

    assert resp.status_code == 200
    assert resp.json()["content"] == "rewritten"
