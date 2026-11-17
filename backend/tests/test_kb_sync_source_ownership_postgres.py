"""A sync source replaces and deletes only its own documents.

Against a real PostgreSQL: two sync sources of one type in one knowledge base
used to replace each other's documents on every run, because the stale sweep
deleted every document of the TYPE the run had not seen. Each document now
records the source that imported it (``KnowledgeDocument.sync_source_id``) and
``kb_sync.owned_sync_documents`` is what a run may match by filename, sweep, or
purge on a force re-sync. Notion's HTTP calls are faked; the upsert, the sweep
and the force re-sync purge run on the database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import knowledge_bases as kb_api
from app.models.knowledge_base import KBSyncSource, KnowledgeBase, KnowledgeDocument
from app.models.user import User
from app.services import kb_helpers, kb_sync
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_BODY = "A paragraph long enough to be stored as a knowledge document."


@dataclass
class World:
    user: User
    kb: KnowledgeBase
    source_a: KBSyncSource
    source_b: KBSyncSource


def _scratch():
    return scratch_database(
        "kbsyncowner", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True, refuse_other_databases=True
    )


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    """The embedding queue and the job cancellation are other services."""
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr(kb_helpers, "cancel_document_processing_jobs", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="test-token"))


async def _seed(sessions, source_type: str = "notion") -> World:
    async with sessions() as db:
        user = User(
            email=f"kbsync-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Owner", role="admin"
        )
        db.add(user)
        await db.flush()
        kb = KnowledgeBase(name=f"kb-{uuid.uuid4().hex[:8]}", user_id=user.id)
        db.add(kb)
        await db.flush()
        source_a = KBSyncSource(knowledge_base_id=kb.id, user_id=user.id, source_type=source_type, config={})
        source_b = KBSyncSource(knowledge_base_id=kb.id, user_id=user.id, source_type=source_type, config={})
        db.add_all([source_a, source_b])
        await db.commit()
    return World(user=user, kb=kb, source_a=source_a, source_b=source_b)


def _notion_pages(pages: dict[str, str], body: str = _BODY):
    """A fake Notion API serving *pages* (page id -> title), one paragraph each."""

    async def _request(_controller, _client, method, url, **_kwargs):
        req = httpx.Request(method, url)
        for page_id, title in pages.items():
            if url.endswith(f"/pages/{page_id}"):
                return httpx.Response(
                    200,
                    json={
                        "object": "page",
                        "id": page_id,
                        "url": f"https://www.notion.so/{page_id.replace('-', '')}",
                        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
                    },
                    request=req,
                )
            if f"/blocks/{page_id}/children" in url:
                block = {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": f"{title}: {body}"}]}}
                return httpx.Response(200, json={"results": [block], "has_more": False}, request=req)
        raise AssertionError(f"unexpected Notion call: {method} {url}")

    return _request


async def _sync_notion(
    sessions, world: World, source: KBSyncSource, page_id: str, title: str, body: str = _BODY
) -> list[str]:
    with patch.object(kb_sync._NotionRequestController, "request", new=_notion_pages({page_id: title}, body)):
        async with sessions() as db:
            kb = await db.get(KnowledgeBase, world.kb.id)
            return await kb_sync.sync_notion_pages(
                db, kb, str(world.user.id), page_id=page_id, sync_source_id=str(source.id)
            )


async def _documents(sessions, kb_id) -> dict[str, KnowledgeDocument]:
    async with sessions() as db:
        rows = (await db.execute(select(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb_id))).scalars()
        return {str(doc.id): doc for doc in rows}


async def _add_document(sessions, world: World, *, filename: str, owner: uuid.UUID | None, source_type="notion"):
    async with sessions() as db:
        doc = KnowledgeDocument(
            knowledge_base_id=world.kb.id,
            filename=filename,
            source_type=source_type,
            sync_source_id=owner,
            content=f"{filename}: {_BODY}",
            content_hash=uuid.uuid4().hex,
            status="ready",
        )
        db.add(doc)
        await db.commit()
        return str(doc.id)


# ── The case: two Notion sources in one knowledge base ──────────────────────


@pytest.mark.asyncio
async def test_two_notion_sources_keep_each_others_pages_across_syncs():
    page_a, page_b = str(uuid.uuid4()), str(uuid.uuid4())
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        [doc_a] = await _sync_notion(scratch.sessions, world, world.source_a, page_a, "Page A")
        [doc_b] = await _sync_notion(scratch.sessions, world, world.source_b, page_b, "Page B")
        again_a = await _sync_notion(scratch.sessions, world, world.source_a, page_a, "Page A")
        again_b = await _sync_notion(scratch.sessions, world, world.source_b, page_b, "Page B")
        docs = await _documents(scratch.sessions, world.kb.id)
        async with scratch.sessions() as db:
            kb = await db.get(KnowledgeBase, world.kb.id)

    assert again_a == [doc_a]
    assert again_b == [doc_b]
    assert set(docs) == {doc_a, doc_b}
    assert docs[doc_a].sync_source_id == world.source_a.id
    assert docs[doc_b].sync_source_id == world.source_b.id
    assert kb.document_count == 2


@pytest.mark.asyncio
async def test_two_notion_pages_with_one_title_stay_two_documents():
    """The filename fallback never takes another source's same-named document."""
    page_a, page_b = str(uuid.uuid4()), str(uuid.uuid4())
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        [doc_a] = await _sync_notion(scratch.sessions, world, world.source_a, page_a, "Runbook")
        [doc_b] = await _sync_notion(scratch.sessions, world, world.source_b, page_b, "Runbook")
        docs = await _documents(scratch.sessions, world.kb.id)

    assert doc_a != doc_b
    assert set(docs) == {doc_a, doc_b}
    assert docs[doc_a].source_url == f"https://www.notion.so/{page_a.replace('-', '')}"
    assert docs[doc_b].source_url == f"https://www.notion.so/{page_b.replace('-', '')}"


@pytest.mark.asyncio
async def test_a_page_both_sources_reach_is_one_document_kept_by_its_owner():
    page = str(uuid.uuid4())
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        [doc_a] = await _sync_notion(scratch.sessions, world, world.source_a, page, "Shared")
        [doc_b] = await _sync_notion(scratch.sessions, world, world.source_b, page, "Shared")
        docs = await _documents(scratch.sessions, world.kb.id)

    assert doc_a == doc_b
    assert set(docs) == {doc_a}
    assert docs[doc_a].sync_source_id == world.source_a.id


# ── The sweep ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_source_still_sweeps_its_own_stale_documents():
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions, "github")
        kept = await _add_document(scratch.sessions, world, filename="kept.md", owner=world.source_a.id, source_type="github")
        gone = await _add_document(scratch.sessions, world, filename="gone.md", owner=world.source_a.id, source_type="github")
        other = await _add_document(scratch.sessions, world, filename="b.md", owner=world.source_b.id, source_type="github")
        async with scratch.sessions() as db:
            kb = await db.get(KnowledgeBase, world.kb.id)
            deleted = await kb_sync._delete_stale_documents(
                db, kb, "github", {kept}, sync_source_id=str(world.source_a.id)
            )
            await db.commit()
        docs = await _documents(scratch.sessions, world.kb.id)

    assert deleted == 1
    assert set(docs) == {kept, other}
    assert gone not in docs


@pytest.mark.asyncio
async def test_unowned_documents_are_claimed_when_seen_and_swept_when_not():
    """Documents synced before the column existed carry no owner: the first source to see one takes it."""
    page = str(uuid.uuid4())
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            legacy = KnowledgeDocument(
                knowledge_base_id=world.kb.id,
                filename="Legacy.md",
                source_type="notion",
                source_url=f"https://www.notion.so/{page.replace('-', '')}",
                content="# Legacy\n\nLegacy: " + _BODY,
                content_hash=uuid.uuid4().hex,
                status="ready",
            )
            db.add(legacy)
            await db.commit()
        unseen = await _add_document(scratch.sessions, world, filename="Unseen.md", owner=None)
        [claimed] = await _sync_notion(scratch.sessions, world, world.source_a, page, "Legacy")
        docs = await _documents(scratch.sessions, world.kb.id)

    assert claimed == str(legacy.id)
    assert set(docs) == {claimed}
    assert docs[claimed].sync_source_id == world.source_a.id
    assert unseen not in docs


def _notion_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


@pytest.mark.asyncio
async def test_an_unowned_document_of_another_page_is_not_claimed_by_its_title():
    """Before the upgrade only page B's "Runbook" was synced: page A, same title, never takes it.

    Claimed by filename, B's document would take A's content under B's URL, then B would find it by
    that URL and write its content back: the two pages would swap it, re-embedded, on every run.
    A's first run leaves it unclaimed and sweeps it as an unowned document it did not see; B then
    gets a document of its own, and from the second round nothing changes or is re-embedded.
    """
    page_a, page_b = str(uuid.uuid4()), str(uuid.uuid4())
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            legacy_b = KnowledgeDocument(
                knowledge_base_id=world.kb.id,
                filename="Runbook.md",
                source_type="notion",
                source_url=_notion_url(page_b),
                content="# Runbook\n\nRunbook: page B, synced before the upgrade.",
                content_hash=uuid.uuid4().hex,
                status="ready",
            )
            db.add(legacy_b)
            await db.commit()
        rounds = []
        for _ in range(2):
            kb_sync.enqueue_documents.reset_mock()
            [doc_a] = await _sync_notion(scratch.sessions, world, world.source_a, page_a, "Runbook", "page A, " + _BODY)
            [doc_b] = await _sync_notion(scratch.sessions, world, world.source_b, page_b, "Runbook", "page B, " + _BODY)
            rounds.append(([call.args[0] for call in kb_sync.enqueue_documents.await_args_list], doc_a, doc_b))
        docs = await _documents(scratch.sessions, world.kb.id)

    legacy_id = str(legacy_b.id)
    [(first_embeds, doc_a, doc_b), (second_embeds, again_a, again_b)] = rounds
    assert legacy_id not in {doc_a, doc_b}
    assert doc_a != doc_b
    assert (again_a, again_b) == (doc_a, doc_b)
    assert first_embeds == [[doc_a], [doc_b]]
    assert second_embeds == [[], []]
    assert set(docs) == {doc_a, doc_b}
    assert (docs[doc_a].sync_source_id, docs[doc_a].source_url) == (world.source_a.id, _notion_url(page_a))
    assert (docs[doc_b].sync_source_id, docs[doc_b].source_url) == (world.source_b.id, _notion_url(page_b))
    assert docs[doc_a].content == "# Runbook\n\nRunbook: page A, " + _BODY
    assert docs[doc_b].content == "# Runbook\n\nRunbook: page B, " + _BODY


@pytest.mark.asyncio
async def test_an_unowned_document_without_a_url_is_claimed_by_its_title_and_takes_the_url():
    """A document imported with no URL is still claimed by filename, then found by its new URL."""
    page = str(uuid.uuid4())
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        legacy = await _add_document(scratch.sessions, world, filename="Legacy.md", owner=None)
        [claimed] = await _sync_notion(scratch.sessions, world, world.source_a, page, "Legacy")
        [again] = await _sync_notion(scratch.sessions, world, world.source_a, page, "Legacy")
        docs = await _documents(scratch.sessions, world.kb.id)

    assert (claimed, again) == (legacy, legacy)
    assert set(docs) == {legacy}
    assert (docs[legacy].sync_source_id, docs[legacy].source_url) == (world.source_a.id, _notion_url(page))


@pytest.mark.parametrize("owner", ["removed-source", "other-kb-source"])
@pytest.mark.asyncio
async def test_a_document_whose_owner_is_no_source_of_its_knowledge_base_is_unowned(owner):
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        if owner == "removed-source":
            owner_id = uuid.uuid4()
        else:
            async with scratch.sessions() as db:
                other_kb = KnowledgeBase(name=f"other-{uuid.uuid4().hex[:8]}", user_id=world.user.id)
                db.add(other_kb)
                await db.flush()
                foreign = KBSyncSource(knowledge_base_id=other_kb.id, user_id=world.user.id, source_type="notion", config={})
                db.add(foreign)
                await db.commit()
            owner_id = foreign.id
        orphan = await _add_document(scratch.sessions, world, filename="Orphan.md", owner=owner_id)
        owned_by_b = await _add_document(scratch.sessions, world, filename="B.md", owner=world.source_b.id)
        async with scratch.sessions() as db:
            kb = await db.get(KnowledgeBase, world.kb.id)
            deleted = await kb_sync._delete_stale_documents(
                db, kb, "notion", set(), sync_source_id=world.source_a.id
            )
            await db.commit()
        docs = await _documents(scratch.sessions, world.kb.id)

    assert deleted == 1
    assert set(docs) == {owned_by_b}
    assert orphan not in docs


@pytest.mark.asyncio
async def test_an_import_without_a_source_sweeps_only_unowned_documents():
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        unowned = await _add_document(scratch.sessions, world, filename="Unowned.md", owner=None)
        owned = await _add_document(scratch.sessions, world, filename="A.md", owner=world.source_a.id)
        async with scratch.sessions() as db:
            kb = await db.get(KnowledgeBase, world.kb.id)
            deleted = await kb_sync._delete_stale_documents(db, kb, "notion", set())
            await db.commit()
        docs = await _documents(scratch.sessions, world.kb.id)

    assert deleted == 1
    assert set(docs) == {owned}
    assert unowned not in docs


# ── Force re-sync ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_force_resync_purges_only_the_sources_own_and_unowned_documents(monkeypatch):
    monkeypatch.setattr(kb_api, "_assert_sync_sources_licensed", AsyncMock())
    monkeypatch.setattr(kb_api, "_spawn_sync_task", lambda coro: coro.close())
    async with _scratch() as scratch:
        world = await _seed(scratch.sessions)
        mine = await _add_document(scratch.sessions, world, filename="A.md", owner=world.source_a.id)
        unowned = await _add_document(scratch.sessions, world, filename="Legacy.md", owner=None)
        theirs = await _add_document(scratch.sessions, world, filename="B.md", owner=world.source_b.id)
        uploaded = await _add_document(scratch.sessions, world, filename="notes.md", owner=None, source_type="markdown")
        async with scratch.sessions() as db:
            answer = await kb_api.force_resync_source(
                kb_id=str(world.kb.id), source_id=str(world.source_a.id), user=world.user, db=db, _ai_gate=None
            )
        docs = await _documents(scratch.sessions, world.kb.id)

    assert answer["purged_documents"] == 2
    assert set(docs) == {theirs, uploaded}
    assert mine not in docs
    assert unowned not in docs
