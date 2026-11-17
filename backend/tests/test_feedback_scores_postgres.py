"""One chat rating per rater and trace, against a real PostgreSQL.

The reading rule (latest row per rater, a retraction counts as no rating) is
checked on rows shaped the way the append-only writers left them, through both
spellings of the rule: the SQLAlchemy subquery and the raw SQL. The sink is
then checked to keep a single row per rater holding its latest action: a
changed rating replaces it, a retraction nulls it, a delayed older action is
refused, and pre-existing duplicates collapse on the next write. Last, a daily
rollup built before the fix is corrected from its raw traces.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.api.v1 import chat
from app.models.chat import Conversation, Message
from app.models.llm_trace import LLMDailyStats, LLMScore, LLMTrace
from app.models.user import User
from app.services.trace_storage import refresh_daily_feedback
from app.services.tracing.feedback_scores import (
    FEEDBACK_PER_TRACE_SQL_POSTGRES,
    LEGACY_RETRACTION_COMMENT,
    feedback_per_trace_subquery,
)
from app.services.tracing.local_sink import LocalPGSink
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

START = datetime(2026, 9, 1, tzinfo=UTC)


def _trace() -> LLMTrace:
    return LLMTrace(id=uuid.uuid4(), source="agent-chat", name="chat", started_at=START)


def _score(
    trace: LLMTrace,
    user_id,
    value,
    minute: int,
    comment: str | None = None,
    metadata: dict | None = None,
) -> LLMScore:
    # ``metadata=None`` is how the append-only chat writer stored its rows.
    return LLMScore(
        trace_id=trace.id,
        user_id=user_id,
        name="user_feedback",
        source="USER",
        data_type="BOOLEAN",
        value=value,
        comment=comment,
        metadata_json=metadata,
        created_at=START + timedelta(minutes=minute),
    )


@pytest.mark.asyncio
async def test_each_rater_counts_once_with_the_latest_rating():
    async with _temp_database("feedback_rule") as sessionmaker:
        changed, retracted, thumbs_down, anonymous, re_rated, typed = (_trace() for _ in range(6))
        alice, bob = uuid.uuid4(), uuid.uuid4()
        async with sessionmaker() as db:
            db.add_all([changed, retracted, thumbs_down, anonymous, re_rated, typed])
            await db.flush()
            db.add_all(
                [
                    # up switched to down by Alice, Bob up: (0 + 1) / 2
                    _score(changed, alice, 1.0, 1),
                    _score(changed, alice, 0.0, 2),
                    _score(changed, bob, 1.0, 3),
                    # up then retracted (legacy spelling): no rating at all
                    _score(retracted, alice, 1.0, 1),
                    _score(retracted, alice, 0.0, 2, LEGACY_RETRACTION_COMMENT),
                    # a real thumbs down stays a 0
                    _score(thumbs_down, alice, 0.0, 1, "wrong invoice"),
                    # rows without a rater each count
                    _score(anonymous, None, 1.0, 1),
                    _score(anonymous, None, 0.0, 2),
                    # retracted, then rated again: the new rating counts
                    _score(re_rated, alice, 0.0, 1),
                    _score(re_rated, alice, 0.0, 2, LEGACY_RETRACTION_COMMENT),
                    _score(re_rated, alice, 1.0, 3),
                    # a thumbs down whose typed comment is the marker text:
                    # a new rating carries metadata, so it is not a retraction
                    _score(typed, alice, 0.0, 1, LEGACY_RETRACTION_COMMENT, {"rating": "negative"}),
                ]
            )
            await db.commit()

        expected = {
            changed.id: 0.5,
            thumbs_down.id: 0.0,
            anonymous.id: 0.5,
            re_rated.id: 1.0,
            typed.id: 0.0,
        }
        async with sessionmaker() as db:
            sub = feedback_per_trace_subquery()
            orm_rows = {row.trace_id: row.feedback_value for row in await db.execute(select(sub))}
            sql_rows = {
                row.trace_id: row.feedback_value
                for row in await db.execute(text(FEEDBACK_PER_TRACE_SQL_POSTGRES))
            }
        assert orm_rows == pytest.approx(expected)
        assert sql_rows == pytest.approx(expected)
        assert retracted.id not in orm_rows


async def _rows(sessionmaker, trace_id) -> list[LLMScore]:
    async with sessionmaker() as db:
        return list((await db.execute(select(LLMScore).where(LLMScore.trace_id == trace_id))).scalars())


@pytest.mark.asyncio
async def test_the_sink_keeps_one_row_per_rater():
    async with _temp_database("feedback_sink") as sessionmaker:
        trace = _trace()
        alice = uuid.uuid4()
        async with sessionmaker() as db:
            db.add(trace)
            await db.flush()
            # Duplicates left by the append-only writer before the fix.
            db.add_all([_score(trace, alice, 1.0, 1), _score(trace, alice, 0.0, 2)])
            await db.commit()

        sink = LocalPGSink(session_factory=sessionmaker)
        payload = {
            "trace_id": str(trace.id),
            "user_id": str(alice),
            "name": "user_feedback",
            "source": "USER",
            "data_type": "BOOLEAN",
        }

        await sink.flush_score({**payload, "value": 1.0, "comment": "right answer"})
        rows = await _rows(sessionmaker, trace.id)
        assert [(row.value, row.comment) for row in rows] == [(1.0, "right answer")]

        await sink.flush_score({**payload, "value": 0.0})
        rows = await _rows(sessionmaker, trace.id)
        assert [(row.value, row.comment) for row in rows] == [(0.0, None)]

        retracted_at = datetime.now(UTC)
        await sink.flush_score({**payload, "value": None, "retract": True, "created_at": retracted_at})
        rows = await _rows(sessionmaker, trace.id)
        assert [row.value for row in rows] == [None]

        # The rating the retraction undid, flushed late: it is older, so ignored.
        await sink.flush_score({**payload, "value": 1.0, "created_at": retracted_at - timedelta(seconds=2)})
        rows = await _rows(sessionmaker, trace.id)
        assert [row.value for row in rows] == [None]
        async with sessionmaker() as db:
            feedback = (await db.execute(select(feedback_per_trace_subquery()))).all()
        assert [row.feedback_value for row in feedback] == [None]

        # A score without a rater is still appended as before.
        await sink.flush_score({**payload, "user_id": None, "value": 1.0})
        await sink.flush_score({**payload, "user_id": None, "value": 1.0})
        assert len(await _rows(sessionmaker, trace.id)) == 3


@pytest.mark.asyncio
async def test_a_rollup_built_before_the_fix_is_corrected():
    async with _temp_database("feedback_rollup") as sessionmaker:
        trace, alice = _trace(), uuid.uuid4()
        async with sessionmaker() as db:
            db.add(trace)
            await db.flush()
            db.add_all([_score(trace, alice, 1.0, 1), _score(trace, alice, 0.0, 2)])
            # The rollup of that day, averaged over every row: 0.5.
            stats = LLMDailyStats(
                date=START.date(), agent_id=None, user_id=None, model="", source="agent-chat", trace_count=1,
                avg_feedback_score=0.5,
            )
            # A group whose traces were purged keeps its value.
            orphan = LLMDailyStats(
                date=START.date(), agent_id=uuid.uuid4(), user_id=None, model="", source="agent-chat", trace_count=3,
                avg_feedback_score=0.75,
            )
            db.add_all([stats, orphan])
            await db.commit()

        async with sessionmaker() as db:
            assert await refresh_daily_feedback(db, START.date()) == 1
            assert await refresh_daily_feedback(db, START.date()) == 0
            values = {
                row.id: row.avg_feedback_score
                for row in (await db.execute(select(LLMDailyStats))).scalars()
            }
        assert values == {stats.id: 0.0, orphan.id: 0.75}


@pytest.mark.asyncio
async def test_the_feedback_refresh_rides_the_source_index():
    """``llm_traces`` has no index on ``started_at`` alone: without a ``source``
    predicate the refresh's day scan reads the whole of another composite index,
    once per refreshed day - the scan fixed in the rollup build."""
    from sqlalchemy import event

    from app.models.llm_trace import LLM_TRACE_SOURCES

    day = (datetime.now(UTC) - timedelta(days=5)).date()
    sources = "ARRAY[" + ", ".join(f"'{s}'" for s in LLM_TRACE_SOURCES) + "]"
    async with _temp_database("feedback_plan") as sessionmaker, sessionmaker() as db:
        await db.execute(
            text(
                f"""
                INSERT INTO llm_traces (
                    id, source, name, model, started_at, status, level,
                    input_tokens, output_tokens, total_cost, duration_ms
                )
                SELECT gen_random_uuid(), ({sources})[1 + (i % 7)], 'plan', 'model-' || (i % 5),
                       now() - (i % 60) * interval '1 day' - (i % 1440) * interval '1 minute',
                       'ok', 'DEFAULT', 10, 5, 0.001, 100 + (i % 900)
                FROM generate_series(1, 20000) i
                """  # noqa: S608 - sources is built from a module constant
            )
        )
        await db.commit()
        await db.execute(text("ANALYZE llm_traces"))
        await db.commit()

        captured: list[tuple[str, object]] = []

        def _capture(_conn, _cursor, statement, parameters, _context, _executemany):
            if statement.lstrip().startswith("UPDATE llm_daily_stats"):
                captured.append((statement, parameters))

        sync_engine = db.bind.sync_engine
        event.listen(sync_engine, "before_cursor_execute", _capture)
        try:
            await refresh_daily_feedback(db, day)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _capture)

        ((statement, parameters),) = captured
        conn = await db.connection()
        await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            row[0] for row in (await conn.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
        )
        index_conds = [line for line in plan.splitlines() if "Index Cond" in line]
        assert any("source" in cond and "started_at" in cond for cond in index_conds), plan


@pytest.mark.asyncio
async def test_a_stale_day_rebuilt_in_place_and_the_refresh_agree_on_its_feedback():
    """A stale day is rebuilt in place from its traces: the rebuild
    and the refresh must count feedback the same way, or each would undo the
    other, and nothing but the feedback may move."""
    from app.services.trace_storage import mark_daily_stats_stale, rebuild_daily_stats

    day = (datetime.now(UTC) - timedelta(days=5)).date()
    started = datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    async with _temp_database("feedback_converge") as sessionmaker:
        alice = uuid.uuid4()
        trace = LLMTrace(id=uuid.uuid4(), source="agent-chat", name="chat", started_at=started, total_cost=2)
        async with sessionmaker() as db:
            db.add(trace)
            await db.flush()
            # Up, then switched to down by the append-only writer: 0.0, not 0.5.
            db.add_all([_score(trace, alice, 1.0, 1), _score(trace, alice, 0.0, 2)])
            db.add(LLMDailyStats(date=day, agent_id=None, user_id=None, model="", source="agent-chat",
                                 trace_count=1, total_cost=2, avg_feedback_score=0.5))
            await db.commit()

        async def _figures():
            async with sessionmaker() as db:
                return [
                    (row.trace_count, float(row.total_cost), row.avg_feedback_score)
                    for row in (await db.execute(select(LLMDailyStats))).scalars()
                ]

        async with sessionmaker() as db:
            await mark_daily_stats_stale(db, [day])
            await db.commit()
            assert await rebuild_daily_stats(db, day) == 1
        rebuilt = await _figures()
        async with sessionmaker() as db:
            assert await refresh_daily_feedback(db, day) == 0
        refreshed = await _figures()

    assert rebuilt == [(1, 2.0, 0.0)]
    assert refreshed == rebuilt


# -- The order of a rater's actions ---------------------------


async def _chat(sessionmaker, *, messages: int = 1):
    """An owner, their conversation, one turn's trace and its rated messages."""
    user = User(email=f"rater-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Rater")
    async with sessionmaker() as db:
        db.add(user)
        await db.flush()
        conversation = Conversation(user_id=user.id)
        trace = _trace()
        db.add_all([conversation, trace])
        await db.flush()
        # The segments of one turn all carry the turn's trace.
        turn = [
            Message(conversation_id=conversation.id, role="assistant", content=f"part {n}", sequence=n,
                    llm_trace_id=trace.id)
            for n in range(messages)
        ]
        db.add_all(turn)
        await db.commit()
    return user, conversation, trace, turn


class _Dispatched:
    """Stands in for the tracing dispatcher: the local scores, in dispatch order."""

    def __init__(self) -> None:
        self.scores: list[dict] = []

    async def record_score(self, score: dict) -> None:
        self.scores.append(score)


def _no_langfuse(monkeypatch) -> _Dispatched:
    async def _nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat, "_push_langfuse_feedback_score", _nothing)
    monkeypatch.setattr(chat, "_retract_langfuse_feedback_score", _nothing)
    dispatched = _Dispatched()
    monkeypatch.setattr(chat, "get_tracing_dispatcher", lambda: dispatched)
    return dispatched


async def _rate(sessionmaker, user, conversation, message, rating: str) -> None:
    async with sessionmaker() as db:
        await chat.upsert_message_feedback(
            conversation_id=str(conversation.id),
            message_id=str(message.id),
            body=chat.MessageFeedbackUpsert(rating=rating),
            user=user,
            db=db,
        )


async def _retract(sessionmaker, user, conversation, message) -> None:
    async with sessionmaker() as db:
        await chat.delete_message_feedback(
            conversation_id=str(conversation.id), message_id=str(message.id), user=user, db=db
        )


async def _trace_feedback(sessionmaker) -> list:
    async with sessionmaker() as db:
        return [row.feedback_value for row in (await db.execute(select(feedback_per_trace_subquery()))).all()]


@pytest.mark.asyncio
async def test_a_rating_held_up_by_langfuse_does_not_outlive_the_retraction_after_it(monkeypatch):
    """The action time was read after the Langfuse push: a rating whose push hung
    while the rater retracted it carried a LATER time than its retraction, and the
    sinks, which keep the latest action, brought it back."""
    async with _temp_database("feedback_order") as sessionmaker:
        user, conversation, _trace_row, (message,) = await _chat(sessionmaker)
        dispatched = _no_langfuse(monkeypatch)
        parked, release = asyncio.Event(), asyncio.Event()

        async def _slow_langfuse(*_args, **_kwargs):
            parked.set()
            await release.wait()

        monkeypatch.setattr(chat, "_push_langfuse_feedback_score", _slow_langfuse)

        rating = asyncio.create_task(_rate(sessionmaker, user, conversation, message, "positive"))
        await asyncio.wait_for(parked.wait(), timeout=10)
        await _retract(sessionmaker, user, conversation, message)
        release.set()
        await rating

        # The retraction is dispatched first, the rating it undid last.
        assert [score.get("retract", False) for score in dispatched.scores] == [True, False]
        sink = LocalPGSink(session_factory=sessionmaker)
        for score in dispatched.scores:
            await sink.flush_score(score)

        assert await _trace_feedback(sessionmaker) == [None]


@pytest.mark.asyncio
async def test_two_messages_of_one_turn_count_as_one_rating_and_neither_erases_the_other(monkeypatch):
    """The segments of one turn share its trace, and the sinks keep ONE score per
    rater and trace: rating both counts their mean, and retracting one leaves the
    other's rating instead of leaving no rating at all."""
    async with _temp_database("feedback_turn") as sessionmaker:
        user, conversation, _trace_row, (segment, answer) = await _chat(sessionmaker, messages=2)
        dispatched = _no_langfuse(monkeypatch)
        sink = LocalPGSink(session_factory=sessionmaker)

        async def _flushed() -> list:
            await sink.flush_score(dispatched.scores[-1])
            return await _trace_feedback(sessionmaker)

        await _rate(sessionmaker, user, conversation, segment, "positive")
        assert await _flushed() == [1.0]
        await _rate(sessionmaker, user, conversation, answer, "negative")
        assert await _flushed() == [0.5]
        await _retract(sessionmaker, user, conversation, segment)
        assert await _flushed() == [0.0]
        assert dispatched.scores[-1].get("retract") is None
        await _retract(sessionmaker, user, conversation, answer)
        assert await _flushed() == [None]
        assert dispatched.scores[-1]["retract"] is True


@pytest.mark.asyncio
async def test_back_to_back_actions_on_one_score_land_in_their_own_millisecond():
    """ClickHouse keeps ``created_at`` to the millisecond and orders two actions of
    the same millisecond by their random ids. The next action on a rater's score
    reads its time only once this one released the lock, so this one holds it
    until the database clock has left its millisecond."""
    from app.services.tracing.feedback_scores import begin_score_action, end_score_action

    def _millisecond(at: datetime) -> int:
        return int(at.timestamp() * 1000)

    async with _temp_database("feedback_ms") as sessionmaker, sessionmaker() as db:
        trace_id, user_id = uuid.uuid4(), uuid.uuid4()
        for _ in range(50):
            action_at = await begin_score_action(db, trace_id, user_id, "user_feedback")
            await end_score_action(db, action_at)
            released_at = (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()
            await db.commit()
            assert _millisecond(released_at) > _millisecond(action_at)


@pytest.mark.asyncio
async def test_an_admin_score_is_not_overwritten_by_an_older_chat_rating_flushed_late():
    """The admin REST path kept the row's old time on an update, so a chat rating
    older than the admin's action, flushed late, still looked newer and won."""
    from app.services.tracing.read_store import PostgresTraceReadStore

    async with _temp_database("feedback_admin") as sessionmaker:
        admin = User(email=f"admin-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x",
                     full_name="Admin", role="admin")
        trace = _trace()
        async with sessionmaker() as db:
            db.add_all([admin, trace])
            await db.commit()
            db_now = (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()

        sink = LocalPGSink(session_factory=sessionmaker)
        chat_score = {
            "trace_id": str(trace.id),
            "user_id": str(admin.id),
            "name": "user_feedback",
            "source": "USER",
            "data_type": "BOOLEAN",
            "metadata": {"rating": "positive"},
        }
        await sink.flush_score({**chat_score, "value": 1.0, "created_at": db_now - timedelta(seconds=10)})
        late = {**chat_score, "value": 0.0, "created_at": db_now - timedelta(seconds=5)}

        async with sessionmaker() as db:
            await PostgresTraceReadStore().create_trace_score(
                db, user=admin, user_group_ids=[], trace_id=trace.id, name="user_feedback",
                data_type="NUMERIC", value=0.25, string_value=None, comment="reviewed", metadata={},
            )
        await sink.flush_score(late)

        rows = await _rows(sessionmaker, trace.id)
        assert [(row.value, row.comment) for row in rows] == [(0.25, "reviewed")]


# -- The chat scores the append-only chat writer left ----------


def _one_day(day) -> dict:
    from app.models.llm_trace import LLM_TRACE_SOURCES

    day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return {"day_start": day_start, "day_end": day_start + timedelta(days=1), "sources": list(LLM_TRACE_SOURCES)}


async def _feedback_by_trace(sessionmaker) -> dict:
    """Both PostgreSQL spellings of the rule, which must agree: the subquery the
    readers join, and the raw SQL over one day the rollup is built with."""
    from app.services.tracing.feedback_scores import FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY

    async with sessionmaker() as db:
        readers = {row.trace_id: row.feedback_value for row in await db.execute(select(feedback_per_trace_subquery()))}
        rollup = {
            row.trace_id: row.feedback_value
            for row in await db.execute(text(FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY), _one_day(START.date()))
        }
    assert rollup == readers
    return readers


async def _old_chat_scores(sessionmaker):
    """One rater's turns as the append-only chat writer left them, with the
    ratings the rater holds now (``message_feedback``)."""
    from app.models.chat import MessageFeedback

    user = User(email=f"rater-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Rater")
    names = ("two_rated", "one_retracted", "re_rated", "admin_after", "all_retracted", "no_messages", "rated_once")
    traces = {name: _trace() for name in names}
    async with sessionmaker() as db:
        db.add(user)
        await db.flush()
        conversation = Conversation(user_id=user.id)
        db.add_all([conversation, *traces.values()])
        await db.flush()
        turns = {
            name: [
                Message(conversation_id=conversation.id, role="assistant", content=f"part {n}", sequence=n,
                        llm_trace_id=traces[name].id)
                for n in range(count)
            ]
            for name, count in (
                ("two_rated", 2), ("one_retracted", 2), ("re_rated", 1), ("admin_after", 2),
                ("all_retracted", 1), ("rated_once", 1),
            )
        }
        db.add_all([message for turn in turns.values() for message in turn])
        await db.flush()

        def rating(name: str, index: int, value: str) -> MessageFeedback:
            return MessageFeedback(message_id=turns[name][index].id, user_id=user.id, rating=value)

        db.add_all(
            [
                rating("two_rated", 0, "positive"),
                rating("two_rated", 1, "negative"),
                rating("one_retracted", 0, "positive"),
                rating("re_rated", 0, "negative"),
                rating("admin_after", 0, "positive"),
                rating("admin_after", 1, "negative"),
                rating("rated_once", 0, "positive"),
            ]
        )

        def old(name: str, value: float, minute: int, comment: str | None = None, metadata: dict | None = None):
            return _score(traces[name], user.id, value, minute, comment, metadata)

        db.add_all(
            [
                # Both segments of one turn rated, differently: the mean is 0.5.
                old("two_rated", 1.0, 1),
                old("two_rated", 0.0, 2),
                # The second rating retracted: the first still stands.
                old("one_retracted", 1.0, 1),
                old("one_retracted", 0.0, 2),
                old("one_retracted", 0.0, 3, LEGACY_RETRACTION_COMMENT),
                # One message re-rated: already read right, and rewritten the same.
                old("re_rated", 1.0, 1),
                old("re_rated", 0.0, 2),
                # An admin REST score after the chat ratings: the latest action, kept.
                old("admin_after", 1.0, 1),
                old("admin_after", 0.0, 2),
                old("admin_after", 0.25, 3, "reviewed", {}),
                # Every rating retracted, or the conversation deleted: nothing to rebuild from.
                old("all_retracted", 1.0, 1),
                old("all_retracted", 0.0, 2, LEGACY_RETRACTION_COMMENT),
                old("no_messages", 1.0, 1),
                old("no_messages", 0.0, 2),
                # A single row: nothing to misread.
                old("rated_once", 1.0, 1),
            ]
        )
        await db.commit()
    return user, conversation, traces, turns


@pytest.mark.asyncio
async def test_the_old_chat_scores_the_rule_misreads_are_rewritten_from_the_ratings(monkeypatch):
    """The old chat rows carry no message identity, so their
    latest row reads two segments of one turn rated 1 then 0 as 0 - main and the
    chat endpoints since give their mean, 0.5 - and a turn whose second rating
    was retracted as no rating at all. Those raters' scores are rebuilt once from
    the ratings they hold; every other score is left as it reads."""
    from app.services.trace_storage import rewrite_legacy_chat_scores

    async with _temp_database("feedback_rewrite") as sessionmaker:
        user, conversation, traces, turns = await _old_chat_scores(sessionmaker)
        ids = {name: trace.id for name, trace in traces.items()}
        assert await _feedback_by_trace(sessionmaker) == pytest.approx(
            {
                ids["two_rated"]: 0.0,
                ids["re_rated"]: 0.0,
                ids["admin_after"]: 0.25,
                ids["no_messages"]: 0.0,
                ids["rated_once"]: 1.0,
            }
        )

        async with sessionmaker() as db:
            assert await rewrite_legacy_chat_scores(db) is True
        expected = {
            ids["two_rated"]: 0.5,
            ids["one_retracted"]: 1.0,
            ids["re_rated"]: 0.0,
            ids["admin_after"]: 0.25,
            ids["no_messages"]: 0.0,
            ids["rated_once"]: 1.0,
        }
        assert await _feedback_by_trace(sessionmaker) == pytest.approx(expected)

        # A rewritten score is the rater's one row, as a chat action writes it,
        # timed right after their last old action.
        (two_rated,) = await _rows(sessionmaker, ids["two_rated"])
        assert (two_rated.value, two_rated.metadata_json["rated_messages"]) == (0.5, 2)
        assert two_rated.created_at == START + timedelta(minutes=2, milliseconds=1)
        (one_retracted,) = await _rows(sessionmaker, ids["one_retracted"])
        assert (one_retracted.value, one_retracted.metadata_json) == (1.0, {"rating": "positive"})
        # The others are untouched, row for row.
        held = {name: len(await _rows(sessionmaker, ids[name])) for name in ("admin_after", "all_retracted", "no_messages")}
        assert held == {"admin_after": 3, "all_retracted": 2, "no_messages": 2}

        # Done: a second run finds nothing to rewrite.
        async with sessionmaker() as db:
            assert await rewrite_legacy_chat_scores(db) is True
        assert await _feedback_by_trace(sessionmaker) == pytest.approx(expected)

        # A chat action since outranks the rewritten row.
        dispatched = _no_langfuse(monkeypatch)
        await _rate(sessionmaker, user, conversation, turns["two_rated"][1], "positive")
        await LocalPGSink(session_factory=sessionmaker).flush_score(dispatched.scores[-1])
        assert (await _feedback_by_trace(sessionmaker))[ids["two_rated"]] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_the_old_chat_scores_are_rewritten_a_batch_per_run():
    from app.services.trace_storage import rewrite_legacy_chat_scores

    async with _temp_database("feedback_rewrite_batch") as sessionmaker:
        await _old_chat_scores(sessionmaker)  # three scores to rewrite
        done = []
        for _ in range(4):
            async with sessionmaker() as db:
                done.append(await rewrite_legacy_chat_scores(db, limit=1))
    assert done == [False, False, True, True]


def _window_rows(plan: dict) -> list[int]:
    """The rows each ``WindowAgg`` of an ``EXPLAIN (ANALYZE, FORMAT JSON)`` plan ranked:
    its input, since it emits only the first of each rater (a run condition, PG 15+)."""
    found = []
    if plan.get("Node Type") == "WindowAgg":
        (ranked,) = plan["Plans"]
        found.append(ranked["Actual Rows"] * ranked["Actual Loops"])
    for child in plan.get("Plans", []):
        found.extend(_window_rows(child))
    return found


@pytest.mark.asyncio
async def test_a_day_scan_ranks_only_the_scores_of_that_days_traces():
    """The refresh ranked every feedback row there is, once per
    refreshed day (the day rollup build and its rebuild too). The rule now
    ranks only that day's scores, and still counts them the same."""
    import json

    from app.models.llm_trace import LLM_TRACE_SOURCES
    from app.services.tracing.feedback_scores import FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY

    day = (datetime.now(UTC) - timedelta(days=5)).date()
    window = _one_day(day)
    sources = "ARRAY[" + ", ".join(f"'{s}'" for s in LLM_TRACE_SOURCES) + "]"
    async with _temp_database("feedback_day_scope") as sessionmaker, sessionmaker() as db:
        # Sixty days of traces, each with one rater's thumbs up switched to a thumbs down.
        await db.execute(
            text(
                f"""
                INSERT INTO llm_traces (
                    id, source, name, model, started_at, status, level,
                    input_tokens, output_tokens, total_cost, duration_ms
                )
                SELECT gen_random_uuid(), ({sources})[1 + (i % 7)], 'plan', 'model-' || (i % 5),
                       now() - (i % 60) * interval '1 day' - (i % 1440) * interval '1 minute',
                       'ok', 'DEFAULT', 10, 5, 0.001, 100 + (i % 900)
                FROM generate_series(1, 6000) i
                """  # noqa: S608 - sources is built from a module constant
            )
        )
        await db.execute(
            text(
                """
                INSERT INTO llm_scores (id, trace_id, user_id, name, source, data_type, value, created_at)
                SELECT gen_random_uuid(), t.id, t.rater, 'user_feedback', 'USER', 'BOOLEAN', v.value,
                       t.started_at + v.after
                FROM (SELECT id, started_at, gen_random_uuid() AS rater FROM llm_traces) t
                CROSS JOIN (VALUES (1.0, interval '1 minute'), (0.0, interval '2 minutes')) v(value, after)
                """
            )
        )
        groups = (
            await db.execute(
                text(
                    "SELECT DISTINCT COALESCE(model, '') AS model, source FROM llm_traces "
                    "WHERE started_at >= :day_start AND started_at < :day_end AND source = ANY(:sources)"
                ),
                window,
            )
        ).all()
        # The day's rollup, built before the fix: every row averaged, 0.5.
        db.add_all(
            [
                LLMDailyStats(date=day, agent_id=None, user_id=None, model=model, source=source, trace_count=1,
                              avg_feedback_score=0.5)
                for model, source in groups
            ]
        )
        await db.commit()
        await db.execute(text("ANALYZE llm_traces"))
        await db.execute(text("ANALYZE llm_scores"))
        await db.commit()

        day_scores = (
            await db.execute(
                text(
                    "SELECT count(*) FROM llm_scores s JOIN llm_traces t ON t.id = s.trace_id "
                    "WHERE t.started_at >= :day_start AND t.started_at < :day_end AND t.source = ANY(:sources)"
                ),
                window,
            )
        ).scalar_one()
        assert 0 < day_scores < 12000

        async def _ranked(sql: str, params: dict) -> list[int]:
            explain = f"EXPLAIN (ANALYZE, FORMAT JSON) SELECT * FROM ({sql}) fb"  # noqa: S608 - the rule's constants
            (raw,) = (await db.execute(text(explain), params)).one()
            return _window_rows((json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"])

        assert await _ranked(FEEDBACK_PER_TRACE_SQL_POSTGRES_ONE_DAY, window) == [day_scores]
        assert await _ranked(FEEDBACK_PER_TRACE_SQL_POSTGRES, {}) == [12000]
        await db.rollback()

        # And the day still reads every rater's latest rating: 0 for each group.
        assert await refresh_daily_feedback(db, day) == len(groups)
        stored = {row[0] for row in (await db.execute(text("SELECT avg_feedback_score FROM llm_daily_stats"))).all()}
        assert stored == {0.0}


# -- A late rating on an older day -----------


@pytest.mark.asyncio
async def test_a_late_rating_on_an_older_day_is_rebuilt_by_the_next_run(monkeypatch):
    """The backfill re-reads the feedback of the last seven complete days only. A
    rating changed, then retracted, on an older trace records its day stale, and
    the next run rebuilds that day - its feedback, with count and cost unchanged.
    A recent day records nothing: the recent refresh reads it every run."""
    from app.services import trace_storage
    from app.services.trace_storage import latest_complete_day, run_daily_stats_backfill

    async def _no_recent_refresh(*_args, **_kwargs):
        return 0  # so only the recorded day's rebuild can move a figure

    monkeypatch.setattr(trace_storage, "refresh_rollup_feedback", _no_recent_refresh)
    latest = latest_complete_day()
    old_day, recent_day = latest - timedelta(days=10), latest - timedelta(days=1)

    def _noon(day) -> datetime:
        return datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)

    alice = uuid.uuid4()
    old, recent = (
        LLMTrace(id=uuid.uuid4(), source="agent-chat", name="chat", started_at=_noon(day), total_cost=2)
        for day in (old_day, recent_day)
    )
    async with _temp_database("feedback_late") as sessionmaker:
        async with sessionmaker() as db:
            db.add_all([old, recent])
            await db.flush()
            for trace, day in ((old, old_day), (recent, recent_day)):
                db.add(_score(trace, alice, 1.0, 5, metadata={"rating": "positive"}))
                db.add(LLMDailyStats(date=day, agent_id=None, user_id=None, model="", source="agent-chat",
                                     trace_count=1, total_cost=2, avg_feedback_score=1.0))
            await db.commit()

        async def _recorded() -> list:
            async with sessionmaker() as db:
                return [row[0] for row in await db.execute(text("SELECT date FROM llm_daily_stats_stale_days"))]

        async def _rollups() -> dict:
            async with sessionmaker() as db:
                return {
                    row.date: (row.trace_count, float(row.total_cost), row.avg_feedback_score)
                    for row in (await db.execute(select(LLMDailyStats))).scalars()
                }

        async def _next_run() -> None:
            async with sessionmaker() as db:
                await run_daily_stats_backfill(db, retention_days=90)

        sink = LocalPGSink(session_factory=sessionmaker)
        rater = {"user_id": str(alice), "name": "user_feedback", "source": "USER", "data_type": "BOOLEAN"}
        acted_at = datetime.now(UTC)
        for trace in (old, recent):
            await sink.flush_score(
                {**rater, "trace_id": str(trace.id), "value": 0.0, "metadata": {"rating": "negative"},
                 "created_at": acted_at}
            )
        assert await _recorded() == [old_day]
        await _next_run()
        assert await _recorded() == []
        assert await _rollups() == {old_day: (1, 2.0, 0.0), recent_day: (1, 2.0, 1.0)}

        # A retraction likewise: that rater has no rating of the day any more.
        await sink.flush_score(
            {**rater, "trace_id": str(old.id), "value": None, "retract": True,
             "created_at": acted_at + timedelta(seconds=1)}
        )
        assert await _recorded() == [old_day]
        await _next_run()
        assert await _recorded() == []
        assert (await _rollups())[old_day] == (1, 2.0, None)


@pytest.mark.asyncio
async def test_a_mark_landing_while_its_day_is_rebuilt_is_kept_for_the_next_run(monkeypatch):
    """A score write marks its day without the rollup lock, so a mark can land
    while that day is being rebuilt, after the rebuild read the day's scores:
    the rebuild clears only the mark it read, and the next one clears this."""
    from app.services import trace_storage
    from app.services.trace_storage import (
        latest_complete_day,
        mark_daily_stats_stale,
        rebuild_daily_stats,
        record_feedback_day_stale,
    )

    day = latest_complete_day() - timedelta(days=10)
    trace = LLMTrace(
        id=uuid.uuid4(), source="agent-chat", name="chat", total_cost=2,
        started_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12),
    )
    async with _temp_database("feedback_mark_race") as sessionmaker:
        async with sessionmaker() as db:
            db.add(trace)
            await db.flush()
            db.add(LLMDailyStats(date=day, agent_id=None, user_id=None, model="", source="agent-chat",
                                 trace_count=1, total_cost=2))
            await db.commit()
            await mark_daily_stats_stale(db, [day])
            await db.commit()

        read_the_day = trace_storage._day_rollup_rows

        async def _a_rating_lands_meanwhile(db, target):
            rows = await read_the_day(db, target)
            async with sessionmaker() as writer:
                assert await record_feedback_day_stale(writer, trace.id) == day
                await writer.commit()
            return rows

        async def _recorded() -> list:
            async with sessionmaker() as db:
                return [row[0] for row in await db.execute(text("SELECT date FROM llm_daily_stats_stale_days"))]

        monkeypatch.setattr(trace_storage, "_day_rollup_rows", _a_rating_lands_meanwhile)
        async with sessionmaker() as db:
            assert await rebuild_daily_stats(db, day) == 1
        assert await _recorded() == [day]

        monkeypatch.setattr(trace_storage, "_day_rollup_rows", read_the_day)
        async with sessionmaker() as db:
            assert await rebuild_daily_stats(db, day) == 1
        assert await _recorded() == []


# A clock that reads the same time again - a coarse clock, two marks in one
# microsecond, a clock set back - installed ahead of pg_catalog on the search
# path of the score write alone.
_FROZEN_CLOCK = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_WAITING_ON_A_LOCK_SQL = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("interleaving", ["committed_before_the_clear", "clear_waits_on_it", "first_mark"])
async def test_a_mark_a_rebuild_did_not_read_survives_it_whatever_the_clock_reads(monkeypatch, interleaving):
    """Two sessions: a rebuild of an older day, and a score write marking that
    day after the rebuild read the day's scores, on a clock that reads exactly
    the ``marked_at`` the rebuild read. The mark still carries a value of its
    own, so the rebuild's clear keeps it, whether the write committed before
    that clear or while the clear waited on it; and a first mark, on a day the
    rebuild found no record for, is kept too. The next rebuild clears it."""
    from app.services import trace_storage
    from app.services.trace_storage import latest_complete_day, rebuild_daily_stats, record_feedback_day_stale

    day = latest_complete_day() - timedelta(days=10)
    trace = LLMTrace(
        id=uuid.uuid4(), source="agent-chat", name="chat", total_cost=2,
        started_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12),
    )
    first_mark = interleaving == "first_mark"
    async with _temp_database("feedback_mark_clock") as sessionmaker:
        async with sessionmaker() as db:
            await db.execute(text("CREATE SCHEMA frozen_clock"))
            await db.execute(
                text(
                    "CREATE FUNCTION frozen_clock.clock_timestamp() RETURNS timestamptz LANGUAGE sql "
                    f"AS $$ SELECT TIMESTAMPTZ '{_FROZEN_CLOCK.isoformat()}' $$"
                )
            )
            db.add(trace)
            await db.flush()
            if not first_mark:
                db.add(LLMDailyStats(date=day, agent_id=None, user_id=None, model="", source="agent-chat",
                                     trace_count=1, total_cost=2))
                # The mark the rebuild reads, at the reading the clock gives again.
                await db.execute(
                    text("INSERT INTO llm_daily_stats_stale_days (date, marked_at) VALUES (:day, :at)"),
                    {"day": day, "at": _FROZEN_CLOCK},
                )
            await db.commit()

        read_the_day = trace_storage._day_rollup_rows
        waits: list[asyncio.Task] = []

        async def _commit_once_the_clear_waits(writer) -> bool:
            waited = False
            try:
                async with sessionmaker() as probe:
                    for _ in range(400):
                        waited = bool(await probe.scalar(text(_WAITING_ON_A_LOCK_SQL)))
                        await probe.rollback()
                        if waited:
                            break
                        await asyncio.sleep(0.025)
            finally:
                await writer.commit()
                await writer.close()
            return waited

        async def _a_rating_lands_meanwhile(db, target):
            rows = await read_the_day(db, target)
            writer = sessionmaker()
            await writer.execute(text("SET LOCAL search_path TO frozen_clock, pg_catalog, public"))
            assert await record_feedback_day_stale(writer, trace.id) == day
            if interleaving == "clear_waits_on_it":
                waits.append(asyncio.create_task(_commit_once_the_clear_waits(writer)))
            else:
                await writer.commit()
                await writer.close()
            return rows

        async def _marks() -> dict:
            async with sessionmaker() as db:
                rows = await db.execute(text("SELECT date, marked_at FROM llm_daily_stats_stale_days"))
                return {row.date: row.marked_at for row in rows}

        monkeypatch.setattr(trace_storage, "_day_rollup_rows", _a_rating_lands_meanwhile)
        async with sessionmaker() as db:
            assert await rebuild_daily_stats(db, day, build_missing=first_mark) == 1
        assert all(await asyncio.gather(*waits))  # the clear did wait on the uncommitted mark
        marks = await _marks()
        assert list(marks) == [day]
        if not first_mark:
            assert marks[day] > _FROZEN_CLOCK

        monkeypatch.setattr(trace_storage, "_day_rollup_rows", read_the_day)
        async with sessionmaker() as db:
            assert await rebuild_daily_stats(db, day) == 1
        assert await _marks() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("retention_days", "recorded"), [(1, True), (90, False)])
async def test_a_retention_shorter_than_the_window_records_the_day_the_refresh_skips(retention_days, recorded):
    """Under a one-day retention the recent refresh reads nothing: a rating
    changed on the one complete day kept records it, off the settings the
    backfill runs with. Under the default retention the refresh reads that day."""
    from app.models.platform_settings import PlatformSettings
    from app.services.trace_storage import latest_complete_day

    latest = latest_complete_day()
    trace = LLMTrace(
        id=uuid.uuid4(), source="agent-chat", name="chat", total_cost=2,
        started_at=datetime.combine(latest, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12),
    )
    alice = uuid.uuid4()
    async with _temp_database("feedback_short_retention") as sessionmaker:
        async with sessionmaker() as db:
            db.add(PlatformSettings(trace_retention_days=retention_days))
            db.add(trace)
            await db.flush()
            db.add(_score(trace, alice, 1.0, 5, metadata={"rating": "positive"}))
            db.add(LLMDailyStats(date=latest, agent_id=None, user_id=None, model="", source="agent-chat",
                                 trace_count=1, total_cost=2, avg_feedback_score=1.0))
            await db.commit()

        await LocalPGSink(session_factory=sessionmaker).flush_score(
            {"trace_id": str(trace.id), "user_id": str(alice), "name": "user_feedback", "source": "USER",
             "data_type": "BOOLEAN", "value": 0.0, "metadata": {"rating": "negative"}, "created_at": datetime.now(UTC)}
        )
        async with sessionmaker() as db:
            days = [row[0] for row in await db.execute(text("SELECT date FROM llm_daily_stats_stale_days"))]
        assert days == ([latest] if recorded else [])
