import asyncio
import contextlib
import sys
import types
from types import SimpleNamespace

import pytest

from app import worker as worker_module
from app.services import document_processor, kb_helpers


@pytest.fixture(autouse=True)
def _ai_enabled(monkeypatch):
    """AI on, without touching the database.

    The jobs now refuse when AI is disabled (FR-007), and resolving that
    opens a session — which in these tests reaches the real engine and leaves a
    connection to be closed after the test's event loop has gone. This suite is
    about concurrency and retry semantics, so the gate is stubbed rather than
    satisfied.
    """

    async def _never_skip(_job: str, _subject: str) -> bool:
        return False

    monkeypatch.setattr(worker_module, "_skip_because_ai_is_disabled", _never_skip)


class _FakeQueue:
    """Mirrors SAQ's success contract: ``enqueue`` returns the Job.

    Returning ``None`` would exercise the dedup branch ("a job with
    this key is already queued"), which has its own dedicated test.
    """

    def __init__(self):
        self.jobs = []

    async def enqueue(self, job):
        self.jobs.append(job)
        return job


class _FakeKeyedQueue:
    """SAQ's lookup by job key: one entry per key asked, ``None`` for an unknown key."""

    def __init__(self, jobs):
        self.by_key = {job.key: job for job in jobs}
        self.lookups = []
        self.aborted = []

    async def jobs(self, keys):
        keys = list(keys)
        self.lookups.append(keys)
        return [self.by_key.get(key) for key in keys]

    async def abort(self, job, error, ttl=5):
        self.aborted.append((job, error, ttl))


@pytest.mark.asyncio
async def test_enqueue_document_processing_uses_retry_and_timeout_settings(monkeypatch):
    fake_queue = _FakeQueue()
    monkeypatch.setattr(worker_module, "queue", fake_queue)
    monkeypatch.setattr(kb_helpers.app_settings, "worker_document_job_timeout", 1800)
    monkeypatch.setattr(kb_helpers.app_settings, "worker_document_job_retries", 3)
    monkeypatch.setattr(
        kb_helpers.app_settings, "worker_document_job_retry_delay_seconds", 10.0
    )
    monkeypatch.setattr(kb_helpers.app_settings, "worker_document_job_retry_backoff", True)

    await kb_helpers.enqueue_document_processing(
        "0414d0ca-5a20-45a4-b7e4-e0b69837db56",
        embedding_ready=True,
    )

    assert len(fake_queue.jobs) == 1
    job = fake_queue.jobs[0]
    assert job.function == "process_document_job"
    assert job.kwargs == {"document_id": "0414d0ca-5a20-45a4-b7e4-e0b69837db56"}
    # Stable per-document key — SAQ dedupes enqueues whose key is already
    # queued/active, so sweeper re-enqueues can't pile up duplicate jobs.
    assert job.key == "process-document-0414d0ca-5a20-45a4-b7e4-e0b69837db56"
    assert job.timeout == 1800
    assert job.retries == 3
    assert job.retry_delay == 10.0
    assert job.retry_backoff is True


@pytest.mark.asyncio
async def test_enqueue_document_processing_treats_dedup_as_success(
    monkeypatch, caplog
):
    """SAQ returns ``None`` from ``enqueue`` when a job with the same key
    is already queued / active / scheduled for retry.

    With stable per-document keys that is the EXPECTED outcome for
    sweeper re-enqueues of a document whose original job is still in
    the queue (e.g. worker saturated or briefly stalled).  The helper
    must treat it as success — the document IS going to be processed —
    and log the dedup at INFO so operators can correlate.
    """
    import logging

    class _DedupQueue:
        async def enqueue(self, job):
            return None  # SAQ's "duplicate key, not enqueued" signal

    monkeypatch.setattr(worker_module, "queue", _DedupQueue())

    caplog.set_level(logging.INFO, logger=kb_helpers.logger.name)
    result = await kb_helpers.enqueue_document_processing(
        "0414d0ca-5a20-45a4-b7e4-e0b69837db56",
        embedding_ready=True,
    )

    assert result is True, (
        "a deduplicated enqueue means the document already has a live "
        "job — that is success, not failure (returning False would make "
        "the sweepers log spurious re-enqueue failures every tick)"
    )
    assert any(
        "already has a queued/active embedding job" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_enqueue_document_processing_bounds_a_hanging_enqueue(monkeypatch):
    """A silently-dead Redis socket must NOT hang the caller forever.

    The enqueue runs on the web process (upload endpoints, KB sync, the
    autonomous sweepers); without the ``asyncio.wait_for`` bound a dead
    connection would park the coroutine indefinitely and freeze the
    whole autonomous tick.  The bound converts that into the normal
    "enqueue failed" path (returns ``False``; the stuck-document
    sweeper retries later).
    """

    class _HangingQueue:
        async def enqueue(self, job):
            await asyncio.sleep(3600)

    monkeypatch.setattr(worker_module, "queue", _HangingQueue())
    monkeypatch.setattr(kb_helpers, "_ENQUEUE_TIMEOUT_SECONDS", 0.05)

    result = await kb_helpers.enqueue_document_processing(
        "0414d0ca-5a20-45a4-b7e4-e0b69837db56",
        embedding_ready=True,
    )

    assert result is False


@pytest.mark.asyncio
async def test_the_workset_pass_enqueue_is_bounded_like_the_embedding_one(monkeypatch):
    """The workset pass is queued from the ingest request too (a borrowed
    document, a metadata re-add): a dead Redis socket must become "could not
    queue", not a ``workset_add`` that never returns."""
    import uuid

    from app.services import workset_processing

    class _HangingQueue:
        async def enqueue(self, job):
            await asyncio.sleep(3600)

    monkeypatch.setattr(worker_module, "queue", _HangingQueue())
    monkeypatch.setattr(kb_helpers, "_ENQUEUE_TIMEOUT_SECONDS", 0.05)

    scheduled = await workset_processing._schedule_pass(
        uuid.uuid4(), key="workset-auto-pass-test", delay=0
    )

    assert scheduled is False


@pytest.mark.asyncio
async def test_the_clustering_enqueue_is_bounded_like_the_embedding_one(monkeypatch):
    """Clustering is queued from requests too (a seal, an ingest that settles a
    sealed corpus): a dead Redis socket must become "could not queue", not a
    request that never returns."""
    import uuid

    from app.services import workset_clustering

    class _HangingQueue:
        async def enqueue(self, job):
            await asyncio.sleep(3600)

    monkeypatch.setattr(worker_module, "queue", _HangingQueue())
    monkeypatch.setattr(kb_helpers, "_ENQUEUE_TIMEOUT_SECONDS", 0.05)

    queued = await asyncio.wait_for(
        workset_clustering.enqueue_clustering(uuid.uuid4()), 5
    )
    assert queued is False


@pytest.mark.asyncio
async def test_warn_if_no_active_workers_fires_when_queue_has_no_workers(
    monkeypatch, caplog
):
    """No registered SAQ worker + work being enqueued = loud ERROR.

    This is the observability fix for the silent-stall failure mode: a
    worker hanging on a dead Redis connection stops refreshing its
    ``worker_info`` heartbeat (60s TTL) and disappears from
    ``queue.info()`` — previously nothing in any log explained why the
    sweeper churned forever.
    """
    import logging

    class _NoWorkerQueue:
        async def info(self):
            return {"workers": {}, "queued": 262, "active": 0}

    monkeypatch.setattr(worker_module, "queue", _NoWorkerQueue())

    caplog.set_level(logging.ERROR, logger=kb_helpers.logger.name)
    fired = await kb_helpers.warn_if_no_active_workers()

    assert fired is True
    assert any(
        "NO active SAQ worker" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_warn_if_no_active_workers_quiet_when_worker_alive(
    monkeypatch, caplog
):
    import logging

    class _HealthyQueue:
        async def info(self):
            return {"workers": {"worker-1": {"queue_key": "default"}}}

    monkeypatch.setattr(worker_module, "queue", _HealthyQueue())

    caplog.set_level(logging.ERROR, logger=kb_helpers.logger.name)
    fired = await kb_helpers.warn_if_no_active_workers()

    assert fired is False
    assert not caplog.records


@pytest.mark.asyncio
async def test_warn_if_no_active_workers_swallows_probe_failures(monkeypatch):
    """Redis being down must not crash the sweeper via the probe."""

    class _BrokenQueue:
        async def info(self):
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(worker_module, "queue", _BrokenQueue())

    assert await kb_helpers.warn_if_no_active_workers() is False


@pytest.mark.asyncio
async def test_process_document_job_limits_document_concurrency(monkeypatch):
    active_jobs = 0
    max_active_jobs = 0

    async def _fake_process_document(document_id: str, **_kwargs) -> int:
        nonlocal active_jobs, max_active_jobs
        assert document_id
        active_jobs += 1
        max_active_jobs = max(max_active_jobs, active_jobs)
        await asyncio.sleep(0.05)
        active_jobs -= 1
        return 1

    monkeypatch.setattr(document_processor, "process_document", _fake_process_document)
    monkeypatch.setattr(worker_module, "_document_processing_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(worker_module.app_settings, "worker_document_slow_warning_seconds", 5)
    monkeypatch.setattr(worker_module.app_settings, "worker_document_job_timeout", 600)

    await asyncio.gather(
        worker_module.process_document_job(
            {}, document_id="0414d0ca-5a20-45a4-b7e4-e0b69837db56"
        ),
        worker_module.process_document_job(
            {}, document_id="5dc6139f-aa86-49a7-a8f0-a745f697236e"
        ),
    )

    assert max_active_jobs == 1


@pytest.mark.asyncio
async def test_process_document_job_releases_semaphore_on_exception(monkeypatch):
    active_jobs = 0
    max_active_jobs = 0

    async def _fake_process_document(document_id: str, **_kwargs) -> int:
        nonlocal active_jobs, max_active_jobs
        active_jobs += 1
        max_active_jobs = max(max_active_jobs, active_jobs)
        await asyncio.sleep(0.01)
        active_jobs -= 1
        if document_id == "fail":
            raise RuntimeError("boom")
        return 1

    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(document_processor, "process_document", _fake_process_document)
    monkeypatch.setattr(worker_module, "_document_processing_semaphore", sem)
    monkeypatch.setattr(worker_module.app_settings, "worker_document_slow_warning_seconds", 1)
    monkeypatch.setattr(worker_module.app_settings, "worker_document_job_timeout", 600)

    pending_before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}

    with pytest.raises(RuntimeError):
        await worker_module.process_document_job({}, document_id="fail")

    assert not sem.locked(), (
        "Semaphore must be released after an exception so the next job "
        "can run; otherwise a single transient failure permanently leaks a slot."
    )

    leaked = {
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    } - pending_before
    assert not leaked, f"Slow-warning task leaked after exception: {leaked!r}"

    await worker_module.process_document_job({}, document_id="ok")
    assert max_active_jobs == 1


@pytest.mark.asyncio
async def test_process_document_job_releases_semaphore_on_cancellation(monkeypatch):
    """SAQ enforces job timeouts by cancelling the running coroutine.

    The semaphore and warning task must be cleaned up on
    ``asyncio.CancelledError`` exactly as on a regular exception,
    otherwise SAQ's hard-timeout path leaks a concurrency slot.
    """

    async def _hanging_process_document(document_id: str, **_kwargs) -> int:
        await asyncio.sleep(10)
        return 0

    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(document_processor, "process_document", _hanging_process_document)
    monkeypatch.setattr(worker_module, "_document_processing_semaphore", sem)
    monkeypatch.setattr(worker_module.app_settings, "worker_document_slow_warning_seconds", 1)
    monkeypatch.setattr(worker_module.app_settings, "worker_document_job_timeout", 600)

    job_task = asyncio.create_task(
        worker_module.process_document_job({}, document_id="cancel-me")
    )
    await asyncio.sleep(0.01)
    job_task.cancel()
    # ``asyncio.wait`` rather than bare ``await job_task`` keeps the
    # static analyser happy (an ``await`` of a Task whose result is
    # discarded is flagged as "statement has no effect" by
    # github-code-quality even though the await IS what observes the
    # cancellation).  ``wait`` returns the done set explicitly so the
    # cancellation check is a normal attribute read.
    done, _pending = await asyncio.wait([job_task])
    finished = done.pop()
    assert finished.cancelled(), (
        "process_document_job must propagate CancelledError from SAQ's "
        "hard-timeout path so the job is recorded as cancelled rather "
        "than swallowed as a success."
    )

    assert not sem.locked()


@pytest.mark.asyncio
async def test_process_document_job_emits_periodic_slow_warning(monkeypatch, caplog):
    """The slow-warning coroutine must keep emitting while the embedding
    job is still running, not fire only once.

    Operators rely on the periodic signal to detect sustained
    degradation between the slow threshold and the SAQ hard timeout.
    """
    import logging

    def _slow_records() -> list[logging.LogRecord]:
        return [r for r in caplog.records if "still processing after" in r.getMessage()]

    async def _slow_process_document(document_id: str, **_kwargs) -> int:
        # Outlives the second warning whatever the machine's speed: a job of
        # fixed length races that warning's timer. Bounded for a warning that
        # never repeats.
        deadline = asyncio.get_running_loop().time() + 10
        while len(_slow_records()) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        return 1

    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(document_processor, "process_document", _slow_process_document)
    monkeypatch.setattr(worker_module, "_document_processing_semaphore", sem)
    # ``process_document_job`` enforces a 1-second floor on the slow
    # threshold; setting 1 here keeps the test short (~2s) while
    # still proving the warning fires more than once.
    monkeypatch.setattr(worker_module.app_settings, "worker_document_slow_warning_seconds", 1)
    monkeypatch.setattr(worker_module.app_settings, "worker_document_job_timeout", 600)

    caplog.set_level(logging.WARNING, logger=worker_module.logger.name)
    await worker_module.process_document_job({}, document_id="slow-doc")

    slow_records = _slow_records()
    assert len(slow_records) >= 2, (
        "Periodic slow warning should fire multiple times for a long-running "
        f"document, but only {len(slow_records)} warning(s) were emitted: "
        f"{[r.getMessage() for r in slow_records]}"
    )


@pytest.mark.asyncio
async def test_ensure_document_processing_semaphore_is_idempotent(monkeypatch):
    """``_ensure_document_processing_semaphore`` must build the
    semaphore lazily inside the running event loop and return the same
    instance on subsequent calls.

    Lazy construction is the whole reason the module-level
    ``_document_processing_semaphore`` defaults to ``None`` — see the
    detailed comment in ``app/worker.py``.  This test locks the
    contract so a future refactor that re-introduces eager
    construction at import time gets caught.
    """
    monkeypatch.setattr(worker_module, "_document_processing_semaphore", None)
    monkeypatch.setattr(
        worker_module.app_settings, "worker_document_processing_concurrency", 3
    )

    sem1 = worker_module._ensure_document_processing_semaphore()
    sem2 = worker_module._ensure_document_processing_semaphore()

    assert isinstance(sem1, asyncio.Semaphore)
    assert sem1 is sem2
    assert worker_module._document_processing_semaphore is sem1


@pytest.mark.asyncio
async def test_ensure_document_processing_semaphore_clamps_concurrency(monkeypatch):
    """Misconfigured ``worker_document_processing_concurrency=0`` (or
    negative) must clamp to ``1`` so we never construct a useless
    zero-permit semaphore that would deadlock every document job.
    """
    monkeypatch.setattr(worker_module, "_document_processing_semaphore", None)
    monkeypatch.setattr(
        worker_module.app_settings, "worker_document_processing_concurrency", 0
    )

    sem = worker_module._ensure_document_processing_semaphore()
    async with sem:
        assert sem.locked()
    assert not sem.locked()


@pytest.mark.asyncio
async def test_process_document_job_logs_unexpected_warn_task_exception(
    monkeypatch, caplog
):
    """Regression: drain of the slow-warning task must surface
    NON-CancelledError exceptions rather than silently swallowing
    them.

    Earlier revisions used
    ``contextlib.suppress(asyncio.CancelledError, Exception)`` which
    hid e.g. a logger handler crash inside ``_warn_if_slow`` —
    operators would see throughput drop with no signal in the worker
    log.  After the narrow-exception-suppression fix, the cancellation
    is still consumed silently (expected path) but any other
    exception is re-surfaced via ``logger.warning(..., exc_info=...)``
    so it lands in error monitoring.
    """
    import logging

    # ``_warn_if_slow`` is defined inline inside
    # ``process_document_job`` so we cannot monkeypatch it directly.
    # Instead, patch ``asyncio.create_task`` to wrap the coroutine in
    # a task whose body raises a non-CancelledError exception when
    # awaited after cancellation.  This drives the drain branch into
    # the "warn_task.exception() is not None" path.
    real_create_task = asyncio.create_task

    async def _exploding_warn() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Simulate a buggy WARNING-emit (e.g. bad %-format,
            # logger handler crash) firing during shutdown.  This
            # exception is NOT a CancelledError, so the drain
            # branch must surface it instead of suppressing.
            raise RuntimeError(
                "simulated logger handler crash inside slow-warning task"
            ) from None

    created_tasks: list[asyncio.Task] = []

    def _capturing_create_task(coro, **kwargs):
        # Replace the inline ``_warn_if_slow`` coro with our
        # exploding variant — the worker only stores the task, not
        # the coro, so swapping at create-time is sufficient.
        if hasattr(coro, "cr_code") and coro.cr_code.co_name == "_warn_if_slow":
            coro.close()
            coro = _exploding_warn()
        t = real_create_task(coro, **kwargs)
        created_tasks.append(t)
        return t

    monkeypatch.setattr(asyncio, "create_task", _capturing_create_task)

    async def _fast_process_document(document_id: str, **_kwargs) -> int:
        # Give the warn-task time to enter its ``asyncio.sleep`` so
        # the cancellation hits inside ``await asyncio.sleep`` and
        # triggers the except branch where we raise RuntimeError.
        await asyncio.sleep(0.05)
        return 1

    sem = asyncio.Semaphore(1)
    monkeypatch.setattr(document_processor, "process_document", _fast_process_document)
    monkeypatch.setattr(worker_module, "_document_processing_semaphore", sem)
    monkeypatch.setattr(worker_module.app_settings, "worker_document_slow_warning_seconds", 1)
    monkeypatch.setattr(worker_module.app_settings, "worker_document_job_timeout", 600)

    caplog.set_level(logging.WARNING, logger=worker_module.logger.name)
    await worker_module.process_document_job({}, document_id="explode-warn")

    failure_records = [
        r
        for r in caplog.records
        if "Slow-warning task" in r.getMessage()
        and "failed unexpectedly" in r.getMessage()
    ]
    assert failure_records, (
        "An unexpected exception inside _warn_if_slow must be logged "
        "as a WARNING with exc_info — earlier revisions used "
        "contextlib.suppress(Exception) which hid this entirely.  "
        f"Observed records: {[r.getMessage() for r in caplog.records]}"
    )
    assert sem.locked() is False, (
        "Semaphore must still be released even when the warn-task "
        "drain encounters an unexpected exception."
    )


def test_job_task_context_guard_handles_cancelled_jobs_without_context():
    from saq import Job

    guard = worker_module._JobTaskContextGuard()
    cancelled_job = Job(function="run_assignment_job", key="cancelled-job-key")
    cancelled_job.error = "  cancelled  "

    assert worker_module._is_cancelled_job(cancelled_job) is True
    assert guard[cancelled_job]["aborted"] == "cancelled"
    assert guard[cancelled_job]["aborted"] is not None


def test_job_task_context_guard_still_raises_for_non_cancelled_jobs():
    from saq import Job

    guard = worker_module._JobTaskContextGuard()
    active_job = Job(function="run_assignment_job", key="active-job-key")
    active_job.error = "boom"

    assert worker_module._is_cancelled_job(active_job) is False
    with pytest.raises(KeyError):
        _ = guard[active_job]


@pytest.mark.asyncio
async def test_startup_wraps_worker_job_contexts_with_guard(monkeypatch):
    fake_models = types.ModuleType("app.models")
    monkeypatch.setitem(sys.modules, "app.models", fake_models)
    worker = types.SimpleNamespace(job_task_contexts={})
    monkeypatch.setattr(worker_module.app_settings, "worker_document_processing_concurrency", 1)

    # ``startup`` has process-wide side effects that have nothing to do with
    # the guard assertion below and would outlive this test's event loop (which
    # pytest-asyncio closes as soon as it returns):
    #
    #   * it starts the orchestration reconciler as a background task and
    #     stores it in the module-level ``worker_module._reconciler_task``.
    #     Unpatched, the *real* reconciler loop runs — hitting the DB from a
    #     unit test — and the global keeps pointing at that task, bound to an
    #     already-closed loop, for the rest of the session.
    #   * it installs a ~50-thread default ``ThreadPoolExecutor`` as this
    #     loop's default executor and records it in ``app.core.executor``
    #     module globals.
    #   * it waits for the database schema to reach the migration head, and a
    #     unit test has no database to wait for.
    #
    # All are stubbed out here; ``monkeypatch`` also restores
    # ``_reconciler_task`` to ``None`` at teardown, and the stub task is
    # drained below so nothing pending survives the loop.
    from app.core import executor as executor_module
    from app.core import schema_readiness
    from app.services import orchestration_reconciler

    async def _noop_reconciler_loop(draining=None) -> None:
        return None

    async def _schema_ready(*, stop=None) -> bool:
        return True

    monkeypatch.setattr(orchestration_reconciler, "reconciler_loop", _noop_reconciler_loop)
    monkeypatch.setattr(executor_module, "configure_default_executor", lambda _prefix: None)
    monkeypatch.setattr(schema_readiness, "wait_for_schema_head", _schema_ready)
    monkeypatch.setattr(worker_module, "_reconciler_task", None)

    try:
        await worker_module.startup({"worker": worker})

        assert isinstance(worker.job_task_contexts, worker_module._JobTaskContextGuard)
    finally:
        reconciler_task = worker_module._reconciler_task
        if reconciler_task is not None:
            reconciler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconciler_task


@pytest.mark.asyncio
async def test_cancel_document_processing_jobs_aborts_the_queued_jobs_of_the_deleted_documents(monkeypatch):
    from saq.job import Status

    deleted, running, kept = (
        "0414d0ca-5a20-45a4-b7e4-e0b69837db56",
        "8a3f7c1e-2b44-4f0e-9d1a-6c5e2f7b9a10",
        "5dc6139f-aa86-49a7-a8f0-a745f697236e",
    )

    def job(key, status=Status.QUEUED):
        return SimpleNamespace(key=key, status=status)

    fake_queue = _FakeKeyedQueue(
        [
            job(kb_helpers.document_job_key(deleted)),
            job(kb_helpers.relock_job_key(deleted)),
            job(kb_helpers.document_job_key(running), Status.ACTIVE),
            job(kb_helpers.document_job_key(kept)),
        ]
    )
    monkeypatch.setattr(worker_module, "queue", fake_queue)

    cancelled = await kb_helpers.cancel_document_processing_jobs([deleted, running])

    assert cancelled == 2
    assert {aborted.key for aborted, _error, _ttl in fake_queue.aborted} == {
        f"process-document-{deleted}",
        f"process-document-{deleted}-relock",
    }
    assert {error for _job, error, _ttl in fake_queue.aborted} == {"document_deleted"}
    # One MGET for the four keys: no scan of the jobs the queue holds.
    assert fake_queue.lookups == [
        [
            f"process-document-{deleted}",
            f"process-document-{deleted}-relock",
            f"process-document-{running}",
            f"process-document-{running}-relock",
        ]
    ]


@pytest.mark.asyncio
async def test_cancel_document_processing_jobs_looks_the_keys_up_in_chunks(monkeypatch):
    monkeypatch.setattr(kb_helpers, "_CANCEL_LOOKUP_CHUNK", 3)
    fake_queue = _FakeKeyedQueue([])
    monkeypatch.setattr(worker_module, "queue", fake_queue)

    assert await kb_helpers.cancel_document_processing_jobs(["a", "b"]) == 0
    assert [len(keys) for keys in fake_queue.lookups] == [3, 1]
    assert fake_queue.aborted == []


@pytest.mark.asyncio
async def test_cancel_document_processing_jobs_short_circuits_on_empty_input(
    monkeypatch,
):
    """Empty (or all-falsy) `doc_ids` must avoid touching the queue at all.

    Without the early return, deleting a KB with no document would still
    make a Redis round trip on every `delete_knowledge_base` call.
    """
    calls = {"jobs": 0}

    class _ExplodingQueue:
        async def jobs(self, keys):
            calls["jobs"] += 1
            return []

        async def abort(self, *_args, **_kwargs):
            raise AssertionError("abort() must not be called for empty input")

    monkeypatch.setattr(worker_module, "queue", _ExplodingQueue())

    assert await kb_helpers.cancel_document_processing_jobs([]) == 0
    assert await kb_helpers.cancel_document_processing_jobs([None, "", "   "[:0]]) == 0
    assert calls["jobs"] == 0


@pytest.mark.asyncio
async def test_enqueue_documents_does_not_repeat_precheck_when_batch_precheck_fails(
    monkeypatch, caplog
):
    """Regression: when the batch-level embedding pre-check inside
    ``enqueue_documents`` returns ``(None, reason)`` (pre-check itself
    failed unexpectedly), the per-doc loop MUST NOT pass ``None``
    through as ``embedding_ready``.

    The old code did exactly that, so each
    ``enqueue_document_processing`` call re-entered the
    ``if embedding_ready is None`` branch, re-ran
    ``_resolve_embedding_enqueue_state()`` (a full DB lookup +
    provider build), and re-emitted the WARNING. On the stuck-document
    sweeper path that can be 200 docs per tick — turning one
    configuration failure into 200 repeated lookups + 200 identical
    WARNINGs per autonomous tick.

    The fix passes ``embedding_ready=True`` regardless of whether the
    batch pre-check came back ``True`` or ``None``: the batch
    WARNING (logged once, above the loop) is the single source of
    truth, and ``True`` short-circuits the per-doc re-check so the
    loop just proceeds best-effort.
    """
    import logging

    fake_queue = _FakeQueue()
    monkeypatch.setattr(worker_module, "queue", fake_queue)
    monkeypatch.setattr(kb_helpers.app_settings, "worker_document_job_timeout", 1)
    monkeypatch.setattr(kb_helpers.app_settings, "worker_document_job_retries", 0)
    monkeypatch.setattr(
        kb_helpers.app_settings, "worker_document_job_retry_delay_seconds", 0.0
    )
    monkeypatch.setattr(
        kb_helpers.app_settings, "worker_document_job_retry_backoff", False
    )

    # Batch pre-check returns (None, reason) on the FIRST (batch) call.
    # Per-document re-checks would also call the same helper if they
    # ever happened — record every invocation so the test asserts
    # the helper is called EXACTLY once across the whole batch.
    resolve_calls: list[None] = []

    async def _flaky_resolve_state():
        resolve_calls.append(None)
        return (None, "boom: provider build failed")

    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", _flaky_resolve_state)

    caplog.set_level(logging.WARNING, logger=kb_helpers.logger.name)
    await kb_helpers.enqueue_documents(
        [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        ]
    )

    assert len(resolve_calls) == 1, (
        "Batch pre-check must run exactly once per enqueue_documents() "
        "call. The previous code re-ran it for every document when the "
        "batch result was None — defeating the single-probe batching "
        "and spamming the log on the autonomous sweeper path. Got "
        f"{len(resolve_calls)} pre-check calls for a 3-document batch."
    )
    # All three documents still get enqueued (proceed best-effort).
    assert len(fake_queue.jobs) == 3

    batch_warnings = [
        r for r in caplog.records if "batch pre-check failed" in r.getMessage()
    ]
    per_doc_warnings = [
        r
        for r in caplog.records
        if "pre-check failed for" in r.getMessage()
        and "batch pre-check" not in r.getMessage()
    ]
    assert len(batch_warnings) == 1, (
        f"Expected exactly one BATCH-level WARNING; got {len(batch_warnings)}."
    )
    assert len(per_doc_warnings) == 0, (
        "Per-document pre-check WARNINGs must NOT fire when the batch "
        f"pre-check already covered the same condition; got: "
        f"{[r.getMessage() for r in per_doc_warnings]}"
    )


@pytest.mark.asyncio
async def test_enqueue_document_processing_honours_false_pre_check_from_caller(
    monkeypatch, caplog
):
    """Defence-in-depth: when a caller already ran the pre-check and
    passes ``embedding_ready=False`` (caller-resolved "not configured"),
    ``enqueue_document_processing`` MUST honour the verdict and skip
    the enqueue.

    The original code branched on ``if embedding_ready is None`` only;
    an explicit ``False`` skipped that block entirely and fell straight
    through to ``queue.enqueue`` — a latent contract hole. The bug
    isn't exercised today (``enqueue_documents`` short-circuits
    before the per-doc loop when batch ``state is False``), but a
    future caller that forwards a cached ``False`` from a different
    probe site would silently enqueue work the platform can't
    process and bury the user with stuck pending docs.

    Belongs to the same family of fixes as the per-doc-recheck
    contract above — pins the ``embedding_ready`` parameter as a
    proper tri-state instead of "None | not-None".
    """
    import logging

    fake_queue = _FakeQueue()
    monkeypatch.setattr(worker_module, "queue", fake_queue)
    monkeypatch.setattr(kb_helpers.app_settings, "worker_document_job_timeout", 1)
    monkeypatch.setattr(kb_helpers.app_settings, "worker_document_job_retries", 0)
    monkeypatch.setattr(
        kb_helpers.app_settings, "worker_document_job_retry_delay_seconds", 0.0
    )
    monkeypatch.setattr(
        kb_helpers.app_settings, "worker_document_job_retry_backoff", False
    )

    caplog.set_level(logging.INFO, logger=kb_helpers.logger.name)
    result = await kb_helpers.enqueue_document_processing(
        "0414d0ca-5a20-45a4-b7e4-e0b69837db56",
        embedding_ready=False,
    )

    assert result is False, (
        "embedding_ready=False from the caller MUST skip the enqueue "
        "and return False, not silently fall through to queue.enqueue."
    )
    assert len(fake_queue.jobs) == 0, (
        "No SAQ job should have been enqueued; got "
        f"{[j.kwargs for j in fake_queue.jobs]}"
    )
    skip_records = [
        r
        for r in caplog.records
        if "caller-provided pre-check result: not configured" in r.getMessage()
    ]
    assert skip_records, (
        "The skip path must log an INFO line so operators can correlate "
        "missing enqueues with the caller-provided pre-check verdict. "
        f"Observed records: {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_cancel_document_processing_jobs_swallows_queue_errors(monkeypatch, caplog):
    """A flaky Redis / SAQ failure must not break a delete request.

    Cancellation is an optimisation — if SAQ is unreachable we still want
    the document/KB delete to succeed; we just log a warning and return 0.
    """
    import logging

    class _FailingQueue:
        async def jobs(self, keys):
            raise RuntimeError("redis unavailable")

        async def abort(self, *_args, **_kwargs):
            raise AssertionError("abort() must not be reached when the lookup fails")

    monkeypatch.setattr(worker_module, "queue", _FailingQueue())

    caplog.set_level(logging.WARNING, logger=kb_helpers.logger.name)
    cancelled = await kb_helpers.cancel_document_processing_jobs(["doc-1"])

    assert cancelled == 0
    assert any(
        "Failed to cancel queued document jobs" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hung", ["jobs", "abort"], ids=["key-lookup", "abort"])
async def test_cancel_document_processing_jobs_gives_up_on_a_queue_that_never_answers(monkeypatch, caplog, hung):
    """The queue's connection has no socket timeout: a hung Redis ends in the warning, never holds the deletion."""
    import logging

    from saq.job import Status

    doc_id = "0414d0ca-5a20-45a4-b7e4-e0b69837db56"
    fake_queue = _FakeKeyedQueue([SimpleNamespace(key=kb_helpers.document_job_key(doc_id), status=Status.QUEUED)])

    async def _never_answers(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(fake_queue, hung, _never_answers)
    monkeypatch.setattr(worker_module, "queue", fake_queue)
    monkeypatch.setattr(kb_helpers, "_ENQUEUE_TIMEOUT_SECONDS", 0.05)

    caplog.set_level(logging.WARNING, logger=kb_helpers.logger.name)
    assert await asyncio.wait_for(kb_helpers.cancel_document_processing_jobs([doc_id]), 5) == 0
    assert any("Failed to cancel queued document jobs for 1" in r.getMessage() for r in caplog.records)
