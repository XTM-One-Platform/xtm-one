"""Every deletion of knowledge documents cancels their queued jobs once it commits.

Against a real PostgreSQL, through each path that deletes documents and cancels
their jobs: the REST knowledge-base delete, document delete and force re-sync,
the agent-builder ``delete_knowledge_base`` and ``delete_kb_document`` tools,
the knowledge-base syncs' stale-document sweep, the agent deletion (its
system-managed knowledge bases) and the skill deletion (the knowledge bases its
package installed). A COMMIT that succeeds cancels exactly the deleted
documents' jobs; a COMMIT that fails - refused by a deferred constraint trigger,
the way a real COMMIT fails after every statement passed - cancels none, and the
documents are still there. The cancellation itself is recorded, not sent: the
queue is the subject of ``test_document_job_queue.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import agent_skills as skills_api
from app.api.v1 import agents as agents_api
from app.api.v1 import knowledge_bases as kb_api
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_skill_package_origin import AgentSkillPackageOrigin
from app.models.knowledge_base import KBSyncSource, KnowledgeBase, KnowledgeDocument
from app.models.user import User
from app.services import agent_memory, kb_helpers, kb_sync
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, ScratchDatabase, scratch_database

pytestmark = pytest.mark.integration


@dataclass
class World:
    user: User
    kb: KnowledgeBase
    deleted: set[str] = field(default_factory=set)
    kept: set[str] = field(default_factory=set)
    target: str = ""


def _document(kb: KnowledgeBase, source_type: str = "text") -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid.uuid4(), knowledge_base_id=kb.id, filename=f"{uuid.uuid4().hex[:8]}.md", source_type=source_type
    )


async def _seed(
    sessions: async_sessionmaker,
    *,
    deleted: int = 2,
    kept: int = 1,
    source_type: str = "text",
    kept_in_same_kb: bool = False,
    kept_source_type: str | None = None,
    system_managed: bool = False,
) -> tuple[World, list[KnowledgeDocument]]:
    async with sessions() as db:
        user = User(
            email=f"docjobs-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Owner", role="admin"
        )
        db.add(user)
        await db.flush()
        kb = KnowledgeBase(name=f"kb-{uuid.uuid4().hex[:8]}", user_id=user.id, is_system_managed=system_managed)
        other = KnowledgeBase(name=f"other-{uuid.uuid4().hex[:8]}", user_id=user.id)
        db.add_all([kb, other])
        await db.flush()
        doomed = [_document(kb, source_type) for _ in range(deleted)]
        survivors = [
            _document(kb if kept_in_same_kb else other, kept_source_type or source_type) for _ in range(kept)
        ]
        db.add_all([*doomed, *survivors])
        await db.commit()
    world = World(user=user, kb=kb, deleted={str(d.id) for d in doomed}, kept={str(d.id) for d in survivors})
    return world, doomed


async def _builder(user: User, tool: str, **arguments: str) -> str:
    _, executor = build_agent_builder_tool(None, str(user.id))
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments))
    return result.content


# ── The deletions ────────────────────────────────────────────────────────────


async def _seed_rest_delete_knowledge_base(sessions):
    world, _ = await _seed(sessions)
    return world


async def _rest_delete_knowledge_base(scratch: ScratchDatabase, world: World) -> None:
    async with scratch.sessions() as db:
        await kb_api.delete_knowledge_base(kb_id=str(world.kb.id), request=None, user=world.user, db=db, _ai_gate=None)


async def _seed_one_document(sessions):
    world, doomed = await _seed(sessions, deleted=1, kept=1, kept_in_same_kb=True)
    world.target = str(doomed[0].id)
    return world


async def _rest_delete_document(scratch: ScratchDatabase, world: World) -> None:
    async with scratch.sessions() as db:
        await kb_api.delete_document(
            kb_id=str(world.kb.id), doc_id=world.target, user=world.user, db=db, _ai_gate=None
        )


async def _seed_force_resync(sessions):
    world, _ = await _seed(sessions, source_type="github", kept_in_same_kb=True, kept_source_type="text")
    async with sessions() as db:
        source = KBSyncSource(knowledge_base_id=world.kb.id, user_id=world.user.id, source_type="github", config={})
        db.add(source)
        await db.commit()
    world.target = str(source.id)
    return world


async def _rest_force_resync(scratch: ScratchDatabase, world: World) -> None:
    async with scratch.sessions() as db:
        await kb_api.force_resync_source(
            kb_id=str(world.kb.id), source_id=world.target, user=world.user, db=db, _ai_gate=None
        )


async def _builder_delete_knowledge_base(scratch: ScratchDatabase, world: World) -> str:
    return await _builder(world.user, "delete_knowledge_base", knowledge_base_id=str(world.kb.id))


async def _builder_delete_kb_document(scratch: ScratchDatabase, world: World) -> str:
    return await _builder(
        world.user, "delete_kb_document", knowledge_base_id=str(world.kb.id), document_id=world.target
    )


async def _seed_stale_sweep(sessions):
    world, _ = await _seed(sessions, source_type="github", kept_in_same_kb=True)
    return world


async def _stale_sweep(scratch: ScratchDatabase, world: World) -> None:
    """What every sync does: sweep the documents it no longer saw, then commit."""
    async with scratch.sessions() as db:
        kb = await db.get(KnowledgeBase, world.kb.id)
        assert await kb_sync._delete_stale_documents(db, kb, "github", world.kept) == len(world.deleted)
        await db.commit()


async def _seed_agent(sessions):
    world, _ = await _seed(sessions, system_managed=True)
    async with sessions() as db:
        agent = Agent(
            user_id=world.user.id,
            name="Doomed",
            slug=f"doomed-{uuid.uuid4().hex[:8]}",
            persona="p",
            knowledge_base_ids=[str(world.kb.id)],
        )
        db.add(agent)
        await db.commit()
    world.target = str(agent.id)
    return world


async def _rest_delete_agent(scratch: ScratchDatabase, world: World) -> None:
    async with scratch.sessions() as db:
        await agents_api.delete_agent(agent_id=world.target, request=None, user=world.user, db=db, _ai_gate=None)


async def _seed_package_skill(sessions):
    world, doomed = await _seed(sessions)
    async with sessions() as db:
        skill = AgentSkill(user_id=world.user.id, name=f"skill-{uuid.uuid4().hex[:8]}", content="c")
        db.add(skill)
        await db.flush()
        db.add_all(
            AgentSkillPackageOrigin(
                skill_id=skill.id,
                resource_kind="reference",
                resource_path=f"references/{doc.filename}",
                knowledge_base_id=world.kb.id,
                knowledge_document_id=doc.id,
            )
            for doc in doomed
        )
        await db.commit()
    world.target = str(skill.id)
    return world


async def _rest_delete_skill(scratch: ScratchDatabase, world: World) -> None:
    async with scratch.sessions() as db:
        await skills_api.delete_skill(skill_id=world.target, request=None, user=world.user, db=db, _ai_gate=None)


Seed = Callable[[async_sessionmaker], Awaitable[World]]
Run = Callable[[ScratchDatabase, World], Awaitable[str | None]]

DELETIONS: dict[str, tuple[Seed, Run]] = {
    "rest-delete-knowledge-base": (_seed_rest_delete_knowledge_base, _rest_delete_knowledge_base),
    "rest-delete-document": (_seed_one_document, _rest_delete_document),
    "rest-force-resync": (_seed_force_resync, _rest_force_resync),
    "builder-delete-knowledge-base": (_seed_rest_delete_knowledge_base, _builder_delete_knowledge_base),
    "builder-delete-kb-document": (_seed_one_document, _builder_delete_kb_document),
    "sync-stale-sweep": (_seed_stale_sweep, _stale_sweep),
    "rest-delete-agent": (_seed_agent, _rest_delete_agent),
    "rest-delete-skill": (_seed_package_skill, _rest_delete_skill),
}


# ── Harness ──────────────────────────────────────────────────────────────────


@pytest.fixture
def cancelled(monkeypatch) -> list[set[str]]:
    calls: list[set[str]] = []

    async def _record(doc_ids) -> int:
        calls.append(set(doc_ids))
        return 0

    monkeypatch.setattr(kb_helpers, "cancel_document_processing_jobs", _record)
    # Outside the deletion: what the routes do around it that reaches other services.
    monkeypatch.setattr(kb_api, "_assert_sync_sources_licensed", AsyncMock())
    monkeypatch.setattr(kb_api, "_spawn_sync_task", lambda coro: coro.close())
    monkeypatch.setattr(agent_memory, "clear_deleted_agent_memory", AsyncMock())
    return calls


async def _settled() -> None:
    loop = asyncio.get_running_loop()
    await asyncio.gather(*(t for t in kb_helpers._cancellations_in_flight if t.get_loop() is loop))


async def _refuse_document_deletes_at_commit(engine: AsyncEngine, *, errcode: str = "P0001", times: int = 0) -> None:
    """Make the COMMIT of any transaction that deleted a document fail.

    *times* = 0 refuses every such COMMIT; otherwise only the first *times*
    (a sequence counts them: ``nextval`` is not rolled back with the refusal).
    """
    guard = "TRUE" if times == 0 else f"nextval('pr4842_refusals') <= {times}"
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SEQUENCE IF NOT EXISTS pr4842_refusals"))
        await conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION pr4842_refuse_commit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF {guard} THEN RAISE EXCEPTION 'pr4842: refused at commit' USING ERRCODE = '{errcode}'; "
                "END IF; RETURN NULL; END $$"
            )
        )
        await conn.execute(
            text(
                "CREATE CONSTRAINT TRIGGER pr4842_refuse_document_delete AFTER DELETE ON knowledge_documents "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION pr4842_refuse_commit()"
            )
        )


async def _document_ids(sessions: async_sessionmaker) -> set[str]:
    async with sessions() as db:
        return {str(i) for i in (await db.execute(select(KnowledgeDocument.id))).scalars()}


def _scratch():
    return scratch_database(
        "docjobs", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True, refuse_other_databases=True
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", DELETIONS)
async def test_a_committed_deletion_cancels_exactly_the_jobs_of_the_documents_it_deleted(cancelled, deletion):
    seed, run = DELETIONS[deletion]
    async with _scratch() as scratch:
        world = await seed(scratch.sessions)
        answer = await run(scratch, world)
        await _settled()
        remaining = await _document_ids(scratch.sessions)

    assert answer is None or "deleted" in answer, answer
    assert cancelled == [world.deleted]
    assert remaining.isdisjoint(world.deleted)
    assert world.kept <= remaining


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", DELETIONS)
async def test_a_deletion_whose_commit_fails_cancels_nothing(cancelled, deletion):
    seed, run = DELETIONS[deletion]
    async with _scratch() as scratch:
        world = await seed(scratch.sessions)
        await _refuse_document_deletes_at_commit(scratch.engine)
        try:
            answer = await run(scratch, world)
        except Exception as exc:  # the REST routes and the sync raise; the builder answers
            answer = f"raised {type(exc).__name__}"
        await _settled()
        remaining = await _document_ids(scratch.sessions)

    assert answer is not None and ("raised" in answer or "Failed to delete" in answer), answer
    assert cancelled == []
    assert world.deleted | world.kept <= remaining


@pytest.mark.asyncio
async def test_a_retried_agent_deletion_cancels_once_for_the_attempt_that_committed(cancelled):
    """A serialization failure at COMMIT is retried: the failed attempt cancels nothing."""
    async with _scratch() as scratch:
        world = await _seed_agent(scratch.sessions)
        await _refuse_document_deletes_at_commit(scratch.engine, errcode="40001", times=1)
        await _rest_delete_agent(scratch, world)
        await _settled()
        remaining = await _document_ids(scratch.sessions)
        async with scratch.sessions() as db:
            assert await db.get(Agent, uuid.UUID(world.target)) is None

    assert cancelled == [world.deleted]
    assert remaining.isdisjoint(world.deleted)
