"""A stopping worker cuts unanswered reads fast; the OpenAPI schema is built once (#2090).

During a rollout a GET that started before SIGTERM used to run for the whole
GRACEFUL_TIMEOUT drain (154 to 180 s seen on ctem.one) before its 503. And a
branding render discarded the cached OpenAPI schema, so the next
``GET /api/openapi.json`` rebuilt it synchronously on the event loop.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import app.main as main_module
from app.core import worker_shutdown
from app.core.utils import is_graceful_shutdown_cancellation
from app.main import GracefulShutdownDrainMiddleware


@pytest.fixture(autouse=True)
def _no_stop_asked(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(worker_shutdown, "_request", None)
    monkeypatch.setattr(worker_shutdown, "_stop_listeners", {})
    monkeypatch.setattr(main_module, "STOP_READ_GRACE_SECONDS", 0.05)
    yield


def _scope(method: str) -> dict[str, Any]:
    return {"type": "http", "method": method, "path": "/api/v1/ctem/cases", "headers": []}


async def _receive() -> Message:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def _slow_app(started: asyncio.Event, *, answer_first: bool = False) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if answer_first:
            await send({"type": "http.response.start", "status": 200, "headers": []})
        started.set()
        await asyncio.sleep(5)
        await send({"type": "http.response.body", "body": b"late", "more_body": False})

    return app


def _collector(sent: list[Message]) -> Send:
    async def send(message: Message) -> None:
        sent.append(message)

    return send


def test_the_stop_cut_message_reads_as_a_platform_stop() -> None:
    assert is_graceful_shutdown_cancellation(asyncio.CancelledError(main_module.STOP_READ_CANCEL_MESSAGE))


async def test_an_unanswered_read_gets_503_soon_after_the_stop() -> None:
    started = asyncio.Event()
    sent: list[Message] = []
    middleware = GracefulShutdownDrainMiddleware(_slow_app(started))
    task = asyncio.create_task(middleware(_scope("GET"), _receive, _collector(sent)))
    await started.wait()
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")

    await asyncio.wait_for(task, timeout=2)
    assert not task.cancelled()
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 503
    assert (b"retry-after", b"1") in sent[0]["headers"]


async def test_a_read_that_starts_after_the_stop_is_cut_too() -> None:
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")
    started = asyncio.Event()
    sent: list[Message] = []
    middleware = GracefulShutdownDrainMiddleware(_slow_app(started))
    task = asyncio.create_task(middleware(_scope("HEAD"), _receive, _collector(sent)))

    await asyncio.wait_for(task, timeout=2)
    assert sent[0]["status"] == 503


async def test_a_write_keeps_the_full_drain() -> None:
    started = asyncio.Event()
    sent: list[Message] = []
    middleware = GracefulShutdownDrainMiddleware(_slow_app(started))
    task = asyncio.create_task(middleware(_scope("POST"), _receive, _collector(sent)))
    await started.wait()
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")
    await asyncio.sleep(0.3)

    assert not task.done()
    assert sent == []
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_read_whose_answer_started_is_not_cut() -> None:
    started = asyncio.Event()
    sent: list[Message] = []
    middleware = GracefulShutdownDrainMiddleware(_slow_app(started, answer_first=True))
    task = asyncio.create_task(middleware(_scope("GET"), _receive, _collector(sent)))
    await started.wait()
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")
    await asyncio.sleep(0.3)

    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_finished_request_leaves_no_stop_listener() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    await GracefulShutdownDrainMiddleware(app)(_scope("GET"), _receive, _collector([]))
    assert worker_shutdown._stop_listeners == {}


def _keep_branding(monkeypatch: pytest.MonkeyPatch) -> None:
    application = main_module.application
    monkeypatch.setattr(application, "title", application.title)
    monkeypatch.setattr(application, "description", application.description)


def test_a_branding_render_keeps_the_built_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, Any] = {"openapi": "3.1.0", "info": {"title": "Old", "version": "1"}, "paths": {"/x": {}}}
    monkeypatch.setattr(main_module.application, "openapi_schema", built)
    _keep_branding(monkeypatch)

    main_module._refresh_openapi_branding()

    schema = main_module.application.openapi_schema
    assert schema is not None
    assert schema["paths"] == {"/x": {}}
    assert schema["info"]["title"] == main_module.application.title
    assert schema["info"]["description"] == main_module.application.description
    assert schema["info"]["version"] == "1"


async def test_the_schema_is_built_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.application, "openapi_schema", None)
    _keep_branding(monkeypatch)
    on_loop: list[bool] = []

    def fake_openapi() -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        schema: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {"title": "t", "version": "1"},
            "paths": {},
        }
        main_module.application.openapi_schema = schema
        return schema

    monkeypatch.setattr(main_module.application, "openapi", fake_openapi)
    await main_module._prebuild_openapi_schema()

    assert on_loop == [False]
    schema = main_module.application.openapi_schema
    assert schema is not None
    assert schema["info"]["title"] == main_module.application.title
