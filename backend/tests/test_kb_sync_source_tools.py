"""Knowledge base sync sources from chat (#469).

The agent tools ``get_knowledge_base_sources``, ``update_knowledge_base_source``
and ``resync_knowledge_base`` call the REST handlers of
``/knowledge-bases/{id}/sync-sources``, so the gates are the UI's. The OneDrive
/ SharePoint sync applies the scope they write: several whole folders, and
exclusions never walked nor imported, so a re-sync never brings an excluded
file back. No database: the handlers and the Graph API are doubles.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1 import knowledge_bases as kb_api
from app.api.v1 import mcp_xtm
from app.services import kb_sync
from app.services.builtin_tool_aliases import alias_scoped_functions
from app.services.builtin_tools import kb_sync_sources as tools
from app.services.tool_classification import infer_tool_type

# ---------------------------------------------------------------------------
# The engine: exclusions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "patterns", "excluded"),
    [
        ("01 Direct Sales/XX. Archives", ["XX. Archives"], True),
        ("01 Direct Sales/XX. Archives/old.docx", ["xx. archives"], True),
        ("01 Direct Sales/MSA.docx", ["XX. Archives"], False),
        ("02 Partnership/drafts/a.docx", ["02 Partnership/drafts"], True),
        ("01 Direct Sales/drafts/a.docx", ["02 Partnership/drafts"], False),
        ("01 Direct Sales/MSA (old).pdf", ["*(old)*"], True),
        ("01 Direct Sales/MSA.pdf", [], False),
    ],
)
def test_a_drive_path_is_excluded_by_name_path_or_glob(path, patterns, excluded):
    assert kb_sync.drive_item_excluded(path, patterns) is excluded


def _item(name, folder, *, is_folder=False):
    parent = f"/drive/root:/{folder}" if folder else "/drive/root:"
    item = {"id": f"id-{folder}/{name}", "name": name, "parentReference": {"path": parent}, "size": 10}
    if is_folder:
        item["folder"] = {}
    else:
        item["@microsoft.graph.downloadUrl"] = f"https://dl/{folder}/{name}"
    return item


class _Resp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload or {}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    """The Graph listing of two folders, one holding an archive sub-folder."""

    def __init__(self, listings):
        self.listings = listings
        self.listed: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, headers=None, **_kw):
        if url.startswith("https://dl/"):
            return _Resp(content=b"text")
        self.listed.append(url)
        return _Resp({"value": self.listings.get(url, [])})


class _Nested:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_the_drive_sync_reads_every_folder_and_never_an_excluded_one(monkeypatch):
    base = f"{kb_sync.GRAPH_API}/me/drive"
    listings = {
        f"{base}/root:/01 Direct Sales:/children": [
            _item("MSA.docx", "01 Direct Sales"),
            _item("XX. Archives", "01 Direct Sales", is_folder=True),
        ],
        f"{base}/items/id-01 Direct Sales/XX. Archives/children": [
            _item("old.docx", "01 Direct Sales/XX. Archives"),
        ],
        f"{base}/root:/02 Partnership:/children": [_item("Reseller.docx", "02 Partnership")],
    }
    client = _Client(listings)
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="t"))
    monkeypatch.setattr(kb_sync, "egress_client", lambda **_kw: client)
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=1))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    from app.services import chunking

    monkeypatch.setattr(chunking, "extract_text_from_file_async", AsyncMock(return_value="body"))
    imported: list[str] = []

    async def _upsert(_db, _kb, name, *_a, **_kw):
        imported.append(name)
        return SimpleNamespace(id=uuid.uuid4()), True

    monkeypatch.setattr(kb_sync, "_upsert_document", _upsert)
    db = SimpleNamespace(begin_nested=lambda: _Nested(), commit=AsyncMock())

    ids = await kb_sync.sync_microsoft_drive(
        db,
        SimpleNamespace(id=uuid.uuid4(), name="Legal"),
        user_id=uuid.uuid4(),
        recursive=True,
        sync_source_id=str(uuid.uuid4()),
        folder_paths=["01 Direct Sales", "02 Partnership"],
        exclude=["XX. Archives"],
    )

    assert sorted(imported) == ["01 Direct Sales/MSA.docx", "02 Partnership/Reseller.docx"]
    assert len(ids) == 2
    assert not any("XX. Archives" in url for url in client.listed)
    # The stale pass runs on what was imported, so an archive file synced
    # before the exclusion is removed.
    kb_sync._delete_stale_documents.assert_awaited_once()


def test_a_stored_list_setting_is_read_as_a_list_or_lines():
    assert kb_sync._drive_scope_list(["a", " ", "b"]) == ["a", "b"]
    assert kb_sync._drive_scope_list("a\nb, c") == ["a", "b", "c"]
    assert kb_sync._drive_scope_list(None) == []


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

_KB = str(uuid.uuid4())
_SRC = str(uuid.uuid4())


def _source(**config):
    return SimpleNamespace(
        id=_SRC,
        source_type="microsoft-drive",
        config=config or {"folder_path": "/", "site_id": "legal"},
        sync_interval="daily",
        enabled=True,
        integration_name="Microsoft",
        last_synced_at="2026-11-19T10:00:00+00:00",
        last_sync_status="success",
        last_sync_error=None,
        last_sync_documents=12,
    )


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def rollback(self):
        return None


@pytest.fixture
def handlers(monkeypatch):
    from app.core import database

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(
        tools._builder,
        "_load_caller_user_for_builder",
        AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")),
    )
    return tools.build_kb_sync_source_tools(str(uuid.uuid4()))[1]


@pytest.mark.asyncio
async def test_the_read_names_scope_schedule_and_last_sync(monkeypatch, handlers):
    source = _source(folder_path="/", folder_paths=["A", "B"], exclude=["XX. Archives"], recursive=True)
    monkeypatch.setattr(kb_api, "list_sync_sources", AsyncMock(return_value=[source]))
    out = await handlers[tools.LIST_TOOL](knowledge_base_id=_KB)
    assert "folders: `A`; `B`" in out
    assert "excluded: `XX. Archives`" in out
    assert "status success, 12 document(s)" in out
    assert "folder_path" not in out


@pytest.mark.asyncio
async def test_the_update_writes_folders_and_exclusions_through_the_handler(monkeypatch, handlers):
    monkeypatch.setattr(kb_api, "list_sync_sources", AsyncMock(return_value=[_source()]))
    seen = {}

    async def _update(kb_id, source_id, body, *, user, db):
        seen["config"] = body.config
        return _source(**body.config)

    monkeypatch.setattr(kb_api, "update_sync_source", _update)
    out = await handlers[tools.UPDATE_TOOL](
        knowledge_base_id=_KB,
        source_id=_SRC,
        include_paths=["01 Direct Sales Templates", "02 Partnership Templates"],
        exclude_paths="XX. Archives",
        recursive=True,
    )
    assert seen["config"]["folder_paths"] == ["01 Direct Sales Templates", "02 Partnership Templates"]
    assert seen["config"]["exclude"] == ["XX. Archives"]
    assert seen["config"]["recursive"] is True
    assert seen["config"]["site_id"] == "legal"  # the rest of the config is kept
    assert "Sync source updated" in out


@pytest.mark.asyncio
async def test_a_person_who_cannot_manage_the_knowledge_base_is_refused(monkeypatch, handlers):
    monkeypatch.setattr(kb_api, "list_sync_sources", AsyncMock(return_value=[_source()]))
    monkeypatch.setattr(
        kb_api,
        "update_sync_source",
        AsyncMock(
            side_effect=HTTPException(403, "You don't have permission to edit sync sources on this knowledge base")
        ),
    )
    out = await handlers[tools.UPDATE_TOOL](knowledge_base_id=_KB, source_id=_SRC, exclude_paths=["x"])
    assert out.startswith("Nothing was changed: You don't have permission")

    monkeypatch.setattr(
        kb_api, "trigger_sync_source", AsyncMock(side_effect=HTTPException(403, "You don't have permission"))
    )
    out = await handlers[tools.RESYNC_TOOL](knowledge_base_id=_KB)
    assert "not started, You don't have permission." in out


@pytest.mark.asyncio
async def test_a_knowledge_base_the_caller_cannot_see_is_not_found(monkeypatch, handlers):
    monkeypatch.setattr(
        kb_api, "list_sync_sources", AsyncMock(side_effect=HTTPException(404, "Knowledge base not found"))
    )
    assert await handlers[tools.LIST_TOOL](knowledge_base_id=_KB) == "Knowledge base not found."


@pytest.mark.asyncio
async def test_a_full_resync_goes_through_force_resync(monkeypatch, handlers):
    monkeypatch.setattr(kb_api, "list_sync_sources", AsyncMock(return_value=[_source()]))
    force = AsyncMock(return_value={"status": "syncing", "purged_documents": 7})
    trigger = AsyncMock()
    monkeypatch.setattr(kb_api, "force_resync_source", force)
    monkeypatch.setattr(kb_api, "trigger_sync_source", trigger)
    out = await handlers[tools.RESYNC_TOOL](knowledge_base_id=_KB, full=True)
    assert force.await_count == 1 and trigger.await_count == 0
    assert "7 document(s) purged first" in out
    await handlers[tools.RESYNC_TOOL](knowledge_base_id=_KB, source_id=_SRC)
    assert trigger.await_count == 1


@pytest.mark.asyncio
async def test_a_source_without_folders_is_not_scoped(monkeypatch, handlers):
    notion = _source()
    notion.source_type = "notion"
    monkeypatch.setattr(kb_api, "list_sync_sources", AsyncMock(return_value=[notion]))
    out = await handlers[tools.UPDATE_TOOL](knowledge_base_id=_KB, source_id=_SRC, include_paths=["a"])
    assert "has no folders or exclusions to set" in out


@pytest.mark.asyncio
async def test_an_anonymous_caller_reads_and_writes_nothing():
    _, anonymous = tools.build_kb_sync_source_tools("")
    for name, args in {
        tools.LIST_TOOL: {"knowledge_base_id": _KB},
        tools.UPDATE_TOOL: {"knowledge_base_id": _KB, "source_id": _SRC, "recursive": True},
        tools.RESYNC_TOOL: {"knowledge_base_id": _KB},
    }.items():
        assert "not available for anonymous users" in await anonymous[name](**args), name


def test_the_read_is_a_read_and_the_writes_are_not():
    assert infer_tool_type(tools.LIST_TOOL) == "read"
    assert infer_tool_type(tools.UPDATE_TOOL) == "write"
    assert infer_tool_type(tools.RESYNC_TOOL) == "write"
    assert tools.LIST_TOOL in mcp_xtm.XTM_MCP_READ_TOOLS
    assert not ({tools.UPDATE_TOOL, tools.RESYNC_TOOL} & mcp_xtm.XTM_MCP_READ_TOOLS)


def test_the_agent_builder_offers_and_registers_the_tools():
    from app.services.builtin_tools import build_agent_builder_tool

    assert set(alias_scoped_functions("agent_builder")) >= tools.KB_SYNC_SOURCE_TOOL_NAMES
    definitions, executor = build_agent_builder_tool(None, "")
    assert {d.name for d in definitions} >= tools.KB_SYNC_SOURCE_TOOL_NAMES
    assert set(executor.tool_names) >= tools.KB_SYNC_SOURCE_TOOL_NAMES
