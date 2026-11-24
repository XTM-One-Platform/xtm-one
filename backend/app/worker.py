# SPDX-License-Identifier: Apache-2.0

"""SAQ background worker – processes assignment execution jobs.

SAQ (Simple Async Queue) replaces arq for Redis 8 compatibility.
Start with:  saq app.worker.settings

Concurrency and job timeout are configurable via environment variables
(WORKER_CONCURRENCY, WORKER_JOB_TIMEOUT) — see core/config.py.

Redis HA note:
    SAQ's ``Queue.from_url`` does not understand Sentinel discovery
    URLs.  When ``REDIS_MODE=sentinel`` the worker queue is built by
    ``app.core.queue.get_queue()``, which uses redis-py's
    ``Sentinel.master_for(...)`` proxy to resolve the current master
    on every command (transparently following failovers) and hands
    that pre-built client to SAQ via ``RedisQueue``.  Standalone
    mode keeps using ``Queue.from_url(REDIS_URL)``.  Redis Cluster
    is intentionally **not** supported — SAQ relies on ``BLMOVE``
    and multi-key Lua scripts that are not cluster-safe.
"""

import asyncio
import contextlib
import logging
import time

from app.core.config import settings as app_settings
from app.core.logging import setup_logging
from app.core.queue import close_queue, get_queue

setup_logging(
    debug=app_settings.debug,
    log_level=app_settings.log_level,
    log_format=app_settings.log_format,
)
logger = logging.getLogger(__name__)

# See app/main.py for why this must run after setup_logging() rather than
# inside the Settings validator (import-time logging is silently dropped).
if app_settings.enabled_feature_flags:
    from app.core.feature_flags import resolve_enabled_feature_flags

    # The flags in effect; no line when none is (AGENTIC_CODING alone in XTM One mode).
    if _in_effect := resolve_enabled_feature_flags():
        logger.info("Feature flags enabled: %s", _in_effect)

queue = get_queue()


def _is_cancelled_job(job: object) -> bool:
    return str(getattr(job, "error", "") or "").strip().lower() == "cancelled"


class _JobTaskContextGuard(dict):
    """Guard missing SAQ task contexts for stale cancelled jobs."""

    def __getitem__(self, key):  # type: ignore[override]
        try:
            return super().__getitem__(key)
        except KeyError:
            if _is_cancelled_job(key):
                logger.warning(
                    "Missing SAQ task context for cancelled job key=%s; skipping completion",
                    getattr(key, "key", "<unknown>"),
                )
                return {"aborted": "cancelled"}
            raise


# Module-level semaphore is created lazily inside ``startup()`` (which
# runs in the SAQ worker's event loop) rather than at import time.
# ``asyncio.Semaphore`` binds to the running event loop on its first
# ``acquire()``; constructing it at import time — before any loop is
# running — and then having ``kb_helpers`` import this module during app
# startup (which runs in a different loop than the SAQ worker's) would
# leave the semaphore bound to the wrong loop and produce
# ``RuntimeError: ... attached to a different loop`` when the first job
# tries to acquire it.  Lazy construction inside the loop that will
# actually consume it guarantees correctness.  Tests that need to
# exercise the semaphore directly can monkeypatch this attribute or
# rely on the ``_ensure_document_processing_semaphore`` helper below
# which is idempotent.
_document_processing_semaphore: asyncio.Semaphore | None = None

# The orchestration reconciler runs as a background task inside the worker's
# event loop (started in ``startup``, cancelled in ``shutdown``). It is the
# single drive loop for the durable FSM engine (assignment dispatch, evaluation
# runs, sub-agent tasks) — replacing the old SAQ ``run_assignment_job`` /
# ``run_evaluation_job`` / ``resume_evaluation_episode_job`` jobs and the
# autonomous-scheduler recovery patchwork.
_reconciler_task: asyncio.Task | None = None

# The platform branding (the name in system prompts, e-mails and tool results)
# is a per-process snapshot: loaded at startup and kept current by this task,
# like every web process does.
_branding_task: asyncio.Task | None = None
_BRANDING_PRIME_TIMEOUT_SECONDS = 10

# Keeps this worker's security options snapshot in step with Settings >
# Security. Started in ``startup``, cancelled in ``shutdown``.
_security_options_refresh_task: asyncio.Task | None = None

# Keeps the LLM service identity loaded and current (``app.llm.identity``).
# Started in ``startup``, cancelled in ``shutdown``.
_service_identity_task: asyncio.Task | None = None

# The startup GPU probe of a Video Studio recording lane, kept so it is
# not garbage-collected while it runs.
_recording_gl_task: asyncio.Task | None = None


async def _prime_branding() -> None:
    from app.core.database import async_session
    from app.services.branding import refresh_branding_cache

    try:
        async with async_session() as db:
            await asyncio.wait_for(refresh_branding_cache(db), _BRANDING_PRIME_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("Could not load the platform branding; using the defaults until the next refresh", exc_info=True)


def _log_reconciler_exit(task: asyncio.Task) -> None:
    """Surface an unexpected reconciler-loop exit.

    The reconciler is meant to run for the worker's whole lifetime. If its task
    finishes with an exception (rather than the clean ``CancelledError`` raised
    by ``shutdown()``), orchestration silently stops while the worker keeps
    serving SAQ jobs — executions would simply stop draining with no obvious
    cause. Log it loudly so ops notices.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Orchestration reconciler loop exited unexpectedly: %s", exc, exc_info=exc
        )


def _note_stop_signals(worker: object) -> None:
    """Have the signals that stop SAQ's *worker* say which one it was, then stop it as SAQ does.

    SAQ sets its stop event on SIGINT / SIGTERM without saying which: the
    reconciler's presence says it, so the worker that resumes the work tells a
    stop asked for (a deploy, a restart) from a worker killed without one.
    """
    event = getattr(worker, "event", None)
    signals = getattr(worker, "SIGNALS", ()) or ()
    if not isinstance(event, asyncio.Event) or not signals:
        return
    import signal as signals_module

    from app.services.orchestration_reconciler import note_stop_signal

    loop = asyncio.get_running_loop()
    for signum in signals:

        def asked(signum: int = signum) -> None:
            stop_asked(signals_module.Signals(signum).name)
            note_stop_signal(signals_module.Signals(signum).name)
            event.set()

        try:
            loop.add_signal_handler(signum, asked)
        except (NotImplementedError, RuntimeError, ValueError):
            return


def stop_asked(signal_name: str) -> None:
    """Record that this worker's host asked it to stop.

    The background tasks it runs checkpoint their turn: from now on each one
    winds down at its next iteration boundary (``worker_shutdown.wind_down_due``,
    the tool call in flight answered and checkpointed) and is handed back to
    the queue at once, resumed by the next worker from that checkpoint, rather
    than cut mid-call when the pod is killed and taken back after its lease.
    """
    from app.core import worker_shutdown

    worker_shutdown.note_shutdown(worker_shutdown.STOP, signal_name)


def _ensure_document_processing_semaphore() -> asyncio.Semaphore:
    """Build the document-processing semaphore on first use.

    Idempotent: subsequent calls return the same instance.  This is the
    single source of truth for the semaphore lifecycle — both
    ``startup()`` and ``process_document_job()`` route through it so
    test-time monkeypatching of ``_document_processing_semaphore`` to a
    pre-built ``asyncio.Semaphore(N)`` (the pattern used in
    ``test_document_job_queue``) continues to work.
    """
    global _document_processing_semaphore
    if _document_processing_semaphore is None:
        _document_processing_semaphore = asyncio.Semaphore(
            max(1, app_settings.worker_document_processing_concurrency)
        )
    return _document_processing_semaphore


async def startup(ctx: dict) -> None:
    """Worker startup hook – import all ORM models so metadata is populated.

    All model registrations are centralized in app/models/__init__.py.
    """
    # Seed OTEL exporter defaults before anything can touch the OTEL SDK: the
    # Langfuse SDK builds its OTEL BatchSpanProcessor from environment values
    # at first client initialization.  ``get_langfuse_client`` seeds lazily
    # right before construction, but seeding eagerly here (mirroring the
    # ``_seed_langfuse_otel_exporter_defaults()`` call in ``main.py``) makes
    # the worker robust to any code path that initializes OTEL machinery
    # without going through it.
    from app.services.langfuse_tracing import _seed_otel_exporter_limits_from_env

    _seed_otel_exporter_limits_from_env()

    import app.models  # noqa: F401

    # A Platform Health consent revocation made by a job runs all its handlers too.
    from app.services.health.consent import register_revocation_handlers

    register_revocation_handlers()

    # Install a generously sized default ThreadPoolExecutor on the worker's
    # event loop FIRST — before the reconciler task or any job runs. asyncpg
    # resolves DNS for every new DB connection on the default executor, which
    # the worker also hammers with blocking S3/Bedrock/docx ``to_thread`` work;
    # the tiny Python default (min(32, cpu+4)) gets saturated under agentic
    # bursts and starves DB connection establishment, wedging the reconciler.
    # Doing this here guarantees our executor is the loop's
    # default before anything can lazily create the small built-in one.
    from app.core.executor import configure_default_executor

    configure_default_executor("xtmone-worker")

    # Event-loop stall watchdog: the worker parses documents
    # and runs the same blocking-prone code paths — a blocked loop here
    # wedges the reconciler heartbeat; log the blocking stack when it happens.
    from app.core.loop_watchdog import start_loop_watchdog

    start_loop_watchdog("worker")

    # Only the platform migrates, and a deploy starts both images together:
    # every read below and every job would run these models against the
    # previous schema until the platform has migrated (a new
    # ``assignments`` column would fail every assignment run of the rollout).
    from app.core.schema_readiness import wait_for_schema_head

    stop = getattr(ctx.get("worker"), "event", None)
    if not await wait_for_schema_head(stop=stop if isinstance(stop, asyncio.Event) else None):
        # SAQ takes a cancelled start as a shutdown: nothing was started, no
        # job was taken, and the shutdown hook runs.
        raise asyncio.CancelledError

    # The identity gateway calls are signed as when no user is bound to them
    # (company-managed KB embedding, runs without a user): only the platform's
    # bootstrap sets it. Loaded up front, then kept current by a task, also
    # when the platform has not created the admin yet.
    global _service_identity_task
    from app.llm.identity import keep_service_identity_current, load_service_identity

    await load_service_identity()
    if _service_identity_task is None or _service_identity_task.done():
        _service_identity_task = asyncio.create_task(keep_service_identity_current())

    # A Video Studio recording lane (VIDEO_STUDIO_RECORDING_GL=gpu)
    # probes its GPU at start and logs the WebGL renderer; it claims no
    # recording until the probe found the GPU, none at all on SwiftShader.
    global _recording_gl_task
    from app.services.video_studio.capture_rate import gl_backend, gl_mode

    if gl_mode() == "gpu" and (_recording_gl_task is None or _recording_gl_task.done()):
        _recording_gl_task = asyncio.create_task(gl_backend())

    # A worker deployed without the platform's ENTERPRISE_LICENSE keeps running
    # the background AI of a licensed deployment (``licence_verdict``).
    from app.services.licence_verdict import accept_licensed_verdicts

    accept_licensed_verdicts()

    # The security options stored in Settings > Access and security: jobs read
    # them from this process's snapshot (custom tools, the code interpreter,
    # runner confinement, ...), primed before the reconciler starts and kept
    # in step by a poll, like the trace storage and payload switches read
    # with them. A failed read leaves the environment / default in
    # force.
    global _security_options_refresh_task
    from app.core.security_options import log_ignored_variables
    from app.services.security_options import (
        refresh_security_options_cache,
        run_security_options_refresh_loop,
    )

    # The worker enforces quotas too: a variable the platform mode overrides
    # (QUOTA_FAIL_MODE=open in XTM One) is said once, never silently ignored.
    log_ignored_variables()
    # Runs read secrets from vaults here too: the providers this
    # process declares, and those refused, are said like on the platform.
    try:
        from app.services.vault import log_vault_configuration

        log_vault_configuration()
    except Exception:
        logger.warning("Could not read the vault provider declarations at worker startup", exc_info=True)
    # Runs fetch web pages here too (KB sync, assignments): a proxy those
    # fetches bypass is said like on the platform.
    from app.core.pinned_request import log_environment_proxy_bypass

    log_environment_proxy_bypass()
    try:
        await refresh_security_options_cache()
    except Exception:
        logger.exception(
            "Could not read the security options at worker startup; the "
            "environment and the defaults apply until the next refresh"
        )
    # And the providers connected in the UI, before a run reads a secret from one.
    try:
        from app.services.vault.store import refresh_vault_providers

        await refresh_vault_providers()
    except Exception:
        logger.exception("Could not read the vault providers at worker startup; the next refresh retries")
    if _security_options_refresh_task is None or _security_options_refresh_task.done():
        _security_options_refresh_task = asyncio.create_task(run_security_options_refresh_loop())

    # Build the document-processing semaphore inside the SAQ worker's
    # event loop so it binds to the right one (see module-level comment).
    _ensure_document_processing_semaphore()

    # Before the reconciler starts runs that put the platform name in prompts.
    global _branding_task
    await _prime_branding()
    if _branding_task is None or _branding_task.done():
        from app.services.branding import keep_branding_current

        _branding_task = asyncio.create_task(keep_branding_current())

    # Start the orchestration reconciler in the worker's event loop. It drains -
    # claims nothing new, lets what runs finish - from the moment a stop is
    # asked (SAQ's stop event), the whole shutdown grace, before the shutdown
    # hook hands back what still runs.
    global _reconciler_task
    if _reconciler_task is None or _reconciler_task.done():
        from app.services.orchestration_reconciler import reconciler_loop

        _note_stop_signals(ctx.get("worker"))
        _reconciler_task = asyncio.create_task(reconciler_loop(draining=stop if isinstance(stop, asyncio.Event) else None))
        _reconciler_task.add_done_callback(_log_reconciler_exit)
        logger.info("Orchestration reconciler task started")

    worker = ctx.get("worker")
    if worker is not None and not isinstance(worker.job_task_contexts, _JobTaskContextGuard):
        worker.job_task_contexts = _JobTaskContextGuard(worker.job_task_contexts)

    # How this worker's Video Studio renders take their frames: probed once, now, on the compositor's
    # Chromium - begun frames only where it begins one within 2 s, else screenshots. In a task of its own.
    if app_settings.video_studio_render_concurrency > 0 or app_settings.video_studio_preview_concurrency > 0:
        try:
            from app.services.video_studio.compositor import start_capture_probe

            start_capture_probe()
        except Exception:
            logger.warning("Video Studio frame capture probe not started; renders take screenshots", exc_info=True)

    # Probe and log the custom-tool sandbox mode. Assignments execute custom
    # tools in THIS process, so the security notice (WARNING for the
    # supported subprocess fallback, ERROR only for an unsatisfiable
    # explicit docker mode) must fire here at startup (mirroring the
    # platform check in main.py) rather than lazily on the first tool
    # execution after every worker restart.
    try:
        from app.services.custom_tool_executor import warn_sandbox_mode_at_startup

        await warn_sandbox_mode_at_startup()
    except asyncio.CancelledError:
        # Cancellation must keep propagating (worker teardown / shutdown).
        # ``CancelledError`` is a ``BaseException``, so the ``except
        # Exception`` below would not catch it anyway; explicit for clarity
        # (mirrors ``release_db_connection_before_loop`` in core/database.py).
        raise
    except Exception:
        logger.warning(
            "Custom tool sandbox startup check failed; continuing.", exc_info=True
        )

    # Log the EFFECTIVE document concurrency (post-clamp) so operators
    # debugging "why is throughput at N?" see the value that actually
    # governs the semaphore, not the raw setting.  Without this, a
    # misconfigured ``worker_document_processing_concurrency=0`` would
    # silently clamp to 1 inside ``_ensure_document_processing_semaphore``
    # but the log would still print "0", masking the discrepancy.
    raw_doc_concurrency = app_settings.worker_document_processing_concurrency
    effective_doc_concurrency = max(1, raw_doc_concurrency)
    if effective_doc_concurrency != raw_doc_concurrency:
        logger.info(
            "Worker started – concurrency=%d, job_timeout=%ds, "
            "document_concurrency=%d (clamped from configured %d)",
            app_settings.worker_concurrency,
            app_settings.worker_job_timeout,
            effective_doc_concurrency,
            raw_doc_concurrency,
        )
    else:
        logger.info(
            "Worker started – concurrency=%d, job_timeout=%ds, "
            "document_concurrency=%d",
            app_settings.worker_concurrency,
            app_settings.worker_job_timeout,
            effective_doc_concurrency,
        )


async def shutdown(ctx: dict) -> None:
    """Worker shutdown hook – stop the reconciler, release Redis, log stop."""
    global _reconciler_task
    # The open browser sessions' cookies and local storage, kept and shared first: a run resumed elsewhere
    # after this stop comes back signed in, a token written since its last action included.
    try:
        from app.services.browser_tool import keep_open_sessions

        await keep_open_sessions()
    except Exception:
        logger.warning("Browser sessions not kept at the worker stop", exc_info=True)
    if _reconciler_task is not None and not _reconciler_task.done():
        from app.services.orchestration_reconciler import stop_reconciler
        from app.services.video_studio.render_process import finish_before_stop

        # The whole renders past half their frames end here rather than wait for a slot elsewhere, within
        # VIDEO_STUDIO_RENDER_DRAIN_SECONDS and the pod's termination grace; the reconciler drains meanwhile.
        try:
            await finish_before_stop()
        except Exception:
            logger.warning("The stop's wait for the renders in flight failed; they are handed over now", exc_info=True)
        stop_reconciler()
        _reconciler_task.cancel()
        try:
            await _reconciler_task
        except asyncio.CancelledError:
            # Expected: we just cancelled the reconciler task above; draining it
            # surfaces the cancellation so shutdown can continue.
            pass
        except Exception:
            logger.debug("Reconciler task error on shutdown", exc_info=True)
        logger.info("Orchestration reconciler task stopped")
    _reconciler_task = None

    # The Video Studio frame-capture probe still running: its Chromium closed with the worker.
    try:
        from app.services.video_studio.compositor import stop_capture_probe

        await stop_capture_probe()
    except Exception:
        logger.debug("Video Studio frame capture probe not stopped", exc_info=True)

    global _branding_task
    if _branding_task is not None and not _branding_task.done():
        _branding_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _branding_task
    _branding_task = None

    global _security_options_refresh_task
    if _security_options_refresh_task is not None and not _security_options_refresh_task.done():
        _security_options_refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _security_options_refresh_task
    _security_options_refresh_task = None

    global _service_identity_task
    if _service_identity_task is not None and not _service_identity_task.done():
        _service_identity_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _service_identity_task
    _service_identity_task = None

    # Memory write screens, once the reconciler that can still finish an
    # execution (and write memory) is stopped.
    from app.services.builtin_tools.memory import drain_memory_screens

    await drain_memory_screens()

    # Proactively finalize this process's interrupted in-process tracing runs
    # (consult children created inside worker-executed delegations,
    # assignment_id IS NULL) so they reach a truthful terminal status within
    # seconds instead of waiting for the periodic stale sweep:
    # shutdown-interrupted work is failed, while a terminal-desync run whose
    # execution actually completed is reconciled to completed.
    # Scoped to the process-local registry — never a global sweep.
    try:
        from app.core.database import async_session as _async_session
        from app.services.autonomous import fail_orphaned_in_process_runs
        from app.services.in_process_runs import get_tracked_in_process_runs

        _own_run_ids = get_tracked_in_process_runs()
        if _own_run_ids:
            async with _async_session() as _sd:
                _finalized_on_shutdown = await fail_orphaned_in_process_runs(
                    _sd, _own_run_ids
                )
            if _finalized_on_shutdown:
                logger.info(
                    "Worker shutdown: finalized %d orphaned in-process "
                    "tracing run(s)",
                    _finalized_on_shutdown,
                )
    except Exception:
        logger.debug(
            "Worker shutdown: in-process tracing run cleanup failed", exc_info=True
        )

    # Close the traces of the runs this stop cuts, as interrupted, before the
    # flush and the dispatcher drain below: a run killed mid-turn otherwise
    # leaves no trace at all (its trace is only written when its turn ends).
    try:
        from app.services.langfuse_tracing import close_open_traces_on_shutdown

        await close_open_traces_on_shutdown("worker_stopped")
    except Exception:
        logger.debug("Open traces not closed on worker shutdown", exc_info=True)

    # Flush buffered Langfuse traces so OTEL-batched spans queued in the SDK's
    # background exporter are not lost when the worker stops (no-op when
    # Langfuse is disabled; never raises).
    try:
        from app.services.langfuse_tracing import flush_langfuse

        flush_langfuse()
    except Exception:
        logger.debug("Langfuse flush error on worker shutdown", exc_info=True)

    # Drain and stop the tracing dispatcher (queue + Redis overflow) so all
    # in-flight local traces are flushed and the _worker_loop asyncio task is
    # stopped cleanly before the executor and Redis connections are torn down.
    # Without this call the _worker_loop task is destroyed while still pending
    # ("Task was destroyed but it is pending!") and any subsequent Redis access
    # in the drain path fails with
    # "redis.exceptions.ConnectionError: cannot schedule new futures after
    # shutdown" because the executor has already been shut down.
    # The drain is bounded to 10 seconds — deliberately tighter than the
    # 30-second budget the platform lifespan uses — because this hook shares
    # the pod's termination grace period with the reconciler stop, the
    # in-process run cleanup, the Langfuse flush, and the queue teardown;
    # overrunning it would get the worker SIGKILLed mid-cleanup, which is
    # strictly worse than spilling. Traces that don't drain in time are
    # spilled to the Redis overflow list and recovered later: the platform
    # lifespan drains the overflow fully at startup, and any running
    # dispatcher trickle-drains one overflow item after each flush.
    try:
        from app.services.tracing.dispatcher import shutdown_tracing_dispatcher

        await shutdown_tracing_dispatcher(timeout_seconds=10.0)
    except Exception:
        logger.debug("Tracing dispatcher shutdown error on worker stop", exc_info=True)

    # Close the SAQ queue (Redis connection) BEFORE shutting down the thread
    # executor.  redis-py's async cleanup paths (pubsub close, connection pool
    # disconnect, SSL teardown) can call loop.run_in_executor() internally;
    # if the default ThreadPoolExecutor is already shut down at that point,
    # redis-py raises ``RuntimeError: cannot schedule new futures after
    # shutdown`` which is then wrapped as
    # ``redis.exceptions.ConnectionError: cannot schedule new futures after
    # shutdown`` — the traceback seen on every rolling-restart pod termination.
    # Closing the queue first guarantees that all Redis I/O
    # completes while the executor is still alive.  This mirrors the platform
    # lifespan teardown order in app/main.py: Langfuse flush, dispatcher
    # drain, queue close, then executor shutdown.
    await close_queue()

    # Best-effort, non-blocking shutdown of the default executor.  Must come
    # AFTER close_queue() — see comment above.  Uses wait=False +
    # cancel_futures=True so a thread still blocked on a slow/blackholed
    # dependency can never make shutdown itself hang.
    try:
        from app.core.executor import shutdown_default_executor

        shutdown_default_executor()
    except Exception:
        logger.debug("Default executor shutdown error on worker stop", exc_info=True)

    # Dedicated offload thread pools + loop watchdog.
    try:
        from app.core.parse_offload import shutdown_offload_executors

        shutdown_offload_executors()
    except Exception:
        logger.debug("Offload executors shutdown error on worker stop", exc_info=True)
    try:
        from app.services.svg_raster import shutdown_svg_raster_worker

        shutdown_svg_raster_worker()
    except Exception:
        logger.debug("SVG raster worker shutdown error on worker stop", exc_info=True)
    try:
        from app.services.nova_matching import shutdown_nova_matching_worker

        shutdown_nova_matching_worker()
    except Exception:
        logger.debug("Nova matching worker shutdown error on worker stop", exc_info=True)
    try:
        from app.core.loop_watchdog import stop_loop_watchdog

        stop_loop_watchdog("worker")
    except Exception:
        logger.debug("Loop watchdog shutdown error on worker stop", exc_info=True)

    logger.info("Worker shutting down gracefully")


async def _skip_because_ai_is_disabled(job: str, subject: str) -> bool:
    """Whether an AI-dependent job must not run right now (FR-007).

    Gating the API layer does not cover a job **already in the queue** when an
    administrator switches AI off, and these jobs reach an embedding provider —
    which FR-007 forbids outright. "AI off" is the background gate's answer
    (``agentic_gate``): switched off, or not licensed - Community Edition,
    which runs the platform with AI off.

    Queued-not-started counts as new work, so it is refused; FR-013's promise
    that work already *running* finishes is about the job currently executing,
    and this check happens before it starts. The distinction is the whole point:
    nothing is cancelled mid-flight, and nothing new begins.

    Returns rather than raises, so the subject stays **pending and retryable**
    instead of being flagged errored — the same reasoning that makes
    ``workset.queue_pending`` fire-and-forget: an unreachable queue leaves the
    document pending, which the progress read already reports as outstanding
    work. Enabling AI and re-running picks it up.

    Fails closed: an unreadable AI state skips the job, because embedding when
    the platform cannot confirm it is allowed is the worse error.
    """
    from app.core.database import async_session
    from app.services.agentic_gate import AI_DISABLED_CODE, agentic_refusal

    async with async_session() as db:
        refusal = await agentic_refusal(db)
    if refusal is None:
        return False

    logger.info(
        "%s: skipping %s for %s. It stays pending and runs once AI is "
        "available again.",
        "AI is disabled on this deployment"
        if refusal == AI_DISABLED_CODE
        else "AI is not licensed on this deployment",
        job,
        subject,
    )
    return True


#: How long a one-shot AI job skipped because AI is unavailable waits before
#: it asks again.
_AI_UNAVAILABLE_RETRY_SECONDS = 600


async def _ask_again_later(
    function: str, key: str, *, timeout: int, **kwargs: str
) -> bool:
    """Schedule *function* again: a one-shot job that just returned would leave
    its subject with nothing coming back for it. Whether a retry is scheduled
    for the subject; never raises.

    *timeout* is the job's own, the one its first scheduling gives it: SAQ's
    default is 10 s, and a rescheduled job does the same work as the original.
    The enqueue itself is bounded like every other: the queue's connection has
    no socket timeout, and a hung one must come back as "not scheduled" - the
    auto pass then fails its items - not outlast the job and strand them.

    The retry key names the subject and the retry's slot, not the enqueue:
    SAQ keeps one job per key queued, scheduled or running, so the jobs of one
    subject refused in the same slot - the several clustering passes a
    workset queues - ask for the same retry, and two refused either side of a
    slot boundary meet on their next one: ONE chain, instead of each job
    retrying on its own for as long as AI is unavailable. A retry that is
    refused again asks for a later slot, never its own key.
    """
    from saq import Job

    from app.services import kb_helpers

    retry_at = int(time.time()) + _AI_UNAVAILABLE_RETRY_SECONDS
    slot = retry_at // _AI_UNAVAILABLE_RETRY_SECONDS
    try:
        await asyncio.wait_for(
            queue.enqueue(
                Job(
                    function=function,
                    kwargs=kwargs,
                    key=f"{key}-ai-retry-{slot}",
                    scheduled=retry_at,
                    timeout=timeout,
                )
            ),
            timeout=kb_helpers._ENQUEUE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("Could not reschedule %s (%s)", function, key, exc_info=True)
        return False
    return True


async def process_document_job(ctx: dict, *, document_id: str, on_behalf_of: str | None = None) -> None:
    """Process a document: chunk, embed, store in pgvector.

    Concurrency, observability, and retry semantics
    -----------------------------------------------
    * The work is gated by ``_document_processing_semaphore`` so the
      number of in-flight document embeddings within a single worker
      process never exceeds ``worker_document_processing_concurrency``.
      Without this cap, large fan-out bursts (e.g. a knowledge base
      sync enqueueing dozens of documents at once) would saturate
      shared embedding resources and trigger the synchronized 600s
      timeouts that motivated this PR.
    * A periodic ``_warn_if_slow`` task emits a WARNING log every
      ``worker_document_slow_warning_seconds`` while the embedding
      coroutine is still running.  Periodic (rather than one-shot)
      warnings give operators ongoing signal for long-running jobs
      instead of going silent after the first warning.
    * On any exception (including ``asyncio.CancelledError`` from SAQ's
      hard timeout enforcement), the ``try/finally`` block guarantees
      the warning task is cancelled and the semaphore is released
      before the exception propagates.  Releasing the semaphore is
      essential — without it a single transient failure would
      permanently leak a concurrency slot and gradually starve the
      worker.

    SAQ retry semantics — what actually triggers a retry
    ----------------------------------------------------
    ``process_document`` (the inner service call) has its OWN retry
    loop with backoff for transient DB errors and handles benign
    races (``StaleDataError`` from concurrent deletes, missing
    documents) by logging a WARNING and returning ``0``.  After
    those internal retries, any remaining failure is converted to
    ``_mark_document_error`` + ``return 0`` so the document row is
    flagged as errored and the embedding metrics stay accurate.

    The consequence — important for operators — is that SAQ-level
    retries (``worker_document_job_retries`` /
    ``worker_document_job_retry_backoff``) only kick in for cases
    where ``process_document`` itself raises rather than returns
    cleanly: principally ``asyncio.CancelledError`` from SAQ's hard
    timeout, and any unexpected error path NOT covered by the
    inner ``try/except`` (e.g. failures inside
    ``_mark_document_error`` itself, which can happen if the DB is
    fully unreachable).  Transient embedding-provider 5xx and
    deadlocks are NOT routed through SAQ retries — they are handled
    in-process by ``process_document``'s own loop.

    *on_behalf_of* is the person who added or changed the document, when the
    enqueue knew one: an LLM provider that excludes them never embeds it
    (``provider_access``).
    """
    from app.services.document_processor import process_document

    if await _skip_because_ai_is_disabled("document processing", document_id):
        return

    logger.info("Processing document: %s", document_id)
    started_at = time.monotonic()
    slow_warning_seconds = max(1, app_settings.worker_document_slow_warning_seconds)

    async def _warn_if_slow() -> None:
        """Emit a WARNING every ``slow_warning_seconds`` until cancelled.

        Operators need ongoing visibility into long-running document
        embedding jobs (some legitimately take 5–10 min); a single
        one-shot warning would go silent between the first threshold
        and the SAQ hard timeout, masking sustained degradation.
        """
        elapsed = 0
        while True:
            try:
                await asyncio.sleep(slow_warning_seconds)
            except asyncio.CancelledError:
                return
            elapsed += slow_warning_seconds
            logger.warning(
                "Document %s still processing after %ds (slow_threshold=%ds, "
                "job_timeout=%ds)",
                document_id,
                elapsed,
                slow_warning_seconds,
                app_settings.worker_document_job_timeout,
            )

    semaphore = _ensure_document_processing_semaphore()
    async with semaphore:
        warn_task = asyncio.create_task(_warn_if_slow())
        try:
            chunks = await process_document(document_id, on_behalf_of=on_behalf_of)
        finally:
            warn_task.cancel()
            # Drain the cancellation so the task never lingers as
            # "task was destroyed but it is pending" noise in logs.
            # ``asyncio.wait`` rather than bare ``await warn_task``
            # gives us the done set explicitly so a static analyser
            # doesn't flag the await as "statement has no effect" —
            # and lets us narrow exception handling per-class:
            #
            # * ``CancelledError`` is EXPECTED (we just cancelled it)
            #   and must be silently consumed.
            # * Any OTHER exception inside ``_warn_if_slow`` (e.g. a
            #   logger.warning call hitting a bad %-format because of
            #   a typo, or a sentry/datadog handler failing) is NOT
            #   expected and MUST surface so it can be triaged.
            #   Earlier revisions used ``contextlib.suppress(
            #   CancelledError, Exception)`` which masked those
            #   operational issues silently.
            await asyncio.wait([warn_task])
            if not warn_task.cancelled():
                warn_exc = warn_task.exception()
                if warn_exc is not None:
                    logger.warning(
                        "Slow-warning task for document %s failed "
                        "unexpectedly: %s",
                        document_id,
                        warn_exc,
                        exc_info=warn_exc,
                    )

    elapsed_seconds = time.monotonic() - started_at
    logger.info(
        "Document %s: %d chunks embedded in %.2fs",
        document_id,
        chunks,
        elapsed_seconds,
    )

    # A workset item's automatic summary + priority rides the job that just
    # embedded it: the reading agent then spends a handful of tool
    # calls on a two-hundred-report corpus instead of an iteration per
    # document. A no-op for every other document, and it never raises — the
    # embedding above has already succeeded, so failing the job here would
    # re-embed for nothing.
    from app.services.workset_processing import run_auto_pass

    await run_auto_pass(document_id)


async def workset_auto_pass_job(ctx: dict, *, document_id: str) -> None:
    """Run the workset summary pass for one already-embedded document.

    Scheduled by ``workset_processing._requeue`` after a TRANSIENT provider
    failure — a rate limit or a 5xx, which a burst ingestion provokes routinely
    — and by ``workset_processing.run_pass_for_unqueued_items`` for an item no
    embedding job is coming for (a borrowed document, a metadata re-add).
    A dedicated job rather than re-running ``process_document_job``: the
    document is already embedded, and re-embedding it to retry a summary would
    pay twice for the wrong half.

    Never raises. ``run_auto_pass`` owns its own failure handling, including
    exhausting the retry budget and failing the item terminally, so there is
    nothing here for SAQ to retry on top.
    """

    if await _skip_because_ai_is_disabled("workset auto-pass", document_id):
        # A one-shot retry: returning would strand the item ``pending`` with
        # nothing coming back for it (``workset_processing``'s invariant), and
        # no sweep recovers workset items. It asks again later - or, when it
        # cannot be put back on the queue, fails its items terminally.
        if not await _ask_again_later(
            "workset_auto_pass_job",
            f"workset-auto-pass-{document_id}",
            # What `workset_processing._requeue` gives the same job.
            timeout=app_settings.worker_job_timeout,
            document_id=document_id,
        ):
            from app.services.workset_processing import fail_items_left_unscheduled

            await fail_items_left_unscheduled(document_id)
        return
    del ctx
    from app.services.workset_processing import run_auto_pass

    await run_auto_pass(document_id)


async def workset_cluster_job(ctx: dict, *, workset_id: str) -> None:
    """Cluster one sealed workset's near-duplicates.

    Scheduled on seal, and again once a sealed corpus finishes processing —
    clustering answers "how many other sources say this", which is a question
    about the whole corpus and is only meaningful once it has stopped moving.

    Never raises. ``cluster_workset`` owns its failure handling; a corpus that
    misses this pass ranks by the summariser's score alone, which is where it
    started.
    """

    if await _skip_because_ai_is_disabled("workset clustering", workset_id):
        # Scheduled once per seal: asks again later rather than leaving the
        # sealed corpus unclustered for good.
        await _ask_again_later(
            "workset_cluster_job",
            f"workset-cluster-{workset_id}",
            # What `workset_clustering.enqueue_clustering` gives the same job.
            timeout=app_settings.worker_job_timeout,
            workset_id=workset_id,
        )
        return
    del ctx
    from app.services.workset_clustering import cluster_workset

    await cluster_workset(workset_id)


async def roi_backfill_job(ctx: dict, *, subject_type: str, subject_id: str) -> None:
    """Count a subject's past runs and done work items under its new ROI declaration (#10006).

    Queued by ``roi_backfill.enqueue_backfill`` once a declaration is
    committed. Not gated on the AI switch: a deterministic database pass, no
    model call, no quota. Never raises past a log line.
    """
    del ctx
    from app.services.roi_backfill import run_backfill

    await run_backfill(subject_type, subject_id)


async def send_notification_push_job(ctx: dict, *, user_id: str, ref_kind: str, ref_id: str) -> None:
    """Push one bell notification to the person's linked phones.

    Queued by ``mobile_push.queue_push`` once the notification is committed.
    Not gated on the AI switch: it is a notification, not agentic work. Never
    raises past a log line: a phone that misses a push still has the Inbox.
    """
    del ctx
    from app.core.database import async_session
    from app.core.utils import format_exception_message
    from app.services.mobile_push import deliver_queued_push

    try:
        async with async_session() as db:
            await deliver_queued_push(db, user_id=user_id, ref_kind=ref_kind, ref_id=ref_id)
    except Exception as exc:
        logger.warning("Phone notification failed: %s", format_exception_message(exc))


async def index_workspace_conversation_job(ctx: dict, *, conversation_id: str) -> None:
    """Index one idle conversation into its workspace's conversation search.

    Queued by the autonomous tick, keyed per conversation. A job skipped while
    AI is unavailable needs no retry of its own: the conversation stays behind
    its index, so the next tick after AI comes back picks it again.

    Never raises: ``index_conversation`` records a failure on the index row,
    and the tick retries a failed conversation an hour later.
    """
    if await _skip_because_ai_is_disabled("workspace conversation indexing", conversation_id):
        return
    del ctx
    from app.services.workspace_conversation_index import index_conversation

    try:
        await index_conversation(conversation_id)
    except Exception:
        logger.exception("Workspace conversation %s indexing failed", conversation_id)


settings = {
    "queue": queue,
    "functions": [
        process_document_job,
        workset_auto_pass_job,
        workset_cluster_job,
        send_notification_push_job,
        index_workspace_conversation_job,
        roi_backfill_job,
    ],
    "startup": startup,
    "shutdown": shutdown,
    "concurrency": app_settings.worker_concurrency,
    # Bound each blocking dequeue (Redis BLMOVE).  SAQ's default of 0
    # blocks FOREVER on a single read: when that TCP connection dies
    # silently (network blip, NAT/idle reset, container restart, host
    # sleep) every concurrency slot hangs indefinitely with no error
    # while enqueued jobs pile up — the worker looks healthy but
    # processes nothing.  With a bounded dequeue each slot returns to
    # redis-py at least every N seconds, picking up reconnects and
    # health checks; combined with the TCP keepalive configured in
    # ``app.core.queue`` a dead Redis peer is detected within ~2 min
    # instead of never.
    #
    # NOTE: per-JOB timeouts are NOT configured here.  An earlier
    # revision passed ``timers: {"job_timeout": ...}`` — but SAQ's
    # ``timers`` dict only understands ``schedule`` / ``worker_info`` /
    # ``sweep`` / ``abort``, so that key was silently ignored.  Job
    # timeouts are set per ``Job(timeout=...)`` at enqueue time (see
    # ``kb_helpers.enqueue_document_processing``).
    "dequeue_timeout": float(app_settings.worker_dequeue_timeout),
    # Grace period (seconds) given to in-flight jobs before SAQ sends
    # cancellation signals on SIGTERM.  Without this (SAQ default = None/0),
    # any job still running when the pod receives SIGTERM is *immediately*
    # force-cancelled.  SAQ runs the ``shutdown`` hook above
    # only AFTER this grace elapses, so the value must leave teardown
    # headroom inside the orchestrator's kill deadline (K8s
    # ``terminationGracePeriodSeconds``, Compose ``stop_grace_period``) —
    # see the sizing note on ``worker_shutdown_grace_period`` in config.py.
    "shutdown_grace_period_s": app_settings.worker_shutdown_grace_period,
}
