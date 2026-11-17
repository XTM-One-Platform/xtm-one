"""Regression tests for the SEC-10 rate-limit expansion.

Pins the new throttles added on previously unthrottled surfaces:

* REST chat message sends — per-user bucket (``chat-send``), 429 past
  ``CHAT_MESSAGES_PER_MINUTE``.
* Web chat message sends — same per-user bucket; an over-limit send is a
  429 with ``Retry-After`` before any stream, and starts no turn (an
  over-limit steer is the steer route's 429, ``test_chat_web_steering.py``).
* Assignment webhook receivers — per-assignment bucket
  (``webhook-assignment``), 429 past ``WEBHOOK_REQUESTS_PER_MINUTE``.

Also pins the new ``bucket`` parameter on ``enforce_rate_limit`` (explicit
key instead of the client IP, ``request`` optional).
"""

from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import chat as chat_module
from app.api.v1 import webhooks as webhooks_module
from app.core import config, rate_limit

from .test_chat_web_steering import _patch_general_chat, _send, fake_rl, ws_env  # noqa: F401

# ── Fake Redis ───────────────────────────────────────────────────────────


class _FakeRedis:
    """Implements just enough of the Lua EVAL path for ``enforce_rate_limit``."""

    def __init__(self):
        self.store: dict[str, int] = {}
        # Per-key remaining window, consulted only on the throttled path to
        # build ``Retry-After``. Absent keys report -1 (Redis "no TTL").
        self.ttls: dict[str, int] = {}

    async def eval(self, script, _numkeys, key, _window, *amount):
        assert (script == rate_limit.INCRBY_EXPIRE_LUA) == bool(amount)
        self.store[key] = self.store.get(key, 0) + (int(amount[0]) if amount else 1)
        return self.store[key]

    async def ttl(self, key):
        return self.ttls.get(key, -1)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr(config.settings, "rate_limit_fail_mode", "open")
    return fake


# ── enforce_rate_limit bucket parameter ──────────────────────────────────


async def test_enforce_rate_limit_uses_explicit_bucket(fake_redis):
    """An explicit bucket must key the counter (no Request needed)."""
    await rate_limit.enforce_rate_limit(
        None, "chat-send", limit=5, window_seconds=60, bucket="user-123"
    )
    assert fake_redis.store == {"rl:chat-send:user-123": 1}


async def test_a_cost_spends_a_budget_of_units_and_the_crossing_request_is_refused(fake_redis):
    """The public video media charge each answer its bytes."""
    budget = {"limit": 10, "window_seconds": 60, "bucket": "203.0.113.7"}
    await rate_limit.enforce_rate_limit(None, "video-bytes", cost=4, **budget)
    await rate_limit.enforce_rate_limit(None, "video-bytes", cost=6, **budget)
    assert fake_redis.store == {"rl:video-bytes:203.0.113.7": 10}
    with pytest.raises(HTTPException) as exc:
        await rate_limit.enforce_rate_limit(None, "video-bytes", cost=1, **budget)
    assert exc.value.status_code == 429
    # One unit is the plain request counter, the script every other limiter runs.
    await rate_limit.enforce_rate_limit(None, "requests", limit=5, window_seconds=60, bucket="203.0.113.7")


async def test_enforce_rate_limit_requires_request_or_bucket(fake_redis):
    with pytest.raises(ValueError):
        await rate_limit.enforce_rate_limit(
            None, "chat-send", limit=5, window_seconds=60
        )


# ── Retry-After on 429 ───────────────────────────────────────────


async def test_429_carries_retry_after_from_the_window_ttl(fake_redis):
    """Machine senders back off on ``Retry-After`` and otherwise hot-retry
    into the same closed window — a bare 429 turns an alert storm into a
    retry storm. The value must be the time actually left in the window."""
    key = "rl:webhook-assignment:target-1"
    fake_redis.store[key] = 60
    fake_redis.ttls[key] = 17

    with pytest.raises(HTTPException) as exc:
        await rate_limit.enforce_rate_limit(
            None, "webhook-assignment", limit=60, window_seconds=60, bucket="target-1"
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "17"


async def test_retry_after_falls_back_to_the_full_window(fake_redis):
    """A key with no TTL (RDB restore, manual ops action) must still produce a
    usable header — over-estimating is safe, omitting it is not."""
    key = "rl:webhook-assignment:target-2"
    fake_redis.store[key] = 60

    with pytest.raises(HTTPException) as exc:
        await rate_limit.enforce_rate_limit(
            None, "webhook-assignment", limit=60, window_seconds=60, bucket="target-2"
        )

    assert exc.value.headers["Retry-After"] == "60"


async def test_retry_after_survives_a_redis_failure(monkeypatch, fake_redis):
    """The TTL lookup is best-effort: a Redis blip while building the header
    must degrade to the full window, never replace the 429 with a 500."""

    async def _boom(_key):
        raise RuntimeError("redis gone")

    monkeypatch.setattr(fake_redis, "ttl", _boom)
    fake_redis.store["rl:webhook-assignment:target-3"] = 60

    with pytest.raises(HTTPException) as exc:
        await rate_limit.enforce_rate_limit(
            None, "webhook-assignment", limit=60, window_seconds=60, bucket="target-3"
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"


# ── Configurable webhook throttle ────────────────────────────────


async def test_webhook_limit_is_configurable(monkeypatch):
    """An alert storm is exactly when deliveries matter most; the hard-coded
    60/min made a legitimate high-cadence feed unusable. A 0/negative override
    must clamp to 1 rather than disabling the throttle."""
    monkeypatch.setattr(config.settings, "webhook_requests_per_minute", 600)
    assert webhooks_module._webhook_requests_per_minute() == 600

    monkeypatch.setattr(config.settings, "webhook_requests_per_minute", 0)
    assert webhooks_module._webhook_requests_per_minute() == 1


# ── REST chat send-message throttle ──────────────────────────────────────


class _NoConversationDB:
    """DB stub whose conversation lookup finds nothing (clean 404 marker
    that the limiter let the request through)."""

    async def execute(self, _stmt):
        class _R:
            def scalar_one_or_none(self):
                return None

        return _R()


async def test_rest_chat_send_over_limit_returns_429(fake_redis):
    user = SimpleNamespace(id=uuid.uuid4())
    # Saturate the per-user bucket so the next send exceeds the limit.
    fake_redis.store[f"rl:chat-send:{user.id}"] = chat_module.CHAT_MESSAGES_PER_MINUTE

    with pytest.raises(HTTPException) as exc:
        await chat_module.send_message(
            conversation_id=str(uuid.uuid4()),
            body=chat_module.SendMessageRequest(content="hello"),
            user=user,
            db=_NoConversationDB(),
        )
    assert exc.value.status_code == 429


async def test_rest_chat_send_under_limit_passes(fake_redis):
    """Under the limit the request must reach the conversation lookup —
    the stub DB finds nothing, so a 404 (not 429) proves the limiter
    passed the request through."""
    user = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        await chat_module.send_message(
            conversation_id=str(uuid.uuid4()),
            body=chat_module.SendMessageRequest(content="hello"),
            user=user,
            db=_NoConversationDB(),
        )
    assert exc.value.status_code == 404
    assert fake_redis.store[f"rl:chat-send:{user.id}"] == 1


# ── Web chat send throttle ───────────────────────────────────────────────


async def test_chat_send_over_limit_is_refused_with_a_429(monkeypatch, ws_env):  # noqa: F811
    """An over-limit send is answered 429 with ``Retry-After`` before its
    stream starts and starts no turn, so the client keeps the
    conversation open and retries after the window resets."""
    ran: list[str] = []

    async def _run(*_a, **_kw):
        ran.append("turn")
        return ("answer", None)

    _patch_general_chat(monkeypatch, _run)
    ws_env.fake_rl.store[f"rl:chat-send:{ws_env.user_id}"] = chat_module.CHAT_MESSAGES_PER_MINUTE

    conn = await _send(ws_env, {"content": "hello"})

    assert conn.refusal is not None, "an over-limit send must be refused"
    assert conn.refusal.status_code == 429
    assert "Too many requests" in conn.refusal.detail
    assert int(conn.refusal.headers["Retry-After"]) > 0
    assert conn.sent == [], "an over-limit message must not start agent processing"
    assert ran == []


async def test_chat_send_under_limit_starts_processing(monkeypatch, ws_env):  # noqa: F811
    """Under the limit the send proceeds to processing — the 'thinking'
    event proves the limiter passed the message through, and the send
    spent one unit of the per-user bucket."""

    async def _run(*_a, **_kw):
        return ("answer", None)

    _patch_general_chat(monkeypatch, _run)

    conn = await _send(ws_env, {"content": "hello"})

    assert any(m.get("type") == "thinking" for m in conn.sent), (
        "an under-limit message must reach agent processing"
    )
    assert ws_env.fake_rl.store[f"rl:chat-send:{ws_env.user_id}"] == 1


# ── Webhook receiver throttle ────────────────────────────────────────────


class _BodySentinelError(Exception):
    """Raised by the request stub's ``body()`` — proves the handler got
    past the rate limiter (which runs before the body is read)."""


class _WebhookRequest:
    def __init__(self):
        self.headers = {}
        self.client = SimpleNamespace(host="203.0.113.30")

    async def body(self):
        raise _BodySentinelError


async def test_direct_assignment_webhook_over_limit_returns_429(fake_redis):
    assignment_id = str(uuid.uuid4())
    fake_redis.store[f"rl:webhook-assignment:{assignment_id}"] = (
        webhooks_module._webhook_requests_per_minute()
    )

    with pytest.raises(HTTPException) as exc:
        await webhooks_module.receive_direct_assignment_webhook(
            assignment_id, _WebhookRequest()
        )
    assert exc.value.status_code == 429


async def test_direct_assignment_webhook_under_limit_passes(fake_redis):
    """Under the limit the handler must proceed to read the body — the
    sentinel from ``body()`` (instead of a 429) proves the limiter let
    the delivery through."""
    assignment_id = str(uuid.uuid4())

    with pytest.raises(_BodySentinelError):
        await webhooks_module.receive_direct_assignment_webhook(
            assignment_id, _WebhookRequest()
        )
    assert fake_redis.store[f"rl:webhook-assignment:{assignment_id}"] == 1


async def test_integration_assignment_webhook_over_limit_returns_429(fake_redis):
    assignment_id = str(uuid.uuid4())
    fake_redis.store[f"rl:webhook-assignment:{assignment_id}"] = (
        webhooks_module._webhook_requests_per_minute()
    )

    with pytest.raises(HTTPException) as exc:
        await webhooks_module.receive_assignment_webhook(
            str(uuid.uuid4()), assignment_id, _WebhookRequest()
        )
    assert exc.value.status_code == 429


async def test_direct_assignment_webhook_bucket_is_uuid_canonical(fake_redis):
    """``uuid.UUID()`` accepts many string forms (uppercase, no-hyphen hex,
    braces) that all reach the same assignment downstream — every variant
    must land in ONE canonical bucket, otherwise a sender evades the
    per-assignment throttle by varying the formatting of the same ID."""
    assignment_id = str(uuid.uuid4())
    fake_redis.store[f"rl:webhook-assignment:{assignment_id}"] = (
        webhooks_module._webhook_requests_per_minute()
    )

    for variant in (
        assignment_id.upper(),
        assignment_id.replace("-", ""),
        "{" + assignment_id + "}",
    ):
        with pytest.raises(HTTPException) as exc:
            await webhooks_module.receive_direct_assignment_webhook(
                variant, _WebhookRequest()
            )
        assert exc.value.status_code == 429, (
            f"variant {variant!r} must hit the saturated canonical bucket"
        )

    # No variant may have created a second bucket for the same assignment.
    assert list(fake_redis.store) == [f"rl:webhook-assignment:{assignment_id}"]


async def test_integration_assignment_webhook_bucket_is_uuid_canonical(fake_redis):
    assignment_id = str(uuid.uuid4())
    fake_redis.store[f"rl:webhook-assignment:{assignment_id}"] = (
        webhooks_module._webhook_requests_per_minute()
    )

    with pytest.raises(HTTPException) as exc:
        await webhooks_module.receive_assignment_webhook(
            str(uuid.uuid4()), assignment_id.upper(), _WebhookRequest()
        )
    assert exc.value.status_code == 429
    assert list(fake_redis.store) == [f"rl:webhook-assignment:{assignment_id}"]


def _legacy_webhook_bucket_key(agent_id: str, path: str) -> str:
    """Mirror the legacy endpoint's bucket: the ``{path:path}`` segment is
    attacker-controlled and unbounded, so it is hashed into the Redis key."""
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return f"rl:webhook-assignment:{agent_id}:{digest}"


async def test_legacy_agent_webhook_over_limit_returns_429(fake_redis):
    agent_id = str(uuid.uuid4())
    fake_redis.store[_legacy_webhook_bucket_key(agent_id, "github")] = (
        webhooks_module._webhook_requests_per_minute()
    )

    with pytest.raises(HTTPException) as exc:
        await webhooks_module.receive_webhook(agent_id, "github", _WebhookRequest())
    assert exc.value.status_code == 429


async def test_legacy_agent_webhook_bucket_key_is_bounded(fake_redis):
    """A multi-KB ``{path:path}`` value must not become a multi-KB Redis
    key — the path segment is hashed into a fixed-size digest."""
    agent_id = str(uuid.uuid4())
    long_path = "a/" * 4096  # 8 KiB catch-all path

    with pytest.raises(_BodySentinelError):
        await webhooks_module.receive_webhook(
            agent_id, long_path, _WebhookRequest()
        )

    keys = list(fake_redis.store)
    assert keys == [_legacy_webhook_bucket_key(agent_id, long_path)]
    # rl:webhook-assignment: + 36-char UUID + : + 16-char digest
    assert len(keys[0]) < 100


async def test_legacy_agent_webhook_bucket_is_uuid_canonical(fake_redis):
    """An uppercase / no-hyphen form of the same agent UUID must land in
    the canonical bucket — formatting variants must not multiply the
    effective per-target limit."""
    agent_id = str(uuid.uuid4())
    fake_redis.store[_legacy_webhook_bucket_key(agent_id, "github")] = (
        webhooks_module._webhook_requests_per_minute()
    )

    with pytest.raises(HTTPException) as exc:
        await webhooks_module.receive_webhook(
            agent_id.upper(), "github", _WebhookRequest()
        )
    assert exc.value.status_code == 429
    assert list(fake_redis.store) == [_legacy_webhook_bucket_key(agent_id, "github")]
