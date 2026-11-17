from __future__ import annotations

import asyncio

from app.api.v1 import hooks
from app.services.event_bus import Event, event_bus


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _AsyncClient:
    def __init__(self, *, status_code: int, calls: list[int], **kwargs):
        self._status_code = status_code
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        self._calls.append(self._status_code)
        return _Response(self._status_code)


async def _emit(event_name: str, count: int) -> None:
    for _ in range(count):
        await event_bus.emit(event_name, {"x": 1}, wait=True)


def _reset_hook_state(hook_id: str, hook: dict) -> None:
    hooks._hooks.pop(hook_id, None)
    hooks._unregister_hook_listeners(hook_id, hook)


async def test_hook_disables_after_consecutive_not_found(monkeypatch):
    event_name = "test.hook.not_found"
    hook_id = "hook-404"
    hook = {"url": "http://internal/api/hooks/assignments/a", "events": [event_name]}
    calls: list[int] = []

    monkeypatch.setattr(
        hooks.httpx,
        "AsyncClient",
        lambda **kwargs: _AsyncClient(status_code=404, calls=calls, **kwargs),
    )

    hooks._hooks[hook_id] = hook
    hooks._register_hook_listeners(hook_id, hook)
    try:
        for _ in range(hooks._HOOK_DISABLE_NOT_FOUND_THRESHOLD):
            hook["next_attempt_at"] = 0
            await event_bus.emit(event_name, {"x": 1}, wait=True)
        assert hook_id not in hooks._hooks
        assert hook_id not in hooks._hook_listener_fns
        assert len(calls) == hooks._HOOK_DISABLE_NOT_FOUND_THRESHOLD
    finally:
        _reset_hook_state(hook_id, hook)


async def test_hook_backoff_skips_immediate_retry(monkeypatch):
    event_name = "test.hook.backoff"
    hook_id = "hook-backoff"
    hook = {"url": "http://example.invalid/hook", "events": [event_name]}
    calls: list[int] = []

    monkeypatch.setattr(
        hooks.httpx,
        "AsyncClient",
        lambda **kwargs: _AsyncClient(status_code=500, calls=calls, **kwargs),
    )

    hooks._hooks[hook_id] = hook
    hooks._register_hook_listeners(hook_id, hook)
    try:
        await _emit(event_name, 2)
        assert len(calls) == 1
        assert hook.get("consecutive_failures") == 1
        assert "next_attempt_at" in hook
    finally:
        _reset_hook_state(hook_id, hook)


async def test_hook_serializes_concurrent_dispatch(monkeypatch):
    """Concurrent events for the same hook must not bypass the backoff gate.

    ``event_bus.emit(wait=False)`` runs each listener as its own task, so
    without per-hook serialization several dispatches would read
    ``next_attempt_at`` before any failure is recorded and all fire at once — a
    storm. With the per-hook lock, only the first delivery goes out; the rest
    queue behind it and skip on the now-updated backoff gate.
    """
    event_name = "test.hook.concurrent"
    hook_id = "hook-concurrent"
    hook = {"url": "http://example.invalid/hook", "events": [event_name]}
    calls: list[int] = []

    class _SlowAsyncClient(_AsyncClient):
        async def post(self, *args, **kwargs):
            # Yield so the other gathered dispatch tasks reach the lock while
            # this delivery is "in flight" — exposes any gate-bypass regression.
            await asyncio.sleep(0.02)
            return await super().post(*args, **kwargs)

    monkeypatch.setattr(
        hooks.httpx,
        "AsyncClient",
        lambda **kwargs: _SlowAsyncClient(status_code=500, calls=calls, **kwargs),
    )

    hooks._hooks[hook_id] = hook
    hooks._register_hook_listeners(hook_id, hook)
    try:
        dispatch = hooks._hook_listener_fns[hook_id][0][1]
        event = Event(name=event_name, data={"x": 1})
        await asyncio.gather(*(dispatch(event) for _ in range(5)))
        assert len(calls) == 1
        assert hook.get("consecutive_failures") == 1
    finally:
        _reset_hook_state(hook_id, hook)
