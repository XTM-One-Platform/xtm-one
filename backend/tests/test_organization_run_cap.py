"""#9157 slice M6: the tracked organizations' daily run cap, the digest and the run estimate."""

from __future__ import annotations

import fnmatch
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import organization_run_cap as cap
from app.services.organization_events import PAYLOAD_KEY

TODAY = datetime(2026, 11, 6, 12, tzinfo=UTC)
TOMORROW = datetime(2026, 11, 7, 0, 5, tzinfo=UTC)
ORG = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class FakeRedis:
    def __init__(self):
        self.kv: dict[str, int] = {}
        self.lists: dict[str, list[str]] = {}
        self.sets: dict[str, set[str]] = {}

    async def incr(self, key):
        self.kv[key] = self.kv.get(key, 0) + 1
        return self.kv[key]

    async def expire(self, key, seconds):
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists[key][start:] if end == -1 else self.lists[key][start : end + 1]

    async def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    async def delete(self, key):
        self.lists.pop(key, None)

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)

    async def srem(self, key, value):
        members = self.sets.get(key, set())
        if value in members:
            members.discard(value)
            return 1
        return 0

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    def keys(self, pattern):
        return [k for k in [*self.kv, *self.lists] if fnmatch.fnmatch(k, pattern)]


def _event(n):
    return {
        "type": "create",
        "message": f"report {n}",
        "data": {"id": f"report--{n}", "name": f"R{n}", "type": "report"},
    }


@pytest.mark.asyncio
async def test_an_event_of_no_tracked_organization_is_never_counted():
    redis = FakeRedis()
    assert await cap.admit(redis, assignment_id="a", organizations={}, payload=_event(1), now=TODAY)
    assert redis.kv == {} and redis.lists == {}


@pytest.mark.asyncio
async def test_the_cap_runs_the_first_events_then_holds_the_rest_for_one_digest():
    redis = FakeRedis()
    ran = [
        await cap.admit(redis, assignment_id="a", organizations={ORG: 5}, payload=_event(n), now=TODAY)
        for n in range(8)
    ]
    assert ran == [True] * 5 + [False] * 3
    assert await cap.used_today(redis, ORG, now=TODAY) == 8
    # Not due the same day.
    assert await cap.due_digests(redis, now=TODAY) == []
    due = await cap.due_digests(redis, now=TOMORROW)
    assert len(due) == 1
    assignment_id, organization_id, day, events, held = due[0]
    assert (assignment_id, organization_id, day, held) == ("a", ORG, "20261106", 3)
    assert [e["name"] for e in events] == ["R5", "R6", "R7"]
    # Claimed once: a second tick finds nothing.
    assert await cap.due_digests(redis, now=TOMORROW) == []


@pytest.mark.asyncio
async def test_an_event_runs_while_one_concerned_organization_is_within_its_cap():
    redis = FakeRedis()
    for n in range(2):
        assert await cap.admit(redis, assignment_id="a", organizations={ORG: 2}, payload=_event(n), now=TODAY)
    assert await cap.admit(redis, assignment_id="a", organizations={ORG: 2, OTHER: 5}, payload=_event(9), now=TODAY)
    assert redis.lists == {}


@pytest.mark.asyncio
async def test_the_digest_payload_names_the_organization_for_the_run_block():
    payload = cap.digest_payload(ORG, "20261106", [cap.held_event(_event(1))], 1)
    assert payload[PAYLOAD_KEY] == [ORG]
    assert payload[cap.DIGEST_PAYLOAD_KEY]["held"] == 1
    assert "2026-11-06" in payload["message"]
    json.dumps(payload)


def test_a_held_event_keeps_only_bounded_fields():
    kept = cap.held_event({"type": "create", "message": "x" * 1000, "data": {"id": "r", "name": "n", "secret": "s"}})
    assert set(kept) == {"type", "message", "id", "name", "entity_type"}
    assert len(kept["message"]) == 300


@pytest.mark.asyncio
async def test_a_redis_outage_runs_the_event(monkeypatch):
    async def caps(db, ids):
        return {ORG: 5}

    async def broken():
        raise ConnectionError("down")

    monkeypatch.setattr(cap, "caps_of", caps)
    monkeypatch.setattr("app.core.redis.redis_client", broken)
    assert await cap.admit_delivery(None, "a", {PAYLOAD_KEY: [ORG], **_event(1)})


@pytest.mark.asyncio
async def test_a_delivery_without_organizations_reads_nothing(monkeypatch):
    async def caps(db, ids):  # pragma: no cover - must not be called
        raise AssertionError("read")

    monkeypatch.setattr(cap, "caps_of", caps)
    assert await cap.admit_delivery(None, "a", _event(1))


def test_the_spec_scope_decides_which_organizations_an_assignment_serves():
    provider = SimpleNamespace(relationship="provider")
    assert not cap.serves({"organizations": "primary"}, provider)
    assert cap.serves({"organizations": "tracked"}, provider)
    assert cap.serves({"organizations": ["provider"]}, provider)
    assert not cap.serves({"organizations": ["customer"]}, provider)


def test_the_estimate_query_names_the_identity_and_the_window():
    f = cap.estimate_filters(["Report"], "identity--1", "2026-10-07T00:00:00+00:00")
    assert f["filters"][0]["key"] == ["created_at"]
    assert f["filters"][1]["values"] == ["Report"]
    assert f["filterGroups"][0]["filters"][0]["values"] == ["identity--1"]


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _query):
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_the_estimate_counts_each_serving_assignment_and_bounds_it_by_the_cap():
    def assignment(name, organizations):
        return SimpleNamespace(
            id=name,
            name=name,
            trigger_config={
                "provider": "opencti",
                "provision": {
                    "fleet": "F5",
                    "purpose": name,
                    "entity_types": ["Report"],
                    "organizations": organizations,
                },
            },
        )

    rows = [
        assignment("suppliers", ["provider"]),
        assignment("customers", ["customer"]),
        assignment("primary", "primary"),
    ]
    organization = SimpleNamespace(
        id=ORG, relationship="provider", opencti_organization_id="identity--1", daily_run_cap=2, monitoring="off"
    )
    calls = []

    async def gql(query, variables):
        calls.append(variables)
        return {"stixCoreObjects": {"pageInfo": {"globalCount": 80}}}, None

    out = await cap.estimate_runs(_Db(rows), gql, organization, now=TODAY)
    assert [a["purpose"] for a in out["assignments"]] == ["suppliers"]
    assert out["assignments"][0]["events_last_30_days"] == 80
    # 2 runs a day over 30 days at most.
    assert out["expected_runs_per_month"] == 60
    assert len(calls) == 1 and out["monitoring"] == "off"


@pytest.mark.asyncio
async def test_the_estimate_without_an_identity_says_so_and_queries_nothing():
    rows = [
        SimpleNamespace(
            id="a",
            name="a",
            trigger_config={
                "provider": "opencti",
                "provision": {"entity_types": ["Report"], "organizations": "tracked"},
            },
        )
    ]
    organization = SimpleNamespace(id=ORG, relationship="partner", opencti_organization_id=None, daily_run_cap=5)

    async def gql(query, variables):  # pragma: no cover
        raise AssertionError("queried")

    out = await cap.estimate_runs(_Db(rows), gql, organization, now=TODAY)
    assert out["assignments"][0]["error"] == "no OpenCTI identity linked"
    assert out["expected_runs_per_month"] == 0


class _RecordingDb(_Db):
    def __init__(self, rows):
        super().__init__(rows)
        self.queries = []

    async def execute(self, query):
        self.queries.append(str(query.compile(compile_kwargs={"literal_binds": False})))
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_the_fleet_estimate_reads_only_its_members_assignments():
    """#9162 R3: GET /fleets/{id}/run-estimate narrows the assignments to the fleet's members."""
    organization = SimpleNamespace(id=ORG, relationship="provider", opencti_organization_id=None, daily_run_cap=5)

    async def gql(query, variables):  # pragma: no cover
        raise AssertionError("queried")

    db = _RecordingDb([])
    await cap.estimate_runs(db, gql, organization, now=TODAY, agent_ids=[uuid.UUID(OTHER)])
    assert len(db.queries) == 1 and "assignments.agent_id IN" in db.queries[0]

    empty = _RecordingDb([SimpleNamespace(id="a", name="a", trigger_config={})])
    out = await cap.estimate_runs(empty, gql, organization, now=TODAY, agent_ids=[])
    # A fleet with no member reads no assignment at all.
    assert empty.queries == [] and out["assignments"] == [] and out["expected_runs_per_month"] == 0
