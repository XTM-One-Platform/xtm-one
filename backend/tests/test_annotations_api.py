"""Tests for the polymorphic annotations API.

Every endpoint is exercised as a NON-admin first: each target type resolves under
the read rule of that target's own detail endpoint, a target the caller cannot
read is a 404 before anything is written, and the per-agent views are gated on
the agent's managers like the agent's feedback comments. The SQL the service
emits is inspected where it carries the contract (the upsert's conflict target,
the live-target clause, the union of both storages); the PostgreSQL behaviour
itself is pinned by ``test_annotations_postgres.py``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.v1 import annotations as annotations_api
from app.core.database import get_db
from app.services import annotations as annotation_service
from app.services.feedback_criteria import ValidatedScore

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Scalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _Result:
    def __init__(self, *, first=None, scalar=None, rows=(), rowcount=0, scalars=()):
        self._first = first
        self._scalar = scalar
        self._rows = list(rows)
        self._scalars = list(scalars)
        self.rowcount = rowcount

    def first(self):
        return self._first

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar

    def all(self):
        return list(self._rows)

    def scalars(self):
        return _Scalars(self._scalars)


#: What a rating's response reads after the write: its scores (none here).
_NO_SCORES = _Result(scalars=[])
#: What the agent summary reads for its criteria block, when none exist: the
#: criteria, the scores grouped by value, and the same per day.
_NO_CRITERIA_SUMMARY = (_Result(scalars=[]), _Result(rows=[]), _Result(rows=[]))


class _FakeDb:
    """Scripted results, in call order, per method; every statement recorded."""

    def __init__(self, execute_results=(), scalar_results=()):
        self._execute_results = list(execute_results)
        self._scalar_results = list(scalar_results)
        self.statements: list[object] = []
        self.added: list[object] = []
        self.commits = 0

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def execute(self, statement, params=None):
        self.statements.append(statement)
        if not self._execute_results:
            raise AssertionError(f"Unexpected execute(): {statement}")
        return self._execute_results.pop(0)

    async def scalar(self, statement):
        self.statements.append(statement)
        if not self._scalar_results:
            raise AssertionError(f"Unexpected scalar(): {statement}")
        return self._scalar_results.pop(0)

    async def commit(self):
        self.commits += 1


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


def _user(*, role: str = "user"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        email=f"{role}@example.com",
        role=role,
        is_admin=role == "admin",
    )


def _annotation_row(*, target_type="assignment_run", target_id=None, rating="positive", comment=None):
    """An ``Annotation`` row, or for ``message`` a ``message_feedback`` one."""
    now = datetime.now(UTC)
    target_id = target_id or uuid.uuid4()
    return SimpleNamespace(
        id=uuid.uuid4(),
        target_type=target_type,
        target_id=target_id,
        message_id=target_id if target_type == "message" else None,
        rating=rating,
        comment=comment,
        created_at=now,
        updated_at=now,
    )


class _FakeLangfuse:
    def __init__(self):
        self.scores: list[dict] = []

    def score(self, **payload):
        self.scores.append(payload)


def _client(fake_db, user, monkeypatch, *, langfuse=None):
    async def _get_langfuse_client(_db):
        return langfuse

    monkeypatch.setattr(
        "app.services.langfuse_tracing.get_langfuse_client", _get_langfuse_client
    )
    app = FastAPI()
    app.include_router(annotations_api.router, prefix="/api/v1/annotations")
    app.dependency_overrides[deps.get_current_user] = lambda: user

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def _url(target_type: str, target_id: uuid.UUID, suffix: str = "") -> str:
    return f"/api/v1/annotations/{target_type}/{target_id}{suffix}"


# ---------------------------------------------------------------------------
# Target resolution: each target's own read rule
# ---------------------------------------------------------------------------


def test_rating_an_own_run_is_upserted_and_forwarded_to_langfuse(monkeypatch):
    user = _user()
    run_id, agent_id = uuid.uuid4(), uuid.uuid4()
    stored = _annotation_row(target_id=run_id, rating="negative", comment="Wrong numbers")
    fake_db = _FakeDb(
        execute_results=[
            _Result(first=(agent_id, "lf-trace-1")),  # resolve_target
            _Result(scalar=stored),  # INSERT ... ON CONFLICT ... RETURNING
            _NO_SCORES,
        ]
    )
    langfuse = _FakeLangfuse()
    client = _client(fake_db, user, monkeypatch, langfuse=langfuse)

    resp = client.post(
        _url("assignment_run", run_id),
        json={"rating": "negative", "comment": "  Wrong numbers  "},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_type"] == "assignment_run"
    assert body["target_id"] == str(run_id)
    assert body["rating"] == "negative"
    assert body["comment"] == "Wrong numbers"
    # No criterion scored: the weighted score is the thumbs.
    assert body["scores"] == []
    assert body["weighted_score"] == 0.0

    resolve_sql = _sql(fake_db.statements[0])
    # A non-admin reads own + company-managed runs only (run_visible_filter).
    assert "assignment_runs.user_id" in resolve_sql
    assert "assignment_runs.is_company_managed" in resolve_sql

    upsert = fake_db.statements[1]
    upsert_sql = _sql(upsert)
    assert "on conflict on constraint uq_annotations_target_user do update" in upsert_sql
    params = upsert.compile(dialect=postgresql.dialect()).params
    # The agent is the run's, resolved server-side; the comment is normalized.
    assert params["agent_id"] == agent_id
    assert params["user_id"] == user.id
    assert params["comment"] == "Wrong numbers"
    assert fake_db.commits == 1

    assert langfuse.scores == [
        {
            "name": "user-feedback",
            "value": 0,
            "trace_id": "lf-trace-1",
            "comment": "Wrong numbers",
        }
    ]


def test_a_run_the_caller_cannot_read_is_a_404_and_nothing_is_written(monkeypatch):
    user = _user()
    fake_db = _FakeDb(execute_results=[_Result(first=None)])
    client = _client(fake_db, user, monkeypatch)

    resp = client.post(_url("assignment_run", uuid.uuid4()), json={"rating": "positive"})

    assert resp.status_code == 404
    assert len(fake_db.statements) == 1
    assert fake_db.commits == 0


def test_an_admin_resolves_any_run(monkeypatch):
    """The run detail endpoint shows an admin every run; so does the rating."""
    user = _user(role="admin")
    run_id = uuid.uuid4()
    fake_db = _FakeDb(
        execute_results=[
            _Result(first=(uuid.uuid4(), None)),
            _Result(scalar=None),  # the admin has not rated it yet
        ]
    )
    client = _client(fake_db, user, monkeypatch)

    resp = client.get(_url("assignment_run", run_id))

    assert resp.status_code == 200
    assert resp.json() is None
    resolve_sql = _sql(fake_db.statements[0])
    assert "is_company_managed" not in resolve_sql


@pytest.mark.parametrize(
    ("target_type", "table"),
    [("sub_agent_task", "sub_agent_tasks"), ("agent_delegation", "agent_delegations")],
)
def test_tasks_and_delegations_resolve_for_their_own_user_only(monkeypatch, target_type, table):
    user = _user()
    target_id = uuid.uuid4()
    stored = _annotation_row(target_type=target_type, target_id=target_id)
    fake_db = _FakeDb(
        scalar_results=[uuid.uuid4()],  # the target agent id
        execute_results=[_Result(scalar=stored), _NO_SCORES],
    )
    client = _client(fake_db, user, monkeypatch)

    resp = client.post(_url(target_type, target_id), json={"rating": "positive"})

    assert resp.status_code == 200, resp.text
    resolve_sql = _sql(fake_db.statements[0])
    assert f"{table}.user_id" in resolve_sql
    assert f"{table}.target_agent_id" in resolve_sql


@pytest.mark.parametrize("target_type", ["sub_agent_task", "agent_delegation"])
def test_someone_elses_task_or_delegation_is_a_404(monkeypatch, target_type):
    user = _user()
    fake_db = _FakeDb(scalar_results=[None])
    client = _client(fake_db, user, monkeypatch)

    resp = client.delete(_url(target_type, uuid.uuid4()))

    assert resp.status_code == 404
    assert fake_db.commits == 0


def test_only_a_consultation_is_rated_as_a_delegation(monkeypatch):
    """A background task's delegation carries the task's own result and a
    transfer's the chat's answer: both are rated where that work lives, so the
    same work never counts twice in the agent's feedback."""
    user = _user()
    fake_db = _FakeDb(scalar_results=[None])  # a background_task delegation: no match
    client = _client(fake_db, user, monkeypatch)

    resp = client.post(_url("agent_delegation", uuid.uuid4()), json={"rating": "positive"})

    assert resp.status_code == 404
    assert fake_db.commits == 0
    statement = fake_db.statements[0]
    assert "agent_delegations.type" in _sql(statement)
    assert "consult" in statement.compile(dialect=postgresql.dialect()).params.values()


def test_a_message_resolves_for_the_conversation_owner_only(monkeypatch):
    user = _user()
    fake_db = _FakeDb(execute_results=[_Result(first=None)])
    client = _client(fake_db, user, monkeypatch)

    resp = client.get(_url("message", uuid.uuid4()))

    assert resp.status_code == 404
    resolve_sql = _sql(fake_db.statements[0])
    assert "conversations.user_id" in resolve_sql


# ---------------------------------------------------------------------------
# A message is rated through the chat's own write path
# ---------------------------------------------------------------------------


def test_rating_a_message_is_the_chat_feedback_write(monkeypatch):
    user = _user()
    message_id = uuid.uuid4()
    msg = SimpleNamespace(id=message_id, metadata_json=None, llm_trace_id=None)
    fake_db = _FakeDb(execute_results=[_Result(first=(msg, uuid.uuid4())), _NO_SCORES])
    calls: list[tuple] = []

    async def _apply(db, message, caller, rating, comment, *, scores=None):
        calls.append((message, caller, rating, comment, scores))
        return _annotation_row(target_type="message", target_id=message_id, rating=rating, comment=comment)

    monkeypatch.setattr(annotations_api, "apply_message_feedback", _apply)
    client = _client(fake_db, user, monkeypatch)

    resp = client.post(_url("message", message_id), json={"rating": "positive", "comment": "   "})

    assert resp.status_code == 200, resp.text
    assert resp.json()["target_type"] == "message"
    # A blank comment is stored as none, exactly like the chat endpoint does;
    # no scores sent means the stored ones are left alone.
    assert calls == [(msg, user, "positive", None, None)]


def test_retracting_a_message_rating_is_the_chat_retraction(monkeypatch):
    user = _user()
    message_id = uuid.uuid4()
    msg = SimpleNamespace(id=message_id)
    fake_db = _FakeDb(execute_results=[_Result(first=(msg, None))])
    calls: list[tuple] = []

    async def _retract(db, message, caller):
        calls.append((message, caller))
        return True

    monkeypatch.setattr(annotations_api, "retract_message_feedback", _retract)
    client = _client(fake_db, user, monkeypatch)

    resp = client.delete(_url("message", message_id))

    assert resp.status_code == 204
    assert calls == [(msg, user)]


# ---------------------------------------------------------------------------
# Retraction
# ---------------------------------------------------------------------------


def test_retracting_forwards_to_langfuse_only_when_a_rating_was_deleted(monkeypatch):
    user = _user()
    run_id = uuid.uuid4()
    langfuse = _FakeLangfuse()

    nothing = _FakeDb(execute_results=[_Result(first=(uuid.uuid4(), "lf")), _Result(rowcount=0)])
    resp = _client(nothing, user, monkeypatch, langfuse=langfuse).delete(_url("assignment_run", run_id))
    assert resp.status_code == 204
    assert nothing.commits == 1
    assert langfuse.scores == []

    one = _FakeDb(execute_results=[_Result(first=(uuid.uuid4(), "lf")), _Result(rowcount=1)])
    resp = _client(one, user, monkeypatch, langfuse=langfuse).delete(_url("assignment_run", run_id))
    assert resp.status_code == 204
    assert langfuse.scores == [
        {
            "name": "user-feedback",
            "value": 0,
            "comment": "feedback retracted by user",
            "trace_id": "lf",
        }
    ]
    delete_sql = _sql(one.statements[1])
    assert delete_sql.startswith("delete from annotations")
    assert "annotations.user_id" in delete_sql


def test_a_run_without_a_langfuse_trace_is_rated_without_forwarding(monkeypatch):
    user = _user()
    run_id = uuid.uuid4()
    langfuse = _FakeLangfuse()
    fake_db = _FakeDb(
        execute_results=[
            _Result(first=(uuid.uuid4(), None)),
            _Result(scalar=_annotation_row(target_id=run_id)),
            _NO_SCORES,
        ]
    )

    resp = _client(fake_db, user, monkeypatch, langfuse=langfuse).post(
        _url("assignment_run", run_id), json={"rating": "positive"}
    )

    assert resp.status_code == 200
    assert langfuse.scores == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (f"/api/v1/annotations/tool_call/{uuid.uuid4()}", {"rating": "positive"}),
        (f"/api/v1/annotations/assignment_run/{uuid.uuid4()}", {"rating": "neutral"}),
        (f"/api/v1/annotations/assignment_run/{uuid.uuid4()}", {"rating": "positive", "comment": "x" * 2001}),
    ],
    ids=["unknown-target-type", "unknown-rating", "comment-too-long"],
)
def test_malformed_requests_are_422_before_any_query(monkeypatch, path, body):
    fake_db = _FakeDb()
    client = _client(fake_db, _user(), monkeypatch)

    resp = client.post(path, json=body)

    assert resp.status_code == 422
    assert fake_db.statements == []


@pytest.mark.parametrize("method", ["get", "post", "delete", "stats"])
def test_a_malformed_target_id_answers_like_an_unknown_target(monkeypatch, method):
    """Not an id names no target: the same 404 as an unknown or unreadable one, no query."""
    fake_db = _FakeDb()
    client = _client(fake_db, _user(), monkeypatch)
    path = "/api/v1/annotations/assignment_run/not-a-uuid"

    if method == "post":
        resp = client.post(path, json={"rating": "positive"})
    elif method == "stats":
        resp = client.get(f"{path}/stats")
    else:
        resp = getattr(client, method)(path)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Target not found"
    assert fake_db.statements == []


def test_normalize_comment():
    assert annotation_service.normalize_comment(None) is None
    assert annotation_service.normalize_comment("   ") is None
    assert annotation_service.normalize_comment("  ok \n") == "ok"


# ---------------------------------------------------------------------------
# The caller's own ratings of a list page, in one request
# ---------------------------------------------------------------------------


def _mine_url(target_type: str, target_ids) -> str:
    query = urlencode([("target_type", target_type), *(("target_id", str(i)) for i in target_ids)])
    return f"/api/v1/annotations/mine?{query}"


def test_the_callers_ratings_of_a_page_come_in_one_request(monkeypatch):
    user = _user()
    rated, unrated = uuid.uuid4(), uuid.uuid4()
    row = _annotation_row(target_type="sub_agent_task", target_id=rated, rating="negative", comment="Too long")
    fake_db = _FakeDb(execute_results=[_Result(scalars=[row]), _NO_SCORES])

    resp = _client(fake_db, user, monkeypatch).get(_mine_url("sub_agent_task", [rated, unrated, rated]))

    assert resp.status_code == 200, resp.text
    [annotation] = resp.json()
    assert (annotation["target_id"], annotation["rating"], annotation["comment"]) == (str(rated), "negative", "Too long")
    # One query for the ratings and one for their scores, whatever the page size.
    assert len(fake_db.statements) == 2
    sql = _sql(fake_db.statements[0])
    assert "annotations.user_id =" in sql
    assert "annotations.target_type =" in sql
    assert "annotations.target_id in" in sql
    # A rating of a deleted target is not returned.
    assert "exists" in sql
    params = fake_db.statements[0].compile(dialect=postgresql.dialect()).params
    assert user.id in params.values()
    # Asking twice for a target reads it once.
    assert [value for value in params.values() if isinstance(value, list)] == [[rated, unrated]]


def test_chat_ratings_of_a_page_are_read_from_message_feedback(monkeypatch):
    user = _user()
    message_id = uuid.uuid4()
    row = _annotation_row(target_type="message", target_id=message_id)
    fake_db = _FakeDb(execute_results=[_Result(scalars=[row]), _NO_SCORES])

    resp = _client(fake_db, user, monkeypatch).get(_mine_url("message", [message_id]))

    assert resp.status_code == 200, resp.text
    assert [item["target_id"] for item in resp.json()] == [str(message_id)]
    sql = _sql(fake_db.statements[0])
    assert "from message_feedback" in sql
    assert "message_feedback.user_id =" in sql


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/annotations/mine?target_type=sub_agent_task",
        f"/api/v1/annotations/mine?target_type=tool_call&target_id={uuid.uuid4()}",
        "/api/v1/annotations/mine?target_type=sub_agent_task&target_id=not-a-uuid",
        _mine_url("sub_agent_task", [uuid.uuid4() for _ in range(annotation_service.MAX_OWN_BATCH + 1)]),
    ],
    ids=["no-target-id", "unknown-target-type", "malformed-target-id", "batch-too-large"],
)
def test_a_malformed_page_request_is_422_before_any_query(monkeypatch, url):
    fake_db = _FakeDb()

    resp = _client(fake_db, _user(), monkeypatch).get(url)

    assert resp.status_code == 422
    assert fake_db.statements == []


# ---------------------------------------------------------------------------
# Criterion scores
# ---------------------------------------------------------------------------

_TONE_OPTIONS = [
    {"value": "cold", "label": "Cold", "score": 0.0},
    {"value": "neutral", "label": "Neutral", "score": 0.5},
    {"value": "warm", "label": "Warm", "score": 1.0},
]


def _scale_criterion(*, weight=1.0, name="Accuracy"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        key=name.lower(),
        name=name,
        type="scale",
        weight=weight,
        enabled=True,
        options=None,
        min_value=1.0,
        max_value=5.0,
    )


def _tone_criterion(*, weight=3.0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        key="tone",
        name="Tone",
        type="categorical",
        weight=weight,
        enabled=True,
        options=_TONE_OPTIONS,
        min_value=None,
        max_value=None,
    )


def _stored_score(criterion, *, annotation_id=None, message_feedback_id=None, numeric=None, text=None):
    return SimpleNamespace(
        annotation_id=annotation_id,
        message_feedback_id=message_feedback_id,
        criterion_id=criterion.id,
        value_numeric=numeric,
        value_text=text,
    )


def test_a_rating_is_saved_with_its_scores(monkeypatch):
    accuracy, tone = _scale_criterion(), _tone_criterion()
    run_id = uuid.uuid4()
    stored = _annotation_row(target_id=run_id, rating="negative")
    fake_db = _FakeDb(
        execute_results=[
            _Result(first=(uuid.uuid4(), None)),  # resolve_target
            _Result(scalars=[accuracy, tone]),  # the enabled criteria
            _Result(scalar=stored),  # upsert
            _Result(rowcount=0),  # the previous scores, replaced
            _Result(
                scalars=[
                    _stored_score(accuracy, annotation_id=stored.id, numeric=5.0),
                    _stored_score(tone, annotation_id=stored.id, text="neutral"),
                ]
            ),
            _Result(scalars=[accuracy, tone]),  # the criteria, to weigh them
        ]
    )

    resp = _client(fake_db, _user(), monkeypatch).post(
        _url("assignment_run", run_id),
        json={
            "rating": "negative",
            "scores": [
                {"criterion_id": str(accuracy.id), "value": 5},
                {"criterion_id": str(tone.id), "value": "neutral"},
            ],
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scores"] == [
        {"criterion_id": str(accuracy.id), "value": 5.0},
        {"criterion_id": str(tone.id), "value": "neutral"},
    ]
    # (1 x 1.0 + 3 x 0.5) / 4: the scores decide, not the thumbs down.
    assert body["weighted_score"] == 0.625
    assert _sql(fake_db.statements[3]).startswith("delete from annotation_scores")
    assert [(row.criterion_id, row.value_numeric, row.value_text) for row in fake_db.added] == [
        (accuracy.id, 5.0, None),
        (tone.id, None, "neutral"),
    ]
    assert all(row.annotation_id == stored.id for row in fake_db.added)
    assert fake_db.commits == 1


def test_invalid_scores_are_a_422_before_anything_is_written(monkeypatch):
    accuracy = _scale_criterion()
    fake_db = _FakeDb(execute_results=[_Result(first=(uuid.uuid4(), None)), _Result(scalars=[accuracy])])

    resp = _client(fake_db, _user(), monkeypatch).post(
        _url("assignment_run", uuid.uuid4()),
        json={"rating": "positive", "scores": [{"criterion_id": str(accuracy.id), "value": 9}]},
    )

    assert resp.status_code == 422
    assert "'Accuracy' takes a value from 1 to 5" in resp.json()["detail"]
    assert len(fake_db.statements) == 2
    assert fake_db.added == []
    assert fake_db.commits == 0


def test_an_empty_score_list_clears_the_scores(monkeypatch):
    stored = _annotation_row()
    fake_db = _FakeDb(
        execute_results=[
            _Result(first=(uuid.uuid4(), None)),
            _Result(scalars=[_scale_criterion()]),
            _Result(scalar=stored),
            _Result(rowcount=2),  # the previous scores, removed
            _NO_SCORES,
        ]
    )

    resp = _client(fake_db, _user(), monkeypatch).post(
        _url("assignment_run", stored.target_id), json={"rating": "positive", "scores": []}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["scores"] == []
    assert _sql(fake_db.statements[3]).startswith("delete from annotation_scores")
    assert fake_db.added == []


def test_scores_of_a_message_go_through_the_chat_write(monkeypatch):
    accuracy = _scale_criterion()
    message_id = uuid.uuid4()
    msg = SimpleNamespace(id=message_id, metadata_json=None, llm_trace_id=None)
    row = _annotation_row(target_type="message", target_id=message_id)
    fake_db = _FakeDb(
        execute_results=[
            _Result(first=(msg, uuid.uuid4())),
            _Result(scalars=[accuracy]),
            _Result(scalars=[_stored_score(accuracy, message_feedback_id=row.id, numeric=4.0)]),
            _Result(scalars=[accuracy]),
        ]
    )
    calls: list[object] = []

    async def _apply(db, message, caller, rating, comment, *, scores=None):
        calls.append(scores)
        return row

    monkeypatch.setattr(annotations_api, "apply_message_feedback", _apply)

    resp = _client(fake_db, _user(), monkeypatch).post(
        _url("message", message_id),
        json={"rating": "positive", "scores": [{"criterion_id": str(accuracy.id), "value": 4}]},
    )

    assert resp.status_code == 200, resp.text
    assert calls == [[ValidatedScore(accuracy.id, 4.0, None)]]
    assert resp.json()["weighted_score"] == 0.75
    # The scores of a chat rating are read by its message_feedback id.
    assert "annotation_scores.message_feedback_id in" in _sql(fake_db.statements[2])


@pytest.mark.parametrize(("scores", "locked"), [(None, False), ([], True)])
async def test_a_scored_message_rating_locks_its_row_and_the_thumbs_do_not(monkeypatch, scores, locked):
    """Scores are replaced delete-then-insert, so a scored write holds the rating
    row: a repeated submit waits for the first instead of failing on the scores'
    unique key. The chat bubble's thumbs send no scores and read as on main."""
    from app.api.v1 import chat as chat_api

    statements: list[object] = []

    class _Db:
        async def execute(self, statement, params=None):
            statements.append(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, _row):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

    async def _nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_api, "_push_langfuse_feedback_score", _nothing)
    monkeypatch.setattr(chat_api, "_record_local_feedback_score", _nothing)
    monkeypatch.setattr(chat_api, "replace_scores", _nothing)
    msg = SimpleNamespace(id=uuid.uuid4(), llm_trace_id=None)

    await chat_api.apply_message_feedback(_Db(), msg, _user(), "positive", None, scores=scores)

    assert ("for update" in _sql(statements[0])) is locked


@pytest.mark.parametrize(
    "scores",
    [
        [{"criterion_id": "not-a-uuid", "value": 3}],
        [{"criterion_id": str(uuid.uuid4()), "value": True}],
        [{"criterion_id": str(uuid.uuid4()), "value": [3]}],
        [{"criterion_id": str(uuid.uuid4()), "value": 3}] * 51,
    ],
)
def test_malformed_scores_are_422_before_any_query(monkeypatch, scores):
    fake_db = _FakeDb()

    resp = _client(fake_db, _user(), monkeypatch).post(
        _url("assignment_run", uuid.uuid4()), json={"rating": "positive", "scores": scores}
    )

    assert resp.status_code == 422
    assert fake_db.statements == []


# ---------------------------------------------------------------------------
# Per-target stats
# ---------------------------------------------------------------------------


def test_stats_count_every_users_rating_of_a_readable_target(monkeypatch):
    user = _user()
    run_id = uuid.uuid4()
    fake_db = _FakeDb(
        execute_results=[
            _Result(first=(uuid.uuid4(), None)),
            _Result(rows=[("positive", 3), ("negative", 1)]),
        ]
    )

    resp = _client(fake_db, user, monkeypatch).get(_url("assignment_run", run_id, "/stats"))

    assert resp.status_code == 200
    assert resp.json() == {
        "target_type": "assignment_run",
        "target_id": str(run_id),
        "positive": 3,
        "negative": 1,
        "total": 4,
    }


def test_stats_of_an_unreadable_target_are_a_404(monkeypatch):
    fake_db = _FakeDb(execute_results=[_Result(first=None)])

    resp = _client(fake_db, _user(), monkeypatch).get(_url("assignment_run", uuid.uuid4(), "/stats"))

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# An agent's feedback: list + summary, for the agent's managers
# ---------------------------------------------------------------------------


def _agent(*, owner_id=None, is_company_managed=False, group_permissions=None):
    group_permissions = group_permissions or {}
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        is_company_managed=is_company_managed,
        shared_group_ids=list(group_permissions),
        shared_group_permissions=group_permissions,
        shared_user_ids=[],
        shared_user_permissions={},
        access_restricted=False,
    )


def _agent_client(fake_db, user, agent, monkeypatch, *, group_ids=()):
    async def _get_user_agent(_db, _uid, _agent_id, _group_ids=None, **_kwargs):
        return agent

    async def _get_user_group_ids(_db, _uid):
        return list(group_ids)

    monkeypatch.setattr(annotation_service, "get_user_agent", _get_user_agent)
    monkeypatch.setattr(annotation_service, "get_user_group_ids", _get_user_group_ids)
    return _client(fake_db, user, monkeypatch)


def _list_url(agent, **params) -> str:
    return "/api/v1/annotations?" + urlencode({"agent_id": str(agent.id), **params})


def _summary_url(agent) -> str:
    return "/api/v1/annotations/summary?" + urlencode({"agent_id": str(agent.id)})


def _list_row(*, target_type="assignment_run", conversation_id=None, rating="negative", comment="Bad"):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        target_type=target_type,
        target_id=uuid.uuid4(),
        conversation_id=conversation_id,
        rating=rating,
        comment=comment,
        user_name="Jane Doe",
        user_email="jane@example.com",
        source="web",
        emoji=None,
        user_id=uuid.uuid4(),
        created_at=now,
        updated_at=now,
    )


def test_the_owner_lists_every_rating_of_the_agents_work(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    conversation_id = uuid.uuid4()
    run_row = _list_row()
    fake_db = _FakeDb(
        scalar_results=[2],
        execute_results=[
            _Result(
                rows=[
                    _list_row(target_type="message", conversation_id=conversation_id, rating="positive", comment=None),
                    run_row,
                ]
            ),
            _NO_SCORES,  # the page's stored ratings
            _NO_SCORES,  # the page's chat ratings
            _Result(rows=[(run_row.target_id,)]),  # the page's runs the caller may open
        ],
    )

    resp = _agent_client(fake_db, user, agent, monkeypatch).get(_list_url(agent))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 2
    assert [item["target_type"] for item in data["items"]] == ["message", "assignment_run"]
    assert data["items"][0]["conversation_id"] == str(conversation_id)
    assert data["items"][1]["conversation_id"] is None
    assert data["items"][1]["user_name"] == "Jane Doe"
    # No criterion scores: each rating weighs as its thumbs.
    assert [item["weighted_score"] for item in data["items"]] == [1.0, 0.0]
    # The owner opens the agent's chats and this run.
    assert [item["can_open"] for item in data["items"]] == [True, True]

    # [count, page, scores of the page]: the page query is the second one.
    list_sql = _sql(fake_db.statements[1])
    # Both storages, each scoped to this agent, and only ratings of live targets.
    assert "union all" in list_sql
    assert "from message_feedback" in list_sql
    assert "conversations.agent_id" in list_sql
    # The agent's answers only: a rating of the user's own prompt is not its work.
    assert "messages.role" in list_sql
    assert "annotations.agent_id" in list_sql
    assert "exists (select assignment_runs.id" in list_sql
    assert "exists (select sub_agent_tasks.id" in list_sql
    assert "exists (select agent_delegations.id" in list_sql
    assert "order by" in list_sql and "updated_at desc" in list_sql


def test_a_stored_type_filter_leaves_chat_ratings_out(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb(scalar_results=[0], execute_results=[_Result(rows=[])])

    resp = _agent_client(fake_db, user, agent, monkeypatch).get(
        _list_url(agent, target_type="sub_agent_task", rating="negative")
    )

    assert resp.status_code == 200
    list_sql = _sql(fake_db.statements[-1])
    assert "message_feedback" not in list_sql
    assert "union all" not in list_sql
    assert "annotations.target_type" in list_sql
    assert "annotations.rating" in list_sql


def test_the_message_filter_reads_chat_ratings_only(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb(scalar_results=[0], execute_results=[_Result(rows=[])])

    resp = _agent_client(fake_db, user, agent, monkeypatch).get(_list_url(agent, target_type="message"))

    assert resp.status_code == 200
    list_sql = _sql(fake_db.statements[-1])
    assert "from message_feedback" in list_sql
    assert "from annotations" not in list_sql


def test_the_search_matches_the_comment_literally(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb(scalar_results=[0], execute_results=[_Result(rows=[])])

    resp = _agent_client(fake_db, user, agent, monkeypatch).get(
        _list_url(agent, target_type="assignment_run", search="50%_off")
    )

    assert resp.status_code == 200
    params = fake_db.statements[-1].compile(dialect=postgresql.dialect()).params
    assert "%50\\%\\_off%" in params.values()


def test_a_non_manager_is_refused_before_any_query(monkeypatch):
    user = _user()
    agent = _agent(owner_id=uuid.uuid4())
    fake_db = _FakeDb()
    client = _agent_client(fake_db, user, agent, monkeypatch)

    assert client.get(_list_url(agent)).status_code == 403
    assert client.get(_summary_url(agent)).status_code == 403
    assert fake_db.statements == []


def test_a_company_managed_agent_is_admin_only(monkeypatch):
    group_id = str(uuid.uuid4())
    agent = _agent(is_company_managed=True, group_permissions={group_id: "config"})

    member = _agent_client(_FakeDb(), _user(), agent, monkeypatch, group_ids=[group_id])
    assert member.get(_summary_url(agent)).status_code == 403

    admin_db = _FakeDb(execute_results=[_Result(rows=[]), _Result(rows=[]), *_NO_CRITERIA_SUMMARY])
    admin = _agent_client(admin_db, _user(role="admin"), agent, monkeypatch)
    assert admin.get(_summary_url(agent)).status_code == 200


def test_a_config_grant_reads_the_agents_feedback(monkeypatch):
    group_id = str(uuid.uuid4())
    agent = _agent(owner_id=uuid.uuid4(), group_permissions={group_id: "config"})
    fake_db = _FakeDb(scalar_results=[0], execute_results=[_Result(rows=[])])

    resp = _agent_client(fake_db, _user(), agent, monkeypatch, group_ids=[group_id]).get(_list_url(agent))

    assert resp.status_code == 200


def test_a_manager_is_linked_only_to_the_work_they_may_open(monkeypatch):
    """A config grant reads every rating, but opens a colleague's run page only
    under the run's own rule, and the agent's chats only with a chats grant."""
    group_id = str(uuid.uuid4())
    agent = _agent(owner_id=uuid.uuid4(), group_permissions={group_id: "config"})
    fake_db = _FakeDb(
        scalar_results=[2],
        execute_results=[
            _Result(rows=[_list_row(target_type="message", conversation_id=uuid.uuid4()), _list_row()]),
            _NO_SCORES,
            _NO_SCORES,
            _Result(rows=[]),  # the run is the owner's, not the caller's
        ],
    )

    resp = _agent_client(fake_db, _user(), agent, monkeypatch, group_ids=[group_id]).get(_list_url(agent))

    assert resp.status_code == 200, resp.text
    assert [item["can_open"] for item in resp.json()["items"]] == [False, False]
    runs_sql = _sql(fake_db.statements[-1])
    assert runs_sql.startswith("select assignment_runs.id")
    # A non-admin opens own + company-managed runs only (run_visible_filter).
    assert "assignment_runs.user_id" in runs_sql
    assert "assignment_runs.is_company_managed" in runs_sql


def test_the_summary_reports_every_target_type(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    fake_db = _FakeDb(
        execute_results=[
            _Result(rows=[("positive", 6), ("negative", 2)]),  # message_feedback
            _Result(rows=[("assignment_run", "negative", 2), ("unknown_type", "positive", 9)]),
            *_NO_CRITERIA_SUMMARY,
        ]
    )

    resp = _agent_client(fake_db, user, agent, monkeypatch).get(_summary_url(agent))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["positive"] == 6
    assert data["negative"] == 4
    assert data["total"] == 10
    assert data["positive_ratio"] == 60.0
    by_type = {item["target_type"]: item for item in data["by_target_type"]}
    assert set(by_type) == {
        "message", "assignment_run", "sub_agent_task", "agent_delegation", "agent_account_reply"
    }
    assert by_type["message"]["positive_ratio"] == 75.0
    assert by_type["assignment_run"] == {
        "target_type": "assignment_run",
        "positive": 0,
        "negative": 2,
        "total": 2,
        "positive_ratio": 0.0,
    }
    assert by_type["sub_agent_task"]["positive_ratio"] is None
    # No criteria: no per-criterion block, and the weighted score is the share
    # of thumbs up.
    assert data["criteria"] == []
    assert data["weighted_score"] == 0.6
    # A chat rating counts as the agent's work only on one of its answers, not
    # on the user's own prompt - here and in the per-criterion scores.
    # [message counts, stored counts, criteria, scores by value, scores by day]
    assert "messages.role" in _sql(fake_db.statements[0])
    assert "messages.role" in _sql(fake_db.statements[3])
    stored_sql = _sql(fake_db.statements[1])
    assert "annotations.agent_id" in stored_sql
    assert "exists" in stored_sql


def test_the_summary_reports_each_criterion(monkeypatch):
    user = _user()
    agent = _agent(owner_id=user.id)
    accuracy = _scale_criterion()
    today = datetime(2026, 9, 27, tzinfo=UTC)
    fake_db = _FakeDb(
        execute_results=[
            _Result(rows=[("positive", 3), ("negative", 1)]),
            _Result(rows=[]),
            _Result(scalars=[accuracy]),
            _Result(rows=[(accuracy.id, 5.0, None, 3), (accuracy.id, 1.0, None, 1)]),
            _Result(rows=[(accuracy.id, today, 5.0, None, 3), (accuracy.id, today, 1.0, None, 1)]),
        ]
    )

    resp = _agent_client(fake_db, user, agent, monkeypatch).get(_summary_url(agent))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    [criterion] = data["criteria"]
    assert criterion["criterion_id"] == str(accuracy.id)
    assert (criterion["count"], criterion["mean_score"]) == (4, 0.75)
    assert [(bucket["label"], bucket["count"]) for bucket in criterion["histogram"]] == [
        ("1", 1),
        ("2", 0),
        ("3", 0),
        ("4", 0),
        ("5", 3),
    ]
    assert criterion["daily"] == [{"date": "2026-09-27", "count": 4, "mean_score": 0.75}]
    # The criteria decide the agent's weighted score once they hold scores.
    assert data["weighted_score"] == 0.75
    # Both storages are read: chat ratings through their message, the others
    # through the annotation of a live target.
    scores_sql = _sql(fake_db.statements[3])
    assert "join message_feedback" in scores_sql
    assert "join annotations" in scores_sql
    assert "union all" in scores_sql


# ---------------------------------------------------------------------------
# Orphan sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_orphan_sweep_deletes_only_known_types_whose_target_is_gone(monkeypatch):
    fake_db = _FakeDb(execute_results=[_Result(rowcount=3)])

    @asynccontextmanager
    async def _session():
        yield fake_db

    monkeypatch.setattr("app.core.database.async_session", _session)

    purged = await annotation_service.purge_orphan_annotations(limit=50)

    assert purged == 3
    assert fake_db.commits == 1
    statement = fake_db.statements[0]
    sql = _sql(statement)
    assert sql.startswith("delete from annotations")
    for table in ("assignment_runs", "sub_agent_tasks", "agent_delegations"):
        assert f"not (exists (select {table}.id" in sql
    params = statement.compile(dialect=postgresql.dialect()).params
    assert set(params.values()) >= {"assignment_run", "sub_agent_task", "agent_delegation", 50}
