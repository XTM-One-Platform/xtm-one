"""Flow triggers that start no run lose nothing and say why.

* The integration-event poll dispatches one run at a time per flow. An event
  polled while a run is active is no longer used up: it goes back unseen with
  the provider cursor, like an event held back while agentic AI is
  unavailable, and fires once on a later poll - never dropped, never twice.
* A webhook delivery that starts no run keeps its 429 and ``Retry-After``,
  worded after the reason: a run already active, agentic AI unavailable, or an
  ownerless flow - no longer "already active" for all three.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_background_edition_gate import COMMUNITY, ENTERPRISE, _pin, _Session
from tests.test_edition_gate_followups import (
    _EVENT_NODE,
    _FakeRedis,
    _flow,
    _integration,
    _patch_flow_triggers,
)

# ── The poll: one run at a time, every event exactly once ──────────────────

_EVENTS = [
    {"message_id": f"m-{i}", "created_at": f"2026-09-27T01:00:0{i}Z"} for i in (1, 2, 3)
]


def _patch_provider(monkeypatch, redis: _FakeRedis) -> None:
    """A provider that returns what arrived after its cursor and advances it."""
    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr("app.core.security.decrypt_value", lambda _value: "token")
    monkeypatch.setattr(
        "app.core.database.release_db_connection_before_loop", AsyncMock()
    )

    async def _poll(_provider, _event_type, _token, target, redis_, _seen, _http):
        key = f"event_cursor:{target.id}"
        cursor = redis_.values.get(key) or ""
        fresh = [event for event in _EVENTS if event["created_at"] > cursor]
        if fresh:
            redis_.values[key] = max(event["created_at"] for event in fresh)
        return [dict(event) for event in fresh]

    monkeypatch.setattr("app.services.event_poller._poll_provider", _poll)


def _patch_runs(monkeypatch, *, active: bool) -> SimpleNamespace:
    """A flow run that stays active until the test ends it."""
    from app.services import flow_triggers

    state = SimpleNamespace(active=active, fired=[])

    async def _has_active_run(_db, _flow_id):
        return state.active

    async def _create(_db, _flow, **kwargs):
        state.fired.append(kwargs["trigger_payload"]["payload"]["message_id"])
        state.active = True
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(flow_triggers, "has_active_run", _has_active_run)
    monkeypatch.setattr(flow_triggers, "create_flow_run", _create)
    return state


@pytest.mark.asyncio
async def test_events_polled_while_a_run_is_active_all_fire_exactly_once(monkeypatch):
    from app.services import flow_triggers

    _pin(monkeypatch, **ENTERPRISE)
    flow = _flow(_EVENT_NODE)
    target = f"flow:{flow.id}:{_EVENT_NODE['id']}"
    redis = _FakeRedis()
    _patch_flow_triggers(monkeypatch, flow, _EVENT_NODE)
    _patch_provider(monkeypatch, redis)
    runs = _patch_runs(monkeypatch, active=True)

    async def _poll() -> None:
        redis.values.pop(f"event_poll:{target}", None)  # the poll interval elapsed
        await flow_triggers.poll_flow_integration_events(_Session([_integration()]))

    # Three events arrive while a run is active: none is used up.
    await _poll()
    assert runs.fired == []
    assert redis.sets.get(f"event_seen_ids:{target}", set()) == set()

    # Each later poll fires the next event once the previous run has ended.
    runs.active = False
    await _poll()
    assert runs.fired == ["m-1"]
    await _poll()  # m-1's run is still active: nothing fires, nothing is lost
    assert runs.fired == ["m-1"]
    for expected in (["m-1", "m-2"], ["m-1", "m-2", "m-3"]):
        runs.active = False
        await _poll()
        assert runs.fired == expected

    # Nothing fires twice.
    runs.active = False
    await _poll()
    await _poll()
    assert runs.fired == ["m-1", "m-2", "m-3"]
    assert redis.sets[f"event_seen_ids:{target}"] == {"m-1", "m-2", "m-3"}


@pytest.mark.asyncio
async def test_a_poll_with_no_active_run_still_fires_one_run_per_event(monkeypatch):
    """Unchanged when the flow is idle between events: the first event fires,
    and the rest wait for its run instead of being consumed by the guard."""
    from app.services import flow_triggers

    _pin(monkeypatch, **ENTERPRISE)
    flow = _flow(_EVENT_NODE)
    target = f"flow:{flow.id}:{_EVENT_NODE['id']}"
    redis = _FakeRedis()
    _patch_flow_triggers(monkeypatch, flow, _EVENT_NODE)
    _patch_provider(monkeypatch, redis)
    runs = _patch_runs(monkeypatch, active=False)

    assert await flow_triggers.poll_flow_integration_events(_Session([_integration()])) == 1
    assert runs.fired == ["m-1"]
    assert redis.sets[f"event_seen_ids:{target}"] == {"m-1"}
    assert redis.values.get(f"event_cursor:{target}") is None, "cursor given back"


# ── The webhook: same 429, worded after the reason ─────────────────────────

_WEBHOOK_NODE = {"id": "w1", "kind": "trigger.webhook", "config": {"path": "alerts"}}


@pytest.mark.asyncio
async def test_a_webhook_outcome_says_why_no_run_was_started(monkeypatch):
    from app.services import flow_triggers

    flow = _flow(_WEBHOOK_NODE)
    created, _own = _patch_flow_triggers(monkeypatch, flow, _WEBHOOK_NODE)

    async def _outcome(db):
        return await flow_triggers.trigger_flow_from_webhook_outcome(
            db, flow, _WEBHOOK_NODE, payload={}, headers={}
        )

    _pin(monkeypatch, **ENTERPRISE)
    monkeypatch.setattr(flow_triggers, "webhook_run_admitted", AsyncMock(return_value=False))
    assert (await _outcome(_Session())).skipped == flow_triggers.WEBHOOK_SKIP_ACTIVE_RUN

    monkeypatch.setattr(flow_triggers, "webhook_run_admitted", AsyncMock(return_value=True))
    _pin(monkeypatch, **COMMUNITY)
    assert (await _outcome(_Session([None, None]))).skipped == (
        flow_triggers.WEBHOOK_SKIP_AI_UNAVAILABLE
    )

    ownerless = _flow(_WEBHOOK_NODE)
    ownerless.user_id = None
    outcome = await flow_triggers.trigger_flow_from_webhook_outcome(
        _Session(), ownerless, _WEBHOOK_NODE, payload={}, headers={}
    )
    assert outcome.skipped == flow_triggers.WEBHOOK_SKIP_OWNERLESS

    _pin(monkeypatch, **ENTERPRISE)
    outcome = await _outcome(_Session())
    assert outcome.skipped is None and outcome.run_id is not None
    created.assert_awaited_once()
    # The old entry point is unchanged: the run id, or None.
    assert await flow_triggers.trigger_flow_from_webhook(
        _Session(), flow, _WEBHOOK_NODE, payload={}, headers={}
    ) is not None


def test_the_429_wording_says_what_happened():
    from app.api.v1 import webhooks
    from app.services import flow_triggers

    active = webhooks.flow_webhook_skip_detail(flow_triggers.WEBHOOK_SKIP_ACTIVE_RUN)
    ai = webhooks.flow_webhook_skip_detail(flow_triggers.WEBHOOK_SKIP_AI_UNAVAILABLE)
    ownerless = webhooks.flow_webhook_skip_detail(flow_triggers.WEBHOOK_SKIP_OWNERLESS)

    # The overlap wording is unchanged; the other two no longer claim it.
    assert active == "A run of this flow is already active — retry shortly."
    assert "agentic AI is not available" in ai
    assert "refused" in ai and "retry later" in ai
    assert "no owner" in ownerless and "refused" in ownerless
    for detail in (ai, ownerless):
        assert "already active" not in detail
    # One wording per reason the trigger reports, none left to the fallback.
    reasons = {
        flow_triggers.WEBHOOK_SKIP_ACTIVE_RUN,
        flow_triggers.WEBHOOK_SKIP_AI_UNAVAILABLE,
        flow_triggers.WEBHOOK_SKIP_OWNERLESS,
    }
    assert set(webhooks._FLOW_WEBHOOK_SKIP_DETAILS) == reasons


def test_the_route_keeps_its_status_and_retry_after():
    from app.api.v1 import webhooks

    source = inspect.getsource(webhooks.receive_flow_webhook)
    assert "status_code=429" in source
    assert 'headers={"Retry-After": "30"}' in source
    assert "flow_webhook_skip_detail(outcome.skipped)" in source
