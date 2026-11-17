"""A CI head whose last completion was never processed still wakes its assignment once.

The delivery of the last check of a head can be lost before it is read at all
(the workers replaced during a rollout): no read failed, so nothing was
deferred, and no later delivery reports the head. A completion the filter
dropped while checks still ran, and that would match once they finish green,
watches its head (``services/event_enrichment_retry.watch``); the scheduler
tick reads it again and triggers the assignment once the head settles green.
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import webhooks
from app.integrations.event_enrichment import event_enricher
from app.integrations.providers.github import event_enrichment as gh_enrichment
from app.services import event_enrichment
from app.services import event_enrichment_retry as retry

ME = "SamuelHassine"
_DESK_GREEN = {
    "action": "completed",
    "workflow_run.conclusion": "success",
    "enriched.head.checks.all_green": True,
    "enriched.pull_request.user.login": ME,
}
_CONFIG = {"provider": "github", "event": "workflow_run", "filter": _DESK_GREEN}
_AGENT = SimpleNamespace(integration_bindings={"github": [str(uuid.uuid4())]}, user_id=None, enabled=True)
_SHA = "c" * 40


def _completion(run_id: int = 1) -> dict:
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "run_attempt": 1,
            "name": "CI",
            "conclusion": "success",
            "head_sha": _SHA,
            "head_branch": "fix/1",
            "pull_requests": [{"number": 1}],
        },
        "repository": {"full_name": "XTM-One-Platform/xtm-one"},
    }


def _checks(*, pending: int, failed: int = 0, total: int = 3) -> dict:
    completed = total - pending
    names = [{"name": f"c{i}", "status": "completed", "conclusion": "success"} for i in range(completed - failed)]
    names += [{"name": f"f{i}", "status": "completed", "conclusion": "failure"} for i in range(failed)]
    names += [{"name": f"p{i}", "status": "in_progress", "conclusion": None} for i in range(pending)]
    return {
        "pull_request": {"user": {"login": ME}},
        "head": {
            "sha": _SHA,
            "checks": {
                "total": total,
                "completed": completed,
                "pending": pending,
                "failed": failed,
                "all_completed": pending == 0,
                "all_green": pending == 0 and failed == 0,
                "names": names,
            },
        },
    }


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.z: dict[str, dict[str, float]] = {}

    async def set(self, key, value, ex=None):
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


def _patch_facts(monkeypatch, *answers):
    states = iter(answers)

    async def _token(_db, _provider, _integration_id, _agent):
        return "tok", "integration"

    async def _miss(_key):
        return None

    async def _remember(_key, _facts):
        return None

    async def _fetch(_event_key, _payload, _token, **_k):
        answer = next(states)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(event_enrichment, "_credential", _token)
    monkeypatch.setattr(event_enrichment, "_cached", _miss)
    monkeypatch.setattr(event_enrichment, "_remember", _remember)
    real = event_enricher

    def _enricher(provider):
        found = real(provider)
        if found is None:
            return None
        return SimpleNamespace(
            applies=found.applies,
            cache_key=found.cache_key,
            fetch=_fetch,
            retryable=found.retryable,
            watch_subject=found.watch_subject,
            settled=found.settled,
            best_case=found.best_case,
        )

    monkeypatch.setattr(event_enrichment, "event_enricher", _enricher)


async def _receive(aid, run_id=1):
    return await webhooks._assignment_filter_reason(
        None,
        "github",
        "workflow_run.completed",
        _completion(run_id),
        event="workflow_run",
        trigger_config=_CONFIG,
        agent=_AGENT,
        assignment_id=aid,
        retry_context={"type": "webhook", "scope": "provider", "provider": "github"},
    )


def test_github_names_the_head_and_its_settled_state():
    assert gh_enrichment.head_subject("workflow_run.completed", _completion()) == f"xtm-one-platform/xtm-one@{_SHA}"
    assert gh_enrichment.head_subject("push", {"after": _SHA, "repository": {"full_name": "a/b"}}) is None
    assert gh_enrichment.head_settled(_checks(pending=1)) is False
    assert gh_enrichment.head_settled(_checks(pending=0)) is True
    best = gh_enrichment.head_best_case(_checks(pending=2))["head"]["checks"]
    assert best["all_green"] is True and best["pending"] == 0
    assert gh_enrichment.head_best_case(_checks(pending=1, failed=1))["head"]["checks"]["all_green"] is False
    # The watch stands in for the lost last delivery, settled or not.
    assert best["last_to_complete"] is True
    assert gh_enrichment.head_best_case(_checks(pending=0))["head"]["checks"]["last_to_complete"] is True


def test_a_watched_head_reads_settled_facts_as_its_last_delivery():
    settled = event_enrichment.as_watched_delivery("github", _checks(pending=0))
    assert settled["head"]["checks"]["last_to_complete"] is True
    running = _checks(pending=1)
    assert event_enrichment.as_watched_delivery("github", running) is running
    assert event_enrichment.as_watched_delivery("github", None) is None


@pytest.mark.asyncio
async def test_a_completion_dropped_while_checks_run_watches_its_head(monkeypatch, fake_redis):
    _patch_facts(monkeypatch, _checks(pending=1))
    aid = str(uuid.uuid4())
    assert await _receive(aid) == "enriched.head.checks.all_green does not match the filter"
    assert len(fake_redis.z[retry.WATCH_KEY]) == 1
    # Nothing deferred: the read did not fail.
    assert not fake_redis.z.get(retry.PENDING_KEY)


@pytest.mark.asyncio
async def test_a_head_already_red_or_finished_is_not_watched(monkeypatch, fake_redis):
    _patch_facts(monkeypatch, _checks(pending=1, failed=1), _checks(pending=0, failed=1))
    aid = str(uuid.uuid4())
    await _receive(aid, 1)
    await _receive(aid, 2)
    assert not fake_redis.z.get(retry.WATCH_KEY)


@pytest.mark.asyncio
async def test_the_admitted_last_delivery_ends_the_watch(monkeypatch, fake_redis):
    _patch_facts(monkeypatch, _checks(pending=1), _checks(pending=0))
    aid = str(uuid.uuid4())
    await _receive(aid, 1)
    assert len(fake_redis.z[retry.WATCH_KEY]) == 1
    assert await _receive(aid, 2) is None
    assert not fake_redis.z[retry.WATCH_KEY]
    assert await retry.is_admitted(aid, f"xtm-one-platform/xtm-one@{_SHA}")


@pytest.mark.asyncio
async def test_a_completion_whose_read_timed_out_is_deferred_and_watches_its_head(monkeypatch, fake_redis):
    from app.services import assignment_engine

    # The last completion's read times out, and so do the two deferred reads
    # (a provider slowdown): the head watch still wakes the assignment once.
    _patch_facts(monkeypatch, TimeoutError(), TimeoutError(), _checks(pending=0))
    aid = uuid.uuid4()
    assert await _receive(str(aid)) == webhooks.DEFERRED_REASON
    assert len(fake_redis.z[retry.PENDING_KEY]) == 1
    assert len(fake_redis.z[retry.WATCH_KEY]) == 1
    wf = SimpleNamespace(id=aid, agent_id=uuid.uuid4(), trigger_type="integration_event", trigger_config=_CONFIG)
    started: list[dict] = []

    async def _trigger(**kwargs):
        started.append(kwargs)
        return "run-1"

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _trigger)
    db = _Db(wf, _AGENT)
    # A watched read that times out again keeps the head watched.
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.WATCH_KEY, entry_prefix=retry.WATCH_ENTRY_PREFIX)
    assert await retry._evaluate_watch(db, entries[0]) is None
    assert len(fake_redis.z[retry.WATCH_KEY]) == 1 and started == []
    # The next read answers the head green: one run.
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.WATCH_KEY, entry_prefix=retry.WATCH_ENTRY_PREFIX)
    assert await retry._evaluate_watch(db, entries[0]) == "run-1"
    assert len(started) == 1
    assert await retry.is_admitted(aid, f"xtm-one-platform/xtm-one@{_SHA}")


@pytest.mark.asyncio
async def test_a_settled_red_head_whose_read_failed_is_watched_then_ends(monkeypatch, fake_redis):
    _patch_facts(monkeypatch, TimeoutError(), _checks(pending=0, failed=1))
    aid = uuid.uuid4()
    await _receive(str(aid))
    assert len(fake_redis.z[retry.WATCH_KEY]) == 1
    wf = SimpleNamespace(id=aid, agent_id=uuid.uuid4(), trigger_type="integration_event", trigger_config=_CONFIG)
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.WATCH_KEY, entry_prefix=retry.WATCH_ENTRY_PREFIX)
    assert await retry._evaluate_watch(_Db(wf, _AGENT), entries[0]) is None
    # Settled and not green: the watch ends.
    assert not fake_redis.z[retry.WATCH_KEY]


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Db:
    def __init__(self, wf, agent):
        self.wf, self.agent = wf, agent

    async def execute(self, _stmt):
        return _Result(self.wf)

    async def get(self, _model, _id):
        return self.agent


def _due(fake_redis):
    for member in fake_redis.z[retry.WATCH_KEY]:
        fake_redis.z[retry.WATCH_KEY][member] = time.time() - 1


@pytest.mark.asyncio
async def test_a_lost_last_completion_wakes_the_assignment_once(monkeypatch, fake_redis):
    from app.services import assignment_engine

    # The delivered completion found a check running; the last one is never delivered.
    _patch_facts(monkeypatch, _checks(pending=1), _checks(pending=1), _checks(pending=0))
    aid = uuid.uuid4()
    await _receive(str(aid))
    wf = SimpleNamespace(id=aid, agent_id=uuid.uuid4(), trigger_type="integration_event", trigger_config=_CONFIG)
    started: list[dict] = []

    async def _trigger(**kwargs):
        started.append(kwargs)
        return "run-1"

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _trigger)
    db = _Db(wf, _AGENT)
    # First read: still running, watched again.
    entries = await retry._claim_due(pending_key=retry.WATCH_KEY, entry_prefix=retry.WATCH_ENTRY_PREFIX)
    assert entries == []
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.WATCH_KEY, entry_prefix=retry.WATCH_ENTRY_PREFIX)
    assert await retry._evaluate_watch(db, entries[0]) is None
    assert len(fake_redis.z[retry.WATCH_KEY]) == 1 and started == []
    # Second read: green, triggered once, then never again.
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.WATCH_KEY, entry_prefix=retry.WATCH_ENTRY_PREFIX)
    assert await retry._evaluate_watch(db, entries[0]) == "run-1"
    assert started[0]["skip_if_active"] is True
    assert started[0]["trigger_context"]["head_watch"]["subject"] == f"xtm-one-platform/xtm-one@{_SHA}"
    assert not fake_redis.z[retry.WATCH_KEY]
    assert await retry._evaluate_watch(db, entries[0]) is None
    assert len(started) == 1


@pytest.mark.asyncio
async def test_a_watch_ends_after_its_ttl(fake_redis):
    aid = uuid.uuid4()
    subject = f"xtm-one-platform/xtm-one@{_SHA}"
    kwargs = dict(
        assignment_id=aid, provider="github", event_key="workflow_run.completed", payload=_completion(), subject=subject
    )
    assert await retry.watch(**kwargs)
    member = next(iter(fake_redis.z[retry.WATCH_KEY]))
    key = f"{retry.WATCH_ENTRY_PREFIX}{member}"
    old = json.loads(fake_redis.kv[key])
    old["watched_since"] = time.time() - retry.WATCH_TTL_SECONDS - 1
    fake_redis.kv[key] = json.dumps(old)
    assert not await retry.watch(**kwargs)


@pytest.mark.asyncio
async def test_a_lost_last_completion_still_wakes_a_last_to_complete_filter(monkeypatch, fake_redis):
    from app.services import assignment_engine

    config = {**_CONFIG, "filter": {**_DESK_GREEN, "enriched.head.checks.last_to_complete": True}}
    _patch_facts(monkeypatch, _checks(pending=1), _checks(pending=0))
    aid = uuid.uuid4()
    reason = await webhooks._assignment_filter_reason(
        None,
        "github",
        "workflow_run.completed",
        _completion(),
        event="workflow_run",
        trigger_config=config,
        agent=_AGENT,
        assignment_id=str(aid),
        retry_context={"type": "webhook", "scope": "provider", "provider": "github"},
    )
    assert reason is not None and len(fake_redis.z[retry.WATCH_KEY]) == 1
    wf = SimpleNamespace(id=aid, agent_id=uuid.uuid4(), trigger_type="integration_event", trigger_config=config)
    started: list[dict] = []

    async def _trigger(**kwargs):
        started.append(kwargs)
        return "run-1"

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _trigger)
    _due(fake_redis)
    entries = await retry._claim_due(pending_key=retry.WATCH_KEY, entry_prefix=retry.WATCH_ENTRY_PREFIX)
    assert await retry._evaluate_watch(_Db(wf, _AGENT), entries[0]) == "run-1"
    assert len(started) == 1
