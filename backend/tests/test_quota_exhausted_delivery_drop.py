"""A delivery for a quota-exhausted agent creates no run.

``_drop_delivery_over_quota`` is the pre-check both delivery entry points
(``trigger_assignment`` and ``trigger_or_queue_assignment``) run before any
run is written: a webhook or integration event whose acting user has spent the
agentic quota is counted as a filtered event with the reason
``quota_exceeded``, logged once per hour, and nothing else happens.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import assignment_engine as engine
from app.services import integration_event_filters as filters
from app.services import quota

pytestmark = pytest.mark.asyncio


def _assignment():
    return SimpleNamespace(
        id=uuid.uuid4(), name="PR review", run_as_user_id=None, user_id=uuid.uuid4()
    )


def _agent(*, builtin: bool = False):
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_builtin=builtin)


@pytest.fixture
def recorded(monkeypatch):
    seen: dict[str, list] = {"filtered": [], "reasons": [], "warnings": [], "checked": []}

    async def _filtered(assignment_id, count):
        seen["filtered"].append((assignment_id, count))

    async def _reasons(assignment_id, reasons):
        seen["reasons"].append((assignment_id, dict(reasons)))

    async def _log_once(assignment, error):
        seen["warnings"].append(error)

    monkeypatch.setattr(filters, "record_filtered_events", _filtered)
    monkeypatch.setattr(filters, "record_filter_reasons", _reasons)
    monkeypatch.setattr(engine, "_log_quota_drop_once", _log_once)
    seen["notices"] = []

    async def _notice(assignment, agent, error, *, where):
        seen["notices"].append((assignment.id, where))

    monkeypatch.setattr(engine, "_notify_quota_exhausted", _notice)
    return seen


def _quota(monkeypatch, recorded, outcome):
    async def _check(_db, user_id):
        recorded["checked"].append(user_id)
        if isinstance(outcome, BaseException):
            raise outcome

    monkeypatch.setattr(quota, "check_agentic_quota", _check)


async def test_a_webhook_for_a_quota_exhausted_agent_is_dropped_and_counted(monkeypatch, recorded):
    _quota(monkeypatch, recorded, quota.QuotaExceededError("agentic", 1000, 1000, "daily"))
    assignment, agent = _assignment(), _agent()
    context = {"type": "webhook"}

    assert await engine._drop_delivery_over_quota(None, assignment, agent, context) is True

    assert recorded["filtered"] == [(assignment.id, 1)]
    assert recorded["reasons"] == [(assignment.id, {"quota_exceeded": 1})]
    assert len(recorded["warnings"]) == 1
    # The acting identity: the assignment's owner, as the run would take.
    assert recorded["checked"] == [assignment.user_id]
    assert context[engine.QUOTA_DROPPED_CONTEXT] is True
    # The once-a-day notice is asked for on every drop; it dedupes itself.
    assert recorded["notices"] == [(assignment.id, "delivery")]


async def test_a_dropped_delivery_is_not_checked_nor_counted_twice(monkeypatch, recorded):
    """The busy fallback a receiver calls after ``trigger_assignment`` returned None."""
    _quota(monkeypatch, recorded, quota.QuotaExceededError("agentic", 2, 2, "daily"))
    assignment, agent = _assignment(), _agent()
    context = {"type": "integration_event"}

    assert await engine._drop_delivery_over_quota(None, assignment, agent, context) is True
    assert await engine._drop_delivery_over_quota(None, assignment, agent, context) is True

    assert recorded["filtered"] == [(assignment.id, 1)]
    assert len(recorded["checked"]) == 1


async def test_a_delivery_under_quota_goes_ahead(monkeypatch, recorded):
    _quota(monkeypatch, recorded, None)

    assert await engine._drop_delivery_over_quota(None, _assignment(), _agent(), {"type": "webhook"}) is False
    assert recorded["filtered"] == [] and recorded["reasons"] == []


@pytest.mark.parametrize("context", [None, {"type": "schedule"}, {"type": "manual"}, {"type": "platform_event"}])
async def test_other_triggers_are_never_pre_checked(monkeypatch, recorded, context):
    _quota(monkeypatch, recorded, quota.QuotaExceededError("agentic", 1, 1, "daily"))

    assert await engine._drop_delivery_over_quota(None, _assignment(), _agent(), context) is False
    assert recorded["checked"] == []


async def test_a_quota_store_that_cannot_answer_drops_nothing(monkeypatch, recorded):
    """The run's own check decides under QUOTA_FAIL_MODE."""
    _quota(monkeypatch, recorded, ConnectionError("redis down"))

    assert await engine._drop_delivery_over_quota(None, _assignment(), _agent(), {"type": "webhook"}) is False
    assert recorded["filtered"] == []


async def test_an_exempt_builtin_delivery_is_not_pre_checked(monkeypatch, recorded):
    _quota(monkeypatch, recorded, quota.QuotaExceededError("agentic", 1, 1, "daily"))
    context = {"type": "webhook", "quota_exempt": True}

    assert await engine._drop_delivery_over_quota(None, _assignment(), _agent(builtin=True), context) is False
    assert recorded["checked"] == []


async def test_trigger_or_queue_answers_dropped_with_the_quota_reason(monkeypatch):
    async def _allowed(_assignment):
        return None

    async def _not_paused(_db, _assignment):
        return None

    async def _drop(*_args, **_kwargs):
        return True

    monkeypatch.setattr(engine, "_refuse_if_ai_disabled", _allowed)
    monkeypatch.setattr(engine, "refuse_if_owner_deactivated", _not_paused)
    monkeypatch.setattr(engine, "_drop_delivery_over_quota", _drop)
    assignment = SimpleNamespace(enabled=True, name="a", id=uuid.uuid4())
    agent = SimpleNamespace(enabled=True, name="b")

    result = await engine.trigger_or_queue_assignment(
        assignment, agent, {}, db=None, trigger_context={"type": "webhook"}
    )

    assert result.status == "dropped"
    assert result.reason == engine.QUOTA_DROP_REASON
    assert result.run_id is None


async def test_the_drop_warning_is_logged_once_per_hour(monkeypatch, caplog):
    store: dict[str, str] = {}

    class _Redis:
        async def set(self, key, value, nx=False, ex=None):
            if nx and key in store:
                return None
            store[key] = value
            return True

    async def _client():
        return _Redis()

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    assignment = _assignment()
    with caplog.at_level("WARNING", logger=engine.logger.name):
        for _ in range(5):
            await engine._log_quota_drop_once(assignment, "agentic quota exceeded: 2/2 (daily)")

    assert sum("quota is exhausted" in r.getMessage() for r in caplog.records) == 1
