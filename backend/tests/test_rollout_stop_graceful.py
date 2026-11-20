"""A worker stopped by a rollout is a graceful stop, never a loss without a word.

- the take-back of workers gone runs before the expired-lease sweep and reads
  expired leases too, so a worker that said it was stopping is taken back on
  its word even when its lease ran out before the next worker started;
- a worker of an older release gone without a word, with no sign of an
  out-of-memory kill, was killed by its deploy: its work is taken back as from
  a worker that stopped on its word (attempt given back, graceful resume);
- a worker of this very release, or one at its memory limit, still spends it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services import orchestration_engine, orchestration_reconciler, worker_presence
from app.services.orchestration_adapters.sub_agent import _taken_back_on_its_word
from app.services.orchestration_engine import _take_back, stopped_on_its_word

_LAST_HEARD_AT = orchestration_engine.last_heard_at


def _record(**fields) -> dict:
    return {
        "worker_id": "worker-old",
        "host": "pod-old",
        "boot_id": "another-boot",
        "state": "running",
        "lapsed": True,
        "version": "1.0.0-old",
        "memory": 1 << 30,
        "memory_limit": 4 << 30,
        "at": (worker_presence._PROCESS_STARTED_AT + timedelta(seconds=5)).isoformat(),
        **fields,
    }


@pytest.fixture(autouse=True)
def _release(monkeypatch):
    monkeypatch.setattr(settings, "app_version", "1.0.1-new")
    monkeypatch.setattr(orchestration_engine, "head_of_queue", AsyncMock(return_value=datetime.now(UTC)))
    monkeypatch.setattr(orchestration_engine, "last_heard_at", lambda row: None)


def test_an_older_release_gone_without_a_word_was_replaced_by_its_deploy():
    assert worker_presence.replaced_by_deploy(_record())


@pytest.mark.parametrize(
    "fields",
    [
        {"version": "1.0.1-new"},  # this very release: a crash, or killed by its work
        {"version": None},
        {"memory": int(3.9 * (1 << 30))},  # at its memory limit: the out-of-memory killer
        {"state": "draining"},  # it said so: stopped on its word already
        {"state": "stopped"},
        # Last heard before this release came up: it died before the rollout.
        {"at": (worker_presence._PROCESS_STARTED_AT - timedelta(seconds=60)).isoformat()},
        {"at": None},
        {"at": "not a time"},
        # No memory telemetry: nothing rules the out-of-memory killer out.
        {"memory": None},
        {"memory_limit": None},
    ],
)
def test_a_crash_or_a_word_is_not_a_deploy_replacement(fields):
    assert not worker_presence.replaced_by_deploy(_record(**fields))


def test_an_out_of_memory_kill_counted_here_is_not_a_deploy_replacement(monkeypatch):
    from app.services.video_studio import resources

    me = worker_presence.identity()
    monkeypatch.setattr(resources, "oom_kills", lambda: 3)
    record = _record(host=me["host"], boot_id=me["boot_id"], oom_kills=2)

    assert not worker_presence.replaced_by_deploy(record)


async def test_gone_marks_the_worker_a_deploy_replaced(monkeypatch):
    replaced, crashed = _record(), _record(worker_id="worker-same", version="1.0.1-new")

    async def records(ids):
        return {"worker-old": replaced, "worker-same": crashed}

    monkeypatch.setattr(worker_presence, "records", records)

    found = await worker_presence.gone(["worker-old", "worker-same"], me="worker-new")

    assert found["worker-old"][worker_presence.REPLACED_BY_DEPLOY] is True
    assert worker_presence.REPLACED_BY_DEPLOY not in found["worker-same"]
    assert stopped_on_its_word(found["worker-old"]) and not stopped_on_its_word(found["worker-same"])
    assert "replaced by a deploy (1.0.0-old -> 1.0.1-new)" in worker_presence.stop_reason(found["worker-old"])


def _row(*, attempt_count: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), adapter="sub_agent", lease_owner="worker-old", lease_expires_at=datetime.now(UTC),
        adapter_state=None, updated_at=None, payload={}, attempt_count=attempt_count, max_attempts=4,
        state="running", run_at=None, last_error=None,
    )


async def test_the_work_of_a_worker_a_deploy_replaced_is_taken_back_as_a_graceful_stop():
    row = _row(attempt_count=4)
    record = {**_record(), worker_presence.REPLACED_BY_DEPLOY: True}

    dead = await _take_back(AsyncMock(), row, datetime.now(UTC), why="Its worker went", head=True, owner_record=record)

    assert dead is False and row.state == "pending" and row.attempt_count == 3
    assert _taken_back_on_its_word(row), "the resume that follows is graceful (cap 20), not counted (cap 3)"


async def test_the_gone_sweep_reads_expired_leases_and_runs_before_the_expired_lease_sweep(monkeypatch):
    calls: list[str] = []

    async def leased_owners(db, *, adapters, include_expired=False):
        calls.append(f"owners include_expired={include_expired}")
        return ["worker-old"]

    async def gone(owners, *, me):
        calls.append("gone")
        return {"worker-old": {"state": "draining", "lapsed": True}}

    async def reclaim_from_gone_owners(db, gone, **kwargs):
        calls.append("reclaim_from_gone_owners")
        return []

    async def reclaim_expired_leases(db, **kwargs):
        calls.append("reclaim_expired_leases")
        return []

    monkeypatch.setattr(orchestration_engine, "leased_owners", leased_owners)
    monkeypatch.setattr(orchestration_engine, "reclaim_from_gone_owners", reclaim_from_gone_owners)
    monkeypatch.setattr(orchestration_engine, "reclaim_expired_leases", reclaim_expired_leases)
    monkeypatch.setattr(worker_presence, "gone", gone)

    await orchestration_reconciler._reclaim_from_gone_workers(object(), "worker-new", ["sub_agent"], 10)

    assert calls == ["owners include_expired=True", "gone", "reclaim_from_gone_owners"]


def _leased(*, expires_in: float, written_ago: float) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        lease_expires_at=now + timedelta(seconds=expires_in), updated_at=now - timedelta(seconds=written_ago),
    )


@pytest.fixture
def _real_last_heard(monkeypatch):
    monkeypatch.setattr(orchestration_engine, "last_heard_at", _LAST_HEARD_AT)


def test_a_lease_renewed_after_the_last_word_is_left_to_its_live_holder(_real_last_heard):
    # Its presence lapsed but its process heartbeat the lease since.
    said = {"at": (datetime.now(UTC) - timedelta(seconds=30)).isoformat()}
    row = _leased(expires_in=50, written_ago=2)

    assert orchestration_engine._renewed_since_its_word(row, said, datetime.now(UTC))


@pytest.mark.parametrize(
    "row,said_ago",
    [
        (_leased(expires_in=-10, written_ago=2), 30),  # expired: the gone worker's
        (_leased(expires_in=50, written_ago=40), 30),  # live, but not written since its word
        (_leased(expires_in=50, written_ago=2), None),  # no word to compare with
    ],
)
def test_an_expired_or_untouched_lease_is_taken_back(_real_last_heard, row, said_ago):
    said = {} if said_ago is None else {"at": (datetime.now(UTC) - timedelta(seconds=said_ago)).isoformat()}

    assert not orchestration_engine._renewed_since_its_word(row, said, datetime.now(UTC))


async def test_the_locked_take_back_skips_a_row_its_holder_renewed(_real_last_heard):
    renewed = _row(attempt_count=1)
    renewed.lease_expires_at = datetime.now(UTC) + timedelta(seconds=50)
    renewed.updated_at = datetime.now(UTC)
    word = {**_record(), "at": (datetime.now(UTC) - timedelta(seconds=30)).isoformat()}
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [renewed]))),
        flush=AsyncMock(),
    )

    dead = await orchestration_engine.reclaim_from_gone_owners(
        db, {"worker-old": word}, adapters=["sub_agent"], limit=10
    )

    assert dead == [] and renewed.state == "running" and renewed.lease_owner == "worker-old"


async def test_the_deadline_hold_repair_is_no_word_of_a_leased_rows_holder(_real_last_heard, monkeypatch):
    # The reclaimer's own write must not read as a renewal by the gone holder: it keeps
    # the holder's updated_at, and keeps it in the UPDATE so the column's onupdate does not stamp it either.
    kept: list[tuple[object, str]] = []
    monkeypatch.setattr(orchestration_engine, "flag_modified", lambda row, key: kept.append((row, key)))
    now = datetime.now(UTC)
    heard = now - timedelta(seconds=40)
    held = _row(attempt_count=1)
    held.lease_expires_at, held.updated_at = now + timedelta(seconds=50), heard
    held.deadline_at = now - timedelta(minutes=5)
    held.payload = {
        orchestration_engine.HOLD_PAYLOAD_KEY: {
            "since": (now - timedelta(minutes=1)).isoformat(), "count": 1, "reason": "held:ai_disabled",
            "budget_seconds": 600.0,
        }
    }
    unleased = _row(attempt_count=1)
    unleased.state, unleased.lease_owner, unleased.lease_expires_at = "pending", None, None
    unleased.deadline_at, unleased.payload = held.deadline_at, {**held.payload}
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [held, unleased]))
        ),
        flush=AsyncMock(),
    )

    assert await orchestration_engine.sweep_deadlines(db, limit=10) == []

    assert held.deadline_at > now and held.updated_at == heard and kept == [(held, "updated_at")]
    assert unleased.deadline_at > now and unleased.updated_at >= now
    word = {"at": (now - timedelta(seconds=20)).isoformat()}
    assert not orchestration_engine._renewed_since_its_word(held, word, now)


def test_the_cycle_takes_back_from_workers_gone_before_any_other_sweep():
    import inspect

    source = inspect.getsource(orchestration_reconciler._run_cycle)
    gone = source.index("_reclaim_from_gone_workers(")
    assert gone < source.index("sweep_deadlines(") and gone < source.index("reclaim_expired_leases(")


# A legacy row (max_attempts 4, the old floor) that loses its worker without a word at its fourth claim, before any
# claim since the deploy raised its bound, is taken back under the bound its adapter runs it under now, not
# dead-lettered at 4.


def _legacy_row(adapter: str) -> SimpleNamespace:
    row = _row(attempt_count=4)
    row.adapter = adapter
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=30)
    row.deadline_at = None
    return row


def _db_with(*rows) -> SimpleNamespace:
    return SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))),
        flush=AsyncMock(),
    )


def _task_bound() -> int:
    from app.services import background_checkpoint

    return background_checkpoint.task_max_attempts(settings.orchestration_default_max_attempts)


@pytest.mark.parametrize("adapter", ["assignment", "sub_agent"])
async def test_a_legacy_row_losing_its_worker_wordlessly_is_requeued_under_the_raised_bound(adapter):
    from app.services.orchestration_adapters.assignment import AssignmentAdapter
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter

    row = _legacy_row(AssignmentAdapter.name if adapter == "assignment" else SubAgentAdapter.name)
    bound_of = orchestration_reconciler.attempt_bound_of([AssignmentAdapter(), SubAgentAdapter()])
    crashed = _record(version="1.0.1-new")  # this very release: a wordless loss that spends the attempt

    dead = await orchestration_engine.reclaim_from_gone_owners(
        _db_with(row), {"worker-old": crashed}, adapters=[row.adapter], limit=10, bound_of=bound_of
    )

    assert dead == [] and row.state == "pending" and row.attempt_count == 4
    assert row.max_attempts == _task_bound() > 4


async def test_a_legacy_row_whose_lease_expired_is_requeued_under_the_raised_bound():
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    row = _legacy_row(AssignmentAdapter.name)
    bound_of = orchestration_reconciler.attempt_bound_of([AssignmentAdapter()])

    dead = await orchestration_engine.reclaim_expired_leases(
        _db_with(row), limit=10, head_adapters=[row.adapter], bound_of=bound_of
    )

    assert dead == [] and row.state == "pending" and row.max_attempts == _task_bound()


async def test_an_adapter_keeping_its_bound_still_dead_letters_an_exhausted_row():
    class _Kept:
        name = "video_render"

        def raised_attempt_bound(self, max_attempts: int) -> int:
            return max_attempts

    class _Broken:
        name = "broken"

        def raised_attempt_bound(self, max_attempts: int) -> int:
            raise RuntimeError("boom")

    bound_of = orchestration_reconciler.attempt_bound_of([_Kept(), _Broken()])
    rows = [_legacy_row("video_render"), _legacy_row("broken"), _legacy_row("not_registered")]

    dead = await orchestration_engine.reclaim_expired_leases(_db_with(*rows), limit=10, bound_of=bound_of)

    assert dead == rows and all(row.state == "dead_letter" and row.max_attempts == 4 for row in rows)


def test_only_the_old_floor_is_raised():
    from app.services.orchestration_adapters.base import OrchestrationAdapter
    from app.services.orchestration_adapters.sub_agent import raised_task_bound

    assert raised_task_bound(4) == _task_bound()
    assert raised_task_bound(3) == 3 and raised_task_bound(7) == 7 and raised_task_bound(_task_bound()) == _task_bound()
    # The generic default keeps every bound: no adapter-specific logic outside the adapters that opt in.
    assert OrchestrationAdapter.raised_attempt_bound(SimpleNamespace(), 4) == 4


def test_the_cycle_hands_the_adapters_bounds_to_both_take_backs():
    import inspect

    source = inspect.getsource(orchestration_reconciler._run_cycle)
    assert "bound_of = attempt_bound_of(adapters)" in source
    assert "_reclaim_from_gone_workers(db, worker_id, resumable, batch, bound_of, now=swept_at)" in source
    assert "head_adapters=resumable, bound_of=bound_of" in source

