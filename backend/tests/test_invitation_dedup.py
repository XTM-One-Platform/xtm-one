"""Unit tests for invitation de-duplication in ``token_store``.

A still-valid pending invitation must block creating a second one for the
same email — the bug where an admin could stack duplicate pending invites
(``POST /api/v1/invitations`` calls ``find_invitation_by_email`` and returns
409 on a hit). The match is case- and whitespace-insensitive; revoking the
invite clears the block.

Pure unit test — a minimal in-memory fake stands in for Redis, so no real
Redis is contacted.
"""

from __future__ import annotations

import fnmatch

import pytest

from app.services import token_store


class FakeRedis:
    """Minimal in-memory async Redis covering the calls ``token_store`` makes."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        existed = key in self.store
        self.store.pop(key, None)
        return 1 if existed else 0

    async def scan_iter(self, match=None):
        for key in list(self.store.keys()):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(token_store, "redis_client", _client)
    return fake


async def test_find_invitation_by_email_returns_pending(fake_redis):
    await token_store.create_invitation(
        email="alice@example.com", role="user", created_by_id="admin", ttl_hours=48
    )
    found = await token_store.find_invitation_by_email("alice@example.com")
    assert found is not None
    assert found["email"] == "alice@example.com"
    assert found["role"] == "user"


async def test_find_invitation_by_email_is_case_and_space_insensitive(fake_redis):
    await token_store.create_invitation(
        email="Bob@Example.com", role="admin", created_by_id="admin", ttl_hours=48
    )
    assert await token_store.find_invitation_by_email("  BOB@example.COM  ") is not None


async def test_create_invitation_normalises_stored_email(fake_redis):
    """Stored payload must be in the canonical form (``.strip().lower()``).

    ``register_with_invitation`` reads the email back from this payload and
    feeds it directly to the case-insensitive ``User.email`` exists check —
    if the stored value disagreed with the issuance-time exists check
    (which already lowers), a mixed-case invite would silently authorise
    creating a duplicate row that the case-sensitive Postgres
    ``UNIQUE(email)`` would let through.  Pinning the storage-side
    normalisation here means a regression that drops the ``.lower()`` in
    ``token_store.create_invitation`` trips loudly without needing a
    full FastAPI test client.
    """
    _raw, token_hash, _exp = await token_store.create_invitation(
        email="  Foo@Example.com  ",
        role="user",
        created_by_id="admin",
        ttl_hours=48,
    )
    key = f"invite:{token_hash}"
    import json as _json

    stored = _json.loads(fake_redis.store[key])
    assert stored["email"] == "foo@example.com", (
        "create_invitation must store the email in the canonical "
        "(.strip().lower()) form so register_with_invitation can rely "
        "on a case-consistent identity comparison."
    )


async def test_find_invitation_by_email_none_when_absent(fake_redis):
    await token_store.create_invitation(
        email="alice@example.com", role="user", created_by_id="admin", ttl_hours=48
    )
    assert await token_store.find_invitation_by_email("nobody@example.com") is None


async def test_revoked_invitation_no_longer_blocks(fake_redis):
    _raw, token_hash, _exp = await token_store.create_invitation(
        email="carol@example.com", role="user", created_by_id="admin", ttl_hours=48
    )
    assert await token_store.find_invitation_by_email("carol@example.com") is not None
    await token_store.revoke_invitation(token_hash)
    assert await token_store.find_invitation_by_email("carol@example.com") is None
