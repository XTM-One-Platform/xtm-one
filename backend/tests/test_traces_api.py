from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pytest import fixture, mark, raises
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.v1 import traces as traces_api
from app.core.config import settings
from app.core.database import get_db
from app.services.tracing.read_store import TRACE_LIST_MAX_OFFSET, page_is_past_end


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)

    def first(self):
        return self._values[0] if self._values else None


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _ScalarListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)


class _RowResult:
    """Fake result that returns rows via .all() (for queries with add_columns)."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _TraceListRow(SimpleNamespace):
    """Row-like object for list_traces() joined results.

    ``list_traces`` reads the base trace as ``row[0]`` and joined metadata via
    attributes (``row.agent_name``, ``row.agent_icon``, ``row.user_email``).
    """

    def __getitem__(self, idx):
        if idx == 0:
            return self.trace
        raise IndexError(idx)


class _FakeDb:
    def __init__(self, execute_results=None, scalar_results=None):
        self._execute_results = list(execute_results or [])
        self._scalar_results = list(scalar_results or [])
        self.execute_statements = []
        self.scalar_statements = []
        self.added = []
        self.deleted = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.execute_statements.append(statement)
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        if not self._scalar_results:
            raise AssertionError("Unexpected scalar() call")
        return self._scalar_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    async def refresh(self, obj):
        pass


@fixture(autouse=True)
def _no_group_manager(monkeypatch):
    """No caller here manages a group, and sessions and agents name no person.

    The manager reads have their own tests (``test_group_managers_postgres.py``):
    these keep their strict fake database to the queries they always made.
    """

    async def _manages_nothing(_db, _user_id):
        return []

    async def _no_session_users(_db, _conversation_ids):
        return {}

    async def _no_agent_owners(_db, _agent_ids):
        return {}

    async def _no_agent_access(_db, _agent_ids, **_kwargs):
        return {}

    monkeypatch.setattr("app.services.group_managers.managed_groups", _manages_nothing)
    monkeypatch.setattr(traces_api, "session_user_emails", _no_session_users)
    monkeypatch.setattr(traces_api, "session_conversation_titles", _no_session_users)
    monkeypatch.setattr(traces_api, "agent_owners", _no_agent_owners)
    monkeypatch.setattr(traces_api, "agent_row_access", _no_agent_access)


def _user(*, is_admin: bool = True):
    # Most traces tests use an admin user by default; non-admin observability
    # coverage is exercised explicitly in dedicated scoped-access tests.
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="admin@example.com" if is_admin else "user@example.com",
        is_admin=is_admin,
        role="admin" if is_admin else "user",
    )


def _trace_obj(*, user_id: uuid.UUID | None = None):
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=None,
        user_id=user_id,
        conversation_id=None,
        assignment_run_id=None,
        parent_trace_id=None,
        source="general-chat",
        name="trace-name",
        model="gpt-5.3-codex",
        environment="production",
        metadata_json={},
        input={},
        output={},
        tags=[],
        started_at=now,
        ended_at=now,
        duration_ms=12.5,
        input_tokens=10,
        output_tokens=12,
        total_tokens=22,
        input_cost=0.1,
        output_cost=0.2,
        total_cost=0.3,
        status="ok",
        error_message=None,
        level="DEFAULT",
        created_at=now,
        updated_at=now,
    )


def _client(fake_db: _FakeDb, fake_user, trace_store=None):
    app = FastAPI()
    app.include_router(traces_api.router, prefix="/api/v1/traces")
    app.dependency_overrides[deps.get_current_user] = lambda: fake_user

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db
    if trace_store is not None:
        app.dependency_overrides[traces_api.require_trace_read_store] = lambda: trace_store
    return TestClient(app)


def test_trace_visible_query_non_admin_has_ga_guard():
    user = _user(is_admin=False)
    q = traces_api.trace_visible_query(user, [str(uuid.uuid4())])
    rendered = str(
        q.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "general-chat" in rendered
    assert "llm_traces.user_id" in rendered
    assert "agents.is_company_managed" in rendered


def test_list_traces_uses_keyset_cursor_and_limit(monkeypatch):
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces",
            params={
                "cursor": "2026-06-16T00:00:00Z",
                "cursor_id": str(uuid.uuid4()),
                "limit": 50,
            },
        )

    assert resp.status_code == 200
    sql = str(fake_db.execute_statements[0]).lower()
    assert "started_at" in sql
    assert "order by" in sql
    assert "llm_traces.id" in sql


def test_list_traces_delegates_to_resolved_read_store(monkeypatch):
    fake_db = _FakeDb()
    fake_user = _user()

    class _Store:
        provider = "postgres"

        def __init__(self):
            self.called = False

        async def list_traces(self, *_args, **_kwargs):
            self.called = True
            return [], 0

    store = _Store()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user, trace_store=store) as client:
        resp = client.get("/api/v1/traces")

    assert resp.status_code == 200
    assert store.called is True
    assert fake_db.execute_statements == []
    assert fake_db.scalar_statements == []


def test_traces_api_accepts_clickhouse_provider_selection(monkeypatch):
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(settings, "tracing_store_provider", "clickhouse")

    with _client(fake_db, fake_user) as client:
        resp = client.get("/api/v1/traces")

    assert resp.status_code == 503
    assert "ClickHouse read client is not configured" in resp.json()["detail"]


def test_list_traces_supports_multi_value_filters(monkeypatch):
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    fake_user = _user()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces",
            params=[
                ("source", "openai"),
                ("source", "anthropic"),
                ("agent_id", str(agent_a)),
                ("agent_id", str(agent_b)),
                ("model", "gpt-5.3-codex"),
                ("model", "claude-opus-4.6"),
            ],
        )

    assert resp.status_code == 200
    compiled = str(
        fake_db.execute_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "llm_traces.source in ('openai', 'anthropic')" in compiled
    assert str(agent_a) in compiled
    assert str(agent_b) in compiled
    assert "llm_traces.model in ('gpt-5.3-codex', 'claude-opus-4.6')" in compiled


def test_list_traces_filters_by_failure_cause(monkeypatch):
    """The Trace Explorer's filter reaches SQL as an IN over the column,
    not as a search over ``error_message`` — the whole point of the structured
    cause is that it is aggregable."""
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces",
            params=[
                ("failure_cause", "provider_rate_limited"),
                ("failure_cause", "context_overflow"),
            ],
        )

    assert resp.status_code == 200
    compiled = str(
        fake_db.execute_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert (
        "llm_traces.failure_cause in ('provider_rate_limited', 'context_overflow')"
        in compiled
    )


def test_list_traces_unrecorded_selects_unclassified_rows(monkeypatch):
    """``unrecorded`` is a sentinel, not a cause: it means "no classification
    at all", which is a different finding from the explicit ``unknown``
    bucket — a gap in instrumentation rather than a gap in the taxonomy."""
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces",
            params=[("failure_cause", "unrecorded"), ("failure_cause", "unknown")],
        )

    assert resp.status_code == 200
    compiled = str(
        fake_db.execute_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "llm_traces.failure_cause in ('unknown')" in compiled
    assert "llm_traces.failure_cause is null" in compiled
    # ``unknown`` is a recorded cause and needs no status guard; ``unrecorded``
    # is a NULL check and does — otherwise it sweeps in every success.
    assert "llm_traces.status = 'error'" in compiled


def test_unrecorded_never_matches_a_successful_trace(monkeypatch):
    """The regression guard for the sentinel's status predicate.

    A successful trace also has ``failure_cause IS NULL``, and on any real
    install successes vastly outnumber failures — so a bare NULL check turns
    "failed traces nobody classified" into a page of successes, and the
    instrumentation-gap metric it exists to expose becomes unreadable.
    """
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get("/api/v1/traces", params={"failure_cause": "unrecorded"})

    assert resp.status_code == 200
    compiled = str(
        fake_db.execute_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    # The NULL check must be conjoined with the failed-status predicate, never
    # stand alone.
    assert "llm_traces.failure_cause is null" in compiled
    assert "llm_traces.status = 'error'" in compiled


def test_list_traces_rejects_an_unknown_failure_cause(monkeypatch):
    """A typo must be a 422, not a silently empty page — an empty result reads
    as "nothing is failing that way", which is the opposite of the truth."""
    fake_db = _FakeDb()
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get("/api/v1/traces", params={"failure_cause": "provider_grumpy"})

    assert resp.status_code == 422
    assert "provider_grumpy" in resp.json()["detail"]


def _compiled(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


def test_list_traces_filters_by_assignment_run(monkeypatch):
    """The run detail's "View traces" shortcut narrows the page and its total
    to the traces that run recorded."""
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    run_id = uuid.uuid4()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, _user()) as client:
        resp = client.get("/api/v1/traces", params={"assignment_run_id": str(run_id)})

    assert resp.status_code == 200
    predicate = f"llm_traces.assignment_run_id = '{run_id}'"
    assert predicate in _compiled(fake_db.execute_statements[0])
    assert predicate in _compiled(fake_db.scalar_statements[0])


def test_list_traces_filters_by_assignment_through_its_runs(monkeypatch):
    """A trace carries its run, not its assignment: the assignment filter goes
    through the run table, on the page and on the total alike."""
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    assignment_id = uuid.uuid4()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, _user()) as client:
        resp = client.get("/api/v1/traces", params={"assignment_id": str(assignment_id)})

    assert resp.status_code == 200
    predicate = (
        "llm_traces.assignment_run_id in (select assignment_runs.id \nfrom assignment_runs \n"
        f"where assignment_runs.assignment_id = '{assignment_id}')"
    )
    assert predicate in _compiled(fake_db.execute_statements[0])
    assert predicate in _compiled(fake_db.scalar_statements[0])


@mark.parametrize("param", ["assignment_id", "assignment_run_id"])
@mark.parametrize("value", ["not-a-uuid", "", " "])
def test_list_traces_rejects_a_malformed_assignment_filter(monkeypatch, param, value):
    fake_db = _FakeDb()

    class _Store:
        provider = "postgres"
        calls = 0

        async def list_traces(self, *_args, **_kwargs):
            _Store.calls += 1
            return [], 0

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, _user(), trace_store=_Store()) as client:
        resp = client.get("/api/v1/traces", params={param: value})

    assert resp.status_code == 422
    assert _Store.calls == 0


def test_an_assignment_filter_does_not_stand_in_for_the_agent_scope(monkeypatch):
    """A non-admin still names the agents they may read: narrowing by
    assignment or run never widens what they see."""
    fake_db = _FakeDb()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, _user(is_admin=False)) as client:
        resp = client.get(
            "/api/v1/traces",
            params={"assignment_id": str(uuid.uuid4()), "assignment_run_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 403
    assert fake_db.execute_statements == []
    assert fake_db.scalar_statements == []


def test_list_traces_accepts_string_input_output_payloads(monkeypatch):
    fake_db = _FakeDb(scalar_results=[1])
    fake_user = _user()
    trace = _trace_obj(user_id=fake_user.id)
    trace.input = '{"messages":["hello"]}'
    trace.output = "plain text response"
    fake_db._execute_results = [
        _RowResult(
            [
                _TraceListRow(
                    trace=trace,
                    agent_name="Support Bot",
                    agent_icon="headset",
                    user_email="user@example.com",
                )
            ]
        )
    ]

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get("/api/v1/traces")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["input"] == '{"messages":["hello"]}'
    assert body["items"][0]["output"] == "plain text response"


def test_list_traces_sorts_by_requested_column_and_pages_by_offset(monkeypatch):
    # The total must reach past the offset, or the page is known to be empty
    # and the store never issues the page query this test inspects.
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[500])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces",
            params={"sort_by": "total_cost", "sort_dir": "asc", "offset": 50, "limit": 50},
        )

    assert resp.status_code == 200
    compiled = str(
        fake_db.execute_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    # Empty costs stay at the bottom whichever way the column is sorted, and id
    # breaks ties so OFFSET paging cannot repeat or skip a row.
    assert "order by llm_traces.total_cost asc nulls last, llm_traces.id desc" in compiled
    assert "offset 50" in compiled


def test_list_traces_ignores_a_stale_cursor_when_sorting_by_another_column(monkeypatch):
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces",
            params={
                "sort_by": "duration_ms",
                "sort_dir": "desc",
                "cursor": "2026-06-16T00:00:00Z",
                "cursor_id": str(uuid.uuid4()),
            },
        )

    assert resp.status_code == 200
    compiled = str(
        fake_db.execute_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    # The keyset predicate is a `<` on started_at: it only describes the default
    # newest-first order, so applying it to a duration sort would silently drop
    # rows. A leftover cursor must be ignored, not honoured.
    assert "order by llm_traces.duration_ms desc nulls last" in compiled
    assert "started_at <" not in compiled


@mark.parametrize("params", [{"sort_by": "agent_name"}, {"sort_dir": "sideways"}])
def test_list_traces_rejects_a_sort_it_cannot_order_by(monkeypatch, params):
    fake_db = _FakeDb()
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get("/api/v1/traces", params=params)

    assert resp.status_code == 422
    assert fake_db.execute_statements == []


def test_list_trace_sessions_sorts_by_requested_column_and_pages_by_offset(monkeypatch):
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[500])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces/sessions",
            params={"sort_by": "error_count", "sort_dir": "desc", "offset": 100},
        )

    assert resp.status_code == 200
    compiled = str(
        fake_db.execute_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "order by anon_1.error_count desc, anon_1.conversation_id asc" in compiled
    assert "offset 100" in compiled


def _compiled_list_query(monkeypatch, path: str, params: dict) -> str:
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[500])

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, _user()) as client:
        resp = client.get(path, params=params)

    assert resp.status_code == 200
    return str(
        fake_db.execute_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


@mark.parametrize(
    ("sort_by", "sort_dir", "forward", "mirrored"),
    [
        (
            "total_cost",
            "desc",
            "llm_traces.total_cost desc nulls last, llm_traces.id desc",
            "llm_traces.total_cost asc nulls first, llm_traces.id asc",
        ),
        (
            "duration_ms",
            "asc",
            "llm_traces.duration_ms asc nulls last, llm_traces.id desc",
            "llm_traces.duration_ms desc nulls first, llm_traces.id asc",
        ),
        ("name", "asc", "llm_traces.name asc, llm_traces.id desc", "llm_traces.name desc, llm_traces.id asc"),
        (
            "started_at",
            "desc",
            "llm_traces.started_at desc, llm_traces.id desc",
            "llm_traces.started_at asc, llm_traces.id asc",
        ),
    ],
)
def test_list_traces_reverse_reads_the_exact_mirror_of_the_order(monkeypatch, sort_by, sort_dir, forward, mirrored):
    """A far-end read is only the same page counted from the other end when
    EVERY term flips: the column, the NULLs pinned to the bottom (now first)
    and the ``id`` tie-breaker. Flipping ``sort_dir`` alone keeps NULLs and ties
    where they were, so the explorer's last page would show the wrong rows."""
    params = {"sort_by": sort_by, "sort_dir": sort_dir, "offset": 50}

    assert f"order by {forward}" in _compiled_list_query(monkeypatch, "/api/v1/traces", params)
    compiled = _compiled_list_query(monkeypatch, "/api/v1/traces", {**params, "reverse": "true"})
    assert f"order by {mirrored}" in compiled
    assert "offset 50" in compiled


@mark.parametrize(
    ("sort_by", "sort_dir", "mirrored"),
    [
        ("error_count", "desc", "anon_1.error_count asc, anon_1.conversation_id desc"),
        ("conversation_id", "asc", "anon_1.conversation_id desc, anon_1.conversation_id desc"),
        ("last_activity_at", "desc", "anon_1.last_activity_at asc, anon_1.conversation_id desc"),
    ],
)
def test_list_trace_sessions_reverse_reads_the_exact_mirror_of_the_order(monkeypatch, sort_by, sort_dir, mirrored):
    compiled = _compiled_list_query(
        monkeypatch,
        "/api/v1/traces/sessions",
        {"sort_by": sort_by, "sort_dir": sort_dir, "offset": 100, "reverse": "true"},
    )

    assert f"order by {mirrored}" in compiled
    assert "offset 100" in compiled


@mark.parametrize(
    ("path", "keyset_predicate"),
    [("/api/v1/traces", "started_at <"), ("/api/v1/traces/sessions", "last_activity_at <")],
)
def test_a_far_end_read_of_the_default_order_ignores_the_keyset_cursor(monkeypatch, path, keyset_predicate):
    """The cursor is a ``<`` on the newest-first order read from its first row:
    applied to the mirrored read it would drop the very rows asked for."""
    compiled = _compiled_list_query(
        monkeypatch,
        path,
        {"cursor": "2026-06-16T00:00:00Z", "cursor_id": str(uuid.uuid4()), "reverse": "true"},
    )

    assert keyset_predicate not in compiled


@mark.parametrize("path", ["/api/v1/traces", "/api/v1/traces/sessions"])
def test_a_far_end_read_keeps_the_offset_ceiling(monkeypatch, path):
    """``reverse`` moves where the offset counts from, never how deep it may go."""
    fake_db = _FakeDb()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, _user()) as client:
        refused = client.get(path, params={"offset": TRACE_LIST_MAX_OFFSET + 1, "reverse": "true"})

    assert refused.status_code == 422
    assert fake_db.execute_statements == []
    assert fake_db.scalar_statements == []


@mark.parametrize("path", ["/api/v1/traces", "/api/v1/traces/sessions"])
def test_trace_lists_refuse_an_offset_past_the_ceiling(monkeypatch, path):
    """A hand-crafted deep page must be refused before any database work,
    or one request can make the database read and discard the whole table."""
    fake_db = _FakeDb()
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        refused = client.get(path, params={"offset": TRACE_LIST_MAX_OFFSET + 1})

    assert refused.status_code == 422
    assert fake_db.execute_statements == []


def test_list_traces_accepts_the_ceiling_itself(monkeypatch):
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[TRACE_LIST_MAX_OFFSET + 50])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces",
            params={"sort_by": "total_cost", "offset": TRACE_LIST_MAX_OFFSET},
        )

    assert resp.status_code == 200
    assert len(fake_db.execute_statements) == 1


@mark.parametrize("path", ["/api/v1/traces", "/api/v1/traces/sessions"])
def test_trace_lists_skip_the_page_query_past_the_last_row(monkeypatch, path):
    """An offset beyond the matching rows is an empty page by definition: the
    total is answered, the page query is never issued."""
    fake_db = _FakeDb(scalar_results=[10])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(path, params={"sort_by": "total_cost", "offset": 50})

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 10
    assert fake_db.execute_statements == []


def test_page_is_past_end_never_skips_the_first_page():
    # The first page (and the keyset path) always runs, even on an empty set.
    assert page_is_past_end(0, 0) is False
    assert page_is_past_end(0, 10) is False
    assert page_is_past_end(9, 10) is False
    assert page_is_past_end(10, 10) is True
    assert page_is_past_end(50, 10) is True


def test_list_trace_sessions_rejects_a_sort_it_cannot_order_by(monkeypatch):
    fake_db = _FakeDb()
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        # A trace column that has no meaning once traces are grouped per session.
        resp = client.get("/api/v1/traces/sessions", params={"sort_by": "duration_ms"})

    assert resp.status_code == 422
    assert fake_db.execute_statements == []


def test_list_trace_sessions_uses_keyset_cursor_and_limit(monkeypatch):
    fake_db = _FakeDb(
        execute_results=[_RowResult([])],
        scalar_results=[0],
    )
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(
            "/api/v1/traces/sessions",
            params={
                "cursor": "2026-06-16T00:00:00Z",
                "cursor_id": str(uuid.uuid4()),
                "limit": 50,
            },
        )

    assert resp.status_code == 200
    sql = str(fake_db.execute_statements[0]).lower()
    assert "last_activity_at" in sql
    assert "conversation_id" in sql
    assert "order by" in sql


def test_list_trace_sessions_returns_grouped_metrics(monkeypatch):
    conversation_id = uuid.uuid4()
    fake_db = _FakeDb(
        execute_results=[
            _RowResult(
                [
                    SimpleNamespace(
                        conversation_id=conversation_id,
                        first_activity_at=datetime(2026, 6, 22, 8, 0, tzinfo=UTC),
                        last_activity_at=datetime(2026, 6, 22, 8, 5, tzinfo=UTC),
                        trace_count=3,
                        error_count=1,
                        agent_count=2,
                        model_count=2,
                        input_tokens=100,
                        output_tokens=50,
                        total_tokens=150,
                        total_cost=0.42,
                    )
                ]
            )
        ],
        scalar_results=[1],
    )
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    async def _titles(_db, _conversation_ids):
        return {str(conversation_id): "Quarterly phishing review"}

    monkeypatch.setattr(traces_api, "session_conversation_titles", _titles)

    with _client(fake_db, fake_user) as client:
        resp = client.get("/api/v1/traces/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    # A session is named by its conversation's title, never by its id alone.
    assert body["items"][0]["conversation_title"] == "Quarterly phishing review"
    assert body["items"][0]["trace_count"] == 3
    assert body["items"][0]["error_count"] == 1
    assert body["items"][0]["agent_count"] == 2
    assert body["items"][0]["model_count"] == 2
    assert body["items"][0]["total_tokens"] == 150


def test_get_trace_returns_404_when_not_visible(monkeypatch):
    fake_db = _FakeDb(execute_results=[_ScalarResult(None)])
    fake_user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, fake_user) as client:
        resp = client.get(f"/api/v1/traces/{uuid.uuid4()}")

    assert resp.status_code == 404


def test_export_trace_bundle_returns_zip(monkeypatch):
    user = _user()
    trace = _trace_obj(user_id=user.id)
    span = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=trace.id,
        parent_span_id=None,
        type="SPAN",
        name="root",
        model=None,
        model_parameters=None,
        input={},
        output={},
        metadata_json={},
        level="DEFAULT",
        status_message=None,
        started_at=datetime.now(UTC),
        ended_at=None,
        duration_ms=1.2,
        time_to_first_token=None,
        input_tokens=None,
        output_tokens=None,
        input_cost=None,
        output_cost=None,
        total_cost=None,
        usage_details=None,
        tool_call_id=None,
        tool_arguments=None,
        stop_reason=None,
        position=0,
        created_at=datetime.now(UTC),
    )
    score = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=trace.id,
        span_id=None,
        user_id=user.id,
        name="user_feedback",
        source="USER",
        data_type="BOOLEAN",
        value=1.0,
        string_value=None,
        comment=None,
        metadata_json={},
        created_at=datetime.now(UTC),
    )

    class _Store:
        provider = "postgres"

        async def get_trace_detail(self, *_args, **_kwargs):
            return trace, "Agent", None, None

        async def get_trace_spans(self, *_args, **_kwargs):
            return [span]

        async def get_trace_scores(self, *_args, **_kwargs):
            return [score]

    fake_db = _FakeDb()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user, trace_store=_Store()) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/export-bundle")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(BytesIO(resp.content), "r") as zf:
        names = sorted(zf.namelist())
        assert "manifest.json" in names
        assert f"traces/0001-{trace.id}/trace.json" in names
        assert f"traces/0001-{trace.id}/spans.json" in names
        assert f"traces/0001-{trace.id}/scores.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["export_mode"] == "agent_span"
        # The first span item is the synthetic trace root injected by
        # _build_trace_span_items, surfaced as root_span_id in the manifest.
        assert manifest["root_span_id"] == f"t-{trace.id}"
        # trace.json must agree with scores.json: one score was returned by the
        # store, so has_scores is True in the exported trace payload.
        exported_trace = json.loads(zf.read(f"traces/0001-{trace.id}/trace.json"))
        assert exported_trace["has_scores"] is True
        exported_scores = json.loads(zf.read(f"traces/0001-{trace.id}/scores.json"))
        assert len(exported_scores) == 1

    audit_entries = [obj for obj in fake_db.added if getattr(obj, "action", None) == "export_bundle"]
    assert len(audit_entries) == 1
    assert audit_entries[0].entity_type == "trace_bundle"
    assert audit_entries[0].entity_id == trace.id


def test_export_session_trace_bundle_returns_zip(monkeypatch):
    user = _user()
    trace = _trace_obj(user_id=user.id)
    trace.conversation_id = uuid.uuid4()
    row = _TraceListRow(
        trace=trace,
        agent_name="Agent",
        agent_icon=None,
        user_email=None,
        # Deliberately stale list-query probe: the exported trace.json must
        # derive has_scores from the materialized scores, not this flag.
        has_scores=False,
    )
    span = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=trace.id,
        parent_span_id=None,
        type="SPAN",
        name="root",
        model=None,
        model_parameters=None,
        input={},
        output={},
        metadata_json={},
        level="DEFAULT",
        status_message=None,
        started_at=datetime.now(UTC),
        ended_at=None,
        duration_ms=1.2,
        time_to_first_token=None,
        input_tokens=None,
        output_tokens=None,
        input_cost=None,
        output_cost=None,
        total_cost=None,
        usage_details=None,
        tool_call_id=None,
        tool_arguments=None,
        stop_reason=None,
        position=0,
        created_at=datetime.now(UTC),
    )
    score = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=trace.id,
        span_id=None,
        user_id=user.id,
        name="user_feedback",
        source="USER",
        data_type="BOOLEAN",
        value=1.0,
        string_value=None,
        comment=None,
        metadata_json={},
        created_at=datetime.now(UTC),
    )

    class _Store:
        provider = "postgres"

        async def list_traces(self, *_args, **kwargs):
            if kwargs.get("cursor") is None:
                return [row], 1
            return [], 1

        async def get_spans_for_trace_ids(self, *_args, **_kwargs):
            return {str(trace.id): [span]}

        async def get_scores_for_trace_ids(self, *_args, **_kwargs):
            return {str(trace.id): [score]}

    fake_db = _FakeDb()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user, trace_store=_Store()) as client:
        resp = client.get(f"/api/v1/traces/sessions/{trace.conversation_id}/export-bundle")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(BytesIO(resp.content), "r") as zf:
        names = sorted(zf.namelist())
        assert "manifest.json" in names
        assert "session.json" in names
        assert f"traces/0001-{trace.id}/trace.json" in names
        # trace.json must agree with scores.json in the same entry: the row's
        # stale has_scores=False probe is overridden by the materialized score.
        exported_trace = json.loads(zf.read(f"traces/0001-{trace.id}/trace.json"))
        assert exported_trace["has_scores"] is True
        exported_scores = json.loads(zf.read(f"traces/0001-{trace.id}/scores.json"))
        assert len(exported_scores) == 1

    audit_entries = [obj for obj in fake_db.added if getattr(obj, "action", None) == "export_bundle"]
    assert len(audit_entries) == 1
    assert audit_entries[0].entity_type == "trace_bundle"
    assert audit_entries[0].entity_id == trace.conversation_id


def test_export_session_trace_bundle_empty_session_returns_404(monkeypatch):
    user = _user()

    class _Store:
        provider = "postgres"

        async def list_traces(self, *_args, **_kwargs):
            return [], 0

    fake_db = _FakeDb()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user, trace_store=_Store()) as client:
        resp = client.get(f"/api/v1/traces/sessions/{uuid.uuid4()}/export-bundle")

    assert resp.status_code == 404
    assert fake_db.added == []


class _ClockResult:
    """The database clock a score action is timed on."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def scalar_one(self):
        return self._now


def _score_write_results(trace, existing, *, action_at: datetime) -> list:
    """The PostgreSQL score write, statement by statement."""
    return [
        _ScalarResult(trace),  # trace visibility lookup
        _ScalarListResult([]),  # the rater's advisory lock (ignored)
        _ClockResult(action_at),  # the time of the action
        _ScalarListResult(existing),  # the rater's existing score
        _ScalarListResult([]),  # held until the action's millisecond is over (ignored)
    ]


@fixture
def _recorded_traces(monkeypatch) -> list:
    """The traces whose rollup day a score write records stale, with the
    number of commits its session had seen by then."""
    calls: list = []

    async def _record(db, trace_id, **_kwargs):
        calls.append((trace_id, db.commits))

    monkeypatch.setattr("app.services.trace_storage.record_feedback_day_stale", _record)
    return calls


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def test_create_score_forces_user_source(monkeypatch, _recorded_traces):
    user = _user()
    action_at = datetime.now(UTC)
    trace = _trace_obj(user_id=user.id)
    fake_db = _FakeDb(execute_results=_score_write_results(trace, [], action_at=action_at))

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user) as client:
        resp = client.post(
            f"/api/v1/traces/{uuid.uuid4()}/scores",
            json={
                "name": "user_feedback",
                "data_type": "BOOLEAN",
                "value": 1.0,
                "source": "SYSTEM",
            },
        )

    assert resp.status_code == 201
    assert len(fake_db.added) == 1
    score = fake_db.added[0]
    assert score.source == "USER"
    assert score.user_id == user.id
    assert score.created_at == action_at
    # Version 7, so a copy into ClickHouse never reads as a legacy retraction.
    assert score.id.version == 7
    assert fake_db.commits == 1
    # The read-then-write is guarded by a per-(trace, user, name) advisory lock.
    assert any("pg_advisory_xact_lock" in str(stmt) for stmt in fake_db.execute_statements)
    # The feedback of the trace's day changed: its rollup is recorded stale, in
    # the score's own transaction, if the recent refresh no longer reads that day.
    assert _recorded_traces == [(trace.id, 0)]


def test_create_score_updates_existing_feedback_instead_of_duplicating(monkeypatch, _recorded_traces):
    """Re-submitting feedback for the same (trace, user, name) UPDATES the
    existing score rather than inserting a duplicate that would skew
    avg_feedback_score."""
    user = _user()
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        span_id=None,
        user_id=user.id,
        name="user_feedback",
        source="USER",
        data_type="BOOLEAN",
        value=1.0,
        string_value=None,
        comment=None,
        metadata_json={},
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    action_at = datetime.now(UTC)
    fake_db = _FakeDb(execute_results=_score_write_results(_trace_obj(user_id=user.id), [existing], action_at=action_at))

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user) as client:
        resp = client.post(
            f"/api/v1/traces/{uuid.uuid4()}/scores",
            json={"name": "user_feedback", "data_type": "BOOLEAN", "value": 0.0},
        )

    assert resp.status_code == 201
    # No new row inserted; the existing score was mutated in place.
    assert fake_db.added == []
    assert existing.value == 0.0
    # And holds the time of this action: the tracing sink refuses a
    # delayed chat rating older than it.
    assert existing.created_at == action_at
    assert fake_db.commits == 1
    # The read-then-write is guarded by a per-(trace, user, name) advisory lock.
    assert any("pg_advisory_xact_lock" in str(stmt) for stmt in fake_db.execute_statements)
    assert len(_recorded_traces) == 1


def test_create_score_leaves_a_later_action_in_place(monkeypatch, _recorded_traces):
    """The rater's score is their latest action: an admin write timed before
    the stored one changes nothing."""
    user = _user()
    action_at = datetime.now(UTC)
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=uuid.uuid4(),
        span_id=None,
        user_id=user.id,
        name="user_feedback",
        source="USER",
        data_type="BOOLEAN",
        value=1.0,
        string_value=None,
        comment=None,
        metadata_json={"rating": "positive"},
        created_at=action_at + timedelta(seconds=1),
    )
    results = _score_write_results(_trace_obj(user_id=user.id), [existing], action_at=action_at)
    fake_db = _FakeDb(execute_results=results[:-1])

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user) as client:
        resp = client.post(
            f"/api/v1/traces/{uuid.uuid4()}/scores",
            json={"name": "user_feedback", "data_type": "BOOLEAN", "value": 0.0},
        )

    assert resp.status_code == 201
    assert resp.json()["value"] == 1.0
    assert existing.value == 1.0
    assert fake_db.added == []
    assert fake_db.commits == 1
    # Nothing changed, so no rollup is stale.
    assert _recorded_traces == []


def test_a_score_under_another_name_records_no_rollup_day(monkeypatch, _recorded_traces):
    """Only ``user_feedback`` scores feed a rollup's feedback."""
    user = _user()
    fake_db = _FakeDb(
        execute_results=_score_write_results(_trace_obj(user_id=user.id), [], action_at=datetime.now(UTC))
    )

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user) as client:
        resp = client.post(
            f"/api/v1/traces/{uuid.uuid4()}/scores",
            json={"name": "accuracy", "data_type": "NUMERIC", "value": 0.5},
        )

    assert resp.status_code == 201
    assert _recorded_traces == []


def test_a_failed_rollup_record_never_fails_the_admin_score_write(monkeypatch):
    """The record is best effort, in a savepoint of the score's transaction:
    here its trace lookup fails, and the score is still written."""
    user = _user()
    fake_db = _FakeDb(
        execute_results=_score_write_results(_trace_obj(user_id=user.id), [], action_at=datetime.now(UTC))
    )
    fake_db.begin_nested = _Savepoint  # type: ignore[attr-defined]

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user) as client:
        resp = client.post(
            f"/api/v1/traces/{uuid.uuid4()}/scores",
            json={"name": "user_feedback", "data_type": "BOOLEAN", "value": 1.0},
        )

    assert resp.status_code == 201
    assert len(fake_db.scalar_statements) == 1  # the record's trace lookup, which failed
    assert len(fake_db.added) == 1
    assert fake_db.commits == 1


def test_delete_trace_forbidden_for_non_admin():
    fake_db = _FakeDb()
    with _client(fake_db, _user(is_admin=False)) as client:
        resp = client.delete(f"/api/v1/traces/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_list_traces_forbids_non_admin_without_agent_scope(monkeypatch):
    fake_db = _FakeDb()
    user = _user(is_admin=False)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces")

    assert resp.status_code == 403
    assert "agent-scoped" in resp.json()["detail"].lower()


def test_list_traces_allows_non_admin_owner_with_agent_scope(monkeypatch):
    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    user = _user(is_admin=False)
    scoped_agent_id = uuid.uuid4()

    async def _groups(_db, _user_id):
        return []

    async def _load_agents(_db, agent_ids):
        return {
            scoped_agent_id: SimpleNamespace(
                id=scoped_agent_id,
                user_id=user.id,
                is_company_managed=False,
                shared_group_ids=[],
                shared_group_permissions={},
            )
        }

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_load_agents_by_ids", _load_agents)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces", params={"agent_id": str(scoped_agent_id)})

    assert resp.status_code == 200


def test_list_traces_forbids_non_admin_read_only_group_scope(monkeypatch):
    fake_db = _FakeDb()
    user = _user(is_admin=False)
    scoped_agent_id = uuid.uuid4()
    shared_group_id = str(uuid.uuid4())

    async def _groups(_db, _user_id):
        return [shared_group_id]

    async def _load_agents(_db, agent_ids):
        return {
            scoped_agent_id: SimpleNamespace(
                id=scoped_agent_id,
                user_id=uuid.uuid4(),
                is_company_managed=False,
                shared_group_ids=[shared_group_id],
                shared_group_permissions={shared_group_id: "read_only"},
            )
        }

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_load_agents_by_ids", _load_agents)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces", params={"agent_id": str(scoped_agent_id)})

    assert resp.status_code == 403


#: A trace holds the conversation it records, so an agent's traces follow the rule for its
#: chats (``helpers.can_view_agent_chats``): configuring the agent is not enough.
_TRACE_TIERS = [("config_with_chats", 200), ("config", 403), ("read_only", 403)]


def _granted_agent(agent_id, user, tier, *, via_group, group_id):
    if via_group:
        return SimpleNamespace(
            id=agent_id,
            user_id=uuid.uuid4(),
            is_company_managed=False,
            shared_group_ids=[group_id],
            shared_group_permissions={group_id: tier},
        )
    return SimpleNamespace(
        id=agent_id,
        user_id=uuid.uuid4(),
        is_company_managed=False,
        shared_user_ids=[str(user.id)],
        shared_user_permissions={str(user.id): tier},
        shared_group_ids=[],
        shared_group_permissions={},
    )


@mark.parametrize("via_group", [False, True], ids=["direct", "group"])
@mark.parametrize(("tier", "status"), _TRACE_TIERS)
def test_a_non_admin_reads_a_trace_only_with_chat_access(monkeypatch, tier, status, via_group):
    user = _user(is_admin=False)
    trace = _trace_obj(user_id=user.id)
    trace.agent_id = uuid.uuid4()
    group_id = str(uuid.uuid4())

    class _Store:
        provider = "postgres"

        async def get_trace_detail(self, *_args, **_kwargs):
            return trace, "Agent", None, None

    async def _groups(_db, _user_id):
        return [group_id]

    async def _load_agents(_db, agent_ids):
        return {trace.agent_id: _granted_agent(trace.agent_id, user, tier, via_group=via_group, group_id=group_id)}

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_load_agents_by_ids", _load_agents)

    with _client(_FakeDb(), user, trace_store=_Store()) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}")

    assert resp.status_code == status
    if status == 403:
        assert resp.json()["detail"] == traces_api.TRACE_ACCESS_DETAIL


@mark.parametrize("via_group", [False, True], ids=["direct", "group"])
@mark.parametrize(("tier", "status"), _TRACE_TIERS)
def test_a_non_admin_lists_an_agents_traces_only_with_chat_access(monkeypatch, tier, status, via_group):
    user = _user(is_admin=False)
    agent_id = uuid.uuid4()
    group_id = str(uuid.uuid4())

    async def _groups(_db, _user_id):
        return [group_id]

    async def _load_agents(_db, agent_ids):
        return {agent_id: _granted_agent(agent_id, user, tier, via_group=via_group, group_id=group_id)}

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_load_agents_by_ids", _load_agents)

    fake_db = _FakeDb(execute_results=[_RowResult([])], scalar_results=[0])
    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces", params={"agent_id": str(agent_id)})

    assert resp.status_code == status


def test_a_non_admin_reads_no_trace_of_a_company_managed_agent(monkeypatch):
    user = _user(is_admin=False)
    agent_id = uuid.uuid4()

    async def _groups(_db, _user_id):
        return []

    async def _load_agents(_db, agent_ids):
        return {
            agent_id: SimpleNamespace(
                id=agent_id,
                user_id=uuid.uuid4(),
                is_company_managed=True,
                shared_group_ids=[],
                shared_group_permissions={},
            )
        }

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_load_agents_by_ids", _load_agents)

    with _client(_FakeDb(), user) as client:
        resp = client.get("/api/v1/traces", params={"agent_id": str(agent_id)})

    assert resp.status_code == 403


def test_the_agent_observability_tab_follows_the_rule_the_api_applies():
    """The tab reads the agent's traces, so it is offered on the server's ``can_view_chats``."""
    from pathlib import Path

    pages = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
    page = (pages / "AgentDetail.tsx").read_text(encoding="utf-8")
    # The tab list lives in its own module, pinned by its test.
    tabs = (pages / "agent-detail" / "tabs.ts").read_text(encoding="utf-8")
    assert '...(canViewChats ? [{ key: "observability" as AgentTab' in tabs
    assert "agentDetailTabs({ canManageMemory, canViewChats," in page
    assert 'tab === "observability" && canViewChats && <ObservabilityTab' in page


def test_get_spans_returns_trace_spans(monkeypatch):
    user = _user()
    trace = _trace_obj(user_id=user.id)
    span = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=trace.id,
        parent_span_id=None,
        type="SPAN",
        name="root",
        model=None,
        model_parameters=None,
        input={},
        output={},
        metadata_json={},
        level="DEFAULT",
        status_message=None,
        started_at=datetime.now(UTC),
        ended_at=None,
        duration_ms=1.2,
        time_to_first_token=None,
        input_tokens=None,
        output_tokens=None,
        input_cost=None,
        output_cost=None,
        total_cost=None,
        usage_details=None,
        tool_call_id=None,
        tool_arguments=None,
        stop_reason=None,
        position=0,
        created_at=datetime.now(UTC),
    )
    # get_trace_spans resolves the trace + user email (get_trace_detail), then
    # re-resolves the visible trace and loads its spans (get_trace_spans).
    fake_db = _FakeDb(
        execute_results=[
            _ScalarResult(trace),
            _ScalarResult(None),
            _ScalarResult(trace),
            _ScalarListResult([span]),
        ]
    )

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans")

    assert resp.status_code == 200
    body = resp.json()
    # The builder prepends a synthetic trace-root span (named after the agent,
    # or "Agent" when there is none) ahead of the real spans.
    assert body["total"] == 2
    assert body["items"][0]["name"] == "Agent"
    assert body["items"][0]["metadata"]["synthetic_root"] is True
    assert body["items"][1]["name"] == "root"


@mark.parametrize(
    ("stored", "expected"),
    [
        # Already-serialized JSON object -> parsed back into a dict.
        ('{"bundle": {"type": "bundle"}}', {"bundle": {"type": "bundle"}}),
        # Opaque legacy text -> stable {"raw": ...} envelope.
        ("plain text arguments", {"raw": "plain text arguments"}),
        # Whitespace-only legacy value carries no content -> null, not {"raw": "   "}.
        ("   ", None),
        # JSON string that is not an object -> {"raw": ...} (schema wants dict|None).
        ("[1, 2]", {"raw": "[1, 2]"}),
        # Native dicts pass through untouched.
        ({"query": "q"}, {"query": "q"}),
        (None, None),
    ],
)
def test_get_spans_coerces_legacy_string_tool_arguments(monkeypatch, stored, expected):
    user = _user()
    trace = _trace_obj(user_id=user.id)
    span = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=trace.id,
        parent_span_id=None,
        type="TOOL",
        name="tool:bundle_import",
        model=None,
        model_parameters=None,
        input={},
        output={},
        metadata_json={},
        level="DEFAULT",
        status_message=None,
        started_at=datetime.now(UTC),
        ended_at=None,
        duration_ms=1.2,
        time_to_first_token=None,
        input_tokens=None,
        output_tokens=None,
        input_cost=None,
        output_cost=None,
        total_cost=None,
        usage_details=None,
        tool_call_id="call_1",
        tool_arguments=stored,
        stop_reason=None,
        position=0,
        created_at=datetime.now(UTC),
    )
    fake_db = _FakeDb(
        execute_results=[
            _ScalarResult(trace),
            _ScalarResult(None),
            _ScalarResult(trace),
            _ScalarListResult([span]),
        ]
    )

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, user) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][1]["name"] == "tool:bundle_import"
    assert body["items"][1]["tool_arguments"] == expected


def test_delete_trace_admin_success(monkeypatch):
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    # Deletion now reads the trace and its spans first, to collect the offloaded
    # payload objects that the rows point at (see the offload test below).
    fake_db = _FakeDb(
        execute_results=[
            _ScalarResult(trace),          # trace detail
            _ScalarResult(admin.email),    # detail: owner email
            _ScalarResult(trace),          # spans: visibility check
            _ScalarListResult([]),         # spans
            _ScalarResult(trace),          # delete_trace: visibility check
        ],
        scalar_results=[],
    )

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(fake_db, admin) as client:
        resp = client.delete(f"/api/v1/traces/{trace.id}")
    assert resp.status_code == 204
    assert fake_db.deleted == [trace]
    assert fake_db.commits == 1


def test_delete_trace_removes_offloaded_payload_objects(monkeypatch):
    """A deleted row must take its object-storage payloads and media with it.

    Otherwise every purged trace leaves unreachable objects in the bucket
    forever — nothing else records the keys.  The cleanup sweeps the trace's
    whole ``traces/<trace_id>/`` prefix rather than collecting pointers out of
    the rows, because a media blob referenced from *inside* an offloaded payload
    is not visible in the rows at all.
    """
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    fake_db = _FakeDb(
        execute_results=[
            _ScalarResult(trace),  # delete_trace: visibility check
        ],
        scalar_results=[],
    )

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    owned = {
        f"traces/{trace.id}/trace/out.json": b"{}",
        f"traces/{trace.id}/s1/media/0001-media.png": b"\x89PNG",
    }
    other = "traces/00000000-0000-0000-0000-000000000000/trace/out.json"

    class _Storage:
        def __init__(self) -> None:
            self.objects = {**owned, other: b"{}"}
            self.deleted: list[str] = []

        async def list_object_names(self, prefix):
            return [n for n in self.objects if n.startswith(prefix)]

        async def delete(self, object_name):
            self.deleted.append(object_name)
            self.objects.pop(object_name, None)

    storage = _Storage()
    monkeypatch.setattr("app.services.file_storage.file_storage", storage)

    with _client(fake_db, admin) as client:
        resp = client.delete(f"/api/v1/traces/{trace.id}")

    assert resp.status_code == 204
    assert sorted(storage.deleted) == sorted(owned)
    assert other in storage.objects, "another trace's objects must be untouched"


def test_ga_trace_invisible_to_other_user():
    """AC 9: non-admin cannot see another user's general-chat trace via visibility query."""
    owner = _user(is_admin=False)
    other = _user(is_admin=False)
    q = traces_api.trace_visible_query(other, [])
    rendered = str(
        q.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    # The GA filter binds user_id to the querying user, not the trace owner
    assert str(other.id) in rendered
    assert str(owner.id) not in rendered
    # Verify the general-chat filter requires user_id match
    assert "general-chat" in rendered
    assert "llm_traces.user_id" in rendered


def test_analytics_filters_model_parses_csv_strings():
    parsed = traces_api.AnalyticsFilters(
        period="7d",
        date_from=None,
        date_to=None,
        agent_ids="a,b",
        models="m1,m2",
        sources="agent-chat,assignment",
    )
    assert parsed.period == "7d"
    assert parsed.agent_ids == "a,b"
    assert parsed.models == "m1,m2"
    assert parsed.sources == "agent-chat,assignment"


def test_resolve_date_range_returns_previous_window():
    start, end, prev_start, prev_end = traces_api._resolve_date_range("7d", None, None)
    assert end > start
    assert prev_end == start
    assert prev_start < prev_end
    assert (end - start) == (prev_end - prev_start)


def test_resolve_date_range_custom_requires_dates():
    with raises(HTTPException):
        traces_api._resolve_date_range("custom", None, None)


def test_use_daily_stats_boundary():
    start = datetime.now(UTC) - timedelta(days=7)
    end = datetime.now(UTC)
    assert traces_api._use_daily_stats(start, end) is False
    assert traces_api._use_daily_stats(start - timedelta(seconds=1), end) is True


def test_build_trace_filters_applies_visibility_and_param_filters(monkeypatch):
    user = _user()
    groups = [str(uuid.uuid4())]
    filters = traces_api.AnalyticsFilters(
        period="30d",
        date_from=None,
        date_to=None,
        agent_ids=str(uuid.uuid4()),
        models="gpt-5.3-codex",
        sources="agent-chat,assignment",
    )
    start = datetime.now(UTC) - timedelta(days=30)
    end = datetime.now(UTC)

    query = traces_api._build_trace_filters(filters, user=user, user_group_ids=groups, start=start, end=end)
    rendered = str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "llm_traces.started_at" in rendered
    assert "llm_traces.model" in rendered
    assert "llm_traces.source in ('agent-chat', 'assignment')" in rendered


def test_build_stats_filters_excludes_null_agent_for_non_admin():
    user = _user(is_admin=False)
    filters = traces_api.AnalyticsFilters(period="90d")
    start = datetime.now(UTC) - timedelta(days=90)
    end = datetime.now(UTC)

    query = traces_api._build_stats_filters(filters, user=user, user_group_ids=[], start=start, end=end)
    rendered = str(
        query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "llm_daily_stats.date" in rendered
    assert "llm_daily_stats.agent_id is not null" in rendered


def test_analytics_summary_returns_previous_block_from_raw(monkeypatch):
    now = datetime.now(UTC)
    fake_db = _FakeDb(
        execute_results=[
            _RowResult(
                [
                    SimpleNamespace(
                        trace_count=5,
                        total_cost=1.5,
                        total_tokens=123,
                        avg_duration_ms=25.0,
                        error_count=1,
                        avg_feedback_score=0.8,
                    )
                ]
            ),
            _RowResult(
                [
                    SimpleNamespace(
                        trace_count=4,
                        total_cost=1.0,
                        total_tokens=100,
                        avg_duration_ms=20.0,
                        error_count=0,
                        avg_feedback_score=0.9,
                    )
                ]
            ),
            _RowResult([SimpleNamespace(last_aggregation_at=now - timedelta(hours=72))]),
            _RowResult([SimpleNamespace(unknown_cost_trace_count=3)]),
            # Coverage probe: earliest rollup date, then earliest raw trace.
            _ScalarResult(None),
            _ScalarResult(now - timedelta(days=3)),
        ]
    )
    user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: False)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces/analytics/summary", params={"period": "7d"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["trace_count"] == 5
    assert data["previous"]["trace_count"] == 4
    assert "previous" in data
    assert data["aggregation_stale"] is True
    assert data["unknown_cost_trace_count"] == 3
    assert data["last_aggregation_at"] is not None
    # No agent_ids filter -> quota/message metrics are skipped entirely
    # (no extra unbounded aggregation queries) and defaults apply.
    assert data["quota_consumed"] == 0
    assert data["message_count"] == 0
    assert data["avg_quota_per_message"] is None
    sql = str(fake_db.execute_statements[0]).lower()
    assert "llm_traces" in sql


def test_analytics_summary_agent_filter_returns_quota_message_metrics(monkeypatch):
    now = datetime.now(UTC)
    agent_id = uuid.uuid4()
    fake_db = _FakeDb(
        execute_results=[
            _RowResult(
                [
                    SimpleNamespace(
                        trace_count=5,
                        total_cost=1.5,
                        total_tokens=123,
                        avg_duration_ms=25.0,
                        error_count=1,
                        avg_feedback_score=0.8,
                    )
                ]
            ),
            _RowResult(
                [
                    SimpleNamespace(
                        trace_count=4,
                        total_cost=1.0,
                        total_tokens=100,
                        avg_duration_ms=20.0,
                        error_count=0,
                        avg_feedback_score=0.9,
                    )
                ]
            ),
            _RowResult([SimpleNamespace(last_aggregation_at=now - timedelta(hours=1))]),
            _RowResult([SimpleNamespace(unknown_cost_trace_count=0)]),
            # Coverage probe: earliest rollup date, then earliest raw trace.
            _ScalarResult(None),
            _ScalarResult(now - timedelta(days=3)),
            # Quota/message metrics (_load_quota_message_metrics): summed
            # attributed quota, then agent-conversation USER message count.
            _ScalarResult(12),
            _ScalarResult(4),
        ]
    )
    user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: False)

    with _client(fake_db, user) as client:
        resp = client.get(
            "/api/v1/traces/analytics/summary",
            params={"period": "7d", "agent_ids": str(agent_id)},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["quota_consumed"] == 12
    assert data["message_count"] == 4
    assert data["avg_quota_per_message"] == 3.0
    # The avg denominator counts user messages only (per-interaction
    # semantics): agentic quota increments once per user turn. Anonymous
    # conversations (user_id IS NULL) are excluded because anonymous users
    # bypass agentic quota metering and would deflate the average.
    message_sql = str(fake_db.execute_statements[-1]).lower()
    assert "messages" in message_sql
    assert "role" in message_sql
    assert "conversations.user_id is not null" in message_sql


def test_analytics_cost_by_model_uses_daily_stats_for_long_period(monkeypatch):
    fake_db = _FakeDb(
        execute_results=[
            # SET LOCAL jit = off, before any rollup-served read.
            _RowResult([]),
            _RowResult(
                [
                    SimpleNamespace(
                        model="gpt-5.3-codex",
                        trace_count=10,
                        input_tokens=100,
                        output_tokens=50,
                        input_cost=1.0,
                        output_cost=2.0,
                        total_cost=3.0,
                    )
                ]
            )
        ]
    )
    user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: True)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces/analytics/cost-by-model", params={"period": "90d"})

    assert resp.status_code == 200
    assert resp.json()["items"][0]["model"] == "gpt-5.3-codex"
    assert str(fake_db.execute_statements[0]).lower() == "set local jit = off"
    sql = str(fake_db.execute_statements[1]).lower()
    assert "llm_daily_stats" in sql


def test_analytics_cost_over_time_shape(monkeypatch):
    fake_db = _FakeDb(
        execute_results=[
            _RowResult([SimpleNamespace(date="2026-06-19", model="gpt-5.3-codex", cost=1.2)])
        ]
    )
    user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: False)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces/analytics/cost-over-time")

    assert resp.status_code == 200
    assert resp.json()["series"][0]["model"] == "gpt-5.3-codex"


def test_analytics_volume_over_time_shape(monkeypatch):
    fake_db = _FakeDb(
        execute_results=[
            _RowResult([SimpleNamespace(date="2026-06-19", source="assignment", count=8)])
        ]
    )
    user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: False)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces/analytics/volume-over-time")

    assert resp.status_code == 200
    assert resp.json()["series"][0]["source"] == "assignment"


def test_analytics_latency_shape(monkeypatch):
    fake_db = _FakeDb(
        execute_results=[
            _RowResult([SimpleNamespace(p50=1.0, p75=2.0, p90=3.0, p95=4.0, p99=5.0)]),
            _RowResult([SimpleNamespace(model="gpt-5.3-codex", p50=1.0, p75=2.0, p90=3.0, p95=4.0, p99=5.0, trace_count=7)]),
            _RowResult([SimpleNamespace(date="2026-06-19", p50=1.3)]),
        ]
    )
    user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: False)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces/analytics/latency")

    assert resp.status_code == 200
    data = resp.json()
    assert data["overall"]["p95"] == 4.0
    assert data["by_model"][0]["model"] == "gpt-5.3-codex"
    assert data["daily_median"][0]["p50"] == 1.3


def test_analytics_cost_by_agent_shape(monkeypatch):
    fake_db = _FakeDb(
        execute_results=[
            _RowResult(
                [
                    SimpleNamespace(
                        agent_id=str(uuid.uuid4()),
                        agent_name="Support Bot",
                        agent_icon="headset",
                        trace_count=5,
                        total_cost=1.5,
                        avg_cost_per_trace=0.3,
                        avg_duration_ms=45.0,
                        avg_feedback_score=0.8,
                    )
                ]
            )
        ]
    )
    user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: False)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces/analytics/cost-by-agent")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["agent_name"] == "Support Bot"


_AGENT_ROW = {
    "agent_id": "6ed1d48c-8df3-4eaa-b3e5-b597fc1aea6c",
    "agent_name": None,
    "agent_icon": None,
    "trace_count": 3,
    "total_cost": 1.0,
    "avg_cost_per_trace": 0.33,
    "avg_duration_ms": 10.0,
    "avg_feedback_score": None,
}


def test_a_cost_by_agent_row_links_only_where_the_caller_may_open_the_agent():
    """The link is the server's answer, set only with ``can_open``."""
    row = {**_AGENT_ROW, "agent_name": "Support Bot"}
    opened = traces_api._cost_by_agent_item(row, {}, {"agent_status": "active", "can_open": True, "last_known_name": None})
    assert (opened.can_open, opened.agent_url, opened.agent_status) == (True, f"/agents/{row['agent_id']}", "active")

    shut = traces_api._cost_by_agent_item(row, {}, {"agent_status": "disabled", "can_open": False, "last_known_name": None})
    assert (shut.can_open, shut.agent_url, shut.agent_status) == (False, None, "disabled")


def test_a_deleted_agent_row_carries_its_last_known_name_and_never_opens():
    """A deleted agent shows its tombstone name, never its bare id."""
    deleted = traces_api._cost_by_agent_item(
        _AGENT_ROW, {}, {"agent_status": "deleted", "can_open": False, "last_known_name": "Old Bot"}
    )
    assert (deleted.agent_name, deleted.agent_status, deleted.can_open, deleted.agent_url) == (
        "Old Bot",
        "deleted",
        False,
        None,
    )
    unknown = traces_api._cost_by_agent_item(_AGENT_ROW, {}, None)
    assert (unknown.agent_status, unknown.can_open, unknown.agent_url) == ("deleted", False, None)


class _UsageByUserStore:
    provider = "postgres"

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def analytics_usage_by_user(self, _db, **kwargs):
        self.calls.append(kwargs)
        return self._rows


def _usage_rows(person: uuid.UUID) -> list[dict]:
    return [
        {"user_id": str(person), "trace_count": 4, "total_tokens": 800, "total_cost": 2.0, "agent_count": 2},
        {"user_id": None, "trace_count": 2, "total_tokens": 100, "total_cost": 0.5, "agent_count": 1},
    ]


def _patch_analytics_scope(monkeypatch) -> None:
    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: False)


def test_analytics_cost_by_user_shape(monkeypatch):
    person, group = uuid.uuid4(), uuid.uuid4()
    fake_db = _FakeDb(
        execute_results=[
            _RowResult([(person, "alice@example.com", "Alice", True)]),
            _RowResult([(person, group)]),
            _RowResult([(group, "Support", 4)]),
        ]
    )
    store = _UsageByUserStore(_usage_rows(person))
    _patch_analytics_scope(monkeypatch)

    with _client(fake_db, _user(), trace_store=store) as client:
        resp = client.get("/api/v1/traces/analytics/cost-by-user?period=30d&limit=5")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [
        {
            "user_id": str(person),
            "user_email": "alice@example.com",
            "user_name": "Alice",
            "is_active": True,
            "group_names": ["Support"],
            "agent_count": 2,
            "trace_count": 4,
            "total_tokens": 800,
            "total_cost": 2.0,
            "avg_cost_per_trace": 0.5,
        }
    ]
    assert body["total_users"] == 1
    assert body["other_users"] is None
    assert body["unattributed"] == {
        "trace_count": 2,
        "total_tokens": 100,
        "total_cost": 0.5,
        "avg_cost_per_trace": 0.25,
    }
    assert store.calls[0]["use_daily"] is False


def test_analytics_cost_by_user_counts_the_users_past_the_limit_together(monkeypatch):
    person = uuid.uuid4()
    fake_db = _FakeDb(
        execute_results=[
            _RowResult([(person, "alice@example.com", "Alice", True)]),
            _RowResult([]),
        ]
    )
    rows = [
        *_usage_rows(person),
        {"user_id": str(uuid.uuid4()), "trace_count": 1, "total_tokens": 40, "total_cost": 0.25, "agent_count": 1},
        {"user_id": str(uuid.uuid4()), "trace_count": 3, "total_tokens": 60, "total_cost": 0.5, "agent_count": 2},
    ]
    _patch_analytics_scope(monkeypatch)

    with _client(fake_db, _user(), trace_store=_UsageByUserStore(rows)) as client:
        resp = client.get("/api/v1/traces/analytics/cost-by-user?period=30d&limit=1")

    assert resp.status_code == 200
    body = resp.json()
    assert [item["user_id"] for item in body["items"]] == [str(person)]
    assert body["total_users"] == 3
    assert body["other_users"] == {
        "user_count": 2,
        "trace_count": 4,
        "total_tokens": 100,
        "total_cost": 0.75,
        "avg_cost_per_trace": 0.1875,
    }


def test_analytics_cost_by_group_shape(monkeypatch):
    person, loner, group = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fake_db = _FakeDb(
        execute_results=[
            _RowResult([(person, group)]),
            _RowResult([(group, "Support", 4)]),
        ]
    )
    rows = [
        *_usage_rows(person),
        {"user_id": str(loner), "trace_count": 3, "total_tokens": 300, "total_cost": 0.75, "agent_count": 1},
    ]
    _patch_analytics_scope(monkeypatch)

    with _client(fake_db, _user(), trace_store=_UsageByUserStore(rows)) as client:
        resp = client.get("/api/v1/traces/analytics/cost-by-group")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [
        {
            "group_id": str(group),
            "group_name": "Support",
            "member_count": 4,
            "active_user_count": 1,
            "avg_cost_per_active_user": 2.0,
            "trace_count": 4,
            "total_tokens": 800,
            "total_cost": 2.0,
            "avg_cost_per_trace": 0.5,
        }
    ]
    assert body["ungrouped"] == {
        "active_user_count": 1,
        "avg_cost_per_active_user": 0.75,
        "trace_count": 3,
        "total_tokens": 300,
        "total_cost": 0.75,
        "avg_cost_per_trace": 0.25,
    }
    assert body["unattributed"]["trace_count"] == 2
    assert body["overlapping_membership"] is False


@mark.parametrize("path", ["cost-by-user", "cost-by-group"])
def test_analytics_cost_by_user_and_group_are_admin_only(monkeypatch, path):
    store = _UsageByUserStore([])
    _patch_analytics_scope(monkeypatch)

    with _client(_FakeDb(), _user(is_admin=False), trace_store=store) as client:
        resp = client.get(f"/api/v1/traces/analytics/{path}")

    assert resp.status_code == 403
    assert store.calls == []


@mark.parametrize("limit", [0, 1001])
def test_analytics_cost_by_user_bounds_the_limit(monkeypatch, limit):
    _patch_analytics_scope(monkeypatch)

    with _client(_FakeDb(), _user(), trace_store=_UsageByUserStore([])) as client:
        resp = client.get(f"/api/v1/traces/analytics/cost-by-user?limit={limit}")

    assert resp.status_code == 422


def test_analytics_errors_shape(monkeypatch):
    fake_db = _FakeDb(
        execute_results=[
            _RowResult(
                [
                    SimpleNamespace(
                        error_message="Rate limit exceeded",
                        count=2,
                        last_seen="2026-06-19T10:00:00Z",
                        agent_name="Support Bot",
                        model="gpt-5.3-codex",
                    )
                ]
            )
        ]
    )
    user = _user()

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "_use_daily_stats", lambda *_args, **_kwargs: False)

    with _client(fake_db, user) as client:
        resp = client.get("/api/v1/traces/analytics/errors")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["error_message"] == "Rate limit exceeded"


# ---------------------------------------------------------------------------
# Payload endpoints — trace payloads are stored whole, fetched on demand
# ---------------------------------------------------------------------------


class _PayloadStore:
    """Trace read store recording which span accessor the endpoint reached for."""

    provider = "postgres"

    def __init__(self, *, trace, span=None):
        self._trace = trace
        self._span = span
        self.get_trace_spans_calls = 0
        self.get_trace_span_calls: list[uuid.UUID] = []

    async def get_trace_detail(self, *_args, **_kwargs):
        return self._trace, None, None, None

    async def get_trace_spans(self, *_args, **_kwargs):
        self.get_trace_spans_calls += 1
        return [self._span] if self._span is not None else []

    async def get_trace_span(self, *_args, span_id=None, **_kwargs):
        self.get_trace_span_calls.append(span_id)
        if self._span is None or span_id != self._span.id:
            raise HTTPException(status_code=404, detail="Span not found for this trace")
        return self._span


def _payload_span(**payloads):
    return SimpleNamespace(
        id=uuid.uuid4(),
        input=payloads.get("input"),
        output=payloads.get("output"),
        metadata_json=payloads.get("metadata"),
    )


def test_span_payload_returns_an_inline_payload_in_full(monkeypatch):
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _payload_span(output={"content": "answer", "reasoning": "the whole thing"})
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans/{span.id}/payload?field=output")

    assert resp.status_code == 200
    body = resp.json()
    assert body["offloaded"] is False
    assert body["unavailable"] is False
    assert body["value"] == {"content": "answer", "reasoning": "the whole thing"}


def test_span_payload_fetches_one_span_not_the_whole_trace(monkeypatch):
    """Loading every span to serve one field would pull megabytes per request.

    Span payloads are no longer capped, so this is the difference between one
    row and an entire trace's worth of prompts, reasoning and tool results.
    """
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _payload_span(output={"content": "answer"})
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans/{span.id}/payload?field=output")

    assert resp.status_code == 200
    assert store.get_trace_span_calls == [span.id]
    assert store.get_trace_spans_calls == 0


def test_span_payload_resolves_an_offloaded_payload(monkeypatch):
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _payload_span(
        output={
            "__xtm_offload__": 1,
            "object_name": "traces/a/b/output.json",
            "size_bytes": 4_000_000,
            "sha256": "abc",
            "preview": '{"content": "head…',
        }
    )
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    async def _fake_resolve(_value):
        return {"content": "answer", "reasoning": "r" * 100}

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "resolve_payload", _fake_resolve)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans/{span.id}/payload?field=output")

    assert resp.status_code == 200
    body = resp.json()
    assert body["offloaded"] is True
    assert body["size_bytes"] == 4_000_000
    assert body["sha256"] == "abc"
    assert body["value"]["reasoning"] == "r" * 100


def test_span_payload_reports_a_payload_that_was_never_stored(monkeypatch):
    """A failed offload must read as a gap, not as the whole payload."""
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _payload_span(
        output={
            "__xtm_offload__": 1,
            "size_bytes": 90_000_000,
            "offload_failed": True,
            "error": "object storage unreachable",
            "preview": '{"content": "head…',
        }
    )
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans/{span.id}/payload?field=output")

    assert resp.status_code == 200
    body = resp.json()
    assert body["unavailable"] is True
    assert body["error"] == "object storage unreachable"
    assert body["value"] == '{"content": "head…'


def test_span_payload_surfaces_an_object_storage_failure(monkeypatch):
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _payload_span(
        output={"__xtm_offload__": 1, "object_name": "traces/a/b/output.json"}
    )
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    async def _boom(_value):
        raise RuntimeError("bucket gone")

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "resolve_payload", _boom)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans/{span.id}/payload?field=output")

    assert resp.status_code == 502


def test_synthetic_root_span_payload_resolves_against_the_trace(monkeypatch):
    """The `t-<trace_id>` span mirrors the trace's own payloads."""
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    trace.output = {"content": "trace-level answer"}
    store = _PayloadStore(trace=trace)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans/t-{trace.id}/payload?field=output")

    assert resp.status_code == 200
    assert resp.json()["value"] == {"content": "trace-level answer"}
    assert store.get_trace_span_calls == []


def test_span_payload_rejects_an_unknown_field(monkeypatch):
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    store = _PayloadStore(trace=trace, span=_payload_span())

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(f"/api/v1/traces/{trace.id}/spans/{uuid.uuid4()}/payload?field=usage")

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Media references are fetchable through the payload they belong to
# ---------------------------------------------------------------------------

_MEDIA_PNG = b"\x89PNG\r\n\x1a\n" + b"pixel" * 400
_MEDIA_SHA = hashlib.sha256(_MEDIA_PNG).hexdigest()


def _media_reference(**overrides):
    reference = {
        "__xtm_media__": 1,
        "object_name": "traces/a/b/media/0001-media.png",
        "content_type": "image/png",
        "size_bytes": len(_MEDIA_PNG),
        "sha256": _MEDIA_SHA,
    }
    reference.update(overrides)
    return reference


def _media_span(reference):
    return _payload_span(
        input={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": reference}},
                    ],
                }
            ]
        }
    )


@fixture
def media_storage(monkeypatch):
    class _Storage:
        def __init__(self) -> None:
            self.objects = {"traces/a/b/media/0001-media.png": _MEDIA_PNG}
            self.downloaded: list[str] = []

        async def download(self, object_name):
            self.downloaded.append(object_name)
            return self.objects[object_name]

    storage = _Storage()
    monkeypatch.setattr("app.services.file_storage.file_storage", storage)
    return storage


def test_span_media_returns_the_blob_bytes(monkeypatch, media_storage):
    """The point of the whole change: the exact image is recoverable."""
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _media_span(_media_reference())
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(
            f"/api/v1/traces/{trace.id}/spans/{span.id}/payload/media"
            f"?field=input&sha256={_MEDIA_SHA}"
        )

    assert resp.status_code == 200
    assert resp.content == _MEDIA_PNG
    assert resp.headers["content-type"] == "image/png"
    assert media_storage.downloaded == ["traces/a/b/media/0001-media.png"]
    # Authorised bytes: browser-only, keyed on the credential, and
    # immutable because the URL names the blob by its digest.
    assert resp.headers["cache-control"] == "private, max-age=86400, immutable"
    assert resp.headers["vary"] == "Authorization"


def test_span_media_is_addressed_by_digest_not_object_name(monkeypatch, media_storage):
    """A digest the payload does not reference is not fetchable.

    This is what keeps the endpoint from being a read primitive for arbitrary
    objects in the bucket.
    """
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _media_span(_media_reference())
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(
            f"/api/v1/traces/{trace.id}/spans/{span.id}/payload/media"
            f"?field=input&sha256={'0' * 64}"
        )

    assert resp.status_code == 404
    assert media_storage.downloaded == []


def test_span_media_rejects_a_malformed_digest(monkeypatch, media_storage):
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _media_span(_media_reference())
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(
            f"/api/v1/traces/{trace.id}/spans/{span.id}/payload/media"
            "?field=input&sha256=../../etc/passwd"
        )

    assert resp.status_code == 400
    assert media_storage.downloaded == []


def test_span_media_reports_a_blob_that_was_never_stored(monkeypatch, media_storage):
    """A failed upload must read as a gap, not as a wrong digest."""
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    reference = _media_reference(offload_failed=True, error="bucket unreachable")
    reference.pop("object_name")
    span = _media_span(reference)
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(
            f"/api/v1/traces/{trace.id}/spans/{span.id}/payload/media"
            f"?field=input&sha256={_MEDIA_SHA}"
        )

    assert resp.status_code == 410
    assert "bucket unreachable" in resp.json()["detail"]


def test_span_media_resolves_through_an_offloaded_payload(monkeypatch, media_storage):
    """Media stays reachable when the payload holding it was offloaded too."""
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = _payload_span(
        input={"__xtm_offload__": 1, "object_name": "traces/a/b/input.json"}
    )
    store = _PayloadStore(trace=trace, span=span)

    async def _groups(_db, _user_id):
        return []

    async def _fake_resolve(_value):
        return _media_span(_media_reference()).input

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(traces_api, "resolve_payload", _fake_resolve)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(
            f"/api/v1/traces/{trace.id}/spans/{span.id}/payload/media"
            f"?field=input&sha256={_MEDIA_SHA}"
        )

    assert resp.status_code == 200
    assert resp.content == _MEDIA_PNG


def test_trace_media_resolves_against_the_trace_row(monkeypatch, media_storage):
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    trace.input = _media_span(_media_reference()).input
    store = _PayloadStore(trace=trace)

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(traces_api, "get_user_group_ids", _groups)

    with _client(_FakeDb(), admin, trace_store=store) as client:
        resp = client.get(
            f"/api/v1/traces/{trace.id}/payload/media?field=input&sha256={_MEDIA_SHA}"
        )

    assert resp.status_code == 200
    assert resp.content == _MEDIA_PNG


def test_span_response_previews_a_large_payload_but_keeps_its_shape(monkeypatch):
    """The regression this whole change exists for: `output.reasoning` must
    survive the transport bound, and `payload_meta` must say it was bounded."""
    admin = _user(is_admin=True)
    trace = _trace_obj(user_id=admin.id)
    span = SimpleNamespace(
        id=uuid.uuid4(),
        trace_id=trace.id,
        parent_span_id=None,
        type="GENERATION",
        name="llm",
        model="claude-opus-5",
        model_parameters=None,
        input={},
        output={"content": "answer", "reasoning": "r" * 80_000},
        metadata_json={},
        level="DEFAULT",
        status_message=None,
        started_at=trace.started_at,
        ended_at=trace.ended_at,
        duration_ms=1.0,
        time_to_first_token=None,
        input_tokens=1,
        output_tokens=2,
        input_cost=None,
        output_cost=None,
        total_cost=None,
        usage_details=None,
        tool_call_id=None,
        tool_arguments=None,
        stop_reason="end_turn",
        position=0,
        created_at=trace.created_at,
    )

    item = traces_api._span_to_response(span)

    assert isinstance(item.output, dict)
    assert item.output["content"] == "answer"
    assert "reasoning" in item.output, "the reasoning key must stay addressable"
    assert item.payload_meta["output"]["truncated"] is True
    assert item.payload_meta["output"]["offloaded"] is False
    assert item.payload_meta["output"]["size_bytes"] > 80_000
