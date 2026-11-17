"""Regression tests for webhook idempotency / deduplication.

Production bug: webhook-triggered assignments could be executed twice
for a single user-visible webhook fire whenever the same POST reached
the FastAPI handler twice (proxy retry on slow response, ALB target-
group retry, sender at-least-once delivery, etc.).  These tests pin the
dedup contract on ``backend/app/api/v1/webhooks.py``:

  * Idempotency key derivation prefers explicit headers, then
    provider-specific payload identifiers, and finally a SHA-256 of
    the URL scope + body.
  * ``_claim_webhook_dedup_slot`` is atomic across workers (uses
    Redis ``SET … NX EX``) and returns the cached response on retries.
  * Failing-open behaviour: a Redis outage logs a warning and lets the
    request through rather than 5xx-ing the webhook.
  * Slot release on exception (``_release_webhook_dedup_slot``) so a
    transient first-attempt failure (signature mismatch, DB blip)
    does not poison the cache and silently swallow legitimate retries
    with a generic ``"duplicate"`` reply.
  * ``_finalize_dedup`` persists a non-trigger response (early-return
    "ignored", filter skip) into the slot so retries replay the
    actual response instead of the misleading "duplicate" answer.

If any of these contracts regress, the user-visible symptom (two
``AssignmentRun`` rows with identical timestamps for one webhook fire,
or a misleading "duplicate" reply masking a real configuration issue)
returns.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from app.api.v1 import webhooks
from app.core.config import settings


def _make_request(headers: dict | None = None) -> SimpleNamespace:
    """Build a minimal stand-in for ``fastapi.Request`` with a ``.headers`` dict.

    The dedup helpers only call ``request.headers.get(...)``, so a plain
    dict-typed ``SimpleNamespace`` is sufficient and avoids pulling the
    full Starlette ``Request`` constructor (which requires a scope dict
    with very specific shape).
    """
    return SimpleNamespace(headers=headers or {})


# ---------------------------------------------------------------------------
# _extract_payload_dedup_id
# ---------------------------------------------------------------------------


def test_extract_payload_id_opencti_notification():
    payload = {"notification_id": "abc-123", "data": {"name": "x"}}
    assert webhooks._extract_payload_dedup_id(payload) == "opencti:abc-123"


def test_extract_payload_id_opencti_camelcase():
    """Some OpenCTI builds emit ``notificationId`` (camelCase) — both
    must resolve to the same prefix so dedup keys stay stable."""
    payload = {"notificationId": "abc-123"}
    assert webhooks._extract_payload_dedup_id(payload) == "opencti:abc-123"


def test_extract_payload_id_slack_event_id():
    """Slack Events API includes a globally-unique top-level ``event_id``
    on every delivery; using it lets us dedup Slack's own retries
    without falling back to a body hash."""
    payload = {
        "type": "event_callback",
        "event_id": "Ev123456",
        "event": {"type": "message"},
    }
    assert webhooks._extract_payload_dedup_id(payload) == "slack:Ev123456"


def test_extract_payload_id_hubspot_event_id():
    payload = {"subscriptionType": "contact.creation", "eventId": 555}
    assert webhooks._extract_payload_dedup_id(payload) == "hubspot:555"


def test_extract_payload_id_generic_type_and_id():
    payload = {"type": "stix2.indicator", "id": "indicator--abc"}
    assert (
        webhooks._extract_payload_dedup_id(payload)
        == "json:stix2.indicator:indicator--abc"
    )


def test_extract_payload_id_returns_none_for_bare_id():
    """Bare ``{"id": "..."}`` without a ``type`` is not stable enough
    (many JSON APIs reuse the same id across distinct events)."""
    assert webhooks._extract_payload_dedup_id({"id": "abc"}) is None


def test_extract_payload_id_returns_none_for_non_dict():
    assert webhooks._extract_payload_dedup_id([1, 2, 3]) is None
    assert webhooks._extract_payload_dedup_id("string") is None
    assert webhooks._extract_payload_dedup_id(None) is None


def test_extract_payload_id_returns_none_for_empty_dict():
    assert webhooks._extract_payload_dedup_id({}) is None


# ---------------------------------------------------------------------------
# _compute_webhook_dedup_key
# ---------------------------------------------------------------------------


def test_compute_key_prefers_idempotency_key_header():
    request = _make_request({"X-Idempotency-Key": "user-supplied-123"})
    body = b'{"id": "json-id", "type": "evt"}'
    key = webhooks._compute_webhook_dedup_key(request, body, scope="scope:1")
    assert key.startswith("hdr:")
    assert "user-supplied-123" in key


def test_compute_key_uses_github_delivery_uuid():
    """GitHub redeliveries use the same payload but a fresh delivery
    UUID — and we *do* want operators to be able to redeliver from the
    GitHub UI without hitting the dedup cache.  Using
    ``X-GitHub-Delivery`` (rather than the body hash) makes that work."""
    request = _make_request(
        {"X-GitHub-Delivery": "11111111-2222-3333-4444-555555555555"}
    )
    body = b'{"action": "opened"}'
    key1 = webhooks._compute_webhook_dedup_key(request, body, scope="scope:1")

    request2 = _make_request(
        {"X-GitHub-Delivery": "99999999-aaaa-bbbb-cccc-dddddddddddd"}
    )
    # Same body, *different* delivery → different keys.
    key2 = webhooks._compute_webhook_dedup_key(request2, body, scope="scope:1")
    assert key1 != key2
    assert "11111111" in key1
    assert "99999999" in key2


def test_compute_key_falls_back_to_payload_id_when_no_header():
    request = _make_request()
    body = b'{"notification_id": "opencti-evt-42"}'
    key = webhooks._compute_webhook_dedup_key(request, body, scope="scope:1")
    assert key == "pl:opencti:opencti-evt-42"


def test_compute_key_falls_back_to_sha256_for_unknown_payload():
    request = _make_request()
    body = b'{"random": "stuff", "without": "stable_id"}'
    key = webhooks._compute_webhook_dedup_key(request, body, scope="scope:1")
    assert key.startswith("sha256:")
    # Same body + same scope must yield the same hash on every call.
    assert key == webhooks._compute_webhook_dedup_key(
        _make_request(), body, scope="scope:1"
    )


def test_compute_key_scope_disambiguates_identical_body():
    """Two endpoints receiving the same body must compute different
    keys, otherwise a curl test against assignment A would
    incorrectly suppress a later test against assignment B."""
    request = _make_request()
    body = b'{"random": "stuff"}'
    key_a = webhooks._compute_webhook_dedup_key(request, body, scope="scope:A")
    key_b = webhooks._compute_webhook_dedup_key(request, body, scope="scope:B")
    assert key_a != key_b


def test_compute_key_handles_invalid_json_gracefully():
    """Form-encoded / binary webhook bodies must not break dedup —
    falling back to the SHA-256 path is the documented behaviour."""
    request = _make_request()
    body = b"this is definitely not json"
    key = webhooks._compute_webhook_dedup_key(request, body, scope="scope:1")
    assert key.startswith("sha256:")


def test_compute_key_handles_empty_body():
    """Some webhook senders POST with an empty body (e.g. ping
    notifications).  The function must not crash."""
    request = _make_request()
    key = webhooks._compute_webhook_dedup_key(request, b"", scope="scope:1")
    assert key.startswith("sha256:")


# ---------------------------------------------------------------------------
# _claim_webhook_dedup_slot
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Minimal Redis stand-in implementing only the ops the dedup layer uses.

    ``SET … NX EX`` semantics:
      - When the key does not yet exist, the value is stored and ``True``
        is returned.  redis-py actually returns ``True`` (or the integer
        ``1``) here — the dedup layer treats *any* truthy value as
        "first arrival".
      - When the key already exists, ``None`` is returned and the
        existing value is preserved.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                deleted += 1
        return deleted


@pytest.mark.asyncio
async def test_claim_first_call_returns_not_duplicate(monkeypatch):
    fake_redis = _FakeRedis()

    async def _redis_client():
        return fake_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    is_dup, cached, key = await webhooks._claim_webhook_dedup_slot(
        _make_request(),
        body=b'{"x": 1}',
        scope="assignments:abc",
    )

    assert is_dup is False
    assert cached is None
    assert key is not None
    assert key.startswith("webhook:dedup:assignments:abc:")
    # Pending marker is in place so retries arriving before persist receive
    # the generic ``{"status": "duplicate"}`` answer.
    assert fake_redis.store[key] == webhooks._WEBHOOK_DEDUP_PENDING_MARKER


@pytest.mark.asyncio
async def test_claim_second_call_with_pending_returns_generic_duplicate(monkeypatch):
    """Retry arrives WHILE the first call is still executing (response
    has not been persisted yet).  Must return ``deduplicated=True`` so
    the caller never re-triggers, even when no cached payload exists."""
    fake_redis = _FakeRedis()

    async def _redis_client():
        return fake_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    # First call: claims the slot.
    await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=b'{"x": 1}', scope="assignments:abc"
    )

    # Second call (before persist): receives the pending marker.
    is_dup, cached, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=b'{"x": 1}', scope="assignments:abc"
    )

    assert is_dup is True
    assert cached == {"status": "duplicate", "deduplicated": True}


@pytest.mark.asyncio
async def test_claim_second_call_returns_cached_response(monkeypatch):
    """The canonical happy path: original delivery persisted its
    response → retry returns the same payload tagged with
    ``deduplicated: True``."""
    fake_redis = _FakeRedis()

    async def _redis_client():
        return fake_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    body = b'{"notification_id": "opencti-42"}'

    # First call.
    _, _, key = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:abc"
    )

    # Persist the original response.
    await webhooks._persist_webhook_dedup_response(
        key,
        {
            "status": "ok",
            "triggered": [{"assignment": "Filigran PIR", "run_id": "run-1"}],
            "count": 1,
        },
    )

    # Second call (after persist): receives the cached payload + dedup flag.
    is_dup, cached, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:abc"
    )

    assert is_dup is True
    assert cached is not None
    assert cached["status"] == "ok"
    assert cached["deduplicated"] is True
    assert cached["triggered"][0]["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_claim_distinct_bodies_do_not_dedup(monkeypatch):
    """Two genuinely distinct events (different bodies) hitting the
    same endpoint MUST both fire — otherwise we'd silently drop real
    work whenever a payload happened to arrive within the TTL of an
    earlier delivery."""
    fake_redis = _FakeRedis()

    async def _redis_client():
        return fake_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    is_dup_1, _, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(),
        body=b'{"notification_id": "evt-1"}',
        scope="assignments:abc",
    )
    is_dup_2, _, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(),
        body=b'{"notification_id": "evt-2"}',
        scope="assignments:abc",
    )

    assert is_dup_1 is False
    assert is_dup_2 is False


@pytest.mark.asyncio
async def test_claim_distinct_scopes_do_not_dedup(monkeypatch):
    """The same body sent to two different webhook endpoints must
    create two independent dedup slots — otherwise a curl test
    against assignment A would suppress a later test against
    assignment B."""
    fake_redis = _FakeRedis()

    async def _redis_client():
        return fake_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    body = b'{"random": "stuff"}'

    is_dup_a, _, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:A"
    )
    is_dup_b, _, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:B"
    )

    assert is_dup_a is False
    assert is_dup_b is False


@pytest.mark.asyncio
async def test_claim_fails_open_on_redis_error(monkeypatch, caplog):
    """A Redis outage MUST NOT 5xx the webhook (the sender would retry
    and amplify the storm).  Fail open: log a WARNING and let the
    request through so production traffic keeps flowing even when
    Redis is unhealthy."""
    failing_redis = AsyncMock()
    failing_redis.set.side_effect = RuntimeError("Redis down")

    async def _redis_client():
        return failing_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    with caplog.at_level("WARNING", logger="app.api.v1.webhooks"):
        is_dup, cached, key = await webhooks._claim_webhook_dedup_slot(
            _make_request(),
            body=b'{"x": 1}',
            scope="assignments:abc",
        )

    assert is_dup is False
    assert cached is None
    assert key is None  # Signals "skip persist" to the caller
    assert any(
        "failing open" in record.message.lower() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_persist_is_no_op_when_redis_key_is_none(monkeypatch):
    """When ``_claim_…`` failed open (returns ``key=None``), the persist
    helper must safely no-op without ever touching Redis — otherwise a
    persistent outage would log a noisy traceback on every webhook."""
    redis_mock = AsyncMock()

    async def _redis_client():
        return redis_mock

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    await webhooks._persist_webhook_dedup_response(None, {"status": "ok"})

    redis_mock.set.assert_not_called()


@pytest.mark.asyncio
async def test_persist_serializes_response_to_json(monkeypatch):
    """Round-trip check: the value we hand to ``persist`` must be the
    JSON bytes a subsequent ``claim`` can decode and tag."""
    fake_redis = _FakeRedis()

    async def _redis_client():
        return fake_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    body = b'{"notification_id": "evt-x"}'
    _, _, key = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:zzz"
    )

    response = {"status": "ok", "triggered": [], "count": 0}
    await webhooks._persist_webhook_dedup_response(key, response)

    is_dup, cached, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:zzz"
    )
    assert is_dup is True
    assert cached == {**response, "deduplicated": True}


# ---------------------------------------------------------------------------
# _release_webhook_dedup_slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_deletes_pending_slot_so_retry_proceeds(monkeypatch):
    """The whole reason ``_release_webhook_dedup_slot`` exists: when an
    exception is raised AFTER claiming a slot but BEFORE persisting the
    response, the next delivery for the same key MUST be treated as a
    first arrival, not as a duplicate.  Otherwise a transient bug or
    misconfiguration that the operator just fixed would still be
    reported as ``deduplicated: True`` for the full TTL.
    """
    fake_redis = _FakeRedis()

    async def _redis_client():
        return fake_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    body = b'{"notification_id": "evt-flaky"}'

    # First call claims the slot.
    _, _, key = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:abc"
    )
    assert fake_redis.store[key] == webhooks._WEBHOOK_DEDUP_PENDING_MARKER

    # Simulate the handler raising before persisting → release.
    await webhooks._release_webhook_dedup_slot(key)
    assert key not in fake_redis.store

    # Retry: should be treated as the first arrival again.
    is_dup, _, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:abc"
    )
    assert is_dup is False


@pytest.mark.asyncio
async def test_release_is_no_op_when_redis_key_is_none(monkeypatch):
    """When ``_claim_…`` failed open (returns ``key=None``), the release
    helper must safely no-op without ever touching Redis."""
    redis_mock = AsyncMock()

    async def _redis_client():
        return redis_mock

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    await webhooks._release_webhook_dedup_slot(None)

    redis_mock.delete.assert_not_called()


@pytest.mark.asyncio
async def test_release_swallows_redis_errors(monkeypatch, caplog):
    """A Redis outage during release MUST NOT propagate to the caller —
    we are already in an exception-handling path; raising again would
    mask the original exception (signature mismatch, DB error, etc.)
    that the caller actually wants to surface."""
    failing_redis = AsyncMock()
    failing_redis.delete.side_effect = RuntimeError("Redis down")

    async def _redis_client():
        return failing_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    with caplog.at_level("DEBUG", logger="app.api.v1.webhooks"):
        await webhooks._release_webhook_dedup_slot("webhook:dedup:scope:1")

    failing_redis.delete.assert_awaited_once_with("webhook:dedup:scope:1")


# ---------------------------------------------------------------------------
# _finalize_dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_dedup_persists_and_returns_response(monkeypatch):
    """A non-trigger early return ("agent disabled", "no agents bound",
    event-filter skip) must be cached so retries replay the actual
    answer instead of the generic ``{"status": "duplicate"}`` reply.
    Otherwise an operator who just enabled the agent would still see
    "duplicate" for up to 5 min — masking that the fix worked."""
    fake_redis = _FakeRedis()

    async def _redis_client():
        return fake_redis

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    body = b'{"notification_id": "evt-ignored"}'

    # First call claims the slot (would normally be inside the handler).
    _, _, key = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:abc"
    )

    # Handler decides this is a no-op and finalises with an "ignored"
    # response.  ``_finalize_dedup`` must (a) persist the response and
    # (b) return it unchanged to the caller.
    ignored = {"status": "ignored", "reason": "agent not found or disabled"}
    returned = await webhooks._finalize_dedup(key, ignored)
    assert returned is ignored

    # Retry within the TTL must replay the actual ignored response with
    # the dedup tag — NOT the generic ``"duplicate"`` answer.
    is_dup, cached, _ = await webhooks._claim_webhook_dedup_slot(
        _make_request(), body=body, scope="assignments:abc"
    )
    assert is_dup is True
    assert cached == {**ignored, "deduplicated": True}


@pytest.mark.asyncio
async def test_finalize_dedup_is_safe_when_redis_key_is_none(monkeypatch):
    """When the original claim failed open (Redis was down), the handler
    still calls ``_finalize_dedup`` on the way out.  It must safely
    no-op on the persist (since there is no slot to persist into) and
    return the response unchanged."""
    redis_mock = AsyncMock()

    async def _redis_client():
        return redis_mock

    monkeypatch.setattr("app.core.redis.redis_client", _redis_client)

    response = {"status": "ok", "triggered": [], "count": 0}
    returned = await webhooks._finalize_dedup(None, response)

    assert returned is response
    redis_mock.set.assert_not_called()


# ---------------------------------------------------------------------------
# Configuration pins
# ---------------------------------------------------------------------------


def test_dedup_ttl_default_pinned_to_five_minutes():
    """Pin the default TTL so a regression that shortens it (and
    re-introduces the double-execution window) trips this suite.

    Five minutes is the documented balance: long enough to absorb
    every common proxy / sender retry policy, short enough that
    genuinely distinct events with identical payloads (rare in
    practice) aren't blocked for long."""
    assert webhooks.DEFAULT_WEBHOOK_DEDUP_TTL_SECONDS == 300
    assert settings.webhook_dedup_ttl_seconds == 300
    assert webhooks._webhook_dedup_ttl_seconds() == 300


def test_dedup_ttl_is_configurable(monkeypatch):
    """The window is a knob: a sender whose event cadence is slower
    than five minutes needs a longer window, one that legitimately repeats an
    identical body needs a shorter one. A 0/negative override must not disable
    dedup outright — it clamps to 1s."""
    monkeypatch.setattr(settings, "webhook_dedup_ttl_seconds", 900)
    assert webhooks._webhook_dedup_ttl_seconds() == 900

    monkeypatch.setattr(settings, "webhook_dedup_ttl_seconds", 0)
    assert webhooks._webhook_dedup_ttl_seconds() == 1


# ---------------------------------------------------------------------------
# _verify_assignment_webhook_secret (auth gate used before the dedup cache)
# ---------------------------------------------------------------------------


def test_verify_assignment_secret_noop_without_secret():
    """No configured secret → unauthenticated-by-design assignment: no raise."""
    webhooks._verify_assignment_webhook_secret(b'{"x":1}', _make_request(), None)
    webhooks._verify_assignment_webhook_secret(b'{"x":1}', _make_request(), "")


def test_verify_assignment_secret_valid_github_passes():
    body = b'{"action":"opened"}'
    secret = "s3cr3t"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = _make_request({"X-Hub-Signature-256": sig})
    # Must not raise for a valid signature.
    webhooks._verify_assignment_webhook_secret(body, request, secret)


def test_verify_assignment_secret_invalid_github_raises_401():
    request = _make_request({"X-Hub-Signature-256": "sha256=deadbeef"})
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(b'{"x":1}', request, "s3cr3t")
    assert ei.value.status_code == 401


def test_verify_assignment_secret_invalid_opencti_token_raises_401():
    request = _make_request({"X-OpenCTI-Token": "wrong-token"})
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(b"", request, "right-token")
    assert ei.value.status_code == 401


def test_verify_assignment_secret_valid_generic_hmac_passes():
    body = b"payload"
    secret = "abc"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = _make_request({"X-Signature": sig})
    webhooks._verify_assignment_webhook_secret(body, request, secret)


def test_verify_assignment_secret_missing_signature_raises_401():
    """The fail-closed contract: a configured secret with NO recognised
    signature/token header (an unsigned replay) must 401 — otherwise the
    delivery could reach the dedup cache without proving knowledge of the
    secret."""
    request = _make_request({})  # no X-OpenCTI-Token / GitHub / Slack / X-Signature
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(b'{"x":1}', request, "s3cr3t")
    assert ei.value.status_code == 401
    assert "missing" in ei.value.detail.lower()


def test_verify_assignment_secret_missing_signature_ignores_unrelated_headers():
    """Headers that are not signature/token headers must not satisfy the
    gate — an attacker setting arbitrary headers still gets a 401."""
    request = _make_request({"X-Forwarded-For": "1.2.3.4", "User-Agent": "evil"})
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_assignment_webhook_secret(b"{}", request, "s3cr3t")
    assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# _verify_signature (shared provider/integration verifier — fail-closed)
# ---------------------------------------------------------------------------


def test_verify_signature_empty_secret_is_noop():
    """An empty secret cannot verify anything; the helper must no-op rather
    than reject (the real callers already guard on a truthy secret, but the
    defensive guard keeps the contract obvious)."""
    webhooks._verify_signature(b"{}", "", _make_request({}))


def test_verify_signature_missing_signature_raises_401():
    """A configured secret with no recognised signature header → 401."""
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_signature(b'{"x":1}', "s3cr3t", _make_request({}))
    assert ei.value.status_code == 401
    assert "missing" in ei.value.detail.lower()


def test_verify_signature_valid_github_passes():
    body = b'{"action":"opened"}'
    secret = "s3cr3t"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    webhooks._verify_signature(body, secret, _make_request({"X-Hub-Signature-256": sig}))


def test_verify_signature_invalid_github_raises_401():
    request = _make_request({"X-Hub-Signature-256": "sha256=deadbeef"})
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_signature(b'{"x":1}', "s3cr3t", request)
    assert ei.value.status_code == 401


def test_verify_signature_valid_opencti_token_passes():
    """OpenCTI sends a static bearer token in X-OpenCTI-Token; a matching
    token must authenticate the request without a signature header."""
    request = _make_request({"X-OpenCTI-Token": "right-token"})
    webhooks._verify_signature(b"", "right-token", request)


def test_verify_signature_invalid_opencti_token_raises_401():
    request = _make_request({"X-OpenCTI-Token": "wrong-token"})
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_signature(b"", "right-token", request)
    assert ei.value.status_code == 401


def test_verify_signature_valid_generic_hmac_passes():
    body = b"payload"
    secret = "abc"
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    webhooks._verify_signature(body, secret, _make_request({"X-Signature": sig}))


# ---------------------------------------------------------------------------
# _verify_hubspot_signature (fail-closed + v3-without-timestamp bypass)
# ---------------------------------------------------------------------------


def test_verify_hubspot_signature_missing_header_raises_401():
    """A configured client_secret with no HubSpot signature header at all
    must 401 instead of silently passing."""
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_hubspot_signature(b"{}", "client-secret", _make_request({}))
    assert ei.value.status_code == 401
    assert "missing" in ei.value.detail.lower()


def test_verify_hubspot_signature_v3_without_timestamp_raises_401():
    """A v3 signature header without the required timestamp used to fall
    through to the (absent) v2/v1 branches and pass — pin that it now 401s."""
    request = _make_request({"X-HubSpot-Signature-v3": "whatever"})
    with pytest.raises(HTTPException) as ei:
        webhooks._verify_hubspot_signature(b"{}", "client-secret", request)
    assert ei.value.status_code == 401


# ---------------------------------------------------------------------------
# _try_verify_webhook_signature (decrypts extra_config → fail-closed)
# ---------------------------------------------------------------------------


def test_try_verify_noop_without_extra_config():
    """No encrypted config → unauthenticated-by-design integration: no raise."""
    webhooks._try_verify_webhook_signature(b"{}", None, _make_request({}))
    webhooks._try_verify_webhook_signature(b"{}", "", _make_request({}))


def test_try_verify_noop_when_no_secret_configured(monkeypatch):
    """An integration whose extra_config has no ``webhook_secret`` is
    unauthenticated by design — an unsigned delivery must NOT 401."""
    monkeypatch.setattr(webhooks, "decrypt_value", lambda _enc: '{"base_url": "x"}')
    webhooks._try_verify_webhook_signature(b"{}", "enc", _make_request({}))


def test_try_verify_missing_signature_raises_when_secret_configured(monkeypatch):
    """The hole the review flagged: an integration WITH a ``webhook_secret``
    configured but an unsigned inbound request must 401 (propagated out of
    the helper as an HTTPException, not swallowed)."""
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: '{"webhook_secret": "s3cr3t"}'
    )
    with pytest.raises(HTTPException) as ei:
        webhooks._try_verify_webhook_signature(b'{"x":1}', "enc", _make_request({}))
    assert ei.value.status_code == 401


def test_try_verify_valid_signature_passes(monkeypatch):
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: '{"webhook_secret": "s3cr3t"}'
    )
    body = b'{"action":"opened"}'
    sig = "sha256=" + hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    webhooks._try_verify_webhook_signature(
        body, "enc", _make_request({"X-Hub-Signature-256": sig})
    )


# ---------------------------------------------------------------------------
# _verify_linkedin_provider_signature (fail-closed for the LinkedIn provider)
#
# LinkedIn signs with the OAuth ``clientSecret`` (separate from the generic
# ``webhook_secret``) via ``X-LI-Signature``. The prior inline check only
# verified a *present* header, so an unsigned LinkedIn replay reached the
# dedup cache unauthenticated. These pin the fail-closed contract.
# ---------------------------------------------------------------------------


async def test_linkedin_provider_missing_signature_raises_401(monkeypatch):
    """LinkedIn provider + configured client secret + NO ``X-LI-Signature``
    must 401 instead of falling through to the dedup cache."""
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _enc: "client-secret")
    with pytest.raises(HTTPException) as ei:
        await webhooks._verify_linkedin_provider_signature(
            b"{}", "linkedin", "enc-secret", _make_request({})
        )
    assert ei.value.status_code == 401


async def test_linkedin_provider_invalid_signature_raises_401(monkeypatch):
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _enc: "client-secret")
    monkeypatch.setattr(webhooks, "verify_linkedin_signature", lambda *_a: False)
    with pytest.raises(HTTPException) as ei:
        await webhooks._verify_linkedin_provider_signature(
            b"{}", "linkedin", "enc-secret", _make_request({"X-LI-Signature": "bad"})
        )
    assert ei.value.status_code == 401


async def test_linkedin_provider_valid_signature_passes(monkeypatch):
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _enc: "client-secret")
    monkeypatch.setattr(webhooks, "verify_linkedin_signature", lambda *_a: True)
    await webhooks._verify_linkedin_provider_signature(
        b"{}", "linkedin", "enc-secret", _make_request({"X-LI-Signature": "good"})
    )


async def test_linkedin_provider_noop_without_client_secret():
    """No client secret configured → nothing to verify; an absent signature
    must NOT 401 (LinkedIn provider)."""
    await webhooks._verify_linkedin_provider_signature(
        b"{}", "linkedin", None, _make_request({})
    )


async def test_non_linkedin_provider_missing_signature_is_noop(monkeypatch):
    """A non-LinkedIn provider with a client secret but no ``X-LI-Signature``
    is NOT LinkedIn-authenticated — it's a no-op (the generic webhook_secret
    path handles those providers), so it must not 401."""
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _enc: "client-secret")
    await webhooks._verify_linkedin_provider_signature(
        b"{}", "github", "enc-secret", _make_request({})
    )


async def test_non_linkedin_provider_present_signature_still_verified(monkeypatch):
    """Defense in depth: a non-LinkedIn provider that nonetheless sends an
    ``X-LI-Signature`` with a client secret configured still has it verified."""
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _enc: "client-secret")
    monkeypatch.setattr(webhooks, "verify_linkedin_signature", lambda *_a: False)
    with pytest.raises(HTTPException) as ei:
        await webhooks._verify_linkedin_provider_signature(
            b"{}", "github", "enc-secret", _make_request({"X-LI-Signature": "bad"})
        )
    assert ei.value.status_code == 401


async def test_linkedin_provider_secret_read_from_a_vault_verifies(monkeypatch):
    """A client secret stored as a vault reference is resolved for the check."""
    owners: list[object] = []

    async def _vault(ciphertext, owner=None):
        owners.append(owner)
        return "client-secret"

    seen: list[str] = []
    monkeypatch.setattr(webhooks, "reveal_ciphertext", _vault)
    monkeypatch.setattr(webhooks, "verify_linkedin_signature", lambda _b, secret, _s: seen.append(secret) or True)
    await webhooks._verify_linkedin_provider_signature(
        b"{}", "linkedin", "enc-reference", _make_request({"X-LI-Signature": "good"}), "od-1"
    )
    assert seen == ["client-secret"]
    assert [(o.kind, o.id, o.field) for o in owners] == [("oauth_default", "od-1", "client_secret")]


async def test_linkedin_provider_secret_its_vault_does_not_give_refuses_the_delivery(monkeypatch):
    from app.services.vault import SecretUnavailableError

    async def _vault(ciphertext, owner=None):
        raise SecretUnavailableError("unreachable")

    monkeypatch.setattr(webhooks, "reveal_ciphertext", _vault)
    monkeypatch.setattr(webhooks, "verify_linkedin_signature", lambda *_a: True)
    with pytest.raises(HTTPException) as ei:
        await webhooks._verify_linkedin_provider_signature(
            b"{}", "linkedin", "enc-reference", _make_request({"X-LI-Signature": "good"}), "od-1"
        )
    assert ei.value.status_code == 503


def test_should_alert_direct_webhook_miss_threshold_and_realert():
    threshold = webhooks._DIRECT_WEBHOOK_MISS_ALERT_THRESHOLD
    realert_every = webhooks._DIRECT_WEBHOOK_MISS_REALERT_EVERY

    assert webhooks._should_alert_direct_webhook_miss(threshold - 1) is False
    assert webhooks._should_alert_direct_webhook_miss(threshold) is True
    assert (
        webhooks._should_alert_direct_webhook_miss(threshold + realert_every - 1)
        is False
    )
    assert (
        webhooks._should_alert_direct_webhook_miss(threshold + realert_every) is True
    )


# ---------------------------------------------------------------------------
# Auth-before-dedup: an invalid signature must be rejected BEFORE the
# idempotency cache is consulted (otherwise a replay could read a cached
# response / plant a pending marker without authentication).
# ---------------------------------------------------------------------------


class _RequestWithBody(SimpleNamespace):
    async def body(self):
        return self._body


def _make_request_with_body(headers: dict, body: bytes) -> _RequestWithBody:
    return _RequestWithBody(headers=headers, _body=body, method="POST", url="http://t")


def _none_assignment_session_ctx(monkeypatch, statements: list | None = None):
    class _Result:
        def scalar_one_or_none(self):
            return None

    class _DB:
        async def execute(self, stmt, *args, **kwargs):
            if statements is not None:
                statements.append(stmt)
            return _Result()

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())


def _forbid_dedup_claim(monkeypatch):
    """Pin that the miss path never claims the idempotency slot.

    The miss is recorded BEFORE the dedup layer on purpose: if a stale
    sender retries an identical payload (same dedup key within the TTL),
    a dedup early-return would stop those deliveries from counting toward
    the drift escalation."""

    async def _must_not_claim(*args, **kwargs):
        raise AssertionError(
            "dedup slot claimed on the miss path — duplicate deliveries "
            "would stop counting toward drift escalation"
        )

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _must_not_claim)


@pytest.mark.asyncio
async def test_direct_assignment_miss_logs_warning_once_with_source_details(
    monkeypatch, caplog
):
    assignment_id = str(uuid.uuid4())
    _none_assignment_session_ctx(monkeypatch)
    # Behind a trusted reverse proxy the leftmost XFF value is the real
    # client — the deployment opts in exactly like the rate limiter does.
    monkeypatch.setattr(webhooks.settings, "trust_proxy_headers", True)

    _forbid_dedup_claim(monkeypatch)
    monkeypatch.setattr(webhooks, "_record_direct_webhook_miss", AsyncMock(return_value=1))

    # Case-insensitive like a real request's headers.
    request = _make_request_with_body(
        Headers({"User-Agent": "pytest-agent", "X-Forwarded-For": "203.0.113.10"}),
        b'{"foo":"bar"}',
    )
    request.client = SimpleNamespace(host="10.0.0.1")

    with caplog.at_level("WARNING", logger="app.api.v1.webhooks"):
        response = await webhooks.receive_direct_assignment_webhook(
            assignment_id, request
        )

    assert response["status"] == "ignored"
    assert response["reason"] == "assignment not found, not a webhook trigger, or disabled"
    assert any(
        "Direct webhook: assignment" in rec.getMessage()
        and "source_ip=203.0.113.10" in rec.getMessage()
        and "user_agent=pytest-agent" in rec.getMessage()
        for rec in caplog.records
    )


def test_webhook_request_source_ignores_xff_by_default(monkeypatch):
    """This endpoint is unauthenticated: a spoofed ``X-Forwarded-For`` must
    NOT be able to plant a fake source IP in the drift diagnostics unless
    the deployment explicitly trusts its proxy headers (same policy as
    ``rate_limit._client_ip``)."""
    monkeypatch.setattr(webhooks.settings, "trust_proxy_headers", False)
    request = SimpleNamespace(
        # Case-insensitive like a real request's headers: a plain dict keyed
        # "X-Forwarded-For" is never read at all, so it passed ungated too.
        headers=Headers({"X-Forwarded-For": "6.6.6.6", "User-Agent": "spoofer"}),
        client=SimpleNamespace(host="192.0.2.7"),
    )

    source_ip, user_agent = webhooks._webhook_request_source(request)

    assert source_ip == "192.0.2.7"
    assert user_agent == "spoofer"


@pytest.mark.asyncio
async def test_direct_assignment_miss_emits_actionable_error_at_threshold(
    monkeypatch, caplog
):
    assignment_id = str(uuid.uuid4())
    _none_assignment_session_ctx(monkeypatch)

    _forbid_dedup_claim(monkeypatch)
    monkeypatch.setattr(
        webhooks,
        "_record_direct_webhook_miss",
        AsyncMock(return_value=webhooks._DIRECT_WEBHOOK_MISS_ALERT_THRESHOLD),
    )
    monkeypatch.setattr(
        webhooks,
        "_check_and_set_drift_alert_dedup",
        AsyncMock(return_value=True),
    )

    request = _make_request_with_body({"User-Agent": "pytest-agent"}, b'{"foo":"bar"}')
    request.client = SimpleNamespace(host="198.51.100.5")

    with caplog.at_level("ERROR", logger="app.api.v1.webhooks"):
        response = await webhooks.receive_direct_assignment_webhook(
            assignment_id, request
        )

    assert response["status"] == "ignored"
    assert response["reason"] == "assignment not found, not a webhook trigger, or disabled"
    assert any(
        "Direct webhook drift detected" in rec.getMessage()
        and str(webhooks._DIRECT_WEBHOOK_MISS_ALERT_THRESHOLD) in rec.getMessage()
        and "source_ip=198.51.100.5" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_drift_alert_downgraded_to_warning_when_dedup_key_exists(
    monkeypatch, caplog
):
    """When a drift ERROR was already emitted within the 24-hour cross-window
    dedup TTL, the repeated alert must be downgraded to WARNING so the same
    persistent condition does not drain the error budget on every fresh 1-hour
    counter window."""
    assignment_id = str(uuid.uuid4())
    _none_assignment_session_ctx(monkeypatch)

    _forbid_dedup_claim(monkeypatch)
    monkeypatch.setattr(
        webhooks,
        "_record_direct_webhook_miss",
        AsyncMock(return_value=webhooks._DIRECT_WEBHOOK_MISS_ALERT_THRESHOLD),
    )
    # Dedup key exists — not the first alert in the 24-hour window.
    monkeypatch.setattr(
        webhooks,
        "_check_and_set_drift_alert_dedup",
        AsyncMock(return_value=False),
    )

    request = _make_request_with_body({"User-Agent": "pytest-agent"}, b'{"foo":"bar"}')
    request.client = SimpleNamespace(host="198.51.100.5")

    with caplog.at_level("WARNING", logger="app.api.v1.webhooks"):
        response = await webhooks.receive_direct_assignment_webhook(
            assignment_id, request
        )

    assert response["status"] == "ignored"
    # Must NOT emit an ERROR — only a WARNING.
    error_records = [
        r for r in caplog.records if r.levelname == "ERROR" and "drift" in r.getMessage().lower()
    ]
    assert not error_records, f"Unexpected ERROR log(s): {[r.getMessage() for r in error_records]}"
    # Must emit a WARNING that mentions "recurring".
    warning_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "drift recurring" in r.getMessage()
    ]
    assert warning_records, "Expected 'drift recurring' WARNING log"
    msg = warning_records[0].getMessage()
    assert assignment_id in msg
    assert "source_ip=198.51.100.5" in msg


@pytest.mark.asyncio
async def test_drift_alert_dedup_fails_open_on_redis_error(monkeypatch):
    """When Redis is unavailable, ``_check_and_set_drift_alert_dedup`` returns
    ``True`` (fail-open) so the ERROR path is always taken — visibility is
    never silently suppressed due to a cache outage."""
    class _FakeRedis:
        async def set(self, *_a, **_kw):
            raise ConnectionError("redis down")

    async def _fake_redis_client():
        return _FakeRedis()

    monkeypatch.setattr("app.core.redis.redis_client", _fake_redis_client)

    result = await webhooks._check_and_set_drift_alert_dedup(str(uuid.uuid4()))
    assert result is True, "fail-open: Redis error must return True so ERROR is still emitted"


@pytest.mark.asyncio
async def test_drift_alert_dedup_set_nx_semantics(monkeypatch):
    """Exercise the helper against a fake Redis (not a mock of the helper):
    the first claim must issue ``SET key 1 NX EX 86400`` and return ``True``;
    a second claim within the window (SET NX returns ``None``) must return
    ``False``."""
    assignment_id = str(uuid.uuid4())
    calls: list[tuple[tuple, dict]] = []

    class _FakeRedis:
        def __init__(self):
            self.store: set[str] = set()

        async def set(self, key, value, **kwargs):
            calls.append(((key, value), kwargs))
            if kwargs.get("nx") and key in self.store:
                return None
            self.store.add(key)
            return True

    fake = _FakeRedis()

    async def _fake_redis_client():
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _fake_redis_client)

    first = await webhooks._check_and_set_drift_alert_dedup(assignment_id)
    second = await webhooks._check_and_set_drift_alert_dedup(assignment_id)

    assert first is True
    assert second is False
    expected_key = f"{webhooks._DIRECT_WEBHOOK_DRIFT_ALERTED_PREFIX}{assignment_id}"
    assert calls[0][0] == (expected_key, "1")
    assert calls[0][1] == {
        "ex": webhooks._DIRECT_WEBHOOK_DRIFT_ALERTED_SECONDS,
        "nx": True,
    }


@pytest.mark.asyncio
async def test_clear_drift_state_deletes_counter_and_dedup_keys(monkeypatch):
    """``_clear_direct_webhook_drift_state`` must delete BOTH the hourly miss
    counter and the 24-hour alert dedup key so a remediated incident leaves no
    residue — a NEW drift afterwards restarts the escalation from scratch and
    its first alert fires at ERROR again."""
    assignment_id = str(uuid.uuid4())
    deleted: list[tuple[str, ...]] = []

    class _FakeRedis:
        async def delete(self, *keys):
            deleted.append(keys)
            return len(keys)

    async def _fake_redis_client():
        return _FakeRedis()

    monkeypatch.setattr("app.core.redis.redis_client", _fake_redis_client)

    await webhooks._clear_direct_webhook_drift_state(assignment_id)

    assert deleted == [
        (
            f"{webhooks._DIRECT_WEBHOOK_MISS_KEY_PREFIX}{assignment_id}",
            f"{webhooks._DIRECT_WEBHOOK_DRIFT_ALERTED_PREFIX}{assignment_id}",
        )
    ]


@pytest.mark.asyncio
async def test_clear_drift_state_swallows_redis_errors(monkeypatch):
    """A Redis blip while clearing the drift state must never break the
    delivery path — stale keys simply expire via their TTLs instead."""
    class _FakeRedis:
        async def delete(self, *_keys):
            raise ConnectionError("redis down")

    async def _fake_redis_client():
        return _FakeRedis()

    monkeypatch.setattr("app.core.redis.redis_client", _fake_redis_client)

    # Must not raise.
    await webhooks._clear_direct_webhook_drift_state(str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_resolved_assignment_clears_drift_state(monkeypatch):
    """As soon as the pre-claim lookup resolves the assignment again, the
    drift state must be cleared — even when the delivery is subsequently
    rejected (here: 401 for a missing signature).  The drift alert tracks the
    TARGET being missing, not sender authentication, so target resolution
    alone proves the condition is remediated."""
    assignment_id = str(uuid.uuid4())
    wf = SimpleNamespace(
        id=assignment_id,
        trigger_type="webhook",
        enabled=True,
        trigger_config={"webhook_secret": "s3cr3t"},
        agent_id=uuid.uuid4(),
        name="WF",
        user_id=None,
        is_company_managed=False,
    )

    class _Result:
        def scalar_one_or_none(self):
            return wf

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Result()

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())

    clear_mock = AsyncMock()
    monkeypatch.setattr(webhooks, "_clear_direct_webhook_drift_state", clear_mock)
    miss_mock = AsyncMock()
    monkeypatch.setattr(webhooks, "_handle_direct_webhook_miss", miss_mock)

    # No signature header -> rejected after the clear.
    request = _make_request_with_body({}, b'{"action":"opened"}')

    with pytest.raises(HTTPException) as ei:
        await webhooks.receive_direct_assignment_webhook(assignment_id, request)

    assert ei.value.status_code == 401
    clear_mock.assert_awaited_once_with(assignment_id)
    miss_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_assignment_miss_suppresses_between_first_and_threshold(
    monkeypatch, caplog
):
    """Miss counts strictly between the first hit and the alert threshold are
    the whole point of the change: they must produce NO warning and NO error
    (the per-request spam the issue reported).  Also pins the counter key
    canonicalization — an uppercase assignment ID must be counted under its
    canonical lowercase UUID form so a sender cannot fragment the counter by
    rotating the formatting of the same ID."""
    canonical_id = str(uuid.uuid4())
    statements: list = []
    _none_assignment_session_ctx(monkeypatch, statements)

    _forbid_dedup_claim(monkeypatch)
    record_mock = AsyncMock(return_value=webhooks._DIRECT_WEBHOOK_MISS_ALERT_THRESHOLD - 1)
    monkeypatch.setattr(webhooks, "_record_direct_webhook_miss", record_mock)

    request = _make_request_with_body({"User-Agent": "pytest-agent"}, b'{"foo":"bar"}')
    request.client = SimpleNamespace(host="198.51.100.5")

    with caplog.at_level("WARNING", logger="app.api.v1.webhooks"):
        response = await webhooks.receive_direct_assignment_webhook(
            canonical_id.upper(), request
        )

    assert response["status"] == "ignored"
    assert not any(
        "Direct webhook" in rec.getMessage() for rec in caplog.records
    )
    record_mock.assert_awaited_once_with(canonical_id)

    # The DB lookup must bind the PARSED uuid.UUID (never the raw path
    # string) against the typed ``Assignment.id`` column, so formatting
    # variants of the same ID behave identically without relying on
    # implicit driver coercion.
    assert len(statements) == 1
    bound = list(statements[0].compile().params.values())
    assert uuid.UUID(canonical_id) in bound
    assert canonical_id.upper() not in bound


@pytest.mark.asyncio
async def test_direct_assignment_miss_toctou_fallback_still_counts(monkeypatch):
    """When the assignment exists at the pre-claim lookup but disappears
    before the main lookup (deleted/disabled in between), the inner miss
    fallback must still record the miss (exactly once) and persist the
    response into the already-claimed dedup slot."""
    assignment_id = str(uuid.uuid4())
    wf_pre = SimpleNamespace(
        id=assignment_id,
        trigger_type="webhook",
        enabled=True,
        trigger_config={},
        agent_id=uuid.uuid4(),
        name="WF",
        user_id=None,
        is_company_managed=False,
    )
    # No secret configured -> the pre-claim verification must not reject.
    monkeypatch.setattr(webhooks.settings, "webhook_require_secret", False)

    results = [wf_pre, None]

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Result(results.pop(0))

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())

    async def _claim(*args, **kwargs):
        return False, None, "dedup-key"

    finalized: list[tuple[str | None, dict]] = []

    async def _finalize(redis_key, response):
        finalized.append((redis_key, response))
        return response

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _claim)
    monkeypatch.setattr(webhooks, "_finalize_dedup", _finalize)
    record_mock = AsyncMock(return_value=2)
    monkeypatch.setattr(webhooks, "_record_direct_webhook_miss", record_mock)

    request = _make_request_with_body({"User-Agent": "pytest-agent"}, b'{"foo":"bar"}')
    request.client = SimpleNamespace(host="198.51.100.5")

    response = await webhooks.receive_direct_assignment_webhook(
        assignment_id, request
    )

    assert response["status"] == "ignored"
    assert response["reason"] == "assignment not found, not a webhook trigger, or disabled"
    record_mock.assert_awaited_once_with(assignment_id)
    assert finalized == [("dedup-key", response)]


@pytest.mark.asyncio
async def test_direct_assignment_webhook_rejects_invalid_sig_before_dedup(monkeypatch):
    """A delivery with an invalid signature must 401 *before*
    ``_claim_webhook_dedup_slot`` is ever called — proving the dedup cache
    is bound to an authenticated request and cannot be read by an
    unauthenticated replay."""
    assignment_id = str(uuid.uuid4())
    wf = SimpleNamespace(
        id=assignment_id,
        trigger_type="webhook",
        enabled=True,
        trigger_config={"webhook_secret": "s3cr3t"},
        agent_id=uuid.uuid4(),
        name="WF",
        user_id=None,
        is_company_managed=False,
    )

    class _Result:
        def scalar_one_or_none(self):
            return wf

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Result()

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())

    async def _must_not_claim(*args, **kwargs):
        raise AssertionError(
            "dedup slot claimed before signature verification — auth bypass!"
        )

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _must_not_claim)

    request = _make_request_with_body(
        {"X-Hub-Signature-256": "sha256=deadbeef"}, b'{"action":"opened"}'
    )

    with pytest.raises(HTTPException) as ei:
        await webhooks.receive_direct_assignment_webhook(assignment_id, request)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_direct_assignment_webhook_rejects_missing_sig_before_dedup(monkeypatch):
    """An *unsigned* delivery (no signature header at all) to an assignment
    that configures a ``webhook_secret`` must also 401 before the dedup slot
    is claimed — the fail-closed half of the auth-before-dedup contract."""
    assignment_id = str(uuid.uuid4())
    wf = SimpleNamespace(
        id=assignment_id,
        trigger_type="webhook",
        enabled=True,
        trigger_config={"webhook_secret": "s3cr3t"},
        agent_id=uuid.uuid4(),
        name="WF",
        user_id=None,
        is_company_managed=False,
    )

    class _Result:
        def scalar_one_or_none(self):
            return wf

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Result()

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())

    async def _must_not_claim(*args, **kwargs):
        raise AssertionError(
            "dedup slot claimed before signature verification — auth bypass!"
        )

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _must_not_claim)

    # No signature header → the unsigned replay must be rejected.
    request = _make_request_with_body({}, b'{"action":"opened"}')

    with pytest.raises(HTTPException) as ei:
        await webhooks.receive_direct_assignment_webhook(assignment_id, request)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_provider_webhook_rejects_missing_sig_before_dedup(monkeypatch):
    """The provider handler authenticates via the shared
    ``_try_verify_webhook_signature`` → ``_verify_signature`` path.  When the
    pre-provisioned OAuthDefault carries a ``webhook_secret`` an unsigned
    delivery must 401 before the dedup slot is claimed.

    The signature secrets now come from the provider routing cache,
    whose loader issues an OAuthDefault query (``scalar_one_or_none``) and an
    agent-ids query (``all``) — the fake result serves both shapes.
    """
    od = SimpleNamespace(id="od-github", extra_config_enc="enc", client_secret_enc=None)

    class _Result:
        def scalar_one_or_none(self):
            return od

        def all(self):
            return []  # no agents bound — loader stops here

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Result()

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    # The routing cache is per-process state — start from a clean slate so a
    # snapshot cached by another test can't serve this provider.
    webhooks._invalidate_provider_routing_cache()
    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: '{"webhook_secret": "s3cr3t"}'
    )

    async def _must_not_claim(*args, **kwargs):
        raise AssertionError(
            "dedup slot claimed before signature verification — auth bypass!"
        )

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _must_not_claim)

    # Valid JSON body that is NOT a Slack url_verification challenge, no sig.
    request = _make_request_with_body({}, b'{"action":"opened"}')

    with pytest.raises(HTTPException) as ei:
        await webhooks.receive_provider_webhook("github", request)
    assert ei.value.status_code == 401


def _hubspot_session_ctx(monkeypatch):
    """Patch ``async_session`` to resolve a HubSpot integration with a
    configured ``client_secret``, and stub ``decrypt_value`` accordingly."""
    integ = SimpleNamespace(provider="hubspot", extra_config_enc="enc")

    class _Result:
        def scalar_one_or_none(self):
            return integ

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Result()

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())
    monkeypatch.setattr(
        webhooks, "decrypt_value", lambda _enc: '{"client_secret": "hs-secret"}'
    )

    async def _must_not_claim(*args, **kwargs):
        raise AssertionError(
            "dedup slot claimed before HubSpot signature verification — "
            "auth bypass!"
        )

    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _must_not_claim)


@pytest.mark.asyncio
async def test_integration_webhook_hubspot_nonlist_rejected_before_dedup(monkeypatch):
    """A HubSpot integration with a configured signing secret must verify the
    HubSpot signature regardless of payload shape. A non-list (crafted) body
    with no valid signature used to fall through to the ``webhook_secret``-only
    path and reach the dedup cache unauthenticated — it must now 401 before the
    slot is claimed."""
    _hubspot_session_ctx(monkeypatch)
    integration_id = str(uuid.uuid4())
    # Non-list HubSpot body, no X-HubSpot-Signature* header.
    request = _make_request_with_body({}, b'{"crafted": "not-a-list"}')

    with pytest.raises(HTTPException) as ei:
        await webhooks.receive_integration_webhook(integration_id, request)
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_assignment_webhook_hubspot_nonlist_rejected_before_dedup(monkeypatch):
    """Same HubSpot shape-gating gap on the per-assignment integration handler:
    a non-list HubSpot body with no valid signature must 401 before the dedup
    slot is claimed."""
    _hubspot_session_ctx(monkeypatch)
    integration_id = str(uuid.uuid4())
    assignment_id = str(uuid.uuid4())
    request = _make_request_with_body({}, b'{"crafted": "not-a-list"}')

    with pytest.raises(HTTPException) as ei:
        await webhooks.receive_assignment_webhook(
            integration_id, assignment_id, request
        )
    assert ei.value.status_code == 401
