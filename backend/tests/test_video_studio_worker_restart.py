"""A render its worker left resumes first, says why, and is never left running. Synthetic rows.

* a worker asked to stop drains: it claims no execution and drives no outbox
  event, its periodic sweeps still run, and its presence says it drains;
* a render handed back goes first in its adapter's queue, due now, and the
  workers are woken for it - never waiting for the next job queued;
* the worker that resumes a render says whether it was a deploy (the
  stopping worker's version is another) or a restart, or how a worker gone
  went;
* a render process the out-of-memory killer killed says so, with the
  container's memory at the time;
* ``get_video_jobs`` says a resuming render waits first in the queue, how
  long it has, and the lease and heartbeat of a running one;
* a render handed back waits as long as its deployment's rolling restart
  takes - the termination grace plus a new worker's start, 10 min
  when the deployment says neither, stamped by the stopping worker -, and for
  as long as a job holds a slot of its lane; one no worker resumed is failed
  naming the workers alive and why none took it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.core.config import settings
from app.models.orchestration import OrchestrationExecution
from app.models.video_studio import JOB_RUNNING, VideoRender
from app.services import orchestration_engine as engine
from app.services import orchestration_reconciler as reconciler
from app.services import worker_presence
from app.services.orchestration_adapters import video_studio as adapters
from app.services.orchestration_adapters.base import AdapterResult
from app.services.video_studio import jobs
from app.services.video_studio.render_process import RenderExit


class _Db:
    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Ctx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_exc):
        return False


async def test_a_worker_asked_to_stop_claims_nothing_new_and_says_it_drains(monkeypatch):
    seen: dict[str, list] = {"claimed": [], "outbox": [], "periodic": [], "presence": []}

    async def empty(*_args, **_kwargs):
        return []

    async def claim_due(_db, **kwargs):
        seen["claimed"].append(kwargs)
        return []

    async def drive_outbox(worker_id):
        seen["outbox"].append(worker_id)

    async def publish(worker_id, *, state, **_kwargs):
        seen["presence"].append(state)

    class _Render:
        name = "video_render"
        resume_when_worker_gone = False

        async def periodic(self, _ctx):
            seen["periodic"].append(True)

    monkeypatch.setattr(reconciler, "async_session", lambda: _Ctx(_Db()))
    monkeypatch.setattr(engine, "sweep_deadlines", empty)
    monkeypatch.setattr(engine, "reclaim_expired_leases", empty)
    monkeypatch.setattr(engine, "claim_due", claim_due)
    monkeypatch.setattr(reconciler, "_drive_outbox", drive_outbox)
    monkeypatch.setattr(reconciler, "registered_adapters", lambda: [_Render()])
    monkeypatch.setattr(worker_presence, "publish", publish)
    draining = asyncio.Event()
    monkeypatch.setattr(reconciler, "_draining", draining)
    await reconciler._run_cycle("w1")
    assert seen["presence"] == ["running"] and seen["outbox"] == ["w1"] and len(seen["claimed"]) == 1
    seen["outbox"].clear()
    seen["claimed"].clear()
    # Asked to stop: it drains.
    draining.set()
    await reconciler._run_cycle("w1")
    assert seen["claimed"] == [] and seen["outbox"] == [] and seen["periodic"] == [True, True]
    assert seen["presence"] == ["running", "draining"]


def _held(**fields) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=uuid.uuid4(),
        adapter="video_render",
        state="running",
        payload={"render_id": str(uuid.uuid4())},
        attempt_count=1,
        max_attempts=3,
        run_at=now - timedelta(minutes=10),
        deadline_at=now + timedelta(hours=4),
        adapter_state=None,
        last_error=None,
        lease_owner="old:1",
        lease_expires_at=now + timedelta(seconds=150),
        updated_at=now,
        **fields,
    )


class _Earliest:
    """The session ``head_of_queue`` reads the earliest pending execution on."""

    def __init__(self, earliest: datetime | None) -> None:
        self.earliest = earliest

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.earliest)

    async def flush(self):
        return None


async def test_a_render_handed_back_goes_first_and_wakes_the_workers(monkeypatch):
    execution = _held()
    queued = datetime.now(UTC) - timedelta(minutes=7)
    monkeypatch.setattr(engine, "_locked_get", AsyncMock(return_value=execution))
    before = datetime.now(UTC)
    await engine.defer(
        _Earliest(queued),
        execution_id=execution.id,
        worker_id="old:1",
        reason="Handed over",
        delay_seconds=0.0,
        adapter_state=jobs.HANDED_OVER_STATE,
        place="head",
    )
    # First in its queue: before the earliest pending one, its deadline counted from now.
    assert execution.state == "pending" and execution.run_at == queued - timedelta(seconds=1)
    assert execution.deadline_at > before + timedelta(hours=3)
    assert execution.lease_owner is None and execution.attempt_count == 0
    # Alone in the queue: due now.
    alone = _held()
    monkeypatch.setattr(engine, "_locked_get", AsyncMock(return_value=alone))
    await engine.defer(
        _Earliest(None), execution_id=alone.id, worker_id="old:1", reason="r", delay_seconds=5, adapter_state="s", place="head"
    )
    assert before <= alone.run_at <= datetime.now(UTC)
    # Applied by the reconciler, the workers are woken at once.
    woken = AsyncMock()
    monkeypatch.setattr(engine, "notify_orchestration_wake", woken)
    monkeypatch.setattr(engine, "defer", AsyncMock())
    result = AdapterResult.defer("Handed over", delay_seconds=0.0, adapter_state=jobs.HANDED_OVER_STATE, place="head")
    await reconciler._apply_result(_Db(), execution.id, "old:1", result)
    woken.assert_awaited_once()
    # A preview waiting its turn is not: it is claimed again later, in its place.
    woken.reset_mock()
    waits = AdapterResult.defer("Waiting", delay_seconds=15.0, adapter_state=jobs.PREVIEW_WAITING_STATE, place="keep")
    await reconciler._apply_result(_Db(), execution.id, "old:1", waits)
    woken.assert_not_awaited()


async def test_a_preview_waiting_its_turn_keeps_its_place(monkeypatch):
    execution = _held()
    place = execution.run_at
    monkeypatch.setattr(engine, "_locked_get", AsyncMock(return_value=execution))
    await engine.defer(
        _Earliest(None),
        execution_id=execution.id,
        worker_id="old:1",
        reason="Waiting",
        delay_seconds=15.0,
        adapter_state=jobs.PREVIEW_WAITING_STATE,
        place="keep",
    )
    assert execution.run_at == place
    not_before = datetime.fromisoformat(execution.payload[engine.NOT_BEFORE_PAYLOAD_KEY])
    assert timedelta(seconds=14) < not_before - datetime.now(UTC) <= timedelta(seconds=15)


def test_the_resumed_render_says_whether_it_was_a_deploy_or_a_restart(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.4.2")

    def handed(version: str) -> SimpleNamespace:
        payload = {"render_id": "r", adapters.HANDED_OVER_KEY: {"worker": "old:1", "version": version}}
        return SimpleNamespace(payload=payload, adapter_state=jobs.HANDED_OVER_STATE, attempt_count=1)

    assert adapters.resumed_after(handed("1.4.1")) == "after a deploy (1.4.1 -> 1.4.2)"
    assert adapters.resumed_after(handed("1.4.2")) == "after its worker restarted (version 1.4.2)"
    gone = SimpleNamespace(
        payload={
            engine.LEASE_EXPIRY_PAYLOAD_KEY: {
                "attempt": 1,
                "why": "Its worker on worker-0 (version 1.4.2) went without a stop request: most likely killed",
                "owner_record": {"version": "1.4.2"},
            }
        },
        adapter_state=engine.OWNER_GONE_STATE,
        attempt_count=2,
    )
    assert adapters.resumed_after(gone) == (
        "after its worker restarted (version 1.4.2): its worker on worker-0 (version 1.4.2) went without a stop "
        "request: most likely killed"
    )
    # A render no worker left says nothing of the kind.
    assert adapters.resumed_after(SimpleNamespace(payload={}, adapter_state=None, attempt_count=1)) is None


def test_a_render_process_the_out_of_memory_killer_killed_says_so_with_the_memory():
    ended = RenderExit(
        kind="crashed",
        returncode=-9,
        peak_memory=3 << 30,
        progress=34,
        stage="Drawing frames (29%)",
        container_memory=int(3.9 * (1 << 30)),
        container_limit=4 << 30,
        oom_killed=True,
    )
    said = ended.describe(ceiling=None, stall_seconds=120)
    assert "was killed by the kernel's out-of-memory killer (signal 9" in said
    assert "the container used 3.9 GiB of its 4.0 GiB limit (97%) then" in said
    # The same signal sent by hand is not the out-of-memory killer's.
    assert "out-of-memory" not in RenderExit(kind="crashed", returncode=-9, progress=34).describe(ceiling=None, stall_seconds=1)


async def test_get_video_jobs_says_a_resuming_render_waits_first_in_the_queue(monkeypatch):
    now = datetime.now(UTC)
    execution = OrchestrationExecution(
        id=uuid.uuid4(),
        adapter="video_render",
        state="pending",
        adapter_state=jobs.HANDED_OVER_STATE,
        payload={jobs.HANDED_OVER_KEY: {"worker": "old:1", "resume_wait_seconds": 600}},
        run_at=now,
        attempt_count=1,
        max_attempts=3,
        last_error="Handed to the next worker at 38%: its worker was asked to stop (SIGTERM).",
        updated_at=now - timedelta(seconds=40),
    )
    render = VideoRender(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        revision=1,
        quality="high",
        status=JOB_RUNNING,
        progress=38,
        stage="Handed to the next worker at 38%",
        progress_at=now - timedelta(seconds=40),
        execution_id=execution.id,
    )

    lane: list[tuple[str, int]] = []

    class _Session:
        async def execute(self, _statement):
            # The executions of the rows asked, then the renders running in the lane a resumed one waits in.
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [execution]), all=lambda: list(lane))

    monkeypatch.setattr(jobs, "async_session", lambda: _Ctx(_Session()))
    [state] = await jobs.job_states("render", [render])
    assert state.status == jobs.STATE_RUNNING and state.lease is None
    told = state.as_dict(now)["resume"]
    assert told["how"] == "handed_over" and 39 <= told["waiting_seconds"] <= 41
    # The wait its stopping worker stamped: 10 min, not 3.
    assert told["wait_seconds"] == 600 and 559 <= told["fails_in_seconds"] <= 561
    said = jobs.lease_clause(state, now)
    assert said.startswith("; handed back by its stopping worker 40 s ago (Handed to the next worker at 38%")
    assert "first in the queue for the next worker - it fails if no worker resumes it in 560 s" in said


# ── A render handed back waits as long as its deployment restarts ─────


def test_a_resumed_render_waits_as_long_as_its_deployment_takes_to_restart(monkeypatch):
    # A deployment that says nothing: Kubernetes' 30 s grace and 570 s for a new worker to start.
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 0)
    monkeypatch.setattr(settings, "worker_startup_seconds", 0)
    assert jobs.resume_wait() == timedelta(minutes=10)
    # Its pod's termination grace plus its startup probe.
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 120)
    monkeypatch.setattr(settings, "worker_startup_seconds", 300)
    assert jobs.resume_wait() == timedelta(minutes=7)
    # One given, the other at its default.
    monkeypatch.setattr(settings, "worker_startup_seconds", 0)
    assert jobs.resume_wait() == timedelta(seconds=120 + jobs.DEFAULT_WORKER_STARTUP_SECONDS)
    # Never less than the 3 min a live worker needs at most.
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 5)
    monkeypatch.setattr(settings, "worker_startup_seconds", 10)
    assert jobs.resume_wait() == jobs.MIN_RESUME_WAIT


def test_the_stopping_worker_stamps_its_own_deployments_wait_on_the_render(monkeypatch):
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 300)
    monkeypatch.setattr(settings, "worker_startup_seconds", 600)
    handed = adapters.handed_over_by("old:1")[jobs.HANDED_OVER_KEY]
    assert handed["worker"] == "old:1" and handed["resume_wait_seconds"] == 900
    # The platform's tick reads what that worker stamped, whatever its own settings say.
    monkeypatch.setattr(settings, "worker_termination_grace_seconds", 0)
    monkeypatch.setattr(settings, "worker_startup_seconds", 0)
    stamped = SimpleNamespace(payload={jobs.HANDED_OVER_KEY: handed})
    assert jobs.waits_for(stamped) == timedelta(minutes=15)
    # A render taken back from a worker gone, or handed back by an older release: this process's wait.
    assert jobs.waits_for(SimpleNamespace(payload={})) == timedelta(minutes=10)
    assert jobs.waits_for(SimpleNamespace(payload={jobs.HANDED_OVER_KEY: {"resume_wait_seconds": True}})) == timedelta(
        minutes=10
    )


def test_a_resumed_render_waits_while_a_job_holds_a_slot_of_its_lane():
    now = datetime.now(UTC)
    execution = SimpleNamespace(
        state="pending",
        adapter="video_render",
        adapter_state=jobs.HANDED_OVER_STATE,
        payload={jobs.HANDED_OVER_KEY: {"resume_wait_seconds": 600}},
        last_error="Handed to the next worker at 62%",
        updated_at=now - timedelta(minutes=12),
    )
    render = SimpleNamespace(progress_at=now - timedelta(minutes=12))
    # A scene preview in a render slot lent to it holds that slot too; one on its own lane does not.
    assert jobs.holds_slot_of(jobs.RENDER_ADAPTER, jobs.PREVIEW_ADAPTER, jobs.RENDER_ADAPTER)
    assert not jobs.holds_slot_of(jobs.RENDER_ADAPTER, jobs.PREVIEW_ADAPTER)
    held = jobs.resume_of(execution, render, lane_running=1)
    assert held.grace_seconds(now) is None and held.as_dict(now)["fails_in_seconds"] is None
    said = held.deadline_clause(now)
    assert "it waits while 1 job holds a slot of its lane" in said and "fails if no worker resumes it within 10 min" in said
    # Its lane free and its wait over: the next check fails it.
    stale = jobs.resume_of(execution, render)
    assert stale.grace_seconds(now) == 0 and "it fails at the next check unless a worker resumes it" in stale.deadline_clause(now)
    # Its progress after the hand-over moves its clock.
    moved = jobs.resume_of(execution, SimpleNamespace(progress_at=now - timedelta(seconds=30)))
    assert 569 <= moved.grace_seconds(now) <= 571


async def test_a_render_no_worker_resumed_names_the_workers_alive_and_why_none_took_it(monkeypatch):
    present = [
        {"worker_id": "w1", "host": "worker-old", "version": "1.4.2", "state": "draining", "signal": "SIGTERM"},
        {"worker_id": "w2", "host": "worker-new", "version": "1.4.3", "state": "running"},
        {"worker_id": "w3", "host": "worker-two", "version": "1.4.3", "state": "running"},
    ]

    async def records(**_kwargs):
        return present

    monkeypatch.setattr(worker_presence, "present", records)
    workers = await jobs.workers_seen([("video_preview", "w3"), ("video_render", "w9")])
    assert workers == [
        "worker-old (version 1.4.2) was asked to stop (SIGTERM) and took no new job",
        "worker-new (version 1.4.3) was running with no render in flight, yet did not take it: see that worker's logs",
        "worker-two (version 1.4.3) was drawing 1 scene preview",
    ]
    resume = jobs.Resume(
        how="handed_over",
        since=datetime.now(UTC),
        reason="Handed to the next worker at 73%: its worker was asked to stop (SIGTERM).",
        wait=timedelta(minutes=10),
    )
    render = SimpleNamespace(progress=73, scene_id=None)
    said = jobs.unresumed_reason(render, resume, workers)
    assert said.startswith(
        "No worker resumed it in 10 min after it went back to the queue at 73% (handed to the next worker at 73%: its "
        "worker was asked to stop (SIGTERM)), while nothing held a slot of its lane. Workers alive then: worker-old"
    )
    assert said.endswith("it resumes from the segments it kept, as this same render.")
    assert "No worker was running then." in jobs.unresumed_reason(render, resume, [])
    assert "could not be read" in jobs.unresumed_reason(render, resume, None)
    # Redis unreadable: unknown, never "no worker".
    monkeypatch.setattr(worker_presence, "present", AsyncMock(return_value=None))
    assert await jobs.workers_seen([]) is None
