"""The owner-sent event: a mailbox owner's own email raises ``agent_account.owner_sent``, once per message."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import platform_events
from app.services.agent_accounts import pipeline
from app.services.agent_accounts.events import AccountEvent

OWNER = uuid.uuid4()
AGENT = uuid.uuid4()


def _account(provider: str = "google_gmail", takeover: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), provider=provider, agent_id=AGENT, user_id=OWNER, owner_takeover_minutes=takeover
    )


def _event(**kwargs) -> AccountEvent:
    values = {
        "provider": "google_gmail",
        "conversation_type": "direct",
        "conversation_id": "t1",
        "thread_id": "t1",
        "message_id": "s1",
        "sender_id": "sam@example.com",
        "sender_name": "Sam",
        "text": "Thanks, see you Tuesday",
        "created_at": datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
        "conversation_name": "Lunch",
        "from_account": True,
        "internet_message_id": "<s1@mail.gmail.com>",
    }
    values.update(kwargs)
    return AccountEvent(**values)


@pytest.fixture
def published(monkeypatch) -> list[tuple[str, dict, str | None]]:
    out: list[tuple[str, dict, str | None]] = []

    async def publish(name, data, *, event_id=None):
        out.append((name, dict(data), event_id))
        return event_id

    async def no(*_args, **_kwargs):
        return False

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(platform_events, "publish", publish)
    monkeypatch.setattr(pipeline.state, "was_sent_by_agent", no)
    monkeypatch.setattr(pipeline.state, "is_sending", no)
    monkeypatch.setattr(pipeline.state, "mark_owner_active", nothing)
    return out


async def test_an_owner_send_raises_the_event_for_the_owner_and_the_agent(published):
    account = _account()
    await pipeline._note_account_message(account, _event())
    assert len(published) == 1
    name, data, event_id = published[0]
    assert name == "agent_account.owner_sent"
    assert event_id == f"agent_account.owner_sent:{account.id}:s1"
    assert data["thread_id"] == "t1" and data["message_id"] == "s1"
    assert data["internet_message_id"] == "<s1@mail.gmail.com>"
    assert data["provider"] == "google_gmail" and data["subject"] == "Lunch"
    assert data["sent_at"] == "2026-11-07T12:00:00+00:00"
    # Personal: only the owner's assignments hear it; the account's agent takes part.
    assert data["personal_owner_id"] == str(OWNER)
    assert data["participant_agent_ids"] == [str(AGENT)]


async def test_the_agents_own_send_raises_nothing(published, monkeypatch):
    async def yes(*_args, **_kwargs):
        return True

    monkeypatch.setattr(pipeline.state, "was_sent_by_agent", yes)
    await pipeline._note_account_message(_account(), _event())
    assert published == []


async def test_another_agent_accounts_send_raises_nothing(published):
    await pipeline._note_account_message(_account(), _event(from_agent_account=True))
    assert published == []


async def test_a_chat_provider_raises_nothing(published):
    await pipeline._note_account_message(_account(provider="microsoft_teams"), _event(provider="microsoft_teams"))
    assert published == []


async def test_the_outlook_mailbox_raises_it_too(published):
    await pipeline._note_account_message(_account(provider="microsoft_outlook"), _event(provider="microsoft_outlook"))
    assert [name for name, _data, _id in published] == ["agent_account.owner_sent"]


def test_the_event_is_in_the_catalog_with_its_filter_keys():
    keys = {e["event"]: e["filter_keys"] for e in platform_events.catalog()}["agent_account.owner_sent"]
    assert {"account_id", "provider", "thread_id", "message_id"} <= set(keys)
