"""DocuSign envelope events: the poll fallback of the ``integration_event`` triggers.

``envelope_completed``, ``envelope_declined``, ``envelope_voided`` and
``recipient_completed`` are polled from ``GET /envelopes?from_date=<cursor>``
(listStatusChanges) every 15 minutes, start from now, and carry a
``message_id`` naming the change so each one runs once per assignment.
The DocuSign API and Redis are fakes; nothing reaches DocuSign.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.integrations.http import _APP_TOKEN_CACHE
from app.integrations.providers import docusign
from app.services import event_poller
from app.services.integration_event_filters import event_filter_reason
from tests.integration_file_fakes import fake_api

_ACCOUNT = "11111111-2222-3333-4444-555555555555"
_BASE = f"https://demo.docusign.net/restapi/v2.1/accounts/{_ACCOUNT}"
_TOKEN = f"demo||{_ACCOUNT}||oauth||access-tok"
_CURSOR = "2026-10-31T08:00:00.0000000Z"


@pytest.fixture(autouse=True)
def _no_cache():
    _APP_TOKEN_CACHE.clear()
    yield
    _APP_TOKEN_CACHE.clear()


class _Redis:
    def __init__(self, cursor: str | None = None, wf_id: object = None):
        self.values: dict[str, str] = {}
        if cursor is not None:
            self.values[f"event_cursor:{wf_id}"] = cursor

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, nx=False, **_k):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


def _wf(event: str):
    return SimpleNamespace(id=uuid.uuid4(), trigger_config={"provider": "docusign", "event": event})


def _envelope(envelope_id: str, status: str, changed: str, signers: list[dict]) -> dict:
    return {
        "envelopeId": envelope_id,
        "status": status,
        "emailSubject": f"Please sign {envelope_id}",
        "sender": {"userName": "Sam", "email": "sam@acme.example"},
        "statusChangedDateTime": changed,
        "completedDateTime": changed if status == "completed" else "",
        "recipients": {"signers": signers},
    }


def _signer(recipient_id: str, email: str, status: str, signed: str = "") -> dict:
    return {
        "recipientId": recipient_id,
        "name": email.split("@")[0].title(),
        "email": email,
        "status": status,
        "signedDateTime": signed,
    }


def _api(envelopes: list[dict]):
    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == "https://account-d.docusign.com/oauth/userinfo":
            return httpx.Response(
                200,
                json={"accounts": [{"account_id": _ACCOUNT, "is_default": True, "base_uri": "https://demo.docusign.net"}]},
            )
        if url == f"{_BASE}/envelopes" and request.method == "GET":
            return httpx.Response(200, json={"envelopes": envelopes, "resultSetSize": str(len(envelopes))})
        return httpx.Response(404, json={"errorCode": "NOT_FOUND"})

    return answer


_ENVELOPES = [
    _envelope(
        "env-done",
        "completed",
        "2026-10-31T08:10:00.1234567Z",
        [
            _signer("1", "ana@client.example", "completed", "2026-10-31T08:05:00Z"),
            _signer("2", "bo@acme.example", "completed", "2026-10-31T08:10:00Z"),
        ],
    ),
    _envelope(
        "env-sent",
        "sent",
        "2026-10-31T08:20:00Z",
        [
            _signer("1", "cy@client.example", "completed", "2026-10-31T08:20:00Z"),
            _signer("2", "di@client.example", "sent"),
        ],
    ),
    _envelope("env-old", "completed", "2026-10-31T07:50:00Z", [_signer("1", "ed@client.example", "completed", "2026-10-31T07:50:00Z")]),
]


async def test_the_first_poll_starts_from_now_and_runs_nothing():
    wf = _wf("envelope_completed")
    redis = _Redis()
    with fake_api(_api(_ENVELOPES)) as sent:
        assert await event_poller._poll_docusign_envelopes(_TOKEN, "envelope_completed", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"]
    assert sent == []


async def test_a_completed_envelope_creates_one_event_with_its_envelope_and_signers():
    wf = _wf("envelope_completed")
    redis = _Redis(_CURSOR, wf.id)
    with fake_api(_api(_ENVELOPES)) as sent:
        events = await event_poller._poll_docusign_envelopes(_TOKEN, "envelope_completed", wf, redis)
    # env-old is completed too: DocuSign answers what from_date asked, the
    # status filter is the event's own (only completed envelopes run it).
    assert [e["envelope_id"] for e in events] == ["env-old", "env-done"]
    done = events[1]
    assert done["message_id"] == "env-done:completed"
    assert done["status"] == "completed" and done["event"] == "envelope_completed"
    assert done["provider"] == "docusign" and done["trigger_type"] == "integration_event"
    assert done["signer_emails"] == ["ana@client.example", "bo@acme.example"]
    assert done["subject"] == "Please sign env-done" and done["sender_email"] == "sam@acme.example"
    assert done["web_link"].endswith("/documents/details/env-done")
    listing = next(r for r in sent if r.url.path.endswith("/envelopes"))
    params = listing.url.params
    assert (params["from_date"], params["order"], params["include"]) == (_CURSOR, "asc", "recipients")
    # The cursor moves to the newest change read, as DocuSign wrote it.
    assert redis.values[f"event_cursor:{wf.id}"] == "2026-10-31T08:20:00Z"


async def test_recipient_completed_runs_once_per_signer_who_signed_after_the_cursor():
    wf = _wf("recipient_completed")
    redis = _Redis(_CURSOR, wf.id)
    with fake_api(_api(_ENVELOPES)):
        events = await event_poller._poll_docusign_envelopes(_TOKEN, "recipient_completed", wf, redis)
    assert sorted(e["signer_email"] for e in events) == ["ana@client.example", "bo@acme.example", "cy@client.example"]
    ids = [e["message_id"] for e in events]
    assert len(ids) == len(set(ids))
    assert "env-done:recipient:1:completed" in ids


def test_an_envelope_event_matches_its_status_only():
    done, sent, _old = _ENVELOPES
    assert docusign.envelope_events(sent, "envelope_completed") == []
    assert docusign.envelope_events(done, "envelope_declined") == []
    assert len(docusign.envelope_events(done, "envelope_completed")) == 1
    declined = _envelope("env-no", "declined", "2026-10-31T09:00:00Z", [_signer("1", "fi@client.example", "declined")])
    (event,) = docusign.envelope_events(declined, "envelope_declined")
    assert event["signer_email"] == "fi@client.example" and event["message_id"] == "env-no:declined"


async def test_a_failed_listing_creates_no_event_and_keeps_the_cursor():
    wf = _wf("envelope_completed")
    redis = _Redis(_CURSOR, wf.id)

    def broken(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorCode": "AUTHORIZATION_INVALID_TOKEN"})

    with fake_api(broken):
        assert await event_poller._poll_docusign_envelopes(_TOKEN, "envelope_completed", wf, redis) == []
    assert redis.values[f"event_cursor:{wf.id}"] == _CURSOR


def test_a_docusign_timestamp_with_seven_fractional_digits_is_read():
    assert docusign.event_instant("2026-10-31T08:10:00.1234567Z").microsecond == 123456
    assert docusign.event_instant("not a date").year == 1


def test_the_filters_select_envelopes_signers_senders_and_subjects():
    done = docusign.envelope_events(_ENVELOPES[0], "envelope_completed")[0]
    assert event_filter_reason("envelope_completed", done, {"envelope_ids": ["ENV-DONE"]}) is None
    assert event_filter_reason("envelope_completed", done, {"envelope_ids": ["env-x"]}) == "envelope is not in envelope_ids"
    assert event_filter_reason("envelope_completed", done, {"signer_emails": ["client.example"]}) is None
    assert event_filter_reason("envelope_completed", done, {"signer_emails": ["other.example"]})
    assert event_filter_reason("envelope_completed", done, {"sender_emails": ["sam@acme.example"]}) is None
    assert event_filter_reason("envelope_completed", done, {"sender_emails": ["x@acme.example"]})
    assert event_filter_reason("envelope_completed", done, {"subject_contains": "sign"}) is None
    assert event_filter_reason("envelope_completed", done, {"subject_contains": ["invoice"]})
    signed = next(
        e
        for e in docusign.envelope_events(_ENVELOPES[0], "recipient_completed")
        if e["signer_email"] == "bo@acme.example"
    )
    # recipient_completed filters on the signer who signed, not every signer.
    assert event_filter_reason("recipient_completed", signed, {"signer_emails": ["acme.example"]}) is None
    assert event_filter_reason("recipient_completed", signed, {"signer_emails": ["client.example"]})


def test_the_catalog_declares_the_four_polled_events_with_their_filters():
    events = {e["event"]: e for e in PROVIDER_CATALOG["docusign"]["available_events"]}
    assert set(events) == set(docusign.ENVELOPE_EVENTS)
    for event in events.values():
        # Polled, and pushed by DocuSign Connect when it is configured.
        assert event["mechanism"] == "hybrid"
        assert set(event["filter_keys"]) == {"envelope_ids", "signer_emails", "sender_emails", "subject_contains"}
    assert all(event_poller.POLL_INTERVALS[e] == 900 for e in docusign.ENVELOPE_EVENTS)
