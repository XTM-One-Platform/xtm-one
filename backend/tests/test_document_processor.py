import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm.exc import StaleDataError

from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.services import document_processor


@pytest.fixture(autouse=True)
def _embedding_config_not_moved(monkeypatch):
    """The platform embedding config is the one each job embedded with."""
    monkeypatch.setattr(document_processor, "embedding_config_moved", AsyncMock(return_value=False))


class _ScalarResult:
    """Minimal stand-in for ``Result``'s ``scalar_one_or_none()`` API.

    Used by both the "missing-row" tests (``_MissingDocumentSession``,
    which constructs with ``value=None``) and the "found-row" tests
    (``_FakeSession``, which constructs with the actual ORM object).
    """

    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _MissingDocumentSession:
    """Async-session fake whose ``execute()`` always reports the
    document does not exist.  Used by ``test_process_document_impl_logs_warning_when_document_is_missing``
    to pin the "stale queue entry" warning-log path.
    """

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def execute(self, _statement):
        return _ScalarResult()


class _LockedDocumentSession:
    """Async-session fake for the ``FOR UPDATE SKIP LOCKED`` path.

    First query (lock attempt) returns no row, second query (existence
    check) confirms the document still exists. The existence-check
    result echoes the *same* UUID that ``_process_document_impl`` is
    looking up, so a future regression that starts validating the
    returned id (instead of just truthiness-checking it) is caught
    by this fake instead of silently passing.
    """

    def __init__(self, document_uuid: uuid.UUID):
        self._results = [_ScalarResult(), _ScalarResult(document_uuid)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("_LockedDocumentSession: unexpected extra execute() call")
        return self._results.pop(0)


class _FakeSession:
    """Richer async-session fake that returns specific rows for the
    ``KnowledgeDocument`` and ``KnowledgeBase`` lookups inside
    ``_process_document_impl`` and records commit/rollback/add calls
    for post-condition assertions.

    ``_process_document_impl`` issues two ``execute()`` calls before
    the chunking branch is reached:

    1. ``select(KnowledgeDocument)`` — doc lookup, returns the doc row.
    2. ``select(KnowledgeBase)`` — kb lookup, returns the kb row.

    It deletes a document's previous embeddings only when it swaps in the
    new ones, so a failure path that issued that delete would hit
    the extra-call guard below.
    """

    def __init__(self, *, doc: KnowledgeDocument, kb: KnowledgeBase):
        self._results = [
            _ScalarResult(doc),
            _ScalarResult(kb),
        ]
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.expunged = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("_FakeSession: unexpected extra execute() call")
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def add(self, value):
        self.added.append(value)

    def expunge(self, value):
        self.expunged.append(value)

    async def get(self, _model, _key):
        # The person the document is embedded for (``bind_llm_caller``): none here.
        return None


def _build_doc_and_kb():
    kb = KnowledgeBase(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="KB",
        chunk_size=256,
        chunk_overlap=32,
    )
    doc = KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=kb.id,
        filename="doc.txt",
        source_type="upload",
        content="Some content",
        # ``status="pending"`` matches the production state at SAQ
        # dequeue time. The model declares ``default="pending"`` but
        # SQLAlchemy column defaults are only materialized on flush /
        # DB INSERT, NOT on Python instance creation — so a test
        # fixture that omits ``status`` here lands at ``status=None``,
        # which the new "non-``pending``-is-a-duplicate-job" guard in
        # ``_process_document_impl`` would
        # treat as advanced-state and short-circuit the function
        # before reaching the error / no-chunks / no-embeddings paths
        # the existing tests exercise. Setting it explicitly here
        # documents the production state and unblocks every test in
        # this file from a single source of truth.
        status="pending",
    )
    return doc, kb


@pytest.mark.asyncio
async def test_fake_session_execute_guard_raises_on_extra_calls():
    """Defensive guard on the ``_FakeSession`` test fixture itself.

    The fixture pre-loads two ``execute()`` results
    (doc lookup, kb lookup) and raises on a third
    call.  If a future refactor of ``_process_document_impl`` issues
    extra ``execute()`` calls, the assertion message points the test
    author straight at the unexpected call rather than at a confusing
    "list index out of range".
    """
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)

    await session.execute(None)
    await session.execute(None)
    with pytest.raises(AssertionError, match="unexpected extra execute"):
        await session.execute(None)


@pytest.mark.asyncio
async def test_no_chunks_produced_during_preprocessing_marks_document_as_error(
    monkeypatch,
):
    """When ``chunk_text`` returns zero chunks (e.g. a document with
    only whitespace, only too-short paragraphs that get filtered out,
    or a content type the preprocessor can't extract), the document
    must be marked as ``error`` with an actionable user-facing message
    and an ``embedding_failed`` activity log — instead of raising
    a hard ``ValueError`` that propagates to the worker retry loop.

    Note: the test document carries non-empty ``content`` —
    ``chunk_text`` is monkeypatched to return ``[]`` to simulate the
    "preprocessor produced no chunks" path.  We exercise that path
    rather than ``doc.content == ""`` because the latter is filtered
    earlier in ``_process_document_impl`` before it reaches chunking.
    """
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    log_mock = AsyncMock()
    get_embedding_provider = AsyncMock()

    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(document_processor, "chunk_text", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr("app.llm.router.get_embedding_provider", get_embedding_provider)

    result = await document_processor._process_document_impl(str(doc.id))

    assert result == 0
    assert doc.status == "error"
    assert "no extractable text" in (doc.error_message or "").lower()
    assert get_embedding_provider.await_count == 0
    assert len(session.added) == 0
    # ``_process_document_impl`` issues 2 commits on this path:
    #   1. doc.status = "processing" (post doc/kb lookup)
    #   2. doc.status = "error" + activity log (inside
    #      ``_mark_document_unprocessable``).
    assert session.commits == 2
    log_mock.assert_awaited_once()
    assert log_mock.await_args.args[2] == "embedding_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("embedding", [[], [0.0, 0.0, 0.0]])
async def test_empty_or_zero_embedding_vectors_handled_gracefully(
    monkeypatch, embedding
):
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    log_mock = AsyncMock()
    provider = SimpleNamespace(embed=AsyncMock(return_value=embedding), provider_name="test")
    get_embedding_provider = AsyncMock(return_value=(provider, "embed-model", str(uuid.uuid4())))

    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor,
        "chunk_text",
        lambda *_args, **_kwargs: ["This chunk has enough content to be embedded."],
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr("app.llm.router.get_embedding_provider", get_embedding_provider)

    result = await document_processor._process_document_impl(str(doc.id))

    assert result == 0
    assert doc.status == "error"
    assert "returned no vectors" in (doc.error_message or "").lower()
    assert get_embedding_provider.await_count == 1
    assert provider.embed.await_count == 1
    assert len(session.added) == 0
    # Same commit accounting as the "no chunks" path — see the
    # commit-count comment in
    # ``test_no_chunks_produced_during_preprocessing_marks_document_as_error``.
    assert session.commits == 2
    log_mock.assert_awaited_once()
    assert log_mock.await_args.args[2] == "embedding_failed"


@pytest.mark.asyncio
async def test_dimension_mismatch_marks_non_retryable_error(monkeypatch):
    """When the provider produces a vector whose size differs from the
    platform-configured embedding dimension, the document must be flagged as a
    *non-retryable* error (a config mismatch re-embedding can't fix) rather than
    silently stored — which would leave the KB permanently "stale" and make
    "Re-embed all" loop forever without converging.
    """
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    log_mock = AsyncMock()
    # Provider configured for 1536 dims but returns a 3-dim vector.
    provider = SimpleNamespace(
        embed=AsyncMock(return_value=[0.1, 0.2, 0.3]),
        provider_name="test",
        embedding_dimensions=1536,
    )
    get_embedding_provider = AsyncMock(
        return_value=(provider, "text-embedding-3-large", str(uuid.uuid4()))
    )

    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor,
        "chunk_text",
        lambda *_args, **_kwargs: ["This chunk has enough content to be embedded."],
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr("app.llm.router.get_embedding_provider", get_embedding_provider)

    result = await document_processor._process_document_impl(str(doc.id))

    assert result == 0
    assert doc.status == "error"
    # Actionable, dimension-specific message.
    assert "dimension" in (doc.error_message or "").lower()
    assert "1536" in (doc.error_message or "")
    # Non-retryable: re-embedding can't reconcile a config mismatch, so the
    # auto-retry schedule must be cleared (no perpetual retry storm).
    assert doc.embedding_next_retry_at is None
    # The mismatched vectors must NOT be persisted, and the previous ones are
    # kept: no delete was issued (the session would refuse a third execute).
    assert len(session.added) == 0
    assert session.commits == 2
    log_mock.assert_awaited_once()
    assert log_mock.await_args.args[2] == "embedding_failed"


@pytest.mark.asyncio
async def test_a_dimension_mismatch_on_a_config_switched_meanwhile_is_due_now(monkeypatch):
    """The job embedded on the old provider while an admin switched to another
    one: the new provider may produce the right size, so the failure is retried
    at once instead of left for good."""
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    provider = SimpleNamespace(
        embed=AsyncMock(return_value=[0.1, 0.2, 0.3]), provider_name="test", embedding_dimensions=1536
    )
    used_config = str(uuid.uuid4())
    commits_when_asked: list[int] = []

    async def _moved(_config_id):
        commits_when_asked.append(session.commits)
        return True

    moved = AsyncMock(side_effect=_moved)
    retried = AsyncMock(return_value=True)
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor, "chunk_text", lambda *_a, **_kw: ["This chunk has enough content to be embedded."]
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", AsyncMock())
    monkeypatch.setattr(document_processor, "embedding_config_moved", moved)
    monkeypatch.setattr(document_processor, "retry_provider_failure_now", retried)
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider",
        AsyncMock(return_value=(provider, "text-embedding-3-large", used_config)),
    )

    await document_processor._process_document_impl(str(doc.id))

    assert doc.status == "error"
    assert "dimension" in doc.error_message
    assert moved.await_args.args[0] == used_config
    # Asked once the error is committed: the job holds no connection meanwhile.
    assert commits_when_asked == [2]
    # A guarded write on the row, never the in-memory document the commit left stale.
    assert retried.await_args.args == (session, doc.id)
    assert doc.embedding_next_retry_at is None


@pytest.mark.asyncio
async def test_a_content_failure_never_asks_whether_the_config_moved(monkeypatch):
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    moved = AsyncMock(return_value=True)
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(document_processor, "chunk_text", lambda *_a, **_kw: [])
    monkeypatch.setattr(document_processor, "log_kb_activity", AsyncMock())
    monkeypatch.setattr(document_processor, "embedding_config_moved", moved)

    await document_processor._process_document_impl(str(doc.id))

    assert doc.status == "error"
    assert doc.embedding_next_retry_at is None
    moved.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dimension_mismatch_stops_after_the_first_batch(monkeypatch):
    """A model that cannot produce the configured size costs one batch, not the document."""
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    provider = SimpleNamespace(
        embed=AsyncMock(return_value=[0.1, 0.2, 0.3]), provider_name="test", embedding_dimensions=1536
    )
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor,
        "chunk_text",
        lambda *_args, **_kwargs: [f"Chunk {i} has enough content to be embedded." for i in range(40)],
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", AsyncMock())
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider",
        AsyncMock(return_value=(provider, "text-embedding-3-large", str(uuid.uuid4()))),
    )

    assert await document_processor._process_document_impl(str(doc.id)) == 0
    assert doc.status == "error"
    assert provider.embed.await_count == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_text",
    [
        "deadlock detected",
        "canceling statement due to statement timeout",
        "LockNotAvailableError: could not obtain lock on row in relation knowledge_documents",
    ],
)
async def test_process_document_retries_on_retryable_db_errors(
    monkeypatch,
    error_text: str,
):
    document_id = str(uuid.uuid4())
    mark_error = AsyncMock()
    sleep_mock = AsyncMock()
    impl_mock = AsyncMock(side_effect=[Exception(error_text), 3])

    monkeypatch.setattr(document_processor, "_process_document_impl", impl_mock)
    monkeypatch.setattr(document_processor, "_mark_document_error", mark_error)
    monkeypatch.setattr(document_processor.asyncio, "sleep", sleep_mock)

    result = await document_processor.process_document(document_id)

    assert result == 3
    assert impl_mock.await_count == 2
    # Sleep must be called once with a positive exponential-backoff delay.
    # The exact value is jittered so we only assert the range: attempt 1
    # uses base=10 s ±25 % → [7.5, 12.5].
    sleep_mock.assert_awaited_once()
    (actual_delay,), _ = sleep_mock.await_args
    assert 7.5 <= actual_delay <= 12.5, f"expected delay in [7.5, 12.5], got {actual_delay}"
    mark_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_document_survives_multi_minute_ddl_wave(monkeypatch):
    """The retry budget must ride out a multi-minute DDL lock wave.

    Production regression: a plain (non-CONCURRENT) HNSW index
    build on ``embeddings`` holds a SHARE lock for minutes, cancelling the
    per-document DELETE with ``QueryCanceledError`` (SQLSTATE 57014) on
    every attempt.  With 3 attempts the document permanently failed
    mid-deploy; 5 attempts span ~5 minutes and recover once the build
    completes.
    """
    document_id = str(uuid.uuid4())
    mark_error = AsyncMock()
    sleep_mock = AsyncMock()
    # Exact production shape: SQLAlchemy wrapper with the asyncpg
    # QueryCanceledError (sqlstate 57014) buried in __cause__.
    def _wave_error() -> Exception:
        wrapper = Exception(
            "(sqlalchemy.dialects.postgresql.asyncpg.Error) "
            "<class 'asyncpg.exceptions.QueryCanceledError'>: "
            "canceling statement due to statement timeout "
            "[SQL: DELETE FROM embeddings WHERE embeddings.source_type = "
            "$1::VARCHAR AND embeddings.source_id = $2::VARCHAR]"
        )
        wrapper.__cause__ = _FakePGError(sqlstate="57014")
        return wrapper

    # 4 consecutive failures (a ~3-4 min lock window), success on attempt 5.
    impl_mock = AsyncMock(side_effect=[*(_wave_error() for _ in range(4)), 7])

    monkeypatch.setattr(document_processor, "_process_document_impl", impl_mock)
    monkeypatch.setattr(document_processor, "_mark_document_error", mark_error)
    monkeypatch.setattr(document_processor.asyncio, "sleep", sleep_mock)

    result = await document_processor.process_document(document_id)

    assert result == 7
    assert impl_mock.await_count == 5
    assert sleep_mock.await_count == 4
    # Delays must follow the exponential schedule (jittered ±25 %, capped).
    delays = [call.args[0] for call in sleep_mock.await_args_list]
    for delay, (low, high) in zip(
        delays, [(7.5, 12.5), (15.0, 25.0), (30.0, 50.0), (45.0, 60.0)], strict=True
    ):
        assert low <= delay <= high, f"delay {delay} outside [{low}, {high}]"
    mark_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_document_error_schedules_retryable_auto_retry(monkeypatch):
    """The outer-retry-exhausted path must leave the doc self-healable.

    Production regression: the per-document embeddings DELETE sits
    *before* the try block in ``_process_document_impl``, so when it fails
    (statement timeout during a DDL lock wave) the inner bookkeeping never
    runs and the doc is marked errored via ``_mark_document_error``. That
    helper MUST schedule a retryable auto-retry (``embedding_next_retry_at``
    set), otherwise the doc is parked in ``error`` forever and the dashboard
    "needs attention" banner never clears without manual action.
    """
    doc = KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename="wave-victim.md",
        source_type="upload",
        content="x" * 64,
        status="processing",
        chunk_count=0,
        embedding_retry_count=0,
        embedding_next_retry_at=None,
    )

    class _ErrorSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return _ScalarResult(doc)

        async def commit(self):
            return None

    monkeypatch.setattr(document_processor, "async_session", lambda: _ErrorSession())

    await document_processor._mark_document_error(
        str(doc.id), RuntimeError("canceling statement due to statement timeout")
    )

    assert doc.status == "error"
    assert doc.error_message is not None
    # The row holds the classified sentence, never the exception text.
    assert "canceling statement" not in doc.error_message
    assert doc.embedding_next_retry_at is not None, (
        "_mark_document_error must schedule an automatic re-embed so the "
        "recover_failed_documents sweeper self-heals the doc"
    )


def test_max_retries_covers_ddl_wave_window():
    """Guard the retry budget: 5 attempts with the exponential schedule.

    Lowering ``_MAX_RETRIES`` (or shrinking the backoff) below a ~4-5 min
    total window reintroduces the permanent re-indexing failures
    during HNSW / DDL deploys.
    """
    assert document_processor._MAX_RETRIES == 5
    # Maximum total sleep across all retries (jitter at +25 %, capped at 60 s).
    max_total_sleep = sum(
        min(
            document_processor._RETRY_BASE_DELAY_SECONDS
            * (2 ** (attempt - 1))
            * (1 + document_processor._RETRY_JITTER_FACTOR),
            document_processor._RETRY_MAX_DELAY_SECONDS,
        )
        for attempt in range(1, document_processor._MAX_RETRIES)
    )
    # Each attempt can additionally burn up to ~30 s of statement_timeout
    # in the lock queue; the whole budget must stay within the SAQ job
    # timeout (1200 s) with ample headroom.
    assert max_total_sleep + 30 * document_processor._MAX_RETRIES < 600


# ---------------------------------------------------------------------------
# _is_retryable_db_error — SQLSTATE chain-walk tests
# ---------------------------------------------------------------------------


class _FakePGError(Exception):
    """Minimal stand-in for an asyncpg / psycopg2 exception with a SQLSTATE."""

    def __init__(self, msg: str = "pg error", *, sqlstate: str | None = None, pgcode: str | None = None):
        super().__init__(msg)
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if pgcode is not None:
            self.pgcode = pgcode


@pytest.mark.parametrize(
    "code",
    ["57014", "55P03", "40001", "40P01"],
)
def test_is_retryable_db_error_detects_sqlstate_on_exception(code: str):
    """``_is_retryable_db_error`` must return True for each of the four
    PostgreSQL SQLSTATEs that indicate transient lock/timeout contention."""
    exc = _FakePGError(sqlstate=code)
    assert document_processor._is_retryable_db_error(exc) is True


def test_is_retryable_db_error_detects_sqlstate_via_pgcode_attr():
    """Detection must also read the ``pgcode`` attribute (some driver/wrapper
    layers expose the SQLSTATE under that name instead of ``sqlstate``)."""
    exc = _FakePGError(pgcode="57014")
    assert document_processor._is_retryable_db_error(exc) is True


def test_is_retryable_db_error_walks_exception_chain():
    """A SQLSTATE buried in ``__cause__`` must still be detected.

    SQLAlchemy wraps asyncpg exceptions so the driver SQLSTATE is on the
    *inner* exception, not the outermost wrapper.
    """
    wrapper = RuntimeError("sqlalchemy wrapped error")
    wrapper.__cause__ = _FakePGError(sqlstate="57014")
    assert document_processor._is_retryable_db_error(wrapper) is True


def test_is_retryable_db_error_detects_sqlstate_in_context_chain():
    """A SQLSTATE on ``__context__`` (implicit chaining) must also be found."""
    wrapper = RuntimeError("context-chained error")
    wrapper.__context__ = _FakePGError(sqlstate="55P03")
    assert document_processor._is_retryable_db_error(wrapper) is True


def test_is_retryable_db_error_returns_false_for_unrelated_sqlstate():
    """A non-retryable SQLSTATE (e.g. foreign key violation) must return False."""
    exc = _FakePGError(sqlstate="23503")  # foreign_key_violation
    # Also make sure string fallback doesn't trigger
    assert document_processor._is_retryable_db_error(exc) is False


# ---------------------------------------------------------------------------
# _compute_retry_delay — exponential backoff + jitter tests
# ---------------------------------------------------------------------------


def test_compute_retry_delay_attempt1_in_expected_range():
    """Attempt 1: base=10 s ±25 % → [7.5, 12.5]."""
    for _ in range(50):
        delay = document_processor._compute_retry_delay(1)
        assert 7.5 <= delay <= 12.5, f"attempt 1 delay {delay} out of [7.5, 12.5]"


def test_compute_retry_delay_attempt2_larger_than_attempt1():
    """Attempt 2 base (20 s) must produce higher expected delay than attempt 1 (10 s)."""
    # Attempt 2's jitter window is [15, 25], so even its minimum sample must
    # exceed attempt 1's maximum possible delay (12.5).
    delays_2 = [document_processor._compute_retry_delay(2) for _ in range(30)]
    assert min(delays_2) >= 15.0, f"attempt 2 min {min(delays_2)} < 15.0"


def test_compute_retry_delay_is_capped_at_max():
    """Very high attempt numbers must not produce a delay above _RETRY_MAX_DELAY_SECONDS."""
    for attempt in (10, 100, 1000):
        delay = document_processor._compute_retry_delay(attempt)
        assert delay <= document_processor._RETRY_MAX_DELAY_SECONDS, (
            f"attempt {attempt} delay {delay} exceeds cap {document_processor._RETRY_MAX_DELAY_SECONDS}"
        )


@pytest.mark.parametrize(
    "stale_origin",
    [
        # Documented production sources of StaleDataError from inside
        # ``_process_document_impl``.  Each test run pins the outer
        # ``except StaleDataError`` contract for one of them — the
        # outer ``process_document`` treats them all identically
        # regardless of which inner line surfaced the stale row.
        pytest.param(
            "select",
            id="stale-from-initial-select",
        ),
        pytest.param(
            "processing-commit",
            id="stale-from-processing-status-commit",
        ),
        pytest.param(
            "embedding-flush",
            id="stale-from-mid-embedding-flush",
        ),
    ],
)
async def test_process_document_treats_stale_data_error_as_benign_regardless_of_source(
    monkeypatch, caplog: pytest.LogCaptureFixture, stale_origin: str
):
    """Outer ``except StaleDataError`` is a single observable contract.

    Whatever inner line of ``_process_document_impl`` surfaces a
    ``StaleDataError`` — the initial doc lookup, the
    ``doc.status='processing'; commit()`` at the top of the function,
    or a flush partway through the embedding loop — the outer
    ``process_document`` retry loop MUST:

    * NOT retry (one ``StaleDataError`` means the document is gone;
      retrying just piles on more concurrent-delete noise).
    * NOT call ``_mark_document_error`` (the document is gone — there
      is nothing to mark).
    * Emit a single ``WARNING``-level "stale row update" log.
    * Return ``0`` chunks.

    Parametrising over the inner source documents the intent (we DO
    care that multiple inner lines route through the same outer
    handler — a future refactor that accidentally moves the
    ``except`` inside a narrower scope would fail at least one
    parameter) while keeping the assertion block (no-retry,
    no-mark, WARNING log) defined exactly once.
    """
    document_id = str(uuid.uuid4())
    mark_error = AsyncMock()

    # Each parametrised origin maps to a different ``side_effect``
    # shape, but they all bottom out at the same outer ``except
    # StaleDataError`` so the observable assertions below are
    # identical.  Mocking the impl is sufficient for this layer:
    # the inner-handler path (recovery commit raising StaleDataError
    # → swallowed by inner except → bare raise → re-raised as the
    # ORIGINAL exception) is purely internal and is not observable
    # at the outer layer, so it would not add coverage to simulate
    # it here.
    impl_mock = AsyncMock(
        side_effect=StaleDataError(
            f"UPDATE statement expected to update 1 row(s); 0 were "
            f"matched. (source={stale_origin})",
            None,
            None,
        )
    )

    monkeypatch.setattr(document_processor, "_process_document_impl", impl_mock)
    monkeypatch.setattr(document_processor, "_mark_document_error", mark_error)

    with caplog.at_level("WARNING"):
        result = await document_processor.process_document(document_id)

    # No-retry contract: ``_process_document_impl`` was awaited exactly
    # once.  A regression that accidentally moves the ``except
    # StaleDataError`` into the retry branch (so it catches → continue
    # instead of catching → return) would fail this assertion.
    assert result == 0
    impl_mock.assert_awaited_once()
    mark_error.assert_not_awaited()
    assert "stale row update" in caplog.text
    assert document_id in caplog.text
    assert not any(
        record.levelname == "ERROR" for record in caplog.records
    )


@pytest.mark.asyncio
async def test_process_document_impl_skips_a_missing_document_at_debug(
    monkeypatch, caplog: pytest.LogCaptureFixture
):
    """A document deleted while its job waited is an expected outcome, not an operator matter.

    Deleting a knowledge base leaves its already-dequeued jobs to find their
    documents gone: 48 WARNING lines in one second on the worker.
    """
    document_id = str(uuid.uuid4())
    monkeypatch.setattr(document_processor, "async_session", _MissingDocumentSession)

    with caplog.at_level("DEBUG", logger=document_processor.logger.name):
        result = await document_processor._process_document_impl(document_id)

    assert result == 0
    [record] = [r for r in caplog.records if "stale queue entry" in r.getMessage()]
    assert record.levelno == logging.DEBUG
    assert document_id in record.getMessage()
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_process_document_impl_reenqueues_when_document_is_locked(
    monkeypatch, caplog: pytest.LogCaptureFixture
):
    """SKIP-LOCKED gap must re-enqueue the job (regression for stuck-pending docs).

    Before this fix, the ``FOR UPDATE SKIP LOCKED`` arm hitting a
    locked-but-existing document caused ``_process_document_impl`` to
    log INFO and return 0 — abandoning the SAQ job entirely.
    Documents stayed in ``pending`` forever because SAQ does not retry
    a function that returned cleanly.  The new behaviour schedules a
    fresh SAQ job with a short delay so the same document is retried
    once the conflicting transaction commits.
    """
    document_id = str(uuid.uuid4())
    document_uuid = uuid.UUID(document_id)
    # Wire the fake so the existence-check returns the *same* UUID that
    # ``_process_document_impl`` parses from ``document_id``. The prod
    # code currently only truthiness-checks the result, but a future
    # regression that compares the returned id to ``document_uuid``
    # would otherwise silently pass against a random ``uuid.uuid4()``.
    monkeypatch.setattr(
        document_processor,
        "async_session",
        lambda: _LockedDocumentSession(document_uuid),
    )
    reenqueue_mock = AsyncMock()
    monkeypatch.setattr(
        document_processor,
        "_reenqueue_document_after_lock",
        reenqueue_mock,
    )

    with caplog.at_level("INFO"):
        result = await document_processor._process_document_impl(document_id)

    assert result == 0
    reenqueue_mock.assert_awaited_once_with(document_id, on_behalf_of=None)
    assert "currently locked by another transaction" in caplog.text
    assert "scheduling retry" in caplog.text
    assert document_id in caplog.text
    # The pre-existing "stale queue entry" branch is reserved for the
    # missing-document case and must not be reached here.
    assert "stale queue entry" not in caplog.text


@pytest.mark.asyncio
async def test_reenqueue_after_lock_schedules_in_the_future(monkeypatch):
    """The re-enqueue MUST hand SAQ a ``scheduled`` epoch in the future.

    ``scheduled=0`` would put the job back into the immediate-dequeue
    list, where it would be picked up before the conflicting
    transaction has had time to commit — exactly the lock-storm the
    SKIP-LOCKED change was supposed to prevent.
    """
    import sys
    import time as _time

    document_id = str(uuid.uuid4())
    captured: dict = {}

    class _FakeQueue:
        async def enqueue(self, job):
            captured["job"] = job

    class _FakeWorkerModule:
        queue = _FakeQueue()

    monkeypatch.setitem(sys.modules, "app.worker", _FakeWorkerModule)
    monkeypatch.setattr(
        document_processor, "_LOCKED_DOC_REENQUEUE_DELAY_SECONDS", 42
    )

    before = int(_time.time())
    await document_processor._reenqueue_document_after_lock(document_id)
    after = int(_time.time())

    job = captured.get("job")
    assert job is not None
    assert job.function == "process_document_job"
    assert job.kwargs == {"document_id": document_id}
    # Distinct stable key: the primary ``process-document-<id>`` key would
    # be deduped by SAQ against OUR OWN still-active job (this re-enqueue
    # fires from inside the job processing the document), silently
    # dropping the retry.  The ``-relock`` suffix keeps it alive while
    # still deduping repeated relock retries for the same doc.
    assert job.key == f"process-document-{document_id}-relock"
    # ``scheduled`` is epoch seconds — must fall inside [now+delay, after+delay].
    assert before + 42 <= job.scheduled <= after + 42


class _AlreadyAdvancedDocumentSession:
    """Async-session fake for the "non-``pending`` status guard" path.

    The guard short-circuits *immediately* after the
    ``FOR UPDATE SKIP LOCKED`` SELECT — so the fake only needs to
    deliver one ``execute()`` result (the doc lookup). A second
    ``execute()`` call would indicate the guard let the function
    proceed into the (KB lookup / chunking) path,
    which is exactly the regression we're guarding against.
    """

    def __init__(self, doc: KnowledgeDocument):
        self._results = [_ScalarResult(doc)]
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self.expunged: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError(
                "_AlreadyAdvancedDocumentSession: unexpected extra execute() — "
                "the status guard must short-circuit immediately after the "
                "first SELECT"
            )
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def add(self, value):
        self.added.append(value)

    def expunge(self, value):
        self.expunged.append(value)


class _NoAutoflushCtx:
    """Minimal ``db.no_autoflush`` context-manager stub."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _ReLockContendedSession:
    """Session fake for the re-lock ``FOR UPDATE SKIP LOCKED`` contention path.

    Simulates the scenario where:

    1. Initial doc lock (SKIP LOCKED) succeeds — doc is returned.
    2. KB lookup succeeds.
    3. Re-lock (SKIP LOCKED) returns **no row** — another worker holds
       the lock and is in the process of committing the ``ready`` state.

    The function under test must react by rolling back the pending
    session writes and returning 0 without raising.
    """

    def __init__(self, *, doc: KnowledgeDocument, kb: KnowledgeBase):
        self._results = [
            _ScalarResult(doc),  # initial SKIP LOCKED doc lock
            _ScalarResult(kb),  # KB lookup
            _ScalarResult(),  # re-lock SKIP LOCKED — no row (locked by other worker)
        ]
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self.expunged: list = []
        self.no_autoflush = _NoAutoflushCtx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("_ReLockContendedSession: unexpected extra execute() call")
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    def add(self, value):
        self.added.append(value)

    def expunge(self, value):
        self.expunged.append(value)

    async def get(self, _model, _key):
        # The person the document is embedded for (``bind_llm_caller``): none here.
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "advanced_status",
    [
        # ``processing``: another worker is actively embedding the doc
        # right now (typical sweeper-induced duplicate under saturated
        # workers — the sweeper re-enqueued because pending > 5 min,
        # then the original job was picked up first).
        "processing",
        # ``ready``: original job finished, second job is stale — the
        # row was already flipped to ``ready`` by the first worker
        # before the duplicate dequeued.
        "ready",
        # ``error``: a previous attempt errored out; admins re-process
        # errored docs via the explicit reprocess API, not by being
        # silently re-picked off a stale queue entry.  Treat as
        # advanced-state and skip.
        "error",
    ],
)
async def test_process_document_impl_skips_when_status_already_advanced(
    monkeypatch, caplog, advanced_status
):
    """Sweeper-induced duplicate jobs must NOT re-embed an already-processed doc.

    Regression: under worker
    saturation the stuck-document sweeper can queue a second job for
    a doc whose original job is still in-flight (or already finished).
    When the second worker dequeues, the row may already have
    advanced past ``pending``.  Without an early-exit guard,
    ``_process_document_impl`` would proceed to delete the just-
    written embeddings and re-do the work — burning duplicate
    embedding-provider tokens and flipping the row's visible status
    from ``ready → pending → ready`` for the user.  The guard short-
    circuits with a single INFO log and zero downstream calls.

    Pins the **attempt == 1** path. The companion test
    ``test_process_document_impl_internal_retry_bypasses_duplicate_guard``
    pins the inverse contract on ``attempt > 1`` so the outer
    ``process_document`` retry loop is never silently defeated by
    this guard.
    """
    doc, _kb = _build_doc_and_kb()
    doc.status = advanced_status
    session = _AlreadyAdvancedDocumentSession(doc=doc)
    chunk_text_mock = AsyncMock()
    get_embedding_provider = AsyncMock()
    log_mock = AsyncMock()
    reenqueue_mock = AsyncMock()

    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(document_processor, "chunk_text", chunk_text_mock)
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr(
        document_processor,
        "_reenqueue_document_after_lock",
        reenqueue_mock,
    )
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider", get_embedding_provider
    )

    with caplog.at_level("INFO"):
        result = await document_processor._process_document_impl(str(doc.id))

    # Contract:
    #   * 0 chunks returned (no work done).
    #   * 0 commits / rollbacks (the SELECT acquired a row lock via
    #     ``FOR UPDATE SKIP LOCKED`` — caller's session context-manager
    #     handles the rollback on exit; the function itself does NOT
    #     issue commits / rollbacks on the early-exit path because
    #     no writes were attempted).
    #   * 0 embedding-provider lookups, 0 chunking calls, no embed
    #     records appended, no KB activity logged.
    #   * Doc status untouched.
    #   * No re-enqueue (this is NOT a SKIP-LOCKED-saw-row-missing
    #     case — the row was found, just already past pending).
    #   * INFO log mentions the document id, the observed status, and
    #     the words "duplicate job" so operators searching log
    #     aggregators can identify the cause.
    assert result == 0
    assert session.commits == 0
    assert session.rollbacks == 0
    assert chunk_text_mock.await_count == 0
    assert get_embedding_provider.await_count == 0
    assert log_mock.await_count == 0
    assert reenqueue_mock.await_count == 0
    assert len(session.added) == 0
    assert doc.status == advanced_status  # untouched
    assert "duplicate job" in caplog.text.lower()
    assert advanced_status in caplog.text
    assert str(doc.id) in caplog.text


@pytest.mark.asyncio
async def test_process_document_impl_relock_skip_locked_exits_cleanly(
    monkeypatch, caplog: pytest.LogCaptureFixture
):
    """Re-lock SKIP LOCKED must roll back and return 0 when the row is unavailable.

    Previously the re-lock used ``nowait=True``: under contention one
    worker still acquired the lock, but every losing worker raised
    ``LockNotAvailableError``. That error propagated into the outer
    retry / ``_mark_document_error`` path and could overwrite the
    winner's already-committed ``ready`` with ``error``.

    The new ``skip_locked=True`` re-lock returns no row when the document
    row is held by another transaction (or was concurrently deleted), so
    this worker cleanly exits: roll back the pending embedding INSERTs and
    return 0. When the holder is the worker that wins the race it commits
    the final ``ready`` state; when it is an unrelated transaction
    (KB sync / reprocess), the row stays ``processing`` and the
    stuck-document sweeper recovers it on a later tick.
    """
    from types import SimpleNamespace

    doc, kb = _build_doc_and_kb()
    session = _ReLockContendedSession(doc=doc, kb=kb)

    # Provide a minimal embedding so the code reaches the re-lock.
    provider = SimpleNamespace(
        embed=AsyncMock(return_value=[0.1, 0.2, 0.3]),
        provider_name="test",
        embedding_dimensions=None,
    )

    async def _fake_get_provider(_db, **_kwargs):
        return (provider, "test-model", str(uuid.uuid4()))

    log_mock = AsyncMock()

    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    # Return one real-length chunk so the >= 10-char filter passes and
    # the code reaches the embedding + re-lock path.
    monkeypatch.setattr(
        document_processor,
        "chunk_text",
        lambda *_a, **_kw: ["this chunk is long enough to pass the ten-char filter"],
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr("app.llm.router.get_embedding_provider", _fake_get_provider)
    # _recount_kb_stats opens its own session — never reached in this path.
    monkeypatch.setattr(document_processor, "_recount_kb_stats", AsyncMock())

    with caplog.at_level("INFO"):
        result = await document_processor._process_document_impl(str(doc.id))

    # The worker that fails to re-acquire the row must:
    #   * return 0 (it did not finalize — the lock holder does, or the
    #     stuck-document sweeper recovers the row if no one finalizes)
    #   * roll back the pending session writes (embedding records NOT committed)
    #   * NOT log an embedding_completed activity (only the finalizing worker does)
    #   * emit an INFO explaining why it is discarding its work
    assert result == 0
    assert session.rollbacks == 1, (
        "rollback must be called exactly once when the re-lock SKIP LOCKED "
        "returns no row — without it the pending embedding records would leak "
        "into the next commit and produce duplicates"
    )
    # Only the status=processing commit fires before the re-lock.
    assert session.commits == 1, "exactly one commit expected before the re-lock: status=processing"
    # The losing worker stages nothing: the embeddings are swapped in only
    # once the re-lock holds, so the previous ones stay untouched.
    assert len(session.added) == 0
    # Only the finalizing worker logs embedding_completed.
    assert log_mock.await_count == 0
    # The INFO explaining the no-row exit must be present so operators can
    # identify the event in log aggregators.
    assert "re-lock returned no row" in caplog.text
    assert str(doc.id) in caplog.text


@pytest.mark.asyncio
async def test_reenqueue_after_lock_swallows_errors(monkeypatch, caplog):
    """Re-enqueue failures must NOT propagate.

    The stuck-document sweeper is the second-line recovery — a Redis
    blip on the re-enqueue path should never surface to the caller
    (the caller is ``_process_document_impl`` which already returned
    0 for the locked-row case; raising here would turn that into a
    doc-error record in the outer ``process_document`` retry loop).
    """
    import sys

    document_id = str(uuid.uuid4())

    class _BrokenQueue:
        async def enqueue(self, job):
            raise RuntimeError("redis is having a bad day")

    class _FakeWorkerModule:
        queue = _BrokenQueue()

    monkeypatch.setitem(sys.modules, "app.worker", _FakeWorkerModule)

    with caplog.at_level("WARNING"):
        # Must not raise.
        await document_processor._reenqueue_document_after_lock(document_id)

    assert "Failed to re-enqueue locked document" in caplog.text
    assert document_id in caplog.text


# ---------------------------------------------------------------------------
# Duplicate-job guard does NOT defeat the internal retry loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "previous_attempt_status",
    [
        # ``processing``: previous attempt committed the status flip
        # at the top of ``_process_document_impl`` (line 346 commit),
        # then failed somewhere in the embedding loop before the final
        # ``ready`` commit. The exception was retryable so
        # ``process_document`` is calling us back — the row's status
        # is still ``processing`` from our own previous attempt.
        "processing",
        # ``error``: previous attempt's inner ``except`` block
        # committed ``status = "error"`` + the ``embedding_failed``
        # activity log before re-raising. The outer wrapper saw a
        # retryable error and is calling us back. Without the
        # ``attempt > 1`` bypass, the duplicate-job guard would see
        # ``error`` and short-circuit, silently defeating the retry.
        "error",
    ],
)
async def test_process_document_impl_internal_retry_bypasses_duplicate_guard(
    monkeypatch, caplog, previous_attempt_status
):
    """Regression for the duplicate-job guard defeating the retry loop.

    The duplicate-job guard (which short-
    circuits the function when ``doc.status != "pending"``) MUST
    bypass on internal retries from ``process_document`` — otherwise
    the retry loop is silently defeated on every transient DB error:

    1. Attempt 1: flip status to ``processing``, commit, embedding
       fails with a retryable error (deadlock / serialization /
       lock-not-available), inner except commits ``status = "error"``,
       re-raise.
    2. ``process_document.except``: ``_is_retryable_db_error(exc)``
       returns True, sleep, call ``_process_document_impl`` again.
    3. Attempt 2 WITHOUT the bypass: SELECT FOR UPDATE sees
       ``status = "error"``, guard fires, return 0. Retry lost.

    The fix gates the guard on ``attempt == 1``. This test pins the
    bypass by driving the real ``_process_document_impl`` with
    ``attempt=2`` against a doc whose status is already advanced
    (the previous attempt's state) and asserts the function proceeds
    PAST the guard (evidenced by reaching the chunking branch — we
    monkeypatch ``chunk_text`` to return ``[]`` so the run completes
    cheaply via the no-chunks path rather than running real embeddings,
    which is enough to prove the guard did not short-circuit).
    """
    doc, kb = _build_doc_and_kb()
    doc.status = previous_attempt_status
    session = _FakeSession(doc=doc, kb=kb)

    def _empty_chunks(*_args, **_kwargs):
        return []

    get_embedding_provider = AsyncMock()
    log_mock = AsyncMock()

    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(document_processor, "chunk_text", _empty_chunks)
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider", get_embedding_provider
    )

    with caplog.at_level("INFO"):
        result = await document_processor._process_document_impl(
            str(doc.id), attempt=2
        )

    # Proof the guard was BYPASSED, not skipped:
    #   * Function reached the chunking branch and committed at least
    #     the ``status = "processing"`` write (line 346) — the guard
    #     path commits ZERO times.
    #   * The no-chunks branch fires (``chunk_text`` returns []),
    #     which writes ``status = "error"`` + logs ``embedding_failed``.
    #     That's only reachable AFTER the guard.
    #   * The "duplicate job" INFO line is NOT present in the log —
    #     that's the guard's signature and would only fire if the
    #     bypass had been silently dropped.
    assert result == 0
    assert session.commits >= 1, (
        "function must commit at least the status=processing write; "
        "zero commits means the guard short-circuited the retry — "
        "regression of the retry-defeat bug"
    )
    assert log_mock.await_count == 1
    assert log_mock.await_args.args[2] == "embedding_failed"
    assert "duplicate job" not in caplog.text.lower(), (
        "guard short-circuited the internal retry — bypass on attempt > 1 "
        "is broken (retry-defeat regression)"
    )


@pytest.mark.asyncio
async def test_process_document_forwards_attempt_index_to_impl(monkeypatch):
    """``process_document`` MUST pass the 1-based ``attempt`` index
    into ``_process_document_impl`` so the duplicate-job guard there
    can distinguish a legitimate internal retry from a stale duplicate
    SAQ job. A regression that drops the kwarg (e.g. ``return await
    _process_document_impl(document_id)``) silently re-introduces the
    retry-defeat bug.
    """
    document_id = str(uuid.uuid4())
    impl_mock = AsyncMock(
        side_effect=[
            Exception("deadlock detected"),  # attempt 1 — retryable
            5,  # attempt 2 — succeeds, returns chunk count
        ]
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(document_processor, "_process_document_impl", impl_mock)
    monkeypatch.setattr(document_processor.asyncio, "sleep", sleep_mock)

    result = await document_processor.process_document(document_id)

    assert result == 5
    assert impl_mock.await_count == 2
    # Pin the kwarg contract: attempt 1, then attempt 2.
    # A regression that drops the kwarg trips here against a missing
    # ``attempt`` key rather than waiting for production to surface
    # the silently-defeated retry behaviour.
    assert impl_mock.await_args_list[0].kwargs.get("attempt") == 1
    assert impl_mock.await_args_list[1].kwargs.get("attempt") == 2
    # First positional arg is still the document id on both attempts.
    assert impl_mock.await_args_list[0].args == (document_id,)
    assert impl_mock.await_args_list[1].args == (document_id,)


@pytest.mark.asyncio
async def test_process_document_end_to_end_retry_through_real_impl(monkeypatch):
    """End-to-end: drive ``process_document`` through the REAL
    ``_process_document_impl`` (not mocked) across attempts 1 → 2
    where attempt 1 commits ``status = "error"`` via the inner except
    and attempt 2 must proceed past the guard.

    This is the canonical regression test for the guard defeating the retry:
    the previous suite mocked ``_process_document_impl`` entirely, so
    the guard-vs-retry interaction was invisible to CI. Driving the
    real impl over two attempts with a doc that transitions
    ``pending → processing → error → processing → error`` (attempt 2
    exits cleanly via the empty-embedding-vector branch) is the only
    way to actually catch a future regression at the integration layer.
    """
    doc, kb = _build_doc_and_kb()

    # Build two independent fake sessions: one per attempt. Production
    # opens a fresh ``async with async_session()`` block per
    # ``_process_document_impl`` invocation, so each call sees its own
    # pre-loaded execute() results.
    session_attempt_1 = _FakeSession(doc=doc, kb=kb)
    session_attempt_2 = _FakeSession(doc=doc, kb=kb)
    sessions_iter = iter([session_attempt_1, session_attempt_2])

    # Attempt 1: the embedding-provider lookup raises a retryable
    # error AFTER ``status = "processing"`` has been committed, so
    # the inner ``except Exception`` block commits ``status = "error"``
    # + the ``embedding_failed`` activity log before re-raising. The
    # outer ``process_document`` sees the retryable error and retries.
    #
    # Attempt 2: the provider lookup succeeds but the provider returns
    # an empty embedding vector. ``_embed_one`` logs a warning and
    # returns ``None`` for the chunk, ``chunk_count`` stays at 0, and
    # the function exits cleanly via ``_mark_document_unprocessable``
    # (status = "error", returns 0). No further retry is triggered
    # because that path returns normally rather than raising.
    attempt_counter = {"n": 0}

    async def _provider_side_effect(_db, **_kwargs):
        attempt_counter["n"] += 1
        if attempt_counter["n"] == 1:
            raise Exception("deadlock detected on embedding-provider lookup")
        provider = SimpleNamespace(
            embed=AsyncMock(return_value=[]),  # empty → _embed_one returns None
            provider_name="test",
        )
        return (provider, "embed-model", str(uuid.uuid4()))

    log_mock = AsyncMock()
    sleep_mock = AsyncMock()

    # Real-length chunk so the >= 10-char filter doesn't short-circuit
    # before the provider lookup — we need attempt 1 to actually reach
    # ``get_embedding_provider`` for the retryable raise to fire.
    def _real_chunks(*_args, **_kwargs):
        return ["this chunk is long enough to pass the ten-character filter"]

    monkeypatch.setattr(
        document_processor, "async_session", lambda: next(sessions_iter)
    )
    monkeypatch.setattr(document_processor, "chunk_text", _real_chunks)
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr(
        "app.llm.router.get_embedding_provider", _provider_side_effect
    )
    monkeypatch.setattr(document_processor.asyncio, "sleep", sleep_mock)

    result = await document_processor.process_document(str(doc.id))

    # End-to-end contract:
    #   * Outer ``process_document`` retries once (attempt 1 raised
    #     retryable error → attempt 2 ran).
    #   * Doc ends in ``error`` — the FINAL state from attempt 2's
    #     empty-vector branch (NOT the intermediate ``error`` attempt
    #     1's inner except committed, although they coincidentally
    #     share the same status value).
    #   * Provider lookup was awaited on BOTH attempts — proves
    #     attempt 2 proceeded PAST the guard (the regression vector).
    #   * Sleep between attempts was awaited exactly once (attempt
    #     1 → attempt 2 back-off, 2 seconds).
    assert result == 0
    assert doc.status == "error"
    assert attempt_counter["n"] == 2, (
        "provider lookup must run on BOTH attempts — if it only ran "
        "once, attempt 2's guard short-circuited the function before "
        "reaching the chunking branch (retry-defeat regression)"
    )
    sleep_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_document_for_an_excluded_person_is_refused_for_good(monkeypatch):
    """An embedding provider that excludes the person a document is embedded
    for refuses it: the document is marked with the platform sentence,
    nothing is embedded, and no automatic retry is booked - a retry would meet
    the same exclusion, and running it without the job's person could send the
    document after all."""
    from app.llm.provider_access import ProviderNotUsableError
    from app.services.kb_document_errors import EMBEDDING_NOT_AVAILABLE_MESSAGE

    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    log_mock = AsyncMock()
    author = str(uuid.uuid4())
    get_embedding_provider = AsyncMock(side_effect=ProviderNotUsableError("excluded"))

    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor,
        "chunk_text",
        lambda *_args, **_kwargs: ["This chunk has enough content to be embedded."],
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", log_mock)
    monkeypatch.setattr("app.llm.router.get_embedding_provider", get_embedding_provider)

    result = await document_processor._process_document_impl(str(doc.id), on_behalf_of=author)

    assert result == 0
    get_embedding_provider.assert_awaited_once_with(session, user_id=author)
    assert doc.status == "error"
    assert doc.error_message == EMBEDDING_NOT_AVAILABLE_MESSAGE
    assert doc.added_by_user_id == uuid.UUID(author)  # a re-embed checks them again
    assert doc.embedding_next_retry_at is None
    assert session.added == []
    log_mock.assert_awaited_once()
    assert log_mock.await_args.args[2] == "embedding_failed"


@pytest.mark.asyncio
async def test_a_document_queued_for_no_one_is_embedded_for_its_knowledge_bases_owner(monkeypatch):
    """A job that names no person (a sweeper re-enqueue, a scheduled retry)
    embeds for the knowledge base's owner, whose exclusions then hold."""
    doc, kb = _build_doc_and_kb()
    session = _FakeSession(doc=doc, kb=kb)
    from app.llm.provider_access import ProviderNotUsableError

    get_embedding_provider = AsyncMock(side_effect=ProviderNotUsableError("excluded"))
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor,
        "chunk_text",
        lambda *_args, **_kwargs: ["This chunk has enough content to be embedded."],
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", AsyncMock())
    monkeypatch.setattr("app.llm.router.get_embedding_provider", get_embedding_provider)

    await document_processor._process_document_impl(str(doc.id))

    get_embedding_provider.assert_awaited_once_with(session, user_id=kb.user_id)


# ── A re-embed checks the person recorded on the document ─────────


def _company_managed_doc(added_by):
    doc, kb = _build_doc_and_kb()
    kb.is_company_managed = True
    kb.user_id = None
    doc.added_by_user_id = added_by
    return doc, kb


def _embedding_lookup(excluded):
    """``get_embedding_provider`` refusing the people in *excluded*, and the
    provider it hands everyone else (whose ``embed`` returns no vector, so the
    job stops right after the provider received the chunk)."""
    from app.llm.provider_access import ProviderNotUsableError

    provider = SimpleNamespace(embed=AsyncMock(return_value=[]), provider_name="test")

    async def _lookup(_db, *, user_id=None):
        if user_id in excluded:
            raise ProviderNotUsableError("excluded")
        return provider, "embed-model", uuid.uuid4()

    return AsyncMock(side_effect=_lookup), provider


def _patch_job(monkeypatch, session, get_embedding_provider):
    monkeypatch.setattr(document_processor, "async_session", lambda: session)
    monkeypatch.setattr(
        document_processor,
        "chunk_text",
        lambda *_args, **_kwargs: ["This chunk has enough content to be embedded."],
    )
    monkeypatch.setattr(document_processor, "log_kb_activity", AsyncMock())
    monkeypatch.setattr("app.llm.router.get_embedding_provider", get_embedding_provider)


async def _reembed_job_kwargs(monkeypatch, doc_id: str) -> dict:
    """The kwargs ``kb_helpers.enqueue_reembedding`` queues the job with,
    while the request that triggers it acts for an allowed administrator."""
    from app.llm.provider_access import acting_for
    from app.services import kb_helpers

    queued = []

    class _Queue:
        async def enqueue(self, job):
            queued.append(job)
            return job

    monkeypatch.setattr("app.worker.queue", _Queue())
    monkeypatch.setattr(
        kb_helpers, "_resolve_embedding_enqueue_state", AsyncMock(return_value=(True, None))
    )
    with acting_for(uuid.uuid4()):
        await kb_helpers.enqueue_reembedding([doc_id])
    assert len(queued) == 1
    return dict(queued[0].kwargs)


@pytest.mark.asyncio
async def test_a_reembed_in_a_company_managed_kb_refuses_a_document_its_person_is_excluded_from(
    monkeypatch,
):
    """A company-managed knowledge base has no owner: a reprocess, which binds
    no one, used to embed for no one and send a refused person's document to
    the provider that excludes them. The job now checks the person recorded
    on the document."""
    from app.services.kb_document_errors import EMBEDDING_NOT_AVAILABLE_MESSAGE

    excluded = uuid.uuid4()
    doc, kb = _company_managed_doc(excluded)
    session = _FakeSession(doc=doc, kb=kb)
    get_embedding_provider, provider = _embedding_lookup({excluded})
    _patch_job(monkeypatch, session, get_embedding_provider)

    kwargs = await _reembed_job_kwargs(monkeypatch, str(doc.id))
    assert kwargs == {"document_id": str(doc.id)}
    result = await document_processor._process_document_impl(**kwargs)

    assert result == 0
    get_embedding_provider.assert_awaited_once_with(session, user_id=excluded)
    provider.embed.assert_not_awaited()
    assert doc.status == "error"
    assert doc.error_message == EMBEDDING_NOT_AVAILABLE_MESSAGE
    assert doc.embedding_next_retry_at is None
    assert doc.added_by_user_id == excluded
    assert session.added == []


@pytest.mark.asyncio
async def test_a_reembed_in_a_company_managed_kb_still_embeds_an_allowed_persons_document(
    monkeypatch,
):
    allowed = uuid.uuid4()
    doc, kb = _company_managed_doc(allowed)
    session = _FakeSession(doc=doc, kb=kb)
    get_embedding_provider, provider = _embedding_lookup({uuid.uuid4()})
    _patch_job(monkeypatch, session, get_embedding_provider)

    kwargs = await _reembed_job_kwargs(monkeypatch, str(doc.id))
    await document_processor._process_document_impl(**kwargs)

    get_embedding_provider.assert_awaited_once_with(session, user_id=allowed)
    provider.embed.assert_awaited_once_with(
        "This chunk has enough content to be embedded.", model="embed-model"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded_one", ["editor", "owner"])
async def test_a_reembed_refuses_a_document_when_its_person_or_its_owner_is_excluded(
    monkeypatch, excluded_one
):
    """In an owned knowledge base too, a document a shared editor was refused
    for is not re-embedded for its allowed owner, nor the other way round."""
    from app.services.kb_document_errors import EMBEDDING_NOT_AVAILABLE_MESSAGE

    editor = uuid.uuid4()
    doc, kb = _build_doc_and_kb()
    doc.added_by_user_id = editor
    session = _FakeSession(doc=doc, kb=kb)
    excluded = editor if excluded_one == "editor" else kb.user_id
    get_embedding_provider, provider = _embedding_lookup({excluded})
    _patch_job(monkeypatch, session, get_embedding_provider)

    await document_processor._process_document_impl(str(doc.id))

    checked = [c.kwargs["user_id"] for c in get_embedding_provider.await_args_list]
    assert checked == ([editor] if excluded_one == "editor" else [editor, kb.user_id])
    provider.embed.assert_not_awaited()
    assert doc.error_message == EMBEDDING_NOT_AVAILABLE_MESSAGE


@pytest.mark.asyncio
async def test_a_job_that_names_a_person_records_them_and_checks_only_them(monkeypatch):
    previous, author = uuid.uuid4(), uuid.uuid4()
    doc, kb = _build_doc_and_kb()
    doc.added_by_user_id = previous
    session = _FakeSession(doc=doc, kb=kb)
    get_embedding_provider, provider = _embedding_lookup({previous, kb.user_id})
    _patch_job(monkeypatch, session, get_embedding_provider)

    await document_processor._process_document_impl(str(doc.id), on_behalf_of=str(author))

    get_embedding_provider.assert_awaited_once_with(session, user_id=str(author))
    assert provider.embed.await_count == 1
    assert doc.added_by_user_id == author


_PERSON = str(uuid.uuid4())
_OWNER = uuid.uuid4()


@pytest.mark.parametrize(
    ("on_behalf_of", "added_by", "kb_owner", "expected"),
    [
        (_PERSON, uuid.uuid4(), _OWNER, [_PERSON]),
        (None, uuid.UUID(_PERSON), _OWNER, [uuid.UUID(_PERSON), _OWNER]),
        (None, _OWNER, _OWNER, [_OWNER]),
        (None, str(_OWNER), _OWNER, [str(_OWNER)]),
        (None, uuid.UUID(_PERSON), None, [uuid.UUID(_PERSON)]),
        (None, None, _OWNER, [_OWNER]),
        (None, None, None, [None]),
        ("", None, None, [None]),
        (None, "not-a-uuid", None, [None]),
    ],
    ids=[
        "named-person-only",
        "recorded-person-and-owner",
        "same-person-once",
        "same-person-once-whatever-its-type",
        "company-managed-recorded-person",
        "no-recorded-person-owner",
        "no-one-known",
        "blank-person-is-no-one",
        "invalid-recorded-person-is-no-one",
    ],
)
def test_whom_a_document_job_embeds_for(on_behalf_of, added_by, kb_owner, expected):
    assert document_processor._embedding_people(on_behalf_of, added_by, kb_owner) == expected
