"""HubSpot integration-event entity-type (CRM object) trigger filtering.

A HubSpot ``integration_event`` assignment can be scoped to specific CRM
entity types via ``trigger_config.filter.scope.crm_object_type`` (the same
generic, catalog-driven mechanism GitHub uses for repository scoping). The
webhook matcher derives the object type from the ``subscriptionType`` prefix
(e.g. ``deal.creation`` -> ``deal``) and:

  * fires every event when no scope is configured (empty / missing list =
    today's behavior, all entity types);
  * fires only events whose object type is in the configured scope otherwise.

Both webhook entry points enforce the filter: ``_handle_hubspot_webhook``
(integration fan-out to all bound assignments) and
``_handle_hubspot_assignment_webhook`` (dedicated per-assignment URL).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import webhooks

_EVENTS = [
    {"subscriptionType": "deal.creation", "objectId": 1, "eventId": 11},
    {"subscriptionType": "contact.creation", "objectId": 2, "eventId": 12},
]


def _make_request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, url="http://test/webhook", method="POST")


def _make_integration() -> SimpleNamespace:
    # No ``extra_config_enc`` -> signature verification is skipped entirely.
    return SimpleNamespace(id=uuid.uuid4(), provider="hubspot", extra_config_enc=None)


def _make_agent() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name="Sales Agent", enabled=True)


def _make_assignment(agent_id, trigger_config: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="New CRM entity",
        trigger_type="integration_event",
        enabled=True,
        trigger_config=trigger_config,
        user_id=None,
        is_company_managed=False,
    )


class _FakeResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._items))

    def all(self):
        # The agent query's ``(agent, unreadable)`` rows.
        return [(item, False) for item in self._items]

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _FakeDB:
    """Returns canned results in the order the handler issues queries."""

    def __init__(self, results):
        self._results = list(results)
        self.committed = False

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0)

    async def commit(self):
        self.committed = True


def _patch_session(monkeypatch, db: _FakeDB):
    class _SessionCtx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())


def _patch_trigger(monkeypatch) -> list[str]:
    """Record the object_type of every triggered run; return the call log."""
    triggered_object_types: list[str] = []

    async def _fake_trigger(
        *, assignment, agent, payload, db, trigger_context, skip_if_active=False
    ):
        triggered_object_types.append(payload["_hubspot_event"]["object_type"])
        return f"run-{len(triggered_object_types)}"

    monkeypatch.setattr(webhooks, "trigger_assignment", _fake_trigger)
    return triggered_object_types


# ---------------------------------------------------------------------------
# Integration fan-out handler: _handle_hubspot_webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fanout_empty_scope_fires_every_entity_type(monkeypatch):
    """No ``crm_object_type`` scope -> both the deal and the contact event
    trigger (unchanged, all-types behavior)."""
    agent = _make_agent()
    wf = _make_assignment(agent.id, {"provider": "hubspot", "event": "entity_created"})
    db = _FakeDB([_FakeResult([agent]), _FakeResult([wf])])
    _patch_session(monkeypatch, db)
    triggered = _patch_trigger(monkeypatch)

    result = await webhooks._handle_hubspot_webhook(
        integration_id=str(uuid.uuid4()),
        integration=_make_integration(),
        body=b"[]",
        events=list(_EVENTS),
        request=_make_request(),
    )

    assert triggered == ["deal", "contact"]
    assert result["count"] == 2
    assert db.committed is True


@pytest.mark.asyncio
async def test_fanout_scope_suppresses_non_matching_entity_types(monkeypatch):
    """A ``crm_object_type`` scope of ``["deal"]`` -> only the deal event
    triggers; the contact event is suppressed."""
    agent = _make_agent()
    wf = _make_assignment(
        agent.id,
        {
            "provider": "hubspot",
            "event": "entity_created",
            "filter": {"scope": {"crm_object_type": ["deal"]}},
        },
    )
    db = _FakeDB([_FakeResult([agent]), _FakeResult([wf])])
    _patch_session(monkeypatch, db)
    triggered = _patch_trigger(monkeypatch)

    result = await webhooks._handle_hubspot_webhook(
        integration_id=str(uuid.uuid4()),
        integration=_make_integration(),
        body=b"[]",
        events=list(_EVENTS),
        request=_make_request(),
    )

    assert triggered == ["deal"]
    assert result["count"] == 1


# ---------------------------------------------------------------------------
# Per-assignment handler: _handle_hubspot_assignment_webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assignment_empty_scope_fires_every_entity_type(monkeypatch):
    """Dedicated per-assignment URL with no scope -> both events trigger."""
    agent = _make_agent()
    wf = _make_assignment(agent.id, {"provider": "hubspot", "event": "entity_created"})
    db = _FakeDB([_FakeResult([wf]), _FakeResult([agent])])
    _patch_session(monkeypatch, db)
    triggered = _patch_trigger(monkeypatch)

    result = await webhooks._handle_hubspot_assignment_webhook(
        integration_id=str(uuid.uuid4()),
        integration=_make_integration(),
        body=b"[]",
        events=list(_EVENTS),
        assignment_id=str(wf.id),
        request=_make_request(),
    )

    assert triggered == ["deal", "contact"]
    assert result["count"] == 2
    assert db.committed is True


@pytest.mark.asyncio
async def test_assignment_scope_suppresses_non_matching_entity_types(monkeypatch):
    """Dedicated per-assignment URL with a ``["contact"]`` scope -> only the
    contact event triggers."""
    agent = _make_agent()
    wf = _make_assignment(
        agent.id,
        {
            "provider": "hubspot",
            "event": "entity_created",
            "filter": {"scope": {"crm_object_type": ["contact"]}},
        },
    )
    db = _FakeDB([_FakeResult([wf]), _FakeResult([agent])])
    _patch_session(monkeypatch, db)
    triggered = _patch_trigger(monkeypatch)

    result = await webhooks._handle_hubspot_assignment_webhook(
        integration_id=str(uuid.uuid4()),
        integration=_make_integration(),
        body=b"[]",
        events=list(_EVENTS),
        assignment_id=str(wf.id),
        request=_make_request(),
    )

    assert triggered == ["contact"]
    assert result["count"] == 1
