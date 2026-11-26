"""Drata workflow webhooks for integration_event triggers (#2185, Drata slice)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import drata_webhooks as dw

TOKEN = "drata-test-token"
EVIDENCE = {
    "event_type": "LINKED_TO_CONTROL",
    "timestamp": "2026-11-25T21:00:00Z",
    "organization_id": "org-1",
    "workspace": {"id": 7, "name": "Primary"},
    "evidence_name": "Access review Q4",
    "evidence_linked_control_code": "DCF-12",
}
INTEGRATION = "00000000-0000-4000-8000-000000000000"


@pytest.mark.parametrize(
    ("event_type", "event"),
    [
        ("CONTROL_EVIDENCE_LINKED", "control_changed"),
        ("control_test_changed_to_fail", "control_changed"),
        ("RENEWAL_PAST_DUE", "evidence_changed"),
        ("RESIDUAL_SCORE_UPDATED", "risk_changed"),
        ("STATUS_CHANGED", "personnel_changed"),
    ],
)
def test_every_drata_trigger_name_maps_to_its_family(event_type, event):
    assert dw.event_of({"event_type": event_type}) == event


def test_an_unknown_trigger_name_falls_back_on_the_property_family():
    assert dw.event_of({"event_type": "CONTROL_SOMETHING_NEW"}) == "control_changed"
    assert dw.event_of({"event_type": "NEW_ONE", "risk_name": "r"}) == "risk_changed"
    assert dw.event_of({"event_type": "NEW_ONE", "personnel_email": "a@b.c"}) == "personnel_changed"
    assert dw.event_of({"event_type": "NEW_ONE"}) is None


def test_the_bearer_token_is_required_and_compared_in_full():
    tokens = dw.webhook_tokens({dw.TOKEN_FIELD: f" old , {TOKEN} "})
    assert tokens == ["old", TOKEN]
    assert dw.verify_bearer(f"Bearer {TOKEN}", tokens)
    assert dw.verify_bearer(f"bearer {TOKEN}", tokens)
    for header in (None, "", TOKEN, f"Basic {TOKEN}", "Bearer ", f"Bearer {TOKEN}x"):
        assert not dw.verify_bearer(header, tokens)


def test_no_configured_token_refuses_every_delivery():
    assert dw.webhook_tokens({}) == []
    assert dw.webhook_tokens({dw.TOKEN_FIELD: " , "}) == []
    assert not dw.verify_bearer(f"Bearer {TOKEN}", [])


def test_the_message_id_is_the_svix_id_else_a_stable_digest():
    assert dw.message_id(EVIDENCE, "msg_2Lx") == "drata:msg_2Lx"
    assert dw.message_id(EVIDENCE, None) == dw.message_id(dict(EVIDENCE), "")
    assert dw.message_id(EVIDENCE, None) != dw.message_id({**EVIDENCE, "timestamp": "x"}, None)


def test_the_run_payload_keeps_the_body_and_names_the_event():
    payload = dw.event_payload(EVIDENCE, "evidence_changed", "msg_1")
    assert payload["provider"] == "drata"
    assert payload["event"] == "evidence_changed"
    assert payload["event_type"] == "LINKED_TO_CONTROL"
    assert payload["evidence_linked_control_code"] == "DCF-12"
    assert payload["message_id"] == "drata:msg_1"


def test_the_event_type_filter_narrows_a_family():
    from app.services.integration_event_filters import event_filter_reason

    payload = dw.event_payload(EVIDENCE, "evidence_changed", "msg_1")
    assert event_filter_reason("evidence_changed", payload, {"event_type": "LINKED_TO_CONTROL"}) is None
    assert event_filter_reason("evidence_changed", payload, {"event_type": "RENEWAL_PAST_DUE"}) is not None
    assert event_filter_reason("evidence_changed", payload, {"workspace.id": 7}) is None


def test_the_catalog_lists_the_drata_events_and_the_token_field():
    entry = PROVIDER_CATALOG["drata"]
    assert [e["event"] for e in entry["available_events"]] == list(dw.EVENT_SPECS)
    for event in entry["available_events"]:
        assert event["mechanism"] == "webhook"
        assert event["filter_keys"] == dw.FILTER_KEYS
        assert "Authorization: Bearer" in event["description"]
    assert dw.TOKEN_FIELD in entry["extra_fields"]
    assert any("Send webhook" in step for step in entry["setup_guide"]["steps"])


@pytest.mark.asyncio
async def test_dispatch_ignores_what_is_not_a_drata_event():
    assert (await dw.dispatch_delivery(INTEGRATION, {"event_type": "NOPE"}))["status"] == "ignored"
    assert (await dw.dispatch_delivery(INTEGRATION, ["not", "an", "object"]))["status"] == "ignored"


@pytest.mark.asyncio
async def test_a_delivery_runs_once_and_its_retry_is_dropped(monkeypatch):
    import app.core.database as database
    import app.core.redis as redis_mod
    import app.services.assignment_engine as engine
    import app.services.docusign_connect as docusign
    import app.services.slack_events as slack_events

    seen: set[tuple[object, str]] = set()

    async def claim_seen(redis, assignment_id, member, *, now=None):
        if (assignment_id, member) in seen:
            return False
        seen.add((assignment_id, member))
        return True

    async def release_seen(redis, assignment_id, member):
        seen.discard((assignment_id, member))

    async def redis_client():
        return object()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    wf = SimpleNamespace(id="wf-1", name="Evidence watch", trigger_config={"provider": "drata"})
    agent = SimpleNamespace(name="GRC")

    async def provider_assignments(db, integration_id, provider, events):
        assert provider == "drata" and events == {"evidence_changed"}
        return [(wf, agent)]

    runs: list[dict] = []

    async def trigger(wf_, agent_, payload, *, db, trigger_context, overflow):
        assert trigger_context["delivery"] == "drata_webhook"
        runs.append(payload)
        return SimpleNamespace(status="started", run_id=f"run-{len(runs)}")

    monkeypatch.setattr(slack_events, "claim_seen", claim_seen)
    monkeypatch.setattr(slack_events, "release_seen", release_seen)
    monkeypatch.setattr(redis_mod, "redis_client", redis_client)
    monkeypatch.setattr(database, "async_session", lambda: Session())
    monkeypatch.setattr(docusign, "provider_assignments", provider_assignments)
    monkeypatch.setattr(engine, "trigger_or_queue_assignment", trigger)

    first = await dw.dispatch_delivery(INTEGRATION, EVIDENCE, "msg_1")
    retry = await dw.dispatch_delivery(INTEGRATION, EVIDENCE, "msg_1")
    assert first["count"] == 1 and retry["count"] == 0
    assert len(runs) == 1 and runs[0]["message_id"] == "drata:msg_1"


@pytest.mark.asyncio
async def test_the_receiver_refuses_a_delivery_without_the_token(monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import webhooks

    monkeypatch.setattr(webhooks, "_read_webhook_config", lambda enc, *, context: {dw.TOKEN_FIELD: TOKEN})
    dispatched: list[tuple] = []

    async def dispatch(integration_id, payload, svix_id=None):
        dispatched.append((integration_id, svix_id))
        return {"status": "ok", "triggered": [], "count": 0}

    monkeypatch.setattr(dw, "dispatch_delivery", dispatch)

    def request(headers):
        return SimpleNamespace(headers=headers)

    for headers in ({}, {"authorization": "Bearer wrong"}):
        with pytest.raises(HTTPException) as refused:
            await webhooks._handle_drata_webhook(INTEGRATION, "enc", EVIDENCE, request(headers))
        assert refused.value.status_code == 401
    assert not dispatched

    await webhooks._handle_drata_webhook(
        INTEGRATION, "enc", EVIDENCE, request({"authorization": f"Bearer {TOKEN}", "svix-id": "msg_9"})
    )
    assert dispatched == [(INTEGRATION, "msg_9")]

    monkeypatch.setattr(webhooks, "_read_webhook_config", lambda enc, *, context: {})
    with pytest.raises(HTTPException) as refused:
        await webhooks._handle_drata_webhook(
            INTEGRATION, "enc", EVIDENCE, request({"authorization": f"Bearer {TOKEN}"})
        )
    assert refused.value.status_code == 401
