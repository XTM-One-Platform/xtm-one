# SPDX-License-Identifier: Apache-2.0

"""The web side of the CTEM storm recovery (#2570).

* a signal-source delivery is written to the durable inbox and answered at once,
  never recorded on the request;
* the inbox drain records the rows oldest first, deletes them, retries a failing
  one and drops it after ``MAX_ATTEMPTS``;
* a CTEM case event whose case is gone is recognised, so its run is skipped at start;
* the interactive requests keep a reserve of slots of the concurrency limiter;
* a large stored view is decoded off the web event loop.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest

from app.main import ConcurrencyLimitMiddleware
from app.services import assignment_engine as ae
from app.services import ctem_cases_view as view_of
from app.services import ctem_signal_inbox as inbox


def test_the_signal_source_webhook_queues_instead_of_recording() -> None:
    import inspect

    from app.api.v1 import webhooks

    source = inspect.getsource(webhooks.receive_signal_source_webhook)
    assert "ctem_signal_inbox.accept" in source
    # OpenCTI never retries a webhook: nothing on this path records inline, nothing answers 429/503.
    assert "ctem_signal_listeners.note_event" not in source.split('"""', 2)[2]
    assert "status_code=429" not in source and "status_code=503" not in source


@pytest.mark.asyncio
async def test_record_hands_a_delivery_to_its_recorders(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def listeners(provider, event_key, payload, *, verified):
        calls.append(f"signal:{provider}")
        return True

    async def mark(provider, payload, *, verified):
        calls.append("mark")
        return []

    async def aev_note(event_key, payload, *, verified):
        calls.append("aev")
        return 0

    async def stamp(event_key, payload, *, integration_id, verified):
        calls.append("stamp")

    from app.services import ctem_analyst_mark, ctem_openaev_events, ctem_signal_listeners

    monkeypatch.setattr(ctem_signal_listeners, "note_event", listeners)
    monkeypatch.setattr(ctem_analyst_mark, "note_event", mark)
    monkeypatch.setattr(ctem_openaev_events, "note_event", aev_note)
    monkeypatch.setattr(ctem_openaev_events, "stamp_reachability_assertion", stamp)

    assert await inbox.record("opencti", "i", "create", {}, verified=True) is True
    assert calls == ["signal:opencti", "mark"]
    calls.clear()
    await inbox.record("openaev", "i", "expectation", {}, verified=True)
    assert calls == ["stamp", "signal:openaev", "aev"]


class _Row:
    def __init__(self, provider: str, attempts: int = 0) -> None:
        self.id = uuid.uuid4()
        self.provider = provider
        self.integration_id = "i"
        self.event_key = "create"
        self.payload: dict[str, Any] = {}
        self.verified = True
        self.attempts = attempts


@pytest.mark.asyncio
async def test_drain_records_deletes_retries_and_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    ok, failing, last = _Row("opencti"), _Row("bad"), _Row("bad", attempts=inbox.MAX_ATTEMPTS - 1)
    deleted: list[Any] = []

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [ok, failing, last]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, _stmt):
            return _Result()

        async def commit(self):
            return None

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session())

    async def record(provider, *_args, **_kwargs):
        if provider == "bad":
            raise RuntimeError("recorder down")
        return True

    async def delete(row_id):
        deleted.append(row_id)

    monkeypatch.setattr(inbox, "record", record)
    monkeypatch.setattr(inbox, "_delete", delete)

    counts = await inbox.drain()
    assert counts == {"recorded": 1, "retried": 1, "dropped": 1}
    assert deleted == [ok.id, last.id]  # the retried one stays for the next tick


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "context",
    [
        None,
        {"type": "webhook"},
        {"type": "platform_event", "object_type": "work_item", "object_id": str(uuid.uuid4())},
        {"type": "platform_event", "object_type": "ctem_case", "object_id": "not-a-uuid"},
    ],
)
async def test_only_a_ctem_case_event_is_checked_for_its_case(context: Any) -> None:
    class _NoDb:
        async def execute(self, _stmt):  # pragma: no cover - must not be reached
            raise AssertionError("no read for a delivery that is not about a CTEM case")

    assert await ae._ctem_case_gone(_NoDb(), context) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("found", "gone"), [(None, True), (uuid.uuid4(), False)])
async def test_a_ctem_case_event_of_a_deleted_case_is_gone(found: Any, gone: bool) -> None:
    class _Result:
        def scalar_one_or_none(self):
            return found

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    context = {"type": "platform_event", "object_type": "ctem_case", "object_id": str(uuid.uuid4())}
    assert await ae._ctem_case_gone(_Db(), context) is gone


def test_the_purged_case_skip_runs_before_any_model_call() -> None:
    import inspect

    source = inspect.getsource(ae.execute_assignment_run)
    assert source.index("_ctem_case_gone") < source.index("May agentic AI run here at all")


async def _call(mw: ConcurrencyLimitMiddleware, path: str, method: str = "GET") -> int:
    status: dict[str, int] = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    await mw({"type": "http", "path": path, "method": method, "headers": []}, receive, send)
    return status["code"]


@pytest.mark.asyncio
async def test_interactive_requests_keep_a_reserve_of_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main

    monkeypatch.setattr(main._startup_done, "is_set", lambda: True)
    release = asyncio.Event()

    async def app(scope, receive, send):
        if scope["path"].startswith("/api/v1/agents"):
            await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = ConcurrencyLimitMiddleware(app, limit=10)
    assert mw._reserve == 1
    held = [asyncio.create_task(_call(mw, "/api/v1/agents")) for _ in range(9)]
    for _ in range(5):
        await asyncio.sleep(0)
    # The ordinary slots are taken: a tenth ordinary request is shed ...
    assert await _call(mw, "/api/v1/agents/x") == 429
    # ... while sign-in, the settings and the case-opening switch still answer.
    assert await _call(mw, "/api/v1/auth/me") == 200
    assert await _call(mw, "/api/v1/ctem-cases/opening", "PUT") == 200
    assert await _call(mw, "/api/v1/assignments/abc", "PATCH") == 200
    release.set()
    assert set(await asyncio.gather(*held)) == {200}


def test_a_small_limit_keeps_no_reserve() -> None:
    assert ConcurrencyLimitMiddleware(None, limit=5)._reserve == 0
    assert ConcurrencyLimitMiddleware(None, limit=200)._reserve == 20


@pytest.mark.asyncio
async def test_a_large_view_is_decoded_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    view_of._decoded.clear()
    loop_thread = threading.get_ident()
    seen: list[int] = []
    original = view_of.ViewerView.from_json

    def from_json(data):
        seen.append(threading.get_ident())
        return original(data)

    monkeypatch.setattr(view_of.ViewerView, "from_json", staticmethod(from_json))
    big = view_of.ViewerView(missing=[f"p{n}" for n in range(20_000)]).to_json()
    assert len(big) >= view_of.OFF_LOOP_DECODE_BYTES
    decoded = await view_of._decode_view_off_loop(big)
    assert decoded.missing == [f"p{n}" for n in range(20_000)]
    assert seen and seen[0] != loop_thread
    # A second read of the same view is the memo, no decode at all.
    await view_of._decode_view_off_loop(big)
    assert len(seen) == 1
    # A small view is decoded on the loop, as before.
    small = view_of.ViewerView(missing=["p"]).to_json()
    await view_of._decode_view_off_loop(small)
    assert seen[-1] == loop_thread
