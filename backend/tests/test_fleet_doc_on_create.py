"""A new fleet's first documentation is generated once at creation (#10316)."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.services import fleet_doc_on_create as on_create
from app.services.fleet_spec import FleetSpecPlan


class _FakeDb:
    def __init__(self, fleet):
        self._fleet = fleet

    async def get(self, _model, _id):
        return self._fleet


@pytest.fixture
def markers(monkeypatch):
    calls: list[tuple[uuid.UUID, bool]] = []

    async def _claim(fleet_id):
        calls.append((fleet_id, True))
        return True

    async def _release(fleet_id, failure=None):
        calls.append((fleet_id, False))

    monkeypatch.setattr(on_create, "claim_generation", _claim)
    monkeypatch.setattr(on_create, "release_generation", _release)
    return calls


@pytest.fixture
def generated(monkeypatch):
    seen: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _gen(fleet_id, user_id):
        seen.append((fleet_id, user_id))

    monkeypatch.setattr(on_create, "_generate", _gen)
    return seen


def _gate(monkeypatch, refusal):
    from app.services import agentic_gate

    async def _refusal(_db):
        return refusal

    monkeypatch.setattr(agentic_gate, "agentic_refusal", _refusal)


@pytest.mark.asyncio
async def test_an_undocumented_new_fleet_is_scheduled_once_with_its_marker(monkeypatch, markers, generated):
    _gate(monkeypatch, None)
    fleet_id, user_id = uuid.uuid4(), uuid.uuid4()
    scheduled = await on_create.schedule_documentation_on_create(
        _FakeDb(SimpleNamespace(documentation=None)), fleet_id, user_id
    )
    await asyncio.gather(*list(on_create._tasks))
    assert scheduled is True
    assert markers == [(fleet_id, True)]
    assert generated == [(fleet_id, user_id)]


@pytest.mark.asyncio
async def test_nothing_is_scheduled_when_agentic_ai_may_not_run(monkeypatch, markers, generated):
    _gate(monkeypatch, "ai_disabled")
    scheduled = await on_create.schedule_documentation_on_create(
        _FakeDb(SimpleNamespace(documentation=None)), uuid.uuid4(), uuid.uuid4()
    )
    assert scheduled is False
    assert markers == [] and generated == []


@pytest.mark.asyncio
async def test_a_fleet_that_already_has_a_documentation_is_left_as_is(monkeypatch, markers, generated):
    _gate(monkeypatch, None)
    documented = SimpleNamespace(documentation={"sections": {"purpose": "Copied at the fork."}, "version": 1})
    scheduled = await on_create.schedule_documentation_on_create(_FakeDb(documented), uuid.uuid4(), uuid.uuid4())
    assert scheduled is False
    assert markers == [] and generated == []


@pytest.mark.asyncio
async def test_no_fleet_id_schedules_nothing(markers, generated):
    assert await on_create.schedule_documentation_on_create(_FakeDb(None), None, uuid.uuid4()) is False
    assert markers == [] and generated == []


@pytest.mark.asyncio
async def test_the_marker_is_cleared_even_when_the_generation_fails(monkeypatch, markers):
    from app.core import database

    class _Session:
        async def __aenter__(self):
            raise RuntimeError("no database")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    fleet_id = uuid.uuid4()
    await on_create._generate(fleet_id, uuid.uuid4())
    assert markers == [(fleet_id, False)]


class _FakeRedis:
    """The few Redis calls the marker makes, with SET NX semantics."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.expires: list[str] = []

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttl[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)

    async def expire(self, key, seconds):
        self.expires.append(key)
        self.ttl[key] = seconds


@pytest.fixture
def fake_redis(monkeypatch):
    from app.core import redis as redis_module

    client = _FakeRedis()

    async def _client():
        return client

    monkeypatch.setattr(redis_module, "redis_client", _client)
    return client


@pytest.mark.asyncio
async def test_a_regenerate_is_recorded_and_a_second_claim_is_refused(fake_redis):
    fleet_id = uuid.uuid4()
    assert await on_create.claim_generation(fleet_id) is True
    state = await on_create.generation_state(fleet_id)
    assert state.generating is True and state.started_at and "T" in state.started_at
    assert fake_redis.ttl[on_create._marker(fleet_id)] == on_create.GENERATING_TTL_SECONDS
    # A second click or a second tab starts no parallel generation (#10817).
    assert await on_create.claim_generation(fleet_id) is False


@pytest.mark.asyncio
async def test_the_marker_is_cleared_on_success_and_a_failure_is_kept_readable(fake_redis):
    fleet_id = uuid.uuid4()
    await on_create.claim_generation(fleet_id)
    await on_create.release_generation(fleet_id)
    assert await on_create.generation_state(fleet_id) == on_create.DocGeneration()
    await on_create.claim_generation(fleet_id)
    await on_create.release_generation(fleet_id, "The model could not answer.")
    state = await on_create.generation_state(fleet_id)
    assert state.generating is False and state.failure == "The model could not answer."
    assert fake_redis.ttl[on_create._failure(fleet_id)] == on_create.FAILURE_TTL_SECONDS
    # The next claim clears the failure: the page spins, it shows no old error.
    assert await on_create.claim_generation(fleet_id) is True
    assert (await on_create.generation_state(fleet_id)).failure is None


@pytest.mark.asyncio
async def test_a_long_generation_refreshes_its_marker_until_it_ends(monkeypatch, fake_redis):
    monkeypatch.setattr(on_create, "GENERATING_REFRESH_SECONDS", 0.01)
    fleet_id = uuid.uuid4()
    await on_create.claim_generation(fleet_id)
    async with on_create.generation_running(fleet_id):
        await asyncio.sleep(0.05)
    refreshed = len(fake_redis.expires)
    assert refreshed >= 2
    assert set(fake_redis.expires) == {on_create._marker(fleet_id)}
    await asyncio.sleep(0.03)
    assert len(fake_redis.expires) == refreshed


@pytest.mark.asyncio
async def test_a_legacy_marker_reads_as_running_with_no_start(fake_redis):
    fleet_id = uuid.uuid4()
    fake_redis.store[on_create._marker(fleet_id)] = "1"
    assert await on_create.generation_state(fleet_id) == on_create.DocGeneration(generating=True)


@pytest.mark.asyncio
async def test_an_unreachable_redis_never_stops_a_generation(monkeypatch):
    from app.core import redis as redis_module

    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis_module, "redis_client", _down)
    fleet_id = uuid.uuid4()
    assert await on_create.claim_generation(fleet_id) is True
    assert await on_create.generation_state(fleet_id) == on_create.DocGeneration()
    await on_create.release_generation(fleet_id, "x")


def test_a_spec_plan_says_whether_it_created_its_fleet():
    plan = FleetSpecPlan(dry_run=False, applied=True)
    plan.add("fleet", "ops", "create")
    assert plan.created_fleet is True
    updated = FleetSpecPlan(dry_run=False, applied=True)
    updated.add("fleet", "ops", "update", "orchestrator")
    assert updated.created_fleet is False
    dry = FleetSpecPlan(dry_run=True, applied=False)
    dry.add("fleet", "ops", "create")
    assert dry.created_fleet is False
