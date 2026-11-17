"""A CI event a run stood down on because a live run held its lease is delivered again (#11988).

The green of a head is delivered once. A run whose gate check failed because
another LIVE run held the lease it reads used to end skipped with the event
gone, so the pull request waited for something else to wake the assignment.
The event is now kept per assignment and head and delivered again once the
holder ended; a scratch branch of the batched commit is never watched.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest

from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.services import assignment_activation_check as gate
from app.services import event_enrichment_retry as retry

_SHA = "d" * 40
_SUBJECT = f"xtm-one-platform/xtm-one@{_SHA}"


def _completion(branch: str = "fix/1") -> dict:
    return {
        "action": "completed",
        "workflow_run": {"id": 1, "conclusion": "success", "head_sha": _SHA, "head_branch": branch},
        "repository": {"full_name": "XTM-One-Platform/xtm-one"},
    }


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.z: dict[str, dict[str, float]] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def zadd(self, key, mapping):
        self.z.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrangebyscore(self, key, lo, hi, start=0, num=None):
        items = sorted((s, m) for m, s in self.z.get(key, {}).items() if lo <= s <= hi)
        members = [m for _, m in items]
        return members[start : start + num] if num is not None else members

    async def zrem(self, key, member):
        return 1 if self.z.get(key, {}).pop(member, None) is not None else 0


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()

    async def _get():
        return redis

    monkeypatch.setattr(retry, "_redis", _get)
    return redis


def _due(fake_redis):
    for member in fake_redis.z.get(retry.HELD_KEY, {}):
        fake_redis.z[retry.HELD_KEY][member] = time.time() - 1


def _hold(aid, holder, run_id=1):
    payload = _completion()
    payload["workflow_run"]["id"] = run_id
    return retry.hold_for_lease(
        assignment_id=aid,
        provider="github",
        event_key="workflow_run.completed",
        payload=payload,
        subject=_SUBJECT,
        holder_run_id=holder,
        trigger_context={"type": "webhook", "provider": "github", "event_key": "workflow_run.completed"},
    )


def test_a_failed_check_names_the_live_holder_only():
    alive = {"held_by_run": {"id": "r1", "alive": True}}
    assert gate.live_holder_id(alive) == "r1"
    assert gate.live_holder_id({"held_by_run": {"id": "r1", "alive": False}}) is None
    assert gate.live_holder_id({"value": {}}) is None
    assert gate.live_holder_id(None) is None


def test_a_scratch_branch_head_is_never_watched():
    assert gh_enrichment.head_subject("workflow_run.completed", _completion()) == _SUBJECT
    scratch = _completion("tmp/commit-batch/1794705674-4f77bdf85987-2967d538")
    assert gh_enrichment.head_subject("workflow_run.completed", scratch) is None
    suite = {
        "check_suite": {"head_sha": _SHA, "head_branch": "tmp/commit-batch/1-a-b"},
        "repository": {"full_name": "a/b"},
    }
    assert gh_enrichment.head_subject("check_suite.completed", suite) is None


@pytest.mark.asyncio
async def test_an_event_held_by_a_live_lease_is_delivered_once_after_release(monkeypatch, fake_redis):
    aid = uuid.uuid4()
    holder = str(uuid.uuid4())
    alive = {"value": True}
    delivered: list[dict] = []

    async def _alive(_db, _rid):
        return alive["value"]

    async def _deliver(_db, entry, *, honour_admitted=True):
        delivered.append({"entry": entry, "honour_admitted": honour_admitted})
        return "run-2"

    monkeypatch.setattr(retry, "_holder_alive", _alive)
    monkeypatch.setattr(retry, "_evaluate_watch", _deliver)

    assert await _hold(aid, holder)
    # The holder still runs: kept, nothing delivered.
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.HELD_KEY, entry_prefix=retry.HELD_ENTRY_PREFIX)
    assert await retry._evaluate_held(None, entries[0]) is None
    assert delivered == [] and len(fake_redis.z[retry.HELD_KEY]) == 1
    # The holder ended: one delivery, the admission not read as an end.
    alive["value"] = False
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.HELD_KEY, entry_prefix=retry.HELD_ENTRY_PREFIX)
    assert await retry._evaluate_held(None, entries[0]) == "run-2"
    assert len(delivered) == 1 and delivered[0]["honour_admitted"] is False
    assert delivered[0]["entry"]["trigger_context"]["held_by_lease"]["holder_run_id"] == holder
    assert not fake_redis.z[retry.HELD_KEY]
    _due(fake_redis)
    assert await retry._claim_due(pending_key=retry.HELD_KEY, entry_prefix=retry.HELD_ENTRY_PREFIX) == []


@pytest.mark.asyncio
async def test_two_events_on_the_same_head_are_one_delivery(fake_redis):
    aid = uuid.uuid4()
    holder = str(uuid.uuid4())
    assert await _hold(aid, holder, run_id=1)
    assert await _hold(aid, holder, run_id=2)
    assert len(fake_redis.z[retry.HELD_KEY]) == 1
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.HELD_KEY, entry_prefix=retry.HELD_ENTRY_PREFIX)
    assert len(entries) == 1
    assert entries[0]["payload"]["workflow_run"]["id"] == 2


@pytest.mark.asyncio
async def test_a_held_event_ends_after_its_ttl(fake_redis):
    import json

    aid = uuid.uuid4()
    holder = str(uuid.uuid4())
    assert await _hold(aid, holder)
    key = f"{retry.HELD_ENTRY_PREFIX}{next(iter(fake_redis.z[retry.HELD_KEY]))}"
    old = json.loads(fake_redis.kv[key])
    old["held_since"] = time.time() - retry.HELD_TTL_SECONDS - 1
    fake_redis.kv[key] = json.dumps(old)
    assert not await _hold(aid, holder)


def test_the_redelivery_delay_is_read_and_bounded():
    assert retry.redeliver_after({}) is None
    assert retry.redeliver_after({"redeliver_after_seconds": 0}) is None
    assert retry.redeliver_after({"redeliver_after_seconds": True}) is None
    assert retry.redeliver_after({"redeliver_after_seconds": "x"}) is None
    assert retry.redeliver_after({"redeliver_after_seconds": 900}) == 900
    assert retry.redeliver_after({"redeliver_after_seconds": 5}) == retry.REDELIVER_MIN_SECONDS
    assert retry.redeliver_after({"redeliver_after_seconds": 10**6}) == retry.REDELIVER_MAX_SECONDS


@pytest.mark.asyncio
async def test_a_green_head_is_redelivered_once_at_the_window_end(monkeypatch, fake_redis):
    aid = uuid.uuid4()
    delivered: list[dict] = []

    async def _deliver(_db, entry, *, honour_admitted=True):
        delivered.append({"entry": entry, "honour_admitted": honour_admitted})
        return "run-3"

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", _Session)
    monkeypatch.setattr(retry, "_evaluate_watch", _deliver)
    args = {
        "assignment_id": aid,
        "provider": "github",
        "event_key": "workflow_run.completed",
        "payload": _completion(),
        "subject": _SUBJECT,
        "delay_seconds": 900,
        "trigger_context": {"provider": "github"},
    }
    assert await retry.schedule_redelivery(**args)
    # A second admission of the same head never schedules a second wake.
    assert not await retry.schedule_redelivery(**args)
    # Not due before the window end.
    assert await retry.process_redeliveries() == 0
    for member in fake_redis.z[retry.REDELIVER_KEY]:
        fake_redis.z[retry.REDELIVER_KEY][member] = time.time() - 1
    assert await retry.process_redeliveries() == 1
    assert len(delivered) == 1 and delivered[0]["honour_admitted"] is False
    assert delivered[0]["entry"]["trigger_context"]["redelivery"] == {"after_seconds": 900}
    assert await retry.process_redeliveries() == 0


@pytest.mark.asyncio
async def test_a_gate_skip_on_a_live_holder_keeps_the_event(monkeypatch, fake_redis):
    from app.services import assignment_engine

    holder = str(uuid.uuid4())
    run = SimpleNamespace(
        id=uuid.uuid4(),
        trigger_context={"type": "webhook", "provider": "github", "event_key": "workflow_run.completed"},
        trigger_payload=_completion(),
    )
    assignment = SimpleNamespace(id=uuid.uuid4(), name="green", trigger_type="integration_event")
    assert await assignment_engine._hold_event_for_lease(run, assignment, holder) is True
    assert len(fake_redis.z[retry.HELD_KEY]) == 1
    # The run itself is never its own holder; a scratch branch is never kept.
    assert await assignment_engine._hold_event_for_lease(run, assignment, str(run.id)) is False
    run.trigger_payload = _completion("tmp/commit-batch/1-a-b")
    assert await assignment_engine._hold_event_for_lease(run, assignment, holder) is False
    # A scheduled run has no event to keep.
    assignment.trigger_type = "schedule"
    run.trigger_payload = _completion()
    assert await assignment_engine._hold_event_for_lease(run, assignment, holder) is False
