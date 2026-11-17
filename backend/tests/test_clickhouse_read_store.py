from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.tracing.read_store import ClickHouseTraceReadStore


class _AllResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        row = self._rows[0]
        if isinstance(row, dict):
            return next(iter(row.values()))
        return row

    def scalars(self):
        return _ScalarsResult(self._rows)


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.execute_statements = []

    async def execute(self, statement):
        self.execute_statements.append(statement)
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)


class _FakeClickHouseClient:
    def __init__(self, query_results):
        self.query_results = list(query_results)
        self.queries: list[str] = []
        self.executions: list[str] = []
        self.inserts: list[tuple[str, list[dict]]] = []

    async def query_json_rows(self, query: str):
        self.queries.append(query)
        if not self.query_results:
            raise AssertionError("Unexpected ClickHouse query")
        return self.query_results.pop(0)

    async def insert_json_rows(self, table: str, rows: list[dict]):
        self.inserts.append((table, rows))

    async def execute(self, query: str):
        self.executions.append(query)

    async def close(self):
        return None


def _user(*, is_admin: bool = False):
    return SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin)


def _trace_row(*, trace_id: uuid.UUID, user_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None):
    now = datetime.now(UTC)
    return {
        "id": str(trace_id),
        "agent_id": str(agent_id) if agent_id else None,
        "user_id": str(user_id) if user_id else None,
        "conversation_id": None,
        "assignment_run_id": None,
        "parent_trace_id": None,
        "source": "general-chat",
        "name": "trace-name",
        "model": "gpt-5.3-codex",
        "environment": "production",
        "metadata": {},
        "input": {},
        "output": {},
        "tags": [],
        "started_at": now.isoformat(),
        "ended_at": now.isoformat(),
        "duration_ms": 12.3,
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "input_cost": 0.1,
        "output_cost": 0.2,
        "total_cost": 0.3,
        "status": "ok",
        "error_message": None,
        "level": "DEFAULT",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }


@pytest.mark.asyncio
async def test_clickhouse_list_traces_returns_clickhouse_rows():
    trace_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user = _user(is_admin=False)
    client = _FakeClickHouseClient(
        query_results=[
            [{"total": 1}],
            [_trace_row(trace_id=trace_id, user_id=user.id, agent_id=agent_id)],
            # has_scores is now a second, page-scoped lookup (not an unfiltered
            # DISTINCT scan of the whole llm_scores table).
            [{"trace_id": str(trace_id)}],
        ]
    )
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)
    db = _FakeDb(
        execute_results=[
            _AllResult([{"id": agent_id}]),
            # The deleted agents the visibility reads next: none.
            _AllResult([]),
            # The visible agents whose activity is hidden from non-admins
            # (#11624): none.
            _AllResult([]),
            _AllResult([{"id": agent_id, "name": "Agent A", "icon": "bolt"}]),
            _AllResult([{"id": user.id, "email": "user@example.com"}]),
        ]
    )

    rows, total = await store.list_traces(
        db,
        user=user,
        user_group_ids=[],
        search=None,
        conversation_id=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        cursor=None,
        cursor_id=None,
        limit=50,
    )

    assert total == 1
    assert rows[0][0].id == trace_id
    assert rows[0].agent_name == "Agent A"
    assert rows[0].user_email == "user@example.com"
    assert rows[0].has_scores is True
    # The has_scores lookup must be scoped to the page's trace ids, not an
    # unbounded full-table DISTINCT scan of llm_scores.
    score_query = client.queries[-1]
    assert "FROM llm_scores" in score_query
    assert str(trace_id) in score_query
    assert "WHERE" in score_query


@pytest.mark.asyncio
async def test_clickhouse_list_trace_sessions_returns_grouped_rows():
    user = _user(is_admin=True)
    conversation_id = uuid.uuid4()
    # F(b): list_trace_sessions now counts distinct conversations in a separate query
    # (so total is stable across pages) and pushes LIMIT into SQL, so the client must
    # answer the count query first and then the paginated GROUP BY query.
    client = _FakeClickHouseClient(
        query_results=[
            [{"total": 1}],
            [
                {
                    "conversation_id": str(conversation_id),
                    "first_activity_at": "2026-06-22T08:00:00+00:00",
                    "last_activity_at": "2026-06-22T08:05:00+00:00",
                    "trace_count": 2,
                    "error_count": 1,
                    "agent_count": 1,
                    "model_count": 1,
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "total_tokens": 20,
                    "total_cost": 0.44,
                }
            ],
        ]
    )
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)
    db = _FakeDb(execute_results=[])

    rows, total = await store.list_trace_sessions(
        db,
        user=user,
        user_group_ids=[],
        search=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        cursor=None,
        cursor_id=None,
        limit=50,
    )

    assert total == 1
    assert rows[0].conversation_id == conversation_id
    assert rows[0].trace_count == 2


@pytest.mark.asyncio
async def test_clickhouse_list_traces_orders_by_the_requested_column():
    """Sorting must reach ClickHouse too, or a header would work on one provider only."""
    user = _user(is_admin=True)
    # The total reaches past the offset, so the page query is issued.
    client = _FakeClickHouseClient(query_results=[[{"total": 500}], []])
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)

    rows, _total = await store.list_traces(
        _FakeDb(execute_results=[]),
        user=user,
        user_group_ids=[],
        search=None,
        conversation_id=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        # A cursor left over from the default order must not narrow a sort it
        # does not describe.
        cursor=datetime.now(UTC),
        cursor_id=str(uuid.uuid4()),
        limit=50,
        sort_by="duration_ms",
        sort_dir="desc",
        offset=50,
    )

    assert rows == []
    page_query = client.queries[-1]
    assert "ORDER BY duration_ms DESC NULLS LAST, id DESC" in page_query
    assert "OFFSET 50" in page_query
    assert "started_at <" not in page_query


@pytest.mark.asyncio
async def test_clickhouse_list_traces_rejects_a_column_outside_the_allow_list():
    """The sort column reaches SQL as raw text, so only the allow-list may name it."""
    user = _user(is_admin=True)
    client = _FakeClickHouseClient(query_results=[[{"total": 0}], []])
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)

    await store.list_traces(
        _FakeDb(execute_results=[]),
        user=user,
        user_group_ids=[],
        search=None,
        conversation_id=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        cursor=datetime(2026, 6, 16, tzinfo=UTC),
        cursor_id=None,
        limit=50,
        sort_by="1 FROM system.tables --",
        sort_dir="desc",
    )

    page_query = client.queries[-1]
    assert "system.tables" not in page_query
    # The default order keeps the SQL it has always had: no NULLS LAST on a
    # column that cannot be NULL.
    assert "ORDER BY started_at DESC, id DESC" in page_query
    # Falling back must be consistent: an order that IS the default one keeps
    # keyset paging, rather than ordering by started_at while ignoring the
    # cursor that describes exactly that order.
    assert "started_at < " in page_query


@pytest.mark.asyncio
async def test_clickhouse_list_trace_sessions_orders_by_the_requested_aggregate():
    user = _user(is_admin=True)
    client = _FakeClickHouseClient(query_results=[[{"total": 500}], []])
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)

    await store.list_trace_sessions(
        _FakeDb(execute_results=[]),
        user=user,
        user_group_ids=[],
        search=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        cursor=datetime.now(UTC),
        cursor_id=None,
        limit=50,
        sort_by="total_cost",
        sort_dir="asc",
        offset=100,
    )

    page_query = client.queries[-1]
    # A session's cost is a SUM over a non-empty group: never NULL, so no
    # NULLS LAST is needed here.
    assert "ORDER BY total_cost ASC, conversation_id ASC" in page_query
    assert "OFFSET 100" in page_query
    # The keyset HAVING describes the default newest-first order only.
    assert "HAVING" not in page_query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_by", "sort_dir", "mirrored"),
    [
        ("total_cost", "desc", "ORDER BY total_cost ASC NULLS FIRST, id ASC"),
        ("duration_ms", "asc", "ORDER BY duration_ms DESC NULLS FIRST, id ASC"),
        ("started_at", "desc", "ORDER BY started_at ASC, id ASC"),
    ],
)
async def test_clickhouse_list_traces_reverse_reads_the_exact_mirror(sort_by, sort_dir, mirrored):
    """Same mirror as the PostgreSQL path: column, NULL placement and tie-breaker
    all flip, and the keyset cursor of the default order is not applied."""
    client = _FakeClickHouseClient(query_results=[[{"total": 500}], []])
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)

    await store.list_traces(
        _FakeDb(execute_results=[]),
        user=_user(is_admin=True),
        user_group_ids=[],
        search=None,
        conversation_id=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        cursor=datetime.now(UTC),
        cursor_id=str(uuid.uuid4()),
        limit=50,
        sort_by=sort_by,
        sort_dir=sort_dir,
        offset=50,
        reverse=True,
    )

    page_query = client.queries[-1]
    assert mirrored in page_query
    assert "OFFSET 50" in page_query
    assert "started_at <" not in page_query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort_by", "sort_dir", "mirrored"),
    [
        ("total_cost", "asc", "ORDER BY total_cost DESC, conversation_id DESC"),
        ("last_activity_at", "desc", "ORDER BY last_activity_at ASC, conversation_id DESC"),
    ],
)
async def test_clickhouse_list_trace_sessions_reverse_reads_the_exact_mirror(sort_by, sort_dir, mirrored):
    client = _FakeClickHouseClient(query_results=[[{"total": 500}], []])
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)

    await store.list_trace_sessions(
        _FakeDb(execute_results=[]),
        user=_user(is_admin=True),
        user_group_ids=[],
        search=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        cursor=datetime.now(UTC),
        cursor_id=None,
        limit=50,
        sort_by=sort_by,
        sort_dir=sort_dir,
        offset=100,
        reverse=True,
    )

    page_query = client.queries[-1]
    assert mirrored in page_query
    assert "OFFSET 100" in page_query
    assert "HAVING" not in page_query


_LIST_KWARGS = {
    "search": None,
    "source": None,
    "agent_id": None,
    "model": None,
    "status_filter": None,
    "level": None,
    "date_from": None,
    "date_to": None,
    "cursor": None,
    "cursor_id": None,
    "limit": 50,
    "sort_by": "total_cost",
    "sort_dir": "asc",
    "offset": 50,
}


@pytest.mark.asyncio
async def test_clickhouse_list_traces_skips_the_page_query_past_the_last_row():
    """An offset beyond the matching rows is an empty page: only the count runs."""
    client = _FakeClickHouseClient(query_results=[[{"total": 10}]])
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)

    rows, total = await store.list_traces(
        _FakeDb(execute_results=[]),
        user=_user(is_admin=True),
        user_group_ids=[],
        conversation_id=None,
        **_LIST_KWARGS,
    )

    assert rows == []
    assert total == 10
    assert len(client.queries) == 1
    assert "count()" in client.queries[0]


@pytest.mark.asyncio
async def test_clickhouse_list_trace_sessions_skips_the_page_query_past_the_last_row():
    client = _FakeClickHouseClient(query_results=[[{"total": 10}]])
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)

    rows, total = await store.list_trace_sessions(
        _FakeDb(execute_results=[]),
        user=_user(is_admin=True),
        user_group_ids=[],
        **_LIST_KWARGS,
    )

    assert rows == []
    assert total == 10
    assert len(client.queries) == 1
    assert "uniqExact(conversation_id)" in client.queries[0]


@pytest.mark.asyncio
async def test_clickhouse_get_trace_spans_and_scores():
    trace_id = uuid.uuid4()
    user = _user(is_admin=True)
    client = _FakeClickHouseClient(
        query_results=[
            [_trace_row(trace_id=trace_id, user_id=user.id)],
            [
                {
                    "id": str(uuid.uuid4()),
                    "trace_id": str(trace_id),
                    "parent_span_id": None,
                    "type": "SPAN",
                    "name": "root",
                    "model": None,
                    "model_parameters": None,
                    "input": {},
                    "output": {},
                    "metadata": {},
                    "level": "DEFAULT",
                    "status_message": None,
                    "started_at": datetime.now(UTC).isoformat(),
                    "ended_at": None,
                    "duration_ms": 1.2,
                    "time_to_first_token": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "input_cost": None,
                    "output_cost": None,
                    "total_cost": None,
                    "usage_details": None,
                    "tool_call_id": None,
                    "tool_arguments": None,
                    "stop_reason": None,
                    "position": 0,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ],
            [_trace_row(trace_id=trace_id, user_id=user.id)],
            [
                {
                    "id": str(uuid.uuid4()),
                    "trace_id": str(trace_id),
                    "span_id": None,
                    "user_id": str(user.id),
                    "name": "user_feedback",
                    "source": "USER",
                    "data_type": "BOOLEAN",
                    "value": 1.0,
                    "string_value": None,
                    "comment": None,
                    "metadata": {},
                    "created_at": datetime.now(UTC).isoformat(),
                }
            ],
        ]
    )
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)
    db = _FakeDb(execute_results=[])

    spans = await store.get_trace_spans(db, user=user, user_group_ids=[], trace_id=trace_id)
    scores = await store.get_trace_scores(db, user=user, user_group_ids=[], trace_id=trace_id)

    assert spans[0].name == "root"
    assert scores[0].name == "user_feedback"


def _span_row(**overrides):
    row = {
        "id": str(uuid.uuid4()),
        "trace_id": str(uuid.uuid4()),
        "parent_span_id": None,
        "type": "GENERATION",
        "name": "span-empty-json",
        "model": "gpt-test",
        "model_parameters": "",
        "input": "",
        "output": "",
        "metadata": "",
        "level": "DEFAULT",
        "status_message": None,
        "started_at": "2026-06-24 12:00:00.000",
        "ended_at": None,
        "duration_ms": None,
        "time_to_first_token": None,
        "input_tokens": None,
        "output_tokens": None,
        "input_cost": None,
        "output_cost": None,
        "total_cost": None,
        "usage_details": "",
        "tool_call_id": None,
        "tool_arguments": "",
        "stop_reason": None,
        "position": 0,
        "created_at": "2026-06-24 12:00:00.000",
    }
    row.update(overrides)
    return row


def test_clickhouse_span_from_row_normalizes_empty_json_strings() -> None:
    store = ClickHouseTraceReadStore()

    span = store._span_from_row(_span_row())

    assert span.model_parameters is None
    assert span.usage_details is None
    assert span.tool_arguments is None
    assert span.input is None
    assert span.output is None
    assert span.metadata_json == {}


def test_clickhouse_span_from_row_coerces_tool_arguments() -> None:
    """ClickHouse rows follow the same tool_arguments contract as the
    Postgres API serializer: JSON objects parse back to dicts and legacy
    opaque strings survive as {"raw": ...} instead of being dropped."""
    store = ClickHouseTraceReadStore()

    parsed = store._span_from_row(
        _span_row(tool_arguments='{"bundle": {"type": "bundle"}}')
    )
    assert parsed.tool_arguments == {"bundle": {"type": "bundle"}}

    opaque = store._span_from_row(_span_row(tool_arguments="plain text arguments"))
    assert opaque.tool_arguments == {"raw": "plain text arguments"}

    whitespace = store._span_from_row(_span_row(tool_arguments="   "))
    assert whitespace.tool_arguments is None


class _ScoreActionDb:
    """The PostgreSQL side of a score write: the rater's lock, the database
    clock the action is timed on, and the commit that releases the lock."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.statements: list[str] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return SimpleNamespace(scalar_one=lambda: self.now)

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.asyncio
async def test_clickhouse_create_score_and_delete_trace():
    trace_id = uuid.uuid4()
    user = _user(is_admin=True)
    client = _FakeClickHouseClient(
        query_results=[
            [_trace_row(trace_id=trace_id, user_id=user.id)],
            [_trace_row(trace_id=trace_id, user_id=user.id)],
        ]
    )
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)
    action_at = datetime(2026, 9, 27, 12, 0, 0, 123456, tzinfo=UTC)
    db = _ScoreActionDb(action_at)

    score = await store.create_trace_score(
        db,
        user=user,
        user_group_ids=[],
        trace_id=trace_id,
        name="user_feedback",
        data_type="BOOLEAN",
        value=1.0,
        string_value=None,
        comment=None,
        metadata={},
    )
    await store.delete_trace(
        db,
        admin=user,
        admin_group_ids=[],
        trace_id=trace_id,
    )

    assert score.source == "USER"
    assert client.inserts[0][0] == "llm_scores"
    # Timed like every writer of a rater's score: under the lock, on the database clock.
    assert client.inserts[0][1][0]["created_at"] == action_at
    # A version-7 id: ClickHouse stores empty metadata like none, and the id is
    # what keeps this score from reading as a legacy retraction.
    assert uuid.UUID(client.inserts[0][1][0]["id"]).version == 7
    assert any("pg_advisory_xact_lock" in sql for sql in db.statements)
    assert db.commits == 1
    assert len(client.executions) == 3


@pytest.mark.asyncio
async def test_clickhouse_read_returns_explicit_503_when_client_unavailable():
    store = ClickHouseTraceReadStore()
    db = _FakeDb(execute_results=[])
    user = _user(is_admin=True)

    with pytest.raises(HTTPException) as exc:
        await store.list_traces(
            db,
            user=user,
            user_group_ids=[],
            search=None,
            conversation_id=None,
            source=None,
            agent_id=None,
            model=None,
            status_filter=None,
            level=None,
            date_from=None,
            date_to=None,
            cursor=None,
            cursor_id=None,
            limit=1,
        )

    assert exc.value.status_code == 503
    assert "ClickHouse read client is not configured" in str(exc.value.detail)


def test_clickhouse_list_filters_narrow_by_assignment_and_run():
    """The run table lives in PostgreSQL only: ClickHouse matches the run on
    its column and the assignment on the column materialized from the id the
    engine stamps in metadata - never a per-row JSON parse, and never a
    ``toString()`` that would keep the bloom filters from skipping."""
    store = ClickHouseTraceReadStore(_client_factory=lambda: _FakeClickHouseClient([]))
    assignment_id = uuid.uuid4()
    run_id = uuid.uuid4()

    clauses = store._list_filters_sql(
        search=None,
        conversation_id=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        include_conversation_search=False,
        assignment_id=assignment_id,
        assignment_run_id=run_id,
    )

    assert clauses == [
        f"assignment_run_id = toUUID('{run_id}')",
        f"assignment_id = '{assignment_id}'",
    ]


@pytest.mark.asyncio
async def test_clickhouse_list_traces_counts_and_pages_within_the_assignment():
    assignment_id = uuid.uuid4()
    client = _FakeClickHouseClient(query_results=[[{"total": 0}], []])
    store = ClickHouseTraceReadStore(_client_factory=lambda: client)

    rows, total = await store.list_traces(
        _FakeDb(execute_results=[]),
        user=_user(is_admin=True),
        user_group_ids=[],
        search=None,
        conversation_id=None,
        source=None,
        agent_id=None,
        model=None,
        status_filter=None,
        level=None,
        date_from=None,
        date_to=None,
        cursor=None,
        cursor_id=None,
        limit=50,
        assignment_id=assignment_id,
    )

    predicate = f"assignment_id = '{assignment_id}'"
    assert (rows, total) == ([], 0)
    count_query, page_query = client.queries
    # The count and the page read the same WHERE: the total is the
    # assignment's, never the whole table's.
    assert count_query.partition(" WHERE ")[2] == predicate
    assert page_query.partition(" WHERE ")[2].partition(" ORDER BY ")[0] == predicate
