"""Workset ingestion against a real PostgreSQL.

Same scratch-database recipe as ``test_flow_runner_postgres.py``. A real
database rather than a fake session because what is under test IS the
persistence: the dedup constraint, the counters, and what survives an
eviction.

The embedding queue is stubbed — the background job is not what these assert;
that a document was *handed to it* is.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.user import User
from app.models.workset import Workset, WorksetItem
from app.services.workset import WorksetSourceError, add_item, resolve_workset
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    # The corpus lives in the KB tables, and `embeddings` carries a pgvector
    # column - the whole schema is created rather than a hand-picked subset, so
    # an FK this feature grows later does not fail here first.
    async with scratch_database("workset", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_user(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"ws-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Workset Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    return user


async def _documents(session, kb_id):
    return (
        (
            await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_id == kb_id
                )
            )
        )
        .scalars()
        .all()
    )


# ── Creation ─────────────────────────────────────────────────────────────────


async def test_a_new_workset_has_no_corpus_until_content_arrives():
    """A workset used only for coordination must not cost a KB row."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await session.commit()

        assert ws.knowledge_base_id is None
        assert (
            await session.scalar(select(func.count()).select_from(KnowledgeBase))
        ) == 0


async def test_resolving_the_same_key_twice_returns_the_same_workset():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        first = await resolve_workset(session, user, key="reports", scope="global")
        await session.commit()
        second = await resolve_workset(session, user, key="reports", scope="global")
        assert first.id == second.id


async def test_scope_isolates_two_runs_of_the_same_flow():
    """The isolation IS the scope's promise — two concurrent runs must not
    share a corpus just because the key matches."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        run_a = await resolve_workset(
            session, user, key="reports", scope="run", scope_ref="run-a"
        )
        run_b = await resolve_workset(
            session, user, key="reports", scope="run", scope_ref="run-b"
        )
        await session.commit()
        assert run_a.id != run_b.id


# ── Ingestion ────────────────────────────────────────────────────────────────


async def test_content_lands_in_an_internal_corpus_and_is_queued():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")

        outcome = await add_item(
            session,
            ws,
            user,
            title="APT report",
            content="A" * 500,
            external_ref="report-1",
            occurred_at=datetime.now(UTC),
        )
        await session.commit()

        assert outcome.ok
        assert outcome.action == "created"
        assert ws.item_count == 1
        assert ws.content_chars == 500

        kb = (
            await session.execute(
                select(KnowledgeBase).where(KnowledgeBase.id == ws.knowledge_base_id)
            )
        ).scalar_one()
        # Never a user-facing collection.
        assert kb.is_internal is True
        docs = await _documents(session, kb.id)
        assert len(docs) == 1
        assert docs[0].content == "A" * 500
        # Reported for queueing AFTER the commit, not enqueued inline:
        # a worker that picks the job up first would find no row.
        assert outcome.pending_documents == [docs[0].id]


async def test_re_adding_identical_content_changes_nothing():
    """The overlap of a sliding window: seen, unchanged, no work."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await add_item(
            session, ws, user, title="R", content="B" * 200, external_ref="r-1"
        )
        await session.commit()

        outcome = await add_item(
            session, ws, user, title="R", content="B" * 200, external_ref="r-1"
        )
        await session.commit()

        assert outcome.action == "unchanged"
        assert outcome.item.revision == 1
        assert ws.item_count == 1
        assert outcome.pending_documents == []
        assert len(await _documents(session, ws.knowledge_base_id)) == 1


async def test_changed_content_is_a_revision_not_a_duplicate():
    """A source that corrects itself days later is the case this exists for."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await add_item(
            session, ws, user, title="R", content="C" * 200, external_ref="r-1"
        )
        item_id = first.item.id
        document_id = first.item.knowledge_document_id
        # An agent had already summarised and ranked it.
        first.item.summary = "Old summary"
        first.item.summary_source = "agent"
        first.item.priority_score = 80
        first.item.priority_source = "agent"
        first.item.status = "ready"
        await session.commit()

        outcome = await add_item(
            session,
            ws,
            user,
            title="R (corrected)",
            content="D" * 300,
            external_ref="r-1",
        )
        await session.commit()

        assert outcome.action == "revised"
        item = outcome.item
        assert item.id == item_id
        assert item.revision == 2
        # Everything derived from the OLD text is void — including what an
        # agent wrote, because it described text that no longer exists.
        assert item.summary is None
        assert item.summary_source is None
        assert item.priority_score is None
        assert item.status == "pending"
        # Same document id: stored citations stay valid.
        assert item.knowledge_document_id == document_id
        docs = await _documents(session, ws.knowledge_base_id)
        assert len(docs) == 1
        assert docs[0].content == "D" * 300
        assert outcome.pending_documents == [document_id]
        # Counters follow the swap, not the sum.
        assert ws.item_count == 1
        assert ws.content_chars == 300


async def test_a_metadata_only_re_add_leaves_derived_state_alone():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await add_item(
            session, ws, user, title="R", content="E" * 200, external_ref="r-1"
        )
        first.item.summary = "Kept"
        first.item.summary_source = "agent"
        await session.commit()

        outcome = await add_item(
            session,
            ws,
            user,
            title="R",
            external_ref="r-1",
            source_url="https://example.test/r1",
        )
        await session.commit()

        assert outcome.action == "updated"
        assert outcome.item.summary == "Kept"
        assert outcome.item.source_url == "https://example.test/r1"
        assert outcome.item.revision == 1


async def test_a_revision_keeps_what_the_same_call_gives_for_the_new_text():
    """The reset voids what described the OLD text; a summary, a score
    and tags given with the new text describe the new text and are kept."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await add_item(
            session, ws, user, title="t", content="A" * 200, external_ref="x",
            summary="s1", priority=80, tags=["a"],
        )
        await session.commit()
        later = datetime(2026, 9, 1, tzinfo=UTC)

        outcome = await add_item(
            session, ws, user, title="t", content="B" * 200, external_ref="x",
            summary="s2", priority=90, tags=["b"],
            source_url="https://example.test/x", occurred_at=later,
        )
        await session.commit()

        assert outcome.action == "revised"
        item = outcome.item
        assert item.id == first.item.id
        assert item.revision == 2
        assert item.summary == "s2"
        assert item.summary_source == "agent"
        assert item.priority_score == 90
        assert item.priority_source == "agent"
        assert item.priority_reason is None
        assert item.tags == ["b"]
        assert item.source_url == "https://example.test/x"
        assert item.occurred_at == later
        # `ready` also means searchable: the job embedding the new text
        # promotes it (test_workset_auto_pass_postgres.py).
        assert item.status == "pending"
        assert outcome.pending_documents == [item.knowledge_document_id]


async def test_a_revision_given_one_value_still_clears_the_other():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await add_item(
            session, ws, user, title="t", content="A" * 200, external_ref="x",
            summary="s1", priority=80,
        )
        await session.commit()

        outcome = await add_item(
            session, ws, user, title="t", content="B" * 200, external_ref="x",
            summary="s2",
        )
        await session.commit()

        assert outcome.action == "revised"
        assert outcome.item.summary == "s2"
        assert outcome.item.summary_source == "agent"
        assert outcome.item.priority_score is None
        assert outcome.item.priority_source is None
        assert outcome.item.status == "pending"


async def test_an_unchanged_re_add_still_takes_the_metadata_it_gives():
    """A caller deduping on `occurred_at` must see the date it just gave, or
    the item ages out of its window although it was handled again."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        earlier = datetime(2026, 9, 1, tzinfo=UTC)
        await add_item(
            session, ws, user, title="t", content="A" * 200, external_ref="x",
            occurred_at=earlier, tags=["a"], summary="s1", priority=80,
        )
        await session.commit()
        later = earlier + timedelta(days=2)

        outcome = await add_item(
            session, ws, user, title="t", content="A" * 200, external_ref="x",
            occurred_at=later, tags=["b"], source_url="https://example.test/x",
            summary="s2", priority=10,
        )
        await session.commit()

        assert outcome.action == "unchanged"
        assert outcome.item.revision == 1
        assert outcome.item.occurred_at == later
        assert outcome.item.tags == ["b"]
        assert outcome.item.source_url == "https://example.test/x"
        # The text is the same, so what was derived from it stands.
        assert outcome.item.summary == "s1"
        assert outcome.item.priority_score == 80
        assert outcome.pending_documents == []


async def test_a_revision_is_weighed_for_eviction_at_the_score_it_is_given():
    """The stored score described the old text: a revision given a lower one
    must not evict an item worth more than what it now is."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global",
            max_items=100, max_content_chars=250,
        )
        other = await _add_scored(session, ws, user, ref="other", score=50)
        await _add_scored(session, ws, user, ref="x", score=90)
        await session.commit()

        outcome = await add_item(
            session, ws, user, title="x", content="Y" * 200, external_ref="x",
            priority=10,
        )
        await session.commit()

        assert outcome.action == "refused"
        assert "budget" in outcome.error
        await session.refresh(other)
        assert other.status != "evicted"
        assert ws.evicted_count == 0


async def test_a_referenced_document_is_neither_copied_nor_re_embedded():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        foreign_kb = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Team wiki")
        session.add(foreign_kb)
        await session.flush()
        foreign_doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=foreign_kb.id,
            filename="wiki.md",
            source_type="text",
            content="F" * 300,
            status="ready",
        )
        session.add(foreign_doc)
        await session.flush()

        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session,
            ws,
            user,
            title="Wiki page",
            knowledge_document_id=foreign_doc.id,
            external_ref="wiki-1",
        )
        await session.commit()

        assert outcome.ok
        assert outcome.item.document_is_foreign is True
        assert outcome.item.knowledge_document_id == foreign_doc.id
        # Not re-embedded, and no corpus was created for a reference alone.
        assert outcome.pending_documents == []
        assert ws.knowledge_base_id is None


# ── Budget and eviction ──────────────────────────────────────────────────────


async def _add_scored(session, ws, user, *, ref, score, chars=100, pinned=False):
    outcome = await add_item(
        session,
        ws,
        user,
        title=ref,
        content="G" * chars,
        external_ref=ref,
        priority=score,
        pinned=pinned,
        occurred_at=datetime.now(UTC) - timedelta(days=1),
    )
    return outcome.item


async def test_the_lowest_scored_item_loses_its_content_first():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=2
        )
        low = await _add_scored(session, ws, user, ref="low", score=10)
        high = await _add_scored(session, ws, user, ref="high", score=90)
        await session.commit()

        outcome = await add_item(
            session, ws, user, title="new", content="H" * 100, external_ref="new"
        )
        await session.commit()

        assert outcome.ok
        assert outcome.evicted == [low.id]

        await session.refresh(low)
        await session.refresh(high)
        assert low.status == "evicted"
        # Content and vectors are what is reclaimed — the analysis survives.
        assert low.knowledge_document_id is None
        assert low.content_chars == 0
        assert low.priority_score == 10
        assert high.status != "evicted"
        assert ws.evicted_count == 1
        assert ws.item_count == 2


async def test_a_pinned_item_is_never_evicted():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=2
        )
        pinned = await _add_scored(
            session, ws, user, ref="pinned", score=1, pinned=True
        )
        other = await _add_scored(session, ws, user, ref="other", score=50)
        await session.commit()

        outcome = await add_item(
            session, ws, user, title="new", content="H" * 100, external_ref="new"
        )
        await session.commit()

        assert outcome.ok
        # The pinned item scores lowest but is untouchable, so the next
        # candidate goes instead.
        assert outcome.evicted == [other.id]
        await session.refresh(pinned)
        assert pinned.status != "evicted"


async def test_nothing_is_evicted_for_an_incoming_item_worth_less():
    """Trading a better item for a worse one and calling it success is the
    failure mode this guard exists for."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=1
        )
        keeper = await _add_scored(session, ws, user, ref="keeper", score=90)
        await session.commit()

        outcome = await add_item(
            session,
            ws,
            user,
            title="new",
            content="H" * 100,
            external_ref="new",
            priority=5,
        )
        await session.commit()

        assert not outcome.ok
        assert outcome.action == "refused"
        assert "budget" in outcome.error
        await session.refresh(keeper)
        assert keeper.status != "evicted"
        assert ws.item_count == 1


async def test_an_unscored_item_outlives_a_known_low_one():
    """Unknown value beats known-low: an item nobody has judged yet may be the
    most important one in the window."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=2
        )
        scored_low = await _add_scored(session, ws, user, ref="low", score=5)
        unscored = await add_item(
            session,
            ws,
            user,
            title="unscored",
            content="I" * 100,
            external_ref="unscored",
        )
        await session.commit()

        outcome = await add_item(
            session, ws, user, title="new", content="J" * 100, external_ref="new"
        )
        await session.commit()

        assert outcome.evicted == [scored_low.id]
        await session.refresh(unscored.item)
        assert unscored.item.status != "evicted"


async def test_the_content_budget_evicts_on_size_not_only_count():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session,
            user,
            key="reports",
            scope="global",
            max_items=100,
            max_content_chars=500,
        )
        low = await _add_scored(session, ws, user, ref="low", score=10, chars=300)
        await _add_scored(session, ws, user, ref="high", score=90, chars=100)
        await session.commit()

        outcome = await add_item(
            session, ws, user, title="new", content="K" * 200, external_ref="new"
        )
        await session.commit()

        assert outcome.ok
        assert outcome.evicted == [low.id]
        assert ws.content_chars <= ws.max_content_chars


async def test_evicting_removes_the_document_from_the_corpus():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=1
        )
        low = await _add_scored(session, ws, user, ref="low", score=10)
        document_id = low.knowledge_document_id
        await session.commit()

        await add_item(
            session, ws, user, title="new", content="L" * 100, external_ref="new"
        )
        await session.commit()

        remaining = (
            await session.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
            )
        ).scalar_one_or_none()
        assert remaining is None


# ── Dedup constraint ─────────────────────────────────────────────────────────


async def test_items_without_a_ref_never_collide():
    """NULLs are distinct in Postgres — a source with no stable id must not
    dedup against another one."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await add_item(session, ws, user, title="one", content="M" * 100)
        await add_item(session, ws, user, title="two", content="N" * 100)
        await session.commit()

        count = await session.scalar(
            select(func.count()).select_from(WorksetItem).where(
                WorksetItem.workset_id == ws.id
            )
        )
        assert count == 2
        assert ws.item_count == 2


async def test_two_worksets_may_hold_the_same_external_ref():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        first = await resolve_workset(session, user, key="a", scope="global")
        second = await resolve_workset(session, user, key="b", scope="global")
        await add_item(
            session, first, user, title="R", content="O" * 100, external_ref="r-1"
        )
        await add_item(
            session, second, user, title="R", content="O" * 100, external_ref="r-1"
        )
        await session.commit()

        count = await session.scalar(select(func.count()).select_from(Workset))
        assert count == 2


# ── Review findings, pinned ──────────────────────────────────────────
#
# Each of these is a bug the review found in the first cut. They are grouped
# here rather than scattered so the next person changing `add_item` can see
# what it must not go back to being.


async def test_a_correction_never_destroys_a_borrowed_document():
    """The severe one: rewriting in place would have deleted a document
    belonging to somebody else's collection."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        foreign_kb = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Team wiki")
        session.add(foreign_kb)
        await session.flush()
        foreign_doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=foreign_kb.id,
            filename="wiki.md",
            source_type="text",
            content="ORIGINAL",
            status="ready",
        )
        session.add(foreign_doc)
        await session.flush()

        ws = await resolve_workset(session, user, key="reports", scope="global")
        await add_item(
            session,
            ws,
            user,
            title="Wiki",
            knowledge_document_id=foreign_doc.id,
            external_ref="wiki-1",
        )
        await session.commit()

        # The same ref comes back, this time with inline text.
        outcome = await add_item(
            session,
            ws,
            user,
            title="Wiki",
            content="CORRECTED " * 20,
            external_ref="wiki-1",
        )
        await session.commit()

        assert outcome.action == "revised"
        # The borrowed document is untouched...
        survivor = await session.get(KnowledgeDocument, foreign_doc.id)
        assert survivor is not None
        assert survivor.content == "ORIGINAL"
        assert survivor.knowledge_base_id == foreign_kb.id
        # ...and the item now owns a fresh document in the workset's corpus.
        assert outcome.item.knowledge_document_id != foreign_doc.id
        assert outcome.item.document_is_foreign is False


async def test_a_document_you_cannot_read_cannot_be_referenced():
    """Workset reads are authorized by the WORKSET, so an unchecked reference
    would be a way to read any document by id."""
    async with _temp_database() as session_factory, session_factory() as session:
        owner = await _seed_user(session)
        stranger = await _seed_user(session)
        private_kb = KnowledgeBase(
            id=uuid.uuid4(), user_id=owner.id, name="Private"
        )
        session.add(private_kb)
        await session.flush()
        private_doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=private_kb.id,
            filename="secret.md",
            source_type="text",
            content="SECRET",
            status="ready",
        )
        session.add(private_doc)
        await session.commit()

        ws = await resolve_workset(session, stranger, key="mine", scope="global")
        outcome = await add_item(
            session,
            ws,
            stranger,
            title="Borrowed",
            knowledge_document_id=private_doc.id,
        )

        assert not outcome.ok
        assert outcome.action == "refused"
        assert "not one you can read" in outcome.error


async def test_one_workset_cannot_borrow_anothers_internal_corpus():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        theirs = await resolve_workset(session, user, key="theirs", scope="global")
        seeded = await add_item(
            session, theirs, user, title="R", content="A" * 200
        )
        await session.commit()

        mine = await resolve_workset(session, user, key="mine", scope="global")
        outcome = await add_item(
            session,
            mine,
            user,
            title="Borrowed",
            knowledge_document_id=seeded.item.knowledge_document_id,
        )
        # Same owner, and still refused: an internal corpus is not a
        # collection anyone — including its owner — reads through another
        # primitive.
        assert not outcome.ok
        assert "not one you can read" in outcome.error


async def test_exactly_one_content_source_is_required():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        seeded = await add_item(session, ws, user, title="R", content="A" * 200)
        await session.commit()

        both = await add_item(
            session,
            ws,
            user,
            title="Ambiguous",
            content="B" * 200,
            knowledge_document_id=seeded.item.knowledge_document_id,
        )
        assert not both.ok
        # Silently preferring one would store a hash describing content that
        # was thrown away.
        assert "exactly one content source" in both.error


async def test_ingesting_a_stored_file_is_refused_until_it_works():
    """The extraction path does not exist yet; accepting the reference would
    create an item that stays pending forever."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session, ws, user, title="A PDF", file_attachment_id=uuid.uuid4()
        )
        assert not outcome.ok
        assert "not implemented yet" in outcome.error


async def test_short_content_is_stored_and_queued_like_any_other():
    """Short content is content — dropping it because it is too small to
    chunk would lose it outright, since the item row holds no text. It is
    queued too: left unqueued, its document sat `pending` until the
    stuck-document sweeper queued it as a lost job, minutes later, and the
    automatic pass that rides that job waited with it."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="Tiny", content="short")
        await session.commit()

        assert outcome.ok
        assert outcome.item.knowledge_document_id is not None
        assert outcome.pending_documents == [outcome.item.knowledge_document_id]
        docs = await _documents(session, ws.knowledge_base_id)
        assert [d.content for d in docs] == ["short"]


async def test_a_refused_add_evicts_nothing():
    """Partial eviction plus a refusal is the worst of both: content gone and
    nothing gained."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session,
            user,
            key="reports",
            scope="global",
            max_items=3,
            max_content_chars=400,
        )
        low = await _add_scored(session, ws, user, ref="low", score=10, chars=100)
        high = await _add_scored(session, ws, user, ref="high", score=99, chars=100)
        await session.commit()

        # Too large to fit even after evicting everything evictable.
        outcome = await add_item(
            session,
            ws,
            user,
            title="huge",
            content="Z" * 5000,
            external_ref="huge",
            priority=50,
        )
        await session.commit()

        assert not outcome.ok
        assert outcome.evicted == []
        assert "Nothing was evicted" in outcome.error
        await session.refresh(low)
        await session.refresh(high)
        assert low.status != "evicted"
        assert high.status != "evicted"
        assert ws.evicted_count == 0


async def test_the_item_budget_can_evict_rows_that_hold_no_content():
    """A by-reference item counts against `max_items` but holds zero chars —
    a workset full of them could otherwise never make room."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        source_kb = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Wiki")
        session.add(source_kb)
        await session.flush()
        docs = []
        for index in range(2):
            doc = KnowledgeDocument(
                id=uuid.uuid4(),
                knowledge_base_id=source_kb.id,
                filename=f"w{index}.md",
                source_type="text",
                content="X" * 200,
                status="ready",
            )
            session.add(doc)
            docs.append(doc)
        await session.flush()

        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=2
        )
        for index, doc in enumerate(docs):
            await add_item(
                session,
                ws,
                user,
                title=f"ref-{index}",
                knowledge_document_id=doc.id,
                external_ref=f"ref-{index}",
            )
        await session.commit()
        assert ws.item_count == 2

        outcome = await add_item(
            session, ws, user, title="new", content="Y" * 100, external_ref="new"
        )
        await session.commit()

        assert outcome.ok, outcome.error
        assert len(outcome.evicted) == 1
        assert ws.item_count == 2


async def test_revising_the_only_item_at_the_limit_does_not_evict_itself():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=1
        )
        first = await add_item(
            session, ws, user, title="R", content="A" * 200, external_ref="r-1"
        )
        item_id = first.item.id
        await session.commit()

        outcome = await add_item(
            session, ws, user, title="R", content="B" * 300, external_ref="r-1"
        )
        await session.commit()

        assert outcome.action == "revised"
        assert outcome.evicted == []
        await session.refresh(outcome.item)
        assert outcome.item.id == item_id
        assert outcome.item.status == "pending"
        # The row it already occupied is still counted exactly once.
        assert ws.item_count == 1
        assert ws.content_chars == 300


async def test_a_non_global_scope_without_its_reference_is_refused():
    """Falling back to the bare key would silently share one corpus across
    every run — the opposite of what the scope promises."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        with pytest.raises(WorksetSourceError) as caught:
            await resolve_workset(session, user, key="reports", scope="run")
        assert "needs the id it is scoped to" in str(caught.value)


async def test_the_corpus_keeps_the_knowledge_bases_document_count_honest():
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=1
        )
        await _add_scored(session, ws, user, ref="first", score=10)
        await session.commit()

        kb = await session.get(KnowledgeBase, ws.knowledge_base_id)
        assert kb.document_count == 1

        # The second add evicts the first, so the count nets back to one.
        await add_item(
            session, ws, user, title="second", content="B" * 200, external_ref="second"
        )
        await session.commit()
        await session.refresh(kb)
        assert kb.document_count == 1


async def test_resolving_the_same_key_concurrently_yields_one_workset():
    """Two workers resolving the same key must not have one of them raise on
    the unique constraint — that is exactly what an idempotent resolver is
    for."""
    async with _temp_database() as session_factory:
        async with session_factory() as setup:
            user = await _seed_user(setup)
            await setup.commit()
            user_id = user.id

        async def _resolve():
            async with session_factory() as session:
                loaded = await session.get(User, user_id)
                ws = await resolve_workset(
                    session, loaded, key="shared", scope="global"
                )
                await session.commit()
                return ws.id

        # Sequential sessions exercise the ON CONFLICT path: the second insert
        # collides and must resolve to the first row rather than raising.
        first = await _resolve()
        second = await _resolve()
        assert first == second

        async with session_factory() as check:
            count = await check.scalar(select(func.count()).select_from(Workset))
            assert count == 1


async def test_revising_the_lowest_scored_item_at_the_limit_is_not_self_eviction():
    """The exact regression the review asked for.

    A workset at `max_items` where the LOWEST-scored item is the one being
    revised: it matched every eviction filter, and the self-guard used a
    strict `>` so an equal score did not stop it. It evicted itself, decremented
    `item_count` without ever restoring it, and — because `_evict_item` nulls
    the document id — the revision then minted a fresh document, silently
    dangling every stored citation.
    """
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=2
        )
        low = await _add_scored(session, ws, user, ref="low", score=5, chars=100)
        await _add_scored(session, ws, user, ref="high", score=90, chars=100)
        await session.commit()

        document_id = low.knowledge_document_id
        assert ws.item_count == 2
        assert ws.evicted_count == 0

        outcome = await add_item(
            session,
            ws,
            user,
            title="low (corrected)",
            content="N" * 150,
            external_ref="low",
        )
        await session.commit()

        assert outcome.action == "revised"
        assert outcome.evicted == []
        # Counters survive a revision untouched.
        assert ws.item_count == 2
        assert ws.evicted_count == 0
        # And the document id is stable, so stored citations still resolve.
        await session.refresh(low)
        assert low.knowledge_document_id == document_id
        assert low.status == "pending"
        assert low.revision == 2
        assert ws.content_chars == 250


async def test_eviction_covers_a_size_deficit_spread_over_many_small_items():
    """The candidate query is paged; correctness must not depend on one page
    being enough."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session,
            user,
            key="reports",
            scope="global",
            max_items=1000,
            max_content_chars=1000,
        )
        for index in range(10):
            await _add_scored(
                session, ws, user, ref=f"s-{index}", score=index, chars=100
            )
        await session.commit()
        assert ws.content_chars == 1000

        outcome = await add_item(
            session, ws, user, title="new", content="Q" * 500, external_ref="new"
        )
        await session.commit()

        assert outcome.ok, outcome.error
        # Five of the ten small items had to go to free 500 characters.
        assert len(outcome.evicted) == 5
        assert ws.content_chars <= ws.max_content_chars


# ── Reading: coverage, chunk expansion, digest ───────────────────────
#
# Search itself needs an embedding provider and is exercised where one exists;
# what is pinned here is everything the READ contract promises without one:
# that coverage is never overstated, that a hit can be widened by whole
# chunks, and that a digest says what it left out.


async def test_coverage_never_calls_an_unsealed_workset_complete():
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session, ws, user, title="R", content="A" * 200, external_ref="r-1"
        )
        outcome.item.status = "ready"
        await session.commit()

        cov = await coverage_of(session, ws)
        assert cov.total == 1
        assert cov.ready == 1
        assert cov.sealed is False
        # Everything added is processed — and it still is NOT complete,
        # because more may arrive.
        assert cov.complete is False
        assert "NOT sealed" in cov.describe()

        ws.sealed_at = datetime.now(UTC)
        await session.commit()
        cov = await coverage_of(session, ws)
        assert cov.complete is True
        assert "NOT sealed" not in cov.describe()


async def test_coverage_reports_a_gap_against_the_declared_total():
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        ws.declared_total = 137
        await add_item(session, ws, user, title="R", content="A" * 200)
        ws.sealed_at = datetime.now(UTC)
        await session.commit()

        cov = await coverage_of(session, ws)
        described = cov.describe()
        # A claim and a fact that disagree are reported as disagreeing, never
        # reconciled behind the reader's back.
        assert "137" in described
        assert "unexplained" in described
        assert cov.complete is False


async def test_coverage_says_evicted_items_are_not_searchable():
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=1
        )
        await _add_scored(session, ws, user, ref="low", score=1)
        await add_item(
            session, ws, user, title="new", content="B" * 100, external_ref="new"
        )
        await session.commit()

        cov = await coverage_of(session, ws)
        assert cov.evicted == 1
        assert "NOT searchable" in cov.describe()


async def test_reading_by_chunk_widens_a_hit_by_whole_chunks():
    """A search hit is a fragment; the sentence that explains it is often the
    one after."""
    from app.services.workset_read import chunks_for_document, read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        # Distinct paragraphs so chunk boundaries are legible in the assert.
        content = "\n\n".join(f"Paragraph {i}. " + ("word " * 200) for i in range(12))
        outcome = await add_item(
            session, ws, user, title="Long report", content=content
        )
        await session.commit()

        resolved = await chunks_for_document(
            session, outcome.item.knowledge_document_id
        )
        # Chunked with the parameters the corpus actually embedded it under.
        assert resolved is not None and resolved.exact
        chunks = resolved.chunks
        assert len(chunks) > 3, "the fixture must actually produce several chunks"

        narrow = await read_item(
            session, ws, user=user, item_id=outcome.item.id, chunk_index=2,
            max_chars=20000,
        )
        widened = await read_item(
            session,
            ws,
            user=user,
            item_id=outcome.item.id,
            chunk_index=2,
            expand_before=1,
            expand_after=1,
            max_chars=20000,
        )

        assert narrow.chunk_index == 2
        assert narrow.chunk_count == len(chunks)
        assert narrow.text == chunks[2]
        # Widening is additive and contiguous — the hit is still in there.
        assert chunks[2] in widened.text
        assert len(widened.text) > len(narrow.text)
        assert widened.text.startswith(chunks[1][:40])


async def test_chunk_expansion_is_clamped_at_the_document_edges():
    from app.services.workset_read import read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        content = "\n\n".join(f"Para {i}. " + ("word " * 120) for i in range(4))
        outcome = await add_item(session, ws, user, title="R", content=content)
        await session.commit()

        # Ask for far more context than exists on either side.
        result = await read_item(
            session,
            ws,
            user=user,
            item_id=outcome.item.id,
            chunk_index=0,
            expand_before=99,
            expand_after=99,
            max_chars=20000,
        )
        assert result.error is None
        assert result.chunk_index == 0
        assert result.text


async def test_an_out_of_range_chunk_index_is_clamped_not_refused():
    from app.services.workset_read import read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session, ws, user, title="R", content="word " * 300
        )
        await session.commit()

        result = await read_item(
            session, ws, user=user, item_id=outcome.item.id, chunk_index=9999
        )
        assert result.error is None
        assert result.chunk_index == result.chunk_count - 1


async def test_paging_by_offset_reports_what_remains():
    from app.services.workset_read import read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="R", content="C" * 5000)
        await session.commit()

        first = await read_item(
            session, ws, user=user, item_id=outcome.item.id, offset=0,
            max_chars=1000,
        )
        assert first.text == "C" * 1000
        assert first.truncated is True
        assert "offset=1000" in first.note

        past_end = await read_item(
            session, ws, user=user, item_id=outcome.item.id, offset=99999
        )
        assert past_end.error is not None
        assert "past the end" in past_end.error


async def test_reading_an_evicted_item_returns_its_summary_and_says_so():
    """Not an error: the analysis survived. What must not happen is the caller
    believing it read the source."""
    from app.services.workset_read import read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", max_items=1
        )
        low = await _add_scored(session, ws, user, ref="low", score=1)
        low.summary = "The gist survived"
        low.summary_source = "agent"
        await session.commit()
        await add_item(
            session, ws, user, title="new", content="D" * 100, external_ref="new"
        )
        await session.commit()

        result = await read_item(session, ws, user=user, item_id=low.id)
        assert result.error is None
        assert result.text == "The gist survived"
        assert "CONTENT EVICTED" in result.note


async def test_an_item_from_another_workset_is_not_readable():
    from app.services.workset_read import read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        mine = await resolve_workset(session, user, key="mine", scope="global")
        theirs = await resolve_workset(session, user, key="theirs", scope="global")
        outcome = await add_item(
            session, theirs, user, title="R", content="E" * 200
        )
        await session.commit()

        result = await read_item(session, mine, user=user, item_id=outcome.item.id)
        assert result.error == "No such item in this workset."


async def test_the_digest_ranks_by_priority_and_carries_citations():
    from app.services.workset_read import digest

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for ref, score in (("low", 10), ("high", 95), ("mid", 50)):
            item = await _add_scored(session, ws, user, ref=ref, score=score)
            item.summary = f"Summary of {ref}"
            item.status = "ready"
        await session.commit()

        result = await digest(session, ws)
        rendered = result.render()

        assert result.included == 3
        assert result.omitted == 0
        # Highest priority first.
        assert rendered.index("high") < rendered.index("mid") < rendered.index("low")
        # Every line is citable back to an item.
        assert rendered.count("cite: item=") == 3
        # Raw content never appears in a digest.
        assert "GGG" not in rendered


async def test_the_digest_says_what_it_left_out():
    """'Here are the 20 that fit' read as 'here are the 20 that matter' is how
    a synthesis silently loses its tail."""
    from app.services.workset_read import digest

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(8):
            item = await _add_scored(
                session, ws, user, ref=f"r-{index}", score=index * 10
            )
            item.summary = f"Summary {index}"
        await session.commit()

        result = await digest(session, ws, top_n=3)
        rendered = result.render()

        assert result.included == 3
        assert result.omitted == 5
        assert "left out for length" in rendered
        assert "NOT because they are irrelevant" in rendered


async def test_the_digest_of_an_empty_workset_is_still_honest():
    from app.services.workset_read import digest

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await session.commit()

        rendered = (await digest(session, ws)).render()
        assert "0 item(s)" in rendered
        assert "NOT sealed" in rendered


async def test_search_without_a_corpus_explains_itself_rather_than_returning_nothing():
    from app.services.workset_read import search

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await session.commit()

        result = await search(session, ws, user=user, query="anything")
        # An empty result set and "there is nothing to search" are different
        # answers, and the agent needs the second one.
        assert result.hits == []
        assert "no item has embedded content" in result.error
        assert "Coverage:" in result.error


# ── Follow-up fixes ────────────────────────────


async def test_a_borrowed_document_is_chunked_with_its_own_collections_settings():
    """A borrowed document was embedded under ITS OWN KB's parameters,
    not the workset corpus's — chunking it at 384/96 produced a list matching
    nothing, so the ordinal was fiction."""
    from app.services.chunking import chunk_text
    from app.services.workset_read import chunks_for_document

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        # A collection with the platform default chunking, deliberately
        # different from the workset corpus's 384/96.
        foreign_kb = KnowledgeBase(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Team wiki",
            chunk_size=512,
            chunk_overlap=128,
        )
        session.add(foreign_kb)
        await session.flush()
        content = "\n\n".join(f"Para {i}. " + ("word " * 200) for i in range(12))
        foreign_doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=foreign_kb.id,
            filename="wiki.md",
            source_type="text",
            content=content,
            status="ready",
        )
        session.add(foreign_doc)
        await session.flush()

        ws = await resolve_workset(session, user, key="reports", scope="global")
        borrowed = await add_item(
            session,
            ws,
            user,
            title="Wiki",
            knowledge_document_id=foreign_doc.id,
            external_ref="wiki-1",
        )
        owned = await add_item(
            session, ws, user, title="Owned", content=content, external_ref="own-1"
        )
        await session.commit()

        borrowed_chunks = await chunks_for_document(
            session, borrowed.item.knowledge_document_id
        )
        owned_chunks = await chunks_for_document(
            session, owned.item.knowledge_document_id
        )

        # Same text, two collections, two chunkings — and each is the one its
        # own collection would have embedded.
        assert borrowed_chunks.chunks == chunk_text(
            content, chunk_size=512, chunk_overlap=128
        )
        assert owned_chunks.chunks == chunk_text(
            content, chunk_size=384, chunk_overlap=96
        )
        assert borrowed_chunks.chunks != owned_chunks.chunks
        assert borrowed_chunks.exact and owned_chunks.exact


async def test_locating_a_hit_works_for_a_borrowed_document():
    """The read layer's headline affordance — widen a hit by whole chunks —
    silently did not work for referenced content."""
    from app.services.workset_read import _locate_chunk, chunks_for_document

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        foreign_kb = KnowledgeBase(
            id=uuid.uuid4(),
            user_id=user.id,
            name="Wiki",
            chunk_size=512,
            chunk_overlap=128,
        )
        session.add(foreign_kb)
        await session.flush()
        content = "\n\n".join(f"Para {i}. " + ("word " * 200) for i in range(10))
        doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=foreign_kb.id,
            filename="w.md",
            source_type="text",
            content=content,
            status="ready",
        )
        session.add(doc)
        await session.flush()

        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session,
            ws,
            user,
            title="Wiki",
            knowledge_document_id=doc.id,
            external_ref="wiki-1",
        )
        await session.commit()

        resolved = await chunks_for_document(session, doc.id)
        # Feed back a real embedded chunk, as a search hit would.
        found = await _locate_chunk(session, outcome.item, resolved.chunks[3])
        assert found == 3


async def test_approximate_chunking_is_declared_instead_of_asserted():
    """When the parameters cannot be established the window is still useful —
    but the ordinal must not be reported as if it were real."""
    from sqlalchemy import text as sa_text

    from app.services.workset_read import read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        kb = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Odd")
        session.add(kb)
        await session.flush()
        content = "\n\n".join(f"Para {i}. " + ("word " * 200) for i in range(8))
        doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=kb.id,
            filename="o.md",
            source_type="text",
            content=content,
            status="ready",
        )
        session.add(doc)
        await session.flush()
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session,
            ws,
            user,
            title="Odd",
            knowledge_document_id=doc.id,
            external_ref="odd-1",
        )
        await session.commit()

        # A collection with no recorded chunking — the one case the list cannot
        # be trusted to match what was embedded.
        await session.execute(
            sa_text(
                "UPDATE knowledge_bases SET chunk_size = 0, chunk_overlap = 0 "
                "WHERE id = :id"
            ),
            {"id": kb.id},
        )
        await session.commit()

        result = await read_item(
            session, ws, user=user, item_id=outcome.item.id, chunk_index=2,
            max_chars=20000,
        )

        assert result.error is None
        assert result.text, "the window is still returned — it is real text"
        # But nothing claims an ordinal that no embedding would agree with.
        assert result.chunk_index is None
        assert result.chunk_count == 0
        assert "APPROXIMATE ADDRESSING" in result.note


async def _borrowed_ready_document(session, user, *, chunk_size, chunk_overlap):
    """A document in a foreign collection, embedded under its collection's
    CURRENT parameters - the rows and counters the embed path leaves."""
    from app.models.embedding import Embedding
    from app.services.chunking import chunk_text

    kb = KnowledgeBase(
        id=uuid.uuid4(),
        user_id=user.id,
        name="Team wiki",
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    session.add(kb)
    await session.flush()
    content = "\n\n".join(f"Para {i}. " + ("word " * 200) for i in range(10))
    embedded = [
        chunk
        for chunk in chunk_text(
            content, chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
        if len(chunk.strip()) >= 10
    ]
    doc = KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=kb.id,
        filename="wiki.md",
        source_type="text",
        content=content,
        status="ready",
        chunk_count=len(embedded),
    )
    session.add(doc)
    await session.flush()
    for chunk in embedded:
        session.add(
            Embedding(
                id=uuid.uuid4(),
                user_id=user.id,
                knowledge_base_id=kb.id,
                source_type="document",
                source_id=str(doc.id),
                content=chunk,
                embedding=[0.1, 0.2, 0.3],
            )
        )
    await session.flush()
    return kb, doc


async def test_a_collection_rechunked_since_embedding_is_not_reported_as_exact():
    """The parameters are read from the collection as it is NOW; the
    built-in KB sync can change them under documents it does not re-embed.
    The recomputed list then matches no embedding, so no ordinal may be
    reported as if it were real."""
    from sqlalchemy import text as sa_text

    from app.services.workset_read import (
        _locate_chunk,
        chunks_for_document,
        read_item,
    )

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        kb, doc = await _borrowed_ready_document(
            session, user, chunk_size=512, chunk_overlap=128
        )
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session, ws, user, title="Wiki",
            knowledge_document_id=doc.id, external_ref="wiki-1",
        )
        await session.commit()

        # While the collection still has the parameters the document was
        # embedded under, addressing is exact.
        embedded = await chunks_for_document(session, doc.id)
        assert embedded.exact
        before = await read_item(
            session, ws, user=user, item_id=outcome.item.id, chunk_index=2,
            max_chars=20000,
        )
        assert before.chunk_index == 2
        assert before.text == embedded.chunks[2]

        # The collection is re-parameterised without re-embedding.
        await session.execute(
            sa_text(
                "UPDATE knowledge_bases SET chunk_size = 2048, chunk_overlap = 256 "
                "WHERE id = :id"
            ),
            {"id": kb.id},
        )
        await session.commit()

        assert not (await chunks_for_document(session, doc.id)).exact
        after = await read_item(
            session, ws, user=user, item_id=outcome.item.id, chunk_index=2,
            max_chars=20000,
        )
        assert after.error is None
        assert after.text, "the window is still real text"
        assert after.chunk_index is None
        assert after.chunk_count == 0
        assert "APPROXIMATE ADDRESSING" in after.note
        # A real embedded chunk (as a search hit returns it) is no longer
        # claimed to sit at an ordinal of a chunking that was never applied.
        assert await _locate_chunk(session, outcome.item, embedded.chunks[3]) is None


async def test_a_zero_overlap_collection_is_addressed_exactly():
    """An overlap of 0 is a real setting ("no overlap"), not a missing
    one: it used to be read as unknown and re-chunked at the workset's 96."""
    from app.services.chunking import chunk_text
    from app.services.workset_read import _locate_chunk, chunks_for_document

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        _kb, doc = await _borrowed_ready_document(
            session, user, chunk_size=512, chunk_overlap=0
        )
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session, ws, user, title="Wiki",
            knowledge_document_id=doc.id, external_ref="wiki-1",
        )
        await session.commit()

        resolved = await chunks_for_document(session, doc.id)
        assert resolved.exact
        assert resolved.chunks == chunk_text(
            doc.content, chunk_size=512, chunk_overlap=0
        )
        assert await _locate_chunk(session, outcome.item, resolved.chunks[3]) == 3


async def test_the_same_chunk_count_with_other_boundaries_is_not_exact():
    """Equal counts do not prove equal boundaries: the check compares the
    stored chunk texts, not how many there are."""
    from sqlalchemy import text as sa_text

    from app.services.workset_read import chunks_for_document

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        _kb, doc = await _borrowed_ready_document(
            session, user, chunk_size=512, chunk_overlap=128
        )
        await session.commit()
        assert (await chunks_for_document(session, doc.id)).exact

        # One stored chunk now ends somewhere else; the count is unchanged.
        await session.execute(
            sa_text(
                "UPDATE embeddings SET content = content || ' shifted' "
                "WHERE id = (SELECT id FROM embeddings WHERE source_id = :doc LIMIT 1)"
            ),
            {"doc": str(doc.id)},
        )
        await session.commit()
        assert not (await chunks_for_document(session, doc.id)).exact


def test_drift_is_judged_against_the_stored_chunk_texts():
    from app.services.workset_read import _chunks_match_embeddings

    chunks = ["a" * 50, "b" * 50, "tiny"]  # the short one is never embedded
    assert _chunks_match_embeddings(chunks, ["b" * 50, "a" * 50]) is True
    # Same count, different boundaries: a count alone would call this exact.
    assert _chunks_match_embeddings(chunks, ["a" * 40, "b" * 60]) is False
    # Partly embedded: no ordinal can be trusted either.
    assert _chunks_match_embeddings(chunks, ["a" * 50]) is False
    # Nothing stored (never embedded, or re-embedding in flight): not judged.
    assert _chunks_match_embeddings(chunks, []) is None


async def test_a_sealed_workset_with_failed_items_is_not_complete():
    """`describe()` mentioned the failures, but `complete` is what code
    branches on — and the module's thesis is that a truncated corpus must never
    look whole."""
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        ready = await add_item(
            session, ws, user, title="ok", content="A" * 200, external_ref="ok"
        )
        broken = await add_item(
            session, ws, user, title="bad", content="B" * 200, external_ref="bad"
        )
        ready.item.status = "ready"
        broken.item.status = "failed"
        ws.sealed_at = datetime.now(UTC)
        await session.commit()

        cov = await coverage_of(session, ws)
        assert cov.failed == 1
        assert cov.pending == 0
        # Nothing is still in flight...
        assert cov.settled is True
        # ...but the corpus is not whole, and the boolean says so.
        assert cov.complete is False
        assert "NOT complete" in cov.describe()


async def test_settled_and_complete_answer_different_questions():
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await add_item(
            session, ws, user, title="ok", content="A" * 200, external_ref="ok"
        )
        await session.commit()

        # Still processing: neither settled nor complete.
        cov = await coverage_of(session, ws)
        assert cov.settled is False
        assert cov.complete is False

        first.item.status = "ready"
        ws.sealed_at = datetime.now(UTC)
        await session.commit()
        cov = await coverage_of(session, ws)
        assert cov.settled is True
        assert cov.complete is True


# ── Derived state and the drain queue ────────────────────────────────────────


async def test_an_item_is_ready_only_once_it_has_both_derived_values():
    """A summary alone is not ready: the digest ranks by priority, and an
    unscored item sorts as never-judged."""
    from app.services.workset import set_priority, set_summary

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="R", content="A" * 200)
        await session.commit()

        await set_summary(session, ws, outcome.item.id, "The gist")
        await session.commit()
        assert outcome.item.status == "pending"
        assert outcome.item.summary_source == "agent"

        await set_priority(session, ws, outcome.item.id, 70, reason="corroborated")
        await session.commit()
        assert outcome.item.status == "ready"
        assert outcome.item.priority_score == 70
        assert outcome.item.priority_reason == "corroborated"


async def test_the_automatic_pass_never_overwrites_what_an_agent_wrote():
    """The precedence the design commits to, enforced rather than documented."""
    from app.services.workset import set_priority, set_summary

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="R", content="A" * 200)
        await session.commit()

        await set_summary(session, ws, outcome.item.id, "Agent's reading")
        await set_priority(session, ws, outcome.item.id, 90)
        await session.commit()

        # The background pass comes along afterwards and fills gaps only.
        await set_summary(
            session, ws, outcome.item.id, "Machine's reading",
            source="auto", overwrite=False,
        )
        await set_priority(
            session, ws, outcome.item.id, 10, source="auto", overwrite=False
        )
        await session.commit()

        assert outcome.item.summary == "Agent's reading"
        assert outcome.item.summary_source == "agent"
        assert outcome.item.priority_score == 90


async def test_the_automatic_pass_does_fill_an_empty_value():
    from app.services.workset import set_summary

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="R", content="A" * 200)
        await session.commit()

        await set_summary(
            session, ws, outcome.item.id, "Machine's reading",
            source="auto", overwrite=False,
        )
        await session.commit()
        assert outcome.item.summary == "Machine's reading"
        assert outcome.item.summary_source == "auto"


async def test_an_item_from_another_workset_is_not_writable():
    from app.services.workset import set_summary

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        mine = await resolve_workset(session, user, key="mine", scope="global")
        theirs = await resolve_workset(session, user, key="theirs", scope="global")
        outcome = await add_item(session, theirs, user, title="R", content="A" * 200)
        await session.commit()

        assert await set_summary(session, mine, outcome.item.id, "nope") is None
        await session.refresh(outcome.item)
        assert outcome.item.summary is None


async def test_claiming_takes_each_item_once():
    """K workers on one queue must never both take the same item."""
    from app.services.workset import claim_next_pending

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(3):
            await add_item(
                session,
                ws,
                user,
                title=f"r-{index}",
                content="A" * 200,
                external_ref=f"r-{index}",
                occurred_at=datetime.now(UTC) - timedelta(days=index),
            )
        await session.commit()

        claimed = []
        while True:
            item = await claim_next_pending(session, ws, agent_id=None)
            await session.commit()
            if item is None:
                break
            claimed.append(item.id)

        assert len(claimed) == 3
        assert len(set(claimed)) == 3, "an item was handed out twice"


async def test_claiming_processes_the_window_oldest_first():
    """A reporting window is processed in the order it happened, not the order
    it arrived."""
    from app.services.workset import claim_next_pending

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        now = datetime.now(UTC)
        # Added newest-first, deliberately.
        for label, age in (("newest", 1), ("middle", 5), ("oldest", 10)):
            await add_item(
                session,
                ws,
                user,
                title=label,
                content="A" * 200,
                external_ref=label,
                occurred_at=now - timedelta(days=age),
            )
        await session.commit()

        order = []
        for _ in range(3):
            item = await claim_next_pending(session, ws, agent_id=None)
            await session.commit()
            order.append(item.title)
        assert order == ["oldest", "middle", "newest"]


async def test_a_stale_claim_is_reclaimable():
    """A worker that died mid-item would otherwise park it forever, and the
    queue would stop draining while still reporting outstanding work."""
    from app.services.workset import claim_next_pending

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="R", content="A" * 200)
        await session.commit()

        first = await claim_next_pending(session, ws, agent_id=None)
        await session.commit()
        assert first is not None
        # Nothing else to take while the claim is fresh.
        assert await claim_next_pending(session, ws, agent_id=None) is None

        # The claim goes stale (the worker died).
        outcome.item.claimed_at = datetime.now(UTC) - timedelta(hours=1)
        await session.commit()

        again = await claim_next_pending(session, ws, agent_id=None)
        await session.commit()
        assert again is not None and again.id == first.id
        # Attempts are counted, which is what eventually makes it terminal.
        assert again.attempt_count == 2


async def test_a_sealed_workset_refuses_a_new_item_and_stays_complete() -> None:
    """Sealing makes "complete" a claim about a corpus that cannot grow: an item
    accepted after it would be invisible to every read that already called the
    corpus whole."""
    from app.services.workset import seal
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await add_item(session, ws, user, title="R", content="A" * 200, external_ref="r-1")
        assert first.item is not None
        first.item.status = "ready"
        await seal(session, ws)
        await session.commit()
        assert (await coverage_of(session, ws)).complete is True

        late = await add_item(session, ws, user, title="Late", content="B" * 200, external_ref="r-2")
        assert late.ok is False
        assert late.action == "refused"
        assert "is sealed" in (late.error or "")
        # A re-add of an existing item is a write too: its revision would
        # reset the derived state of a corpus already called whole.
        revised = await add_item(session, ws, user, title="R", content="C" * 200, external_ref="r-1")
        assert revised.action == "refused"
        await session.commit()

        count = await session.scalar(
            select(func.count()).select_from(WorksetItem).where(WorksetItem.workset_id == ws.id)
        )
        assert count == 1
        await session.refresh(first.item)
        assert first.item.status == "ready"
        assert (await coverage_of(session, ws)).complete is True


async def test_a_resolved_workset_refuses_a_new_item() -> None:
    """A resolved corpus is waiting for the retention sweep: an item written
    into it would be purged with it and its writer would never know."""
    from app.services.workset import resolve as resolve_status

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await resolve_status(session, ws)
        await session.commit()

        outcome = await add_item(session, ws, user, title="R", content="A" * 200)
        assert outcome.action == "refused"
        assert "is resolved" in (outcome.error or "")
        assert ws.item_count == 0
        assert ws.knowledge_base_id is None


async def test_a_failed_item_becomes_terminal_so_a_waiter_can_stop():
    from app.services.workset import claim_next_pending, fail_item
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="R", content="A" * 200)
        await session.commit()

        for attempt in range(1, 4):
            claimed = await claim_next_pending(session, ws, agent_id=None)
            assert claimed is not None, f"attempt {attempt} found nothing to claim"
            await fail_item(
                session, ws, claimed.id, error="provider exploded", max_attempts=3
            )
            await session.commit()
            claimed.claimed_at = None  # the next claim is not blocked by staleness

        await session.refresh(outcome.item)
        assert outcome.item.status == "failed"
        assert outcome.item.error_message == "provider exploded"

        ws.sealed_at = datetime.now(UTC)
        await session.commit()
        cov = await coverage_of(session, ws)
        # Nothing can progress — so a waiter stops...
        assert cov.settled is True
        # ...and the corpus is still not whole.
        assert cov.complete is False


async def test_discarding_keeps_the_row_and_the_reason():
    from app.services.workset import discard_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="Noise", content="A" * 200)
        await session.commit()

        await discard_item(
            session, ws, outcome.item.id, reason="marketing, not intelligence"
        )
        await session.commit()

        assert outcome.item.status == "discarded"
        assert outcome.item.error_message == "marketing, not intelligence"
        # Kept, not deleted: a corpus that forgets what it rejected cannot
        # explain its own coverage.
        count = await session.scalar(
            select(func.count()).select_from(WorksetItem).where(
                WorksetItem.workset_id == ws.id
            )
        )
        assert count == 1


async def test_sealing_is_idempotent_and_declaring_a_total_is_a_claim():
    from app.services.workset import declare_total, seal
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await declare_total(session, ws, 137, source="opencti query")
        await session.commit()

        assert ws.declared_total == 137
        assert ws.declared_total_source == "opencti query"

        await seal(session, ws)
        await session.commit()
        first_seal = ws.sealed_at
        await seal(session, ws)
        await session.commit()
        # The first close is the one that happened.
        assert ws.sealed_at == first_seal

        cov = await coverage_of(session, ws)
        # 137 announced, 0 present, sealed — reported as a gap, not reconciled.
        assert cov.complete is False
        assert "137" in cov.describe()


async def test_resolving_hands_the_workset_to_retention():
    from app.services.workset import resolve as resolve_status

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await session.commit()

        await resolve_status(session, ws, note="synthesis delivered")
        await session.commit()

        assert ws.status == "resolved"
        assert ws.resolved_at is not None
        assert "synthesis delivered" in ws.label


# ── Listing ──────────────────────────────────────────────────────────────────


async def test_listing_ranks_by_priority_and_never_returns_content():
    from app.services.workset_read import list_items

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for ref, score in (("low", 10), ("high", 95), ("mid", 50)):
            item = await _add_scored(session, ws, user, ref=ref, score=score)
            item.summary = f"Summary of {ref}"
        await session.commit()

        page = await list_items(session, ws)
        assert [i.title for i in page.items] == ["high", "mid", "low"]
        assert page.total == 3
        # Rows carry the summary, never the raw text.
        assert all(i.summary for i in page.items)
        assert not any("GGG" in (i.summary or "") for i in page.items)
        assert page.coverage.total == 3


async def test_listing_filters_on_the_documents_own_date():
    """A reporting window means the documents' dates, not the ingestion order."""
    from app.services.workset_read import list_items

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        now = datetime.now(UTC)
        for label, age in (("recent", 2), ("old", 40)):
            await add_item(
                session,
                ws,
                user,
                title=label,
                content="A" * 200,
                external_ref=label,
                occurred_at=now - timedelta(days=age),
            )
        await session.commit()

        window = await list_items(session, ws, since=now - timedelta(days=14))
        assert [i.title for i in window.items] == ["recent"]
        # `total` counts the FILTERED set, so a caller cannot mistake the
        # window for the whole corpus.
        assert window.total == 1
        assert window.coverage.total == 2


async def test_listing_returns_each_items_external_ref():
    """A scheduled agent skips what it already handled by matching the key it
    added the item with: the row it lists must carry that key, and the
    tool line must print it."""
    from app.services.builtin_tools.workset import list_item_line
    from app.services.workset_read import list_items

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        now = datetime.now(UTC)
        await add_item(
            session,
            ws,
            user,
            title="judged",
            content="A" * 200,
            external_ref="indicator--5467",
            occurred_at=now,
        )
        await add_item(session, ws, user, title="unkeyed", content="B" * 200, occurred_at=now)
        await session.commit()

        page = await list_items(session, ws, since=now - timedelta(days=1), sort="date")

    refs = {i.title: i.external_ref for i in page.items}
    assert refs == {"judged": "indicator--5467", "unkeyed": None}
    lines = {i.title: list_item_line(i) for i in page.items}
    assert lines["judged"].endswith(", ref=indicator--5467")
    assert "ref=" not in lines["unkeyed"]


async def test_listing_pages_without_repeating_or_skipping():
    from app.services.workset_read import list_items

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(7):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=index)
        await session.commit()

        first = await list_items(session, ws, limit=3, offset=0)
        second = await list_items(session, ws, limit=3, offset=3)
        third = await list_items(session, ws, limit=3, offset=6)
        seen = [i.item_id for i in first.items + second.items + third.items]
        assert len(seen) == 7
        assert len(set(seen)) == 7


# ── Progress and ETA ─────────────────────────────────────────────────────────


async def _complete_items(session, ws, count, *, seconds_each):
    """Mark *count* pending items processed, each taking *seconds_each*."""
    from app.services.workset import set_priority, set_summary

    items = (
        (
            await session.execute(
                select(WorksetItem)
                .where(
                    WorksetItem.workset_id == ws.id,
                    WorksetItem.status == "pending",
                )
                .limit(count)
            )
        )
        .scalars()
        .all()
    )
    base = datetime.now(UTC) - timedelta(minutes=5)
    for offset, item in enumerate(items):
        await set_summary(session, ws, item.id, f"summary {offset}")
        await set_priority(session, ws, item.id, 50)
        item.processing_started_at = base + timedelta(seconds=offset * 60)
        item.processing_completed_at = item.processing_started_at + timedelta(
            seconds=seconds_each
        )
    await session.commit()
    return items


async def test_no_eta_is_offered_before_three_completions():
    """A fabricated estimate is worse than none: the agent waits on it."""
    from app.services.workset_read import progress_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(10):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=None)
        await session.commit()

        await _complete_items(session, ws, 2, seconds_each=30)

        result = await progress_of(session, ws)
        assert result.samples == 2
        assert result.eta_seconds is None
        assert result.eta_confidence == "unknown"
        assert "No estimate yet" in result.describe()
        # Waiting is still legal — it just is not estimated.
        assert result.suggested_wait_seconds > 0


async def test_an_eta_appears_with_a_range_once_it_is_earned():
    from app.services.workset_read import progress_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(10):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=None)
        await session.commit()

        await _complete_items(session, ws, 4, seconds_each=30)

        result = await progress_of(session, ws)
        assert result.samples == 4
        assert result.pending == 6
        # 6 remaining at 30s each with one worker.
        assert result.eta_seconds == 180
        assert result.eta_range is not None
        low, high = result.eta_range
        assert low <= result.eta_seconds <= high
        # Four samples is "rough", not "stable".
        assert result.eta_confidence == "rough"
        assert "Estimated 180s" in result.describe()


async def test_the_eta_divides_by_the_worker_count():
    """Derived from concurrency, not from one item extrapolated."""
    from app.services.workset_read import progress_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(
            session, user, key="reports", scope="global", workers=3
        )
        for index in range(10):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=None)
        await session.commit()

        await _complete_items(session, ws, 4, seconds_each=30)

        result = await progress_of(session, ws)
        # 6 remaining × 30s ÷ 3 workers.
        assert result.eta_seconds == 60
        assert result.throughput_per_min == 6.0


async def test_a_suggested_wait_never_exceeds_what_one_wait_may_cover():
    """The agent is handed a number `wait` will accept, not one it will clamp."""
    from app.services.workset_read import progress_of
    from app.tools.wait_policy import DEFAULT_MAXIMUM_WAIT_TIME

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(200):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=None)
        await session.commit()

        await _complete_items(session, ws, 4, seconds_each=120)

        result = await progress_of(session, ws)
        assert result.eta_seconds is not None
        assert result.eta_seconds > DEFAULT_MAXIMUM_WAIT_TIME
        assert result.suggested_wait_seconds == DEFAULT_MAXIMUM_WAIT_TIME


async def test_a_stall_is_surfaced_and_stops_the_suggestion_to_wait():
    """A wedged processor must not read as "still working"."""
    from app.services.workset_read import progress_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(5):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=None)
        await session.commit()
        await _complete_items(session, ws, 3, seconds_each=30)

        # Nothing has completed in a long time, and work is outstanding.
        ws.last_progress_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()

        result = await progress_of(session, ws)
        assert result.stalled is True
        assert "STALLED" in result.describe()
        # And it stops telling the agent to keep waiting.
        assert result.suggested_wait_seconds == 0


async def test_progress_reports_a_declared_total_gap_without_reconciling_it():
    from app.services.workset import declare_total
    from app.services.workset_read import progress_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await declare_total(session, ws, 137, source="opencti query")
        for index in range(3):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=None)
        await session.commit()

        result = await progress_of(session, ws)
        assert result.declared_total == 137
        assert result.added == 3
        described = result.describe()
        assert "announced 137" in described
        assert "unexplained" in described


async def test_progress_distinguishes_settled_from_complete():
    from app.services.workset import seal
    from app.services.workset_read import progress_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(3):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=None)
        await session.commit()
        items = await _complete_items(session, ws, 3, seconds_each=30)
        items[0].status = "failed"
        await seal(session, ws)
        await session.commit()

        result = await progress_of(session, ws)
        assert result.pending == 0
        assert result.settled is True
        # One item will never be summarised, so the corpus is not whole.
        assert result.complete is False
        assert "failed permanently" in result.describe()
        assert "Nothing can still progress" in result.describe()


async def test_progress_accounts_for_every_added_item_evicted_included():
    """634 processed of 1202, with 568 evicted never named, did not add up."""
    from app.services.workset_read import progress_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(7):
            await _add_scored(session, ws, user, ref=f"r-{index}", score=None)
        await session.commit()
        items = await _complete_items(session, ws, 6, seconds_each=30)
        items[0].status = "failed"
        items[1].status = "evicted"
        items[2].status = "evicted"
        items[3].status = "discarded"
        await session.commit()

        result = await progress_of(session, ws)
        assert (result.ready, result.failed, result.evicted, result.discarded, result.pending) == (2, 1, 2, 1, 1)
        assert result.ready + result.failed + result.evicted + result.discarded + result.pending == result.added
        described = result.describe()
        assert (
            "Of the 7 added: 2 summarised, 1 failed, 2 evicted (summary kept, content reclaimed, "
            "not searchable), 1 discarded, 1 outstanding."
        ) in described
        # The counts the API and the waiters read are unchanged.
        assert result.processed == 4
        assert result.settled is False


# ── The REST surface ─────────────────────────────────────────────────
#
# The handlers are called directly rather than through a test client: they are
# thin over services already covered, and what is new is the visibility
# boundary, what a purge deletes, and what the endpoints refuse.


async def test_a_purge_deletes_the_owned_corpus_but_never_a_borrowed_document():
    """The invariant the whole by-reference design rests on."""
    from app.api.v1.worksets import _purge
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        foreign_kb = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Wiki")
        session.add(foreign_kb)
        await session.flush()
        foreign_doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=foreign_kb.id,
            filename="w.md",
            source_type="text",
            content="F" * 400,
            status="ready",
        )
        session.add(foreign_doc)
        await session.flush()

        ws = await resolve_workset(session, user, key="reports", scope="global")
        owned = await add_item(
            session, ws, user, title="Owned", content="A" * 400, external_ref="own"
        )
        await add_item(
            session,
            ws,
            user,
            title="Borrowed",
            knowledge_document_id=foreign_doc.id,
            external_ref="borrowed",
        )
        await session.commit()
        owned_document_id = owned.item.knowledge_document_id
        corpus_id = ws.knowledge_base_id

        await _purge(session, ws)
        await session.commit()

        # The workset, its items, its own document and its corpus are gone...
        assert await session.get(Workset, ws.id) is None
        assert await session.get(KnowledgeDocument, owned_document_id) is None
        assert await session.get(KnowledgeBase, corpus_id) is None
        remaining = await session.scalar(
            select(func.count()).select_from(WorksetItem)
        )
        assert remaining == 0
        # ...and the borrowed document, in someone else's collection, is not.
        survivor = await session.get(KnowledgeDocument, foreign_doc.id)
        assert survivor is not None
        assert survivor.content == "F" * 400
        assert await session.get(KnowledgeBase, foreign_kb.id) is not None


async def test_another_users_workset_is_not_reachable():
    from fastapi import HTTPException

    from app.api.v1.worksets import _load

    async with _temp_database() as session_factory, session_factory() as session:
        owner = await _seed_user(session)
        stranger = await _seed_user(session)
        ws = await resolve_workset(session, owner, key="private", scope="global")
        await session.commit()

        # The owner reaches it.
        assert (await _load(session, owner, str(ws.id))).id == ws.id
        # A stranger gets a 404, not a 403: existence is not theirs to learn.
        with pytest.raises(HTTPException) as caught:
            await _load(session, stranger, str(ws.id))
        assert caught.value.status_code == 404


async def test_a_company_managed_workset_is_visible_to_everyone():
    from app.api.v1.worksets import _load

    async with _temp_database() as session_factory, session_factory() as session:
        owner = await _seed_user(session)
        reader = await _seed_user(session)
        ws = await resolve_workset(
            session, owner, key="shared", scope="global", is_company_managed=True
        )
        await session.commit()
        assert (await _load(session, reader, str(ws.id))).id == ws.id


async def test_a_malformed_id_is_a_404_not_a_crash():
    from fastapi import HTTPException

    from app.api.v1.worksets import _load

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        with pytest.raises(HTTPException) as caught:
            await _load(session, user, "not-a-uuid")
        assert caught.value.status_code == 404


async def test_the_detail_endpoint_shows_what_the_agents_see():
    """An operator answering "what did it collect, and what did it leave out?"
    must not be shown a rosier picture than the agents get."""
    from app.api.v1.worksets import get_workset

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        ws.declared_total = 5
        await add_item(
            session, ws, user, title="R", content="A" * 400, external_ref="r-1"
        )
        await session.commit()

        payload = await get_workset(str(ws.id), user=user, db=session)
        assert payload["key"] == "reports"
        coverage = payload["coverage"]
        # Unsealed, so never complete — the same conservative answer the tools
        # give, and the declared-total gap is surfaced too.
        assert coverage["sealed"] is False
        assert coverage["complete"] is False
        assert "NOT sealed" in coverage["description"]
        assert "announced 5" in payload["progress"]["description"]
        # No estimate has been earned yet, and it says so rather than guessing.
        assert payload["progress"]["eta_confidence"] == "unknown"
        assert payload["progress"]["eta_seconds"] is None


async def test_the_items_endpoint_returns_summaries_not_source_text():
    """Reading a document verbatim is the agent tools' job, under their bounds.
    A REST endpoint streaming raw ingested content would be a much wider
    surface than this needs."""
    from app.api.v1.worksets import list_workset_items

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session,
            ws,
            user,
            title="A report",
            content="SECRETCONTENT" * 40,
            external_ref="r-1",
            summary="The gist",
            priority=70,
        )
        await session.commit()

        payload = await list_workset_items(str(ws.id), user=user, db=session)
        assert payload["total"] == 1
        row = payload["items"][0]
        assert row["id"] == str(outcome.item.id)
        assert row["summary"] == "The gist"
        assert row["priority_score"] == 70
        # The size is reported; the text is not.
        assert row["chars"] > 0
        assert "SECRETCONTENT" not in str(payload)


async def test_an_unfiltered_bulk_delete_is_refused():
    """"Delete everything visible" is never what someone means, and a corpus
    mid-run is somebody's job."""
    from fastapi import HTTPException

    from app.api.v1.worksets import WorksetBulkDeleteRequest, bulk_delete_worksets

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        await resolve_workset(session, user, key="live", scope="global")
        await session.commit()

        with pytest.raises(HTTPException) as caught:
            await bulk_delete_worksets(
                WorksetBulkDeleteRequest(), user=user, db=session
            )
        assert caught.value.status_code == 400
        assert "rather than deleting everything" in str(caught.value.detail)
        # And nothing was touched.
        assert await session.scalar(select(func.count()).select_from(Workset)) == 1


async def test_bulk_delete_can_target_resolved_worksets():
    from app.api.v1.worksets import WorksetBulkDeleteRequest, bulk_delete_worksets
    from app.services.workset import resolve as resolve_status

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        done = await resolve_workset(session, user, key="done", scope="global")
        await resolve_workset(session, user, key="live", scope="global")
        await resolve_status(session, done, note="delivered")
        await session.commit()

        result = await bulk_delete_worksets(
            WorksetBulkDeleteRequest(resolved_only=True), user=user, db=session
        )
        assert result["deleted"] == 1
        remaining = (await session.execute(select(Workset.key))).scalars().all()
        assert remaining == ["live"]


async def test_resolving_through_the_api_records_the_note():
    from app.api.v1.worksets import WorksetResolveRequest, resolve_workset_endpoint

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        await session.commit()

        payload = await resolve_workset_endpoint(
            str(ws.id),
            WorksetResolveRequest(note="synthesis delivered"),
            user=user,
            db=session,
        )
        assert payload["status"] == "resolved"
        assert payload["resolved_at"] is not None
        assert "synthesis delivered" in payload["label"]


# ── Visibility is not permission ──────────────────────────────
#
# The first cut used the read predicate as the only gate, so any user who could
# SEE a company-managed corpus could resolve or purge it. These pin the rule the
# work-item API already applies: readable by everyone, writable by admins.


async def _seed_company_workset(session):
    """A fleet-wide corpus, plus a non-admin who can see but not touch it."""
    admin = await _seed_user(session)
    admin.role = "admin"
    member = await _seed_user(session)
    ws = await resolve_workset(
        session, admin, key="fleet", scope="global", is_company_managed=True
    )
    await add_item(
        session, ws, admin, title="R", content="A" * 400, external_ref="r-1"
    )
    await session.commit()
    return admin, member, ws


async def test_a_member_can_read_a_company_managed_workset():
    from app.api.v1.worksets import get_workset

    async with _temp_database() as session_factory, session_factory() as session:
        _admin, member, ws = await _seed_company_workset(session)
        payload = await get_workset(str(ws.id), user=member, db=session)
        assert payload["id"] == str(ws.id)
        assert payload["is_company_managed"] is True


async def test_a_member_cannot_resolve_a_company_managed_workset():
    from fastapi import HTTPException

    from app.api.v1.worksets import WorksetResolveRequest, resolve_workset_endpoint

    async with _temp_database() as session_factory, session_factory() as session:
        admin, member, ws = await _seed_company_workset(session)

        with pytest.raises(HTTPException) as caught:
            await resolve_workset_endpoint(
                str(ws.id), WorksetResolveRequest(), user=member, db=session
            )
        assert caught.value.status_code == 403
        assert "Only admins can resolve" in str(caught.value.detail)
        await session.refresh(ws)
        assert ws.status == "active"

        # The admin can.
        await resolve_workset_endpoint(
            str(ws.id), WorksetResolveRequest(), user=admin, db=session
        )
        await session.refresh(ws)
        assert ws.status == "resolved"


async def test_a_member_cannot_delete_a_company_managed_workset():
    from fastapi import HTTPException

    from app.api.v1.worksets import delete_workset

    async with _temp_database() as session_factory, session_factory() as session:
        _admin, member, ws = await _seed_company_workset(session)

        with pytest.raises(HTTPException) as caught:
            await delete_workset(str(ws.id), user=member, db=session)
        assert caught.value.status_code == 403
        # And nothing was purged on the way to the refusal.
        assert await session.get(Workset, ws.id) is not None
        items = await session.scalar(
            select(func.count()).select_from(WorksetItem)
        )
        assert items == 1


async def test_bulk_delete_refuses_rather_than_skipping_what_is_not_yours():
    """Reporting "deleted 1" for a request that named 2 would hide the one that
    was not the caller's to purge."""
    from fastapi import HTTPException

    from app.api.v1.worksets import WorksetBulkDeleteRequest, bulk_delete_worksets

    async with _temp_database() as session_factory, session_factory() as session:
        _admin, member, fleet = await _seed_company_workset(session)
        mine = await resolve_workset(session, member, key="mine", scope="global")
        await session.commit()

        with pytest.raises(HTTPException) as caught:
            await bulk_delete_worksets(
                WorksetBulkDeleteRequest(ids=[str(fleet.id), str(mine.id)]),
                user=member,
                db=session,
            )
        assert caught.value.status_code == 403
        assert "company-managed" in str(caught.value.detail)
        # Neither was deleted — including the one the member does own.
        assert await session.scalar(select(func.count()).select_from(Workset)) == 2


async def test_a_member_can_bulk_delete_their_own():
    from app.api.v1.worksets import WorksetBulkDeleteRequest, bulk_delete_worksets

    async with _temp_database() as session_factory, session_factory() as session:
        member = await _seed_user(session)
        mine = await resolve_workset(session, member, key="mine", scope="global")
        await session.commit()

        result = await bulk_delete_worksets(
            WorksetBulkDeleteRequest(ids=[str(mine.id)]), user=member, db=session
        )
        assert result["deleted"] == 1


async def test_resolved_only_bulk_delete_still_spares_fleet_corpora():
    """The `resolved_only` path reaches rows the caller never named, so it needs
    the same guard as an explicit id list."""
    from fastapi import HTTPException

    from app.api.v1.worksets import WorksetBulkDeleteRequest, bulk_delete_worksets
    from app.services.workset import resolve as resolve_status

    async with _temp_database() as session_factory, session_factory() as session:
        admin, member, fleet = await _seed_company_workset(session)
        await resolve_status(session, fleet, note="done")
        await session.commit()
        del admin

        with pytest.raises(HTTPException) as caught:
            await bulk_delete_worksets(
                WorksetBulkDeleteRequest(resolved_only=True),
                user=member,
                db=session,
            )
        assert caught.value.status_code == 403
        assert await session.get(Workset, fleet.id) is not None


# ── Follow-up fixes ────────────────────────────────────


def test_two_scopes_differing_past_the_key_limit_are_not_one_corpus():
    """The stored key is capped at 200 characters and the composed key
    put the DISCRIMINATOR at the end, so two concurrent runs of a flow with a
    long path shared a corpus — silently, with no error, just another run's
    documents in the analysis window."""
    from app.services.workset import _scope_key

    shared_prefix = "flows/threat-intel/biweekly-report/" + "x" * 200
    first = _scope_key("run", f"{shared_prefix}/run-aaaaaaaa", "reports")
    second = _scope_key("run", f"{shared_prefix}/run-bbbbbbbb", "reports")

    assert len(first) <= 200 and len(second) <= 200
    assert first != second, "two runs would share one corpus"
    # The head stays readable — the key is a thing operators grep for.
    assert first.startswith("run:flows/threat-intel/biweekly-report/")


async def test_two_long_source_ids_do_not_dedup_against_each_other():
    """The other truncated key: `external_ref` is the dedup
    key, so a collision means one source silently overwriting another."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")

        prefix = "https://intel.example.test/reports/" + "y" * 200
        first = await add_item(
            session, ws, user, title="First", content="A" * 200,
            external_ref=f"{prefix}/report-0001",
        )
        second = await add_item(
            session, ws, user, title="Second", content="B" * 200,
            external_ref=f"{prefix}/report-0002",
        )
        await session.commit()

        assert first.action == "created"
        assert second.action == "created", "the second source deduped onto the first"
        assert first.item.id != second.item.id
        assert ws.item_count == 2


async def test_a_declared_total_is_held_to_the_same_standard_as_a_score():
    """`int(True)` is 1, so a tool call carrying `{"total": true}` used
    to record a declared total of 1 — a number nobody claimed, corrupting the
    one diagnostic that says an ingestion lost something."""
    from app.services.workset import declare_total

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")

        await declare_total(session, ws, True)
        assert ws.declared_total is None, "a JSON true became a declared total"
        await declare_total(session, ws, False)
        assert ws.declared_total is None

        # Everything else keeps behaving exactly as it did.
        await declare_total(session, ws, 12.7)
        assert ws.declared_total == 12
        await declare_total(session, ws, "nope")
        assert ws.declared_total == 12
        await declare_total(session, ws, 137, source="the query")
        assert ws.declared_total == 137
        assert ws.declared_total_source == "the query"


async def test_an_agent_can_recover_an_item_the_automatic_pass_failed():
    """A `failed` item is "the automatic path gave up", not "beyond repair" —
    every failure message tells the agent to write the two values by hand, and
    doing so has to actually finish the item."""
    from app.services.workset import fail_item, set_priority, set_summary
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(session, ws, user, title="R", content="A" * 200)
        await session.commit()

        await fail_item(
            session, ws, outcome.item.id, error="provider exploded",
            max_attempts=1,
        )
        assert outcome.item.status == "failed"

        await set_summary(session, ws, outcome.item.id, "I read it myself.")
        assert outcome.item.status == "failed", "a summary alone is not ready"
        await set_priority(session, ws, outcome.item.id, 70, reason="mine")
        assert outcome.item.status == "ready"
        assert outcome.item.error_message is None

        ws.sealed_at = datetime.now(UTC)
        await session.commit()
        assert (await coverage_of(session, ws)).complete is True


# ── a borrowed document is re-authorized on every read ───────────────


async def _borrowed_setup(session):
    """A workset holding one borrowed document from a collection of its own."""
    user = await _seed_user(session)
    foreign_kb = KnowledgeBase(
        id=uuid.uuid4(), user_id=user.id, name="Team wiki",
        chunk_size=512, chunk_overlap=128,
    )
    session.add(foreign_kb)
    await session.flush()
    foreign_doc = KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=foreign_kb.id,
        filename="wiki.md",
        source_type="text",
        content="The incident began on a Tuesday. " * 40,
        status="ready",
    )
    session.add(foreign_doc)
    await session.flush()

    ws = await resolve_workset(session, user, key="reports", scope="global")
    borrowed = await add_item(
        session, ws, user, title="Wiki page",
        knowledge_document_id=foreign_doc.id, external_ref="wiki-1",
        summary="What the wiki said.",
    )
    await session.commit()
    return user, ws, borrowed.item, foreign_kb


async def test_losing_access_to_a_borrowed_collection_stops_its_content(
):
    """Authorization at ingest is a SNAPSHOT. Unshare the collection and
    the workset was still a readable copy of content its reader could no longer
    open anywhere else."""
    from app.services.workset_read import list_items, read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user, ws, item, foreign_kb = await _borrowed_setup(session)

        # Readable while the collection is still theirs.
        before = await read_item(session, ws, user=user, item_id=item.id)
        assert "The incident began" in before.text

        # The collection moves to someone else — the shape an unshare takes.
        stranger = await _seed_user(session)
        foreign_kb.user_id = stranger.id
        await session.commit()

        after = await read_item(session, ws, user=user, item_id=item.id)
        assert "The incident began" not in after.text
        assert "CONTENT OUT OF REACH" in (after.note or "")
        # The analysis outlives the content — same shape as an eviction.
        assert after.text == "What the wiki said."

        page = await list_items(session, ws)
        assert [row.summary for row in page.items] == ["What the wiki said."]
        assert page.coverage.total == 1


async def test_a_revoked_borrowed_document_is_not_searched_either(monkeypatch):
    """The same revocation, on the other read. Search must not return passages
    from a collection the caller can no longer open."""
    from app.services import workset_read

    async with _temp_database() as session_factory, session_factory() as session:
        user, ws, _item, foreign_kb = await _borrowed_setup(session)
        stranger = await _seed_user(session)
        foreign_kb.user_id = stranger.id
        await session.commit()

        scope = await workset_read._search_scope(session, ws, user)
        assert scope.document_ids == [], "a revoked document stayed in scope"
        assert scope.withheld == 1

        result = await workset_read.search(
            session, ws, user=user, query="incident"
        )
        # Not silence: an empty result that says nothing reads as "nothing
        # matches", and that is the failure this module exists to refuse.
        assert result.hits == []
        assert "no longer open" in (result.error or "")


async def test_an_owned_document_is_not_re_authorized_against_its_own_corpus():
    """The workset's own corpus is INTERNAL, so a visibility check would refuse
    it for everyone including its owner. Owned content is authorized by the
    workset — this pins that the re-check applies to borrowed items only."""
    from app.services.workset_read import read_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session, ws, user, title="Owned", content="Verbatim content here. " * 20
        )
        await session.commit()

        result = await read_item(session, ws, user=user, item_id=outcome.item.id)
        assert "Verbatim content here." in result.text
        assert result.note is None or "OUT OF REACH" not in result.note


# ── the ownership filter runs IN the query ───────────────────────────


def _stub_search(monkeypatch, captured: dict, results: list | None = None):
    """Point search() at a fake embedder and capture the query it builds."""

    class _Provider:
        async def embed(self, text, model=None):
            del text, model
            return [0.1, 0.2, 0.3]

    async def _provider(db):
        del db
        return _Provider(), "test-embed", None

    async def _hybrid(db, **kwargs):
        del db
        captured.update(kwargs)
        return list(results or [])

    monkeypatch.setattr("app.llm.router.get_embedding_provider", _provider)
    monkeypatch.setattr(
        "app.services.hybrid_search.hybrid_search_embeddings", _hybrid
    )


async def test_search_names_this_worksets_documents_in_the_query(monkeypatch):
    """The ownership filter used to run in Python AFTER an over-fetch,
    so on a large borrowed collection the whole fused pool could be documents
    the workset does not own — every one dropped, and an empty hit list with no
    error, indistinguishable from "nothing matches"."""
    from app.services.workset_read import search

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        # A big shared collection, of which the workset borrows exactly one.
        wiki = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Team wiki")
        session.add(wiki)
        await session.flush()
        documents = []
        for index in range(20):
            doc = KnowledgeDocument(
                id=uuid.uuid4(),
                knowledge_base_id=wiki.id,
                filename=f"page-{index}.md",
                source_type="text",
                content=f"Page {index}. " + ("word " * 60),
                status="ready",
            )
            session.add(doc)
            documents.append(doc)
        await session.flush()

        ws = await resolve_workset(session, user, key="reports", scope="global")
        borrowed = await add_item(
            session, ws, user, title="One page",
            knowledge_document_id=documents[3].id, external_ref="p3",
        )
        await session.commit()

        captured: dict = {}
        _stub_search(monkeypatch, captured)
        await search(session, ws, user=user, query="word")

        assert str(wiki.id) in captured["knowledge_base_ids"]
        # The restriction is IN the query, and names only what this workset
        # holds — not the other nineteen pages of the collection.
        assert captured["source_ids"] == [
            str(borrowed.item.knowledge_document_id)
        ]
        # And with the filter pushed down, `top_k` is a count of rows that can
        # actually be returned rather than a guess to be thinned afterwards.
        assert captured["top_k"] == 10


async def test_a_priority_filter_that_hides_every_match_says_so(monkeypatch):
    """The one predicate that cannot be pushed into the query lives on
    `workset_items`. When it drops everything, the result must not look like an
    empty corpus."""
    from app.services.hybrid_search import HybridResult
    from app.services.workset_read import search

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        outcome = await add_item(
            session, ws, user, title="Low", content="A" * 400, priority=10
        )
        await session.commit()

        captured: dict = {}
        _stub_search(
            monkeypatch,
            captured,
            [
                HybridResult(
                    id=uuid.uuid4(),
                    content="A" * 100,
                    source_type="document",
                    source_id=str(outcome.item.knowledge_document_id),
                    knowledge_base_id=ws.knowledge_base_id,
                    rrf_score=0.5,
                )
            ],
        )
        result = await search(
            session, ws, user=user, query="anything", min_priority=80
        )

        assert result.hits == []
        assert result.error is None
        assert "DID match" in (result.note or "")
        assert "min_priority=80" in result.note


# ── the retention sweep ──────────────────────────────────────────────


async def test_the_sweep_purges_a_resolved_corpus_but_spares_a_live_one(
    monkeypatch,
):
    """The platform backstop: a workset is the heaviest coordination primitive,
    so one nobody purged is storage, not a stale row."""
    from app.services.workset import resolve as resolve_status
    from app.services.workset import sweep_worksets

    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            done = await resolve_workset(session, user, key="done", scope="global")
            collected = await add_item(session, done, user, title="R", content="A" * 200)
            assert collected.item is not None
            live = await resolve_workset(session, user, key="live", scope="global")
            await add_item(session, live, user, title="R", content="B" * 200)
            await resolve_status(session, done)
            # Backdated together, in their real order: collected, then
            # resolved. An item newer than the resolution would mean the
            # corpus is still written to, which the sweep spares.
            collected.item.created_at = datetime.now(UTC) - timedelta(days=31)
            done.resolved_at = datetime.now(UTC) - timedelta(days=30)
            await session.commit()
            done_kb, live_id = done.knowledge_base_id, live.id

        purged = await sweep_worksets(resolved_after=timedelta(days=7))
        assert purged == {"resolved": 1, "expired": 0}

        async with session_factory() as session:
            assert await session.get(Workset, live_id) is not None
            # The corpus went with it — documents, chunks and the internal KB.
            assert await _documents(session, done_kb) == []
            assert (
                await session.scalar(
                    select(func.count()).select_from(KnowledgeBase).where(
                        KnowledgeBase.id == done_kb
                    )
                )
                == 0
            )


async def test_the_sweep_spares_a_resolved_corpus_written_after_resolution() -> None:
    """A resolved workset that took an item after it was resolved is still in
    use: deleting it would pull a live corpus from under its writer, and the
    next write would recreate it cold. Retention 0 included."""
    from app.services.workset import resolve as resolve_status
    from app.services.workset import sweep_worksets

    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            used = await resolve_workset(session, user, key="used", scope="global")
            collected = await add_item(session, used, user, title="R", content="A" * 200)
            assert collected.item is not None
            await resolve_status(session, used)
            collected.item.created_at = datetime.now(UTC) - timedelta(days=31)
            used.resolved_at = datetime.now(UTC) - timedelta(days=30)
            # A write that landed after the resolution: the guard refuses it
            # today, so it is written the way an older build or a bypassing
            # path would have left it.
            session.add(
                WorksetItem(
                    id=uuid.uuid4(),
                    workset_id=used.id,
                    title="late",
                    source_type="inline",
                    status="pending",
                    created_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
            await session.commit()
            used_id = used.id

        purged = await sweep_worksets(resolved_after=timedelta(0))
        assert purged == {"resolved": 0, "expired": 0}
        async with session_factory() as session:
            assert await session.get(Workset, used_id) is not None


async def test_the_sweep_purges_an_expired_corpus_nobody_resolved(monkeypatch):
    """The TTL is the half that carries the feature: nothing resolves a workset
    when its run fails halfway, and a run-scoped corpus outliving its run is
    exactly the leak it prevents."""
    from app.services.workset import sweep_worksets

    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            abandoned = await resolve_workset(
                session, user, key="abandoned", scope="run", scope_ref="run-1",
                expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
            await add_item(session, abandoned, user, title="R", content="A" * 200)
            forever = await resolve_workset(
                session, user, key="recurring", scope="flow", scope_ref="flow-1"
            )
            await session.commit()
            abandoned_id, forever_id = abandoned.id, forever.id

        purged = await sweep_worksets(resolved_after=timedelta(days=7))
        assert purged == {"resolved": 0, "expired": 1}

        async with session_factory() as session:
            assert await session.get(Workset, abandoned_id) is None
            # An ACTIVE workset with no expiry is never swept: it may be the
            # corpus of a fortnightly job, and deleting it on inactivity would
            # silently make the next run start cold.
            assert await session.get(Workset, forever_id) is not None


async def test_the_sweep_never_deletes_a_borrowed_document(monkeypatch):
    """The purge takes the corpus a workset OWNS. A referenced document belongs
    to somebody's collection and only ever had a pointer to it."""
    from app.services.workset import sweep_worksets

    async with _temp_database() as session_factory:
        async with session_factory() as session:
            _user, ws, _item, _kb = await _borrowed_setup(session)
            ws.expires_at = datetime.now(UTC) - timedelta(hours=1)
            await session.commit()
            document_id = _item.knowledge_document_id

        assert await sweep_worksets(resolved_after=None) == {
            "resolved": 0, "expired": 1,
        }

        async with session_factory() as session:
            survivor = await session.get(KnowledgeDocument, document_id)
            assert survivor is not None, "the sweep took someone else's document"
            assert "The incident began" in survivor.content


async def test_an_evicted_item_does_not_make_a_corpus_incomplete():
    """The other half of the completeness rule, pinned as a DECISION rather
    than left an accident of the predicate: eviction is a deliberate, budgeted
    trade whose summary survives and which every read already names, so it
    does not count against `complete`. `failed` is unintended loss with nothing surviving, and does."""
    from app.services.workset_read import coverage_of

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        kept = await add_item(
            session, ws, user, title="ok", content="A" * 200, external_ref="ok"
        )
        reclaimed = await add_item(
            session, ws, user, title="old", content="B" * 200, external_ref="old"
        )
        kept.item.status = "ready"
        reclaimed.item.status = "ready"
        reclaimed.item.summary = "What it said, before its content went."
        reclaimed.item.status = "evicted"
        ws.sealed_at = datetime.now(UTC)
        await session.commit()

        cov = await coverage_of(session, ws)
        assert cov.evicted == 1
        assert cov.complete is True
        # It is not silent about it either — the coverage line says the content
        # is gone and that the item is no longer searchable.
        assert "NOT searchable" in cov.describe()
