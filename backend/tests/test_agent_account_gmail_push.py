"""Gmail push: users.watch to a Pub/Sub topic, its renewal, verified deliveries, the fallback poll."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.agent_accounts import listening
from app.services.agent_accounts.providers import ProviderError, ProviderSession
from app.services.agent_accounts.providers import google_gmail as gmail
from app.services.agent_accounts.rules import ListeningPlan

GMAIL = gmail.GMAIL
ME = "sam.personal@example.com"
TOPIC = "projects/xtm-one-push/topics/gmail-inbox"
SIGNER = "gmail-push@xtm-one-push.iam.gserviceaccount.com"


class FakeGmail:
    def __init__(self, routes: dict[tuple[str, str], Any]):
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, session, method, url, *, params=None, payload=None, allow_missing=False):
        self.calls.append({"method": method, "url": url, "params": params, "payload": payload})
        if (method, url) not in self.routes:
            raise ProviderError(f"unexpected {method} {url}")
        return self.routes[(method, url)]


def _provider(monkeypatch, routes) -> tuple[gmail.GmailProvider, FakeGmail]:
    provider = gmail.GmailProvider()
    fake = FakeGmail(routes)
    monkeypatch.setattr(provider, "_call", fake)
    return provider, fake


def _session(**config: str) -> ProviderSession:
    return ProviderSession(token="token", account_external_id=ME, config=config)


PUSH = {gmail.PUBSUB_TOPIC_KEY: TOPIC, gmail.PUSH_SERVICE_ACCOUNT_KEY: SIGNER.upper()}
EXPIRY = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
WATCH = {"historyId": "1234", "expiration": str(int(EXPIRY.timestamp() * 1000))}


def test_push_is_declared_with_its_fallback():
    caps = gmail.GmailProvider.capabilities
    assert caps.push and caps.poll and caps.push_wakes_poll and caps.poll_fallback
    assert gmail.GmailProvider().required_config_keys({}) == ()


async def test_no_topic_no_watch_the_account_is_polled(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    assert await provider.desired_subscriptions(_session(), ListeningPlan(chats=True)) == []
    assert fake.calls == []


async def test_a_bad_topic_is_refused_before_any_call(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    with pytest.raises(ProviderError):
        await provider.desired_subscriptions(
            _session(**{gmail.PUBSUB_TOPIC_KEY: "my-topic", gmail.PUSH_SERVICE_ACCOUNT_KEY: SIGNER}),
            ListeningPlan(chats=True),
        )
    assert fake.calls == []


async def test_the_inbox_is_watched_when_heard(monkeypatch):
    provider, _ = _provider(monkeypatch, {})
    wanted = await provider.desired_subscriptions(_session(**PUSH), ListeningPlan(chats=True))
    assert [(d.resource, d.covers) for d in wanted] == [(gmail.WATCH_RESOURCE, "inbox")]
    assert await provider.desired_subscriptions(_session(**PUSH), ListeningPlan()) == []


async def test_watch_records_the_signer_as_its_proof(monkeypatch):
    provider, fake = _provider(monkeypatch, {("POST", f"{GMAIL}/watch"): WATCH})
    created = await provider.create_subscription(
        _session(**PUSH), gmail.WATCH_RESOURCE, notification_url="u", lifecycle_url="u?l=1", client_state="random"
    )
    assert created.external_id == gmail.watch_id(ME)
    assert created.expires_at == EXPIRY
    assert created.client_state == SIGNER
    assert fake.calls[0]["payload"] == {
        "topicName": TOPIC,
        "labelIds": ["INBOX", "SENT"],
        "labelFilterBehavior": "include",
    }


async def test_renewal_calls_watch_again_and_reads_the_new_expiry(monkeypatch):
    provider, fake = _provider(monkeypatch, {("POST", f"{GMAIL}/watch"): WATCH})
    assert await provider.renew_subscription(_session(**PUSH), gmail.watch_id(ME)) == EXPIRY
    assert [c["url"] for c in fake.calls] == [f"{GMAIL}/watch"]


async def test_renewal_once_push_is_switched_off_drops_the_watch(monkeypatch):
    provider, fake = _provider(monkeypatch, {})
    assert await provider.renew_subscription(_session(), gmail.watch_id(ME)) is None
    assert fake.calls == []


async def test_a_watch_is_due_for_renewal_a_day_before_its_seven_days():
    assert gmail.GmailProvider.subscription_lifetime_minutes == 7 * 24 * 60
    renew_before: timedelta = listening.RENEW_BEFORE
    assert renew_before.total_seconds() <= 24 * 3600


async def test_delete_stops_the_watch(monkeypatch):
    provider, fake = _provider(monkeypatch, {("POST", f"{GMAIL}/stop"): {}})
    assert await provider.delete_subscription(_session(**PUSH), gmail.watch_id(ME)) is True
    assert fake.calls[0]["url"] == f"{GMAIL}/stop"


def _push_body(address: str = ME, history: str = "1240") -> bytes:
    data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": history}).encode()).decode()
    return json.dumps(
        {"message": {"data": data, "messageId": "1"}, "subscription": "projects/p/subscriptions/s"}
    ).encode()


def test_a_delivery_names_the_mailbox_watch_and_its_signer():
    [entry] = gmail.push_entries(_push_body(ME.upper()), SIGNER)
    assert entry.subscription_id == gmail.watch_id(ME)
    assert entry.client_state == SIGNER
    assert entry.payload == {"historyId": "1240"}


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"message": 3}'])
def test_a_malformed_delivery_is_refused(body):
    with pytest.raises(ValueError):
        gmail.push_entries(body, SIGNER)


async def test_an_unsigned_delivery_proves_nothing(monkeypatch):
    provider = gmail.GmailProvider()
    [entry] = await provider.parse_delivery(_push_body(), {}, "https://x/notifications/google_gmail")
    assert entry.client_state == ""
    assert await listening.accept_notifications(provider, [entry]) == []


async def test_a_signed_delivery_carries_the_verified_signer(monkeypatch):
    seen: dict[str, str] = {}

    async def verify(authorization: str, audience: str) -> str:
        seen.update(authorization=authorization, audience=audience)
        return SIGNER

    monkeypatch.setattr(gmail, "verified_push_signer", verify)
    provider = gmail.GmailProvider()
    [entry] = await provider.parse_delivery(
        _push_body(), {"authorization": "Bearer t"}, "https://x/api/v1/agent-accounts/notifications/google_gmail"
    )
    assert entry.client_state == SIGNER
    assert seen == {
        "authorization": "Bearer t",
        "audience": "https://x/api/v1/agent-accounts/notifications/google_gmail",
    }


@pytest.mark.parametrize("header", ["", "Basic abc", "Bearer ", "Bearer not-a-jwt"])
async def test_a_bad_token_names_no_signer(header):
    assert await gmail.verified_push_signer(header, "https://x") == ""


async def test_a_push_reads_the_history_since_the_cursor(monkeypatch):
    provider = gmail.GmailProvider()
    calls: list[str | None] = []

    async def poll(session, plan, cursor):
        calls.append(cursor)
        return [], "1300"

    cursors: dict[str, str] = {"acc": "1200"}

    async def read_cursor(account_id):
        return cursors.get(account_id)

    async def write_cursor(account_id, cursor):
        cursors[account_id] = cursor

    async def take_lease(name, seconds):
        return True

    async def release_lease(name):
        return None

    monkeypatch.setattr(provider, "poll", poll)
    monkeypatch.setattr(listening.state, "read_cursor", read_cursor)
    monkeypatch.setattr(listening.state, "write_cursor", write_cursor)
    monkeypatch.setattr(listening.state, "take_lease", take_lease)
    monkeypatch.setattr(listening.state, "release_lease", release_lease)

    class Account:
        id = "acc"

    await listening._poll_on_push(Account(), provider, _session(**PUSH), ListeningPlan(chats=True))
    assert calls == ["1200"]
    assert cursors["acc"] == "1300"


async def test_a_push_during_a_read_starts_no_second_read(monkeypatch):
    provider = gmail.GmailProvider()

    async def poll(session, plan, cursor):  # pragma: no cover - must not run
        raise AssertionError("polled twice")

    async def take_lease(name, seconds):
        return False

    monkeypatch.setattr(provider, "poll", poll)
    monkeypatch.setattr(listening.state, "take_lease", take_lease)

    class Account:
        id = "acc"

    await listening._poll_on_push(Account(), provider, _session(**PUSH), ListeningPlan(chats=True))
