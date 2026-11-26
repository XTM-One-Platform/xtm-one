"""#2235: a company-managed agent's timer reaches its own timer.due listener whoever the arming run acted for."""

from __future__ import annotations

import uuid

import pytest

from app.models.platform_timer import TIMER_FIRED, TIMER_FIRED_UNHEARD
from app.services import platform_events, platform_timers
from app.services.platform_timers import TIMER_DUE_EVENT, timer_due_data
from tests.test_platform_timers import _NOW, _others, _Session, _sub, _timer

_KEY = "ctem-loop-reminder:case-1"
_FILTER = {"correlation_key": {"starts_with": "ctem-loop-reminder:"}}


def _world(monkeypatch, *, company_managed, subs):
    """A timer of agent A armed for person U2, the given listeners live; (timer, owner U1)."""
    timer = _timer(correlation_key=_KEY)
    owner = uuid.uuid4()

    async def _subs(name):
        assert name == TIMER_DUE_EVENT
        return [s(timer, owner) for s in subs]

    monkeypatch.setattr(platform_events, "live_subscriptions", _subs)
    _others(monkeypatch, (), company_managed=company_managed)
    return timer, owner


def _own_listener(timer, owner):
    """The arming agent's own-scoped timer.due assignment, owned by U1, not by the person the run acted for."""
    return _sub(timer.agent_id, owner, _FILTER)


def _other_agent_listener(timer, owner):
    """Another agent's own-scoped listener on the same key: never in scope of this timer."""
    return _sub(uuid.uuid4(), owner, _FILTER)


@pytest.mark.parametrize(("company_managed", "personal_owner"), [(True, False), (False, True)])
def test_only_a_personal_agents_timer_is_a_personal_object(company_managed, personal_owner):
    timer = _timer(correlation_key=_KEY)
    data = timer_due_data(timer, shared=company_managed)
    assert (data["personal_owner_id"] == str(timer.user_id)) is personal_owner
    assert data["participant_agent_ids"] == [str(timer.agent_id)] and data["user_id"] == str(timer.user_id)


@pytest.mark.asyncio
async def test_a_company_managed_agents_timer_reaches_its_own_listener_owned_by_another_person(monkeypatch):
    timer, _owner = _world(monkeypatch, company_managed=True, subs=[_own_listener, _other_agent_listener])

    data = timer_due_data(timer, shared=True)
    in_scope, taking = await platform_timers.timer_listeners(data)

    # The agent's own listener hears it; another agent's own-scoped listener stays out of scope.
    assert [str(s.agent_id) for s in taking] == [str(timer.agent_id)]
    assert all(str(s.agent_id) == str(timer.agent_id) for s in in_scope)
    assert (
        await platform_timers.unheard_warning(platform_timers.serialize_timer(timer), user_id=str(timer.user_id))
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("company_managed", "state"), [(True, TIMER_FIRED), (False, TIMER_FIRED_UNHEARD)])
async def test_the_fire_is_heard_for_a_company_managed_agent_only(monkeypatch, company_managed, state):
    from app.core import database

    timer, _owner = _world(monkeypatch, company_managed=company_managed, subs=[_own_listener])
    session = _Session([timer])
    published: list = []

    async def _publish(name, data, *, event_id=None):
        published.append((name, data, event_id))
        return event_id

    async def _unless(_timer):
        return None

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(platform_events, "publish", _publish)
    monkeypatch.setattr(platform_timers, "unless_reason", _unless)

    assert await platform_timers.fire_due_timers(now=_NOW) == 1

    assert timer.state == state and len(published) == 1
    _name, data, _event_id = published[0]
    # What dispatch receives is what was judged: the agent's own listener is in scope exactly when shared.
    listener = _sub(timer.agent_id, _owner, _FILTER)
    assert (platform_events.mismatch_reason(listener, TIMER_DUE_EVENT, {**data, "event": TIMER_DUE_EVENT}) is None) is (
        company_managed
    )


@pytest.mark.asyncio
async def test_a_personal_agents_warning_names_the_owner_mismatch(monkeypatch):
    timer, owner = _world(monkeypatch, company_managed=False, subs=[_own_listener])

    async def _emails(ids):
        return [{str(timer.user_id): "acting@example.com", str(owner): "owner@example.com"}[i] for i in ids]

    monkeypatch.setattr(platform_timers, "user_emails", _emails)

    warning = await platform_timers.unheard_warning(platform_timers.serialize_timer(timer), user_id=str(timer.user_id))

    assert warning is not None and warning.startswith(f"Warning: the {TIMER_DUE_EVENT} assignment of this agent")
    assert "owner@example.com" in warning and "acting@example.com" in warning
    assert TIMER_FIRED_UNHEARD in warning
    # Not the old claim that the agent has no listener at all.
    assert "no enabled platform_event assignment" not in warning


@pytest.mark.asyncio
async def test_an_unreadable_company_managed_flag_keeps_the_timer_personal(monkeypatch):
    from app.core import database

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(database, "async_session", lambda: _Boom())
    assert await platform_timers.agent_is_company_managed(str(uuid.uuid4())) is False
    assert await platform_timers.agent_is_company_managed("not-a-uuid") is False
