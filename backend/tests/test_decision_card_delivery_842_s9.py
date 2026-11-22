"""The Teams bot sends a decision card where it is a member (#842, S9 step 2), without a database."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import decision_card_delivery as delivery
from app.services import work_item_notify


class _Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttl[key] = ex

    async def get(self, key):
        return self.store.get(key)


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()

    async def _client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return fake


SERVICE = "https://smba.trafficmanager.net/emea/"


def test_a_channel_thread_is_recorded_as_its_channel(redis):
    asyncio.run(delivery.remember_bot_conversation("19:abc@thread.tacv2;messageid=42", SERVICE, "channel"))
    ref = asyncio.run(delivery.bot_conversation("19:abc@thread.tacv2"))
    assert ref == {"service_url": SERVICE, "conversation_id": "19:abc@thread.tacv2", "conversation_type": "channel"}
    assert redis.ttl[delivery.conversation_key("19:abc@thread.tacv2")] == 60 * 60 * 24 * 90


@pytest.mark.parametrize("kind", ["personal", "groupChat", "channel"])
def test_each_conversation_type_is_kept(redis, kind):
    asyncio.run(delivery.remember_bot_conversation("c-1", SERVICE, kind))
    assert asyncio.run(delivery.bot_conversation("c-1"))["conversation_type"] == kind


def test_an_unknown_type_is_left_for_the_channel_to_infer(redis):
    asyncio.run(delivery.remember_bot_conversation("c-1", SERVICE, "?"))
    assert asyncio.run(delivery.bot_conversation("c-1"))["conversation_type"] == ""


def test_the_bot_is_not_known_where_it_never_heard(redis):
    assert asyncio.run(delivery.bot_conversation("19:other@thread.v2")) is None
    assert asyncio.run(delivery.bot_conversation("")) is None


def test_a_broken_record_is_not_trusted(redis):
    redis.store[delivery.conversation_key("c-1")] = "{not json"
    assert asyncio.run(delivery.bot_conversation("c-1")) is None
    redis.store[delivery.conversation_key("c-1")] = json.dumps({"conversation_id": "c-1"})
    assert asyncio.run(delivery.bot_conversation("c-1")) is None


def test_nothing_is_recorded_without_a_service_url(redis):
    asyncio.run(delivery.remember_bot_conversation("c-1", "", "personal"))
    assert redis.store == {}


def test_the_card_message_carries_the_card_and_the_question_as_preview():
    card = {"type": "AdaptiveCard", "body": []}
    decision = SimpleNamespace(ask={"question": "Ship  the fix\ntonight?"})
    message = delivery.card_message(card, decision)
    assert message.metadata["adaptive_card"] is card
    assert message.text == "Ship the fix tonight?"


def _item(**fields):
    values = {"id": uuid.uuid4(), "needs_human": True}
    values.update(fields)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("spec", "looked_up"),
    [
        ({"channel": "teams_channel", "team_id": "t", "channel_id": "19:c@thread.tacv2"}, ("bot", "19:c@thread.tacv2")),
        ({"channel": "teams_dm", "chat_id": "19:g@thread.v2"}, ("bot", "19:g@thread.v2")),
        ({"channel": "teams_dm", "user": "ada@example.com"}, ("personal", "ada@example.com")),
    ],
)
def test_the_bot_target_follows_the_notify_target(monkeypatch, spec, looked_up):
    seen = []

    async def _bot(cid):
        seen.append(("bot", cid))

    async def _personal(email):
        seen.append(("personal", email))

    monkeypatch.setattr(delivery, "bot_conversation", _bot)
    monkeypatch.setattr(delivery, "personal_bot_conversation", _personal)
    asyncio.run(work_item_notify._bot_target(spec))
    assert seen == [looked_up]


def test_the_accounts_own_chat_never_goes_to_the_bot(monkeypatch):
    async def _never(_cid):
        raise AssertionError("not looked up")

    monkeypatch.setattr(delivery, "bot_conversation", _never)
    spec = {"channel": "teams_dm", "chat_id": work_item_notify.SELF_CHAT_ID}
    assert asyncio.run(work_item_notify._bot_target(spec)) is None


def test_an_item_waiting_for_nobody_keeps_the_text_path(monkeypatch):
    async def _never(_spec):
        raise AssertionError("no card for an item that waits for no decision")

    monkeypatch.setattr(work_item_notify, "_bot_target", _never)
    assert asyncio.run(work_item_notify._send_decision_card(_item(needs_human=False), {})) is False


def test_without_the_bot_in_the_target_the_text_path_stays(monkeypatch):
    async def _none(_spec):
        return None

    monkeypatch.setattr(work_item_notify, "_bot_target", _none)
    assert asyncio.run(work_item_notify._send_decision_card(_item(), {"user": "a@b.c"})) is False


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_the_open_decision_goes_as_the_bots_card(monkeypatch):
    conversation = {"service_url": SERVICE, "conversation_id": "a:1", "conversation_type": "personal"}
    decision = SimpleNamespace(public_id="work_item:x")
    sent = []

    async def _target(_spec):
        return conversation

    async def _open(_db, source_type, _source_id):
        assert source_type == "work_item"
        return decision

    async def _send(_db, row, conv, *, viewer, language):
        sent.append((row, conv, viewer))
        return True

    monkeypatch.setattr(work_item_notify, "_bot_target", _target)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    monkeypatch.setattr(delivery, "open_decision", _open)
    monkeypatch.setattr(delivery, "send_decision_card", _send)
    assert asyncio.run(work_item_notify._send_decision_card(_item(), {"user": "a@b.c"})) is True
    assert sent == [(decision, conversation, None)]


def test_a_failed_card_send_falls_back_to_the_text(monkeypatch):
    async def _target(_spec):
        raise RuntimeError("redis down")

    monkeypatch.setattr(work_item_notify, "_bot_target", _target)
    assert asyncio.run(work_item_notify._send_decision_card(_item(), {"user": "a@b.c"})) is False


def test_a_paused_run_without_a_decision_keeps_its_text(monkeypatch):
    from app.services import notify

    async def _none(_db, _type, _id):
        return None

    monkeypatch.setattr(delivery, "open_decision", _none)
    run = SimpleNamespace(id=uuid.uuid4())
    ref = {"service_url": SERVICE, "conversation_id": "a:1"}
    assert asyncio.run(notify._send_run_decision_card(None, run, SimpleNamespace(), ref)) is False


def test_a_paused_run_sends_its_card_to_the_owner(monkeypatch):
    from app.services import notify

    decision = SimpleNamespace(public_id="run:x")
    user = SimpleNamespace(language="fr")
    calls = []

    async def _open(_db, source_type, _id):
        assert source_type == "run"
        return decision

    async def _send(_db, row, conv, *, viewer, language):
        calls.append((row, conv["conversation_id"], viewer))
        return True

    monkeypatch.setattr(delivery, "open_decision", _open)
    monkeypatch.setattr(delivery, "send_decision_card", _send)
    run = SimpleNamespace(id=uuid.uuid4())
    ref = {"service_url": SERVICE, "conversation_id": "a:1"}
    assert asyncio.run(notify._send_run_decision_card(None, run, user, ref)) is True
    assert calls == [(decision, "a:1", user)]
