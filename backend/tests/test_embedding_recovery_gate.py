"""Tests for the per-tick embedding-readiness gate in ``kb_helpers``.

The three KB recovery sweeps (stuck / failed / stale documents) all exist to
get documents *embedded*. On an install with no embedding-capable provider
configured they can never accomplish anything, yet each one used to claim a
batch with ``FOR UPDATE SKIP LOCKED``, look the provider config up, log an
identical ~500-character INFO line and roll the claim back — on every 60 s
autonomous tick, forever (the log-spam report this module fixes).

``resolve_embedding_recovery_state`` is the single probe the autonomous tick
runs *before* touching any row:

  * ``False`` → the tick skips all three sweeps (nothing claimed, no locks
    taken, no per-sweep log line), and the operator-facing line is emitted
    here — throttled to "first occurrence + reason change + hourly heartbeat".
  * ``True`` / ``None`` → forwarded to the sweeps via ``embedding_ready`` so
    they don't repeat the provider-config lookup three more times.

Covers the throttle state machine, the ``embedding_ready`` pass-through
contract of all three sweeps, and the tick wiring itself.
"""

from __future__ import annotations

import inspect
import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.knowledge_base import KnowledgeDocument
from app.services import autonomous, kb_helpers

_UNCONFIGURED = "An embedding-capable provider is required (...)"


@pytest.fixture(autouse=True)
def _reset_throttle():
    """Throttle memory is module-level (it must survive across ticks), so each
    test starts and ends from a clean slate — otherwise test order would decide
    whether a line is emitted at INFO or demoted to DEBUG.
    """
    kb_helpers.reset_embedding_state_log_throttle()
    yield
    kb_helpers.reset_embedding_state_log_throttle()


@pytest.fixture
def fake_clock(monkeypatch):
    """Drive the throttle's ``time.monotonic`` from the test.

    Patched on the ``kb_helpers`` module rather than the global ``time``
    module: only ``_should_log_embedding_state`` reads it, and a global patch
    would leak into unrelated library code for the rest of the session.
    """
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(kb_helpers, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def _resolver(*results):
    """Patchable ``_resolve_embedding_enqueue_state`` returning *results* in order."""
    return AsyncMock(side_effect=list(results))


def _levels(caplog, needle: str) -> list[str]:
    return [r.levelname for r in caplog.records if needle in r.getMessage()]


# ---------------------------------------------------------------------------
# resolve_embedding_recovery_state — verdicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_returns_false_when_no_provider(monkeypatch, caplog):
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver((False, _UNCONFIGURED)),
    )

    with caplog.at_level("DEBUG"):
        assert await kb_helpers.resolve_embedding_recovery_state() is False

    # The reason must survive into the log line: it is the only place an
    # operator learns *which* piece of configuration is missing.
    assert _levels(caplog, "KB embedding recovery sweeps skipped") == ["INFO"]
    assert _UNCONFIGURED in caplog.text


@pytest.mark.asyncio
async def test_gate_returns_true_when_configured(monkeypatch, caplog):
    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", _resolver((True, None)))

    with caplog.at_level("DEBUG"):
        assert await kb_helpers.resolve_embedding_recovery_state() is True

    # Healthy is the boring case — it must stay silent, this runs every 60 s.
    assert caplog.records == []


@pytest.mark.asyncio
async def test_gate_returns_none_and_warns_when_probe_fails(monkeypatch, caplog):
    """A probe that blew up is NOT "embedding unavailable": the sweeps must
    still run best-effort, and the anomaly gets a WARNING (not INFO)."""
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver((None, "connection reset")),
    )

    with caplog.at_level("DEBUG"):
        assert await kb_helpers.resolve_embedding_recovery_state() is None

    assert _levels(caplog, "KB embedding recovery pre-check failed") == ["WARNING"]


# ---------------------------------------------------------------------------
# resolve_embedding_recovery_state — log throttling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_unavailable_state_is_demoted_to_debug(monkeypatch, caplog, fake_clock):
    """The whole point of the fix: a static misconfiguration must not produce
    one INFO line per tick.  First tick INFO, every tick after that DEBUG."""
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver(*[(False, _UNCONFIGURED)] * 5),
    )

    with caplog.at_level("DEBUG"):
        for _ in range(5):
            fake_clock.now += 60.0  # one autonomous tick apart
            assert await kb_helpers.resolve_embedding_recovery_state() is False

    assert _levels(caplog, "KB embedding recovery sweeps skipped") == [
        "INFO",
        "DEBUG",
        "DEBUG",
        "DEBUG",
        "DEBUG",
    ]


@pytest.mark.asyncio
async def test_unavailable_state_reemits_info_after_heartbeat_interval(monkeypatch, caplog, fake_clock):
    """Suppression must not be permanent — an hourly heartbeat keeps the state
    discoverable for an operator reading logs at INFO."""
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver(*[(False, _UNCONFIGURED)] * 3),
    )

    with caplog.at_level("DEBUG"):
        assert await kb_helpers.resolve_embedding_recovery_state() is False
        fake_clock.now += kb_helpers._EMBEDDING_STATE_LOG_INTERVAL_SECONDS - 1
        assert await kb_helpers.resolve_embedding_recovery_state() is False
        fake_clock.now += 2
        assert await kb_helpers.resolve_embedding_recovery_state() is False

    assert _levels(caplog, "KB embedding recovery sweeps skipped") == [
        "INFO",
        "DEBUG",
        "INFO",
    ]


@pytest.mark.asyncio
async def test_changed_reason_reemits_once_the_floor_has_passed(monkeypatch, caplog, fake_clock):
    """A different reason is genuinely new information (e.g. the admin fixed
    the provider but left the model unset) — it does not wait out the hourly
    heartbeat, only the short reason floor, which is what keeps a flapping
    reason from re-emitting on every tick. A change that happened
    inside the floor and stays is reported when the floor passes."""
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver(
            (False, _UNCONFIGURED),
            (False, "embedding_model is not set"),
            (False, "embedding_model is not set"),
        ),
    )

    with caplog.at_level("DEBUG"):
        await kb_helpers.resolve_embedding_recovery_state()
        fake_clock.now += 60.0
        await kb_helpers.resolve_embedding_recovery_state()
        fake_clock.now += kb_helpers._EMBEDDING_STATE_REASON_FLOOR_SECONDS
        await kb_helpers.resolve_embedding_recovery_state()

    assert _levels(caplog, "KB embedding recovery sweeps skipped") == ["INFO", "DEBUG", "INFO"]


@pytest.mark.asyncio
async def test_flapping_reasons_emit_at_most_once_per_floor(monkeypatch, caplog, fake_clock):
    """The "pre-check failed" reason is an exception's text, so it can
    alternate between two values; each flip used to re-emit the WARNING."""
    ticks = 10
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver(*[(None, "timeout" if i % 2 else "connection reset") for i in range(ticks)]),
    )

    with caplog.at_level("DEBUG"):
        for _ in range(ticks):
            fake_clock.now += 60.0
            assert await kb_helpers.resolve_embedding_recovery_state() is None

    levels = _levels(caplog, "KB embedding recovery pre-check failed")
    assert levels[0] == "WARNING"
    # 10 ticks x 60 s = 540 s: the first line plus at most one per floor.
    assert levels.count("WARNING") <= 1 + int(ticks * 60 // kb_helpers._EMBEDDING_STATE_REASON_FLOOR_SECONDS)
    assert levels.count("WARNING") < ticks // 2


@pytest.mark.asyncio
async def test_recovering_to_healthy_reports_a_later_outage(monkeypatch, caplog, fake_clock):
    """A healthy verdict is remembered: a *later* outage reports its first
    line as soon as the reason floor has passed, instead of being swallowed by
    the heartbeat window the previous outage left behind."""
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver((False, _UNCONFIGURED), (True, None), (False, _UNCONFIGURED)),
    )

    with caplog.at_level("DEBUG"):
        await kb_helpers.resolve_embedding_recovery_state()
        fake_clock.now += 60.0
        await kb_helpers.resolve_embedding_recovery_state()
        fake_clock.now += kb_helpers._EMBEDDING_STATE_REASON_FLOOR_SECONDS
        await kb_helpers.resolve_embedding_recovery_state()

    assert _levels(caplog, "KB embedding recovery sweeps skipped") == ["INFO", "INFO"]


@pytest.mark.asyncio
async def test_healthy_flapping_emits_at_most_once_per_floor(monkeypatch, caplog, fake_clock):
    """A flaky lookup alternating healthy / failed used to wipe the throttle
    on every healthy verdict and re-emit the WARNING on every failure."""
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver(*[(True, None) if i % 2 else (None, "connection reset") for i in range(4)]),
    )

    with caplog.at_level("DEBUG"):
        for _ in range(4):
            fake_clock.now += 60.0
            await kb_helpers.resolve_embedding_recovery_state()

    assert _levels(caplog, "KB embedding recovery pre-check failed") == ["WARNING", "DEBUG"]


@pytest.mark.asyncio
async def test_unavailable_and_probe_failure_throttle_independently(monkeypatch, caplog, fake_clock):
    """The two lines have separate throttle keys: a suppressed "unavailable"
    heartbeat must not swallow the first "probe failed" WARNING."""
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        _resolver(
            (False, _UNCONFIGURED),
            (None, "connection reset"),
            (False, _UNCONFIGURED),
        ),
    )

    with caplog.at_level("DEBUG"):
        await kb_helpers.resolve_embedding_recovery_state()
        fake_clock.now += 60.0
        await kb_helpers.resolve_embedding_recovery_state()
        fake_clock.now += 60.0
        await kb_helpers.resolve_embedding_recovery_state()

    assert _levels(caplog, "KB embedding recovery sweeps skipped") == ["INFO", "DEBUG"]
    assert _levels(caplog, "KB embedding recovery pre-check failed") == ["WARNING"]


# ---------------------------------------------------------------------------
# ``embedding_ready`` pass-through on the three sweeps
# ---------------------------------------------------------------------------


class _FakeSession:
    """Async-session fake serving one fixed batch, counting commits/rollbacks."""

    def __init__(self, docs):
        self._docs = list(docs)
        self.commits = 0
        self.rollbacks = 0

    class _Result:
        def __init__(self, docs):
            self._docs = docs

        def scalars(self):
            return self

        def all(self):
            return list(self._docs)

    async def execute(self, statement):
        return self._Result(self._docs)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _doc(*, status: str, minutes_ago: float = 30.0) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename="doc.md",
        source_type="upload",
        content="x" * 64,
        status=status,
        chunk_count=0,
        embedding_retry_count=0,
        embedding_next_retry_at=(datetime.now(UTC) - timedelta(minutes=1) if status == "error" else None),
        updated_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def sweep_env(monkeypatch):
    """Neutralise everything the sweeps touch besides the embedding probe."""
    monkeypatch.setattr(
        kb_helpers,
        "app_settings",
        SimpleNamespace(
            embedding_auto_retry_enabled=True,
            embedding_auto_retry_batch_size=100,
            embedding_auto_retry_max_attempts=0,
            embedding_stale_auto_remediation_enabled=True,
            embedding_stale_remediation_batch_size=100,
            worker_document_job_timeout=600,
            worker_document_job_retries=1,
            worker_document_job_retry_delay_seconds=1,
            worker_document_job_retry_backoff=1,
        ),
    )
    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                embedding_model="text-embedding-3-large",
                embedding_dimensions=1536,
            )
        ),
    )
    monkeypatch.setattr(kb_helpers, "warn_if_no_active_workers", AsyncMock(return_value=False))
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue)
    resolver = AsyncMock()
    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", resolver)
    return SimpleNamespace(enqueue=enqueue, resolver=resolver)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sweep", "doc_status"),
    [
        ("recover_stuck_documents", "pending"),
        ("recover_failed_documents", "error"),
        ("recover_stale_documents", "ready"),
    ],
)
async def test_sweep_reuses_caller_verdict_instead_of_reprobing(sweep, doc_status, sweep_env):
    """``embedding_ready=True`` means the tick already probed — the sweep must
    NOT spend another provider-config lookup (three sweeps × every 60 s)."""
    session = _FakeSession([_doc(status=doc_status)])

    processed = await getattr(kb_helpers, sweep)(session, embedding_ready=True)

    assert processed == 1
    sweep_env.resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sweep", "doc_status"),
    [
        ("recover_stuck_documents", "pending"),
        ("recover_failed_documents", "error"),
        ("recover_stale_documents", "ready"),
    ],
)
async def test_sweep_honours_caller_provided_false_verdict(sweep, doc_status, sweep_env):
    """A caller-provided ``False`` must be honoured as-is: no re-probe, no
    enqueue, no commit, and the ``FOR UPDATE SKIP LOCKED`` row locks released.

    Without this the parameter would be a latent contract hole — a cached
    ``False`` from a different probe site would let the sweep enqueue work the
    platform cannot process.
    """
    doc = _doc(status=doc_status)
    session = _FakeSession([doc])

    processed = await getattr(kb_helpers, sweep)(session, embedding_ready=False)

    assert processed == 0
    sweep_env.resolver.assert_not_awaited()
    sweep_env.enqueue.assert_not_awaited()
    assert doc.status == doc_status  # untouched
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sweep", "doc_status"),
    [
        ("recover_stuck_documents", "pending"),
        ("recover_failed_documents", "error"),
        ("recover_stale_documents", "ready"),
    ],
)
async def test_sweep_still_probes_when_no_verdict_is_supplied(sweep, doc_status, sweep_env):
    """The standalone contract is unchanged: no caller verdict → probe."""
    sweep_env.resolver.return_value = (True, None)
    session = _FakeSession([_doc(status=doc_status)])

    processed = await getattr(kb_helpers, sweep)(session)

    assert processed == 1
    sweep_env.resolver.assert_awaited_once()


# ---------------------------------------------------------------------------
# Autonomous tick wiring
# ---------------------------------------------------------------------------


def test_autonomous_tick_gates_the_recovery_sweeps_on_one_probe():
    """Pin the tick wiring by source shape.

    Exercising the tick end-to-end would need the whole tick's
    dependencies (Redis, OAuth refresh, assignment scheduling, …), so this
    asserts the structural invariant the fix is made of instead: the single
    gate probe runs BEFORE the three sweeps, they sit behind the
    ``is not False`` guard, and none of them re-probes on its own.
    """
    src = inspect.getsource(autonomous._run_autonomous_tick_inner)

    gate_at = src.index("await _probe_embedding_readiness()")
    guard_at = src.index("if embedding_ready is not False:")
    assert gate_at < guard_at

    for sweep in (
        "recover_stuck_documents",
        "recover_failed_documents",
        "recover_stale_documents",
    ):
        call_at = src.index(f"await {sweep}(")
        assert guard_at < call_at, f"{sweep} must be gated by the readiness probe"
        # Inside the guard the verdict is ``True`` or ``None`` (probe failed);
        # both are forwarded as ``True`` so no sweep repeats the lookup — or,
        # on a failing probe, the WARNING the gate already emitted.
        assert "embedding_ready=True" in src[call_at : call_at + 80]


# ---------------------------------------------------------------------------
# Tick probe plumbing error — throttled in ``autonomous``
# ---------------------------------------------------------------------------


@pytest.fixture
def probe_env(monkeypatch):
    """A failing / succeeding readiness probe and a clock for the tick helper.

    The throttle state is reset per test; the clock is patched on the
    ``autonomous`` module only, like ``fake_clock`` above.
    """
    clock = SimpleNamespace(now=5000.0)
    monkeypatch.setattr(autonomous, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(autonomous, "_embedding_probe_error_logged", None)
    outcomes: list = []

    async def _probe():
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(kb_helpers, "resolve_embedding_recovery_state", _probe)
    return SimpleNamespace(clock=clock, outcomes=outcomes)


async def _run_probe(probe_env, *outcomes, step: float = 60.0) -> list:
    probe_env.outcomes.extend(outcomes)
    verdicts = []
    for _ in outcomes:
        probe_env.clock.now += step
        verdicts.append(await autonomous._probe_embedding_readiness())
    return verdicts


@pytest.mark.asyncio
async def test_tick_probe_plumbing_error_is_error_once_then_debug(probe_env, caplog):
    """A persistent breakage used to log a full ERROR + traceback every tick."""
    with caplog.at_level("DEBUG"):
        verdicts = await _run_probe(probe_env, *[ImportError("boom")] * 3)

    assert verdicts == [None, None, None]
    assert _levels(caplog, "KB embedding readiness probe error") == ["ERROR", "DEBUG", "DEBUG"]


@pytest.mark.asyncio
async def test_tick_probe_plumbing_error_has_an_hourly_heartbeat(probe_env, caplog):
    with caplog.at_level("DEBUG"):
        await _run_probe(probe_env, ImportError("boom"))
        await _run_probe(
            probe_env,
            ImportError("boom"),
            step=autonomous._EMBEDDING_PROBE_ERROR_LOG_INTERVAL_SECONDS,
        )

    assert _levels(caplog, "KB embedding readiness probe error") == ["ERROR", "ERROR"]


@pytest.mark.asyncio
async def test_tick_probe_success_does_not_rearm_the_error(probe_env, caplog):
    """An intermittent failure must not re-emit on every flip; a failure after
    the floor is reported again, and the verdict passes through untouched."""
    with caplog.at_level("DEBUG"):
        verdicts = await _run_probe(probe_env, ImportError("boom"), True, ImportError("boom"), False)
        await _run_probe(
            probe_env,
            ImportError("boom"),
            step=autonomous._EMBEDDING_PROBE_ERROR_FLOOR_SECONDS,
        )

    assert verdicts == [None, True, None, False]
    assert _levels(caplog, "KB embedding readiness probe error") == ["ERROR", "DEBUG", "ERROR"]


@pytest.mark.asyncio
async def test_unimportable_kb_helpers_skips_the_sweeps_and_logs_once(probe_env, caplog, monkeypatch):
    """The three sweeps import from ``kb_helpers`` too: running them on a
    broken import would log three unthrottled errors per tick. ``False`` makes
    the tick skip them (the ``is not False`` guard pinned above)."""
    monkeypatch.setitem(sys.modules, "app.services.kb_helpers", None)

    with caplog.at_level("DEBUG"):
        verdicts = await _run_probe(probe_env, None, None, None)

    assert verdicts == [False, False, False]
    assert _levels(caplog, "KB embedding readiness probe error") == ["ERROR", "DEBUG", "DEBUG"]


@pytest.mark.asyncio
async def test_same_named_failures_from_different_modules_are_different_kinds(probe_env, caplog):
    first = type("TimeoutError", (Exception,), {"__module__": "httpx"})
    second = type("TimeoutError", (Exception,), {"__module__": "asyncio"})

    with caplog.at_level("DEBUG"):
        await _run_probe(probe_env, first("a"))
        await _run_probe(
            probe_env,
            second("b"),
            step=autonomous._EMBEDDING_PROBE_ERROR_FLOOR_SECONDS,
        )

    assert _levels(caplog, "KB embedding readiness probe error") == ["ERROR", "ERROR"]
