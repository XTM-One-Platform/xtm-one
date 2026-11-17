"""Bounded SSE stream lifetime for /api/v1/activity/stream.

An unbounded SSE response never completes on its own, which held uvicorn's
graceful drain open forever during MAX_REQUESTS worker recycling (the
recycling worker stopped accepting connections but waited on open dashboard
streams indefinitely, eventually failing the pod's liveness probes) and let a
stream authenticated once at connect time outlive its JWT. The endpoint must
end each response cleanly after ``SSE_MAX_LIFETIME_SECONDS`` — EventSource
clients reconnect transparently.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import activity as activity_module


def test_sse_lifetime_is_finite_and_sane():
    # The whole point of the bound is that a stream always ends on its own,
    # so a recycling worker's drain (bounded separately by GRACEFUL_TIMEOUT)
    # never waits on an unbounded response. The 1h ceiling here is a sanity
    # cap against future edits, not a relationship to GRACEFUL_TIMEOUT; the
    # shipped value is 15 minutes.
    assert 0 < activity_module.SSE_MAX_LIFETIME_SECONDS <= 60 * 60


@pytest.mark.asyncio
async def test_activity_stream_ends_after_max_lifetime(
    monkeypatch: pytest.MonkeyPatch,
):
    """With the lifetime bound at 0 the generator must end on its first
    iteration (before emitting anything) and still run the pub/sub cleanup."""
    user_id = uuid.uuid4()
    monkeypatch.setattr(activity_module, "SSE_MAX_LIFETIME_SECONDS", 0)
    monkeypatch.setattr(
        "app.api.deps.decode_access_token", lambda _t: {"sub": str(user_id)}
    )
    monkeypatch.setattr(
        activity_module, "release_db_connection_before_loop", AsyncMock()
    )

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.connection.disconnect = AsyncMock()
    pubsub.connection_pool.release = AsyncMock()
    pubsub.get_message = AsyncMock(return_value=None)
    redis = MagicMock()
    redis.pubsub = MagicMock(return_value=pubsub)
    redis.aclose = AsyncMock()
    monkeypatch.setattr(
        activity_module, "redis_pubsub_client", AsyncMock(return_value=redis)
    )

    db = AsyncMock()
    db_result = MagicMock()
    # ``is_active``: the stream refuses a deactivated account, so the stand-in
    # has to state it the way a real User row would.
    db_result.scalar_one_or_none.return_value = SimpleNamespace(
        id=user_id, is_active=True, password_changed_at=None
    )
    db.execute = AsyncMock(return_value=db_result)

    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=False)

    response = await activity_module.activity_stream(request, token="tok", db=db)
    chunks = [chunk async for chunk in response.body_iterator]

    # Expired immediately: no events/heartbeats were emitted, the response
    # completed on its own, and the dedicated pub/sub client was cleaned up.
    assert chunks == []
    pubsub.connection_pool.release.assert_awaited_once()
    redis.aclose.assert_awaited_once()
