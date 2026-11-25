"""Notion native webhooks for integration_event triggers (#2185, Notion slice)."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.services import notion_webhooks as nw

TOKEN = "test-verification-token"  # noqa: S105 - a test fixture, not a credential
EVENT = {
    "id": "367cba44-b6f3-4c92-81e7-6a2e9659efd4",
    "timestamp": "2024-12-05T23:55:34.285Z",
    "workspace_id": "13950b26-c203-4f3b-b97d-93ec06319565",
    "workspace_name": "Quantify Labs",
    "subscription_id": "29d75c0d-5546-4414-8459-7b7a92f1fc4b",
    "integration_id": "0ef104cd-477e-4b81-b8a8-5bc0d0b1a9ff",
    "type": "comment.created",
    "authors": [{"id": "c7c11cca-1d73-471d-9b6e-bdef51470190", "type": "person"}],
    "attempt_number": 1,
    "entity": {"id": "153104cd-477e-809d-8dc4-ff2d96ae3090", "type": "comment"},
    "data": {"page_id": "0ef104cd-477e-80e1-8571-cf43b1e1f7d1", "parent": {"id": "0ef104cd", "type": "page"}},
}
BODY = json.dumps(EVENT).encode()


def test_the_signature_is_hmac_sha256_of_the_raw_body_with_the_token():
    expected = "sha256=" + hmac.new(TOKEN.encode(), BODY, hashlib.sha256).hexdigest()
    assert nw.signature(TOKEN, BODY) == expected
    assert nw.verify_signature(BODY, expected, TOKEN)


@pytest.mark.parametrize("header", [None, "", "sha256=00", "sha256=" + "a" * 64])
def test_a_missing_or_wrong_signature_is_refused(header):
    assert not nw.verify_signature(BODY, header, TOKEN)


def test_a_tampered_body_is_refused():
    assert not nw.verify_signature(BODY + b" ", nw.signature(TOKEN, BODY), TOKEN)


def test_no_stored_token_refuses_every_delivery():
    assert not nw.verify_signature(BODY, nw.signature(TOKEN, BODY), None)
    assert not nw.verify_signature(BODY, nw.signature("", BODY), "")


def test_the_handshake_is_told_from_an_event():
    assert nw.handshake_token({"verification_token": "secret_abc"}) == "secret_abc"
    assert nw.handshake_token(EVENT) is None
    assert nw.handshake_token({"verification_token": " "}) is None
    assert nw.handshake_token(["x"]) is None


def test_the_stored_token_is_read_from_the_extra_config():
    assert nw.stored_token({nw.TOKEN_FIELD: " secret_x "}) == "secret_x"
    assert nw.stored_token({}) is None
    assert nw.stored_token(None) is None


def test_the_event_payload_carries_the_envelope_and_a_message_id_per_event():
    payload = nw.event_payload(EVENT)
    assert payload is not None
    assert payload["provider"] == "notion"
    assert payload["event"] == "comment.created"
    assert payload["message_id"] == f"notion:{EVENT['id']}"
    assert payload["entity"]["id"] == EVENT["entity"]["id"]
    assert payload["data"]["page_id"] == EVENT["data"]["page_id"]
    assert payload["authors"][0]["type"] == "person"
    assert "list_notion_comments" in payload["read_with"]


def test_a_retried_delivery_keeps_the_same_message_id():
    retried = {**EVENT, "attempt_number": 3}
    assert nw.event_payload(retried)["message_id"] == nw.event_payload(EVENT)["message_id"]


@pytest.mark.parametrize(
    "event",
    [{**EVENT, "type": "page.deleted"}, {**EVENT, "id": ""}, {k: v for k, v in EVENT.items() if k != "id"}],
)
def test_an_unknown_type_or_an_event_without_id_is_not_one(event):
    assert nw.event_payload(event) is None


def test_a_comment_mentioning_the_bot_is_found_whatever_the_dashes():
    bot = "a1b2c3d4-0000-4000-8000-000000000001"
    comment = {
        "rich_text": [
            {"type": "text", "plain_text": "cc "},
            {"type": "mention", "mention": {"type": "user", "user": {"id": bot.replace("-", "")}}},
        ]
    }
    assert nw.comment_mentions(comment, bot)
    assert not nw.comment_mentions(comment, "ffffffff-0000-4000-8000-000000000002")
    assert not nw.comment_mentions({"rich_text": [{"type": "text"}]}, bot)


@pytest.mark.asyncio
async def test_mentions_bot_reads_the_comment_and_the_bot_user():
    bot = "a1b2c3d4-0000-4000-8000-000000000001"
    comment = {"rich_text": [{"type": "mention", "mention": {"user": {"id": bot}}}]}

    async def read(comment_id):
        assert comment_id == "c1"
        return {"comment": comment, "bot_id": bot}, None

    async def fails(comment_id):
        return None, "Notion answered HTTP 404/404"

    assert await nw.mentions_bot(read, "c1") is True
    assert await nw.mentions_bot(fails, "c1") is None


def test_the_mention_filter_drops_a_comment_that_does_not_mention_the_bot():
    from app.services.integration_event_filters import event_filter_reason

    payload = {**nw.event_payload(EVENT), nw.MENTIONS_BOT_KEY: False}
    assert event_filter_reason("comment.created", payload, {"mentions_bot": True}) is not None
    payload[nw.MENTIONS_BOT_KEY] = True
    assert event_filter_reason("comment.created", payload, {"mentions_bot": True}) is None
    assert event_filter_reason("comment.created", payload, {"authors.0.type": "bot"}) is not None


def test_the_catalog_lists_the_notion_events_with_the_service_filter_keys():
    events = PROVIDER_CATALOG["notion"]["available_events"]
    assert [e["event"] for e in events] == list(nw.EVENTS)
    for entry in events:
        assert entry["mechanism"] == "webhook"
        assert entry["filter_keys"] == nw.FILTER_KEYS
        assert "poll" not in entry["description"].lower()


@pytest.mark.asyncio
async def test_dispatch_ignores_what_is_not_a_notion_event():
    answer = await nw.dispatch_delivery("00000000-0000-4000-8000-000000000000", {"type": "page.deleted", "id": "x"})
    assert answer["status"] == "ignored"
    answer = await nw.dispatch_delivery("00000000-0000-4000-8000-000000000000", ["not", "an", "object"])
    assert answer["status"] == "ignored"


@pytest.mark.asyncio
async def test_a_signed_event_runs_once_and_a_duplicate_is_dropped(monkeypatch):
    import app.core.database as database
    import app.core.redis as redis_mod
    import app.services.assignment_engine as engine
    import app.services.docusign_connect as docusign

    class FakeRedis:
        def __init__(self):
            self.members: dict[str, set[str]] = {}

    seen: set[tuple[object, str]] = set()

    async def claim_seen(redis, assignment_id, member, *, now=None):
        if (assignment_id, member) in seen:
            return False
        seen.add((assignment_id, member))
        return True

    async def release_seen(redis, assignment_id, member):
        seen.discard((assignment_id, member))

    async def redis_client():
        return FakeRedis()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    wf = SimpleNamespace(id="wf-1", name="Legal mentions", trigger_config={"provider": "notion"})
    agent = SimpleNamespace(name="Legal")

    async def provider_assignments(db, integration_id, provider, events):
        assert provider == "notion" and events == {"comment.created"}
        return [(wf, agent)]

    runs: list[dict] = []

    async def trigger(wf_, agent_, payload, *, db, trigger_context, overflow):
        runs.append(payload)
        return SimpleNamespace(status="started", run_id=f"run-{len(runs)}")

    import app.services.slack_events as slack_events

    monkeypatch.setattr(slack_events, "claim_seen", claim_seen)
    monkeypatch.setattr(slack_events, "release_seen", release_seen)
    monkeypatch.setattr(redis_mod, "redis_client", redis_client)
    monkeypatch.setattr(database, "async_session", lambda: Session())
    monkeypatch.setattr(docusign, "provider_assignments", provider_assignments)
    monkeypatch.setattr(engine, "trigger_or_queue_assignment", trigger)

    first = await nw.dispatch_delivery("00000000-0000-4000-8000-000000000000", EVENT)
    again = await nw.dispatch_delivery("00000000-0000-4000-8000-000000000000", {**EVENT, "attempt_number": 2})
    assert first["count"] == 1 and again["count"] == 0
    assert len(runs) == 1 and runs[0]["message_id"] == f"notion:{EVENT['id']}"
