"""Criterion scores against a real PostgreSQL.

``test_annotations_api.py`` and ``test_feedback_criteria.py`` pin the rules;
this pins what the schema does with them: a rating's scores are replaced as a
set, kept when a re-rating sends none, removed with the rating, with the chat
rating, and with the criterion, and the agent summary reads both storages.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.models.annotation import AnnotationScore
from app.models.chat import MessageFeedback
from app.models.feedback_criterion import FeedbackCriterion
from app.services import annotations as annotation_service
from app.services.feedback_criteria import ValidatedScore, load_scores, replace_scores
from tests.test_annotations_postgres import _seed, _target
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


async def _criteria(sessionmaker) -> tuple[FeedbackCriterion, FeedbackCriterion]:
    async with sessionmaker() as session:
        accuracy = FeedbackCriterion(
            key="accuracy", name="Accuracy", type="scale", weight=1.0, min_value=1.0, max_value=5.0
        )
        tone = FeedbackCriterion(
            key="tone",
            name="Tone",
            type="categorical",
            weight=3.0,
            options=[
                {"value": "cold", "label": "Cold", "score": 0.0},
                {"value": "warm", "label": "Warm", "score": 1.0},
            ],
        )
        session.add_all([accuracy, tone])
        await session.commit()
        return accuracy, tone


async def _score_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(await session.scalar(select(func.count()).select_from(AnnotationScore)))


@pytest.mark.asyncio
async def test_a_ratings_scores_are_replaced_kept_and_removed_with_it():
    async with _temp_database("crit_scores") as sessionmaker:
        seeded = await _seed(sessionmaker)
        accuracy, tone = await _criteria(sessionmaker)
        user, agent = seeded["user"], seeded["agent"]
        target = _target("assignment_run", seeded["run"], agent.id)

        async with sessionmaker() as session:
            rating = await annotation_service.upsert_annotation(
                session,
                target,
                user.id,
                "negative",
                None,
                scores=[ValidatedScore(accuracy.id, 5.0, None), ValidatedScore(tone.id, None, "warm")],
            )
        assert await _score_count(sessionmaker) == 2

        # A re-rating with scores replaces the whole set.
        async with sessionmaker() as session:
            await annotation_service.upsert_annotation(
                session, target, user.id, "negative", None, scores=[ValidatedScore(accuracy.id, 2.0, None)]
            )
        # A re-rating without scores leaves them alone.
        async with sessionmaker() as session:
            await annotation_service.upsert_annotation(session, target, user.id, "positive", "Better", scores=None)
        async with sessionmaker() as session:
            stored = (await load_scores(session, annotation_ids=[rating.id]))[rating.id]
            entries, _total = await annotation_service.list_agent_annotations(session, agent.id)
        assert [(score.criterion_id, score.value_numeric) for score in stored] == [(accuracy.id, 2.0)]
        run_entry = next(entry for entry in entries if entry.target_type == "assignment_run")
        # Scale 1-5, scored 2: a quarter of the way, whatever the thumbs say.
        assert run_entry.weighted_score == 0.25

        # Retracting the rating removes its scores.
        async with sessionmaker() as session:
            assert await annotation_service.delete_annotation(session, target, user.id) is True
        assert await _score_count(sessionmaker) == 0


@pytest.mark.asyncio
async def test_chat_rating_scores_count_for_the_agent_and_go_with_the_rating():
    async with _temp_database("crit_chat") as sessionmaker:
        seeded = await _seed(sessionmaker)
        accuracy, _tone = await _criteria(sessionmaker)
        agent = seeded["agent"]

        async with sessionmaker() as session:
            feedback_id = await session.scalar(
                select(MessageFeedback.id).where(MessageFeedback.message_id == seeded["message"])
            )
            await replace_scores(
                session, message_feedback_id=feedback_id, scores=[ValidatedScore(accuracy.id, 5.0, None)]
            )
            await annotation_service.upsert_annotation(
                session,
                _target("sub_agent_task", seeded["task"], agent.id),
                seeded["user"].id,
                "negative",
                None,
                scores=[ValidatedScore(accuracy.id, 1.0, None)],
            )

        async with sessionmaker() as session:
            summary = await annotation_service.summarize_agent_criteria(session, agent.id)
        by_key = {stats.criterion.key: stats for stats in summary.statistics}
        # One chat rating (5) and one task rating (1): both storages are read.
        assert (by_key["accuracy"].count, by_key["accuracy"].mean_score) == (2, 0.5)
        assert [bucket.count for bucket in by_key["accuracy"].histogram] == [1, 0, 0, 0, 1]
        [today] = summary.daily[accuracy.id]
        assert (today.count, today.mean_score) == (2, 0.5)
        # An enabled criterion nobody scored is still listed, empty.
        assert (by_key["tone"].count, by_key["tone"].mean_score) == (0, None)

        async with sessionmaker() as session:
            await session.execute(delete(MessageFeedback).where(MessageFeedback.id == feedback_id))
            await session.commit()
        assert await _score_count(sessionmaker) == 1


@pytest.mark.asyncio
async def test_deleting_a_criterion_deletes_its_scores_only():
    async with _temp_database("crit_delete") as sessionmaker:
        seeded = await _seed(sessionmaker)
        accuracy, tone = await _criteria(sessionmaker)
        async with sessionmaker() as session:
            await annotation_service.upsert_annotation(
                session,
                _target("agent_delegation", seeded["delegation"], seeded["agent"].id),
                seeded["user"].id,
                "positive",
                None,
                scores=[ValidatedScore(accuracy.id, 3.0, None), ValidatedScore(tone.id, None, "cold")],
            )

        async with sessionmaker() as session:
            await session.execute(delete(FeedbackCriterion).where(FeedbackCriterion.id == tone.id))
            await session.commit()

        async with sessionmaker() as session:
            remaining = (await session.execute(select(AnnotationScore.criterion_id))).scalars().all()
        assert remaining == [accuracy.id]


@pytest.mark.asyncio
async def test_a_score_belongs_to_exactly_one_rating_and_holds_one_value():
    async with _temp_database("crit_checks") as sessionmaker:
        await _seed(sessionmaker)
        accuracy, _tone = await _criteria(sessionmaker)
        # CHECK constraints fire on the row itself, before the foreign keys.
        for score, constraint in (
            (AnnotationScore(criterion_id=accuracy.id, value_numeric=3.0), "ck_annotation_scores_one_rating"),
            (
                AnnotationScore(
                    annotation_id=uuid.uuid4(), criterion_id=accuracy.id, value_numeric=3.0, value_text="3"
                ),
                "ck_annotation_scores_one_value",
            ),
        ):
            async with sessionmaker() as session:
                session.add(score)
                with pytest.raises(IntegrityError, match=constraint):
                    await session.commit()
