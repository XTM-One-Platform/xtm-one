import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

import app.api.v1.custom_tools as custom_tools_mod
import app.api.v1.helpers as api_helpers
import app.api.v1.knowledge_bases as kb_mod
from app.api.v1 import platform_settings_helpers
from app.api.v1.schemas.custom_tool import CustomToolUpdate
from app.api.v1.schemas.knowledge_base import CreateSyncSourceRequest


class _Rows:
    """What ``db.execute`` returns, in the shapes these routes read."""

    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._rows[0]


class _ScriptedDB:
    """Answers each ``execute`` with the next scripted result, in order."""

    def __init__(self, *results):
        self._results = list(results)
        self.statements: list = []
        self.commit = AsyncMock()

    async def execute(self, stmt, *_args, **_kwargs):
        self.statements.append(stmt)
        return self._results.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_gate", "assert_gate", "subject"),
    [
        pytest.param(
            api_helpers.package_origin_custom_tool_error,
            api_helpers.assert_custom_tool_not_package_origin,
            "Custom tools",
            id="custom_tool",
        ),
        pytest.param(
            api_helpers.package_origin_knowledge_base_error,
            api_helpers.assert_knowledge_base_not_package_origin,
            "Knowledge bases",
            id="knowledge_base",
        ),
    ],
)
async def test_the_shared_gate_refuses_only_a_package_installed_entity(
    error_gate, assert_gate, subject
):
    """One gate for every write surface: the string the agent tools
    return and the 403 the REST routes raise are the same answer."""
    entity_id = uuid.uuid4()

    assert await error_gate(_ScriptedDB(_Rows([entity_id])), entity_id, action="deleted") == (
        f"{subject} imported from skill packages cannot be deleted"
    )
    with pytest.raises(HTTPException) as refused:
        await assert_gate(_ScriptedDB(_Rows([entity_id])), entity_id)
    assert refused.value.status_code == 403
    assert refused.value.detail == f"{subject} imported from skill packages cannot be modified"

    assert await error_gate(_ScriptedDB(_Rows([])), entity_id) is None
    await assert_gate(_ScriptedDB(_Rows([])), entity_id)


@pytest.mark.asyncio
async def test_create_sync_source_blocks_package_origin_kb(monkeypatch):
    kb = SimpleNamespace(id=uuid.uuid4(), is_builtin=False, is_company_managed=False)
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()

    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", AsyncMock(return_value=kb))
    monkeypatch.setattr(
        api_helpers, "is_package_origin_knowledge_base", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(kb_mod, "require_admin_for_company_managed", MagicMock())

    body = CreateSyncSourceRequest(
        source_type="website",
        config={"url": "https://example.com"},
        sync_interval="manual",
        integration_id=None,
    )

    with pytest.raises(HTTPException, match="imported from skill packages cannot be modified"):
        await kb_mod.create_sync_source(kb_id=str(kb.id), body=body, user=user, db=db)


@pytest.mark.asyncio
async def test_update_custom_tool_blocks_package_origin(monkeypatch):
    tool = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    request = MagicMock()

    monkeypatch.setattr(custom_tools_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        custom_tools_mod,
        "get_manageable_entity",
        AsyncMock(return_value=tool),
    )
    monkeypatch.setattr(
        api_helpers, "is_package_origin_custom_tool", AsyncMock(return_value=True)
    )

    with pytest.raises(HTTPException, match="imported from skill packages cannot be modified"):
        await custom_tools_mod.update_custom_tool(
            tool_id=str(tool.id),
            body=CustomToolUpdate(),
            request=request,
            user=user,
            db=db,
        )


@pytest.mark.asyncio
async def test_delete_custom_tool_blocks_package_origin(monkeypatch):
    tool = SimpleNamespace(id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    db = AsyncMock()
    request = MagicMock()

    monkeypatch.setattr(
        custom_tools_mod,
        "get_manageable_entity",
        AsyncMock(return_value=tool),
    )
    monkeypatch.setattr(
        api_helpers, "is_package_origin_custom_tool", AsyncMock(return_value=True)
    )

    with pytest.raises(HTTPException, match="imported from skill packages cannot be deleted"):
        await custom_tools_mod.delete_custom_tool(
            tool_id=str(tool.id),
            request=request,
            user=user,
            db=db,
        )


def _global_doc(knowledge_base_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=knowledge_base_id,
        filename="a.md",
        source_type="text",
        source_url=None,
        status="error",
        chunk_count=0,
        error_message="boom",
        created_at=datetime.now(UTC),
    )


async def _global_documents(db: _ScriptedDB):
    return await kb_mod.list_global_documents(
        limit=50,
        offset=0,
        status=None,
        kb_name=None,
        kb_ids=None,
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=False),
        db=db,
    )


@pytest.mark.asyncio
async def test_global_documents_say_which_rows_a_skill_package_installed(monkeypatch):
    """The documents page offers a per-row Reprocess the route refuses on a
    package-installed KB, so every row says whether its KB is one - with ONE
    origin lookup for the whole page."""
    monkeypatch.setattr(kb_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    package_kb, plain_kb = uuid.uuid4(), uuid.uuid4()
    rows = [
        (_global_doc(package_kb), "From a package"),
        (_global_doc(package_kb), "From a package"),
        (_global_doc(plain_kb), "Mine"),
    ]
    db = _ScriptedDB(_Rows([len(rows)]), _Rows(rows), _Rows([package_kb]))

    page = await _global_documents(db)

    assert [
        (item.knowledge_base_id, item.knowledge_base_is_package_origin)
        for item in page.items
    ] == [(str(package_kb), True), (str(package_kb), True), (str(plain_kb), False)]
    assert len(db.statements) == 3  # count, page, one origin lookup

    empty = _ScriptedDB(_Rows([0]), _Rows([]))
    assert (await _global_documents(empty)).items == []
    assert len(empty.statements) == 2  # no lookup for an empty page


def _stub_reembed_all(monkeypatch) -> AsyncMock:
    """Isolate ``reprocess_all_stale`` from settings, groups and the queue."""
    monkeypatch.setattr(
        platform_settings_helpers,
        "get_platform_settings",
        AsyncMock(
            return_value=SimpleNamespace(embedding_model="model", embedding_dimensions=768)
        ),
    )
    monkeypatch.setattr(kb_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    enqueued = AsyncMock()
    monkeypatch.setattr(kb_mod, "enqueue_documents", enqueued)
    monkeypatch.setattr(kb_mod, "enqueue_reembedding", enqueued)
    return enqueued


async def _reembed_all(db: _ScriptedDB) -> dict:
    return await kb_mod.reprocess_all_stale(
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=True), db=db, _ai_gate=None
    )


@pytest.mark.asyncio
async def test_reembed_all_skips_package_installed_knowledge_bases(monkeypatch):
    """The dashboard's "Re-embed all" leaves a package-installed KB to its
    package: only the other stale KBs are counted, reset and re-queued."""
    enqueued = _stub_reembed_all(monkeypatch)
    package_kb, plain_kb = uuid.uuid4(), uuid.uuid4()
    plain_doc = SimpleNamespace(
        id=uuid.uuid4(),
        status="ready",
        error_message=None,
        embedding_retry_count=2,
        embedding_next_retry_at=None,
    )
    db = _ScriptedDB(
        _Rows([SimpleNamespace(id=package_kb), SimpleNamespace(id=plain_kb)]),
        _Rows([(package_kb,), (plain_kb,)]),  # the KBs holding stale tuples
        _Rows([package_kb]),  # the origin lookup
        _Rows([plain_doc]),  # the documents of the re-embeddable KBs
        _Rows([]),  # the purge of their stale embeddings
    )

    result = await _reembed_all(db)

    assert (result["kbs_affected"], result["requeued"], result["kbs_skipped"]) == (1, 1, 1)
    assert "skill package" in result["message"]
    documents_query = repr(db.statements[3].compile(dialect=postgresql.dialect()).params)
    assert str(plain_kb) in documents_query
    assert str(package_kb) not in documents_query
    assert (plain_doc.status, plain_doc.embedding_retry_count) == ("pending", 0)
    enqueued.assert_awaited_once_with([str(plain_doc.id)])


@pytest.mark.asyncio
async def test_reembed_all_never_calls_stale_package_installed_kbs_up_to_date(monkeypatch):
    """Stale embeddings only in package-installed KBs: nothing is re-embedded,
    and the answer says why instead of reporting a false "up to date"."""
    enqueued = _stub_reembed_all(monkeypatch)
    package_kb = uuid.uuid4()
    db = _ScriptedDB(
        _Rows([SimpleNamespace(id=package_kb)]),
        _Rows([(package_kb,)]),  # the KB holding stale tuples
        _Rows([package_kb]),  # the origin lookup
    )

    result = await _reembed_all(db)

    assert (result["kbs_affected"], result["requeued"], result["kbs_skipped"]) == (0, 0, 1)
    assert "up to date" not in result["message"]
    assert "skill package" in result["message"]
    enqueued.assert_not_awaited()

    fresh = _ScriptedDB(_Rows([SimpleNamespace(id=package_kb)]), _Rows([]))
    up_to_date = await _reembed_all(fresh)
    assert (up_to_date["kbs_skipped"], up_to_date["message"]) == (
        0,
        "All knowledge bases are up to date.",
    )
    assert len(fresh.statements) == 2  # no origin lookup without stale KBs
