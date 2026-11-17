"""A CI completion whose enriched facts could not be read is read again, not lost.

The delivery reporting the last check of a head is the only one that turns
``enriched.head.checks.all_green`` true; GitHub does not deliver it twice. A
read that timed out (a slow GitHub, a worker rolled out) is deferred for a
bounded number of further reads (``services/event_enrichment_retry.py``).
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import webhooks
from app.integrations.event_enrichment import PermanentEnrichmentError, event_enricher
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


def _completion(run_id: int = 1) -> dict:
    return {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "run_attempt": 1,
            "name": "CI",
            "conclusion": "success",
            "head_sha": "b" * 40,
            "head_branch": "fix/1",
            "pull_requests": [{"number": 1}],
        },
        "repository": {"full_name": "XTM-One-Platform/xtm-one"},
    }


def _green() -> dict:
    return {
        "pull_request": {"user": {"login": ME}},
        "head": {"sha": "b" * 40, "checks": {"all_green": True, "pending": 0}},
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


def _patch_enrichment(monkeypatch, fetch):
    async def _token(_db, _provider, _integration_id, _agent):
        return "tok", "integration"

    async def _miss(_key):
        return None

    async def _remember(_key, _facts):
        return None

    enricher = event_enricher("github")
    monkeypatch.setattr(event_enrichment, "_credential", _token)
    monkeypatch.setattr(event_enrichment, "_cached", _miss)
    monkeypatch.setattr(event_enrichment, "_remember", _remember)
    monkeypatch.setattr(
        event_enrichment,
        "event_enricher",
        lambda _p: SimpleNamespace(
            applies=enricher.applies, cache_key=enricher.cache_key, fetch=fetch, retryable=enricher.retryable
        ),
    )


_AGENT = SimpleNamespace(integration_bindings={"github": [str(uuid.uuid4())]}, user_id=None, enabled=True)


def test_github_marks_completions_retryable_only():
    enricher = event_enricher("github")
    assert enricher.retryable is not None
    assert enricher.retryable("workflow_run.completed", _completion()) is True
    assert enricher.retryable("workflow_run.requested", {**_completion(), "action": "requested"}) is False
    assert enricher.retryable("push", {"after": "b" * 40}) is False


@pytest.mark.asyncio
async def test_a_timed_out_completion_is_marked_retryable(monkeypatch):
    async def _slow(_event_key, _payload, _token):
        raise TimeoutError()

    _patch_enrichment(monkeypatch, _slow)
    facts = await event_enrichment.enrichment_for(
        None, "github", "workflow_run.completed", _completion(), trigger_config=_CONFIG, agent=_AGENT
    )
    assert facts["unavailable"] and event_enrichment.enrichment_retryable(facts)


@pytest.mark.asyncio
async def test_a_permanent_failure_is_never_retried(monkeypatch):
    async def _capped(_event_key, _payload, _token):
        raise PermanentEnrichmentError("the commit has more than 1000 check runs")

    _patch_enrichment(monkeypatch, _capped)
    facts = await event_enrichment.enrichment_for(
        None, "github", "workflow_run.completed", _completion(), trigger_config=_CONFIG, agent=_AGENT
    )
    assert facts["unavailable"] and not event_enrichment.enrichment_retryable(facts)


@pytest.mark.asyncio
async def test_the_check_runs_cap_is_permanent(monkeypatch):
    async def _request(_client, _method, url, *_a, **_k):
        class _Full:
            def json(self):
                return {"total_count": 5000, "check_runs": [{"name": "x"}] * gh_enrichment.CHECKS_PER_PAGE}

        if url.endswith("/pulls"):

            class _Empty:
                def json(self):
                    return []

            return _Empty(), None
        return _Full(), None

    monkeypatch.setattr(gh_enrichment._common, "safe_request", _request)
    with pytest.raises(PermanentEnrichmentError):
        await gh_enrichment.fetch("workflow_run.completed", _completion(), "tok")


@pytest.mark.asyncio
async def test_the_receiver_defers_a_completion_it_could_not_judge(monkeypatch, fake_redis):
    async def _slow(_event_key, _payload, _token):
        raise TimeoutError()

    _patch_enrichment(monkeypatch, _slow)
    assignment_id = str(uuid.uuid4())
    reason = await webhooks._assignment_filter_reason(
        None,
        "github",
        "workflow_run.completed",
        _completion(),
        event="workflow_run",
        trigger_config=_CONFIG,
        agent=_AGENT,
        assignment_id=assignment_id,
    )
    assert reason == webhooks.DEFERRED_REASON
    assert len(fake_redis.z[retry.PENDING_KEY]) == 1
    # Without an assignment id (a caller that cannot defer) the drop stands, named.
    reason = await webhooks._assignment_filter_reason(
        None, "github", "workflow_run.completed", _completion(), event="workflow_run", trigger_config=_CONFIG,
        agent=_AGENT,
    )
    assert reason is not None and "could not be read" in reason


@pytest.mark.asyncio
async def test_a_filter_miss_is_never_deferred(monkeypatch, fake_redis):
    async def _pending(_event_key, _payload, _token):
        return {"pull_request": {"user": {"login": ME}}, "head": {"sha": "b" * 40, "checks": {"all_green": False}}}

    _patch_enrichment(monkeypatch, _pending)
    reason = await webhooks._assignment_filter_reason(
        None, "github", "workflow_run.completed", _completion(), event="workflow_run", trigger_config=_CONFIG,
        agent=_AGENT, assignment_id=str(uuid.uuid4()),
    )
    assert reason == "enriched.head.checks.all_green does not match the filter"
    assert not fake_redis.z.get(retry.PENDING_KEY)


@pytest.mark.asyncio
async def test_defer_is_bounded_and_one_entry_per_delivery(fake_redis):
    aid = uuid.uuid4()
    payload = _completion()
    assert await retry.defer(assignment_id=aid, provider="github", event_key="workflow_run.completed",
                             payload=payload, reason="r")
    assert await retry.defer(assignment_id=aid, provider="github", event_key="workflow_run.completed",
                             payload=payload, reason="r")
    assert len(fake_redis.z[retry.PENDING_KEY]) == 1
    assert not await retry.defer(assignment_id=aid, provider="github", event_key="workflow_run.completed",
                                 payload=payload, reason="r", attempt=retry.MAX_ATTEMPTS)


@pytest.mark.asyncio
async def test_claim_takes_only_due_entries_once(fake_redis):
    aid = uuid.uuid4()
    await retry.defer(assignment_id=aid, provider="github", event_key="workflow_run.completed",
                      payload=_completion(), reason="r")
    assert await retry._claim_due() == []  # not due yet
    member = next(iter(fake_redis.z[retry.PENDING_KEY]))
    fake_redis.z[retry.PENDING_KEY][member] = time.time() - 1
    entries = await retry._claim_due()
    assert [e["assignment_id"] for e in entries] == [str(aid)] and entries[0]["attempt"] == 1
    assert await retry._claim_due() == []


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


def _entry(aid, attempt=1) -> dict:
    return {
        "assignment_id": str(aid),
        "provider": "github",
        "event_key": "workflow_run.completed",
        "payload": _completion(),
        "receiver_integration_id": None,
        "trigger_context": {"type": "webhook", "scope": "provider", "provider": "github"},
        "attempt": attempt,
        "first_reason": "enriched.head.checks.all_green could not be read: TimeoutError",
    }


@pytest.mark.asyncio
async def test_a_deferred_completion_triggers_once_the_facts_read(monkeypatch, fake_redis):
    from app.services import assignment_engine

    aid = uuid.uuid4()
    wf = SimpleNamespace(id=aid, agent_id=uuid.uuid4(), trigger_type="integration_event", trigger_config=_CONFIG)
    started: list[dict] = []

    async def _facts(*_a, **_k):
        return _green()

    async def _trigger(**kwargs):
        started.append(kwargs)
        return "run-1"

    monkeypatch.setattr(event_enrichment, "enrichment_for", _facts)
    monkeypatch.setattr(assignment_engine, "trigger_assignment", _trigger)
    run_id = await retry._evaluate(_Db(wf, _AGENT), _entry(aid))
    assert run_id == "run-1"
    assert started[0]["skip_if_active"] is True
    assert started[0]["trigger_context"]["enrichment_retry"]["attempt"] == 1


@pytest.mark.asyncio
async def test_a_deferred_completion_still_unreadable_is_deferred_then_dropped(monkeypatch, fake_redis):
    from app.services import assignment_engine, integration_event_filters

    aid = uuid.uuid4()
    wf = SimpleNamespace(id=aid, agent_id=uuid.uuid4(), trigger_type="integration_event", trigger_config=_CONFIG)
    counted: list[int] = []

    async def _down(*_a, **_k):
        return {"unavailable": "TimeoutError", "retryable": True}

    async def _never(**_k):
        raise AssertionError("no run on an unreadable head")

    async def _count(_aid, n):
        counted.append(n)

    async def _reasons(_aid, _reasons):
        return None

    monkeypatch.setattr(event_enrichment, "enrichment_for", _down)
    monkeypatch.setattr(assignment_engine, "trigger_assignment", _never)
    monkeypatch.setattr(integration_event_filters, "record_filtered_events", _count)
    monkeypatch.setattr(integration_event_filters, "record_filter_reasons", _reasons)
    # The first further read fails: deferred again for the last attempt.
    assert await retry._evaluate(_Db(wf, _AGENT), _entry(aid, attempt=1)) is None
    assert len(fake_redis.z[retry.PENDING_KEY]) == 1 and counted == []
    # The last one fails too: dropped and counted like any filtered delivery.
    assert await retry._evaluate(_Db(wf, _AGENT), _entry(aid, attempt=retry.MAX_ATTEMPTS)) is None
    assert counted == [1]


@pytest.mark.asyncio
async def test_a_disabled_or_edited_assignment_takes_no_deferred_delivery(monkeypatch, fake_redis):
    from app.services import assignment_engine

    async def _never(**_k):
        raise AssertionError("no run")

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _never)
    aid = uuid.uuid4()
    assert await retry._evaluate(_Db(None, _AGENT), _entry(aid)) is None
    other = SimpleNamespace(
        id=aid, agent_id=uuid.uuid4(), trigger_type="integration_event", trigger_config={**_CONFIG, "provider": "slack"}
    )
    assert await retry._evaluate(_Db(other, _AGENT), _entry(aid)) is None

