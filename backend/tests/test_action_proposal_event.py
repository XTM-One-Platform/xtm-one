"""Live `action_proposal` SSE event (FR-007).

The event exists so a reviewer already watching a run sees the approval request
appear rather than waiting for the next poll. It is a convenience, not a
correctness mechanism — the proposals are persisted on the run, so a dropped
event costs live rendering and nothing else.

What matters here is routing. It mirrors ``_publish_run_status`` exactly:
both describe the same run, so a divergence would deliver the approval prompt to
people who cannot act on it, or fail to deliver it to the one who can.
"""

from __future__ import annotations

import json

import pytest

import app.services.assignment_helpers as helpers

PROPOSALS = [
    {
        "tool_call_id": "call_A",
        "tool_name": "delete_entity",
        "tool_description": "Delete an entity.",
        "arguments": {"entity_id": "e-1"},
        "input_schema": {"type": "object", "properties": {}},
        "source": "integration:opencti",
    }
]


class _FakeRedis:
    def __init__(self, fail: bool = False):
        self.published: list[tuple[str, str]] = []
        self._fail = fail

    async def publish(self, channel, payload):
        if self._fail:
            raise RuntimeError("redis down")
        self.published.append((channel, payload))


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(helpers, "redis_client", _client)
    return fake


async def _publish(redis_unused=None, **overrides):
    kwargs = {
        "user_id": "user-1",
        "run_id": "run-1",
        "assignment_name": "Nightly enrichment",
        "proposals": PROPOSALS,
    }
    kwargs.update(overrides)
    await helpers.publish_action_proposal(
        kwargs.pop("user_id"),
        kwargs.pop("run_id"),
        kwargs.pop("assignment_name"),
        kwargs.pop("proposals"),
        **kwargs,
    )


# --- payload ------------------------------------------------------------


async def test_payload_matches_the_contract(redis):
    await _publish()

    _channel, raw = redis.published[0]
    payload = json.loads(raw)
    assert payload["type"] == "action_proposal"
    assert payload["run_id"] == "run-1"
    assert payload["assignment_name"] == "Nightly enrichment"
    assert payload["proposals"] == PROPOSALS


async def test_type_field_is_how_clients_discriminate(redis):
    """The SSE endpoint is a raw passthrough with no event: names.

    Every consumer switches on this field, so it is the whole routing contract.
    """
    await _publish()

    assert json.loads(redis.published[0][1])["type"] == "action_proposal"


async def test_autonomous_flag_is_carried(redis):
    """Lets the UI mark a proposal nobody is actively waiting on."""
    await _publish(is_autonomous=True)
    assert json.loads(redis.published[0][1])["is_autonomous"] is True

    redis.published.clear()
    await _publish(is_autonomous=False)
    assert json.loads(redis.published[0][1])["is_autonomous"] is False


# --- routing (mirrors _publish_run_status) ------------------------------


async def test_personal_run_goes_to_the_owners_channel(redis):
    await _publish(user_id="user-42")
    assert redis.published[0][0] == "activity:user-42"


async def test_company_managed_run_broadcasts(redis):
    """A single owner's channel would not reach the people meant to see it."""
    await _publish(user_id="user-42", is_company=True)
    assert redis.published[0][0] == "activity:company"


async def test_owner_less_run_falls_back_to_broadcast(redis):
    """Dropping the event entirely would be worse than over-delivering it."""
    await _publish(user_id="")
    assert redis.published[0][0] == "activity:company"


async def test_string_none_user_id_is_treated_as_absent(redis):
    """``str(run.user_id)`` yields "None" for an owner-less run.

    Publishing to ``activity:None`` would silently reach nobody.
    """
    await _publish(user_id="None")
    assert redis.published[0][0] == "activity:company"


async def test_routing_matches_run_status_publishing(redis):
    """Pin the mirror explicitly — the two must not drift apart."""
    await helpers._publish_run_status(
        "user-7", "run-1", "awaiting_human", "A", is_company=False
    )
    status_channel = redis.published[-1][0]

    await _publish(user_id="user-7")
    assert redis.published[-1][0] == status_channel


# --- failure is survivable ----------------------------------------------


async def test_publish_failure_does_not_raise(monkeypatch):
    """A dropped event must never cost the pause it is announcing.

    The proposals are persisted on the run, so the reviewer still finds them.
    """
    failing = _FakeRedis(fail=True)

    async def _client():
        return failing

    monkeypatch.setattr(helpers, "redis_client", _client)

    await _publish()  # must not raise
