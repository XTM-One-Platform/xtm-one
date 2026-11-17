"""DocuSign Connect: the real-time path of the DocuSign envelope events.

A Connect delivery on the integration's webhook URL is verified with the
integration's Connect HMAC keys (``X-DocuSign-Signature-<n>``), turned into the
payloads the 15-minute poll builds, and claimed in the seen set the poll shares,
so an envelope change runs once per assignment whichever path reads it first.
Redis, the database and the assignment engine are fakes; nothing reaches DocuSign.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import webhooks
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.providers import docusign
from app.services import docusign_connect
from app.services.slack_events import claim_seen

_KEY = "connect-key-one"
_INTEGRATION = str(uuid.uuid4())


class _Redis:
    """The sorted-set calls of the seen set (``slack_events.claim_seen`` / ``release_seen``)."""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}

    async def sismember(self, _key, _member):
        return False

    async def zremrangebyscore(self, key, _low, high):
        members = self.zsets.get(key, {})
        for member, score in list(members.items()):
            if high != "-inf" and score <= float(high):
                members.pop(member)

    async def zadd(self, key, mapping, nx=False):
        members = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if nx and member in members:
                continue
            members[member] = score
            added += 1
        return added

    async def expire(self, _key, _seconds):
        return True

    async def zrem(self, key, member):
        self.zsets.get(key, {}).pop(member, None)


def _signer(recipient_id: str, email: str, status: str, signed: str = "") -> dict:
    return {"recipientId": recipient_id, "name": email.split("@")[0].title(), "email": email, "status": status, "signedDateTime": signed}


def _summary(envelope_id: str, status: str, signers: list[dict]) -> dict:
    return {
        "envelopeId": envelope_id,
        "status": status,
        "emailSubject": f"Please sign {envelope_id}",
        "sender": {"userName": "Sam", "email": "sam@acme.example"},
        "statusChangedDateTime": "2026-10-31T08:10:00.1234567Z",
        "completedDateTime": "2026-10-31T08:10:00.1234567Z" if status == "completed" else "",
        "recipients": {"signers": signers},
    }


def _delivery(event: str, summary: dict, *, recipient_id: str = "", generated: str | None = None) -> dict:
    data: dict = {"accountId": "acc", "envelopeId": summary["envelopeId"], "envelopeSummary": summary}
    if recipient_id:
        data["recipientId"] = recipient_id
    return {
        "event": event,
        "apiVersion": "v2.1",
        "uri": f"/restapi/v2.1/accounts/acc/envelopes/{summary['envelopeId']}",
        "retryCount": 0,
        "configurationId": 1,
        "generatedDateTime": generated or datetime.now(UTC).isoformat(),
        "data": data,
    }


_SIGNERS = [
    _signer("1", "ana@client.example", "completed", "2026-10-31T08:05:00Z"),
    _signer("2", "bo@acme.example", "completed", "2026-10-31T08:10:00Z"),
]
_COMPLETED = _delivery("envelope-completed", _summary("env-done", "completed", _SIGNERS))


# -- signature ---------------------------------------------------------------


def test_a_signature_under_a_stored_key_is_accepted_and_any_other_refused():
    body = json.dumps(_COMPLETED).encode()
    signed = {"X-DocuSign-Signature-1": docusign_connect.connect_signature(body, _KEY)}
    assert docusign_connect.verify_connect_signature(body, [_KEY], signed)
    assert not docusign_connect.verify_connect_signature(body + b" ", [_KEY], signed)
    assert not docusign_connect.verify_connect_signature(body, ["another-key"], signed)
    # No header, or no key: never accepted unverified.
    assert not docusign_connect.verify_connect_signature(body, [_KEY], {})
    assert not docusign_connect.verify_connect_signature(body, [], signed)


def test_any_numbered_signature_header_and_any_stored_key_verify_during_a_rotation():
    body = b'{"event":"envelope-completed"}'
    headers = {
        "x-docusign-signature-1": docusign_connect.connect_signature(body, "old-key"),
        "X-DocuSign-Signature-2": docusign_connect.connect_signature(body, "new-key"),
    }
    assert docusign_connect.verify_connect_signature(body, ["new-key"], headers)
    assert docusign_connect.connect_hmac_keys({"connect_hmac_key": " old-key, new-key \n"}) == ["old-key", "new-key"]
    assert docusign_connect.connect_hmac_keys({}) == []
    assert docusign_connect.connect_hmac_keys({"connect_hmac_key": 12}) == []
    # A header that only looks like one is not read.
    assert not docusign_connect.verify_connect_signature(body, ["new-key"], {"X-DocuSign-Signature-x": headers["X-DocuSign-Signature-2"]})


async def test_the_webhook_refuses_a_docusign_delivery_without_a_key_or_a_valid_signature(monkeypatch):
    body = json.dumps(_COMPLETED).encode()
    stored: dict = {}
    monkeypatch.setattr(webhooks, "_read_webhook_config", lambda _enc, *, context: stored)
    dispatched: list = []

    async def _dispatch(integration_id, extra, payload):
        dispatched.append(integration_id)
        return {"status": "ok", "triggered": [], "count": 0}

    monkeypatch.setattr(docusign_connect, "dispatch_connect_delivery", _dispatch)
    signed = SimpleNamespace(headers={"X-DocuSign-Signature-1": docusign_connect.connect_signature(body, _KEY)})
    with pytest.raises(HTTPException) as no_key:
        await webhooks._handle_docusign_connect(_INTEGRATION, "enc", body, _COMPLETED, signed)
    assert no_key.value.status_code == 401
    stored["connect_hmac_key"] = _KEY
    with pytest.raises(HTTPException) as unsigned:
        await webhooks._handle_docusign_connect(_INTEGRATION, "enc", body, _COMPLETED, SimpleNamespace(headers={}))
    assert unsigned.value.status_code == 401
    assert dispatched == []
    answer = await webhooks._handle_docusign_connect(_INTEGRATION, "enc", body, _COMPLETED, signed)
    assert answer["status"] == "ok" and dispatched == [_INTEGRATION]


# -- the poll's payloads -----------------------------------------------------


def test_a_connect_delivery_builds_the_payload_the_poll_builds():
    (event,) = docusign_connect.connect_events(_COMPLETED, "envelope_completed", environment="demo")
    (polled,) = docusign.envelope_events(_COMPLETED["data"]["envelopeSummary"], "envelope_completed", environment="demo")
    assert event == polled
    assert event["message_id"] == "env-done:completed"
    assert docusign_connect.connect_events(_COMPLETED, "envelope_declined") == []


def test_recipient_completed_is_the_signer_the_delivery_names():
    delivery = _delivery("recipient-completed", _summary("env-sent", "sent", _SIGNERS), recipient_id="2")
    (event,) = docusign_connect.connect_events(delivery, "recipient_completed")
    assert event["signer_email"] == "bo@acme.example"
    assert event["message_id"] == "env-sent:recipient:2:completed"
    polled = docusign.envelope_events(delivery["data"]["envelopeSummary"], "recipient_completed")
    assert event in polled
    # Another event, or a recipient who is not a signer, runs nothing.
    assert docusign_connect.connect_events(_COMPLETED, "recipient_completed") == []
    cc = _delivery("recipient-completed", _summary("env-sent", "sent", _SIGNERS), recipient_id="9")
    assert docusign_connect.connect_events(cc, "recipient_completed") == []


def test_a_delivery_without_the_envelope_data_or_too_old_is_not_read():
    bare = {"event": "envelope-completed", "data": {"envelopeId": "env-x"}}
    assert docusign_connect.connect_envelope(bare) is None
    assert docusign_connect.connect_events(bare, "envelope_completed") == []
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    assert docusign_connect.delivery_too_old({"generatedDateTime": old})
    assert not docusign_connect.delivery_too_old(_COMPLETED)
    assert not docusign_connect.delivery_too_old({})


# -- once per envelope change, Connect or poll -------------------------------


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _wf(event: str, *, flt: dict | None = None):
    config = {"provider": "docusign", "event": event, "integration_id": _INTEGRATION}
    if flt:
        config["filter"] = flt
    return SimpleNamespace(id=uuid.uuid4(), name=f"on {event}", trigger_config=config)


@pytest.fixture
def engine(monkeypatch):
    redis = _Redis()
    runs: list[tuple] = []
    state = {"status": "ok", "assignments": []}

    async def _redis_client():
        return redis

    async def _assignments(_db, integration_id):
        assert integration_id == _INTEGRATION
        return state["assignments"]

    async def _trigger(wf, agent, payload, *, db, trigger_context, overflow):
        assert overflow == "reject" and trigger_context["delivery"] == "docusign_connect"
        if state["status"] != "ok":
            return SimpleNamespace(status=state["status"], run_id=None)
        runs.append((wf.id, payload["message_id"]))
        return SimpleNamespace(status="ok", run_id=str(uuid.uuid4()))

    async def _record(_assignment_id, _count):
        return None

    monkeypatch.setattr(docusign_connect, "redis_client", _redis_client)
    monkeypatch.setattr(docusign_connect, "async_session", _Session)
    monkeypatch.setattr(docusign_connect, "connect_assignments", _assignments)
    monkeypatch.setattr(docusign_connect, "trigger_or_queue_assignment", _trigger)
    monkeypatch.setattr(docusign_connect, "record_filtered_events", _record)
    return SimpleNamespace(redis=redis, runs=runs, state=state)


async def test_one_run_per_envelope_event_whatever_delivers_it_first(engine):
    agent = SimpleNamespace(name="signer-watch")
    completed, signed = _wf("envelope_completed"), _wf("recipient_completed")
    engine.state["assignments"] = [(completed, agent), (signed, agent)]
    first = await docusign_connect.dispatch_connect_delivery(_INTEGRATION, {}, _COMPLETED)
    assert first["count"] == 1 and engine.runs == [(completed.id, "env-done:completed")]
    # DocuSign retries the same delivery: its events are claimed, nothing runs.
    again = await docusign_connect.dispatch_connect_delivery(_INTEGRATION, {}, _COMPLETED)
    assert again["count"] == 0 and len(engine.runs) == 1
    # The poll reads the same change later: the claim it shares refuses it.
    (polled,) = docusign.envelope_events(_COMPLETED["data"]["envelopeSummary"], "envelope_completed")
    assert not await claim_seen(engine.redis, completed.id, polled["message_id"])


async def test_an_event_the_poll_ran_first_does_not_run_again_from_connect(engine):
    wf = _wf("envelope_completed")
    engine.state["assignments"] = [(wf, SimpleNamespace(name="a"))]
    assert await claim_seen(engine.redis, wf.id, "env-done:completed", now=time.time())
    answer = await docusign_connect.dispatch_connect_delivery(_INTEGRATION, {}, _COMPLETED)
    assert answer["count"] == 0 and engine.runs == []


async def test_an_event_that_cannot_run_now_is_left_to_the_poll(engine):
    wf = _wf("envelope_completed")
    engine.state["assignments"] = [(wf, SimpleNamespace(name="a"))]
    engine.state["status"] = "queue_full"
    assert (await docusign_connect.dispatch_connect_delivery(_INTEGRATION, {}, _COMPLETED))["count"] == 0
    # Released: the poll (or the next delivery) claims it.
    assert await claim_seen(engine.redis, wf.id, "env-done:completed")


async def test_the_trigger_filter_drops_an_event_without_a_run(engine):
    wf = _wf("envelope_completed", flt={"signer_emails": ["other.example"]})
    engine.state["assignments"] = [(wf, SimpleNamespace(name="a"))]
    assert (await docusign_connect.dispatch_connect_delivery(_INTEGRATION, {}, _COMPLETED))["count"] == 0
    assert engine.runs == []


async def test_a_delivery_that_is_not_an_envelope_change_runs_nothing(engine):
    engine.state["assignments"] = [(_wf("envelope_completed"), SimpleNamespace(name="a"))]
    answer = await docusign_connect.dispatch_connect_delivery(_INTEGRATION, {}, {"event": "envelope-completed"})
    assert answer["status"] == "ignored" and engine.runs == []
    old = _delivery("envelope-completed", _summary("env-done", "completed", _SIGNERS), generated="2026-01-01T00:00:00Z")
    assert (await docusign_connect.dispatch_connect_delivery(_INTEGRATION, {}, old))["status"] == "ignored"


def test_the_catalog_declares_the_connect_key_and_the_hybrid_delivery():
    entry = PROVIDER_CATALOG["docusign"]
    assert docusign_connect.HMAC_KEY_FIELD in entry["extra_fields"]
    assert all(e["mechanism"] == "hybrid" for e in entry["available_events"])
