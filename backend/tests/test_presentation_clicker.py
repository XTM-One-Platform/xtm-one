"""Hand the clicker (xtm-one #10552, slice B): one holder at a time, relayed to
the presenter over their activity stream, deck access required, take-back."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid

import pytest


class _FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.published: list[tuple[str, dict]] = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value
        if ex is not None:
            self.expiries[key] = ex

    async def expire(self, key, ttl):
        if key in self.values:
            self.expiries[key] = ttl

    async def delete(self, key):
        self.values.pop(key, None)

    async def publish(self, channel, payload):
        self.published.append((channel, json.loads(payload)))


def _patch(monkeypatch, fake):
    from app.services import presentation_clicker as mod

    async def _factory():
        return fake

    monkeypatch.setattr(mod, "redis_client", _factory)
    return mod


def test_handing_the_clicker_replaces_the_holder_one_at_a_time(monkeypatch):
    fake = _FakeRedis()
    mod = _patch(monkeypatch, fake)
    deck, presenter, alice, bob = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    asyncio.run(mod.hand_clicker(deck, presenter_id=presenter, holder_id=alice, holder_name="Alice"))
    asyncio.run(mod.hand_clicker(deck, presenter_id=presenter, holder_id=bob, holder_name="Bob"))
    entry = asyncio.run(mod.read_clicker(deck))
    assert entry["holder_id"] == str(bob) and entry["holder_name"] == "Bob"
    assert entry["presenter_id"] == str(presenter)
    # A holder whose tab died stops holding it: the entry expires.
    assert fake.expiries[mod.clicker_key(deck)] == mod.CLICKER_TTL_SECONDS


def test_taking_the_clicker_back_leaves_no_holder(monkeypatch):
    mod = _patch(monkeypatch, _FakeRedis())
    deck = uuid.uuid4()
    asyncio.run(mod.hand_clicker(deck, presenter_id=uuid.uuid4(), holder_id=uuid.uuid4(), holder_name=None))
    asyncio.run(mod.release_clicker(deck))
    assert asyncio.run(mod.read_clicker(deck)) is None


def test_a_command_reaches_the_presenter_on_their_activity_channel(monkeypatch):
    fake = _FakeRedis()
    mod = _patch(monkeypatch, fake)
    deck, presenter, holder = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    frame = mod.clicker_frame(deck, command="goto", index=4, sender_id=holder)
    asyncio.run(mod.publish_to_user(presenter, frame))
    channel, payload = fake.published[0]
    assert channel == f"activity:{presenter}"
    assert payload["type"] == mod.CLICKER_EVENT_COMMAND
    assert payload["command"] == "goto" and payload["index"] == 4
    assert payload["presentation_id"] == str(deck) and payload["from_user_id"] == str(holder)
    # The index only travels with goto.
    assert mod.clicker_frame(deck, command="next", index=4, sender_id=holder)["index"] is None


def test_redis_down_reads_no_clicker_and_refuses_to_hand_it(monkeypatch):
    from app.services import presentation_clicker as mod

    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(mod, "redis_client", _down)
    deck = uuid.uuid4()
    assert asyncio.run(mod.read_clicker(deck)) is None
    with pytest.raises(mod.ClickerUnavailableError):
        asyncio.run(mod.hand_clicker(deck, presenter_id=uuid.uuid4(), holder_id=uuid.uuid4(), holder_name=None))


def test_a_malformed_entry_reads_as_no_clicker(monkeypatch):
    fake = _FakeRedis()
    mod = _patch(monkeypatch, fake)
    deck = uuid.uuid4()
    fake.values[mod.clicker_key(deck)] = "not json"
    assert asyncio.run(mod.read_clicker(deck)) is None
    fake.values[mod.clicker_key(deck)] = json.dumps({"holder_id": 3})
    assert asyncio.run(mod.read_clicker(deck)) is None


def test_the_routes_require_deck_access_and_the_current_holder():
    """The holder must be able to read the deck (deck_permission, never a public
    link: the route takes a user id), only the holder drives (409 otherwise),
    the presenter or the holder releases it, every hand and release is audited,
    and commands are budgeted per person and deck."""
    from app.api.v1 import presentations as routes

    hand = inspect.getsource(routes.hand_presentation_clicker)
    assert "_load_visible(db, user, presentation_id, without_spec=True)" in hand
    assert "deck_permission(" in hand and "holder.is_active" in hand
    assert "HTTP_404_NOT_FOUND" in hand
    assert "await log_audit(" in hand and "CLICKER_EVENT_STATE" in hand

    command = inspect.getsource(routes.presentation_clicker_command)
    assert source_order(command, "await enforce_rate_limit(", "await _load_visible(")
    assert "limit=CLICKER_COMMANDS_PER_MINUTE" in command
    assert 'entry["holder_id"] != str(user.id)' in command and "HTTP_409_CONFLICT" in command
    assert 'entry["presenter_id"]' in command

    release = inspect.getsource(routes.release_presentation_clicker)
    assert 'me not in (entry["presenter_id"], entry["holder_id"])' in release
    assert "await log_audit(" in release

    read = inspect.getsource(routes.get_presentation_clicker)
    assert "keep_clicker" in read


def source_order(source: str, first: str, second: str) -> bool:
    return source.index(first) < source.index(second)


def test_the_command_schema_takes_only_moves():
    from pydantic import ValidationError

    from app.api.v1.schemas.presentation import PresentationClickerCommandRequest

    assert PresentationClickerCommandRequest(command="goto", index=2).index == 2
    for bad in ({"command": "blackout"}, {"command": "goto", "index": -1}, {"command": "exec"}):
        with pytest.raises(ValidationError):
            PresentationClickerCommandRequest(**bad)
