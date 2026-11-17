from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1 import chat
from app.services.feedback_criteria import ValidatedScore

# What the database clock reads for the action: the sinks order a
# rater's actions by it.
_ACTION_AT = datetime(2026, 9, 27, 12, 0, 0, 123456, tzinfo=UTC)


class _Result:
    def __init__(self, *, scalar=None, rows=(), rowcount: int = 1) -> None:
        self._scalar = scalar
        self._rows = list(rows)
        # What a DELETE reports: how many ratings it removed.
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def all(self):
        return list(self._rows)


class _FakeDB:
    """The feedback write's statements: the rater's score lock and the database
    clock, the feedback row, and the rater's ratings on the trace."""

    def __init__(self, *, select_value=None, trace_ratings=(), rowcount: int = 1) -> None:
        self._select_value = select_value
        self._trace_ratings = [SimpleNamespace(rating=rating, comment=comment) for rating, comment in trace_ratings]
        self._rowcount = rowcount
        self.added: list[object] = []
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, query, params=None):
        sql = str(query)
        self.statements.append(sql)
        if "pg_sleep" in sql:
            return _Result()
        if "clock_timestamp()" in sql:
            return _Result(scalar=_ACTION_AT)
        if "JOIN messages" in sql:
            return _Result(rows=self._trace_ratings)
        return _Result(scalar=self._select_value, rowcount=self._rowcount)

    def add(self, row) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    def took_the_score_lock(self) -> bool:
        return any("pg_advisory_xact_lock" in sql for sql in self.statements)


class _FakeDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.scores: list[dict] = []

    async def record_score(self, score: dict) -> None:
        if self.fail:
            raise RuntimeError("score write failed")
        self.scores.append(score)


def _user():
    return SimpleNamespace(id=uuid4())


def _message(*, with_trace: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        conversation_id=uuid4(),
        llm_trace_id=uuid4() if with_trace else None,
        metadata_json=None,
    )


def _patch(monkeypatch, msg, dispatcher) -> None:
    async def _owned(*_args, **_kwargs):
        return object(), msg

    async def _langfuse(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat, "_get_owned_message", _owned)
    monkeypatch.setattr(chat, "_push_langfuse_feedback_score", _langfuse)
    monkeypatch.setattr(chat, "_retract_langfuse_feedback_score", _langfuse)
    monkeypatch.setattr(chat, "get_tracing_dispatcher", lambda: dispatcher)


async def _upsert(msg, user, db, *, rating: str, comment: str | None):
    return await chat.upsert_message_feedback(
        conversation_id=str(uuid4()),
        message_id=str(msg.id),
        body=chat.MessageFeedbackUpsert(rating=rating, comment=comment),
        user=user,
        db=db,
    )


async def _delete(msg, user, db):
    return await chat.delete_message_feedback(
        conversation_id=str(uuid4()),
        message_id=str(msg.id),
        user=user,
        db=db,
    )


@pytest.mark.asyncio
async def test_upsert_feedback_dispatches_local_score(monkeypatch):
    user = _user()
    msg = _message(with_trace=True)
    db = _FakeDB(select_value=None, trace_ratings=[("positive", "helpful")])
    dispatcher = _FakeDispatcher()
    _patch(monkeypatch, msg, dispatcher)

    response = await _upsert(msg, user, db, rating="positive", comment="  helpful  ")

    assert response.rating == "positive"
    assert response.comment == "helpful"
    assert db.commits == 1
    assert db.took_the_score_lock()
    assert len(dispatcher.scores) == 1
    score = dispatcher.scores[0]
    assert score["trace_id"] == str(msg.llm_trace_id)
    assert score["user_id"] == str(user.id)
    assert score["name"] == "user_feedback"
    assert score["source"] == "USER"
    assert score["data_type"] == "BOOLEAN"
    assert score["value"] == 1.0
    assert score["metadata"] == {"rating": "positive"}
    assert score["comment"] == "helpful"
    # The time of the action, read under the rater's lock before any external call.
    assert score["created_at"] == _ACTION_AT


@pytest.mark.asyncio
async def test_upsert_feedback_skips_local_score_without_trace(monkeypatch):
    user = _user()
    msg = _message(with_trace=False)
    db = _FakeDB(select_value=None)
    dispatcher = _FakeDispatcher()
    _patch(monkeypatch, msg, dispatcher)

    response = await _upsert(msg, user, db, rating="negative", comment=None)

    assert response.rating == "negative"
    assert response.comment is None
    assert db.commits == 1
    assert not db.took_the_score_lock()
    assert dispatcher.scores == []


@pytest.mark.asyncio
async def test_upsert_feedback_local_score_failure_isolated(monkeypatch):
    user = _user()
    msg = _message(with_trace=True)
    db = _FakeDB(select_value=None, trace_ratings=[("positive", "ok")])
    dispatcher = _FakeDispatcher(fail=True)
    _patch(monkeypatch, msg, dispatcher)

    response = await _upsert(msg, user, db, rating="positive", comment="ok")

    assert response.rating == "positive"
    assert response.comment == "ok"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_a_second_rated_message_of_the_turn_averages_the_raters_score(monkeypatch):
    """The segments of one turn share its trace: one score per rater."""
    user = _user()
    msg = _message(with_trace=True)
    db = _FakeDB(select_value=None, trace_ratings=[("positive", "helpful"), ("negative", None)])
    dispatcher = _FakeDispatcher()
    _patch(monkeypatch, msg, dispatcher)

    await _upsert(msg, user, db, rating="positive", comment="helpful")

    (score,) = dispatcher.scores
    assert score["value"] == 0.5
    assert score["metadata"] == {"rating": "positive", "rated_messages": 2}
    assert score["comment"] == "helpful"


@pytest.mark.asyncio
async def test_delete_feedback_dispatches_retraction_local_score(monkeypatch):
    user = _user()
    msg = _message(with_trace=True)
    db = _FakeDB(select_value=None)
    dispatcher = _FakeDispatcher()
    _patch(monkeypatch, msg, dispatcher)

    response = await _delete(msg, user, db)

    assert response.status_code == 204
    assert db.commits == 1
    assert db.took_the_score_lock()
    assert len(dispatcher.scores) == 1
    score = dispatcher.scores[0]
    assert score["trace_id"] == str(msg.llm_trace_id)
    assert score["user_id"] == str(user.id)
    assert score["name"] == "user_feedback"
    assert score["source"] == "USER"
    assert score["data_type"] == "BOOLEAN"
    # A retraction is not a 0 - the sinks keep a NULL-valued score.
    assert score["value"] is None
    assert score["retract"] is True
    assert score["comment"] == "feedback retracted by user"
    assert score["created_at"] == _ACTION_AT


@pytest.mark.asyncio
async def test_delete_feedback_keeps_the_rating_left_on_the_trace(monkeypatch):
    """Retracting one message of a turn leaves the rater's rating of another."""
    user = _user()
    msg = _message(with_trace=True)
    db = _FakeDB(select_value=None, trace_ratings=[("negative", "wrong")])
    dispatcher = _FakeDispatcher()
    _patch(monkeypatch, msg, dispatcher)

    await _delete(msg, user, db)

    (score,) = dispatcher.scores
    assert score["value"] == 0.0
    assert "retract" not in score
    assert score["metadata"] == {"rating": "negative"}
    assert score["comment"] == "wrong"


@pytest.mark.asyncio
async def test_delete_with_nothing_to_delete_records_no_retraction(monkeypatch):
    """A repeated DELETE (double click, retry) removes nothing: it changed no
    rating, so no score goes to the trace store and no "feedback retracted"
    score to Langfuse, whose scores are append-only."""
    user = _user()
    msg = _message(with_trace=True)
    db = _FakeDB(select_value=None, rowcount=0)
    dispatcher = _FakeDispatcher()
    langfuse_retractions: list[object] = []

    async def _owned(*_args, **_kwargs):
        return object(), msg

    async def _langfuse_retract(*args, **_kwargs):
        langfuse_retractions.append(args)

    monkeypatch.setattr(chat, "_get_owned_message", _owned)
    monkeypatch.setattr(chat, "_retract_langfuse_feedback_score", _langfuse_retract)
    monkeypatch.setattr(chat, "get_tracing_dispatcher", lambda: dispatcher)

    response = await chat.delete_message_feedback(
        conversation_id=str(uuid4()),
        message_id=str(msg.id),
        user=user,
        db=db,
    )

    assert response.status_code == 204
    assert db.commits == 1
    assert dispatcher.scores == []
    assert langfuse_retractions == []
    # Nothing is recorded: neither the rater's ratings nor the millisecond are waited on.
    assert not any("JOIN messages" in sql or "pg_sleep" in sql for sql in db.statements)


@pytest.mark.asyncio
async def test_a_scored_rating_of_a_traced_message_counts_as_the_raters_one_score(monkeypatch):
    """The annotations router rates a message through ``apply_message_feedback``
    with criterion scores: the rater's score lock is taken before the
    rating row is read FOR UPDATE, the scores are replaced in the same
    transaction, and the trace store gets the rater's one score."""
    user = _user()
    msg = _message(with_trace=True)
    stored = SimpleNamespace(id=uuid4(), rating="positive", comment=None)
    db = _FakeDB(select_value=stored, trace_ratings=[("negative", "slow")])
    dispatcher = _FakeDispatcher()
    _patch(monkeypatch, msg, dispatcher)
    replaced: list[tuple] = []

    async def _replace_scores(_db, *, message_feedback_id, scores):
        replaced.append((message_feedback_id, scores))

    monkeypatch.setattr(chat, "replace_scores", _replace_scores)
    scores = [ValidatedScore(uuid4(), 2.0, None)]

    feedback = await chat.apply_message_feedback(db, msg, user, "negative", "slow", scores=scores)

    assert feedback is stored and stored.rating == "negative"
    lock = next(i for i, sql in enumerate(db.statements) if "pg_advisory_xact_lock" in sql)
    row = next(i for i, sql in enumerate(db.statements) if "FOR UPDATE" in sql.upper())
    assert lock < row
    assert replaced == [(stored.id, scores)]
    assert db.commits == 1
    (score,) = dispatcher.scores
    assert score["value"] == 0.0
    assert score["metadata"] == {"rating": "negative"}
    assert score["comment"] == "slow"
    assert score["created_at"] == _ACTION_AT


@pytest.mark.asyncio
async def test_delete_feedback_skips_local_score_without_trace(monkeypatch):
    user = _user()
    msg = _message(with_trace=False)
    db = _FakeDB(select_value=None)
    dispatcher = _FakeDispatcher()
    _patch(monkeypatch, msg, dispatcher)

    response = await _delete(msg, user, db)

    assert response.status_code == 204
    assert db.commits == 1
    assert dispatcher.scores == []
