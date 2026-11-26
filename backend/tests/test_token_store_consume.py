"""Tests for the atomic single-use consume helpers in ``services/token_store``.

These pin the contract that two concurrent callers cannot both succeed when
consuming the same invitation or password-reset token. The consume path uses
Redis ``GETDEL`` (Redis 6.2+) so the read+delete is a single round-trip, and a
regression to the older non-atomic ``get`` + ``delete`` shape would let both
callers pass the read and race on the downstream state mutation
(``register-with-invitation`` would create two users for the same email, the
DB's ``UNIQUE(email)`` would arbitrate; ``reset-password`` has no such
backstop so both callers could set the password and last-write-wins).

These are pure unit tests against a minimal in-memory fake Redis — the same
shape as ``test_invitation_dedup.py``. No real Redis is contacted.
"""

from __future__ import annotations

import json

import pytest

from app.core.security import hash_token
from app.services import token_store


class FakeRedisAtomic:
    """In-memory Redis with the GETDEL semantics we rely on for single-use."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.getdel_calls: list[str] = []

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def getdel(self, key):
        self.getdel_calls.append(key)
        return self.store.pop(key, None)

    async def delete(self, key):
        existed = key in self.store
        self.store.pop(key, None)
        return 1 if existed else 0


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedisAtomic()

    async def _client():
        return fake

    monkeypatch.setattr(token_store, "redis_client", _client)
    return fake


# ─── consume_invitation ──────────────────────────────────────────────────────


async def test_consume_invitation_returns_payload_and_deletes_key(fake_redis):
    """First caller must receive the decoded payload AND the Redis key must
    be gone afterwards so a replay returns ``None``.
    """
    raw, token_hash, _ = await token_store.create_invitation(
        email="alice@example.com", role="user", created_by_id="admin", ttl_hours=48
    )
    key = f"invite:{token_hash}"
    assert key in fake_redis.store

    payload = await token_store.consume_invitation(raw)

    assert payload is not None
    assert payload["email"] == "alice@example.com"
    assert payload["role"] == "user"
    assert key not in fake_redis.store, "GETDEL must remove the key on the consume path"


async def test_consume_invitation_second_call_returns_none(fake_redis):
    """Second concurrent caller MUST get ``None`` — this is the property the
    route relies on to short-circuit a replay with 410. A regression to
    plain ``get`` + ``delete`` would let both calls see the payload.
    """
    raw, _, _ = await token_store.create_invitation(
        email="bob@example.com", role="admin", created_by_id="admin", ttl_hours=24
    )

    first = await token_store.consume_invitation(raw)
    second = await token_store.consume_invitation(raw)

    assert first is not None
    assert second is None


async def test_consume_invitation_uses_hashed_key_not_raw_token(fake_redis):
    """Lookup key MUST be the SHA-256 hash of the raw token (via
    :func:`app.core.security.hash_token`), never the raw token itself.

    The raw token is high-entropy (``secrets.token_urlsafe(32)`` ⇒
    256 bits) and therefore not brute-forceable from a hashed key, so a
    leaked Redis dump alone cannot be reversed into a usable invitation.
    The plain-SHA-256 (vs HMAC-keyed) primitive is sufficient here
    because the hash exists only to make the storage key safe to log /
    enumerate / dump — it is not a credential-strengthening step like
    a password hash, where a key would be needed to defeat rainbow
    tables against low-entropy inputs.
    """
    raw, token_hash, _ = await token_store.create_invitation(
        email="carol@example.com", role="user", created_by_id="admin", ttl_hours=24
    )

    await token_store.consume_invitation(raw)

    assert fake_redis.getdel_calls == [f"invite:{token_hash}"]
    assert hash_token(raw) == token_hash, "key derivation must come from hash_token()"
    assert raw not in fake_redis.getdel_calls
    assert f"invite:{raw}" not in fake_redis.getdel_calls


async def test_consume_invitation_unknown_token_returns_none(fake_redis):
    """A never-issued or already-expired token MUST return ``None`` cleanly
    (not raise) so the route can map it to a 410 without leaking info.
    """
    assert await token_store.consume_invitation("nope_not_a_real_token") is None


async def test_consume_invitation_does_not_revive_peek_only_path(fake_redis):
    """After ``consume_invitation`` removes the key, the peek-only
    ``get_invitation`` MUST also see it as gone — they must operate on
    the same storage key (i.e. nothing duplicates state in two places).
    """
    raw, _, _ = await token_store.create_invitation(
        email="dave@example.com", role="user", created_by_id="admin", ttl_hours=24
    )

    assert await token_store.get_invitation(raw) is not None
    await token_store.consume_invitation(raw)
    assert await token_store.get_invitation(raw) is None


# ─── consume_password_reset ──────────────────────────────────────────────────


async def test_consume_password_reset_returns_payload_and_deletes_key(fake_redis):
    """First caller receives the payload; the Redis key is gone afterwards
    so the second caller can't replay. Same shape as the invitation case
    — pinned separately because the route surfaces are independent.
    """
    raw, _ = await token_store.create_password_reset(
        user_id="11111111-1111-1111-1111-111111111111",
        created_by_id="admin",
        ttl_hours=24,
        email="user@example.com",
    )
    key = f"pwreset:{hash_token(raw)}"
    assert key in fake_redis.store

    payload = await token_store.consume_password_reset(raw)

    assert payload is not None
    assert payload["user_id"] == "11111111-1111-1111-1111-111111111111"
    assert key not in fake_redis.store


async def test_consume_password_reset_second_call_returns_none(fake_redis):
    """The core single-use guarantee for ``reset-password``: only one of
    two concurrent reset calls with the same token can win, the other
    gets ``None`` → 410. Without this, both could set a password and
    last-write-wins arbitrates with no audit trail.
    """
    raw, _ = await token_store.create_password_reset(
        user_id="22222222-2222-2222-2222-222222222222",
        created_by_id="admin",
        ttl_hours=24,
        email="user@example.com",
    )

    first = await token_store.consume_password_reset(raw)
    second = await token_store.consume_password_reset(raw)

    assert first is not None
    assert second is None


async def test_consume_password_reset_uses_hashed_key_not_raw_token(fake_redis):
    """Same hash-not-raw key contract as the invitation path."""
    raw, _ = await token_store.create_password_reset(
        user_id="33333333-3333-3333-3333-333333333333",
        created_by_id="admin",
        ttl_hours=24,
        email="user@example.com",
    )

    await token_store.consume_password_reset(raw)

    assert fake_redis.getdel_calls == [f"pwreset:{hash_token(raw)}"]
    assert raw not in fake_redis.getdel_calls


async def test_consume_password_reset_unknown_token_returns_none(fake_redis):
    """Unknown / already-expired tokens cleanly return ``None`` instead
    of raising — same shape as the invitation case.
    """
    assert await token_store.consume_password_reset("nope") is None


async def test_consume_password_reset_does_not_double_dispatch(fake_redis):
    """Belt-and-suspenders: the consume helper must perform exactly ONE
    Redis round-trip (``getdel``) and never fall back to a non-atomic
    get-then-delete shape — that would re-open the race.
    """
    raw, _ = await token_store.create_password_reset(
        user_id="44444444-4444-4444-4444-444444444444",
        created_by_id="admin",
        ttl_hours=24,
        email="user@example.com",
    )

    await token_store.consume_password_reset(raw)
    assert len(fake_redis.getdel_calls) == 1, (
        "consume_password_reset must use a single GETDEL — a get+delete pair "
        "would re-introduce the race window the helper exists to close."
    )


# ─── Payload fidelity ────────────────────────────────────────────────────────


async def test_consume_invitation_payload_round_trips_json(fake_redis):
    """Sanity guard that the consume helper actually JSON-decodes the
    stored payload — a regression that returned the raw bytes would break
    every caller silently.
    """
    raw, token_hash, _ = await token_store.create_invitation(
        email="eve@example.com", role="user", created_by_id="admin", ttl_hours=24
    )
    raw_stored = fake_redis.store[f"invite:{token_hash}"]
    assert json.loads(raw_stored)["email"] == "eve@example.com"

    payload = await token_store.consume_invitation(raw)
    assert isinstance(payload, dict)
    assert payload["email"] == "eve@example.com"
