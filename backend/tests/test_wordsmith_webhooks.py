"""Wordsmith webhook endpoints for integration_event triggers (#160, slice 3)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import trigger_provisioning
from app.services import wordsmith_webhooks as ws

NOW = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)

# The vector of https://wordsmithai.mintlify.app/webhooks (Validating webhook signatures).
DOC_SECRET = "whsec_test_secret_123"
DOC_BODY = b'{"id":"test","status":"completed"}'
DOC_T = 1234567890
DOC_V1 = "c60c0cc7241d79e8bf2a88fdc6ce257c2fd547048bb244495309b27ad07884bf"


def test_the_docs_vector_signs_and_verifies():
    assert ws.signature(DOC_SECRET, DOC_T, DOC_BODY) == DOC_V1
    assert ws.verify_signature(DOC_BODY, f"t={DOC_T},v1={DOC_V1}", [DOC_SECRET], now=DOC_T + 5)


def test_a_rotating_header_with_two_v1_passes_on_either_secret():
    old = ws.signature("whsec_old", DOC_T, DOC_BODY)
    new = ws.signature("whsec_new", DOC_T, DOC_BODY)
    header = f"t={DOC_T},v1={new},v1={old}"
    assert ws.verify_signature(DOC_BODY, header, ["whsec_old"], now=DOC_T)
    assert ws.verify_signature(DOC_BODY, header, ["whsec_new"], now=DOC_T)
    assert not ws.verify_signature(DOC_BODY, header, ["whsec_other"], now=DOC_T)


@pytest.mark.parametrize(
    "header, now",
    [
        (f"t={DOC_T},v1={DOC_V1}", DOC_T + 61),
        (f"t={DOC_T},v1={DOC_V1}", DOC_T - 61),
        (f"v1={DOC_V1}", DOC_T),
        (f"t={DOC_T}", DOC_T),
        (f"t=abc,v1={DOC_V1}", DOC_T),
        ("", DOC_T),
        (None, DOC_T),
    ],
)
def test_a_stale_missing_or_malformed_signature_is_refused(header, now):
    assert not ws.verify_signature(DOC_BODY, header, [DOC_SECRET], now=now)


def test_a_tampered_body_is_refused():
    assert not ws.verify_signature(DOC_BODY + b" ", f"t={DOC_T},v1={DOC_V1}", [DOC_SECRET], now=DOC_T)


def test_no_secret_refuses_every_delivery():
    assert not ws.verify_signature(DOC_BODY, f"t={DOC_T},v1={DOC_V1}", [], now=DOC_T)
    assert not ws.verify_signature(DOC_BODY, f"t={DOC_T},v1={DOC_V1}", [""], now=DOC_T)


EVENT = {
    "id": "8d2f4a61-3c7e-4b09-a5d1-9e6f0b2c7a48",
    "type": "task.assigned",
    "api_version": "2026-11-01",
    "created_at": "2026-11-09T10:12:03Z",
    "resource": {"type": "task", "id": "5f0c9a12"},
    "actor": {"kind": "user", "id": "1a2b3c4d"},
    "data": {
        "task_id": "5f0c9a12",
        "status": "open",
        "assignee_id": "9c1d2e3f",
        "team_id": None,
        "agent_id": None,
        "url": "https://app.wordsmith.ai/tasks?activeId=5f0c9a12",
    },
}


def test_an_event_payload_lifts_ids_for_the_filter_and_names_the_read_tool():
    payload = ws.event_payload(EVENT)
    assert payload["message_id"] == EVENT["id"]
    assert payload["type"] == "task.assigned"
    assert payload["task_id"] == "5f0c9a12" and payload["assignee_id"] == "9c1d2e3f" and payload["status"] == "open"
    assert payload["resource_type"] == "task" and payload["actor_kind"] == "user"
    assert payload["data"] == EVENT["data"]
    assert payload["follow_up_tool"] == "get_wordsmith_tasks operation=get_task"


def test_a_body_that_is_not_an_event_gives_no_payload():
    assert ws.event_payload({"id": "x"}) is None
    assert ws.event_payload({"type": "task.created"}) is None


def test_the_trigger_filter_reads_the_lifted_keys():
    from app.services.integration_event_filters import event_filter_reason

    payload = ws.event_payload(EVENT)
    assert event_filter_reason("task.assigned", payload, {"assignee_id": "9c1d2e3f"}) is None
    assert event_filter_reason("task.assigned", payload, {"assignee_id": "someone-else"}) is not None


@pytest.mark.parametrize(
    "event, audience, refused",
    [
        ("alert.triggered", "user", True),
        ("alert.triggered", "customer", False),
        ("operation.succeeded", "customer", True),
        ("input_request.created", "customer", True),
        ("task.created", "customer", False),
        ("task.created", "user", False),
    ],
)
def test_the_audience_rules_of_wordsmith(event, audience, refused):
    assert (ws.audience_error(event, audience) is not None) is refused


def test_the_catalog_lists_every_event_as_a_webhook_and_the_provisioner_is_registered():
    events = {e["event"]: e for e in PROVIDER_CATALOG["wordsmith"]["available_events"]}
    assert set(events) == set(ws.EVENTS)
    assert {e["mechanism"] for e in events.values()} == {"webhook"}
    assert {"type", "status", "agent_id", "assignee_id", "task_id"} <= set(events["task.created"]["filter_keys"])
    assert trigger_provisioning._PROVISIONERS["wordsmith"] == "app.services.wordsmith_webhooks"


class FakeWordsmith:
    """Records calls; answers like the webhook endpoint routes."""

    def __init__(self, *, endpoint: dict | None = None, fail_create: bool = False, events_pages=None):
        self.calls: list[tuple[str, str, object, object]] = []
        self.endpoint = endpoint
        self.fail_create = fail_create
        self.events_pages = list(events_pages or [])

    async def __call__(self, method, path, body, query):
        self.calls.append((method, path, body, query))
        if method == "GET" and path.startswith("/webhook_endpoints/"):
            if self.endpoint is None:
                return None, 404, "Error: resource_not_found"
            return dict(self.endpoint), 200, None
        if method == "PATCH":
            return {"status": "enabled"}, 200, None
        if method == "DELETE":
            return {}, 204, None
        if method == "POST" and path == "/webhook_endpoints":
            if self.fail_create:
                return None, 409, "Error: webhook_endpoint_limit_reached"
            return {"id": "ep-new", "status": "enabled", "secret": "whsec_new_once"}, 201, None
        if method == "GET" and path == "/events":
            return (self.events_pages.pop(0) if self.events_pages else {"items": [], "cursor": None}), 200, None
        raise AssertionError((method, path))


def _encrypt(value: str) -> str:
    return f"enc:{value}"


async def _reconcile(fake, state=None, **overrides):
    kwargs = {
        "url": "https://xtm.example.com/api/hooks/integrations/i-1",
        "event_types": ["task.created"],
        "audience": "user",
        "flow_name": "Legal intake",
        "flow_id": "a-1",
        "now": NOW,
        "encrypt": _encrypt,
    }
    kwargs.update(overrides)
    return await ws.reconcile(fake, state, **kwargs)


@pytest.mark.asyncio
async def test_a_new_trigger_creates_an_integration_endpoint_and_stores_the_secret_encrypted():
    fake = FakeWordsmith()
    state = await _reconcile(fake)
    method, path, body, _ = fake.calls[-1]
    assert (method, path) == ("POST", "/webhook_endpoints")
    assert body["integration"] == {"platform": "other", "flow_id": "a-1", "flow_name": "Legal intake"}
    assert body["event_types"] == ["task.created"] and body["audience"] == "user"
    assert body["send_test_event"] is False
    assert state["status"] == "registered" and state["endpoint_id"] == "ep-new"
    assert state["secret_enc"] == "enc:whsec_new_once"
    assert "whsec_new_once" not in json.dumps({k: v for k, v in state.items() if k != "secret_enc"})


@pytest.mark.asyncio
async def test_an_enabled_endpoint_is_kept():
    held = await _reconcile(FakeWordsmith())
    fake = FakeWordsmith(endpoint={"id": "ep-new", "status": "enabled"})
    state = await _reconcile(fake, held)
    assert [c[0] for c in fake.calls] == ["GET"]
    assert state["endpoint_id"] == "ep-new" and state["status"] == "registered"


@pytest.mark.asyncio
async def test_a_failing_endpoint_is_turned_back_on():
    held = await _reconcile(FakeWordsmith())
    fake = FakeWordsmith(endpoint={"id": "ep-new", "status": "failing", "disabled_reason": None})
    state = await _reconcile(fake, held)
    assert [c[0] for c in fake.calls] == ["GET", "PATCH"]
    assert fake.calls[1][2] == {"enabled": True}
    assert state["status"] == "registered"


@pytest.mark.asyncio
async def test_a_revoked_or_missing_endpoint_is_created_again():
    held = await _reconcile(FakeWordsmith())
    revoked = FakeWordsmith(endpoint={"id": "ep-new", "status": "disabled", "disabled_reason": "credential_revoked"})
    state = await _reconcile(revoked, held)
    assert [c[0] for c in revoked.calls] == ["GET", "DELETE", "POST"]
    assert state["status"] == "registered"
    missing = FakeWordsmith(endpoint=None)
    state = await _reconcile(missing, held)
    assert [c[0] for c in missing.calls] == ["GET", "DELETE", "POST"]


@pytest.mark.asyncio
async def test_a_changed_event_type_replaces_the_endpoint():
    held = await _reconcile(FakeWordsmith())
    fake = FakeWordsmith(endpoint={"id": "ep-new", "status": "enabled"})
    await _reconcile(fake, held, event_types=["review.completed"])
    assert [c[0] for c in fake.calls] == ["DELETE", "POST"]


@pytest.mark.asyncio
async def test_a_refused_creation_keeps_the_reason():
    state = await _reconcile(FakeWordsmith(fail_create=True))
    assert state["status"] == "error" and "webhook_endpoint_limit_reached" in state["error"]
    assert state["endpoint_id"] is None


@pytest.mark.asyncio
async def test_catch_up_pages_the_event_log_from_the_last_event():
    fake = FakeWordsmith(
        events_pages=[
            {"items": [EVENT], "cursor": "c-2"},
            {"items": [{**EVENT, "id": "e-2"}], "cursor": None},
        ]
    )
    events, err = await ws.catch_up(
        fake, event_types=["task.assigned"], audience="customer", since="2026-11-18T19:00:00Z"
    )
    assert err is None and [e["id"] for e in events] == [EVENT["id"], "e-2"]
    first, second = fake.calls[0][3], fake.calls[1][3]
    assert first["created_after"] == "2026-11-18T19:00:00Z" and first["audience"] == "customer"
    assert second["cursor"] == "c-2" and "created_after" not in second


def test_a_registered_endpoint_is_rechecked_after_six_hours_and_an_error_after_one():
    registered = {"status": "registered", "checked_at": (NOW - timedelta(hours=5)).isoformat()}
    failed = {"status": "error", "checked_at": (NOW - timedelta(hours=1, minutes=1)).isoformat()}
    assert not ws.due(registered, NOW)
    assert ws.due(failed, NOW)
    assert ws.due(None, NOW)


def test_the_stored_secret_is_read_back_decrypted():
    from app.core.security import encrypt_value

    assignment = SimpleNamespace(trigger_config={ws.STATE_KEY: {"secret_enc": encrypt_value(DOC_SECRET)}})
    assert ws.endpoint_secret(assignment) == DOC_SECRET
    assert ws.endpoint_secret(SimpleNamespace(trigger_config={})) == ""


def test_a_signed_delivery_verifies_end_to_end_with_the_stored_secret():
    from app.core.security import encrypt_value

    body = json.dumps(EVENT).encode()
    t = int(NOW.timestamp())
    header = f"t={t},v1={hmac.new(DOC_SECRET.encode(), f'{t}.'.encode() + body, hashlib.sha256).hexdigest()}"
    assignment = SimpleNamespace(trigger_config={ws.STATE_KEY: {"secret_enc": encrypt_value(DOC_SECRET)}})
    assert ws.verify_signature(body, header, [ws.endpoint_secret(assignment)], now=t)
