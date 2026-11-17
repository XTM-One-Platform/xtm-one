"""Every write to a knowledge base's documents moves its revision.

``KnowledgeBase.revision`` is the evaluation reuse fingerprint's view of a
knowledge base's content: a ``full_loop`` prediction is reused while the
revisions of the agent's knowledge bases are unchanged. Against a real
PostgreSQL, through each path that adds, edits or deletes a document of a
knowledge base that outlives the write - the REST routes, sync ingestion, the
agent tools, the agent builder, a workset's corpus, a chat attachment and the
built-in seed - the revision read back after the write is higher than before.
A write that leaves the content as it was - a document re-submitted as it is,
a sync that finds its source unchanged - does not move it.

The embedding queue is not under test: with no embedding provider configured
on the scratch database, enqueueing is declined before it reaches the queue.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.agents import seed
from app.api.v1 import chat as chat_api
from app.api.v1 import knowledge_bases as kb_api
from app.api.v1.schemas.knowledge_base import (
    AddTextDocumentRequest,
    CrawlURLRequest,
    UpdateKnowledgeDocumentRequest,
)
from app.llm.base import FileRef
from app.models.agent import Agent
from app.models.chat import Conversation
from app.models.knowledge_base import KBSyncSource, KnowledgeBase, KnowledgeDocument
from app.models.user import User
from app.services import kb_helpers, kb_sync
from app.services.builtin_tools import (
    build_agent_builder_tool,
    build_kb_document_update_tool,
    build_kb_store_tool,
)
from app.services.file_storage import file_storage
from app.services.workset import add_item, resolve_workset
from app.tools.base import FileContext, ToolCall
from tests.scratch_database import ALL_TABLES, ScratchDatabase, scratch_database

pytestmark = pytest.mark.integration

_CONTENT = "Rotate the signing keys every 90 days and log each rotation."


@dataclass
class World:
    user: User
    kb: KnowledgeBase
    document: KnowledgeDocument
    synced: KnowledgeDocument
    source: KBSyncSource
    conversation: Conversation


async def _seed(sessions: async_sessionmaker) -> World:
    async with sessions() as db:
        user = User(
            email=f"kbrev-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Owner", role="admin"
        )
        db.add(user)
        await db.flush()
        kb = KnowledgeBase(name=f"kb-{uuid.uuid4().hex[:8]}", user_id=user.id)
        db.add(kb)
        await db.flush()
        document = KnowledgeDocument(
            knowledge_base_id=kb.id,
            filename="policy.md",
            source_type="text",
            content=_CONTENT,
            status="ready",
        )
        synced = KnowledgeDocument(
            knowledge_base_id=kb.id, filename="README.md", source_type="github", content=_CONTENT, status="ready"
        )
        source = KBSyncSource(knowledge_base_id=kb.id, user_id=user.id, source_type="github", config={})
        agent = Agent(
            name="Agent",
            slug=f"a-{uuid.uuid4().hex[:6]}",
            user_id=user.id,
            knowledge_base_ids=[str(kb.id)],
            knowledge_base_permissions={str(kb.id): "write"},
        )
        db.add_all([document, synced, source, agent])
        await db.flush()
        conversation = Conversation(user_id=user.id, agent_id=agent.id, channel="web", title="Chat")
        db.add(conversation)
        kb.document_count = 2
        await db.commit()
    return World(user=user, kb=kb, document=document, synced=synced, source=source, conversation=conversation)


async def _revision(sessions: async_sessionmaker, kb_id: uuid.UUID) -> int:
    async with sessions() as db:
        return (await db.execute(select(KnowledgeBase.revision).where(KnowledgeBase.id == kb_id))).scalar_one()


async def _tool(executor, name: str, **arguments) -> str:
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))
    return result.content


def _writable(world: World) -> list[tuple[str, str]]:
    return [(str(world.kb.id), world.kb.name)]


# ── The writers ──────────────────────────────────────────────────────────────


async def _rest_crawl(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    page = SimpleNamespace(title="Key rotation", text=_CONTENT)
    monkeypatch.setattr("app.services.web_fetch.fetch_and_extract", AsyncMock(return_value=page))
    async with scratch.sessions() as db:
        await kb_api.crawl_url_document(
            kb_id=str(world.kb.id),
            body=CrawlURLRequest(url="https://docs.example.com/keys"),
            user=world.user,
            db=db,
            _ai_gate=None,
        )


async def _rest_add_text(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    async with scratch.sessions() as db:
        await kb_api.add_text_document(
            kb_id=str(world.kb.id),
            body=AddTextDocumentRequest(filename="notes.md", content=_CONTENT),
            user=world.user,
            db=db,
            _ai_gate=None,
        )


async def _rest_upload(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    async with scratch.sessions() as db:
        await kb_api.upload_document(
            kb_id=str(world.kb.id),
            file=UploadFile(file=io.BytesIO(_CONTENT.encode()), filename="upload.md"),
            user=world.user,
            db=db,
            _ai_gate=None,
        )


async def _rest_edit_content(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    async with scratch.sessions() as db:
        await kb_api.update_document(
            kb_id=str(world.kb.id),
            doc_id=str(world.document.id),
            body=UpdateKnowledgeDocumentRequest(content="Rotate the signing keys every 30 days."),
            user=world.user,
            db=db,
            _ai_gate=None,
        )


async def _rest_rename(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    async with scratch.sessions() as db:
        await kb_api.update_document(
            kb_id=str(world.kb.id),
            doc_id=str(world.document.id),
            body=UpdateKnowledgeDocumentRequest(content=_CONTENT, filename="key-rotation.md"),
            user=world.user,
            db=db,
            _ai_gate=None,
        )


async def _rest_delete(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    async with scratch.sessions() as db:
        await kb_api.delete_document(
            kb_id=str(world.kb.id), doc_id=str(world.document.id), user=world.user, db=db, _ai_gate=None
        )


async def _rest_force_resync(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    monkeypatch.setattr(kb_api, "_assert_sync_sources_licensed", AsyncMock())
    monkeypatch.setattr(kb_api, "_spawn_sync_task", lambda coro: coro.close())
    async with scratch.sessions() as db:
        await kb_api.force_resync_source(
            kb_id=str(world.kb.id), source_id=str(world.source.id), user=world.user, db=db, _ai_gate=None
        )


_SITE = "https://docs.example.com"


def _crawl(monkeypatch, pages: dict[str, str | BaseException]) -> None:
    """A site whose start page links to every other page of *pages*.

    Each value is the page's text, or what fetching the page raises.
    """
    links = "".join(f'<a href="{url}">page</a>' for url in pages if url != _SITE)

    async def _fetch(_client, url, **_kwargs):
        page = pages[url.rstrip("/")]
        if isinstance(page, BaseException):
            raise page
        html = links if url.rstrip("/") == _SITE else "<p>page</p>"
        response = httpx.Response(
            200, request=httpx.Request("GET", url), headers={"content-type": "text/html"}, text=html
        )
        return response, False, False

    monkeypatch.setattr(kb_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(kb_sync, "_fetch_with_retry", _fetch)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr("app.services.web_fetch.extract_from_html", lambda _html, url: (pages[url.rstrip("/")], {}))


async def _sync_site(scratch: ScratchDatabase, world: World) -> None:
    async with scratch.sessions() as db:
        kb = await db.get(KnowledgeBase, world.kb.id)
        await kb_sync.sync_website(db, kb, _SITE, max_depth=1)


async def _sync_website(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    _crawl(monkeypatch, {_SITE: _CONTENT})
    await _sync_site(scratch, world)


async def _sync_website_stopped_mid_crawl(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    # The sync job is cancelled after the first page committed and before the
    # final recount: the page's content is stored, so its revision moved.
    _crawl(monkeypatch, {_SITE: _CONTENT, f"{_SITE}/child": asyncio.CancelledError()})
    with pytest.raises(asyncio.CancelledError):
        await _sync_site(scratch, world)


async def _agent_store_text(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    # The provider returns no vector: the document is stored with status
    # ``error``, still a new document of the knowledge base.
    provider = SimpleNamespace(provider_name="test", embed=AsyncMock(return_value=[]))
    monkeypatch.setattr("app.llm.router.get_embedding_provider", AsyncMock(return_value=(provider, "test", None)))
    _, executor = build_kb_store_tool(None, str(world.user.id), _writable(world))
    await _tool(executor, "store_knowledge", title="analysis", content=_CONTENT, replace=False)


async def _agent_store_file(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    data = _CONTENT.encode()
    ref = FileRef(
        file_id=str(uuid.uuid4()),
        filename="rotation.md",
        content_type="text/markdown",
        size=len(data),
        s3_object_name="chat/conv/rotation.md",
        _data=data,
        source="agent_generated",
    )
    _, executor = build_kb_store_tool(None, str(world.user.id), _writable(world))
    executor.file_context = FileContext.from_file_refs([ref])
    await _tool(executor, "store_knowledge", title="", file_id=f"[[FILE:{ref.file_id}]]")


async def _agent_update(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    _, executor = build_kb_document_update_tool(None, str(world.user.id), _writable(world))
    await _tool(executor, "update_knowledge_document", document_id=str(world.document.id), content="30 days.")


async def _builder_add(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    _, executor = build_agent_builder_tool(None, str(world.user.id))
    await _tool(executor, "add_kb_document", knowledge_base_id=str(world.kb.id), title="faq", content=_CONTENT)


async def _builder_delete(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    _, executor = build_agent_builder_tool(None, str(world.user.id))
    await _tool(
        executor, "delete_kb_document", knowledge_base_id=str(world.kb.id), document_id=str(world.document.id)
    )


async def _chat_attachment(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    async def _upload(*, data, filename, content_type, prefix):
        return f"{prefix}/{filename}"

    monkeypatch.setattr(file_storage, "upload", _upload)
    async with scratch.sessions() as db:
        user = await db.get(User, world.user.id)
        await chat_api.upload_file_to_conversation(
            str(world.conversation.id),
            file=UploadFile(file=io.BytesIO(_CONTENT.encode()), filename="timeline.txt"),
            create_message=True,
            user=user,
            db=db,
            _ai_gate=None,
        )


async def _seed_folder(scratch: ScratchDatabase, world: World, monkeypatch, tmp_path) -> None:
    folder = tmp_path / "knowledge_base" / "builtin"
    folder.mkdir(parents=True)
    (folder / "policy.md").write_text("Rotate the signing keys every 30 days.", encoding="utf-8")
    monkeypatch.setattr(seed, "SEED_DATA_DIR", tmp_path)
    async with scratch.sessions() as db:
        kb = await db.get(KnowledgeBase, world.kb.id)
        await seed._seed_documents_from_folder(db, kb, "builtin")
        await db.commit()


async def _seed_new_sources(scratch: ScratchDatabase, world: World, monkeypatch) -> None:
    async with scratch.sessions() as db:
        kb = await db.get(KnowledgeBase, world.kb.id)
        await seed._sync_kb_sources(
            db,
            kb,
            [{"source_type": "github", "config": {"repo": "acme/docs"}, "sync_interval": "daily"}],
        )
        await db.commit()


Writer = Callable[..., Awaitable[None]]

WRITERS: dict[str, Writer] = {
    "rest_crawl": _rest_crawl,
    "rest_add_text": _rest_add_text,
    "rest_upload": _rest_upload,
    "rest_edit_content": _rest_edit_content,
    "rest_rename": _rest_rename,
    "rest_delete": _rest_delete,
    "rest_force_resync": _rest_force_resync,
    "sync_website": _sync_website,
    "sync_website_stopped_mid_crawl": _sync_website_stopped_mid_crawl,
    "agent_store_text": _agent_store_text,
    "agent_store_file": _agent_store_file,
    "agent_update": _agent_update,
    "builder_add": _builder_add,
    "builder_delete": _builder_delete,
    "chat_attachment": _chat_attachment,
    "seed_folder": _seed_folder,
    "seed_new_sources": _seed_new_sources,
}


def _scratch():
    return scratch_database("kbrev", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


@pytest.fixture(autouse=True)
def _no_job_cancellation(monkeypatch):
    # A deletion cancels its documents' queued jobs once it commits; the queue
    # is not under test (``test_document_job_cancel_after_commit_postgres.py``).
    monkeypatch.setattr(kb_helpers, "cancel_document_processing_jobs", AsyncMock(return_value=0))


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", WRITERS)
async def test_a_document_write_moves_the_knowledge_base_revision(monkeypatch, tmp_path, writer):
    write = WRITERS[writer]
    extra = {"tmp_path": tmp_path} if writer == "seed_folder" else {}
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        before = await _revision(scratch.sessions, world.kb.id)

        await write(scratch, world, monkeypatch, **extra)

        assert await _revision(scratch.sessions, world.kb.id) > before


@pytest.mark.asyncio
async def test_re_submitting_a_document_as_it_is_leaves_the_revision_alone():
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        before = await _revision(scratch.sessions, world.kb.id)

        async with scratch.sessions() as db:
            await kb_api.update_document(
                kb_id=str(world.kb.id),
                doc_id=str(world.document.id),
                body=UpdateKnowledgeDocumentRequest(content=_CONTENT),
                user=world.user,
                db=db,
                _ai_gate=None,
            )

        assert await _revision(scratch.sessions, world.kb.id) == before


@pytest.mark.asyncio
async def test_a_sync_moves_the_revision_only_when_the_source_changed(monkeypatch):
    """A scheduled re-sync of an unchanged source keeps reused predictions valid."""
    child = f"{_SITE}/child"
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        _crawl(monkeypatch, {_SITE: _CONTENT, child: "Log each rotation in the key register."})
        await _sync_site(scratch, world)
        synced = await _revision(scratch.sessions, world.kb.id)

        await _sync_site(scratch, world)
        unchanged = await _revision(scratch.sessions, world.kb.id)

        # The child page is gone from the site: the sync deletes its document.
        _crawl(monkeypatch, {_SITE: _CONTENT})
        await _sync_site(scratch, world)
        removed = await _revision(scratch.sessions, world.kb.id)

    assert unchanged == synced
    assert removed > unchanged


def _github(monkeypatch, files: dict[str, str]) -> None:
    """A public GitHub repository holding *files* (path to content)."""

    def _response(url: str, **kwargs) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url), **kwargs)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url: str, **_kwargs):
            if "/git/trees/" in url:
                tree = [{"type": "blob", "path": path, "size": len(text)} for path, text in files.items()]
                return _response(url, json={"tree": tree})
            path = next(p for p in files if url.endswith(f"/{p}"))
            return _response(url, text=files[path], headers={"content-type": "text/plain"})

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setattr(kb_sync.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())


@pytest.mark.asyncio
async def test_a_single_commit_sync_moves_the_revision_only_when_the_repository_changed(monkeypatch):
    """The syncs that commit once, after their recount, move the revision only on a change."""

    async def _sync() -> int:
        async with scratch.sessions() as db:
            kb = await db.get(KnowledgeBase, world.kb.id)
            await kb_sync.sync_github_repo(db, kb, world.user.id, "acme/docs", public=True)
        return await _revision(scratch.sessions, world.kb.id)

    rotation = "# Key rotation\n\nRotate the signing keys every 90 days."
    register = "# Key register\n\nLog each rotation in the key register."
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        _github(monkeypatch, {"rotation.md": rotation, "register.md": register})
        synced = await _sync()
        unchanged = await _sync()

        _github(monkeypatch, {"rotation.md": rotation.replace("90", "30"), "register.md": register})
        edited = await _sync()

        _github(monkeypatch, {"rotation.md": rotation.replace("90", "30")})
        removed = await _sync()

    assert unchanged == synced
    assert edited > unchanged
    assert removed > edited


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revised", "evicted"])
async def test_a_workset_corpus_write_moves_its_knowledge_base_revision(change):
    async with _scratch() as scratch, scratch.sessions() as session:
        user = User(email=f"ws-{uuid.uuid4().hex[:8]}@example.com", full_name="Workset", hashed_password="x")
        session.add(user)
        await session.flush()
        ws = await resolve_workset(session, user, key="reports", scope="global", max_items=1)
        await add_item(session, ws, user, title="R", content="C" * 200, external_ref="r-1")
        await session.commit()
        before = await _revision(scratch.sessions, ws.knowledge_base_id)

        if change == "revised":
            await add_item(session, ws, user, title="R (corrected)", content="D" * 300, external_ref="r-1")
        else:
            await add_item(session, ws, user, title="new", content="L" * 100, external_ref="new")
        await session.commit()

        assert await _revision(scratch.sessions, ws.knowledge_base_id) > before

