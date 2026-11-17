"""``token_store.reissue_invitation``: one pending invitation per email, sent again under a new link.

Only a link's hash is stored, so the link an invitation was first sent with
cannot be sent again: a reissue moves the invitation to a new token, keeps its
email and role, and gives it the longer of what it had left and the lifetime
asked. In-memory Redis, as in ``test_token_store_consume.py``; the last tests
run the move itself against a real Redis (``REDIS_URL``), and skip without one.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as redis_asyncio

from app.services import token_store


class _Redis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiry: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.expiry[key] = ex

    async def get(self, key):
        return self.store.get(key)

    async def eval(self, script, _numkeys, old_key, new_key, expected, value, ttl):
        assert script == token_store.REISSUE_INVITATION_LUA
        if self.store.get(old_key) != expected:
            return 0
        del self.store[old_key], self.expiry[old_key]
        await self.set(new_key, value, ex=ttl)
        return 1

    async def ttl(self, key):
        return self.expiry.get(key, -2) if key in self.store else -2

    async def scan_iter(self, match):
        prefix = match.rstrip("*")
        for key in list(self.store):
            if key.startswith(prefix):
                yield key


@pytest.fixture
def redis(monkeypatch):
    fake = _Redis()

    async def _client():
        return fake

    monkeypatch.setattr(token_store, "redis_client", _client)
    return fake


def _around(expected: datetime, actual: datetime) -> bool:
    return abs(actual - expected) < timedelta(seconds=5)


async def test_a_reissued_invitation_keeps_its_email_and_role_under_a_new_link(redis):
    first, first_hash, _ = await token_store.create_invitation(
        email="Jane@Acme.com",
        role="user",
        created_by_id="admin",
        ttl_hours=48,
        destination={"platform": "opencrq", "path": "/t-1"},
    )
    created = await token_store.get_invitation(first)

    second, second_hash, _ = await token_store.reissue_invitation(
        first_hash, ttl_hours=48, destination={"platform": "opencrq", "path": "/t-2"}
    )

    assert second != first
    assert await token_store.get_invitation(first) is None
    payload = await token_store.get_invitation(second)
    assert (payload["email"], payload["role"], payload["created_at"]) == (
        "jane@acme.com",
        "user",
        created["created_at"],
    )
    assert payload["destination"] == {"platform": "opencrq", "path": "/t-2"}
    [pending] = await token_store.list_invitations()
    assert pending["id"] == second_hash


@pytest.mark.parametrize("seconds_left", [60, 3600, 48 * 3600], ids=["a-minute", "an-hour", "a-full-lifetime"])
async def test_a_reissued_link_is_valid_for_the_whole_lifetime_asked(redis, seconds_left):
    """Sent nearly expired, the new link would be dead before the person opens it."""
    _raw, token_hash, _ = await token_store.create_invitation(
        email="jane@acme.com", role="user", created_by_id="admin", ttl_hours=48
    )
    redis.expiry[f"invite:{token_hash}"] = seconds_left

    raw, new_hash, expires_at = await token_store.reissue_invitation(token_hash, ttl_hours=48)

    assert redis.expiry[f"invite:{new_hash}"] == 48 * 3600
    assert _around(datetime.now(UTC) + timedelta(hours=48), expires_at)
    assert (await token_store.get_invitation(raw))["expires_at"] == expires_at.isoformat()


async def test_a_reissue_keeps_a_longer_lifetime_chosen_at_creation(redis):
    _raw, token_hash, _ = await token_store.create_invitation(
        email="jane@acme.com", role="user", created_by_id="admin", ttl_hours=720
    )
    redis.expiry[f"invite:{token_hash}"] = 600 * 3600

    raw, new_hash, expires_at = await token_store.reissue_invitation(token_hash, ttl_hours=168)

    assert redis.expiry[f"invite:{new_hash}"] == 600 * 3600
    assert _around(datetime.now(UTC) + timedelta(hours=600), expires_at)
    assert (await token_store.get_invitation(raw))["expires_at"] == expires_at.isoformat()


async def test_a_reissue_without_a_destination_keeps_the_stored_one(redis):
    _raw, token_hash, _ = await token_store.create_invitation(
        email="jane@acme.com", role="admin", created_by_id="admin", ttl_hours=48
    )
    raw, _new_hash, _ = await token_store.reissue_invitation(token_hash, ttl_hours=48)

    payload = json.loads(next(iter(redis.store.values())))
    assert payload["role"] == "admin"
    assert "destination" not in payload
    assert (await token_store.get_invitation(raw))["email"] == "jane@acme.com"


async def test_a_gone_invitation_is_not_reissued(redis):
    assert await token_store.reissue_invitation("no-such-hash", ttl_hours=48) is None
    assert redis.store == {}


@pytest.fixture
async def real_redis(monkeypatch):
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        pytest.skip("REDIS_URL is not set")
    client = redis_asyncio.Redis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis unreachable at {url}: {exc}")
    email = f"reissue-{uuid.uuid4().hex}@acme.com"

    async def _client():
        return client

    monkeypatch.setattr(token_store, "redis_client", _client)
    try:
        yield client, email
    finally:
        for item in await token_store.list_invitations():
            if item["email"] == email:
                await client.delete(f"invite:{item['id']}")
        await client.aclose()


def _mine(invitations: list[dict], email: str) -> list[dict]:
    return [item for item in invitations if item["email"] == email]


@pytest.mark.integration
async def test_the_move_to_the_new_link_runs_on_a_real_redis(real_redis):
    client, email = real_redis
    first, first_hash, _ = await token_store.create_invitation(
        email=email, role="user", created_by_id="admin", ttl_hours=1
    )

    second, second_hash, _ = await token_store.reissue_invitation(first_hash, ttl_hours=48)

    assert await token_store.get_invitation(first) is None
    assert (await token_store.get_invitation(second))["email"] == email
    assert 47 * 3600 < await client.ttl(f"invite:{second_hash}") <= 48 * 3600
    assert [item["id"] for item in _mine(await token_store.list_invitations(), email)] == [second_hash]


@pytest.mark.integration
async def test_of_two_concurrent_reissues_one_wins_and_one_invitation_stays(real_redis):
    _client, email = real_redis
    _raw, token_hash, _ = await token_store.create_invitation(
        email=email, role="user", created_by_id="admin", ttl_hours=48
    )

    results = await asyncio.gather(
        token_store.reissue_invitation(token_hash, ttl_hours=48),
        token_store.reissue_invitation(token_hash, ttl_hours=48),
    )

    [won] = [result for result in results if result is not None]
    [pending] = _mine(await token_store.list_invitations(), email)
    assert pending["id"] == won[1]
