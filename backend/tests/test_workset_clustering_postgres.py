"""Near-duplicate clustering and the corroboration score.

A real database, because what is under test is arithmetic over stored vectors
and the state it writes back. The vectors are hand-made rather than embedded:
the point is not whether an embedding model thinks two reports are alike, it is
what this does once it has decided — which items cluster, who the
representative is, how the score is composed, and that running it twice does
not compound anything.

The columns these fill (``cluster_id``, ``duplicate_of``,
``corroboration_count``) shipped with the data model and nothing computed them,
so a digest rendered "[corroborated by N]" that could never appear and the
model's comment promised a salience signal the platform did not have.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper
from app.models.embedding import Embedding
from app.models.user import User
from app.models.workset import WorksetItem
from app.services.workset import add_item, resolve_workset
from app.services.workset_clustering import (
    CORROBORATION_MAX,
    CORROBORATION_PER_SOURCE,
    recompute_clusters,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_DIMENSIONS = 8


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("wsclus", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_user(session) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"clus-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Cluster Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    return user


def _direction(angle: float) -> list[float]:
    """A unit vector at *angle* radians in the first two dimensions.

    Two vectors an angle θ apart have cosine similarity cos(θ), so a test can
    ask for "0.99 alike" or "0.2 alike" and mean it.
    """
    return [math.cos(angle), math.sin(angle)] + [0.0] * (_DIMENSIONS - 2)


async def _seed_item(session, workset, user, *, title, angle, **kwargs):
    """One item whose single chunk sits at *angle* in the vector space."""
    outcome = await add_item(
        session, workset, user, title=title, content=f"{title}. " + "word " * 60,
        external_ref=title.lower().replace(" ", "-"), **kwargs,
    )
    session.add(
        Embedding(
            id=uuid.uuid4(),
            user_id=user.id,
            knowledge_base_id=workset.knowledge_base_id,
            source_type="document",
            source_id=str(outcome.item.knowledge_document_id),
            content=title,
            embedding=_direction(angle),
            embedding_dimensions=_DIMENSIONS,
            embedding_model="test-embed",
        )
    )
    await session.flush()
    return outcome.item


async def test_two_reports_of_one_incident_corroborate_each_other():
    """The motivating case: a two-week window covers the same incident from
    several sources, and what several sources report is more trustworthy than
    one model's read of one document."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        # Two vendors on one incident, and one unrelated advisory.
        first = await _seed_item(session, ws, user, title="Vendor A", angle=0.0)
        second = await _seed_item(
            session, ws, user, title="Vendor B", angle=0.05
        )
        other = await _seed_item(session, ws, user, title="Patch notes", angle=1.2)
        for item, score in ((first, 60), (second, 55), (other, 60)):
            item.priority_score = score
            item.priority_source = "auto"
            item.priority_components = {"llm": score}
            item.summary = "…"
            item.status = "ready"
        await session.commit()

        outcome = await recompute_clusters(session, ws)
        await session.commit()

        assert outcome.clusters == 1
        assert outcome.corroborated_items == 2

        assert first.corroboration_count == 1
        assert second.corroboration_count == 1
        assert first.cluster_id is not None
        assert first.cluster_id == second.cluster_id
        # The unrelated one is untouched, which is the half that makes the
        # signal mean anything.
        assert other.corroboration_count == 0
        assert other.cluster_id is None
        assert other.priority_score == 60

        # The parts stay separate rather than folded into one opaque number.
        assert first.priority_components == {
            "llm": 60, "corroboration": CORROBORATION_PER_SOURCE
        }
        assert first.priority_score == 60 + CORROBORATION_PER_SOURCE
        # …and a corroborated item now outranks an equally-scored solitary one.
        assert first.priority_score > other.priority_score


async def test_the_representative_is_the_copy_worth_reading():
    """`duplicate_of` points at one member of the cluster, and it should be the
    one an analyst would rather open."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        thin = await _seed_item(session, ws, user, title="Wire copy", angle=0.0)
        full = await _seed_item(session, ws, user, title="Full report", angle=0.02)
        thin.priority_score, thin.priority_source = 30, "auto"
        thin.priority_components = {"llm": 30}
        full.priority_score, full.priority_source = 80, "auto"
        full.priority_components = {"llm": 80}
        await session.commit()

        await recompute_clusters(session, ws)
        await session.commit()

        assert full.duplicate_of is None, "the best-scored copy is the original"
        assert thin.duplicate_of == full.id
        assert thin.cluster_id == full.cluster_id


async def test_an_agents_own_score_is_recorded_but_never_overruled():
    """The same precedence the automatic summariser follows: a machine does not
    replace an agent's reading of a document. The corroboration is still
    reported — it is a fact about the corpus — the SCORE is what is left
    alone."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        judged = await _seed_item(session, ws, user, title="Judged", angle=0.0)
        machine = await _seed_item(session, ws, user, title="Machine", angle=0.03)
        judged.priority_score, judged.priority_source = 40, "agent"
        judged.priority_components = {"agent": 40}
        machine.priority_score, machine.priority_source = 40, "auto"
        machine.priority_components = {"llm": 40}
        await session.commit()

        await recompute_clusters(session, ws)
        await session.commit()

        assert judged.corroboration_count == 1
        assert judged.priority_components["corroboration"] == (
            CORROBORATION_PER_SOURCE
        )
        assert judged.priority_score == 40, "an agent's number was overruled"
        assert machine.priority_score == 40 + CORROBORATION_PER_SOURCE


async def test_running_the_pass_twice_does_not_compound_the_bonus():
    """The base is read back out of the components, not off the stored score —
    otherwise every re-run would inflate the corpus."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await _seed_item(session, ws, user, title="A", angle=0.0)
        second = await _seed_item(session, ws, user, title="B", angle=0.02)
        for item in (first, second):
            item.priority_score, item.priority_source = 50, "auto"
            item.priority_components = {"llm": 50}
        await session.commit()

        await recompute_clusters(session, ws)
        await session.commit()
        once = first.priority_score
        first_cluster = first.cluster_id

        await recompute_clusters(session, ws)
        await session.commit()

        assert first.priority_score == once
        # And the cluster keeps its identity, so anything that cited it still
        # names the same group.
        assert first.cluster_id == first_cluster


async def test_a_corroboration_that_is_no_longer_true_is_taken_back():
    """An item whose twin is discarded stops being corroborated, and its score
    goes back to what the summariser gave it."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        kept = await _seed_item(session, ws, user, title="Kept", angle=0.0)
        twin = await _seed_item(session, ws, user, title="Twin", angle=0.02)
        for item in (kept, twin):
            item.priority_score, item.priority_source = 50, "auto"
            item.priority_components = {"llm": 50}
        await session.commit()

        await recompute_clusters(session, ws)
        await session.commit()
        assert kept.corroboration_count == 1

        twin.status = "discarded"
        await session.commit()
        await recompute_clusters(session, ws)
        await session.commit()

        assert kept.corroboration_count == 0
        assert kept.cluster_id is None
        assert kept.duplicate_of is None
        assert kept.priority_score == 50


async def test_corroboration_ranks_within_a_band_rather_than_dominating_it():
    """Five copies of a trivial notice must not outrank one report of a
    breach, so the bonus is capped."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        crowd = [
            await _seed_item(
                session, ws, user, title=f"Copy {index}", angle=0.01 * index
            )
            for index in range(6)
        ]
        for item in crowd:
            item.priority_score, item.priority_source = 20, "auto"
            item.priority_components = {"llm": 20}
        breach = await _seed_item(session, ws, user, title="Breach", angle=1.5)
        breach.priority_score, breach.priority_source = 95, "auto"
        breach.priority_components = {"llm": 95}
        await session.commit()

        await recompute_clusters(session, ws)
        await session.commit()

        assert crowd[0].corroboration_count == 5
        assert crowd[0].priority_components["corroboration"] == CORROBORATION_MAX
        assert crowd[0].priority_score == 20 + CORROBORATION_MAX
        assert breach.priority_score > crowd[0].priority_score


async def test_an_unscored_item_records_the_count_without_inventing_a_score():
    """Clustering never reads a document. With nothing to add the bonus to,
    inventing a number would be this module scoring text it never saw."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await _seed_item(session, ws, user, title="A", angle=0.0)
        second = await _seed_item(session, ws, user, title="B", angle=0.02)
        await session.commit()

        await recompute_clusters(session, ws)
        await session.commit()

        assert first.corroboration_count == 1
        assert first.priority_score is None
        assert second.priority_score is None


async def test_the_digest_shows_what_corroborates_an_item():
    """The rendering that was unreachable: `[corroborated by N]` had no code
    path that could ever set the count."""
    from app.services.workset_read import digest

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await _seed_item(session, ws, user, title="Vendor A", angle=0.0)
        second = await _seed_item(session, ws, user, title="Vendor B", angle=0.02)
        for item in (first, second):
            item.summary = "One incident, two sources."
            item.priority_score, item.priority_source = 60, "auto"
            item.priority_components = {"llm": 60}
            item.status = "ready"
            item.occurred_at = datetime.now(UTC)
        await session.commit()

        await recompute_clusters(session, ws)
        await session.commit()

        rendered = (await digest(session, ws)).render()
        assert "[corroborated by 1]" in rendered


async def test_an_evicted_item_is_not_clustered():
    """Eviction reclaimed the content and its vectors; a cluster built on them
    would be built on nothing."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        live = await _seed_item(session, ws, user, title="Live", angle=0.0)
        gone = await _seed_item(session, ws, user, title="Gone", angle=0.02)
        gone.status = "evicted"
        await session.commit()

        outcome = await recompute_clusters(session, ws)
        await session.commit()

        assert outcome.clusters == 0
        assert live.corroboration_count == 0


async def test_vectors_from_different_models_are_not_compared():
    """Two models can share a dimensionality — 1536 is the shape of several —
    and vectors from different models occupy different spaces however wide they
    are. The cosine between them is a number, not a similarity, so grouping on
    dimension alone would cluster items that have nothing to do with each
    other."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await _seed_item(session, ws, user, title="A", angle=0.0)
        second = await _seed_item(session, ws, user, title="B", angle=0.0)
        # Identical vectors, identical width — and a different model, which is
        # the whole point: nothing about the numbers says they are unrelated.
        row = (
            await session.execute(
                text(
                    "UPDATE embeddings SET embedding_model = 'other-embed' "
                    "WHERE source_id = :sid RETURNING id"
                ),
                {"sid": str(second.knowledge_document_id)},
            )
        ).first()
        assert row is not None
        await session.commit()

        outcome = await recompute_clusters(session, ws)
        await session.commit()

        assert outcome.clusters == 0
        assert first.corroboration_count == 0
        assert second.corroboration_count == 0


async def test_vectors_of_different_widths_are_not_compared_either():
    """The other half of the same rule: a corpus embedded across a provider
    change holds vectors that cannot be multiplied together at all."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await _seed_item(session, ws, user, title="A", angle=0.0)
        second = await _seed_item(session, ws, user, title="B", angle=0.0)
        row = (
            await session.execute(
                text(
                    "UPDATE embeddings SET embedding_dimensions = 16, "
                    "embedding = :vec WHERE source_id = :sid RETURNING id"
                ),
                {
                    "vec": "[" + ",".join(["0.25"] * 16) + "]",
                    "sid": str(second.knowledge_document_id),
                },
            )
        ).first()
        assert row is not None
        await session.commit()

        outcome = await recompute_clusters(session, ws)
        await session.commit()

        assert outcome.clusters == 0
        assert first.corroboration_count == 0


async def test_the_job_entry_point_never_raises(monkeypatch):
    """It runs after the work it depends on has already succeeded, so failing
    the job would re-run that work to no purpose."""
    from app.services.workset_clustering import cluster_workset

    async with _temp_database() as session_factory:
        async with session_factory() as session:
            user = await _seed_user(session)
            ws = await resolve_workset(session, user, key="reports", scope="global")
            await _seed_item(session, ws, user, title="A", angle=0.0)
            await _seed_item(session, ws, user, title="B", angle=0.02)
            await session.commit()
            workset_id = ws.id

        assert (await cluster_workset("not-a-uuid")).error == "Not a workset id."
        assert (await cluster_workset(uuid.uuid4())).error == "No such workset."

        outcome = await cluster_workset(str(workset_id))
        assert outcome.error is None
        assert outcome.clusters == 1

        async with session_factory() as session:
            stored = (
                await session.execute(
                    WorksetItem.__table__.select().where(
                        WorksetItem.workset_id == workset_id
                    )
                )
            ).all()
            assert all(row.corroboration_count == 1 for row in stored)


async def test_another_collections_copy_of_a_document_is_not_read():
    """`Embedding.source_id` is not unique on its own — the table is indexed on
    `(knowledge_base_id, source_id)` because the same document id has rows under
    several collections. Matching on `source_id` alone pulled another tenant's
    copy of a borrowed document into THIS corpus's centroid."""
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        mine = await _seed_item(session, ws, user, title="Mine", angle=0.0)
        theirs = await _seed_item(session, ws, user, title="Theirs", angle=1.4)

        # Another tenant's collection, holding rows whose `source_id` collides
        # with one of ours and whose vectors would drag the centroid across the
        # threshold if they were read.
        stranger = await _seed_user(session)
        other_kb = KnowledgeBase(
            id=uuid.uuid4(), user_id=stranger.id, name="Someone else's"
        )
        session.add(other_kb)
        await session.flush()
        for _ in range(8):
            session.add(
                Embedding(
                    id=uuid.uuid4(),
                    user_id=stranger.id,
                    knowledge_base_id=other_kb.id,
                    source_type="document",
                    # The SAME document id, in a collection this workset has
                    # nothing to do with.
                    source_id=str(theirs.knowledge_document_id),
                    content="not ours",
                    embedding=_direction(0.0),
                    embedding_dimensions=_DIMENSIONS,
                    embedding_model="test-embed",
                )
            )
        await session.commit()

        outcome = await recompute_clusters(session, ws)
        await session.commit()

        # Eight foreign chunks at angle 0 would swamp our single chunk at 1.4
        # and cluster the two items together. They are not read at all.
        assert outcome.clusters == 0
        assert mine.corroboration_count == 0
        assert theirs.corroboration_count == 0
        assert isinstance(
            await session.get(KnowledgeDocument, theirs.knowledge_document_id),
            KnowledgeDocument,
        )


async def test_a_borrowed_documents_own_collection_is_still_read():
    """The other half of the same rule: scoping to the WORKSET's knowledge base
    would drop every borrowed document, which is the ordinary case here. The
    scope is each document's own collection, from `knowledge_documents`."""
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
    from app.services.workset import add_item

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        wiki = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Team wiki")
        session.add(wiki)
        await session.flush()
        foreign_doc = KnowledgeDocument(
            id=uuid.uuid4(),
            knowledge_base_id=wiki.id,
            filename="wiki.md",
            source_type="text",
            content="The incident began on a Tuesday. " * 30,
            status="ready",
        )
        session.add(foreign_doc)
        await session.flush()

        ws = await resolve_workset(session, user, key="reports", scope="global")
        owned = await _seed_item(session, ws, user, title="Owned", angle=0.0)
        borrowed = await add_item(
            session, ws, user, title="Borrowed",
            knowledge_document_id=foreign_doc.id, external_ref="wiki-1",
        )
        # The borrowed document's vectors live in ITS collection, not ours.
        session.add(
            Embedding(
                id=uuid.uuid4(),
                user_id=user.id,
                knowledge_base_id=wiki.id,
                source_type="document",
                source_id=str(foreign_doc.id),
                content="wiki",
                embedding=_direction(0.02),
                embedding_dimensions=_DIMENSIONS,
                embedding_model="test-embed",
            )
        )
        await session.commit()

        outcome = await recompute_clusters(session, ws)
        await session.commit()

        assert outcome.clusters == 1
        assert owned.corroboration_count == 1
        assert borrowed.item.corroboration_count == 1


async def test_the_document_cap_bounds_the_similarity_matrix(monkeypatch):
    """The row budget bounds vectors READ; the similarity matrix is quadratic
    in DOCUMENTS and the row budget does not constrain it at all. 20 000
    single-chunk documents pass the vector cap and produce a 1.6 GB product in
    a worker process."""
    from app.services import workset_clustering as module

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        for index in range(6):
            await _seed_item(
                session, ws, user, title=f"Report {index}", angle=0.01 * index
            )
        await session.commit()

        monkeypatch.setattr(module, "_MAX_CLUSTER_DOCUMENTS", 4)
        outcome = await module.recompute_clusters(session, ws)
        await session.commit()

        # Four clustered, two left out — and SAID to be left out, rather than
        # quietly ranking a corpus it only partly looked at.
        assert outcome.skipped == 2
        assert len(outcome.clustered_item_ids) == 4


async def test_a_partly_read_document_is_dropped_rather_than_half_clustered(
    monkeypatch,
):
    """The row budget cuts INSIDE one document's chunk list. A centroid built
    from an arbitrary prefix is a wrong answer rather than a missing one."""
    from app.services import workset_clustering as module

    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        first = await _seed_item(session, ws, user, title="A", angle=0.0)
        second = await _seed_item(session, ws, user, title="B", angle=0.02)
        # Rows come back ordered by `source_id`, so the document that gets the
        # extra chunk has to be the one sorting LAST — otherwise the cut falls
        # somewhere else and the test measures a different thing on every run.
        pair = sorted((first, second), key=lambda i: str(i.knowledge_document_id))
        head, boundary = pair[0], pair[1]
        # A second chunk pointing the other way: a prefix-only centroid would
        # look like the head document, a whole one would not.
        session.add(
            Embedding(
                id=uuid.uuid4(),
                user_id=user.id,
                knowledge_base_id=ws.knowledge_base_id,
                source_type="document",
                source_id=str(boundary.knowledge_document_id),
                content="tail",
                embedding=_direction(2.5),
                embedding_dimensions=_DIMENSIONS,
                embedding_model="test-embed",
            )
        )
        await session.commit()

        # Two rows fit: all of the head document, and the first chunk of the
        # boundary one.
        monkeypatch.setattr(module, "_MAX_VECTORS", 2)
        outcome = await module.recompute_clusters(session, ws)
        await session.commit()

        # Excluded and COUNTED, rather than clustered on half of itself — the
        # old behaviour reported nothing skipped and grouped the two.
        assert outcome.skipped == 1
        assert outcome.clusters == 0
        assert head.corroboration_count == 0
        assert boundary.corroboration_count == 0


async def test_a_reset_item_does_not_keep_a_stale_corroboration_component():
    """`corroboration_count = 0` beside `{"corroboration": 14}` is the count and
    the component disagreeing, and reads surface the component."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        kept = await _seed_item(session, ws, user, title="Kept", angle=0.0)
        twin = await _seed_item(session, ws, user, title="Twin", angle=0.02)
        await session.commit()

        # Unscored on purpose: this is the branch that used to fall through and
        # keep the old dict.
        await recompute_clusters(session, ws)
        await session.commit()
        assert kept.priority_components["corroboration"] == (
            CORROBORATION_PER_SOURCE
        )

        twin.status = "discarded"
        await session.commit()
        await recompute_clusters(session, ws)
        await session.commit()

        assert kept.corroboration_count == 0
        assert "corroboration" not in (kept.priority_components or {})


async def test_the_reset_keeps_components_it_does_not_own():
    """Merging rather than replacing: this pass owns one key."""
    async with _temp_database() as session_factory, session_factory() as session:
        user = await _seed_user(session)
        ws = await resolve_workset(session, user, key="reports", scope="global")
        solo = await _seed_item(session, ws, user, title="Solo", angle=0.0)
        solo.priority_score, solo.priority_source = 50, "auto"
        solo.priority_components = {"llm": 50, "recency": 5}
        await session.commit()

        await recompute_clusters(session, ws)
        await session.commit()

        assert solo.priority_components["recency"] == 5
        assert solo.priority_components["llm"] == 50
        assert "corroboration" not in solo.priority_components
