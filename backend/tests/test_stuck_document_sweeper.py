"""Tests for ``kb_helpers.recover_stuck_documents``.

The sweeper is the second-line recovery layer for KB document
embeddings: if the SAQ enqueue itself was lost (Redis blip, worker
crash between dequeue and the ``processing`` flip), or if the worker
that picked up the doc died mid-embedding, this code path re-enqueues
the document so the user-visible "Re-indexing in progress" banner
clears without manual intervention.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from app.models.knowledge_base import KnowledgeDocument
from app.services import kb_helpers


def _compile_statement(statement) -> str:
    """Render a SQLAlchemy statement to PostgreSQL SQL with inline binds."""
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _statement_limit(statement) -> int | None:
    """Extract ``LIMIT n`` from a statement's rendered SQL.

    Uses only the public ``compile()`` API — inspecting SQLAlchemy's
    private ``_limit`` / ``_limit_clause`` attributes would be brittle
    across versions (and can hold a clause element rather than a plain
    ``int``).
    """
    match = re.search(r"\bLIMIT\s+(\d+)\b", _compile_statement(statement), re.I)
    return int(match.group(1)) if match else None


class _FakeSession:
    """Minimal async-session fake that hands out a queue of stuck docs.

    The session also captures the commit calls so the test can assert
    that ``recover_stuck_documents`` commits its status-flip BEFORE
    calling ``enqueue_document_processing`` — otherwise the worker
    could pick the doc up and observe the stale ``processing`` status,
    re-enqueue itself, and loop forever.

    ``execute()`` models the real per-chunk claim semantics: each
    ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT n`` consumes up to ``n``
    docs from the remaining queue and never hands the same doc out
    twice.  On a real database that exclusion comes from the sweeper
    bumping ``updated_at`` above the cutoff before each chunk commit —
    a fake that kept returning the full fixed list would make the
    production claim loop spin forever, masking exactly the
    termination bug the loop's contract forbids.

    ``executed_statements`` retains the SQLAlchemy Core / ORM statements
    that flowed through ``execute()`` so the lock-contract test below
    can compile every claim SELECT to SQL and assert it carries
    ``FOR UPDATE SKIP LOCKED``.
    """

    def __init__(self, docs):
        self._remaining = list(docs)
        self.commits = 0
        self.rollbacks = 0
        self.last_query_returned: list[KnowledgeDocument] = []
        self.executed_statements: list = []

    class _Result:
        def __init__(self, docs):
            self._docs = docs

        def scalars(self):
            return self

        def all(self):
            return list(self._docs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False

    async def execute(self, statement):
        # Capture the statement so lock-contract tests can introspect
        # the SELECT shape, then serve the next chunk of docs honoring
        # the statement's rendered ``LIMIT`` (extracted from compiled
        # SQL via the public API — see ``_statement_limit``; falls
        # back to "everything" for statements without one).
        self.executed_statements.append(statement)
        limit = _statement_limit(statement)
        take = len(self._remaining) if limit is None else max(0, limit)
        chunk = self._remaining[:take]
        self._remaining = self._remaining[take:]
        self.last_query_returned = list(chunk)
        return self._Result(chunk)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        # Track rollback calls so the embed-disabled short-circuit
        # path can be pinned: the sweeper MUST release the
        # ``FOR UPDATE SKIP LOCKED`` row locks before returning,
        # otherwise the autonomous tick's shared ``db`` session
        # would hold those locks across all downstream tick work.
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def _quiet_worker_liveness_probe(monkeypatch):
    """Keep the sweeper tests hermetic: the post-re-enqueue worker
    liveness probe talks to the real SAQ queue (Redis) — stub it out
    here so no test depends on a Redis server.  The probe itself has
    dedicated tests in ``test_document_job_queue.py`` and a sweeper
    integration test below that re-patches it with a recording mock.
    """
    monkeypatch.setattr(
        kb_helpers, "warn_if_no_active_workers", AsyncMock(return_value=False)
    )


def _make_doc(
    *,
    status: str,
    updated_minutes_ago: float,
) -> KnowledgeDocument:
    """Build an in-memory ``KnowledgeDocument`` for sweeper tests.

    We deliberately bypass the SQLAlchemy ``onupdate`` hook by
    constructing through the ORM class — the sweeper reads
    ``updated_at`` straight off the row, so the test only needs the
    column to have a value.
    """
    return KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename=f"doc-{updated_minutes_ago}m.md",
        source_type="website",
        content="x" * 32,
        content_hash="deadbeef",
        status=status,
        chunk_count=0,
        updated_at=datetime.now(UTC) - timedelta(minutes=updated_minutes_ago),
    )


@pytest.mark.asyncio
async def test_recover_stuck_documents_reenqueues_old_pending(monkeypatch):
    """Pending docs older than the threshold MUST be re-enqueued AND have
    ``updated_at`` bumped so the next tick doesn't pick them up again.
    """
    fresh = _make_doc(status="pending", updated_minutes_ago=0.5)
    stuck = _make_doc(status="pending", updated_minutes_ago=10)
    stuck_updated_before = stuck.updated_at

    session = _FakeSession([stuck])  # query already filters by cutoff
    # ``return_value=True`` mirrors the post-fix contract of
    # ``enqueue_document_processing`` (returns ``bool`` — ``True`` on a
    # successful SAQ enqueue, ``False`` on skip / exception).  A bare
    # ``AsyncMock()`` would return ``MagicMock`` (truthy by accident),
    # so the test would pass for the wrong reason — explicit ``True``
    # pins the success branch.
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 1
    # The fresh doc is filtered out by the SELECT — production relies
    # on Postgres for the cutoff filter, not the Python layer.  We
    # only need to confirm the call path queues the doc the session
    # actually returned.
    enqueue_mock.assert_awaited_once_with(str(stuck.id), embedding_ready=True)
    # Pending → pending is a no-op status-wise but ``updated_at`` MUST
    # advance so the next tick gives the freshly re-enqueued job a
    # full ``pending_threshold_seconds`` window before sweeping again.
    # Without this assertion, a regression where pending rows are not
    # dirtied would let the sweeper loop on the same row every tick
    # (the Copilot reviewer's exact finding on the prior revision).
    assert stuck.updated_at > stuck_updated_before
    assert session.commits == 1
    assert fresh.status == "pending"  # untouched (sanity)


@pytest.mark.asyncio
async def test_recover_stuck_documents_resets_processing_to_pending(monkeypatch):
    """Stuck ``processing`` docs MUST be reset to ``pending`` AND re-enqueued.

    Resetting to ``pending`` is what makes the worker's status path
    (pending → processing → ready/error) idempotent across the next
    pickup: if we left the doc in ``processing`` and just re-enqueued,
    the worker would flip it processing → processing (no-op), then
    proceed — that works today but ties the recovery to a specific
    quirk of the processor.  The pending reset is the contract.
    """
    stuck = _make_doc(status="processing", updated_minutes_ago=60)
    session = _FakeSession([stuck])
    # See pending-arm test for why ``return_value=True`` is explicit.
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    stuck_updated_before = stuck.updated_at
    recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 1
    assert stuck.status == "pending"
    assert stuck.error_message is not None
    assert "Worker did not finish embedding" in stuck.error_message
    # ``updated_at`` must advance for the same reason the pending arm
    # advances it — the freshly re-enqueued job gets a full window
    # before the sweeper is allowed to fire on it again.
    assert stuck.updated_at > stuck_updated_before
    enqueue_mock.assert_awaited_once_with(str(stuck.id), embedding_ready=True)


@pytest.mark.asyncio
async def test_recover_stuck_documents_short_circuits_without_embedding(
    monkeypatch, caplog
):
    """When embedding isn't configured the sweeper MUST NOT touch docs.

    Re-enqueueing into a queue whose worker has no embedding provider
    would just bounce the doc back to ``pending`` after the worker's
    own pre-check fails — and ``enqueue_document_processing`` already
    silently no-ops in that case.  Short-circuiting at the sweeper
    layer keeps the logs clean (one INFO per tick instead of N).
    """
    stuck = _make_doc(status="pending", updated_minutes_ago=10)
    session = _FakeSession([stuck])
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(False, "no provider configured")),
    )

    with caplog.at_level("INFO"):
        recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 0
    enqueue_mock.assert_not_awaited()
    # Doc must remain untouched — we deliberately do NOT commit if we
    # short-circuited (the status flip would otherwise dirty the row
    # and refresh updated_at, masking the underlying stuckness).
    assert stuck.status == "pending"
    assert session.commits == 0
    assert "Stuck-document sweep skipped" in caplog.text
    # CRITICAL: the ``FOR UPDATE SKIP LOCKED`` row locks taken by the
    # SELECT above MUST be released before returning.  The autonomous
    # tick reuses this ``db`` session across the full tick (KB sync,
    # memory maintenance, …); leaking the locks would block the
    # embedding worker and other maintenance tasks until the session
    # is finally closed.  Pin the explicit ``rollback()`` here so a
    # future regression that drops the call trips this assertion
    # before the lock-leak surfaces in production as worker stalls.
    assert session.rollbacks == 1, (
        "embed-disabled short-circuit MUST release row locks via "
        "rollback() before returning — otherwise the autonomous "
        "tick's shared session holds the FOR UPDATE SKIP LOCKED "
        "locks across all downstream tick work, blocking the "
        "embedding worker and other maintenance tasks"
    )


@pytest.mark.asyncio
async def test_recover_stuck_documents_returns_zero_when_nothing_stuck(monkeypatch):
    """The fast path: empty SELECT → no commit, no enqueue, return 0."""
    session = _FakeSession([])
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    # Embedding resolver should NOT be hit when the batch is empty —
    # asserting that prevents the implementation from accidentally
    # making a DB round-trip to the LLM provider config table on
    # every quiet tick.
    resolver = AsyncMock()
    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", resolver)

    recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 0
    enqueue_mock.assert_not_awaited()
    resolver.assert_not_awaited()
    assert session.commits == 0


@pytest.mark.asyncio
async def test_recover_stuck_documents_counts_actual_enqueues_not_attempts(
    monkeypatch, caplog
):
    """The sweeper used to count every ``enqueue_document_processing``
    *call* as a success — first by ignoring the return value entirely
    (overstated recovery, hid Redis outages from dashboards), and then
    by checking ``if enqueue_result is None`` (which classified *every*
    attempt as a failure because the helper returned ``None`` even on
    success).

    With the helper now returning ``bool``, the sweeper MUST count
    only the calls that actually queued a SAQ job.  The mix below
    (3 stuck docs, helper returns ``True`` / ``False`` / ``True``)
    asserts the contract end-to-end:

    * ``recovered == 2`` (only the two truthy returns count)
    * the failing doc emits a sweep-context WARNING so on-call can
      tell sweep losses apart from upstream sync-enqueue losses
    * the INFO summary line reports ``2 of 3`` so dashboards correctly
      surface partial recovery rather than a misleading round number
    """
    docs = [
        _make_doc(status="pending", updated_minutes_ago=10),
        _make_doc(status="pending", updated_minutes_ago=12),
        _make_doc(status="pending", updated_minutes_ago=15),
    ]
    session = _FakeSession(docs)
    # First and third calls succeed; second fails (e.g. Redis blip).
    enqueue_mock = AsyncMock(side_effect=[True, False, True])
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    with caplog.at_level("INFO"):
        recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 2, (
        "sweeper must count only successful enqueues; the prior "
        "code-paths either counted all attempts (overstated) or "
        "classified every call as failed (understated to 0), until the "
        "helper returned a bool"
    )
    assert enqueue_mock.await_count == 3, (
        "every stuck doc must be attempted, even after one failure — "
        "a single Redis hiccup must not abort the rest of the batch"
    )
    assert "re-enqueued 2 of 3 document(s)" in caplog.text
    assert (
        "Stuck-document sweeper failed to re-enqueue document" in caplog.text
    ), "the failing doc must emit a sweep-context WARNING"


@pytest.mark.asyncio
async def test_recover_stuck_documents_commits_updates_in_chunks_before_enqueue(
    monkeypatch,
):
    """The sweeper must claim + commit status updates in smaller chunks.

    This guards against the production timeout where a single large
    ORM executemany UPDATE for the full 200-doc batch hit Postgres
    statement_timeout.  We force a tiny chunk size and assert
    each enqueue sees the expected commit count progression.

    The chunking contract has a second, subtler half: each chunk MUST
    be claimed by its own ``SELECT ... FOR UPDATE SKIP LOCKED LIMIT
    <chunk>`` rather than slicing one big upfront SELECT.  With a
    single upfront SELECT, the first chunk's ``commit()`` releases the
    row locks of EVERY selected row — including rows in chunks not yet
    updated — so the embedding worker could finish one of those rows
    (``status = 'ready'``) and the sweeper's stale ORM instance would
    then overwrite it back to ``pending``, re-introducing the exact
    race ``test_recover_stuck_documents_locks_rows_with_for_update_
    skip_locked`` pins.  The per-statement assertions below trip on a
    regression back to the slice-one-big-SELECT shape.
    """
    docs = [
        _make_doc(status="pending", updated_minutes_ago=10 + i)
        for i in range(5)
    ]
    session = _FakeSession(docs)
    monkeypatch.setattr(kb_helpers, "_STUCK_UPDATE_COMMIT_CHUNK_SIZE", 2)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )
    commit_counts_seen_by_enqueue: list[int] = []

    async def _enqueue_with_commit_snapshot(_doc_id: str, *, embedding_ready: bool):
        assert embedding_ready is True
        commit_counts_seen_by_enqueue.append(session.commits)
        return True

    monkeypatch.setattr(
        kb_helpers,
        "enqueue_document_processing",
        AsyncMock(side_effect=_enqueue_with_commit_snapshot),
    )

    recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 5
    # 5 docs / chunk 2 → chunks of 2, 2, 1 → one commit per chunk.
    assert session.commits == 3
    # Every enqueue must observe its own chunk already committed:
    # docs 1-2 after commit #1, docs 3-4 after commit #2, doc 5 after
    # commit #3.
    assert commit_counts_seen_by_enqueue == [1, 1, 2, 2, 3]

    # Per-chunk claim contract: one SELECT per chunk (the final short
    # read of 1 < 2 signals the drain, so no trailing empty SELECT),
    # each capped at the chunk size and each carrying FOR UPDATE SKIP
    # LOCKED so a commit never releases a lock on a row the sweeper
    # has not updated yet.
    assert len(session.executed_statements) == 3, (
        "each chunk must be claimed by its own SELECT — slicing one "
        "upfront SELECT reintroduces the unlock-then-overwrite race; "
        f"got {len(session.executed_statements)} statement(s)"
    )
    for statement in session.executed_statements:
        compiled = _compile_statement(statement).upper()
        # Assert on the rendered SQL (not SQLAlchemy internals) so the
        # test stays stable across library versions.
        assert re.search(r"\bLIMIT 2\b", compiled), (
            "every chunk claim must be capped at the chunk size — an "
            "uncapped SELECT recreates the oversized executemany the "
            f"chunking exists to prevent; got: {compiled}"
        )
        assert "FOR UPDATE" in compiled and "SKIP LOCKED" in compiled, (
            "every chunk claim must re-acquire FOR UPDATE SKIP LOCKED "
            f"row locks; got: {compiled}"
        )


@pytest.mark.asyncio
async def test_recover_stuck_documents_locks_rows_with_for_update_skip_locked(
    monkeypatch,
):
    """The sweeper SELECT MUST carry ``FOR UPDATE SKIP LOCKED``.

    Without it, the sweep races the embedding worker
    (``document_processor._process_document_impl``, which itself uses
    ``FOR UPDATE SKIP LOCKED`` on line ~240): the worker can finish
    between our SELECT and our commit, write ``status = 'ready'``,
    then our stale ORM instance commits ``status = 'pending'`` over
    it.  The doc gets re-enqueued (idempotent embedding so no actual
    data loss), but the user sees a ``ready → pending → ready`` flap,
    the misleading ``"Worker did not finish embedding…"`` error_message
    is written to a doc whose worker did finish, and we burn duplicate
    API tokens.

    The ``SKIP LOCKED`` clause is the correctness contract: rows
    currently held by a worker are silently excluded from this batch;
    rows whose worker already committed are excluded by the WHERE
    (``status NOT IN (pending, processing)``); rows whose worker died
    mid-flight (lock released by Postgres on disconnect) flow through
    cleanly — exactly the recovery this sweeper exists for.
    """
    stuck = _make_doc(status="processing", updated_minutes_ago=60)
    session = _FakeSession([stuck])
    monkeypatch.setattr(
        kb_helpers,
        "enqueue_document_processing",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    await kb_helpers.recover_stuck_documents(session)

    # A single stuck doc fits in the first chunk (short read < chunk
    # size drains the sweep), so exactly one claim SELECT is issued.
    # Compile its rendered SQL and assert the FOR UPDATE SKIP LOCKED
    # clause is present.  The chunked-commit test above extends this
    # contract to every per-chunk claim.
    assert len(session.executed_statements) == 1, (
        f"sweeper should issue exactly one claim SELECT for a "
        f"single-doc drain; got {len(session.executed_statements)}"
    )
    statement = session.executed_statements[0]
    # The PostgreSQL dialect renders ``with_for_update(skip_locked=True)``
    # as ``FOR UPDATE SKIP LOCKED``.
    compiled = _compile_statement(statement).upper()
    # Two separate clauses asserted independently so a regression that
    # only drops one (e.g. ``with_for_update()`` without ``skip_locked``)
    # surfaces with a precise failure.  ruff S608 false-positives on
    # f-strings that quote SQL keywords; assert with simple containment
    # checks + plain-string messages instead.
    missing_for_update = "sweeper SELECT must include the row-lock clause"
    assert "FOR UPDATE" in compiled, missing_for_update + ": " + compiled
    missing_skip_locked = "sweeper SELECT must include the skip-locked modifier"
    assert "SKIP LOCKED" in compiled, missing_skip_locked + ": " + compiled


@pytest.mark.asyncio
async def test_recover_stuck_documents_does_not_repeat_precheck_when_batch_precheck_fails(
    monkeypatch, caplog
):
    """The sweeper must NOT cause N per-doc pre-check re-runs when the
    batch-level pre-check returns ``(None, reason)``.

    The bug was: ``recover_stuck_documents`` forwarded ``state`` to
    each ``enqueue_document_processing`` call, but the batch pre-check
    can return ``(None, reason)`` (the resolve itself failed
    unexpectedly).  The per-doc function would then re-enter its
    ``if embedding_ready is None`` branch, re-run the full pre-check
    (DB lookup + provider build) and re-emit the WARNING for every
    document — turning a single resolver failure into ``batch_size``
    DB lookups and ``batch_size`` identical log lines per autonomous
    tick.  Mirrors the exact failure mode the round-N fix to
    ``enqueue_documents`` resolved at that sibling call site
    (``test_enqueue_documents_does_not_repeat_precheck_when_batch_precheck_fails``
    in ``test_document_job_queue.py`` covers it there).

    Pinned by:

    * Asserting ``_resolve_embedding_enqueue_state`` is called exactly
      once across the whole sweep (the batch-level probe, plus zero
      per-doc re-runs).
    * Asserting every doc still gets enqueued (proceed best-effort).
    * Asserting every per-doc call passes the explicit
      ``embedding_ready=True`` short-circuit (NEVER forwards the raw
      ``state`` value).
    * Asserting exactly one batch-level WARNING fires, with zero
      per-doc WARNINGs.

    A regression that re-introduces ``embedding_ready=state`` in the
    sweeper trips the resolver call-count assertion AND the per-doc
    kwarg assertion before the log-count check fires.
    """
    docs = [
        _make_doc(status="pending", updated_minutes_ago=10),
        _make_doc(status="pending", updated_minutes_ago=12),
        _make_doc(status="pending", updated_minutes_ago=15),
    ]
    session = _FakeSession(docs)
    # ``True`` for the per-doc helper so the success branch is the
    # one being asserted on (the helper is monkey-patched, so the
    # internal ``if embedding_ready is None`` branch never re-runs the
    # real probe — but the kwarg recorded by ``await_args_list`` still
    # surfaces the regression cleanly).
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    # The batch resolver returns ``(None, reason)`` — the "pre-check
    # itself failed unexpectedly" branch.  This is the only state in
    # which the bug exercises: ``True`` would not trigger a per-doc
    # re-check anyway, and ``False`` short-circuits the sweep before
    # the loop.
    resolver = AsyncMock(return_value=(None, "provider config probe failed"))
    monkeypatch.setattr(
        kb_helpers, "_resolve_embedding_enqueue_state", resolver
    )

    with caplog.at_level("WARNING"):
        recovered = await kb_helpers.recover_stuck_documents(session)

    # Best-effort: every doc still goes through enqueue even though
    # the batch probe was inconclusive.
    assert recovered == 3
    assert enqueue_mock.await_count == 3, (
        "every stuck doc must be attempted even when the batch "
        "pre-check is inconclusive — the batch warning is the "
        "operator signal, not a hard stop"
    )

    # CRITICAL: every per-doc call passes ``embedding_ready=True``,
    # NEVER the raw ``state`` (which would be ``None`` here).  A
    # regression to ``embedding_ready=state`` trips this assertion
    # before the resolver call-count check fires.
    for call in enqueue_mock.await_args_list:
        assert call.kwargs.get("embedding_ready") is True, (
            "sweeper must pass ``embedding_ready=True`` regardless of "
            "whether the batch pre-check returned True or None — "
            "forwarding None re-triggers the per-doc resolver storm; "
            f"got kwargs={call.kwargs}"
        )

    # The resolver is invoked EXACTLY ONCE for the batch.  The
    # ``enqueue_document_processing`` mock above replaces the real
    # helper, so its internal ``if embedding_ready is None`` branch
    # can't re-call the resolver anyway — but ``call_count == 1``
    # is the strict structural assertion that catches a regression
    # where the sweeper itself starts probing per-doc (e.g. someone
    # moves the probe inside the loop "for accuracy").
    assert resolver.await_count == 1, (
        "the embedding resolver must be probed once per sweep, never "
        "per document — forwarding ``state`` as ``embedding_ready`` "
        "would multiply the probe N times via the helper's own "
        "``if embedding_ready is None`` branch"
    )

    # Exactly one batch-level WARNING fires, no per-doc duplicates.
    # The sweeper now mirrors ``enqueue_documents`` and emits a single
    # batch WARNING for the ``state is None`` case so the operator has
    # a signal that the probe failed (previously the resolver
    # exception was silent because the sweeper short-circuited only on
    # ``state is False`` and forwarded ``embedding_ready=True``
    # silently for ``state is None``).
    batch_warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING"
        and "Stuck-document sweep batch pre-check failed" in rec.getMessage()
    ]
    assert len(batch_warnings) == 1, (
        "sweeper MUST emit exactly one batch WARNING when the "
        "embedding resolver returns ``(None, reason)`` — without it "
        "the resolver exception is silent while the sweep proceeds; "
        f"got {len(batch_warnings)} batch WARNING(s): {batch_warnings}"
    )
    # The reason text from the resolver MUST reach the operator via
    # the warning so the failure is diagnosable.
    assert "provider config probe failed" in batch_warnings[0].getMessage(), (
        f"batch WARNING must surface the underlying resolver reason "
        f"so operators can diagnose; got: {batch_warnings[0].getMessage()}"
    )

    # The matcher below catches the message shape
    # ``enqueue_document_processing`` emits inside its
    # ``if embedding_ready is None`` branch — those must be ZERO
    # because the sweeper forwards ``embedding_ready=True`` so the
    # helper short-circuits its own per-doc probe.
    per_doc_warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING"
        and "Embedding configuration pre-check failed for" in rec.getMessage()
    ]
    assert per_doc_warnings == [], (
        "no per-doc WARNINGs about the pre-check should fire — the "
        "sweeper proceeds best-effort with embedding_ready=True so "
        f"the helper short-circuits the recheck; got: {per_doc_warnings}"
    )


@pytest.mark.asyncio
async def test_recover_stuck_documents_warns_on_resolver_failure(
    monkeypatch, caplog
):
    """Dedicated regression: when ``_resolve_embedding_enqueue_state``
    returns ``(None, reason)`` (the resolver itself raised
    unexpectedly), the sweeper MUST emit a single batch-level
    WARNING with the underlying reason BEFORE proceeding best-effort.

    Without this WARNING, the resolver exception was completely
    silent while the sweep proceeded — operators had no signal that
    the probe failed and could not correlate spurious behaviour with
    the underlying provider-config failure.  Mirrors the existing
    behaviour of the sibling ``enqueue_documents`` call site for
    symmetry.

    A regression that drops the WARNING (e.g. someone refactors the
    ``if state is None`` branch into the per-doc loop) trips this
    test on the structural assertion before any side-effect surfaces.
    """
    docs = [_make_doc(status="pending", updated_minutes_ago=10)]
    session = _FakeSession(docs)
    monkeypatch.setattr(
        kb_helpers,
        "enqueue_document_processing",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(None, "provider config probe blew up")),
    )

    with caplog.at_level("WARNING"):
        recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 1, (
        "sweeper MUST still proceed best-effort on resolver failure — "
        "the WARNING is the signal, not a hard stop"
    )

    matches = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING"
        and "Stuck-document sweep batch pre-check failed" in rec.getMessage()
    ]
    assert len(matches) == 1, (
        f"sweeper MUST emit exactly one batch WARNING when the "
        f"resolver returns (None, reason); got {len(matches)}: "
        f"{[m.getMessage() for m in matches]}"
    )
    msg = matches[0].getMessage()
    assert "provider config probe blew up" in msg, (
        f"batch WARNING must include the resolver's reason string so "
        f"operators can identify the root cause without grepping for "
        f"upstream tracebacks; got: {msg}"
    )
    # The doc count must also be in the message — operators need to
    # know the scale of the impact at a glance.
    assert "1 document" in msg, (
        f"batch WARNING must include the document count so the scale "
        f"of the batch-pre-check failure is visible at a glance; got: "
        f"{msg}"
    )


@pytest.mark.asyncio
async def test_recover_stuck_documents_probes_worker_liveness_after_reenqueue(
    monkeypatch,
):
    """After actually re-enqueueing stuck docs, the sweeper MUST probe
    whether any SAQ worker is alive.

    Stuck documents on every tick + jobs accepted by Redis + still no
    progress is the signature of a dead or silently-stalled worker
    (e.g. hanging forever on a dead BLMOVE socket).  Without the probe
    the sweep churns silently and nothing in any log explains why the
    "Re-indexing in progress" banner never clears.
    """
    stuck = _make_doc(status="pending", updated_minutes_ago=10)
    session = _FakeSession([stuck])
    monkeypatch.setattr(
        kb_helpers, "enqueue_document_processing", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(kb_helpers, "warn_if_no_active_workers", probe)

    recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 1
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_recover_stuck_documents_skips_liveness_probe_when_nothing_reenqueued(
    monkeypatch,
):
    """No successful re-enqueues -> no probe (e.g. every enqueue failed:
    Redis is the problem and already screams elsewhere)."""
    stuck = _make_doc(status="pending", updated_minutes_ago=10)
    session = _FakeSession([stuck])
    monkeypatch.setattr(
        kb_helpers, "enqueue_document_processing", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )
    probe = AsyncMock(return_value=False)
    monkeypatch.setattr(kb_helpers, "warn_if_no_active_workers", probe)

    recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 0
    probe.assert_not_awaited()


# ---------------------------------------------------------------------------
# InvalidCachedStatementError retry: a DDL migration during a rolling
# deploy invalidates asyncpg's prepared-statement cache, and the sweep on the
# autonomous tick right after the deploy is a frequent first victim.
# ---------------------------------------------------------------------------


class _FakeInvalidCachedError(Exception):
    """Stand-in for ``asyncpg.exceptions.InvalidCachedStatementError`` — the
    detector matches by class name, so the name here is what matters."""


# Mimic asyncpg's class name exactly so the name-based branch is exercised.
_FakeInvalidCachedError.__name__ = "InvalidCachedStatementError"


def _make_cache_invalidation_dbapi_error() -> DBAPIError:
    return DBAPIError(
        "SELECT ... FOR UPDATE SKIP LOCKED",
        None,
        _FakeInvalidCachedError(
            "cached statement plan is invalid due to a database schema "
            "or configuration change"
        ),
    )


def test_is_invalid_cached_statement_error_detects_wrapped_orig():
    assert kb_helpers._is_invalid_cached_statement_error(
        _make_cache_invalidation_dbapi_error()
    )


def test_is_invalid_cached_statement_error_detects_by_message():
    exc = RuntimeError(
        "cached statement plan is invalid due to a database schema change"
    )
    assert kb_helpers._is_invalid_cached_statement_error(exc)


def test_is_invalid_cached_statement_error_rejects_unrelated():
    assert not kb_helpers._is_invalid_cached_statement_error(
        RuntimeError("some other failure")
    )
    assert not kb_helpers._is_invalid_cached_statement_error(
        DBAPIError("SELECT 1", None, RuntimeError("connection reset"))
    )


class _CacheInvalidationSession(_FakeSession):
    """Raises ``InvalidCachedStatementError`` on the first ``execute`` then
    succeeds — simulating asyncpg's cache being invalidated by a DDL change."""

    def __init__(self, docs, *, fail_times: int = 1):
        super().__init__(docs)
        self._fail_times = fail_times
        self.execute_calls = 0

    async def execute(self, statement):
        self.execute_calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise _make_cache_invalidation_dbapi_error()
        return await super().execute(statement)


@pytest.mark.asyncio
async def test_recover_stuck_documents_retries_on_cache_invalidation(monkeypatch):
    """The sweep MUST roll back and retry once when the first SELECT hits
    ``InvalidCachedStatementError``, then succeed — not crash the tick."""
    stuck = _make_doc(status="pending", updated_minutes_ago=10)
    session = _CacheInvalidationSession([stuck], fail_times=1)
    monkeypatch.setattr(
        kb_helpers, "enqueue_document_processing", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    recovered = await kb_helpers.recover_stuck_documents(session)

    assert recovered == 1
    # First SELECT raised, retry succeeded → exactly 2 execute attempts.
    assert session.execute_calls == 2
    # The failed statement aborts the transaction, so the helper must roll
    # back before retrying (otherwise the retry runs in an aborted txn).
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_recover_stuck_documents_does_not_retry_unrelated_dbapi_error(
    monkeypatch,
):
    """A non-cache DBAPIError MUST propagate immediately — no rollback/retry
    loop that could mask a genuine failure."""

    class _BoomSession(_FakeSession):
        def __init__(self, docs):
            super().__init__(docs)
            self.execute_calls = 0

        async def execute(self, statement):
            self.execute_calls += 1
            raise DBAPIError("SELECT 1", None, RuntimeError("connection reset"))

    session = _BoomSession([])
    monkeypatch.setattr(
        kb_helpers, "enqueue_document_processing", AsyncMock(return_value=True)
    )

    with pytest.raises(DBAPIError):
        await kb_helpers.recover_stuck_documents(session)

    assert session.execute_calls == 1  # no retry
    assert session.rollbacks == 0


def test_processing_threshold_seconds_tracks_worker_job_timeout(monkeypatch):
    """The default ``processing``-arm threshold MUST track the configured
    ``worker_document_job_timeout`` plus a safety margin — hard-coding
    it (the prior 30 min literal) would let an operator that bumps the
    job timeout above the literal trigger a race where the sweeper
    yanks a legitimately-running embedding job before SAQ ever times
    it out.
    """
    fake_settings = type("S", (), {"worker_document_job_timeout": 2400})()
    monkeypatch.setattr(kb_helpers, "app_settings", fake_settings)

    threshold = kb_helpers._processing_threshold_seconds()

    assert threshold == 2400 + kb_helpers.STUCK_PROCESSING_THRESHOLD_MARGIN_SECONDS
    assert threshold > 2400, "threshold must include a positive margin"


@pytest.mark.asyncio
async def test_the_sweeper_claims_only_pending_and_processing_documents(monkeypatch):
    """A document refused for an excluded person is ``error``:
    the sweeper, whose re-enqueue names no one, must never claim it. Read off
    the claim's WHERE clause, not its rendered text."""
    from sqlalchemy.sql import operators, visitors

    session = _FakeSession([_make_doc(status="pending", updated_minutes_ago=60)])
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", AsyncMock(return_value=True))
    monkeypatch.setattr(
        kb_helpers, "_resolve_embedding_enqueue_state", AsyncMock(return_value=(True, None))
    )

    await kb_helpers.recover_stuck_documents(session)

    assert len(session.executed_statements) == 1
    claimed = sorted(
        node.right.value
        for node in visitors.iterate(session.executed_statements[0].whereclause)
        if getattr(node, "operator", None) is operators.eq
        and getattr(node.left, "key", None) == "status"
    )
    assert claimed == ["pending", "processing"]
