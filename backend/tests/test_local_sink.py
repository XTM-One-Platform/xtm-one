from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.llm_trace import LLMScore, LLMSpan, LLMTrace
from app.services.tracing.base import SpanData, TraceData
from app.services.tracing.local_sink import LocalPGSink, _strip_null_bytes


def _sample_trace() -> TraceData:
    now = datetime.now(UTC)
    return TraceData(
        id=uuid4(),
        source="assignment",
        name="assignment:daily-digest",
        started_at=now,
        ended_at=now,
        duration_ms=10.0,
        status="ok",
        user_id=uuid4(),
        agent_id=uuid4(),
        conversation_id=uuid4(),
        assignment_run_id=uuid4(),
        model="claude-sonnet-4.6",
        usage_details={"input": 100, "output": 25, "total": 125},
        input_tokens=100,
        output_tokens=25,
        total_tokens=125,
        metadata={"provider": "anthropic"},
        input={"messages": []},
        output={"text": "ok"},
        tags=["source:assignment"],
    )


def _sample_span(trace_id) -> SpanData:
    now = datetime.now(UTC)
    return SpanData(
        id=uuid4(),
        trace_id=trace_id,
        type="GENERATION",
        name="llm_call",
        position=1,
        started_at=now,
        ended_at=now,
        duration_ms=4.2,
        usage_details={"input": 10, "output": 5, "total": 15},
        input_tokens=10,
        output_tokens=5,
        output={"text": "done"},
    )


@pytest.mark.asyncio
async def test_flush_trace_with_session_persists_trace_and_spans(monkeypatch):
    trace = _sample_trace()
    span = _sample_span(trace.id)

    async def fake_resolve_price(*_args, **_kwargs):
        return SimpleNamespace()

    def fake_calculate_costs(_usage, _price):
        return {"input_cost": 0.001, "output_cost": 0.002, "total_cost": 0.003}

    monkeypatch.setattr(
        "app.services.tracing.local_sink.resolve_model_price",
        fake_resolve_price,
    )
    monkeypatch.setattr(
        "app.services.tracing.local_sink.calculate_costs",
        fake_calculate_costs,
    )

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.added_all: list[object] = []
            self.flush_calls = 0

        def add(self, row) -> None:
            self.added.append(row)

        def add_all(self, rows) -> None:
            self.added_all.extend(rows)

        async def flush(self) -> None:
            self.flush_calls += 1

    session = FakeSession()
    sink = LocalPGSink(session_factory=lambda: None)
    await sink._flush_trace_with_session(session, trace, [span])

    assert len(session.added) == 1
    assert isinstance(session.added[0], LLMTrace)
    assert session.added[0].source == "assignment"
    assert float(session.added[0].total_cost) == 0.003
    assert session.flush_calls == 1
    assert len(session.added_all) == 1
    assert isinstance(session.added_all[0], LLMSpan)
    assert float(session.added_all[0].total_cost) == 0.003


@pytest.mark.asyncio
async def test_flush_prices_cached_and_reasoning_tokens(monkeypatch):
    """A streamed OpenAI call with cache hits and reasoning was recorded
    at $0.60 for a $2.28 call - its cached and reasoning tokens priced $0."""
    from datetime import date
    from decimal import Decimal

    from app.services.langfuse_tracing import _normalize_usage
    from app.services.tracing.cost_calculator import ModelPrice

    usage = _normalize_usage(
        {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 500_000,
            "total_tokens": 1_500_000,
            "prompt_tokens_details": {"cached_tokens": 800_000},
            "completion_tokens_details": {"reasoning_tokens": 400_000},
        }
    )
    trace = _sample_trace()
    trace.usage_details = usage
    span = _sample_span(trace.id)
    span.usage_details = usage
    price = ModelPrice(
        provider="openai",
        model_id="claude-sonnet-4.6",
        model_pattern=None,
        input_price_per_1m=Decimal("1"),
        output_price_per_1m=Decimal("4"),
        cache_read_price_per_1m=Decimal("0.1"),
        cache_write_price_per_1m=None,
        effective_from=date(2026, 1, 1),
        is_custom=False,
    )

    async def fake_resolve_price(*_args, **_kwargs):
        return price

    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price", fake_resolve_price)

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.added_all: list[object] = []

        def add(self, row) -> None:
            self.added.append(row)

        def add_all(self, rows) -> None:
            self.added_all.extend(rows)

        async def flush(self) -> None:
            return None

    session = FakeSession()
    await LocalPGSink(session_factory=lambda: None)._flush_trace_with_session(session, trace, [span])

    assert Decimal(str(session.added[0].total_cost)) == Decimal("2.28")
    assert Decimal(str(session.added_all[0].total_cost)) == Decimal("2.28")


@pytest.mark.asyncio
async def test_flush_trace_drops_orphan_parent_span_id(monkeypatch):
    trace = _sample_trace()
    span = _sample_span(trace.id)
    span.parent_span_id = uuid4()

    async def fake_resolve_price(*_args, **_kwargs):
        return SimpleNamespace()

    def fake_calculate_costs(_usage, _price):
        return {"input_cost": 0.001, "output_cost": 0.002, "total_cost": 0.003}

    monkeypatch.setattr(
        "app.services.tracing.local_sink.resolve_model_price",
        fake_resolve_price,
    )
    monkeypatch.setattr(
        "app.services.tracing.local_sink.calculate_costs",
        fake_calculate_costs,
    )

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.added_all: list[object] = []
            self.flush_calls = 0

        def add(self, row) -> None:
            self.added.append(row)

        def add_all(self, rows) -> None:
            self.added_all.extend(rows)

        async def flush(self) -> None:
            self.flush_calls += 1

    session = FakeSession()
    sink = LocalPGSink(session_factory=lambda: None)
    await sink._flush_trace_with_session(session, trace, [span])

    assert len(session.added_all) == 1
    assert isinstance(session.added_all[0], LLMSpan)
    assert session.added_all[0].parent_span_id is None


@pytest.mark.asyncio
async def test_flush_trace_swallowed_on_failure():
    trace = _sample_trace()

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Factory:
        def __call__(self):
            return _Ctx()

    sink = LocalPGSink(session_factory=_Factory())

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("db write failed")

    sink._flush_trace_with_session = _boom  # type: ignore[method-assign]

    await sink.flush_trace(trace, [])

    assert sink.tracing_errors == 1


class _Scalars:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class _RaterSession:
    """Fake session for a rater's score: the advisory lock, then the lookup."""

    def __init__(self, existing: list[LLMScore] | None = None) -> None:
        self.existing = existing or []
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.locked = False

    async def execute(self, statement, params=None):
        if params and "lock_key" in params:
            self.locked = True
            return None
        return _Scalars(self.existing)

    async def delete(self, row) -> None:
        self.deleted.append(row)

    def add(self, row) -> None:
        self.added.append(row)


def _feedback(trace_id, user_id, **extra) -> dict:
    return {
        "trace_id": str(trace_id),
        "user_id": str(user_id),
        "name": "user_feedback",
        "source": "USER",
        "data_type": "BOOLEAN",
        **extra,
    }


def _stored(trace_id, user_id, value) -> LLMScore:
    return LLMScore(
        id=uuid4(),
        trace_id=trace_id,
        user_id=user_id,
        name="user_feedback",
        source="USER",
        data_type="BOOLEAN",
        value=value,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_a_changed_rating_replaces_the_raters_score_and_drops_duplicates():
    """One row per (trace, user, name), holding the latest value."""
    trace_id, user_id = uuid4(), uuid4()
    latest, older = _stored(trace_id, user_id, 1.0), _stored(trace_id, user_id, 1.0)
    session = _RaterSession([latest, older])

    await LocalPGSink(session_factory=lambda: None)._flush_score_with_session(
        session, _feedback(trace_id, user_id, value=0.0, comment="wrong"), trace_id
    )

    assert session.locked
    assert session.added == []
    assert session.deleted == [older]
    assert latest.value == 0.0
    assert latest.comment == "wrong"


@pytest.mark.asyncio
async def test_a_retraction_leaves_the_rater_a_null_score_instead_of_a_zero():
    trace_id, user_id = uuid4(), uuid4()
    latest, older = _stored(trace_id, user_id, 1.0), _stored(trace_id, user_id, 0.0)
    session = _RaterSession([latest, older])

    await LocalPGSink(session_factory=lambda: None)._flush_score_with_session(
        session, _feedback(trace_id, user_id, value=0.0, retract=True), trace_id
    )

    assert session.deleted == [older]
    assert session.added == []
    assert latest.value is None


@pytest.mark.asyncio
async def test_an_action_older_than_the_raters_stored_one_is_ignored():
    """Flushes are detached tasks: a rating can land after its retraction."""
    trace_id, user_id = uuid4(), uuid4()
    retraction = _stored(trace_id, user_id, None)
    session = _RaterSession([retraction])
    earlier = retraction.created_at - timedelta(seconds=5)

    await LocalPGSink(session_factory=lambda: None)._flush_score_with_session(
        session, _feedback(trace_id, user_id, value=1.0, created_at=earlier), trace_id
    )

    assert retraction.value is None
    assert session.added == []
    assert session.deleted == []


@pytest.mark.asyncio
async def test_flush_score_with_session_persists_score_row():
    trace_id = uuid4()
    user_id = uuid4()
    score = {
        "trace_id": str(trace_id),
        "user_id": str(user_id),
        "name": "user_feedback",
        "source": "USER",
        "data_type": "BOOLEAN",
        "value": 1.0,
        "string_value": None,
        "comment": "helpful answer",
    }

    session = _RaterSession()
    sink = LocalPGSink(session_factory=lambda: None)
    await sink._flush_score_with_session(session, score, trace_id)

    assert session.locked
    assert len(session.added) == 1
    assert isinstance(session.added[0], LLMScore)
    assert session.added[0].trace_id == trace_id
    assert session.added[0].user_id == user_id
    assert session.added[0].name == "user_feedback"
    assert session.added[0].source == "USER"
    assert session.added[0].data_type == "BOOLEAN"
    assert session.added[0].value == 1.0
    assert session.added[0].comment == "helpful answer"


@pytest.mark.asyncio
async def test_flush_score_skips_when_trace_id_missing():
    factory_calls = 0

    class _Factory:
        def __call__(self):
            nonlocal factory_calls
            factory_calls += 1
            raise AssertionError("session factory should not be called")

    sink = LocalPGSink(session_factory=_Factory())
    await sink.flush_score(
        {
            "name": "user_feedback",
            "source": "USER",
            "data_type": "BOOLEAN",
            "value": 1.0,
        }
    )

    assert factory_calls == 0


@pytest.mark.asyncio
async def test_flush_score_swallowed_on_failure():
    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _Factory:
        def __call__(self):
            return _Ctx()

    sink = LocalPGSink(session_factory=_Factory())

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("db write failed")

    sink._flush_score_with_session = _boom  # type: ignore[method-assign]

    await sink.flush_score(
        {
            "trace_id": str(uuid4()),
            "name": "user_feedback",
            "source": "USER",
            "data_type": "BOOLEAN",
            "value": 1.0,
        }
    )

    assert sink.tracing_errors == 1


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _CommitSession:
    """A score write's session; its trace lookup fails, as a record's would on an outage."""

    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def flush(self) -> None:
        return None

    def begin_nested(self):
        return _Savepoint()

    async def scalar(self, _statement):
        raise ConnectionRefusedError("database unreachable")

    async def commit(self) -> None:
        self.commits += 1


def _feedback_payload(**extra) -> dict:
    return {"trace_id": str(uuid4()), "user_id": str(uuid4()), "name": "user_feedback", "value": 0.0, **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed", "name", "recorded"),
    [(True, "user_feedback", True), (False, "user_feedback", False), (True, "accuracy", False)],
)
async def test_a_changed_feedback_score_records_its_rollup_day_with_the_score(monkeypatch, changed, name, recorded):
    """The backfill re-reads the feedback of the recent days only: an older
    day's rollup is refreshed from the record the score write leaves, in the
    same transaction, so a crash cannot keep the one without the other."""
    session = _CommitSession()
    sink = LocalPGSink(session_factory=lambda: session)
    calls: list = []

    async def _write(_session, _score, _trace_id):
        return changed

    async def _record(db, trace_id, **_kwargs):
        calls.append((db, trace_id, session.commits))

    sink._flush_score_with_session = _write  # type: ignore[method-assign]
    monkeypatch.setattr("app.services.trace_storage.record_feedback_day_stale", _record)
    score = _feedback_payload(name=name)

    await sink.flush_score(score)

    assert calls == ([(session, UUID(score["trace_id"]), 0)] if recorded else [])
    assert session.commits == 1


@pytest.mark.asyncio
async def test_a_failed_rollup_record_never_fails_the_score_write():
    session = _CommitSession()
    sink = LocalPGSink(session_factory=lambda: session)

    async def _write(*_args):
        return True

    sink._flush_score_with_session = _write  # type: ignore[method-assign]

    await sink.flush_score(_feedback_payload())

    # The record's trace lookup failed in its savepoint; the score is committed.
    assert session.commits == 1
    assert sink.tracing_errors == 0


# ---------------------------------------------------------------------------
# _strip_null_bytes unit tests
# ---------------------------------------------------------------------------


def test_strip_null_bytes_string():
    assert _strip_null_bytes("hello\u0000world") == "helloworld"
    assert _strip_null_bytes("no nulls here") == "no nulls here"
    assert _strip_null_bytes("") == ""
    assert _strip_null_bytes("\u0000") == ""


def test_strip_null_bytes_nested_dict():
    result = _strip_null_bytes({"key\u0000": "val\u0000ue", "nested": {"a": "\u0000b"}})
    assert result == {"key": "value", "nested": {"a": "b"}}


def test_strip_null_bytes_list():
    result = _strip_null_bytes(["a\u0000", "\u0000b", "c"])
    assert result == ["a", "b", "c"]


def test_strip_null_bytes_tuple():
    # Tuples are serialized to JSON arrays by the JSONB codec, so they must be
    # sanitized like lists (returned as lists — the JSON shape is identical).
    result = _strip_null_bytes(("a\u0000", {"k": "\u0000v"}))
    assert result == ["a", {"k": "v"}]


def test_strip_null_bytes_passthrough_non_string():
    assert _strip_null_bytes(42) == 42
    assert _strip_null_bytes(3.14) == 3.14
    assert _strip_null_bytes(None) is None
    assert _strip_null_bytes(True) is True


def test_strip_null_bytes_mixed_structure():
    payload = {
        "messages": [{"role": "user", "content": "hi\u0000there"}],
        "count": 1,
        "nested": {"deep": "\u0000"},
    }
    result = _strip_null_bytes(payload)
    assert result == {
        "messages": [{"role": "user", "content": "hithere"}],
        "count": 1,
        "nested": {"deep": ""},
    }


@pytest.mark.asyncio
async def test_flush_trace_strips_null_bytes_from_span_fields(monkeypatch):
    """Null bytes in span JSONB/string fields must be stripped before DB write.

    This is a regression test for the UntranslatableCharacterError that caused
    entire trace batches to be silently dropped when tool output contained \\u0000
    (e.g. from a GitHub GraphQL response body).
    """
    trace = _sample_trace()
    now = datetime.now(UTC)
    span = SpanData(
        id=uuid4(),
        trace_id=trace.id,
        type="TOOL",
        name="github_graphql_query\u0000",
        position=1,
        started_at=now,
        ended_at=now,
        duration_ms=1.0,
        output={"text": "body with\u0000null byte"},
        input={"query": "{ viewer { login\u0000 } }"},
        model_parameters={"temperature\u0000": 0.5},
        tool_arguments={"repo": "owner/repo\u0000"},
        usage_details={"input": 10, "output": 5},
        status_message="ok\u0000",
        stop_reason="end_turn\u0000",
    )

    async def fake_resolve_price(*_args, **_kwargs):
        return SimpleNamespace()

    def fake_calculate_costs(_usage, _price):
        return {"input_cost": 0.0, "output_cost": 0.0, "total_cost": 0.0}

    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price", fake_resolve_price)
    monkeypatch.setattr("app.services.tracing.local_sink.calculate_costs", fake_calculate_costs)

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.added_all: list[object] = []
            self.flush_calls = 0

        def add(self, row) -> None:
            self.added.append(row)

        def add_all(self, rows) -> None:
            self.added_all.extend(rows)

        async def flush(self) -> None:
            self.flush_calls += 1

    session = FakeSession()
    sink = LocalPGSink(session_factory=lambda: None)
    await sink._flush_trace_with_session(session, trace, [span])

    assert len(session.added_all) == 1
    span_row = session.added_all[0]
    assert isinstance(span_row, LLMSpan)

    # Null bytes must be stripped from all string and JSONB fields
    assert "\u0000" not in span_row.name
    assert span_row.name == "github_graphql_query"
    assert span_row.output == {"text": "body withnull byte"}
    assert span_row.input == {"query": "{ viewer { login } }"}
    assert span_row.model_parameters == {"temperature": 0.5}
    assert span_row.tool_arguments == {"repo": "owner/repo"}
    assert span_row.status_message == "ok"
    assert span_row.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_flush_trace_strips_null_bytes_from_trace_fields(monkeypatch):
    """Null bytes in trace-level JSONB/string fields are also stripped."""
    trace = _sample_trace()
    trace.name = "trace\u0000name"
    trace.output = {"result": "val\u0000ue"}
    trace.input = {"messages": [{"role": "user", "content": "hi\u0000"}]}
    trace.metadata = {"provider\u0000": "anthropic"}
    trace.error_message = "err\u0000or"

    async def fake_resolve_price(*_args, **_kwargs):
        return SimpleNamespace()

    def fake_calculate_costs(_usage, _price):
        return {"input_cost": 0.0, "output_cost": 0.0, "total_cost": 0.0}

    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price", fake_resolve_price)
    monkeypatch.setattr("app.services.tracing.local_sink.resolve_model_price_any", fake_resolve_price)
    monkeypatch.setattr("app.services.tracing.local_sink.calculate_costs", fake_calculate_costs)

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.added_all: list[object] = []
            self.flush_calls = 0

        def add(self, row) -> None:
            self.added.append(row)

        def add_all(self, rows) -> None:
            self.added_all.extend(rows)

        async def flush(self) -> None:
            self.flush_calls += 1

    session = FakeSession()
    sink = LocalPGSink(session_factory=lambda: None)
    await sink._flush_trace_with_session(session, trace, [])

    assert len(session.added) == 1
    trace_row = session.added[0]
    assert isinstance(trace_row, LLMTrace)

    assert trace_row.name == "tracename"
    assert trace_row.output == {"result": "value"}
    assert trace_row.input == {"messages": [{"role": "user", "content": "hi"}]}
    assert trace_row.metadata_json == {"provider": "anthropic"}
    assert trace_row.error_message == "error"


@pytest.mark.asyncio
async def test_flush_score_strips_null_bytes():
    """User-feedback scores carry free text (comment/string_value) — a null
    byte there must not drop the score row either."""

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, row) -> None:
            self.added.append(row)

    session = FakeSession()
    sink = LocalPGSink(session_factory=lambda: None)
    await sink._flush_score_with_session(
        session,
        {
            "name": "user_feedback\u0000",
            "string_value": "thumbs\u0000up",
            "comment": "great\u0000answer",
            "metadata": {"key\u0000": "val\u0000ue"},
            "value": True,
        },
        uuid4(),
    )

    assert len(session.added) == 1
    score_row = session.added[0]
    assert isinstance(score_row, LLMScore)
    assert score_row.name == "user_feedback"
    assert score_row.string_value == "thumbsup"
    assert score_row.comment == "greatanswer"
    assert score_row.metadata_json == {"key": "value"}
