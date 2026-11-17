"""Regression tests for ``autonomous._cleanup_stale_assignment_runs``.

The orchestration migration removed the startup / periodic stale-run sweep for
assignment runs on the assumption that the worker reconciler recovers every
interrupted run. But the reconciler can only recover runs that still have a
*live* ``orchestration_executions`` row. Several classes of ``AssignmentRun``
have no live execution and were therefore stranded in ``running`` / ``pending``
forever after a backend/worker restart:

  * in-process chat tracing runs (chat handover parents, consult / transfer
    children) created with ``assignment_id IS NULL`` and no execution row,
  * legacy runs created before the orchestration engine existed,
  * desyncs where the execution reached a terminal state but the terminal-state
    sync hook never flipped the run.

These tests pin the safety-net behaviour: orphaned/stale runs are failed once
past their deadline, desync runs are reconciled to the execution's actual
outcome (``completed`` execution -> ``completed`` run, any other terminal state
-> ``failed``), while runs the engine is still actively managing
(execution in ``pending`` / ``running``) are never touched.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import autonomous
from app.services.orchestration_adapters.assignment import execution_dedup_key


class _ScalarsResult:
    """Result whose ``.scalars().all()`` returns the seeded ORM-like rows."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _RowsResult:
    """Result whose ``.all()`` yields indexable rows (``row[0]`` == value)."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        if not self._results:
            raise AssertionError("Unexpected extra execute() call")
        return self._results.pop(0)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _run(
    *,
    status="running",
    assignment_id=None,
    age_seconds=7200,
    started=True,
    trigger_payload=None,
    trigger_context=None,
):
    now = datetime.now(UTC)
    started_at = now - timedelta(seconds=age_seconds) if started else None
    return SimpleNamespace(
        id=uuid.uuid4(),
        assignment_id=assignment_id,
        agent_id=uuid.uuid4(),
        status=status,
        created_at=now - timedelta(seconds=age_seconds),
        started_at=started_at,
        completed_at=None,
        error=None,
        trigger_payload=trigger_payload,
        # Carried because the sweep reads it to spot external A2A runs, which
        # hold their own execution lease. Defaults to None so the ordinary
        # tracing/legacy cases here take the same path they always did.
        trigger_context=trigger_context,
        result_summary=None,
    )


def _exec_rows(*pairs):
    """Build the (dedup_key, state) rows result for given (run, state) pairs."""
    return _RowsResult([(execution_dedup_key(run.id), state) for run, state in pairs])


def _delegation_rows(*runs):
    """Build the live sub-agent delegation probe result for the given runs.

    The sweep issues this query whenever at least one candidate has no
    ``assignment_run:<run_id>`` execution row; each returned row is the run id
    carried in a live ``sub_agent`` execution's payload.
    """
    return _RowsResult([(str(run.id),) for run in runs])


@pytest.mark.asyncio
async def test_orphaned_tracing_run_without_execution_is_failed(monkeypatch):
    """An old chat-tracing run (assignment_id NULL, no execution) is failed."""
    # Pin the fallback so the test asserts the age threshold, not the
    # (env-configurable) default of ``stale_assignment_run_seconds``.
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 3600)
    run = _run(status="running", assignment_id=None, age_seconds=7200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),  # candidates
            _exec_rows(),  # no orchestration executions
            _delegation_rows(),  # no live sub-agent delegation either
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert run.completed_at is not None
    assert run.error.startswith(autonomous.STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_recent_orphan_run_is_left_alone(monkeypatch):
    """A run with no execution but younger than the fallback threshold is kept."""
    # Pin the fallback so the test asserts the age threshold, not the
    # (env-configurable) default of ``stale_assignment_run_seconds``.
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 3600)
    run = _run(status="running", assignment_id=None, age_seconds=120)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert run.completed_at is None
    assert db.commits == 0


@pytest.mark.asyncio
async def test_run_with_live_execution_is_never_touched():
    """A run whose execution is still running belongs to the worker reconciler."""
    run = _run(status="running", assignment_id=None, age_seconds=99999)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "running")),  # engine still managing it
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_run_with_pending_execution_is_never_touched():
    """A delayed/pending run with a pending execution (scheduled) is preserved."""
    run = _run(status="delayed", assignment_id=None, age_seconds=99999, started=False)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "pending")),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "delayed"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_terminal_execution_desync_is_reconciled_immediately():
    """A dead-lettered execution whose run was never finalized is failed now —
    no age grace, because the engine already returned a terminal verdict."""
    run = _run(status="running", assignment_id=None, age_seconds=5)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "dead_letter")),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert "dead_letter" in run.error
    assert db.commits == 1


@pytest.mark.asyncio
async def test_completed_execution_desync_reconciles_run_to_completed():
    """When the underlying execution completed successfully but the sync hook
    was skipped, the run must be reconciled to ``completed`` — not
    force-failed.  Misreporting successful work as failed is the core bug."""
    run = _run(status="running", assignment_id=None, age_seconds=5)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "completed")),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "completed"
    assert run.completed_at is not None
    # No error must be set — the underlying work succeeded.
    assert run.error is None
    # The lost finalize write means the real summary is unrecoverable — the
    # sweep fills the Result card with an explicit placeholder instead of
    # leaving it blank.
    assert run.result_summary == autonomous.RECONCILED_COMPLETED_RESULT_SUMMARY
    assert db.commits == 1


@pytest.mark.asyncio
async def test_completed_execution_desync_clears_stale_error_and_keeps_summary():
    """A reconciled-to-completed run must never surface a leftover error
    string (RunCard renders ``run.error`` in red regardless of status), and an
    already-present partial summary is preserved — the placeholder only fills
    a blank Result card."""
    run = _run(status="running", assignment_id=None, age_seconds=5)
    run.error = "leftover error from an earlier attempt"
    run.result_summary = "partial summary persisted before the desync"
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "completed")),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "completed"
    assert run.error is None
    assert run.result_summary == "partial summary persisted before the desync"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_failed_execution_desync_reconciles_run_to_failed():
    """A ``failed`` execution whose sync hook was skipped reconciles the run
    to ``failed`` (distinct from ``dead_letter`` but same outcome)."""
    run = _run(status="running", assignment_id=None, age_seconds=5)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "failed")),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert run.error is not None
    assert "failed" in run.error
    assert db.commits == 1


@pytest.mark.asyncio
async def test_cancelled_execution_desync_reconciles_run_to_failed():
    """A ``cancelled`` execution (also a terminal failure outcome) reconciles
    the run to ``failed``."""
    run = _run(status="running", assignment_id=None, age_seconds=5)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "cancelled")),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert "cancelled" in run.error
    assert db.commits == 1


@pytest.mark.asyncio
async def test_completed_desync_logs_info_not_warning(caplog):
    """Reconciling a desync-completed run emits an INFO log (not WARNING) so
    monitoring systems do not alert on successful work."""
    import logging

    run = _run(status="running", assignment_id=None, age_seconds=5)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "completed")),
        ]
    )

    with caplog.at_level(logging.DEBUG, logger="app.services.autonomous"):
        await autonomous._cleanup_stale_assignment_runs(db)

    info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(str(run.id) in r.message for r in info_msgs), (
        "Expected an INFO log containing the run id for a completed desync"
    )
    # Must not produce a WARNING for a successful run.
    warn_msgs = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and str(run.id) in r.message
    ]
    assert not warn_msgs, (
        "A completed desync must not produce a WARNING-level log"
    )


@pytest.mark.asyncio
async def test_assignment_timeout_drives_the_deadline(monkeypatch):
    """A run carrying an assignment uses ``timeout + grace`` (not the fallback)
    as its stale deadline."""
    monkeypatch.setattr(autonomous.settings, "orchestration_grace_seconds", 120)
    assignment_id = uuid.uuid4()
    # 600s old; timeout 300 + grace 120 = 420s deadline -> stale.
    run = _run(status="running", assignment_id=assignment_id, age_seconds=600)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),  # no execution row
            _delegation_rows(),  # no live sub-agent delegation
            _RowsResult([(assignment_id, 300)]),  # assignment timeout_seconds
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_assignment_run_within_timeout_is_kept(monkeypatch):
    """A run younger than ``timeout + grace`` is still in-flight; keep it."""
    monkeypatch.setattr(autonomous.settings, "orchestration_grace_seconds", 120)
    assignment_id = uuid.uuid4()
    # 200s old; deadline 420s -> not stale.
    run = _run(status="running", assignment_id=assignment_id, age_seconds=200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
            _RowsResult([(assignment_id, 300)]),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_no_candidates_short_circuits():
    """Empty candidate set issues a single query and never commits."""
    db = _FakeDB(results=[_ScalarsResult([])])

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert db.commits == 0
    assert len(db.statements) == 1


def test_active_statuses_exclude_awaiting_human():
    """HITL ``awaiting_human`` is an intentional indefinite wait — it must never
    be in the sweep's active-status set."""
    assert autonomous.STALE_ASSIGNMENT_ACTIVE_STATUSES == (
        "running",
        "pending",
        "delayed",
    )
    assert "awaiting_human" not in autonomous.STALE_ASSIGNMENT_ACTIVE_STATUSES


@pytest.mark.asyncio
async def test_candidate_query_filters_to_active_statuses():
    """The candidate query restricts to the active statuses (and not HITL)."""
    db = _FakeDB(results=[_ScalarsResult([])])

    await autonomous._cleanup_stale_assignment_runs(db)

    compiled = db.statements[0].compile(compile_kwargs={"literal_binds": True})
    sql = " ".join(str(compiled).split()).lower()
    assert "awaiting_human" not in sql
    for active in ("running", "pending", "delayed"):
        assert f"'{active}'" in sql


# ---------------------------------------------------------------------------
# Early-warning logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphaned_run_in_warn_zone_emits_warning_and_is_not_failed(
    monkeypatch, caplog
):
    """A run older than warn_seconds but younger than the stale deadline emits
    a WARNING and is NOT failed — early alert without behavioural change."""
    import logging

    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 600)
    # Disable the shorter in-process threshold so this test exercises the
    # global fallback (stale_assignment_run_seconds=1800).
    monkeypatch.setattr(autonomous.settings, "stale_in_process_run_seconds", 0)
    # 900s old: past the 600s warn threshold, but before the 1800s stale deadline.
    run = _run(status="running", assignment_id=None, age_seconds=900)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),  # no execution
            _delegation_rows(),  # no live sub-agent delegation
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert run.completed_at is None
    assert db.commits == 0
    # An early warning must have been emitted.
    assert any("has no orchestration execution" in r.message for r in caplog.records)
    assert any(str(run.id) in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_run_below_warn_threshold_emits_no_warning(monkeypatch, caplog):
    """A run younger than warn_seconds produces no log output."""
    import logging

    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 600)
    # 120s old: well below both thresholds.
    run = _run(status="running", assignment_id=None, age_seconds=120)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert not caplog.records


@pytest.mark.asyncio
async def test_warn_threshold_zero_disables_early_warning(monkeypatch, caplog):
    """Setting stale_assignment_warn_seconds=0 suppresses early-warning logs."""
    import logging

    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 0)
    # Disable the shorter in-process threshold so the deadline is 1800s.
    monkeypatch.setattr(autonomous.settings, "stale_in_process_run_seconds", 0)
    # 900s old: would trigger the warning if warn_seconds > 0.
    run = _run(status="running", assignment_id=None, age_seconds=900)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert not caplog.records


@pytest.mark.asyncio
async def test_stale_run_past_deadline_is_failed_not_warned(monkeypatch, caplog):
    """A run past the stale deadline is failed (warning logged at failure, not
    as an early warning)."""
    import logging

    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 600)
    # 3600s old: well past the 1800s stale deadline.
    run = _run(status="running", assignment_id=None, age_seconds=3600)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert db.commits == 1
    # The failure warning (not the early-warning) must be present.
    assert any("Stale assignment run" in r.message for r in caplog.records)
    # The early-warning message must NOT appear (run was failed, not warned).
    assert not any(
        "has no orchestration execution" in r.message
        and "will be force-failed" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Live background-delegation child runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_delegation_child_run_is_not_warned(monkeypatch, caplog):
    """A delegation child run backed by a live ``sub_agent`` execution is
    skipped — no 'orphaned by a restart or crash' warning, even in the warn
    zone. The child run has no ``assignment_run:<id>`` execution row (its
    execution is deduped on ``sub_agent_task:<task_id>``), which is exactly
    the misclassification that flooded production logs."""
    import logging

    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 600)
    # 900s old: in the warn zone for a true orphan.
    run = _run(status="running", assignment_id=None, age_seconds=900)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),  # no assignment_run:* execution
            _delegation_rows(run),  # ...but a live sub_agent execution owns it
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert db.commits == 0
    assert not caplog.records


@pytest.mark.asyncio
async def test_live_delegation_child_run_past_stale_deadline_is_not_failed(
    monkeypatch,
):
    """A delegation child run past the fallback stale deadline is still NOT
    force-failed while its ``sub_agent`` execution is live — a
    task can inherit an assignment budget above the fallback, so killing it
    at 1800s would fail legitimately running work mid-flight."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    run = _run(status="running", assignment_id=None, age_seconds=7200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(run),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert run.completed_at is None
    assert db.commits == 0


@pytest.mark.asyncio
async def test_dead_delegation_child_run_is_still_failed(monkeypatch):
    """A delegation child run whose sub-agent execution is gone (not returned
    by the live-delegation probe) keeps the orphan contract: failed once past
    the stale deadline."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    run = _run(status="running", assignment_id=None, age_seconds=7200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),  # probe finds no live sub_agent execution
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_delegation_probe_is_skipped_when_all_runs_have_executions():
    """When every candidate resolves through its ``assignment_run:<id>`` key,
    the sweep must not issue the extra sub-agent payload query."""
    run = _run(status="running", assignment_id=None, age_seconds=99999)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "running")),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    # candidates + dedup-key probe only — no third query.
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_delegation_probe_query_filters_live_sub_agent_executions():
    """The payload probe is scoped to the ``sub_agent`` adapter and live
    (pending/running) states so a terminal execution can never suppress the
    orphan handling."""
    run = _run(status="running", assignment_id=None, age_seconds=120)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    await autonomous._cleanup_stale_assignment_runs(db)

    compiled = db.statements[2].compile(compile_kwargs={"literal_binds": True})
    sql = " ".join(str(compiled).split()).lower()
    assert "sub_agent" in sql
    assert "assignment_run_id" in sql
    assert "'pending'" in sql
    assert "'running'" in sql
    assert "'completed'" not in sql


# ---------------------------------------------------------------------------
# Live in-flight agent_chat turns
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal Redis stub for the chat:processing liveness probe."""

    def __init__(self, live_keys: set[str] | None = None):
        self._live_keys: set[str] = live_keys or set()
        self.calls: list[list[str]] = []

    async def mget(self, *keys: str):
        self.calls.append(list(keys))
        return ["1" if k in self._live_keys else None for k in keys]


def _make_redis_patcher(fake: _FakeRedis | None = None, raise_exc: Exception | None = None):
    """Return an async function suitable for patching ``app.core.redis.redis_client``."""

    async def _client():
        if raise_exc is not None:
            raise raise_exc
        return fake

    return _client


@pytest.mark.asyncio
async def test_live_agent_chat_run_in_warn_zone_is_not_warned(monkeypatch, caplog):
    """A chat-tracing run backed by a live chat:processing marker is silently
    skipped — no 'orphaned by a restart or crash' warning even in the warn
    zone. Mirrors the test for delegation runs."""
    import logging

    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 600)
    conv_id = str(uuid.uuid4())
    # 900s old: in the warn zone for a true orphan.
    run = _run(
        status="running",
        assignment_id=None,
        age_seconds=900,
        trigger_payload={"conversation_id": conv_id},
    )
    fake_redis = _FakeRedis(live_keys={f"chat:processing:{conv_id}"})
    import app.core.redis as _redis_mod

    monkeypatch.setattr(_redis_mod, "redis_client", _make_redis_patcher(fake=fake_redis))
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),  # no assignment_run:* execution
            _delegation_rows(),  # no live sub_agent execution either
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert db.commits == 0
    assert not caplog.records
    # Redis was probed for the conversation's processing marker.
    assert fake_redis.calls and f"chat:processing:{conv_id}" in fake_redis.calls[0]


@pytest.mark.asyncio
async def test_live_agent_chat_run_past_stale_deadline_is_not_failed(monkeypatch):
    """A chat-tracing run past the fallback stale deadline is still NOT
    force-failed while its chat:processing Redis marker is alive."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    conv_id = str(uuid.uuid4())
    run = _run(
        status="running",
        assignment_id=None,
        age_seconds=7200,
        trigger_payload={"conversation_id": conv_id},
    )
    fake_redis = _FakeRedis(live_keys={f"chat:processing:{conv_id}"})
    import app.core.redis as _redis_mod

    monkeypatch.setattr(_redis_mod, "redis_client", _make_redis_patcher(fake=fake_redis))
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert run.completed_at is None
    assert db.commits == 0


@pytest.mark.asyncio
async def test_dead_agent_chat_run_without_processing_marker_is_failed(monkeypatch):
    """A chat-tracing run whose conversation has no live processing marker is a
    genuine orphan and must be failed once past the stale deadline."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    conv_id = str(uuid.uuid4())
    run = _run(
        status="running",
        assignment_id=None,
        age_seconds=7200,
        trigger_payload={"conversation_id": conv_id},
    )
    # No live key for this conversation.
    fake_redis = _FakeRedis(live_keys=set())
    import app.core.redis as _redis_mod

    monkeypatch.setattr(_redis_mod, "redis_client", _make_redis_patcher(fake=fake_redis))
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_agent_chat_redis_probe_failure_skips_runs_safely(monkeypatch, caplog):
    """When the Redis probe raises (transient outage), all chat-tracing runs with
    a conversation_id are conservatively skipped rather than force-failed — a
    spurious warning is far preferable to killing a live turn mid-flight."""
    import logging

    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    conv_id = str(uuid.uuid4())
    # Past the stale deadline — would be failed if the Redis probe succeeds and
    # returns no marker.  But the probe raises, so the run must be skipped.
    run = _run(
        status="running",
        assignment_id=None,
        age_seconds=7200,
        trigger_payload={"conversation_id": conv_id},
    )
    import app.core.redis as _redis_mod

    monkeypatch.setattr(
        _redis_mod,
        "redis_client",
        _make_redis_patcher(raise_exc=ConnectionError("Redis unavailable")),
    )
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.services.autonomous"):
        cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert db.commits == 0
    # A warning about the Redis failure must be emitted.
    assert any("Failed to probe Redis" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_agent_chat_run_without_conversation_id_falls_through_to_orphan(
    monkeypatch,
):
    """A run with no conversation_id in trigger_payload gets no chat-turn
    protection and is failed by the regular orphan logic once past the deadline."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    # trigger_payload has no conversation_id.
    run = _run(
        status="running",
        assignment_id=None,
        age_seconds=7200,
        trigger_payload={"trigger_type": "delegation"},
    )
    import app.core.redis as _redis_mod

    # Patch to detect whether Redis is probed at all (it must NOT be for this run).
    fake_redis = _FakeRedis(live_keys=set())
    monkeypatch.setattr(_redis_mod, "redis_client", _make_redis_patcher(fake=fake_redis))
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert db.commits == 1
    # Redis must NOT have been probed — no conversation_id means no chat marker.
    assert fake_redis.calls == []


@pytest.mark.asyncio
async def test_agent_chat_redis_probe_batches_multiple_conversations(monkeypatch):
    """Multiple probeless runs with different conversation_ids are probed in a
    single batched MGET — one live, one not."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    conv_live = str(uuid.uuid4())
    conv_dead = str(uuid.uuid4())
    run_live = _run(
        status="running",
        assignment_id=None,
        age_seconds=7200,
        trigger_payload={"conversation_id": conv_live},
    )
    run_dead = _run(
        status="running",
        assignment_id=None,
        age_seconds=7200,
        trigger_payload={"conversation_id": conv_dead},
    )
    fake_redis = _FakeRedis(live_keys={f"chat:processing:{conv_live}"})
    import app.core.redis as _redis_mod

    monkeypatch.setattr(_redis_mod, "redis_client", _make_redis_patcher(fake=fake_redis))
    db = _FakeDB(
        results=[
            _ScalarsResult([run_live, run_dead]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    # Only the dead-conversation run is failed; the live one is skipped.
    assert cleaned == 1
    assert run_live.status == "running"
    assert run_dead.status == "failed"
    assert db.commits == 1
    # Both conversations were probed in a single mget call.
    assert len(fake_redis.calls) == 1
    assert len(fake_redis.calls[0]) == 2


# ---------------------------------------------------------------------------
# fail_orphaned_in_process_runs — registry-scoped shutdown sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fail_orphaned_in_process_runs_fails_tracked_run():
    """A tracked active run with assignment_id=None is immediately failed."""
    run = _run(status="running", assignment_id=None, age_seconds=120)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),  # no orchestration execution
        ]
    )

    failed = await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)})

    assert failed == 1
    assert run.status == "failed"
    assert run.completed_at is not None
    assert run.error.startswith(autonomous.STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX)
    assert "shutdown" in run.error
    assert db.commits == 1


@pytest.mark.asyncio
async def test_fail_orphaned_in_process_runs_empty_registry_is_noop():
    """No tracked runs — the sweep never queries and never commits."""
    db = _FakeDB(results=[])

    assert await autonomous.fail_orphaned_in_process_runs(db, set()) == 0
    assert await autonomous.fail_orphaned_in_process_runs(db, None) == 0
    assert db.commits == 0
    assert db.statements == []


@pytest.mark.asyncio
async def test_fail_orphaned_in_process_runs_invalid_ids_are_noop():
    """A registry snapshot with only non-UUID junk never reaches the DB."""
    db = _FakeDB(results=[])

    failed = await autonomous.fail_orphaned_in_process_runs(db, {"not-a-uuid"})

    assert failed == 0
    assert db.statements == []


@pytest.mark.asyncio
async def test_fail_orphaned_in_process_runs_no_active_candidates():
    """Tracked runs already terminal in the DB short-circuit without commits."""
    db = _FakeDB(results=[_ScalarsResult([])])

    failed = await autonomous.fail_orphaned_in_process_runs(
        db, {str(uuid.uuid4())}
    )

    assert failed == 0
    assert db.commits == 0
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_fail_orphaned_in_process_runs_skips_live_execution():
    """A run backed by a live (non-terminal) execution is deferred to the reconciler."""
    run = _run(status="running", assignment_id=None, age_seconds=120)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "running")),  # engine still owns this run
        ]
    )

    failed = await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)})

    assert failed == 0
    assert run.status == "running"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_fail_orphaned_in_process_runs_reconciles_terminal_execution_desync():
    """A dead-lettered execution whose run was never finalized is also failed,
    and the stored error names the desync (with the execution state) rather
    than falsely claiming a shutdown interruption."""
    run = _run(status="running", assignment_id=None, age_seconds=5)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "dead_letter")),
        ]
    )

    failed = await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)})

    assert failed == 1
    assert run.status == "failed"
    assert "dead_letter" in run.error
    assert "interrupted by a platform shutdown" not in run.error
    assert db.commits == 1


@pytest.mark.asyncio
async def test_fail_orphaned_in_process_runs_completed_desync_is_completed():
    """The shutdown sweep applies the same outcome mapping as the periodic
    sweep: a ``completed`` execution desync reconciles the run to
    ``completed`` with no error, never a force-fail."""
    run = _run(status="running", assignment_id=None, age_seconds=5)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "completed")),
        ]
    )

    finalized = await autonomous.fail_orphaned_in_process_runs(db, {str(run.id)})

    assert finalized == 1
    assert run.status == "completed"
    assert run.completed_at is not None
    assert run.error is None
    assert run.result_summary == autonomous.RECONCILED_COMPLETED_RESULT_SUMMARY
    assert db.commits == 1


@pytest.mark.asyncio
async def test_fail_orphaned_in_process_runs_query_is_scoped_to_registry():
    """The candidate query filters on the tracked ids AND assignment_id IS NULL.

    The id scoping is the multi-replica safety contract: another replica's
    live chat runs (and worker-managed delegation child runs) must never be
    swept by this process's shutdown.
    """
    tracked_id = uuid.uuid4()
    db = _FakeDB(results=[_ScalarsResult([])])

    await autonomous.fail_orphaned_in_process_runs(db, {str(tracked_id)})

    compiled = db.statements[0].compile(compile_kwargs={"literal_binds": True})
    sql = " ".join(str(compiled).split()).lower()
    assert "assignment_id is null" in sql
    # UUID literals compile to the undashed hex form.
    assert tracked_id.hex in sql


# ---------------------------------------------------------------------------
# in_process_runs — process-local registry
# ---------------------------------------------------------------------------


def test_in_process_run_registry_track_untrack():
    from app.services import in_process_runs

    run_id = uuid.uuid4()
    in_process_runs.track_in_process_run(run_id)
    assert str(run_id) in in_process_runs.get_tracked_in_process_runs()

    # The snapshot is a copy — mutating it never corrupts the registry.
    in_process_runs.get_tracked_in_process_runs().clear()
    assert str(run_id) in in_process_runs.get_tracked_in_process_runs()

    in_process_runs.untrack_in_process_run(str(run_id))
    assert str(run_id) not in in_process_runs.get_tracked_in_process_runs()

    # Untracking an unknown id is a no-op, never a KeyError.
    in_process_runs.untrack_in_process_run(uuid.uuid4())


# ---------------------------------------------------------------------------
# update_assignment_run — registry untrack happens only after the update applies
# ---------------------------------------------------------------------------


class _ScalarOneResult:
    """Result whose ``.scalar_one_or_none()`` returns the seeded row."""

    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _RaisingDB:
    """Session whose ``execute`` always raises (simulated DB outage)."""

    async def execute(self, stmt):
        raise RuntimeError("simulated DB failure")


@pytest.mark.asyncio
async def test_update_assignment_run_untracks_after_terminal_update():
    """A found run marked terminal is untracked from the process registry."""
    from app.services import in_process_runs, sub_agent

    run = _run(status="running", assignment_id=None, age_seconds=5)
    in_process_runs.track_in_process_run(run.id)

    db = _FakeDB(results=[_ScalarOneResult(run)])
    await sub_agent.update_assignment_run(db, str(run.id), "completed")

    assert run.status == "completed"
    assert str(run.id) not in in_process_runs.get_tracked_in_process_runs()


@pytest.mark.asyncio
async def test_update_assignment_run_keeps_tracking_when_lookup_fails():
    """A failed SELECT must NOT untrack — the run is still active in the DB.

    Untracking before the update applied would hide the run from the
    shutdown sweep and push it back to the periodic stale sweep's
    ``stale_assignment_run_seconds`` fallback (default 30 min).
    """
    from app.services import in_process_runs, sub_agent

    run_id = uuid.uuid4()
    in_process_runs.track_in_process_run(run_id)
    try:
        await sub_agent.update_assignment_run(_RaisingDB(), str(run_id), "failed")
        assert str(run_id) in in_process_runs.get_tracked_in_process_runs()
    finally:
        in_process_runs.untrack_in_process_run(run_id)


@pytest.mark.asyncio
async def test_update_assignment_run_keeps_tracking_when_run_missing():
    """A lookup miss (run not found) must NOT untrack the registry entry."""
    from app.services import in_process_runs, sub_agent

    run_id = uuid.uuid4()
    in_process_runs.track_in_process_run(run_id)
    try:
        db = _FakeDB(results=[_ScalarOneResult(None)])
        await sub_agent.update_assignment_run(db, str(run_id), "completed")
        assert str(run_id) in in_process_runs.get_tracked_in_process_runs()
    finally:
        in_process_runs.untrack_in_process_run(run_id)


# ── In-process shorter threshold ────────────────────────────────


@pytest.mark.asyncio
async def test_in_process_run_uses_shorter_in_process_threshold(monkeypatch):
    """Runs with assignment_id=None use stale_in_process_run_seconds (default
    900s) as their deadline rather than the global stale_assignment_run_seconds
    (default 1800s) so the warning-flood window is shorter after a pod crash."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 600)
    monkeypatch.setattr(autonomous.settings, "stale_in_process_run_seconds", 900)
    # 950s old: past the in-process 900s threshold but under the global 1800s.
    run = _run(status="running", assignment_id=None, age_seconds=950)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    # Must be force-failed because 950 > 900 (in-process threshold).
    assert cleaned == 1
    assert run.status == "failed"
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_regular_assignment_run_uses_global_threshold(monkeypatch):
    """Runs with assignment_id set must NOT be affected by stale_in_process_run_seconds."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 0)
    monkeypatch.setattr(autonomous.settings, "stale_in_process_run_seconds", 900)
    # 950s old: would be stale under the in-process threshold but NOT under 1800s.
    aid = uuid.uuid4()
    run = _run(status="running", assignment_id=aid, age_seconds=950)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),  # no orchestration execution
            _delegation_rows(),  # no live sub-agent delegation
            _RowsResult([(aid, None)]),  # assignment timeout query (no timeout set)
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    # Must NOT be force-failed: the global 1800s threshold applies.
    assert cleaned == 0
    assert run.status == "running"


@pytest.mark.asyncio
async def test_stale_in_process_run_seconds_zero_falls_back_to_global(monkeypatch):
    """Setting stale_in_process_run_seconds=0 disables the tighter threshold
    and falls back to stale_assignment_run_seconds for all orphaned runs."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 0)
    monkeypatch.setattr(autonomous.settings, "stale_in_process_run_seconds", 0)
    # 950s old: stale under 900s but NOT under the global 1800s fallback.
    run = _run(status="running", assignment_id=None, age_seconds=950)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    # Must NOT be force-failed when the tighter threshold is disabled.
    assert cleaned == 0
    assert run.status == "running"


# ── Prometheus counters ───────────────────────────


def _warned_total(orphan_class: str) -> float:
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value(
            "xtmone_stale_run_warnings_total", {"orphan_class": orphan_class}
        )
        or 0.0
    )


def _finalized_total(outcome: str, orphan_class: str) -> float:
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value(
            "xtmone_stale_runs_finalized_total",
            {"outcome": outcome, "orphan_class": orphan_class},
        )
        or 0.0
    )


@pytest.mark.asyncio
async def test_force_failed_orphan_increments_finalized_counter(monkeypatch):
    """Force-failing a no-execution orphan increments
    xtmone_stale_runs_finalized_total{outcome=failed, orphan_class=no_execution}."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    run = _run(status="running", assignment_id=None, age_seconds=7200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )
    before = _finalized_total("failed", "no_execution")

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert _finalized_total("failed", "no_execution") == before + 1


@pytest.mark.asyncio
async def test_completed_desync_increments_finalized_counter_with_desync_class():
    """Reconciling a completed-execution desync increments the finalized
    counter under {outcome=completed, orphan_class=terminal_desync}."""
    run = _run(status="running", assignment_id=None, age_seconds=5)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "completed")),
        ]
    )
    before = _finalized_total("completed", "terminal_desync")

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert _finalized_total("completed", "terminal_desync") == before + 1


@pytest.mark.asyncio
async def test_warn_zone_orphan_increments_warning_counter(monkeypatch):
    """An early warning for an approaching-deadline orphan increments
    xtmone_stale_run_warnings_total{orphan_class=no_execution}."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_assignment_warn_seconds", 600)
    monkeypatch.setattr(autonomous.settings, "stale_in_process_run_seconds", 0)
    # 900s old: in the warn zone (600 < 900 < 1800).
    run = _run(status="running", assignment_id=None, age_seconds=900)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )
    before = _warned_total("no_execution")

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert _warned_total("no_execution") == before + 1


# --- External A2A runs ----------------------------------------------
# These look exactly like an orphan in the DB: assignment_id IS NULL, no
# orchestration execution, and no conversation_id for the Redis probe to find.
# They are not orphans - a2a.tasks.reap_stale_tasks owns them, and uniquely
# among the classes this sweep sees they carry a real per-run heartbeat. Without
# the lease probe a partner's long-running task was force-failed mid-flight with
# "orphaned by a restart or crash", and the executor's own finalize - conditional
# on the run still being 'running' - then matched nothing, so the real result was
# silently discarded and the partner polled a FAILED task for completed work.


def _a2a_run(*, age_seconds=7200, status="running"):
    return _run(
        status=status,
        assignment_id=None,
        age_seconds=age_seconds,
        trigger_payload={"a2a_message_id": str(uuid.uuid4()), "message": "hi"},
        trigger_context={"source": "a2a", "client_id": "a2a-abc"},
    )


def _lease_rows(*runs):
    """The fresh-lease probe result: one row per run still holding its lease."""
    return _RowsResult([(run.id,) for run in runs])


@pytest.mark.asyncio
async def test_leased_a2a_run_is_not_failed_mid_flight(monkeypatch):
    """Well past the generic deadline, but heartbeating - so left alone."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_in_process_run_seconds", 900)
    monkeypatch.setattr(autonomous.settings, "a2a_run_lease_seconds", 900)
    run = _a2a_run(age_seconds=7200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
            _lease_rows(run),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
    assert run.error is None
    assert db.commits == 0


@pytest.mark.asyncio
async def test_a2a_run_with_an_expired_lease_is_still_failed(monkeypatch):
    """The gate is a FRESH lease, not "is an A2A run".

    If the task row is gone (its exposure cascade-deleted with the agent) or the
    lease genuinely expired without its own reaper clearing it, this sweep stays
    the backstop - otherwise skipping would make such a run un-reapable forever.
    """
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    monkeypatch.setattr(autonomous.settings, "stale_in_process_run_seconds", 900)
    monkeypatch.setattr(autonomous.settings, "a2a_run_lease_seconds", 900)
    run = _a2a_run(age_seconds=7200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
            _lease_rows(),  # no fresh lease for this run
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"
    assert autonomous.STALE_ASSIGNMENT_CLEANUP_ERROR_PREFIX in run.error


@pytest.mark.asyncio
async def test_lease_probe_is_skipped_for_non_a2a_runs(monkeypatch):
    """No extra query on the ordinary path.

    ``_FakeDB`` raises on an unexpected execute(), so seeding exactly the three
    pre-existing results is what proves the lease probe is not issued for a run
    that is not external.
    """
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    run = _run(status="running", assignment_id=None, age_seconds=7200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows(),
            _delegation_rows(),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 1
    assert run.status == "failed"


@pytest.mark.asyncio
async def test_a2a_run_with_a_live_execution_still_defers_to_the_reconciler(
    monkeypatch,
):
    """The pre-existing skip wins first: a live execution means the engine owns
    the run, so the lease probe is never even reached for it."""
    monkeypatch.setattr(autonomous.settings, "stale_assignment_run_seconds", 1800)
    run = _a2a_run(age_seconds=7200)
    db = _FakeDB(
        results=[
            _ScalarsResult([run]),
            _exec_rows((run, "running")),
        ]
    )

    cleaned = await autonomous._cleanup_stale_assignment_runs(db)

    assert cleaned == 0
    assert run.status == "running"
